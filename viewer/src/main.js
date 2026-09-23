import * as THREE from 'three';
import { createStage } from './scene.js';
import { loadModel, collectStructures, modelURL } from './loader.js';
import { createInteraction } from './interaction.js';
import { createUI } from './ui.js';
import { vrAvailability, enterVR, exitVR } from './xr.js';

const worldScaleVec = new THREE.Vector3();

const overlay = document.getElementById('overlay');
const stage = createStage(document.getElementById('app'));

let xrActive = false;

const interaction = createInteraction({
  renderer: stage.renderer,
  scene: stage.scene,
  modelRoot: stage.modelRoot,
  onToggle: (name, visible) => ui.setVisibility(name, visible),
});

const ui = createUI(overlay, {
  stage,
  onToggleVisible: (name, visible) => interaction.setStructureVisible(name, visible),
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
  xrSupported: false,
  xrActive: false,
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
}

stage.onFrame(() => {
  interaction.update();
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
