import * as THREE from 'three';
import { modelURL, assetsBaseURL, payloadURL } from '../src/loader.js';
import { structureGroup } from '../src/structures.js';

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
 *
 * Contract D clinical payload (ischemia layer) lives alongside: decoded once
 * into a static per-mesh `aVFFR` Float32 attribute (never updated per frame),
 * with the `uMode` uniform selecting visualization mode A (the iodine
 * playback above, unchanged) or mode B (per-vertex vFFR colormap, shading
 * preserved through pre-lighting albedo). Probe readout / pullback queries
 * (`createClinicalModel`) and the canvas painters for the Navvus card and the
 * ACIST RXi-style pullback graph follow at the end of the file.
 */

const TAP_EPS = 1e-6;

// ---- contract D ischemia layer (vFFR colormap + probe readout) ------------

/** vFFR threshold (contract D): <= 0.80 red, > 0.80 green (card + graph line). */
export const VFFR_THRESHOLD = 0.8;

/** `tfc.fps` fallback (contract D): bolus-transit frames are 30 fps when absent. */
const DEFAULT_TFC_FPS = 30;

/**
 * Contract D vFFR colormap stops (mode B): crimson below 0.75, amber
 * 0.75-0.80, green above 0.80, smoothstep transitions of +/- 0.02 around
 * each stop boundary.
 */
const VFFR_STOP_CRIMSON = [0.749, 0.078, 0.125];
const VFFR_STOP_AMBER = [0.949, 0.753, 0.075];
const VFFR_STOP_GREEN = [0.106, 0.549, 0.227];
const VFFR_BAND_LO = 0.75;
const VFFR_TRANSITION = 0.02;

const VFFR_COLORMAP_GLSL = `
vec3 vffrColormap(float v) {
  vec3 c = mix(
    vec3(${VFFR_STOP_CRIMSON.join(', ')}),
    vec3(${VFFR_STOP_AMBER.join(', ')}),
    smoothstep(${VFFR_BAND_LO} - ${VFFR_TRANSITION}, ${VFFR_BAND_LO} + ${VFFR_TRANSITION}, v));
  return mix(
    c,
    vec3(${VFFR_STOP_GREEN.join(', ')}),
    smoothstep(${VFFR_THRESHOLD} - ${VFFR_TRANSITION}, ${VFFR_THRESHOLD} + ${VFFR_TRANSITION}, v));
}
`;

/** CSS color for one colormap stop (Navvus card / pullback graph painting). */
function stopCSS(stop) {
  return `rgb(${Math.round(stop[0] * 255)},${Math.round(stop[1] * 255)},${Math.round(stop[2] * 255)})`;
}

const VFFR_COLOR_RED = stopCSS(VFFR_STOP_CRIMSON);
const VFFR_COLOR_GREEN = stopCSS(VFFR_STOP_GREEN);
const VFFR_COLOR_AMBER = stopCSS(VFFR_STOP_AMBER);

/** Nullable JSON number -> number | null (`Number(null)` is 0, never use it). */
function numOrNull(value) {
  const n = Number(value);
  return value !== null && value !== '' && value !== undefined && Number.isFinite(n) ? n : null;
}

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
attribute float aVFFR;
uniform float uMix;
varying float vContrast;
varying float vVffr;
`;

const FRAGMENT_HEAD = `
varying float vContrast;
varying float vVffr;
uniform float uContrastOn;
uniform float uMode;
uniform float uVffrOn;
${CONTRAST_TF_GLSL}
${VFFR_COLORMAP_GLSL}
`;

const VERTEX_BODY = `
  float c = mix(aC0, aC1, uMix) / 255.0;
  vContrast = clamp(c, 0.0, 1.0);
  vVffr = aVFFR;
`;

/** Mode B albedo swap (pre-lighting: shading preserved), vFFR colormap. */
const FRAGMENT_ALBEDO_BODY = `
  diffuseColor.rgb = mix(diffuseColor.rgb, vffrColormap(vVffr), uMode * uVffrOn);
`;

const FRAGMENT_BODY = `
  vec4 ct = iodineTransfer(vContrast);
  float gate = uContrastOn * (1.0 - uMode);
  gl_FragColor.rgb = mix(gl_FragColor.rgb, ct.rgb, ct.a * gate);
  gl_FragColor.a = mix(gl_FragColor.a, ct.a, gate);
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
 * Slowest finite `valueOf(row)` among the `major`/`distal` flagged rows (the
 * established distal transit-time selection, root -> distal convective delay),
 * falling back to every row when none is flagged. Null when no row yields a
 * finite value; rows without one (JSON null on negligible-mean-flow branches)
 * never win the selection. Ties keep the first row (payload order).
 */
function slowestDistalRow(rows, valueOf) {
  const list = (rows || []).filter((row) => row && valueOf(row) !== null);
  const prioritized = list.filter((row) => row.major || row.distal);
  const pool = prioritized.length > 0 ? prioritized : list;
  if (pool.length === 0) return null;
  let best = pool[0];
  for (const row of pool) {
    if (valueOf(row) > valueOf(best)) best = row;
  }
  return best;
}

/**
 * Distal transit-time readout source (metadata `branches`): prioritize
 * `major`/`distal` flagged branches and report the slowest `transit_ms`
 * (root -> distal convective delay) among them. `readout` renders every
 * nullable field of the selected row (see branchReadout) so nulls surface as
 * `n/a`, never `null ms`/NaN. `transitCalibratedMs` is the contract H
 * counterpart — the same selection over the calibrated `transit_calibrated_ms`
 * column, the global header fallback when no clinical payload is loaded.
 */
function selectTransit(branches) {
  const best = slowestDistalRow(branches, (b) => numOrNull(b.transit_ms));
  const calibrated = slowestDistalRow(branches, (b) => numOrNull(b.transit_calibrated_ms));
  const transitCalibratedMs = calibrated ? numOrNull(calibrated.transit_calibrated_ms) : null;
  if (!best) {
    return {
      transitMs: null,
      transitBranch: null,
      transitCalibratedMs,
      readout: branchReadout(null),
    };
  }
  return {
    transitMs: numOrNull(best.transit_ms),
    transitBranch: best.name || null,
    transitCalibratedMs,
    readout: branchReadout(best),
  };
}

/**
 * Calibrated tree transit (contract H) from the clinical payload's
 * `branches[]` rows: the slowest distal `transit_calibrated_s` (signals-scale
 * calibrated transit) with its raw pair from `transit_s` (alias of the
 * pre-calibration `t_arr_s`). Both fields stay null on payloads that predate
 * the calibrated columns — the caller then falls back to the contrast
 * metadata `transit_calibrated_ms`.
 */
function clinicalTreeTransit(json) {
  const rows = (json && json.branches) || [];
  const rawOf = (row) => numOrNull(row.transit_s) ?? numOrNull(row.t_arr_s);
  const calibratedRow = slowestDistalRow(rows, (row) => numOrNull(row.transit_calibrated_s));
  const rawRow = slowestDistalRow(rows, rawOf);
  return {
    calibratedS: calibratedRow ? numOrNull(calibratedRow.transit_calibrated_s) : null,
    rawS: rawRow ? rawOf(rawRow) : null,
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
 * @returns {object} {load, play, pause, setSpeed, setProfile, scrub, update,
 *   dispose, applyShader, attachVFFR, setMode, state}
 */
export function createCfdPlayback({ scene, stage, gltf, assetsBase, onReadout = () => {} }) {
  const root = (gltf && gltf.scene) || scene;
  // Shared uniform objects: every playback program reads the same instances.
  // uMode selects the visualization mode (contract D): 0 = contrast playback
  // (mode A, unchanged), 1 = static vFFR colormap (mode B). Flipping it is the
  // whole mode switch — no attribute or material churn, no render-loop work.
  const uniforms = {
    uMix: { value: 0 },
    uContrastOn: { value: 0 },
    uMode: { value: 0 },
  };

  /** mesh objects that carry playback attributes (insertion = file order). */
  const meshes = [];
  /** records: {material, prev, objects, uVffrOn} per patched material. */
  const materials = [];
  /** meshes carrying the static contract D `aVFFR` attribute. */
  const vffrMeshes = new Set();
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
  let transitCalibratedMs = null;
  let transitReadout = branchReadout(null);
  /**
   * Contract H tree transit read from the clinical payload (attachVFFR):
   * `{calibratedS, rawS, tfcFps}` — the preferred source of the global
   * header transit, ahead of the contrast metadata fallback.
   */
  let clinicalTransit = { calibratedS: null, rawS: null, tfcFps: null };
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
   * Patch one scene node for playback: inject the contrast/vFFR shader into
   * every material and attach the zero-filled `aC0`/`aC1` attributes. Called
   * from main's initClipping traverse (post GLTF load/decode); safe to re-call
   * (shared materials record every patched object for `uVffrOn` bookkeeping).
   */
  function applyShader(object) {
    if (!object || !object.isMesh || !object.geometry) return;
    const attrs = ensureAttributes(object.geometry);
    if (!attrs) return;
    if (!meshes.includes(object)) meshes.push(object);
    const list = Array.isArray(object.material) ? object.material : [object.material];
    for (const material of list) {
      if (!material) continue;
      let record = material.userData.cfdContrast;
      if (!record) {
        record = {
          material,
          prev: {
            onBeforeCompile: material.onBeforeCompile,
            customProgramCacheKey: material.customProgramCacheKey,
            transparent: material.transparent,
            depthWrite: material.depthWrite,
          },
          objects: [],
          uVffrOn: { value: 0 },
        };
        material.userData.cfdContrast = record;
        material.onBeforeCompile = (shader) => {
          shader.uniforms.uMix = uniforms.uMix;
          shader.uniforms.uContrastOn = uniforms.uContrastOn;
          shader.uniforms.uMode = uniforms.uMode;
          shader.uniforms.uVffrOn = record.uVffrOn;
          shader.vertexShader =
            VERTEX_HEAD +
            shader.vertexShader.replace(
              '#include <begin_vertex>',
              `#include <begin_vertex>${VERTEX_BODY}`,
            );
          let fragment = shader.fragmentShader;
          if (fragment.includes('#include <color_fragment>')) {
            fragment = fragment.replace(
              '#include <color_fragment>',
              `#include <color_fragment>${FRAGMENT_ALBEDO_BODY}`,
            );
          } else if (fragment.includes('#include <map_fragment>')) {
            fragment = fragment.replace(
              '#include <map_fragment>',
              `#include <map_fragment>${FRAGMENT_ALBEDO_BODY}`,
            );
          } else {
            console.warn('[cfd_playback] no albedo injection point in', material.type);
          }
          const head = FRAGMENT_HEAD;
          if (fragment.includes('#include <dithering_fragment>')) {
            fragment = fragment.replace(
              '#include <dithering_fragment>',
              `#include <dithering_fragment>${FRAGMENT_BODY}`,
            );
          } else if (fragment.includes('#include <opaque_fragment>')) {
            fragment = fragment.replace(
              '#include <opaque_fragment>',
              `#include <opaque_fragment>${FRAGMENT_BODY}`,
            );
          } else {
            console.warn('[cfd_playback] no fragment injection point in', material.type);
          }
          shader.fragmentShader = head + fragment;
        };
        material.customProgramCacheKey = () => 'cfd-contrast-v2';
        materials.push(record);
      }
      if (!record.objects.includes(object)) record.objects.push(object);
    }
  }

  /** uVffrOn = 1 only where every mesh of the material carries `aVFFR`. */
  function refreshVffrOn() {
    for (const record of materials) {
      const on =
        record.objects.length > 0 && record.objects.every((object) => vffrMeshes.has(object));
      record.uVffrOn.value = on ? 1 : 0;
    }
  }

  /**
   * Attach decoded per-vertex vFFR (contract D) as one static `aVFFR` Float32
   * attribute per target mesh — created once at load, never touched per frame.
   * Target lookup: `spec.meshes[]` rows (C5-style node/primitive_index/
   * vertex_offset/vertex_count) when the payload provides them, else the
   * coronaries-group meshes matching `spec.vertices` (one mesh with that count,
   * or all of them concatenated in traversal order), else any single mesh with
   * exactly `spec.vertices` positions. Returns false (and warns) when nothing
   * matches — the probe readout keeps working without the attribute. Also
   * records the clinical payload's calibrated tree transit (contract H) for
   * the Contrast HUD header — done first so the header keeps working even
   * when the attribute itself cannot be attached.
   *
   * @param {Float32Array} vffr dequantized `vertex_vffr_u8` (see loadClinical)
   * @param {object} spec contract D payload (for `vertices`/`meshes`)
   * @returns {boolean} attribute attached
   */
  function attachVFFR(vffr, spec) {
    const tree = clinicalTreeTransit(spec);
    clinicalTransit = {
      calibratedS: tree.calibratedS,
      rawS: tree.rawS,
      tfcFps: numOrNull(spec && spec.tfc && spec.tfc.fps) || DEFAULT_TFC_FPS,
    };
    const total = Number(spec && spec.vertices) || vffr.length;
    if (!vffr || vffr.length !== total) {
      console.warn('[cfd_playback] vFFR length does not match clinical vertices:', total);
      return false;
    }
    /** @type {Array<{object: object, offset: number, count: number}>} */
    let targets = [];
    if (Array.isArray(spec.meshes)) {
      for (const entry of spec.meshes) {
        const object = findMeshObject(entry);
        const offset = Number(entry.vertex_offset) || 0;
        const count = object ? positionCount(object) : 0;
        if (object && offset + count <= total) {
          targets.push({ object, offset, count });
        } else {
          console.warn('[cfd_playback] no mesh for clinical vFFR entry:', entry.node);
        }
      }
    }
    if (targets.length === 0) {
      const coronaries = meshes.filter((mesh) => structureGroup(mesh.name || '') === 'coronaries');
      const byCount = (mesh) => positionCount(mesh) === total;
      const pool = coronaries.filter(byCount);
      const fallback = pool.length > 0 ? pool : meshes.filter(byCount);
      if (fallback.length > 0) {
        targets = [{ object: fallback[0], offset: 0, count: total }];
      } else if (coronaries.length > 0) {
        const sum = coronaries.reduce((t, mesh) => t + positionCount(mesh), 0);
        if (sum === total) {
          let offset = 0;
          for (const mesh of coronaries) {
            const count = positionCount(mesh);
            targets.push({ object: mesh, offset, count });
            offset += count;
          }
        }
      }
    }
    if (targets.length === 0) {
      console.warn('[cfd_playback] no mesh matches clinical vertices:', total);
      return false;
    }
    for (const { object, offset, count } of targets) {
      applyShader(object);
      const geometry = object.geometry;
      if (!geometry.getAttribute('aVFFR')) {
        geometry.setAttribute('aVFFR', new THREE.BufferAttribute(vffr.subarray(offset, offset + count), 1));
        vffrMeshes.add(object);
      }
    }
    refreshVffrOn();
    return true;
  }

  /** Visualization mode (contract D): 'A' iodine playback, 'B' vFFR map. */
  function setMode(mode) {
    uniforms.uMode.value = mode === 'B' ? 1 : 0;
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

  /**
   * Global tree transit for the HUD header (contract H): the clinical
   * payload's calibrated `transit_calibrated_s` (attachVFFR) first, else the
   * contrast metadata's `transit_calibrated_ms`; null when neither exists
   * (the header then renders no headline instead of a raw 15 s-scale value).
   */
  function headerCalibratedS() {
    if (clinicalTransit.calibratedS !== null) return clinicalTransit.calibratedS;
    return transitCalibratedMs === null ? null : transitCalibratedMs / 1000;
  }

  /**
   * Raw tree transit (s) paired with `headerCalibratedS` — a small secondary
   * annotation only (e.g. `raw 11.6 s`), never the headline transit.
   */
  function headerRawS() {
    if (clinicalTransit.rawS !== null) return clinicalTransit.rawS;
    return transitMs === null ? null : transitMs / 1000;
  }

  function emit() {
    const duration_s = payload ? payload.duration : 0;
    const t_s = payload ? payload.t0 + clamp01(time01) * duration_s : 0;
    const transitCalibratedS = headerCalibratedS();
    const transitRawS = headerRawS();
    const transitTfcFps = clinicalTransit.tfcFps || DEFAULT_TFC_FPS;
    const signature = [
      !!payload,
      playing,
      loop,
      time01,
      speed,
      profile,
      transitMs,
      transitBranch,
      transitCalibratedS,
      transitRawS,
      transitTfcFps,
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
      transitCalibratedS,
      transitRawS,
      transitTfcFps,
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
    transitCalibratedMs = transit.transitCalibratedMs;
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
      geometry.deleteAttribute('aVFFR');
    }
    meshes.length = 0;
    vffrMeshes.clear();
    for (const record of materials) {
      const { material, prev } = record;
      record.uVffrOn.value = 0;
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
    transitCalibratedMs = null;
    transitReadout = branchReadout(null);
    clinicalTransit = { calibratedS: null, rawS: null, tfcFps: null };
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
      transitCalibratedS: headerCalibratedS(),
      transitRawS: headerRawS(),
      transitTfcFps: clinicalTransit.tfcFps || DEFAULT_TFC_FPS,
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
    attachVFFR,
    setMode,
    state,
  };
}

// ---- clinical viewer payload (contract D) --------------------------------

/**
 * `?clinical=<url>` override (empty when absent, contract D), mirroring the
 * `?payload=` accessor in loader.js.
 */
export function clinicalOverride() {
  const fromQuery = new URLSearchParams(window.location.search).get('clinical');
  const trimmed = fromQuery ? fromQuery.trim() : '';
  return trimmed;
}

/**
 * Clinical payload URL (contract D), mirroring resolvePayloadURLs():
 *  - `?clinical=<url>` ending in `.json` -> that file
 *  - `?clinical=<url>` otherwise -> base directory holding the derived name
 *  - absent -> `clinical/case_<id>_clinical.json` at the app root
 */
export function resolveClinicalURL(caseId) {
  const name = `case_${caseId}_clinical.json`;
  const override = clinicalOverride();
  if (override) {
    return override.endsWith('.json')
      ? new URL(override, document.baseURI)
      : new URL(name, dirURL(override));
  }
  return new URL(`clinical/${name}`, document.baseURI);
}

/** Base64 -> Uint8Array (payload decode at load time, never per frame). */
function base64Bytes(text) {
  const binary = atob(text);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

/**
 * Fetch, validate and decode the contract D clinical payload for `caseId`.
 * Resolves with `{ json, vffr }`: the parsed payload plus its
 * `vertex_vffr_u8` dequantized through `vffr_scale`
 * (`min + (max - min) * u8 / 255`) as one Float32Array of `vertices` entries.
 * Rejects with an Error describing the failure (caller shows the notice).
 *
 * @param {string} caseId case id (`?case=<id>` / model URL digits)
 * @returns {Promise<{json: object, vffr: Float32Array}>}
 */
export async function loadClinical(caseId) {
  const url = resolveClinicalURL(caseId);
  const response = await fetchOK(url, 'clinical payload');
  const json = await response.json();
  if (
    !json ||
    !Array.isArray(json.branches) ||
    !Array.isArray(json.pullbacks) ||
    !Array.isArray(json.lesions)
  ) {
    throw new Error(`clinical payload "${url.href}" is missing branches/pullbacks/lesions`);
  }
  const vertices = Number(json.vertices);
  const scale = json.vffr_scale || {};
  const lo = numOrNull(scale.min);
  const hi = numOrNull(scale.max);
  if (!Number.isFinite(vertices) || vertices <= 0 || lo === null || hi === null) {
    throw new Error(`clinical payload "${url.href}" is missing vertices/vffr_scale`);
  }
  const raw = typeof json.vertex_vffr_u8 === 'string' ? base64Bytes(json.vertex_vffr_u8) : null;
  if (!raw || raw.length !== vertices) {
    throw new Error(
      `clinical payload "${url.href}" vertex_vffr_u8 is ${raw ? raw.length : 0} B, ` +
        `expected ${vertices} B (${vertices} vertices uint8)`,
    );
  }
  const vffr = new Float32Array(vertices);
  const k = (hi - lo) / 255;
  for (let i = 0; i < vertices; i += 1) vffr[i] = lo + raw[i] * k;
  return { json, vffr };
}

/** Nullable JSON number rendered for the card/graph (`n/a` when absent). */
function fmtNum(value, digits) {
  const n = numOrNull(value);
  return n === null ? NA_READOUT : n.toFixed(digits);
}

/**
 * Clinical viewer payload queries (contract D): nearest `centerline_ras`
 * sample for a probe point, its Navvus readout fields and pullback selection.
 * All lookup tables are built once here; every query is event-driven (probe
 * placement / automation calls), never run from a render loop.
 *
 * Coordinate frame: `*_xyz_ras` / `centerline_ras` are GLB model-frame
 * meters (the GLB POSITION space), matching probe points passed in by main.
 *
 * @param {object} json schema flowscope.clinical.viewer v1 payload
 * @returns {object} { probeAt, readoutFor, pullbackFor, pullbackById }
 */
export function createClinicalModel(json) {
  const branches = json.branches;
  const pullbacks = json.pullbacks;
  const lesions = json.lesions;
  const tfcFps = numOrNull(json.tfc && json.tfc.fps) || DEFAULT_TFC_FPS;

  // Flattened centerline samples (setup-time allocation): the nearest-sample
  // probe lookup is one typed-array scan over every branch's 1 mm stations.
  let samples = 0;
  for (const branch of branches) samples += (branch.centerline_ras || []).length;
  const sampleXYZ = new Float32Array(samples * 3);
  const sampleS = new Float32Array(samples);
  const sampleBranch = new Int32Array(samples);
  const branchById = new Map();
  let at = 0;
  for (const [index, branch] of branches.entries()) {
    branchById.set(branch.branch_id, branch);
    const line = branch.centerline_ras || [];
    const stations = branch.centerline_s_mm || [];
    for (let i = 0; i < line.length; i += 1) {
      const p = line[i];
      sampleXYZ[at * 3] = numOrNull(p && p[0]) || 0;
      sampleXYZ[at * 3 + 1] = numOrNull(p && p[1]) || 0;
      sampleXYZ[at * 3 + 2] = numOrNull(p && p[2]) || 0;
      sampleS[at] = numOrNull(stations[i]) || 0;
      sampleBranch[at] = index;
      at += 1;
    }
  }

  /**
   * Nearest centerline sample for a probe point (GLB model frame): the probe
   * descriptor used by readoutFor/pullbackFor. Null when no samples exist.
   */
  function probeAt(x, y, z) {
    if (samples === 0) return null;
    let best = 0;
    let bestD2 = Infinity;
    for (let i = 0; i < samples; i += 1) {
      const dx = sampleXYZ[i * 3] - x;
      const dy = sampleXYZ[i * 3 + 1] - y;
      const dz = sampleXYZ[i * 3 + 2] - z;
      const d2 = dx * dx + dy * dy + dz * dz;
      if (d2 < bestD2) {
        bestD2 = d2;
        best = i;
      }
    }
    const branch = branches[sampleBranch[best]];
    return {
      position: [x, y, z],
      branch_id: branch.branch_id,
      label: branch.label,
      s_mm: sampleS[best],
      branch,
    };
  }

  /** First lesion on the probe's branch whose span contains the probe s. */
  function lesionAt(probe) {
    for (const lesion of lesions) {
      if (!lesion || lesion.branch_id !== probe.branch_id) continue;
      const from = numOrNull(lesion.s_mm) || 0;
      const to = from + (numOrNull(lesion.length_mm) || 0);
      if (probe.s_mm >= from && probe.s_mm <= to) return lesion;
    }
    return null;
  }

  /** Index of the `s_mm` station nearest to `s` (-1 for an empty grid). */
  function nearestIndex(sArr, s) {
    if (!Array.isArray(sArr) || sArr.length === 0) return -1;
    let best = 0;
    let bestD = Infinity;
    for (let i = 0; i < sArr.length; i += 1) {
      const d = Math.abs((numOrNull(sArr[i]) || 0) - s);
      if (d < bestD) {
        bestD = d;
        best = i;
      }
    }
    return best;
  }

  /** First finite entry of a pullback curve (proximal end for P_rest). */
  function firstFinite(arr) {
    if (!Array.isArray(arr)) return null;
    for (const value of arr) {
      const n = numOrNull(value);
      if (n !== null) return n;
    }
    return null;
  }

  /**
   * Auto-selected pullback for the probe: among the `pullbacks[]` entries
   * containing its branch, the longest match (greatest `s_mm` extent, then
   * the most branch_ids, then payload order).
   */
  function pullbackFor(probe) {
    let best = null;
    let bestSpan = -1;
    let bestCount = -1;
    for (const entry of pullbacks) {
      if (!entry || !Array.isArray(entry.branch_ids)) continue;
      if (!entry.branch_ids.includes(probe.branch_id)) continue;
      const sArr = Array.isArray(entry.s_mm) ? entry.s_mm : [];
      const first = sArr.length > 0 ? numOrNull(sArr[0]) || 0 : 0;
      const last = sArr.length > 0 ? numOrNull(sArr[sArr.length - 1]) || 0 : 0;
      const span = Math.abs(last - first);
      const count = entry.branch_ids.length;
      if (!best || span > bestSpan || (span === bestSpan && count > bestCount)) {
        best = entry;
        bestSpan = span;
        bestCount = count;
      }
    }
    return best;
  }

  /** Pullback entry by `path_id` (automation `selectPath`), else null. */
  function pullbackById(pathId) {
    for (const entry of pullbacks) {
      if (entry && entry.path_id === pathId) return entry;
    }
    return null;
  }

  /** Edge-local branch length L (contract D): last `centerline_s_mm` station. */
  function branchLength(branch) {
    const stations = branch && branch.centerline_s_mm;
    if (!Array.isArray(stations) || stations.length === 0) return null;
    return numOrNull(stations[stations.length - 1]);
  }

  /**
   * Frame mapping for pullback column lookups: `pullbacks[].s_mm` is
   * path-absolute (0..L_path over `branch_ids`, each branch tiling
   * `[run_start, run_start + L)` proximal-to-distal) while `probe.s_mm` is
   * edge-local, so `s_abs = run_start + s_local`. L is the branch's last
   * `centerline_s_mm` station; a chain branch without one falls back to its
   * pullback run extent (the residual path length shared by such runs).
   * Null when the probe's branch is not on this path (no mapping -> the
   * columns stay `n/a` rather than silently reading another run).
   */
  function sAbsFor(probe, pullback) {
    if (!probe || !pullback || !Array.isArray(pullback.branch_ids)) return null;
    const ids = pullback.branch_ids;
    let slot = -1;
    let known = 0;
    let missing = 0;
    for (let i = 0; i < ids.length; i += 1) {
      if (slot < 0 && ids[i] === probe.branch_id) slot = i;
      const len = branchLength(branchById.get(ids[i]));
      if (len === null) missing += 1;
      else known += len;
    }
    if (slot < 0) return null;
    const sArr = Array.isArray(pullback.s_mm) ? pullback.s_mm : [];
    const first = sArr.length > 0 ? numOrNull(sArr[0]) || 0 : 0;
    const last = sArr.length > 0 ? numOrNull(sArr[sArr.length - 1]) || 0 : 0;
    const residual = missing > 0 ? Math.max(0, Math.abs(last - first) - known) / missing : 0;
    let runStart = 0;
    for (let i = 0; i < slot; i += 1) {
      const len = branchLength(branchById.get(ids[i]));
      runStart += len === null ? residual : len;
    }
    return runStart + (numOrNull(probe.s_mm) || 0);
  }

  /**
   * Navvus readout fields for the probe (contract D card): vessel label /
   * branch_id, local lumen `d_mm` + stenosis `%AS` (lesion containing s, else
   * 0 / the local `d_mm` profile), the Pa/Pd/dP/vFFR quartet and the distal
   * bolus transit `t_arr_s` / `tfc_frames` (calibrated pair
   * `transit_calibrated_s` / `tfc_calibrated_frames`) from the branch row.
   *
   * The quartet resolves through the contract H chain at this single site
   * (the Navvus card canvas and its `data-fs="cath-readout"` mirror both
   * render this readout): (1) the mapped pullback station columns — Pa is
   * the pullback's proximal `P_rest_mmHg`, Pd the local `P_hyper_mmHg`, dP
   * the local `delta_p_mmHg` (= Pa - Pd) and vFFR the local `vFFR`; (2) the
   * branch/lesion row quartet (`p_aorta_mmHg` / `p_distal_mmHg` /
   * `delta_p_mmHg` / `vffr`, legacy `Pa` / `Pd` / `dP` / `dp_hyper_mmHg`
   * aliases, the lesion row first inside a lesion); (3) derivation from what
   * is known — `dP = Pa - Pd`, `vFFR = Pd / Pa`. Fields render `n/a` only
   * when every source is absent. The profile columns (`d_mm`,
   * `P_hyper_mmHg`, `vFFR`, `delta_p_mmHg`) are indexed through `sAbsFor` —
   * the probe's edge-local `s_mm` mapped into the pullback's path-absolute
   * frame (works for any `pullbacks[]` entry, side paths included).
   */
  function readoutFor(probe, pullback) {
    const branch = probe.branch || {};
    const lesion = lesionAt(probe);
    const hasPullback = !!pullback && Array.isArray(pullback.s_mm) && pullback.s_mm.length > 0;
    const sAbs = hasPullback ? sAbsFor(probe, pullback) : null;
    const idx = sAbs !== null ? nearestIndex(pullback.s_mm, sAbs) : -1;
    const local = (arr) => (hasPullback && idx >= 0 && Array.isArray(arr) ? numOrNull(arr[idx]) : null);
    const dProfile = local(pullback && pullback.d_mm);
    const dLesion = lesion ? numOrNull(lesion.d_min_mm) : null;
    const dBranch = numOrNull(branch.d_mm);
    // (1) mapped pullback station columns
    const sPa = hasPullback ? firstFinite(pullback.P_rest_mmHg) : null;
    const sPd = local(pullback && pullback.P_hyper_mmHg);
    const sDp = local(pullback && pullback.delta_p_mmHg);
    const sV = local(pullback && pullback.vFFR);
    // (2) branch/lesion row quartet (contract E names + legacy aliases)
    const rowNum = (names) => {
      for (const node of lesion ? [lesion, branch] : [branch]) {
        for (const name of names) {
          const value = numOrNull(node[name]);
          if (value !== null) return value;
        }
      }
      return null;
    };
    const rPa = rowNum(['p_aorta_mmHg', 'Pa']);
    const rPd = rowNum(['p_distal_mmHg', 'Pd']);
    const rDp = rowNum(['delta_p_mmHg', 'dP', 'dp_hyper_mmHg']);
    const rV = rowNum(['vffr']);
    // (3) derivation: dP = Pa - Pd, vFFR = Pd / Pa
    const pa_mmHg = sPa !== null ? sPa : rPa;
    const pd_mmHg = sPd !== null ? sPd : rPd;
    const dp_mmHg =
      sDp !== null
        ? sDp
        : rDp !== null
          ? rDp
          : pa_mmHg !== null && pd_mmHg !== null
            ? +(pa_mmHg - pd_mmHg).toFixed(1)
            : null;
    const vffr =
      sV !== null
        ? sV
        : rV !== null
          ? rV
          : pa_mmHg !== null && pd_mmHg !== null && pa_mmHg > 0
            ? +(pd_mmHg / pa_mmHg).toFixed(2)
            : null;
    return {
      label: branch.label || null,
      branch_id: probe.branch_id,
      s_mm: probe.s_mm,
      d_mm: dProfile !== null ? dProfile : dLesion !== null ? dLesion : dBranch,
      as_pct: lesion ? numOrNull(lesion.as_pct) || 0 : 0,
      lesion_id: lesion ? lesion.lesion_id : null,
      pa_mmHg,
      pd_mmHg,
      dp_mmHg,
      vffr,
      t_arr_s: numOrNull(branch.t_arr_s),
      tfc_frames: numOrNull(branch.tfc_frames),
      transit_calibrated_s: numOrNull(branch.transit_calibrated_s),
      tfc_calibrated_frames: numOrNull(branch.tfc_calibrated_frames),
      tfc_fps: tfcFps,
    };
  }

  return { probeAt, readoutFor, pullbackFor, pullbackById };
}

/**
 * Card transit line for one readout: the calibrated tree-to-node transit
 * `transit_calibrated_s` / `tfc_calibrated_frames` when the branch row has
 * them (contract H: raw values must not headline where calibrated ones
 * belong), else the raw distal bolus transit `t_arr_s` / `tfc_frames`.
 */
function transitCardLine(r) {
  const calibratedS = numOrNull(r.transit_calibrated_s);
  if (calibratedS !== null) {
    const fps = numOrNull(r.tfc_fps) || DEFAULT_TFC_FPS;
    const frames = numOrNull(r.tfc_calibrated_frames) ?? Math.round(calibratedS * fps);
    return `t_arr ${calibratedS.toFixed(2)} s (cal) · TFC ${frames} f @ ${fps} fps`;
  }
  return `t_arr ${fmtNum(r.t_arr_s, 2)} s · TFC ${fmtNum(r.tfc_frames, 0)} f @ ${fmtNum(r.tfc_fps, 0)} fps`;
}

/**
 * Readout -> card lines (contract D fields), shared by the XR diagnostic card
 * canvas and its DOM mirror. The vFFR line is bold and color-coded
 * (<= threshold red, > threshold green).
 *
 * @param {object} readout createClinicalModel().readoutFor result
 * @returns {Array<{text: string, bold?: boolean, color?: string|null}>}
 */
export function cathCardLines(readout) {
  const r = readout || {};
  const vffr = numOrNull(r.vffr);
  const vessel = r.label ? `${r.label} (${r.branch_id})` : `${r.branch_id || NA_READOUT}`;
  return [
    { text: vessel, bold: true },
    { text: `d ${fmtNum(r.d_mm, 2)} mm · ${fmtNum(r.as_pct, 0)} %AS` },
    { text: `Pa ${fmtNum(r.pa_mmHg, 1)} mmHg · Pd ${fmtNum(r.pd_mmHg, 1)} mmHg` },
    { text: `dP ${fmtNum(r.dp_mmHg, 1)} mmHg` },
    {
      text: `vFFR ${fmtNum(vffr, 2)}`,
      bold: true,
      color: vffr === null ? null : vffr <= VFFR_THRESHOLD ? VFFR_COLOR_RED : VFFR_COLOR_GREEN,
    },
    { text: transitCardLine(r) },
  ];
}

/** Rounded card background shared by the two canvas painters. */
function paintCardBackground(ctx, width, height) {
  ctx.clearRect(0, 0, width, height);
  ctx.fillStyle = 'rgba(8, 10, 14, 0.78)';
  if (typeof ctx.roundRect === 'function') {
    ctx.beginPath();
    ctx.roundRect(4, 4, width - 8, height - 8, 18);
    ctx.fill();
  } else {
    ctx.fillRect(4, 4, width - 8, height - 8);
  }
}

/**
 * Paint the Navvus diagnostic card (`cathCardLines`) into a 2D canvas used as
 * the XR card texture. Redrawn only when the probe/readout changes — never
 * from a render loop.
 */
export function drawCathCard(ctx, lines, width, height) {
  paintCardBackground(ctx, width, height);
  ctx.textBaseline = 'top';
  const padX = 22;
  const padY = 18;
  const maxWidth = width - 2 * padX;
  const lineH = (height - 2 * padY) / Math.max(lines.length, 1);
  let y = padY;
  for (const line of lines) {
    let size = line.bold ? 30 : 26;
    const font = () => (line.bold ? `700 ${size}px ui-monospace, monospace` : `${size}px ui-monospace, monospace`);
    ctx.font = font();
    // shrink to fit: long numeric lines must never clip at the card edge
    while (size > 12 && ctx.measureText(line.text).width > maxWidth) {
      size -= 1;
      ctx.font = font();
    }
    ctx.fillStyle = line.color || '#dfe5ee';
    ctx.fillText(line.text, padX, y);
    y += lineH;
  }
}

/**
 * Paint the selected pullback path (contract D `pullbacks[]`, ACIST RXi-style
 * console): `P_rest_mmHg` + `P_hyper_mmHg` on the left axis and `vFFR` on the
 * right axis, both vs `s_mm`, with the vFFR threshold line. The one canvas
 * serves the desktop DOM overlay and the XR texture plane. Redrawn only when
 * the selection changes — never from a render loop.
 */
export function drawPullback(ctx, pullback, width, height) {
  paintCardBackground(ctx, width, height);
  const sArr = (pullback && pullback.s_mm) || [];
  const curves = [
    { arr: (pullback && pullback.P_rest_mmHg) || [], color: '#7fb3ff' },
    { arr: (pullback && pullback.P_hyper_mmHg) || [], color: '#ffb454' },
  ];
  const vArr = (pullback && pullback.vFFR) || [];
  if (sArr.length < 2) return;

  // plot frame (px layout only; every displayed number derives from the data)
  const mL = 78;
  const mR = 70;
  const mT = 74;
  const mB = 46;
  const plotW = width - mL - mR;
  const plotH = height - mT - mB;

  let sMin = Infinity;
  let sMax = -Infinity;
  let pMin = Infinity;
  let pMax = -Infinity;
  let vMin = VFFR_THRESHOLD;
  let vMax = VFFR_THRESHOLD;
  for (let i = 0; i < sArr.length; i += 1) {
    const s = numOrNull(sArr[i]);
    if (s === null) continue;
    if (s < sMin) sMin = s;
    if (s > sMax) sMax = s;
    for (const curve of curves) {
      const p = numOrNull(curve.arr[i]);
      if (p === null) continue;
      if (p < pMin) pMin = p;
      if (p > pMax) pMax = p;
    }
    const v = numOrNull(vArr[i]);
    if (v !== null) {
      if (v < vMin) vMin = v;
      if (v > vMax) vMax = v;
    }
  }
  if (!(sMax > sMin) || !Number.isFinite(pMin) || !Number.isFinite(pMax)) return;
  const pPad = pMax > pMin ? (pMax - pMin) * 0.08 : 1;
  const vPad = vMax > vMin ? (vMax - vMin) * 0.08 : 0.05;
  pMin -= pPad;
  pMax += pPad;
  vMin -= vPad;
  vMax += vPad;

  const xOf = (s) => mL + ((s - sMin) / (sMax - sMin)) * plotW;
  const yP = (p) => mT + plotH - ((p - pMin) / (pMax - pMin)) * plotH;
  const yV = (v) => mT + plotH - ((v - vMin) / (vMax - vMin)) * plotH;

  ctx.strokeStyle = 'rgba(255, 255, 255, 0.12)';
  ctx.lineWidth = 1;
  ctx.font = '20px ui-monospace, monospace';
  ctx.textBaseline = 'middle';
  for (let i = 0; i <= 3; i += 1) {
    const frac = i / 3;
    const x = mL + frac * plotW;
    const y = mT + plotH - frac * plotH;
    ctx.beginPath();
    ctx.moveTo(x, mT);
    ctx.lineTo(x, mT + plotH);
    ctx.moveTo(mL, y);
    ctx.lineTo(mL + plotW, y);
    ctx.stroke();
    ctx.fillStyle = '#9fb0c4';
    ctx.textAlign = 'right';
    ctx.fillText((sMin + frac * (sMax - sMin)).toFixed(0), x, mT + plotH + 20);
    ctx.fillText((pMin + frac * (pMax - pMin)).toFixed(0), mL - 10, y);
    ctx.textAlign = 'left';
    ctx.fillText((vMin + frac * (vMax - vMin)).toFixed(2), mL + plotW + 10, y);
  }

  for (const curve of curves) {
    ctx.strokeStyle = curve.color;
    ctx.lineWidth = 2;
    ctx.beginPath();
    let pen = false;
    for (let i = 0; i < sArr.length; i += 1) {
      const s = numOrNull(sArr[i]);
      const p = numOrNull(curve.arr[i]);
      if (s === null || p === null) {
        pen = false;
        continue;
      }
      const x = xOf(s);
      const y = yP(p);
      if (!pen) {
        ctx.moveTo(x, y);
        pen = true;
      } else {
        ctx.lineTo(x, y);
      }
    }
    ctx.stroke();
  }

  ctx.strokeStyle = '#dfe5ee';
  ctx.lineWidth = 2;
  ctx.beginPath();
  let pen = false;
  for (let i = 0; i < sArr.length; i += 1) {
    const s = numOrNull(sArr[i]);
    const v = numOrNull(vArr[i]);
    if (s === null || v === null) {
      pen = false;
      continue;
    }
    const x = xOf(s);
    const y = yV(v);
    if (!pen) {
      ctx.moveTo(x, y);
      pen = true;
    } else {
      ctx.lineTo(x, y);
    }
  }
  ctx.stroke();

  // vFFR threshold line
  ctx.strokeStyle = VFFR_COLOR_AMBER;
  ctx.lineWidth = 1.5;
  ctx.setLineDash([8, 6]);
  ctx.beginPath();
  ctx.moveTo(mL, yV(VFFR_THRESHOLD));
  ctx.lineTo(mL + plotW, yV(VFFR_THRESHOLD));
  ctx.stroke();
  ctx.setLineDash([]);

  // header: title row, legend row, then axis captions above the plot — each
  // row is collision-free for any label length
  ctx.textBaseline = 'top';
  ctx.textAlign = 'left';
  ctx.fillStyle = '#ffffff';
  ctx.font = '700 20px ui-monospace, monospace';
  const title = pullback.label ? `${pullback.label} (${pullback.path_id})` : `${pullback.path_id}`;
  ctx.fillText(title, 14, 6);
  ctx.font = '16px ui-monospace, monospace';
  const legend = [
    { name: 'P_rest', color: '#7fb3ff' },
    { name: 'P_hyper', color: '#ffb454' },
    { name: 'vFFR', color: '#dfe5ee' },
    { name: `vFFR ${VFFR_THRESHOLD.toFixed(2)}`, color: VFFR_COLOR_AMBER },
  ];
  let legendX = 14;
  for (const item of legend) {
    ctx.fillStyle = item.color;
    ctx.fillText(item.name, legendX, 32);
    legendX += ctx.measureText(item.name).width + 18;
  }
  ctx.fillStyle = '#9fb0c4';
  ctx.font = '18px ui-monospace, monospace';
  ctx.textAlign = 'left';
  ctx.fillText('P (mmHg)', mL, 54);
  ctx.textAlign = 'right';
  ctx.fillText('vFFR', mL + plotW, 54);
  ctx.textAlign = 'center';
  ctx.fillText('s (mm)', mL + plotW / 2, height - 26);
}
