#!/usr/bin/env python3
"""Export the viewer clinical payload for one case (contract D: flowscope.clinical.viewer v1).

Inputs (read-only):
  viewer/public/assets/coronary_{case}.glb        (fallback out/coronary_{case}.glb) anatomy GLB
  out/report_{case}.json                          Phase-1 pipeline report world transform
  out/rom/graphs/{case}_graph.json                C1 edge centerlines (RAS mm)
  out/rom/hemodynamics/{case}/hemodynamics.json   C2 clinical rest/hyper blocks + signals.A
  out/clinical/rxi/case_{case}_rxi.json           contract C paths / pullbacks / landmarks
  out/clinical/lesions/case_{case}_lesions.json   contract A stenosis measurements

Outputs (identical bytes in both places):
  viewer/models/clinical/case_{case}_clinical.json
  viewer/public/clinical/case_{case}_clinical.json

FRAME CONTRACT (binding): every ``location_xyz_ras`` / ``centerline_ras`` coordinate
is in GLB model-frame METERS — the exact coordinate space of the GLB ``POSITION``
vertices the viewer raycasts in — despite the legacy ``_ras`` names. RAS-mm math
runs through ``_model_to_ras``; graph ``points_mm`` (RAS mm) are mapped INTO the
model frame with the forward map that ``_model_to_ras`` inverts.

Per-vertex vFFR: every vertex is associated to the nearest 1 mm centerline station
(cKDTree, like the contrast exporter's sampling) and takes that station's vFFR from
the rxi pullback when its edge lies on a path (interp at the station), else the
branch's hyperemic distal vFFR. Quantization:
``u8 = round((clip(vffr, 0.4, 1.0) - 0.4) / 0.6 * 255)``.

Rounding: coords 1e-6 m, pressures 0.01 mmHg, vFFR/Pd/Pa 0.001, geometry/times 0.001.
Hard failure over 2 MB per emitted file.
"""

from __future__ import annotations

# machine spec: 8 BLAS threads, pinned before numpy loads (contract preamble)
import os

os.environ['OMP_NUM_THREADS'] = '8'
os.environ['MKL_NUM_THREADS'] = '8'
os.environ['OPENBLAS_NUM_THREADS'] = '8'

import argparse
import base64
import json
import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from scipy.spatial import cKDTree

# proven helpers from tools/export_cfd_payload.py (imported read-only; never modified)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from export_cfd_payload import (  # noqa: E402  (path shim must run first)
    _alnum,
    _branches_meta,
    _edge_flags,
    _lookup_flags,
    _model_to_ras,
    _read_glb,
    _resolve_transform,
)

TOOL = {'name': 'tools/export_clinical_payload.py', 'version': '0.1.0'}
SCHEMA = 'flowscope.clinical.viewer'
SCHEMA_VERSION = 1
VFFR_MIN, VFFR_MAX = 0.4, 1.0
FPS = 30
C_LAD_CUTOFF = 27
STATION_MM = 1.0
MAX_FILE_BYTES = 2_000_000


def _fail(msg: str) -> None:
    raise SystemExit(f'export_clinical_payload: error: {msg}')


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description='Export per-case viewer clinical payloads (contract D: '
                    'flowscope.clinical.viewer v1).'
    )
    parser.add_argument('--cases', nargs='+', default=['601', '700', '798'],
                        help='case ids (default 601 700 798)')
    parser.add_argument('--report-dir', default='out')
    parser.add_argument('--graph-dir', default='out/rom/graphs')
    parser.add_argument('--hemo-dir', default='out/rom/hemodynamics')
    parser.add_argument('--rxi-dir', default='out/clinical/rxi')
    parser.add_argument('--lesions-dir', default='out/clinical/lesions')
    parser.add_argument('--glb-dir', default='viewer/public/assets')
    parser.add_argument('--models-out', default='viewer/models/clinical')
    parser.add_argument('--public-out', default='viewer/public/clinical')
    return parser.parse_args(argv)


def _load_json(path: Path) -> dict:
    if not path.exists():
        _fail(f'{path}: missing')
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        _fail(f'{path}: invalid JSON ({exc})')


def _resolve_input(directory: Path, names: list[str], what: str) -> Path:
    """First existing candidate name in ``directory`` (case_ prefix and bare both probed)."""
    for name in names:
        path = directory / name
        if path.exists():
            return path
    _fail(f'{what}: none of {names} found in {directory}')


def _round(x, nd: int) -> float:
    return round(float(x), nd)


def _frames(t_s: float) -> int:
    """TIMI frames at 30 fps (contract: round(t x 30), half away from zero;
    1e-9 epsilon keeps exact halves above the floor despite float fuzz)."""
    return int(math.floor(t_s * FPS + 0.5 + 1e-9))


def _vffr_scalar(v: float) -> float:
    return _round(min(max(float(v), 1e-3), 1.0), 3)


def _ras_to_model(x_mm: np.ndarray, tr: dict) -> np.ndarray:
    """Forward map ``y_m = x_mm[:, perm] * sign * scale_m_per_mm - center_m``:
    the exact model-frame meters that ``_model_to_ras`` maps back to RAS mm."""
    scale = np.asarray(tr['sign'], dtype=np.float64) * float(tr['scale_m_per_mm'])
    return x_mm[:, tr['perm']] * scale - np.asarray(tr['center_m'], dtype=np.float64)


def _edge_arc(edge: dict) -> np.ndarray:
    arc = edge.get('arc_mm')
    if isinstance(arc, list):
        a = np.asarray(arc, dtype=np.float64)
    else:
        a = np.linspace(0.0, float(edge['length_mm']), len(edge['points_mm']))
    return a - a[0]


def _edge_radius(edge: dict) -> np.ndarray:
    radius = edge.get('radius_mm')
    if isinstance(radius, list):
        return np.asarray(radius, dtype=np.float64)
    return np.full(len(edge['points_mm']), float(radius))


def _stations(length_mm: float) -> np.ndarray:
    """1 mm stations from 0 covering [0, length_mm] end-to-end (exact tip appended)."""
    n = int(math.floor(length_mm / STATION_MM + 1e-9))
    s = [i * STATION_MM for i in range(n + 1)]
    if length_mm - s[-1] > 1e-6:
        s.append(float(length_mm))
    return np.asarray(s, dtype=np.float64)


def _resample(edge: dict, s: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Edge centerline and radius sampled at edge-local arc positions ``s`` (mm)."""
    arc = _edge_arc(edge)
    pts = np.asarray(edge['points_mm'], dtype=np.float64)
    sc = np.clip(s, arc[0], arc[-1])
    xyz = np.stack([np.interp(sc, arc, pts[:, c]) for c in range(3)], axis=1)
    return xyz, np.interp(sc, arc, _edge_radius(edge))


class _EdgeIndex:
    """Graph-edge lookup with exact / casefold / alnum fallbacks."""

    def __init__(self, edges: list[dict]):
        self._tables = ({}, {}, {})
        for edge in edges:
            name = edge.get('name')
            if not isinstance(name, str):
                _fail('graph edges[]: entry without a string "name"')
            for table, key in zip(self._tables,
                                  (name, name.casefold(), _alnum(name))):
                table.setdefault(key, edge)

    def get(self, name: str) -> dict:
        for table, key in zip(self._tables, (name, name.casefold(), _alnum(name))):
            hit = table.get(key)
            if hit is not None:
                return hit
        _fail(f'graph edges[]: no edge named {name!r}')


def _hyper_vffr(clin: dict, name: str) -> float:
    """Contract B hyperemic distal vFFR of one branch (= its to_node P / Pa)."""
    rec = ((clin.get('hyper') or {}).get('branches') or {}).get(name)
    if rec is None or not isinstance(rec.get('vffr'), (int, float)):
        _fail(f'clinical.hyper.branches[{name}].vffr: expected a number')
    return float(rec['vffr'])


def _case_payload(case: str, args, t0: float) -> dict:
    glb_name = f'coronary_{case}.glb'
    candidates = [Path(args.glb_dir) / glb_name, Path('out') / glb_name]
    glb_path = next((p for p in candidates if p.exists()), None)
    if glb_path is None:
        _fail(f'{glb_name}: not found in any of '
              + ', '.join(str(p.parent) for p in candidates))
    report_path = Path(args.report_dir) / f'report_{case}.json'
    graph_path = Path(args.graph_dir) / f'{case}_graph.json'
    hemo_path = Path(args.hemo_dir) / case / 'hemodynamics.json'
    rxi_path = _resolve_input(
        Path(args.rxi_dir), [f'case_{case}_rxi.json', f'{case}_rxi.json'], 'contract C rxi',
    )
    lesions_path = _resolve_input(
        Path(args.lesions_dir), [f'case_{case}_lesions.json', f'{case}_lesions.json'],
        'contract A lesions',
    )

    report = _load_json(report_path)          # hard-errors when absent
    transform = _resolve_transform(report, None, report_path)  # ... or without transform
    graph = _load_json(graph_path)
    hemo = _load_json(hemo_path)
    rxi = _load_json(rxi_path)
    lesions_doc = _load_json(lesions_path)

    clin = hemo.get('clinical')
    if not isinstance(clin, dict):
        _fail(f'{hemo_path}: no "clinical" block (run the C2 clinical extension first)')
    edges = graph.get('edges') or []
    if not edges:
        _fail(f'{graph_path}: no edges[]')
    edge_index = _EdgeIndex(edges)

    meshes, v_m, _triangles = _read_glb(glb_path)
    vertices = int(v_m.shape[0])
    x_mm = _model_to_ras(v_m, transform)

    # ---- 1 mm centerline stations over every edge + per-station vFFR
    rows = _branches_meta(hemo, graph, 'A')   # C2 branch naming (contract C5 helper)
    flag_index = _edge_flags(graph)
    paths = rxi.get('paths') or []
    path_of_edge: dict[str, tuple[dict, float]] = {}
    for path in paths:
        offset = 0.0
        for name in path.get('branch_ids') or []:
            path_of_edge.setdefault(name, (path, offset))
            offset += float(edge_index.get(name)['length_mm'])

    st_xyz, st_vffr, branch_geom = [], [], {}
    for row in rows:
        name = row['name']
        edge = edge_index.get(name)
        length = float(edge['length_mm'])
        s = _stations(length)
        xyz, radius = _resample(edge, s)
        hit = path_of_edge.get(name)
        if hit is not None:
            path, offset = hit
            pull = path['pullback']
            s_arr = np.asarray([row['s_mm'] for row in pull], dtype=np.float64)
            v_arr = np.asarray([row['vFFR'] for row in pull], dtype=np.float64)
            vffr = np.interp(offset + s, s_arr, v_arr,
                             left=v_arr[0], right=v_arr[-1])
        else:
            vffr = np.full(s.size, _hyper_vffr(clin, name))
        st_xyz.append(xyz)
        st_vffr.append(vffr)
        branch_geom[name] = (s, xyz, radius, edge, row)

    st_xyz = np.concatenate(st_xyz, axis=0)
    st_vffr = np.concatenate(st_vffr, axis=0)
    tree = cKDTree(st_xyz)
    _, nearest = tree.query(x_mm, k=1)
    v_vert = np.clip(st_vffr[nearest], VFFR_MIN, VFFR_MAX)
    u8 = np.rint((v_vert - VFFR_MIN) / (VFFR_MAX - VFFR_MIN) * 255.0).astype(np.uint8)
    vertex_vffr_u8 = base64.b64encode(u8.tobytes()).decode('ascii')

    # ---- lesions (contract A geometry + contract B hyperemic metrics)
    clin_lesions = clin.get('lesions') or {}
    lesions = []
    for lesion in lesions_doc.get('lesions') or []:
        for key in ('lesion_id', 'branch_id', 's_mm', 'length_mm', 'as_pct',
                    'dia_reduction_pct', 'd_ref_mm', 'd_min_mm'):
            if key not in lesion:
                _fail(f'{lesions_path}: lesion entry missing {key!r}')
        metrics = clin_lesions.get(lesion['lesion_id'])
        if not isinstance(metrics, dict):
            _fail(f'{hemo_path}: clinical.lesions has no entry for {lesion["lesion_id"]!r}')
        for key in ('dp_hyper_mmHg', 'vffr'):
            if not isinstance(metrics.get(key), (int, float)):
                _fail(f'{hemo_path}: clinical.lesions[{lesion["lesion_id"]}].{key}: '
                      f'expected a number')
        edge = edge_index.get(lesion['branch_id'])
        xyz, _ = _resample(edge, np.asarray([float(lesion['s_mm'])]))
        loc = _ras_to_model(xyz, transform)[0]
        lesions.append({
            'lesion_id': lesion['lesion_id'],
            'branch_id': lesion['branch_id'],
            's_mm': _round(lesion['s_mm'], 3),
            'length_mm': _round(lesion['length_mm'], 3),
            'as_pct': _round(lesion['as_pct'], 3),
            'dia_reduction_pct': _round(lesion['dia_reduction_pct'], 3),
            'd_ref_mm': _round(lesion['d_ref_mm'], 3),
            'd_min_mm': _round(lesion['d_min_mm'], 3),
            'dp_hyper_mmHg': _round(metrics['dp_hyper_mmHg'], 2),
            'vffr': _vffr_scalar(metrics['vffr']),
            'location_xyz_ras': [_round(v, 6) for v in loc],
        })

    # ---- branches (C2 naming via _branches_meta, C1 geometry + flags)
    branches = []
    for row in rows:
        name = row['name']
        s, xyz, radius, _edge, _ = branch_geom[name]
        rest_rec = ((clin.get('rest') or {}).get('branches') or {}).get(name)
        if rest_rec is None or not isinstance(rest_rec.get('pd_pa'), (int, float)):
            _fail(f'{hemo_path}: clinical.rest.branches[{name}].pd_pa: expected a number')
        flags = _lookup_flags(flag_index, name)
        t_arr = (None if row['transit_ms'] is None else _round(row['transit_ms'] * 1e-3, 3))
        xyz_model = _ras_to_model(xyz, transform)
        branches.append({
            'branch_id': name,
            'label': name,
            'vffr': _vffr_scalar(_hyper_vffr(clin, name)),
            'pd_pa_rest': _round(rest_rec['pd_pa'], 3),
            't_arr_s': t_arr,
            'tfc_frames': None if t_arr is None else _frames(t_arr),
            'd_mm': _round(float(np.mean(2.0 * radius)), 3),
            'major': bool(flags.get('major', False)),
            'distal': bool(flags.get('distal', False)),
            'centerline_ras': [[_round(v, 6) for v in p] for p in xyz_model],
            'centerline_s_mm': [_round(v, 3) for v in s],
        })

    # ---- pullbacks (contract C columns) + TFC landmarks
    pullbacks = [
        {
            'path_id': path['path_id'],
            'label': path['label'],
            'branch_ids': path['branch_ids'],
            's_mm': [_round(row['s_mm'], 3) for row in path['pullback']],
            'd_mm': [_round(row['d_mm'], 3) for row in path['pullback']],
            'P_rest_mmHg': [_round(row['P_rest_mmHg'], 2) for row in path['pullback']],
            'P_hyper_mmHg': [_round(row['P_hyper_mmHg'], 2) for row in path['pullback']],
            'vFFR': [_vffr_scalar(row['vFFR']) for row in path['pullback']],
        }
        for path in paths
    ]

    generated = datetime.now(timezone.utc).isoformat()
    return {
        'schema': SCHEMA,
        'schema_version': SCHEMA_VERSION,
        'tool': {
            **TOOL,
            'command': ' '.join(sys.argv),
            'generated': generated,
            'runtime_s': _round(time.perf_counter() - t0, 3),
        },
        'generated': generated,
        'case': case,
        'model': glb_name,
        'vertices': vertices,
        'vffr_scale': {'min': VFFR_MIN, 'max': VFFR_MAX},
        'vertex_vffr_u8': vertex_vffr_u8,
        'meshes': [
            {'node': m['node'], 'primitive_index': m['primitive_index'],
             'vertex_offset': m['vertex_offset'], 'vertex_count': m['vertex_count']}
            for m in meshes
        ],
        'lesions': lesions,
        'branches': branches,
        'pullbacks': pullbacks,
        'tfc': {
            'fps': FPS,
            'c_lad_cutoff': C_LAD_CUTOFF,
            'landmarks': rxi.get('landmarks') or [],
        },
    }


def main(argv=None) -> int:
    args = parse_args(argv)
    models_out = Path(args.models_out)
    public_out = Path(args.public_out)
    for case in args.cases:
        t0 = time.perf_counter()
        doc = _case_payload(case, args, t0)
        text = json.dumps(doc, indent=2) + '\n'
        n_bytes = len(text.encode('utf-8'))
        if n_bytes > MAX_FILE_BYTES:
            _fail(f'case_{case}_clinical.json: {n_bytes / 1e6:.3f} MB exceeds the '
                  f'{MAX_FILE_BYTES / 1e6:.0f} MB ceiling')
        for out_dir in (models_out, public_out):
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = out_dir / f'case_{case}_clinical.json'
            out_path.write_text(text)
        print(f'{models_out / f"case_{case}_clinical.json"} + public mirror: '
              f'{doc["vertices"]} vertices, {len(doc["lesions"])} lesions, '
              f'{len(doc["branches"])} branches, {n_bytes / 1e6:.3f} MB')
    return 0


if __name__ == '__main__':
    sys.exit(main())
