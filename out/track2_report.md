# Track 2 — headless hardware pre-staging metrics

- Generated: 2026-09-23T11:46:39+00:00
- Tool: `tools/hw_precheck.py` v1.0.0
- Command: `/home/wavy/ai/flowscope/.venv/bin/python tools/hw_precheck.py`
- Load-time probe: `tools/loadtime_probe.mjs` v1.0.0 (`node /home/wavy/ai/flowscope/tools/loadtime_probe.mjs --base https://192.168.1.229:8444 --samples 3`)
- Endpoint: https://192.168.1.229:8444

## Scope and method

Everything in this document was measured headless on a workstation with no headset and no GPU:

- **static geometry/buffer accounting** parsed directly from the GLB binaries
  (triangles, vertices, index/vertex buffer bytes, quantization ratio vs float32),
- **draw-call estimates** for the three.js WebXR renderer (see Definitions),
- **scene combos** of simultaneously loaded models checked against the pipeline
  budget window [100,000, 150,000] triangles,
- **pure HTTP load timing** against the LAN HTTPS viewer endpoint
  (3 samples per resource, min/median/max; no browser involved).

Not measured here, by construction: rendering cost, GPU load, thermals, and frame rates.

## Target hardware assumptions (pre-staging only)

Stated as assumptions for planning — nothing has been measured on the device yet:

- **Device**: Meta Quest 2 standalone headset.
- **SoC**: Qualcomm Snapdragon XR2 with an Adreno 650-class tile-based GPU and
  unified CPU/GPU memory (no discrete VRAM budget — geometry and buffers compete
  with the browser runtime and textures).
- **Runtime**: headset browser (Chromium-class) with WebXR; secure context required,
  delivered over LAN Wi-Fi with a self-signed certificate the user accepts once.
- **Implications used above**: tile-based GPUs care about fill/overdraw — the extra
  BLEND pass per translucent structure matters more than raw triangle count;
  `KHR_mesh_quantization` buffers are decoded either natively or at load
  (the viewer dequantizes to float32 at load, trading memory for the robust path).

## Per-GLB summary

| GLB | structures | triangles | vertices | index bytes | vertex bytes | quantized vs f32 | draw calls est | buffer bytes | file bytes | load ms (median) |
|---|---|---|---|---|---|---|---|---|---|---|
| `out/coronary_601.glb` | 1 | 119,998 | 60,003 | 719,988 | 540,027 | 0.3750 | 1 | 1,260,015 | 1,261,272 | 88.9 |
| `out/coronary_700.glb` | 1 | 119,998 | 60,003 | 719,988 | 540,027 | 0.3750 | 1 | 1,260,015 | 1,261,268 | 90.6 |
| `out/coronary_798.glb` | 1 | 119,998 | 59,993 | 719,988 | 539,937 | 0.3750 | 1 | 1,259,925 | 1,261,176 | 55.7 |
| `out/cardiac_s0004.glb` | 6 | 103,274 | 51,647 | 619,644 | 464,823 | 0.3750 | 7 | 1,084,467 | 1,090,456 | 51.3 |
| `out/cardiac_s0015.glb` | 6 | 119,994 | 60,175 | 719,964 | 541,575 | 0.3750 | 7 | 1,261,539 | 1,267,532 | 65.9 |
| `out/single_aorta.glb` | 1 | 43,352 | 21,678 | 260,112 | 195,102 | 0.3750 | 1 | 455,214 | 456,428 | 27.7 |

`single_aorta` is a single-structure control model sitting below the budget-window
floor; the Phase-1 pipeline keeps raw aggregates below the floor as-is and reports
`within_window: false` (see `out/single_aorta.json`). It appears in no scene combo.

## Per-structure breakdown

| GLB | structure | triangles | vertices | index bytes | vertex bytes | quantized_ratio_vs_f32 | draw_calls_estimate | alphaMode |
|---|---|---|---|---|---|---|---|---|
| `out/coronary_601.glb` | coronary_arteries | 119,998 | 60,003 | 719,988 | 540,027 | 0.3750 | 1 | OPAQUE |
| `out/coronary_700.glb` | coronary_arteries | 119,998 | 60,003 | 719,988 | 540,027 | 0.3750 | 1 | OPAQUE |
| `out/coronary_798.glb` | coronary_arteries | 119,998 | 59,993 | 719,988 | 539,937 | 0.3750 | 1 | OPAQUE |
| `out/cardiac_s0004.glb` | aorta | 34,810 | 17,407 | 208,860 | 156,663 | 0.3750 | 1 | OPAQUE |
| `out/cardiac_s0004.glb` | heart_atrial_appendage_left | 3,574 | 1,789 | 21,444 | 16,101 | 0.3750 | 1 | OPAQUE |
| `out/cardiac_s0004.glb` | heart | 36,000 | 17,996 | 216,000 | 161,964 | 0.3750 | 2 | BLEND |
| `out/cardiac_s0004.glb` | vena_cava_inferior | 16,678 | 8,341 | 100,068 | 75,069 | 0.3750 | 1 | OPAQUE |
| `out/cardiac_s0004.glb` | pulmonary_veins | 7,550 | 3,781 | 45,300 | 34,029 | 0.3750 | 1 | OPAQUE |
| `out/cardiac_s0004.glb` | vena_cava_superior | 4,662 | 2,333 | 27,972 | 20,997 | 0.3750 | 1 | OPAQUE |
| `out/cardiac_s0015.glb` | aorta | 33,532 | 16,806 | 201,192 | 151,254 | 0.3750 | 1 | OPAQUE |
| `out/cardiac_s0015.glb` | heart_atrial_appendage_left | 1,551 | 797 | 9,306 | 7,173 | 0.3750 | 1 | OPAQUE |
| `out/cardiac_s0015.glb` | heart | 54,881 | 27,512 | 329,286 | 247,608 | 0.3750 | 2 | BLEND |
| `out/cardiac_s0015.glb` | vena_cava_inferior | 18,780 | 9,392 | 112,680 | 84,528 | 0.3750 | 1 | OPAQUE |
| `out/cardiac_s0015.glb` | pulmonary_veins | 9,272 | 4,663 | 55,632 | 41,967 | 0.3750 | 1 | OPAQUE |
| `out/cardiac_s0015.glb` | vena_cava_superior | 1,978 | 1,005 | 11,868 | 9,045 | 0.3750 | 1 | OPAQUE |
| `out/single_aorta.glb` | aorta | 43,352 | 21,678 | 260,112 | 195,102 | 0.3750 | 1 | OPAQUE |

## Scene combos (worst-case co-loading)

| combo | GLBs | total triangles | within budget | draw calls est | buffer bytes | flag |
|---|---|---|---|---|---|---|
| combo (a): cardiac_s0004 alone | `out/cardiac_s0004.glb` | 103,274 | yes | 7 | 1,084,467 | — |
| combo (b): cardiac_s0015 alone | `out/cardiac_s0015.glb` | 119,994 | yes | 7 | 1,261,539 | — |
| combo (c): coronary_601 alone | `out/coronary_601.glb` | 119,998 | yes | 1 | 1,260,015 | — |
| combo (d): cardiac_s0004 + coronary_601 loaded together (the brief's named worst case) | `out/cardiac_s0004.glb` + `out/coronary_601.glb` | 223,272 | NO | 8 | 2,344,482 | exceeds the 150,000 triangle scene ceiling by 73,272 triangles (48.8% over) — scene-level re-budgeting required before on-headset validation |
| combo (e): coronary_601 + coronary_700 + coronary_798 loaded together | `out/coronary_601.glb` + `out/coronary_700.glb` + `out/coronary_798.glb` | 359,994 | NO | 3 | 3,779,955 | exceeds the 150,000 triangle scene ceiling by 209,994 triangles (140.0% over) — scene-level re-budgeting required before on-headset validation |

`within_budget` = total triangles inside the pipeline window [100,000, 150,000].

### Re-budgeting for over-budget combos

**combo (d): cardiac_s0004 + coronary_601 loaded together (the brief's named worst case)** — 223,272 triangles, 73,272 over the ceiling. Recommended per-model split of 150,000:

| GLB | current triangles | share | recommended `--budget` |
|---|---|---|---|
| `out/cardiac_s0004.glb` | 103,274 | 46.25% | 69,000 |
| `out/coronary_601.glb` | 119,998 | 53.75% | 81,000 |

- Command: `re-run pipeline/build_cardiac_glb.py per model with --budget 69000 (for cardiac_s0004) and --budget 81000 (for coronary_601)`
- Caveat: build_cardiac_glb.py hard-clamps every output into its per-file window [100000, 150000] (WINDOW_LO/WINDOW_HI), so any per-model budget below the floor additionally requires parameterizing that window (or one combined run over the union of the combo structures with --budget 150000)

**combo (e): coronary_601 + coronary_700 + coronary_798 loaded together** — 359,994 triangles, 209,994 over the ceiling. Recommended per-model split of 150,000:

| GLB | current triangles | share | recommended `--budget` |
|---|---|---|---|
| `out/coronary_601.glb` | 119,998 | 33.33% | 50,000 |
| `out/coronary_700.glb` | 119,998 | 33.33% | 50,000 |
| `out/coronary_798.glb` | 119,998 | 33.33% | 50,000 |

- Command: `re-run pipeline/build_cardiac_glb.py per model with --budget 50000 (for coronary_601) and --budget 50000 (for coronary_700) and --budget 50000 (for coronary_798)`
- Caveat: build_cardiac_glb.py hard-clamps every output into its per-file window [100000, 150000] (WINDOW_LO/WINDOW_HI), so any per-model budget below the floor additionally requires parameterizing that window (or one combined run over the union of the combo structures with --budget 150000)

## LAN HTTPS load-time baseline (headless)

Probe: `tools/loadtime_probe.mjs` v1.0.0 (`node /home/wavy/ai/flowscope/tools/loadtime_probe.mjs --base https://192.168.1.229:8444 --samples 3`), 3 samples per resource over https://192.168.1.229:8444 — min / median / max ms.

Method: HTTP(S) GET via node:https/node:http (no browser, no renderer); fresh TLS connection per sample (cold-connection timing), connection: close; accept-encoding: identity (byte counts are uncompressed wire payload).

| resource | URL | kind | bytes | TTFB ms | download ms | total ms |
|---|---|---|---|---|---|---|
| page_root | `https://192.168.1.229:8444/` | page_html | 1,127 | 13.9 / 14.9 / 75.7 | 0.6 / 1.1 / 4.2 | 15.0 / 15.6 / 79.9 |
| page_model_coronary_601 | `https://192.168.1.229:8444/?model=assets/coronary_601.glb` | page_html | 1,127 | 12.8 / 13.0 / 14.7 | 0.8 / 2.1 / 2.2 | 13.8 / 14.9 / 16.9 |
| coronary_601 | `https://192.168.1.229:8444/assets/coronary_601.glb` | glb | 1,261,272 | 13.9 / 23.4 / 27.5 | 61.4 / 64.7 / 75.7 | 78.6 / 88.9 / 99.1 |
| coronary_700 | `https://192.168.1.229:8444/assets/coronary_700.glb` | glb | 1,261,268 | 23.8 / 31.8 / 52.1 | 49.2 / 59.1 / 66.8 | 81.0 / 90.6 / 111.2 |
| coronary_798 | `https://192.168.1.229:8444/assets/coronary_798.glb` | glb | 1,261,176 | 9.4 / 21.5 / 45.2 | 29.9 / 34.2 / 92.1 | 39.3 / 55.7 / 137.3 |
| cardiac_s0004 | `https://192.168.1.229:8444/assets/cardiac.glb` | glb | 1,090,456 | 12.3 / 12.6 / 14.0 | 35.1 / 39.1 / 48.1 | 49.1 / 51.3 / 60.7 |
| cardiac_s0015 | `https://192.168.1.229:8444/assets/cardiac_s0015.glb` | glb | 1,267,532 | 12.1 / 17.8 / 22.8 | 42.1 / 48.1 / 83.6 | 64.9 / 65.9 / 95.7 |
| single_aorta | `https://192.168.1.229:8444/assets/single_aorta.glb` | glb | 456,428 | 12.5 / 14.9 / 16.1 | 12.8 / 14.5 / 14.5 | 27.0 / 27.7 / 30.5 |

Load ms in the per-GLB table = median `total_ms` of the matching resource.

## Definitions

- `quantized_ratio_vs_f32` = stored vertex-attribute buffer bytes (POSITION + NORMAL as
  packed by `pipeline/glb_writer.py`) divided by the same attributes stored as float32.
  The quantized layout stores POSITION as normalized unsigned short (6 bytes/vertex) and
  NORMAL as normalized signed byte (3 bytes/vertex): 9 bytes/vertex vs 24 bytes/vertex
  float32, so every quantized structure reads 0.375 (a float32 GLB reads 1.0 by
  definition). The JSON variant `quantized_ratio_vs_f32_all_buffers` includes the
  index buffer in both terms.
- `draw_calls_estimate` = 1 pass per mesh primitive + 1 extra pass when the material
  `alphaMode` is `BLEND` (three.js renders double-sided transparent geometry in two
  passes: back faces then front faces; the pipeline emits `doubleSided: true`).
- `total_bytes` = sum of per-structure `index_bytes + vertex_bytes` (GLB BIN payload);
  `file_bytes` = on-disk GLB size including the JSON chunk.
- `load_ms` / load-time totals = request start to last body byte over a fresh TLS
  connection per sample (cold-connection timing).

## Pre-headset checklist (to be executed ON the Meta Quest 2)

This is what will be measured on-device; none of it is covered by this headless document:

- [ ] Pair the headset to the LAN, open the served URL in the headset browser, accept the
      self-signed certificate warning once (secure context required for WebXR).
- [ ] Confirm WebXR availability and enter an immersive session for every model target
      (`?model=...`) and for each scene combo (a)–(e).
- [ ] ON-headset: capture **FPS telemetry via the in-XR readout** while running each combo and verify against the brief target band 72-90 FPS (TARGET for the session, not a result - measurement pending)
      against the headset target display-refresh band and log the actuals — the only
      admissible source of frame-rate data.
- [ ] ON-headset: model load times per `?model=` target over the LAN Wi-Fi path, compared
      against the headless load-time baseline above.
- [ ] ON-headset: render-correctness pass — KHR_mesh_quantization decode, per-structure
      visibility toggles, and the two-pass BLEND/double-sided transparency of the
      translucent structures.
- [ ] ON-headset: draw-call behavior per combo vs the estimates above (device GPU counters
      where exposed).
- [ ] ON-headset: thermal + battery soak for the named worst-case combo (d).
- [ ] ON-headset: controller interaction and passthrough/guardian entry-exit.

frame rates NOT verified without headset telemetry
