/** Immersive session modes the viewer can start. */
export const XR_MODES = ['immersive-vr', 'immersive-ar'];

/** `?xr=` values accepted on the loading link. */
const MODE_ALIASES = { vr: 'immersive-vr', ar: 'immersive-ar' };

/**
 * Immersive mode configured by the loading link: `?xr=vr` or `?xr=ar` (bare or
 * `immersive-` prefixed). Default immersive-ar (Quest 3 passthrough MR);
 * unknown values fall back to the default with a console warning.
 */
export function sessionMode() {
  const raw = new URLSearchParams(window.location.search).get('xr');
  const key = raw ? raw.trim().toLowerCase().replace(/^immersive-/, '') : '';
  const mode = MODE_ALIASES[key];
  if (mode) return mode;
  if (raw) {
    console.warn(`[flowscope] unknown ?xr=${raw} — use ?xr=vr or ?xr=ar; defaulting to immersive-ar`);
  }
  return 'immersive-ar';
}

/** Session options per the shared contract (all optional features). */
const SESSION_OPTIONS = {
  optionalFeatures: ['local-floor', 'bounded-floor', 'hand-tracking'],
};

/**
 * immersive-ar runs over the Quest 3 passthrough feed (full-color MR):
 * `environmentBlendMode: 'alpha-blend'` composites the scene over the camera
 * view (given a transparent clear — see scene.js) and the optional hit-test /
 * plane-detection / anchors modules unlock the platform's spatial features.
 */
const AR_SESSION_OPTIONS = {
  ...SESSION_OPTIONS,
  optionalFeatures: [
    ...SESSION_OPTIONS.optionalFeatures,
    'hit-test',
    'plane-detection',
    'anchors',
  ],
  environmentBlendMode: 'alpha-blend',
};

function sessionOptions(mode) {
  return mode === 'immersive-ar' ? AR_SESSION_OPTIONS : SESSION_OPTIONS;
}

/**
 * WebXR availability for an immersive mode with an explanatory reason (for the
 * Enter button tooltip). Returns { supported, reason }.
 */
export async function xrAvailability(mode) {
  if (window.isSecureContext === false) {
    return {
      supported: false,
      reason:
        'WebXR needs a secure context: use https:// (npm run cert && npm run serve:https) or http://localhost.',
    };
  }
  if (!navigator.xr || typeof navigator.xr.isSessionSupported !== 'function') {
    return {
      supported: false,
      reason: 'This browser does not expose the WebXR API (navigator.xr is missing).',
    };
  }
  try {
    const supported = await navigator.xr.isSessionSupported(mode);
    return supported
      ? { supported: true, reason: '' }
      : { supported: false, reason: `This device reports no ${mode} support.` };
  } catch (err) {
    return {
      supported: false,
      reason: `${mode} support check failed: ${err && err.message ? err.message : err}`,
    };
  }
}

/**
 * Start an immersive session ('immersive-vr' over an opaque backdrop,
 * 'immersive-ar' over the passthrough feed) with local-floor reference space
 * and wire the stage's desktop/XR placement. Resolves with the XRSession.
 */
export async function enterXR(renderer, stage, mode) {
  const session = await navigator.xr.requestSession(mode, sessionOptions(mode));
  renderer.xr.setReferenceSpaceType('local-floor');
  await renderer.xr.setSession(session);
  stage.setXRActive(true, mode);
  session.addEventListener('end', () => stage.setXRActive(false), { once: true });
  return session;
}

export function exitXR(renderer) {
  const session = renderer.xr.getSession();
  if (session) session.end().catch(() => {});
}
