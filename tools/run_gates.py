#!/usr/bin/env python3
"""Consolidated verification suite: one command answering "does this tree work?".

Usage: python tools/run_gates.py [--tier {quick,full}] [--python PATH]
                                 [--json out/gates.json]

Runs named gates, prints one PASS/FAIL line per gate, exits 0 iff all pass
(failing gate names are listed on stderr). Tiers:
  quick -- import-smokes, seg-backend (stdlib + venv-import level, CI-safe)
  full  -- quick gates plus artifact-presence, data-sanity, pipeline-probe

--python selects the interpreter for all subprocess smokes (default
<repo_root>/.venv/bin/python when present, else sys.executable); the resolved
interpreter is printed in the run summary and in the JSON `host` block. When
that interpreter cannot import numpy, import-smokes degrades to py_compile
syntax checks (suitable for dependency-less CI worktrees).
"""
import argparse
import datetime
import importlib.util
import json
import os
import platform
import shutil
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENV_PY = os.path.join(REPO, ".venv", "bin", "python")
SUBPROCESS_TIMEOUT = 60

EXPECTED_ARTIFACTS = [
    "out/aggregate_report.json",
    "out/raw_scale_validation.json",
    "out/track2_metrics.json",
    "out/rom/report.json",
    "out/transport/report.json",
    "out/surrogate/metrics.json",
    "data/manifest.json",
    "data/synthetic_corpus/index.json",
    "docs/bmt_value_comparison.md",
    "docs/license_audit.md",
    "docs/lbm_solver_evaluation.md",
]

IMPORT_SMOKE_SCRIPTS = [
    "tools/batch_build.py",
    "tools/aggregate_report.py",
    "tools/make_sample_case.py",
    "pipeline/extract_volume_roi.py",
    "phase2/surrogate/train.py",
    "phase2/surrogate/evaluate.py",
    "phase2/rom/run_zerod.py",
    "phase2/transport/compare.py",
]

GATE_TIERS = {
    "import-smokes": "quick",
    "seg-backend": "quick",
    "artifact-presence": "full",
    "data-sanity": "full",
    "pipeline-probe": "full",
}


def resolve_interp(arg):
    if arg:
        return arg
    return VENV_PY if os.path.exists(VENV_PY) else sys.executable


_HAS_NUMPY = {}


def has_numpy(interp):
    if interp not in _HAS_NUMPY:
        try:
            # -s: ignore user site-packages, i.e. "bare interpreter, no
            # site-packages" — a dep-less CI python3 must degrade even if the
            # user happens to have numpy in ~/.local.
            r = run([interp, "-s", "-c", "import numpy"])
            _HAS_NUMPY[interp] = r.returncode == 0
        except Exception:
            _HAS_NUMPY[interp] = False
    return _HAS_NUMPY[interp]


def run(argv, capture=True):
    return subprocess.run(
        argv, cwd=REPO, capture_output=capture, text=True, timeout=SUBPROCESS_TIMEOUT
    )


def gate_import_smokes(interp):
    problems = []
    if has_numpy(interp):
        degraded = False
        for script in IMPORT_SMOKE_SCRIPTS:
            path = os.path.join(REPO, script)
            if not os.path.exists(path):
                problems.append(f"{script} (missing)")
                continue
            try:
                r = run([interp, path, "--help"])
            except subprocess.TimeoutExpired:
                problems.append(f"{script} --help (timeout)")
                continue
            if r.returncode != 0:
                tail = (r.stderr or r.stdout or "").strip().splitlines()
                problems.append(f"{script} --help exit {r.returncode}: {tail[-1] if tail else ''}")
    else:
        degraded = True
        missing = [s for s in IMPORT_SMOKE_SCRIPTS
                   if not os.path.exists(os.path.join(REPO, s))]
        problems.extend(f"{s} (missing)" for s in missing)
        paths = [os.path.join(REPO, s) for s in IMPORT_SMOKE_SCRIPTS
                 if s not in missing]
        checker = (
            "import sys\n"
            "for p in sys.argv[1:]:\n"
            "    with open(p, 'rb') as fh:\n"
            "        compile(fh.read(), p, 'exec')\n"
        )
        try:
            r = run([interp, "-c", checker] + paths)
            if r.returncode != 0:
                tail = (r.stderr or "").strip().splitlines()
                problems.append(
                    f"py_compile failed exit {r.returncode}: {tail[-1] if tail else ''}"
                )
        except subprocess.TimeoutExpired:
            problems.append("py_compile (timeout)")
    if not shutil.which("node"):
        problems.append("node (not found on PATH)")
    else:
        try:
            r = run(["node", "-e", "require('node:https')"])
            if r.returncode != 0:
                problems.append(f"node -e require('node:https') exit {r.returncode}")
        except subprocess.TimeoutExpired:
            problems.append("node -e require('node:https') (timeout)")
    if problems:
        return False, "; ".join(problems)
    if degraded:
        return True, f"degraded: py_compile (no numpy in {interp})"
    return True, f"{len(IMPORT_SMOKE_SCRIPTS)} scripts --help ok; node import ok"


def _load_seg_backend():
    path = os.path.join(REPO, "tools", "seg_backend.py")
    spec = importlib.util.spec_from_file_location("seg_backend_gatecheck", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def gate_seg_backend(interp):
    # Subprocess: exit 0 and print valid JSON with platform/machine/device/bin.
    try:
        r = run([interp, os.path.join("tools", "seg_backend.py")])
    except subprocess.TimeoutExpired:
        return False, "tools/seg_backend.py (timeout)"
    if r.returncode != 0:
        tail = (r.stderr or "").strip().splitlines()
        return False, f"tools/seg_backend.py exit {r.returncode}: {tail[-1] if tail else ''}"
    try:
        info = json.loads(r.stdout.splitlines()[0])
    except (IndexError, ValueError) as e:
        return False, f"tools/seg_backend.py output not valid JSON: {e}"
    missing = [k for k in ("platform", "machine", "device", "bin") if k not in info]
    if missing:
        return False, f"tools/seg_backend.py JSON missing keys: {', '.join(missing)}"

    # In-process demo-guard check.
    try:
        mod = _load_seg_backend()
    except Exception as e:
        return False, f"import tools/seg_backend.py failed: {e!r}"
    orig_silicon = mod.is_apple_silicon
    orig_device_env = os.environ.get("TOTALSEG_DEVICE")
    notes = []
    try:
        os.environ.pop("TOTALSEG_DEVICE", None)
        mod.is_apple_silicon = lambda: True
        try:
            cmd = mod.build_command("i", "o", demo=True)
        except RuntimeError:
            # binary not installed here: stub resolution, we test command shape
            mod.resolve_bin = lambda: "TotalSegmentator"
            cmd = mod.build_command("i", "o", demo=True)
            notes.append("resolve_bin stubbed (TotalSegmentator absent)")
        if cmd[-3:] != ["--ml", "--resampling_order", "1"]:
            return False, f"demo command does not end with --ml flags: {cmd!r}"
        if os.path.join("o", "labels.nii.gz") not in cmd:
            return False, f"demo command does not name labels.nii.gz: {cmd!r}"
        # Restore monkeypatch; demo must raise when resolve_device() == 'cpu'.
        mod.is_apple_silicon = orig_silicon
        os.environ["TOTALSEG_DEVICE"] = "cpu"
        if mod.resolve_device() != "cpu":
            return False, "resolve_device() != 'cpu' under TOTALSEG_DEVICE=cpu"
        try:
            mod.build_command("i", "o", demo=True)
        except ValueError:
            pass
        else:
            return False, "build_command(demo=True) did not raise on device 'cpu'"
    finally:
        mod.is_apple_silicon = orig_silicon
        if orig_device_env is None:
            os.environ.pop("TOTALSEG_DEVICE", None)
        else:
            os.environ["TOTALSEG_DEVICE"] = orig_device_env
    detail = f"device={info['device']} bin={info['bin']}"
    if notes:
        detail += f" ({'; '.join(notes)})"
    return True, detail


def gate_artifact_presence(_interp):
    problems = []
    for rel in EXPECTED_ARTIFACTS:
        path = os.path.join(REPO, rel)
        if not os.path.exists(path):
            problems.append(f"{rel} (missing)")
        elif os.path.getsize(path) == 0:
            problems.append(f"{rel} (empty)")
    if problems:
        return False, "; ".join(problems)
    return True, f"{len(EXPECTED_ARTIFACTS)} artifacts present and non-empty"


def _load_json(rel):
    with open(os.path.join(REPO, rel)) as f:
        return json.load(f)


def gate_data_sanity(_interp):
    problems = []
    try:
        manifest = _load_json("data/manifest.json")
    except (OSError, ValueError) as e:
        return False, f"data/manifest.json unreadable: {e}"
    rows = manifest.get("artifacts")
    if not isinstance(rows, list):
        problems.append(f"manifest artifacts not a list: {type(rows).__name__}")
    else:
        if len(rows) < 1400:
            problems.append(f"manifest artifacts rows {len(rows)} < 1400")
        no_path = sum(1 for r in rows if not (isinstance(r, dict) and "path" in r))
        if no_path:
            problems.append(f"manifest artifacts rows without path key: {no_path}")

    try:
        agg = _load_json("out/aggregate_report.json")
        if agg.get("n_cases") != 355:
            problems.append(f"aggregate_report n_cases {agg.get('n_cases')!r} != 355")
    except (OSError, ValueError) as e:
        problems.append(f"out/aggregate_report.json unreadable: {e}")

    try:
        corpus = _load_json("data/synthetic_corpus/index.json")
        for key, want in (("n_train", 400), ("n_val", 50)):
            got = corpus.get(key)
            if got != want:
                problems.append(f"corpus index {key} {got!r} != {want}")
        samples = corpus.get("samples")
        n = len(samples) if isinstance(samples, list) else None
        if n != 450:
            problems.append(f"corpus index len(samples) {n!r} != 450")
    except (OSError, ValueError) as e:
        problems.append(f"data/synthetic_corpus/index.json unreadable: {e}")

    if problems:
        return False, "; ".join(problems)
    return True, f"manifest rows {len(rows)}; n_cases 355; n_train 400 n_val 50 samples 450"


def gate_pipeline_probe(interp):
    path = os.path.join("pipeline", "build_cardiac_glb.py")
    if not os.path.exists(os.path.join(REPO, path)):
        return False, f"{path} (missing)"
    try:
        r = run([interp, path, "--help"])
    except subprocess.TimeoutExpired:
        return False, f"{path} --help (timeout)"
    if r.returncode != 0:
        tail = (r.stderr or r.stdout or "").strip().splitlines()
        return False, f"{path} --help exit {r.returncode}: {tail[-1] if tail else ''}"
    return True, "--help ok"


GATES = [
    ("import-smokes", gate_import_smokes),
    ("seg-backend", gate_seg_backend),
    ("artifact-presence", gate_artifact_presence),
    ("data-sanity", gate_data_sanity),
    ("pipeline-probe", gate_pipeline_probe),
]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tier", choices=("quick", "full"), default="full")
    ap.add_argument("--python", help="interpreter for subprocess smokes "
                    "(default <repo_root>/.venv/bin/python if present, else sys.executable)")
    ap.add_argument("--json", help="write JSON result summary to this path")
    args = ap.parse_args(argv)

    interp = resolve_interp(args.python)
    print(f"interpreter: {interp}")

    results = []
    for name, fn in GATES:
        tier = GATE_TIERS[name]
        if args.tier == "quick" and tier != "quick":
            continue
        try:
            ok, detail = fn(interp)
        except Exception as e:
            ok, detail = False, f"gate crashed: {e!r}"
        status = "PASS" if ok else "FAIL"
        print(f"{status}  {name}  {detail}")
        results.append(
            {"name": name, "tier": tier, "status": status, "detail": detail}
        )

    passed = [r["name"] for r in results if r["status"] == "PASS"]
    failed = [r["name"] for r in results if r["status"] == "FAIL"]

    if args.json:
        out_path = args.json if os.path.isabs(args.json) else os.path.join(REPO, args.json)
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        with open(out_path, "w") as f:
            json.dump(
                {
                    "created": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "tier": args.tier,
                    "host": {
                        "name": platform.node() or platform.platform(),
                        "platform": platform.system(),
                        "machine": platform.machine(),
                        "python": interp,
                    },
                    "gates": results,
                    "passed": passed,
                    "failed": failed,
                },
                f,
                indent=2,
            )
            f.write("\n")

    if failed:
        print(f"failing gates: {', '.join(failed)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
