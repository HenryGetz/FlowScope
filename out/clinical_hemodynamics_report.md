# FlowScope clinical hemodynamics report

- generated: 2026-09-24T19:27:14.492346+00:00
- command: `tools/run_clinical_batch.py --cases 601 700 798`
- runtime_s: 6912.09
- out_dir: out
- cases: 601 700 798
- synthetic lesion ladder (dia_reduction %): 50 70 90
- decision rule: vFFR <= 0.8 -> Revascularization Indicated (vFFR <= 0.80); otherwise Normal Flow / Non-Ischemic
- all numbers are compiled from the contract A-D artifact JSONs; missing or failed stages render as `unavailable: <reason>`, never as placeholder values

## Case 601 — ok

- synthetic ladder lesion placement: edge `601_b001` at s = 71.746 mm (longest edge, mid-length)

### Pipeline stages

| stage | status | exit code | runtime (s) | log | reason |
|---|---|---|---|---|---|
| stenosis | ok | 0 | 0.915 | out/clinical_logs/601_stenosis.log | — |
| hemodynamics | ok | 0 | 281.926 | out/clinical_logs/601_hemodynamics.log | — |
| stenosis_ladder | ok | 0 | 0.878 | out/clinical_logs/601_stenosis_ladder.log | — |
| hemodynamics_syn50 | ok | 0 | 238.765 | out/clinical_logs/601_hemodynamics_syn50.log | — |
| hemodynamics_syn70 | ok | 0 | 311.484 | out/clinical_logs/601_hemodynamics_syn70.log | — |
| hemodynamics_syn90 | ok | 0 | 248.289 | out/clinical_logs/601_hemodynamics_syn90.log | — |
| rxi_metrics | ok | 0 | 2.649 | out/clinical_logs/601_rxi_metrics.log | — |
| export_payload | ok | 0 | 2.867 | out/clinical_logs/601_export_payload.log | — |

### Clinical summary

| Target vessels evaluated | D_min (mm) | max %AS | Resting Pd/Pa | Hyperemic vFFR | Trans-lesion dP (mmHg, hyperemia) | TFC (frames) | calibrated transit (s) | Clinical decision |
|---|---|---|---|---|---|---|---|---|
| primary trunk 1 (root->tip, L=267.8 mm) (4 branches) | 1.107 | 59.9193 | 0.889556 | 0.674513 | 22.7312 | 47 / raw 136 | 1.57361 | Revascularization Indicated (vFFR <= 0.80) |
| primary trunk 2 (root->tip, L=166.3 mm) (3 branches) | 1.385 | 76.7263 | 0.846617 | 0.534004 | 34.5631 | 22 / raw 64 | 0.743127 | Revascularization Indicated (vFFR <= 0.80) |
| primary trunk 3 (root->tip, L=159.8 mm) (6 branches) | 2.4 | 65.9848 | 0.950887 | 0.774203 | 16.8114 | 13 / raw 38 | 0.443173 | Revascularization Indicated (vFFR <= 0.80) |

- case Hyperemic vFFR (min over target vessels): 0.534004 (path_2) — case Clinical decision: Revascularization Indicated (vFFR <= 0.80)
- vFFR provenance: row values are min(clinical.hyper.branches[e].vffr) over each path's branch_ids (per-edge values and cross-checks against paths[].min_vffr / pullback vFFR are in the JSON twin); Resting Pd/Pa = min(clinical.rest.branches[e].pd_pa); D_min / TFC / calibrated transit come from the ACIST RXi pullback and transit_anomaly; max %AS and trans-lesion dP from contract A lesions joined with clinical.lesions.
- vFFR context bands (not decision criteria): > 0.80 normal, 0.75–0.80 borderline, < 0.75 severe.


## Case 700 — ok

- synthetic ladder lesion placement: edge `700_b000` at s = 73.867 mm (longest edge, mid-length)

### Pipeline stages

| stage | status | exit code | runtime (s) | log | reason |
|---|---|---|---|---|---|
| stenosis | ok | 0 | 0.929 | out/clinical_logs/700_stenosis.log | — |
| hemodynamics | ok | 0 | 231.844 | out/clinical_logs/700_hemodynamics.log | — |
| stenosis_ladder | ok | 0 | 0.842 | out/clinical_logs/700_stenosis_ladder.log | — |
| hemodynamics_syn50 | ok | 0 | 274.939 | out/clinical_logs/700_hemodynamics_syn50.log | — |
| hemodynamics_syn70 | ok | 0 | 384.132 | out/clinical_logs/700_hemodynamics_syn70.log | — |
| hemodynamics_syn90 | ok | 0 | 590.76 | out/clinical_logs/700_hemodynamics_syn90.log | — |
| rxi_metrics | ok | 0 | 5.008 | out/clinical_logs/700_rxi_metrics.log | — |
| export_payload | ok | 0 | 5.328 | out/clinical_logs/700_export_payload.log | — |

### Clinical summary

| Target vessels evaluated | D_min (mm) | max %AS | Resting Pd/Pa | Hyperemic vFFR | Trans-lesion dP (mmHg, hyperemia) | TFC (frames) | calibrated transit (s) | Clinical decision |
|---|---|---|---|---|---|---|---|---|
| primary trunk 1 (root->tip, L=166.6 mm) (4 branches) | 1.504 | 64.7228 | 0.880452 | 0.644113 | 19.0904 | 19 / raw 38 | 0.617751 | Revascularization Indicated (vFFR <= 0.80) |
| primary trunk 2 (root->tip, L=163.1 mm) (4 branches) | 1.504 | 64.7228 | 0.877875 | 0.63725 | 19.0904 | 19 / raw 38 | 0.625139 | Revascularization Indicated (vFFR <= 0.80) |
| primary trunk 3 (root->tip, L=162.7 mm) (4 branches) | 1.504 | 64.7228 | 0.881022 | 0.645363 | 19.0904 | 19 / raw 38 | 0.616953 | Revascularization Indicated (vFFR <= 0.80) |

- case Hyperemic vFFR (min over target vessels): 0.63725 (path_2) — case Clinical decision: Revascularization Indicated (vFFR <= 0.80)
- vFFR provenance: row values are min(clinical.hyper.branches[e].vffr) over each path's branch_ids (per-edge values and cross-checks against paths[].min_vffr / pullback vFFR are in the JSON twin); Resting Pd/Pa = min(clinical.rest.branches[e].pd_pa); D_min / TFC / calibrated transit come from the ACIST RXi pullback and transit_anomaly; max %AS and trans-lesion dP from contract A lesions joined with clinical.lesions.
- vFFR context bands (not decision criteria): > 0.80 normal, 0.75–0.80 borderline, < 0.75 severe.


## Case 798 — ok

- synthetic ladder lesion placement: edge `798_b030` at s = 89.67 mm (longest edge, mid-length)

### Pipeline stages

| stage | status | exit code | runtime (s) | log | reason |
|---|---|---|---|---|---|
| stenosis | ok | 0 | 2.014 | out/clinical_logs/798_stenosis.log | — |
| hemodynamics | ok | 0 | 1201.56 | out/clinical_logs/798_hemodynamics.log | — |
| stenosis_ladder | ok | 0 | 3.543 | out/clinical_logs/798_stenosis_ladder.log | — |
| hemodynamics_syn50 | ok | 0 | 1145.32 | out/clinical_logs/798_hemodynamics_syn50.log | — |
| hemodynamics_syn70 | ok | 0 | 1034.16 | out/clinical_logs/798_hemodynamics_syn70.log | — |
| hemodynamics_syn90 | ok | 0 | 928.088 | out/clinical_logs/798_hemodynamics_syn90.log | — |
| rxi_metrics | ok | 0 | 2.947 | out/clinical_logs/798_rxi_metrics.log | — |
| export_payload | ok | 0 | 4.918 | out/clinical_logs/798_export_payload.log | — |

### Clinical summary

| Target vessels evaluated | D_min (mm) | max %AS | Resting Pd/Pa | Hyperemic vFFR | Trans-lesion dP (mmHg, hyperemia) | TFC (frames) | calibrated transit (s) | Clinical decision |
|---|---|---|---|---|---|---|---|---|
| primary trunk 1 (root->tip, L=243.0 mm) (6 branches) | 3.024 | 56.8384 | 0.986219 | 0.942972 | 2.42584 | 8 / raw 93 | 0.272852 | Normal Flow / Non-Ischemic |
| primary trunk 2 (root->tip, L=226.1 mm) (8 branches) | 1.21 | 90.9326 | 0.215478 | 0.111399 | 79.6826 | 26 / raw 299 | 0.877281 [raw anomaly addressed by calibration: raw 9.95s -> calibrated 0.88s] | Revascularization Indicated (vFFR <= 0.80) |
| primary trunk 3 (root->tip, L=215.9 mm) (7 branches) | 1.21 | 90.9326 | 0.216569 | 0.112564 | 79.6826 | 31 / raw 347 | 1.01918 [raw anomaly addressed by calibration: raw 11.56s -> calibrated 1.02s] | Revascularization Indicated (vFFR <= 0.80) |

- case Hyperemic vFFR (min over target vessels): 0.111399 (path_2) — case Clinical decision: Revascularization Indicated (vFFR <= 0.80)
- vFFR provenance: row values are min(clinical.hyper.branches[e].vffr) over each path's branch_ids (per-edge values and cross-checks against paths[].min_vffr / pullback vFFR are in the JSON twin); Resting Pd/Pa = min(clinical.rest.branches[e].pd_pa); D_min / TFC / calibrated transit come from the ACIST RXi pullback and transit_anomaly; max %AS and trans-lesion dP from contract A lesions joined with clinical.lesions.
- vFFR context bands (not decision criteria): > 0.80 normal, 0.75–0.80 borderline, < 0.75 severe.


## Synthetic lesion ladder

### Case 601

| Synthetic dia_reduction (%) | as_pct | dP_hyper (mmHg) | vFFR | Clinical decision |
|---|---|---|---|---|
| 50 | 75 | 65.0163 | 0.235218 | Revascularization Indicated (vFFR <= 0.80) |
| 70 | 91 | 81.0361 | 0.066218 | Revascularization Indicated (vFFR <= 0.80) |
| 90 | 99 | 82.0454 | 0.05557 | Revascularization Indicated (vFFR <= 0.80) |

- placement: synthetic lesion on edge `601_b001` at s = 71.746 mm (longest edge, mid-length); severity ladder proves monotonic response 50 -> 70 -> 90 (monotonicity check under Verification).

### Case 700

| Synthetic dia_reduction (%) | as_pct | dP_hyper (mmHg) | vFFR | Clinical decision |
|---|---|---|---|---|
| 50 | 75 | 20.5309 | 0.764511 | Revascularization Indicated (vFFR <= 0.80) |
| 70 | 91 | 73.6745 | 0.180101 | Revascularization Indicated (vFFR <= 0.80) |
| 90 | 99 | 84.9796 | 0.05578 | Revascularization Indicated (vFFR <= 0.80) |

- placement: synthetic lesion on edge `700_b000` at s = 73.867 mm (longest edge, mid-length); severity ladder proves monotonic response 50 -> 70 -> 90 (monotonicity check under Verification).

### Case 798

| Synthetic dia_reduction (%) | as_pct | dP_hyper (mmHg) | vFFR | Clinical decision |
|---|---|---|---|---|
| 50 | 75 | 45.8511 | 0.477739 | Revascularization Indicated (vFFR <= 0.80) |
| 70 | 91 | 81.2563 | 0.092431 | Revascularization Indicated (vFFR <= 0.80) |
| 90 | 99 | 84.6397 | 0.05561 | Revascularization Indicated (vFFR <= 0.80) |

- placement: synthetic lesion on edge `798_b030` at s = 89.67 mm (longest edge, mid-length); severity ladder proves monotonic response 50 -> 70 -> 90 (monotonicity check under Verification).

## Transit calibration

### Case 601

| path | distal transit raw (s) | distal transit calibrated (s) | TFC raw (frames) | TFC calibrated (frames) | flag |
|---|---|---|---|---|---|
| primary trunk 1 (root->tip, L=267.8 mm) | 4.52775 | 1.57361 | 136 | 47 | — |
| primary trunk 2 (root->tip, L=166.3 mm) | 2.1382 | 0.743127 | 64 | 22 | — |
| primary trunk 3 (root->tip, L=159.8 mm) | 1.27514 | 0.443173 | 38 | 13 | — |

- [path_1] note: calibrated distal transit 1.57 s is within the 5.0 s threshold (raw 4.53 s -> calibrated 1.57 s)
- [path_2] note: calibrated distal transit 0.74 s is within the 5.0 s threshold (raw 2.14 s -> calibrated 0.74 s)
- [path_3] note: calibrated distal transit 0.44 s is within the 5.0 s threshold (raw 1.28 s -> calibrated 0.44 s)
- TFC reference: fps 30, C-LAD cutoff 27 frames (contract C tfc block).

### Case 700

| path | distal transit raw (s) | distal transit calibrated (s) | TFC raw (frames) | TFC calibrated (frames) | flag |
|---|---|---|---|---|---|
| primary trunk 1 (root->tip, L=166.6 mm) | 1.25978 | 0.617751 | 38 | 19 | — |
| primary trunk 2 (root->tip, L=163.1 mm) | 1.27485 | 0.625139 | 38 | 19 | — |
| primary trunk 3 (root->tip, L=162.7 mm) | 1.25815 | 0.616953 | 38 | 19 | — |

- [path_1] note: calibrated distal transit 0.62 s is within the 5.0 s threshold (raw 1.26 s -> calibrated 0.62 s)
- [path_2] note: calibrated distal transit 0.63 s is within the 5.0 s threshold (raw 1.27 s -> calibrated 0.63 s)
- [path_3] note: calibrated distal transit 0.62 s is within the 5.0 s threshold (raw 1.26 s -> calibrated 0.62 s)
- TFC reference: fps 30, C-LAD cutoff 27 frames (contract C tfc block).

### Case 798

| path | distal transit raw (s) | distal transit calibrated (s) | TFC raw (frames) | TFC calibrated (frames) | flag |
|---|---|---|---|---|---|
| primary trunk 1 (root->tip, L=243.0 mm) | 3.09471 | 0.272852 | 93 | 8 | — |
| primary trunk 2 (root->tip, L=226.1 mm) | 9.95019 | 0.877281 | 299 | 26 | — |
| primary trunk 3 (root->tip, L=215.9 mm) | 11.5596 | 1.01918 | 347 | 31 | — |

- [path_1] note: calibrated distal transit 0.27 s is within the 5.0 s threshold (raw 3.09 s -> calibrated 0.27 s)
- [path_2] note: raw distal transit 9.95 s exceeds the 5.0 s threshold, but velocity calibration addresses the anomaly (raw 9.95 s -> calibrated 0.88 s, within the threshold)
- [path_3] note: raw distal transit 11.56 s exceeds the 5.0 s threshold, but velocity calibration addresses the anomaly (raw 11.56 s -> calibrated 1.02 s, within the threshold)
- TFC reference: fps 30, C-LAD cutoff 27 frames (contract C tfc block).

## Verification

### hyperemic flow increase in [2.5, 4.0]x per case

- PASS: case 601 — clinical.hyper.flow_increase_x = 2.55959x (value: 2.55959; expected: [2.5, 4.0]x)
- PASS: case 700 — clinical.hyper.flow_increase_x = 2.64194x (value: 2.64194; expected: [2.5, 4.0]x)
- PASS: case 798 — clinical.hyper.flow_increase_x = 2.70196x (value: 2.70196; expected: [2.5, 4.0]x)

### primary-trunk calibrated transit within [1.2, 3.5] s per case

- FAIL: case 601 — path_1: 1.57361 s (transit_anomaly[path_1].distal_calibrated_s); path_1 convective L/(kappa*u) cross-reference: 0.577198 s (velocity_calibration.primary_paths[path_1]); path_2 distal_calibrated_s 0.743127 s (band 1.2-3.5); path_2 convective L/(kappa*u) cross-reference: 0.557837 s (velocity_calibration.primary_paths[path_2]); path_3 distal_calibrated_s 0.443173 s (band 1.2-3.5); path_3 convective L/(kappa*u) cross-reference: 0.557837 s (per-edge sum over 6 path branch_ids) (value: 1.57361, 0.743127, 0.443173; expected: [1.2, 3.5] s)
- FAIL: case 700 — path_1 distal_calibrated_s 0.617751 s (band 1.2-3.5); path_1 convective L/(kappa*u) cross-reference: 0.59188 s (velocity_calibration.primary_paths[path_1]); path_2 distal_calibrated_s 0.625139 s (band 1.2-3.5); path_2 convective L/(kappa*u) cross-reference: 0.577554 s (velocity_calibration.primary_paths[path_2]); path_3 distal_calibrated_s 0.616953 s (band 1.2-3.5); path_3 convective L/(kappa*u) cross-reference: 0.577554 s (per-edge sum over 4 path branch_ids) (value: 0.617751, 0.625139, 0.616953; expected: [1.2, 3.5] s)
- FAIL: case 798 — path_1 distal_calibrated_s 0.272852 s (band 1.2-3.5); path_1 convective L/(kappa*u) cross-reference: 1.86037 s (velocity_calibration.primary_paths[path_1]); path_2 distal_calibrated_s 0.877281 s (band 1.2-3.5) [raw anomaly addressed by calibration: raw 9.95s -> calibrated 0.88s]; path_2 convective L/(kappa*u) cross-reference: 2.02281 s (velocity_calibration.primary_paths[path_2]); path_3 distal_calibrated_s 1.01918 s (band 1.2-3.5) [raw anomaly addressed by calibration: raw 11.56s -> calibrated 1.02s]; path_3 convective L/(kappa*u) cross-reference: 2.02281 s (per-edge sum over 7 path branch_ids) (value: 0.272852, 0.877281, 1.01918; expected: [1.2, 3.5] s)

### ladder vFFR monotonically non-increasing with severity (50 -> 70 -> 90) per case

- PASS: case 601 — 50%: 0.235218 -> 70%: 0.066218 -> 90%: 0.05557 (value: 0.235218, 0.066218, 0.05557; expected: non-increasing with dia_reduction)
- PASS: case 700 — 50%: 0.764511 -> 70%: 0.180101 -> 90%: 0.05578 (value: 0.764511, 0.180101, 0.05578; expected: non-increasing with dia_reduction)
- PASS: case 798 — 50%: 0.477739 -> 70%: 0.092431 -> 90%: 0.05561 (value: 0.477739, 0.092431, 0.05561; expected: non-increasing with dia_reduction)

### payload < 2 MB per case

- PASS: case 601 — viewer/models/clinical/case_601_clinical.json: 195484 bytes (0.186 MB); viewer/public/clinical/case_601_clinical.json: 195484 bytes (0.186 MB) (value: 195484 bytes; expected: < 2097152 bytes (2 MB))
- PASS: case 700 — viewer/models/clinical/case_700_clinical.json: 165690 bytes (0.158 MB); viewer/public/clinical/case_700_clinical.json: 165690 bytes (0.158 MB) (value: 165690 bytes; expected: < 2097152 bytes (2 MB))
- PASS: case 798 — viewer/models/clinical/case_798_clinical.json: 243843 bytes (0.233 MB); viewer/public/clinical/case_798_clinical.json: 243843 bytes (0.233 MB) (value: 243843 bytes; expected: < 2097152 bytes (2 MB))

### vFFR decision rule applied consistently per case

- PASS: case 601 — 7/7 decision instances match vFFR <= 0.8 rule; row vFFR = min clinical.hyper.branches[*].vffr over path branch_ids (3 rows cross-checked) (value: Revascularization Indicated (vFFR <= 0.80), Revascularization Indicated (vFFR <= 0.80), Revascularization Indicated (vFFR <= 0.80), Revascularization Indicated (vFFR <= 0.80), Revascularization Indicated (vFFR <= 0.80), Revascularization Indicated (vFFR <= 0.80), Revascularization Indicated (vFFR <= 0.80); expected: <= 0.8 -> Revascularization Indicated (vFFR <= 0.80) else Normal Flow / Non-Ischemic)
- PASS: case 700 — 7/7 decision instances match vFFR <= 0.8 rule; row vFFR = min clinical.hyper.branches[*].vffr over path branch_ids (3 rows cross-checked) (value: Revascularization Indicated (vFFR <= 0.80), Revascularization Indicated (vFFR <= 0.80), Revascularization Indicated (vFFR <= 0.80), Revascularization Indicated (vFFR <= 0.80), Revascularization Indicated (vFFR <= 0.80), Revascularization Indicated (vFFR <= 0.80), Revascularization Indicated (vFFR <= 0.80); expected: <= 0.8 -> Revascularization Indicated (vFFR <= 0.80) else Normal Flow / Non-Ischemic)
- PASS: case 798 — 7/7 decision instances match vFFR <= 0.8 rule; row vFFR = min clinical.hyper.branches[*].vffr over path branch_ids (3 rows cross-checked) (value: Normal Flow / Non-Ischemic, Revascularization Indicated (vFFR <= 0.80), Revascularization Indicated (vFFR <= 0.80), Revascularization Indicated (vFFR <= 0.80), Revascularization Indicated (vFFR <= 0.80), Revascularization Indicated (vFFR <= 0.80), Revascularization Indicated (vFFR <= 0.80); expected: <= 0.8 -> Revascularization Indicated (vFFR <= 0.80) else Normal Flow / Non-Ischemic)

- overall: FAIL (12/15 checks pass)

## failures

Mirrors `out/clinical_errors.log`.

No failures recorded — `out/clinical_errors.log` was not created.
