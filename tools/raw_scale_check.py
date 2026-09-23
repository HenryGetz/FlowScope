#!/usr/bin/env python3
"""Raw-scale reduction validation (Track 1 brief).

Measures the 90-95% reduction claim on 1.5M-5M triangle RAW meshes built from
REAL segmentation masks (TotalSegmentator CT case s0004). Per row: the binary
masks are ROI-cropped, linearly upsampled (scipy ``zoom(order=1)`` at zoom 3),
re-extracted with Flying Edges and merged into one multi-component raw mesh,
then run through the pipeline's Taubin smoothing (25 iters, pass band 0.1,
1% volume-drift cap with iteration-ladder fallback) and
``decimate_pro`` decimation toward the 120,000-triangle budget (the exact
``pipeline/build_cardiac_glb.py`` functions are reused). Each row records the
achieved reduction %, volume drift and GLB integrity in
``out/raw_scale_validation.json``; every row is labelled ``synthetic_derived:
true`` (the raw meshes are derived/tessellated, not scanned polygon soups).

Rows (zoom 3 default):
  s0004_heart_aorta      heart + aorta (the brief's example mask pair)
  s0004_cardiovascular   heart, aorta, pulmonary veins, SVC/IVC, LAA + great
                         vessels (brachiocephalic, carotid, subclavian)
  s0004_cardiothoracic   the cardiovascular set + sternum, costal cartilages,
                         ribs 1-4 (multi-component raw mesh)

Upsampling scales Flying Edges output ~zoom^2 per component (measured: 4.00x
at zoom 2, 9.04x at zoom 3), so rows are sized from real masks whose native
Flying Edges output already approaches the range.

Usage::

    .venv/bin/python tools/raw_scale_check.py [--rows id,id] [--zoom Z]
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import shlex
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import nibabel as nib
import numpy as np
import pyvista as pv
import vtk
from scipy.ndimage import zoom as ndzoom

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(REPO_ROOT / 'pipeline'))

import build_cardiac_glb as B  # noqa: E402  (Flying Edges / Taubin / decimate_pro / GLB)
from batch_build import integrity_check  # noqa: E402

TOOL = 'tools/raw_scale_check.py'
TOOL_VERSION = 1

MASK_ROOT = REPO_ROOT / 'data' / 'raw' / 'totalseg_ct' / 's0004' / 'segmentations'

SMOOTH_ITERS = 25
PASS_BAND = 0.1
DECIMATOR = 'decimate_pro'
RAW_RANGE = (1_500_000, 5_000_000)

_VASCULAR = (
    'heart',
    'aorta',
    'pulmonary_vein',
    'superior_vena_cava',
    'inferior_vena_cava',
    'atrial_appendage_left',
    'brachiocephalic_trunk',
    'brachiocephalic_vein_left',
    'brachiocephalic_vein_right',
    'common_carotid_artery_left',
    'common_carotid_artery_right',
    'subclavian_artery_left',
    'subclavian_artery_right',
)
_CHEST_WALL = (
    'sternum',
    'costal_cartilages',
    'rib_left_1',
    'rib_left_2',
    'rib_left_3',
    'rib_left_4',
    'rib_right_1',
    'rib_right_2',
    'rib_right_3',
    'rib_right_4',
)

ROWS = (
    {
        'id': 's0004_heart_aorta',
        'masks': ('heart', 'aorta'),
        'note': "brief's example mask pair (s0004 heart + aorta); native Flying "
        'Edges output is only ~109k triangles, so even at zoom 3 this row '
        'lands below the 1.5M raw target (recorded for reference)',
    },
    {
        'id': 's0004_cardiovascular',
        'masks': _VASCULAR,
        'note': 'heart + aorta + cardiac vessels/LAA + great vessels, merged '
        'multi-component raw mesh (in the 1.5M-5M raw range at zoom 3)',
    },
    {
        'id': 's0004_cardiothoracic',
        'masks': _VASCULAR + _CHEST_WALL,
        'note': 'cardiovascular set + sternum, costal cartilages, ribs 1-4 '
        '(in the 1.5M-5M raw range at zoom 3)',
    },
)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description='Raw-scale 1.5M-5M triangle reduction validation (Track 1).'
    )
    parser.add_argument('--out', default='out/raw_scale_validation.json')
    parser.add_argument('--glb-dir', default='out/raw_scale')
    parser.add_argument('--budget', type=int, default=120000)
    parser.add_argument('--threads', type=int, default=None)
    parser.add_argument(
        '--rows',
        default='all',
        help='comma-separated row ids (default all): '
        + ', '.join(r['id'] for r in ROWS),
    )
    parser.add_argument(
        '--zoom',
        type=float,
        default=None,
        help='linear upsample factor override (default 3.0 per row)',
    )
    args = parser.parse_args(argv)
    if args.threads is None:
        args.threads = os.cpu_count() or 1
    if args.threads < 1:
        parser.error('--threads must be >= 1')
    if args.zoom is not None and not (args.zoom > 0):
        parser.error('--zoom must be > 0')
    return args


def select_rows(spec: str) -> list[dict]:
    if spec.strip() == 'all':
        return list(ROWS)
    wanted = [s.strip() for s in spec.split(',') if s.strip()]
    by_id = {r['id']: r for r in ROWS}
    missing = [w for w in wanted if w not in by_id]
    if missing:
        raise SystemExit(f'unknown row id(s): {missing}; have {sorted(by_id)}')
    return [by_id[w] for w in wanted]


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _zoom_fe(mask_name: str, zoom: float, cache: dict):
    """ROI-crop, linearly upsample one binary mask and Flying-Edges it to mm RAS."""
    key = (mask_name, zoom)
    if key in cache:
        return cache[key]
    mask_path = MASK_ROOT / f'{mask_name}.nii.gz'
    img = nib.load(str(mask_path))
    binary = np.asanyarray(img.dataobj) > 0.5
    coords = np.argwhere(binary)
    lo = np.maximum(coords.min(axis=0) - 2, 0)
    hi = np.minimum(coords.max(axis=0) + 3, binary.shape)
    sub = binary[lo[0] : hi[0], lo[1] : hi[1], lo[2] : hi[2]]
    up = ndzoom(sub.astype(np.float32), zoom, order=1) > 0.5
    # original index i = (upsampled index + lo) / zoom  ->  p = (affine @ N) @ [i', 1]
    step = np.eye(4)
    step[:3, :3] = np.eye(3) / zoom
    step[:3, 3] = lo / zoom
    mesh = B._flying_edges(up, np.asarray(img.affine) @ step)
    del up, sub, binary
    gc.collect()
    if mesh is None or mesh.n_faces == 0:
        raise RuntimeError(f'Flying Edges produced no surface for {mask_path}')
    if float(mesh.volume) < 0.0:  # inward orientation: flip for signed volume
        faces = np.asarray(mesh.faces).reshape(-1, 4)
        faces[:, [2, 3]] = faces[:, [3, 2]]
        mesh.faces = faces
    cache[key] = (mesh, _display_path(mask_path))
    return cache[key]


def run_row(row: dict, args, cache: dict, glb_dir: Path) -> dict:
    zoom = args.zoom if args.zoom is not None else row.get('zoom', 3.0)
    started = time.perf_counter()
    parts, sources = [], []
    for mask_name in row['masks']:
        mesh, src = _zoom_fe(mask_name, zoom, cache)
        parts.append(mesh)
        sources.append(src)
    merged = pv.merge(parts, merge_points=False)  # keep components closed/manifold
    raw_tris = int(merged.n_faces)
    watertight = bool(merged.is_manifold and merged.n_open_edges == 0)

    smoothed, vol_raw, vol_smoothed, drift_pct, smooth_rec = B._smooth(
        merged, SMOOTH_ITERS, PASS_BAND, watertight
    )
    final, achieved = B.decimate_toward(smoothed, args.budget, B.FLOOR_TRIS, DECIMATOR)
    vol_final = float(final.volume) if watertight else None
    drift_total = (
        abs(vol_final - vol_raw) / vol_raw * 100.0 if watertight and vol_raw else None
    )

    items = B._finalize_geometry([SimpleNamespace(name=row['id'], mesh=final)])
    glb_path = glb_dir / f'{row["id"]}.glb'
    info = B.glb_writer.write_glb(str(glb_path), items, quantize=True)
    ic = integrity_check(glb_path, int(achieved), expect_quantized=bool(info['quantized']))

    in_range = RAW_RANGE[0] <= raw_tris <= RAW_RANGE[1]
    result = {
        'id': row['id'],
        'synthetic_derived': True,
        'note': row.get('note'),
        'source_masks': sources,
        'source_dataset': 'TotalSegmentator CT s0004 (real masks)',
        'upsample': f'scipy.ndimage.zoom order=1 (linear) on ROI-cropped binary '
        f'masks at zoom {zoom}, threshold 0.5',
        'zoom': zoom,
        'combine': 'per-mask Flying Edges, merged multi-component mesh',
        'raw_components': len(parts),
        'raw_triangles': raw_tris,
        'raw_range_target': list(RAW_RANGE),
        'in_raw_range': in_range,
        'watertight': watertight,
        'target_triangles': args.budget,
        'final_triangles': int(achieved),
        'achieved_reduction_pct': round((1.0 - achieved / raw_tris) * 100.0, 3),
        'volume_raw_mm3': round(vol_raw, 3) if watertight else None,
        'volume_smoothed_mm3': round(vol_smoothed, 3) if watertight else None,
        'volume_final_mm3': round(vol_final, 3) if watertight else None,
        'volume_drift_pct': round(drift_pct, 4) if drift_pct is not None else None,
        'volume_drift_total_pct': round(drift_total, 4) if drift_total is not None else None,
        'volume_note': (
            'merged multi-component volume = sum over components (overlapping '
            'components double-count their intersection); drift is measured '
            'consistently raw -> smoothed -> final'
        ),
        'smooth': {
            'method': 'Taubin smooth_taubin (pipeline/_smooth)',
            'n_iter': SMOOTH_ITERS,
            'iters_used': smooth_rec['iters_used'],
            'pass_band': PASS_BAND,
            'boundary_smoothing': False,
            'drift_cap_pct': B.DRIFT_CAP_PCT,
            'note': smooth_rec['note'],
            'volume_drift_uncapped_pct': smooth_rec['volume_drift_uncapped_pct'],
        },
        'decimator': DECIMATOR,
        'glb': {'path': _display_path(glb_path), 'glb_bytes': info['glb_bytes']},
        'integrity_ok': bool(ic['ok']),
        'integrity_checks': ic['checks'],
        'integrity_reason': ic['reason'],
        'runtime_s': round(time.perf_counter() - started, 2),
    }
    return result


def main(argv=None) -> int:
    args = parse_args(argv)
    rows = select_rows(args.rows)
    vtk.vtkMultiThreader.SetGlobalDefaultNumberOfThreads(args.threads)
    vtk.vtkSMPTools.Initialize(args.threads)

    glb_dir = Path(args.glb_dir)
    glb_dir.mkdir(parents=True, exist_ok=True)
    cache: dict = {}

    results = []
    for row in rows:
        print(f"[raw-scale] {row['id']}: {len(row['masks'])} masks ...", flush=True)
        result = run_row(row, args, cache, glb_dir)
        results.append(result)
        print(
            f"[raw-scale] {row['id']}: raw={result['raw_triangles']:,} tris "
            f"(in_range={result['in_raw_range']}) -> final={result['final_triangles']:,} "
            f"({result['achieved_reduction_pct']}% reduction), "
            f"drift={result['volume_drift_pct']}%, integrity_ok={result['integrity_ok']}, "
            f"{result['runtime_s']}s",
            flush=True,
        )

    in_range_rows = [r for r in results if r['in_raw_range']]
    drifts = [r['volume_drift_pct'] for r in results if r['volume_drift_pct'] is not None]
    payload = {
        'tool': TOOL,
        'tool_version': TOOL_VERSION,
        'command': shlex.join([sys.executable, *sys.argv]),
        'created': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'pipeline': 'pipeline/build_cardiac_glb.py (Flying Edges, Taubin _smooth, '
        'decimate_pro decimate_toward, glb_writer reused verbatim)',
        'budget': args.budget,
        'window': [B.WINDOW_LO, B.WINDOW_HI],
        'raw_range_target': list(RAW_RANGE),
        'units': 'volumes mm^3 (RAS affine), triangles dimensionless, SI elsewhere',
        'summary': {
            'rows_in_raw_range': f'{len(in_range_rows)}/{len(results)}',
            'in_range_reduction_pct_min': (
                min(r['achieved_reduction_pct'] for r in in_range_rows)
                if in_range_rows
                else None
            ),
            'in_range_reduction_pct_max': (
                max(r['achieved_reduction_pct'] for r in in_range_rows)
                if in_range_rows
                else None
            ),
            'volume_drift_pct_max': max(drifts) if drifts else None,
            'integrity_all_ok': all(r['integrity_ok'] for r in results),
        },
        'rows': results,
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2) + '\n')
    print(f'wrote {out_path}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
