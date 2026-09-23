# Track 4.2 - 0D/1D reduced-order network prototype on real vessel trees

Generated: 2026-09-23T12:23:09.242660+00:00

## Solver

- Backend (headline): svzerodsolver (pysvzerod, SimVascular svZeroDSolver)
- Import/probe: `importlib.util.find_spec('pysvzerod') -> found; import pysvzerod (pysvzerod==2.0, SimVascular/svZeroDSolver git 8a0c68e6)`
- Invocation: `python -c "import pysvzerod; s=pysvzerod.Solver(<solver_config.json>); s.run(); s.get_full_result().to_csv('solution.csv', index=False)"`
- CLI equivalent: `svzerodsolver solver_config.json solution.csv`
- version pysvzerod: 2.0
- version python: 3.12.14
- version numpy: 2.5.3
- version scipy: 1.18.1
- version pandas: 3.0.6
- openBF: not used: svZeroDSolver ran successfully (openBF would additionally require Julia, absent on this host)

## Commands

- extract_network: `python phase2/rom/extract_network.py --cases 601 700 798`
- run_zerod: `phase2/rom/run_zerod.py --cases 601 700 798 --solver both`
- analyze: `phase2/rom/analyze.py --cases 601 700 798 --backend svzerodsolver`

## Model summary

Centerline branches (extract_network.py) are discretized into ~10 mm 0D sub-segments (BloodVessel R/L/C elements). Inlet: idealized sinusoidal flow waveform, total 225 mL/min, split between tree inlets by root radius^3. Outlets: RCR Windkessel, distal conductance ~ r^3, per-tree scale calibrated to 90 mmHg mean inlet pressure at 5 mmHg venous pressure. Branch transit time (ms) = sum over its sub-segments of volume / cycle-mean flow (inlet-to-outlet plug-flow convective delay of that segment). Contrast arrival curves: idealized raised-cosine bolus (3-5 s) advected with the computed flow via exact plug-flow characteristics (integral Q dt = V per segment), flows extended periodically from the last cardiac cycle; arrival delays (t50/tpeak/centroid) are measured relative to the inlet bolus. `root_path_t50_ms` on a branch is the cumulative root-to-branch-end delay.

## Case 601

- segments (branches / sub-segments): 21 / 107, outlets: 8, inlets: 2, centerline: 1032 mm
- solver wall time: solve 1.265 s, total 8.058 s (svzerodsolver (pysvzerod, SimVascular svZeroDSolver))
- branch transit time (ms): min 31.1, mean 807.8, max 4290.6
- reference cross-check: mean-flow diff max 0.00% (mean 0.00%), sub-segment transit diff max 0.00 ms

### Branch transit times

| branch | parent | length (mm) | r_mean (mm) | mean flow (mL/min) | segment transit (ms) | root-path bolus t50 (ms) |
|---|---|---|---|---|---|---|
| 601_b000 | - | 226.0 | 1.44 | 36.86 | 2396.9 | 2420.0 |
| 601_b001 | 601_b000 | 143.5 | 1.30 | 14.32 | 3221.9 | 5660.0 |
| 601_b002 | 601_b000 | 37.9 | 1.55 | 22.54 | 798.5 | 3220.0 |
| 601_b003 | 601_b001 | 5.5 | 0.92 | 14.32 | 61.3 | 5720.0 |
| 601_b004 | 601_b002 | 13.5 | 1.05 | 22.54 | 124.2 | 3350.0 |
| 601_b005 | 601_b003 | 3.9 | 1.12 | 14.32 | 63.4 | 5790.0 |
| 601_b006 | - | 4.6 | 2.68 | 188.14 | 32.7 | 40.0 |
| 601_b007 | 601_b006 | 28.9 | 2.07 | 54.31 | 435.0 | 540.0 |
| 601_b008 | 601_b006 | 28.6 | 2.24 | 133.84 | 208.4 | 320.0 |
| 601_b009 | 601_b007 | 2.6 | 1.86 | 54.31 | 31.1 | 570.0 |
| 601_b010 | 601_b008 | 23.9 | 1.84 | 81.01 | 191.3 | 510.0 |
| 601_b011 | 601_b008 | 29.4 | 1.32 | 52.83 | 184.6 | 510.0 |
| 601_b012 | 601_b009 | 17.9 | 2.13 | 13.62 | 1128.6 | 1620.0 |
| 601_b013 | 601_b009 | 44.7 | 1.70 | 40.69 | 612.9 | 1190.0 |
| 601_b014 | 601_b010 | 3.2 | 2.16 | 81.01 | 35.2 | 540.0 |
| 601_b015 | 601_b012 | 124.5 | 1.55 | 13.62 | 4290.6 | 6000.0 |
| 601_b016 | 601_b014 | 2.5 | 2.01 | 58.43 | 32.3 | 560.0 |
| 601_b017 | 601_b014 | 5.5 | 1.68 | 22.58 | 128.9 | 640.0 |
| 601_b018 | 601_b016 | 260.4 | 1.66 | 55.84 | 2450.4 | 3000.0 |
| 601_b019 | 601_b016 | 2.4 | 1.11 | 2.59 | 212.2 | 730.0 |
| 601_b020 | 601_b017 | 22.8 | 1.30 | 22.58 | 323.1 | 950.0 |

### Outlet contrast arrival

| outlet | r (mm) | arrival t50 (ms) | t_peak (ms) | centroid (ms) | transit V/Q (ms) | peak conc |
|---|---|---|---|---|---|---|
| out_000 | 1.25 | 3350.0 | 4310.0 | 3311.3 | 3319.6 | 1.000 |
| out_001 | 1.08 | 5790.0 | 6730.0 | 5731.1 | 5743.5 | 1.000 |
| out_002 | 1.37 | 510.0 | 1410.0 | 402.0 | 425.8 | 1.000 |
| out_003 | 1.25 | 1190.0 | 2100.0 | 1091.5 | 1111.7 | 1.000 |
| out_004 | 0.87 | 6000.0 | 6850.0 | 5907.6 | 5918.0 | 1.000 |
| out_005 | 1.40 | 3000.0 | 3970.0 | 2928.2 | 2950.4 | 1.000 |
| out_006 | 0.50 | 730.0 | 1750.0 | 694.7 | 712.2 | 1.000 |
| out_007 | 1.03 | 950.0 | 1920.0 | 911.2 | 919.8 | 1.000 |

## Case 700

- segments (branches / sub-segments): 16 / 70, outlets: 9, inlets: 2, centerline: 659 mm
- solver wall time: solve 1.391 s, total 5.437 s (svzerodsolver (pysvzerod, SimVascular svZeroDSolver))
- branch transit time (ms): min 6.9, mean 424.4, max 2758.4
- reference cross-check: mean-flow diff max 0.00% (mean 0.00%), sub-segment transit diff max 0.00 ms

### Branch transit times

| branch | parent | length (mm) | r_mean (mm) | mean flow (mL/min) | segment transit (ms) | root-path bolus t50 (ms) |
|---|---|---|---|---|---|---|
| 700_b000 | - | 285.3 | 1.67 | 55.46 | 2758.4 | 2830.0 |
| 700_b001 | - | 8.9 | 2.48 | 169.54 | 60.9 | 70.0 |
| 700_b002 | 700_b001 | 33.9 | 2.00 | 106.95 | 242.1 | 400.0 |
| 700_b003 | 700_b001 | 23.4 | 1.90 | 62.59 | 259.0 | 410.0 |
| 700_b004 | 700_b002 | 129.2 | 1.31 | 44.75 | 964.4 | 1340.0 |
| 700_b005 | 700_b002 | 1.8 | 1.12 | 62.21 | 6.9 | 400.0 |
| 700_b006 | 700_b003 | 45.7 | 1.57 | 26.05 | 820.1 | 1220.0 |
| 700_b007 | 700_b003 | 49.3 | 1.60 | 36.54 | 658.4 | 1030.0 |
| 700_b008 | 700_b004 | 6.6 | 1.32 | 29.86 | 72.3 | 1400.0 |
| 700_b009 | 700_b004 | 2.7 | 0.96 | 6.63 | 70.7 | 1390.0 |
| 700_b010 | 700_b004 | 3.1 | 1.11 | 8.27 | 87.7 | 1410.0 |
| 700_b011 | 700_b005 | 4.2 | 1.10 | 62.21 | 15.7 | 420.0 |
| 700_b012 | 700_b011 | 18.7 | 1.15 | 29.12 | 160.3 | 560.0 |
| 700_b013 | 700_b011 | 19.4 | 1.30 | 33.09 | 185.9 | 580.0 |
| 700_b014 | 700_b012 | 2.5 | 0.69 | 1.50 | 150.4 | 680.0 |
| 700_b015 | 700_b012 | 23.7 | 1.31 | 27.61 | 276.6 | 780.0 |

### Outlet contrast arrival

| outlet | r (mm) | arrival t50 (ms) | t_peak (ms) | centroid (ms) | transit V/Q (ms) | peak conc |
|---|---|---|---|---|---|---|
| out_000 | 1.19 | 2830.0 | 3740.0 | 2741.1 | 2758.4 | 1.000 |
| out_001 | 1.29 | 1220.0 | 2110.0 | 1119.7 | 1140.0 | 1.000 |
| out_002 | 1.45 | 1030.0 | 1950.0 | 968.8 | 978.3 | 1.000 |
| out_003 | 1.37 | 1400.0 | 2370.0 | 1314.4 | 1339.7 | 1.000 |
| out_004 | 0.83 | 1390.0 | 2370.0 | 1310.1 | 1338.1 | 1.000 |
| out_005 | 0.89 | 1410.0 | 2390.0 | 1328.0 | 1355.1 | 1.000 |
| out_006 | 1.40 | 580.0 | 1530.0 | 482.1 | 511.4 | 1.000 |
| out_007 | 0.50 | 680.0 | 1670.0 | 615.1 | 636.2 | 1.000 |
| out_008 | 1.32 | 780.0 | 1770.0 | 756.1 | 762.4 | 1.000 |

## Case 798

- segments (branches / sub-segments): 35 / 112, outlets: 13, inlets: 2, centerline: 1071 mm
- solver wall time: solve 1.577 s, total 5.859 s (svzerodsolver (pysvzerod, SimVascular svZeroDSolver))
- branch transit time (ms): min 22.7, mean 1303.0, max 8182.1
- reference cross-check: mean-flow diff max 0.07% (mean 0.02%), sub-segment transit diff max 0.60 ms

### Branch transit times

| branch | parent | length (mm) | r_mean (mm) | mean flow (mL/min) | segment transit (ms) | root-path bolus t50 (ms) |
|---|---|---|---|---|---|---|
| 798_b000 | - | 7.9 | 2.06 | 32.67 | 193.1 | 260.0 |
| 798_b001 | 798_b000 | 8.5 | 1.77 | 32.67 | 153.9 | 440.0 |
| 798_b002 | 798_b001 | 5.1 | 1.93 | 32.67 | 109.3 | 530.0 |
| 798_b003 | 798_b002 | 22.4 | 2.17 | 32.67 | 607.9 | 1150.0 |
| 798_b004 | 798_b003 | 5.6 | 2.18 | 32.66 | 154.3 | 1300.0 |
| 798_b005 | 798_b004 | 64.6 | 2.15 | 21.81 | 2576.1 | 3840.0 |
| 798_b006 | 798_b004 | 7.1 | 2.06 | 10.86 | 525.6 | 1800.0 |
| 798_b007 | 798_b005 | 110.9 | 1.47 | 5.57 | 8182.1 | 12000.0 |
| 798_b008 | 798_b005 | 19.1 | 2.20 | 16.23 | 1070.9 | 4890.0 |
| 798_b009 | 798_b006 | 74.8 | 1.81 | 10.85 | 4311.1 | 6120.0 |
| 798_b010 | 798_b008 | 50.8 | 1.54 | 4.95 | 4580.8 | 9490.0 |
| 798_b011 | 798_b008 | 101.3 | 1.87 | 11.27 | 6092.1 | 11000.0 |
| 798_b012 | 798_b009 | 4.2 | 1.08 | 1.41 | 652.6 | 6770.0 |
| 798_b013 | 798_b009 | 3.1 | 1.67 | 9.44 | 173.6 | 6280.0 |
| 798_b014 | 798_b013 | 20.1 | 1.55 | 7.47 | 1224.5 | 7520.0 |
| 798_b015 | 798_b013 | 3.6 | 1.24 | 1.97 | 534.9 | 6830.0 |
| 798_b016 | - | 12.8 | 2.63 | 192.33 | 87.2 | 110.0 |
| 798_b017 | 798_b016 | 4.8 | 1.79 | 66.79 | 43.1 | 170.0 |
| 798_b018 | 798_b016 | 5.5 | 1.87 | 125.54 | 29.3 | 150.0 |
| 798_b019 | 798_b017 | 3.4 | 1.54 | 66.79 | 22.7 | 200.0 |
| 798_b020 | 798_b018 | 5.1 | 1.97 | 117.06 | 31.9 | 190.0 |
| 798_b021 | 798_b018 | 4.9 | 1.79 | 8.49 | 346.6 | 540.0 |
| 798_b022 | 798_b019 | 8.2 | 2.06 | 60.64 | 107.5 | 350.0 |
| 798_b023 | 798_b019 | 1.9 | 1.10 | 6.15 | 68.7 | 300.0 |
| 798_b024 | 798_b020 | 13.7 | 1.99 | 117.06 | 87.1 | 310.0 |
| 798_b025 | 798_b021 | 75.5 | 2.14 | 8.49 | 7907.4 | 8430.0 |
| 798_b026 | 798_b022 | 34.5 | 1.89 | 60.64 | 384.6 | 670.0 |
| 798_b027 | 798_b023 | 7.6 | 1.15 | 6.15 | 308.0 | 590.0 |
| 798_b028 | 798_b024 | 13.3 | 1.58 | 25.15 | 250.2 | 560.0 |
| 798_b029 | 798_b024 | 14.9 | 1.72 | 91.91 | 90.5 | 410.0 |
| 798_b030 | 798_b026 | 179.3 | 1.62 | 35.54 | 2505.8 | 3180.0 |
| 798_b031 | 798_b026 | 17.3 | 1.31 | 25.10 | 223.1 | 880.0 |
| 798_b032 | 798_b027 | 7.9 | 1.22 | 6.15 | 358.3 | 920.0 |
| 798_b033 | 798_b029 | 134.0 | 1.82 | 63.41 | 1370.6 | 1710.0 |
| 798_b034 | 798_b029 | 17.5 | 1.44 | 28.50 | 239.6 | 620.0 |

### Outlet contrast arrival

| outlet | r (mm) | arrival t50 (ms) | t_peak (ms) | centroid (ms) | transit V/Q (ms) | peak conc |
|---|---|---|---|---|---|---|
| out_000 | 1.42 | 12000.0 | 12980.0 | 11969.7 | 11976.7 | 1.000 |
| out_001 | 1.37 | 9490.0 | 10460.0 | 9429.1 | 9446.3 | 1.000 |
| out_002 | 1.80 | 11000.0 | 11960.0 | 10943.8 | 10957.5 | 1.000 |
| out_003 | 0.90 | 6770.0 | 7700.0 | 6691.4 | 6707.9 | 1.000 |
| out_004 | 1.57 | 7520.0 | 8440.0 | 7438.4 | 7453.3 | 1.000 |
| out_005 | 1.00 | 6830.0 | 7760.0 | 6745.1 | 6763.8 | 1.000 |
| out_006 | 1.00 | 8430.0 | 9470.0 | 8342.7 | 8370.4 | 1.000 |
| out_007 | 1.44 | 560.0 | 1490.0 | 461.5 | 485.6 | 1.000 |
| out_008 | 1.62 | 3180.0 | 4160.0 | 3138.1 | 3150.9 | 1.000 |
| out_009 | 1.44 | 880.0 | 1870.0 | 861.8 | 868.3 | 1.000 |
| out_010 | 0.90 | 920.0 | 1870.0 | 885.2 | 888.0 | 1.000 |
| out_011 | 1.97 | 1710.0 | 2680.0 | 1692.8 | 1696.5 | 1.000 |
| out_012 | 1.50 | 620.0 | 1580.0 | 541.0 | 565.4 | 1.000 |

## Notes and limitations

- headline numbers come from svZeroDSolver (pysvzerod); the `reference` backend is an engineering cross-check only
- network extraction is a prototype (skeleton + distance transform), not a commercial centerline tool; see network JSON `limits`
- flows at sub-segment inlet DOFs are used for transport; compressibility storage between inlet/outlet ports is neglected in the transport model
- arrival curves use pure advection (plug flow): no dispersion, no mixing beyond the bolus shape
- tiny/negative instantaneous flow dips are clamped to 1e-12 m^3/s in the transport map
- no headset/frame-rate or clinical claims are made anywhere in this report
- figure: out/rom/figs/601_arrival_transit.png
- figure: out/rom/figs/700_arrival_transit.png
- figure: out/rom/figs/798_arrival_transit.png
