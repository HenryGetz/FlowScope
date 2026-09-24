import * as THREE from 'three';
import { resolveStructure } from './structures.js';

const RAY_LENGTH = 0.4;
const GRIP_COLOR = 0x39455a;
const GRIP_GRAB_COLOR = 0x7fd4a0;

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
// Timeline scrub (while a grip holds the model): same thumbstick/touchpad Y.
const SCRUB_RATE = 0.5; // time01 per second at full deflection
const SCRUB_MAX_STEP = 0.05; // hard per-frame bound (time01)
const TAP_DOUBLE_MS = 300; // two trigger presses within this => play/pause
const CLICK_MOVE_PX = 5; // desktop click vs orbit drag: max pointer travel

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
const _ndc = new THREE.Vector2();

/**
 * 6DOF controller interaction, shared by the desktop page and the XR smoke
 * page (identical module in both):
 *  - grip squeeze (squeezestart/squeezeend) grabs the model group: one hand
 *    applies the grip's 6DOF delta in world space, two hands drive rotation +
 *    uniform scale from the relative transform between the grips; on release
 *    the model stays where it was placed.
 *  - `select` (trigger) ray-picks a structure and toggles its visibility; two
 *    trigger presses within TAP_DOUBLE_MS additionally flip playback
 *    (`onPlayPause()`) while every press still runs the pick (onToggle intact).
 *  - probe placement (contract D): a desktop click (pointer travel below
 *    CLICK_MOVE_PX, so orbit drags are exempt) and every XR trigger ray-hit
 *    offer the hit {point, object, name} to `onProbe(hit)`; a truthy return
 *    consumes the press (no visibility toggle) — main places the Navvus
 *    probe when the hit lands on a coronaries structure with clinical data.
 *  - while no hand grips the model, thumbstick/touchpad Y of either controller
 *    nudges the cross-section clip offset through `onClipNudge(delta01)`.
 *  - while a grip holds the model, the same axes scrub the playback timeline
 *    through `onScrub(delta01)` (the modifier keeps clip-nudging intact).
 *
 * The model group must be a direct child of the scene (identity parent).
 */
export function createInteraction({
  renderer,
  scene,
  modelRoot,
  camera = null,
  onToggle = () => {},
  onProbe = () => {},
  onClipNudge = () => {},
  onScrub = () => {},
  onPlayPause = () => {},
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
      grabbing: false,
      squeezed: false,
      selectActed: false,
      lastPressAt: 0,
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
        endGrab(hand);
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
    startGrab(hand);
  }

  // Exactly one pick per trigger press (`select` may trail `selectend`); a
  // second press within TAP_DOUBLE_MS additionally flips playback.
  function onSelect(event, hand) {
    const down = buttonDown(event, TRIGGER_BUTTON);
    if (down === false) return;
    if (hand.selectActed) return;
    hand.selectActed = true;
    const now = performance.now();
    const doubleTap = now - hand.lastPressAt < TAP_DOUBLE_MS;
    hand.lastPressAt = doubleTap ? 0 : now;
    pick(hand);
    if (doubleTap) onPlayPause();
  }

  // ---- grabbing ----------------------------------------------------------

  function startGrab(hand) {
    if (hand.grabbing) return;
    hand.grabbing = true;
    hand.gripBox.material.color.setHex(GRIP_GRAB_COLOR);
    capture();
  }

  function endGrab(hand) {
    if (!hand.grabbing) return;
    hand.grabbing = false;
    hand.gripBox.material.color.setHex(GRIP_COLOR);
    capture(); // re-baseline the remaining hand(s): the model stays put
  }

  function gripMatrix(hand, out) {
    hand.grip.updateWorldMatrix(true, false);
    return out.copy(hand.grip.matrixWorld);
  }

  function capture() {
    grabRef = null;
    const active = hands.filter((hand) => hand.grabbing);
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

  /** Per-frame update: stick scrub while gripping, clip nudge otherwise. */
  function update(dt) {
    if (hands.some((hand) => hand.grabbing)) scrubTimeline(dt);
    else nudgeClip(dt);
    if (!grabRef) return;
    const active = hands.filter((hand) => hand.grabbing);
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

  // ---- stick timeline scrub / cross-section clip nudge --------------------

  /** Thumbstick/touchpad Y deflection of a hand (0 when idle or no gamepad). */
  function stickAxis(hand) {
    const gamepad = hand.inputSource && hand.inputSource.gamepad;
    const axes = gamepad && gamepad.axes;
    if (!axes) return 0;
    const thumb = axes.length > THUMBSTICK_Y_AXIS ? axes[THUMBSTICK_Y_AXIS] : 0;
    const pad = axes.length > TOUCHPAD_Y_AXIS ? axes[TOUCHPAD_Y_AXIS] : 0;
    return Math.abs(pad) > Math.abs(thumb) ? pad : thumb;
  }

  /** Deadzone-corrected deflection of the strongest axis across both hands. */
  function stickDeflection() {
    let axis = 0;
    for (const hand of hands) {
      const value = stickAxis(hand);
      if (Math.abs(value) > Math.abs(axis)) axis = value;
    }
    const magnitude = (Math.abs(axis) - CLIP_DEADZONE) / (1 - CLIP_DEADZONE);
    return magnitude <= 0 ? 0 : magnitude * Math.sign(axis);
  }

  /** Bounded step from a deflection at the given rate over `dt` milliseconds. */
  function stickStep(dt, rate, maxStep) {
    const seconds = (Number.isFinite(dt) ? Math.min(Math.max(dt, 0), 100) : 1000 / 60) / 1000;
    const delta = -stickDeflection() * rate * seconds; // stick up = positive step
    return Math.max(-maxStep, Math.min(maxStep, delta));
  }

  /**
   * While a grip holds the model (the scrub modifier), thumbstick/touchpad Y
   * scrubs the playback timeline at a bounded per-frame rate via
   * `onScrub(delta01)` — clip nudging stays on the no-grip path below.
   */
  function scrubTimeline(dt) {
    const delta = stickStep(dt, SCRUB_RATE, SCRUB_MAX_STEP);
    if (delta !== 0) onScrub(delta);
  }

  /**
   * While NOT gripping the model with a hand, thumbstick Y (xr-standard
   * axes[1]) or touchpad Y (axes[3]) of either controller nudges the
   * cross-section offset at a bounded per-frame rate (stick up = deeper cut).
   */
  function nudgeClip(dt) {
    // Gamepad Y is negative toward the top of the stick: up = deeper cut.
    const delta = stickStep(dt, CLIP_NUDGE_RATE, CLIP_NUDGE_MAX_STEP);
    if (delta !== 0) onClipNudge(delta);
  }

  // ---- trigger / click picking -------------------------------------------

  /** Raycast every visible structure along the current raycaster ray. */
  function visibleHits() {
    if (structures.length === 0) return null;
    const targets = structures
      .filter((entry) => entry.object.visible)
      .map((entry) => entry.object);
    return raycaster.intersectObjects(targets, true);
  }

  /** Best hit -> { entry, hit } (structure entry owning the hit object). */
  function bestHit(hits) {
    if (!hits || hits.length === 0) return null;
    const entry = entryFor(hits[0].object);
    if (!entry) return null;
    return { entry, hit: hits[0] };
  }

  function pick(hand) {
    hand.targetRay.updateWorldMatrix(true, false);
    _origin.setFromMatrixPosition(hand.targetRay.matrixWorld);
    _dir.set(0, 0, -1).transformDirection(hand.targetRay.matrixWorld);
    raycaster.set(_origin, _dir);
    const found = bestHit(visibleHits());
    if (!found) return null;
    if (onProbe({ point: found.hit.point, object: found.hit.object, name: found.entry.name })) {
      return found.entry.name; // consumed (Navvus probe placement): no toggle
    }
    setStructureVisible(found.entry.name, !found.entry.object.visible);
    return found.entry.name;
  }

  // ---- desktop click probe placement -------------------------------------
  // A click (pointer travel below CLICK_MOVE_PX, so OrbitControls drags stay
  // exempt) offers the surface hit to `onProbe` exactly like an XR trigger.

  let pointerDown = false;
  let downX = 0;
  let downY = 0;
  renderer.domElement.addEventListener('pointerdown', (event) => {
    if (event.button !== 0) return;
    pointerDown = true;
    downX = event.clientX;
    downY = event.clientY;
  });
  renderer.domElement.addEventListener('pointerup', (event) => {
    if (!pointerDown || event.button !== 0) return;
    pointerDown = false;
    const dx = event.clientX - downX;
    const dy = event.clientY - downY;
    if (dx * dx + dy * dy > CLICK_MOVE_PX * CLICK_MOVE_PX) return; // orbit drag
    if (!camera) return;
    const rect = renderer.domElement.getBoundingClientRect();
    if (!(rect.width > 0) || !(rect.height > 0)) return;
    _ndc.set(
      ((event.clientX - rect.left) / rect.width) * 2 - 1,
      -((event.clientY - rect.top) / rect.height) * 2 + 1,
    );
    raycaster.setFromCamera(_ndc, camera);
    const found = bestHit(visibleHits());
    if (!found) return;
    onProbe({ point: found.hit.point, object: found.hit.object, name: found.entry.name });
  });

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
