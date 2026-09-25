import * as THREE from 'three';
import { createStage } from './scene.js';
import { loadModel, loadVolume, collectStructures, modelURL, assetsBaseURL, payloadURL } from './loader.js';
import {
  createCfdPlayback,
  initialCaseId,
  initialProfile,
  loadClinical,
  clinicalOverride,
  createClinicalModel,
  cathCardLines,
  drawCathCard,
  drawPullback,
} from '../js/cfd_playback.js';
import { createInteraction } from './interaction.js';
import { createUI } from './ui.js';
import { structureGroup, GROUPS } from './structures.js';
import { sessionMode, xrAvailability, enterXR, exitXR } from './xr.js';
import { assertSharedParent } from './coordinates.js';
import { MprSlice } from './mprSlice.js';
import { ClippingSync } from './clippingSync.js';
import { createTelemetry } from './telemetry.js';

const worldScaleVec = new THREE.Vector3();

const overlay = document.getElementById('overlay');
const stage = createStage(document.getElementById('app'));

/** Immersive mode configured by the loading link (`?xr=vr|ar`; default AR). */
const xrMode = sessionMode();

/** True while the immersive session presents. */
let xrRunning = false;

// ---- CFD contrast playback (contract C5 payload) --------------------------
// Created after the GLB decode (it needs the glTF to map payload meshes), its
// shader patch runs inside initClipping's traverse and its transport is driven
// from stage.onFrame. No payload -> inert state + notice, anatomy untouched.
let playback = null;
let playState = null;

// ---- MPR slicing + hardware mesh clipping --------------------------------
// One authoritative slice plane (ClippingSync, modelRoot-local) drives both
// the MPR slice quad and the per-material clippingPlanes of the heart meshes.
// The Cross-section offset positions the plane over the ROI extent along its
// normal (0 = near bound, 1 = far bound) and never disables clipping; without
// a volume pair nothing is wired and the viewer runs without MPR.
stage.renderer.localClippingEnabled = true; // drives material.clippingPlanes
stage.renderer.xr.setFoveation(1.0);

const clipMaterials = [];
let mpr = null; // MprSlice once the <stem>_volume.bin/<stem>_meta.json pair loaded
let clippingSync = null; // owns the authoritative slice plane (modelRoot-local)
let interaction = null; // built with the loaded scene (needs the MPR mesh handle)
let clipOffset01 = 0.5;
let windowHu = 600;
let levelHu = 150;
let presetName = null;
let planeGrabActive = false;

const telemetry = createTelemetry({ renderer: stage.renderer, stage });

// ---- chambers default / auto-show rule ------------------------------------
// Chambers are visible at load iff no myocardium structure is visible; turning
// the Myocardium group OFF auto-shows the chambers group and the reverse
// transition auto-hides it again — until the user explicitly toggles a chamber
// structure, after which the auto rule stands down (chambersOverridden).
let chambersOverridden = false;
/** True while main applies defaults/auto visibility (never a user toggle). */
let applyingAuto = false;

const _dragPlane = new THREE.Plane();
const _dragMat = new THREE.Matrix4();
const _wandInv = new THREE.Matrix4();
const _wandNormalMat = new THREE.Matrix3();
const _wandOrigin = new THREE.Vector3();
const _wandNormal = new THREE.Vector3();

/**
 * Build the XR interaction rig once the scene is loaded — the MPR quad handle
 * exists only after the volume pair loads, and squeeze plane-grab arbitration
 * needs it at construction (null = no MPR: plain model grab + wand only).
 */
function setupInteraction(mprMesh) {
  interaction = createInteraction({
    renderer: stage.renderer,
    scene: stage.scene,
    modelRoot: stage.modelRoot,
    camera: stage.camera,
    mprMesh,
    onToggle: (name, visible) => {
      // Trigger picks (and UI rows routed through setStructureVisible) are
      // explicit user toggles and stand the chambers auto rule down; the rule's
      // own auto-show/hide runs inside applyingAuto and never counts.
      if (!applyingAuto && structureGroup(name) === 'chambers') chambersOverridden = true;
      ui.setVisibility(name, visible);
      updateHook();
    },
    // Contract D Navvus probe: consume the click/trigger hit only when clinical
    // data is loaded and the hit lands on a coronaries structure (the virtual
    // catheter gesture); every other hit keeps the existing pick/toggle path.
    onProbe: (hit) => {
      if (!clinicalModel || structureGroup(hit.name) !== 'coronaries') return false;
      placeProbeWorld(hit.point);
      return true;
    },
    onClipNudge: (delta) => setClipOffset(clipOffset01 + delta),
    // XR scrub modifier: only fired while a grip holds the model (interaction.js)
    onScrub: (delta) => {
      if (playback && playState) playback.scrub(playState.time01 + delta);
    },
    onPlayPause: () => togglePlaying(),
    onPlaneGrabStart: () => {
      planeGrabActive = true;
    },
    onPlaneGrabEnd: () => {
      planeGrabActive = false;
    },
    onPlaneDrag: (deltaWorld) => {
      if (!clippingSync || !planeGrabActive) return;
      // Follow the hand's world delta exactly: lift the modelRoot-local plane
      // through modelRoot.matrixWorld, apply the world delta, map back.
      const plane = clippingSync.plane;
      const modelMatrix = stage.modelRoot.matrixWorld;
      _dragPlane.copy(plane).applyMatrix4(modelMatrix);
      _dragPlane.applyMatrix4(deltaWorld);
      _dragPlane.applyMatrix4(_dragMat.copy(modelMatrix).invert());
      plane.copy(_dragPlane);
    },
    onWandPose: (originWorld, normalWorld, active) => {
      if (!clippingSync || !active) return; // release keeps the plane in place
      _wandInv.copy(stage.modelRoot.matrixWorld).invert();
      _wandOrigin.copy(originWorld).applyMatrix4(_wandInv);
      _wandNormal
        .copy(normalWorld)
        .applyMatrix3(_wandNormalMat.getNormalMatrix(_wandInv))
        .normalize();
      clippingSync.setPlaneFromPose(_wandOrigin, _wandNormal);
    },
  });
}

const ui = createUI(overlay, {
  stage,
  telemetry,
  onToggleVisible: (name, visible) => interaction.setStructureVisible(name, visible),
  onToggleGroup: (group, visible) => toggleGroup(group, visible),
  onClipChange: (offset01) => setClipOffset(offset01),
  onMode: (mode) => setViewMode(mode),
  onPlayToggle: () => togglePlaying(),
  onPlayLoop: (on) => {
    if (playback) playback.setLoop(!!on);
  },
  onPlaySpeed: (x) => {
    if (playback) playback.setSpeed(x);
  },
  onPlayProfile: (p) => {
    if (playback) {
      playback.setProfile(p).catch((err) => {
        console.error(err);
        ui.showError(
          `Could not load the CFD contrast profile "${p}".\n` +
            `The viewer remains fully usable without it. (${err.message})`,
        );
        ui.setPlayback(playState); // re-render the unchanged selection
      });
    }
  },
  onPlayScrub: (t01) => {
    if (playback) playback.scrub(t01);
  },
  onWindowChange: (widthHu) => applyWindow(widthHu),
  onLevelChange: (centerHu) => applyLevel(centerHu),
  onPreset: (name) => applyPreset(name),
  mode: xrMode,
  onEnter: async () => {
    const session = await enterXR(stage.renderer, stage, xrMode);
    xrRunning = true;
    ui.setActive(true);
    syncProbeVisibility();
    updateHook();
    session.addEventListener(
      'end',
      () => {
        xrRunning = false;
        ui.setActive(false);
        syncProbeVisibility();
        updateHook();
      },
      { once: true },
    );
  },
  onExit: () => exitXR(stage.renderer),
});

// ---- Navvus probe widget + pullback console (contract D) ------------------
// Probe state (null until placed) plus the one pullback canvas shared by the
// desktop DOM console (data-fs="pullback") and the in-XR texture plane.
// A missing clinical payload keeps all of this inert.
let clinicalModel = null; // createClinicalModel queries (null until loaded)
let probe = null; // last probeAt() descriptor (null until placed)
let probeReadout = null; // last readoutFor() result (hook.clinical.readout)
let probePullback = null; // selected pullbacks[] entry (auto or selectPath)
let viewMode = 'A'; // 'A' contrast playback | 'B' vFFR ischemia map

// Probe widget: a GLB-frame anchor (rides model grabs) plus a scene-space
// billboard group (small marker, Navvus diagnostic card, pullback graph).
// Geometry/textures are created once here; the canvases redraw only on probe
// or selection changes and the per-frame hook only copies preallocated
// vectors — zero render-loop allocation.
const probeAnchor = new THREE.Object3D();
const probeGroup = new THREE.Group();
const probeMarker = new THREE.Mesh(
  new THREE.SphereGeometry(0.004, 16, 12),
  new THREE.MeshBasicMaterial({ color: 0x67e0c2 }),
);
const cardCanvas = document.createElement('canvas');
cardCanvas.width = 512;
cardCanvas.height = 320;
const cardCtx = cardCanvas.getContext('2d');
const cardTexture = new THREE.CanvasTexture(cardCanvas);
cardTexture.colorSpace = THREE.SRGBColorSpace;
const cardPlane = new THREE.Mesh(
  new THREE.PlaneGeometry(0.12, 0.075),
  new THREE.MeshBasicMaterial({
    map: cardTexture,
    transparent: true,
    depthTest: false,
    depthWrite: false,
    toneMapped: false,
  }),
);
const pullbackCanvas = ui.pullbackCanvas;
const pullbackCtx = pullbackCanvas.getContext('2d');
const pullbackTexture = new THREE.CanvasTexture(pullbackCanvas);
pullbackTexture.colorSpace = THREE.SRGBColorSpace;
const pullbackPlane = new THREE.Mesh(
  new THREE.PlaneGeometry(0.16, 0.09),
  new THREE.MeshBasicMaterial({
    map: pullbackTexture,
    transparent: true,
    depthTest: false,
    depthWrite: false,
    toneMapped: false,
  }),
);
cardPlane.position.set(0, 0.075, 0);
pullbackPlane.position.set(0, -0.075, 0);
cardPlane.renderOrder = 901;
pullbackPlane.renderOrder = 902;
probeGroup.add(probeMarker, cardPlane, pullbackPlane);
probeGroup.visible = false;
stage.scene.add(probeGroup);

// onFrame scratch (preallocated: the render loop never allocates).
const probeWorldPos = new THREE.Vector3();
const probeCamPos = new THREE.Vector3();
const probeRas = new THREE.Vector3();

// Automation hook: mutated in place, refreshed every second and once at load.
const hook = {
  ready: false,
  fps: 0,
  frameMs: 0,
  triangles: 0,
  structures: [],
  /** group name -> every loaded member structure visible. */
  groups: { myocardium: true, chambers: false, great_vessels: true, coronaries: true, other: false },
  /** Cross-section state (enabled = MPR active; offset01 = plane position 0..1). */
  clip: { enabled: false, offset01: 0.5 },
  /** MPR slice state (set* are no-ops until a volume pair is loaded). */
  mpr: {
    ready: false,
    window: 600,
    level: 150,
    preset: null,
    offset01: 0.5,
    setOffset(t) {
      if (mpr) setClipOffset(t);
    },
    setWindow(w) {
      if (mpr) applyWindow(w);
    },
    setLevel(l) {
      if (mpr) applyLevel(l);
    },
    setPreset(name) {
      if (mpr) applyPreset(name);
    },
  },
  /** Frame/GPU telemetry (refreshed in updateHook). */
  telemetry: { fps: 0, frameMs: 0, gpuMs: null, triangles: 0, drawCalls: 0 },
  /** Contrast playback state (C5 payload; available false when absent). */
  playback: {
    playing: false,
    loop: true,
    time01: 0,
    speed: 1,
    profile: 'A',
    transitMs: null,
  },
  /** Contract D clinical state (read-only; readout() returns the card fields). */
  clinical: {
    ready: false,
    mode: 'A',
    probe: null,
    path_id: null,
    readout() {
      return probeReadout;
    },
  },
  /** Boot probe of the configured mode only (`?xr=`); the other stays false. */
  xrSupported: false,
  arSupported: false,
  xrActive: false,
  /** Running session mode (configured mode while presenting, else null). */
  xrMode: null,
  /** Set a contract group's visibility (toggles all its member structures). */
  setGroup(name, visible) {
    if (Object.prototype.hasOwnProperty.call(GROUPS, name)) toggleGroup(name, !!visible);
  },
  /** Cross-section offset in 0..1 (plane position over the ROI extent). */
  setClip(offset01) {
    setClipOffset(offset01);
  },
  /** Reframe the desktop camera: 'anterior' | 'lateral' | 'lao'. */
  setView(preset) {
    setCameraView(preset);
  },
  /** Contrast playback: start/pause the timeline. */
  setPlaying(flag) {
    if (flag) playback?.play();
    else playback?.pause();
  },
  /** Contrast playback: speed preset (0.25 | 0.5 | 1 | 2). */
  setSpeed(x) {
    playback?.setSpeed(x);
  },
  /** Contrast playback: injection profile 'A' | 'B' | 'C' (async switch). */
  setProfile(p) {
    return playback ? playback.setProfile(p) : Promise.resolve();
  },
  /** Contrast playback: scrub the timeline to `t01` in 0..1. */
  scrub(t01) {
    playback?.scrub(t01);
  },
  /** Contract D visualization mode: 'A' (contrast) | 'B' (vFFR map). */
  setMode(mode) {
    setViewMode(mode);
  },
  /**
   * Place the Navvus probe at a GLB model-frame point (contract D *_xyz_ras
   * meters — the centerline_ras frame). Returns the card readout or null.
   */
  placeProbe(x, y, z) {
    return placeProbeRas(x, y, z);
  },
  /** Select a pullback path by id (overrides the longest-match selection). */
  selectPath(path_id) {
    return selectPullbackPath(path_id);
  },
  /** Selected `pullbacks[]` entry (contract D), or null before selection. */
  getPullback() {
    return probePullback;
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
  hook.xrActive = xrRunning;
  hook.xrMode = xrRunning ? xrMode : null;
  for (const group of Object.keys(hook.groups)) hook.groups[group] = groupVisible(group);
  hook.clip.enabled = !!mpr;
  hook.clip.offset01 = clipOffset01;
  hook.mpr.ready = !!mpr;
  hook.mpr.window = windowHu;
  hook.mpr.level = levelHu;
  hook.mpr.preset = presetName;
  hook.mpr.offset01 = clipOffset01;
  const frame = telemetry.sample();
  hook.telemetry.fps = frame.fps;
  hook.telemetry.frameMs = frame.frameMs;
  hook.telemetry.gpuMs = frame.gpuMs;
  hook.telemetry.triangles = frame.triangles;
  hook.telemetry.drawCalls = frame.drawCalls;
  hook.clinical.ready = !!clinicalModel;
  hook.clinical.mode = viewMode;
}

/** Playback readout -> Contrast HUD + automation hook (every state change). */
function handlePlayReadout(state) {
  playState = state;
  const pb = hook.playback;
  pb.playing = state.playing;
  pb.loop = state.loop;
  pb.time01 = state.time01;
  pb.speed = state.speed;
  pb.profile = state.profile;
  pb.transitMs = state.transitMs;
  ui.setPlayback(state);
}

/** Play/pause flip for the HUD button, XR double-tap and the automation hook. */
function togglePlaying() {
  if (!playback) return;
  if (playState && playState.playing) playback.pause();
  else playback.play();
}

// ---- Navvus probe + pullback console (contract D) ------------------------

/** Visualization mode (contract D): 'A' contrast playback | 'B' vFFR map. */
function setViewMode(mode) {
  viewMode = mode === 'B' ? 'B' : 'A';
  if (playback) playback.setMode(viewMode);
  ui.setMode(viewMode);
  updateHook();
}

/** Redraw the Navvus card + DOM mirror for the current probe/selection. */
function refreshProbeCard() {
  probeReadout = probe && clinicalModel ? clinicalModel.readoutFor(probe, probePullback) : null;
  const lines = cathCardLines(probeReadout);
  drawCathCard(cardCtx, lines, cardCanvas.width, cardCanvas.height);
  cardTexture.needsUpdate = true;
  ui.setReadout(probeReadout, lines);
  hook.clinical.probe = probe
    ? { position: probe.position, branch_id: probe.branch_id, s_mm: probe.s_mm }
    : null;
}

/** Redraw the pullback graph (only ever called on a selection change). */
function refreshPullback() {
  if (probePullback) {
    drawPullback(pullbackCtx, probePullback, pullbackCanvas.width, pullbackCanvas.height);
    pullbackTexture.needsUpdate = true;
  }
  ui.setPullbackVisible(!!probePullback);
  syncProbeVisibility();
}

/** Probe widget visibility (mode-independent; the graph plane is XR-only). */
function syncProbeVisibility() {
  const placed = !!probe;
  probeGroup.visible = placed;
  cardPlane.visible = placed;
  pullbackPlane.visible = placed && !!probePullback && xrRunning;
}

/** Place the probe at a world-space surface hit (raycast pick result). */
function placeProbeWorld(point) {
  if (!clinicalModel || !loadedRoot) return null;
  loadedRoot.updateWorldMatrix(true, false);
  probeRas.copy(point);
  loadedRoot.worldToLocal(probeRas);
  return placeProbeRas(probeRas.x, probeRas.y, probeRas.z);
}

/**
 * Place the probe at a GLB model-frame point (contract D *_xyz_ras meters):
 * snaps to the nearest centerline sample, selects the longest-match pullback
 * and redraws the card/graph only where the state actually changed.
 */
function placeProbeRas(x, y, z) {
  if (!clinicalModel || !loadedRoot) return null;
  const placed = clinicalModel.probeAt(x, y, z);
  if (!placed) return null;
  probe = placed;
  probeAnchor.position.set(placed.position[0], placed.position[1], placed.position[2]);
  const auto = clinicalModel.pullbackFor(placed);
  if (auto !== probePullback) {
    probePullback = auto;
    hook.clinical.path_id = auto ? auto.path_id : null;
    refreshPullback();
  }
  refreshProbeCard();
  syncProbeVisibility();
  updateHook();
  return probeReadout;
}

/** Select a pullback path by id (automation override of the longest match). */
function selectPullbackPath(pathId) {
  if (!clinicalModel) return false;
  const entry = clinicalModel.pullbackById(pathId);
  if (!entry) return false;
  if (entry !== probePullback) {
    probePullback = entry;
    hook.clinical.path_id = entry.path_id;
    refreshPullback();
    refreshProbeCard(); // Pa/Pd/vFFR of the card follow the selected path
    updateHook();
  }
  return true;
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

/**
 * Cross-section offset in 0..1: plane position over the ROI extent along the
 * plane normal (0 = near bound, 1 = far bound). Drives ClippingSync when a
 * volume pair is loaded and nothing otherwise.
 */
function setClipOffset(offset01) {
  clipOffset01 = Math.min(1, Math.max(0, Number(offset01) || 0));
  ui.setClipValue(clipOffset01);
  if (clippingSync) clippingSync.setOffset(clipOffset01);
  updateHook();
}

/** MPR window width in HU (full width of the displayed grayscale ramp). */
function applyWindow(widthHu) {
  windowHu = Number(widthHu);
  if (mpr) {
    mpr.setWindow(windowHu);
    ui.setWindowLevel(windowHu, levelHu);
  }
  updateHook();
}

/** MPR window level in HU (center of the displayed grayscale ramp). */
function applyLevel(centerHu) {
  levelHu = Number(centerHu);
  if (mpr) {
    mpr.setLevel(levelHu);
    ui.setWindowLevel(windowHu, levelHu);
  }
  updateHook();
}

/** MPR plane preset ('axial' | 'coronal' | 'sagittal'), re-centred on the ROI. */
function applyPreset(name) {
  presetName = name || null;
  if (clippingSync && presetName) clippingSync.setPreset(presetName);
  if (presetName) ui.setPresetActive(presetName);
  updateHook();
}

// Mirror of pipeline structures.STRUCTURE_GROUPS great_vessels membership.
const GREAT_VESSEL_IDS = new Set([
  'aorta',
  'pulmonary_artery',
  'pulmonary_veins',
  'vena_cava_superior',
  'vena_cava_inferior',
]);

/** Collect every heart material (the ClippingSync wiring targets) + depth ladder. */
function initClipping(root) {
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
    // Playback shader patch (contrast TF + aC0/aC1/uMix/uContrastOn) lands in
    // this same post-decode traverse; the materials stay on clipMaterials so
    // the shared clipping plane keeps cutting them.
    if (playback && node.isMesh) playback.applyShader(node);
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
  if (interaction) interaction.update(dt);
  if (clippingSync) clippingSync.update();
  telemetry.tick(dt);
  if (playback) playback.update(dt); // dt in ms (stage.onFrame convention)
  // Navvus probe widget: follow the GLB-frame anchor and billboard toward the
  // camera — preallocated vectors only, zero per-frame allocation.
  if (probeGroup.visible) {
    probeAnchor.getWorldPosition(probeWorldPos);
    probeGroup.position.copy(probeWorldPos);
    stage.camera.getWorldPosition(probeCamPos);
    probeGroup.lookAt(probeCamPos);
  }
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

xrAvailability(xrMode).then(({ supported, reason }) => {
  hook[xrMode === 'immersive-ar' ? 'arSupported' : 'xrSupported'] = supported;
  ui.setSessionState({ supported, reason });
  updateHook();
});

Promise.all([loadModel(modelURL()), loadVolume(modelURL())])
  .then(([gltf, volume]) => {
    stage.setModel(gltf.scene);
    const structures = collectStructures(gltf.scene);
    loadedRoot = gltf.scene;
    loadedRoot.add(probeAnchor); // the probe rides the GLB model frame
    scanAttributeBasis(gltf.scene);
    hook.debug.model.bindings = scanBindings(gltf.scene);
    initClipping(gltf.scene);

    if (volume) {
      const { texture, meta } = volume;
      mpr = new MprSlice({ texture, meta });
      stage.modelRoot.add(mpr.mesh);
      assertSharedParent(stage.modelRoot, mpr.mesh);
    }

    setupInteraction(mpr ? mpr.mesh : null);
    interaction.setStructures(structures);
    ui.setStructures(structures);
    structureNames = structures.map((item) => item.name);
    hook.structures = structureNames;
    // Playback exists before initClipping so its shader patch joins the same
    // traverse (and therefore the clipMaterials list).
    playback = createCfdPlayback({
      scene: stage.scene,
      stage,
      gltf,
      assetsBase: assetsBaseURL(),
      onReadout: handlePlayReadout,
    });
    initClipping(gltf.scene);

    if (mpr) {
      clippingSync = new ClippingSync({
        mpr,
        materials: clipMaterials,
        modelRoot: stage.modelRoot,
        glbLocalFromWorld: () => loadedRoot.matrixWorld.clone().invert(),
      });
      setClipOffset(0.5);
    }
    ui.setVolumeReady(!!mpr);
    applyLoadDefaults();
    loadFrameMark = stage.renderedFrames;
    modelLoaded = true;
    ui.clearBoot();
    updateHook();
    loadContrastPayload();
    loadClinicalPayload();
  })
  .catch((err) => {
    console.error(err);
    setupInteraction(null);
    ui.clearBoot();
    ui.showError(
      `Could not load the cardiac model "${modelURL()}".\n` +
        'Place the GLB at viewer/models/case_01.glb or open with ?model=<url>.\n' +
        `(${err.message})`,
    );
  });

/**
 * Kick off the C5 contrast payload load. A missing/unfetchable payload
 * degrades to a notice only: no playback, but groups, picking and clipping
 * keep working on the untouched anatomy.
 */
function loadContrastPayload() {
  const caseId = initialCaseId();
  const profile = initialProfile();
  const override = payloadURL();
  if (!caseId && !override) {
    ui.showError(
      `No CFD contrast payload for model "${modelURL()}".\n` +
        'Open with ?case=<id> or ?payload=<url> to enable contrast playback.\n' +
        'The viewer remains fully usable without it.',
    );
    return;
  }
  playback.load(caseId, profile).catch((err) => {
    console.error(err);
    ui.showError(
      `Could not load the CFD contrast payload for case "${caseId || override}" (profile ${profile}).\n` +
        `The viewer remains fully usable without it. (${err.message})`,
    );
    ui.setPlayback(null);
  });
}

/**
 * Kick off the contract D clinical payload load. A missing/unfetchable
 * payload is a graceful no-op (console notice only): no vFFR map, probe or
 * pullback console, but anatomy, contrast playback, groups, picking and
 * clipping keep working untouched. An explicit `?clinical=<url>` failure
 * additionally surfaces the error banner (the user asked for that file).
 */
function loadClinicalPayload() {
  const caseId = initialCaseId();
  const override = clinicalOverride();
  if (!caseId && !override) {
    console.warn(
      '[main] no clinical payload for model',
      modelURL(),
      '— open with ?case=<id> or ?clinical=<url> to enable the ischemia map and Navvus probe',
    );
    return;
  }
  loadClinical(caseId)
    .then(({ json, vffr }) => {
      clinicalModel = createClinicalModel(json);
      if (playback) playback.attachVFFR(vffr, json);
      hook.clinical.ready = true;
      updateHook();
    })
    .catch((err) => {
      console.warn('[main] clinical payload unavailable:', err);
      if (override) {
        ui.showError(
          `Could not load the clinical payload "${override}".\n` +
            `The viewer remains fully usable without it. (${err.message})`,
        );
      }
    });
}
