/**
 * Canonical cardiac structure contract shared by the pipeline and this viewer.
 *
 * Palette values are RGBA in 0..1 and MUST stay identical to the shared GLB
 * contract: alpha < 0.99 => alphaMode BLEND, otherwise OPAQUE. Unknown/raw
 * names keep their raw name and get an opaque hashed-hue color.
 */

export const PALETTE = {
  heart_myocardium: [0.78, 0.25, 0.25, 0.35],
  heart_ventricle_left: [0.85, 0.20, 0.20, 1],
  heart_atrium_left: [0.60, 0.30, 0.70, 1],
  heart_ventricle_right: [0.20, 0.45, 0.85, 1],
  heart_atrium_right: [0.30, 0.65, 0.85, 1],
  heart_atrial_appendage_left: [0.70, 0.50, 0.85, 1],
  heart: [0.70, 0.32, 0.32, 0.30],
  coronary_artery_left: [0.95, 0.75, 0.10, 1],
  coronary_artery_right: [0.90, 0.60, 0.10, 1],
  coronary_arteries: [0.95, 0.70, 0.10, 1],
  pulmonary_veins: [0.40, 0.80, 0.70, 1],
  pulmonary_artery: [0.40, 0.75, 0.80, 1],
  aorta: [0.90, 0.30, 0.30, 1],
  vena_cava_superior: [0.30, 0.50, 0.90, 1],
  vena_cava_inferior: [0.25, 0.45, 0.85, 1],
  pericardial_fat: [0.95, 0.90, 0.60, 0.50],
  epicardial_fat: [0.95, 0.85, 0.55, 0.50],
};

export const OPAQUE = 'OPAQUE';
export const BLEND = 'BLEND';

export function alphaModeFor(rgba) {
  return rgba[3] < 0.99 ? BLEND : OPAQUE;
}

/** Normalize a raw name for alias matching: lowercase, alphanumerics only. */
export function normalizeName(raw) {
  return String(raw).toLowerCase().replace(/[^a-z0-9]/g, '');
}

/**
 * Alias map: normalized lowercase alnum key -> canonical id. Covers
 * TotalSegmentator heart schema names (same spelling as the canonical ids),
 * heartchambers_highres `--ml` label ids 1..7, and the ImageCAS
 * 14-structure vocabulary, plus common filename/word-order variants.
 */
const ALIASES = {
  // TotalSegmentator 104-class schema: whole-heart envelope
  // (anatomically distinct from heart_myocardium — never alias between them)
  heart: 'heart',
  // ImageCAS 14-structure vocabulary
  lvmyocardium: 'heart_myocardium',
  lv: 'heart_ventricle_left',
  la: 'heart_atrium_left',
  ra: 'heart_atrium_right',
  rv: 'heart_ventricle_right',
  laa: 'heart_atrial_appendage_left',
  coronaryarteries: 'coronary_arteries',
  pulmonaryveins: 'pulmonary_veins',
  pericardialfat: 'pericardial_fat',
  epicardialfat: 'epicardial_fat',
  pulmonaryarteries: 'pulmonary_artery',
  aorta: 'aorta',
  svc: 'vena_cava_superior',
  ivc: 'vena_cava_inferior',
  // heartchambers_highres `--ml` label ids
  1: 'heart_myocardium',
  2: 'heart_atrium_left',
  3: 'heart_ventricle_left',
  4: 'heart_atrium_right',
  5: 'heart_ventricle_right',
  6: 'aorta',
  7: 'pulmonary_artery',
  // filename / word-order variants
  myocardium: 'heart_myocardium',
  leftventricle: 'heart_ventricle_left',
  rightventricle: 'heart_ventricle_right',
  leftatrium: 'heart_atrium_left',
  rightatrium: 'heart_atrium_right',
  leftatrialappendage: 'heart_atrial_appendage_left',
  pulmonaryvein: 'pulmonary_veins',
  pulmonaryartery: 'pulmonary_artery',
  coronaryartery: 'coronary_arteries',
  coronaryarteryleft: 'coronary_artery_left',
  leftcoronaryartery: 'coronary_artery_left',
  coronaryarteryright: 'coronary_artery_right',
  rightcoronaryartery: 'coronary_artery_right',
  venacavasuperior: 'vena_cava_superior',
  superiorvenacava: 'vena_cava_superior',
  venacavainferior: 'vena_cava_inferior',
  inferiorvenacava: 'vena_cava_inferior',
};

// Every canonical id matches itself through normalized lookup as well.
for (const id of Object.keys(PALETTE)) {
  const key = normalizeName(id);
  if (!(key in ALIASES)) ALIASES[key] = id;
}

/** Deterministic opaque hashed-hue color (RGBA 0..1) for unknown names. */
export function hashedHue(name) {
  const key = normalizeName(name);
  let hash = 2166136261;
  for (let i = 0; i < key.length; i += 1) {
    hash ^= key.charCodeAt(i);
    hash = Math.imul(hash, 16777619);
  }
  const hue = ((hash >>> 0) % 360) / 360;
  return [...hslToRgb(hue, 0.55, 0.55), 1];
}

function hslToRgb(h, s, l) {
  const f = (n) => {
    const k = (n + h * 12) % 12;
    const a = s * Math.min(l, 1 - l);
    return l - a * Math.max(-1, Math.min(k - 3, 9 - k, 1));
  };
  return [f(0), f(8), f(4)];
}

/**
 * Resolve a raw GLB node/file name to the shared contract entry.
 * Unknown names keep their raw name and a hashed-hue opaque material color.
 */
export function resolveStructure(rawName) {
  const key = normalizeName(rawName);
  const known = Object.prototype.hasOwnProperty.call(ALIASES, key);
  const id = known ? ALIASES[key] : String(rawName);
  const rgba = known ? [...PALETTE[id]] : hashedHue(id);
  return { id, rgba, alphaMode: alphaModeFor(rgba), known };
}
