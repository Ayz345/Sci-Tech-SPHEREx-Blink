"""spx.access tests.

Offline: a synthetic Level 2-like file (same HDU order and FLAGS tiling as SPHEREx DR1) is built in memory and
the range-request layer is pointed at its bytes; every cutout layer must equal astropy's full read.
Network (SPX_NETWORK=1): the same comparison on one real DR1 file from S3 (downloads ~66 MB once).
"""
from __future__ import annotations

import io
import os
import warnings

import numpy as np
import pytest
from astropy.io import fits
from astropy.wcs import WCS

from spx import access

NX, NY = 180, 200


def _image_header():
    h = fits.Header()
    h["CTYPE1"], h["CTYPE2"] = "RA---TAN-SIP", "DEC--TAN-SIP"
    h["CRPIX1"], h["CRPIX2"] = 90.5, 100.5
    h["CRVAL1"], h["CRVAL2"] = 133.78, -7.24
    h["CDELT1"], h["CDELT2"] = 1.0, 1.0
    h["PC1_1"], h["PC1_2"], h["PC2_1"], h["PC2_2"] = 0.00162, -0.00048, -0.00054, -0.00164
    h["CUNIT1"], h["CUNIT2"] = "deg", "deg"
    h["A_ORDER"], h["B_ORDER"] = 2, 2
    h["A_0_2"], h["A_1_1"], h["A_2_0"] = 2e-6, -1e-6, 3e-6
    h["B_0_2"], h["B_1_1"], h["B_2_0"] = -1e-6, 2e-6, 1e-6
    h["AP_ORDER"], h["BP_ORDER"] = 2, 2
    h["AP_0_2"], h["AP_1_1"], h["AP_2_0"] = -2e-6, 1e-6, -3e-6
    h["BP_0_2"], h["BP_1_1"], h["BP_2_0"] = 1e-6, -2e-6, -1e-6
    for k, v in {"WCSAXESW": 2, "CRPIX1W": 1, "CRPIX2W": 1, "CDELT1W": 1, "CDELT2W": 1, "CUNIT1W": "um",
                 "CUNIT2W": "um", "CTYPE1W": "WAVE-TAB", "CTYPE2W": "WAVE-TAB", "CRVAL1W": 1, "CRVAL2W": 1,
                 "PV1_3W": 1, "PV2_3W": 2, "PS1_0W": "WCS-WAVE", "PS2_0W": "WCS-WAVE", "PS1_1W": "VALUES",
                 "PS2_1W": "VALUES", "PS1_2W": "X", "PS2_2W": "Y"}.items():
        h[k] = v
    h["BUNIT"] = "MJy / sr"
    h["DETECTOR"] = 3
    h["OBSID"] = "TEST_0001_1"
    h["MJD-BEG"], h["MJD-AVG"], h["MJD-END"] = 60800.70, 60800.7007, 60800.7014
    h["PSF_FWHM"] = 7.4
    return h


def _synthetic_file():
    rng = np.random.default_rng(1)
    ih = _image_header()
    img = rng.normal(1.0, 0.1, (NY, NX)).astype(np.float32)
    flags = rng.integers(0, 1 << 29, (NY, NX)).astype(np.int32)
    flags[rng.random((NY, NX)) < 0.7] = 0  # mostly clean, like the real data
    var = rng.random((NY, NX)).astype(np.float32)
    zodi = rng.random((NY, NX)).astype(np.float32)
    xs = np.array([1, 60, 120, NX], np.int32)
    ys = np.array([1, 50, 100, 150, NY], np.int32)
    vals = np.zeros((len(ys), len(xs), 2), np.float32)
    vals[..., 0] = 1.63 + 0.004 * ys[:, None] + 0.0001 * xs[None, :]
    vals[..., 1] = 0.04 + 0.00001 * xs[None, :]
    wave = fits.BinTableHDU.from_columns([
        fits.Column("X", f"{len(xs)}J", array=xs[None]),
        fits.Column("Y", f"{len(ys)}J", array=ys[None]),
        fits.Column("VALUES", f"{vals.size}E", dim=f"(2,{len(xs)},{len(ys)})", array=vals[None]),
    ], name="WCS-WAVE")
    epsf = fits.BinTableHDU.from_columns([fits.Column("BINX", "J", array=np.arange(3))], name="EPSF")
    hl = fits.HDUList([
        fits.PrimaryHDU(),
        fits.ImageHDU(img, ih, name="IMAGE"),
        fits.CompImageHDU(flags, name="FLAGS", compression_type="RICE_1", tile_shape=(1, NX)),
        fits.ImageHDU(var, name="VARIANCE"),
        fits.ImageHDU(zodi, name="ZODI"),
        epsf, wave,
    ])
    buf = io.BytesIO()
    hl.writeto(buf)
    return buf.getvalue()


@pytest.fixture()
def synthetic(tmp_path, monkeypatch):
    blob = _synthetic_file()
    calls = []

    def fake_get_range(url, start, length, retries=4, timeout=60.0):
        total = len(blob)
        if start is None:
            a = max(0, total - length)
        else:
            a = start
            if a >= total:
                raise access.FetchError("416")
        b = min(total, a + length)
        calls.append((a, b - a))
        return a, blob[a:b], total

    monkeypatch.setattr(access, "_get_range", fake_get_range)
    monkeypatch.setattr(access, "CACHE_DIR", tmp_path)
    access._layout_mem.clear()
    with fits.open(io.BytesIO(blob)) as hl:
        full = {k: np.asarray(hl[k].data) for k in ("IMAGE", "FLAGS", "VARIANCE", "ZODI")}
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            ih = hl["IMAGE"].header.copy()
            for k in [k for k in ih if k.split("_")[0] in ("A", "B", "AP", "BP")]:
                del ih[k]
            ww = WCS(ih, fobj=hl, key="W")
            ww.sip = None
            ww.pixel_to_world_values(1.0, 1.0)
        wcel = WCS(hl["IMAGE"].header, naxis=2)
    ref = access.ImageRef(obs_id="TEST_0001_1", detector=3, collection="test", s3_key="test/file.fits",
                          access_url="", t_min=60800.70, t_max=60800.7014, em_min_um=1.6, em_max_um=2.4,
                          bandpass="SPHEREx-D3", exptime_s=113.6, s_region="")
    yield {"ref": ref, "full": full, "ww": ww, "wcel": wcel, "calls": calls, "size": len(blob)}
    access._layout_mem.clear()


def _check(cut, s):
    n = cut.image.shape[0]
    yy, xx = np.mgrid[cut.y0:cut.y0 + n, cut.x0:cut.x0 + n]
    inb = (xx >= 0) & (yy >= 0) & (xx < NX) & (yy < NY)
    assert inb.any()
    for name, arr in (("IMAGE", cut.image), ("FLAGS", cut.flags), ("VARIANCE", cut.variance), ("ZODI", cut.zodi)):
        if arr is None:
            continue
        assert np.array_equal(arr[inb], s["full"][name][yy[inb], xx[inb]]), name
        if name == "FLAGS":
            assert np.all(arr[~inb] == access.OFF_DETECTOR_FLAG)
        else:
            assert np.all(np.isnan(arr[~inb]))
    wl, bw = s["ww"].pixel_to_world_values(xx[inb].astype(float), yy[inb].astype(float))
    np.testing.assert_allclose(cut.wavelength_map[inb], wl, rtol=1e-6)
    np.testing.assert_allclose(cut.bandwidth_map[inb], bw, rtol=1e-6)
    assert np.all(np.isnan(cut.wavelength_map[~inb]))


@pytest.mark.parametrize("px,py", [(90.3, 100.2), (3.4, 2.6), (176.6, 120.0), (40.0, 198.9)])
def test_cutout_matches_full_read(synthetic, px, py):
    s = synthetic
    ra, dec = (float(v) for v in s["wcel"].pixel_to_world_values(px, py))
    st = {}
    cuts, nskip = access.fetch_cutouts([s["ref"]], ra, dec, half_px=20,
                                       layers=("IMAGE", "FLAGS", "VARIANCE", "ZODI"), stats=st)
    assert nskip == 0 and len(cuts) == 1
    c = cuts[0]
    assert c.image.shape == (41, 41) and c.image.dtype == np.float32 and c.flags.dtype == np.int32
    _check(c, s)
    # WCS shifted to the cutout: same sky for the same detector pixel
    tx, ty = c.target_xy
    assert abs(tx + c.x0 - px) < 1e-6 and abs(ty + c.y0 - py) < 1e-6
    a = np.array(c.wcs.pixel_to_world_values([0.0, 7.5, 40.0], [0.0, 12.0, 40.0]))
    b = np.array(s["wcel"].pixel_to_world_values(np.array([0.0, 7.5, 40.0]) + c.x0, np.array([0.0, 12.0, 40.0]) + c.y0))
    assert np.abs(a - b).max() * 3600 < 1e-6
    wl, bw = s["ww"].pixel_to_world_values(px, py)
    assert abs(c.wavelength_um - float(wl)) < 1e-4 and abs(c.bandwidth_um - float(bw)) < 1e-5
    assert c.detector == 3 and c.obs_id == "TEST_0001_1" and c.mjd == pytest.approx(60800.7007)
    # never asked for the whole file
    assert max(n for _, n in s["calls"]) < s["size"] // 4


def test_second_fetch_comes_from_cache(synthetic):
    s = synthetic
    ra, dec = (float(v) for v in s["wcel"].pixel_to_world_values(80.0, 90.0))
    st1, st2 = {}, {}
    access.fetch_cutouts([s["ref"]], ra, dec, half_px=15, stats=st1)
    assert st1["bytes"] > 0
    access._layout_mem.clear()  # disk cache only
    s["calls"].clear()
    cuts, _ = access.fetch_cutouts([s["ref"]], ra, dec, half_px=15, stats=st2)
    assert st2["bytes"] == 0 and st2["n_from_cache"] == 1 and not s["calls"]
    _check(cuts[0], s)


def test_missing_layer_is_added_to_cache(synthetic):
    s = synthetic
    ra, dec = (float(v) for v in s["wcel"].pixel_to_world_values(80.0, 90.0))
    access.fetch_cutouts([s["ref"]], ra, dec, half_px=15, layers=("IMAGE",))
    cuts, _ = access.fetch_cutouts([s["ref"]], ra, dec, half_px=15, layers=("IMAGE", "ZODI"))
    assert cuts[0].zodi is not None and cuts[0].flags is None
    _check(cuts[0], s)


def test_off_detector_and_broken_refs_are_skipped(synthetic, monkeypatch):
    s = synthetic
    ra, dec = (float(v) for v in s["wcel"].pixel_to_world_values(NX + 500.0, NY + 500.0))
    st = {}
    cuts, nskip = access.fetch_cutouts([s["ref"]], ra, dec, half_px=10, stats=st)
    assert cuts == [] and nskip == 1 and "position off the detector" in st["skip_reasons"]

    def boom(*a, **k):
        raise access.FetchError("HTTP 503")
    monkeypatch.setattr(access, "_get_range", boom)
    access._layout_mem.clear()
    bad = access.ImageRef(**{**s["ref"].to_dict(), "s3_key": "test/other.fits"})
    cuts, nskip = access.fetch_cutouts([bad], ra, dec, half_px=10)
    assert cuts == [] and nskip == 1


def test_busy_cache_file_does_not_lose_the_cutout(synthetic, monkeypatch):
    s = synthetic
    ra, dec = (float(v) for v in s["wcel"].pixel_to_world_values(80.0, 90.0))

    def busy(*a, **k):
        raise PermissionError(13, "file in use by another process")
    monkeypatch.setattr(access.os, "replace", busy)
    monkeypatch.setattr(access.time, "sleep", lambda t: None)
    cuts, nskip = access.fetch_cutouts([s["ref"]], ra, dec, half_px=15)
    assert nskip == 0
    _check(cuts[0], s)


def test_select_refs_caps_per_epoch_and_detector():
    refs = []
    for e, t0 in enumerate((60800.0, 61000.0)):
        for d in (1, 6):
            for i in range(80):
                refs.append(access.ImageRef(f"o{e}_{i}", d, "c", f"k{e}_{d}_{i}", "", t0 + i * 0.1, t0 + i * 0.1 + 0.001,
                                            1, 2, f"SPHEREx-D{d}", 100.0, ""))
    out = access.select_refs(refs, 10)
    assert len(out) == 2 * 2 * 10
    labels, table = access.epochs_for(refs)
    assert len(table) == 2


def test_bilinear():
    a = np.arange(12, dtype=float).reshape(3, 4)
    assert access._bilinear(a, 1.5, 0.5) == pytest.approx(3.5)
    assert np.isnan(access._bilinear(a, 3.5, 0.0))


@pytest.mark.skipif(os.environ.get("SPX_NETWORK") != "1", reason="set SPX_NETWORK=1 to run against S3/IRSA")
def test_real_file_against_full_download(tmp_path, monkeypatch):
    import requests
    monkeypatch.setattr(access, "CACHE_DIR", tmp_path)
    access._layout_mem.clear()
    refs = access.find_images(133.7951, -7.2451)
    assert len(refs) > 100
    ref = [r for r in refs if r.detector == 3][0]
    blob = requests.get(ref.url, timeout=600).content
    with fits.open(io.BytesIO(blob)) as hl:
        wcel = WCS(hl["IMAGE"].header, naxis=2)
        full = {k: np.asarray(hl[k].data) for k in ("IMAGE", "FLAGS", "VARIANCE", "ZODI")}
    nx, ny = full["IMAGE"].shape[1], full["IMAGE"].shape[0]
    for px, py in [(1020.0, 1020.0), (10.3, 5.7), (nx - 4.8, 1500.4)]:
        ra, dec = (float(v) for v in wcel.pixel_to_world_values(px, py))
        cuts, nskip = access.fetch_cutouts([ref], ra, dec, half_px=50, layers=("IMAGE", "FLAGS", "VARIANCE", "ZODI"))
        c = cuts[0]
        n = c.image.shape[0]
        yy, xx = np.mgrid[c.y0:c.y0 + n, c.x0:c.x0 + n]
        inb = (xx >= 0) & (yy >= 0) & (xx < nx) & (yy < ny)
        for name, arr in (("IMAGE", c.image), ("FLAGS", c.flags), ("VARIANCE", c.variance), ("ZODI", c.zodi)):
            assert np.array_equal(arr[inb], full[name][yy[inb], xx[inb]], equal_nan=name != "FLAGS"), name
