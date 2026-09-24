"""Canonical cardiac structure ids, dataset alias table, and material table (shared contract).

Matching is "normalized lowercase alnum": lowercase, strip non-alphanumerics and
dataset filename suffixes, then look up the alias table. TotalSegmentator
heartchambers_highres schema names and the ImageCAS 14-structure vocabulary are
covered. Unknown/raw names keep their raw name and get an opaque hashed-hue
material.
"""

from __future__ import annotations

import colorsys
import hashlib
from dataclasses import dataclass

CANONICAL_IDS: tuple[str, ...] = (
    'heart_myocardium',
    'heart_ventricle_left',
    'heart_atrium_left',
    'heart_ventricle_right',
    'heart_atrium_right',
    'heart_atrial_appendage_left',
    'coronary_artery_left',
    'coronary_artery_right',
    'coronary_arteries',
    'pulmonary_veins',
    'pulmonary_artery',
    'aorta',
    'vena_cava_superior',
    'vena_cava_inferior',
    'pericardial_fat',
    'epicardial_fat',
)

# TotalSegmentator 104-class whole-heart envelope. Anatomically distinct from
# heart_myocardium (whole envelope vs LV myocardium) -- deliberately NOT an
# alias of it. Not part of the 16-id contract list, but first-class here.
EXTRA_IDS: tuple[str, ...] = ('heart',)

@dataclass(frozen=True)
class MaterialSpec:
    """PBR glTF material parameters for one structure (shared contract).

    Hex RGB is the source of truth; ``rgba`` (channel/255 floats) and
    ``alpha_mode`` are derived at access so the JS mirror cannot drift from
    stored floats.
    """

    hex: int     # 0xRRGGBB, source of truth
    alpha: float
    roughness: float  # glTF pbrMetallicRoughness.roughnessFactor
    metallic: float   # glTF pbrMetallicRoughness.metallicFactor

    @property
    def rgba(self) -> tuple[float, float, float, float]:
        """baseColorFactor [R, G, B, A] 0..1 with channel/255 floats."""
        return (
            ((self.hex >> 16) & 0xFF) / 255.0,
            ((self.hex >> 8) & 0xFF) / 255.0,
            (self.hex & 0xFF) / 255.0,
            self.alpha,
        )

    @property
    def alpha_mode(self) -> str:
        """glTF alphaMode per the contract rule: A < 0.99 -> BLEND, else OPAQUE."""
        return 'BLEND' if self.alpha < ALPHA_BLEND_MAX else 'OPAQUE'


# Contract material table: id -> (hex RGB, alpha, roughnessFactor, metallicFactor).
MATERIALS: dict[str, MaterialSpec] = {
    'heart_myocardium': MaterialSpec(0xA64B4B, 1.0, 0.65, 0.05),
    'heart': MaterialSpec(0xA64B4B, 1.0, 0.65, 0.05),
    'aorta': MaterialSpec(0xD32F2F, 1.0, 0.35, 0.0),
    'pulmonary_artery': MaterialSpec(0x1976D2, 1.0, 0.35, 0.0),
    'pulmonary_veins': MaterialSpec(0x0D47A1, 1.0, 0.35, 0.0),
    'vena_cava_superior': MaterialSpec(0x0D47A1, 1.0, 0.35, 0.0),
    'vena_cava_inferior': MaterialSpec(0x0D47A1, 1.0, 0.35, 0.0),
    'heart_ventricle_left': MaterialSpec(0xC62828, 0.4, 0.35, 0.0),
    'heart_atrium_left': MaterialSpec(0xC62828, 0.4, 0.35, 0.0),
    'heart_atrial_appendage_left': MaterialSpec(0xC62828, 0.4, 0.35, 0.0),
    'heart_ventricle_right': MaterialSpec(0x0D47A1, 0.4, 0.35, 0.0),
    'heart_atrium_right': MaterialSpec(0x0D47A1, 0.4, 0.35, 0.0),
    'coronary_artery_left': MaterialSpec(0xFFB300, 1.0, 0.2, 0.0),
    'coronary_artery_right': MaterialSpec(0xFFB300, 1.0, 0.2, 0.0),
    'coronary_arteries': MaterialSpec(0xFFB300, 1.0, 0.2, 0.0),
    'pericardial_fat': MaterialSpec(0xEFE3A8, 1.0, 0.8, 0.0),
    'epicardial_fat': MaterialSpec(0xEFE3A8, 1.0, 0.8, 0.0),
}

# Contract structure groups (id -> 'myocardium'|'chambers'|'great_vessels'|
# 'coronaries'|'other'). Pericardial/epicardial fat and any unmapped/raw name
# are 'other'.
STRUCTURE_GROUPS: dict[str, str] = {
    'heart_myocardium': 'myocardium',
    'heart': 'myocardium',
    'heart_ventricle_left': 'chambers',
    'heart_atrium_left': 'chambers',
    'heart_ventricle_right': 'chambers',
    'heart_atrium_right': 'chambers',
    'heart_atrial_appendage_left': 'chambers',
    'aorta': 'great_vessels',
    'pulmonary_artery': 'great_vessels',
    'pulmonary_veins': 'great_vessels',
    'vena_cava_superior': 'great_vessels',
    'vena_cava_inferior': 'great_vessels',
    'coronary_artery_left': 'coronaries',
    'coronary_artery_right': 'coronaries',
    'coronary_arteries': 'coronaries',
    'pericardial_fat': 'other',
    'epicardial_fat': 'other',
}

ALPHA_BLEND_MAX = 0.99

# Selection whitelist for mask/multilabel ingestion: the material table ids
# (the 16 contract ids plus 'heart'). Anything else is skipped with reason
# 'non_cardiac' unless whitelisted via --extra-names or ingested via
# --all-names. --stl-dir is an explicit file set and is never filtered.
CARDIAC_IDS: frozenset[str] = frozenset(MATERIALS)

# heartchambers_highres `--ml` label ids (default multilabel map)
DEFAULT_LABEL_MAP: dict[int, str] = {
    1: 'heart_myocardium',
    2: 'heart_atrium_left',
    3: 'heart_ventricle_left',
    4: 'heart_atrium_right',
    5: 'heart_ventricle_right',
    6: 'aorta',
    7: 'pulmonary_artery',
}

_FILE_SUFFIXES = ('.nii.gz', '.nii', '.stl')

# Alias keys are already normalized (lowercase alphanumerics only).
# Covers the ImageCAS 14-structure vocabulary (LV myocardium, LV, LA, RA, RV,
# LAA, coronary arteries, pulmonary veins, pericardial fat, epicardial fat,
# pulmonary arteries, aorta, SVC, IVC) plus common filename spellings; the
# TotalSegmentator schema names are the canonical ids themselves.
_DATASET_ALIASES: dict[str, str] = {
    'lvmyocardium': 'heart_myocardium',
    'myocardium': 'heart_myocardium',
    'lv': 'heart_ventricle_left',
    'leftventricle': 'heart_ventricle_left',
    'heartleftventricle': 'heart_ventricle_left',  # side-first "heart_left_ventricle"
    'la': 'heart_atrium_left',
    'leftatrium': 'heart_atrium_left',
    'heartleftatrium': 'heart_atrium_left',  # side-first "heart_left_atrium"
    'ra': 'heart_atrium_right',
    'rightatrium': 'heart_atrium_right',
    'heartrightatrium': 'heart_atrium_right',  # side-first "heart_right_atrium"
    'rv': 'heart_ventricle_right',
    'rightventricle': 'heart_ventricle_right',
    'heartrightventricle': 'heart_ventricle_right',  # side-first "heart_right_ventricle"
    'laa': 'heart_atrial_appendage_left',
    'leftatrialappendage': 'heart_atrial_appendage_left',
    'atrialappendage': 'heart_atrial_appendage_left',
    'atrialappendageleft': 'heart_atrial_appendage_left',
    'laappendage': 'heart_atrial_appendage_left',
    'coronaryarteries': 'coronary_arteries',
    'coronaryartery': 'coronary_arteries',
    'leftcoronaryartery': 'coronary_artery_left',
    'rightcoronaryartery': 'coronary_artery_right',
    'lca': 'coronary_artery_left',
    'rca': 'coronary_artery_right',
    'lad': 'coronary_artery_left',
    'lcx': 'coronary_artery_left',
    'pulmonaryveins': 'pulmonary_veins',
    'pulmonaryvein': 'pulmonary_veins',
    'pericardialfat': 'pericardial_fat',
    'precardialfat': 'pericardial_fat',  # repo typo variant of "pericardial fat"
    'epicardialfat': 'epicardial_fat',
    'pulmonaryarteries': 'pulmonary_artery',
    'pulmonaryartery': 'pulmonary_artery',
    'pa': 'pulmonary_artery',
    'svc': 'vena_cava_superior',
    'superiorvenacava': 'vena_cava_superior',
    'ivc': 'vena_cava_inferior',
    'inferiorvenacava': 'vena_cava_inferior',
}


def normalize(name: str) -> str:
    """Normalized lowercase alphanumerics-only form used for alias matching."""
    return ''.join(ch for ch in name.lower() if ch.isalnum())


def strip_suffix(name: str) -> str:
    """Drop a trailing dataset filename suffix (.nii.gz/.nii/.stl), if present."""
    lowered = name.lower()
    for suffix in _FILE_SUFFIXES:
        if lowered.endswith(suffix):
            return name[: -len(suffix)]
    return name


ALIASES: dict[str, str] = {
    **{normalize(key): value for key, value in _DATASET_ALIASES.items()},
    **{normalize(cid): cid for cid in (*CANONICAL_IDS, *EXTRA_IDS)},
}


def resolve_name(raw: str) -> str:
    """Map a dataset filename/label name to its canonical id.

    Known source extensions (.nii.gz/.nii/.stl) are stripped before alias
    resolution; unresolvable names fall back to the bare stem (kept raw, with
    an opaque hashed-hue material).
    """
    stem = strip_suffix(raw)
    return ALIASES.get(normalize(stem), stem)


def structure_group(name: str) -> str:
    """Contract group for a raw structure name.

    Resolves the raw name through resolve_name first; unmapped/raw names and
    fat structures are 'other'.
    """
    return STRUCTURE_GROUPS.get(resolve_name(name), 'other')


def hashed_hue(name: str) -> MaterialSpec:
    """Deterministic opaque hashed-hue fallback material for unknown/raw names.

    Hash/hue scheme unchanged (opaque 1.0 alpha); per the contract the
    fallback is roughness 0.85, metallic 0.0 and the color is stored as 8-bit
    hex (source of truth) like every other material.
    """
    digest = hashlib.sha256(name.encode('utf-8')).digest()
    hue = int.from_bytes(digest[:4], 'big') / float(0xFFFFFFFF)
    red, green, blue = colorsys.hls_to_rgb(hue, 0.55, 0.6)
    rgb = (round(red * 255) << 16) | (round(green * 255) << 8) | round(blue * 255)
    return MaterialSpec(rgb, 1.0, 0.85, 0.0)


def material_for(name: str) -> tuple[MaterialSpec, bool]:
    """Return (MaterialSpec, is_canonical) for a structure name.

    is_canonical False -> hashed_hue fallback material (unknown/raw name).
    """
    spec = MATERIALS.get(name)
    if spec is not None:
        return spec, True
    return hashed_hue(name), False
