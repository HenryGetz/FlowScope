#!/usr/bin/env python3
"""FlowScope Phase 2.5 — clinical vFFR / stenosis / ACIST RXi batch runner.

Drives the Phase 2.5 clinical CLIs as subprocesses (this script implements no
physiological numerics; it only aggregates the artifact JSONs they write) and
writes {out-dir}/clinical_hemodynamics_report.md +
{out-dir}/clinical_hemodynamics_report.json (machine-readable twin).

Per case, in order (child env pins OMP/MKL/OPENBLAS_NUM_THREADS=8):
  1. phase2/clinical/stenosis_analyzer.py --cases {case}
       --graph-dir {out}/rom/graphs --out-dir {out}/clinical/lesions
     -> {out}/clinical/lesions/{case}_lesions.json (contract A, intrinsic)
  2. phase2/rom/run_hemodynamics.py --cases {case}
       --graph-dir {out}/rom/graphs --out-dir {out}/rom/hemodynamics
       --lesions <resolved intrinsic lesions file>
     (resolved from {out}/clinical/lesions/{case}_lesions.json — both "{case}_"
     and "case_{case}_" spellings probed at run time ->
      {out}/rom/hemodynamics/{case}/hemodynamics.json with the "clinical"
      block, contract B)
  3. synthetic lesion ladder on the case's longest edge (mid-length
     coordinate, picked from {out}/rom/graphs/{case}_graph.json):
     phase2/clinical/stenosis_analyzer.py --cases {case}
       --graph-dir {out}/rom/graphs --out-dir {out}/clinical/lesions
       --ladder {sev...} --branch {edge} --s {mm}
     -> {out}/clinical/lesions/{case}_lesions_syn{sev}.json (contract A), then
     per severity:
     phase2/rom/run_hemodynamics.py --cases {case}
       --graph-dir {out}/rom/graphs
       --lesions {out}/clinical/lesions/{case}_lesions_syn{sev}.json
       --out-dir {out}/clinical/ladder/{case}_syn{sev}
  4. phase2/clinical/generate_rxi_metrics.py --cases {case}
       --graph-dir {out}/rom/graphs --hemo-dir {out}/rom/hemodynamics
       --lesions-dir {out}/clinical/lesions --out-dir {out}/clinical/rxi
     -> {out}/clinical/rxi/{case}_rxi.json (contract C, ACIST RXi pullback)
  5. tools/export_clinical_payload.py --cases {case}
       --report-dir {out} --graph-dir {out}/rom/graphs
       --hemo-dir {out}/rom/hemodynamics --rxi-dir {out}/clinical/rxi
       --lesions-dir {out}/clinical/lesions --glb-dir viewer/public/assets
       --models-out viewer/models/clinical --public-out viewer/public/clinical
     -> viewer/models/clinical/{case}_clinical.json +
        viewer/public/clinical/{case}_clinical.json (contract D)

Keep-going semantics mirror tools/run_cfd_overnight.py: a failing stage is
recorded (ISO-timestamped record appended to the errors log, default
{out}/clinical_errors.log) and the batch continues; later stages are skipped
with a reason when their intermediate artifacts are missing. Child
stdout/stderr is captured to {out}/clinical_logs/{case}_{stage}.log. Reports
are rewritten atomically after every case, so an interrupted run leaves
consistent, already-written reports behind. Like run_cfd_overnight, a stale
errors log is dropped up front and recreated only if this run records a
failure — a fully green run leaves the errors log absent (--clean makes that
cleanup explicit).

Every number in the reports is compiled from the contract A-D artifact JSONs
on disk; missing or failed stages render as "unavailable: <reason>", never as
placeholder numbers. vFFR decision rule: vFFR <= 0.80 ->
"Revascularization Indicated (vFFR <= 0.80)", else "Normal Flow / Non-Ischemic"
(vFFR bands > 0.80 normal, 0.75-0.80 borderline, < 0.75 severe are context
only and never drive the decision).
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
import signal
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

TOOL_NAME = "tools/run_clinical_batch.py"
TOOL_VERSION = "0.1.0"
REPO_ROOT = Path(__file__).resolve().parent.parent

STDERR_TAIL_CHARS = 2000
DEFAULT_LADDER = (50, 70, 90)

VFFR_THRESHOLD = 0.80
DECISION_INDICATED = "Revascularization Indicated (vFFR <= 0.80)"
DECISION_NORMAL = "Normal Flow / Non-Ischemic"
FLOW_INCREASE_BAND = (2.5, 4.0)
TRANSIT_BAND_S = (1.2, 3.5)
TIMI_TRANSIT_S = 5.0
TIMI_ATTRIB = ("[TIMI 1/2 flagged: severe microvascular resistance — "
               "pathology, not calibration error]")
PAYLOAD_MAX_BYTES = 2 * 1024 * 1024  # contract D: < 2 MB per file

SECTION_SUMMARY = "Clinical summary"
SECTION_LADDER = "Synthetic lesion ladder"
SECTION_TRANSIT = "Transit calibration"
SECTION_VERIFICATION = "Verification"
SECTION_FAILURES = "failures"


# --------------------------------------------------------------------------- #
# small helpers (mirrors tools/run_cfd_overnight.py)
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


def is_num(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def edge_metric(table, edge: str, key: str):
    """table[edge][key] for per-edge contract B tables; None when absent."""
    if isinstance(table, dict):
        entry = table.get(edge)
        if isinstance(entry, dict) and is_num(entry.get(key)):
            return entry[key]
    return None


def per_edge_values(table, edges: list[str], key: str) -> dict:
    return {e: edge_metric(table, e, key) for e in edges
            if edge_metric(table, e, key) is not None}


def cell_value(field_rec: dict, nd: int = 6) -> str:
    value = field_rec.get("value")
    if value is not None:
        return fnum(value, nd)
    return f"unavailable: {field_rec.get('reason') or 'no source artifact'}"


def transit_annotation(flag, raw_s, cal_s) -> str | None:
    """Bracket suffix for a path's transit rendering, driven strictly by
    transit_anomaly[pid].flag: the TIMI 1/2 attribution only when the artifact
    carries a flag; when flag is null but the raw transit exceeds
    TIMI_TRANSIT_S, note instead that velocity calibration addressed the raw
    anomaly; otherwise no suffix."""
    if flag:
        return TIMI_ATTRIB
    if is_num(raw_s) and raw_s > TIMI_TRANSIT_S and is_num(cal_s):
        return (f"[raw anomaly addressed by calibration: raw {raw_s:.2f}s"
                f" -> calibrated {cal_s:.2f}s]")
    return None


def vffr_decision(v):
    if not is_num(v):
        return None
    return DECISION_INDICATED if v <= VFFR_THRESHOLD else DECISION_NORMAL


def vffr_band(v):
    if not is_num(v):
        return None
    if v < 0.75:
        return "severe"
    if v <= VFFR_THRESHOLD:
        return "borderline"
    return "normal"


# --------------------------------------------------------------------------- #
# subprocess plumbing (mirrors tools/run_cfd_overnight.py)
# --------------------------------------------------------------------------- #

def child_env() -> dict:
    env = dict(os.environ)
    for key in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS"):
        env[key] = "8"
    return env


def run_logged(argv: list[str], log_path: Path, log_header: str,
               cwd: str | Path | None = None) -> dict:
    """Run a child process, capture stdout/stderr to log_path.

    `cwd` defaults to the repo root (stage CLIs run there). Returns a record
    with command/exit_code/runtime_s/stdout/stderr/stderr_tail (or error for
    spawn failures). Never raises.
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
# artifact locations (contract A-D producers; dual spellings probed)
# --------------------------------------------------------------------------- #

def graph_rel(o: str, case: str) -> str:
    return f"{o}/rom/graphs/{case}_graph.json"


def hemo_rel(o: str, case: str) -> str:
    return f"{o}/rom/hemodynamics/{case}/hemodynamics.json"


def lesion_candidates(o: str, case: str) -> tuple[str, ...]:
    return (f"{o}/clinical/lesions/case_{case}_lesions.json",
            f"{o}/clinical/lesions/{case}_lesions.json")


def syn_lesion_candidates(o: str, case: str, sev) -> tuple[str, ...]:
    return (f"{o}/clinical/lesions/case_{case}_lesions_syn{sev}.json",
            f"{o}/clinical/lesions/{case}_lesions_syn{sev}.json")


def rxi_candidates(o: str, case: str) -> tuple[str, ...]:
    return (f"{o}/clinical/rxi/case_{case}_rxi.json",
            f"{o}/clinical/rxi/{case}_rxi.json")


def ladder_hemo_candidates(o: str, case: str, sev) -> tuple[str, ...]:
    return (f"{o}/clinical/ladder/{case}_syn{sev}/{case}/hemodynamics.json",
            f"{o}/clinical/ladder/{case}_syn{sev}/hemodynamics.json",
            f"{o}/clinical/ladder/case_{case}_syn{sev}/{case}/hemodynamics.json",
            f"{o}/clinical/ladder/case_{case}_syn{sev}/hemodynamics.json")


def payload_model_candidates(case: str) -> tuple[str, ...]:
    return (f"viewer/models/clinical/case_{case}_clinical.json",
            f"viewer/models/clinical/{case}_clinical.json")


def payload_public_candidates(case: str) -> tuple[str, ...]:
    return (f"viewer/public/clinical/case_{case}_clinical.json",
            f"viewer/public/clinical/{case}_clinical.json")


def resolve_artifact(candidates: tuple[str, ...] | list[str]) -> dict:
    """Load the first candidate that exists and parses.

    Returns {"obj", "path", "tried", "reason"}; never raises. `path` is the
    resolved repo-relative spelling (provenance for the reports).
    """
    tried = [rel_str(p) for p in candidates]
    reasons = []
    for cand in candidates:
        obj, reason = load_json(repo_path(cand))
        if obj is not None:
            return {"obj": obj, "path": rel_str(cand), "tried": tried, "reason": None}
        reasons.append(reason)
    return {"obj": None, "path": None, "tried": tried,
            "reason": join_reasons(*reasons) or "artifact unavailable"}


# --------------------------------------------------------------------------- #
# per-case pipeline
# --------------------------------------------------------------------------- #

@dataclass
class Stage:
    name: str
    argv: list[str]
    # each prereq is a group of candidate repo-relative paths; satisfied when
    # any one candidate exists (producers disagree on a "case_" name prefix)
    prereqs: list[tuple[str, ...]] = field(default_factory=list)
    produces: list[str] = field(default_factory=list)
    block_reason: str | None = None
    # placeholder token -> candidate paths; resolved to the file that actually
    # exists right before launch (producers disagree on a "case_" name prefix)
    resolve: dict[str, tuple[str, ...]] = field(default_factory=dict)


def longest_edge_placement(graph) -> tuple[str | None, float | None, str | None]:
    """(edge name, mid-length s_mm, reason) for the synthetic ladder lesion."""
    edges = graph.get("edges") if isinstance(graph, dict) else None
    if not isinstance(edges, list) or not edges:
        return None, None, "graph has no edges[] (cannot place the synthetic ladder lesion)"
    best = None
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        length = edge.get("length_mm")
        if not is_num(length):
            continue
        if best is None or length > best[0]:
            best = (float(length), edge)
    if best is None:
        return None, None, "no graph edge carries a numeric length_mm"
    length, edge = best
    name = edge.get("name") or edge.get("id")
    if not name:
        return None, None, "longest graph edge has no name/id"
    return str(name), length / 2.0, None


def build_stages(case: str, ladder: list[int], out_dir: str, python: str,
                 branch: str | None, s_mm: float | None,
                 placement_reason: str | None) -> list[Stage]:
    o = rel_str(out_dir)
    graph = (graph_rel(o, case),)
    lesions = lesion_candidates(o, case)
    hemo = (hemo_rel(o, case),)
    rxi = rxi_candidates(o, case)
    payload = payload_model_candidates(case) + payload_public_candidates(case)
    stages = [
        Stage(
            name="stenosis",
            argv=[python, "phase2/clinical/stenosis_analyzer.py",
                  "--cases", case,
                  "--graph-dir", f"{o}/rom/graphs",
                  "--out-dir", f"{o}/clinical/lesions"],
            prereqs=[graph],
            produces=list(lesions),
        ),
        Stage(
            name="hemodynamics",
            argv=[python, "phase2/rom/run_hemodynamics.py",
                  "--cases", case,
                  "--graph-dir", f"{o}/rom/graphs",
                  "--out-dir", f"{o}/rom/hemodynamics",
                  "--lesions", "@lesions@"],
            prereqs=[graph, lesions],
            produces=list(hemo),
            resolve={"@lesions@": lesions},
        ),
        Stage(
            name="stenosis_ladder",
            argv=[python, "phase2/clinical/stenosis_analyzer.py",
                  "--cases", case,
                  "--graph-dir", f"{o}/rom/graphs",
                  "--out-dir", f"{o}/clinical/lesions",
                  "--ladder", *[str(s) for s in ladder],
                  *(["--branch", branch, "--s", f"{s_mm:.3f}"]
                    if branch is not None and s_mm is not None else [])],
            prereqs=[graph],
            produces=[p for sev in ladder for p in syn_lesion_candidates(o, case, sev)],
            block_reason=placement_reason,
        ),
    ]
    for sev in ladder:
        syn_les = syn_lesion_candidates(o, case, sev)
        stages.append(Stage(
            name=f"hemodynamics_syn{sev}",
            argv=[python, "phase2/rom/run_hemodynamics.py",
                  "--cases", case,
                  "--graph-dir", f"{o}/rom/graphs",
                  "--lesions", "@lesions@",
                  "--out-dir", f"{o}/clinical/ladder/{case}_syn{sev}"],
            prereqs=[graph, syn_les],
            produces=list(ladder_hemo_candidates(o, case, sev)),
            resolve={"@lesions@": syn_les},
        ))
    stages.append(Stage(
        name="rxi_metrics",
        argv=[python, "phase2/clinical/generate_rxi_metrics.py",
              "--cases", case,
              "--graph-dir", f"{o}/rom/graphs",
              "--hemo-dir", f"{o}/rom/hemodynamics",
              "--lesions-dir", f"{o}/clinical/lesions",
              "--out-dir", f"{o}/clinical/rxi"],
        prereqs=[graph, hemo, lesions],
        produces=list(rxi),
    ))
    stages.append(Stage(
        name="export_payload",
        argv=[python, "tools/export_clinical_payload.py",
              "--cases", case,
              "--report-dir", o,
              "--graph-dir", f"{o}/rom/graphs",
              "--hemo-dir", f"{o}/rom/hemodynamics",
              "--rxi-dir", f"{o}/clinical/rxi",
              "--lesions-dir", f"{o}/clinical/lesions",
              "--glb-dir", "viewer/public/assets",
              "--models-out", "viewer/models/clinical",
              "--public-out", "viewer/public/clinical"],
        prereqs=[rxi, hemo, lesions],
        produces=list(payload),
    ))
    return stages


def run_case(case: str, ladder: list[int], out_dir: str, python: str,
             logs_dir: Path, error_log: ErrorLog) -> dict:
    o = rel_str(out_dir)
    graph_obj, graph_reason = load_json(repo_path(graph_rel(o, case)))
    branch, s_mm, placement_reason = longest_edge_placement(graph_obj)
    if placement_reason and graph_reason:
        placement_reason = f"{placement_reason} ({graph_reason})"
    stages = build_stages(case, ladder, out_dir, python, branch, s_mm,
                          placement_reason)

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
            "reason": stage.block_reason,
        }
        if stage.block_reason is None:
            missing = []
            for group in stage.prereqs:
                if not any(repo_path(a).is_file() for a in group):
                    src = None
                    src_state = None
                    for a in group:
                        if producer.get(a):
                            src = producer[a]
                            src_state = stage_status.get(src)
                            break
                    note = " or ".join(rel_str(a) for a in group)
                    if src and src_state not in (None, "ok"):
                        note += f" (producing stage {src}: {src_state})"
                    missing.append(note)
            if missing:
                row["reason"] = "missing intermediate artifact(s): " + ", ".join(missing)
            else:
                row["reason"] = None
                argv = list(stage.argv)
                for i, tok in enumerate(argv):
                    cands = stage.resolve.get(tok)
                    if cands:
                        hit = next((c for c in cands if repo_path(c).is_file()),
                                   cands[0])
                        argv[i] = rel_str(hit)
                row["command"] = shlex.join(argv)
                log_path = logs_dir / f"{case}_{stage.name}.log"
                try:
                    record = run_logged(
                        argv, log_path,
                        f"{TOOL_NAME} stage={stage.name} case={case}")
                except Exception:  # noqa: BLE001 — one stage must not kill the batch
                    tb = traceback.format_exc()
                    row["status"] = "failed"
                    row["reason"] = f"exception: {tb.strip().splitlines()[-1]}"
                    error_log.append({
                        "case": case,
                        "stage": stage.name,
                        "command": row["command"],
                        "exit_code": None,
                        "stderr_tail": "",
                        "exception": tb.strip().splitlines()[-1],
                        "traceback": tb,
                    })
                    record = None
                if record is not None:
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

    # An artifact is trusted only when its producing stage (and every upstream
    # producing stage it depends on) ran ok this run — a failed or skipped
    # stage must never contribute stale numbers to a clinical report.
    deps: dict[str, set[str]] = {}
    for stage in stages:
        d = set()
        for group in stage.prereqs:
            for a in group:
                src = producer.get(a)
                if src and src != stage.name:
                    d.add(src)
        deps[stage.name] = d
    stage_trust: dict[str, bool] = {}

    def trusted(name: str, visiting: set[str]) -> bool:
        if name in stage_trust:
            return stage_trust[name]
        if name in visiting or stage_status.get(name) != "ok":
            stage_trust[name] = False
            return False
        visiting.add(name)
        ok = all(trusted(dep, visiting) for dep in deps.get(name, ()))
        visiting.discard(name)
        stage_trust[name] = ok
        return ok

    for stage in stages:
        trusted(stage.name, set())

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
        "stage_trust": stage_trust,
        "runtime_s": round3(sum(r["runtime_s"] or 0.0 for r in rows)),
        "ladder_placement": {
            "branch": branch,
            "s_mm": round3(s_mm) if s_mm is not None else None,
            "unavailable_reason": placement_reason,
        },
        "stages": rows,
    }


# --------------------------------------------------------------------------- #
# report compiler (aggregates contract A-D artifacts — never recomputes physics)
# --------------------------------------------------------------------------- #

def contract_a(obj) -> tuple[dict[str, list[dict]], list[dict]]:
    """Tolerant contract A reader -> ({branch_id: [samples]}, [lesions])."""
    branches: dict[str, list[dict]] = {}
    lesions: list[dict] = []
    if not isinstance(obj, dict):
        return branches, lesions
    raw = obj.get("branches")
    if isinstance(raw, dict):
        for bid, val in raw.items():
            samples = val.get("samples") if isinstance(val, dict) else val
            branches[str(bid)] = [s for s in (samples or []) if isinstance(s, dict)]
    elif isinstance(raw, list):
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            bid = str(entry.get("branch_id", entry.get("id", entry.get("name", "?"))))
            if isinstance(entry.get("samples"), list):
                branches[bid] = [s for s in entry["samples"] if isinstance(s, dict)]
            elif any(k in entry for k in ("s_mm", "d_mm", "as_pct")):
                branches.setdefault(bid, []).append(entry)
    raw_les = obj.get("lesions")
    if isinstance(raw_les, list):
        lesions = [l for l in raw_les if isinstance(l, dict)]
    elif isinstance(raw_les, dict):
        for lid, val in raw_les.items():
            if isinstance(val, dict):
                rec = dict(val)
                rec.setdefault("lesion_id", lid)
                lesions.append(rec)
    return branches, lesions


def normalize_lesions(a_obj, clinical, payload) -> list[dict]:
    """One record per lesion_id, fields merged A (geometry) + B (pressure drops)
    + D (viewer payload fallback)."""
    _, a_lesions = contract_a(a_obj)
    cl_lesions = clinical.get("lesions") if isinstance(clinical, dict) else None
    p_lesions = payload.get("lesions") if isinstance(payload, dict) else None
    by_id: dict[str, dict] = {}

    def rec_for(lid) -> dict:
        return by_id.setdefault(str(lid), {"lesion_id": str(lid)})

    for l in a_lesions:
        rec = rec_for(l.get("lesion_id", l.get("id", len(by_id))))
        for k in ("source", "branch_id", "s_mm", "length_mm", "dia_reduction_pct",
                  "as_pct", "d_ref_mm", "d_min_mm", "A0_over_As"):
            if l.get(k) is not None:
                rec.setdefault(k, l.get(k))
    if isinstance(cl_lesions, dict):
        for lid, d in cl_lesions.items():
            if not isinstance(d, dict):
                continue
            rec = rec_for(lid)
            for k in ("dp_rest_mmHg", "dp_hyper_mmHg", "kv", "kt", "u_throat_m_s",
                      "vffr", "pd_location_mm"):
                if d.get(k) is not None:
                    rec.setdefault(k, d.get(k))
    if isinstance(p_lesions, list):
        for l in p_lesions:
            if not isinstance(l, dict):
                continue
            rec = rec_for(l.get("lesion_id", len(by_id)))
            for k in ("branch_id", "s_mm", "length_mm", "dia_reduction_pct",
                      "as_pct", "d_ref_mm", "d_min_mm", "dp_hyper_mmHg", "vffr"):
                if l.get(k) is not None:
                    rec.setdefault(k, l.get(k))
    return list(by_id.values())


def pullback_points(pdef: dict, payload_pullback: dict | None) -> list[dict]:
    """[{s_mm,d_mm,P_rest_mmHg,P_hyper_mmHg,vFFR}] from contract C (contract D
    parallel-array form accepted as fallback)."""
    points = []
    raw = pdef.get("pullback")
    if isinstance(raw, list):
        for pt in raw:
            if isinstance(pt, dict):
                points.append(pt)
        if points:
            return points
    src = payload_pullback if isinstance(payload_pullback, dict) else None
    if src is None:
        return points
    s_vals = src.get("s_mm")
    if isinstance(s_vals, list):
        series = {k: src.get(k) for k in ("s_mm", "d_mm", "P_rest_mmHg",
                                          "P_hyper_mmHg", "vFFR")}
        n = len(s_vals)
        for i in range(n):
            points.append({k: (v[i] if isinstance(v, list) and i < len(v) else None)
                           for k, v in series.items()})
    elif is_num(s_vals):
        points.append({k: src.get(k) for k in ("s_mm", "d_mm", "P_rest_mmHg",
                                               "P_hyper_mmHg", "vFFR")})
    return points


def path_defs(rxi, payload) -> tuple[list[dict], dict[str, dict], str | None]:
    """(path defs, payload pullback by path_id, reason when no defs)."""
    payload_by_id: dict[str, dict] = {}
    for pb in (payload.get("pullbacks") or []) if isinstance(payload, dict) else []:
        if isinstance(pb, dict):
            payload_by_id.setdefault(str(pb.get("path_id", "?")), pb)
    defs = [p for p in (rxi.get("paths") or []) if isinstance(p, dict)] \
        if isinstance(rxi, dict) else []
    if defs:
        return defs, payload_by_id, None
    defs = [{"path_id": pid, "label": pb.get("label") or pid,
             "branch_ids": pb.get("branch_ids")}
            for pid, pb in payload_by_id.items()]
    if defs:
        return defs, payload_by_id, None
    return [], payload_by_id, "no paths[] in the RXi artifact and no pullbacks[] in the viewer payload"


def build_path_row(pdef, *, clinical, rxi, a_branches, lesions,
                   payload_by_id) -> dict:
    pid = str(pdef.get("path_id", "?"))
    label = pdef.get("label") or pid
    branch_ids = [str(b) for b in (pdef.get("branch_ids") or [])]
    points = pullback_points(pdef, payload_by_id.get(pid))

    hyper_branches = dget(clinical, "hyper.branches") or {}
    rest_branches = dget(clinical, "rest.branches") or {}
    vc = clinical.get("velocity_calibration") if isinstance(clinical, dict) else None
    vc = vc if isinstance(vc, dict) else {}
    pa_mmHg = clinical.get("pa_mmHg") if isinstance(clinical, dict) else None
    ta = {}
    if isinstance(rxi, dict) and isinstance(rxi.get("transit_anomaly"), dict):
        entry = rxi["transit_anomaly"].get(pid)
        if isinstance(entry, dict):
            ta = entry

    fields: dict[str, dict] = {}
    derive: dict[str, dict] = {}

    # --- Hyperemic vFFR: min over clinical.hyper.branches on the path --------
    v_per_edge = per_edge_values(hyper_branches, branch_ids, "vffr")
    pb_vffr = [pt.get("vFFR") for pt in points if is_num(pt.get("vFFR"))]
    if v_per_edge:
        fields["vffr_hyper"] = {"value": min(v_per_edge.values()),
                                "source": "min clinical.hyper.branches[*].vffr over path branch_ids",
                                "reason": None}
    elif pb_vffr:
        fields["vffr_hyper"] = {"value": min(pb_vffr),
                                "source": "min pullback vFFR along the path",
                                "reason": None}
    else:
        fields["vffr_hyper"] = {"value": None, "source": None,
                                "reason": join_reasons(
                                    "no clinical.hyper.branches vffr for the path edges",
                                    "no pullback vFFR samples") or "no vFFR source"}
    derive["vffr_hyper"] = {
        "per_edge": v_per_edge,
        "cross_check": {
            "paths.min_vffr": pdef.get("min_vffr"),
            "pullback_min_vFFR": min(pb_vffr) if pb_vffr else None,
        },
    }

    # --- Resting Pd/Pa: min over clinical.rest.branches on the path ----------
    pd_per_edge = per_edge_values(rest_branches, branch_ids, "pd_pa")
    pb_pd = [(pt.get("P_rest_mmHg") / pa_mmHg) for pt in points
             if is_num(pt.get("P_rest_mmHg")) and is_num(pa_mmHg) and pa_mmHg]
    if pd_per_edge:
        fields["pd_pa_rest"] = {"value": min(pd_per_edge.values()),
                                "source": "min clinical.rest.branches[*].pd_pa over path branch_ids",
                                "reason": None}
    elif pb_pd:
        fields["pd_pa_rest"] = {"value": min(pb_pd),
                                "source": "min pullback P_rest_mmHg / clinical.pa_mmHg",
                                "reason": None}
    else:
        fields["pd_pa_rest"] = {"value": None, "source": None,
                                "reason": "no clinical.rest.branches pd_pa and no pullback/pa_mmHg pair"}
    derive["pd_pa_rest"] = {
        "per_edge": pd_per_edge,
        "cross_check": {"pullback_min_P_rest_over_pa": min(pb_pd) if pb_pd else None},
    }

    # --- D_min along the path -----------------------------------------------
    pb_d = [pt.get("d_mm") for pt in points if is_num(pt.get("d_mm"))]
    sample_d = [s.get("d_mm") for e in branch_ids for s in a_branches.get(e, [])
                if is_num(s.get("d_mm"))]
    lesion_d = [l.get("d_min_mm") for l in lesions
                if str(l.get("branch_id")) in branch_ids and is_num(l.get("d_min_mm"))]
    d_pool = pb_d or sample_d or lesion_d
    fields["d_min_mm"] = {
        "value": min(d_pool) if d_pool else None,
        "source": ("min pullback d_mm" if pb_d else
                   "min contract-A branch sample d_mm" if sample_d else
                   "min lesion d_min_mm" if lesion_d else None),
        "reason": None if d_pool else "no pullback d_mm / branch samples / lesion d_min_mm for the path",
    }

    # --- max %AS along the path ---------------------------------------------
    sample_as = [s.get("as_pct") for e in branch_ids for s in a_branches.get(e, [])
                 if is_num(s.get("as_pct"))]
    lesion_as = [l.get("as_pct") for l in lesions
                 if str(l.get("branch_id")) in branch_ids and is_num(l.get("as_pct"))]
    as_pool = [v for v in sample_as + lesion_as if is_num(v)]
    fields["max_as_pct"] = {
        "value": max(as_pool) if as_pool else None,
        "source": ("max as_pct over contract-A branch samples + lesions on the path"
                   if as_pool else None),
        "reason": None if as_pool else "no contract-A as_pct samples/lesions on the path",
    }

    # --- trans-lesion dP at hyperemia: lesion with max dP --------------------
    on_path = [l for l in lesions
               if str(l.get("branch_id")) in branch_ids and is_num(l.get("dp_hyper_mmHg"))]
    if on_path:
        worst = max(on_path, key=lambda l: l["dp_hyper_mmHg"])
        fields["dp_lesion_hyper_mmHg"] = {
            "value": worst["dp_hyper_mmHg"],
            "source": f"clinical.lesions[{worst['lesion_id']}].dp_hyper_mmHg (max over path lesions)",
            "reason": None,
        }
        derive["dp_lesion_hyper_mmHg"] = {
            "lesion_id": worst.get("lesion_id"), "branch_id": worst.get("branch_id"),
            "as_pct": worst.get("as_pct"),
        }
    else:
        fields["dp_lesion_hyper_mmHg"] = {
            "value": None, "source": None,
            "reason": "no lesion with dp_hyper_mmHg mapped to the path's branches",
        }

    # --- TFC + transit (contract C transit_anomaly) --------------------------
    def ta_num(key):
        return ta.get(key) if is_num(ta.get(key)) else None

    fields["tfc_frames"] = {"value": ta_num("tfc_frames"),
                            "source": f"transit_anomaly[{pid}].tfc_frames",
                            "reason": None if ta_num("tfc_frames") is not None
                            else f"no transit_anomaly[{pid}].tfc_frames"}
    fields["tfc_calibrated_frames"] = {"value": ta_num("tfc_calibrated_frames"),
                                       "source": f"transit_anomaly[{pid}].tfc_calibrated_frames",
                                       "reason": None if ta_num("tfc_calibrated_frames") is not None
                                       else f"no transit_anomaly[{pid}].tfc_calibrated_frames"}
    fields["transit_raw_s"] = {"value": ta_num("distal_raw_s"),
                               "source": f"transit_anomaly[{pid}].distal_raw_s",
                               "reason": None if ta_num("distal_raw_s") is not None
                               else f"no transit_anomaly[{pid}].distal_raw_s"}
    t_cal = ta_num("distal_calibrated_s")
    if t_cal is None:
        tcs = vc.get("transit_calibrated_s")
        if isinstance(tcs, dict):
            vals = [tcs[e] for e in branch_ids if is_num(tcs.get(e))]
            if vals:
                t_cal = sum(vals)
                fields["transit_calibrated_s"] = {
                    "value": t_cal,
                    "source": "sum clinical.velocity_calibration.transit_calibrated_s over path branch_ids",
                    "reason": None,
                }
        if t_cal is None:
            fields["transit_calibrated_s"] = {
                "value": None, "source": None,
                "reason": f"no transit_anomaly[{pid}].distal_calibrated_s and "
                          "no per-edge velocity_calibration.transit_calibrated_s",
            }
    else:
        fields["transit_calibrated_s"] = {"value": t_cal,
                                          "source": f"transit_anomaly[{pid}].distal_calibrated_s",
                                          "reason": None}

    # --- transit flag rendering is driven by transit_anomaly[pid].flag only;
    # the > 5.0 s exceeds fact is recorded but never synthesized into a flag --
    transit_vals = [v for v in (fields["transit_raw_s"]["value"],
                                fields["transit_calibrated_s"]["value"]) if is_num(v)]
    exceeds = any(v > TIMI_TRANSIT_S for v in transit_vals)
    artifact_flag = ta.get("flag")
    flag_text = str(artifact_flag) if artifact_flag else None

    decision = vffr_decision(fields["vffr_hyper"]["value"])
    reasons = [f"{k}: {f['reason']}" for k, f in fields.items()
               if f["value"] is None and f.get("reason")]
    row = {
        "path_id": pid,
        "label": label,
        "branch_ids": branch_ids,
        "branch_count": len(branch_ids),
        "length_mm": pdef.get("length_mm"),
        "fields": fields,
        "decision": decision,
        "vffr_band": vffr_band(fields["vffr_hyper"]["value"]),
        "transit_flag": flag_text,
        "transit_flag_rule_exceeds_5s": exceeds,
        "transit_artifact_flag": artifact_flag,
        "transit_reason": ta.get("reason"),
        "dp_ds": (rxi.get("dp_ds") or {}).get(pid) if isinstance(rxi, dict) else None,
        "derive": derive,
        "unavailable_reason": join_reasons(*reasons),
    }
    return row


def pick_syn_lesion(lesions: list[dict], sev) -> dict | None:
    exact = [l for l in lesions if l.get("dia_reduction_pct") == sev
             or (is_num(l.get("dia_reduction_pct")) and float(l["dia_reduction_pct"]) == float(sev))]
    if exact:
        return exact[0]
    if len(lesions) == 1:
        return lesions[0]
    scored = [l for l in lesions if is_num(l.get("as_pct"))]
    if scored:
        return max(scored, key=lambda l: l["as_pct"])
    return lesions[0] if lesions else None


def build_ladder_row(sev, syn_res, ladd_hemo_res) -> dict:
    clinical = dget(ladd_hemo_res["obj"], "clinical") if ladd_hemo_res["obj"] else None
    a_branches, a_lesions = contract_a(syn_res["obj"]) if syn_res["obj"] else ({}, [])
    pick = pick_syn_lesion(a_lesions, sev)
    fields: dict[str, dict] = {}

    def missing(res, what):
        return res["reason"] or f"no {what}"

    fields["as_pct"] = {
        "value": pick.get("as_pct") if pick and is_num(pick.get("as_pct")) else None,
        "source": f"synthetic lesion {pick.get('lesion_id') if pick else ''} as_pct"
                  if pick else None,
        "reason": None if pick and is_num(pick.get("as_pct"))
        else (missing(syn_res, f"synthetic lesion at dia_reduction {sev}%")
              if syn_res["obj"] is None or not a_lesions
              else f"synthetic lesion at dia_reduction {sev}% has no numeric as_pct"),
    }

    cl_lesions = clinical.get("lesions") if isinstance(clinical, dict) else None
    entry = None
    if isinstance(cl_lesions, dict) and pick is not None:
        entry = cl_lesions.get(str(pick.get("lesion_id")))
        if not isinstance(entry, dict) and len(cl_lesions) == 1:
            entry = next(iter(cl_lesions.values()))
    dp = entry.get("dp_hyper_mmHg") if isinstance(entry, dict) else None
    fields["dp_hyper_mmHg"] = {
        "value": dp if is_num(dp) else None,
        "source": "clinical.lesions[synthetic].dp_hyper_mmHg" if is_num(dp) else None,
        "reason": None if is_num(dp) else join_reasons(
            missing(ladd_hemo_res, "ladder hemodynamics clinical block")
            if clinical is None else None,
            "no clinical.lesions[..].dp_hyper_mmHg for the synthetic lesion"),
    }
    vffr = entry.get("vffr") if isinstance(entry, dict) else None
    vffr_source = "clinical.lesions[synthetic].vffr"
    if not is_num(vffr):
        hyper_branches = dget(clinical, "hyper.branches") or {}
        vals = [e.get("vffr") for e in hyper_branches.values()
                if isinstance(e, dict) and is_num(e.get("vffr"))]
        if vals:
            vffr = min(vals)
            vffr_source = "min clinical.hyper.branches[*].vffr (ladder run)"
    fields["vffr"] = {
        "value": vffr if is_num(vffr) else None,
        "source": vffr_source if is_num(vffr) else None,
        "reason": None if is_num(vffr) else join_reasons(
            missing(ladd_hemo_res, "ladder hemodynamics clinical block")
            if clinical is None else None,
            "no synthetic-lesion vffr in clinical.lesions and no clinical.hyper.branches vffr"),
    }
    decision = vffr_decision(fields["vffr"]["value"])
    reasons = [f"{k}: {f['reason']}" for k, f in fields.items()
               if f["value"] is None and f.get("reason")]
    return {
        "severity_dia_reduction_pct": sev,
        "lesion_id": pick.get("lesion_id") if pick else None,
        "dia_reduction_pct": pick.get("dia_reduction_pct") if pick else None,
        "fields": fields,
        "decision": decision,
        "vffr_band": vffr_band(fields["vffr"]["value"]),
        "unavailable_reason": join_reasons(*reasons),
    }


def build_transit_rows(path_rows, rxi) -> list[dict]:
    rows = []
    label_of = {r["path_id"]: r["label"] for r in path_rows}
    anomaly = rxi.get("transit_anomaly") if isinstance(rxi, dict) else None
    anomaly = anomaly if isinstance(anomaly, dict) else {}
    for pid in list(label_of) + [p for p in anomaly if p not in label_of]:
        ta = anomaly.get(pid) if isinstance(anomaly.get(pid), dict) else {}
        raw = ta.get("distal_raw_s") if is_num(ta.get("distal_raw_s")) else None
        cal = ta.get("distal_calibrated_s") if is_num(ta.get("distal_calibrated_s")) else None
        exceeds = any(v > TIMI_TRANSIT_S for v in (raw, cal) if is_num(v))
        flag = ta.get("flag") or None
        rows.append({
            "path_id": pid,
            "label": label_of.get(pid, pid),
            "distal_raw_s": raw,
            "distal_calibrated_s": cal,
            "tfc_frames": ta.get("tfc_frames") if is_num(ta.get("tfc_frames")) else None,
            "tfc_calibrated_frames": ta.get("tfc_calibrated_frames")
            if is_num(ta.get("tfc_calibrated_frames")) else None,
            "flag": flag,
            "flag_rule_exceeds_5s": exceeds,
            "reason": ta.get("reason"),
            "unavailable_reason": None if (raw is not None or cal is not None)
            else (ta.get("reason") or f"no transit_anomaly[{pid}] in the RXi artifact"),
        })
    return rows


def empty_clinical(reason: str) -> dict:
    """Full-shape placeholder used when compilation itself fails."""
    return {
        "case": None,
        "artifacts": {},
        "paths": [],
        "case_summary": {"vffr_min": None, "vffr_min_path_id": None,
                         "decision": None, "vffr_band": None,
                         "unavailable_reason": reason},
        "ladder": [],
        "transit_calibration": [],
        "landmarks": [],
        "tfc": None,
        "payload_files": [],
        "hyper_flow_increase_x": None,
        "hyper_q_total_ml_min": None,
        "hyper_rd_scale": None,
        "pa_mmHg": None,
        "q_rest_ml_min": None,
        "velocity_calibration": {},
        "solver": {},
        "unavailable_reason": reason,
    }


def compile_case(case: str, ladder: list[int], out_dir: str,
                 stage_trust: dict[str, bool], stage_rows: list[dict]) -> dict:
    o = rel_str(out_dir)
    art: dict[str, dict] = {}

    def grab(name, candidates, produced_by=None):
        if produced_by is not None and not stage_trust.get(produced_by, False):
            row = next((r for r in stage_rows if r.get("stage") == produced_by), {})
            status = row.get("status") or "absent"
            detail = row.get("reason")
            if status == "ok":
                detail = "output untrusted: an upstream stage failed or was skipped"
            reason = f"producing stage {produced_by} {status} this run" + \
                (f" ({detail})" if detail else "")
            tried = [rel_str(p) for p in candidates]
            art[name] = {"path": None, "tried": tried, "reason": reason}
            return {"obj": None, "path": None, "tried": tried, "reason": reason}
        res = resolve_artifact(candidates)
        art[name] = {"path": res["path"], "tried": res["tried"], "reason": res["reason"]}
        return res

    graph_res = grab("graph", [graph_rel(o, case)])
    lesions_res = grab("lesions", lesion_candidates(o, case), "stenosis")
    hemo_res = grab("hemodynamics", [hemo_rel(o, case)], "hemodynamics")
    rxi_res = grab("rxi", rxi_candidates(o, case), "rxi_metrics")
    payload_res = grab("payload_models", payload_model_candidates(case), "export_payload")
    payload_pub_res = grab("payload_public", payload_public_candidates(case), "export_payload")
    syn_res = {sev: grab(f"lesions_syn{sev}", syn_lesion_candidates(o, case, sev),
                         "stenosis_ladder")
               for sev in ladder}
    ladd_res = {sev: grab(f"hemodynamics_syn{sev}", ladder_hemo_candidates(o, case, sev),
                          f"hemodynamics_syn{sev}")
                for sev in ladder}

    hemo = hemo_res["obj"]
    clinical = dget(hemo, "clinical") if isinstance(hemo, dict) else None
    clinical_reason = (hemo_res["reason"] if hemo is None else
                       None if isinstance(clinical, dict) else
                       f"no clinical block in {art['hemodynamics']['path']}")
    rxi = rxi_res["obj"]
    payload = payload_res["obj"] if payload_res["obj"] is not None else payload_pub_res["obj"]

    a_branches, _ = contract_a(lesions_res["obj"])
    lesions = normalize_lesions(lesions_res["obj"], clinical, payload)
    defs, payload_by_id, defs_reason = path_defs(rxi, payload)

    paths = [build_path_row(p, clinical=clinical, rxi=rxi, a_branches=a_branches,
                            lesions=lesions, payload_by_id=payload_by_id)
             for p in defs]

    # case summary: worst (min) vFFR across the evaluated target vessels
    scored = [r for r in paths if is_num(r["fields"]["vffr_hyper"]["value"])]
    if scored:
        worst = min(scored, key=lambda r: r["fields"]["vffr_hyper"]["value"])
        v_min = worst["fields"]["vffr_hyper"]["value"]
        case_summary = {
            "vffr_min": v_min,
            "vffr_min_path_id": worst["path_id"],
            "decision": vffr_decision(v_min),
            "vffr_band": vffr_band(v_min),
            "unavailable_reason": None,
        }
    else:
        case_summary = {
            "vffr_min": None, "vffr_min_path_id": None, "decision": None,
            "vffr_band": None,
            "unavailable_reason": defs_reason or "no path carries a vFFR value",
        }

    ladder_rows = [build_ladder_row(sev, syn_res[sev], ladd_res[sev]) for sev in ladder]
    transit_rows = build_transit_rows(paths, rxi)
    landmarks = [l for l in (rxi.get("landmarks") or []) if isinstance(l, dict)] \
        if isinstance(rxi, dict) else []

    payload_files = []
    for slot, res in (("viewer/models/clinical", payload_res),
                      ("viewer/public/clinical", payload_pub_res)):
        entry = {"slot": slot, "path": res["path"], "bytes": None, "mb": None,
                 "unavailable_reason": res["reason"]}
        if res["path"] is not None:
            try:
                size = repo_path(res["path"]).stat().st_size
                entry["bytes"] = size
                entry["mb"] = round3(size / (1024.0 * 1024.0))
            except OSError as exc:
                entry["unavailable_reason"] = f"cannot stat {res['path']}: {exc}"
        payload_files.append(entry)

    overall = join_reasons(
        f"contract A lesions unavailable: {lesions_res['reason']}" if lesions_res["reason"] else None,
        f"contract B hemodynamics unavailable: {clinical_reason}" if clinical_reason else None,
        f"contract C RXi unavailable: {rxi_res['reason']}" if rxi_res["reason"] else None,
        f"contract D payload unavailable: {join_reasons(payload_res['reason'], payload_pub_res['reason'])}"
        if payload_res["reason"] and payload_pub_res["reason"] else None,
        defs_reason,
    )

    return {
        "case": case,
        "artifacts": art,
        "pa_mmHg": clinical.get("pa_mmHg") if isinstance(clinical, dict) else None,
        "q_rest_ml_min": clinical.get("q_rest_ml_min") if isinstance(clinical, dict) else None,
        "hyper_flow_increase_x": dget(clinical, "hyper.flow_increase_x"),
        "hyper_q_total_ml_min": dget(clinical, "hyper.q_total_ml_min"),
        "hyper_rd_scale": dget(clinical, "hyper.rd_scale"),
        "velocity_calibration": (clinical.get("velocity_calibration")
                                 if isinstance(clinical, dict) and
                                 isinstance(clinical.get("velocity_calibration"), dict)
                                 else {}),
        "solver": (clinical.get("solver") if isinstance(clinical, dict) and
                   isinstance(clinical.get("solver"), dict) else {}),
        "graph_stats": (graph_res["obj"].get("stats")
                        if isinstance(graph_res["obj"], dict) and
                        isinstance(graph_res["obj"].get("stats"), dict) else {}),
        "lesions": lesions,
        "paths": paths,
        "case_summary": case_summary,
        "ladder": ladder_rows,
        "transit_calibration": transit_rows,
        "landmarks": landmarks,
        "tfc": (rxi.get("tfc") if isinstance(rxi, dict) and
                isinstance(rxi.get("tfc"), dict) else None),
        "payload_files": payload_files,
        "unavailable_reason": overall,
    }


# --------------------------------------------------------------------------- #
# verification checks (PASS/FAIL recomputed from the compiled artifact values)
# --------------------------------------------------------------------------- #

def _check(check_id, case, description, ok, value, expected, detail) -> dict:
    return {"id": check_id, "case": case, "description": description,
            "status": "PASS" if ok else "FAIL", "value": value,
            "expected": expected, "detail": detail}


def build_verification(cl: dict) -> list[dict]:
    case = cl.get("case")
    items = []

    # 1 — hyperemic flow increase in [2.5, 4.0]x (clinical.hyper.flow_increase_x)
    fi = cl.get("hyper_flow_increase_x")
    lo, hi = FLOW_INCREASE_BAND
    if is_num(fi):
        items.append(_check(
            "flow_increase", case, f"hyperemic flow increase in [{lo}, {hi}]x",
            lo <= fi <= hi, fi, f"[{lo}, {hi}]x",
            f"clinical.hyper.flow_increase_x = {fnum(fi)}x"))
    else:
        items.append(_check(
            "flow_increase", case, f"hyperemic flow increase in [{lo}, {hi}]x",
            False, None, f"[{lo}, {hi}]x",
            f"unavailable: {cl['unavailable_reason'] or 'no clinical.hyper.flow_increase_x'}"))

    # 2 — primary-trunk calibrated transit within [1.2, 3.5] s per case.
    # Checked quantity: each primary root->tip trunk's calibrated distal
    # transit (contract C transit_anomaly[pid].distal_calibrated_s — the same
    # numbers as the "calibrated transit (s)" summary column; out-of-band parts
    # carry the flag-driven transit annotation). The pure convective
    # L/(kappa*u) sums (velocity_calibration.primary_paths / per-edge
    # transit_calibrated_s; never sum trunk_edges — it mixes both root trees)
    # are printed as a labeled cross-reference only: real convective sums do
    # not span this band.
    lo, hi = TRANSIT_BAND_S
    vc = cl.get("velocity_calibration") or {}
    primary_paths = (vc.get("primary_paths")
                     if isinstance(vc.get("primary_paths"), dict) else {})
    per_edge_t = (vc.get("transit_calibrated_s")
                  if isinstance(vc.get("transit_calibrated_s"), dict) else {})
    bids_of = {r["path_id"]: r.get("branch_ids") or []
               for r in cl.get("paths") or []}
    transits = {r["path_id"]: r for r in cl.get("transit_calibration") or []}
    targets = list(bids_of) + [pid for pid in transits if pid not in bids_of]
    vals = []
    detail_parts = []
    for pid in targets:
        row = transits.get(pid)
        t = row.get("distal_calibrated_s") if isinstance(row, dict) else None
        if is_num(t):
            vals.append(t)
            if lo <= t <= hi:
                detail_parts.append(
                    f"{pid}: {fnum(t)} s (transit_anomaly[{pid}].distal_calibrated_s)")
            else:
                part = f"{pid} distal_calibrated_s {fnum(t)} s (band {lo}-{hi})"
                suffix = transit_annotation(row.get("flag"),
                                            row.get("distal_raw_s"), t)
                if suffix:
                    part += f" {suffix}"
                detail_parts.append(part)
        else:
            why = (row.get("unavailable_reason") if isinstance(row, dict) else None) \
                or f"no transit_anomaly[{pid}].distal_calibrated_s"
            detail_parts.append(f"{pid}: unavailable: {why}")
        pp = primary_paths.get(pid) if isinstance(primary_paths.get(pid), dict) else {}
        conv = pp.get("transit_calibrated_s") \
            if is_num(pp.get("transit_calibrated_s")) else None
        csrc = f"velocity_calibration.primary_paths[{pid}]"
        if conv is None and bids_of.get(pid):
            ev = [per_edge_t.get(e) for e in bids_of[pid]]
            if ev and all(is_num(v) for v in ev):
                conv = sum(ev)
                csrc = f"per-edge sum over {len(ev)} path branch_ids"
        if is_num(conv):
            detail_parts.append(
                f"{pid} convective L/(kappa*u) cross-reference: {fnum(conv)} s ({csrc})")
    n_targets = len(targets)
    ok = n_targets > 0 and len(vals) == n_targets and all(lo <= v <= hi for v in vals)
    items.append(_check(
        "trunk_transit", case,
        f"primary-trunk calibrated transit within [{lo}, {hi}] s",
        ok, vals or None, f"[{lo}, {hi}] s", "; ".join(detail_parts)
        or f"unavailable: {cl['unavailable_reason'] or 'no primary trunks to measure'}"))

    # 3 — ladder vFFR monotonically non-increasing with severity (50 -> 70 -> 90)
    series = []
    missing = []
    for row in cl.get("ladder") or []:
        v = row["fields"]["vffr"]["value"]
        if is_num(v):
            series.append((row["severity_dia_reduction_pct"], v))
        else:
            missing.append(f"dia_reduction {row['severity_dia_reduction_pct']}%: "
                           f"unavailable: {row['fields']['vffr']['reason']}")
    chain = " -> ".join(f"{sev}%: {fnum(v)}" for sev, v in series)
    mono = (len(series) >= 2 and not missing and
            all(series[i][1] >= series[i + 1][1] - 1e-12 for i in range(len(series) - 1)))
    items.append(_check(
        "ladder_monotonic", case,
        "ladder vFFR monotonically non-increasing with severity (50 -> 70 -> 90)",
        mono, [v for _, v in series] or None, "non-increasing with dia_reduction",
        chain or f"unavailable: {cl['unavailable_reason'] or 'no ladder vFFR values'}"
        + (("; " + "; ".join(missing)) if missing else "")))

    # 4 — payload < 2 MB per case (contract D: viewer payload files)
    sizes = [(f["path"], f["bytes"]) for f in cl.get("payload_files") or []
             if is_num(f.get("bytes"))]
    max_bytes = max((b for _, b in sizes), default=None)
    detail_parts = []
    for f in cl.get("payload_files") or []:
        if is_num(f.get("bytes")):
            detail_parts.append(f"{f['path']}: {f['bytes']} bytes ({fnum(f['mb'])} MB)")
        else:
            detail_parts.append(f"{f['slot']}: unavailable: {f['unavailable_reason']}")
    ok = bool(sizes) and all(b < PAYLOAD_MAX_BYTES for _, b in sizes)
    items.append(_check(
        "payload_size", case,
        "viewer payload < 2 MB per file", ok,
        {"max_bytes": max_bytes,
         "max_mb": round3(max_bytes / (1024.0 * 1024.0)) if max_bytes is not None else None},
        f"< {PAYLOAD_MAX_BYTES} bytes (2 MB)", "; ".join(detail_parts)))

    # 5 — vFFR decision rule applied consistently (and row vFFR provenance
    #     holds: the table value equals min over clinical.hyper.branches).
    instances = []
    for row in cl.get("paths") or []:
        instances.append((f"path {row['path_id']}",
                          row["fields"]["vffr_hyper"]["value"], row["decision"]))
    cs = cl.get("case_summary") or {}
    instances.append(("case", cs.get("vffr_min"), cs.get("decision")))
    for row in cl.get("ladder") or []:
        instances.append((f"ladder {row['severity_dia_reduction_pct']}%",
                          row["fields"]["vffr"]["value"], row["decision"]))
    bad = []
    for name, v, dec in instances:
        want = vffr_decision(v)
        if want is None:
            if dec is not None:
                bad.append(f"{name}: decision {dec!r} rendered without a vFFR value")
        elif dec != want:
            bad.append(f"{name}: {dec!r} != rule result {want!r} for vFFR {fnum(v)}")
    provenance_bad = []
    for row in cl.get("paths") or []:
        per_edge = (row.get("derive") or {}).get("vffr_hyper", {}).get("per_edge") or {}
        v = row["fields"]["vffr_hyper"]["value"]
        if per_edge and (not is_num(v) or abs(min(per_edge.values()) - v) > 1e-12):
            provenance_bad.append(
                f"path {row['path_id']}: table vFFR {fnum(v)} != "
                f"min clinical.hyper.branches {fnum(min(per_edge.values()))}")
    decided = [i for i in instances if i[2] is not None]
    cross_checked = sum(
        1 for r in cl.get("paths") or []
        if ((r.get("derive") or {}).get("vffr_hyper") or {}).get("per_edge"))
    detail = (f"{len(decided)}/{len(instances)} decision instances match "
              f"vFFR <= {VFFR_THRESHOLD} rule; row vFFR = min "
              f"clinical.hyper.branches[*].vffr over path branch_ids "
              f"({cross_checked} rows cross-checked)")
    if bad:
        detail += "; MISMATCH: " + "; ".join(bad)
    if provenance_bad:
        detail += "; PROVENANCE MISMATCH: " + "; ".join(provenance_bad)
    items.append(_check(
        "decision_rule", case,
        "vFFR decision rule applied consistently", bool(decided) and not bad and not provenance_bad,
        [i[2] for i in instances],
        f"<= {VFFR_THRESHOLD} -> {DECISION_INDICATED} else {DECISION_NORMAL}",
        detail))
    return items


# --------------------------------------------------------------------------- #
# report writers
# --------------------------------------------------------------------------- #

def render_case_markdown(case_rec: dict) -> str:
    cl = case_rec["clinical"]
    lines: list[str] = []
    failing = case_rec.get("failing_stage")
    head = f"## Case {case_rec['case']} — {case_rec['status']}"
    if failing:
        head += f" (failing stage: {failing})"
    lines.append(head)
    lines.append("")
    placement = case_rec.get("ladder_placement") or {}
    if placement.get("branch") is not None:
        lines.append(f"- synthetic ladder lesion placement: edge `{placement['branch']}` "
                     f"at s = {fnum(placement['s_mm'])} mm (longest edge, mid-length)")
    elif placement.get("unavailable_reason"):
        lines.append(f"- synthetic ladder lesion placement: unavailable: "
                     f"{placement['unavailable_reason']}")
    lines.append("")
    lines.append("### Pipeline stages")
    lines.append("")
    lines.append("| stage | status | exit code | runtime (s) | log | reason |")
    lines.append("|---|---|---|---|---|---|")
    for row in case_rec["stages"]:
        lines.append(
            f"| {row['stage']} | {row['status']} | {fnum(row['exit_code'])} | "
            f"{fnum(row['runtime_s'])} | {row['log'] or '—'} | {row['reason'] or '—'} |")
    lines.append("")

    # --- mandated per-case summary table ------------------------------------
    lines.append(f"### {SECTION_SUMMARY}")
    lines.append("")
    lines.append("| Target vessels evaluated | D_min (mm) | max %AS | Resting Pd/Pa "
                 "| Hyperemic vFFR | Trans-lesion dP (mmHg, hyperemia) | TFC (frames) "
                 "| calibrated transit (s) | Clinical decision |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    if cl["paths"]:
        for row in cl["paths"]:
            f = row["fields"]
            tfc_bits = []
            if f["tfc_calibrated_frames"]["value"] is not None:
                tfc_bits.append(fnum(f["tfc_calibrated_frames"]["value"]))
            if f["tfc_frames"]["value"] is not None:
                tfc_bits.append(f"raw {fnum(f['tfc_frames']['value'])}")
            tfc_cell = " / ".join(tfc_bits) if tfc_bits else \
                f"unavailable: {f['tfc_calibrated_frames']['reason']}"
            dp_rec = f["dp_lesion_hyper_mmHg"]
            dp_cell = (fnum(dp_rec["value"]) if dp_rec["value"] is not None
                       else f"unavailable: {dp_rec['reason']}")
            decision = row["decision"] or f"unavailable: {f['vffr_hyper']['reason']}"
            transit_cell = cell_value(f["transit_calibrated_s"])
            suffix = transit_annotation(row.get("transit_artifact_flag"),
                                        f["transit_raw_s"]["value"],
                                        f["transit_calibrated_s"]["value"])
            if suffix:
                transit_cell += f" {suffix}"
            lines.append(
                f"| {row['label']} ({row['branch_count']} branches) "
                f"| {cell_value(f['d_min_mm'])} | {cell_value(f['max_as_pct'])} "
                f"| {cell_value(f['pd_pa_rest'])} | {cell_value(f['vffr_hyper'])} "
                f"| {dp_cell} | {tfc_cell} | {transit_cell} "
                f"| {decision} |")
    else:
        lines.append("| " + " | ".join(
            [f"unavailable: {cl['unavailable_reason'] or 'no target vessel paths'}"]
            + ["—"] * 7 + [f"unavailable: {cl['case_summary']['unavailable_reason']}"]) + " |")
    lines.append("")
    cs = cl["case_summary"]
    if cs["decision"] is not None:
        lines.append(f"- case Hyperemic vFFR (min over target vessels): "
                     f"{fnum(cs['vffr_min'])} ({cs['vffr_min_path_id']}) — "
                     f"case Clinical decision: {cs['decision']}")
    else:
        lines.append(f"- case Hyperemic vFFR: unavailable: {cs['unavailable_reason']}")
    lines.append(f"- vFFR provenance: row values are min(clinical.hyper.branches[e].vffr) "
                 f"over each path's branch_ids (per-edge values and cross-checks against "
                 f"paths[].min_vffr / pullback vFFR are in the JSON twin); Resting Pd/Pa = "
                 f"min(clinical.rest.branches[e].pd_pa); D_min / TFC / calibrated transit "
                 f"come from the ACIST RXi pullback and transit_anomaly; max %AS and "
                 f"trans-lesion dP from contract A lesions joined with clinical.lesions.")
    lines.append("- vFFR context bands (not decision criteria): > 0.80 normal, "
                 "0.75–0.80 borderline, < 0.75 severe.")
    if cl["unavailable_reason"]:
        lines.append(f"- unavailable: {cl['unavailable_reason']}")
    lines.append("")
    return "\n".join(lines)


def render_ladder_markdown(case_rec: dict) -> list[str]:
    cl = case_rec["clinical"]
    lines = [f"### Case {case_rec['case']}", ""]
    lines.append("| Synthetic dia_reduction (%) | as_pct | dP_hyper (mmHg) | vFFR "
                 "| Clinical decision |")
    lines.append("|---|---|---|---|---|")
    for row in cl["ladder"]:
        f = row["fields"]
        decision = row["decision"] or f"unavailable: {f['vffr']['reason']}"
        lines.append(f"| {row['severity_dia_reduction_pct']} | {cell_value(f['as_pct'])} "
                     f"| {cell_value(f['dp_hyper_mmHg'])} | {cell_value(f['vffr'])} "
                     f"| {decision} |")
    if not cl["ladder"]:
        lines.append("| — | — | — | — | — |")
    lines.append("")
    for row in cl["ladder"]:
        if row["unavailable_reason"]:
            lines.append(f"- dia_reduction {row['severity_dia_reduction_pct']}%: "
                         f"unavailable: {row['unavailable_reason']}")
    placement = case_rec.get("ladder_placement") or {}
    if placement.get("branch") is not None:
        lines.append(f"- placement: synthetic lesion on edge `{placement['branch']}` "
                     f"at s = {fnum(placement['s_mm'])} mm (longest edge, mid-length); "
                     f"severity ladder proves monotonic response 50 -> 70 -> 90 "
                     f"(monotonicity check under {SECTION_VERIFICATION}).")
    lines.append("")
    return lines


def render_transit_markdown(case_rec: dict) -> list[str]:
    cl = case_rec["clinical"]
    lines = [f"### Case {case_rec['case']}", ""]
    lines.append("| path | distal transit raw (s) | distal transit calibrated (s) "
                 "| TFC raw (frames) | TFC calibrated (frames) | flag |")
    lines.append("|---|---|---|---|---|---|")
    any_flag = False
    for row in cl["transit_calibration"]:
        flag = row["flag"] or "—"
        if row["flag"]:
            any_flag = True
        lines.append(
            f"| {row['label']} | {fnum(row['distal_raw_s'])} "
            f"| {fnum(row['distal_calibrated_s'])} | {fnum(row['tfc_frames'])} "
            f"| {fnum(row['tfc_calibrated_frames'])} | {flag} |")
    if not cl["transit_calibration"]:
        lines.append("| — | — | — | — | — | — |")
    lines.append("")
    for row in cl["transit_calibration"]:
        if row["unavailable_reason"]:
            lines.append(f"- [{row['path_id']}] unavailable: {row['unavailable_reason']}")
        elif row["reason"]:
            lines.append(f"- [{row['path_id']}] note: {row['reason']}")
    if any_flag:
        lines.append(f"- flagged distal transit (> {TIMI_TRANSIT_S} s): "
                     f"TIMI 1/2 — severe microvascular resistance or near-total flow "
                     f"arrest. This reflects the distal microcirculation and is never "
                     f"attributed to the velocity-calibration scaling.")
    tfc = cl.get("tfc") or {}
    fps = tfc.get("fps") if is_num(tfc.get("fps")) else None
    cutoff = tfc.get("c_lad_cutoff") if is_num(tfc.get("c_lad_cutoff")) else None
    if fps is not None and cutoff is not None:
        lines.append(f"- TFC reference: fps {fnum(fps)}, C-LAD cutoff {fnum(cutoff)} "
                     f"frames (contract C tfc block).")
    else:
        lines.append("- TFC reference: unavailable: no contract C tfc block "
                     "(fps / c_lad_cutoff).")
    lines.append("")
    return lines


def render_markdown(report: dict) -> str:
    tool = report["tool"]
    lines = [
        "# FlowScope clinical hemodynamics report",
        "",
        f"- generated: {tool['generated']}",
        f"- command: `{tool['command']}`",
        f"- runtime_s: {fnum(tool['runtime_s'])}",
        f"- out_dir: {report['out_dir']}",
        f"- cases: {' '.join(report['cases_requested'])}",
        f"- synthetic lesion ladder (dia_reduction %): "
        f"{' '.join(str(s) for s in report['ladder'])}",
        f"- decision rule: vFFR {'<='} {VFFR_THRESHOLD} -> {DECISION_INDICATED}; "
        f"otherwise {DECISION_NORMAL}",
        "- all numbers are compiled from the contract A-D artifact JSONs; missing or "
        "failed stages render as `unavailable: <reason>`, never as placeholder values",
    ]
    if report.get("interrupted"):
        lines.append("- interrupted: true — batch stopped early; sections below cover "
                     "the cases that completed")
    lines.append("")

    for case_rec in report["cases"]:
        lines.append(render_case_markdown(case_rec))
        lines.append("")

    lines.append(f"## {SECTION_LADDER}")
    lines.append("")
    for case_rec in report["cases"]:
        lines.extend(render_ladder_markdown(case_rec))

    lines.append(f"## {SECTION_TRANSIT}")
    lines.append("")
    for case_rec in report["cases"]:
        lines.extend(render_transit_markdown(case_rec))

    lines.append(f"## {SECTION_VERIFICATION}")
    lines.append("")
    checks = []
    for case_rec in report["cases"]:
        checks.extend(case_rec["verification"])
    by_id: dict[str, list[dict]] = {}
    for item in checks:
        by_id.setdefault(item["id"], []).append(item)
    order = ["flow_increase", "trunk_transit", "ladder_monotonic",
             "payload_size", "decision_rule"]
    titles = {
        "flow_increase": f"hyperemic flow increase in [{FLOW_INCREASE_BAND[0]}, "
                         f"{FLOW_INCREASE_BAND[1]}]x per case",
        "trunk_transit": f"primary-trunk calibrated transit within "
                         f"[{TRANSIT_BAND_S[0]}, {TRANSIT_BAND_S[1]}] s per case",
        "ladder_monotonic": "ladder vFFR monotonically non-increasing with severity "
                            "(50 -> 70 -> 90) per case",
        "payload_size": "payload < 2 MB per case",
        "decision_rule": "vFFR decision rule applied consistently per case",
    }
    for cid in order:
        lines.append(f"### {titles[cid]}")
        lines.append("")
        for item in by_id.get(cid, []):
            value = item["value"]
            if cid == "payload_size" and isinstance(value, dict):
                value = (f"{value['max_bytes']} bytes"
                         if value.get("max_bytes") is not None else "n/a")
            elif isinstance(value, list):
                value = ", ".join(fnum(v) for v in value) if value else "n/a"
            else:
                value = fnum(value) if not isinstance(value, str) else value
            lines.append(f"- {item['status']}: case {item['case']} — {item['detail']} "
                         f"(value: {value}; expected: {item['expected']})")
        lines.append("")
    overall = "PASS" if checks and all(i["status"] == "PASS" for i in checks) else "FAIL"
    lines.append(f"- overall: {overall} "
                 f"({sum(1 for i in checks if i['status'] == 'PASS')}/{len(checks)} checks pass)")
    lines.append("")

    lines.append(f"## {SECTION_FAILURES}")
    lines.append("")
    lines.append(f"Mirrors `{report['errors_log']}`.")
    lines.append("")
    if not report["failures"]:
        lines.append(f"No failures recorded — `{report['errors_log']}` was not created.")
    else:
        for rec in report["failures"]:
            lines.append("```text")
            lines.append(error_record_text(rec).rstrip())
            lines.append("```")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def build_report(state: dict) -> dict:
    return {
        "schema": "flowscope.clinical.batch_report",
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
        "ladder": state["ladder"],
        "decision_rule": {
            "threshold": VFFR_THRESHOLD,
            "indicated": DECISION_INDICATED,
            "normal": DECISION_NORMAL,
            "bands_context_only": {"normal": "> 0.80", "borderline": "0.75-0.80",
                                   "severe": "< 0.75"},
        },
        "errors_log": state["errors_log_rel"],
        "interrupted": state["interrupted"],
        "cases": state["case_records"],
        "failures": [
            {k: v for k, v in rec.items() if k != "record_text"}
            for rec in state["error_log"].records
        ],
    }


def write_reports(state: dict) -> None:
    report = build_report(state)
    atomic_write(repo_path(state["report_md"]), render_markdown(report))
    atomic_write(repo_path(state["report_json"]), json.dumps(report, indent=2) + "\n")


# --------------------------------------------------------------------------- #
# CLI / main
# --------------------------------------------------------------------------- #

def discover_cases(out_dir: str) -> list[str]:
    graphs = sorted(repo_path(out_dir).joinpath("rom", "graphs").glob("*_graph.json"))
    return [p.name[: -len("_graph.json")] for p in graphs]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=TOOL_NAME,
        description="FlowScope Phase 2.5: clinical vFFR / stenosis / ACIST RXi batch "
                    "runner (stenosis -> hemodynamics -> synthetic ladder -> RXi "
                    "metrics -> viewer payload per case) and clinical report writer.")
    parser.add_argument("--cases", nargs="+", metavar="CASE", default=None,
                        help="case ids to run (default: discovered from "
                             "{out-dir}/rom/graphs/*_graph.json)")
    parser.add_argument("--ladder", nargs="+", type=int, metavar="PCT",
                        default=list(DEFAULT_LADDER),
                        help="synthetic lesion severities (dia_reduction %) for the "
                             "monotonic ladder (default: 50 70 90)")
    parser.add_argument("--out-dir", default="out",
                        help="pipeline output root; logs, error log and reports live "
                             "here too unless overridden (default: out)")
    parser.add_argument("--errors-log", default=None, metavar="PATH",
                        help="failure log (default: {out-dir}/clinical_errors.log)")
    parser.add_argument("--report", default=None, metavar="PATH",
                        help="Markdown report path "
                             "(default: {out-dir}/clinical_hemodynamics_report.md)")
    parser.add_argument("--report-json", default=None, metavar="PATH",
                        help="machine-readable twin path "
                             "(default: {out-dir}/clinical_hemodynamics_report.json)")
    parser.add_argument("--python", default=sys.executable,
                        help="Python interpreter used to run the stage scripts "
                             "(default: sys.executable)")
    parser.add_argument("--clean", action="store_true",
                        help="remove the stale errors log before running (the runner "
                             "always drops it for idempotent reruns, mirroring "
                             "tools/run_cfd_overnight.py; the flag makes the cleanup "
                             "explicit)")
    args = parser.parse_args(argv)
    seen: set[str] = set()
    args.ladder = [s for s in args.ladder
                   if not (s in seen or seen.add(s))]  # dedupe, keep order
    args.out_dir = rel_str(args.out_dir)
    if args.errors_log is None:
        args.errors_log = f"{args.out_dir}/clinical_errors.log"
    if args.report is None:
        args.report = f"{args.out_dir}/clinical_hemodynamics_report.md"
    if args.report_json is None:
        args.report_json = f"{args.out_dir}/clinical_hemodynamics_report.json"
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    def _sigterm(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _sigterm)

    cases = args.cases if args.cases else discover_cases(args.out_dir)
    logs_dir = repo_path(args.out_dir) / "clinical_logs"
    errors_path = repo_path(args.errors_log)
    # Idempotent reruns overwrite previous outputs: drop the stale error log up
    # front (--clean makes this explicit); it is recreated (append-only) only if
    # this run records a failure, so a fully green run leaves it absent.
    if errors_path.is_file():
        errors_path.unlink()

    state = {
        "t0": time.perf_counter(),
        "command": shlex.join([TOOL_NAME, *(argv if argv is not None else sys.argv[1:])]),
        "out_dir": args.out_dir,
        "cases": cases,
        "ladder": args.ladder,
        "report_md": args.report,
        "report_json": args.report_json,
        "errors_log_rel": rel_str(args.errors_log),
        "interrupted": False,
        "case_records": [],
        "error_log": ErrorLog(errors_path),
    }

    try:
        for case in cases:
            try:
                case_rec = run_case(case, args.ladder, args.out_dir, args.python,
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
                    "ladder_placement": {"branch": None, "s_mm": None,
                                         "unavailable_reason": "case pipeline crashed"},
                    "stages": [],
                }
            try:
                case_rec["clinical"] = compile_case(
                    case, args.ladder, args.out_dir,
                    case_rec.get("stage_trust") or {}, case_rec.get("stages") or [])
            except Exception:  # noqa: BLE001 — report gaps must not kill the batch
                tb = traceback.format_exc()
                state["error_log"].append({
                    "case": case,
                    "stage": "report",
                    "command": None,
                    "exit_code": None,
                    "stderr_tail": "",
                    "exception": tb.strip().splitlines()[-1],
                    "traceback": tb,
                })
                case_rec["clinical"] = empty_clinical(
                    f"report compilation failed: {tb.strip().splitlines()[-1]}")
                case_rec["clinical"]["case"] = case
                case_rec["status"] = "failed"
                case_rec["failing_stage"] = case_rec.get("failing_stage") or "report"
            try:
                case_rec["verification"] = build_verification(case_rec["clinical"])
            except Exception:  # noqa: BLE001 — verification must not kill the batch
                tb = traceback.format_exc()
                state["error_log"].append({
                    "case": case,
                    "stage": "verification",
                    "command": None,
                    "exit_code": None,
                    "stderr_tail": "",
                    "exception": tb.strip().splitlines()[-1],
                    "traceback": tb,
                })
                case_rec["verification"] = [_check(
                    "verification", case, "verification checks computed",
                    False, None, "all checks computed",
                    f"unavailable: verification crashed: {tb.strip().splitlines()[-1]}")]
                case_rec["status"] = "failed"
                case_rec["failing_stage"] = case_rec.get("failing_stage") or "verification"
            state["case_records"].append(case_rec)
            write_reports(state)  # incremental + atomic: interrupted runs stay consistent
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
