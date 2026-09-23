"""Synthetic run corpus generator for FlowScope surrogate training (Track 4.1).

SYNTHETIC ONLY -- no patient data. Produces `data/synthetic_corpus/` per shared
contract 1: `samples/{id:05d}.npz` (keys: u, p, params, delta_p + documented
extensions occ, C) and `index.json` (schema, provenance, equations solved,
parameter layout/conventions, per-sample metadata, train/val split).

The physics are analytic/semi-analytic ("Stokes-lite" Hagen-Poiseuille +
curvature/stenosis/junction corrections, plus an IMEX upwind advection-diffusion
solve for contrast transport). See `synthetic_physics.py` module docstring and
the "equations" block in index.json: this is NOT full Navier-Stokes CFD.

Usage:
    .venv/bin/python phase2/surrogate/generate_corpus.py \
        --n-train 400 --n-val 50 --grid 48 --seed 1234 \
        --out data/synthetic_corpus
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import time
from datetime import datetime, timezone

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import synthetic_physics as sp  # noqa: E402

TOOL_VERSION = "1.0.0"
PARAMS_LAYOUT = [
    "Re", "D", "inlet_bc", "R0", "kappa", "stenosis", "branch_angle",
    "geometry_type", "L", "u_mean_in", "stenosis_axial_frac",
    "branch_axial_frac", "branch_radius_ratio", "t_end", "Q_in",
    "centerline_length",
]
PARAMS_DIM = len(PARAMS_LAYOUT)


# ----------------------------------------------------------------------------
# parameter sampling (all ranges seeded; deterministic per sample id)

def sample_geometry(rng: np.random.Generator) -> dict:
    g = {
        "L": sp.L_DOMAIN,
        "geometry_type": int(rng.integers(0, 4)),
        "R0": float(rng.uniform(1.0e-3, 3.0e-3)),        # m
        "u_mean_in": float(rng.uniform(0.2, 1.0)),        # m/s
        "kappa": 0.0, "curv_sign": 1.0, "turn_angle": 0.0,
        "stenosis": 0.0, "stenosis_axial_frac": 0.0,
        "branch_angle": 0.0, "branch_axial_frac": 0.0,
        "branch_radius_ratio": 1.0,
        "D": float(10.0 ** rng.uniform(-9.0, -6.0)),      # m^2/s
        "inlet_bc": int(rng.integers(0, 3)),
        "t_end": float(rng.uniform(0.1, 1.0)),            # s
    }
    t = g["geometry_type"]
    if t == sp.CURVED:
        phi = float(np.deg2rad(rng.uniform(20.0, 90.0)))   # total turn angle
        sgn = 1.0 if rng.random() < 0.5 else -1.0
        g["turn_angle"] = phi
        g["curv_sign"] = sgn
        g["kappa"] = sgn * phi / g["L"]
    elif t == sp.STENOSIS:
        g["stenosis"] = float(rng.uniform(0.1, 0.7))       # area reduction
        g["stenosis_axial_frac"] = float(rng.uniform(0.3, 0.7))
    elif t == sp.BIFURCATION:
        g["branch_angle"] = float(np.deg2rad(rng.uniform(15.0, 60.0)))
        g["branch_axial_frac"] = float(rng.uniform(0.4, 0.6))
        r = float(rng.uniform(1.0, 1.6))                   # R1/R2
        g["branch_radius_ratio"] = float((r ** 3 / (1.0 + r ** 3)) ** (1.0 / 3.0))
    return g


# ----------------------------------------------------------------------------
# per-sample build (runs in workers)

def build_sample(sample_id: int, g: dict, n: int):
    vessels = sp.make_vessels(g)
    pts = sp.grid_points(n, g["L"])
    u, p, occ, peak_u = sp.build_fields(g, vessels, n, g["L"], pts)
    open_f, inlet_f = sp.face_flags(vessels, occ, n, g["L"])
    cl_len = float(sum(v["ell"] for v in vessels))
    tau = cl_len / g["u_mean_in"]
    t_c = float(np.clip(g["t_end"] - 0.6 * tau, 0.05 * g["t_end"], g["t_end"]))
    c_field = sp.solve_transport(u, occ, open_f, inlet_f, g["D"], g["inlet_bc"],
                                 g["t_end"], t_c, tau, n, g["L"])

    re_num = sp.RHO * g["u_mean_in"] * 2.0 * g["R0"] / sp.MU
    q_in = g["u_mean_in"] * np.pi * g["R0"] ** 2
    delta_p = float(next(v for v in vessels if v["is_inlet"])["p_prof"][0])

    vec = np.array([
        re_num, g["D"], g["inlet_bc"], g["R0"], g["kappa"], g["stenosis"],
        g["branch_angle"], g["geometry_type"], g["L"], g["u_mean_in"],
        g["stenosis_axial_frac"], g["branch_axial_frac"],
        g["branch_radius_ratio"], g["t_end"], q_in, cl_len,
    ], dtype=np.float32)

    arrays = {
        "u": u, "p": p, "params": vec,
        "delta_p": np.asarray(delta_p, dtype=np.float32),
        "occ": occ.astype(np.uint8),
        "C": c_field,
    }
    meta = {
        "id": sample_id,
        "file": f"samples/{sample_id:05d}.npz",
        "params": vec.tolist(),
        "geometry": dict(g),
        "Re": re_num,
        "Q_in_m3_s": q_in,
        "centerline_length_m": cl_len,
        "delta_p": delta_p,
        "stats": {
            "peak_u_mps": peak_u,
            "fluid_voxels": int(occ.sum()),
            "max_C": float(c_field.max()),
        },
    }
    return meta, arrays


def _worker(args):
    return build_sample(*args)


# ----------------------------------------------------------------------------
# index.json

def index_document(seed, n, n_train, n_val, metas, command, wall_s,
                   corpus_bytes, workers):
    for m in metas:
        m["split"] = "train" if m["id"] < n_train else "val"
    metas.sort(key=lambda m: m["id"])
    rows = [{
        "id": m["id"], "file": m["file"], "split": m["split"],
        "geometry_type": sp.GEOM_NAMES[m["geometry"]["geometry_type"]],
        "params": m["params"], "geometry": m["geometry"],
        "delta_p": m["delta_p"], "stats": m["stats"],
    } for m in metas]

    dp = np.array([m["delta_p"] for m in metas])
    pu = np.array([m["stats"]["peak_u_mps"] for m in metas])
    re = np.array([m["Re"] for m in metas])
    fv = np.array([m["stats"]["fluid_voxels"] for m in metas])

    def rng3(a):
        return {"min": float(a.min()), "median": float(np.median(a)),
                "max": float(a.max())}

    return {
        "schema_version": 1,
        "generator": {
            "tool": "phase2/surrogate/generate_corpus.py",
            "physics_module": "phase2/surrogate/synthetic_physics.py",
            "version": TOOL_VERSION,
            "command": command,
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "python": platform.python_version(),
            "numpy": np.__version__,
            "seed": seed,
            "n_workers": workers,
            "generation_wall_time_s": wall_s,
            "corpus_bytes": corpus_bytes,
            "corpus_bytes_scope": "sum of samples/*.npz; index.json excluded",
        },
        "seed": seed,
        "grid": [n, n, n],
        "n_train": n_train,
        "n_val": n_val,
        "fields": {"u": [n, n, n, 3], "p": [n, n, n]},
        "extra_fields": {"occ": [n, n, n], "C": [n, n, n]},
        "npz_keys": {
            "u": "float32 [N,N,N,3] velocity field (m/s), 0 on solid voxels",
            "p": "float32 [N,N,N] pressure (Pa), gauge: mean outlet = 0",
            "params": "float32 [16] parameter vector, see params.layout",
            "delta_p": "float32 scalar pressure drop (Pa), see delta_p block",
            "occ": "EXTENSION uint8 [N,N,N] lumen occupancy, 1=fluid 0=solid",
            "C": "EXTENSION float32 [N,N,N] contrast concentration at t_end "
                 "(dimensionless 0..1)",
        },
        "params": {
            "layout": PARAMS_LAYOUT,
            "dtype": "float32[16]",
            "Re": {
                "desc": "inlet Reynolds number rho*u_mean_in*2*R0/mu",
                "range": rng3(re),
            },
            "D": {"desc": "contrast diffusion coefficient", "units": "m^2/s",
                  "sampling": "log-uniform in [1e-9, 1e-6]"},
            "inlet_bc": {
                "desc": "contrast injection profile id (drives the inlet-cap "
                        "advective flux). Timed relative to the domain transit "
                        "time tau = centerline_length/u_mean_in so the "
                        "contrast feature lies inside the domain at t_end: "
                        "t_c = clamp(t_end - 0.6*tau, 0.05*t_end, t_end).",
                "0": "step_bolus: C=1 for t<t_c, then 0 (washout front)",
                "1": "gaussian_bolus: exp(-((t-t_c)/(0.2*tau))^2/2)",
                "2": "square_pulse: C=1 for t_c-0.25*tau<t<t_c+0.25*tau, else 0",
            },
            "geometry": {
                "family": "parametric tubular domains (straight/arc segments "
                          "with radius profiles R(s)) on a cell-centered "
                          "[N,N,N] grid",
                "types": {"0": "straight", "1": "curved (circular arc)",
                          "2": "bifurcation (Y-junction)",
                          "3": "stenosis (straight + cos^2 radius bump)"},
                "sampling_ranges": {
                    "R0_m": [1e-3, 3e-3],
                    "u_mean_in_mps": [0.2, 1.0],
                    "stenosis_area_reduction": [0.1, 0.7],
                    "branch_angle_deg_total": [15.0, 60.0],
                    "curvature_turn_angle_deg": [20.0, 90.0],
                    "D_m2_s": [1e-9, 1e-6],
                    "t_end_s": [0.1, 1.0],
                    "injection_timing": "t_c = clamp(t_end - 0.6*tau, "
                                        "0.05*t_end, t_end), tau = "
                                        "centerline_length/u_mean_in",
                },
                "conventions": {
                    "grid": "cell-centered, x_i=(i+0.5)*L/N; npz arrays are "
                            "C-order [i,j,k] -> (x,y,z)",
                    "domain": f"[0,L]^3, L={sp.L_DOMAIN} m; the lumen bounding "
                              "box is translated to the domain center",
                    "parent": "inlet vessel starts at local (0,0,0) heading +x "
                              "(before translation); the inlet cap is the "
                              "proximal end of vessel 0 (params index 0 = Re)",
                    "curved": "circular arc; params[4] kappa is signed "
                              "(kappa = curv_sign*turn_angle/ell, ell is the "
                              "domain-fitted arc length <= L), bending in the "
                              "x-y plane",
                    "stenosis": "R(s) = R0*sqrt(1 - S*bump(s)), "
                                "bump = 0.5*(1+cos(pi*(s-s0)/w)) for |s-s0|<w "
                                "(0 outside), w=2.5*R0, s0=params[10]*ell (ell "
                                "= fitted vessel length = params[15] for "
                                "single-vessel geometries); S=params[5] is the "
                                "fractional lumen-AREA reduction at the apex "
                                "(Young-Tsai-style idealized stenosis)",
                    "bifurcation": "straight parent to branch point at "
                                   "params[11]*span_x (span_x = L - 2*margin - "
                                   "R0 - max(R1,R2), margin = L/48); two "
                                   "straight daughters in the x-z plane "
                                   "symmetric at +/-params[6]/2 from +x "
                                   "(params[6] is the TOTAL daughter angle), "
                                   "each reaching the x-end of span_x; "
                                   "R1=params[12]*R0, R2=(R0^3-R1^3)^(1/3); "
                                   "flow split Murray's law Q_i ~ R_i^3",
                    "occupancy": "occ=1 where distance-to-nearest-centerline < "
                                 "local R(s); analytic reconstruction is "
                                 "possible from params + these conventions, but "
                                 "the npz occ key is authoritative",
                },
            },
        },
        "delta_p": {
            "desc": "inlet pressure minus flow-rate-weighted mean outlet "
                    "pressure; gauge is chosen so mean outlet p=0 and "
                    "p(inlet)=delta_p",
            "units": "Pa",
            "range": rng3(dp),
        },
        "equations": {
            "flow": (
                "Steady laminar Hagen-Poiseuille profiles attached to the "
                "parametric centerlines (u_axial=2Q/(pi R(s)^2)*(1-(r/R)^2)) "
                "with the lubrication pressure law dp/ds=8 mu Q/(pi R(s)^4) "
                "integrated per vessel. Stenosis acts only through R(s); "
                "bifurcations use a Murray's-law flow split with pressure "
                "continuity and a heuristic junction minor loss "
                "0.3*(1/2 rho u_p^2); curved tubes add a heuristic Dean-like "
                "secondary vortex pair (streamfunction amplitude "
                "min(0.15, 0.02*sqrt(De)), De=Re*sqrt(R0|kappa|)). Vessel "
                "fields blend by normalized Poiseuille shape weights and lack "
                "a radial component in varying-area tubes, so the field is "
                "only approximately divergence-free near junctions and "
                "stenosis throats."),
            "transport": (
                "dC/dt + div(u C) = D Lap(C) on the voxel grid, solved in the "
                "equivalent non-conservative form (conservative flux "
                "difference + C*div(u) correction) because the model velocity "
                "is only approximately solenoidal (no radial component in "
                "varying-area tubes, junction cross-flow blending); this "
                "preserves the concentration maximum principle. First-order "
                "IMEX in time: explicit MUSCL/minmod TVD-upwind advection + "
                "implicit backward-Euler diffusion (sparse LU); no-flux at "
                "lumen walls, advective in/outflow at vessel caps (inlet "
                "faces driven by params.inlet_bc), no diffusive cap flux. "
                "Cell Peclet u*dx/D >> 2 for D <= ~1e-5 m^2/s here: small-D "
                "samples carry residual scheme dispersion and are qualitative "
                "transport ground truth."),
            "simplifications": (
                "Newtonian mu=3.5e-3 Pa*s, rho=1060 kg/m^3; steady (no "
                "pulsatility/Womersley); rigid walls; no flow separation, "
                "recirculation, turbulence or FSI; no inertia-dominated losses "
                "beyond the single junction minor-loss term; Poiseuille is not "
                "valid at severe stenosis throats where continuity-based jet "
                "speed-up (throat peak u up to ~2*u_mean/(1-S) for area "
                "stenosis S) is expected and observed. THIS IS A SYNTHETIC "
                "CORPUS, NOT FULL NAVIER-STOKES CFD."),
        },
        "units": "SI (m, m/s, Pa); C dimensionless",
        "constants": {"mu_Pa_s": sp.MU, "rho_kg_m3": sp.RHO,
                      "junction_loss_k": sp.JUNCTION_LOSS_K,
                      "stenosis_half_width_R0": sp.STENOSIS_HALF_WIDTH_R},
        "stats": {"delta_p_Pa": rng3(dp), "peak_u_mps": rng3(pu),
                  "Re": rng3(re), "fluid_voxels": rng3(fv)},
        "train": [m["id"] for m in metas if m["id"] < n_train],
        "val": [m["id"] for m in metas if m["id"] >= n_train],
        "samples": rows,
    }


# ----------------------------------------------------------------------------
# CLI

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--n-train", type=int, default=400)
    ap.add_argument("--n-val", type=int, default=50)
    ap.add_argument("--grid", type=int, default=48)
    ap.add_argument("--seed", type=int, default=1234)
    ap.add_argument("--out", default="data/synthetic_corpus")
    ap.add_argument("--workers", type=int, default=min(4, os.cpu_count() or 1))
    args = ap.parse_args(argv)

    out_dir = args.out
    samples_dir = os.path.join(out_dir, "samples")
    os.makedirs(samples_dir, exist_ok=True)
    n_total = args.n_train + args.n_val
    command = " ".join([sys.executable] + sys.argv)

    t0 = time.perf_counter()
    jobs = []
    for sid in range(n_total):
        rng = np.random.default_rng([args.seed, sid])
        jobs.append((sid, sample_geometry(rng), args.grid))

    metas = []
    corpus_bytes = 0
    if args.workers > 1:
        from concurrent.futures import ProcessPoolExecutor
        pool = ProcessPoolExecutor(max_workers=args.workers)
        results = pool.map(_worker, jobs, chunksize=4)
    else:
        results = map(_worker, jobs)

    for i, (meta, arrays) in enumerate(results):
        path = os.path.join(samples_dir, f"{meta['id']:05d}.npz")
        np.savez_compressed(path, **arrays)
        corpus_bytes += os.path.getsize(path)
        metas.append(meta)
        if (i + 1) % 25 == 0 or i + 1 == n_total:
            el = time.perf_counter() - t0
            print(f"[{i+1}/{n_total}] {el:.1f}s elapsed", flush=True)
    if args.workers > 1:
        pool.shutdown()

    wall_s = time.perf_counter() - t0
    doc = index_document(args.seed, args.grid, args.n_train, args.n_val, metas,
                         command, wall_s, corpus_bytes, args.workers)
    with open(os.path.join(out_dir, "index.json"), "w") as f:
        json.dump(doc, f, indent=1)

    # contract validation: load one train and one val sample, assert shapes
    n = args.grid
    for sid in (0, n_total - 1):
        with np.load(os.path.join(samples_dir, f"{sid:05d}.npz")) as z:
            assert z["u"].shape == (n, n, n, 3) and z["u"].dtype == np.float32
            assert z["p"].shape == (n, n, n) and z["p"].dtype == np.float32
            assert z["params"].shape == (PARAMS_DIM,) \
                and z["params"].dtype == np.float32
            assert z["delta_p"].dtype == np.float32 and z["delta_p"].ndim == 0
            assert z["occ"].shape == (n, n, n) and z["occ"].dtype == np.uint8
            assert z["C"].shape == (n, n, n) and z["C"].dtype == np.float32

    dp = np.array([m["delta_p"] for m in metas])
    pu = np.array([m["stats"]["peak_u_mps"] for m in metas])
    print(f"done: {n_total} samples in {wall_s:.1f}s, "
          f"corpus {corpus_bytes/1e6:.1f} MB (npz)")
    print(f"delta_p  Pa: min={dp.min():.0f} median={np.median(dp):.0f} "
          f"max={dp.max():.0f}")
    print(f"peak_u  m/s: min={pu.min():.2f} median={np.median(pu):.2f} "
          f"max={pu.max():.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
