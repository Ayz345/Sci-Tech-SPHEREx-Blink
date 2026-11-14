"""Probe 4: byte layout of one Level 2 file on S3 (where each HDU starts, how FLAGS is compressed). Read-only."""
import sys

import requests
from astropy.io import fits

URL = sys.argv[1] if len(sys.argv) > 1 else (
    "https://nasa-irsa-spherex.s3.amazonaws.com/dr1/level2/2025W19_1B/l2b-v27-2026-197/3/"
    "level2_2025W19_1B_0116_1D3_spx_l2b-v27-2026-197.fits"
)
BLOCK = 2880
s = requests.Session()
size = int(s.head(URL, timeout=60).headers["Content-Length"])
print("file size", size)


def rng(a, n):
    r = s.get(URL, headers={"Range": f"bytes={a}-{min(a + n, size) - 1}"}, timeout=120)
    r.raise_for_status()
    return r.content


def read_header(off):
    """Return (header, header_bytes) for the HDU starting at off."""
    buf = b""
    while True:
        buf += rng(off + len(buf), BLOCK * 8)
        for i in range(0, len(buf), 80):
            if buf[i:i + 8] == b"END     ":
                n = ((i + 80 + BLOCK - 1) // BLOCK) * BLOCK
                return fits.Header.fromstring(buf[:n].decode("ascii", "replace")), n
        if off + len(buf) >= size:
            raise RuntimeError("no END")


off = 0
while off < size:
    h, n = read_header(off)
    naxis = [h.get(f"NAXIS{i + 1}") for i in range(h.get("NAXIS", 0))]
    data = 0
    if naxis:
        data = abs(h["BITPIX"]) // 8 * h.get("GCOUNT", 1) * (h.get("PCOUNT", 0) + _prod) if False else None
    npix = 1
    for a in naxis:
        npix *= a
    data = abs(h["BITPIX"]) // 8 * (h.get("PCOUNT", 0) + (npix if naxis else 0))
    padded = ((data + BLOCK - 1) // BLOCK) * BLOCK
    print(f"\n@{off:>10}  hdr {n:>6}  data {data:>10}  EXTNAME={h.get('EXTNAME')}  XTENSION={h.get('XTENSION')}  BITPIX={h['BITPIX']}  NAXIS={naxis}  PCOUNT={h.get('PCOUNT')}")
    keys = [k for k in h if k.startswith(("ZCMP", "ZTILE", "ZNAXIS", "ZBITPIX", "ZNAME", "ZVAL", "TTYPE", "TFORM", "TDIM", "THEAP", "ZQUANT"))]
    if keys:
        print("   ", {k: h[k] for k in keys})
    if h.get("EXTNAME") == "IMAGE":
        want = ("BUNIT", "DETECTOR", "MJD-OBS", "MJD-BEG", "MJD-END", "DATE-OBS", "DATE-AVG", "EXPTIME", "OBSID", "CTYPE1", "CTYPE2", "A_ORDER", "AP_ORDER", "CTYPE1W", "CTYPE2W", "PS1_0W", "PS1_1W", "PS2_0W", "WCSNAMEW", "PV1_3W")
        print("   ", {k: h[k] for k in want if k in h})
        print("    all W keys:", [k for k in h if k.endswith("W") and len(k) > 3][:60])
    if h.get("EXTNAME") == "WCS-WAVE":
        print("    full header:\n" + "\n".join("      " + c for c in repr(h).split("\n")))
    off += n + padded
print("\nend offset", off)
