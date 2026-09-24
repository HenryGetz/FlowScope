#!/usr/bin/env python3
"""FlowScope C7 — overnight CFD batch orchestrator and report writer.

Drives the FlowScope C1–C5 CLIs as subprocesses (this script implements no
numerics) and writes out/cfd_overnight_report.md + out/cfd_overnight_report.json
(machine-readable twin) per contract C7 in local://phase2_contracts.md.

Per case, in order (child env pins OMP/MKL/OPENBLAS_NUM_THREADS=8):
  1. phase2/rom/build_network_graph.py --cases {case}
  2. phase2/rom/run_hemodynamics.py --cases {case}
  3. phase2/transport/build_carrier.py --cases {case}
  4. phase2/transport/solve_advection.py --cases {case} --profiles ...
  5. tools/export_cfd_payload.py --case {case} --profile {p}   (per profile)

Keep-going semantics are ALWAYS on: a failing stage is recorded (ISO-timestamped
record appended to out/cfd_errors.log) and the batch continues; later stages are
skipped with a reason when their intermediate artifacts are missing. Child
stdout/stderr is captured to out/cfd_logs/{case}_{stage}.log. Reports are
rewritten atomically after every case, so an interrupted run leaves consistent,
already-written reports behind.

After the batch a best-effort load-time benchmark runs (skipped under
--skip-probe): (a) site benchmark — npm ci/cert as needed, npm run build, a
local HTTPS viewer server, then node tools/loadtime_probe.mjs --samples 3 with
its per-resource min/median/max parsed from stdout; (b) payload benchmark —
every exported case_*_{profile}_contrast.bin + metadata fetched over the same
base 3x, timing ttfb/download/total with time.perf_counter. Every benchmark
failure becomes a recorded string (load_time_benchmark.unavailable_reason and
its sub-objects) and never aborts the run.
"""

from __future__ import annotations

import os

# Machine spec: pin math-library threading before any numpy import (none here;
# children inherit this env as well and get it forced in child_env()).
for _thread_var in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS"):
    os.environ[_thread_var] = "8"

import argparse
import json
import shlex
import shutil
import signal
import ssl
import statistics
import subprocess
import sys
import time
import traceback
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

TOOL_NAME = "tools/run_cfd_overnight.py"
TOOL_VERSION = "0.1.0"
PROBE_BASE_DEFAULT = "https://127.0.0.1:8443"
PROBE_SAMPLES = 3
STDERR_TAIL_CHARS = 2000
PROFILE_CHOICES = ("A", "B", "C")

SECTION_MESH = "mesh triangle count and branch count"
SECTION_ROM = "1D ROM solve duration (s) and mass balance conservation"
SECTION_TRANSPORT = "3D scalar transport solve duration (s) and maximum numerical residual"
SECTION_TRANSIT = "distal transit times (ms) under Profiles A, B, and C"
SECTION_PAYLOAD = "final WebXR payload size (MB) and load-time benchmark"
SECTION_FAILURES = "failures"

REPO_ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #

def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def round3(value: float) -> float:
    return round(value, 3)


def repo_path(rel: str | Path) -> Path:
    p = Path(rel)
    return p if p.is_absolute() else REPO_ROOT / p


def rel_str(rel: str | Path) -> str:
    """Human-facing repo-relative (posix) form of a path as used in commands."""
    return Path(rel).as_posix()


def display_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPO_ROOT.resolve()).as_posix()
    except ValueError:
        return str(path)


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def load_json(path: Path):
    """Return (obj, None) or (None, reason-string). Never raises."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh), None
    except FileNotFoundError:
        return None, f"missing artifact {display_path(path)}"
    except json.JSONDecodeError as exc:
        return None, f"invalid JSON in {display_path(path)}: {exc}"
    except OSError as exc:
        return None, f"cannot read {display_path(path)}: {exc}"


def dget(obj, dotted: str, default=None):
    cur = obj
    for key in dotted.split("."):
        if isinstance(cur, dict) and key in cur:
            cur = cur[key]
        else:
            return default
    return cur


def join_reasons(*reasons) -> str | None:
    parts = [r for r in reasons if r]
    return "; ".join(parts) if parts else None


def fnum(value, nd: int = 6) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        return f"{value:.{nd}g}"
    return str(value)


def fms(value) -> str:
    return "n/a" if value is None else f"{fnum(value, 6)} ms"


def ftriple(stats) -> str:
    if not isinstance(stats, dict):
        return "n/a"
    if stats.get("min") is None and stats.get("median") is None and stats.get("max") is None:
        return "n/a"
    return f"{fnum(stats.get('min'))} / {fnum(stats.get('median'))} / {fnum(stats.get('max'))}"


def stat_summary(values):
    ok = sorted(v for v in values if isinstance(v, (int, float)))
    if not ok:
        return {"min": None, "median": None, "max": None, "samples": []}
    return {
        "min": ok[0],
        "median": round3(float(statistics.median(ok))),
        "max": ok[-1],
        "samples": ok,
    }


def stat_trim(stats):
    if isinstance(stats, dict):
        return {
            "min": stats.get("min"),
            "median": stats.get("median"),
            "max": stats.get("max"),
            "samples": stats.get("samples", []),
        }
    return {"min": None, "median": None, "max": None, "samples": []}


# --------------------------------------------------------------------------- #
# subprocess plumbing
# --------------------------------------------------------------------------- #

def child_env() -> dict:
    env = dict(os.environ)
    for key in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS"):
        env[key] = "8"
    return env


def run_logged(argv: list[str], log_path: Path, log_header: str,
               cwd: str | Path | None = None) -> dict:
    """Run a child process, capture stdout/stderr to log_path.

    `cwd` defaults to the repo root (stage CLIs and the node probe run there);
    npm invocations pass the viewer directory (npm needs its package.json).

    Returns a record with command/exit_code/runtime_s/stdout/stderr/stderr_tail
    (or error for spawn failures). Never raises.
    """
    command = shlex.join(argv)
    record = {
        "command": command,
        "argv": argv,
        "log": display_path(log_path),
        "exit_code": None,
        "runtime_s": None,
        "error": None,
        "stdout": "",
        "stderr": "",
        "stderr_tail": "",
        "started": iso_now(),
    }
    t0 = time.perf_counter()
    try:
        proc = subprocess.run(
            argv,
            cwd=str(cwd) if cwd is not None else str(REPO_ROOT),
            env=child_env(),
            capture_output=True,
            text=True,
            errors="replace",
        )
        record["exit_code"] = proc.returncode
        record["stdout"] = proc.stdout or ""
        record["stderr"] = proc.stderr or ""
    except OSError as exc:
        record["error"] = f"{type(exc).__name__}: {exc}"
        record["stderr"] = record["error"]
    record["runtime_s"] = round3(time.perf_counter() - t0)
    record["stderr_tail"] = record["stderr"][-STDERR_TAIL_CHARS:]
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "w", encoding="utf-8") as fh:
        fh.write(f"# {log_header}\n")
        fh.write(f"# command: {command}\n")
        fh.write(f"# started: {record['started']}\n")
        fh.write(f"# exit_code: {record['exit_code']}\n")
        if record["error"]:
            fh.write(f"# spawn_error: {record['error']}\n")
        fh.write("----- stdout -----\n")
        fh.write(record["stdout"])
        if record["stdout"] and not record["stdout"].endswith("\n"):
            fh.write("\n")
        fh.write("----- stderr -----\n")
        fh.write(record["stderr"])
        if record["stderr"] and not record["stderr"].endswith("\n"):
            fh.write("\n")
    return record


def error_record_text(rec: dict) -> str:
    lines = [
        f"[{rec['timestamp']}] case={rec['case']} stage={rec['stage']} "
        f"exit_code={rec['exit_code']}",
        f"command: {rec.get('command') or '(none)'}",
    ]
    if rec.get("exception"):
        lines.append(f"exception: {rec['exception']}")
    if rec.get("traceback"):
        lines.append("traceback:")
        lines.append(rec["traceback"].rstrip())
    lines.append("stderr_tail:")
    lines.append(rec.get("stderr_tail") or "(empty)")
    return "\n".join(lines) + "\n"


class ErrorLog:
    """Append-only failure log; created lazily — only when a failure occurs."""

    def __init__(self, path: Path):
        self.path = path
        self.records: list[dict] = []

    def append(self, rec: dict) -> None:
        rec = dict(rec)
        rec.setdefault("timestamp", iso_now())
        text = error_record_text(rec)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(text + "\n")
            fh.flush()
        rec["record_text"] = text
        self.records.append(rec)


# --------------------------------------------------------------------------- #
# per-case pipeline
# --------------------------------------------------------------------------- #

@dataclass
class Stage:
    name: str
    argv: list[str]
    prereqs: list[str] = field(default_factory=list)     # repo-relative inputs
    produces: list[str] = field(default_factory=list)    # repo-relative outputs


def build_stages(case: str, profiles: list[str], out_dir: str, python: str) -> list[Stage]:
    o = rel_str(out_dir)
    network = f"{o}/rom/networks/{case}_network.json"
    graph = f"{o}/rom/graphs/{case}_graph.json"
    hemo = f"{o}/rom/hemodynamics/{case}/hemodynamics.json"
    carrier_npz = f"{o}/transport/carrier/{case}_carrier.npz"
    carrier_json = f"{o}/transport/carrier/{case}_carrier.json"
    glb = f"{o}/coronary_{case}.glb"
    stages = [
        Stage(
            name="graph",
            argv=[python, "phase2/rom/build_network_graph.py", "--cases", case,
                  "--networks-dir", f"{o}/rom/networks", "--out-dir", f"{o}/rom/graphs"],
            prereqs=[network],
            produces=[graph],
        ),
        Stage(
            name="hemodynamics",
            argv=[python, "phase2/rom/run_hemodynamics.py", "--cases", case,
                  "--graph-dir", f"{o}/rom/graphs", "--out-dir", f"{o}/rom/hemodynamics"],
            prereqs=[graph],
            produces=[hemo],
        ),
        Stage(
            name="carrier",
            argv=[python, "phase2/transport/build_carrier.py", "--cases", case,
                  "--graph-dir", f"{o}/rom/graphs", "--out-dir", f"{o}/transport/carrier"],
            prereqs=[graph],
            produces=[carrier_npz, carrier_json],
        ),
        Stage(
            name="advection",
            argv=[python, "phase2/transport/solve_advection.py", "--cases", case,
                  "--profiles", *profiles,
                  "--carrier-dir", f"{o}/transport/carrier",
                  "--hemo-dir", f"{o}/rom/hemodynamics",
                  "--out-dir", f"{o}/transport/contrast"],
            prereqs=[carrier_npz, carrier_json, hemo],
            produces=[f"{o}/transport/contrast/{case}_{p}.{ext}"
                      for p in profiles for ext in ("npz", "json")],
        ),
    ]
    for profile in profiles:
        stages.append(Stage(
            name=f"export_{profile}",
            argv=[python, "tools/export_cfd_payload.py", "--case", case, "--profile", profile,
                  "--glb", glb, "--contrast-dir", f"{o}/transport/contrast",
                  "--hemo-dir", f"{o}/rom/hemodynamics",
                  "--assets-dir", "viewer/public/assets"],
            prereqs=[f"{o}/transport/contrast/{case}_{profile}.npz",
                     f"{o}/transport/contrast/{case}_{profile}.json", hemo, glb],
            produces=[f"viewer/public/assets/case_{case}_{profile}_contrast.bin",
                      f"viewer/public/assets/case_{case}_{profile}_metadata.json"],
        ))
    return stages


def run_case(case: str, profiles: list[str], out_dir: str, python: str,
             logs_dir: Path, error_log: ErrorLog) -> dict:
    stages = build_stages(case, profiles, out_dir, python)
    producer: dict[str, str] = {}
    for stage in stages:
        for artifact in stage.produces:
            producer.setdefault(artifact, stage.name)
    stage_status: dict[str, str] = {}
    rows: list[dict] = []
    failing_stage = None

    for stage in stages:
        row = {
            "stage": stage.name,
            "command": shlex.join(stage.argv),
            "status": "skipped",
            "exit_code": None,
            "runtime_s": None,
            "log": None,
            "reason": None,
        }
        missing = [rel_str(a) for a in stage.prereqs if not repo_path(a).is_file()]
        if missing:
            notes = []
            for artifact in missing:
                src = producer.get(artifact)
                if src and stage_status.get(src) not in (None, "ok"):
                    notes.append(f"{artifact} (producing stage {src}: {stage_status[src]})")
                else:
                    notes.append(artifact)
            row["reason"] = "missing intermediate artifact(s): " + ", ".join(notes)
        else:
            log_path = logs_dir / f"{case}_{stage.name}.log"
            record = run_logged(stage.argv, log_path, f"{TOOL_NAME} stage={stage.name} case={case}")
            row["exit_code"] = record["exit_code"]
            row["runtime_s"] = record["runtime_s"]
            row["log"] = record["log"]
            if record["error"] is not None:
                row["status"] = "failed"
                row["reason"] = f"spawn failed: {record['error']}"
                error_log.append({
                    "case": case,
                    "stage": stage.name,
                    "command": record["command"],
                    "exit_code": None,
                    "stderr_tail": record["stderr_tail"],
                    "exception": record["error"],
                })
            elif record["exit_code"] != 0:
                row["status"] = "failed"
                row["reason"] = f"exit code {record['exit_code']}"
                error_log.append({
                    "case": case,
                    "stage": stage.name,
                    "command": record["command"],
                    "exit_code": record["exit_code"],
                    "stderr_tail": record["stderr_tail"],
                })
            else:
                row["status"] = "ok"
        stage_status[stage.name] = row["status"]
        if row["status"] == "failed" and failing_stage is None:
            failing_stage = stage.name
        rows.append(row)

    if failing_stage is not None:
        status = "failed"
    elif any(r["status"] == "skipped" for r in rows):
        status = "incomplete"
    else:
        status = "ok"
    return {
        "case": case,
        "status": status,
        "failing_stage": failing_stage,
        "runtime_s": round3(sum(r["runtime_s"] or 0.0 for r in rows)),
        "stages": rows,
    }


# --------------------------------------------------------------------------- #
# metrics (parsed from stage artifacts — never recomputed)
# --------------------------------------------------------------------------- #

def triangles_from_meta(meta) -> int | None:
    value = meta.get("triangles") if isinstance(meta, dict) else None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    meshes = meta.get("meshes") if isinstance(meta, dict) else None
    if isinstance(meshes, list) and meshes:
        counts = [m.get("triangles") for m in meshes if isinstance(m, dict)]
        if counts and all(isinstance(c, (int, float)) and not isinstance(c, bool) for c in counts):
            return int(sum(counts))
    return None


def collect_metrics(case: str, profiles: list[str], out_dir: str) -> dict:
    o = rel_str(out_dir)
    graph_j, graph_reason = load_json(repo_path(f"{o}/rom/graphs/{case}_graph.json"))
    hemo_j, hemo_reason = load_json(repo_path(f"{o}/rom/hemodynamics/{case}/hemodynamics.json"))
    meta = {}
    contrast = {}
    for profile in profiles:
        meta[profile] = load_json(
            repo_path(f"viewer/public/assets/case_{case}_{profile}_metadata.json"))
        contrast[profile] = load_json(
            repo_path(f"{o}/transport/contrast/{case}_{profile}.json"))

    # --- mesh triangle count and branch count (C1 stats + C5 metadata) --------
    mesh = {
        "triangles": None, "branch_count": None, "n_nodes": None, "n_terminals": None,
        "n_roots": None, "total_length_mm": None, "unavailable_reason": None,
    }
    reasons = []
    if graph_j is not None:
        stats = graph_j.get("stats") if isinstance(graph_j.get("stats"), dict) else {}
        mesh["branch_count"] = stats.get("n_edges")
        mesh["n_nodes"] = stats.get("n_nodes")
        mesh["n_terminals"] = stats.get("n_terminals")
        mesh["n_roots"] = stats.get("n_roots")
        mesh["total_length_mm"] = stats.get("total_length_mm")
        if mesh["branch_count"] is None:
            reasons.append(f"branch_count unavailable: no stats.n_edges in {o}/rom/graphs/{case}_graph.json")
    else:
        reasons.append(f"branch_count unavailable: {graph_reason}")
    tri_reasons = []
    for profile in profiles:
        meta_obj, meta_reason = meta[profile]
        if meta_obj is None:
            tri_reasons.append(meta_reason or f"no metadata for profile {profile}")
            continue
        mesh["triangles"] = triangles_from_meta(meta_obj)
        if mesh["triangles"] is not None:
            break
        tri_reasons.append(
            f"no 'triangles' field in viewer/public/assets/"
            f"case_{case}_{profile}_metadata.json")
    if mesh["triangles"] is None:
        reasons.append("triangles unavailable: "
                       + (", ".join(tri_reasons) or f"no metadata for profiles {profiles}"))
    mesh["unavailable_reason"] = join_reasons(*reasons)

    # --- 1D ROM solve duration + mass balance (C2) ---------------------------
    rom = {
        "solve_s": dget(hemo_j, "timings.solve_s"),
        "transport_1d_s": dget(hemo_j, "timings.transport_1d_s"),
        "total_s": dget(hemo_j, "timings.total_s"),
        "mass_balance": {},
        "unavailable_reason": None,
    }
    reasons = []
    if hemo_j is None:
        reasons.append(f"solve_s unavailable: {hemo_reason}")
    elif rom["solve_s"] is None:
        reasons.append(f"solve_s unavailable: no timings.solve_s in {o}/rom/hemodynamics/{case}/hemodynamics.json")
    for profile in profiles:
        rel_error = dget(hemo_j, f"mass_balance.{profile}.rel_error")
        method = dget(hemo_j, f"mass_balance.{profile}.method")
        rom["mass_balance"][profile] = {"rel_error": rel_error, "method": method}
        if rel_error is None:
            reasons.append(f"mass_balance.{profile}.rel_error unavailable: "
                           f"{hemo_reason or f'no mass_balance.{profile} in hemodynamics.json'}")
    rom["unavailable_reason"] = join_reasons(*reasons)

    # --- 3D scalar transport duration + max residual (C4) --------------------
    transport = {"profiles": {}, "solve_s_total": None, "eps_mass_max": None,
                 "unavailable_reason": None}
    reasons = []
    runtimes = []
    residuals = []
    for profile in profiles:
        c_obj, c_reason = contrast[profile]
        entry = {
            "runtime_s": dget(c_obj, "runtime_s"),
            "eps_mass_max": dget(c_obj, "mass.eps_mass_max"),
            "dt_s_mean": dget(c_obj, "dt_s_mean"),
            "pe": dget(c_obj, "pe"),
            "cfl": dget(c_obj, "cfl"),
            "verdict": dget(c_obj, "verdict"),
            "unavailable_reason": None,
        }
        preason = []
        if c_obj is None:
            preason.append(c_reason)
        else:
            if entry["runtime_s"] is None:
                preason.append(f"no runtime_s in {o}/transport/contrast/{case}_{profile}.json")
            if entry["eps_mass_max"] is None:
                preason.append(f"no mass.eps_mass_max in {o}/transport/contrast/{case}_{profile}.json")
        entry["unavailable_reason"] = join_reasons(*preason)
        if entry["runtime_s"] is not None:
            runtimes.append(entry["runtime_s"])
        if entry["eps_mass_max"] is not None:
            residuals.append(entry["eps_mass_max"])
        reasons.extend(preason)
        transport["profiles"][profile] = entry
    if runtimes:
        transport["solve_s_total"] = round3(sum(runtimes))
    if residuals:
        transport["eps_mass_max"] = max(residuals)
    transport["unavailable_reason"] = join_reasons(*reasons)

    # --- distal transit times (C5 branches, C2 signals fallback) -------------
    transit = {}
    for profile in profiles:
        transit[profile] = transit_rows(case, profile, meta[profile][0], hemo_j, graph_j,
                                        meta_reason=meta[profile][1], hemo_reason=hemo_reason,
                                        graph_reason=graph_reason)

    # --- WebXR payload size (C5 metadata) ------------------------------------
    payload = {}
    for profile in profiles:
        meta_obj, meta_reason = meta[profile]
        entry = {
            "total_mb": dget(meta_obj, "total_mb"),
            "frames": dget(meta_obj, "frames"),
            "vertices": dget(meta_obj, "vertices"),
            "bin_file": dget(meta_obj, "bin_file"),
            "unavailable_reason": None,
        }
        if entry["total_mb"] is None:
            preason = (meta_reason or
                       f"no total_mb in viewer/public/assets/case_{case}_{profile}_metadata.json")
            entry["unavailable_reason"] = f"payload size unavailable: {preason}"
        payload[profile] = entry

    return {
        "mesh": mesh,
        "rom_1d": rom,
        "transport_3d": transport,
        "transit_ms": transit,
        "payload": payload,
        "load_time_benchmark": pending_benchmark_view(),
    }


def pending_benchmark_view() -> dict:
    return {
        "unavailable_reason": "load-time benchmark has not run yet",
        "site": None,
        "payloads": {"unavailable_reason": "not run", "measurements": []},
    }


def empty_metrics(profiles: list[str], reason: str) -> dict:
    """Full-shape metrics placeholder used when metric collection itself fails."""
    return {
        "mesh": {"triangles": None, "branch_count": None, "n_nodes": None,
                 "n_terminals": None, "n_roots": None, "total_length_mm": None,
                 "unavailable_reason": reason},
        "rom_1d": {"solve_s": None, "transport_1d_s": None, "total_s": None,
                   "mass_balance": {p: {"rel_error": None, "method": None}
                                    for p in profiles},
                   "unavailable_reason": reason},
        "transport_3d": {
            "profiles": {p: {"runtime_s": None, "eps_mass_max": None, "dt_s_mean": None,
                             "pe": None, "cfl": None, "verdict": None,
                             "unavailable_reason": reason} for p in profiles},
            "solve_s_total": None, "eps_mass_max": None, "unavailable_reason": reason},
        "transit_ms": {p: {"branches": [], "unavailable_reason": reason}
                       for p in profiles},
        "payload": {p: {"total_mb": None, "frames": None, "vertices": None,
                        "bin_file": None, "unavailable_reason": reason}
                    for p in profiles},
        "load_time_benchmark": pending_benchmark_view(),
    }


def transit_rows(case: str, profile: str, meta_obj, hemo_j, graph_j, *,
                 meta_reason, hemo_reason, graph_reason) -> dict:
    out = {"branches": [], "unavailable_reason": None}
    if isinstance(meta_obj, dict) and isinstance(meta_obj.get("branches"), list):
        rows = []
        for branch in meta_obj["branches"]:
            if not isinstance(branch, dict) or not branch.get("distal"):
                continue
            rows.append({
                "name": branch.get("name"),
                "transit_ms": branch.get("transit_ms"),
                "t_arrival_ms": branch.get("t_arrival_ms"),
                "t_peak_ms": branch.get("t_peak_ms"),
                "timi_frames_30fps": branch.get("timi_frames_30fps"),
            })
        out["branches"] = rows
        if not rows and meta_obj["branches"]:
            out["unavailable_reason"] = (
                f"no branches flagged distal in viewer/public/assets/"
                f"case_{case}_{profile}_metadata.json")
        return out
    # fallback: C2 signals.<profile>.branches restricted to C1 distal edges
    signals = dget(hemo_j, f"signals.{profile}.branches")
    distal_names = set()
    if isinstance(graph_j, dict):
        for edge in graph_j.get("edges", []):
            if isinstance(edge, dict) and dget(edge, "flags.distal"):
                distal_names.add(edge.get("name"))
    if isinstance(signals, dict) and distal_names:
        rows = []
        for name in sorted(signals):
            if name not in distal_names:
                continue
            branch = signals[name]
            arrival_s = branch.get("t_arrival_s")
            peak_s = branch.get("t_peak_s")
            rows.append({
                "name": name,
                "transit_ms": branch.get("transit_ms"),
                "t_arrival_ms": round3(arrival_s * 1000.0) if isinstance(arrival_s, (int, float)) else None,
                "t_peak_ms": round3(peak_s * 1000.0) if isinstance(peak_s, (int, float)) else None,
                "timi_frames_30fps": branch.get("timi_frames_30fps"),
            })
        out["branches"] = rows
        return out
    out["unavailable_reason"] = join_reasons(
        f"metadata branches unavailable: {meta_reason}"
        if meta_reason else "metadata has no 'branches' list",
        None if isinstance(signals, dict) else f"signals.{profile}.branches unavailable: {hemo_reason}",
        None if distal_names else f"distal edge flags unavailable: {graph_reason or 'graph has no distal flags'}",
    ) or "no distal transit source available"
    return out


# --------------------------------------------------------------------------- #
# load-time benchmark (best-effort; every failure -> recorded string)
# --------------------------------------------------------------------------- #

def timed_fetch(url: str, tls_ctx) -> dict:
    request = urllib.request.Request(
        url, headers={"accept-encoding": "identity", "connection": "close"})
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=30, context=tls_ctx) as response:
            t_headers = time.perf_counter()
            body = response.read()
            t_end = time.perf_counter()
        return {
            "ttfb_ms": round3((t_headers - t0) * 1000.0),
            "download_ms": round3((t_end - t_headers) * 1000.0),
            "total_ms": round3((t_end - t0) * 1000.0),
            "bytes": len(body),
            "error": None,
        }
    except Exception as exc:  # noqa: BLE001 — benchmark rows record any failure
        return {
            "ttfb_ms": None, "download_ms": None, "total_ms": None, "bytes": 0,
            "error": f"{type(exc).__name__}: {exc}",
        }


def wait_ready(base: str, tls_ctx, proc, timeout_s: float = 20.0) -> bool:
    deadline = time.perf_counter() + timeout_s
    while time.perf_counter() < deadline:
        if proc is not None and proc.poll() is not None:
            return False
        probe = timed_fetch(base + "/", tls_ctx)
        if probe["error"] is None or probe["error"].startswith("HTTPError"):
            return True
        time.sleep(0.25)
    return False


def stop_server(proc) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def site_resources_from_probe(doc: dict) -> list[dict]:
    resources = []
    for row in doc.get("resources", []):
        if not isinstance(row, dict):
            continue
        resources.append({
            "name": row.get("name"),
            "url": row.get("url"),
            "kind": row.get("kind"),
            "bytes": row.get("bytes"),
            "samples_ok": row.get("samples_ok"),
            "errors": row.get("errors"),
            "ttfb_ms": stat_trim(row.get("ttfb_ms")),
            "download_ms": stat_trim(row.get("download_ms")),
            "total_ms": stat_trim(row.get("total_ms")),
        })
    return resources


def payload_measurements(case: str, profiles: list[str], base: str, tls_ctx,
                         logs_dir: Path) -> list[dict]:
    rows = []
    for profile in profiles:
        for suffix in ("contrast.bin", "metadata.json"):
            name = f"case_{case}_{profile}_{suffix}"
            local = repo_path(f"viewer/public/assets/{name}")
            url = f"{base}/assets/{name}"
            row = {"case": case, "profile": profile, "name": name, "url": url,
                   "bytes": 0, "samples": [], "ttfb_ms": None, "download_ms": None,
                   "total_ms": None, "error": None, "unavailable_reason": None}
            if not local.is_file():
                row["unavailable_reason"] = (
                    f"local file {display_path(local)} missing (export skipped or failed)")
                rows.append(row)
                continue
            samples = [timed_fetch(url, tls_ctx) for _ in range(PROBE_SAMPLES)]
            row["samples"] = samples
            ok = [s for s in samples if s["error"] is None]
            row["bytes"] = ok[0]["bytes"] if ok else 0
            row["ttfb_ms"] = stat_summary([s["ttfb_ms"] for s in ok])
            row["download_ms"] = stat_summary([s["download_ms"] for s in ok])
            row["total_ms"] = stat_summary([s["total_ms"] for s in ok])
            errors = [s["error"] for s in samples if s["error"]]
            if errors:
                row["error"] = "; ".join(sorted(set(errors)))
            rows.append(row)
    log_path = logs_dir / "probe_payloads.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "w", encoding="utf-8") as fh:
        fh.write(f"# {TOOL_NAME} payload load-time benchmark ({PROBE_SAMPLES} samples per URL)\n")
        fh.write(f"# base: {base}\n")
        fh.write(json.dumps(rows, indent=2))
        fh.write("\n")
    return rows


def run_loadtime_benchmark(args, case_profiles: dict[str, list[str]],
                           logs_dir: Path) -> dict:
    benchmark = {
        "unavailable_reason": None,
        "site": {
            "unavailable_reason": None,
            "base": args.probe_base,
            "samples_per_resource": PROBE_SAMPLES,
            "resources": [],
        },
        "payloads": {"unavailable_reason": None, "measurements": []},
    }

    def fail(reason: str, *, site: bool = True, payloads: bool = True) -> None:
        if site:
            benchmark["site"]["unavailable_reason"] = reason
        if payloads:
            benchmark["payloads"]["unavailable_reason"] = reason

    if args.skip_probe:
        fail("skipped via --skip-probe")
        benchmark["unavailable_reason"] = "skipped via --skip-probe"
        return benchmark

    try:
        node = shutil.which("node")
        if node is None:
            fail("node not found on PATH (needed for viewer/serve-https.mjs and "
                 "tools/loadtime_probe.mjs)")
        elif not repo_path("viewer/serve-https.mjs").is_file():
            fail("missing viewer/serve-https.mjs")
        else:
            tls_ctx = ssl._create_unverified_context()  # self-signed LAN/WebXR path
            stop = {"reason": None}

            def prepare_site() -> bool:
                npm = shutil.which("npm")
                if not repo_path("viewer/node_modules").is_dir():
                    if npm is None:
                        stop["reason"] = "npm not found on PATH (needed: npm ci)"
                        return False
                    rec = run_logged([npm, "ci"], logs_dir / "probe_npm_ci.log",
                                     f"{TOOL_NAME} load-time benchmark: npm ci",
                                     cwd=repo_path("viewer"))
                    if rec["exit_code"] != 0:
                        stop["reason"] = (f"npm ci failed (exit {rec['exit_code']}); "
                                          f"see {rec['log']}")
                        return False
                cert = repo_path("viewer/certs/cert.pem")
                key = repo_path("viewer/certs/key.pem")
                if not (cert.is_file() and key.is_file()):
                    if npm is None:
                        stop["reason"] = "npm not found on PATH (needed: npm run cert)"
                        return False
                    rec = run_logged([npm, "run", "cert"], logs_dir / "probe_cert.log",
                                     f"{TOOL_NAME} load-time benchmark: npm run cert",
                                     cwd=repo_path("viewer"))
                    if rec["exit_code"] != 0:
                        stop["reason"] = (f"npm run cert failed (exit {rec['exit_code']}); "
                                          f"see {rec['log']}")
                        return False
                if npm is None:
                    stop["reason"] = "npm not found on PATH (needed: npm run build)"
                    return False
                rec = run_logged([npm, "run", "build"], logs_dir / "probe_build.log",
                                 f"{TOOL_NAME} load-time benchmark: npm run build",
                                 cwd=repo_path("viewer"))
                if rec["exit_code"] != 0:
                    stop["reason"] = (f"npm run build failed (exit {rec['exit_code']}); "
                                      f"see {rec['log']}")
                    return False
                return True

            if not prepare_site():
                fail(stop["reason"])
            else:
                # The local server must bind the endpoint the probe and payload
                # fetches actually hit (--probe-base); a hardcoded bind would
                # benchmark a different server whenever the base is overridden.
                base_url = urllib.parse.urlsplit(args.probe_base)
                serve_host = base_url.hostname or "127.0.0.1"
                serve_port = base_url.port or (443 if base_url.scheme == "https" else 80)
                server = None
                try:
                    server = subprocess.Popen(
                        [node, "viewer/serve-https.mjs", "--host", serve_host,
                         "--port", str(serve_port)],
                        cwd=str(REPO_ROOT), env=child_env(),
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                    )
                    if not wait_ready(args.probe_base, tls_ctx, server):
                        code = server.poll()
                        detail = f" (exit {code})" if code is not None else ""
                        fail(f"viewer/serve-https.mjs failed to become ready at "
                             f"{args.probe_base}{detail}")
                    else:
                        # (a) site benchmark via tools/loadtime_probe.mjs
                        rec = run_logged(
                            [node, "tools/loadtime_probe.mjs",
                             "--base", args.probe_base, "--samples", str(PROBE_SAMPLES)],
                            logs_dir / "probe_loadtime.log",
                            f"{TOOL_NAME} load-time benchmark: tools/loadtime_probe.mjs")
                        probe_doc = None
                        if rec["exit_code"] != 0:
                            benchmark["site"]["unavailable_reason"] = (
                                f"tools/loadtime_probe.mjs failed (exit {rec['exit_code']}); "
                                f"see {rec['log']}")
                        else:
                            try:
                                probe_doc = json.loads(rec["stdout"])
                            except json.JSONDecodeError as exc:
                                benchmark["site"]["unavailable_reason"] = (
                                    f"tools/loadtime_probe.mjs emitted invalid JSON: {exc}; "
                                    f"see {rec['log']}")
                        if probe_doc is not None:
                            benchmark["site"]["resources"] = site_resources_from_probe(probe_doc)
                            if not benchmark["site"]["resources"]:
                                benchmark["site"]["unavailable_reason"] = (
                                    "tools/loadtime_probe.mjs reported no resources")
                        # (b) payload benchmark over the same base
                        measurements = []
                        for case, profiles in case_profiles.items():
                            measurements.extend(payload_measurements(
                                case, profiles, args.probe_base, tls_ctx, logs_dir))
                        benchmark["payloads"]["measurements"] = measurements
                        measured = [m for m in measurements if m.get("samples")]
                        if not measured:
                            benchmark["payloads"]["unavailable_reason"] = (
                                "no payload fetched successfully")
                        else:
                            failed = [m for m in measured if m.get("error")]
                            if len(failed) == len(measured):
                                benchmark["payloads"]["unavailable_reason"] = (
                                    "every payload fetch failed: "
                                    + "; ".join(sorted({m["error"] for m in failed if m["error"]})))
                finally:
                    stop_server(server)
    except Exception:  # noqa: BLE001 — benchmark must never abort the run
        fail(f"unexpected benchmark error: {traceback.format_exc(limit=4).strip()}")

    parts = [f"site: {benchmark['site']['unavailable_reason']}"
             if benchmark["site"]["unavailable_reason"] else None,
             f"payloads: {benchmark['payloads']['unavailable_reason']}"
             if benchmark["payloads"]["unavailable_reason"] else None]
    benchmark["unavailable_reason"] = join_reasons(*parts)
    return benchmark


def case_benchmark_view(benchmark: dict, case: str) -> dict:
    payloads = benchmark.get("payloads") or {}
    measurements = [m for m in payloads.get("measurements", []) if m.get("case") == case]
    site = benchmark.get("site")
    return {
        "unavailable_reason": benchmark.get("unavailable_reason"),
        "site": site,
        "payloads": {
            "unavailable_reason": payloads.get("unavailable_reason"),
            "measurements": measurements,
        },
    }


# --------------------------------------------------------------------------- #
# report writers
# --------------------------------------------------------------------------- #

def render_case_markdown(case_rec: dict) -> str:
    m = case_rec["metrics"]
    lines: list[str] = []
    failing = case_rec.get("failing_stage")
    head = f"## Case {case_rec['case']} — {case_rec['status']}"
    if failing:
        head += f" (failing stage: {failing})"
    lines.append(head)
    lines.append("")
    lines.append("| stage | status | exit code | runtime (s) | log | reason |")
    lines.append("|---|---|---|---|---|---|")
    for row in case_rec["stages"]:
        lines.append(
            f"| {row['stage']} | {row['status']} | {fnum(row['exit_code'])} | "
            f"{fnum(row['runtime_s'])} | {row['log'] or '—'} | {row['reason'] or '—'} |")
    lines.append("")

    # --- mesh triangle count and branch count --------------------------------
    mesh = m["mesh"]
    lines.append(f"### {SECTION_MESH}")
    lines.append("")
    lines.append(f"- mesh triangle count: {fnum(mesh['triangles'])}")
    lines.append(f"- branch count: {fnum(mesh['branch_count'])}"
                 f" (nodes: {fnum(mesh['n_nodes'])}, terminals: {fnum(mesh['n_terminals'])},"
                 f" roots: {fnum(mesh['n_roots'])},"
                 f" total centerline length: {fnum(mesh['total_length_mm'])} mm)")
    if mesh["unavailable_reason"]:
        lines.append(f"- unavailable: {mesh['unavailable_reason']}")
    lines.append("")

    # --- 1D ROM solve duration + mass balance --------------------------------
    rom = m["rom_1d"]
    lines.append(f"### {SECTION_ROM}")
    lines.append("")
    lines.append(f"- 1D ROM solve duration: {fnum(rom['solve_s'])} s"
                 + (f" (1D transport: {fnum(rom['transport_1d_s'])} s,"
                    f" total: {fnum(rom['total_s'])} s)"
                    if rom.get("transport_1d_s") is not None or rom.get("total_s") is not None
                    else ""))
    for profile, mb in rom["mass_balance"].items():
        method = f", method: {mb['method']}" if mb.get("method") else ""
        lines.append(f"- mass balance conservation [{profile}]: rel_error "
                     f"{fnum(mb['rel_error'])}{method}")
    if rom["unavailable_reason"]:
        lines.append(f"- unavailable: {rom['unavailable_reason']}")
    lines.append("")

    # --- 3D scalar transport duration + residual -----------------------------
    tr = m["transport_3d"]
    lines.append(f"### {SECTION_TRANSPORT}")
    lines.append("")
    lines.append("| profile | solve duration (s) | max numerical residual (eps_mass_max) "
                 "| dt mean (s) | Pe | CFL | verdict |")
    lines.append("|---|---|---|---|---|---|---|")
    for profile, entry in tr["profiles"].items():
        lines.append(
            f"| {profile} | {fnum(entry['runtime_s'])} | {fnum(entry['eps_mass_max'])} | "
            f"{fnum(entry['dt_s_mean'])} | {fnum(entry['pe'])} | {fnum(entry['cfl'])} | "
            f"{entry['verdict'] or '—'} |")
    lines.append("")
    lines.append(f"- total solve duration across profiles: {fnum(tr['solve_s_total'])} s")
    lines.append(f"- maximum numerical residual across profiles: {fnum(tr['eps_mass_max'])}")
    if tr["unavailable_reason"]:
        lines.append(f"- unavailable: {tr['unavailable_reason']}")
    lines.append("")

    # --- distal transit times ------------------------------------------------
    lines.append(f"### {SECTION_TRANSIT}")
    lines.append("")
    lines.append("| profile | branch | transit (ms) | arrival (ms) | peak (ms) "
                 "| TIMI frames @30 fps |")
    lines.append("|---|---|---|---|---|---|")
    any_row = False
    for profile, entry in m["transit_ms"].items():
        for branch in entry["branches"]:
            any_row = True
            lines.append(
                f"| {profile} | {branch['name']} | {fnum(branch['transit_ms'])} | "
                f"{fnum(branch['t_arrival_ms'])} | {fnum(branch['t_peak_ms'])} | "
                f"{fnum(branch['timi_frames_30fps'])} |")
    if not any_row:
        lines.append(f"| — | — | — | — | — | — |")
    lines.append("")
    for profile, entry in m["transit_ms"].items():
        if entry["unavailable_reason"]:
            lines.append(f"- [{profile}] unavailable: {entry['unavailable_reason']}")
    lines.append("")

    # --- payload size + load-time benchmark ----------------------------------
    pay = m["payload"]
    bench = m["load_time_benchmark"]
    lines.append(f"### {SECTION_PAYLOAD}")
    lines.append("")
    sizes = ", ".join(f"{p}: {fnum(e['total_mb'])} MB" for p, e in pay.items())
    lines.append(f"- final WebXR payload size (bin + metadata json): {sizes}")
    for profile, entry in pay.items():
        if entry["unavailable_reason"]:
            lines.append(f"- [{profile}] {entry['unavailable_reason']}")
    lines.append("")
    if bench.get("unavailable_reason") and not (
            (bench.get("site") or {}).get("resources")
            or (bench.get("payloads") or {}).get("measurements")):
        lines.append(f"- load-time benchmark: unavailable — {bench['unavailable_reason']}")
    else:
        if bench.get("unavailable_reason"):
            lines.append(f"- load-time benchmark partially unavailable — {bench['unavailable_reason']}")
        measurements = (bench.get("payloads") or {}).get("measurements", [])
        lines.append(f"- payload fetch benchmark over {args_base_of(bench)}"
                     f" ({PROBE_SAMPLES} samples each; min / median / max ms):")
        lines.append("")
        lines.append("| payload | bytes | ttfb | download | total |")
        lines.append("|---|---|---|---|---|")
        for row in measurements:
            note = f" — {row['unavailable_reason'] or row['error']}" if (
                row.get("unavailable_reason") or row.get("error")) else ""
            lines.append(
                f"| {row['name']}{note} | {fnum(row['bytes'])} | {ftriple(row['ttfb_ms'])} | "
                f"{ftriple(row['download_ms'])} | {ftriple(row['total_ms'])} |")
        if not measurements:
            lines.append("| — | — | — | — | — |")
        lines.append("")
        site = bench.get("site") or {}
        lines.append(f"- site load-time benchmark (node tools/loadtime_probe.mjs, "
                     f"{PROBE_SAMPLES} samples; min / median / max ms):")
        if site.get("unavailable_reason"):
            lines.append(f"- site benchmark unavailable — {site['unavailable_reason']}")
        lines.append("")
        lines.append("| resource | ttfb | download | total | bytes | ok samples |")
        lines.append("|---|---|---|---|---|---|")
        for row in site.get("resources", []):
            note = f" — {'; '.join(row['errors'])}" if row.get("errors") else ""
            lines.append(
                f"| {row['name']}{note} | {ftriple(row['ttfb_ms'])} | "
                f"{ftriple(row['download_ms'])} | {ftriple(row['total_ms'])} | "
                f"{fnum(row['bytes'])} | {fnum(row['samples_ok'])} |")
        if not site.get("resources"):
            lines.append("| — | — | — | — | — | — |")
    lines.append("")
    return "\n".join(lines)


def args_base_of(bench: dict) -> str:
    return (bench.get("site") or {}).get("base") or PROBE_BASE_DEFAULT


def render_markdown(report: dict) -> str:
    tool = report["tool"]
    lines = [
        "# FlowScope CFD overnight report",
        "",
        f"- generated: {tool['generated']}",
        f"- command: `{tool['command']}`",
        f"- runtime_s: {fnum(tool['runtime_s'])}",
        f"- out_dir: {report['out_dir']}",
        f"- cases: {' '.join(report['cases_requested'])}",
        f"- profiles: {' '.join(report['profiles'])}",
    ]
    if report.get("interrupted"):
        lines.append("- interrupted: true — batch stopped early; sections below cover "
                     "the cases that completed")
    lines.append("")
    for case_rec in report["cases"]:
        lines.append(render_case_markdown(case_rec))
        lines.append("")

    lines.append(f"## {SECTION_FAILURES}")
    lines.append("")
    lines.append(f"Mirrors `out/cfd_errors.log`"
                 f" ({report['out_dir']}/cfd_errors.log).")
    lines.append("")
    if not report["failures"]:
        lines.append("No failures recorded — `cfd_errors.log` was not created.")
    else:
        for rec in report["failures"]:
            lines.append("```text")
            lines.append(error_record_text(rec).rstrip())
            lines.append("```")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def build_report(state: dict) -> dict:
    benchmark = state["load_time_benchmark"]
    for case_rec in state["case_records"]:
        case_rec["metrics"]["load_time_benchmark"] = case_benchmark_view(
            benchmark, case_rec["case"])
    return {
        "schema": "flowscope.cfd.overnight_report",
        "schema_version": 1,
        "tool": {
            "name": TOOL_NAME,
            "version": TOOL_VERSION,
            "command": state["command"],
            "generated": iso_now(),
            "runtime_s": round3(time.perf_counter() - state["t0"]),
        },
        "out_dir": state["out_dir"],
        "cases_requested": state["cases"],
        "profiles": state["profiles"],
        "probe_base": state["probe_base"],
        "skip_probe": state["skip_probe"],
        "interrupted": state["interrupted"],
        "cases": state["case_records"],
        "load_time_benchmark": benchmark,
        "failures": [
            {k: v for k, v in rec.items() if k != "record_text"}
            for rec in state["error_log"].records
        ],
    }


def write_reports(state: dict) -> None:
    report = build_report(state)
    md_path = repo_path(state["out_dir"]) / "cfd_overnight_report.md"
    json_path = repo_path(state["out_dir"]) / "cfd_overnight_report.json"
    atomic_write(md_path, render_markdown(report))
    atomic_write(json_path, json.dumps(report, indent=2) + "\n")


# --------------------------------------------------------------------------- #
# CLI / main
# --------------------------------------------------------------------------- #

def discover_cases(out_dir: str) -> list[str]:
    nets = sorted(repo_path(out_dir).joinpath("rom", "networks").glob("*_network.json"))
    return [p.name[: -len("_network.json")] for p in nets]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=TOOL_NAME,
        description="FlowScope C7: end-to-end overnight CFD batch orchestrator "
                    "(C1->C2->C3->C4->C5 per case) and report writer.")
    parser.add_argument("--cases", nargs="+", metavar="CASE", default=None,
                        help="case ids to run (default: discovered from "
                             "{out-dir}/rom/networks/*_network.json)")
    parser.add_argument("--profiles", nargs="+", metavar="PROFILE",
                        default=list(PROFILE_CHOICES), choices=list(PROFILE_CHOICES),
                        help="injection profiles to solve/export (default: A B C)")
    parser.add_argument("--out-dir", default="out",
                        help="pipeline output root; logs, error log and reports live "
                             "here too (default: out)")
    parser.add_argument("--python", default=sys.executable,
                        help="Python interpreter used to run the stage scripts "
                             "(default: sys.executable)")
    parser.add_argument("--skip-probe", action="store_true",
                        help="skip the load-time benchmark (site + payload fetch)")
    parser.add_argument("--probe-base", default=None, metavar="URL",
                        help=f"base URL for the load-time benchmark "
                             f"(default: {PROBE_BASE_DEFAULT})")
    parser.add_argument("--keep-going", action="store_true",
                        help="accepted for compatibility: the batch ALWAYS keeps going "
                             "past failing cases")
    args = parser.parse_args(argv)
    seen: set[str] = set()
    args.profiles = [p for p in args.profiles
                     if not (p in seen or seen.add(p))]  # dedupe, keep order
    args.probe_base = (args.probe_base or PROBE_BASE_DEFAULT).rstrip("/")
    args.out_dir = rel_str(args.out_dir)
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    def _sigterm(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _sigterm)

    cases = args.cases if args.cases else discover_cases(args.out_dir)
    out_root = repo_path(args.out_dir)
    logs_dir = out_root / "cfd_logs"
    errors_path = out_root / "cfd_errors.log"
    # Idempotent reruns overwrite previous outputs: drop the stale error log up
    # front; it is recreated (append-only) only if this run records a failure.
    if errors_path.is_file():
        errors_path.unlink()

    state = {
        "t0": time.perf_counter(),
        "command": shlex.join([TOOL_NAME, *(argv if argv is not None else sys.argv[1:])]),
        "out_dir": args.out_dir,
        "cases": cases,
        "profiles": args.profiles,
        "probe_base": args.probe_base,
        "skip_probe": args.skip_probe,
        "interrupted": False,
        "case_records": [],
        "error_log": ErrorLog(errors_path),
        "load_time_benchmark": {
            "unavailable_reason": "load-time benchmark did not run (batch incomplete "
                                  "or interrupted)",
            "site": {"unavailable_reason": "not run", "base": args.probe_base,
                     "samples_per_resource": PROBE_SAMPLES, "resources": []},
            "payloads": {"unavailable_reason": "not run", "measurements": []},
        },
    }

    try:
        for case in cases:
            try:
                case_rec = run_case(case, args.profiles, args.out_dir, args.python,
                                    logs_dir, state["error_log"])
            except Exception:  # noqa: BLE001 — one case must not kill the batch
                tb = traceback.format_exc()
                state["error_log"].append({
                    "case": case,
                    "stage": "orchestrator",
                    "command": None,
                    "exit_code": None,
                    "stderr_tail": "",
                    "exception": tb.strip().splitlines()[-1],
                    "traceback": tb,
                })
                case_rec = {
                    "case": case,
                    "status": "failed",
                    "failing_stage": "orchestrator",
                    "runtime_s": None,
                    "stages": [],
                }
            try:
                case_rec["metrics"] = collect_metrics(case, args.profiles, args.out_dir)
            except Exception:  # noqa: BLE001 — metric gaps must not kill the batch
                tb = traceback.format_exc()
                state["error_log"].append({
                    "case": case,
                    "stage": "metrics",
                    "command": None,
                    "exit_code": None,
                    "stderr_tail": "",
                    "exception": tb.strip().splitlines()[-1],
                    "traceback": tb,
                })
                case_rec["metrics"] = empty_metrics(
                    args.profiles, f"metrics collection failed: {tb.strip().splitlines()[-1]}")
                case_rec["status"] = "failed"
                case_rec["failing_stage"] = case_rec.get("failing_stage") or "metrics"
            state["case_records"].append(case_rec)
            write_reports(state)  # incremental + atomic: interrupted runs stay consistent

        case_profiles = {case: args.profiles for case in cases}
        state["load_time_benchmark"] = run_loadtime_benchmark(args, case_profiles, logs_dir)
    except KeyboardInterrupt:
        state["interrupted"] = True
    finally:
        write_reports(state)

    failures = state["error_log"].records
    incomplete = [c for c in state["case_records"] if c["status"] != "ok"]
    if state["interrupted"]:
        return 130
    return 1 if failures or incomplete else 0


if __name__ == "__main__":
    sys.exit(main())
