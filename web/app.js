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

// ---------- user wavelength windows (F1): band id w<lo>_<hi> in nm, built by the live server on request ----------
const isCustom = (id) => /^w\d+_\d+$/.test(id || "");
// ?winmock=1 (developer, until the server builds windows): a window is served as the target's "W" band
const WINMOCK = params.get("winmock") === "1";
const bandPath = (band) => (WINMOCK && isCustom(band) ? "W" : band);
const viaApi = (band) => (USE_API || isCustom(band)) && !!API && !(WINMOCK && !USE_API);

// ---------- data source: one code path for static files and API (USE_API decides per target) ----------
const src = {
  index: () => `${DATA}/index.json`,
  manifest: (q) => USE_API
    ? `${API}/api/manifest?` + new URLSearchParams(q.target ? { target: q.target } : Object.fromEntries(
      Object.entries({ ra: q.ra, dec: q.dec, fov: q.fov, pmra: q.pmra, pmdec: q.pmdec, ref_epoch: q.ref_epoch, name: q.name })
        .filter(([, v]) => v != null && v !== ""))) // optional pm/name: the server then builds target_track
    : q.key || q.target ? `${DATA}/${q.key || q.target}/manifest.json` : null,
  // a user window always comes from the live server, also for a gallery target opened from static files
  band: (key, band) => viaApi(band) ? `${API}/api/band?key=${enc(key)}&band=${enc(bandPath(band))}` : `${DATA}/${key}/${bandPath(band)}.json`,
  frame: (key, epoch, band, kind) => viaApi(band)
    ? `${API}/api/frame?key=${enc(key)}&epoch=${epoch}&band=${enc(bandPath(band))}` + (kind === "image" ? "" : `&kind=${kind}`)
    : `${DATA}/${key}/${epoch}_${bandPath(band)}${kind === "image" ? "" : "_" + kind}.f32`,
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
  if (isCustom(want)) addCustomBand(want); // a shared address with band=w4300_5000 opens the same window
  S.band = (S.m.bands.find((b) => b.id === want) ||S.m.bands.find((b) => b.id === S.m.default_band) || ready[0] || S.m.bands[0])?.id;
  const rgbq = (params.get("rgb") || "").split(",");
  if (rgbq.length === 3 && rgbq.every((b) => S.m.bands.some((x) => x.id === b))) S.rgb = { r: rgbq[0], g: rgbq[1], b: rgbq[2] };
  setupRGB();
  renderBands(); renderFov(); updateLinks();
  if (S.mode === "rgb") loadRGB();
  if (!params.get("zoom") && S.m.grid.shape[1] > 200) { S.zoom = 2; $("zoom").value = "2"; } // big grid on screen
  const bigField = USE_API && (+S.m.query?.fov_deg || 0) > 0.2;
  status(bigField
    ? `${S.m.epochs.length} epochs, ${S.m.bands.length} bands. ${(+S.m.query.fov_deg * 60).toFixed(0)}′ field: the server reads about 120 MB ` +
      `per band from the archive, so the first band (${S.band}) usually takes 30–70 s; the other bands follow.`
    : `${S.m.epochs.length} epochs, ${S.m.bands.length} bands. Loading ${S.band}…`);

  fetchLogged(src.gaia(S.key)).then((g) => { S.gaia = g; draw(); }).catch(() => { S.gaia = []; });
  loadCandidates().then(draw);
  loadCatwise().then(draw);
  loadWise().then(() => { draw(); syncUrl(); });
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
    // a user window needs the live server: when the page opens straight on band=w…, wait for the server check
    const ask = isCustom(band) && !API && !WINMOCK
      ? serverReady.then((ok) => (ok ? fetchLogged(src.band(S.key, band))
        : Promise.reject(new Error("Your own wavelength window needs the live server, which is not reachable right now."))))
      : fetchLogged(src.band(S.key, band));
    const p = ask.then(
      (j) => { S.bandInfo.set(band, j); if (isCustom(band)) customBandLoaded(band, j); return j; },
      (e) => { S.bandInfo.delete(band); if (isCustom(band)) customBandFailed(band, e); return null; });
    S.bandInfo.set(band, p);
  }
  // frames after the band file: an epoch with n_used 0 has no file, so it is not requested (no 404 noise)
  return Promise.resolve(S.bandInfo.get(band))
    .then(() => Promise.all(S.m.epochs.map((ep, i) => loadFrame(band, ep.id, S.layer, emptyEpoch(band, i)))))
    .then(() => {
      renderBands();
      if (band !== S.band) return;
      const failed = S.m.bands.find((b) => b.id === band)?.status === "failed";
      if (!failed && !$("status").textContent.includes("low-coverage")) status(""); // keep a failure message
      if (isCustom(band) && !failed && S.m.epochs.every((_, i) => emptyEpoch(band, i)))
        $("winNote").textContent = "No exposure has its field-centre wavelength inside this window in any epoch; try a wider window.";
      fixEpoch(); draw(); syncUrl();
    });
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
  const wiseMark = wiseActive() ? [Object.assign(document.createElement("span"), {
    textContent: "WISE", className: "wise" + (S.onWise ? " cur" : ""), title: `WISE ${S.wiseBand} ${S.wise.date || ""} (not SPHEREx)` })] : [];
  $("epochMarks").replaceChildren(...wiseMark, ...S.m.epochs.map((e, i) => {
    const s = document.createElement("span"), low = isLow(S.band, i), empty = emptyEpoch(S.band, i);
    const n = bandEpoch(S.band, i)?.n_used;
    s.textContent = e.date.slice(2, 7);
    s.className = (empty ? "empty" : low ? "low" : "") + (i === S.epoch && !S.onWise ? " cur" : "");
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
// credit lines in the footer while the matching layer is in use (texts verbatim from the IRSA / ESA pages)
function updateCredits() {
  const wiseUsed = !!(S.wise && S.wiseOn), catUsed = !!(S.catwise && $("ovCat").checked);
  $("creditWise").hidden = $("creditCat").hidden = !(wiseUsed || catUsed);
  $("creditGaia").hidden = !(S.gaia?.length && $("ovGaia").checked);
}
function draw() {
  if (!S.m?.grid) return;
  updateCredits();
  const [ny, nx] = S.m.grid.shape, z = S.zoom, cv = $("canvas"), ctx = cv.getContext("2d");
  cv.width = nx * z; cv.height = ny * z;
  ctx.fillStyle = "#000"; ctx.fillRect(0, 0, cv.width, cv.height);
  if (S.onWise && wiseActive()) { // the WISE frame: its own image, overlays at the WISE date, no SPHEREx processing
    $("epoch").value = -1; renderEpochMarks();
    $("epochLabel").textContent = `WISE ${S.wiseBand}  ${S.wise.date || ""}${S.wise.mjd != null ? `  MJD ${(+S.wise.mjd).toFixed(2)}` : ""}\nnot SPHEREx: ${S.wise.survey}`;
    drawWise(ctx, nx, ny, z);
    overlays(ctx, { id: "wise", mjd_mid: S.wise.mjd, date: S.wise.date });
    wiseBanner(ctx);
    return;
  }
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
  if (S.onWise) { // a mark on the WISE frame carries the WISE date; without one it cannot be timed
    if (S.wise.mjd == null) { status("This WISE image has no date in wise.json, so a mark on it cannot be timed; mark the SPHEREx frames instead.", true); return; }
    S.marks.push({ n: ++S.markSeq, ra, dec, mjd: +S.wise.mjd, epoch: -1, date: S.wise.date || `MJD ${S.wise.mjd}`, band: `WISE ${S.wiseBand}` });
    renderMarks(); draw(); return;
  }
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
    const here = S.onWise ? m.epoch === -1 : m.epoch === S.epoch, r = Math.max(5, 2 * z);
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
    nights: (j.nights || []).map((n) => ({ night: n.night, nAcc: n.n_accepted, nTot: n.n_total, included: n.included, nDraft: n.n_in_draft })),
    counts: { found: j.n_exposures_found, measured: j.n_measured, accepted: j.n_accepted, draft: j.n_in_draft },
    testForms: j.mpc_test_forms || {},
    meas: (j.measurements || []).map((m) => ({ id: m.obs_id, time: m.obs_time, mjd: m.mjd, ra: m.ra, dec: m.dec,
      // two different S/N: image S/N (snr_image, behind the draft's logSNR) and acceptance S/N
      // (min of snr_fit and snr_calibrated; a row is accepted at >= 5); null -> "—"
      rmsRa: m.rms_ra, rmsDec: m.rms_dec, snrImage: m.snr_image ?? null,
      snrAccept: [m.snr_fit, m.snr_calibrated].filter((v) => v != null).reduce((a, v) => Math.min(a, v), Infinity),
      ok: !!m.accepted, reason: m.reason || "", band: m.band, night: m.night,
      inDraft: m.in_ades })), // in_ades: the server's word on whether this row is in the MPC draft
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
  const c = t.counts, cnt = c.found != null ? `\n${c.found} exposures found, ${c.measured} measured, ${c.accepted} accepted, ${c.draft} in the MPC draft` : "";
  head.textContent = `${t.regime}${t.regimeReason ? ": " + t.regimeReason : ""}` + cnt +
    (f ? `\n${rateText(f, ok.length, "single-exposure measurements")}` : "\nfewer than two accepted measurements: no rate");
  head.style.whiteSpace = "pre-wrap";
  const tbl = document.createElement("table");
  // "reason" says why a measurement is not in the MPC draft; an accepted one can still be left out of it
  const state = (m) => (!m.ok ? `rejected: ${escapeHtml(m.reason)}`
    : m.inDraft === false || (m.inDraft == null && m.reason) ? `accepted · not in the MPC draft${m.reason ? ": " + escapeHtml(m.reason) : ""}`
    : m.inDraft ? "accepted · in the MPC draft" : "accepted");
  const num = (v) => (v == null || !Number.isFinite(+v) ? "—" : (+v).toFixed(1));
  tbl.innerHTML = "<tr><th>time (UTC)</th><th>RA</th><th>Dec</th><th>±″</th>" +
    "<th title=\"S/N of the source in the image (snr_image); the draft's logSNR is log10 of this\">image S/N</th>" +
    "<th title=\"min(snr_fit, snr_calibrated); a measurement is accepted at 5 or more\">acceptance S/N</th><th>band</th><th></th></tr>" +
    t.meas.map((m) => `<tr class="${m.ok ? "" : "rejected"}"><td>${escapeHtml(String(m.time ?? ""))}</td><td>${(+m.ra).toFixed(5)}</td>` +
      `<td>${(+m.dec).toFixed(5)}</td><td>${Math.hypot(m.rmsRa, m.rmsDec).toFixed(1)}</td><td>${num(m.snrImage)}</td><td>${num(m.snrAccept)}</td>` +
      `<td>${escapeHtml(String(m.band ?? ""))}</td><td style="text-align:left">${state(m)}</td></tr>`).join("");
  $("refineResult").replaceChildren(head, tbl);
}

// report window
const ADES_BLOCKED_BETWEEN = "The Minor Planet Center accepts Solar System objects only. This motion over months looks like a star or brown dwarf; use the CSV.";
function openReport() {
  const t = S.track; if (!t) return;
  const v = t.validation || {};
  // validation text from the server (lines separated by \n), shown as it is
  $("dlgValidation").textContent = [v.text, v.timing === "not verified" ? "Timing: not verified." : "",
    v.pixel_scale != null ? String(v.pixel_scale) : "", v.same_frame_code ? `Same-frame code: ${v.same_frame_code}.` : ""].filter(Boolean).join("\n");
  // the MPC's own test forms: links from the server if it gives them, else the fixed ones in the page
  if (t.testForms.psv) $("reportDlg").querySelector('a[href*="submit_psv_test"]').href = t.testForms.psv;
  $("dlgNights").replaceChildren(...t.nights.map((n) => {
    // n_in_draft from the server decides; older answers fall back to "3 accepted"
    const div = document.createElement("div"), inFile = n.nDraft != null ? n.nDraft > 0 : n.included !== false && n.nAcc >= 3;
    div.className = inFile ? "" : "excluded";
    div.textContent = `24-h window from ${n.night} (UTC): ${n.nAcc} accepted of ${n.nTot}` + (n.nDraft != null ? `, ${n.nDraft} in the draft` : "") +
      (inFile ? "" : " — not in the MPC draft (fewer than 3 accepted in the window)");
    return div;
  }));
  const goodNight = t.counts.draft != null ? t.counts.draft > 0 : t.nights.some((n) => n.included !== false && n.nAcc >= 3);
  const why = t.regime === "between_epochs" ? ADES_BLOCKED_BETWEEN
    : t.regime !== "within_visit" ? `The server did not classify this motion as within one visit (${t.regime ?? "unknown"}); use the CSV.`
    : !goodNight ? "The MPC asks for at least three observations per night; no 24-hour window has three accepted measurements. Use the CSV."
    : S.health?.ades_schema === false ? "The server cannot check ADES files against the MPC schema right now, so it does not offer them; use the CSV." : "";
  $("dlgAdes").disabled = $("dlgAdesXml").disabled = !!why; $("dlgWhy").textContent = why;
  $("dlgStatus").textContent = ""; $("dlgStatus").className = "";
  $("reportDlg").showModal();
}
async function downloadReport(format) {
  const t = S.track, st = $("dlgStatus");
  const submitter = $("dlgSubmitter").value.trim(), trksub = $("dlgTrksub").value.trim();
  const ades = format.startsWith("ades");
  if (ades && !submitter) { st.textContent = "Enter the submitter name (for example I. M. Lastname)."; st.className = "err"; return; }
  st.textContent = "Preparing the file…"; st.className = "";
  try {
    let blob, name;
    if (MOCK) { // developer mock: no server, so the fixture itself is saved, clearly named
      blob = new Blob([JSON.stringify({ mock: true, format, submitter, trksub, track: t }, null, 1)], { type: "application/json" });
      name = `MOCK_${format}_report.json`;
    } else {
      const q = new URLSearchParams({ id: t.id, format });
      if (ades) { q.set("submitter", submitter); if (trksub) q.set("trksub", trksub); }
      const r = await fetch(`${API}/api/report?${q}`);
      if (!r.ok) { let msg = `${r.status}`; try { msg = (await r.json()).message || msg; } catch { /* not json */ } throw new Error(msg); }
      blob = await r.blob();
      S.schemaChecked = r.headers.get("X-ADES-Schema-Checked");
      const cd = r.headers.get("Content-Disposition") || "";
      // name chosen by the server (needs Access-Control-Expose-Headers when the server is on another origin)
      name = (cd.match(/filename\*?=(?:UTF-8'')?"?([^";]+)"?/i) || [])[1] || `spherex_report.${{ csv: "csv", ades: "psv", ades_xml: "xml" }[format] || "txt"}`;
    }
    const a = Object.assign(document.createElement("a"), { href: URL.createObjectURL(blob), download: decodeURIComponent(name) });
    document.body.append(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(a.href), 5000);
    st.textContent = `Downloaded ${decodeURIComponent(name)}.` +
      (ades && !MOCK ? (S.schemaChecked === "true" ? " The server checked it against the ADES schema."
        : S.schemaChecked ? ` Schema check reported by the server: ${S.schemaChecked}.` : " The server did not report a schema check.") : "");
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
  if ($("ovGaia").checked && S.gaia && ep.mjd_mid != null) { // no date (a WISE frame without epoch): not drawn
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
  drawCatwise(ctx, ep.mjd_mid);
  drawCandidates(ctx, ep);
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
  Object.assign(svg.dataset, { x0, x1, L, R, W }); // for the wavelength-window drag on the spectrum
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
    // hover text: the point's values and the names of the FLAGS bits set in its aperture (F3)
    const tip = `<title>${escapeHtml(pointTip(p, yk))}</title>`;
    if (p.saturated && yk === "flux") { // lower limit: open marker with an upward tick
      h += `<g stroke="${c}" fill="none" opacity="0.7"><circle cx="${cx}" cy="${cy.toFixed(1)}" r="2.6"/>` +
        `<path d="M${cx} ${(cy - 2.6).toFixed(1)}v-7m-2.5 2.5l2.5-2.5 2.5 2.5"/>${tip}</g>`;
    } else if (p.overflow && yk === "flux") { // recovered from overflow: a valid measurement, filled diamond
      h += `<path d="M${cx} ${(cy - 3.2).toFixed(1)}l3.2 3.2-3.2 3.2-3.2-3.2z" fill="${c}" opacity="${faint ? 0.2 : 1}">${tip}</path>`;
    } else {
      h += `<circle cx="${cx}" cy="${cy.toFixed(1)}" r="2.2" fill="${c}" opacity="${faint ? 0.2 : 1}">${tip}</circle>`;
    }
  }
  // epoch legend in its own row above the plot, so it never covers data points
  const step = Math.min(68, (W - L - R) / Math.max(1, S.m.epochs.length));
  h += S.m.epochs.map((e, i) => `<text x="${(L + i * step).toFixed(0)}" y="12" fill="${EPOCH_COLORS[e.id % EPOCH_COLORS.length]}" font-size="10">${e.date}</text>`).join("");
  svg.innerHTML = h;
}
function pointTip(p, yk) {
  const v = p[yk], unit = S.spectrum?.meta?.flux_unit || "";
  const lines = [
    `${(+p.wavelength_um).toFixed(3)} µm · ${Number.isFinite(+v) ? (+v).toPrecision(3) : "—"} ${yk === "wing_flux" ? "MJy/sr" : unit}` +
      (p.err != null && yk === "flux" ? ` ± ${(+p.err).toPrecision(2)}` : ""),
    `MJD ${(+p.mjd).toFixed(3)}${p.band ? ` · ${p.band}` : ""}`,
  ];
  if (p.saturated) lines.push("saturated (lower limit)"); else if (p.overflow) lines.push("recovered from overflow");
  if (p.flagged) lines.push("flagged");
  if (Array.isArray(p.flags)) lines.push(p.flags.length ? `flags: ${sortFlags(p.flags).join(", ")}` : "flags: none");
  return lines.join("\n");
}
// Informational bits, as named in the server's meta.flags_region text ("informational bits (SOURCE,
// FULLSAMPLE) included"); SOURCE is set on nearly every point at a star. They are listed last and dimmed.
const INFO_FLAGS = new Set(["SOURCE", "FULLSAMPLE"]);
const sortFlags = (fs) => [...fs].sort((a, b) => INFO_FLAGS.has(a) - INFO_FLAGS.has(b));
// meta.flag_legend {name: one-line meaning}, shown as it is in a closed "Flags" section; names seen in this
// spectrum first (by count), informational ones last; source and region text from meta
function renderFlagLegend(pts, meta) {
  const leg = meta?.flag_legend, box = $("flagBox");
  box.hidden = !leg || !Object.keys(leg).length;
  if (box.hidden) return;
  const seen = new Map();
  for (const p of pts) for (const f of p.flags || []) seen.set(f, (seen.get(f) || 0) + 1);
  const names = [...new Set([...Object.keys(leg), ...seen.keys()])].sort((a, b) =>
    INFO_FLAGS.has(a) - INFO_FLAGS.has(b) || (seen.get(b) || 0) - (seen.get(a) || 0) || a.localeCompare(b));
  $("flagSummary").textContent = `Flags (${seen.size} kinds in this spectrum)`;
  $("flagSource").textContent = [meta.flags_region, meta.flag_legend_source && `Meanings: ${meta.flag_legend_source}`]
    .filter(Boolean).map((t) => t.trim().replace(/([^.])$/, "$1.")).join(" ");
  $("flagList").replaceChildren(...names.map((n) => {
    const info = INFO_FLAGS.has(n) ? " (informational)" : "";
    const dt = Object.assign(document.createElement("dt"), { textContent: `${n}${seen.get(n) ? ` — ${seen.get(n)} points` : ""}${info}` });
    const dd = Object.assign(document.createElement("dd"), { textContent: leg[n] ?? "no meaning given" });
    if (!seen.get(n) || info) dt.className = dd.className = "muted";
    return [dt, dd];
  }).flat());
}

function plots() {
  const raw = S.spectrum;
  const pts = (Array.isArray(raw) ? raw : raw?.points || []).filter((p) => Number.isFinite(p.flux));
  const meta = raw?.meta || {}, unit = meta.flux_unit || "flux";
  $("specWhere").textContent = S.specAt === "target" ? "(target position)" : S.specAt ? `(${S.specAt.map((v) => v.toFixed(5)).join(", ")})` : "";
  scatter($("spec"), pts, "wavelength_um", "flux", "µm", unit); // flagged points drawn faint
  renderFlagLegend(pts, meta);
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
    if (b.custom) { // user window: removable
      const x = Object.assign(document.createElement("span"), { className: "rm", textContent: "×", title: "remove this window" });
      x.onclick = (e) => { e.stopPropagation(); removeCustomBand(b.id); };
      btn.append(x);
    }
    return btn;
  }));
}

// ---------- sky context panel (F5): Aladin Lite, loaded only when the panel is opened ----------
// Aladin Lite 3.8.2 (CDS, LGPL-3.0 per the LICENSE file in the npm package), an ES module from jsDelivr.
// Survey ids are from Aladin's own built-in list / the CDS MocServer (checked 14 Nov 2026).
const ALADIN_URL = "https://cdn.jsdelivr.net/npm/aladin-lite@3.8.2/dist/aladin.js";
// Surveys are given to Aladin as HiPS URLs, not ids: the id resolver Aladin 3.8.2 calls (alasky.unistra.fr)
// does not resolve. The URLs are each survey's hips_service_url mirrors from its CDS MocServer record; the
// page tries them in this order and uses the first whose "properties" file answers.
const SURVEY_MIRRORS = {
  "CDS/P/allWISE/color": ["https://irsa.ipac.caltech.edu/data/hips/CDS/AllWISE/RGB-W4-W2-W1", "https://skies.esac.esa.int/AllWISEColor",
    "https://hips.china-vo.org/m/CDS_P_allWISE_color", "https://alasky.cds.unistra.fr/AllWISE/RGB-W4-W2-W1"],
  "CDS/P/allWISE/W2": ["https://irsa.ipac.caltech.edu/data/hips/CDS/AllWISE/W2", "https://alasky.cds.unistra.fr/AllWISE/W2"],
  "CDS/P/unWISE/color-W2-W1W2-W1": ["https://alasky.cds.unistra.fr/unWISE/color-W2-W1W2-W1", "https://alaskybis.cds.unistra.fr/unWISE/color-W2-W1W2-W1"],
  "CDS/P/NEOWISER/Color": ["https://alasky.cds.unistra.fr/NEOWISER/W1W2", "https://alaskybis.cds.unistra.fr/NEOWISER/W1W2"],
  "CDS/P/2MASS/color": ["https://irsa.ipac.caltech.edu/data/hips/CDS/2MASS/Color", "https://alasky.cds.unistra.fr/2MASS/Color"],
  "CDS/P/DSS2/color": ["https://irsa.ipac.caltech.edu/data/hips/CDS/DSS2/color", "https://skies.esac.esa.int/DSSColor",
    "https://stpubdata.s3.us-east-1.amazonaws.com/mast/skybackgrounds/DSSColor", "https://alasky.cds.unistra.fr/DSS/DSSColor"],
  "CDS/P/PanSTARRS/DR1/color-z-zg-g": ["https://alasky.cds.unistra.fr/Pan-STARRS/DR1/color-z-zg-g", "https://alaskybis.cds.unistra.fr/Pan-STARRS/DR1/color-z-zg-g"],
};
const mirrorCache = {};
async function pickMirror(id) {
  if (id in mirrorCache) return mirrorCache[id];
  for (const u of SURVEY_MIRRORS[id] || []) {
    const ctl = new AbortController(), t = setTimeout(() => ctl.abort(), 4000);
    try { const r = await fetch(`${u}/properties`, { signal: ctl.signal }); if (r.ok) return (mirrorCache[id] = u); }
    catch { /* try the next mirror */ } finally { clearTimeout(t); }
  }
  return (mirrorCache[id] = null);
}
async function setSurvey() {
  const id = $("aladinSurvey").value, label = $("aladinSurvey").selectedOptions[0].textContent;
  $("aladinMsg").textContent = `Looking for a server for ${label}…`;
  const url = await pickMirror(id);
  if (!url) { $("aladinMsg").textContent = `${label}: none of its servers answers right now; choose another survey.`; return null; }
  if (aladin) aladin.setBaseImageLayer(url);
  $("aladinMsg").textContent = `${label} from ${new URL(url).host}`;
  return url;
}
let aladin = null, aladinLoading = null;
async function openAladin() {
  if (aladin || aladinLoading || !S.m?.grid) return aladinLoading;
  const msg = $("aladinMsg");
  msg.textContent = "Loading Aladin Lite…";
  aladinLoading = (async () => {
    try {
      const A = (await import(ALADIN_URL)).default;
      await A.init; // throws when WebGL2 is not available
      const first = await setSurvey(); // a reachable mirror of the chosen survey (or null)
      const g = S.m.grid, [ra0, dec0] = pix2world((g.shape[1] - 1) / 2, (g.shape[0] - 1) / 2);
      const fov = Math.max(g.shape[0], g.shape[1]) * Math.abs(g.cdelt[1]) * 1.3;
      aladin = A.aladin("#aladinDiv", { ...(first ? { survey: first } : {}), fov, target: `${ra0} ${dec0}`,
        showReticle: false, showCooGrid: false, showLayersControl: false, showGotoControl: false, showFullscreenControl: false,
        showFrame: false, showZoomControl: true, showProjectionControl: false, showSimbadPointerControl: false, showShareControl: false });
      // this grid's outline and the target's current position
      const ov = A.graphicOverlay({ color: "#e89a3c", lineWidth: 2 });
      aladin.addOverlay(ov);
      const [ny, nx] = g.shape, c = [[-0.5, -0.5], [nx - 0.5, -0.5], [nx - 0.5, ny - 0.5], [-0.5, ny - 0.5]].map(([x, y]) => pix2world(x, y));
      ov.add(A.polygon(c));
      const t = S.m.target_track?.find((p) => p.epoch === S.m.epochs[S.epoch]?.id) || S.m.target_track?.at(-1) || S.m.query;
      if (t) {
        const cat = A.catalog({ name: "target", color: "#ff4fd8", sourceSize: 18, shape: "circle" });
        aladin.addCatalog(cat); cat.addSources([A.source(+t.ra, +t.dec)]);
      }
    } catch (e) {
      aladin = null;
      msg.textContent = `The sky context viewer (Aladin Lite) could not be loaded (${e?.message || e}); the rest of the page works without it.`;
    } finally { aladinLoading = null; }
  })();
  return aladinLoading;
}

// ---------- WISE image in the blink: {key}/wise.json + wise_W1.f32 / wise_W2.f32 on this field's grid ----------
// Shown only in blink, first in the sequence (oldest), with its own scale and a "WISE, not SPHEREx" banner.
// Not used in difference, sum, false colour or candidates. ?wise=W1|W2 turns it on; frame=wise opens on it.
S.wise = null; S.wiseOn = /^W[12]$/.test(params.get("wise") || ""); S.wiseBand = params.get("wise") || "W2"; S.onWise = false;
const wiseActive = () => !!(S.wise && S.wiseOn && S.mode === "blink");
async function loadWise() {
  let meta;
  try { const r = await fetch(`${DATA}/${S.key}/wise.json`); if (!r.ok) return; meta = await r.json(); } catch { return; }
  // field names (one place to adapt): epoch_mjd (or null), date, survey, note, bands[{id, file, scale, wave_um}]
  const bands = (meta.bands || []).filter((b) => /^W[12]$/.test(b.id));
  const frames = {};
  for (const b of bands) {
    try {
      const r = await fetch(`${DATA}/${S.key}/${b.file || `wise_${b.id}.f32`}`);
      if (r.ok) frames[b.id] = new Float32Array(littleEndian(await r.arrayBuffer()));
    } catch { /* that band is left out */ }
  }
  if (!Object.keys(frames).length) return;
  S.wise = { mjd: meta.epoch_mjd ?? meta.mjd ?? null, date: meta.date || "", survey: meta.survey || "WISE", note: meta.note || "",
    scale: Object.fromEntries(bands.map((b) => [b.id, b.scale])), frames };
  if (!frames[S.wiseBand]) S.wiseBand = Object.keys(frames)[0];
  $("wiseBox").hidden = false; $("wiseOn").checked = S.wiseOn; $("wiseBand").value = S.wiseBand;
  [...$("wiseBand").options].forEach((o) => { o.disabled = !frames[o.value]; });
  const unit = bands.find((b) => b.id === S.wiseBand)?.unit;
  $("wiseNote").textContent = [`${S.wise.survey}, ${S.wise.date || "date not given"}`, meta.epoch_source, unit && `unit: ${unit}`, S.wise.note]
    .filter(Boolean).map((t) => String(t).trim().replace(/([^.])$/, "$1.")).join(" ");
  if (params.get("frame") === "wise" && wiseActive()) S.onWise = true;
  wiseSlider();
}
// the slider gets a position −1 for the WISE frame while it is in the sequence
function wiseSlider() { $("epoch").min = wiseActive() ? -1 : 0; if (!wiseActive()) S.onWise = false; }
function drawWise(ctx, nx, ny, z) {
  const a = S.wise.frames[S.wiseBand], sc = S.wise.scale[S.wiseBand];
  const s = sc && sc.vmax != null ? { vmin: +sc.vmin, vmax: +sc.vmax, soft: +(sc.soft ?? 0) } : (() => {
    const [lo, hi] = percentile([a], [1, 99.7]); return { vmin: lo, vmax: hi, soft: (hi - lo) * 0.05 || 1e-6 }; })();
  off.width = nx; off.height = ny;
  const oc = off.getContext("2d"), img = oc.createImageData(nx, ny);
  for (let y = 0; y < ny; y++) {
    const row = (ny - 1 - y) * nx; // first data row is south
    for (let x = 0; x < nx; x++) {
      const t = stretch(a[y * nx + x], s), o = (row + x) * 4;
      if (t < 0) { img.data.set([40, 18, 22, 255], o); continue; }
      const g = 255 * t; img.data.set([g, g, g, 255], o);
    }
  }
  oc.putImageData(img, 0, 0); ctx.imageSmoothingEnabled = false; ctx.drawImage(off, 0, 0, nx * z, ny * z);
  $("scaleSrc").textContent = `WISE ${S.wiseBand}: its own scale from wise.json`;
}
function wiseBanner(ctx) {
  const t = `WISE ${S.wiseBand} · ${S.wise.date || "date not given"} · not SPHEREx`;
  ctx.font = "bold 13px system-ui";
  const w = ctx.measureText(t).width + 12, x = (ctx.canvas.width - w) / 2;
  ctx.fillStyle = "rgba(0,0,0,0.8)"; ctx.fillRect(x, 6, w, 22);
  ctx.fillStyle = "#79c0ff"; ctx.fillText(t, x + 6, 22);
}

// ---------- CatWISE2020 layer: {key}/catwise.json (gallery) or /api/catwise?key= (live fields) ----------
S.catwise = null;
const CAT_COLOR = "#7ee787", CAT_EPOCH = 2015.4;
// server answer -> page fields (one place to adapt): source_name, ra, dec, pmra/pmdec (mas/yr or null), w1mag, w2mag, ref_mjd
function normCatwise(j) {
  return (Array.isArray(j) ? j : j.sources || []).map((s) => ({
    name: s.source_name ?? s.name ?? "", ra: +s.ra, dec: +s.dec,
    pmra: s.pmra == null ? null : +s.pmra, pmdec: s.pmdec == null ? null : +s.pmdec,
    w1: s.w1mag ?? null, w2: s.w2mag ?? null, reliable: s.pm_reliable !== false, pmCheck: s.pm_check ?? null,
    sameAs: s.same_object_as ?? null, // another row of the same object: not drawn, named in that row's readout
    target: s.target ?? null, // the row that is the field's named target
    refMjd: s.ref_mjd != null ? +s.ref_mjd : mjdOfYear(s.ref_epoch ?? CAT_EPOCH) }));
}
async function loadCatwise() {
  const url = USE_API ? `${API}/api/catwise?key=${enc(S.key)}` : `${DATA}/${S.key}/catwise.json`;
  let r;
  try { r = await fetch(url); } catch { return; }
  const box = $("catBox"), note = $("catNote");
  if (r.status === 202) { // the server is preparing it: ask again
    box.hidden = false; $("ovCat").disabled = true; note.textContent = "The WISE catalogue is being prepared…";
    setTimeout(() => loadCatwise().then(draw), 5000); return;
  }
  if (r.status === 503) { box.hidden = false; $("ovCat").disabled = true; note.textContent = "WISE catalogue is not available right now."; return; }
  if (!r.ok) { box.hidden = true; return; } // no layer for this field (e.g. R Dra in the gallery): no button
  let j;
  try { j = await r.json(); S.catwise = normCatwise(j); S.catBright = null; } catch { box.hidden = true; return; }
  const m = j?.meta || {};
  box.hidden = false;
  if (!S.catwise.length) { // an empty answer: the catalogue has nothing here (not an outage)
    $("ovCat").disabled = true; $("ovCat").checked = false;
    note.textContent = m.empty_note || "CatWISE2020 has no sources in this field.";
    return;
  }
  $("ovCat").disabled = false;
  const moved = S.catwise.some((s) => s.pmra != null && s.pmdec != null && s.reliable), fast = S.catwise.filter((s) => !s.reliable).length;
  note.textContent = [`${S.catwise.length} sources; ` + (moved ? "moved to each epoch with their proper motion"
    : `positions at the catalogue epoch (${CAT_EPOCH}), not moved`),
    fast && `${fast} with an unreliable catalogue motion are drawn dashed at the catalogue position, not moved`,
    m.pm_convention, m.fast_note].filter(Boolean).map((t) => String(t).trim().replace(/([^.])$/, "$1.")).join(" ");
}
// which rows are drawn: by default the 150 brightest in W2, plus rows with a pm_check and rows within 30″ of the
// target (at this epoch); "show all" draws every row. Duplicate rows (same_object_as) are never drawn.
const CAT_BRIGHT = 150, CAT_NEAR_ARCSEC = 30;
function catVisible(mjd) {
  const all = S.catwise.filter((s) => !s.sameAs);
  if ($("catAll").checked) return all;
  if (!S.catBright) S.catBright = new Set([...all].filter((s) => s.w2 != null).sort((a, b) => a.w2 - b.w2).slice(0, CAT_BRIGHT));
  const ep = S.m.epochs.find((e) => e.mjd_mid === mjd);
  const t = (ep && S.m.target_track?.find((p) => p.epoch === ep.id)) || S.m.target_track?.at(-1) || S.m.query;
  return all.filter((s) => S.catBright.has(s) || s.pmCheck != null ||
    (t && angSep(+t.ra, +t.dec, ...catPos(s, mjd)) * 3600 <= CAT_NEAR_ARCSEC));
}
function catPos(s, mjd) {
  // unreliable motions (pm_reliable false: CatWISE fits of fast movers) stay at the catalogue position
  if (!s.reliable || s.pmra == null || s.pmdec == null || mjd == null) return [s.ra, s.dec];
  const dt = (mjd - s.refMjd) / 365.25;
  return [s.ra + (s.pmra * dt) / 3.6e6 / Math.cos(s.dec * D2R), s.dec + (s.pmdec * dt) / 3.6e6];
}
function drawCatwise(ctx, mjd) {
  if (!S.catwise || !$("ovCat").checked) { $("catCount").textContent = ""; return; }
  const a = Math.max(6, 2.5 * S.zoom);
  ctx.save(); ctx.strokeStyle = CAT_COLOR; ctx.lineWidth = 1.5;
  const vis = catVisible(mjd);
  $("catCount").textContent = $("catAll").checked ? `all ${vis.length}` : `${vis.length} of ${S.catwise.filter((s) => !s.sameAs).length}: ` +
    `the ${CAT_BRIGHT} brightest in W2, plus checked rows and rows within ${CAT_NEAR_ARCSEC}″ of the target`;
  for (const s of vis) {
    const [x, y] = toScreen(...catPos(s, mjd));
    ctx.setLineDash(s.reliable ? [] : [3, 3]); // unreliable motion: dashed square at the catalogue position
    ctx.strokeRect(x - a / 2, y - a / 2, a, a);
  }
  ctx.restore();
}
// the CatWISE source under the cursor (within a few screen pixels), for the readout
function catwiseAt(sx, sy, mjd) {
  if (!S.catwise || !$("ovCat").checked) return null;
  let best = null, bd = Math.max(8, 3 * S.zoom);
  for (const s of catVisible(mjd)) {
    const [x, y] = toScreen(...catPos(s, mjd)), dd = Math.hypot(x - sx, y - sy); if (dd < bd) { bd = dd; best = s; }
  }
  if (best) {
    const dup = S.catwise.filter((s) => s.sameAs === best.name).map((s) => s.name);
    if (dup.length) best = { ...best, name: `${best.name} (${dup.length + 1} catalogue entries for this object: also ${dup.join(", ")})` };
  }
  return best;
}

// ---------- automatic candidates (F2): {key}/candidates.json or /api/candidates?key= ----------
// Places to look at, not detection claims. Without a file the control stays hidden.
S.cands = null;
const CAND_COLOR = "#ffd84f";
function candLabel(c) {
  if (c.near_gaia) return `known: Gaia ${String(c.near_gaia.source_id ?? "").slice(-6)}`;
  return c.kind || "candidate";
}
async function loadCandidates() {
  const url = USE_API ? `${API}/api/candidates?key=${enc(S.key)}` : `${DATA}/${S.key}/candidates.json`;
  try {
    const r = await fetch(url); // quiet: a missing file only means no candidates for this field
    if (r.status === 202) { // the server computes candidates once all bands of the field are built: ask again
      $("candBox").hidden = false; $("candBox").classList.add("pending");
      $("ovCand").disabled = true; $("candPending").textContent = "Automatic candidates are being computed…";
      setTimeout(() => loadCandidates().then(draw), 5000);
      return;
    }
    if (!r.ok) { $("candBox").hidden = true; return; } // 404: no candidates for this field
    S.cands = await r.json();
  } catch { return; }
  const cs = S.cands.candidates || [];
  $("candBox").hidden = false; $("candBox").classList.remove("pending");
  $("ovCand").disabled = false; $("candPending").textContent = "";
  $("candList").replaceChildren(...cs.map((c) => {
    const li = document.createElement("li"), b = document.createElement("button");
    const rate = c.rate_arcsec_per_year ? ` · ${Math.hypot(...c.rate_arcsec_per_year).toFixed(1)}″/yr` : "";
    b.textContent = `${c.id} ${candLabel(c)} · ${(c.bands || []).join(", ")} · S/N ${(+c.snr).toFixed(1)}${rate}`;
    b.title = `${c.notes || ""}\nclick: load its ${c.positions?.length || 0} positions as marks in the measure tool`;
    b.onclick = () => candidateToMarks(c);
    li.append(b); return li;
  }));
  const rej = Object.entries(S.cands.rejected || {}).map(([k, v]) => `  ${v} × ${k}`).join("\n");
  $("candHow").textContent = [
    S.cands.method && `method: ${S.cands.method}`,
    S.cands.params && `parameters: ${Object.entries(S.cands.params).map(([k, v]) => `${k} ${v}`).join(", ")}`,
    S.cands.bands_searched && `bands searched: ${S.cands.bands_searched.join(", ")}`,
    `${cs.length} candidates shown`, rej && `rejected:\n${rej}`,
    ...[].concat(S.cands.validation || []),
  ].filter(Boolean).join("\n");
}
// a candidate's positions become marks in the measure tool (then refine/report as usual)
function candidateToMarks(c) {
  S.marks = (c.positions || []).map((p) => {
    const i = S.m.epochs.findIndex((e) => e.id === p.epoch), ep = S.m.epochs[i];
    return ep && { n: ++S.markSeq, ra: +p.ra, dec: +p.dec, mjd: ep.mjd_mid, epoch: i, date: ep.date, band: (c.bands || [])[0] || S.band };
  }).filter(Boolean);
  if (c.bands?.[0] && c.bands[0] !== S.band && S.m.bands.some((b) => b.id === c.bands[0])) selectBand(c.bands[0]);
  renderMarks(); draw();
  status(`Candidate ${c.id}: ${S.marks.length} positions loaded as marks in the measure tool.`);
  $("measureBox").scrollIntoView({ block: "nearest" });
}
function drawCandidates(ctx, ep) {
  if (!S.cands || !$("ovCand").checked || ep.id === "wise") return; // candidates are SPHEREx-only
  const z = S.zoom;
  ctx.save(); ctx.font = "11px system-ui";
  for (const c of S.cands.candidates || []) {
    const ps = c.positions || [];
    // the position at this epoch; otherwise the nearest in time, drawn faint
    let p = ps.find((q) => q.epoch === ep.id), faint = false;
    if (!p && ps.length) {
      faint = true;
      const mjdOf = (q) => S.m.epochs.find((e) => e.id === q.epoch)?.mjd_mid ?? 0;
      p = ps.reduce((a, q) => (Math.abs(mjdOf(q) - ep.mjd_mid) < Math.abs(mjdOf(a) - ep.mjd_mid) ? q : a));
    }
    if (!p) continue;
    const [x, y] = toScreen(+p.ra, +p.dec), r = Math.max(7, 3.5 * z);
    ctx.globalAlpha = faint ? 0.4 : 1; ctx.strokeStyle = CAND_COLOR; ctx.lineWidth = 1.5; ctx.setLineDash([4, 3]);
    ctx.beginPath(); ctx.arc(x, y, r, 0, 2 * Math.PI); ctx.stroke(); ctx.setLineDash([]);
    const t = `${c.id} ${candLabel(c)}`;
    ctx.lineWidth = 3; ctx.strokeStyle = "#000"; ctx.strokeText(t, x + r + 3, y + r);
    ctx.fillStyle = CAND_COLOR; ctx.fillText(t, x + r + 3, y + r);
  }
  ctx.restore();
}

// ---------- user wavelength windows ----------
// nm: lo rounded down, hi up; narrower than 50 nm is widened around its centre (the server's minimum)
function windowId(lo, hi) {
  let a = Math.floor(lo * 1000), b = Math.ceil(hi * 1000);
  if (b - a < 50) { const c = (a + b) / 2; a = Math.floor(c - 25); b = a + 50; }
  a = Math.max(750, a); b = Math.min(5000, Math.max(b, a + 50)); if (b - a < 50) a = b - 50;
  return `w${a}_${b}`;
}
function addCustomBand(id) {
  if (S.m.bands.some((b) => b.id === id)) return id;
  const [lo, hi] = id.slice(1).split("_").map((v) => +v / 1000);
  S.m.bands.push({ id, label: `${lo}–${hi} µm (your window)`, wave_um: [lo, hi], n_exposures: [], status: "pending", custom: true });
  return id;
}
function customBandLoaded(id, j) { // the band file says what was built: label, measured wavelengths, counts, rule
  const b = S.m.bands.find((x) => x.id === id); if (!b) return;
  Object.assign(b, { status: "ready", label: (j.label || b.label), wave_um: j.wave_um || b.wave_um, n_exposures: j.n_exposures || b.n_exposures, note: j.note || "" });
  if (id === S.band) $("winNote").textContent = b.note;
  renderBands(); plots();
}
function customBandFailed(id, e) {
  const b = S.m.bands.find((x) => x.id === id); if (b) b.status = "failed";
  // the server's message as sent (e.g. "the window must be at least 50 nm wide")
  if (id === S.band) { const m = String(e.message).replace(/^\d{3}\s*/, ""); status(`Window ${id}: ${m}`, true); $("winNote").textContent = m; }
  renderBands();
}
function removeCustomBand(id) {
  S.m.bands = S.m.bands.filter((b) => b.id !== id);
  S.bandInfo.delete(id);
  for (const k of [...S.frames.keys()]) if (k.startsWith(id + "|")) S.frames.delete(k);
  if (S.band === id) { S.band = null; selectBand(S.m.default_band || S.m.bands[0].id); } else { renderBands(); syncUrl(); }
}
function blinkWindow(lo, hi) {
  if (!API && !WINMOCK) { $("winNote").textContent = "Your own wavelength window needs the live server, which is not reachable right now."; return; }
  if (!(lo >= 0.75 && hi <= 5.0 && lo < hi)) { $("winNote").textContent = "Choose a window between 0.75 and 5.00 µm, with the first number smaller."; return; }
  const id = addCustomBand(windowId(lo, hi));
  $("winNote").textContent = `Building ${id} on the server…`;
  renderBands(); selectBand(id);
}

function selectBand(id) {
  if (id === S.band) return;
  S.prevBand = S.band; S.band = id; S.userScale = null;
  if (S.prevBand) { $("swapBand").textContent = `⇄ ${S.prevBand} (t)`; $("swapBand").disabled = false; }
  // a user window shows the server's selection rule (note) once its band file is in
  const cb = isCustom(id) && S.m.bands.find((b) => b.id === id);
  $("winNote").textContent = cb ? cb.note || `Building ${id} on the server…` : "";
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
      b.onclick = () => (am >= 15 ? askBigField(deg) : changeFov(deg));
      return b;
    }));
}
// 15′: say what it costs before the field changes (in-page confirmation). Numbers measured by the data
// session on 14 Nov 2026: 99.5–137.4 MB and 30.5–69.8 s per band, 727–760 MB for all six.
function askBigField(deg) {
  const box = $("fovConfirm");
  box.hidden = false;
  box.replaceChildren(
    Object.assign(document.createElement("span"), { textContent: "A 15′ field reads about 120 MB per band from the archive, about 730 MB for all six. " +
      "On a home connection that took 30 to 70 s per band (measured 14 Nov 2026). Larger fields help with fast-moving objects. " }),
    Object.assign(document.createElement("button"), { type: "button", textContent: "Open 15′", onclick: () => { box.hidden = true; changeFov(deg); } }),
    Object.assign(document.createElement("button"), { type: "button", textContent: "Keep 7′", onclick: () => { box.hidden = true; } }));
}
async function changeFov(deg) {
  const fov = String(Math.floor(deg * 1e6) / 1e6); // never round up past the server limit (7′ = max_fov_deg)
  // a 15′ grid is 300 px across: 2× keeps it on screen (the zoom is cosmetic)
  const keep = { band: S.band, mode: S.mode, zoom: deg > 0.2 ? Math.min(S.zoom, 2) : S.zoom };
  const t = params.get("target");
  if (t) { // a gallery target becomes a free field: resolve it so the server still centres on the moving object
    try {
      const r = await fetchLogged(`${API}/api/resolve?name=${enc(S.m.name || t)}`);
      navigate({ ra: r.ra, dec: r.dec, fov, name: r.name, pmra: r.pmra, pmdec: r.pmdec, ref_epoch: r.ref_epoch, ...keep });
    } catch (e) { status(`Could not resolve ${S.m.name || t} for a new field: ${e.message}`, true); }
    return;
  }
  navigate({ ra: S.m?.query?.ra ?? params.get("ra"), dec: S.m?.query?.dec ?? params.get("dec"), fov, name: params.get("name"), pmra: params.get("pmra"),
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
  wiseSlider(); // the WISE frame is in the blink only
  draw(); syncUrl(); loadRGB();
}

function syncUrl() {
  const p = new URLSearchParams(location.search);
  p.set("band", S.band); p.set("mode", S.mode); p.set("frame", S.onWise ? "wise" : S.epoch); p.set("zoom", S.zoom);
  if (S.wise && S.wiseOn) p.set("wise", S.wiseBand); else if (S.wise) p.delete("wise");
  // free position: keep the server's own query.ra/dec in the address (exactly as returned), so a reload or a
  // shared link asks for the same field key and does not land on the other side of the server's rounding
  if (p.has("ra") && S.m?.query?.ra != null) { p.set("ra", String(S.m.query.ra)); p.set("dec", String(S.m.query.dec)); }
  if (S.layer !== "image") p.set("layer", S.layer); else p.delete("layer");
  if (S.mode === "rgb" && S.m?.bands) { const c = rgbBands(); p.set("rgb", [c.r, c.g, c.b].join(",")); } else p.delete("rgb");
  if (S.showLow) p.set("low", "1"); else p.delete("low");
  history.replaceState(null, "", "?" + p.toString());
  updateLinks();
}

function step(d) {
  if (!S.m?.epochs?.length) return;
  const n = S.m.epochs.length;
  // the sequence: [WISE (as −1) if it is in the blink] + the playable SPHEREx epochs
  const seq = [...(wiseActive() ? [-1] : []), ...[...Array(n).keys()].filter((i) => playable(S.band, i))];
  if (!seq.length) return;
  const cur = S.onWise ? -1 : S.epoch;
  let pos = seq.indexOf(cur);
  if (pos < 0) pos = d > 0 ? seq.findIndex((v) => v > cur) - 1 : seq.findIndex((v) => v >= cur); // from a skipped frame
  const next = seq[(((pos + d) % seq.length) + seq.length) % seq.length];
  if (next === -1) S.onWise = true; else { S.onWise = false; S.epoch = next; }
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
  $("wiseOn").onchange = (e) => { S.wiseOn = e.target.checked; if (!S.wiseOn) S.onWise = false; wiseSlider(); draw(); syncUrl(); };
  $("wiseBand").onchange = (e) => { S.wiseBand = e.target.value; draw(); syncUrl(); };
  $("copyEmbed").onclick = copyEmbed;
  $("swapBand").onclick = () => S.prevBand && selectBand(S.prevBand);
  $("speed").oninput = () => S.playing && setPlaying(true);
  $("epoch").oninput = (e) => {
    const v = +e.target.value, prev = S.epoch;
    if (v === -1) { if (wiseActive()) { S.onWise = true; draw(); syncUrl(); } return; } // the WISE frame
    S.onWise = false;
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
  for (const id of ["ovQuery", "ovTrack", "ovGaia", "ovCand", "ovCat", "catAll"]) $(id).onchange = draw;

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
    const cr = cv.getBoundingClientRect(), ck = cv.width / cr.width;
    const cw = catwiseAt((ev.clientX - cr.left) * ck, (ev.clientY - cr.top) * ck, S.m.epochs[S.epoch]?.mjd_mid);
    const pmc = cw?.pmCheck == null ? "" : `  check: ${typeof cw.pmCheck === "object" ? JSON.stringify(cw.pmCheck) : cw.pmCheck}`;
    const mag = (v) => (v == null || !Number.isFinite(+v) ? "—" : (+v).toFixed(2));
    const catTxt = cw ? `   CatWISE ${cw.name}${cw.target ? " (the target)" : ""}  W1 ${mag(cw.w1)}  W2 ${mag(cw.w2)}${cw.reliable ? "" : " (catalogue motion not reliable)"}${pmc}` : "";
    const head = `x ${xi}  y ${yi}   RA ${ra.toFixed(5)}  Dec ${dec.toFixed(5)}${catTxt}\n`;
    if (S.onWise && wiseActive()) { // the WISE frame's own value
      const a = S.wise.frames[S.wiseBand], v = inside ? a[yi * nx + xi] : NaN;
      $("readout").textContent = head + `WISE ${S.wiseBand} value ${fmt(v)} (not SPHEREx)`;
      return;
    }
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
  // wavelength window: drag across the spectrum (or type two numbers), then "Blink this window"
  const spec = $("spec");
  const umAt = (ev) => {
    const d = spec.dataset; if (d.x0 == null) return null;
    const r = spec.getBoundingClientRect(), vx = ((ev.clientX - r.left) * +d.W) / r.width;
    const um = +d.x0 + ((vx - +d.L) / (+d.W - +d.L - +d.R)) * (+d.x1 - +d.x0);
    return Math.min(5, Math.max(0.75, um));
  };
  const showSel = (a, b) => {
    spec.querySelector("#winSel")?.remove();
    const d = spec.dataset, px = (um) => +d.L + ((um - +d.x0) / (+d.x1 - +d.x0)) * (+d.W - +d.L - +d.R);
    const x = Math.min(px(a), px(b)), w = Math.abs(px(b) - px(a));
    spec.insertAdjacentHTML("beforeend", `<rect id="winSel" x="${x}" y="24" width="${w}" height="166" fill="#e89a3c" opacity="0.18"/>`);
  };
  let selFrom = null;
  spec.addEventListener("mousedown", (ev) => { selFrom = umAt(ev); });
  spec.addEventListener("mousemove", (ev) => { if (selFrom != null) showSel(selFrom, umAt(ev)); });
  window.addEventListener("mouseup", (ev) => {
    if (selFrom == null) return;
    const to = umAt(ev) ?? selFrom, lo = Math.min(selFrom, to), hi = Math.max(selFrom, to); selFrom = null;
    if (hi - lo < 0.01) return; // a click, not a drag
    $("winLo").value = lo.toFixed(2); $("winHi").value = hi.toFixed(2);
    $("winNote").textContent = `${lo.toFixed(2)}–${hi.toFixed(2)} µm selected: press "Blink this window".`;
  });
  $("winGo").onclick = () => blinkWindow(+$("winLo").value, +$("winHi").value);
  $("aladinBox").addEventListener("toggle", () => { if ($("aladinBox").open) openAladin(); });
  $("aladinSurvey").onchange = () => { if (aladin) setSurvey(); };
  $("refineBtn").onclick = refine;
  $("reportBtn").onclick = openReport;
  $("dlgCsv").onclick = () => downloadReport("csv");
  $("dlgAdes").onclick = () => downloadReport("ades");
  $("dlgAdesXml").onclick = () => downloadReport("ades_xml");
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
// developer hook (?debug=1): place marks at exact positions/times, e.g. to replay a known asteroid passage
if (params.get("debug") === "1") {
  window.__spx = { S, refine, openReport, aladin: () => aladin,
    mark: (ra, dec, mjd, band = S.band) => { S.marks.push({ n: ++S.markSeq, ra, dec, mjd, epoch: S.epoch, date: `MJD ${mjd}`, band }); renderMarks(); draw(); } };
}
