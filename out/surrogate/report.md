# Track 4.1 — surrogate model metrics

Corpus: `data/synthetic_corpus` (shared); grid 24^3; latency = median of 30 forward passes after 5 warmup, batch 1, CPU (16 threads).

| model | params | epochs | rel L2 u | rel L2 p | delta_p err % | infer ms (b1, 1-thread) | infer ms (b1, all-thread) |
|---|---:|---:|---:|---:|---:|---:|---:|
| gino | 1,408,844 | 5 | 0.2888 | 0.2829 | 24.48 | 311.5 | 2628.8 |
| transolver | 109,440 | 17 | 0.8221 | 0.8050 | 64.03 | 77.8 | 3875.8 |

Command: `phase2/surrogate/evaluate.py --models gino transolver --latency-runs 30 --out out/surrogate` (torch 2.14.0+cu130, numpy 2.5.3).

Notes:
- Host is shared (multiple concurrent CPU jobs); latency medians include contention.
- all-threads uses torch intra-op parallelism over all logical CPUs; on this host thread-pool oversubscription can make it slower than single-thread.
