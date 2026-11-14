// SPHEREx Blink — placeholder test harness for the v1 data contract (PLAN.md section 5).
// Plain ES module, no build step. Gallery targets come from static files; free positions, name lookup and field
// changes go to the live server found on start (config.js), or to the server forced with ?api= (developer).

const $ = (id) => document.getElementById(id);
const params = new URLSearchParams(location.search);
// ?api=<url> (developer): everything goes to that server, no probe. ?api=none: no server. No ?api=: the page
// probes window.SPX_API (config.js) once on start; gallery targets stay static, the rest uses the server if found.
const API_PARAM = params.get("api");
const FORCED = !!API_PARAM && API_PARAM !== "none";
let API = FORCED ? API_PARAM.replace(/\/$/, "") : ""; // the live server in use ("" = none)
let USE_API = FORCED; // whether the target on screen is read from the server (otherwise static files)
function probeServer() {
  const url = (window.SPX_API || "").replace(/\/$/, "");
  if (FORCED) return fetch(`${API}/api/health`).then((r) => r.json()).then((h) => { S.health = h; return true; }).catch(() => false);
  if (API_PARAM === "none" || !url) return Promise.resolve(false);
  const ctl = new AbortController(), timer = setTimeout(() => ctl.abort(), 1500), t0 = performance.now();
  const li = document.createElement("li");
  return fetch(`${url}/api/health`, { signal: ctl.signal })
    .then((r) => (r.ok ? r.json() : null))
    .then((h) => { li.textContent = `${h ? 200 : "ERR"} ${Math.round(performance.now() - t0)} ms ${url}/api/health (probe)`; if (!h) return false; API = url; S.health = h; return true; })
    .catch(() => { li.textContent = `no answer ${Math.round(performance.now() - t0)} ms ${url}/api/health (probe): gallery only`; return false; })
    .finally(() => { clearTimeout(timer); $("log").prepend(li); });
}
const EMBED = params.get("embed") === "1";
const DATA = (params.get("data") || "data").replace(/\/$/, "");
const D2R = Math.PI / 180;

// ---------- data source: one code path for static files and API (USE_API decides per target) ----------
const src = {
  index: () => `${DATA}/index.json`,
  manifest: (q) => USE_API
    ? `${API}/api/manifest?` + new URLSearchParams(q.target ? { target: q.target } : Object.fromEntries(
      Object.entries({ ra: q.ra, dec: q.dec, fov: q.fov, pmra: q.pmra, pmdec: q.pmdec, ref_epoch: q.ref_epoch, name: q.name })
        .filter(([, v]) => v != null && v !== ""))) // optional pm/name: the server then builds target_track
    : q.key || q.target ? `${DATA}/${q.key || q.target}/manifest.json` : null,
  band: (key, band) => USE_API ? `${API}/api/band?key=${enc(key)}&band=${enc(band)}` : `${DATA}/${key}/${band}.json`,
  frame: (key, epoch, band, kind) => USE_API
    ? `${API}/api/frame?key=${enc(key)}&epoch=${epoch}&band=${enc(band)}` + (kind === "image" ? "" : `&kind=${kind}`)
    : `${DATA}/${key}/${epoch}_${band}${kind === "image" ? "" : "_" + kind}.f32`,
  gaia: (key) => USE_API ? `${API}/api/gaia?key=${enc(key)}` : `${DATA}/${key}/gaia.json`,
  spectrum: (key, ra, dec) => USE_API
    ? `${API}/api/spectrum?key=${enc(key)}` + (ra != null ? `&ra=${ra}&dec=${dec}` : "")
    : `${DATA}/${key}/spectrum.json`,
};
const enc = encodeURIComponent;

async function fetchLogged(url, as, init) {
  const li = document.createElement("li");
  const t0 = performance.now();
  li.textContent = `… ${url}`;
  $("log").prepend(li);
  try {
    let r;
    try { r = await fetch(url, init); } catch (netErr) { // network failure (server down), not an HTTP error
      if (API && url.startsWith(API)) setOffline(true);
      throw netErr;
    }
    if (API && url.startsWith(API)) setOffline(false);
    const ms = Math.round(performance.now() - t0);
    li.textContent = `${r.status} ${ms} ms ${url}`;
    if (!r.ok) {
      li.className = "bad";
      let msg = "";
      try { msg = (await r.json()).message || ""; } catch { /* not json */ }
      const e = new Error(`${r.status} ${msg}`.trim());
      e.status = r.status;
      throw e;
    }
    if (as === "f32") {
      const buf = await r.arrayBuffer();
      li.textContent += ` (${buf.byteLength} B)`;
      return new Float32Array(littleEndian(buf));
    }
    return await r.json();
  } catch (e) {
    li.className = "bad";
    if (!e.status) li.textContent = `ERR ${Math.round(performance.now() - t0)} ms ${url} ${e.message}`;
    throw e;
  }
}

// server reachability: shown to the user while the API is down; on recovery the band on screen is asked for again
let offline = false;
function setOffline(on) {
  if (on === offline) return;
  offline = on;
  if (on) status("The live server is not reachable right now; retrying every few seconds…", true);
  else {
    status("Live server reachable again.");
    if (S.m?.bands && S.band) loadBand(S.band).then(draw);
  }
  if (S.m?.grid) draw();
}

// Float32Array uses platform byte order; the contract is little-endian.
function littleEndian(buf) {
  if (new Uint8Array(new Uint16Array([1]).buffer)[0] === 1) return buf;
  const dv = new DataView(buf), out = new ArrayBuffer(buf.byteLength), ov = new DataView(out);
  for (let i = 0; i < buf.byteLength; i += 4) ov.setFloat32(i, dv.getFloat32(i, true));
  return out;
}

// ---------- state ----------
const S = {
  m: null, key: null, band: null, epoch: +params.get("frame") || 0,
  mode: params.get("mode") || "blink", layer: params.get("layer") || "image", zoom: +params.get("zoom") || 3,
  playing: false, timer: null,
  frames: new Map(),   // `${band}|${epoch}|${kind}` -> Float32Array | null (no data) | Promise
  bandInfo: new Map(), // band -> band json | null | Promise
  gaia: null, spectrum: null, specAt: null, userScale: null,
};

// ---------- WCS (plain TAN from manifest.grid; pixel coords 0-based, y = 0 south) ----------
function world2pix(ra, dec) {
  const g = S.m.grid, a0 = g.crval[0] * D2R, d0 = g.crval[1] * D2R, a = ra * D2R, d = dec * D2R;
  const cosc = Math.sin(d0) * Math.sin(d) + Math.cos(d0) * Math.cos(d) * Math.cos(a - a0);
  const xi = Math.cos(d) * Math.sin(a - a0) / cosc / D2R;
  const eta = (Math.cos(d0) * Math.sin(d) - Math.sin(d0) * Math.cos(d) * Math.cos(a - a0)) / cosc / D2R;
  return [g.crpix[0] - 1 + xi / g.cdelt[0], g.crpix[1] - 1 + eta / g.cdelt[1]];
}
function pix2world(x, y) {
  const g = S.m.grid, d0 = g.crval[1] * D2R;
  const xi = (x - (g.crpix[0] - 1)) * g.cdelt[0] * D2R, eta = (y - (g.crpix[1] - 1)) * g.cdelt[1] * D2R;
  const den = Math.cos(d0) - eta * Math.sin(d0);
  const ra = g.crval[0] + Math.atan2(xi, den) / D2R;
  const dec = Math.atan2(eta * Math.cos(d0) + Math.sin(d0), Math.hypot(xi, den)) / D2R;
  return [(ra + 360) % 360, dec];
}
const mjdOfYear = (yr) => 51544.5 + (yr - 2000) * 365.25;
const pixArcsec = () => Math.abs(S.m.grid.cdelt[1]) * 3600;

// ---------- loading ----------
async function loadIndex() {
  let list = [];
  try {
    const j = await fetchLogged(src.index());
    list = Array.isArray(j) ? j : j.targets || [];
  } catch {
    list = ["barnard", "eps-indi-b", "wise-0855", "rw-aur", "r-dra"].map((key) => ({ key, name: key }));
  }
  $("targets").replaceChildren(...list.map((t) => {
    const b = document.createElement("button");
    b.textContent = t.name || t.key;
    b.dataset.key = t.key;
    b.onclick = () => navigate({ target: t.key });
    return b;
  }));
}

function navigate(q) {
  const p = new URLSearchParams();
  if (API_PARAM) p.set("api", API_PARAM); // only an explicit (developer) choice is carried; the probe runs again
  if (params.get("data")) p.set("data", DATA);
  for (const [k, v] of Object.entries(q)) if (v != null && v !== "" && !(typeof v === "number" && !Number.isFinite(v))) p.set(k, v);
  location.search = p.toString();
}

async function loadTarget() {
  const q = { target: params.get("target"), key: params.get("key"), ra: params.get("ra"), dec: params.get("dec"),
    fov: params.get("fov") || "0.1", pmra: params.get("pmra"), pmdec: params.get("pmdec"),
    ref_epoch: params.get("ref_epoch"), name: params.get("name") };
  if (!q.target && !q.key && !(q.ra && q.dec)) { status("Pick a target or enter coordinates."); return; }
  const gallery = !!(q.target || q.key);
  if (!FORCED) {
    // gallery targets open from static files at once (no waiting for the probe); a free position needs the server
    USE_API = !gallery && await serverReady;
    if (!gallery && !USE_API) { status("The live server is not reachable right now, so only the five gallery targets are available.", true); return; }
  }
  let url = src.manifest(q);
  S.manifestUrl = url;
  status("Loading manifest…");
  try { S.m = await fetchLogged(url); } catch (e) {
    if (offline) { // server down: say so and try again, the page recovers by itself
      status("The live server is not reachable right now; retrying every few seconds…", true);
      setTimeout(loadTarget, 3000);
      return;
    }
    // a target that is not in the static gallery: ask the live server instead, if there is one
    if (!FORCED && gallery && !USE_API && await serverReady) { USE_API = true; return loadTarget(); }
    status(`Manifest failed: ${e.message}`, true); return;
  }
  S.key = S.m.key;
  // fallback only: the server builds target_track when given pm (manifest ?pmra=&pmdec=&ref_epoch=);
  // if a manifest still has none, propagate the URL's proper motion in the browser
  const pmra = parseFloat(params.get("pmra")), pmdec = parseFloat(params.get("pmdec"));
  if (!S.m.target_track?.length && S.m.epochs?.length && Number.isFinite(pmra) && Number.isFinite(pmdec) && q.ra) {
    const ra0 = +S.m.query.ra, dec0 = +S.m.query.dec, ref = parseFloat(params.get("ref_epoch")) || 2000;
    S.m.target_track = S.m.epochs.map((e) => {
      const dt = (e.mjd_mid - mjdOfYear(ref)) / 365.25;
      return { epoch: e.id, ra: ra0 + (pmra * dt) / 3.6e6 / Math.cos(dec0 * D2R), dec: dec0 + (pmdec * dt) / 3.6e6 };
    });
    S.m.target_source = `${params.get("name") || "resolved name"}: pm ${pmra}, ${pmdec} mas/yr from epoch ${ref}, propagated in the browser`;
  }
  if (params.get("name") && !S.m.name?.includes(params.get("name"))) S.m.name = `${params.get("name")} (${S.m.name || S.key})`;
  document.title = `${S.m.name || S.key} · SPHEREx Blink`;
  renderMeta();
  const items = (S.m.notes || []).map((n) => Object.assign(document.createElement("li"), { textContent: n }));
  if (EMBED && items.length) { // embedded: notes fold into one line ("2 notes ▸") so the player fits the frame
    const det = document.createElement("details"), sum = document.createElement("summary"), ul = document.createElement("ul");
    sum.textContent = `${items.length} note${items.length > 1 ? "s" : ""}`; ul.append(...items); det.append(sum, ul);
    $("notes").replaceChildren(det);
  } else $("notes").replaceChildren(...items);
  if (!S.m.epochs?.length) { status(S.m.message || "No epochs for this position.", true); return; }

  $("epoch").max = S.m.epochs.length - 1;
  S.epoch = Math.min(S.epoch, S.m.epochs.length - 1);
  const ready = S.m.bands.filter((b) => b.status === "ready");
  const want = params.get("band");
  S.band = (S.m.bands.find((b) => b.id === want) || S.m.bands.find((b) => b.id === S.m.default_band) || ready[0] || S.m.bands[0])?.id;
  const rgbq = (params.get("rgb") || "").split(",");
  if (rgbq.length === 3 && rgbq.every((b) => S.m.bands.some((x) => x.id === b))) S.rgb = { r: rgbq[0], g: rgbq[1], b: rgbq[2] };
  setupRGB();
  renderBands(); renderFov(); updateLinks();
  if (S.mode === "rgb") loadRGB();
  status(`${S.m.epochs.length} epochs, ${S.m.bands.length} bands. Loading ${S.band}…`);

  fetchLogged(src.gaia(S.key)).then((g) => { S.gaia = g; draw(); }).catch(() => { S.gaia = []; });
  // static: spectrum.json is the target position; API: ask for the latest track point (or the query position)
  const at = S.m.target_track?.at(-1) || S.m.query || {};
  S.specTarget = at;
  loadSpectrum(at.ra, at.dec, "target");

  if (USE_API) {
    // the server builds default_band first and the rest in the background; ask only for the band on screen
    // (an /api/band request moves that band to the front of the queue) and poll the manifest for the others
    loadBand(S.band).then(async () => {
      draw();
      // bands that are already built: fetch them one after another (no queue jumping, they are ready)
      for (const b of S.m.bands) if (b.status === "ready" && !S.bandInfo.has(b.id)) await loadBand(b.id);
    });
    pollManifest();
    return;
  }
  await loadBand(S.band);
  draw();
  // static: fetch the other bands in the background, one after another
  for (const b of S.m.bands) if (b.id !== S.band && b.status !== "empty") await loadBand(b.id).catch(() => {});
}

function loadSpectrum(ra, dec, where) {
  return fetchLogged(src.spectrum(S.key, ra, dec)).then((s) => { S.spectrum = s; S.specAt = where; plots(); }).catch(() => plots());
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function pollManifest() {
  const busy = () => S.m.bands.some((b) => b.status === "pending");
  let hadPending = busy();
  while (busy() || offline) { // keep polling while the server is down, so the page recovers by itself
    await sleep(3000);
    let m;
    try { m = await fetchLogged(S.manifestUrl); } catch { continue; }
    for (const b of m.bands || []) {
      const o = S.m.bands.find((x) => x.id === b.id);
      if (o) { o.status = b.status; o.progress = b.progress; o.n_exposures = b.n_exposures ?? o.n_exposures; }
    }
    renderBands();
    // load bands that just became ready (the selected one is already requested)
    for (const b of S.m.bands) if (b.status === "ready" && !S.bandInfo.has(b.id)) loadBand(b.id);
  }
  // the spectrum only covers bands that were ready when it was asked for: ask again once all are built
  if (hadPending && S.specAt === "target" && S.spectrum?.meta?.bands_pending?.length) {
    loadSpectrum(S.specTarget.ra, S.specTarget.dec, "target");
  }
}

function loadBand(band) {
  if (!S.bandInfo.has(band)) {
    // a failure (404, 504 timeout) is not cached, so the band can be asked for again later
    const p = fetchLogged(src.band(S.key, band)).then(
      (j) => { S.bandInfo.set(band, j); return j; },
      () => { S.bandInfo.delete(band); return null; });
    S.bandInfo.set(band, p);
  }
  // frames after the band file: an epoch with n_used 0 has no file, so it is not requested (no 404 noise)
  return Promise.resolve(S.bandInfo.get(band))
    .then(() => Promise.all(S.m.epochs.map((ep, i) => loadFrame(band, ep.id, S.layer, emptyEpoch(band, i)))))
    .then(() => { renderBands(); if (band === S.band) { if (!$("status").textContent.includes("low-coverage")) status(""); fixEpoch(); draw(); syncUrl(); } });
}

// ---------- low-coverage frames ({band}.json epochs[].quality === "low", fewer than 5 exposures) ----------
// skipped by the player unless "show low-coverage frames" (?low=1) is on; without the field nothing changes
S.showLow = params.get("low") === "1";
function bandEpoch(band, i) {
  const bj = S.bandInfo.get(band);
  return bj && !(bj instanceof Promise) ? bj.epochs?.find((e) => e.id === S.m.epochs[i].id) : null;
}
const isLow = (band, i) => bandEpoch(band, i)?.quality === "low";
const allowed = (band, i) => S.showLow || !isLow(band, i);
// playback and arrow keys also skip epochs with no data in this band (0 exposures), whatever the low option;
// the slider can still reach them and shows "no data", so the epoch is seen to exist
const playable = (band, i) => allowed(band, i) && !emptyEpoch(band, i);
// keep the current epoch on a frame that is shown; switch low frames on when fewer than 2 good frames remain
function fixEpoch() {
  if (!S.m?.epochs?.length || !S.band) return;
  const idx = S.m.epochs.map((_, i) => i);
  const good = idx.filter((i) => !isLow(S.band, i) && !emptyEpoch(S.band, i));
  if (!S.showLow && good.length < 2 && idx.some((i) => isLow(S.band, i))) {
    S.showLow = true; $("showLow").checked = true;
    status(`${S.band}: fewer than 2 frames with 5 or more exposures, so low-coverage frames are shown.`);
    syncUrl();
  }
  if (!playable(S.band, S.epoch)) {
    const ok = idx.filter((i) => playable(S.band, i));
    S.epoch = ok.find((i) => i > S.epoch) ?? ok[0] ?? S.epoch;
  }
}
// epoch markers under the slider: skipped (low-coverage) epochs are drawn faint
function renderEpochMarks() {
  if (!S.m?.epochs) return;
  $("epochMarks").replaceChildren(...S.m.epochs.map((e, i) => {
    const s = document.createElement("span"), low = isLow(S.band, i), empty = emptyEpoch(S.band, i);
    const n = bandEpoch(S.band, i)?.n_used;
    s.textContent = e.date.slice(2, 7);
    s.className = (empty ? "empty" : low ? "low" : "") + (i === S.epoch ? " cur" : "");
    s.title = empty ? `${e.date}: no data in this band (skipped in playback)`
      : low ? `low coverage: ${n ?? "?"} exposures — ${S.showLow ? "shown" : "skipped"}` : e.date;
    return s;
  }));
}

// no data for this band/epoch: listed with 0 exposures, or the band file says n_used 0
function emptyEpoch(band, i) {
  if (S.m.bands.find((b) => b.id === band)?.n_exposures?.[i] === 0) return true;
  const bj = S.bandInfo.get(band), ep = S.m.epochs[i];
  return !!(bj && !(bj instanceof Promise) && bj.epochs?.find((e) => e.id === ep.id)?.n_used === 0);
}

function loadFrame(band, epoch, kind, known_empty) {
  const k = `${band}|${epoch}|${kind}`;
  if (S.frames.has(k)) return Promise.resolve(S.frames.get(k));
  if (known_empty) { S.frames.set(k, null); return Promise.resolve(null); }
  const p = fetchLogged(src.frame(S.key, epoch, band, kind), "f32").then(
    (a) => { S.frames.set(k, a); if (band === S.band) draw(); return a; },
    () => { S.frames.delete(k); if (band === S.band) draw(); return null; }); // not cached: retried on the next request
  S.frames.set(k, p);
  return p;
}
const frameOf = (band, epoch, kind = S.layer) => {
  const f = S.frames.get(`${band}|${epoch}|${kind}`);
  return f instanceof Float32Array ? f : null;
};

// ---------- saturation union ----------
// A pixel that is +Infinity in any epoch of a band is shown saturated in every epoch of that band (blink, sum,
// false colour) and as "no data" in the difference; otherwise a changing saturated fraction blinks like a
// variable star. The files are unchanged; ?satunion=0 turns this off for comparison.
// Only frames on screen take part (low-coverage frames are left out while they are skipped), and the union is
// applied to an epoch only where that epoch is bright there too (value >= the band's scale.vmax): a saturated
// core stays steady, but a one-epoch artefact is not printed onto empty sky in the other epochs.
const SATUNION = params.get("satunion") !== "0";
function satMask(band) {
  if (!SATUNION || !S.m?.epochs) return null;
  const frames = S.m.epochs.map((e, i) => (allowed(band, i) ? frameOf(band, e.id, "image") : null)).filter(Boolean);
  const key = `${frames.length}|${S.showLow}`, c = (S.satCache ||= {})[band];
  if (c && c.key === key) return c.mask; // frames are only ever added, so count + low flag is a valid key
  let mask = null;
  for (const f of frames) for (let i = 0; i < f.length; i++) if (f[i] === Infinity) (mask ||= new Uint8Array(f.length))[i] = 1;
  S.satCache[band] = { key, mask };
  return mask;
}
// ref = this epoch's own frame (null: apply wherever the mask is set); thr = the band's vmax
function satHere(mask, i, ref, thr) {
  return !!mask?.[i] && (!ref || ref[i] === Infinity || ref[i] >= thr);
}
function applySat(a, mask, value, ref = null, thr = Infinity) {
  if (!a || !mask) return a;
  const o = Float32Array.from(a);
  for (let i = 0; i < o.length; i++) if (satHere(mask, i, ref, thr)) o[i] = value;
  return o;
}

// ---------- display array & scale ----------
function displayArray() {
  const eps = S.m.epochs.map((e) => e.id);
  const cur = frameOf(S.band, eps[S.epoch]);
  if (S.layer !== "image") return cur;
  const sat = satMask(S.band), thr = sat ? bandScale(S.band).vmax : Infinity;
  if (S.mode === "blink") return applySat(cur, sat, Infinity, cur, thr);
  // sum and difference use only the frames on screen (low-coverage frames are left out while they are skipped)
  const others = eps.filter((_, i) => allowed(S.band, i) && (S.mode === "sum" || i !== S.epoch))
    .map((e) => frameOf(S.band, e)).filter(Boolean);
  if (!others.length || (S.mode === "diff" && !cur)) return null;
  // +Infinity (saturated) propagates into the sum; any non-finite result is shown as "no data" (contract v1)
  const n = others[0].length, mean = new Float32Array(n);
  for (let i = 0; i < n; i++) {
    let s = 0, c = 0;
    for (const f of others) if (!Number.isNaN(f[i])) { s += f[i]; c++; }
    mean[i] = c && Number.isFinite(s) ? s / c : NaN;
  }
  if (S.mode === "sum") return applySat(mean, sat, Infinity);
  const d = new Float32Array(n);
  for (let i = 0; i < n; i++) { const v = cur[i] - mean[i]; d[i] = Number.isFinite(v) ? v : NaN; }
  return applySat(d, sat, NaN, cur, thr);
}

function percentile(arrs, ps) {
  const v = [];
  for (const a of arrs) for (let i = 0; i < a.length; i += 3) if (Number.isFinite(a[i])) v.push(a[i]);
  if (!v.length) return ps.map(() => 0);
  v.sort((x, y) => x - y);
  return ps.map((p) => v[Math.min(v.length - 1, Math.floor((p / 100) * v.length))]);
}

// Scale shared by all epochs of the band (so blink does not flicker).
function autoScale() {
  const all = S.m.epochs.map((e) => frameOf(S.band, e.id)).filter(Boolean);
  if (S.layer !== "image") {
    const [lo, hi] = percentile(all, [0, 100]);
    return { vmin: lo, vmax: hi || lo + 1, soft: 0, src: `${S.layer}: linear min–max` };
  }
  if (S.mode === "diff") {
    const d = displayArray();
    const [hi] = d ? percentile([d.map(Math.abs)], [99.5]) : [1];
    return { vmin: -hi, vmax: hi, soft: 0, src: "difference: linear ±99.5th percentile of |diff|" };
  }
  const bj = S.bandInfo.get(S.band);
  if (bj && bj.scale) return { ...bj.scale, src: `band file ${S.band}.json` };
  const [lo, hi] = percentile(all, [1, 99.7]);
  return { vmin: lo, vmax: hi, soft: (hi - lo) * 0.05 || 1e-6, src: "fallback: 1–99.7th percentile of all epochs" };
}

function stretch(x, s) {
  if (x === Infinity) return 1; // saturated core: brightest value
  if (!Number.isFinite(x)) return -1; // NaN: no data
  let t;
  if (s.soft > 0) t = Math.asinh((x - s.vmin) / s.soft) / Math.asinh((s.vmax - s.vmin) / s.soft);
  else t = (x - s.vmin) / (s.vmax - s.vmin);
  return Math.max(0, Math.min(1, t));
}

// ---------- false colour ----------
// default channels: blue = short, green = middle, red = long detector band (D2, D4, D6 when present)
function rgbBands() {
  if (S.rgb) return S.rgb;
  const d = S.m.bands.map((b) => b.id).filter((id) => /^D\d$/.test(id));
  const pick = (want, fb) => (d.includes(want) ? want : fb);
  return { r: pick("D6", d.at(-1)), g: pick("D4", d[Math.floor(d.length / 2)]), b: pick("D2", d[0]) };
}
// each channel is stretched with its own band's shared scale (percentile fallback when the band file has none)
function bandScale(band) {
  const bj = S.bandInfo.get(band);
  if (bj && !(bj instanceof Promise) && bj.scale) return bj.scale;
  const all = S.m.epochs.map((e) => frameOf(band, e.id, "image")).filter(Boolean);
  const [lo, hi] = percentile(all, [1, 99.7]);
  return { vmin: lo, vmax: hi, soft: (hi - lo) * 0.05 || 1e-6 };
}
function drawRGB(ctx, ep, nx, ny, z) {
  const ch = rgbBands(), ids = [ch.r, ch.g, ch.b];
  const sc = ids.map(bandScale);
  const fr = ids.map((b, k) => { const f = frameOf(b, ep.id, "image"); return applySat(f, satMask(b), Infinity, f, sc[k].vmax); });
  $("scaleSrc").textContent = `false colour: R ${ch.r} · G ${ch.g} · B ${ch.b}, each with its band's own scale`;
  if (!fr.some(Boolean)) {
    ctx.fillStyle = "#888"; ctx.font = "14px system-ui";
    ctx.fillText(ids.some((b) => S.frames.get(`${b}|${ep.id}|image`) instanceof Promise) ? "loading…" : "no data for these bands/epoch", 12, 24);
    return;
  }
  off.width = nx; off.height = ny;
  const oc = off.getContext("2d"), img = oc.createImageData(nx, ny);
  for (let y = 0; y < ny; y++) {
    const row = (ny - 1 - y) * nx;
    for (let x = 0; x < nx; x++) {
      const i = y * nx + x, o = (row + x) * 4;
      // per channel: NaN (or a missing frame) = no data in that channel (0); +Infinity = brightest (1)
      const t = fr.map((f, k) => (f ? stretch(f[i], sc[k]) : -1));
      if (t.every((v) => v < 0)) { img.data.set([40, 18, 22, 255], o); continue; }
      img.data.set([255 * Math.max(0, t[0]), 255 * Math.max(0, t[1]), 255 * Math.max(0, t[2]), 255], o);
    }
  }
  oc.putImageData(img, 0, 0);
  ctx.imageSmoothingEnabled = false;
  ctx.drawImage(off, 0, 0, nx * z, ny * z);
}
function setupRGB() {
  const ch = rgbBands();
  for (const [k, id] of [["r", "rgbR"], ["g", "rgbG"], ["b", "rgbB"]]) {
    const sel = $(id);
    sel.replaceChildren(...S.m.bands.map((b) => new Option(`${b.id} ${b.label || ""}`, b.id)));
    sel.value = ch[k];
    sel.onchange = () => { S.rgb = { ...rgbBands(), [k]: sel.value }; loadRGB(); syncUrl(); };
  }
}
function loadRGB() {
  $("rgbBox").hidden = S.mode !== "rgb";
  if (S.mode !== "rgb" || !S.m?.bands) return;
  draw();
  const ch = rgbBands();
  for (const b of new Set([ch.r, ch.g, ch.b])) loadBand(b).then(draw);
}

// ---------- drawing ----------
const off = document.createElement("canvas");
function draw() {
  if (!S.m?.grid) return;
  const [ny, nx] = S.m.grid.shape, z = S.zoom, cv = $("canvas"), ctx = cv.getContext("2d");
  cv.width = nx * z; cv.height = ny * z;
  ctx.fillStyle = "#000"; ctx.fillRect(0, 0, cv.width, cv.height);
  const ep = S.m.epochs[S.epoch];
  $("epoch").value = S.epoch;
  renderEpochMarks();
  const n = S.m.bands.find((b) => b.id === S.band)?.n_exposures?.[S.epoch];
  const bj = S.bandInfo.get(S.band);
  const used = bj?.epochs?.find((e) => e.id === ep.id)?.n_used;
  $("epochLabel").textContent = `${S.epoch + 1}/${S.m.epochs.length}  ${ep.date}  MJD ${(+ep.mjd_min).toFixed(2)}–${(+ep.mjd_max).toFixed(2)}` +
    `\n${S.band}: ${n ?? "?"} exposures listed` + (used != null ? `, ${used} used` : "");

  if (S.mode === "rgb") { drawRGB(ctx, ep, nx, ny, z); overlays(ctx, ep); return; }
  const a = displayArray();
  const s = S.userScale || autoScale();
  if (!S.userScale) setScaleInputs(s);
  $("scaleSrc").textContent = S.userScale ? "manual" : s.src;
  if (a) {
    off.width = nx; off.height = ny;
    const oc = off.getContext("2d"), img = oc.createImageData(nx, ny);
    const diverging = S.mode === "diff" && S.layer === "image";
    for (let y = 0; y < ny; y++) {
      const row = (ny - 1 - y) * nx; // first data row is south -> bottom of the canvas
      for (let x = 0; x < nx; x++) {
        const t = stretch(a[y * nx + x], s), o = (row + x) * 4;
        if (t < 0) { img.data.set([40, 18, 22, 255], o); continue; } // NaN: no data
        if (diverging) { // blue-white-red is fine for a placeholder
          const u = t * 2 - 1;
          img.data.set(u < 0 ? [255 * (1 + u), 255 * (1 + u), 255, 255] : [255, 255 * (1 - u), 255 * (1 - u), 255], o);
        } else { const g = 255 * t; img.data.set([g, g, g, 255], o); }
      }
    }
    oc.putImageData(img, 0, 0);
    ctx.imageSmoothingEnabled = false;
    ctx.drawImage(off, 0, 0, nx * z, ny * z);
  } else {
    ctx.fillStyle = "#888"; ctx.font = "14px system-ui";
    const pending = S.frames.get(`${S.band}|${ep.id}|${S.layer}`) instanceof Promise;
    ctx.fillText(pending ? "loading…" : offline ? "server not reachable, retrying…" : "no data for this band/epoch", 12, 24);
  }
  overlays(ctx, ep);
  lowLabel(ctx);
}

// ---------- measure / mark ----------
// Marks are made by the user on co-added frames: {n, ra, dec, mjd (frame's mjd_mid), epoch, date, band}.
// The motion is a straight-line fit of position against time over all marks; the uncertainty assumes the click
// is good to about one grid pixel per mark. Everything here runs in the browser, without the server.
S.marks = []; S.measuring = false; S.markSeq = 0;
const MARK_COLOR = "#ff4fd8";
function addMark(ra, dec) {
  const ep = S.m.epochs[S.epoch];
  S.marks.push({ n: ++S.markSeq, ra, dec, mjd: ep.mjd_mid, epoch: S.epoch, date: ep.date, band: S.mode === "rgb" ? "rgb" : S.band });
  renderMarks(); draw();
}
function removeMark(n) { S.marks = S.marks.filter((m) => m.n !== n); renderMarks(); draw(); }
function setMeasuring(on) {
  S.measuring = on;
  $("measureBtn").classList.toggle("on", on);
  $("canvas").classList.toggle("measuring", on);
  $("measureHelp").textContent = on
    ? "Click the object in one frame, step to another frame (←/→) and click it again. Drag a mark to move it."
    : S.marks.length ? "" : "Turn on to mark a moving object in two or more frames.";
}
const compass = (pa) => ["north", "north-east", "east", "south-east", "south", "south-west", "west", "north-west"][Math.round(pa / 45) % 8];
function motion() {
  const ms = [...S.marks].sort((a, b) => a.mjd - b.mjd);
  if (ms.length < 2) return null;
  const r0 = ms[0], cosd = Math.cos(r0.dec * D2R);
  // offsets in arcsec (x = east, y = north) against time in years
  const p = ms.map((m) => ({ t: (m.mjd - r0.mjd) / 365.25, x: ((m.ra - r0.ra + 540) % 360 - 180) * cosd * 3600, y: (m.dec - r0.dec) * 3600 }));
  const tm = p.reduce((s, q) => s + q.t, 0) / p.length, xm = p.reduce((s, q) => s + q.x, 0) / p.length, ym = p.reduce((s, q) => s + q.y, 0) / p.length;
  const stt = p.reduce((s, q) => s + (q.t - tm) ** 2, 0);
  if (stt === 0) return { sameTime: true };
  const vx = p.reduce((s, q) => s + (q.t - tm) * (q.x - xm), 0) / stt, vy = p.reduce((s, q) => s + (q.t - tm) * (q.y - ym), 0) / stt;
  const pix = pixArcsec(), sv = pix / Math.sqrt(stt); // per-axis rate uncertainty from ~1 px per click
  const last = ms.at(-1), sep = angSep(r0.ra, r0.dec, last.ra, last.dec) * 3600;
  return { n: ms.length, vx, vy, rate: Math.hypot(vx, vy), sv, pa: (Math.atan2(vx, vy) / D2R + 360) % 360,
    sep, sepErr: Math.SQRT2 * pix, dtDays: last.mjd - r0.mjd, pix };
}
function angSep(ra1, dec1, ra2, dec2) {
  const a = Math.sin(((dec2 - dec1) * D2R) / 2) ** 2 + Math.cos(dec1 * D2R) * Math.cos(dec2 * D2R) * Math.sin(((ra2 - ra1) * D2R) / 2) ** 2;
  return (2 * Math.asin(Math.min(1, Math.sqrt(a)))) / D2R;
}
function renderMarks() {
  $("markList").replaceChildren(...S.marks.map((m) => {
    const li = document.createElement("li"), del = document.createElement("button");
    li.value = m.n;
    li.append(`${m.date} ${m.band}  RA ${m.ra.toFixed(5)}  Dec ${m.dec.toFixed(5)}`);
    del.textContent = "×"; del.title = "delete this mark"; del.onclick = () => removeMark(m.n);
    li.append(del);
    return li;
  }));
  const r = motion();
  let t = "";
  if (S.marks.length === 1) t = "Mark the same object in another frame.";
  else if (r?.sameTime) t = "All marks are in the same frame: mark the object in at least two different frames.";
  else if (r) {
    const short = r.dtDays < 2, k = short ? 1 / (365.25 * 24) : 1, unit = short ? "″/h" : "″/yr";
    t = `shift ${r.sep.toFixed(1)}″ ± ${r.sepErr.toFixed(1)}″ over ${r.dtDays < 2 ? (r.dtDays * 24).toFixed(1) + " h" : r.dtDays.toFixed(1) + " days"}` +
      `\nrate ${(r.rate * k).toPrecision(3)} ± ${(r.sv * k).toPrecision(2)} ${unit} towards ${compass(r.pa)} (position angle ${r.pa.toFixed(0)}°)` +
      `\n  east ${(r.vx * k).toPrecision(3)}, north ${(r.vy * k).toPrecision(3)} ${unit}` +
      `\nfrom your clicks on co-added frames (${r.n} marks), assuming each click is good to about one grid pixel (${r.pix.toFixed(1)}″)`;
  }
  $("measureResult").textContent = t;
  if (!S.measuring) setMeasuring(false);
  // marks changed: an earlier single-exposure result no longer matches them
  if (S.track) { S.track = null; $("refineResult").textContent = "Marks changed: refine again."; }
  updateRefineButtons();
}
function drawMarks(ctx) {
  if (!S.marks.length) return;
  const z = S.zoom, ms = [...S.marks].sort((a, b) => a.mjd - b.mjd).map((m) => ({ m, p: toScreen(m.ra, m.dec) }));
  ctx.save();
  ctx.strokeStyle = MARK_COLOR; ctx.lineWidth = 1; ctx.setLineDash([2, 3]);
  ctx.beginPath(); ms.forEach(({ p }, i) => (i ? ctx.lineTo(p[0], p[1]) : ctx.moveTo(p[0], p[1]))); ctx.stroke();
  ctx.setLineDash([]);
  for (const { m, p } of ms) {
    const here = m.epoch === S.epoch, r = Math.max(5, 2 * z);
    ctx.lineWidth = here ? 2 : 1; ctx.globalAlpha = here ? 1 : 0.6;
    ctx.beginPath(); ctx.arc(p[0], p[1], r, 0, 2 * Math.PI); ctx.stroke();
    ctx.font = "bold 12px system-ui"; ctx.lineWidth = 3; ctx.strokeStyle = "#000"; ctx.strokeText(String(m.n), p[0] + r + 2, p[1] - r);
    ctx.fillStyle = MARK_COLOR; ctx.fillText(String(m.n), p[0] + r + 2, p[1] - r); ctx.strokeStyle = MARK_COLOR;
  }
  ctx.restore();
}

// ---------- refine on single exposures, report (live server) ----------
// POST /api/track re-measures the marked object on single exposures; GET /api/report downloads the file the
// server writes (CSV, or an MPC draft in ADES). The page never contacts the MPC and never writes report content.
// ?mocktrack=within|between (developer) reads web/dev-fixtures/track_*.json instead of the server.
const MOCK = params.get("mocktrack");
S.track = null;
const serverForTrack = () => !!(MOCK || API);
function updateRefineButtons() {
  const srv = serverForTrack(), n = S.marks.length;
  const why = !srv ? "Refining and reports need the live server, which is not reachable right now."
    : n < 2 ? "Mark the object in at least two frames to refine it on single exposures." : "";
  $("refineBtn").disabled = !!why; $("refineWhy").textContent = why;
  $("reportBtn").disabled = !srv || !S.track;
  $("reportBtn").title = !srv ? "needs the live server" : !S.track ? "refine on single exposures first" : "";
}
// server answer -> page fields (one place to adapt if the server's field names differ)
function normTrack(j) {
  return {
    id: j.report_id ?? j.id, regime: j.regime, regimeReason: j.regime_reason ?? "",
    nights: (j.nights || []).map((n) => ({ night: n.night, nAcc: n.n_accepted, nTot: n.n_total, included: n.included })),
    meas: (j.measurements || []).map((m) => ({ id: m.obs_id, time: m.obs_time, mjd: m.mjd, ra: m.ra, dec: m.dec,
      rmsRa: m.rms_ra, rmsDec: m.rms_dec, snr: m.snr, ok: !!m.accepted, reason: m.reason || "", band: m.band, night: m.night })),
    validation: j.validation || {},
  };
}
// straight-line fit of position against time (arcsec, years); sigma per axis from the point uncertainties
function fitMotion(pts) {
  if (pts.length < 2) return null;
  const r0 = pts[0], cosd = Math.cos(r0.dec * D2R);
  const p = pts.map((m) => ({ t: (m.mjd - r0.mjd) / 365.25, x: ((m.ra - r0.ra + 540) % 360 - 180) * cosd * 3600, y: (m.dec - r0.dec) * 3600, s: m.sig }));
  const mean = (f) => p.reduce((a, q) => a + f(q), 0) / p.length, tm = mean((q) => q.t), xm = mean((q) => q.x), ym = mean((q) => q.y);
  const stt = p.reduce((a, q) => a + (q.t - tm) ** 2, 0);
  if (stt === 0) return null;
  const vx = p.reduce((a, q) => a + (q.t - tm) * (q.x - xm), 0) / stt, vy = p.reduce((a, q) => a + (q.t - tm) * (q.y - ym), 0) / stt;
  return { vx, vy, sv: mean((q) => q.s) / Math.sqrt(stt), pa: (Math.atan2(vx, vy) / D2R + 360) % 360, dtDays: pts.at(-1).mjd - r0.mjd };
}
function rateText(f, n, what) {
  const short = f.dtDays < 2, k = short ? 1 / (365.25 * 24) : 1, unit = short ? "″/h" : "″/yr";
  return `rate ${(Math.hypot(f.vx, f.vy) * k).toPrecision(3)} ± ${(f.sv * k).toPrecision(2)} ${unit} towards ${compass(f.pa)} ` +
    `(position angle ${f.pa.toFixed(0)}°) from ${n} ${what}`;
}
async function refine() {
  $("refineResult").textContent = "Measuring the object on single exposures…";
  S.track = null; updateRefineButtons();
  const body = { key: S.key, band: S.band, marks: [...S.marks].sort((a, b) => a.mjd - b.mjd).map((m) => ({ ra: m.ra, dec: m.dec, mjd: m.mjd, band: m.band })) };
  try {
    const j = MOCK ? await fetchLogged(`dev-fixtures/track_${MOCK}.json`)
      : await fetchLogged(`${API}/api/track`, "json", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    S.track = normTrack(j); renderTrack();
  } catch (e) { $("refineResult").textContent = `Refining failed: ${e.message}`; }
  updateRefineButtons();
}
function renderTrack() {
  const t = S.track, ok = t.meas.filter((m) => m.ok).sort((a, b) => a.mjd - b.mjd);
  const f = fitMotion(ok.map((m) => ({ ra: m.ra, dec: m.dec, mjd: m.mjd, sig: Math.hypot(m.rmsRa, m.rmsDec) / Math.SQRT2 })));
  const head = document.createElement("div");
  head.className = "mono";
  head.textContent = `${t.regime}${t.regimeReason ? ": " + t.regimeReason : ""}` +
    (f ? `\n${rateText(f, ok.length, "single-exposure measurements")}` : "\nfewer than two accepted measurements: no rate");
  head.style.whiteSpace = "pre-wrap";
  const tbl = document.createElement("table");
  tbl.innerHTML = "<tr><th>time (UTC)</th><th>RA</th><th>Dec</th><th>±″</th><th>S/N</th><th>band</th><th></th></tr>" +
    t.meas.map((m) => `<tr class="${m.ok ? "" : "rejected"}"><td>${escapeHtml(String(m.time ?? ""))}</td><td>${(+m.ra).toFixed(5)}</td>` +
      `<td>${(+m.dec).toFixed(5)}</td><td>${Math.hypot(m.rmsRa, m.rmsDec).toFixed(1)}</td><td>${(+m.snr).toFixed(1)}</td>` +
      `<td>${escapeHtml(String(m.band ?? ""))}</td><td style="text-align:left">${m.ok ? "accepted" : "rejected: " + escapeHtml(m.reason)}</td></tr>`).join("");
  $("refineResult").replaceChildren(head, tbl);
}

// report window
const ADES_BLOCKED_BETWEEN = "The Minor Planet Center accepts Solar System objects only. This motion over months looks like a star or brown dwarf; use the CSV.";
function openReport() {
  const t = S.track; if (!t) return;
  const v = t.validation || {};
  $("dlgValidation").textContent = [v.text, v.timing === "not verified" ? "Timing: not verified." : ""].filter(Boolean).join(" ");
  $("dlgNights").replaceChildren(...t.nights.map((n) => {
    const div = document.createElement("div"), inFile = n.included !== false && n.nAcc >= 3;
    div.className = inFile ? "" : "excluded";
    div.textContent = `${n.night}: ${n.nAcc} accepted of ${n.nTot}` + (inFile ? "" : " — not in the MPC draft (fewer than 3 accepted that night)");
    return div;
  }));
  const goodNight = t.nights.some((n) => n.included !== false && n.nAcc >= 3);
  const why = t.regime === "between_epochs" ? ADES_BLOCKED_BETWEEN
    : t.regime !== "within_visit" ? `The server did not classify this motion as within one visit (${t.regime ?? "unknown"}); use the CSV.`
    : !goodNight ? "The MPC asks for at least three observations per night; no night has three accepted measurements. Use the CSV." : "";
  $("dlgAdes").disabled = !!why; $("dlgWhy").textContent = why;
  $("dlgStatus").textContent = ""; $("dlgStatus").className = "";
  $("reportDlg").showModal();
}
async function downloadReport(format) {
  const t = S.track, st = $("dlgStatus");
  const submitter = $("dlgSubmitter").value.trim(), trksub = $("dlgTrksub").value.trim();
  if (format === "ades" && !submitter) { st.textContent = "Enter the submitter name (for example I. M. Lastname)."; st.className = "err"; return; }
  st.textContent = "Preparing the file…"; st.className = "";
  try {
    let blob, name;
    if (MOCK) { // developer mock: no server, so the fixture itself is saved, clearly named
      blob = new Blob([JSON.stringify({ mock: true, format, submitter, trksub, track: t }, null, 1)], { type: "application/json" });
      name = `MOCK_${format}_report.json`;
    } else {
      const q = new URLSearchParams({ id: t.id, format });
      if (format === "ades") { q.set("submitter", submitter); if (trksub) q.set("trksub", trksub); }
      const r = await fetch(`${API}/api/report?${q}`);
      if (!r.ok) { let msg = `${r.status}`; try { msg = (await r.json()).message || msg; } catch { /* not json */ } throw new Error(msg); }
      blob = await r.blob();
      const cd = r.headers.get("Content-Disposition") || "";
      name = (cd.match(/filename\*?=(?:UTF-8'')?"?([^";]+)"?/i) || [])[1] || `spherex_${format}.txt`; // name chosen by the server
    }
    const a = Object.assign(document.createElement("a"), { href: URL.createObjectURL(blob), download: decodeURIComponent(name) });
    document.body.append(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(a.href), 5000);
    st.textContent = `Downloaded ${decodeURIComponent(name)}.`;
  } catch (e) { st.textContent = e.message; st.className = "err"; } // server messages are shown as they are
}

// visible label on a low-coverage frame (only reachable with "show low-coverage frames" on)
function lowLabel(ctx) {
  const bands = S.mode === "rgb" ? [...new Set(Object.values(rgbBands()))] : [S.band];
  const low = bands.filter((b) => isLow(b, S.epoch));
  if (!low.length) return;
  const txt = `low coverage: ${low.map((b) => `${bandEpoch(b, S.epoch)?.n_used ?? "?"} exposures` + (bands.length > 1 ? ` (${b})` : "")).join(", ")}`;
  ctx.font = "bold 13px system-ui";
  const w = ctx.measureText(txt).width + 12, x = (ctx.canvas.width - w) / 2;
  ctx.fillStyle = "rgba(0,0,0,0.75)"; ctx.fillRect(x, 6, w, 22);
  ctx.fillStyle = "#ffcc66"; ctx.fillText(txt, x + 6, 22);
}

function toScreen(ra, dec) {
  const [x, y] = world2pix(ra, dec), z = S.zoom, ny = S.m.grid.shape[0];
  return [(x + 0.5) * z, (ny - 1 - y + 0.5) * z];
}

function overlays(ctx, ep) {
  const z = S.zoom;
  ctx.lineWidth = 1.5;
  if ($("ovGaia").checked && S.gaia) {
    ctx.strokeStyle = "#4fd1c5";
    for (const g of S.gaia) {
      const dt = (ep.mjd_mid - mjdOfYear(g.ref_epoch ?? 2016)) / 365.25;
      const dec = g.dec + ((g.pmdec || 0) * dt) / 3.6e6;
      const ra = g.ra + ((g.pmra || 0) * dt) / 3.6e6 / Math.cos(g.dec * D2R);
      const [sx, sy] = toScreen(ra, dec);
      ctx.beginPath(); ctx.arc(sx, sy, Math.max(4, 1.2 * z * (1.5 + (19 - (g.g_mag ?? 19)) / 3)), 0, 2 * Math.PI); ctx.stroke();
    }
  }
  if ($("ovTrack").checked && S.m.target_track?.length) {
    const pts = S.m.target_track.map((t) => [t.epoch, ...toScreen(t.ra, t.dec)]);
    // marks must not cover the source: small dots for other epochs, open ticks around the current one
    ctx.strokeStyle = ctx.fillStyle = "#e89a3c";
    for (const [e, x, y] of pts) {
      if (e === ep.id) {
        const a = 2.5 * z, b = 5 * z;
        ctx.beginPath();
        ctx.moveTo(x - b, y); ctx.lineTo(x - a, y); ctx.moveTo(x + a, y); ctx.lineTo(x + b, y);
        ctx.moveTo(x, y - b); ctx.lineTo(x, y - a); ctx.moveTo(x, y + a); ctx.lineTo(x, y + b);
        ctx.stroke();
      } else { ctx.beginPath(); ctx.arc(x, y, Math.max(1.5, 0.4 * z), 0, 2 * Math.PI); ctx.fill(); }
    }
  }
  if ($("ovQuery").checked && S.m.query) {
    const [x, y] = toScreen(S.m.query.ra, S.m.query.dec), r = 6 * z;
    ctx.strokeStyle = "#ff5c8a";
    ctx.beginPath(); ctx.moveTo(x - r, y); ctx.lineTo(x - r / 3, y); ctx.moveTo(x + r / 3, y); ctx.lineTo(x + r, y);
    ctx.moveTo(x, y - r); ctx.lineTo(x, y - r / 3); ctx.moveTo(x, y + r / 3); ctx.lineTo(x, y + r); ctx.stroke();
  }
  if (S.specAt && S.specAt !== "target") {
    const [x, y] = toScreen(...S.specAt);
    ctx.strokeStyle = "#fff"; ctx.strokeRect(x - 3 * z, y - 3 * z, 6 * z, 6 * z);
  }
  // scale bar (30″) and compass: north up, east left
  const len = (30 / pixArcsec()) * z, H = ctx.canvas.height;
  ctx.font = "12px system-ui";
  // dark outline so the labels stay readable on a white (difference) background
  const label = (t, x, y) => { ctx.lineWidth = 3; ctx.strokeStyle = "#000"; ctx.strokeText(t, x, y); ctx.fillStyle = "#eee"; ctx.fillText(t, x, y); };
  ctx.lineWidth = 4; ctx.strokeStyle = "#000"; ctx.beginPath(); ctx.moveTo(10, H - 12); ctx.lineTo(10 + len, H - 12); ctx.stroke();
  ctx.lineWidth = 1.5; ctx.strokeStyle = "#eee"; ctx.beginPath(); ctx.moveTo(10, H - 12); ctx.lineTo(10 + len, H - 12); ctx.stroke();
  label(`30″  (grid ${pixArcsec().toFixed(2)}″/px)`, 10, H - 18);
  label("N ↑  E ←", ctx.canvas.width - 64, 16);
  drawMarks(ctx);
}

// ---------- plots (spectrum + light curve) ----------
const EPOCH_COLORS = ["#4fd1c5", "#e89a3c", "#c084fc", "#7ee787", "#ff7b72", "#79c0ff"];
function scatter(svg, pts, xk, yk, xlabel, ylabel) {
  const W = 460, H = 220, L = 52, B = 30, T = 24, R = 10; // T leaves a row above the plot for the epoch legend
  if (!pts.length) { svg.innerHTML = `<text x="20" y="30" fill="#888" font-size="13">no data</text>`; return; }
  const xs = pts.map((p) => p[xk]), good = pts.filter((p) => !p.flagged);
  const ys = (good.length ? good : pts).map((p) => p[yk]);
  const [x0, x1] = [Math.min(...xs), Math.max(...xs)], [y0, y1] = percentile([Float32Array.from(ys)], [1, 99]);
  const sx = (x) => L + ((x - x0) / (x1 - x0 || 1)) * (W - L - R);
  const sy = (y) => H - B - ((y - y0) / (y1 - y0 || 1)) * (H - B - T);
  let h = `<line x1="${L}" y1="${H - B}" x2="${W - R}" y2="${H - B}" stroke="#555"/><line x1="${L}" y1="${T}" x2="${L}" y2="${H - B}" stroke="#555"/>`;
  h += `<text x="${L}" y="${H - 8}" fill="#888" font-size="11">${x0.toFixed(2)}</text><text x="${W - R}" y="${H - 8}" fill="#888" font-size="11" text-anchor="end">${x1.toFixed(2)} ${xlabel}</text>`;
  h += `<text x="4" y="${T + 10}" fill="#888" font-size="11">${y1.toPrecision(3)}</text><text x="4" y="${H - B}" fill="#888" font-size="11">${y0.toPrecision(3)}</text>`;
  h += `<text x="4" y="${H / 2}" fill="#888" font-size="11">${ylabel}</text>`;
  for (const p of pts) {
    const c = EPOCH_COLORS[(p.epoch ?? 0) % EPOCH_COLORS.length];
    // faint: flagged, or (psf_deblended) consistent with zero given the neighbour residual (|flux| < 0.15 × neighbor_flux)
    const faint = (p.flagged && !p.overflow) || (p.neighbor_flux > 0 && Math.abs(p[yk]) < 0.15 * p.neighbor_flux);
    const cx = sx(p[xk]).toFixed(1), cy = Math.max(T, Math.min(H - B, sy(p[yk])));
    if (p.saturated && yk === "flux") { // lower limit: open marker with an upward tick
      h += `<g stroke="${c}" fill="none" opacity="0.7"><circle cx="${cx}" cy="${cy.toFixed(1)}" r="2.6"/>` +
        `<path d="M${cx} ${(cy - 2.6).toFixed(1)}v-7m-2.5 2.5l2.5-2.5 2.5 2.5"/></g>`;
    } else if (p.overflow && yk === "flux") { // recovered from overflow: a valid measurement, filled diamond
      h += `<path d="M${cx} ${(cy - 3.2).toFixed(1)}l3.2 3.2-3.2 3.2-3.2-3.2z" fill="${c}" opacity="${faint ? 0.2 : 1}"/>`;
    } else {
      h += `<circle cx="${cx}" cy="${cy.toFixed(1)}" r="2.2" fill="${c}" opacity="${faint ? 0.2 : 1}"/>`;
    }
  }
  // epoch legend in its own row above the plot, so it never covers data points
  const step = Math.min(68, (W - L - R) / Math.max(1, S.m.epochs.length));
  h += S.m.epochs.map((e, i) => `<text x="${(L + i * step).toFixed(0)}" y="12" fill="${EPOCH_COLORS[e.id % EPOCH_COLORS.length]}" font-size="10">${e.date}</text>`).join("");
  svg.innerHTML = h;
}
function plots() {
  const raw = S.spectrum;
  const pts = (Array.isArray(raw) ? raw : raw?.points || []).filter((p) => Number.isFinite(p.flux));
  const meta = raw?.meta || {}, unit = meta.flux_unit || "flux";
  $("specWhere").textContent = S.specAt === "target" ? "(target position)" : S.specAt ? `(${S.specAt.map((v) => v.toFixed(5)).join(", ")})` : "";
  scatter($("spec"), pts, "wavelength_um", "flux", "µm", unit); // flagged points drawn faint
  // light curve: selected band's wavelength range only (mixing wavelengths turns the spectrum into fake scatter)
  const w = S.m?.bands?.find((b) => b.id === S.band)?.wave_um;
  const inBand = (p) => !w || (p.wavelength_um >= w[0] && p.wavelength_um <= w[1]);
  $("lcWhere").textContent = w ? `(${S.band}, ${w[0]}–${w[1]} µm)` : "";
  // light curve: valid points only — flagged and saturated out, recovered-from-overflow points in
  const usable = (p) => !p.saturated && (!p.flagged || p.overflow);
  scatter($("lc"), pts.filter((p) => usable(p) && inBand(p)), "mjd", "flux", "MJD", unit);
  // saturated stars: relative brightness from the PSF wings (MJy/sr) — its own plot, never on the mJy axis
  const wing = pts.filter((p) => Number.isFinite(p.wing_flux));
  $("wingBox").hidden = !wing.length;
  if (wing.length) {
    const wbs = meta.wing_bands || {}, cur = wbs[S.band];
    // the selected band has no wing series: say so and offer the bands that have one (never switch by itself)
    const missing = Object.keys(wbs).length > 0 && !cur;
    $("wingMissing").hidden = !missing; $("wingPlot").hidden = missing;
    if (missing) {
      const links = Object.keys(wbs).map((b) => Object.assign(document.createElement("a"),
        { href: "#", textContent: b, title: `switch to ${b}`, onclick: (e) => { e.preventDefault(); selectBand(b); } }));
      $("wingMissing").replaceChildren(`No PSF-wing series in ${S.band}. Available in: `,
        ...links.flatMap((a, i) => (i ? [", ", a] : [a])));
      $("wingWhere").textContent = `(${S.band})`;
    } else {
      const wb = wing.filter(inBand);
      $("wingWhere").textContent = `(${S.band}; ${wb.length} of ${wing.length} saturated exposures)`;
      scatter($("wing"), wb, "mjd", "wing_flux", "MJD", "MJy/sr");
    }
    // meta.wing (method text, from the co-add session) verbatim, in a "Method details" section closed by default
    const mw = meta.wing;
    const method = mw == null ? "" : typeof mw === "string" ? mw
      : mw.text ?? Object.entries(mw).map(([k, v]) => `${k}: ${typeof v === "object" ? JSON.stringify(v) : v}`).join(" · ");
    $("wingMethod").hidden = !method; $("wingMethodText").textContent = method;
    // meta.wing_bands[band] = {ratio, threshold, significant}: the selected band first, then a short summary
    const fx = (v) => `×${(+v).toFixed(2)}`;
    // neutral comparison: the wing series is consistent with variability, it does not prove it
    // (meta.wing_bands[].significant only picks one of the two phrases; the word itself is never shown)
    const cmp = (v) => (v.significant ? "exceeds the bright-star scatter" : "within the bright-star scatter");
    const line = cur ? `${S.band}: ${fx(cur.ratio)} between epochs; bright control stars reach ${fx(cur.threshold)} — ${cmp(cur)}` : "";
    const summary = Object.keys(wbs).length ? "All bands: " + Object.entries(wbs).map(([b, v]) =>
      `${b} ${fx(v.ratio)} (${cmp(v)})`).join(", ") : "";
    $("wingNote").replaceChildren(...[line, summary].filter(Boolean).map((t) => Object.assign(document.createElement("div"), { textContent: t })));
  }
  const nf = pts.filter((p) => p.flagged).length, ns = pts.filter((p) => p.saturated).length;
  const no = pts.filter((p) => p.overflow && !p.saturated).length;
  const nz = pts.filter((p) => !p.flagged && p.neighbor_flux > 0 && Math.abs(p.flux) < 0.15 * p.neighbor_flux).length;
  // {source_id: string|null, g_mag: number|null, ra, dec}; source_id stays a string (64-bit id)
  const nb = [].concat(meta.deblended_neighbors || []).map((n) => typeof n !== "object" || !n ? String(n)
    : n.source_id != null ? `Gaia DR3 ${n.source_id}${n.g_mag != null ? ` (G = ${(+n.g_mag).toFixed(1)})` : ""}`
    : `Gaia DR3 star at RA ${(+n.ra).toFixed(5)}, Dec ${(+n.dec).toFixed(5)}`);
  $("specNote").textContent = [
    meta.photometry && `photometry: ${meta.photometry}`,
    meta.photometry === "psf_deblended" && `neighbour removed by PSF fit${nb.length ? ": " + nb.join(", ") : ""}`,
    nz && `${nz} points consistent with zero (|flux| < 0.15 × removed neighbour flux, drawn faint)`,
    ns && `${ns} saturated (lower limits: open marker with arrow, not in the light curve)`,
    no && `${no} recovered from overflow (reduced accuracy; filled diamond, kept in the light curve)`,
    meta.bands_pending?.length && `bands still being built (not in this spectrum yet): ${meta.bands_pending.join(", ")}`,
    meta.method && `method: ${meta.method}`,
    meta.aperture_radius_px != null && `aperture ${meta.aperture_radius_px} px` +
      (meta.background_annulus_px ? `, background annulus ${[].concat(meta.background_annulus_px).join("–")} px` : ""),
    meta.aperture_corrected === false && "fluxes are NOT aperture-corrected",
    `${pts.length} points, ${nf} flagged (faint in the spectrum, left out of the light curve)`,
  ].filter(Boolean).join(" · ");
}

// ---------- UI ----------
function status(t, err = false) { $("status").textContent = t; $("status").className = err ? "err" : ""; }

function renderBands() {
  $("bands").replaceChildren(...S.m.bands.map((b, i) => {
    const btn = document.createElement("button");
    const loaded = S.m.epochs.filter((e) => frameOf(b.id, e.id)).length;
    const pct = b.status === "pending" && b.progress != null ? ` ${Math.round(b.progress * 100)}%` : "";
    btn.innerHTML = `${b.id}<small>${b.label || ""}</small><small>${b.status}${pct} · ${loaded}/${S.m.epochs.length}</small>`;
    btn.title = `exposures per epoch: ${(b.n_exposures || []).join(", ")}  (key ${i + 1})`;
    btn.className = b.id === S.band ? "on" : "";
    btn.disabled = b.status === "empty";
    btn.onclick = () => selectBand(b.id);
    return btn;
  }));
}

function selectBand(id) {
  if (id === S.band) return;
  S.prevBand = S.band; S.band = id; S.userScale = null;
  $("swapBand").textContent = `⇄ ${S.prevBand} (t)`; $("swapBand").disabled = false;
  fixEpoch(); renderBands(); draw(); syncUrl(); plots();
  loadBand(id).then(draw);
}

function renderMeta() {
  const m = S.m, rows = [
    ["Name / key", `${m.name || ""} (${m.key})`],
    ["Query", m.query ? `RA ${m.query.ra}  Dec ${m.query.dec}  FOV ${(+m.query.fov_deg).toFixed(4)}°` : "—"],
    ["Grid", m.grid ? `${m.grid.shape.join("×")} px, ${pixArcsec().toFixed(2)}″/px (SPHEREx native 6.15″/px)` : "—"],
    ["Collections", (m.collections || []).join(", ")],
    ["Data range", m.data_range ? `MJD ${m.data_range.mjd_min} – ${m.data_range.mjd_max}` : "—"],
    ["Target position source", m.target_source || "—"],
    ["Processing", m.processing || "—"],
    ["Acknowledgement", m.acknowledgement || "—"],
  ];
  $("meta").innerHTML = "<dl>" + rows.map(([k, v]) => `<dt>${k}</dt><dd>${escapeHtml(String(v))}</dd>`).join("") + "</dl>";
}
const escapeHtml = (s) => s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);

function setScaleInputs(s) {
  $("vmin").value = (+s.vmin).toPrecision(4); $("vmax").value = (+s.vmax).toPrecision(4); $("soft").value = (+s.soft).toPrecision(4);
}

// ---------- field of view (3′ / 7′ / 15′) ----------
const FOVS = [[3, "3′"], [7, "7′"], [15, "15′"]];
function renderFov() {
  const cur = S.m?.query?.fov_deg, max = S.m?.max_fov_deg ?? S.health?.max_fov_deg;
  $("fovButtons").replaceChildren(Object.assign(document.createElement("span"), { className: "muted", textContent: "field" }),
    ...FOVS.map(([am, label]) => {
      const deg = am / 60, b = document.createElement("button");
      b.type = "button"; b.textContent = label;
      b.className = cur && Math.abs(cur * 60 - am) < 0.6 ? "on" : "";
      let why = "";
      if (!API) why = `the gallery is fixed at ${cur ? (cur * 60).toFixed(1) + "′" : "its field"}; another field needs the live server, which is not reachable right now`;
      else if (max && deg > max + 1e-9) why = `this server allows at most ${(max * 60).toFixed(1)}′ (max_fov_deg ${(+max).toFixed(4)}°)`;
      b.disabled = !!why;
      b.title = why || `${label} field: larger fields catch faster movers but take longer to load`;
      b.onclick = () => changeFov(deg);
      return b;
    }));
}
async function changeFov(deg) {
  const fov = String(Math.floor(deg * 1e6) / 1e6); // never round up past the server limit (7′ = max_fov_deg)
  const keep = { band: S.band, mode: S.mode, zoom: S.zoom };
  const t = params.get("target");
  if (t) { // a gallery target becomes a free field: resolve it so the server still centres on the moving object
    try {
      const r = await fetchLogged(`${API}/api/resolve?name=${enc(S.m.name || t)}`);
      navigate({ ra: r.ra, dec: r.dec, fov, name: r.name, pmra: r.pmra, pmdec: r.pmdec, ref_epoch: r.ref_epoch, ...keep });
    } catch (e) { status(`Could not resolve ${S.m.name || t} for a new field: ${e.message}`, true); }
    return;
  }
  navigate({ ra: params.get("ra"), dec: params.get("dec"), fov, name: params.get("name"), pmra: params.get("pmra"),
    pmdec: params.get("pmdec"), ref_epoch: params.get("ref_epoch"), ...keep });
}

// ---------- links, embed ----------
function viewUrl(changes = {}) {
  const u = new URL(location.href);
  for (const [k, v] of Object.entries(changes)) (v == null ? u.searchParams.delete(k) : u.searchParams.set(k, v));
  return u.toString();
}
// external tools; only URL formats read from the tools themselves (see README)
// WiseView reads its state from the URL hash on load (checked in a browser on 14 Nov 2026: locInput = "ra dec",
// bandInput = band); the other values are the defaults WiseView writes into the hash itself.
const WISEVIEW_DEFAULTS = "size=176&band=2&speed=500&minbright=-50.0000&maxbright=500.0000&window=0.5&diff_window=1" +
  "&linear=1&color=&zoom=9&border=0&gaia=0&invert=1&maxdyr=0&scandir=0&neowise=0&diff=0&outer_epochs=0" +
  "&unique_window=1&smooth_scan=0&shift=0&pmra=0&pmdec=0";
function externalLinks(ra, dec) {
  return [
    ["Open in WiseView", `http://byw.tools/wiseview#ra=${ra.toFixed(6)}&dec=${dec.toFixed(6)}&${WISEVIEW_DEFAULTS}`,
      "unWISE/NEOWISE blink at this position (WiseView defaults: W2, size 176)"],
    ["Open in IRSA", "https://irsa.ipac.caltech.edu/applications/spherex/",
      "IRSA SPHEREx Data Explorer: opens its start page (no documented URL that opens it at a position)"],
  ];
}
function updateLinks() {
  if (!S.m?.query) return;
  const { ra, dec } = S.m.target_track?.at(-1) || S.m.query; // where the object is now, not the catalogue position
  $("links").replaceChildren(...externalLinks(+ra, +dec).map(([textContent, href, title]) =>
    Object.assign(document.createElement("a"), { textContent, href, title, target: "_blank", rel: "noopener" })));
  $("fullView").href = viewUrl({ embed: null });
  $("ackLine").textContent = S.m.acknowledgement || "";
  $("copyEmbed").disabled = false;
}
async function copyEmbed() {
  const code = `<iframe src="${viewUrl({ embed: 1 })}" width="760" height="560" style="border:0" loading="lazy" ` +
    `title="SPHEREx Blink: ${escapeHtml(S.m?.name || S.key || "")}"></iframe>`;
  try { await navigator.clipboard.writeText(code); status("Embed code copied to the clipboard."); }
  catch { status(`Copy this embed code: ${code}`); }
}

function setMode(mode) {
  S.mode = mode; $("mode").value = mode; S.userScale = null;
  if (mode === "rgb" && S.layer !== "image") { S.layer = "image"; $("layer").value = "image"; } // colour uses image frames
  $("rgbBox").hidden = mode !== "rgb";
  draw(); syncUrl(); loadRGB();
}

function syncUrl() {
  const p = new URLSearchParams(location.search);
  p.set("band", S.band); p.set("mode", S.mode); p.set("frame", S.epoch); p.set("zoom", S.zoom);
  if (S.layer !== "image") p.set("layer", S.layer); else p.delete("layer");
  if (S.mode === "rgb" && S.m?.bands) { const c = rgbBands(); p.set("rgb", [c.r, c.g, c.b].join(",")); } else p.delete("rgb");
  if (S.showLow) p.set("low", "1"); else p.delete("low");
  history.replaceState(null, "", "?" + p.toString());
  updateLinks();
}

function step(d) {
  if (!S.m?.epochs?.length) return;
  const n = S.m.epochs.length;
  let e = S.epoch;
  for (let k = 0; k < n; k++) { e = (e + d + n) % n; if (playable(S.band, e)) break; } // skip low-coverage and empty frames
  S.epoch = e;
  draw(); syncUrl();
}
function setPlaying(on) {
  S.playing = on; $("play").textContent = on ? "Pause" : "Play";
  clearInterval(S.timer);
  if (on) S.timer = setInterval(() => step(1), 1000 / +$("speed").value);
}

function wire() {
  $("mode").value = S.mode; $("layer").value = S.layer; $("zoom").value = String(S.zoom);
  $("coordForm").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target), name = (f.get("name") || "").trim();
    if (!name) { navigate({ ra: f.get("ra"), dec: f.get("dec"), fov: f.get("fov") }); return; }
    if (!(await serverReady)) { // no live server: match a gallery entry by key or name
      const norm = (s) => String(s).toLowerCase().replace(/[^a-z0-9]/g, "");
      const hit = [...$("targets").children].find((b) => norm(b.dataset.key) === norm(name) || norm(b.textContent) === norm(name));
      if (hit) navigate({ target: hit.dataset.key });
      else status(`"${name}" is not one of the five gallery targets, and the live server is not reachable right now to look it up.`, true);
      return;
    }
    status(`Resolving "${name}"…`);
    try {
      const r = await fetchLogged(`${API}/api/resolve?name=${enc(name)}`);
      // keep the proper motion in the URL so the page can draw the expected position at each epoch
      navigate(r.target ? { target: r.target } : { ra: r.ra, dec: r.dec, fov: f.get("fov"), name: r.name,
        pmra: r.pmra, pmdec: r.pmdec, ref_epoch: r.ref_epoch });
    } catch (err) { status(`Could not resolve "${name}": ${err.message}`, true); }
  };
  $("play").onclick = () => setPlaying(!S.playing);
  $("copyEmbed").onclick = copyEmbed;
  $("swapBand").onclick = () => S.prevBand && selectBand(S.prevBand);
  $("speed").oninput = () => S.playing && setPlaying(true);
  $("epoch").oninput = (e) => {
    const v = +e.target.value, prev = S.epoch;
    S.epoch = v;
    if (!allowed(S.band, v)) { S.epoch = prev; step(v > prev ? 1 : -1); return; } // a skipped frame: move past it
    draw(); syncUrl();
  };
  $("showLow").checked = S.showLow;
  $("showLow").onchange = (e) => { S.showLow = e.target.checked; status(""); fixEpoch(); draw(); syncUrl(); };
  $("mode").onchange = (e) => setMode(e.target.value);
  $("layer").onchange = (e) => {
    S.layer = e.target.value; S.userScale = null;
    if (S.m) S.m.epochs.forEach((ep, i) => loadFrame(S.band, ep.id, S.layer, emptyEpoch(S.band, i)));
    draw(); syncUrl();
  };
  $("zoom").onchange = (e) => { S.zoom = +e.target.value; draw(); syncUrl(); };
  for (const id of ["vmin", "vmax", "soft"]) $(id).onchange = () => {
    S.userScale = { vmin: +$("vmin").value, vmax: +$("vmax").value, soft: +$("soft").value }; draw();
  };
  $("autoScale").onclick = () => { S.userScale = null; draw(); };
  for (const id of ["ovQuery", "ovTrack", "ovGaia"]) $(id).onchange = draw;

  const cv = $("canvas");
  const gridAt = (ev) => {
    const r = cv.getBoundingClientRect(), k = cv.width / r.width;
    const x = ((ev.clientX - r.left) * k) / S.zoom - 0.5, y = S.m.grid.shape[0] - 1 - (((ev.clientY - r.top) * k) / S.zoom - 0.5);
    return [x, y];
  };
  cv.onmousemove = (ev) => {
    if (!S.m?.grid) return;
    const [x, y] = gridAt(ev), [ra, dec] = pix2world(x, y), [ny, nx] = S.m.grid.shape;
    const xi = Math.round(x), yi = Math.round(y);
    const inside = xi >= 0 && yi >= 0 && xi < nx && yi < ny;
    const fmt = (v) => (v === Infinity ? "saturated" : Number.isFinite(v) ? v.toPrecision(4) : "no data");
    const head = `x ${xi}  y ${yi}   RA ${ra.toFixed(5)}  Dec ${dec.toFixed(5)}\n`;
    if (S.mode === "rgb") {
      const c = rgbBands(), ep = S.m.epochs[S.epoch].id;
      $("readout").textContent = head + [["R", c.r], ["G", c.g], ["B", c.b]].map(([k, b]) => {
        const f = frameOf(b, ep, "image"), raw = f && inside ? f[yi * nx + xi] : NaN;
        const other = inside && raw !== Infinity && satHere(satMask(b), yi * nx + xi, f, bandScale(b).vmax);
        return `${k} ${b} ${fmt(raw)}${other ? " (saturated in another epoch)" : ""}`;
      }).join("   ");
      return;
    }
    // the real value of this epoch's file; the saturation union only changes how it is drawn
    const i = yi * nx + xi, epId = S.m.epochs[S.epoch].id;
    const a = displayArray(), shown = a && inside ? a[i] : NaN;
    const raw = S.mode === "blink" && S.layer === "image" ? frameOf(S.band, epId)?.[i] ?? NaN : shown;
    const own = frameOf(S.band, epId);
    const other = S.layer === "image" && inside && own?.[i] !== Infinity && satHere(satMask(S.band), i, own, bandScale(S.band).vmax);
    const w = frameOf(S.band, epId, "weight"), fw = w && inside ? w[i] : null;
    $("readout").textContent = head + `${S.layer} value ${fmt(inside ? raw : NaN)}` +
      (other ? " (saturated in another epoch)" : "") + (fw != null ? `   weight ${fw}` : "");
  };
  // measure mode: a click adds a mark, a drag moves the mark under the cursor
  $("measureBtn").onclick = () => setMeasuring(!S.measuring);
  $("clearMarks").onclick = () => { S.marks = []; renderMarks(); draw(); };
  $("refineBtn").onclick = refine;
  $("reportBtn").onclick = openReport;
  $("dlgCsv").onclick = () => downloadReport("csv");
  $("dlgAdes").onclick = () => downloadReport("ades");
  $("dlgClose").onclick = () => $("reportDlg").close();
  updateRefineButtons();
  setMeasuring(false);
  let drag = null, dragged = false, downOnMark = false;
  const markAt = (ev) => {
    const r = cv.getBoundingClientRect(), k = cv.width / r.width, x = (ev.clientX - r.left) * k, y = (ev.clientY - r.top) * k;
    return S.marks.find((m) => { const p = toScreen(m.ra, m.dec); return Math.hypot(p[0] - x, p[1] - y) < Math.max(8, 2.5 * S.zoom); });
  };
  cv.addEventListener("mousedown", (ev) => { if (S.measuring && S.m?.grid) { drag = markAt(ev) || null; downOnMark = !!drag; dragged = false; } });
  cv.addEventListener("mousemove", (ev) => {
    if (!drag) return;
    [drag.ra, drag.dec] = pix2world(...gridAt(ev)); dragged = true; draw();
  });
  window.addEventListener("mouseup", () => { if (drag && dragged) renderMarks(); drag = null; });
  cv.onclick = (ev) => {
    if (!S.m?.grid) return;
    if (S.measuring) { if (!dragged && !downOnMark) addMark(...pix2world(...gridAt(ev))); dragged = false; downOnMark = false; return; }
    const [ra, dec] = pix2world(...gridAt(ev));
    if (!API) { status("A spectrum at a clicked point needs the live server, which is not reachable right now; the gallery has the target's spectrum."); return; }
    S.specAt = [ra, dec]; draw();
    // the live server measures any point, also for a gallery target opened from static files (same key)
    fetchLogged(`${API}/api/spectrum?key=${enc(S.key)}&ra=${ra.toFixed(6)}&dec=${dec.toFixed(6)}`)
      .then((s) => { S.spectrum = s; plots(); }).catch((e) => status(`Spectrum failed: ${e.message}`, true));
  };

  document.addEventListener("keydown", (e) => {
    if (e.target.closest?.("input, select, textarea")) return;
    if (e.key === " ") { e.preventDefault(); setPlaying(!S.playing); }
    else if (e.key === "ArrowRight") step(1);
    else if (e.key === "ArrowLeft") step(-1);
    else if (/^[1-9]$/.test(e.key) && S.m?.bands?.[+e.key - 1]) selectBand(S.m.bands[+e.key - 1].id);
    else if (e.key === "t" && S.prevBand) selectBand(S.prevBand);
    else if (e.key === "m") setMeasuring(!S.measuring);
    else if (e.key === "b" || e.key === "d" || e.key === "s" || e.key === "c") setMode({ b: "blink", d: "diff", s: "sum", c: "rgb" }[e.key]); else if (e.key === "+" || e.key === "-") {
      const zs = [1, 2, 3, 4, 6], i = zs.indexOf(S.zoom);
      S.zoom = zs[Math.max(0, Math.min(zs.length - 1, (i < 0 ? 2 : i) + (e.key === "+" ? 1 : -1)))];
      $("zoom").value = String(S.zoom); draw(); syncUrl();
    }
  });
}

if (params.get("embed") === "1") document.body.classList.add("embed");
if (params.get("ra")) { const f = $("coordForm"); f.ra.value = params.get("ra"); f.dec.value = params.get("dec"); f.fov.value = params.get("fov") || "0.1"; }
// header: which source the page uses (gallery files, live server or both)
function renderSource(probing) {
  const h = S.health, srv = API ? `live server ${API}` + (h ? ` (${h.version ?? ""} ${h.commit ?? ""}, max field ${(+h.max_fov_deg * 60).toFixed(1)}′)` : "") : "";
  $("source").textContent = "source: " + (FORCED ? `${srv} (all targets)`
    : [`static gallery ${DATA}/`, probing ? "looking for the live server…" : srv || "live server not reachable"].join(" · "));
}
const serverReady = probeServer(); // one quick check; nothing below waits for it except free positions and names
renderSource(true);
serverReady.then((ok) => {
  renderSource(false); renderFov(); updateRefineButtons();
  if (ok && S.health?.max_fov_deg) {
    const fov = $("coordForm").fov;
    fov.max = S.health.max_fov_deg; fov.title = `max ${(+S.health.max_fov_deg).toFixed(4)}° on this server`;
  }
  if (FORCED && !ok) status(`Server ${API} is not reachable; without ?api= the page shows the five gallery targets.`, true);
});
wire();
loadIndex();
loadTarget();
