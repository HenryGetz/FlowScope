#!/usr/bin/env python3
"""TotalSegmentator backend resolution: device autodetect and command building.

Device: TOTALSEG_DEVICE override wins; else "mps" on Apple Silicon (native Metal),
"gpu" when torch reports CUDA, else "cpu".
Binary: TOTALSEG_BIN override, else <repo>/.venv/bin/TotalSegmentator, else PATH.

Demo mode (demo=True) appends `--ml --resampling_order 1` and requires device "mps".
"""
import json
import os
import platform
import shutil


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
    on_path = shutil.which("TotalSegmentator")
    if on_path:
        return on_path
    raise RuntimeError(
        "TotalSegmentator not found; set TOTALSEG_BIN or install with `.venv/bin/pip install TotalSegmentator`"
    )


def build_command(inp, outdir, task="heartchambers_highres", device=None,
                  license_number=None, demo=False):
    device = device or resolve_device()
    if demo and device != "mps":
        raise ValueError(f"demo mode requires device 'mps', resolved {device!r}")
    cmd = [resolve_bin(), "-i", inp, "-o", outdir, "-ta", task, "-d", device]
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
    demo_cmd = build_command("in.nii.gz", "out", demo=True) if info["apple_silicon"] else None
    print(json.dumps(demo_cmd))


if __name__ == "__main__":
    main()
