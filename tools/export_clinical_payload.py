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

Per-vertex vFFR: every vertex is first associated to its OWN branch (the graph
edge whose centerline polyline is exactly nearest, point-to-segment) and then to
the nearest 1 mm centerline station of that branch only (per-branch cKDTree, like
the contrast exporter's sampling) — a side-branch vertex can never be stolen by a
neighbouring branch's ostial station, and every graph edge contributes stations
(no unassigned indices). The station's vFFR comes from the rxi pullback when its
edge lies on a path (interp at the station), else the branch's hyperemic distal
vFFR. Per-branch vertex counts are printed as the association proof.
Quantization: ``u8 = round((clip(vffr, 0.4, 1.0) - 0.4) / 0.6 * 255)``.

Contract E (standardized node fields): every branches[]/lesions[] row carries the
quartet ``p_aorta_mmHg`` (clinical.pa_mmHg), ``p_distal_mmHg`` (branch: solved
hyperemic p_dist_mmHg, rest fallback; lesion: the Pd behind its vffr =
vffr * pa), ``delta_p_mmHg`` (branch: pa - p_distal; lesion: Young-Tsai
dp_hyper_mmHg) and ``vffr``; per-branch rows add ``transit_s`` (alias of
``t_arr_s``), ``transit_calibrated_s`` (signals.A transit_calibrated_ms),
``tfc_calibrated_frames`` and ``u_m_s`` (clinical.hyper.branches u_m_s).
Contract G: ``pullbacks[]`` carries every rxi path — primary ``paths[]`` then
``side_paths[]`` — each flagged with ``primary`` and columnar
``s_mm/d_mm/P_rest_mmHg/P_hyper_mmHg/vFFR/delta_p_mmHg`` where
``delta_p_mmHg[i] = p_aorta_mmHg - P_hyper_mmHg[i]``.

Rounding: coords 1e-6 m, pressures 0.01 mmHg, vFFR/Pd/Pa 0.001, geometry/times
0.001, velocity 1e-6 m/s. Hard failure over 2 MB per emitted file.
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


def _nearest_branch(x_mm: np.ndarray, edges: list[dict], chunk: int = 2048
                    ) -> tuple[np.ndarray, np.ndarray]:
    """Exact per-vertex own-branch association: for each vertex the index of the
    graph edge whose centerline polyline (point-to-segment) is nearest, plus that
    distance in mm. Chunked over vertices to bound the temporaries."""
    best_d = np.full(x_mm.shape[0], np.inf, dtype=np.float64)
    best_i = np.zeros(x_mm.shape[0], dtype=np.int64)
    for i, edge in enumerate(edges):
        pts = np.asarray(edge['points_mm'], dtype=np.float64)
        a, b = pts[:-1], pts[1:]
        ab = b - a
        ab2 = np.maximum(np.einsum('ij,ij->i', ab, ab), 1e-18)
        for lo in range(0, x_mm.shape[0], chunk):
            x = x_mm[lo:lo + chunk]
            t = np.clip(np.einsum('nsc,sc->ns', x[:, None, :] - a[None], ab) / ab2,
                        0.0, 1.0)
            proj = a[None] + t[:, :, None] * ab[None]
            d = np.sqrt(((x[:, None, :] - proj) ** 2).sum(axis=2)).min(axis=1)
            bd = best_d[lo:lo + chunk]   # views: the updates land in best_d/best_i
            bi = best_i[lo:lo + chunk]
            sel = d < bd
            bd[sel] = d[sel]
            bi[sel] = i
    return best_i, best_d


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

    # ---- 1 mm centerline stations over every graph edge + per-station vFFR
    rows = _branches_meta(hemo, graph, 'A')   # C2 branch naming (contract C5 helper)
    flag_index = _edge_flags(graph)
    paths = rxi.get('paths') or []
    side_paths = rxi.get('side_paths') or []
    all_paths = [(path, True) for path in paths] + [(path, False) for path in side_paths]
    path_of_edge: dict[str, tuple[dict, float]] = {}
    for path, _primary in all_paths:
        offset = 0.0
        for name in path.get('branch_ids') or []:
            path_of_edge.setdefault(name, (path, offset))
            offset += float(edge_index.get(name)['length_mm'])

    edge_stations, branch_geom = [], {}
    for edge in edges:
        name = edge['name']
        s = _stations(float(edge['length_mm']))
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
        edge_stations.append((xyz, vffr))
        branch_geom[name] = (s, xyz, radius, edge)

    # Vertex association: every vertex is bound to its OWN branch first (exact
    # point-to-segment distance over every graph edge) and only then to that
    # branch's nearest 1 mm station (per-branch cKDTree). A side-branch vertex
    # can never be stolen by a neighbouring branch's ostial station, and since
    # all edges contribute stations there is no unassigned index either.
    branch_of_vertex, _dist = _nearest_branch(x_mm, edges)
    v_vert = np.empty(vertices, dtype=np.float64)
    assoc_counts = []
    for i, edge in enumerate(edges):
        sel = np.nonzero(branch_of_vertex == i)[0]
        assoc_counts.append((edge['name'], int(sel.size)))
        if sel.size == 0:
            continue
        st_xyz, st_vffr = edge_stations[i]
        tree = cKDTree(st_xyz)
        _, nearest = tree.query(x_mm[sel], k=1)
        v_vert[sel] = st_vffr[nearest]
    print('  vertex->branch: '
          + ', '.join(f'{name}={count}' for name, count in assoc_counts))
    v_vert = np.clip(v_vert, VFFR_MIN, VFFR_MAX)
    u8 = np.rint((v_vert - VFFR_MIN) / (VFFR_MAX - VFFR_MIN) * 255.0).astype(np.uint8)
    vertex_vffr_u8 = base64.b64encode(u8.tobytes()).decode('ascii')

    # ---- contract E: mean aortic pressure (clinical.pa_mmHg; derivation from
    # the solved mean inlet pressure when the clinical block lacks the key)
    pa = clin.get('pa_mmHg')
    if not isinstance(pa, (int, float)):
        pa = (hemo.get('physiology') or {}).get('p_mean_mmHg')
    if not isinstance(pa, (int, float)):
        _fail(f'{hemo_path}: clinical.pa_mmHg: expected a number')
    p_aorta = _round(pa, 2)

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
        dp_hyper = _round(metrics['dp_hyper_mmHg'], 2)
        lesions.append({
            'lesion_id': lesion['lesion_id'],
            'branch_id': lesion['branch_id'],
            's_mm': _round(lesion['s_mm'], 3),
            'length_mm': _round(lesion['length_mm'], 3),
            'as_pct': _round(lesion['as_pct'], 3),
            'dia_reduction_pct': _round(lesion['dia_reduction_pct'], 3),
            'd_ref_mm': _round(lesion['d_ref_mm'], 3),
            'd_min_mm': _round(lesion['d_min_mm'], 3),
            'dp_hyper_mmHg': dp_hyper,
            'p_aorta_mmHg': p_aorta,
            # the Pd sampled >= 20 mm distal of the lesion, i.e. the same solver
            # value behind its vffr (vffr = Pd / Pa)
            'p_distal_mmHg': _round(float(metrics['vffr']) * pa, 2),
            'delta_p_mmHg': dp_hyper,
            'vffr': _vffr_scalar(metrics['vffr']),
            'location_xyz_ras': [_round(v, 6) for v in loc],
        })

    # ---- branches (C2 naming via _branches_meta, C1 geometry + flags +
    # contract E standardized quartet and calibrated transit fields)
    branches = []
    for row in rows:
        name = row['name']
        edge = edge_index.get(name)
        s, xyz, radius, _edge = branch_geom[edge['name']]
        rest_rec = ((clin.get('rest') or {}).get('branches') or {}).get(name)
        if rest_rec is None or not isinstance(rest_rec.get('pd_pa'), (int, float)):
            _fail(f'{hemo_path}: clinical.rest.branches[{name}].pd_pa: expected a number')
        hyper_rec = ((clin.get('hyper') or {}).get('branches') or {}).get(name) or {}
        p_dist = hyper_rec.get('p_dist_mmHg')
        if not isinstance(p_dist, (int, float)):
            p_dist = rest_rec.get('p_dist_mmHg')   # contract E fallback: rest p_dist
        p_distal = None if not isinstance(p_dist, (int, float)) else _round(p_dist, 2)
        u_m_s = hyper_rec.get('u_m_s')
        flags = _lookup_flags(flag_index, name)
        t_arr = (None if row['transit_ms'] is None else _round(row['transit_ms'] * 1e-3, 3))
        t_cal = (None if row.get('transit_calibrated_ms') is None
                 else _round(row['transit_calibrated_ms'] * 1e-3, 3))
        xyz_model = _ras_to_model(xyz, transform)
        branches.append({
            'branch_id': name,
            'label': name,
            'p_aorta_mmHg': p_aorta,
            'p_distal_mmHg': p_distal,
            'delta_p_mmHg': (None if p_distal is None
                             else _round(p_aorta - p_distal, 2)),
            'vffr': _vffr_scalar(_hyper_vffr(clin, name)),
            'pd_pa_rest': _round(rest_rec['pd_pa'], 3),
            't_arr_s': t_arr,
            'transit_s': t_arr,
            'transit_calibrated_s': t_cal,
            'tfc_frames': None if t_arr is None else _frames(t_arr),
            'tfc_calibrated_frames': None if t_cal is None else _frames(t_cal),
            'u_m_s': (None if not isinstance(u_m_s, (int, float))
                      else _round(u_m_s, 6)),
            'd_mm': _round(float(np.mean(2.0 * radius)), 3),
            'major': bool(flags.get('major', False)),
            'distal': bool(flags.get('distal', False)),
            'centerline_ras': [[_round(v, 6) for v in p] for p in xyz_model],
            'centerline_s_mm': [_round(v, 3) for v in s],
        })

    # ---- pullbacks (contract C/G columns; every rxi path — primary paths[] then
    # side_paths[] — with the primary flag; delta_p_mmHg = pa - P_hyper per station)
    pullbacks = []
    for path, primary in all_paths:
        p_hyper = [_round(row['P_hyper_mmHg'], 2) for row in path['pullback']]
        pullbacks.append({
            'path_id': path['path_id'],
            'label': path['label'],
            'branch_ids': path['branch_ids'],
            'primary': bool(primary),
            's_mm': [_round(row['s_mm'], 3) for row in path['pullback']],
            'd_mm': [_round(row['d_mm'], 3) for row in path['pullback']],
            'P_rest_mmHg': [_round(row['P_rest_mmHg'], 2) for row in path['pullback']],
            'P_hyper_mmHg': p_hyper,
            'vFFR': [_vffr_scalar(row['vFFR']) for row in path['pullback']],
            'delta_p_mmHg': [_round(p_aorta - v, 2) for v in p_hyper],
        })

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
