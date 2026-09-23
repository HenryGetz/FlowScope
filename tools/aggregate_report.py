#!/usr/bin/env python3
"""Aggregate per-batch reports into out/aggregate_report.{json,md} (Track 1).

Merges every ``out/batches/*/report.json`` (shared contract 2) into
``out/aggregate_report.json`` (union of the case rows plus per-batch aggregate
rows) and ``out/aggregate_report.md`` (human tables: triangle budget vs the
100k-150k window, reduction %, volume drift %, GLB bytes, runtime, anomalies).

Usage::

    .venv/bin/python tools/aggregate_report.py
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from batch_build import aggregate_block

TOOL = 'tools/aggregate_report.py'
TOOL_VERSION = 1


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description='Merge out/batches/*/report.json into aggregate_report.{json,md}.'
    )
    parser.add_argument('--batches-dir', default='out/batches')
    parser.add_argument('--out-json', default='out/aggregate_report.json')
    parser.add_argument('--out-md', default='out/aggregate_report.md')
    parser.add_argument('--budget', type=int, default=120000)
    parser.add_argument('--window-lo', type=int, default=100000)
    parser.add_argument('--window-hi', type=int, default=150000)
    return parser.parse_args(argv)


def load_batches(batches_dir: Path) -> list[dict]:
    batches = []
    for report_path in sorted(batches_dir.glob('*/report.json')):
        batches.append(json.loads(report_path.read_text()))
    return batches


def _stats(values: list[float]) -> dict:
    arr = np.asarray(values, dtype=float)
    return {
        'n': int(arr.size),
        'min': float(arr.min()),
        'median': float(np.median(arr)),
        'mean': float(arr.mean()),
        'p90': float(np.percentile(arr, 90, method='linear')),
        'p95': float(np.percentile(arr, 95, method='linear')),
        'p99': float(np.percentile(arr, 99, method='linear')),
        'max': float(arr.max()),
    }


def _exceeds(values: list[float]) -> dict:
    arr = np.asarray(values, dtype=float)
    exceeds = {}
    for threshold in (0.5, 1.0, 5.0):
        count = int((arr > threshold).sum())
        exceeds[f'{threshold:g}%'] = {
            'threshold_pct': threshold,
            'count': count,
            'pct': 100.0 * count / arr.size,
        }
    return exceeds


def compute_drift_distribution(cases: list[dict], batches_dir: Path) -> dict:
    """Volume drift distribution over the per-batch evidence (shared contract 2).

    Case-level stats come from anomaly-free case rows' ``volume_drift_pct_max``;
    per-structure stats come from each ``<case>.json`` 'structures' list. Open
    (FOV-truncated) rows carry null metrics and are counted separately, as are
    skipped rows.
    """
    case_vals: list[float] = []
    case_nulls = anomaly_cases = 0
    struct_vals: list[float] = []
    signed_vals: list[float] = []
    null_metric_rows = skipped_rows = 0
    for case in cases:
        if case['anomaly']:
            anomaly_cases += 1
        else:
            drift = case['volume_drift_pct_max']
            if drift is None:
                case_nulls += 1
            else:
                case_vals.append(drift)
        case_path = batches_dir / case['batch_id'] / f"{case['case_id']}.json"
        for row in json.loads(case_path.read_text())['structures']:
            if 'skipped' in row:
                skipped_rows += 1
                continue
            drift = row['volume_drift_pct']
            if drift is None:
                null_metric_rows += 1
                continue
            struct_vals.append(drift)
            raw = row['volume_raw_mm3']
            signed_vals.append((row['volume_smoothed_mm3'] - raw) / raw * 100.0)
    signed = np.asarray(signed_vals, dtype=float)
    shrink = int((signed < 0).sum())
    grow = int((signed > 0).sum())
    return {
        'percentile_method': 'numpy.percentile with linear interpolation',
        'case_level': {
            'n_cases': len(case_vals) + case_nulls,
            'null_count': case_nulls,
            'excluded_anomaly_count': anomaly_cases,
            **_stats(case_vals),
            'exceeds': _exceeds(case_vals),
        },
        'per_structure': {
            **_stats(struct_vals),
            'null_metric_count': null_metric_rows,
            'skipped_count': skipped_rows,
            'exceeds': _exceeds(struct_vals),
        },
        'signed_drift': {
            'n': int(signed.size),
            'min': float(signed.min()),
            'median': float(np.median(signed)),
            'mean': float(signed.mean()),
            'max': float(signed.max()),
            'shrink_count': shrink,
            'shrink_pct': 100.0 * shrink / signed.size,
            'unchanged_count': int(signed.size) - shrink - grow,
            'grow_count': grow,
            'grow_pct': 100.0 * grow / signed.size,
        },
    }


def _fmt(value) -> str:
    if value is None:
        return 'n/a'
    if isinstance(value, float):
        return f'{value:,.2f}'.rstrip('0').rstrip('.')
    return f'{value:,}' if isinstance(value, int) else str(value)


def _fmt_stat(value: float) -> str:
    """Drift/signed-drift stat: trim to 6 decimals without losing small values."""
    return f'{value:.6f}'.rstrip('0').rstrip('.')


def _cell(value) -> str:
    """Markdown table cell: escape pipes (anomaly traces contain them)."""
    return str(value).replace('|', '\\|')


def render_drift_md(report: dict) -> list[str]:
    dd = report['drift_distribution']
    cl, ps, sd = dd['case_level'], dd['per_structure'], dd['signed_drift']
    lines = [
        '',
        f"## Volume drift distribution ({report['n_cases']} measured scans)",
        '',
        f"Case-level max per-structure drift (`volume_drift_pct_max`) over anomaly-free case rows "
        f"(n = {_fmt(cl['n'])} measured, {_fmt(cl['null_count'])} null of {_fmt(cl['n_cases'])} rows; "
        f"{_fmt(cl['excluded_anomaly_count'])} anomaly rows excluded):",
        '',
        '| n | null | min | median | mean | p90 | p95 | p99 | max |',
        '| --- | --- | --- | --- | --- | --- | --- | --- | --- |',
        f"| {_fmt(cl['n'])} | {_fmt(cl['null_count'])} | "
        + ' | '.join(_fmt_stat(cl[k]) for k in ('min', 'median', 'mean', 'p90', 'p95', 'p99', 'max'))
        + ' |',
        '',
        '| threshold | cases exceeding | % of measured cases |',
        '| --- | --- | --- |',
    ]
    for label, ex in cl['exceeds'].items():
        lines.append(f"| > {label} | {_fmt(ex['count'])} | {_fmt(ex['pct'])}% |")
    lines += [
        '',
        f"Per-structure drift (`volume_drift_pct`) over measured structure rows "
        f"(n = {_fmt(ps['n'])}; {_fmt(ps['null_metric_count'])} open/FOV-truncated rows with null metrics "
        f"and {_fmt(ps['skipped_count'])} skipped rows counted separately):",
        '',
        '| n | null-metric (open/FOV) | skipped | min | median | mean | p90 | p95 | p99 | max |',
        '| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |',
        f"| {_fmt(ps['n'])} | {_fmt(ps['null_metric_count'])} | {_fmt(ps['skipped_count'])} | "
        + ' | '.join(_fmt_stat(ps[k]) for k in ('min', 'median', 'mean', 'p90', 'p95', 'p99', 'max'))
        + ' |',
        '',
        '| threshold | structures exceeding | % of measured structures |',
        '| --- | --- | --- |',
    ]
    for label, ex in ps['exceeds'].items():
        lines.append(f"| > {label} | {_fmt(ex['count'])} | {_fmt(ex['pct'])}% |")
    lines += [
        '',
        f"Signed drift (`(volume_smoothed_mm3 - volume_raw_mm3) / volume_raw_mm3 * 100`) over the same "
        f"measured structure rows (n = {_fmt(sd['n'])}):",
        '',
        '| n | min | median | mean | max | shrink (< 0) | unchanged (= 0) | grow (> 0) |',
        '| --- | --- | --- | --- | --- | --- | --- | --- |',
        f"| {_fmt(sd['n'])} | "
        + ' | '.join(_fmt_stat(sd[k]) for k in ('min', 'median', 'mean', 'max'))
        + f" | {_fmt(sd['shrink_count'])} ({_fmt(sd['shrink_pct'])}%) "
        f"| {_fmt(sd['unchanged_count'])} | {_fmt(sd['grow_count'])} ({_fmt(sd['grow_pct'])}%) |",
        '',
    ]
    if sd['shrink_count'] == 0:
        lines.append(
            f"Taubin non-shrinking holds strictly: median signed drift {_fmt_stat(sd['median'])}%, "
            f"0% of structures shrink."
        )
    else:
        lines.append(
            f"Taubin non-shrinking does NOT hold strictly (no-volume-loss holds only approximately): "
            f"median signed drift {_fmt_stat(sd['median'])}%, "
            f"{_fmt(sd['shrink_pct'])}% of structures shrink "
            f"(signed drift range {_fmt_stat(sd['min'])}% to {_fmt_stat(sd['max'])}%)."
        )
    lines += ['', f"- percentile method: {dd['percentile_method']}."]
    return lines


def render_md(report: dict, batches: list[dict], args) -> str:
    agg = report['aggregate']
    cl = report['drift_distribution']['case_level']
    lo, hi = args.window_lo, args.window_hi
    drift_stats = ' / '.join(_fmt_stat(cl[k]) for k in ('min', 'median', 'mean', 'p90', 'p95', 'p99', 'max'))
    case_exceeds = ' / '.join(f"{ex['count']} ({_fmt(ex['pct'])}%)" for ex in cl['exceeds'].values())
    lines = [
        '# Aggregate batch report (Flowscope Track 1)',
        '',
        f"- generated: {report['created']} by `{report['tool']}` v{report['tool_version']}",
        f"- command: `{report['command']}`",
        f"- batches: {len(batches)} | cases: {report['n_cases']}",
        f"- triangle budget: {args.budget:,} (hard window {lo:,}-{hi:,})",
        '',
        '## Aggregate (all batches)',
        '',
        '| metric | value |',
        '| --- | --- |',
        f"| cases in {lo:,}-{hi:,} window | {agg['in_window']} |",
        f"| final triangles min / max / mean | {_fmt(agg['final_min'])} / {_fmt(agg['final_max'])} / {_fmt(agg['final_mean'])} |",
        f"| reduction % min / max / mean | {_fmt(agg['reduction_min'])} / {_fmt(agg['reduction_max'])} / {_fmt(agg['reduction_mean'])} |",
        f"| volume drift % (case max) min / median / mean / p90 / p95 / p99 / max | {drift_stats} |",
        f"| cases over 0.5% / 1% / 5% volume drift | {case_exceeds} |",
        f"| GLB bytes min / max / mean | {_fmt(agg['glb_bytes_min'])} / {_fmt(agg['glb_bytes_max'])} / {_fmt(agg['glb_bytes_mean'])} |",
        f"| runtime total s | {_fmt(agg['runtime_total_s'])} |",
    ]
    lines += render_drift_md(report)
    lines += [
        '',
        '## Per-batch',
        '',
        f'| batch | created | cases | in window | final min | final max | final mean | reduction mean % | drift max % | GLB mean bytes | runtime s |',
        '| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |',
    ]
    for batch in report['batches']:
        a = batch['aggregate']
        lines.append(
            f"| {batch['batch_id']} | {batch['created']} | {batch['n_cases']} "
            f"| {a['in_window']} | {_fmt(a['final_min'])} | {_fmt(a['final_max'])} "
            f"| {_fmt(a['final_mean'])} | {_fmt(a['reduction_mean'])} "
            f"| {_fmt(a['drift_max'])} | {_fmt(a['glb_bytes_mean'])} "
            f"| {_fmt(a['runtime_total_s'])} |"
        )
    lines += [
        '',
        '## Per-case',
        '',
        f'| batch | case | source | input tris | final tris | in window ({lo:,}-{hi:,}) | reduction % | drift % max | GLB bytes | runtime s | integrity | anomaly |',
        '| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |',
    ]
    for case in report['cases']:
        window = 'yes' if case['within_window'] else 'no'
        integrity = 'ok' if case['integrity_ok'] else 'FAIL'
        anomaly = _cell(case['anomaly'] or '-')
        lines.append(
            f"| {case['batch_id']} | {case['case_id']} | `{case['source']}` "
            f"| {_fmt(case['input_triangles'])} | {_fmt(case['final_triangles'])} "
            f"| {window} | {_fmt(case['reduction_pct'])} "
            f"| {_fmt(case['volume_drift_pct_max'])} | {_fmt(case['glb_bytes'])} "
            f"| {_fmt(case['runtime_s'])} | {integrity} | {anomaly} |"
        )
    anomalies = [c for c in report['cases'] if c['anomaly']]
    lines += ['', '## Anomalies', '']
    if anomalies:
        for case in anomalies:
            lines.append(
                f"- `{case['batch_id']}/{case['case_id']}`: {_cell(case['anomaly'])}"
            )
    else:
        lines.append('none')
    lines.append('')
    return '\n'.join(lines)


def main(argv=None) -> int:
    args = parse_args(argv)
    batches = load_batches(Path(args.batches_dir))
    if not batches:
        raise SystemExit(f'no */report.json under {args.batches_dir}')

    created = datetime.now(timezone.utc).isoformat(timespec='seconds')
    command = shlex.join([sys.executable, *sys.argv])
    cases = [
        {'batch_id': batch['batch_id'], **case}
        for batch in batches
        for case in batch['cases']
    ]
    per_batch = [
        {
            'batch_id': batch['batch_id'],
            'created': batch['created'],
            'n_cases': batch['n_cases'],
            'aggregate': batch['aggregate'],
        }
        for batch in batches
    ]
    report = {
        'batch_id': 'aggregate',
        'created': created,
        'n_cases': len(cases),
        'cases': cases,
        'aggregate': aggregate_block(cases),
        'drift_distribution': compute_drift_distribution(cases, Path(args.batches_dir)),
        'batches': per_batch,
        'tool': TOOL,
        'tool_version': TOOL_VERSION,
        'command': command,
    }
    json_path = Path(args.out_json)
    md_path = Path(args.out_md)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, indent=2) + '\n')
    md_path.write_text(render_md(report, batches, args))
    print(f'wrote {json_path} and {md_path} ({len(batches)} batches, {len(cases)} cases)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
