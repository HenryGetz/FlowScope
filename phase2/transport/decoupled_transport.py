#!/usr/bin/env python3
"""Track 4.3 - decoupled steady-state transport prototype for live contrast prediction.

Governing equation (SI units, C = dimensionless contrast volume fraction):

    dC/dt + u . grad(C) = D * laplacian(C)

(advection-dominated at arterial velocities: cell Peclet |u| h / D ~ 1e6..1e9).

Decoupling strategy
-------------------
The blood velocity field is time-periodic over the cardiac cycle and is computed
ONCE per anatomy:

    u(x, t) = u_bar(x) * (1 + a * sin(omega * t)),   a ~ 0.2,  HR 60..80 bpm

where ``u_bar`` comes from a steady solve (steady Stokes, finite volume on a
staggered MAC grid, streamwise-periodic, no-slip lumen walls, constant body
force calibrated to a target flow rate).  The result is cached to
``out/transport/u_cache.npz``.  Contrast transport (the scalar equation above)
is then re-solved for ARBITRARY injection parameters without touching the flow
solve: bolus volume 5..20 mL, injection rate 1..5 mL/s, molecular diffusion
D in 1e-9..1e-6 m^2/s.

Scalar discretization (finite volume, cell centred, conservative flux form):
  * advection: 2nd-order upwind face reconstruction (1st-order fallback where
    the 2nd upwind donor cell is not fluid), optional central scheme;
  * diffusion: 2nd-order central face gradients (zero flux at lumen walls);
  * time: forward Euler with CFL-controlled substepping,
    dt = min( CFL / sum_d max|u_d|(1+a)/h_d , 0.5 / (2 D sum_d 1/h_d^2) );
  * inlet: prescribed bolus C_in(t) (Dirichlet ghost), outlet: advective
    outflow with zero-gradient ghosts.

Geometry family (same synthetic tubular family as the phase-2 surrogate work,
implemented locally): box W x W x L, tube along x with gently curved centre
line y_c(x) = W/2 + curv_c * W * sin(2 pi x / L) and radius profile
R(x) = R0 * (1 - stenosis_a * exp(-((x - x0 L)/(stenosis_w L))^2)), lumen =
(y - y_c)^2 + (z - W/2)^2 < R(x)^2.  Physical scales (L = 0.12 m, W = 0.016 m,
diameter 0.008 m) are chosen for bolus-transit realism in SI units.

CLI:  python phase2/transport/decoupled_transport.py --grid 48
      sweeps the injection-parameter grid (decoupled arm) and writes
      out/transport/u_cache.npz + out/transport/sweep/** .
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import math
import os
import sys
import time

# BLAS threading is pathological for the many tiny dense ops in the substep loop
# and the Krylov back-solves (OpenBLAS spins up its pool per 1e4-element dot:
# ~55 ms -> ~20 us single-threaded on this host).  Pin before numpy loads.
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")

import numpy as np
import scipy.sparse as sp
import scipy.sparse.linalg as spla

TOOL = "phase2/transport/decoupled_transport.py"
VERSION = "1.0.0"

MU = 3.5e-3  # Pa s blood dynamic viscosity
RHO = 1060.0  # kg/m^3 (reported for reference; Stokes solve is rho-free)
U_MEAN = 0.30  # m/s target mean lumen velocity


def default_geometry() -> dict:
    """Synthetic tubular domain (phase-2 surrogate geometry family, SI scales)."""
    return {
        "kind": "stenosed_curved_tube",
        "length_m": 0.120,
        "width_m": 0.016,
        "diameter_m": 0.008,
        "stenosis_a": 0.25,  # fractional radius reduction at the throat
        "stenosis_x0": 0.5,  # throat position / length
        "stenosis_w": 0.10,  # gaussian width / length
        "curv_c": 0.05,  # centre line curvature amplitude / width
    }


def default_modulation(hr_bpm: float = 72.0, amp: float = 0.2) -> dict:
    return {"heart_rate_bpm": hr_bpm, "amp": amp, "omega_rad_s": 2.0 * math.pi * hr_bpm / 60.0}


def target_flow_m3s(geom: dict) -> float:
    """Baseline blood flow: U_MEAN over the nominal (unstenosed) lumen area."""
    r0 = 0.5 * geom["diameter_m"]
    return U_MEAN * math.pi * r0 * r0


# --------------------------------------------------------------------------------------
# Domain / grid
# --------------------------------------------------------------------------------------


def build_domain(n: int, geom: dict) -> dict:
    """Cell-centred grid n^3 over [0,L] x [0,W] x [0,W]; lumen mask + staggered face masks."""
    L = geom["length_m"]
    W = geom["width_m"]
    hx, hy, hz = L / n, W / n, W / n
    xc = (np.arange(n) + 0.5) * hx
    yc_ax = W / 2.0 + geom["curv_c"] * W * np.sin(2.0 * math.pi * xc / L)
    r = 0.5 * geom["diameter_m"] * (
        1.0
        - geom["stenosis_a"]
        * np.exp(-(((xc - geom["stenosis_x0"] * L) / (geom["stenosis_w"] * L)) ** 2))
    )
    Y = (np.arange(n) + 0.5) * hy
    Z = (np.arange(n) + 0.5) * hz
    rr2 = (Y[None, :, None] - yc_ax[:, None, None]) ** 2 + (Z[None, None, :] - W / 2.0) ** 2
    M = rr2 < r[:, None, None] ** 2  # (n, n, n) fluid mask

    # Staggered velocity face active masks: face carries a DOF iff both
    # neighbouring cells are fluid -> no-slip at the staircase lumen wall.
    vfx = np.roll(M, 1, axis=0) & M  # face i between cell i-1 | i, streamwise periodic
    vfy = np.zeros((n, n + 1, n), dtype=bool)  # face j between cell j-1 | j
    vfy[:, 1:n, :] = M[:, 0 : n - 1, :] & M[:, 1:n, :]
    vfz = np.zeros((n, n, n + 1), dtype=bool)  # face k between cell k-1 | k
    vfz[:, :, 1:n] = M[:, :, 0 : n - 1] & M[:, :, 1:n]

    # Transport x-faces: inlet face 0 (ghost | cell 0) .. outlet face n (cell n-1 | ghost);
    # inlet/outlet share the streamwise-periodic velocity face.
    cells_pad = np.concatenate([np.ones((1, n, n), bool), M, np.ones((1, n, n), bool)], 0)
    tfx = cells_pad[:-1] & cells_pad[1:]  # (n+1, n, n)

    # 2nd-order-upwind donor validity (cell c -> validity index c+2):
    # x-direction: inlet ghosts prescribed (valid), outlet ghosts zero-gradient (valid);
    # y/z: only fluid cells valid -> first-order fallback at the box/lumen boundary.
    valid_x = np.concatenate([np.ones((2, n, n), bool), M, np.ones((2, n, n), bool)], 0)
    valid_y = np.concatenate([np.zeros((n, 2, n), bool), M, np.zeros((n, 2, n), bool)], 1)
    valid_z = np.concatenate([np.zeros((n, n, 2), bool), M, np.zeros((n, n, 2), bool)], 2)

    def donor_flags(valid: np.ndarray, axis: int):
        # face f: CL=cell f-1, CLL=cell f-2, CR=cell f, CRR=cell f+1 (donors)
        s = [slice(None)] * 3
        s[axis] = slice(0, n + 1)  # valid(f-2): pad index f
        ok2p = np.ascontiguousarray(valid[tuple(s)])
        s[axis] = slice(3, 3 + n + 1)  # valid(f+1): pad index f+3
        ok2m = np.ascontiguousarray(valid[tuple(s)])
        return ok2p, ok2m

    ok2p_x, ok2m_x = donor_flags(valid_x, 0)
    ok2p_y, ok2m_y = donor_flags(valid_y, 1)
    ok2p_z, ok2m_z = donor_flags(valid_z, 2)

    # Face -> neighbouring cell index maps (for the divergence/gradient block)
    idx_cells = np.full((n, n, n), -1, np.int64)
    idx_cells[M] = np.arange(int(M.sum()), dtype=np.int64)

    def face_cells(axis):
        """(Lmap, Rmap): cell indices on each side of every face (-1 if none)."""
        fshp = [(n, n, n), (n, n + 1, n), (n, n, n + 1)][axis]
        Lm = np.full(fshp, -1, np.int64)
        Rm = np.full(fshp, -1, np.int64)
        if axis == 0:  # streamwise wrap: face i between cell i-1 | i
            Lm[:] = np.roll(idx_cells, 1, axis=0)
            Rm[:] = idx_cells
        else:
            dstL = [slice(None)] * 3
            srcL = [slice(None)] * 3
            dstR = [slice(None)] * 3
            srcR = [slice(None)] * 3
            dstL[axis] = slice(1, n + 1)  # face f>=1 has L cell f-1
            srcL[axis] = slice(0, n)
            dstR[axis] = slice(0, n)  # face f<n has R cell f
            srcR[axis] = slice(0, n)
            Lm[tuple(dstL)] = idx_cells[tuple(srcL)]
            Rm[tuple(dstR)] = idx_cells[tuple(srcR)]
        return Lm, Rm

    Lx, Rx = face_cells(0)
    Ly, Ry = face_cells(1)
    Lz, Rz = face_cells(2)

    return {
        "n": n,
        "geom": geom,
        "hx": hx, "hy": hy, "hz": hz,
        "x": xc, "yc": yc_ax, "r_profile": r,
        "M": M,
        "vfx": vfx, "vfy": vfy, "vfz": vfz,
        "tfx": tfx,
        "donor": {"x": (ok2p_x, ok2m_x), "y": (ok2p_y, ok2m_y), "z": (ok2p_z, ok2m_z)},
        "face_cells": {"x": (Lx, Rx), "y": (Ly, Ry), "z": (Lz, Rz)},
        "idx_cells": idx_cells,
        "cell_volume": hx * hy * hz,
        "n_fluid": int(M.sum()),
    }


# --------------------------------------------------------------------------------------
# Steady velocity solve (steady Stokes, MAC FV, MINRES on the symmetric saddle system)
# --------------------------------------------------------------------------------------


def _shift_index(arr: np.ndarray, axis: int, off: int) -> np.ndarray:
    """out[i] = arr[i + off] with -1 fill for out-of-range (off != 0)."""
    out = np.full_like(arr, -1)
    src = [slice(None)] * arr.ndim
    dst = [slice(None)] * arr.ndim
    if off > 0:
        src[axis] = slice(0, arr.shape[axis] - off)
        dst[axis] = slice(off, arr.shape[axis])
    else:
        src[axis] = slice(-off, arr.shape[axis])
        dst[axis] = slice(0, arr.shape[axis] + off)
    out[tuple(dst)] = arr[tuple(src)]
    return out


def assemble_stokes(dom: dict, mu: float = MU, f_body: float = 1.0):
    """Assemble the symmetric saddle system [[A, B^T], [B, 0]] (+ tiny (2,2) regularization).

    Unknowns: active staggered face velocities and fluid-cell pressures.
    Momentum rows: A = -mu * laplacian on dual volumes (u = 0 on inactive/wall
    faces, streamwise periodic), plus B^T p where B is the outward-flux
    divergence (solved pressure variable is q = -p_phys).  RHS: constant
    streamwise body force f_body [N/m^3].
    """
    n = dom["n"]
    hx, hy, hz = dom["hx"], dom["hy"], dom["hz"]
    a_x, a_y, a_z = hy * hz / hx, hx * hz / hy, hx * hy / hz
    V = dom["cell_volume"]

    comps = [("x", dom["vfx"]), ("y", dom["vfy"]), ("z", dom["vfz"])]
    idx_maps = {}
    offsets = {}
    off = 0
    for name, act in comps:
        idx = np.full(act.shape, -1, np.int64)
        idx[act] = np.arange(int(act.sum()), dtype=np.int64) + off
        idx_maps[name] = idx
        offsets[name] = (off, int(act.sum()))
        off += int(act.sum())
    n_faces = off
    n_cells = dom["n_fluid"]

    rowsA, colsA, valsA = [], [], []
    for name, act in comps:
        idx = idx_maps[name]
        nact = int(act.sum())
        diag = np.zeros(nact)
        for axis, (wt, periodic) in enumerate(((a_x, True), (a_y, False), (a_z, False))):
            for sgn in (-1, 1):
                if periodic:
                    nb = np.roll(idx, sgn, axis=axis)
                else:
                    nb = _shift_index(idx, axis, sgn)
                sel = act & (nb >= 0)
                if sel.any():
                    rowsA.append(idx[sel])
                    colsA.append(nb[sel])
                    valsA.append(np.full(int(sel.sum()), -mu * wt))
                diag += mu * wt  # Dirichlet-0 neighbours fold into the diagonal
        rowsA.append(idx[act])
        colsA.append(idx[act])
        valsA.append(diag)

    # ---- divergence block B (cells x faces): +area at L side, -area at R side ----
    rowsB, colsB, valsB = [], [], []
    areas = {"x": hy * hz, "y": hx * hz, "z": hx * hy}
    for name, act in comps:
        Lm, Rm = dom["face_cells"][name]
        fidx = idx_maps[name]
        sel = act
        rowsB.append(Lm[sel])
        colsB.append(fidx[sel])
        valsB.append(np.full(int(sel.sum()), areas[name]))
        rowsB.append(Rm[sel])
        colsB.append(fidx[sel])
        valsB.append(np.full(int(sel.sum()), -areas[name]))

    A = sp.coo_matrix(
        (np.concatenate(valsA), (np.concatenate(rowsA), np.concatenate(colsA))),
        shape=(n_faces, n_faces),
    ).tocsr()
    B = sp.coo_matrix(
        (np.concatenate(valsB), (np.concatenate(rowsB), np.concatenate(colsB))),
        shape=(n_cells, n_faces),
    ).tocsr()

    Md = sp.bmat([[A, B.T], [B, None]], format="csr")

    # tiny regularization of the pressure null space (constant p)
    scale = float(abs(A.diagonal()).max())
    eps = 1e-12 * scale
    reg = np.arange(n_faces, n_faces + n_cells)
    Md = (Md + sp.coo_matrix((np.full(n_cells, -eps), (reg, reg)), shape=Md.shape)).tocsr()

    rhs = np.zeros(n_faces + n_cells)
    fxv = idx_maps["x"][dom["vfx"]]
    rhs[fxv] = f_body * V

    layout = {
        "total": n_faces + n_cells,
        "n_faces": n_faces,
        "n_cells": n_cells,
        "off_p": n_faces,
        "idx": idx_maps,
    }
    return Md, rhs, layout


def solve_steady_stokes(dom: dict, target_q_m3s: float, mu: float = MU,
                        k_q: float | None = None, cg_maxiter: int = 5000) -> dict:
    """One steady Stokes solve with the body force calibrated to the target flow.

    Solver: the saddle system [[A, B^T], [B, -eps]] is reduced to the symmetric
    positive definite Schur complement (B A^-1 B^T + eps I) q = B A^-1 F, solved
    with preconditioned conjugate gradients; A is block diagonal in the three
    face-velocity components and is applied through sparse LU of each block.
    Stokes linearity: the map f_body -> Q is exactly linear, so with a known
    calibration constant k_q = Q / f_body (from any reference solve) the body
    force f = Q_target / k_q hits the target flow with no outer loop.  The
    achieved flow is measured and reported either way.
    """
    r0 = 0.5 * dom["geom"]["diameter_m"]
    f_guess = 8.0 * mu * U_MEAN / (r0 * r0)  # Poiseuille: u_mean = f R0^2 / (8 mu)
    f_body = f_guess if k_q is None else target_q_m3s / k_q

    t0 = time.perf_counter()
    M, rhs, layout = assemble_stokes(dom, mu=mu, f_body=f_body)
    t_asm = time.perf_counter() - t0

    nf = layout["n_faces"]
    nc = layout["n_cells"]
    ix = layout["idx"]
    nx = int(ix["x"].max()) + 1
    ny = int(ix["y"].max()) + 1 - nx
    nz = nf - nx - ny
    A = M[:nf, :nf].tocsc()
    Bm = M[nf:, :nf].tocsr()
    reg_eps = -float(M.diagonal()[nf])  # the (2,2) block stores -eps

    # LU of the three component blocks of A
    t0 = time.perf_counter()
    blocks = []
    off = 0
    for cnt in (nx, ny, nz):
        blocks.append(spla.splu(A[off : off + cnt, off : off + cnt]))
        off += cnt
    t_factor = time.perf_counter() - t0

    def ainv(v):
        out = np.empty_like(v)
        off = 0
        for cnt, lu in zip((nx, ny, nz), blocks):
            out[off : off + cnt] = lu.solve(v[off : off + cnt])
            off += cnt
        return out

    F = rhs[:nf]
    q_rhs = Bm @ ainv(F)

    def schur(v):
        return Bm @ ainv(Bm.T @ v) + reg_eps * v

    # Jacobi preconditioner: diag(B diag(A)^-1 B^T)
    dA = np.abs(A.diagonal())
    B2 = Bm.copy()
    B2.data **= 2
    ds = B2 @ (1.0 / np.maximum(dA, 1e-300)) + reg_eps
    P = spla.LinearOperator((nc, nc), matvec=lambda v: v / ds)
    Sop = spla.LinearOperator((nc, nc), matvec=schur)

    t0 = time.perf_counter()
    try:
        q_var, info = spla.cg(Sop, q_rhs, rtol=1e-10, atol=0.0, maxiter=cg_maxiter, M=P)
    except TypeError:
        q_var, info = spla.cg(Sop, q_rhs, tol=1e-10, atol=0.0, maxiter=cg_maxiter, M=P)
    t_cg = time.perf_counter() - t0

    u_vec = ainv(F - Bm.T @ q_var)
    x = np.concatenate([u_vec, q_var])
    res = float(np.linalg.norm(M @ x - rhs) / max(np.linalg.norm(rhs), 1e-300))

    n = dom["n"]
    ux = np.zeros((n, n, n))
    uy = np.zeros((n, n + 1, n))
    uz = np.zeros((n, n, n + 1))
    ux[dom["vfx"]] = x[ix["x"][dom["vfx"]]]
    uy[dom["vfy"]] = x[ix["y"][dom["vfy"]]]
    uz[dom["vfz"]] = x[ix["z"][dom["vfz"]]]
    p = np.zeros((n, n, n))
    p[dom["M"]] = -q_var  # saddle variable q = -p_phys
    p[dom["M"]] -= p[dom["M"]].mean()

    q = float((ux * dom["vfx"])[0].sum() * dom["hy"] * dom["hz"])  # any x-station: faces at fixed i
    return {
        "ux": ux, "uy": uy, "uz": uz, "p": p,
        "q_m3s": q,
        "k_q": q / f_body,
        "f_body": f_body,
        "residual": res,
        "cg_info": int(info),
        "assemble_s": t_asm,
        "factor_s": t_factor,
        "solve_s": t_cg,
    }


def velocity_cell_centred(sol: dict) -> np.ndarray:
    """(n,n,n,3) cell-centred velocity (caching/reporting convenience)."""
    ux, uy, uz = sol["ux"], sol["uy"], sol["uz"]
    return np.stack(
        [
            0.5 * (ux + np.roll(ux, -1, axis=0)),
            0.5 * (uy[:, :-1, :] + uy[:, 1:, :]),
            0.5 * (uz[:, :, :-1] + uz[:, :, 1:]),
        ],
        axis=-1,
    )


def validate_poiseuille(n: int = 32) -> dict:
    """Straight tube: compare the steady solve with the analytic parabolic profile."""
    geom = default_geometry()
    geom.update({"stenosis_a": 0.0, "curv_c": 0.0})
    dom = build_domain(n, geom)
    sol = solve_steady_stokes(dom, target_flow_m3s(geom))
    r0 = 0.5 * geom["diameter_m"]
    Y = (np.arange(n) + 0.5) * dom["hy"]
    Z = (np.arange(n) + 0.5) * dom["hz"]
    rr = np.sqrt((Y[:, None] - geom["width_m"] / 2) ** 2 + (Z[None, :] - geom["width_m"] / 2) ** 2)
    num = 0.5 * (sol["ux"] + np.roll(sol["ux"], -1, axis=0))  # cell-centred axial velocity
    ana = np.clip(r0 ** 2 - rr ** 2, 0.0, None)  # analytic parabola (shape only)
    ana3 = np.broadcast_to(ana[None], (n, n, n))
    act = dom["M"]
    s = (num[act] * ana3[act]).sum() / (ana3[act] ** 2).sum()  # best scale
    u_max = float(np.abs(num[act]).max())
    err_all = np.abs(num[act] - s * ana3[act]) / max(u_max, 1e-300)
    core = act & np.broadcast_to((rr < 0.8 * r0)[None], (n, n, n))
    err_core = np.abs(num[core] - s * ana3[core]) / max(u_max, 1e-300)
    return {
        "grid": n,
        "max_rel_err_all": float(err_all.max()),
        "max_rel_err_core": float(err_core.max()),
        "rms_rel_err_core": float(np.sqrt((err_core ** 2).mean())),
        "solver_residual": sol["residual"],
        "achieved_q_mlps": sol["q_m3s"] * 1e6,
    }


# --------------------------------------------------------------------------------------
# Injection parameters / inlet concentration
# --------------------------------------------------------------------------------------


def make_injection(volume_ml: float, rate_mlps: float, diffusivity: float,
                   q_blood_m3s: float, window_extra_s: float = 1.5,
                   window_max_s: float = 4.0) -> dict:
    """Bolus model: `volume_ml` mL of pure contrast injected at `rate_mlps` mL/s.

    Inlet concentration is the flow-diluted square bolus with smooth (tanh) edges:
        C_in(t) = C_pk * 0.5 * [tanh(t/eps) - tanh((t - T_bolus)/eps)]
        C_pk    = rate / (Q_blood + rate)      (volume fraction of pure contrast)
        T_bolus = volume / rate
    so the injected pure-contrast volume is `volume_ml` mL to O(eps).
    """
    t_bolus = volume_ml / rate_mlps
    q_b_mlps = q_blood_m3s * 1e6
    c_peak = rate_mlps / (q_b_mlps + rate_mlps)
    eps = 0.05 * t_bolus
    case_id = f"V{volume_ml:g}_Q{rate_mlps:g}_D{diffusivity:.0e}".replace("-", "")

    def c_in(t):
        t = np.asarray(t, dtype=np.float64)
        return c_peak * 0.5 * (np.tanh(t / eps) - np.tanh((t - t_bolus) / eps))

    return {
        "case_id": case_id,
        "volume_ml": volume_ml,
        "rate_mlps": rate_mlps,
        "diffusivity_m2s": diffusivity,
        "bolus_s": t_bolus,
        "c_peak": c_peak,
        "eps_s": eps,
        "window_s": min(t_bolus + window_extra_s, window_max_s),
        "q_total_mlps": q_b_mlps + rate_mlps,
        "c_in": c_in,
    }


def injection_grid(volumes_ml=(5.0, 10.0, 20.0), rates_mlps=(1.0, 2.5, 5.0),
                   diffusivities=(1e-9, 1e-7, 1e-6)) -> list[tuple]:
    return [(v, q, dd) for v in volumes_ml for q in rates_mlps for dd in diffusivities]


# --------------------------------------------------------------------------------------
# Scalar transport solve
# --------------------------------------------------------------------------------------


class TransportSolver:
    """Finite-volume scalar transport on the masked tubular domain.

    The spatial operator is assembled ONCE as sparse matrices over the fluid
    cells: F_adv = m(t) (P c + r C_in) and F_diff = G c + g C_in at faces, with
    dC/dt = -div(F_adv) + div(F_diff) -> dc = m(t) (L_adv c + s_adv C_in) +
    (L_diff c + s_diff C_in).  The cardiac modulation factor m(t) is a positive
    scalar, so upwind directions (and hence the sparsity) are time-invariant.
    Wall faces carry zero flux (mask), inlet ghosts are prescribed C_in(t),
    outlet/transverse out-of-domain donors fold to zero-gradient/first-order.
    Each time step is two sparse matvecs plus axpys -> CFL-controlled
    forward-Euler substepping is cheap enough for injection sweeps.
    """

    SKIP, GIN, GOUT = -1, -2, -3  # donor id markers (gin: C_in(t), gout: edge substitute)

    def __init__(self, dom: dict, u: dict, diffusivity: float, scheme: str = "upwind2"):
        self.dom = dom
        self.n = dom["n"]
        self.D = diffusivity
        self.scheme = scheme
        self.axes = ("x", "y", "z")
        self.axis_id = {"x": 0, "y": 1, "z": 2}
        vx, vy, vz = u["ux"], u["uy"], u["uz"]
        # transport x-faces f = 0..n (f between cell f-1 | f); faces 0 and n share
        # the streamwise-periodic velocity face (same Q in and out).
        self.uf = {
            "x": np.ascontiguousarray(np.concatenate([vx, vx[0:1]], axis=0)),
            "y": np.ascontiguousarray(vy),
            "z": np.ascontiguousarray(vz),
        }
        self.act = {"x": dom["tfx"], "y": dom["vfy"], "z": dom["vfz"]}
        self.h = {"x": dom["hx"], "y": dom["hy"], "z": dom["hz"]}
        self._assemble()
        self.c = np.zeros(dom["n_fluid"])
        self._tmp = np.zeros(dom["n_fluid"])
        self._fvec = np.zeros(dom["n_fluid"])
        # (assembly builds L_adv, L_diff, s_adv, s_diff and inlet/outlet flux rows)

    # -- assembly ---------------------------------------------------------
    def _donor_ids(self, a: str, slot: int) -> np.ndarray:
        """Unknown id per face for donor slot (0=CL,1=CLL,2=CR,3=CRR); markers for ghosts."""
        d = self.axis_id[a]
        n = self.n
        ids = np.full(self.uf[a].shape, self.SKIP, np.int64)
        idx_cells = self.dom["idx_cells"]
        donor_c = np.arange(n + 1) + np.array([-1, -2, 0, 1])[slot]
        for f, c in enumerate(donor_c):
            t = [slice(None)] * 3
            t[d] = f
            t = tuple(t)
            if a == "x":
                if c < 0:
                    ids[t] = self.GIN
                elif c >= n:  # zero-gradient outlet ghost -> substitute the edge cell
                    edge = [slice(None)] * 3
                    edge[d] = n - 1
                    ids[t] = idx_cells[tuple(edge)]
                else:
                    cell = [slice(None)] * 3
                    cell[d] = c
                    ids[t] = idx_cells[tuple(cell)]
            elif 0 <= c < n:
                cell = [slice(None)] * 3
                cell[d] = c
                ids[t] = idx_cells[tuple(cell)]
        return ids

    def _assemble(self):
        dom, n = self.dom, self.n
        nc = dom["n_fluid"]
        rowsP, colsP, valsP = [], [], []
        rowsG, colsG, valsG = [], [], []
        total_faces = sum(int(self.act[a].sum()) for a in self.axes)
        r_adv = np.zeros(total_faces)
        r_diff = np.zeros(total_faces)
        face_off = {}
        fcount = 0
        for a in self.axes:
            uf = self.uf[a]
            act = self.act[a]
            h = self.h[a]
            fid = np.full(uf.shape, -1, np.int64)
            fid[act] = np.arange(int(act.sum())) + fcount
            face_off[a] = fcount
            fcount += int(act.sum())
            if a == "x":
                self._x_fid = fid
            ok2p, ok2m = self.dom["donor"][a]
            v = np.where(act, uf, 0.0)

            # --- advective face interpolation coefficients (sign of uf fixed) ---
            if self.scheme == "central":
                coef = {0: 0.5 * v, 2: 0.5 * v, 1: np.zeros_like(v), 3: np.zeros_like(v)}
            else:
                wp = ok2p.astype(np.float64)
                wm = ok2m.astype(np.float64)
                pos = v > 0.0
                neg = v < 0.0
                coef = {
                    0: np.where(pos, v * (1.0 + 0.5 * wp), 0.0),  # CL
                    1: np.where(pos, -v * (0.5 * wp), 0.0),  # CLL
                    2: np.where(neg, v * (1.0 + 0.5 * wm), 0.0),  # CR
                    3: np.where(neg, -v * (0.5 * wm), 0.0),  # CRR
                }
            for slot, cf in coef.items():
                ids = self._donor_ids(a, slot)
                m_nonzero = cf != 0.0
                gid = ids[m_nonzero]
                cvs = cf[m_nonzero]
                f_sel = fid[m_nonzero]
                bad = (gid < 0) & (gid != self.GIN)
                if bad.any():
                    raise RuntimeError(f"nonzero coefficient with invalid donor ({a} slot {slot})")
                is_gin = gid == self.GIN
                if is_gin.any():
                    np.add.at(r_adv, f_sel[is_gin], cvs[is_gin])
                interior = gid >= 0
                rowsP.append(f_sel[interior])
                colsP.append(gid[interior])
                valsP.append(cvs[interior])

            # --- diffusive face gradient: K (C_R - C_L), K = D/h on active faces ---
            K = np.where(act, self.D / h, 0.0)
            for slot, sgn in ((0, -1.0), (2, +1.0)):
                ids = self._donor_ids(a, slot)
                sel = K != 0.0
                gid = ids[sel]
                cvs = sgn * K[sel]
                f_sel = fid[sel]
                bad = (gid < 0) & (gid != self.GIN)
                if bad.any():
                    raise RuntimeError(f"diffusion donor invalid ({a} slot {slot})")
                is_gin = gid == self.GIN
                if is_gin.any():
                    np.add.at(r_diff, f_sel[is_gin], cvs[is_gin])
                interior = gid >= 0
                rowsG.append(f_sel[interior])
                colsG.append(gid[interior])
                valsG.append(cvs[interior])

        # divergence Dt (cells x faces): +1/h at out-face, -1/h at in-face;
        # solid cells have no equation and inactive faces carry zero flux (absent)
        rowsD, colsD, valsD = [], [], []
        idx_cells = dom["idx_cells"]
        fluid = dom["M"]
        for a in self.axes:
            if a == "x":
                fid = self._x_fid
            else:
                fid = np.full(self.uf[a].shape, -1, np.int64)
                fid[self.act[a]] = np.arange(int(self.act[a].sum())) + face_off[a]
            h = self.h[a]
            d = self.axis_id[a]
            fin = [slice(None)] * 3
            fout = [slice(None)] * 3
            fin[d] = slice(0, n)
            fout[d] = slice(1, n + 1)
            fid_in = fid[tuple(fin)]
            fid_out = fid[tuple(fout)]
            sel_in = fluid & (fid_in >= 0)
            sel_out = fluid & (fid_out >= 0)
            rowsD.append(idx_cells[sel_in])
            colsD.append(fid_in[sel_in])
            valsD.append(np.full(int(sel_in.sum()), -1.0 / h))
            rowsD.append(idx_cells[sel_out])
            colsD.append(fid_out[sel_out])
            valsD.append(np.full(int(sel_out.sum()), 1.0 / h))

        Pm = sp.coo_matrix((np.concatenate(valsP), (np.concatenate(rowsP), np.concatenate(colsP))),
                           shape=(total_faces, nc)).tocsr()
        Gm = sp.coo_matrix((np.concatenate(valsG), (np.concatenate(rowsG), np.concatenate(colsG))),
                           shape=(total_faces, nc)).tocsr()
        Dt = sp.coo_matrix((np.concatenate(valsD), (np.concatenate(rowsD), np.concatenate(colsD))),
                           shape=(nc, total_faces)).tocsr()

        self.L_adv = (-Dt @ Pm).tocsr()
        self.L_diff = (Dt @ Gm).tocsr()
        self.s_adv = -(Dt @ r_adv)
        self.s_diff = Dt @ r_diff

        # inlet/outlet accounting: sum over the transport x-face rows (0 = inlet, n = outlet)
        fx = self._x_fid
        vin = np.zeros(total_faces)
        vout = np.zeros(total_faces)
        in_ids, out_ids = fx[0], fx[n]
        vin[in_ids[in_ids >= 0]] = 1.0
        vout[out_ids[out_ids >= 0]] = 1.0
        self._lin = Pm.T @ vin  # cell vector: inlet advective flux per unit C
        self._lout = Pm.T @ vout
        self._rin_s = float(vin @ r_adv)
        self._rout_s = float(vout @ r_adv)
        self._gin_row = Gm.T @ vin
        self._gout_row = Gm.T @ vout
        self._gin_r = float(vin @ r_diff)
        self._gout_r = float(vout @ r_diff)
        self.q_out_face0 = float(self.uf["x"][n][self.act["x"][n]].sum() * dom["hy"] * dom["hz"])

    # -- time-step bound ---------------------------------------------------
    def dt_bound(self, modulation_amp: float, cfl: float = 0.4) -> float:
        """dt = min(advection CFL bound, 0.5 x explicit-diffusion bound); |u| <= |u_bar|(1+a)."""
        s = 0.0
        for a in self.axes:
            s += float(np.abs(self.uf[a]).max()) * (1.0 + modulation_amp) / self.h[a]
        dt_adv = cfl / max(s, 1e-300)
        if self.D > 0.0:
            dt_diff = 0.5 / (2.0 * self.D * sum(1.0 / self.h[a] ** 2 for a in self.axes))
        else:
            dt_diff = math.inf
        return min(dt_adv, dt_diff)

    # -- stepping ----------------------------------------------------------
    def solve(self, injection: dict, modulation: dict, t_end: float,
              shared_dt: float | None = None, cfl: float = 0.4) -> dict:
        """March dC/dt + u.grad C = D lap C over [0, t_end]; record the outlet curve."""
        dt = shared_dt if shared_dt is not None else self.dt_bound(modulation["amp"], cfl)
        n_steps = max(1, int(math.ceil(t_end / dt)))
        dt = t_end / n_steps
        a_amp = modulation["amp"]
        omega = modulation["omega_rad_s"]
        c_in = injection["c_in"]
        self.c.fill(0.0)
        c = self.c
        dc = self._fvec
        tmp = self._tmp

        times = np.empty(n_steps + 1)
        c_out = np.empty(n_steps + 1)
        q_out = np.empty(n_steps + 1)
        adv_in = adv_out = diff_in = diff_out = 0.0
        q_o0 = self.q_out_face0
        area_x = self.dom["hy"] * self.dom["hz"]
        lin, lout = self._lin, self._lout
        gin_r, gout_r = self._gin_row, self._gout_row

        t0 = time.perf_counter()
        for it in range(n_steps + 1):
            t = it * dt
            m = 1.0 + a_amp * math.sin(omega * t)
            cin_v = float(c_in(t))
            # outlet curve: flow-weighted mean concentration at the outlet face
            # (F_adv_face = m * (P_row c + r_row C_in); m cancels in the ratio)
            f_out = float(lout @ c) + self._rout_s * cin_v
            f_in = float(lin @ c) + self._rin_s * cin_v
            times[it] = t
            q_out[it] = m * q_o0
            c_out[it] = f_out * area_x / max(q_o0, 1e-300)
            if it == n_steps:
                break
            np.multiply(self.L_adv @ c, m, out=dc)
            dc += self.L_diff @ c
            dc += (m * self.s_adv + self.s_diff) * cin_v
            c += dt * dc
            adv_in += m * f_in * area_x * dt
            adv_out += m * f_out * area_x * dt
            diff_in += (float(gin_r @ c) + self._gin_r * cin_v) * area_x * dt
            diff_out += (float(gout_r @ c) + self._gout_r * cin_v) * area_x * dt
        wall = time.perf_counter() - t0

        m_final = float(self.dom["cell_volume"] * self.c.sum())
        accounted = adv_in + diff_in - adv_out - diff_out
        denom = max(abs(adv_in) + abs(adv_out), 1e-300)
        mass_rel = abs(m_final - accounted) / denom

        out_valid = np.isfinite(c_out)
        peak = float(np.nanmax(c_out)) if out_valid.any() else 0.0
        t_peak = float(times[int(np.nanargmax(np.where(out_valid, c_out, -np.inf)))]) if out_valid.any() else float("nan")

        return {
            "times": times,
            "c_out": c_out,
            "q_out": q_out,
            "dt": dt,
            "n_steps": n_steps,
            "window_s": t_end,
            "wall_time_s": wall,
            "mass_balance_rel": float(mass_rel),
            "outlet_peak": peak,
            "outlet_t_peak_s": t_peak,
        }


def solve_transport(dom: dict, u: dict, injection: dict, modulation: dict,
                    scheme: str = "upwind2", shared_dt: float | None = None,
                    cfl: float = 0.4) -> dict:
    ts = TransportSolver(dom, u, injection["diffusivity_m2s"], scheme=scheme)
    return ts.solve(injection, modulation, injection["window_s"], shared_dt=shared_dt, cfl=cfl)


# --------------------------------------------------------------------------------------
# Velocity cache
# --------------------------------------------------------------------------------------


def save_velocity_cache(path: str, sol: dict, dom: dict, modulation: dict, extra: dict):
    np.savez_compressed(
        path,
        ux=sol["ux"], uy=sol["uy"], uz=sol["uz"], p=sol["p"],
        u_cell=velocity_cell_centred(sol),
    )
    meta = {
        "schema_version": 1,
        "tool": TOOL,
        "tool_version": VERSION,
        "command": " ".join(sys.argv),
        "created": _dt.datetime.now().isoformat(timespec="seconds"),
        "grid": dom["n"],
        "geometry": dom["geom"],
        "modulation": modulation,
        "mu_pas": MU,
        "rho_kgm3": RHO,
        "u_mean_target_ms": U_MEAN,
        "q_target_m3s": extra.get("q_target_m3s"),
        "q_achieved_m3s": sol["q_m3s"],
        "k_q_m3s_per_pam": sol["k_q"],
        "f_body_pam3": sol["f_body"],
        "solver_residual": sol["residual"],
        "cg_info": sol["cg_info"],
        "assemble_s": sol["assemble_s"],
        "factor_s": sol["factor_s"],
        "cg_s": sol["solve_s"],
        "note": "u(x,t) = u_face * (1 + amp*sin(omega*t)). Steady Stokes linearity: "
                "flow-rate changes rescale u exactly (verified in out/transport/report.json).",
    }
    with open(os.path.splitext(path)[0] + ".json", "w") as fh:
        json.dump(meta, fh, indent=2)
    return meta


def load_velocity_cache(path: str) -> tuple[dict, dict]:
    data = np.load(path)
    u = {"ux": data["ux"], "uy": data["uy"], "uz": data["uz"], "p": data["p"]}
    with open(os.path.splitext(path)[0] + ".json") as fh:
        meta = json.load(fh)
    return u, meta


# --------------------------------------------------------------------------------------
# CLI: decoupled sweep over the injection-parameter grid
# --------------------------------------------------------------------------------------


def _parse_floats(s: str) -> tuple:
    return tuple(float(v) for v in s.split(","))


def run_sweep(args) -> dict:
    geom = default_geometry()
    modulation = default_modulation(args.hr, args.amp)
    dom = build_domain(args.grid, geom)
    q_b = target_flow_m3s(geom)
    os.makedirs(args.out, exist_ok=True)
    cache_path = os.path.join(args.out, "u_cache.npz")

    print(f"[transport] steady velocity solve (grid {args.grid}, target Q = {q_b*1e6:.2f} mL/s)")
    sol = solve_steady_stokes(dom, q_b)
    print(f"[transport]   residual {sol['residual']:.2e}, Q = {sol['q_m3s']*1e6:.3f} mL/s, "
          f"{sol['assemble_s']:.1f}s asm + {sol['factor_s']:.1f}s lu + {sol['solve_s']:.1f}s cg")
    cache_meta = save_velocity_cache(cache_path, sol, dom, modulation, {"q_target_m3s": q_b})

    sweep_dir = os.path.join(args.out, "sweep")
    os.makedirs(sweep_dir, exist_ok=True)
    rows = []
    grid = injection_grid(_parse_floats(args.volumes), _parse_floats(args.rates),
                          _parse_floats(args.diffusivities))
    print(f"[transport] decoupled sweep: {len(grid)} injection variants")
    for vol, rate, diff in grid:
        inj = make_injection(vol, rate, diff, q_b, args.window_extra, args.window_max)
        res = solve_transport(dom, sol, inj, modulation, scheme=args.advection, cfl=args.cfl)
        np.savez_compressed(
            os.path.join(sweep_dir, inj["case_id"] + ".npz"),
            times=res["times"], c_out=res["c_out"], q_out=res["q_out"],
            c_in=inj["c_in"](res["times"]),
        )
        row = {
            "case_id": inj["case_id"],
            "volume_ml": vol,
            "rate_mlps": rate,
            "diffusivity_m2s": diff,
            "bolus_s": inj["bolus_s"],
            "window_s": res["window_s"],
            "dt_s": res["dt"],
            "n_steps": res["n_steps"],
            "wall_time_s": res["wall_time_s"],
            "mass_balance_rel": res["mass_balance_rel"],
            "outlet_peak": res["outlet_peak"],
            "outlet_t_peak_s": res["outlet_t_peak_s"],
        }
        rows.append(row)
        print(f"[transport]   {inj['case_id']:<22} {res['wall_time_s']:7.1f}s  "
              f"peak {res['outlet_peak']:.4f} @ {res['outlet_t_peak_s']:.3f}s  "
              f"mass-bal {res['mass_balance_rel']:.1e}")

    report = {
        "schema_version": 1,
        "tool": TOOL,
        "tool_version": VERSION,
        "command": " ".join(sys.argv),
        "created": _dt.datetime.now().isoformat(timespec="seconds"),
        "benchmark": "decoupled injection-parameter sweep (u cached, C re-solved per injection)",
        "equation": "dC/dt + u . grad(C) = D laplacian(C)",
        "settings": {
            "grid": args.grid,
            "geometry": geom,
            "modulation": modulation,
            "advection_scheme": args.advection,
            "cfl": args.cfl,
            "window_extra_s": args.window_extra,
            "window_max_s": args.window_max,
            "q_blood_mlps": q_b * 1e6,
            "injection_grid": {
                "volumes_ml": _parse_floats(args.volumes),
                "rates_mlps": _parse_floats(args.rates),
                "diffusivities_m2s": _parse_floats(args.diffusivities),
            },
        },
        "u_cache": os.path.relpath(cache_path),
        "u_cache_meta": cache_meta,
        "n_variants": len(rows),
        "t_total_s": float(sum(r["wall_time_s"] for r in rows)),
        "mass_balance_rel_max": float(max(r["mass_balance_rel"] for r in rows)),
        "variants": rows,
    }
    out_path = os.path.join(args.out, "sweep_report.json")
    with open(out_path, "w") as fh:
        json.dump(report, fh, indent=2)
    print(f"[transport] wrote {out_path}")
    return report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Decoupled steady-state transport: injection sweep")
    ap.add_argument("--grid", type=int, default=48, help="cells per axis (benchmark range 48-64)")
    ap.add_argument("--hr", type=float, default=72.0, help="heart rate [bpm] (60-80)")
    ap.add_argument("--amp", type=float, default=0.2, help="cardiac velocity modulation amplitude a")
    ap.add_argument("--volumes", default="5,10,20", help="bolus volumes [mL]")
    ap.add_argument("--rates", default="1,2.5,5", help="injection rates [mL/s]")
    ap.add_argument("--diffusivities", default="1e-9,1e-7,1e-6", help="contrast diffusivity [m^2/s]")
    ap.add_argument("--window-extra", type=float, default=1.5, help="curve window = bolus + extra [s]")
    ap.add_argument("--window-max", type=float, default=4.0, help="max curve window [s]")
    ap.add_argument("--cfl", type=float, default=0.4)
    ap.add_argument("--advection", choices=("upwind2", "central"), default="upwind2")
    ap.add_argument("--out", default="out/transport")
    ap.add_argument("--validate", action="store_true", help="run Poiseuille validation first")
    args = ap.parse_args(argv)
    if args.validate:
        v = validate_poiseuille(min(args.grid, 32))
        print(f"[transport] Poiseuille validation: max rel err (core) {v['max_rel_err_core']:.3%}, "
              f"rms {v['rms_rel_err_core']:.3%}")
    run_sweep(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
