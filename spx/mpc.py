"""Draft astrometry reports for the Minor Planet Center: ADES PSV (IAU standard) and plain CSV.

Nothing here talks to the MPC. The functions only build text that a person reviews and submits.

    text = write_ades_psv(detections, context)     # raises ValueError when something required is missing
    text = write_csv(detections, context)
    errors = validate_psv(text)                     # [] when the file passes the checks below

detections: one dict per single exposure (never a co-added frame):
    obs_time   ISO-8601 UTC of MID-exposure as in the IMAGE header DATE-AVG, e.g. "2025-05-05T16:49:00.018"
    ra, dec    degrees (ICRS)
    rms_ra     arcsec, 1-sigma of RA*cos(Dec);  rms_dec  arcsec
    sc_pos_km  (x, y, z) km, geocentric spacecraft position at mid-exposure (header X_SC, Y_SC, Z_SC)
    sc_vel_kms (vx, vy, vz) km/s, optional (header VX_SC ...)
    exp_s      exposure time [s] (header XPOSURE), optional
    snr_calibrated  calibrated signal-to-noise (empty-position calibration), optional; the only S/N written
               (as logSNR). A fit's S/N (snr_fit) is never written.
    finast     FINAST of the exposure; only 0 is accepted for ADES
    obs_id, band, wavelength_um   optional, go into remarks
context:
    submitter (required; no file without it), trk_sub (optional, else generated), measurers (default [submitter]),
    tool_version, code_hash (written as comments and software line).

Format facts come from the ADES description (cache/ades/ADES_Description.pdf, ades.txt) and its machine-readable
master file (cache/ades/adesmaster.xml): version "2022" (the only VersionType value), obsContext requires
observatory (mpcCode), submitter, measurers and telescope (design, aperture, detector); optical records need an
identifier (trkSub for a new object), mode, stn, obsTime, ra, dec, astCat; a space telescope adds the Location
group (sys ICRF_KM, ctr 399, pos1..3 [km], optional vel1..3 [km/s]). Precision: digits after the decimal point
DP = ceil(1 - log10(sigma)) for measured values; uncertainties with at most two significant figures.

Values NOT verified yet (the MPC keeps the lists; its web pages refuse automated reads) live in ONE place below,
and every file says so in a comment line until they are confirmed.

MPC rules and value lists (MPC documentation hub, saved under cache/ades/mpcdocs/):
  observations_valid-ades-values.txt      mode list (UNK = Unknown), astCat list (Gaia3 = Gaia DR 3), band list;
                                          "Use of an unlisted value ... will cause a batch to be rejected"
  astrometry_reporting-observations.txt   at least three observations per object per night, no single positions;
                                          temporary designations: seven characters or less, unique, not shaped like
                                          MPC packed/unpacked designations; no approximate or preliminary measures
  observations_observational-details.txt  names: initials + surname ('J. R. R. Tolkien', 'J.-F. Hulot')
  observations_ades-format.txt            keep PII out of ADES, comments and remarks included; MPC test forms
                                          submit_psv_test / submit_xml_test
  observatory-and-program-codes_program-codes.txt  program codes for archival observations still to be announced
  astrometry_getting-started.txt          pixel scale guidance for ground observers (<= 2"/px, 3" at worst)

Why pos1..3 are required, not decoration: for an object at 1 au the direction seen from the spacecraft differs from
the geocentric one by up to ~10 arcsec (co-add session's parallax estimate), far above the measurement error.
"""
from __future__ import annotations

import csv
import hashlib
import io
import math
import re
from pathlib import Path

ADES_VERSION = "2022"            # adesmaster.xml VersionType: the only allowed value
STATION = "288"                  # SPHEREx, satellite (MPC obscodes service, cache/ades/obscodes_extract.json)
STATION_NAME = "SPHEREx"         # MPC short_name for 288
LOCATION_SYS = "ICRF_KM"         # space-based station, km (adesmaster SysType)
LOCATION_CTR = "399"             # geocentre: the only CtrType value

# ---------------------------------------------------------------- the single setting point for unverified values
#: MPC list values (observations_valid-ades-values.txt). mode UNK: the detector is a Teledyne H2RG HgCdTe array,
#: never called CMOS in the Explanatory Supplement, so no closer list value was confirmed. astCat Gaia3 = Gaia DR 3,
#: the reference of SPHEREx astrometry (Explanatory Supplement v2.1, Sec. 3.2.8).
UNVERIFIED = {"mode": "UNK", "astCat": "Gaia3"}   # name kept for the single setting point; both values verified
VERIFIED = {"mode": True, "astCat": True}
#: Telescope context (required: design, aperture [m], detector; ADES Table 18). Source: SPHEREx Explanatory
#: Supplement v2.1, sec. 1.3 p. 5 ("20-cm effective aperture ... Three Mirror Anastigmat"), Table 2 p. 6 (f/3;
#: Hawaii-2RG), p. 6 ("Teledyne H2RG HgCdTe 2k x 2k"); read by the co-add session, 14 Nov 2026.
#: None in a required field = not verified -> no ADES file is written.
TELESCOPE = {"design": "reflector", "aperture": "0.20", "detector": "HgCdTe", "fRatio": "3"}
TELESCOPE_REQUIRED = ("design", "aperture", "detector")
#: Reference catalogue of SPHEREx astrometry (Explanatory Supplement v2.1, sec. 3.2.8 p. 35: Gaia DR3, propagated
#: to the observation epoch); its MPC astCat code is still unverified (UNVERIFIED["astCat"]).
ASTROMETRY_REFERENCE = "Gaia DR3 (SPHEREx Explanatory Supplement v2.1, Sec. 3.2.8)"
#: Spacecraft position frame: header XYZ_SC_SYSTEM = GEOCENTER; axes ICRF/J2000 equatorial, checked by the co-add
#: session against the header ground track (0.06 deg; Earth-fixed axes would be ~95 deg off).
#: Other facts not verified yet, listed in the file's comment.
UNVERIFIED_FACTS: list = []
#: Only exposures with a good fine-astrometric solution go into an ADES draft (Explanatory Supplement v2.1:
#: FINAST 0 good, 1 partly good, higher not to be used); the others stay in the CSV with the reason.
GOOD_FINAST = (0,)
#: Only measurements made within one visit go into an ADES draft (between-epoch tracks are proper-motion stars).
ADES_REGIME = "within_visit"

#: The co-add session's validation of the per-exposure astrometry against JPL Horizons
#: (python tests/test_astrometry.py [--object]); read at write time, never typed in here.
VALIDATION_FILE: Path | None = Path(__file__).resolve().parent.parent / "handoff" / "results" / \
    "astrometry_validation_v2.json"
VALIDATION_KEYS: dict = {
    "object": "object", "median_ra": "eros.median_dra_arcsec", "median_dec": "eros.median_ddec_arcsec",
    "scatter_ra": "eros.scatter_ra_arcsec", "scatter_dec": "eros.scatter_dec_arcsec",
    "along_track": "eros.median_along_track_arcsec", "n_accepted": "eros.n_accepted", "n_total": "eros.n_total",
    "floor": "wcs_floor_arcsec", "frame_code_hash": "frame_code_hash",
    "timing_verified": "timing.verified",
    "object2": "second_object.object", "n2_accepted": "second_object.n_accepted",
    "n2_total": "second_object.n_total", "timing_note": "timing.note",
    "along_track2": "second_object.median_along_track_arcsec",
    "cross_track2": "second_object.median_cross_track_arcsec",
    "dt2": "timing.second_object_time_offset_s",
}
#: MPC: report at least three observations of each object from each night; batches with single positions are
#: returned (astrometry_reporting-observations.txt). Applied per 24-hour window of the ADES detections.
MIN_DETECTIONS = 3
MIN_DETECTIONS_VERIFIED = True
NIGHT_HOURS = 24.0
MPC_PSV_TEST = "https://minorplanetcenter.net/submit_psv_test"
MPC_XML_TEST = "https://minorplanetcenter.net/submit_xml_test"
SCHEMA_PASSED = "This file passed the ADES submit schema (submit.xsd) when it was generated."
SCHEMA_NOT_CHECKED = "Writer output passes the official ADES submit check in tests; this file was not checked."


def snr_threshold() -> float | None:
    """The acceptance S/N of the co-add session's measure_track (its default), read from the code."""
    try:
        import inspect
        from spx import astrometry
        v = inspect.signature(astrometry.measure_track).parameters["snr_min"].default
        return float(v) if v is not inspect.Parameter.empty else None
    except Exception:
        return None

POS_DECIMALS = 3                 # km -> 1 m; the header gives no position uncertainty
VEL_DECIMALS = 6                 # km/s -> 1 mm/s

MASTER = Path(__file__).resolve().parent.parent / "cache" / "ades" / "adesmaster.xml"
#: The official submission schema, made from MASTER by scripts/make_submit_xsd.py (ADES submitxsd.xslt).
SUBMIT_XSD = MASTER.parent / "xslt" / "submit.xsd"


class SchemaError(RuntimeError):
    """A draft made by this writer failed the ADES submit schema: a bug on our side, not the user's."""

_TRK_RE = re.compile(r"[-A-Za-z0-9_]{1,7}")   # ADES allows 8; the MPC asks for seven characters or less
#: Shapes of MPC designations a temporary designation must not take (astrometry_reporting-observations.txt):
#: packed provisional (K23A00B), packed permanent (00001, A0001, ~0023), unpacked provisional (2023AB, 2023 AB12),
#: unpacked numbers (1, 620127), comet-like (C/2023 A1, 1P).
_DESIGNATION_LIKE = [
    re.compile(r"[IJK]\d{2}[A-HJ-Y][0-9A-Za-z]\d[A-Z]"),
    re.compile(r"[0-9A-Za-z]\d{4}"),
    re.compile(r"~[0-9A-Za-z]{4}"),
    re.compile(r"\d{4}\s?[A-HJ-Y][A-HJ-Z](\d{1,3})?"),
    re.compile(r"\d+"),
    re.compile(r"\d+[PDI]"),
    re.compile(r"[PCDXAI]/.*"),
]


# ---------------------------------------------------------------- numbers
def _sig2(x: float) -> str:
    """Uncertainty with at most two significant figures (ADES: 2 if the leading digit is 1, else 1 or 2)."""
    if not (x > 0 and math.isfinite(x)):
        raise ValueError(f"uncertainty must be positive and finite, got {x}")
    e = math.floor(math.log10(x))
    dp = max(0, 1 - e)
    s = f"{round(x, dp):.{dp}f}"
    if float(s) == 0:
        raise ValueError(f"uncertainty {x} rounds to zero")
    return s


def _dp(sigma: float) -> int:
    """ADES: digits after the decimal point = ceil(1 - log10(sigma)), clipped to the schema's 0..9."""
    return int(min(9, max(0, math.ceil(1 - math.log10(sigma)))))


def _fmt_ra(ra: float, rms_ra_arcsec: float, dec: float) -> str:
    sigma_deg = rms_ra_arcsec / 3600.0 / max(1e-6, math.cos(math.radians(dec)))
    dp = _dp(sigma_deg)
    s = f"{ra % 360.0:.{dp}f}"
    return f"{0.0:.{dp}f}" if float(s) >= 360.0 else s


def _fmt_dec(dec: float, rms_dec_arcsec: float) -> str:
    dp = _dp(rms_dec_arcsec / 3600.0)
    s = f"{dec:.{dp}f}"
    return "+" + s if not s.startswith("-") else s


def _fmt_signed(x: float, nd: int) -> str:
    s = f"{x:.{nd}f}"
    return s if s.startswith("-") else "+" + s


def _fmt_time(t: str) -> str:
    t = t.strip()
    if t.endswith("Z"):
        t = t[:-1]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?", t):
        raise ValueError(f"obs_time must be ISO-8601 UTC like 2025-05-05T16:49:00.018, got {t!r}")
    return t + "Z"


# ---------------------------------------------------------------- identifiers
def make_trksub(detections, prefix: str = "SPX") -> str:
    """Deterministic temporary designation (7 characters, [-A-Za-z0-9_]) from the detections' times and positions."""
    h = hashlib.sha1("|".join(f"{d['obs_time']}:{d['ra']:.6f}:{d['dec']:.6f}" for d in detections).encode())
    n = int(h.hexdigest(), 16)
    chars = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    tail = ""
    for _ in range(7 - len(prefix)):
        n, r = divmod(n, 36)
        tail += chars[r]
    return prefix + tail


def check_trksub(t: str) -> str:
    """A temporary designation the MPC accepts: 1-7 characters [-A-Za-z0-9_], not shaped like an MPC designation."""
    if not _TRK_RE.fullmatch(t or ""):
        raise ValueError("the temporary designation (trkSub) must be 1-7 characters from A-Z, a-z, 0-9, '-' and '_'")
    if any(r.fullmatch(t) for r in _DESIGNATION_LIKE):
        raise ValueError(f"the temporary designation {t!r} looks like an MPC designation (e.g. K23A00B, 2023AB, "
                         "00001, 620127); choose another, e.g. starting with letters")
    return t


_NAME_RE = re.compile(r"(?:[A-Z]\.(?:-[A-Z]\.)?\s)+[^\W\d_][\w'\-]*(?:\s[^\W\d_][\w'\-]*)*")


def check_name(name: str) -> str:
    """Submitter / measurer name as the MPC wants it: initials + surname, initials separated by spaces
    ('I. M. Submit', 'J.-F. Hulot'). No e-mail or other personal data beyond the name."""
    name = " ".join((name or "").split())
    if not name:
        raise ValueError("submitter name is required (no file is produced without it)")
    if "@" in name or re.search(r"\d", name):
        raise ValueError("give only a name (initials + surname); no e-mail address or numbers")
    if not _NAME_RE.fullmatch(name):
        raise ValueError(f"name {name!r}: write initials then surname, initials separated by spaces, "
                         "e.g. 'I. M. Submit' or 'J.-F. Hulot' (MPC rule)")
    if len(name) > 100:
        raise ValueError("name longer than 100 characters")
    return name


def _clean_text(s: str, limit: int) -> str:
    s = " ".join(str(s).replace("|", "/").split())
    return s[:limit]


# ---------------------------------------------------------------- writers
def _comment_lines(context: dict) -> list[str]:
    tool = f"SPHEREx Blink {context.get('tool_version', '')}".strip()
    code = context.get("code_hash")
    lines = [
        "DRAFT generated by software. Review every line before submitting to the MPC.",
        f"Made with {tool}" + (f", code {code}" if code else "") + ".",
        "Data: NASA/IPAC IRSA SPHEREx archive (DR1/QR3 Level 2 images).",
        "Positions measured on single exposures; obsTime is mid-exposure (UTC).",
        "Telescope: SPHEREx Explanatory Supplement v2.1, Sec. 1.3 and Table 2",
        "Spacecraft position: header X_SC, Y_SC, Z_SC (geocentric, ICRF equatorial;",
        "checked against the header ground-track longitude)",
        f"Astrometric reference: {ASTROMETRY_REFERENCE}",
        "mode UNK: detector is a Teledyne H2RG HgCdTe array;",
        "no closer value in the MPC list was confirmed.",
        "No magnitudes: SPHEREx wavelengths have no MPC band code.",
        "Station 288 (SPHEREx). Third-party submissions of archival astrometry may need",
        "a program code: contact the MPC before submitting.",
        "Pixel scale 6.15 arcsec (MPC guidance for ground observers: 2-3 arcsec);",
        "measured single-exposure accuracy on (433) Eros is in the validation line.",
    ]
    thr = snr_threshold()
    if thr is not None:
        lines.append(f"Only detections with S/N >= {thr:g} are reported (the measurement's acceptance threshold).")
    lines.append(f"At least {MIN_DETECTIONS} positions per {NIGHT_HOURS:g}-hour window (MPC: no single positions per night).")
    lines.append(context.get("schema_line") or SCHEMA_NOT_CHECKED)
    lines.append("Before submitting, check the file with the MPC test form:")
    lines.append(context.get("test_form") or MPC_PSV_TEST)
    lines += validation_lines(validation_summary())
    unver = [k for k, ok in VERIFIED.items() if not ok]
    if unver:
        lines.append("unverified: " + ", ".join(unver))
    for f in UNVERIFIED_FACTS:
        lines.append("unverified: " + f)
    if not MIN_DETECTIONS_VERIFIED:
        lines.append(f"unverified: minimum number of detections ({MIN_DETECTIONS}) is not taken from an MPC rule")
    for x in lines:
        if len(x) > 100:
            raise AssertionError(f"comment line over 100 characters: {x!r}")
    return lines


#: Optical record fields, in the schema's sequence (adesmaster.xml optical type), as written by this tool.
FIELDS = ["trkSub", "mode", "stn", "sys", "ctr", "pos1", "pos2", "pos3", "vel1", "vel2", "vel3",
          "obsTime", "ra", "dec", "rmsRA", "rmsDec", "astCat", "logSNR", "exp", "remarks"]


def ades_rows(detections, trk_sub: str) -> list[dict]:
    rows = []
    for d in detections:
        if d.get("sc_pos_km") is None:
            raise ValueError("every detection needs the spacecraft position (sc_pos_km)")
        r = {"trkSub": trk_sub, "mode": UNVERIFIED["mode"], "stn": STATION, "sys": LOCATION_SYS,
             "ctr": LOCATION_CTR}
        for i, v in enumerate(d["sc_pos_km"], 1):
            r[f"pos{i}"] = _fmt_signed(float(v), POS_DECIMALS)
        if d.get("sc_vel_kms") is not None:
            for i, v in enumerate(d["sc_vel_kms"], 1):
                r[f"vel{i}"] = _fmt_signed(float(v), VEL_DECIMALS)
        r["obsTime"] = _fmt_time(d["obs_time"])
        r["ra"] = _fmt_ra(float(d["ra"]), float(d["rms_ra"]), float(d["dec"]))
        r["dec"] = _fmt_dec(float(d["dec"]), float(d["rms_dec"]))
        r["rmsRA"] = _sig2(float(d["rms_ra"]))
        r["rmsDec"] = _sig2(float(d["rms_dec"]))
        r["astCat"] = UNVERIFIED["astCat"]
        if d.get("snr_calibrated") and d["snr_calibrated"] > 0:  # only a calibrated S/N; never the fit's S/N
            r["logSNR"] = f"{math.log10(d['snr_calibrated']):.2f}"
        if d.get("exp_s"):
            r["exp"] = f"{float(d['exp_s']):.2f}"[:6].rstrip(".")
        rem = []
        if d.get("obs_id"):
            rem.append(f"SPHEREx obsID {d['obs_id']}")
        if d.get("band"):
            rem.append(str(d["band"]))
        if d.get("wavelength_um"):
            rem.append(f"{float(d['wavelength_um']):.3f} um")
        if rem:
            r["remarks"] = _clean_text(" ".join(rem), 300)
        rows.append(r)
    return rows


def ades_reason(d) -> str | None:
    """Why a detection stays out of an ADES draft (None = it goes in): not accepted (e.g. blended), FINAST not 0,
    trailed, a regime other than within_visit, or no spacecraft position."""
    if d.get("accepted") is False:
        return d.get("reason") or "not accepted by the measurement"
    if d.get("finast") is not None and int(d["finast"]) not in GOOD_FINAST:
        return f"FINAST={d['finast']} (only {', '.join(map(str, GOOD_FINAST))} used for ADES)"
    if d.get("trailed"):
        return "trailed"
    if d.get("regime") is not None and d["regime"] != ADES_REGIME:
        return f"regime {d['regime']} (ADES only for {ADES_REGIME})"
    if d.get("sc_pos_km") is None:
        return "no spacecraft position"
    return None


def validation_summary() -> dict | None:
    """The co-add session's Horizons validation, read from its test output; None when not available."""
    import json
    if VALIDATION_FILE is None or not Path(VALIDATION_FILE).exists() or not VALIDATION_KEYS:
        return None
    data = json.loads(Path(VALIDATION_FILE).read_text(encoding="utf-8"))

    def get(path):
        cur = data
        for k in path.split("."):
            if not isinstance(cur, dict) or k not in cur:
                return None
            cur = cur[k]
        return cur
    out = {name: get(path) for name, path in VALIDATION_KEYS.items()}
    if any(v is None for v in out.values()):
        return None
    from spx import frame_code_hash
    out["same_frame_code"] = out["frame_code_hash"] == frame_code_hash()
    return out


def validation_lines(v: dict | None) -> list[str]:
    """Human-readable validation and timing lines (each <= 100 characters), from validation_summary()."""
    if v is None:
        return ["Method validation summary not available",
                "obsTime is the header mid-exposure time (DATE-AVG); not independently verified."]
    sysmax = max(abs(v["along_track"]), abs(v["cross_track2"]))
    dt, dte = v["dt2"]
    m = re.search(r"time offset larger than ~?([0-9.]+) s is not supported", v["timing_note"])
    lines = [
        f"Method validated on {v['object']} against JPL Horizons: median offset {v['median_ra']:+.2f}, "
        f"{v['median_dec']:+.2f} arcsec;",
        f"scatter {v['scatter_ra']:.2f}, {v['scatter_dec']:.2f} arcsec ({v['n_accepted']} of {v['n_total']} exposures).",
        f"Position uncertainties include a {v['floor']:.2f} arcsec floor per axis measured on Gaia DR3 stars.",
        "obsTime = header DATE-AVG (mid-exposure), uncorrected; timing not independently verified.",
        f"Along-track residual: {v['object']} {v['along_track']:+.2f} arcsec "
        f"({v['n_accepted']}/{v['n_total']} exposures);",
        f"{v['object2']} {v['along_track2']:+.2f} arcsec ({v['n2_accepted']}/{v['n2_total']} exposures) "
        f"= {dt:+.1f} +- {dte:.1f} s.",
        f"Object-dependent systematics up to {sysmax:.2f} arcsec remain unexplained.",
    ]
    if m:
        lines.append(f"A time offset larger than ~{m.group(1)} s is not supported (co-add session's validation).")
    if not v["same_frame_code"]:
        lines.append(f"Validation made with frame code {v['frame_code_hash']}, not the current one.")
    return lines


def _mjd(t: str) -> float:
    from astropy.time import Time
    return float(Time(_fmt_time(t)[:-1], format="isot", scale="utc").mjd)


def night_windows(detections, indices=None) -> list[list[int]]:
    """Group detection indices into NIGHT_HOURS windows (each window starts at its first detection), time order."""
    idx = sorted(indices if indices is not None else range(len(detections)),
                 key=lambda i: _mjd(detections[i]["obs_time"]))
    groups, start = [], None
    for i in idx:
        m = _mjd(detections[i]["obs_time"])
        if start is None or (m - start) * 24.0 >= NIGHT_HOURS:
            groups.append([i])
            start = m
        else:
            groups[-1].append(i)
    return groups


def select_for_ades(detections) -> dict:
    """{index: reason} for every detection that stays out of the ADES draft: per-detection reasons (ades_reason),
    a second position at the same obsTime (one position per time; the higher S/N is kept), and 24-hour windows
    with fewer than MIN_DETECTIONS positions (MPC: no single positions per night)."""
    detections = list(detections)
    out = {i: r for i, d in enumerate(detections) if (r := ades_reason(d)) is not None}
    by_time = {}
    for i, d in enumerate(detections):
        if i in out:
            continue
        t = _fmt_time(d["obs_time"])
        if t in by_time:
            j = by_time[t]
            key = lambda k: (detections[k].get("snr_calibrated") or 0, detections[k].get("snr_fit") or 0)  # noqa: E731
            drop, keep = (i, j) if key(i) <= key(j) else (j, i)
            out[drop] = "same obsTime as another position (one position per time)"
            by_time[t] = keep
        else:
            by_time[t] = i
    for g in night_windows(detections, [i for i in range(len(detections)) if i not in out]):
        if len(g) < MIN_DETECTIONS:
            for i in g:
                out[i] = f"fewer than {MIN_DETECTIONS} in one night ({NIGHT_HOURS:g}-hour window)"
    return out


def build_ades(detections, context: dict):
    """Validated content shared by the PSV and XML writers: (context records, fields, rows).
    context records: [(record, [(child, value), ...]), ...] in the PSV order.
    Detections left out by select_for_ades stay in the CSV with their reason."""
    detections = list(detections)
    sub = check_name(context.get("submitter") or "")
    left_out = select_for_ades(detections)
    detections = [d for i, d in enumerate(detections) if i not in left_out]
    if len(detections) < MIN_DETECTIONS:
        raise ValueError(f"at least {MIN_DETECTIONS} single-exposure detections in one night are needed for an "
                         f"ADES draft, got {len(detections)} usable")
    missing = [k for k in TELESCOPE_REQUIRED if TELESCOPE.get(k) in (None, "")]
    if missing:
        raise ValueError("telescope context not verified yet: " + ", ".join(missing))
    trk = check_trksub(context.get("trk_sub") or make_trksub(detections))
    measurers = [check_name(m) for m in (context.get("measurers") or [sub])]
    ctx = [("observatory", [("mpcCode", STATION), ("name", STATION_NAME)]),
           ("submitter", [("name", sub)]),
           ("measurers", [("name", m) for m in measurers]),
           ("telescope", [(k, str(TELESCOPE[k])) for k in ("design", "aperture", "detector", "fRatio")
                          if TELESCOPE.get(k) not in (None, "")]),
           ("software", [("astrometry", _clean_text(f"SPHEREx Blink {context.get('tool_version', '')} "
                                                    f"{context.get('code_hash', '')}", 100))]),
           ("comment", [("line", x) for x in _comment_lines(context)])]
    rows = ades_rows(detections, trk)
    fields = [f for f in FIELDS if any(f in r for r in rows)]
    return ctx, fields, rows


def write_ades_psv(detections, context: dict) -> str:
    """ADES PSV draft (version 2022). Raises ValueError when a required input or a verified fact is missing."""
    ctx, fields, rows = build_ades(detections, context)
    out = [f"# version={ADES_VERSION}"]
    for rec, children in ctx:
        out.append(f"# {rec}")
        out += [f"! {k} {v}" for k, v in children]
    width = {f: max(len(f), *(len(r.get(f, "")) for r in rows)) for f in fields}
    out.append("|".join(f.ljust(width[f]) for f in fields).rstrip())
    for r in rows:
        cells = [r.get(f, "").ljust(width[f]) if f in ("remarks", "trkSub") else r.get(f, "").rjust(width[f])
                 for f in fields]
        out.append("|".join(cells).rstrip())
    return "\n".join(out) + "\n"


def write_ades_xml(detections, context: dict) -> str:
    """The same content as write_ades_psv in ADES XML (<ades version="2022"> with one obsBlock)."""
    from xml.sax.saxutils import escape
    ctx, fields, rows = build_ades(detections, {"test_form": MPC_XML_TEST, **context})
    x = ['<?xml version="1.0" encoding="UTF-8"?>', f'<ades version="{ADES_VERSION}">', "  <obsBlock>",
         "    <obsContext>"]
    for rec, children in ctx:
        x.append(f"      <{rec}>")
        x += [f"        <{k}>{escape(v)}</{k}>" for k, v in children]
        x.append(f"      </{rec}>")
    x += ["    </obsContext>", "    <obsData>"]
    for r in rows:
        x.append("      <optical>")
        x += [f"        <{f}>{escape(r[f])}</{f}>" for f in fields if r.get(f, "") != ""]
        x.append("      </optical>")
    x += ["    </obsData>", "  </obsBlock>", "</ades>"]
    return "\n".join(x) + "\n"


_schema_cache = {}


def submit_schema():
    """The compiled submit.xsd, or None when it has not been generated on this installation."""
    if not SUBMIT_XSD.exists():
        return None
    key = SUBMIT_XSD.stat().st_mtime
    if _schema_cache.get("key") != key:
        from lxml import etree
        _schema_cache.update(key=key, schema=etree.XMLSchema(etree.parse(str(SUBMIT_XSD))))
    return _schema_cache["schema"]


def checked_ades(detections, context: dict, fmt: str = "psv") -> tuple[str, bool]:
    """Write an ADES draft and check its content against submit.xsd before handing it out.

    Returns (text, schema_checked). With the schema present, the XML rendering of the same content must validate,
    else SchemaError (the file is not given out); the file then says it passed the schema. Without the schema the
    file is given with the line that it was not checked. 'Passed the schema' does not mean the MPC accepts it."""
    from lxml import etree
    schema = submit_schema()
    ctx = dict(context)
    if schema is not None:
        ctx["schema_line"] = SCHEMA_PASSED
        xml = write_ades_xml(detections, ctx)
        if not schema.validate(etree.fromstring(xml.encode())):
            errs = "; ".join(f"line {e.line}: {e.message}" for e in list(schema.error_log)[:5])
            raise SchemaError(f"the generated ADES draft failed the submit schema: {errs}")
    else:
        ctx["schema_line"] = SCHEMA_NOT_CHECKED
    text = write_ades_xml(detections, ctx) if fmt == "xml" else write_ades_psv(detections, ctx)
    return text, schema is not None


CSV_FIELDS = ["trk_sub", "obs_time_utc_mid", "ra_deg", "dec_deg", "rms_ra_arcsec", "rms_dec_arcsec",
              "snr_calibrated", "snr_fit", "exp_s", "sc_x_km", "sc_y_km", "sc_z_km", "sc_vx_kms", "sc_vy_kms",
              "sc_vz_kms", "obs_id", "band", "wavelength_um", "finast", "in_ades", "reason"]


def write_csv(detections, context: dict) -> str:
    """Plain CSV of the same single-exposure measurements (always available; also for stars and brown dwarfs)."""
    detections = list(detections)
    trk = context.get("trk_sub") or (make_trksub(detections) if detections else "")
    left_out = select_for_ades(detections) if detections else {}
    buf = io.StringIO()
    buf.write("# Measurements on single SPHEREx exposures (NASA/IPAC IRSA SPHEREx archive). obs_time is mid-exposure"
              " UTC.\n")
    if context.get("note"):
        buf.write("# " + _clean_text(context["note"], 300) + "\n")
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(CSV_FIELDS)
    for i, d in enumerate(detections):
        p = d.get("sc_pos_km") or (None, None, None)
        v = d.get("sc_vel_kms") or (None, None, None)
        why = left_out.get(i)
        w.writerow([trk, d.get("obs_time"), d.get("ra"), d.get("dec"), d.get("rms_ra"), d.get("rms_dec"),
                    d.get("snr_calibrated"), d.get("snr_fit"), d.get("exp_s"), *p, *v, d.get("obs_id"), d.get("band"),
                    d.get("wavelength_um"), d.get("finast"), why is None, why or ""])
    return buf.getvalue()


# ---------------------------------------------------------------- checks against the ADES master file
def _master():
    from lxml import etree
    return etree.parse(str(MASTER)).getroot()


def _type_checker(root):
    """name -> callable(value) -> error | None, following each simpletype's restriction chain in adesmaster.xml.
    Patterns at one level are alternatives (OR); levels combine with AND. Unions accept any member."""
    types = {t.get("name"): t for t in root.iter("simpletype")}
    elems = {e.get("name"): e.get("type") for e in root.iter("element") if e.get("name") and e.get("type")}

    def check(tname: str, v: str, depth=0):
        if depth > 20:
            return None
        if tname.startswith("xsd:"):
            if tname == "xsd:decimal":
                return None if re.fullmatch(r"[+\-]?(\d+(\.\d*)?|\.\d+)", v) else f"{v!r} is not a decimal"
            if tname == "xsd:integer":
                return None if re.fullmatch(r"[+\-]?\d+", v) else f"{v!r} is not an integer"
            return None
        t = types.get(tname)
        if t is None:
            return None
        un = t.find("union")
        if un is not None:
            members = (un.get("memberTypes") or "").split()
            sub = t.find("submitunion")
            if sub is not None:
                members = (sub.get("memberTypes") or "").split()
            errs = [check(m, v, depth + 1) for m in members]
            return None if any(e is None for e in errs) else f"{v!r} matches none of {members}"
        res = t.find("restriction")
        if res is None:
            return None
        pats = [r.get("value") for r in res.findall("restrict") if r.get("name") == "xsd:pattern"]
        if pats and not any(re.fullmatch(p, v) for p in pats):
            return f"{v!r} does not match {tname} pattern(s) {pats}"
        enums = [r.get("value") for r in res.findall("restrict") if r.get("name") == "xsd:enumeration"]
        if enums and v not in enums:
            return f"{v!r} not in {tname} values {enums}"
        for r in res.findall("restrict"):
            n, lim = r.get("name"), r.get("value")
            try:
                if n == "xsd:maxLength" and len(v) > int(lim):
                    return f"{v!r} longer than {lim} ({tname})"
                if n == "xsd:minLength" and len(v) < int(lim):
                    return f"{v!r} shorter than {lim} ({tname})"
                if n in ("xsd:minInclusive", "xsd:maxInclusive", "xsd:minExclusive", "xsd:maxExclusive"):
                    x, L = float(v), float(lim)
                    bad = {"xsd:minInclusive": x < L, "xsd:maxInclusive": x > L,
                           "xsd:minExclusive": x <= L, "xsd:maxExclusive": x >= L}[n]
                    if bad:
                        return f"{v!r} outside {n}={lim} ({tname})"
            except ValueError:
                return f"{v!r} is not numeric ({tname})"
        base = res.get("base")
        return check(base, v, depth + 1) if base else None

    return check, elems, types


def _optical_sequence(root) -> tuple[list[str], set[str]]:
    """Field order of the submitted optical record and the required fields (flattening groups)."""
    groups = {g.get("name"): g for g in root.iter("grouptype")}
    opt = None
    for ct in root.iter("complextype"):
        if ct.get("name") in ("OpticalType", "optical"):
            opt = ct
    if opt is None:  # the first sequence that starts with OpticalID and has astCat
        for seq in root.iter("sequence"):
            names = [c.get("ref") for c in seq]
            if names and names[0] == "OpticalID" and "astCat" in names:
                opt = seq
                break
    order, required = [], set()

    def walk(node, optional=False):
        for c in node:
            ref, use = c.get("ref"), c.get("use")
            if use == "NoSubmit":
                continue
            opt_here = optional or use == "optional"
            if c.tag == "group" and ref in groups:
                walk(groups[ref], opt_here)
            elif c.tag == "element" and ref:
                order.append(ref)
                if not opt_here:
                    required.add(ref)
            elif c.tag in ("sequence", "choice"):
                walk(c, opt_here or c.tag == "choice")
    walk(opt if opt.tag == "sequence" else opt.find("sequence"))
    return order, required


def validate_psv(text: str) -> list[str]:
    """Check a PSV file against adesmaster.xml: version, required obsContext records and children, keyword order
    (the schema's optical sequence), required optical fields (trkSub, mode, stn, obsTime, ra, dec, astCat and,
    for a space station, the Location group) and every value against its simple type.

    A quick in-house check that needs only lxml. The official checks (iau-ades psvtoxml + valsubmit, and the
    submit.xsd made from adesmaster.xml with the ADES XSLT) run in tests/test_mpc.py.
    Returns a list of error strings ([] = passed)."""
    root = _master()
    check, elems, _ = _type_checker(root)
    order, _required = _optical_sequence(root)
    errs = []
    lines = text.splitlines()
    if not lines or lines[0].replace(" ", "") != f"#version={ADES_VERSION}":
        errs.append(f"first line must be '# version={ADES_VERSION}'")
    ctx, cur = {}, None
    kw, data = None, []
    for ln in lines[1:]:
        if ln.startswith("#"):
            cur = ln[1:].split()[0]
            ctx.setdefault(cur, [])
        elif ln.startswith("!"):
            parts = ln[1:].strip().split(None, 1)
            if cur is None:
                errs.append(f"'!' record before any '#' record: {ln}")
            else:
                ctx[cur].append((parts[0], parts[1] if len(parts) > 1 else ""))
        elif ln.strip():
            toks = [t.strip() for t in ln.split("|")]
            if kw is None:
                kw = toks
            else:
                data.append(toks)
    for need, children in (("observatory", ["mpcCode"]), ("submitter", ["name"]), ("measurers", ["name"]),
                           ("telescope", ["design", "aperture", "detector"])):
        if need not in ctx:
            errs.append(f"missing obsContext record '# {need}'")
            continue
        have = {k for k, _ in ctx[need]}
        for c in children:
            if c not in have:
                errs.append(f"'# {need}' lacks '! {c}'")
    if next(iter(ctx), None) != "observatory":
        errs.append("obsContext must start with '# observatory'")
    for k, v in ctx.get("comment", []):
        if k != "line" or not v or len(v) > 100 or "|" in v:
            errs.append(f"bad comment line: {k} {v!r}")
    ctx_types = {("observatory", "mpcCode"): "StationType", ("submitter", "name"): "StringTypeW100",
                 ("measurers", "name"): "StringTypeW100", ("telescope", "design"): "StringTypeW35",
                 ("telescope", "aperture"): "PosDecimalTypeW6", ("telescope", "detector"): "StringTypeW25",
                 ("telescope", "name"): "StringTypeW100", ("telescope", "fRatio"): "PosDecimalTypeW6",
                 ("telescope", "arraySize"): "StringTypeW25", ("telescope", "pixelScale"): "PosDecimalTypeW6",
                 ("observatory", "name"): "StringTypeW100", ("software", "astrometry"): "StringTypeW100"}
    for (rec, child), t in ctx_types.items():
        for k, v in ctx.get(rec, []):
            if k == child:
                e = check(t, v)
                if e:
                    errs.append(f"# {rec} ! {child}: {e}")
    if kw is None:
        errs.append("no keyword record")
        return errs
    if kw[0] not in ("permID", "provID", "artSat", "trkSub"):
        errs.append("the keyword record must start with an identification field")
    unknown = [k for k in kw if k not in order]
    if unknown:
        errs.append(f"fields not in the submitted optical record: {unknown}")
    pos = [order.index(k) for k in kw if k in order]
    if pos != sorted(pos):
        errs.append("keyword order differs from the schema's optical sequence")
    for need in ("mode", "stn", "obsTime", "ra", "dec", "astCat"):
        if need not in kw:
            errs.append(f"required field {need} missing")
    if not any(k in kw for k in ("permID", "provID", "artSat", "trkSub")):
        errs.append("no identifier (permID, provID, artSat or trkSub)")
    if any(k in kw for k in ("sys", "ctr", "pos1", "pos2", "pos3")) or True:
        for need in ("sys", "ctr", "pos1", "pos2", "pos3"):
            if need not in kw:
                errs.append(f"space-based station needs Location field {need}")
    for i, row in enumerate(data, 1):
        if len(row) != len(kw):
            errs.append(f"data record {i}: {len(row)} fields, keyword record has {len(kw)}")
            continue
        for k, v in zip(kw, row):
            if v == "":
                if k in ("trkSub", "mode", "stn", "obsTime", "ra", "dec", "astCat", "sys", "ctr", "pos1", "pos2",
                         "pos3"):
                    errs.append(f"data record {i}: required field {k} is empty")
                continue
            t = elems.get(k)
            e = check(t, v) if t else f"no type for {k}"
            if e:
                errs.append(f"data record {i} {k}: {e}")
    if not data:
        errs.append("no data records")
    return errs
