import { resolveStructure } from './structures.js';

const STYLE_ID = 'flowscope-viewer-style';

const STYLE = `
.fs-panel{position:fixed;top:10px;left:10px;z-index:10;min-width:230px;max-height:calc(100vh - 20px);overflow:auto;background:rgba(13,16,22,.88);color:#dfe5ee;font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;padding:10px 12px;border:1px solid rgba(255,255,255,.08);border-radius:10px;backdrop-filter:blur(4px);-webkit-backdrop-filter:blur(4px)}
.fs-title{font-weight:700;letter-spacing:.04em;margin-bottom:6px;color:#fff}
.fs-stats{color:#9fb0c4;margin-bottom:6px}
.fs-section{color:#9fb0c4;letter-spacing:.04em;margin:6px 0 2px}
.fs-groups{display:flex;flex-direction:column;gap:2px;margin:2px 0 6px}
.fs-structures{display:flex;flex-direction:column;gap:2px;margin:2px 0 8px}
.fs-row{display:flex;align-items:center;gap:7px;cursor:pointer;padding:1px 3px;border-radius:4px}
.fs-row:hover{background:rgba(255,255,255,.06)}
.fs-row input{margin:0;flex:none}
.fs-swatch{width:12px;height:12px;border-radius:3px;border:1px solid rgba(0,0,0,.45);flex:none}
.fs-name{overflow-wrap:anywhere}
.fs-clip{display:flex;align-items:center;gap:7px;margin:4px 0 8px}
.fs-clip input[type=range]{flex:1;min-width:80px;accent-color:#4f9cff}
.fs-clip-pct{color:#9fb0c4;min-width:30px;text-align:right}
.fs-play{display:flex;align-items:center;gap:7px;margin:4px 0}
.fs-play input[type=range]{flex:1;min-width:80px;accent-color:#ffb454}
.fs-play-time{color:#9fb0c4;min-width:54px;text-align:right}
.fs-btnrow{display:flex;align-items:center;gap:4px;flex-wrap:wrap;margin:2px 0 6px}
.fs-btn{padding:2px 8px;border-radius:5px;border:1px solid rgba(255,255,255,.15);background:rgba(255,255,255,.05);color:#dfe5ee;font:inherit;cursor:pointer}
.fs-btn:hover:not(:disabled){background:rgba(255,255,255,.12)}
.fs-btn.fs-on{background:#5a3a10;border-color:#c98a2e;color:#ffe1ad;font-weight:700}
.fs-btn:disabled{opacity:.5;cursor:not-allowed}
.fs-transit{color:#ffcf8a}
.fs-vr{width:100%;padding:6px 10px;border-radius:6px;border:1px solid rgba(120,180,255,.35);background:#1d3a5f;color:#eaf2ff;font:inherit;font-weight:700;cursor:pointer}
.fs-vr:hover:not(:disabled){background:#274b78}
.fs-vr:disabled{opacity:.55;cursor:not-allowed}
.fs-error{position:fixed;top:60px;left:50%;transform:translateX(-50%);z-index:20;display:none;max-width:min(680px,92vw);background:rgba(60,12,16,.95);color:#ffd9dd;border:1px solid #aa3333;font:13px/1.55 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;padding:12px 16px;border-radius:10px;white-space:pre-wrap}
.fs-error.fs-visible{display:block}
`;

/** Contract group -> panel row label, in panel order (exactly 5 rows). */
const GROUP_ROWS = [
  ['myocardium', 'Myocardium Shell'],
  ['chambers', 'Internal Chambers'],
  ['great_vessels', 'Great Vessels'],
  ['coronaries', 'Coronary Tree'],
  ['other', 'Other'],
];

/** Mandated load defaults (chambers resolve from the load-time myocardium state). */
const GROUP_DEFAULT_VISIBLE = {
  myocardium: true,
  chambers: false,
  great_vessels: true,
  coronaries: true,
  other: false,
};

function ensureStyle() {
  if (document.getElementById(STYLE_ID)) return;
  const style = document.createElement('style');
  style.id = STYLE_ID;
  style.textContent = STYLE;
  document.head.appendChild(style);
}

/**
 * DOM overlay (top-left, dark panel): FPS + frame ms (rolling 1 s),
 * renderer.info.render.triangles, the 5 group toggle rows (checkbox per
 * contract structure group), the structure toggle list (checkbox + palette
 * swatch per GLB node name, coherent with the group state both ways), the
 * Cross-section slider (0-100, fires onClipChange(offset01)), the Contrast
 * playback HUD (scrub timeline + Play/Pause + Loop + speed 0.25/0.5/1/2 +
 * profile A/B/C + distal transit-time readout, driven by setPlayback and the
 * onPlay* callbacks) and the Enter VR button. Also mirrors the stats (plus
 * contrast time/transit when a payload is active) into the in-XR head-locked
 * quad and hosts the on-screen error banner.
 */
export function createUI(
  container,
  {
    stage,
    onToggleVisible,
    onToggleGroup = () => {},
    onClipChange = () => {},
    onPlayToggle = () => {},
    onPlayLoop = () => {},
    onPlaySpeed = () => {},
    onPlayProfile = () => {},
    onPlayScrub = () => {},
    onEnterVR,
    onExitVR,
  },
) {
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
    <div class="fs-section">Groups</div>
    <div class="fs-groups"></div>
    <div class="fs-section">Structures</div>
    <div class="fs-structures"></div>
    <div class="fs-clip">
      <span class="fs-name">Cross-section</span>
      <input data-fs="clip" type="range" min="0" max="100" step="1" value="0" />
      <span class="fs-clip-pct" data-fs="clip-pct">0%</span>
    </div>
    <div class="fs-section">Contrast</div>
    <div class="fs-play">
      <input data-fs="play-scrub" type="range" min="0" max="1000" step="1" value="0" disabled />
      <span class="fs-play-time" data-fs="play-time">&mdash;</span>
    </div>
    <div class="fs-btnrow">
      <button class="fs-btn" type="button" data-fs="play-toggle" disabled>Play</button>
      <label class="fs-row"><input data-fs="play-loop" type="checkbox" checked disabled /><span class="fs-name">Loop</span></label>
    </div>
    <div class="fs-btnrow">
      <button class="fs-btn" type="button" data-fs="play-speed" value="0.25">0.25&times;</button>
      <button class="fs-btn" type="button" data-fs="play-speed" value="0.5">0.5&times;</button>
      <button class="fs-btn fs-on" type="button" data-fs="play-speed" value="1">1&times;</button>
      <button class="fs-btn" type="button" data-fs="play-speed" value="2">2&times;</button>
    </div>
    <div class="fs-btnrow">
      <button class="fs-btn fs-on" type="button" data-fs="play-profile" value="A">A</button>
      <button class="fs-btn" type="button" data-fs="play-profile" value="B">B</button>
      <button class="fs-btn" type="button" data-fs="play-profile" value="C">C</button>
    </div>
    <div class="fs-clip">
      <span class="fs-name">Transit</span>
      <span class="fs-transit" data-fs="transit">&mdash;</span>
    </div>
    <button class="fs-vr" type="button">Enter VR</button>
  `;
  container.appendChild(panel);

  const error = document.createElement('div');
  error.className = 'fs-error';
  container.appendChild(error);

  const fpsEl = panel.querySelector('[data-fs="fps"]');
  const frameEl = panel.querySelector('[data-fs="frame"]');
  const trisEl = panel.querySelector('[data-fs="tris"]');
  const groupsBox = panel.querySelector('.fs-groups');
  const structuresBox = panel.querySelector('.fs-structures');
  const clipSlider = panel.querySelector('[data-fs="clip"]');
  const clipPct = panel.querySelector('[data-fs="clip-pct"]');
  const vrButton = panel.querySelector('.fs-vr');
  const checkboxes = new Map();
  const rowGroups = new Map();
  /** group -> { checkbox, members: [{ name, checkbox }] } */
  const groupRows = new Map();

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

  clipSlider.addEventListener('input', () => {
    const pct = Number(clipSlider.value);
    clipPct.textContent = `${pct}%`;
    onClipChange(pct / 100);
  });

  // ---- contrast playback (state-driven render, see setPlayback) -----------
  const playScrub = panel.querySelector('[data-fs="play-scrub"]');
  const playTime = panel.querySelector('[data-fs="play-time"]');
  const playToggle = panel.querySelector('[data-fs="play-toggle"]');
  const playLoop = panel.querySelector('[data-fs="play-loop"]');
  const transitEl = panel.querySelector('[data-fs="transit"]');
  const speedButtons = Array.from(panel.querySelectorAll('[data-fs="play-speed"]'));
  const profileButtons = Array.from(panel.querySelectorAll('[data-fs="play-profile"]'));
  /** Latest playback readout (null until a payload is active). */
  let playState = null;

  playScrub.addEventListener('input', () => {
    onPlayScrub(Number(playScrub.value) / 1000);
  });
  playToggle.addEventListener('click', () => onPlayToggle());
  playLoop.addEventListener('change', () => onPlayLoop(playLoop.checked));
  for (const button of speedButtons) {
    button.addEventListener('click', () => onPlaySpeed(Number(button.value)));
  }
  for (const button of profileButtons) {
    button.addEventListener('click', () => onPlayProfile(button.value));
  }

  /** Render the Contrast HUD from the stored readout (also driven by tick). */
  function renderPlayback() {
    const available = !!(playState && playState.available);
    playScrub.disabled = !available;
    playToggle.disabled = !available;
    playLoop.disabled = !available;
    for (const button of speedButtons) button.disabled = !available;
    for (const button of profileButtons) button.disabled = !available;
    if (!available) {
      playScrub.value = '0';
      playTime.textContent = '—';
      playToggle.textContent = 'Play';
      transitEl.textContent = '—';
      return;
    }
    playScrub.value = String(Math.round(Math.min(1, Math.max(0, playState.time01)) * 1000));
    playTime.textContent = `${playState.t_s.toFixed(2)} s`;
    playToggle.textContent = playState.playing ? 'Pause' : 'Play';
    playLoop.checked = !!playState.loop;
    for (const button of speedButtons) {
      button.classList.toggle('fs-on', Number(button.value) === playState.speed);
    }
    for (const button of profileButtons) {
      button.classList.toggle('fs-on', button.value === playState.profile);
    }
    transitEl.textContent =
      playState.transitMs == null ? '—' : `${Math.round(playState.transitMs)} ms`;
  }

  function tick() {
    const { fps, frameMs } = stage.metrics;
    const tris = stage.triangles();
    fpsEl.textContent = `FPS ${fps.toFixed(1)}`;
    frameEl.textContent = `frame ${frameMs.toFixed(1)} ms`;
    trisEl.textContent = `triangles ${tris.toLocaleString('en-US')}`;
    renderPlayback();
    const stats = [`FPS ${fps.toFixed(1)}`, `frame ${frameMs.toFixed(1)} ms`, `triangles ${tris}`];
    if (playState && playState.available) {
      let line = `contrast t ${playState.t_s.toFixed(2)} s`;
      if (playState.transitMs != null) line += `  transit ${Math.round(playState.transitMs)} ms`;
      stats.push(line);
    }
    stage.setStatsText(stats);
  }
  setInterval(tick, 250);
  tick();

  function setStructures(list) {
    groupsBox.textContent = '';
    structuresBox.textContent = '';
    checkboxes.clear();
    rowGroups.clear();
    groupRows.clear();

    for (const [group, label] of GROUP_ROWS) {
      const row = document.createElement('label');
      row.className = 'fs-row';

      const checkbox = document.createElement('input');
      checkbox.type = 'checkbox';
      checkbox.checked = GROUP_DEFAULT_VISIBLE[group];
      checkbox.addEventListener('change', () => onToggleGroup(group, checkbox.checked));

      const name = document.createElement('span');
      name.className = 'fs-name';
      name.textContent = label;

      row.append(checkbox, name);
      groupsBox.appendChild(row);
      groupRows.set(group, { checkbox, members: [] });
    }

    for (const item of list) {
      const info = resolveStructure(item.name);
      const label = document.createElement('label');
      label.className = 'fs-row';

      const checkbox = document.createElement('input');
      checkbox.type = 'checkbox';
      checkbox.checked = true;
      checkbox.addEventListener('change', () => {
        onToggleVisible(item.name, checkbox.checked);
        syncGroup(info.group);
      });

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
      rowGroups.set(item.name, info.group);
      const group = groupRows.get(info.group);
      if (group) group.members.push({ name: item.name, checkbox });
    }

    for (const group of groupRows.keys()) syncGroup(group);
  }

  /** Group checkbox mirrors its member rows (indeterminate when mixed). */
  function syncGroup(group) {
    const entry = groupRows.get(group);
    if (!entry || entry.members.length === 0) return; // absent group: keep default
    let visible = 0;
    for (const member of entry.members) if (member.checkbox.checked) visible += 1;
    entry.checkbox.checked = visible === entry.members.length;
    entry.checkbox.indeterminate = visible > 0 && visible < entry.members.length;
  }

  function setVisibility(name, visible) {
    const checkbox = checkboxes.get(name);
    if (!checkbox) return;
    checkbox.checked = visible;
    syncGroup(rowGroups.get(name));
  }

  /** Mirror the clip offset (0..1) into the Cross-section slider (no event). */
  function setClipValue(offset01) {
    const pct = Math.round(Math.min(1, Math.max(0, offset01)) * 100);
    clipSlider.value = String(pct);
    clipPct.textContent = `${pct}%`;
  }

  function showError(message) {
    error.textContent = message;
    error.classList.add('fs-visible');
  }

  renderVRButton();

  return {
    setStructures,
    setVisibility,
    setClipValue,
    /** Store the playback readout (or null) and refresh the Contrast HUD. */
    setPlayback(state) {
      playState = state;
      renderPlayback();
    },
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
