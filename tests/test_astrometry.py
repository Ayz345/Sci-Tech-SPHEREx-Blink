"""Validation of spx.astrometry (single-exposure positions for MPC/ADES drafts).

Run:  python tests/test_astrometry.py        (network: IRSA MOST + SIA2, JPL Horizons, Gaia; S3 cutouts are cached)
Writes handoff/results/astrometry_validation_v2.json (read by spx/mpc.py and /api/track).

Parts
  axes     X_SC/Y_SC/Z_SC frame: treating them as GCRS (ICRF axes, geocentric) must reproduce the header ground-track
           longitude SGT_LON_MIDPT; Earth-fixed or true-of-date axes must not.
  static   Gaia DR3 stars (G 12-17, proper motion to the exposure) in the WISE 0855 field, every 2nd exposure: free
           PSF fit vs Gaia -> residual scatter and median by S/N, fit error, pull width, per-axis floor.
  eros     (433) Eros: SPHEREx exposures found by IRSA MOST; prediction = JPL Horizons geocentric astrometric position
           moved to the spacecraft (X_SC, ICRF); fit with MOVER_ALLOWED_BITS; residuals overall, per band, per S/N,
           along / cross the motion; strict vs relaxed flag rule.
  wise0855 measure_track in the between_epochs regime; per-epoch weighted means vs the co-add two-PSF positions.
Timing is NOT validated: the along-track residual is reported, but one object's motion direction cannot separate a
time offset from a constant sky offset.
"""
from __future__ import annotations

import io
import json
import sys
import warnings
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
warnings.filterwarnings("ignore")

import astropy.units as u
import numpy as np
import pyvo
import requests
from astropy.coordinates import GCRS, ITRS, CartesianRepresentation, EarthLocation, SkyCoord
from astropy.io import ascii, fits
from astropy.time import Time

import spx
from spx import access
from spx.astrometry import MOVER_ALLOWED_BITS, TRAIL_LIMIT_ARCSEC, WCS_ERR_ARCSEC, fit_position, measure_track
from spx.coadd import assign_epochs, half_px_for, target_at
from spx.photometry import _move
from spx.targets import target_coord

OUT = ROOT / "handoff" / "results" / "astrometry_validation_v2.json"
CACHE = ROOT / "cache" / "astrometry"
CACHE.mkdir(parents=True, exist_ok=True)
N_EROS = 450
TIMING_NOTE = 'Not verified. (433) Eros (motion PA 59-92 deg, 0.017-0.043 arcsec/s): along-track residual -0.15 arcsec, -4 s if it were all timing. (31) Euphrosyne (0.002-0.010 arcsec/s, different direction): along-track +0.01 +- 0.02 arcsec, i.e. +1.3 +- 2.3 s, but cross-track +0.14 arcsec. Object-dependent systematics of ~0.15 arcsec remain unexplained; a time offset larger than ~5 s is not supported. obsTime = DATE-AVG, uncorrected; rmsTime is not given. SPHEREx observes at ~90 deg solar elongation, so no retrograde motion is available to separate the two.'
rs = lambda v: float(1.4826 * np.median(np.abs(np.asarray(v) - np.median(v))))  # noqa: E731


# ------------------------------------------------------------------ axes
def image_header(s3_key):
    url = access.S3_BASE + s3_key
    buf = requests.get(url, headers={"Range": "bytes=0-60000"}, timeout=120).content
    h0 = fits.Header.fromstring(buf[:buf.index(b"END" + b" " * 77) + 80].decode("ascii"))
    off = ((len(h0.tostring()) + 2879) // 2880) * 2880
    rest = buf[off:]
    return fits.Header.fromstring(rest[:rest.index(b"END" + b" " * 77) + 80].decode("ascii"))


def axes_test(s3_keys):
    rows = []
    for k in s3_keys:
        h = image_header(k)
        t = Time(h["MJD-AVG"], format="mjd", scale="utc")
        r = np.array([h["X_SC"], h["Y_SC"], h["Z_SC"]])
        g = GCRS(CartesianRepresentation(r * u.km), obstime=t).transform_to(ITRS(obstime=t))
        lon_gcrs = EarthLocation.from_geocentric(g.cartesian.x, g.cartesian.y, g.cartesian.z).lon.deg
        lon_itrs = EarthLocation.from_geocentric(*(r * u.km)).lon.deg
        lon_tod = (np.degrees(np.arctan2(r[1], r[0])) - t.sidereal_time("mean", "greenwich").deg)
        d = lambda a: float((a - h["SGT_LON_MIDPT"] + 180) % 360 - 180)  # noqa: E731
        rows.append({"s3_key": k, "frame": h["XYZ_SC_SYSTEM"], "dlon_gcrs_deg": d(lon_gcrs), "dlon_earth_fixed_deg": d(lon_itrs),
                     "dlon_true_of_date_deg": d(lon_tod), "altitude_km": float(np.linalg.norm(r) - 6378.137)})
    return {"conclusion": "X_SC/Y_SC/Z_SC are geocentric with ICRF (GCRS, J2000) axes", "files": rows,
            "max_abs_dlon_gcrs_deg": max(abs(r["dlon_gcrs_deg"]) for r in rows)}


# ------------------------------------------------------------------ static stars
def gaia_rows(c, radius_deg, gmax=19.0):
    t = pyvo.dal.TAPService("https://gea.esac.esa.int/tap-server/tap").run_sync(
        "select ra, dec, pmra, pmdec, phot_g_mean_mag as g from gaiadr3.gaia_source where "
        f"1=CONTAINS(POINT('ICRS',ra,dec), CIRCLE('ICRS',{c.ra.deg},{c.dec.deg},{radius_deg})) and phot_g_mean_mag < {gmax}").to_table()
    sc = SkyCoord(np.array(t["ra"]) * u.deg, np.array(t["dec"]) * u.deg, pm_ra_cosdec=np.nan_to_num(np.array(t["pmra"])) * u.mas / u.yr,
                  pm_dec=np.nan_to_num(np.array(t["pmdec"])) * u.mas / u.yr, obstime=Time(2016.0, format="jyear"))
    return sc, np.array(t["g"]), np.isfinite(np.array(t["pmra"]))


def static_test():
    c = target_at(target_coord("wise-0855"), 61000.0)
    refs = access.find_images(c.ra.deg, c.dec.deg)
    cuts, _ = access.fetch_cutouts(refs, c.ra.deg, c.dec.deg, half_px=half_px_for(7.0))
    allg, g, haspm = gaia_rows(c, 0.06)
    sel = np.nonzero((g > 12) & (g < 17) & haspm)[0]
    sel = np.random.default_rng(0).choice(sel, min(25, len(sel)), replace=False)
    rows = []
    for cu in cuts[::2]:
        nb = _move(allg, cu.mjd)
        for i in sel:
            others = nb[np.arange(len(nb)) != i]
            f = fit_position(cu, nb[i], others[others.separation(nb[i]).arcsec < 25])
            if f is None or f["flagged"] or f["saturated"]:
                continue
            rows.append(((f["ra"] - nb[i].ra.deg) * np.cos(np.radians(nb[i].dec.deg)) * 3600, (f["dec"] - nb[i].dec.deg) * 3600,
                         f["sig_ra_arcsec"], f["sig_dec_arcsec"], f["snr"]))
    a = np.array(rows)
    bins = []
    for lo, hi in ((5, 10), (10, 20), (20, 50), (50, 1e9)):
        m = (a[:, 4] >= lo) & (a[:, 4] < hi)
        if m.sum() < 10:
            continue
        bins.append({"snr": [lo, hi if hi < 1e9 else None], "n": int(m.sum()),
                     "median_dra": float(np.median(a[m, 0])), "median_ddec": float(np.median(a[m, 1])),
                     "scatter_ra": rs(a[m, 0]), "scatter_dec": rs(a[m, 1]),
                     "fit_err_ra": float(np.median(a[m, 2])), "fit_err_dec": float(np.median(a[m, 3])),
                     "pull_ra": rs(a[m, 0] / a[m, 2]), "pull_dec": rs(a[m, 1] / a[m, 3])})
    top = [b for b in bins if b["snr"][0] >= 20]
    floor = [float(np.sqrt(max(0.0, np.mean([b["scatter_ra"] ** 2 - b["fit_err_ra"] ** 2 for b in top])))),
             float(np.sqrt(max(0.0, np.mean([b["scatter_dec"] ** 2 - b["fit_err_dec"] ** 2 for b in top]))))]
    return {"field": "wise-0855", "n_fits": len(a), "median_dra": float(np.median(a[:, 0])), "median_ddec": float(np.median(a[:, 1])),
            "by_snr": bins, "floor_arcsec": floor, "floor_used_arcsec": WCS_ERR_ARCSEC}


# ------------------------------------------------------------------ Eros
def most_table(number="433"):
    p = CACHE / f"most_{number}.tbl"
    if not p.exists():
        r = requests.get("https://irsa.ipac.caltech.edu/cgi-bin/MOST/nph-most", params={
            "catalog": "spherex", "input_type": "name_input", "obj_name": number, "obs_begin": "2025 05 01",
            "obs_end": "2026 10 31", "output_mode": "Brief"}, timeout=600)
        p.write_bytes(r.content)
    return ascii.read(str(p), format="ipac")


def horizons_geocentric(command, mjds):
    out = []
    jd = [f"{m + 2400000.5:.9f}" for m in mjds]
    for k in range(0, len(jd), 50):
        r = requests.get("https://ssd.jpl.nasa.gov/api/horizons.api", params={
            "format": "json", "COMMAND": f"'{command}'", "EPHEM_TYPE": "OBSERVER", "CENTER": "'500@399'",
            "TLIST": " ".join(f"'{x}'" for x in jd[k:k + 50]), "TIME_TYPE": "UT", "QUANTITIES": "'1,20'",
            "ANG_FORMAT": "DEG", "EXTRA_PREC": "YES", "CSV_FORMAT": "YES", "OBJ_DATA": "NO"}, timeout=180)
        body = r.json()["result"].split("$$SOE")[1].split("$$EOE")[0].strip().splitlines()
        for line in body:
            nums = [x for x in (y.strip() for y in line.split(",")[1:]) if x not in ("", "*", "C", "N", "A", "m")]
            out.append((float(nums[0]), float(nums[1]), float(nums[2]) * 1.495978707e8))
    return np.array(out)


def topocentric(ra, dec, d_km, sc_km):
    v = d_km * SkyCoord(ra * u.deg, dec * u.deg).cartesian.xyz.value - sc_km
    return SkyCoord(np.degrees(np.arctan2(v[1], v[0])) % 360, np.degrees(np.arcsin(v[2] / np.linalg.norm(v))), unit="deg")


def eros_test(n=N_EROS, dt_s=30.0, number="433", name="(433) Eros"):
    t = most_table(number)
    rows = t[np.sort(np.random.default_rng(7).choice(len(t), min(n, len(t)), replace=False))]
    mjd = np.array([0.5 * (float(r["mjd_start"]) + float(r["mjd_end"])) for r in rows])
    e0, em, ep = (horizons_geocentric(f"{number};", mjd + k * dt_s / 86400) for k in (0, -1, 1))

    def one(i):
        r = rows[i]
        uri, det = str(r["uri"]), int(str(r["productid"])[1:])
        ref = access.ImageRef(obs_id=str(r["observationid"]), detector=det, collection="spherex_dr1",
                              s3_key=uri.split("ibe/data/spherex/")[1], access_url="https://irsa.ipac.caltech.edu/" + uri,
                              t_min=float(r["mjd_start"]), t_max=float(r["mjd_end"]),
                              em_min_um=access.DETECTOR_BANDS_UM[det][0], em_max_um=access.DETECTOR_BANDS_UM[det][1],
                              bandpass=f"SPHEREx-D{det}", exptime_s=float(r["exposure_time"]), s_region="")
        cuts, _ = access.fetch_cutouts([ref], e0[i][0], e0[i][1], half_px=20, workers=1)
        if not cuts:
            return None
        cu = cuts[0]
        sc, vsc = np.array(cu.sc_pos_km), np.array(cu.sc_vel_kms)
        p0, pm, pp = topocentric(*e0[i], sc), topocentric(*em[i], sc - vsc * dt_s), topocentric(*ep[i], sc + vsc * dt_s)
        cosd = np.cos(np.radians(p0.dec.deg))
        rate = np.array([(pp.ra.deg - pm.ra.deg) * cosd, pp.dec.deg - pm.dec.deg]) * 3600 / (2 * dt_s)
        speed = float(np.hypot(*rate))
        al = rate / speed
        out = {"obs_id": ref.obs_id, "detector": det, "mjd": float(mjd[i]), "finast": cu.finast, "speed": speed,
               "trail": speed * float(cu.exptime_s or 113.58),
               "parallax": float(p0.separation(SkyCoord(e0[i][0] * u.deg, e0[i][1] * u.deg)).arcsec)}
        for name, bits in (("strict", 0), ("relaxed", MOVER_ALLOWED_BITS)):
            f = fit_position(cu, p0, None, allow_bits=bits)
            if f is None:
                out[name] = None
                continue
            d = np.array([(f["ra"] - p0.ra.deg) * cosd * 3600, (f["dec"] - p0.dec.deg) * 3600])
            out[name] = {"d": d.tolist(), "along": float(d @ al), "cross": float(d @ np.array([-al[1], al[0]])),
                         "snr": f["snr"], "ok": (not f["flagged"]) and (not f["saturated"]) and f["snr"] >= 5}
        return out

    with ThreadPoolExecutor(16) as ex:
        res = [r for r in ex.map(one, range(len(rows))) if r is not None]
    good = [r for r in res if r["finast"] == 0]
    acc = [r for r in good if r["relaxed"] and r["relaxed"]["ok"]]
    strict = [r for r in good if r["strict"] and r["strict"]["ok"]]
    gained = [r for r in acc if not (r["strict"] and r["strict"]["ok"])]

    def summ(rr, key="relaxed"):
        if len(rr) < 3:
            return {"n": len(rr)}
        d = np.array([r[key]["d"] for r in rr])
        big = np.sort(np.hypot(d[:, 0], d[:, 1]))[-3:]
        return {"n": len(rr), "median_dra_arcsec": float(np.median(d[:, 0])), "median_ddec_arcsec": float(np.median(d[:, 1])),
                "scatter_ra_arcsec": rs(d[:, 0]), "scatter_dec_arcsec": rs(d[:, 1]),
                "median_along_track_arcsec": float(np.median([r[key]["along"] for r in rr])),
                "median_cross_track_arcsec": float(np.median([r[key]["cross"] for r in rr])),
                "largest_residuals_arcsec": [round(float(x), 2) for x in big]}

    s = summ(acc)
    sp = np.array([r["speed"] for r in acc])
    along = np.array([r["relaxed"]["along"] for r in acc])
    dt_est = float(np.median(along) / np.median(sp)) if len(acc) else None
    dt_err = float(1.2533 * rs(along) / np.sqrt(len(acc)) / np.median(sp)) if len(acc) > 2 else None
    return {"object": name, "speed_range_arcsec_per_s": [float(sp.min()), float(sp.max())] if len(acc) else None,
            "time_offset_if_all_along_track_s": [dt_est, dt_err], "prediction": "JPL Horizons geocentric astrometric RA/Dec + range, moved to the "
            "spacecraft position X_SC (ICRF); exposures from IRSA MOST (catalog=spherex)",
            "n_total": len(res), "n_finast0": len(good), "n_accepted": len(acc), **s,
            "strict_rule": summ(strict, "strict"), "gained_by_mover_bits": summ(gained),
            "per_band": {f"D{d}": summ([r for r in acc if r["detector"] == d]) for d in range(1, 7)},
            "per_snr": {f"{lo}-{hi}": summ([r for r in acc if lo <= r["relaxed"]["snr"] < hi]) for lo, hi in ((5, 10), (10, 20), (20, 40), (40, 1000))},
            "parallax_range_arcsec": [float(min(r["parallax"] for r in acc)), float(max(r["parallax"] for r in acc))],
            "trail_range_arcsec": [float(min(r["trail"] for r in acc)), float(max(r["trail"] for r in acc))]}


# ------------------------------------------------------------------ WISE 0855
def wise_test():
    from test_coadd_wise0855 import gaia_stars
    det = json.loads((ROOT / "handoff" / "results" / "wise0855_detection_v2.json").read_text(encoding="utf-8"))
    E = det["epochs"]
    marks = [{"ra": E[0]["ra"], "dec": E[0]["dec"], "mjd": E[0]["mjd"]}, {"ra": E[2]["ra"], "dec": E[2]["dec"], "mjd": E[2]["mjd"]}]
    c = target_at(target_coord("wise-0855"), 61000.0)
    refs = access.find_images(c.ra.deg, c.dec.deg)
    _, ep = access.epochs_for(refs)
    cuts, _ = access.fetch_cutouts(refs, c.ra.deg, c.dec.deg, half_px=half_px_for(7.0))
    cuts = [x for x in cuts if 4.3 <= x.wavelength_um <= 5.0]
    rows, info = measure_track(cuts, marks, neighbors=gaia_stars(c, 0.07, gmax=19.0))
    lab = assign_epochs([r["mjd_mid"] for r in rows], ep)
    per = []
    for e, Ee in zip(sorted(set(lab)), E):
        q = [r for r, l in zip(rows, lab) if l == e and r["ra"] is not None and np.isfinite(r["rms_ra"] or np.nan)
             and "blended" not in r["reason"] and "degenerate" not in r["reason"]
             and min(r["snr_fit"], r["snr_calibrated"] or r["snr_fit"]) >= 3]
        item = {"epoch": int(e), "date": Ee["date"], "n_exposures": int(sum(l == e for l in lab)),
                "n_accepted": int(sum(r["accepted"] for r, l in zip(rows, lab) if l == e)), "n_snr_ge_3_unblended": len(q)}
        if len(q) >= 2:
            w = np.array([1 / r["rms_ra"] ** 2 for r in q]); wd = np.array([1 / r["rms_dec"] ** 2 for r in q])
            m = SkyCoord(np.sum(w * [r["ra"] for r in q]) / w.sum(), np.sum(wd * [r["dec"] for r in q]) / wd.sum(), unit="deg")
            item["sep_from_coadd_arcsec"] = float(m.separation(SkyCoord(Ee["ra"], Ee["dec"], unit="deg")).arcsec)
            item["expected_arcsec"] = float(np.hypot(np.hypot(1 / np.sqrt(w.sum()), 1 / np.sqrt(wd.sum())),
                                                     np.hypot(Ee["err_ra_arcsec"], Ee["err_dec_arcsec"])))
        per.append(item)
    return {"regime": info["regime"], "n_exposures": len(rows), "n_accepted": int(sum(r["accepted"] for r in rows)),
            "per_epoch": per, "note": "too faint per exposure for MPC-grade positions; epoch means agree with the co-add"}


def run():
    eros = eros_test()
    keys = [str(r["uri"]).split("ibe/data/spherex/")[1] for r in most_table("433")[:3]]
    res = {"frame_code_hash": spx.frame_code_hash(), "code_hash": spx.code_hash(), "object": "(433) Eros",
           "wcs_floor_arcsec": WCS_ERR_ARCSEC, "trail_limit_arcsec": TRAIL_LIMIT_ARCSEC,
           "timing": {"verified": False, "obs_time": "DATE-AVG (header mid-exposure, UTC), not corrected",
                      "along_track_median_arcsec": eros.get("median_along_track_arcsec"),
                      "note": TIMING_NOTE},
           "axes": axes_test(keys), "static_stars": static_test(), "eros": eros, "wise0855": wise_test()}
    OUT.write_text(json.dumps(res, indent=1, default=float), encoding="utf-8", newline="\n")
    return res


def test_astrometry():
    res = run()
    e, s, a, w = res["eros"], res["static_stars"], res["axes"], res["wise0855"]
    print(f"axes: |dlon| GCRS <= {a['max_abs_dlon_gcrs_deg']:.3f} deg; Earth-fixed {[round(r['dlon_earth_fixed_deg'], 1) for r in a['files']]}; "
          f"true-of-date {[round(r['dlon_true_of_date_deg'], 2) for r in a['files']]}")
    print(f"static ({s['n_fits']} fits): median {s['median_dra']:+.3f}/{s['median_ddec']:+.3f}\"; floor {s['floor_arcsec'][0]:.2f}/{s['floor_arcsec'][1]:.2f}\"")
    for b in s["by_snr"]:
        print(f"   S/N {b['snr']}: n={b['n']} scatter {b['scatter_ra']:.2f}/{b['scatter_dec']:.2f} fit err {b['fit_err_ra']:.2f}/{b['fit_err_dec']:.2f} pull {b['pull_ra']:.2f}/{b['pull_dec']:.2f}")
    print(f"Eros: {e['n_accepted']}/{e['n_total']} accepted; median {e['median_dra_arcsec']:+.3f}/{e['median_ddec_arcsec']:+.3f}\"; "
          f"scatter {e['scatter_ra_arcsec']:.2f}/{e['scatter_dec_arcsec']:.2f}\"; along {e['median_along_track_arcsec']:+.3f}\" cross {e['median_cross_track_arcsec']:+.3f}\"; "
          f"strict n={e['strict_rule']['n']}, gained n={e['gained_by_mover_bits']['n']} scatter {e['gained_by_mover_bits'].get('scatter_ra_arcsec', 0):.2f}")
    for p in w["per_epoch"]:
        print(f"WISE 0855 epoch {p['epoch']}: accepted {p['n_accepted']}/{p['n_exposures']}, mean vs co-add {p.get('sep_from_coadd_arcsec', float('nan')):.2f}\" (expected {p.get('expected_arcsec', float('nan')):.2f}\")")
    assert a["max_abs_dlon_gcrs_deg"] < 0.2 and all(abs(r["dlon_earth_fixed_deg"]) > 5 for r in a["files"])
    assert e["n_accepted"] >= 100 and abs(e["median_dra_arcsec"]) < 0.3 and abs(e["median_ddec_arcsec"]) < 0.3
    assert e["scatter_ra_arcsec"] < 0.4 and e["scatter_dec_arcsec"] < 0.4
    assert abs(s["median_dra"]) < 0.15 and abs(s["median_ddec"]) < 0.15
    for p in w["per_epoch"]:
        if "sep_from_coadd_arcsec" in p and p["n_snr_ge_3_unblended"] >= 4:
            assert p["sep_from_coadd_arcsec"] < 3 * p["expected_arcsec"]


def second_object(number="31", name="(31) Euphrosyne", n=350):
    """Same validation on a second asteroid with a different motion direction and speed; merged into the JSON."""
    res = json.loads(OUT.read_text(encoding="utf-8"))
    res["second_object"] = eros_test(n=n, number=number, name=name)
    OUT.write_text(json.dumps(res, indent=1, default=float), encoding="utf-8", newline=chr(10))
    return res["second_object"]


if __name__ == "__main__":
    if "--object" in sys.argv:
        o = second_object()
        print(f"{o['object']}: {o['n_accepted']}/{o['n_total']} accepted; median {o['median_dra_arcsec']:+.3f}/{o['median_ddec_arcsec']:+.3f}\"; "
              f"scatter {o['scatter_ra_arcsec']:.2f}/{o['scatter_dec_arcsec']:.2f}\"; along {o['median_along_track_arcsec']:+.3f}\" cross "
              f"{o['median_cross_track_arcsec']:+.3f}\"; time offset if along-track {o['time_offset_if_all_along_track_s']}")
    else:
        test_astrometry()
