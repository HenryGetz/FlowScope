#!/usr/bin/env node
/**
 * Headless HTTP(S) load-time probe (Track 2 hardware pre-staging).
 *
 * Pure HTTP timing against the LAN HTTPS viewer endpoint: NO browser, NO
 * renderer, NO frame-rate measurement. Each resource is fetched `--samples`
 * times (default 3) sequentially over a fresh TLS connection per sample
 * (cold-connection timing, `accept-encoding: identity` so byte counts are
 * uncompressed wire payload) and min/median/max are reported per metric.
 *
 * Metrics per sample:
 *   ttfb_ms     request start -> response headers (first byte of the response)
 *   download_ms response headers -> last body byte (transfer)
 *   total_ms    request start -> last body byte (ttfb + download)
 *   bytes       body bytes received
 *
 * Usage:
 *   node tools/loadtime_probe.mjs [--base https://192.168.1.229:8444] [--samples 3]
 *
 * Prints one self-describing JSON document to stdout (tools/hw_precheck.py
 * embeds it as the `loadtime` section of out/track2_metrics.json). Exit code 0
 * even when individual resources fail; failures are recorded per resource.
 */
import http from 'node:http';
import https from 'node:https';
import { performance } from 'node:perf_hooks';

const TOOL_NAME = 'tools/loadtime_probe.mjs';
const TOOL_VERSION = '1.0.0';
const DEFAULT_BASE = 'https://192.168.1.229:8444';
const REQUEST_TIMEOUT_MS = 30000;

/**
 * Resources measured: the page (root + a `?model=` HTML fetch) and every GLB
 * asset the viewer serves. `glb` maps a served asset to its out/ source file
 * (out/cardiac_s0004.glb is served under the default asset name cardiac.glb;
 * the two files are byte-identical, md5 384cc1145161ce0f9f69967d358df0b8).
 */
const RESOURCES = [
  { name: 'page_root', url: '/', kind: 'page_html' },
  {
    name: 'page_model_coronary_601',
    url: '/?model=assets/coronary_601.glb',
    kind: 'page_html',
  },
  { name: 'coronary_601', url: '/assets/coronary_601.glb', kind: 'glb', glb: 'out/coronary_601.glb' },
  { name: 'coronary_700', url: '/assets/coronary_700.glb', kind: 'glb', glb: 'out/coronary_700.glb' },
  { name: 'coronary_798', url: '/assets/coronary_798.glb', kind: 'glb', glb: 'out/coronary_798.glb' },
  {
    name: 'cardiac_s0004',
    url: '/assets/cardiac.glb',
    kind: 'glb',
    glb: 'out/cardiac_s0004.glb',
    note: 'served under the viewer default asset name assets/cardiac.glb (byte-identical to out/cardiac_s0004.glb)',
  },
  {
    name: 'cardiac_s0015',
    url: '/assets/cardiac_s0015.glb',
    kind: 'glb',
    glb: 'out/cardiac_s0015.glb',
  },
  {
    name: 'single_aorta',
    url: '/assets/single_aorta.glb',
    kind: 'glb',
    glb: 'out/single_aorta.glb',
  },
];

let base = DEFAULT_BASE;
let samples = 3;
const argv = process.argv.slice(2);
for (let i = 0; i < argv.length; i += 1) {
  const arg = argv[i];
  const next = argv[i + 1];
  if (arg === '--base' && next && !next.startsWith('--')) {
    base = next.replace(/\/+$/, '');
    i += 1;
  } else if (arg.startsWith('--base=')) {
    base = arg.slice('--base='.length).replace(/\/+$/, '');
  } else if (arg === '--samples' && next && !next.startsWith('--')) {
    samples = Number(next);
    i += 1;
  } else if (arg.startsWith('--samples=')) {
    samples = Number(arg.slice('--samples='.length));
  } else if (arg === '--help' || arg === '-h') {
    console.log(`usage: node ${TOOL_NAME} [--base ${DEFAULT_BASE}] [--samples 3]`);
    process.exit(0);
  } else {
    console.error(`unknown argument: ${arg}`);
    process.exit(1);
  }
}
if (!Number.isInteger(samples) || samples < 1) {
  console.error('invalid --samples: expected a positive integer');
  process.exit(1);
}

// Self-signed LAN certificate: explicitly opted out of verification (documented
// assumption of the WebXR delivery path; the user accepts the warning once).
const tlsAgent = new https.Agent({ rejectUnauthorized: false, keepAlive: false });

/** One timed GET. Resolves never-rejects: errors come back as `error`. */
function timedGet(url) {
  return new Promise((resolve) => {
    const isTls = url.startsWith('https:');
    const t0 = performance.now();
    let status = 0;
    let tHeaders = null;
    let tEnd = null;
    let bytes = 0;
    const request = (isTls ? https : http).get(
      url,
      {
        agent: isTls ? tlsAgent : undefined,
        headers: { 'accept-encoding': 'identity', connection: 'close' },
      },
      (response) => {
        status = response.statusCode || 0;
        tHeaders = performance.now();
        response.on('data', (chunk) => {
          bytes += chunk.length;
        });
        response.on('end', () => {
          tEnd = performance.now();
          resolve({
            status,
            ttfb_ms: round3(tHeaders - t0),
            download_ms: round3(tEnd - tHeaders),
            total_ms: round3(tEnd - t0),
            bytes,
            error: status === 200 ? null : `HTTP ${status}`,
          });
        });
        response.on('error', (err) => {
          resolve({ status, bytes, error: `body: ${message(err)}` });
        });
      },
    );
    request.setTimeout(REQUEST_TIMEOUT_MS, () => {
      request.destroy(new Error(`timeout after ${REQUEST_TIMEOUT_MS} ms`));
    });
    request.on('error', (err) => {
      resolve({ status, bytes, error: message(err) });
    });
  });
}

function message(err) {
  return String((err && err.message) || err);
}

function round3(value) {
  return Math.round(value * 1000) / 1000;
}

function stats(values) {
  if (values.length === 0) return { min: null, median: null, max: null, samples: [] };
  const sorted = [...values].sort((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  const median =
    sorted.length % 2 === 1 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
  return { min: sorted[0], median: round3(median), max: sorted[sorted.length - 1], samples: sorted };
}

async function probeResource(resource) {
  const rows = [];
  for (let i = 0; i < samples; i += 1) {
    rows.push(await timedGet(base + resource.url));
  }
  const ok = rows.filter((row) => !row.error);
  const out = {
    name: resource.name,
    url: base + resource.url,
    kind: resource.kind,
    samples_requested: samples,
    samples_ok: ok.length,
    status: rows.length > 0 ? rows[rows.length - 1].status || null : null,
    bytes: ok.length > 0 ? ok[0].bytes : 0,
    ttfb_ms: stats(ok.map((row) => row.ttfb_ms)),
    download_ms: stats(ok.map((row) => row.download_ms)),
    total_ms: stats(ok.map((row) => row.total_ms)),
    raw: rows,
  };
  if (resource.glb) out.glb = resource.glb;
  if (resource.note) out.note = resource.note;
  const errors = rows.filter((row) => row.error).map((row) => row.error);
  if (errors.length > 0) out.errors = errors;
  return out;
}

const resources = [];
for (const resource of RESOURCES) {
  resources.push(await probeResource(resource));
}

const document = {
  schema_version: 1,
  tool: { name: TOOL_NAME, version: TOOL_VERSION, command: `node ${process.argv.slice(1).join(' ')}`, node: process.version },
  generated: new Date().toISOString(),
  base,
  samples_per_resource: samples,
  method: {
    transport: 'HTTP(S) GET via node:https/node:http (no browser, no renderer)',
    tls: 'self-signed certificate accepted (rejectUnauthorized: false) — LAN WebXR delivery path',
    connection: 'fresh TLS connection per sample (cold-connection timing), connection: close',
    encoding: 'accept-encoding: identity (byte counts are uncompressed wire payload)',
    sequencing: 'resources and samples fetched sequentially (no request contention)',
    ttfb_ms: 'request start -> response headers (first byte of the HTTP response)',
    download_ms: 'response headers -> last body byte (transfer)',
    total_ms: 'request start -> last body byte (ttfb + download)',
    note: 'pure HTTP timing; no frame-rate or rendering measurement of any kind',
  },
  resources,
};

process.stdout.write(`${JSON.stringify(document, null, 2)}\n`);
