#!/usr/bin/env python
"""Track 4.1 — evaluation: held-out accuracy + CPU inference latency.

Loads checkpoints produced by ``train.py`` (out/surrogate/checkpoints/*.pt),
evaluates held-out relative L2 errors for u and p, delta_p error %, and batch-1
CPU inference latency (single thread and all threads), then writes
``out/surrogate/metrics.json`` + ``out/surrogate/report.md``.

Usage:
    .venv/bin/python phase2/surrogate/evaluate.py [--corpus data/synthetic_corpus] \
        [--out out/surrogate] [--models gino transolver] [--latency-runs 30]
"""

import argparse
import json
import os
import platform
import sys
import time

import numpy as np
import torch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from models import MODEL_NAMES, build_model, count_params, rel_l2  # noqa: E402
from train import (  # noqa: E402
    CorpusDataset, resolve_corpus, resolve_split,
)
from torch.utils.data import DataLoader  # noqa: E402


def evaluate_accuracy(model, loader, u_rms, p_rms, occ_available):
    errs_u, errs_p, dp_errs = [], [], []
    with torch.no_grad():
        for b in loader:
            pred = model(b["occ"], b["params"])
            u_p = pred["u"] * u_rms
            p_p = pred["p"] * p_rms
            errs_u.extend(rel_l2(u_p, b["u"]).tolist())
            errs_p.extend(rel_l2(p_p[..., None], b["p"][..., None]).tolist())
            for i in range(p_p.shape[0]):
                occ = b["occ"][i, 0] if occ_available else None
                m0 = occ[0] if occ is not None else torch.ones_like(p_p[i, 0])
                m1 = occ[-1] if occ is not None else torch.ones_like(p_p[i, -1])
                if float(m0.sum()) == 0 or float(m1.sum()) == 0:
                    m0 = torch.ones_like(m0)
                    m1 = torch.ones_like(m1)
                dp_p_i = float(p_p[i, 0][m0 > 0.5].mean() - p_p[i, -1][m1 > 0.5].mean())
                dp_t_i = float(b["p"][i, 0][m0 > 0.5].mean() - b["p"][i, -1][m1 > 0.5].mean())
                dp_errs.append(abs(dp_p_i - dp_t_i) / (abs(dp_t_i) + 1e-8) * 100.0)
    return {
        "rel_l2_u": float(np.mean(errs_u)),
        "rel_l2_p": float(np.mean(errs_p)),
        "delta_p_err_pct": float(np.mean(dp_errs)),
    }


def measure_latency(model, grid, cond_dim, runs, warmup=5):
    model.eval()
    occ = torch.ones(1, 1, grid, grid, grid)
    cond = torch.zeros(1, cond_dim)
    lat = {}
    for tag, threads in (("inference_ms_batch1_single", 1),
                         ("inference_ms_batch1_all_threads", os.cpu_count())):
        torch.set_num_threads(threads)
        with torch.no_grad():
            for _ in range(warmup):
                model(occ, cond)
            times = []
            for _ in range(runs):
                t0 = time.perf_counter()
                model(occ, cond)
                times.append((time.perf_counter() - t0) * 1000.0)
        lat[tag] = float(np.median(times))
        lat[tag + "_mean"] = float(np.mean(times))
    torch.set_num_threads(os.cpu_count())
    return lat


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--corpus", default="data/synthetic_corpus")
    ap.add_argument("--out", default="out/surrogate")
    ap.add_argument("--models", nargs="+", default=list(MODEL_NAMES))
    ap.add_argument("--latency-runs", type=int, default=30)
    args = ap.parse_args()

    corpus_dir, index, dev_fallback = resolve_corpus(args.corpus)
    grid_spec = index.get("grid")
    grid = int(grid_spec[0] if isinstance(grid_spec, list) else (grid_spec or 32))
    occ_available = True
    models_out = {}
    ckpt_dir = os.path.join(args.out, "checkpoints")

    for name in args.models:
        ckpt_path = os.path.join(ckpt_dir, f"{name}.pt")
        if not os.path.isfile(ckpt_path):
            print(f"[eval] skip {name}: no checkpoint at {ckpt_path}")
            continue
        ckpt = torch.load(ckpt_path, map_location="cpu", weights_only=False)
        grid = ckpt.get("grid", grid)
        stride = int(ckpt.get("grid_stride", 1) or 1)
        _, val_ids = resolve_split(index, corpus_dir)
        if ckpt.get("val_ids"):
            val_ids = [v for v in ckpt["val_ids"]]
        cond_stats = None
        if ckpt.get("cond_mean") is not None:
            cond_stats = (ckpt["cond_mean"], ckpt["cond_std"])
        ds = CorpusDataset(corpus_dir, index, val_ids, grid, cond_stats=cond_stats, stride=stride)
        dl = DataLoader(ds, batch_size=4)
        model = build_model(name, grid, ckpt["cond_dim"])
        model.load_state_dict(ckpt["state_dict"])
        occ_available = ds.occ_available
        acc = evaluate_accuracy(model, dl, ckpt["u_rms"], ckpt["p_rms"], occ_available)
        lat = measure_latency(model, grid, ckpt["cond_dim"], args.latency_runs)
        entry = {
            **acc, **lat,
            "params": count_params(model),
            "epochs": ckpt["epochs"],
            "loss": ckpt.get("loss", "rel_l2"),
            "corpus": "dev-fallback" if dev_fallback else "shared",
            "corpus_dir": corpus_dir,
            "val_samples": len(val_ids),
            "train_limit": ckpt.get("train_limit"),
            "val_limit": ckpt.get("val_limit"),
            "grid": grid,
            "corpus_grid": ckpt.get("corpus_grid", grid),
            "grid_stride": ckpt.get("grid_stride", 1),
            "final_train_loss": ckpt["train_loss"],
            "final_val_loss": ckpt["val_loss"],
            "checkpoint": ckpt_path,
            "train_command": ckpt.get("command"),
        }
        models_out[name] = entry
        print(f"[eval] {name}: rel_l2_u={acc['rel_l2_u']:.4f} rel_l2_p={acc['rel_l2_p']:.4f} "
              f"dp_err={acc['delta_p_err_pct']:.2f}% "
              f"lat={lat['inference_ms_batch1_single']:.1f}/{lat['inference_ms_batch1_all_threads']:.1f}ms")

    metrics = {
        "tool": "phase2/surrogate/evaluate.py",
        "command": " ".join(sys.argv),
        "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "torch": torch.__version__,
        "numpy": np.__version__,
        "python": platform.python_version(),
        "cpu": platform.processor() or platform.machine(),
        "cpu_threads": os.cpu_count(),
        "latency_method": f"median of {args.latency_runs} forward passes after 5 warmup, batch 1",
        "notes": [
            "Host is shared (multiple concurrent CPU jobs); latency medians include contention.",
            "all-threads uses torch intra-op parallelism over all logical CPUs; on this host "
            "thread-pool oversubscription can make it slower than single-thread.",
        ],
        "corpus": "dev-fallback" if dev_fallback else "shared",
        "corpus_dir": corpus_dir,
        "models": models_out,
    }
    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "metrics.json"), "w") as f:
        json.dump(metrics, f, indent=1)

    lines = [
        "# Track 4.1 — surrogate model metrics",
        "",
        f"Corpus: `{corpus_dir}` ({metrics['corpus']}); grid {grid}^3; "
        f"latency = {metrics['latency_method']}, CPU ({metrics['cpu_threads']} threads).",
        "",
        "| model | params | epochs | rel L2 u | rel L2 p | delta_p err % | "
        "infer ms (b1, 1-thread) | infer ms (b1, all-thread) |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name, m in models_out.items():
        lines.append(
            f"| {name} | {m['params']:,} | {m['epochs']} | {m['rel_l2_u']:.4f} | "
            f"{m['rel_l2_p']:.4f} | {m['delta_p_err_pct']:.2f} | "
            f"{m['inference_ms_batch1_single']:.1f} | {m['inference_ms_batch1_all_threads']:.1f} |"
        )
    lines += [
        "",
        f"Command: `{metrics['command']}` (torch {metrics['torch']}, numpy {metrics['numpy']}).",
        "",
        "Notes:",
    ]
    lines += [f"- {n}" for n in metrics["notes"]]
    with open(os.path.join(args.out, "report.md"), "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"[eval] wrote {args.out}/metrics.json + report.md")


if __name__ == "__main__":
    main()
