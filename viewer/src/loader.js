import * as THREE from 'three';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';

/** Default model location relative to the page (viewer/models/, served at /models/). */
export const DEFAULT_MODEL_URL = 'models/case_01.glb';

/** Model URL: `?model=<url>` override, else `models/case_01.glb` relative. */
export function modelURL() {
  const fromQuery = new URLSearchParams(window.location.search).get('model');
  const trimmed = fromQuery ? fromQuery.trim() : '';
  return trimmed || DEFAULT_MODEL_URL;
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
 * Volume-pair URLs derived from the model URL stem: `<stem>_volume.bin` and
 * `<stem>_meta.json` beside the model. Returns null when the model URL is not
 * a *.glb.
 */
export function volumeURLs(modelUrl = modelURL()) {
  const match = /^(.*)\.glb$/i.exec(String(modelUrl));
  return match ? { volume: `${match[1]}_volume.bin`, meta: `${match[1]}_meta.json` } : null;
}

let volumeWarned = false;

/**
 * Load the quantized CT volume pair (uint8 `<stem>_volume.bin` + `<stem>_meta.json`)
 * as a gl.R8 Data3DTexture ready for `texture(sampler3D, uvw)`. Resolves null
 * when the pair is missing: the viewer then runs without MPR (warns once).
 */
export async function loadVolume(modelUrl = modelURL()) {
  const urls = volumeURLs(modelUrl);
  if (!urls) return null;
  try {
    const [binResponse, metaResponse] = await Promise.all([
      fetch(new URL(urls.volume, document.baseURI).href),
      fetch(new URL(urls.meta, document.baseURI).href),
    ]);
    if (!binResponse.ok || !metaResponse.ok) {
      throw new Error(`HTTP ${!binResponse.ok ? binResponse.status : metaResponse.status}`);
    }
    const [buffer, meta] = await Promise.all([binResponse.arrayBuffer(), metaResponse.json()]);
    const [dx, dy, dz] = meta.dimensions;
    const texture = new THREE.Data3DTexture(new Uint8Array(buffer), dx, dy, dz);
    texture.format = THREE.RedFormat;
    texture.type = THREE.UnsignedByteType;
    texture.minFilter = THREE.LinearFilter;
    texture.magFilter = THREE.LinearFilter;
    texture.wrapS = THREE.ClampToEdgeWrapping;
    texture.wrapT = THREE.ClampToEdgeWrapping;
    texture.wrapR = THREE.ClampToEdgeWrapping;
    texture.unpackAlignment = 1;
    texture.needsUpdate = true;
    return { texture, meta };
  } catch {
    if (!volumeWarned) {
      volumeWarned = true;
      console.warn(`no volume pair for "${modelUrl}" — running without MPR`);
    }
    return null;
  }
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
