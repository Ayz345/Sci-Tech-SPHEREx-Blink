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
                    "snr_calibrated": 12.5, "finast": 0, "obs_id": f"2025W45_2A_0746_{i}", "band": "D6", "wavelength_um": 4.805})
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
    assert not any("unverified" in ln for ln in lines) and "Gaia3" in txt
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
        mpc.write_ades_psv(d, {"submitter": "I. M. Tester", "trk_sub": "too-long-name"})
    with pytest.raises(ValueError, match="at least"):
        mpc.write_ades_psv(d[:2], {"submitter": "I. M. Tester"})
    t = mpc.make_trksub(d)
    assert len(t) == 7 and mpc._TRK_RE.fullmatch(t) and t == mpc.make_trksub(d)
    txt = mpc.write_ades_psv(d, {"submitter": "I. M. Tester", "trk_sub": "K25ab-1"})
    assert "K25ab-1" in txt and mpc.validate_psv(txt) == []


def test_no_ades_without_verified_telescope(monkeypatch):
    monkeypatch.setattr(mpc, "TELESCOPE", {**mpc.TELESCOPE, "aperture": None})
    with pytest.raises(ValueError, match="telescope context not verified"):
        mpc.write_ades_psv(_dets(), {"submitter": "I. M. Tester"})


def test_real_telescope_context_and_finast():
    txt = mpc.write_ades_psv(_dets(), {"submitter": "A. Tester"})
    assert mpc.validate_psv(txt) == []
    for line in ("! design reflector", "! aperture 0.20", "! detector HgCdTe", "! fRatio 3",
                 "! line Telescope: SPHEREx Explanatory Supplement v2.1, Sec. 1.3 and Table 2",
                 "! line " + mpc.SCHEMA_NOT_CHECKED):
        assert line in txt.splitlines()
    assert "Gaia DR3" in txt and "Gaia3" in txt and "unverified" not in txt
    assert "unverified: spacecraft position axes" not in txt
    assert all(len(ln) - len("! line ") <= 100 for ln in txt.splitlines() if ln.startswith("! line "))
    d = _dets(4)
    d[1]["finast"] = 1                      # left out of ADES, kept in the CSV with the reason
    rows = _psv_parse(mpc.write_ades_psv(d, {"submitter": "I. M. Tester"}))[1]
    assert len(rows) == 3 and all("_1 " not in r.get("remarks", "") + " " for r in rows)
    csv_txt = mpc.write_csv(d, {})
    assert "FINAST=1" in csv_txt and csv_txt.count("False") == 1
    d[2]["finast"] = 2
    with pytest.raises(ValueError, match="at least"):
        mpc.write_ades_psv(d, {"submitter": "I. M. Tester"})


def test_logsnr_only_from_calibrated_snr():
    d = _dets()
    for x in d:
        x.pop("snr_calibrated", None)
        x["snr_fit"] = 20.0
    assert _row_logsnr(d) == [None, None, None]


def _row_logsnr(d):
    rows = _psv_parse(mpc.write_ades_psv(d, {"submitter": "I. M. Tester"}))[1]
    return [r.get("logSNR") for r in rows]


def test_logsnr_is_the_image_snr():
    d = _dets()                                     # snr_calibrated 12.5
    for x in d:
        x["snr_fit"] = 5.0                          # low fit S/N must not lower logSNR any more
    d[0]["snr_variance"], d[0]["snr_image"] = 120.0, 120.0
    d[1]["snr_variance"], d[1]["snr_image"] = 300.0, 12.5
    d[2]["snr_variance"], d[2]["snr_image"] = 80.0, None    # no calibrated S/N on that row -> no logSNR
    assert _row_logsnr(d) == ["2.08", "1.10", None]
    csv_txt = mpc.write_csv(d, {})
    head = next(ln for ln in csv_txt.splitlines() if ln.startswith("trk_sub"))
    assert ",snr_calibrated,snr_fit,snr_variance,snr_image," in head
    assert ",12.5,5.0,120.0,120.0," in csv_txt


def test_acceptance_and_logsnr_are_described_apart():
    txt = mpc.write_ades_psv(_dets(), {"submitter": "I. M. Tester"})
    lines = [ln[7:] for ln in txt.splitlines() if ln.startswith("! line ")]
    if mpc.snr_threshold() is not None:
        assert any(ln.startswith("Reported only if min(fit S/N, calibrated S/N) >= ") for ln in lines)
        assert "logSNR is another number: S/N of the source in the image (VARIANCE and empty-sky noise)." in lines
    assert not any("Only detections with S/N" in ln for ln in lines)


def test_logsnr_is_the_smaller_of_fit_and_calibrated():
    """Rows without snr_image (measured before the field existed)."""
    d = _dets()                                     # snr_calibrated 12.5
    d[0]["snr_fit"] = 5.0                           # fit lower -> log10(5)
    d[1]["snr_fit"] = 375.0                         # fit 30x higher -> calibrated
    d[2]["snr_fit"] = None                          # no fit S/N -> calibrated
    assert _row_logsnr(d) == ["0.70", "1.10", "1.10"]
    assert mpc.log_snr_value({"snr_fit": 5.0, "snr_calibrated": None}) is None
    assert mpc.log_snr_value({"snr_fit": 5.0, "snr_calibrated": 0.0}) is None
    assert mpc.log_snr_value({"snr_fit": float("nan"), "snr_calibrated": 8.0}) == 8.0


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
    assert "trk_sub,obs_time_utc_mid,ra_deg" in txt and txt.count("\n") == 5 and "in_ades,reason" in txt


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
    c = {"submitter": "A. Tester", "trk_sub": "SPXtst1", "tool_version": "0.1", "code_hash": "abc",
         "test_form": mpc.MPC_PSV_TEST}   # the two writers otherwise point to their own MPC test form
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
    _, rows = _psv_parse(mpc.write_ades_psv(d, {"submitter": "I. M. Tester"}))
    assert [r["pos1"] for r in rows] == ["+3821.104", "+4051.104", "+4281.104"]
    assert [r["pos3"] for r in rows] == ["-4079.724", "-4442.724", "-4805.724"]
    assert len({r["vel1"] for r in rows}) == 3
    assert len({r["obsTime"] for r in rows}) == 3


def test_ades_selection_rules_and_validation_lines(tmp_path, monkeypatch):
    import json
    d = _dets(6)
    d[0]["trailed"] = True
    d[1]["accepted"], d[1]["reason"] = False, "blended"
    d[2]["regime"] = "between_epochs"
    for x in d[3:]:
        x["regime"], x["accepted"], x["trailed"] = "within_visit", True, False
    rows = _psv_parse(mpc.write_ades_psv(d, {"submitter": "I. M. Tester"}))[1]
    assert len(rows) == 3
    csv_txt = mpc.write_csv(d, {})
    for why in ("trailed", "blended", "regime between_epochs"):
        assert why in csv_txt
    monkeypatch.setattr(mpc, "VALIDATION_FILE", tmp_path / "missing.json")
    txt = mpc.write_ades_psv(d, {"submitter": "I. M. Tester"})
    assert "rmsTime" not in txt and "Method validation summary not available" in txt
    assert "obsTime is the header mid-exposure time (DATE-AVG); not independently verified." in txt
    import spx
    f = tmp_path / "v.json"
    f.write_text(json.dumps({"object": "(433) Eros", "frame_code_hash": spx.frame_code_hash(), "wcs_floor_arcsec": 0.23,
                             "eros": {"median_dra_arcsec": -0.123, "median_ddec_arcsec": -0.053,
                                      "scatter_ra_arcsec": 0.175, "scatter_dec_arcsec": 0.158,
                                      "median_along_track_arcsec": -0.146, "n_accepted": 382, "n_total": 450},
                             "second_object": {"object": "(31) Euphrosyne", "n_accepted": 275, "n_total": 350,
                                               "median_along_track_arcsec": 0.009, "median_cross_track_arcsec": 0.142},
                             "timing": {"verified": False, "second_object_time_offset_s": [1.31, 2.34],
                                        "note": "Not verified. ... a time offset larger than ~5 s is not supported."}}),
                 encoding="utf-8")
    monkeypatch.setattr(mpc, "VALIDATION_FILE", f)
    txt = mpc.write_ades_psv(d, {"submitter": "I. M. Tester"})
    lines = [ln[7:] for ln in txt.splitlines() if ln.startswith("! line ")]
    assert "Method validated on (433) Eros against JPL Horizons: median offset -0.12, -0.05 arcsec;" in lines
    assert "(31) Euphrosyne +0.01 arcsec (275/350 exposures) = +1.3 +- 2.3 s." in lines
    assert "A time offset larger than ~5 s is not supported (see the tool's Methods page)." in lines
    assert not any("session" in ln for ln in lines)
    assert "Object-dependent systematics up to 0.15 arcsec remain unexplained." in lines
    assert all(len(x) <= 100 for x in lines) and mpc.validate_psv(txt) == []


def _validation_json(tmp_path, drop=()):
    import json
    import spx
    v = {"object": "(433) Eros", "frame_code_hash": spx.frame_code_hash(), "wcs_floor_arcsec": 0.23,
         "eros": {"median_dra_arcsec": -0.123, "median_ddec_arcsec": -0.053, "scatter_ra_arcsec": 0.175,
                  "scatter_dec_arcsec": 0.158, "median_along_track_arcsec": -0.146, "n_accepted": 382, "n_total": 450},
         "second_object": {"object": "(31) Euphrosyne", "n_accepted": 275, "n_total": 350,
                           "median_along_track_arcsec": 0.009, "median_cross_track_arcsec": 0.142},
         "timing": {"verified": False, "second_object_time_offset_s": [1.31, 2.34],
                    "note": "Not verified. ... a time offset larger than ~5 s is not supported."}}
    for path in drop:
        *head, last = path.split(".")
        cur = v
        for k in head:
            cur = cur[k]
        del cur[last]
    f = tmp_path / "v.json"
    f.write_text(json.dumps(v), encoding="utf-8")
    return f


def test_validation_without_second_object_keeps_the_first(tmp_path, monkeypatch):
    monkeypatch.setattr(mpc, "VALIDATION_FILE", _validation_json(tmp_path, drop=["second_object"]))
    v = mpc.validation_summary()
    assert v is not None and v["second_available"] is False and v["object2"] is None
    lines = mpc.validation_lines(v)
    assert "Method validated on (433) Eros against JPL Horizons: median offset -0.12, -0.05 arcsec;" in lines
    assert "second object: not available." in lines
    assert not any("Euphrosyne" in ln or "systematics" in ln for ln in lines)
    txt = mpc.write_ades_psv(_dets(), {"submitter": "I. M. Tester"})
    assert "! line second object: not available." in txt and mpc.validate_psv(txt) == []
    st = mpc.validation_status()
    assert st["status"] == "partial" and "object2" in st["missing_keys"] and st["same_frame_code"] is True
    # timing text alone missing: still a full summary, no time-offset line
    monkeypatch.setattr(mpc, "VALIDATION_FILE", _validation_json(tmp_path, drop=["timing.note"]))
    lines = mpc.validation_lines(mpc.validation_summary())
    assert "second object: not available." not in lines and not any("time offset" in ln for ln in lines)


def test_validation_status_states(tmp_path, monkeypatch):
    monkeypatch.setattr(mpc, "VALIDATION_FILE", _validation_json(tmp_path))
    st = mpc.validation_status()
    assert st == {"file": "v.json", "status": "complete", "missing_keys": [],
                  "frame_code_hash": st["frame_code_hash"], "same_frame_code": True}
    monkeypatch.setattr(mpc, "VALIDATION_FILE", _validation_json(tmp_path, drop=["eros.n_total"]))
    assert mpc.validation_summary() is None
    st = mpc.validation_status()
    assert st["status"] == "incomplete" and st["missing_keys"] == ["n_total"]
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(mpc, "VALIDATION_FILE", bad)
    assert mpc.validation_status()["status"] == "unreadable" and mpc.validation_summary() is None
    monkeypatch.setattr(mpc, "VALIDATION_FILE", tmp_path / "missing.json")
    assert mpc.validation_status()["status"] == "missing"


# ---------------------------------------------------------------- official checks
SUBMIT_XSD = mpc.MASTER.parent / "xslt" / "submit.xsd"   # made from adesmaster.xml with the ADES submitxsd.xslt


@pytest.mark.skipif(not SUBMIT_XSD.exists(), reason="cache/ades/xslt/submit.xsd not generated")
def test_xml_passes_the_official_submit_schema():
    from lxml import etree
    schema = etree.XMLSchema(etree.parse(str(SUBMIT_XSD)))
    good = mpc.write_ades_xml(_dets(), {"submitter": "A. Tester", "tool_version": "0.1", "code_hash": "x"})
    assert schema.validate(etree.fromstring(good.encode())), [str(e) for e in schema.error_log]
    bad = good.replace('version="2022"', 'version="2017"')
    assert not schema.validate(etree.fromstring(bad.encode()))


def _official(psv_text, tmp_path):
    """IAU tools: psvtoxml then valsubmit (iau-ades); returns (ok, message)."""
    import subprocess
    import sys
    (tmp_path / "in.psv").write_text(psv_text, encoding="utf-8", newline="\n")
    r1 = subprocess.run([sys.executable, "-m", "ades.psvtoxml", "in.psv", "in.xml"], cwd=tmp_path,
                        capture_output=True, text=True, timeout=120)
    if r1.returncode != 0 or not (tmp_path / "in.xml").exists():
        return False, "psvtoxml: " + (r1.stderr or r1.stdout)[-300:]
    r2 = subprocess.run([sys.executable, "-m", "ades.valsubmit", "in.xml"], cwd=tmp_path, capture_output=True,
                        text=True, timeout=120)
    msg = (tmp_path / "valsubmit.file").read_text() if (tmp_path / "valsubmit.file").exists() else r2.stdout
    return r2.returncode == 0 and "submit is OK" in msg, msg.strip()[:300]


def test_psv_passes_the_official_ades_tools(tmp_path):
    pytest.importorskip("ades", reason="iau-ades not installed (pip install -r requirements-test.txt)")
    good = mpc.write_ades_psv(_dets(), {"submitter": "A. Tester", "tool_version": "0.1", "code_hash": "x"})
    ok, msg = _official(good, tmp_path)
    assert ok, msg
    bad_dir = tmp_path / "bad"
    bad_dir.mkdir()
    ok, msg = _official(good.replace("ICRF_KM", "ICRF_XX"), bad_dir)
    assert not ok


def test_mpc_designation_rules():
    for bad in ("K23A00B", "00001", "A0001", "~0023", "2023AB", "2023AB12", "1", "620127", "1P", "SPXtoolong"):
        with pytest.raises(ValueError):
            mpc.check_trksub(bad)
    for good in ("SPX1A2B", "ab12cd", "Blink-1"):
        assert mpc.check_trksub(good) == good


def test_submitter_name_rules():
    for good in ("I. M. Submit", "J. R. R. Tolkien", "J.-F. Hulot", "A. Öztürk"):
        assert mpc.check_name(good) == good
    for bad in ("Ivan Submit", "J.R.R. Tolkien", "someone@example.org", "X", "I. M. Submit 2"):
        with pytest.raises(ValueError):
            mpc.check_name(bad)


def test_night_minimum_and_one_position_per_time():
    d = _dets(3) + _dets(2)
    for x in d[3:]:                      # two more positions two days later: their night has fewer than 3
        x["obs_time"] = x["obs_time"].replace("2025-11-09", "2025-11-11")
    reasons = mpc.select_for_ades(d)
    assert set(reasons) == {3, 4} and all("fewer than 3" in r for r in reasons.values())
    rows = _psv_parse(mpc.write_ades_psv(d, {"submitter": "I. M. Tester"}))[1]
    assert len(rows) == 3
    dup = _dets(4)
    dup[3]["obs_time"] = dup[0]["obs_time"]   # same time twice (e.g. two detector files of one exposure)
    dup[3]["snr_calibrated"] = 99.0
    reasons = mpc.select_for_ades(dup)
    assert list(reasons) == [0] and "same obsTime" in reasons[0]
    rows = _psv_parse(mpc.write_ades_psv(dup, {"submitter": "I. M. Tester"}))[1]
    assert len({r["obsTime"] for r in rows}) == len(rows) == 3


def test_comment_lines_have_no_free_text_and_fit():
    txt = mpc.write_ades_psv(_dets(), {"submitter": "I. M. Tester", "note": "my e-mail is a@b.c"})
    assert "a@b.c" not in txt
    lines = [ln[7:] for ln in txt.splitlines() if ln.startswith("! line ")]
    assert all(len(x) <= 100 for x in lines)
    assert "Gaia3" in txt and "unverified" not in txt
    assert mpc.MPC_PSV_TEST in lines and "No magnitudes: SPHEREx wavelengths have no MPC band code." in lines


def test_checked_ades_with_and_without_schema(monkeypatch, tmp_path):
    d = _dets()
    if mpc.SUBMIT_XSD.exists():
        txt, checked = mpc.checked_ades(d, {"submitter": "I. M. Tester"})
        assert checked and mpc.SCHEMA_PASSED in txt and mpc.validate_psv(txt) == []
        xml, checked = mpc.checked_ades(d, {"submitter": "I. M. Tester"}, fmt="xml")
        assert checked and xml.startswith("<?xml") and mpc.MPC_XML_TEST in xml
        # a draft the schema rejects is not handed out
        monkeypatch.setattr(mpc, "LOCATION_SYS", "ICRF_XX")
        with pytest.raises(mpc.SchemaError):
            mpc.checked_ades(d, {"submitter": "I. M. Tester"})
        monkeypatch.setattr(mpc, "LOCATION_SYS", "ICRF_KM")
    monkeypatch.setattr(mpc, "SUBMIT_XSD", tmp_path / "missing.xsd")
    txt, checked = mpc.checked_ades(d, {"submitter": "I. M. Tester"})
    assert not checked and mpc.SCHEMA_NOT_CHECKED in txt
