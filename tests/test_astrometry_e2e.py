"""End-to-end check of the MPC draft chain on a real (433) Eros pass.

Run:  python tests/test_astrometry_e2e.py      (network: MOST table cached by test_astrometry, Horizons, Gaia; S3 cached)
Writes handoff/results/astrometry_e2e_v2.json and the draft handoff/results/astrometry_e2e_draft_v2.psv.

Chain: two user marks on one pass (Horizons position at two exposures + 3" random click error)
-> spx.astrometry.measure_track (cutouts centred on the mark model, Gaia neighbours) -> spx.mpc.write_ades_psv
-> validate_psv -> RA/Dec read back from the file and compared with Horizons at the file's obsTime, moved to the
spacecraft position written in the same file (pos1..3). Also: same-time positions on two detectors (D1/D4, D2/D5,
D3/D6 see the same sky through the dichroic), whose difference is free of timing and ephemeris errors.
"""
from __future__ import annotations

import json
import sys
import warnings
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
warnings.filterwarnings("ignore")

import astropy.units as u
import numpy as np
from astropy.coordinates import SkyCoord
from astropy.time import Time

from spx import access, code_hash, frame_code_hash, mpc
from spx.astrometry import measure_track, track_model
from test_astrometry import gaia_rows, horizons_geocentric, most_table, topocentric

OUT = ROOT / "handoff" / "results" / "astrometry_e2e_v2.json"
DRAFT = ROOT / "handoff" / "results" / "astrometry_e2e_draft_v2.psv"


def run():
    t = most_table("433")
    mjd_all = np.array([0.5 * (float(r["mjd_start"]) + float(r["mjd_end"])) for r in t])
    best = max(range(len(t)), key=lambda i: np.sum(np.abs(mjd_all - mjd_all[i]) < 1.5))  # densest 3-day pass
    win = np.nonzero(np.abs(mjd_all - mjd_all[best]) < 1.5)[0]
    rows, mjd = t[win], mjd_all[win]
    o = np.argsort(mjd)
    rows, mjd = rows[o], mjd[o]
    eph = horizons_geocentric("433;", mjd)
    refs = []
    for r in rows:
        uri, det = str(r["uri"]), int(str(r["productid"])[1:])
        refs.append(access.ImageRef(obs_id=str(r["observationid"]), detector=det, collection="spherex_dr1",
                                    s3_key=uri.split("ibe/data/spherex/")[1], access_url="https://irsa.ipac.caltech.edu/" + uri,
                                    t_min=float(r["mjd_start"]), t_max=float(r["mjd_end"]),
                                    em_min_um=access.DETECTOR_BANDS_UM[det][0], em_max_um=access.DETECTOR_BANDS_UM[det][1],
                                    bandpass=f"SPHEREx-D{det}", exptime_s=float(r["exposure_time"]), s_region=""))
    rng = np.random.default_rng(3)
    marks = []
    for i in (1, len(rows) - 2):  # two user clicks near the ends of the pass, 3" click error
        c = SkyCoord(eph[i][0] * u.deg, eph[i][1] * u.deg).spherical_offsets_by(*(rng.normal(0, 3, 2) * u.arcsec))
        marks.append({"ra": float(c.ra.deg), "dec": float(c.dec.deg), "mjd": float(mjd[i])})
    pos, _ = track_model(marks)

    def cut(k):
        p = pos(mjd[k])
        cs, _ = access.fetch_cutouts([refs[k]], p.ra.deg, p.dec.deg, half_px=20, workers=1)
        return cs[0] if cs else None

    with ThreadPoolExecutor(16) as ex:
        cuts = [c for c in ex.map(cut, range(len(refs))) if c is not None]
    nb, g, _ = gaia_rows(pos(float(np.median(mjd))), 0.3)
    det_rows, info = measure_track(cuts, marks, neighbors=nb[g < 19])
    acc = [d for d in det_rows if d["accepted"]]

    psv = mpc.write_ades_psv(det_rows, {"submitter": "J. Doe", "tool_version": "test_astrometry_e2e"})
    errs = mpc.validate_psv(psv)
    DRAFT.write_text(psv, encoding="utf-8", newline=chr(10))

    lines = [ln for ln in psv.splitlines() if ln and not ln.startswith(("#", "!"))]
    hdr = [h.strip() for h in lines[0].split("|")]
    recs = [dict(zip(hdr, [x.strip() for x in ln.split("|")])) for ln in lines[1:]]
    obs_mjd = np.array([Time(r["obsTime"].rstrip("Z"), scale="utc").mjd for r in recs])
    e2 = horizons_geocentric("433;", obs_mjd)
    res = []
    for r, e in zip(recs, e2):
        p = topocentric(*e, np.array([float(r["pos1"]), float(r["pos2"]), float(r["pos3"])]))
        ra, dec = float(r["ra"]), float(r["dec"])
        res.append(((ra - p.ra.deg) * np.cos(np.radians(dec)) * 3600, (dec - p.dec.deg) * 3600, float(r["rmsRA"]), float(r["rmsDec"])))
    a = np.array(res)

    by = defaultdict(list)
    for d in acc:
        by[d["obs_time"]].append(d)
    pairs = []
    for ds in by.values():
        if len(ds) == 2:
            p1, p2 = sorted(ds, key=lambda d: d["detector"])
            pairs.append(((p1["ra"] - p2["ra"]) * np.cos(np.radians(p1["dec"])) * 3600, (p1["dec"] - p2["dec"]) * 3600,
                          float(np.hypot(p1["rms_ra"], p2["rms_ra"])), float(np.hypot(p1["rms_dec"], p2["rms_dec"]))))
    pa = np.array(pairs)
    summary = {"frame_code_hash": frame_code_hash(), "code_hash": code_hash(), "object": "(433) Eros",
               "pass_mjd": [float(mjd.min()), float(mjd.max())], "marks": marks, "regime": info["regime"],
               "n_exposures": len(det_rows), "n_accepted": len(acc), "n_records_in_draft": len(recs), "validate_errors": errs,
               "left_out_of_draft": {str(k): v for k, v in mpc.select_for_ades(det_rows).items()},
               "residual_vs_horizons": {
                   "median_dra_arcsec": float(np.median(a[:, 0])), "median_ddec_arcsec": float(np.median(a[:, 1])),
                   "rms_ra_arcsec": float(np.sqrt(np.mean(a[:, 0] ** 2))), "rms_dec_arcsec": float(np.sqrt(np.mean(a[:, 1] ** 2))),
                   "max_arcsec": float(np.max(np.hypot(a[:, 0], a[:, 1]))),
                   "median_file_rms_arcsec": [float(np.median(a[:, 2])), float(np.median(a[:, 3]))],
                   "frac_within_3_sigma": [float(np.mean(np.abs(a[:, 0] / a[:, 2]) < 3)), float(np.mean(np.abs(a[:, 1] / a[:, 3]) < 3))]},
               "same_time_detector_pairs": {
                   "n": len(pa), "median_diff_arcsec": [float(np.median(pa[:, 0])), float(np.median(pa[:, 1]))] if len(pa) else None,
                   "rms_diff_arcsec": [float(np.sqrt(np.mean(pa[:, 0] ** 2))), float(np.sqrt(np.mean(pa[:, 1] ** 2)))] if len(pa) else None,
                   "expected_arcsec": [float(np.median(pa[:, 2])), float(np.median(pa[:, 3]))] if len(pa) else None}}
    OUT.write_text(json.dumps(summary, indent=1), encoding="utf-8", newline=chr(10))
    return summary


def test_e2e():
    s = run()
    r, p = s["residual_vs_horizons"], s["same_time_detector_pairs"]
    print(f"pass MJD {s['pass_mjd'][0]:.3f}-{s['pass_mjd'][1]:.3f}: {s['n_exposures']} exposures, {s['n_accepted']} accepted, "
          f"{s['n_records_in_draft']} records in the ADES draft; validate_psv errors: {s['validate_errors']}")
    print(f"draft vs Horizons (spacecraft): median {r['median_dra_arcsec']:+.2f}/{r['median_ddec_arcsec']:+.2f}\", rms {r['rms_ra_arcsec']:.2f}/"
          f"{r['rms_dec_arcsec']:.2f}\", max {r['max_arcsec']:.2f}\"; file rms {r['median_file_rms_arcsec'][0]:.2f}/{r['median_file_rms_arcsec'][1]:.2f}\"; "
          f"within 3 sigma {r['frac_within_3_sigma']}")
    print(f"{p['n']} same-time detector pairs: rms difference {p['rms_diff_arcsec']}, expected {p['expected_arcsec']}")
    assert not s["validate_errors"], s["validate_errors"]
    assert s["n_records_in_draft"] >= 3
    assert min(r["frac_within_3_sigma"]) == 1.0
    if p["n"] >= 5:
        assert all(x <= 1.5 * e for x, e in zip(p["rms_diff_arcsec"], p["expected_arcsec"]))


if __name__ == "__main__":
    test_e2e()
