import * as THREE from 'three';
import { roiBoundsMm, glbLocalFromRasMm, worldToVolume } from './coordinates.js';

const UP_Z = new THREE.Vector3(0, 0, 1);
const AXIS_X = new THREE.Vector3(1, 0, 0);
const AXIS_Y = new THREE.Vector3(0, 1, 0);
const _n = new THREE.Vector3();
const _ex = new THREE.Vector3();
const _ey = new THREE.Vector3();
const _p = new THREE.Vector3();
const _rel = new THREE.Vector3();
const _center = new THREE.Vector3();
const _q = new THREE.Quaternion();

const VERTEX_SHADER = /* glsl */ `
varying vec3 vWorldPosition;

void main() {
  vWorldPosition = (modelMatrix * vec4(position, 1.0)).xyz;
  gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
}
`;

const FRAGMENT_SHADER = /* glsl */ `
uniform sampler3D uVolumeTexture;
uniform mat4 uWorldToVolume;
uniform vec4 uSlicePlane;
uniform float uWindow;
uniform float uLevel;
uniform float uHuMin;
uniform float uHuRange;

varying vec3 vWorldPosition;

out vec4 fragColor;

void main() {
  vec3 uvw = (uWorldToVolume * vec4(vWorldPosition, 1.0)).xyz;
  if (any(lessThan(uvw, vec3(0.0))) || any(greaterThan(uvw, vec3(1.0)))) discard;
  float v = texture(uVolumeTexture, uvw).r;
  float hu = v * uHuRange + uHuMin;
  float intensity = clamp((hu - (uLevel - 0.5 * uWindow)) / max(uWindow, 1e-3), 0.0, 1.0);
  fragColor = vec4(vec3(intensity), 1.0);
}
`;

/**
 * A single quad displaying one oblique CT slice of a 256^3 quantized volume.
 *
 * The quad is authored in GLB-local space (parent it to the model root) and
 * sized from the volume ROI so its cross-section is covered for any plane
 * orientation. The world-space slice plane + volume UVW mapping are refreshed
 * each update through `syncWorld`. One draw call, 2 triangles, exactly one 3D
 * texture fetch per fragment.
 */
export class MprSlice {
  constructor({ texture, meta }) {
    this.meta = meta;

    const win = meta.window ?? { min_hu: -150, max_hu: 450 };

    this.uniforms = {
      uVolumeTexture: { value: texture },
      uWorldToVolume: { value: new THREE.Matrix4() },
      uSlicePlane: { value: new THREE.Vector4(0, 0, 1, 0) },
      uWindow: { value: 600 },
      uLevel: { value: 150 },
      uHuMin: { value: win.min_hu },
      uHuRange: { value: win.max_hu - win.min_hu },
    };

    this.geometry = new THREE.PlaneGeometry(1, 1);
    this.material = new THREE.ShaderMaterial({
      glslVersion: THREE.GLSL3,
      uniforms: this.uniforms,
      vertexShader: VERTEX_SHADER,
      fragmentShader: FRAGMENT_SHADER,
      side: THREE.DoubleSide,
      transparent: false,
      depthWrite: true,
      polygonOffset: true,
      polygonOffsetFactor: -2,
      polygonOffsetUnits: -2,
    });
    this.mesh = new THREE.Mesh(this.geometry, this.material);
    this.mesh.frustumCulled = false;

    // ROI corners in GLB-local space (fixed for a given meta) = roiBoundsMm
    // RAS-mm corners mapped RAS->GLB-local; exposed for ClippingSync bounds.
    // Quad sizing uses the plane-frame set passed to setPlaneLocal, not this one.
    const glbFromRas = glbLocalFromRasMm(meta);
    const bounds = roiBoundsMm(meta);
    this.roiCorners = [];
    for (let i = 0; i < 8; i++) {
      _p.set(
        i & 1 ? bounds.max.x : bounds.min.x,
        i & 2 ? bounds.max.y : bounds.min.y,
        i & 4 ? bounds.max.z : bounds.min.z,
      );
      this.roiCorners.push(_p.clone().applyMatrix4(glbFromRas));
    }
  }

  /**
   * Position + orient the quad on the given plane (n . x + c = 0), in the
   * quad's parent frame (modelRoot-local). `roiCornersLocal` = the 8 ROI
   * corners already mapped into that same frame; they drive the quad center
   * and cross-section sizing (+1% margin).
   */
  setPlaneLocal(normalLocal, constantLocal, roiCornersLocal) {
    const invLen = 1 / Math.max(normalLocal.length(), 1e-12);
    _n.copy(normalLocal).multiplyScalar(invLen);
    const c = constantLocal * invLen;

    _q.setFromUnitVectors(UP_Z, _n);
    _ex.copy(AXIS_X).applyQuaternion(_q);
    _ey.copy(AXIS_Y).applyQuaternion(_q);

    // Quad center: ROI center projected onto the plane.
    _center.set(0, 0, 0);
    for (const corner of roiCornersLocal) _center.add(corner);
    _center.multiplyScalar(1 / roiCornersLocal.length);
    _center.addScaledVector(_n, -(_n.dot(_center) + c));

    // Half-extents covering the ROI cross-section (+1% margin).
    let hx = 0;
    let hy = 0;
    for (const corner of roiCornersLocal) {
      _rel.copy(corner).sub(_center);
      hx = Math.max(hx, Math.abs(_rel.dot(_ex)));
      hy = Math.max(hy, Math.abs(_rel.dot(_ey)));
    }

    this.mesh.position.copy(_center);
    this.mesh.quaternion.copy(_q);
    this.mesh.scale.set(2 * hx * 1.01, 2 * hy * 1.01, 1);
  }

  setWindow(widthHu) {
    this.uniforms.uWindow.value = widthHu;
  }

  setLevel(centerHu) {
    this.uniforms.uLevel.value = centerHu;
  }

  /** Refresh the world->volume UVW mapping and world-space slice plane. */
  syncWorld(glbLocalFromWorld, worldPlane) {
    this.uniforms.uWorldToVolume.value.copy(worldToVolume(this.meta, glbLocalFromWorld));
    this.uniforms.uSlicePlane.value.set(
      worldPlane.normal.x,
      worldPlane.normal.y,
      worldPlane.normal.z,
      worldPlane.constant,
    );
  }

  dispose() {
    this.geometry.dispose();
    this.material.dispose();
  }
}
