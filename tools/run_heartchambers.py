#!/usr/bin/env python3
"""Run TotalSegmentator -ta heartchambers_highres on one acquired raw CT and record the run.

Usage: .venv/bin/python tools/run_heartchambers.py <case_id> [license_number] [--demo]

Platform autodetect: on Apple Silicon the stock TotalSegmentator engine runs with
--device mps (native Metal GPU); CUDA hosts use gpu; everything else cpu.
Overrides: TOTALSEG_DEVICE (device), TOTALSEG_BIN (engine binary).

--demo (Apple Silicon only) is the live-demo optimization recipe:
  --ml                writes a single multilabel labels.nii.gz (integer ids 1..7 per
                      pipeline/structures.py DEFAULT_LABEL_MAP; consumable via
                      pipeline/build_cardiac_glb.py --ts-dir) and skips ~8-12s of
                      per-structure compression;
  --resampling_order 1 linear resampling skips ~5-8s of cubic pre-processing;
  PYTORCH_ENABLE_MPS_FALLBACK=1 prevents hard failures on edge-case ops by letting
                      them drop to CPU instead of halting.
Target: end-to-end ~30s-class live demos on M1 Max.

Input : data/raw/totalseg_ct/<case_id>/ct.nii.gz
Output: data/segmentations/<case_id>_heartchambers_highres/ containing per-structure
        .nii.gz files (standard mode) or a single labels.nii.gz (demo mode: integer
        ids 1..7 per pipeline/structures.py DEFAULT_LABEL_MAP)
        + compatibility symlink data/segmentations/<case_id> -> <case_id>_heartchambers_highres
Run record appended to tools/segmentation_runs.json (runtime_s, device, outputs).

heartchambers_highres requires a TotalSegmentator license (non-commercial free at
https://backend.totalsegmentator.com/license-academic/, number like 'aca_...'). Pass it as
argv[2] or via TOTALSEG_LICENSE. Only a user can accept a license; this tool never does.
"""
import json
import os
import subprocess
import sys
import time

import seg_backend

args = [a for a in sys.argv[1:] if a != "--demo"]
demo = "--demo" in sys.argv[1:]
if demo and not seg_backend.is_apple_silicon():
    sys.exit("--demo is an Apple Silicon option (multilabel + linear resampling on the "
             "mps device); this host is not Apple Silicon — rerun without --demo")

case = args[0] if args else "s0004"
inp = f"data/raw/totalseg_ct/{case}/ct.nii.gz"
outdir = f"data/segmentations/{case}_heartchambers_highres"
assert os.path.exists(inp), inp

license_number = args[1] if len(args) > 1 else os.environ.get("TOTALSEG_LICENSE")
cmd = seg_backend.build_command(inp, outdir, task="heartchambers_highres",
                                license_number=license_number, demo=demo)
device = seg_backend.resolve_device()
env = {**os.environ, **seg_backend.run_env(device)}
print("running:", " ".join(cmd), flush=True)
t0 = time.time()
proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
wall = time.time() - t0
print(proc.stdout[-4000:])
print(proc.stderr[-4000:], file=sys.stderr)

outputs = sorted(f for f in os.listdir(outdir)) if os.path.isdir(outdir) else []
link = f"data/segmentations/{case}"
if os.path.isdir(outdir) and not os.path.exists(link):
    os.symlink(os.path.basename(outdir), link)

runs_path = "tools/segmentation_runs.json"
runs = json.load(open(runs_path)) if os.path.exists(runs_path) else []
runs.append({
    "task": "heartchambers_highres",
    "case_id": case,
    "command": " ".join(cmd),
    "input": inp,
    "output_dir": outdir,
    "output_alias_symlink": link,
    "runtime_s": round(wall, 1),
    "device": device,
    "mode": "demo" if demo else "standard",
    "backend": seg_backend.describe(),
    "returncode": proc.returncode,
    "outputs": outputs,
    "stdout_tail": proc.stdout[-2000:],
    "stderr_tail": proc.stderr[-2000:],
})
with open(runs_path, "w") as f:
    json.dump(runs, f, indent=2)
print(f"wall time {wall:.1f}s device {device} rc {proc.returncode} outputs {outputs}")
sys.exit(proc.returncode)
