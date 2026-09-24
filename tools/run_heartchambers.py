#!/usr/bin/env python3
"""Run the 3-pass TotalSegmentator cardiac extraction on one CT and record the passes.

Usage: .venv/bin/python tools/run_heartchambers.py <case_id_or_input> [license_number]
                       [--task {heartchambers_highres,coronary_arteries,veins}] [--demo]

Default: all three passes, executed sequentially (TotalSegmentator model downloads
share one tmp_download_file.zip; concurrent passes corrupt each other), under one case
output root $OUTPUT_DIR = data/segmentations/<case_id>/:
  1) chambers/   -ta heartchambers_highres   7 structures: heart_myocardium,
                 heart_atrium_left, heart_ventricle_left, heart_atrium_right,
                 heart_ventricle_right, aorta, pulmonary_artery (per-structure .nii.gz
                 files; multilabel ids 1..7 per pipeline/structures.py DEFAULT_LABEL_MAP)
  2) coronaries/ -ta coronary_arteries       1 structure: coronary_arteries
  3) veins/      --roi_subset superior_vena_cava inferior_vena_cava --fast on the
                 default 117-class total task (the venae cavae exist only in that
                 schema): superior_vena_cava.nii.gz, inferior_vena_cava.nii.gz
--task runs a single pass only, into the same <root>/<pass dir>/ layout.

Input (first positional arg), resolved in order:
  - an existing NIfTI file (.nii/.nii.gz) or an existing DICOM series directory is
    passed straight to TotalSegmentator -i; case_id derives from the filename stem /
    directory name;
  - anything else is a case id resolving data/raw/totalseg_ct/<case_id>/ct.nii.gz.

Platform autodetect: on Apple Silicon the stock TotalSegmentator engine runs with
--device mps (native Metal GPU); CUDA hosts use gpu; everything else cpu.
Overrides: TOTALSEG_DEVICE (device), TOTALSEG_BIN (engine binary).

--demo (Apple Silicon only) applies the live-demo optimization recipe to the
heartchambers_highres pass (chambers/) wherever that pass runs:
  --ml                writes a single multilabel labels.nii.gz (integer ids 1..7 per
                      pipeline/structures.py DEFAULT_LABEL_MAP; consumable via
                      pipeline/build_cardiac_glb.py --ts-dir) and skips ~8-12s of
                      per-structure compression;
  --resampling_order 1 linear resampling skips ~5-8s of cubic pre-processing;
  PYTORCH_ENABLE_MPS_FALLBACK=1 prevents hard failures on edge-case ops by letting
                      them drop to CPU instead of halting.
Target: end-to-end ~30s-class live demos on M1 Max. The other passes always run
standard.

Run record per pass appended to tools/segmentation_runs.json (task, case_id, command,
input, output_dir, output_root, runtime_s, device, outputs, ...). output_alias_symlink
is null in every record: the case output root data/segmentations/<case_id>/ is a real
directory in this layout (a stale <case_id> -> <case_id>_<task> compatibility symlink
from the old layout must be removed before rerunning that case).

License: heartchambers_highres and coronary_arteries are license-required tasks (the
veins/total pass is not). An academic license is registered machine-wide on this host
via `.venv/bin/totalseg_set_license` (~/.totalsegmentator/config.json), so no flag is
needed; argv[2] (license_number) and TOTALSEG_LICENSE remain optional `-l` overrides.
The task weights are restricted to non-commercial use unless separately licensed
(https://backend.totalsegmentator.com/license-academic/, numbers like 'aca_...'). Only a
user can accept a license; this tool never accepts a license on the user's behalf.
"""
import argparse
import json
import os
import subprocess
import sys
import time

import seg_backend


def resolve_input(target):
    """NIfTI file / DICOM series dir -> (path passed to -i, derived case_id);
    anything else -> case id resolving data/raw/totalseg_ct/<case_id>/ct.nii.gz."""
    if os.path.isdir(target):
        return target, os.path.basename(os.path.normpath(target))
    if os.path.isfile(target):
        name = os.path.basename(target)
        low = name.lower()
        if low.endswith(".nii.gz"):
            return target, name[:-len(".nii.gz")]
        if low.endswith(".nii"):
            return target, name[:-len(".nii")]
        sys.exit(f"direct input must be a NIfTI file (.nii/.nii.gz) or a DICOM series "
                 f"directory; got existing file {target}")
    return f"data/raw/totalseg_ct/{target}/ct.nii.gz", target


def append_run(record):
    runs_path = "tools/segmentation_runs.json"
    runs = json.load(open(runs_path)) if os.path.exists(runs_path) else []
    runs.append(record)
    with open(runs_path, "w") as f:
        json.dump(runs, f, indent=2)


def main():
    ap = argparse.ArgumentParser(
        description="3-pass TotalSegmentator cardiac extraction (chambers/coronaries/veins)")
    ap.add_argument("case_id_or_input", nargs="?", default="s0004",
                    help="case id (data/raw/totalseg_ct/<case_id>/ct.nii.gz), or an "
                         "existing NIfTI file / DICOM series directory passed to -i")
    ap.add_argument("license_number", nargs="?",
                    help="optional -l override (the academic license is registered "
                         "machine-wide on this host)")
    ap.add_argument("--task", choices=seg_backend.PASSES,
                    help="run a single pass only (default: all three, sequentially)")
    ap.add_argument("--demo", action="store_true",
                    help="Apple Silicon: live-demo recipe on the heartchambers_highres pass")
    args = ap.parse_args()

    if args.demo and not seg_backend.is_apple_silicon():
        sys.exit("--demo is an Apple Silicon option (multilabel + linear resampling on the "
                 "mps device); this host is not Apple Silicon — rerun without --demo")
    if args.demo and args.task and args.task != "heartchambers_highres":
        sys.exit("--demo (--ml multilabel labels.nii.gz) applies to the "
                 "heartchambers_highres pass only; rerun with --task heartchambers_highres "
                 "or drop --demo")

    inp, case_id = resolve_input(args.case_id_or_input)
    assert os.path.exists(inp), inp
    root = f"data/segmentations/{case_id}"
    if os.path.islink(root):
        sys.exit(f"{root} is a stale compatibility symlink from the old "
                 f"<case_id>_<task> layout; remove it so the case output root can be a "
                 f"real directory")

    passes = (args.task,) if args.task else seg_backend.PASSES
    license_number = args.license_number or os.environ.get("TOTALSEG_LICENSE")
    device = seg_backend.resolve_device()
    env = {**os.environ, **seg_backend.run_env(device)}
    backend = seg_backend.describe()
    rcs = []
    for task in passes:
        outdir = os.path.join(root, seg_backend.PASS_DIRS[task])
        os.makedirs(outdir, exist_ok=True)
        demo = bool(args.demo and task == "heartchambers_highres")
        cmd = seg_backend.build_command(inp, outdir, task=task, device=device,
                                        license_number=license_number, demo=demo)
        print("running:", " ".join(cmd), flush=True)
        t0 = time.time()
        proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
        wall = time.time() - t0
        print(proc.stdout[-4000:])
        print(proc.stderr[-4000:], file=sys.stderr)
        outputs = sorted(f for f in os.listdir(outdir)) if os.path.isdir(outdir) else []
        append_run({
            "task": task,
            "case_id": case_id,
            "command": " ".join(cmd),
            "input": inp,
            "output_dir": outdir,
            "output_root": root,
            "output_alias_symlink": None,
            "runtime_s": round(wall, 1),
            "device": device,
            "mode": "demo" if demo else "standard",
            "backend": backend,
            "returncode": proc.returncode,
            "outputs": outputs,
            "stdout_tail": proc.stdout[-2000:],
            "stderr_tail": proc.stderr[-2000:],
        })
        print(f"[{task}] wall time {wall:.1f}s device {device} rc {proc.returncode} "
              f"outputs {outputs}")
        rcs.append(proc.returncode)
    sys.exit(next((rc for rc in rcs if rc), 0))


if __name__ == "__main__":
    main()
