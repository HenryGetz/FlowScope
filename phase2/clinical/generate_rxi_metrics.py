#!/usr/bin/env python3
"""Generate RXi clinical metrics: hyperemic pullback, dP/ds, TFC (contract ``flowscope.clinical.rxi`` v1).

Inputs (read-only):
  out/rom/graphs/{case}_graph.json                  C1 root-to-tip centerline DAG (RAS mm)
  out/rom/hemodynamics/{case}/hemodynamics.json     C2 ``clinical`` rest/hyper per-edge pressures
                                                    and ``signals.<P>.branches`` transit times
  out/clinical/lesions/case_{case}_lesions.json     contract A stenosis measurements

Output (one per case):
  out/clinical/rxi/case_{case}_rxi.json             flowscope.clinical.rxi v1

Method (contract C):
  * paths    : the 3 longest root-to-tip edge chains of the C1 DAG (walk over
               parent_edge/child_edges), branch_ids proximal -> distal.
  * side_paths (contract F): every remaining root-to-tip chain (path_4..N),
               same item shape as ``paths[]``; their pullback rows add the
               contract E column ``delta_p_mmHg = Pa - P_hyper_mmHg`` at source
               precision.  ``paths[]`` stays byte-stable (its rows keep exactly
               s_mm/d_mm/P_rest_mmHg/P_hyper_mmHg/vFFR) and dp_ds / landmarks /
               transit_anomaly / tfc remain primary-only.  The union of
               ``branch_ids`` over paths[] + side_paths[] must equal every graph
               edge name (asserted).
  * pullback : 1 mm stations from the ostium to the distal tip (plus exact
               tip and edge-junction stations). P_rest/P_hyper redistribute each
               edge's solved C2 drop (its coupled stenosis loss is already inside
               it): each lesion span carries its Young-Tsai share
               ``dP = Kv*mu/r0^2*u*L + Kt*rho/2*(A0/As-1)^2*u^2`` with
               ``Kv = 32*(L/D0)*(A0/As)^2``, ``Kt = 1.52``, ``mu = 0.0035 Pa*s``,
               ``rho = 1050 kg/m^3`` and ``u = solved q_ml_s / throat area`` as a
               smoothstep CDF (bounded by the edge's solved drop), while the
               residual drop runs a constant gradient outside the spans: the
               solved drop is conserved exactly and P meets the solved node
               pressures at every edge junction. ``vFFR = P_hyper / Pa``
               (unclipped; P in (0, Pa]).
               ``d_mm`` is 2x the C1 radius profile with the contract A throat
               trapezoid overriding it inside each lesion span: ``d_ref_mm`` at
               both span ends, linear ramps over the outer quarters, the measured
               ``d_min_mm`` flat over the middle half of the span.
  * dp_ds    : mmHg/cm gradients of the hyperemic pullback; ``focal`` when
               max|dP/ds| > 3*median|dP/ds| and the top decile of |dP/ds| spans
               < 30% of the path length, else ``diffuse``.
  * TFC      : profile-A branch transit times (``signals.A.branches[..].transit_ms``),
               frames at 30 fps; distal calibrated arrivals from
               ``signals.A.branches[tip].transit_calibrated_ms`` (fallback
               ``raw/kappa``) — the microcirculation-inclusive path-cumulative
               arrival, never the epicardial convective
               ``clinical.velocity_calibration.transit_calibrated_s`` entries.
               A calibrated distal transit > 5.0 s flags
               "TIMI 1/2: severe microvascular resistance or near-total flow
               arrest" (pathology, not calibration error); a raw distal transit
               > 5.0 s that velocity calibration pulls back under the threshold
               is noted in ``reason`` and never flagged.

Rounding: vFFR/Pd/Pa 0.001, geometry (s_mm/d_mm) 0.001, dP/ds 0.001; pullback
P_rest/P_hyper and transit_anomaly distal times are carried at source precision
so junction pressures and calibrated arrivals reproduce the solved/signal values
exactly (contract checks at 1e-6).
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
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

# proven helpers from tools/export_cfd_payload.py (imported read-only; never modified)
sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'tools')
)
from export_cfd_payload import _alnum  # noqa: E402  (path shim must run first)

TOOL = {'name': 'phase2/clinical/generate_rxi_metrics.py', 'version': '0.1.0'}
SCHEMA = 'flowscope.clinical.rxi'
SCHEMA_VERSION = 1

STATION_MM = 1.0          # pullback / centerline station spacing
FPS = 30                  # TIMI frame rate (contract C tfc.fps)
C_LAD_CUTOFF = 27         # TIMI frame count cutoff for the C-LAD (contract C)
TIMI_FLAG = 'TIMI 1/2: severe microvascular resistance or near-total flow arrest'
CALIBRATED_DISTAL_LIMIT_S = 5.0

# Young-Tsai stenosis loss (same constants as the C2 clinical block)
MU_PA_S = 0.0035
RHO_KG_M3 = 1050.0
KT_STENOSIS = 1.52
PA_PER_MMHG = 133.322368  # == phase2/rom/run_zerod.py MMHG


def _fail(msg: str) -> None:
    raise SystemExit(f'generate_rxi_metrics: error: {msg}')


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description='Generate per-case RXi clinical metrics (contract C: '
                    'flowscope.clinical.rxi v1).'
    )
    parser.add_argument('--cases', nargs='+', default=['601', '700', '798'],
                        help='case ids (default 601 700 798)')
    parser.add_argument('--graph-dir', default='out/rom/graphs')
    parser.add_argument('--hemo-dir', default='out/rom/hemodynamics')
    parser.add_argument('--lesions-dir', default='out/clinical/lesions')
    parser.add_argument('--out-dir', default='out/clinical/rxi')
    parser.add_argument('--profiles', nargs='+', default=['A', 'B', 'C'],
                        help='injection profiles present in hemodynamics.json; the '
                             'first one supplies the TFC/landmark transit times')
    return parser.parse_args(argv)


# ------------------------------------------------------------------- input load


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


class _NameIndex:
    """Edge-name lookup with exact / casefold / alnum fallbacks (contrast exporter style)."""

    def __init__(self, items, what: str):
        self._exact: dict = {}
        self._folded: dict = {}
        self._stripped: dict = {}
        self._what = what
        for item in items:
            name = item.get('name') if isinstance(item, dict) else None
            if not isinstance(name, str):
                _fail(f'{what}: entry without a string "name"')
            for table, key in ((self._exact, name), (self._folded, name.casefold()),
                               (self._stripped, _alnum(name))):
                table.setdefault(key, item)

    def get(self, name: str):
        for table, key in ((self._exact, name), (self._folded, name.casefold()),
                           (self._stripped, _alnum(name))):
            hit = table.get(key)
            if hit is not None:
                return hit
        _fail(f'{self._what}: no entry named {name!r}')


def _edge_radius(edge: dict) -> np.ndarray:
    """Per-vertex radius profile of one C1 edge (scalar radius tolerated)."""
    radius = edge.get('radius_mm')
    if isinstance(radius, list):
        return np.asarray(radius, dtype=np.float64)
    if isinstance(radius, (int, float)):
        n = len(edge.get('points_mm') or [0])
        return np.full(n, float(radius))
    _fail(f'edge {edge.get("name")!r}: malformed radius_mm')


def _edge_arc(edge: dict) -> np.ndarray:
    arc = edge.get('arc_mm')
    if isinstance(arc, list):
        a = np.asarray(arc, dtype=np.float64)
    else:
        a = np.linspace(0.0, float(edge['length_mm']), len(_edge_radius(edge)))
    return a - a[0]


# ---------------------------------------------------------------------- geometry


def _root_to_tip_chains(edges: list[dict]) -> list[list[dict]]:
    """Every root->tip edge chain (DAG walk over parent_edge/child_edges)."""
    by_id = {}
    for edge in edges:
        if 'id' not in edge:
            _fail(f'edge {edge.get("name")!r}: missing "id"')
        by_id[int(edge['id'])] = edge

    def walk(edge: dict, prefix: tuple, acc: list) -> list[list[dict]]:
        eid = int(edge['id'])
        if eid in prefix:
            _fail(f'edge {edge["name"]!r}: cycle in parent/child edge links')
        chain = acc + [edge]
        kids = edge.get('child_edges') or []
        if not kids:
            return [chain]
        out: list[list[dict]] = []
        for kid in kids:
            child = by_id.get(int(kid))
            if child is None:
                _fail(f'edge {edge["name"]!r}: child edge id {kid} not in edges[]')
            out.extend(walk(child, prefix | {eid}, chain))
        return out

    roots = [e for e in edges if e.get('parent_edge') is None]
    if not roots:
        _fail('graph has no root edge (every edge has a parent_edge)')
    chains: list[list[dict]] = []
    for root in roots:
        chains.extend(walk(root, frozenset(), []))
    return chains


def _order_chains(chains: list[list[dict]]) -> list[list[dict]]:
    """Root-to-tip chains longest-first; ties broken deterministically by branch names."""
    def total(chain):
        return sum(float(e['length_mm']) for e in chain)

    return sorted(chains, key=lambda c: (-total(c), tuple(e['name'] for e in c)))


def _stations(length_mm: float) -> np.ndarray:
    """1 mm stations from 0 covering [0, length_mm] end-to-end (exact tip appended)."""
    n = int(math.floor(length_mm / STATION_MM + 1e-9))
    s = [i * STATION_MM for i in range(n + 1)]
    if length_mm - s[-1] > 1e-6:
        s.append(float(length_mm))
    return np.asarray(s, dtype=np.float64)


def _edge_index_at(offsets: np.ndarray, s: float) -> int:
    return int(min(np.searchsorted(offsets[1:], s, side='right'), offsets.size - 2))


def _diameter_at(edge: dict, x_mm: float) -> float:
    arc = _edge_arc(edge)
    return float(2.0 * np.interp(np.clip(x_mm, arc[0], arc[-1]), arc, _edge_radius(edge)))


def _smoothstep(u: np.ndarray) -> np.ndarray:
    u = np.clip(u, 0.0, 1.0)
    return u * u * (3.0 - 2.0 * u)


# ------------------------------------------------------------------ stenosis loss


def _young_tsai_dp_mmhg(lesion: dict, q_ml_s: float) -> float:
    """Young-Tsai focal+separation loss across one lesion (SI internally, mmHg out)."""
    d_ref = float(lesion['d_ref_mm'])
    d_min = float(lesion['d_min_mm'])
    length = float(lesion['length_mm'])
    if d_ref <= 0.0 or d_min <= 0.0 or length <= 0.0:
        _fail(f'lesion {lesion.get("lesion_id")!r}: non-positive d_ref/d_min/length')
    a0_as = float(lesion.get('A0_over_As') or (d_ref / d_min) ** 2)
    d0_m = d_ref * 1e-3
    ds_m = d_min * 1e-3
    length_m = length * 1e-3
    r0_m = 0.5 * d0_m
    throat_area_m2 = math.pi * (0.5 * ds_m) ** 2
    u = max(float(q_ml_s), 0.0) * 1e-6 / throat_area_m2  # mL/s -> m/s at the throat
    kv = 32.0 * (length_m / d0_m) * a0_as ** 2
    dp_pa = (kv * MU_PA_S / r0_m ** 2 * u * length_m
             + KT_STENOSIS * RHO_KG_M3 / 2.0 * (a0_as - 1.0) ** 2 * u ** 2)
    return dp_pa / PA_PER_MMHG


def _lesion_q(lesion: dict, clin: dict, state: str) -> float:
    branch = (clin.get(state) or {}).get('branches') or {}
    rec = branch.get(lesion['branch_id'])
    if rec is None:
        _fail(f'clinical.{state}.branches: no entry for lesion branch {lesion["branch_id"]!r}')
    q = rec.get('q_ml_s')
    if not isinstance(q, (int, float)):
        _fail(f'clinical.{state}.branches[{lesion["branch_id"]}].q_ml_s: expected a number')
    return float(q)


def _lesion_span(lesion: dict, edge_index: _NameIndex) -> dict:
    """Edge-local lesion span [s0, s1] on its branch (contract A s_mm marker).

    Contract A ``s_mm`` is the lesion center/throat marker on ``branch_id``, so the
    span is ``[s_mm - length/2, s_mm + length/2]`` clamped to the edge.
    """
    edge = edge_index.get(lesion['branch_id'])
    half = 0.5 * float(lesion['length_mm'])
    throat = float(lesion['s_mm'])
    s0 = max(throat - half, 0.0)
    s1 = min(throat + half, float(edge['length_mm']))
    if s1 <= s0:
        _fail(f'lesion {lesion.get("lesion_id")!r}: empty span on {edge["name"]!r}')
    return {'edge_name': edge['name'], 's0': s0, 's1': s1}


def _lesion_diameter(profile: np.ndarray, stations: np.ndarray,
                     s0_mm: float, s1_mm: float, lesion: dict) -> np.ndarray:
    """Contract A throat trapezoid overriding the graph inside the lesion span.

    ``d_ref_mm`` at both span ends, linear ramps over the outer quarters, the
    measured ``d_min_mm`` flat across the middle half of the span — the same
    trapezoid the StenosisAnalyzer overlays on its ``as_pct`` samples.  The
    override wins over the graph profile inside the span.
    """
    d_ref = float(lesion['d_ref_mm'])
    d_min = float(lesion['d_min_mm'])
    d = profile.copy()
    s0, s1 = float(s0_mm), float(s1_mm)
    inside = (stations >= s0) & (stations <= s1)
    if not inside.any():
        return d
    q1, q2 = s0 + 0.25 * (s1 - s0), s0 + 0.75 * (s1 - s0)
    d[inside] = np.interp(stations[inside], [s0, q1, q2, s1],
                          [d_ref, d_min, d_min, d_ref])
    return d


def _redistributed_pressure(x_mm: np.ndarray, length_mm: float, p_prox: float,
                            p_dist: float, spans: list[tuple]) -> np.ndarray:
    """One edge's solved pressure drop redistributed along ``x_mm``.

    The solved ``p_prox -> p_dist`` drop (its stenosis loss is already coupled
    into the C2 solve) is conserved exactly.  Each lesion span ``(x0, x1, w)``
    carries its Young-Tsai ``w`` mmHg share of that same solved total as a
    smoothstep CDF — scaled down when the shares overflow the solved drop, so a
    span never drops more than the edge does — while the residual drop runs at a
    constant gradient over the non-lesion arc.  Endpoints land exactly on the
    solved boundary pressures and P is monotone non-increasing throughout.
    """
    drop = p_prox - p_dist
    if drop <= 0.0 or not spans:
        return np.where(x_mm <= 0.0, p_prox,
                        p_prox + (p_dist - p_prox) * (x_mm / length_mm))
    weights = [max(w, 0.0) for _, _, w in spans]
    total_w = sum(weights)
    span_len = sum(max(x1 - x0, 0.0) for x0, x1, _ in spans)
    free_len = max(length_mm - span_len, 0.0)
    if total_w > 0.0:
        scale = min(1.0, drop / total_w) if free_len > 0.0 else drop / total_w
    else:
        scale = 0.0
    deltas = [w * scale for w in weights]
    slope = (drop - sum(deltas)) / free_len if free_len > 0.0 else 0.0
    covered = np.zeros_like(x_mm)
    shaped = np.zeros_like(x_mm)
    for (x0, x1, _), delta in zip(spans, deltas):
        width = max(x1 - x0, 0.0)
        covered += np.clip(x_mm - x0, 0.0, width)
        shaped += delta * _smoothstep((x_mm - x0) / max(width, 1e-9))
    p = p_prox - (slope * (x_mm - covered) + shaped)
    p[x_mm <= 0.0] = p_prox
    p[x_mm >= length_mm] = p_dist
    return p


# --------------------------------------------------------------------- TFC timing


def _calibrated_time(t_raw_s: float, vc: dict, branch: str) -> float:
    """Velocity-calibrated transit: per-branch transit_calibrated_s, else raw/kappa.

    ``kappa`` is the calibrated/raw trunk velocity ratio, so calibrated transit is
    ``raw / kappa``; when the C2 block carries explicit trunk transits the raw
    times are scaled by ``transit_calibrated_s / transit_raw_s`` (same ratio).
    """
    cal = vc.get('transit_calibrated_s')
    if isinstance(cal, dict):
        for key in (branch, branch.casefold(), _alnum(branch)):
            hit = cal.get(key)
            if isinstance(hit, (int, float)):
                return float(hit)
        for key, hit in cal.items():
            if (isinstance(key, str) and _alnum(key) == _alnum(branch)
                    and isinstance(hit, (int, float))):
                return float(hit)
    raw_ref = vc.get('transit_raw_s')
    if isinstance(cal, (int, float)) and isinstance(raw_ref, (int, float)) and raw_ref > 0:
        return t_raw_s * float(cal) / float(raw_ref)
    kappa = vc.get('kappa')
    if isinstance(kappa, (int, float)) and kappa > 0:
        return t_raw_s / float(kappa)
    return t_raw_s


def _distal_calibrated_s(t_raw_s: float, tip: str, sig_branches: dict, vc: dict) -> float:
    """Velocity-calibrated distal arrival (microcirculation-inclusive, path-cumulative).

    Preferred: ``signals.<A>.branches[tip].transit_calibrated_ms / 1000``
    (``= transit_ms / kappa``, the same arrival aggregation as the raw distal
    transit); fallback ``raw / kappa`` with an alnum-tolerant
    ``velocity_calibration.kappa`` lookup.  The epicardial convective
    ``velocity_calibration.transit_calibrated_s`` entries are a different
    quantity (per-edge L/(kappa*u) transit) and never feed this field.
    """
    rec = sig_branches.get(tip)
    if isinstance(rec, dict) and isinstance(rec.get('transit_calibrated_ms'), (int, float)):
        return float(rec['transit_calibrated_ms']) * 1e-3
    kappa = vc.get('kappa')
    if not isinstance(kappa, (int, float)):
        for key, val in vc.items():
            if isinstance(key, str) and _alnum(key) == 'kappa' and isinstance(val, (int, float)):
                kappa = val
                break
    if isinstance(kappa, (int, float)) and kappa > 0:
        return t_raw_s / float(kappa)
    return t_raw_s


def _frames(t_s: float) -> int:
    """TIMI frames at 30 fps (contract: round(t x 30), half away from zero;
    1e-9 epsilon keeps exact halves above the floor despite float fuzz)."""
    return int(math.floor(t_s * FPS + 0.5 + 1e-9))


def _round(x: float, nd: int) -> float:
    return round(float(x), nd)


def _station_transit_s(sig_branches: dict, branch: str) -> float:
    rec = sig_branches.get(branch)
    if rec is None:
        _fail(f'signals branches: no entry for {branch!r}')
    transit = rec.get('transit_ms')
    if not isinstance(transit, (int, float)):
        _fail(f'signals branches[{branch}].transit_ms: expected a number, got {transit!r}')
    return float(transit) * 1e-3


# ------------------------------------------------------------------------ driver


def _build_path(path_no: int, chain: list[dict], clin: dict, lesions: list[dict],
                edge_index: _NameIndex, sig_branches: dict, pa_mmHg: float,
                vc: dict, primary: bool = True) -> tuple[dict, dict, list, dict]:
    branch_ids = [e['name'] for e in chain]
    lengths = np.asarray([float(e['length_mm']) for e in chain], dtype=np.float64)
    offsets = np.concatenate([[0.0], np.cumsum(lengths)])
    total = float(offsets[-1])
    path_id = f'path_{path_no}'
    stations = _stations(total)
    # exact edge-junction stations: the pullback carries the solved node
    # pressures at every junction (P(junction) == solved boundary pressure)
    junctions = offsets[1:-1]
    if junctions.size:
        keep = np.asarray([bool(np.all(np.abs(junctions - s) > 1e-6)) for s in stations])
        stations = np.unique(np.concatenate([stations[keep], junctions]))

    # --- lesions landing on this path (edge-local span -> path arc length)
    on_path = []
    for lesion in lesions:
        span = _lesion_span(lesion, edge_index)
        try:
            j = branch_ids.index(span['edge_name'])
        except ValueError:
            continue
        on_path.append({
            'edge_i': j,
            'lesion': lesion,
            'a': offsets[j] + span['s0'],
            'b': min(offsets[j] + span['s1'], total),
        })

    # --- per-station geometry and station -> edge map
    d_mm = np.empty_like(stations)
    edge_of = np.empty(stations.size, dtype=int)
    for i, s in enumerate(stations):
        ie = _edge_index_at(offsets, float(s))
        edge_of[i] = ie
        d_mm[i] = _diameter_at(chain[ie], float(s) - float(offsets[ie]))

    # --- contract A throat trapezoid overriding the graph inside each span
    for item in on_path:
        d_mm = _lesion_diameter(d_mm, stations, item['a'], item['b'], item['lesion'])

    # --- redistributed pressure: each edge's solved drop already carries the
    #     coupled stenosis loss, so the lesion spans only redistribute it (never
    #     add to it): each span takes its Young-Tsai share as a smoothstep CDF,
    #     the residual runs a constant gradient outside the spans
    p_state = {}
    for state in ('rest', 'hyper'):
        for item in on_path:
            q = _lesion_q(item['lesion'], clin, state)
            item[f'dp_{state}'] = _young_tsai_dp_mmhg(item['lesion'], q)
        p = np.empty_like(stations)
        for j, edge in enumerate(chain):
            rec = ((clin.get(state) or {}).get('branches') or {}).get(edge['name'])
            if rec is None:
                _fail(f'clinical.{state}.branches: no entry for {edge["name"]!r}')
            spans = [(item['a'] - offsets[j], item['b'] - offsets[j], item[f'dp_{state}'])
                     for item in on_path if item['edge_i'] == j]
            sel = edge_of == j
            p[sel] = _redistributed_pressure(
                stations[sel] - offsets[j], float(edge['length_mm']),
                float(rec['p_prox_mmHg']), float(rec['p_dist_mmHg']), spans)
        # contract acceptance: monotone non-increasing across every lesion span
        for item in on_path:
            i0 = int(np.searchsorted(stations, item['a'], side='left'))
            i1 = int(np.searchsorted(stations, item['b'], side='right')) - 1
            if i1 > i0:
                p[i0:i1 + 1] = np.minimum.accumulate(p[i0:i1 + 1])
        p_state[state] = p

    # --- hard asserts: physical pressure range and solved-junction consistency
    for state in ('rest', 'hyper'):
        p = p_state[state]
        lo, hi = float(p.min()), float(p.max())
        if lo <= 0.0 or hi > pa_mmHg + 1e-9:
            _fail(f'{path_id}: {state} P(s) outside (0, {pa_mmHg}] mmHg '
                  f'(min {lo:.6f}, max {hi:.6f})')
        br = (clin.get(state) or {}).get('branches') or {}
        err = 0.0
        for j in range(1, len(chain)):
            k = int(np.searchsorted(stations, offsets[j], side='left'))
            err = max(err,
                      abs(p[k] - float(br[chain[j]['name']]['p_prox_mmHg'])),
                      abs(p[k] - float(br[chain[j - 1]['name']]['p_dist_mmHg'])))
        if err > 1e-6:
            _fail(f'{path_id}: {state} pullback P off the solved junction pressures '
                  f'by {err:.3e} mmHg')

    vffr = p_state['hyper'] / pa_mmHg  # P in (0, Pa] asserted -> vFFR in (0, 1]

    pullback = []
    for i, s in enumerate(stations):
        row = {
            's_mm': _round(s, 3),
            'd_mm': _round(d_mm[i], 3),
            'P_rest_mmHg': float(p_state['rest'][i]),
            'P_hyper_mmHg': float(p_state['hyper'][i]),
            'vFFR': _round(vffr[i], 3),
        }
        if not primary:
            # contract F/E: side-path rows carry delta_p_mmHg = Pa - P_hyper at
            # source precision; primary rows stay byte-stable (report contract)
            row['delta_p_mmHg'] = float(pa_mmHg - p_state['hyper'][i])
        pullback.append(row)
    k_min = int(np.argmin(vffr))
    record = {
        'path_id': path_id,
        'label': f'{"primary trunk" if primary else "side path"} {path_no} '
                 f'(root->tip, L={total:.1f} mm)',
        'branch_ids': branch_ids,
        'length_mm': _round(total, 3),
        'min_vffr': _round(vffr[k_min], 3),
        'min_vffr_s_mm': _round(stations[k_min], 3),
        'pullback': pullback,
    }
    if not primary:
        # contract F: side_paths[] carry the pullback item only — dp_ds and the
        # TFC timing blocks (landmarks / transit_anomaly / tfc) stay primary-only
        return record, {}, [], {}

    # --- dp_ds from the hyperemic pullback (mmHg/cm)
    ds_mm = np.diff(stations)
    grads = np.diff(p_state['hyper']) / (ds_mm / 10.0)
    s_mid = 0.5 * (stations[:-1] + stations[1:])
    mag = np.abs(grads)
    top_n = max(1, int(math.ceil(0.1 * mag.size)))
    top = np.argsort(-mag)[:top_n]
    top_frac = float(ds_mm[top].sum() / total) if total > 0 else 1.0
    median_mag = float(np.median(mag))
    k_max = int(np.argmax(mag))
    classification = ('focal' if (mag[k_max] > 3.0 * median_mag and top_frac < 0.30)
                      else 'diffuse')
    dp_ds = {
        path_id: {
            's_mm': [_round(v, 3) for v in s_mid],
            'mmHg_cm': [_round(v, 3) for v in grads],
            'classification': classification,
            'max_mmHg_cm': _round(mag[k_max], 3),
            'max_s_mm': _round(s_mid[k_max], 3),
        }
    }

    # --- TFC landmarks (proximal = first branch, distal = tip branch, profile A)
    landmarks = []
    distal_raw = None
    for tag, name, s_mm in (('prox', branch_ids[0], 0.0), ('dist', branch_ids[-1], total)):
        t_raw = _station_transit_s(sig_branches, name)
        t_cal = _calibrated_time(t_raw, vc, name)
        landmarks.append({
            'id': f'{path_id}_{tag}',
            'path_id': path_id,
            's_mm': _round(s_mm, 3),
            'name': name,
            't_arr_s': _round(t_raw, 3),
            't_arr_calibrated_s': _round(t_cal, 3),
            'tfc_frames': _frames(t_raw),
            'tfc_calibrated_frames': _frames(t_cal),
            'flag': None,
        })
        if tag == 'dist':
            distal_raw = t_raw

    t_distal_raw = distal_raw
    t_distal_cal = _distal_calibrated_s(t_distal_raw, branch_ids[-1], sig_branches, vc)
    if t_distal_cal > CALIBRATED_DISTAL_LIMIT_S:
        flag = TIMI_FLAG
        reason = (f'calibrated distal transit {t_distal_cal:.2f} s exceeds the '
                  f'{CALIBRATED_DISTAL_LIMIT_S:.1f} s threshold (raw {t_distal_raw:.2f} s -> '
                  f'calibrated {t_distal_cal:.2f} s): severe microvascular resistance or '
                  f'near-total flow arrest; pathology, not calibration error')
    elif t_distal_raw > CALIBRATED_DISTAL_LIMIT_S:
        flag = None
        reason = (f'raw distal transit {t_distal_raw:.2f} s exceeds the '
                  f'{CALIBRATED_DISTAL_LIMIT_S:.1f} s threshold, but velocity calibration '
                  f'addresses the anomaly (raw {t_distal_raw:.2f} s -> calibrated '
                  f'{t_distal_cal:.2f} s, within the threshold)')
    else:
        flag = None
        reason = (f'calibrated distal transit {t_distal_cal:.2f} s is within the '
                  f'{CALIBRATED_DISTAL_LIMIT_S:.1f} s threshold (raw {t_distal_raw:.2f} s -> '
                  f'calibrated {t_distal_cal:.2f} s)')
    landmarks[-1]['flag'] = flag
    anomaly = {
        path_id: {
            'distal_raw_s': float(t_distal_raw),
            'distal_calibrated_s': float(t_distal_cal),
            'tfc_frames': _frames(t_distal_raw),
            'tfc_calibrated_frames': _frames(t_distal_cal),
            'flag': flag,
            'reason': reason,
        }
    }
    return record, dp_ds, landmarks, anomaly


def _case_rxi(case: str, args) -> dict:
    graph_path = Path(args.graph_dir) / f'{case}_graph.json'
    hemo_path = Path(args.hemo_dir) / case / 'hemodynamics.json'
    lesions_path = _resolve_input(
        Path(args.lesions_dir), [f'case_{case}_lesions.json', f'{case}_lesions.json'],
        'contract A lesions',
    )
    graph = _load_json(graph_path)
    hemo = _load_json(hemo_path)
    lesions_doc = _load_json(lesions_path)

    clin = hemo.get('clinical')
    if not isinstance(clin, dict):
        _fail(f'{hemo_path}: no "clinical" block (run the C2 clinical extension first)')
    pa = clin.get('pa_mmHg')
    if not isinstance(pa, (int, float)) or pa <= 0:
        _fail(f'{hemo_path}: clinical.pa_mmHg must be a positive number')
    pa_mmHg = float(pa)
    vc = clin.get('velocity_calibration') or {}

    profile = args.profiles[0]
    sig = hemo.get('signals', {}).get(profile)
    if not isinstance(sig, dict) or not isinstance(sig.get('branches'), dict):
        _fail(f'{hemo_path}: no signals.{profile}.branches transit records')
    sig_branches = sig['branches']

    edges = graph.get('edges') or []
    if not edges:
        _fail(f'{graph_path}: no edges[]')
    edge_index = _NameIndex(edges, f'{graph_path} edges')
    lesions = lesions_doc.get('lesions') or []
    if not isinstance(lesions, list):
        _fail(f'{lesions_path}: "lesions" must be a list')
    for lesion in lesions:
        for key in ('lesion_id', 'branch_id', 's_mm', 'length_mm', 'd_ref_mm', 'd_min_mm'):
            if key not in lesion:
                _fail(f'{lesions_path}: lesion entry missing {key!r}')

    chains = _root_to_tip_chains(edges)
    ordered = _order_chains(chains)
    paths = ordered[:3]
    side_chains = ordered[3:]
    if not paths:
        _fail(f'{graph_path}: no root-to-tip chains')

    path_records, dp_ds, landmarks, transit_anomaly = [], {}, [], {}
    for path_no, chain in enumerate(paths, start=1):
        rec, grads, lms, anomaly = _build_path(
            path_no, chain, clin, lesions, edge_index, sig_branches, pa_mmHg, vc,
        )
        path_records.append(rec)
        dp_ds.update(grads)
        landmarks.extend(lms)
        transit_anomaly.update(anomaly)

    # contract F: side_paths[] cover every remaining root-to-tip chain
    # (path_4..N); only the pullback item is kept (dp_ds/TFC blocks primary-only)
    side_records = []
    for path_no, chain in enumerate(side_chains, start=len(paths) + 1):
        rec, _grads, _lms, _anomaly = _build_path(
            path_no, chain, clin, lesions, edge_index, sig_branches, pa_mmHg, vc,
            primary=False,
        )
        side_records.append(rec)

    # hard assert: paths[] + side_paths[] together name every graph edge exactly
    covered: set = set()
    for rec in path_records + side_records:
        covered.update(rec['branch_ids'])
    graph_names = {e['name'] for e in edges}
    missing = sorted(graph_names - covered)
    unknown = sorted(covered - graph_names)
    if missing or unknown:
        _fail(f'{graph_path}: paths[]+side_paths[] branch_ids do not equal the graph '
              f'edge names'
              + (f'; uncovered edges: {missing}' if missing else '')
              + (f'; non-graph branch_ids: {unknown}' if unknown else ''))

    generated = datetime.now(timezone.utc).isoformat()
    return {
        'schema': SCHEMA,
        'schema_version': SCHEMA_VERSION,
        'case': case,
        'tool': {
            **TOOL,
            'command': ' '.join(sys.argv),
            'generated': generated,
            'runtime_s': None,  # settled below once the case is built
        },
        'generated': generated,
        'paths': path_records,
        'side_paths': side_records,
        'dp_ds': dp_ds,
        'landmarks': landmarks,
        'transit_anomaly': transit_anomaly,
        'tfc': {'fps': FPS, 'c_lad_cutoff': C_LAD_CUTOFF},
    }


def main(argv=None) -> int:
    args = parse_args(argv)
    out_dir = Path(args.out_dir)
    for case in args.cases:
        t0 = time.perf_counter()
        doc = _case_rxi(case, args)
        doc['tool']['runtime_s'] = _round(time.perf_counter() - t0, 3)
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f'case_{case}_rxi.json'
        out_path.write_text(json.dumps(doc, indent=2) + '\n')
        covered = {b for p in doc['paths'] + doc['side_paths'] for b in p['branch_ids']}
        n_stations = sum(len(p['pullback']) for p in doc['paths'] + doc['side_paths'])
        print(f'{out_path}: {len(doc["paths"])} primary + '
              f'{len(doc["side_paths"])} side paths, '
              f'edge-coverage 100% ({len(covered)} graph edges), '
              f'{n_stations} pullback stations, {len(doc["landmarks"])} landmarks')
    return 0


if __name__ == '__main__':
    sys.exit(main())
