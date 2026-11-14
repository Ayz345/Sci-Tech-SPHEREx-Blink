"""Acceptance checks, data side (PLAN.md section 9). One command, item by item PASS / FAIL with the measured number.

    python scripts/acceptance.py                        # static gallery + running API at http://127.0.0.1:8000
    python scripts/acceptance.py --static-only          # web/data only (no network)
    python scripts/acceptance.py --profile deployed --api https://<host>

Status per item: PASS, FAIL, PENDING (an input from another session is missing; counts as not passed) and
INFO (measured, no threshold in this profile). Thresholds are fixed below and are never relaxed to make an
item pass. The report is printed and written as JSON (--out, default cache/acceptance/acceptance_<utc>.json).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import random
import socket
import subprocess
import sys
import time
import warnings
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from astropy.coordinates import SkyCoord  # noqa: E402
from astropy.wcs import WCS  # noqa: E402
import astropy.units as u  # noqa: E402

from spx.targets import TARGETS  # noqa: E402

# --------------------------------------------------------------------------- thresholds (do not relax)

MIN_EPOCHS = 3
TRACK_MARGIN_PX = 5                  # target track at least this far inside the grid
DETECT_SNR = 5.0                     # "a source is visible" at the track, every epoch
POS_TOL_PX = 2.0                     # centroid within 2 grid px (6") of the track, every epoch
MOTION_REL_TOL = 0.20                # |measured - expected motion vector| <= max(20 %, 1.5"/yr)
MOTION_ABS_TOL = 1.5                 # arcsec / yr
STATIC_MAX = 1.5                     # arcsec / yr: "no moving component"
DEBLENDED_MAX_SIGMA = 2.0            # deblended motion vs literature, per component, in scaled errors
CORE_RADIUS_ARCSEC = 36.0            # region around the track checked for holes
CORE_PX = 2.0                        # "the core": within 2 grid px of the track
SATURATED_CORE = {"barnard": "inf", "r-dra": "inf", "rw-aur": "finite"}   # expected core per target
CORE_LINK_ARCSEC = 12.0              # a +Inf group is a star core if it comes this close to the track ...
CORE_LINK_GMAX = 12.0                # ... or to a Gaia source brighter than this G (propagated to the epoch)
SAT_FRACTION_MAX = 0.15              # a non-finite "finite" core is allowed only where > 15 % of exposures saturate
SATURATION_CHECK = ROOT / "handoff" / "results" / "saturation_check_v2.json"
PROFILES = {  # seconds
    "local": {"manifest": 5.0, "first_band": 30.0, "cached": 3.0, "six_bands": None},
    "deployed": {"manifest": 5.0, "first_band": 20.0, "cached": 3.0, "six_bands": 90.0},
}

PASS, FAIL, PENDING, INFO = "PASS", "FAIL", "PENDING", "INFO"


class Report:
    def __init__(self):
        self.items = []

    def add(self, section, item, status, measured=None, threshold=None, detail=""):
        self.items.append({"section": section, "item": item, "status": status, "measured": measured,
                           "threshold": threshold, "detail": detail})
        mark = {"PASS": "ok ", "FAIL": "XX ", "PENDING": "?? ", "INFO": "-- "}[status]
        m = "" if measured is None else f"  measured={measured}"
        t = "" if threshold is None else f"  threshold={threshold}"
        print(f"  {mark}{status:7s} [{section}] {item}{m}{t}{('  | ' + detail) if detail else ''}", flush=True)

    def check(self, section, item, ok, measured=None, threshold=None, detail=""):
        self.add(section, item, PASS if ok else FAIL, measured, threshold, detail)


def strict_json(path_or_bytes):
    b = path_or_bytes.read_bytes() if isinstance(path_or_bytes, Path) else path_or_bytes

    def bad(c):
        raise ValueError(f"non-finite JSON number {c}")
    return json.loads(b, parse_constant=bad)


# --------------------------------------------------------------------------- image measurements

def grid_wcs(grid) -> WCS:
    w = WCS(naxis=2)
    w.wcs.ctype = grid["ctype"]
    w.wcs.crval = grid["crval"]
    w.wcs.crpix = grid["crpix"]
    w.wcs.cdelt = grid["cdelt"]
    return w


def read_f32(path: Path, shape) -> np.ndarray:
    return np.fromfile(path, "<f4").reshape(shape)


def _prep(img):
    a = np.array(img, float)
    fin = np.isfinite(a)
    if np.isposinf(a).any():  # saturated core: brightest finite value, so it still pulls the centroid
        a[np.isposinf(a)] = np.nanmax(np.where(fin, a, np.nan))
    return a


def centroid(img, x, y, r=4.0, iters=3):
    """Background-subtracted, positive-weighted centroid within r px of (x, y). None if nothing there."""
    a = _prep(img)
    ny, nx = a.shape
    yy, xx = np.mgrid[0:ny, 0:nx]
    cx, cy = x, y
    for _ in range(iters):
        rr = np.hypot(xx - cx, yy - cy)
        ann = (rr >= r + 2) & (rr <= r + 6) & np.isfinite(a)
        if ann.sum() < 10:
            return None
        bg = np.median(a[ann])
        win = (rr <= r) & np.isfinite(a)
        w = np.clip(a[win] - bg, 0, None)
        if w.sum() <= 0:
            return None
        cx, cy = float((w * xx[win]).sum() / w.sum()), float((w * yy[win]).sum() / w.sum())
    return cx, cy


def aperture(img, x, y, r=1.5, r_in=5.0, r_out=9.0):
    """(sum over aperture - background, number of pixels, saturated?)"""
    a = np.array(img, float)
    ny, nx = a.shape
    yy, xx = np.mgrid[0:ny, 0:nx]
    rr = np.hypot(xx - x, yy - y)
    ap = rr <= r
    sat = bool(np.isposinf(a[ap]).any())
    ann = (rr >= r_in) & (rr <= r_out) & np.isfinite(a)
    if ann.sum() < 10 or not np.isfinite(a[ap] if not sat else 0).all():
        return None
    bg = np.median(a[ann])
    vals = _prep(a)[ap]
    return float((vals - bg).sum()), int(ap.sum()), sat


def fit_motion(t_yr, ra, dec):
    """Linear motion (arcsec/yr east, north) from positions; needs >= 2 epochs."""
    ra0, dec0 = np.mean(ra), np.mean(dec)
    x = (np.asarray(ra) - ra0) * np.cos(np.radians(dec0)) * 3600
    y = (np.asarray(dec) - dec0) * 3600
    A = np.column_stack([np.asarray(t_yr) - np.mean(t_yr), np.ones(len(t_yr))])
    vx = np.linalg.lstsq(A, x, rcond=None)[0][0]
    vy = np.linalg.lstsq(A, y, rcond=None)[0][0]
    return float(vx), float(vy)


# --------------------------------------------------------------------------- static gallery

class Static:
    def __init__(self, root: Path, key: str):
        self.root, self.key = root, key
        self.d = root / key
        self.m = strict_json(self.d / "manifest.json")
        self.shape = tuple(self.m["grid"]["shape"])
        self.w = grid_wcs(self.m["grid"])
        self.bands = {b["id"]: b for b in self.m["bands"]}
        self.bj = {b: strict_json(self.d / f"{b}.json") for b, v in self.bands.items() if v["status"] == "ready"}
        self.mjd = {e["id"]: e["mjd_mid"] for e in self.m["epochs"]}
        self.track = {t["epoch"]: (t["ra"], t["dec"]) for t in self.m.get("target_track", [])}

    def frames(self, band):
        out = {}
        for e in self.bj.get(band, {}).get("epochs", []):
            if e["n_used"] > 0:
                out[e["id"]] = (read_f32(self.d / f"{e['id']}_{band}.f32", self.shape), e)
        return out

    def track_px(self, epoch):
        ra, dec = self.track[epoch]
        x, y = self.w.world_to_pixel_values(ra, dec)
        return float(x), float(y)


def check_static_files(rep: Report, root: Path, key: str) -> Static | None:
    sec = f"static:{key}"
    try:
        s = Static(root, key)
    except Exception as e:
        rep.add(sec, "manifest and band JSON parse strictly", FAIL, detail=repr(e)[:200])
        return None
    rep.check(sec, "manifest schema 1, key matches", s.m.get("schema") == 1 and s.m.get("key") == key)
    n_px = s.shape[0] * s.shape[1]
    bad, n_files = [], 0
    for b, bj in s.bj.items():
        for e in bj["epochs"]:
            for kind in ("", "_weight", "_flagfrac"):
                p = s.d / f"{e['id']}_{b}{kind}.f32"
                if e["n_used"] > 0:
                    n_files += 1
                    if not p.exists() or p.stat().st_size != 4 * n_px:
                        bad.append(p.name)
                elif p.exists():
                    bad.append(p.name + " (n_used 0 but file exists)")
    rep.check(sec, "every frame file present with 4*ny*nx bytes; none for n_used 0", not bad, measured=n_files,
              detail=", ".join(bad[:5]))
    n_ep = len(s.m["epochs"])
    rep.check(sec, "epochs in manifest", n_ep >= MIN_EPOCHS, measured=n_ep, threshold=f">= {MIN_EPOCHS}")
    best = max((len(s.frames(b)) for b in s.bj), default=0)
    rep.check(sec, "frames in the fullest band", best >= MIN_EPOCHS, measured=best, threshold=f">= {MIN_EPOCHS}")
    if s.track:
        ny, nx = s.shape
        pts = [s.track_px(e) for e in s.track]
        inside = min(min(x, y, nx - 1 - x, ny - 1 - y) for x, y in pts)
        rep.check(sec, "target_track inside the grid (min distance to edge, px)", inside >= TRACK_MARGIN_PX,
                  measured=round(inside, 1), threshold=f">= {TRACK_MARGIN_PX}")
    else:
        rep.add(sec, "target_track present", FAIL)
    try:
        g = strict_json(s.d / "gaia.json")
        ok = isinstance(g, list) and len(g) > 0 and all({"ra", "dec", "pmra", "pmdec", "g_mag", "ref_epoch"} <= set(r) for r in g)
        rep.check(sec, "gaia.json: list with contract fields", ok, measured=len(g))
    except Exception as e:
        rep.add(sec, "gaia.json parses strictly", FAIL, detail=repr(e)[:200])
    try:
        sp = strict_json(s.d / "spectrum.json")
        pts = sp.get("points", [])
        fields = {"wavelength_um", "bandwidth_um", "flux", "err", "mjd", "epoch", "band", "flagged"}
        ok = isinstance(sp.get("meta"), dict) and all(fields <= set(p) for p in pts)
        rep.check(sec, "spectrum.json: {meta, points} with contract fields", ok, measured=len(pts))
        n_ref = s.m.get("n_images")
        if n_ref:
            frac = len(pts) / n_ref
            rep.check(sec, "spectrum points per image at the target", frac >= 0.5, measured=round(frac, 2),
                      threshold=">= 0.5", detail="points lost for saturated targets are counted here")
    except Exception as e:
        rep.add(sec, "spectrum.json parses strictly", FAIL, detail=repr(e)[:200])
    rep.check(sec, "provenance.json present", (s.d / "provenance.json").exists())
    return s


def check_core(rep: Report, s: Static, key: str):
    """No data holes around the target: within CORE_RADIUS_ARCSEC of the track, a pixel with weight > 0 or
    with >= 6 of its 8 neighbours finite must not be NaN (saturated = +Inf, contract). Barnard and R Dra
    must show +Inf in the core, RW Aur a finite core."""
    sec = f"target:{key}"
    if not s.track:
        rep.add(sec, "core check needs target_track", FAIL)
        return
    rad_px = CORE_RADIUS_ARCSEC / (abs(s.m["grid"]["cdelt"][1]) * 3600)
    ny, nx = s.shape
    yy, xx = np.mgrid[0:ny, 0:nx]
    holes, n_inf, n_core_nonfinite, per = 0, 0, 0, {}
    low_holes = low_inf = 0
    excused, unexcused = {}, {}
    satfrac = {}
    if SATURATION_CHECK.exists():
        for r_ in strict_json(SATURATION_CHECK):
            satfrac[(r_["target"], r_["band"], int(r_["epoch"]))] = r_.get("sat_fraction_at_target")
    for b in sorted(s.bj):
        for e, (img, ej) in sorted(s.frames(b).items()):
            if e not in s.track:
                continue
            tx, ty = s.track_px(e)
            rr = np.hypot(xx - tx, yy - ty)
            near = rr <= rad_px
            w = read_f32(s.d / f"{e}_{b}_weight.f32", s.shape)
            filled = np.isfinite(img) | np.isposinf(img)
            pad = np.pad(filled, 1, constant_values=False)
            nb = sum(pad[1 + dy:ny + 1 + dy, 1 + dx:nx + 1 + dx].astype(int)
                     for dy in (-1, 0, 1) for dx in (-1, 0, 1) if dy or dx)
            should = near & ((np.nan_to_num(w) > 0) | (nb >= 6))
            h = int((np.isnan(img) & should).sum())
            core = rr <= CORE_PX
            i = int(np.isposinf(img[near]).sum())
            c = int((~np.isfinite(img[core])).sum())
            holes += h
            n_inf += i
            n_core_nonfinite += c
            if c:
                fr = satfrac.get((key, b, int(e)))
                (excused if fr is not None and fr > SAT_FRACTION_MAX else unexcused)[f"{e}_{b}"] = \
                    {"nonfinite_core_px": c, "sat_fraction_at_target": fr}
            low = ej.get("quality") == "low"
            if low:
                low_holes += h
                low_inf += i
            if h or i:
                per[f"{e}_{b}"] = {"nan_holes": h, "inf": i, **({"quality": "low"} if low else {})}
    rep.check(sec, f"no NaN holes within {CORE_RADIUS_ARCSEC:g} arcsec of the track (weight > 0 or filled neighbours)",
              holes == 0, measured=holes, threshold="0", detail=json.dumps(per)[:600])
    rep.add(sec, "of which in frames marked quality=low (n_used < 5)", INFO,
            measured={"nan_holes": low_holes, "inf_near_track": low_inf})
    want = SATURATED_CORE.get(key)
    if want == "inf":
        rep.check(sec, "saturated core is +Inf (pixels near the track, all frames)", n_inf > 0, measured=n_inf,
                  threshold="> 0")
    elif want == "finite":
        rep.check(sec, f"core finite, or truly saturated there (> {SAT_FRACTION_MAX:.0%} of exposures, "
                       f"{SATURATION_CHECK.name})", not unexcused,
                  measured={"nonfinite_core_px": n_core_nonfinite, "excused": excused, "not_excused": unexcused},
                  threshold=f"every non-finite core pixel has sat_fraction_at_target > {SAT_FRACTION_MAX}")


def check_empty_sky_inf(rep: Report, s: Static, key: str):
    """+Inf (saturated) only on stars. +Inf pixels are grouped into 8-connected components; a component is a
    star core when one of its pixels lies within CORE_LINK_ARCSEC of the track or of a Gaia source brighter than
    G = CORE_LINK_GMAX (propagated to the epoch); otherwise it is empty sky. Item: empty-sky pixels = 0, all
    frames (low quality counted apart as INFO). Same definition as the co-add session's test."""
    from astropy.time import Time
    from scipy.ndimage import binary_dilation, label
    sec = f"target:{key}"
    gaia = [g for g in strict_json(s.d / "gaia.json") if g.get("g_mag") is not None and g["g_mag"] < CORE_LINK_GMAX]
    gc = None
    if gaia:
        gc = SkyCoord(ra=[g["ra"] for g in gaia] * u.deg, dec=[g["dec"] for g in gaia] * u.deg,
                      pm_ra_cosdec=[g.get("pmra") or 0.0 for g in gaia] * u.mas / u.yr,
                      pm_dec=[g.get("pmdec") or 0.0 for g in gaia] * u.mas / u.yr,
                      obstime=Time(2016.0, format="jyear"))
    ny, nx = s.shape
    yy, xx = np.mgrid[0:ny, 0:nx]
    rad = CORE_LINK_ARCSEC / (abs(s.m["grid"]["cdelt"][1]) * 3600)
    total, low_total, per = 0, 0, {}
    total_nodil, per_nodil = 0, {}
    for e in s.m["epochs"]:
        anchor = np.zeros((ny, nx), bool)
        if e["id"] in s.track:
            tx, ty = s.track_px(e["id"])
            anchor |= np.hypot(xx - tx, yy - ty) <= rad
        if gc is not None:
            ge = gc.apply_space_motion(new_obstime=Time(e["mjd_mid"], format="mjd"))
            gx, gy = s.w.world_to_pixel(SkyCoord(ge.ra, ge.dec))
            for sx, sy in zip(gx, gy):
                if -rad < sx < nx + rad and -rad < sy < ny + rad:
                    anchor |= np.hypot(xx - sx, yy - sy) <= rad
        for b in sorted(s.bj):
            fr = s.frames(b).get(e["id"])
            if fr is None:
                continue
            img, ej = fr
            inf = np.isposinf(img)
            if not inf.any():
                continue
            # without dilation (INFO only)
            lab0, n0 = label(inf, structure=np.ones((3, 3), int))
            core0 = set(np.unique(lab0[anchor & (lab0 > 0)]))
            k0 = int(((lab0 > 0) & ~np.isin(lab0, list(core0))).sum())
            if k0:
                per_nodil[f"{e['id']}_{b}"] = k0
                total_nodil += k0
            # the item: dilate by one pixel, then group, then count only +Inf pixels
            grown = binary_dilation(inf, structure=np.ones((3, 3), bool), iterations=1)
            lab, n = label(grown, structure=np.ones((3, 3), int))
            core_ids = set(np.unique(lab[anchor & (lab > 0)]))
            empty = inf & ~np.isin(lab, list(core_ids))
            k = int(empty.sum())
            if k:
                low = ej.get("quality") == "low"
                per[f"{e['id']}_{b}"] = k if not low else f"{k} (quality low)"
                total += k
                low_total += k if low else 0
    rep.check(sec, f"no +Inf on empty sky (+Inf map dilated by 1 px, 8-connected groups not touching the track or "
                   f"a Gaia G < {CORE_LINK_GMAX:g} star within {CORE_LINK_ARCSEC:g} arcsec)",
              total == 0, measured=total, threshold="0", detail=json.dumps(per)[:600])
    rep.add(sec, "same count without the dilation step", INFO, measured=total_nodil, detail=json.dumps(per_nodil)[:400])
    rep.add(sec, "of which in frames marked quality=low", INFO, measured=low_total)


def check_quality_flags(rep: Report, s: Static, key: str):
    """Every frame's epoch record says quality ok/low, low exactly when n_used < 5."""
    sec = f"static:{key}"
    bad, low = [], []
    for b, bj in s.bj.items():
        for e in bj["epochs"]:
            if e["n_used"] <= 0:
                continue
            want = "low" if e["n_used"] < 5 else "ok"
            if e.get("quality") != want:
                bad.append(f"{e['id']}_{b}:{e.get('quality')}")
            if want == "low":
                low.append(f"{e['id']}_{b} (n={e['n_used']})")
    rep.check(sec, "band.json quality = low exactly when n_used < 5", not bad, measured=len(bad), detail=", ".join(bad[:8]))
    rep.add(sec, "frames marked quality=low", INFO, measured=low)


def measure_motion(s: Static, band: str, r=4.0):
    """Centroid near the track in every epoch of `band` -> (rows, (vx, vy)). rows: epoch, x, y, dist_px, snr."""
    rows = []
    for e, (img, ej) in sorted(s.frames(band).items()):
        if e not in s.track:
            continue
        tx, ty = s.track_px(e)
        c = centroid(img, tx, ty, r)
        ap = aperture(img, tx, ty)
        noise = ej.get("noise")
        snr = None
        if ap is not None and noise:
            snr = math.inf if ap[2] else ap[0] / (noise * math.sqrt(ap[1]))
        if c is None:
            rows.append({"epoch": e, "x": None, "y": None, "dist_px": None, "snr": snr})
            continue
        ra, dec = s.w.pixel_to_world_values(*c)
        rows.append({"epoch": e, "x": round(c[0], 2), "y": round(c[1], 2), "ra": float(ra), "dec": float(dec),
                     "dist_px": round(math.hypot(c[0] - tx, c[1] - ty), 2),
                     "snr": None if snr is None else (snr if math.isinf(snr) else round(snr, 1)), "mjd": s.mjd[e]})
    good = [r_ for r_ in rows if r_.get("ra") is not None]
    v = None
    if len(good) >= 2:
        v = fit_motion([(r_["mjd"] - 51544.5) / 365.25 for r_ in good], [r_["ra"] for r_ in good], [r_["dec"] for r_ in good])
    return rows, v


def best_band(s: Static, bands):
    """Band whose weakest epoch has the highest S/N at the track."""
    best, best_snr = None, -1.0
    for b in bands:
        rows, _ = measure_motion(s, b)
        snrs = [r["snr"] for r in rows if r["snr"] is not None]
        if len(rows) >= MIN_EPOCHS and len(snrs) == len(rows):
            m = min(snrs)
            if m > best_snr:
                best, best_snr = b, m
    return best, best_snr


def check_mover(rep: Report, s: Static, key: str, band: str | None = None, allow_not_detected=False,
                motion_as_info: str | None = None):
    sec = f"target:{key}"
    t = TARGETS[key]
    pm = (t["pmra"] / 1000.0, t["pmdec"] / 1000.0)
    exp = math.hypot(*pm)
    bands = [band] if band else [b for b in s.bj if b.startswith("D")]
    b, snr_min = best_band(s, bands)
    if b is None:
        rep.add(sec, "a band with the target measurable in >= 3 epochs", FAIL)
        return
    rows, v = measure_motion(s, b)
    detected = snr_min >= DETECT_SNR
    rep.add(sec, f"source at the track in every epoch ({b}, min S/N)", PASS if detected else (INFO if allow_not_detected else FAIL),
            measured=snr_min if math.isinf(snr_min) else round(snr_min, 1), threshold=f">= {DETECT_SNR}",
            detail=json.dumps(rows))
    if not detected:
        if allow_not_detected:
            notes = " ".join(s.m.get("notes", [])).lower()
            said = any(w in notes for w in ("not detected", "not visible", "no source", "upper limit"))
            rep.check(sec, "not detected, and the page says so (manifest notes)", said,
                      detail="notes: " + (" | ".join(s.m.get("notes", [])) or "none"))
        return
    dmax = max(r_["dist_px"] for r_ in rows if r_["dist_px"] is not None)
    rep.check(sec, f"centroid on the track every epoch ({b}, max distance px)", dmax <= POS_TOL_PX,
              measured=dmax, threshold=f"<= {POS_TOL_PX}")
    if v is None:
        rep.add(sec, "motion measured", FAIL)
        return
    dv = math.hypot(v[0] - pm[0], v[1] - pm[1])
    tol = max(MOTION_REL_TOL * exp, MOTION_ABS_TOL)
    meas = {"v": [round(v[0], 2), round(v[1], 2)], "|v|": round(math.hypot(*v), 2),
            "expected": [round(pm[0], 2), round(pm[1], 2)], "|v-pm|": round(dv, 2)}
    if motion_as_info:
        rep.add(sec, f"raw centroid motion ({b}), arcsec/yr", INFO, measured=meas, detail=motion_as_info)
        return v
    rep.check(sec, f"motion vector vs proper motion ({b}); measured |v| and |v - pm| in arcsec/yr",
              dv <= tol, measured=meas, threshold=f"|v-pm| <= {tol:.2f}")
    return v


WISE_DETECTION = ROOT / "handoff" / "results" / "wise0855_detection_v2.json"


def check_wise(rep: Report, s: Static):
    """In W the moving object and a fixed Gaia star (11" -> 4") blend within one PSF, so a plain centroid
    moves slower than the object. The motion item uses the image-quality session's two-PSF positions;
    the plain centroid is kept as information next to its expected diluted value."""
    sec = "target:wise-0855"
    t = TARGETS["wise-0855"]
    pm = (t["pmra"] / 1000.0, t["pmdec"] / 1000.0)
    det = strict_json(WISE_DETECTION) if WISE_DETECTION.exists() else None
    note = "centroid of object + fixed star; see the deblended item"
    if det:
        fc = det.get("flux_check", {})
        fm, fs = fc.get("W_mover_mean_mjy"), fc.get("W_star_mean_mjy")
        if fm and fs:
            k = fm / (fm + fs)
            note += (f"; expected diluted motion = f_obj/(f_obj+f_star) x pm = {k:.2f} x "
                     f"{math.hypot(*pm):.2f} = {k * math.hypot(*pm):.2f} arcsec/yr (W fluxes {fm:.3f}, {fs:.3f} mJy)")
    check_mover(rep, s, "wise-0855", band="W", motion_as_info=note)
    if det is None:
        rep.add(sec, "deblended motion (image-quality two-PSF positions)", FAIL, detail=f"missing {WISE_DETECTION}")
    else:
        mo = det["motion_arcsec_per_yr"]
        era, edec = mo.get("pmra_err_scaled", mo["pmra_err"]), mo.get("pmdec_err_scaled", mo["pmdec_err"])
        zra, zdec = (mo["pmra"] - pm[0]) / era, (mo["pmdec"] - pm[1]) / edec
        rep.check(sec, "deblended motion vs literature (two-PSF positions, scaled errors)",
                  abs(zra) <= DEBLENDED_MAX_SIGMA and abs(zdec) <= DEBLENDED_MAX_SIGMA,
                  measured={"pmra": f"{mo['pmra']:.2f} +- {era:.2f}", "pmdec": f"{mo['pmdec']:.2f} +- {edec:.2f}",
                            "literature": [round(pm[0], 3), round(pm[1], 3)], "z": [round(zra, 2), round(zdec, 2)]},
                  threshold=f"|z| <= {DEBLENDED_MAX_SIGMA} per component",
                  detail=f"source {WISE_DETECTION.relative_to(ROOT)}")
    rows, v = measure_motion(s, "D2")
    if v is None:
        rep.add(sec, "D2: brightest source near the track measured", FAIL, detail=json.dumps(rows))
        return
    sp = math.hypot(*v)
    rep.check(sec, "D2: the source near the track does not move (background star)", sp <= STATIC_MAX,
              measured=round(sp, 2), threshold=f"<= {STATIC_MAX} arcsec/yr", detail=json.dumps(rows))


VARIABILITY_KIND = {"rw-aur": "flux", "r-dra": "wing"}   # R Dra: every aperture point saturated -> wing series
MIN_VARIABLE_BANDS = {"rw-aur": 3, "r-dra": 1}


def check_variable(rep: Report, s: Static, key: str):
    """Variability with server.pipeline.variability_report on the published spectrum (the numbers the page
    shows): a band is variable per spx.photometry.is_variable (>= 5 controls, ratio > 1.15 and > every control),
    undetermined with fewer controls. PASS with >= MIN_VARIABLE_BANDS variable bands, INFO when undetermined,
    FAIL otherwise. R Dra uses the PSF-wing series."""
    sec = f"target:{key}"
    kind = VARIABILITY_KIND[key]
    from server import pipeline as P
    sp = strict_json(s.d / "spectrum.json")
    f = P.open_field(target=key)
    cuts = []
    for d in range(1, 7):
        c, _ = P.fetch_band_cutouts(f, f"D{d}", ("IMAGE", "FLAGS", "VARIANCE"))
        cuts += c
    r_ = P.variability_report(f, cuts, sp["points"], strict_json(s.d / "gaia.json"), kind)
    per = r_["per_band"]
    variable = [b for b in sorted(per) if per[b]["variable"]]
    undetermined = [b for b in sorted(per) if per[b]["variable"] is None]
    decided_no = [b for b in sorted(per) if per[b]["variable"] is False]
    need = MIN_VARIABLE_BANDS.get(key, 1)
    if not per:
        status = FAIL
    else:
        status = PASS if len(variable) >= need else (INFO if undetermined else FAIL)
    rep.add(sec, "per band: target ratio, largest control, controls", INFO,
            measured={b: [d["ratio"], d["controls_max"], d["n_controls"]] for b, d in sorted(per.items())})
    rep.add(sec, "bands where is_variable is true", INFO, measured=variable)
    rep.add(sec, f"brightness change at the target ({kind} series; >= {need} bands variable)", status,
            measured={"variable": variable, "not_variable": decided_no, "undetermined": undetermined},
            threshold="spx.photometry.is_variable: >= 5 controls, ratio > 1.15 and > every control",
            detail=f"target G {r_['target_g']}, saturated={r_['saturated']}; {r_['n_controls']} control stars"
                   + ("; undetermined = fewer than 5 controls, not counted as a pass" if status == INFO else ""))


# --------------------------------------------------------------------------- server

def free_port():
    with socket.socket() as so:
        so.bind(("127.0.0.1", 0))
        return so.getsockname()[1]


def wait_http(url, timeout=60):
    import requests
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            if requests.get(url, timeout=3).status_code < 500:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def get(sess, url, **params):
    t = time.time()
    r = sess.get(url, params=params, timeout=200)
    return r, time.time() - t


def wait_idle(sess, api, timeout=300):
    """Cold timings are measured on an idle server: wait until no band is queued or being built (earlier
    fields of this run keep filling their bands in the background). Returns seconds waited, or None."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        h = strict_json(sess.get(f"{api}/api/health", timeout=10).content)
        q = h.get("bands_queued", {})
        if not any(q.values()) and h.get("bands_running", 0) == 0:
            return round(time.time() - t0, 1)
        time.sleep(2)
    return None


def free_field(rep: Report, sess, api, prof, ra, dec, label, need_epochs=1):
    sec = f"server:{label}"
    waited = wait_idle(sess, api)
    if waited is None:
        rep.add(sec, "server idle before a cold measurement", FAIL, detail="queue not empty after 300 s")
    elif waited > 0:
        rep.add(sec, "waited for the server to be idle (s)", INFO, measured=waited)
    r, t_man = get(sess, f"{api}/api/manifest", ra=ra, dec=dec)
    if r.status_code != 200:
        rep.add(sec, f"manifest ({ra}, {dec})", FAIL, measured=r.status_code, detail=r.text[:200])
        return None
    m = strict_json(r.content)
    # manifest time = median of 3 cold SIA2 lookups: this position and two 11"/22" away (queue=0: no bands)
    times = [t_man]
    for k in (1, 2):
        d2 = max(-90.0, min(90.0, dec + 0.003 * k * (1 if dec < 89 else -1)))
        _, t_k = get(sess, f"{api}/api/manifest", ra=ra, dec=round(d2, 4), queue=0)
        times.append(t_k)
    t_med = float(np.median(times))
    rep.check(sec, f"manifest seconds, median of 3 cold ({ra}, {dec})", t_med <= prof["manifest"],
              measured=round(t_med, 1), threshold=f"<= {prof['manifest']}",
              detail="each: " + ", ".join(f"{t:.1f}" for t in times))
    rep.check(sec, "epochs", len(m["epochs"]) >= need_epochs, measured=len(m["epochs"]), threshold=f">= {need_epochs}")
    if not m["epochs"]:
        return m
    b0 = m.get("default_band") or m["bands"][0]["id"]
    r2, t_band = get(sess, f"{api}/api/band", key=m["key"], band=b0)
    ok = r2.status_code == 200
    rep.check(sec, f"first band {b0} seconds (cold)", ok and t_band <= prof["first_band"],
              measured=round(t_band, 1) if ok else r2.status_code, threshold=f"<= {prof['first_band']}")
    if not ok:
        return m
    bj = strict_json(r2.content)
    n_fr = sum(e["n_used"] > 0 for e in bj["epochs"])
    rep.check(sec, "frames in the first band", n_fr >= need_epochs, measured=n_fr, threshold=f">= {need_epochs}")
    m["_first_band"] = b0
    m["_n_frames"] = n_fr
    return m


def check_server(rep: Report, api: str, prof: dict, seed: int):
    import requests
    sess = requests.Session()
    try:
        h = strict_json(sess.get(f"{api}/api/health", timeout=10).content)
    except Exception as e:
        rep.add("server", f"API reachable at {api}", FAIL, detail=repr(e)[:200])
        return
    rep.check("server", "health ok", h.get("ok") is True, measured=h.get("commit"))
    r = sess.get(f"{api}/api/health", timeout=10)
    rep.check("server", "Access-Control-Allow-Origin: * without an Origin header",
              r.headers.get("access-control-allow-origin") == "*")
    rnd = random.Random(seed)

    # cold random position, then the same requests again
    ra, dec = round(rnd.uniform(0, 360), 4), round(rnd.uniform(-60, 60), 4)
    t0 = time.time()
    m = free_field(rep, sess, api, prof, ra, dec, "random")
    if m and m.get("_first_band"):
        key, b0 = m["key"], m["_first_band"]
        # let this field's background bands finish so the counters only see the repeated requests
        t_all = None
        if wait_idle(sess, api) is not None:
            t_all = time.time() - t0
        before = strict_json(sess.get(f"{api}/api/health", timeout=10).content).get("network_totals", {})
        _, t1 = get(sess, f"{api}/api/manifest", ra=ra, dec=dec)
        r2, t2 = get(sess, f"{api}/api/band", key=key, band=b0)
        bj = strict_json(r2.content)
        for e in bj["epochs"]:
            if e["n_used"] > 0:
                for kind in (None, "weight", "flagfrac"):
                    p = {"key": key, "band": b0, "epoch": e["id"]}
                    if kind:
                        p["kind"] = kind
                    sess.get(f"{api}/api/frame", params=p, timeout=60)
        after = strict_json(sess.get(f"{api}/api/health", timeout=10).content).get("network_totals", {})
        rep.check("server:random", "same request again: seconds (manifest + band)", t1 + t2 <= prof["cached"],
                  measured=round(t1 + t2, 2), threshold=f"<= {prof['cached']}")
        if before and after:
            same = before.get("s3_bytes") == after.get("s3_bytes") and before.get("sia_queries") == after.get("sia_queries")
            rep.check("server:random", "same request again downloads nothing (S3 bytes, SIA2 queries)", same,
                      measured={"before": before, "after": after})
        else:
            rep.add("server:random", "network counters in /api/health", FAIL)
        # all six bands (the field was the only work on the server, so idle = all its bands built)
        mm = strict_json(sess.get(f"{api}/api/manifest", params={"ra": ra, "dec": dec}, timeout=30).content)
        if not all(b["status"] != "pending" for b in mm["bands"] if b["id"].startswith("D")):
            t_all = None
        lim = prof["six_bands"]
        if lim is None:
            rep.add("server:random", "all six bands ready (seconds from the first request)", INFO,
                    measured=None if t_all is None else round(t_all, 1), detail="no threshold in the local profile")
        else:
            rep.check("server:random", "all six bands ready (seconds)", t_all is not None and t_all <= lim,
                      measured=None if t_all is None else round(t_all, 1), threshold=f"<= {lim}")

    # RA 0 / 360
    d = round(rnd.uniform(-60, 60), 4)
    a = free_field(rep, sess, api, prof, round(rnd.uniform(0.0001, 0.001), 4), d, "ra0")
    b = free_field(rep, sess, api, prof, round(rnd.uniform(359.999, 359.9999), 4), d, "ra360")
    if a and b and a.get("n_images") and b.get("n_images"):
        diff = abs(a["n_images"] - b["n_images"]) / max(a["n_images"], b["n_images"])
        rep.check("server:ra0/360", "both sides of RA 0 see the same images (relative difference)", diff <= 0.01,
                  measured=round(diff, 4), threshold="<= 0.01",
                  detail=f"{a['n_images']} vs {b['n_images']}")
    free_field(rep, sess, api, prof, round(rnd.uniform(0, 360), 4), round(rnd.uniform(85.5, 89.9), 4), "dec>85N")
    free_field(rep, sess, api, prof, round(rnd.uniform(0, 360), 4), round(rnd.uniform(-89.9, -85.5), 4), "dec>85S")


def check_no_coverage(rep: Report, tmp: Path):
    """A second server that searches only spherex_qr3, where WISE 0855's position has no image."""
    import requests
    port = free_port()
    env = dict(os.environ, SPX_COLLECTIONS="spherex_qr3", SPX_CACHE=str(tmp / "cache_qr3"))
    p = subprocess.Popen([sys.executable, "-m", "uvicorn", "server.app:app", "--host", "127.0.0.1", "--port", str(port),
                          "--log-level", "warning"], cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if not wait_http(f"http://127.0.0.1:{port}/api/health"):
            rep.add("server:no-coverage", "second server started", FAIL)
            return
        r = requests.get(f"http://127.0.0.1:{port}/api/manifest", params={"ra": 133.7951, "dec": -7.2451}, timeout=60)
        m = strict_json(r.content)
        rep.check("server:no-coverage", "200, epochs [], bands [], message", r.status_code == 200 and m["epochs"] == []
                  and m["bands"] == [] and bool(m.get("message")), measured=r.status_code, detail=m.get("message", ""))
    finally:
        p.terminate()
        p.wait(10)


def check_static_without_server(rep: Report, root_web: Path):
    """Server down: the gallery is plain files. Serve web/ alone and read every target; the API port is closed."""
    import requests
    port = free_port()
    p = subprocess.Popen([sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1", "-d", str(root_web)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if not wait_http(f"http://127.0.0.1:{port}/data/index.json"):
            rep.add("robustness", "static server started", FAIL)
            return
        dead = free_port()
        try:
            requests.get(f"http://127.0.0.1:{dead}/api/health", timeout=3)
            api_down = False
        except requests.exceptions.ConnectionError:
            api_down = True
        idx = strict_json(requests.get(f"http://127.0.0.1:{port}/data/index.json", timeout=10).content)
        keys = [t["key"] for t in idx["targets"]]
        ok = []
        for k in keys:
            m = requests.get(f"http://127.0.0.1:{port}/data/{k}/manifest.json", timeout=10)
            b = strict_json(m.content)["bands"][0]["id"]
            bj = requests.get(f"http://127.0.0.1:{port}/data/{k}/{b}.json", timeout=10)
            ok.append(m.status_code == 200 and bj.status_code == 200)
        rep.check("robustness", "API down, gallery served as static files (targets readable)",
                  api_down and all(ok) and set(TARGETS) <= set(keys), measured=f"{sum(ok)}/{len(keys)}",
                  threshold=f"all of {sorted(TARGETS)}")
    finally:
        p.terminate()
        p.wait(10)


def check_final(rep: Report, root: Path):
    commits, dirty = set(), {}
    for k in TARGETS:
        p = root / k / "provenance.json"
        if not p.exists():
            dirty[k] = "no provenance.json"
            continue
        pv = strict_json(p)
        commits.add(pv.get("spx_commit"))
        if pv.get("spx_uncommitted_code"):
            dirty[k] = pv["spx_uncommitted_code"]
    rep.check("final", "gallery built from committed code (spx_uncommitted_code empty, one commit)",
              not dirty and len(commits) == 1, measured={"commits": sorted(c or "?" for c in commits), "dirty": dirty})


# --------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default=str(ROOT / "web" / "data"))
    ap.add_argument("--api", default="http://127.0.0.1:8000")
    ap.add_argument("--profile", choices=list(PROFILES), default="local")
    ap.add_argument("--static-only", action="store_true")
    ap.add_argument("--seed", type=int, default=None, help="random positions (default: time-based, so caches are cold)")
    ap.add_argument("--out", default=None)
    ap.add_argument("--docs", action="store_true",
                    help="also copy the report to docs/acceptance/ (the final run, kept in the repository)")
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    warnings.simplefilter("ignore")
    seed = a.seed if a.seed is not None else int(time.time())
    root = Path(a.data)
    rep = Report()
    started = dt.datetime.now(dt.timezone.utc)
    print(f"acceptance {started.isoformat(timespec='seconds')}  data={root}  api={None if a.static_only else a.api}  "
          f"profile={a.profile}  seed={seed}")

    print("\n== static gallery")
    try:
        idx = strict_json(root / "index.json")
        keys = [t["key"] for t in idx["targets"]]
        rep.check("static", "index.json lists the five targets", set(TARGETS) <= set(keys), measured=keys)
    except Exception as e:
        rep.add("static", "index.json parses strictly", FAIL, detail=repr(e)[:200])
    st = {k: check_static_files(rep, root, k) for k in TARGETS if (root / k).exists()}
    for k in TARGETS:
        if k not in st:
            rep.add(f"static:{k}", "folder present", FAIL)

    print("\n== behaviour at the target")
    for k, sk in st.items():
        if sk is not None:
            check_quality_flags(rep, sk, k)
            check_core(rep, sk, k)
            check_empty_sky_inf(rep, sk, k)
    if st.get("barnard"):
        check_mover(rep, st["barnard"], "barnard")
    if st.get("eps-indi-b"):
        check_mover(rep, st["eps-indi-b"], "eps-indi-b", allow_not_detected=True)
    if st.get("wise-0855"):
        check_wise(rep, st["wise-0855"])
    for k in ("rw-aur", "r-dra"):
        if st.get(k):
            check_variable(rep, st[k], k)

    print("\n== final build")
    check_final(rep, root)

    if not a.static_only:
        tmp = ROOT / "cache" / "acceptance"
        tmp.mkdir(parents=True, exist_ok=True)
        print("\n== robustness")
        check_static_without_server(rep, root.parent)
        print("\n== server")
        check_server(rep, a.api.rstrip("/"), PROFILES[a.profile], seed)
        check_no_coverage(rep, tmp)

    counts = {s: sum(i["status"] == s for i in rep.items) for s in (PASS, FAIL, PENDING, INFO)}
    from server import pipeline as _P
    P_SMALL_MARGIN, P_CLEARLY_ABOVE = _P.SMALL_MARGIN, _P.CLEARLY_ABOVE
    print(f"\nSUMMARY  {counts}")
    out = Path(a.out) if a.out else ROOT / "cache" / "acceptance" / f"acceptance_{started.strftime('%Y%m%dT%H%M%SZ')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"started_utc": started.isoformat(timespec="seconds"), "profile": a.profile,
                               "api": None if a.static_only else a.api, "seed": seed, "summary": counts,
                               "thresholds": {"MIN_EPOCHS": MIN_EPOCHS, "DETECT_SNR": DETECT_SNR, "POS_TOL_PX": POS_TOL_PX,
                                              "MOTION_REL_TOL": MOTION_REL_TOL, "MOTION_ABS_TOL": MOTION_ABS_TOL,
                                              "STATIC_MAX": STATIC_MAX, "profile": PROFILES[a.profile]},
                               "wording_thresholds": {
                                   "note": "used only to word the page notes (server.pipeline.measured_notes); "
                                           "they never decide PASS/FAIL",
                                   "SMALL_MARGIN": P_SMALL_MARGIN, "CLEARLY_ABOVE": P_CLEARLY_ABOVE},
                               "items": rep.items}, indent=1, default=str), encoding="utf-8", newline="\n")
    print(f"report: {out}")
    if a.docs:
        dst = ROOT / "docs" / "acceptance" / out.name
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(out.read_bytes())
        print(f"copied: {dst}")
    sys.exit(0 if counts[FAIL] == 0 and counts[PENDING] == 0 else 1)


if __name__ == "__main__":
    main()
