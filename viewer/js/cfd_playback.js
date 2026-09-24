import * as THREE from 'three';
import { modelURL, assetsBaseURL, payloadURL } from '../src/loader.js';

/**
 * CFD contrast playback (contract C5 payload):
 *   case_{case}_{profile}_contrast.bin      row-major K x V uint8 (frame-major rows)
 *   case_{case}_{profile}_metadata.json     flowscope.cfd.contrast v1
 *
 * The K x V block is decoded once into a Uint8Array and streamed to the GPU as
 * two per-mesh uint8 BufferAttributes `aC0`/`aC1` (normalized: false) plus a
 * `uMix` uniform: `time01` -> keyframe index k + frac, and only when k advances
 * are the two frame slices copied into the per-mesh attributes (never a full
 * K x V upload). The attributes are attached AFTER the GLTF load/decode so the
 * loader's Float32 rewrite of normalized attributes never touches them.
 *
 * Rendering (playback-enabled mesh materials only): an iodine transfer
 * function overlays the lit surface — transparent amber at C = 0 rising along
 * an opacity ramp to dense radiopaque white at C = 1. A `uContrastOn` gate
 * keeps the overlay inert until a payload is actually loaded, so a missing
 * payload degrades to the untouched anatomy.
 */

const TAP_EPS = 1e-6;

/** Iodine contrast transfer function: transparent amber -> radiopaque white. */
const CONTRAST_TF_GLSL = `
vec4 iodineTransfer(float c) {
  c = clamp(c, 0.0, 1.0);
  vec3 amber = vec3(0.93, 0.60, 0.16);
  vec3 white = vec3(1.0, 1.0, 1.0);
  vec3 rgb = mix(amber, white, smoothstep(0.15, 0.9, c));
  float a = mix(0.15, 1.0, smoothstep(0.0, 0.85, c));
  return vec4(rgb, a);
}
`;

const VERTEX_HEAD = `
attribute float aC0;
attribute float aC1;
uniform float uMix;
varying float vContrast;
`;

const FRAGMENT_HEAD = `
varying float vContrast;
uniform float uContrastOn;
${CONTRAST_TF_GLSL}
`;

const VERTEX_BODY = `
  float c = mix(aC0, aC1, uMix) / 255.0;
  vContrast = clamp(c, 0.0, 1.0);
`;

const FRAGMENT_BODY = `
  vec4 ct = iodineTransfer(vContrast);
  float w = ct.a * uContrastOn;
  gl_FragColor.rgb = mix(gl_FragColor.rgb, ct.rgb, w);
  gl_FragColor.a = mix(gl_FragColor.a, ct.a, uContrastOn);
`;

/** Case id for `load()` when absent: `?case=<id>` else digits in the model URL. */
export function initialCaseId() {
  const fromQuery = new URLSearchParams(window.location.search).get('case');
  const trimmed = fromQuery ? fromQuery.trim() : '';
  if (trimmed) return trimmed;
  const match = /(\d{3})/.exec(decodeURIComponent(modelURL()));
  return match ? match[1] : '';
}

/** Profile for `load()` when absent: `?profile=<A|B|C>` else 'A'. */
export function initialProfile() {
  const fromQuery = new URLSearchParams(window.location.search).get('profile');
  const trimmed = fromQuery ? fromQuery.trim().toUpperCase() : '';
  return trimmed.length === 1 && 'ABC'.includes(trimmed) ? trimmed : 'A';
}

/** Resolve `base` as a directory URL (default: the model URL's base). */
function dirURL(base) {
  const href = typeof base === 'string' && base ? base : assetsBaseURL();
  const url = new URL(href, document.baseURI);
  return url.href.endsWith('/') ? url : new URL(`${url.href}/`);
}

function payloadNames(caseId, profile) {
  const stem = `case_${caseId}_${profile}`;
  return { meta: `${stem}_metadata.json`, bin: `${stem}_contrast.bin` };
}

/**
 * Payload URLs, `?payload=<url>` override modeled on modelURL():
 *  - `*_metadata.json` (or any .json): metadata URL; bin via its `bin_file`
 *  - `*_contrast.bin` (or any .bin): bin URL; metadata beside it
 *  - anything else: a base directory holding the derived `case_...` names
 * Default: the derived names inside the model URL's `assets/` base.
 */
function resolvePayloadURLs(caseId, profile) {
  const names = payloadNames(caseId, profile);
  const override = payloadURL();
  if (override) {
    if (override.endsWith('.json')) {
      return { meta: new URL(override, document.baseURI), bin: null };
    }
    if (override.endsWith('.bin')) {
      const bin = new URL(override, document.baseURI);
      const meta = new URL(
        override.replace(/_contrast\.bin$/, '_metadata.json').replace(/\.bin$/, '.json'),
        bin,
      );
      return { meta, bin };
    }
    const base = dirURL(override);
    return { meta: new URL(names.meta, base), bin: new URL(names.bin, base) };
  }
  const base = dirURL();
  return { meta: new URL(names.meta, base), bin: new URL(names.bin, base) };
}

async function fetchOK(url, kind) {
  const response = await fetch(url);
  if (!response.ok) {
    throw new Error(`failed to fetch ${kind} "${url.href}": HTTP ${response.status}`);
  }
  return response;
}

function positionCount(object) {
  const geometry = object && object.geometry;
  const attr = geometry && geometry.getAttribute && geometry.getAttribute('position');
  return attr ? attr.count : 0;
}

function clamp01(value) {
  return Math.min(1, Math.max(0, Number(value) || 0));
}

/** Readout rendering for nullable metadata fields (JSON null, contract C2). */
const NA_READOUT = 'n/a';

/**
 * Readout text for one nullable `branches[]` field: `n/a` for JSON null (never
 * `null ms`/NaN) and byte-identical HUD numeric text otherwise (`<round> ms`
 * for the millisecond fields, plain digits for the TIMI frame count).
 */
function readoutText(value, unit) {
  if (!Number.isFinite(value)) return NA_READOUT;
  return unit === 'frames' ? `${Math.round(value)}` : `${Math.round(value)} ms`;
}

/**
 * Null-safe readout strings for one metadata branch row: `transit` renders
 * `transit_ms`, `arrival` renders `t_arrival_ms`, `peak` renders `t_peak_ms`,
 * `frames` renders `timi_frames_30fps`; each is `n/a` when its field is null
 * (a null/absent row renders all-`n/a`).
 */
function branchReadout(branch) {
  const row = branch || {};
  return {
    branch: row.name || null,
    transit: readoutText(row.transit_ms, 'ms'),
    arrival: readoutText(row.t_arrival_ms, 'ms'),
    peak: readoutText(row.t_peak_ms, 'ms'),
    frames: readoutText(row.timi_frames_30fps, 'frames'),
  };
}

/**
 * Distal transit-time readout source (metadata `branches`): prioritize
 * `major`/`distal` flagged branches and report the slowest `transit_ms`
 * (root -> distal convective delay) among them. Rows without a finite
 * `transit_ms` (JSON null on negligible-mean-flow branches) never win the
 * selection; `readout` renders every nullable field of the selected row
 * (see branchReadout) so nulls surface as `n/a`, never `null ms`/NaN.
 */
function selectTransit(branches) {
  const list = (branches || []).filter((b) => b && Number.isFinite(b.transit_ms));
  const prioritized = list.filter((b) => b.major || b.distal);
  const pool = prioritized.length > 0 ? prioritized : list;
  if (pool.length === 0) {
    return { transitMs: null, transitBranch: null, readout: branchReadout(null) };
  }
  let best = pool[0];
  for (const branch of pool) {
    if (branch.transit_ms > best.transit_ms) best = branch;
  }
  return {
    transitMs: best.transit_ms,
    transitBranch: best.name || null,
    readout: branchReadout(best),
  };
}

/**
 * Contrast playback controller.
 *
 * @param {object} options
 * @param {THREE.Scene} [options.scene] scene fallback root when `gltf` is absent
 * @param {object} [options.stage] rendering stage (API parity; unused internals)
 * @param {object} [options.gltf] loaded GLTF (uses `gltf.scene` as the mesh root)
 * @param {string} [options.assetsBase] payload base URL (default: model URL's base)
 * @param {(state: object) => void} [options.onReadout] state-change readout
 * @returns {object} {load, play, pause, setSpeed, setProfile, scrub, update, dispose}
 */
export function createCfdPlayback({ scene, stage, gltf, assetsBase, onReadout = () => {} }) {
  const root = (gltf && gltf.scene) || scene;
  // Shared uniform objects: every playback program reads the same instances.
  const uniforms = {
    uMix: { value: 0 },
    uContrastOn: { value: 0 },
  };

  /** mesh objects that carry playback attributes (insertion = file order). */
  const meshes = [];
  /** materials patched with the contrast shader (+ their pre-playback state). */
  const materials = [];
  /** profile -> decoded payload */
  const payloads = new Map();

  let caseId = '';
  let profile = 'A';
  let payload = null; // active: { meta, bytes, frames, vertices, ts, t0, duration }
  let time01 = 0;
  let playing = false;
  let loop = true;
  let speed = 1;
  let frameK = -1;
  let transitMs = null;
  let transitBranch = null;
  let transitReadout = branchReadout(null);
  let lastEmit = '';

  // ---- shader / attributes (attach post-decode, never through the loader) --

  function ensureAttributes(geometry) {
    const count = positionCount({ geometry });
    if (count === 0) return null;
    let attr0 = geometry.getAttribute('aC0');
    let attr1 = geometry.getAttribute('aC1');
    if (!attr0 || attr0.count !== count) {
      attr0 = new THREE.BufferAttribute(new Uint8Array(count), 1, false);
      geometry.setAttribute('aC0', attr0);
    }
    if (!attr1 || attr1.count !== count) {
      attr1 = new THREE.BufferAttribute(new Uint8Array(count), 1, false);
      geometry.setAttribute('aC1', attr1);
    }
    return { attr0, attr1, count };
  }

  /**
   * Patch one scene node for playback: inject the contrast shader into every
   * material and attach the zero-filled `aC0`/`aC1` attributes. Called from
   * main's initClipping traverse (post GLTF load/decode); safe to re-call.
   */
  function applyShader(object) {
    if (!object || !object.isMesh || !object.geometry) return;
    const attrs = ensureAttributes(object.geometry);
    if (!attrs) return;
    if (!meshes.includes(object)) meshes.push(object);
    const list = Array.isArray(object.material) ? object.material : [object.material];
    for (const material of list) {
      if (!material || material.userData.cfdContrast) continue;
      material.userData.cfdContrast = true;
      const prev = {
        onBeforeCompile: material.onBeforeCompile,
        customProgramCacheKey: material.customProgramCacheKey,
        transparent: material.transparent,
        depthWrite: material.depthWrite,
      };
      material.onBeforeCompile = (shader) => {
        shader.uniforms.uMix = uniforms.uMix;
        shader.uniforms.uContrastOn = uniforms.uContrastOn;
        shader.vertexShader =
          VERTEX_HEAD +
          shader.vertexShader.replace(
            '#include <begin_vertex>',
            `#include <begin_vertex>${VERTEX_BODY}`,
          );
        const head = FRAGMENT_HEAD;
        if (shader.fragmentShader.includes('#include <dithering_fragment>')) {
          shader.fragmentShader =
            head +
            shader.fragmentShader.replace(
              '#include <dithering_fragment>',
              `#include <dithering_fragment>${FRAGMENT_BODY}`,
            );
        } else if (shader.fragmentShader.includes('#include <opaque_fragment>')) {
          shader.fragmentShader =
            head +
            shader.fragmentShader.replace(
              '#include <opaque_fragment>',
              `#include <opaque_fragment>${FRAGMENT_BODY}`,
            );
        } else {
          console.warn('[cfd_playback] no fragment injection point in', material.type);
          shader.fragmentShader = head + shader.fragmentShader;
        }
      };
      material.customProgramCacheKey = () => 'cfd-contrast-v1';
      materials.push({ material, prev });
    }
  }

  /** Flip the playback materials onto the opacity-ramp look (or back). */
  function applyActivation() {
    const active = !!payload;
    uniforms.uContrastOn.value = active ? 1 : 0;
    for (const { material, prev } of materials) {
      const transparent = active ? true : prev.transparent;
      const depthWrite = active ? true : prev.depthWrite;
      if (material.transparent !== transparent || material.depthWrite !== depthWrite) {
        material.transparent = transparent;
        material.depthWrite = depthWrite;
        material.needsUpdate = true;
      }
    }
  }

  // ---- metadata mesh mapping ----------------------------------------------

  /** Locate the scene mesh for a metadata entry (node, primitive_index). */
  function findMeshObject(entry) {
    const want = entry.vertex_count;
    const byName = root.getObjectByName(entry.node);
    if (byName) {
      const child = byName.children && byName.children[entry.primitive_index];
      if (child && child.isMesh && positionCount(child) === want) return child;
      const suffixed = root.getObjectByName(`${entry.node}_${entry.primitive_index}`);
      if (suffixed && suffixed.isMesh && positionCount(suffixed) === want) return suffixed;
      if (byName.isMesh && entry.primitive_index === 0 && positionCount(byName) === want) {
        return byName;
      }
      if (byName.isMesh && positionCount(byName) === want) return byName;
    }
    return null;
  }

  /** Map every metadata mesh row to its attribute pair + byte slice. */
  function mapMeshes(meta) {
    const claimed = new Map(); // geometry -> entry index (shared-geometry guard)
    const rows = [];
    const unassigned = [];
    for (const [index, entry] of meta.meshes.entries()) {
      const object = findMeshObject(entry);
      if (object) assign(index, entry, object);
      else unassigned.push([index, entry]);
    }
    // Fallback: file order ~= scene traversal order, match by vertex count.
    if (unassigned.length > 0) {
      const pool = meshes.filter((mesh) => !claimed.has(mesh.geometry));
      unassigned.sort((a, b) => a[1].vertex_offset - b[1].vertex_offset);
      for (const [index, entry] of unassigned) {
        const object = pool.find((mesh) => positionCount(mesh) === entry.vertex_count);
        if (object) {
          console.warn('[cfd_playback] mesh matched by order, not name:', entry.node);
          assign(index, entry, object);
          pool.splice(pool.indexOf(object), 1);
        } else {
          console.warn('[cfd_playback] no scene mesh for payload entry:', entry.node, entry.name);
          rows[index] = null;
        }
      }
    }
    return rows;

    function assign(index, entry, object) {
      const prior = claimed.get(object.geometry);
      if (prior !== undefined) {
        console.warn('[cfd_playback] shared geometry between payload entries; keeping first');
        rows[index] = rows[prior];
        return;
      }
      const attrs = ensureAttributes(object.geometry);
      if (!attrs) {
        rows[index] = null;
        return;
      }
      claimed.set(object.geometry, index);
      rows[index] = {
        offset: entry.vertex_offset,
        count: attrs.count,
        attr0: attrs.attr0,
        attr1: attrs.attr1,
        array0: attrs.attr0.array,
        array1: attrs.attr1.array,
      };
    }
  }

  // ---- keyframe interpolation ---------------------------------------------

  /** Refresh `aC0`/`aC1` (frame k) and `uMix` (frac) for the current time. */
  function applyFrame(force = false) {
    if (!payload) return;
    const { bytes, frames, vertices, ts, t0, duration } = payload;
    let k = 0;
    let frac = 0;
    if (frames > 1 && duration > TAP_EPS) {
      const t = t0 + clamp01(time01) * duration;
      k = frames - 2;
      frac = 1;
      for (let i = 0; i < frames - 1; i += 1) {
        if (t <= ts[i + 1]) {
          k = i;
          const span = ts[i + 1] - ts[i];
          frac = span > TAP_EPS ? (t - ts[i]) / span : 0;
          break;
        }
      }
    }
    if (k !== frameK || force) {
      frameK = k;
      const k1 = Math.min(k + 1, frames - 1);
      for (const row of payload.rows) {
        if (!row) continue;
        const n = Math.min(row.count, vertices - row.offset);
        const a = k * vertices + row.offset;
        const b = k1 * vertices + row.offset;
        row.array0.set(bytes.subarray(a, a + n));
        row.array1.set(bytes.subarray(b, b + n));
        row.attr0.needsUpdate = true;
        row.attr1.needsUpdate = true;
      }
    }
    uniforms.uMix.value = Math.min(1, Math.max(0, frac));
  }

  // ---- readout ------------------------------------------------------------

  function emit() {
    const duration_s = payload ? payload.duration : 0;
    const t_s = payload ? payload.t0 + clamp01(time01) * duration_s : 0;
    const signature = [
      !!payload,
      playing,
      loop,
      time01,
      speed,
      profile,
      transitMs,
      transitBranch,
      transitReadout.arrival,
      transitReadout.peak,
      transitReadout.frames,
      t_s,
    ].join('|');
    if (signature === lastEmit) return;
    lastEmit = signature;
    onReadout({
      available: !!payload,
      playing,
      loop,
      time01,
      speed,
      profile,
      transitMs,
      transitBranch,
      transitReadout,
      t_s,
      duration_s,
    });
  }

  // ---- payload loading ----------------------------------------------------

  /** Activate a decoded payload (also the profile-switch target). */
  function activate(next) {
    payload = next;
    profile = next.meta.profile || profile;
    const transit = selectTransit(next.meta.branches);
    transitMs = transit.transitMs;
    transitBranch = transit.transitBranch;
    transitReadout = transit.readout;
    frameK = -1;
    applyActivation();
    applyFrame(true);
    emit();
  }

  /**
   * Load the C5 payload for (caseId, profile), decode the K x V uint8 block
   * and activate it. Previously loaded profiles stay cached for setProfile.
   * Rejects with an Error describing the failure (caller shows the notice).
   */
  async function load(nextCaseId, nextProfile) {
    const cid = String(nextCaseId || '');
    const pid = String(nextProfile || 'A').toUpperCase();
    const key = `${cid}:${pid}`;
    const cached = payloads.get(key);
    caseId = cid;
    if (cached) {
      time01 = clamp01(time01);
      playing = false;
      activate(cached);
      return cached;
    }

    const urls = resolvePayloadURLs(cid, pid);
    const metaResponse = await fetchOK(urls.meta, 'contrast metadata');
    const meta = await metaResponse.json();
    if (!meta || !Array.isArray(meta.timestamps_s) || !Array.isArray(meta.meshes)) {
      throw new Error(`contrast metadata "${urls.meta.href}" is missing timestamps_s/meshes`);
    }
    const frames = Number(meta.frames) || meta.timestamps_s.length;
    const vertices = Number(meta.vertices);
    const binURL = urls.bin || new URL(meta.bin_file || payloadNames(cid, pid).bin, urls.meta);
    const binResponse = await fetchOK(binURL, 'contrast bin');
    const bytes = new Uint8Array(await binResponse.arrayBuffer());
    const expected = frames * vertices;
    if (!Number.isFinite(vertices) || vertices <= 0 || bytes.byteLength !== expected) {
      throw new Error(
        `contrast bin "${binURL.href}" is ${bytes.byteLength} B, expected ${expected} B ` +
          `(${frames} frames x ${vertices} vertices uint8)`,
      );
    }

    const next = {
      meta,
      key,
      caseId: cid,
      bytes,
      frames,
      vertices,
      ts: meta.timestamps_s.map(Number),
      rows: null,
      t0: Number(meta.timestamps_s[0]) || 0,
      duration: 0,
    };
    next.duration = Math.max(0, (Number(meta.timestamps_s[frames - 1]) || 0) - next.t0);
    next.rows = mapMeshes(meta);
    payloads.set(key, next);
    caseId = cid;
    time01 = 0;
    playing = false;
    activate(next);
    return next;
  }

  // ---- public API ---------------------------------------------------------

  function play() {
    if (!payload || payload.frames < 2) return;
    if (time01 >= 1 - TAP_EPS) time01 = 0; // restart when starting from the end
    playing = true;
    applyFrame();
    emit();
  }

  function pause() {
    playing = false;
    emit();
  }

  function setSpeed(x) {
    const value = Number(x);
    if (!Number.isFinite(value) || value <= 0) return;
    speed = value;
    emit();
  }

  function setLoop(on) {
    loop = !!on;
    emit();
  }

  /** Swap the injection profile (cached profiles swap instantly). */
  function setProfile(p) {
    const pid = String(p || '').toUpperCase();
    if (!pid || pid === profile) return Promise.resolve();
    return load(caseId, pid);
  }

  /** Scrub the timeline to `t01` in 0..1 (0..SCRUB_STEPS on the HUD slider). */
  function scrub(t) {
    time01 = clamp01(t);
    applyFrame();
    emit();
  }

  /** Per-frame advance; `dt` in milliseconds (stage.onFrame convention). */
  function update(dt) {
    if (!payload) return;
    if (playing && payload.frames > 1 && payload.duration > TAP_EPS) {
      const seconds = (Number.isFinite(dt) ? Math.min(Math.max(dt, 0), 100) : 1000 / 60) / 1000;
      time01 += ((seconds * speed) / payload.duration);
      if (time01 >= 1) {
        if (loop) {
          time01 %= 1;
          frameK = -1; // wrap: force the pair refresh
        } else {
          time01 = 1;
          playing = false;
        }
      }
    }
    applyFrame();
    emit();
  }

  /** Release payloads, attributes and shader patches (materials restored). */
  function dispose() {
    payloads.clear();
    payload = null;
    playing = false;
    frameK = -1;
    uniforms.uMix.value = 0;
    applyActivation();
    for (const mesh of meshes) {
      const geometry = mesh.geometry;
      if (!geometry) continue;
      geometry.deleteAttribute('aC0');
      geometry.deleteAttribute('aC1');
    }
    meshes.length = 0;
    for (const { material, prev } of materials) {
      material.userData.cfdContrast = false;
      material.onBeforeCompile = prev.onBeforeCompile;
      material.customProgramCacheKey = prev.customProgramCacheKey;
      material.transparent = prev.transparent;
      material.depthWrite = prev.depthWrite;
      material.needsUpdate = true;
    }
    materials.length = 0;
    transitMs = null;
    transitBranch = null;
    transitReadout = branchReadout(null);
    emit();
  }

  /** Live state snapshot (same shape as the onReadout payload). */
  function state() {
    const duration_s = payload ? payload.duration : 0;
    return {
      available: !!payload,
      playing,
      loop,
      time01,
      speed,
      profile,
      transitMs,
      transitBranch,
      transitReadout,
      t_s: payload ? payload.t0 + clamp01(time01) * duration_s : 0,
      duration_s,
    };
  }

  return {
    load,
    play,
    pause,
    setSpeed,
    setProfile,
    setLoop,
    scrub,
    update,
    dispose,
    applyShader,
    state,
  };
}
