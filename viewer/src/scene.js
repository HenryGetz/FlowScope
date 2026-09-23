import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

/** The GLB is fitted to this max dimension (meters), centered at the origin. */
export const MODEL_MAX_SIZE = 0.35;
/** Model height above the floor (m) while an XR session is running. */
export const XR_MODEL_Y = 1.15;

const QUAD_DISTANCE = 0.4;

/**
 * Rendering stage: renderer/scene/camera/lights, OrbitControls (LMB orbit,
 * RMB / shift+LMB pan, wheel zoom), model fitting + auto-framing, rolling
 * 1 s frame metrics, and the head-locked in-XR stats quad.
 */
export function createStage(container) {
  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0x0a0d12);

  const camera = new THREE.PerspectiveCamera(60, 1, 0.01, 100);
  camera.position.set(0.45, 0.32, 0.55);
  scene.add(camera); // the in-XR stats quad rides on the camera (head-locked)

  scene.add(new THREE.HemisphereLight(0xffffff, 0x223344, 1.1));
  const keyLight = new THREE.DirectionalLight(0xffffff, 1.5);
  keyLight.position.set(1.5, 3, 2);
  scene.add(keyLight);
  const fillLight = new THREE.DirectionalLight(0x88aaff, 0.6);
  fillLight.position.set(-2, 1, -1.5);
  scene.add(fillLight);

  const renderer = new THREE.WebGLRenderer({ antialias: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.xr.enabled = true;
  renderer.xr.setFoveation(1);
  container.appendChild(renderer.domElement);

  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = false;

  const modelRoot = new THREE.Group(); // world placement + XR grab target
  const fitGroup = new THREE.Group(); // static fit: center + normalize scale
  modelRoot.add(fitGroup);
  scene.add(modelRoot);

  // ---- head-locked stats quad (visible while presenting) -----------------
  const quadCanvas = document.createElement('canvas');
  quadCanvas.width = 512;
  quadCanvas.height = 256;
  const quadTexture = new THREE.CanvasTexture(quadCanvas);
  quadTexture.colorSpace = THREE.SRGBColorSpace;
  const statsQuad = new THREE.Mesh(
    new THREE.PlaneGeometry(0.12, 0.06),
    new THREE.MeshBasicMaterial({
      map: quadTexture,
      transparent: true,
      depthTest: false,
      depthWrite: false,
      toneMapped: false,
    }),
  );
  // top-left of the view at QUAD_DISTANCE in camera space
  statsQuad.position.set(-0.15, 0.18, -QUAD_DISTANCE);
  statsQuad.renderOrder = 999;
  statsQuad.visible = false;
  camera.add(statsQuad);

  function setStatsText(lines) {
    const ctx = quadCanvas.getContext('2d');
    ctx.clearRect(0, 0, quadCanvas.width, quadCanvas.height);
    ctx.fillStyle = 'rgba(8, 10, 14, 0.78)';
    if (typeof ctx.roundRect === 'function') {
      ctx.beginPath();
      ctx.roundRect(4, 4, quadCanvas.width - 8, quadCanvas.height - 8, 18);
      ctx.fill();
    } else {
      ctx.fillRect(4, 4, quadCanvas.width - 8, quadCanvas.height - 8);
    }
    ctx.fillStyle = '#dde3ee';
    ctx.font = '600 42px ui-monospace, monospace';
    ctx.textBaseline = 'top';
    let y = 22;
    for (const line of lines) {
      ctx.fillText(line, 22, y);
      y += 58;
    }
    quadTexture.needsUpdate = true;
  }

  // ---- model fitting + auto framing --------------------------------------

  /**
   * Deterministic world-space bounds: decoded attribute values (getX/getY/getZ
   * honoring normalization) x node matrixWorld — the exact chain validated by
   * the debug attrMin/attrMax readouts; no cached or object-level bounds.
   */
  function measureBox(root, target = new THREE.Box3()) {
    root.updateWorldMatrix(true, true);
    target.makeEmpty();
    const v = new THREE.Vector3();
    root.traverse((node) => {
      const geometry = node.geometry;
      const attr = geometry && geometry.getAttribute && geometry.getAttribute('position');
      if (!attr || typeof attr.getX !== 'function') return;
      for (let i = 0; i < attr.count; i += 1) {
        v.set(attr.getX(i), attr.getY(i), attr.getZ(i)).applyMatrix4(node.matrixWorld);
        target.expandByPoint(v);
      }
    });
    return target;
  }

  /** Fit an object to MODEL_MAX_SIZE at the origin (inside modelRoot). */
  function setModel(object3d) {
    while (fitGroup.children.length > 0) fitGroup.remove(fitGroup.children[0]);
    object3d.updateWorldMatrix(true, true);
    const box = measureBox(object3d);
    const size = box.getSize(new THREE.Vector3());
    const center = box.getCenter(new THREE.Vector3());
    const maxDim = Math.max(size.x, size.y, size.z);
    // A non-finite maxDim (degenerate/infinite bounds) must never collapse the
    // model via scale=0.35/Inf -> 0 (drawn at zero size: tris counted, no
    // pixels) nor poison the placement with NaN centers.
    const scale =
      Number.isFinite(maxDim) && maxDim > 1e-9 ? MODEL_MAX_SIZE / maxDim : 1;
    fitGroup.scale.setScalar(scale);
    if (Number.isFinite(center.x) && Number.isFinite(center.y) && Number.isFinite(center.z)) {
      fitGroup.position.copy(center).multiplyScalar(-scale);
    } else {
      fitGroup.position.set(0, 0, 0);
    }
    fitGroup.add(object3d);
    frameCamera();
  }

  /** Auto-frame the model bbox with the desktop camera. */
  function frameCamera() {
    modelRoot.updateWorldMatrix(true, true);
    const box = measureBox(modelRoot);
    const sphere = box.isEmpty()
      ? new THREE.Sphere(new THREE.Vector3(), 0.5)
      : box.getBoundingSphere(new THREE.Sphere());
    if (!Number.isFinite(sphere.radius) || sphere.radius <= 0) {
      sphere.center.set(0, 0, 0);
      sphere.radius = 0.5;
    }
    const fovY = THREE.MathUtils.degToRad(camera.fov);
    const fovX = 2 * Math.atan(Math.tan(fovY / 2) * camera.aspect);
    let dist = Math.max(
      sphere.radius / Math.sin(fovY / 2),
      sphere.radius / Math.sin(fovX / 2),
    ) * 1.15;
    if (!Number.isFinite(dist) || dist <= 1e-3) dist = 1;
    const direction = new THREE.Vector3(0.75, 0.5, 1).normalize();
    camera.position.copy(sphere.center).addScaledVector(direction, dist);
    camera.near = Math.max(dist / 100, 0.01);
    camera.far = Math.max(dist * 20, 100);
    camera.updateProjectionMatrix();
    controls.target.copy(sphere.center);
    controls.update();
  }

  // ---- frame loop + rolling 1 s metrics -----------------------------------

  const frameHooks = [];
  const renderHooks = [];
  const samples = [];
  const metrics = { fps: 0, frameMs: 0 };
  let lastTime = performance.now();
  let renderedFrames = 0;

  renderer.setAnimationLoop(() => {
    const now = performance.now();
    const dt = now - lastTime;
    lastTime = now;
    samples.push(now);
    const cutoff = now - 1000;
    while (samples.length > 0 && samples[0] < cutoff) samples.shift();
    if (samples.length > 1) {
      const span = samples[samples.length - 1] - samples[0];
      metrics.fps = ((samples.length - 1) * 1000) / span;
      metrics.frameMs = span / (samples.length - 1);
    } else {
      metrics.fps = dt > 0 ? 1000 / dt : 0;
      metrics.frameMs = dt;
    }
    controls.update();
    for (const hook of frameHooks) hook(dt);
    renderer.render(scene, camera);
    renderedFrames += 1;
    for (const hook of renderHooks) hook();
  });

  function resize() {
    if (renderer.xr.isPresenting) return;
    const width = container.clientWidth || window.innerWidth;
    const height = container.clientHeight || window.innerHeight;
    camera.aspect = width / Math.max(height, 1);
    camera.updateProjectionMatrix();
    renderer.setSize(width, height);
  }
  window.addEventListener('resize', resize);
  resize();

  return {
    renderer,
    scene,
    camera,
    controls,
    modelRoot,
    fitGroup,
    metrics,
    setModel,
    frameCamera,
    measureBox,
    setStatsText,
    onFrame(hook) {
      frameHooks.push(hook);
    },
    onRendered(hook) {
      renderHooks.push(hook);
    },
    get renderedFrames() {
      return renderedFrames;
    },
    triangles() {
      return renderer.info.render.triangles;
    },
    /** Toggle desktop/XR presentation state (model placement + stats quad). */
    setXRActive(active) {
      controls.enabled = !active;
      statsQuad.visible = active;
      modelRoot.position.set(0, active ? XR_MODEL_Y : 0, 0);
      modelRoot.quaternion.identity();
      modelRoot.scale.setScalar(1);
      if (!active) {
        resize();
        frameCamera();
      }
    },
  };
}
