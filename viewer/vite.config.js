import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vite';

const root = dirname(fileURLToPath(import.meta.url));

export default defineConfig({
  base: './',
  publicDir: 'public',
  build: {
    target: 'esnext',
    outDir: 'dist',
    rollupOptions: {
      input: {
        // `/` is the landing hub; the viewer app itself lives at app.html so
        // the root can list every view (anatomy + the three CFD cases).
        landing: resolve(root, 'index.html'),
        app: resolve(root, 'app.html'),
        'xr-smoke': resolve(root, 'xr-smoke.html'),
      },
    },
  },
});
