"""CatWISE2020 sources for the gallery targets: web/data/<key>/catwise.json (static, so the WISE overlay needs no live
service during a demo). PLAN.md 17 F5; these small JSON files may be committed (exception to 14.6).

    python scripts/make_catwise.py --fetch            # download the raw answers (IRSA SCS) into cache/catwise/
    python scripts/make_catwise.py                    # write catwise.json from the cached answers
    python scripts/make_catwise.py --fetch wise-0855  # one target

Source: IRSA Simple Cone Search, table catwise_2020, one cone per field covering the whole grid (corners included).
IRSA answered 502 to most requests on 14 Nov 2026, so the fetch keeps trying (short waits) and counts every answer;
an answer with QUERY_STATUS OK but no rows is treated as a failure too (CatWISE has ~1000 sources in such a cone).

Positions: ra_pm, dec_pm = the motion fit's position at MJD 57170 (2015.405), per the IRSA column description
("Right ascension from psf model incl. motion at epoch ... MJD=57170 (2015.405) for CatWISE2020") and the paper
(Marocco et al. 2021, Sec. II: "Coordinates in the CatWISE2020 Catalog are in the ICRS system at epoch MJD=57170").
Proper motions are written only when PM_CHECK (below) says the column convention was verified; otherwise null and
the positions are not moved (meta.pm_convention says so).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import sys
import time
import warnings
from pathlib import Path

import numpy as np
import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SCS_URL = "https://irsa.ipac.caltech.edu/SCS"
TABLE = "catwise_2020"
RAW_DIR = ROOT / "cache" / "catwise"
REF_MJD = 57170.0
MARGIN_DEG = 10 / 3600
MAX_ATTEMPTS = int(__import__("os").environ.get("SPX_CATWISE_ATTEMPTS") or 80)
SOURCE = "CatWISE2020 (IRSA catwise_2020, Simple Cone Search)"
SYSTEMATIC_NOTE = ("CatWISE2020 positions and motions carry an uncorrected systematic, typically 30 mas in position and "
                   "10 mas/yr in motion (IRSA catwise_colDescriptions; Marocco et al. 2021, Sec. III.3); small "
                   "against the 3 arcsec grid.")
#: The PMRA convention, checked by tests/test_catwise.py on tests/data/catwise_pm_check.json (accepted 14 Nov 2026).
PM_CHECK: dict | None = {"text": (
    "pmra = mu_alpha cos(dec), pmdec = mu_delta, mas/yr (catalogue arcsec/yr x 1000), positions at MJD 57170 "
    "(ra_pm, dec_pm). cos(dec) is not stated for the column in the CatWISE2020 paper or the IRSA column "
    "description; checked here against Gaia DR3 on 9 stars of the Epsilon Indi B field (Dec -56.8; G < 16, "
    "|pm| > 15 mas/yr, one CatWISE source within 2 arcsec): weighted slope CatWISE PMRA / Gaia pmra = 0.937 +- 0.102 "
    "(1.000 if cos(dec) is included, 1.826 if not; 8.7 sigma from the latter, 4.9 sigma with errors scaled by "
    "sqrt(chi2/dof)); fields near the equator agree too (RW Aur 1.02 +- 0.09, Barnard 1.21 +- 0.15).")}
#: CatWISE fits of objects faster than this are not reliable: Epsilon Indi B (Gaia 3.98, -2.47 arcsec/yr) is listed
#: with 0.90, -0.68 arcsec/yr, 2.7 arcsec from its predicted position.
PM_RELIABLE_MAX_MAS = 1000.0
FAST_NOTE = ("CatWISE motions are not reliable for objects moving faster than about 1 arcsec/yr (Epsilon Indi B: "
             "catalogue 0.90/-0.68, Gaia 3.98/-2.47 arcsec/yr); such rows have pm_reliable false and are drawn at "
             "the catalogue position, not moved, unless their motion agrees within 10% with an independent value (the "
             "named target's, or a single Gaia DR3 source within 2 arcsec); pm_check says which.")


def cone(manifest: dict) -> tuple[float, float, float]:
    """Centre and radius (deg) covering the whole grid, corners included."""
    g = manifest["grid"]
    ra, dec = g["crval"]
    half = max(g["shape"]) * abs(g["cdelt"][1]) / 2 * math.sqrt(2) + MARGIN_DEG
    return float(ra), float(dec), round(half, 5)


def n_rows(content: bytes) -> int | None:
    from astropy.io.votable import parse
    import io
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            return len(parse(io.BytesIO(content)).get_first_table().array)
        except Exception:
            return None


def fetch(key: str, data: Path, log: list) -> bool:
    m = json.loads((data / key / "manifest.json").read_text(encoding="utf-8"))
    ra, dec, r = cone(m)
    for i in range(MAX_ATTEMPTS):
        t = time.time()
        try:
            resp = requests.get(SCS_URL, params={"table": TABLE, "RA": ra, "DEC": dec, "SR": r}, timeout=(15, 90))
            code, content = resp.status_code, resp.content
        except requests.RequestException as e:
            code, content = type(e).__name__, b""
        n = n_rows(content) if code == 200 else None
        log.append({"key": key, "attempt": i + 1, "status": code, "seconds": round(time.time() - t, 2), "rows": n})
        print(key, log[-1], flush=True)
        if code == 200 and n:
            RAW_DIR.mkdir(parents=True, exist_ok=True)
            (RAW_DIR / f"{key}_scs.xml").write_bytes(content)
            meta = {"key": key, "ra": ra, "dec": dec, "radius_deg": r, "retrieved_utc":
                    dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "rows": n, "attempts": i + 1}
            (RAW_DIR / f"{key}_scs.json").write_text(json.dumps(meta, indent=1))
            return True
        time.sleep(min(2.0, 0.5 * 2 ** i))
    return False


def _mas(v):
    return None if v is None else round(v * 1000.0, 2)


def read_rows(key: str) -> tuple[list[dict], dict]:
    """Contract fields from the cached answer: source_name, ra, dec (ra_pm, dec_pm at MJD 57170), pmra, pmdec
    (catalogue PMRA, PMDec converted from arcsec/yr to mas/yr, like gaia.json), w1mag, w2mag, ref_mjd."""
    from astropy.io.votable import parse
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        t = parse(str(RAW_DIR / f"{key}_scs.xml")).get_first_table()
    names = [f.name for f in t.fields]
    a = t.array

    def col(n):
        return a[a.dtype.names[names.index(n)]]

    def f(v):
        return None if np.ma.is_masked(v) or not np.isfinite(float(v)) else float(v)
    rows = []
    for i in range(len(a)):
        rows.append({"source_name": str(col("source_name")[i]), "ra": f(col("ra_pm")[i]), "dec": f(col("dec_pm")[i]),
                     "pmra": _mas(f(col("pmra")[i])), "pmdec": _mas(f(col("pmdec")[i])), "w1mag": f(col("w1mpro_pm")[i]),
                     "w2mag": f(col("w2mpro_pm")[i]), "ref_mjd": REF_MJD})
    meta = json.loads((RAW_DIR / f"{key}_scs.json").read_text())
    return rows, meta


PM_CHECK_FILE = ROOT / "tests" / "data" / "catwise_pm_check.json"
GAIA_MJD = 57388.5   # Gaia DR3 epoch J2016.0


def _raw_columns(key: str, names_wanted) -> dict:
    from astropy.io.votable import parse
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        t = parse(str(RAW_DIR / f"{key}_scs.xml")).get_first_table()
    names = [f.name for f in t.fields]
    a = t.array
    return {n: a[a.dtype.names[names.index(n)]] for n in names_wanted}


def pm_check_rows(key: str, data: Path, gmax=16.0, pmmin=15.0, rmatch=2.0) -> list[dict]:
    """Gaia DR3 stars of the field (G < gmax, |pm| > pmmin mas/yr, moved to MJD 57170) with exactly one CatWISE
    source (ra_pm, dec_pm) within rmatch arcsec: both proper motions, CatWISE sigmas (mas/yr)."""
    c = _raw_columns(key, ("source_name", "ra_pm", "dec_pm", "pmra", "pmdec", "sigpmra", "sigpmdec"))
    ra, dec = np.asarray(c["ra_pm"], float), np.asarray(c["dec_pm"], float)
    gaia = json.loads((data / key / "gaia.json").read_text(encoding="utf-8"))
    dt = (REF_MJD - GAIA_MJD) / 365.25
    out = []
    for g in gaia:
        if g.get("g_mag") is None or g["g_mag"] >= gmax or g.get("pmra") is None or g.get("pmdec") is None:
            continue
        if math.hypot(g["pmra"], g["pmdec"]) <= pmmin:
            continue
        d0 = g["dec"] + g["pmdec"] / 3.6e6 * dt
        r0 = g["ra"] + g["pmra"] / 3.6e6 * dt / math.cos(math.radians(g["dec"]))
        sep = np.hypot((ra - r0) * math.cos(math.radians(d0)), dec - d0) * 3600
        near = np.where(sep < rmatch)[0]
        if len(near) != 1:
            continue
        j = int(near[0])
        vals = [float(c[n][j]) for n in ("pmra", "pmdec", "sigpmra", "sigpmdec")]
        if not all(np.isfinite(vals)):
            continue
        out.append({"source_name": str(c["source_name"][j]), "sep_arcsec": round(float(sep[j]), 3), "g_mag": g["g_mag"],
                    "dec": g["dec"], "gaia_pmra": g["pmra"], "gaia_pmdec": g["pmdec"],
                    "cw_pmra": round(vals[0] * 1e3, 2), "cw_pmdec": round(vals[1] * 1e3, 2),
                    "cw_sigpmra": round(vals[2] * 1e3, 2), "cw_sigpmdec": round(vals[3] * 1e3, 2)})
    return out


def write_pm_check(keys, data: Path) -> None:
    doc = {"what": "CatWISE2020 PMRA/PMDec (IRSA catwise_2020, mas/yr) against Gaia DR3 pmra (= mu_alpha cos dec) "
                   "and pmdec, stars matched by scripts/make_catwise.py --pm-check (G < 16, |pm| > 15 mas/yr, one "
                   "CatWISE source within 2 arcsec of the Gaia position moved to MJD 57170)", "fields": {}}
    for k in keys:
        if (RAW_DIR / f"{k}_scs.xml").exists():
            doc["fields"][k] = pm_check_rows(k, data)
    if (RAW_DIR / "wise-0855_scs.xml").exists():
        c = _raw_columns("wise-0855", ("source_name", "ra", "dec", "meanobsmjd", "ra_pm", "dec_pm", "pmra", "pmdec"))
        rows = []
        for j in range(len(c["ra"])):
            if str(c["source_name"][j]).strip().endswith(("J085507.96-071439.3", "J085506.91-071437.5")):
                rows.append({n: (str(c[n][j]).strip() if n == "source_name" else float(c[n][j])) for n in c})
        doc["wise0855_rows"] = rows
    PM_CHECK_FILE.parent.mkdir(parents=True, exist_ok=True)
    PM_CHECK_FILE.write_text(json.dumps(doc, indent=1), encoding="utf-8")
    print("written", PM_CHECK_FILE, {k: len(v) for k, v in doc["fields"].items()})


#: A fast row (|pm| > 1 arcsec/yr) counts as reliable when its motion agrees with an independent value to 10%.
PM_AGREE_FRAC = 0.10
MATCH_ARCSEC = 2.0


def independent_motions(key: str, data: Path) -> list[dict]:
    """Independent proper motions in the field, at MJD 57170: the named target's (spx.targets) and every Gaia DR3
    source of gaia.json (mu_alpha cos dec, mas/yr)."""
    from spx.targets import TARGETS
    from astropy.time import Time
    out = []
    t = TARGETS.get(key)
    if t is not None:
        dt = (REF_MJD - float(Time(t["ref_epoch"], format="jyear", scale="tdb").mjd)) / 365.25
        out.append({"what": f"{t['name']} ({t['source']})", "ra": t["ra"] + t["pmra"] / 3.6e6 * dt /
                    math.cos(math.radians(t["dec"])), "dec": t["dec"] + t["pmdec"] / 3.6e6 * dt,
                    "pmra": t["pmra"], "pmdec": t["pmdec"], "kind": "target"})
    gp = data / key / "gaia.json"
    if gp.exists():
        dt = (REF_MJD - GAIA_MJD) / 365.25
        for g in json.loads(gp.read_text(encoding="utf-8")):
            if g.get("pmra") is None or g.get("pmdec") is None:
                continue
            out.append({"what": f"Gaia DR3 {g.get('source_id', '')}".strip(), "ra": g["ra"] + g["pmra"] / 3.6e6 * dt /
                        math.cos(math.radians(g["dec"])), "dec": g["dec"] + g["pmdec"] / 3.6e6 * dt,
                        "pmra": g["pmra"], "pmdec": g["pmdec"], "kind": "gaia"})
    return out


def pm_agrees(row: dict, refs: list[dict]) -> dict | None:
    """The named target within MATCH_ARCSEC, else the single Gaia source within MATCH_ARCSEC: does the catalogue
    motion agree to PM_AGREE_FRAC (|difference| / |reference|)? None when there is nothing to compare with."""
    def sep(x):
        return math.hypot((x["ra"] - row["ra"]) * math.cos(math.radians(row["dec"])), x["dec"] - row["dec"]) * 3600
    tgt = [x for x in refs if x["kind"] == "target" and sep(x) < MATCH_ARCSEC]
    gai = [x for x in refs if x["kind"] == "gaia" and sep(x) < MATCH_ARCSEC]
    ref = tgt[0] if tgt else (gai[0] if len(gai) == 1 else None)
    if ref is None:
        return None
    frac = math.hypot(row["pmra"] - ref["pmra"], row["pmdec"] - ref["pmdec"]) / math.hypot(ref["pmra"], ref["pmdec"])
    ok = frac <= PM_AGREE_FRAC
    return {"ok": ok, "text": (f"{'agrees' if ok else 'disagrees'} with {ref['what']}: {ref['pmra']:.0f}, "
                               f"{ref['pmdec']:.0f} mas/yr ({sep(ref):.1f} arcsec away), difference {100 * frac:.1f}% "
                               f"({'within' if ok else 'beyond'} {100 * PM_AGREE_FRAC:.0f}%)")}


def write(key: str, data: Path) -> int:
    rows, meta = read_rows(key)
    if PM_CHECK is None:
        conv = ("positions at the catalogue epoch (MJD 57170), not moved: whether the catalogue column PMRA includes "
                "cos(dec) is not stated for the column in the CatWISE2020 paper or the IRSA column description, and "
                "the check against Gaia DR3 has not passed; pmra and pmdec are null")
        for r in rows:
            r["pmra"] = r["pmdec"] = None
    else:
        conv = PM_CHECK["text"]
        refs = independent_motions(key, data)
        for r in rows:
            if r["pmra"] is not None and r["pmdec"] is not None:
                r["pm_reliable"] = math.hypot(r["pmra"], r["pmdec"]) <= PM_RELIABLE_MAX_MAS
                if not r["pm_reliable"]:
                    chk = pm_agrees(r, refs)
                    if chk is not None:
                        r["pm_check"] = chk["text"]
                        r["pm_reliable"] = chk["ok"]
    out = {"schema": 1, "key": key, "catalog": "CatWISE2020",
           "meta": {"source": SOURCE, "retrieved_utc": meta["retrieved_utc"], "radius_deg": meta["radius_deg"],
                    "centre": [meta["ra"], meta["dec"]], "ref_mjd": REF_MJD, "position_columns": "ra_pm, dec_pm",
                    "pm_unit": "mas/yr", "pm_convention": conv, "note": SYSTEMATIC_NOTE, "fast_note": FAST_NOTE,
                    "n": len(rows), "n_pm_unreliable": sum(1 for r in rows if r.get("pm_reliable") is False)},
           "sources": rows}
    from server import pipeline as P
    P.write_json(data / key / "catwise.json", out, indent=None)
    print(f"{key}: {len(rows)} sources -> {data / key / 'catwise.json'}")
    return len(rows)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("keys", nargs="*")
    ap.add_argument("--data", default=str(ROOT / "web" / "data"))
    ap.add_argument("--fetch", action="store_true", help="download the raw answers first")
    ap.add_argument("--pm-check", action="store_true",
                    help="write tests/data/catwise_pm_check.json (Gaia-matched stars) from the cached answers")
    a = ap.parse_args(argv)
    data = Path(a.data)
    keys = a.keys or [t["key"] for t in json.loads((data / "index.json").read_text(encoding="utf-8"))["targets"]]
    if a.fetch:
        log: list = []
        ok = {k: fetch(k, data, log) for k in keys}
        RAW_DIR.mkdir(parents=True, exist_ok=True)
        lp = RAW_DIR / "fetch_log.json"
        old = json.loads(lp.read_text()) if lp.exists() else []
        lp.write_text(json.dumps(old + log, indent=1))
        codes: dict = {}
        for x in log:
            codes[str(x["status"]) + ("" if x["status"] != 200 or x["rows"] else " empty")] = \
                codes.get(str(x["status"]) + ("" if x["status"] != 200 or x["rows"] else " empty"), 0) + 1
        print("answers:", codes, "fetched:", ok)
        if not all(ok.values()):
            return 1
        return 0
    if a.pm_check:
        write_pm_check(keys, data)
        return 0
    for k in keys:
        write(k, data)
    return 0


if __name__ == "__main__":
    sys.exit(main())
