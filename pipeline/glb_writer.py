"""Hand-rolled binary glTF 2.0 (.glb) writer with KHR_mesh_quantization.

Layout per the shared contract: root node ``cardiac``, one named child node per
structure (node AND mesh named with the canonical id / raw name), exactly one
triangle primitive per mesh, 4-byte-aligned buffer views.

Quantized mode: POSITION is componentType 5123 (unsigned short) with
``normalized: true``; decoded f = c/65535 maps the mesh-local unit cube onto the
mesh bbox via node.translation = bbox min and node.scale = bbox extent per axis
(epsilon-guarded to >= 1e-6). NORMAL is componentType 5120 (signed byte),
normalized. INDICES are 5123 when n_verts <= 65535 else 5125. POSITION accessor
min/max hold the decoded min/max. ``extensionsUsed: ["KHR_mesh_quantization"]``.

``quantize=False`` (--no-quantize): float32 (5126) world-space positions and
normals, identity node transform, no extension.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path

import numpy as np

from structures import material_for

_GLB_MAGIC = 0x46546C67
_GLB_VERSION = 2
_CHUNK_JSON = 0x4E4F534A
_CHUNK_BIN = 0x004E4942

_TARGET_VERTEX = 34962
_TARGET_INDEX = 34963

_COMPONENT_BYTE = 5120   # signed int8, normalized
_COMPONENT_USHORT = 5123  # unsigned short
_COMPONENT_UINT = 5125    # unsigned int
_COMPONENT_FLOAT = 5126   # float32

_POS_QUANT = 65535.0
_NRM_QUANT = 127.0
_SCALE_EPS = 1e-6


def write_glb(path, meshes, quantize: bool = True) -> dict:
    """Write one binary .glb; return {'glb_bytes': int, 'quantized': bool}.

    ``meshes``: iterable of dicts with keys
      id      -- structure node/mesh name (canonical id, or raw name if unmapped)
      points  -- (N, 3) float world-space positions in meters
      normals -- (N, 3) float per-vertex unit normals
      indices -- (3 * n_triangles,) int triangle corner indices
    """
    bin_data = bytearray()
    buffer_views: list[dict] = []
    accessors: list[dict] = []
    meshes_json: list[dict] = []
    materials_json: list[dict] = []
    material_index_by_key: dict[tuple, int] = {}
    nodes_json: list[dict] = [{'name': 'cardiac', 'children': []}]

    def add_view(data: bytes, target: int) -> int:
        while len(bin_data) % 4:
            bin_data.append(0)
        buffer_views.append(
            {'buffer': 0, 'byteOffset': len(bin_data), 'byteLength': len(data), 'target': target}
        )
        bin_data.extend(data)
        return len(buffer_views) - 1

    def add_accessor(view: int, component: int, count: int, kind: str,
                     normalized: bool = False, dec_min=None, dec_max=None) -> int:
        accessor = {'bufferView': view, 'componentType': component, 'count': count, 'type': kind}
        if normalized:
            accessor['normalized'] = True
        if dec_min is not None:
            accessor['min'] = dec_min
            accessor['max'] = dec_max
        accessors.append(accessor)
        return len(accessors) - 1

    for item in meshes:
        name = item['id']
        points = np.asarray(item['points'], dtype=np.float64)
        normals = np.asarray(item['normals'], dtype=np.float64)
        indices = np.asarray(item['indices'], dtype=np.int64).reshape(-1)
        n_verts = len(points)

        spec, _known = material_for(name)
        # one shared glTF material per unique (rgba, roughness, metallic,
        # alphaMode) signature -- e.g. LV/LA/LAA share one material index
        mat_key = (spec.rgba, spec.roughness, spec.metallic, spec.alpha_mode)
        material_index = material_index_by_key.get(mat_key)
        if material_index is None:
            material_index = len(materials_json)
            material_index_by_key[mat_key] = material_index
            materials_json.append(
                {
                    'name': name,
                    'pbrMetallicRoughness': {
                        'baseColorFactor': [float(v) for v in spec.rgba],
                        'roughnessFactor': float(spec.roughness),
                        'metallicFactor': float(spec.metallic),
                    },
                    'doubleSided': True,
                    'alphaMode': spec.alpha_mode,
                }
            )

        if quantize:
            bbox_min = points.min(axis=0)
            scale = np.maximum(points.max(axis=0) - bbox_min, _SCALE_EPS)
            unit = np.clip((points - bbox_min) / scale, 0.0, 1.0)
            codes = np.rint(unit * _POS_QUANT).astype(np.uint16)
            pos_bytes = codes.astype('<u2').tobytes()
            pos_component = _COMPONENT_USHORT
            # decoded f = c/65535 ; min/max = decoded min/max (contract)
            dec_min = [float(v) for v in codes.min(axis=0) / _POS_QUANT]
            dec_max = [float(v) for v in codes.max(axis=0) / _POS_QUANT]

            lengths = np.linalg.norm(normals, axis=1, keepdims=True)
            unit_n = normals / np.maximum(lengths, 1e-12)
            nrm_bytes = np.clip(np.rint(unit_n * _NRM_QUANT), -127.0, 127.0).astype('<i1').tobytes()
            nrm_component = _COMPONENT_BYTE
        else:
            pos32 = points.astype('<f4')
            pos_bytes = pos32.tobytes()
            pos_component = _COMPONENT_FLOAT
            dec_min = [float(v) for v in pos32.min(axis=0)]
            dec_max = [float(v) for v in pos32.max(axis=0)]

            nrm_bytes = normals.astype('<f4').tobytes()
            nrm_component = _COMPONENT_FLOAT

        if n_verts <= 65535:
            idx_bytes = indices.astype('<u2').tobytes()
            idx_component = _COMPONENT_USHORT
        else:
            idx_bytes = indices.astype('<u4').tobytes()
            idx_component = _COMPONENT_UINT

        pos_view = add_view(pos_bytes, _TARGET_VERTEX)
        nrm_view = add_view(nrm_bytes, _TARGET_VERTEX)
        idx_view = add_view(idx_bytes, _TARGET_INDEX)

        pos_acc = add_accessor(pos_view, pos_component, n_verts, 'VEC3',
                               normalized=quantize, dec_min=dec_min, dec_max=dec_max)
        nrm_acc = add_accessor(nrm_view, nrm_component, n_verts, 'VEC3', normalized=quantize)
        idx_acc = add_accessor(idx_view, idx_component, len(indices), 'SCALAR')

        meshes_json.append(
            {
                'name': name,
                'primitives': [
                    {
                        'attributes': {'POSITION': pos_acc, 'NORMAL': nrm_acc},
                        'indices': idx_acc,
                        'material': material_index,
                    }
                ],
            }
        )

        node = {'name': name, 'mesh': len(meshes_json) - 1}
        if quantize:
            node['translation'] = [float(v) for v in bbox_min]
            node['scale'] = [float(v) for v in scale]
        nodes_json.append(node)
        nodes_json[0]['children'].append(len(nodes_json) - 1)

    gltf = {
        'asset': {'version': '2.0', 'generator': 'flowscope pipeline'},
        'scene': 0,
        'scenes': [{'nodes': [0]}],
        'nodes': nodes_json,
        'meshes': meshes_json,
        'materials': materials_json,
        'buffers': [{'byteLength': len(bin_data)}],
        'bufferViews': buffer_views,
        'accessors': accessors,
    }
    if quantize:
        gltf['extensionsUsed'] = ['KHR_mesh_quantization']

    json_bytes = json.dumps(gltf, separators=(',', ':'), ensure_ascii=True).encode('utf-8')
    json_chunk = json_bytes + b' ' * ((-len(json_bytes)) % 4)
    bin_chunk = bytes(bin_data) + b'\x00' * ((-len(bin_data)) % 4)
    total = 12 + 8 + len(json_chunk) + 8 + len(bin_chunk)

    blob = (
        struct.pack('<4sII', b'glTF', _GLB_VERSION, total)
        + struct.pack('<II', len(json_chunk), _CHUNK_JSON)
        + json_chunk
        + struct.pack('<II', len(bin_chunk), _CHUNK_BIN)
        + bin_chunk
    )
    out_path = Path(path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(blob)
    return {'glb_bytes': total, 'quantized': quantize}
