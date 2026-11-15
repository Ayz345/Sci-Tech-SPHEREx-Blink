"""Field pipeline shared by the API server and scripts/precompute.py (data contract v1: web/tool/README.md).

    field = open_field(target="wise-0855")            # or open_field(ra=..., dec=..., fov_deg=...): SIA2 only
    man   = manifest(field)                             # dict, JSON-ready
    bj, cuts = build_band(field, "D6", out_dir)         # S3 cutouts -> spx.coadd frames -> .f32 files + band dict
    gaia  = gaia_sources(field)                         # Gaia DR3 (ESA archive TAP), G < 19
    spec  = spectrum(field, cuts_all_bands, ra, dec)    # spx.photometry.spectrum_points, passed through as is

File names inside `out_dir` are the static ones: {epoch}_{band}.f32, _weight.f32, _flagfrac.f32, {band}.json.
NaN (no data) and +Inf (saturated) are written into the .f32 files as they are. JSON never carries a non-finite
number: those become null.
"""
from __future__ import annotations

import json
import logging
import math
import os
import subprocess
import time
import warnings
from dataclasses import dataclass, field as dc_field
from pathlib import Path

import numpy as np
from astropy.coordinates import SkyCoord
import astropy.units as u

import spx
from spx import access
from spx.coadd import build_frames, common_scale, half_px_for, make_grid, target_at
from spx.targets import TARGETS, target_coord

log = logging.getLogger("spx.pipeline")

SCHEMA = 1
PIX_ARCSEC = 3.0
DEFAULT_FOV_DEG = 7.0 / 60
#: Largest field the server accepts (deg): 15' on request (default stays 7'). Measured 14 Nov, cold, home link:
#: a 15' field took 30-70 s per band, 230-330 s for six bands and 730-760 MB from S3 (handoff/results/
#: fov_timing_v2.json); the page says so before loading one. SPX_MAX_FOV_DEG overrides.
MAX_FOV_DEG = float(os.environ.get("SPX_MAX_FOV_DEG") or 0.25)
#: Mid-mission epoch used to point the SIA2 query at a moving named target (DR1 spans MJD 60790-61318).
QUERY_MJD = 61050.0
MAX_PER_EPOCH_BAND = access.MAX_PER_EPOCH_BAND
MIN_EXPOSURES = 3
#: A frame built from fewer exposures is marked "quality": "low" in {band}.json (patchy coverage, ghosts survive).
MIN_GOOD_EXPOSURES = 5

# Verbatim from https://irsa.ipac.caltech.edu/Missions/spherex.html (read 14 Nov 2026).
ACKNOWLEDGEMENT = ("This publication makes use of data products from the Spectro-Photometer for the History of the "
                   "Universe, Epoch of Reionization and Ices Explorer (SPHEREx), which is a joint project of the Jet "
                   "Propulsion Laboratory and the California Institute of Technology, and is funded by the National "
                   "Aeronautics and Space Administration.")

DETECTOR_BANDS = [{"id": f"D{d}", "label": f"{lo:.2f}–{hi:.2f} µm", "wave_um": [lo, hi]}
                  for d, (lo, hi) in sorted(access.DETECTOR_BANDS_UM.items())]

# --------------------------------------------------------------------------- user wavelength windows
#: Band id w<lo>_<hi> in whole nanometres, e.g. w4300_5000. Built on request by the live server only.
WINDOW_PREFIX = "w"
WINDOW_NM_RANGE = (750, 5000)
#: Narrowest window. Measured on the five gallery fields (spectrum.json, one point per exposure): the bandwidth of
#: one exposure at the target is 17-30 nm (D1), 25-40 (D2), 39-60 (D3), 47-104 (D4), 35-38 (D5, D6), so a window
#: narrower than ~50 nm adds no spectral detail, it only drops exposures. Exposures per epoch per nm of window:
#: 0.013 (D4) to 0.065 (D1) on typical fields, so 50 nm holds ~1-3 exposures per epoch (frames marked low) and
#: a frame of quality ok (>= 5) needs ~80-400 nm.
MIN_WINDOW_NM = 50
WINDOW_NOTE = ("Custom window: an exposure is used when its wavelength at the field centre (the target, for a named "
               "target) lies inside the window; one value per exposure, as for the other bands. n_exposures counts "
               "those exposures per epoch before masking.")


def parse_window(band_id: str) -> dict | None:
    """Band definition of a user window id, None when band_id is not one (D1..D6, W, ...).
    ValueError with a readable message for an id that starts with 'w' but is malformed or out of range."""
    if not isinstance(band_id, str) or not band_id.startswith(WINDOW_PREFIX):
        return None
    parts = band_id[len(WINDOW_PREFIX):].split("_")
    if len(parts) != 2 or not all(x.isdigit() and 3 <= len(x) <= 4 for x in parts):
        raise ValueError(f"band {band_id!r}: a wavelength window is w<lo>_<hi> in whole nanometres, e.g. w4300_5000")
    lo, hi = int(parts[0]), int(parts[1])
    nlo, nhi = WINDOW_NM_RANGE
    if not (nlo <= lo < hi <= nhi):
        raise ValueError(f"band {band_id!r}: need {nlo} <= lo < hi <= {nhi} nm")
    if hi - lo < MIN_WINDOW_NM:
        raise ValueError(f"band {band_id!r}: the window must be at least {MIN_WINDOW_NM} nm wide (one exposure "
                         f"covers 17-104 nm)")
    return {"id": band_id, "label": f"{lo / 1000:.3f}–{hi / 1000:.3f} µm", "wave_um": [lo / 1000, hi / 1000],
            "window": True}


def is_window(band_id: str) -> bool:
    try:
        return parse_window(band_id) is not None
    except ValueError:
        return False


def processing_text() -> str:
    return (f"spx {spx.__version__}: FLAGS mask, local background, clipped inverse-variance mean per epoch and band, "
            f"fixed {PIX_ARCSEC:g} arcsec TAN grid (north up); data {', '.join(access.COLLECTIONS)}")


# --------------------------------------------------------------------------- helpers

def json_safe(o):
    """numpy -> python, non-finite floats -> None (JSON cannot carry NaN/Infinity)."""
    if isinstance(o, dict):
        return {str(k): json_safe(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [json_safe(v) for v in o]
    if isinstance(o, np.ndarray):
        return [json_safe(v) for v in o.tolist()]
    if isinstance(o, (np.bool_,)):
        return bool(o)
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (float, np.floating)):
        f = float(o)
        return f if math.isfinite(f) else None
    return o


def dumps(o, indent=None) -> str:
    return json.dumps(json_safe(o), allow_nan=False, ensure_ascii=False, indent=indent)


def _tmp_name(path: Path) -> Path:
    """Temp file unique to this process and thread: two writers of the same file never share it."""
    import threading
    return path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")


def _replace(tmp: Path, path: Path, tries: int = 8) -> None:
    """os.replace, retried while Windows reports the target busy (another process reading it)."""
    for i in range(tries):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if i == tries - 1:
                try:
                    tmp.unlink()
                except OSError:
                    pass
                raise
            time.sleep(0.05 * (i + 1))


def write_json(path: Path, o, indent=1) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_name(path)
    tmp.write_text(dumps(o, indent), encoding="utf-8", newline="\n")
    _replace(tmp, path)


def write_f32(path: Path, a: np.ndarray) -> None:
    """Little-endian float32, row 0 = south (grid y = 0). NaN and +Inf are kept as they are."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_name(path)
    tmp.write_bytes(np.ascontiguousarray(a, dtype="<f4").tobytes())
    _replace(tmp, path)


def _enc(x: float, nd: int) -> str:
    x = round(float(x), nd) + 0.0
    return ("m" if x < 0 else "") + f"{abs(x):.{nd}f}".replace(".", "p")


def grid_px(fov_deg: float) -> int:
    """Grid pixels across for a requested field (even, as spx.coadd.make_grid makes them). A 0.01 px tolerance makes
    0.1166, 0.116666 and 0.11667 deg (all meant as 7') give the same 140."""
    n = int(math.ceil(float(fov_deg) * 3600.0 / PIX_ARCSEC - 0.01))
    return max(2, n + n % 2)


def canonical_fov(fov_deg: float) -> float:
    """The field size the key and the grid both come from: exactly grid_px(fov_deg) pixels in make_grid (a hair
    under n pixels, so its ceil gives n). A free field is opened, keyed and reopened with this value only."""
    return (grid_px(fov_deg) * PIX_ARCSEC - 1e-6) / 3600.0


def free_key(ra: float, dec: float, fov_deg: float, pm: tuple | None = None) -> str:
    """r133p7951_dm7p2451_n140 (p = decimal point, dp/dm = Dec sign, n = grid pixels across, 3 arcsec each).
    The key carries the grid size itself, so a field reopened from its key gets the same grid (with the field in
    degrees rounded to 0.001, 0.1166 deg gave a 140-pixel grid and its key, reopened, 142).

    With a proper motion (pmra, pmdec mas/yr at ref_epoch, e.g. from /api/resolve) the key gets
    _pm<pmra>_<pmdec>_e<epoch> (m = minus), e.g. ..._pmm3781p74_769p47_e2000p000.
    """
    ra = round(float(ra) % 360.0, 4) % 360.0  # 359.99999 -> 0.0000, never 360.0000
    dec = round(float(dec), 4) + 0.0  # -0.0 -> 0.0
    s = "dm" if dec < 0 else "dp"
    key = (f"r{ra:.4f}_{s}{abs(dec):.4f}").replace(".", "p") + f"_n{grid_px(fov_deg)}"
    if pm is not None:
        key += f"_pm{_enc(pm[0], 2)}_{_enc(pm[1], 2)}_e{_enc(pm[2], 3)}"
    return key


def spx_commit() -> dict:
    """Commit of the code that produced the data (the git dir may be outside the work tree), and the
    code files under spx/, server/, scripts/ that differ from it (empty list = clean)."""
    root = Path(__file__).resolve().parent.parent
    for gd in (os.environ.get("SPX_GIT_DIR"), str(root.parent / (root.name + ".git")), str(root / ".git")):
        if not gd or not Path(gd).exists():
            continue
        try:
            git = ["git", f"--git-dir={gd}", f"--work-tree={root}"]
            r = subprocess.run(git + ["rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=10)
            if r.returncode != 0:
                continue
            st = subprocess.run(git + ["status", "--porcelain", "--untracked-files=all", "--", "spx", "server", "scripts"],
                                capture_output=True, text=True, timeout=20)
            dirty = [ln[3:] for ln in st.stdout.splitlines() if ln.strip() and ln[3:].endswith(".py")]
            return {"commit": r.stdout.strip(), "uncommitted_code": dirty}
        except (OSError, subprocess.TimeoutExpired):
            pass
    return {"commit": os.environ.get("SPX_COMMIT"), "uncommitted_code": None}  # e.g. inside the Docker image


# --------------------------------------------------------------------------- field

@dataclass
class Field:
    key: str
    name: str
    query_ra: float
    query_dec: float
    fov_deg: float
    target: SkyCoord               # with pm + obstime for named targets; static point otherwise
    tinfo: dict | None             # TARGETS entry for named targets
    refs: list
    labels: np.ndarray             # epoch label per ref (from ALL refs)
    epochs: list                   # epoch_table
    center: SkyCoord
    grid: object
    notes: list = dc_field(default_factory=list)
    extra_bands: list = dc_field(default_factory=list)   # custom windows (the target's, or a gallery target's in the field)
    moving: bool = False           # target has a proper motion (named target, or ra/dec given with pm)
    source: str | None = None      # where the target position / motion comes from

    @property
    def fov_arcmin(self) -> float:
        return self.fov_deg * 60.0

    @property
    def half_px(self) -> int:
        return half_px_for(self.fov_arcmin)

    def band_def(self, band_id: str) -> dict | None:
        """Listed bands (D1..D6 and the field's own windows) or a user window w<lo>_<hi> (not listed)."""
        for b in self.band_defs():
            if b["id"] == band_id:
                return b
        try:
            return parse_window(band_id)
        except ValueError:
            return None

    def band_defs(self) -> list[dict]:
        out = [dict(b) for b in DETECTOR_BANDS]
        for b in self.extra_bands:
            if all(o["id"] != b["id"] for o in out):
                out.append({"id": b["id"], "label": b["label"], "wave_um": list(b["wave_um"])})
        return out

    def detectors_for(self, band_id: str) -> list[int]:
        b = self.band_def(band_id)
        if b is None:
            raise KeyError(band_id)
        if band_id.startswith("D") and band_id[1:].isdigit():
            return [int(band_id[1:])]
        lo, hi = b["wave_um"]
        # exposure selection: the measured LVF ends when the co-add module has them, else the nominal ranges
        from spx import coadd
        ranges = getattr(coadd, "DETECTOR_LVF_RANGE_UM", None) or access.DETECTOR_BANDS_UM
        return [d for d, (dlo, dhi) in sorted(ranges.items()) if dlo < hi and dhi > lo]


def open_field(target: str | None = None, ra: float | None = None, dec: float | None = None,
               fov_deg: float | None = None, pm: tuple | None = None, name: str | None = None,
               source: str | None = None) -> Field:
    """Resolve the position, list the images (SIA2) and fix epochs, centre and grid. No pixel download.

    pm = (pmra, pmdec, ref_epoch) for a free position (mas/yr, Julian year; e.g. a SIMBAD answer from
    /api/resolve): the target is then propagated like a named one (grid centred on its mid-data position,
    target_track per epoch)."""
    fov_deg = float(fov_deg or DEFAULT_FOV_DEG)
    if target is not None:
        t = TARGETS[target]
        tc = target_coord(target)
        fov_deg = float(fov_deg if fov_deg else t.get("fov_arcmin", 7.0) / 60)
        q = target_at(tc, QUERY_MJD)
        refs = find_images(q.ra.deg, q.dec.deg)
        key, name, tinfo = target, t["name"], t
        qra, qdec = float(t["ra"]), float(t["dec"])
    else:
        ra = float(ra) % 360.0
        dec = float(dec)
        if pm is not None:
            from astropy.time import Time
            tc = SkyCoord(ra=ra * u.deg, dec=dec * u.deg, pm_ra_cosdec=float(pm[0]) * u.mas / u.yr,
                          pm_dec=float(pm[1]) * u.mas / u.yr,
                          obstime=Time(float(pm[2]), format="jyear", scale="tdb"), frame="icrs")
            q = target_at(tc, QUERY_MJD)
            refs = find_images(q.ra.deg, q.dec.deg)
        else:
            tc = SkyCoord(ra * u.deg, dec * u.deg, frame="icrs")
            refs = find_images(ra, dec)
        key, tinfo = free_key(ra, dec, fov_deg, pm), None
        name = name or f"RA {ra:.4f}, Dec {dec:+.4f}"
        qra, qdec = ra, dec
    labels, epochs = access.epochs_for(refs) if refs else (np.zeros(0, int), [])
    moving = target is not None or pm is not None
    if moving and refs:
        center = target_at(tc, float(np.median([r.mjd for r in refs])))  # mover stays near the middle
    else:
        center = SkyCoord(qra * u.deg, qdec * u.deg, frame="icrs")
    grid = make_grid(center, fov_deg * 60, PIX_ARCSEC)
    f = Field(key, name, qra, qdec, fov_deg, tc, tinfo, refs, labels, epochs, center, grid,
              moving=moving, source=(tinfo or {}).get("source") or source)
    f.notes = list((tinfo or {}).get("notes", []))
    if target is not None:  # notes measured by the gallery build (precompute) when it exists
        gm = GALLERY_DIR / target / "manifest.json"
        try:
            f.notes = json.loads(gm.read_text(encoding="utf-8")).get("notes", f.notes)
        except (OSError, ValueError):
            pass
    f.extra_bands = list((tinfo or {}).get("extra_bands", []))
    if target is None and refs:
        notes, extra = _nearby_targets(f)
        f.notes += notes
        f.extra_bands += extra
    return f


def _nearby_targets(f: Field) -> tuple[list[str], list[dict]]:
    """A free field that contains a named target: say where the target really is (e.g. WISE 0855 has moved
    ~2' from the position the challenge page quotes) and offer its custom bands (WISE 0855: W)."""
    notes, extra = [], []
    mid = float(np.median([r.mjd for r in f.refs]))
    half = f.fov_deg / 2
    for key, t in TARGETS.items():
        pos = target_at(target_coord(key), mid)
        sep = pos.separation(f.center).deg
        if sep > 1.5 * half:
            continue
        x, y = f.grid.wcs.world_to_pixel(pos)
        inside = 0 <= x < f.grid.shape[1] and 0 <= y < f.grid.shape[0]
        where = "inside this field" if inside else "just outside this field"
        notes.append(f"{t['name']} is {where}: at mid-data ({pos.ra.deg:.5f}, {pos.dec.deg:+.5f}), "
                     f"{sep * 60:.2f}′ from the centre. Open ?target={key} for the field centred on it.")
        extra += t.get("extra_bands", [])
    return notes, extra


# --------------------------------------------------------------------------- manifest

def _n_exposures(f: Field, dets: list[int]) -> list[int]:
    out = []
    for e in f.epochs:
        out.append(int(sum(1 for r, l in zip(f.refs, f.labels) if l == e["id"] and r.detector in dets)))
    return out


def choose_default_band(counts: dict) -> str | None:
    """counts: {band: [per-epoch exposures]}. The band whose weakest epoch with data is strongest; a tie goes
    to the shorter wavelength (D1 < ... < D6)."""
    best, best_key = None, None
    for b, n in counts.items():
        used = [x for x in n if x and x > 0]
        if not used:
            continue
        k = (min(used), -int(b[1:]))
        if best_key is None or k > best_key:
            best, best_key = b, k
    return best


def manifest(f: Field, status: dict | None = None, n_exposures: dict | None = None,
             n_used: dict | None = None) -> dict:
    """status: {band_id: 'pending'|'ready'|'empty'}; n_exposures overrides the SIA2 count for a band
    (custom windows: exposures whose wavelength at the target falls inside, known after download)."""
    status = status or {}
    n_exposures = n_exposures or {}
    m = {"schema": SCHEMA, "key": f.key, "name": f.name,
         "query": {"ra": f.query_ra, "dec": f.query_dec, "fov_deg": f.fov_deg},
         "collections": list(access.COLLECTIONS),
         "max_fov_deg": MAX_FOV_DEG}
    note = index_note()
    if note:
        m["image_list"] = note
    if not f.refs:
        m.update({"data_range": None, "grid": f.grid.to_manifest(), "epochs": [], "bands": [],
                  "message": "No SPHEREx images cover this position in " + ", ".join(access.COLLECTIONS) + ".",
                  "processing": processing_text(), "acknowledgement": ACKNOWLEDGEMENT})
        return m
    mjd = [r.mjd for r in f.refs]
    m["data_range"] = {"mjd_min": float(min(r.t_min for r in f.refs)), "mjd_max": float(max(r.t_max for r in f.refs))}
    m["grid"] = f.grid.to_manifest()
    m["epochs"] = [{"id": e["id"], "date": e["date"], "mjd_mid": e["mjd_mid"], "mjd_min": e["mjd_min"],
                    "mjd_max": e["mjd_max"]} for e in f.epochs]
    bands = []
    for b in f.band_defs():
        nexp = n_exposures.get(b["id"]) or _n_exposures(f, f.detectors_for(b["id"]))
        st = status.get(b["id"], "pending")
        if sum(nexp) == 0:
            st = "empty"
        bands.append({**b, "n_exposures": nexp, "status": st})
    m["bands"] = bands
    notes = list(dict.fromkeys(list(f.notes) + _weak_epoch_notes(f, bands)))  # no duplicates
    if f.moving:
        m["target_track"] = [{"epoch": e["id"], "ra": p[0], "dec": p[1]}
                             for e in f.epochs for p in [_radec(target_at(f.target, e["mjd_mid"]))]]
        if f.source:
            m["target_source"] = f.source
    if f.tinfo and f.tinfo.get("default_band"):
        m["default_band"] = f.tinfo["default_band"]
    else:  # built bands: exposures used per epoch; before that: the SIA2 counts
        det = {b["id"]: (n_used or {}).get(b["id"]) or b["n_exposures"] for b in bands
               if b["id"].startswith("D") and b["id"][1:].isdigit() and b["status"] != "empty"}
        db = choose_default_band(det)
        if db:
            m["default_band"] = db
    if notes:
        m["notes"] = notes
    m["processing"] = processing_text()
    m["acknowledgement"] = ACKNOWLEDGEMENT
    m["n_images"] = len(f.refs)
    m["mjd_median"] = float(np.median(mjd))
    return m


def _weak_epoch_notes(f: Field, bands: list[dict]) -> list[str]:
    """Say so when one epoch has far fewer exposures than the others (its frames are noisier)."""
    det = [b for b in bands if b["id"].startswith("D") and b["id"][1:].isdigit()]
    if len(f.epochs) < 2 or not det:
        return []
    per_epoch = np.array([[b["n_exposures"][i] for b in det] for i in range(len(f.epochs))])  # epoch x band
    tot = per_epoch.sum(axis=1)
    notes = []
    for i, e in enumerate(f.epochs):
        others = np.delete(tot, i)
        if tot[i] > 0 and tot[i] < 0.5 * np.median(others):
            lo, hi = int(per_epoch[i].min()), int(per_epoch[i].max())
            olo, ohi = int(np.delete(per_epoch, i, axis=0).min()), int(np.delete(per_epoch, i, axis=0).max())
            notes.append(f"Epoch {e['id']} ({e['date']}) has only {lo}–{hi} exposures per band "
                         f"(the other epochs {olo}–{ohi}), so its frames are noisier.")
    return notes


def _radec(c: SkyCoord) -> tuple[float, float]:
    return float(c.ra.deg), float(c.dec.deg)


# --------------------------------------------------------------------------- bands

def band_refs(f: Field, band_id: str, cap: int = MAX_PER_EPOCH_BAND) -> list:
    dets = f.detectors_for(band_id)
    idx = [i for i, r in enumerate(f.refs) if r.detector in dets]
    sub = [f.refs[i] for i in idx]
    return access.select_refs(sub, cap, labels=f.labels[idx]) if sub else []


def fetch_band_cutouts(f: Field, band_id: str, layers=("IMAGE", "FLAGS", "VARIANCE"), cap: int = MAX_PER_EPOCH_BAND,
                       progress=None, stats: dict | None = None, workers: int = access.S3_MAX_CONCURRENCY,
                       box: bool = False):
    """box=True: cutouts are the detector bounding box of the grid (fewer rows, used by the live server);
    False: squares sized for any rotation (half_px_for), which is what the gallery cache holds."""
    refs = band_refs(f, band_id, cap)
    if not refs:
        return [], 0
    box_deg = f.grid.shape[0] * f.grid.pix_arcsec / 3600 if box else None
    return access.fetch_cutouts(refs, f.center.ra.deg, f.center.dec.deg, half_px=f.half_px, layers=layers,
                                progress=progress, stats=stats, workers=workers, box_deg=box_deg)


BUILD_PARAMS = {"pix_arcsec": PIX_ARCSEC, "min_exposures": MIN_EXPOSURES, "max_exposures": MAX_PER_EPOCH_BAND,
                "per_detector": True, "epoch_rule": "split_epochs(gap_days=30, max_epoch_days=60) on all exposures"}


def frames_for_band(f: Field, band_id: str, cuts):
    kw = dict(fov_arcmin=f.fov_arcmin, pix_arcsec=PIX_ARCSEC, epochs=f.epochs, center=f.center,
              max_exposures=MAX_PER_EPOCH_BAND, min_exposures=MIN_EXPOSURES)
    b = f.band_def(band_id)
    if band_id.startswith("D") and band_id[1:].isdigit():
        frames = build_frames([c for c in cuts if c.detector == int(band_id[1:])], f.target, per_detector=True, **kw)
    else:
        frames = build_frames(cuts, f.target, band_um=tuple(b["wave_um"]), band_id=band_id, **kw)
    return [fr for fr in frames if fr.band == band_id]


def band_n_exposures(f: Field, band_id: str, cuts) -> list[int] | None:
    """For a custom window: exposures (before masking) whose wavelength at the target falls inside, per epoch."""
    if band_id.startswith("D") and band_id[1:].isdigit():
        return None
    lo, hi = f.band_def(band_id)["wave_um"]
    from spx.coadd import assign_epochs
    lab = assign_epochs([c.mjd for c in cuts], f.epochs) if cuts else np.zeros(0, int)
    return [int(sum(1 for c, l in zip(cuts, lab) if l == e["id"] and lo <= c.wavelength_um <= hi)) for e in f.epochs]


def build_band(f: Field, band_id: str, out_dir: Path, layers=("IMAGE", "FLAGS", "VARIANCE"), cuts=None,
               progress=None, stats: dict | None = None):
    """Fetch (or reuse `cuts`), co-add, write frame files and {band}.json into out_dir.

    Returns (band_json, cutouts, frames). band_json lists every epoch; an epoch without a frame has n_used 0
    and no files.
    """
    t0 = time.time()
    if cuts is None:
        cuts, _ = fetch_band_cutouts(f, band_id, layers, progress=progress, stats=stats)
    t1 = time.time()
    frames = frames_for_band(f, band_id, cuts) if cuts else []
    t2 = time.time()
    out_dir = Path(out_dir)
    by_epoch = {fr.epoch: fr for fr in frames}
    for fr in frames:
        stem = f"{fr.epoch}_{band_id}"
        write_f32(out_dir / f"{stem}.f32", fr.image)
        write_f32(out_dir / f"{stem}_weight.f32", fr.weight)
        write_f32(out_dir / f"{stem}_flagfrac.f32", fr.flag_frac)
    bj = {"schema": SCHEMA, "key": f.key, "band": band_id, "shape": list(f.grid.shape)}
    if frames:
        vmin, vmax, soft = common_scale(frames)[band_id]
        bj["scale"] = {"vmin": vmin, "vmax": vmax, "soft": soft}
    bj["epochs"] = []
    for e in f.epochs:
        fr = by_epoch.get(e["id"])
        if fr is None:
            bj["epochs"].append({"id": e["id"], "n_used": 0, "noise": None, "mjd_mid": e["mjd_mid"]})
        else:
            bj["epochs"].append({"id": e["id"], "n_used": fr.n_used, "noise": fr.noise, "mjd_mid": fr.mjd,
                                 "date": fr.epoch_label, "band_um": list(fr.band_um),
                                 "target_xy": list(fr.target_xy),
                                 "quality": "low" if fr.n_used < MIN_GOOD_EXPOSURES else "ok"})
    b = f.band_def(band_id)
    if b is not None and b.get("window"):
        bj.update({"label": b["label"], "wave_um": b["wave_um"], "n_exposures": band_n_exposures(f, band_id, cuts),
                   "note": WINDOW_NOTE})
    write_json(out_dir / f"{band_id}.json", bj)
    if stats is not None:
        stats.update({"t_fetch": t1 - t0, "t_coadd": t2 - t1, "n_frames": len(frames)})
    return bj, cuts, frames


# --------------------------------------------------------------------------- automatic candidates

def detect_module():
    """spx.detect, or None when it is not installed."""
    try:
        from spx import detect
    except ImportError:
        return None
    return detect if hasattr(detect, "find_candidates") and hasattr(detect, "load_field") else None


def make_candidates(field_dir: Path, key: str) -> dict:
    """candidates.json content for a built field folder (server cache or web/data/<key>, same layout; gaia.json must
    be there): spx.detect.find_candidates(spx.detect.load_field(dir)) plus schema and key. Raises RuntimeError
    when spx.detect is missing."""
    det = detect_module()
    if det is None:
        raise RuntimeError("spx.detect.find_candidates is not available")
    out = det.find_candidates(det.load_field(Path(field_dir)))
    return {"schema": SCHEMA, "key": key, **out}


# --------------------------------------------------------------------------- gaia, spectrum

GAIA_TAP = "https://gea.esac.esa.int/tap-server/tap/sync"
GAIA_TABLE = "gaiadr3.gaia_source"
GAIA_GMAX = 19.0
#: Seconds one Gaia query may take in all, retries included (the ESA archive reset connections after 16-20 s on
#: 15 Nov; the old 3 x 60 s could hold a thread for three minutes). SPX_GAIA_BUDGET_S overrides.
GAIA_BUDGET_S = float(os.environ.get("SPX_GAIA_BUDGET_S") or 15.0)


def gaia_sources(f: Field, gmax: float = GAIA_GMAX, max_age_days: float = 30.0) -> list[dict]:
    """Gaia DR3 sources around the field (ra/dec at epoch 2016.0; the page propagates them per epoch).

    ESA Gaia archive TAP (sync, CSV), cached on disk per field centre and radius.
    """
    # half-diagonal + room for ~10 years of 10"/yr motion
    radius = f.fov_deg / 2 * math.sqrt(2) + 0.03
    ra, dec = float(f.center.ra.deg), float(f.center.dec.deg)
    q = (f"SELECT source_id, ra, dec, pmra, pmdec, phot_g_mean_mag FROM {GAIA_TABLE} WHERE "
         f"1=CONTAINS(POINT('ICRS', ra, dec), CIRCLE('ICRS', {ra:.7f}, {dec:.7f}, {radius:.5f})) "
         f"AND phot_g_mean_mag < {gmax:g}")
    return _gaia_query(q, f"{ra:.5f}_{dec:+.5f}_{radius:.4f}_{gmax:g}", max_age_days)


def gaia_path(points, margin_arcsec: float = 60.0, gmax: float = GAIA_GMAX, max_age_days: float = 30.0):
    """Gaia DR3 sources within margin of a sky path given as [(ra, dec)] (one box query, then a distance cut)."""
    ra = np.array([p[0] for p in points], float) % 360.0
    dec = np.array([p[1] for p in points], float)
    m = margin_arcsec / 3600.0
    d0, d1 = max(-90.0, dec.min() - m), min(90.0, dec.max() + m)
    cosd = max(0.01, math.cos(math.radians(min(89.0, max(abs(d0), abs(d1))))))
    span = (ra.max() - ra.min()) % 360.0
    if span > 180:  # the path crosses RA 0
        r = (ra + 180.0) % 360.0 - 180.0
        r0, r1 = r.min() - m / cosd, r.max() + m / cosd
        cond = f"(ra >= {r0 % 360.0:.6f} OR ra <= {r1:.6f})"
    else:
        r0, r1 = ra.min() - m / cosd, ra.max() + m / cosd
        cond = f"ra BETWEEN {r0:.6f} AND {r1:.6f}"
    q = (f"SELECT source_id, ra, dec, pmra, pmdec, phot_g_mean_mag FROM {GAIA_TABLE} WHERE "
         f"{cond} AND dec BETWEEN {d0:.6f} AND {d1:.6f} AND phot_g_mean_mag < {gmax:g}")
    key = hashlib_short(q)
    rows = _gaia_query(q, f"path_{key}", max_age_days)
    if not rows:
        return []
    pc = SkyCoord(ra * u.deg, dec * u.deg)
    gc = SkyCoord([g["ra"] for g in rows] * u.deg, [g["dec"] for g in rows] * u.deg)
    _, sep, _ = gc.match_to_catalog_sky(pc)
    return [g for g, s in zip(rows, sep.arcsec) if s <= margin_arcsec]


def hashlib_short(s: str) -> str:
    import hashlib
    return hashlib.sha1(s.encode()).hexdigest()[:12]


def _gaia_query(q: str, cache_name: str, max_age_days: float = 30.0, budget_s: float | None = None) -> list[dict]:
    """ESA Gaia archive TAP (sync, CSV) with a disk cache; retries within budget_s seconds in all."""
    import csv
    import io
    import requests
    path = access.CACHE_DIR / "gaia" / f"{cache_name}.json"
    if path.exists() and (time.time() - path.stat().st_mtime) / 86400 <= max_age_days:
        return json.loads(path.read_text(encoding="utf-8"))
    last = None
    budget = GAIA_BUDGET_S if budget_s is None else float(budget_s)
    t0 = time.monotonic()
    for attempt in range(10):
        left = budget - (time.monotonic() - t0)
        if attempt:
            left -= 1.0
            if left < 2.0:
                break
            time.sleep(1.0)
        try:
            r = requests.get(GAIA_TAP, params={"REQUEST": "doQuery", "LANG": "ADQL", "FORMAT": "csv", "QUERY": q},
                             timeout=(min(5.0, left), max(1.0, left)))
        except (requests.ConnectionError, requests.Timeout) as e:
            last = e
            continue
        if r.status_code != 200 or not r.text.startswith("source_id"):
            last = RuntimeError(f"Gaia TAP HTTP {r.status_code}: {r.text[:200]}")
            continue
        out = []
        for row in csv.DictReader(io.StringIO(r.text)):
            def val(k):
                v = row.get(k, "")
                return float(v) if v not in ("", None) else None
            out.append({"ra": val("ra"), "dec": val("dec"), "pmra": val("pmra") or 0.0, "pmdec": val("pmdec") or 0.0,
                        "g_mag": val("phot_g_mean_mag"), "ref_epoch": 2016.0, "source_id": row["source_id"]})
        out.sort(key=lambda s: s["g_mag"] if s["g_mag"] is not None else 99)
        access._atomic_write_bytes(path, json.dumps(out).encode())
        return out
    raise RuntimeError(f"Gaia TAP unreachable in {time.monotonic() - t0:.0f} s: {last!r}")


SELF_MATCH_ARCSEC = 3.0


def gaia_neighbors(coord: SkyCoord, gaia: list[dict] | None, self_match_arcsec: float = SELF_MATCH_ARCSEC):
    """Gaia rows as a SkyCoord with proper motion (epoch 2016.0) for spectrum_points(neighbors=...).

    The source at the photometry position itself (closer than self_match_arcsec at 2016.0, e.g. Barnard's
    Star) is left out: it is the object being measured, not a neighbour. Returns (SkyCoord | None, rows).
    """
    from astropy.time import Time
    rows = [g for g in (gaia or []) if g.get("ra") is not None and g.get("dec") is not None]
    if not rows:
        return None, []
    ref = Time(2016.0, format="jyear", scale="tdb")
    c = SkyCoord(ra=[g["ra"] for g in rows] * u.deg, dec=[g["dec"] for g in rows] * u.deg,
                 pm_ra_cosdec=[g.get("pmra") or 0.0 for g in rows] * u.mas / u.yr,
                 pm_dec=[g.get("pmdec") or 0.0 for g in rows] * u.mas / u.yr, obstime=ref, frame="icrs")
    here = target_at(coord, float(ref.mjd))
    keep = SkyCoord(c.ra, c.dec, frame="icrs").separation(here).arcsec > self_match_arcsec
    if not keep.any():
        return None, []
    return c[keep], [r for r, k in zip(rows, keep) if k]


#: The wing validation (handoff/results/wing_validation_v2.json): summary.controls_p90 = 90th percentile of the wing-series epoch ratio on
#: unsaturated control stars; a target ratio above it is significant. Read from the file, never typed in here.
WING_VALIDATION = Path(__file__).resolve().parent.parent / "handoff" / "results" / "wing_validation_v2.json"
GALLERY_DIR = Path(os.environ.get("SPX_GALLERY") or Path(__file__).resolve().parent.parent / "web" / "data")


def wing_threshold() -> float | None:
    try:
        return float(json.loads(WING_VALIDATION.read_text(encoding="utf-8"))["summary"]["controls_p90"])
    except (OSError, KeyError, ValueError, TypeError) as e:
        log.warning("wing threshold unavailable (%s): %r", WING_VALIDATION, e)
        return None


def wing_bands(points) -> dict | None:
    """meta.wing_bands (contract): per band with wing points, {ratio, threshold, significant}."""
    from spx import photometry as ph
    thr = wing_threshold()
    if thr is None:
        return None
    v = ph.variability(ph.binned_series(points, kind="wing"))
    return {b: {"ratio": d["ratio"], "threshold": thr, "significant": bool(d["ratio"] > thr)} for b, d in v.items()}


def variability_report(f: Field, cuts, points, gaia, kind: str = "flux") -> dict:
    """The variability metric (spx.photometry) on the PUBLISHED spectrum points, with control stars measured the
    same way in the same cutouts (spx.photometry select_controls / binned_series / variability / is_variable).
    Single source for the page notes and scripts/acceptance.py."""
    from astropy.time import Time
    from spx import photometry as ph
    tvar = ph.variability(ph.binned_series(points, kind=kind))
    t = f.target
    tc = SkyCoord(t.ra, t.dec, frame="icrs")
    close = [g for g in gaia if g.get("g_mag") is not None
             and SkyCoord(g["ra"] * u.deg, g["dec"] * u.deg).separation(tc).arcsec < 3]
    tg = min((g["g_mag"] for g in close), default=None)  # brightest within 3" (RW Aur: A, not the 1.5" B)
    saturated = sum(bool(p.get("saturated")) for p in points) / max(1, len(points)) > 0.5
    ctrl = ph.select_controls(gaia, (float(tc.ra.deg), float(tc.dec.deg)), tg, saturated=saturated)
    cvar = {}
    for g in ctrl:
        cc = SkyCoord(ra=g["ra"] * u.deg, dec=g["dec"] * u.deg, pm_ra_cosdec=(g.get("pmra") or 0) * u.mas / u.yr,
                      pm_dec=(g.get("pmdec") or 0) * u.mas / u.yr, obstime=Time(2016.0, format="jyear"))
        pts = ph.spectrum_points(cuts, cc, epochs=f.epochs, wings=(kind == "wing"))["points"]
        for b, d in ph.variability(ph.binned_series(pts, kind=kind)).items():
            cvar.setdefault(b, []).append(d["ratio"])
    per = {}
    for b, d in tvar.items():
        cs = cvar.get(b, [])
        per[b] = {"ratio": d["ratio"], "n_bins": d["n_bins"], "n_controls": len(cs),
                  "controls_max": round(max(cs), 3) if cs else None,
                  "controls_min": round(min(cs), 3) if cs else None, "variable": ph.is_variable(d["ratio"], cs)}
    cg = [g["g_mag"] for g in ctrl if g.get("g_mag") is not None]
    return {"kind": kind, "target_g": tg, "saturated": saturated, "n_controls": len(ctrl),
            "control_g": [round(min(cg), 1), round(max(cg), 1)] if cg else None, "per_band": per}


#: Wording thresholds for the measured notes (how a variable band is described, not whether it is variable).
SMALL_MARGIN = 1.20        # variable, but target / largest control below this: "exceeds its controls by a small margin"
CLEARLY_ABOVE = 1.10       # wing ratio above threshold x this: "clearly above"; else "just above"
WISE_DETECTION = Path(__file__).resolve().parent.parent / "handoff" / "results" / "wise0855_detection_v2.json"


def measured_notes(f: Field, cuts, spec: dict, gaia) -> list[str]:
    """Notes whose numbers are measured from this build's data (gallery targets only)."""
    key = f.key
    notes = []
    fmt = lambda x: f"×{x:.2f}"  # noqa: E731
    if key == "rw-aur":
        rep = variability_report(f, cuts, spec["points"], gaia, "flux")
        per = rep["per_band"]
        var = [b for b in sorted(per) if per[b]["variable"]]
        if var:
            single = [b for b in var if per[b]["n_bins"] == 1]
            small = [b for b in var if b not in single and per[b]["controls_max"]
                     and per[b]["ratio"] / per[b]["controls_max"] < SMALL_MARGIN]
            robust = [b for b in var if b not in single and b not in small]
            detail = ", ".join(f"{b} {fmt(per[b]['ratio'])} (controls up to {fmt(per[b]['controls_max'])}, "
                               f"{per[b]['n_controls']} stars)" for b in var)
            s = (f"By the control-star test RW Aurigae changes between epochs in {len(var)} of {len(per)} bands: "
                 f"{detail}.")
            parts = []
            if robust:
                parts.append(f"most robust in {', '.join(robust)} (several wavelength bins)")
            if single:
                parts.append(f"{', '.join(single)} rest{'s' if len(single) == 1 else ''} on a single wavelength bin")
            if small:
                parts.append(f"{', '.join(small)} exceed{'s' if len(small) == 1 else ''} its controls by a small margin")
            tail = "; ".join(parts)
            notes.append(s + (" " + tail[0].upper() + tail[1:] + "." if parts else ""))
        else:
            notes.append("By the control-star test no band of RW Aurigae is variable in this data.")
    if key == "r-dra":
        pts = spec["points"]
        n_sat = sum(bool(p.get("saturated")) for p in pts)
        notes.append(f"R Draconis is saturated in {n_sat} of {len(pts)} exposures, so its own flux cannot be "
                     "measured; its change shows in the brightness of the halo (common scale across epochs) and in "
                     "the PSF-wing series.")
        wb = spec.get("meta", {}).get("wing_bands") or {}
        if wb:
            thr = next(iter(wb.values()))["threshold"]
            above = [b for b, d in sorted(wb.items()) if d["ratio"] > thr]
            clear = [b for b in above if wb[b]["ratio"] > CLEARLY_ABOVE * thr]
            just = [b for b in above if b not in clear]
            no = [b for b, d in sorted(wb.items()) if d["ratio"] <= thr]
            lst = lambda bs: " and ".join(f"{b} {fmt(wb[b]['ratio'])}" for b in bs)  # noqa: E731
            seg = []
            if clear:
                seg.append(f"{lst(clear)} exceed{'s' if len(clear) == 1 else ''} it")
            if just:
                seg.append(f"{lst(just)} {'is' if len(just) == 1 else 'are'} just above it")
            if no:
                seg.append(f"{lst(no)} {'does' if len(no) == 1 else 'do'} not exceed it")
            rep = variability_report(f, cuts, pts, gaia, "wing")
            cr = [d[k] for d in rep["per_band"].values() for k in ("controls_min", "controls_max")
                  if d.get(k) is not None]
            s = (f"PSF-wing brightness between epochs is indicative, not a formal detection. Against the scatter of "
                 f"bright control stars in other fields (×{thr:.2f}, 90th percentile): " + "; ".join(seg) + ".")
            if rep["n_controls"] and cr:
                g = rep["control_g"]
                s += (f" The {rep['n_controls']} fainter control stars in this field"
                      + (f" (G {g[0]:.1f}–{g[1]:.1f})" if g else "")
                      + f" vary more in the same measurement (×{min(cr):.2f}–{max(cr):.2f}), so the formal "
                        "variability test is undetermined.")
            notes.append(s)
    if key == "wise-0855" and WISE_DETECTION.exists():
        det = json.loads(WISE_DETECTION.read_text(encoding="utf-8"))
        seps = [e.get("sep_to_blend_star_arcsec") for e in det.get("epochs", [])]
        if seps and all(x is not None for x in seps):
            notes.append("Measured separation between WISE 0855 and that star (two-source PSF fit): "
                         + " → ".join(f"{x:.1f}″" for x in seps) + " over the three epochs.")
    return notes


def wings_for(f: Field, ra: float | None) -> bool:
    """PSF-wing flux series: only for a gallery target that asks for it (TARGETS[...]["wings"]), and only at
    the target itself; never for a clicked position or a free field."""
    return ra is None and bool((f.tinfo or {}).get("wings", False))


def spectrum(f: Field, cuts, ra: float | None = None, dec: float | None = None, gaia: list[dict] | None = None) -> dict:
    """spx.photometry.spectrum_points at the target (propagated per exposure) or at a fixed (ra, dec),
    with the Gaia sources around it passed as neighbours (deblended PSF photometry). Returned as is."""
    from spx.photometry import spectrum_points
    coord = f.target if ra is None else SkyCoord(float(ra) * u.deg, float(dec) * u.deg, frame="icrs")
    nb, info = gaia_neighbors(coord, gaia)
    wings = wings_for(f, ra)
    out = spectrum_points(cuts, coord, epochs=f.epochs, neighbors=nb, neighbor_info=info or None, wings=wings)
    if wings:
        wb = wing_bands(out["points"])
        if wb is not None:
            out["meta"]["wing_bands"] = wb
    return out


# --------------------------------------------------------------------------- single-exposure track (MPC drafts)

TRACK_HALF_PX = 20          # spx.astrometry needs >= 15 px; 20 leaves room for its calibration positions
TRACK_SIA_STEP_DEG = 0.5    # SIA2 sample spacing along the predicted path (a detector is ~3.5 deg wide)


def track_detectors(band: str) -> list[int]:
    if band.startswith("D") and band[1:].isdigit() and int(band[1:]) in access.DETECTOR_BANDS_UM:
        return [int(band[1:])]
    raise ValueError(f"band must be one of D1..D6 for a track, got {band!r}")


# --------------------------------------------------------------------------- image lists without IRSA

#: Our own index of the public bucket (server/s3index.py): server/index/*.npz, or SPX_INDEX (a file or folder;
#: "off" = not used). Absent: image lists come from IRSA SIA2 as before.
INDEX_PATH = os.environ.get("SPX_INDEX") or str(Path(__file__).resolve().parent / "index")
_index_state: dict = {"loaded": False, "indexes": []}


def s3_indexes() -> list:
    """The loaded indexes (one per file: dr1 and qr3 may be separate); [] when there is none."""
    if not _index_state["loaded"]:
        _index_state["loaded"] = True
        if INDEX_PATH.lower() != "off":
            from server.s3index import S3Index
            p = Path(INDEX_PATH)
            files = sorted(p.glob("*.npz")) if p.is_dir() else ([p] if p.exists() else [])
            for f in files:
                try:
                    _index_state["indexes"].append(S3Index.load(f))
                    log.info("image index %s: %d images", f.name, _index_state["indexes"][-1].n)
                except Exception as e:
                    log.warning("image index %s not loaded: %r", f, e)
    return _index_state["indexes"]


def index_note() -> str | None:
    """The manifest's sentence about where image lists come from, when the index is in use."""
    idx = s3_indexes()
    if not idx:
        return None
    n = sum(i.n for i in idx)
    built = min(i.meta["built_utc"] for i in idx)[:10]
    mjd = max(i.meta["mjd_max"] for i in idx)
    from astropy.time import Time
    last = Time(mjd, format="mjd").isot[:10]
    return (f"image list: our own index of the public SPHEREx bucket (built {built}, {n:,} images up to {last}), "
            "not IRSA SIA2; a list IRSA gave earlier is used where it is still cached")


def _fresh(path: Path) -> bool:
    if not path.exists():
        return False
    age = (time.time() - path.stat().st_mtime) / 86400
    rows = json.loads(path.read_text(encoding="utf-8")).get("rows")
    return age <= (access.SIA_MAX_AGE_DAYS if rows else access.SIA_EMPTY_MAX_AGE_DAYS)


def find_images(ra: float, dec: float):
    """spx.access.find_images, fed from our index when there is one: for each collection the index covers, a list
    still cached (the gallery seed, or an earlier IRSA answer) is kept; otherwise the index's list is written into
    the same cache file, so spx reads it and asks IRSA nothing. Exposures newer than the index are asked of IRSA
    in the background (index_late_refs); if IRSA does not answer, nothing waits for it."""
    idxs = s3_indexes()
    if idxs:
        ra_n, dec_n = float(ra) % 360.0, float(dec)
        for coll in access.COLLECTIONS:
            path = access._sia_cache_path(ra_n, dec_n, coll, access.SIA_RADIUS_DEG)
            mine = [i for i in idxs if i.covers(coll)]
            if not mine or _fresh(path):
                continue
            rows = [r for i in mine for r in i.rows_at(ra_n, dec_n, coll)]
            access._atomic_write_bytes(path, json.dumps({"ra": ra_n, "dec": dec_n, "collection": coll,
                                                         "radius_deg": access.SIA_RADIUS_DEG, "queried": time.time(),
                                                         "source": "s3index", "rows": rows}).encode())
            _later_from_irsa(ra_n, dec_n, coll, path, max(i.meta["mjd_max"] for i in mine))
        # collection by collection: one the index does not cover is asked of IRSA and skipped if IRSA fails
        out = {}
        for coll in access.COLLECTIONS:
            try:
                refs = access.find_images(ra, dec, collections=[coll])
            except access.SiaError as e:
                if any(i.covers(coll) for i in idxs):
                    raise
                log.info("%s not in the index and IRSA did not answer (%r): left out", coll, e)
                continue
            for r in refs:
                out.setdefault((r.obs_id, r.detector), r)     # the earlier collection wins, as in spx
        return sorted(out.values(), key=lambda r: (r.t_min, r.detector, r.obs_id))
    return access.find_images(ra, dec)


LATE_SIA_BUDGET_S = 20.0


def _later_from_irsa(ra: float, dec: float, coll: str, path: Path, mjd_max: float) -> None:
    """In the background: exposures after the index's last one, from IRSA; merged into the cached list. Silent when
    IRSA does not answer (the list from the index stays)."""
    import threading

    def run():
        try:
            rows = access._sia_query(ra, dec, coll, access.SIA_RADIUS_DEG, budget_s=LATE_SIA_BUDGET_S)
        except Exception as e:
            log.info("later exposures for %.4f %+.4f %s not fetched: %r", ra, dec, coll, e)
            return
        late = [r for r in rows if r["t_min"] > mjd_max]
        if not late:
            return
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
            have = {(r["obs_id"], r["detector"]) for r in d["rows"]}
            d["rows"] += [r for r in late if (r["obs_id"], r["detector"]) not in have]
            d["source"] = "s3index + IRSA SIA2 (later exposures)"
            access._atomic_write_bytes(path, json.dumps(d).encode())
        except Exception as e:
            log.warning("merging later exposures into %s failed: %r", path, e)
    threading.Thread(target=run, daemon=True, name="sia-late").start()


_gallery_field_cache: dict = {}


def gallery_images_at(ra: float, dec: float):
    """The image list of a gallery target whose field contains (ra, dec), else None. The field's footprint is read
    from web/data/<key>/manifest.json and its list from the cache (seeded from web/data/_sia), so a point inside a
    gallery field needs no IRSA query. Every image of that list covers the field centre; a SPHEREx detector is
    ~3.5 deg wide, so it covers a point a few arcminutes away as well."""
    for key in TARGETS:
        mp = GALLERY_DIR / key / "manifest.json"
        if not mp.exists():
            continue
        g = json.loads(mp.read_text(encoding="utf-8"))["grid"]
        half = max(g["shape"]) * abs(g["cdelt"][1]) / 2 * math.sqrt(2)
        c0 = SkyCoord(g["crval"][0] * u.deg, g["crval"][1] * u.deg)
        if c0.separation(SkyCoord(float(ra) * u.deg, float(dec) * u.deg)).deg > half:
            continue
        if key not in _gallery_field_cache:
            _gallery_field_cache[key] = open_field(target=key).refs
        return _gallery_field_cache[key]
    return None


def track_refs(marks, band: str):
    """Exposures of `band` inside the model's time window along the predicted path: SIA2 at points along the path
    (a fast object leaves any fixed field), union of the lists, cut to the window. Points inside a gallery field use
    that field's list (gallery_images_at), without IRSA. Returns (refs, position, info)."""
    from spx.astrometry import track_model
    position, info = track_model(marks)
    lo, hi = info["window_mjd"]
    if not (np.isfinite(lo) and np.isfinite(hi)):  # between_epochs: the marks' own span
        ms = [m["mjd"] for m in marks]
        lo, hi = min(ms) - 1.0, max(ms) + 1.0
        query_window = (-np.inf, np.inf)
    else:
        query_window = (lo, hi)
    start, end = position(lo), position(hi)
    n = max(2, int(math.ceil(start.separation(end).deg / TRACK_SIA_STEP_DEG)) + 1)
    dets = set(track_detectors(band))
    refs, seen = [], set()
    for t in np.linspace(lo, hi, n):
        c = position(float(t))
        here = gallery_images_at(float(c.ra.deg), float(c.dec.deg))
        for r in (here if here is not None else find_images(float(c.ra.deg), float(c.dec.deg))):
            k = (r.obs_id, r.detector)
            if k in seen or r.detector not in dets:
                continue
            if not (query_window[0] <= r.mjd <= query_window[1]):
                continue
            seen.add(k)
            refs.append(r)
    refs.sort(key=lambda r: r.mjd)
    return refs, position, info


def track_cutouts(refs, position, layers=("IMAGE", "FLAGS", "VARIANCE"), workers: int = 16):
    """One cutout per exposure, centred on the object's predicted position at that exposure's time."""
    from concurrent.futures import ThreadPoolExecutor

    def one(ref):
        c = position(ref.mjd)
        cuts, _ = access.fetch_cutouts([ref], float(c.ra.deg), float(c.dec.deg), half_px=TRACK_HALF_PX,
                                       layers=layers, workers=1)
        return cuts[0] if cuts else None
    with ThreadPoolExecutor(max_workers=max(1, min(workers, access.S3_MAX_CONCURRENCY))) as ex:
        cuts = [c for c in ex.map(one, refs) if c is not None]
    cuts.sort(key=lambda c: c.mjd)
    return cuts


def measure_marks(marks, band: str):
    """Marks -> per-exposure measurements (spx.astrometry.measure_track) with Gaia neighbours along the path.
    Returns (rows, info, n_refs)."""
    from astropy.time import Time
    from spx.astrometry import measure_track
    refs, position, info = track_refs(marks, band)
    cuts = track_cutouts(refs, position)
    path = [(float(position(c.mjd).ra.deg), float(position(c.mjd).dec.deg)) for c in cuts] or \
        [(m["ra"], m["dec"]) for m in marks]
    gaia_note = "Gaia DR3 stars along the path were fitted as fixed neighbours"
    try:
        gaia = gaia_path(path)
    except Exception as e:  # positions still come out, only without fixed neighbours
        log.warning("gaia along track failed: %r", e)
        gaia = []
        gaia_note = ("not available: the Gaia archive did not answer, so neighbouring stars were NOT fitted; a "
                     "blended position can be off")
    nb = None
    if gaia:
        nb = SkyCoord(ra=[g["ra"] for g in gaia] * u.deg, dec=[g["dec"] for g in gaia] * u.deg,
                      pm_ra_cosdec=[g.get("pmra") or 0.0 for g in gaia] * u.mas / u.yr,
                      pm_dec=[g.get("pmdec") or 0.0 for g in gaia] * u.mas / u.yr,
                      obstime=Time(2016.0, format="jyear", scale="tdb"), frame="icrs")
    rows, info2 = measure_track(cuts, marks, neighbors=nb)
    return rows, {**info, **info2, "gaia": gaia_note}, len(refs)
