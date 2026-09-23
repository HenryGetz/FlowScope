"""Canonical cardiac structure ids, dataset alias table, and palette (shared contract).

Matching is "normalized lowercase alnum": lowercase, strip non-alphanumerics and
dataset filename suffixes, then look up the alias table. TotalSegmentator
heartchambers_highres schema names and the ImageCAS 14-structure vocabulary are
covered. Unknown/raw names keep their raw name and get an opaque hashed-hue
material.
"""

from __future__ import annotations

import colorsys
import hashlib

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

# [R, G, B, A] 0..1 ; A < 0.99 -> alphaMode BLEND, else OPAQUE
PALETTE: dict[str, tuple[float, float, float, float]] = {
    'heart_myocardium': (0.78, 0.25, 0.25, 0.35),
    'heart_ventricle_left': (0.85, 0.20, 0.20, 1.0),
    'heart_atrium_left': (0.60, 0.30, 0.70, 1.0),
    'heart_ventricle_right': (0.20, 0.45, 0.85, 1.0),
    'heart_atrium_right': (0.30, 0.65, 0.85, 1.0),
    'heart_atrial_appendage_left': (0.70, 0.50, 0.85, 1.0),
    'coronary_artery_left': (0.95, 0.75, 0.10, 1.0),
    'coronary_artery_right': (0.90, 0.60, 0.10, 1.0),
    'coronary_arteries': (0.95, 0.70, 0.10, 1.0),
    'pulmonary_veins': (0.40, 0.80, 0.70, 1.0),
    'pulmonary_artery': (0.40, 0.75, 0.80, 1.0),
    'aorta': (0.90, 0.30, 0.30, 1.0),
    'vena_cava_superior': (0.30, 0.50, 0.90, 1.0),
    'vena_cava_inferior': (0.25, 0.45, 0.85, 1.0),
    'pericardial_fat': (0.95, 0.90, 0.60, 0.50),
    'epicardial_fat': (0.95, 0.85, 0.55, 0.50),
    'heart': (0.70, 0.32, 0.32, 0.30),
}

ALPHA_BLEND_MAX = 0.99

# Selection whitelist for mask/multilabel ingestion: the palette ids (the 16
# contract ids plus 'heart'). Anything else is skipped with reason
# 'non_cardiac' unless whitelisted via --extra-names or ingested via
# --all-names. --stl-dir is an explicit file set and is never filtered.
CARDIAC_IDS: frozenset[str] = frozenset(PALETTE)

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
    'la': 'heart_atrium_left',
    'leftatrium': 'heart_atrium_left',
    'ra': 'heart_atrium_right',
    'rightatrium': 'heart_atrium_right',
    'rv': 'heart_ventricle_right',
    'rightventricle': 'heart_ventricle_right',
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


def alpha_mode(rgba: tuple[float, float, float, float]) -> str:
    """glTF alphaMode per palette rule: A < 0.99 -> BLEND, else OPAQUE."""
    return 'BLEND' if rgba[3] < ALPHA_BLEND_MAX else 'OPAQUE'


def hashed_hue(name: str) -> tuple[float, float, float, float]:
    """Deterministic opaque hashed-hue RGBA for unknown/raw names."""
    digest = hashlib.sha256(name.encode('utf-8')).digest()
    hue = int.from_bytes(digest[:4], 'big') / float(0xFFFFFFFF)
    red, green, blue = colorsys.hls_to_rgb(hue, 0.55, 0.6)
    return (red, green, blue, 1.0)


def material_for(name: str) -> tuple[tuple[float, float, float, float], bool]:
    """Return (RGBA baseColorFactor, is_canonical) for a structure name."""
    rgba = PALETTE.get(name)
    if rgba is not None:
        return rgba, True
    return hashed_hue(name), False
