#!/usr/bin/env python3
"""Batch cardiac GLB build + integrity validation (Track 1).

Runs ``pipeline/build_cardiac_glb.py`` once per case and integrity-checks each
emitted GLB (glTF magic/version, 4-byte-aligned chunks, trimesh load, triangle
count vs the pipeline report, KHR_mesh_quantization). Writes::

    out/batches/<batch_id>/<case>.glb     quantized GLB per case
    out/batches/<batch_id>/<case>.json    pipeline report per case
    out/batches/<batch_id>/report.json    batch report (shared contract 2)

Cases come from ``--cases-glob`` (repeatable; matched directories are TS mask
dirs ingested with ``--ts-dir``, matched files are multilabel NIfTIs ingested
with ``--multilabel``) and/or ``--case-list`` (JSON list of
``{"case_id"?, "ts_dir"|"multilabel", "label_map"?}`` objects, or plain text:
one input path per line, ``#`` comments). Case ids default to the parent
directory name. Failures become ``anomaly`` rows and never abort the batch.

Usage::

    .venv/bin/python tools/batch_build.py \\
        --cases-glob 'data/raw/totalseg_ct/*/segmentations' \\
        --cases-glob 'data/raw/imagecas/*/label.nii.gz' \\
        --label-map out/imagecas_kaggle_label_map.json \\
        --batch-id batch-005 --budget 120000 --threads 4 --jobs 4
"""

from __future__ import annotations

import argparse
import glob as globmod
import json
import os
import shlex
import struct
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import trimesh

TOOL = 'tools/batch_build.py'
TOOL_VERSION = 1

REPO_ROOT = Path(__file__).resolve().parent.parent

_GLB_MAGIC = b'glTF'
_GLB_VERSION = 2
_CHUNK_JSON = 0x4E4F534A
_CHUNK_BIN = 0x004E4942

# ids that collide with the batch-level artifact names
RESERVED_IDS = frozenset({'report', 'integrity'})


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description='Batch-build cardiac GLBs and integrity-check them (Track 1).'
    )
    parser.add_argument(
        '--cases-glob',
        action='append',
        default=[],
        metavar='PATTERN',
        help='repeatable glob; matched dir -> ts-dir case, matched file -> '
        'multilabel case',
    )
    parser.add_argument(
        '--case-list',
        action='append',
        default=[],
        metavar='FILE',
        help='JSON list of {"case_id"?, "ts_dir"|"multilabel", "label_map"?} '
        'objects, or plain text: one input path per line (# comments)',
    )
    parser.add_argument('--batch-id', required=True)
    parser.add_argument('--budget', type=int, default=120000)
    parser.add_argument(
        '--threads',
        type=int,
        default=None,
        help='VTK threads per case (default max(1, cpu // jobs))',
    )
    parser.add_argument(
        '--jobs', type=int, default=4, help='cases built in parallel (default 4)'
    )
    parser.add_argument(
        '--decimator', choices=('decimate_pro', 'quadric'), default='decimate_pro'
    )
    parser.add_argument(
        '--label-map', help='label map JSON for multilabel cases (required for them)'
    )
    parser.add_argument('--out-dir', default='out/batches')
    parser.add_argument(
        '--python',
        default=None,
        help='interpreter for the pipeline subprocess (default .venv/bin/python)',
    )
    parser.add_argument('--pipeline', default='pipeline/build_cardiac_glb.py')
    parser.add_argument(
        '--list-cases', action='store_true', help='resolve and print cases, then exit'
    )
    args = parser.parse_args(argv)
    if not args.cases_glob and not args.case_list:
        parser.error('need --cases-glob and/or --case-list')
    if args.jobs < 1:
        parser.error('--jobs must be >= 1')
    if args.threads is not None and args.threads < 1:
        parser.error('--threads must be >= 1')
    if args.threads is None:
        args.threads = max(1, (os.cpu_count() or 1) // args.jobs)
    if args.python is None:
        venv_python = REPO_ROOT / '.venv' / 'bin' / 'python'
        args.python = str(venv_python) if venv_python.exists() else sys.executable
    return args


def _case_id(path: Path) -> str:
    """Parent directory name; generic wrapper dirs use their parent instead."""
    if path.is_dir() and path.name not in ('segmentations', 'masks'):
        return path.name
    return path.parent.name or path.name


def _case_from_path(path: Path, label_map: str | None) -> dict:
    if path.is_dir():
        return {
            'case_id': _case_id(path),
            'mode': 'ts-dir',
            'path': str(path),
            'label_map': None,
        }
    if path.is_file():
        return {
            'case_id': _case_id(path),
            'mode': 'multilabel',
            'path': str(path),
            'label_map': label_map,
        }
    raise SystemExit(f'case path is neither file nor directory: {path}')


def _load_case_list(path: Path, label_map: str | None) -> list[dict]:
    raw = path.read_text()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        parsed = [parsed]
    if isinstance(parsed, list):
        cases = []
        for entry in parsed:
            if 'ts_dir' in entry:
                src, mode = entry['ts_dir'], 'ts-dir'
                entry_map = None
            elif 'multilabel' in entry:
                src, mode = entry['multilabel'], 'multilabel'
                entry_map = entry.get('label_map') or label_map
            else:
                raise SystemExit(
                    f'{path}: case entry needs "ts_dir" or "multilabel": {entry}'
                )
            cases.append(
                {
                    'case_id': entry.get('case_id') or _case_id(Path(src)),
                    'mode': mode,
                    'path': str(src),
                    'label_map': entry_map,
                }
            )
        return cases
    cases = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        cases.append(_case_from_path(Path(line), label_map))
    return cases


def discover_cases(args) -> list[dict]:
    cases: list[dict] = []
    for pattern in args.cases_glob:
        matches = sorted(globmod.glob(pattern))
        if not matches:
            raise SystemExit(f'--cases-glob matched nothing: {pattern}')
        cases.extend(_case_from_path(Path(m), args.label_map) for m in matches)
    for list_file in args.case_list:
        cases.extend(_load_case_list(Path(list_file), args.label_map))
    seen: set[str] = set()
    for case in cases:
        base = case['case_id']
        candidate, k = base, 2
        while candidate in seen or candidate in RESERVED_IDS:
            candidate = f'{base}-{k}'
            k += 1
        case['case_id'] = candidate
        seen.add(candidate)
    cases.sort(key=lambda c: c['case_id'])
    for case in cases:
        if case['mode'] == 'multilabel' and not case['label_map']:
            raise SystemExit(
                f"multilabel case {case['case_id']} needs --label-map "
                '(or a per-case "label_map" in --case-list)'
            )
    return cases


def integrity_check(glb_path, expected_triangles, expect_quantized: bool = True) -> dict:
    """Validate one GLB; return check results and observed triangle count.

    Checks: glTF magic, version 2, declared length, 4-byte-aligned well-formed
    chunks (single JSON + single BIN), JSON chunk parses, KHR_mesh_quantization
    present (when the build is quantized), trimesh loads the file, and the
    loaded triangle count equals the pipeline report's final triangle count.
    """
    checks: dict[str, bool] = {}
    observed = None
    data = Path(glb_path).read_bytes()

    def _fail_all():
        for key in (
            'glTF_magic',
            'glTF_version_2',
            'declared_length_matches',
            'chunks_4byte_aligned',
            'chunk_layout_json_bin',
            'json_chunk_parses',
            'KHR_mesh_quantization',
        ):
            checks.setdefault(key, False)

    if len(data) >= 12 and data[:4] == _GLB_MAGIC:
        checks['glTF_magic'] = True
        version, declared = struct.unpack_from('<II', data, 4)
        checks['glTF_version_2'] = version == _GLB_VERSION
        checks['declared_length_matches'] = declared == len(data)
        chunks: list[tuple[int, int]] = []  # (type, length)
        off = 12
        while off + 8 <= len(data):
            clen, ctype = struct.unpack_from('<II', data, off)
            chunks.append((ctype, clen))
            off += 8 + clen
        walk_complete = off == len(data)
        checks['chunks_4byte_aligned'] = walk_complete and all(
            clen % 4 == 0 for _, clen in chunks
        )
        checks['chunk_layout_json_bin'] = (
            walk_complete
            and len(chunks) == 2
            and chunks[0][0] == _CHUNK_JSON
            and chunks[1][0] == _CHUNK_BIN
        )
        gltf = None
        try:
            gltf = json.loads(data[20 : 20 + chunks[0][1]])
            checks['json_chunk_parses'] = True
        except Exception:
            checks['json_chunk_parses'] = False
        if expect_quantized:
            checks['KHR_mesh_quantization'] = bool(
                gltf and 'KHR_mesh_quantization' in gltf.get('extensionsUsed', [])
            )
        else:
            # non-quantized build: extension presence is not applicable
            checks['KHR_mesh_quantization'] = True
    else:
        _fail_all()

    try:
        scene = trimesh.load(str(glb_path), force='scene')
        observed = sum(len(g.faces) for g in scene.geometry.values())
        checks['trimesh_load'] = True
        checks['triangle_count_matches_report'] = observed == expected_triangles
    except Exception:
        checks['trimesh_load'] = False
        checks['triangle_count_matches_report'] = False

    ok = all(checks.values())
    failed = [key for key, value in checks.items() if not value]
    return {
        'ok': ok,
        'checks': checks,
        'expected_triangles': expected_triangles,
        'observed_triangles': observed,
        'reason': None if ok else 'failed checks: ' + ', '.join(failed),
    }


def run_case(case: dict, args, case_dir: Path) -> dict:
    """Build + verify one case; failures are folded into the ``anomaly`` field."""
    cid = case['case_id']
    glb_path = case_dir / f'{cid}.glb'
    report_path = case_dir / f'{cid}.json'
    cmd = [
        args.python,
        args.pipeline,
        '--out',
        str(glb_path),
        '--report',
        str(report_path),
        '--budget',
        str(args.budget),
        '--threads',
        str(args.threads),
        '--decimator',
        args.decimator,
    ]
    if case['mode'] == 'ts-dir':
        cmd += ['--ts-dir', case['path']]
    else:
        cmd += ['--multilabel', case['path'], '--label-map', case['label_map']]

    row = {
        'case_id': cid,
        'source': case['path'],
        'input_triangles': None,
        'final_triangles': None,
        'reduction_pct': None,
        'volume_drift_pct_max': None,
        'glb_bytes': None,
        'integrity_ok': False,
        'runtime_s': None,
        'decimator': args.decimator,
        'within_window': False,
        'anomaly': None,
        'checks': {},
    }
    problems: list[str] = []

    started = time.perf_counter()
    proc = subprocess.run(cmd, capture_output=True, text=True)
    row['runtime_s'] = round(time.perf_counter() - started, 2)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or '').strip().splitlines()[-3:]
        problems.append(
            f'pipeline exit {proc.returncode}'
            + (': ' + ' | '.join(tail) if tail else '')
        )

    report = None
    totals = None
    try:
        report = json.loads(report_path.read_text())
        totals = report['totals']
    except Exception as exc:
        problems.append(f'pipeline report unreadable: {exc}')

    if totals is not None:
        row['input_triangles'] = totals['aggregate_input_triangles']
        row['final_triangles'] = totals['aggregate_final_triangles']
        row['reduction_pct'] = totals['aggregate_reduction_pct']
        row['glb_bytes'] = totals['glb_bytes']
        row['decimator'] = totals['decimator']
        row['within_window'] = bool(totals['within_window'])
        drifts = [
            abs(s['volume_drift_pct'])
            for s in report['structures']
            if s.get('volume_drift_pct') is not None
        ]
        row['volume_drift_pct_max'] = round(max(drifts), 4) if drifts else None
        ingested = [s for s in report['structures'] if 'final_triangles' in s]
        if not ingested or not totals['aggregate_final_triangles']:
            problems.append('pipeline produced no structures')

    if totals is not None and glb_path.exists():
        result = integrity_check(
            glb_path,
            totals['aggregate_final_triangles'],
            expect_quantized=bool(totals['quantized']),
        )
    else:
        result = {
            'ok': False,
            'checks': {},
            'reason': f'glb or report missing (glb exists: {glb_path.exists()})',
        }
    row['checks'] = result['checks']
    row['integrity_ok'] = bool(result['ok'])
    if not result['ok']:
        problems.append('integrity: ' + str(result['reason']))

    row['anomaly'] = '; '.join(problems) if problems else None
    return row


def aggregate_block(rows: list[dict]) -> dict:
    """Shared contract 2 aggregate over case rows (stats on anomaly-free rows)."""

    def stat(values):
        values = [v for v in values if v is not None]
        if not values:
            return None, None, None
        return min(values), max(values), round(sum(values) / len(values), 2)

    ok = [r for r in rows if r['anomaly'] is None]
    final_min, final_max, final_mean = stat(r['final_triangles'] for r in ok)
    bytes_min, bytes_max, bytes_mean = stat(r['glb_bytes'] for r in ok)
    red_min, red_max, red_mean = stat(r['reduction_pct'] for r in ok)
    drifts = [
        r['volume_drift_pct_max'] for r in ok if r['volume_drift_pct_max'] is not None
    ]
    in_window = sum(1 for r in rows if r['within_window'])
    return {
        'in_window': f'{in_window}/{len(rows)}',
        'final_min': final_min,
        'final_max': final_max,
        'final_mean': final_mean,
        'drift_max': round(max(drifts), 4) if drifts else None,
        'glb_bytes_min': bytes_min,
        'glb_bytes_max': bytes_max,
        'glb_bytes_mean': bytes_mean,
        'runtime_total_s': round(sum(r['runtime_s'] or 0.0 for r in rows), 2),
        'reduction_min': red_min,
        'reduction_max': red_max,
        'reduction_mean': red_mean,
    }


def main(argv=None) -> int:
    args = parse_args(argv)
    cases = discover_cases(args)
    if args.list_cases:
        for case in cases:
            extra = f"  label_map={case['label_map']}" if case['label_map'] else ''
            print(f"{case['case_id']:<16}{case['mode']:<11}{case['path']}{extra}")
        return 0

    case_dir = Path(args.out_dir) / args.batch_id
    case_dir.mkdir(parents=True, exist_ok=True)
    created = datetime.now(timezone.utc).isoformat(timespec='seconds')
    command = shlex.join([sys.executable, *sys.argv])
    print(
        f"[batch {args.batch_id}] {len(cases)} cases  "
        f'jobs={args.jobs} threads/case={args.threads} budget={args.budget}'
    )

    rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = {
            pool.submit(run_case, case, args, case_dir): case for case in cases
        }
        for future in as_completed(futures):
            row = future.result()
            rows.append(row)
            mark = 'OK     ' if row['anomaly'] is None else 'ANOMALY'
            line = (
                f"[{mark}] {row['case_id']}: final={row['final_triangles']} tris, "
                f"{row['runtime_s']}s, integrity_ok={row['integrity_ok']}"
            )
            if row['anomaly']:
                line += f" -- {row['anomaly']}"
            print(line)

    rows.sort(key=lambda r: r['case_id'])
    report = {
        'batch_id': args.batch_id,
        'created': created,
        'n_cases': len(rows),
        'cases': rows,
        'aggregate': aggregate_block(rows),
        'tool': TOOL,
        'tool_version': TOOL_VERSION,
        'command': command,
    }
    report_path = case_dir / 'report.json'
    report_path.write_text(json.dumps(report, indent=2) + '\n')
    print(f'wrote {report_path}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
