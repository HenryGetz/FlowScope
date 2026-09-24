import * as THREE from 'three';
import { PLANE_PRESETS, rasDirectionToGlbLocal } from './coordinates.js';

// Module-level scratch (allocated once; never escapes), same pattern as
// coordinates.js. The GLB->modelRoot-local map is only needed while mapping
// the ROI corners at construction, never on the per-frame path.
const _worldFromGlb = new THREE.Matrix4();
const _localFromGlb = new THREE.Matrix4();

/**
 * Map the GLB-local ROI corners (the coordinates.js frame of `mpr.roiCorners`)
 * into modelRoot-local (the plane's frame):
 * `inverse(modelRoot.matrixWorld) * inverse(glbLocalFromWorld())`. The fit
 * chain between the two frames is static after load, so this map is
 * grab-invariant and the mapped corners are computed once and cached (they
 * also feed MprSlice's quad half-extents/center via setPlaneLocal).
 */
function mapRoiCorners(roiCorners, modelRoot, glbLocalFromWorld) {
  modelRoot.updateWorldMatrix(true, true);
  _localFromGlb
    .copy(modelRoot.matrixWorld)
    .invert()
    .multiply(_worldFromGlb.copy(glbLocalFromWorld()).invert());
  return roiCorners.map((corner) => corner.clone().applyMatrix4(_localFromGlb));
}

/**
 * ClippingSync — the single authoritative slice plane driving BOTH the heart
 * mesh clip and the MPR slice quad (Phase-4.3 synchronization).
 *
 * `plane` is authored in modelRoot-local space (THREE.Plane equation
 * n . x + constant = 0) so it tracks VR grabs 1:1 with the anatomy. Every
 * mutator updates `plane` and funnels through one apply path: the slice quad
 * is repositioned onto the plane (mpr.setPlaneLocal), then update() derives
 * the world plane shared by material.clippingPlanes and the shader's
 * uSlicePlane and refreshes the quad's uWorldToVolume (mpr.syncWorld).
 *
 * Clipping is wired PER-MATERIAL (renderer.localClippingEnabled); this class
 * never touches renderer.clippingPlanes — a global plane would clip the
 * coplanar slice quad itself.
 */
export class ClippingSync {
  /**
   * @param {object} opts
   * @param {import('./mprSlice.js').MprSlice} opts.mpr slice quad; must expose
   *   `roiCorners` (8 GLB-local Vector3 corners of the volume/ROI grid, as
   *   derived from meta via coordinates.js) plus
   *   setPlaneLocal(normalLocal, constantLocal, roiCornersLocal)/syncWorld.
   * @param {Iterable<THREE.Material>} opts.materials heart-materials only
   *   (NOT the slice material).
   * @param {THREE.Object3D} opts.modelRoot grab target the plane is authored in.
   * @param {() => THREE.Matrix4} opts.glbLocalFromWorld refreshed per call.
   */
  constructor({ mpr, materials, modelRoot, glbLocalFromWorld }) {
    this.plane = new THREE.Plane(new THREE.Vector3(0, 0, 1), 0);
    this._mpr = mpr;
    this._modelRoot = modelRoot;
    this._glbLocalFromWorld = glbLocalFromWorld;
    this._materials = [...materials];
    // One shared world-plane instance, mutated in place: materials hold the
    // same array/identity for the lifetime of the wiring, so the assignment
    // (and its program recompile) happens only on the enable transition.
    this._worldPlane = new THREE.Plane();
    this._worldPlanes = [this._worldPlane];
    this._enabled = false;
    for (const material of this._materials) material.clipIntersection = false;
    // The ROI corners in the plane's frame (static after load): bounds for
    // setOffset, center for setPreset, sizing/centering for the MPR quad.
    this._roiCornersLocal = mapRoiCorners(mpr.roiCorners, modelRoot, glbLocalFromWorld);
  }

  /**
   * Slide along the plane normal over the ROI extent measured along that
   * normal: t01 = 0 the near bound, 1 the far bound. Clamped to [0, 1].
   * (Legacy slider semantics superseded: offset no longer disables clipping.)
   */
  setOffset(t01) {
    const t = Math.min(1, Math.max(0, t01));
    let dMin = Infinity;
    let dMax = -Infinity;
    for (const corner of this._roiCornersLocal) {
      const d = this.plane.normal.dot(corner);
      if (d < dMin) dMin = d;
      if (d > dMax) dMax = d;
    }
    // n . x + constant = 0 sweeps n . x = -constant over [dMin, dMax].
    this.plane.constant = -(dMin + (dMax - dMin) * t);
    this._apply();
  }

  /**
   * Anatomical preset ('axial' | 'coronal' | 'sagittal'): the normal is the
   * RAS preset mapped through coordinates.rasDirectionToGlbLocal (directions
   * are frame-free across the uniform fit scale), and the plane re-centres on
   * the ROI center (offset resets to 0.5).
   */
  setPreset(name) {
    const preset = Object.hasOwn(PLANE_PRESETS, name) ? PLANE_PRESETS[name] : null;
    if (!preset) throw new Error(`ClippingSync: unknown plane preset "${name}"`);
    this.plane.normal.copy(rasDirectionToGlbLocal(preset));
    // 0.5 lands on the box mid-projection, which is exactly the ROI center's
    // projection for any normal (the mapped ROI is a parallelepiped).
    this.setOffset(0.5);
  }

  /**
   * VR direct-grab/wand entry: the exact plane through `originLocal` with the
   * unit normal `normalLocal` (both modelRoot-local).
   */
  setPlaneFromPose(originLocal, normalLocal) {
    this.plane.normal.copy(normalLocal).normalize();
    this.plane.constant = -this.plane.normal.dot(originLocal);
    this._apply();
  }

  /**
   * Per frame (and after any set*): world plane refreshed in place, materials
   * wired on the enable transition only, then the slice quad uniforms synced.
   */
  update() {
    const model = this._modelRoot;
    // Whole-subtree refresh keeps glbLocalFromWorld()'s gltf.scene.matrixWorld
    // consistent with modelRoot.matrixWorld for this frame's plane + UVW.
    model.updateWorldMatrix(true, true);
    this._worldPlane.copy(this.plane).applyMatrix4(model.matrixWorld);
    if (!this._enabled) {
      this._enabled = true;
      for (const material of this._materials) {
        material.clippingPlanes = this._worldPlanes;
        material.needsUpdate = true;
      }
    }
    this._mpr.syncWorld(this._glbLocalFromWorld(), this._worldPlane);
  }

  /** Restore the wired materials to their pre-wiring clipping state. */
  dispose() {
    for (const material of this._materials) {
      material.clippingPlanes = null;
      material.needsUpdate = true;
    }
    this._materials.length = 0;
    this._enabled = false;
  }

  /** One apply path (Phase-4.3): quad on the plane, then world sync. */
  _apply() {
    this._mpr.setPlaneLocal(this.plane.normal, this.plane.constant, this._roiCornersLocal);
    this.update();
  }
}
