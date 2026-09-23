#!/usr/bin/env python3
"""Range-extract case members from the ImageCAS release on Kaggle.

Upload layout (empirically verified): each Kaggle file is a one-entry wrapper ZIP whose
member has the same name (the uploader wrapped every archive volume to dodge Kaggle's
auto-unzip). The INNER files NNN-MMM.z01..z04 + NNN-MMM.change2zip ("change to zip" rename
hint) are the five volumes of one split ZIP whose members are the case files
<case_id>/img.nii.gz and <case_id>/label.nii.gz. Inner volumes are deflate streams inside
their wrappers, so members are reached by range-fetching wrapper bytes and inflating
incrementally from each volume's start (members early in a volume are cheap to reach).

Usage:
  tools/kaggle_fetch.py list GROUP             # parse the split-zip CD, list members
  tools/kaggle_fetch.py fetch GROUP MEMBER...  # extract members to data/raw/imagecas/<case_id>/

Downloads use https://www.kaggle.com/api/v1/datasets/download/xiaoweixumedicalai/imagecas/<file>
(open, Range-capable; sub-path names must be percent-encoded). Total fetched bytes are
kept under a 4 GiB cap.
"""
import json
import os
import struct
import sys
import urllib.parse
import urllib.request
import zlib

BASE = "https://www.kaggle.com/api/v1/datasets/download/xiaoweixumedicalai/imagecas/"
DEST = "data/raw/imagecas"
TOTAL_CAP = 4 * 1024 * 1024 * 1024
CHUNK = 32 * 1024 * 1024

BYTES_FETCHED = 0


def _headers():
    h = {"User-Agent": "flowscope-data-acq"}
    try:
        key = json.load(open(os.path.expanduser("~/.kaggle/kaggle.json"))).get("key")
        if key:
            h["Authorization"] = f"Bearer {key}"
    except Exception:
        pass
    return h


def fetch_range(name, start, end):
    """Fetch inclusive byte range of a Kaggle file (percent-encode the name)."""
    global BYTES_FETCHED
    url = BASE + urllib.parse.quote(name, safe="")
    req = urllib.request.Request(url, headers={**_headers(), "Range": f"bytes={start}-{end}"})
    with urllib.request.urlopen(req, timeout=600) as r:
        data = r.read()
    BYTES_FETCHED += len(data)
    if BYTES_FETCHED > TOTAL_CAP:
        raise IOError(f"total fetched bytes exceeded cap ({BYTES_FETCHED})")
    return data


def fetch_size(name):
    url = BASE + urllib.parse.quote(name, safe="")
    req = urllib.request.Request(url, headers={**_headers(), "Range": "bytes=0-0"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return int(r.headers["Content-Range"].rsplit("/", 1)[1])


def parse_zip_cd(name, fsize):
    """Parse the central directory of a standalone (wrapper) zip file. Returns entries."""
    tail = fetch_range(name, max(0, fsize - 131072), fsize - 1)
    tail_base = max(0, fsize - 131072)
    eocd = tail.rfind(b"PK\x05\x06")
    assert eocd >= 0, f"{name}: no EOCD"
    cd_size, cd_off = struct.unpack("<II", tail[eocd + 12:eocd + 20])
    loc = tail.rfind(b"PK\x06\x07", 0, eocd)
    if cd_off == 0xFFFFFFFF or cd_size == 0xFFFFFFFF or loc >= 0:
        assert loc >= 0, f"{name}: zip64 locator missing"
        z64_off = struct.unpack("<Q", tail[loc + 8:loc + 16])[0]
        rec = fetch_range(name, z64_off, z64_off + 55)
        assert rec[:4] == b"PK\x06\x06", f"{name}: bad zip64 EOCD"
        cd_size, cd_off = struct.unpack("<QQ", rec[40:56])
    else:
        cd_off += 0  # file-relative
    cd = fetch_range(name, cd_off, cd_off + cd_size - 1)
    return parse_cd_bytes(cd)


def parse_cd_bytes(cd):
    entries, p = [], 0
    while p < len(cd):
        assert cd[p:p + 4] == b"PK\x01\x02", (p, cd[p:p + 4])
        (method,) = struct.unpack("<H", cd[p + 10:p + 12])
        comp, uncomp = struct.unpack("<II", cd[p + 20:p + 28])
        fname_len, extra_len, comment_len = struct.unpack("<HHH", cd[p + 28:p + 34])
        (disk,) = struct.unpack("<H", cd[p + 34:p + 36])
        (lho,) = struct.unpack("<I", cd[p + 42:p + 46])
        name = cd[p + 46:p + 46 + fname_len].decode("utf-8", "replace")
        extra = cd[p + 46 + fname_len:p + 46 + fname_len + extra_len]
        if 0xFFFFFFFF in (comp, uncomp, lho):
            q = 0
            while q + 4 <= len(extra):
                eid, esz = struct.unpack("<HH", extra[q:q + 4])
                if eid == 0x0001:
                    r = q + 4
                    if uncomp == 0xFFFFFFFF:
                        uncomp = struct.unpack("<Q", extra[r:r + 8])[0]; r += 8
                    if comp == 0xFFFFFFFF:
                        comp = struct.unpack("<Q", extra[r:r + 8])[0]; r += 8
                    if lho == 0xFFFFFFFF:
                        lho = struct.unpack("<Q", extra[r:r + 8])[0]
                    break
                q += 4 + esz
        entries.append({"name": name, "method": method, "comp": comp,
                        "uncomp": uncomp, "lho": lho, "disk": disk})
        p += 46 + fname_len + extra_len + comment_len
    return entries


class InnerVolume:
    """One inner split-zip volume: the deflate member of its wrapper zip file."""

    def __init__(self, wrapper_name):
        self.wrapper = wrapper_name
        self.fsize = fetch_size(wrapper_name)
        ents = parse_zip_cd(wrapper_name, self.fsize)
        assert len(ents) == 1, f"{wrapper_name}: expected 1 entry, got {len(ents)}"
        self.entry = ents[0]
        hdr = fetch_range(wrapper_name, self.entry["lho"], self.entry["lho"] + 29)
        assert hdr[:4] == b"PK\x03\x04", wrapper_name
        fnl, exl = struct.unpack("<HH", hdr[26:30])
        self.data_start = self.entry["lho"] + 30 + fnl + exl
        assert self.entry["method"] == 8, f"{wrapper_name}: wrapper member method {self.entry['method']}"
        self.inner_size = self.entry["uncomp"]
        self._dobj = zlib.decompressobj(-15)
        self._comp_pos = 0        # compressed bytes consumed from member start
        self._inner_pos = 0       # inner bytes produced
        self.buf = bytearray()    # all inner bytes produced so far (inflate cache)

    def ensure(self, upto):
        """Inflate inner bytes until position `upto` is available in the cache."""
        while self._inner_pos < upto and self._comp_pos < self.entry["comp"]:
            n = min(CHUNK, self.entry["comp"] - self._comp_pos)
            chunk = fetch_range(self.wrapper, self.data_start + self._comp_pos,
                                self.data_start + self._comp_pos + n - 1)
            self._comp_pos += n
            produced = self._dobj.decompress(chunk)
            self.buf += produced
            self._inner_pos += len(produced)
        return self._inner_pos

    def read(self, rel, n):
        """Read inner bytes [rel, rel+n) (inflating from the member start as needed)."""
        assert rel + n <= self.inner_size, (rel, n, self.inner_size)
        self.ensure(rel + n)
        assert rel + n <= self._inner_pos
        return bytes(self.buf[rel:rel + n])


class SplitZip:
    """The inner split zip: z01..z04 data volumes + change2zip tail volume (CD + EOCD)."""

    def __init__(self, group):
        self.group = group
        names = [f"{group}.z{i:02d}" for i in range(1, 5)] + [f"{group}.change2zip"]
        self.vols = [InnerVolume(n) for n in names]
        self.cum = []
        acc = 0
        for v in self.vols:
            self.cum.append(acc)
            acc += v.inner_size
        self.stream_size = acc
        self.entries = None

    def parse_cd(self):
        tailv = self.vols[-1]
        base = max(0, tailv.inner_size - 131072)
        tail = tailv.read(base, tailv.inner_size - base)
        eocd = tail.rfind(b"PK\x05\x06")
        assert eocd >= 0, "inner zip: no EOCD"
        cd_size, cd_off = struct.unpack("<II", tail[eocd + 12:eocd + 20])
        # classic spanned zip: record offsets are relative to the volume holding them
        vol_base = self.cum[-1]
        if cd_off == 0xFFFFFFFF or cd_size == 0xFFFFFFFF:
            loc = tail.rfind(b"PK\x06\x07", 0, eocd)
            assert loc >= 0, "inner zip: zip64 locator missing"
            z64_off = struct.unpack("<Q", tail[loc + 8:loc + 16])[0]
            rec = self._read_stream(vol_base + z64_off, 56)
            assert rec[:4] == b"PK\x06\x06", "inner zip: bad zip64 EOCD"
            cd_size, cd_off = struct.unpack("<QQ", rec[40:56])
        cd = self._read_stream(vol_base + cd_off, cd_size)
        self.entries = parse_cd_bytes(cd)
        return self.entries

    def _vol_of(self, abs_off):
        for i in range(len(self.vols) - 1, -1, -1):
            if abs_off >= self.cum[i]:
                return i, abs_off - self.cum[i]
        raise IOError("offset before stream start")

    def _read_stream(self, abs_off, length):
        """Read inner bytes [abs_off, abs_off+length) (may span volumes)."""
        out = bytearray()
        end = abs_off + length
        pos = abs_off
        while pos < end:
            i, rel = self._vol_of(pos)
            take = min(end - pos, self.vols[i].inner_size - rel)
            out += self.vols[i].read(rel, take)
            pos += take
        return bytes(out)

    def extract(self, entry):
        # lho is relative to the volume named by the entry's disk-start (classic span)
        abs_lho = self.cum[entry["disk"]] + entry["lho"]
        hdr = self._read_stream(abs_lho, 30)
        assert hdr[:4] == b"PK\x03\x04", (entry["name"], hdr[:4])
        fnl, exl = struct.unpack("<HH", hdr[26:30])
        data_off = abs_lho + 30 + fnl + exl
        raw = self._read_stream(data_off, entry["comp"])
        if entry["method"] == 0:
            data = raw
        elif entry["method"] == 8:
            data = zlib.decompress(raw, -15)
        else:
            raise IOError(f"{entry['name']}: method {entry['method']}")
        assert len(data) == entry["uncomp"], (entry["name"], len(data), entry["uncomp"])
        return data


def main():
    mode, group = sys.argv[1], sys.argv[2]
    sz = SplitZip(group)
    print("inner volumes: " + ", ".join(f"{v.wrapper}={v.inner_size}" for v in sz.vols))
    if mode == "list":
        entries = sz.parse_cd()
        files = [e for e in entries if not e["name"].endswith("/")]
        print(f"{len(files)} files (stream {sz.stream_size} bytes)")
        for e in sorted(files, key=lambda e: e["lho"]):
            print(f"  lho={e['lho']:12d} comp={e['comp']:12d} uncomp={e['uncomp']:12d} {e['name']}")
    elif mode == "fetch":
        want = set(sys.argv[3:])
        entries = {e["name"]: e for e in sz.parse_cd()}
        total = 0
        for name in sys.argv[3:]:
            e = entries[name]
            if total + e["uncomp"] > TOTAL_CAP:
                print(f"skip (cap): {name}")
                continue
            data = sz.extract(e)
            fname = e["name"].rstrip("/").split("/")[-1]
            case_id = fname.split(".")[0]
            out = os.path.join(DEST, case_id, fname)
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with open(out, "wb") as f:
                f.write(data)
            total += len(data)
            print(f"extracted {name} -> {out} ({len(data)} bytes)")
        print(f"done, {total} payload bytes, {BYTES_FETCHED} fetched bytes")
    else:
        raise SystemExit(f"unknown mode {mode}")


if __name__ == "__main__":
    main()
