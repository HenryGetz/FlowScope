#!/usr/bin/env python3
"""Build a quantized cardiac anatomy GLB from segmentation inputs (Phase 1).

Three mutually exclusive ingestion modes (exactly one required):
  --stl-dir     ImageCAS-style per-structure STL meshes
  --ts-dir      TotalSegmentator output dir(s), repeatable (or
                space-separated): per-structure *.nii.gz masks and/or a single
                labels.nii.gz / *_multilabel.nii.gz volume; a case root whose
                chambers/ veins/ coronaries/ subdirs hold the 3-pass outputs
                is recursed one level (legacy flat dirs unchanged)
  --multilabel  one multilabel NIfTI (integer label per structure)

Mask/multilabel modes select cardiac anatomy by default: only structures whose
resolved id is in the palette (the 16 contract ids plus 'heart') are ingested;
everything else is recorded as a skipped row with reason 'non_cardiac'.
--extra-names whitelists additional raw names (normalized), --all-names ingests
everything. --stl-dir is an explicit file set and is never filtered.

Multi-source precedence: when one canonical id appears in several source dirs,
heartchambers_highres output dirs (and chambers/ subdirs) rank above veins/,
which rank above coronaries/, which rank above every other mask dir; each
loser becomes a skipped row. `heart` (whole-heart muscular envelope) is kept
even when `heart_myocardium` (LV wall) is present: the envelope is the visible
outer anchor, the wall the high-res inner shell.

Per-structure chain (contract order):
  1. ingest        STL: pv.read -> clean() + triangulate() (watertight via
                   is_manifold/n_open_edges); masks: pv.ImageData + multithreaded
                   Flying Edges (vtkFlyingEdges3D, iso 0.5), FE index-space
                   points mapped to mm RAS with the nibabel affine
  2. Taubin        drift-capped smooth_taubin (vtkWindowedSinc,
                   boundary_smoothing=False so open/truncated rims are not
                   slid): an iteration ladder (n, n/2, n/4, n/8, max(1, n/16))
                   falls back toward the unsmoothed input until the raw ->
                   smoothed volume drift fits DRIFT_CAP_PCT (rung 0 = keep the
                   input). Measured Taubin smoothing shrinks volume slightly
                   (median ~-0.27%, 98.7% of structures shrink across 355
                   scans), so the cap bounds rather than eliminates volume
                   change. Volume metrics (raw/smoothed/final/drift) recorded
                   per structure and emitted null + volume_note on open
                   (non-watertight) surfaces where the divergence-theorem
                   volume is meaningless
  3. decimation    to the per-structure budget allocation (stratified tier
                   shares of --budget: myocardium 0.30, chambers 0.26,
                   great_vessels 0.22, coronaries 0.22, renormalized over the
                   tiers present; `other`-group structures get per-structure
                   floors only) via --decimator:
                   decimate_pro (default) = PolyData.decimate_pro(reduction=...)
                   [vtkDecimatePro, the deliverable-named API]; quadric =
                   PolyData.decimate(target_reduction=...,
                   volume_preservation=True) [vtkQuadricDecimation]. Reduction
                   clamped <= 0.95, binary-searched (<= 6 iters) to within 2% of
                   target; per-structure floors survive trim/relax passes (the
                   the floor only drops when the input mesh itself is smaller);
                   then trim/relax passes onto the hard window; finally every
                   open boundary loop of the FOV-truncated inlet/outlet ends of
                   _CAP_STRUCTURE_IDS (aorta, vena_cava_superior) is capped
                   with a planar centroid fan (render geometry only: volume
                   metrics stay on the pre-cap mesh)
  4. finalize      smooth per-vertex point normals (consistent, splitting off,
                   flat feature angle -- no crease splitting, so PBR lighting
                   follows anatomical curvature) and apply the world transform
                   (RAS mm -> Y-up meters, bbox center of ALL structures at the
                   origin)
  5. emit          one binary .glb (glb_writer, KHR_mesh_quantization) + report
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import nibabel as nib
import numpy as np
import pyvista as pv
from vtkmodules.vtkCommonCore import vtkMultiThreader
from vtkmodules.vtkCommonCore import vtkSMPTools
from vtkmodules.vtkCommonDataModel import vtkDataObject
from vtkmodules.vtkFiltersCore import vtkFlyingEdges3D

import glb_writer
import structures

# per-structure abs % volume-drift cap (measured healthy band: p95 0.572%, p99 1.018%)
DRIFT_CAP_PCT = 1.0

WINDOW_LO = 100000
WINDOW_HI = 150000
FLOOR_TRIS = 1200
FLOOR_TRIS_VESSEL = 2500
VESSEL_IDS = frozenset(
    {'coronary_artery_left', 'coronary_artery_right', 'coronary_arteries', 'pulmonary_veins'}
)
CAP_FRACTION = 0.30
# stratified per-tier budget weights of --budget (renormalized over the tiers
# actually present; 'other' gets per-structure floors only)
GROUP_WEIGHTS = {
    'myocardium': 0.30,
    'chambers': 0.26,
    'great_vessels': 0.22,
    'coronaries': 0.22,
}
# the mandate's per-tier triangle ranges (fixed contract); the hard total
# window wins over these on any conflict
TIER_RANGE = {
    'myocardium': (35000, 45000),
    'great_vessels': (25000, 33000),
    'chambers': (30000, 40000),
    'coronaries': (25000, 35000),
}
# decimation-scatter guard for the window top-up: aim slightly above WINDOW_LO
# so per-structure decimation scatter (observed 0..-2 tris each) cannot slip
# the achieved aggregate below WINDOW_LO -- that would trip _aggregate_passes'
# relax backstop, which re-aims at --budget and wrecks the tier ranges. The
# mandated acceptance band is WINDOW_LO..WINDOW_LO+2000, so this stays inside.
_TOPUP_MARGIN = 500
# FOV-truncated vessel ends whose open rims get planar caps after decimation
_CAP_STRUCTURE_IDS = frozenset({'aorta', 'vena_cava_superior'})
MAX_SEARCH_ITERS = 6
MAX_AGGREGATE_PASSES = 8
REDUCTION_CAP = 0.95
# (x, y, z) RAS mm -> (x, z, -y) Y-up meters
_Y_UP_PERM = [0, 2, 1]
_Y_UP_SIGN = np.array([1.0, 1.0, -1.0])
_MM_TO_M = 1e-3


@dataclass
class _Work:
    """One processed structure between ingest and emission."""

    name: str
    source_file: str
    mode: str
    watertight: bool
    input_triangles: int
    post_fe_triangles: int
    volume_raw_mm3: float | None
    volume_smoothed_mm3: float | None
    volume_drift_pct: float | None
    volume_final_mm3: float | None
    smooth_iters_requested: int
    smooth_iters_used: int
    volume_drift_uncapped_pct: float | None
    smoothing_note: str | None
    smoothed: object
    mesh: object
    achieved: int
    target: int
    boundary_loops_capped: int = 0


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description='Build a quantized cardiac anatomy GLB (WebXR Phase 1).'
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--stl-dir', help='ImageCAS-style per-structure STL mesh directory')
    source.add_argument(
        '--ts-dir',
        action='append',
        nargs='+',
        metavar='DIR',
        help='TotalSegmentator output dir(s), repeatable or space-separated: '
        'per-structure *.nii.gz masks and/or a single labels.nii.gz / '
        '*_multilabel.nii.gz volume; a case root containing chambers/ veins/ '
        'coronaries/ subdirs is recursed one level',
    )
    source.add_argument('--multilabel', help='single multilabel NIfTI (integer label per structure)')
    parser.add_argument(
        '--name-map', help='JSON {filename: canonical_or_raw_name} override (with --stl-dir)'
    )
    parser.add_argument(
        '--label-map',
        help='JSON {label: canonical_or_raw_name}; default = heartchambers_highres ids 1..7',
    )
    parser.add_argument('--out', default='out/cardiac.glb')
    parser.add_argument('--report', default='out/report.json')
    parser.add_argument(
        '--budget',
        type=int,
        default=135000,
        help='total triangle budget, stratified over tiers (myocardium 0.30, '
        'chambers 0.26, great_vessels 0.22, coronaries 0.22, renormalized '
        'over present tiers; other = floors only)',
    )
    parser.add_argument('--smooth-iters', type=int, default=25)
    parser.add_argument('--pass-band', type=float, default=0.1)
    parser.add_argument('--threads', type=int, default=os.cpu_count() or 1)
    parser.add_argument('--no-quantize', action='store_true')
    parser.add_argument(
        '--extra-names',
        default='',
        help='comma-separated additional raw names (normalized) to whitelist in '
        'mask/multilabel modes',
    )
    parser.add_argument(
        '--all-names',
        action='store_true',
        help='ingest every structure in mask/multilabel modes (no cardiac-only filter)',
    )
    parser.add_argument(
        '--decimator',
        choices=('decimate_pro', 'quadric'),
        default='decimate_pro',
        help='decimation filter: decimate_pro (default, PolyData.decimate_pro) or '
        'quadric (PolyData.decimate with volume preservation)',
    )
    args = parser.parse_args(argv)
    if args.ts_dir:
        args.ts_dir = [d for group in args.ts_dir for d in group]
    if args.name_map and not args.stl_dir:
        parser.error('--name-map requires --stl-dir')
    if args.label_map and not (args.multilabel or args.ts_dir):
        parser.error('--label-map requires --multilabel or --ts-dir')
    if args.threads < 1:
        parser.error('--threads must be >= 1')
    return args


def _load_label_map(path) -> dict[int, str]:
    raw = json.loads(Path(path).read_text())
    out: dict[int, str] = {}
    for key, value in raw.items():
        try:
            label = int(key)
        except (TypeError, ValueError):
            raise SystemExit(f'--label-map: invalid label key {key!r} (expected integer)')
        name = str(value).strip()
        out[label] = name or f'label_{label}'
    return out


def _load_name_map(path) -> dict[str, str]:
    raw = json.loads(Path(path).read_text())
    return {str(key): str(value).strip() for key, value in raw.items()}


def _load_volume(path, volumes):
    key = str(path)
    if key not in volumes:
        img = nib.load(key)
        volumes[key] = (np.asanyarray(img.dataobj), np.asarray(img.affine, dtype=np.float64))
    return volumes[key]


def _is_multilabel_filename(name: str) -> bool:
    stem = structures.strip_suffix(name).lower()
    return stem == 'labels' or stem == 'multilabel' or stem.endswith('_multilabel')


def _multilabel_specs(path, data, label_map) -> list[dict]:
    specs = []
    for value in np.unique(data):
        label = int(value)
        if label == 0:
            continue
        mapped = label_map.get(label)
        raw = mapped if mapped else f'label_{label}'
        specs.append(
            {
                'name': structures.resolve_name(raw),
                'raw': structures.strip_suffix(raw),
                'source_file': str(path),
                'mode': 'multilabel',
                'path': path,
                'label': label,
            }
        )
    return specs


def _ts_files(root: Path) -> list[Path]:
    """Mask/multilabel NIfTIs under ``root``: the dir itself plus one sub-level.

    Accepts legacy flat TotalSegmentator output dirs and 3-pass case roots
    (``chambers/``, ``veins/``, ``coronaries/`` subdirs) unchanged.
    """
    files = [p for p in root.glob('*.nii.gz') if p.is_file()]
    for sub in sorted(p for p in root.glob('*') if p.is_dir()):
        files.extend(p for p in sub.glob('*.nii.gz') if p.is_file())
    return sorted(files)


def _source_rank(root: Path, path: Path) -> int:
    """Source precedence tier of one mask/multilabel file (lower wins).

    heartchambers_highres output dirs and ``chambers/`` subdirs rank above
    ``veins/``, which rank above ``coronaries/``, which rank above every other
    mask dir. Symlinked roots are judged by their resolved dir name too (the
    data/segmentations/<case> -> <case>_heartchambers_highres convenience
    links).
    """
    names = [root.name, *path.relative_to(root).parts[:-1]]
    try:
        resolved_name = root.resolve().name
    except OSError:
        resolved_name = root.name
    names.append(resolved_name)
    lowered = [name.lower() for name in names]
    if any('heartchambers_highres' in name for name in lowered) or 'chambers' in lowered:
        return 0
    if 'veins' in lowered:
        return 1
    if 'coronaries' in lowered:
        return 2
    return 3


def _collect_specs(args, label_map, volumes) -> list[dict]:
    """Collect structure specs across all sources with precedence dedupe.

    Sources: --stl-dir meshes, one or more --ts-dir trees (each scanned flat
    plus one sub-level for case roots), or a single --multilabel volume. When
    one canonical id appears more than once the highest-precedence source wins
    (heartchambers_highres/chambers > veins > coronaries > other; ties break
    first-seen) and each loser becomes a skipped row. `heart` is kept alongside
    `heart_myocardium` (envelope vs LV wall; both share the myocardium group).
    """
    specs: list[dict] = []
    if args.stl_dir:
        name_map = _load_name_map(args.name_map) if args.name_map else {}
        for path in sorted(Path(args.stl_dir).glob('*.stl')):
            override = name_map.get(path.name, name_map.get(path.stem))
            raw = override if override else path.name
            specs.append(
                {
                    'name': structures.resolve_name(raw),
                    'raw': structures.strip_suffix(raw),
                    'source_file': str(path),
                    'mode': 'stl',
                    'path': path,
                    'label': None,
                }
            )
    elif args.ts_dir:
        for root_str in args.ts_dir:
            root = Path(root_str)
            files = _ts_files(root)
            for path in files:
                if _is_multilabel_filename(path.name):
                    continue
                specs.append(
                    {
                        'name': structures.resolve_name(path.name),
                        'raw': structures.strip_suffix(path.name),
                        'source_file': str(path),
                        'mode': 'mask',
                        'path': path,
                        'label': None,
                        'rank': _source_rank(root, path),
                    }
                )
            for path in files:
                if not _is_multilabel_filename(path.name):
                    continue
                data, _affine = _load_volume(path, volumes)
                for spec in _multilabel_specs(path, data, label_map):
                    spec['rank'] = _source_rank(root, path)
                    specs.append(spec)
    else:
        path = Path(args.multilabel)
        data, _affine = _load_volume(path, volumes)
        specs.extend(_multilabel_specs(path, data, label_map))

    # cardiac-only selection for mask/multilabel modes (--stl-dir is an explicit
    # file set and is never filtered)
    if not args.all_names:
        extras = {structures.normalize(s) for s in args.extra_names.split(',') if s.strip()}
        for spec in specs:
            if spec['mode'] == 'stl':
                continue
            if spec['name'] in structures.CARDIAC_IDS:
                continue
            if structures.normalize(spec['raw']) in extras:
                continue
            spec['skipped'] = 'non_cardiac'

    # duplicate canonical ids: the highest-precedence source wins (first seen
    # on a tie); every loser becomes a skipped row
    kept: dict[str, tuple[int, int]] = {}
    for order, spec in enumerate(specs):
        if 'skipped' in spec:
            continue
        name = spec['name']
        rank = spec.get('rank', 3)
        prev = kept.get(name)
        if prev is None:
            kept[name] = (rank, order)
            continue
        if (rank, order) < prev:
            winner, loser = spec, specs[prev[1]]
            kept[name] = (rank, order)
        else:
            winner, loser = specs[prev[1]], spec
        loser['skipped'] = (
            f'duplicate structure name (superseded by {winner["source_file"]})'
        )

    return specs


def _flying_edges(binary, affine):
    """Multithreaded Flying Edges on a binary mask -> mm RAS surface mesh."""
    image = pv.ImageData(
        dimensions=tuple(int(s) for s in binary.shape),
        spacing=(1.0, 1.0, 1.0),
        origin=(0.0, 0.0, 0.0),
    )
    image.point_data['mask'] = binary.astype(np.float32).ravel(order='F')
    image.point_data.active_scalars_name = 'mask'
    alg = vtkFlyingEdges3D()
    alg.SetInputData(image)
    alg.SetValue(0, 0.5)
    alg.ComputeNormalsOff()
    alg.ComputeGradientsOff()
    alg.ComputeScalarsOff()
    alg.SetInputArrayToProcess(0, 0, 0, vtkDataObject.FIELD_ASSOCIATION_POINTS, 'mask')
    alg.Update()
    out = pv.wrap(alg.GetOutput())
    if out.n_faces == 0:
        return None
    points = np.asarray(out.points, dtype=np.float64)
    out.points = points @ affine[:3, :3].T + affine[:3, 3]  # p_mm = A @ [i, j, k, 1]
    return out


def _ingest(spec, volumes):
    """Return (mesh, input_triangles, post_fe_triangles, skip_reason)."""
    if spec['mode'] == 'stl':
        raw_mesh = pv.read(str(spec['path']))
        input_triangles = int(raw_mesh.n_faces)
        mesh = raw_mesh.clean().triangulate()
        post_fe_triangles = int(mesh.n_faces)
        if post_fe_triangles == 0:
            return None, input_triangles, post_fe_triangles, 'empty mesh (no triangles)'
        return mesh, input_triangles, post_fe_triangles, None

    data, affine = _load_volume(spec['path'], volumes)
    if data.ndim != 3:
        return None, 0, 0, f'unsupported volume shape {tuple(data.shape)}'
    binary = (data == spec['label']) if spec['label'] is not None else (data > 0.5)
    nonzero = int(np.count_nonzero(binary))
    if nonzero <= 1:
        return None, 0, 0, f'empty/1-voxel mask ({nonzero} nonzero voxels)'
    mesh = _flying_edges(binary, affine)
    if mesh is None:
        return None, 0, 0, 'Flying Edges produced no surface'
    count = int(mesh.n_faces)
    return mesh, count, count, None


def _smooth(mesh, n_iter, pass_band, watertight):
    """Taubin smooth with boundary_smoothing off (open rims are not slid).

    Drift-capped per structure: an iteration ladder (n_iter, //2, //4, //8,
    max(1, //16)) is tried and the first rung whose raw -> smoothed signed
    volume drift stays within DRIFT_CAP_PCT is applied; if no rung qualifies
    the input mesh is kept unsmoothed (rung 0). Measured Taubin smoothing
    shrinks volume slightly (median ~-0.27%, 98.7% of structures shrink
    across 355 scans), so the cap bounds rather than eliminates volume
    change.

    Volume metrics are only computed for watertight meshes; the
    divergence-theorem volume of an open (FOV-truncated) surface is
    origin-dependent garbage.

    Returns (smoothed, vol_raw, vol_smoothed, drift_pct, record): drift_pct
    is abs percent raw -> applied smoothing (0.0 when the input is kept);
    record = {'iters_requested', 'iters_used', 'volume_drift_uncapped_pct',
    'note'}.
    """
    record = {
        'iters_requested': n_iter,
        'iters_used': n_iter,
        'volume_drift_uncapped_pct': None,
        'note': None,
    }
    if not watertight:
        smoothed = mesh.smooth_taubin(
            n_iter=n_iter, pass_band=pass_band, boundary_smoothing=False
        )
        record['note'] = (
            'drift unmeasured: open surface (FOV-truncated); smoothing uncapped'
        )
        return smoothed, None, None, None, record

    before = float(mesh.volume)
    if not np.isfinite(before) or before <= 0.0:
        record['iters_used'] = 0
        record['note'] = f'smoothing skipped: raw volume {before:.3f} mm3, drift unmeasurable'
        return mesh, before, before, 0.0, record

    ladder = (n_iter, n_iter // 2, n_iter // 4, n_iter // 8, max(1, n_iter // 16))
    rungs = sorted({rung for rung in ladder if rung >= 1}, reverse=True)
    applied, applied_signed, applied_after = mesh, 0.0, before  # rung 0: keep input
    uncapped_signed = 0.0  # drift at rung n_iter (unsmoothed rung 0 = 0%)
    iters_used = 0
    for rung in rungs:
        candidate = mesh.smooth_taubin(
            n_iter=rung, pass_band=pass_band, boundary_smoothing=False
        )
        after = float(candidate.volume)
        signed = (after - before) / before * 100.0
        if rung == n_iter:
            uncapped_signed = signed
        if abs(signed) <= DRIFT_CAP_PCT:
            applied, applied_signed, applied_after, iters_used = candidate, signed, after, rung
            break
    record['iters_used'] = iters_used
    if iters_used != n_iter:
        record['volume_drift_uncapped_pct'] = abs(uncapped_signed)
        if iters_used:
            record['note'] = (
                f'drift cap {DRIFT_CAP_PCT:g}%: {n_iter} iters drift '
                f'{uncapped_signed:+.2f}% -> used {iters_used} iters ({applied_signed:+.2f}%)'
            )
        else:
            record['note'] = (
                f'drift cap {DRIFT_CAP_PCT:g}%: kept unsmoothed '
                f'({n_iter} iters drift {uncapped_signed:+.2f}%)'
            )
    return applied, before, applied_after, abs(applied_signed), record


def _allocate_targets(names, raws, budget) -> list[int]:
    """Stratified per-tier budget allocation (tier weights of ``budget``).

    Tier weights -- myocardium 0.30, chambers 0.26, great_vessels 0.22,
    coronaries 0.22 (``structures.structure_group``) -- are renormalized over
    the tiers actually present: absent tiers hand their weight to the present
    ones. Each tier's share of ``budget`` is clamped into its mandated
    ``TIER_RANGE`` and bounded above by its summed raw triangles: a tier whose
    raws fall below its range minimum stays at its raw total (supply-limited,
    no synthetic upsampling). The tier targets are then topped up to the hard
    window floor (``_top_up_to_window``; WINDOW_LO wins over the ranges on any
    conflict). Within a tier the target splits proportional to raw triangle
    counts with the existing floors (``_floor_base``) and the 0.30
    single-structure cap applied against the TIER target, not the global
    budget. Capped excess is redistributed to the other tier members; when the
    tier cannot absorb it the cap releases (it never starves the tier below its
    share). ``other``-group structures get per-structure floors only.
    """
    targets = [0] * len(names)
    groups: dict[str, list[int]] = {}
    for index, name in enumerate(names):
        groups.setdefault(structures.structure_group(name), []).append(index)
    for index in groups.get('other', ()):
        targets[index] = max(1, min(_floor_base(names[index]), raws[index]))
    present = [group for group in GROUP_WEIGHTS if group in groups]
    weight_total = sum(GROUP_WEIGHTS[group] for group in present)
    tier_targets: dict[str, float] = {}
    tier_raws: dict[str, int] = {}
    for group in present:
        share = budget * GROUP_WEIGHTS[group] / weight_total if weight_total else 0.0
        raw_sum = sum(raws[i] for i in groups[group])
        low, high = TIER_RANGE[group]
        tier_raws[group] = raw_sum
        tier_targets[group] = min(max(round(share), low), high, raw_sum)
    _top_up_to_window(tier_targets, tier_raws, sum(targets))
    for group in present:
        _split_group_targets(names, raws, groups[group], tier_targets[group], targets)
    return targets


def _top_up_to_window(tier_targets, tier_raws, fixed) -> None:
    """Raise tier targets until the allocation total reaches the window floor.

    Phase 1 feeds the tiers with remaining raw headroom (``tier_raws - target
    > 0``) in proportion to their weights, never past a tier's ``TIER_RANGE``
    maximum. Phase 2 (the hard window wins over the ranges): once every tier
    sits at min(range max, raw sum) the range maxima relax and the remainder
    spreads proportionally to the remaining raw headroom -- the only step where
    a target may exceed its tier's raw sum. ``fixed`` is the untouchable
    floors-only mass (``other``-group structures).
    """
    deficit = WINDOW_LO + _TOPUP_MARGIN - (sum(tier_targets.values()) + fixed)
    if deficit <= 0:
        return
    for _ in range(2 * len(tier_targets) + 4):
        feeders = [
            group
            for group, target in tier_targets.items()
            if tier_raws[group] > target and target < TIER_RANGE[group][1]
        ]
        if not feeders:
            break
        weight_total = sum(GROUP_WEIGHTS[group] for group in feeders)
        offered = deficit
        for group in feeders:
            room = min(
                TIER_RANGE[group][1] - tier_targets[group],
                tier_raws[group] - tier_targets[group],
            )
            add = min(offered * GROUP_WEIGHTS[group] / weight_total, room)
            if add > 0:
                tier_targets[group] += add
                deficit -= add
        if deficit <= 0:
            return
    if not tier_targets or not all(
        tier_targets[group] >= min(TIER_RANGE[group][1], tier_raws[group]) - 1e-6
        for group in tier_targets
    ):
        return
    headroom = {
        group: tier_raws[group] - tier_targets[group]
        for group in tier_targets
        if tier_raws[group] > tier_targets[group]
    }
    spread = sum(headroom.values())
    if spread > 0:
        for group, head in headroom.items():
            tier_targets[group] += deficit * head / spread
    else:
        # no raw headroom anywhere; the hard window still wins
        weight_total = sum(GROUP_WEIGHTS[group] for group in tier_targets)
        for group in tier_targets:
            tier_targets[group] += deficit * GROUP_WEIGHTS[group] / weight_total


def _split_group_targets(names, raws, indexes, group_target, targets) -> None:
    """Split one tier's target over its members (floors + 0.30 water-filled cap)."""
    total_raw = sum(raws[i] for i in indexes)
    if group_target <= 0 or total_raw <= 0:
        for i in indexes:
            targets[i] = max(1, min(_floor_base(names[i]), raws[i]))
        return
    cap = CAP_FRACTION * group_target
    lowers = {i: min(_floor_base(names[i]), raws[i]) for i in indexes}
    uppers = {i: max(min(raws[i], cap), lowers[i]) for i in indexes}
    alloc = {i: group_target * raws[i] / total_raw for i in indexes}
    cap_released = False
    for _ in range(len(indexes) + 4):
        for i in indexes:
            alloc[i] = min(max(alloc[i], lowers[i]), uppers[i])
        delta = group_target - sum(alloc.values())
        if abs(delta) < 1e-6:
            break
        if delta > 0:
            room = {i: uppers[i] - alloc[i] for i in indexes if uppers[i] > alloc[i]}
            if not room:
                if cap_released:
                    break
                # too little absorbable headroom for the 0.30 cap to bind
                # without leaving the tier under target -- release it
                cap_released = True
                for i in indexes:
                    uppers[i] = raws[i]
                continue
            total_room = sum(room.values())
            for i, head in room.items():
                alloc[i] += delta * head / total_room
        else:
            head = {i: alloc[i] - lowers[i] for i in indexes if alloc[i] > lowers[i]}
            if not head:
                break
            total_head = sum(head.values())
            for i, slack in head.items():
                alloc[i] += delta * slack / total_head
    for i in indexes:
        targets[i] = max(1, int(round(alloc[i])))


def _floor_base(name):
    return FLOOR_TRIS_VESSEL if name in VESSEL_IDS else FLOOR_TRIS


def _run_decimate(mesh, reduction, decimator):
    if decimator == 'decimate_pro':
        return mesh.decimate_pro(reduction=reduction)
    return mesh.decimate(target_reduction=reduction, volume_preservation=True)


def decimate_toward(mesh, target, floor, decimator):
    """Decimate toward ``target`` triangles with the selected filter.

    ``decimate_pro`` -> PolyData.decimate_pro(reduction=...) [vtkDecimatePro,
    the deliverable-named API]; ``quadric`` -> PolyData.decimate(
    target_reduction=..., volume_preservation=True) [vtkQuadricDecimation,
    volume-preserving quadric]. The reduction is clamped to <= 0.95 and
    binary-searched (<= MAX_SEARCH_ITERS evaluations) to land within 2% of
    target. Candidates below ``floor`` are rejected -- the floor only drops when
    the input mesh itself is smaller than the floor. Structures already under
    target are kept as-is (reduction 0). Returns (mesh, achieved_triangles).
    """
    n = mesh.n_faces
    if n <= target:
        return mesh, n
    lower = min(floor, n)
    lo, hi = 0.0, REDUCTION_CAP
    reduction = min(1.0 - target / n, REDUCTION_CAP)
    best, best_n = None, None
    for _ in range(MAX_SEARCH_ITERS):
        out = _run_decimate(mesh, reduction, decimator)
        count = out.n_faces
        if count >= lower and (best is None or abs(count - target) < abs(best_n - target)):
            best, best_n = out, count
        if count >= lower and abs(count - target) <= 0.02 * target:
            break
        if count > target:
            lo = reduction
        else:
            hi = reduction
        new_reduction = 0.5 * (lo + hi)
        if abs(new_reduction - reduction) < 1e-6:
            break
        reduction = new_reduction
    if best is None:
        return mesh, n  # never breach the floor: keep the input mesh
    return best, best_n


def _aggregate_passes(work, budget, decimator) -> None:
    """Trim/relax passes until the aggregate lands in the hard window.

    trim  (total > 150k): decimate the current meshes further (chains
          reductions), scaling only the triangle mass above the per-structure
          floors so the floors survive
    relax (total < 100k): re-decimate from the smoothed meshes toward larger
          targets (bounded by the raw triangle counts)
    """
    aim = min(max(float(budget), WINDOW_LO / 0.98), WINDOW_HI / 1.02)
    total = sum(w.achieved for w in work)
    for _ in range(MAX_AGGREGATE_PASSES):
        if WINDOW_LO <= total <= WINDOW_HI:
            return
        if total > WINDOW_HI:
            lowers = [min(_floor_base(w.name), w.achieved) for w in work]
            fixed = sum(lowers)
            movable = total - fixed
            scale = max(aim - fixed, 0.0) / movable if movable > 0 else 0.0
            for w, lower in zip(work, lowers):
                target = min(w.achieved, lower + int(round((w.achieved - lower) * scale)))
                if target < w.achieved:
                    w.mesh, w.achieved = decimate_toward(
                        w.mesh, target, _floor_base(w.name), decimator
                    )
                    w.target = target
        else:
            deficit = aim - total
            headroom = [max(0, w.input_triangles - w.achieved) for w in work]
            slack = sum(headroom)
            if slack <= 0:
                return
            for w, head in zip(work, headroom):
                if head <= 0:
                    continue
                target = min(w.input_triangles, w.achieved + int(round(deficit * head / slack)))
                if target > w.achieved:
                    w.mesh, w.achieved = decimate_toward(
                        w.smoothed, target, _floor_base(w.name), decimator
                    )
                    w.target = target
        total = sum(w.achieved for w in work)


def _boundary_loops(mesh) -> list[list[int]]:
    """Open-boundary loops as vertex-index cycles, directed by the adjacent    face winding (consecutive half-edges u -> v chain via nxt[u] == v, so a
    cap patch closing the loop must traverse v -> u)."""
    faces = np.asarray(mesh.faces, dtype=np.int64).reshape(-1, 4)[:, 1:]
    n_verts = int(len(mesh.points))
    half_edges = np.concatenate(
        [faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]], axis=0
    )
    codes = half_edges[:, 0] * n_verts + half_edges[:, 1]
    reverses = half_edges[:, 1] * n_verts + half_edges[:, 0]
    boundary = half_edges[~np.isin(reverses, codes)]
    if boundary.size == 0:
        return []
    nxt: dict[int, int] = {}
    for u, v in boundary:
        nxt.setdefault(int(u), int(v))
    loops: list[list[int]] = []
    visited: set[int] = set()
    for start in list(nxt):
        if start in visited:
            continue
        loop = [start]
        visited.add(start)
        cur = nxt[start]
        while cur != start and cur not in visited and cur in nxt:
            loop.append(cur)
            visited.add(cur)
            cur = nxt[cur]
        if cur == start and len(loop) >= 3:
            loops.append(loop)
    return loops


def _cap_open_rims(work) -> None:
    """Planar caps for the open rims of FOV-truncated vessel ends (render only).

    After decimation, every open boundary loop of the ``_CAP_STRUCTURE_IDS``
    structures (aorta, vena_cava_superior) is capped with a planar centroid
    fan -- one fan vertex at the loop centroid (the loop best-fit plane passes
    through it), triangles reversed against the adjacent face winding -- so no
    raw open polygon rims reach the GLB. Metric semantics are unchanged:
    volume drift was computed on the pre-cap geometry and open rows keep null
    metrics. Refreshes w.mesh / w.achieved and sets w.boundary_loops_capped.
    """
    for w in work:
        w.boundary_loops_capped = 0
        if structures.resolve_name(w.name) not in _CAP_STRUCTURE_IDS:
            continue
        loops = [loop for loop in _boundary_loops(w.mesh) if len(loop) >= 3]
        if not loops:
            continue
        points = np.asarray(w.mesh.points, dtype=np.float64)
        faces = np.asarray(w.mesh.faces, dtype=np.int64).reshape(-1, 4)[:, 1:]
        centroids = np.asarray(
            [points[loop].mean(axis=0) for loop in loops], dtype=np.float64
        )
        caps = []
        for loop_index, loop in enumerate(loops):
            center = len(points) + loop_index
            for i, u in enumerate(loop):
                v = loop[(i + 1) % len(loop)]
                caps.append((v, u, center))  # reversed half-edge closes the surface
        capped_faces = np.vstack([faces, np.asarray(caps, dtype=np.int64)])
        padded = np.hstack(
            [np.full((len(capped_faces), 1), 3, dtype=np.int64), capped_faces]
        ).ravel()
        w.mesh = pv.PolyData(np.vstack([points, centroids]), padded)
        w.achieved = int(w.mesh.n_faces)
        w.boundary_loops_capped = len(loops)


class _GeometryItems(list):
    """Finalized items; ``transform`` records the world transform applied to points.

    A ``list`` subclass so consumers that just iterate the meshes
    (glb_writer.write_glb, the raw-scale validator) are unaffected.
    """

    transform: dict


def _transform_record(center) -> dict:
    """Invertible record of the RAS-mm -> Y-up-m world transform of _finalize_geometry."""
    return {
        'space': 'ras_mm->y_up_m',
        'perm': list(_Y_UP_PERM),
        'sign': [float(v) for v in _Y_UP_SIGN],
        'scale_m_per_mm': float(_MM_TO_M),
        'center_m': [float(v) for v in np.asarray(center, dtype=np.float64).reshape(3)],
    }


def _finalize_geometry(work) -> _GeometryItems:
    """Step 4: smooth point normals, world transform, recenter on the global bbox.

    Normals are smooth per-vertex point normals (compute_normals with
    point_normals=True / cell_normals=False, consistent, splitting off and a
    flat feature angle -- no crease splitting) so PBR lighting follows
    anatomical curvature. Accepts anything with .name and .mesh (the raw-scale
    validator passes plain namespaces). Returns the items carrying the applied
    world transform in ``.transform`` (see ``_transform_record``).
    """
    items = []
    for w in work:
        mesh = w.mesh.compute_normals(
            cell_normals=False,
            point_normals=True,
            split_vertices=False,
            feature_angle=180.0,
            consistent_normals=True,
        )
        normals = np.asarray(mesh.point_data['Normals'], dtype=np.float64)
        points = np.asarray(mesh.points, dtype=np.float64)
        indices = np.asarray(mesh.faces, dtype=np.int64).reshape(-1, 4)[:, 1:].reshape(-1)
        items.append(
            {
                'id': w.name,
                'points': points[:, _Y_UP_PERM] * _Y_UP_SIGN * _MM_TO_M,
                'normals': normals[:, _Y_UP_PERM] * _Y_UP_SIGN,
                'indices': indices,
            }
        )
    center = 0.5 * (
        np.min([it['points'].min(axis=0) for it in items], axis=0)
        + np.max([it['points'].max(axis=0) for it in items], axis=0)
    )
    for it in items:
        it['points'] = it['points'] - center
    out = _GeometryItems(items)
    out.transform = _transform_record(center)
    return out


def _row_for(result, decimator) -> dict:
    if not isinstance(result, _Work):
        return {
            **result,
            'boundary_loops_capped': 0,
            'normals': 'smooth_point',
            'decimator': decimator,
        }
    w = result
    reduction = (1.0 - w.achieved / w.input_triangles) * 100.0 if w.input_triangles else 0.0
    if w.watertight:
        volumes = {
            'volume_raw_mm3': round(w.volume_raw_mm3, 3),
            'volume_smoothed_mm3': round(w.volume_smoothed_mm3, 3),
            'volume_final_mm3': round(w.volume_final_mm3, 3),
            'volume_drift_pct': round(w.volume_drift_pct, 4),
        }
    else:
        volumes = {
            'volume_raw_mm3': None,
            'volume_smoothed_mm3': None,
            'volume_final_mm3': None,
            'volume_drift_pct': None,
            'volume_note': (
                'open surface (FOV-truncated); divergence-theorem volume not meaningful'
            ),
        }
    return {
        'name': w.name,
        'source_file': w.source_file,
        'mode': w.mode,
        'watertight': w.watertight,
        'input_triangles': w.input_triangles,
        'post_fe_triangles': w.post_fe_triangles,
        **volumes,
        'smooth_iters_requested': w.smooth_iters_requested,
        'smooth_iters_used': w.smooth_iters_used,
        'volume_drift_uncapped_pct': (
            round(w.volume_drift_uncapped_pct, 4)
            if w.volume_drift_uncapped_pct is not None
            else None
        ),
        'smoothing_note': w.smoothing_note,
        'boundary_loops_capped': w.boundary_loops_capped,
        'normals': 'smooth_point',
        'final_triangles': w.achieved,
        'target_triangles': w.target,
        'reduction_pct': round(reduction, 3),
        'decimator': decimator,
    }


def _print_table(rows, totals) -> None:
    print(f'{"name":<32}{"mode":<12}{"wt":<4}{"input":>9}{"postFE":>9}'
          f'{"target":>9}{"final":>9}{"red%":>8}{"drift%":>8}')
    for row in rows:
        if 'skipped' in row:
            print(f'{row["name"]:<32}{row["mode"]:<12}skipped: {row["skipped"]}')
            continue
        drift = row['volume_drift_pct']
        drift_str = f'{drift:.3f}' if drift is not None else '-'
        print(
            f'{row["name"]:<32}{row["mode"]:<12}'
            f'{"yes" if row["watertight"] else "no":<4}'
            f'{row["input_triangles"]:>9}{row["post_fe_triangles"]:>9}'
            f'{row["target_triangles"]:>9}{row["final_triangles"]:>9}'
            f'{row["reduction_pct"]:>8.2f}{drift_str:>8}'
        )
    print(
        f'totals: in={totals["aggregate_input_triangles"]} '
        f'out={totals["aggregate_final_triangles"]} '
        f'red={totals["aggregate_reduction_pct"]:.2f}% '
        f'budget={totals["budget"]} window={totals["window"]} '
        f'within_window={"yes" if totals["within_window"] else "no"} '
        f'glb_bytes={totals["glb_bytes"]} '
        f'quantized={"yes" if totals["quantized"] else "no"}'
    )


def main(argv=None) -> int:
    args = parse_args(argv)
    vtkMultiThreader.SetGlobalDefaultNumberOfThreads(args.threads)
    vtkSMPTools.Initialize(args.threads)

    label_map = _load_label_map(args.label_map) if args.label_map else dict(
        structures.DEFAULT_LABEL_MAP
    )
    volumes: dict = {}
    specs = _collect_specs(args, label_map, volumes)

    results = []
    work = []
    for spec in specs:
        if 'skipped' in spec:
            results.append(
                {
                    'name': spec['name'],
                    'source_file': spec['source_file'],
                    'mode': spec['mode'],
                    'skipped': spec['skipped'],
                }
            )
            continue
        mesh, input_triangles, post_fe_triangles, reason = _ingest(spec, volumes)
        if reason is not None:
            results.append(
                {
                    'name': spec['name'],
                    'source_file': spec['source_file'],
                    'mode': spec['mode'],
                    'skipped': reason,
                }
            )
            continue
        watertight = bool(mesh.is_manifold and mesh.n_open_edges == 0)
        smoothed, vol_raw, vol_smoothed, drift_pct, smooth_rec = _smooth(
            mesh, args.smooth_iters, args.pass_band, watertight
        )
        record = _Work(
            name=spec['name'],
            source_file=spec['source_file'],
            mode=spec['mode'],
            watertight=watertight,
            input_triangles=input_triangles,
            post_fe_triangles=post_fe_triangles,
            volume_raw_mm3=vol_raw,
            volume_smoothed_mm3=vol_smoothed,
            volume_drift_pct=drift_pct,
            volume_final_mm3=None,
            smooth_iters_requested=smooth_rec['iters_requested'],
            smooth_iters_used=smooth_rec['iters_used'],
            volume_drift_uncapped_pct=smooth_rec['volume_drift_uncapped_pct'],
            smoothing_note=smooth_rec['note'],
            smoothed=smoothed,
            mesh=smoothed,
            achieved=int(smoothed.n_faces),
            target=int(smoothed.n_faces),
        )
        results.append(record)
        work.append(record)

    if work:
        raw_total = sum(w.input_triangles for w in work)
        if raw_total < WINDOW_LO:
            # raw aggregate below the window: keep everything and report it
            targets = [w.input_triangles for w in work]
        else:
            targets = _allocate_targets(
                [w.name for w in work], [w.input_triangles for w in work], args.budget
            )
        for w, target in zip(work, targets):
            w.target = target
            w.mesh, w.achieved = decimate_toward(
                w.smoothed, target, _floor_base(w.name), args.decimator
            )
        _aggregate_passes(work, args.budget, args.decimator)
        for w in work:
            w.volume_final_mm3 = float(w.mesh.volume) if w.watertight else None
        _cap_open_rims(work)  # render geometry only; metrics stay pre-cap
        items = _finalize_geometry(work)
        transform = items.transform
    else:
        items = []
        transform = _transform_record(np.zeros(3))

    info = glb_writer.write_glb(args.out, items, quantize=not args.no_quantize)

    rows = [_row_for(result, args.decimator) for result in results]
    total_in = sum(w.input_triangles for w in work)
    total_out = sum(w.achieved for w in work)
    totals = {
        'aggregate_input_triangles': total_in,
        'aggregate_final_triangles': total_out,
        'aggregate_reduction_pct': (
            round((1.0 - total_out / total_in) * 100.0, 3) if total_in else 0.0
        ),
        'budget': args.budget,
        'window': [WINDOW_LO, WINDOW_HI],
        'within_window': bool(WINDOW_LO <= total_out <= WINDOW_HI),
        'glb_bytes': info['glb_bytes'],
        'quantized': info['quantized'],
        'decimator': args.decimator,
    }

    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps({'structures': rows, 'totals': totals, 'transform': transform}, indent=2)
    )

    _print_table(rows, totals)
    return 0


if __name__ == '__main__':
    sys.exit(main())
