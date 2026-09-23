#!/usr/bin/env python3
"""Run TotalSegmentator -ta heartchambers_highres on one acquired raw CT and record the run.

Usage: .venv/bin/python tools/run_heartchambers.py <case_id> [license_number]

Input : data/raw/totalseg_ct/<case_id>/ct.nii.gz
Output: data/segmentations/<case_id>_heartchambers_highres/ (per-structure .nii.gz)
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

TS_BIN = "/home/wavy/ai/flowscope/.venv/bin/TotalSegmentator"

case = sys.argv[1] if len(sys.argv) > 1 else "s0004"
inp = f"data/raw/totalseg_ct/{case}/ct.nii.gz"
outdir = f"data/segmentations/{case}_heartchambers_highres"
assert os.path.exists(inp), inp

import torch
device = "gpu" if torch.cuda.is_available() else "cpu"

cmd = [TS_BIN, "-i", inp, "-o", outdir, "-ta", "heartchambers_highres"]
license_number = sys.argv[2] if len(sys.argv) > 2 else os.environ.get("TOTALSEG_LICENSE")
if license_number:
    cmd += ["-l", license_number]
print("running:", " ".join(cmd), flush=True)
t0 = time.time()
proc = subprocess.run(cmd, capture_output=True, text=True)
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
    "returncode": proc.returncode,
    "outputs": outputs,
    "stdout_tail": proc.stdout[-2000:],
    "stderr_tail": proc.stderr[-2000:],
})
with open(runs_path, "w") as f:
    json.dump(runs, f, indent=2)
print(f"wall time {wall:.1f}s device {device} rc {proc.returncode} outputs {outputs}")
sys.exit(proc.returncode)
