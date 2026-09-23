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


def _fmt(value) -> str:
    if value is None:
        return 'n/a'
    if isinstance(value, float):
        return f'{value:,.2f}'.rstrip('0').rstrip('.')
    return f'{value:,}' if isinstance(value, int) else str(value)


def _cell(value) -> str:
    """Markdown table cell: escape pipes (anomaly traces contain them)."""
    return str(value).replace('|', '\\|')


def render_md(report: dict, batches: list[dict], args) -> str:
    agg = report['aggregate']
    lo, hi = args.window_lo, args.window_hi
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
        f"| volume drift % max | {_fmt(agg['drift_max'])} |",
        f"| GLB bytes min / max / mean | {_fmt(agg['glb_bytes_min'])} / {_fmt(agg['glb_bytes_max'])} / {_fmt(agg['glb_bytes_mean'])} |",
        f"| runtime total s | {_fmt(agg['runtime_total_s'])} |",
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
