#!/usr/bin/env python3
"""Batch PROBE-then-fetch acquisition of TotalSegmentator CT dataset v2 cardiac masks.

Scales the Phase-1 single-case fetcher (tools/ts_fetch.py) to a validated batch for
geometry-pipeline batch validation. For case ids s0000..s1027 (window extended per
the scale-up mandate; skipping the locally acquired s0004/s0015) only
`sXXXX/segmentations/heart.nii.gz` is fetched first
(~0.3 MB probe); a case QUALIFIES when the heart mask has >= 50,000 nonzero voxels
(real heart coverage; head/abdomen scans have empty/tiny heart masks). Qualifying
cases get the other 5 cardiac-mappable masks of the TotalSegmentator 'total' schema
fetched into the same sXXXX/segmentations/ layout as the existing cases:
  aorta, pulmonary_vein, superior_vena_cava, inferior_vena_cava, atrial_appendage_left
ct.nii.gz is fetched for the FIRST 10 qualifying cases only (future
heartchambers_highres input once its license lands; TotalSegmentator is NOT run here).
Fetching stops when --target qualifying cases are complete or the probe window is
exhausted. Entries are range-extracted 6-8 wide via remotezip (one RemoteZip per
worker thread). Every fetched mask is validated with nibabel (nonzero_voxels,
bbox_mm, ...) and data/manifest.json gains one artifact row per fetched file plus a
self-describing `batch_acquisition` summary. Non-qualifying probe files are deleted
and recorded (skipped_probes); persistent entry failures are recorded
(failed_entries) and their partial cases removed and recorded (dropped_cases) —
nothing is ever silently dropped.

Resumable: progress checkpoints to data/raw/totalseg_ct/.ts_batch_state.json after
every probe/fetch; re-running continues where the previous invocation stopped.

Usage:
  .venv/bin/python tools/ts_fetch_batch.py [--target 200] [--window 400]
      [--min-heart-voxels 50000] [--workers 8] [--ct-cases 10] [--no-manifest]
"""
import argparse
import json
import os
import re
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed

import nibabel as nib
import numpy as np
import requests
from remotezip import RemoteZip

import build_manifest as bm

TOOL = "tools/ts_fetch_batch.py"
TOOL_VERSION = "1.0.0"
URL = bm.ZENODO
DEST = "data/raw/totalseg_ct"
STATE_PATH = os.path.join(DEST, ".ts_batch_state.json")
MANIFEST_PATH = "data/manifest.json"

PREEXISTING = ("s0004", "s0015")  # Phase-1 cases, already local, out of the batch run
WINDOW_LO, WINDOW_HI = "s0000", "s0399"
MASK_STEMS = ("heart", "aorta", "pulmonary_vein", "superior_vena_cava",
              "inferior_vena_cava", "atrial_appendage_left")
PROBE_STEM = "heart"


def mask_member(case, stem):
    return f"{case}/segmentations/{stem}.nii.gz"


def ct_member(case):
    return f"{case}/ct.nii.gz"


def local_path(member):
    return os.path.join(DEST, *member.split("/"))


# --------------------------------------------------------------------------- state

class State:
    """Incremental, self-describing checkpoint (JSON, atomic rewrite under a lock)."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        self.d = {
            "tool": TOOL,
            "tool_version": TOOL_VERSION,
            "command": None,
            "source_url": URL,
            "license": "CC BY 4.0",
            "invocations": 0,
            "probes": {},        # case -> {heart_nonzero_voxels, qualified, ...}
            "fetched": {},       # zip member -> bytes kept on disk
            "failed": {},        # zip member -> {kind, case_id, error, attempts, ...}
            "dropped_cases": {},  # case -> {reason, removed_members}
        }
        if os.path.exists(path):
            try:
                with open(path) as f:
                    prev = json.load(f)
                for k in ("probes", "fetched", "failed", "dropped_cases", "invocations"):
                    if isinstance(prev.get(k), type(self.d[k])):
                        self.d[k] = prev[k]
            except (OSError, ValueError):
                pass

    def _save_locked(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.d, f, indent=1, sort_keys=True)
        os.replace(tmp, self.path)

    def bump_invocation(self, command):
        with self.lock:
            self.d["command"] = command
            self.d["invocations"] = int(self.d.get("invocations", 0)) + 1
            self._save_locked()

    def record_probe(self, case, rec):
        with self.lock:
            self.d["probes"][case] = rec
            self._save_locked()

    def record_fetch(self, member, nbytes):
        with self.lock:
            self.d["fetched"][member] = int(nbytes)
            self.d["failed"].pop(member, None)
            self._save_locked()

    def record_fail(self, member, info):
        with self.lock:
            self.d["failed"][member] = info
            self._save_locked()

    def clear_member(self, member):
        with self.lock:
            self.d["fetched"].pop(member, None)
            self.d["failed"].pop(member, None)

    def drop_case(self, case, reason, removed_members, member_errors):
        with self.lock:
            for m in removed_members:
                self.d["fetched"].pop(m, None)
            self.d["dropped_cases"][case] = {
                "reason": reason,
                "removed_members": removed_members,
                "member_errors": member_errors,
            }
            self._save_locked()


# ------------------------------------------------------------------- remote access

_tl = threading.local()
_print_lock = threading.Lock()


def _get_zip():
    """Thread-local RemoteZip: one central-directory read per worker thread, then
    one cheap range request per entry. remotezip's RemoteIO is single-stream state,
    so one instance per thread is the concurrency unit."""
    z = getattr(_tl, "z", None)
    if z is None:
        sess = requests.Session()
        z = RemoteZip(URL, session=sess, timeout=120,
                      headers={"User-Agent": "flowscope-ts-fetch/1.0"})
        _tl.z = z
    return z


def _drop_zip():
    z = getattr(_tl, "z", None)
    if z is not None:
        try:
            z.close()
        except Exception:
            pass
        _tl.z = None


def fetch_entry(member, out_path, attempts=4):
    """Range-extract one zip member to out_path (atomic); returns bytes written."""
    last = None
    for i in range(attempts):
        try:
            data = _get_zip().read(member)
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            tmp = out_path + ".part"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, out_path)
            return len(data)
        except Exception as e:  # noqa: BLE001 - record + retry any transport error
            last = e
            _drop_zip()
            time.sleep(min(20.0, 2.0 * 2 ** i))
    raise RuntimeError(f"{member}: fetch failed after {attempts} attempts: {last}")


def mask_nonzero(path):
    img = nib.load(path)
    return int(np.count_nonzero(np.asanyarray(img.dataobj)))


def prune_empty(*dirs):
    for d in dirs:
        try:
            os.rmdir(d)
        except OSError:
            pass


def cleanup_parts():
    for dirpath, _, filenames in os.walk(DEST):
        for fname in filenames:
            if fname.endswith(".part"):
                try:
                    os.remove(os.path.join(dirpath, fname))
                except OSError:
                    pass


# ------------------------------------------------------------------------ probing

def probe_case(state, case, min_nz):
    """Fetch (or reuse) the heart mask, count nonzero voxels, qualify >= min_nz.
    Non-qualifying probe files are deleted (recorded in state as skipped)."""
    member = mask_member(case, PROBE_STEM)
    path = local_path(member)
    cached = state.d["probes"].get(case)
    if cached and cached.get("status") == "ok":
        return cached
    if not (os.path.exists(path) and os.path.getsize(path) > 0):
        state.record_fetch(member, fetch_entry(member, path))
    try:
        nz = mask_nonzero(path)
    except Exception:  # corrupt probe file: refetch once
        os.remove(path)
        state.clear_member(member)
        state.record_fetch(member, fetch_entry(member, path))
        try:
            nz = mask_nonzero(path)
        except Exception as e2:  # noqa: BLE001
            state.record_fail(member, {"kind": "probe", "case_id": case, "error":
                                       f"nibabel load failed: {e2}", "attempts": 2})
            rec = {"case_id": case, "status": "error", "error": f"nibabel load failed: {e2}"}
            state.record_probe(case, rec)
            return rec
    qualified = nz >= min_nz
    if qualified:
        rec = {"case_id": case, "status": "ok", "heart_nonzero_voxels": nz, "qualified": True}
    else:
        nbytes = os.path.getsize(path)
        os.remove(path)
        prune_empty(os.path.dirname(path), os.path.dirname(os.path.dirname(path)))
        state.clear_member(member)
        rec = {"case_id": case, "status": "ok", "heart_nonzero_voxels": nz,
               "qualified": False, "probe_bytes": nbytes}
        with _print_lock:
            print(f"  probe {case}: heart nz={nz} < {min_nz} -> skipped", flush=True)
    state.record_probe(case, rec)
    return rec


def probe_wave(pool, state, cases, min_nz):
    out = {}
    futs = {pool.submit(probe_case, state, c, min_nz): c for c in cases}
    for f in as_completed(futs):
        c = futs[f]
        try:
            out[c] = f.result()
        except Exception as e:  # noqa: BLE001
            out[c] = {"case_id": c, "status": "error", "error": str(e)}
    return [out[c] for c in cases]


# ------------------------------------------------------------------------- fetch

def missing_stems(case):
    return [s for s in MASK_STEMS
            if not (os.path.exists(local_path(mask_member(case, s)))
                    and os.path.getsize(local_path(mask_member(case, s))) > 0)]


def case_complete(case):
    return not missing_stems(case)


def fetch_members(pool, state, members, label):
    """Fetch members 8-wide; returns {member: error} for persistent failures."""
    failures = {}
    if not members:
        return failures
    done = [0]
    nbytes_run = [0]
    futs = {pool.submit(fetch_entry, m, local_path(m)): m for m in members}
    for f in as_completed(futs):
        m = futs[f]
        try:
            nb = f.result()
            state.record_fetch(m, nb)
            with _print_lock:
                done[0] += 1
                nbytes_run[0] += nb
                if done[0] % 25 == 0 or done[0] == len(members):
                    print(f"  {label}: {done[0]}/{len(members)} entries, "
                          f"{nbytes_run[0] / 1e6:.1f} MB", flush=True)
        except Exception as e:  # noqa: BLE001
            failures[m] = str(e)
    if failures:  # one repair round on fresh connections
        retry = list(failures)
        failures = {}
        futs = {pool.submit(fetch_entry, m, local_path(m)): m for m in retry}
        for f in as_completed(futs):
            m = futs[f]
            try:
                nb = f.result()
                state.record_fetch(m, nb)
            except Exception as e:  # noqa: BLE001
                failures[m] = str(e)
    return failures


def drop_case_files(state, case, member_errors):
    removed = []
    for stem in MASK_STEMS:
        m = mask_member(case, stem)
        p = local_path(m)
        if os.path.exists(p):
            os.remove(p)
            removed.append(m)
    pm = ct_member(case)
    if os.path.exists(local_path(pm)):
        os.remove(local_path(pm))
        removed.append(pm)
    prune_empty(os.path.join(DEST, case, "segmentations"), os.path.join(DEST, case))
    state.drop_case(case, "persistent member fetch failure; partial files removed "
                          "to keep the batch invariant (6 cardiac masks per case)",
                    removed, member_errors)
    return removed


# ------------------------------------------------------------- rows and manifest

def make_row(rel, fpath):
    """Artifact row for one totalseg_ct file, identical in shape to the rows
    produced by tools/build_manifest.py collect_rows() for this root."""
    fname = os.path.basename(rel)
    case_id = rel.split("/")[3]
    stem = fname[:-7] if fname.endswith(".nii.gz") else os.path.splitext(fname)[0]
    is_image = stem in ("ct", "img") or stem.endswith("_0000") or stem.endswith(".img")
    load_error = None
    try:
        validated, nz = bm.validate_nii(fpath, is_image)
    except Exception as e:  # noqa: BLE001 - keep honest row + failure record
        validated, nz = {"loaded": False, "error": str(e)}, None
        load_error = str(e)
    sid, is_can = bm.resolve_name(stem)
    row = {
        "path": rel,
        "dataset": "totalseg_ct",
        "case_id": case_id,
        "structures": [] if is_image else [sid],
        "source_url": bm.ZENODO,
        "license": bm.LICENSES["totalseg_ct"],
        "bytes": os.path.getsize(fpath),
        "validated": validated,
    }
    if not is_image:
        row["raw_name"] = stem
        row["canonical"] = is_can
        row["empty"] = (nz == 0)
    return row, load_error, nz


def collect_new_files(existing_paths):
    files = []
    for dirpath, _, filenames in os.walk(DEST):
        for fname in sorted(filenames):
            if not fname.endswith(".nii.gz"):
                continue
            fpath = os.path.join(dirpath, fname)
            rel = fpath.replace(os.sep, "/")
            if rel in existing_paths:
                continue
            files.append((rel, fpath))
    files.sort()
    return files


def update_manifest(pool, summary, do_write):
    with open(MANIFEST_PATH) as f:
        manifest = json.load(f)
    existing = {r["path"] for r in manifest["artifacts"]}
    files = collect_new_files(existing)
    rows = []
    load_failures = []
    empty_masks = []
    futs = {pool.submit(make_row, rel, fpath): rel for rel, fpath in files}
    for fu in as_completed(futs):
        row, load_error, nz = fu.result()
        rows.append(row)
        if load_error:
            load_failures.append({"member": row["path"], "error": f"nibabel load failed: {load_error}"})
        if nz == 0 and row["structures"]:
            empty_masks.append(row["path"])
    rows.sort(key=lambda r: r["path"])
    summary["manifest_rows_added"] = len(rows)
    summary["validation_load_failures"] = load_failures
    summary["empty_masks"] = sorted(empty_masks)
    if do_write:
        manifest["artifacts"].extend(rows)
        manifest["artifacts"].sort(key=lambda r: r["path"])
        manifest["batch_acquisition"] = summary
        tmp = MANIFEST_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(manifest, f, indent=2)
        os.replace(tmp, MANIFEST_PATH)
    return rows


# --------------------------------------------------------------------------- main

def archive_inventory():
    with RemoteZip(URL, timeout=120) as z:
        names = set(z.namelist())
    return names


def batch_case_dirs():
    out = []
    if os.path.isdir(DEST):
        for name in sorted(os.listdir(DEST)):
            if (os.path.isdir(os.path.join(DEST, name))
                    and re.fullmatch(r"s\d{4}", name) and name not in PREEXISTING):
                out.append(name)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--target", type=int, default=200,
                    help="stop when this many NEW qualifying cases are complete")
    ap.add_argument("--window", type=int, default=1028,
                    help="probe case ids s0000..s%(default)s-1 minus the local cases")
    ap.add_argument("--min-heart-voxels", type=int, default=50000)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--ct-cases", type=int, default=10)
    ap.add_argument("--no-manifest", action="store_true",
                    help="skip the data/manifest.json update (checkpoint only)")
    args = ap.parse_args()
    window_hi = f"s{args.window - 1:04d}"

    t0 = time.time()
    command = ".venv/bin/python " + " ".join(sys.argv)
    state = State(STATE_PATH)
    state.bump_invocation(command)
    cleanup_parts()

    t_probe = time.time()
    print(f"inventory: fetching zip central directory from {URL}", flush=True)
    names = archive_inventory()
    all_cases = sorted({n.split("/")[0] for n in names
                        if len(n) > 5 and n[0] == "s" and n[1:5].isdigit() and "/" in n})
    all_set = set(all_cases)
    window_cases = [c for c in all_cases if WINDOW_LO <= c <= window_hi]
    ids_absent = [f"s{i:04d}" for i in range(args.window)
                  if f"s{i:04d}" not in all_set]
    archive_missing = []
    candidates = []
    for c in window_cases:
        if c in PREEXISTING:
            continue
        miss = [mask_member(c, s) for s in MASK_STEMS if mask_member(c, s) not in names]
        if miss:
            for m in miss:
                archive_missing.append({"kind": "archive", "case_id": c, "member": m,
                                        "error": "absent from archive central directory"})
            state.record_fail(miss[0], {"kind": "archive", "case_id": c,
                                        "error": f"{len(miss)} cardiac mask member(s) absent from archive",
                                        "attempts": 0})
            continue
        candidates.append(c)
    print(f"inventory: {len(all_cases)} case dirs in archive, {len(window_cases)} in window "
          f"{WINDOW_LO}..{window_hi}, {len(candidates)} probe candidates "
          f"({len(ids_absent)} window ids absent from archive)", flush=True)

    pool = ThreadPoolExecutor(args.workers)
    complete = set()
    qualified_order = []
    skipped_probes = []
    failed_probes = []
    dropped = {}

    try:
        # Resume: cases already on disk (prior invocation) are complete material;
        # their probes are counted from disk so records stay complete.
        for c in batch_case_dirs():
            r = probe_case(state, c, args.min_heart_voxels)
            if r.get("status") == "ok" and r.get("qualified"):
                if c not in qualified_order:
                    qualified_order.append(c)
                if case_complete(c):
                    complete.add(c)
            elif r.get("status") == "ok":
                skipped_probes.append(c)

        # PROBE-then-fetch main loop
        probe_cursor = 0
        reservoir = deque()
        pending = deque()
        while len(complete) < args.target:
            while len(complete) + len(pending) < args.target:
                if reservoir:
                    pending.append(reservoir.popleft())
                elif probe_cursor < len(candidates):
                    wave = candidates[probe_cursor:probe_cursor + args.workers]
                    probe_cursor += len(wave)
                    for r in probe_wave(pool, state, wave, args.min_heart_voxels):
                        c = r["case_id"]
                        if r.get("status") == "error":
                            failed_probes.append({"kind": "probe", "case_id": c,
                                                  "error": r.get("error", "unknown")})
                        elif r.get("qualified"):
                            if c not in qualified_order:
                                qualified_order.append(c)
                            reservoir.append(c)
                        else:
                            skipped_probes.append(c)
                    nq = sum(1 for c in wave
                             if state.d["probes"].get(c, {}).get("qualified"))
                    print(f"probe: {probe_cursor}/{len(candidates)} candidates probed, "
                          f"{len(qualified_order)} qualified, {len(complete)} complete",
                          flush=True)
                else:
                    break
            if not pending:
                break
            cases = list(pending)
            pending.clear()
            members = [mask_member(c, s) for c in cases for s in missing_stems(c)]
            failures = fetch_members(pool, state, members, "fetch masks")
            for c in cases:
                if case_complete(c):
                    complete.add(c)
                    continue
                errs = {m: failures[m] for m in failures
                        if m.startswith(c + "/")}
                missing = [mask_member(c, s) for s in missing_stems(c)]
                for m in missing:
                    info = {"kind": "member", "case_id": c, "error": errs.get(m, "unknown"),
                            "attempts": 8, "action": "case_files_removed"}
                    state.record_fail(m, info)
                removed = drop_case_files(state, c, errs)
                dropped[c] = {"missing_members": missing, "errors": errs,
                              "removed_members": removed}
                print(f"  case {c} dropped after persistent failures: {missing}", flush=True)

        # ct.nii.gz for the FIRST --ct-cases qualifying cases only (never a
        # TotalSegmentator run: raw input kept for future heartchambers_highres).
        ct_targets = [c for c in qualified_order[:args.ct_cases] if c in complete]
        ct_members = [ct_member(c) for c in ct_targets
                      if not (os.path.exists(local_path(ct_member(c)))
                              and os.path.getsize(local_path(ct_member(c))) > 0)]
        ct_failures = fetch_members(pool, state, ct_members, "fetch ct")
        for m, err in ct_failures.items():
            state.record_fail(m, {"kind": "ct", "case_id": m.split("/")[0], "error": err,
                                  "attempts": 8,
                                  "action": "kept 6 masks, no ct.nii.gz"})
        probe_wall = time.time() - t_probe

        # validated rows for every fetched file + manifest merge
        t_val = time.time()
        summary = {
            "tool": TOOL,
            "tool_version": TOOL_VERSION,
            "command": command,
            "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "source_url": URL,
            "license": "CC BY 4.0",
            "strategy": ("PROBE-then-fetch: fetch only sXXXX/segmentations/heart.nii.gz per "
                         "candidate (s0000..%s, minus local %s); qualify at >= %d heart-mask "
                         "nonzero voxels; qualifying cases get the other 5 cardiac-mappable "
                         "masks (aorta, pulmonary_vein, superior_vena_cava, "
                         "inferior_vena_cava, atral_appendage_left); ct.nii.gz only for the "
                         "first %d qualifying cases (future heartchambers_highres input; "
                         "TotalSegmentator NOT run)"
                         % (window_hi, "/".join(PREEXISTING), args.min_heart_voxels,
                            args.ct_cases)),
            "probe_window": [WINDOW_LO, window_hi],
            "window_ids_absent_from_archive": ids_absent,
            "cases_missing_archive_members": archive_missing,
            "min_heart_voxels": args.min_heart_voxels,
            "workers": args.workers,
            "preexisting_cases": list(PREEXISTING),
            "ct_selection_rule": "first %d qualifying (probe order = ascending case id) "
                                 "cases that completed" % args.ct_cases,
            "invocations": state.d["invocations"],
        }
        rows = update_manifest(pool, summary, not args.no_manifest)

        probed = state.d["probes"]
        probe_bytes_discarded = sum(int(r.get("probe_bytes", 0)) for r in probed.values())
        qualified_all = [c for c in qualified_order
                         if probed.get(c, {}).get("qualified")]
        for c in probed:  # resumed inserts beyond qualified_order keep manifest order
            if probed[c].get("qualified") and c not in qualified_all:
                qualified_all.append(c)
        failed_entries = list(archive_missing) + list(failed_probes)
        for m, info in sorted(state.d["failed"].items()):
            failed_entries.append({"member": m, **info})
        summary.update({
            "probed": len(probed),
            "qualified": len(qualified_all),
            "fetched_cases": len(complete),
            "fetched_files": len(state.d["fetched"]),
            "bytes_total": int(sum(state.d["fetched"].values())),
            "wall_time_s": round(time.time() - t0, 1),
            "failed_entries": failed_entries,
            "batch_cases_total": len(complete) + len(PREEXISTING),
            "completed_case_ids": sorted(complete),
            "ct_case_ids": ct_targets,
            "skipped_probes": [{"case_id": c, **{k: v for k, v in probed[c].items()
                                                 if k != "status"}}
                               for c in sorted(set(skipped_probes))],
            "qualified_surplus_or_unfetched": sorted(set(qualified_all) - complete),
            "dropped_cases": dropped,
            "probe_files_discarded_bytes": probe_bytes_discarded,
            "phases": {"probe_and_fetch_s": round(probe_wall, 1),
                       "validate_and_manifest_s": round(time.time() - t_val, 1)},
        })
        if not args.no_manifest:
            with open(MANIFEST_PATH) as f:
                manifest = json.load(f)
            manifest["batch_acquisition"] = summary
            tmp = MANIFEST_PATH + ".tmp"
            with open(tmp, "w") as f:
                json.dump(manifest, f, indent=2)
            os.replace(tmp, MANIFEST_PATH)
    finally:
        pool.shutdown()

    print(json.dumps({k: summary[k] for k in
                      ("probed", "qualified", "fetched_cases", "fetched_files",
                       "bytes_total", "wall_time_s", "batch_cases_total")}, indent=2))
    print(f"batch acquisition complete: {summary['fetched_cases']} new cases "
          f"(+{len(PREEXISTING)} preexisting = {summary['batch_cases_total']} batch "
          f"cases), {len(summary['skipped_probes'])} skipped probes, "
          f"{len(summary['failed_entries'])} failed entries", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
