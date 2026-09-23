#!/usr/bin/env python
"""Track 4.2 - prototype 1D centerline network extractor for real coronary trees.

Derives a 1D centerline NETWORK from a binary coronary mask (ImageCAS labels):

  mask -> connected-component cleanup -> Euclidean distance transform (radius)
       -> 3D skeletonization (skimage.morphology.skeletonize, Lee thinning)
       -> skeleton graph -> spur pruning -> vessel segments (polylines between
       nodes) -> radius profile per point (distance transform)
       -> svZeroDSolver-style network JSON (vessels with length/radius profiles,
       junctions, inlet/outlet nodes)

Prototype limits vs. commercial centerline tools (VMTK/HeartVista/Synopsys):
  * Skeletonization is topology-preserving thinning, not a centerline solve: the
    curve is not guaranteed centered at bifurcation crowns and can zig-zag along
    voxel diagonals (only moving-average smoothing is applied).
  * Radius = distance-transform value at the skeleton point (maximal inscribed
    sphere proxy). It is biased low at bifurcations and noisy near the mask
    boundary; no orthogonal cross-section fitting is performed (commercial tools
    slice the surface normal to the centerline).
  * Bifurcation anatomy collapses to a single contracted node: no ostial
    angulation, no branch-domain splitting (VMTK BranchId/BifurcationId).
  * Loops/anastomoses are cut (cycle edges dropped) to force a tree/forest.
  * Spurs are pruned by length only (no morphological significance criterion).
  * Root (inlet) selection is a heuristic (thickest leaf endpoint), not a user
    picked seed on a surface cap.
  * Output is adequate for 0D network prototyping, NOT for clinical sizing.

Units in the emitted JSON: millimetres (fields suffixed _mm); run_zerod.py
converts to SI for svZeroDSolver.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

import nibabel as nib
import numpy as np
from scipy import ndimage as ndi
from skimage.morphology import skeletonize

TOOL = {"name": "phase2/rom/extract_network.py", "version": "0.1.0"}

N26 = [d for d in np.ndindex(3, 3, 3) if d != (1, 1, 1)]  # 26-neighborhood deltas


class UnionFind:
    def __init__(self, n):
        self.p = list(range(n))

    def find(self, a):
        while self.p[a] != a:
            self.p[a] = self.p[self.p[a]]
            a = self.p[a]
        return a

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return False
        self.p[rb] = ra
        return True


def smooth1d(x, w):
    """Moving average with edge padding."""
    x = np.asarray(x, dtype=float)
    if w <= 1 or len(x) < 3:
        return x
    k = np.ones(w) / w
    pad = w // 2
    return np.convolve(np.pad(x, pad, mode="edge"), k, mode="valid")[: len(x)]


# --------------------------------------------------------------------------- #
# extraction pipeline
# --------------------------------------------------------------------------- #
def clean_components(mask, min_vox, stats):
    cc, n = ndi.label(mask, structure=np.ones((3, 3, 3)))
    sizes = np.bincount(cc.ravel())
    keep = np.where(sizes >= min_vox)[0]
    keep = keep[keep > 0]
    out = np.isin(cc, keep)
    stats["components_raw"] = int(n)
    stats["components_kept"] = int(len(keep))
    stats["components_dropped"] = int(n - len(keep))
    stats["voxels_dropped_small_components"] = int(mask.sum() - out.sum())
    return out


def skeleton_graph(skel, r_field, world_pts):
    """Build node clusters + polyline edges from a 3D skeleton.

    Nodes: 26-connected clusters of voxels with != 2 skeleton neighbors
    (endpoints deg 1, bifurcations deg >= 3). Edges: chains of deg-2 voxels.
    Cycle-forming edges are dropped so the result is a forest.
    """
    vox = np.argwhere(skel)
    v2i = {tuple(v): i for i, v in enumerate(vox)}
    K = len(vox)

    nbrs = [[] for _ in range(K)]
    for i, v in enumerate(vox):
        for d in N26:
            j = v2i.get((v[0] + d[0] - 1, v[1] + d[1] - 1, v[2] + d[2] - 1))
            if j is not None:
                nbrs[i].append(j)

    deg = np.array([len(nb) for nb in nbrs])
    is_node = deg != 2
    node_ids = np.full(K, -1, dtype=int)

    clusters = []
    for i in np.where(is_node)[0]:
        if node_ids[i] >= 0:
            continue
        cid = len(clusters)
        members = [i]
        node_ids[i] = cid
        stack = [i]
        while stack:
            u = stack.pop()
            for v in nbrs[u]:
                if is_node[v] and node_ids[v] < 0:
                    node_ids[v] = cid
                    members.append(v)
                    stack.append(v)
        clusters.append(members)

    nodes = []
    for members in clusters:
        rr = np.array([r_field[tuple(vox[m])] for m in members])
        nodes.append(
            {
                "position_mm": world_pts[members].mean(axis=0),
                "radius_mm": float(rr.mean()),
                "max_radius_mm": float(rr.max()),
                "n_voxels": len(members),
            }
        )

    assigned = np.zeros(K, dtype=bool)
    edges = []
    chain_loops = 0
    for i in np.where(~is_node)[0]:
        if assigned[i]:
            continue
        # walk the deg-2 chain in both directions from voxel i
        def walk(start):
            seq, prev, u = [], i, start
            while not is_node[u]:
                if assigned[u]:
                    return -1, seq  # ran into an already-walked chain
                seq.append(u)
                assigned[u] = True
                nxt = [v for v in nbrs[u] if v != prev]
                if len(nxt) != 1:
                    return -1, seq
                prev, u = u, nxt[0]
            return u, seq

        assigned[i] = True
        a_id, seq_a = walk(nbrs[i][0])
        b_id, seq_b = walk(nbrs[i][1])
        if a_id < 0 or b_id < 0 or not nbrs[i]:
            chain_loops += 1  # closed deg-2 ring: drop
            continue
        if node_ids[a_id] == node_ids[b_id]:
            chain_loops += 1  # self-loop around one cluster: drop
            continue
        chain_vox = [i] + list(reversed(seq_a)) + seq_b
        edges.append(
            {
                "node_a": int(node_ids[a_id]),
                "node_b": int(node_ids[b_id]),
                "chain": chain_vox,
            }
        )

    uf = UnionFind(len(nodes))
    tree_edges = []
    cycle_dropped = 0
    for e in edges:
        if uf.union(e["node_a"], e["node_b"]):
            tree_edges.append(e)
        else:
            cycle_dropped += 1

    return {
        "nodes": nodes,
        "edges": tree_edges,
        "vox": vox,
        "world": world_pts,
        "chain_loops_dropped": chain_loops,
        "cycle_edges_dropped": cycle_dropped,
    }


def edge_polyline(g, e):
    pts = [g["nodes"][e["node_a"]]["position_mm"]]
    pts.extend(g["world"][c] for c in e["chain"])
    pts.append(g["nodes"][e["node_b"]]["position_mm"])
    return np.array(pts)


def edge_length(g, e):
    p = edge_polyline(g, e)
    return float(np.linalg.norm(np.diff(p, axis=0), axis=1).sum())


def build_adjacency(n_nodes, edges):
    adj = {i: [] for i in range(n_nodes)}
    for ei, e in enumerate(edges):
        adj[e["node_a"]].append(ei)
        adj[e["node_b"]].append(ei)
    return adj


def prune_spurs(g, spur_len_mm):
    pruned = 0
    changed = True
    while changed and g["edges"]:
        changed = False
        adj = build_adjacency(len(g["nodes"]), g["edges"])
        for i, eis in adj.items():
            if len(eis) != 1:
                continue
            ei = eis[0]
            if edge_length(g, g["edges"][ei]) < spur_len_mm:
                g["edges"][ei] = None
                g["edges"] = [e for e in g["edges"] if e is not None]
                pruned += 1
                changed = True
                break
    return pruned


def extract(case, mask_path, glb_path, args):
    t0 = datetime.now(timezone.utc)
    stats = {}
    img = nib.load(mask_path)
    aff = np.array(img.affine)
    zooms = tuple(float(z) for z in img.header.get_zooms()[:3])
    mask = np.asanyarray(img.dataobj) > 0
    mask = clean_components(mask, args.min_component_vox, stats)

    # crop to mask bbox (+margin) for EDT/skeletonize speed
    margin = 3
    lo = np.maximum(np.array(np.nonzero(mask)).min(axis=1) - margin, 0)
    hi = np.minimum(np.array(np.nonzero(mask)).max(axis=1) + margin + 1, mask.shape)
    sl = tuple(slice(a, b) for a, b in zip(lo, hi))
    stats["crop_voxel_shape"] = [int(b - a) for a, b in zip(lo, hi)]
    mcrop = mask[sl]

    # radius field (mm) from EDT
    edt = ndi.distance_transform_edt(mcrop, sampling=zooms)
    r_field = np.maximum(edt, args.r_min_mm)

    skel = skeletonize(mcrop)
    stats["skeleton_voxels"] = int(skel.sum())
    if skel.sum() == 0:
        raise RuntimeError(f"{case}: empty skeleton")

    # world coordinates of cropped voxel centers
    vox = np.argwhere(skel)  # (K,3) in crop frame
    vox_full = vox + lo
    world_pts = nib.affines.apply_affine(aff, vox_full)

    def r_at_idx(i):
        v = vox[i]
        return float(r_field[v[0], v[1], v[2]])

    g = skeleton_graph(skel, r_field, world_pts)
    stats["graph_nodes_raw"] = len(g["nodes"])
    stats["graph_edges_raw"] = len(g["edges"]) + g["cycle_edges_dropped"] + g["chain_loops_dropped"]
    stats["chain_loops_dropped"] = g["chain_loops_dropped"]
    stats["cycle_edges_dropped"] = g["cycle_edges_dropped"]

    stats["spurs_pruned"] = prune_spurs(g, args.spur_len_mm)

    # drop nodes orphaned by pruning, compact ids
    used = {n for e in g["edges"] for n in (e["node_a"], e["node_b"])}
    remap, nodes2 = {}, []
    for i, nd in enumerate(g["nodes"]):
        if i in used:
            remap[i] = len(nodes2)
            nodes2.append(nd)
    g["nodes"] = nodes2
    for e in g["edges"]:
        e["node_a"] = remap[e["node_a"]]
        e["node_b"] = remap[e["node_b"]]
    adj = build_adjacency(len(g["nodes"]), g["edges"])
    stats["graph_nodes"] = len(g["nodes"])
    stats["graph_edges"] = len(g["edges"])

    def r_at(pt_mm):
        v = np.round(nib.affines.apply_affine(np.linalg.inv(aff), pt_mm)).astype(int) - lo
        v = np.clip(v, 0, np.array(skel.shape) - 1)
        return float(r_field[tuple(v)])

    # polyline + radius profile per edge
    for e in g["edges"]:
        p = edge_polyline(g, e)
        seg = np.linalg.norm(np.diff(p, axis=0), axis=1)
        s = np.concatenate([[0.0], np.cumsum(seg)])
        r = smooth1d(np.array([r_at(q) for q in p]), args.smooth_window)
        e["points_mm"] = p
        e["s_mm"] = s
        e["length_mm"] = float(s[-1])
        e["radius_mm"] = r

    def leaf_score(i):
        """Radius at a leaf endpoint (inlet heuristic)."""
        ei = adj[i][0]
        e = g["edges"][ei]
        return float(e["radius_mm"][0] if e["node_a"] == i else e["radius_mm"][-1])

    # one root per connected component (ImageCAS left/right trees are separate)
    roots = []
    unseen = set(range(len(g["nodes"])))
    while unseen:
        seed = min(unseen)
        comp = {seed}
        stack = [seed]
        while stack:
            u = stack.pop()
            for ei in adj[u]:
                e = g["edges"][ei]
                v = e["node_b"] if e["node_a"] == u else e["node_a"]
                if v not in comp:
                    comp.add(v)
                    stack.append(v)
        leaves = [i for i in comp if len(adj[i]) == 1]
        r_root = max(leaves, key=leaf_score) if leaves else max(comp, key=lambda i: g["nodes"][i]["radius_mm"])
        roots.append(r_root)
        unseen -= comp

    # BFS from each root -> directed branches
    branches = []
    inlet_nodes, outlet_nodes, junctions = [], [], []
    for root_i in roots:
        nd = g["nodes"][root_i]
        iname = f"root_{len(inlet_nodes)}"
        inlet_nodes.append(
            {
                "name": iname,
                "node_id": root_i,
                "position_mm": [float(x) for x in nd["position_mm"]],
                "radius_mm": float(nd["radius_mm"]),
                "root_radius_mm": leaf_score(root_i),
            }
        )
        in_branch = {root_i: None}
        node_name = {root_i: iname}
        parent_node = {root_i: None}
        comp_start = len(branches)
        queue, visited = [root_i], {root_i}
        while queue:
            u = queue.pop(0)
            for ei in adj[u]:
                e = g["edges"][ei]
                a, b = e["node_a"], e["node_b"]
                v = b if a == u else a
                if v == parent_node[u]:
                    continue  # edge back toward root; already emitted
                if a == u:
                    pts, s, r = e["points_mm"], e["s_mm"], e["radius_mm"]
                else:
                    pts = e["points_mm"][::-1]
                    s = e["s_mm"][-1] - e["s_mm"][::-1]
                    r = e["radius_mm"][::-1]
                bid = len(branches)
                bname = f"{case}_b{bid:03d}"
                branches.append(
                    {
                        "vessel_id": bid,
                        "vessel_name": bname,
                        "parent_vessel": in_branch[u],
                        "from_node": node_name[u],
                        "to_node_id": v,
                        "points_mm": pts,
                        "s_mm": s,
                        "radius_mm": r,
                        "length_mm": float(s[-1]),
                    }
                )
                in_branch[v] = bid
                node_name[v] = f"{bname}_end"
                if v not in visited:
                    visited.add(v)
                    parent_node[v] = u
                    queue.append(v)

        # classify distal nodes: outlet (leaf) vs junction (bifurcation)
        child_map = {bi: [] for bi in range(comp_start, len(branches))}
        for bj in range(comp_start, len(branches)):
            p = branches[bj]["parent_vessel"]
            if p is not None:
                child_map[p].append(bj)
        for bi in range(comp_start, len(branches)):
            br = branches[bi]
            v = br["to_node_id"]
            nd = g["nodes"][v]
            kids = child_map[bi]
            if not kids:
                oname = f"out_{len(outlet_nodes):03d}"
                outlet_nodes.append(
                    {
                        "name": oname,
                        "node_id": v,
                        "vessel_id": bi,
                        "vessel_name": br["vessel_name"],
                        "position_mm": [float(x) for x in nd["position_mm"]],
                        "radius_mm": float(nd["radius_mm"]),
                    }
                )
                br["to_node"] = oname
                br["to_kind"] = "outlet"
            else:
                jn = f"J_{len(junctions):03d}"
                junctions.append(
                    {
                        "name": jn,
                        "node_id": v,
                        "position_mm": [float(x) for x in nd["position_mm"]],
                        "inlet_vessels": [bi],
                        "outlet_vessels": kids,
                    }
                )
                br["to_node"] = jn
                br["to_kind"] = "junction"
                for k in kids:
                    branches[k]["from_node"] = jn

    stats["branches"] = len(branches)
    stats["junctions"] = len(junctions)
    stats["outlets"] = len(outlet_nodes)
    stats["inlets"] = len(inlet_nodes)
    stats["total_centerline_length_mm"] = float(sum(b["length_mm"] for b in branches))

    # sub-segments per branch (uniform arc-length split)
    for br in branches:
        L, s, r = br["length_mm"], br["s_mm"], br["radius_mm"]
        n_sub = max(1, int(round(L / args.sub_len_mm)))
        edges_s = np.linspace(0.0, L, n_sub + 1)
        subs = []
        for k in range(n_sub):
            lo_s, hi_s = edges_s[k], edges_s[k + 1]
            subs.append(
                {
                    "index": k,
                    "length_mm": float(hi_s - lo_s),
                    "mean_radius_mm": float(np.interp((lo_s + hi_s) / 2, s, r)),
                    "r_in_mm": float(np.interp(lo_s, s, r)),
                    "r_out_mm": float(np.interp(hi_s, s, r)),
                }
            )
        br["sub_segments"] = subs
        br["mean_radius_mm"] = float(np.trapezoid(r, s) / max(L, 1e-9))

    vessels_json = []
    for br in branches:
        s, r, p = br["s_mm"], br["radius_mm"], br["points_mm"]
        step = max(1, len(s) // 64)
        profile = [[float(a), float(b)] for a, b in zip(s[::step], r[::step])]
        if profile[-1][0] != float(s[-1]):
            profile.append([float(s[-1]), float(r[-1])])
        cstep = max(1, len(p) // 64)
        centerline = [[float(x) for x in q] for q in p[::cstep]]
        vessels_json.append(
            {
                "vessel_id": br["vessel_id"],
                "vessel_name": br["vessel_name"],
                "parent_vessel": None
                if br["parent_vessel"] is None
                else branches[br["parent_vessel"]]["vessel_name"],
                "inlet_node": br["from_node"],
                "outlet_node": br["to_node"],
                "outlet_kind": br["to_kind"],
                "length_mm": br["length_mm"],
                "mean_radius_mm": br["mean_radius_mm"],
                "radius_profile_mm": profile,
                "centerline_mm": centerline,
                "sub_segments": br["sub_segments"],
            }
        )

    return {
        "schema": "flowscope.rom.network",
        "schema_version": 1,
        "tool": {
            **TOOL,
            "command": " ".join(sys.argv),
            "generated": datetime.now(timezone.utc).isoformat(),
            "runtime_s": (datetime.now(timezone.utc) - t0).total_seconds(),
        },
        "case": case,
        "source": {
            "mask": mask_path,
            "glb": glb_path,
            "voxel_spacing_mm": list(zooms),
            "affine": [[float(x) for x in row] for row in aff],
        },
        "units": {"coordinates": "mm", "length": "mm", "radius": "mm"},
        "extraction_params": {
            "min_component_vox": args.min_component_vox,
            "spur_len_mm": args.spur_len_mm,
            "sub_len_mm": args.sub_len_mm,
            "smooth_window": args.smooth_window,
            "r_min_mm": args.r_min_mm,
            "root_selection": "thickest leaf endpoint (distance-transform radius)",
        },
        "stats": stats,
        "inlet_nodes": inlet_nodes,
        "outlet_nodes": outlet_nodes,
        "junctions": junctions,
        "vessels": vessels_json,
        "limits": [
            "prototype skeleton centerlines: not guaranteed centered at bifurcations",
            "radius from distance transform at skeleton points (biased at bifurcations; no cross-section fitting)",
            "loops/anastomoses cut to force a tree; spurs pruned by length only",
            "inlet chosen by thickest-leaf heuristic, not a surface-cap seed",
            "not for clinical sizing; adequate for 0D network prototyping",
        ],
    }


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--cases", nargs="+", default=["601", "700", "798"])
    ap.add_argument("--data-dir", default="data/raw/imagecas")
    ap.add_argument("--glb-dir", default="out")
    ap.add_argument("--out-dir", default="out/rom/networks")
    ap.add_argument("--min-component-vox", type=int, default=250)
    ap.add_argument("--spur-len-mm", type=float, default=2.0)
    ap.add_argument("--sub-len-mm", type=float, default=10.0)
    ap.add_argument("--smooth-window", type=int, default=5)
    ap.add_argument("--r-min-mm", type=float, default=0.5)
    args = ap.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)
    for case in args.cases:
        mask_path = f"{args.data_dir}/{case}/label.nii.gz"
        glb_path = f"{args.glb_dir}/coronary_{case}.glb"
        print(f"[extract_network] case {case}: {mask_path}", flush=True)
        net = extract(
            case,
            mask_path,
            glb_path if os.path.exists(glb_path) else None,
            args,
        )
        out_path = f"{args.out_dir}/{case}_network.json"
        with open(out_path, "w") as f:
            json.dump(net, f, indent=1)
        st = net["stats"]
        print(
            f"  branches={st['branches']} junctions={st['junctions']} outlets={st['outlets']} "
            f"inlets={st['inlets']} length={st['total_centerline_length_mm']:.0f} mm "
            f"-> {out_path}",
            flush=True,
        )


if __name__ == "__main__":
    main()
