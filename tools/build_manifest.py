#!/usr/bin/env python3
"""Validate acquired data under data/ and write data/manifest.json per the shared contract.

Layout produced (contract):
  {artifacts: [{path, dataset, case_id, structures, source_url, license, bytes,
                validated: {loaded, nonzero_voxels|triangles, bbox_mm, ...}}],
   blocked:   [{target, reason, user_action}],
   label_map: {...},            # mandated by orchestrator: integer-label -> structure ids
   segmentation_runs: [...]}    # runtime_s / device of TotalSegmentator runs

Structure-name resolution: normalized (lowercase alnum) matching against the canonical
structure ids and the alias table below (TotalSegmentator schema, heartchambers_highres,
ImageCAS/CCT-FM vocabulary). Unmapped names keep their raw name (contract rule).
"""
import json
import os
import sys
import time

import numpy as np
import nibabel as nib

CANONICAL = [
    "heart_myocardium", "heart_ventricle_left", "heart_atrium_left",
    "heart_ventricle_right", "heart_atrium_right", "heart_atrial_appendage_left",
    "coronary_artery_left", "coronary_artery_right", "coronary_arteries",
    "pulmonary_veins", "pulmonary_artery", "aorta",
    "vena_cava_superior", "vena_cava_inferior",
    "pericardial_fat", "epicardial_fat",
]

# alias (normalized) -> canonical id. Covers: canonical ids themselves, TotalSegmentator
# schema (heartchambers_highres + total task), and the ImageCAS/CCT-FM 14-structure
# vocabulary ("LV myocardium", "SVC", "LA Appendage", "Precardial Fat", ...).
ALIASES = {}


def _reg(canonical, *aliases):
    ALIASES[_norm(canonical)] = canonical
    for a in aliases:
        ALIASES[_norm(a)] = canonical


def _norm(s):
    return "".join(ch for ch in s.lower() if ch.isalnum())


_reg("heart_myocardium", "lv_myocardium", "left_ventricular_myocardium", "lv myocardium", "lvm")
_reg("heart_ventricle_left", "left_ventricle", "lv", "left_ventricle_blood_pool")
_reg("heart_atrium_left", "left_atrium", "la", "left_atrium_blood_pool")
_reg("heart_ventricle_right", "right_ventricle", "rv", "right_ventricle_blood_pool")
_reg("heart_atrium_right", "right_atrium", "ra", "right_atrium_blood_pool")
_reg("heart_atrial_appendage_left", "atrial_appendage_left", "left_atrial_appendage",
     "la_appendage", "laa")
_reg("coronary_artery_left", "left_coronary_artery")
_reg("coronary_artery_right", "right_coronary_artery")
_reg("coronary_arteries", "coronary_artery", "coronary_arteries_all")
_reg("pulmonary_veins", "pulmonary_vein", "pv")
_reg("pulmonary_artery", "pulmonary_arteries", "pa")
_reg("aorta", "ascending_aorta", "aor")
_reg("vena_cava_superior", "superior_vena_cava", "svc")
_reg("vena_cava_inferior", "inferior_vena_cava", "ivc")
_reg("pericardial_fat", "precardial_fat", "pf")
_reg("epicardial_fat", "ef")

ZENODO = "https://zenodo.org/records/8367088/files/Totalsegmentator_dataset_v2.zip?download=1"
ZENODO_RECORD = "https://doi.org/10.5281/zenodo.8367088"
HF_BASE = "https://huggingface.co/datasets/AI-CVM/Cardiac-CT"

LICENSES = {
    "imagecas": "CC-BY-NC-ND-4.0",
    "imagecas_stl": "CC-BY-NC-ND-4.0",
    "kaggle-imagecas": "Attribution-NonCommercial 4.0 International (CC BY-NC 4.0)",
    "totalseg_ct": "CC BY 4.0",
    "totalseg_heartchambers_highres": "CC BY 4.0 (input); TotalSegmentator heartchambers_highres weights documented as restricted to non-commercial use unless separately licensed",
}

KAGGLE_URL = "https://www.kaggle.com/datasets/xiaoweixumedicalai/imagecas"

LABEL_MAP = {
    "imagecas_multilabel": {
        "note": ("Integer-label -> structure id for the multilabel segmentations "
                 "Train(ImageCAS)/segmentations/<case>.nii.gz of HF AI-CVM/Cardiac-CT "
                 "(nnU-Net Dataset051_CT_Cardio_FULL_Organs labelsTr). 0 = background. "
                 "Canonical ids follow the shared contract alias mapping."),
        "labels": {
            "0": {"source_name": "background", "structure_id": "background"},
            "1": {"source_name": "Coronary Artery", "structure_id": "coronary_arteries"},
            "2": {"source_name": "LV Myocardium", "structure_id": "heart_myocardium"},
            "3": {"source_name": "Left Atrium", "structure_id": "heart_atrium_left"},
            "4": {"source_name": "Left Ventricle", "structure_id": "heart_ventricle_left"},
            "5": {"source_name": "Right Atrium", "structure_id": "heart_atrium_right"},
            "6": {"source_name": "Right Ventricle", "structure_id": "heart_ventricle_right"},
            "7": {"source_name": "Aorta", "structure_id": "aorta"},
            "8": {"source_name": "Pulmonary Arteries", "structure_id": "pulmonary_artery"},
            "9": {"source_name": "Precardial Fat (repo spelling; pericardial fat)", "structure_id": "pericardial_fat"},
            "10": {"source_name": "Epicardial Fat", "structure_id": "epicardial_fat"},
            "11": {"source_name": "Pulmonary Vein", "structure_id": "pulmonary_veins"},
            "12": {"source_name": "Superior Vena Cava", "structure_id": "vena_cava_superior"},
            "13": {"source_name": "Inferior Vena Cava", "structure_id": "vena_cava_inferior"},
            "14": {"source_name": "LA Appendage", "structure_id": "heart_atrial_appendage_left"},
        },
        "sources": [
            {"url": "https://github.com/AI-in-Cardiovascular-Medicine/CCT-FM/blob/main/model_training_and_benchmarks/README.md",
             "note": "primary source: official CCT-FM repo label table of nnU-Net Dataset051_CT_Cardio_FULL_Organs (labelsTr = the released multilabel masks)"},
            {"url": "https://arxiv.org/abs/2607.11287",
             "note": "paper (Sec. 4.3.1 + Fig. 1 abbreviations) corroborates the 14-structure vocabulary: LV, RV, LA, RA, LAA, LV myocardium, ascending aorta, pulmonary arteries, pulmonary veins, SVC, IVC, coronary arteries, epicardial fat, pericardial fat"},
        ],
        "verified_from_primary_source": True,
        "empirically_verified": False,
        "empirical_note": ("integer ids could not be checked against the released .nii.gz "
                           "files because the HF repo data files are gated (HTTP 401 GatedRepo); "
                           "re-verify with per-label voxel counts once access is granted"),
    },
    "imagecas_kaggle": {
        "note": ("Integer-label -> structure id for the base ImageCAS label volumes "
                 "(<ID>/label.nii.gz of Kaggle xiaoweixumedicalai/imagecas). Empirically "
                 "binary {0,1} in all three acquired cases (601, 700, 798)."),
        "labels": {
            "0": {
                "source_name": "background",
                "structure_id": "background",
                "status": "verified",
                "evidence": "unique-value scan of all 3 acquired label.nii.gz (only ids 0 and 1 occur)",
            },
            "1": {
                "source_name": "coronary artery (LM, LAD, LCx, RCA, diagonals 1-3, OM 1-3, ramus intermedius, PDA, AM 1 and other vessels per AHA 17 segments - all merged)",
                "structure_id": "coronary_arteries",
                "status": "verified",
                "evidence": ("the dataset annotates exactly one structure class (title/subtitle: "
                             "'Coronary Artery Segmentation'); paper arXiv:2211.01607 Fig. 1 "
                             "caption: 'subclasses of coronary arteries are not further "
                             "individually labeled', Sec. annotation: the labeled coronary "
                             "artery includes LM/LAD/LCx/RCA/diagonal 1-3/OM 1-3/ramus "
                             "intermedius/PDA/AM 1 and other vessels per the AHA 17-paragraph "
                             "convention (merged), and the aorta root is excluded (they critique "
                             "other datasets for including it); official benchmark code "
                             "data/Image_loader.py consumes a single label.nii.gz per case for "
                             "the coronary task. Empirically the single foreground class is a "
                             "contrast-filled vessel tree (case 601: HU mean 203 inside vs -302 "
                             "background, bbox 100.9x83.7x115.5 mm, affine-matched grid). The "
                             "release ships no numeric label table; the integer is pinned by "
                             "observation of the single foreground class."),
            },
        },
        "sources": [
            {"url": "https://arxiv.org/abs/2211.01607",
             "note": "ImageCAS paper (CMIG 2023): label definition + merged-class Fig. 1 caption"},
            {"url": "https://www.kaggle.com/datasets/xiaoweixumedicalai/imagecas",
             "note": "dataset release (label volumes; per-case <ID>.img.nii.gz / <ID>.label.nii.gz in split-zip groups)"},
            {"url": "https://github.com/XiaoweiXu/ImageCAS-A-Large-Scale-Dataset-and-Benchmark-for-Coronary-Artery-Segmentation-based-on-CT",
             "note": "official benchmark repo"},
        ],
        "empirical_note": ("unique label ids {0,1} with per-label voxel counts recorded per "
                           "file in validated.unique_labels / validated.per_label_voxels: "
                           "601 {0: 63578517, 1: 122475}, 700 {0: 53904453, 1: 97211}, "
                           "798 {0: 70143509, 1: 111083}"),
    },
    "heartchambers_highres_ml": {
        "note": ("TotalSegmentator -ta heartchambers_highres multilabel (--ml) integer ids; "
                 "empirically verified on this host via `TotalSegmentator -lc "
                 "heartchambers_highres` (TotalSegmentator 2.18.0), which matches the shared "
                 "contract"),
        "engine_verified": True,
        "labels": {
            "1": {"source_name": "heart_myocardium", "structure_id": "heart_myocardium"},
            "2": {"source_name": "heart_atrium_left", "structure_id": "heart_atrium_left"},
            "3": {"source_name": "heart_ventricle_left", "structure_id": "heart_ventricle_left"},
            "4": {"source_name": "heart_atrium_right", "structure_id": "heart_atrium_right"},
            "5": {"source_name": "heart_ventricle_right", "structure_id": "heart_ventricle_right"},
            "6": {"source_name": "aorta", "structure_id": "aorta"},
            "7": {"source_name": "pulmonary_artery", "structure_id": "pulmonary_artery"},
        },
    },
}

BLOCKED = [
    {
        "target": "ImageCAS 14-structure volumes + multilabel segmentations (HF AI-CVM/Cardiac-CT: Train(ImageCAS)/images + Train(ImageCAS)/segmentations, 1000 cases; ExtTest-5(MM-WHS)/segmentations, 20 cases)",
        "reason": ("gated repository: anonymous data-file resolves return HTTP 401 with "
                   "X-Error-Code: GatedRepo, X-Error-Message: 'Access to dataset "
                   "AI-CVM/Cardiac-CT is restricted. You must have access to it and be "
                   "authenticated to access it. Please log in.' Re-tested 2026-09-23 with a "
                   "user-provided HF token (terms accepted): still denied, now HTTP 403 - "
                   "access request pending author review/approval. The dataset card Status "
                   "section additionally states the dataset is 'not publickly available "
                   "and will be released here once the paper is accepted for publishing "
                   "in a peer-reviewed journal (currently under review)'."),
        "user_action": ("dataset terms have been accepted and an HF token provided; now wait "
                        "for the dataset author to approve the access request at "
                        "https://huggingface.co/datasets/AI-CVM/Cardiac-CT (or contact the "
                        "AI-CVM authors), then export HF_TOKEN=hf_... and run tools/hf_fetch.py "
                        "(fetches 2-3 complete cases into data/raw/imagecas/<case>/) and rerun "
                        "tools/build_manifest.py"),
    },
    {
        "target": "ImageCAS pre-extracted .stl meshes (14,280 claimed)",
        "reason": ("no such files exist in the repository: a recursive full-tree listing "
                   "(API ?recursive=true, 2022 files / 2020 .nii.gz) found 0 *.stl and no "
                   "meshes/ directory; the pre-extracted mesh release is not uploaded "
                   "(inventory: tools/hf_tree.json)"),
        "user_action": ("request the mesh release from the dataset authors (AI-CVM, "
                        "https://github.com/AI-in-Cardiovascular-Medicine/CCT-FM) or generate "
                        "meshes downstream from the segmentations (the paper's STL figures "
                        "were produced from the masks)"),
    },
    {
        "target": "'TotalSegmentator CT High-Res' (name used in the brief)",
        "reason": ("informational naming discrepancy: no public dataset exists under that "
                   "name. The acquired secondary data comes from the TotalSegmentator CT "
                   "dataset v2 (Zenodo DOI 10.5281/zenodo.8367088, 1228 scans, CC BY 4.0, "
                   "one 23.6 GB zip, case dirs sXXXX/ with ct.nii.gz + segmentations/), "
                   "which matches the brief's schema and license claim; note its masks use "
                   "the TotalSegmentator 'total' (104-class) label schema of its release "
                   "time, not a heart-chambers schema"),
        "user_action": "confirm with the data provider that TotalSegmentator CT dataset v2 is the intended set",
    },
    {
        "target": "TotalSegmentator licensed tasks heartchambers_highres + coronary_arteries (informational: runnable on this host since 2026-09-23)",
        "reason": ("current truth (supersedes the earlier license blocker): an "
                   "academic license is registered machine-wide on this host, set "
                   "2026-09-23 via `.venv/bin/totalseg_set_license` and stored in "
                   "~/.totalsegmentator/config.json (validated online at registration). "
                   "The license-required tasks are runnable on this host "
                   "(TotalSegmentator 2.18.0): heartchambers_highres (CT, license "
                   "required, 7 classes) and coronary_arteries (CT, license required, 1 "
                   "class). Runs are recorded via tools/segmentation_runs.json (exposed "
                   "as segmentation_runs in this manifest). The task weights remain "
                   "restricted to non-commercial use unless separately licensed (see "
                   "LICENSES.totalseg_heartchambers_highres)."),
        "user_action": ("none needed for non-commercial use (free academic license form: "
                        "https://backend.totalsegmentator.com/license-academic/); for "
                        "commercial use contact jakob.wasserthal@usb.ch to obtain a separate "
                        "commercial license first"),
    },
    {
        "target": "License of acquired base ImageCAS vs the brief's primary set (informational)",
        "reason": ("the brief's primary set (HF 14-structure extension AI-CVM/Cardiac-CT) is "
                   "CC-BY-NC-ND-4.0, but the acquired base ImageCAS release on Kaggle "
                   "(xiaoweixumedicalai/imagecas) states license 'Attribution-NonCommercial "
                   "4.0 International (CC BY-NC 4.0)' (no ND clause) on its dataset page - "
                   "the two are different licenses and only CC BY-NC 4.0 governs the "
                   "kaggle-imagecas rows in this manifest"),
        "user_action": ("confirm with the data provider which license applies to your use; "
                        "for the Kaggle data comply with CC BY-NC 4.0 (attribution, "
                        "non-commercial)"),
    },
]


def resolve_name(stem):
    key = _norm(stem)
    canonical = ALIASES.get(key)
    return (canonical, True) if canonical else (stem, False)


KAGGLE_LABEL_MAP = None  # filled from LABEL_MAP["imagecas_kaggle"] at manifest build


def kaggle_structures(stem, validated):
    """Structure ids for Kaggle ImageCAS masks: label.nii.gz is the multilabel/binary
    coronary mask -> expand via label_map.imagecas_kaggle by the label ids present."""
    if stem == "label" or stem.endswith(".label"):
        lmap = (KAGGLE_LABEL_MAP or {}).get("labels", {})
        present = [lmap[str(v)]["structure_id"]
                   for v in validated.get("unique_labels", []) if str(v) in lmap]
        if present:
            return present, all(str(v) in lmap for v in validated.get("unique_labels", []))
    return [stem], False


def bbox_extent_mm(img, mask):
    idx = np.argwhere(mask)
    if idx.size == 0:
        return None
    lo, hi = idx.min(0), idx.max(0)
    aff = img.affine
    corners = np.array([[x, y, z, 1.0] for x in (lo[0], hi[0] + 1)
                        for y in (lo[1], hi[1] + 1) for z in (lo[2], hi[2] + 1)])
    world = corners @ aff.T
    ext = world[:, :3].max(0) - world[:, :3].min(0)
    return [round(float(v), 2) for v in ext]


def validate_nii(path, is_image):
    img = nib.load(path)
    arr = np.asanyarray(img.dataobj)
    out = {"loaded": True}
    out["shape"] = list(int(v) for v in arr.shape)
    zooms = img.header.get_zooms()[:3]
    out["spacing_mm"] = [round(float(v), 4) for v in zooms]
    if is_image:
        lo, hi = float(arr.min()), float(arr.max())
        out["intensity_range"] = [round(lo, 1), round(hi, 1)]
        aff = img.affine
        corners = np.array([[x, y, z, 1.0] for x in (0, arr.shape[0])
                            for y in (0, arr.shape[1]) for z in (0, arr.shape[2])])
        world = corners @ aff.T
        out["bbox_mm"] = [round(float(v), 2) for v in (world[:, :3].max(0) - world[:, :3].min(0))]
        out["nonzero_voxels"] = int(np.count_nonzero(arr))
        return out, None
    mask = arr > 0
    nz = int(mask.sum())
    out["nonzero_voxels"] = nz
    vals, counts = np.unique(arr[arr > 0], return_counts=True) if nz else ([], [])
    out["unique_labels"] = [int(v) for v in vals]
    out["per_label_voxels"] = {str(int(v)): int(c) for v, c in zip(vals, counts)}
    ext = bbox_extent_mm(img, mask)
    out["bbox_mm"] = ext
    if ext is not None:
        out["bbox_plausible_cardiac_20_400mm"] = bool(20.0 <= max(ext) <= 400.0)
    return out, nz


def collect_rows():
    rows = []
    roots = ["data/raw/imagecas", "data/raw/imagecas_stl",
             "data/raw/totalseg_ct", "data/segmentations"]
    for root in roots:
        if not os.path.isdir(root):
            continue
        for dirpath, _, filenames in os.walk(root):
            for fname in sorted(filenames):
                fpath = os.path.join(dirpath, fname)
                rel = fpath.replace(os.sep, "/")
                case_id = rel.split("/")[3]
                stem = fname[:-7] if fname.endswith(".nii.gz") else os.path.splitext(fname)[0]
                bytes_ = os.path.getsize(fpath)
                if rel.startswith("data/segmentations/"):
                    dataset = "totalseg_heartchambers_highres"
                    case_id = rel.split("/")[2].removesuffix("_heartchambers_highres")
                    source_url = (f"derived from data/raw/totalseg_ct/{case_id}/ct.nii.gz "
                                  f"(origin {ZENODO_RECORD}) via TotalSegmentator -ta heartchambers_highres")
                elif rel.startswith("data/raw/totalseg_ct/"):
                    dataset, source_url = "totalseg_ct", ZENODO
                elif rel.startswith("data/raw/imagecas_stl/"):
                    dataset, source_url = "imagecas_stl", HF_BASE
                elif stem in ("img", "label") or stem.endswith((".img", ".label")):
                    dataset, source_url = "kaggle-imagecas", KAGGLE_URL
                else:
                    dataset, source_url = "imagecas", HF_BASE
                if fname.endswith((".nii", ".nii.gz")):
                    is_image = (stem in ("ct", "img") or stem.endswith("_0000")
                                or stem.endswith(".img"))
                    validated, nz = validate_nii(fpath, is_image)
                    sid, is_can = resolve_name(stem)
                    # ImageCAS convention: <case>.nii.gz is the 14-structure multilabel mask
                    is_multilabel = not is_image and stem == case_id and dataset == "imagecas"
                    if is_multilabel:
                        lmap = LABEL_MAP["imagecas_multilabel"]["labels"]
                        present = [lmap[str(v)]["structure_id"]
                                   for v in validated["unique_labels"] if str(v) in lmap]
                        structures, is_can = present, len(present) == len(validated["unique_labels"])
                    elif not is_image and dataset == "kaggle-imagecas":
                        structures, is_can = kaggle_structures(stem, validated)
                    else:
                        structures = [] if is_image else [sid]
                    row = {
                        "path": rel,
                        "dataset": dataset,
                        "case_id": case_id,
                        "structures": structures,
                        "source_url": source_url if source_url != "derived" else
                        f"derived from data/raw/totalseg_ct/{case_id}/ct.nii.gz (origin {ZENODO_RECORD}) via TotalSegmentator -ta heartchambers_highres",
                        "license": LICENSES[dataset],
                        "bytes": bytes_,
                        "validated": validated,
                    }
                    if not is_image:
                        row["raw_name"] = stem
                        row["canonical"] = is_can
                        row["empty"] = nz == 0
                    rows.append(row)
                elif fname.lower().endswith(".stl"):
                    import trimesh
                    mesh = trimesh.load(fpath, force="mesh")
                    sid, is_can = resolve_name(stem)
                    rows.append({
                        "path": rel,
                        "dataset": dataset,
                        "case_id": case_id,
                        "structures": [sid],
                        "source_url": source_url,
                        "license": LICENSES[dataset],
                        "bytes": bytes_,
                        "raw_name": stem,
                        "canonical": is_can,
                        "validated": {
                            "loaded": True,
                            "triangles": int(mesh.faces.shape[0]),
                            "bbox_mm": [round(float(v), 2) for v in (mesh.bounds[1] - mesh.bounds[0])],
                        },
                    })
    rows.sort(key=lambda r: r["path"])
    return rows


def main():
    global KAGGLE_LABEL_MAP
    KAGGLE_LABEL_MAP = LABEL_MAP.get("imagecas_kaggle")
    t0 = time.time()
    rows = collect_rows()
    manifest = {
        "artifacts": rows,
        "blocked": BLOCKED,
        "label_map": LABEL_MAP,
    }
    runs_path = "tools/segmentation_runs.json"
    if os.path.exists(runs_path):
        manifest["segmentation_runs"] = json.load(open(runs_path))
    os.makedirs("data", exist_ok=True)
    with open("data/manifest.json", "w") as f:
        json.dump(manifest, f, indent=2)
    n_img = sum(1 for r in rows if not r["structures"] and r["dataset"] == "totalseg_ct")
    n_can = sum(1 for r in rows if r.get("canonical"))
    print(f"manifest.json: {len(rows)} artifact rows ({n_can} canonical, {n_img} images), "
          f"{len(manifest.get('segmentation_runs', []))} segmentation runs, {len(BLOCKED)} blocked entries, "
          f"{time.time() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
