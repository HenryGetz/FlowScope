# FlowScope CFD overnight report

- generated: 2026-09-24T09:11:55.473673+00:00
- command: `tools/run_cfd_overnight.py --cases 601 700 798 --probe-base https://127.0.0.1:8543`
- runtime_s: 2312.45
- out_dir: out
- cases: 601 700 798
- profiles: A B C

## Case 601 — ok

| stage | status | exit code | runtime (s) | log | reason |
|---|---|---|---|---|---|
| graph | ok | 0 | 0.314 | out/cfd_logs/601_graph.log | — |
| hemodynamics | ok | 0 | 157.556 | out/cfd_logs/601_hemodynamics.log | — |
| carrier | ok | 0 | 7.433 | out/cfd_logs/601_carrier.log | — |
| advection | ok | 0 | 853.57 | out/cfd_logs/601_advection.log | — |
| export_A | ok | 0 | 2.296 | out/cfd_logs/601_export_A.log | — |
| export_B | ok | 0 | 2.805 | out/cfd_logs/601_export_B.log | — |
| export_C | ok | 0 | 2.089 | out/cfd_logs/601_export_C.log | — |

### mesh triangle count and branch count

- mesh triangle count: 100498
- branch count: 21 (nodes: 23, terminals: 8, roots: 2, total centerline length: 712.675 mm)

### 1D ROM solve duration (s) and mass balance conservation

- 1D ROM solve duration: 29.2564 s (1D transport: 126.493 s, total: 156.375 s)
- mass balance conservation [A]: rel_error 1.83851e-13, method: 1D finite-volume MUSCL-TVD (Superbee limiter) explicit advection + central diffusion (D=1e-3 mm^2/s), adaptive CFL<=0.5 sub-cycling, flow-weighted junction mixing, discrete face-flux mass budget
- mass balance conservation [B]: rel_error 1.50323e-13, method: 1D finite-volume MUSCL-TVD (Superbee limiter) explicit advection + central diffusion (D=1e-3 mm^2/s), adaptive CFL<=0.5 sub-cycling, flow-weighted junction mixing, discrete face-flux mass budget
- mass balance conservation [C]: rel_error 1.66262e-13, method: 1D finite-volume MUSCL-TVD (Superbee limiter) explicit advection + central diffusion (D=1e-3 mm^2/s), adaptive CFL<=0.5 sub-cycling, flow-weighted junction mixing, discrete face-flux mass budget

### 3D scalar transport solve duration (s) and maximum numerical residual

| profile | solve duration (s) | max numerical residual (eps_mass_max) | dt mean (s) | Pe | CFL | verdict |
|---|---|---|---|---|---|---|
| A | 278.636 | 3.78201e-15 | 0.000150884 | 1.56275e+06 | 0.45 | pass |
| B | 337.275 | 1.7288e-15 | 0.000196476 | 1.46507e+06 | 0.45 | pass |
| C | 236.574 | 5.06243e-15 | 0.00015011 | 1.56275e+06 | 0.45 | pass |

- total solve duration across profiles: 852.486 s
- maximum numerical residual across profiles: 5.06243e-15

### distal transit times (ms) under Profiles A, B, and C

| profile | branch | transit (ms) | arrival (ms) | peak (ms) | TIMI frames @30 fps |
|---|---|---|---|---|---|
| A | 601_b002 | 2016.25 | 1912.74 | 2782 | 60 |
| A | 601_b003 | 4465.36 | 3306.4 | 5098 | 134 |
| A | 601_b004 | 2138.2 | 2025.27 | 3796 | 64 |
| A | 601_b005 | 4527.75 | 3372.39 | 5184 | 136 |
| A | 601_b008 | 236.766 | 220.784 | 2044 | 7 |
| A | 601_b009 | 493.021 | 457.421 | 2048 | 15 |
| A | 601_b010 | 423.775 | 425.175 | 2044 | 13 |
| A | 601_b011 | 419.132 | 414.602 | 2066 | 13 |
| A | 601_b012 | 1608.61 | 1500.63 | 3126 | 48 |
| A | 601_b013 | 1098.79 | 951.607 | 2114 | 33 |
| A | 601_b015 | 3804.09 | 3706.7 | 5320 | 114 |
| A | 601_b016 | 489.649 | 484.962 | 2046 | 15 |
| A | 601_b017 | 585.612 | 522.506 | 2032 | 18 |
| A | 601_b018 | 1275.14 | 1256.06 | 2098 | 38 |
| A | 601_b019 | 699.35 | 585.946 | 2512 | 21 |
| A | 601_b020 | 904.841 | 801.905 | 2100 | 27 |
| B | 601_b002 | 2639.31 | 4141.43 | 5040 | 79 |
| B | 601_b003 | 5844.03 | 5466.76 | 6794 | 175 |
| B | 601_b004 | 2799.57 | 4261.16 | 5600 | 84 |
| B | 601_b005 | 5926.06 | 5532.41 | 6880 | 178 |
| B | 601_b008 | 309.44 | 579.648 | 3594 | 9 |
| B | 601_b009 | 642.062 | 1247.78 | 3796 | 19 |
| B | 601_b010 | 553.828 | 1125.39 | 3684 | 17 |
| B | 601_b011 | 548.452 | 1082.09 | 3656 | 16 |
| B | 601_b012 | 2085.82 | 3712.58 | 4898 | 63 |
| B | 601_b013 | 1434.9 | 2557.47 | 4052 | 43 |
| B | 601_b015 | 4948.97 | 5837.73 | 7024 | 148 |
| B | 601_b016 | 639.962 | 1283.63 | 3736 | 19 |
| B | 601_b017 | 765.587 | 1382.25 | 3720 | 23 |
| B | 601_b018 | 1668.59 | 3347.05 | 4380 | 50 |
| B | 601_b019 | 914.639 | 1556.18 | 4022 | 27 |
| B | 601_b020 | 1183.73 | 2122.48 | 3782 | 36 |
| C | 601_b002 | 2005.16 | 1912.68 | 3030 | 60 |
| C | 601_b003 | 4440.82 | 3198.4 | 4638 | 133 |
| C | 601_b004 | 2126.43 | 2023.59 | 3494 | 64 |
| C | 601_b005 | 4502.86 | 3264.41 | 4724 | 135 |
| C | 601_b008 | 235.473 | 220.753 | 490 | 7 |
| C | 601_b009 | 490.375 | 457.609 | 1164 | 15 |
| C | 601_b010 | 421.462 | 425.137 | 742 | 13 |
| C | 601_b011 | 416.83 | 414.491 | 928 | 13 |
| C | 601_b012 | 1600.16 | 1501.89 | 2770 | 48 |
| C | 601_b013 | 1092.82 | 951.801 | 1664 | 33 |
| C | 601_b015 | 3783.78 | 3636.48 | 4960 | 114 |
| C | 601_b016 | 486.976 | 484.928 | 824 | 15 |
| C | 601_b017 | 582.41 | 522.464 | 876 | 17 |
| C | 601_b018 | 1268.14 | 1256.07 | 1986 | 38 |
| C | 601_b019 | 695.52 | 585.909 | 1984 | 21 |
| C | 601_b020 | 899.879 | 801.835 | 1208 | 27 |


### final WebXR payload size (MB) and load-time benchmark

- final WebXR payload size (bin + metadata json): A: 4.5305 MB, B: 4.5305 MB, C: 4.5304 MB

- payload fetch benchmark over https://127.0.0.1:8543 (3 samples each; min / median / max ms):

| payload | bytes | ttfb | download | total |
|---|---|---|---|---|
| case_601_A_contrast.bin | 4522770 | 15.203 / 17.061 / 70.823 | 52.358 / 193.022 / 207.728 | 69.419 / 208.225 / 278.551 |
| case_601_A_metadata.json | 7754 | 16.12 / 17.92 / 46.559 | 0.19 / 0.222 / 0.396 | 16.311 / 18.142 / 46.955 |
| case_601_B_contrast.bin | 4522770 | 11.741 / 15.209 / 15.923 | 61.069 / 134.743 / 180.369 | 72.81 / 149.952 / 196.292 |
| case_601_B_metadata.json | 7715 | 10.212 / 14.076 / 14.915 | 0.223 / 0.24 / 0.268 | 10.48 / 14.316 / 15.138 |
| case_601_C_contrast.bin | 4522770 | 12.746 / 14.2 / 19.037 | 97.872 / 98.029 / 107.453 | 110.618 / 117.065 / 121.653 |
| case_601_C_metadata.json | 7677 | 11.023 / 13.075 / 14.107 | 0.197 / 0.221 / 0.24 | 11.263 / 13.296 / 14.304 |

- site load-time benchmark (node tools/loadtime_probe.mjs, 3 samples; min / median / max ms):

| resource | ttfb | download | total | bytes | ok samples |
|---|---|---|---|---|---|
| page_root | 17.604 / 18.333 / 66.717 | 0.769 / 1.142 / 3.774 | 18.372 / 19.474 / 70.491 | 1127 | 3 |
| page_model_coronary_601 | 14.986 / 23.514 / 26.116 | 0.404 / 0.813 / 1.831 | 15.39 / 25.346 / 26.929 | 1127 | 3 |
| coronary_601 | 14.982 / 18.826 / 19.691 | 25.52 / 27.93 / 28.202 | 40.502 / 46.756 / 47.893 | 1056532 | 3 |
| coronary_700 | 12.699 / 15.209 / 43.256 | 13.997 / 18.016 / 20.606 | 30.715 / 35.816 / 57.252 | 1056548 | 3 |
| coronary_798 | 8.688 / 13.387 / 16.884 | 21.744 / 25.155 / 25.923 | 34.611 / 38.542 / 38.628 | 1056460 | 3 |
| cardiac_s0004 | 6.59 / 7.203 / 12.862 | 17.327 / 17.494 / 20.138 | 24.084 / 24.531 / 33 | 1134044 | 3 |
| cardiac_s0015 | 7.786 / 8.958 / 9.024 | 15.885 / 18.815 / 31.01 | 24.908 / 26.601 / 39.969 | 1267532 | 3 |
| single_aorta | 9 / 22.842 / 48.418 | 7.593 / 15.213 / 22.966 | 30.436 / 31.966 / 63.631 | 456428 | 3 |


## Case 700 — ok

| stage | status | exit code | runtime (s) | log | reason |
|---|---|---|---|---|---|
| graph | ok | 0 | 0.31 | out/cfd_logs/700_graph.log | — |
| hemodynamics | ok | 0 | 113.374 | out/cfd_logs/700_hemodynamics.log | — |
| carrier | ok | 0 | 3.439 | out/cfd_logs/700_carrier.log | — |
| advection | ok | 0 | 317.561 | out/cfd_logs/700_advection.log | — |
| export_A | ok | 0 | 1.866 | out/cfd_logs/700_export_A.log | — |
| export_B | ok | 0 | 2.121 | out/cfd_logs/700_export_B.log | — |
| export_C | ok | 0 | 2.031 | out/cfd_logs/700_export_C.log | — |

### mesh triangle count and branch count

- mesh triangle count: 100500
- branch count: 16 (nodes: 18, terminals: 9, roots: 2, total centerline length: 508.96 mm)

### 1D ROM solve duration (s) and mass balance conservation

- 1D ROM solve duration: 22.6223 s (1D transport: 89.2485 s, total: 112.316 s)
- mass balance conservation [A]: rel_error 8.71978e-14, method: 1D finite-volume MUSCL-TVD (Superbee limiter) explicit advection + central diffusion (D=1e-3 mm^2/s), adaptive CFL<=0.5 sub-cycling, flow-weighted junction mixing, discrete face-flux mass budget
- mass balance conservation [B]: rel_error 1.01344e-13, method: 1D finite-volume MUSCL-TVD (Superbee limiter) explicit advection + central diffusion (D=1e-3 mm^2/s), adaptive CFL<=0.5 sub-cycling, flow-weighted junction mixing, discrete face-flux mass budget
- mass balance conservation [C]: rel_error 6.83637e-14, method: 1D finite-volume MUSCL-TVD (Superbee limiter) explicit advection + central diffusion (D=1e-3 mm^2/s), adaptive CFL<=0.5 sub-cycling, flow-weighted junction mixing, discrete face-flux mass budget

### 3D scalar transport solve duration (s) and maximum numerical residual

| profile | solve duration (s) | max numerical residual (eps_mass_max) | dt mean (s) | Pe | CFL | verdict |
|---|---|---|---|---|---|---|
| A | 114.844 | 8.58774e-15 | 0.000242285 | 973561 | 0.45 | pass |
| B | 92.0456 | 2.53395e-15 | 0.000315667 | 912713 | 0.45 | pass |
| C | 109.882 | 1.13762e-15 | 0.00024077 | 973561 | 0.45 | pass |

- total solve duration across profiles: 316.772 s
- maximum numerical residual across profiles: 8.58774e-15

### distal transit times (ms) under Profiles A, B, and C

| profile | branch | transit (ms) | arrival (ms) | peak (ms) | TIMI frames @30 fps |
|---|---|---|---|---|---|
| A | 700_b000 | 1505.22 | 1418.27 | 2466 | 45 |
| A | 700_b003 | 314.763 | 266.894 | 810 | 9 |
| A | 700_b004 | 1188.83 | 1107.85 | 2384 | 36 |
| A | 700_b006 | 1121.6 | 1042.33 | 2630 | 34 |
| A | 700_b007 | 962.607 | 858.928 | 2256 | 29 |
| A | 700_b008 | 1259.78 | 1175.39 | 2712 | 38 |
| A | 700_b009 | 1258.15 | 1156.16 | 2580 | 38 |
| A | 700_b010 | 1274.85 | 1183.61 | 2472 | 38 |
| A | 700_b011 | 320.149 | 296.758 | 2062 | 10 |
| A | 700_b012 | 478.083 | 473.59 | 866 | 14 |
| A | 700_b013 | 503.136 | 466.615 | 2042 | 15 |
| A | 700_b014 | 626.161 | 563.859 | 2064 | 19 |
| A | 700_b015 | 750.541 | 700.742 | 2064 | 23 |
| B | 700_b000 | 1976.11 | 3610.7 | 4644 | 59 |
| B | 700_b003 | 410.587 | 718.884 | 3662 | 12 |
| B | 700_b004 | 1554.79 | 2944.75 | 4340 | 47 |
| B | 700_b006 | 1464.33 | 2793 | 4250 | 44 |
| B | 700_b007 | 1257.02 | 2297.67 | 4086 | 38 |
| B | 700_b008 | 1647.78 | 3124.44 | 4454 | 49 |
| B | 700_b009 | 1645.64 | 3073.6 | 4276 | 49 |
| B | 700_b010 | 1667.51 | 3146.32 | 4472 | 50 |
| B | 700_b011 | 418.187 | 784.487 | 3628 | 13 |
| B | 700_b012 | 624.652 | 1255.09 | 3812 | 19 |
| B | 700_b013 | 657.529 | 1235.39 | 3738 | 20 |
| B | 700_b014 | 818.344 | 1497.28 | 3906 | 25 |
| B | 700_b015 | 981.043 | 1862.41 | 3988 | 29 |
| C | 700_b000 | 1496.82 | 1417.23 | 2392 | 45 |
| C | 700_b003 | 313.06 | 266.983 | 586 | 9 |
| C | 700_b004 | 1182.32 | 1107.06 | 1646 | 35 |
| C | 700_b006 | 1115.5 | 1041.94 | 1540 | 33 |
| C | 700_b007 | 957.373 | 858.334 | 1254 | 29 |
| C | 700_b008 | 1252.88 | 1174.48 | 1714 | 38 |
| C | 700_b009 | 1251.26 | 1155.3 | 1708 | 38 |
| C | 700_b010 | 1267.86 | 1182.64 | 1746 | 38 |
| C | 700_b011 | 318.406 | 296.709 | 534 | 10 |
| C | 700_b012 | 475.476 | 473.54 | 742 | 14 |
| C | 700_b013 | 500.391 | 466.56 | 742 | 15 |
| C | 700_b014 | 622.743 | 563.812 | 1436 | 19 |
| C | 700_b015 | 746.441 | 700.699 | 1008 | 22 |


### final WebXR payload size (MB) and load-time benchmark

- final WebXR payload size (bin + metadata json): A: 4.5295 MB, B: 4.5295 MB, C: 4.5295 MB

- payload fetch benchmark over https://127.0.0.1:8543 (3 samples each; min / median / max ms):

| payload | bytes | ttfb | download | total |
|---|---|---|---|---|
| case_700_A_contrast.bin | 4522860 | 10.329 / 10.745 / 22.862 | 45.955 / 71.436 / 115.486 | 56.284 / 82.181 / 138.349 |
| case_700_A_metadata.json | 6681 | 9.259 / 9.577 / 11.596 | 0.184 / 0.196 / 0.993 | 9.772 / 10.252 / 11.78 |
| case_700_B_contrast.bin | 4522860 | 21.058 / 21.666 / 22.308 | 111.32 / 120.74 / 158.719 | 132.986 / 141.798 / 181.027 |
| case_700_B_metadata.json | 6680 | 21.933 / 22.015 / 22.069 | 0.258 / 0.333 / 0.395 | 22.328 / 22.328 / 22.348 |
| case_700_C_contrast.bin | 4522860 | 13.077 / 13.138 / 20.874 | 61.088 / 81.026 / 117.751 | 74.226 / 94.104 / 138.625 |
| case_700_C_metadata.json | 6635 | 8.285 / 9.32 / 11.103 | 0.148 / 0.16 / 0.243 | 8.433 / 9.48 / 11.346 |

- site load-time benchmark (node tools/loadtime_probe.mjs, 3 samples; min / median / max ms):

| resource | ttfb | download | total | bytes | ok samples |
|---|---|---|---|---|---|
| page_root | 17.604 / 18.333 / 66.717 | 0.769 / 1.142 / 3.774 | 18.372 / 19.474 / 70.491 | 1127 | 3 |
| page_model_coronary_601 | 14.986 / 23.514 / 26.116 | 0.404 / 0.813 / 1.831 | 15.39 / 25.346 / 26.929 | 1127 | 3 |
| coronary_601 | 14.982 / 18.826 / 19.691 | 25.52 / 27.93 / 28.202 | 40.502 / 46.756 / 47.893 | 1056532 | 3 |
| coronary_700 | 12.699 / 15.209 / 43.256 | 13.997 / 18.016 / 20.606 | 30.715 / 35.816 / 57.252 | 1056548 | 3 |
| coronary_798 | 8.688 / 13.387 / 16.884 | 21.744 / 25.155 / 25.923 | 34.611 / 38.542 / 38.628 | 1056460 | 3 |
| cardiac_s0004 | 6.59 / 7.203 / 12.862 | 17.327 / 17.494 / 20.138 | 24.084 / 24.531 / 33 | 1134044 | 3 |
| cardiac_s0015 | 7.786 / 8.958 / 9.024 | 15.885 / 18.815 / 31.01 | 24.908 / 26.601 / 39.969 | 1267532 | 3 |
| single_aorta | 9 / 22.842 / 48.418 | 7.593 / 15.213 / 22.966 | 30.436 / 31.966 / 63.631 | 456428 | 3 |


## Case 798 — ok

| stage | status | exit code | runtime (s) | log | reason |
|---|---|---|---|---|---|
| graph | ok | 0 | 0.282 | out/cfd_logs/798_graph.log | — |
| hemodynamics | ok | 0 | 303.547 | out/cfd_logs/798_hemodynamics.log | — |
| carrier | ok | 0 | 7.519 | out/cfd_logs/798_carrier.log | — |
| advection | ok | 0 | 512.508 | out/cfd_logs/798_advection.log | — |
| export_A | ok | 0 | 2.251 | out/cfd_logs/798_export_A.log | — |
| export_B | ok | 0 | 2.037 | out/cfd_logs/798_export_B.log | — |
| export_C | ok | 0 | 2.114 | out/cfd_logs/798_export_C.log | — |

### mesh triangle count and branch count

- mesh triangle count: 100500
- branch count: 35 (nodes: 37, terminals: 13, roots: 2, total centerline length: 1053.69 mm)

### 1D ROM solve duration (s) and mass balance conservation

- 1D ROM solve duration: 39.511 s (1D transport: 262.192 s, total: 302.445 s)
- mass balance conservation [A]: rel_error 6.10498e-14, method: 1D finite-volume MUSCL-TVD (Superbee limiter) explicit advection + central diffusion (D=1e-3 mm^2/s), adaptive CFL<=0.5 sub-cycling, flow-weighted junction mixing, discrete face-flux mass budget
- mass balance conservation [B]: rel_error 6.01028e-10, method: 1D finite-volume MUSCL-TVD (Superbee limiter) explicit advection + central diffusion (D=1e-3 mm^2/s), adaptive CFL<=0.5 sub-cycling, flow-weighted junction mixing, discrete face-flux mass budget
- mass balance conservation [C]: rel_error 4.76727e-14, method: 1D finite-volume MUSCL-TVD (Superbee limiter) explicit advection + central diffusion (D=1e-3 mm^2/s), adaptive CFL<=0.5 sub-cycling, flow-weighted junction mixing, discrete face-flux mass budget

### 3D scalar transport solve duration (s) and maximum numerical residual

| profile | solve duration (s) | max numerical residual (eps_mass_max) | dt mean (s) | Pe | CFL | verdict |
|---|---|---|---|---|---|---|
| A | 164.436 | 2.81059e-15 | 0.00027894 | 723477 | 0.45 | pass |
| B | 151.68 | 3.20534e-15 | 0.000363567 | 678259 | 0.450001 | pass |
| C | 195.267 | 2.67006e-15 | 0.000277812 | 723477 | 0.45 | pass |

- total solve duration across profiles: 511.382 s
- maximum numerical residual across profiles: 3.20534e-15

### distal transit times (ms) under Profiles A, B, and C

| profile | branch | transit (ms) | arrival (ms) | peak (ms) | TIMI frames @30 fps |
|---|---|---|---|---|---|
| A | 798_b005 | 3733.18 | 3157.95 | 5288 | 112 |
| A | 798_b007 | 11559.6 | n/a | n/a | 347 |
| A | 798_b008 | 4786.59 | 4278.58 | 6370 | 144 |
| A | 798_b009 | 5944.9 | 5660.63 | 7584 | 178 |
| A | 798_b010 | 9283.1 | n/a | n/a | 278 |
| A | 798_b011 | 9950.19 | n/a | n/a | 299 |
| A | 798_b012 | 6584.67 | 6100.79 | 7986 | 198 |
| A | 798_b013 | 6115.19 | 5817.1 | 7784 | 183 |
| A | 798_b014 | 7316.33 | 7084.15 | 8000 | 219 |
| A | 798_b015 | 6639.61 | 6270.42 | 8000 | 199 |
| A | 798_b021 | 457.667 | 408.149 | 2038 | 14 |
| A | 798_b024 | 231.45 | 231.005 | 494 | 7 |
| A | 798_b025 | 8271.25 | n/a | n/a | 248 |
| A | 798_b026 | 634.504 | 626.785 | 1376 | 19 |
| A | 798_b027 | 521.986 | 536.996 | 2316 | 16 |
| A | 798_b028 | 478.001 | 407.385 | 770 | 14 |
| A | 798_b029 | 320.311 | 323.879 | 2016 | 10 |
| A | 798_b030 | 3094.71 | 2120.84 | 3834 | 93 |
| A | 798_b031 | 854.22 | 847.698 | 1430 | 26 |
| A | 798_b032 | 875.275 | 878.417 | 2662 | 26 |
| A | 798_b033 | 1664.67 | 1355.37 | 3398 | 50 |
| A | 798_b034 | 556.142 | 532.43 | 2140 | 17 |
| B | 798_b005 | 4878.34 | 5374.79 | 6926 | 146 |
| B | 798_b007 | 15156 | n/a | n/a | 455 |
| B | 798_b008 | 6259.15 | 6489.23 | 8006 | 188 |
| B | 798_b009 | 7800.42 | 7918.29 | 9226 | 234 |
| B | 798_b010 | 12175.1 | n/a | n/a | 365 |
| B | 798_b011 | 13052.4 | n/a | n/a | 392 |
| B | 798_b012 | 8644.25 | 8359.44 | 9500 | 259 |
| B | 798_b013 | 8024.86 | 8075.03 | 9326 | 241 |
| B | 798_b014 | 9608.68 | 9337.75 | 9500 | 288 |
| B | 798_b015 | 8716.66 | 8529.12 | 9500 | 261 |
| B | 798_b021 | 581.945 | 1429.99 | 3778 | 17 |
| B | 798_b024 | 302.756 | 604.386 | 3560 | 9 |
| B | 798_b025 | 10628.5 | 3522.37 | 3536 | 319 |
| B | 798_b026 | 827.78 | 1696.58 | 3656 | 25 |
| B | 798_b027 | 683.872 | 1393.98 | 3960 | 21 |
| B | 798_b028 | 627.112 | 1047.91 | 2854 | 19 |
| B | 798_b029 | 419.176 | 845.752 | 3630 | 13 |
| B | 798_b030 | 4047.67 | 4294.97 | 5484 | 121 |
| B | 798_b031 | 1116.99 | 2282.21 | 4088 | 34 |
| B | 798_b032 | 1148.34 | 2280.43 | 4364 | 34 |
| B | 798_b033 | 2184.23 | 3531.94 | 4930 | 66 |
| B | 798_b034 | 729.588 | 1383.4 | 3582 | 22 |
| C | 798_b005 | 3712.81 | 3086.97 | 4782 | 111 |
| C | 798_b007 | 11495.5 | n/a | n/a | 345 |
| C | 798_b008 | 4760.38 | 4206.39 | 5864 | 143 |
| C | 798_b009 | 5911.81 | 5594.11 | 7098 | 177 |
| C | 798_b010 | 9231.54 | 8486.7 | 9000 | 277 |
| C | 798_b011 | 9894.88 | n/a | n/a | 297 |
| C | 798_b012 | 6547.93 | 6034.13 | 7468 | 196 |
| C | 798_b013 | 6081.13 | 5750.55 | 7204 | 182 |
| C | 798_b014 | 7275.43 | 7017.42 | 8486 | 218 |
| C | 798_b015 | 6602.56 | 6203.75 | 7634 | 198 |
| C | 798_b021 | 455.51 | 409.514 | 1762 | 14 |
| C | 798_b024 | 230.181 | 230.95 | 1188 | 7 |
| C | 798_b025 | 8229.87 | 8178.27 | 9000 | 247 |
| C | 798_b026 | 631.07 | 626.919 | 1168 | 19 |
| C | 798_b027 | 519.102 | 536.788 | 1916 | 16 |
| C | 798_b028 | 475.342 | 407.175 | 1198 | 14 |
| C | 798_b029 | 318.551 | 323.776 | 1196 | 10 |
| C | 798_b030 | 3077.76 | 2079.35 | 3354 | 92 |
| C | 798_b031 | 849.545 | 847.818 | 1430 | 25 |
| C | 798_b032 | 870.406 | 878.035 | 2282 | 26 |
| C | 798_b033 | 1655.41 | 1355.1 | 2828 | 50 |
| C | 798_b034 | 553.05 | 532.226 | 1206 | 17 |


### final WebXR payload size (MB) and load-time benchmark

- final WebXR payload size (bin + metadata json): A: 4.5326 MB, B: 4.5326 MB, C: 4.5325 MB

- payload fetch benchmark over https://127.0.0.1:8543 (3 samples each; min / median / max ms):

| payload | bytes | ttfb | download | total |
|---|---|---|---|---|
| case_798_A_contrast.bin | 4521960 | 9.28 / 9.738 / 12.554 | 55.779 / 180.764 / 236.276 | 65.517 / 193.318 / 245.556 |
| case_798_A_metadata.json | 10682 | 12.603 / 13.72 / 16.671 | 0.205 / 0.222 / 0.223 | 12.826 / 13.925 / 16.892 |
| case_798_B_contrast.bin | 4521960 | 9.597 / 16.849 / 24.806 | 96.243 / 102.731 / 183.431 | 105.84 / 127.537 / 200.28 |
| case_798_B_metadata.json | 10614 | 7.349 / 7.613 / 7.934 | 0.181 / 0.181 / 0.187 | 7.536 / 7.794 / 8.115 |
| case_798_C_contrast.bin | 4521960 | 7.112 / 8.139 / 8.614 | 31.656 / 47.806 / 54.362 | 40.27 / 55.946 / 61.475 |
| case_798_C_metadata.json | 10588 | 8.386 / 11.29 / 13.181 | 0.174 / 0.262 / 0.322 | 8.56 / 11.612 / 13.443 |

- site load-time benchmark (node tools/loadtime_probe.mjs, 3 samples; min / median / max ms):

| resource | ttfb | download | total | bytes | ok samples |
|---|---|---|---|---|---|
| page_root | 17.604 / 18.333 / 66.717 | 0.769 / 1.142 / 3.774 | 18.372 / 19.474 / 70.491 | 1127 | 3 |
| page_model_coronary_601 | 14.986 / 23.514 / 26.116 | 0.404 / 0.813 / 1.831 | 15.39 / 25.346 / 26.929 | 1127 | 3 |
| coronary_601 | 14.982 / 18.826 / 19.691 | 25.52 / 27.93 / 28.202 | 40.502 / 46.756 / 47.893 | 1056532 | 3 |
| coronary_700 | 12.699 / 15.209 / 43.256 | 13.997 / 18.016 / 20.606 | 30.715 / 35.816 / 57.252 | 1056548 | 3 |
| coronary_798 | 8.688 / 13.387 / 16.884 | 21.744 / 25.155 / 25.923 | 34.611 / 38.542 / 38.628 | 1056460 | 3 |
| cardiac_s0004 | 6.59 / 7.203 / 12.862 | 17.327 / 17.494 / 20.138 | 24.084 / 24.531 / 33 | 1134044 | 3 |
| cardiac_s0015 | 7.786 / 8.958 / 9.024 | 15.885 / 18.815 / 31.01 | 24.908 / 26.601 / 39.969 | 1267532 | 3 |
| single_aorta | 9 / 22.842 / 48.418 | 7.593 / 15.213 / 22.966 | 30.436 / 31.966 / 63.631 | 456428 | 3 |


## failures

Mirrors `out/cfd_errors.log` (out/cfd_errors.log).

No failures recorded — `cfd_errors.log` was not created.
