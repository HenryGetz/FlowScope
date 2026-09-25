#!/usr/bin/env python
"""Track 4.3 - directed topological tree graph builder (contract C1).

Reads the svZeroDSolver-style network JSON emitted by extract_network.py
(`out/rom/networks/{case}_network.json`, schema ``flowscope.rom.network`` v1) and
emits `out/rom/graphs/{case}_graph.json` (schema ``flowscope.rom.graph`` v1):

  * nodes merged from ``inlet_nodes`` (kind ``root``), ``junctions`` (kind
    ``bifurcation``) and ``outlet_nodes`` (kind ``terminal``), with the edge ids
    incident at each node kept mutually consistent with the edge endpoints;
  * one edge per node-to-node span: a vessel spanning several junctions is split
    automatically (automated bifurcation splitting) into one edge per span, with
    points/radii interpolated at the split arc locations; zero-length spans are
    dropped and counted in ``stats.dropped_spans``;
  * Murray's-law checks at every bifurcation node (>= 2 out-edges) for
    gamma in {2.7, 3.0} using node-end radii of the incident edges,
    ``rel_error = |r_p^g - sum r_c^g| / r_p^g`` (pass <= 0.15, prototype radii);
  * per-edge ``flags``: ``distal`` (within 2 edges of any terminal node) and
    ``major`` (subtree terminal count >= 2 and ``length_mm`` >= 15).

Units: mm. The input ``radius_profile_mm`` column 0 (``s_mm``) is the arc of the
undecimated centerline and can be scaled differently from the stored polyline
arc, so a profile resample (counts differing) maps by relative arc length.
"""

import os

os.environ["OMP_NUM_THREADS"] = "8"
os.environ["MKL_NUM_THREADS"] = "8"
os.environ["OPENBLAS_NUM_THREADS"] = "8"

import argparse
import json
import sys
from datetime import datetime, timezone

import numpy as np

TOOL = {"name": "phase2/rom/build_network_graph.py", "version": "0.1.0"}

GAMMA_MURRAY = 2.7
MURRAY_GAMMAS = (2.7, 3.0)
MURRAY_TOL = 0.15
ZERO_LEN_MM = 1e-6  # spans at or below this arc length are dropped
MAJOR_LEN_MM = 15.0
DISTAL_EDGE_STEPS = 2

# --------------------------------------------------------------------------- #
# polyline helpers
# --------------------------------------------------------------------------- #
def polyline_arcs(pts):
    """Segment lengths and cumulative polyline arc (starts at 0)."""
    seg = np.linalg.norm(np.diff(pts, axis=0), axis=1)
    arc = np.concatenate([[0.0], np.cumsum(seg)])
    return seg, arc


def lerp_at(pts, seg, arc, s):
    """Point on the polyline at arc length s (clamped to the polyline)."""
    k = int(np.searchsorted(arc, s, side="right") - 1)
    k = min(max(k, 0), len(pts) - 2)
    t = (s - arc[k]) / seg[k] if seg[k] > 0 else 0.0
    t = min(max(t, 0.0), 1.0)
    return pts[k] + t * (pts[k + 1] - pts[k])


def project_polyline(pts, seg, arc, q):
    """Nearest point on the polyline to q -> (distance_mm, arc_mm)."""
    best_d, best_s = np.inf, 0.0
    for k in range(len(seg)):
        a, b = pts[k], pts[k + 1]
        ab = b - a
        denom = float(ab @ ab)
        t = float(np.clip((q - a) @ ab / denom, 0.0, 1.0)) if denom > 0 else 0.0
        d = float(np.linalg.norm(q - (a + t * ab)))
        if d < best_d:
            best_d, best_s = d, arc[k] + t * seg[k]
    return best_d, float(best_s)


def resample_radii(vessel, arc):
    """Per-point radii from ``radius_profile_mm`` col 1 (resampled if counts differ)."""
    pts_r = np.asarray(vessel["radius_profile_mm"], dtype=float).reshape(-1, 2)
    r = pts_r[:, 1]
    if len(r) == len(arc):
        return r
    s_src = pts_r[:, 0]
    span = s_src[-1] - s_src[0]
    if span <= 0:
        return np.interp(np.linspace(0.0, 1.0, len(arc)), np.linspace(0.0, 1.0, len(r)), r)
    x_src = (s_src - s_src[0]) / span
    x_dst = (arc - arc[0]) / (arc[-1] - arc[0]) if arc[-1] > arc[0] else np.zeros_like(arc)
    return np.interp(x_dst, x_src, r)


def slice_span(pts, seg, arc, r, s0, s1):
    """Polyline/radii/arc of the span [s0, s1] with interpolated endpoints."""
    i0 = int(np.searchsorted(arc, s0, side="right"))
    i1 = int(np.searchsorted(arc, s1, side="left"))
    pts_out = [lerp_at(pts, seg, arc, s0), *pts[i0:i1], lerp_at(pts, seg, arc, s1)]
    r_out = [float(np.interp(s0, arc, r)), *r[i0:i1], float(np.interp(s1, arc, r))]
    p = np.asarray(pts_out, dtype=float)
    _, span_arc = polyline_arcs(p)
    return p, np.asarray(r_out, dtype=float), span_arc


def count_subsegments(vessel, f0, f1):
    """Sub-segments of the source vessel whose midpoint lies in normalized arc [f0, f1]."""
    subs = vessel.get("sub_segments") or []
    if not subs:
        length = float(vessel.get("length_mm", 0.0))
        return max(1, int(round((f1 - f0) * length / 10.0)))
    total = sum(float(s["length_mm"]) for s in subs)
    if total <= 0:
        return len(subs)
    acc, n = 0.0, 0
    for s in subs:
        length = float(s["length_mm"])
        if f0 <= (acc + 0.5 * length) / total < f1:
            n += 1
        acc += length
    return max(1, n)


# --------------------------------------------------------------------------- #
# graph assembly
# --------------------------------------------------------------------------- #
def build_graph(case, net, command, t0):
    dropped_spans = 0

    # ---- node registry: roots, junctions, terminals in input order ----------
    nodes, by_name = [], {}

    def add_node(name, kind, x_mm, radius_mm):
        if name in by_name:
            return by_name[name]
        nid = len(nodes)
        nodes.append(
            {
                "id": nid,
                "name": name,
                "kind": kind,
                "x_mm": [float(c) for c in x_mm],
                "radius_mm": float(radius_mm),
                "in_edges": [],
                "out_edges": [],
            }
        )
        by_name[name] = nid
        return nid

    for n in net["inlet_nodes"]:
        add_node(n["name"], "root", n["position_mm"], n.get("root_radius_mm", n["radius_mm"]))
    for j in net["junctions"]:
        add_node(j["name"], "bifurcation", j["position_mm"], 0.0)  # filled from incident edges
    for o in net["outlet_nodes"]:
        add_node(o["name"], "terminal", o["position_mm"], o["radius_mm"])

    # ---- which vessels touch which nodes (beyond their declared endpoints) ---
    touch = {}
    for j in net["junctions"]:
        for vid in list(j.get("inlet_vessels", [])) + list(j.get("outlet_vessels", [])):
            touch.setdefault(int(vid), set()).add(j["name"])
    for o in net["outlet_nodes"]:
        touch.setdefault(int(o["vessel_id"]), set()).add(o["name"])

    # ---- one edge per node-to-node span (split vessels at intermediate nodes)
    edges = []
    edge_src = []  # source vessel record per edge (for parent_vessel disambiguation)
    for v in net["vessels"]:
        pts = np.asarray(v["centerline_mm"], dtype=float).reshape(-1, 3)
        if len(pts) < 2:
            print(f"  [warn] vessel {v['vessel_name']}: degenerate centerline, skipped", flush=True)
            continue
        seg, arc = polyline_arcs(pts)
        L = float(arc[-1])
        r = resample_radii(v, arc)
        name_a, name_b = v["inlet_node"], v["outlet_node"]
        for nm in (name_a, name_b):
            if nm not in by_name:
                raise KeyError(f"{case}: vessel {v['vessel_name']} references unknown node {nm!r}")

        splits = []
        for nm in sorted(touch.get(int(v["vessel_id"]), ())):
            if nm in (name_a, name_b):
                continue
            d, s = project_polyline(pts, seg, arc, np.asarray(nodes[by_name[nm]]["x_mm"]))
            s = min(max(s, 0.0), L)
            if d > max(2.0, 2.0 * float(np.mean(r))):
                print(
                    f"  [warn] vessel {v['vessel_name']}: split node {nm} is {d:.2f} mm off the "
                    "centerline (split at its projection)",
                    flush=True,
                )
            splits.append((s, nm))
        splits.sort()
        chain = [(0.0, name_a)] + splits + [(L, name_b)]

        spans = []
        for k in range(len(chain) - 1):
            s0, n0 = chain[k]
            s1, n1 = chain[k + 1]
            if s1 - s0 <= ZERO_LEN_MM:
                dropped_spans += 1
                continue
            spans.append((s0, s1, n0, n1))
        whole = len(spans) == 1 and spans[0][0] == 0.0 and spans[0][1] == L

        for k, (s0, s1, n0, n1) in enumerate(spans):
            p, pr, p_arc = slice_span(pts, seg, arc, r, s0, s1)
            name = v["vessel_name"] if whole else f"{v['vessel_name']}_s{k:02d}"
            edges.append(
                {
                    "id": len(edges),
                    "name": name,
                    "from_node": by_name[n0],
                    "to_node": by_name[n1],
                    "parent_edge": None,
                    "child_edges": [],
                    "points_mm": [[float(c) for c in q] for q in p],
                    "arc_mm": [float(a) for a in p_arc],
                    "radius_mm": [float(x) for x in pr],
                    "length_mm": float(p_arc[-1]),
                    "r_in_mm": float(pr[0]),
                    "r_out_mm": float(pr[-1]),
                    "mean_radius_mm": float(np.mean(pr)),
                    "n_subsegments": count_subsegments(v, s0 / L, s1 / L) if L > 0 else 1,
                    "flags": {"major": False, "distal": False},
                }
            )
            edge_src.append(v)

    # ---- node <-> edge consistency, parent/child tree links -----------------
    for e in edges:
        nodes[e["from_node"]]["out_edges"].append(e["id"])
        nodes[e["to_node"]]["in_edges"].append(e["id"])
    for e, v in zip(edges, edge_src):
        ins = nodes[e["from_node"]]["in_edges"]
        pid = ins[0] if ins else None
        if len(ins) > 1:  # merge: prefer the parent vessel recorded upstream
            pv = v.get("parent_vessel")
            for cand in ins:
                src = edge_src[cand]
                if (isinstance(pv, str) and src["vessel_name"] == pv) or (
                    isinstance(pv, int) and int(src["vessel_id"]) == pv
                ):
                    pid = cand
                    break
        e["parent_edge"] = pid
        e["child_edges"] = list(nodes[e["to_node"]]["out_edges"])

    # ---- node radii at bifurcations from incident edge ends -----------------
    for n in nodes:
        if n["kind"] != "bifurcation":
            continue
        ends = [edges[i]["r_out_mm"] for i in n["in_edges"]] + [
            edges[i]["r_in_mm"] for i in n["out_edges"]
        ]
        n["radius_mm"] = float(np.mean(ends)) if ends else 0.0

    # ---- flags.distal: within 2 edge-steps of any terminal node -------------
    def incident(nid):
        return set(nodes[nid]["in_edges"]) | set(nodes[nid]["out_edges"])

    for n in nodes:
        if n["kind"] != "terminal":
            continue
        frontier, marked = incident(n["id"]), set()
        for _ in range(DISTAL_EDGE_STEPS):
            frontier -= marked
            if not frontier:
                break
            for eid in frontier:
                edges[eid]["flags"]["distal"] = True
            marked |= frontier
            nxt = set()
            for eid in frontier:
                e = edges[eid]
                nxt |= incident(e["from_node"]) | incident(e["to_node"])
            frontier = nxt

    # ---- flags.major: subtree terminal count >= 2 and length >= 15 mm -------
    term_count = {}

    def count_terminals(nid, stack):
        if nid in term_count:
            return term_count[nid]
        n = nodes[nid]
        if n["kind"] == "terminal":
            term_count[nid] = 1
            return 1
        if nid in stack:  # cycle guard
            return 0
        stack.add(nid)
        total = sum(count_terminals(edges[eid]["to_node"], stack) for eid in n["out_edges"])
        stack.discard(nid)
        term_count[nid] = total
        return total

    for e in edges:
        n_sub = count_terminals(e["to_node"], set())
        e["flags"]["major"] = bool(n_sub >= 2 and e["length_mm"] >= MAJOR_LEN_MM)

    # ---- Murray's law at every bifurcation (>= 2 out-edges) -----------------
    def node_end_radius(e, nid):
        return e["r_out_mm"] if e["to_node"] == nid else e["r_in_mm"]

    murray_checks = []
    for n in nodes:
        outs = n["out_edges"]
        if len(outs) < 2:
            continue
        pid = n["in_edges"][0] if n["in_edges"] else None
        r_p = node_end_radius(edges[pid], n["id"]) if pid is not None else n["radius_mm"]
        r_c = [node_end_radius(edges[c], n["id"]) for c in outs]
        for gamma in MURRAY_GAMMAS:
            p_g = r_p**gamma
            c_g = sum(rc**gamma for rc in r_c)
            if p_g > 0:
                rel = abs(p_g - c_g) / p_g
            else:
                rel = 0.0 if c_g == 0 else 1.0
            murray_checks.append(
                {
                    "node_id": n["id"],
                    "parent_edge": pid,
                    "child_edges": list(outs),
                    "r_parent_mm": float(r_p),
                    "gamma": gamma,
                    "r_children_gamma_sum_mm": float(c_g),
                    "rel_error": float(rel),
                    "pass": bool(rel <= MURRAY_TOL),
                }
            )

    n_pass = sum(1 for row in murray_checks if row["pass"])
    stats = {
        "n_nodes": len(nodes),
        "n_edges": len(edges),
        "n_roots": sum(1 for n in nodes if n["kind"] == "root"),
        "n_terminals": sum(1 for n in nodes if n["kind"] == "terminal"),
        "total_length_mm": float(sum(e["length_mm"] for e in edges)),
        "murray_pass_frac": float(n_pass / len(murray_checks)) if murray_checks else 1.0,
        "max_murray_rel_error": float(max(row["rel_error"] for row in murray_checks))
        if murray_checks
        else 0.0,
        "dropped_spans": int(dropped_spans),
    }

    return {
        "schema": "flowscope.rom.graph",
        "schema_version": 1,
        "tool": {
            **TOOL,
            "command": command,
            "generated": datetime.now(timezone.utc).isoformat(),
            "runtime_s": (datetime.now(timezone.utc) - t0).total_seconds(),
        },
        "case": case,
        "gamma_murray": GAMMA_MURRAY,
        "nodes": nodes,
        "edges": edges,
        "murray_checks": murray_checks,
        "stats": stats,
    }


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--cases", nargs="+", default=None)
    ap.add_argument("--networks-dir", default="out/rom/networks")
    ap.add_argument("--out-dir", default="out/rom/graphs")
    args = ap.parse_args()

    if args.cases:
        cases = list(args.cases)
    elif os.path.isdir(args.networks_dir):
        suffix = "_network.json"
        cases = sorted(
            f[: -len(suffix)] for f in os.listdir(args.networks_dir) if f.endswith(suffix)
        )
    else:
        cases = []
    if not cases:
        print(f"[build_network_graph] no *_network.json found in {args.networks_dir}", flush=True)
        sys.exit(1)

    os.makedirs(args.out_dir, exist_ok=True)
    for case in cases:
        t0 = datetime.now(timezone.utc)
        net_path = f"{args.networks_dir}/{case}_network.json"
        print(f"[build_network_graph] case {case}: {net_path}", flush=True)
        with open(net_path) as f:
            net = json.load(f)
        graph = build_graph(case, net, " ".join(sys.argv), t0)
        out_path = f"{args.out_dir}/{case}_graph.json"
        with open(out_path, "w") as f:
            json.dump(graph, f, indent=1)
        st = graph["stats"]
        print(
            f"  nodes={st['n_nodes']} edges={st['n_edges']} roots={st['n_roots']} "
            f"terminals={st['n_terminals']} dropped_spans={st['dropped_spans']} "
            f"murray_rows={len(graph['murray_checks'])} pass_frac={st['murray_pass_frac']:.2f} "
            f"length={st['total_length_mm']:.0f} mm -> {out_path}",
            flush=True,
        )


if __name__ == "__main__":
    main()
