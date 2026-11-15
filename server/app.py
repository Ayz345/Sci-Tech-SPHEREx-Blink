"""SPHEREx blink API (data contract v1: web/tool/README.md).

    uvicorn server.app:app --host 127.0.0.1 --port 8000          (from the repository root)

GET /api/manifest?ra=&dec=&fov=  |  ?target=     SIA2 only (seconds); queues every band, default band first
GET /api/band?key=&band=                           waits (<= BAND_WAIT_S) until the band is built
GET /api/frame?key=&epoch=&band=[&kind=weight|flagfrac]
GET /api/gaia?key=     GET /api/spectrum?key=[&ra=&dec=]     GET /api/resolve?name=     GET /api/health

Bands are the unit of work: asking for a band (or one of its frames) moves it to the front of the queue;
the others keep filling in the background. Built files live in cache/fields/<version>/<key>/ with the same
names as the static gallery, so a second request never downloads again.
Settings (environment variables) and deployment: DEPLOY.md.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
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
def _code_hash() -> str:
    """Hash of the code that shapes the built files (spx/*.py, server/pipeline.py, spx/targets.py). Built bands
    live under a folder named after it, so new code never serves files an older version wrote."""
    import hashlib
    root = Path(__file__).resolve().parent.parent
    h = hashlib.sha1()
    for f in sorted(list((root / "spx").glob("*.py")) + [root / "server" / "pipeline.py"]):
        h.update(f.name.encode())
        h.update(f.read_bytes().replace(b"\r\n", b"\n"))
    return h.hexdigest()[:10]


CODE_HASH = _code_hash()
STARTED_COMMIT = P.spx_commit()  # git HEAD (and uncommitted code) when this process started
FIELDS_DIR = access.CACHE_DIR / "fields" / f"v{spx.__version__}-{CODE_HASH}"
MAX_FIELDS_IN_MEMORY = 32
#: User wavelength windows kept per field; the oldest finished one is dropped (job and files).
MAX_WINDOWS_PER_FIELD = int(os.environ.get("SPX_MAX_WINDOWS_PER_FIELD") or 12)
#: Cutouts kept in memory for this many fields (least recently used dropped). They dominate the server's memory:
#: two 15' fields peaked at 689 MB working set (14 Nov, fov_timing_v2.json). Field objects (image lists) are small.
MAX_CUTOUT_SETS = int(os.environ.get("SPX_MAX_CUTOUT_SETS") or 2)


# --------------------------------------------------------------------------- disk cache limit (public server)

#: Size limit of the cache folder's cutouts and built fields (SPX_CACHE_MAX_GB; the Dockerfile sets 5; unset or 0 =
#: no limit and no janitor, as on a development machine whose cache also holds the gallery's cutouts). When over,
#: files are removed down to CACHE_LOW_WATER of it: built fields of other code versions first, then the oldest
#: cutouts, then the oldest fields of this version that are not open. sia/, gaia/, ades/, acceptance/ are kept.
CACHE_MAX_BYTES = int(float(os.environ.get("SPX_CACHE_MAX_GB") or 0) * 1e9)
CACHE_LOW_WATER = 0.9
CACHE_CHECK_S = 600.0
#: After a band is built the cache is checked too (at most once per CACHE_AFTER_BUILD_S): on Cloud Run the disk is
#: memory, and one 15' field writes ~1 GB in a few minutes, faster than the janitor's interval.
CACHE_AFTER_BUILD_S = 60.0
CACHE_STATE: dict = {"bytes": None, "checked": None, "last_cleanup": None}


def _tree_size(path: Path) -> tuple[int, float]:
    """(bytes, newest mtime) of a folder."""
    total, newest = 0, 0.0
    for dirpath, _, files in os.walk(path):
        for f in files:
            try:
                st = os.stat(os.path.join(dirpath, f))
            except OSError:
                continue
            total += st.st_size
            newest = max(newest, st.st_mtime)
    return total, newest


def cache_cleanup(root: Path, max_bytes: int, current: Path, keep: set) -> dict:
    """Remove cache files until the managed part (cutout/, layout/, fields/) is under max_bytes * CACHE_LOW_WATER.
    current = this version's fields folder; keep = field keys that are open (never removed)."""
    import shutil
    victims = []   # (order, mtime, bytes, path, is_dir)
    protected = 0
    fields_root = root / "fields"
    if fields_root.exists():
        for vdir in fields_root.iterdir():
            if not vdir.is_dir():
                continue
            if vdir.resolve() != current.resolve():
                b, t = _tree_size(vdir)
                victims.append((0, t, b, vdir, True))
            else:
                for fdir in vdir.iterdir():
                    if fdir.is_dir():
                        b, t = _tree_size(fdir)
                        if fdir.name in keep:
                            protected += b          # counted, never removed
                        else:
                            victims.append((2, t, b, fdir, True))
    for sub in ("cutout", "layout"):
        d = root / sub
        if d.exists():
            for dirpath, _, files in os.walk(d):
                for f in files:
                    fp = Path(dirpath) / f
                    try:
                        st = fp.stat()
                    except OSError:
                        continue
                    victims.append((1, st.st_mtime, st.st_size, fp, False))
    total = sum(v[2] for v in victims) + protected
    kept_total = total
    removed = {"files": 0, "folders": 0, "bytes": 0}
    if total > max_bytes:
        target = max_bytes * CACHE_LOW_WATER
        for order, mtime, b, path, is_dir in sorted(victims, key=lambda v: (v[0], v[1])):
            if kept_total <= target:
                break
            try:
                if is_dir:
                    shutil.rmtree(path)
                    removed["folders"] += 1
                else:
                    path.unlink()
                    removed["files"] += 1
            except OSError:
                continue
            kept_total -= b
            removed["bytes"] += b
    return {"bytes_before": total, "bytes_after": kept_total, "max_bytes": max_bytes, **removed}


def cache_check() -> dict:
    reg = registry()
    with reg.lock:
        keep = set(reg.fields) | set(reg.opening)
    out = cache_cleanup(access.CACHE_DIR, CACHE_MAX_BYTES, FIELDS_DIR, keep)
    CACHE_STATE.update(bytes=out["bytes_after"], checked=time.time())
    if out["files"] or out["folders"]:
        CACHE_STATE["last_cleanup"] = {**out, "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        log.warning("cache cleanup: %s", out)
    return out


def cache_check_after_build() -> None:
    if CACHE_MAX_BYTES <= 0:
        return
    last = CACHE_STATE.get("checked")
    if last is not None and time.time() - last < CACHE_AFTER_BUILD_S:
        return
    try:
        cache_check()
    except Exception as e:
        error_id("cache check after build", e)


def _cache_janitor():
    while True:
        try:
            cache_check()
        except Exception as e:  # never stop the janitor
            error_id("cache cleanup", e)
        time.sleep(CACHE_CHECK_S)


# --------------------------------------------------------------------------- request limits (public server)

#: Requests per client IP per minute on the expensive endpoints (a page opening one field asks the manifest a few
#: times, 6-7 bands and polls candidates every 5 s). SPX_RATE_LIMIT=0 turns all limits off.
RATE_ON = (os.environ.get("SPX_RATE_LIMIT") or "1") != "0"
RATE_PER_MIN = {"manifest": 20, "band": 60, "spectrum": 20, "candidates": 30, "track": 6}
#: New fields (not yet on the server) per client IP per NEW_FIELD_WINDOW_S, and fields with unfinished bands at once.
NEW_FIELDS_PER_IP = int(os.environ.get("SPX_NEW_FIELDS_PER_IP") or 10)
NEW_FIELD_WINDOW_S = 600.0
MAX_ACTIVE_FIELDS = int(os.environ.get("SPX_MAX_ACTIVE_FIELDS") or 4)
_rate_lock = threading.Lock()
_rate_hits: dict = {}      # (ip, group) -> deque of times


def client_ip(request) -> str:
    """The client's address; behind the host's proxy uvicorn --proxy-headers has put X-Forwarded-For here."""
    return (request.client.host if request is not None and request.client else None) or "unknown"


def _hit(ip: str, group: str, limit: int, window: float) -> float | None:
    """Count one request; seconds to wait when over the limit, else None."""
    from collections import deque
    now = time.monotonic()
    with _rate_lock:
        q = _rate_hits.setdefault((ip, group), deque())
        while q and now - q[0] > window:
            q.popleft()
        if len(q) >= limit:
            return max(1.0, window - (now - q[0]))
        q.append(now)
        if len(_rate_hits) > 20000:            # forget idle clients
            for k in [k for k, v in _rate_hits.items() if not v or now - v[-1] > 3600][:10000]:
                _rate_hits.pop(k, None)
    return None


def rate_limit(request, group: str) -> None:
    if not RATE_ON:
        return
    wait = _hit(client_ip(request), group, RATE_PER_MIN[group], 60.0)
    if wait is not None:
        raise ApiError(429, f"too many {group} requests from your address ({RATE_PER_MIN[group]} per minute); "
                            f"try again in {wait:.0f} s", {"Retry-After": f"{wait:.0f}"})


def _unhit(ip: str, group: str) -> None:
    """Take back the last counted request (a new field that could not be opened does not use up the limit)."""
    with _rate_lock:
        q = _rate_hits.get((ip, group))
        if q:
            q.pop()


def new_field_allowed(request, key: str) -> bool:
    """A field not yet on the server costs downloads: per-IP and server-wide limits before it is opened."""
    if not RATE_ON:
        return False
    reg = registry()
    with reg.lock:
        if key in reg.fields or key in reg.opening:
            return False
        active = sum(1 for fs in reg.fields.values()
                     if any(not j.event.is_set() for b, j in fs.jobs.items() if b not in fs.windows))
    if active >= MAX_ACTIVE_FIELDS:
        raise ApiError(429, f"the server is building {active} fields; try again in a minute (or open a gallery "
                            "target, which needs no download)", {"Retry-After": "60"})
    wait = _hit(client_ip(request), "new_field", NEW_FIELDS_PER_IP, NEW_FIELD_WINDOW_S)
    if wait is not None:
        raise ApiError(429, f"too many new fields from your address ({NEW_FIELDS_PER_IP} per "
                            f"{NEW_FIELD_WINDOW_S / 60:.0f} min); try again in {wait:.0f} s",
                       {"Retry-After": f"{wait:.0f}"})
    return True


# --------------------------------------------------------------------------- errors / json

def error_id(what: str, exc: BaseException | None = None) -> str:
    """Log an internal error with a short id and return the id; clients get the id, never the exception text
    (it can hold file paths or upstream pages)."""
    import uuid
    eid = uuid.uuid4().hex[:8]
    if exc is not None:
        log.error("[%s] %s: %s", eid, what, f"{type(exc).__name__}: {exc}", exc_info=exc)
    else:
        log.error("[%s] %s", eid, what)
    return eid


class ApiError(Exception):
    def __init__(self, status: int, message: str, headers: dict | None = None):
        super().__init__(message)
        self.status, self.message, self.headers = status, message, headers or {}


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
    write_lock: threading.Lock = dc_field(default_factory=threading.Lock)  # one manifest writer at a time
    windows: list = dc_field(default_factory=list)    # user windows w<lo>_<hi>, least recently asked first
    candidates_state: str | None = None                # None | running | ready | error: ...

    def status(self) -> dict:
        """Listed bands only (user windows are not in the manifest)."""
        return {b: ("ready" if j.state == "ready" else "empty" if j.state == "empty" else "pending")
                for b, j in self.jobs.items() if b not in self.windows}


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
    def near_key(self, key: str, ra: float, dec: float) -> str | None:
        """A free field already in memory that is the same request up to rounding: same fov and proper motion
        (the key after its position part) and query centre within half a grid pixel. The key rounds to 1e-4 deg,
        so 3.059049 and 3.05905 gave two keys and built the same 414-exposure field twice (14 Nov)."""
        tail = key.split("_", 2)[2]
        lim = P.PIX_ARCSEC / 2.0
        with self.lock:
            cands = [(k, fs.field) for k, fs in self.fields.items()
                     if k != key and fs.field.tinfo is None and k.startswith("r") and k.split("_", 2)[2:] == [tail]]
        for k, f in cands:
            dra = ((ra - f.query_ra + 180.0) % 360.0 - 180.0) * math.cos(math.radians(dec))
            if math.hypot(dra, dec - f.query_dec) * 3600.0 < lim:
                return k
        return None

    def get(self, key: str, open_kw: dict | None = None, queue: bool = True) -> FieldState:
        """open_kw: exact position for a new free field (the key itself is rounded).
        queue=False: open the field without queueing its bands (they still build when asked for)."""
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
            fs = self._register(f, queue)
            return fs
        finally:
            with self.lock:
                self.opening.pop(key, None)
            ev.set()

    def _register(self, f: P.Field, queue: bool = True) -> FieldState:
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
        if not queue:
            return fs
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
                job = fs.jobs.get(band)        # a user window may have been dropped while queued
                if job is None or job.state != "pending":
                    continue
                job.state, job.t_start, job.background = "running", time.time(), is_bg
            try:
                self._build(fs, job)
            except Exception as e:
                log.exception("band %s %s failed", key, band)
                job.state, job.error = "error", error_id(f"band {key} {band}", e)
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
        # the band is done; the manifest copy on disk is only a convenience and must not turn it into an error
        try:
            with fs.write_lock:
                P.write_json(fs.out / "manifest.json",
                             P.manifest(f, status=fs.status(), n_exposures=self.n_exposures(fs)))
        except OSError as e:
            log.warning("manifest copy for %s not written: %r", f.key, e)
        log.info("built %s %s: %d frames in %.1fs", f.key, band, len(frames), time.time() - job.t_start)
        cache_check_after_build()
        self.maybe_candidates(fs)

    def listed_bands_done(self, fs: FieldState) -> bool:
        return all(j.event.is_set() for b, j in fs.jobs.items() if b not in fs.windows)

    def maybe_candidates(self, fs: FieldState) -> None:
        """Once every listed band of the field is built: candidates.json, once, in a thread of its own."""
        path = fs.out / "candidates.json"
        with self.lock:
            if fs.candidates_state is not None or path.exists() or not self.listed_bands_done(fs):
                return
            if P.detect_module() is None:
                return
            fs.candidates_state = "running"
        threading.Thread(target=self._candidates, args=(fs,), daemon=True, name="candidates").start()

    def _candidates(self, fs: FieldState) -> None:
        t0 = time.time()
        try:
            gaia_ok = (fs.out / "gaia.json").exists()
            if not gaia_ok:
                gaia = gaia_for(fs.field)        # the detector still runs without it, without the known-star check
                if gaia is not None:
                    P.write_json(fs.out / "gaia.json", gaia, indent=None)
                    gaia_ok = True
            out = P.make_candidates(fs.out, fs.field.key)
            if not gaia_ok:
                out["gaia"] = ("not available: the Gaia archive did not answer, so no candidate is marked as a known "
                               "Gaia star and none is checked against Gaia")
            out["seconds"] = round(time.time() - t0, 2)
            P.write_json(fs.out / "candidates.json", out)
            fs.candidates_state = "ready"
            log.info("candidates %s: %d in %.1fs", fs.field.key, len(out.get("candidates", [])), time.time() - t0)
        except Exception as e:
            log.exception("candidates %s failed", fs.field.key)
            fs.candidates_state = "error: " + error_id(f"candidates {fs.field.key}", e)

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
        return {b: j.n_exposures for b, j in fs.jobs.items() if j.n_exposures is not None and b not in fs.windows}

    def manifest(self, fs: FieldState) -> dict:
        m = P.manifest(fs.field, status=fs.status(), n_exposures=self.n_exposures(fs))
        for b in m.get("bands", []):
            j = fs.jobs.get(b["id"])
            if j and j.state in ("running", "pending") and j.total:
                b["progress"] = round(j.done / j.total, 3)
            if j and j.state == "error":
                b["error"] = f"could not be built (error id {j.error})"
        return m

    def touch(self, key: str) -> None:
        """The page polls the manifest of the field on screen: its pending bands go first in the background."""
        with self.cv:
            mine = [it for it in self.background if it[0] == key]
            if mine:
                self.background = mine + [it for it in self.background if it[0] != key]

    def window_job(self, fs: FieldState, band: str) -> BandJob:
        """Job of a user window w<lo>_<hi>, made on first request (files from before a restart are reused).
        Keeps at most MAX_WINDOWS_PER_FIELD windows per field: the oldest finished one is dropped."""
        drop = []
        with self.lock:
            job = fs.jobs.get(band)
            if job is not None:
                if band in fs.windows:
                    fs.windows.remove(band)
                fs.windows.append(band)
                return job
            job = BandJob(band)
            if (fs.out / f"{band}.json").exists():
                job.state = "ready" if any(fs.out.glob(f"*_{band}.f32")) else "empty"
                job.event.set()
            elif not fs.field.refs:
                job.state = "empty"
                job.event.set()
            fs.jobs[band] = job
            fs.windows.append(band)
            while len(fs.windows) > MAX_WINDOWS_PER_FIELD:
                old = next((b for b in fs.windows if b != band and fs.jobs[b].event.is_set()), None)
                if old is None:
                    break
                fs.windows.remove(old)
                fs.jobs.pop(old, None)
                drop.append(old)
        for old in drop:
            files = list(fs.out.glob(f"*_{old}.f32")) + list(fs.out.glob(f"*_{old}_*.f32")) + [fs.out / f"{old}.json"]
            for pth in files:
                try:
                    pth.unlink()
                except OSError:
                    pass
        return job

    def wait_band(self, fs: FieldState, band: str, timeout: float) -> BandJob:
        if band not in fs.jobs:
            try:
                is_win = P.parse_window(band) is not None
            except ValueError as e:
                raise ApiError(400, str(e))
            if not is_win:
                raise ApiError(404, f"no band {band!r} for {fs.field.key}; bands: {list(fs.jobs)} or a window "
                                    f"w<lo>_<hi> (nm)")
        job = self.window_job(fs, band) if P.is_window(band) else fs.jobs[band]
        if not job.event.is_set():
            self.enqueue(fs.field.key, band, urgent=True)
            job.event.wait(timeout)
        return job


def _ades_schema_available() -> bool:
    from spx import mpc
    return mpc.SUBMIT_XSD.exists()


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
_janitor_started = False


#: IRSA SIA2 attempts per list (spx.access keeps its own default of 10; the frame code there is frozen, so the
#: server sets it). Measured 15 Nov from here: 6 of 30 SIA2 queries answered, 502s in 0.8-2.6 s, answers in
#: 23-24 s; simulated on those answers, budget 60 s with 20 attempts gets both collections of a field 96% of the
#: time (25 s / 10 attempts: 70%), median 31 s, 90th percentile 54 s. Set with SPX_SIA_RETRY_BUDGET_S too.
if os.environ.get("SPX_SIA_MAX_ATTEMPTS"):
    access.SIA_MAX_ATTEMPTS = int(os.environ["SPX_SIA_MAX_ATTEMPTS"])
#: Threads of the default pool (asyncio.to_thread). Python's default is CPU + 4: 5 on a one-CPU instance, and
#: opening a field holds one while IRSA is retried (up to a minute), so a few new fields would block everything.
THREAD_POOL = int(os.environ.get("SPX_THREADS") or 32)
_pool_set = False


def seed_sia_cache(gallery: Path | None = None, cache: Path | None = None) -> int:
    """Copy the gallery's SIA2 lists (web/data/_sia/, written by scripts/precompute.py) into the cache where they are
    missing, keeping their file time: the five targets then open without IRSA. A list older than
    access.SIA_MAX_AGE_DAYS is asked again, and kept if IRSA does not answer."""
    import shutil
    src = Path(gallery or P.GALLERY_DIR) / "_sia"
    dst = Path(cache or access.CACHE_DIR) / "sia"
    n = 0
    if src.is_dir():
        for f in src.rglob("*.json"):
            d = dst / f.relative_to(src)
            if not d.exists():
                d.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, d)
                n += 1
    return n


@app.middleware("http")
async def _start_janitor(request: Request, call_next):
    """At the first request (not at import: tests import the app): the default thread pool's size, and the
    disk-cache janitor."""
    global _janitor_started, _pool_set
    if not _pool_set:
        try:
            log.info("SIA2 seed: %d gallery image lists copied into the cache", seed_sia_cache())
        except OSError as e:
            error_id("SIA2 seed", e)
        from concurrent.futures import ThreadPoolExecutor
        asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=THREAD_POOL,
                                                                           thread_name_prefix="spx"))
        _pool_set = True
    if not _janitor_started and CACHE_MAX_BYTES > 0:
        _janitor_started = True
        threading.Thread(target=_cache_janitor, daemon=True, name="cache-janitor").start()
    return await call_next(request)


#: Response headers the browser may read across origins (/api/report: file name, schema check).
EXPOSE_HEADERS = "Content-Disposition, X-ADES-Schema-Checked"


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
    # The page runs on another origin: without this the browser hides the draft's file name and schema flag.
    resp.headers["Access-Control-Expose-Headers"] = EXPOSE_HEADERS
    return resp


@app.exception_handler(ApiError)
async def _api_error(request: Request, exc: ApiError):
    resp = jresp({"schema": P.SCHEMA, "message": exc.message}, exc.status)
    for k, v in exc.headers.items():
        resp.headers[k] = v
    return resp


@app.exception_handler(Exception)
async def _internal_error(request: Request, exc: Exception):
    """Unhandled errors: JSON like every other answer, with the CORS headers (this handler runs outside the CORS
    middleware), and an id instead of the exception text."""
    eid = error_id(f"{request.method} {request.url.path}", exc)
    resp = jresp({"schema": P.SCHEMA, "message": f"internal error (error id {eid}); this is a bug on our side"}, 500)
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Expose-Headers"] = EXPOSE_HEADERS
    return resp


@app.exception_handler(access.SiaError)
async def _sia_error(request: Request, exc: access.SiaError):
    return jresp({"schema": P.SCHEMA, "message": "IRSA SIA2 is not answering; try again in a few minutes "
                                                 f"(error id {error_id('SIA2', exc)})"}, 503)


@app.exception_handler(access.FetchError)
async def _fetch_error(request: Request, exc: access.FetchError):
    return jresp({"schema": P.SCHEMA, "message": "SPHEREx data on S3 is not reachable; try again in a few minutes "
                                                 f"(error id {error_id('S3', exc)})"}, 503)


async def _field(key: str, open_kw: dict | None = None, queue: bool = True) -> FieldState:
    return await asyncio.to_thread(registry().get, key, open_kw, queue)


@app.get("/api/manifest")
async def api_manifest(request: Request, target: str | None = None, ra: str | None = None, dec: str | None = None,
                       fov: float | None = None, pmra: float | None = None, pmdec: float | None = None,
                       ref_epoch: float | None = None, name: str | None = None, queue: int = 1):
    """pmra/pmdec (mas/yr) and ref_epoch (Julian year, default 2000.0) make a free position move like a
    named target (e.g. pass on what /api/resolve returned). queue=0 lists the field without starting any
    band (used by scripts/acceptance.py to time SIA2 alone)."""
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
        if key not in registry().fields:
            key = registry().near_key(key, r, d) or key
        kw = {"ra": r, "dec": d, "fov_deg": fv, "pm": pm, "name": name,
              "source": "SIMBAD via CDS Sesame (proper motion given)" if pm else None}
    rate_limit(request, "manifest")
    counted = new_field_allowed(request, key)
    try:
        fs = await _field(key, kw, bool(queue))
    except Exception:
        if counted:   # IRSA did not answer, etc.: the attempt does not use up this address's new-field limit
            _unhit(client_ip(request), "new_field")
        raise
    registry().touch(fs.field.key)
    return jresp(registry().manifest(fs))


#: Requests waiting for a band at once. The wait itself holds no thread: the job is queued in a thread at once,
#: then polled from the event loop. (Holding a thread for up to 90 s each would starve the default pool, which has
#: only CPU + 4 threads: 5 on a one-CPU Cloud Run instance, shared with field opening, spectra and names.)
MAX_BAND_WAITERS = int(os.environ.get("SPX_MAX_BAND_WAITERS") or 64)
_band_waiters = 0


def check_band_id(band: str) -> None:
    """Reject a band id that no field can have BEFORE the field is opened (opening it asks IRSA, and an IRSA outage
    must not turn a bad id into a 503): a malformed window w<lo>_<hi> is 400; an id that is neither D1..D6, a
    window, nor a gallery target's own band (e.g. W) is 404."""
    if band.startswith("D") and band[1:].isdigit() and int(band[1:]) in access.DETECTOR_BANDS_UM:
        return
    try:
        if P.parse_window(band) is not None:
            return
    except ValueError as e:
        raise ApiError(400, str(e))
    known = {b["id"] for t in TARGETS.values() for b in t.get("extra_bands", [])}
    if band not in known:
        raise ApiError(404, f"no band {band!r}: bands are D1..D6, a window w<lo>_<hi> (nm), or a target's own band "
                            f"({', '.join(sorted(known))})")


async def wait_band_async(fs: FieldState, band: str, timeout: float) -> BandJob:
    global _band_waiters
    job = await asyncio.to_thread(registry().wait_band, fs, band, 0.0)      # queue it (urgent), no waiting here
    if job.event.is_set():
        return job
    if _band_waiters >= MAX_BAND_WAITERS:
        raise ApiError(429, f"{_band_waiters} requests are already waiting for bands; try again in a few seconds",
                       {"Retry-After": "10"})
    _band_waiters += 1
    try:
        t0 = time.monotonic()
        while not job.event.is_set() and time.monotonic() - t0 < timeout:
            await asyncio.sleep(0.25)
    finally:
        _band_waiters -= 1
    return job


@app.get("/api/band")
async def api_band(request: Request, key: str, band: str):
    check_band_id(band)
    rate_limit(request, "band")
    fs = await _field(key)
    job = await wait_band_async(fs, band, BAND_WAIT_S)
    if job.state == "error":
        raise ApiError(503, f"band {band} could not be built (error id {job.error}); try again later")
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
    check_band_id(band)
    fs = await _field(key)
    job = await wait_band_async(fs, band, BAND_WAIT_S)
    if not job.event.is_set():
        raise ApiError(504, f"band {band} is still being built; try again")
    path = fs.out / (f"{epoch}_{band}" + (f"_{kind}" if kind else "") + ".f32")
    if not path.exists():
        raise ApiError(404, f"no frame for epoch {epoch} in band {band}")
    return Response(path.read_bytes(), media_type="application/octet-stream",
                    headers={"Cache-Control": "public, max-age=3600"})


@app.get("/api/candidates")
async def api_candidates(request: Request, key: str):
    """Automatic candidates (spx.detect), made once all listed bands of the field are built.
    202 {"status": "pending"} until then; 503 when the detector is missing or failed."""
    rate_limit(request, "candidates")
    fs = await _field(key)
    path = fs.out / "candidates.json"
    if path.exists():
        return Response(path.read_bytes(), media_type="application/json")
    if P.detect_module() is None:
        raise ApiError(503, "the candidate finder (spx.detect) is not installed on this server")
    st = fs.candidates_state
    if st and st.startswith("error"):
        raise ApiError(503, f"candidate search failed (error id {st[7:]})")
    reg = registry()
    if st is None:
        reg.maybe_candidates(fs)
    pending = [b for b, j in fs.jobs.items() if b not in fs.windows and not j.event.is_set()]
    for b in pending:                  # the page waits for these: build them next
        reg.enqueue(fs.field.key, b, urgent=False)
    reg.touch(fs.field.key)
    return jresp({"schema": P.SCHEMA, "key": fs.field.key, "status": "pending", "bands_pending": pending,
                  "message": "candidates are searched once every band of the field is built"}, status=202)


@app.get("/api/catwise")
async def api_catwise(key: str):
    """CatWISE2020 sources of a gallery field: its web/data/<key>/catwise.json as is (scripts/make_catwise.py).
    Free fields have none yet: 404 + message (a live IRSA fetch is planned after the challenge)."""
    path = P.GALLERY_DIR / key / "catwise.json"
    if key not in TARGETS or not path.exists():
        raise ApiError(404, "no WISE catalogue for this field (CatWISE2020 is served for the gallery targets only)")
    return Response(path.read_bytes(), media_type="application/json")


_gallery_gaia: dict = {}
GAIA_MISSING = ("The Gaia archive is not answering, so Gaia stars cannot be shown right now; try again in a few "
                "minutes")


def gaia_for(field: P.Field) -> list | None:
    """Gaia DR3 sources of a field: a gallery target's are read from its web/data/<key>/gaia.json (the same query,
    made when the gallery was built: the archive is not asked again); other fields ask the ESA archive within
    P.GAIA_BUDGET_S. None when the archive does not answer."""
    if field.tinfo is not None:
        if field.key not in _gallery_gaia:
            path = P.GALLERY_DIR / field.key / "gaia.json"
            _gallery_gaia[field.key] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
        if _gallery_gaia[field.key] is not None:
            return _gallery_gaia[field.key]
    try:
        return P.gaia_sources(field)
    except Exception as e:
        error_id("gaia " + field.key, e)
        return None


@app.get("/api/gaia")
async def api_gaia(key: str):
    fs = await _field(key)
    g = await asyncio.to_thread(gaia_for, fs.field)
    if g is None:
        raise ApiError(503, GAIA_MISSING)
    return jresp(g)


_spec_cache: OrderedDict = OrderedDict()


@app.get("/api/spectrum")
async def api_spectrum(request: Request, key: str, ra: float | None = None, dec: float | None = None):
    rate_limit(request, "spectrum")
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
        gaia = gaia_for(f)
        out = P.spectrum(f, cuts, ra, dec, gaia=gaia or [])
        # say it when there was no Gaia list: the photometry then cannot remove neighbouring stars
        out["meta"]["gaia"] = ("Gaia DR3 stars within 25 arcsec are fitted and removed as neighbours" if gaia is not None
                               else "not available: the Gaia archive did not answer, so neighbouring stars were NOT "
                                    "removed; a blended point can include a neighbour's light")
        out["meta"]["bands_included"] = [f"D{d}" for d in dets]
        # where it was measured: a fixed point stays put while a fast target moves off it (WISE 0855 at its last
        # track point: 28 of 255 points flagged, at the target 23 of 255, the gallery's number)
        out["meta"]["position"] = (
            "target, moved to each exposure with its proper motion (as the gallery's spectrum.json)" if ra is None else
            f"fixed point RA {ra:.6f}, Dec {dec:.6f} for every exposure (a moving target is not followed)")
        out["meta"]["layers"] = list(LIVE_LAYERS)
        out["meta"]["bands_pending"] = [b for b in fs.jobs if b.startswith("D") and f"D{b[1:]}" not in ready]
        return out
    out = await asyncio.to_thread(run)
    if out["meta"]["gaia"].startswith("not available"):
        return jresp(out)        # not kept: asked again once the archive answers
    _spec_cache[ck] = out
    while len(_spec_cache) > 200:
        _spec_cache.popitem(last=False)
    return jresp(out)


#: Name resolutions: at most RESOLVE_MAX names, least recently used dropped; a name found is kept
#: RESOLVE_TTL_S, a name SIMBAD does not know RESOLVE_NEG_TTL_S; an outage (no answer, non-200) is never cached.
_resolve_cache: OrderedDict = OrderedDict()     # normalized name -> (time, answer or None)
RESOLVE_MAX = 2000
RESOLVE_TTL_S = 7 * 86400.0
RESOLVE_NEG_TTL_S = 600.0


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower().replace("'s", ""))


@app.get("/api/resolve")
async def api_resolve(name: str = Query(..., min_length=1, max_length=200)):
    n = _norm(name)
    for key, t in TARGETS.items():
        if n in (_norm(key), _norm(t["name"])):
            return jresp({"name": t["name"], "ra": t["ra"], "dec": t["dec"], "pmra": t["pmra"], "pmdec": t["pmdec"],
                          "ref_epoch": t["ref_epoch"], "source": t["source"], "target": key})
    hit = _resolve_cache.get(n)
    if hit is not None and time.time() - hit[0] <= (RESOLVE_TTL_S if hit[1] is not None else RESOLVE_NEG_TTL_S):
        _resolve_cache.move_to_end(n)
        r = hit[1]
    else:
        r = await asyncio.to_thread(_sesame, name)      # raises 503 on an outage: nothing cached
        _resolve_cache[n] = (time.time(), r)
        _resolve_cache.move_to_end(n)
        while len(_resolve_cache) > RESOLVE_MAX:
            _resolve_cache.popitem(last=False)
    if r is None:
        raise ApiError(404, f"name {name!r} not found (SIMBAD via CDS Sesame)")
    return jresp(r)


def _sesame(name: str) -> dict | None:
    import requests
    try:
        resp = requests.get("https://cds.unistra.fr/cgi-bin/nph-sesame/-oxp/SNV?" + quote(name), timeout=5)
    except (requests.ConnectionError, requests.Timeout) as e:
        raise ApiError(503, f"name resolver not answering (error id {error_id('resolve', e)})")
    if resp.status_code != 200:   # an outage, not an answer (CDS showed "Page unavailable" on 14 Nov)
        raise ApiError(503, f"name resolver not answering (HTTP {resp.status_code}); try again later")
    txt = resp.text

    def g(tag, s=txt):
        m = re.search(f"<{tag}>([^<]*)</{tag}>", s)
        return m.group(1).strip() if m else None
    ra, dec = g("jradeg"), g("jdedeg")
    if ra is None or dec is None:
        return None
    pm = re.search(r"<pm>(.*?)</pm>", txt, re.S)
    pmra = float(g("pmRA", pm.group(1))) if pm and g("pmRA", pm.group(1)) else 0.0
    pmdec = float(g("pmDE", pm.group(1))) if pm and g("pmDE", pm.group(1)) else 0.0
    return {"name": g("oname") or name, "ra": float(ra), "dec": float(dec), "pmra": pmra, "pmdec": pmdec,
            "ref_epoch": 2000.0, "source": "SIMBAD via CDS Sesame"}


def _validation_status() -> dict:
    from spx import mpc
    try:
        return mpc.validation_status()
    except Exception as e:  # health must answer even if the check itself breaks
        return {"status": "error", "message": f"error id {error_id('validation status', e)}"}


@app.get("/api/health")
async def api_health():
    reg = registry()
    with reg.lock:
        fields = len(reg.fields)
        queued = {"urgent": len(reg.urgent), "background": len(reg.background)}
        running = sum(j.state == "running" for fs in reg.fields.values() for j in fs.jobs.values())
    return jresp({"schema": P.SCHEMA, "ok": True, "version": spx.__version__, "commit": STARTED_COMMIT.get("commit"),
                  "uncommitted_code_at_start": STARTED_COMMIT.get("uncommitted_code"),
                  "collections": list(access.COLLECTIONS), "max_fov_deg": P.MAX_FOV_DEG,
                  "live_layers": list(LIVE_LAYERS), "fields_in_memory": fields, "bands_queued": queued, "bands_running": running,
                  "s3_max_concurrency": access.S3_MAX_CONCURRENCY, "code_hash": CODE_HASH,
                  "spx_frame_code_hash": spx.frame_code_hash(), "spx_code_hash": spx.code_hash(),
                  "ades_schema": _ades_schema_available(), "astrometry_validation": _validation_status(),
                  "cache": {"bytes": CACHE_STATE["bytes"], "max_bytes": CACHE_MAX_BYTES,
                            "checked_utc": None if CACHE_STATE["checked"] is None else
                            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(CACHE_STATE["checked"])),
                            "last_cleanup": CACHE_STATE["last_cleanup"]},
                  "band_waiters": {"now": _band_waiters, "max": MAX_BAND_WAITERS},
                  "sia": {"retry_budget_s": access.SIA_RETRY_BUDGET_S, "max_attempts": access.SIA_MAX_ATTEMPTS},
                  "threads": THREAD_POOL,
                  "memory_limits": {"fields_in_memory": MAX_FIELDS_IN_MEMORY, "cutout_sets": MAX_CUTOUT_SETS,
                                    "cutout_sets_in_memory": len(reg.cutouts),
                                    "windows_per_field": MAX_WINDOWS_PER_FIELD},
                  "network_totals": dict(access.TOTALS)})


# --------------------------------------------------------------------------- moving object: track and MPC draft

_reports: OrderedDict = OrderedDict()     # report_id -> {rows, info, band, created}
MAX_REPORTS = 200


def _night_label(rows, group) -> str:
    from astropy.time import Time
    t0 = min(rows[i]["mjd_mid"] for i in group)
    return Time(t0, format="mjd").iso[:10]


def _track_response(rid: str, rows: list, info: dict, band: str, n_refs: int) -> dict:
    from spx import mpc
    regime = info.get("regime")
    timed = [i for i, r in enumerate(rows) if r.get("obs_time")]
    left_out = mpc.select_for_ades([rows[i] for i in timed]) if timed else {}
    left_out = {timed[k]: v for k, v in left_out.items()}
    in_ades = {i for i in timed if i not in left_out} if regime == mpc.ADES_REGIME else set()
    groups = mpc.night_windows(rows, timed) if timed else []
    night_of = {i: _night_label(rows, g) for g in groups for i in g}
    nights = []
    for g in groups:
        n_in = sum(1 for i in g if i in in_ades)
        nights.append({"night": _night_label(rows, g), "n_total": len(g),
                       "n_accepted": sum(1 for i in g if rows[i].get("accepted")),
                       "n_in_draft": n_in, "included": regime == mpc.ADES_REGIME and n_in >= mpc.MIN_DETECTIONS})
    meas = []
    for i, r in enumerate(rows):
        m = dict(r)
        m["mjd"] = r.get("mjd_mid")
        m["night"] = night_of.get(i)
        m["in_ades"] = i in in_ades
        if r.get("accepted") and i in left_out:
            m["reason"] = left_out[i]
        meas.append(m)
    span = float(info.get("span_days") or 0.0)
    if regime == mpc.ADES_REGIME:
        why = (f"The marks span {span * 24:.1f} h, within one survey visit: motion like a Solar System object. "
               "An MPC draft can be made from the accepted single exposures.")
    else:
        why = (f"The marks span {span:.0f} days, across survey epochs: this is the proper motion of a star or a brown "
               "dwarf. The Minor Planet Center takes Solar System objects only; use the CSV.")
    sp = float(info.get("speed_arcsec_per_s") or 0.0)
    v = mpc.validation_summary()
    validation = {"text": "\n".join(mpc.validation_lines(v)),
                  "timing": "verified" if (v and v.get("timing_verified")) else "not verified",
                  "pixel_scale": "Pixel scale 6.15 arcsec (MPC guidance for ground observers: 2-3 arcsec); measured "
                                 "single-exposure accuracy is in the validation text.",
                  "same_frame_code": None if v is None else v["same_frame_code"]}
    return {"schema": P.SCHEMA, "report_id": rid, "band": band, "regime": regime, "regime_reason": why,
            "rate": {"arcsec_per_hour": sp * 3600.0, "arcsec_per_year": sp * 86400.0 * 365.25,
                     "arcsec_per_day": list(info.get("rate_arcsec_per_day") or [])},
            "window_mjd": list(info.get("window_mjd") or []), "n_exposures_found": n_refs,
            "n_measured": len(rows), "n_accepted": sum(1 for r in rows if r.get("accepted")),
            "n_in_draft": len(in_ades), "nights": nights, "measurements": meas, "validation": validation,
            "gaia": info.get("gaia"),
            "mpc_test_forms": {"psv": mpc.MPC_PSV_TEST, "xml": mpc.MPC_XML_TEST}}


#: /api/track limits for a public server: marks, body size, SIA2 points along the path (cost), and the time
#: window of a within-visit (Solar System) track. Tracks across survey epochs (proper-motion stars, CSV only) span
#: up to ~500 days by nature; their path is short, so the SIA2-point limit bounds them.
TRACK_MAX_MARKS = 20
TRACK_MAX_BODY_BYTES = 16384
TRACK_MAX_SIA_POINTS = 12
TRACK_MAX_WINDOW_DAYS = 60.0


def track_cost(marks) -> dict:
    """The SIA2 points and time window /api/track would use (same rule as pipeline.track_refs), without querying."""
    import math
    from spx.astrometry import track_model
    position, info = track_model(marks)
    lo, hi = info["window_mjd"]
    within = bool(np.isfinite(lo) and np.isfinite(hi))
    if not within:
        ms = [m["mjd"] for m in marks]
        lo, hi = min(ms) - 1.0, max(ms) + 1.0
    sep = position(lo).separation(position(hi)).deg
    n = max(2, int(math.ceil(sep / P.TRACK_SIA_STEP_DEG)) + 1)
    return {"sia_points": n, "window_days": float(hi - lo), "within_visit": within, "path_deg": float(sep)}


@app.post("/api/track")
async def api_track(request: Request):
    """Measure a marked moving object on single exposures. Body: {"band": "D1".."D6", "marks": [{ra, dec, mjd}, ...]}
    (>= 2 marks, same object, different times). Downloads the cutouts it needs (S3), never contacts the MPC."""
    import uuid
    rate_limit(request, "track")
    raw = await request.body()
    if len(raw) > TRACK_MAX_BODY_BYTES:
        raise ApiError(413, f"body too large ({len(raw)} bytes; at most {TRACK_MAX_BODY_BYTES})")
    try:
        body = await request.json()
    except Exception:
        raise ApiError(400, "body must be JSON: {band, marks: [{ra, dec, mjd}]}")
    if not isinstance(body, dict):
        raise ApiError(400, "body must be a JSON object: {band, marks: [{ra, dec, mjd}]}")
    band = str(body.get("band") or "")
    marks = body.get("marks") or []
    if not isinstance(marks, list) or len(marks) > TRACK_MAX_MARKS:
        raise ApiError(400, f"marks must be a list of at most {TRACK_MAX_MARKS} points")
    try:
        marks = [{"ra": float(m["ra"]) % 360.0, "dec": float(m["dec"]), "mjd": float(m["mjd"])} for m in marks]
    except (KeyError, TypeError, ValueError):
        raise ApiError(400, "each mark needs numeric ra, dec (deg) and mjd")
    if len(marks) < 2 or len({round(m["mjd"], 6) for m in marks}) < 2:
        raise ApiError(400, "give at least two marks of the same object at different times")
    if any(not -90 <= m["dec"] <= 90 for m in marks):
        raise ApiError(400, "dec must be within [-90, 90]")
    try:
        P.track_detectors(band)
    except ValueError as e:
        raise ApiError(400, str(e))
    try:
        cost = await asyncio.to_thread(track_cost, marks)
    except ValueError as e:
        raise ApiError(400, str(e))
    if cost["sia_points"] > TRACK_MAX_SIA_POINTS:
        raise ApiError(400, f"the marked path is {cost['path_deg']:.1f} deg long ({cost['sia_points']} image searches); "
                            f"at most {TRACK_MAX_SIA_POINTS} on this server: mark a shorter stretch")
    if cost["within_visit"] and cost["window_days"] > TRACK_MAX_WINDOW_DAYS:
        raise ApiError(400, f"the track spans {cost['window_days']:.0f} days; at most {TRACK_MAX_WINDOW_DAYS:.0f} "
                            "for a moving object within a visit")
    try:
        rows, info, n_refs = await asyncio.to_thread(P.measure_marks, marks, band)
    except ValueError as e:
        raise ApiError(400, str(e))
    rid = uuid.uuid4().hex[:16]
    _reports[rid] = {"rows": rows, "info": info, "band": band, "created": time.time()}
    while len(_reports) > MAX_REPORTS:
        _reports.popitem(last=False)
    return jresp(_track_response(rid, rows, info, band, n_refs))


@app.get("/api/report")
async def api_report(id: str, format: str = "csv", submitter: str | None = None, trksub: str | None = None):
    """The draft for a measured track: format=csv (always), ades (PSV) or ades_xml. ADES needs a submitter
    ('I. M. Submit'), a within-visit track and >= 3 accepted positions in one night; every ADES file passes the
    ADES submit schema before it is handed out (when the schema is installed)."""
    from spx import mpc
    rep = _reports.get(id)
    if rep is None:
        raise ApiError(404, "unknown or expired report id; measure the track again")
    rows, info = rep["rows"], rep["info"]
    fmt = {"ades_psv": "ades"}.get(format, format)
    if fmt not in ("csv", "ades", "ades_xml"):
        raise ApiError(400, "format must be csv, ades or ades_xml")
    trk = None
    if trksub:
        try:
            trk = mpc.check_trksub(trksub.strip())
        except ValueError as e:
            raise ApiError(400, str(e))
    timed = [r for r in rows if r.get("obs_time")]
    trk = trk or (mpc.make_trksub(timed) if timed else "SPX0000")
    stamp = (timed[0]["obs_time"][:10].replace("-", "") if timed else "nodata")
    if fmt == "csv":
        note = None if info.get("regime") == mpc.ADES_REGIME else \
            "Proper motion between survey epochs: not for the Minor Planet Center (Solar System objects only)."
        text = mpc.write_csv(rows, {"trk_sub": trk, "note": note})
        return Response(text, media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="spherex_{trk}_{stamp}.csv"'})
    if info.get("regime") != mpc.ADES_REGIME:
        raise ApiError(422, "An MPC draft is only for motion within one survey visit (Solar System objects). This "
                            "track spans survey epochs: a star or brown dwarf; the MPC does not take those. Use the CSV.")
    try:
        name = mpc.check_name(submitter or "")
    except ValueError as e:
        raise ApiError(400, str(e))
    ctx = {"submitter": name, "trk_sub": trk, "tool_version": spx.__version__, "code_hash": spx.frame_code_hash()}
    try:
        text, checked = await asyncio.to_thread(mpc.checked_ades, rows, ctx, "xml" if fmt == "ades_xml" else "psv")
    except mpc.SchemaError as e:
        log.error("ADES draft for report %s failed the submit schema: %s", id, e)
        raise ApiError(500, "The generated MPC draft failed the ADES submit schema. This is a bug on our side, not "
                            "yours; the file was withheld.")
    except ValueError as e:
        raise ApiError(422, str(e))
    ext, mt = ("xml", "application/xml; charset=utf-8") if fmt == "ades_xml" else ("psv", "text/plain; charset=utf-8")
    return Response(text, media_type=mt, headers={
        "Content-Disposition": f'attachment; filename="spherex_{trk}_{stamp}_ades.{ext}"',
        "X-ADES-Schema-Checked": "true" if checked else "false"})
