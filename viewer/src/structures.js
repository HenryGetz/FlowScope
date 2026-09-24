/**
 * Canonical cardiac structure contract shared by the pipeline and this viewer.
 *
 * Material rows are (hex RGB, alpha, roughness, metallic): the hex ints are
 * the single source of truth and the RGBA floats are derived as channel/255
 * for guaranteed parity with the Python pipeline. alpha < 0.99 => alphaMode
 * BLEND, otherwise OPAQUE. Unknown/raw names keep their raw name and get an
 * opaque hashed-hue color. The GLB carries these same PBR values per
 * structure: glTF alphaMode/roughness/metallic are authoritative in the
 * viewer — never override opacity, alpha or transparent there.
 */

/** id -> (hex RGB, alpha, roughness, metallic), mirrored verbatim by pipeline/. */
export const MATERIALS = {
  heart_myocardium: [0xa64b4b, 1.0, 0.65, 0.05],
  heart: [0xa64b4b, 1.0, 0.65, 0.05],
  aorta: [0xd32f2f, 1.0, 0.35, 0.0],
  pulmonary_artery: [0x1976d2, 1.0, 0.35, 0.0],
  pulmonary_veins: [0x0d47a1, 1.0, 0.35, 0.0],
  vena_cava_superior: [0x0d47a1, 1.0, 0.35, 0.0],
  vena_cava_inferior: [0x0d47a1, 1.0, 0.35, 0.0],
  heart_ventricle_left: [0xc62828, 0.4, 0.35, 0.0],
  heart_atrium_left: [0xc62828, 0.4, 0.35, 0.0],
  heart_atrial_appendage_left: [0xc62828, 0.4, 0.35, 0.0],
  heart_ventricle_right: [0x0d47a1, 0.4, 0.35, 0.0],
  heart_atrium_right: [0x0d47a1, 0.4, 0.35, 0.0],
  coronary_artery_left: [0xffb300, 1.0, 0.2, 0.0],
  coronary_artery_right: [0xffb300, 1.0, 0.2, 0.0],
  coronary_arteries: [0xffb300, 1.0, 0.2, 0.0],
  pericardial_fat: [0xefe3a8, 1.0, 0.8, 0.0],
  epicardial_fat: [0xefe3a8, 1.0, 0.8, 0.0],
};

/** Structure group -> member ids (unmapped/raw names fall into `other`). */
export const GROUPS = {
  myocardium: ['heart_myocardium', 'heart'],
  chambers: [
    'heart_ventricle_left',
    'heart_atrium_left',
    'heart_ventricle_right',
    'heart_atrium_right',
    'heart_atrial_appendage_left',
  ],
  great_vessels: [
    'aorta',
    'pulmonary_artery',
    'pulmonary_veins',
    'vena_cava_superior',
    'vena_cava_inferior',
  ],
  coronaries: ['coronary_artery_left', 'coronary_artery_right', 'coronary_arteries'],
  other: ['pericardial_fat', 'epicardial_fat'],
};

export const OPAQUE = 'OPAQUE';
export const BLEND = 'BLEND';

/** Group of pericardial/epicardial fat and any unmapped/raw name. */
export const OTHER_GROUP = 'other';

/** PBR fallback for unmapped/raw names (hashed-hue color, no contract row). */
const UNKNOWN_ROUGHNESS = 0.5;
const UNKNOWN_METALLIC = 0.0;

export function alphaModeFor(rgba) {
  return rgba[3] < 0.99 ? BLEND : OPAQUE;
}

/** RGBA in 0..1 derived from a contract hex-int row (channel/255 parity). */
function rgbaFrom(hex, alpha) {
  return [((hex >> 16) & 0xff) / 255, ((hex >> 8) & 0xff) / 255, (hex & 0xff) / 255, alpha];
}

/** id -> RGBA in 0..1, derived; the hex ints in MATERIALS are the truth. */
export const PALETTE = Object.fromEntries(
  Object.entries(MATERIALS).map(([id, [hex, alpha]]) => [id, rgbaFrom(hex, alpha)]),
);

const GROUP_BY_ID = {};
for (const [group, ids] of Object.entries(GROUPS)) {
  for (const id of ids) GROUP_BY_ID[id] = group;
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
  // side-first chamber order (e.g. `heart_left_ventricle` beside
  // heartchambers_highres' chamber-first `heart_ventricle_left`)
  heartleftventricle: 'heart_ventricle_left',
  heartleftatrium: 'heart_atrium_left',
  heartrightventricle: 'heart_ventricle_right',
  heartrightatrium: 'heart_atrium_right',
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
for (const id of Object.keys(MATERIALS)) {
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
  const material = known ? MATERIALS[id] : null;
  return {
    id,
    rgba,
    alphaMode: alphaModeFor(rgba),
    known,
    group: known ? GROUP_BY_ID[id] : OTHER_GROUP,
    roughness: material ? material[2] : UNKNOWN_ROUGHNESS,
    metallic: material ? material[3] : UNKNOWN_METALLIC,
  };
}

/** Viewer group of a raw GLB node/file name (unmapped/raw names: `other`). */
export function structureGroup(rawName) {
  return resolveStructure(rawName).group;
}
