import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';

/** Default model location relative to the page (viewer/public/assets/). */
export const DEFAULT_MODEL_URL = 'assets/cardiac.glb';

/** Model URL: `?model=<url>` override, else `assets/cardiac.glb` relative. */
export function modelURL() {
  const fromQuery = new URLSearchParams(window.location.search).get('model');
  const trimmed = fromQuery ? fromQuery.trim() : '';
  return trimmed || DEFAULT_MODEL_URL;
}

/**
 * Directory URL beside the resolved model (usually `viewer/public/assets/`):
 * the default base for the C5 contrast payloads `case_{case}_{profile}_...`.
 */
export function assetsBaseURL() {
  const url = new URL(modelURL(), document.baseURI);
  return url.href.endsWith('/') ? url.href : `${url.href.slice(0, url.href.lastIndexOf('/') + 1)}`;
}

/**
 * Contrast payload override (C5): `?payload=<url>` pointing at the metadata
 * JSON, the contrast bin, or a base directory — empty when absent (then
 * cfd_playback derives the names under `assetsBaseURL()`).
 */
export function payloadURL() {
  const fromQuery = new URLSearchParams(window.location.search).get('payload');
  const trimmed = fromQuery ? fromQuery.trim() : '';
  return trimmed;
}

/**
 * Load a GLB (KHR_mesh_quantization decodes natively in GLTFLoader).
 * Resolves with the GLTF, rejects with an Error describing the failure.
 *
 * Runtime note: normalized integer vertex fetch with single-draw frames
 * rasterizes zero fragments on headless llvmpipe/SwiftShader (shape-sensitive
 * VAO path); mitigated by runtime dequantization to Float32 at load; the
 * shipped .glb keeps KHR_mesh_quantization quantized buffers per the brief's
 * compressed-vertex-buffer requirement. The residual 65535x fit-basis error on
 * single-structure loads was eliminated by the explicit measureBox walk +
 * bounds-cache invalidation after attribute replacement.
 */
export function loadModel(url = modelURL(), onProgress = null) {
  const resolved = new URL(url, document.baseURI).href;
  const loader = new GLTFLoader();
  return new Promise((resolve, reject) => {
    loader.load(
      resolved,
      (gltf) => {
        decodeNormalizedAttributes(gltf.scene);
        resolve(gltf);
      },
      onProgress ? (event) => onProgress(event) : undefined,
      (err) => reject(new Error(`failed to load "${url}": ${err && err.message ? err.message : err}`)),
    );
  });
}

/**
 * Decode normalized integer attributes (KHR_mesh_quantization position/normal
 * buffers) to Float32 once at load. The shipped GLB keeps its quantized
 * buffers; this is a runtime decode detail that sidesteps the normalized
 * integer vertex-fetch path entirely.
 */
function decodeNormalizedAttributes(gltfScene) {
  gltfScene.traverse((node) => {
    const geometry = node.geometry;
    if (!geometry || !geometry.attributes) return;
    for (const key of Object.keys(geometry.attributes)) {
      // Playback attributes (cfd_playback.js) must stay out of this Float32
      // rewrite: they are uint8, un-normalized, and attached after decode.
      if (key === 'aC0' || key === 'aC1') continue;
      const attr = geometry.attributes[key];
      if (!attr || attr.normalized !== true || typeof attr.getComponent !== 'function') continue;
      if (attr.isInterleavedBufferAttribute) continue;
      const { count, itemSize } = attr;
      const decoded = new Float32Array(count * itemSize);
      for (let i = 0; i < count; i += 1) {
        for (let k = 0; k < itemSize; k += 1) {
          decoded[i * itemSize + k] = attr.getComponent(i, k);
        }
      }
      geometry.setAttribute(key, new attr.constructor(decoded, itemSize, false));
    }
    // Any bounds computed before the replacement describe the raw quantized
    // basis — force every consumer to recompute from the decoded attributes.
    geometry.boundingBox = null;
    geometry.boundingSphere = null;
  });
}

/**
 * Collect per-structure nodes from a loaded GLB: the contract root node
 * `cardiac` has exactly one named child per structure (node AND mesh named
 * with the canonical id, or the raw name when unmapped).
 * Returns [{ name, object }] in GLB node order.
 */
export function collectStructures(gltfScene) {
  const root = gltfScene.getObjectByName('cardiac') || gltfScene;
  return root.children.map((child, index) => ({
    name: child.name || firstMeshName(child) || `structure_${index}`,
    object: child,
  }));
}

function firstMeshName(object) {
  let found = null;
  object.traverse((node) => {
    if (!found && node.isMesh && node.name) found = node.name;
  });
  return found;
}
