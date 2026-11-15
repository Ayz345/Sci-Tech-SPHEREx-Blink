"""API server tests, offline: SIA2 and S3 are replaced by a fake image list and synthetic cutouts, while the
real spx.coadd / spx.photometry code runs, so manifest -> band -> frame -> spectrum is exercised end to end.

    python -m pytest -q tests/test_server.py
"""
from __future__ import annotations

import json
import math

import numpy as np
import pytest
from astropy.wcs import WCS

from spx import access

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

RA0, DEC0 = 210.123, -35.456
EPOCH_MJD = (60800.0, 60990.0, 61170.0)


def _refs(ra, dec):
    out = []
    for e, t0 in enumerate(EPOCH_MJD):
        for d in range(1, 7):
            for i in range(4):
                t = t0 + i * 0.7 + d * 0.01
                out.append(access.ImageRef(f"E{e}_{i}", d, "spherex_dr1", f"fake/{e}/{d}/{i}.fits", "", t, t + 0.0013,
                                           *access.DETECTOR_BANDS_UM[d], f"SPHEREx-D{d}", 113.6, ""))
    return out


def _cutout(ref, ra, dec, half_px, layers, rng):
    n = 2 * half_px + 1
    w = WCS(naxis=2)
    w.wcs.ctype = ["RA---TAN", "DEC--TAN"]
    w.wcs.crval = [ra, dec]
    w.wcs.crpix = [half_px + 1, half_px + 1]
    w.wcs.cdelt = [-6.15 / 3600, 6.15 / 3600]
    yy, xx = np.mgrid[0:n, 0:n]
    img = rng.normal(0, 0.02, (n, n)) + 2.0 * np.exp(-((xx - half_px) ** 2 + (yy - half_px) ** 2) / 2.0)
    img[0, 0] = np.nan
    lo, hi = access.DETECTOR_BANDS_UM[ref.detector]
    wl = 0.5 * (lo + hi)
    return access.Cutout(
        image=img.astype(np.float32), flags=np.zeros((n, n), np.int32) if "FLAGS" in layers else None,
        variance=np.full((n, n), 4e-4, np.float32) if "VARIANCE" in layers else None, zodi=None, wcs=w,
        x0=500, y0=600, mjd=ref.mjd, detector=ref.detector, obs_id=ref.obs_id, wavelength_um=wl,
        bandwidth_um=wl / 40, wavelength_map=np.full((n, n), wl, np.float32),
        bandwidth_map=np.full((n, n), wl / 40, np.float32), target_xy=(half_px, half_px), s3_key=ref.s3_key)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from server import app as A
    from server import pipeline as P
    calls = {"sia": 0, "fetch": 0}

    def fake_find(ra, dec, *a, **k):
        calls["sia"] += 1
        if dec < -80:  # "no coverage" region for the test
            return []
        return _refs(ra, dec)

    def fake_fetch(refs, ra, dec, half_px=50, layers=("IMAGE", "FLAGS", "VARIANCE"), workers=32, progress=None,
                   stats=None, box_deg=None):
        calls["box_deg"] = box_deg
        calls.setdefault("box_by_ra", []).append((round(ra, 1), box_deg))
        calls["fetch"] += 1
        calls["layers"] = tuple(layers)
        rng = np.random.default_rng(len(refs))
        cuts = [_cutout(r, ra, dec, half_px, layers, rng) for r in refs]
        if stats is not None:
            stats.update({"n_refs": len(refs), "n_ok": len(cuts), "n_skipped": 0, "n_from_cache": 0, "bytes": 0,
                          "seconds": 0.0, "skip_reasons": {}})
        return cuts, 0

    monkeypatch.setattr(access, "find_images", fake_find)
    monkeypatch.setattr(access, "fetch_cutouts", fake_fetch)
    monkeypatch.setattr(access, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(A, "FIELDS_DIR", tmp_path / "fields")
    monkeypatch.setattr(P, "gaia_sources", lambda f, **k: [{"ra": RA0, "dec": DEC0, "pmra": 0.0, "pmdec": 0.0,
                                                             "g_mag": 12.0, "ref_epoch": 2016.0}])
    monkeypatch.setattr(A, "_spec_cache", A.OrderedDict())
    monkeypatch.setattr(A, "RATE_ON", False)            # request limits are tested on their own
    monkeypatch.setattr(A, "_rate_hits", {})
    regs = []

    def new_registry():
        regs.append(A.Registry())
        monkeypatch.setattr(A, "REG", regs[-1])
    new_registry()
    with TestClient(A.app) as c:
        c.calls = calls
        c.new_registry = new_registry
        yield c
    for r in regs:  # stop background builders before the fakes are removed
        r.close()


def _wait_idle(timeout=60):
    import time
    from server import app as A
    t0 = time.time()
    while time.time() - t0 < timeout:
        with A.REG.lock:
            busy = A.REG.urgent or A.REG.background or any(
                j.state == "running" for fs in A.REG.fields.values() for j in fs.jobs.values())
        if not busy:
            return
        time.sleep(0.05)
    raise AssertionError("registry did not become idle")


def _strict(r):
    return json.loads(r.content, parse_constant=lambda c: (_ for _ in ()).throw(AssertionError(f"non-finite {c}")))


def test_manifest_band_frame(client):
    r = client.get("/api/manifest", params={"ra": RA0, "dec": DEC0, "fov": 0.1})
    assert r.status_code == 200 and r.headers["access-control-allow-origin"] == "*"
    m = _strict(r)
    assert m["schema"] == 1 and m["key"] == "r210p1230_dm35p4560_n120"
    assert len(m["epochs"]) == 3 and [b["id"] for b in m["bands"]] == [f"D{d}" for d in range(1, 7)]
    assert all(b["n_exposures"] == [4, 4, 4] for b in m["bands"])
    assert m["grid"]["shape"] == [120, 120] and m["grid"]["ctype"] == ["RA---TAN", "DEC--TAN"]
    assert m["max_fov_deg"] > 0 and m["acknowledgement"].startswith("This publication makes use")

    r = client.get("/api/band", params={"key": m["key"], "band": "D3"})
    assert r.status_code == 200
    bj = _strict(r)
    assert bj["band"] == "D3" and [e["n_used"] for e in bj["epochs"]] == [4, 4, 4]
    assert bj["scale"]["vmax"] > bj["scale"]["vmin"]
    for kind in (None, "weight", "flagfrac"):
        p = {"key": m["key"], "band": "D3", "epoch": 1}
        if kind:
            p["kind"] = kind
        r = client.get("/api/frame", params=p)
        assert r.status_code == 200 and len(r.content) == 120 * 120 * 4
        a = np.frombuffer(r.content, "<f4").reshape(120, 120)
        if kind == "weight":
            assert np.nanmax(a) == 4
    img = np.frombuffer(client.get("/api/frame", params={"key": m["key"], "band": "D3", "epoch": 0}).content, "<f4")
    assert np.nanargmax(img) in {i * 120 + j for i in (59, 60) for j in (59, 60)}  # the star is in the middle
    assert client.calls["layers"] == ("IMAGE", "FLAGS")  # live path skips VARIANCE

    # same request again: no new SIA2 query, no new download (after the background bands have finished,
    # otherwise a background fetch can land between the two snapshots)
    _wait_idle()
    n_sia, n_fetch = client.calls["sia"], client.calls["fetch"]
    assert client.get("/api/manifest", params={"ra": RA0, "dec": DEC0, "fov": 0.1}).status_code == 200
    assert client.get("/api/band", params={"key": m["key"], "band": "D3"}).status_code == 200
    assert (client.calls["sia"], client.calls["fetch"]) == (n_sia, n_fetch)

    assert client.get("/api/frame", params={"key": m["key"], "band": "D3", "epoch": 7}).status_code == 404
    assert client.get("/api/frame", params={"key": m["key"], "band": "D3", "epoch": 0, "kind": "x"}).status_code == 400
    assert client.get("/api/band", params={"key": m["key"], "band": "D9"}).status_code == 404


def test_spectrum_and_gaia(client):
    m = client.get("/api/manifest", params={"ra": RA0, "dec": DEC0}).json()
    assert client.get("/api/band", params={"key": m["key"], "band": "D1"}).status_code == 200
    r = client.get("/api/spectrum", params={"key": m["key"], "ra": RA0, "dec": DEC0})
    assert r.status_code == 200
    s = _strict(r)
    assert set(s) >= {"meta", "points"} and "D1" in s["meta"]["bands_included"]
    assert s["points"] and all(p["band"] in s["meta"]["bands_included"] for p in s["points"])
    g = _strict(client.get("/api/gaia", params={"key": m["key"]}))
    assert g[0]["ref_epoch"] == 2016.0


def test_no_coverage_and_errors(client):
    r = client.get("/api/manifest", params={"ra": 10.0, "dec": -85.5})
    assert r.status_code == 200
    m = _strict(r)
    assert m["epochs"] == [] and m["bands"] == [] and "No SPHEREx images" in m["message"]
    assert client.get("/api/manifest", params={"ra": 10, "dec": 95}).status_code == 400
    assert client.get("/api/manifest", params={"ra": 10, "dec": 5, "fov": 5}).status_code == 400
    assert client.get("/api/manifest").status_code == 400
    assert client.get("/api/manifest", params={"target": "planet-x"}).status_code == 404
    assert client.get("/api/band", params={"key": "nonsense", "band": "D1"}).status_code == 404


def test_coordinate_forms_and_wrap(client):
    a = client.get("/api/manifest", params={"ra": "14:00:29.52", "dec": "-35:27:21.6", "fov": 0.1}).json()
    assert a["key"] == "r210p1230_dm35p4560_n120"
    b = client.get("/api/manifest", params={"ra": -0.5, "dec": 10}).json()
    assert b["key"].startswith("r359p5000_dp10p0000")


def test_key_roundtrip_reopens_after_restart(client, monkeypatch):
    from server import app as A
    m = client.get("/api/manifest", params={"ra": RA0, "dec": DEC0, "fov": 0.1}).json()
    client.get("/api/band", params={"key": m["key"], "band": "D2"})
    client.new_registry()  # a restarted server: only the disk is left
    r = client.get("/api/band", params={"key": m["key"], "band": "D2"})
    assert r.status_code == 200 and _strict(r)["band"] == "D2"
    k = A.parse_key(m["key"])
    assert (k["ra"], k["dec"]) == (210.123, -35.456) and m["key"].endswith("_n120")
    from server import pipeline as P
    assert P.grid_px(k["fov_deg"]) == 120 == m["grid"]["shape"][0]      # the key gives back the same grid


def test_resolve_named_target_offline(client):
    r = _strict(client.get("/api/resolve", params={"name": "Barnard's Star"}))
    assert r["target"] == "barnard" and math.isclose(r["pmdec"], 10362.394)
    r = _strict(client.get("/api/resolve", params={"name": "wise 0855"}))
    assert r["target"] == "wise-0855"


def test_health(client):
    h = _strict(client.get("/api/health"))
    assert h["ok"] and h["collections"] and h["live_layers"] == ["IMAGE", "FLAGS"]
    assert h["max_fov_deg"] >= 0.25 or __import__("os").environ.get("SPX_MAX_FOV_DEG")
    ml = h["memory_limits"]
    assert ml["cutout_sets"] >= 1 and ml["fields_in_memory"] >= ml["cutout_sets"] and ml["windows_per_field"] == 12
    av = h["astrometry_validation"]
    assert av["status"] in ("complete", "partial", "incomplete", "missing", "unreadable")
    assert set(av) >= {"file", "status", "missing_keys", "frame_code_hash", "same_frame_code"}


def test_json_safe():
    from server import pipeline as P
    assert P.dumps({"a": float("nan"), "b": [np.float32(np.inf), 1.5], "c": np.int64(3)}) == \
        '{"a": null, "b": [null, 1.5], "c": 3}'
    assert P.free_key(-0.00001, -0.0, 0.11666) == "r0p0000_dp0p0000_n140"


def test_free_position_with_proper_motion(client):
    pmra, pmdec = -3781.7, 769.5
    r = client.get("/api/manifest", params={"ra": RA0, "dec": DEC0, "pmra": pmra, "pmdec": pmdec,
                                            "ref_epoch": 2000.0, "name": "Mover"})
    m = _strict(r)
    assert m["key"].endswith("_pmm3781p70_769p50_e2000p000") and m["name"] == "Mover"
    tr = m["target_track"]
    assert len(tr) == 3 and "SIMBAD" in m["target_source"]
    from astropy.coordinates import SkyCoord
    import astropy.units as u
    a = SkyCoord(tr[0]["ra"] * u.deg, tr[0]["dec"] * u.deg)
    b = SkyCoord(tr[2]["ra"] * u.deg, tr[2]["dec"] * u.deg)
    dt_yr = (m["epochs"][2]["mjd_mid"] - m["epochs"][0]["mjd_mid"]) / 365.25
    assert a.separation(b).arcsec == pytest.approx(math.hypot(pmra, pmdec) / 1000 * dt_yr, rel=0.01)
    # the key reopens the same moving field after a restart
    client.new_registry()
    m2 = _strict(client.get("/api/manifest", params={"ra": RA0, "dec": DEC0, "pmra": pmra, "pmdec": pmdec,
                                                     "ref_epoch": 2000.0}))
    assert m2["key"] == m["key"] and len(m2["target_track"]) == 3
    assert client.get("/api/manifest", params={"ra": RA0, "dec": DEC0, "pmra": 1e6}).status_code == 400


def test_free_field_with_gallery_target_gets_its_bands(client):
    m = _strict(client.get("/api/manifest", params={"ra": "08h55m10.831s", "dec": "-07d14m42.53s"}))
    ids = [b["id"] for b in m["bands"]]
    assert "W" in ids and any("WISE 0855" in n for n in m.get("notes", []))


def test_box_cutouts_only_for_free_fields(client):
    m = _strict(client.get("/api/manifest", params={"ra": RA0, "dec": DEC0, "fov": 0.1}))
    client.get("/api/band", params={"key": m["key"], "band": "D1"})
    m = _strict(client.get("/api/manifest", params={"target": "rw-aur"}))
    client.get("/api/band", params={"key": "rw-aur", "band": "D1"})
    by_ra = client.calls["box_by_ra"]
    assert {round(b, 6) for r, b in by_ra if r == round(RA0, 1)} == {0.1}
    assert {b for r, b in by_ra if abs(r - 77.0) < 0.2} == {None}  # gallery targets reuse their square cutouts


@pytest.mark.skipif(__import__("os").environ.get("SPX_NETWORK") != "1", reason="set SPX_NETWORK=1 (Sesame, Gaia, SIA2)")
def test_proxima_track_matches_gaia():
    """SIMBAD (J2000 + pm, via /api/resolve) propagated to each epoch == Gaia DR3 (2016.0) propagated."""
    import csv
    import io
    import requests
    from astropy.coordinates import SkyCoord
    from astropy.time import Time
    import astropy.units as u
    from server import app as A
    from server import pipeline as P
    r = A._sesame("Proxima Centauri")
    assert r and r["pmra"] < -3000
    f = P.open_field(ra=r["ra"], dec=r["dec"], fov_deg=0.1, pm=(r["pmra"], r["pmdec"], r["ref_epoch"]))
    m = P.manifest(f)
    q = ("SELECT TOP 1 source_id, ra, dec, pmra, pmdec, phot_g_mean_mag FROM gaiadr3.gaia_source WHERE "
         f"1=CONTAINS(POINT('ICRS', ra, dec), CIRCLE('ICRS', {r['ra']}, {r['dec']}, 0.05)) AND pmra < -3000 "
         "ORDER BY phot_g_mean_mag")
    g = next(csv.DictReader(io.StringIO(requests.get(P.GAIA_TAP, params={"REQUEST": "doQuery", "LANG": "ADQL",
                                                                         "FORMAT": "csv", "QUERY": q}, timeout=60).text)))
    gc = SkyCoord(ra=float(g["ra"]) * u.deg, dec=float(g["dec"]) * u.deg, pm_ra_cosdec=float(g["pmra"]) * u.mas / u.yr,
                  pm_dec=float(g["pmdec"]) * u.mas / u.yr, obstime=Time(2016.0, format="jyear"), frame="icrs")
    assert len(m["target_track"]) >= 3
    for t, e in zip(m["target_track"], m["epochs"]):
        expect = P.target_at(gc, e["mjd_mid"])
        got = SkyCoord(t["ra"] * u.deg, t["dec"] * u.deg)
        assert got.separation(expect).arcsec < 0.5, (e, got.separation(expect).arcsec)


def test_touch_moves_field_first_in_background():
    from server import app as A
    reg = A.Registry.__new__(A.Registry)  # no worker threads
    import threading
    reg.lock = threading.RLock()
    reg.cv = threading.Condition(reg.lock)
    reg.seen = {}
    reg.background = [("a", "D2"), ("a", "D3"), ("b", "D2"), ("b", "D3")]
    reg.touch("b")
    assert reg.background == [("b", "D2"), ("b", "D3"), ("a", "D2"), ("a", "D3")]


def test_manifest_queue0_starts_no_band(client):
    m = _strict(client.get("/api/manifest", params={"ra": RA0, "dec": DEC0 + 1, "queue": 0}))
    assert m["bands"] and all(b["status"] == "pending" for b in m["bands"])
    _wait_idle()
    assert client.calls["fetch"] == 0
    assert client.get("/api/band", params={"key": m["key"], "band": "D4"}).status_code == 200  # still builds on request
    assert client.calls["fetch"] >= 1


def test_wings_only_for_r_dra_target(monkeypatch):
    from server import pipeline as P
    from spx import photometry as ph
    seen = []
    monkeypatch.setattr(ph, "spectrum_points", lambda *a, **k: seen.append(k.get("wings")) or {"meta": {}, "points": []})

    class F:
        def __init__(self, tinfo):
            self.tinfo, self.epochs = tinfo, []
            from astropy.coordinates import SkyCoord
            self.target = SkyCoord(10, 10, unit="deg")
    from spx.targets import TARGETS
    P.spectrum(F(TARGETS["r-dra"]), [])                       # the target: wing series on
    P.spectrum(F(TARGETS["r-dra"]), [], ra=10.0, dec=10.0)    # a clicked point: off
    P.spectrum(F(TARGETS["rw-aur"]), [])                      # other targets: off
    P.spectrum(F(None), [])                                   # free field: off
    assert seen == [True, False, False, False]


def test_wing_bands_threshold_from_quality_file(monkeypatch, tmp_path):
    from server import pipeline as P
    from spx import photometry as ph
    f = tmp_path / "wing_validation_v2.json"
    f.write_text('{"summary": {"controls_p90": 1.5}}', encoding="utf-8")
    monkeypatch.setattr(P, "WING_VALIDATION", f)
    monkeypatch.setattr(ph, "variability", lambda series: {"D1": {"ratio": 1.2, "n_bins": 8},
                                                           "D2": {"ratio": 2.0, "n_bins": 8}})
    monkeypatch.setattr(ph, "binned_series", lambda points, kind="flux": [])
    assert P.wing_bands([]) == {"D1": {"ratio": 1.2, "threshold": 1.5, "significant": False},
                                "D2": {"ratio": 2.0, "threshold": 1.5, "significant": True}}
    monkeypatch.setattr(P, "WING_VALIDATION", tmp_path / "missing.json")
    assert P.wing_bands([]) is None  # no threshold -> the field is left out, never guessed



# --------------------------------------------------------------------------- track and MPC draft (offline)

def _track_rows(regime="within_visit"):
    import sys
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
    import test_mpc as T
    rows = T._dets(5)
    for i, r in enumerate(rows):
        r.update({"mjd_mid": 60988.99 + i * 0.0007, "detector": 6, "accepted": True, "reason": "ok", "trailed": False,
                  "regime": regime, "snr_fit": 30.0, "snr_variance": 110.0, "snr_image": 12.5})
    rows[4].update({"accepted": False, "reason": "blended"})
    return rows


@pytest.fixture()
def track_client(client, monkeypatch):
    from server import pipeline as P
    state = {"regime": "within_visit"}

    def fake_measure(marks, band):
        rows = _track_rows(state["regime"])
        span = 0.1 if state["regime"] == "within_visit" else 180.0
        info = {"regime": state["regime"], "span_days": span, "rate_arcsec_per_day": (86.4, 0.0),
                "speed_arcsec_per_s": 0.001, "window_mjd": (60988.0, 60990.0), "n_marks": len(marks)}
        return rows, info, 7
    monkeypatch.setattr(P, "measure_marks", fake_measure)
    client.track_state = state
    return client


MARKS = [{"ra": 133.76, "dec": -7.24, "mjd": 60988.99}, {"ra": 133.762, "dec": -7.2402, "mjd": 60989.0}]


def test_track_and_reports(track_client):
    c = track_client
    r = c.post("/api/track", json={"band": "D6", "marks": MARKS})
    assert r.status_code == 200
    t = _strict(r)
    assert t["regime"] == "within_visit" and t["report_id"] and t["n_accepted"] == 4 and t["n_in_draft"] == 4
    assert t["nights"][0]["included"] and t["nights"][0]["n_accepted"] == 4
    assert [m["in_ades"] for m in t["measurements"]] == [True, True, True, True, False]
    assert all(m["snr_image"] == 12.5 and m["snr_variance"] == 110.0 for m in t["measurements"])
    assert t["measurements"][4]["reason"] == "blended" and t["rate"]["arcsec_per_hour"] == pytest.approx(3.6)
    assert t["validation"]["timing"] == "not verified" and t["validation"]["text"]
    rid = t["report_id"]
    r = c.get("/api/report", params={"id": rid, "format": "csv"}, headers={"Origin": "http://127.0.0.1:8765"})
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    exposed = {h.strip().lower() for h in r.headers["access-control-expose-headers"].split(",")}
    assert {"content-disposition", "x-ades-schema-checked"} <= exposed            # page and server differ in origin
    assert r.headers["access-control-allow-origin"] == "*"
    assert "blended" in r.text and r.headers["content-type"].startswith("text/csv")
    r = c.get("/api/report", params={"id": rid, "format": "ades", "submitter": "I. M. Tester"})
    assert r.status_code == 200 and r.text.startswith("# version=2022")
    assert r.headers["content-disposition"].endswith('_ades.psv"')
    from spx import mpc
    assert mpc.validate_psv(r.text) == []
    if mpc.SUBMIT_XSD.exists():
        assert r.headers["x-ades-schema-checked"] == "true" and mpc.SCHEMA_PASSED in r.text
    r = c.get("/api/report", params={"id": rid, "format": "ades_xml", "submitter": "I. M. Tester", "trksub": "SPXab12"})
    assert r.status_code == 200 and "<trkSub>SPXab12</trkSub>" in r.text


def test_report_errors(track_client):
    c = track_client
    rid = _strict(c.post("/api/track", json={"band": "D6", "marks": MARKS}))["report_id"]
    assert c.get("/api/report", params={"id": "nope"}).status_code == 404
    assert c.get("/api/report", params={"id": rid, "format": "ades"}).status_code == 400            # no submitter
    r = c.get("/api/report", params={"id": rid, "format": "ades", "submitter": "Ivan Tester"})
    assert r.status_code == 400 and "initials" in _strict(r)["message"]
    r = c.get("/api/report", params={"id": rid, "format": "ades", "submitter": "I. M. Tester", "trksub": "K23A00B"})
    assert r.status_code == 400 and "designation" in _strict(r)["message"]
    assert c.get("/api/report", params={"id": rid, "format": "pdf"}).status_code == 400
    c.track_state["regime"] = "between_epochs"
    rid2 = _strict(c.post("/api/track", json={"band": "D6", "marks": MARKS}))["report_id"]
    r = c.get("/api/report", params={"id": rid2, "format": "ades", "submitter": "I. M. Tester"})
    assert r.status_code == 422 and "Solar System" in _strict(r)["message"]
    r = c.get("/api/report", params={"id": rid2, "format": "csv"})
    assert r.status_code == 200 and "not for the Minor Planet Center" in r.text


def test_track_input_errors(track_client):
    c = track_client
    assert c.post("/api/track", json={"band": "D6", "marks": MARKS[:1]}).status_code == 400
    assert c.post("/api/track", json={"band": "D9", "marks": MARKS}).status_code == 400
    assert c.post("/api/track", json={"band": "D6", "marks": [{"ra": "x"}] * 2}).status_code == 400
    assert c.post("/api/track", content=b"not json", headers={"content-type": "application/json"}).status_code == 400


def test_schema_failure_is_a_500(track_client, monkeypatch):
    from spx import mpc
    if not mpc.SUBMIT_XSD.exists():
        pytest.skip("submit.xsd not generated")
    c = track_client
    rid = _strict(c.post("/api/track", json={"band": "D6", "marks": MARKS}))["report_id"]
    monkeypatch.setattr(mpc, "LOCATION_SYS", "ICRF_XX")
    r = c.get("/api/report", params={"id": rid, "format": "ades", "submitter": "I. M. Tester"})
    assert r.status_code == 500 and "bug on our side" in _strict(r)["message"]


# --------------------------------------------------------------------------- user wavelength windows (F1)

def test_user_window_band_and_frame(client):
    from server import pipeline as P
    m = _strict(client.get("/api/manifest", params={"ra": RA0, "dec": DEC0}))
    key = m["key"]
    for b in ("D5", "D6"):
        assert client.get("/api/band", params={"key": key, "band": b}).status_code == 200
    _wait_idle()
    n_fetch = client.calls["fetch"]
    r = client.get("/api/band", params={"key": key, "band": "w4600_4800"})      # synthetic D6 exposures: 4.71 um
    assert r.status_code == 200, r.text
    bj = _strict(r)
    assert bj["band"] == "w4600_4800" and bj["wave_um"] == [4.6, 4.8] and bj["label"] == "4.600–4.800 µm"
    assert bj["n_exposures"] == [4, 4, 4] and bj["note"] == P.WINDOW_NOTE and "scale" in bj
    assert [e["n_used"] for e in bj["epochs"]] == [4, 4, 4]
    assert all(4.6 <= e["band_um"][0] <= e["band_um"][1] <= 4.8 for e in bj["epochs"])
    assert client.calls["fetch"] == n_fetch                       # D6 cutouts reused, nothing downloaded
    shape = m["grid"]["shape"]
    f = client.get("/api/frame", params={"key": key, "band": "w4600_4800", "epoch": 0})
    assert f.status_code == 200 and len(f.content) == 4 * shape[0] * shape[1]
    m2 = _strict(client.get("/api/manifest", params={"ra": RA0, "dec": DEC0}))
    assert all(not b["id"].startswith("w") for b in m2["bands"])  # windows are not listed
    # a window with no exposure inside (between the synthetic D5 4.11 and D6 4.71 um): 200, no frames
    e = _strict(client.get("/api/band", params={"key": key, "band": "w4200_4400"}))
    assert e["n_exposures"] == [0, 0, 0] and all(x["n_used"] == 0 for x in e["epochs"])


def test_user_window_errors(client):
    key = _strict(client.get("/api/manifest", params={"ra": RA0, "dec": DEC0}))["key"]
    for bad, why in (("w4600_4620", "at least 50 nm"), ("w0100_0900", "750 <= lo"), ("w4800_4600", "lo < hi"),
                     ("w4600-4800", "whole nanometres"), ("w46000_48000", "whole nanometres"),
                     ("w4600_5100", "hi <= 5000")):
        r = client.get("/api/band", params={"key": key, "band": bad})
        assert r.status_code == 400 and why in r.json()["message"], (bad, r.text)
        assert client.get("/api/frame", params={"key": key, "band": bad, "epoch": 0}).status_code == 400
    assert client.get("/api/band", params={"key": key, "band": "D9"}).status_code == 404


def test_user_windows_per_field_are_capped(client, monkeypatch):
    from server import app as A
    monkeypatch.setattr(A, "MAX_WINDOWS_PER_FIELD", 2)
    key = _strict(client.get("/api/manifest", params={"ra": RA0, "dec": DEC0}))["key"]
    out = A.FIELDS_DIR / key
    for w in ("w4600_4800", "w4500_4900", "w4400_5000"):
        assert client.get("/api/band", params={"key": key, "band": w}).status_code == 200
    fs = A.REG.fields[key]
    assert fs.windows == ["w4500_4900", "w4400_5000"] and "w4600_4800" not in fs.jobs
    assert not (out / "w4600_4800.json").exists() and not list(out.glob("*_w4600_4800*.f32"))
    assert (out / "w4400_5000.json").exists()
    # asked again: built again
    assert client.get("/api/band", params={"key": key, "band": "w4600_4800"}).status_code == 200
    assert (out / "w4600_4800.json").exists() and "w4500_4900" not in fs.jobs


def test_parse_window():
    from server import pipeline as P
    assert P.parse_window("D6") is None and P.parse_window("W") is None
    assert P.parse_window("w750_800")["wave_um"] == [0.75, 0.8]
    assert P.is_window("w4300_5000") and not P.is_window("w4300_4310") and not P.is_window("D1")


# --------------------------------------------------------------------------- flag names at the clicked point (F3)

def test_spectrum_passes_flag_names_through(client, monkeypatch):
    from spx import photometry as ph
    if not hasattr(ph, "flag_legend"):
        pytest.skip("spx.photometry has no flag names yet (spectrum point flags, spx.photometry.flag_names)")
    name = "TRANSIENT" if "TRANSIENT" in ph.FLAG_BITS else sorted(ph.FLAG_BITS, key=ph.FLAG_BITS.get)[0]
    bit = ph.FLAG_BITS[name]
    fake = access.fetch_cutouts

    def flagged(refs, ra, dec, *a, **k):
        cuts, n = fake(refs, ra, dec, *a, **k)
        for c in cuts:
            if c.detector == 6 and c.flags is not None:
                cy, cx = (int(round(v)) for v in c.target_xy[::-1])
                c.flags[cy, cx] |= np.int32(1 << bit)
        return cuts, n
    monkeypatch.setattr(access, "fetch_cutouts", flagged)
    key = _strict(client.get("/api/manifest", params={"ra": RA0, "dec": DEC0}))["key"]
    for b in ("D5", "D6"):
        assert client.get("/api/band", params={"key": key, "band": b}).status_code == 200
    s = _strict(client.get("/api/spectrum", params={"key": key}))
    d6 = [p for p in s["points"] if p["band"] == "D6"]
    others = [p for p in s["points"] if p["band"] != "D6"]
    assert d6 and all(name in p["flags"] for p in d6), [p["flags"] for p in d6][:3]
    assert others and all(p["flags"] == [] for p in others)
    assert s["meta"]["flag_legend"][name] == ph.FLAG_LEGEND[name]
    assert set(s["meta"]["flag_legend"]) == {n for p in s["points"] for n in p["flags"]}
    assert s["meta"]["position"].startswith("target, moved") and s["meta"]["layers"] == ["IMAGE", "FLAGS"]
    if any("masked_fraction" in p for p in s["points"]):        # quality 575c505: passed through as is
        assert all(0.0 <= p["masked_fraction"] <= 1.0 for p in s["points"]) and s["meta"].get("err_note")


# --------------------------------------------------------------------------- same field, different rounding

def test_rounding_boundary_reuses_the_open_field(client):
    from server import app as A
    a = _strict(client.get("/api/manifest", params={"ra": 288.770496, "dec": 3.059049, "queue": 0}))
    b = _strict(client.get("/api/manifest", params={"ra": 288.7705, "dec": 3.05905, "queue": 0}))
    assert a["key"] == "r288p7705_dp3p0590_n140" and b["key"] == a["key"]          # was _dp3p0591 on 14 Nov
    assert client.calls["sia"] == 1
    # 2 arcsec away (more than half a 3" grid pixel): a field of its own; another fov too
    c = _strict(client.get("/api/manifest", params={"ra": 288.770496, "dec": 3.059049 + 2.0 / 3600, "queue": 0}))
    d = _strict(client.get("/api/manifest", params={"ra": 288.7705, "dec": 3.05905, "fov": 0.05, "queue": 0}))
    assert len({a["key"], c["key"], d["key"]}) == 3 and client.calls["sia"] == 3
    assert A.REG.near_key("r288p7705_dp3p0591_n140", 288.7705, 3.05905) == a["key"]
    assert d["key"] == "r288p7705_dp3p0591_n60"
    assert A.REG.near_key("r288p7705_dp3p0590_n60", 288.770496, 3.059049) == d["key"]   # same fov: found
    assert A.REG.near_key("r288p7705_dp3p0591_n140_pm1p00_0p00_e2000p000", 288.7705, 3.05905) is None


# --------------------------------------------------------------------------- automatic candidates (F2)

class _FakeDetect:
    """Stands in for spx.detect: records what the server hands over."""
    def __init__(self, with_cands=False):
        self.seen = []
        self.with_cands = with_cands

    def load_field(self, path):
        from pathlib import Path
        path = Path(path)
        return {"path": path, "manifest": json.loads((path / "manifest.json").read_text()),
                "gaia": json.loads((path / "gaia.json").read_text()) if (path / "gaia.json").exists() else None,
                "bands": sorted(p.stem for p in path.glob("D*.json"))}

    def find_candidates(self, field, bands=None, params=None):
        self.seen.append(field)
        cands = [{"id": 1, "kind": "mover", "snr": 9.0, "bands": ["D5", "W"], "n_bands": 2, "grade": "A",
                  "grade_reason": "two bands", "known": False, "rate_arcsec_per_year": [-8.6, 0.7]}] if self.with_cands else []
        return {"method": "fake", "params": {}, "bands_searched": field["bands"], "candidates": cands,
                "rejected": {}, "validation": ["fake detector"]}


def test_candidates_after_all_bands(client, monkeypatch):
    from server import pipeline as P
    fake = _FakeDetect()
    monkeypatch.setattr(P, "detect_module", lambda: fake)
    m = _strict(client.get("/api/manifest", params={"ra": RA0, "dec": DEC0}))
    r = client.get("/api/candidates", params={"key": m["key"]})
    if r.status_code == 202:                       # bands still building: pending, with what is missing
        assert r.json()["status"] == "pending" and isinstance(r.json()["bands_pending"], list)
    _wait_idle()
    import time
    for _ in range(100):
        r = client.get("/api/candidates", params={"key": m["key"]})
        if r.status_code == 200:
            break
        time.sleep(0.05)
    c = _strict(r)
    assert c["schema"] == 1 and c["key"] == m["key"] and c["method"] == "fake"
    assert c["bands_searched"] == ["D1", "D2", "D3", "D4", "D5", "D6"] and "seconds" in c
    assert len(fake.seen) == 1 and fake.seen[0]["gaia"] and fake.seen[0]["manifest"]["key"] == m["key"]
    client.get("/api/candidates", params={"key": m["key"]})
    assert len(fake.seen) == 1                     # made once, then served from the file


def test_candidates_without_detector_is_503(client, monkeypatch):
    from server import pipeline as P
    monkeypatch.setattr(P, "detect_module", lambda: None)
    key = _strict(client.get("/api/manifest", params={"ra": RA0, "dec": DEC0}))["key"]
    r = client.get("/api/candidates", params={"key": key})
    assert r.status_code == 503 and "spx.detect" in r.json()["message"]


def test_make_candidates_script_on_a_gallery_folder(tmp_path, monkeypatch):
    import importlib.util
    from server import pipeline as P
    spec = importlib.util.spec_from_file_location("make_candidates", P.Path(__file__).parent.parent / "scripts" / "make_candidates.py")
    mc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mc)
    d = tmp_path / "wise-0855"
    d.mkdir()
    (tmp_path / "index.json").write_text(json.dumps({"targets": [{"key": "wise-0855"}, {"key": "barnard"}]}))
    (d / "manifest.json").write_text(json.dumps({"schema": 1, "key": "wise-0855"}))
    (d / "gaia.json").write_text(json.dumps([{"ra": 1.0, "dec": 2.0}]))
    (d / "D1.json").write_text("{}")
    fake = _FakeDetect()
    monkeypatch.setattr(P, "detect_module", lambda: fake)
    assert mc.main(["--data", str(tmp_path)]) == 1                     # barnard has no folder
    c = json.loads((d / "candidates.json").read_text())
    assert c["schema"] == 1 and c["key"] == "wise-0855" and c["bands_searched"] == ["D1"]
    assert fake.seen[0]["gaia"] == [{"ra": 1.0, "dec": 2.0}]
    monkeypatch.setattr(P, "detect_module", lambda: None)
    assert mc.main(["--data", str(tmp_path), "wise-0855"]) == 2


def test_candidate_fields_pass_through_unchanged(client, monkeypatch):
    from server import pipeline as P
    monkeypatch.setattr(P, "detect_module", lambda: _FakeDetect(with_cands=True))
    m = _strict(client.get("/api/manifest", params={"ra": RA0, "dec": DEC0}))
    _wait_idle()
    import time
    for _ in range(100):
        r = client.get("/api/candidates", params={"key": m["key"]})
        if r.status_code == 200:
            break
        time.sleep(0.05)
    c = _strict(r)["candidates"][0]
    assert {k: c[k] for k in ("n_bands", "grade", "grade_reason", "known")} ==         {"n_bands": 2, "grade": "A", "grade_reason": "two bands", "known": False}


def test_catwise_served_for_gallery_targets_only(client):
    r = client.get("/api/catwise", params={"key": "wise-0855"})
    if r.status_code == 404:
        pytest.skip("no web/data/wise-0855/catwise.json")
    c = _strict(r)
    assert c["catalog"] == "CatWISE2020" and c["key"] == "wise-0855" and c["sources"]
    key = _strict(client.get("/api/manifest", params={"ra": RA0, "dec": DEC0, "queue": 0}))["key"]
    r = client.get("/api/catwise", params={"key": key})
    assert r.status_code == 404 and "no WISE catalogue" in r.json()["message"]


# --------------------------------------------------------------------------- public server: errors

def test_unhandled_error_is_json_with_cors_and_no_detail(client, monkeypatch):
    from server import app as A
    def boom(*a, **k):
        raise RuntimeError(r"C:\secret\path\file.fits exploded")
    monkeypatch.setattr(A.Registry, "manifest", boom)
    with TestClient(A.app, raise_server_exceptions=False) as c:
        r = c.get("/api/manifest", params={"ra": RA0, "dec": DEC0}, headers={"Origin": "http://127.0.0.1:8765"})
    assert r.status_code == 500 and r.headers["access-control-allow-origin"] == "*"
    j = r.json()
    assert j["schema"] == 1 and "error id" in j["message"] and "secret" not in r.text and "RuntimeError" not in r.text


def test_band_failure_does_not_leak_the_exception(client, monkeypatch):
    from server import pipeline as P
    def boom(*a, **k):
        raise OSError(r"[Errno 2] No such file: 'C:\cache\private\x.npz'")
    monkeypatch.setattr(P, "build_band", boom)
    key = _strict(client.get("/api/manifest", params={"ra": RA0, "dec": DEC0}))["key"]
    r = client.get("/api/band", params={"key": key, "band": "D1"})
    assert r.status_code == 503 and "error id" in r.json()["message"] and "private" not in r.text
    m = client.get("/api/manifest", params={"ra": RA0, "dec": DEC0}).json()
    errs = [b.get("error") for b in m["bands"] if b.get("error")]
    assert errs and all("private" not in e and "error id" in e for e in errs)


def test_track_body_must_be_an_object(track_client):
    for body in ([], "x", 3, None):
        r = track_client.post("/api/track", json=body)
        assert r.status_code == 400 and r.json()["schema"] == 1, (body, r.text)


# --------------------------------------------------------------------------- public server: /api/track limits

def test_track_limits(track_client):
    from server import app as A
    c = track_client
    many = [{"ra": 133.76 + 0.0001 * i, "dec": -7.24, "mjd": 60988.99 + 0.001 * i} for i in range(A.TRACK_MAX_MARKS + 1)]
    r = c.post("/api/track", json={"band": "D6", "marks": many})
    assert r.status_code == 400 and "at most" in r.json()["message"]
    pad = "x" * (A.TRACK_MAX_BODY_BYTES + 10)
    r = c.post("/api/track", json={"band": "D6", "marks": MARKS, "pad": pad})
    assert r.status_code == 413
    far = [{"ra": 133.0, "dec": -7.0, "mjd": 60988.0}, {"ra": 143.0, "dec": -7.0, "mjd": 60989.0}]   # 10 deg in a day
    r = c.post("/api/track", json={"band": "D6", "marks": far})
    assert r.status_code == 400 and "image searches" in r.json()["message"]
    assert c.post("/api/track", json={"band": "D6", "marks": MARKS}).status_code == 200      # a normal track still works


def test_track_cost_rule():
    from server import app as A
    cost = A.track_cost(MARKS)
    assert cost["within_visit"] and cost["sia_points"] == 2 and cost["window_days"] < 5



# --------------------------------------------------------------------------- public server: request limits

def test_rate_limit_per_ip(client, monkeypatch):
    from server import app as A
    monkeypatch.setattr(A, "RATE_ON", True)
    monkeypatch.setattr(A, "RATE_PER_MIN", dict(A.RATE_PER_MIN, manifest=3))
    for _ in range(3):
        assert client.get("/api/manifest", params={"ra": RA0, "dec": DEC0, "queue": 0}).status_code == 200
    r = client.get("/api/manifest", params={"ra": RA0, "dec": DEC0, "queue": 0})
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1 and "per minute" in r.json()["message"]
    assert r.headers["access-control-allow-origin"] == "*"


def test_new_field_limits(client, monkeypatch):
    from server import app as A
    monkeypatch.setattr(A, "RATE_ON", True)
    monkeypatch.setattr(A, "NEW_FIELDS_PER_IP", 2)
    ok = [client.get("/api/manifest", params={"ra": RA0 + i, "dec": DEC0, "queue": 0}).status_code for i in range(2)]
    assert ok == [200, 200]
    assert client.get("/api/manifest", params={"ra": RA0, "dec": DEC0, "queue": 0}).status_code == 200   # open: free
    r = client.get("/api/manifest", params={"ra": RA0 + 5, "dec": DEC0, "queue": 0})
    assert r.status_code == 429 and "new fields" in r.json()["message"]
    # server-wide: only fields someone is waiting for count. Bands nobody queued (queue=0) do not: on 15 Nov four
    # such fields kept every new field out while nothing was being built.
    monkeypatch.setattr(A, "NEW_FIELDS_PER_IP", 100)
    monkeypatch.setattr(A, "MAX_ACTIVE_FIELDS", 1)
    with A.REG.lock:
        fs = next(iter(A.REG.fields.values()))
        job = next(iter(fs.jobs.values()))
        assert not job.event.is_set() and job.state == "pending"
    assert client.get("/api/manifest", params={"ra": RA0 + 6, "dec": DEC0, "queue": 0}).status_code == 200
    job.state, job.background = "running", False           # a band being built for a request
    try:
        r = client.get("/api/manifest", params={"ra": RA0 + 7, "dec": DEC0, "queue": 0})
        assert r.status_code == 429 and "waiting for" in r.json()["message"] and r.headers["retry-after"] == "60"
        job.background = True                                # built ahead, nobody waiting: no limit
        assert client.get("/api/manifest", params={"ra": RA0 + 7, "dec": DEC0, "queue": 0}).status_code == 200
        h = client.get("/api/health").json()
        assert h["fields_waiting"] == {"now": 0, "max": 1}
    finally:
        job.state = "pending"


def test_bands_are_built_ahead_for_the_newest_fields_only(monkeypatch):
    from server import app as A
    reg = A.Registry.__new__(A.Registry)                     # no worker threads: the queue is only looked at
    import threading
    from collections import OrderedDict
    reg.lock = threading.RLock()
    reg.cv = threading.Condition(reg.lock)
    reg.fields, reg.urgent, reg.background, reg.seen, reg.recent = OrderedDict(), [], [], {}, []
    for k in ("a", "b", "c"):
        reg.fields[k] = A.FieldState(None, None, {b: A.BandJob(b) for b in ("D1", "D2", "D3")})
    monkeypatch.setattr(A, "PREBUILD_FIELDS", 2)
    for k in ("a", "b", "c"):
        reg.enqueue(k, "D1", urgent=True)
        reg.prebuild(k, ["D2", "D3"])
    assert reg.recent == ["c", "b"] and {k for k, _ in reg.background} == {"b", "c"}
    assert [k for k, _ in reg.urgent] == ["c", "b", "a"]     # asked-for bands always stay
    assert reg.waiting_fields() == set()                       # queued, but nobody asked about them (seen unset)
    reg.seen["a"] = __import__("time").time()
    assert reg.waiting_fields() == {"a"}
    reg.prebuild("a", ["D2", "D3"], asked=True)               # the candidates tab of an older field
    assert reg.recent == ["a", "c"] and {k for k, _ in reg.background} == {"a", "c"}
    monkeypatch.setattr(A, "PREBUILD_FIELDS", 0)
    reg.prebuild("b", ["D2", "D3"])
    assert reg.recent == [] and reg.background == []
    reg.prebuild("b", ["D2"], asked=True)
    assert reg.background == [("b", "D2")]


# --------------------------------------------------------------------------- public server: disk cache limit

def test_cache_cleanup_order_and_protection(tmp_path):
    import os
    from server import app as A
    def put(rel, n, age_s):
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"x" * n)
        t = __import__("time").time() - age_s
        os.utime(f, (t, t))
    cur = tmp_path / "fields" / "v0.1-new"
    put("fields/v0.1-old/somefield/D1.json", 400, 10)          # other code version: removed first
    put("fields/v0.1-new/open/D1.json", 400, 9000)             # open field: never removed
    put("fields/v0.1-new/closed/D1.json", 400, 8000)           # not open, oldest of this version
    put("cutout/a/old.npz", 300, 7000)                         # oldest cutout
    put("cutout/a/new.npz", 300, 5)
    put("sia/spherex_dr1/x.json", 5000, 9999)                  # image lists: not managed
    out = A.cache_cleanup(tmp_path, 1500, cur, keep={"open"})
    assert out["bytes_before"] == 1800 and out["bytes_after"] <= 1500 * A.CACHE_LOW_WATER
    assert not (tmp_path / "fields/v0.1-old").exists() and not (tmp_path / "cutout/a/old.npz").exists()
    assert (tmp_path / "fields/v0.1-new/open/D1.json").exists() and (tmp_path / "sia/spherex_dr1/x.json").exists()
    assert (tmp_path / "cutout/a/new.npz").exists()                    # enough was freed before reaching it
    out = A.cache_cleanup(tmp_path, 100000, cur, keep=set())             # under the limit: nothing removed
    assert out["files"] == out["folders"] == 0


def test_health_reports_cache(client):
    h = _strict(client.get("/api/health"))
    assert set(h["cache"]) == {"bytes", "max_bytes", "checked_utc", "last_cleanup"}


# --------------------------------------------------------------------------- public server: name cache

class _Resp:
    def __init__(self, code, text):
        self.status_code, self.text = code, text


def test_resolve_outage_not_cached_and_cache_bounded(client, monkeypatch):
    import requests
    from server import app as A
    monkeypatch.setattr(A, "_resolve_cache", A.OrderedDict())
    calls = []
    answers = []

    def get(url, timeout=None):
        calls.append(url)
        return answers.pop(0)
    monkeypatch.setattr(requests, "get", get)
    answers[:] = [_Resp(502, "<html>Page unavailable</html>")]
    r = client.get("/api/resolve", params={"name": "Some Star"})
    assert r.status_code == 503 and "not answering" in r.json()["message"] and not A._resolve_cache
    found = _Resp(200, "<Resolver><jradeg>10.5</jradeg><jdedeg>-20.25</jdedeg><oname>Some Star</oname></Resolver>")
    answers[:] = [found]
    assert _strict(client.get("/api/resolve", params={"name": "Some Star"}))["ra"] == 10.5
    assert _strict(client.get("/api/resolve", params={"name": "Some Star"}))["dec"] == -20.25 and len(calls) == 2
    answers[:] = [_Resp(200, "<Resolver><INFO>nothing found</INFO></Resolver>")]
    assert client.get("/api/resolve", params={"name": "No Such Thing"}).status_code == 404
    assert client.get("/api/resolve", params={"name": "No Such Thing"}).status_code == 404 and len(calls) == 3
    n = A._norm("No Such Thing")
    t, v = A._resolve_cache[n]
    A._resolve_cache[n] = (t - A.RESOLVE_NEG_TTL_S - 1, v)          # a "not found" expires
    answers[:] = [found]
    assert client.get("/api/resolve", params={"name": "No Such Thing"}).status_code == 200 and len(calls) == 4
    monkeypatch.setattr(A, "RESOLVE_MAX", 2)
    answers[:] = [found, found]
    for nm in ("A1", "A2"):
        client.get("/api/resolve", params={"name": nm})
    assert len(A._resolve_cache) == 2 and list(A._resolve_cache) == [A._norm("A1"), A._norm("A2")]


def test_cache_checked_after_a_build(client, monkeypatch):
    from server import app as A
    calls = []
    monkeypatch.setattr(A, "CACHE_MAX_BYTES", 10 ** 12)
    monkeypatch.setattr(A, "CACHE_STATE", {"bytes": None, "checked": None, "last_cleanup": None})
    monkeypatch.setattr(A, "cache_check", lambda: (calls.append(1), A.CACHE_STATE.update(checked=__import__("time").time())))
    client.get("/api/manifest", params={"ra": RA0, "dec": DEC0})
    _wait_idle()
    assert len(calls) == 1                         # six bands built, one check (throttled to once a minute)


# --------------------------------------------------------------------------- restart: a clean start (Cloud Run)

def test_restart_mid_build_starts_clean(client):
    from server import app as A
    m = _strict(client.get("/api/manifest", params={"ra": RA0, "dec": DEC0}))
    _wait_idle()
    out = A.FIELDS_DIR / m["key"]
    (out / "D3.json").unlink()                     # the process died while D3 was being written: frames, no json
    (out / ".D3.json.tmp123").write_text("partial")
    n_fetch = client.calls["fetch"]
    client.new_registry()                          # a new process: empty registry, files on disk
    m2 = _strict(client.get("/api/manifest", params={"ra": RA0, "dec": DEC0}))
    st = {b["id"]: b["status"] for b in m2["bands"]}
    assert st["D1"] == "ready" and st["D3"] in ("pending", "ready")
    _wait_idle()
    assert (out / "D3.json").exists() and _strict(client.get("/api/band", params={"key": m["key"], "band": "D3"}))
    assert client.calls["fetch"] == n_fetch + 1    # only D3's detector downloaded again; D1, D2 ... from disk


# --------------------------------------------------------------------------- public server: band waiters

def test_band_waiters_capped_ready_bands_free(client, monkeypatch):
    from server import app as A
    from server import pipeline as P
    m = _strict(client.get("/api/manifest", params={"ra": RA0, "dec": DEC0, "queue": 0}))
    real_build = P.build_band
    monkeypatch.setattr(P, "build_band", lambda *a, **k: (__import__("time").sleep(1.0), real_build(*a, **k))[1])
    monkeypatch.setattr(A, "MAX_BAND_WAITERS", 0)
    r = client.get("/api/band", params={"key": m["key"], "band": "D1"})        # not built yet (takes 1 s): would wait
    assert r.status_code == 429 and r.headers["retry-after"] == "10"
    _wait_idle()                                                                 # it was queued all the same
    assert client.get("/api/band", params={"key": m["key"], "band": "D1"}).status_code == 200   # ready: no wait
    h = _strict(client.get("/api/health"))
    assert h["band_waiters"] == {"now": 0, "max": 0}


def test_bad_band_ids_rejected_before_the_field_opens(client, monkeypatch):
    """A fresh server (the field not open) with IRSA down: a bad id is still 400/404, never 503."""
    def down(*a, **k):
        raise access.SiaError("SIA2 HTTP 502")
    monkeypatch.setattr(access, "find_images", down)
    for path in ("/api/band", "/api/frame"):
        for band, code in (("w1_2", 400), ("w4300_4250", 400), ("wabc", 400), ("X9", 404)):
            params = {"key": "wise-0855", "band": band}
            if path == "/api/frame":
                params["epoch"] = 0
            r = client.get(path, params=params)
            assert r.status_code == code and r.json()["schema"] == 1, (path, band, r.status_code, r.text)
    r = client.get("/api/band", params={"key": "wise-0855", "band": "D1"})     # a good id: opening fails, 503
    assert r.status_code == 503


def test_health_shows_sia_and_threads(client):
    h = _strict(client.get("/api/health"))
    assert h["sia"]["max_attempts"] >= 1 and h["sia"]["retry_budget_s"] > 0 and h["threads"] >= 5


# --------------------------------------------------------------------------- IRSA down: the gallery's SIA2 seed

def test_gallery_targets_open_from_the_seed_with_irsa_down(tmp_path, monkeypatch):
    from pathlib import Path
    from server import app as A
    from server import pipeline as P
    gallery = Path(__file__).resolve().parent.parent / "web" / "data"
    if not (gallery / "_sia").is_dir():
        pytest.skip("no web/data/_sia (scripts/precompute.py --sia-seed-only)")

    def down(*a, **k):
        raise access.SiaError("SIA2 HTTP 502")

    def fake_fetch(refs, ra, dec, half_px=50, layers=("IMAGE", "FLAGS", "VARIANCE"), workers=32, progress=None,
                   stats=None, box_deg=None):
        rng = np.random.default_rng(len(refs))
        return [_cutout(r, ra, dec, min(half_px, 20), layers, rng) for r in refs], 0
    monkeypatch.setattr(access, "_sia_query", down)
    monkeypatch.setattr(access, "fetch_cutouts", fake_fetch)
    monkeypatch.setattr(access, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(A, "FIELDS_DIR", tmp_path / "fields")
    monkeypatch.setattr(A, "RATE_ON", False)
    monkeypatch.setattr(P, "gaia_sources", lambda f, **k: [])
    assert A.seed_sia_cache(gallery, tmp_path) == 10
    assert A.seed_sia_cache(gallery, tmp_path) == 0                       # present: not copied again
    reg = A.Registry()
    monkeypatch.setattr(A, "REG", reg)
    try:
        with TestClient(A.app) as c:
            for key in ("barnard", "eps-indi-b", "wise-0855", "rw-aur", "r-dra"):
                m = _strict(c.get("/api/manifest", params={"target": key, "queue": 0}))
                prov = json.loads((gallery / key / "provenance.json").read_text(encoding="utf-8"))
                assert m["n_images"] == prov["n_images_sia2"], (key, m["n_images"], prov["n_images_sia2"])
            b = c.get("/api/band", params={"key": "wise-0855", "band": "w4300_5000"})
            assert b.status_code == 200, b.text
            assert sum(b.json()["n_exposures"]) > 0
    finally:
        reg.close()


# --------------------------------------------------------------------------- every error the server makes carries CORS

def test_cors_on_every_error_status(client, monkeypatch):
    from server import app as A
    from server import pipeline as P
    H = {"Origin": "https://sci-techspherex.com"}
    seen = {}

    def check(r):
        seen[r.status_code] = r.headers.get("access-control-allow-origin")
    check(client.get("/api/band", params={"key": "wise-0855", "band": "w1_2"}, headers=H))            # 400
    check(client.get("/api/band", params={"key": "wise-0855", "band": "X9"}, headers=H))              # 404
    check(client.post("/api/track", content=b"{" + b" " * (A.TRACK_MAX_BODY_BYTES + 1) + b"}", headers=H))  # 413
    key = _strict(client.get("/api/manifest", params={"ra": RA0, "dec": DEC0, "queue": 0}))["key"]
    monkeypatch.setattr(A, "MAX_BAND_WAITERS", 0)
    check(client.get("/api/band", params={"key": key, "band": "D2"}, headers=H))                      # 429
    monkeypatch.setattr(A, "MAX_BAND_WAITERS", 64)
    _wait_idle()
    monkeypatch.setattr(A, "BAND_WAIT_S", 0.0)
    monkeypatch.setattr(P, "build_band", lambda *a, **k: __import__("time").sleep(2) or (_ for _ in ()).throw(OSError("x")))
    check(client.get("/api/band", params={"key": key, "band": "w4600_4800"}, headers=H))              # 504
    _wait_idle()
    check(client.get("/api/band", params={"key": key, "band": "w4600_4800"}, headers=H))              # 503
    monkeypatch.setattr(A.Registry, "manifest", lambda *a, **k: 1 / 0)
    with TestClient(A.app, raise_server_exceptions=False) as c:
        check(c.get("/api/manifest", params={"ra": RA0, "dec": DEC0}, headers=H))                     # 500
    assert set(seen) >= {400, 404, 413, 429, 500, 503, 504}, seen
    assert all(v == "*" for v in seen.values()), seen


def test_failed_new_field_does_not_count(client, monkeypatch):
    from server import app as A
    monkeypatch.setattr(A, "RATE_ON", True)
    monkeypatch.setattr(A, "NEW_FIELDS_PER_IP", 2)
    state = {"down": True}
    real = access.find_images

    def maybe_down(*a, **k):
        if state["down"]:
            raise access.SiaError("SIA2 HTTP 502")
        return real(*a, **k)
    monkeypatch.setattr(access, "find_images", maybe_down)
    for i in range(5):                                         # five failed attempts: none counted
        assert client.get("/api/manifest", params={"ra": RA0 + i, "dec": DEC0, "queue": 0}).status_code == 503
    state["down"] = False
    for i in range(2):                                         # the limit (2) is still all there
        assert client.get("/api/manifest", params={"ra": RA0 + 10 + i, "dec": DEC0, "queue": 0}).status_code == 200
    assert client.get("/api/manifest", params={"ra": RA0 + 20, "dec": DEC0, "queue": 0}).status_code == 429


# --------------------------------------------------------------------------- Gaia: gallery from its file, budget, honest notes

def test_gaia_gallery_from_file_free_field_budgeted(client, monkeypatch):
    import requests
    from server import app as A
    from server import pipeline as P
    calls = []

    def esa_down(*a, **k):
        calls.append(k.get("timeout"))
        raise requests.ConnectionError("connection reset by peer")
    monkeypatch.setattr(requests, "get", esa_down)
    # the client fixture replaced gaia_sources by a fake list: here it asks ESA (down) within a 3 s budget
    monkeypatch.setattr(P, "gaia_sources", lambda f, **k: P._gaia_query("SELECT 1", "test_" + f.key, budget_s=3.0))
    monkeypatch.setattr(A, "_gallery_gaia", {})
    # gallery target: from web/data/<key>/gaia.json, no ESA call
    g = client.get("/api/gaia", params={"key": "wise-0855"})
    assert g.status_code == 200 and isinstance(g.json(), list) and len(g.json()) > 0 and calls == []
    # free field, ESA down: 503 with a plain sentence, CORS, within the budget
    key = _strict(client.get("/api/manifest", params={"ra": RA0, "dec": DEC0}))["key"]
    t0 = __import__("time").time()
    r = client.get("/api/gaia", params={"key": key}, headers={"Origin": "https://sci-techspherex.com"})
    assert r.status_code == 503 and "Gaia archive is not answering" in r.json()["message"]
    assert r.headers["access-control-allow-origin"] == "*" and __import__("time").time() - t0 < 10
    # spectrum without Gaia: still made, says so, not cached
    _wait_idle()
    s = _strict(client.get("/api/spectrum", params={"key": key}))
    assert s["meta"]["gaia"].startswith("not available") and s["points"]
    monkeypatch.setattr(P, "gaia_sources", lambda f, **k: [{"ra": RA0, "dec": DEC0, "pmra": 0.0, "pmdec": 0.0,
                                                            "g_mag": 12.0, "ref_epoch": 2016.0}])
    s2 = _strict(client.get("/api/spectrum", params={"key": key}))          # asked again: Gaia is back
    assert s2["meta"]["gaia"].startswith("Gaia DR3")


def test_gaia_query_budget():
    import time
    import requests
    from server import pipeline as P
    t0 = time.time()

    def slow(*a, **k):
        time.sleep(min(k["timeout"][1], 2.0))
        raise requests.Timeout("read timed out")
    orig = requests.get
    requests.get = slow
    try:
        with pytest.raises(RuntimeError, match="unreachable"):
            P._gaia_query("SELECT 1", "budget_test_never_cached", budget_s=4.0)
    finally:
        requests.get = orig
    assert time.time() - t0 < 7


def test_track_inside_a_gallery_field_needs_no_irsa(tmp_path, monkeypatch):
    from pathlib import Path
    from server import app as A
    from server import pipeline as P
    gallery = Path(__file__).resolve().parent.parent / "web" / "data"
    if not (gallery / "_sia").is_dir():
        pytest.skip("no web/data/_sia")
    sia_calls = []

    def down(*a, **k):
        sia_calls.append(a)
        raise access.SiaError("SIA2 HTTP 502")

    def fake_fetch(refs, ra, dec, half_px=50, layers=("IMAGE", "FLAGS", "VARIANCE"), workers=32, progress=None,
                   stats=None, box_deg=None):
        rng = np.random.default_rng(len(refs))
        return [_cutout(r, ra, dec, min(half_px, 20), layers, rng) for r in refs], 0
    monkeypatch.setattr(access, "_sia_query", down)
    monkeypatch.setattr(access, "fetch_cutouts", fake_fetch)
    monkeypatch.setattr(access, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(A, "FIELDS_DIR", tmp_path / "fields")
    monkeypatch.setattr(A, "RATE_ON", False)
    monkeypatch.setattr(P, "gaia_path", lambda *a, **k: [])
    monkeypatch.setattr(P, "_gallery_field_cache", {})
    A.seed_sia_cache(gallery, tmp_path)
    rows = [r for f in (gallery / "_sia").rglob("133.75978_-7.24233_*.json")
            for r in json.loads(f.read_text(encoding="utf-8"))["rows"] if r["detector"] == 6]
    t = sorted(r["t_min"] for r in rows)[len(rows) // 2]          # a real D6 exposure of the WISE 0855 field
    marks = [{"ra": 133.7600, "dec": -7.2424, "mjd": t}, {"ra": 133.7601, "dec": -7.2424, "mjd": t + 0.05}]
    with TestClient(A.app) as c:
        r = c.post("/api/track", json={"band": "D6", "marks": marks})
        assert r.status_code == 200, r.text
        assert sia_calls == [] and r.json()["n_exposures_found"] > 0
        far = [{"ra": 140.0, "dec": 10.0, "mjd": t}, {"ra": 140.0001, "dec": 10.0, "mjd": t + 0.05}]   # outside
        assert c.post("/api/track", json={"band": "D6", "marks": far}).status_code == 503


# --------------------------------------------------------------------------- key and grid from one value

def test_key_and_grid_agree(client):
    # (a) the same 7' asked two ways: one key, one grid, also after a restart (reopened from the key)
    a = _strict(client.get("/api/manifest", params={"ra": RA0, "dec": DEC0, "fov": 0.1166}))
    b = _strict(client.get("/api/manifest", params={"ra": RA0, "dec": DEC0, "fov": 0.116666}))
    assert a["key"] == b["key"] and a["key"].endswith("_n140") and a["grid"]["shape"] == b["grid"]["shape"] == [140, 140]
    _wait_idle()
    client.new_registry()
    c = _strict(client.get("/api/band", params={"key": a["key"], "band": "D1"}))
    assert c["shape"] == [140, 140]
    # (b) frames are exactly the manifest's grid, and say so
    for fov in (0.05, 0.11667, 0.25):
        m = _strict(client.get("/api/manifest", params={"ra": RA0 + 1, "dec": DEC0, "fov": fov}))
        ny, nx = m["grid"]["shape"]
        bj = _strict(client.get("/api/band", params={"key": m["key"], "band": "D2"}))
        assert bj["shape"] == [ny, nx] and m["key"].endswith(f"_n{ny}")
        f = client.get("/api/frame", params={"key": m["key"], "band": "D2", "epoch": 0},
                       headers={"Origin": "https://sci-techspherex.com"})
        assert len(f.content) == 4 * ny * nx and f.headers["x-shape"] == f"{ny},{nx}"
        assert "x-shape" in f.headers["access-control-expose-headers"].lower()
    # (c) gallery keys are names and keep their 7' grid
    g = _strict(client.get("/api/manifest", params={"target": "wise-0855", "queue": 0}))
    assert g["key"] == "wise-0855" and g["grid"]["shape"] == [140, 140]


def test_old_style_keys_still_open_consistently():
    from server import app as A
    from server import pipeline as P
    k = A.parse_key("r10p0000_dp20p0000_f0p117")
    assert P.grid_px(k["fov_deg"]) == 142            # an old key reopens with the grid its own value gives
