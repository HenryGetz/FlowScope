#!/usr/bin/env node
/**
 * Dependency-free HTTPS static server for viewer/dist/ — the Quest Browser
 * delivery path (WebXR requires a secure context). Usage:
 *
 *   node serve-https.mjs [--host <addr>] [--port <n>]
 *
 * Defaults: 0.0.0.0:8443. Certificates come from viewer/certs/ (npm run cert).
 */
import { createReadStream, existsSync, readFileSync, statSync } from 'node:fs';
import https from 'node:https';
import { networkInterfaces } from 'node:os';
import { dirname, extname, join, normalize, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = dirname(fileURLToPath(import.meta.url));
const distDir = join(root, 'dist');
const certDir = join(root, 'certs');

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.map': 'application/json; charset=utf-8',
  '.glb': 'model/gltf-binary',
  '.gltf': 'model/gltf+json',
  '.bin': 'application/octet-stream',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.gif': 'image/gif',
  '.webp': 'image/webp',
  '.svg': 'image/svg+xml',
  '.ico': 'image/x-icon',
  '.wasm': 'application/wasm',
  '.txt': 'text/plain; charset=utf-8',
  '.woff': 'font/woff',
  '.woff2': 'font/woff2',
  '.mp3': 'audio/mpeg',
  '.mp4': 'video/mp4',
};

let host = '0.0.0.0';
let port = 8443;
const argv = process.argv.slice(2);
for (let i = 0; i < argv.length; i += 1) {
  const arg = argv[i];
  const next = argv[i + 1];
  if (arg === '--host' && next && !next.startsWith('--')) {
    host = next;
    i += 1;
  } else if (arg.startsWith('--host=')) {
    host = arg.slice('--host='.length);
  } else if (arg === '--port' && next && !next.startsWith('--')) {
    port = Number(next);
    i += 1;
  } else if (arg.startsWith('--port=')) {
    port = Number(arg.slice('--port='.length));
  }
}
if (!Number.isInteger(port) || port < 1 || port > 65535) {
  console.error('invalid --port: expected an integer in 1..65535');
  process.exit(1);
}

const certPath = join(certDir, 'cert.pem');
const keyPath = join(certDir, 'key.pem');
if (!existsSync(certPath) || !existsSync(keyPath)) {
  console.error(`missing ${certPath} / ${keyPath} — run "npm run cert" first (WebXR needs HTTPS).`);
  process.exit(1);
}
if (!existsSync(distDir)) {
  console.error(`missing build output ${distDir} — run "npm run build" first.`);
  process.exit(1);
}

function statOrNull(path) {
  try {
    return statSync(path);
  } catch {
    return null;
  }
}

function send(response, status, body) {
  response.writeHead(status, {
    'Content-Type': 'text/plain; charset=utf-8',
    'Content-Length': Buffer.byteLength(body),
    'Cache-Control': 'no-cache',
  });
  response.end(body);
}

const server = https.createServer(
  { cert: readFileSync(certPath), key: readFileSync(keyPath) },
  (request, response) => {
    if (request.method !== 'GET' && request.method !== 'HEAD') {
      send(response, 405, 'method not allowed\n');
      return;
    }
    let pathname;
    try {
      pathname = decodeURIComponent(new URL(request.url, 'https://localhost/').pathname);
    } catch {
      send(response, 400, 'bad request\n');
      return;
    }
    if (pathname.endsWith('/')) pathname += 'index.html';

    let target = normalize(join(distDir, pathname.replace(/^\/+/, '')));
    if (target !== distDir && !target.startsWith(distDir + sep)) {
      send(response, 403, 'forbidden\n');
      return;
    }
    let stat = statOrNull(target);
    if (stat && stat.isDirectory()) {
      target = join(target, 'index.html');
      stat = statOrNull(target);
    }
    if (!stat || !stat.isFile()) {
      send(response, 404, `not found: ${pathname}\n`);
      return;
    }

    const type = MIME[extname(target).toLowerCase()] || 'application/octet-stream';
    response.writeHead(200, {
      'Content-Type': type,
      'Content-Length': stat.size,
      'Cache-Control': 'no-cache',
    });
    if (request.method === 'HEAD') {
      response.end();
      return;
    }
    const stream = createReadStream(target);
    stream.on('error', () => response.destroy());
    stream.pipe(response);
  },
);

function lanIPv4() {
  const addresses = new Set();
  for (const infos of Object.values(networkInterfaces())) {
    for (const info of infos || []) {
      if ((info.family === 'IPv4' || info.family === 4) && !info.internal && info.address) {
        addresses.add(info.address);
      }
    }
  }
  return Array.from(addresses);
}

server.listen(port, host, () => {
  console.log(`FlowScope viewer serving ${distDir}`);
  console.log(`  local:  https://localhost:${port}/`);
  for (const address of lanIPv4()) {
    console.log(`  quest:  https://${address}:${port}/  (accept the self-signed warning once)`);
  }
});
