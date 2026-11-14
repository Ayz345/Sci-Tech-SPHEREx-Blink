"""SPHEREx data access: SIA2 image lists and S3 range-read cutouts.

Two calls, both cached on disk under ``cache/`` (override with the SPX_CACHE environment variable):

    refs = find_images(ra, dec)                       # IRSA SIA2, ~3 s cold, instant warm
    cutouts, n_skipped = fetch_cutouts(refs, ra, dec, half_px=50)

How a cutout is read (layout measured in CONTEXT.md 2b; every size is taken from the headers):
  1. File layout, once per file and then cached: PRIMARY + IMAGE headers, FLAGS header and its
     row-descriptor table, VARIANCE header, the WCS-WAVE table (and ZODI header if asked for).
  2. Target pixel from the IMAGE celestial WCS (TAN-SIP).
  3. IMAGE / VARIANCE / ZODI: one range request for the needed full-width rows; columns are cut in memory.
  4. FLAGS (Rice-compressed, one tile per row): only the heap bytes of the needed rows are fetched,
     then a small compressed-image HDU is assembled from the original header and decoded by astropy.
  5. Wavelength per pixel from the WCS-WAVE lookup table (detector coordinates).

Rules kept here: IRSA only through SIA2; at most S3_MAX_CONCURRENCY simultaneous S3 requests in this
process; TLS verification is never disabled; every range response is checked (status 206, the
Content-Range we asked for, the byte count) before use. A cutout that cannot be read is skipped
(and logged), never raised.
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import threading
import time
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import requests
from astropy.io import fits
from astropy.io.votable import parse as vo_parse
from astropy.wcs import WCS, FITSFixedWarning, Sip

log = logging.getLogger("spx.access")

# --------------------------------------------------------------------------- settings

#: Data releases searched, in order of preference (a duplicate exposure keeps the first one).
#: A new release = one new name here.
COLLECTIONS = ("spherex_dr1", "spherex_qr3")

SIA_URL = "https://irsa.ipac.caltech.edu/SIA"
SIA_RADIUS_DEG = 0.001
S3_BUCKET = "nasa-irsa-spherex"
S3_BASE = f"https://{S3_BUCKET}.s3.amazonaws.com/"
S3_MAX_CONCURRENCY = 32

CACHE_DIR = Path(os.environ.get("SPX_CACHE") or Path(__file__).resolve().parent.parent / "cache")
SIA_MAX_AGE_DAYS = 7.0

LAYERS = ("IMAGE", "FLAGS", "VARIANCE", "ZODI")
DETECTOR_BANDS_UM = {1: (0.75, 1.09), 2: (1.10, 1.62), 3: (1.63, 2.41),
                     4: (2.42, 3.82), 5: (3.83, 4.41), 6: (4.42, 5.00)}

#: FLAGS value written where a cutout runs off the detector (MISSING_DATA plane, bit 9),
#: so code that only looks at FLAGS also ignores those pixels. IMAGE/VARIANCE/ZODI are NaN there.
OFF_DETECTOR_FLAG = 1 << 9

BLOCK = 2880
_LAYOUT_VERSION = 1


class FetchError(RuntimeError):
    """A range request failed or returned something other than the bytes asked for."""


class SiaError(RuntimeError):
    """IRSA SIA2 could not be reached or returned an error."""


# --------------------------------------------------------------------------- data classes

@dataclass(frozen=True)
class ImageRef:
    """One Level 2 spectral image (one exposure x one detector) from the SIA2 list."""
    obs_id: str
    detector: int
    collection: str
    s3_key: str
    access_url: str
    t_min: float            # MJD
    t_max: float            # MJD
    em_min_um: float        # whole-detector range; the wavelength at a pixel comes from WCS-WAVE
    em_max_um: float
    bandpass: str           # e.g. "SPHEREx-D3"
    exptime_s: float
    s_region: str

    @property
    def mjd(self) -> float:
        return 0.5 * (self.t_min + self.t_max)

    @property
    def url(self) -> str:
        return S3_BASE + self.s3_key

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


@dataclass
class Cutout:
    """A square piece of one exposure, centred on the requested position.

    Pixel (0, 0) of every array is detector pixel (x0, y0), 0-based; arrays are [y, x].
    Parts outside the detector: NaN in image/variance/zodi/wavelength maps, OFF_DETECTOR_FLAG in flags.
    """
    image: np.ndarray                   # float32 MJy/sr (zodi NOT subtracted)
    flags: np.ndarray | None            # int32 bit planes (None if FLAGS was not requested)
    variance: np.ndarray | None         # float32 MJy^2/sr^2
    zodi: np.ndarray | None             # float32 MJy/sr zodiacal model
    wcs: WCS                            # celestial TAN-SIP, shifted to cutout pixels
    x0: int
    y0: int
    mjd: float                          # MJD-AVG (mid-exposure) from the IMAGE header
    detector: int
    obs_id: str
    wavelength_um: float                # at the requested position
    bandwidth_um: float
    wavelength_map: np.ndarray          # float32 per pixel, µm
    bandwidth_map: np.ndarray
    target_xy: tuple                    # requested position in cutout pixels (0-based, float)
    collection: str = ""
    s3_key: str = ""
    mjd_beg: float = float("nan")
    mjd_end: float = float("nan")
    psf_fwhm_arcsec: float = float("nan")
    meta: dict = field(default_factory=dict, repr=False)

    @property
    def shape(self):
        return self.image.shape


# --------------------------------------------------------------------------- small helpers

def _atomic_write_bytes(path: Path, data: bytes) -> bool:
    """Write via a temp file + rename. On Windows the rename fails while another process has the target
    open (several sessions share cache/), so it is retried briefly; a cache write that still fails is
    logged and dropped (returns False) -- it must never cost the caller its data."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_bytes(data)
    except OSError as e:
        log.warning("cache write %s failed: %r", path, e)
        return False
    for attempt in range(6):
        try:
            os.replace(tmp, path)
            return True
        except PermissionError:
            time.sleep(0.05 * (attempt + 1))
        except OSError as e:
            log.warning("cache write %s failed: %r", path, e)
            break
    try:
        tmp.unlink()
    except OSError:
        pass
    log.info("cache file %s busy; not cached this time", path)
    return False


def _atomic_save_npz(path: Path, **arrays) -> bool:
    buf = io.BytesIO()
    np.savez(buf, **arrays)
    return _atomic_write_bytes(path, buf.getvalue())


def _pad(n: int) -> int:
    return ((n + BLOCK - 1) // BLOCK) * BLOCK


def _key_hash(s: str) -> str:
    return hashlib.sha1(s.encode()).hexdigest()[:16]


def _quiet_wcs(*args, **kw) -> WCS:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", FITSFixedWarning)
        return WCS(*args, **kw)


# --------------------------------------------------------------------------- HTTP

_s3_slots = threading.BoundedSemaphore(S3_MAX_CONCURRENCY)
_sess_lock = threading.Lock()
_sess: requests.Session | None = None


def _session() -> requests.Session:
    global _sess
    with _sess_lock:
        if _sess is None:
            s = requests.Session()
            ad = requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=S3_MAX_CONCURRENCY, max_retries=0)
            s.mount("https://", ad)
            s.headers["User-Agent"] = "spx-blink/0.1 (python-requests)"
            _sess = s
        return _sess


_RETRY_STATUS = {429, 500, 502, 503, 504}


def _get_range(url: str, start: int | None, length: int, retries: int = 4, timeout: float = 60.0):
    """GET a byte range. start=None means the last `length` bytes (suffix range).

    Returns (first_byte_offset, body, total_file_size). The body may be shorter than `length`
    only when the range runs past the end of the file (as reported by Content-Range).
    """
    rng = f"bytes=-{length}" if start is None else f"bytes={start}-{start + length - 1}"
    last = None
    for attempt in range(retries):
        if attempt:
            time.sleep(min(8.0, 0.5 * 2 ** attempt))
        try:
            with _s3_slots:
                r = _session().get(url, headers={"Range": rng}, timeout=timeout, stream=True)
                try:
                    if r.status_code == 206:
                        cr = r.headers.get("Content-Range", "")
                        try:
                            unit, spec = cr.split(" ", 1)
                            span, total = spec.split("/")
                            a, b = (int(v) for v in span.split("-"))
                            total = int(total)
                        except ValueError:
                            raise FetchError(f"bad Content-Range {cr!r} for {rng}")
                        if unit != "bytes":
                            raise FetchError(f"bad Content-Range unit {cr!r}")
                        if start is not None and a != start:
                            raise FetchError(f"asked {rng}, got {cr}")
                        if start is None and b != total - 1:
                            raise FetchError(f"asked {rng}, got {cr}")
                        body = r.content
                        if len(body) != b - a + 1:
                            raise FetchError(f"short body {len(body)} for {cr}")
                        if len(body) < length and b != total - 1:
                            raise FetchError(f"truncated range {cr} for {rng}")
                        return a, body, total
                    if r.status_code in _RETRY_STATUS:
                        last = FetchError(f"HTTP {r.status_code} for {rng}")
                        continue
                    # 200 = server ignored Range (would be the whole 66 MB file): never read it.
                    raise FetchError(f"HTTP {r.status_code} for {rng} {url}")
                finally:
                    r.close()
        except (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError) as e:
            last = e
    raise FetchError(f"{rng} {url}: {last!r}")


def _find_end(buf: bytes, start: int = 0) -> int | None:
    """Byte length of the FITS header starting at buf[start:], padded to 2880; None if no END card."""
    for i in range(start, len(buf) - 79, 80):
        if buf[i:i + 8] == b"END     ":
            return _pad(i + 80 - start)
    return None


def _read_header(url: str, off: int, guess: int, prefetch: bytes = b""):
    """Header at file offset `off`. Returns (Header, header_len, bytes_fetched_from_off)."""
    buf = prefetch
    while True:
        n = _find_end(buf)
        if n is not None and n <= len(buf):
            return fits.Header.fromstring(buf[:n].decode("ascii")), n, buf
        _, more, total = _get_range(url, off + len(buf), max(guess, len(buf)))
        if not more:
            raise FetchError(f"no END card in header at {off}")
        buf += more
        if off + len(buf) >= total and _find_end(buf) is None:
            raise FetchError(f"no END card in header at {off}")


def _data_bytes(h: fits.Header) -> int:
    naxis = h.get("NAXIS", 0)
    npix = 1
    for k in range(naxis):
        npix *= h[f"NAXIS{k + 1}"]
    if naxis == 0:
        npix = 0
    return abs(h["BITPIX"]) // 8 * h.get("GCOUNT", 1) * (h.get("PCOUNT", 0) + npix)


def _expect(h: fits.Header, extname: str, what: str) -> None:
    if h.get("EXTNAME") != extname:
        raise FetchError(f"expected {extname} at the {what} position, found {h.get('EXTNAME')!r}")


# --------------------------------------------------------------------------- SIA2

def _sia_cache_path(ra: float, dec: float, collection: str, radius_deg: float) -> Path:
    return CACHE_DIR / "sia" / collection / f"{ra:.5f}_{dec:+.5f}_{radius_deg:g}.json"


def _sia_query(ra: float, dec: float, collection: str, radius_deg: float, retries: int = 3) -> list[dict]:
    params = {"COLLECTION": collection, "POS": f"circle {ra:.7f} {dec:.7f} {radius_deg:g}"}
    last = None
    for attempt in range(retries):
        if attempt:
            time.sleep(2.0 * attempt)
        try:
            r = requests.get(SIA_URL, params=params, timeout=90)
        except (requests.ConnectionError, requests.Timeout) as e:
            last = e
            continue
        if r.status_code in _RETRY_STATUS:
            last = SiaError(f"SIA2 HTTP {r.status_code}")
            continue
        if r.status_code != 200:
            raise SiaError(f"SIA2 HTTP {r.status_code}: {r.text[:200]}")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            vt = vo_parse(io.BytesIO(r.content))
        status = None
        for res in vt.resources:
            for info in res.infos:
                if info.name == "QUERY_STATUS":
                    status = (info.value, (info.content or "").strip())
        if status and status[0] == "ERROR":
            raise SiaError(f"SIA2 error: {status[1]}")
        if status and status[0] == "OVERFLOW":
            log.warning("SIA2 OVERFLOW for %s %s %s: list truncated", collection, ra, dec)
        tables = list(vt.iter_tables())
        if not tables:
            return []
        tab = tables[0]
        names = [f.name for f in tab.fields]  # astropy may rename columns col_N; real names live here
        arr = tab.array
        cols = {n: arr[arr.dtype.names[i]] for i, n in enumerate(names)}
        rows = []
        for i in range(len(arr)):
            def g(n, default=None):
                if n not in cols:
                    return default
                v = cols[n][i]
                if np.ma.is_masked(v):
                    return default
                return v.item() if hasattr(v, "item") else v
            if g("dataproduct_type") not in (None, "image") or g("calib_level") not in (None, 2):
                continue
            access_url = str(g("access_url", ""))
            s3_key = None
            ca = g("cloud_access")
            if ca:
                try:
                    aws = json.loads(ca)["aws"]
                    if aws.get("bucket_name") == S3_BUCKET:
                        s3_key = aws["key"]
                except (ValueError, KeyError, TypeError):
                    s3_key = None
            if not s3_key and "ibe/data/spherex/" in access_url:
                s3_key = access_url.split("ibe/data/spherex/", 1)[1]
            bp = str(g("energy_bandpassname", ""))
            try:
                det = int(bp.rsplit("D", 1)[1])
            except (IndexError, ValueError):
                det = None
            if not s3_key or det not in DETECTOR_BANDS_UM:
                log.warning("SIA2 row without S3 key or detector skipped: %s %s", g("obs_id"), bp)
                continue
            rows.append({
                "obs_id": str(g("obs_id", "")), "detector": det, "collection": str(g("obs_collection", collection)),
                "s3_key": s3_key, "access_url": access_url,
                "t_min": float(g("t_min", np.nan)), "t_max": float(g("t_max", np.nan)),
                "em_min_um": float(g("em_min", np.nan)) * 1e6, "em_max_um": float(g("em_max", np.nan)) * 1e6,
                "bandpass": bp, "exptime_s": float(g("t_exptime", np.nan)), "s_region": str(g("s_region", "")),
            })
        return rows
    raise SiaError(f"SIA2 unreachable for {collection}: {last!r}")


def find_images(ra: float, dec: float, collections=COLLECTIONS, radius_deg: float = SIA_RADIUS_DEG,
                max_age_days: float | None = SIA_MAX_AGE_DAYS) -> list[ImageRef]:
    """All Level 2 images whose footprint contains (ra, dec), sorted by time then detector.

    One SIA2 query per collection, each cached on disk for `max_age_days` (None = forever).
    The same exposure in two collections is kept once (the earlier collection in the list wins).
    Raises SiaError if IRSA cannot answer and there is no cached list.
    """
    ra = float(ra) % 360.0
    dec = float(dec)
    out, seen = [], set()
    for coll in collections:
        path = _sia_cache_path(ra, dec, coll, radius_deg)
        rows = None
        if path.exists():
            age_days = (time.time() - path.stat().st_mtime) / 86400
            if max_age_days is None or age_days <= max_age_days:
                rows = json.loads(path.read_text())["rows"]
        if rows is None:
            try:
                rows = _sia_query(ra, dec, coll, radius_deg)
            except SiaError:
                if path.exists():  # stale cache beats no answer
                    log.warning("SIA2 failed for %s; using stale cache %s", coll, path)
                    rows = json.loads(path.read_text())["rows"]
                else:
                    raise
            else:
                _atomic_write_bytes(path, json.dumps({"ra": ra, "dec": dec, "collection": coll,
                                                      "radius_deg": radius_deg, "queried": time.time(),
                                                      "rows": rows}).encode())
        for r in rows:
            k = (r["obs_id"], r["detector"])
            if k in seen:
                continue
            seen.add(k)
            out.append(ImageRef(**r))
    out.sort(key=lambda r: (r.t_min, r.detector, r.obs_id))
    return out


#: Epoch rule of the contract (PLAN.md 6): split_epochs(gap_days=30, max_epoch_days=60), on ALL exposures.
EPOCH_GAP_DAYS = 30.0
MAX_EPOCH_DAYS = 60.0
MAX_PER_EPOCH_BAND = 60


def epochs_for(refs) -> tuple[np.ndarray, list[dict]]:
    """(epoch label per ref, epoch_table) from ALL refs of a field, with the contract's epoch rule."""
    from spx.coadd import epoch_table, split_epochs
    mjd = np.array([r.mjd for r in refs], float)
    lab = split_epochs(mjd, EPOCH_GAP_DAYS, MAX_EPOCH_DAYS)
    return lab, epoch_table(mjd, lab) if len(mjd) else []


def select_refs(refs, n: int = MAX_PER_EPOCH_BAND, labels=None):
    """At most n refs per (epoch, detector), spread in time (spx.coadd.select_spread), before downloading.

    labels: epoch label per ref from epochs_for(refs); computed here if omitted (from ALL refs).
    """
    from spx.coadd import select_spread
    refs = list(refs)
    if not refs:
        return []
    if labels is None:
        labels, _ = epochs_for(refs)
    mjd = np.array([r.mjd for r in refs], float)
    keep = []
    for e in np.unique(labels):
        for d in sorted({r.detector for r in refs}):
            idx = np.flatnonzero((labels == e) & np.array([r.detector == d for r in refs]))
            if len(idx):
                keep.extend(idx[select_spread(mjd[idx], n)])
    return [refs[i] for i in sorted(keep)]


# --------------------------------------------------------------------------- file layout

@dataclass
class _Layout:
    size: int
    image_hdr: str
    image_data: int
    flags_hdr: str
    flags_data: int
    flags_heap: int
    flags_desc: np.ndarray       # (nrow, 2) int64: (n_bytes, heap_offset)
    var_hdr: str
    var_data: int
    zodi_hdr: str | None
    zodi_data: int | None
    wave_hdr: str
    wave_data: bytes
    # in-memory only (rebuilt from the headers above; never saved)
    cel_hdr: str | None = None   # compact celestial WCS header (WCS + SIP cards), saved
    _info: dict | None = None
    _wcs: WCS | None = None
    _wave: WCS | None = None

    def info(self) -> dict:
        if self._info is None:
            ih = fits.Header.fromstring(self.image_hdr)
            self._info = {k: ih.get(k) for k in ("NAXIS1", "NAXIS2", "MJD-AVG", "MJD-BEG", "MJD-END", "DETECTOR",
                                                 "OBSID", "PSF_FWHM", "BUNIT", "DATE-AVG")}
        return self._info

    def celestial(self) -> WCS:
        if self._wcs is None:
            if self.cel_hdr is None:
                full = _quiet_wcs(fits.Header.fromstring(self.image_hdr), naxis=2)
                self.cel_hdr = full.to_header(relax=True).tostring()
            self._wcs = _quiet_wcs(fits.Header.fromstring(self.cel_hdr))
        return self._wcs

    def wave(self) -> WCS:
        if self._wave is None:
            self._wave = _wave_wcs(self)
        return self._wave


_layout_mem: dict[str, _Layout] = {}
_layout_lock = threading.Lock()


def _layout_path(s3_key: str) -> Path:
    return CACHE_DIR / "layout" / f"{_key_hash(s3_key)}.npz"


def _save_layout(s3_key: str, L: _Layout) -> None:
    meta = {"v": _LAYOUT_VERSION, "s3_key": s3_key, "size": L.size, "image_hdr": L.image_hdr, "image_data": L.image_data,
            "flags_hdr": L.flags_hdr, "flags_data": L.flags_data, "flags_heap": L.flags_heap,
            "var_hdr": L.var_hdr, "var_data": L.var_data, "zodi_hdr": L.zodi_hdr, "zodi_data": L.zodi_data,
            "wave_hdr": L.wave_hdr, "cel_hdr": L.cel_hdr}
    _atomic_save_npz(_layout_path(s3_key), meta=np.array(json.dumps(meta)), desc=L.flags_desc,
                     wave=np.frombuffer(L.wave_data, np.uint8))


def _load_layout(s3_key: str) -> _Layout | None:
    p = _layout_path(s3_key)
    if not p.exists():
        return None
    try:
        with np.load(io.BytesIO(p.read_bytes()), allow_pickle=False) as z:
            m = json.loads(str(z["meta"]))
            if m.get("v") != _LAYOUT_VERSION or m.get("s3_key") != s3_key:
                return None
            return _Layout(size=m["size"], image_hdr=m["image_hdr"], image_data=m["image_data"],
                           flags_hdr=m["flags_hdr"], flags_data=m["flags_data"], flags_heap=m["flags_heap"],
                           flags_desc=z["desc"].astype(np.int64), var_hdr=m["var_hdr"], var_data=m["var_data"],
                           zodi_hdr=m["zodi_hdr"], zodi_data=m["zodi_data"], wave_hdr=m["wave_hdr"],
                           wave_data=z["wave"].tobytes(), cel_hdr=m.get("cel_hdr"))
    except Exception as e:  # corrupt cache file -> refetch
        log.warning("layout cache %s unreadable (%r); refetching", p, e)
        return None


def _fetch_layout(url: str, s3_key: str) -> tuple[_Layout, int]:
    nb = 0
    # PRIMARY + IMAGE headers (measured 2880 + 23040 B)
    _, head, size = _get_range(url, 0, BLOCK * 14)
    nb += len(head)
    h0, n0, _ = _read_header(url, 0, BLOCK * 4, head)
    if not h0.get("SIMPLE"):
        raise FetchError("not a FITS file")
    off = n0 + _pad(_data_bytes(h0))
    ih, n1, buf = _read_header(url, off, BLOCK * 8, head[off:])
    nb += max(0, off + len(buf) - len(head))
    _expect(ih, "IMAGE", "first extension")
    if ih["BITPIX"] != -32 or ih["NAXIS"] != 2:
        raise FetchError(f"unexpected IMAGE BITPIX/NAXIS {ih['BITPIX']}/{ih['NAXIS']}")
    if ih.get("BSCALE", 1) != 1 or ih.get("BZERO", 0) != 0:
        raise FetchError("IMAGE has BSCALE/BZERO; not supported")
    image_data = off + n1

    # FLAGS: header (measured 17 280 B) + row descriptors (NAXIS1*NAXIS2 = 16 320 B) in one request
    foff = image_data + _pad(_data_bytes(ih))
    _, fbuf, _ = _get_range(url, foff, BLOCK * 20)
    nb += len(fbuf)
    fh, fn, fbuf = _read_header(url, foff, BLOCK * 8, fbuf)
    _expect(fh, "FLAGS", "second extension")
    if fh.get("ZCMPTYPE") != "RICE_1" or fh.get("ZTILE2") != 1 or fh.get("ZTILE1") != fh.get("ZNAXIS1") \
            or fh.get("TFIELDS") != 1 or not str(fh.get("TFORM1", "")).startswith("1P"):
        raise FetchError("FLAGS is not row-tiled RICE_1 with one P column")
    nrow, rowb = fh["NAXIS2"], fh["NAXIS1"]
    flags_data = foff + fn
    need = fn + nrow * rowb
    if len(fbuf) < need:
        _, more, _ = _get_range(url, foff + len(fbuf), need - len(fbuf))
        nb += len(more)
        fbuf += more
    desc = np.frombuffer(fbuf[fn:need], dtype=">i4").reshape(nrow, 2).astype(np.int64)
    flags_heap = flags_data + fh.get("THEAP", nrow * rowb)

    # VARIANCE header (measured 11 520 B)
    voff = flags_data + _pad(_data_bytes(fh))
    vh, vn, vbuf = _read_header(url, voff, BLOCK * 6)
    nb += len(vbuf)
    _expect(vh, "VARIANCE", "third extension")
    var_data = voff + vn

    # WCS-WAVE: last HDU of the file (measured 5 760 B); search the tail for its header
    tail_len = BLOCK * 6
    tstart, tail, _ = _get_range(url, None, tail_len)
    nb += len(tail)
    wave_hdr = wave_data = None
    for i in range(0, len(tail), BLOCK):
        if tail[i:i + 8] != b"XTENSION":
            continue
        n = _find_end(tail, i)
        if n is None or i + n > len(tail):
            continue
        h = fits.Header.fromstring(tail[i:i + n].decode("ascii"))
        if h.get("EXTNAME") == "WCS-WAVE":
            nd = _data_bytes(h)
            if i + n + nd > len(tail):
                raise FetchError("WCS-WAVE table larger than the fetched tail")
            wave_hdr, wave_data = h.tostring(), tail[i + n:i + n + nd]
            break
    if wave_hdr is None:
        raise FetchError("WCS-WAVE not found in the last 17 KB")

    L = _Layout(size=size, image_hdr=ih.tostring(), image_data=image_data, flags_hdr=fh.tostring(),
                flags_data=flags_data, flags_heap=flags_heap, flags_desc=desc, var_hdr=vh.tostring(),
                var_data=var_data, zodi_hdr=None, zodi_data=None, wave_hdr=wave_hdr, wave_data=wave_data)
    return L, nb


def _ensure_zodi(url: str, s3_key: str, L: _Layout) -> int:
    if L.zodi_data is not None:
        return 0
    vh = fits.Header.fromstring(L.var_hdr)
    zoff = L.var_data + _pad(_data_bytes(vh))
    zh, zn, zbuf = _read_header(url, zoff, BLOCK * 6)
    _expect(zh, "ZODI", "fourth extension")
    L.zodi_hdr, L.zodi_data = zh.tostring(), zoff + zn
    _save_layout(s3_key, L)
    return len(zbuf)


def _get_layout(ref: ImageRef) -> tuple[_Layout, int]:
    with _layout_lock:
        L = _layout_mem.get(ref.s3_key)
    if L is not None:
        return L, 0
    L = _load_layout(ref.s3_key)
    nb = 0
    if L is None:
        L, nb = _fetch_layout(ref.url, ref.s3_key)
        L.celestial()
        _save_layout(ref.s3_key, L)
    elif L.cel_hdr is None:
        L.celestial()
        _save_layout(ref.s3_key, L)
    with _layout_lock:
        if len(_layout_mem) > 5000:
            _layout_mem.clear()
        _layout_mem[ref.s3_key] = L
    return L, nb


# --------------------------------------------------------------------------- decoding

def _decode_flag_rows(url: str, L: _Layout, ys: int, ye: int) -> tuple[np.ndarray, int]:
    """FLAGS rows ys:ye (0-based, end exclusive) from their compressed tiles only."""
    fh = fits.Header.fromstring(L.flags_hdr)
    d = L.flags_desc[ys:ye].copy()
    lo = int(d[:, 1].min())
    hi = int((d[:, 1] + d[:, 0]).max())
    _, heap, _ = _get_range(url, L.flags_heap + lo, hi - lo)
    if len(heap) != hi - lo:
        raise FetchError("short FLAGS heap read")
    d[:, 1] -= lo
    n = ye - ys
    h = fh.copy()
    h["NAXIS2"] = n
    h["ZNAXIS2"] = n
    h["PCOUNT"] = len(heap)
    for k in ("THEAP", "CHECKSUM", "DATASUM", "ZHECKSUM", "ZDATASUM"):
        h.remove(k, ignore_missing=True)
    body = d.astype(">i4").tobytes() + heap
    body += b"\0" * (-len(body) % BLOCK)
    blob = fits.PrimaryHDU().header.tostring().encode("ascii") + h.tostring().encode("ascii") + body
    with fits.HDUList.fromstring(blob) as hl:
        arr = np.asarray(hl[1].data)
    if arr.shape != (n, fh["ZNAXIS1"]):
        raise FetchError(f"FLAGS decode gave {arr.shape}")
    return arr.astype(np.int32), len(heap)


def _read_rows(url: str, data_off: int, hdr: fits.Header, ys: int, ye: int) -> tuple[np.ndarray, int]:
    nx = hdr["NAXIS1"]
    bpp = abs(hdr["BITPIX"]) // 8
    _, raw, _ = _get_range(url, data_off + ys * nx * bpp, (ye - ys) * nx * bpp)
    if len(raw) != (ye - ys) * nx * bpp:
        raise FetchError("short image row read")
    return np.frombuffer(raw, dtype=">f4").reshape(ye - ys, nx), len(raw)


def _shift_wcs(w: WCS, x0: int, y0: int) -> WCS:
    """Same sky mapping, pixel origin moved to detector pixel (x0, y0)."""
    out = w.deepcopy()
    out.wcs.crpix = out.wcs.crpix - np.array([x0, y0], float)
    if out.sip is not None:
        s = out.sip
        out.sip = Sip(s.a, s.b, s.ap, s.bp, np.asarray(s.crpix, float) - np.array([x0, y0], float))
    out.wcs.set()
    return out


def _wave_wcs(L: _Layout) -> WCS:
    ih = fits.Header.fromstring(L.image_hdr)
    # the celestial SIP cards would otherwise be attached to the W (wavelength) solution too
    for k in [k for k in ih if k.split("_")[0] in ("A", "B", "AP", "BP")]:
        del ih[k]
    wh = fits.Header.fromstring(L.wave_hdr)
    hl = fits.HDUList([fits.PrimaryHDU(), fits.BinTableHDU.fromstring(wh.tostring().encode("ascii") + L.wave_data
                                                                      + b"\0" * (-len(L.wave_data) % BLOCK))])
    w = _quiet_wcs(header=ih, fobj=hl, key="W")
    w.sip = None
    return w


def _bilinear(a: np.ndarray, x: float, y: float) -> float:
    ny, nx = a.shape
    i0, j0 = int(np.floor(y)), int(np.floor(x))
    if not (0 <= i0 < ny - 1 and 0 <= j0 < nx - 1):
        return float("nan")
    fy, fx = y - i0, x - j0
    v = (a[i0, j0] * (1 - fx) * (1 - fy) + a[i0, j0 + 1] * fx * (1 - fy)
         + a[i0 + 1, j0] * (1 - fx) * fy + a[i0 + 1, j0 + 1] * fx * fy)
    return float(v)


def _cutout_path(s3_key: str, x0: int, y0: int, n: int) -> Path:
    return CACHE_DIR / "cutout" / _key_hash(s3_key) / f"{x0}_{y0}_{n}.npz"


def _cut_one(ref: ImageRef, ra: float, dec: float, half_px: int, layers: tuple):
    """Returns (Cutout | None, reason, bytes_downloaded, from_cache)."""
    L, nb = _get_layout(ref)
    url = ref.url
    info = L.info()
    nx, ny = info["NAXIS1"], info["NAXIS2"]
    wfull = L.celestial()
    tx, ty = (float(v) for v in wfull.world_to_pixel_values(ra, dec))
    if not (np.isfinite(tx) and np.isfinite(ty)):
        return None, "WCS gives no pixel", nb, False
    xc, yc = int(np.floor(tx + 0.5)), int(np.floor(ty + 0.5))
    n = 2 * half_px + 1
    x0, y0 = xc - half_px, yc - half_px
    xs, xe = max(0, x0), min(nx, x0 + n)
    ys, ye = max(0, y0), min(ny, y0 + n)
    if xe <= xs or ye <= ys:
        return None, "position off the detector", nb, False
    if not (0 <= xc < nx and 0 <= yc < ny):
        # cutout touches the detector but the target pixel itself is outside: still useful for
        # a wide field, so it is kept; coverage maps show the hole.
        pass

    cpath = _cutout_path(ref.s3_key, x0, y0, n)
    cached = {}
    if cpath.exists():
        try:
            with np.load(io.BytesIO(cpath.read_bytes()), allow_pickle=False) as z:
                cached = {k: z[k] for k in z.files}
        except Exception as e:
            log.warning("cutout cache %s unreadable (%r)", cpath, e)
            cached = {}
    want = {"IMAGE": "image", "FLAGS": "flags", "VARIANCE": "variance", "ZODI": "zodi"}
    missing = [l for l in layers if want[l] not in cached]
    need_wave = "wavelength_map" not in cached
    from_cache = not missing and nb == 0
    sl = (slice(ys - y0, ye - y0), slice(xs - x0, xe - x0))

    for lay in missing:
        if lay == "FLAGS":
            rows, b = _decode_flag_rows(url, L, ys, ye)
            arr = np.full((n, n), OFF_DETECTOR_FLAG, np.int32)
            arr[sl] = rows[:, xs:xe]
        else:
            if lay == "IMAGE":
                hdr, off = fits.Header.fromstring(L.image_hdr), L.image_data
            elif lay == "VARIANCE":
                hdr, off = fits.Header.fromstring(L.var_hdr), L.var_data
            else:
                nb += _ensure_zodi(url, ref.s3_key, L)
                hdr, off = fits.Header.fromstring(L.zodi_hdr), L.zodi_data
            rows, b = _read_rows(url, off, hdr, ys, ye)
            arr = np.full((n, n), np.nan, np.float32)
            arr[sl] = rows[:, xs:xe]
        nb += b
        cached[want[lay]] = arr
    if need_wave:
        # wavelength per pixel (WCS-WAVE is in detector coordinates)
        wl = np.full((n, n), np.nan, np.float32)
        bw = np.full((n, n), np.nan, np.float32)
        yy, xx = np.mgrid[ys:ye, xs:xe]
        wv, bv = L.wave().pixel_to_world_values(xx.astype(float), yy.astype(float))
        wl[sl], bw[sl] = wv, bv
        cached["wavelength_map"], cached["bandwidth_map"] = wl, bw
    if missing or need_wave:
        _atomic_save_npz(cpath, **cached)
    wl, bw = cached["wavelength_map"], cached["bandwidth_map"]
    # at the exact position: bilinear in the per-pixel map (the LVF changes ~1e-3 um per pixel)
    wt, bt = _bilinear(wl, tx - x0, ty - y0), _bilinear(bw, tx - x0, ty - y0)
    if not (np.isfinite(wt) and np.isfinite(bt)):
        wt, bt = (float(v) for v in L.wave().pixel_to_world_values(tx, ty))

    cut = Cutout(
        image=cached["image"], flags=cached.get("flags"), variance=cached.get("variance"), zodi=cached.get("zodi"),
        wcs=_shift_wcs(wfull, x0, y0), x0=x0, y0=y0, mjd=float(info["MJD-AVG"] or ref.mjd),
        detector=int(info["DETECTOR"] or ref.detector), obs_id=str(info["OBSID"] or ref.obs_id),
        wavelength_um=wt, bandwidth_um=bt, wavelength_map=wl, bandwidth_map=bw,
        target_xy=(tx - x0, ty - y0), collection=ref.collection, s3_key=ref.s3_key,
        mjd_beg=float(info["MJD-BEG"] or ref.t_min), mjd_end=float(info["MJD-END"] or ref.t_max),
        psf_fwhm_arcsec=float(info["PSF_FWHM"] or np.nan),
        meta={"bunit": info["BUNIT"], "date_avg": info["DATE-AVG"], "target_on_detector": bool(0 <= xc < nx and 0 <= yc < ny)},
    )
    return cut, "", nb, from_cache


def fetch_cutouts(refs, ra: float, dec: float, half_px: int = 50, layers=("IMAGE", "FLAGS", "VARIANCE"),
                  workers: int = S3_MAX_CONCURRENCY, progress=None, stats: dict | None = None):
    """Cut a (2*half_px+1)^2 square around (ra, dec) out of every image in `refs`.

    Returns (cutouts sorted by mjd then detector, n_skipped). Unreadable images and images where the
    square misses the detector are skipped and logged, never raised.
    progress(done, total) is called from worker threads after each image.
    stats, if given, is filled with counts, bytes downloaded, seconds and skip reasons.
    """
    layers = tuple(dict.fromkeys(("IMAGE",) + tuple(l.upper() for l in layers)))
    bad = [l for l in layers if l not in LAYERS]
    if bad:
        raise ValueError(f"unknown layers {bad}; choose from {LAYERS}")
    ra = float(ra) % 360.0
    dec = float(dec)
    refs = list(refs)
    t0 = time.time()
    out, reasons = [], {}
    nbytes = ncached = 0
    lock = threading.Lock()
    done = 0

    def work(ref):
        try:
            return _cut_one(ref, ra, dec, int(half_px), layers)
        except Exception as e:  # never raise for one bad image
            log.warning("skip %s D%s: %r", ref.obs_id, ref.detector, e)
            return None, f"{type(e).__name__}: {e}"[:200], 0, False

    with ThreadPoolExecutor(max_workers=max(1, min(int(workers), S3_MAX_CONCURRENCY))) as ex:
        futs = [ex.submit(work, r) for r in refs]
        for f in as_completed(futs):
            cut, reason, nb, hit = f.result()
            with lock:
                done += 1
                nbytes += nb
                if cut is None:
                    reasons[reason] = reasons.get(reason, 0) + 1
                else:
                    out.append(cut)
                    ncached += hit
            if progress:
                try:
                    progress(done, len(refs))
                except Exception:
                    log.exception("progress callback failed")
    out.sort(key=lambda c: (c.mjd, c.detector))
    n_skipped = len(refs) - len(out)
    if stats is not None:
        stats.update({"n_refs": len(refs), "n_ok": len(out), "n_skipped": n_skipped, "n_from_cache": ncached,
                      "bytes": nbytes, "seconds": time.time() - t0, "skip_reasons": reasons})
    return out, n_skipped
