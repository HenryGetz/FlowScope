# Taubin drift cap — before/after volume drift (re-run batch `rerun-drift`)

*Generated 2026-09-23 from JSON report evidence only; every number below is copied or computed from the listed files — nothing estimated.*

## 3a. What was rebuilt and why

The Taubin smoothing step was changed to enforce a per-structure volume-drift cap (`DRIFT_CAP_PCT = 1.0`, absolute %) with an iteration ladder **25 -> 12 -> 6 -> 3 -> 1** (rung 0 = keep the raw Flying Edges mesh unsmoothed), plus per-structure reporting (`smooth_iters_requested`, `smooth_iters_used`, `volume_drift_uncapped_pct`, `smoothing_note`).

**Affected set = 15 before case-build rows** (below): every per-structure row in the before reports whose pre-fix absolute Taubin drift (`volume_drift_pct`, raw -> smoothed volume) was **> 1.0%** — exactly the rows the drift cap can change. Every other row already passed its 25-iter smoothing inside the 1.0% cap and therefore follows the identical code path before and after; the pipeline is deterministic (verified: the duplicated case-builds in batch-050/100/200 carry identical per-structure drift, triangle counts and raw volumes), so all other rows are unchanged by construction — section 3e confirms measured equality for all 26 measurable unaffected rows of these 8 cases.

The 15 rows cover **8 unique scans** (all TotalSegmentator `ts-dir` cases). Identical inputs + flags give identical outputs, so each scan was rebuilt **once**; the single after build serves every duplicate before row:

| scan | before case-builds sharing the one after build |
|---|---|
| s0011 | batch-050, batch-100, batch-200 |
| s0109 | batch-050, batch-100, batch-200 |
| s0120 | batch-100, batch-200 |
| s0166 | batch-100, batch-200 |
| s0189 | batch-100, batch-200 |
| s0236 | batch-200 |
| s0382 | batch-200 |
| s0386 | batch-200 |

Re-run (from the repo root; `tools/batch_build.py` used as-is):

    /home/wavy/ai/flowscope/.venv/bin/python tools/batch_build.py \
        --case-list out/rerun/cases_affected.txt --batch-id rerun-drift --out-dir out/rerun --jobs 6

Evidence: before = `out/batches/{batch-050,batch-100,batch-200}/<case>.json` + each batch's `report.json` (`cases` rows); after = `out/rerun/rerun-drift/<case>.json` + `out/rerun/rerun-drift/report.json` (`cases` rows). `out/batches/` was read only.

Conventions: `volume_drift_pct` and `volume_drift_uncapped_pct` are positive magnitudes of Taubin shrinkage, computed raw -> smoothed (`(raw - smoothed)/raw`); the `smoothing_note` strings quote the same drift signed (negative = shrinkage). "<= 1.0" below means absolute value. Drift spans smoothing only (raw -> smoothed); `volume_final_mm3` additionally reflects decimation and is out of scope here.

## 3b. Affected structures — 15 before case-build rows

One row per affected before case-build; `before` columns from that build's before report, `after` columns from the shared after build of the same scan (duplicate builds therefore repeat identical after values).

| batch | case | structure | drift before % | drift after % | after iters used | after smoothing_note | after volume_raw mm3 | after volume_smoothed mm3 | final_tris before | final_tris after |
|---|---|---|---|---|---|---|---|---|---|---|
| batch-050 | s0011 | heart_atrial_appendage_left | 1.0094 | 0.1061 | 12 | `drift cap 1%: 25 iters drift -1.01% -> used 12 iters (-0.11%)` | 4417.453 | 4412.766 | 2264 | 2264 |
| batch-050 | s0109 | pulmonary_veins | 100.0 | 0.0 | 0 | `drift cap 1%: kept unsmoothed (25 iters drift -100.00%)` | 3.937 | 3.937 | 24 | 24 |
| batch-100 | s0011 | heart_atrial_appendage_left | 1.0094 | 0.1061 | 12 | `drift cap 1%: 25 iters drift -1.01% -> used 12 iters (-0.11%)` | 4417.453 | 4412.766 | 2264 | 2264 |
| batch-100 | s0109 | pulmonary_veins | 100.0 | 0.0 | 0 | `drift cap 1%: kept unsmoothed (25 iters drift -100.00%)` | 3.937 | 3.937 | 24 | 24 |
| batch-100 | s0120 | pulmonary_veins | 94.3085 | 0.0 | 0 | `drift cap 1%: kept unsmoothed (25 iters drift -94.31%)` | 15.891 | 15.891 | 48 | 48 |
| batch-100 | s0166 | vena_cava_superior | 3.165 | 0.17 | 6 | `drift cap 1%: 25 iters drift -3.16% -> used 6 iters (-0.17%)` | 331.594 | 331.03 | 348 | 348 |
| batch-100 | s0189 | heart_atrial_appendage_left | 1.0733 | 0.2696 | 12 | `drift cap 1%: 25 iters drift -1.07% -> used 12 iters (-0.27%)` | 3632.625 | 3622.83 | 1746 | 1746 |
| batch-200 | s0011 | heart_atrial_appendage_left | 1.0094 | 0.1061 | 12 | `drift cap 1%: 25 iters drift -1.01% -> used 12 iters (-0.11%)` | 4417.453 | 4412.766 | 2264 | 2264 |
| batch-200 | s0109 | pulmonary_veins | 100.0 | 0.0 | 0 | `drift cap 1%: kept unsmoothed (25 iters drift -100.00%)` | 3.937 | 3.937 | 24 | 24 |
| batch-200 | s0120 | pulmonary_veins | 94.3085 | 0.0 | 0 | `drift cap 1%: kept unsmoothed (25 iters drift -94.31%)` | 15.891 | 15.891 | 48 | 48 |
| batch-200 | s0166 | vena_cava_superior | 3.165 | 0.17 | 6 | `drift cap 1%: 25 iters drift -3.16% -> used 6 iters (-0.17%)` | 331.594 | 331.03 | 348 | 348 |
| batch-200 | s0189 | heart_atrial_appendage_left | 1.0733 | 0.2696 | 12 | `drift cap 1%: 25 iters drift -1.07% -> used 12 iters (-0.27%)` | 3632.625 | 3622.83 | 1746 | 1746 |
| batch-200 | s0236 | pulmonary_veins | 3.2916 | 0.2497 | 6 | `drift cap 1%: 25 iters drift -3.29% -> used 6 iters (-0.25%)` | 289.406 | 288.684 | 324 | 324 |
| batch-200 | s0382 | heart_atrial_appendage_left | 1.0199 | 0.368 | 12 | `drift cap 1%: 25 iters drift -1.02% -> used 12 iters (-0.37%)` | 2425.922 | 2416.995 | 1780 | 1780 |
| batch-200 | s0386 | heart_atrial_appendage_left | 1.6599 | 0.5698 | 12 | `drift cap 1%: 25 iters drift -1.66% -> used 12 iters (-0.57%)` | 1909.547 | 1898.666 | 1192 | 1192 |

Checks behind the table (verified against the JSONs): `volume_raw_mm3` is identical before and after in all 15 rows — the raw Flying Edges volume is preserved, geometry is never discarded — and `final_triangles` is identical before/after in all 15 rows. `volume_drift_uncapped_pct` in the after report equals the before drift exactly in all 15 rows (the ladder reproduces the same 25-iter outcome before taking a lower rung).

## 3c. Per-case-build max drift and after-build integrity — 15 rows

`case max drift` = `volume_drift_pct_max` of the batch `report.json` row (max absolute measurable drift in the case). After-build columns repeat for duplicate builds sharing one after build. `input_triangles` matches before == after everywhere (ingest unchanged).

| batch | case | case max drift before % | case max drift after % | input_tris before | input_tris after | integrity_ok | within_window (before -> after) | anomaly |
|---|---|---|---|---|---|---|---|---|
| batch-050 | s0011 | 1.0094 | 0.5409 | 145592 | 145592 | true | true -> true | n/a |
| batch-050 | s0109 | 100.0 | 0.5257 | 93280 | 93280 | true | false -> false | n/a |
| batch-100 | s0011 | 1.0094 | 0.5409 | 145592 | 145592 | true | true -> true | n/a |
| batch-100 | s0109 | 100.0 | 0.5257 | 93280 | 93280 | true | false -> false | n/a |
| batch-100 | s0120 | 94.3085 | 0.5078 | 78932 | 78932 | true | false -> false | n/a |
| batch-100 | s0166 | 3.165 | 0.5386 | 97756 | 97756 | true | false -> false | n/a |
| batch-100 | s0189 | 1.0733 | 0.5466 | 157852 | 157852 | true | true -> true | n/a |
| batch-200 | s0011 | 1.0094 | 0.5409 | 145592 | 145592 | true | true -> true | n/a |
| batch-200 | s0109 | 100.0 | 0.5257 | 93280 | 93280 | true | false -> false | n/a |
| batch-200 | s0120 | 94.3085 | 0.5078 | 78932 | 78932 | true | false -> false | n/a |
| batch-200 | s0166 | 3.165 | 0.5386 | 97756 | 97756 | true | false -> false | n/a |
| batch-200 | s0189 | 1.0733 | 0.5466 | 157852 | 157852 | true | true -> true | n/a |
| batch-200 | s0236 | 3.2916 | 0.296 | 98108 | 98108 | true | false -> false | n/a |
| batch-200 | s0382 | 1.0199 | 0.368 | 138728 | 138728 | true | true -> true | n/a |
| batch-200 | s0386 | 1.6599 | 0.5698 | 85170 | 85170 | true | false -> false | n/a |

## 3d. Whole-case after audit (all rows of the 8 rerun reports)

Measurable (watertight) rows: **34**. Every one has `volume_drift_pct` <= 1.0 (max **0.5698**, `s0386` `heart_atrial_appendage_left`). **Violations: none** — no row needs flagging.

Unmeasurable rows are explicitly flagged: **8** open (non-watertight, FOV-truncated) rows have null volume fields and `smoothing_note` `drift unmeasured: open surface (FOV-truncated); smoothing uncapped` (they ran the uncapped 25 iters); **6** rows are skipped (`empty/1-voxel mask (0 nonzero voxels)`) with no geometry in either build.

Open rows (drift unmeasurable):

| case | structure | final_tris | volume_note | smoothing_note |
|---|---|---|---|---|
| s0236 | heart | 49472 | open surface (FOV-truncated); divergence-theorem volume not meaningful | `drift unmeasured: open surface (FOV-truncated); smoothing uncapped` |
| s0382 | aorta | 43463 | open surface (FOV-truncated); divergence-theorem volume not meaningful | `drift unmeasured: open surface (FOV-truncated); smoothing uncapped` |
| s0382 | heart | 53915 | open surface (FOV-truncated); divergence-theorem volume not meaningful | `drift unmeasured: open surface (FOV-truncated); smoothing uncapped` |
| s0382 | vena_cava_inferior | 1415 | open surface (FOV-truncated); divergence-theorem volume not meaningful | `drift unmeasured: open surface (FOV-truncated); smoothing uncapped` |
| s0386 | aorta | 29276 | open surface (FOV-truncated); divergence-theorem volume not meaningful | `drift unmeasured: open surface (FOV-truncated); smoothing uncapped` |
| s0386 | heart | 34468 | open surface (FOV-truncated); divergence-theorem volume not meaningful | `drift unmeasured: open surface (FOV-truncated); smoothing uncapped` |
| s0386 | vena_cava_inferior | 8430 | open surface (FOV-truncated); divergence-theorem volume not meaningful | `drift unmeasured: open surface (FOV-truncated); smoothing uncapped` |
| s0386 | vena_cava_superior | 4932 | open surface (FOV-truncated); divergence-theorem volume not meaningful | `drift unmeasured: open surface (FOV-truncated); smoothing uncapped` |

Every row with `smooth_iters_used` < 25 (the ladder engaged), with its note:

| case | structure | iters requested | iters used | rung | drift after % | drift uncapped % | final_tris | smoothing_note |
|---|---|---|---|---|---|---|---|---|
| s0011 | heart_atrial_appendage_left | 25 | 12 | reduced iters | 0.1061 | 1.0094 | 2264 | `drift cap 1%: 25 iters drift -1.01% -> used 12 iters (-0.11%)` |
| s0109 | pulmonary_veins | 25 | 0 | rung 0 (kept unsmoothed) | 0.0 | 100.0 | 24 | `drift cap 1%: kept unsmoothed (25 iters drift -100.00%)` |
| s0120 | pulmonary_veins | 25 | 0 | rung 0 (kept unsmoothed) | 0.0 | 94.3085 | 48 | `drift cap 1%: kept unsmoothed (25 iters drift -94.31%)` |
| s0166 | vena_cava_superior | 25 | 6 | reduced iters | 0.17 | 3.165 | 348 | `drift cap 1%: 25 iters drift -3.16% -> used 6 iters (-0.17%)` |
| s0189 | heart_atrial_appendage_left | 25 | 12 | reduced iters | 0.2696 | 1.0733 | 1746 | `drift cap 1%: 25 iters drift -1.07% -> used 12 iters (-0.27%)` |
| s0236 | pulmonary_veins | 25 | 6 | reduced iters | 0.2497 | 3.2916 | 324 | `drift cap 1%: 25 iters drift -3.29% -> used 6 iters (-0.25%)` |
| s0382 | heart_atrial_appendage_left | 25 | 12 | reduced iters | 0.368 | 1.0199 | 1780 | `drift cap 1%: 25 iters drift -1.02% -> used 12 iters (-0.37%)` |
| s0386 | heart_atrial_appendage_left | 25 | 12 | reduced iters | 0.5698 | 1.6599 | 1192 | `drift cap 1%: 25 iters drift -1.66% -> used 12 iters (-0.57%)` |

Rung-0 rows (`smooth_iters_used == 0`, kept as the raw Flying Edges mesh): **2** — the degenerate `pulmonary_veins` slivers `s0109` (24 tris, note `drift cap 1%: kept unsmoothed (25 iters drift -100.00%)`) and `s0120` (48 tris, note `drift cap 1%: kept unsmoothed (25 iters drift -94.31%)`), both with `final_triangles > 0`.

Geometry parity before -> after (verified row by row): all 6 structure names of each before report are present in its after report (8/8 cases); all **42** non-skipped rows keep `final_triangles > 0` and identical to before — including the degenerate `pulmonary_veins` slivers (s0109: 24 tris, s0120: 48 tris) and all 8 open rows; the 6 skipped rows are skipped identically in both (they never had geometry). **No geometry discarded.**

## 3e. Unaffected-structure sanity (before drift <= 1.0)

All **26** measurable rows of the 8 rerun cases whose before drift was <= 1.0 keep `smooth_iters_used == 25` (requested 25, `volume_drift_uncapped_pct`/`smoothing_note` null) and their before/after drift match **exactly** (max |delta| = 0.0 over 26 rows — far inside 4-decimal report rounding). The 8 open rows carry no measurable drift before or after and are audited in 3d; the 6 skipped rows had no geometry.

| case | structure | drift before % | drift after % | abs delta | iters used |
|---|---|---|---|---|---|
| s0011 | aorta | 0.2612 | 0.2612 | 0.0 | 25 |
| s0011 | heart | 0.5409 | 0.5409 | 0.0 | 25 |
| s0011 | vena_cava_inferior | 0.1642 | 0.1642 | 0.0 | 25 |
| s0011 | pulmonary_veins | 0.407 | 0.407 | 0.0 | 25 |
| s0011 | vena_cava_superior | 0.1386 | 0.1386 | 0.0 | 25 |
| s0109 | aorta | 0.2816 | 0.2816 | 0.0 | 25 |
| s0109 | heart | 0.5257 | 0.5257 | 0.0 | 25 |
| s0109 | vena_cava_inferior | 0.2595 | 0.2595 | 0.0 | 25 |
| s0120 | aorta | 0.2078 | 0.2078 | 0.0 | 25 |
| s0120 | heart | 0.5078 | 0.5078 | 0.0 | 25 |
| s0120 | vena_cava_inferior | 0.2153 | 0.2153 | 0.0 | 25 |
| s0166 | aorta | 0.145 | 0.145 | 0.0 | 25 |
| s0166 | heart_atrial_appendage_left | 0.1238 | 0.1238 | 0.0 | 25 |
| s0166 | heart | 0.5386 | 0.5386 | 0.0 | 25 |
| s0166 | vena_cava_inferior | 0.172 | 0.172 | 0.0 | 25 |
| s0166 | pulmonary_veins | 0.3167 | 0.3167 | 0.0 | 25 |
| s0189 | aorta | 0.3696 | 0.3696 | 0.0 | 25 |
| s0189 | heart | 0.5466 | 0.5466 | 0.0 | 25 |
| s0189 | vena_cava_inferior | 0.1477 | 0.1477 | 0.0 | 25 |
| s0189 | pulmonary_veins | 0.3282 | 0.3282 | 0.0 | 25 |
| s0189 | vena_cava_superior | 0.0559 | 0.0559 | 0.0 | 25 |
| s0236 | aorta | 0.296 | 0.296 | 0.0 | 25 |
| s0236 | vena_cava_inferior | 0.276 | 0.276 | 0.0 | 25 |
| s0382 | pulmonary_veins | 0.332 | 0.332 | 0.0 | 25 |
| s0382 | vena_cava_superior | 0.1923 | 0.1923 | 0.0 | 25 |
| s0386 | pulmonary_veins | 0.213 | 0.213 | 0.0 | 25 |

## 3f. Summary

- **Max drift across the affected set:** before **100.0%** (pulmonary_veins in batch-050/s0109, 3 case-builds) -> after **0.5698%** (heart_atrial_appendage_left in s0386, 12 iters used). Max case-level drift (`volume_drift_pct_max`) across the 8 rerun reports: **0.5698%** (s0386).
- **Ladder engaged** (`smooth_iters_used` < 25): **8/8 unique affected structures** = all 15/15 affected case-build rows. Of these, **6 unique structures / 10 of the 15 case-build rows** settled on reduced iters — 12 iters (7 rows): s0011 heart_atrial_appendage_left (batch-050/100/200), s0189 heart_atrial_appendage_left (batch-100/200), s0382 and s0386 heart_atrial_appendage_left (batch-200 each); 6 iters (3 rows): s0166 vena_cava_superior (batch-100/200), s0236 pulmonary_veins (batch-200).
- **Kept unsmoothed (rung 0):** **2 unique structures / 5 of the 15 case-build rows** (s0109 pulmonary_veins in batch-050/100/200, s0120 pulmonary_veins in batch-100/200) — each retaining its raw Flying Edges mesh (`volume_smoothed == volume_raw`) with final_triangles 24 / 48 > 0.
- **Integrity verdict: PASS** — 8/8 builds `integrity_ok == true`, `anomaly == null`, all 9 GLB checks true per build, `report.json` + 8 `<case>.json` + 8 `<case>.glb` present; every watertight rerun row within the 1.0% drift cap; all 42 non-skipped rows keep their geometry (`final_triangles` unchanged, > 0); all 26 measurable unaffected rows bit-matched to before.
