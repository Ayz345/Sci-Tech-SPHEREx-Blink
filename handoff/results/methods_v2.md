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
  - Against that threshold, R Dra's D2 ×2.42 and D6 ×1.77 are above it, D3 ×1.49 is just above it, and D1 ×1.31 is not significant.
  - The 3–4 fainter control stars in the R Dra field itself show wing ratios of ×2.2–3.2, larger than R Dra's. With fewer than 5 controls the formal test is therefore undetermined.
- **WISE 0855 blending:** the object passes 11.4″ → 8.2″ → 3.7″ (measured) from a constant star (Gaia DR3 5756956644387831296, G = 16.3). That star is what shows at short wavelengths. Its light is removed by the two-source PSF fit, with the 5–15% residual noted above.

Reproduce: `python tests/test_coadd_metrics.py`, `tests/test_coadd_wise0855.py`, `tests/test_coadd_saturation.py`, `tests/test_coadd_wings.py`, `tests/test_coadd_variability.py`; figures with `python -m spx.display`.
