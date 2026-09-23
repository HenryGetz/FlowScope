#!/usr/bin/env python3
"""Headless hardware pre-staging measurements (Track 2).

Parses the Phase-1 GLB corpus (KHR_mesh_quantization, ``pipeline/glb_writer.py``
layout) and reports static per-structure geometry/buffer accounting, draw-call
estimates for the three.js WebXR renderer, worst-case scene combos against the
pipeline budget window, and (via ``tools/loadtime_probe.mjs``) pure HTTP load
timing against the LAN HTTPS viewer endpoint.

This tool performs NO rendering and NO frame-rate measurement of any kind; the
host has no headset and no GPU. Frame rates require on-headset telemetry:
"frame rates NOT verified without headset telemetry".

Outputs (self-describing, per shared contract 3):
  out/track2_metrics.json   machine-readable metrics
  out/track2_report.md      tables + pre-headset checklist

Usage:
  .venv/bin/python tools/hw_precheck.py
      [--json out/track2_metrics.json] [--report out/track2_report.md]
      [--endpoint https://192.168.1.229:8444] [--samples 3] [--skip-loadtime]
"""

from __future__ import annotations

import argparse
import json
import struct
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

TOOL_NAME = 'tools/hw_precheck.py'
TOOL_VERSION = '1.0.0'
PROBE_NAME = 'tools/loadtime_probe.mjs'

REPO = Path(__file__).resolve().parent.parent

# The pipeline budget window (pipeline/build_cardiac_glb.py WINDOW_LO/WINDOW_HI):
# single-model triangle targets land in [100000, 150000]; scene combos are
# checked against the same window.
BUDGET_WINDOW_LO = 100000
BUDGET_WINDOW_HI = 150000

# The deliverable GLB set (per the brief's enumeration). Other out/*.glb probe
# artifacts (stl_probe, cardiac_s0004_quadric, single_aorta_f32) are out of
# scope. `served_url` is the asset path the LAN HTTPS viewer serves.
GLBS = [
    {'name': 'coronary_601', 'path': 'out/coronary_601.glb', 'served_url': '/assets/coronary_601.glb'},
    {'name': 'coronary_700', 'path': 'out/coronary_700.glb', 'served_url': '/assets/coronary_700.glb'},
    {'name': 'coronary_798', 'path': 'out/coronary_798.glb', 'served_url': '/assets/coronary_798.glb'},
    {
        'name': 'cardiac_s0004',
        'path': 'out/cardiac_s0004.glb',
        'served_url': '/assets/cardiac.glb',
        'note': 'served under the viewer default asset name assets/cardiac.glb; '
                'byte-identical to out/cardiac_s0004.glb (md5 384cc1145161ce0f9f69967d358df0b8)',
    },
    {'name': 'cardiac_s0015', 'path': 'out/cardiac_s0015.glb', 'served_url': '/assets/cardiac_s0015.glb'},
    {'name': 'single_aorta', 'path': 'out/single_aorta.glb', 'served_url': '/assets/single_aorta.glb'},
]

# Worst-case scene combos from the brief. (d) is the named worst case.
COMBOS = [
    ('solo_cardiac_s0004', 'combo (a): cardiac_s0004 alone', ['cardiac_s0004']),
    ('solo_cardiac_s0015', 'combo (b): cardiac_s0015 alone', ['cardiac_s0015']),
    ('solo_coronary_601', 'combo (c): coronary_601 alone', ['coronary_601']),
    (
        'worst_case_cardiac_s0004+coronary_601',
        "combo (d): cardiac_s0004 + coronary_601 loaded together (the brief's named worst case)",
        ['cardiac_s0004', 'coronary_601'],
    ),
    (
        'all_coronary_cases',
        'combo (e): coronary_601 + coronary_700 + coronary_798 loaded together',
        ['coronary_601', 'coronary_700', 'coronary_798'],
    ),
]

COMPONENT_BYTES = {5120: 1, 5121: 1, 5122: 2, 5123: 2, 5125: 4, 5126: 4}
TYPE_COMPONENTS = {'SCALAR': 1, 'VEC2': 2, 'VEC3': 3, 'VEC4': 4, 'MAT2': 4, 'MAT3': 9, 'MAT4': 16}

_GLTF_CHUNK = 0x4E4F534A  # 'JSON'
_BIN_CHUNK = 0x004E4942   # 'BIN\0'


# --------------------------------------------------------------------------- #
# GLB parsing (stdlib only; the writer layout is pipeline/glb_writer.py)
# --------------------------------------------------------------------------- #

def _accessor_stored_bytes(accessor: dict) -> int:
    comps = TYPE_COMPONENTS[accessor['type']]
    return int(accessor['count']) * comps * COMPONENT_BYTES[int(accessor['componentType'])]


def _accessor_f32_bytes(accessor: dict) -> int:
    return int(accessor['count']) * TYPE_COMPONENTS[accessor['type']] * 4


def parse_glb(path: Path) -> dict:
    """Parse one GLB: per-structure (per-mesh) geometry/buffer accounting."""
    data = path.read_bytes()
    magic, _version, length = struct.unpack_from('<4sII', data, 0)
    if magic != b'glTF':
        raise ValueError(f'{path}: not a GLB (bad magic {magic!r})')
    gltf = None
    offset = 12
    while offset < min(length, len(data)):
        chunk_len, chunk_type = struct.unpack_from('<II', data, offset)
        offset += 8
        chunk = data[offset:offset + chunk_len]
        offset += chunk_len
        if chunk_type == _GLTF_CHUNK:
            gltf = json.loads(chunk)
    if gltf is None:
        raise ValueError(f'{path}: no JSON chunk')

    accessors = gltf.get('accessors', [])
    materials = gltf.get('materials', [])
    meshes = gltf.get('meshes', [])

    # node name per mesh index (glb_writer names node AND mesh with the id)
    node_name_by_mesh: dict[int, str] = {}
    for node in gltf.get('nodes', []):
        if 'mesh' in node and node.get('name'):
            node_name_by_mesh.setdefault(int(node['mesh']), node['name'])

    structures = []
    for mesh_index, mesh in enumerate(meshes):
        name = mesh.get('name') or node_name_by_mesh.get(mesh_index) or f'mesh_{mesh_index}'
        triangles = 0
        vertices = 0
        index_bytes = 0
        vertex_bytes = 0
        f32_vertex_bytes = 0
        draw_calls = 0
        alpha_modes = set()
        n_primitives = 0
        for prim in mesh.get('primitives', []):
            n_primitives += 1
            attributes = prim.get('attributes', {})
            if 'POSITION' not in attributes:
                continue
            position = accessors[attributes['POSITION']]
            vertices += int(position['count'])

            for accessor_index in attributes.values():
                accessor = accessors[accessor_index]
                vertex_bytes += _accessor_stored_bytes(accessor)
                f32_vertex_bytes += _accessor_f32_bytes(accessor)

            if 'indices' in prim:
                indices = accessors[prim['indices']]
                index_bytes += _accessor_stored_bytes(indices)
                mode = int(prim.get('mode', 4))
                count = int(indices['count'])
                triangles += count // 3 if mode == 4 else max(0, count - 2)
            else:
                mode = int(prim.get('mode', 4))
                count = int(position['count'])
                triangles += count // 3 if mode == 4 else max(0, count - 2)

            material = materials[prim['material']] if 'material' in prim else {}
            alpha_mode = material.get('alphaMode', 'OPAQUE')
            alpha_modes.add(alpha_mode)
            # three.js renders double-sided transparent geometry in two passes
            # (back faces + front faces); the pipeline emits doubleSided:true.
            draw_calls += 1 + (1 if alpha_mode == 'BLEND' else 0)

        ratio = (vertex_bytes / f32_vertex_bytes) if f32_vertex_bytes else None
        all_buffer_ratio = (
            (vertex_bytes + index_bytes) / (f32_vertex_bytes + index_bytes)
            if (f32_vertex_bytes + index_bytes)
            else None
        )
        structures.append(
            {
                'name': name,
                'triangles': triangles,
                'vertices': vertices,
                'index_bytes': index_bytes,
                'vertex_bytes': vertex_bytes,
                'quantized_ratio_vs_f32': round(ratio, 4) if ratio is not None else None,
                'draw_calls_estimate': draw_calls,
                # self-description extras
                'primitives': n_primitives,
                'alpha_modes': sorted(alpha_modes),
                'buffer_bytes': index_bytes + vertex_bytes,
                'f32_vertex_bytes': f32_vertex_bytes,
                'quantized_ratio_vs_f32_all_buffers': round(all_buffer_ratio, 4)
                if all_buffer_ratio is not None
                else None,
            }
        )

    total_vertex_bytes = sum(s['vertex_bytes'] for s in structures)
    total_f32_bytes = sum(s['f32_vertex_bytes'] for s in structures)
    return {
        'structures': structures,
        'total_draw_calls': sum(s['draw_calls_estimate'] for s in structures),
        'total_bytes': sum(s['buffer_bytes'] for s in structures),
        'file_bytes': len(data),
        'total_triangles': sum(s['triangles'] for s in structures),
        'total_vertices': sum(s['vertices'] for s in structures),
        'quantized_ratio_vs_f32': round(total_vertex_bytes / total_f32_bytes, 4)
        if total_f32_bytes
        else None,
        'extensions_used': gltf.get('extensionsUsed', []),
    }


# --------------------------------------------------------------------------- #
# Scene combos + re-budgeting
# --------------------------------------------------------------------------- #

def _rebudget_split(member_triangles: list[tuple[str, int]]) -> list[dict]:
    """Split BUDGET_WINDOW_HI across members proportional to current triangles.

    Whole-thousand targets; the residual (from flooring) is assigned to the
    largest member so the split sums exactly to the combined ceiling.
    """
    total = sum(t for _, t in member_triangles)
    cap = BUDGET_WINDOW_HI
    targets = {
        name: max(1000, int(cap * t / total) // 1000 * 1000) for name, t in member_triangles
    }
    residual = cap - sum(targets.values())
    largest = max(member_triangles, key=lambda item: item[1])[0]
    targets[largest] += residual
    return [
        {
            'name': name,
            'current_triangles': t,
            'share_pct': round(100.0 * t / total, 2),
            'recommended_budget': targets[name],
        }
        for name, t in member_triangles
    ]


def build_combo(name: str, label: str, members: list[str], by_name: dict) -> dict:
    glbs = [by_name[m] for m in members]
    total_triangles = sum(g['total_triangles'] for g in glbs)
    within = BUDGET_WINDOW_LO <= total_triangles <= BUDGET_WINDOW_HI
    over = total_triangles > BUDGET_WINDOW_HI
    entry = {
        'name': name,
        'label': label,
        'glbs': [by_name[m]['path'] for m in members],
        'total_triangles': total_triangles,
        'within_budget': within,
        # self-description extras
        'budget_window': [BUDGET_WINDOW_LO, BUDGET_WINDOW_HI],
        'total_draw_calls': sum(g['total_draw_calls'] for g in glbs),
        'total_bytes': sum(g['total_bytes'] for g in glbs),
        'over_budget': over,
        'flag': None,
        'rebudget': None,
    }
    if over:
        over_by = total_triangles - BUDGET_WINDOW_HI
        entry['flag'] = (
            f'exceeds the {BUDGET_WINDOW_HI:,} triangle scene ceiling by {over_by:,} '
            f'triangles ({100.0 * over_by / BUDGET_WINDOW_HI:.1f}% over) — scene-level '
            f're-budgeting required before on-headset validation'
        )
        per_model = _rebudget_split([(m, by_name[m]['total_triangles']) for m in members])
        entry['rebudget'] = {
            'combined_target_triangles': BUDGET_WINDOW_HI,
            'rule': 'per-model split proportional to current triangle share of the combo, '
                    'floored to whole thousands; residual assigned to the largest model '
                    f'so the split sums exactly to {BUDGET_WINDOW_HI}',
            'per_model': per_model,
            'command_hint': 're-run pipeline/build_cardiac_glb.py per model with '
                            + ' and '.join(
                                f'--budget {row["recommended_budget"]} (for {row["name"]})'
                                for row in per_model
                            ),
            'caveat': 'build_cardiac_glb.py hard-clamps every output into its per-file window '
                      f'[{BUDGET_WINDOW_LO}, {BUDGET_WINDOW_HI}] (WINDOW_LO/WINDOW_HI), so any '
                      'per-model budget below the floor additionally requires parameterizing '
                      'that window (or one combined run over the union of the combo structures '
                      f'with --budget {BUDGET_WINDOW_HI})',
        }
    elif not within:
        entry['flag'] = (
            f'below the {BUDGET_WINDOW_LO:,} triangle floor of the budget window '
            f'({total_triangles:,} triangles) — acceptable only for partial/control models'
        )
    return entry


# --------------------------------------------------------------------------- #
# Load-time probe orchestration
# --------------------------------------------------------------------------- #

def run_loadtime(endpoint: str, samples: int) -> dict | None:
    command = ['node', str(REPO / PROBE_NAME), '--base', endpoint, '--samples', str(samples)]
    result = subprocess.run(command, capture_output=True, text=True, cwd=REPO)
    if result.returncode != 0:
        print(f'loadtime probe failed (exit {result.returncode}): {result.stderr.strip()}', file=sys.stderr)
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        print(f'loadtime probe produced invalid JSON: {exc}', file=sys.stderr)
        return None


# --------------------------------------------------------------------------- #
# Report rendering
# --------------------------------------------------------------------------- #

def _n(value) -> str:
    return f'{value:,}' if isinstance(value, int) else str(value)


def _ratio(value) -> str:
    return f'{value:.4f}' if isinstance(value, (int, float)) else 'n/a'


def _ms(row: dict | None) -> str:
    if not row or row.get('median') is None:
        return 'n/a'
    return f'{row["median"]:.1f}'


def _ms3(row: dict | None) -> str:
    if not row or row.get('median') is None:
        return 'n/a'
    return f'{row["min"]:.1f} / {row["median"]:.1f} / {row["max"]:.1f}'


def render_report(metrics: dict) -> str:
    glbs = metrics['glbs']
    combos = metrics['combos']
    loadtime = metrics.get('loadtime')
    load_by_name = {r['name']: r for r in (loadtime or {}).get('resources', [])}
    lines: list[str] = []
    add = lines.append

    add('# Track 2 — headless hardware pre-staging metrics')
    add('')
    add(f'- Generated: {metrics["generated"]}')
    add(f'- Tool: `{metrics["tool"]["name"]}` v{metrics["tool"]["version"]}')
    add(f'- Command: `{metrics["tool"]["command"]}`')
    probe_tool = (loadtime or {}).get('tool', {})
    add(
        f'- Load-time probe: `{probe_tool.get("name", PROBE_NAME)}` '
        f'v{probe_tool.get("version", "n/a")} (`{probe_tool.get("command", "not run")}`)'
    )
    add(f'- Endpoint: {metrics["endpoint"]}')
    add('')
    add('## Scope and method')
    add('')
    add('Everything in this document was measured headless on a workstation with no headset and no GPU:')
    add('')
    add('- **static geometry/buffer accounting** parsed directly from the GLB binaries')
    add('  (triangles, vertices, index/vertex buffer bytes, quantization ratio vs float32),')
    add('- **draw-call estimates** for the three.js WebXR renderer (see Definitions),')
    add('- **scene combos** of simultaneously loaded models checked against the pipeline')
    add(f'  budget window [{_n(BUDGET_WINDOW_LO)}, {_n(BUDGET_WINDOW_HI)}] triangles,')
    add('- **pure HTTP load timing** against the LAN HTTPS viewer endpoint')
    add('  (3 samples per resource, min/median/max; no browser involved).')
    add('')
    add('Not measured here, by construction: rendering cost, GPU load, thermals, and frame rates.')
    add('')
    add('## Target hardware assumptions (pre-staging only)')
    add('')
    add('Stated as assumptions for planning — nothing has been measured on the device yet:')
    add('')
    add('- **Device**: Meta Quest 2 standalone headset.')
    add('- **SoC**: Qualcomm Snapdragon XR2 with an Adreno 650-class tile-based GPU and')
    add('  unified CPU/GPU memory (no discrete VRAM budget — geometry and buffers compete')
    add('  with the browser runtime and textures).')
    add('- **Runtime**: headset browser (Chromium-class) with WebXR; secure context required,')
    add('  delivered over LAN Wi-Fi with a self-signed certificate the user accepts once.')
    add('- **Implications used above**: tile-based GPUs care about fill/overdraw — the extra')
    add('  BLEND pass per translucent structure matters more than raw triangle count;')
    add('  `KHR_mesh_quantization` buffers are decoded either natively or at load')
    add('  (the viewer dequantizes to float32 at load, trading memory for the robust path).')
    add('')
    add('## Per-GLB summary')
    add('')
    add('| GLB | structures | triangles | vertices | index bytes | vertex bytes | quantized vs f32 | draw calls est | buffer bytes | file bytes | load ms (median) |')
    add('|---|---|---|---|---|---|---|---|---|---|---|')
    for g in glbs:
        load = load_by_name.get(g['name'])
        add(
            f'| `{g["path"]}` | {len(g["structures"])} | {_n(g["total_triangles"])} | '
            f'{_n(g["total_vertices"])} | {_n(sum(s["index_bytes"] for s in g["structures"]))} | '
            f'{_n(sum(s["vertex_bytes"] for s in g["structures"]))} | '
            f'{_ratio(g["quantized_ratio_vs_f32"])} | {g["total_draw_calls"]} | '
            f'{_n(g["total_bytes"])} | {_n(g["file_bytes"])} | {_ms(load and load.get("total_ms"))} |'
        )
    add('')
    add('`single_aorta` is a single-structure control model sitting below the budget-window')
    add('floor; the Phase-1 pipeline keeps raw aggregates below the floor as-is and reports')
    add('`within_window: false` (see `out/single_aorta.json`). It appears in no scene combo.')
    add('')
    add('## Per-structure breakdown')
    add('')
    add('| GLB | structure | triangles | vertices | index bytes | vertex bytes | quantized_ratio_vs_f32 | draw_calls_estimate | alphaMode |')
    add('|---|---|---|---|---|---|---|---|---|')
    for g in glbs:
        for s in g['structures']:
            add(
                f'| `{g["path"]}` | {s["name"]} | {_n(s["triangles"])} | {_n(s["vertices"])} | '
                f'{_n(s["index_bytes"])} | {_n(s["vertex_bytes"])} | '
                f'{_ratio(s["quantized_ratio_vs_f32"])} | {s["draw_calls_estimate"]} | '
                f'{"+".join(s["alpha_modes"])} |'
            )
    add('')
    add('## Scene combos (worst-case co-loading)')
    add('')
    add('| combo | GLBs | total triangles | within budget | draw calls est | buffer bytes | flag |')
    add('|---|---|---|---|---|---|---|')
    for c in combos:
        flag = c['flag'] or '—'
        add(
            f'| {c["label"]} | {" + ".join(f"`{p}`" for p in c["glbs"])} | '
            f'{_n(c["total_triangles"])} | {"yes" if c["within_budget"] else "NO"} | '
            f'{c["total_draw_calls"]} | {_n(c["total_bytes"])} | {flag} |'
        )
    add('')
    add(f'`within_budget` = total triangles inside the pipeline window '
        f'[{_n(BUDGET_WINDOW_LO)}, {_n(BUDGET_WINDOW_HI)}].')
    add('')
    over = [c for c in combos if c['rebudget']]
    if over:
        add('### Re-budgeting for over-budget combos')
        add('')
        for c in over:
            add(f'**{c["label"]}** — {_n(c["total_triangles"])} triangles, '
                f'{_n(c["total_triangles"] - BUDGET_WINDOW_HI)} over the ceiling. '
                f'Recommended per-model split of {_n(BUDGET_WINDOW_HI)}:')
            add('')
            add('| GLB | current triangles | share | recommended `--budget` |')
            add('|---|---|---|---|')
            for row in c['rebudget']['per_model']:
                add(f'| `{"out/" + row["name"] + ".glb"}` | {_n(row["current_triangles"])} | '
                    f'{row["share_pct"]:.2f}% | {_n(row["recommended_budget"])} |')
            add('')
            add(f'- Command: `{c["rebudget"]["command_hint"]}`')
            add(f'- Caveat: {c["rebudget"]["caveat"]}')
            add('')
    add('## LAN HTTPS load-time baseline (headless)')
    add('')
    if loadtime:
        add(f'Probe: `{loadtime["tool"]["name"]}` v{loadtime["tool"]["version"]} '
            f'(`{loadtime["tool"]["command"]}`), {loadtime["samples_per_resource"]} samples per '
            f'resource over {loadtime["base"]} — min / median / max ms.')
        add('')
        add('Method: ' + loadtime['method']['transport'] + '; ' + loadtime['method']['connection']
            + '; ' + loadtime['method']['encoding'] + '.')
        add('')
        add('| resource | URL | kind | bytes | TTFB ms | download ms | total ms |')
        add('|---|---|---|---|---|---|---|')
        for r in loadtime['resources']:
            add(f'| {r["name"]} | `{r["url"]}` | {r["kind"]} | {_n(r["bytes"])} | '
                f'{_ms3(r.get("ttfb_ms"))} | {_ms3(r.get("download_ms"))} | {_ms3(r.get("total_ms"))} |')
        add('')
        add('Load ms in the per-GLB table = median `total_ms` of the matching resource.')
    else:
        add('Load-time probe skipped (`--skip-loadtime` or probe failure) — no load metrics in this run.')
    add('')
    add('## Definitions')
    add('')
    add('- `quantized_ratio_vs_f32` = stored vertex-attribute buffer bytes (POSITION + NORMAL as')
    add('  packed by `pipeline/glb_writer.py`) divided by the same attributes stored as float32.')
    add('  The quantized layout stores POSITION as normalized unsigned short (6 bytes/vertex) and')
    add('  NORMAL as normalized signed byte (3 bytes/vertex): 9 bytes/vertex vs 24 bytes/vertex')
    add('  float32, so every quantized structure reads 0.375 (a float32 GLB reads 1.0 by')
    add('  definition). The JSON variant `quantized_ratio_vs_f32_all_buffers` includes the')
    add('  index buffer in both terms.')
    add('- `draw_calls_estimate` = 1 pass per mesh primitive + 1 extra pass when the material')
    add('  `alphaMode` is `BLEND` (three.js renders double-sided transparent geometry in two')
    add('  passes: back faces then front faces; the pipeline emits `doubleSided: true`).')
    add('- `total_bytes` = sum of per-structure `index_bytes + vertex_bytes` (GLB BIN payload);')
    add('  `file_bytes` = on-disk GLB size including the JSON chunk.')
    add('- `load_ms` / load-time totals = request start to last body byte over a fresh TLS')
    add('  connection per sample (cold-connection timing).')
    add('')
    add('## Pre-headset checklist (to be executed ON the Meta Quest 2)')
    add('')
    add('This is what will be measured on-device; none of it is covered by this headless document:')
    add('')
    add('- [ ] Pair the headset to the LAN, open the served URL in the headset browser, accept the')
    add('      self-signed certificate warning once (secure context required for WebXR).')
    add('- [ ] Confirm WebXR availability and enter an immersive session for every model target')
    add('      (`?model=...`) and for each scene combo (a)–(e).')
    add('- [ ] ON-headset: capture **FPS telemetry via the in-XR readout** while running each combo and verify against the brief target band 72-90 FPS (TARGET for the session, not a result - measurement pending)')
    add('      against the headset target display-refresh band and log the actuals — the only')
    add('      admissible source of frame-rate data.')
    add('- [ ] ON-headset: model load times per `?model=` target over the LAN Wi-Fi path, compared')
    add('      against the headless load-time baseline above.')
    add('- [ ] ON-headset: render-correctness pass — KHR_mesh_quantization decode, per-structure')
    add('      visibility toggles, and the two-pass BLEND/double-sided transparency of the')
    add('      translucent structures.')
    add('- [ ] ON-headset: draw-call behavior per combo vs the estimates above (device GPU counters')
    add('      where exposed).')
    add('- [ ] ON-headset: thermal + battery soak for the named worst-case combo (d).')
    add('- [ ] ON-headset: controller interaction and passthrough/guardian entry-exit.')
    add('')
    add('frame rates NOT verified without headset telemetry')
    add('')
    return '\n'.join(lines)


# --------------------------------------------------------------------------- #

def main() -> int:
    parser = argparse.ArgumentParser(description='Headless hardware pre-staging measurements (Track 2).')
    parser.add_argument('--json', default='out/track2_metrics.json')
    parser.add_argument('--report', default='out/track2_report.md')
    parser.add_argument('--endpoint', default='https://192.168.1.229:8444')
    parser.add_argument('--samples', type=int, default=3)
    parser.add_argument('--skip-loadtime', action='store_true')
    args = parser.parse_args()

    command = f'{sys.executable} ' + ' '.join(sys.argv)

    entries = []
    by_name = {}
    for spec in GLBS:
        path = REPO / spec['path']
        parsed = parse_glb(path)
        entry = {
            'path': spec['path'],
            'name': spec['name'],
            'served_url': spec['served_url'],
            'structures': parsed['structures'],
            'total_draw_calls': parsed['total_draw_calls'],
            'total_bytes': parsed['total_bytes'],
            'load_ms': None,
            # self-description extras
            'file_bytes': parsed['file_bytes'],
            'total_triangles': parsed['total_triangles'],
            'total_vertices': parsed['total_vertices'],
            'quantized_ratio_vs_f32': parsed['quantized_ratio_vs_f32'],
            'extensions_used': parsed['extensions_used'],
            'within_window': BUDGET_WINDOW_LO <= parsed['total_triangles'] <= BUDGET_WINDOW_HI,
        }
        if spec.get('note'):
            entry['note'] = spec['note']
        entries.append(entry)
        by_name[spec['name']] = entry

    loadtime = None
    if not args.skip_loadtime:
        loadtime = run_loadtime(args.endpoint, args.samples)
        if loadtime is not None:
            load_by_glb = {r.get('glb'): r for r in loadtime['resources'] if r.get('glb')}
            for entry in entries:
                resource = load_by_glb.get(entry['path'])
                if resource and resource.get('total_ms', {}).get('median') is not None:
                    entry['load_ms'] = resource['total_ms']['median']

    combos = [build_combo(name, label, members, by_name) for name, label, members in COMBOS]

    notes = [
        'frame rates NOT verified without headset telemetry',
        'No renderer runs here: no GPU timing, no display timing, no frame-rate numbers '
        'appear anywhere in this artifact. Frame rates are measured exclusively ON the '
        'headset via the in-XR FPS readout (see out/track2_report.md pre-headset checklist).',
        'Target hardware is stated as assumptions only (Meta Quest 2 / Snapdragon XR2); '
        'nothing has been validated on the device.',
        'quantized_ratio_vs_f32 = stored vertex-attribute buffer bytes / float32-equivalent '
        'vertex-attribute bytes (count * components * 4). The quantized layout stores '
        'POSITION as normalized unsigned short (6 B/vertex) and NORMAL as normalized signed '
        'byte (3 B/vertex) = 9 B/vertex vs 24 B/vertex float32 -> ratio 0.375. The variant '
        'quantized_ratio_vs_f32_all_buffers includes the index buffer in both terms.',
        'draw_calls_estimate = 1 pass per mesh primitive + 1 extra pass when the material '
        'alphaMode is BLEND; three.js renders double-sided transparent geometry in two '
        'passes (back faces then front faces) and the pipeline emits doubleSided:true.',
        'total_bytes = sum of per-structure index_bytes + vertex_bytes (GLB BIN payload); '
        'file_bytes = on-disk GLB size (JSON chunk + padding included).',
        'load_ms = median total_ms (request start -> last body byte, fresh TLS connection per '
        'sample) from tools/loadtime_probe.mjs; 3 samples per resource with min/median/max and '
        'raw samples retained under "loadtime".',
        'out/cardiac_s0004.glb is served as assets/cardiac.glb (viewer default model); the two '
        'files are byte-identical (md5 384cc1145161ce0f9f69967d358df0b8), so its load timing '
        'is the true asset timing.',
        f'Combo within_budget is checked against the pipeline window [{BUDGET_WINDOW_LO}, '
        f'{BUDGET_WINDOW_HI}] (build_cardiac_glb.py WINDOW_LO/WINDOW_HI, same as the '
        'within_window check in the Phase-1 report schema).',
        'Combos exceeding the ceiling carry an explicit flag and a per-model budget split '
        '(rebudget); note the caveat there: build_cardiac_glb.py hard-clamps each output into '
        f'[{BUDGET_WINDOW_LO}, {BUDGET_WINDOW_HI}], so per-model budgets below the floor need '
        'that window parameterized first.',
    ]

    metrics = {
        'schema_version': 1,
        'generated': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'tool': {
            'name': TOOL_NAME,
            'version': TOOL_VERSION,
            'command': command,
            'python': sys.version.split()[0],
        },
        'endpoint': args.endpoint,
        'budget_window': [BUDGET_WINDOW_LO, BUDGET_WINDOW_HI],
        'target_hardware': {
            'device': 'Meta Quest 2',
            'soc': 'Qualcomm Snapdragon XR2 (Adreno 650-class tile-based GPU, unified memory)',
            'assumptions_only': True,
            'note': 'planning assumptions for pre-staging; no on-device measurement performed',
        },
        'glbs': entries,
        'combos': combos,
        'loadtime': loadtime,
        'notes': notes,
    }

    json_path = REPO / args.json
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(metrics, indent=2) + '\n')

    report_path = REPO / args.report
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(render_report(metrics))

    print(
        f'{TOOL_NAME} v{TOOL_VERSION}: {len(entries)} GLBs, {len(combos)} combos, '
        f'loadtime={"yes" if loadtime else "no"} -> {args.json}, {args.report}'
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
