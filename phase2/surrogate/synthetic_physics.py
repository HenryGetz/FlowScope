"""Physics ground truth for the FlowScope synthetic surrogate corpus.

SYNTHETIC ONLY -- no patient data. Analytic/semi-analytic fields on a [N,N,N]
voxel grid. This is NOT full Navier-Stokes CFD. The equations actually solved
and their simplifications are also recorded in the corpus `index.json`
("equations" block).

Flow -- steady laminar "Stokes-lite" (Hagen-Poiseuille + corrections):
  * u_axial(r,s) = 2*Q/(pi R(s)^2) * (1 - (r/R(s))^2)
    Poiseuille profile attached to each parametric centerline; Q per vessel.
  * dp/ds = 8*mu*Q/(pi R(s)^4)   (lubrication/Poiseuille law, integrated along
    each vessel; stenosis enters only through R(s)).
  * Bifurcation: Murray's-law split Q_i ~ R_i^3, pressure continuity at the
    junction plus a heuristic minor loss 0.3 * (1/2 rho u_p^2) ("junction
    correction"). Vessels blend with normalized Poiseuille shape weights, so the
    blended field is only approximately divergence-free near the junction.
  * Curved tubes: heuristic Dean-like secondary vortex pair from a streamfunction
    psi = A*ubar*R*h(r/R)*sin(2*phi), h(z) = z^2*(1-z^2)^2, amplitude
    A = min(0.15, 0.02*sqrt(De)), De = Re*sqrt(R0*|kappa|). Not a solved Dean
    flow -- an order-of-magnitude secondary pattern only.
  Assumptions: Newtonian mu = 3.5e-3 Pa*s, rho = 1060 kg/m^3, steady (no
  pulsatility/Womersley), rigid walls, no flow separation, no turbulence, no FSI.

Transport -- contrast advection-diffusion:
    dC/dt + div(u C) = D * Lap(C),   D ~ 1e-9..1e-6 m^2/s
  solved in its equivalent non-conservative form dC/dt + u.grad C = D Lap(C)
  (implemented as the conservative flux difference plus a C*div(u) correction):
  the model velocity is only approximately solenoidal (no radial component in
  varying-area tubes, junction cross-flow blending) and the non-conservative
  form preserves the concentration maximum principle despite that. First-order
  IMEX in time: explicit conservative MUSCL/minmod TVD-upwind advection +
  implicit backward-Euler diffusion (sparse LU). No-flux at lumen walls;
  advective in/outflow at vessel end caps (inlet faces driven by the injection
  profile, C dimensionless 0..1); no diffusive flux at caps. The cell Peclet
  number u*dx/D >> 2 for small D at these resolutions, so small-D samples are
  dominated by residual scheme dispersion: treat as qualitative transport
  ground truth, not resolved micro-diffusion.
"""

from __future__ import annotations

import numpy as np
from scipy.sparse import coo_matrix, identity
from scipy.sparse.linalg import splu
from scipy.spatial import cKDTree

MU = 3.5e-3        # Pa*s, dynamic viscosity
RHO = 1060.0       # kg/m^3
L_DOMAIN = 0.036   # m, cube edge of the voxel domain

STRAIGHT, CURVED, BIFURCATION, STENOSIS = 0, 1, 2, 3
GEOM_NAMES = {0: "straight", 1: "curved", 2: "bifurcation", 3: "stenosis"}
INLET_BC_NAMES = {0: "step_bolus", 1: "gaussian_bolus", 2: "square_pulse"}

N_CENTERLINE = 512            # dense centerline samples per vessel
STENOSIS_HALF_WIDTH_R = 2.5   # stenosis bump half-width in units of R0
JUNCTION_LOSS_K = 0.3         # heuristic minor-loss coefficient


# ----------------------------------------------------------------------------
# grid helpers

def cell_centers(n: int, length: float) -> np.ndarray:
    """Cell-centered coordinates along one axis (n samples in [0, length])."""
    return (np.arange(n) + 0.5) * (length / n)


def grid_points(n: int, length: float) -> np.ndarray:
    """Flattened (n^3, 3) cell centers, C-order matching reshape(n, n, n)."""
    x = cell_centers(n, length)
    gx, gy, gz = np.meshgrid(x, x, x, indexing="ij")
    return np.stack([gx, gy, gz], axis=-1).reshape(-1, 3)


def inlet_concentration(profile_id: int, t: float, t_c: float, tau: float) -> float:
    """Inlet contrast injection profiles (dimensionless 0..1).

    Profiles are timed relative to the domain transit time tau = L_cl/u_mean so
    that the contrast feature lies inside the domain at t_end: t_c (the hold
    end / bolus center) = clamp(t_end - 0.6*tau, 0.05*t_end, t_end).
    """
    if profile_id == 0:          # step_bolus: hold then release
        return 1.0 if t < t_c else 0.0
    if profile_id == 1:          # gaussian_bolus
        return float(np.exp(-0.5 * ((t - t_c) / (0.2 * tau)) ** 2))
    return 1.0 if t_c - 0.25 * tau < t < t_c + 0.25 * tau else 0.0  # square_pulse


# ----------------------------------------------------------------------------
# geometry: vessels are (arc)segments with radius profile R(s)

def _mk_vessel(o, t0, n0, kappa, ell, r_profile, kind, has_prox_cap,
               has_dist_cap, is_inlet):
    o = np.asarray(o, dtype=np.float64)
    t0 = np.asarray(t0, dtype=np.float64)
    t0 = t0 / np.linalg.norm(t0)
    n0 = np.asarray(n0, dtype=np.float64)
    n0 = n0 - np.dot(n0, t0) * t0
    n0 = n0 / np.linalg.norm(n0)

    s = np.linspace(0.0, ell, N_CENTERLINE)
    if kappa == 0.0:
        c = o[None, :] + s[:, None] * t0[None, :]
        tt = np.repeat(t0[None, :], N_CENTERLINE, axis=0)
        e1 = np.repeat(n0[None, :], N_CENTERLINE, axis=0)
    else:
        ks = kappa * s
        c = (o[None, :] + (np.sin(ks) / kappa)[:, None] * t0[None, :]
             + ((1.0 - np.cos(ks)) / kappa)[:, None] * n0[None, :])
        tt = (np.cos(ks)[:, None] * t0[None, :]
              + np.sin(ks)[:, None] * n0[None, :])
        # Frenet normal (toward curvature center), rotates along the arc
        sgn = 1.0 if kappa > 0 else -1.0
        e1 = sgn * (-np.sin(ks)[:, None] * t0[None, :]
                    + np.cos(ks)[:, None] * n0[None, :])
    e2 = np.cross(tt, e1)
    rs = np.asarray(r_profile(s), dtype=np.float64)
    return {
        "kind": kind, "s": s, "c": c, "tt": tt, "e1": e1, "e2": e2,
        "Rs": rs, "ell": float(ell), "kappa": float(kappa),
        "has_prox_cap": has_prox_cap, "has_dist_cap": has_dist_cap,
        "is_inlet": is_inlet,
    }


def make_vessels(g: dict) -> list:
    """Build vessel centerlines from the sampled geometry parameter dict.

    Vessel spans are radius-aware so the lumen (centerline padded by R(s)) fits
    the [0,L]^3 domain with a ~1 voxel margin. For curved tubes the realized
    arc length may be shorter than L (chord/sagitta fit); g["kappa"] is updated
    to the realized signed curvature phi/ell.
    """
    L, R0 = g["L"], g["R0"]
    margin = L / 48.0          # ~1 voxel safety at N=48
    q_in = g["u_mean_in"] * np.pi * R0 ** 2
    vessels = []

    if g["geometry_type"] == BIFURCATION:
        th = g["branch_angle"]
        r1 = g["branch_radius_ratio"] * R0
        r2 = (R0 ** 3 - r1 ** 3) ** (1.0 / 3.0)
        span_x = L - 2.0 * margin - R0 - max(r1, r2)
        xb = g["branch_axial_frac"] * span_x
        q_split = q_in * r1 ** 3 / (r1 ** 3 + r2 ** 3)
        parent = _mk_vessel([0, 0, 0], [1, 0, 0], [0, 1, 0], 0.0, xb,
                            lambda s: np.full_like(s, R0), "parent",
                            has_prox_cap=True, has_dist_cap=False, is_inlet=True)
        parent["Q"] = q_in
        vessels.append(parent)
        ld = (span_x - xb) / np.cos(th / 2.0)
        for sign, r_d, q_d, name in ((+1.0, r1, q_split, "daughter1"),
                                     (-1.0, r2, q_in - q_split, "daughter2")):
            d = _mk_vessel([xb, 0, 0],
                           [np.cos(th / 2), 0, sign * np.sin(th / 2)],
                           [0, 1, 0], 0.0, ld,
                           lambda s, r=r_d: np.full_like(s, r), name,
                           has_prox_cap=False, has_dist_cap=True, is_inlet=False)
            d["Q"] = q_d
            vessels.append(d)
    else:
        sign_y = g.get("curv_sign", 1.0)
        if g["geometry_type"] == CURVED:
            phi = abs(g["turn_angle"])
            bound = L - 2.0 * margin - 2.0 * R0
            ell = min(L,
                      bound * (0.5 * phi) / np.sin(0.5 * phi),
                      bound * phi / (1.0 - np.cos(0.5 * phi)))
            g["kappa"] = sign_y * phi / ell
        else:
            ell = L - 2.0 * margin - 2.0 * R0
        kappa = g["kappa"]
        if g["geometry_type"] == STENOSIS:
            s0 = g["stenosis_axial_frac"] * ell
            w = STENOSIS_HALF_WIDTH_R * R0
            sev = g["stenosis"]

            def r_profile(s, s0=s0, w=w, sev=sev, R0=R0):
                bump = np.where(np.abs(s - s0) < w,
                                0.5 * (1.0 + np.cos(np.pi * (s - s0) / w)),
                                0.0)
                # sev = fractional lumen-area reduction at the apex
                # (Young-Tsai-style idealized stenosis)
                return R0 * np.sqrt(1.0 - sev * bump)
        else:
            def r_profile(s, R0=R0):
                return np.full_like(s, R0)
        v = _mk_vessel([0, 0, 0], [1, 0, 0], [0, sign_y, 0], kappa, ell,
                       r_profile, GEOM_NAMES[g["geometry_type"]],
                       has_prox_cap=True, has_dist_cap=True, is_inlet=True)
        v["Q"] = q_in
        vessels.append(v)

    _add_pressure_profiles(vessels)
    _center_in_box(vessels, L)
    for v in vessels:
        v["tree"] = cKDTree(v["c"])
    return vessels


def _add_pressure_profiles(vessels: list) -> None:
    """p(s) via the lubrication law, junction minor loss, mean-outlet gauge 0."""
    for v in vessels:
        grad = 8.0 * MU * v["Q"] / (np.pi * v["Rs"] ** 4)
        ds = np.diff(v["s"])
        drop = np.concatenate(
            [[0.0], np.cumsum(0.5 * (grad[1:] + grad[:-1]) * ds)])
        v["drop"] = drop

    inlet = next(v for v in vessels if v["is_inlet"])
    inlet["p_prof"] = -inlet["drop"]
    for v in vessels:
        if v["is_inlet"]:
            continue
        u_p = inlet["Q"] / (np.pi * inlet["Rs"][-1] ** 2)
        loss = JUNCTION_LOSS_K * 0.5 * RHO * u_p ** 2
        v["p_prof"] = np.full_like(v["drop"], inlet["p_prof"][-1] - loss) \
            - v["drop"]

    term = [v for v in vessels if v["has_dist_cap"]]
    q_tot = sum(v["Q"] for v in term)
    p_out = sum(v["Q"] * v["p_prof"][-1] for v in term) / q_tot
    for v in vessels:
        v["p_prof"] = v["p_prof"] - p_out   # mean outlet gauge = 0


def _center_in_box(vessels: list, length: float) -> None:
    lo = np.min([np.min(v["c"] - v["Rs"][:, None], axis=0) for v in vessels],
                axis=0)
    hi = np.max([np.max(v["c"] + v["Rs"][:, None], axis=0) for v in vessels],
                axis=0)
    if np.any((hi - lo) > length):
        raise ValueError("geometry does not fit the domain box")
    shift = length / 2.0 - 0.5 * (lo + hi)
    for v in vessels:
        v["c"] = v["c"] + shift[None, :]


def project(v: dict, pts: np.ndarray):
    """Nearest-centerline projection -> (s_loc, rho, t, e1, e2, R, a, b).

    a, b are cross-plane coordinates in the (e1, e2) frame (e1 toward the
    curvature center for curved vessels).
    """
    _, k = v["tree"].query(pts)
    s_loc = np.clip(v["s"][k] + np.einsum("ij,ij->i", pts - v["c"][k],
                                          v["tt"][k]), 0.0, v["ell"])

    def interp_vec(arr):
        out = np.empty((pts.shape[0], 3))
        for j in range(3):
            out[:, j] = np.interp(s_loc, v["s"], arr[:, j])
        return out / np.maximum(np.linalg.norm(out, axis=1, keepdims=True), 1e-30)

    c_loc = np.empty((pts.shape[0], 3))
    for j in range(3):
        c_loc[:, j] = np.interp(s_loc, v["s"], v["c"][:, j])
    t_loc = interp_vec(v["tt"])
    e1_loc = interp_vec(v["e1"])
    e2_loc = np.cross(t_loc, e1_loc)
    r_loc = np.interp(s_loc, v["s"], v["Rs"])

    dv = pts - c_loc
    perp = dv - np.einsum("ij,ij->i", dv, t_loc)[:, None] * t_loc
    rho = np.linalg.norm(perp, axis=1)
    a = np.einsum("ij,ij->i", perp, e1_loc)
    b = np.einsum("ij,ij->i", perp, e2_loc)
    return s_loc, rho, t_loc, e1_loc, e2_loc, r_loc, a, b


def _vessel_velocity(v, ubar, rho, t_loc, e1_loc, e2_loc, r_loc, a, b, re_in):
    """Blending contribution: Poiseuille profile + optional Dean correction."""
    vel = 2.0 * ubar[:, None] * t_loc
    if v["kappa"] != 0.0:
        de = re_in * np.sqrt(v["Rs"][0] * abs(v["kappa"]))
        amp = min(0.15, 0.02 * np.sqrt(max(de, 0.0)))
        rh = np.clip(rho / r_loc, 0.0, 1.0)
        rho_s = np.maximum(rho, 1e-12)
        sin2p = 2.0 * a * b / rho_s ** 2
        cos2p = (a ** 2 - b ** 2) / rho_s ** 2
        h = rh ** 2 * (1.0 - rh ** 2) ** 2
        u_a = 2.0 * amp * ubar * r_loc * h * cos2p / rho_s
        u_b = -2.0 * amp * ubar * rh * (1.0 - rh ** 2) * (1.0 - 3.0 * rh ** 2) \
            * sin2p
        vel = vel + u_a[:, None] * e1_loc + u_b[:, None] * e2_loc
    return vel


def build_fields(g: dict, vessels: list, n: int, length: float, pts: np.ndarray):
    """Blended velocity/pressure/occupancy on the grid.

    Returns u [N,N,N,3] f32, p [N,N,N] f32, occ [N,N,N] bool, peak_u (m/s).
    """
    nf = pts.shape[0]
    num = np.zeros((nf, 3))
    den = np.zeros(nf)
    pnum = np.zeros(nf)
    occ = np.zeros(nf, dtype=bool)
    re_in = RHO * g["u_mean_in"] * 2.0 * g["R0"] / MU

    for v in vessels:
        s_loc, rho, t_loc, e1_loc, e2_loc, r_loc, a, b = project(v, pts)
        ubar = v["Q"] / (np.pi * r_loc ** 2)
        vel = _vessel_velocity(v, ubar, rho, t_loc, e1_loc, e2_loc, r_loc,
                               a, b, re_in)
        shape = np.clip(1.0 - (rho / r_loc) ** 2, 0.0, None)
        w = shape ** 2
        num += w[:, None] * vel
        pnum += w * np.interp(s_loc, v["s"], v["p_prof"])
        den += shape
        occ |= rho < r_loc

    safe = np.maximum(den, 1e-30)
    u = np.where(den[:, None] > 0, num / safe[:, None], 0.0)
    p = np.where(den > 0, pnum / safe, 0.0)

    shape3 = (n, n, n)
    u = u.reshape(*shape3, 3).astype(np.float32)
    p = p.reshape(*shape3).astype(np.float32)
    occ = occ.reshape(*shape3)
    speeds = np.linalg.norm(u.reshape(-1, 3)[occ.reshape(-1)], axis=1)
    peak_u = float(speeds.max()) if speeds.size else 0.0
    return u, p, occ, peak_u


def face_flags(vessels, occ, n, length):
    """Open/inlet face flags (n^3,3,2) for the transport solver.

    open  = advective in/outflow through a vessel end cap (no diffusive flux);
    inlet = subset of open faces at the inlet-vessel proximal cap, driven by the
            injection profile. Only the inlet vessel declares a proximal cap and
            only non-continuation ends declare a distal cap (the parent of a
            bifurcation continues into the junction and declares none).
    """
    nf = n ** 3
    dx = length / n
    pts = grid_points(n, length)
    fluid = np.flatnonzero(occ.reshape(-1))
    pf = pts[fluid]
    open_f = np.zeros((nf, 3, 2), dtype=bool)
    inlet_f = np.zeros((nf, 3, 2), dtype=bool)
    if fluid.size == 0:
        return open_f, inlet_f

    for k in range(3):
        for side in (0, 1):
            avec = np.zeros(3)
            avec[k] = -1.0 if side == 0 else 1.0
            probe = pf + dx * avec[None, :]
            for v in vessels:
                for prox in (True, False):
                    if prox and not v["has_prox_cap"]:
                        continue
                    if (not prox) and not v["has_dist_cap"]:
                        continue
                    r_end = v["Rs"][0] if prox else v["Rs"][-1]
                    s_loc, rho, _, _, _, _, _, _ = project(v, probe)
                    if prox:
                        mark = (s_loc <= 0.0) & (rho < r_end)
                    else:
                        mark = (s_loc >= v["ell"]) & (rho < r_end)
                    if not mark.any():
                        continue
                    open_f[fluid[mark], k, side] = True
                    if prox and v["is_inlet"]:
                        inlet_f[fluid[mark], k, side] = True
    return open_f, inlet_f


# ----------------------------------------------------------------------------
# transport: conservative MUSCL/minmod upwind advection + implicit diffusion

def _minmod(a, b):
    return np.where(a * b > 0.0,
                    np.where(np.abs(a) < np.abs(b), a, b), 0.0)


def _gather(arr, idx, fill):
    """arr[idx] with idx=-1 rows replaced by fill (scalar or per-row array)."""
    return np.where(idx >= 0, arr[np.maximum(idx, 0)], fill)


def _face_div(nbr, uc, opn, dx):
    """Discrete face-velocity divergence matching _adv_rhs's flux sharing.

    The model velocity is only approximately solenoidal (no radial component in
    varying-area tubes, junction cross-flow blending), so transport uses the
    non-conservative form -u.grad C = -div(u C) + C div(u); this is that div(u).
    """
    n_fl = uc.shape[0]
    d = np.zeros(n_fl)
    for axis in range(3):
        xm = nbr[:, 2 * axis]
        xp = nbr[:, 2 * axis + 1]
        uk = uc[:, axis]
        uq = 0.5 * (uk + _gather(uk, xp, uk))
        fp = np.where(xp >= 0, uq, np.where(opn[:, axis, 1], uk, 0.0))
        fm = np.where(xm >= 0, _gather(fp, xm, 0.0),
                      np.where(opn[:, axis, 0], uk, 0.0))
        d += (fp - fm) / dx
    return d


def _adv_rhs(c, nbr, uc, opn, inl, cin, dx, dvol):
    """-div(u C) with MUSCL/minmod upwind fluxes on the compact fluid set."""
    out = np.zeros_like(c)
    for axis in range(3):
        xm = nbr[:, 2 * axis]
        xp = nbr[:, 2 * axis + 1]
        xpp = np.where(xp >= 0, nbr[np.maximum(xp, 0), 2 * axis + 1], -1)
        uk = uc[:, axis]

        c_xm = _gather(c, xm, c)
        c_xp = _gather(c, xp, c)
        c_xpp = _gather(c, xpp, c_xp)
        s_i = _minmod(c_xp - c, c - c_xm)
        s_p = _minmod(c_xpp - c_xp, c_xp - c)

        uq = 0.5 * (uk + _gather(uk, xp, uk))
        fp = np.where(uq > 0.0,
                      uq * (c - 0.5 * s_i),
                      uq * (c_xp + 0.5 * s_p))
        # plus-side boundary faces: open -> advective only (outflow upwinds to c,
        # inflow brings 0 unless the face is an injection face), else wall.
        far_p = np.where(inl[:, axis, 1], cin, 0.0)
        fp_b = np.where(uq > 0.0, uq * c, uq * far_p)
        fp = np.where(xp >= 0, fp, np.where(opn[:, axis, 1], fp_b, 0.0))

        um = uk
        far_m = np.where(inl[:, axis, 0], cin, 0.0)
        fm_b = np.where(um > 0.0, um * far_m, um * c)
        fm = np.where(xm >= 0, _gather(fp, xm, 0.0),
                      np.where(opn[:, axis, 0], fm_b, 0.0))

        out -= (fp - fm) / dx
    out += c * dvol            # -> non-conservative -u.grad C (bounded)
    return out


def solve_transport(u, occ, open_f, inlet_f, d_diff, profile_id, t_end, t_c,
                    tau, n, length, cfl=0.7):
    """IMEX advection-diffusion of contrast C on the lumen voxels.

    Returns C as a float32 [N,N,N] grid (0 on solid voxels).
    """
    dx = length / n
    nf = n ** 3
    fluid = np.flatnonzero(occ.reshape(-1))
    n_fl = fluid.size
    if n_fl == 0:
        return np.zeros((n, n, n), dtype=np.float32)

    id_map = np.full(nf, -1, dtype=np.int32)
    id_map[fluid] = np.arange(n_fl, dtype=np.int32)
    idx3 = np.arange(nf, dtype=np.int32).reshape(n, n, n)
    neigh_full = np.empty((nf, 6), dtype=np.int32)
    for axis in range(3):
        m = np.full_like(idx3, -1)
        p = np.full_like(idx3, -1)
        sm0, sm1 = [slice(None)] * 3, [slice(None)] * 3
        sm0[axis], sm1[axis] = slice(1, None), slice(None, -1)
        m[tuple(sm0)] = idx3[tuple(sm1)]
        sp0, sp1 = [slice(None)] * 3, [slice(None)] * 3
        sp0[axis], sp1[axis] = slice(None, -1), slice(1, None)
        p[tuple(sp0)] = idx3[tuple(sp1)]
        neigh_full[:, 2 * axis] = m.reshape(-1)
        neigh_full[:, 2 * axis + 1] = p.reshape(-1)
    nbr = id_map[neigh_full[fluid]]

    uc = u.reshape(-1, 3)[fluid].astype(np.float64)
    opn = open_f.reshape(nf, 3, 2)[fluid]
    inl = inlet_f.reshape(nf, 3, 2)[fluid]
    dvol = _face_div(nbr, uc, opn, dx)

    # diffusion: graph Laplacian over fluid-fluid faces (Neumann elsewhere)
    rows, cols = [], []
    for axis in range(3):
        p_id = nbr[:, 2 * axis + 1]
        ok = p_id >= 0
        i = np.nonzero(ok)[0]
        rows += [i, p_id[ok]]
        cols += [p_id[ok], i]
    if rows:
        rows = np.concatenate(rows)
        cols = np.concatenate(cols)
        deg = np.bincount(rows, minlength=n_fl).astype(np.float64)
        rows = np.concatenate([rows, np.arange(n_fl)])
        cols = np.concatenate([cols, np.arange(n_fl)])
        data = np.concatenate([np.ones(rows.size - n_fl), -deg])
    else:
        rows = cols = np.arange(n_fl)
        data = np.zeros(n_fl)
    lap = coo_matrix((data, (rows, cols)), shape=(n_fl, n_fl)).tocsc()

    max_u = float(np.linalg.norm(uc, axis=1).max())
    n_steps = max(1, int(np.ceil(t_end / (cfl * dx / max(max_u, 1e-12)))))
    dt = t_end / n_steps
    lu = splu((identity(n_fl, format="csc") - (dt * d_diff) * lap).tocsc())

    c = np.zeros(n_fl)
    for step in range(n_steps):
        cin = inlet_concentration(profile_id, (step + 1) * dt, t_c, tau)
        rhs = c + dt * _adv_rhs(c, nbr, uc, opn, inl, cin, dx, dvol)
        c = lu.solve(rhs)
        np.maximum(c, 0.0, out=c)

    c_full = np.zeros(nf, dtype=np.float32)
    c_full[fluid] = c.astype(np.float32)
    return c_full.reshape(n, n, n)
