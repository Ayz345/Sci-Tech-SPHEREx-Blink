# Methods (image processing and photometry)

Code: `spx/coadd.py`, `spx/photometry.py` (freeze 535d93a). Every number below was measured on SPHEREx DR1/QR3 data in this project; the commands that reproduce them are listed at the end.

We read SPHEREx Level 2 images (IRSA SIA2 lists, public S3 byte-range cutouts) and select exposures by the linear-variable-filter wavelength at the target pixel, computed from each file's WCS-WAVE table, not by the detector's nominal range. In every exposure we mask pixels with the FLAGS bits TRANSIENT, SUR_ERROR, NONFUNC, DICHROIC, MISSING_DATA, HOT, COLD, PHANMISS, NONLINEAR, PERSIST, OUTLIER, CROSSTALK, GHOST, GHOST_FPA, GHOST_EXT, STREAK, BLOOM, SNOWBALL and HALO; SOURCE and FULLSAMPLE are informational and kept. A pixel flagged OVERFLOW without SUR_ERROR is kept as a recovered flux (its slope comes from the reads before the overflow threshold): on RW Aurigae it agrees with clean pixels at the same epoch and wavelength to a median ratio of 1.04 over 10 pairs (range 1.00–1.51). We subtract a sigma-clipped local background from unflagged, non-SOURCE pixels, weight each exposure by 1/σ² of that background, and resample it with a mask-aware bilinear interpolation onto one fixed, north-up 3″ grid shared by all epochs and bands (the native pixel is 6.15″). Each grid pixel is a weighted mean over the epoch's exposures after rejecting samples further than 3.5 × max(per-pixel scatter, exposure noise) + 0.5 × |median| from the median; the relative term keeps real differences between exposures taken at different wavelengths. Epochs are runs of exposures separated by more than 30 days, cut into pieces when longer than 60 days. A grid pixel is saturated (+∞, drawn at the brightest colour) when no usable sample is left and some exposure shows saturation there, or when more than 15% of its exposures are truly saturated (OVERFLOW together with SUR_ERROR, or NONLINEAR); NaN only means no coverage, and all epochs of a band share one asinh display scale. Against the prototype on the same cutouts, the background noise fell 1.2–2.7× (median 1.9 over six target-epochs), isolated dark pixels fell from 214–337 to 2–8 per 10⁴ (WISE 0855), and the star FWHM stayed 9.2–10.2″ (prototype 9.0–9.5″ where measurable): no smoothing is added and no detail is invented; any upscaling on the page is cosmetic.

Per-exposure photometry uses a 2-pixel (12.3″) aperture with a 4–7 pixel background annulus, in mJy and without aperture correction; when a Gaia DR3 star lies within 25″ of the target we fit both with a pixel-integrated Gaussian PSF at fixed positions, which leaves 5–15% of the neighbour's flux on the target. Saturated exposures are kept as lower limits. Variability is the median over narrow wavelength bins of the ratio between the brightest and faintest epoch. A band counts as variable when at least 5 control stars from the same cutouts, measured the same way, have a ratio, and the target exceeds both 1.15 and the largest control ratio. Controls are Gaia DR3 stars 30″–4.5′ from the target, within ±1.5 mag of its G (G 10–15 when most of its points are saturated), with no neighbour within 20″ brighter than their G + 2.5.

## Results this supports

The numbers below are the published ones: `server/pipeline.variability_report` on `web/data/*/spectrum.json`, reproduced by `tests/test_coadd_variability.py`.

- **WISE 0855** is detected at 4.3–5.0 µm in all three epochs, with S/N 10.5, 16.9 and 19.1 calibrated on 3000 empty positions per epoch. Its motion measured from SPHEREx alone is −8.8 ± 1.2 and +1.5 ± 1.2 ″/yr; the literature values are −8.12 and +0.68. The challenge coordinate is its ~2010 position; it is now about 2.1′ west of it.
- **RW Aurigae** is variable in all six detector bands, measured on PSF-deblended points against 5 control stars (G 12.18 ± 1.5). The most robust bands rest on 6–8 wavelength bins:

  | Band | Target | Largest control | Bins |
  |---|---|---|---|
  | D1 | ×1.40 | ×1.03 | 8 |
  | D2 | ×1.37 | ×1.06 | 6 |
  | D5 | ×1.94 | ×1.06 | 7 |
  | D6 | ×1.64 | ×1.11 | 8 |
  | D3 | ×1.46 | ×1.05 | 1 |
  | D4 | ×1.17 | ×1.06 | 1 |

  D3 and D4 rest on a single wavelength bin, and D4 exceeds its controls only by a small margin (×1.11).
- **Supplementary analysis (not published):** with aperture photometry and controls in G 10–15 (12 stars), the ratios are D1 ×1.54 against ×1.04, D5 ×1.70 against ×1.18, and D6 ×1.46 against ×1.29. The difference comes from two choices. First, the points: aperture versus PSF-deblended. Second, the control brightness range: the published run correctly treats RW Aur as unsaturated, since only 7 of 333 points are saturated once recovered-overflow pixels are used.

## Known limits

- **Difference images:** bright constant stars leave small black-and-white pairs. Their source is sub-pixel misalignment and PSF differences between epochs. Per-epoch alignment to Gaia is not done yet.
- **PSF-wing brightness** (shown for R Draconis only, whose core is saturated in 412 of 413 exposures; indicative, not a formal detection):
  - On bright unsaturated control stars in the RW Aur and Eps Indi B fields (3 stars, 48 bins), the wing/aperture ratio changes between epochs by ×1.20 (median) and ×1.47 (90th percentile).
  - Against that threshold, R Dra's D2 ×2.42 and D6 ×1.77 are above it, D3 ×1.49 is just above it, and D1 ×1.31 is below it.
  - The control stars in the R Dra field itself cannot calibrate the method: they sit in R Dra's own scattered light. Their wing measurements are not noise-limited (S/N 53–55 per point for the G 13.8 and 14.2 stars), yet they change between epochs by ×2.0–2.8. In the RW Aur field, controls with similar or higher S/N change by only ×1.03–1.44 (×1.05–1.11 at S/N 150–250). One R Dra-field control even lies inside R Dra's saturated zone, so its wing annulus is pushed out to 26–30 px.
  - Their epoch curves follow R Dra's. The table below gives D2 values per epoch, each wavelength bin normalised by its own mean over epochs before the median, so the different LVF wavelength sampling of each epoch does not enter:

    | Source | Epoch 0 | 1 | 2 | 3 | 5 |
    |---|---|---|---|---|---|
    | R Dra | 1.14 | 1.37 | 1.01 | 1.03 | 0.59 |
    | Field star G 14.2, 2.1′ away | 1.29 | 1.53 | 0.85 | 0.86 | 0.59 |
    | Field star G 13.8, 2.1′ away | 1.24 | 1.51 | 0.86 | 0.88 | 0.60 |
    | Field star G 12.5, 3.2′ away | 1.24 | 1.45 | 0.87 | 0.95 | 0.65 |

    D6 shows the same pattern (R Dra 1.38 / 1.34 / 0.96 / 0.73 in epochs 0, 3, 4, 5; field stars 1.24–1.33 / 1.03–1.59 / 0.39–0.81 / 0.34–0.56).
  - Field stars 2–3′ away brighten and fade with R Dra in the same epochs: its scattered light dominates there, so no independent control exists. This agreement is evidence that the common change is R Dra's own light, since an instrumental change in the PSF wings would also appear in the RW Aur field, and it does not. It is not an independent calibration, however. The amount of scattered light may also depend on where R Dra falls on the detector in each exposure, and we have not separated that effect.
  - The wing series is therefore shown as indicative only. The formal variability test is undetermined: fewer than 5 independent controls, and the field's controls are contaminated by the target.
  - Measured with `spectrum_points(..., wings=True)` → `binned_series(kind="wing")` → `variability` on the cached R Dra cutouts, with controls from `select_controls` on `web/data/r-dra/gaia.json`.
- **Control selection:** `select_controls` does not exclude stars embedded in the target's own halo or scattered light. This had no visible effect for RW Aur, Barnard or Eps Indi B, but it makes the R Dra controls unusable (above). A future improvement is to reject controls whose wing or background tracks the target.
- **WISE 0855 blending:** the object passes 11.4″ → 8.2″ → 3.7″ (measured) from a constant star (Gaia DR3 5756956644387831296, G = 16.3). That star is what shows at short wavelengths. Its light is removed by the two-source PSF fit, with the 5–15% residual noted above.
- **Per-exposure astrometry, time not verified:** an offset larger than ~5 s is not supported, but the time is not verified. Object-dependent systematics of ~0.15″ remain unexplained:
  - Eros: −0.15″ along its motion.
  - Euphrosyne: +0.14″ across its motion.
  - Possible sources, none established: PSF asymmetry within the trail, or the object's colour across the LVF band.
  - The structural reason the time cannot be separated from a sky offset: SPHEREx scans at ~90° solar elongation, so asteroids almost never move retrograde (1–3% of exposures for the candidates checked). An opposite-direction test is therefore not possible with these data.

## Per-exposure astrometry (MPC drafts)

Code: `spx/astrometry.py` (ada328d); validation `tests/test_astrometry.py` → `handoff/results/astrometry_validation_v2.json`.

For an MPC report each exposure is measured on its own, since blink frames combine days and clip fast movers.

- **Measurement:** a pixel-integrated Gaussian PSF (the exposure's PSF_FWHM) is fitted at a free position near the motion predicted from the user's marks. Gaia DR3 neighbours within 25″ are fitted as fixed PSFs. Pixels flagged only OUTLIER or TRANSIENT on the object's core are kept, because a moving source raises them.
- **Uncertainty:** the fit error, scaled by √χ²ᵣ, added in quadrature to a 0.23″ per-axis floor measured on Gaia stars.
- **Realistic single-exposure error per axis:** ~0.5″ at S/N 5–10, ~0.4″ at S/N 10–20, ~0.3″ at S/N 20–50, against a 6.15″ pixel.
- **Validation:** positions were compared with JPL Horizons, moved from the geocentre to the spacecraft position in the header (X_SC/Y_SC/Z_SC, which we verified to be geocentric ICRF axes; the parallax correction is 1–10″).

  | Object | Accepted | Median residual (RA·cosδ / Dec) | Robust scatter |
  |---|---|---|---|
  | (433) Eros | 382 of 450 | −0.12″ / −0.05″ | 0.17″ / 0.16″ |
  | (31) Euphrosyne | 275 of 350 | −0.01″ / +0.02″ | 0.24″ / 0.18″ |

  Gaia stars measured the same way show no bias: −0.01″ / −0.03″.
- **Rejected exposures** are listed with the reason. An exposure is rejected for any of the following:
  - S/N < 5, using the smaller of the fit S/N and an S/N calibrated on random positions of the same cutout;
  - unusable core pixels;
  - saturation;
  - FINAST ≠ 0;
  - a Gaia neighbour closer than 0.6 FWHM;
  - more than 6.15″ of motion within the exposure (validated up to 4.9″).
- **Time is not independently verified.** The along-track residual is −0.15″ for Eros and +0.01 ± 0.02″ for Euphrosyne (+1.3 ± 2.3 s). obsTime is the header mid-exposure time (DATE-AVG), uncorrected, and no time uncertainty is claimed.
- **MPC rules followed** (MPC "Getting started" and "Reporting observations" pages): only detections of sufficient S/N, at least three observations per object per night, mid-exposure times, no approximate measures, and consistency better than 1″.
- **WISE 0855** is too faint per exposure for MPC-grade positions; its epoch means agree with the co-add positions.

Reproduce: `python tests/test_coadd_metrics.py`, `tests/test_coadd_wise0855.py`, `tests/test_coadd_saturation.py`, `tests/test_coadd_wings.py`, `tests/test_coadd_variability.py`, `tests/test_astrometry.py` (second object: `--object`); figures with `python -m spx.display`.
