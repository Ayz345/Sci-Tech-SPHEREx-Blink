"""Write fake data in the v1 contract layout (PLAN.md section 5) for testing the web page.

    python web/tool/dev-fixtures/make_fixtures.py
    -> web/dev-fixtures/data/index.json, fake-mover/..., fake-nocover/...

Open with:  http://localhost:8765/?data=dev-fixtures/data&target=fake-mover
Everything here is synthetic; nothing comes from SPHEREx.
"""
import json
import math
from pathlib import Path

import numpy as np

OUT = Path(__file__).resolve().parent / "data"
rng = np.random.default_rng(42)

NY = NX = 140
PIX_DEG = 3.0 / 3600
CRVAL = (133.78, -7.244)
CRPIX = (70.5, 70.5)  # FITS, 1-based
EPOCHS = [
    {"id": 0, "date": "2025-05-06", "mjd_mid": 60801.2, "mjd_min": 60800.7, "mjd_max": 60803.1},
    {"id": 1, "date": "2025-11-07", "mjd_mid": 60986.4, "mjd_min": 60984.9, "mjd_max": 60988.0},
    {"id": 2, "date": "2026-05-02", "mjd_mid": 61162.1, "mjd_min": 61160.7, "mjd_max": 61163.5},
]
BANDS = [
    ("D1", "0.75–1.09 µm", [0.75, 1.09]),
    ("D2", "1.10–1.62 µm", [1.10, 1.62]),
    ("D3", "1.63–2.41 µm", [1.63, 2.41]),
    ("D4", "2.42–3.82 µm", [2.42, 3.82]),
    ("D5", "3.83–4.41 µm", [3.83, 4.41]),
    ("D6", "4.42–5.00 µm", [4.42, 5.00]),
    ("W", "4.3–5.0 µm", [4.3, 5.0]),
]


def pix2world(x, y):
    """0-based grid pixel -> (ra, dec) deg, plain TAN."""
    d2r = math.pi / 180
    xi = (x - (CRPIX[0] - 1)) * -PIX_DEG * d2r
    eta = (y - (CRPIX[1] - 1)) * PIX_DEG * d2r
    a0, d0 = CRVAL[0] * d2r, CRVAL[1] * d2r
    den = math.cos(d0) - eta * math.sin(d0)
    ra = a0 + math.atan2(xi, den)
    dec = math.atan2(eta * math.cos(d0) + math.sin(d0), math.hypot(xi, den))
    return (ra / d2r) % 360, dec / d2r


yy, xx = np.mgrid[0:NY, 0:NX]


def psf(x, y, flux, sigma=1.8):
    return flux * np.exp(-((xx - x) ** 2 + (yy - y) ** 2) / (2 * sigma**2))


# static stars: (x, y, flux at short wavelength, flux at long wavelength)
stars = [(rng.uniform(5, NX - 5), rng.uniform(5, NY - 5), f, f * rng.uniform(0.2, 0.6))
         for f in rng.uniform(0.05, 1.5, 40)]
stars.append((80.0, 72.0, 1.0, 0.4))  # the "Gaia neighbour" the mover approaches
mover = [(66.0, 69.0), (70.0, 70.5), (74.0, 72.0)]  # cold mover: bright only at long wavelengths
variable = (40.0, 100.0, [0.4, 1.2, 0.7])  # variable star, flux per epoch


def frame(band_idx, ep):
    long = band_idx >= 4
    img = rng.normal(0, 0.03 if long else 0.02, (NY, NX))
    for x, y, fs, fl in stars:
        img += psf(x, y, fl if long else fs)
    mx, my = mover[ep]
    img += psf(mx, my, 0.5 if long else 0.01)
    vx, vy, vf = variable
    img += psf(vx, vy, vf[ep] * (0.5 if long else 1.0))
    img += psf(105.0, 35.0, 6.0 if not long else 2.0, sigma=2.2)  # bright star
    if not long:
        img[(xx - 105) ** 2 + (yy - 35) ** 2 <= 4] = np.inf  # saturated core (+Infinity, contract v1)
    img[:, :6] = np.nan  # a strip without coverage
    return img.astype("<f4")


def write_f32(path, arr):
    path.write_bytes(np.ascontiguousarray(arr, dtype="<f4").tobytes())


def make_mover():
    key = "fake-mover"
    d = OUT / key
    d.mkdir(parents=True, exist_ok=True)
    bands = []
    for bi, (bid, label, wave) in enumerate(BANDS):
        n_exp = [int(rng.integers(10, 20)) for _ in EPOCHS]
        if bid == "D3":
            n_exp[2] = 0  # tests the "no data for this epoch" case
        status = "pending" if bid == "D5" else "ready"  # D5: band json and frames missing on purpose
        bands.append({"id": bid, "label": label, "wave_um": wave, "n_exposures": n_exp, "status": status})
        if status != "ready":
            continue
        frames = []
        for ep in EPOCHS:
            if n_exp[ep["id"]] == 0:
                continue
            img = frame(bi, ep["id"])
            frames.append(img)
            write_f32(d / f"{ep['id']}_{bid}.f32", img)
            weight = np.full((NY, NX), n_exp[ep["id"]], "<f4") - rng.integers(0, 4, (NY, NX))
            weight[:, :6] = 0
            write_f32(d / f"{ep['id']}_{bid}_weight.f32", weight)
            write_f32(d / f"{ep['id']}_{bid}_flagfrac.f32", rng.beta(1, 12, (NY, NX)))
        allv = np.concatenate([f[np.isfinite(f)] for f in frames])
        band_json = {
            "schema": 1, "key": key, "band": bid,
            "scale": {"vmin": float(np.percentile(allv, 1)), "vmax": float(np.percentile(allv, 99.7)), "soft": 0.05},
            "epochs": [{"id": ep["id"], "n_used": max(0, n_exp[ep["id"]] - 2), "noise": 0.03 if bi >= 4 else 0.02,
                        "mjd_mid": ep["mjd_mid"]} for ep in EPOCHS],
        }
        if bid == "D2":
            del band_json["scale"]  # tests the percentile fallback
        # low-coverage frames: D4 epoch 0 is skipped by default; D6 has only one good frame, so the page
        # switches "show low-coverage frames" on by itself
        for e in band_json["epochs"]:
            if (bid == "D4" and e["id"] == 0) or (bid == "D6" and e["id"] in (0, 1)):
                e["n_used"], e["quality"] = 3, "low"
        (d / f"{bid}.json").write_text(json.dumps(band_json, indent=1, ensure_ascii=False), encoding="utf-8")

    qra, qdec = pix2world(30, 40)  # "catalogue position" far from where the mover is now
    manifest = {
        "schema": 1, "key": key, "name": "Fake mover (synthetic)",
        "query": {"ra": round(qra, 5), "dec": round(qdec, 5), "fov_deg": NX * PIX_DEG},
        "collections": ["spherex_dr1", "spherex_qr3"],
        "data_range": {"mjd_min": EPOCHS[0]["mjd_min"], "mjd_max": EPOCHS[-1]["mjd_max"]},
        "grid": {"ctype": ["RA---TAN", "DEC--TAN"], "crval": list(CRVAL), "crpix": list(CRPIX),
                 "cdelt": [-PIX_DEG, PIX_DEG], "shape": [NY, NX]},
        "epochs": EPOCHS,
        "bands": bands,
        "default_band": "W",
        "target_track": [dict(zip(("ra", "dec"), pix2world(*mover[e["id"]])), epoch=e["id"]) for e in EPOCHS],
        "target_source": "synthetic",
        "processing": "synthetic fixture: gaussian PSFs + noise (make_fixtures.py)",
        "notes": ["Synthetic data. The mover is visible only in D5/D6/W and approaches a static star.",
                  "D3 has no data in epoch 2; D5 is pending (no files); D2 has no scale (percentile fallback)."],
        "acknowledgement": "Placeholder: the IRSA SPHEREx acknowledgement text comes from the real manifest.",
    }
    (d / "manifest.json").write_text(json.dumps(manifest, indent=1, ensure_ascii=False), encoding="utf-8")

    gaia = []
    for x, y, fs, _ in stars:
        ra, dec = pix2world(x, y)
        gaia.append({"ra": ra, "dec": dec, "pmra": 0.0, "pmdec": 0.0, "g_mag": round(18 - 2.5 * math.log10(fs / 0.05), 2),
                     "ref_epoch": 2016.0})
    (d / "gaia.json").write_text(json.dumps(gaia), encoding="utf-8")

    spec = []
    for ep in EPOCHS:
        for _ in range(40):
            w = float(rng.uniform(0.75, 5.0))
            f = 0.02 + 0.5 / (1 + math.exp(-(w - 3.8) * 4))
            spec.append({"wavelength_um": round(w, 4), "bandwidth_um": round(w / 40, 4),
                         "flux": round(f + float(rng.normal(0, 0.03)), 5), "err": 0.03, "mjd": ep["mjd_mid"] + float(rng.uniform(-1, 1)),
                         "epoch": ep["id"], "band": f"D{min(6, 1 + int(w > 1.09) + int(w > 1.62) + int(w > 2.41) + int(w > 3.82) + int(w > 4.41))}",
                         "flagged": bool(rng.random() < 0.05)})
    names = ["TRANSIENT", "SOURCE", "OUTLIER", "PERSIST"]  # F3: FLAGS bit names in the aperture (synthetic choice)
    for p in spec:
        k = int(rng.integers(0, 3))
        p["flags"] = sorted(set(rng.choice(names, size=k, replace=False).tolist())) if k else []
    for p in spec:  # psf_deblended fields (contract addition proposed 14 Nov): neighbour flux removed per point
        p["neighbor_flux"] = round(0.8 if p["wavelength_um"] < 2.4 else 0.3, 3)
        p["overflow"] = 1.2 <= p["wavelength_um"] < 1.6  # recovered from overflow: valid, kept in the light curve
        if p["overflow"]:
            p["flagged"] = True
        p["saturated"] = p["wavelength_um"] < 1.2  # a few saturated exposures: lower limits + wing brightness
        if p["saturated"]:
            p["flagged"] = True
            p["wing_flux"] = round(3.0 + 1.5 * p["epoch"] + float(rng.normal(0, 0.2)), 3)
            p["wing_err"] = 0.2
    meta = {"flux_unit": "mJy", "aperture_radius_px": 2, "background_annulus_px": [4, 7], "aperture_corrected": False,
            "method": "synthetic", "photometry": "psf_deblended",
            "wing": "synthetic: wing brightness relative to control stars",
            "wing_bands": {"D1": {"ratio": 2.42, "threshold": 1.47, "significant": True},
                           "D2": {"ratio": 1.31, "threshold": 1.47, "significant": False}},
            "deblended_neighbors": [{"source_id": "0000000000000000001", "g_mag": 16.3, "ra": 133.76, "dec": -7.24},
                                    {"source_id": None, "g_mag": None, "ra": 133.77, "dec": -7.25}],
            "n_points": len(spec), "n_flagged": sum(p["flagged"] for p in spec),
            # SYNTHETIC placeholders: the real one-line meanings come from the Explanatory Supplement (Table 16)
            "flag_legend": {n: f"SYNTHETIC placeholder meaning for {n} (fixture)" for n in names + ["HOT", "COLD"]}}
    (d / "spectrum.json").write_text(json.dumps({"meta": meta, "points": spec}), encoding="utf-8")

    # F2: automatic candidates in the PLAN.md section 17 layout (synthetic)
    def pos(xy, ep):
        ra, dec = pix2world(*xy)
        return {"epoch": ep["id"], "ra": round(ra, 6), "dec": round(dec, 6), "snr": 9.0}
    gx, gy = stars[3][0], stars[3][1]
    cands = {
        "schema": 1, "method": "SYNTHETIC fixture: matched filter on difference frames (no real search)",
        "params": {"snr_min": 5.0, "match_radius_px": 2.0}, "bands_searched": ["D5", "D6", "W"],
        "candidates": [
            {"id": "c1", "kind": "mover", "snr": 9.0, "bands": ["W", "D6"], "positions": [pos(mover[e["id"]], e) for e in EPOCHS],
             "rate_arcsec_per_year": [-23.9, 7.3], "near_gaia": None, "notes": "synthetic mover", "grade": "A", "n_bands": 2},
            {"id": "c2", "kind": "variable", "snr": 7.5, "bands": ["D1"], "positions": [pos(variable[:2], EPOCHS[1])],
             "rate_arcsec_per_year": None, "near_gaia": None, "notes": "synthetic variable", "grade": "B", "n_bands": 1},
            {"id": "c3", "kind": "known", "snr": 6.1, "bands": ["W"], "positions": [pos((gx, gy), e) for e in EPOCHS],
             "rate_arcsec_per_year": [0.0, 0.0], "near_gaia": {"source_id": "0000000000000000003", "sep_arcsec": 0.8, "g_mag": 15.2},
             "notes": "synthetic: explained by a Gaia star"},
        ],
        "rejected": {"alignment residual (same direction on bright stars)": 4, "masked: near a saturated core": 2},
        "validation": ["SYNTHETIC limit: objects moving less than about 10″ are mostly not found (fixture).", "SYNTHETIC injection result: not run on this fixture.", "SYNTHETIC grades: A = found in two or more bands, B = one band (fixture).", "technical: per-band statistics would follow here (fixture)."],
    }
    (d / "candidates.json").write_text(json.dumps(cands, indent=1), encoding="utf-8")

    # WISE layers (SYNTHETIC; field names proposed by the web page until the data session fixes them).
    # The mover moves 8 px/yr west and 3 px/yr north on this 3″ grid: pmra -24000, pmdec +9000 mas/yr.
    wise_mjd, t_now = 58300.0, EPOCHS[0]["mjd_mid"]
    dt = (wise_mjd - t_now) / 365.25
    mx, my = mover[0][0] + 8 * dt, mover[0][1] + 3 * dt  # where the mover was in 2018
    for wb, sig in (("W1", 2.6), ("W2", 2.8)):
        img = rng.normal(0, 0.02, (NY, NX))
        for x, y, fs, fl in stars:
            img += psf(x, y, fl * (1.4 if wb == "W1" else 1.0), sigma=sig)
        img += psf(mx, my, 0.05 if wb == "W1" else 0.6, sigma=sig)
        write_f32(d / f"wise_{wb}.f32", img)
    (d / "wise.json").write_text(json.dumps({
        "schema": 1, "survey": "SYNTHETIC (stands in for unWISE)", "epoch_mjd": wise_mjd, "date": "2018-07-04",
        "bands": [{"id": "W1", "file": "wise_W1.f32", "wave_um": 3.4, "scale": {"vmin": -0.05, "vmax": 1.2, "soft": 0.05}},
                  {"id": "W2", "file": "wise_W2.f32", "wave_um": 4.6, "scale": {"vmin": -0.05, "vmax": 0.9, "soft": 0.05}}],
        "note": "SYNTHETIC fixture: resampled onto this field's grid."}, indent=1), encoding="utf-8")
    cw = [{"source_name": f"SYN J{i:04d}", "ra": ra, "dec": dec, "pmra": 0.0, "pmdec": 0.0, "w1mag": 14.0, "w2mag": 13.8,
           "ref_mjd": 57170.0} for i, (ra, dec) in enumerate(pix2world(x, y) for x, y, *_ in stars[:12])]
    rx, ry = mover[0][0] + 8 * (57170.0 - t_now) / 365.25, mover[0][1] + 3 * (57170.0 - t_now) / 365.25
    ra, dec = pix2world(rx, ry)
    cw.append({"source_name": "SYN MOVER", "ra": ra, "dec": dec, "pmra": -24000.0, "pmdec": 9000.0, "w1mag": 16.9,
               "w2mag": 14.1, "ref_mjd": 57170.0})
    (d / "catwise.json").write_text(json.dumps({"schema": 1, "catalog": "SYNTHETIC (stands in for CatWISE2020)",
                                                "sources": cw}, indent=1), encoding="utf-8")


def make_nocover():
    d = OUT / "fake-nocover"
    d.mkdir(parents=True, exist_ok=True)
    (d / "manifest.json").write_text(json.dumps({
        "schema": 1, "key": "fake-nocover", "name": "Fake: no coverage",
        "query": {"ra": 10.0, "dec": 89.9, "fov_deg": 0.1}, "collections": ["spherex_dr1"],
        "epochs": [], "bands": [], "message": "No SPHEREx images cover this position (synthetic fixture).",
    }, indent=1), encoding="utf-8")


if __name__ == "__main__":
    make_mover()
    make_nocover()
    (OUT / "index.json").write_text(json.dumps({"schema": 1, "targets": [
        {"key": "fake-mover", "name": "Fake mover (synthetic)"},
        {"key": "fake-nocover", "name": "Fake: no coverage"},
    ]}, indent=1), encoding="utf-8")
    print("wrote", OUT)
