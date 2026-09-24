import * as THREE from 'three';
import { createStage } from './scene.js';
import { loadModel, collectStructures, modelURL } from './loader.js';
import { createInteraction } from './interaction.js';
import { createUI } from './ui.js';
import { structureGroup, GROUPS } from './structures.js';
import { vrAvailability, enterVR, exitVR } from './xr.js';

const worldScaleVec = new THREE.Vector3();

const overlay = document.getElementById('overlay');
const stage = createStage(document.getElementById('app'));

let xrActive = false;

// ---- cross-section clipping (one global plane for every group) ------------
// The plane keeps the default-camera half space and sweeps the fitted-model
// bbox along the default camera view direction: offset 0 is fully clear of the
// near side (clipping disabled), 1 fully past the far side. The sweep range
// tracks the model through XR grabs and re-framing via the modelRoot-local
// bbox corners captured at load.
stage.renderer.localClippingEnabled = true; // drives material.clippingPlanes

const clipPlane = new THREE.Plane(new THREE.Vector3(0, 0, -1), 0);
const clipMaterials = [];
const clipCorners = [];
let clipOffset01 = 0;
let clipMaterialsEnabled = null; // last applied clippingPlanes state

// ---- chambers default / auto-show rule ------------------------------------
// Chambers are visible at load iff no myocardium structure is visible; turning
// the Myocardium group OFF auto-shows the chambers group and the reverse
// transition auto-hides it again — until the user explicitly toggles a chamber
// structure, after which the auto rule stands down (chambersOverridden).
let chambersOverridden = false;
/** True while main applies defaults/auto visibility (never a user toggle). */
let applyingAuto = false;

const _clipBox = new THREE.Box3();
const _clipInv = new THREE.Matrix4();
const _clipCorner = new THREE.Vector3();

const interaction = createInteraction({
  renderer: stage.renderer,
  scene: stage.scene,
  modelRoot: stage.modelRoot,
  onToggle: (name, visible) => {
    // Trigger picks (and UI rows routed through setStructureVisible) are
    // explicit user toggles and stand the chambers auto rule down; the rule's
    // own auto-show/hide runs inside applyingAuto and never counts.
    if (!applyingAuto && structureGroup(name) === 'chambers') chambersOverridden = true;
    ui.setVisibility(name, visible);
    updateHook();
  },
  onClipNudge: (delta) => setClipOffset(clipOffset01 + delta),
});

const ui = createUI(overlay, {
  stage,
  onToggleVisible: (name, visible) => interaction.setStructureVisible(name, visible),
  onToggleGroup: (group, visible) => toggleGroup(group, visible),
  onClipChange: (offset01) => setClipOffset(offset01),
  onEnterVR: async () => {
    const session = await enterVR(stage.renderer, stage);
    xrActive = true;
    ui.setActive(true);
    updateHook();
    session.addEventListener(
      'end',
      () => {
        xrActive = false;
        ui.setActive(false);
        updateHook();
      },
      { once: true },
    );
  },
  onExitVR: () => exitVR(stage.renderer),
});

// Automation hook: mutated in place, refreshed every second and once at load.
const hook = {
  ready: false,
  fps: 0,
  frameMs: 0,
  triangles: 0,
  structures: [],
  /** group name -> every loaded member structure visible. */
  groups: { myocardium: true, chambers: false, great_vessels: true, coronaries: true, other: false },
  /** Cross-section state (offset01 0 => clipping disabled). */
  clip: { enabled: false, offset01: 0 },
  xrSupported: false,
  xrActive: false,
  /** Set a contract group's visibility (toggles all its member structures). */
  setGroup(name, visible) {
    if (Object.prototype.hasOwnProperty.call(GROUPS, name)) toggleGroup(name, !!visible);
  },
  /** Cross-section offset in 0..1 (0 disables clipping). */
  setClip(offset01) {
    setClipOffset(offset01);
  },
  /** Reframe the desktop camera: 'anterior' | 'lateral' | 'lao'. */
  setView(preset) {
    setCameraView(preset);
  },
  // Per-frame render debug (data-driven verification), mutated in place after
  // every rendered frame.
  debug: {
    camera: { position: [0, 0, 0], fov: 0, near: 0, far: 0 },
    model: {
      bboxMin: [0, 0, 0],
      bboxMax: [0, 0, 0],
      worldScale: [0, 0, 0],
      // decoded position-attribute basis as the loader sees it (written at
      // load; distinguishes empty/raw/mm/m reading of the quantized buffers)
      attrMin: [0, 0, 0],
      attrMax: [0, 0, 0],
      // per-mesh GPU-binding facts at load (working-vs-broken comparison):
      // attribute array types/normalized/interleaving, index type, draw state
      bindings: [],
    },
    viewport: { width: 0, height: 0, pixelRatio: 0 },
    drawCalls: 0,
  },
};
window.__XRVIEWER__ = hook;

let structureNames = [];
let modelLoaded = false;
let loadFrameMark = 0;
let loadedRoot = null;

function updateHook() {
  hook.fps = stage.metrics.fps;
  hook.frameMs = stage.metrics.frameMs;
  hook.triangles = stage.renderer.info.render.triangles;
  hook.structures = structureNames;
  hook.xrActive = xrActive;
  for (const group of Object.keys(hook.groups)) hook.groups[group] = groupVisible(group);
  hook.clip.enabled = clipOffset01 > 0;
  hook.clip.offset01 = clipOffset01;
}

/** All loaded structure names belonging to a contract group. */
function groupNames(group) {
  return structureNames.filter((name) => structureGroup(name) === group);
}

function isStructureVisible(name) {
  const entry = interaction.entries().find((item) => item.name === name);
  return !!entry && entry.object.visible;
}

function groupVisible(group) {
  const names = groupNames(group);
  return names.length > 0 && names.every((name) => isStructureVisible(name));
}

function setGroupVisible(group, visible) {
  for (const name of groupNames(group)) interaction.setStructureVisible(name, visible);
}

/** Chambers auto-show/hide (load rule + Myocardium transitions), never user. */
function autoChambers(show) {
  if (chambersOverridden) return;
  applyingAuto = true;
  try {
    setGroupVisible('chambers', show);
  } finally {
    applyingAuto = false;
  }
}

/** Group toggle (UI row / automation hook): user-explicit; runs the auto rule. */
function toggleGroup(group, visible) {
  if (group === 'chambers') chambersOverridden = true;
  setGroupVisible(group, visible);
  if (group === 'myocardium') autoChambers(!visible);
  updateHook();
}

/** Mandated load defaults, including the chambers default rule. */
function applyLoadDefaults() {
  applyingAuto = true;
  try {
    setGroupVisible('myocardium', true);
    setGroupVisible('great_vessels', true);
    setGroupVisible('coronaries', true);
    setGroupVisible('other', false);
    // Chambers: visible iff no myocardium structure is visible at load.
    const myocardiumVisible = groupNames('myocardium').some((name) => isStructureVisible(name));
    setGroupVisible('chambers', !myocardiumVisible);
  } finally {
    applyingAuto = false;
  }
}

/** Cross-section offset in 0..1 (0 disables clipping entirely). */
function setClipOffset(offset01) {
  clipOffset01 = Math.min(1, Math.max(0, Number(offset01) || 0));
  ui.setClipValue(clipOffset01);
  applyClipMaterials();
  updateClipPlane();
  updateHook();
}

function applyClipMaterials(force = false) {
  const enabled = clipOffset01 > 0;
  if (!force && enabled === clipMaterialsEnabled) return;
  clipMaterialsEnabled = enabled;
  const planes = enabled ? [clipPlane] : [];
  for (const material of clipMaterials) material.clippingPlanes = planes;
}

/** Move the plane to the current offset over the fitted-model bbox extent. */
function updateClipPlane() {
  if (!(clipOffset01 > 0) || clipCorners.length === 0) return;
  const model = stage.modelRoot;
  model.updateWorldMatrix(true, false);
  let dMin = Infinity;
  let dMax = -Infinity;
  for (const corner of clipCorners) {
    _clipCorner.copy(corner).applyMatrix4(model.matrixWorld);
    const d = clipPlane.normal.dot(_clipCorner);
    if (d < dMin) dMin = d;
    if (d > dMax) dMax = d;
  }
  // offset 0 = fully clear of the near side, 1 = fully past the far side
  clipPlane.constant = -(dMin + (dMax - dMin) * clipOffset01);
}

// Mirror of pipeline structures.STRUCTURE_GROUPS great_vessels membership.
const GREAT_VESSEL_IDS = new Set([
  'aorta',
  'pulmonary_artery',
  'pulmonary_veins',
  'vena_cava_superior',
  'vena_cava_inferior',
]);

/** Shared clipping plane: capture the normal, sweep range and every material. */
function initClipping(root) {
  // The normal is the default camera view direction (setModel just framed it).
  clipPlane.normal.copy(stage.controls.target).sub(stage.camera.position).normalize();

  clipMaterials.length = 0;
  // Interpenetrating masks (vessel roots through the heart envelope, coronaries
  // lying on the epicardium) are coincident within a depth unit and z-fight as
  // color flakes. Nudge each tier toward the camera so the anatomically outer
  // surface wins cleanly: coronaries > pulmonary artery > other great vessels
  // > everything else.
  const depthRankOf = (name) => {
    if (name.startsWith('coronary')) return -3;
    // The PA trunk crosses anterior to the aortic arch in these masks and the
    // two interpenetrate; it must win their shared-rank fight explicitly.
    if (name === 'pulmonary_artery') return -2;
    if (GREAT_VESSEL_IDS.has(name)) return -1;
    return 0;
  };
  root.traverse((node) => {
    const material = node.material;
    if (!material) return;
    const rank = depthRankOf(node.name || '');
    const mats = Array.isArray(material) ? material : [material];
    clipMaterials.push(...mats);
    if (rank !== 0) {
      for (const mat of mats) {
        mat.polygonOffset = true;
        // ~millimeter-equivalent steps at this camera's depth precision:
        // raw depth units are sub-micron here and cannot separate the ~1mm
        // interpenetration of adjacent masks.
        mat.polygonOffsetFactor = rank * 8;
        mat.polygonOffsetUnits = rank * 2048;
        mat.needsUpdate = true;
      }
    }
  });

  clipCorners.length = 0;
  stage.measureBox(stage.modelRoot, _clipBox);
  if (!_clipBox.isEmpty()) {
    _clipInv.copy(stage.modelRoot.matrixWorld).invert();
    for (const x of [_clipBox.min.x, _clipBox.max.x]) {
      for (const y of [_clipBox.min.y, _clipBox.max.y]) {
        for (const z of [_clipBox.min.z, _clipBox.max.z]) {
          clipCorners.push(new THREE.Vector3(x, y, z).applyMatrix4(_clipInv));
        }
      }
    }
  }
  applyClipMaterials(true);
  updateClipPlane();
}

/** setView preset -> direction from the fitted model center toward the camera. */
const VIEW_DIRECTIONS = {
  anterior: new THREE.Vector3(0, 0, -1),
  lateral: new THREE.Vector3(-1, 0, 0),
  // LAO 50deg toward patient left from anterior, 20deg cranial elevation
  lao: new THREE.Vector3(
    -Math.sin(THREE.MathUtils.degToRad(50)) * Math.cos(THREE.MathUtils.degToRad(20)),
    Math.sin(THREE.MathUtils.degToRad(20)),
    -Math.cos(THREE.MathUtils.degToRad(50)) * Math.cos(THREE.MathUtils.degToRad(20)),
  ),
};

/** Place the camera on a sphere around the fitted model center, then update. */
function setCameraView(preset) {
  const direction = VIEW_DIRECTIONS[preset];
  if (!direction) return;
  const { camera, controls } = stage;
  const distance = camera.position.distanceTo(controls.target);
  camera.position.copy(controls.target).addScaledVector(direction, distance);
  controls.update();
  updateHook();
}

stage.onFrame((dt) => {
  interaction.update(dt);
  updateClipPlane();
  // ready only after the GLB is loaded AND one frame has rendered since then
  if (modelLoaded && !hook.ready && stage.renderedFrames > loadFrameMark) {
    hook.ready = true;
    updateHook();
  }
});

// Per-frame render debug (post-render so renderer.info reflects this frame).
const debugBox = new THREE.Box3();
stage.onRendered(() => {
  const dbg = hook.debug;
  const cam = stage.camera;
  dbg.camera.position[0] = cam.position.x;
  dbg.camera.position[1] = cam.position.y;
  dbg.camera.position[2] = cam.position.z;
  dbg.camera.fov = cam.fov;
  dbg.camera.near = cam.near;
  dbg.camera.far = cam.far;

  // Explicit decoded-attribute x matrixWorld measurement (the verified chain)
  // instead of Box3.setFromObject internals.
  stage.measureBox(stage.modelRoot, debugBox);
  if (debugBox.isEmpty()) {
    debugBox.min.set(0, 0, 0);
    debugBox.max.set(0, 0, 0);
  }
  dbg.model.bboxMin[0] = debugBox.min.x;
  dbg.model.bboxMin[1] = debugBox.min.y;
  dbg.model.bboxMin[2] = debugBox.min.z;
  dbg.model.bboxMax[0] = debugBox.max.x;
  dbg.model.bboxMax[1] = debugBox.max.y;
  dbg.model.bboxMax[2] = debugBox.max.z;
  // Effective world scale of the LOADED GLB root (fitGroup x modelRoot x root)
  // — not modelRoot alone, whose scale is the user-manipulation scale (1).
  (loadedRoot || stage.modelRoot).getWorldScale(worldScaleVec);
  dbg.model.worldScale[0] = worldScaleVec.x;
  dbg.model.worldScale[1] = worldScaleVec.y;
  dbg.model.worldScale[2] = worldScaleVec.z;

  dbg.viewport.width = stage.renderer.domElement.width;
  dbg.viewport.height = stage.renderer.domElement.height;
  dbg.viewport.pixelRatio = stage.renderer.getPixelRatio();
  dbg.drawCalls = stage.renderer.info.render.calls;
});

/** Record the decoded position-attribute basis actually seen by the loader. */
function scanAttributeBasis(root) {
  const min = [Infinity, Infinity, Infinity];
  const max = [-Infinity, -Infinity, -Infinity];
  let count = 0;
  root.traverse((node) => {
    const geometry = node.geometry;
    const attr = geometry && geometry.getAttribute && geometry.getAttribute('position');
    if (!attr || !attr.getX) return;
    for (let i = 0; i < attr.count; i += 1) {
      const v = [attr.getX(i), attr.getY(i), attr.getZ(i)];
      for (let k = 0; k < 3; k += 1) {
        if (v[k] < min[k]) min[k] = v[k];
        if (v[k] > max[k]) max[k] = v[k];
      }
    }
    count += attr.count;
  });
  if (count > 0) {
    for (let k = 0; k < 3; k += 1) {
      hook.debug.model.attrMin[k] = min[k];
      hook.debug.model.attrMax[k] = max[k];
    }
  }
}

/** Snapshot of the GL-binding-relevant state per mesh (for A/B comparison). */
function scanBindings(root) {
  const desc = (attr) =>
    attr
      ? {
          type: attr.isInterleavedBufferAttribute
            ? attr.data.array.constructor.name
            : attr.array.constructor.name,
          itemSize: attr.itemSize,
          normalized: !!attr.normalized,
          count: attr.count,
          interleaved: !!attr.isInterleavedBufferAttribute,
        }
      : null;
  const list = [];
  root.traverse((node) => {
    if (!node.isMesh) return;
    const geometry = node.geometry;
    const material = node.material;
    list.push({
      name: node.name,
      attrNormalizesAfterDecode: {
        position: !!geometry.getAttribute('position')?.normalized,
        normal: !!geometry.getAttribute('normal')?.normalized,
      },
      nodeScale: [node.scale.x, node.scale.y, node.scale.z],
      visible: node.visible,
      frustumCulled: node.frustumCulled,
      layers: node.layers.mask,
      renderOrder: node.renderOrder,
      groups: geometry.groups.length,
      position: desc(geometry.getAttribute('position')),
      normal: desc(geometry.getAttribute('normal')),
      index: geometry.index
        ? { type: geometry.index.array.constructor.name, count: geometry.index.count }
        : null,
      material: {
        type: material.type,
        visible: material.visible,
        side: material.side,
        transparent: material.transparent,
        alphaTest: material.alphaTest,
        depthTest: material.depthTest,
        depthWrite: material.depthWrite,
        blending: material.blending,
      },
    });
  });
  return list;
}

updateHook();
setInterval(updateHook, 1000);

vrAvailability().then(({ supported, reason }) => {
  hook.xrSupported = supported;
  ui.setVRState({ supported, reason });
  updateHook();
});

loadModel(modelURL())
  .then((gltf) => {
    stage.setModel(gltf.scene);
    const structures = collectStructures(gltf.scene);
    loadedRoot = gltf.scene;
    scanAttributeBasis(gltf.scene);
    hook.debug.model.bindings = scanBindings(gltf.scene);
    interaction.setStructures(structures);
    ui.setStructures(structures);
    structureNames = structures.map((item) => item.name);
    hook.structures = structureNames;
    initClipping(gltf.scene);
    applyLoadDefaults();
    loadFrameMark = stage.renderedFrames;
    modelLoaded = true;
    ui.clearBoot();
    updateHook();
  })
  .catch((err) => {
    console.error(err);
    ui.clearBoot();
    ui.showError(
      `Could not load the cardiac model "${modelURL()}".\n` +
        'Place the GLB at viewer/public/assets/cardiac.glb or open with ?model=<url>.\n' +
        `(${err.message})`,
    );
  });
