"""SPHEREx blink API (data contract v1, PLAN.md section 5).

    uvicorn server.app:app --host 127.0.0.1 --port 8000          (from the repository root)

GET /api/manifest?ra=&dec=&fov=  |  ?target=     SIA2 only (seconds); queues every band, default band first
GET /api/band?key=&band=                           waits (<= BAND_WAIT_S) until the band is built
GET /api/frame?key=&epoch=&band=[&kind=weight|flagfrac]
GET /api/gaia?key=     GET /api/spectrum?key=[&ra=&dec=]     GET /api/resolve?name=     GET /api/health

Bands are the unit of work: asking for a band (or one of its frames) moves it to the front of the queue;
the others keep filling in the background. Built files live in cache/fields/<version>/<key>/ with the same
names as the static gallery, so a second request never downloads again.
Settings (environment): SPX_MAX_FOV_DEG, SPX_LIVE_LAYERS (default "IMAGE,FLAGS"), SPX_BACKGROUND_S3_WORKERS, SPX_COLLECTIONS, SPX_CACHE.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from urllib.parse import quote

import numpy as np
from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, Response

import spx
from server import pipeline as P
from spx import access
from spx.targets import TARGETS

log = logging.getLogger("spx.server")

BAND_WAIT_S = float(os.environ.get("SPX_BAND_WAIT_S") or 90)
LIVE_LAYERS = tuple(x.strip().upper() for x in (os.environ.get("SPX_LIVE_LAYERS") or "IMAGE,FLAGS").split(","))
#: Band builders: one serves only requested bands (full S3 concurrency); the other fills the remaining
#: bands in the background with few S3 connections, and takes requested bands first when there are any.
BACKGROUND_S3_WORKERS = int(os.environ.get("SPX_BACKGROUND_S3_WORKERS") or 8)
FIELDS_DIR = access.CACHE_DIR / "fields" / f"v{spx.__version__}"
MAX_FIELDS_IN_MEMORY = 32
MAX_CUTOUT_SETS = 2


# --------------------------------------------------------------------------- errors / json

class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def jresp(obj, status: int = 200, cache: str | None = None) -> Response:
    headers = {"Cache-Control": cache} if cache else {}
    return Response(P.dumps(obj), status_code=status, media_type="application/json", headers=headers)


# --------------------------------------------------------------------------- field / band state

@dataclass
class BandJob:
    band: str
    state: str = "pending"            # pending | running | ready | empty | error
    error: str | None = None
    done: int = 0
    total: int = 0
    event: threading.Event = dc_field(default_factory=threading.Event)
    n_exposures: list | None = None   # custom windows: counted after download
    t_start: float | None = None
    background: bool = False
    t_end: float | None = None


@dataclass
class FieldState:
    field: P.Field
    out: Path
    jobs: dict
    created: float = dc_field(default_factory=time.time)

    def status(self) -> dict:
        return {b: ("ready" if j.state == "ready" else "empty" if j.state == "empty" else "pending")
                for b, j in self.jobs.items()}


class Registry:
    """Fields by key, a prioritised band queue and the worker threads that build bands."""

    def __init__(self):
        self.lock = threading.RLock()
        self.cv = threading.Condition(self.lock)
        self.fields: OrderedDict[str, FieldState] = OrderedDict()
        self.urgent: list[tuple[str, str]] = []         # requested bands, newest request first
        self.background: list[tuple[str, str]] = []     # the rest, in band order
        self.opening: dict[str, threading.Event] = {}
        self.cutouts: OrderedDict[str, dict] = OrderedDict()   # key -> {detector: [Cutout]}
        self.closed = False
        self.workers = [threading.Thread(target=self._work, args=(False,), daemon=True, name="band-urgent"),
                        threading.Thread(target=self._work, args=(True,), daemon=True, name="band-background")]
        for w in self.workers:
            w.start()

    # ---- fields
    def get(self, key: str, open_kw: dict | None = None) -> FieldState:
        """open_kw: exact position for a new free field (the key itself is rounded)."""
        with self.lock:
            fs = self.fields.get(key)
            if fs is not None:
                self.fields.move_to_end(key)
                return fs
            ev = self.opening.get(key)
            if ev is None:
                ev = self.opening[key] = threading.Event()
                mine = True
            else:
                mine = False
        if not mine:  # another request is opening the same field: wait for it
            ev.wait(120)
            with self.lock:
                if key in self.fields:
                    return self.fields[key]
            raise ApiError(503, f"opening field {key} failed")
        try:
            kw = open_kw or parse_key(key)
            f = P.open_field(**kw)
            fs = self._register(f)
            return fs
        finally:
            with self.lock:
                self.opening.pop(key, None)
            ev.set()

    def _register(self, f: P.Field) -> FieldState:
        out = FIELDS_DIR / f.key
        jobs = {}
        for b in f.band_defs():
            j = BandJob(b["id"])
            if (out / f"{b['id']}.json").exists():  # built earlier (also before a restart)
                j.state = "ready" if any(out.glob(f"*_{b['id']}.f32")) else "empty"
                j.event.set()
            jobs[b["id"]] = j
        fs = FieldState(f, out, jobs)
        with self.lock:
            self.fields[f.key] = fs
            while len(self.fields) > MAX_FIELDS_IN_MEMORY:
                self.fields.popitem(last=False)
        if not f.refs:
            for j in jobs.values():
                j.state = "empty"
                j.event.set()
            return fs
        order = [b["id"] for b in f.band_defs()]
        if f.tinfo and f.tinfo.get("default_band") in order:
            order.remove(f.tinfo["default_band"])
            order.insert(0, f.tinfo["default_band"])
        self.enqueue(f.key, order[0], urgent=True)  # the page asks for this one first
        for b in order[1:]:
            self.enqueue(f.key, b, urgent=False)
        return fs

    def close(self) -> None:
        """Stop the workers after their current band (tests, shutdown)."""
        with self.cv:
            self.closed = True
            self.urgent.clear()
            self.background.clear()
            self.cv.notify_all()
        for w in self.workers:
            w.join(timeout=30)

    # ---- queue
    def enqueue(self, key: str, band: str, urgent: bool = True) -> None:
        with self.cv:
            fs = self.fields.get(key)
            if fs is None or band not in fs.jobs or fs.jobs[band].state != "pending":
                return
            item = (key, band)
            if urgent:
                if item in self.background:
                    self.background.remove(item)
                if item in self.urgent:
                    self.urgent.remove(item)
                self.urgent.insert(0, item)
            elif item not in self.urgent and item not in self.background:
                self.background.append(item)
            self.cv.notify_all()

    def _work(self, background: bool):
        while True:
            with self.cv:
                while not (self.closed or self.urgent or (background and self.background)):
                    self.cv.wait()
                if self.closed:
                    return
                is_bg = not self.urgent
                key, band = (self.urgent or self.background).pop(0)
                fs = self.fields.get(key)
                if fs is None:
                    continue
                job = fs.jobs[band]
                if job.state != "pending":
                    continue
                job.state, job.t_start, job.background = "running", time.time(), is_bg
            try:
                self._build(fs, job)
            except Exception as e:
                log.exception("band %s %s failed", key, band)
                job.state, job.error = "error", f"{type(e).__name__}: {e}"[:300]
            finally:
                job.t_end = time.time()
                job.event.set()

    def _build(self, fs: FieldState, job: BandJob):
        f, band = fs.field, job.band

        def progress(done, total):
            job.done, job.total = done, total
        dets = f.detectors_for(band)
        by_det = self._cutout_set(f.key)
        cuts = []
        workers = BACKGROUND_S3_WORKERS if job.background else access.S3_MAX_CONCURRENCY
        for d in dets:
            if d not in by_det:
                c, _ = P.fetch_band_cutouts(f, f"D{d}", LIVE_LAYERS, progress=progress, workers=workers,
                                            box=use_box(f))
                by_det[d] = c
            cuts += by_det[d]
        bj, _, frames = P.build_band(f, band, fs.out, cuts=cuts)
        job.n_exposures = P.band_n_exposures(f, band, cuts)
        job.state = "ready" if frames else "empty"
        P.write_json(fs.out / "manifest.json", P.manifest(f, status=fs.status(), n_exposures=self.n_exposures(fs)))
        log.info("built %s %s: %d frames in %.1fs", f.key, band, len(frames), time.time() - job.t_start)

    def _cutout_set(self, key: str) -> dict:
        with self.lock:
            s = self.cutouts.get(key)
            if s is None:
                s = self.cutouts[key] = {}
            self.cutouts.move_to_end(key)
            while len(self.cutouts) > MAX_CUTOUT_SETS:
                self.cutouts.popitem(last=False)
            return s

    def cutouts_for(self, fs: FieldState, detectors) -> list:
        """Cutouts of built bands (disk cache, so no download for bands already built)."""
        by_det = self._cutout_set(fs.field.key)
        out = []
        for d in detectors:
            if d not in by_det:
                by_det[d], _ = P.fetch_band_cutouts(fs.field, f"D{d}", LIVE_LAYERS, box=use_box(fs.field))
            out += by_det[d]
        return out

    @staticmethod
    def n_exposures(fs: FieldState) -> dict:
        return {b: j.n_exposures for b, j in fs.jobs.items() if j.n_exposures is not None}

    def manifest(self, fs: FieldState) -> dict:
        m = P.manifest(fs.field, status=fs.status(), n_exposures=self.n_exposures(fs))
        for b in m.get("bands", []):
            j = fs.jobs.get(b["id"])
            if j and j.state in ("running", "pending") and j.total:
                b["progress"] = round(j.done / j.total, 3)
            if j and j.state == "error":
                b["error"] = j.error
        return m

    def touch(self, key: str) -> None:
        """The page polls the manifest of the field on screen: its pending bands go first in the background."""
        with self.cv:
            mine = [it for it in self.background if it[0] == key]
            if mine:
                self.background = mine + [it for it in self.background if it[0] != key]

    def wait_band(self, fs: FieldState, band: str, timeout: float) -> BandJob:
        if band not in fs.jobs:
            raise ApiError(404, f"no band {band!r} for {fs.field.key}; bands: {list(fs.jobs)}")
        job = fs.jobs[band]
        if not job.event.is_set():
            self.enqueue(fs.field.key, band, urgent=True)
            job.event.wait(timeout)
        return job


def use_box(f: P.Field) -> bool:
    """Free fields read the grid's detector bounding box (fewer rows). Gallery targets keep the square
    cutouts their disk cache already holds, so nothing is downloaded again."""
    return f.tinfo is None


REG: Registry | None = None


def registry() -> Registry:
    global REG
    if REG is None:
        REG = Registry()
    return REG


# --------------------------------------------------------------------------- keys and coordinates

_KEY_RE = re.compile(r"^r(\d+p\d+)_d([pm])(\d+p\d+)_f(\d+p\d+)"
                     r"(?:_pm(m?\d+p\d+)_(m?\d+p\d+)_e(m?\d+p\d+))?$")


def _dec_num(s: str) -> float:
    return (-1 if s.startswith("m") else 1) * float(s.lstrip("m").replace("p", "."))


def parse_key(key: str) -> dict:
    if key in TARGETS:
        return {"target": key}
    m = _KEY_RE.match(key or "")
    if not m:
        raise ApiError(404, f"unknown key {key!r}")
    ra = float(m.group(1).replace("p", "."))
    dec = float(m.group(3).replace("p", ".")) * (-1 if m.group(2) == "m" else 1)
    fov = float(m.group(4).replace("p", "."))
    check_fov(fov)
    out = {"ra": ra, "dec": dec, "fov_deg": fov}
    if m.group(5):
        out["pm"] = (_dec_num(m.group(5)), _dec_num(m.group(6)), _dec_num(m.group(7)))
    return out


def parse_coord(ra: str, dec: str) -> tuple[float, float]:
    """Decimal degrees, or sexagesimal (RA in hours: '08:55:10.8' / '08h55m10.8s', Dec in degrees)."""
    try:
        r, d = float(ra), float(dec)
    except ValueError:
        from astropy.coordinates import SkyCoord
        import astropy.units as u
        try:
            c = SkyCoord(ra, dec, unit=(u.hourangle, u.deg), frame="icrs")
        except Exception:
            raise ApiError(400, f"cannot read coordinates ra={ra!r} dec={dec!r}")
        r, d = float(c.ra.deg), float(c.dec.deg)
    if not (-90 <= d <= 90) or not np.isfinite(r):
        raise ApiError(400, f"dec must be within [-90, 90], got {d}")
    return r % 360.0, d


def check_fov(fov: float) -> None:
    if not (0.01 <= fov <= P.MAX_FOV_DEG + 0.0005):  # keys carry the field rounded to 0.001 deg
        raise ApiError(400, f"fov must be within [0.01, {P.MAX_FOV_DEG:.4f}] deg on this server, got {fov}")


# --------------------------------------------------------------------------- app

app = FastAPI(title="SPHEREx blink API", version=spx.__version__)


@app.middleware("http")
async def _cors(request: Request, call_next):
    """Access-Control-Allow-Origin: * on every response (contract), not only when Origin is sent."""
    if request.method == "OPTIONS":
        resp = Response(status_code=204)
    else:
        resp = await call_next(request)
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "*"
    return resp


@app.exception_handler(ApiError)
async def _api_error(request: Request, exc: ApiError):
    return jresp({"schema": P.SCHEMA, "message": exc.message}, exc.status)


@app.exception_handler(access.SiaError)
async def _sia_error(request: Request, exc: access.SiaError):
    return jresp({"schema": P.SCHEMA, "message": f"IRSA SIA2 is not answering: {exc}"}, 503)


@app.exception_handler(access.FetchError)
async def _fetch_error(request: Request, exc: access.FetchError):
    return jresp({"schema": P.SCHEMA, "message": f"SPHEREx data on S3 is not reachable: {exc}"}, 503)


async def _field(key: str, open_kw: dict | None = None) -> FieldState:
    return await asyncio.to_thread(registry().get, key, open_kw)


@app.get("/api/manifest")
async def api_manifest(target: str | None = None, ra: str | None = None, dec: str | None = None,
                       fov: float | None = None, pmra: float | None = None, pmdec: float | None = None,
                       ref_epoch: float | None = None, name: str | None = None):
    """pmra/pmdec (mas/yr) and ref_epoch (Julian year, default 2000.0) make a free position move like a
    named target (e.g. pass on what /api/resolve returned)."""
    if target:
        if target not in TARGETS:
            raise ApiError(404, f"unknown target {target!r}; known: {list(TARGETS)}")
        key, kw = target, {"target": target}
    else:
        if ra is None or dec is None:
            raise ApiError(400, "give ?target= or ?ra=&dec=[&fov=]")
        r, d = parse_coord(ra, dec)
        fv = float(fov) if fov else P.DEFAULT_FOV_DEG
        check_fov(fv)
        pm = None
        if pmra is not None or pmdec is not None:
            pm = (float(pmra or 0.0), float(pmdec or 0.0), float(ref_epoch if ref_epoch is not None else 2000.0))
            if not all(np.isfinite(pm)) or abs(pm[0]) > 2e4 or abs(pm[1]) > 2e4:
                raise ApiError(400, f"proper motion out of range: {pm}")
        key = P.free_key(r, d, fv, pm)
        kw = {"ra": r, "dec": d, "fov_deg": fv, "pm": pm, "name": name,
              "source": "SIMBAD via CDS Sesame (proper motion given)" if pm else None}
    fs = await _field(key, kw)
    registry().touch(fs.field.key)
    return jresp(registry().manifest(fs))


@app.get("/api/band")
async def api_band(key: str, band: str):
    fs = await _field(key)
    job = await asyncio.to_thread(registry().wait_band, fs, band, BAND_WAIT_S)
    if job.state == "error":
        raise ApiError(503, f"band {band} failed: {job.error}")
    if not job.event.is_set():
        raise ApiError(504, f"band {band} is still being built ({job.done}/{job.total} images); try again")
    path = fs.out / f"{band}.json"
    if not path.exists():
        raise ApiError(404, f"band {band} has no data")
    return Response(path.read_bytes(), media_type="application/json")


@app.get("/api/frame")
async def api_frame(key: str, epoch: int, band: str, kind: str | None = None):
    if kind not in (None, "", "weight", "flagfrac"):
        raise ApiError(400, "kind must be weight or flagfrac (omit it for the image)")
    fs = await _field(key)
    job = await asyncio.to_thread(registry().wait_band, fs, band, BAND_WAIT_S)
    if not job.event.is_set():
        raise ApiError(504, f"band {band} is still being built; try again")
    path = fs.out / (f"{epoch}_{band}" + (f"_{kind}" if kind else "") + ".f32")
    if not path.exists():
        raise ApiError(404, f"no frame for epoch {epoch} in band {band}")
    return Response(path.read_bytes(), media_type="application/octet-stream",
                    headers={"Cache-Control": "public, max-age=3600"})


@app.get("/api/gaia")
async def api_gaia(key: str):
    fs = await _field(key)
    try:
        g = await asyncio.to_thread(P.gaia_sources, fs.field)
    except Exception as e:
        raise ApiError(503, f"Gaia archive is not answering: {e}")
    return jresp(g)


_spec_cache: OrderedDict = OrderedDict()


@app.get("/api/spectrum")
async def api_spectrum(key: str, ra: float | None = None, dec: float | None = None):
    fs = await _field(key)
    f = fs.field
    if (ra is None) != (dec is None):
        raise ApiError(400, "give both ra and dec, or neither (= the target)")
    ready = [b for b, j in fs.jobs.items() if j.state == "ready" and b.startswith("D") and b[1:].isdigit()]
    dets = sorted(int(b[1:]) for b in ready)
    ck = (key, None if ra is None else round(ra, 5), None if dec is None else round(dec, 5), tuple(dets))
    if ck in _spec_cache:
        return jresp(_spec_cache[ck])

    def run():
        cuts = registry().cutouts_for(fs, dets)
        try:
            gaia = P.gaia_sources(f)
        except Exception as e:  # photometry still works without deblending
            log.warning("gaia for spectrum %s failed: %r", key, e)
            gaia = []
        out = P.spectrum(f, cuts, ra, dec, gaia=gaia)
        out["meta"]["bands_included"] = [f"D{d}" for d in dets]
        out["meta"]["bands_pending"] = [b for b in fs.jobs if b.startswith("D") and f"D{b[1:]}" not in ready]
        return out
    out = await asyncio.to_thread(run)
    _spec_cache[ck] = out
    while len(_spec_cache) > 200:
        _spec_cache.popitem(last=False)
    return jresp(out)


_resolve_cache: dict = {}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower().replace("'s", ""))


@app.get("/api/resolve")
async def api_resolve(name: str = Query(..., min_length=1, max_length=200)):
    n = _norm(name)
    for key, t in TARGETS.items():
        if n in (_norm(key), _norm(t["name"])):
            return jresp({"name": t["name"], "ra": t["ra"], "dec": t["dec"], "pmra": t["pmra"], "pmdec": t["pmdec"],
                          "ref_epoch": t["ref_epoch"], "source": t["source"], "target": key})
    if n in _resolve_cache:
        r = _resolve_cache[n]
    else:
        r = await asyncio.to_thread(_sesame, name)
        _resolve_cache[n] = r
    if r is None:
        raise ApiError(404, f"name {name!r} not found (SIMBAD via CDS Sesame)")
    return jresp(r)


def _sesame(name: str) -> dict | None:
    import requests
    try:
        resp = requests.get("https://cds.unistra.fr/cgi-bin/nph-sesame/-oxp/SNV?" + quote(name), timeout=5)
    except (requests.ConnectionError, requests.Timeout) as e:
        raise ApiError(503, f"name resolver not answering: {e}")
    txt = resp.text

    def g(tag, s=txt):
        m = re.search(f"<{tag}>([^<]*)</{tag}>", s)
        return m.group(1).strip() if m else None
    ra, dec = g("jradeg"), g("jdedeg")
    if resp.status_code != 200 or ra is None or dec is None:
        return None
    pm = re.search(r"<pm>(.*?)</pm>", txt, re.S)
    pmra = float(g("pmRA", pm.group(1))) if pm and g("pmRA", pm.group(1)) else 0.0
    pmdec = float(g("pmDE", pm.group(1))) if pm and g("pmDE", pm.group(1)) else 0.0
    return {"name": g("oname") or name, "ra": float(ra), "dec": float(dec), "pmra": pmra, "pmdec": pmdec,
            "ref_epoch": 2000.0, "source": "SIMBAD via CDS Sesame"}


@app.get("/api/health")
async def api_health():
    reg = registry()
    with reg.lock:
        fields = len(reg.fields)
        queued = {"urgent": len(reg.urgent), "background": len(reg.background)}
    return jresp({"schema": P.SCHEMA, "ok": True, "version": spx.__version__, "commit": P.spx_commit().get("commit"),
                  "collections": list(access.COLLECTIONS), "max_fov_deg": P.MAX_FOV_DEG,
                  "live_layers": list(LIVE_LAYERS), "fields_in_memory": fields, "bands_queued": queued,
                  "s3_max_concurrency": access.S3_MAX_CONCURRENCY})
