#!/usr/bin/env python3
"""TotalSegmentator backend resolution: device autodetect and command building.

Device: TOTALSEG_DEVICE override wins; else "mps" on Apple Silicon (native Metal),
"gpu" when torch reports CUDA, else "cpu".
Binary: TOTALSEG_BIN override, else <repo>/.venv/bin/TotalSegmentator, else PATH.

Passes (PASSES; the 3-pass extraction writes under one case output root
data/segmentations/<case_id>/ with a subdir per pass, PASS_DIRS):
  heartchambers_highres  -ta heartchambers_highres -> chambers/   (heart_myocardium,
                         heart_atrium_left, heart_ventricle_left, heart_atrium_right,
                         heart_ventricle_right, aorta, pulmonary_artery)
  coronary_arteries      -ta coronary_arteries -> coronaries/    (coronary_arteries)
  veins                  --roi_subset superior_vena_cava inferior_vena_cava --fast on
                         the default 117-class total task -> veins/ (the venae cavae
                         exist only in that schema)
tools/run_heartchambers.py runs passes sequentially: TotalSegmentator model downloads
share one tmp_download_file.zip and concurrent passes corrupt each other.

License: heartchambers_highres and coronary_arteries are license-required tasks; an
academic license is registered machine-wide via totalseg_set_license
(~/.totalsegmentator/config.json), so the license_number `-l` override (and the
TOTALSEG_LICENSE env passthrough) is normally unnecessary.

Demo mode (demo=True, heartchambers_highres pass only) appends `--ml
--resampling_order 1`, names the output `labels.nii.gz` inside outdir (2.18.0 `--ml`
writes `<path>.nii` outside a bare-dir `-o`, so the demo path names the file
explicitly), and requires device "mps".
"""
import json
import os
import platform
import shutil
import sys


def is_apple_silicon():
    return platform.system() == "Darwin" and platform.machine() in ("arm64", "aarch64")


def resolve_device():
    override = os.environ.get("TOTALSEG_DEVICE")
    if override:
        return override
    if is_apple_silicon():
        return "mps"
    try:
        import torch
        if torch.cuda.is_available():
            return "gpu"
    except Exception:
        pass
    return "cpu"


def resolve_bin():
    override = os.environ.get("TOTALSEG_BIN")
    if override:
        return override
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    venv_bin = os.path.join(repo_root, ".venv", "bin", "TotalSegmentator")
    if os.path.exists(venv_bin):
        return venv_bin
    next_to_python = os.path.join(os.path.dirname(sys.executable), "TotalSegmentator")
    if os.path.exists(next_to_python):
        return next_to_python
    on_path = shutil.which("TotalSegmentator")
    if on_path:
        return on_path
    raise RuntimeError(
        "TotalSegmentator not found; set TOTALSEG_BIN or install with `.venv/bin/pip install TotalSegmentator`"
    )


PASSES = ("heartchambers_highres", "coronary_arteries", "veins")

# pass name -> subdir under the one case output root data/segmentations/<case_id>/
PASS_DIRS = {"heartchambers_highres": "chambers",
             "coronary_arteries": "coronaries",
             "veins": "veins"}

# pass name -> task/roi argv ("veins" restricts the default 117-class total task to the
# venae cavae -- SVC/IVC exist only in that schema)
PASS_ARGS = {
    "heartchambers_highres": ["-ta", "heartchambers_highres"],
    "coronary_arteries": ["-ta", "coronary_arteries"],
    "veins": ["--roi_subset", "superior_vena_cava", "inferior_vena_cava", "--fast"],
}


def build_command(inp, outdir, task="heartchambers_highres", device=None,
                  license_number=None, demo=False):
    device = device or resolve_device()
    if demo and task != "heartchambers_highres":
        raise ValueError(f"demo mode (--ml multilabel labels.nii.gz, ids 1..7 per "
                         f"pipeline/structures.py DEFAULT_LABEL_MAP) is "
                         f"heartchambers_highres-specific, got task {task!r}")
    if demo and device != "mps":
        raise ValueError(f"demo mode requires device 'mps', resolved {device!r}")
    out = os.path.join(outdir, "labels.nii.gz") if demo else outdir
    cmd = [resolve_bin(), "-i", inp, "-o", out,
           *PASS_ARGS.get(task, ["-ta", task]), "-d", device]
    if license_number:
        cmd += ["-l", license_number]
    if demo:
        cmd += ["--ml", "--resampling_order", "1"]
    return cmd


def run_env(device=None):
    if (device or resolve_device()) == "mps":
        return {"PYTORCH_ENABLE_MPS_FALLBACK": "1"}
    return {}


def describe():
    try:
        import torch
        torch_version = torch.__version__
    except Exception:
        torch_version = None
    return {
        "platform": platform.system(),
        "machine": platform.machine(),
        "apple_silicon": is_apple_silicon(),
        "device": resolve_device(),
        "bin": resolve_bin(),
        "torch_version": torch_version,
    }


def main():
    info = describe()
    print(json.dumps(info))
    print(json.dumps(build_command("in.nii.gz", "out")))
    for task in PASSES:
        print(json.dumps(build_command("in.nii.gz", os.path.join("out", PASS_DIRS[task]),
                                       task=task)))
    demo_cmd = build_command("in.nii.gz", "out", demo=True) if info["apple_silicon"] else None
    print(json.dumps(demo_cmd))


if __name__ == "__main__":
    main()
