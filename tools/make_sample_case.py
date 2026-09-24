#!/usr/bin/env python3
"""Generate a synthetic contrast-CT cardiac case for pipeline verification.

Writes a patient-space NIfTI case on an RAS grid (affine = diag(spacing) with a
real origin) into data/case_01 by default:

    ct.nii.gz                   int16 HU volume (raw ints + scl_slope/scl_inter,
                                get_fdata() yields HU)
    chambers/*.nii.gz           8 cardiac label masks (uint8, 1 inside, 0 outside)
    veins/*.nii.gz              5 great-vessel label masks
    coronaries/*.nii.gz         2 coronary label masks

Geometry is analytic (numpy meshgrid + ellipsoid/tube level sets): every mask
and its CT lumen are produced from one level set, so lumen boundaries in the CT
match the mask boundaries exactly (co-registration by construction). CT HU
inside each mask is exactly the anatomy table value; gaussian noise (sigma 15
HU) textures everything outside the masks (air, body, lungs, bone, walls), and
the whole volume is clipped to the standard [-1024, 3071] HU range.

Anatomy (RAS mm; +x patient right, +y anterior, +z superior):
  air -1000 | body ellipse +40 | two lung lobes -800 (antero-lateral, covering
  the heart bbox laterally) | vertebral body +700 with a +800 cortical rim
  (posterior, inside the cardiac bbox + 15 mm) | LV wall shell +55 (~10 mm
  ellipsoid shell whose interior is the LV cavity) | contrast-filled cavities
  and great vessels +250 (LV/RV cavities interior to the myocardial shell,
  atria/auricle inside CT-rendered +55 walls) | pericardial/epicardial fat -85
  (surface rind + grooves + posterior pad) | coronaries +250 (thin tubes in
  the epicardial fat grooves).

Mask filenames are binding and verified against pipeline/structures.py:
resolve_name(<file>) must map every filename to itself, loudly failing
otherwise.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:  # deferred so --help works in dependency-less environments
    import nibabel as nib
    import numpy as np
except ImportError as _exc:  # surfaced after argparse in main()
    np = None
    nib = None
    _IMPORT_ERROR: ImportError | None = _exc
else:
    _IMPORT_ERROR = None

REPO_ROOT = Path(__file__).resolve().parent.parent

# --- grid (RAS mm) -----------------------------------------------------------
ORIGIN_MM = (-160.0, -160.0, -120.0)  # RAS mm at voxel (0, 0, 0) == affine[:3][3]
FOV_MM = (320.0, 320.0, 256.0)  # at 1 mm spacing -> 320 x 320 x 256 voxels
MAX_VOXELS = 32_000_000

# --- HU table ----------------------------------------------------------------
AIR_HU = -1000.0
BODY_HU = 40.0
LUNG_HU = -800.0
BONE_HU = 700.0  # vertebral body (cancellous)
CORTEX_HU = 800.0  # vertebral cortical rim (the ~+800 HU band)
WALL_HU = 55.0  # myocardium / atrial wall soft tissue
CONTRAST_HU = 250.0  # blood pools and great vessels
FAT_HU = -85.0  # pericardial / epicardial fat
NOISE_SIGMA_HU = 15.0
HU_CLIP = (-1024.0, 3071.0)
SCL_SLOPE = 1.0  # on-disk: raw int16 = HU + 1024
SCL_INTER = -1024.0

# --- anatomy (world RAS mm) --------------------------------------------------
BODY_C = (0.0, 0.0, -5.0)
BODY_R = (140.0, 105.0, 112.0)
LUNG_L = ((-78.0, 2.0, 28.0), (52.0, 58.0, 88.0))  # patient left (x < 0)
LUNG_R = ((78.0, 2.0, 28.0), (52.0, 58.0, 88.0))  # patient right

VERTEBRA_C = (0.0, -52.0, 10.0)  # posterior midline
VERTEBRA_R = (16.0, 14.0, 15.0)
CORTEX_MM = 2.0  # cortical rim thickness

# Ventricular mass: outer envelope minus the two blood pools (>= 10 mm wall).
HEART_C = (-8.0, 0.0, 5.0)  # slightly left of midline
ENVELOPE_R = (48.0, 42.0, 60.0)  # outer ellipsoid
CHAMBER_R = (38.0, 32.0, 50.0)  # outer eroded 10 mm (cavity interior bound)
SEPTUM_N = (0.8556225, 0.5033074, 0.1207938)  # LV -> RV unit normal
SEPTUM_HALF_GAP = 6.0  # 12 mm interventricular septum
LV_POOL_C = (-16.0, -4.0, 1.0)  # posterior-left
LV_POOL_R = (24.0, 21.0, 38.0)
RV_POOL_C = (6.0, 9.0, 7.0)  # anterior-right, outflow to +z
RV_POOL_R = (26.0, 20.0, 40.0)

# Atria + left auricle (sitting on the ventricular base; CT-only walls).
LA_C = (-28.0, -14.0, 71.0)
LA_R = (21.0, 17.0, 18.0)
RA_C = (18.0, -4.0, 70.0)
RA_R = (19.0, 17.0, 20.0)
LAA_C = (-38.0, 6.0, 62.0)
LAA_R = (10.0, 7.0, 9.0)
ATRIAL_WALL_MM = 3.0

# Great vessels: chains of ((x, y, z), radius) control points, densified.
AORTA_CHAINS = [  # rising from the LV outflow, arching posterior-left
    [((-18.0, -4.0, 28.0), 12.0), ((-15.0, -5.0, 46.0), 12.0),
     ((-10.0, -8.0, 64.0), 11.0), ((-10.0, -14.0, 80.0), 10.5),
     ((-16.0, -22.0, 90.0), 10.0), ((-26.0, -30.0, 88.0), 9.5),
     ((-38.0, -34.0, 72.0), 9.0)],
]
PA_CHAINS = [  # main trunk + left branch, then right branch from the bifurcation
    [((4.0, 10.0, 34.0), 11.0), ((2.0, 12.0, 52.0), 11.0),
     ((0.0, 14.0, 66.0), 10.0), ((-10.0, 12.0, 78.0), 8.5),
     ((-30.0, 8.0, 84.0), 8.0)],
    [((0.0, 14.0, 66.0), 10.0), ((14.0, 10.0, 76.0), 8.5),
     ((34.0, 4.0, 80.0), 8.0)],
]
PV_CHAINS = [  # four tubes entering the LA
    [((-70.0, -2.0, 84.0), 7.0), ((-52.0, -8.0, 78.0), 6.5),
     ((-34.0, -12.0, 73.0), 6.5)],  # left superior
    [((-68.0, -6.0, 52.0), 7.0), ((-50.0, -10.0, 58.0), 6.5),
     ((-34.0, -12.0, 65.0), 6.5)],  # left inferior
    [((38.0, -30.0, 88.0), 7.0), ((20.0, -28.0, 82.0), 6.5),
     ((2.0, -24.0, 77.0), 6.5), ((-12.0, -17.0, 73.0), 6.5)],  # right sup.
    [((38.0, -32.0, 56.0), 7.0), ((18.0, -30.0, 60.0), 6.5),
     ((-4.0, -22.0, 64.0), 6.5), ((-14.0, -16.0, 67.0), 6.5)],  # right inf.
]
SVC_CHAINS = [  # into the top of the RA
    [((12.0, -6.0, 118.0), 10.0), ((13.0, -7.0, 98.0), 10.0),
     ((14.0, -8.0, 84.0), 10.0), ((15.0, -7.0, 78.0), 9.5)],
]
IVC_CHAINS = [  # up the right-posterior flank into the RA
    [((34.0, -34.0, -52.0), 11.0), ((33.0, -31.0, -20.0), 11.0),
     ((31.0, -26.0, 10.0), 11.0), ((27.0, -19.0, 38.0), 10.5),
     ((19.0, -10.0, 60.0), 10.0)],
]

# --- fat pads / epicardial grooves ------------------------------------------
FAT_SHELL_MM = 5.0  # epicardial fat: shell just outside the ventricular wall
PERI_SHELL_MM = 8.0  # pericardial fat rind: second shell outside it
AV_GROOVE_Z = (36.0, 50.0)  # atrioventricular groove band (base of ventricles)
IVS_GROOVE_D = 7.0  # anterior interventricular groove half-width (plane metric)
IVS_GROOVE_Z = (-56.0, 36.0)
PERI_RIND_ZMAX = 44.0
PERI_PAD_C = (-11.0, -44.0, 11.0)  # posterior pad (heart-to-spine fat)
PERI_PAD_R = (28.0, 12.0, 26.0)

CORONARY_R = 1.5  # 3 mm tubes
CORONARY_OFFSET = 2.5  # centerline sits mid-fat-groove, off the wall surface
AV_RING_Z = 42.0  # coronaries in the AV groove run at this height
LAD_Z = (34.0, -52.0)  # LAD: base to apex along the anterior IV groove
RCA_MARGINAL_ALPHA_DEG = 25.0  # right-marginal branch, fixed surface angle
RCA_MARGINAL_Z = (34.0, -16.0)


# --- slice primitives (level sets on one z slice of the meshgrid) -------------
def _ellipsoid(x, y, z, center, radii, inflate=0.0):
    """Ellipsoid level set |(p - c) / (r + inflate)|^2 <= 1."""
    cx, cy, cz = center
    rx, ry, rz = (r + inflate for r in radii)
    return (((x - cx) / rx) ** 2 + ((y - cy) / ry) ** 2 + ((z - cz) / rz) ** 2) <= 1.0


def _zlim_ell(center, radii, inflate=0.0):
    """World-z extent of an ellipsoid (evaluation window; exact)."""
    return (center[2] - radii[2] - inflate, center[2] + radii[2] + inflate)


def _tube_samples(chains, step=0.8):
    """Densify ((x,y,z), radius) control chains into overlapping spheres.

    The union of the spheres is the tapered tube level set
    min_i (|p - c_i| - r_i) <= 0; 0.8 mm steps bound scalloping well below a
    voxel for the thinnest (3 mm) tubes.
    """
    samples = []
    for chain in chains:
        pts = [np.asarray(p, dtype=float) for p, _ in chain]
        radii = [r for _, r in chain]
        for i in range(len(chain) - 1):
            seg = pts[i + 1] - pts[i]
            n = max(1, int(np.ceil(np.linalg.norm(seg) / step)))
            for t in np.linspace(0.0, 1.0, n, endpoint=False):
                samples.append((pts[i] + t * seg, radii[i] + t * (radii[i + 1] - radii[i])))
        samples.append((pts[-1], radii[-1]))
    return samples


def _tube(x, y, z, samples):
    out = np.zeros(x.shape, dtype=bool)
    for (px, py, pz), r in samples:
        dz = z - pz
        if abs(dz) > r:  # sphere cannot touch this slice
            continue
        out |= ((x - px) ** 2 + (y - py) ** 2 + dz * dz) <= r * r
    return out


def _zlim_tubes(samples):
    los = [pz - r for (px, py, pz), r in samples]
    his = [pz + r for (px, py, pz), r in samples]
    return (min(los), max(his))


def _plane_d(x, y, z):
    """Signed offset from the septal plane through HEART_C."""
    nx, ny, nz = SEPTUM_N
    return nx * (x - HEART_C[0]) + ny * (y - HEART_C[1]) + nz * (z - HEART_C[2])


def _surface_point(z, alpha, inflate):
    """Point at height z and azimuth alpha on the inflated ventricular envelope."""
    cx, cy, cz = HEART_C
    rx, ry, rz = (r + inflate for r in ENVELOPE_R)
    s2 = 1.0 - ((z - cz) / rz) ** 2
    if s2 <= 0.0:
        return None
    s = np.sqrt(s2)
    return (cx + rx * s * np.cos(alpha), cy + ry * s * np.sin(alpha), z)


def _lad_alpha(z):
    """Anterior azimuth where the septal plane cuts the inflated envelope at height z."""
    cz = HEART_C[2]
    rx, ry, rz = (r + CORONARY_OFFSET for r in ENVELOPE_R)
    s2 = 1.0 - ((z - cz) / rz) ** 2
    if s2 <= 0.0:
        return None
    s = np.sqrt(s2)
    nx, ny, nz = SEPTUM_N
    a, b, c = nx * rx * s, ny * ry * s, -nz * (z - cz)
    r = float(np.hypot(a, b))
    if r == 0.0:
        return None
    t = float(np.clip(c / r, -1.0, 1.0))
    phi = float(np.arctan2(a, b))
    cands = [float(np.arcsin(t)) - phi, np.pi - float(np.arcsin(t)) - phi]
    cands = [al for al in cands if np.sin(al) >= 0.0]  # anterior surface only
    return max(cands, key=lambda al: np.sin(al)) if cands else None


def _arc_points(z, a0_deg, a1_deg, step_deg=5.0):
    pts = []
    for deg in np.arange(a0_deg, a1_deg + 0.5 * step_deg, step_deg):
        p = _surface_point(z, np.deg2rad(deg), CORONARY_OFFSET)
        if p is not None:
            pts.append(p)
    return pts


def _coronary_chains():
    """Left coronary (LAD + circumflex) and right coronary (arc + marginal)."""
    lad = []
    for z in np.arange(LAD_Z[0], LAD_Z[1] - 1.0, -4.0):
        al = _lad_alpha(float(z))
        if al is None:
            continue
        p = _surface_point(float(z), al, CORONARY_OFFSET)
        if p is not None:
            lad.append((p, CORONARY_R))
    lcx = [(p, CORONARY_R) for p in _arc_points(AV_RING_Z, 100.0, 250.0)]
    rca_arc = [(p, CORONARY_R) for p in _arc_points(AV_RING_Z, -85.0, 80.0)]
    marginal = []
    for z in np.arange(RCA_MARGINAL_Z[0], RCA_MARGINAL_Z[1] - 1.0, -4.0):
        p = _surface_point(float(z), np.deg2rad(RCA_MARGINAL_ALPHA_DEG), CORONARY_OFFSET)
        if p is not None:
            marginal.append((p, CORONARY_R))
    left = [lad, lcx]
    right = [rca_arc, marginal]
    return left, right


# --- structure table (paint priority low -> high; later carves earlier) -------
def _structures():
    """Return [Structure, ...] in ascending paint priority.

    Every analytic(x, y, z) -> bool ndarray level set; zlim is a world-z
    evaluation window that contains its support. In overlaps the higher
    (later) structure owns the voxels, so masks end pairwise disjoint and each
    mask's CT lumen is exactly its table HU.
    """
    def _make_ell(c, r, inf):
        return lambda x, y, z: _ellipsoid(x, y, z, c, r, inf)

    def _make_tube(chains):
        samples = _tube_samples(chains)
        return (lambda x, y, z: _tube(x, y, z, samples), _zlim_tubes(samples))

    def _union(fns, zlims):
        lo = min(z[0] for z in zlims)
        hi = max(z[1] for z in zlims)

        def fn(x, y, z, fns=fns):
            out = np.zeros(x.shape, dtype=bool)
            for f in fns:
                out |= f(x, y, z)
            return out

        return fn, (lo, hi)

    lv_in = lambda x, y, z: (_ellipsoid(x, y, z, LV_POOL_C, LV_POOL_R)
                             & _ellipsoid(x, y, z, HEART_C, CHAMBER_R)
                             & (_plane_d(x, y, z) <= -SEPTUM_HALF_GAP))
    rv_in = lambda x, y, z: (_ellipsoid(x, y, z, RV_POOL_C, RV_POOL_R)
                             & _ellipsoid(x, y, z, HEART_C, CHAMBER_R)
                             & (_plane_d(x, y, z) >= SEPTUM_HALF_GAP))
    lv_z = _zlim_ell(LV_POOL_C, LV_POOL_R)
    rv_z = _zlim_ell(RV_POOL_C, RV_POOL_R)
    env_z = _zlim_ell(HEART_C, ENVELOPE_R)
    myo = (lambda x, y, z: _ellipsoid(x, y, z, HEART_C, ENVELOPE_R)
           & ~lv_in(x, y, z) & ~rv_in(x, y, z)), env_z

    def _atrial(c, r):
        return (lambda x, y, z, c=c, r=r: _ellipsoid(x, y, z, c, r)), _zlim_ell(c, r)

    # fat: epicardial shell in the grooves; pericardial rind + posterior pad
    shell_lo, shell_hi = FAT_SHELL_MM, PERI_SHELL_MM
    epi = (lambda x, y, z: (
        _ellipsoid(x, y, z, HEART_C, ENVELOPE_R, shell_hi)
        & ~_ellipsoid(x, y, z, HEART_C, ENVELOPE_R, shell_lo)
        & (((z >= AV_GROOVE_Z[0]) & (z <= AV_GROOVE_Z[1]))
           | ((np.abs(_plane_d(x, y, z)) <= IVS_GROOVE_D)
              & (y >= 0.0)
              & (z >= IVS_GROOVE_Z[0]) & (z <= IVS_GROOVE_Z[1])))),
        (IVS_GROOVE_Z[0], AV_GROOVE_Z[1]))
    peri_rind = (lambda x, y, z: (
        _ellipsoid(x, y, z, HEART_C, ENVELOPE_R, PERI_SHELL_MM)
        & ~_ellipsoid(x, y, z, HEART_C, ENVELOPE_R, shell_hi)
        & (z <= PERI_RIND_ZMAX)), _zlim_ell(HEART_C, ENVELOPE_R, PERI_SHELL_MM))
    pad = (lambda x, y, z: (_ellipsoid(x, y, z, PERI_PAD_C, PERI_PAD_R)
                            & ~_ellipsoid(x, y, z, VERTEBRA_C, VERTEBRA_R, CORTEX_MM)),
           _zlim_ell(PERI_PAD_C, PERI_PAD_R))
    peri = _union([peri_rind[0], pad[0]], [peri_rind[1], pad[1]])

    lca_chains, rca_chains = _coronary_chains()
    aorta = _make_tube(AORTA_CHAINS)
    pa = _make_tube(PA_CHAINS)
    pvs = _make_tube(PV_CHAINS)
    svc = _make_tube(SVC_CHAINS)
    ivc = _make_tube(IVC_CHAINS)
    lca = _make_tube(lca_chains)
    rca = _make_tube(rca_chains)

    S = _Structure
    return [
        S('pericardial_fat', 'chambers', FAT_HU, *peri),
        S('epicardial_fat', 'chambers', FAT_HU, *epi),
        S('heart_myocardium', 'chambers', WALL_HU, *myo),
        S('heart_ventricle_left', 'chambers', CONTRAST_HU, lv_in, lv_z),
        S('heart_ventricle_right', 'chambers', CONTRAST_HU, rv_in, rv_z),
        S('heart_atrium_left', 'chambers', CONTRAST_HU, *_atrial(LA_C, LA_R)),
        S('heart_atrium_right', 'chambers', CONTRAST_HU, *_atrial(RA_C, RA_R)),
        S('heart_atrial_appendage_left', 'chambers', CONTRAST_HU,
          *_atrial(LAA_C, LAA_R)),
        S('aorta', 'veins', CONTRAST_HU, *aorta),
        S('pulmonary_artery', 'veins', CONTRAST_HU, *pa),
        S('pulmonary_veins', 'veins', CONTRAST_HU, *pvs),
        S('vena_cava_superior', 'veins', CONTRAST_HU, *svc),
        S('vena_cava_inferior', 'veins', CONTRAST_HU, *ivc),
        S('coronary_artery_left', 'coronaries', CONTRAST_HU, *lca),
        S('coronary_artery_right', 'coronaries', CONTRAST_HU, *rca),
    ]


class _Structure:
    """One cardiac label mask: stem/subdir naming, table HU, level set."""

    def __init__(self, stem, subdir, hu, analytic, zlim):
        self.stem = stem
        self.subdir = subdir
        self.hu = hu
        self.analytic = analytic
        self.zlim = zlim


# --- base (unmasked) anatomy -------------------------------------------------
def _base_slice(x, y, z):
    """Air/body/lungs/vertebra/atrial walls for one z slice (pre-noise HU)."""
    sl = np.full(x.shape, AIR_HU, dtype=np.float32)
    body = _ellipsoid(x, y, z, BODY_C, BODY_R)
    sl[body] = BODY_HU
    for lung in (LUNG_L, LUNG_R):
        sl[_ellipsoid(x, y, z, *lung) & body] = LUNG_HU
    vb = _ellipsoid(x, y, z, VERTEBRA_C, VERTEBRA_R)
    sl[vb] = CORTEX_HU  # cortical rim band
    sl[_ellipsoid(x, y, z, VERTEBRA_C, VERTEBRA_R, -CORTEX_MM)] = BONE_HU
    for c, r in ((LA_C, LA_R), (RA_C, RA_R), (LAA_C, LAA_R)):  # CT-only walls
        wall = _ellipsoid(x, y, z, c, r, ATRIAL_WALL_MM) & ~_ellipsoid(x, y, z, c, r)
        sl[wall] = WALL_HU
    return sl


# --- name verification (binding filenames) -----------------------------------
def _verify_mask_names(structures):
    sys.path.insert(0, str(REPO_ROOT))
    try:
        from pipeline.structures import CARDIAC_IDS, resolve_name
    except ImportError as exc:
        raise SystemExit(f'cannot import pipeline.structures for name checks: {exc}')
    bad = []
    print('mask filename -> canonical id (pipeline.structures):')
    for st in structures:
        fname = f'{st.stem}.nii.gz'
        got = resolve_name(fname)
        in_cardiac = st.stem in CARDIAC_IDS
        print(f'  {fname} -> {got}  '
              f'[{"cardiac" if in_cardiac else "NOT in CARDIAC_IDS"}]')
        if got != st.stem:
            bad.append(f'{fname} -> {got!r} (must map to itself)')
        if not in_cardiac:
            bad.append(f'{fname} ({st.stem!r}) not in '
                       f'pipeline.structures.CARDIAC_IDS '
                       f'(pipeline would skip it as non_cardiac)')
    if bad:
        raise SystemExit('mask filenames must be canonical ids: resolve_name '
                         'self-map AND CARDIAC_IDS membership:\n  '
                         + '\n  '.join(bad))


# --- generation --------------------------------------------------------------
def _generate(args, out_dir):
    spacing = float(args.spacing_mm)
    if spacing <= 0.0:
        raise SystemExit('--spacing-mm must be positive')
    dims = tuple(int(round(f / spacing)) for f in FOV_MM)
    n_vox = dims[0] * dims[1] * dims[2]
    if n_vox > MAX_VOXELS:
        raise SystemExit(f'{dims[0]}x{dims[1]}x{dims[2]} = {n_vox} voxels exceeds '
                         f'{MAX_VOXELS}; raise --spacing-mm above {spacing:.3g}')

    affine = np.diag([spacing, spacing, spacing, 1.0])
    affine[:3, 3] = ORIGIN_MM
    xs = ORIGIN_MM[0] + spacing * np.arange(dims[0])
    ys = ORIGIN_MM[1] + spacing * np.arange(dims[1])
    zs = ORIGIN_MM[2] + spacing * np.arange(dims[2])
    x, y = np.meshgrid(xs, ys, indexing='ij')  # (nx, ny) slice meshgrid

    structures = _structures()
    _verify_mask_names(structures)

    rng = np.random.default_rng(args.seed)
    hu = np.empty(dims, dtype=np.float32)
    for k, z in enumerate(zs):
        sl = _base_slice(x, y, float(z))
        sl += NOISE_SIGMA_HU * rng.standard_normal(sl.shape, dtype=np.float32)
        hu[:, :, k] = sl

    out_dir.mkdir(parents=True, exist_ok=True)
    claimed = np.zeros(dims, dtype=bool)
    counts = {}
    for st in reversed(structures):  # highest paint priority keeps its voxels
        lo, hi = st.zlim
        mask = np.zeros(dims, dtype=bool)
        k0 = int(np.clip(np.searchsorted(zs, lo - spacing), 0, dims[2]))
        k1 = int(np.clip(np.searchsorted(zs, hi + spacing), 0, dims[2]))
        for k in range(k0, k1):
            mask[:, :, k] = st.analytic(x, y, float(zs[k]))
        mask &= ~claimed
        if not mask.any():
            raise SystemExit(f'mask {st.stem} is empty -- anatomy parameters drifted')
        claimed |= mask
        hu[mask] = st.hu  # exact table HU (masks are disjoint)
        path = out_dir / st.subdir / f'{st.stem}.nii.gz'
        path.parent.mkdir(parents=True, exist_ok=True)
        nib.save(nib.Nifti1Image(mask.astype(np.uint8), affine), path)
        counts[st.stem] = int(mask.sum())

    # vertebral body must sit inside the cardiac label bbox + 15 mm margin
    idx = np.argwhere(claimed)
    origin = np.asarray(ORIGIN_MM)
    lo_mm = origin + spacing * idx.min(axis=0)
    hi_mm = origin + spacing * idx.max(axis=0)
    for axis in range(3):
        if (VERTEBRA_C[axis] - VERTEBRA_R[axis] < lo_mm[axis] - 15.0 - spacing
                or VERTEBRA_C[axis] + VERTEBRA_R[axis] > hi_mm[axis] + 15.0 + spacing):
            raise SystemExit(
                f'vertebral body escapes the cardiac bbox + 15 mm margin on axis '
                f'{"xyz"[axis]}: heart [{lo_mm[axis]:.1f}, {hi_mm[axis]:.1f}] mm')

    np.clip(hu, HU_CLIP[0], HU_CLIP[1], out=hu)
    np.rint(hu, out=hu)
    raw = (hu - SCL_INTER).astype(np.int16)  # raw int16 = HU + 1024
    img = nib.Nifti1Image(raw, affine)
    img.header.set_data_dtype(np.int16)
    img.header.set_slope_inter(SCL_SLOPE, SCL_INTER)
    ct_path = out_dir / 'ct.nii.gz'
    nib.save(img, ct_path)

    print(f'case written to {out_dir}')
    print(f'  grid {dims[0]}x{dims[1]}x{dims[2]} @ {spacing:g} mm, '
          f'origin {tuple(ORIGIN_MM)} (RAS)')
    for st in structures:
        print(f'  {st.subdir}/{st.stem}.nii.gz  {counts[st.stem]:>9d} voxels  '
              f'{st.hu:g} HU')
    _verify_written(out_dir, ct_path, structures, affine, counts)


def _verify_written(out_dir, ct_path, structures, affine, counts):
    """Reload everything from disk and re-check the acceptance invariants."""
    ct = nib.load(str(ct_path))
    if not np.allclose(ct.affine, affine):
        raise SystemExit('ct.nii.gz affine differs from the generation grid')
    if ct.get_data_dtype() != np.int16:
        raise SystemExit(f'ct.nii.gz dtype {ct.get_data_dtype()}, expected int16')
    slope, inter = ct.dataobj.slope, ct.dataobj.inter
    if (slope, inter) != (SCL_SLOPE, SCL_INTER):
        raise SystemExit(f'ct.nii.gz scl_slope/scl_inter {slope}/{inter}, '
                         f'expected {SCL_SLOPE}/{SCL_INTER}')
    fdata = ct.get_fdata()  # HU
    lo, hi = float(fdata.min()), float(fdata.max())
    if lo > -950.0 or hi < 800.0:
        raise SystemExit(f'ct HU range [{lo:.0f}, {hi:.0f}] misses the '
                         f'~[-1000, +800] bands')
    for st in structures:
        mimg = nib.load(str(out_dir / st.subdir / f'{st.stem}.nii.gz'))
        if not np.allclose(mimg.affine, affine):
            raise SystemExit(f'{st.stem}: affine differs from the CT grid')
        if mimg.get_data_dtype() != np.uint8:
            raise SystemExit(f'{st.stem}: dtype {mimg.get_data_dtype()}, expected uint8')
        mask = np.asanyarray(mimg.dataobj)
        if not np.isin(mask, (0, 1)).all():
            raise SystemExit(f'{st.stem}: mask values outside {{0, 1}}')
        if int(mask.sum()) != counts[st.stem]:
            raise SystemExit(f'{st.stem}: reloaded voxel count differs from generated')
        if not (fdata[mask.astype(bool)] == st.hu).all():
            raise SystemExit(f'{st.stem}: CT HU inside the mask is not exactly '
                             f'{st.hu:g} -- lumen/mask boundaries diverged')
    print(f'  ct HU range [{lo:.0f}, {hi:.0f}], 15 masks verified on reload')


def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        description='Generate a synthetic contrast-CT cardiac sample case '
                    '(RAS-grid NIfTI CT + 15 cardiac label masks).')
    ap.add_argument('--out', default=None,
                    help='output case dir (default <repo>/data/case_01)')
    ap.add_argument('--seed', type=int, default=0,
                    help='noise RNG seed (default 0)')
    ap.add_argument('--spacing-mm', type=float, default=1.0,
                    help='isotropic voxel spacing in mm (default 1.0)')
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if _IMPORT_ERROR is not None:
        raise SystemExit(f'make_sample_case requires numpy and nibabel: {_IMPORT_ERROR}')
    out_dir = Path(args.out) if args.out else REPO_ROOT / 'data' / 'case_01'
    _generate(args, out_dir)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
