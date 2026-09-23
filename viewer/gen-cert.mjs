#!/usr/bin/env node
/**
 * Generate a self-signed dev certificate for the Quest Browser LAN path.
 * SANs cover localhost, 127.0.0.1 and every host IPv4 from
 * os.networkInterfaces(). Writes viewer/certs/cert.pem + key.pem via the
 * openssl CLI. Zero npm dependencies.
 */
import { execFileSync } from 'node:child_process';
import { existsSync, mkdirSync } from 'node:fs';
import { networkInterfaces } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = dirname(fileURLToPath(import.meta.url));
const certDir = join(root, 'certs');
const certPath = join(certDir, 'cert.pem');
const keyPath = join(certDir, 'key.pem');

// SANs: localhost + 127.0.0.1 + every host IPv4
const addresses = new Set(['127.0.0.1']);
for (const infos of Object.values(networkInterfaces())) {
  for (const info of infos || []) {
    if ((info.family === 'IPv4' || info.family === 4) && info.address) {
      addresses.add(info.address);
    }
  }
}
const sans = ['DNS:localhost', ...Array.from(addresses, (address) => `IP:${address}`)];

mkdirSync(certDir, { recursive: true });

try {
  execFileSync(
    'openssl',
    [
      'req', '-x509', '-newkey', 'rsa:2048', '-sha256', '-days', '365', '-nodes',
      '-keyout', keyPath,
      '-out', certPath,
      '-subj', '/CN=localhost/O=FlowScope',
      '-addext', `subjectAltName=${sans.join(',')}`,
      '-addext', 'keyUsage=digitalSignature,keyEncipherment',
      '-addext', 'extendedKeyUsage=serverAuth',
    ],
    { stdio: ['ignore', 'pipe', 'pipe'] },
  );
} catch (err) {
  if (err.code === 'ENOENT') {
    console.error('openssl CLI not found on PATH — install openssl to generate the dev certificate.');
  } else {
    console.error(`openssl failed: ${err.stderr ? String(err.stderr) : err.message}`);
  }
  process.exit(1);
}

if (!existsSync(certPath) || !existsSync(keyPath)) {
  console.error('openssl reported success but the certificate files are missing');
  process.exit(1);
}

console.log(`wrote ${certPath}`);
console.log(`wrote ${keyPath}`);
console.log(`subjectAltName=${sans.join(',')}`);
