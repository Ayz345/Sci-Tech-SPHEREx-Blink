"""Cold-cache time of the first band for different per-(epoch, band) caps, live path (IMAGE+FLAGS, box cutouts).

    SPX_CACHE=<empty dir> python scripts/measure_cap.py RA DEC CAP [BAND]

Run each cap with its own empty cache directory, otherwise the second run reads the first one's files.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from server import pipeline as P  # noqa: E402


def main():
    ra, dec, cap = float(sys.argv[1]), float(sys.argv[2]), int(sys.argv[3])
    band = sys.argv[4] if len(sys.argv) > 4 else "D1"
    t0 = time.time()
    f = P.open_field(ra=ra, dec=dec)
    t1 = time.time()
    st = {}
    cuts, _ = P.fetch_band_cutouts(f, band, ("IMAGE", "FLAGS"), cap=cap, stats=st, box=True)
    t2 = time.time()
    frames = P.frames_for_band(f, band, cuts)
    t3 = time.time()
    print(f"cap {cap:3d} {band}: epochs {len(f.epochs)}  refs {st.get('n_refs')}  manifest {t1 - t0:.1f}s  "
          f"fetch {t2 - t1:.1f}s {st.get('bytes', 0) / 1e6:.0f} MB  coadd {t3 - t2:.1f}s  total band {t3 - t1:.1f}s  "
          f"frames n_used {[fr.n_used for fr in frames]}  noise {[round(fr.noise, 4) for fr in frames]}", flush=True)


if __name__ == "__main__":
    main()
