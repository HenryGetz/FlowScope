#!/usr/bin/env python3
"""Offline CT ROI extraction + quantization for the MPR volume pair (Phase 2).

Given a case root (CT + cardiac masks) this derives the shared-Contract ROI
grid, trilinearly resamples the CT onto it and quantizes to the viewer volume
pair written beside a GLB ``<stem>``::

    <stem>_volume.bin   256^3 uint8, x_fastest (byte = x + y*256 + 256*256*z)
    <stem>_meta.json    Contract meta (ijk->RAS affine, model_center_ras_mm, ...)

ROI definition (shared Contract): tight index bbox over the union of all
cardiac label voxels -> RAS mm -> expand 15 mm each side -> cube of side = max
extent per axis, centered on the padded bbox center; spacing = cube side / 256
(exactly 256^3 isotropic). Label voxels = every discovered mask/multilabel
file in the case root except the CT (all labels of a multilabel included):
non-palette cardiac labels (auricle, cardiac fats, ...) count toward the ROI
even though GLB mesh selection stays restricted to the palette.

CT input: ``ct.nii.gz | ct.nii | image.nii.gz | img.nii.gz`` in the case root
(nibabel; HU after scl_slope/scl_inter), else a ``dicom/`` or ``DICOM/`` series
subdir (SimpleITK; a clear error when SimpleITK is not importable). The ROI
resample is trilinear via SimpleITK when available, else
``scipy.ndimage.affine_transform``, else a pure-numpy gather.

Coordinate conventions follow the shared Contract: masks and CT share one voxel
index grid (checked), the meta affine maps voxel index (i, j, k, 1) -> RAS mm
and ``model_center_ras_mm`` is the RAS mm point that maps to the GLB-local
origin (the ``_finalize_geometry`` recenter of ``build_cardiac_glb.py``).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np

import structures

# CT lookup order in the case root (shared Contract)
CT_FILENAMES = ('ct.nii.gz', 'ct.nii', 'image.nii.gz', 'img.nii.gz')
DICOM_SUBDIRS = ('dicom', 'DICOM')
# output grid: exactly 256^3 isotropic (shared Contract)
GRID_DIM = 256
# ROI padding: 15 mm per side (shared Contract)
MARGIN_MM = 15.0
# quantization window (shared Contract): q = clamp(floor((HU + 150) * 255/600 + 0.5), 0, 255)
MIN_HU = -150
MAX_HU = 450
# HU read outside the CT FOV (air): quantizes to 0
_OUTSIDE_HU = -1000.0


def find_ct(case_dir) -> Path:
    """Locate the CT of one case root (shared Contract order).

    ``ct.nii.gz | ct.nii | image.nii.gz | img.nii.gz`` in the dir root, else a
    ``dicom/`` or ``DICOM/`` series subdir, else SystemExit with an actionable
    message. A DICOM input requires importable SimpleITK (clear error else).
    """
    root = Path(case_dir)
    for name in CT_FILENAMES:
        candidate = root / name
        if candidate.is_file():
            return candidate
    for sub in DICOM_SUBDIRS:
        candidate = root / sub
        if candidate.is_dir():
            _simpleitk()  # clear error now, not at series-read time
            return candidate
    raise SystemExit(
        f'find_ct: no CT under {root}: expected one of {" | ".join(CT_FILENAMES)} '
        f'in the case root, or a {" / ".join(DICOM_SUBDIRS)}/ DICOM series subdir'
    )


def load_ct(path):
    """Load one CT -> nibabel-style image (data at HU, affine RAS mm).

    NIfTI files load via nibabel (scl_slope/scl_inter applied on read); a
    DICOM series directory loads via SimpleITK and is wrapped in an equivalent
    in-memory NIfTI image (DICOM LPS physical space mapped to RAS mm).
    """
    p = Path(path)
    if p.is_dir():
        return _load_dicom_series(p)
    return _nibabel().load(str(p))


def _nibabel():
    try:
        import nibabel as nib
    except ImportError as exc:
        raise SystemExit(
            'extract_volume_roi: nibabel is required for NIfTI CT/mask input '
            '(pip install nibabel)'
        ) from exc
    return nib


def _simpleitk():
    try:
        import SimpleITK as sitk
    except ImportError as exc:
        raise SystemExit(
            'extract_volume_roi: the CT input is a DICOM series but SimpleITK is '
            'not importable (pip install SimpleITK), or provide a NIfTI CT '
            f'({" | ".join(CT_FILENAMES)})'
        ) from exc
    return sitk


def _load_dicom_series(dicom_dir: Path):
    """SimpleITK series read -> NIfTI-equivalent image (HU, RAS-mm affine)."""
    sitk = _simpleitk()
    reader = sitk.ImageSeriesReader()
    series_ids = reader.GetGDCMSeriesIDs(str(dicom_dir)) or ()
    if not series_ids:
        raise SystemExit(f'DICOM input {dicom_dir}: no DICOM series found')
    reader.SetFileNames(reader.GetGDCMSeriesFileNames(str(dicom_dir), series_ids[0]))
    image = reader.Execute()
    # GetArrayFromImage is [z, y, x]; Contract arrays are [x, y, z] (i, j, k)
    hu = np.transpose(sitk.GetArrayFromImage(image), (2, 1, 0)).astype(np.float32)
    spacing = np.asarray(image.GetSpacing(), dtype=np.float64)  # (x, y, z) mm
    direction = np.asarray(image.GetDirection(), dtype=np.float64).reshape(3, 3)
    affine = np.eye(4)
    affine[:3, :3] = direction @ np.diag(spacing)  # index -> LPS mm
    affine[:3, 3] = np.asarray(image.GetOrigin(), dtype=np.float64)
    affine[:3, :] = np.diag([-1.0, -1.0, 1.0]) @ affine[:3, :]  # LPS -> RAS mm
    img = _nibabel().Nifti1Image(hu, affine)  # data already at HU (slope 1, inter 0)
    return img


def _hu_and_affine(ct_img):
    """(HU float32 [x, y, z], RAS-mm affine) of a nibabel-style CT image.

    scl_slope / scl_inter are applied explicitly (raw stored values -> HU).
    On load nibabel moves the scaling into the ArrayProxy (the header reports
    None), so the proxy's slope/inter win over the header's.
    """
    dataobj = ct_img.dataobj
    get_unscaled = getattr(dataobj, 'get_unscaled', None)
    if get_unscaled is not None:
        raw = np.asanyarray(get_unscaled())
        slope = getattr(dataobj, 'slope', None)
        inter = getattr(dataobj, 'inter', None)
    else:
        raw = np.asanyarray(dataobj)
        slope = inter = None
    if slope is None or inter is None:
        header = getattr(ct_img, 'header', None)
        if header is not None:
            head_slope, head_inter = header.get_slope_inter()
            slope = head_slope if slope is None else slope
            inter = head_inter if inter is None else inter
    slope = 1.0 if slope is None or not np.isfinite(slope) or slope == 0.0 else float(slope)
    inter = 0.0 if inter is None or not np.isfinite(inter) else float(inter)
    hu = raw.astype(np.float32)
    if slope != 1.0 or inter != 0.0:
        hu = hu * np.float32(slope) + np.float32(inter)
    return hu, np.asarray(ct_img.affine, dtype=np.float64)


def assert_mask_grid(mask_shape, mask_affine, ct_shape, ct_affine, source) -> None:
    """Require one cardiac mask to share the CT voxel grid (index-space union)."""
    if tuple(mask_shape) != tuple(ct_shape) or not np.allclose(
        np.asarray(mask_affine, dtype=np.float64),
        np.asarray(ct_affine, dtype=np.float64),
        atol=1e-3,
    ):
        raise SystemExit(
            f'extract_volume_roi: {source} is not on the CT voxel grid '
            f'(mask {tuple(mask_shape)} vs CT {tuple(ct_shape)}); the ROI index bbox '
            'requires masks sharing the CT grid (resample the masks first)'
        )


def tight_roi_bbox(label_arrays: list[np.ndarray]):
    """Union of nonzero voxels over all cardiac masks -> inclusive (ijk_min, ijk_max).

    Bounds are int64 (3,) arrays in the shared mask index space; an empty
    union raises ValueError.
    """
    lo = hi = None
    for arr in label_arrays:
        mask = np.asarray(arr) != 0
        if mask.ndim != 3:
            raise ValueError(f'tight_roi_bbox: expected 3D label arrays, got {mask.shape}')
        if not mask.any():
            continue
        amin = np.empty(3, dtype=np.int64)
        amax = np.empty(3, dtype=np.int64)
        for axis in range(3):
            others = tuple(a for a in range(3) if a != axis)
            idx = np.flatnonzero(mask.any(axis=others))
            amin[axis] = int(idx[0])
            amax[axis] = int(idx[-1])
        lo = amin if lo is None else np.minimum(lo, amin)
        hi = amax if hi is None else np.maximum(hi, amax)
    if lo is None:
        raise ValueError('tight_roi_bbox: no nonzero voxels in any label array')
    return lo, hi


def build_roi_grid(affine, ijk_min, ijk_max, margin_mm=MARGIN_MM) -> dict:
    """Padded RAS-mm bbox -> exactly 256^3 isotropic cube grid (shared Contract).

    The 8 corners of the inclusive voxel bbox map through ``affine`` to RAS mm
    and grow by ``margin_mm`` per side; the cube side is the max padded extent
    per axis, centered on the padded bbox center, with spacing = side / 256.
    Voxel (0, 0, 0) sits half a voxel in from the cube face so the 256 voxels
    tile the cube exactly (UVW [0, 1]^3 == the cube in the viewer).

    Returns ``{dimensions, spacing, origin, affine}``; ``affine`` maps voxel
    index (i, j, k, 1) -> RAS mm (NIfTI convention).
    """
    a = np.asarray(affine, dtype=np.float64)
    lo = np.minimum(np.asarray(ijk_min, dtype=np.float64), np.asarray(ijk_max, dtype=np.float64))
    hi = np.maximum(np.asarray(ijk_min, dtype=np.float64), np.asarray(ijk_max, dtype=np.float64))
    corners = np.array(
        [[i, j, k] for i in (lo[0], hi[0]) for j in (lo[1], hi[1]) for k in (lo[2], hi[2])],
        dtype=np.float64,
    )
    ras = corners @ a[:3, :3].T + a[:3, 3]
    bbox_lo = ras.min(axis=0) - float(margin_mm)
    bbox_hi = ras.max(axis=0) + float(margin_mm)
    side = float(np.max(bbox_hi - bbox_lo))
    center = 0.5 * (bbox_lo + bbox_hi)
    spacing = side / GRID_DIM
    # voxel (0, 0, 0) center: half a voxel inside the cube face
    origin = center - 0.5 * side + 0.5 * spacing
    grid_affine = np.diag([spacing, spacing, spacing, 1.0])
    grid_affine[:3, 3] = origin
    return {
        'dimensions': [GRID_DIM] * 3,
        'spacing': [float(spacing)] * 3,
        'origin': [float(v) for v in origin],
        'affine': grid_affine,
    }


def resample_hu(ct_img, grid) -> np.ndarray:
    """Trilinear resample of the CT (HU after scl_slope/scl_inter) onto ``grid``.

    Returns float32 (256, 256, 256) indexed [x, y, z] like the grid affine.
    SimpleITK when available, else ``scipy.ndimage.affine_transform``, else a
    pure-numpy trilinear gather; voxels outside the CT FOV read air (below the
    quantization window -> 0).
    """
    hu, ct_affine = _hu_and_affine(ct_img)
    dims = tuple(int(d) for d in grid['dimensions'])
    grid_affine = np.asarray(grid['affine'], dtype=np.float64)
    # grid voxel index -> CT voxel index
    matrix = np.linalg.solve(ct_affine, grid_affine)
    try:
        import SimpleITK as sitk
    except ImportError:
        sitk = None
    if sitk is not None:
        out = _resample_sitk(sitk, hu, ct_affine, grid, dims)
        if out is not None:
            return out
    try:
        from scipy import ndimage
    except ImportError:
        return _resample_numpy(hu, matrix, dims)
    return ndimage.affine_transform(
        hu,
        matrix[:3, :3],
        offset=matrix[:3, 3],
        output_shape=dims,
        order=1,
        mode='constant',
        cval=_OUTSIDE_HU,
        prefilter=False,
    ).astype(np.float32)


def _sitk_geometry(affine):
    """(spacing, origin, direction-row-major) of a SimpleITK image, or None.

    None when the affine is sheared (SimpleITK directions must be orthonormal);
    those inputs fall back to the scipy/numpy resamplers.
    """
    linear = np.asarray(affine, dtype=np.float64)[:3, :3]
    spacing = np.linalg.norm(linear, axis=0)
    if np.any(spacing <= 0.0):
        return None
    direction = linear / spacing
    if not np.allclose(direction.T @ direction, np.eye(3), atol=1e-6):
        return None
    return (
        [float(v) for v in spacing],
        [float(v) for v in np.asarray(affine, dtype=np.float64)[:3, 3]],
        [float(v) for v in direction.ravel()],
    )


def _resample_sitk(sitk, hu, ct_affine, grid, dims):
    ct_geom = _sitk_geometry(ct_affine)
    grid_geom = _sitk_geometry(np.asarray(grid['affine'], dtype=np.float64))
    if ct_geom is None or grid_geom is None:
        return None
    input_img = sitk.GetImageFromArray(np.transpose(hu, (2, 1, 0)))
    input_img.SetSpacing(ct_geom[0])
    input_img.SetOrigin(ct_geom[1])
    input_img.SetDirection(ct_geom[2])
    reference = sitk.Image([int(d) for d in dims], sitk.sitkFloat32)
    reference.SetSpacing(grid_geom[0])
    reference.SetOrigin(grid_geom[1])
    reference.SetDirection(grid_geom[2])
    out = sitk.Resample(
        input_img,
        reference,
        sitk.Transform(3, sitk.sitkIdentity),
        sitk.sitkLinear,
        float(_OUTSIDE_HU),
        sitk.sitkFloat32,
    )
    return np.transpose(sitk.GetArrayFromImage(out), (2, 1, 0)).astype(np.float32)


def _resample_numpy(hu, matrix, dims):
    """Pure-numpy trilinear gather of grid voxel centers (one z plane at a time)."""
    out = np.empty(dims, dtype=np.float32)
    m = matrix
    ii = np.arange(dims[0], dtype=np.float64)[:, None]
    jj = np.arange(dims[1], dtype=np.float64)[None, :]
    for kk in range(dims[2]):
        cx = m[0, 0] * ii + m[0, 1] * jj + (m[0, 2] * kk + m[0, 3])
        cy = m[1, 0] * ii + m[1, 1] * jj + (m[1, 2] * kk + m[1, 3])
        cz = m[2, 0] * ii + m[2, 1] * jj + (m[2, 2] * kk + m[2, 3])
        out[:, :, kk] = _trilinear_slice(hu, cx, cy, cz)
    return out


def _trilinear_slice(vol, cx, cy, cz):
    """Trilinear gather at [x, y] coords (float arrays broadcastable to (nx, ny))."""
    nx, ny, nz = vol.shape
    x0 = np.clip(np.floor(cx), 0, nx - 2)
    y0 = np.clip(np.floor(cy), 0, ny - 2)
    z0 = np.clip(np.floor(cz), 0, nz - 2)
    fx = (cx - x0).astype(np.float32)
    fy = (cy - y0).astype(np.float32)
    fz = (cz - z0).astype(np.float32)
    x0 = x0.astype(np.intp)
    y0 = y0.astype(np.intp)
    z0 = z0.astype(np.intp)
    x1, y1, z1 = x0 + 1, y0 + 1, z0 + 1
    c00 = vol[x0, y0, z0] * (1.0 - fx) + vol[x1, y0, z0] * fx
    c10 = vol[x0, y1, z0] * (1.0 - fx) + vol[x1, y1, z0] * fx
    c01 = vol[x0, y0, z1] * (1.0 - fx) + vol[x1, y0, z1] * fx
    c11 = vol[x0, y1, z1] * (1.0 - fx) + vol[x1, y1, z1] * fx
    blend = (c00 * (1.0 - fy) + c10 * fy) * (1.0 - fz) + (c01 * (1.0 - fy) + c11 * fy) * fz
    inside = (
        (cx >= 0.0) & (cx <= nx - 1) & (cy >= 0.0) & (cy <= ny - 1) & (cz >= 0.0) & (cz <= nz - 1)
    )
    return np.where(inside, blend, np.float32(_OUTSIDE_HU)).astype(np.float32)


def quantize_hu(hu) -> np.ndarray:
    """uint8 = clamp(floor((HU - MIN_HU) * (255/600) + 0.5), 0, 255) (shared Contract)."""
    q = (np.asarray(hu, dtype=np.float64) - MIN_HU) * (255.0 / 600.0) + 0.5
    return np.clip(np.floor(q), 0.0, 255.0).astype(np.uint8)


def volume_paths(glb_path) -> tuple[Path, Path]:
    """(<stem>_volume.bin, <stem>_meta.json) beside the GLB."""
    p = Path(glb_path)
    return (
        p.with_name(p.stem + '_volume.bin'),
        p.with_name(p.stem + '_meta.json'),
    )


def export_volume(out_bin: Path, out_meta: Path, vol_u8, grid, case_id, model_center_ras_mm) -> None:
    """Write the x_fastest volume bin + Contract meta.json (256^3 = 16777216 bytes)."""
    dims = [int(d) for d in grid['dimensions']]
    vol_u8 = np.asarray(vol_u8)
    if vol_u8.dtype != np.uint8 or list(vol_u8.shape) != dims:
        raise ValueError(
            f'export_volume: expected uint8 {tuple(dims)}, got {vol_u8.dtype} {vol_u8.shape}'
        )
    out_bin = Path(out_bin)
    out_meta = Path(out_meta)
    out_bin.parent.mkdir(parents=True, exist_ok=True)
    # F-order flatten: byte index = x + y*dimX + dimX*dimY*z (x fastest)
    out_bin.write_bytes(np.ascontiguousarray(vol_u8).tobytes(order='F'))
    meta = {
        'case_id': str(case_id),
        'dimensions': dims,
        'spacing': [float(s) for s in grid['spacing']],
        'origin': [float(v) for v in grid['origin']],
        'affine': [[float(v) for v in row] for row in np.asarray(grid['affine'])],
        'model_center_ras_mm': [float(v) for v in model_center_ras_mm],
        'window': {'min_hu': MIN_HU, 'max_hu': MAX_HU},
        'layout': 'x_fastest',
    }
    out_meta.write_text(json.dumps(meta, indent=2) + '\n')


def _is_multilabel_filename(name: str) -> bool:
    stem = structures.strip_suffix(name).lower()
    return stem == 'labels' or stem == 'multilabel' or stem.endswith('_multilabel')


def _iter_label_masks(case_dir: Path, ct_path: Path):
    """Yield (path, binary) label masks of a case root (ROI membership).

    Mask discovery mirrors ``--ts-dir`` (the dir itself plus one sub-level of
    ``*.nii.gz``), excluding the CT file: ROI = the union of ALL label-mask
    voxels, so every discovered mask file contributes (mask files via
    ``data > 0.5``, multilabel files via ``data != 0`` = every label) without
    any palette filter. All yielded masks share the CT voxel grid (checked by
    the caller).
    """
    nib = _nibabel()
    root = Path(case_dir)
    exclude = Path(ct_path).resolve()
    files = [p for p in root.glob('*.nii.gz') if p.is_file()]
    for sub in sorted(p for p in root.glob('*') if p.is_dir()):
        files.extend(p for p in sub.glob('*.nii.gz') if p.is_file())
    for path in sorted(files):
        if path.resolve() == exclude:
            continue
        data = np.asanyarray(nib.load(str(path)).dataobj)
        binary = (data != 0) if _is_multilabel_filename(path.name) else (data > 0.5)
        if binary.any():
            yield path, binary


def _resolve_model_center(args, glb_path: Path) -> list[float]:
    """model_center_ras_mm from --model-center-ras-mm, else <stem>.json, else exit."""
    if args.model_center_ras_mm is not None:
        return [float(v) for v in args.model_center_ras_mm]
    report = glb_path.with_suffix('.json')
    if report.is_file():
        try:
            center = json.loads(report.read_text()).get('model_center_ras_mm')
        except (OSError, ValueError):
            center = None
        if isinstance(center, (list, tuple)) and len(center) == 3:
            return [float(v) for v in center]
    raise SystemExit(
        'extract_volume_roi: model_center_ras_mm (RAS mm point mapping to the GLB-local '
        'origin) is unknown: pass --model-center-ras-mm X Y Z, or build the GLB with '
        "build_cardiac_glb.py --input (which records 'model_center_ras_mm' in its report "
        f'JSON; looked for {report.name} beside --output)'
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description='Extract + quantize the co-registered 256^3 CT ROI volume pair for one case.'
    )
    parser.add_argument(
        '--input', required=True, metavar='DIR', help='case root (CT + cardiac masks)'
    )
    parser.add_argument(
        '--output',
        required=True,
        metavar='GLB',
        help='target GLB path; <stem>_volume.bin / <stem>_meta.json are written beside it',
    )
    parser.add_argument(
        '--case-id', default=None, help='case id recorded in meta.json (default: GLB stem)'
    )
    parser.add_argument(
        '--model-center-ras-mm',
        nargs=3,
        type=float,
        default=None,
        metavar=('X', 'Y', 'Z'),
        help='RAS mm point mapping to the GLB-local origin (default: the '
        "report JSON 'model_center_ras_mm' beside --output)",
    )
    args = parser.parse_args(argv)

    case_dir = Path(args.input)
    glb_path = Path(args.output)
    case_id = args.case_id if args.case_id else glb_path.stem

    ct_path = find_ct(case_dir)
    ct_img = load_ct(ct_path)
    hu_ct, ct_affine = _hu_and_affine(ct_img)
    ct_shape = tuple(int(s) for s in hu_ct.shape)

    lo = hi = None
    n_masks = 0
    for path, binary in _iter_label_masks(case_dir, ct_path):
        assert_mask_grid(binary.shape, ct_affine, ct_shape, ct_affine, path)
        mask_lo, mask_hi = tight_roi_bbox([binary])
        lo = mask_lo if lo is None else np.minimum(lo, mask_lo)
        hi = mask_hi if hi is None else np.maximum(hi, mask_hi)
        n_masks += 1
    if lo is None:
        raise SystemExit(
            f'extract_volume_roi: no label masks under {case_dir} '
            '(ROI = union of ALL mask files in the case root, minus the CT)'
        )

    grid = build_roi_grid(ct_affine, lo, hi)
    vol_u8 = quantize_hu(resample_hu(ct_img, grid))
    model_center_ras_mm = _resolve_model_center(args, glb_path)
    out_bin, out_meta = volume_paths(glb_path)
    export_volume(out_bin, out_meta, vol_u8, grid, case_id, model_center_ras_mm)
    print(
        f'{case_id}: roi masks {n_masks}, ijk {tuple(int(v) for v in lo)}..{tuple(int(v) for v in hi)}'
        f' -> 256^3 @ {grid["spacing"][0]:.4f} mm; wrote {out_bin} '
        f'({out_bin.stat().st_size} bytes) + {out_meta}'
    )
    return 0


if __name__ == '__main__':
    sys.exit(main())
