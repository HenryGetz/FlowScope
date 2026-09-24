/**
 * coordinates.js — pure coordinate math shared by the MPR slice quad, the mesh
 * clipping sync, and the XR interaction layer. Imports only `three`; no I/O.
 *
 * ## Change of basis: RAS mm -> GLB-local meters
 *
 * The pipeline emits NIfTI-convention data: `affine` maps voxel index
 * (i, j, k, 1) to RAS millimetres (x = Right, y = Anterior, z = Superior).
 * The GLB is authored Y-up: +X right, +Y up, +Z toward the viewer.
 *
 * Matching the pipeline's `_Y_UP_SIGN` / `[:, [0,2,1]]` reordering exactly:
 *
 *     l = 1e-3 * (x, z, -y)          // RAS mm -> GLB-local meters
 *
 * i.e. Right -> +X, Superior -> +Y, Anterior -> -Z (forward), with a uniform
 * mm -> m scale of `MM_TO_M`. The GLB-local origin sits at the RAS point
 * `model_center_ras_mm` (the global mesh bbox center subtracted at export),
 * so the full map is `l = RAS_BASIS * (ras - model_center_ras_mm)`, which
 * equals `RAS_BASIS * ras - c` with `c = 1e-3 * (cx, cz, -cy)` because
 * `RAS_BASIS * model_center_ras_mm === c`.
 *
 * ## Texel-centered UVW
 *
 * `uvwFromRasMm` composes `S(1/dimensions) * T(0.5) * inverse(affine)`: a RAS
 * point is first mapped back to continuous voxel index space by the inverse
 * affine, then shifted by half a voxel and scaled into [0, 1]^3. This is the
 * texel-center convention — a voxel-center RAS point `affine * (i, j, k)` maps
 * to `((i + 0.5)/dx, (j + 0.5)/dy, (k + 0.5)/dz)`, so `texture(sampler3D, uvw)`
 * samples the exact centre of voxel (i, j, k) with no half-texel bias.
 *
 * All matrix-returning functions allocate exactly the returned matrix(s)
 * (plus, for `roiBoundsMm`, its returned vectors); everything else reuses
 * module-level scratch and never touches its inputs.
 */
import * as THREE from 'three'

/** Millimetre -> metre scale (GLB space is meters). */
export const MM_TO_M = 1e-3

/**
 * Linear RAS-mm -> GLB-local-meter basis change: (x, y, z) -> (x, z, -y)*1e-3.
 * Pure linear (no translation): columns are the images of the RAS unit axes
 * R -> +X, A -> -Z, S -> +Y. See the module docstring for the derivation.
 */
export const RAS_BASIS = new THREE.Matrix4().makeBasis(
  new THREE.Vector3(MM_TO_M, 0, 0), // R -> +X
  new THREE.Vector3(0, 0, -MM_TO_M), // A -> -Z
  new THREE.Vector3(0, MM_TO_M, 0), // S -> +Y
)

// -- module-level scratch (allocated once; never escapes) --------------------
const _affine = new THREE.Matrix4()
const _scale = new THREE.Matrix4()
const _shift = new THREE.Matrix4()
const _point = new THREE.Vector3()

/** meta.affine (row-major 4x4 rows) -> THREE.Matrix4. */
function affineMatrix(meta) {
  const a = meta.affine
  return _affine.set(
    a[0][0], a[0][1], a[0][2], a[0][3],
    a[1][0], a[1][1], a[1][2], a[1][3],
    a[2][0], a[2][1], a[2][2], a[2][3],
    a[3][0], a[3][1], a[3][2], a[3][3],
  )
}

/**
 * RAS mm -> GLB-local meters: `RAS_BASIS * (ras - model_center_ras_mm)`.
 */
export function glbLocalFromRasMm(meta) {
  const [cx, cy, cz] = meta.model_center_ras_mm ?? [0, 0, 0]
  const m = RAS_BASIS.clone()
  // translation column = -c, with c = RAS_BASIS * (cx, cy, cz) = (cx, cz, -cy)*1e-3
  m.setPosition(-cx * MM_TO_M, -cz * MM_TO_M, cy * MM_TO_M)
  return m
}

/**
 * GLB-local meters -> RAS mm. Exact numeric inverse of `glbLocalFromRasMm`:
 * `rasMmFromGlbLocal(meta).multiply(glbLocalFromRasMm(meta))` is identity.
 */
export function rasMmFromGlbLocal(meta) {
  return glbLocalFromRasMm(meta).invert()
}

/**
 * RAS mm -> [0,1]^3 texel-centered UVW:
 * `S(1/dimensions) * T(0.5) * inverse(affine)` — voxel index plus half a
 * voxel, normalized per axis (see the module docstring).
 */
export function uvwFromRasMm(meta) {
  const [dx, dy, dz] = meta.dimensions
  const invAffine = affineMatrix(meta).invert() // scratch, read-only below
  _scale.makeScale(1 / dx, 1 / dy, 1 / dz)
  _shift.makeTranslation(0.5, 0.5, 0.5)
  return _scale.clone().multiply(_shift).multiply(invAffine) // one fresh matrix
}

/**
 * GLB-local meters -> [0,1]^3 texel-centered UVW:
 * `uvwFromRasMm(meta) * rasMmFromGlbLocal(meta)`.
 */
export function uvwFromGlbLocal(meta) {
  return uvwFromRasMm(meta).multiply(rasMmFromGlbLocal(meta))
}

/**
 * World space -> [0,1]^3 texel-centered UVW for the 3D texture fetch:
 * `uvwFromGlbLocal(meta) * glbLocalFromWorld` (three.js convention: the
 * rightmost factor applies first).
 */
export function worldToVolume(meta, glbLocalFromWorld) {
  return uvwFromGlbLocal(meta).multiply(glbLocalFromWorld)
}

/**
 * RAS-mm AABB of the voxel grid: corners `affine * (i, j, k)` over
 * `i in {0, dx-1}`, `j in {0, dy-1}`, `k in {0, dz-1}` (the affine need not be
 * axis-aligned, so all 8 corners are transformed and min/maxed).
 */
export function roiBoundsMm(meta) {
  const m = affineMatrix(meta).clone()
  const [dx, dy, dz] = meta.dimensions
  const min = new THREE.Vector3(Infinity, Infinity, Infinity)
  const max = new THREE.Vector3(-Infinity, -Infinity, -Infinity)
  for (const i of [0, dx - 1]) {
    for (const j of [0, dy - 1]) {
      for (const k of [0, dz - 1]) {
        _point.set(i, j, k).applyMatrix4(m)
        min.min(_point)
        max.max(_point)
      }
    }
  }
  return { min, max }
}

/**
 * RAS direction -> GLB-local direction (the `RAS_BASIS` linear map; directions
 * are scale-free). Returns a NEW unit-length Vector3 in GLB-local space, so
 * RAS preset normals pass through safely without aliasing/mutation.
 */
export function rasDirectionToGlbLocal(v) {
  return new THREE.Vector3(v.x * MM_TO_M, v.z * MM_TO_M, -v.y * MM_TO_M).normalize()
}

/**
 * Anatomical slice-plane preset normals in RAS (unit direction the plane
 * normal points along): axial -> Superior, coronal -> Anterior,
 * sagittal -> Right. Map through `rasDirectionToGlbLocal` before use in
 * GLB-local space.
 */
export const PLANE_PRESETS = Object.freeze({
  axial: new THREE.Vector3(0, 0, 1), // +S
  coronal: new THREE.Vector3(0, 1, 0), // +A
  sagittal: new THREE.Vector3(1, 0, 0), // +R
})

/**
 * Phase-2.3 registration guard: the volume proxy (MPR slice quad) must be a
 * direct child of the GLB root group (`modelRoot`) so its plane, authored in
 * modelRoot-local space, tracks VR grabs 1:1 with the anatomy.
 */
export function assertSharedParent(modelRoot, obj) {
  if (obj.parent !== modelRoot) {
    throw new Error('volume proxy must share the GLB parent group')
  }
}
