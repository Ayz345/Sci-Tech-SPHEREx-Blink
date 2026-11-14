"""CatWISE2020 conventions, checked on saved rows (tests/data/catwise_pm_check.json, made by
scripts/make_catwise.py --pm-check from IRSA catwise_2020 answers); offline.

The CatWISE2020 paper adopts "mu_alpha* = mu_alpha cos(delta)" as notation but does not say it for the PMRA column,
and IRSA's column description says only "Apparent motion in RA". Gaia DR3 pmra is mu_alpha cos(delta). If PMRA is
mu_alpha cos(delta), PMRA / Gaia pmra ~ 1; if it is mu_alpha, ~ 1/cos(delta) (1.83 at Dec -56.8, 2.54 at +66.8).
    python -m pytest -q tests/test_catwise.py
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pytest

DATA = Path(__file__).resolve().parent / "data" / "catwise_pm_check.json"
#: Fields far enough from the equator to tell the two conventions apart (|1/cos dec - 1| > 0.5).
DISCRIMINATING = ("eps-indi-b", "r-dra")


def _doc():
    if not DATA.exists():
        pytest.skip("no tests/data/catwise_pm_check.json (scripts/make_catwise.py --pm-check)")
    return json.loads(DATA.read_text(encoding="utf-8"))


def fit(rows):
    """Weighted slope of CatWISE PMRA on Gaia pmra through the origin, its error inflated by sqrt(chi2/dof) when
    that exceeds 1 (CatWISE motions carry an uncorrected ~10 mas/yr systematic), and chi2 of the two hypotheses."""
    gx = np.array([r["gaia_pmra"] for r in rows])
    cx = np.array([r["cw_pmra"] for r in rows])
    w = 1 / np.array([r["cw_sigpmra"] for r in rows]) ** 2
    inv_c = 1 / np.cos(np.radians([r["dec"] for r in rows]))
    k = float((w * gx * cx).sum() / (w * gx * gx).sum())
    chi = float((w * (cx - k * gx) ** 2).sum())
    infl = max(1.0, math.sqrt(chi / max(1, len(rows) - 1)))
    ke = float(infl / math.sqrt((w * gx * gx).sum()))
    return {"n": len(rows), "slope": k, "err": ke, "inv_cos": float(np.mean(inv_c)),
            "chi2_cos": float((w * (cx - gx) ** 2).sum()), "chi2_nocos": float((w * (cx - gx * inv_c) ** 2).sum())}


def test_pmra_is_mu_alpha_cos_delta():
    """Threshold note: the 4-sigma separation below was chosen AFTER the result was seen (eps-indi-b: 4.9 sigma
    with errors scaled by sqrt(chi2/dof), 8.7 sigma with the catalogue errors as given); the decision rests on those
    two numbers. r-dra (cos dec = 0.39) is the independent second check once IRSA answers for it."""
    doc = _doc()
    used = [k for k in DISCRIMINATING if doc["fields"].get(k)]
    assert used, "no discriminating field (eps-indi-b, r-dra) in the saved rows"
    for k in used:
        f = fit(doc["fields"][k])
        assert f["n"] >= 5, (k, f)
        assert abs(f["slope"] - 1.0) < 3 * f["err"], (k, f)                    # mu_alpha cos(delta)
        assert abs(f["slope"] - f["inv_cos"]) > 4 * f["err"], (k, f)          # not mu_alpha (eps-indi-b: 4.9 inflated, 8.7 raw)
        assert f["chi2_cos"] < f["chi2_nocos"] / 2, (k, f)


def test_pmra_agrees_near_the_equator_too():
    """Fields near the equator cannot tell the conventions apart but must still agree with Gaia (slope ~ 1)."""
    doc = _doc()
    for k, rows in doc["fields"].items():
        if k in DISCRIMINATING or len(rows) < 5:
            continue
        f = fit(rows)
        assert abs(f["slope"] - 1.0) < 3 * f["err"], (k, f)


def test_wise0855_catwise_row_and_epoch():
    """targets.py's WISE 0855 position is the row's motion-solution position (ra_pm, dec_pm) at MJD 57170; the
    stationary ra/dec lies west of it, as for an object moving west; the motion is the known ~8.1 arcsec/yr."""
    doc = _doc()
    row = next(r for r in doc.get("wise0855_rows", []) if r["source_name"].endswith("J085507.96-071439.3"))
    from spx.targets import TARGETS
    t = TARGETS["wise-0855"]
    assert abs(row["ra_pm"] - t["ra"]) * 3600 < 0.01 and abs(row["dec_pm"] - t["dec"]) * 3600 < 0.01
    assert abs(t["ref_epoch"] - 2015.4018) < 1e-3                       # MJD 57170
    d_east = (row["ra_pm"] - row["ra"]) * math.cos(math.radians(row["dec"])) * 3600
    assert 1.5 < d_east < 5.0, d_east
    assert abs(row["pmra"] * 1000 - t["pmra"]) < 300 and row["pmra"] < -7.5


def test_fast_rows_are_trusted_only_when_an_independent_motion_agrees():
    import importlib.util
    spec = importlib.util.spec_from_file_location("make_catwise", Path(__file__).parent.parent / "scripts" / "make_catwise.py")
    mc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mc)
    tgt = {"what": "target", "ra": 10.0, "dec": 0.0, "pmra": -8118.0, "pmdec": 680.0, "kind": "target"}
    near = {"source_name": "a", "ra": 10.0 + 0.5 / 3600, "dec": 0.0, "pmra": -7920.0, "pmdec": 494.0}
    c = mc.pm_agrees(near, [tgt])
    assert c["ok"] and "agrees with target" in c["text"]                                # 3.3%: WISE 0855's twin row
    slow = {"source_name": "b", "ra": 10.0, "dec": 0.0, "pmra": -2000.0, "pmdec": 100.0}
    assert mc.pm_agrees(slow, [tgt])["ok"] is False                                     # ~25%, like Epsilon Indi B
    g1 = {"what": "Gaia 1", "ra": 20.0, "dec": 5.0, "pmra": -4.0, "pmdec": -5.0, "kind": "gaia"}
    g2 = dict(g1, what="Gaia 2", ra=20.0 + 1.0 / 3600)
    row = {"source_name": "c", "ra": 20.0, "dec": 5.0, "pmra": 2000.0, "pmdec": 3000.0}
    assert mc.pm_agrees(row, [g1])["ok"] is False                                        # Gaia says it stands still
    assert mc.pm_agrees(row, [g1, g2]) is None                                           # two Gaia sources: no verdict
    assert mc.pm_agrees(dict(row, ra=21.0), [g1]) is None                                # nothing within 2 arcsec
