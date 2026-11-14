"""End-to-end MPC draft on a real asteroid (network; SPX_NETWORK=1):

    marks on one (433) Eros pass -> POST /api/track -> GET /api/report?format=ades -> every position in the draft
    compared with JPL Horizons as seen from the spacecraft position written in the same line -> the same draft
    through the IAU tools (psvtoxml + valsubmit).

Uses the co-add session's helpers from tests/test_astrometry.py (IRSA MOST list, Horizons API, topocentric shift).
Writes the numbers to cache/acceptance/track_e2e_eros.json.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import numpy as np
import pytest

pytestmark = pytest.mark.skipif(os.environ.get("SPX_NETWORK") != "1", reason="set SPX_NETWORK=1 (IRSA, S3, Horizons)")

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
MIN_IN_PASS = 5


def _pass_with_marks():
    """A (433) Eros pass: the detector with most MOST exposures within one day; marks = Horizons positions seen
    from the spacecraft at the first and last of them."""
    sys.path.insert(0, str(HERE))
    import test_astrometry as TA
    from spx import access
    t = TA.most_table("433")
    mjd = np.array([0.5 * (float(r["mjd_start"]) + float(r["mjd_end"])) for r in t])
    det = np.array([int(str(r["productid"])[1:]) for r in t])
    best = None
    for i in np.argsort(mjd):
        for d in range(1, 7):
            sel = np.flatnonzero((det == d) & (mjd >= mjd[i]) & (mjd < mjd[i] + 1.0))
            if len(sel) >= MIN_IN_PASS and (best is None or len(sel) > len(best[1])):
                best = (d, sel)
        if best and len(best[1]) >= 12:
            break
    assert best, "no Eros pass with enough exposures in one band"
    d, sel = best
    sel = sel[np.argsort(mjd[sel])]
    marks = []
    for i in (sel[0], sel[-1]):
        r = t[i]
        uri = str(r["uri"])
        ref = access.ImageRef(obs_id=str(r["observationid"]), detector=d, collection="spherex_dr1",
                              s3_key=uri.split("ibe/data/spherex/")[1], access_url="https://irsa.ipac.caltech.edu/" + uri,
                              t_min=float(r["mjd_start"]), t_max=float(r["mjd_end"]),
                              em_min_um=access.DETECTOR_BANDS_UM[d][0], em_max_um=access.DETECTOR_BANDS_UM[d][1],
                              bandpass=f"SPHEREx-D{d}", exptime_s=float(r["exposure_time"]), s_region="")
        geo = TA.horizons_geocentric("433;", [mjd[i]])[0]
        cuts, _ = access.fetch_cutouts([ref], geo[0], geo[1], half_px=20, workers=1)
        assert cuts, "mark exposure not readable"
        top = TA.topocentric(*geo, np.array(cuts[0].sc_pos_km))
        marks.append({"ra": float(top.ra.deg), "dec": float(top.dec.deg), "mjd": float(cuts[0].mjd)})
    return f"D{d}", marks, TA


def _psv_rows(txt):
    kw, rows = None, []
    for ln in txt.splitlines():
        if ln.startswith(("#", "!")) or not ln.strip():
            continue
        toks = [x.strip() for x in ln.split("|")]
        if kw is None:
            kw = toks
        else:
            rows.append(dict(zip(kw, toks)))
    return rows


def test_eros_track_to_ades_draft(tmp_path):
    from astropy.time import Time
    from fastapi.testclient import TestClient
    from server.app import app
    from spx import mpc
    band, marks, TA = _pass_with_marks()
    with TestClient(app) as c:
        r = c.post("/api/track", json={"band": band, "marks": marks})
        assert r.status_code == 200, r.text
        tr = r.json()
        assert tr["regime"] == "within_visit"
        r = c.get("/api/report", params={"id": tr["report_id"], "format": "ades", "submitter": "I. M. Tester"})
        assert r.status_code == 200, r.text
        psv = r.text
    rows = _psv_rows(psv)
    assert len(rows) >= mpc.MIN_DETECTIONS
    # logSNR = log10(snr_image), the S/N of the source in the image, row by row (matched on the obsID remark).
    # This pass (2025W29-W30, D3) gave 2.11-2.19 on 14 Nov; the quality session's 2.06-2.09 is its 52-exposure
    # 2025W28 pass. The old rule, min(snr_fit, snr_calibrated), is lower on every row (the fit S/N).
    assert all("snr_image" in m and "snr_variance" in m for m in tr["measurements"])
    by_id = {m["obs_id"]: m for m in tr["measurements"] if m.get("in_ades")}
    log_snr = []
    for x in rows:
        m = by_id[x["remarks"].split("obsID ")[1].split()[0]]
        assert x["logSNR"] == f"{np.log10(m['snr_image']):.2f}", (x["logSNR"], m)
        assert float(x["logSNR"]) > np.log10(min(m["snr_fit"], m["snr_calibrated"]))
        log_snr.append(float(x["logSNR"]))
    mjds = [float(Time(x["obsTime"][:-1], format="isot", scale="utc").mjd) for x in rows]
    geo = TA.horizons_geocentric("433;", mjds)
    d_ra, d_dec = [], []
    for x, g in zip(rows, geo):
        sc = np.array([float(x["pos1"]), float(x["pos2"]), float(x["pos3"])])
        p = TA.topocentric(*g, sc)
        cosd = np.cos(np.radians(p.dec.deg))
        d_ra.append((float(x["ra"]) - p.ra.deg) * cosd * 3600.0)
        d_dec.append((float(x["dec"]) - p.dec.deg) * 3600.0)
    d_ra, d_dec = np.array(d_ra), np.array(d_dec)
    sys.path.insert(0, str(HERE))
    import test_mpc as TM
    ok, msg = TM._official(psv, tmp_path)
    res = {"object": "(433) Eros", "band": band, "marks": marks, "regime": tr["regime"],
           "n_exposures_found": tr["n_exposures_found"], "n_measured": tr["n_measured"],
           "n_accepted": tr["n_accepted"], "n_in_draft": len(rows),
           "median_dra_arcsec": float(np.median(d_ra)), "median_ddec_arcsec": float(np.median(d_dec)),
           "max_abs_offset_arcsec": float(np.max(np.hypot(d_ra, d_dec))),
           "rms_ra_median_reported": float(np.median([float(x["rmsRA"]) for x in rows])),
           "log_snr_range": [min(log_snr), max(log_snr)],
           "official_valsubmit": msg, "schema_checked_on_server": mpc.SCHEMA_PASSED in psv}
    out = ROOT / "cache" / "acceptance" / "track_e2e_eros.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(res, indent=1), encoding="utf-8")
    (out.parent / "track_e2e_eros.psv").write_text(psv, encoding="utf-8", newline="\n")
    print(json.dumps(res, indent=1))
    assert ok, msg
    assert abs(res["median_dra_arcsec"]) < 0.5 and abs(res["median_ddec_arcsec"]) < 0.5
    assert res["max_abs_offset_arcsec"] < 2.0
