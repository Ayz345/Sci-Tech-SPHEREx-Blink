"""spx.mpc: ADES PSV and CSV drafts, checked against the ADES master file (cache/ades/adesmaster.xml).

    python -m pytest -q tests/test_mpc.py
"""
from __future__ import annotations

import pytest

from spx import mpc

pytestmark = pytest.mark.skipif(not mpc.MASTER.exists(), reason="cache/ades/adesmaster.xml not present")

# Values for the telescope context in TESTS ONLY: real ones must come from a document (see mpc.TELESCOPE).
TEST_TELESCOPE = {"design": "test-design", "aperture": "0.2", "detector": "test-detector"}


def _dets(n=3):
    out = []
    for i in range(n):
        out.append({"obs_time": f"2025-11-09T23:5{i}:58.863", "ra": 133.7605 + 0.001 * i, "dec": -7.2420 - 0.0002 * i,
                    "rms_ra": 0.36, "rms_dec": 0.41, "sc_pos_km": (3821.1041018897895, -4262.409661218919, -4079.7242300254366),
                    "sc_vel_kms": (-3.8325864583196534, 2.3358846931402435, -6.045714827964446), "exp_s": 113.5826,
                    "snr": 12.5, "obs_id": f"2025W45_2A_0746_{i}", "band": "D6", "wavelength_um": 4.805})
    return out


@pytest.fixture()
def telescope(monkeypatch):
    monkeypatch.setattr(mpc, "TELESCOPE", dict(TEST_TELESCOPE))


def test_psv_passes_the_master_checks(telescope):
    txt = mpc.write_ades_psv(_dets(), {"submitter": "A. Tester", "tool_version": "0.1", "code_hash": "abc123"})
    assert mpc.validate_psv(txt) == []
    lines = txt.splitlines()
    assert lines[0] == "# version=2022"
    assert "! mpcCode 288" in lines
    assert any(ln.startswith("! line DRAFT") for ln in lines)
    assert any("unverified: mode, astCat" in ln for ln in lines)
    kw = [t.strip() for t in next(ln for ln in lines if ln.startswith("trkSub")).split("|")]
    assert kw[:3] == ["trkSub", "mode", "stn"] and "pos1" in kw and "obsTime" in kw
    row = dict(zip(kw, [t.strip() for t in lines[lines.index(next(ln for ln in lines if ln.startswith("trkSub"))) + 1].split("|")]))
    assert row["obsTime"] == "2025-11-09T23:50:58.863Z"      # mid-exposure as given, UTC
    assert row["sys"] == "ICRF_KM" and row["ctr"] == "399" and row["stn"] == "288"
    assert row["rmsRA"] == "0.36" and row["rmsDec"] == "0.41"  # two significant figures (allowed: one or two)
    assert row["ra"] == "133.76050"                          # 0.36"/cos(dec) -> 1e-4 deg -> 5 decimals
    assert row["dec"] == "-7.24200"
    assert row["logSNR"] == "1.10" and row["exp"] == "113.58"


def test_trksub_and_required_inputs(telescope):
    d = _dets()
    with pytest.raises(ValueError, match="submitter"):
        mpc.write_ades_psv(d, {"submitter": ""})
    with pytest.raises(ValueError, match="trkSub"):
        mpc.write_ades_psv(d, {"submitter": "X", "trk_sub": "too-long-name"})
    with pytest.raises(ValueError, match="at least"):
        mpc.write_ades_psv(d[:2], {"submitter": "X"})
    t = mpc.make_trksub(d)
    assert len(t) == 8 and mpc._TRK_RE.fullmatch(t) and t == mpc.make_trksub(d)
    txt = mpc.write_ades_psv(d, {"submitter": "X", "trk_sub": "K25ab-1"})
    assert "K25ab-1" in txt and mpc.validate_psv(txt) == []


def test_no_ades_without_verified_telescope():
    with pytest.raises(ValueError, match="telescope context not verified"):
        mpc.write_ades_psv(_dets(), {"submitter": "X"})


def test_validator_catches_errors(telescope):
    good = mpc.write_ades_psv(_dets(), {"submitter": "A. Tester"})
    assert mpc.validate_psv(good.replace("# version=2022", "# version=2017"))
    assert mpc.validate_psv(good.replace("ICRF_KM", "ICRF_XX"))
    assert mpc.validate_psv(good.replace("|  399|", "|  398|").replace("|399|", "|398|"))
    assert mpc.validate_psv(good.replace("2025-11-09T23:50:58.863Z", "2025-11-09 23:50:58Z"))
    assert mpc.validate_psv(good.replace("# telescope", "# telescopeX"))
    bad_rms = good.replace("0.36", "0.36123456")
    assert any("rmsRA" in e for e in mpc.validate_psv(bad_rms))


def test_uncertainty_digits():
    assert mpc._sig2(0.0123) == "0.012" and mpc._sig2(1.27) == "1.3" and mpc._sig2(0.36) == "0.36"
    assert mpc._dp(0.36 / 3600) == 5 and mpc._dp(3.6 / 3600) == 4


def test_csv_always_available():
    txt = mpc.write_csv(_dets(2), {"note": "Barnard's Star: not for the MPC (a star)."})
    assert txt.startswith("# Measurements on single SPHEREx exposures")
    assert "trk_sub,obs_time_utc_mid,ra_deg" in txt and txt.count("\n") == 5


def _psv_parse(txt):
    ctx, cur, kw, rows = [], None, None, []
    for ln in txt.splitlines()[1:]:
        if ln.startswith("#"):
            cur = (ln[1:].strip(), [])
            ctx.append(cur)
        elif ln.startswith("!"):
            k, v = ln[1:].strip().split(None, 1)
            cur[1].append((k, v))
        elif ln.strip():
            toks = [t.strip() for t in ln.split("|")]
            if kw is None:
                kw = toks
            else:
                rows.append({k: v for k, v in zip(kw, toks) if v != ""})
    return ctx, rows


def test_psv_and_xml_carry_the_same_content(telescope):
    from lxml import etree
    d = _dets(4)
    c = {"submitter": "A. Tester", "trk_sub": "SPXtest1", "tool_version": "0.1", "code_hash": "abc"}
    pctx, prows = _psv_parse(mpc.write_ades_psv(d, c))
    root = etree.fromstring(mpc.write_ades_xml(d, c).encode())
    assert root.tag == "ades" and root.get("version") == "2022"
    xctx = [(e.tag, [(ch.tag, ch.text) for ch in e]) for e in root.find("obsBlock/obsContext")]
    xrows = [{ch.tag: ch.text for ch in o} for o in root.findall("obsBlock/obsData/optical")]
    assert pctx == xctx
    assert prows == xrows and len(prows) == 4
    assert [list(r) for r in xrows] == [[f for f in mpc.FIELDS if f in r] for r in xrows]  # schema order


def test_each_exposure_keeps_its_own_spacecraft_position(telescope):
    d = _dets(3)
    for i, x in enumerate(d):  # spacecraft moves ~7 km/s: 60 s apart -> hundreds of km
        x["sc_pos_km"] = (3821.104 + 230.0 * i, -4262.410 + 140.0 * i, -4079.724 - 363.0 * i)
        x["sc_vel_kms"] = (-3.8326 + 0.01 * i, 2.3359, -6.0457)
    _, rows = _psv_parse(mpc.write_ades_psv(d, {"submitter": "X"}))
    assert [r["pos1"] for r in rows] == ["+3821.104", "+4051.104", "+4281.104"]
    assert [r["pos3"] for r in rows] == ["-4079.724", "-4442.724", "-4805.724"]
    assert len({r["vel1"] for r in rows}) == 3
    assert len({r["obsTime"] for r in rows}) == 3
