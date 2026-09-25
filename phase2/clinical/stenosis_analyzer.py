#!/usr/bin/env python3
"""Phase-2.5 clinical stenosis analysis: intrinsic lesions + synthetic throats (contracts A/B).

Inputs (read-only):
  out/rom/graphs/{case}_graph.json    C1 centerline graph; each edge carries
      arc_mm / radius_mm lumen samples along its centerline

Outputs (contract A, flowscope.clinical.lesions v1):
  out/clinical/lesions/case_{case}_lesions.json          intrinsic run (default)
  out/clinical/lesions/case_{case}_lesions_syn{NN}.json  one file per synthetic
      insertion (NN = dia_reduction_pct, contract B)

Each edge radius profile is resampled to 0.5 mm stations covering [0, length_mm]
(linear interpolation of radius_mm along arc_mm) and scored with the area
stenosis as_pct = (1 - (r / r_ref)^2) * 100 against a moving-window proximal
maximum reference: r_ref(s) = max r over the window_mm behind the station
(the window includes the station itself), so r_ref >= r and as_pct is in
[0, 100].  At s = 0 the window is just the local radius -> as_pct 0.

Intrinsic lesions (contract B) are the contiguous station spans with
as_pct >= as_threshold_pct, one lesion per span.  Synthetic insertion overlays
a trapezoidal throat reaching d_min = d_ref * (1 - dia_reduction_pct/100) at
mid-span over length_mm (linear ramps to d_min over the outer quarters, d_min
flat over the middle half).  The intrinsic d_mm / d_ref_mm profile stays in
branches[].samples; the throat is recorded as the lesion record plus the
modified as_pct samples over its span.
"""

from __future__ import annotations

# machine spec: 8 BLAS threads, pinned before numpy loads (contract preamble)
import os

os.environ['OMP_NUM_THREADS'] = '8'
os.environ['MKL_NUM_THREADS'] = '8'
os.environ['OPENBLAS_NUM_THREADS'] = '8'

import argparse
import json
import math
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

TOOL = {'name': 'phase2/clinical/stenosis_analyzer.py', 'version': '0.1.0'}
SCHEMA = 'flowscope.clinical.lesions'
SCHEMA_VERSION = 1
STATION_STEP_MM = 0.5
MIN_LESION_LENGTH_MM = 2.0
R_REF_METHOD = (
    'moving-window proximal maximum: r_ref(s) = max r over stations in '
    '(s - window_mm, s] (window includes the station itself); at s = 0 the '
    'local radius, giving as_pct 0'
)
AS_FORMULA = 'as_pct = (1 - (d/d_ref)^2) * 100 (area stenosis); A0_over_As = 1/(1 - as_pct/100)'
LESION_ORDER = 'proximal-to-distal: (root tree index, arc distance from ostium to lesion centroid)'
SYNTHETIC_SHAPE = (
    'trapezoidal throat over length_mm: d_ref at both span ends, linear ramps '
    'over the outer quarters to d_min flat across the middle half; d_min at '
    'mid-span = d_ref * (1 - dia_reduction_pct/100)'
)


def _fail(msg: str) -> None:
    raise SystemExit(f'stenosis_analyzer: error: {msg}')


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description='Phase-2.5 clinical stenosis detection + synthetic insertion (contracts A/B).'
    )
    parser.add_argument('--cases', nargs='+', default=['601', '700', '798'],
                        help='case ids (default 601 700 798)')
    parser.add_argument('--graph-dir', default='out/rom/graphs',
                        help='directory with {case}_graph.json')
    parser.add_argument('--out-dir', default='out/clinical/lesions',
                        help='output directory for contract-A lesion JSON files')
    parser.add_argument('--window-mm', type=float, default=6.0,
                        help='proximal reference window length in mm (default 6.0)')
    parser.add_argument('--as-threshold', type=float, default=50.0,
                        help='intrinsic area-stenosis detection threshold in percent (default 50.0)')
    parser.add_argument('--insert', nargs='+', action='extend', default=None,
                        metavar='EDGE:S:DIA[:LEN]',
                        help='synthetic insertion spec(s); one syn<NN> output file each')
    parser.add_argument('--ladder', nargs='+', action='extend', type=float, default=None,
                        metavar='DIA',
                        help='synthetic diameter-reduction ladder, e.g. 50 70 90 '
                             '(needs --branch and --s); one syn<NN> file per severity')
    parser.add_argument('--branch', default=None,
                        help='edge name for --ladder (exact, casefold, or alnum-stripped)')
    parser.add_argument('--s', type=float, default=None,
                        help='throat center s_mm along --branch for --ladder')
    parser.add_argument('--length-mm', type=float, default=10.0,
                        help='synthetic throat span in mm (default 10.0)')
    args = parser.parse_args(argv)
    if args.ladder is not None and (args.branch is None or args.s is None):
        parser.error('--ladder requires --branch and --s')
    if args.ladder is not None and not args.ladder:
        parser.error('--ladder needs at least one severity')
    if args.insert is not None and not args.insert:
        parser.error('--insert needs at least one spec')
    if args.window_mm <= 0.0:
        parser.error('--window-mm must be positive')
    if not 0.0 <= args.as_threshold <= 100.0:
        parser.error('--as-threshold must be in [0, 100]')
    if args.length_mm <= 0.0:
        parser.error('--length-mm must be positive')
    return args


# --------------------------------------------------------------- edge matching


def _alnum(name: str) -> str:
    return ''.join(ch for ch in name.casefold() if ch.isalnum())


def _edge_index(graph: dict):
    """Edges indexed by name (exact, casefold, alnum-stripped fallbacks, as C1)."""
    exact: dict = {}
    folded: dict = {}
    stripped: dict = {}
    for edge in graph.get('edges', []):
        name = edge.get('name')
        if not isinstance(name, str):
            continue
        exact.setdefault(name, edge)
        folded.setdefault(name.casefold(), edge)
        stripped.setdefault(_alnum(name), edge)
    return exact, folded, stripped


def _lookup_edge(index, name: str):
    exact, folded, stripped = index
    for table, key in ((exact, name), (folded, name.casefold()), (stripped, _alnum(name))):
        edge = table.get(key)
        if edge is not None:
            return edge
    return None


# ----------------------------------------------------------------- resampling


def _stations(length_mm: float) -> np.ndarray:
    """0.5 mm stations covering [0, length_mm] (final step short when needed)."""
    n = int(math.floor(length_mm / STATION_STEP_MM))
    s = np.arange(n + 1, dtype=np.float64) * STATION_STEP_MM
    if length_mm - s[-1] > 1e-9:
        s = np.append(s, length_mm)
    else:
        s[-1] = length_mm
    return s


def _local_reference(r: np.ndarray, s: np.ndarray, window_mm: float) -> np.ndarray:
    """Moving-window proximal maximum of r over (s - window_mm, s] per station."""
    lo = np.searchsorted(s, s - window_mm, side='left')
    r_ref = np.empty_like(r)
    for i in range(r.size):
        r_ref[i] = r[lo[i]:i + 1].max()
    return r_ref


def _reference_at(r: np.ndarray, s: np.ndarray, window_mm: float, s_q: float) -> float:
    """Moving-window proximal maximum of r at an arbitrary arc position s_q."""
    j0 = int(np.searchsorted(s, s_q - window_mm, side='left'))
    j1 = int(np.searchsorted(s, s_q, side='right')) - 1
    return float(r[max(j0, j1):j1 + 1].max() if j1 >= j0 else r[j0])


def _process_edge(edge: dict, window_mm: float):
    """Stations, local radii, reference radii, and area stenosis for one edge."""
    length_mm = float(edge['length_mm'])
    arc = np.asarray(edge['arc_mm'], dtype=np.float64)
    rad = np.asarray(edge['radius_mm'], dtype=np.float64)
    if arc.size == 0 or arc.size != rad.size:
        _fail(f'{edge.get("name")}: arc_mm/radius_mm size mismatch')
    if not np.all(np.isfinite(arc)) or not np.all(np.isfinite(rad)):
        _fail(f'{edge.get("name")}: non-finite centerline samples')
    if np.any(rad <= 0.0) or np.any(np.diff(arc) < 0.0):
        _fail(f'{edge.get("name")}: non-positive radius or non-monotonic arc_mm')
    s = _stations(length_mm)
    r = np.interp(s, arc, rad)
    r_ref = _local_reference(r, s, window_mm)
    as_pct = np.clip((1.0 - (r / r_ref) ** 2) * 100.0, 0.0, 100.0)
    return s, r, r_ref, as_pct


def _branch_record(edge: dict, s: np.ndarray, d: np.ndarray,
                   d_ref: np.ndarray, as_pct: np.ndarray) -> dict:
    i_min = int(np.argmin(d))
    i_max = int(np.argmax(as_pct))
    samples = [
        {'s_mm': round(float(si), 4), 'd_mm': round(float(di), 4),
         'd_ref_mm': round(float(ri), 4), 'as_pct': round(float(ai), 4)}
        for si, di, ri, ai in zip(s, d, d_ref, as_pct)
    ]
    return {
        'branch_id': edge['name'],
        'edge_name': edge['name'],
        'length_mm': round(float(edge['length_mm']), 4),
        'd_ref_mm': round(float(d_ref.max()), 4),
        'min_d_mm': round(float(d[i_min]), 4),
        'min_s_mm': round(float(s[i_min]), 4),
        'max_as_pct': round(float(as_pct[i_max]), 4),
        'max_as_s_mm': round(float(s[i_max]), 4),
        'samples': samples,
    }


# --------------------------------------------------------------- lesion records


def _tree_positions(graph: dict) -> dict:
    """Per edge name: (root tree index, arc distance from ostium to its start)."""
    edges = graph.get('edges', [])
    roots = [n['id'] for n in graph.get('nodes', []) if n.get('kind') == 'root']
    out_by_node: dict = {}
    for edge in edges:
        out_by_node.setdefault(edge['from_node'], []).append(edge)
    pos: dict = {}
    for tree_idx, root_id in enumerate(roots):
        frontier = deque([(root_id, 0.0)])
        while frontier:
            node_id, dist = frontier.popleft()
            for edge in out_by_node.get(node_id, ()):
                name = edge['name']
                if name in pos:
                    continue
                pos[name] = (tree_idx, dist)
                frontier.append((edge['to_node'], dist + float(edge['length_mm'])))
    for order, edge in enumerate(edges):
        pos.setdefault(edge['name'], (len(roots), float(order)))
    return pos


def _intrinsic_lesions(case: str, per_edge, threshold: float, positions: dict) -> list:
    """One lesion per contiguous as_pct >= threshold span, proximal-to-distal ids."""
    entries = []
    for edge, s, d, d_ref, as_pct in per_edge:
        above = as_pct >= threshold
        i, n = 0, s.size
        while i < n:
            if not above[i]:
                i += 1
                continue
            j = i
            while j + 1 < n and above[j + 1]:
                j += 1
            w = i + int(np.argmax(as_pct[i:j + 1]))
            s_centroid = 0.5 * (float(s[i]) + float(s[j]))
            as_max = float(as_pct[w])
            d_ref_w = float(d_ref[w])
            d_w = float(d[w])
            fields = {
                'source': 'intrinsic',
                'branch_id': edge['name'],
                'edge_name': edge['name'],
                's_mm': round(s_centroid, 4),
                'length_mm': round(max(float(s[j] - s[i]), MIN_LESION_LENGTH_MM), 4),
                'dia_reduction_pct': round((1.0 - d_w / d_ref_w) * 100.0, 4),
                'as_pct': round(as_max, 4),
                'd_ref_mm': round(d_ref_w, 4),
                'd_min_mm': round(d_w, 4),
                'A0_over_As': round(1.0 / (1.0 - as_max / 100.0), 4),
            }
            tree_idx, proximal_mm = positions[edge['name']]
            key = (tree_idx, proximal_mm + s_centroid, edge['name'], s_centroid)
            entries.append((key, fields))
            i = j + 1
    entries.sort(key=lambda e: e[0])
    return [{'lesion_id': f'{case}_L{k}', **fields}
            for k, (_, fields) in enumerate(entries, 1)]


def _apply_synthetic(case: str, edge: dict, s: np.ndarray, r: np.ndarray,
                     as_pct: np.ndarray, window_mm: float,
                     s_c: float, dia_pct: float, length_mm: float):
    """Trapezoidal throat overlay on as_pct + its single synthetic lesion record."""
    if not 0.0 < dia_pct < 100.0:
        _fail(f'dia_reduction_pct {dia_pct}: must be in (0, 100)')
    span_len = float(edge['length_mm'])
    if not 0.0 <= s_c <= span_len:
        _fail(f'{edge["name"]}: s {s_c} outside [0, {span_len}]')
    span = min(float(length_mm), span_len)
    a = min(max(s_c - 0.5 * span, 0.0), span_len - span)
    b = a + span
    c = 0.5 * (a + b)
    q1, q2 = a + 0.25 * span, a + 0.75 * span

    d_ref_mm = 2.0 * _reference_at(r, s, window_mm, c)
    d_min_mm = d_ref_mm * (1.0 - dia_pct / 100.0)
    d_throat = np.interp(s, [a, q1, q2, b], [d_ref_mm, d_min_mm, d_min_mm, d_ref_mm])
    as_throat = np.clip((1.0 - (d_throat / d_ref_mm) ** 2) * 100.0, 0.0, 100.0)
    mod_as = as_pct.copy()
    inside = (s >= a) & (s <= b)
    mod_as[inside] = as_throat[inside]

    as_lesion = (1.0 - (1.0 - dia_pct / 100.0) ** 2) * 100.0
    lesion = {
        'lesion_id': f'{case}_L1',
        'source': 'synthetic',
        'branch_id': edge['name'],
        'edge_name': edge['name'],
        's_mm': round(c, 4),
        'length_mm': round(b - a, 4),
        'dia_reduction_pct': round(float(dia_pct), 4),
        'as_pct': round(as_lesion, 4),
        'd_ref_mm': round(d_ref_mm, 4),
        'd_min_mm': round(d_min_mm, 4),
        'A0_over_As': round(1.0 / (1.0 - dia_pct / 100.0) ** 2, 4),
    }
    insertion = {
        'edge_name': edge['name'],
        's_mm': round(s_c, 4),
        'dia_reduction_pct': round(float(dia_pct), 4),
        'length_mm': round(float(length_mm), 4),
    }
    return mod_as, lesion, insertion


# ----------------------------------------------------------------- file output


def _payload(case: str, graph_path: Path, params: dict, branches: list,
             lesions: list, t_file: float, generated: str) -> dict:
    return {
        'schema': SCHEMA,
        'schema_version': SCHEMA_VERSION,
        'case': case,
        'tool': {
            **TOOL,
            'command': ' '.join(sys.argv),
            'generated': generated,
            'runtime_s': time.perf_counter() - t_file,
        },
        'generated': generated,
        'source_graph': str(graph_path),
        'params': params,
        'branches': branches,
        'lesions': lesions,
    }


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, allow_nan=False))


def _dia_tag(dia_pct: float) -> str:
    return f'{int(dia_pct):02d}' if float(dia_pct).is_integer() else f'{dia_pct:g}'


def _parse_insert(token: str, default_len: float):
    parts = token.split(':')
    if len(parts) not in (3, 4):
        _fail(f'--insert {token!r}: expected EDGE:S_MM:DIA_PCT[:LENGTH_MM]')
    try:
        s_c, dia = float(parts[1]), float(parts[2])
        length = float(parts[3]) if len(parts) == 4 else default_len
    except ValueError:
        _fail(f'--insert {token!r}: non-numeric S_MM/DIA_PCT/LENGTH_MM')
    return parts[0], s_c, dia, length


# ------------------------------------------------------------------------- main


def main(argv=None) -> int:
    t0 = time.perf_counter()
    args = parse_args(argv)
    cases = [str(c) for c in args.cases]
    out_dir = Path(args.out_dir)

    graphs = {}
    for case in cases:
        graph_path = Path(args.graph_dir) / f'{case}_graph.json'
        if not graph_path.exists():
            _fail(f'{graph_path}: missing')
        graphs[case] = (graph_path, json.loads(graph_path.read_text()))

    base_params = {
        'window_mm': float(args.window_mm),
        'as_threshold_pct': float(args.as_threshold),
        'r_ref_method': R_REF_METHOD,
        'station_step_mm': STATION_STEP_MM,
        'as_formula': AS_FORMULA,
    }
    generated = datetime.now(timezone.utc).isoformat()

    if args.insert is None and args.ladder is None:
        for case in cases:
            t_file = time.perf_counter()
            graph_path, graph = graphs[case]
            per_edge = []
            for edge in graph['edges']:
                s, r, r_ref, as_pct = _process_edge(edge, args.window_mm)
                per_edge.append((edge, s, 2.0 * r, 2.0 * r_ref, as_pct))
            branches = [_branch_record(e, s, d, d_ref, a) for e, s, d, d_ref, a in per_edge]
            lesions = _intrinsic_lesions(case, per_edge, args.as_threshold,
                                         _tree_positions(graph))
            params = {**base_params, 'lesion_order': LESION_ORDER, 'mode': 'intrinsic'}
            payload = _payload(case, graph_path, params, branches, lesions,
                               t_file, generated)
            out_path = out_dir / f'case_{case}_lesions.json'
            _write_json(out_path, payload)
            n_samples = sum(len(b['samples']) for b in branches)
            print(f'{out_path}: {len(branches)} branches, {n_samples} samples, '
                  f'{len(lesions)} intrinsic lesions')
        return 0

    # synthetic mode: one output file per insertion / ladder severity
    specs = [_parse_insert(tok, args.length_mm) for tok in (args.insert or [])]
    if args.ladder is not None:
        specs.extend((args.branch, args.s, float(dia), args.length_mm)
                     for dia in args.ladder)
    indexes = {case: _edge_index(graph) for case, (_, graph) in graphs.items()}

    used_names = set()
    for name, s_c, dia_pct, length_mm in specs:
        t_file = time.perf_counter()
        hits = [(case, graphs[case][0], graphs[case][1],
                 _lookup_edge(indexes[case], name))
                for case in cases]
        hits = [h for h in hits if h[3] is not None]
        if not hits:
            _fail(f'edge {name!r}: not found in cases {cases}')
        if len(hits) > 1:
            _fail(f'edge {name!r}: ambiguous across cases {[h[0] for h in hits]}')
        case, graph_path, graph, edge = hits[0]

        s, r, r_ref, as_pct = _process_edge(edge, args.window_mm)
        mod_as, lesion, insertion = _apply_synthetic(
            case, edge, s, r, as_pct, args.window_mm, s_c, dia_pct, length_mm)
        branches = [_branch_record(edge, s, 2.0 * r, 2.0 * r_ref, mod_as)]
        params = {
            **base_params,
            'mode': 'synthetic',
            'synthetic_shape': SYNTHETIC_SHAPE,
            'insertion': insertion,
        }
        payload = _payload(case, graph_path, params, branches, [lesion],
                           t_file, generated)
        tag = _dia_tag(dia_pct)
        stem = f'case_{case}_lesions_syn{tag}'
        out_name = f'{stem}.json'
        k = 2
        while out_name in used_names:
            out_name = f'{stem}_{k}.json'
            k += 1
        used_names.add(out_name)
        out_path = out_dir / out_name
        _write_json(out_path, payload)
        print(f'{out_path}: synthetic {dia_pct:g}% throat on {edge["name"]} '
              f'@ {lesion["s_mm"]:.4f} mm (as_pct {lesion["as_pct"]:.2f}, '
              f'A0/As {lesion["A0_over_As"]:.4f})')
    return 0


if __name__ == '__main__':
    sys.exit(main())
