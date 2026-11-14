"""The five challenge test targets.

Astrometry from CONTEXT.md 4b (values supplied by the image-quality session, 14 Nov 2026):
Gaia DR3 gaiadr3.gaia_source, ref_epoch 2016.0; ra/dec in degrees, proper motion in mas/yr
(pmra = mu_alpha * cos(dec)).

WISE 0855 is not in Gaia: CatWISE2020 J085507.96-071439.3 position at MJD 57170 with the Luhman & Esplin 2016
proper motion; the image-quality session verified it in SPHEREx DR1 (3 epochs).

Optional per-target fields used by the manifest: default_band, extra_bands, notes, challenge_radec.
wings: True publishes spx.photometry's PSF-wing flux series in the target's spectrum (only R Dra).
"""
from __future__ import annotations

import astropy.units as u
from astropy.coordinates import SkyCoord
from astropy.time import Time

# ref_epoch is a Julian year (TDB) like Gaia's; ref_mjd is the same instant as MJD.
TARGETS = {
    "barnard": {
        "name": "Barnard's Star", "ra": 269.44850252543836, "dec": 4.739420051112412,
        "pmra": -801.551, "pmdec": 10362.394, "parallax": 546.98, "ref_epoch": 2016.0,
        "source": "Gaia DR3 4472832130942575872", "kind": "high proper motion star", "fov_arcmin": 7.0,
    },
    "eps-indi-b": {
        "name": "Epsilon Indi B", "ra": 331.07645259656397, "dec": -56.79381206688221,
        "pmra": 3981.977, "pmdec": -2466.832, "parallax": 270.66, "ref_epoch": 2016.0,
        "source": "Gaia DR3 6412596012146801152 (Ba+Bb, G = 18.0)", "kind": "brown dwarf binary", "fov_arcmin": 7.0,
    },
    "wise-0855": {
        "name": "WISE 0855", "ra": 133.7831722, "dec": -7.2442516,
        "pmra": -8118.0, "pmdec": 680.0, "parallax": None, "ref_epoch": 2015.4018,  # MJD 57170
        "source": "CatWISE2020 + Luhman & Esplin 2016; verified in SPHEREx DR1 (3 epochs)",
        "kind": "Y dwarf, fast proper motion", "fov_arcmin": 7.0,
        # the challenge page gives the ~2010 position; by 2026 the object is ~2.15' west of it
        "challenge_radec": (133.79512916666667, -7.245147222222222),  # 08h55m10.831s -07d14m42.53s
        "default_band": "W",
        "extra_bands": [{"id": "W", "label": "4.3–5.0 µm", "wave_um": [4.3, 5.0]}],
        "notes": [
            "At short wavelengths (1.0–2.5 µm) the source next to the marker is NOT WISE 0855: it is a "
            "fixed background star, Gaia DR3 5756956644387831296 (G = 16.3, RA 133.75818, Dec −7.24116). "
            "WISE 0855 moves towards it.",
            "WISE 0855 itself is seen only at long wavelengths (band W, 4.3–5.0 µm).",
        ],
    },
    "rw-aur": {
        "name": "RW Aurigae", "ra": 76.95608119552952, "dec": 30.401204589097233,
        "pmra": 2.641, "pmdec": -27.398, "parallax": 6.41, "ref_epoch": 2016.0,
        "source": "Gaia DR3 156430822114424576", "kind": "variable star", "fov_arcmin": 7.0,
    },
    "r-dra": {
        "name": "R Draconis", "ra": 248.1673783672889, "dec": 66.75500674547219,
        "pmra": -20.643, "pmdec": 6.149, "parallax": 1.25, "ref_epoch": 2016.0,
        "source": "Gaia DR3 1648328016354496384", "kind": "Mira variable", "fov_arcmin": 7.0,
        "wings": True,  # saturated in every aperture point: publish the PSF-wing flux series (decision 14 Nov)
    },
}
for _k, _t in TARGETS.items():
    _t["key"] = _k
    _t["ref_mjd"] = float(Time(_t["ref_epoch"], format="jyear", scale="tdb").mjd)
    _t["pm_total_arcsec_yr"] = round((_t["pmra"] ** 2 + _t["pmdec"] ** 2) ** 0.5 / 1000.0, 2)

# Page notes. Numbers come from measurements in this project: proper motions from the entries above, RW Aur and
# R Dra from handoff/results/methods_v2.md (co-add session, freeze 4341aae), Eps Indi A from Gaia DR3 (ESA TAP,
# 14 Nov 2026: 6412595290592307840, G = 4.32, 402" west of B at 2016.0) and the gallery frames (west-edge strip
# 1.7-6.5 x the noise in every band and epoch, east edge ~0).
TARGETS["barnard"]["notes"] = [
    f"Barnard's Star is saturated at its core (drawn at the brightest colour). It moves "
    f"{TARGETS['barnard']['pm_total_arcsec_yr']:.1f}″/yr, almost due north (Gaia DR3 proper motion).",
]
TARGETS["eps-indi-b"]["notes"] = [
    f"Epsilon Indi B moves {TARGETS['eps-indi-b']['pm_total_arcsec_yr']:.1f}″/yr (Gaia DR3 proper motion).",
    "The bright star Epsilon Indi A (Gaia DR3 6412595290592307840, G = 4.3) lies 402″ west of B, outside this "
    "field; its halo raises the background along the west edge.",
]
# RW Aur, R Dra and WISE 0855's measured numbers are written at build time from the data
# (server/pipeline.measured_notes): variability from the published spectrum, wing bands, two-PSF separations.
TARGETS["rw-aur"]["notes"] = [
    "Its brightest pixels overflow; their flux is recovered from the reads before the overflow (median ratio 1.04 "
    "to clean pixels over 10 pairs, range 1.00–1.51; co-add session, methods_v2.md).",
]
TARGETS["r-dra"]["notes"] = []


def target_coord(key: str) -> SkyCoord:
    """SkyCoord with proper motion and obstime, ready for spx.coadd.target_at / build_frames."""
    t = TARGETS[key]
    return SkyCoord(ra=t["ra"] * u.deg, dec=t["dec"] * u.deg, pm_ra_cosdec=t["pmra"] * u.mas / u.yr,
                    pm_dec=t["pmdec"] * u.mas / u.yr, obstime=Time(t["ref_epoch"], format="jyear", scale="tdb"),
                    frame="icrs")


def position_at(key: str, mjd: float) -> tuple[float, float]:
    """(ra, dec) in degrees at `mjd`, linear proper motion only (no parallax)."""
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        c = target_coord(key).apply_space_motion(new_obstime=Time(mjd, format="mjd"))
    return float(c.ra.deg), float(c.dec.deg)
