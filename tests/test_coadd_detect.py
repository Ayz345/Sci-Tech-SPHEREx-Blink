"""Validation of spx.detect (v1.2 F2 automatic candidate finder) on the gallery frames. Offline: reads web/data/.

Run:  python tests/test_coadd_detect.py            (result in cache/test_results/; --write updates
      handoff/results/detect_validation_v2.json, see tests/resultio.py)

Parts
  targets    the five gallery fields with one parameter set: is WISE 0855 found (band W) and Epsilon Indi B,
             without giving their positions; what happens with Barnard's Star; candidates per field.
  injection  synthetic moving point sources (Gaussian PSF with the band's measured FWHM, linear motion, the same
             flux in every epoch) added to the real frames of a sparse field (WISE 0855, W) and a crowded field
             (Barnard's Star, D2); recovery rate against per-epoch S/N and total motion over the data span.
             Recovered = a mover candidate within 1.5 px (at t_ref) whose motion differs by <= 1 px over the span.
Thresholds are never changed per field.
"""
from __future__ import annotations

import copy
import json
import math
import sys
import time
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
warnings.filterwarnings("ignore")

import numpy as np
from astropy.coordinates import SkyCoord

from resultio import write_json
from spx import code_hash
from spx import detect as D

OUT_NAME = "detect_validation_v2.json"
FIELDS = ["barnard", "eps-indi-b", "wise-0855", "rw-aur", "r-dra"]
SNR_LEVELS = [3.0, 4.0, 5.0, 6.0, 8.0, 12.0]   # per-epoch PSF S/N of the injected source
SHIFTS_PX = [1.0, 2.0, 3.0, 5.0]               # total motion over the data span (grid px, 3" each)
N_PER_RUN = 6
REPEATS = 3


def target_part():
    out = {}
    for key in FIELDS:
        t = time.time()
        f = D.load_field(ROOT / "web" / "data" / key)
        res = D.find_candidates(f)
        dt = time.time() - t
        man = f["manifest"]
        track = {e["epoch"]: (e["ra"], e["dec"]) for e in man.get("target_track", [])}
        w = D.grid_wcs(man["grid"])
        found = None
        for c in res["candidates"]:
            ds = []
            for pos in c["positions"]:
                if pos["epoch"] in track:
                    a = SkyCoord(*track[pos["epoch"]], unit="deg"); b = SkyCoord(pos["ra"], pos["dec"], unit="deg")
                    ds.append(a.separation(b).arcsec)
            if ds and float(np.median(ds)) <= 6.0:
                found = {"id": c["id"], "kind": c["kind"], "known": c["known"], "snr": c["snr"], "bands": c["bands"],
                         "rate_arcsec_per_year": c["rate_arcsec_per_year"],
                         "median_sep_from_target_track_arcsec": round(float(np.median(ds)), 2),
                         "near_gaia": c["near_gaia"]}
                break
        others = [c for c in res["candidates"] if found is None or c["id"] != found["id"]]
        out[key] = {"seconds": round(dt, 1), "bands_searched": res["bands_searched"], "target": found,
                    "n_candidates": len(res["candidates"]),
                    "other_candidates": {"mover": sum(c["kind"] == "mover" and not c["known"] for c in others),
                                         "mover_known": sum(c["kind"] == "mover" and c["known"] for c in others),
                                         "variable": sum(c["kind"] == "variable" for c in others)},
                    "rejected": res["rejected"], "validation": res["validation"],
                    "candidates": res["candidates"]}
    return out


def _inject(field, bid, rng, snr, shift_px):
    """Copy of field with N_PER_RUN moving sources added to band bid. Returns (field, truths)."""
    B = D._Band(bid, field["bands"][bid], dict(D.DEFAULT_PARAMS))
    ny, nx = B.shape
    sg = B.sigma
    # per-epoch amplitude noise (same scale as the detector's Z maps), median over the frame
    sig_amp = []
    for k in range(B.n):
        ivar = np.where(B.good, 1.0 / B.var[k], 0.0)
        den = np.pi * sg * sg * D.gaussian_filter(ivar, sg / math.sqrt(2), mode="constant")
        sig_amp.append(float(np.median(1.0 / np.sqrt(den[B.inner]))))
    amp = snr * float(np.median(sig_amp))
    v = shift_px / B.span
    truths = []
    tries = 0
    m = 12
    while len(truths) < N_PER_RUN and tries < 500:
        tries += 1
        x, y = rng.uniform(m, nx - m), rng.uniform(m, ny - m)
        ang = rng.uniform(0, 2 * math.pi)
        vx, vy = v * math.cos(ang), v * math.sin(ang)
        pts = [(x + vx * d, y + vy * d) for d in B.dt]
        if not all(B.good[int(round(py)), int(round(px))] for px, py in pts):
            continue
        if any(math.hypot(x - t["x"], y - t["y"]) < 15 for t in truths):
            continue
        truths.append({"x": x, "y": y, "vx": vx, "vy": vy})
    g = copy.deepcopy(field)
    yy, xx = np.mgrid[0:ny, 0:nx]
    for k, e in enumerate(B.eps):
        img = g["bands"][bid]["frames"][e["id"]]["image"].astype(np.float64)
        for t in truths:
            img += amp * D._psf(xx, yy, t["x"] + t["vx"] * B.dt[k], t["y"] + t["vy"] * B.dt[k], sg)
        g["bands"][bid]["frames"][e["id"]]["image"] = img.astype(np.float32)
    return g, truths, B


def injection_part():
    out = {}
    for key, bid, label in (("wise-0855", "W", "sparse"), ("barnard", "D2", "crowded")):
        field = D.load_field(ROOT / "web" / "data" / key)
        base = D.find_candidates(field, bands=[bid])
        rows = []
        for snr in SNR_LEVELS:
            for sh in SHIFTS_PX:
                n_tot = n_rec = n_any = 0
                for rep in range(REPEATS):
                    rng = np.random.default_rng(int(1000 * snr + 10 * sh + rep))
                    g, truths, B = _inject(field, bid, rng, snr, sh)
                    res = D.find_candidates(g, bands=[bid])
                    w = D.grid_wcs(field["manifest"]["grid"])
                    cands = []
                    for c in res["candidates"]:
                        if c["kind"] != "mover":
                            continue
                        xs, ys = [], []
                        for pos in c["positions"]:
                            x, y = w.wcs_world2pix([pos["ra"]], [pos["dec"]], 0)
                            xs.append(float(x[0])); ys.append(float(y[0]))
                        cands.append((float(np.mean(xs)), float(np.mean(ys)), xs, ys))
                    for t in truths:
                        n_tot += 1
                        tx = [t["x"] + t["vx"] * d for d in B.dt]; ty = [t["y"] + t["vy"] * d for d in B.dt]
                        hit = any(math.hypot(cx - t["x"], cy - t["y"]) <= 1.5 for cx, cy, _, _ in cands)
                        good = any(math.hypot(cx - t["x"], cy - t["y"]) <= 1.5 and
                                   math.hypot((xs[-1] - xs[0]) - (tx[-1] - tx[0]), (ys[-1] - ys[0]) - (ty[-1] - ty[0])) <= 1.0
                                   for cx, cy, xs, ys in cands)
                        n_any += hit; n_rec += good
                rows.append({"snr_per_epoch": snr, "shift_px": sh, "shift_arcsec": sh * 3.0, "n": n_tot,
                             "recovered": n_rec, "rate": round(n_rec / n_tot, 2) if n_tot else None,
                             "position_only": n_any})
        out[label] = {"field": key, "band": bid, "baseline_candidates": len(base["candidates"]), "rows": rows}
    return out


def run():
    res = {"code_hash": code_hash(), "params": D.DEFAULT_PARAMS, "targets": target_part(), "injection": injection_part()}
    write_json(OUT_NAME, res, default=float)
    return res


def report(res):
    for key, o in res["targets"].items():
        print(f"{key:11s} {o['seconds']:5.1f}s bands {o['bands_searched']} target {o['target']} others {o['other_candidates']} "
              f"rejected {o['rejected']}")
    for label, o in res["injection"].items():
        print(f"injection {label} ({o['field']} {o['band']}): recovery rate, rows = per-epoch S/N, cols = total motion px {SHIFTS_PX}")
        for snr in SNR_LEVELS:
            r = [x for x in o["rows"] if x["snr_per_epoch"] == snr]
            print(f"   S/N {snr:4.1f}: " + "  ".join(f"{x['rate']:.2f} ({x['recovered']}/{x['n']})" for x in r))


def test_detect():
    res = run()
    report(res)
    t = res["targets"]
    assert t["wise-0855"]["target"] and t["wise-0855"]["target"]["kind"] == "mover" and "W" in t["wise-0855"]["target"]["bands"]
    assert t["eps-indi-b"]["target"] and t["eps-indi-b"]["target"]["kind"] == "mover"
    sparse = {(x["snr_per_epoch"], x["shift_px"]): x["rate"] for x in res["injection"]["sparse"]["rows"]}
    assert sparse[(12.0, 3.0)] >= 0.8


if __name__ == "__main__":
    test_detect()
