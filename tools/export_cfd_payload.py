#!/usr/bin/env python3
"""Export the WebXR CFD contrast payload for one case + injection profile (contract C5).

Inputs (read-only):
  out/transport/contrast/{case}_{profile}.npz + .json   C4 contrast solve (K x N_a)
  out/coronary_{case}.glb                               glTF 2.0 anatomy (JSON+BIN chunks)
  out/report_{case}.json                                Phase-1 report world transform
  out/rom/hemodynamics/{case}/hemodynamics.json         C2 branch timing + pressures
  out/rom/graphs/{case}_graph.json                      C1 edge flags (major/distal)

Outputs (one profile per invocation):
  viewer/public/assets/case_{case}_{profile}_contrast.bin     row-major K x V uint8
  viewer/public/assets/case_{case}_{profile}_metadata.json    flowscope.cfd.contrast v1

Vertex RAS positions come from the GLB model-space vertices mapped back through
the inverse of the report ``transform`` (ras_mm->y_up_m). Per-vertex
concentration is a trilinear sample of the C4 carrier lattice whose masked
(non-lumen) cells are inpainted with their nearest active cell's value, so
surface vertices just outside the lumen sample a continuous field.
"""

from __future__ import annotations

# machine spec: 8 BLAS threads, pinned before numpy loads (contract preamble)
import os

os.environ['OMP_NUM_THREADS'] = '8'
os.environ['MKL_NUM_THREADS'] = '8'
os.environ['OPENBLAS_NUM_THREADS'] = '8'

import argparse
import json
import struct
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from scipy.spatial import cKDTree

TOOL = {'name': 'tools/export_cfd_payload.py', 'version': '0.1.0'}
SCHEMA = 'flowscope.cfd.contrast'
SCHEMA_VERSION = 1
MAX_TOTAL_MB = 15.0
C_MIN, C_MAX = 0.0, 1.0
# Fallback world transform when the report carries none and --center-m is given:
# identical to pipeline/build_cardiac_glb.py _transform_record constants.
_DEFAULT_PERM = [0, 2, 1]
_DEFAULT_SIGN = [1.0, 1.0, -1.0]
_DEFAULT_SCALE_M_PER_MM = 1e-3
# Carrier lattice cell-center convention (C3): cell (i, j, k) center sits at
# origin_mm + (ijk + _CELL_OFFSET) * spacing_mm with origin_mm the lattice
# origin (grid corner), matching the phase-2 transport cell-centered grids.
_CELL_OFFSET = 0.5

_GLB_MAGIC = 0x46546C67
_CHUNK_JSON = 0x4E4F534A
_CHUNK_BIN = 0x004E4942
_COMPONENT = {
    5120: np.dtype('i1'),
    5121: np.dtype('u1'),
    5122: np.dtype('<i2'),
    5123: np.dtype('<u2'),
    5125: np.dtype('<u4'),
    5126: np.dtype('<f4'),
}
_NCOMP = {'SCALAR': 1, 'VEC2': 2, 'VEC3': 3, 'VEC4': 4}


def _fail(msg: str) -> None:
    raise SystemExit(f'export_cfd_payload: error: {msg}')


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description='Export one case/profile CFD contrast payload (C5) for the viewer.'
    )
    parser.add_argument('--case', required=True, help='case id (601, 700, 798)')
    parser.add_argument('--profile', required=True, choices=['A', 'B', 'C'])
    parser.add_argument('--glb', default=None,
                        help='anatomy GLB (default out/coronary_{case}.glb)')
    parser.add_argument('--contrast-dir', default='out/transport/contrast')
    parser.add_argument('--hemo-dir', default='out/rom/hemodynamics')
    parser.add_argument('--graph-dir', default='out/rom/graphs')
    parser.add_argument('--report', default=None,
                        help='Phase-1 report JSON (default out/report_{case}.json)')
    parser.add_argument('--assets-dir', default='viewer/public/assets')
    parser.add_argument('--center-m', nargs=3, type=float, metavar=('X', 'Y', 'Z'),
                        default=None,
                        help='override the transform center_m (x y z, meters)')
    return parser.parse_args(argv)


# --------------------------------------------------------------------------- glTF


def _read_accessor(gltf: dict, blob: bytes, acc: dict) -> np.ndarray:
    """Decode one glTF accessor from the GLB BIN chunk (tightly/strided packed)."""
    comp = _COMPONENT.get(acc.get('componentType'))
    ncomp = _NCOMP.get(acc.get('type'))
    if comp is None or ncomp is None:
        _fail(f'unsupported accessor componentType={acc.get("componentType")} '
              f'type={acc.get("type")}')
    if 'bufferView' not in acc:
        _fail('accessor without bufferView (sparse accessors unsupported)')
    view = gltf['bufferViews'][acc['bufferView']]
    count = int(acc['count'])
    base = int(view.get('byteOffset', 0)) + int(acc.get('byteOffset', 0))
    item = comp.itemsize
    stride = int(view.get('byteStride', 0)) or item * ncomp
    need = base + (count - 1) * stride + item * ncomp if count else base
    if need > len(blob):
        _fail('accessor reads past the GLB binary chunk')
    if stride == item * ncomp:
        flat = np.frombuffer(blob, dtype=comp, count=count * ncomp, offset=base)
        arr = flat.reshape(count, ncomp)
    else:
        arr = np.ndarray((count, ncomp), dtype=comp, buffer=blob, offset=base,
                         strides=(stride, item))
    return arr.reshape(count) if ncomp == 1 else arr


def _node_trs(node: dict) -> tuple[np.ndarray, np.ndarray]:
    """Rotation-free node TRS: world = translation + scale * local."""
    if 'matrix' in node:
        _fail(f'node {node.get("name")!r}: matrix transforms unsupported')
    rot = node.get('rotation')
    if rot is not None and [float(v) for v in rot] != [0.0, 0.0, 0.0, 1.0]:
        _fail(f'node {node.get("name")!r}: rotation unsupported')
    translation = np.asarray(node.get('translation', (0.0, 0.0, 0.0)), dtype=np.float64)
    scale = np.asarray(node.get('scale', (1.0, 1.0, 1.0)), dtype=np.float64)
    if translation.shape != (3,) or scale.shape != (3,):
        _fail(f'node {node.get("name")!r}: malformed translation/scale')
    return translation, scale


def _read_glb(path: Path) -> tuple[list[dict], np.ndarray, int]:
    """Decode a glTF 2.0 .glb: per-primitive mesh records, world vertices, triangles.

    Vertex order is primitives in GLB file order (``meshes[]`` outer,
    ``primitives[]`` inner). Quantized (KHR_mesh_quantization) POSITION is 5123
    u16 normalized over the mesh-local unit cube mapped via node.translation
    (bbox min) + node.scale (bbox extent); the --no-quantize variant is 5126
    f32 world-space under an identity node transform.
    """
    raw = path.read_bytes()
    if len(raw) < 20:
        _fail(f'{path}: truncated GLB')
    magic, version, total = struct.unpack_from('<4sII', raw, 0)
    if magic != _GLB_MAGIC.to_bytes(4, 'little'):
        _fail(f'{path}: not a GLB container')
    if version != 2:
        _fail(f'{path}: GLB version {version} unsupported (want 2)')
    gltf = None
    blob = b''
    off = 12
    while off + 8 <= min(total, len(raw)):
        clen, ctype = struct.unpack_from('<II', raw, off)
        off += 8
        chunk = raw[off:off + clen]
        off += clen
        if ctype == _CHUNK_JSON:
            gltf = json.loads(chunk.decode('utf-8'))
        elif ctype == _CHUNK_BIN:
            blob = bytes(chunk)
    if gltf is None:
        _fail(f'{path}: no JSON chunk')
    if not blob:
        _fail(f'{path}: no BIN chunk (external buffers unsupported)')

    node_by_mesh: dict[int, tuple[str, np.ndarray, np.ndarray]] = {}
    for node in gltf.get('nodes', []):
        mi = node.get('mesh')
        if mi is None:
            continue
        if mi in node_by_mesh:
            _fail(f'{path}: mesh {mi} instanced by multiple nodes; cannot map vertices 1:1')
        node_by_mesh[mi] = (str(node.get('name', '')), *_node_trs(node))

    meshes: list[dict] = []
    parts: list[np.ndarray] = []
    triangles = 0
    vertex_offset = 0
    for mi, mesh in enumerate(gltf.get('meshes', [])):
        if mi not in node_by_mesh:
            _fail(f'{path}: mesh {mi} is not referenced by any node')
        node_name, translation, scale = node_by_mesh[mi]
        for pi, prim in enumerate(mesh.get('primitives', [])):
            if prim.get('mode', 4) != 4:
                _fail(f'{path}: mesh {mi} primitive {pi}: only TRIANGLES supported')
            attrs = prim.get('attributes', {})
            if 'POSITION' not in attrs:
                _fail(f'{path}: mesh {mi} primitive {pi} has no POSITION')
            pos_acc = gltf['accessors'][attrs['POSITION']]
            if pos_acc.get('type') != 'VEC3':
                _fail(f'{path}: mesh {mi} primitive {pi}: POSITION is not VEC3')
            pos = _read_accessor(gltf, blob, pos_acc)
            comp = pos_acc.get('componentType')
            if comp == 5123 and pos_acc.get('normalized'):
                # KHR_mesh_quantization: u16 normalized -> unit cube -> node bbox
                v_m = pos.astype(np.float64) / 65535.0 * scale + translation
            elif comp == 5126:
                v_m = pos.astype(np.float64) * scale + translation
            else:
                _fail(f'{path}: mesh {mi} primitive {pi}: POSITION componentType '
                      f'{comp} normalized={pos_acc.get("normalized")} unsupported')
            n_vert = v_m.shape[0]

            if 'indices' in prim:
                idx = _read_accessor(gltf, blob, gltf['accessors'][prim['indices']])
                idx = idx.astype(np.int64).reshape(-1)
            else:
                idx = np.arange(n_vert, dtype=np.int64)
            if idx.size % 3:
                _fail(f'{path}: mesh {mi} primitive {pi}: index count not divisible by 3')
            if idx.size and (int(idx.min()) < 0 or int(idx.max()) >= n_vert):
                _fail(f'{path}: mesh {mi} primitive {pi}: indices out of vertex range')

            triangles += int(idx.size // 3)
            parts.append(v_m)
            meshes.append(
                {
                    'name': str(mesh.get('name', mi)),
                    'node': node_name,
                    'primitive_index': pi,
                    'vertex_offset': vertex_offset,
                    'vertex_count': int(n_vert),
                }
            )
            vertex_offset += n_vert

    positions = (np.concatenate(parts, axis=0) if parts
                 else np.zeros((0, 3), dtype=np.float64))
    return meshes, positions, triangles


# ------------------------------------------------------------------- coordinates


def _resolve_transform(report: dict | None, center_override, report_path: Path) -> dict:
    """World transform record used for the model -> RAS mapping (HARD ERROR when absent)."""
    tr = (report or {}).get('transform') if isinstance(report, dict) else None
    if tr is None:
        if center_override is None:
            _fail(f'{report_path}: report has no "transform" and --center-m was not '
                  f'given; refusing to guess the RAS alignment')
        out = {
            'space': 'ras_mm->y_up_m',
            'perm': list(_DEFAULT_PERM),
            'sign': list(_DEFAULT_SIGN),
            'scale_m_per_mm': _DEFAULT_SCALE_M_PER_MM,
            'center_m': [float(v) for v in center_override],
        }
    else:
        out = {
            'space': tr.get('space'),
            'perm': [int(v) for v in tr['perm']],
            'sign': [float(v) for v in tr['sign']],
            'scale_m_per_mm': float(tr['scale_m_per_mm']),
            'center_m': [float(v) for v in tr['center_m']],
        }
        if center_override is not None:
            out['center_m'] = [float(v) for v in center_override]
    if sorted(out['perm']) != [0, 1, 2]:
        _fail(f'{report_path}: transform perm {out["perm"]} is not a permutation of [0, 1, 2]')
    if len(out['sign']) != 3 or any(v == 0.0 for v in out['sign']):
        _fail(f'{report_path}: transform sign {out["sign"]} malformed')
    if len(out['center_m']) != 3:
        _fail(f'{report_path}: transform center_m {out["center_m"]} must have 3 elements')
    if out['scale_m_per_mm'] <= 0.0:
        _fail(f'{report_path}: transform scale_m_per_mm {out["scale_m_per_mm"]} must be > 0')
    return out


def _model_to_ras(v_m: np.ndarray, tr: dict) -> np.ndarray:
    """Inverse of y_m = x_mm[:, perm] * sign * scale - center (perm is self-inverse)."""
    denom = np.asarray(tr['sign'], dtype=np.float64) * float(tr['scale_m_per_mm'])
    return ((v_m + np.asarray(tr['center_m'], dtype=np.float64)) / denom)[:, tr['perm']]


# ---------------------------------------------------------------- C4 contrast


def _load_contrast(contrast_dir: Path, case: str, profile: str):
    npz_path = contrast_dir / f'{case}_{profile}.npz'
    json_path = contrast_dir / f'{case}_{profile}.json'
    for path in (npz_path, json_path):
        if not path.exists():
            _fail(f'{path}: missing (run phase2/transport/solve_advection.py first)')
    with np.load(npz_path) as data:
        missing = [k for k in ('t_s', 'C', 'cell_ravel', 'grid_shape', 'origin_mm',
                               'spacing_mm') if k not in data]
        if missing:
            _fail(f'{npz_path}: missing arrays {missing}')
        t_s = np.asarray(data['t_s'], dtype=np.float64).reshape(-1)
        c_arr = np.asarray(data['C'], dtype=np.float32)
        cell_ravel = np.asarray(data['cell_ravel'], dtype=np.int64).reshape(-1)
        grid_shape = tuple(int(v) for v in np.asarray(data['grid_shape']).reshape(3))
        origin = np.asarray(data['origin_mm'], dtype=np.float64).reshape(3)
        spacing = float(np.asarray(data['spacing_mm'], dtype=np.float64).reshape(()))

    if c_arr.ndim != 2 or c_arr.shape != (t_s.size, cell_ravel.size):
        _fail(f'{npz_path}: C shape {c_arr.shape} != (K={t_s.size}, N_a={cell_ravel.size})')
    if cell_ravel.size == 0:
        _fail(f'{npz_path}: no active carrier cells')
    n_cell = int(np.prod(grid_shape))
    if min(grid_shape) <= 0 or int(cell_ravel.min()) < 0 or int(cell_ravel.max()) >= n_cell:
        _fail(f'{npz_path}: cell_ravel out of range for grid_shape {grid_shape}')
    if np.unique(cell_ravel).size != cell_ravel.size:
        _fail(f'{npz_path}: cell_ravel has duplicates')
    if not np.isfinite(t_s).all() or not np.isfinite(c_arr).all():
        _fail(f'{npz_path}: non-finite values in t_s/C')
    if t_s.size > 1 and not bool(np.all(np.diff(t_s) > 0.0)):
        _fail(f'{npz_path}: t_s not strictly increasing')

    sidecar = json.loads(json_path.read_text())
    if sidecar.get('profile', profile) != profile:
        _fail(f'{json_path}: profile {sidecar.get("profile")!r} != {profile!r}')
    if sidecar.get('frames', int(t_s.size)) != int(t_s.size):
        _fail(f'{json_path}: frames {sidecar.get("frames")} != K={t_s.size}')
    return t_s, c_arr, cell_ravel, grid_shape, origin, spacing


def _sample_lattice(c_arr, cell_ravel, grid_shape, origin, spacing, x_mm):
    """Trilinear sample of C[k] at RAS x_mm (N, 3) -> (K, N) row-major uint8.

    Masked (non-lumen) lattice cells are inpainted with their nearest active
    cell's value ("nearest-active fill"), sampled through the 8 surrounding
    lattice nodes of the vertex's containing cell.
    """
    ijk = np.stack(np.unravel_index(cell_ravel, grid_shape))  # (3, N_a), C-order
    centers = origin + (ijk.T + _CELL_OFFSET) * spacing        # (N_a, 3) mm
    tree = cKDTree(centers)

    n_ax = np.asarray(grid_shape, dtype=np.float64)
    g = np.clip((x_mm - origin) / spacing - _CELL_OFFSET, 0.0, n_ax - 1.0)
    lo = np.floor(g).astype(np.int64)
    frac = (g - lo).astype(np.float32)
    hi = np.minimum(lo + 1, np.asarray(grid_shape, dtype=np.int64) - 1)

    n_vert = x_mm.shape[0]
    corner_ijk = np.empty((8, n_vert, 3), dtype=np.int64)
    corner_w = np.empty((8, n_vert), dtype=np.float32)
    b = 0
    for dx in (0, 1):
        ix, wx = (hi[:, 0], frac[:, 0]) if dx else (lo[:, 0], 1.0 - frac[:, 0])
        for dy in (0, 1):
            iy, wy = (hi[:, 1], frac[:, 1]) if dy else (lo[:, 1], 1.0 - frac[:, 1])
            for dz in (0, 1):
                iz, wz = (hi[:, 2], frac[:, 2]) if dz else (lo[:, 2], 1.0 - frac[:, 2])
                corner_ijk[b, :, 0] = ix
                corner_ijk[b, :, 1] = iy
                corner_ijk[b, :, 2] = iz
                corner_w[b] = wx * wy * wz
                b += 1

    corner_xyz = origin + (corner_ijk + _CELL_OFFSET) * spacing   # (8, N, 3) mm
    _, nearest = tree.query(corner_xyz, k=1)   # (8, N) active-cell order of each node
    del corner_ijk, corner_xyz, tree

    out = np.empty((c_arr.shape[0], n_vert), dtype=np.uint8)
    for k in range(c_arr.shape[0]):
        vals = c_arr[k][nearest]                      # (8, N) inpainted node values
        sampled = (vals * corner_w).sum(axis=0)       # trilinear -> (N,)
        out[k] = np.rint(np.clip(sampled, C_MIN, C_MAX) * 255.0).astype(np.uint8)
    return out


# ---------------------------------------------------------- C1 / C2 read-outs


def _alnum(name: str) -> str:
    return ''.join(ch for ch in name.casefold() if ch.isalnum())


def _edge_flags(graph: dict):
    """C1 edge flags indexed by name (exact, casefold, alnum-stripped fallbacks)."""
    exact: dict = {}
    folded: dict = {}
    stripped: dict = {}
    for edge in graph.get('edges', []):
        name = edge.get('name')
        if not isinstance(name, str):
            continue
        flags = edge.get('flags') or {}
        exact.setdefault(name, flags)
        folded.setdefault(name.casefold(), flags)
        stripped.setdefault(_alnum(name), flags)
    return exact, folded, stripped


def _lookup_flags(index, name: str) -> dict:
    exact, folded, stripped = index
    for table, key in ((exact, name), (folded, name.casefold()), (stripped, _alnum(name))):
        flags = table.get(key)
        if flags is not None:
            return flags
    return {}


def _branches_meta(hemo: dict, graph: dict, profile: str) -> list[dict]:
    """C5 branches from C2 signals.<profile>.branches + C1 edge flags."""
    sig = hemo.get('signals', {}).get(profile)
    if not isinstance(sig, dict) or not isinstance(sig.get('branches'), dict):
        _fail(f'hemodynamics.json: no signals.{profile}.branches')
    index = _edge_flags(graph)
    rows = []
    for name, rec in sig['branches'].items():
        flags = _lookup_flags(index, name)
        rows.append(
            {
                'name': name,
                'distal': bool(flags.get('distal', False)),
                'major': bool(flags.get('major', False)),
                # Nullable C2 fields (JSON null on negligible-mean-flow branches,
                # division guards Q_EPS_MLS/Q_MEAN_MIN_MLS upstream) pass through
                # as JSON null — never a float coercion of None, never 0.0.
                'transit_ms': None if rec['transit_ms'] is None else float(rec['transit_ms']),
                't_arrival_ms': (None if rec['t_arrival_s'] is None
                                 else float(rec['t_arrival_s']) * 1e3),
                't_peak_ms': (None if rec['t_peak_s'] is None
                              else float(rec['t_peak_s']) * 1e3),
                'timi_frames_30fps': (None if rec['timi_frames_30fps'] is None
                                      else int(rec['timi_frames_30fps'])),
            }
        )
    return rows


def _pressures(hemo: dict, profile: str) -> dict:
    """Mean inlet pressure + per-outlet mean pressures (list when available)."""
    physiology = hemo.get('physiology') or {}
    out = {'p_in_mean_mmHg': float(physiology.get('p_mean_mmHg', 90.0))}
    sig = hemo.get('signals', {}).get(profile) or {}
    vals = sig.get('p_outlet_mean_mmHg')
    if not isinstance(vals, list) or not vals:
        per_outlet = (hemo.get('windkessel') or {}).get('per_outlet') or []
        if per_outlet and all('p_outlet_mean_mmHg' in row for row in per_outlet):
            vals = [row['p_outlet_mean_mmHg'] for row in per_outlet]
    if isinstance(vals, list) and vals:
        out['p_outlet_mean_mmHg'] = [float(v) for v in vals]
    return out


# ---------------------------------------------------------------------------- main


def main(argv=None) -> int:
    t0 = time.perf_counter()
    args = parse_args(argv)
    case, profile = args.case, args.profile

    glb_path = Path(args.glb) if args.glb else Path(f'out/coronary_{case}.glb')
    report_path = Path(args.report) if args.report else Path(f'out/report_{case}.json')
    hemo_path = Path(args.hemo_dir) / case / 'hemodynamics.json'
    graph_path = Path(args.graph_dir) / f'{case}_graph.json'
    assets_dir = Path(args.assets_dir)
    bin_name = f'case_{case}_{profile}_contrast.bin'
    meta_name = f'case_{case}_{profile}_metadata.json'

    for path in (glb_path, hemo_path, graph_path):
        if not path.exists():
            _fail(f'{path}: missing')

    report = json.loads(report_path.read_text()) if report_path.exists() else None
    transform = _resolve_transform(report, args.center_m, report_path)
    hemo = json.loads(hemo_path.read_text())
    graph = json.loads(graph_path.read_text())

    meshes, v_m, triangles = _read_glb(glb_path)
    x_mm = _model_to_ras(v_m, transform)

    t_s, c_arr, cell_ravel, grid_shape, origin, spacing = _load_contrast(
        Path(args.contrast_dir), case, profile
    )
    payload = _sample_lattice(c_arr, cell_ravel, grid_shape, origin, spacing, x_mm)
    frames, vertices = int(payload.shape[0]), int(payload.shape[1])

    generated = datetime.now(timezone.utc).isoformat()
    meta = {
        'schema': SCHEMA,
        'schema_version': SCHEMA_VERSION,
        'tool': {
            **TOOL,
            'command': ' '.join(sys.argv),
            'generated': generated,
            'runtime_s': time.perf_counter() - t0,
        },
        'case': case,
        'profile': profile,
        'generated': generated,
        'bin_file': bin_name,
        'dtype': 'uint8',
        'layout': 'frames x vertices row-major',
        'frames': frames,
        'vertices': vertices,
        'timestamps_s': [float(t) for t in t_s],
        'quantization': {'c_min': C_MIN, 'c_max': C_MAX},
        'meshes': meshes,
        'branches': _branches_meta(hemo, graph, profile),
        'pressures': _pressures(hemo, profile),
        'triangles': int(triangles),
        'sampling': {
            'method': 'trilinear_masked_inpaint',
            'masked_cells': 'nearest_active_fill',
            'cell_offset': _CELL_OFFSET,
        },
        'total_mb': 0.0,
    }

    # total_mb includes its own JSON bytes: settle the self-referential size.
    bin_bytes = int(payload.nbytes)
    meta_text = json.dumps(meta, indent=2)
    for _ in range(5):
        total = (bin_bytes + len(meta_text.encode('utf-8'))) / 1e6
        new = round(total, 4)
        if meta['total_mb'] == new:
            break
        meta['total_mb'] = new
        meta_text = json.dumps(meta, indent=2)
    total_mb = float(meta['total_mb'])
    if total_mb >= MAX_TOTAL_MB:
        _fail(f'payload {total_mb:.3f} MB (bin {bin_bytes / 1e6:.3f} MB) exceeds the '
              f'{MAX_TOTAL_MB} MB per-profile ceiling')

    assets_dir.mkdir(parents=True, exist_ok=True)
    (assets_dir / bin_name).write_bytes(payload.tobytes())
    (assets_dir / meta_name).write_text(meta_text)
    print(f'{assets_dir / bin_name}: {frames} frames x {vertices} vertices '
          f'({triangles} triangles)')
    print(f'{assets_dir / meta_name}: total {total_mb:.3f} MB')
    return 0


if __name__ == '__main__':
    sys.exit(main())
