// SPHEREx Blink, design study v5: the plate screen (v2's look) on real data.
// Two sources, one contract (v1, PLAN.md section 5), both read only:
//   gallery  the five challenge targets, built in advance: web/data/<key>/ (static files)
//   live     any other position (a click on the sky, typed coordinates): the live server named in web/config.js
//            (/api/manifest, /api/band, /api/frame, /api/gaia, /api/spectrum). It asks the archive which exposures
//            cover the field, reads their pixels and stacks them; a band can take up to a minute.
// Rules taken from the placeholder page web/app.js (orchestrator, 14 Nov): little-endian .f32 with row 0 = south,
// one asinh scale per band shared by all passes, NaN = no data, +Infinity = saturated (brightest), saturation
// carried to the other passes where they are bright too, passes with fewer than 5 exposures skipped by default,
// passes with 0 exposures have no file. Unknown fields are ignored. Nothing here is synthetic; the decorative sky
// behind fades out before a frame shows.
const D2R = Math.PI / 180, SVGNS = "http://www.w3.org/2000/svg", enc = encodeURIComponent;
const DATA = new URL("../../web/data/", import.meta.url).href;
export const FULL_TOOL = new URL("../../web/index.html", import.meta.url).href;
export const FIELD_DEG = 0.1166;                  // 7 arcmin: the server's largest field today
// A free position is rounded once, to 4 decimals (0.36 arcsec; the grid pixel is 3 arcsec), and that one value is used
// for the server request and for the address. The server keys a field on 4 decimals: a 6-decimal request and a
// 5-decimal address could fall on two sides of a rounding edge and build the same field twice (orchestrator, 14 Nov).
export const round4 = (x) => Math.round(x * 1e4) / 1e4;

// ---------------------------------------------------------------- data
const store = new Map();
function littleEndian(buf) {
  if (new Uint8Array(new Uint16Array([1]).buffer)[0] === 1) return buf;
  const dv = new DataView(buf), out = new ArrayBuffer(buf.byteLength), ov = new DataView(out);
  for (let i = 0; i < buf.byteLength; i += 4) ov.setFloat32(i, dv.getFloat32(i, true));
  return out;
}
async function getJSON(u) { const r = await fetch(u); if (!r.ok) throw new Error(`${r.status} ${u.split("?")[0].split("/").pop()}`); return r.json(); }
async function getF32(u) { const r = await fetch(u); if (!r.ok) return null; return new Float32Array(littleEndian(await r.arrayBuffer())); }
// the live server: looked for when it is first needed, and again after a failure
let API = "";
export async function findServer() {
  if (API) return API;
  const url = (window.SPX_API || "").replace(/\/$/, "");
  if (!url) return "";
  const ctl = new AbortController(), timer = setTimeout(() => ctl.abort(), 1800);
  try { const r = await fetch(`${url}/api/health`, { signal: ctl.signal }); if (r.ok) API = url; } catch { /* not reachable */ }
  clearTimeout(timer);
  return API;
}
const entry = (key, m, live) => ({ key, m, live, bandJson: {}, frames: new Map(), sat: {}, spectrum: null, gaia: null });
const urls = (e) => (e.live ? {
  band: (b) => `${API}/api/band?key=${enc(e.key)}&band=${enc(b)}`,
  frame: (ep, b) => `${API}/api/frame?key=${enc(e.key)}&epoch=${ep}&band=${enc(b)}`,
  gaia: () => `${API}/api/gaia?key=${enc(e.key)}`,
  spectrum: () => `${API}/api/spectrum?key=${enc(e.key)}&ra=${e.m.query.ra}&dec=${e.m.query.dec}`,
} : {
  band: (b) => `${DATA}${e.key}/${b}.json`,
  frame: (ep, b) => `${DATA}${e.key}/${ep}_${b}.f32`,
  gaia: () => `${DATA}${e.key}/gaia.json`,
  spectrum: () => `${DATA}${e.key}/spectrum.json`,
});
export function loadTarget(key) {
  if (!store.has(key)) store.set(key, getJSON(`${DATA}${key}/manifest.json`).then((m) => entry(key, m, false)));
  return store.get(key);
}
export async function loadPosition(ra, dec) {
  if (!(await findServer())) throw new Error("no-server");
  const m = await getJSON(`${API}/api/manifest?ra=${round4(ra).toFixed(4)}&dec=${round4(dec).toFixed(4)}&fov=${FIELD_DEG}`);
  if (!store.has(m.key)) store.set(m.key, Promise.resolve(entry(m.key, m, true)));
  const e = await store.get(m.key);
  e.m = m;                                        // the newest band statuses
  return e;
}
export async function loadBand(e, band) {
  const u = urls(e);
  if (!(band in e.bandJson)) {
    if (e.live) e.bandJson[band] = await getJSON(u.band(band));             // waits until the server has built it
    else e.bandJson[band] = await getJSON(u.band(band)).catch(() => null);
  }
  const mb = e.m.bands.find((b) => b.id === band);
  if (e.live && mb && mb.status === "pending") mb.status = "ready";
  await Promise.all(e.m.epochs.map(async (ep, i) => {
    const k = `${band}|${ep.id}`;
    if (e.frames.has(k)) return;
    const empty = mb?.n_exposures?.[i] === 0 || e.bandJson[band]?.epochs?.find((x) => x.id === ep.id)?.n_used === 0;
    e.frames.set(k, empty ? null : await getF32(u.frame(ep.id, band)));
  }));
}
export const bandEpoch = (e, band, i) => e.bandJson[band]?.epochs?.find((x) => x.id === e.m.epochs[i].id) || null;
export const isLow = (e, band, i) => bandEpoch(e, band, i)?.quality === "low";
export const isEmpty = (e, band, i) => e.m.bands.find((b) => b.id === band)?.n_exposures?.[i] === 0 || bandEpoch(e, band, i)?.n_used === 0 || e.frames.get(`${band}|${e.m.epochs[i].id}`) === null;
export const playable = (e, band, i, showLow) => (showLow || !isLow(e, band, i)) && !isEmpty(e, band, i);
export const usedOf = (e, band, i) => bandEpoch(e, band, i)?.n_used ?? e.m.bands.find((b) => b.id === band)?.n_exposures?.[i] ?? 0;
export const dateOf = (e, band, i) => bandEpoch(e, band, i)?.date || e.m.epochs[i].date;
function scaleOf(e, band) {
  const s = e.bandJson[band]?.scale;
  if (s) return s;
  const v = [];
  for (const ep of e.m.epochs) { const f = e.frames.get(`${band}|${ep.id}`); if (f) for (let i = 0; i < f.length; i += 3) if (Number.isFinite(f[i])) v.push(f[i]); }
  v.sort((a, b) => a - b);
  const lo = v[Math.floor(v.length * .01)] ?? 0, hi = v[Math.floor(v.length * .997)] ?? 1;
  return { vmin: lo, vmax: hi, soft: (hi - lo) * .05 || 1e-6 };
}
// a pixel saturated in any pass on screen is drawn saturated in the other passes too, where they are bright there
function satMask(e, band, showLow) {
  const key = `${band}|${showLow}`;
  if (key in e.sat) return e.sat[key];
  let mask = null;
  e.m.epochs.forEach((ep, i) => {
    const f = showLow || !isLow(e, band, i) ? e.frames.get(`${band}|${ep.id}`) : null;
    if (f) for (let k = 0; k < f.length; k++) if (f[k] === Infinity) (mask ||= new Uint8Array(f.length))[k] = 1;
  });
  return (e.sat[key] = mask);
}
const BLACK = [0, 0, 0], WHITE = [242, 242, 240], NODATA = [26, 26, 26];
// draws pass i of a band; returns null when there is no frame, else { saturated: bool }
export function paint(cv, e, band, i, { invert = false, showLow = false } = {}) {
  const [ny, nx] = e.m.grid.shape, g = cv.getContext("2d");
  if (cv.width !== nx || cv.height !== ny) { cv.width = nx; cv.height = ny; }
  const f = e.frames.get(`${band}|${e.m.epochs[i].id}`);
  if (!f) { g.clearRect(0, 0, nx, ny); return null; }
  const s = scaleOf(e, band), img = g.createImageData(nx, ny), den = Math.asinh((s.vmax - s.vmin) / s.soft), mask = satMask(e, band, showLow);
  const [lo, hi] = invert ? [WHITE, BLACK] : [BLACK, WHITE];
  let sat = false;
  for (let y = 0; y < ny; y++) for (let x = 0; x < nx; x++) {
    const k = y * nx + x, v = f[k], o = ((ny - 1 - y) * nx + x) * 4;
    let t;
    if (v === Infinity || (mask && mask[k] && v >= s.vmax)) { t = 1; sat = true; }
    else if (!Number.isFinite(v)) { img.data[o] = NODATA[0]; img.data[o + 1] = NODATA[1]; img.data[o + 2] = NODATA[2]; img.data[o + 3] = 255; continue; }
    else t = Math.max(0, Math.min(1, s.soft > 0 ? Math.asinh((v - s.vmin) / s.soft) / den : (v - s.vmin) / (s.vmax - s.vmin)));
    img.data[o] = lo[0] + (hi[0] - lo[0]) * t; img.data[o + 1] = lo[1] + (hi[1] - lo[1]) * t; img.data[o + 2] = lo[2] + (hi[2] - lo[2]) * t; img.data[o + 3] = 255;
  }
  g.putImageData(img, 0, 0);
  return { saturated: sat };
}
// WCS: plain TAN from manifest.grid; pixel coordinates 0-based, y = 0 south
export function world2pix(m, ra, dec) {
  const g = m.grid, a0 = g.crval[0] * D2R, d0 = g.crval[1] * D2R, a = ra * D2R, d = dec * D2R;
  const cosc = Math.sin(d0) * Math.sin(d) + Math.cos(d0) * Math.cos(d) * Math.cos(a - a0);
  const xi = Math.cos(d) * Math.sin(a - a0) / cosc / D2R;
  const eta = (Math.cos(d0) * Math.sin(d) - Math.sin(d0) * Math.cos(d) * Math.cos(a - a0)) / cosc / D2R;
  return [g.crpix[0] - 1 + xi / g.cdelt[0], g.crpix[1] - 1 + eta / g.cdelt[1]];
}
export const toPlate = (m, ra, dec) => { const [x, y] = world2pix(m, ra, dec); return [x + .5, m.grid.shape[0] - 1 - y + .5]; };
const mjdOfYear = (yr) => 51544.5 + (yr - 2000) * 365.25;
export function gaiaAt(g, mjd) {
  const dt = (mjd - mjdOfYear(g.ref_epoch ?? 2016)) / 365.25;
  return [g.ra + ((g.pmra || 0) * dt) / 3.6e6 / Math.cos(g.dec * D2R), g.dec + ((g.pmdec || 0) * dt) / 3.6e6];
}
// text into a node whose CSS is text-transform: uppercase: "µm" must stay lower case (upper case it becomes the Greek capital
// Mu, "MM", a wrong unit); the unit goes into its own .unit span (text-transform: none)
export function setUpper(node, text) {
  node.replaceChildren();
  String(text).split(/(µm)/).forEach((part) => {
    if (part === "µm") { const u = document.createElement("span"); u.className = "unit"; u.textContent = part; node.append(u); }
    else if (part) node.append(part);
  });
}
export const fmtDate = (iso) => new Date(iso + "T00:00:00Z").toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" });
const el = (tag, attrs = {}, parent) => { const e = document.createElementNS(SVGNS, tag); for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v); if (parent) parent.appendChild(e); return e; };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
// target marker (open circle with four ticks) and the dashed track across passes, in plate pixels.
// A field without a named target gets four ticks around the position that was asked for.
export function drawMarker(ov, e, i, ink, animate = false) {
  const tr = e.m.target_track;
  if (!tr?.length) {
    if (!e.m.query) return;
    const [x, y] = toPlate(e.m, e.m.query.ra, e.m.query.dec), r = 4.2;
    el("path", { d: `M${x - r - 3},${y} h2.4 M${x + r + .6},${y} h2.4 M${x},${y - r - 3} v2.4 M${x},${y + r + .6} v2.4`, stroke: ink, "stroke-width": .4, fill: "none" }, ov);
    return;
  }
  const pts = tr.map((t) => ({ e: t.epoch, p: toPlate(e.m, t.ra, t.dec) }));
  el("polyline", { points: pts.map((q) => q.p.join(",")).join(" "), fill: "none", stroke: ink, "stroke-width": .25, "stroke-dasharray": ".8 1.2", opacity: .55 }, ov);
  const cur = pts.find((q) => q.e === e.m.epochs[i].id);
  if (!cur) return;
  const [x, y] = cur.p, r = 4.2;
  const c = el("circle", { cx: x, cy: y, r, fill: "none", stroke: ink, "stroke-width": .4 }, ov);
  const ticks = el("path", { d: `M${x - r - 3},${y} h2.4 M${x + r + .6},${y} h2.4 M${x},${y - r - 3} v2.4 M${x},${y + r + .6} v2.4`, stroke: ink, "stroke-width": .4, fill: "none" }, ov);
  if (animate) {
    const L = 2 * Math.PI * r;
    c.style.strokeDasharray = L; c.style.strokeDashoffset = L; c.style.animation = "draw .8s cubic-bezier(.2,.7,.2,1) forwards";
    ticks.animate([{ opacity: 0 }, { opacity: 1 }], { duration: 400, delay: 600, fill: "backwards" });
  }
}

// ---------------------------------------------------------------- the screen
export function createTool(c) {
  const { $, S, view, radec, toRaDec, slerpV, tween, E, lerp, clamp, W, H, portrait, REDUCED } = c;
  const Q = { e: null, t: null, band: null, epoch: 0, showLow: false, invert: false, mark: true, gaia: true, ready: false, playing: false, timer: null, run: 0, open: false, field: FIELD_DEG };
  let platePx = 0, plateBox = null;
  const setStatus = (h) => ($("status").innerHTML = h);
  const INK = () => (Q.invert ? "#000" : "#fff");
  const bandOf = (id = Q.band) => Q.e.m.bands.find((b) => b.id === id);
  const nPass = () => Q.e.m.epochs.length;
  const ok = (i) => playable(Q.e, Q.band, i, Q.showLow);
  const coords = (ra, dec) => `RA ${ra.toFixed(4)}°  Dec ${(dec < 0 ? "−" : "+") + Math.abs(dec).toFixed(4)}°`;

  function measurePlate() {
    const r = $("platecol").getBoundingClientRect();
    plateBox = r; platePx = r.width;
    return { cx: (r.left + r.width / 2) / W(), cy: (r.top + r.height / 2) / H() };
  }
  // ------------------------------------------------ sky -> zoom -> plate
  // t: { id, name } for a gallery target, or { ra, dec, free: true } for any other position
  async function open(t, { fast = false } = {}) {
    if (Q.open || !S.live) return;
    if (t.free) t = { ...t, ra: round4(t.ra), dec: round4(t.dec) };
    Q.open = true; Q.t = t; Q.e = null; Q.field = FIELD_DEG;
    S.toolOn = true; S.toolFade = 1; S.toolGrid = .06;
    const run = ++Q.run;
    document.body.classList.add("tool");
    history.replaceState({}, "", t.free ? `?ra=${t.ra.toFixed(4)}&dec=${t.dec.toFixed(4)}` : `?target=${enc(t.id)}`);
    const tool = $("tool");
    tool.hidden = false; tool.style.visibility = "hidden"; tool.scrollTop = 0;
    reset();
    const pc = measurePlate();
    let goal;
    // the request for the field's list of exposures runs while we fly there
    const asked = (t.free ? loadPosition(t.ra, t.dec) : loadTarget(t.id)).then((e) => ({ e }), (err) => ({ err }));
    if (t.free) goal = radec(t.ra, t.dec);
    else {
      const got = await asked;
      if (run !== Q.run) return;
      if (got.err) return fail(`This target's gallery files could not be read (${got.err.message}).`);
      Q.e = got.e; Q.field = Q.e.m.grid.shape[0] * Math.abs(Q.e.m.grid.cdelt[1]);
      goal = radec(Q.e.m.grid.crval[0], Q.e.m.grid.crval[1]);
    }
    await zoomTo(goal, pc, run, fast);
    if (run !== Q.run) return;
    tool.style.visibility = "";
    if (t.free) {
      $("tname").textContent = "This field"; $("tcoord").textContent = coords(t.ra, t.dec);
      setStatus("Asking the archive which SPHEREx exposures cover this field.");
      const pulse = $("pframe").animate([{ opacity: 1 }, { opacity: .35 }, { opacity: 1 }], { duration: 1400, iterations: Infinity });
      // the archive can be slow; the server keeps asking for up to about half a minute, and the page waits with it
      const slow = setTimeout(() => run === Q.run && setStatus("The archive is slow to answer right now. The server keeps asking; this can take up to half a minute."), 5000);
      const got = await asked;
      clearTimeout(slow); pulse.cancel();
      if (run !== Q.run) return;
      if (got.err) return setStatus(got.err.message === "no-server"
        ? `This position needs the live server, which is not part of this site. The five targets work here. <a href="https://github.com/Ayz345/Sci-Tech-SPHEREx-Blink#run-it" target="_blank" rel="noopener">How to run the server</a>`
        : `The live server could not answer for this field (${got.err.message}).`);
      Q.e = got.e;
      if (!Q.e.m.epochs?.length) return setStatus(Q.e.m.message || "SPHEREx has no exposures of this field in the released data.");
      Q.field = Q.e.m.grid.shape[0] * Math.abs(Q.e.m.grid.cdelt[1]);
      // the plate shows the server's grid: settle on its centre and its exact size
      S.sky.dir.copy(radec(Q.e.m.grid.crval[0], Q.e.m.grid.crval[1])); S.sky.goal.copy(S.sky.dir);
      S.sky.fov = Q.field * H() / platePx;
      landFrame();
    }
    Q.band = Q.e.m.default_band || (Q.e.m.bands.find((b) => b.status === "ready") || Q.e.m.bands[0]).id; Q.showLow = false;
    reveal(run, fast);
  }
  function fail(msg) {
    const tool = $("tool");
    tool.style.visibility = ""; $("tname").textContent = Q.t.name || "This field"; setStatus(msg);
  }
  async function zoomTo(goal, pc, run, fast) {
    const from = S.sky.dir.clone(), fov0 = S.sky.fov, fieldDeg = Q.field;
    const fovEnd = fieldDeg * H() / platePx, v0 = { ...view };
    const hud = $("hud"), lab = $("fovlab");
    hud.style.opacity = 1;
    const angle = Math.acos(clamp(from.dot(goal), -1, 1)) / D2R;
    // the designer, 14 Nov evening: zoom in the middle of the screen, then slide the field to the plate's place on the left.
    // mid = the screen's horizontal centre at the plate's height, so the slide afterwards is a sideways move only.
    const mid = { cx: .5, cy: pc.cy };
    // slew: longer for a longer way; the field widens a little mid-slew (a breath before the plunge)
    await tween(fast ? 0 : clamp(700 + angle * 9, 900, 2000), (k) => {
      S.sky.dir.copy(slerpV(from, goal, k)); S.sky.goal.copy(S.sky.dir);
      S.sky.fov = fov0 * (1 + .18 * Math.sin(Math.PI * k));
      Object.assign(view, { cx: lerp(v0.cx, mid.cx, k), cy: lerp(v0.cy, mid.cy, k) });
      S.toolGrid = .06 * (1 - k * .5);
      lab.textContent = `slewing · ${toRaDec(S.sky.dir).map((v) => v.toFixed(2) + "°").join("  ")}`;
    }, E.inOut);
    if (run !== Q.run) return;
    // zoom: logarithmic from 50 degrees to the field; the decorative stars fade out before the data arrives
    await tween(fast ? 0 : 2600, (k) => {
      const f = Math.exp(lerp(Math.log(fov0), Math.log(fovEnd), k));
      S.sky.fov = f;
      S.toolFade = clamp((f - .5) / 5, 0, 1);
      S.toolGrid = .03 * clamp((f - 4) / 20, 0, 1);
      lab.textContent = `field of view · ${f >= 1 ? f.toFixed(1) + "°" : (f * 60).toFixed(f * 60 >= 10 ? 0 : 1) + "′"}`;
      drawFrame(fieldDeg / f * H(), k, f, Math.sin(Math.PI * clamp(k * 1.08, 0, 1)), mid);
    }, E.expoInOut);
    S.sky.dir.copy(goal); S.sky.goal.copy(goal); S.sky.fov = fovEnd; S.toolFade = 0; S.toolGrid = 0;
    if (run !== Q.run) return;
    // slide: the zoomed field and its frame move together from the middle to the plate
    await tween(fast || Math.abs(pc.cx - mid.cx) < .01 ? 0 : 900, (k) => {
      const c = { cx: lerp(mid.cx, pc.cx, k), cy: lerp(mid.cy, pc.cy, k) };
      Object.assign(view, c);
      drawFrame(platePx, 1, 0, 0, c);
    }, E.inOut);
    if (run !== Q.run) return;
    Object.assign(view, pc);
    landFrame();
    lab.textContent = `field · ${(fieldDeg * 60).toFixed(0)}′ × ${(fieldDeg * 60).toFixed(0)}′ · north up, east left`;
    setTimeout(() => { if (run === Q.run) hud.style.opacity = 0; }, fast ? 0 : 2200);
  }
  // the field outline that grows out of a point and becomes the plate's frame
  // at: the frame's centre as screen fractions {cx, cy} (the zoom happens mid-screen); default the plate's centre
  function drawFrame(sizePx, k, fov = 0, ret = 0, at = null) {
    const fr = $("frame");
    fr.replaceChildren();
    fr.setAttribute("width", W()); fr.setAttribute("height", H());
    fr.style.width = W() + "px"; fr.style.height = H() + "px";
    if (!plateBox) return;
    const r = plateBox, cx = at ? at.cx * W() : r.left + r.width / 2, cy = at ? at.cy * H() : r.top + r.height / 2;
    const mk = (tag, a) => el(tag, a, fr);
    if (ret > .01 && fov > 0) drawReticle(mk, cx, cy, fov, ret);
    if (sizePx < 2) return;
    frameMarks(mk, cx - sizePx / 2, cy - sizePx / 2, sizePx, clamp(k * 3, 0, 1));
  }
  function frameMarks(mk, x0, y0, s, op) {
    mk("rect", { x: x0, y: y0, width: s, height: s, fill: "none", stroke: "#fff", "stroke-opacity": .55 * op, "stroke-width": 1 });
    const cl = Math.min(22, s * .18);
    for (const [px, py, dx, dy] of [[x0, y0, 1, 1], [x0 + s, y0, -1, 1], [x0, y0 + s, 1, -1], [x0 + s, y0 + s, -1, -1]])
      mk("path", { d: `M${px},${py + dy * cl} V${py} H${px + dx * cl}`, fill: "none", stroke: "#fff", "stroke-width": 1.6, "stroke-opacity": op });
    if (s > 40) {
      const per = s / (Q.field * 60);   // one tick per arcminute
      for (let i = 0; i * per <= s + .5; i++) mk("line", { x1: x0 + i * per, y1: y0 + s, x2: x0 + i * per, y2: y0 + s + (i % 5 ? 4 : 8), stroke: "#fff", "stroke-opacity": .5 * op });
    }
  }
  // once landed, the frame lives on the plate itself (so it scrolls with it on phones)
  function landFrame() {
    $("frame").replaceChildren();
    const pf = $("pframe"), s = platePx;
    pf.replaceChildren(); pf.setAttribute("viewBox", `0 0 ${s} ${s}`);
    frameMarks((tag, a) => el(tag, a, pf), 0, 0, s, 1);
  }
  // angular scale through the field centre: ticks re-space as the field narrows, finer ones appear
  const STEPS = [30, 10, 5, 2, 1, .5, 1 / 6, 5 / 60, 2 / 60, 1 / 60, .5 / 60];
  function drawReticle(mk, cx, cy, fov, a) {
    const ppd = H() / fov;
    let step = STEPS[0];
    for (const s of STEPS) if (s * ppd >= 70) step = s;
    const fine = STEPS[Math.min(STEPS.length - 1, STEPS.indexOf(step) + 1)];
    const fineA = clamp((fine * ppd - 18) / 40, 0, 1);
    const fmt = (deg) => (deg >= 1 ? `${+deg.toFixed(1)}°` : `${+(deg * 60).toFixed(1)}′`);
    mk("line", { x1: 0, y1: cy, x2: W(), y2: cy, stroke: "#fff", "stroke-opacity": .14 * a });
    mk("line", { x1: cx, y1: 0, x2: cx, y2: H(), stroke: "#fff", "stroke-opacity": .14 * a });
    const n = Math.ceil(Math.max(W(), H()) / (fine * ppd)) + 1;
    for (let i = -n; i <= n; i++) {
      if (i === 0) continue;
      const d = i * fine, major = Math.abs(d / step - Math.round(d / step)) < 1e-6;
      const p = d * ppd, len = major ? 9 : 4, op = (major ? .6 : .35 * fineA) * a;
      if (op < .01) continue;
      mk("line", { x1: cx + p, y1: cy - len, x2: cx + p, y2: cy + len, stroke: "#fff", "stroke-opacity": op });
      mk("line", { x1: cx - len, y1: cy + p, x2: cx + len, y2: cy + p, stroke: "#fff", "stroke-opacity": op });
      if (major && i > 0 && cx + p < W() - 30) mk("text", { x: cx + p, y: cy + 24, "text-anchor": "middle", fill: "#fff", "fill-opacity": .55 * a, "font-size": 10, "font-family": "IBM Plex Mono, monospace", "letter-spacing": ".06em" }).textContent = fmt(d);
    }
  }

  // ------------------------------------------------ the plate
  function reset() {
    clearInterval(Q.timer); Q.playing = false; Q.ready = false; Q.epoch = 0;
    $("plate").style.opacity = 0; $("cv").classList.remove("developing");
    $("ov").replaceChildren(); $("envelope").textContent = ""; $("tnotes").replaceChildren();
    for (const id of ["spec", "timecol", "controls"]) $(id).classList.remove("on");
    $("play").disabled = true; setStatus(""); $("time").replaceChildren(); $("wave").replaceChildren();
    $("tname").textContent = ""; $("tcoord").textContent = ""; $("specnote").textContent = "";
  }
  async function reveal(run, fast) {
    const alive = () => run === Q.run, m = Q.e.m, live = Q.e.live, wait = (ms) => sleep(fast || REDUCED ? 0 : ms);
    $("tname").textContent = live && !m.target_track?.length ? "This field" : m.name || "This field";
    $("tcoord").textContent = coords(m.query.ra, m.query.dec);
    $("tfull").href = live ? `${FULL_TOOL}?ra=${m.query.ra}&dec=${m.query.dec}&fov=${FIELD_DEG}` : `${FULL_TOOL}?target=${enc(Q.e.key)}`;
    $("tnotes").replaceChildren(...(m.notes || []).map((n) => Object.assign(document.createElement("li"), { textContent: n })));
    for (const x of [$("tback"), $("tname"), $("tcoord"), $("status")]) x.animate([{ opacity: 0, transform: "translateY(8px)" }, { opacity: 1, transform: "none" }], { duration: REDUCED ? 1 : 700, easing: "cubic-bezier(.2,.7,.2,1)", fill: "backwards" });
    drawTime(0); drawWave(0);
    // A. what there is: the passes drop onto the ruler at their real dates
    const total = m.n_images ?? m.bands.filter((b) => /^D\d$/.test(b.id)).reduce((s, b) => s + b.n_exposures.reduce((a, x) => a + x, 0), 0);
    const span = `<strong>${total} SPHEREx exposures</strong> in <strong>${nPass()} ${nPass() === 1 ? "pass" : "passes"}</strong>, ${fmtDate(m.epochs[0].date)} to ${fmtDate(m.epochs.at(-1).date)}`;
    setStatus(live ? `The archive lists ${span} for this field.` : `Gallery target. Its frames were built in advance from ${span}.`);
    const pulse = $("pframe").animate([{ opacity: 1 }, { opacity: .35 }, { opacity: 1 }], { duration: 1400, iterations: Infinity });
    $("timecol").classList.add("on");
    for (let i = 1; i <= nPass(); i++) { drawTime(i, i); await wait(340); if (!alive()) return pulse.cancel(); }
    // B. the frames of the opening band, the spectrum at the position, the Gaia stars of the field
    $("spec").classList.add("on"); $("plate").style.opacity = 1;
    const sweep = REDUCED ? null : $("scan").animate([{ top: "0%", opacity: 0 }, { opacity: .9, offset: .1 }, { opacity: .9, offset: .9 }, { top: "100%", opacity: 0 }], { duration: 900, iterations: Infinity, easing: "linear" });
    setStatus(live ? `The server is reading this field's pixels from the public archive and stacking band ${Q.band} (${bandOf().label}), one frame per pass. This can take up to a minute.`
      : `Loading band ${Q.band} (${bandOf().label}): one cleaned frame per pass, all on the same sky grid.`);
    const stop = () => { sweep?.cancel(); pulse.cancel(); };
    const u = urls(Q.e);
    // the spectrum and the Gaia stars come when they come; the frames do not wait for them
    if (!Q.e.spectrum) getJSON(u.spectrum()).then((s) => (Q.e.spectrum = Array.isArray(s) ? { meta: {}, points: s } : s), () => (Q.e.spectrum = { meta: {}, points: [] })).then(() => alive() && Q.ready && drawWave(1));
    if (!Q.e.gaia) getJSON(u.gaia()).then((g) => (Q.e.gaia = g), () => (Q.e.gaia = [])).then(() => alive() && Q.ready && render());
    try { await Promise.all([loadBand(Q.e, Q.band), wait(700)]); }
    catch (err) { stop(); return setStatus(`The frames could not be read (${err.message}).`); }
    stop();
    if (!alive()) return;
    fixLow();
    // C. each pass develops in place
    const list = m.epochs.map((_, i) => i).filter(ok);
    if (!list.length) { Q.ready = true; $("controls").classList.add("on"); render(); return setStatus(`Band ${Q.band} has no usable exposures of this field. Choose another band on the ruler.`); }
    for (const i of list) {
      setStatus(`Pass ${i + 1} of ${nPass()}, ${fmtDate(dateOf(Q.e, Q.band, i))}: ${usedOf(Q.e, Q.band, i)} exposures stacked, bad pixels masked.`);
      Q.epoch = i; render(true);
      await wait(900); if (!alive()) return;
    }
    Q.ready = true; Q.epoch = list[0] ?? 0;
    $("low").hidden = !m.epochs.some((_, i) => isLow(Q.e, Q.band, i));
    $("play").disabled = list.length < 2; $("controls").classList.add("on");
    render(false, true);
    doneStatus();
    if (list.length > 1) startPlay();
  }
  function doneStatus() {
    const n = Q.e.m.epochs.filter((_, i) => ok(i)).length, named = !!Q.e.m.target_track?.length;
    setStatus(`${n > 1 ? `Blinking ${n} passes` : n ? "One pass" : "No usable pass"} at ${bandOf().label}. ${named ? "The circle is where the target should be on each date" : "The four ticks mark the position you chose"}${Q.e.gaia?.length ? "; rings are the brighter Gaia stars, moved to that date" : ""}. Anything ${named ? "else " : ""}that shifts between passes moved on its own.`);
  }
  // fewer than two good frames in this band: show the low-coverage ones rather than nothing
  function fixLow() {
    const idx = Q.e.m.epochs.map((_, i) => i);
    const good = idx.filter((i) => !isLow(Q.e, Q.band, i) && !isEmpty(Q.e, Q.band, i));
    if (!Q.showLow && good.length < 2 && idx.some((i) => isLow(Q.e, Q.band, i))) { Q.showLow = true; $("low").setAttribute("aria-pressed", true); }
    if (!ok(Q.epoch)) Q.epoch = idx.find((i) => i > Q.epoch && ok(i)) ?? idx.find(ok) ?? Q.epoch;
  }
  // the Gaia rings: the 48 brightest stars of the field and every star within 40 arcsec of the position (a crowded
  // field would otherwise hide the frame under its own overlay)
  function ringStars() {
    if (Q.e.rings) return Q.e.rings;
    const m = Q.e.m, tr = m.target_track?.[0] || m.query, near = (g) => tr && Math.hypot((g.ra - tr.ra) * Math.cos(tr.dec * D2R), g.dec - tr.dec) * 3600 < 40;
    const sorted = [...Q.e.gaia].sort((a, b) => (a.g_mag ?? 99) - (b.g_mag ?? 99));
    return (Q.e.rings = sorted.filter((g, i) => i < 48 || near(g)));
  }
  function render(develop = false, animateMark = false) {
    const cv = $("cv"), i = Q.epoch, b = bandOf();
    const res = paint(cv, Q.e, Q.band, i, { invert: Q.invert, showLow: Q.showLow });
    const ov = $("ov"); ov.replaceChildren();
    ov.setAttribute("viewBox", `0 0 ${Q.e.m.grid.shape[1]} ${Q.e.m.grid.shape[0]}`);
    setUpper($("envelope"), `pass ${i + 1}/${nPass()} · ${fmtDate(dateOf(Q.e, Q.band, i))} · ${b.id} ${b.label} · ${res ? usedOf(Q.e, Q.band, i) + " exp. stacked" : "no data"}${isLow(Q.e, Q.band, i) ? " · low coverage" : ""}${res?.saturated ? " · saturated core drawn brightest" : ""}`);
    if (!res) el("text", { x: 70, y: 71, "font-size": 3.6, fill: "#8a8a88", "text-anchor": "middle", "font-family": "IBM Plex Mono, monospace" }, ov).textContent = `No exposures in band ${Q.band} during this pass.`;
    else {
      if (develop && !REDUCED) { cv.classList.remove("developing"); void cv.offsetWidth; cv.classList.add("developing"); }
      if (Q.gaia && Q.e.gaia?.length) {
        const mjd = bandEpoch(Q.e, Q.band, i)?.mjd_mid ?? Q.e.m.epochs[i].mjd_mid, [ny, nx] = Q.e.m.grid.shape;
        for (const g of ringStars()) {
          const [x, y] = toPlate(Q.e.m, ...gaiaAt(g, mjd));
          if (x < 0 || y < 0 || x > nx || y > ny) continue;
          el("circle", { cx: x, cy: y, r: 1.6 + Math.max(0, 19 - (g.g_mag ?? 19)) / 3.2, fill: "none", stroke: Q.invert ? "#5a5a5e" : "#d6d3cb", "stroke-width": .28, "stroke-dasharray": "1 .7", opacity: .7 }, ov);
        }
      }
      if (Q.mark) drawMarker(ov, Q.e, i, INK(), animateMark && !REDUCED);
    }
    drawTime(nPass()); drawWave(1);
  }

  // time ruler: one block per pass at its real date; its height is the number of exposures in this band
  function drawTime(nShown, dropIdx = -1) {
    const sv = $("time"); sv.replaceChildren();
    const m = Q.e.m, Wd = sv.clientWidth || 800, Hh = 112, L = 2, R = Wd - 2, base = 70;
    sv.setAttribute("viewBox", `0 0 ${Wd} ${Hh}`); sv.setAttribute("height", Hh);
    const t0 = Math.floor(m.data_range.mjd_min - 25), t1 = Math.ceil(m.data_range.mjd_max + 25);
    const X = (t) => L + ((t - t0) / (t1 - t0)) * (R - L);
    el("line", { x1: L, y1: base, x2: R, y2: base, stroke: "#fff", "stroke-opacity": .35 }, sv);
    const d = new Date((t0 - 40587) * 864e5); d.setUTCDate(1);
    for (; ; d.setUTCMonth(d.getUTCMonth() + 1)) {
      const mjd = d.getTime() / 864e5 + 40587;
      if (mjd > t1) break;
      if (mjd < t0) continue;
      const jan = d.getUTCMonth() === 0;
      el("line", { x1: X(mjd), y1: base, x2: X(mjd), y2: base + (jan ? 8 : 4), stroke: "#fff", "stroke-opacity": jan ? .6 : .3 }, sv);
      if (!jan && (R - L) * 30.4 / (t1 - t0) < 46) continue;
      el("text", { x: X(mjd) + 3, y: base + 24, "font-size": jan ? 13 : 12, fill: jan ? "#f2f2f0" : "#6a6a67", "letter-spacing": ".06em" }, sv).textContent =
        jan ? String(d.getUTCFullYear()) : d.toLocaleDateString("en-GB", { month: "short", timeZone: "UTC" }).toUpperCase();
    }
    const b = bandOf(), maxN = Math.max(...m.bands.flatMap((x) => x.n_exposures || [0]), 1);
    m.epochs.slice(0, nShown).forEach((e, i) => {
      const n = Q.ready ? usedOf(Q.e, Q.band, i) : b?.n_exposures?.[i] || 0, h = 6 + 28 * (n / maxN), cur = Q.ready && i === Q.epoch;
      const skipped = Q.ready && !ok(i);
      const x0 = X(e.mjd_min), w = Math.max(10, X(e.mjd_max) - x0);
      const anchor = x0 < 40 ? "start" : x0 > Wd - 60 ? "end" : "middle", tx = anchor === "start" ? x0 : anchor === "end" ? x0 + w : x0 + w / 2;
      const iso = Q.e ? dateOf(Q.e, Q.band, i) : e.date;   // the header uses the selected band's date: so does the ruler
      const g = el("g", { class: "epoch", tabindex: 0, role: "button", "aria-label": `Pass ${i + 1}, ${fmtDate(iso)}, ${n} exposures${skipped ? ", skipped" : ""}` }, sv);
      const rect = el("rect", { x: x0, y: base - h, width: w, height: h, fill: cur || !Q.ready ? "#fff" : "none", stroke: "#fff", "stroke-width": 1, opacity: n ? (cur || !Q.ready ? 1 : skipped ? .3 : .6) : .25 }, g);
      if (skipped) rect.setAttribute("stroke-dasharray", "2 2");
      const short = Wd < 560 || nPass() > 4;
      el("text", { x: tx, y: base - h - 9, "font-size": 12.5, "text-anchor": anchor, fill: cur ? "#fff" : "#8a8a88", "letter-spacing": ".04em" }, g).textContent =
        `${short ? new Date(iso + "T00:00:00Z").toLocaleDateString("en-GB", { month: "short", year: "2-digit", timeZone: "UTC" }) : fmtDate(iso)} · ${n}`;
      if (i === dropIdx - 1 && !REDUCED) rect.animate([{ transform: "translateY(-24px)", opacity: 0 }, { transform: "none", opacity: 1 }], { duration: 420, easing: "cubic-bezier(.3,1.4,.5,1)" });
      g.onclick = () => Q.ready && selectEpoch(i);
      g.onkeydown = (ev) => (ev.key === "Enter" || ev.key === " ") && Q.ready && (ev.preventDefault(), selectEpoch(i));
    });
  }
  // wavelength ruler: the bands of the manifest are the band picker; the spectrum at the position sits on the same axis
  function drawWave(progress) {
    const sv = $("wave"); sv.replaceChildren();
    const Wd = sv.clientWidth || 340, Hh = 226, m = Q.e.m;
    sv.setAttribute("viewBox", `0 0 ${Wd} ${Hh}`); sv.setAttribute("height", Hh);
    const L = 2, R = Wd - 10, lo = .7, hi = 5.1, X = (w) => L + ((w - lo) / (hi - lo)) * (R - L);
    const top = 26, plotH = 100, rule = 160;
    const sp = Q.e.spectrum, all = Q.ready && sp ? sp.points || [] : [];
    // R Draconis: saturated in nearly every exposure, so its relative brightness from the PSF wings is shown instead
    const wing = all.some((p) => Number.isFinite(p.wing_flux)), val = (p) => (wing ? p.wing_flux : p.flux);
    const pts = all.filter((p) => Number.isFinite(val(p)));
    if (pts.length) {
      const fl = pts.map(val).sort((a, b) => a - b), f0 = fl[Math.floor(fl.length * .02)], f1 = fl[Math.floor(fl.length * .98)];
      const Y = (f) => top + plotH - ((Math.min(f1, Math.max(f0, f)) - f0) / (f1 - f0 || 1)) * plotH;
      el("line", { x1: L, y1: top + plotH, x2: R, y2: top + plotH, stroke: "#fff", "stroke-opacity": .15 }, sv);
      const ep = m.epochs[Q.epoch].id;
      for (const p of [...pts].sort((a, b) => (a.epoch === ep) - (b.epoch === ep))) {
        const here = p.epoch === ep, faint = p.flagged || (Number.isFinite(p.neighbor_flux) && Math.abs(p.flux) < .15 * p.neighbor_flux);
        el("circle", { cx: X(p.wavelength_um), cy: Y(val(p)), r: here ? 2.6 : 1.8, fill: p.flagged || (p.saturated && !wing) ? "none" : "#fff", stroke: "#fff", "stroke-width": .6, opacity: (here ? 1 : .28) * (faint ? .45 : 1) }, sv);
      }
      el("text", { x: L, y: 11, "font-size": 11.5, fill: "#8a8a88", "letter-spacing": ".08em" }, sv).textContent =
        wing ? "RELATIVE BRIGHTNESS FROM THE PSF WINGS" : `BRIGHTNESS AT THE ${m.target_track?.length ? "TARGET" : "CHOSEN POINT"} (${sp.meta?.flux_unit || "mJy"})`;
    } else el("text", { x: L, y: top + plotH / 2, "font-size": 12.5, fill: "#8a8a88" }, sv).textContent = !Q.ready || !sp ? "The spectrum appears with the frames." : "No spectrum points at this position.";
    const usable = (b) => b.status === "ready" || (Q.e.live && b.status === "pending");
    for (const b of m.bands) {
      const x0 = X(b.wave_um[0]), x1 = X(b.wave_um[1]), extra = !/^D\d$/.test(b.id), on = b.id === Q.band, ready = usable(b);
      const y = extra ? rule - 28 : rule, h = extra ? 15 : 24;
      const g = el("g", { class: "band", tabindex: ready ? 0 : -1, role: "button", "aria-pressed": on, "aria-label": `Band ${b.id}, ${b.label}` }, sv);
      el("rect", { x: x0 + .5, y, width: x1 - x0 - 1, height: h, fill: on ? "#fff" : "none", stroke: "#fff", "stroke-opacity": ready ? .7 : .25 }, g);
      if (!Q.ready && on && progress > 0) el("rect", { x: x0 + .5, y: y + h - 2, width: (x1 - x0 - 1) * progress, height: 2, fill: "#fff" }, g);
      el("text", { x: (x0 + x1) / 2, y: y + h / 2 + 4.2, "font-size": extra ? 11 : 12.5, "text-anchor": "middle", fill: on ? "#000" : ready ? "#f2f2f0" : "#555" }, g).textContent = b.id;
      if (ready && Q.ready) {
        g.onclick = () => selectBand(b.id);
        g.onkeydown = (ev) => (ev.key === "Enter" || ev.key === " ") && (ev.preventDefault(), selectBand(b.id));
      }
    }
    for (const w of [1, 2, 3, 4, 5]) {
      el("line", { x1: X(w), y1: rule + 30, x2: X(w), y2: rule + 35, stroke: "#fff", "stroke-opacity": .5 }, sv);
      el("text", { x: X(w), y: rule + 52, "font-size": 12.5, "text-anchor": "middle", fill: "#8a8a88" }, sv).textContent = w === 5 ? "5 µm" : w;
    }
    $("specnote").textContent = !Q.ready || !sp ? "" : wing ? "Indicative only, not a formal detection of variability. The star is saturated, so its own flux cannot be measured."
      : !pts.length ? "" : `One point per exposure; this pass is bright, the others faint. ${sp.meta?.photometry === "psf_deblended" ? "Measured with a neighbouring star fitted and removed." : ""} Open circles are flagged or saturated (lower limits).`;
  }

  function selectEpoch(i) { Q.epoch = i; render(); }
  let picking = 0;
  async function selectBand(id) {
    if (id === Q.band || !Q.ready) return;
    const prev = Q.band, mine = ++picking, run = Q.run, had = id in Q.e.bandJson;
    Q.band = id; drawWave(1);
    setStatus(Q.e.live && !had ? `The server is stacking band ${id} (${bandOf().label}) from the archive. This can take up to a minute.` : `Loading band ${id} (${bandOf().label}).`);
    try { await loadBand(Q.e, id); } catch (err) { if (mine === picking && run === Q.run) { Q.band = prev; render(); setStatus(`Band ${id} could not be read (${err.message}).`); } return; }
    if (mine !== picking || run !== Q.run) return;
    fixLow();
    $("low").hidden = !Q.e.m.epochs.some((_, i) => isLow(Q.e, Q.band, i));
    $("low").setAttribute("aria-pressed", Q.showLow);
    render(true); doneStatus();
    const n = Q.e.m.epochs.filter((_, i) => ok(i)).length;
    $("play").disabled = n < 2;
    if (n < 2) stopPlay(); else if (!Q.playing) startPlay();
  }
  function step(d) {
    if (!Q.ready) return;
    const n = nPass();
    for (let k = 1; k <= n; k++) { const i = (Q.epoch + d * k + n * k) % n; if (ok(i)) { Q.epoch = i; break; } }
    render();
  }
  function startPlay() { stopPlay(); Q.playing = true; $("play").textContent = "Pause"; Q.timer = setInterval(() => step(1), 1000 / +$("speed").value); }
  function stopPlay() { clearInterval(Q.timer); Q.playing = false; $("play").textContent = "Play"; }

  // ------------------------------------------------ plate -> sky: zoom back out the way we came
  async function close() {
    if (!Q.open || Q.closing) return;
    Q.closing = true;
    const run = ++Q.run;
    stopPlay(); Q.ready = false;
    history.replaceState({}, "", location.pathname + "?stage=sky");
    const tool = $("tool");
    await tool.animate([{ opacity: 1 }, { opacity: 0 }], { duration: REDUCED ? 1 : 350, fill: "forwards" }).finished;
    $("pframe").replaceChildren(); $("hud").style.opacity = 0;
    const fovA = S.sky.fov, v0 = { ...view }, v1 = { cx: .5, cy: portrait() ? .38 : .42 };
    await tween(1700, (k) => {
      const f = Math.exp(lerp(Math.log(fovA), Math.log(50), k));
      S.sky.fov = f;
      S.toolFade = clamp((f - 1.2) / 7, 0, 1); S.toolGrid = .06 * k;
      Object.assign(view, { cx: lerp(v0.cx, v1.cx, k), cy: lerp(v0.cy, v1.cy, k) });
      drawFrame(Q.field / f * H(), 1 - k);
    }, E.expoInOut);
    Q.closing = false;
    if (run !== Q.run) return;
    $("frame").replaceChildren();
    tool.getAnimations().forEach((a) => a.cancel());
    tool.hidden = true;
    S.sky.goal.copy(S.sky.dir); S.sky.fov = 50;
    Q.open = false; S.toolOn = false;
    document.body.classList.remove("tool");
  }
  // leaving for the start page: no zoom, everything off at once
  function kill() {
    ++Q.run; stopPlay(); Q.ready = false; Q.open = false; Q.closing = false; S.toolOn = false;
    $("tool").hidden = true; $("frame").replaceChildren(); $("hud").style.opacity = 0;
    document.body.classList.remove("tool");
  }
  function onResize() {
    if (!Q.open || $("tool").style.visibility === "hidden") return;
    const pc = measurePlate();
    Object.assign(view, pc);
    S.sky.fov = Q.field * H() / platePx;
    landFrame();
    if (Q.ready) { drawTime(nPass()); drawWave(1); }
  }
  function onKey(e) {
    if (!Q.open || e.target.closest?.("input")) return false;
    if (e.key === "ArrowRight") step(1);
    else if (e.key === "ArrowLeft") step(-1);
    else if (e.key === "Escape") close();
    else if (e.key === " " && Q.ready && !e.target.closest?.("button, [role=button], a")) { e.preventDefault(); Q.playing ? stopPlay() : startPlay(); }
    else return false;
    return true;
  }
  $("tback").onclick = close;
  $("play").onclick = () => (Q.playing ? stopPlay() : startPlay());
  $("speed").oninput = () => Q.playing && startPlay();
  const toggle = (id, key, after) => ($(id).onclick = () => { Q[key] = !Q[key]; $(id).setAttribute("aria-pressed", Q[key]); if (Q.ready) after(); });
  toggle("invert", "invert", () => render());
  toggle("mark", "mark", () => render());
  toggle("gaia", "gaia", () => render());
  toggle("low", "showLow", () => { fixLow(); render(); doneStatus(); });
  // the plate keeps its place on the sky when a phone scrolls the tool
  $("tool").addEventListener("scroll", () => { if (Q.open && $("tool").style.visibility !== "hidden") Object.assign(view, measurePlate()); }, { passive: true });
  return { open, close, kill, onResize, onKey, get isOpen() { return Q.open; } };
}
