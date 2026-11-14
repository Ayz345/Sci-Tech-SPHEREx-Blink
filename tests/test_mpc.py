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
