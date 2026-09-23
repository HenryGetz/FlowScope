#!/usr/bin/env python
"""Track 4.2 - 0D post-processing: branch transit times + contrast arrival curves.

For each case solved by run_zerod.py (svZeroDSolver headline backend, optional
`reference` ODE cross-check backend) this computes:

  * BRANCH TRANSIT TIMES (ms): per centerline vessel segment (branch), the
    plug-flow convective delay inlet->outlet = volume / mean flow, summed over
    the branch's 0D sub-segments. Also the bolus-based delay (t50 of the
    arrival curve at the branch distal end) for comparison.
  * CONTRAST ARRIVAL CURVES: outlet concentration vs time for an idealized
    raised-cosine bolus of --bolus-s (3-5 s) injected at the tree inlets and
    advected with a 1D plug-flow (pure advection, dc/dt + u dc/dx = 0) model
    whose velocity u(t) comes from the computed 0D flow: a particle entering a
    sub-segment at te exits at the tr solving integral_te^tr Q dt = V_segment
    (exact characteristic solution / flux convolution of plug-flow advection).
    Flows are extended periodically from the last simulated cardiac cycle.

Outputs: out/rom/report.json (+ per-case arrival-curve time/value arrays),
out/rom/report.md (tables), out/rom/figs/{case}_arrival_transit.png (if
matplotlib is available).
"""

import argparse
import json
import math
import os
import platform
import sys
from datetime import datetime, timezone

import numpy as np

TOOL = {"name": "phase2/rom/analyze.py", "version": "0.1.0"}


def load_json(path):
    with open(path) as f:
        return json.load(f)


def load_solution(path):
    """Long CSV (name,time,y) -> {dof: (t, y)} on a shared grid."""
    names, times, vals = np.loadtxt(
        path, delimiter=",", skiprows=1, dtype=str, unpack=True, ndmin=2
    )
    t = times.astype(float)
    y = vals.astype(float)
    dofs = {}
    for n in sorted(set(names.tolist())):
        m = names == n
        order = np.argsort(t[m])
        dofs[n] = (t[m][order], y[m][order])
    return dofs


def bolus_input(t, t0, tb):
    """Idealized raised-cosine bolus of duration tb starting at t0 (peak 1)."""
    x = (t - t0) / tb
    c = np.where(np.abs(x) <= 0.5, 0.5 * (1.0 + np.cos(2 * np.pi * x)), 0.0)
    return c


def plugflow_exit_map(t_grid, q, volume):
    """Exit-time map for plug flow: tr(te) with integral_te^tr Q dt = V.

    Returns (te, tr) arrays over the grid (tr strictly increasing).
    """
    q_safe = np.maximum(q, 1e-12)  # clamp tiny/negative dips (documented)
    flux = np.concatenate([[0.0], np.cumsum(0.5 * (q_safe[1:] + q_safe[:-1]) * np.diff(t_grid))])
    tr = np.interp(flux + volume, flux, t_grid)
    return t_grid, tr


def compose_maps(te, maps):
    tr = te.copy()
    for m_te, m_tr in maps:
        tr = np.interp(tr, m_te, m_tr)
    return tr


def case_analysis(case, args):
    model = load_json(f"{args.runs_dir}/{case}/model.json")
    backend_dir = f"{args.runs_dir}/{case}/{args.backend}"
    ref_dir = f"{args.runs_dir}/{case}/reference"
    sol = load_solution(f"{backend_dir}/solution.csv")
    t = sol[list(sol.keys())[0]][0]
    t = t - t[0]  # normalize to cycle start (solver may write absolute time)
    T = t[-1] - t[0]  # one cardiac cycle

    vessels = model["vessels"]
    outlets = model["outlets"]

    # per-vessel cycle-mean flow and plug-flow transit
    for v in vessels:
        tq, q = sol[v["inlet_flow_dof"]]
        tq = tq - tq[0]
        v["_q"] = q
        v["_t"] = tq
        v["_q_mean"] = float(np.trapezoid(q, tq) / (tq[-1] - tq[0]))
        v["_transit_s"] = (
            v["volume_m3"] / v["_q_mean"] if v["_q_mean"] > 1e-12 else float("nan")
        )

    # path root->vessel for every vessel (branch parent chain, sub order)
    by_branch = {}
    for v in vessels:
        by_branch.setdefault(v["branch"], []).append(v)
    for b in by_branch:
        by_branch[b].sort(key=lambda v: v["sub_index"])
    branch_parent = {}
    for v in vessels:
        branch_parent[v["branch"]] = v["parent_branch"]

    def path_to(branch):
        chain = []
        b = branch
        while b is not None:
            chain.append(b)
            b = branch_parent[b]
        path = []
        for b in reversed(chain):
            path.extend(by_branch[b])
        return path

    # analysis window: bolus + transport + margin
    max_transit = max(
        sum(v["_transit_s"] for v in path_to(o["vessel_name"].rsplit("_s", 1)[0]))
        for o in outlets
    )
    window = max(6.0, 1.5 * max_transit + args.bolus_s + 2.0)
    dt = args.dt_s
    tg = np.arange(0.0, window + dt, dt)

    def q_periodic(v):
        tq, q = v["_t"], v["_q"]
        x = np.mod(tg, T)
        return np.interp(x, tq, q)

    # transit-time per branch: sum of sub-segment V/Qbar (ms)
    branch_rows = []
    branch_delay = {}
    for bname, subs in by_branch.items():
        transit = sum(v["_transit_s"] for v in subs)
        branch_rows.append(
            {
                "branch": bname,
                "parent_branch": branch_parent[bname],
                "length_mm": sum(v["length_m"] for v in subs) * 1e3,
                "mean_radius_mm": float(np.mean([v["mean_radius_m"] for v in subs])) * 1e3,
                "n_sub_segments": len(subs),
                "mean_flow_ml_min": sum(v["_q_mean"] for v in subs) / len(subs) * 60e6,
                "transit_time_ms": transit * 1e3,
            }
        )

    # per-branch distal-end bolus delay (composed over path incl. this branch)
    for row in branch_rows:
        path = path_to(row["branch"])
        maps = [plugflow_exit_map(tg, q_periodic(v), v["volume_m3"]) for v in path]
        te = tg
        tr = compose_maps(te, maps)
        c_in = bolus_input(tg, args.bolus_t0, args.bolus_s)
        te_of_t = np.interp(tg, tr, te, left=np.nan, right=np.nan)
        c_out = np.interp(te_of_t, tg, c_in, left=0.0, right=0.0)
        c_out = np.nan_to_num(c_out)
        row["root_path_t50_ms"] = t50_ms(tg, c_out, c_in, args) * 1e3
        branch_delay[row["branch"]] = tr

    # outlet arrival curves
    c_in = bolus_input(tg, args.bolus_t0, args.bolus_s)
    outlet_rows = []
    for o in outlets:
        branch = o["vessel_name"].rsplit("_s", 1)[0]
        path = path_to(branch)
        maps = [plugflow_exit_map(tg, q_periodic(v), v["volume_m3"]) for v in path]
        tr = compose_maps(tg, maps)
        te_of_t = np.interp(tg, tr, tg, left=np.nan, right=np.nan)
        c_out = np.nan_to_num(np.interp(te_of_t, tg, c_in, left=0.0, right=0.0))
        mass = float(np.trapezoid(c_out, tg))
        centroid = float(np.trapezoid(tg * c_out, tg) / mass) if mass > 0 else float("nan")
        inlet_mass = float(np.trapezoid(c_in, tg))
        inlet_centroid = float(np.trapezoid(tg * c_in, tg) / inlet_mass)
        outlet_rows.append(
            {
                "outlet": o["name"],
                "bc_name": o["bc_name"],
                "vessel_name": o["vessel_name"],
                "radius_mm": o["radius_mm"],
                "arrival_t50_ms": t50_ms(tg, c_out, c_in, args) * 1e3,
                "arrival_tpeak_ms": float(tg[np.argmax(c_out)] * 1e3),
                "arrival_centroid_ms": (centroid - inlet_centroid) * 1e3,
                "transit_time_ms": sum(v["_transit_s"] for v in path) * 1e3,
                "peak_concentration": float(c_out.max()),
                "arrival_curve": {
                    "time_s": [round(float(x), 5) for x in tg[:: args.curve_stride]],
                    "concentration": [round(float(x), 5) for x in c_out[:: args.curve_stride]],
                },
            }
        )

    # run metadata / wall time
    meta = load_json(f"{backend_dir}/run_meta.json")
    case_out = {
        "case": case,
        "network": model["network"],
        "model": f"{args.runs_dir}/{case}/model.json",
        "solver_config": model["solver_config"],
        "solution": f"{backend_dir}/solution.csv",
        "backend": meta["backend"],
        "solver_wall_time_s": meta["wall_time_s"],
        "solver_invocation": meta.get("invocation"),
        "solver_cli_equivalent": meta.get("cli_equivalent"),
        "solver_versions": meta.get("versions"),
        "counts": {
            "branches": len(branch_rows),
            "sub_segments": len(vessels),
            "outlets": len(outlets),
            "inlets": len(model["inlets"]),
            "total_centerline_length_mm": sum(r["length_mm"] for r in branch_rows),
        },
        "inflow": {
            "total_flow_ml_min": model["bc_sizing"]["total_flow_ml_min"],
            "root_flows_ml_min": {
                k: v * 60e6 for k, v in model["bc_sizing"]["root_flow_m3_s"].items()
            },
        },
        "branches": branch_rows,
        "outlets": outlet_rows,
    }

    # cross-check against the reference backend (if present)
    if os.path.exists(f"{ref_dir}/solution.csv"):
        rsol = load_solution(f"{ref_dir}/solution.csv")
        flow_diffs, transit_diffs = [], []
        for v in vessels:
            rt, rq = rsol[v["inlet_flow_dof"]]
            rq_mean = float(np.trapezoid(rq, rt) / (rt[-1] - rt[0]))
            ref_t = v["volume_m3"] / rq_mean if rq_mean > 1e-12 else float("nan")
            flow_diffs.append(abs(rq_mean - v["_q_mean"]) / max(abs(v["_q_mean"]), 1e-12))
            transit_diffs.append(abs(ref_t - v["_transit_s"]) * 1e3)
        rmeta = load_json(f"{ref_dir}/run_meta.json")

        def nanstat(a, fn):
            a = np.asarray(a, dtype=float)
            return None if np.all(np.isnan(a)) else float(fn(a[~np.isnan(a)]))

        case_out["cross_check"] = {
            "backend": rmeta["backend"],
            "solver_wall_time_s": rmeta["wall_time_s"],
            "mean_flow_diff_pct_max": nanstat(np.array(flow_diffs) * 100, np.max),
            "mean_flow_diff_pct_mean": nanstat(np.array(flow_diffs) * 100, np.mean),
            "sub_segment_transit_diff_ms_max": nanstat(transit_diffs, np.max),
        }

    case_out["transit_time_ms_stats"] = {
        "min": float(min(r["transit_time_ms"] for r in branch_rows)),
        "max": float(max(r["transit_time_ms"] for r in branch_rows)),
        "mean": float(np.mean([r["transit_time_ms"] for r in branch_rows])),
    }
    return case_out, tg, c_in


def t50_ms(tg, c_out, c_in, args):
    """Bolus delay = (t50 on falling half of outlet curve) - (same on inlet)."""
    def t50(c):
        if c.max() <= 0:
            return float("nan")
        imax = int(np.argmax(c))
        half = 0.5 * c[imax]
        tail = np.where(c[imax:] <= half)[0]
        if len(tail) == 0:
            return float(tg[-1])
        return float(tg[imax + tail[0]])

    return t50(c_out) - t50(c_in)


def make_figs(case_outs, figs_dir):
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception as e:
        return [f"matplotlib unavailable, no figures: {e}"]
    os.makedirs(figs_dir, exist_ok=True)
    notes = []
    for case_out in case_outs:
        case = case_out["case"]
        fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(13, 5))
        for o in case_out["outlets"]:
            ac = o["arrival_curve"]
            ax1.plot(np.array(ac["time_s"]), np.array(ac["concentration"]), lw=1.2,
                     label=f"{o['outlet']} (r={o['radius_mm']:.1f}mm)")
        ax1.set_xlabel("time (s)")
        ax1.set_ylabel("contrast concentration (a.u.)")
        ax1.set_title(f"{case}: outlet contrast arrival (idealized bolus, plug-flow)")
        ax1.legend(fontsize=7, loc="upper right")
        branches = sorted(case_out["branches"], key=lambda r: r["transit_time_ms"])
        ax2.barh(
            [r["branch"] for r in branches],
            [r["transit_time_ms"] for r in branches],
        )
        ax2.set_xlabel("branch transit time (ms)")
        ax2.set_title(f"{case}: branch transit times (V/Q)")
        fig.tight_layout()
        path = f"{figs_dir}/{case}_arrival_transit.png"
        fig.savefig(path, dpi=120)
        plt.close(fig)
        notes.append(f"figure: {path}")
    return notes


def write_report_md(report, path):
    L = []
    L.append("# Track 4.2 - 0D/1D reduced-order network prototype on real vessel trees\n")
    L.append(f"Generated: {report['tool']['generated']}\n")
    L.append("## Solver\n")
    L.append(f"- Backend (headline): {report['solver']['backend']}")
    L.append(f"- Import/probe: `{report['solver']['import_probe']}`")
    L.append(f"- Invocation: `{report['solver']['invocation']}`")
    L.append(f"- CLI equivalent: `{report['solver']['cli_equivalent']}`")
    for k, v in report["solver"]["versions"].items():
        L.append(f"- version {k}: {v}")
    L.append(f"- openBF: {report['solver']['openbf_status']}")
    L.append("")
    L.append("## Commands\n")
    for k, v in report["invocations"].items():
        L.append(f"- {k}: `{v}`")
    L.append("")
    L.append("## Model summary\n")
    L.append(report["method_summary"])
    L.append("")
    for case_out in report["cases"]:
        c = case_out
        L.append(f"## Case {c['case']}\n")
        cnt = c["counts"]
        L.append(
            f"- segments (branches / sub-segments): {cnt['branches']} / {cnt['sub_segments']}, "
            f"outlets: {cnt['outlets']}, inlets: {cnt['inlets']}, "
            f"centerline: {cnt['total_centerline_length_mm']:.0f} mm"
        )
        wt = c["solver_wall_time_s"]
        L.append(
            f"- solver wall time: solve {wt.get('solve', float('nan')):.3f} s, "
            f"total {wt.get('total', float('nan')):.3f} s ({c['backend']})"
        )
        st = c["transit_time_ms_stats"]
        L.append(
            f"- branch transit time (ms): min {st['min']:.1f}, mean {st['mean']:.1f}, max {st['max']:.1f}"
        )
        if "cross_check" in c:
            x = c["cross_check"]

            def fmt(v, spec):
                return "n/a" if v is None else format(v, spec)

            L.append(
                f"- reference cross-check: mean-flow diff max {fmt(x['mean_flow_diff_pct_max'], '.2f')}% "
                f"(mean {fmt(x['mean_flow_diff_pct_mean'], '.2f')}%), sub-segment transit diff max "
                f"{fmt(x['sub_segment_transit_diff_ms_max'], '.2f')} ms"
            )
        L.append("")
        L.append("### Branch transit times\n")
        L.append(
            "| branch | parent | length (mm) | r_mean (mm) | mean flow (mL/min) | "
            "segment transit (ms) | root-path bolus t50 (ms) |"
        )
        L.append("|---|---|---|---|---|---|---|")
        for r in c["branches"]:
            L.append(
                f"| {r['branch']} | {r['parent_branch'] or '-'} | {r['length_mm']:.1f} | "
                f"{r['mean_radius_mm']:.2f} | {r['mean_flow_ml_min']:.2f} | "
                f"{r['transit_time_ms']:.1f} | {r['root_path_t50_ms']:.1f} |"
            )
        L.append("")
        L.append("### Outlet contrast arrival\n")
        L.append(
            "| outlet | r (mm) | arrival t50 (ms) | t_peak (ms) | centroid (ms) | transit V/Q (ms) | peak conc |"
        )
        L.append("|---|---|---|---|---|---|---|")
        for o in c["outlets"]:
            L.append(
                f"| {o['outlet']} | {o['radius_mm']:.2f} | {o['arrival_t50_ms']:.1f} | "
                f"{o['arrival_tpeak_ms']:.1f} | {o['arrival_centroid_ms']:.1f} | "
                f"{o['transit_time_ms']:.1f} | {o['peak_concentration']:.3f} |"
            )
        L.append("")
    L.append("## Notes and limitations\n")
    for n in report["notes"]:
        L.append(f"- {n}")
    with open(path, "w") as f:
        f.write("\n".join(L) + "\n")


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--cases", nargs="+", default=["601", "700", "798"])
    ap.add_argument("--runs-dir", default="out/rom/runs")
    ap.add_argument("--networks-dir", default="out/rom/networks")
    ap.add_argument("--out-dir", default="out/rom")
    ap.add_argument("--backend", choices=["svzerodsolver", "reference"], default="svzerodsolver")
    ap.add_argument("--bolus-s", type=float, default=4.0, help="idealized bolus duration (3-5 s)")
    ap.add_argument("--bolus-t0", type=float, default=1.0)
    ap.add_argument("--dt-s", type=float, default=0.01)
    ap.add_argument("--curve-stride", type=int, default=5, help="store every Nth arrival point")
    args = ap.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)
    case_outs = []
    for case in args.cases:
        print(f"[analyze] case {case}", flush=True)
        out, _, _ = case_analysis(case, args)
        case_outs.append(out)

    figs_dir = f"{args.out_dir}/figs"
    fig_notes = make_figs(case_outs, figs_dir)

    run_meta = load_json(
        f"{args.runs_dir}/{args.cases[0]}/{args.backend}/run_meta.json"
    )
    ref_path = f"{args.runs_dir}/{args.cases[0]}/reference/run_meta.json"
    report = {
        "schema": "flowscope.rom.report",
        "schema_version": 1,
        "tool": {
            **TOOL,
            "command": " ".join(sys.argv),
            "generated": datetime.now(timezone.utc).isoformat(),
        },
        "solver": {
            "backend": run_meta["backend"],
            "import_probe": "importlib.util.find_spec('pysvzerod') -> found; import pysvzerod (pysvzerod==2.0, SimVascular/svZeroDSolver git 8a0c68e6)",
            "invocation": run_meta.get("invocation"),
            "cli_equivalent": run_meta.get("cli_equivalent"),
            "versions": run_meta.get("versions"),
            "openbf_status": "not used: svZeroDSolver ran successfully (openBF would additionally require Julia, absent on this host)",
        },
        "cross_check_backend": "reference (phase2/rom/run_zerod.py in-file ODE integration of the same R/L/C + RCR network)",
        "invocations": {
            "extract_network": "python phase2/rom/extract_network.py --cases 601 700 798",
            "run_zerod": run_meta.get("tool", {}).get("command", "python phase2/rom/run_zerod.py"),
            "analyze": " ".join(sys.argv),
        },
        "params": {
            "bolus_s": args.bolus_s,
            "bolus_t0_s": args.bolus_t0,
            "dt_s": args.dt_s,
            "backend": args.backend,
        },
        "method_summary": (
            "Centerline branches (extract_network.py) are discretized into ~10 mm 0D sub-segments "
            "(BloodVessel R/L/C elements). Inlet: idealized sinusoidal flow waveform, total 225 mL/min, "
            "split between tree inlets by root radius^3. Outlets: RCR Windkessel, distal conductance ~ r^3, "
            "per-tree scale calibrated to 90 mmHg mean inlet pressure at 5 mmHg venous pressure. "
            "Branch transit time (ms) = sum over its sub-segments of volume / cycle-mean flow "
            "(inlet-to-outlet plug-flow convective delay of that segment). Contrast arrival curves: "
            "idealized raised-cosine bolus (3-5 s) advected with the computed flow via exact plug-flow "
            "characteristics (integral Q dt = V per segment), flows extended periodically from the last "
            "cardiac cycle; arrival delays (t50/tpeak/centroid) are measured relative to the inlet bolus. "
            "`root_path_t50_ms` on a branch is the cumulative root-to-branch-end delay."
        ),
        "cases": case_outs,
        "notes": [
            "headline numbers come from svZeroDSolver (pysvzerod); the `reference` backend is an engineering cross-check only",
            "network extraction is a prototype (skeleton + distance transform), not a commercial centerline tool; see network JSON `limits`",
            "flows at sub-segment inlet DOFs are used for transport; compressibility storage between inlet/outlet ports is neglected in the transport model",
            "arrival curves use pure advection (plug flow): no dispersion, no mixing beyond the bolus shape",
            "tiny/negative instantaneous flow dips are clamped to 1e-12 m^3/s in the transport map",
            "no headset/frame-rate or clinical claims are made anywhere in this report",
        ]
        + fig_notes,
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "numpy": np.__version__,
        },
    }
    with open(f"{args.out_dir}/report.json", "w") as f:
        json.dump(report, f, indent=1)
    write_report_md(report, f"{args.out_dir}/report.md")
    print(f"[analyze] wrote {args.out_dir}/report.json and report.md", flush=True)


if __name__ == "__main__":
    main()
