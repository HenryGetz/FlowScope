# Aggregate batch report (Flowscope Track 1)

- generated: 2026-09-23T19:52:12+00:00 by `tools/aggregate_report.py` v1
- command: `/home/wavy/ai/flowscope/.venv/bin/python tools/aggregate_report.py`
- batches: 4 | cases: 355
- triangle budget: 120,000 (hard window 100,000-150,000)

## Aggregate (all batches)

| metric | value |
| --- | --- |
| cases in 100,000-150,000 window | 207/355 |
| final triangles min / max / mean | 39,836 / 120,000 / 96,939.3 |
| reduction % min / max / mean | 0 / 53.73 / 13.33 |
| volume drift % (case max) min / median / mean / p90 / p95 / p99 / max | 0.1141 / 0.5443 / 2.120291 / 0.5833 / 0.9734 / 94.3085 / 100 |
| cases over 0.5% / 1% / 5% volume drift | 206 (68.44%) / 15 (4.98%) / 5 (1.66%) |
| GLB bytes min / max / mean | 424,240 / 1,267,532 / 1,024,124.55 |
| runtime total s | 4,250.24 |

## Volume drift distribution (355 measured scans)

Case-level max per-structure drift (`volume_drift_pct_max`) over anomaly-free case rows (n = 301 measured, 54 null of 355 rows; 0 anomaly rows excluded):

| n | null | min | median | mean | p90 | p95 | p99 | max |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 301 | 54 | 0.1141 | 0.5443 | 2.120291 | 0.5833 | 0.9734 | 94.3085 | 100 |

| threshold | cases exceeding | % of measured cases |
| --- | --- | --- |
| > 0.5% | 206 | 68.44% |
| > 1% | 15 | 4.98% |
| > 5% | 5 | 1.66% |

Per-structure drift (`volume_drift_pct`) over measured structure rows (n = 1,119; 641 open/FOV-truncated rows with null metrics and 577 skipped rows counted separately):

| n | null-metric (open/FOV) | skipped | min | median | mean | p90 | p95 | p99 | max |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1,119 | 641 | 577 | 0.001 | 0.2718 | 0.753454 | 0.55408 | 0.5722 | 1.01801 | 100 |

| threshold | structures exceeding | % of measured structures |
| --- | --- | --- |
| > 0.5% | 242 | 21.63% |
| > 1% | 15 | 1.34% |
| > 5% | 5 | 0.45% |

Signed drift (`(volume_smoothed_mm3 - volume_raw_mm3) / volume_raw_mm3 * 100`) over the same measured structure rows (n = 1,119):

| n | min | median | mean | max | shrink (< 0) | unchanged (= 0) | grow (> 0) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1,119 | -100 | -0.271802 | -0.753032 | 0.034399 | 1,104 (98.66%) | 0 | 15 (1.34%) |

Taubin non-shrinking does NOT hold strictly (no-volume-loss holds only approximately): median signed drift -0.271802%, 98.66% of structures shrink (signed drift range -100% to 0.034399%).

- percentile method: numpy.percentile with linear interpolation.

## Per-batch

| batch | created | cases | in window | final min | final max | final mean | reduction mean % | drift max % | GLB mean bytes | runtime s |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| batch-005 | 2026-09-23T11:50:54+00:00 | 5 | 5/5 | 103,274 | 119,998 | 116,652.4 | 18.2 | 0.53 | 1,228,340.8 | 126.55 |
| batch-050 | 2026-09-23T12:33:06+00:00 | 50 | 30/50 | 54,806 | 119,998 | 99,941.62 | 13.35 | 100 | 1,055,998.72 | 540.35 |
| batch-100 | 2026-09-23T12:22:08+00:00 | 100 | 53/100 | 39,836 | 120,000 | 94,875.62 | 12.41 | 100 | 1,002,441.28 | 1,003.71 |
| batch-200 | 2026-09-23T12:25:17+00:00 | 200 | 119/200 | 39,836 | 120,000 | 96,727.74 | 13.67 | 100 | 1,021,892.24 | 2,579.63 |

## Per-case

| batch | case | source | input tris | final tris | in window (100,000-150,000) | reduction % | drift % max | GLB bytes | runtime s | integrity | anomaly |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| batch-005 | 601 | `data/raw/imagecas/601/label.nii.gz` | 150,128 | 119,998 | yes | 20.07 | 0.21 | 1,261,272 | 35.15 | ok | - |
| batch-005 | 700 | `data/raw/imagecas/700/label.nii.gz` | 127,076 | 119,998 | yes | 5.57 | 0.24 | 1,261,268 | 31.25 | ok | - |
| batch-005 | 798 | `data/raw/imagecas/798/label.nii.gz` | 143,596 | 119,998 | yes | 16.43 | 0.11 | 1,261,176 | 37.77 | ok | - |
| batch-005 | s0004 | `data/raw/totalseg_ct/s0004/segmentations` | 149,444 | 103,274 | yes | 30.89 | 0.53 | 1,090,456 | 10.32 | ok | - |
| batch-005 | s0015 | `data/raw/totalseg_ct/s0015/segmentations` | 146,378 | 119,994 | yes | 18.02 | 0.19 | 1,267,532 | 12.06 | ok | - |
| batch-050 | s0010 | `data/raw/totalseg_ct/s0010/segmentations` | 75,824 | 75,824 | no | 0 | 0.57 | 801,000 | 7.75 | ok | - |
| batch-050 | s0011 | `data/raw/totalseg_ct/s0011/segmentations` | 145,592 | 103,422 | yes | 28.96 | 1.01 | 1,092,084 | 17.72 | ok | - |
| batch-050 | s0012 | `data/raw/totalseg_ct/s0012/segmentations` | 97,386 | 97,386 | no | 0 | 0.25 | 1,028,624 | 8.28 | ok | - |
| batch-050 | s0013 | `data/raw/totalseg_ct/s0013/segmentations` | 54,806 | 54,806 | no | 0 | n/a | 580,584 | 6.77 | ok | - |
| batch-050 | s0014 | `data/raw/totalseg_ct/s0014/segmentations` | 82,190 | 82,190 | no | 0 | 0.23 | 871,032 | 10.01 | ok | - |
| batch-050 | s0016 | `data/raw/totalseg_ct/s0016/segmentations` | 69,588 | 69,588 | no | 0 | 0.16 | 738,320 | 8.29 | ok | - |
| batch-050 | s0019 | `data/raw/totalseg_ct/s0019/segmentations` | 159,288 | 119,998 | yes | 24.67 | 0.59 | 1,266,080 | 10.74 | ok | - |
| batch-050 | s0024 | `data/raw/totalseg_ct/s0024/segmentations` | 259,316 | 119,998 | yes | 53.73 | 0.57 | 1,266,096 | 25.13 | ok | - |
| batch-050 | s0028 | `data/raw/totalseg_ct/s0028/segmentations` | 133,412 | 101,518 | yes | 23.91 | 0.53 | 1,072,004 | 12.56 | ok | - |
| batch-050 | s0029 | `data/raw/totalseg_ct/s0029/segmentations` | 204,084 | 100,822 | yes | 50.6 | 0.55 | 1,064,720 | 15.33 | ok | - |
| batch-050 | s0030 | `data/raw/totalseg_ct/s0030/segmentations` | 111,408 | 111,408 | yes | 0 | 0.22 | 1,178,740 | 10.89 | ok | - |
| batch-050 | s0031 | `data/raw/totalseg_ct/s0031/segmentations` | 61,054 | 61,054 | no | 0 | 0.2 | 648,088 | 6.64 | ok | - |
| batch-050 | s0032 | `data/raw/totalseg_ct/s0032/segmentations` | 105,224 | 105,224 | yes | 0 | 0.97 | 1,111,148 | 5.67 | ok | - |
| batch-050 | s0037 | `data/raw/totalseg_ct/s0037/segmentations` | 141,720 | 119,991 | yes | 15.33 | 0.54 | 1,266,220 | 10.35 | ok | - |
| batch-050 | s0039 | `data/raw/totalseg_ct/s0039/segmentations` | 57,074 | 57,074 | no | 0 | 0.54 | 604,336 | 4.35 | ok | - |
| batch-050 | s0040 | `data/raw/totalseg_ct/s0040/segmentations` | 157,740 | 102,644 | yes | 34.93 | 0.56 | 1,083,888 | 15.12 | ok | - |
| batch-050 | s0042 | `data/raw/totalseg_ct/s0042/segmentations` | 60,146 | 60,146 | no | 0 | n/a | 637,436 | 7.7 | ok | - |
| batch-050 | s0045 | `data/raw/totalseg_ct/s0045/segmentations` | 98,798 | 98,798 | no | 0 | 0.26 | 1,045,492 | 6.5 | ok | - |
| batch-050 | s0046 | `data/raw/totalseg_ct/s0046/segmentations` | 161,090 | 119,997 | yes | 25.51 | 0.55 | 1,266,368 | 11.34 | ok | - |
| batch-050 | s0049 | `data/raw/totalseg_ct/s0049/segmentations` | 200,208 | 119,996 | yes | 40.06 | 0.58 | 1,266,084 | 13.92 | ok | - |
| batch-050 | s0050 | `data/raw/totalseg_ct/s0050/segmentations` | 134,520 | 119,994 | yes | 10.8 | 0.55 | 1,266,036 | 9.64 | ok | - |
| batch-050 | s0052 | `data/raw/totalseg_ct/s0052/segmentations` | 68,156 | 68,156 | no | 0 | n/a | 721,280 | 6.6 | ok | - |
| batch-050 | s0053 | `data/raw/totalseg_ct/s0053/segmentations` | 91,142 | 91,142 | no | 0 | 0.25 | 965,076 | 10.37 | ok | - |
| batch-050 | s0054 | `data/raw/totalseg_ct/s0054/segmentations` | 68,996 | 68,996 | no | 0 | 0.12 | 731,676 | 6.88 | ok | - |
| batch-050 | s0057 | `data/raw/totalseg_ct/s0057/segmentations` | 148,230 | 119,997 | yes | 19.05 | 0.14 | 1,267,052 | 9.48 | ok | - |
| batch-050 | s0058 | `data/raw/totalseg_ct/s0058/segmentations` | 101,090 | 101,090 | yes | 0 | 0.23 | 1,070,044 | 9.74 | ok | - |
| batch-050 | s0059 | `data/raw/totalseg_ct/s0059/segmentations` | 127,048 | 119,990 | yes | 5.55 | 0.58 | 1,266,072 | 8.84 | ok | - |
| batch-050 | s0065 | `data/raw/totalseg_ct/s0065/segmentations` | 197,260 | 119,994 | yes | 39.17 | 0.58 | 1,266,060 | 18.04 | ok | - |
| batch-050 | s0067 | `data/raw/totalseg_ct/s0067/segmentations` | 120,252 | 119,998 | yes | 0.21 | 0.49 | 1,266,020 | 7.29 | ok | - |
| batch-050 | s0070 | `data/raw/totalseg_ct/s0070/segmentations` | 122,448 | 119,996 | yes | 2 | 0.55 | 1,266,064 | 10.18 | ok | - |
| batch-050 | s0071 | `data/raw/totalseg_ct/s0071/segmentations` | 132,684 | 101,144 | yes | 23.77 | 0.52 | 1,068,120 | 12.95 | ok | - |
| batch-050 | s0072 | `data/raw/totalseg_ct/s0072/segmentations` | 71,040 | 71,040 | no | 0 | 0.26 | 752,220 | 8.52 | ok | - |
| batch-050 | s0075 | `data/raw/totalseg_ct/s0075/segmentations` | 93,586 | 93,586 | no | 0 | 0.22 | 990,624 | 10.29 | ok | - |
| batch-050 | s0076 | `data/raw/totalseg_ct/s0076/segmentations` | 185,808 | 119,998 | yes | 35.42 | 0.58 | 1,266,120 | 20.7 | ok | - |
| batch-050 | s0078 | `data/raw/totalseg_ct/s0078/segmentations` | 84,472 | 84,472 | no | 0 | 0.27 | 893,100 | 7.2 | ok | - |
| batch-050 | s0080 | `data/raw/totalseg_ct/s0080/segmentations` | 159,334 | 100,791 | yes | 36.74 | 0.54 | 1,064,640 | 11.09 | ok | - |
| batch-050 | s0082 | `data/raw/totalseg_ct/s0082/segmentations` | 63,376 | 63,376 | no | 0 | 0.25 | 671,296 | 6.36 | ok | - |
| batch-050 | s0083 | `data/raw/totalseg_ct/s0083/segmentations` | 125,306 | 119,995 | yes | 4.24 | 0.23 | 1,266,760 | 6.08 | ok | - |
| batch-050 | s0084 | `data/raw/totalseg_ct/s0084/segmentations` | 105,350 | 105,350 | yes | 0 | 0.29 | 1,113,896 | 5.33 | ok | - |
| batch-050 | s0085 | `data/raw/totalseg_ct/s0085/segmentations` | 108,184 | 108,184 | yes | 0 | 0.55 | 1,143,380 | 6.28 | ok | - |
| batch-050 | s0086 | `data/raw/totalseg_ct/s0086/segmentations` | 174,320 | 119,996 | yes | 31.16 | 0.55 | 1,266,076 | 22.56 | ok | - |
| batch-050 | s0088 | `data/raw/totalseg_ct/s0088/segmentations` | 92,634 | 92,634 | no | 0 | n/a | 982,824 | 6.37 | ok | - |
| batch-050 | s0091 | `data/raw/totalseg_ct/s0091/segmentations` | 209,164 | 119,994 | yes | 42.63 | 0.57 | 1,266,060 | 21.42 | ok | - |
| batch-050 | s0095 | `data/raw/totalseg_ct/s0095/segmentations` | 222,208 | 119,992 | yes | 46 | 0.58 | 1,266,076 | 20.14 | ok | - |
| batch-050 | s0096 | `data/raw/totalseg_ct/s0096/segmentations` | 94,834 | 94,834 | no | 0 | 0.18 | 1,004,408 | 9.01 | ok | - |
| batch-050 | s0102 | `data/raw/totalseg_ct/s0102/segmentations` | 157,824 | 119,994 | yes | 23.97 | 0.56 | 1,266,036 | 12.13 | ok | - |
| batch-050 | s0106 | `data/raw/totalseg_ct/s0106/segmentations` | 125,982 | 119,997 | yes | 4.75 | n/a | 1,267,428 | 6.77 | ok | - |
| batch-050 | s0107 | `data/raw/totalseg_ct/s0107/segmentations` | 97,192 | 97,192 | no | 0 | 0.24 | 1,027,188 | 10.32 | ok | - |
| batch-050 | s0109 | `data/raw/totalseg_ct/s0109/segmentations` | 93,280 | 93,280 | no | 0 | 100 | 983,448 | 10.5 | ok | - |
| batch-050 | s0111 | `data/raw/totalseg_ct/s0111/segmentations` | 216,272 | 119,995 | yes | 44.52 | 0.57 | 1,266,512 | 14.21 | ok | - |
| batch-100 | s0010 | `data/raw/totalseg_ct/s0010/segmentations` | 75,824 | 75,824 | no | 0 | 0.57 | 801,000 | 8.48 | ok | - |
| batch-100 | s0011 | `data/raw/totalseg_ct/s0011/segmentations` | 145,592 | 103,422 | yes | 28.96 | 1.01 | 1,092,084 | 17.92 | ok | - |
| batch-100 | s0012 | `data/raw/totalseg_ct/s0012/segmentations` | 97,386 | 97,386 | no | 0 | 0.25 | 1,028,624 | 6.96 | ok | - |
| batch-100 | s0013 | `data/raw/totalseg_ct/s0013/segmentations` | 54,806 | 54,806 | no | 0 | n/a | 580,584 | 6.3 | ok | - |
| batch-100 | s0014 | `data/raw/totalseg_ct/s0014/segmentations` | 82,190 | 82,190 | no | 0 | 0.23 | 871,032 | 9.67 | ok | - |
| batch-100 | s0016 | `data/raw/totalseg_ct/s0016/segmentations` | 69,588 | 69,588 | no | 0 | 0.16 | 738,320 | 10.76 | ok | - |
| batch-100 | s0019 | `data/raw/totalseg_ct/s0019/segmentations` | 159,288 | 119,998 | yes | 24.67 | 0.59 | 1,266,080 | 12.76 | ok | - |
| batch-100 | s0024 | `data/raw/totalseg_ct/s0024/segmentations` | 259,316 | 119,998 | yes | 53.73 | 0.57 | 1,266,096 | 24.06 | ok | - |
| batch-100 | s0028 | `data/raw/totalseg_ct/s0028/segmentations` | 133,412 | 101,518 | yes | 23.91 | 0.53 | 1,072,004 | 17.55 | ok | - |
| batch-100 | s0029 | `data/raw/totalseg_ct/s0029/segmentations` | 204,084 | 100,822 | yes | 50.6 | 0.55 | 1,064,720 | 18.47 | ok | - |
| batch-100 | s0030 | `data/raw/totalseg_ct/s0030/segmentations` | 111,408 | 111,408 | yes | 0 | 0.22 | 1,178,740 | 10.76 | ok | - |
| batch-100 | s0031 | `data/raw/totalseg_ct/s0031/segmentations` | 61,054 | 61,054 | no | 0 | 0.2 | 648,088 | 8.46 | ok | - |
| batch-100 | s0032 | `data/raw/totalseg_ct/s0032/segmentations` | 105,224 | 105,224 | yes | 0 | 0.97 | 1,111,148 | 7.99 | ok | - |
| batch-100 | s0037 | `data/raw/totalseg_ct/s0037/segmentations` | 141,720 | 119,991 | yes | 15.33 | 0.54 | 1,266,220 | 9.07 | ok | - |
| batch-100 | s0039 | `data/raw/totalseg_ct/s0039/segmentations` | 57,074 | 57,074 | no | 0 | 0.54 | 604,336 | 5.18 | ok | - |
| batch-100 | s0040 | `data/raw/totalseg_ct/s0040/segmentations` | 157,740 | 102,644 | yes | 34.93 | 0.56 | 1,083,888 | 14.84 | ok | - |
| batch-100 | s0042 | `data/raw/totalseg_ct/s0042/segmentations` | 60,146 | 60,146 | no | 0 | n/a | 637,436 | 9.47 | ok | - |
| batch-100 | s0045 | `data/raw/totalseg_ct/s0045/segmentations` | 98,798 | 98,798 | no | 0 | 0.26 | 1,045,492 | 8.24 | ok | - |
| batch-100 | s0046 | `data/raw/totalseg_ct/s0046/segmentations` | 161,090 | 119,997 | yes | 25.51 | 0.55 | 1,266,368 | 13.54 | ok | - |
| batch-100 | s0049 | `data/raw/totalseg_ct/s0049/segmentations` | 200,208 | 119,996 | yes | 40.06 | 0.58 | 1,266,084 | 13.91 | ok | - |
| batch-100 | s0050 | `data/raw/totalseg_ct/s0050/segmentations` | 134,520 | 119,994 | yes | 10.8 | 0.55 | 1,266,036 | 11.23 | ok | - |
| batch-100 | s0052 | `data/raw/totalseg_ct/s0052/segmentations` | 68,156 | 68,156 | no | 0 | n/a | 721,280 | 7.64 | ok | - |
| batch-100 | s0053 | `data/raw/totalseg_ct/s0053/segmentations` | 91,142 | 91,142 | no | 0 | 0.25 | 965,076 | 13.18 | ok | - |
| batch-100 | s0054 | `data/raw/totalseg_ct/s0054/segmentations` | 68,996 | 68,996 | no | 0 | 0.12 | 731,676 | 7.83 | ok | - |
| batch-100 | s0057 | `data/raw/totalseg_ct/s0057/segmentations` | 148,230 | 119,997 | yes | 19.05 | 0.14 | 1,267,052 | 9.57 | ok | - |
| batch-100 | s0058 | `data/raw/totalseg_ct/s0058/segmentations` | 101,090 | 101,090 | yes | 0 | 0.23 | 1,070,044 | 10.35 | ok | - |
| batch-100 | s0059 | `data/raw/totalseg_ct/s0059/segmentations` | 127,048 | 119,990 | yes | 5.55 | 0.58 | 1,266,072 | 10.59 | ok | - |
| batch-100 | s0065 | `data/raw/totalseg_ct/s0065/segmentations` | 197,260 | 119,994 | yes | 39.17 | 0.58 | 1,266,060 | 17.01 | ok | - |
| batch-100 | s0067 | `data/raw/totalseg_ct/s0067/segmentations` | 120,252 | 119,998 | yes | 0.21 | 0.49 | 1,266,020 | 7.32 | ok | - |
| batch-100 | s0070 | `data/raw/totalseg_ct/s0070/segmentations` | 122,448 | 119,996 | yes | 2 | 0.55 | 1,266,064 | 8.7 | ok | - |
| batch-100 | s0071 | `data/raw/totalseg_ct/s0071/segmentations` | 132,684 | 101,144 | yes | 23.77 | 0.52 | 1,068,120 | 9.83 | ok | - |
| batch-100 | s0072 | `data/raw/totalseg_ct/s0072/segmentations` | 71,040 | 71,040 | no | 0 | 0.26 | 752,220 | 7.05 | ok | - |
| batch-100 | s0075 | `data/raw/totalseg_ct/s0075/segmentations` | 93,586 | 93,586 | no | 0 | 0.22 | 990,624 | 8.86 | ok | - |
| batch-100 | s0076 | `data/raw/totalseg_ct/s0076/segmentations` | 185,808 | 119,998 | yes | 35.42 | 0.58 | 1,266,120 | 18.25 | ok | - |
| batch-100 | s0078 | `data/raw/totalseg_ct/s0078/segmentations` | 84,472 | 84,472 | no | 0 | 0.27 | 893,100 | 6.61 | ok | - |
| batch-100 | s0080 | `data/raw/totalseg_ct/s0080/segmentations` | 159,334 | 100,791 | yes | 36.74 | 0.54 | 1,064,640 | 9.95 | ok | - |
| batch-100 | s0082 | `data/raw/totalseg_ct/s0082/segmentations` | 63,376 | 63,376 | no | 0 | 0.25 | 671,296 | 7.99 | ok | - |
| batch-100 | s0083 | `data/raw/totalseg_ct/s0083/segmentations` | 125,306 | 119,995 | yes | 4.24 | 0.23 | 1,266,760 | 7.01 | ok | - |
| batch-100 | s0084 | `data/raw/totalseg_ct/s0084/segmentations` | 105,350 | 105,350 | yes | 0 | 0.29 | 1,113,896 | 4.91 | ok | - |
| batch-100 | s0085 | `data/raw/totalseg_ct/s0085/segmentations` | 108,184 | 108,184 | yes | 0 | 0.55 | 1,143,380 | 5.39 | ok | - |
| batch-100 | s0086 | `data/raw/totalseg_ct/s0086/segmentations` | 174,320 | 119,996 | yes | 31.16 | 0.55 | 1,266,076 | 17.52 | ok | - |
| batch-100 | s0088 | `data/raw/totalseg_ct/s0088/segmentations` | 92,634 | 92,634 | no | 0 | n/a | 982,824 | 6.21 | ok | - |
| batch-100 | s0091 | `data/raw/totalseg_ct/s0091/segmentations` | 209,164 | 119,994 | yes | 42.63 | 0.57 | 1,266,060 | 17.13 | ok | - |
| batch-100 | s0095 | `data/raw/totalseg_ct/s0095/segmentations` | 222,208 | 119,992 | yes | 46 | 0.58 | 1,266,076 | 13.73 | ok | - |
| batch-100 | s0096 | `data/raw/totalseg_ct/s0096/segmentations` | 94,834 | 94,834 | no | 0 | 0.18 | 1,004,408 | 6.61 | ok | - |
| batch-100 | s0102 | `data/raw/totalseg_ct/s0102/segmentations` | 157,824 | 119,994 | yes | 23.97 | 0.56 | 1,266,036 | 11.23 | ok | - |
| batch-100 | s0106 | `data/raw/totalseg_ct/s0106/segmentations` | 125,982 | 119,997 | yes | 4.75 | n/a | 1,267,428 | 6.62 | ok | - |
| batch-100 | s0107 | `data/raw/totalseg_ct/s0107/segmentations` | 97,192 | 97,192 | no | 0 | 0.24 | 1,027,188 | 8.91 | ok | - |
| batch-100 | s0109 | `data/raw/totalseg_ct/s0109/segmentations` | 93,280 | 93,280 | no | 0 | 100 | 983,448 | 8.16 | ok | - |
| batch-100 | s0111 | `data/raw/totalseg_ct/s0111/segmentations` | 216,272 | 119,995 | yes | 44.52 | 0.57 | 1,266,512 | 12.05 | ok | - |
| batch-100 | s0114 | `data/raw/totalseg_ct/s0114/segmentations` | 162,984 | 119,996 | yes | 26.38 | 0.55 | 1,266,092 | 11.49 | ok | - |
| batch-100 | s0115 | `data/raw/totalseg_ct/s0115/segmentations` | 189,992 | 101,457 | yes | 46.6 | 0.58 | 1,071,624 | 12.65 | ok | - |
| batch-100 | s0119 | `data/raw/totalseg_ct/s0119/segmentations` | 117,330 | 117,330 | yes | 0 | 0.28 | 1,241,448 | 9.97 | ok | - |
| batch-100 | s0120 | `data/raw/totalseg_ct/s0120/segmentations` | 78,932 | 78,932 | no | 0 | 94.31 | 832,880 | 7.94 | ok | - |
| batch-100 | s0123 | `data/raw/totalseg_ct/s0123/segmentations` | 150,700 | 119,996 | yes | 20.37 | 0.54 | 1,266,084 | 10.95 | ok | - |
| batch-100 | s0124 | `data/raw/totalseg_ct/s0124/segmentations` | 167,072 | 119,994 | yes | 28.18 | 0.54 | 1,266,032 | 18.96 | ok | - |
| batch-100 | s0128 | `data/raw/totalseg_ct/s0128/segmentations` | 46,494 | 46,494 | no | 0 | 0.51 | 492,736 | 5.71 | ok | - |
| batch-100 | s0133 | `data/raw/totalseg_ct/s0133/segmentations` | 71,230 | 71,230 | no | 0 | 0.24 | 754,696 | 11.45 | ok | - |
| batch-100 | s0135 | `data/raw/totalseg_ct/s0135/segmentations` | 59,900 | 59,900 | no | 0 | n/a | 635,240 | 9.02 | ok | - |
| batch-100 | s0137 | `data/raw/totalseg_ct/s0137/segmentations` | 71,278 | 71,278 | no | 0 | 0.55 | 752,100 | 10.4 | ok | - |
| batch-100 | s0138 | `data/raw/totalseg_ct/s0138/segmentations` | 161,400 | 119,991 | yes | 25.66 | 0.77 | 1,266,292 | 14.73 | ok | - |
| batch-100 | s0141 | `data/raw/totalseg_ct/s0141/segmentations` | 143,564 | 101,240 | yes | 29.48 | 0.52 | 1,069,148 | 15.14 | ok | - |
| batch-100 | s0145 | `data/raw/totalseg_ct/s0145/segmentations` | 216,984 | 119,994 | yes | 44.7 | 0.57 | 1,266,060 | 18.85 | ok | - |
| batch-100 | s0146 | `data/raw/totalseg_ct/s0146/segmentations` | 160,692 | 119,996 | yes | 25.32 | 0.55 | 1,266,100 | 12.48 | ok | - |
| batch-100 | s0147 | `data/raw/totalseg_ct/s0147/segmentations` | 85,066 | 85,066 | no | 0 | 0.26 | 897,652 | 10.07 | ok | - |
| batch-100 | s0150 | `data/raw/totalseg_ct/s0150/segmentations` | 89,510 | 89,510 | no | 0 | 0.56 | 944,668 | 6.97 | ok | - |
| batch-100 | s0153 | `data/raw/totalseg_ct/s0153/segmentations` | 82,514 | 82,514 | no | 0 | 0.33 | 870,136 | 6.26 | ok | - |
| batch-100 | s0156 | `data/raw/totalseg_ct/s0156/segmentations` | 61,698 | 61,698 | no | 0 | n/a | 653,464 | 5.18 | ok | - |
| batch-100 | s0158 | `data/raw/totalseg_ct/s0158/segmentations` | 62,998 | 62,998 | no | 0 | n/a | 666,672 | 7.31 | ok | - |
| batch-100 | s0160 | `data/raw/totalseg_ct/s0160/segmentations` | 66,494 | 66,494 | no | 0 | n/a | 706,060 | 7.37 | ok | - |
| batch-100 | s0162 | `data/raw/totalseg_ct/s0162/segmentations` | 151,952 | 119,996 | yes | 21.03 | 0.53 | 1,266,056 | 10.08 | ok | - |
| batch-100 | s0163 | `data/raw/totalseg_ct/s0163/segmentations` | 189,376 | 119,994 | yes | 36.64 | 0.56 | 1,266,024 | 14.75 | ok | - |
| batch-100 | s0165 | `data/raw/totalseg_ct/s0165/segmentations` | 56,930 | 56,930 | no | 0 | n/a | 605,452 | 5.66 | ok | - |
| batch-100 | s0166 | `data/raw/totalseg_ct/s0166/segmentations` | 97,756 | 97,756 | no | 0 | 3.17 | 1,032,544 | 7.62 | ok | - |
| batch-100 | s0168 | `data/raw/totalseg_ct/s0168/segmentations` | 63,642 | 63,642 | no | 0 | 0.55 | 671,920 | 6.26 | ok | - |
| batch-100 | s0171 | `data/raw/totalseg_ct/s0171/segmentations` | 169,664 | 119,994 | yes | 29.28 | 0.55 | 1,265,996 | 13.87 | ok | - |
| batch-100 | s0175 | `data/raw/totalseg_ct/s0175/segmentations` | 129,108 | 119,992 | yes | 7.06 | 0.62 | 1,266,244 | 7.55 | ok | - |
| batch-100 | s0178 | `data/raw/totalseg_ct/s0178/segmentations` | 69,248 | 69,248 | no | 0 | 0.53 | 730,524 | 7.27 | ok | - |
| batch-100 | s0182 | `data/raw/totalseg_ct/s0182/segmentations` | 75,220 | 75,220 | no | 0 | n/a | 796,140 | 6.02 | ok | - |
| batch-100 | s0183 | `data/raw/totalseg_ct/s0183/segmentations` | 85,036 | 85,036 | no | 0 | 0.33 | 897,448 | 8.32 | ok | - |
| batch-100 | s0184 | `data/raw/totalseg_ct/s0184/segmentations` | 186,904 | 119,997 | yes | 35.8 | 0.57 | 1,266,360 | 11.05 | ok | - |
| batch-100 | s0189 | `data/raw/totalseg_ct/s0189/segmentations` | 157,852 | 119,996 | yes | 23.98 | 1.07 | 1,266,132 | 11.86 | ok | - |
| batch-100 | s0191 | `data/raw/totalseg_ct/s0191/segmentations` | 132,324 | 102,264 | yes | 22.72 | 0.41 | 1,080,860 | 5.5 | ok | - |
| batch-100 | s0194 | `data/raw/totalseg_ct/s0194/segmentations` | 55,346 | 55,346 | no | 0 | n/a | 586,424 | 4.74 | ok | - |
| batch-100 | s0196 | `data/raw/totalseg_ct/s0196/segmentations` | 178,128 | 119,996 | yes | 32.63 | 0.54 | 1,266,156 | 15.46 | ok | - |
| batch-100 | s0197 | `data/raw/totalseg_ct/s0197/segmentations` | 169,168 | 119,994 | yes | 29.07 | 0.55 | 1,265,968 | 11.15 | ok | - |
| batch-100 | s0199 | `data/raw/totalseg_ct/s0199/segmentations` | 65,542 | 65,542 | no | 0 | n/a | 697,284 | 5.96 | ok | - |
| batch-100 | s0201 | `data/raw/totalseg_ct/s0201/segmentations` | 48,238 | 48,238 | no | 0 | n/a | 511,400 | 7.79 | ok | - |
| batch-100 | s0204 | `data/raw/totalseg_ct/s0204/segmentations` | 72,784 | 72,784 | no | 0 | 0.51 | 767,368 | 7.95 | ok | - |
| batch-100 | s0206 | `data/raw/totalseg_ct/s0206/segmentations` | 65,092 | 65,092 | no | 0 | 0.19 | 689,856 | 5.74 | ok | - |
| batch-100 | s0209 | `data/raw/totalseg_ct/s0209/segmentations` | 149,434 | 109,404 | yes | 26.79 | 0.47 | 1,155,844 | 6.03 | ok | - |
| batch-100 | s0210 | `data/raw/totalseg_ct/s0210/segmentations` | 108,048 | 108,048 | yes | 0 | 0.23 | 1,143,744 | 10.9 | ok | - |
| batch-100 | s0212 | `data/raw/totalseg_ct/s0212/segmentations` | 120,192 | 120,000 | yes | 0.16 | 0.58 | 1,264,072 | 8.13 | ok | - |
| batch-100 | s0213 | `data/raw/totalseg_ct/s0213/segmentations` | 74,252 | 74,252 | no | 0 | 0.55 | 782,780 | 6 | ok | - |
| batch-100 | s0215 | `data/raw/totalseg_ct/s0215/segmentations` | 39,836 | 39,836 | no | 0 | n/a | 424,240 | 5.3 | ok | - |
| batch-100 | s0218 | `data/raw/totalseg_ct/s0218/segmentations` | 57,614 | 57,614 | no | 0 | n/a | 610,040 | 7.89 | ok | - |
| batch-100 | s0219 | `data/raw/totalseg_ct/s0219/segmentations` | 76,350 | 76,350 | no | 0 | 0.61 | 809,712 | 5.44 | ok | - |
| batch-100 | s0220 | `data/raw/totalseg_ct/s0220/segmentations` | 49,562 | 49,562 | no | 0 | 0.29 | 526,356 | 4.9 | ok | - |
| batch-100 | s0223 | `data/raw/totalseg_ct/s0223/segmentations` | 145,502 | 119,992 | yes | 17.53 | 0.54 | 1,266,312 | 7.8 | ok | - |
| batch-100 | s0224 | `data/raw/totalseg_ct/s0224/segmentations` | 183,912 | 102,258 | yes | 44.4 | 0.57 | 1,079,752 | 21.99 | ok | - |
| batch-200 | s0010 | `data/raw/totalseg_ct/s0010/segmentations` | 75,824 | 75,824 | no | 0 | 0.57 | 801,000 | 7.5 | ok | - |
| batch-200 | s0011 | `data/raw/totalseg_ct/s0011/segmentations` | 145,592 | 103,422 | yes | 28.96 | 1.01 | 1,092,084 | 21.09 | ok | - |
| batch-200 | s0012 | `data/raw/totalseg_ct/s0012/segmentations` | 97,386 | 97,386 | no | 0 | 0.25 | 1,028,624 | 8.48 | ok | - |
| batch-200 | s0013 | `data/raw/totalseg_ct/s0013/segmentations` | 54,806 | 54,806 | no | 0 | n/a | 580,584 | 6.59 | ok | - |
| batch-200 | s0014 | `data/raw/totalseg_ct/s0014/segmentations` | 82,190 | 82,190 | no | 0 | 0.23 | 871,032 | 9.48 | ok | - |
| batch-200 | s0016 | `data/raw/totalseg_ct/s0016/segmentations` | 69,588 | 69,588 | no | 0 | 0.16 | 738,320 | 10.66 | ok | - |
| batch-200 | s0019 | `data/raw/totalseg_ct/s0019/segmentations` | 159,288 | 119,998 | yes | 24.67 | 0.59 | 1,266,080 | 15.89 | ok | - |
| batch-200 | s0024 | `data/raw/totalseg_ct/s0024/segmentations` | 259,316 | 119,998 | yes | 53.73 | 0.57 | 1,266,096 | 34.64 | ok | - |
| batch-200 | s0028 | `data/raw/totalseg_ct/s0028/segmentations` | 133,412 | 101,518 | yes | 23.91 | 0.53 | 1,072,004 | 18 | ok | - |
| batch-200 | s0029 | `data/raw/totalseg_ct/s0029/segmentations` | 204,084 | 100,822 | yes | 50.6 | 0.55 | 1,064,720 | 22.8 | ok | - |
| batch-200 | s0030 | `data/raw/totalseg_ct/s0030/segmentations` | 111,408 | 111,408 | yes | 0 | 0.22 | 1,178,740 | 19.17 | ok | - |
| batch-200 | s0031 | `data/raw/totalseg_ct/s0031/segmentations` | 61,054 | 61,054 | no | 0 | 0.2 | 648,088 | 10.82 | ok | - |
| batch-200 | s0032 | `data/raw/totalseg_ct/s0032/segmentations` | 105,224 | 105,224 | yes | 0 | 0.97 | 1,111,148 | 12.45 | ok | - |
| batch-200 | s0037 | `data/raw/totalseg_ct/s0037/segmentations` | 141,720 | 119,991 | yes | 15.33 | 0.54 | 1,266,220 | 17.6 | ok | - |
| batch-200 | s0039 | `data/raw/totalseg_ct/s0039/segmentations` | 57,074 | 57,074 | no | 0 | 0.54 | 604,336 | 5.44 | ok | - |
| batch-200 | s0040 | `data/raw/totalseg_ct/s0040/segmentations` | 157,740 | 102,644 | yes | 34.93 | 0.56 | 1,083,888 | 16.81 | ok | - |
| batch-200 | s0042 | `data/raw/totalseg_ct/s0042/segmentations` | 60,146 | 60,146 | no | 0 | n/a | 637,436 | 12.42 | ok | - |
| batch-200 | s0045 | `data/raw/totalseg_ct/s0045/segmentations` | 98,798 | 98,798 | no | 0 | 0.26 | 1,045,492 | 13.91 | ok | - |
| batch-200 | s0046 | `data/raw/totalseg_ct/s0046/segmentations` | 161,090 | 119,997 | yes | 25.51 | 0.55 | 1,266,368 | 19.78 | ok | - |
| batch-200 | s0049 | `data/raw/totalseg_ct/s0049/segmentations` | 200,208 | 119,996 | yes | 40.06 | 0.58 | 1,266,084 | 19.93 | ok | - |
| batch-200 | s0050 | `data/raw/totalseg_ct/s0050/segmentations` | 134,520 | 119,994 | yes | 10.8 | 0.55 | 1,266,036 | 19.49 | ok | - |
| batch-200 | s0052 | `data/raw/totalseg_ct/s0052/segmentations` | 68,156 | 68,156 | no | 0 | n/a | 721,280 | 15.99 | ok | - |
| batch-200 | s0053 | `data/raw/totalseg_ct/s0053/segmentations` | 91,142 | 91,142 | no | 0 | 0.25 | 965,076 | 15.58 | ok | - |
| batch-200 | s0054 | `data/raw/totalseg_ct/s0054/segmentations` | 68,996 | 68,996 | no | 0 | 0.12 | 731,676 | 13.66 | ok | - |
| batch-200 | s0057 | `data/raw/totalseg_ct/s0057/segmentations` | 148,230 | 119,997 | yes | 19.05 | 0.14 | 1,267,052 | 18.16 | ok | - |
| batch-200 | s0058 | `data/raw/totalseg_ct/s0058/segmentations` | 101,090 | 101,090 | yes | 0 | 0.23 | 1,070,044 | 15.47 | ok | - |
| batch-200 | s0059 | `data/raw/totalseg_ct/s0059/segmentations` | 127,048 | 119,990 | yes | 5.55 | 0.58 | 1,266,072 | 18.01 | ok | - |
| batch-200 | s0065 | `data/raw/totalseg_ct/s0065/segmentations` | 197,260 | 119,994 | yes | 39.17 | 0.58 | 1,266,060 | 26.46 | ok | - |
| batch-200 | s0067 | `data/raw/totalseg_ct/s0067/segmentations` | 120,252 | 119,998 | yes | 0.21 | 0.49 | 1,266,020 | 12.59 | ok | - |
| batch-200 | s0070 | `data/raw/totalseg_ct/s0070/segmentations` | 122,448 | 119,996 | yes | 2 | 0.55 | 1,266,064 | 18.09 | ok | - |
| batch-200 | s0071 | `data/raw/totalseg_ct/s0071/segmentations` | 132,684 | 101,144 | yes | 23.77 | 0.52 | 1,068,120 | 17.62 | ok | - |
| batch-200 | s0072 | `data/raw/totalseg_ct/s0072/segmentations` | 71,040 | 71,040 | no | 0 | 0.26 | 752,220 | 12.18 | ok | - |
| batch-200 | s0075 | `data/raw/totalseg_ct/s0075/segmentations` | 93,586 | 93,586 | no | 0 | 0.22 | 990,624 | 10.63 | ok | - |
| batch-200 | s0076 | `data/raw/totalseg_ct/s0076/segmentations` | 185,808 | 119,998 | yes | 35.42 | 0.58 | 1,266,120 | 31.67 | ok | - |
| batch-200 | s0078 | `data/raw/totalseg_ct/s0078/segmentations` | 84,472 | 84,472 | no | 0 | 0.27 | 893,100 | 9.96 | ok | - |
| batch-200 | s0080 | `data/raw/totalseg_ct/s0080/segmentations` | 159,334 | 100,791 | yes | 36.74 | 0.54 | 1,064,640 | 20.51 | ok | - |
| batch-200 | s0082 | `data/raw/totalseg_ct/s0082/segmentations` | 63,376 | 63,376 | no | 0 | 0.25 | 671,296 | 10.42 | ok | - |
| batch-200 | s0083 | `data/raw/totalseg_ct/s0083/segmentations` | 125,306 | 119,995 | yes | 4.24 | 0.23 | 1,266,760 | 13.12 | ok | - |
| batch-200 | s0084 | `data/raw/totalseg_ct/s0084/segmentations` | 105,350 | 105,350 | yes | 0 | 0.29 | 1,113,896 | 9.47 | ok | - |
| batch-200 | s0085 | `data/raw/totalseg_ct/s0085/segmentations` | 108,184 | 108,184 | yes | 0 | 0.55 | 1,143,380 | 9.77 | ok | - |
| batch-200 | s0086 | `data/raw/totalseg_ct/s0086/segmentations` | 174,320 | 119,996 | yes | 31.16 | 0.55 | 1,266,076 | 36.45 | ok | - |
| batch-200 | s0088 | `data/raw/totalseg_ct/s0088/segmentations` | 92,634 | 92,634 | no | 0 | n/a | 982,824 | 11.05 | ok | - |
| batch-200 | s0091 | `data/raw/totalseg_ct/s0091/segmentations` | 209,164 | 119,994 | yes | 42.63 | 0.57 | 1,266,060 | 34.38 | ok | - |
| batch-200 | s0095 | `data/raw/totalseg_ct/s0095/segmentations` | 222,208 | 119,992 | yes | 46 | 0.58 | 1,266,076 | 27.16 | ok | - |
| batch-200 | s0096 | `data/raw/totalseg_ct/s0096/segmentations` | 94,834 | 94,834 | no | 0 | 0.18 | 1,004,408 | 11.99 | ok | - |
| batch-200 | s0102 | `data/raw/totalseg_ct/s0102/segmentations` | 157,824 | 119,994 | yes | 23.97 | 0.56 | 1,266,036 | 24.01 | ok | - |
| batch-200 | s0106 | `data/raw/totalseg_ct/s0106/segmentations` | 125,982 | 119,997 | yes | 4.75 | n/a | 1,267,428 | 14.85 | ok | - |
| batch-200 | s0107 | `data/raw/totalseg_ct/s0107/segmentations` | 97,192 | 97,192 | no | 0 | 0.24 | 1,027,188 | 21.27 | ok | - |
| batch-200 | s0109 | `data/raw/totalseg_ct/s0109/segmentations` | 93,280 | 93,280 | no | 0 | 100 | 983,448 | 13.83 | ok | - |
| batch-200 | s0111 | `data/raw/totalseg_ct/s0111/segmentations` | 216,272 | 119,995 | yes | 44.52 | 0.57 | 1,266,512 | 24.77 | ok | - |
| batch-200 | s0114 | `data/raw/totalseg_ct/s0114/segmentations` | 162,984 | 119,996 | yes | 26.38 | 0.55 | 1,266,092 | 17.11 | ok | - |
| batch-200 | s0115 | `data/raw/totalseg_ct/s0115/segmentations` | 189,992 | 101,457 | yes | 46.6 | 0.58 | 1,071,624 | 30.84 | ok | - |
| batch-200 | s0119 | `data/raw/totalseg_ct/s0119/segmentations` | 117,330 | 117,330 | yes | 0 | 0.28 | 1,241,448 | 18.4 | ok | - |
| batch-200 | s0120 | `data/raw/totalseg_ct/s0120/segmentations` | 78,932 | 78,932 | no | 0 | 94.31 | 832,880 | 9.99 | ok | - |
| batch-200 | s0123 | `data/raw/totalseg_ct/s0123/segmentations` | 150,700 | 119,996 | yes | 20.37 | 0.54 | 1,266,084 | 17.7 | ok | - |
| batch-200 | s0124 | `data/raw/totalseg_ct/s0124/segmentations` | 167,072 | 119,994 | yes | 28.18 | 0.54 | 1,266,032 | 24.5 | ok | - |
| batch-200 | s0128 | `data/raw/totalseg_ct/s0128/segmentations` | 46,494 | 46,494 | no | 0 | 0.51 | 492,736 | 12.73 | ok | - |
| batch-200 | s0133 | `data/raw/totalseg_ct/s0133/segmentations` | 71,230 | 71,230 | no | 0 | 0.24 | 754,696 | 19.39 | ok | - |
| batch-200 | s0135 | `data/raw/totalseg_ct/s0135/segmentations` | 59,900 | 59,900 | no | 0 | n/a | 635,240 | 10.98 | ok | - |
| batch-200 | s0137 | `data/raw/totalseg_ct/s0137/segmentations` | 71,278 | 71,278 | no | 0 | 0.55 | 752,100 | 11.62 | ok | - |
| batch-200 | s0138 | `data/raw/totalseg_ct/s0138/segmentations` | 161,400 | 119,991 | yes | 25.66 | 0.77 | 1,266,292 | 12.28 | ok | - |
| batch-200 | s0141 | `data/raw/totalseg_ct/s0141/segmentations` | 143,564 | 101,240 | yes | 29.48 | 0.52 | 1,069,148 | 21.67 | ok | - |
| batch-200 | s0145 | `data/raw/totalseg_ct/s0145/segmentations` | 216,984 | 119,994 | yes | 44.7 | 0.57 | 1,266,060 | 33.55 | ok | - |
| batch-200 | s0146 | `data/raw/totalseg_ct/s0146/segmentations` | 160,692 | 119,996 | yes | 25.32 | 0.55 | 1,266,100 | 23.91 | ok | - |
| batch-200 | s0147 | `data/raw/totalseg_ct/s0147/segmentations` | 85,066 | 85,066 | no | 0 | 0.26 | 897,652 | 14.51 | ok | - |
| batch-200 | s0150 | `data/raw/totalseg_ct/s0150/segmentations` | 89,510 | 89,510 | no | 0 | 0.56 | 944,668 | 9.94 | ok | - |
| batch-200 | s0153 | `data/raw/totalseg_ct/s0153/segmentations` | 82,514 | 82,514 | no | 0 | 0.33 | 870,136 | 18.95 | ok | - |
| batch-200 | s0156 | `data/raw/totalseg_ct/s0156/segmentations` | 61,698 | 61,698 | no | 0 | n/a | 653,464 | 9.18 | ok | - |
| batch-200 | s0158 | `data/raw/totalseg_ct/s0158/segmentations` | 62,998 | 62,998 | no | 0 | n/a | 666,672 | 10.35 | ok | - |
| batch-200 | s0160 | `data/raw/totalseg_ct/s0160/segmentations` | 66,494 | 66,494 | no | 0 | n/a | 706,060 | 14.41 | ok | - |
| batch-200 | s0162 | `data/raw/totalseg_ct/s0162/segmentations` | 151,952 | 119,996 | yes | 21.03 | 0.53 | 1,266,056 | 20.6 | ok | - |
| batch-200 | s0163 | `data/raw/totalseg_ct/s0163/segmentations` | 189,376 | 119,994 | yes | 36.64 | 0.56 | 1,266,024 | 31.09 | ok | - |
| batch-200 | s0165 | `data/raw/totalseg_ct/s0165/segmentations` | 56,930 | 56,930 | no | 0 | n/a | 605,452 | 10.52 | ok | - |
| batch-200 | s0166 | `data/raw/totalseg_ct/s0166/segmentations` | 97,756 | 97,756 | no | 0 | 3.17 | 1,032,544 | 11.93 | ok | - |
| batch-200 | s0168 | `data/raw/totalseg_ct/s0168/segmentations` | 63,642 | 63,642 | no | 0 | 0.55 | 671,920 | 10.62 | ok | - |
| batch-200 | s0171 | `data/raw/totalseg_ct/s0171/segmentations` | 169,664 | 119,994 | yes | 29.28 | 0.55 | 1,265,996 | 22.63 | ok | - |
| batch-200 | s0175 | `data/raw/totalseg_ct/s0175/segmentations` | 129,108 | 119,992 | yes | 7.06 | 0.62 | 1,266,244 | 14.42 | ok | - |
| batch-200 | s0178 | `data/raw/totalseg_ct/s0178/segmentations` | 69,248 | 69,248 | no | 0 | 0.53 | 730,524 | 13.55 | ok | - |
| batch-200 | s0182 | `data/raw/totalseg_ct/s0182/segmentations` | 75,220 | 75,220 | no | 0 | n/a | 796,140 | 13.42 | ok | - |
| batch-200 | s0183 | `data/raw/totalseg_ct/s0183/segmentations` | 85,036 | 85,036 | no | 0 | 0.33 | 897,448 | 12.77 | ok | - |
| batch-200 | s0184 | `data/raw/totalseg_ct/s0184/segmentations` | 186,904 | 119,997 | yes | 35.8 | 0.57 | 1,266,360 | 15.46 | ok | - |
| batch-200 | s0189 | `data/raw/totalseg_ct/s0189/segmentations` | 157,852 | 119,996 | yes | 23.98 | 1.07 | 1,266,132 | 17.55 | ok | - |
| batch-200 | s0191 | `data/raw/totalseg_ct/s0191/segmentations` | 132,324 | 102,264 | yes | 22.72 | 0.41 | 1,080,860 | 6.6 | ok | - |
| batch-200 | s0194 | `data/raw/totalseg_ct/s0194/segmentations` | 55,346 | 55,346 | no | 0 | n/a | 586,424 | 10.13 | ok | - |
| batch-200 | s0196 | `data/raw/totalseg_ct/s0196/segmentations` | 178,128 | 119,996 | yes | 32.63 | 0.54 | 1,266,156 | 21.66 | ok | - |
| batch-200 | s0197 | `data/raw/totalseg_ct/s0197/segmentations` | 169,168 | 119,994 | yes | 29.07 | 0.55 | 1,265,968 | 17.31 | ok | - |
| batch-200 | s0199 | `data/raw/totalseg_ct/s0199/segmentations` | 65,542 | 65,542 | no | 0 | n/a | 697,284 | 7.96 | ok | - |
| batch-200 | s0201 | `data/raw/totalseg_ct/s0201/segmentations` | 48,238 | 48,238 | no | 0 | n/a | 511,400 | 8.87 | ok | - |
| batch-200 | s0204 | `data/raw/totalseg_ct/s0204/segmentations` | 72,784 | 72,784 | no | 0 | 0.51 | 767,368 | 8.33 | ok | - |
| batch-200 | s0206 | `data/raw/totalseg_ct/s0206/segmentations` | 65,092 | 65,092 | no | 0 | 0.19 | 689,856 | 8.81 | ok | - |
| batch-200 | s0209 | `data/raw/totalseg_ct/s0209/segmentations` | 149,434 | 109,404 | yes | 26.79 | 0.47 | 1,155,844 | 8.27 | ok | - |
| batch-200 | s0210 | `data/raw/totalseg_ct/s0210/segmentations` | 108,048 | 108,048 | yes | 0 | 0.23 | 1,143,744 | 12.64 | ok | - |
| batch-200 | s0212 | `data/raw/totalseg_ct/s0212/segmentations` | 120,192 | 120,000 | yes | 0.16 | 0.58 | 1,264,072 | 9.78 | ok | - |
| batch-200 | s0213 | `data/raw/totalseg_ct/s0213/segmentations` | 74,252 | 74,252 | no | 0 | 0.55 | 782,780 | 6 | ok | - |
| batch-200 | s0215 | `data/raw/totalseg_ct/s0215/segmentations` | 39,836 | 39,836 | no | 0 | n/a | 424,240 | 4.95 | ok | - |
| batch-200 | s0218 | `data/raw/totalseg_ct/s0218/segmentations` | 57,614 | 57,614 | no | 0 | n/a | 610,040 | 8.35 | ok | - |
| batch-200 | s0219 | `data/raw/totalseg_ct/s0219/segmentations` | 76,350 | 76,350 | no | 0 | 0.61 | 809,712 | 5.5 | ok | - |
| batch-200 | s0220 | `data/raw/totalseg_ct/s0220/segmentations` | 49,562 | 49,562 | no | 0 | 0.29 | 526,356 | 6.47 | ok | - |
| batch-200 | s0223 | `data/raw/totalseg_ct/s0223/segmentations` | 145,502 | 119,992 | yes | 17.53 | 0.54 | 1,266,312 | 12.36 | ok | - |
| batch-200 | s0224 | `data/raw/totalseg_ct/s0224/segmentations` | 183,912 | 102,258 | yes | 44.4 | 0.57 | 1,079,752 | 23.55 | ok | - |
| batch-200 | s0227 | `data/raw/totalseg_ct/s0227/segmentations` | 54,122 | 54,122 | no | 0 | 0.54 | 571,912 | 10.07 | ok | - |
| batch-200 | s0230 | `data/raw/totalseg_ct/s0230/segmentations` | 172,354 | 119,995 | yes | 30.38 | 0.58 | 1,266,220 | 15.05 | ok | - |
| batch-200 | s0231 | `data/raw/totalseg_ct/s0231/segmentations` | 131,840 | 119,994 | yes | 8.98 | 0.5 | 1,266,096 | 11.69 | ok | - |
| batch-200 | s0232 | `data/raw/totalseg_ct/s0232/segmentations` | 66,254 | 66,254 | no | 0 | 0.14 | 702,512 | 9.28 | ok | - |
| batch-200 | s0235 | `data/raw/totalseg_ct/s0235/segmentations` | 49,618 | 49,618 | no | 0 | 0.16 | 526,276 | 5.32 | ok | - |
| batch-200 | s0236 | `data/raw/totalseg_ct/s0236/segmentations` | 98,108 | 98,108 | no | 0 | 3.29 | 1,035,296 | 7.06 | ok | - |
| batch-200 | s0238 | `data/raw/totalseg_ct/s0238/segmentations` | 48,554 | 48,554 | no | 0 | n/a | 514,788 | 5.5 | ok | - |
| batch-200 | s0239 | `data/raw/totalseg_ct/s0239/segmentations` | 222,768 | 119,994 | yes | 46.13 | 0.57 | 1,266,072 | 18.21 | ok | - |
| batch-200 | s0240 | `data/raw/totalseg_ct/s0240/segmentations` | 166,338 | 119,997 | yes | 27.86 | 0.56 | 1,266,416 | 11.89 | ok | - |
| batch-200 | s0241 | `data/raw/totalseg_ct/s0241/segmentations` | 125,626 | 119,994 | yes | 4.48 | 0.54 | 1,266,264 | 9.43 | ok | - |
| batch-200 | s0243 | `data/raw/totalseg_ct/s0243/segmentations` | 56,784 | 56,784 | no | 0 | n/a | 602,096 | 6.58 | ok | - |
| batch-200 | s0244 | `data/raw/totalseg_ct/s0244/segmentations` | 78,140 | 78,140 | no | 0 | 0.56 | 824,220 | 9.56 | ok | - |
| batch-200 | s0245 | `data/raw/totalseg_ct/s0245/segmentations` | 55,046 | 55,046 | no | 0 | n/a | 586,092 | 5.47 | ok | - |
| batch-200 | s0248 | `data/raw/totalseg_ct/s0248/segmentations` | 61,962 | 61,962 | no | 0 | n/a | 656,804 | 8.33 | ok | - |
| batch-200 | s0249 | `data/raw/totalseg_ct/s0249/segmentations` | 50,568 | 50,568 | no | 0 | n/a | 537,828 | 4.44 | ok | - |
| batch-200 | s0250 | `data/raw/totalseg_ct/s0250/segmentations` | 72,490 | 72,490 | no | 0 | n/a | 767,640 | 7.2 | ok | - |
| batch-200 | s0252 | `data/raw/totalseg_ct/s0252/segmentations` | 59,918 | 59,918 | no | 0 | 0.54 | 633,696 | 6.77 | ok | - |
| batch-200 | s0253 | `data/raw/totalseg_ct/s0253/segmentations` | 137,192 | 119,992 | yes | 12.54 | 0.55 | 1,266,140 | 12.66 | ok | - |
| batch-200 | s0255 | `data/raw/totalseg_ct/s0255/segmentations` | 83,224 | 83,224 | no | 0 | 0.56 | 878,984 | 7.3 | ok | - |
| batch-200 | s0256 | `data/raw/totalseg_ct/s0256/segmentations` | 55,304 | 55,304 | no | 0 | n/a | 585,760 | 4.2 | ok | - |
| batch-200 | s0258 | `data/raw/totalseg_ct/s0258/segmentations` | 61,052 | 61,052 | no | 0 | n/a | 648,556 | 5.18 | ok | - |
| batch-200 | s0260 | `data/raw/totalseg_ct/s0260/segmentations` | 66,192 | 66,192 | no | 0 | n/a | 701,004 | 11.56 | ok | - |
| batch-200 | s0264 | `data/raw/totalseg_ct/s0264/segmentations` | 52,702 | 52,702 | no | 0 | n/a | 558,512 | 5.48 | ok | - |
| batch-200 | s0266 | `data/raw/totalseg_ct/s0266/segmentations` | 49,410 | 49,410 | no | 0 | n/a | 524,396 | 6.86 | ok | - |
| batch-200 | s0270 | `data/raw/totalseg_ct/s0270/segmentations` | 130,322 | 119,992 | yes | 7.93 | 0.3 | 1,267,224 | 11.51 | ok | - |
| batch-200 | s0271 | `data/raw/totalseg_ct/s0271/segmentations` | 197,032 | 119,994 | yes | 39.1 | 0.54 | 1,266,344 | 15.95 | ok | - |
| batch-200 | s0275 | `data/raw/totalseg_ct/s0275/segmentations` | 72,376 | 72,376 | no | 0 | 0.53 | 763,616 | 10.84 | ok | - |
| batch-200 | s0285 | `data/raw/totalseg_ct/s0285/segmentations` | 143,606 | 119,993 | yes | 16.44 | 0.54 | 1,266,188 | 13.25 | ok | - |
| batch-200 | s0287 | `data/raw/totalseg_ct/s0287/segmentations` | 222,252 | 119,994 | yes | 46.01 | 0.56 | 1,266,100 | 31.73 | ok | - |
| batch-200 | s0290 | `data/raw/totalseg_ct/s0290/segmentations` | 73,366 | 73,366 | no | 0 | n/a | 777,548 | 8.71 | ok | - |
| batch-200 | s0293 | `data/raw/totalseg_ct/s0293/segmentations` | 85,984 | 85,984 | no | 0 | 0.22 | 910,096 | 7.99 | ok | - |
| batch-200 | s0294 | `data/raw/totalseg_ct/s0294/segmentations` | 68,446 | 68,446 | no | 0 | n/a | 726,216 | 5.67 | ok | - |
| batch-200 | s0299 | `data/raw/totalseg_ct/s0299/segmentations` | 116,010 | 116,010 | yes | 0 | 0.57 | 1,225,124 | 8.75 | ok | - |
| batch-200 | s0303 | `data/raw/totalseg_ct/s0303/segmentations` | 138,316 | 100,428 | yes | 27.39 | 0.53 | 1,060,608 | 10.5 | ok | - |
| batch-200 | s0305 | `data/raw/totalseg_ct/s0305/segmentations` | 64,000 | 64,000 | no | 0 | n/a | 680,560 | 5.86 | ok | - |
| batch-200 | s0308 | `data/raw/totalseg_ct/s0308/segmentations` | 167,800 | 101,456 | yes | 39.54 | 0.54 | 1,071,376 | 14.73 | ok | - |
| batch-200 | s0310 | `data/raw/totalseg_ct/s0310/segmentations` | 107,586 | 107,586 | yes | 0 | 0.57 | 1,136,100 | 7.28 | ok | - |
| batch-200 | s0311 | `data/raw/totalseg_ct/s0311/segmentations` | 84,048 | 84,048 | no | 0 | 0.55 | 885,640 | 9.04 | ok | - |
| batch-200 | s0315 | `data/raw/totalseg_ct/s0315/segmentations` | 188,682 | 103,310 | yes | 45.25 | 0.56 | 1,090,960 | 15.4 | ok | - |
| batch-200 | s0319 | `data/raw/totalseg_ct/s0319/segmentations` | 107,980 | 107,980 | yes | 0 | 0.55 | 1,137,864 | 9.43 | ok | - |
| batch-200 | s0321 | `data/raw/totalseg_ct/s0321/segmentations` | 77,872 | 77,872 | no | 0 | 0.26 | 824,516 | 7.93 | ok | - |
| batch-200 | s0322 | `data/raw/totalseg_ct/s0322/segmentations` | 150,834 | 119,995 | yes | 20.45 | 0.56 | 1,266,308 | 10.95 | ok | - |
| batch-200 | s0327 | `data/raw/totalseg_ct/s0327/segmentations` | 183,560 | 119,996 | yes | 34.63 | 0.56 | 1,266,080 | 11.77 | ok | - |
| batch-200 | s0329 | `data/raw/totalseg_ct/s0329/segmentations` | 76,140 | 76,140 | no | 0 | 0.24 | 806,056 | 9.13 | ok | - |
| batch-200 | s0331 | `data/raw/totalseg_ct/s0331/segmentations` | 148,648 | 119,993 | yes | 19.28 | 0.56 | 1,266,256 | 12 | ok | - |
| batch-200 | s0332 | `data/raw/totalseg_ct/s0332/segmentations` | 153,444 | 119,994 | yes | 21.8 | 0.55 | 1,266,064 | 16.02 | ok | - |
| batch-200 | s0334 | `data/raw/totalseg_ct/s0334/segmentations` | 167,168 | 101,002 | yes | 39.58 | 0.55 | 1,066,624 | 15.58 | ok | - |
| batch-200 | s0335 | `data/raw/totalseg_ct/s0335/segmentations` | 77,394 | 77,394 | no | 0 | n/a | 820,780 | 5.61 | ok | - |
| batch-200 | s0336 | `data/raw/totalseg_ct/s0336/segmentations` | 138,310 | 119,995 | yes | 13.24 | 0.56 | 1,266,304 | 8.81 | ok | - |
| batch-200 | s0338 | `data/raw/totalseg_ct/s0338/segmentations` | 107,870 | 107,870 | yes | 0 | 0.59 | 1,140,780 | 5.67 | ok | - |
| batch-200 | s0339 | `data/raw/totalseg_ct/s0339/segmentations` | 59,936 | 59,936 | no | 0 | 0.19 | 635,508 | 5.97 | ok | - |
| batch-200 | s0342 | `data/raw/totalseg_ct/s0342/segmentations` | 77,562 | 77,562 | no | 0 | 0.55 | 818,096 | 6.72 | ok | - |
| batch-200 | s0343 | `data/raw/totalseg_ct/s0343/segmentations` | 64,320 | 64,320 | no | 0 | n/a | 680,920 | 4.94 | ok | - |
| batch-200 | s0344 | `data/raw/totalseg_ct/s0344/segmentations` | 168,788 | 119,994 | yes | 28.91 | 0.56 | 1,266,064 | 14.78 | ok | - |
| batch-200 | s0345 | `data/raw/totalseg_ct/s0345/segmentations` | 182,464 | 119,996 | yes | 34.24 | 0.55 | 1,266,028 | 16.39 | ok | - |
| batch-200 | s0347 | `data/raw/totalseg_ct/s0347/segmentations` | 105,296 | 105,296 | yes | 0 | 0.51 | 1,112,112 | 8.61 | ok | - |
| batch-200 | s0349 | `data/raw/totalseg_ct/s0349/segmentations` | 223,812 | 119,998 | yes | 46.38 | 0.58 | 1,266,296 | 15.96 | ok | - |
| batch-200 | s0350 | `data/raw/totalseg_ct/s0350/segmentations` | 174,240 | 102,666 | yes | 41.08 | 0.54 | 1,084,120 | 13.87 | ok | - |
| batch-200 | s0354 | `data/raw/totalseg_ct/s0354/segmentations` | 152,840 | 100,297 | yes | 34.38 | 0.55 | 1,059,460 | 8.38 | ok | - |
| batch-200 | s0355 | `data/raw/totalseg_ct/s0355/segmentations` | 68,600 | 68,600 | no | 0 | n/a | 725,424 | 7.94 | ok | - |
| batch-200 | s0356 | `data/raw/totalseg_ct/s0356/segmentations` | 103,198 | 103,198 | yes | 0 | 0.37 | 1,091,784 | 5.05 | ok | - |
| batch-200 | s0357 | `data/raw/totalseg_ct/s0357/segmentations` | 143,506 | 119,991 | yes | 16.39 | 0.54 | 1,266,196 | 9.51 | ok | - |
| batch-200 | s0358 | `data/raw/totalseg_ct/s0358/segmentations` | 160,256 | 119,996 | yes | 25.12 | 0.49 | 1,266,420 | 13.97 | ok | - |
| batch-200 | s0362 | `data/raw/totalseg_ct/s0362/segmentations` | 185,396 | 100,284 | yes | 45.91 | 0.55 | 1,059,120 | 14.79 | ok | - |
| batch-200 | s0363 | `data/raw/totalseg_ct/s0363/segmentations` | 158,614 | 119,995 | yes | 24.35 | 0.54 | 1,266,352 | 10.7 | ok | - |
| batch-200 | s0364 | `data/raw/totalseg_ct/s0364/segmentations` | 125,104 | 119,996 | yes | 4.08 | 0.56 | 1,266,244 | 8.2 | ok | - |
| batch-200 | s0365 | `data/raw/totalseg_ct/s0365/segmentations` | 165,802 | 119,997 | yes | 27.63 | 0.57 | 1,266,252 | 11.58 | ok | - |
| batch-200 | s0367 | `data/raw/totalseg_ct/s0367/segmentations` | 161,970 | 119,993 | yes | 25.92 | 0.57 | 1,266,372 | 12.35 | ok | - |
| batch-200 | s0368 | `data/raw/totalseg_ct/s0368/segmentations` | 103,318 | 103,318 | yes | 0 | 0.41 | 1,094,272 | 7.3 | ok | - |
| batch-200 | s0369 | `data/raw/totalseg_ct/s0369/segmentations` | 175,968 | 100,408 | yes | 42.94 | 0.55 | 1,060,388 | 13.78 | ok | - |
| batch-200 | s0370 | `data/raw/totalseg_ct/s0370/segmentations` | 188,112 | 102,774 | yes | 45.37 | 0.55 | 1,085,212 | 17.53 | ok | - |
| batch-200 | s0371 | `data/raw/totalseg_ct/s0371/segmentations` | 134,072 | 102,064 | yes | 23.87 | 0.54 | 1,077,812 | 10.94 | ok | - |
| batch-200 | s0372 | `data/raw/totalseg_ct/s0372/segmentations` | 122,064 | 100,612 | yes | 17.57 | 0.53 | 1,062,548 | 10.61 | ok | - |
| batch-200 | s0373 | `data/raw/totalseg_ct/s0373/segmentations` | 148,410 | 119,995 | yes | 19.15 | 0.54 | 1,266,304 | 11.15 | ok | - |
| batch-200 | s0374 | `data/raw/totalseg_ct/s0374/segmentations` | 126,766 | 119,990 | yes | 5.34 | 0.5 | 1,266,172 | 9.87 | ok | - |
| batch-200 | s0375 | `data/raw/totalseg_ct/s0375/segmentations` | 138,804 | 119,998 | yes | 13.55 | 0.56 | 1,266,092 | 14.25 | ok | - |
| batch-200 | s0376 | `data/raw/totalseg_ct/s0376/segmentations` | 157,904 | 119,995 | yes | 24.01 | 0.53 | 1,266,332 | 10.56 | ok | - |
| batch-200 | s0377 | `data/raw/totalseg_ct/s0377/segmentations` | 144,988 | 119,992 | yes | 17.24 | 0.66 | 1,266,400 | 10.61 | ok | - |
| batch-200 | s0378 | `data/raw/totalseg_ct/s0378/segmentations` | 150,344 | 119,996 | yes | 20.19 | 0.56 | 1,266,308 | 9.04 | ok | - |
| batch-200 | s0380 | `data/raw/totalseg_ct/s0380/segmentations` | 142,292 | 119,993 | yes | 15.67 | 0.56 | 1,266,196 | 8.94 | ok | - |
| batch-200 | s0382 | `data/raw/totalseg_ct/s0382/segmentations` | 138,728 | 119,993 | yes | 13.51 | 1.02 | 1,267,040 | 9.3 | ok | - |
| batch-200 | s0383 | `data/raw/totalseg_ct/s0383/segmentations` | 158,188 | 119,992 | yes | 24.15 | 0.53 | 1,266,012 | 10.03 | ok | - |
| batch-200 | s0386 | `data/raw/totalseg_ct/s0386/segmentations` | 85,170 | 85,170 | no | 0 | 1.66 | 902,188 | 5.53 | ok | - |
| batch-200 | s0389 | `data/raw/totalseg_ct/s0389/segmentations` | 120,772 | 119,997 | yes | 0.64 | 0.54 | 1,266,564 | 6.79 | ok | - |
| batch-200 | s0390 | `data/raw/totalseg_ct/s0390/segmentations` | 79,588 | 79,588 | no | 0 | 0.21 | 844,532 | 7.31 | ok | - |
| batch-200 | s0391 | `data/raw/totalseg_ct/s0391/segmentations` | 111,390 | 111,390 | yes | 0 | 0.45 | 1,177,308 | 6.06 | ok | - |
| batch-200 | s0392 | `data/raw/totalseg_ct/s0392/segmentations` | 134,148 | 119,994 | yes | 10.55 | 0.54 | 1,266,116 | 9.82 | ok | - |
| batch-200 | s0393 | `data/raw/totalseg_ct/s0393/segmentations` | 155,806 | 119,994 | yes | 22.98 | 0.53 | 1,266,268 | 10.67 | ok | - |
| batch-200 | s0394 | `data/raw/totalseg_ct/s0394/segmentations` | 209,664 | 100,518 | yes | 52.06 | 0.58 | 1,061,752 | 12.05 | ok | - |
| batch-200 | s0398 | `data/raw/totalseg_ct/s0398/segmentations` | 153,078 | 119,994 | yes | 21.61 | 0.58 | 1,266,268 | 9.95 | ok | - |
| batch-200 | s0402 | `data/raw/totalseg_ct/s0402/segmentations` | 166,856 | 101,098 | yes | 39.41 | 0.54 | 1,067,616 | 11.87 | ok | - |
| batch-200 | s0403 | `data/raw/totalseg_ct/s0403/segmentations` | 188,984 | 119,996 | yes | 36.51 | 0.54 | 1,266,044 | 15.45 | ok | - |
| batch-200 | s0405 | `data/raw/totalseg_ct/s0405/segmentations` | 162,592 | 119,992 | yes | 26.2 | 0.55 | 1,266,312 | 9.48 | ok | - |
| batch-200 | s0406 | `data/raw/totalseg_ct/s0406/segmentations` | 124,576 | 119,997 | yes | 3.68 | 0.58 | 1,266,056 | 10.19 | ok | - |
| batch-200 | s0407 | `data/raw/totalseg_ct/s0407/segmentations` | 179,294 | 119,996 | yes | 33.07 | 0.55 | 1,266,336 | 10.51 | ok | - |
| batch-200 | s0408 | `data/raw/totalseg_ct/s0408/segmentations` | 168,616 | 119,996 | yes | 28.84 | 0.55 | 1,266,112 | 13.68 | ok | - |
| batch-200 | s0412 | `data/raw/totalseg_ct/s0412/segmentations` | 149,030 | 100,502 | yes | 32.56 | 0.56 | 1,061,612 | 11.7 | ok | - |
| batch-200 | s0413 | `data/raw/totalseg_ct/s0413/segmentations` | 157,424 | 119,997 | yes | 23.77 | 0.56 | 1,266,284 | 8.97 | ok | - |
| batch-200 | s0414 | `data/raw/totalseg_ct/s0414/segmentations` | 153,566 | 119,992 | yes | 21.86 | 0.56 | 1,266,264 | 9.05 | ok | - |
| batch-200 | s0416 | `data/raw/totalseg_ct/s0416/segmentations` | 63,912 | 63,912 | no | 0 | 0.22 | 675,920 | 5.55 | ok | - |

## Anomalies

none
