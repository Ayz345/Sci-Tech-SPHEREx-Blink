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
    snr        signal-to-noise of the measurement, optional (written as logSNR)
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
#: MPC-maintained lists (mode, astCat) and the telescope context. Placeholders until confirmed; files carry an
#: "unverified" comment while any of these is not confirmed.
UNVERIFIED = {
    "mode": "UNK",           # instrument mode code from the MPC list: NOT VERIFIED
    "astCat": "UNK",         # reference catalogue of SPHEREx astrometry, MPC catalogue code: NOT VERIFIED
}
VERIFIED = {"mode": False, "astCat": False}
#: Telescope context (required by ADES). None = not verified from a document yet -> no ADES file is written.
TELESCOPE = {"design": None, "aperture": None, "detector": None}
#: Other facts not verified yet, listed in the file's comment.
UNVERIFIED_FACTS = ["spacecraft position axes (header XYZ_SC_SYSTEM = GEOCENTER; ICRF equatorial assumed)"]
#: Minimum accepted single-exposure detections for an ADES draft. MPC rule not found in our sources.
MIN_DETECTIONS = 3
MIN_DETECTIONS_VERIFIED = False

POS_DECIMALS = 3                 # km -> 1 m; the header gives no position uncertainty
VEL_DECIMALS = 6                 # km/s -> 1 mm/s

MASTER = Path(__file__).resolve().parent.parent / "cache" / "ades" / "adesmaster.xml"

_TRK_RE = re.compile(r"[-A-Za-z0-9_]{1,8}")


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
    """Deterministic trkSub (<= 8 characters, [-A-Za-z0-9_]) from the detections' times and positions."""
    h = hashlib.sha1("|".join(f"{d['obs_time']}:{d['ra']:.6f}:{d['dec']:.6f}" for d in detections).encode())
    n = int(h.hexdigest(), 16)
    chars = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    tail = ""
    for _ in range(8 - len(prefix)):
        n, r = divmod(n, 36)
        tail += chars[r]
    return prefix + tail


def check_trksub(t: str) -> str:
    if not _TRK_RE.fullmatch(t or ""):
        raise ValueError("trkSub must be 1-8 characters from A-Z, a-z, 0-9, '-' and '_'")
    return t


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
    ]
    unver = [k for k, ok in VERIFIED.items() if not ok]
    if unver:
        lines.append("unverified: " + ", ".join(unver))
    for f in UNVERIFIED_FACTS:
        lines.append("unverified: " + f)
    if not MIN_DETECTIONS_VERIFIED:
        lines.append(f"unverified: minimum number of detections ({MIN_DETECTIONS}) is not taken from an MPC rule")
    return [_clean_text(x, 100) for x in lines]


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
        if d.get("snr") and d["snr"] > 0:
            r["logSNR"] = f"{math.log10(d['snr']):.2f}"
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


def write_ades_psv(detections, context: dict) -> str:
    """ADES PSV draft (version 2022). Raises ValueError when a required input or a verified fact is missing."""
    detections = list(detections)
    sub = _clean_text(context.get("submitter") or "", 100)
    if not sub:
        raise ValueError("submitter name is required (no file is produced without it)")
    if len(detections) < MIN_DETECTIONS:
        raise ValueError(f"at least {MIN_DETECTIONS} single-exposure detections are needed, got {len(detections)}")
    missing = [k for k, v in TELESCOPE.items() if v in (None, "")]
    if missing:
        raise ValueError("telescope context not verified yet: " + ", ".join(missing))
    trk = check_trksub(context.get("trk_sub") or make_trksub(detections))
    measurers = [_clean_text(m, 100) for m in (context.get("measurers") or [sub]) if _clean_text(m, 100)]
    out = [f"# version={ADES_VERSION}",
           "# observatory", f"! mpcCode {STATION}", f"! name {STATION_NAME}",
           "# submitter", f"! name {sub}",
           "# measurers", *[f"! name {m}" for m in measurers],
           "# telescope", f"! design {TELESCOPE['design']}", f"! aperture {TELESCOPE['aperture']}",
           f"! detector {TELESCOPE['detector']}",
           "# software",
           "! astrometry " + _clean_text(f"SPHEREx Blink {context.get('tool_version', '')} "
                                         f"{context.get('code_hash', '')}", 100),
           "# comment", *[f"! line {x}" for x in _comment_lines(context)]]
    rows = ades_rows(detections, trk)
    fields = [f for f in FIELDS if any(f in r for r in rows)]
    width = {f: max(len(f), *(len(r.get(f, "")) for r in rows)) for f in fields}
    out.append("|".join(f.ljust(width[f]) if f == fields[-1] == "remarks" else f.ljust(width[f]) for f in fields).rstrip())
    for r in rows:
        cells = []
        for f in fields:
            v = r.get(f, "")
            cells.append(v.ljust(width[f]) if f in ("remarks", "trkSub") else v.rjust(width[f]))
        out.append("|".join(cells).rstrip())
    return "\n".join(out) + "\n"


CSV_FIELDS = ["trk_sub", "obs_time_utc_mid", "ra_deg", "dec_deg", "rms_ra_arcsec", "rms_dec_arcsec", "snr",
              "exp_s", "sc_x_km", "sc_y_km", "sc_z_km", "sc_vx_kms", "sc_vy_kms", "sc_vz_kms", "obs_id", "band",
              "wavelength_um"]


def write_csv(detections, context: dict) -> str:
    """Plain CSV of the same single-exposure measurements (always available; also for stars and brown dwarfs)."""
    detections = list(detections)
    trk = context.get("trk_sub") or (make_trksub(detections) if detections else "")
    buf = io.StringIO()
    buf.write("# Measurements on single SPHEREx exposures (NASA/IPAC IRSA SPHEREx archive). obs_time is mid-exposure"
              " UTC.\n")
    if context.get("note"):
        buf.write("# " + _clean_text(context["note"], 300) + "\n")
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(CSV_FIELDS)
    for d in detections:
        p = d.get("sc_pos_km") or (None, None, None)
        v = d.get("sc_vel_kms") or (None, None, None)
        w.writerow([trk, d.get("obs_time"), d.get("ra"), d.get("dec"), d.get("rms_ra"), d.get("rms_dec"),
                    d.get("snr"), d.get("exp_s"), *p, *v, d.get("obs_id"), d.get("band"), d.get("wavelength_um")])
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

    This is NOT the IAU's official validator (iau-ades: psvtoxml + valsubmit); it is used while that package
    cannot be installed here. Returns a list of error strings ([] = passed)."""
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
                 ("telescope", "aperture"): "PosDecimalTypeW6", ("telescope", "detector"): "StringTypeW25"}
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
