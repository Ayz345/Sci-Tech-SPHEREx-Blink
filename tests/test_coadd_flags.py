"""FLAGS names at a clicked point (v1.2 F3): spectrum point "flags" and meta.flag_legend.

Run:  python tests/test_coadd_flags.py      (or pytest; offline once the Barnard cutouts are cached)

Checks
  legend    every FLAG_LEGEND line occurs in Table 16 of the Explanatory Supplement v2.1 (compared on letters and
            digits only, because the PDF text loses the dash, the multiplication sign and line-end hyphenation);
            needs cache/docs/SPHEREx_Expsupp_v2.1.pdf (IRSA: data/SPHEREx/docs/), skipped without it.
  headers   names and bit numbers in spx.coadd.FLAG_BITS agree with the MSKN cards of cached FLAGS headers.
  exposure  on real Barnard's Star exposures (aperture photometry, and PSF photometry forced by a fake
            neighbour) the point's flags equal the bits decoded independently from the raw FLAGS pixels of the
            measurement region with that file's own MSKN cards; a flagged point names a non-informational bit; a
            saturated point names OVERFLOW; meta.flag_legend covers exactly the names that occur.
"""
from __future__ import annotations

import glob
import json
import re
import sys
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
warnings.filterwarnings("ignore")

import astropy.units as u
import numpy as np
from astropy.coordinates import SkyCoord
from astropy.io import fits

from spx import access
from spx.coadd import FLAG_BITS, INFO_ONLY, half_px_for, target_at
from spx.photometry import FLAG_LEGEND, _aperture_weights, spectrum_points
from spx.targets import target_coord

PDF = ROOT / "cache" / "docs" / "SPHEREx_Expsupp_v2.1.pdf"
KEY = "barnard"


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _mskn(header: fits.Header) -> dict:
    """{bit: name} from MSKNnnnn cards."""
    return {int(k[4:]): str(v).strip() for k, v in header.items() if re.fullmatch(r"MSKN\d{4}", k)}


def test_flag_names_unit():
    from spx.photometry import UNKNOWN_FLAG_MEANING, flag_legend, flag_names
    f = np.zeros((5, 5), np.int32)
    region = np.zeros((5, 5), bool); region[1:4, 1:4] = True
    assert flag_names(f, region) == []
    f[0, 0] = 1 << FLAG_BITS["HOT"]                       # outside the region: not reported
    f[2, 2] = (1 << FLAG_BITS["SOURCE"]) | (1 << 30)      # informational bit + a bit Table 16 does not list
    f[1, 3] = 1 << FLAG_BITS["SATELLITE_HALO"]
    assert flag_names(f, region) == ["SOURCE", "SATELLITE_HALO", "BIT30"]
    leg = flag_legend([{"flags": ["BIT30", "SOURCE"]}, {"flags": []}])
    assert list(leg) == ["SOURCE", "BIT30"] and leg["BIT30"] == UNKNOWN_FLAG_MEANING
    print("unit: [] without bits; bit order; unknown bit -> BIT30")


def test_legend_from_table16():
    assert set(FLAG_LEGEND) == set(FLAG_BITS), set(FLAG_LEGEND) ^ set(FLAG_BITS)
    if not PDF.exists():
        print(f"skip: {PDF} not present")
        return
    import pypdf
    r = pypdf.PdfReader(str(PDF))
    text = "".join(r.pages[i].extract_text() for i in range(len(r.pages)) if 30 <= i <= 36)
    i = text.find("Table 16: Image pixel flags used in spectral images at Level 2")
    assert i >= 0, "Table 16 not found"
    table = _norm(text[i:i + 12000])
    missing = [n for n, s in FLAG_LEGEND.items() if _norm(s) not in table]
    assert not missing, f"legend lines not in Table 16: {missing}"
    for n, b in FLAG_BITS.items():  # name followed by its bit number in the table rows
        assert _norm(f"{n} {b}") in table, (n, b)
    print(f"legend: {len(FLAG_LEGEND)} lines found verbatim (letters/digits) in Table 16 of v2.1")


def test_headers_agree():
    files = sorted(glob.glob(str(ROOT / "cache" / "layout" / "*.npz")))[::50]
    seen = {}
    for f in files:
        m = json.loads(str(np.load(f)["meta"]))
        for b, n in _mskn(fits.Header.fromstring(m["flags_hdr"])).items():
            seen.setdefault(b, set()).add(n)
    if not seen:
        print("skip: no cached layouts")
        return
    for b, names in seen.items():
        assert len(names) == 1, (b, names)
        n = names.pop()
        assert FLAG_BITS.get(n) == b, (n, b)
    print(f"headers: {len(files)} cached FLAGS headers, {len(seen)} bits, names and numbers agree with FLAG_BITS")


def _header_for(cut) -> fits.Header:
    m = json.loads(str(np.load(access._layout_path(cut.s3_key))["meta"]))
    return fits.Header.fromstring(m["flags_hdr"])


def _decode(cut, region) -> list[str]:
    names = _mskn(_header_for(cut))
    v = int(np.bitwise_or.reduce(cut.flags[region].astype(np.int64).ravel(), initial=0))
    return sorted((names.get(b, f"BIT{b}") for b in range(63) if (v >> b) & 1), key=lambda n: FLAG_BITS.get(n, 99))


def run():
    tgt = target_coord(KEY)
    c = target_at(tgt, 61000.0)
    refs = access.find_images(c.ra.deg, c.dec.deg)
    _, ep = access.epochs_for(refs)
    cuts, _ = access.fetch_cutouts(refs, c.ra.deg, c.dec.deg, half_px=half_px_for(7.0))
    by_obs = {(cu.obs_id, cu.detector, round(cu.mjd, 5)): cu for cu in cuts}
    out = {}
    # a fixed fake neighbour 12" east of the target forces the PSF (deblended) path
    fake = SkyCoord([c.ra.deg + 12 / 3600 / np.cos(c.dec.rad)], [c.dec.deg], unit="deg")
    for label, kw in (("aperture", {}), ("psf_deblended", {"neighbors": fake})):
        res = spectrum_points(cuts, tgt, epochs=ep, **kw)
        pts, meta = res["points"], res["meta"]
        n_checked = n_eq = 0
        for p in pts:
            cu = by_obs[(p["obs_id"], int(p["band"][1:]), round(p["mjd"], 5))]
            x, y = cu.wcs.world_to_pixel(target_at(tgt, cu.mjd))
            if p.get("photometry", meta["photometry"]) == "aperture":
                region = _aperture_weights(cu.image.shape, float(x), float(y), 2.0) > 0
            else:
                yy, xx = np.mgrid[0:cu.image.shape[0], 0:cu.image.shape[1]]
                region = np.hypot(xx - float(x), yy - float(y)) <= 1.5
            n_checked += 1
            n_eq += _decode(cu, region) == p["flags"]
        names = sorted({n for p in pts for n in p["flags"]}, key=lambda n: FLAG_BITS.get(n, 99))
        out[label] = {"photometry": meta["photometry"], "n_points": len(pts), "n_checked": n_checked, "n_equal": n_eq,
                      "names": names, "legend_keys": list(meta["flag_legend"]),
                      "legend_source": meta["flag_legend_source"],
                      "n_flagged_without_bad_name": sum(p["flagged"] and not (set(p["flags"]) - set(INFO_ONLY)) for p in pts),
                      "n_saturated_without_overflow": sum(p["saturated"] and "OVERFLOW" not in p["flags"]
                                                          and "NONLINEAR" not in p["flags"] for p in pts),
                      "n_empty": sum(not p["flags"] for p in pts),
                      "example_saturated": next(({"obs_id": p["obs_id"], "band": p["band"], "flags": p["flags"]}
                                                 for p in pts if p["saturated"]), None),
                      "all_points_have_flags": all(isinstance(p.get("flags"), list) for p in pts)}
    return out


def test_flags_on_barnard():
    out = run()
    for label, o in out.items():
        print(f"{label}: {o['n_points']} points, flags equal to independent decode {o['n_equal']}/{o['n_checked']}, "
              f"empty {o['n_empty']}; names {o['names']}; example saturated {o['example_saturated']}")
        assert o["photometry"] == label
        assert o["all_points_have_flags"]
        assert o["n_equal"] == o["n_checked"] > 0
        assert o["legend_keys"] == o["names"]
        assert o["n_flagged_without_bad_name"] == 0
        assert o["n_saturated_without_overflow"] == 0
        assert "Table 16" in o["legend_source"] and "v2.1" in o["legend_source"]
    assert out["aperture"]["example_saturated"] is not None  # Barnard's core saturates in D1/D2


if __name__ == "__main__":
    test_flag_names_unit()
    test_legend_from_table16()
    test_headers_agree()
    test_flags_on_barnard()
