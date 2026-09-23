#!/usr/bin/env python3
"""Build a quantized cardiac anatomy GLB from segmentation inputs (Phase 1).

Three mutually exclusive ingestion modes (exactly one required):
  --stl-dir     ImageCAS-style per-structure STL meshes
  --ts-dir      TotalSegmentator output dir: per-structure *.nii.gz masks and/or
                a single labels.nii.gz / *_multilabel.nii.gz volume
  --multilabel  one multilabel NIfTI (integer label per structure)

Mask/multilabel modes select cardiac anatomy by default: only structures whose
resolved id is in the palette (the 16 contract ids plus 'heart') are ingested;
everything else is recorded as a skipped row with reason 'non_cardiac'.
--extra-names whitelists additional raw names (normalized), --all-names ingests
everything. --stl-dir is an explicit file set and is never filtered.

Per-structure chain (contract order):
  1. ingest        STL: pv.read -> clean() + triangulate() (watertight via
                   is_manifold/n_open_edges); masks: pv.ImageData + multithreaded
                   Flying Edges (vtkFlyingEdges3D, iso 0.5), FE index-space
                   points mapped to mm RAS with the nibabel affine
  2. Taubin        non-shrinking smooth_taubin (vtkWindowedSinc,
                   boundary_smoothing=False so open/truncated rims are not
                   slid); volume metrics (raw/smoothed/final/drift) recorded
                   per structure and emitted null + volume_note on open
                   (non-watertight) surfaces where the divergence-theorem
                   volume is meaningless
  3. decimation    to the per-structure budget allocation via --decimator:
                   decimate_pro (default) = PolyData.decimate_pro(reduction=...)
                   [vtkDecimatePro, the deliverable-named API]; quadric =
                   PolyData.decimate(target_reduction=...,
                   volume_preservation=True) [vtkQuadricDecimation]. Reduction
                   clamped <= 0.95, binary-searched (<= 6 iters) to within 2% of
                   target; per-structure floors survive trim/relax passes (the
                   floor only drops when the input mesh itself is smaller);
                   then trim/relax passes onto the hard window
  4. finalize      recompute per-vertex normals (consistent, non-splitting) and
                   apply the world transform (RAS mm -> Y-up meters, bbox center
                   of ALL structures at the origin)
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

WINDOW_LO = 100000
WINDOW_HI = 150000
FLOOR_TRIS = 1200
FLOOR_TRIS_VESSEL = 2500
VESSEL_IDS = frozenset(
    {'coronary_artery_left', 'coronary_artery_right', 'coronary_arteries', 'pulmonary_veins'}
)
CAP_FRACTION = 0.30
MAX_SEARCH_ITERS = 6
MAX_AGGREGATE_PASSES = 8
REDUCTION_CAP = 0.95
# (x, y, z) RAS mm -> (x, z, -y) Y-up meters
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
    smoothed: object
    mesh: object
    achieved: int
    target: int


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description='Build a quantized cardiac anatomy GLB (WebXR Phase 1).'
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--stl-dir', help='ImageCAS-style per-structure STL mesh directory')
    source.add_argument(
        '--ts-dir',
        help='TotalSegmentator output dir: per-structure *.nii.gz masks and/or '
        'a single labels.nii.gz / *_multilabel.nii.gz volume',
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
    parser.add_argument('--budget', type=int, default=120000)
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


def _collect_specs(args, label_map, volumes) -> list[dict]:
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
        files = sorted(Path(args.ts_dir).glob('*.nii.gz'))
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
                }
            )
        for path in files:
            if not _is_multilabel_filename(path.name):
                continue
            data, _affine = _load_volume(path, volumes)
            specs.extend(_multilabel_specs(path, data, label_map))
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

    # first selected source for a duplicated structure name wins; later ones are skipped
    seen: dict[str, str] = {}
    for spec in specs:
        if 'skipped' in spec:
            continue
        name = spec['name']
        if name in seen:
            spec['skipped'] = f'duplicate structure name (first seen in {seen[name]})'
        else:
            seen[name] = spec['source_file']
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

    Volume metrics are only computed for watertight meshes; the
    divergence-theorem volume of an open (FOV-truncated) surface is
    origin-dependent garbage.
    """
    before = float(mesh.volume) if watertight else None
    smoothed = mesh.smooth_taubin(
        n_iter=n_iter, pass_band=pass_band, boundary_smoothing=False
    )
    if before is None:
        return smoothed, None, None, None
    after = float(smoothed.volume)
    drift = abs(after - before) / before if before else 0.0
    return smoothed, before, after, drift * 100.0


def _allocate_targets(names, raws, budget) -> list[int]:
    """Budget share proportional to raw triangles, floors, 30% single cap."""
    raw_total = sum(raws)
    targets = []
    for name, raw in zip(names, raws):
        floor = FLOOR_TRIS_VESSEL if name in VESSEL_IDS else FLOOR_TRIS
        lower = min(floor, raw)  # floor only drops when the input mesh is smaller
        target = max(budget * raw / raw_total, lower)
        target = max(min(target, CAP_FRACTION * budget, raw), lower)
        targets.append(max(1, int(round(target))))
    return targets


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


def _finalize_geometry(work) -> list[dict]:
    """Step 4: recompute normals, world transform, recenter on the global bbox."""
    items = []
    for w in work:
        mesh = w.mesh.compute_normals(
            cell_normals=False,
            point_normals=True,
            split_vertices=False,
            consistent_normals=True,
        )
        normals = np.asarray(mesh.point_data['Normals'], dtype=np.float64)
        points = np.asarray(mesh.points, dtype=np.float64)
        indices = np.asarray(mesh.faces, dtype=np.int64).reshape(-1, 4)[:, 1:].reshape(-1)
        items.append(
            {
                'id': w.name,
                'points': points[:, [0, 2, 1]] * _Y_UP_SIGN * _MM_TO_M,
                'normals': normals[:, [0, 2, 1]] * _Y_UP_SIGN,
                'indices': indices,
            }
        )
    center = 0.5 * (
        np.min([it['points'].min(axis=0) for it in items], axis=0)
        + np.max([it['points'].max(axis=0) for it in items], axis=0)
    )
    for it in items:
        it['points'] = it['points'] - center
    return items


def _row_for(result, decimator) -> dict:
    if not isinstance(result, _Work):
        return {**result, 'decimator': decimator}
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
        smoothed, vol_raw, vol_smoothed, drift_pct = _smooth(
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
        items = _finalize_geometry(work)
    else:
        items = []

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
    report_path.write_text(json.dumps({'structures': rows, 'totals': totals}, indent=2))

    _print_table(rows, totals)
    return 0


if __name__ == '__main__':
    sys.exit(main())
