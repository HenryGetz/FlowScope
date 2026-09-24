import * as THREE from 'three';
import { resolveStructure } from './structures.js';

const RAY_LENGTH = 0.4;
const GRIP_COLOR = 0x39455a;
const GRIP_GRAB_COLOR = 0x7fd4a0;

// Squeeze arbitration distances (m): grip proximity to the MPR slice quad
// surface, and pick-ray hit distance to the same quad.
const QUAD_GRAB_DIST = 0.04;
const QUAD_RAY_GRAB_DIST = 0.15;

/** Per-hand squeeze modes, arbitrated once per squeeze press. */
const MODE_MODEL = 'model';
const MODE_PLANE = 'plane';
const MODE_WAND = 'wand';

// xr-standard gamepad button indices (also used by the smoke device config).
const TRIGGER_BUTTON = 0;
const SQUEEZE_BUTTON = 1;

// Cross-section clip nudge while no hand grips the model: thumbstick Y
// (xr-standard axes[1]) or touchpad Y (axes[3]) of either controller.
const THUMBSTICK_Y_AXIS = 1;
const TOUCHPAD_Y_AXIS = 3;
const CLIP_DEADZONE = 0.15;
const CLIP_NUDGE_RATE = 0.5; // offset01 per second at full deflection
const CLIP_NUDGE_MAX_STEP = 0.05; // hard per-frame bound (offset01)

const _delta = new THREE.Matrix4();
const _model = new THREE.Matrix4();
const _gripA = new THREE.Matrix4();
const _gripB = new THREE.Matrix4();
const _gripAInv = new THREE.Matrix4();
const _mrel = new THREE.Matrix4();
const _q = new THREE.Quaternion();
const _qInv = new THREE.Quaternion();
const _v0 = new THREE.Vector3();
const _v1 = new THREE.Vector3();
const _mid = new THREE.Vector3();
const _origin = new THREE.Vector3();
const _dir = new THREE.Vector3();
const _dragDelta = new THREE.Matrix4();
const _quadInv = new THREE.Matrix4();
const _pA = new THREE.Vector3();
const _pB = new THREE.Vector3();
const _wPos = new THREE.Vector3();
const _wDir = new THREE.Vector3();

/**
 * 6DOF controller interaction, shared by the desktop page and the XR smoke
 * page (identical module in both). Grip squeeze (squeezestart/squeezeend)
 * arbitrates once per press, in priority order:
 *  - grip within QUAD_GRAB_DIST of the MPR slice quad surface, or its pick-ray
 *    hitting the quad within QUAD_RAY_GRAB_DIST = plane grab: onPlaneGrabStart,
 *    then onPlaneDrag(deltaWorld) every update with the hand's world delta
 *    since the last frame, and onPlaneGrabEnd on release.
 *  - pick-ray hitting a structure = model grab: one hand applies the grip's
 *    6DOF delta in world space, two hands drive rotation + uniform scale from
 *    the relative transform between the grips; on release the model stays
 *    where it was placed.
 *  - otherwise (empty space) = Slicing Wand: onWandPose(hand world position,
 *    hand forward (-Z) world direction, true) every update while held, and
 *    exactly one (... false) on release (the plane stays put).
 *  - `select` (trigger) ray-picks a structure and toggles its visibility.
 *  - while no hand holds a squeeze mode, thumbstick/touchpad Y of either
 *    controller nudges the cross-section clip offset through
 *    `onClipNudge(delta01)`.
 *
 * mprMesh is the MprSlice slice quad (unit PlaneGeometry under its mesh
 * transform); when null the plane-grab branch is skipped. The model group must
 * be a direct child of the scene (identity parent).
 */
export function createInteraction({
  renderer,
  scene,
  modelRoot,
  mprMesh = null,
  onToggle = () => {},
  onClipNudge = () => {},
  onPlaneGrabStart = () => {},
  onPlaneDrag = () => {},
  onPlaneGrabEnd = () => {},
  onWandPose = () => {},
}) {
  const structures = [];
  const raycaster = new THREE.Raycaster();
  const hands = [createHand(0), createHand(1)];

  /** Grab reference captured at grab start / hand-count change. */
  let grabRef = null;

  function createHand(index) {
    const targetRay = renderer.xr.getController(index);
    const grip = renderer.xr.getControllerGrip(index);

    const rayGeometry = new THREE.BufferGeometry().setFromPoints([
      new THREE.Vector3(0, 0, 0),
      new THREE.Vector3(0, 0, -RAY_LENGTH),
    ]);
    const ray = new THREE.Line(
      rayGeometry,
      new THREE.LineBasicMaterial({ color: 0x8fa3b8, transparent: true, opacity: 0.85 }),
    );
    targetRay.add(ray);

    const gripBox = new THREE.Mesh(
      new THREE.BoxGeometry(0.035, 0.07, 0.035),
      new THREE.MeshStandardMaterial({ color: GRIP_COLOR, roughness: 0.6, metalness: 0.1 }),
    );
    grip.add(gripBox);

    scene.add(targetRay);
    scene.add(grip);

    const hand = {
      index,
      targetRay,
      grip,
      gripBox,
      mode: null, // active squeeze mode (MODE_MODEL/MODE_PLANE/MODE_WAND)
      prevGripInv: new THREE.Matrix4(), // inverse grip pose at the last plane drag
      squeezed: false,
      selectActed: false,
      inputSource: null,
    };

    // WebXRController forwards every input event to BOTH the target-ray and
    // the grip group; the handlers below are idempotent per hand.
    for (const target of [targetRay, grip]) {
      target.addEventListener('connected', (event) => {
        if (event.data) hand.inputSource = event.data;
      });
      target.addEventListener('disconnected', () => {
        hand.inputSource = null;
      });
      target.addEventListener('squeezestart', (event) => onSqueeze(event, hand));
      target.addEventListener('squeeze', (event) => onSqueeze(event, hand));
      target.addEventListener('squeezeend', () => {
        hand.squeezed = false;
        endSqueeze(hand);
      });
      target.addEventListener('selectstart', (event) => onSelect(event, hand));
      target.addEventListener('select', (event) => onSelect(event, hand));
      target.addEventListener('selectend', () => {
        hand.selectActed = false;
      });
    }
    return hand;
  }

  function buttonDown(event, index) {
    const gamepad = event.data && event.data.gamepad;
    if (!gamepad || !gamepad.buttons || index >= gamepad.buttons.length) return null;
    return !!gamepad.buttons[index].pressed;
  }

  // Exactly one grab start per squeeze press, across hosts that fire
  // `squeeze` before `squeezestart` (press) or after `squeezeend` (release).
  function onSqueeze(event, hand) {
    const down = buttonDown(event, SQUEEZE_BUTTON);
    if (down === false) return;
    hand.squeezed = true;
    startSqueeze(hand);
  }

  // Exactly one pick per trigger press (`select` may trail `selectend`).
  function onSelect(event, hand) {
    const down = buttonDown(event, TRIGGER_BUTTON);
    if (down === false) return;
    if (hand.selectActed) return;
    hand.selectActed = true;
    pick(hand);
  }

  // ---- squeeze arbitration: plane grab / model grab / slicing wand -------

  /**
   * Exactly one arbitration per squeeze press. Priority: slice-quad plane
   * grab, then structure-ray model grab, then Slicing Wand in empty space.
   */
  function startSqueeze(hand) {
    if (hand.mode !== null) return;
    hand.gripBox.material.color.setHex(GRIP_GRAB_COLOR);
    if (nearSliceQuad(hand) || rayHitsSliceQuad(hand)) {
      hand.mode = MODE_PLANE;
      gripMatrix(hand, _gripA);
      hand.prevGripInv.copy(_gripA).invert();
      onPlaneGrabStart();
    } else if (raycastStructures(hand)) {
      hand.mode = MODE_MODEL;
      capture();
    } else {
      hand.mode = MODE_WAND;
    }
  }

  function endSqueeze(hand) {
    if (hand.mode === null) return;
    const mode = hand.mode;
    hand.mode = null;
    hand.gripBox.material.color.setHex(GRIP_COLOR);
    if (mode === MODE_MODEL) {
      capture(); // re-baseline the remaining hand(s): the model stays put
    } else if (mode === MODE_PLANE) {
      onPlaneGrabEnd();
    } else {
      emitWand(hand, false); // exactly one release pose: the plane stays put
    }
  }

  /** Grip world position within QUAD_GRAB_DIST of the slice quad surface. */
  function nearSliceQuad(hand) {
    if (!mprMesh || !mprMesh.visible) return false;
    mprMesh.updateWorldMatrix(true, false);
    hand.grip.updateWorldMatrix(true, false);
    _pA.setFromMatrixPosition(hand.grip.matrixWorld);
    _quadInv.copy(mprMesh.matrixWorld).invert();
    _pB.copy(_pA).applyMatrix4(_quadInv);
    // quad geometry space: the surface is z = 0 over [-0.5, 0.5]^2
    _pB.set(
      Math.min(0.5, Math.max(-0.5, _pB.x)),
      Math.min(0.5, Math.max(-0.5, _pB.y)),
      0,
    );
    _pB.applyMatrix4(mprMesh.matrixWorld);
    return _pA.distanceTo(_pB) <= QUAD_GRAB_DIST;
  }

  /** Pick-ray hits the slice quad within QUAD_RAY_GRAB_DIST (m). */
  function rayHitsSliceQuad(hand) {
    if (!mprMesh || !mprMesh.visible) return false;
    hand.targetRay.updateWorldMatrix(true, false);
    _origin.setFromMatrixPosition(hand.targetRay.matrixWorld);
    _dir.set(0, 0, -1).transformDirection(hand.targetRay.matrixWorld);
    raycaster.set(_origin, _dir);
    const hits = raycaster.intersectObject(mprMesh, true);
    return hits.length > 0 && hits[0].distance <= QUAD_RAY_GRAB_DIST;
  }

  /** First visible-structure pick-ray hit along the hand's ray, or null. */
  function raycastStructures(hand) {
    if (structures.length === 0) return null;
    hand.targetRay.updateWorldMatrix(true, false);
    _origin.setFromMatrixPosition(hand.targetRay.matrixWorld);
    _dir.set(0, 0, -1).transformDirection(hand.targetRay.matrixWorld);
    raycaster.set(_origin, _dir);
    const targets = structures
      .filter((entry) => entry.object.visible)
      .map((entry) => entry.object);
    const hits = raycaster.intersectObjects(targets, true);
    return hits.length > 0 ? hits[0] : null;
  }

  /** Slicing Wand pose: hand (grip) world position and forward (-Z). */
  function emitWand(hand, active) {
    hand.grip.updateWorldMatrix(true, false);
    _wPos.setFromMatrixPosition(hand.grip.matrixWorld);
    _wDir.set(0, 0, -1).transformDirection(hand.grip.matrixWorld);
    onWandPose(_wPos, _wDir, active);
  }

  function gripMatrix(hand, out) {
    hand.grip.updateWorldMatrix(true, false);
    return out.copy(hand.grip.matrixWorld);
  }

  function capture() {
    grabRef = null;
    const active = hands.filter((hand) => hand.mode === MODE_MODEL);
    if (active.length === 0) return;

    modelRoot.updateMatrix();
    _model.copy(modelRoot.matrix).decompose(_v0, _q, _v1);
    const pos0 = _v0.clone();
    const quat0 = _q.clone();
    const scl0 = _v1.clone();

    if (active.length === 1) {
      const g0 = gripMatrix(active[0], new THREE.Matrix4());
      grabRef = {
        mode: 1,
        hand: active[0],
        g0Inv: g0.invert(),
        model0: _model.clone(),
      };
      return;
    }

    const [handA, handB] = active.slice().sort((a, b) => a.index - b.index);
    const ga = gripMatrix(handA, _gripA.clone());
    const gb = gripMatrix(handB, _gripB.clone());
    const pa = new THREE.Vector3().setFromMatrixPosition(ga);
    const pb = new THREE.Vector3().setFromMatrixPosition(gb);
    _mrel.multiplyMatrices(gb, ga.clone().invert()).decompose(_v0, _q, _v1);
    grabRef = {
      mode: 2,
      handA,
      handB,
      rel0: _q.clone(), // relative orientation of gripB w.r.t. gripA
      mid0: pa.clone().add(pb).multiplyScalar(0.5),
      dist0: pa.distanceTo(pb),
      pos0,
      quat0,
      scl0,
    };
  }

  /** Per-frame update: clip nudge, plane drags/wands, then the grip configuration. */
  function update(dt) {
    nudgeClip(dt);
    for (const hand of hands) {
      if (hand.mode === MODE_PLANE) {
        gripMatrix(hand, _gripA);
        _dragDelta.multiplyMatrices(_gripA, hand.prevGripInv);
        hand.prevGripInv.copy(_gripA).invert();
        onPlaneDrag(_dragDelta);
      } else if (hand.mode === MODE_WAND) {
        emitWand(hand, true);
      }
    }
    if (!grabRef) return;
    const active = hands.filter((hand) => hand.mode === MODE_MODEL);
    if (active.length !== grabRef.mode) {
      capture();
      return;
    }

    if (grabRef.mode === 1) {
      gripMatrix(grabRef.hand, _gripA);
      _delta.multiplyMatrices(_gripA, grabRef.g0Inv);
      _model.multiplyMatrices(_delta, grabRef.model0);
      _model.decompose(modelRoot.position, modelRoot.quaternion, modelRoot.scale);
    } else {
      gripMatrix(grabRef.handA, _gripA);
      gripMatrix(grabRef.handB, _gripB);
      _mrel.multiplyMatrices(_gripB, _gripAInv.copy(_gripA).invert());
      _q.setFromRotationMatrix(_mrel); // current relative orientation
      _qInv.copy(grabRef.rel0).invert();
      _q.multiply(_qInv); // rotation delta of the grip pair
      _v0.setFromMatrixPosition(_gripA);
      _v1.setFromMatrixPosition(_gripB);
      _mid.copy(_v0).add(_v1).multiplyScalar(0.5);
      const scale = grabRef.dist0 > 1e-6 ? _v0.distanceTo(_v1) / grabRef.dist0 : 1;
      modelRoot.position
        .copy(grabRef.pos0)
        .sub(grabRef.mid0)
        .multiplyScalar(scale)
        .applyQuaternion(_q)
        .add(_mid);
      modelRoot.quaternion.copy(_q).multiply(grabRef.quat0);
      modelRoot.scale.copy(grabRef.scl0).multiplyScalar(scale);
    }
    modelRoot.updateMatrix();
  }

  // ---- cross-section clip nudge ------------------------------------------

  /** Thumbstick/touchpad Y deflection of a hand (0 when idle or no gamepad). */
  function clipAxis(hand) {
    const gamepad = hand.inputSource && hand.inputSource.gamepad;
    const axes = gamepad && gamepad.axes;
    if (!axes) return 0;
    const thumb = axes.length > THUMBSTICK_Y_AXIS ? axes[THUMBSTICK_Y_AXIS] : 0;
    const pad = axes.length > TOUCHPAD_Y_AXIS ? axes[TOUCHPAD_Y_AXIS] : 0;
    return Math.abs(pad) > Math.abs(thumb) ? pad : thumb;
  }

  /**
   * While no hand holds a squeeze mode (model/plane grab or slicing wand),
   * thumbstick Y (xr-standard axes[1]) or touchpad Y (axes[3]) of either
   * controller nudges the cross-section offset at a bounded per-frame rate
   * (stick up = deeper cut).
   */
  function nudgeClip(dt) {
    if (hands.some((hand) => hand.mode !== null)) return;
    let axis = 0;
    for (const hand of hands) {
      const value = clipAxis(hand);
      if (Math.abs(value) > Math.abs(axis)) axis = value;
    }
    const magnitude = (Math.abs(axis) - CLIP_DEADZONE) / (1 - CLIP_DEADZONE);
    if (magnitude <= 0) return;
    const deflection = magnitude * Math.sign(axis);
    const seconds = (Number.isFinite(dt) ? Math.min(Math.max(dt, 0), 100) : 1000 / 60) / 1000;
    // Gamepad Y is negative toward the top of the stick: up = deeper cut.
    const delta = -deflection * CLIP_NUDGE_RATE * seconds;
    onClipNudge(Math.max(-CLIP_NUDGE_MAX_STEP, Math.min(CLIP_NUDGE_MAX_STEP, delta)));
  }

  // ---- trigger picking ---------------------------------------------------

  function pick(hand) {
    const hit = raycastStructures(hand);
    if (!hit) return null;
    const entry = entryFor(hit.object);
    if (!entry) return null;
    setStructureVisible(entry.name, !entry.object.visible);
    return entry.name;
  }

  function entryFor(object) {
    let node = object;
    while (node && node !== modelRoot) {
      const entry = structures.find((item) => item.object === node);
      if (entry) return entry;
      node = node.parent;
    }
    return null;
  }

  // ---- structure registry ------------------------------------------------

  function setStructures(list) {
    structures.length = 0;
    for (const item of list) {
      structures.push({
        name: item.name,
        object: item.object,
        info: resolveStructure(item.name),
      });
    }
  }

  function setStructureVisible(name, visible) {
    const entry = structures.find((item) => item.name === name);
    if (!entry || entry.object.visible === visible) return;
    entry.object.visible = visible;
    onToggle(name, visible);
  }

  function visibleNames() {
    return structures.filter((entry) => entry.object.visible).map((entry) => entry.name);
  }

  return {
    setStructures,
    setStructureVisible,
    visibleNames,
    entries() {
      return structures.slice();
    },
    update,
  };
}
