import { resolveStructure } from './structures.js';

const STYLE_ID = 'flowscope-viewer-style';

const STYLE = `
.fs-panel{position:fixed;top:10px;left:10px;z-index:10;min-width:230px;max-height:calc(100vh - 20px);overflow:auto;background:rgba(13,16,22,.88);color:#dfe5ee;font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;padding:10px 12px;border:1px solid rgba(255,255,255,.08);border-radius:10px;backdrop-filter:blur(4px);-webkit-backdrop-filter:blur(4px)}
.fs-title{font-weight:700;letter-spacing:.04em;margin-bottom:6px;color:#fff}
.fs-stats{color:#9fb0c4;margin-bottom:6px}
.fs-structures{display:flex;flex-direction:column;gap:2px;margin:6px 0 8px}
.fs-row{display:flex;align-items:center;gap:7px;cursor:pointer;padding:1px 3px;border-radius:4px}
.fs-row:hover{background:rgba(255,255,255,.06)}
.fs-row input{margin:0;flex:none}
.fs-swatch{width:12px;height:12px;border-radius:3px;border:1px solid rgba(0,0,0,.45);flex:none}
.fs-name{overflow-wrap:anywhere}
.fs-vr{width:100%;padding:6px 10px;border-radius:6px;border:1px solid rgba(120,180,255,.35);background:#1d3a5f;color:#eaf2ff;font:inherit;font-weight:700;cursor:pointer}
.fs-vr:hover:not(:disabled){background:#274b78}
.fs-vr:disabled{opacity:.55;cursor:not-allowed}
.fs-error{position:fixed;top:60px;left:50%;transform:translateX(-50%);z-index:20;display:none;max-width:min(680px,92vw);background:rgba(60,12,16,.95);color:#ffd9dd;border:1px solid #aa3333;font:13px/1.55 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;padding:12px 16px;border-radius:10px;white-space:pre-wrap}
.fs-error.fs-visible{display:block}
`;

function ensureStyle() {
  if (document.getElementById(STYLE_ID)) return;
  const style = document.createElement('style');
  style.id = STYLE_ID;
  style.textContent = STYLE;
  document.head.appendChild(style);
}

/**
 * DOM overlay (top-left, dark panel): FPS + frame ms (rolling 1 s),
 * renderer.info.render.triangles, structure toggle list (checkbox + palette
 * swatch per GLB node name) and the Enter VR button. Also mirrors the stats
 * into the in-XR head-locked quad and hosts the on-screen error banner.
 */
export function createUI(container, { stage, onToggleVisible, onEnterVR, onExitVR }) {
  ensureStyle();

  const panel = document.createElement('div');
  panel.className = 'fs-panel';
  panel.innerHTML = `
    <div class="fs-title">FlowScope</div>
    <div class="fs-stats">
      <div data-fs="fps"></div>
      <div data-fs="frame"></div>
      <div data-fs="tris"></div>
    </div>
    <div class="fs-structures"></div>
    <button class="fs-vr" type="button">Enter VR</button>
  `;
  container.appendChild(panel);

  const error = document.createElement('div');
  error.className = 'fs-error';
  container.appendChild(error);

  const fpsEl = panel.querySelector('[data-fs="fps"]');
  const frameEl = panel.querySelector('[data-fs="frame"]');
  const trisEl = panel.querySelector('[data-fs="tris"]');
  const structuresBox = panel.querySelector('.fs-structures');
  const vrButton = panel.querySelector('.fs-vr');
  const checkboxes = new Map();

  let vrSupported = false;
  let vrReason = 'Checking WebXR support…';
  let xrActive = false;
  let busy = false;

  function renderVRButton() {
    vrButton.textContent = xrActive ? 'Exit VR' : 'Enter VR';
    vrButton.disabled = busy || (!xrActive && !vrSupported);
    vrButton.title = xrActive
      ? 'End the immersive-vr session'
      : vrSupported
        ? 'Start an immersive-vr session'
        : vrReason;
  }

  vrButton.addEventListener('click', async () => {
    if (xrActive) {
      onExitVR();
      return;
    }
    busy = true;
    renderVRButton();
    try {
      await onEnterVR();
    } catch (err) {
      console.error(err);
      showError(`Could not start a WebXR session: ${err && err.message ? err.message : err}`);
    } finally {
      busy = false;
      renderVRButton();
    }
  });

  function tick() {
    const { fps, frameMs } = stage.metrics;
    const tris = stage.triangles();
    fpsEl.textContent = `FPS ${fps.toFixed(1)}`;
    frameEl.textContent = `frame ${frameMs.toFixed(1)} ms`;
    trisEl.textContent = `triangles ${tris.toLocaleString('en-US')}`;
    stage.setStatsText([`FPS ${fps.toFixed(1)}`, `frame ${frameMs.toFixed(1)} ms`, `triangles ${tris}`]);
  }
  setInterval(tick, 250);
  tick();

  function setStructures(list) {
    structuresBox.textContent = '';
    checkboxes.clear();
    for (const item of list) {
      const info = resolveStructure(item.name);
      const label = document.createElement('label');
      label.className = 'fs-row';

      const checkbox = document.createElement('input');
      checkbox.type = 'checkbox';
      checkbox.checked = true;
      checkbox.addEventListener('change', () => onToggleVisible(item.name, checkbox.checked));

      const swatch = document.createElement('span');
      swatch.className = 'fs-swatch';
      const [r, g, b, a] = info.rgba;
      swatch.style.background = `rgba(${Math.round(r * 255)}, ${Math.round(g * 255)}, ${Math.round(b * 255)}, ${a})`;
      if (!info.known) swatch.title = 'unknown structure: hashed hue';

      const name = document.createElement('span');
      name.className = 'fs-name';
      name.textContent = item.name;

      label.append(checkbox, swatch, name);
      structuresBox.appendChild(label);
      checkboxes.set(item.name, checkbox);
    }
  }

  function setVisibility(name, visible) {
    const checkbox = checkboxes.get(name);
    if (checkbox) checkbox.checked = visible;
  }

  function showError(message) {
    error.textContent = message;
    error.classList.add('fs-visible');
  }

  renderVRButton();

  return {
    setStructures,
    setVisibility,
    setVRState({ supported, reason }) {
      vrSupported = supported;
      vrReason = reason || '';
      renderVRButton();
    },
    setActive(active) {
      xrActive = active;
      renderVRButton();
    },
    showError,
    clearBoot() {
      const boot = container.querySelector('#boot');
      if (boot) boot.remove();
    },
  };
}
