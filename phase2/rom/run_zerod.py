#!/usr/bin/env python
"""Track 4.2 - build svZeroDSolver input for extracted coronary networks and run 0D.

Reads out/rom/networks/{case}_network.json (extract_network.py) and builds the
svZeroDSolver (pysvzerod) model JSON:

  * 0D vessel elements: each centerline sub-segment -> "BloodVessel" with
    R_poiseuille (Poiseuille), L (inertance), C (linear-wall compliance):
        R = 8*mu*L_len/(pi*r^4),  L = rho*L_len/(pi*r^2),
        C = 2*pi*r^2*L_len/K_wall,  K_wall = Eh/r (wall stiffness, Pa)
  * RCR (Windkessel) outlet BCs at every terminal node, sized with a r^3 flow
    law (distal conductance proportional to outlet radius^3), proximal fraction
    Rp = rp_fraction * R_total, time constant tau = Rd * C, distal pressure Pd.
    A per-tree scale factor is calibrated (DC network collapse + bisection) so
    mean inlet pressure lands on --target-map-mmhg at the commanded flow.
  * inlet FLOW waveform: total --flow-mlmin mL/min (default 225, i.e. ~200-250
    mL/min coronary flow) split between tree inlets by root radius^3, idealized
    sinusoidal pulsatility around the mean.

Backends (--solver):
  * svzerodsolver (default, HEADLINE): pysvzerod 2.0 (svZeroDSolver, SimVascular,
    git 8a0c68e6). Usage: solver = pysvzerod.Solver(config); solver.run().
  * reference: in-file engineering cross-check - direct ODE integration
    (scipy solve_ivp) of the same lumped R/L/C network. NEVER the headline
    numbers; used to validate the svZeroDSolver results.

Outputs per case under out/rom/runs/{case}/:
  solver_config.json   exact svZeroDSolver input (documented JSON schema)
  model.json           self-describing element/BC parameters + DOF map
  svzerodsolver/solution.csv + run_meta.json
  reference/solution.csv     + run_meta.json
"""

import argparse
import json
import math
import os
import platform
import sys
import time
from datetime import datetime, timezone

import numpy as np

TOOL = {"name": "phase2/rom/run_zerod.py", "version": "0.1.0"}

MMHG = 133.322368  # Pa


# --------------------------------------------------------------------------- #
# network -> lumped elements
# --------------------------------------------------------------------------- #
def load_network(path):
    with open(path) as f:
        return json.load(f)


def build_graph(net):
    """Expand branches into sub-segment vessels and node connectivity.

    Returns vessels: [{name, branch, sub_index, length_m, r_in_m, r_out_m,
    from_node, to_node, parent_vessel, is_root_child, outlet_bc, root_bc}]
    and nodes: {name: {kind, children_vessels, inlet_vessels}}.
    """
    vessels = []
    for br in net["vessels"]:
        subs = br["sub_segments"]
        n = len(subs)
        parent = br["parent_vessel"]
        for k, sub in enumerate(subs):
            if k == 0:
                frm = br["inlet_node"]
            else:
                frm = f"k_{br['vessel_name']}_{k-1}"
            if k == n - 1:
                to = br["outlet_node"]
            else:
                to = f"k_{br['vessel_name']}_{k}"
            vessels.append(
                {
                    "name": f"{br['vessel_name']}_s{k}",
                    "branch": br["vessel_name"],
                    "sub_index": k,
                    "length_m": sub["length_mm"] * 1e-3,
                    "r_in_m": sub["r_in_mm"] * 1e-3,
                    "r_out_m": sub["r_out_mm"] * 1e-3,
                    "from_node": frm,
                    "to_node": to,
                    "parent_branch": parent,
                    "is_root_child": parent is None,
                    "is_outlet_parent": br["outlet_kind"] == "outlet" and k == n - 1,
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

    inlets = {d["name"]: d for d in net["inlet_nodes"]}
    outlets = {d["name"]: d for d in net["outlet_nodes"]}
    junctions = {d["name"]: d for d in net["junctions"]}
    for name, nd in nodes.items():
        if name in inlets:
            nd["kind"] = "inlet"
        elif name in outlets:
            nd["kind"] = "outlet"
        elif name in junctions:
            nd["kind"] = "junction"
        else:
            nd["kind"] = "chain"
    return vessels, nodes, inlets, outlets


def vessel_elements(v, args):
    """R [Pa s/m^3], L [Pa s^2/m^3], C [m^3/Pa] for a sub-segment."""
    L_len = v["length_m"]
    r = 0.5 * (v["r_in_m"] + v["r_out_m"])
    r = max(r, args.r_min_m)
    R = 8.0 * args.mu * L_len / (math.pi * r**4)
    L = args.rho * L_len / (math.pi * r**2)
    C = 2.0 * math.pi * r**2 * L_len / args.wall_k
    return R, L, C, r


def subtree_resistance(v_by_name, nodes, elem, outlet_R, start_node):
    """DC input resistance seen looking into start_node (subtree collapse)."""
    cache = {}

    def branch_R(vname):
        v = v_by_name[vname]
        R, _, _, _ = elem[vname]
        return R + cache.get(v["to_node"], 0.0)

    def post(node):
        nd = nodes[node]
        if nd["kind"] == "outlet":
            cache[node] = outlet_R[node]
            return
        for vn in nd["out_vessels"]:
            post(v_by_name[vn]["to_node"])
        inv = 0.0
        for vn in nd["out_vessels"]:
            inv += 1.0 / branch_R(vn)
        cache[node] = 0.0 if inv == 0 else 1.0 / inv

    post(start_node)
    inv = 0.0
    for vn in nodes[start_node]["out_vessels"]:
        inv += 1.0 / branch_R(vn)
    return 0.0 if inv == 0 else 1.0 / inv


# --------------------------------------------------------------------------- #
# BC sizing
# --------------------------------------------------------------------------- #
def size_rcr(outlets, elem, v_by_name, nodes, args, roots, root_flow):
    """Per-outlet RCR with r^3 conductance law; per-tree scale -> target MAP."""
    result = {}
    for root in roots:
        rname = root["name"]
        tree_outlets = [
            o for o in outlets.values() if nodes[o["name"]].get("_root") == rname
        ]
        s3 = sum(o["radius_mm"] ** 3 for o in tree_outlets)
        beta0 = max(s3, 1e-30)

        def outlet_R_map(beta):
            return {o["name"]: beta / (o["radius_mm"] ** 3) for o in tree_outlets}

        q = root_flow[rname]
        target_dp = (args.target_map_mmhg - args.p_venous_mmhg) * MMHG

        def map_error(log_beta):
            beta = math.exp(log_beta)
            oR = outlet_R_map(beta)
            rin = subtree_resistance(v_by_name, nodes, elem, oR, rname)
            return q * rin - target_dp

        lo, hi = math.log(beta0) - 25.0, math.log(beta0) + 25.0
        for _ in range(80):
            mid = 0.5 * (lo + hi)
            if map_error(mid) > 0:
                hi = mid
            else:
                lo = mid
        beta = math.exp(0.5 * (lo + hi))
        for o in tree_outlets:
            R_tot = beta / (o["radius_mm"] ** 3)
            Rp = args.rp_fraction * R_tot
            Rd = (1.0 - args.rp_fraction) * R_tot
            C = args.tau_s / Rd
            result[o["name"]] = {
                "bc_name": f"RCR_{o['name']}",
                "bc_type": "RCR",
                "bc_values": {
                    "Rp": Rp,
                    "C": C,
                    "Rd": Rd,
                    "Pd": args.p_venous_mmhg * MMHG,
                },
                "R_total": R_tot,
                "r_out_mm": o["radius_mm"],
                "beta": beta,
                "tree_root": rname,
            }
    return result


def tag_tree(v_by_name, nodes, roots):
    """Attach _root to every node/outlet by BFS from each inlet node."""
    for root in roots:
        rname = root["name"]
        seen = {rname}
        stack = [rname]
        while stack:
            node = stack.pop()
            for vn in nodes[node]["out_vessels"]:
                v = v_by_name[vn]
                v["_root"] = rname
                if v["to_node"] not in seen:
                    seen.add(v["to_node"])
                    nodes[v["to_node"]]["_root"] = rname
                    stack.append(v["to_node"])


# --------------------------------------------------------------------------- #
# svZeroDSolver config assembly
# --------------------------------------------------------------------------- #
def build_solver_config(net, vessels, nodes, inlets, outlets, elem, rcr, args, root_flow, wave_t, wave_q):
    solver_vessels = []
    v_by_name = {v["name"]: v for v in vessels}
    for i, v in enumerate(vessels):
        R, L, C, r = elem[v["name"]]
        entry = {
            "vessel_id": v["vessel_id"],
            "vessel_name": v["name"],
            "vessel_length": v["length_m"],
            "zero_d_element_type": "BloodVessel",
            "zero_d_element_values": {
                "R_poiseuille": R,
                "C": C,
                "L": L,
                "stenosis_coefficient": 0.0,
            },
        }
        bc = {}
        if nodes[v["from_node"]]["kind"] == "inlet":
            bc["inlet"] = f"IN_{v['from_node']}"
        if nodes[v["to_node"]]["kind"] == "outlet":
            bc["outlet"] = rcr[v["to_node"]]["bc_name"]
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
    for root in inlets.values():
        rname = root["name"]
        share = root_flow[rname]
        # normalize shape to requested mean
        bcs.append(
            {
                "bc_name": f"IN_{rname}",
                "bc_type": "FLOW",
                "bc_values": {
                    "Q": [float(share * q) for q in wave_q],
                    "t": [float(t) for t in wave_t],
                },
            }
        )
    for name in sorted(rcr):
        b = rcr[name]
        bcs.append(
            {"bc_name": b["bc_name"], "bc_type": "RCR", "bc_values": b["bc_values"]}
        )

    config = {
        "simulation_parameters": {
            "number_of_cardiac_cycles": args.cycles,
            "number_of_time_pts_per_cardiac_cycle": args.steps_per_cycle,
            "steady_initial": True,
            "output_variable_based": True,
            "output_all_cycles": False,
            "output_interval": 1,
            "cardiac_period": args.period_s,
            "absolute_tolerance": 1e-8,
        },
        "vessels": solver_vessels,
        "junctions": junctions,
        "boundary_conditions": bcs,
    }
    return config


def dof_map(vessels, nodes, inlets, outlets, rcr, elem):
    rows = []
    for v in vessels:
        frm, to = v["from_node"], v["to_node"]
        up = f"IN_{frm}" if nodes[frm]["kind"] == "inlet" else f"JN_{frm}"
        down = rcr[to]["bc_name"] if nodes[to]["kind"] == "outlet" else f"JN_{to}"
        R, L, C, r = elem[v["name"]]
        rows.append(
            {
                "vessel_name": v["name"],
                "branch": v["branch"],
                "parent_branch": v["parent_branch"],
                "sub_index": v["sub_index"],
                "length_m": v["length_m"],
                "mean_radius_m": r,
                "volume_m3": math.pi * r**2 * v["length_m"],
                "R_poiseuille": R,
                "L": L,
                "C": C,
                "from_node": frm,
                "to_node": to,
                "inlet_flow_dof": f"flow:{up}:{v['name']}",
                "inlet_pressure_dof": f"pressure:{up}:{v['name']}",
                "outlet_flow_dof": f"flow:{v['name']}:{down}",
                "outlet_pressure_dof": f"pressure:{v['name']}:{down}",
                "root": v.get("_root"),
            }
        )
    outlets_meta = [
        {
            "name": o["name"],
            "vessel_name": nodes[o["name"]]["in_vessels"][0],
            "bc_name": rcr[o["name"]]["bc_name"],
            "radius_mm": o["radius_mm"],
        }
        for o in outlets.values()
    ]
    inlets_meta = [
        {
            "name": r["name"],
            "bc_name": f"IN_{r['name']}",
            "root_radius_mm": r["root_radius_mm"],
        }
        for r in inlets.values()
    ]
    return rows, outlets_meta, inlets_meta


# --------------------------------------------------------------------------- #
# reference backend (engineering cross-check)
# --------------------------------------------------------------------------- #
def run_reference(vessels, nodes, inlets, outlets, elem, rcr, args, root_flow, meta_dir):
    """Exact periodic solution of the reference ODE network (harmonic balance).

    The reference network is the same lumped model as the svZeroDSolver config:
    node pressures P_n (vessel-wall compliance halves + RCR compliances lumped
    at nodes) and vessel flows Q_v (one inertance per sub-segment):
        C_n dP_n/dt = sum(Q_in) - sum(Q_out) [- (P_n - Pd)/Rd at RCR caps]
        L_v dQ_v/dt = P_up - P_down - R_v Q_v   (leaf vessels carry +Rp)
    This system is linear with a mean + first-harmonic (sinusoidal) inlet
    forcing, so its periodic state is solved exactly by harmonic balance
    (two linear solves) instead of time marching - the time-marching form of
    the same ODEs is stiff (R*C poles ~30 us) and integrates slowly. The result
    is the engineering cross-check for the svZeroDSolver run, never headline.
    """
    names = [v["name"] for v in vessels]
    v_idx = {n: i for i, n in enumerate(names)}
    node_names = list(nodes.keys())
    n_idx = {n: i for i, n in enumerate(node_names)}
    nN, nV = len(node_names), len(names)
    n = nN + nV

    C_n = np.zeros(nN)
    R_v = np.zeros(nV)
    L_v = np.zeros(nV)
    up_v = np.zeros(nV, dtype=int)
    dn_v = np.zeros(nV, dtype=int)
    for v in vessels:
        R, L, C, _ = elem[v["name"]]
        i = v_idx[v["name"]]
        R_v[i], L_v[i] = R, L
        up_v[i], dn_v[i] = n_idx[v["from_node"]], n_idx[v["to_node"]]
        C_n[up_v[i]] += C / 2.0
        C_n[dn_v[i]] += C / 2.0

    rcr_k = {}
    for oname, b in rcr.items():
        ni = n_idx[oname]
        C_n[ni] += b["bc_values"]["C"]
        rcr_k[ni] = (b["bc_values"]["Rd"], b["bc_values"]["Pd"])
    for v in vessels:
        if v["to_node"] in rcr:
            R_v[v_idx[v["name"]]] += rcr[v["to_node"]]["bc_values"]["Rp"]

    A = np.zeros((n, n))
    b0 = np.zeros(n)
    b1 = np.zeros(n)
    for i in range(nV):
        u, d = up_v[i], dn_v[i]
        # mass balance: Q_i flows u -> d (drains u, fills d)
        A[u, nN + i] -= 1.0 / C_n[u]
        A[d, nN + i] += 1.0 / C_n[d]
        A[nN + i, u] += 1.0 / L_v[i]
        A[nN + i, d] -= 1.0 / L_v[i]
        A[nN + i, nN + i] -= R_v[i] / L_v[i]
    for ni, (Rd, Pd) in rcr_k.items():
        A[ni, ni] -= 1.0 / (Rd * C_n[ni])
        b0[ni] += Pd / (Rd * C_n[ni])
    for node, q in root_flow.items():
        ni = n_idx[node]
        b0[ni] += q / C_n[ni]
        b1[ni] += q * args.flow_pulsatility / C_n[ni]

    t0 = time.perf_counter()
    x_bar = np.linalg.solve(A, -b0)  # mean (DC) state
    w = 2.0 * math.pi / args.period_s
    K = np.block([[A, w * np.eye(n)], [-w * np.eye(n), A]])
    ab = np.linalg.solve(K, np.concatenate([-b1, np.zeros(n)]))
    alpha, beta = ab[:n], ab[n:]
    wall = time.perf_counter() - t0

    t = np.linspace(0.0, args.period_s, args.steps_per_cycle)
    x = x_bar[:, None] + alpha[:, None] * np.sin(w * t) + beta[:, None] * np.cos(w * t)
    P, Q = x[:nN], x[nN:]

    rows = []
    for v in vessels:
        i = v_idx[v["name"]]
        up = f"IN_{v['from_node']}" if nodes[v["from_node"]]["kind"] == "inlet" else f"JN_{v['from_node']}"
        down = rcr[v["to_node"]]["bc_name"] if nodes[v["to_node"]]["kind"] == "outlet" else f"JN_{v['to_node']}"
        name = v["name"]
        rows.append((f"flow:{up}:{name}", Q[i]))
        rows.append((f"pressure:{up}:{name}", P[up_v[i]]))
        rows.append((f"flow:{name}:{down}", Q[i]))
        rows.append((f"pressure:{name}:{down}", P[dn_v[i]]))
    return t, rows, wall


# --------------------------------------------------------------------------- #
# svzerodsolver backend
# --------------------------------------------------------------------------- #
def run_svzerodsolver(config, out_csv, meta):
    import pysvzerod

    t0 = time.perf_counter()
    solver = pysvzerod.Solver(config)
    t_build = time.perf_counter() - t0
    t1 = time.perf_counter()
    solver.run()
    t_solve = time.perf_counter() - t1
    df = solver.get_full_result()
    t_fetch = time.perf_counter() - t1
    df.to_csv(out_csv, index=False)
    meta["wall_time_s"] = {
        "construct": t_build,
        "solve": t_solve,
        "result_and_csv": t_fetch - t_solve,
        "total": time.perf_counter() - t0,
    }
    meta["invocation"] = (
        "python -c \"import pysvzerod; s=pysvzerod.Solver(<solver_config.json>); "
        "s.run(); s.get_full_result().to_csv('solution.csv', index=False)\""
    )
    meta["cli_equivalent"] = "svzerodsolver solver_config.json solution.csv"
    meta["versions"] = {
        "pysvzerod": dist_version("pysvzerod"),
        "python": platform.python_version(),
        "numpy": np.__version__,
    }
    return df


def dist_version(name):
    try:
        from importlib import metadata

        return metadata.version(name)
    except Exception:
        return "unknown"


def write_solution_csv(t, rows, path):
    with open(path, "w") as f:
        f.write("name,time,y\n")
        for name, series in rows:
            for ti, yi in zip(t, series):
                f.write(f"{name},{ti:.9g},{yi:.9g}\n")


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--cases", nargs="+", default=["601", "700", "798"])
    ap.add_argument("--networks-dir", default="out/rom/networks")
    ap.add_argument("--out-dir", default="out/rom/runs")
    ap.add_argument("--solver", choices=["svzerodsolver", "reference", "both"], default="svzerodsolver")
    # physiology
    ap.add_argument("--rho", type=float, default=1060.0, help="blood density kg/m^3")
    ap.add_argument("--mu", type=float, default=3.5e-3, help="blood viscosity Pa s")
    ap.add_argument("--wall-k", type=float, default=2.0e5, help="wall stiffness Eh/r [Pa]; PWV = sqrt(K/(2 rho))")
    ap.add_argument("--r-min-m", type=float, default=0.5e-3)
    # inlet waveform
    ap.add_argument("--flow-mlmin", type=float, default=225.0, help="total coronary flow, split by root radius^3")
    ap.add_argument("--flow-pulsatility", type=float, default=0.35)
    ap.add_argument("--period-s", type=float, default=0.8, help="cardiac period (75 bpm)")
    # RCR sizing
    ap.add_argument("--rp-fraction", type=float, default=0.1)
    ap.add_argument("--tau-s", type=float, default=0.3, help="Windkessel time constant Rd*C")
    ap.add_argument("--target-map-mmhg", type=float, default=90.0)
    ap.add_argument("--p-venous-mmhg", type=float, default=5.0)
    # numerics
    ap.add_argument("--cycles", type=int, default=8)
    ap.add_argument("--steps-per-cycle", type=int, default=512)
    args = ap.parse_args()

    for case in args.cases:
        net_path = f"{args.networks_dir}/{case}_network.json"
        print(f"[run_zerod] case {case}: {net_path}", flush=True)
        net = load_network(net_path)
        vessels, nodes, inlets, outlets = build_graph(net)
        v_by_name = {v["name"]: v for v in vessels}
        elem = {v["name"]: vessel_elements(v, args) for v in vessels}
        tag_tree(v_by_name, nodes, list(inlets.values()))

        # inlet flow split by root radius^3
        q_total = args.flow_mlmin / 60e6  # m^3/s
        s3 = sum(r["root_radius_mm"] ** 3 for r in inlets.values())
        root_flow = {
            r["name"]: q_total * (r["root_radius_mm"] ** 3) / s3 for r in inlets.values()
        }

        rcr = size_rcr(outlets, elem, v_by_name, nodes, args, list(inlets.values()), root_flow)

        # idealized inlet waveform (unit mean, sinusoidal pulsatility)
        tt = np.linspace(0.0, args.period_s, 257)
        wave_t = tt.tolist()
        wave_q = (1.0 + args.flow_pulsatility * np.sin(2 * np.pi * tt / args.period_s)).tolist()

        config = build_solver_config(
            net, vessels, nodes, inlets, outlets, elem, rcr, args, root_flow, wave_t, wave_q
        )
        dofs, out_meta, in_meta = dof_map(vessels, nodes, inlets, outlets, rcr, elem)

        case_dir = f"{args.out_dir}/{case}"
        os.makedirs(case_dir, exist_ok=True)
        with open(f"{case_dir}/solver_config.json", "w") as f:
            json.dump(config, f, indent=1)
        model = {
            "schema": "flowscope.rom.zerod_model",
            "schema_version": 1,
            "tool": {
                **TOOL,
                "command": " ".join(sys.argv),
                "generated": datetime.now(timezone.utc).isoformat(),
            },
            "case": case,
            "network": net_path,
            "solver_config": f"{case_dir}/solver_config.json",
            "physiology": {
                "rho_kg_m3": args.rho,
                "mu_Pa_s": args.mu,
                "wall_K_Pa": args.wall_k,
                "p_wave_speed_m_s": math.sqrt(args.wall_k / (2 * args.rho)),
                "r_min_m": args.r_min_m,
                "formulas": {
                    "R_poiseuille": "8*mu*L/(pi*r^4)",
                    "L_inertance": "rho*L/(pi*r^2)",
                    "C_compliance": "2*pi*r^2*L/(K_wall)  [K_wall = Eh/r]",
                },
            },
            "bc_sizing": {
                "total_flow_m3_s": q_total,
                "total_flow_ml_min": args.flow_mlmin,
                "root_split_law": "Q_root ~ root_radius^3",
                "outlet_conductance_law": "G_outlet ~ outlet_radius^3 (R_total = beta / r_mm^3)",
                "rp_fraction": args.rp_fraction,
                "tau_s": args.tau_s,
                "target_map_mmhg": args.target_map_mmhg,
                "p_venous_mmhg": args.p_venous_mmhg,
                "flow_pulsatility": args.flow_pulsatility,
                "period_s": args.period_s,
                "root_flow_m3_s": root_flow,
            },
            "rcr": rcr,
            "vessels": dofs,
            "outlets": out_meta,
            "inlets": in_meta,
        }
        with open(f"{case_dir}/model.json", "w") as f:
            json.dump(model, f, indent=1)

        if args.solver in ("svzerodsolver", "both"):
            os.makedirs(f"{case_dir}/svzerodsolver", exist_ok=True)
            meta = {
                "backend": "svzerodsolver (pysvzerod, SimVascular svZeroDSolver)",
                "case": case,
                "solver_config": f"{case_dir}/solver_config.json",
            }
            t0 = time.perf_counter()
            df = run_svzerodsolver(config, f"{case_dir}/svzerodsolver/solution.csv", meta)
            meta.setdefault("wall_time_s", {})["wall_total_with_imports"] = time.perf_counter() - t0
            meta["tool"] = {
                **TOOL,
                "command": " ".join(sys.argv),
                "generated": datetime.now(timezone.utc).isoformat(),
            }
            meta["versions"]["scipy"] = dist_version("scipy")
            meta["versions"]["pandas"] = dist_version("pandas")
            with open(f"{case_dir}/svzerodsolver/run_meta.json", "w") as f:
                json.dump(meta, f, indent=1)
            wt = meta["wall_time_s"]
            print(
                f"  svzerodsolver: solve={wt['solve']:.3f}s total={wt['total']:.3f}s "
                f"rows={len(df)}",
                flush=True,
            )

        if args.solver in ("reference", "both"):
            os.makedirs(f"{case_dir}/reference", exist_ok=True)
            t0 = time.perf_counter()
            t, rows, wall = run_reference(
                vessels, nodes, inlets, outlets, elem, rcr, args, root_flow, case_dir
            )
            write_solution_csv(t, rows, f"{case_dir}/reference/solution.csv")
            meta = {
                "backend": "reference (phase2/rom/run_zerod.py in-file ODE, engineering cross-check)",
                "case": case,
                "wall_time_s": {
                    "solve": wall,
                    "total": time.perf_counter() - t0,
                },
                "invocation": "run_zerod.py --solver reference (in-file harmonic-balance solution of the linear R/L/C + RCR ODE network)",
                "method": "harmonic balance (exact periodic solution of the linear reference ODEs; mean + first harmonic, two linear solves)",
                "tool": {
                    **TOOL,
                    "command": " ".join(sys.argv),
                    "generated": datetime.now(timezone.utc).isoformat(),
                },
                "versions": {
                    "python": platform.python_version(),
                    "numpy": np.__version__,
                    "scipy": dist_version("scipy"),
                },
            }
            with open(f"{case_dir}/reference/run_meta.json", "w") as f:
                json.dump(meta, f, indent=1)
            print(f"  reference: solve={wall:.3f}s", flush=True)


if __name__ == "__main__":
    main()
