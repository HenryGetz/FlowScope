#!/usr/bin/env python
"""Track 4.2 / C2 - 0D hemodynamics + 1D contrast transport for C1 graphs.

Reads out/rom/graphs/{case}_graph.json (schema flowscope.rom.graph v1, built by
phase2/rom/build_network_graph.py) and emits per case under
out/rom/hemodynamics/{case}/:

  svzero_config.json   exact pysvzerod (svZeroDSolver) model JSON: every C1
                       edge split into ~10 mm BloodVessel R/L/C sub-segments,
                       terminal outlets as 3-element Windkessels (RCR), root
                       FLOW boundary conditions (profile-A inflow waveform).
  hemodynamics.json    schema flowscope.rom.hemodynamics v1 (contract C2):
                       physiology, windkessel calibration, profiles A/B/C,
                       per-profile inlet signals + per-branch 1D-transport
                       arrival curves, mass balance, timings.

0D elements (proven phase2/rom/run_zerod.py parameter set; helpers imported
read-only from it):
    R = 8*mu*L/(pi*r^4)   (Poiseuille)      rho = 1060 kg/m^3
    L = rho*L/(pi*r^2)    (inertance)       mu  = 3.5e-3 Pa s
    C = 2*pi*r^2*L/K_wall (linear wall)     K_wall = 2e5 Pa

Terminal Windkessel (Rp, C, Rd), calibrated per C2:
    R_total = (p_mean - p_venous) / q_terminal, q_terminal split ~ r^3,
    Rp = 0.10 * R_total (5-10% band), tau = Rd*C = 1.75 s (1.5-2.0 s band).
Exact values + calibration notes are recorded under "windkessel".

Inflow protocols A/B/C per C2 ("profiles"): in `replace` mode (default) during
an injection segment the root inflow IS the injectate (c_in = 1 for contrast,
0 for saline), washout is baseline blood at Q_b = 3.75 mL/s (225 mL/min) split
over roots ~ root_radius^3; `additive` adds the injectate on top of the
baseline. `--injection-root all` distributes the injectate ~ root_radius^3.

Backends (--solver): svzerodsolver (pysvzerod 2.0, headline), reference (scipy
solve_ivp integration of the same lumped R/L/C network, engineering
cross-check), or both. A failed/stiff svzerodsolver run falls back to the
reference integrator and the fallback is recorded in the output.

1D contrast transport along each edge's C1 arc discretization: finite-volume
MUSCL-TVD advection with the Superbee limiter (explicit, adaptive CFL <= 0.5
sub-cycling), central diffusion D = 1e-3 mm^2/s, flow-weighted mixing of the
outgoing concentrations at junctions. The scheme is in flux form so the
per-profile contrast mass budget closes to machine precision; the discrete
rel_error is recorded in "mass_balance".

Phase 2.5 (Contract B "clinical" block): pressure-driven REST and maximal-
hyperemia solves (roots at Pa = 90 mmHg, terminals = the same 3-element
Windkessels with Rd -> 0.25*Rd_rest under hyperemia) with optional Young-Tsai
(1979) stenosis losses from a Contract-A lesions file (--lesions, or
auto-loaded out/clinical/lesions/case_<case>_lesions.json). Each lesion loss
dP = Kv*mu/r0^2*u*L + Kt*rho/2*(A0/As-1)^2*u^2 (Kv = 32*(L/D0)*(A0/As)^2,
Kt = 1.52, rho = 1050 kg/m^3) is coupled as a series quadratic dP(Q) term
between its proximal/distal sub-segment nodes via Picard iteration on Q. The
block also carries per-edge pressures/flows/velocities, per-lesion dP + vFFR
(Pd sampled >= 20 mm distal of the lesion) and the resting major-trunk
velocity calibration (kappa; transit_calibrated_ms is added beside each
signals.<P>.branches transit_ms). The existing flow-BC solves used for
transport are unchanged.
"""

import os

for _tnv in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS"):
    os.environ[_tnv] = "8"

import argparse
import json
import math
import sys
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import numpy as np

# proven helpers from run_zerod.py (imported read-only; never modified)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from run_zerod import MMHG, dist_version, dof_map, vessel_elements

TOOL = {"name": "phase2/rom/run_hemodynamics.py", "version": "0.1.0"}

# --------------------------------------------------------------------------- #
# physiology / numerics constants (contract C2)
# --------------------------------------------------------------------------- #
RHO_KG_M3 = 1060.0
MU_PA_S = 3.5e-3
WALL_K_PA = 2.0e5  # wall stiffness Eh/r [Pa]; same as run_zerod.py
R_MIN_M = 1e-5  # pure division guard (real geometry is never clamped)
Q_BASE_MLS = 3.75  # 225 mL/min baseline total coronary flow
P_MEAN_MMHG = 90.0
P_SYSTOLIC_MMHG = 120.0
P_DIASTOLIC_MMHG = 80.0
P_VENOUS_MMHG = 5.0
D_MM2_S = 1e-3  # contrast diffusivity in blood [mm^2/s]
RP_FRACTION = 0.10  # Rp share of R_total (5-10% band of C2)
TAU_S = 1.75  # Windkessel runoff Rd*C (1.5-2.0 s band of C2)
SUBSEG_MM = 10.0  # target 0D sub-segment length [mm]
DT_MAX_S = 0.002  # signal grid spacing bound (C2: dt <= 2 ms)
CFL_MAX = 0.5  # 1D transport CFL bound
Q_EPS_MLS = 1e-12  # near-zero-flow guard
Q_MEAN_MIN_MLS = 1e-9  # below this a mean flow counts as zero (guards V/q)

# ---- Phase 2.5 clinical hemodynamics (Contract B "clinical" block) --------
RHO_YOUNG_TSAI = 1050.0  # kg/m^3, Young-Tsai inertial term only (spec)
MU_YOUNG_TSAI = 3.5e-3  # Pa s (same blood viscosity as MU_PA_S)
KT_YOUNG_TSAI = 1.52  # Young-Tsai inertial loss coefficient
P_AORTA_MMHG = 90.0  # mean aortic pressure driving the clinical solves
HYPER_RD_SCALE = 0.25  # maximal hyperemia: every terminal Rd -> 0.25*Rd_rest
FLOW_INCREASE_BAND = (2.5, 4.0)  # accepted hyper.flow_increase_x
TARGET_U_TRUNK_MS = 0.22  # resting major-trunk velocity benchmark (15-30 cm/s)
U_TRUNK_BAND_MS = (0.15, 0.30)
TRUNK_R_IN_MM = 0.75  # trunk = edge with r_in_mm >= 0.75
PD_DISTAL_MM = 20.0  # per-lesion Pd sampled >= 20 mm distal of the lesion
PICARD_TOL_MLS = 1e-9  # Picard residual on Q [mL/s]
PICARD_MAX_ITER = 500
CLIN_PERIOD_S = 4.0  # pysvzerod cross-check window (constant BCs, settled)

MM3_PER_ML = 1.0e3  # 1 mL = 1e3 mm^3
M3_PER_ML = 1.0e-6  # 1 mL = 1e-6 m^3
SI_PER_MMHG_ML = MMHG * 1e6  # mmHg s/mL -> Pa s/m^3 and mL/mmHg -> m^3/Pa

PROFILES = {
    "A": {
        "label": "ACIST CVi coronary bolus",
        "segments": [{"fluid": "contrast", "q_mLs": 4.0, "t_s": 2.0}],
        "washout_s": 6.0,
    },
    "B": {
        "label": "Micro-catheter selective infusion",
        "segments": [{"fluid": "contrast", "q_mLs": 1.5, "t_s": 3.5}],
        "washout_s": 6.0,
    },
    "C": {
        "label": "Saline flush protocol",
        "segments": [
            {"fluid": "contrast", "q_mLs": 4.0, "t_s": 1.5},
            {"fluid": "saline", "q_mLs": 4.0, "t_s": 1.5},
        ],
        "washout_s": 6.0,
    },
}


class SolveError(RuntimeError):
    """0D solve produced unusable output (triggers recorded fallback)."""


# --------------------------------------------------------------------------- #
# graph -> 0D sub-segments (run_zerod build_graph/dof_map shapes)
# --------------------------------------------------------------------------- #
def load_graph(path):
    with open(path) as f:
        return json.load(f)


def edge_radius_sampler(edge):
    """(s_mm, r_mm) along the edge polyline arc, s measured from the proximal end."""
    arc = np.asarray(edge["arc_mm"], dtype=float)
    rad = np.asarray(edge["radius_mm"], dtype=float)
    l_mm = float(edge["length_mm"])
    if arc.size != rad.size:  # defensive: resample radius onto the arc grid
        s = np.linspace(0.0, l_mm, max(arc.size, rad.size))
        return s, np.interp(s, np.linspace(0.0, l_mm, rad.size), rad)
    s = arc - arc[0]
    if s.size < 2 or s[-1] <= 0:
        return (
            np.array([0.0, l_mm]),
            np.array([float(edge["r_in_mm"]), float(edge["r_out_mm"])]),
        )
    return s, rad


def build_model(graph):
    """Expand C1 edges into ~10 mm BloodVessel sub-segments + node maps.

    Returns (vessels, nodes, inlets, outlets, edges): vessels in the
    run_zerod.py shapes used by vessel_elements/dof_map
    ({name, branch, parent_branch, sub_index, length_m, r_in_m, r_out_m,
    from_node, to_node, vessel_id}), nodes {name: {kind, in_vessels,
    out_vessels}} with kind inlet/outlet/junction, and the C1 edge records.
    """
    nodes_g = graph["nodes"]
    edges_g = graph["edges"]
    name_of = {}
    for nd in nodes_g:
        if nd["name"] in name_of:
            raise SystemExit(
                f"run_hemodynamics: duplicate graph node name {nd['name']!r}"
            )
        name_of[nd["id"]] = nd["name"]
    edge_by_id = {e["id"]: e for e in edges_g}
    edge_name_of = {e["id"]: e["name"] for e in edges_g}

    vessels = []
    for e in edges_g:
        s_prof, r_prof = edge_radius_sampler(e)
        l_mm = float(e["length_mm"])
        n_sub = max(1, int(round(l_mm / SUBSEG_MM)))
        parent = edge_name_of.get(e["parent_edge"])
        for k in range(n_sub):
            s0, s1 = l_mm * k / n_sub, l_mm * (k + 1) / n_sub
            frm = name_of[e["from_node"]] if k == 0 else f"k_{e['name']}_{k-1}"
            to = name_of[e["to_node"]] if k == n_sub - 1 else f"k_{e['name']}_{k}"
            vessels.append(
                {
                    "name": f"{e['name']}_s{k}",
                    "branch": e["name"],
                    "parent_branch": parent,
                    "sub_index": k,
                    "length_m": (s1 - s0) * 1e-3,
                    "r_in_m": float(np.interp(s0, s_prof, r_prof)) * 1e-3,
                    "r_out_m": float(np.interp(s1, s_prof, r_prof)) * 1e-3,
                    "from_node": frm,
                    "to_node": to,
                    "is_root_child": parent is None,
                    "edge_id": e["id"],
                    "vessel_id": len(vessels),
                }
            )

    nodes = {}
    for v in vessels:
        nodes.setdefault(
            v["from_node"], {"kind": None, "in_vessels": [], "out_vessels": []}
        )["out_vessels"].append(v["name"])
        nodes.setdefault(
            v["to_node"], {"kind": None, "in_vessels": [], "out_vessels": []}
        )["in_vessels"].append(v["name"])
    kind_map = {"root": "inlet", "terminal": "outlet", "bifurcation": "junction"}
    for nd in nodes_g:
        nodes[name_of[nd["id"]]]["kind"] = kind_map.get(nd["kind"], "junction")
    for nd in nodes.values():  # internal sub-segment chain nodes
        if nd["kind"] is None:
            nd["kind"] = "junction"

    inlets = {}
    outlets = {}
    for nd in nodes_g:
        nm = name_of[nd["id"]]
        if nd["kind"] == "root":
            e0 = edge_by_id[nd["out_edges"][0]] if nd["out_edges"] else None
            inlets[nm] = {
                "name": nm,
                "root_radius_mm": float(
                    nd.get("radius_mm", e0["r_in_mm"] if e0 else 1.0)
                ),
            }
        elif nd["kind"] == "terminal":
            e0 = edge_by_id[nd["in_edges"][0]] if nd["in_edges"] else None
            outlets[nm] = {
                "name": nm,
                "radius_mm": float(
                    nd.get("radius_mm", e0["r_out_mm"] if e0 else 1.0)
                ),
                "edge_name": edge_name_of.get(e0["id"]) if e0 else None,
            }
    return vessels, nodes, inlets, outlets, edges_g


# --------------------------------------------------------------------------- #
# Windkessel (3-element RCR) sizing per C2
# --------------------------------------------------------------------------- #
def size_windkessel(outlets):
    """R_total = (p_mean - p_venous)/q_terminal with q_terminal ~ r^3 split."""
    s3 = sum(o["radius_mm"] ** 3 for o in outlets.values())
    if s3 <= 0:
        raise SystemExit("run_hemodynamics: degenerate terminal radii (sum r^3 = 0)")
    wk = {}
    for name, o in sorted(outlets.items()):
        q_i = Q_BASE_MLS * (o["radius_mm"] ** 3) / s3  # mL/s
        r_tot = (P_MEAN_MMHG - P_VENOUS_MMHG) / q_i  # mmHg s/mL
        rp = RP_FRACTION * r_tot
        rd = (1.0 - RP_FRACTION) * r_tot
        c_wk = TAU_S / rd  # mL/mmHg
        wk[name] = {
            "bc_name": f"RCR_{name}",
            "bc_values": {  # SI, exactly as handed to pysvzerod
                "Rp": rp * SI_PER_MMHG_ML,
                "C": c_wk / SI_PER_MMHG_ML,
                "Rd": rd * SI_PER_MMHG_ML,
                "Pd": P_VENOUS_MMHG * MMHG,
            },
            "outlet": o["edge_name"],
            "node": name,
            "r_out_mm": o["radius_mm"],
            "q_terminal_mLs": q_i,
            "R_total_mmHg_s_mL": r_tot,
            "Rp_mmHg_s_mL": rp,
            "C_mL_mmHg": c_wk,
            "Rd_mmHg_s_mL": rd,
            "tau_s": TAU_S,
        }
    return wk


# --------------------------------------------------------------------------- #
# inflow protocol: per-root piecewise-constant schedules
# --------------------------------------------------------------------------- #
def root_weights(inlets):
    s3 = sum(r["root_radius_mm"] ** 3 for r in inlets.values())
    return {n: (r["root_radius_mm"] ** 3) / s3 for n, r in inlets.items()}


def resolve_injection_root(injection_root, inlets):
    if injection_root == "all" or injection_root in inlets:
        return injection_root
    names = sorted(inlets)  # allow root_0/root_1 by order on renamed graphs
    try:
        k = int(injection_root.rsplit("_", 1)[1])
    except (ValueError, IndexError):
        raise SystemExit(
            f"run_hemodynamics: --injection-root {injection_root} not among "
            f"graph roots {names}"
        )
    if k < 0 or k >= len(names):
        raise SystemExit(
            f"run_hemodynamics: --injection-root {injection_root} not among "
            f"graph roots {names}"
        )
    return names[k]


def profile_schedule(profile, inflow_mode, injection_root, inlets):
    """Per-root piecewise-constant inflow pieces for one protocol.

    Each piece: {t0, t1, fluid, Q: {root: mL/s}, c: {root: dimless}} where
    c is the contrast fraction of the fluid entering that root.
    """
    w3 = root_weights(inlets)
    root_names = sorted(inlets)
    target = resolve_injection_root(injection_root, inlets)
    if target == "all":
        s3 = sum(inlets[n]["root_radius_mm"] ** 3 for n in root_names)
        v3 = {n: (inlets[n]["root_radius_mm"] ** 3) / s3 for n in root_names}
    else:
        v3 = {target: 1.0}

    pieces = []
    t0 = 0.0
    for seg in profile["segments"]:
        q = float(seg["q_mLs"])
        f_c = 1.0 if seg["fluid"] == "contrast" else 0.0
        Q, c = {}, {}
        for n in root_names:
            q_base = w3[n] * Q_BASE_MLS
            q_inj = v3.get(n, 0.0) * q
            if inflow_mode == "replace":
                q_tot = q_inj
                c[n] = f_c if q_tot > 0 else 0.0
            else:
                q_tot = q_base + q_inj
                c[n] = (q_inj * f_c) / q_tot if q_tot > 0 else 0.0
            Q[n] = q_tot
        pieces.append({"t0": t0, "t1": t0 + float(seg["t_s"]),
                       "fluid": seg["fluid"], "Q": Q, "c": c})
        t0 += float(seg["t_s"])
    Q = {n: w3[n] * Q_BASE_MLS for n in root_names}
    pieces.append({"t0": t0, "t1": t0 + profile["washout_s"], "fluid": "blood",
                   "Q": Q, "c": {n: 0.0 for n in root_names}})
    return pieces, w3, v3


def eval_root_series(pieces, root_names, t):
    """Q_i(t) [mL/s] and c_i(t) per root on grid t (right-continuous steps)."""
    q_mat = np.zeros((len(root_names), t.size))
    c_mat = np.zeros((len(root_names), t.size))
    bnds = np.array([p["t0"] for p in pieces[1:]] + [float("inf")])
    idx = np.searchsorted(bnds, t, side="right")
    for i, p in enumerate(pieces):
        m = idx == i
        if np.any(m):
            for j, n in enumerate(root_names):
                q_mat[j, m] = p["Q"][n]
                c_mat[j, m] = p["c"][n]
    return q_mat, c_mat


def step_waveform(pieces, root, unit):
    """pysvzerod FLOW BC (t [s], Q [m^3/s]) for one root, piecewise constant."""
    t, q = [], []
    for p in pieces:
        val = p["Q"][root] * unit
        t += [p["t0"], max(p["t1"] - 1e-6, p["t0"] + 1e-9)]
        q += [val, val]
    t.append(pieces[-1]["t1"])
    q.append(pieces[-1]["Q"][root] * unit)
    return t, q


# --------------------------------------------------------------------------- #
# svZeroDSolver config assembly (mirrors run_zerod.build_solver_config, with
# per-root inflow waveforms)
# --------------------------------------------------------------------------- #
def build_svzero_config(vessels, nodes, elem, wk, root_waves, period_s, n_pts):
    v_by_name = {v["name"]: v for v in vessels}
    solver_vessels = []
    for v in vessels:
        r_el, l_el, c_el, _ = elem[v["name"]]
        entry = {
            "vessel_id": v["vessel_id"],
            "vessel_name": v["name"],
            "vessel_length": v["length_m"],
            "zero_d_element_type": "BloodVessel",
            "zero_d_element_values": {
                "R_poiseuille": r_el,
                "C": c_el,
                "L": l_el,
                "stenosis_coefficient": 0.0,
            },
        }
        bc = {}
        if nodes[v["from_node"]]["kind"] == "inlet":
            bc["inlet"] = f"IN_{v['from_node']}"
        if nodes[v["to_node"]]["kind"] == "outlet":
            bc["outlet"] = wk[v["to_node"]]["bc_name"]
        if bc:
            entry["boundary_conditions"] = bc
        solver_vessels.append(entry)

    junctions = []
    for name, nd in nodes.items():
        if nd["kind"] in ("inlet", "outlet"):
            continue
        junctions.append(
            {
                "junction_name": f"JN_{name}",
                "junction_type": "NORMAL_JUNCTION",
                "inlet_vessels": [v_by_name[x]["vessel_id"] for x in nd["in_vessels"]],
                "outlet_vessels": [v_by_name[x]["vessel_id"] for x in nd["out_vessels"]],
            }
        )

    bcs = []
    for rname in sorted(root_waves):
        t_list, q_list = root_waves[rname]
        bcs.append(
            {
                "bc_name": f"IN_{rname}",
                "bc_type": "FLOW",
                "bc_values": {
                    "Q": [float(x) for x in q_list],
                    "t": [float(x) for x in t_list],
                },
            }
        )
    for name in sorted(wk):
        bcs.append(
            {"bc_name": wk[name]["bc_name"], "bc_type": "RCR",
             "bc_values": wk[name]["bc_values"]}
        )

    return {
        "simulation_parameters": {
            "number_of_cardiac_cycles": 1,
            "number_of_time_pts_per_cardiac_cycle": int(n_pts),
            "steady_initial": True,
            "output_variable_based": True,
            "output_all_cycles": False,
            "output_interval": 1,
            "cardiac_period": float(period_s),
            "absolute_tolerance": 1e-8,
        },
        "vessels": solver_vessels,
        "junctions": junctions,
        "boundary_conditions": bcs,
    }


# --------------------------------------------------------------------------- #
# result extraction
# --------------------------------------------------------------------------- #
def parse_result_df(df):
    """Long (name, time, value) solver result -> {dof: (t, y)}; wide tolerated."""
    cols = list(df.columns)
    if len(cols) == 3:
        lower = [str(c).strip().lower() for c in cols]
        i_name = i_time = i_val = None
        for i, lc in enumerate(lower):
            if i_name is None and any(k in lc for k in ("name", "dof", "variable", "point")):
                i_name = i
            elif i_time is None and ("time" in lc or lc == "t"):
                i_time = i
        if i_name is None:  # the non-numeric column carries the dof names
            for i in range(3):
                dt = str(df[cols[i]].dtype)
                if dt == "object" or dt.startswith("str"):
                    i_name = i
                    break
        if i_time is None and i_name is not None:
            cand = [i for i in range(3) if i != i_name]
            for i in cand:  # time column repeats identically per dof group
                ref = None
                ok = True
                for _, g in df.groupby(cols[i_name], sort=False):
                    arr = g[cols[i]].to_numpy()
                    if ref is None:
                        ref = arr
                    elif arr.shape != ref.shape or not np.allclose(arr, ref):
                        ok = False
                        break
                if ok:
                    i_time = i
                    break
        for i in range(3):
            if i not in (i_name, i_time):
                i_val = i
        if None in (i_name, i_time, i_val):
            raise SolveError(f"cannot classify solver result columns {cols}")
        out = {}
        for name, g in df.groupby(cols[i_name], sort=False):
            out[str(name)] = (
                g[cols[i_time]].to_numpy(dtype=float),
                g[cols[i_val]].to_numpy(dtype=float),
            )
        return out
    # wide format: first column time, remaining columns one dof each
    t = df[cols[0]].to_numpy(dtype=float)
    return {str(c): (t, df[c].to_numpy(dtype=float)) for c in cols[1:]}


def _norm(name):
    return str(name).replace("#", ":").replace("/", ":").strip()


def pick_dof(dofs, name):
    if name in dofs:
        return dofs[name]
    target = _norm(name)
    for k in dofs:
        if _norm(k) == target:
            return dofs[k]
    tp = target.split(":")
    for k in dofs:  # tolerate per-part whitespace/format drift
        parts = _norm(k).split(":")
        if len(parts) == len(tp) and all(
            a == b or a.split() == b.split() for a, b in zip(parts, tp)
        ):
            return dofs[k]
    raise SolveError(f"solver result missing dof {name!r}")


def interp_dof(dofs, name, t_s, scale):
    t, y = pick_dof(dofs, name)
    o = np.argsort(t)
    return np.interp(t_s, t[o], y[o]) * scale


# --------------------------------------------------------------------------- #
# svzerodsolver backend (run_zerod proven wiring: Solver(config); run(); ...)
# --------------------------------------------------------------------------- #
def solve_svzerodsolver(config):
    import pysvzerod

    t0 = time.perf_counter()
    solver = pysvzerod.Solver(config)
    t_build = time.perf_counter() - t0
    solver.run()
    t_run = time.perf_counter() - t0
    df = solver.get_full_result()
    wall = {
        "construct": t_build,
        "solve": t_run - t_build,
        "result": time.perf_counter() - t0 - t_run,
        "total": time.perf_counter() - t0,
    }
    return df, wall


# --------------------------------------------------------------------------- #
# reference backend: scipy ODE integration of the same lumped R/L/C network
# --------------------------------------------------------------------------- #
def run_reference(vessels, nodes, elem, wk, root_names, pieces, t_s):
    """dx/dt = A x + b_const + B Q(t) in mixed units (P: mmHg, Q: mL/s).

    States: node pressures + one inertance flow per sub-segment; vessel wall
    compliance split half/half over the end nodes, Windkessel compliance at the
    terminal nodes, Rp in series with the leaf vessel. Initial condition: the
    steady state at the baseline washout inflow (the tree is pre-perfused at
    Q_b before the protocol starts at t = 0). The forcing is piecewise
    constant (the inflow protocol), so the stiff system is integrated piece by
    piece with scipy.integrate.solve_ivp; BDF -> Radau -> LSODA fallbacks are
    recorded.
    """
    from scipy.integrate import solve_ivp

    names = [v["name"] for v in vessels]
    v_idx = {n: i for i, n in enumerate(names)}
    node_names = list(nodes.keys())
    n_idx = {n: i for i, n in enumerate(node_names)}
    n_n, n_v = len(node_names), len(names)

    c_n = np.zeros(n_n)  # mL/mmHg
    r_v = np.zeros(n_v)  # mmHg s/mL
    l_v = np.zeros(n_v)  # mmHg s^2/mL
    up_v = np.zeros(n_v, dtype=int)
    dn_v = np.zeros(n_v, dtype=int)
    for v in vessels:
        r_si, l_si, c_si, _ = elem[v["name"]]
        i = v_idx[v["name"]]
        r_v[i] = r_si / SI_PER_MMHG_ML
        l_v[i] = l_si / SI_PER_MMHG_ML
        c_mix = c_si * SI_PER_MMHG_ML
        up_v[i], dn_v[i] = n_idx[v["from_node"]], n_idx[v["to_node"]]
        c_n[up_v[i]] += c_mix / 2.0
        c_n[dn_v[i]] += c_mix / 2.0

    rcr_k = {}
    for oname, b in wk.items():
        ni = n_idx[oname]
        c_n[ni] += b["C_mL_mmHg"]
        rcr_k[ni] = (b["Rd_mmHg_s_mL"], P_VENOUS_MMHG)
    for v in vessels:
        if v["to_node"] in wk:
            r_v[v_idx[v["name"]]] += wk[v["to_node"]]["Rp_mmHg_s_mL"]

    n = n_n + n_v
    a_mat = np.zeros((n, n))
    b_const = np.zeros(n)
    for i in range(n_v):
        u, d = int(up_v[i]), int(dn_v[i])
        a_mat[u, n_n + i] -= 1.0 / c_n[u]
        a_mat[d, n_n + i] += 1.0 / c_n[d]
        a_mat[n_n + i, u] += 1.0 / l_v[i]
        a_mat[n_n + i, d] -= 1.0 / l_v[i]
        a_mat[n_n + i, n_n + i] -= r_v[i] / l_v[i]
    for ni, (rd, pd) in rcr_k.items():
        a_mat[ni, ni] -= 1.0 / (rd * c_n[ni])
        b_const[ni] += pd / (rd * c_n[ni])
    root_rows = [n_idx[nd] for nd in root_names]

    def forcing(q_root_mls):
        b = b_const.copy()
        for ni, q in zip(root_rows, q_root_mls):
            b[ni] += q / c_n[ni]
        return b

    def rhs(t, x, b):
        return a_mat @ x + b

    q_base_ic = np.array([pieces[-1]["Q"][nd] for nd in root_names])  # washout split
    x_cur = np.linalg.solve(a_mat, -forcing(q_base_ic))

    x_out = np.zeros((n, t_s.size))
    used = []
    assigned = np.zeros(t_s.size, dtype=bool)
    for p in pieces:
        b = forcing(np.array([p["Q"][nd] for nd in root_names]))
        ta, tb = p["t0"], p["t1"]
        if p is pieces[-1]:
            m = (t_s >= ta) & (t_s <= tb + 1e-12)
        else:
            m = (t_s >= ta) & (t_s < tb)
        m &= ~assigned
        t_eval = t_s[m]
        prepend = t_eval.size == 0 or t_eval[0] > ta + 1e-12
        if prepend:
            t_eval = np.concatenate([[ta], t_eval])
        sol = None
        err = "no integrator attempted"
        for method in ("BDF", "Radau", "LSODA"):
            try:
                sol = solve_ivp(
                    rhs, (ta, tb), x_cur, args=(b,), method=method,
                    t_eval=t_eval, jac=lambda t, x, *a: a_mat,
                    rtol=1e-7, atol=1e-10,
                )
                if sol.success:
                    used.append(method)
                    break
                err = sol.message
                sol = None
            except Exception as exc:  # integrator failure -> recorded fallback
                err = str(exc)
                sol = None
        if sol is None:
            raise SolveError(f"reference ODE integration failed: {err}")
        x_cur = sol.y[:, -1]
        x_out[:, m] = sol.y[:, 1:] if prepend else sol.y
        assigned |= m

    flows = {names[i]: x_out[n_n + i] for i in range(n_v)}
    p_node = {nm: x_out[n_idx[nm]] for nm in list(wk) + list(root_names)}
    meta = {
        "method": ("solve_ivp (mixed units P[mmHg], Q[mL/s], linear R/L/C + RCR "
                   "network, piecewise-constant inflow)"),
        "integrators_used": sorted(set(used)),
        "stiff_fallbacks": sorted({m for m in used if m != "BDF"}),
    }
    return flows, p_node, meta


# --------------------------------------------------------------------------- #
# tree utilities
# --------------------------------------------------------------------------- #
def path_subs(edge_name, vessels, edges_g):
    """Sub-segments root -> distal end of the edge (parent-edge chain)."""
    by_name = {e["name"]: e for e in edges_g}
    by_id = {e["id"]: e for e in edges_g}
    chain = []
    cur = by_name[edge_name]
    while cur is not None:
        chain.append(cur["name"])
        cur = by_id[cur["parent_edge"]] if cur["parent_edge"] is not None else None
    subs = []
    for bname in reversed(chain):
        subs.extend(sorted(
            (v for v in vessels if v["branch"] == bname),
            key=lambda v: v["sub_index"],
        ))
    return subs


def node_topsort(edges_g):
    """Kahn topological order of graph node ids (DAG of edges)."""
    n_in = {}
    succ = {}
    for e in edges_g:
        n_in.setdefault(e["from_node"], 0)
        n_in[e["to_node"]] = n_in.get(e["to_node"], 0) + 1
        succ.setdefault(e["from_node"], []).append(e["to_node"])
    stack = sorted(nid for nid, d in n_in.items() if d == 0)
    order = []
    while stack:
        nid = stack.pop(0)
        order.append(nid)
        for dst in succ.get(nid, []):
            n_in[dst] -= 1
            if n_in[dst] == 0:
                stack.append(dst)
    if len(order) != len(n_in):
        raise SystemExit("run_hemodynamics: graph contains a cycle (not a tree DAG)")
    return order


def project_flows(q_edge, edges_g, topo_ids):
    """Scale junction outflows so sum(q_in) = sum(q_out) node by node (root
    inflows stay prescribed). These are tiny corrections of the solved flows
    (0D wall storage); they make junction mixing exactly mass conserving."""
    q = {k: v.copy() for k, v in q_edge.items()}
    for nid in topo_ids:
        in_edges = [e for e in edges_g if e["to_node"] == nid]
        out_edges = [e for e in edges_g if e["from_node"] == nid]
        if not in_edges or not out_edges:
            continue  # root inflow prescribed / terminal outflow free
        q_in = np.sum([q[e["name"]] for e in in_edges], axis=0)
        q_out = np.sum([q[e["name"]] for e in out_edges], axis=0)
        ok = np.abs(q_out) > Q_EPS_MLS
        scale = np.where(ok, q_in / np.where(ok, q_out, 1.0), 1.0)
        for e in out_edges:
            q[e["name"]] = q[e["name"]] * scale
    return q


# --------------------------------------------------------------------------- #
# 1D contrast transport (finite-volume MUSCL-TVD / Superbee)
# --------------------------------------------------------------------------- #
def superbee(r):
    """Superbee limiter phi(r) = max(0, min(2r, 1), min(r, 2))."""
    return np.maximum(0.0, np.maximum(np.minimum(2.0 * r, 1.0), np.minimum(r, 2.0)))


class EdgeGrid1D:
    """FV grid on the C1 arc discretization of one edge (units mm, s)."""

    def __init__(self, edge):
        s, r = edge_radius_sampler(edge)
        self.h = np.diff(s)  # (K,) cell widths
        self.k = self.h.size
        r_mid = 0.5 * (r[:-1] + r[1:])
        self.area = np.pi * r_mid**2  # (K,) cell areas mm^2
        self.vol = self.area * self.h  # (K,) cell volumes mm^3
        self.face_area = np.pi * r**2  # (K+1,) face areas mm^2
        self.c = np.zeros(self.k)  # cell concentrations
        if self.k > 1:
            ds = 0.5 * (self.h[:-1] + self.h[1:])
            self.g_diff = D_MM2_S * self.face_area[1:-1] / np.maximum(ds, 1e-30)
        else:
            self.g_diff = np.zeros(0)

    def dt_stable(self, q_mm3s):
        """Explicit-Euler stability limit (advection + diffusion)."""
        dt = float("inf")
        if self.k > 1:
            lap = np.zeros(self.k)
            lap[:-1] += self.g_diff
            lap[1:] += self.g_diff
            dt = min(dt, float(np.min(self.vol / np.maximum(lap, 1e-30))))
        aq = abs(q_mm3s)
        if aq > Q_EPS_MLS * MM3_PER_ML:
            dt = min(dt, float(np.min(self.vol / aq)))
        return dt

    def fluxes(self, q_mm3s, c_up):
        """Face fluxes (mm^3/s) with MUSCL-TVD Superbee advection.

        Boundary faces are purely advective (zero diffusive flux, Danckwerts
        outflow); reversals upwind from the adjacent cell (zero-gradient
        external state).
        """
        k = self.k
        c = self.c
        f = np.zeros(k + 1)
        if q_mm3s >= 0:
            f[0] = q_mm3s * c_up
            f[k] = q_mm3s * c[-1]
            if k >= 2:
                d_r = c[1:] - c[:-1]  # donor -> acceptor difference
                d_d = np.empty(k - 1)  # donor upwind difference
                d_d[0] = c[0] - c_up
                d_d[1:] = c[1:-1] - c[:-2]
                den = np.where(np.abs(d_r) > 1e-12, d_r, 1.0)
                r = np.where(np.abs(d_r) > 1e-12, d_d / den, 0.0)
                c_face = c[:-1] + 0.5 * superbee(r) * d_r
                f[1:k] = q_mm3s * c_face - self.g_diff * d_r
        else:
            f[0] = q_mm3s * c[0]
            f[k] = q_mm3s * c_up
            if k >= 2:
                d_l = c[:-1] - c[1:]  # donor(=right) -> acceptor difference
                d_u = np.empty(k - 1)  # donor upwind difference (right side)
                d_u[-1] = c_up - c[-1]
                d_u[:-1] = c[2:] - c[1:-1]
                den = np.where(np.abs(d_l) > 1e-12, d_l, 1.0)
                r = np.where(np.abs(d_l) > 1e-12, d_u / den, 0.0)
                c_face = c[1:] + 0.5 * superbee(r) * d_l
                f[1:k] = q_mm3s * c_face - self.g_diff * (c[1:] - c[:-1])
        return f

    def advance(self, q_mm3s, c_up, dt):
        f = self.fluxes(q_mm3s, c_up)
        self.c += dt * (f[:-1] - f[1:]) / self.vol
        return f


def transport_1d(edges_g, root_names, root_edge, q_proj, q_roots, c_roots, t_s):
    """Explicit FV MUSCL-TVD (Superbee) transport with flow-weighted junction
    mixing and adaptive CFL <= 0.5 sub-cycling between the t_s outputs.

    Flows are held at their left-endpoint values within one output interval so
    the node flow identity (projected flows) holds exactly at every sub-step;
    junction upstream concentrations are frozen from the pre-sub-step state so
    each parent outflux equals the summed child influx. The external face
    fluxes used in the update are the ones accumulated in the mass budget.

    Returns ({edge_name: distal concentration on t_s}, budget dict).
    """
    grids = {e["name"]: EdgeGrid1D(e) for e in edges_g}
    node_parents = {}
    for e in edges_g:
        node_parents.setdefault(e["to_node"], []).append(e["name"])
    terminal_edges = [e["name"] for e in edges_g if not any(
        e2["parent_edge"] == e["id"] for e2 in edges_g)]
    root_edge_name = {e["name"]: n for n, e in root_edge.items()}
    root_idx = {n: j for j, n in enumerate(root_names)}

    n = t_s.size
    arrival = {e["name"]: np.zeros(n) for e in edges_g}
    w_in = 0.0
    w_out = 0.0
    n_steps = 0

    for it in range(n):
        for e in edges_g:
            g = grids[e["name"]]
            arrival[e["name"]][it] = g.c[-1] if g.k >= 1 else 0.0
        if it + 1 >= n:
            break

        q_at = {nm: q_proj[nm][it] for nm in q_proj}  # left-endpoint flows
        span = float(t_s[it + 1] - t_s[it])
        dt_stab = float("inf")
        for e in edges_g:
            q_ref = max(abs(q_at[e["name"]]), abs(q_proj[e["name"]][it + 1]))
            dt_stab = min(dt_stab, grids[e["name"]].dt_stable(q_ref * MM3_PER_ML))
        m_sub = max(1, int(math.ceil(span / (CFL_MAX * dt_stab))))
        dt_sub = span / m_sub

        for _ in range(m_sub):
            # upstream concentrations from the pre-step state (exact discrete
            # conservation: parent outflux == summed child influx at nodes)
            c_up = {}
            for e in edges_g:
                if e["name"] in root_edge_name:
                    c_up[e["name"]] = c_roots[root_idx[root_edge_name[e["name"]]]][it]
                    continue
                parents = node_parents.get(e["from_node"], [])
                wsum = 0.0
                cmix = 0.0
                for pm in parents:
                    qp = q_at[pm]
                    if qp > Q_EPS_MLS:
                        wsum += qp
                        cmix += qp * grids[pm].c[-1]
                if wsum > Q_EPS_MLS:
                    c_up[e["name"]] = cmix / wsum
                elif parents:
                    c_up[e["name"]] = float(np.mean([grids[pm].c[-1] for pm in parents]))
                else:
                    c_up[e["name"]] = 0.0

            for e in edges_g:
                g = grids[e["name"]]
                f = g.advance(q_at[e["name"]] * MM3_PER_ML, c_up[e["name"]], dt_sub)
                if e["name"] in root_edge_name:
                    w_in += dt_sub * f[0]
                if e["name"] in terminal_edges:
                    w_out += dt_sub * f[-1]
                n_steps += 1

    stored = sum(float(np.sum(grids[e["name"]].c * grids[e["name"]].vol))
                 for e in edges_g)
    rel = abs(stored - (w_in - w_out)) / max(w_in, 1e-30)
    budget = {
        "rel_error": rel,
        "method": ("1D finite-volume MUSCL-TVD (Superbee limiter) explicit advection "
                   "+ central diffusion (D=1e-3 mm^2/s), adaptive CFL<=0.5 sub-cycling, "
                   "flow-weighted junction mixing, discrete face-flux mass budget"),
        "injected_mL": w_in / MM3_PER_ML,
        "outlet_mL": w_out / MM3_PER_ML,
        "stored_mL": stored / MM3_PER_ML,
        "n_substeps": n_steps,
    }
    return arrival, budget


# --------------------------------------------------------------------------- #
# Phase 2.5 clinical hemodynamics: Young-Tsai stenosis losses + pressure-driven
# REST / maximal-hyperemia solves (Contract B "clinical" block)
# --------------------------------------------------------------------------- #
def load_lesions_doc(case, args):
    """Contract C lesion selection: --lesions <path> (its `case` must match the
    run) or auto-load out/clinical/lesions/case_<case>_lesions.json (canonical
    spelling first, legacy <case>_lesions.json fallback) when present.

    Returns (doc, path) or (None, None) when nothing is selected."""
    if args.lesions:
        path = args.lesions
        if not os.path.isfile(path):
            raise SystemExit(f"run_hemodynamics: --lesions {path}: missing")
    else:
        path = None
        for cand in (f"out/clinical/lesions/case_{case}_lesions.json",
                     f"out/clinical/lesions/{case}_lesions.json"):
            if os.path.isfile(cand):
                path = cand
                break
        if path is None:
            return None, None
    with open(path) as f:
        doc = json.load(f)
    if str(doc.get("case")) != str(case):
        raise SystemExit(
            f"run_hemodynamics: lesions file {path} carries case "
            f"{doc.get('case')!r} but the run case is {case!r} (Contract C: the "
            f"file's case must match the run)"
        )
    return doc, path


def young_tsai_coefficients(lesion):
    """Young-Tsai (1979) stenosis loss as dP = a*Q + b*Q^2 (Q [mL/s], dP [mmHg]).

    dP = Kv*mu/r0^2*u*L + Kt*rho/2*(A0/As - 1)^2*u^2 with u = Q/As the throat
    velocity, Kv = 32*(L/D0)*(A0/As)^2, Kt = 1.52, L = the lesion length and
    A0 = pi*r_ref^2 at the lesion centroid (r_ref = d_ref_mm/2). The throat
    area As = A0/A0_over_As with A0_over_As the Contract-A field
    (= 1/(1 - as_pct/100), area stenosis). Evaluated in SI first (mu = 0.0035
    Pa s, rho = 1050 kg/m^3 for the inertial term), converted to mmHg and mL/s
    (1 mmHg = 133.322368 Pa)."""
    r0 = 0.5 * float(lesion["d_ref_mm"]) * 1e-3
    a0 = math.pi * r0**2
    ratio = lesion.get("A0_over_As")
    ratio = (1.0 / (1.0 - float(lesion["as_pct"]) / 100.0)
             if ratio is None else float(ratio))
    a_s = a0 / ratio  # throat area As = A0 / A0_over_As [m^2]
    l_m = float(lesion["length_mm"]) * 1e-3
    kv = 32.0 * (l_m / (2.0 * r0)) * ratio**2
    # u = Q/As -> dP = (Kv*mu*L/r0^2/As)*Q + (Kt*rho/2*(A0/As-1)^2/As^2)*Q^2
    a_si = kv * MU_YOUNG_TSAI * l_m / r0**2 / a_s  # Pa s/m^3
    b_si = (KT_YOUNG_TSAI * RHO_YOUNG_TSAI / 2.0
            * (ratio - 1.0) ** 2 / a_s**2)  # Pa s^2/m^6
    return {
        "kv": float(kv),
        "kt": KT_YOUNG_TSAI,
        "a0_m2": a0,
        "as_m2": a_s,
        "a0_over_as": ratio,
        "a_si_Pa_s_m3": a_si,
        "b_si_Pa_s2_m6": b_si,
        "a_mmHg_s_mL": a_si / SI_PER_MMHG_ML,
        "b_mmHg_s2_mL2": b_si * M3_PER_ML**2 / MMHG,
    }


def anchor_lesions(lesions, vessels, edges_g):
    """Attach each lesion's series loss to the 0D sub-segment spanned by the
    lesion centroid s_mm (its proximal/distal sub-segment nodes): the
    sub-segment chain is in series, so folding (a, b) into that one element is
    exactly a series Young-Tsai loss between those nodes."""
    edge_of = {e["name"]: e for e in edges_g}
    by_branch = {}
    for v in vessels:
        by_branch.setdefault(v["branch"], []).append(v)
    for b in by_branch:
        by_branch[b].sort(key=lambda v: v["sub_index"])
    anchored = {}
    records = {}
    for les in lesions:
        en = les.get("edge_name") or les.get("branch_id")
        if en not in edge_of:
            raise SystemExit(
                f"run_hemodynamics: lesion {les.get('lesion_id')}: "
                f"unknown edge {en!r}"
            )
        subs = by_branch[en]
        l_mm = float(edge_of[en]["length_mm"])
        s_mm = float(les["s_mm"])
        k = (min(len(subs) - 1, max(0, int(s_mm / l_mm * len(subs))))
             if l_mm > 0 else 0)
        coef = young_tsai_coefficients(les)
        anchored.setdefault(subs[k]["name"], []).append(coef)
        records[str(les["lesion_id"])] = {
            "lesion": les, "coef": coef, "vessel": subs[k]["name"], "edge": en,
        }
    return anchored, records


def root_to_tip_paths(edges_g):
    """Root -> tip edge chains (lists of edge records), longest first."""
    by_id = {e["id"]: e for e in edges_g}
    tips = [e for e in edges_g
            if not any(c["parent_edge"] == e["id"] for c in edges_g)]
    paths = []
    for tip in tips:
        chain = []
        cur = tip
        while cur is not None:
            chain.append(cur)
            cur = by_id[cur["parent_edge"]] if cur["parent_edge"] is not None else None
        chain.reverse()
        paths.append(chain)
    paths.sort(key=lambda ch: -sum(e["length_mm"] for e in ch))
    return paths


def edge_boundaries(edges_g, vessels):
    """{(s_mm, node_name)} per edge: s = 0 proximal node, then each
    sub-segment distal node at its arc position."""
    by_branch = {}
    for v in vessels:
        by_branch.setdefault(v["branch"], []).append(v)
    out = {}
    for e in edges_g:
        subs = sorted(by_branch[e["name"]], key=lambda v: v["sub_index"])
        n = len(subs)
        l_mm = float(e["length_mm"])
        pts = [(0.0, subs[0]["from_node"])]
        pts += [((k + 1) * l_mm / n, subs[k]["to_node"]) for k in range(n)]
        out[e["name"]] = pts
    return out


def sample_distal_pd(les_rec, edge_of, child_edges, bounds, q_edge):
    """Per-lesion Pd sample node >= 20 mm distal of the lesion distal end
    (s_mm + length_mm/2), continuing along the dominant-flow child path past a
    branch end. Returns (node, pd_location_mm, edge_name, s_on_edge_mm,
    distal_of_lesion_end_mm) with pd_location_mm the sample position measured
    from the lesion edge's proximal end (the same branch-distance convention
    as s_mm), continued over child edges."""
    les = les_rec["lesion"]
    target = (float(les["s_mm"]) + 0.5 * float(les["length_mm"]) + PD_DISTAL_MM)
    end = target - PD_DISTAL_MM
    e = edge_of[les_rec["edge"]]
    base = 0.0
    seen = set()
    while True:
        seen.add(e["name"])
        l_mm = float(e["length_mm"])
        for s1, node in bounds[e["name"]]:
            if base + s1 >= target - 1e-9:
                return node, base + s1, e["name"], s1, base + s1 - end
        base += l_mm
        kids = [c for c in child_edges.get(e["id"], [])
                if c["name"] not in seen]
        if not kids:  # path ends short of the target: deepest node reached
            s1, node = bounds[e["name"]][-1]
            return node, base - l_mm + s1, e["name"], s1, base - l_mm + s1 - end
        e = max(kids, key=lambda c: abs(q_edge.get(c["name"], 0.0)))


def wk_split(wk, rp_fraction):
    """Terminal DC view of the 3-element Windkessels at a given Rp/Rd split
    (R_total preserved; C is an open circuit at steady state)."""
    out = {}
    for name, w in wk.items():
        r_tot = w["R_total_mmHg_s_mL"]
        out[name] = {
            "Rp_mmHg_s_mL": rp_fraction * r_tot,
            "Rd_mmHg_s_mL": (1.0 - rp_fraction) * r_tot,
        }
    return out


def solve_clinical_steady(vessels, nodes, elem, wrp, anchored, rd_scale,
                          pa_mmHg):
    """Steady pressure-driven solve of the 0D network with each lesion's
    Young-Tsai loss coupled self-consistently (Picard iteration on Q).

    The d/dt = 0 limit of the same R/L/C model: inertance and wall compliance
    vanish, each sub-segment is its Poiseuille resistance, each anchored lesion
    adds dP = a*Q + b*Q^2 in series between its proximal/distal sub-segment
    nodes, each terminal is its 3-element Windkessel in DC (Rp + rd_scale*Rd
    to Pd), each root is driven at pa_mmHg. The quadratic term is linearized as
    b*|Q_k|*Q and iterated (damped Picard) to residual < PICARD_TOL_MLS mL/s.

    Returns {"p": node->mmHg, "q": vessel->mL/s, "residual", "iterations"}."""
    names = list(nodes)
    idx = {n: i for i, n in enumerate(names)}
    n = len(names)
    els = []  # (i, j, a_lin [mmHg s/mL], b_quad [mmHg s^2/mL^2], vessel name)
    for v in vessels:
        r_si, _, _, _ = elem[v["name"]]
        a_lin = r_si / SI_PER_MMHG_ML
        b_quad = 0.0
        for coef in anchored.get(v["name"], ()):
            a_lin += coef["a_mmHg_s_mL"]
            b_quad += coef["b_mmHg_s2_mL2"]
        els.append((idx[v["from_node"]], idx[v["to_node"]], a_lin, b_quad,
                    v["name"]))
    term = [(idx[oname], w["Rp_mmHg_s_mL"] + rd_scale * w["Rd_mmHg_s_mL"])
            for oname, w in sorted(wrp.items())]
    roots = [idx[nm] for nm in names if nodes[nm]["kind"] == "inlet"]

    def assemble(q_prev):
        G = np.zeros((n, n))
        rhs = np.zeros(n)
        for k, (i, j, a_lin, b_quad, _) in enumerate(els):
            g = 1.0 / (a_lin + b_quad * abs(q_prev[k]))
            G[i, i] += g
            G[j, j] += g
            G[i, j] -= g
            G[j, i] -= g
        for ni, rt in term:
            G[ni, ni] += 1.0 / rt
            rhs[ni] += P_VENOUS_MMHG / rt
        for ni in roots:
            G[ni, :] = 0.0
            G[ni, ni] = 1.0
            rhs[ni] = pa_mmHg
        return G, rhs

    q = np.zeros(len(els))
    lam = 1.0
    prev_res = None
    res = float("inf")
    for it in range(1, PICARD_MAX_ITER + 1):
        G, rhs = assemble(q)
        p = np.linalg.solve(G, rhs)
        q_star = np.array([
            (p[i] - p[j]) / (a_lin + b_quad * abs(q[k]))
            for k, (i, j, a_lin, b_quad, _) in enumerate(els)
        ])
        q_new = q + lam * (q_star - q)
        res = float(np.max(np.abs(q_new - q))) if els else 0.0
        q = q_new
        if res < PICARD_TOL_MLS:
            break
        if prev_res is not None and res > 0.5 * prev_res and lam == 1.0:
            lam = 0.5  # quadratic dominance -> damped Picard
        prev_res = res
    else:
        raise SolveError(
            f"clinical Picard iteration stalled at residual {res:.3g} mL/s"
        )
    G, rhs = assemble(q)  # pressures consistent with the converged flows
    p = np.linalg.solve(G, rhs)
    return {
        "p": {nm: float(p[idx[nm]]) for nm in names},
        "q": {els[k][4]: float(q[k]) for k in range(len(els))},
        "residual": float(res),
        "iterations": it,
    }


def build_clinical_svzero_config(vessels, nodes, elem, wrp, anchored, rd_scale,
                                 pa_mmHg, period_s):
    """pysvzerod clinical model: closest native element mapping of the
    Young-Tsai loss (BloodVessel.stenosis_coefficient = Kt quadratic term,
    R_poiseuille += Kv viscous term), constant PRESSURE roots at pa_mmHg and
    RESISTANCE terminals equal to the Windkessel DC (Rp + rd_scale*Rd)."""
    v_by_name = {v["name"]: v for v in vessels}
    solver_vessels = []
    for v in vessels:
        r_el, l_el, c_el, _ = elem[v["name"]]
        coefs = anchored.get(v["name"], ())
        entry = {
            "vessel_id": v["vessel_id"],
            "vessel_name": v["name"],
            "vessel_length": v["length_m"],
            "zero_d_element_type": "BloodVessel",
            "zero_d_element_values": {
                "R_poiseuille": r_el + sum(c["a_si_Pa_s_m3"] for c in coefs),
                "C": c_el,
                "L": l_el,
                "stenosis_coefficient": sum(c["b_si_Pa_s2_m6"] for c in coefs),
            },
        }
        bc = {}
        if nodes[v["from_node"]]["kind"] == "inlet":
            bc["inlet"] = f"IN_{v['from_node']}"
        if nodes[v["to_node"]]["kind"] == "outlet":
            bc["outlet"] = f"CLIN_{v['to_node']}"
        if bc:
            entry["boundary_conditions"] = bc
        solver_vessels.append(entry)

    junctions = []
    for name, nd in nodes.items():
        if nd["kind"] in ("inlet", "outlet"):
            continue
        junctions.append(
            {
                "junction_name": f"JN_{name}",
                "junction_type": "NORMAL_JUNCTION",
                "inlet_vessels": [v_by_name[x]["vessel_id"] for x in nd["in_vessels"]],
                "outlet_vessels": [v_by_name[x]["vessel_id"] for x in nd["out_vessels"]],
            }
        )

    bcs = []
    p_pa = pa_mmHg * MMHG
    for rname in sorted(nm for nm in nodes if nodes[nm]["kind"] == "inlet"):
        bcs.append({"bc_name": f"IN_{rname}", "bc_type": "PRESSURE",
                    "bc_values": {"P": [p_pa, p_pa], "t": [0.0, period_s]}})
    for oname in sorted(wrp):
        w = wrp[oname]
        r_si = (w["Rp_mmHg_s_mL"] + rd_scale * w["Rd_mmHg_s_mL"]) * SI_PER_MMHG_ML
        bcs.append({"bc_name": f"CLIN_{oname}", "bc_type": "RESISTANCE",
                    "bc_values": {"R": r_si, "Pd": P_VENOUS_MMHG * MMHG}})

    return {
        "simulation_parameters": {
            "number_of_cardiac_cycles": 1,
            "number_of_time_pts_per_cardiac_cycle": 512,
            "steady_initial": True,
            "output_variable_based": True,
            "output_all_cycles": False,
            "output_interval": 1,
            "cardiac_period": float(period_s),
            "absolute_tolerance": 1e-10,
        },
        "vessels": solver_vessels,
        "junctions": junctions,
        "boundary_conditions": bcs,
    }


def clinical_svzero_crosscheck(vessels, nodes, elem, wk, wrp, anchored,
                               rd_scale, state):
    """Run the same steady network through pysvzerod (constant BCs over one
    settled window) and compare its settled means with the Picard solution."""
    subs_by_edge = {}
    for v in vessels:
        subs_by_edge.setdefault(v["branch"], []).append(v)
    for b in subs_by_edge:
        subs_by_edge[b].sort(key=lambda v: v["sub_index"])

    cfg = build_clinical_svzero_config(vessels, nodes, elem, wrp, anchored,
                                       rd_scale, P_AORTA_MMHG, CLIN_PERIOD_S)
    df, wall = solve_svzerodsolver(cfg)
    dofs = parse_result_df(df)

    def settled_mean(dof):
        t, y = pick_dof(dofs, dof)
        m = t >= 0.5 * CLIN_PERIOD_S
        return float(np.mean(y[m]))

    diffs = {}
    q_in = 0.0
    for oname in sorted(wk):
        leaf = subs_by_edge[wk[oname]["outlet"]][-1]["name"]
        face = settled_mean(f"pressure:{leaf}:CLIN_{oname}") / MMHG  # Pa -> mmHg
        diffs[oname] = face - state["p"][oname]
    for nm in sorted(n for n in nodes if nodes[n]["kind"] == "inlet"):
        first = nodes[nm]["out_vessels"][0]
        q_in += settled_mean(f"flow:IN_{nm}:{first}") * 1e6  # m^3/s -> mL/s
    q_out = sum(
        (state["p"][oname] - P_VENOUS_MMHG)
        / (wrp[oname]["Rp_mmHg_s_mL"] + rd_scale * wrp[oname]["Rd_mmHg_s_mL"])
        for oname in wrp
    )
    return {
        "method": ("pysvzerod steady window (constant PRESSURE roots / "
                   "RESISTANCE = Windkessel DC terminals), settled means over "
                   "the second half of the window vs the Picard solution"),
        "wall_time_s": round(wall["total"], 4),
        "max_abs_outlet_pressure_diff_mmHg": round(
            max(abs(d) for d in diffs.values()), 6),
        "abs_total_inflow_diff_mLs": round(abs(q_in - q_out), 9),
    }


def edge_mean_area(edges_g):
    """Mean lumen area A_mean = (1/L)*integral(pi r^2 ds) per edge [m^2]."""
    out = {}
    for e in edges_g:
        s, rad = edge_radius_sampler(e)
        l_mm = float(e["length_mm"])
        if s.size > 1 and l_mm > 0:
            v_mm3 = float(np.trapezoid(np.pi * rad**2, s))
        else:
            v_mm3 = math.pi * float(rad[0]) ** 2 * l_mm
        out[e["name"]] = (v_mm3 / l_mm) * 1e-6 if l_mm > 0 else 1e-12
    return out


def build_clinical(vessels, nodes, elem, wk, edges_g, lesions_doc,
                   lesions_path):
    """Contract B "clinical" block: pressure-driven REST and maximal-hyperemia
    solves (Pa = 90 mmHg, terminals = the existing 3-element Windkessels with
    Rd -> 0.25*Rd_rest under hyperemia) with Young-Tsai lesion losses, the
    per-edge/per-lesion read-outs and the resting-trunk velocity calibration."""
    anchored, les_recs = anchor_lesions(
        (lesions_doc or {}).get("lesions") or [], vessels, edges_g)
    edge_of = {e["name"]: e for e in edges_g}
    subs_by_edge = {}
    for v in vessels:
        subs_by_edge.setdefault(v["branch"], []).append(v)
    for b in subs_by_edge:
        subs_by_edge[b].sort(key=lambda v: v["sub_index"])
    bounds = edge_boundaries(edges_g, vessels)
    child_edges = {}
    for e in edges_g:
        if e["parent_edge"] is not None:
            child_edges.setdefault(e["parent_edge"], []).append(e)

    def terminal_total(st):
        return sum(st["q"][v["name"]] for v in vessels
                   if nodes[v["to_node"]]["kind"] == "outlet")

    def solve_states(rp_fraction):
        wrp = wk_split(wk, rp_fraction)
        rest = solve_clinical_steady(vessels, nodes, elem, wrp, anchored,
                                     1.0, P_AORTA_MMHG)
        hyper = solve_clinical_steady(vessels, nodes, elem, wrp, anchored,
                                      HYPER_RD_SCALE, P_AORTA_MMHG)
        return wrp, rest, hyper

    rp_fraction = RP_FRACTION
    adjustments = []
    wrp, rest, hyper = solve_states(rp_fraction)
    q_rest, q_hyper = terminal_total(rest), terminal_total(hyper)
    ratio = q_hyper / q_rest
    if not (FLOW_INCREASE_BAND[0] <= ratio <= FLOW_INCREASE_BAND[1]):
        # adjust the terminal Rp/Rd split (microvascular Rd ~85-90% of
        # R_total) until flow_increase_x lands in the accepted band
        adjustments.append({
            "rp_fraction": RP_FRACTION,
            "rd_share_of_R_total": 1.0 - RP_FRACTION,
            "flow_increase_x": round(ratio, 6),
            "accepted": False,
            "reason": ("realized hyper.flow_increase_x outside [2.5, 4.0] at the "
                       "default RP_FRACTION"),
        })
        cands = ([0.075, 0.05, 0.025] if ratio < FLOW_INCREASE_BAND[0]
                 else [0.125, 0.15, 0.20])
        for cand in cands:
            wrp_c, rest_c, hyper_c = solve_states(cand)
            q_r, q_h = terminal_total(rest_c), terminal_total(hyper_c)
            ratio_c = q_h / q_r
            adjustments.append({
                "rp_fraction": cand,
                "rd_share_of_R_total": 1.0 - cand,
                "flow_increase_x": round(ratio_c, 6),
                "accepted": FLOW_INCREASE_BAND[0] <= ratio_c <= FLOW_INCREASE_BAND[1],
            })
            if adjustments[-1]["accepted"]:
                rp_fraction, wrp, rest, hyper = cand, wrp_c, rest_c, hyper_c
                q_rest, q_hyper, ratio = q_r, q_h, ratio_c
                break

    a_mean = edge_mean_area(edges_g)
    q_edge = {}
    branches = {}
    for tag, st, hyper_flag in (("rest", rest, False), ("hyper", hyper, True)):
        bmap = {}
        qmap = {}
        for e in edges_g:
            subs = subs_by_edge[e["name"]]
            q_e = float(np.mean([st["q"][v["name"]] for v in subs]))
            p_prox = st["p"][subs[0]["from_node"]]
            p_dist = st["p"][subs[-1]["to_node"]]
            u = q_e * M3_PER_ML / a_mean[e["name"]]
            qmap[e["name"]] = q_e
            rec = {
                "p_prox_mmHg": round(p_prox, 4),
                "p_dist_mmHg": round(p_dist, 4),
                "q_ml_s": round(q_e, 6),
                "u_m_s": round(u, 6),
            }
            if hyper_flag:
                rec["vffr"] = round(p_dist / P_AORTA_MMHG, 6)
            else:
                rec["pd_pa"] = round(p_dist / P_AORTA_MMHG, 6)
            bmap[e["name"]] = rec
        branches[tag] = bmap
        q_edge[tag] = qmap

    les_block = {}
    for lid, rec in sorted(les_recs.items()):
        les, coef = rec["lesion"], rec["coef"]
        q_r = rest["q"][rec["vessel"]]
        q_h = hyper["q"][rec["vessel"]]
        a, b = coef["a_mmHg_s_mL"], coef["b_mmHg_s2_mL2"]
        node, loc, pd_edge, s_on, distal = sample_distal_pd(
            rec, edge_of, child_edges, bounds, q_edge["hyper"])
        les_block[lid] = {
            "dp_rest_mmHg": a * q_r + b * q_r * q_r,
            "dp_hyper_mmHg": a * q_h + b * q_h * q_h,
            "kv": round(coef["kv"], 4),
            "kt": coef["kt"],
            "u_throat_m_s": round(q_h * M3_PER_ML / coef["as_m2"], 6),
            "vffr": round(hyper["p"][node] / P_AORTA_MMHG, 6),
            "pd_location_mm": round(loc, 3),
            # additive detail keys
            "edge_name": rec["edge"],
            "u_throat_rest_m_s": round(q_r * M3_PER_ML / coef["as_m2"], 6),
            "u_throat_hyper_m_s": round(q_h * M3_PER_ML / coef["as_m2"], 6),
            "q_rest_ml_s": round(q_r, 6),
            "q_hyper_ml_s": round(q_h, 6),
            "pd_edge": pd_edge,
            "pd_sample_s_mm": round(s_on, 3),
            "pd_distal_of_lesion_end_mm": round(distal, 3),
        }

    # ---- velocity calibration (resting major-trunk benchmark 15-30 cm/s) ----
    u_raw = {e["name"]: q_edge["rest"][e["name"]] * M3_PER_ML / a_mean[e["name"]]
             for e in edges_g}
    trunks = [e["name"] for e in edges_g
              if float(e["r_in_mm"]) >= TRUNK_R_IN_MM]
    trunk_method = "edges with r_in_mm >= 0.75 mm"
    if len(trunks) < 3:
        prox = []
        for ch in root_to_tip_paths(edges_g)[:3]:
            total = sum(e["length_mm"] for e in ch)
            acc = 0.0
            for e in ch:
                if acc >= total / 3.0:
                    break
                prox.append(e["name"])
                acc += e["length_mm"]
        trunks = sorted(set(prox))
        trunk_method = ("fewer than 3 edges with r_in_mm >= 0.75 mm; trunks = "
                        "the proximal third of the 3 longest root-to-tip paths")
    trunk_u = [u_raw[t] for t in trunks if u_raw[t] > Q_EPS_MLS]
    kappa = (float(np.median([TARGET_U_TRUNK_MS / u for u in trunk_u]))
             if trunk_u else 1.0)
    transit_raw_s = {
        e["name"]: (float(e["length_mm"]) * 1e-3 / u_raw[e["name"]]
                    if u_raw[e["name"]] > Q_EPS_MLS else None)
        for e in edges_g
    }
    transit_cal_s = {
        k: (v / kappa if v is not None else None)
        for k, v in transit_raw_s.items()
    }
    primary_paths = {}
    for i, ch in enumerate(root_to_tip_paths(edges_g)[:3]):
        t_raw = sum(transit_raw_s[e["name"]] for e in ch
                    if transit_raw_s[e["name"]] is not None)
        primary_paths[f"path_{i}"] = {
            "tip_edge": ch[-1]["name"],
            "edges": [e["name"] for e in ch],
            "length_mm": round(sum(e["length_mm"] for e in ch), 3),
            "transit_raw_s": round(t_raw, 6),
            "transit_calibrated_s": round(t_raw / kappa, 6),
        }
    vel_cal = {
        "target_u_trunk_m_s": TARGET_U_TRUNK_MS,
        "u_trunk_band_m_s": list(U_TRUNK_BAND_MS),
        "trunk_edges": trunks,
        "kappa": kappa,
        "u_cal_trunk_median_m_s": (round(float(np.median(
            [kappa * u_raw[t] for t in trunks if u_raw[t] > Q_EPS_MLS])), 6)
            if trunk_u else None),
        "u_cal_trunk_band_ok": (None if not trunk_u else all(
            U_TRUNK_BAND_MS[0] <= kappa * u <= U_TRUNK_BAND_MS[1]
            for u in trunk_u)),
        "u_raw_m_s": {k: round(v, 6) for k, v in u_raw.items()},
        "u_cal_m_s": {k: round(kappa * v, 6) for k, v in u_raw.items()},
        "transit_raw_s": {k: (round(v, 6) if v is not None else None)
                          for k, v in transit_raw_s.items()},
        "transit_calibrated_s": {k: (round(v, 6) if v is not None else None)
                                 for k, v in transit_cal_s.items()},
        "primary_paths": primary_paths,
        "method": (
            "rest-state edge velocities u_raw = Q/A_mean (A_mean = (1/L)*"
            "integral(pi r^2 ds) over the edge arc); kappa = median over trunks "
            f"of (0.22 m/s / u_raw) [{trunk_method}]; u_cal = kappa*u_raw; "
            "per-edge convective transit t = L/u raw and L/(kappa*u) "
            "calibrated"),
    }

    # ---- honest solver record + pysvzerod element cross-check --------------
    xchecks = {}
    for tag, st, rds in (("rest", rest, 1.0), ("hyper", hyper, HYPER_RD_SCALE)):
        try:
            xchecks[tag] = clinical_svzero_crosscheck(
                vessels, nodes, elem, wk, wrp, anchored, rds, st)
        except Exception as exc:  # recorded, never fabricated
            xchecks[tag] = {"status": "error", "error": str(exc)[:400]}
    backend = (
        "steady pressure-driven 0D network solve (reference nodal solver, "
        f"Picard iteration on Q coupling each lesion's Young-Tsai dP = a*Q + "
        f"b*Q^2 series loss to residual < {PICARD_TOL_MLS:g} mL/s); terminals = "
        "3-element Windkessels in DC (Rp + rd_scale*Rd, C open), roots at Pa. "
        "pysvzerod has no native Kv/Kt stenosis/valve element (no "
        "pysvzerod.solver module; BloodVessel.stenosis_coefficient is a pure "
        "quadratic Ks*Q^2 verified numerically and ValveTanh/PiecewiseValve are "
        "closed-loop heart blocks): the closest native element mapping "
        "(stenosis_coefficient = Kt quadratic term, R_poiseuille += Kv viscous "
        "term) is run through pysvzerod as a cross-check "
        "(see solver.pysvzerod.cross_check)"
    )
    solver_info = {
        "backend": backend,
        "residual": max(rest["residual"], hyper["residual"]),
        "rho_young_tsai": RHO_YOUNG_TSAI,
        "mu_young_tsai_Pa_s": MU_YOUNG_TSAI,
        "kt": KT_YOUNG_TSAI,
        "picard": {
            "scheme": ("dP = a*Q + b*|Q_k|*Q Picard on Q, damping 0.5 on "
                       "stall, residual = max |dQ| between iterates"),
            "tolerance_mLs": PICARD_TOL_MLS,
            "residual_rest_mLs": rest["residual"],
            "residual_hyper_mLs": hyper["residual"],
            "iterations_rest": rest["iterations"],
            "iterations_hyper": hyper["iterations"],
        },
        "rp_fraction_used": rp_fraction,
        "rp_fraction_default": RP_FRACTION,
        "rd_share_of_R_total": 1.0 - rp_fraction,
        "rp_rd_adjustments": adjustments,
        "lesions_file": lesions_path,
        "lesion_placement": (
            "each lesion's Young-Tsai loss is a series dP = a*Q + b*Q^2 between "
            "the proximal/distal nodes of the 0D sub-segment containing the "
            "lesion centroid s_mm (recorded per lesion in solver-independent "
            "units via kv/kt/u_throat_m_s)"),
        "pd_sampling": (
            "per-lesion Pd sampled at the first sub-segment node >= 20 mm "
            "distal of the lesion distal end (s_mm + length_mm/2), continuing "
            "along the dominant-hyperemia-flow child path past a branch end; "
            "pd_location_mm is that sample's position in mm along the path "
            "measured from the lesion edge's proximal end (s_mm convention)"),
        "pysvzerod": {
            "version": dist_version("pysvzerod"),
            "element_mapping": (
                "BloodVessel.stenosis_coefficient = Kt*rho/2*(A0/As-1)^2/As^2 "
                "(dP = Ks*Q^2, verified numerically), R_poiseuille += "
                "Kv*mu*L/(r0^2*As) (Young-Tsai viscous term)"),
            "cross_check": xchecks,
        },
    }

    return {
        "pa_mmHg": P_AORTA_MMHG,
        "q_rest_ml_min": round(q_rest * 60.0, 4),
        "rest": {
            "q_total_ml_min": round(q_rest * 60.0, 4),
            "branches": branches["rest"],
        },
        "hyper": {
            "rd_scale": HYPER_RD_SCALE,
            "q_total_ml_min": round(q_hyper * 60.0, 4),
            "flow_increase_x": round(ratio, 6),
            "branches": branches["hyper"],
        },
        "lesions": les_block,
        "velocity_calibration": vel_cal,
        "solver": solver_info,
    }


# --------------------------------------------------------------------------- #
# per-case driver
# --------------------------------------------------------------------------- #
def make_signal_grid(total_s):
    n = int(math.ceil(total_s / DT_MAX_S)) + 1
    return np.linspace(0.0, total_s, n)


def upcrossing(t, c):
    """10% upcrossing of the curve's own peak (sub-sample linear) + peak time."""
    peak = float(np.max(c)) if c.size else 0.0
    if peak <= 1e-9:
        return None, None
    thr = 0.1 * peak
    i = int(np.argmax(c >= thr))
    if i == 0:
        t_arr = float(t[0])
    else:
        c0, c1 = float(c[i - 1]), float(c[i])
        frac = (thr - c0) / (c1 - c0) if c1 > c0 else 0.0
        t_arr = float(t[i - 1] + frac * (t[i] - t[i - 1]))
    return t_arr, float(t[int(np.argmax(c))])


def run_case(case, args):
    t_case = time.perf_counter()
    graph_path = f"{args.graph_dir}/{case}_graph.json"
    graph = load_graph(graph_path)
    if not graph.get("edges") or not graph.get("nodes"):
        raise SystemExit(
            f"run_hemodynamics: case {case}: empty graph "
            f"({len(graph.get('nodes', []))} nodes, "
            f"{len(graph.get('edges', []))} edges) in {graph_path}"
        )
    vessels, nodes, inlets, outlets, edges_g = build_model(graph)
    if not inlets or not outlets:
        raise SystemExit(
            f"run_hemodynamics: case {case}: graph has no roots/terminals "
            f"({len(inlets)} roots, {len(outlets)} terminals)"
        )
    elem_args = SimpleNamespace(rho=RHO_KG_M3, mu=MU_PA_S, r_min_m=R_MIN_M,
                                wall_k=WALL_K_PA)
    elem = {v["name"]: vessel_elements(v, elem_args) for v in vessels}
    wk = size_windkessel(outlets)
    topo_ids = node_topsort(edges_g)

    root_names = sorted(inlets)
    node_id_of_name = {}
    for nd in graph["nodes"]:
        node_id_of_name[nd["name"]] = nd["id"]
    root_edge = {}
    for n in root_names:
        nid = node_id_of_name[n]
        cand = [e for e in edges_g if e["from_node"] == nid]
        if len(cand) != 1:
            raise SystemExit(
                f"run_hemodynamics: case {case}: root {n} has {len(cand)} out-edges"
            )
        root_edge[n] = cand[0]

    subs_by_edge = {}
    for v in vessels:
        subs_by_edge.setdefault(v["branch"], []).append(v)
    for b in subs_by_edge:
        subs_by_edge[b].sort(key=lambda v: v["sub_index"])
    rows, _, _ = dof_map(vessels, nodes, inlets, outlets, wk, elem)
    row_of = {r["vessel_name"]: r for r in rows}

    # ---------------- Phase 2.5 clinical block (Contract B) ----------------
    lesions_doc, lesions_path = load_lesions_doc(case, args)
    if lesions_path is not None:
        print(f"[run_hemodynamics] case {case}: lesions <- {lesions_path} "
              f"({len((lesions_doc or {}).get('lesions') or [])} lesions)",
              flush=True)
    clinical = build_clinical(vessels, nodes, elem, wk, edges_g,
                              lesions_doc, lesions_path)
    kappa_cal = clinical["velocity_calibration"]["kappa"]

    def ref_outlet_tip(p_node_ref, flows_ref):
        """Vessel-tip (pre-Rp) pressure series per outlet.

        The reference lumps Rp into the leaf vessel (run_zerod form), so its
        terminal node is the post-Rp Windkessel cap pressure; svZeroDSolver's
        RCR attachment node is the pre-Rp tip. Add the Rp drop back so both
        backends report the same physical point (vessel tip).
        """
        out = {}
        for oname in wk:
            last = subs_by_edge[wk[oname]["outlet"]][-1]["name"]
            out[oname] = (p_node_ref[oname]
                          + flows_ref[last] * wk[oname]["Rp_mmHg_s_mL"])
        return out

    case_dir = f"{args.out_dir}/{case}"
    os.makedirs(case_dir, exist_ok=True)

    signals = {}
    mass_balance = {}
    timings = {"solve_s": 0.0, "transport_1d_s": 0.0, "per_profile": {}}
    fallbacks = []
    backend_used = {}
    cross = {}
    config_a = None

    for pid, prof in PROFILES.items():
        pieces, _, _ = profile_schedule(prof, args.inflow_mode,
                                        args.injection_root, inlets)
        total_s = pieces[-1]["t1"]
        t_s = make_signal_grid(total_s)
        q_roots, c_roots = eval_root_series(pieces, root_names, t_s)
        q_total = q_roots.sum(axis=0)
        q_safe = np.where(q_total > Q_EPS_MLS, q_total, 1.0)
        c_in = np.where(q_total > Q_EPS_MLS,
                        np.sum(q_roots * c_roots, axis=0) / q_safe, 0.0)
        alpha = q_total / Q_BASE_MLS

        root_waves = {n: step_waveform(pieces, n, M3_PER_ML) for n in root_names}
        n_pts = max(4096, int(math.ceil(total_s / DT_MAX_S)))
        config = build_svzero_config(vessels, nodes, elem, wk, root_waves,
                                     total_s, n_pts)
        if pid == "A":
            config_a = config

        # ---------------- 0D solve (headline / fallback / cross-check) ------
        t_mean = float(t_s[-1] - t_s[0])
        q_bar_roots = [
            float(np.trapezoid(q_roots[j], t_s) / t_mean) for j in range(len(root_names))
        ]
        t_solve0 = time.perf_counter()
        q_sub = p_term = solve_meta = None
        want_sv = args.solver in ("svzerodsolver", "both")
        want_ref = args.solver in ("reference", "both")
        if want_sv:
            try:
                df, wall = solve_svzerodsolver(config)
                dofs = parse_result_df(df)
                q_sub = {
                    v["name"]: interp_dof(dofs, row_of[v["name"]]["inlet_flow_dof"],
                                          t_s, 1e6)  # m^3/s -> mL/s
                    for v in vessels
                }
                p_term = {
                    oname: float(np.mean(interp_dof(
                        dofs,
                        row_of[subs_by_edge[wk[oname]["outlet"]][-1]["name"]]
                        ["outlet_pressure_dof"],
                        t_s, 1.0 / MMHG)))
                    for oname in wk
                }
                p_root = {
                    n: float(np.mean(interp_dof(
                        dofs,
                        row_of[subs_by_edge[root_edge[n]["name"]][0]["name"]]
                        ["inlet_pressure_dof"],
                        t_s, 1.0 / MMHG)))
                    for n in root_names
                }
                for arr in q_sub.values():
                    if not np.all(np.isfinite(arr)):
                        raise SolveError("non-finite solver flow series")
                solve_meta = {"backend": "svzerodsolver (pysvzerod 2.0)",
                              "wall_time_s": wall}
                backend_used[pid] = "svzerodsolver"
            except Exception as exc:  # recorded fallback to the reference ODE
                fallbacks.append({"profile": pid, "from": "svzerodsolver",
                                  "to": "reference", "reason": str(exc)[:400]})
        ref_meta = None
        if want_ref or q_sub is None:
            t_r0 = time.perf_counter()
            flows_ref, p_node_ref, ref_meta = run_reference(
                vessels, nodes, elem, wk, root_names, pieces, t_s)
            ref_meta["wall_time_s"] = time.perf_counter() - t_r0
            if q_sub is None:
                q_sub = flows_ref
                p_term = {k: float(np.mean(v))
                          for k, v in ref_outlet_tip(p_node_ref, flows_ref).items()}
                p_root = {n: float(np.mean(p_node_ref[n])) for n in root_names}
                solve_meta = {"backend": "reference (scipy solve_ivp)", **ref_meta}
                backend_used[pid] = "reference"
            else:  # engineering cross-check of the headline run
                diffs = []
                for v in vessels:
                    qm = float(np.trapezoid(q_sub[v["name"]], t_s) / t_mean)
                    rm = float(np.trapezoid(flows_ref[v["name"]], t_s) / t_mean)
                    diffs.append(abs(rm - qm) / max(abs(qm), Q_MEAN_MIN_MLS))
                ref_tip = {
                    k: float(np.mean(v))
                    for k, v in ref_outlet_tip(p_node_ref, flows_ref).items()
                }
                ref_root = {n: float(np.mean(p_node_ref[n])) for n in root_names}
                w_sum_r = sum(q_bar_roots)
                p_in_ref = (
                    sum(ref_root[n] * qb for n, qb in zip(root_names, q_bar_roots)) / w_sum_r
                    if w_sum_r > Q_MEAN_MIN_MLS
                    else float(np.mean([ref_root[n] for n in root_names]))
                )
                p_in_sv = (
                    sum(p_root[n] * qb for n, qb in zip(root_names, q_bar_roots)) / w_sum_r
                    if w_sum_r > Q_MEAN_MIN_MLS
                    else float(np.mean([p_root[n] for n in root_names]))
                )
                cross[pid] = {
                    "backend": "reference (scipy solve_ivp)",
                    "method": ref_meta.get("method"),
                    "integrators_used": ref_meta.get("integrators_used"),
                    "max_rel_mean_flow_diff": float(np.max(diffs)),
                    "max_abs_outlet_pressure_diff_mmHg": float(np.max([
                        abs(ref_tip[k] - p_term[k]) for k in wk
                    ])),
                    "abs_inlet_pressure_diff_mmHg": abs(p_in_ref - p_in_sv),
                    "note": ("outlet pressure compared at the vessel tip (upstream of "
                             "Windkessel Rp) in both backends; residual inlet "
                             "differences come from the initial-condition convention "
                             "(pysvzerod steady_initial vs reference steady baseline, "
                             "Windkessel memory tau = 1.75 s)"),
                }
        timings["solve_s"] += time.perf_counter() - t_solve0

        # ---------------- flows: means, per-edge series, projection ---------
        q_mean_sub = {
            v["name"]: float(np.trapezoid(q_sub[v["name"]], t_s) / t_mean)
            for v in vessels
        }
        q_edge = {
            e["name"]: np.mean([q_sub[v["name"]] for v in subs_by_edge[e["name"]]],
                               axis=0)
            for e in edges_g
        }
        for j, n in enumerate(root_names):  # root inflow is prescribed exactly
            q_edge[root_edge[n]["name"]] = q_roots[j].copy()
        q_proj = project_flows(q_edge, edges_g, topo_ids)

        # ---------------- 1D transport -------------------------------------
        t_tr0 = time.perf_counter()
        arrival, budget = transport_1d(edges_g, root_names, root_edge, q_proj,
                                       q_roots, c_roots, t_s)
        transport_s = time.perf_counter() - t_tr0
        timings["transport_1d_s"] += transport_s

        # ---------------- branch signals -----------------------------------
        branches = {}
        for e in edges_g:
            c_d = arrival[e["name"]]
            t_arr, t_pk = upcrossing(t_s, c_d)
            transit_ms = 0.0
            ok = True
            for v in path_subs(e["name"], vessels, edges_g):
                qm = q_mean_sub[v["name"]]
                if qm < Q_MEAN_MIN_MLS:
                    ok = False  # near-zero-flow branch: guard the division
                    break
                r_m = 0.5 * (v["r_in_m"] + v["r_out_m"])
                vol_mm3 = math.pi * r_m**2 * v["length_m"] * 1e9
                transit_ms += (vol_mm3 / (qm * MM3_PER_ML)) * 1e3
            q_mean_e = float(np.trapezoid(q_edge[e["name"]], t_s) / t_mean)
            branches[e["name"]] = {
                "arrival_c": [round(float(x), 6) for x in c_d],
                "t_arrival_s": round(t_arr, 6) if t_arr is not None else None,
                "t_peak_s": round(t_pk, 6) if t_pk is not None else None,
                "transit_ms": round(transit_ms, 4) if ok else None,
                "transit_calibrated_ms": (round(transit_ms / kappa_cal, 4)
                                          if ok else None),
                "timi_frames_30fps": (int(round(transit_ms / 1000.0 * 30))
                                      if ok else None),
                "q_mean_mLs": round(q_mean_e, 6),
            }

        w_sum = sum(q_bar_roots)
        p_in_mean = (
            sum(p_root[n] * qb for n, qb in zip(root_names, q_bar_roots)) / w_sum
            if w_sum > Q_MEAN_MIN_MLS else float(np.mean([p_root[n] for n in root_names]))
        )
        signals[pid] = {
            "t_s": [round(float(x), 6) for x in t_s],
            "q_root_mLs": [round(float(x), 6) for x in q_total],
            "alpha": [round(float(x), 6) for x in alpha],
            "c_in": [round(float(x), 6) for x in c_in],
            "branches": branches,
            "p_outlet_mean_mmHg": [round(float(p_term[n]), 4) for n in sorted(wk)],
            "p_in_mean_mmHg": round(float(p_in_mean), 4),
            "_solve": solve_meta,
        }
        mass_balance[pid] = budget
        timings["per_profile"][pid] = {
            "solve_s": round(time.perf_counter() - t_solve0, 4),
            "transport_1d_s": round(transport_s, 4),
            "backend": backend_used.get(pid),
        }

    # ---------------- artifacts --------------------------------------------
    with open(f"{case_dir}/svzero_config.json", "w") as f:
        json.dump(config_a, f, indent=1)

    wk_sorted = sorted(wk)
    p_out_rows = []
    for i, on in enumerate(wk_sorted):
        b = wk[on]
        p_out_rows.append({
            "outlet": b["outlet"],
            "Rp_mmHg_s_mL": round(b["Rp_mmHg_s_mL"], 9),
            "C_mL_mmHg": round(b["C_mL_mmHg"], 9),
            "Rd_mmHg_s_mL": round(b["Rd_mmHg_s_mL"], 9),
            "tau_s": b["tau_s"],
            "outlet_node": b["node"],
            "r_out_mm": round(b["r_out_mm"], 6),
            "q_terminal_mLs": round(b["q_terminal_mLs"], 6),
            "R_total_mmHg_s_mL": round(b["R_total_mmHg_s_mL"], 9),
            "p_outlet_mean_mmHg": signals["A"]["p_outlet_mean_mmHg"][i],
        })

    timings["total_s"] = round(time.perf_counter() - t_case, 4)
    timings["solve_s"] = round(timings["solve_s"], 4)
    timings["transport_1d_s"] = round(timings["transport_1d_s"], 4)
    if fallbacks:
        timings["fallbacks"] = fallbacks

    head = sorted(set(backend_used.values()))
    if head == ["svzerodsolver"]:
        backend = "svzerodsolver (pysvzerod 2.0)"
    elif head == ["reference"]:
        backend = ("reference (scipy solve_ivp)"
                   if not fallbacks else
                   "reference (scipy solve_ivp); fallback from svzerodsolver "
                   "(see timings.fallbacks)")
    else:
        backend = f"mixed ({','.join(head)}); see timings.per_profile"

    for pid in PROFILES:
        signals[pid].pop("_solve", None)

    out = {
        "schema": "flowscope.rom.hemodynamics",
        "schema_version": 1,
        "tool": {
            **TOOL,
            "command": " ".join(sys.argv),
            "generated": datetime.now(timezone.utc).isoformat(),
            "runtime_s": timings["total_s"],
        },
        "case": case,
        "backend": backend,
        "graph": graph_path,
        "physiology": {
            "rho_kg_m3": RHO_KG_M3,
            "mu_Pa_s": MU_PA_S,
            "q_base_mLs": Q_BASE_MLS,
            "p_mean_mmHg": P_MEAN_MMHG,
            "p_systolic_mmHg": P_SYSTOLIC_MMHG,
            "p_diastolic_mmHg": P_DIASTOLIC_MMHG,
            "p_venous_mmHg": P_VENOUS_MMHG,
            "D_mm2_s": D_MM2_S,
        },
        "windkessel": {
            "per_outlet": p_out_rows,
            "calibration": {
                "r_total_formula": "(p_mean_mmHg - p_venous_mmHg) / q_terminal_mLs",
                "terminal_split": "q_terminal = q_base_mLs * r_out_mm^3 / sum(r_out_mm^3)",
                "rp_fraction": RP_FRACTION,
                "rp_band": "Rp = 5-10% of R_total (C2)",
                "tau_s": TAU_S,
                "tau_band_s": [1.5, 2.0],
                "q_base_mLs": Q_BASE_MLS,
                "p_mean_mmHg": P_MEAN_MMHG,
                "p_venous_mmHg": P_VENOUS_MMHG,
                "per_outlet_order": "outlet_node ascending; signals.<P>.p_outlet_mean_mmHg follows the same order",
                "p_outlet_definition": "mean pressure at the distal end of the terminal edge (vessel tip, upstream of Windkessel Rp), over the full profile window",
                "notes": [
                    "Rp = 0.10*R_total, Rd = 0.90*R_total, C = tau/Rd with "
                    "tau = 1.75 s (mid of the C2 1.5-2.0 s runoff band).",
                    "R_total gives a 90 -> 5 mmHg drop across each Windkessel at "
                    "its r^3 terminal share of Q_b; the solved operating point is "
                    "recorded per branch in signals.<P>.branches [q_mean_mLs], per "
                    "outlet in p_outlet_mean_mmHg and at the inlets in "
                    "signals.<P>.p_in_mean_mmHg.",
                    "SI solver values: Rp/Rd [Pa s/m^3] = [mmHg s/mL] * "
                    "133.322368*1e6, C [m^3/Pa] = [mL/mmHg] / (133.322368*1e6), "
                    "Pd = 5 mmHg in Pa.",
                    "0D sub-segments are ~10 mm BloodVessel R/L/C elements "
                    "(R = 8*mu*L/(pi*r^4), L = rho*L/(pi*r^2), C = 2*pi*r^2*L/"
                    "K_wall, K_wall = 2e5 Pa) exactly as phase2/rom/run_zerod.py.",
                ],
            },
        },
        "profiles": PROFILES,
        "signals": signals,
        "clinical": clinical,
        "mass_balance": mass_balance,
        "timings": timings,
        "inflow": {
            "mode": args.inflow_mode,
            "injection_root": args.injection_root,
            "root_split_law": "Q_root ~ root_radius_mm^3 (injectate and baseline)",
            "roots": [
                {
                    "name": n,
                    "root_radius_mm": round(inlets[n]["root_radius_mm"], 6),
                    "baseline_share": round(root_weights(inlets)[n], 6),
                    "edge": root_edge[n]["name"],
                }
                for n in root_names
            ],
            "c_in_definition": (
                "flow-weighted mean contrast fraction of the total root inflow "
                "(= 1 during a contrast segment and 0 during saline/washout in "
                "`replace` mode; diluted by the baseline in `additive` mode)"
            ),
            "initial_condition": (
                "steady baseline perfusion (washout split Q_b ~ root_radius^3) at "
                "t=0; the protocol's first segment starts at t=0"
            ),
        },
        "svzero_config": {
            "file": "svzero_config.json",
            "inflow_profile": "A",
            "note": ("exact pysvzerod model JSON used for the profile-A solve; "
                     "profiles B/C reuse the identical model with their own FLOW "
                     "waveforms (per-root piecewise-constant inflow protocol)"),
        },
    }
    if cross:
        out["cross_check"] = cross
    with open(f"{case_dir}/hemodynamics.json", "w") as f:
        json.dump(out, f, indent=1)
    return case_dir


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--cases", nargs="+", default=["601", "700", "798"])
    ap.add_argument("--graph-dir", default="out/rom/graphs")
    ap.add_argument("--out-dir", default="out/rom/hemodynamics")
    ap.add_argument("--solver", choices=["svzerodsolver", "reference", "both"],
                    default="svzerodsolver")
    ap.add_argument("--inflow-mode", choices=["replace", "additive"], default="replace")
    ap.add_argument("--injection-root", choices=["all", "root_0", "root_1"],
                    default="all")
    ap.add_argument("--lesions", default=None,
                    help="Contract-A lesions JSON (flowscope.clinical.lesions "
                         "v1; its case must match the run). Without the flag "
                         "auto-load out/clinical/lesions/case_<case>_lesions."
                         "json (canonical spelling first, legacy "
                         "<case>_lesions.json fallback) when present.")
    args = ap.parse_args()

    for case in args.cases:
        print(f"[run_hemodynamics] case {case}: {args.graph_dir}/{case}_graph.json",
              flush=True)
        t0 = time.perf_counter()
        case_dir = run_case(case, args)
        print(f"[run_hemodynamics] case {case}: wrote {case_dir}/svzero_config.json "
              f"+ hemodynamics.json ({time.perf_counter() - t0:.1f}s)", flush=True)


if __name__ == "__main__":
    main()
