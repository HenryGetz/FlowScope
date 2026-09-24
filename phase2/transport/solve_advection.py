#!/usr/bin/env python3
"""C4 — 3D contrast transport on the carrier lattice (conservative FV, TVD/Superbee).

Solves the 3D scalar transport equation

    dC/dt + alpha(t) u_b . grad C = D laplacian C

for the dimensionless contrast fraction C(x, t) in [0, 1] on the active-cell carrier
lattice from ``out/transport/carrier/{case}_carrier.npz`` (contract C3), driven by the
inlet signal ``signals.<profile>.alpha/c_in`` of
``out/rom/hemodynamics/{case}/hemodynamics.json`` (contract C2), and writes
``out/transport/contrast/{case}_{profile}.npz`` + ``.json`` (contract C4).

Geometry (contract ruling)
--------------------------
``origin_mm`` is the lattice CORNER: cell (i, j, k) has its center at
``origin_mm + (i+0.5, j+0.5, k+0.5) * spacing_mm`` (matching the carrier ``xyz``),
and every lattice face is the plane halfway between two cell centers with area
``spacing_mm**2``.  ``cell_ravel``, ``grid_shape``, ``origin_mm``, ``spacing_mm``
are copied verbatim into the C4 npz.

Numerics
--------
Conservative finite-volume flux form on the carrier lattice (face topology rebuilt
from ``cell_ravel`` + ``grid_shape``, C-order ravel): MUSCL reconstruction with the
Superbee TVD limiter for the advective face flux from cell-centred ``u_b``
interpolated to faces, central face diffusion (D = 1e-3 mm^2/s), zero normal flux
across mask faces (no-slip walls).  Per contract C3 the open cross-sections of
``is_inlet`` / ``is_outlet`` cells are *not* lattice faces and carry exactly the
cell's interior-face flux imbalance (the ``u_b`` cell values carry the carrier
reconstruction's alternating nullspace mode and are not one-sided face
velocities, so the open-face flux is the cell imbalance — never a geometric
``u_b . n`` estimate).  Inlet open faces advect alpha-scaled ``c_in(t)`` in;
outlet open faces advect the cell's upwind value out; any residual interior
imbalance (f32 round-off in ``u_b``) is carried by the matched conservative
flux ``r_i C_i`` (upwind value = the cell's own value, valid for either sign of
``r_i``), so every cell is exactly divergence-free in the combined flux set and
the C in [0, 1] maximum principle holds.  Update and budget share the *same*
face fluxes, open faces included, and there are no volumetric source/sink terms,
so the C-mass budget

    M(t) = int C dV == int_0^t (Q_in C_in - Q_out C_out) d tau

closes to f8 round-off (``mass.eps_mass_max`` REQUIRED <= 1e-4; hard-fail above it).
Boundedness comes from the TVD limiter + a convex (combined advective/diffusive)
step bound — no clipping of the running state, hence no clipping-driven mass leaks.

Explicit adaptive time stepping (SSP-RK2 / Heun, second-order TVD-preserving) with
the combined CFL number (advection + diffusion) <= CFL_TARGET (spec bound 0.9).
Steps never cross keyframe times and the K = ``--frames`` keyframes are the uniform
grid over the ``signals.<profile>.t_s`` span, so keyframes are independent of the
adaptive step count.
"""

from __future__ import annotations

import os

# Machine spec: pin BLAS/OpenMP threading before numpy loads (overrides older pinning).
os.environ["OMP_NUM_THREADS"] = "8"
os.environ["MKL_NUM_THREADS"] = "8"
os.environ["OPENBLAS_NUM_THREADS"] = "8"

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

TOOL = {"name": "phase2/transport/solve_advection.py", "version": "0.1.0"}
SCHEMA = "flowscope.transport.contrast"
SCHEMA_VERSION = 1

D_MM2_S = 1e-3  # contrast diffusivity, mm^2/s
CFL_TARGET = 0.45  # adaptive combined CFL ceiling (spec bound: CFL <= 0.9)
CFL_LIMIT = 0.9
DT_CAP_S = 2.0e-2  # max step (signal/budget quadrature resolution), e.g. alpha ~ 0 windows
EPS_MASS_TOL = 1e-4
MONO_TOL = 1e-9  # allowed round-off excursion outside [0, 1]; larger => hard fail
RATIO_TOL = 1e-30  # |C_R - C_L| below this: limiter slope is zero (denominator guard)
MM3_PER_ML_INV = 1.0e-3  # mm^3 -> mL (one consistent volume unit in the JSON budget)

CARRIER_KEYS = (
    "grid_shape",
    "origin_mm",
    "spacing_mm",
    "cell_ravel",
    "u_b",
    "is_inlet",
    "is_outlet",
    "vol_mm3",
)


# ------------------------------------------------------------------------------------ #
# Inputs
# ------------------------------------------------------------------------------------ #


def load_carrier(path: Path) -> dict:
    """Load + validate the C3 carrier npz (arrays only; topology is rebuilt below)."""
    if not path.is_file():
        raise FileNotFoundError(
            f"carrier not found: {path} (run phase2/transport/build_carrier.py first)"
        )
    with np.load(path) as z:
        missing = [k for k in CARRIER_KEYS if k not in z.files]
        if missing:
            raise ValueError(f"{path}: carrier npz missing keys {missing} (contract C3)")
        keys = list(CARRIER_KEYS) + (["xyz"] if "xyz" in z.files else [])
        car = {k: z[k] for k in keys}

    grid_shape = np.asarray(car["grid_shape"], dtype=np.int64).reshape(3)
    if np.any(grid_shape < 1):
        raise ValueError(f"{path}: grid_shape must be positive, got {grid_shape.tolist()}")
    ravel = np.asarray(car["cell_ravel"], dtype=np.int64).reshape(-1)
    n_a = int(ravel.size)
    if n_a == 0:
        raise ValueError(f"{path}: carrier has no active cells (cell_ravel is empty)")
    n_cell = int(np.prod(grid_shape))
    if int(ravel.min()) < 0 or int(ravel.max()) >= n_cell:
        raise ValueError(f"{path}: cell_ravel entries outside [0, {n_cell})")
    if np.unique(ravel).size != n_a:
        raise ValueError(f"{path}: cell_ravel contains duplicate ravel indices")

    expect = {"u_b": (n_a, 3), "vol_mm3": (n_a,), "is_inlet": (n_a,), "is_outlet": (n_a,)}
    for key, shape in expect.items():
        if np.asarray(car[key]).shape != shape:
            raise ValueError(
                f"{path}: {key} has shape {np.asarray(car[key]).shape}, expected {shape}"
            )
    if np.asarray(car["origin_mm"]).shape != (3,):
        raise ValueError(f"{path}: origin_mm must have shape (3,)")

    u_b = np.asarray(car["u_b"], dtype=np.float64)
    vol = np.asarray(car["vol_mm3"], dtype=np.float64)
    if not np.all(np.isfinite(u_b)):
        raise ValueError(f"{path}: u_b contains non-finite entries")
    if not np.all(np.isfinite(vol)) or not np.all(vol > 0.0):
        n_bad = int(np.sum(~(np.isfinite(vol) & (vol > 0.0))))
        raise ValueError(f"{path}: vol_mm3 must be finite and strictly positive ({n_bad} bad cells)")

    is_in = np.asarray(car["is_inlet"]).astype(bool)
    is_out = np.asarray(car["is_outlet"]).astype(bool)
    if not is_in.any():
        raise ValueError(
            f"{path}: empty inlet mask (is_inlet has no active cells) — cannot inject contrast"
        )
    if not is_out.any():
        raise ValueError(
            f"{path}: empty outlet mask (is_outlet has no active cells) — no outflow boundary"
        )

    out = dict(car)  # verbatim arrays (original dtype/values) for the C4 npz copies
    out["grid_shape_i64"] = grid_shape  # normalized working views
    out["cell_ravel_i64"] = ravel
    out["u_b"] = u_b
    out["vol_mm3"] = vol
    out["is_inlet"] = is_in
    out["is_outlet"] = is_out

    if "xyz" in car:
        xyz = np.asarray(car["xyz"], dtype=np.float64)
        if xyz.shape != (n_a, 3):
            raise ValueError(f"{path}: xyz has shape {xyz.shape}, expected {(n_a, 3)}")
        # Contract ruling: origin_mm is the lattice corner; cell centers sit at
        # origin + (ijk + 0.5) * spacing. Anything else corrupts downstream sampling.
        origin = np.asarray(car["origin_mm"], dtype=np.float64)
        spacing = float(np.asarray(car["spacing_mm"], dtype=np.float64))
        ijk = np.column_stack(np.unravel_index(ravel, grid_shape))
        centers = origin[None, :] + (ijk + 0.5) * spacing
        err = float(np.abs(xyz - centers).max())
        if err > 1e-3:
            raise ValueError(
                f"{path}: xyz deviates from corner-based cell centers "
                f"origin_mm + (ijk+0.5)*spacing_mm by {err:.3e} mm (contract ruling)"
            )
    return out


def load_hemo(path: Path, case: str) -> dict:
    if not path.is_file():
        raise FileNotFoundError(
            f"hemodynamics not found: {path} (run phase2/rom/run_hemodynamics.py first)"
        )
    hemo = json.loads(path.read_text())
    if "signals" not in hemo or not isinstance(hemo["signals"], dict):
        raise ValueError(f"{path}: missing 'signals' block (contract C2)")
    hcase = hemo.get("case")
    if hcase is not None and str(hcase) != str(case):
        raise ValueError(f"{path}: hemodynamics case {hcase!r} does not match requested {case!r}")
    return hemo


def extract_signal(hemo: dict, profile: str, path: Path):
    signals = hemo["signals"]
    if profile not in signals:
        raise ValueError(
            f"{path}: profile {profile!r} not in signals {sorted(signals)} "
            f"(requested profiles must exist in hemodynamics.json)"
        )
    sig = signals[profile]
    missing = [k for k in ("t_s", "alpha", "c_in") if k not in sig]
    if missing:
        raise ValueError(f"{path}: signals.{profile} missing keys {missing}")
    t = np.asarray(sig["t_s"], dtype=np.float64).reshape(-1)
    alpha = np.asarray(sig["alpha"], dtype=np.float64).reshape(-1)
    c_in = np.asarray(sig["c_in"], dtype=np.float64).reshape(-1)
    if t.size < 2 or not (t.size == alpha.size == c_in.size):
        raise ValueError(
            f"{path}: signals.{profile} t_s/alpha/c_in must share one length >= 2 "
            f"(got {t.size}/{alpha.size}/{c_in.size})"
        )
    if not np.all(np.isfinite(t)) or not np.all(np.diff(t) > 0.0):
        raise ValueError(f"{path}: signals.{profile}.t_s must be strictly increasing")
    if not np.all(np.isfinite(alpha)) or not np.all(np.isfinite(c_in)):
        raise ValueError(f"{path}: signals.{profile} alpha/c_in must be finite")
    segs = (hemo.get("profiles") or {}).get(profile, {}).get("segments") or []
    injectate_mL = sum(float(s["q_mLs"]) * float(s["t_s"]) for s in segs)
    return Signal(t, alpha, c_in, injectate_mL)


class Signal:
    """Linear interpolation of alpha(t), c_in(t) on the hemodynamics t_s grid."""

    def __init__(self, t: np.ndarray, alpha: np.ndarray, c_in: np.ndarray,
                 injectate_mL: float = 0.0):
        self.t = t
        self.alpha = alpha
        self.c_in = c_in
        self.injectate_mL = injectate_mL  # gross injectate volume, mL (C2 segments)

    @property
    def t_span(self) -> tuple[float, float]:
        return float(self.t[0]), float(self.t[-1])

    def alpha_at(self, t: float) -> float:
        return float(np.interp(t, self.t, self.alpha))

    def cin_at(self, t: float) -> float:
        return float(np.interp(t, self.t, self.c_in))

    def alpha_hi(self, t: float, dt: float) -> float:
        """Upper bound of alpha over [t, t+dt] (signal grid samples + endpoints)."""
        hi_t = min(t + dt, float(self.t[-1]))
        i0 = int(np.searchsorted(self.t, t, side="left"))
        i1 = int(np.searchsorted(self.t, hi_t, side="right"))
        m = max(self.alpha_at(t), self.alpha_at(hi_t))
        if i1 > i0:
            m = max(m, float(self.alpha[i0:i1].max()))
        return m


# ------------------------------------------------------------------------------------ #
# Face topology on the carrier lattice
# ------------------------------------------------------------------------------------ #


def build_topology(car: dict) -> dict:
    """Rebuild the FV face topology from cell_ravel + grid_shape (C-order ravel).

    Internal faces: (fl, fr) active-cell pairs along each axis, face velocity =
    0.5 * (u_b[fl] + u_b[fr]) component along the axis (the C3 face-flux
    convention).  Faces with one active side are no-flux walls (no-slip).  The
    open boundary (contract C3) is not a lattice face: each is_inlet / is_outlet
    cell's open cross-section carries exactly the cell's interior-face flux
    imbalance r = div_int ("the sum of the interior face fluxes of an inlet cell
    equals the inflow through its open face"), because u_b cell values carry the
    carrier reconstruction's alternating nullspace mode and must not be read as
    one-sided face velocities.  Inlet open faces (r > 0 at is_inlet cells) carry
    r at c_in(t); outlet open faces (r < 0 at is_outlet cells) carry -r at the
    cell value; any remaining imbalance (f32 round-off at interior cells, or
    flag/sign mismatches) is carried by the matched conservative term r_i * C_i.
    """
    grid = tuple(int(v) for v in car["grid_shape_i64"])
    ravel = car["cell_ravel_i64"]
    n_a = int(ravel.size)
    u_b = car["u_b"]
    is_in = car["is_inlet"]
    is_out = car["is_outlet"]
    h = float(np.asarray(car["spacing_mm"], dtype=np.float64))
    if not h > 0.0:
        raise ValueError(f"spacing_mm must be positive, got {h}")

    cell_of = np.full(int(np.prod(grid)), -1, dtype=np.int64)
    cell_of[ravel] = np.arange(n_a)
    lattice = cell_of.reshape(grid)  # C-order ravel semantics
    ijk = np.column_stack(np.unravel_index(ravel, grid))  # (n_a, 3)
    area = h * h

    axes = []
    g_acc = np.zeros(n_a)  # per-cell sum of |face flux| over interior faces
    h_acc = np.zeros(n_a)  # per-cell sum of A / h over interior (diffusive) faces
    div_int = np.zeros(n_a)  # net interior-face outflux per cell, mm^3/s at alpha = 1

    for d in range(3):
        prev = np.full(n_a, -1, dtype=np.int64)
        ok = ijk[:, d] > 0
        q = ijk[ok].copy()
        q[:, d] -= 1
        prev[ok] = lattice[tuple(q.T)]
        nxt = np.full(n_a, -1, dtype=np.int64)
        ok = ijk[:, d] < grid[d] - 1
        q = ijk[ok].copy()
        q[:, d] += 1
        nxt[ok] = lattice[tuple(q.T)]

        has = nxt >= 0
        fl = np.arange(n_a, dtype=np.int64)[has]
        fr = nxt[has]
        uf = 0.5 * (u_b[fl, d] + u_b[fr, d])  # face-normal velocity, positive L -> R
        pos = uf > 0.0
        far = np.where(pos, prev[fl], nxt[fr])
        far_ok = far >= 0
        axes.append(
            {
                "fl": fl,
                "fr": fr,
                "uf": uf,
                "uc": np.where(pos, fl, fr),  # upwind cell
                "far": np.where(far_ok, far, 0),  # far (2nd) upwind cell, 0 placeholder
                "far_ok": far_ok,
                "pos": pos,
                "sgn": np.where(pos, 1.0, -1.0),  # reconstruction side
                "invVl": 1.0 / car["vol_mm3"][fl],
                "invVr": 1.0 / car["vol_mm3"][fr],
            }
        )

        af = area * np.abs(uf)
        hf = np.full(fl.size, area / h)
        sf = area * uf  # outflux from L across the face (positive L -> R)
        g_acc += np.bincount(fl, weights=af, minlength=n_a)
        g_acc += np.bincount(fr, weights=af, minlength=n_a)
        h_acc += np.bincount(fl, weights=hf, minlength=n_a)
        h_acc += np.bincount(fr, weights=hf, minlength=n_a)
        div_int += np.bincount(fl, weights=sf, minlength=n_a)
        div_int -= np.bincount(fr, weights=sf, minlength=n_a)

    # ---- open boundary = the carrier's per-cell imbalance (C3 ruling) --------
    # The open cross-sections of is_inlet / is_outlet cells are not lattice
    # faces: build_carrier assigns every open cell its exact interior-face flux
    # imbalance to its open face ("the sum of the interior face fluxes of an
    # inlet cell equals the inflow through its open face") and reconstructs u_b
    # so that only the pairwise face averages 0.5*(u_L + u_R) are meaningful
    # (per-cell values carry an arbitrary alternating mode).  The open-face flux
    # is therefore the imbalance r = div_int, never a geometric u_b . n estimate.
    r = div_int  # net interior-face outflux per cell, mm^3/s at alpha = 1
    in_m = is_in & (r > 0.0)  # inlet cells: open inflow face carries r at c_in
    out_m = is_out & (r < 0.0)  # outlet cells: open outflow face carries -r at C_i
    in_cells = np.flatnonzero(in_m)
    out_cells = np.flatnonzero(out_m)
    in_q = r[in_cells]  # open inflow capacity, mm^3/s at alpha = 1
    out_q = -r[out_cells]  # open outflow capacity, mm^3/s at alpha = 1
    if in_cells.size == 0 or out_cells.size == 0:
        raise ValueError(
            "carrier open-cell imbalances contradict the inlet/outlet flags "
            f"(is_inlet cells with positive imbalance: {int(in_m.sum())}/{int(is_in.sum())}, "
            f"is_outlet cells with negative imbalance: {int(out_m.sum())}/{int(is_out.sum())})"
        )
    ain = float(in_q.sum())
    aout = float(out_q.sum())
    if not (ain > 0.0 and aout > 0.0):
        raise ValueError(
            f"non-positive open-face flux capacity (A_in={ain}, A_out={aout} mm^3/s)"
        )

    # Every cell not covered by the flag rule (f32 round-off imbalance at
    # interior cells, flag/sign mismatches) keeps the matched conservative flux
    # r_i * C_i (upwind value = the cell's own value), which preserves C in
    # [0, 1] for either sign of r_i (see TransportModel.rhs).
    covered = np.zeros(n_a, dtype=bool)
    covered[in_cells] = True
    covered[out_cells] = True
    res_cells = np.flatnonzero(~covered)
    res_r = r[res_cells]
    residual_rel_max = (
        float(np.max(np.abs(res_r) / np.maximum(g_acc[res_cells], 1e-30)))
        if res_r.size else 0.0
    )

    # positivity cap per cell: (sum |face flux| + |imbalance|) / V
    g_acc += np.abs(r)
    open_cells = np.union1d(in_cells, out_cells)
    both_flags = int((is_in & is_out).sum())
    sign_mismatch = int((~covered & (is_in | is_out)).sum())
    div_num = np.zeros(n_a)  # unmatched divergence after the open-face rule
    div_num[res_cells] = res_r

    return {
        "axes": axes,
        "in_cells": in_cells,
        "in_q": in_q,
        "out_cells": out_cells,
        "out_q": out_q,
        "res_cells": res_cells,
        "res_r": res_r,
        "area": area,
        "spacing_mm": h,
        "g_acc": g_acc,
        "h_acc": h_acc,
        "div_num": div_num,
        "open_balance_max_rel": 0.0,  # open-face flux := cell imbalance: exact by construction
        "residual_rel_max": residual_rel_max,
        "n_open_cells": int(open_cells.size),
        "n_both_flags": both_flags,
        "n_sign_mismatch": sign_mismatch,
    }


# ------------------------------------------------------------------------------------ #
# Semi-discrete operator (conservative FV fluxes) + SSP-RK2 stepping
# ------------------------------------------------------------------------------------ #


class TransportModel:
    """Conservative FV operator for dC/dt = L(C, t) on the active-cell lattice."""

    def __init__(self, car: dict, topo: dict):
        self.n_a = int(car["vol_mm3"].size)
        self.V = car["vol_mm3"]
        self._h = topo["spacing_mm"]
        self._area = topo["area"]
        self.axes = topo["axes"]
        self.in_cells = topo["in_cells"]
        self.in_q = topo["in_q"]  # open inflow capacity, mm^3/s at alpha = 1
        self.out_cells = topo["out_cells"]
        self.out_q = topo["out_q"]  # open outflow capacity, mm^3/s at alpha = 1
        self.res_cells = topo["res_cells"]
        self.res_r = topo["res_r"]  # matched residual imbalance, signed
        self.Ain = float(topo["in_q"].sum())
        rp = self.res_r > 0.0
        self.res_pos_cells, self.res_pos_r = self.res_cells[rp], self.res_r[rp]
        self.res_neg_cells, self.res_neg_r = self.res_cells[~rp], -self.res_r[~rp]

        # static scatter map for the per-stage RHS accumulation
        parts = [np.concatenate([ax["fl"], ax["fr"]]) for ax in self.axes]
        parts += [self.in_cells, self.out_cells, self.res_cells]
        self.slot_idx = np.concatenate(parts)

        self.gv = topo["g_acc"] / self.V  # (sum |F_face| + |imbalance|) / V
        self.dh = D_MM2_S * topo["h_acc"] / self.V  # diffusion rate coefficient
        self.div_num = topo["div_num"]

    def step_dt(self, alpha_hi: float) -> float:
        """Combined advective + diffusive positivity/stability bound (CFL_TARGET-scaled)."""
        rate = float(np.max(alpha_hi * self.gv + self.dh))
        return CFL_TARGET / rate if rate > 0.0 else DT_CAP_S

    def rhs(self, C: np.ndarray, alpha: float, cin: float):
        """L(C, t) plus the boundary mass rates (B_in, B_out) used by that same update.

        Superbee-TVD (MUSCL) upwind advective face fluxes + central diffusion;
        zero flux on walls.  The open faces are the carrier's per-cell imbalances
        (contract C3): inlet open faces carry alpha*cin at capacity in_q, outlet
        open faces the cell's own value at capacity out_q, and any residual
        imbalance r_i is carried by the matched flux alpha*r_i*C_i (upwind value
        = the cell's own value; at a global extremum the reconstruction collapses
        to the cell value and the combined flux set is exactly divergence-free,
        so the C in [0, 1] maximum principle holds for either sign of r_i).
        B_in/B_out are computed from these exact face fluxes, so any time
        integrator built on rhs() conserves sum(V*C) against them to round-off.
        """
        weights = []
        for ax in self.axes:
            fl, fr = ax["fl"], ax["fr"]
            den = C[fr] - C[fl]
            uc = C[ax["uc"]]
            farv = C[ax["far"]]
            num = np.where(ax["pos"], uc - farv, farv - uc)
            num = np.where(ax["far_ok"], num, 0.0)
            small = np.abs(den) < RATIO_TOL
            den_s = np.where(small, 1.0, den)
            r = num / den_s
            # Superbee: phi(r) = max(0, min(2r, 1), min(r, 2)); phi <= 2 keeps the
            # face value inside [min(C_L, C_R), max(C_L, C_R)] (TVD, no clipping).
            phi = np.maximum(0.0, np.maximum(np.minimum(2.0 * r, 1.0), np.minimum(r, 2.0)))
            phi = np.where(ax["far_ok"] & ~small, phi, 0.0)
            face_c = uc + ax["sgn"] * (0.5 * phi * den)  # upwind-cell reconstruction
            F = alpha * ax["uf"] * self._area * face_c - D_MM2_S * self._area * den / self._h
            w = np.empty(2 * fl.size)
            w[: fl.size] = -F * ax["invVl"]
            w[fl.size :] = F * ax["invVr"]
            weights.append(w)

        b_in = alpha * (
            cin * self.Ain
            + (float(np.dot(self.res_pos_r, C[self.res_pos_cells]))
               if self.res_pos_r.size else 0.0)
        )
        b_out = alpha * (
            float(np.dot(self.out_q, C[self.out_cells]))
            + (float(np.dot(self.res_neg_r, C[self.res_neg_cells]))
               if self.res_neg_r.size else 0.0)
        )
        weights.append(alpha * cin * self.in_q / self.V[self.in_cells])
        weights.append(-alpha * self.out_q * C[self.out_cells] / self.V[self.out_cells])
        weights.append(alpha * self.res_r * C[self.res_cells] / self.V[self.res_cells])
        acc = np.bincount(self.slot_idx, weights=np.concatenate(weights), minlength=self.n_a)
        return acc, b_in, b_out

    @property
    def div_residual(self) -> float:
        return float(np.max(np.abs(self.div_num)))


def solve_profile(model: TransportModel, sig: Signal, frames: int) -> dict:
    """Integrate one injection profile; return keyframe states + budget bookkeeping."""
    t0, t1 = sig.t_span
    kf = np.linspace(t0, t1, frames)
    n_a = model.n_a

    C = np.zeros(n_a)
    Cf4 = np.empty((frames, n_a), dtype=np.float32)
    rows = []  # (t, M, S, N) at each keyframe
    S = 0.0  # cumulative int (Q_in C_in - Q_out C_out) d tau
    N = 0.0  # cumulative int Q_in C_in d tau

    n_steps = 0
    dt_sum = 0.0
    dt_min = float("inf")
    dt_max = 0.0
    cfl_max = 0.0
    cfl_adv_max = 0.0
    cfl_diff_max = 0.0
    c_min = 0.0
    c_max = 0.0

    t = t0
    for k in range(frames):
        if k > 0:
            tb = float(kf[k])
            while t < tb:
                a_hi = sig.alpha_hi(t, DT_CAP_S)
                dt_lim = min(model.step_dt(a_hi), DT_CAP_S)
                dt = min(dt_lim, tb - t)
                last = (tb - t) <= dt_lim

                a1 = sig.alpha_at(t)
                cin1 = sig.cin_at(t)
                k1, b1i, b1o = model.rhs(C, a1, cin1)
                t2 = t + dt
                a2 = sig.alpha_at(t2)
                cin2 = sig.cin_at(t2)
                k2, b2i, b2o = model.rhs(C + dt * k1, a2, cin2)

                # SSP-RK2 (Heun): budget uses the same stage fluxes as the update.
                C = C + (0.5 * dt) * (k1 + k2)
                S += (0.5 * dt) * ((b1i - b1o) + (b2i - b2o))
                N += (0.5 * dt) * (b1i + b2i)

                a_step = max(a1, a2)
                adv = float(np.max(a_step * model.gv))
                dif = float(np.max(model.dh))
                cfl_adv_max = max(cfl_adv_max, dt * adv)
                cfl_diff_max = max(cfl_diff_max, dt * dif)
                cfl_max = max(cfl_max, dt * (adv + dif))
                c_min = min(c_min, float(C.min()))
                c_max = max(c_max, float(C.max()))

                n_steps += 1
                dt_sum += dt
                dt_min = min(dt_min, dt)
                dt_max = max(dt_max, dt)
                t = tb if last else t + dt

        rows.append((float(kf[k]), float(np.dot(model.V, C)), S, N))
        # Output-range guard: stores f8 state quantized to f4 within [0, 1]. The mass
        # budget is accounted on the unclipped f8 state above; excursions beyond
        # MONO_TOL hard-fail in run_profile, so this only trims round-off dust
        # (|delta| <= 1e-9, far below f4 resolution) and cannot leak mass.
        np.clip(C, 0.0, 1.0, out=Cf4[k], casting="unsafe")

    n_final = rows[-1][3]
    n_cut = 1e-12 * n_final
    series = []
    for (t_k, m_k, s_k, n_k) in rows:
        if n_k > n_cut:
            series.append((t_k, abs(m_k - s_k) / n_k))
        else:  # no measurable contrast has entered yet; budget is exact at round-off
            series.append((t_k, 0.0))
    eps_max = max(e for _, e in series)

    return {
        "kf": kf,
        "C": Cf4,
        "series": series,
        "rows": rows,
        "eps_mass_max": eps_max,
        "budget": {
            "int_C_dV": rows[-1][1],
            "int_boundary_flux": rows[-1][2],
            "rel": series[-1][1],
        },
        "n_steps": n_steps,
        "dt_s_mean": (t1 - t0) / n_steps if n_steps else 0.0,
        "dt_s_min": dt_min if n_steps else 0.0,
        "dt_s_max": dt_max,
        "cfl": cfl_max,
        "cfl_adv": cfl_adv_max,
        "cfl_diff": cfl_diff_max,
        "c_min": c_min,
        "c_max": c_max,
        "int_q_in_c_in": n_final,
    }


# ------------------------------------------------------------------------------------ #
# Per (case, profile) driver
# ------------------------------------------------------------------------------------ #


def envelope(command: str, runtime_s: float) -> dict:
    return {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "tool": {
            **TOOL,
            "command": command,
            "generated": datetime.now(timezone.utc).isoformat(),
            "runtime_s": runtime_s,
        },
    }


def run_profile(case: str, profile: str, car: dict, topo: dict, sig: Signal,
                frames: int, out_dir: Path, command: str) -> dict:
    t_start = time.perf_counter()
    model = TransportModel(car, topo)

    u_face_max = max(
        [float(np.abs(ax["uf"]).max()) for ax in topo["axes"]]
        + [float(topo["in_q"].max() / topo["area"]),
           float(topo["out_q"].max() / topo["area"])]
    )
    alpha_peak = float(sig.alpha.max())
    t0, t1 = sig.t_span

    res = solve_profile(model, sig, frames)
    runtime_s = time.perf_counter() - t_start

    ok_eps = res["eps_mass_max"] <= EPS_MASS_TOL
    ok_cfl = res["cfl"] <= CFL_LIMIT
    ok_mono = (res["c_min"] >= -MONO_TOL) and (res["c_max"] <= 1.0 + MONO_TOL)
    verdict = "pass" if (ok_eps and ok_cfl and ok_mono) else "fail"

    q_in_c_in_mL = res["int_q_in_c_in"] * MM3_PER_ML_INV
    sig_check_mL = (
        float(np.trapezoid(sig.alpha * sig.c_in, sig.t)) * model.Ain * MM3_PER_ML_INV
    )

    payload = {
        **envelope(command, runtime_s),
        "case": str(case),
        "profile": profile,
        "frames": int(frames),
        "dt_s_mean": res["dt_s_mean"],
        "pe": alpha_peak * u_face_max * topo["spacing_mm"] / D_MM2_S,
        "cfl": res["cfl"],
        "D_mm2_s": D_MM2_S,
        "mass": {
            "eps_mass_series": [[t_k, e] for (t_k, e) in res["series"]],
            "eps_mass_max": res["eps_mass_max"],
            "budget": {
                "int_C_dV": res["budget"]["int_C_dV"] * MM3_PER_ML_INV,
                "int_boundary_flux": res["budget"]["int_boundary_flux"] * MM3_PER_ML_INV,
                "rel": res["budget"]["rel"],
                "int_q_in_c_in": q_in_c_in_mL,
                "int_injectate_dV": sig.injectate_mL,
                "note": (
                    "one consistent volume unit everywhere in mass.budget: volumes in "
                    "mL, flow-time integrals in mL-fraction (Q in mL/s, C dimensionless). "
                    "eps_mass = |int_C_dV - int_boundary_flux| / int_q_in_c_in with "
                    "int_C_dV = int C dV at the final frame, int_boundary_flux = "
                    "int (Q_in C_in - Q_out C_out) d tau, int_q_in_c_in = "
                    "int Q_in C_in d tau; update and budget use identical face fluxes "
                    "(open faces included), so int_C_dV == int_boundary_flux to round-off. "
                    "int_q_in_c_in matches the independent hemodynamics signal integral "
                    "int q_root_mLs * c_in d t (numerics.int_q_in_c_in_signal_mL). "
                    "int_injectate_dV = sum(q_mLs * t_s) over the C2 injectate segments "
                    "(gross injectate volume 8.0 / 5.25 / 12.0 mL for A / B / C); for "
                    "profile C it counts the saline flush at c_in = 0, so the "
                    "contrast-weighted int_q_in_c_in = 4.0*1.5*1 = 6 mL-fraction."
                ),
            },
        },
        "runtime_s": runtime_s,
        "verdict": verdict,
        "numerics": {
            "scheme": (
                "conservative FV flux form, MUSCL Superbee TVD upwind advection + "
                "central diffusion, SSP-RK2 (Heun), no-flux walls; open faces = the "
                "carrier's per-cell interior-face imbalance (C3): inlet open faces "
                "advect alpha(t)*c_in(t), outlet open faces the cell's upwind value, "
                "residual interior imbalance carried by the matched flux r_i*C_i; "
                "identical face fluxes in update + budget, no volumetric sources"
            ),
            "open_face_model": (
                "open-face flux = cell interior-face flux imbalance (build_carrier "
                "C3 ruling: 'the sum of the interior face fluxes of an inlet cell "
                "equals the inflow through its open face'); never recomputed "
                "geometrically from u_b . n (u_b cell values carry the carrier "
                "reconstruction's alternating nullspace mode)"
            ),
            "time_window_s": [t0, t1],
            "keyframe_dt_s": (t1 - t0) / (frames - 1),
            "n_steps": res["n_steps"],
            "dt_s_min": res["dt_s_min"],
            "dt_s_max": res["dt_s_max"],
            "dt_cap_s": DT_CAP_S,
            "cfl_target": CFL_TARGET,
            "cfl_adv_max": res["cfl_adv"],
            "cfl_diff_max": res["cfl_diff"],
            "pe_def": "alpha_peak * u_face_max * spacing_mm / D (mesh Peclet)",
            "cfl_def": (
                "dt * max_i (alpha*(sum |F_face| + |r_i|) + D*sum A/h) / V_i per step "
                "(max over steps)"
            ),
            "n_active_cells": model.n_a,
            "geometry_convention": (
                "origin_mm = lattice corner; cell centers = origin_mm + (ijk+0.5)*spacing_mm; "
                "lattice faces halfway between cell centers, area = spacing_mm^2"
            ),
            "spacing_mm": float(np.asarray(car["spacing_mm"], dtype=np.float64)),
            "div_residual_max_mm3_s": model.div_residual,
            "open_face_balance_max_rel": topo["open_balance_max_rel"],
            "residual_flux_rel_max": topo["residual_rel_max"],
            "n_open_cells": topo["n_open_cells"],
            "n_open_dual_flag_cells": topo["n_both_flags"],
            "n_open_sign_mismatch_cells": topo["n_sign_mismatch"],
            "n_matched_residual_cells": int(model.res_cells.size),
            "q_in_base_mLs": model.Ain / 1e3,
            "q_out_base_mLs": float(topo["out_q"].sum()) / 1e3,
            "int_q_in_c_in": q_in_c_in_mL,
            "int_q_in_c_in_signal_mL": sig_check_mL,
            "int_q_in_c_in_signal_abs_err_mL": abs(q_in_c_in_mL - sig_check_mL),
            "C_min_observed": res["c_min"],
            "C_max_observed": res["c_max"],
            "eps_series_zero_before_N": 1e-12 * q_in_c_in_mL,
        },
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    npz_path = out_dir / f"{case}_{profile}.npz"
    json_path = out_dir / f"{case}_{profile}.json"
    np.savez_compressed(
        npz_path,
        t_s=res["kf"].astype(np.float32),
        C=res["C"],
        cell_ravel=car["cell_ravel"],
        grid_shape=car["grid_shape"],
        origin_mm=np.asarray(car["origin_mm"]),
        spacing_mm=np.asarray(car["spacing_mm"]),
    )
    json_path.write_text(json.dumps(payload, indent=2) + "\n")

    print(
        f"[solve_advection] case {case} profile {profile}: steps={res['n_steps']} "
        f"cfl={res['cfl']:.3f} eps_mass_max={res['eps_mass_max']:.3e} "
        f"C in [{res['c_min']:.3e}, {res['c_max']:.6f}] verdict={verdict}",
        flush=True,
    )

    if verdict == "fail":
        worst = sorted(res["series"], key=lambda r: -r[1])[:5]
        detail = "; ".join(f"t={t:.6f}s eps={e:.3e}" for t, e in worst)
        raise RuntimeError(
            f"case {case} profile {profile}: hard fail — eps_mass_max={res['eps_mass_max']:.3e} "
            f"(tol {EPS_MASS_TOL:.0e}), cfl={res['cfl']:.3f} (limit {CFL_LIMIT}), "
            f"C range [{res['c_min']:.3e}, {res['c_max']:.6f}] (tol {MONO_TOL:.0e}). "
            f"worst eps rows: {detail}. budget [mL / mL-fraction]: "
            f"int_C_dV={res['budget']['int_C_dV'] * MM3_PER_ML_INV:.6e}, "
            f"int_boundary_flux={res['budget']['int_boundary_flux'] * MM3_PER_ML_INV:.6e}, "
            f"int_q_in_c_in={res['int_q_in_c_in'] * MM3_PER_ML_INV:.6e}. "
            f"Outputs written to {npz_path} / {json_path}."
        )
    return payload


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--cases", nargs="+", default=["601", "700", "798"])
    ap.add_argument("--profiles", nargs="+", default=["A", "B", "C"])
    ap.add_argument("--carrier-dir", default="out/transport/carrier")
    ap.add_argument("--hemo-dir", default="out/rom/hemodynamics")
    ap.add_argument("--out-dir", default="out/transport/contrast")
    ap.add_argument("--frames", type=int, default=90,
                    help="uniform keyframes per profile (contract range 60-120)")
    args = ap.parse_args(argv)
    if args.frames < 2:
        ap.error("--frames must be >= 2")

    command = " ".join(sys.argv)
    for case in args.cases:
        car = load_carrier(Path(args.carrier_dir) / f"{case}_carrier.npz")
        topo = build_topology(car)
        hemo = load_hemo(Path(args.hemo_dir) / str(case) / "hemodynamics.json", case)
        for profile in args.profiles:
            sig = extract_signal(hemo, profile, Path(args.hemo_dir) / str(case) / "hemodynamics.json")
            run_profile(case, profile, car, topo, sig, args.frames,
                        Path(args.out_dir), command)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
