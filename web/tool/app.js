// SPHEREx Blink — the full tool (web/tool/), reading data contract v1 (PLAN.md section 5).
// Plain ES module, no build step. Gallery targets come from static files; free positions, name lookup and field
// changes go to the live server found on start (config.js), or to the server forced with ?api= (developer).

const $ = (id) => document.getElementById(id);
const params = new URLSearchParams(location.search);
// ?api=<url> (developer): everything goes to that server, no probe. ?api=none: no server. No ?api=: the page
// probes window.SPX_API (config.js) once on start; gallery targets stay static, the rest uses the server if found.
// ?api= and ?data= come from a link anyone can make: only safe values are taken, the rest is ignored with a reason
const PARAM_REJECTS = [];
function safeApi(v) {
  if (v == null || v === "" || v === "none") return v;
  try {
    const u = new URL(v);
    const local = u.protocol === "http:" && ["localhost", "127.0.0.1"].includes(u.hostname);
    if ((u.protocol === "https:" || local) && !u.username && !u.password) return u.origin + u.pathname.replace(/\/$/, "");
  } catch { /* not a URL */ }
  PARAM_REJECTS.push("?api= ignored: only an https address or a server on localhost/127.0.0.1 is accepted");
  return null;
}
function safeData(v) {
  if (v == null || v === "") return v;
  // a relative path on this site: letters, digits, . _ - and /, no "..", no scheme, no leading slash
  if (/^[A-Za-z0-9_\-][A-Za-z0-9._\-\/]*$/.test(v) && !v.split("/").includes("..")) return v;
  PARAM_REJECTS.push("?data= ignored: only a relative path on this site is accepted");
  return null;
}
const API_PARAM = safeApi(params.get("api"));
const FORCED = !!API_PARAM && API_PARAM !== "none";
let API = FORCED ? API_PARAM.replace(/\/$/, "") : ""; // the live server in use ("" = none)
// no server address at all (the public site writes SPX_API = ""): not a fault, the site simply has none
const NO_SERVER = !FORCED && (API_PARAM === "none" || !String(window.SPX_API || "").trim());
const README_RUN = "https://github.com/Ayz345/Sci-Tech-SPHEREx-Blink#run-it";
const NOSRV_TXT = "This needs the live server, which is not part of this site. The five gallery targets work here; " +
  "run the server locally for any other position (see the README).";
// the message for a server-only feature: "no server on this site" (with the README link) or "not reachable right now"
function noServer(el, what, err = false) {
  if (NO_SERVER) {
    const a = Object.assign(document.createElement("a"), { href: README_RUN, target: "_blank", rel: "noopener", textContent: "README: run it" });
    el.replaceChildren(NOSRV_TXT + " ", a);
  } else el.textContent = `${what} needs the live server, which is not reachable right now.`;
  if (el.id === "status") el.className = err && !NO_SERVER ? "err" : "";
}
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
const DATA_PARAM = safeData(params.get("data"));
const DATA = (DATA_PARAM || "../data").replace(/\/$/, ""); // the gallery sits next to the tool: web/data/
const D2R = Math.PI / 180;
// design/v5 palette for drawings: off-white and greys plus one accent (ember)
const INK = { fg: "#f2f2f0", fg2: "#c9c9c5", fg3: "#a3a3a0", fg4: "#8d8d93", rule: "rgba(242,242,240,0.32)", ember: "#e0482a" };

// ---------- user wavelength windows (F1): band id w<lo>_<hi> in nm, built by the live server on request ----------
const isCustom = (id) => /^w\d+_\d+$/.test(id || "");
const viaApi = (band) => (USE_API || isCustom(band)) && !!API;

// ---------- data source: one code path for static files and API (USE_API decides per target) ----------
const src = {
  index: () => `${DATA}/index.json`,
  manifest: (q) => USE_API
    ? `${API}/api/manifest?` + new URLSearchParams(q.target ? { target: q.target } : Object.fromEntries(
      Object.entries({ ra: q.ra, dec: q.dec, fov: q.fov, pmra: q.pmra, pmdec: q.pmdec, ref_epoch: q.ref_epoch, name: q.name })
        .filter(([, v]) => v != null && v !== ""))) // optional pm/name: the server then builds target_track
    : q.key || q.target ? `${DATA}/${q.key || q.target}/manifest.json` : null,
  // a user window always comes from the live server, also for a gallery target opened from static files
  band: (key, band) => viaApi(band) ? `${API}/api/band?key=${enc(key)}&band=${enc(band)}` : `${DATA}/${key}/${band}.json`,
  frame: (key, epoch, band, kind) => viaApi(band)
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
    try { r = await fetch(url, init); } catch (netErr) { // no readable answer: server down, or an error without CORS headers
      if (API && url.startsWith(API)) {
        // one failed request is not "offline": ask /api/health; if the server answers, the error belongs to this request
        if (await healthOk()) {
          const e = new Error("the server could not answer this request (it is up; the error is for this request only)");
          e.status = "ERR"; li.textContent = `ERR ${Math.round(performance.now() - t0)} ms ${url}: ${e.message}`;
          throw e;
        }
        setOffline(true);
      }
      throw netErr;
    }
    if (API && url.startsWith(API)) setOffline(false);
    const ms = Math.round(performance.now() - t0);
    li.textContent = `${r.status} ${ms} ms ${url}`;
    if (!r.ok) {
      li.className = "bad";
      let msg = "";
      try { msg = (await r.json()).message || ""; } catch { /* not json */ }
      // 429: how long the server asks us to wait (Retry-After, or "try again in N s" in its sentence)
      const wait = r.status === 429 ? (+r.headers.get("Retry-After") || +(msg.match(/try again in (\d+)\s*s/i)?.[1] || 0)
        || (/in a minute/i.test(msg) ? 60 : 0)) : 0;
      // a short wait is not the visitor's business: wait it out once, quietly, and ask again
      if (r.status === 429 && wait > 0 && wait <= 10 && !(init && init.__retried)) {
        li.textContent += ` (waiting ${wait} s, then asking again)`;
        await sleep(wait * 1000 + 300);
        return fetchLogged(url, as, { ...(init || {}), __retried: true });
      }
      const e = new Error(`${r.status} ${msg}`.trim());
      e.status = r.status; e.retryAfter = wait || null;
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

// server errors in words a visitor can act on; the server's own sentence (and error id) is kept after it
function friendly(e, what, archive = "the SPHEREx archive (IRSA)") {
  const code = +e.status, msg = String(e.message || "").replace(/^\d{3}\s*/, "").trim();
  const tail = msg ? ` (server: ${msg})` : "";
  if (code === 429) { // the server says how long to wait ("try again in 404 s"); otherwise about a minute
    const s = +(msg.match(/try again in (\d+)\s*s/i)?.[1] || 0);
    const wait = s > 90 ? `about ${Math.ceil(s / 60)} minutes` : s > 0 ? `${s} seconds` : "a minute";
    return `${what}: too many requests to the live server in a short time. Wait ${wait}, then try again.${tail}`;
  }
  if ([502, 503, 504].includes(code)) return `${what}: ${archive} or the live server is not answering right now. ` +
    `Try again in a few minutes${archive.includes("IRSA") ? "; the five gallery targets work meanwhile" : ""}.${tail}`;
  return `${what} failed: ${msg || e}`;
}
// the status line with a "Try again" button (for a field that could not be loaded)
// with a wait from the server (429 Retry-After), the button counts down and can be used when the wait is over
function statusRetry(text, again, waitS = 0) {
  const b = Object.assign(document.createElement("button"), { type: "button", textContent: "Try again" });
  b.onclick = () => { b.disabled = true; again(); };
  $("status").replaceChildren(text + " ", b); $("status").className = "err";
  if (waitS > 0) {
    let left = Math.ceil(waitS); b.disabled = true;
    const tick = () => {
      if (!b.isConnected) return;
      if (left <= 0) { b.disabled = false; b.textContent = "Try again"; return; }
      b.textContent = `Try again in ${left >= 90 ? `${Math.ceil(left / 60)} min` : `${left} s`}`; left--; setTimeout(tick, 1000);
    };
    tick();
  }
}
// Gaia DR3 stars: never waited for (blink and spectrum go on); the line under the toggle says what happened
function loadGaia() {
  const note = $("gaiaNote"), key = S.key;
  S.gaia = null; note.textContent = "Gaia DR3 stars are loading…";
  fetchLogged(src.gaia(key)).then((g) => {
    if (key !== S.key) return;
    S.gaia = g; note.textContent = g.length ? "" : "No Gaia DR3 stars in this field.";
    draw();
  }).catch((e) => {
    if (key !== S.key) return;
    S.gaia = [];
    const again = Object.assign(document.createElement("a"), { href: "#", textContent: "Try again", onclick: (ev) => { ev.preventDefault(); loadGaia(); } });
    note.replaceChildren(friendly(e, "Gaia DR3 stars", "the Gaia archive (ESA)") + " The image and the spectrum work without them. ", again);
  });
}
// one shared /api/health check (3 s) for failed requests that arrive together
let healthCheck = null;
function healthOk() {
  if (!healthCheck) {
    const ctl = new AbortController(), timer = setTimeout(() => ctl.abort(), 3000);
    healthCheck = fetch(`${API}/api/health`, { signal: ctl.signal }).then((r) => r.ok).catch(() => false)
      .finally(() => { clearTimeout(timer); setTimeout(() => { healthCheck = null; }, 2000); });
  }
  return healthCheck;
}
// server reachability: shown to the user while the API is down; on recovery the band on screen is asked for again
let offline = false;
function setOffline(on) {
  if (on === offline) return;
  offline = on;
  if (on) {
    status("The live server is not reachable right now; retrying every few seconds…", true);
    // ask /api/health every 3 s until it answers (the manifest poll may have stopped already)
    const retry = () => setTimeout(async () => { if (!offline) return; if (await healthOk()) setOffline(false); else retry(); }, 3000);
    retry();
  }
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
  gaia: null, spectrum: null, specAt: null, specAsk: 0, userScale: null,
};

// ---------- WCS (plain TAN from manifest.grid; pixel coords 0-based, y = 0 south) ----------
function world2pix(ra, dec, g = S.m.grid) {
  const a0 = g.crval[0] * D2R, d0 = g.crval[1] * D2R, a = ra * D2R, d = dec * D2R;
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
// "133.795", "08:55:10.831", "08h55m10.831s", "08 55 10.831" (RA in hours when sexagesimal); Dec likewise in degrees
function parseAngle(str, isRa) {
  const t = String(str ?? "").trim().replace(/[−‒–]/g, "-");
  if (!t) return NaN;
  if (/^[+-]?\d+(\.\d*)?$/.test(t)) return +t; // decimal degrees
  const m = t.match(/^([+-]?)(\d+(?:\.\d*)?)\s*[hd:°\s]\s*(\d+(?:\.\d*)?)?\s*[m:'′\s]?\s*(\d+(?:\.\d*)?)?\s*[s"″]?$/i);
  if (!m) return NaN;
  const v = +m[2] + (+m[3] || 0) / 60 + (+m[4] || 0) / 3600, sign = m[1] === "-" ? -1 : 1;
  return sign * v * (isRa ? 15 : 1);
}
// "133.79 -7.24", "133.79, -7.24", "08:55:10.8 -07:14:43", "08h55m10.8s −07d14m43s", "08 55 10.8 -07 14 43"
// -> [ra, dec] in degrees (as text), or null when the text is not a position
function splitCoords(text) {
  const t = String(text || "").trim().replace(/[−‒–]/g, "-");
  if (!t) return null;
  let parts = t.includes(",") ? t.split(",").map((x) => x.trim()) : null;
  if (!parts) {
    const tok = t.split(/\s+/);
    parts = tok.length === 2 ? tok : tok.length === 6 ? [tok.slice(0, 3).join(" "), tok.slice(3).join(" ")] : null;
  }
  if (!parts || parts.length !== 2) return null;
  const ra = parseAngle(parts[0], true), dec = parseAngle(parts[1], false);
  if (!(ra >= 0 && ra < 360 && dec >= -90 && dec <= 90)) return null;
  return [ra.toFixed(6), dec.toFixed(6)];
}
// without a server: does a position fall inside one of the gallery fields? -> {key, name, ra, dec} of the best one
async function galleryFieldAt(ra, dec) {
  if (!Number.isFinite(ra) || !Number.isFinite(dec)) return null;
  const list = [...$("targets").children].map((b) => ({ key: b.dataset.key, name: b.textContent }));
  let best = null;
  for (const t of list) {
    let m; try { m = await (await fetch(src.manifest({ target: t.key }))).json(); } catch { continue; }
    const g = m.grid; if (!g) continue;
    const [x, y] = world2pix(ra, dec, g), [ny, nx] = g.shape;
    if (!(x >= -0.5 && y >= -0.5 && x <= nx - 0.5 && y <= ny - 0.5)) continue;
    const off = Math.hypot(x - (nx - 1) / 2, y - (ny - 1) / 2);
    if (!best || off < best.off) best = { ...t, off, ra: g.crval[0], dec: g.crval[1] };
  }
  return best;
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
  if (DATA_PARAM) p.set("data", DATA); else p.delete("data");
  for (const [k, v] of Object.entries(q)) if (v != null && v !== "" && !(typeof v === "number" && !Number.isFinite(v))) p.set(k, v);
  location.search = p.toString();
}

async function loadTarget() {
  const q = { target: params.get("target"), key: params.get("key"), ra: params.get("ra"), dec: params.get("dec"),
    fov: params.get("fov") || "0.1", pmra: params.get("pmra"), pmdec: params.get("pmdec"),
    ref_epoch: params.get("ref_epoch"), name: params.get("name") };
  // the bare address opens WISE 0855 and plays it (the target buttons stay above); reduced motion: no autoplay
  if (!q.target && !q.key && !(q.ra && q.dec)) {
    q.target = "wise-0855";
    S.autoplay = !matchMedia("(prefers-reduced-motion: reduce)").matches;
    const p = new URLSearchParams(location.search); p.set("target", q.target); history.replaceState(null, "", "?" + p.toString());
  }
  const gallery = !!(q.target || q.key);
  if (!FORCED) {
    // gallery targets open from static files at once (no waiting for the probe); a free position needs the server
    USE_API = !gallery && await serverReady;
    if (!gallery && !USE_API) {
      // a position inside one of the gallery fields still works without a server: open that field
      if (!$("targets").children.length) await loadIndex();
      const ra = parseAngle(q.ra, true), dec = parseAngle(q.dec, false), hit = await galleryFieldAt(ra, dec);
      // open that gallery target; "at" keeps the typed position so the note (and a shared link) can say why
      if (hit) { navigate({ target: hit.key, at: `${ra.toFixed(6)},${dec.toFixed(6)}` }); return; }
      if (NO_SERVER) noServer($("status"), "");
      else status("The live server is not reachable right now, so only the five gallery targets are available.", true);
      return;
    }
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
    statusRetry(friendly(e, "This field could not be loaded"), loadTarget, e.retryAfter || 0); return;
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
  // the stage heading: the target's name and where it is
  $("tname").textContent = S.m.name || S.key;
  // "← Sky" goes back to the start page on the same place (a gallery target by key, a free field by position)
  $("home").href = gallery ? `../?target=${enc(S.key)}`
    : `../?ra=${enc(String(S.m.query?.ra ?? ""))}&dec=${enc(String(S.m.query?.dec ?? ""))}`;
  // the target in the top bar is marked as the current one (when the button list is there)
  const markCurrent = () => [...$("targets").children].forEach((b) => b.setAttribute("aria-current", String(b.dataset.key === S.key)));
  markCurrent(); setTimeout(markCurrent, 800);
  const qy = S.m.query || {};
  $("tcoord").textContent = qy.ra != null ? fmtCoord(+qy.ra, +qy.dec) : "";
  renderMeta();
  const items = (S.m.notes || []).map((n) => Object.assign(document.createElement("li"), { textContent: n }));
  // the target's notes: one box that can be closed (closed in the embed, so the player fits the frame)
  $("notes").replaceChildren(...items);
  const syncNotes = () => {
    const n = $("notes").children.length;
    $("notesBox").hidden = !n; $("notesSum").textContent = `${n} note${n === 1 ? "" : "s"} on this target`;
  };
  $("notesBox").open = !EMBED;
  // opened from a typed position inside this gallery field (no server): say so above the notes
  const typed = (params.get("at") || "").split(",").map(Number);
  if (gallery && typed.length === 2 && typed.every(Number.isFinite)) {
    const g = S.m.grid;
    $("notes").prepend(Object.assign(document.createElement("li"), { textContent:
      `This position (RA ${typed[0].toFixed(5)}°, Dec ${typed[1].toFixed(5)}°) lies in the ${S.m.name || S.key} gallery field ` +
      `(centre RA ${(+g.crval[0]).toFixed(5)}°, Dec ${(+g.crval[1]).toFixed(5)}°); showing that field.` }));
  }
  syncNotes();
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
  {
    // the stage's size comes from the skin (CSS --P): the drawing takes the largest whole zoom that the stage covers,
    // rounded down so the image is scaled up a little, never down (that would break the thin marks and text).
    // A zoom in the address can raise it but not lower it in the full view: the image is shown at the stage's size
    // anyway, and a low zoom (an old link, or a page first opened while hidden) would only make it blurred.
    const cssW = parseFloat(getComputedStyle($("canvas")).width) || 0;
    const nx = S.m.grid.shape[1], room = EMBED ? 420 : cssW > 100 ? cssW : stageSize();
    const fit = Math.max(1, Math.min(6, Math.floor(room / nx))), asked = +params.get("zoom") || 0;
    S.zoom = EMBED ? (asked || fit) : Math.max(fit, Math.min(6, asked)); $("zoom").value = String(S.zoom);
  }
  const bigField = USE_API && (+S.m.query?.fov_deg || 0) > 0.2;
  status(bigField
    ? `${S.m.epochs.length} epochs, ${S.m.bands.length} bands. ${(+S.m.query.fov_deg * 60).toFixed(0)}′ field: the server reads about 120 to 190 MB ` +
      `per band from the archive, so the first band (${S.band}) usually takes 30–70 s; the other bands follow.`
    : `${S.m.epochs.length} epochs, ${S.m.bands.length} bands. Loading ${S.band}…`);

  loadGaia();
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
  if (S.autoplay) { S.autoplay = false; setPlaying(true); }
  // static: fetch the other bands in the background, one after another
  for (const b of S.m.bands) if (b.id !== S.band && b.status !== "empty") await loadBand(b.id).catch(() => {});
}

function loadSpectrum(ra, dec, where) {
  // the target: no ra/dec, so the server follows it to each exposure with its proper motion
  // (a fixed point would miss a fast target in the earlier epochs); ra/dec only for other places
  if (where === "target") ra = dec = null;
  return fetchLogged(src.spectrum(S.key, ra, dec)).then((s) => { S.spectrum = s; S.specAt = where; S.specErr = ""; plots(); })
    .catch((e) => { // say so in the plot instead of keeping an older, emptier answer on screen
      if (where !== "target") return;
      const again = !S.specRetried; S.specRetried = true; // one quiet second try for the target's spectrum
      S.specErr = friendly(e, "Spectrum") + (again ? " Asking again in 15 s." : "");
      plots();
      if (again) setTimeout(() => loadSpectrum(ra, dec, "target"), 15000);
    });
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function pollManifest() {
  const busy = () => S.m.bands.some((b) => b.status === "pending");
  let hadPending = busy();
  // ask again every 3 s at first, then less often (at most every 10 s): the public server allows 20 manifest requests
  // a minute per address, shared by everyone behind it
  let gap = 3000;
  while (busy() || offline) { // keep polling while the server is down, so the page recovers by itself
    await sleep(gap); gap = Math.min(10000, Math.round(gap * 1.5));
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
    const ask = isCustom(band) && !API
      ? serverReady.then((ok) => (ok ? fetchLogged(src.band(S.key, band))
        : Promise.reject(new Error(NO_SERVER ? NOSRV_TXT : "Your own wavelength window needs the live server, which is not reachable right now."))))
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
// a pass's date and time are those of the selected band's own frame (each band is a different set of exposures);
// the manifest's epoch values only stand in until the band file is loaded
const frameDate = (band, i) => bandEpoch(band, i)?.date || S.m.epochs[i].date;
const frameMjd = (band, i) => +(bandEpoch(band, i)?.mjd_mid ?? S.m.epochs[i].mjd_mid);
// "RA 133.7839°  Dec −7.2443°", as on the start page (4 decimals, the true minus sign, two spaces)
const fmtCoord = (ra, dec) => `RA ${ra.toFixed(4)}°  Dec ${dec < 0 ? "−" : ""}${Math.abs(dec).toFixed(4)}°`;
// "12 Nov 2025", as on the start page
const fmtDate = (iso) => { const d = new Date(String(iso).slice(0, 10) + "T00:00:00Z");
  return isNaN(d) ? String(iso || "") : d.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" }); };
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
// the stage's image size: as tall as the window allows above the time strip, leaving the right column ≥ 440 px
function stageSize() {
  if (innerWidth < 1000) return Math.max(280, innerWidth - 32);
  const gut = Math.min(48, Math.max(16, innerWidth * 0.032));
  // top bar ~72, gaps ~48, legend line ~30, time strip ~190
  return Math.round(Math.max(360, Math.min(innerHeight - 340, innerWidth - 2 * gut - 64 - 440)));
}
// the time strip: every pass over the field is a dated stop (low coverage faint, no data dashed); the WISE image,
// when the field has one, is the first stop and is labelled as a different instrument
function renderEpochMarks() {
  if (!S.m?.epochs) return;
  const stops = [];
  if (S.wise) {
    const yr = (S.wise.date || "").slice(0, 4) || "WISE";
    const w = document.createElement("button");
    w.type = "button"; w.className = "stop wise" + (S.onWise ? " cur" : "");
    w.setAttribute("aria-pressed", String(!!S.onWise));
    w.innerHTML = `<b>${escapeHtml(yr)} · WISE</b><span>${escapeHtml(S.wiseBand)}</span><small>not SPHEREx</small>`;
    w.title = `WISE ${S.wiseBand}, AllWISE tile middle ${S.wise.date ? fmtDate(S.wise.date) : "(date not given)"}: not SPHEREx; click to blink it with the SPHEREx passes`;
    w.onclick = () => {
      if (!S.wiseOn) { S.wiseOn = true; $("wiseOn").checked = true; wiseSlider(); }
      S.onWise = true; $("epoch").value = -1; draw(); syncUrl();
    };
    stops.push(w, Object.assign(document.createElement("span"), { className: "gap", textContent: "…", ariaHidden: "true" }));
  }
  stops.push(...S.m.epochs.map((e, i) => {
    const b = document.createElement("button"), low = isLow(S.band, i), empty = emptyEpoch(S.band, i);
    const n = bandEpoch(S.band, i)?.n_used, cur = i === S.epoch && !S.onWise;
    b.type = "button"; b.className = "stop" + (empty ? " empty" : low ? " low" : "") + (cur ? " cur" : "");
    b.setAttribute("aria-pressed", String(cur));
    const day = fmtDate(frameDate(S.band, i));
    // three short lines that fit the card at its narrowest (date, pass, exposures); the full text is in the tooltip
    b.innerHTML = `<b>${escapeHtml(day)}</b><span>pass ${i + 1}${low && !empty ? " · low" : ""}</span><small>${empty ? "no data" : `${n ?? "?"} exp.`}</small>`;
    b.title = empty ? `${day}: no data in ${S.band} (skipped in playback)`
      : low ? `low coverage: ${n ?? "?"} exposures — ${S.showLow ? "shown" : "skipped"}` : `SPHEREx pass ${i + 1}: ${S.band} frame of ${day}, MJD ${frameMjd(S.band, i).toFixed(2)}, ${n ?? "?"} exposures`;
    b.disabled = empty;
    b.onclick = () => { $("epoch").value = i; $("epoch").oninput({ target: $("epoch") }); };
    return b;
  }));
  $("epochMarks").replaceChildren(...stops);
  const cur = S.onWise ? null : S.m.epochs[S.epoch]?.id;
  for (const id of ["spec", "lc"]) $(id).setAttribute("class", cur == null ? "" : `cur-${cur}`);
}
// legend under the image: only the layers that are switched on, drawn as on the image
function renderLegend() {
  const it = (on, svg, text) => (on ? `<span><svg viewBox="0 0 16 16" aria-hidden="true">${svg}</svg>${text}</span>` : "");
  const g = (c, extra = "") => `fill="none" stroke="${c}" stroke-width="1.5" ${extra}`;
  $("layerLegend").innerHTML = [
    it($("ovTrack").checked && S.m?.target_track?.length, `<path d="M8 1v4M8 11v4M1 8h4M11 8h4" ${g(INK.fg)}/>`, "target (expected position)"),
    it($("ovQuery").checked, `<path d="M2 8h4M10 8h4M8 2v4M8 10v4" ${g(INK.fg3)}/>`, "query position"),
    it($("ovGaia").checked && S.gaia?.length, `<circle cx="8" cy="8" r="5" ${g(INK.fg4)}/>`, "Gaia DR3 star"),
    it(S.catwise && $("ovCat").checked, `<rect x="3" y="3" width="10" height="10" ${g(INK.fg3)}/>`, "CatWISE2020 source"),
    it(S.cands && $("ovCand").checked, `<circle cx="8" cy="8" r="5.5" ${g(INK.fg2, 'stroke-dasharray="2.5 2"')}/>`, "automatic candidate"),
    it(S.marks.length, `<circle cx="8" cy="8" r="5" ${g(INK.ember)}/>`, "your marks"),
  ].join("");
}
// the side panel: one section at a time (the embed shows its few controls without tabs)
// the extra tools under the data: closed at first; a tab opens its section, the same tab closes it again
// one strip and one slot for the data and the tools (Spectrum / Light curve / PSF wings / View / Layers / Measure /
// About): a tool opens in the slot where the plot was, so nothing else on the page moves; its tab again goes back
function setTab(name) {
  if (!name) { setPlot(S.plot || "specBox"); return; }
  S.tab = name;
  document.querySelectorAll("#plotTabs [data-tab]").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === name)));
  document.querySelectorAll("#plotTabs [data-plot]").forEach((b) => b.setAttribute("aria-selected", "false"));
  document.querySelectorAll(".plotPane").forEach((p) => p.classList.add("off"));
  document.querySelectorAll(".tabpane").forEach((p) => { p.hidden = !EMBED && p.dataset.pane !== name; });
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
    (a) => {
      // a frame of another grid size must never be drawn on this grid (it would come out sheared)
      const [ny, nx] = S.m.grid.shape;
      if (a.length !== nx * ny) {
        S.frames.set(k, null); S.gridMismatch = true; (S.badFrames ||= new Set()).add(k);
        status(`A ${band} frame does not match this field's grid (${a.length * 4} bytes, expected ${nx * ny * 4} for ${nx}×${ny}); ` +
          "it is not drawn. Reloading the page usually fixes this.", true);
        if (band === S.band) draw(); return null;
      }
      S.frames.set(k, a); if (band === S.band) draw(); return a;
    },
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
    ctx.fillStyle = INK.fg4; ctx.font = "15px 'IBM Plex Mono', ui-monospace, monospace";
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
    $("epochLabel").textContent = `WISE ${S.wiseBand} · ${fmtDate(S.wise.date) || "date not given"} · not SPHEREx (${S.wise.survey})`;
    $("plateCap").textContent = `${(S.wise.date || "").slice(0, 4)} · WISE ${S.wiseBand} · NOT SPHEREX`;
    $("epochLabel").title = S.wise.mjd != null ? `MJD ${(+S.wise.mjd).toFixed(2)}` : "";
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
  // "Pass 1 of 3 · 6 May 2025 · 5 exposures stacked"; the MJD details are in the tooltip
  $("epochLabel").textContent = `Pass ${S.epoch + 1} of ${S.m.epochs.length} · ${fmtDate(frameDate(S.band, S.epoch))} · ` +
    // when the server stacked only part of what was listed (it caps busy fields), both numbers are said
    (used != null ? (n != null && n > used ? `${used} of ${n} exposures stacked` : `${used} exposure${used === 1 ? "" : "s"} stacked`)
      : `${n ?? "?"} exposures listed`);
  { const b = S.m.bands.find((x) => x.id === S.band), rng = String(b?.label || "").replace(/\s*µm$/, "");
    const d = frameDate(S.band, S.epoch), dd = new Date(String(d).slice(0, 10) + "T00:00:00Z");
    const day = isNaN(dd) ? String(d) : dd.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" }).toUpperCase();
    $("plateCap").textContent = `PASS ${S.epoch + 1}/${S.m.epochs.length} · ${day} · ${S.band} ${rng} µm` +
      (used != null ? ` · ${used} EXP. STACKED` : ""); }
  $("epochLabel").title = `${S.band} frame MJD ${frameMjd(S.band, S.epoch).toFixed(2)}; pass MJD ${(+ep.mjd_min).toFixed(2)}–${(+ep.mjd_max).toFixed(2)}` +
    `; ${n ?? "?"} exposures listed` + (used != null ? `, ${used} used` : "");

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
        if (diverging) { // blue-white-red: negative and positive changes must differ at a glance
          const u = t * 2 - 1;
          img.data.set(u < 0 ? [255 * (1 + u), 255 * (1 + u), 255, 255] : [255, 255 * (1 - u), 255 * (1 - u), 255], o);
        } else { const g = 255 * t; img.data.set([g, g, g, 255], o); }
      }
    }
    oc.putImageData(img, 0, 0);
    ctx.imageSmoothingEnabled = false;
    ctx.drawImage(off, 0, 0, nx * z, ny * z);
  } else {
    ctx.fillStyle = INK.fg4; ctx.font = "15px 'IBM Plex Mono', ui-monospace, monospace";
    const pending = S.frames.get(`${S.band}|${ep.id}|${S.layer}`) instanceof Promise;
    const bad = S.badFrames?.has(`${S.band}|${S.m.epochs[S.epoch].id}|${S.layer}`);
    ctx.fillText(pending ? "loading…" : offline ? "server not reachable, retrying…"
      : bad ? "this frame does not match the field's grid, so it is not drawn" : "no data for this band/epoch", 12, 24);
  }
  overlays(ctx, ep);
  lowLabel(ctx);
}

// ---------- measure / mark ----------
// Marks are made by the user on co-added frames: {n, ra, dec, mjd (frame's mjd_mid), epoch, date, band}.
// The motion is a straight-line fit of position against time over all marks; the uncertainty assumes the click
// is good to about one grid pixel per mark. Everything here runs in the browser, without the server.
S.marks = []; S.measuring = false; S.markSeq = 0;
const MARK_COLOR = INK.ember; // the user's own marks carry the one accent
function addMark(ra, dec) {
  if (S.onWise) { // a mark on the WISE frame carries the WISE date; without one it cannot be timed
    if (S.wise.mjd == null) { status("This WISE image has no date in wise.json, so a mark on it cannot be timed; mark the SPHEREx frames instead.", true); return; }
    S.marks.push({ n: ++S.markSeq, ra, dec, mjd: +S.wise.mjd, epoch: -1, date: S.wise.date || `MJD ${S.wise.mjd}`, band: `WISE ${S.wiseBand}` });
    renderMarks(); draw(); return;
  }
  const ep = S.m.epochs[S.epoch];
  // the time of the frame on screen: the band's own frame (rgb mixes bands, so the pass's middle)
  const rgb = S.mode === "rgb";
  S.marks.push({ n: ++S.markSeq, ra, dec, mjd: rgb ? ep.mjd_mid : frameMjd(S.band, S.epoch), epoch: S.epoch,
    date: rgb ? ep.date : frameDate(S.band, S.epoch), band: rgb ? "rgb" : S.band });
  renderMarks(); draw();
}
function removeMark(n) { S.marks = S.marks.filter((m) => m.n !== n); renderMarks(); draw(); }
function clickHint() {
  $("clickHint").textContent = S.measuring ? "Measure is on: click the object to mark it in this frame"
    : NO_SERVER ? "Click the image: a spectrum at that point needs the live server" : "Click the image: the spectrum at that point";
}
function setMeasuring(on) {
  S.measuring = on; clickHint();
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
    ctx.font = "bold 14px 'IBM Plex Mono', ui-monospace, monospace"; ctx.lineWidth = 3; ctx.strokeStyle = "#000"; ctx.strokeText(String(m.n), p[0] + r + 2, p[1] - r);
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
  const why = !srv ? (NO_SERVER ? NOSRV_TXT : "Refining and reports need the live server, which is not reachable right now.")
    : n < 2 ? "Mark the object in at least two frames to refine it on single exposures." : "";
  $("refineBtn").disabled = !!why;
  if (!srv) noServer($("refineWhy"), "Refining and reports"); else $("refineWhy").textContent = why;
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
// single exposures belong to one detector band (D1–D6); W and own windows combine bands, so the detector band
// that overlaps the selected band's wavelengths most is used (W 4.3–5.0 µm -> D6 4.42–5.00 µm)
function detectorBand(id) {
  if (/^D[1-6]$/.test(id)) return id;
  const w = S.m?.bands?.find((b) => b.id === id)?.wave_um;
  const ds = (S.m?.bands || []).filter((b) => /^D[1-6]$/.test(b.id) && b.wave_um);
  if (!w || !ds.length) return "D6";
  const over = (b) => Math.min(w[1], b.wave_um[1]) - Math.max(w[0], b.wave_um[0]);
  return ds.reduce((best, b) => (over(b) > over(best) ? b : best)).id;
}
async function refine() {
  const db = detectorBand(S.band), swapped = db !== S.band;
  $("refineResult").textContent = `Measuring the object on single exposures${swapped ? ` in ${db} (${S.band} combines detector bands; single exposures are measured in ${db}, the detector band that overlaps it most)` : ""}…`;
  S.track = null; updateRefineButtons();
  const body = { key: S.key, band: db, marks: [...S.marks].sort((a, b) => a.mjd - b.mjd).map((m) => ({ ra: m.ra, dec: m.dec, mjd: m.mjd, band: /^D[1-6]$/.test(m.band) ? m.band : db })) };
  try {
    const j = MOCK ? await fetchLogged(`dev-fixtures/track_${MOCK}.json`)
      : await fetchLogged(`${API}/api/track`, "json", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    S.track = normTrack(j); S.track.bandNote = swapped ? `Single exposures measured in ${db} (marks made in ${S.band}).` : ""; renderTrack();
  } catch (e) { $("refineResult").textContent = friendly(e, "Refining"); }
  updateRefineButtons();
}
function renderTrack() {
  const t = S.track, ok = t.meas.filter((m) => m.ok).sort((a, b) => a.mjd - b.mjd);
  const f = fitMotion(ok.map((m) => ({ ra: m.ra, dec: m.dec, mjd: m.mjd, sig: Math.hypot(m.rmsRa, m.rmsDec) / Math.SQRT2 })));
  const head = document.createElement("div");
  head.className = "mono";
  const c = t.counts, cnt = c.found != null ? `\n${c.found} exposures found, ${c.measured} measured, ${c.accepted} accepted, ${c.draft} in the MPC draft` : "";
  head.textContent = (t.bandNote ? t.bandNote + "\n" : "") + `${t.regime}${t.regimeReason ? ": " + t.regimeReason : ""}` + cnt +
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
  if (/^https:\/\/(www\.)?minorplanetcenter\.net\//.test(String(t.testForms.psv || ""))) $("reportDlg").querySelector('a[href*="submit_psv_test"]').href = t.testForms.psv;
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
  ctx.font = "500 14px 'IBM Plex Mono', ui-monospace, monospace";
  const w = ctx.measureText(txt).width + 12, x = (ctx.canvas.width - w) / 2;
  ctx.fillStyle = "rgba(0,0,0,0.75)"; ctx.fillRect(x, 6, w, 22);
  ctx.fillStyle = INK.fg; ctx.fillText(txt, x + 6, 22);
}

function toScreen(ra, dec) {
  const [x, y] = world2pix(ra, dec), z = S.zoom, ny = S.m.grid.shape[0];
  return [(x + 0.5) * z, (ny - 1 - y + 0.5) * z];
}

function overlays(ctx, ep) {
  const z = S.zoom;
  ctx.lineWidth = 1.5;
  if ($("ovGaia").checked && S.gaia && ep.mjd_mid != null) { // no date (a WISE frame without epoch): not drawn
    ctx.strokeStyle = INK.fg4; // Gaia: grey circles
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
    ctx.strokeStyle = ctx.fillStyle = INK.fg; // target track: off-white ticks and dots
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
    ctx.strokeStyle = INK.fg3; // query position: grey cross
    ctx.beginPath(); ctx.moveTo(x - r, y); ctx.lineTo(x - r / 3, y); ctx.moveTo(x + r / 3, y); ctx.lineTo(x + r, y);
    ctx.moveTo(x, y - r); ctx.lineTo(x, y - r / 3); ctx.moveTo(x, y + r / 3); ctx.lineTo(x, y + r); ctx.stroke();
  }
  if (S.specAt && S.specAt !== "target") {
    const [x, y] = toScreen(...S.specAt);
    ctx.strokeStyle = "#fff"; ctx.strokeRect(x - 3 * z, y - 3 * z, 6 * z, 6 * z);
  }
  // scale bar (30″) and compass: north up, east left
  const len = (30 / pixArcsec()) * z, H = ctx.canvas.height;
  ctx.font = "400 13px 'IBM Plex Mono', ui-monospace, monospace";
  // quiet, as on the start page; a thin dark outline keeps them readable on a white (difference) background
  const label = (t, x, y) => { ctx.lineWidth = 2; ctx.strokeStyle = "rgba(0,0,0,.6)"; ctx.strokeText(t, x, y); ctx.fillStyle = "rgba(242,242,240,.7)"; ctx.fillText(t, x, y); };
  ctx.lineWidth = 4; ctx.strokeStyle = "#000"; ctx.beginPath(); ctx.moveTo(10, H - 12); ctx.lineTo(10 + len, H - 12); ctx.stroke();
  ctx.lineWidth = 1.5; ctx.strokeStyle = "#eee"; ctx.beginPath(); ctx.moveTo(10, H - 12); ctx.lineTo(10 + len, H - 12); ctx.stroke();
  label(`30″  (grid ${pixArcsec().toFixed(2)}″/px)`, 10, H - 18);
  label("N ↑  E ←", ctx.canvas.width - ctx.measureText("N ↑  E ←").width - 10, 20); // placed by its own width
  drawCatwise(ctx, ep.mjd_mid);
  drawCandidates(ctx, ep);
  drawMarks(ctx);
  renderLegend();
}

// ---------- plots (spectrum + light curve) ----------
// design/v5 palette: off-white and greys plus one accent (ember); epochs differ by tone and marker shape, not hue
const EPOCH_TONES = ["#f2f2f0", "#c9c9c5", "#a3a3a0", "#e6e1d6", "#b5b5b0", "#8d8d93"];
const EPOCH_SHAPES = ["circle", "square", "up", "down", "x", "plus"]; // the diamond stays for points recovered from overflow
const epochTone = (e) => EPOCH_TONES[(e ?? 0) % EPOCH_TONES.length];
// one epoch marker as SVG: filled, or open (lower limits); x and plus are always strokes
function epochMarker(e, cx, cy, r, open, extra = "") {
  const shape = EPOCH_SHAPES[(e ?? 0) % EPOCH_SHAPES.length], c = epochTone(e), x = +cx, y = +cy, f = (v) => v.toFixed(1);
  const paint = open || shape === "x" || shape === "plus" ? `fill="none" stroke="${c}" stroke-width="1.2"` : `fill="${c}"`;
  if (shape === "circle") return `<circle cx="${f(x)}" cy="${f(y)}" r="${r}" ${paint} ${extra}/>`;
  if (shape === "square") return `<rect x="${f(x - r * 0.9)}" y="${f(y - r * 0.9)}" width="${f(r * 1.8)}" height="${f(r * 1.8)}" ${paint} ${extra}/>`;
  if (shape === "up") return `<path d="M${f(x)} ${f(y - r * 1.15)}L${f(x + r)} ${f(y + r * 0.75)}H${f(x - r)}Z" ${paint} ${extra}/>`;
  if (shape === "down") return `<path d="M${f(x)} ${f(y + r * 1.15)}L${f(x + r)} ${f(y - r * 0.75)}H${f(x - r)}Z" ${paint} ${extra}/>`;
  if (shape === "x") return `<path d="M${f(x - r)} ${f(y - r)}L${f(x + r)} ${f(y + r)}M${f(x + r)} ${f(y - r)}L${f(x - r)} ${f(y + r)}" ${paint} ${extra}/>`;
  return `<path d="M${f(x - r * 1.2)} ${f(y)}H${f(x + r * 1.2)}M${f(x)} ${f(y - r * 1.2)}V${f(y + r * 1.2)}" ${paint} ${extra}/>`;
}
// about n round tick values between a and b (1, 2 or 5 times a power of ten apart)
function niceTicks(a, b, n) {
  if (!(b > a)) return { ticks: [a], step: 1 };
  const raw = (b - a) / n, p = 10 ** Math.floor(Math.log10(raw)), f = raw / p;
  const step = (f < 1.5 ? 1 : f < 3.5 ? 2 : f < 7.5 ? 5 : 10) * p, ticks = [];
  for (let v = Math.ceil(a / step) * step; v <= b + step * 1e-9; v += step) ticks.push(+v.toPrecision(12));
  return { ticks, step };
}
// a plot's legend under it, in HTML: the epochs (tone and marker) and only the kinds of point that are in the plot
function plotLegend(svg, kinds) {
  let el = document.getElementById(svg.id + "Legend");
  if (!el) { el = Object.assign(document.createElement("div"), { id: svg.id + "Legend", className: "plotLegend mono" }); svg.after(el); }
  const sw = (inner) => `<svg viewBox="0 0 14 14" aria-hidden="true">${inner}</svg>`;
  // the light curve is one band: its frames' own dates, as in the time strip; the spectrum mixes bands: "pass N" only
  const ep = (S.m?.epochs || []).map((e, i) => `<span>${sw(epochMarker(e.id, 7, 7, 3.6, false))}pass ${i + 1}` +
    `${svg.id === "lc" ? ` · ${escapeHtml(fmtDate(frameDate(S.band, i)))}` : ""}</span>`);
  const k = [
    kinds.err && `<span>${sw(`<path d="M7 1v12M4 1h6M4 13h6" stroke="${INK.fg3}" stroke-width="1.2"/>`)}error bar (±1σ)</span>`,
    kinds.flagged && `<span>${sw(`<circle cx="7" cy="7" r="3.6" fill="none" stroke="${INK.fg3}" stroke-width="1.2" opacity="0.6"/>`)}flagged (open)</span>`,
    kinds.zero && `<span>${sw(`<circle cx="7" cy="7" r="3.6" fill="${INK.fg3}" opacity="0.3"/>`)}consistent with zero (faint)</span>`,
    kinds.sat && `<span>${sw(`<circle cx="7" cy="10" r="2.6" fill="none" stroke="${INK.fg3}"/><path d="M7 7.4V1.5M4.8 3.7 7 1.5l2.2 2.2" fill="none" stroke="${INK.fg3}"/>`)}saturated: lower limit</span>`,
    kinds.over && `<span>${sw(`<path d="M7 3l4 4-4 4-4-4z" fill="${INK.fg3}"/>`)}recovered from overflow</span>`,
  ].filter(Boolean);
  el.innerHTML = ep.join("") + (k.length ? `<span class="sep"></span>` + k.join("") : "");
}
// empty, waiting or failed: the same quiet box inside the plot frame
// plots are drawn in the svg's own pixels, so text keeps its size at any width
function plotSize(svg) {
  const W = Math.max(300, Math.round(svg.getBoundingClientRect().width || 460)), H = W < 520 ? 240 : 300;
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  return [W, H];
}
function plotMessage(svg, text) {
  const [W, H] = plotSize(svg);
  svg.innerHTML = `<rect x="1" y="1" width="${W - 2}" height="${H - 2}" fill="none" stroke="${INK.rule}" stroke-dasharray="3 4"/>` +
    `<text x="${W / 2}" y="${H / 2}" fill="${INK.fg3}" font-size="14" text-anchor="middle">${escapeHtml(text)}</text>`;
  document.getElementById(svg.id + "Legend")?.replaceChildren();
}
function scatter(svg, pts, xk, yk, xlabel, ylabel, empty = "No points to show here.") {
  const [W, H] = plotSize(svg), L = 62, B = 42, T = 10, R = 14;
  if (!pts.length) { plotMessage(svg, empty); return; }
  const xs = pts.map((p) => p[xk]), good = pts.filter((p) => !p.flagged);
  const ys = (good.length ? good : pts).map((p) => p[yk]);
  // y: nearly all points (0.5–99.5 %) plus a margin, so the outermost points are not pressed against the frame
  const [x0, x1] = [Math.min(...xs), Math.max(...xs)], [ya, yb] = percentile([Float32Array.from(ys)], [0.5, 99.5]);
  const pad = (yb - ya) * 0.06 || Math.abs(yb) * 0.1 || 1, y0 = ya - pad, y1 = yb + pad;
  const sx = (x) => L + ((x - x0) / (x1 - x0 || 1)) * (W - L - R);
  Object.assign(svg.dataset, { x0, x1, L, R, W, T, B, H }); // for the wavelength-window drag on the spectrum
  const sy = (y) => H - B - ((y - y0) / (y1 - y0 || 1)) * (H - B - T);
  // axes: light grid at round values, numbers on both axes, and a title for each axis with its unit
  const fmt = (v, step) => (Math.abs(step) >= 1 ? v.toFixed(0) : v.toFixed(Math.min(4, Math.ceil(-Math.log10(step)))));
  const xt = niceTicks(x0, x1, 5), yt = niceTicks(y0, y1, 4);
  let h = `<rect x="${L}" y="${T}" width="${W - L - R}" height="${H - B - T}" fill="none" stroke="${INK.rule}"/>`;
  for (const v of yt.ticks) {
    const y = sy(v).toFixed(1);
    h += `<line x1="${L}" x2="${W - R}" y1="${y}" y2="${y}" stroke="rgba(242,242,240,0.07)"/>` +
      `<text x="${L - 5}" y="${(+y + 4).toFixed(1)}" fill="${INK.fg3}" font-size="13" text-anchor="end">${fmt(v, yt.step)}</text>`;
  }
  for (const v of xt.ticks) {
    const x = sx(v).toFixed(1);
    h += `<line x1="${x}" x2="${x}" y1="${T}" y2="${H - B}" stroke="rgba(242,242,240,0.07)"/>` +
      `<text x="${x}" y="${H - B + 15}" fill="${INK.fg3}" font-size="13" text-anchor="middle">${fmt(v, xt.step)}</text>`;
  }
  const xTitle = xlabel === "µm" ? "wavelength (µm)" : xlabel === "MJD" ? "date (MJD)" : String(xlabel);
  const yTitle = /^flux|brightness/i.test(String(ylabel)) ? String(ylabel) : `flux (${ylabel})`;
  h += `<text x="${((L + W - R) / 2).toFixed(0)}" y="${H - 6}" fill="${INK.fg2}" font-size="13" text-anchor="middle">${escapeHtml(xTitle)}</text>`;
  h += `<text transform="translate(13 ${((T + H - B) / 2).toFixed(0)}) rotate(-90)" fill="${INK.fg2}" font-size="13" text-anchor="middle">${escapeHtml(yTitle)}</text>`;
  const kinds = {};
  // error bars first, under the markers (not for lower limits)
  if (yk === "flux") for (const p of pts) {
    if (p.saturated || !(+p.err > 0)) continue;
    const x = sx(p[xk]).toFixed(1), ya = Math.max(T, sy(p[yk] + +p.err)), yb = Math.min(H - B, sy(p[yk] - +p.err));
    if (yb - ya < 1.5) continue;
    kinds.err = true;
    h += `<line data-ep="${p.epoch}" x1="${x}" x2="${x}" y1="${ya.toFixed(1)}" y2="${yb.toFixed(1)}" stroke="${epochTone(p.epoch)}" stroke-width="0.8" opacity="${p.flagged ? 0.25 : 0.55}"/>`;
  }
  for (const p of pts) {
    const c = epochTone(p.epoch);
    // flagged: the open marker; consistent with zero given the removed neighbour (|flux| < 0.15 × neighbor_flux): faint
    const flagged = p.flagged && !p.overflow, zero = !flagged && p.neighbor_flux > 0 && Math.abs(p[yk]) < 0.15 * p.neighbor_flux;
    const faint = flagged || zero;
    const cx = sx(p[xk]).toFixed(1), cy = Math.max(T, Math.min(H - B, sy(p[yk])));
    // hover text: the point's values and the names of the FLAGS bits set in its aperture (F3)
    const tip = `<title>${escapeHtml(pointTip(p, yk))}</title>`;
    if (p.saturated && yk === "flux") { // lower limit: open marker with an upward tick
      kinds.sat = true;
      h += `<g data-ep="${p.epoch}" stroke="${c}" fill="none" opacity="0.7">${epochMarker(p.epoch, cx, cy, 2.6, true)}` +
        `<path d="M${cx} ${(cy - 2.6).toFixed(1)}v-7m-2.5 2.5l2.5-2.5 2.5 2.5"/>${tip}</g>`;
    } else if (p.overflow && yk === "flux") { // recovered from overflow: a valid measurement, filled diamond
      kinds.over = true;
      h += `<path data-ep="${p.epoch}" d="M${cx} ${(cy - 3.2).toFixed(1)}l3.2 3.2-3.2 3.2-3.2-3.2z" fill="${c}" opacity="${zero ? 0.3 : 1}">${tip}</path>`;
    } else if (flagged) { // flagged: the epoch's marker, open
      kinds.flagged = true;
      h += `<g data-ep="${p.epoch}" opacity="0.6">${epochMarker(p.epoch, cx, cy, 2.8, true)}${tip}</g>`;
    } else {
      if (zero) kinds.zero = true;
      h += `<g data-ep="${p.epoch}" opacity="${faint ? 0.3 : 1}">${epochMarker(p.epoch, cx, cy, 2.8, false)}${tip}</g>`;
    }
  }
  svg.innerHTML = h;
  plotLegend(svg, kinds);
}
// the loaded spectrum as CSV, made in the browser: one column per point field, units and meta notes as # comments
function spectrumCsv() {
  const raw = S.spectrum, pts = Array.isArray(raw) ? raw : raw?.points || [], meta = raw?.meta || {};
  const cols = [...new Set(pts.flatMap((p) => Object.keys(p)))];
  const cell = (v) => {
    if (v == null) return "";
    const t = Array.isArray(v) ? v.join("|") : typeof v === "object" ? JSON.stringify(v) : String(v);
    return /[",\n#]/.test(t) ? `"${t.replace(/"/g, '""')}"` : t;
  };
  const one = (t) => String(t).replace(/\s*\n\s*/g, " ");
  const where = S.specAt && S.specAt !== "target" ? `point RA ${S.specAt[0].toFixed(6)}, Dec ${S.specAt[1].toFixed(6)}` : "target";
  const head = [
    `SPHEREx Blink spectrum: ${S.m?.name || S.key} (${S.key}), ${where}`,
    `units: flux and err in ${meta.flux_unit || "the flux unit of the data"}; wavelength_um and bandwidth_um in micrometres; mjd in days (MJD)`,
    "flags: names of the FLAGS bits set in the aperture, separated by |; points with masked_fraction >= 0.999 have no usable pixel (the page does not draw them)",
    ...Object.entries(meta).filter(([, v]) => v != null && typeof v !== "object").map(([k, v]) => `${k}: ${one(v)}`),
    ...Object.entries(meta).filter(([, v]) => v && typeof v === "object").map(([k, v]) => `${k}: ${one(JSON.stringify(v))}`),
    "data: SPHEREx (NASA/IPAC IRSA); see the page's acknowledgement",
  ];
  return head.map((h) => `# ${h}`).join("\n") + "\n" + cols.join(",") + "\n" +
    pts.map((p) => cols.map((c) => cell(p[c])).join(",")).join("\n") + "\n";
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
  // aperture photometry: masked share of the aperture weight; PSF photometry: of the pixels within 1.5 px
  if (p.masked_fraction != null && Number.isFinite(+p.masked_fraction)) lines.push(`masked ${Math.min(99, Math.round(+p.masked_fraction * 100))}% of the measurement region`); // drawn points never fully masked
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
  const all = (Array.isArray(raw) ? raw : raw?.points || []).filter((p) => Number.isFinite(p.flux));
  // no measurement pixel left (masked_fraction ≈ 1): not drawn, not in the light curve, counted in the note
  const empty = (p) => p.masked_fraction != null && +p.masked_fraction >= 0.999;
  const pts = all.filter((p) => !empty(p)), nEmpty = all.length - pts.length;
  const meta = raw?.meta || {}, unit = meta.flux_unit || "flux";
  $("specCsv").disabled = !(Array.isArray(raw) ? raw : raw?.points || []).length;
  $("specWhere").textContent = S.specAt === "target" ? "(target position)" : S.specAt ? `(${S.specAt.map((v) => v.toFixed(5)).join(", ")})` : "";
  scatter($("spec"), pts, "wavelength_um", "flux", "µm", unit,
    S.specErr || (raw ? "This spectrum has no points with usable pixels." : "No spectrum is available for this field.")); // flagged points open
  renderFlagLegend(pts, meta);
  // light curve: selected band's wavelength range only (mixing wavelengths turns the spectrum into fake scatter)
  const w = S.m?.bands?.find((b) => b.id === S.band)?.wave_um;
  const inBand = (p) => !w || (p.wavelength_um >= w[0] && p.wavelength_um <= w[1]);
  $("lcWhere").textContent = w ? `(${S.band}, ${w[0]}–${w[1]} µm)` : "";
  // light curve: valid points only — flagged and saturated out, recovered-from-overflow points in
  const usable = (p) => !p.saturated && (!p.flagged || p.overflow);
  const inb = pts.filter(inBand);
  scatter($("lc"), inb.filter(usable), "mjd", "flux", "MJD", unit,
    !inb.length ? `No spectrum points in ${S.band}.` : `All ${inb.length} points in ${S.band} are saturated or flagged, so there is no light curve here.`);
  // saturated stars: relative brightness from the PSF wings (MJy/sr) — its own plot, never on the mJy axis
  const wing = pts.filter((p) => Number.isFinite(p.wing_flux));
  $("wingBox").hidden = !wing.length; $("wingTab").hidden = !wing.length;
  if (!wing.length && S.plot === "wingBox") { S.plot = "specBox"; document.querySelectorAll(".plotPane").forEach((p) => p.classList.toggle("off", p.id !== "specBox"));
    document.querySelectorAll("#plotTabs [data-plot]").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.plot === "specBox"))); }
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
  const gaiaMissing = typeof meta.gaia === "string" && /not available|NOT removed/i.test(meta.gaia);
  // what a reader needs stays visible; how it was measured goes into a closed section
  const shown = [
    meta.position && `position: ${meta.position}`, // "target, moved to each exposure …" or "fixed point …"
    // without Gaia the server cannot remove neighbours: then its own sentence (meta.gaia) is shown, never "removed"
    gaiaMissing ? String(meta.gaia)
      : meta.photometry === "psf_deblended" && `neighbour removed by PSF fit${nb.length ? ": " + nb.join(", ") : ""}`,
    !gaiaMissing && typeof meta.gaia === "string" && meta.gaia,
    `${pts.length} points, ${nf} flagged (faint in the spectrum, left out of the light curve)`,
    nEmpty && `${nEmpty} point${nEmpty === 1 ? "" : "s"} without usable pixels left out`,
    nz && `${nz} points consistent with zero (|flux| < 0.15 × removed neighbour flux, drawn faint)`,
    ns && `${ns} saturated (lower limits: open marker with arrow, not in the light curve)`,
    no && `${no} recovered from overflow (reduced accuracy; filled diamond, kept in the light curve)`,
    meta.bands_pending?.length && `bands still being built (not in this spectrum yet): ${meta.bands_pending.join(", ")}`,
    meta.err_note, // the error-bar caveat from the empty-sky test (co-add session), shown as it is
  ].filter(Boolean);
  const secs = meta.seconds && typeof meta.seconds === "object"
    ? "server time: " + Object.entries(meta.seconds).map(([k, v]) => `${k} ${(+v).toFixed(1)} s`).join(", ") : "";
  const how = [
    meta.photometry && `photometry: ${meta.photometry}`,
    meta.method && `method: ${meta.method}`,
    meta.aperture_radius_px != null && `aperture ${meta.aperture_radius_px} px` +
      (meta.background_annulus_px ? `, background annulus ${[].concat(meta.background_annulus_px).join("–")} px` : ""),
    meta.aperture_corrected === false && "fluxes are NOT aperture-corrected",
    secs,
  ].filter(Boolean);
  const det = document.createElement("details"), sum = document.createElement("summary"), body = document.createElement("div");
  sum.textContent = "How this spectrum was measured"; body.textContent = how.join(" · "); det.append(sum, body);
  $("specNote").replaceChildren(Object.assign(document.createElement("div"), { textContent: shown.join(" · ") }), ...(how.length ? [det] : []));
}

// ---------- UI ----------
function status(t, err = false) { $("status").textContent = t; $("status").className = err ? "err" : ""; }

function renderBands() {
  const sel = S.m.bands.find((b) => b.id === S.band);
  // the unit stays out of the label's upper-case style ("µm" would turn into "MM")
  $("bandLab").replaceChildren("wavelength · band ", Object.assign(document.createElement("span"), { className: "nt", textContent: sel ? `${sel.id} · ${sel.label || ""}` : "" }));
  $("bands").replaceChildren(...S.m.bands.map((b, i) => {
    const btn = document.createElement("button");
    const loaded = S.m.epochs.filter((e) => frameOf(b.id, e.id)).length;
    const pct = b.status === "pending" && b.progress != null ? ` ${Math.round(b.progress * 100)}%` : "";
    // one dot per pass: filled = its frame is here; a band still being built says so
    const dots = S.m.epochs.map((e) => `<i class="${frameOf(b.id, e.id) ? "has" : ""}"></i>`).join("");
    const state = b.status === "pending" ? `building${pct}` : b.status === "empty" ? "no data" : "";
    // one short pill per band; its range is in the label above (selected band) and in the tooltip
    btn.innerHTML = `<b>${escapeHtml(String(b.id))}</b>${state ? `<small>${escapeHtml(state)}</small>` : ""}`;
    btn.setAttribute("aria-label", `${b.id}, ${b.label || ""}, ${loaded} of ${S.m.epochs.length} passes loaded`);
    void dots;
    btn.title = `${b.id}: ${b.label || ""}; exposures per pass: ${(b.n_exposures || []).join(", ")}  (key ${i + 1})`;
    btn.className = b.id === S.band ? "on" : "";
    btn.setAttribute("aria-pressed", String(b.id === S.band));
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
      const ov = A.graphicOverlay({ color: INK.ember, lineWidth: 2 });
      aladin.addOverlay(ov);
      const [ny, nx] = g.shape, c = [[-0.5, -0.5], [nx - 0.5, -0.5], [nx - 0.5, ny - 0.5], [-0.5, ny - 0.5]].map(([x, y]) => pix2world(x, y));
      ov.add(A.polygon(c));
      const t = S.m.target_track?.find((p) => p.epoch === S.m.epochs[S.epoch]?.id) || S.m.target_track?.at(-1) || S.m.query;
      if (t) {
        const cat = A.catalog({ name: "target", color: INK.fg, sourceSize: 18, shape: "circle" });
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
      if (r.ok) { const a = new Float32Array(littleEndian(await r.arrayBuffer()));
        if (a.length === S.m.grid.shape[0] * S.m.grid.shape[1]) frames[b.id] = a; } // a WISE file for another grid is left out
    } catch { /* that band is left out */ }
  }
  if (!Object.keys(frames).length) return;
  S.wise = { mjd: meta.epoch_mjd ?? meta.mjd ?? null, date: meta.date || "", survey: meta.survey || "WISE", note: meta.note || "",
    scale: Object.fromEntries(bands.map((b) => [b.id, b.scale])), frames };
  if (!frames[S.wiseBand]) S.wiseBand = Object.keys(frames)[0];
  $("wiseBox").hidden = false; $("wiseNone").hidden = true; $("wiseOn").checked = S.wiseOn; $("wiseBand").value = S.wiseBand;
  [...$("wiseBand").options].forEach((o) => { o.disabled = !frames[o.value]; });
  const unit = bands.find((b) => b.id === S.wiseBand)?.unit;
  const yr = (S.wise.date || "").slice(0, 4);
  $("wiseLabel").textContent = `${yr ? yr + " · " : ""}WISE`;
  $("wiseShort").textContent = `${S.wise.survey}${yr ? ` from ${yr}` : ""}: switch it on to blink it with the SPHEREx epochs (a moving object sits ${yr ? "where it was in " + yr : "where it was then"}).`;
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
  ctx.font = "500 14px 'IBM Plex Mono', ui-monospace, monospace";
  const w = ctx.measureText(t).width + 12, x = (ctx.canvas.width - w) / 2;
  ctx.fillStyle = "rgba(0,0,0,0.8)"; ctx.fillRect(x, 6, w, 22);
  ctx.fillStyle = INK.fg; ctx.fillText(t, x + 6, 22);
}

// ---------- CatWISE2020 layer: {key}/catwise.json (gallery) or /api/catwise?key= (live fields) ----------
S.catwise = null;
const CAT_COLOR = INK.fg3, CAT_EPOCH = 2015.4;
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
    fast && `${fast} with an unreliable catalogue motion are drawn dashed at the catalogue position, not moved`]
    .filter(Boolean).map((t) => String(t).trim().replace(/([^.])$/, "$1.")).join(" ");
  // the catalogue's conventions and caveats: in "About this catalogue"
  const more = [m.pm_convention, m.fast_note].filter(Boolean).map((t) => String(t).trim().replace(/([^.])$/, "$1.")).join(" ");
  $("catMore").hidden = !more; $("catMoreText").textContent = more;
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
const CAND_COLOR = INK.fg2;
// n_bands counts independent wavelength ranges (W overlaps D5/D6), so it is never written next to "bands"
const NUMW = ["no", "one", "two", "three", "four", "five", "six", "seven"];
const rangesTxt = (n) => n === 1 ? "one independent wavelength range" : `${NUMW[n] ?? n} independent ranges`;
// "known" = motion explained by a Gaia DR3 proper motion; near_gaia alone (a star nearby) is only in the tooltip
function candLabel(c) {
  if (c.known) return `known: Gaia DR3 ${c.near_gaia?.source_id ?? ""}`.trim();
  return c.kind || "candidate";
}
async function loadCandidates() {
  const url = USE_API ? `${API}/api/candidates?key=${enc(S.key)}` : `${DATA}/${S.key}/candidates.json`;
  try {
    const r = await fetch(url); // quiet: a missing file only means no candidates for this field
    if (r.status === 202) { // the server computes candidates once all bands of the field are built: ask again
      $("candBox").hidden = false; $("candBox").classList.add("pending");
      let bp = []; try { bp = (await r.json()).bands_pending || []; } catch { /* no body */ }
      $("ovCand").disabled = true;
      $("candPending").textContent = `Automatic candidates are being computed…${bp.length ? ` (waiting for bands ${bp.join(", ")})` : ""}`;
      setTimeout(() => loadCandidates().then(draw), 5000);
      return;
    }
    if (r.status === 503) { // the detector is missing or failed: say so
      let msg = ""; try { msg = (await r.json()).message || ""; } catch { /* no body */ }
      $("candBox").hidden = false; $("ovCand").disabled = true; $("candPending").textContent = `Automatic candidates are not available right now${msg ? `: ${msg}` : "."}`;
      return;
    }
    if (!r.ok) { $("candBox").hidden = true; return; } // 404: no candidates for this field
    S.cands = await r.json();
  } catch { return; }
  const cs = S.cands.candidates || [];
  $("candBox").hidden = false; $("candBox").classList.remove("pending");
  $("ovCand").disabled = false; $("candPending").textContent = "";
  // a table: class (A/B and why), what and where, bands, S/N, rate; a row (or "Go") loads it into the measure tool
  const tbl = document.createElement("table");
  tbl.innerHTML = `<thead><tr><th>#</th><th>class</th><th>what · where</th><th>bands</th><th>S/N</th><th>″/yr</th><th></th></tr></thead>`;
  const tb = document.createElement("tbody");
  for (const c of cs) {
    const tr = document.createElement("tr"), p0 = (c.positions || [])[0];
    const rate = c.rate_arcsec_per_year ? Math.hypot(...c.rate_arcsec_per_year).toFixed(1) : "—";
    const where = p0 ? `RA ${(+p0.ra).toFixed(4)} Dec ${(+p0.dec).toFixed(4)}` : "";
    const cell = (t, cls = "") => Object.assign(document.createElement("td"), { textContent: t, className: cls });
    const cls = cell(c.grade || "—", "grade");
    if (c.grade_reason) cls.append(Object.assign(document.createElement("small"), { textContent: c.grade_reason }));
    const what = cell(candLabel(c), "what");
    what.append(Object.assign(document.createElement("small"), { textContent: where }));
    const go = Object.assign(document.createElement("button"), { type: "button", textContent: "Go",
      title: `load its ${c.positions?.length || 0} positions as marks in the measure tool` });
    const goTd = document.createElement("td"); goTd.append(go);
    tr.append(cell(String(c.id)), cls, what,
      cell(`${(c.bands || []).join(", ")}${c.n_bands != null ? ` (${rangesTxt(c.n_bands)})` : ""}`, "bands"),
      cell((+c.snr).toFixed(1), "num"), cell(rate, "num"), goTd);
    if (c.grade === "B") tr.classList.add("gradeB");
    const ng = c.near_gaia ? `
near Gaia DR3 ${c.near_gaia.source_id} (${(+c.near_gaia.sep_arcsec).toFixed(1)}″, G ${(+c.near_gaia.g_mag).toFixed(1)})` : "";
    tr.title = `${c.notes || ""}${ng}`.trim();
    const open = () => { candidateToMarks(c); setTab("measure"); };
    tr.onclick = (e) => { if (e.target !== go) open(); };
    go.onclick = open;
    tb.append(tr);
  }
  tbl.append(tb);
  $("candList").replaceChildren(...(cs.length ? [tbl] : []));
  const rej = Object.entries(S.cands.rejected || {}).map(([k, v]) => `  ${v} × ${k}`).join("\n");
  // the first three validation lines are user-facing (limit, injection result, what the grades mean): always
  // shown under the toggle; the rest stays in the closed "How candidates are found" section
  // visible: what it can find and what the classes mean; the injection result goes with the method
  const val = [].concat(S.cands.validation || []), NV = 3;
  $("candLimits").textContent = [val[0], val[2]].filter(Boolean).join(" ");
  $("candHow").textContent = [
    S.cands.method && `method: ${S.cands.method}`,
    S.cands.params && `parameters: ${Object.entries(S.cands.params).map(([k, v]) => `${k} ${v}`).join(", ")}`,
    S.cands.bands_searched && `bands searched: ${S.cands.bands_searched.join(", ")}`,
    `${cs.length} candidates shown`, rej && `rejected:\n${rej}`,
    val[1], ...val.slice(NV),
  ].filter(Boolean).join("\n");
}
// a candidate's positions become marks in the measure tool (then refine/report as usual)
function candidateToMarks(c) {
  S.marks = (c.positions || []).map((p) => {
    const i = S.m.epochs.findIndex((e) => e.id === p.epoch), ep = S.m.epochs[i];
    const b = (c.bands || [])[0] || S.band; // the time of that band's frame (the pass's middle until its file is loaded)
    return ep && { n: ++S.markSeq, ra: +p.ra, dec: +p.dec, mjd: frameMjd(b, i), epoch: i, date: frameDate(b, i), band: b };
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
    ctx.globalAlpha = (faint ? 0.4 : 1) * (c.grade === "B" ? 0.5 : 1); // grade B drawn faint ctx.strokeStyle = CAND_COLOR; ctx.lineWidth = 1.5; ctx.setLineDash([4, 3]);
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
  if (!API) { noServer($("winNote"), "Your own wavelength window"); return; }
  if (!(lo >= 0.75 && hi <= 5.0 && lo < hi)) { $("winNote").textContent = "Choose a window between 0.75 and 5.00 µm, with the first number smaller."; return; }
  const id = addCustomBand(windowId(lo, hi));
  $("winNote").textContent = `Building ${id} on the server…`; $("specMore").open = true;
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
    // where the list of exposures came from (our own index of the public bucket, when the server used it); no row otherwise
    ...(m.image_list ? [["Image list", m.image_list]] : []),
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
  // another field is always built by the live server, so its own limit wins over the one in a gallery manifest
  const cur = S.m?.query?.fov_deg, max = (API && S.health?.max_fov_deg) || S.m?.max_fov_deg;
  $("fovButtons").replaceChildren(Object.assign(document.createElement("span"), { className: "lab", textContent: "field" }),
    ...FOVS.map(([am, label]) => {
      const deg = am / 60, b = document.createElement("button");
      b.type = "button"; b.textContent = label;
      b.className = cur && Math.abs(cur * 60 - am) < 0.6 ? "on" : "";
      let why = "";
      if (NO_SERVER) why = ""; // clickable: says why it needs the server instead of staying silent
      else if (!API) why = `the gallery is fixed at ${cur ? (cur * 60).toFixed(1) + "′" : "its field"}; another field needs the live server, which is not reachable right now`;
      // over the server's limit: stays clickable and says why (a disabled button says nothing on a touch screen)
      const over = max && deg > max + 1e-9;
      b.disabled = !!why;
      if (over) b.classList.add("over");
      b.title = over ? `this server allows fields up to ${(max * 60).toFixed(1)}′` : why || `${label} field: larger fields catch faster movers but take longer to load`;
      b.onclick = () => (NO_SERVER ? noServer($("status"), "Another field")
        : over ? status(`A ${label} field is not available on this server: it allows fields up to ${(max * 60).toFixed(1)}′, ` +
          "to keep the load on the shared archive small. Run the server yourself for larger fields (see the README).")
        : am >= 15 ? askBigField(deg) : changeFov(deg));
      return b;
    }));
}
// 15′: say what it costs before the field changes (in-page confirmation). Numbers measured by the data
// session on 14 Nov 2026: 99.5–137.4 MB and 30.5–69.8 s per band, 727–760 MB for all six.
function askBigField(deg) {
  const box = $("fovConfirm");
  box.hidden = false;
  box.replaceChildren(
    Object.assign(document.createElement("span"), { textContent: "A 15′ field reads about 120 to 190 MB per band from the archive, 730 to 1100 MB for all six. " +
      "On a home connection that took 30 to 70 s per band (measured 14–15 Nov 2026). Larger fields help with fast-moving objects. " }),
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
    // one box: two angles are a position, anything else is a name
    const pos = splitCoords(name);
    if (pos) { navigate({ ra: pos[0], dec: pos[1], fov: f.get("fov") }); return; }
    if (!name) { if (f.get("ra") && f.get("dec")) navigate({ ra: f.get("ra"), dec: f.get("dec"), fov: f.get("fov") }); return; }
    if (!(await serverReady)) { // no live server: match a gallery entry by key or name
      const norm = (s) => String(s).toLowerCase().replace(/[^a-z0-9]/g, "");
      const hit = [...$("targets").children].find((b) => norm(b.dataset.key) === norm(name) || norm(b.textContent) === norm(name));
      if (hit) navigate({ target: hit.dataset.key });
      else if (NO_SERVER) noServer($("status"), "");
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
  $("specCsv").onclick = () => {
    const at = S.specAt && S.specAt !== "target" ? `_ra${S.specAt[0].toFixed(5)}_dec${S.specAt[1].toFixed(5)}` : "";
    const a = Object.assign(document.createElement("a"), { href: URL.createObjectURL(new Blob([spectrumCsv()], { type: "text/csv" })), download: `${S.key}${at}_spectrum.csv` });
    document.body.append(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(a.href), 5000);
  };
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
    spec.insertAdjacentHTML("beforeend", `<rect id="winSel" x="${x}" y="${d.T}" width="${w}" height="${+d.H - +d.B - +d.T}" fill="${INK.ember}" opacity="0.2"/>`);
  };
  let selFrom = null;
  spec.addEventListener("mousedown", (ev) => { selFrom = umAt(ev); });
  spec.addEventListener("mousemove", (ev) => { if (selFrom != null) showSel(selFrom, umAt(ev)); });
  window.addEventListener("mouseup", (ev) => {
    if (selFrom == null) return;
    const to = umAt(ev) ?? selFrom, lo = Math.min(selFrom, to), hi = Math.max(selFrom, to); selFrom = null;
    if (hi - lo < 0.01) return; // a click, not a drag
    $("winLo").value = lo.toFixed(2); $("winHi").value = hi.toFixed(2); $("specMore").open = true;
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
    if (!API) {
      if (NO_SERVER) noServer($("status"), "");
      else status("A spectrum at a clicked point needs the live server, which is not reachable right now; the gallery has the target's spectrum.");
      return;
    }
    S.specAt = [ra, dec]; draw();
    // the live server measures any point, also for a gallery target opened from static files (same key);
    // a new point reads every exposure again, which can take minutes: say so, and only the last click counts
    const ask = ++S.specAsk, t0 = performance.now();
    $("specWhere").textContent = `(${ra.toFixed(5)}, ${dec.toFixed(5)}: measuring…)`;
    status(`Measuring the spectrum at RA ${ra.toFixed(5)}, Dec ${dec.toFixed(5)} on the live server. It reads every exposure ` +
      "of this field again, which can take a few minutes; the image stays usable meanwhile.");
    fetchLogged(`${API}/api/spectrum?key=${enc(S.key)}&ra=${ra.toFixed(6)}&dec=${dec.toFixed(6)}`)
      .then((s) => {
        if (ask !== S.specAsk) return; // a later click replaced this one
        S.spectrum = s; plots();
        status(`Spectrum at RA ${ra.toFixed(5)}, Dec ${dec.toFixed(5)} ready (${Math.round((performance.now() - t0) / 1000)} s).`);
      })
      .catch((e) => { if (ask === S.specAsk && !offline) status(friendly(e, "Spectrum at this point"), true); }); // offline: keep the "not reachable" line
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
      const zs = [1, 2, 3, 4, 5, 6], i = zs.indexOf(S.zoom);
      S.zoom = zs[Math.max(0, Math.min(zs.length - 1, (i < 0 ? 2 : i) + (e.key === "+" ? 1 : -1)))];
      $("zoom").value = String(S.zoom); draw(); syncUrl();
    }
  });
}

if (params.get("embed") === "1") document.body.classList.add("embed");
if (params.get("ra")) { const f = $("coordForm"); f.ra.value = params.get("ra"); f.dec.value = params.get("dec"); f.fov.value = params.get("fov") || "0.1";
  if (!params.get("name")) f.name.value = `${params.get("ra")} ${params.get("dec")}`; }
// header: which source the page uses (gallery files, live server or both)
// header: a short badge for the server; the details (address, version, data folder) are in its tooltip
function renderSource(probing) {
  const h = S.health, srv = API ? `live server ${API}` + (h ? ` (${h.version ?? ""} ${h.commit ?? ""}, max field ${(+h.max_fov_deg * 60).toFixed(1)}′)` : "") : "";
  const rej = PARAM_REJECTS.length ? " · " + PARAM_REJECTS.join(" · ") : "";
  const full = NO_SERVER ? "gallery of five targets (no live server on this site)"
    : FORCED ? `${srv} (all targets)` : [`gallery files ${DATA}/`, probing ? "looking for the live server…" : srv || "live server not reachable"].join(" · ");
  const [txt, cls] = NO_SERVER ? ["gallery only", "off"] : probing ? ["checking server…", "wait"]
    : API ? ["live server", "on"] : ["server offline · gallery only", "off"];
  const b = $("source");
  b.textContent = txt + (PARAM_REJECTS.length ? " · parameter ignored" : ""); b.className = `badge ${cls}`;
  b.title = full + rej;
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
// the start page and the tool hand each other the target's name and position for the first frame of the page change
$("home").addEventListener("click", () => {
  try {
    const q = S.m?.query || {};
    sessionStorage.setItem("spx-vt", JSON.stringify({ to: "start", key: params.get("target") || params.get("key") || null, // a gallery key; null for a free field
      ra: q.ra != null ? +q.ra : null, dec: q.dec != null ? +q.dec : null, name: $("tname").textContent, coord: $("tcoord").textContent, t: Date.now() }));
  } catch { /* storage off: the start page uses the referrer */ }
});
try {
  const h = JSON.parse(sessionStorage.getItem("spx-vt") || "null");
  if (h?.to === "tool" && Date.now() - h.t < 15000 && !$("tname").textContent) {
    if (h.name) $("tname").textContent = h.name;
    if (h.coord) $("tcoord").textContent = h.coord;
  }
} catch { /* nothing to show early */ }
// plots follow the page width (drawn in real pixels): redraw after a resize settles
{ let t = 0; addEventListener("resize", () => { clearTimeout(t); t = setTimeout(() => {
    if (!S.m) return;
    if (!EMBED) { // a larger stage (or a page first opened while hidden) gets a sharper drawing
      const cssW = parseFloat(getComputedStyle($("canvas")).width) || 0, fit = Math.min(6, Math.floor(cssW / S.m.grid.shape[1]));
      if (fit > S.zoom) { S.zoom = fit; $("zoom").value = String(fit); syncUrl(); }
    }
    draw(); plots();
  }, 200); }); }
if (EMBED) { // the embed keeps its time controls, view and measure beside the image (no slot), so it fits 560 px
  $("panel").prepend($("timeBox"));
  $("panel").append(document.querySelector('.tabpane[data-pane="view"]'), document.querySelector('.tabpane[data-pane="measure"]'));
}
document.querySelectorAll("#plotTabs [data-tab]").forEach((b) => { b.onclick = () => setTab(S.tab === b.dataset.tab ? null : b.dataset.tab); });
$("plotTabs").onkeydown = (e) => { // arrow keys move along the strip
  if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
  const bs = [...document.querySelectorAll("#plotTabs button:not([hidden])")], i = bs.findIndex((b) => b.getAttribute("aria-selected") === "true");
  const n = bs[(i + (e.key === "ArrowRight" ? 1 : bs.length - 1)) % bs.length];
  n.dataset.tab ? setTab(n.dataset.tab) : setPlot(n.dataset.plot); n.focus(); e.stopPropagation(); e.preventDefault();
};
// the data box: one plot at a time (spectrum / light curve / PSF wings); drawn again at its width when shown
S.plot = "specBox";
function setPlot(name) {
  if (name === "wingBox" && $("wingBox").hidden) name = "specBox";
  S.plot = name; S.tab = null;
  document.querySelectorAll("#plotTabs [data-tab]").forEach((b) => b.setAttribute("aria-selected", "false"));
  document.querySelectorAll(".tabpane").forEach((p) => { p.hidden = !EMBED; });
  document.querySelectorAll("#plotTabs [data-plot]").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.plot === name)));
  document.querySelectorAll(".plotPane").forEach((p) => p.classList.toggle("off", p.id !== name));
  if (S.m) plots();
}
document.querySelectorAll("#plotTabs [data-plot]").forEach((b) => { b.onclick = () => setPlot(b.dataset.plot); });
setPlot("specBox");
// "How to use this": a short dialog in the start page's style; after the page change, not in the embed; remembered
// in this browser when asked (without storage it simply shows each time)
{
  const KEY = "spx-howto-off", dlg = $("howto");
  let from = null;
  const save = () => { try { if ($("howtoNever").checked) localStorage.setItem(KEY, "1"); else localStorage.removeItem(KEY); } catch { /* storage off */ } };
  const open = () => { if (dlg.open) return; from = document.activeElement; try { $("howtoNever").checked = localStorage.getItem(KEY) === "1"; } catch { /* storage off */ } dlg.showModal(); $("howtoOk").focus(); };
  const close = () => { save(); try { sessionStorage.setItem("spx-howto-seen", "1"); } catch { /* storage off */ } dlg.close(); };
  dlg.addEventListener("cancel", () => { save(); try { sessionStorage.setItem("spx-howto-seen", "1"); } catch { /* storage off */ } }); // Esc
  dlg.addEventListener("close", () => { if (from && from.focus) from.focus(); });
  dlg.addEventListener("click", (e) => { // a click outside the box (on the backdrop) closes it
    const r = dlg.getBoundingClientRect();
    if (e.target === dlg && (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom)) close();
  });
  $("howtoOk").onclick = close; $("howtoClose").onclick = close; $("howtoBtn").onclick = open;
  // not again in this tab once closed (a Sky <-> tool round trip would otherwise bring it back every time)
  let off = false; try { off = localStorage.getItem(KEY) === "1" || sessionStorage.getItem("spx-howto-seen") === "1"; } catch { /* storage off */ }
  const still = matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (!EMBED && !off) (window.__vtDone || Promise.resolve()).then(() => setTimeout(open, still ? 0 : window.__vtHad ? 300 : 600));
}
loadIndex();
loadTarget();
// developer hook (?debug=1): place marks at exact positions/times, e.g. to replay a known asteroid passage
if (params.get("debug") === "1") {
  window.__spx = { S, refine, openReport, loadGaia, aladin: () => aladin,
    mark: (ra, dec, mjd, band = S.band) => { S.marks.push({ n: ++S.markSeq, ra, dec, mjd, epoch: S.epoch, date: `MJD ${mjd}`, band }); renderMarks(); draw(); } };
}
