#!/usr/bin/env python3
"""Range-extract selected case directories from the TotalSegmentator CT dataset v2
Zenodo zip (23.6 GB) using remotezip, without downloading the whole archive.

Usage:
  .venv/bin/python tools/ts_fetch.py list              # list members matching case dirs
  .venv/bin/python tools/ts_fetch.py fetch s0001 [...] # extract whole case dirs to data/raw/totalseg_ct/
"""
import os
import sys
import time

from remotezip import RemoteZip

URL = "https://zenodo.org/records/8367088/files/Totalsegmentator_dataset_v2.zip?download=1"
DEST = "data/raw/totalseg_ct"


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "list"
    cases = sys.argv[2:]
    t0 = time.time()
    with RemoteZip(URL) as z:
        names = z.namelist()
        print(f"{len(names)} members in zip ({time.time()-t0:.1f}s)")
        if mode == "list":
            top = {}
            for n in names:
                parts = n.split("/")
                if len(parts) >= 2 and parts[0].startswith("s"):
                    top.setdefault(parts[0], []).append(n)
            print(f"{len(top)} case dirs")
            for case in sorted(top)[:15]:
                total = sum(i.file_size for i in z.infolist() if i.filename.startswith(case + "/"))
                members = [m for m in top[case] if not m.endswith("/")]
                print(f"  {case}: {len(members)} files, {total/1e6:.1f} MB")
                for m in members[:8]:
                    print("     ", m)
        elif mode == "fetch":
            total_bytes = 0
            for case in cases:
                prefix = case.rstrip("/") + "/"
                members = [n for n in names if n.startswith(prefix) and not n.endswith("/")]
                infos = [i for i in z.infolist() if i.filename.startswith(prefix) and not i.filename.endswith("/")]
                size = sum(i.file_size for i in infos)
                print(f"extracting {case}: {len(members)} files, {size/1e6:.1f} MB")
                for n in members:
                    out = os.path.join(DEST, n)
                    os.makedirs(os.path.dirname(out), exist_ok=True)
                    with z.open(n) as src, open(out, "wb") as dst:
                        data = src.read()
                        dst.write(data)
                    total_bytes += len(data)
                    print(f"    {n} -> {len(data)} bytes")
            print(f"done, {total_bytes} bytes in {time.time()-t0:.1f}s")
        else:
            raise SystemExit(f"unknown mode {mode}")


if __name__ == "__main__":
    main()
