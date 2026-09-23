#!/usr/bin/env python3
"""Fetch ImageCAS cases from HF dataset AI-CVM/Cardiac-CT into data/raw/imagecas/<case>/.

The repo is gated (401 GatedRepo on data-file resolve). Once access has been granted at
https://huggingface.co/datasets/AI-CVM/Cardiac-CT, provide a token and re-run:

    HF_TOKEN=hf_xxx .venv/bin/python tools/hf_fetch.py            # default 3 cases
    HF_TOKEN=hf_xxx .venv/bin/python tools/hf_fetch.py 10016975   # specific case ids

Per case it fetches the single multilabel segmentation <case>.nii.gz (14 structures as
integer labels, see data/manifest.json label_map) and the CT volume <case>_0000.nii.gz.
Volumes over 300 MB are skipped; total fetch is capped at 6 GB.
"""
import os
import sys
import urllib.error
import urllib.request

BASE = "https://huggingface.co/datasets/AI-CVM/Cardiac-CT/resolve/main/Train(ImageCAS)"
DEST = "data/raw/imagecas"
MAX_VOLUME_BYTES = 300 * 1024 * 1024
TOTAL_CAP = 6 * 1024 * 1024 * 1024

# smallest complete cases (volume MB): chosen to stay well under the caps
DEFAULT_CASES = ["12067914", "11644536", "12054903"]


def headers():
    h = {"User-Agent": "flowscope-data-acq"}
    tok = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_TOKEN")
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h


def fetch(remote_path, out_path):
    url = f"{BASE}/{urllib.request.quote(remote_path)}"
    req = urllib.request.Request(url, headers=headers())
    with urllib.request.urlopen(req, timeout=600) as r:
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "wb") as f:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
    return os.path.getsize(out_path)


def main():
    cases = sys.argv[1:] or DEFAULT_CASES
    total = 0
    for case in cases:
        for remote, fname, limit in (
            (f"segmentations/{case}.nii.gz", f"{case}.nii.gz", None),
            (f"images/{case}_0000.nii.gz", f"{case}_0000.nii.gz", MAX_VOLUME_BYTES),
        ):
            out = os.path.join(DEST, case, fname)
            if os.path.exists(out):
                print(f"skip (exists): {out}")
                continue
            # size probe via tree listing is in tools/hf_tree.json; rely on Content-Length here
            req = urllib.request.Request(f"{BASE}/{urllib.request.quote(remote)}", headers=headers(), method="HEAD")
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    size = int(r.headers.get("Content-Length", "0"))
            except urllib.error.HTTPError as e:
                if e.code in (401, 403):
                    print(f"GATED ({e.code}): {remote} -> {e.headers.get('X-Error-Message')}")
                    print("Accept dataset terms at https://huggingface.co/datasets/AI-CVM/Cardiac-CT and set HF_TOKEN.")
                    sys.exit(2)
                raise
            if limit and size > limit:
                print(f"skip (volume {size} bytes > {limit}): {remote}")
                continue
            if total + size > TOTAL_CAP:
                print(f"skip (total cap): {remote}")
                continue
            n = fetch(remote, out)
            total += n
            print(f"fetched {out} ({n} bytes)")
    print(f"done, {total} bytes total")


if __name__ == "__main__":
    main()
