#!/usr/bin/env python3
"""C3 - build the baseline carrier (velocity) field for 3D contrast transport.

Reads out/rom/graphs/{case}_graph.json (C1, schema flowscope.rom.graph v1) and
writes out/transport/carrier/{case}_carrier.npz + {case}_carrier.json (C3).

Lattice (shared with solve_advection.py C4 / export_cfd_payload.py C5):
uniform Cartesian grid over the tree bbox at --spacing mm; ``origin_mm`` is the
lattice CORNER (min corner of cell (0,0,0)), the center of cell (i,j,k) is
``origin_mm + (i+0.5, j+0.5, k+0.5) * spacing_mm``, cells stored in C-order
ravel over ``grid_shape`` (grids above 256^3 cells are refused).

Active-cell fields (npz, exact C3 names/dtypes):
  grid_shape (3,) i4, origin_mm (3,) f4, spacing_mm f4,
  cell_ravel (N_a,) i4, xyz (N_a,3) f4, u_b (N_a,3) f4, edge_id (N_a,) i4,
  s_mm (N_a,) f4, is_inlet (N_a,) bool, is_outlet (N_a,) bool, vol_mm3 (N_a,) f4

Physics of the baseline carrier velocity u_b at Q_b = 3.75 mL/s total inflow:
  * Poiseuille parabolic profile along the local centerline tangent,
    u(r) = u_max (1 - r^2/R^2), u_max = 2 Q_e / (pi R^2)  (no-slip: the
    profile vanishes at the wall r = R);
  * per-edge flux Q_e follows the 0D Q ~ r^3 bifurcation split with exact
    continuity at every junction (enforced by the discrete cell-balance
    constraints; verified per edge in stats.edge_flux_rel_error_max);
  * zero flux across lumen-mask faces; flow enters/leaves only through the open
    cross-sections flagged is_inlet / is_outlet;
  * discrete divergence-free at interior cells (<= 1e-6 normalized residual)
    via a least-squares Helmholtz projection of the face fluxes (local flux
    balancing onto the discrete divergence-free space).

Face-flux convention (mirrored by C4): the flux through the face between
neighbouring active cells a, b is exactly

    F = spacing^2 * n_hat * 0.5 * (u_b[a] + u_b[b])   (face area h^2, V = h^3)

u_b is reconstructed from the balanced face fluxes so this identity holds.
Wall faces (active-inactive) carry zero flux; the open faces of is_inlet /
is_outlet cells carry the cell imbalance (the sum of the interior face fluxes
of an inlet cell equals the inflow through its open face).

Thin vessels (radius < spacing) are minimally dilated along their centerline to
a face-connected >= 1-cell chain (counted in the JSON). Bifurcation cells carry
edge_id = -1. Disconnected mask components are dropped and counted.
"""

from __future__ import annotations

import os

# Machine spec: pin BLAS/OpenMP threading BEFORE numpy loads (overrides the
# older thread pinning in the phase2 transport prototypes).
for _var in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS"):
    os.environ[_var] = "8"

import argparse
import json
import sys
import time
from collections import deque
from datetime import datetime, timezone

import numpy as np
import scipy.sparse as sp
import scipy.sparse.linalg as spla
from scipy.sparse.csgraph import connected_components

TOOL_NAME = "phase2/transport/build_carrier.py"
TOOL_VERSION = "0.1.0"
SCHEMA = "flowscope.transport.carrier"
Q_BASE_MLS = 3.75  # mL/s total baseline coronary flow
Q_BASE_MM3S = Q_BASE_MLS * 1000.0  # mm^3/s
MAX_GRID_CELLS = 256 ** 3

NOSLIP_NOTE = (
    "Poiseuille profile u(r)=u_max*(1-r^2/R^2) along the local centerline "
    "tangent vanishing at the wall r=R (no-slip); wall-adjacent lumen cells "
    "shrink toward zero velocity and every lumen-mask face carries zero flux "
    "except the open inlet/outlet cross-sections (is_inlet/is_outlet cells)."
)


# --------------------------------------------------------------------------- #
# graph (C1)
# --------------------------------------------------------------------------- #
def load_graph(path):
    with open(path) as f:
        g = json.load(f)
    nodes = {}
    for n in g["nodes"]:
        nodes[int(n["id"])] = {
            "id": int(n["id"]),
            "name": n["name"],
            "kind": n["kind"],
            "x": np.asarray(n["x_mm"], dtype=np.float64),
            "radius_mm": float(n["radius_mm"]),
            "in_edges": [int(e) for e in n["in_edges"]],
            "out_edges": [int(e) for e in n["out_edges"]],
        }
    edges = {}
    for e in g["edges"]:
        pts = np.asarray(e["points_mm"], dtype=np.float64)
        arc = np.asarray(e["arc_mm"], dtype=np.float64)
        rad = np.asarray(e["radius_mm"], dtype=np.float64)
        if pts.ndim != 2 or pts.shape[1] != 3 or pts.shape[0] < 2:
            raise SystemExit(f"edge {e['id']}: points_mm must be (N>=2, 3)")
        if arc.shape[0] != pts.shape[0] or rad.shape[0] != pts.shape[0]:
            raise SystemExit(f"edge {e['id']}: arc_mm/radius_mm length mismatch")
        edges[int(e["id"])] = {
            "id": int(e["id"]),
            "name": e["name"],
            "from_node": int(e["from_node"]),
            "to_node": int(e["to_node"]),
            "parent_edge": None if e["parent_edge"] is None else int(e["parent_edge"]),
            "child_edges": [int(c) for c in e["child_edges"]],
            "pts": pts,
            "arc": arc,
            "rad": rad,
            "length_mm": float(e["length_mm"]),
            "r_in_mm": float(e["r_in_mm"]),
            "r_out_mm": float(e["r_out_mm"]),
        }
    return {"case": g.get("case", "?"), "nodes": nodes, "edges": edges}


def edge_children(graph, eid):
    e = graph["edges"][eid]
    kids = list(e["child_edges"]) or list(graph["nodes"][e["to_node"]]["out_edges"])
    return kids


def split_fluxes(graph, q_total_mm3s):
    """Per-edge flux Q_e [mm^3/s]: roots split ~ root_radius^3 and every
    bifurcation splits ~ child r_in_mm^3 (0D r^3 law, exact continuity)."""
    nodes, edges = graph["nodes"], graph["edges"]
    roots = [e for e in edges.values() if nodes[e["from_node"]]["kind"] == "root"]
    if not roots:
        raise SystemExit("graph has no root nodes")
    q = {eid: 0.0 for eid in edges}
    w = np.array([nodes[e["from_node"]]["radius_mm"] ** 3 for e in roots], dtype=np.float64)
    w /= w.sum()
    for e, wi in zip(roots, w):
        q[e["id"]] = q_total_mm3s * wi
    stack = [e["id"] for e in roots]
    while stack:
        eid = stack.pop()
        kids = edge_children(graph, eid)
        if not kids:
            continue
        rw = np.array([edges[k]["r_in_mm"] ** 3 for k in kids], dtype=np.float64)
        rw /= rw.sum()
        for k, wk in zip(kids, rw):
            q[k] = q[eid] * wk
            stack.append(k)
    return q


# --------------------------------------------------------------------------- #
# lattice + polyline helpers
# --------------------------------------------------------------------------- #
def build_lattice(graph, h):
    """Tree bbox over the lumen tubes (per-sample p -/+ r); origin_mm is the
    bbox min corner floored to the spacing grid (lattice corner ruling)."""
    lo = np.full(3, np.inf)
    hi = np.full(3, -np.inf)
    for e in graph["edges"].values():
        lo = np.minimum(lo, (e["pts"] - e["rad"][:, None]).min(axis=0))
        hi = np.maximum(hi, (e["pts"] + e["rad"][:, None]).max(axis=0))
    origin = np.floor(lo / h) * h
    # enough layers that lattice centers cover the bbox max
    shape = (np.floor((hi - origin) / h + 0.5).astype(np.int64) + 2).tolist()
    if int(np.prod(shape)) > MAX_GRID_CELLS:
        raise SystemExit(
            f"grid {shape} = {int(np.prod(shape))} cells exceeds the "
            f"{MAX_GRID_CELLS} (256^3) cell limit; increase --spacing "
            f"(currently {h} mm)"
        )
    return origin, tuple(int(s) for s in shape), lo, hi


def cell_index_of(x, origin, h, shape):
    """Lattice index of the cell whose center is nearest to point x (clipped)."""
    g = (np.atleast_2d(np.asarray(x, dtype=np.float64)) - origin) / h - 0.5
    idx = np.clip(np.rint(g).astype(np.int64), 0, np.asarray(shape) - 1)
    return idx


def project_on_edge(edge, X):
    """(s, rho): polyline projection of points X (arc coordinate + perp dist)."""
    pts, arc = edge["pts"], edge["arc"]
    X = np.atleast_2d(np.asarray(X, dtype=np.float64))
    best_d = np.full(X.shape[0], np.inf)
    best_s = np.zeros(X.shape[0])
    for i in range(pts.shape[0] - 1):
        p0, p1 = pts[i], pts[i + 1]
        d = p1 - p0
        l2 = float(d @ d)
        if l2 < 1e-18:
            continue
        t = np.clip(((X - p0) @ d) / l2, 0.0, 1.0)
        dist = np.linalg.norm(X - (p0 + t[:, None] * d), axis=1)
        upd = dist < best_d
        best_d = np.where(upd, dist, best_d)
        best_s = np.where(upd, arc[i] + t * (arc[i + 1] - arc[i]), best_s)
    return best_s, best_d


def tangent_at(edge, s):
    pts, arc = edge["pts"], edge["arc"]
    i = int(np.searchsorted(arc, min(max(s, 0.0), edge["length_mm"]), side="right") - 1)
    i = min(max(i, 0), pts.shape[0] - 2)
    d = pts[i + 1] - pts[i]
    n = float(np.linalg.norm(d))
    return d / n if n > 1e-15 else np.array([1.0, 0.0, 0.0])


def radius_at(edge, s):
    return float(np.interp(s, edge["arc"], edge["rad"]))


# --------------------------------------------------------------------------- #
# mask rasterization + nearest polyline features
# --------------------------------------------------------------------------- #
def rasterize(graph, origin, h, shape):
    """Cells within the local radius of any edge polyline (cell centers), plus
    the running nearest-polyline feature (edge id, s, rho) per grid cell."""
    in_mask = np.zeros(shape, dtype=bool)
    best = np.full(shape, np.inf)
    nf_edge = np.full(shape, -1, dtype=np.int32)
    nf_s = np.zeros(shape)
    nf_rho = np.full(shape, np.inf)
    shp = np.asarray(shape)

    for e in graph["edges"].values():
        pts, arc, rad = e["pts"], e["arc"], e["rad"]
        for i in range(pts.shape[0] - 1):
            p0, p1 = pts[i], pts[i + 1]
            r0, r1 = float(rad[i]), float(rad[i + 1])
            d = p1 - p0
            l2 = float(d @ d)
            if l2 < 1e-18:
                continue
            rm = max(r0, r1) + h
            lo = np.maximum(
                np.floor((np.minimum(p0, p1) - rm - origin) / h - 0.5).astype(int) - 1, 0
            )
            hi = np.minimum(
                np.floor((np.maximum(p0, p1) + rm - origin) / h - 0.5).astype(int) + 1,
                shp - 1,
            )
            if np.any(lo > hi):
                continue
            axes = [np.arange(lo[k], hi[k] + 1) for k in range(3)]
            I, J, K = np.meshgrid(axes[0], axes[1], axes[2], indexing="ij")
            X = origin + (np.stack([I, J, K], axis=-1) + 0.5) * h
            t = np.clip(((X - p0) @ d) / l2, 0.0, 1.0)
            dist = np.linalg.norm(X - (p0 + t[..., None] * d), axis=-1)
            rt = r0 + t * (r1 - r0)
            hit = dist <= rt
            w = (slice(lo[0], hi[0] + 1), slice(lo[1], hi[1] + 1), slice(lo[2], hi[2] + 1))
            in_mask[w] |= hit
            s = arc[i] + t * (arc[i + 1] - arc[i])
            upd = hit & (dist < best[w])
            if upd.any():
                best[w] = np.where(upd, dist, best[w])
                nf_edge[w] = np.where(upd, e["id"], nf_edge[w])
                nf_s[w] = np.where(upd, s, nf_s[w])
                nf_rho[w] = np.where(upd, dist, nf_rho[w])
    return in_mask, nf_edge, nf_s, nf_rho


def sample_edge(edge, h):
    """Dense samples along the edge (<= h/2 arc step) for the forced chain."""
    n = max(int(np.ceil(edge["length_mm"] / (0.5 * h))), 2) + 1
    s = np.linspace(0.0, edge["length_mm"], n)
    p = np.stack([np.interp(s, edge["arc"], edge["pts"][:, k]) for k in range(3)], axis=1)
    return s, p


# --------------------------------------------------------------------------- #
# per-case build
# --------------------------------------------------------------------------- #
def build_case(case, graph_dir, out_dir, h, command):
    t0 = time.time()
    graph_path = f"{graph_dir}/{case}_graph.json"
    if not os.path.exists(graph_path):
        raise SystemExit(f"missing C1 graph: {graph_path}")
    graph = load_graph(graph_path)
    nodes, edges = graph["nodes"], graph["edges"]
    endpoints = {eid: (e["from_node"], e["to_node"]) for eid, e in edges.items()}
    q_edge = split_fluxes(graph, Q_BASE_MM3S)
    root_edges = [e for e in edges.values() if nodes[e["from_node"]]["kind"] == "root"]
    term_edges = [e for e in edges.values() if nodes[e["to_node"]]["kind"] == "terminal"]

    origin, shape, bbox_lo, bbox_hi = build_lattice(graph, h)
    n_cells = int(np.prod(shape))
    stride = (shape[1] * shape[2], shape[2], 1)
    xyz_of = lambda flat: origin + (np.stack(np.unravel_index(flat, shape), axis=-1) + 0.5) * h

    in_mask, nf_edge, nf_s, nf_rho = rasterize(graph, origin, h, shape)

    # ---- forced centerline chains (thin-vessel dilation) --------------------
    forced = {}  # flat -> {edge_id: min s}
    chain = {}  # edge_id -> [(flat, s), ...] along the polyline
    for e in edges.values():
        s_s, p_s = sample_edge(e, h)
        fl = np.ravel_multi_index(
            tuple(cell_index_of(p_s, origin, h, shape).T), shape
        )
        order, seen = [], set()
        for k in range(fl.shape[0]):
            f = int(fl[k])
            d = forced.setdefault(f, {})
            d[e["id"]] = min(d.get(e["id"], np.inf), float(s_s[k]))
            in_mask[np.unravel_index(f, shape)] = True
            if f not in seen:
                seen.add(f)
                order.append((f, float(s_s[k])))
        chain[e["id"]] = order

    # root/terminal end layers never become junction cells
    g_prot = np.zeros(shape, dtype=bool)
    for e in root_edges:
        for f, s in chain[e["id"]]:
            if s <= 0.75 * h:
                g_prot[np.unravel_index(f, shape)] = True
    for e in term_edges:
        for f, s in chain[e["id"]]:
            if e["length_mm"] - s <= 0.75 * h:
                g_prot[np.unravel_index(f, shape)] = True

    mcell = np.flatnonzero(in_mask.ravel())  # masked cells (flat), sorted
    m_xyz = xyz_of(mcell)
    m_prot = g_prot.ravel()[mcell]
    n_m = int(mcell.shape[0])

    # ---- junction classification (edge_id = -1 at bifurcation cells) --------
    m_jnode = np.full(n_m, -1, dtype=np.int32)
    m_jdist = np.full(n_m, np.inf)
    m_forcers = [set(forced.get(int(f), {}).keys()) for f in mcell]
    for jid, jn in nodes.items():
        if jn["kind"] != "bifurcation":
            continue
        inc = [edges[i] for i in (jn["in_edges"] + jn["out_edges"]) if i in edges]
        if not inc:
            continue
        inc_ids = {e["id"] for e in inc}
        # cells forced onto a non-incident edge's centerline chain keep that
        # edge (its chain must stay face-connected); junction cells come from
        # unforced lumen cells and cells of incident chains (the confluence)
        f_ok = np.array(
            [len(fs) == 0 or bool(fs & inc_ids) for fs in m_forcers], dtype=bool
        )
        r_end = np.array(
            [e["r_out_mm"] if e["to_node"] == jid else e["r_in_mm"] for e in inc]
        )
        d = np.linalg.norm(m_xyz - jn["x"], axis=1)
        caps = (d[:, None] <= r_end[None, :]).sum(axis=1)
        cand = (caps >= 2) & (~m_prot) & (d < m_jdist) & f_ok
        m_jnode = np.where(cand, jid, m_jnode)
        m_jdist = np.where(cand, d, m_jdist)
    # hub fallback: thin bifurcations need >= 1 junction cell (all incident
    # chains share the lattice cell nearest the node)
    for jid, jn in nodes.items():
        if jn["kind"] != "bifurcation":
            continue
        inc = [edges[i] for i in (jn["in_edges"] + jn["out_edges"]) if i in edges]
        if not inc or (m_jnode == jid).any():
            continue
        d = np.linalg.norm(m_xyz - jn["x"], axis=1)
        forced_by_inc = np.array(
            [any(e["id"] in forced.get(int(f), {}) for e in inc) for f in mcell]
        )
        ok = (~m_prot) & (m_jnode < 0) & forced_by_inc
        if not ok.any():
            ok = (~m_prot) & (m_jnode < 0)
        if not ok.any():
            raise SystemExit(f"no junction cell available at node {jn['name']}")
        k = int(np.argmin(np.where(ok, d, np.inf)))
        m_jnode[k] = jid

    # ---- ownership (forced > nearest feature; junction wins) ----------------
    m_owner = np.zeros(n_m, dtype=np.int32)
    m_s = np.zeros(n_m)
    m_rho = np.zeros(n_m)
    for k in range(n_m):
        if m_jnode[k] >= 0:
            m_owner[k] = -1
            continue
        fk = forced.get(int(mcell[k]), {})
        if len(fk) == 1:
            m_owner[k] = next(iter(fk))
        else:
            m_owner[k] = int(nf_edge.ravel()[mcell[k]])
            if m_owner[k] < 0 and fk:
                m_owner[k] = next(iter(fk))
    for eid, e in edges.items():
        sel = m_owner == eid
        if sel.any():
            s_c, r_c = project_on_edge(e, m_xyz[sel])
            m_s[sel], m_rho[sel] = s_c, r_c
    jsel = m_owner < 0
    m_s[jsel] = nf_s.ravel()[mcell[jsel]]
    m_rho[jsel] = nf_rho.ravel()[mcell[jsel]]
    if (jsel & (m_jnode < 0)).any():
        raise SystemExit("internal: masked cell with neither owner nor junction node")

    # full-grid metadata (for bridging + face work)
    g_owner = np.full(n_cells, -2, dtype=np.int32)
    g_jnode = np.full(n_cells, -1, dtype=np.int32)
    g_s = np.zeros(n_cells)
    g_rho = np.zeros(n_cells)
    g_owner[mcell] = m_owner
    g_jnode[mcell] = m_jnode
    g_s[mcell] = m_s
    g_rho[mcell] = m_rho

    # ---- bridge the forced chains to face connectivity ----------------------
    def pair_live(fa, fb):
        oa, ob = int(g_owner[fa]), int(g_owner[fb])
        if oa >= 0 and ob >= 0:
            return oa == ob
        if oa < 0 and ob < 0:
            return int(g_jnode[fa]) == int(g_jnode[fb])
        if oa < 0:
            fa, fb = fb, fa
            oa, ob = ob, oa
        return int(g_jnode[fb]) in endpoints[oa]

    def enterable(f, eid):
        o = int(g_owner[f])
        if o == -2:
            return True  # inactive: activated as chain cell of eid
        if o == eid:
            return True
        return o < 0 and int(g_jnode[f]) in endpoints[eid]

    def step_ok(fa, fb, eid):
        """Would the face (fa, fb) be live once a chain of eid uses it?"""
        if not (enterable(fa, eid) and enterable(fb, eid)):
            return False
        oa = eid if int(g_owner[fa]) == -2 else int(g_owner[fa])
        ob = eid if int(g_owner[fb]) == -2 else int(g_owner[fb])
        if oa >= 0 and ob >= 0:
            return oa == ob
        if oa < 0 and ob < 0:
            return int(g_jnode[fa]) == int(g_jnode[fb])
        if oa < 0:
            return int(g_jnode[fa]) in endpoints[ob]
        return int(g_jnode[fb]) in endpoints[oa]

    def bridge(fa, fb, eid, s_mid):
        ia = np.unravel_index(fa, shape)
        ib = np.unravel_index(fb, shape)
        if sum(abs(int(ia[t]) - int(ib[t])) for t in range(3)) == 1 and step_ok(
            fa, fb, eid
        ):
            return []
        lo = np.maximum(np.minimum(ia, ib) - 4, 0)
        hi = np.minimum(np.maximum(ia, ib) + 4, np.asarray(shape) - 1)
        prev = {fa: None}
        dq = deque([fa])
        while dq:
            f = dq.popleft()
            if f == fb:
                break
            fi = np.unravel_index(f, shape)
            for t in range(3):
                for sgn in (+1, -1):
                    j = [int(fi[u]) for u in range(3)]
                    j[t] += sgn
                    if j[t] < lo[t] or j[t] > hi[t]:
                        continue
                    g = int(np.ravel_multi_index(tuple(j), shape))
                    if g in prev:
                        continue
                    if not step_ok(f, g, eid):
                        continue
                    prev[g] = f
                    dq.append(g)
        if fb not in prev:
            raise SystemExit(
                f"failed to bridge centerline chain of edge {eid} between grid "
                f"cells {tuple(int(v) for v in ia)} and {tuple(int(v) for v in ib)}; "
                f"reduce --spacing"
            )
        path, g = [], fb
        while prev[g] is not None:
            path.append(g)
            g = prev[g]
        path.reverse()
        added = []
        for f in path[:-1]:  # activate only previously inactive cells
            if int(g_owner[f]) != -2:
                continue
            added.append(f)
            d = forced.setdefault(int(f), {})
            d[eid] = min(d.get(eid, np.inf), s_mid)
            in_mask[np.unravel_index(int(f), shape)] = True
            g_owner[f] = eid
            g_jnode[f] = -1
            g_s[f] = s_mid
            g_rho[f] = 0.0
        return added

    n_dilated = {eid: 0 for eid in edges}
    raw0 = set(int(f) for f in mcell)
    for eid, order in chain.items():
        keep = []
        for f, s in order:
            o = int(g_owner[f])
            if o == -2:
                continue
            if o < 0 and int(g_jnode[f]) not in endpoints[eid]:
                continue  # foreign junction cell: route around it
            if o >= 0 and o != eid:
                continue  # shared cell resolved to another edge: route around
            keep.append((int(f), s))
        for (fa, sa), (fb, sb) in zip(keep[:-1], keep[1:]):
            added = bridge(fa, fb, eid, 0.5 * (sa + sb))
            n_dilated[eid] += sum(1 for f in added if int(f) not in raw0)

    # inter-edge junction links: C1 polylines end near (not exactly at) the node
    # position, so the incident chains need not share cells - bridge every
    # incident edge to the junction hub cell (nearest cell of the hub set)
    for jid, jn in nodes.items():
        if jn["kind"] != "bifurcation":
            continue
        inc = [edges[i] for i in (jn["in_edges"] + jn["out_edges"]) if i in edges]
        if not inc:
            continue
        jcand = np.flatnonzero((g_owner.ravel() == -1) & (g_jnode.ravel() == jid))
        if jcand.size == 0:
            continue
        xj = jn["x"]
        hub = int(jcand[int(np.argmin(np.linalg.norm(xyz_of(jcand) - xj, axis=1)))])
        for e in inc:
            pool = np.flatnonzero(g_owner.ravel() == e["id"])
            if pool.size == 0:
                pool = np.array(
                    [
                        f
                        for f, dmap in forced.items()
                        if e["id"] in dmap
                        and (
                            int(g_owner[f]) == -2
                            or int(g_owner[f]) == e["id"]
                            or (int(g_owner[f]) < 0 and int(g_jnode[f]) in endpoints[e["id"]])
                        )
                    ],
                    dtype=np.int64,
                )
            if pool.size == 0:
                continue
            entry = int(pool[int(np.argmin(np.linalg.norm(xyz_of(pool) - xj, axis=1)))])
            added = bridge(entry, hub, e["id"], float(g_s[entry]))
            n_dilated[e["id"]] += sum(1 for f in added if int(f) not in raw0)

    # ---- rebuild masked-cell bookkeeping incl. bridge cells -----------------
    mcell = np.flatnonzero(in_mask.ravel())
    n_m = int(mcell.shape[0])
    m_xyz = xyz_of(mcell)
    m_owner = g_owner.ravel()[mcell]
    m_jnode = g_jnode.ravel()[mcell]
    m_s = g_s.ravel()[mcell]
    m_rho = g_rho.ravel()[mcell]

    # ---- targets + inlet/outlet flags ---------------------------------------
    def edge_target(e, s, rho):
        R = max(radius_at(e, s), 1e-9)
        phi = max(0.0, 1.0 - (rho / R) ** 2)
        return (2.0 * q_edge[e["id"]] / (np.pi * R * R)) * phi * tangent_at(e, s)

    u_t = np.zeros((n_m, 3))
    for k in range(n_m):
        if m_owner[k] >= 0:
            u_t[k] = edge_target(edges[int(m_owner[k])], m_s[k], m_rho[k])
    for jid in np.unique(m_jnode[m_jnode >= 0]):
        sel = np.flatnonzero(m_jnode == jid)
        jn = nodes[int(jid)]
        inc = [edges[i] for i in (jn["in_edges"] + jn["out_edges"]) if i in edges]
        acc = np.zeros((sel.shape[0], 3))
        wsum = 0.0
        for e in inc:
            s_c, r_c = project_on_edge(e, m_xyz[sel])
            w = q_edge[e["id"]]
            wsum += w
            for kk, ci in enumerate(sel):
                acc[kk] += w * edge_target(e, s_c[kk], r_c[kk])
        u_t[sel] = acc / max(wsum, 1e-30)

    b = np.zeros(n_m)
    is_inlet = np.zeros(n_m, dtype=bool)
    is_outlet = np.zeros(n_m, dtype=bool)
    for e in root_edges:
        sel = np.flatnonzero((m_owner == e["id"]) & (m_s <= 0.5 * h))
        if sel.size == 0:
            pool = np.flatnonzero(m_owner == e["id"])
            if pool.size == 0:
                raise SystemExit(f"root edge {e['id']} lost all cells")
            sel = np.array([pool[int(np.argmin(m_s[pool]))]])
        t = tangent_at(e, 0.0)
        w = np.maximum(u_t[sel] @ t, 0.0)
        if w.sum() <= 0:
            w = np.ones(sel.size)
        b[sel] = q_edge[e["id"]] * w / w.sum()
        is_inlet[sel] = True
    for e in term_edges:
        sel = np.flatnonzero(
            (m_owner == e["id"]) & (e["length_mm"] - m_s <= 0.5 * h)
        )
        if sel.size == 0:
            pool = np.flatnonzero(m_owner == e["id"])
            if pool.size == 0:
                raise SystemExit(f"terminal edge {e['id']} lost all cells")
            sel = np.array([pool[int(np.argmax(m_s[pool]))]])
        t = tangent_at(e, e["length_mm"])
        w = np.maximum(u_t[sel] @ t, 0.0)
        if w.sum() <= 0:
            w = np.ones(sel.size)
        b[sel] -= q_edge[e["id"]] * w / w.sum()
        is_outlet[sel] = True

    # ---- faces over masked cells (live unknowns vs hard-zero) ---------------
    mid = np.full(n_cells, -1, dtype=np.int64)
    mid[mcell] = np.arange(n_m)
    fa_l, fb_l, fd_l, fg_l, flive = [], [], [], [], []
    shp = np.asarray(shape)
    for d in range(3):
        a_idx = np.arange(n_cells).reshape(shape)
        hi_d = [slice(None)] * 3
        hi_d[d] = slice(1, None)
        lo_d = [slice(None)] * 3
        lo_d[d] = slice(0, -1)
        ga = a_idx[tuple(lo_d)].ravel()
        gb = a_idx[tuple(hi_d)].ravel()
        both = (mid[ga] >= 0) & (mid[gb] >= 0)
        ga, gb = ga[both], gb[both]
        live = np.zeros(ga.shape[0], dtype=bool)
        for k in range(ga.shape[0]):
            live[k] = pair_live(int(ga[k]), int(gb[k]))
        fa_l.append(mid[ga])
        fb_l.append(mid[gb])
        fd_l.append(np.full(ga.shape[0], d, dtype=np.int8))
        fg_l.append(ga.astype(np.int64))
        flive.append(live)
    fa = np.concatenate(fa_l)
    fb = np.concatenate(fb_l)
    fd = np.concatenate(fd_l)
    fg = np.concatenate(fg_l)
    flive = np.concatenate(flive)

    # ---- flux components over live faces; keep those with an inlet ----------
    rows = np.concatenate([fa[flive], fb[flive]])
    cols = np.concatenate([fb[flive], fa[flive]])
    adj = sp.coo_matrix(
        (np.ones(rows.size), (rows, cols)), shape=(n_m, n_m)
    ).tocsr()
    n_comp, lab = connected_components(adj, directed=False)
    comp_has_inlet = np.zeros(n_comp, dtype=bool)
    comp_has_inlet[lab[is_inlet]] = True
    keep_cell = comp_has_inlet[lab]
    n_dropped_components = int(n_comp - comp_has_inlet.sum())
    n_dropped_cells = int((~keep_cell).sum())
    if not keep_cell.any():
        raise SystemExit("no active cells survive component filtering")

    for e in root_edges:
        if not ((m_owner == e["id"]) & keep_cell & is_inlet).any():
            raise SystemExit(
                f"root edge {e['id']} has no inlet cells in a kept component "
                f"(owner cells: {int((m_owner == e['id']).sum())}, kept: "
                f"{int(((m_owner == e['id']) & keep_cell).sum())})"
            )
    for e in term_edges:
        if not ((m_owner == e["id"]) & keep_cell & is_outlet).any():
            raise SystemExit(
                f"terminal edge {e['id']} has no outlet cells in a kept component "
                f"(owner cells: {int((m_owner == e['id']).sum())}, kept: "
                f"{int(((m_owner == e['id']) & keep_cell).sum())}, outlet: "
                f"{int(((m_owner == e['id']) & is_outlet).sum())}; mask "
                f"disconnected - reduce --spacing)"
            )
    for c in range(n_comp):
        if not comp_has_inlet[c]:
            continue
        sel = (lab == c) & keep_cell
        if abs(b[sel].sum()) > 1e-6 * Q_BASE_MM3S:
            raise SystemExit(
                f"flux component {c} is unbalanced (sum b = {b[sel].sum():.3e} "
                f"mm^3/s); mask disconnected - reduce --spacing"
            )

    # ---- Helmholtz projection: F = F_t - A^T p with A F = b -----------------
    keep_idx = np.flatnonzero(keep_cell)
    n_k = int(keep_idx.size)
    kid = np.full(n_m, -1, dtype=np.int64)
    kid[keep_idx] = np.arange(n_k)

    lk = (flive) & keep_cell[fa] & keep_cell[fb]
    ka, kb = fa[lk], fb[lk]
    kd = fd[lk].astype(np.int64)
    F_t = h * h * (u_t[ka, kd] + u_t[kb, kd]) / 2.0

    rows = np.concatenate([kid[ka], kid[kb], kid[ka], kid[kb]])
    cols = np.concatenate([kid[ka], kid[kb], kid[kb], kid[ka]])
    vals = np.concatenate(
        [np.ones(ka.size), np.ones(ka.size), -np.ones(ka.size), -np.ones(ka.size)]
    )
    L = sp.coo_matrix((vals, (rows, cols)), shape=(n_k, n_k)).tocsr()

    rhs = np.zeros(n_k)
    np.add.at(rhs, kid[ka], F_t)
    np.add.at(rhs, kid[kb], -F_t)
    rhs -= b[keep_idx]

    # pin one DOF per kept component (Laplacian nullspace = constants)
    kl = lab[keep_idx]
    pin = np.full(n_comp, -1, dtype=np.int64)
    for i in range(n_k):
        c = kl[i]
        if pin[c] < 0 or i < pin[c]:
            pin[c] = i
    sel_mask = np.ones(n_k, dtype=bool)
    sel_mask[pin[pin >= 0]] = False
    sel = np.flatnonzero(sel_mask)
    red = L[sel][:, sel].tocsc()
    p = np.zeros(n_k)
    if sel.size:
        p[sel] = spla.spsolve(red, rhs[sel])

    F = F_t - (p[kid[ka]] - p[kid[kb]])

    # ---- u_b reconstruction: (u_a + u_b)/2 * h^2 == F on every kept face ----
    kk_face = keep_cell[fa] & keep_cell[fb]  # all kept-kept faces (live + zeroed)
    fa_k, fb_k, fd_k, fg_k = fa[kk_face], fb[kk_face], fd[kk_face], fg[kk_face]
    face_flux = np.zeros(fa_k.size)  # live kept faces carry F, zeroed ones 0
    flux_by_key = {}
    for i in range(ka.size):
        flux_by_key[(int(ka[i]), int(kb[i]))] = F[i]
    for i in range(fa_k.size):
        face_flux[i] = flux_by_key.get((int(fa_k[i]), int(fb_k[i])), 0.0)

    u = u_t[keep_idx].copy()
    gff = [np.zeros(n_cells) for _ in range(3)]
    for i in range(fa_k.size):
        gff[int(fd_k[i])][int(fg_k[i])] = face_flux[i]

    for d in range(3):
        sel_d = fd_k == d
        a_d, b_d = fa_k[sel_d], fb_k[sel_d]
        rows = np.concatenate([a_d, b_d])
        cols = np.concatenate([b_d, a_d])
        adj_d = sp.coo_matrix(
            (np.ones(rows.size), (rows, cols)), shape=(n_k, n_k)
        ).tocsr()
        n_run, run_lab = connected_components(adj_d, directed=False)
        for r in range(n_run):
            cells = np.flatnonzero(run_lab == r)
            if cells.size < 2:
                continue
            flat_c = mcell[keep_idx[cells]]
            order = np.argsort(flat_c)
            cells = cells[order]
            flat_c = flat_c[order]
            m = cells.size
            g = np.empty(m - 1)
            for i in range(m - 1):
                g[i] = gff[d][min(int(flat_c[i]), int(flat_c[i + 1]))]
            part = np.zeros(m)
            for i in range(m - 1):
                part[i + 1] = 2.0 * g[i] / (h * h) - part[i]
            sgn = np.where(np.arange(m) % 2 == 0, 1.0, -1.0)
            alpha = float(np.mean(sgn * (u_t[keep_idx][cells, d] - part)))
            u[cells, d] = part + sgn * alpha

    # ---- f32 snap + divergence-free residual --------------------------------
    u32 = u.astype(np.float32)
    gff32 = [np.zeros(n_cells) for _ in range(3)]
    u64 = u32.astype(np.float64)
    kid_flat = mcell[keep_idx]
    kid_ijk = np.stack(np.unravel_index(kid_flat, shape), axis=-1)
    u_by_flat = np.zeros((n_cells, 3))
    u_by_flat[kid_flat] = u64
    for d in range(3):
        sel_d = fd_k == d
        for i in np.flatnonzero(sel_d):
            a, b_ = int(fg_k[i]), int(fg_k[i]) + stride[d]
            gff32[d][a] = 0.5 * h * h * (u_by_flat[a, d] + u_by_flat[b_, d])
    mean_u = float(np.linalg.norm(u64, axis=1).mean())

    def div_residual(gffs):
        div = np.zeros(n_k)
        for d in range(3):
            div += gffs[d][kid_flat]  # +d face flux (0 without a kept neighbour)
            lo_ok = kid_ijk[:, d] > 0
            div[lo_ok] -= gffs[d][kid_flat[lo_ok] - stride[d]]
            # faces to non-kept/inactive neighbours are walls (flux 0): nothing
        return div / (h ** 3)

    interior = ~(is_inlet[keep_idx] | is_outlet[keep_idx])
    div32 = div_residual(gff32)
    max_div32 = float(np.abs(div32[interior]).max()) if interior.any() else 0.0
    div64 = div_residual(gff)
    max_div64 = float(np.abs(div64[interior]).max()) if interior.any() else 0.0

    norm_a = mean_u / h  # |div u| / (mean|u|/h)  (dimensionless)
    norm_b = h * mean_u  # |div u| / (h*mean|u|)  (contract wording)
    div_free_residual = float(max(max_div32 / norm_a, max_div32 / norm_b))

    # ---- per-edge end-interface flux verification (0D r^3 split) -----------
    # the flux into each pipe at its from_node junction blob (open inlet face
    # for roots) and out at its to_node junction blob (open outlet face for
    # terminals) equals Q_e exactly - short pipes lie inside their junction
    # blobs' reach, so the junction faces ARE the pipe ends; the r^3 split is
    # mediated by the junction-blob balances (continuity exact).
    fo_a = m_owner[fa_k]
    fo_b = m_owner[fb_k]
    ja_k = m_jnode[fa_k].astype(np.int64)
    jb_k = m_jnode[fb_k].astype(np.int64)
    edge_rows = []
    edge_rel = []
    for eid, e in edges.items():
        sel = (m_owner == eid) & keep_cell
        if not sel.any():
            continue
        q = q_edge[eid]
        m_from = ((fo_b == eid) & (fo_a < 0) & (ja_k == e["from_node"])) | (
            (fo_a == eid) & (fo_b < 0) & (jb_k == e["from_node"])
        )
        in_flux = float(
            np.where(fo_b[m_from] == eid, face_flux[m_from], -face_flux[m_from]).sum()
        )
        if nodes[e["from_node"]]["kind"] == "root":
            in_flux += float(b[is_inlet & sel].sum())
        m_to = ((fo_a == eid) & (fo_b < 0) & (jb_k == e["to_node"])) | (
            (fo_b == eid) & (fo_a < 0) & (ja_k == e["to_node"])
        )
        out_flux = float(
            np.where(fo_a[m_to] == eid, face_flux[m_to], -face_flux[m_to]).sum()
        )
        if nodes[e["to_node"]]["kind"] == "terminal":
            out_flux += float(-b[is_outlet & sel].sum())
        rel = max(abs(in_flux / q - 1.0), abs(out_flux / q - 1.0))
        edge_rel.append(rel)
        edge_rows.append(
            {
                "edge_id": eid,
                "name": e["name"],
                "q_r3_mLs": q / 1000.0,
                "q_in_end_mLs": in_flux / 1000.0,
                "q_out_end_mLs": out_flux / 1000.0,
                "rel_error": rel,
            }
        )
    edge_flux_rel_error_max = float(max(edge_rel)) if edge_rel else 0.0

    # ---- assemble npz + json ------------------------------------------------
    order = np.argsort(mcell[keep_idx])
    keep_sorted = keep_idx[order]
    cell_ravel = mcell[keep_sorted].astype(np.int32)
    ijk = np.stack(np.unravel_index(mcell[keep_sorted], shape), axis=-1)
    xyz = (origin + (ijk + 0.5) * h).astype(np.float32)
    u_b = u32[order]
    edge_id_arr = m_owner[keep_sorted].astype(np.int32)
    s_arr = m_s[keep_sorted].astype(np.float32)
    inlet_arr = is_inlet[keep_sorted]
    outlet_arr = is_outlet[keep_sorted]
    vol = np.full(cell_ravel.shape, h ** 3, dtype=np.float32)
    n_a = int(cell_ravel.shape[0])

    os.makedirs(out_dir, exist_ok=True)
    npz_path = f"{out_dir}/{case}_carrier.npz"
    np.savez(
        npz_path,
        grid_shape=np.asarray(shape, dtype=np.int32),
        origin_mm=origin.astype(np.float32),
        spacing_mm=np.asarray(h, dtype=np.float32),
        cell_ravel=cell_ravel,
        xyz=xyz,
        u_b=u_b,
        edge_id=edge_id_arr,
        s_mm=s_arr,
        is_inlet=inlet_arr,
        is_outlet=outlet_arr,
        vol_mm3=vol,
    )

    dilated_edges = [
        {"edge_id": eid, "n_dilated_cells": int(n)}
        for eid, n in sorted(n_dilated.items())
        if n > 0
    ]
    n_junction_cells = int((edge_id_arr < 0).sum())
    lumen_volume = float(n_a * h ** 3)
    runtime_s = time.time() - t0
    doc = {
        "schema": SCHEMA,
        "schema_version": 1,
        "tool": {
            "name": TOOL_NAME,
            "version": TOOL_VERSION,
            "command": command,
            "generated": datetime.now(timezone.utc).isoformat(),
            "runtime_s": runtime_s,
        },
        "case": str(case),
        "q_base_mLs": Q_BASE_MLS,
        "lumen_volume_mm3": lumen_volume,
        "n_active_cells": n_a,
        "div_free_residual": div_free_residual,
        "noslip": NOSLIP_NOTE,
        "stats": {
            "n_dropped_components": n_dropped_components,
            "n_dropped_cells": n_dropped_cells,
            "grid_shape": list(shape),
            "spacing_mm": h,
            "origin_mm": [float(v) for v in origin],
            "bbox_mm": [[float(v) for v in bbox_lo], [float(v) for v in bbox_hi]],
            "n_masked_cells": n_m,
            "n_edges": len(edges),
            "n_roots": len(root_edges),
            "n_terminals": len(term_edges),
            "n_inlet_cells": int(inlet_arr.sum()),
            "n_outlet_cells": int(outlet_arr.sum()),
            "n_junction_cells": n_junction_cells,
            "n_dilated_cells": int(sum(n_dilated.values())),
            "dilated_edges": dilated_edges,
            "thin_vessel_dilation": (
                "vessels with radius < spacing keep a face-connected >= 1-cell "
                "chain along the centerline; the lumen mask is minimally "
                "dilated by those chain/bridge cells (see dilated_edges)"
            ),
            "flux_split": (
                "Q_total = 3.75 mL/s split ~ root_radius_mm^3 across roots and "
                "~ child r_in_mm^3 at every bifurcation (0D r^3 law); continuity "
                "at junctions is exact (discrete cell balances)"
            ),
            "cell_center_formula": (
                "xyz = origin_mm + (i+0.5, j+0.5, k+0.5) * spacing_mm "
                "(origin_mm = lattice corner = tree bbox min floored to the "
                "spacing grid; C-order ravel over grid_shape)"
            ),
            "face_flux": (
                "F = spacing^2 * n_hat * 0.5*(u_L + u_R) exactly divergence-free "
                "at interior cells (cell volume = spacing^3); wall faces zero; "
                "open faces at is_inlet/is_outlet cells carry the cell imbalance"
            ),
            "div_free_max_abs_mm3s": max_div32 * h ** 3,
            "div_free_residual_f64": float(max(max_div64 / norm_a, max_div64 / norm_b)),
            "edge_flux_rel_error_max": edge_flux_rel_error_max,
            "edge_flux": edge_rows,
        },
    }
    json_path = f"{out_dir}/{case}_carrier.json"
    with open(json_path, "w") as f:
        json.dump(doc, f, indent=2)

    print(
        f"[carrier] case {case}: grid {list(shape)} @ {h} mm, N_a = {n_a} "
        f"(lumen {lumen_volume:.1f} mm^3), div_free_residual = "
        f"{div_free_residual:.3e}, edge flux rel err = "
        f"{edge_flux_rel_error_max:.3e}, dropped {n_dropped_components} "
        f"components / {n_dropped_cells} cells -> {npz_path}"
    )
    return doc


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--cases", nargs="+", default=["601", "700", "798"])
    ap.add_argument("--graph-dir", default="out/rom/graphs")
    ap.add_argument("--out-dir", default="out/transport/carrier")
    ap.add_argument("--spacing", type=float, default=1.0, help="lattice spacing [mm]")
    args = ap.parse_args()
    if args.spacing <= 0:
        raise SystemExit("--spacing must be positive")
    command = " ".join(sys.argv)
    for case in args.cases:
        build_case(str(case), args.graph_dir, args.out_dir, float(args.spacing), command)


if __name__ == "__main__":
    main()
