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
                   stats=None):
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


def _strict(r):
    return json.loads(r.content, parse_constant=lambda c: (_ for _ in ()).throw(AssertionError(f"non-finite {c}")))


def test_manifest_band_frame(client):
    r = client.get("/api/manifest", params={"ra": RA0, "dec": DEC0, "fov": 0.1})
    assert r.status_code == 200 and r.headers["access-control-allow-origin"] == "*"
    m = _strict(r)
    assert m["schema"] == 1 and m["key"] == "r210p1230_dm35p4560_f0p100"
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

    # same request again: no new SIA2 query, no new download
    n_sia, n_fetch = client.calls["sia"], client.calls["fetch"]
    assert client.get("/api/manifest", params={"ra": RA0, "dec": DEC0, "fov": 0.1}).status_code == 200
    assert client.get("/api/band", params={"key": m["key"], "band": "D3"}).status_code == 200
    assert (client.calls["sia"], client.calls["fetch"]) == (n_sia, n_fetch)

    assert client.get("/api/frame", params={"key": m["key"], "band": "D3", "epoch": 7}).status_code == 404
    assert client.get("/api/frame", params={"key": m["key"], "band": "D3", "epoch": 0, "kind": "x"}).status_code == 400
    assert client.get("/api/band", params={"key": m["key"], "band": "D9"}).status_code == 404


def test_spectrum_and_gaia(client):
    m = client.get("/api/manifest", params={"ra": RA0, "dec": DEC0}).json()
    client.get("/api/band", params={"key": m["key"], "band": "D1"})
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
    assert a["key"] == "r210p1230_dm35p4560_f0p100"
    b = client.get("/api/manifest", params={"ra": -0.5, "dec": 10}).json()
    assert b["key"].startswith("r359p5000_dp10p0000")


def test_key_roundtrip_reopens_after_restart(client, monkeypatch):
    from server import app as A
    m = client.get("/api/manifest", params={"ra": RA0, "dec": DEC0, "fov": 0.1}).json()
    client.get("/api/band", params={"key": m["key"], "band": "D2"})
    client.new_registry()  # a restarted server: only the disk is left
    r = client.get("/api/band", params={"key": m["key"], "band": "D2"})
    assert r.status_code == 200 and _strict(r)["band"] == "D2"
    assert A.parse_key(m["key"]) == {"ra": 210.123, "dec": -35.456, "fov_deg": 0.1}


def test_resolve_named_target_offline(client):
    r = _strict(client.get("/api/resolve", params={"name": "Barnard's Star"}))
    assert r["target"] == "barnard" and math.isclose(r["pmdec"], 10362.394)
    r = _strict(client.get("/api/resolve", params={"name": "wise 0855"}))
    assert r["target"] == "wise-0855"


def test_health(client):
    h = _strict(client.get("/api/health"))
    assert h["ok"] and h["collections"] and h["live_layers"] == ["IMAGE", "FLAGS"]


def test_json_safe():
    from server import pipeline as P
    assert P.dumps({"a": float("nan"), "b": [np.float32(np.inf), 1.5], "c": np.int64(3)}) == \
        '{"a": null, "b": [null, 1.5], "c": 3}'
    assert P.free_key(-0.00001, -0.0, 0.11666) == "r0p0000_dp0p0000_f0p117"
