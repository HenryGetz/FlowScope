#!/usr/bin/env python3
"""Enumerate the full file tree of the AI-CVM/Cardiac-CT HF dataset.

Writes tools/hf_tree.json with all file paths + sizes, prints a summary and
any *.stl matches. Anonymous API access; no token.
"""
import json
import sys
import urllib.parse
import urllib.request

API = "https://huggingface.co/api/datasets/AI-CVM/Cardiac-CT/tree/main"


def fetch_page(url):
    req = urllib.request.Request(url, headers={"User-Agent": "flowscope-data-acq"})
    with urllib.request.urlopen(req, timeout=120) as r:
        links = r.headers.get("Link", "")
        return json.load(r), links


def next_cursor(links):
    # Link: <url>; rel="next"
    for part in links.split(","):
        if 'rel="next"' in part:
            url = part[part.index("<") + 1 : part.index(">")]
            return url
    return None


def main():
    out_path = sys.argv[1] if len(sys.argv) > 1 else "tools/hf_tree.json"
    entries = []
    url = API + "?recursive=true&limit=1000"
    while url:
        page, links = fetch_page(url)
        entries.extend(page)
        url = next_cursor(links)
        print(f"fetched {len(entries)} entries...", file=sys.stderr)

    files = [e for e in entries if e["type"] == "file"]
    dirs = [e["path"] for e in entries if e["type"] == "directory"]
    with open(out_path, "w") as f:
        json.dump(files, f)

    stls = [e for e in files if e["path"].lower().endswith(".stl")]
    nii = [e for e in files if e["path"].lower().endswith((".nii", ".nii.gz"))]
    print(f"total files={len(files)} dirs={len(dirs)} nii={len(nii)} stl={len(stls)}")
    top_dirs = sorted({d.split("/")[0] for d in dirs} | {e["path"].split("/")[0] for e in files})
    print("top-level entries:", top_dirs)
    # second level distribution
    from collections import Counter
    c = Counter("/".join(e["path"].split("/")[:2]) for e in files)
    for k, v in sorted(c.items()):
        print(f"  {k}: {v}")
    if stls:
        print("STL examples:")
        for e in stls[:20]:
            print(" ", e["path"], e["size"])


if __name__ == "__main__":
    main()
