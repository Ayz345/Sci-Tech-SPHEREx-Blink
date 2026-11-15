# Methods (image processing and photometry)

Code: `spx/coadd.py`, `spx/photometry.py`, `spx/detect.py`, `spx/astrometry.py` (freeze 6a55b82: spx_code_hash 744c2be5ca1c, frame_code_hash ecd39f140011; the gallery is built from it). Every number below was measured on SPHEREx DR1/QR3 data in this project; the commands that reproduce them are listed at the end.

We read SPHEREx Level 2 images (IRSA SIA2 lists, public S3 byte-range cutouts) and select exposures by the linear-variable-filter wavelength at the target pixel, computed from each file's WCS-WAVE table, not by the detector's nominal range. In every exposure we mask pixels with the FLAGS bits TRANSIENT, SUR_ERROR, NONFUNC, DICHROIC, MISSING_DATA, HOT, COLD, PHANMISS, NONLINEAR, PERSIST, OUTLIER, CROSSTALK, GHOST, GHOST_FPA, GHOST_EXT, STREAK, BLOOM, SNOWBALL, HALO and SATELLITE_HALO (bit 29, Explanatory Supplement v2.1 Table 16; absent from every exposure of the five gallery fields); SOURCE and FULLSAMPLE are informational and kept. A pixel flagged OVERFLOW without SUR_ERROR is kept as a recovered flux (its slope comes from the reads before the overflow threshold): on RW Aurigae it agrees with clean pixels at the same epoch and wavelength to a median ratio of 1.04 over 10 pairs (range 1.00–1.51). We subtract a sigma-clipped local background from unflagged, non-SOURCE pixels, weight each exposure by 1/σ² of that background, and resample it with a mask-aware bilinear interpolation onto one fixed, north-up 3″ grid shared by all epochs and bands (the native pixel is 6.15″). Each grid pixel is a weighted mean over the epoch's exposures after rejecting samples further than 3.5 × max(per-pixel scatter, exposure noise) + 0.5 × |median| from the median; the relative term keeps real differences between exposures taken at different wavelengths. Epochs are runs of exposures separated by more than 30 days, cut into pieces when longer than 60 days. A grid pixel is saturated (+∞, drawn at the brightest colour) when no usable sample is left and some exposure shows saturation there, or when more than 15% of its exposures are truly saturated (OVERFLOW together with SUR_ERROR, or NONLINEAR); NaN only means no coverage, and all epochs of a band share one asinh display scale. Against the first exploration prototype (re-implemented as the baseline in `tests/test_coadd_metrics.py`), on the same cutouts, the background noise fell 1.2–2.7× (median 1.9 over six target-epochs), isolated dark pixels fell from 214–337 to 2–8 per 10⁴ (WISE 0855), and the star FWHM stayed 9.2–10.2″ (prototype 9.0–9.5″ where measurable): no smoothing is added and no detail is invented; any upscaling on the page is cosmetic.

Per-exposure photometry uses a 2-pixel (12.3″) aperture with a 4–7 pixel background annulus, in mJy and without aperture correction; when a Gaia DR3 star lies within 25″ of the target we fit both with a pixel-integrated Gaussian PSF at fixed positions, which leaves 5–15% of the neighbour's flux on the target. Saturated exposures are kept as lower limits. Variability is the median over narrow wavelength bins of the ratio between the brightest and faintest epoch. A band counts as variable when at least 5 control stars from the same cutouts, measured the same way, have a ratio, and the target exceeds both 1.15 and the largest control ratio. Controls are Gaia DR3 stars 30″–4.5′ from the target, within ±1.5 mag of its G (G 10–15 when most of its points are saturated), with no neighbour within 20″ brighter than their G + 2.5.

## Results this supports

The numbers below are the published ones: `server/pipeline.variability_report` on `web/data/*/spectrum.json`, reproduced by `tests/test_coadd_variability.py`.

- **WISE 0855** is detected at 4.3–5.0 µm in all three epochs, with S/N 10.9, 17.2 and 19.9 calibrated on 3000 empty positions per epoch. Its motion measured from SPHEREx alone is −8.8 ± 1.2 and +1.5 ± 1.2 ″/yr; the literature values are −8.12 and +0.68. The measured positions lie 0.88″, 0.20″ and 1.53″ from the prediction (CatWISE2020 motion-solution position at MJD 57170 moved with the Luhman & Esplin 2016 proper motion). Until 14 Nov the prediction started from CatWISE2020's stationary-fit position labelled with the motion fit's epoch, 2.7″ east of the motion-fit position. That offset gave 2.1″, 2.7″ and 1.9″. The positions are free PSF centres; the prediction only picks the S/N peak within 6″. With the corrected start they moved by ≤ 0.03″ (re-gridding), and the fluxes, S/N, separations from the star and measured motion stayed the same. The remaining 0.2–1.5″ are not modelled: parallax is not applied to the prediction, and in the third epoch the object is 3.8″ from the G = 16.3 star, where the two-Gaussian fit leaves a residual (see Known limits). The challenge coordinate is its ~2010 position; it is now about 2.1′ west of it.
- **RW Aurigae** is variable in all six detector bands, measured on PSF-deblended points against 5 control stars (G 12.18 ± 1.5). The most robust bands rest on 6–8 wavelength bins:

  | Band | Target | Largest control | Bins |
  |---|---|---|---|
  | D1 | ×1.40 | ×1.02 | 8 |
  | D2 | ×1.37 | ×1.06 | 6 |
  | D5 | ×1.94 | ×1.06 | 7 |
  | D6 | ×1.64 | ×1.10 | 8 |
  | D3 | ×1.46 | ×1.05 | 1 |
  | D4 | ×1.17 | ×1.04 | 1 |

  D3 and D4 rest on a single wavelength bin, and D4 exceeds its controls only by a small margin (×1.13). Values from `variability_v2.json` (re-run with the frozen code); the gallery note rounds the same numbers.
- **Supplementary analysis (not published):** with aperture photometry and controls in G 10–15 (12 stars), the ratios are D1 ×1.54 against ×1.04, D5 ×1.70 against ×1.18, and D6 ×1.46 against ×1.29. The difference comes from two choices. First, the points: aperture versus PSF-deblended. Second, the control brightness range: the published run correctly treats RW Aur as unsaturated, since only 7 of 333 points are saturated once recovered-overflow pixels are used.

## Known limits

- **Difference images:** bright constant stars leave small black-and-white pairs. Their source is sub-pixel misalignment and PSF differences between epochs. Per-epoch alignment to Gaia is not done yet.
- **PSF-wing brightness** (shown for R Draconis only, whose core is saturated in 412 of 412 exposures that give a spectrum point (`spectrum.json`; an earlier build counted 413, a figure not re-derived here); indicative, not a formal detection):
  - On bright unsaturated control stars in the RW Aur and Eps Indi B fields (3 stars, 50 bins), the wing/aperture ratio changes between epochs by ×1.23 (median) and ×1.51 (90th percentile); re-measured on 14 Nov after masked pixels were left out of the aperture (before: 48 bins, ×1.20 and ×1.47).
  - Against that threshold, R Dra's D2 ×2.42 and D6 ×1.77 are above it, D3 ×1.49 is now just below it (it was just above ×1.47), and D1 ×1.31 is below it.
  - The control stars in the R Dra field itself cannot calibrate the method: they sit in R Dra's own scattered light. Their wing measurements are not noise-limited (S/N 53–55 per point for the G 13.8 and 14.2 stars), yet they change between epochs by ×2.0–2.8. In the RW Aur field, controls with similar or higher S/N change by only ×1.03–1.44 (×1.05–1.11 at S/N 150–250). One R Dra-field control even lies inside R Dra's saturated zone, so its wing annulus is pushed out to 26–30 px.
  - Their epoch curves follow R Dra's. The table below (measured before the 14 Nov masked-pixel change) gives D2 values per epoch, each wavelength bin normalised by its own mean over epochs before the median, so the different LVF wavelength sampling of each epoch does not enter:

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
- **WISE 0855 blending:** the object passes 11.4″ → 8.2″ → 3.8″ (measured) from a constant star (Gaia DR3 5756956644387831296, G = 16.3). That star is what shows at short wavelengths. Its light is removed by the two-source PSF fit, with the 5–15% residual noted above.
- **Per-exposure astrometry, time not verified:** an offset larger than ~5 s is not supported, but the time is not verified. Object-dependent systematics of ~0.15″ remain unexplained:
  - Eros: −0.15″ along its motion.
  - Euphrosyne: +0.14″ across its motion.
  - Possible sources, none established: PSF asymmetry within the trail, or the object's colour across the LVF band.
  - The structural reason the time cannot be separated from a sky offset: SPHEREx scans at ~90° solar elongation, so asteroids almost never move retrograde (1–3% of exposures for the candidates checked). An opposite-direction test is therefore not possible with these data.

## Spectrum points: masked pixels and the 'flagged' rule (v1.2, 14 Nov)

Code: `spx/photometry.py` (`aperture_flux`); validation `tests/test_coadd_flagged_rule.py` → `handoff/results/flagged_rule_v2.json`.

- **Masked pixels are left out.** These are pixels with any non-informational FLAGS bit (recovered overflow kept) or no data. The flux and its error are scaled by the PSF-weighted aperture fraction, a Gaussian with the exposure's PSF_FWHM. Truly saturated pixels are kept, so a saturated core gives a lower limit as before.
  - Scaling by area instead over-corrects a point source, because masked pixels sit mostly in the faint wings. On isolated stars, the area-scaled ratio was 1.08 at 5–15% masked and 1.18 above 15%.
- **flagged** = more than 15% of the aperture weight masked, or a masked pixel within 1 px of the centre, or a lower limit. Each point carries `masked_fraction`. Previously any masked pixel flagged the point.
- **Isolated stars.** G 11–16.5, three fields, 1117 points. Flux relative to unmasked points of the same star, band, epoch and wavelength (median ratio / robust scatter):
  - none masked: 0.996 / 8.8%;
  - < 5%: 0.986 / 9.1%;
  - 5–15%: 0.998 / 9.8%;
  - > 15%: 0.997 / 11.4%;
  - masked core: 1.004 / 16.3%.
- **OUTLIER on empty sky.** OUTLIER (Explanatory Supplement v2.1 §3.2.10) is set per exposure on isolated deviant pixels after the known-source pixels are masked. It therefore never appears on catalogued stars, and its effect is measured on empty sky: 27 746 points at 80 random empty positions.
  - Flux/err of unflagged points: median −0.10, robust scatter **1.40**. The listed aperture errors are too small by about that factor (fixed-position PSF photometry: 1.32). The page says so; the errors are not rescaled.
  - Where OUTLIER pixels were left out, the median is −0.35 to −0.53σ: the removed outliers are the positive ones.
  - Before this change, when those pixels were still summed, masked fractions above 15% and masked cores gave +0.6 to +0.8σ, with 6–7.5% of points beyond 5σ. After the change it is 0–2.4%.

## Per-exposure astrometry (MPC drafts)

Code: `spx/astrometry.py` (ada328d); validation `tests/test_astrometry.py` → `handoff/results/astrometry_validation_v2.json`.

For an MPC report each exposure is measured on its own, since blink frames combine days and clip fast movers.

- **Measurement:** a pixel-integrated Gaussian PSF (the exposure's PSF_FWHM) is fitted at a free position near the motion predicted from the user's marks. Gaia DR3 neighbours within 25″ are fitted as fixed PSFs. Pixels flagged only OUTLIER or TRANSIENT on the object's core are kept, because a moving source raises them.
- **Uncertainty:** the fit error, scaled by √χ²ᵣ, added in quadrature to a 0.23″ per-axis floor measured on Gaia stars.
- **Signal-to-noise, three numbers per exposure.** On one 3-day Eros pass (52 exposures, all bands) they are 15–66, 126–613 and 57–150, in the order below.
  - **Fit S/N (`snr_fit`)** is the PSF amplitude divided by its fit error scaled by √χ²ᵣ. It therefore drops as the Gaussian model misfits a bright source (χ²ᵣ 2–82 on Eros): it measures fit quality, not image noise.
  - **Calibrated S/N (`snr_calibrated`)** is the flux divided by the scatter of the same PSF flux at 24 empty positions of the cutout. It sees only the background noise, so it overstates a bright source whose own photon noise dominates (about 4× the background term on Eros).
  - **Image S/N (`snr_image`)** is the smaller of the calibrated S/N and the S/N propagated from the VARIANCE layer (background plus source photon noise). It is the S/N of the source in the image, as ADES defines logSNR, and is what logSNR should carry.
  - Acceptance still uses the smaller of the fit and calibrated S/N.
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

## Automatic candidate finder (v1.2 F2)

Code: `spx/detect.py`; validation `tests/test_coadd_detect.py` → `handoff/results/detect_validation_v2.json`. It runs on the published frames and downloads nothing. It is a classical detector with no machine learning, and one parameter set is used for every field (`DEFAULT_PARAMS`). A candidate is for a person to inspect; it is not a detection claim.

- **Method, per band.**
  - Each epoch frame minus the inverse-variance mean of all epochs, so the static sky cancels.
  - Noise from the weight maps plus a bright-star systematic. Both are measured on the frames. The matched-filter noise is ~2.5–4× the white-pixel estimate, because native 6.15″ pixels are resampled onto the 3″ grid, and this measured factor is applied.
  - A Gaussian matched filter.
  - Shift-and-stack over trial velocities, using all epochs at once (bands with ≥ 3 epochs).
  - Local fits of a moving source and of a fixed variable source, with the template subtraction built into both models.
  - Gaia DR3 proper motions label a mover as known.
- **Rejected, and counted:**
  - candidates within 60″ of a saturated core;
  - much weaker changes near a much stronger candidate (the halo of a bright star);
  - changes better fitted by a twice-wider profile, where the change exceeds 2/3 of the template amplitude;
  - movers whose motion sits at the edge of the search range;
  - weaker movers that share an epoch position with a stronger one;
  - motions common to bright stars (alignment residuals).
- **Disclosure.** The 2/3 limit on the extended test was added after RW Aur was first rejected as "extended": the Gaussian model under-fits a bright star's wings, so a wider profile wins for any change of a bright point source. The limit is the same in every field.
- **Targets, found without being given their positions.**
  - WISE 0855: mover in W and D5, S/N 10.8, motion −8.65 / +0.67 ″/yr (literature −8.12 / +0.68), grade A by three passes on a line (gallery `candidates.json`).
  - Epsilon Indi B: mover in D2, D3, D5 and D6, S/N 22.5, labelled known (grade A). Its motion matches Gaia DR3 6412596012146801152, at 3.89 / −3.01 ″/yr.
  - Barnard's Star: known mover in D6 only (grade A), S/N 40.8, at −1.16 / +11.0 ″/yr. In D1–D5 its core is saturated and masked, so it is not found there.
  - RW Aur: variable in D1, D5 and D6, S/N 12.7 (grade A, two independent bands).
  - R Dra: not found. Its saturated core and 60″ halo are masked in every band.
- **Other candidates per field** (not the target; unverified, likely artefacts): Barnard 7 (6 variables, 1 mover), RW Aur 5 variables, Epsilon Indi B 1 variable, WISE 0855 1 variable, R Dra 0.
- **Injection and recovery.** Gaussian sources moving linearly with the same flux in every epoch were added to the real frames; recovered means a mover within 1.5 px whose motion is within 1 px.
  - Sparse field (WISE 0855, W): at a per-epoch S/N of 12, the recovery rate is 0 / 0.06 / 0.83 / 1.00 for total motions of 1 / 2 / 3 / 5 px (3–15″) over the data span. At S/N 8 and 5 px it is 0.89; at S/N ≤ 6 and 5 px it is ≤ 0.22.
  - Crowded field (Barnard, D2): at S/N 12 the rates are 0 / 0.11 / 0.28 / 0.44. 11% of that frame lies in the halos of saturated stars, and bright-star residuals lower the sensitivity elsewhere.
- **Grades.** A = seen in two independent wavelength ranges (bands whose ranges overlap, such as W with D5/D6, count once), or a known Gaia mover, or "three passes on a line": a mover whose matched-filter S/N in each epoch's own frame, at its fitted track position, passes the detection threshold (6) in at least 3 epochs of one band. The linear motion is the fitted model, preferred over a fixed variable source. B = one band only. WISE 0855 is A by three passes (single-pass S/N 11.5 / 14.2 / 18.2 in W); the off-target mover in the Barnard field is B (−0.8 / 4.4 / −3.0). The difference-image S/N per epoch is not used here: when an object moves less than a PSF width between epochs the template contains it, so its middle epoch nearly vanishes from the difference (WISE 0855: 1.4 in W). Single-band changes were tested for the linear-variable-filter effect and it does not explain them: the epochs' mean wavelengths differ by 0.002–0.07 µm, which predicts ~0.2% against ~30% measured (correlation −0.05). Their cause is not established.
- **Limit.** A mover must move at least about one PSF FWHM (3.4 px ≈ 10″) over the data span; smaller motions are largely removed by the template subtraction. WISE 0855 moved 8.6″ (2.9 px) over the 0.99 yr of data, just under that limit. It is still found because it is bright in every visit (single-pass S/N 11.5–18 in W, 15–21 in D5), in line with the injections: at S/N 12 per pass, 3 px of motion is recovered in 15 of 18 cases.

Reproduce: `python tests/test_coadd_metrics.py`, `tests/test_coadd_wise0855.py`, `tests/test_coadd_saturation.py`, `tests/test_coadd_wings.py`, `tests/test_coadd_variability.py`, `tests/test_astrometry.py` (second object: `--object`), `tests/test_coadd_detect.py`, `tests/test_coadd_flags.py` (add `--write` to update the files here); figures with `python -m spx.display`.
