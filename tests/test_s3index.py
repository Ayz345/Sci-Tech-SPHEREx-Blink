"""Our own index of the public bucket (server/s3index.py) and its use for image lists (server.pipeline.find_images),
offline: a synthetic scan chunk, IRSA made to fail.
    python -m pytest -q tests/test_s3index.py
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent))
from spx import access  # noqa: E402

COLS = ["CRPIX1", "CRPIX2", "PC1_1", "PC1_2", "PC2_1", "PC2_2", "CDELT1", "CDELT2", "CRVAL1", "CRVAL2", "MJD-BEG",
        "MJD-AVG", "MJD-END", "TELAPSE"]
RA0, DEC0 = 210.123, -35.456
SCALE = 6.15 / 3600


def _chunk(path: Path, rows):
    """rows: (key, det, crval1, crval2, rot_deg, mjd_beg)"""
    keys, dets, vals = [], [], []
    for key, det, c1, c2, rot, t in rows:
        a = math.radians(rot)
        pc = [-SCALE * math.cos(a), SCALE * math.sin(a), SCALE * math.sin(a), SCALE * math.cos(a)]
        keys.append(key)
        dets.append(det)
        vals.append([1020.5, 1020.5, *pc, 1.0, 1.0, c1, c2, t, t + 0.00068, t + 0.00137, 118.19])
    np.savez_compressed(path, key=np.array(keys), det=np.array(dets, np.int8), cols=np.array(COLS),
                        val=np.array(vals, float))


def _key(rel, plan, ver, seq, sub, det):
    return f"{rel}/level2/{plan}/{ver}/{det}/level2_{plan}_{seq:04d}_{sub}D{det}_spx_{ver}.fits"


@pytest.fixture()
def index(tmp_path):
    from server import s3index as X
    rows = []
    for e, t in enumerate((60800.5, 60990.5, 61170.5)):
        plan = ("2025W19_1B", "2025W45_2A", "2026W15_1A")[e]
        for det in range(1, 7):
            for i in range(3):
                rows.append((_key("dr1", plan, "l2b-v27-2026-197", 100 + i, 1, det), det, RA0 + 0.3, DEC0 - 0.2,
                             20.0, t + i * 0.01))
    # a reprocessed copy of one exposure (the retry must win) and an image elsewhere in the sky
    rows.append((_key("dr1", "2025W19_1B", "l2b_retry-v27-2026-197", 100, 1, 3), 3, RA0 + 0.3, DEC0 - 0.2, 20.0, 60800.5))
    rows.append((_key("dr1", "2025W19_1B", "l2b-v27-2026-197", 900, 1, 1), 1, RA0 + 40, DEC0, 0.0, 60800.6))
    rows.append(("dr1/level2/odd/name_not_in_the_pattern_D2.fits", 2, RA0 + 40, DEC0, 0.0, 60800.7))
    _chunk(tmp_path / "dr1__test.npz", rows)
    (tmp_path / "dr1__partial.tmp.npz").write_bytes(b"being written")
    out = tmp_path / "index" / "spx_index.npz"
    rep = X.build_compact(sorted(tmp_path.glob("*.npz")), out, "2026-11-15T15:00:00Z")
    return X.S3Index.load(out), rep


def test_compact_index_round_trip_and_query(index):
    idx, rep = index
    assert rep["rows_kept"] == 3 * 6 * 3 + 3 and rep["raw_keys"] == 1 and rep["chunks"] == 1      # .tmp not read
    assert rep["encoding_error"]["crval_arcsec"] < 0.2
    rows = idx.rows_at(RA0, DEC0, "spherex_dr1")
    assert len(rows) == 3 * 6 * 3                                     # one row per exposure and detector
    r = next(x for x in rows if x["obs_id"] == "2025W19_1B_0100_1" and x["detector"] == 3)
    assert "l2b_retry" in r["s3_key"] and r["access_url"].endswith(r["s3_key"])
    assert abs(r["t_min"] - 60800.5) * 86400 < 0.1 and r["bandpass"] == "SPHEREx-D3"
    assert r["s_region"].startswith("POLYGON ICRS ") and len(r["s_region"].split()) == 12
    assert idx.rows_at(RA0, DEC0, "spherex_qr3") == []                # collection not in the index


def test_footprint_edges(index):
    idx, _ = index
    # the detector spans +-1020 px (6.15"/px) around its centre: just inside, inside the margin, outside
    c1, c2 = RA0 + 40, DEC0                     # the unrotated detector: its y axis points north
    half = 1020 * SCALE
    cosd = math.cos(math.radians(c2))

    def n_at(dx_deg):
        return len(idx.indices_at(c1, c2 + dx_deg))
    assert n_at(0.0) > 0 and n_at(half * 0.98) > 0
    assert n_at(half + 30 * SCALE) > 0                                # within the 40 px margin
    assert n_at(half + 80 * SCALE) == 0                               # beyond it
    assert len(idx.indices_at(c1 + 3.0 / cosd, c2)) == 0              # another part of the sky
    assert len(idx.indices_at(RA0 + 0.3, DEC0 - 0.2)) == 3 * 6 * 3 + 1   # the rotated ones (+ the retry copy)


def test_irsa_down_free_field_opens_from_the_index(index, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from server import app as A
    from server import pipeline as P
    import test_server as TS
    idx, _ = index

    def down(*a, **k):
        raise access.SiaError("SIA2 HTTP 502")

    def fake_fetch(refs, ra, dec, half_px=50, layers=("IMAGE", "FLAGS", "VARIANCE"), workers=32, progress=None,
                   stats=None, box_deg=None):
        rng = np.random.default_rng(len(refs))
        return [TS._cutout(r, ra, dec, min(half_px, 20), layers, rng) for r in refs], 0
    monkeypatch.setattr(access, "_sia_query", down)
    monkeypatch.setattr(access, "fetch_cutouts", fake_fetch)
    monkeypatch.setattr(access, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(A, "FIELDS_DIR", tmp_path / "fields")
    monkeypatch.setattr(A, "RATE_ON", False)
    monkeypatch.setattr(P, "gaia_sources", lambda f, **k: [])
    monkeypatch.setattr(P, "_index_state", {"loaded": True, "indexes": [idx]})
    reg = A.Registry()
    monkeypatch.setattr(A, "REG", reg)
    try:
        with TestClient(A.app) as c:
            m = c.get("/api/manifest", params={"ra": RA0, "dec": DEC0, "queue": 0})
            assert m.status_code == 200, m.text
            m = m.json()
            assert m["n_images"] == 54 and len(m["epochs"]) == 3 and "our own index" in m["image_list"]
            b = c.get("/api/band", params={"key": m["key"], "band": "D1"})
            assert b.status_code == 200 and sum(e["n_used"] for e in b.json()["epochs"]) > 0
    finally:
        reg.close()
    cached = json.loads(access._sia_cache_path(RA0, DEC0, "spherex_dr1", access.SIA_RADIUS_DEG).read_text())
    assert cached["source"].startswith("s3index") and len(cached["rows"]) == 54


def test_a_cached_list_is_kept_over_the_index(index, tmp_path, monkeypatch):
    from server import pipeline as P
    idx, _ = index
    monkeypatch.setattr(access, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(access, "_sia_query", lambda *a, **k: (_ for _ in ()).throw(access.SiaError("down")))
    monkeypatch.setattr(P, "_index_state", {"loaded": True, "indexes": [idx]})
    path = access._sia_cache_path(RA0, DEC0, "spherex_dr1", access.SIA_RADIUS_DEG)
    path.parent.mkdir(parents=True)
    row = {"obs_id": "X_0001_1", "detector": 1, "collection": "spherex_dr1", "s3_key": "k", "access_url": "",
           "t_min": 60800.0, "t_max": 60800.001, "em_min_um": 0.75, "em_max_um": 1.09, "bandpass": "SPHEREx-D1",
           "exptime_s": 113.6, "s_region": ""}
    path.write_text(json.dumps({"ra": RA0, "dec": DEC0, "collection": "spherex_dr1", "radius_deg": 0.001,
                                "queried": 0, "rows": [row]}))
    refs = [r for r in P.find_images(RA0, DEC0) if r.collection == "spherex_dr1"]
    assert [r.obs_id for r in refs] == ["X_0001_1"]                  # the seed / IRSA list wins


def test_no_index_means_no_change(tmp_path, monkeypatch):
    from server import pipeline as P
    monkeypatch.setattr(P, "_index_state", {"loaded": False, "indexes": []})
    monkeypatch.setattr(P, "INDEX_PATH", str(tmp_path / "missing"))
    assert P.s3_indexes() == [] and P.index_note() is None
    calls = []
    monkeypatch.setattr(access, "find_images", lambda ra, dec, *a, **k: calls.append((ra, dec)) or [])
    assert P.find_images(10.0, 20.0) == [] and calls == [(10.0, 20.0)]


def test_health_reports_the_index(index, monkeypatch):
    from fastapi.testclient import TestClient
    from server import app as A
    from server import pipeline as P
    idx, _ = index
    monkeypatch.setattr(P, "_index_state", {"loaded": True, "indexes": [idx]})
    with TestClient(A.app) as c:
        h = c.get("/api/health").json()
    assert h["image_index"]["rows"] == idx.n and h["image_index"]["built_utc"].startswith("2026-11-15")
    monkeypatch.setattr(P, "_index_state", {"loaded": True, "indexes": []})
    with TestClient(A.app) as c:
        assert c.get("/api/health").json()["image_index"] is None


def test_health_never_loads_the_index_and_callers_wait_for_the_load(index, monkeypatch):
    import threading
    import time
    from fastapi.testclient import TestClient
    from server import app as A
    from server import pipeline as P
    from server import s3index as X
    idx, _ = index
    monkeypatch.setattr(P, "_index_state", {"loaded": False, "indexes": []})
    monkeypatch.setattr(P, "INDEX_PATH", idx.meta["path"])
    started, release = threading.Event(), threading.Event()
    real = X.S3Index.load

    def slow_load(path):
        started.set()
        release.wait(5)
        return real(path)
    monkeypatch.setattr(X.S3Index, "load", staticmethod(slow_load))
    monkeypatch.setattr(A, "_pool_set", True)                 # no start-up thread in this test
    with TestClient(A.app) as c:
        assert c.get("/api/health").json()["image_index"] == {"loading": True}   # health does not wait or load
    got = {}
    t1 = threading.Thread(target=lambda: got.setdefault("a", P.s3_indexes()))
    t1.start()
    assert started.wait(5)
    t2 = threading.Thread(target=lambda: got.setdefault("b", P.s3_indexes()))
    t2.start()
    time.sleep(0.2)
    assert "b" not in got                                       # the second caller waits for the load ...
    release.set()
    t1.join(5)
    t2.join(5)
    assert len(got["a"]) == 1 and got["b"] is got["a"]          # ... and gets the index, not an empty list
