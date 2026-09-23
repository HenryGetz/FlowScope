/** Session options per the shared contract (all optional features). */
const SESSION_OPTIONS = {
  optionalFeatures: ['local-floor', 'bounded-floor', 'hand-tracking'],
};

/**
 * WebXR availability with an explanatory reason (for the Enter VR tooltip).
 * Returns { supported, reason }.
 */
export async function vrAvailability() {
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
    const supported = await navigator.xr.isSessionSupported('immersive-vr');
    return supported
      ? { supported: true, reason: '' }
      : { supported: false, reason: 'This device reports no immersive-vr support.' };
  } catch (err) {
    return {
      supported: false,
      reason: `immersive-vr support check failed: ${err && err.message ? err.message : err}`,
    };
  }
}

export async function isVRSupported() {
  return (await vrAvailability()).supported;
}

/**
 * Start an immersive-vr session with local-floor reference space and wire the
 * stage's desktop/XR placement. Resolves with the XRSession.
 */
export async function enterVR(renderer, stage) {
  const session = await navigator.xr.requestSession('immersive-vr', SESSION_OPTIONS);
  renderer.xr.setReferenceSpaceType('local-floor');
  await renderer.xr.setSession(session);
  stage.setXRActive(true);
  session.addEventListener('end', () => stage.setXRActive(false), { once: true });
  return session;
}

export function exitVR(renderer) {
  const session = renderer.xr.getSession();
  if (session) session.end().catch(() => {});
}
