/**
 * Frame telemetry: rolling fps/frameMs from the stage, renderer draw counters,
 * and GPU frame time via EXT_disjoint_timer_query_webgl2 when the context
 * exposes it (gpuMs stays null otherwise).
 *
 * GPU timing model: one TIME_ELAPSED query is kept open across consecutive
 * tick() calls, so in steady state each query brackets exactly one rendered
 * frame (the render that runs between two ticks) — one begin/end per frame,
 * no allocation per frame (query slots are preallocated). Results are read
 * back with a two-frame lag; intervals that report a GPU disjoint are dropped
 * and the last valid sample is kept.
 */
export function createTelemetry({ renderer, stage }) {
  const stats = { fps: 0, frameMs: 0, gpuMs: null, triangles: 0, drawCalls: 0 };

  const gl = renderer.getContext();
  const ext = typeof gl.getExtension === 'function' ? gl.getExtension('EXT_disjoint_timer_query_webgl2') : null;
  let timing = !!(ext && gl.createQuery && gl.beginQuery && gl.endQuery && gl.deleteQuery && gl.getQueryParameter);

  const TIME_ELAPSED = ext ? ext.TIME_ELAPSED_EXT : 0;
  const GPU_DISJOINT = ext ? ext.GPU_DISJOINT_EXT : 0;
  const READ_LAG = 2; // frames between a query ending and its result being read
  const SLOTS = READ_LAG + 2;

  /** Preallocated result slots (query ended `frame` frames ago). */
  const slots = [];
  for (let i = 0; i < SLOTS; i += 1) slots.push({ query: null, frame: -1 });
  let head = 0; // next slot a finished query lands in
  let open = null; // query bracketing the in-flight frame
  let frame = 0;

  function discardQueries() {
    if (open) {
      gl.deleteQuery(open);
      open = null;
    }
    for (const slot of slots) {
      if (slot.query) {
        gl.deleteQuery(slot.query);
        slot.query = null;
      }
    }
  }

  /**
   * Call once per frame (the render runs between consecutive calls).
   * dt (ms) is accepted for the stage frame-hook signature; query pacing is
   * frame-counted, not time-based.
   */
  function tick(dt) {
    if (!timing) return;
    frame += 1;
    if (open) {
      gl.endQuery(TIME_ELAPSED);
      const slot = slots[head];
      if (slot.query) gl.deleteQuery(slot.query); // lagged result: drop
      slot.query = open;
      slot.frame = frame;
      head = (head + 1) % SLOTS;
      open = null;
    }
    for (const slot of slots) {
      if (!slot.query || frame - slot.frame < READ_LAG) continue;
      if (gl.getQueryParameter(slot.query, gl.QUERY_RESULT_AVAILABLE)) {
        if (!gl.getParameter(GPU_DISJOINT)) {
          stats.gpuMs = gl.getQueryParameter(slot.query, gl.QUERY_RESULT) / 1e6;
        }
        gl.deleteQuery(slot.query);
        slot.query = null;
      }
    }
    try {
      open = gl.createQuery();
      gl.beginQuery(TIME_ELAPSED, open);
    } catch {
      timing = false;
      discardQueries();
    }
  }

  /** Latest counters; the returned object is reused across calls. */
  function sample() {
    const metrics = stage.metrics;
    stats.fps = metrics.fps;
    stats.frameMs = metrics.frameMs;
    stats.triangles = renderer.info.render.triangles;
    stats.drawCalls = renderer.info.render.calls;
    return stats;
  }

  function dispose() {
    if (!timing) return;
    timing = false;
    if (open) {
      gl.endQuery(TIME_ELAPSED);
    }
    discardQueries();
  }

  return { tick, sample, dispose };
}
