"""Bright extended objects and the SPHEREx CROSSTALK flag: how much of each exposure it covers, and what the frames
become (+inf / NaN shares per pass) as built and, for comparison only, with CROSSTALK not masked.

    python scripts/check_crosstalk.py            # writes cache/crosstalk_check.json
    python scripts/check_crosstalk.py --write    # also handoff/results/crosstalk_check.json (tracked)

Fields are 7' free fields (the live server's default). The comparison run changes spx.coadd.BAD_MASK inside this
process only: it shows where the empty and white areas come from, it is not a proposed setting.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import tempfile
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server import app as A  # noqa: E402
from server import pipeline as P  # noqa: E402
from spx import access  # noqa: E402
from spx import coadd as C  # noqa: E402

CASES = [("M31", "r10p6847_dp41p2688_n140", ["D1", "D2"]), ("Orion Nebula", "r83p8221_dm5p3911_n140", ["D1"]),
         ("Centaurus A", "r201p3651_dm43p0191_n140", ["D1", "D2"])]
BIT = 1 << C.FLAG_BITS["CROSSTALK"]


def frames(f, band, cuts) -> list:
    out = Path(tempfile.mkdtemp())
    P.build_band(f, band, out, cuts=cuts)
    res = []
    for p in sorted(out.glob(f"*_{band}.f32"), key=lambda p: int(p.name.split("_")[0])):
        a = np.fromfile(p, np.float32)
        res.append({"pass": int(p.name.split("_")[0]), "inf": round(float(np.isposinf(a).mean()), 3),
                    "nan": round(float(np.isnan(a).mean()), 3)})
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args(argv)
    full_mask = C.BAD_MASK
    res = {"checked_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
           "note_threshold": A.CROSSTALK_NOTE_FRAC, "fields": []}
    for name, key, bands in CASES:
        f = P.open_field(**A.parse_key(key))
        for band in bands:
            cuts, _ = P.fetch_band_cutouts(f, band, ("IMAGE", "FLAGS", "VARIANCE"), workers=32, box=True)
            fr = []
            for c in cuts:
                on = c.flags != access.OFF_DETECTOR_FLAG
                if on.any():
                    fr.append(float(((c.flags[on] & BIT) != 0).mean()))
            sat = [float(C.saturated_pixels(c.flags)[c.flags != access.OFF_DETECTOR_FLAG].mean()) for c in cuts]
            C.BAD_MASK = full_mask
            built = frames(f, band, cuts)
            C.BAD_MASK = full_mask & ~BIT
            unmasked = frames(f, band, cuts)
            C.BAD_MASK = full_mask
            row = {"object": name, "key": key, "band": band, "exposures": len(fr),
                   "crosstalk_median": round(float(np.median(fr)), 3), "crosstalk_max": round(float(max(fr)), 3),
                   "truly_saturated_max": round(max(sat), 4), "frames_as_built": built,
                   "frames_crosstalk_not_masked": unmasked, "band_note": A.crosstalk_note(band, cuts) is not None}
            print(json.dumps(row), flush=True)
            res["fields"].append(row)
    text = json.dumps(res, indent=1)
    (ROOT / "cache").mkdir(exist_ok=True)
    (ROOT / "cache" / "crosstalk_check.json").write_bytes(text.encode("utf-8"))
    if a.write:
        (ROOT / "handoff" / "results" / "crosstalk_check.json").write_bytes(text.encode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
