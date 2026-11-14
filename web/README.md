# web/ — placeholder test page

A plain test page for checking the data pipeline and the server against data contract v1 (PLAN.md section 5).
This is **not the final design**. No build step, no dependencies.

## Run

```
python -m http.server 8765 --bind 127.0.0.1 -d web
```

On start the page checks the live server named in `config.js` (`window.SPX_API`, one `/api/health` request with a
1.5 s timeout; set it to the deployed server, or `""` for gallery only). Users never add parameters:

- Gallery targets (`?target=`) always open from the static files under `web/data/`, at once, without waiting for
  the check, and keep working when the server is down.
- Free coordinates, name lookup, field changes and the spectrum at a clicked point go to the live server if the
  check found one; otherwise the page says "The live server is not reachable right now, so only the five gallery
  targets are available."
- A target that is not in the static gallery is asked from the live server.
- The header ("source: …") says which source is in use.

Developer parameters (they override the check):

| URL | Data source |
|---|---|
| `http://127.0.0.1:8765/?target=wise-0855` | gallery from `web/data/`, live server from `config.js` |
| `http://127.0.0.1:8765/?data=dev-fixtures/data&target=fake-mover` | synthetic fixtures |
| `http://127.0.0.1:8765/?api=http://127.0.0.1:8000&target=wise-0855` | everything, gallery included, from that server |
| `http://127.0.0.1:8765/?api=none` | no server check (gallery only) |
| `…&ra=133.79&dec=-7.24&fov=0.1` | free coordinates (needs a live server) |

State in the URL: `band`, `mode` (blink · diff · sum), `frame`, `zoom`, `layer` (image · weight · flagfrac).

Synthetic fixtures (not committed): `python web/dev-fixtures/make_fixtures.py`. They cover a mover, Gaia stars,
a band without a `scale` (percentile fallback), an epoch with 0 exposures, a `pending` band without files and a
position without coverage.

## What the page checks

- manifest: `grid` (TAN, `crpix` 1-based, `cdelt`, `shape`), `epochs`, `bands[]` (buttons come from this list),
  `default_band`, `notes`, `target_track`, `query`, `message`, metadata fields
- `{band}.json`: `scale` (asinh) shared by all epochs; percentile fallback when missing; `n_used`
- `.f32`: little-endian, first row = south (drawn at the bottom); NaN = no data, +Infinity = saturated
  (brightest colour, "saturated" under the cursor; a non-finite result in diff/sum = no data); `_weight`, `_flagfrac`
- `gaia.json`: propagated to each epoch's `mjd_mid` with `pmra`/`pmdec`
- `spectrum`: `{meta, points}` (a plain array also works); flagged points are faint in the spectrum and left out of
  the light curve; the unit and aperture notes come from `meta`; API mode asks for the clicked point.
  Optional fields: `saturated` → lower-limit marker (open circle + arrow); `wing_flux` → separate
  "relative brightness from PSF wings" plot (MJy/sr, never on the mJy axis); `meta.photometry == "psf_deblended"` →
  note naming `meta.deblended_neighbors`; `|flux| < 0.15 × neighbor_flux` → drawn faint ("consistent with zero"),
  kept in the light curve. The light curve shows the selected band's wavelength range only.
- Key `t` (or the ⇄ button) switches back to the previous band, keeping the frame (e.g. WISE 0855: W ↔ D2).

## Other views

- `mode=rgb` (key `c`): false colour from three bands, `rgb=R,G,B` in the URL (default D6, D4, D2: long → red,
  short → blue). Each channel is stretched with its own band file's shared scale; per channel NaN = 0 (a pixel
  with no data in all three is "no data"), +Infinity = full brightness.
- `embed=1`: player and band/frame/view controls only, with the acknowledgement as one small line and an
  "open full view" link. The "copy embed code" button in the header copies an iframe snippet for the current view.
- Field buttons 3′ / 7′ / 15′: need the server; options above `max_fov_deg` (manifest or `/api/health`) are
  disabled with the reason in the tooltip. A gallery target becomes a free field via `/api/resolve`, so the grid
  still follows the moving object. 7′ is the default; 15′ is enabled when the server allows it (`max_fov_deg`
  0.25) and first shows an in-page confirmation with its cost (about 120 MB and 30–70 s per band, about 730 MB
  for all six; measured 14 Nov 2026) and "Open 15′" / "Keep 7′". A 15′ field opens at 2× zoom (300 px grid) and
  the status line says what the wait is for until the first band arrives. Not offered in the embedded view.
- Links: WiseView reads its state from the URL hash on load (`#ra=&dec=&size=&band=…`, checked in a browser);
  the link uses the object's latest track position and WiseView's own defaults. The IRSA SPHEREx Data Explorer
  has no documented URL that opens it at a position, so the link opens its start page.
- Saturation union (default on; `?satunion=0` turns it off): a pixel that is +Infinity in any shown epoch of a
  band is drawn saturated in another epoch of that band only where that epoch is bright there too (value ≥ the
  band's `scale.vmax`); blink and false colour show it saturated, the difference shows no data, the sum shows it
  saturated. A saturated core stays steady; a one-epoch artefact is not printed onto empty sky. The cursor readout
  still shows the epoch's own value, with "(saturated in another epoch)".
- Low-coverage frames (`{band}.json` `epochs[].quality == "low"`, fewer than 5 exposures): skipped by the player,
  the slider and the arrow keys, and left out of sum, difference and the saturation union; the markers under the
  slider show them faint. "show low-coverage frames" (`?low=1`) shows them with a "low coverage: N exposures"
  label. When a band has fewer than 2 good frames the option switches on by itself, with a message.
- Epochs with no data in the band (0 exposures): always skipped by playback and the arrow keys, whatever the
  low option. The slider can still reach them (dashed marker, "no data in this band") and shows a "no data" frame,
  so the epoch is seen to exist.
- PSF-wing plot: labelled "Indicative only, not a formal detection of variability." For the selected band it shows
  `meta.wing_bands[band]` as a neutral comparison ("×2.42 between epochs; bright control stars reach ×1.47 —
  exceeds / within the bright-star scatter"; `significant` only picks the phrase) and a one-line summary over all
  bands. `meta.wing` (method text) is shown verbatim inside a "Method details" section that is closed by default.
  If the selected band has no `wing_bands` entry, the plot is replaced by "No PSF-wing series in D5. Available
  in: D1, D2, …" with links that switch band; the page never switches band by itself.
- API mode survives a server restart: "server not reachable, retrying…" and it carries on when the server is back.
- Spectrum: `overflow: true` = recovered from overflow (valid, reduced accuracy): filled diamond, kept in the
  light curve; `saturated: true` stays a lower limit and is left out.

## Your own wavelength window (F1)

Drag across the spectrum (or type two wavelengths in µm) and press "Blink this window". The band id is
`w<lo>_<hi>` in nm (e.g. `w4300_5000`, 0.75–5.00 µm); `/api/band` and `/api/frame` build it on request on the
live server, also for a gallery target opened from static files. The window gets its own removable band button
(×), the address carries `band=w4300_5000` and reopening it opens the same window. The band file's `label`,
`wave_um`, `n_exposures` and `note` (the selection rule: an exposure is used when the wavelength at the field
centre falls in the window) are shown. Without the live server the page says so. Developer: `?winmock=1` serves
any window as the target's `W` band, to try the interface before the server builds windows.

## Credits in the footer

While the WISE image or the CatWISE layer is in use, the footer shows the AllWISE and CatWISE2020 credit lines;
while the Gaia overlay is on, the Gaia DR3 acknowledgement. Texts verbatim from the IRSA and ESA pages.

## WISE image in the blink

When `{key}/wise.json` and `wise_W1.f32` / `wise_W2.f32` exist (resampled onto this field's grid, row 0 = south),
the Frame box shows "Add the WISE image to the blink" with a W1/W2 choice (`?wise=W2` in the address; `frame=wise`
opens on it, also in the embedded view). The WISE frame goes first in the sequence (oldest), is drawn with its
own `bands[].scale` and carries a "WISE W2 · date · not SPHEREx" banner; the slider's leftmost position and a
"WISE" marker stand for it. It is in the blink only: difference, sum, false colour and candidates never use it.
Overlays on it use the WISE date (Gaia and CatWISE moved to `epoch_mjd`; without a date Gaia is not drawn). The
measure tool accepts marks on it with that date; when `epoch_mjd` is null it refuses and says why. Field names
(`epoch_mjd, date, survey, note, bands[{id, file, scale}]`) are read in `loadWise()`.

## CatWISE2020 layer

When the field has it (`{key}/catwise.json` for gallery targets, `/api/catwise?key=` for live fields), the
Overlays box shows "CatWISE2020": squares (Gaia uses circles). Sources with `pmra`/`pmdec` are moved to each
epoch from `ref_mjd`; when the catalogue has no proper motions the note says "positions at the catalogue epoch
(2015.4), not moved". Rows with `pm_reliable: false` (CatWISE fits of objects faster than about 1″/yr) are not moved: they are drawn as dashed squares at the catalogue position and the note quotes `meta.fast_note`. The cursor readout names the source under it with W1/W2. Live server: 202 = "being
prepared" (asked again every 5 s), 503 = "WISE catalogue is not available right now"; an empty `sources` list shows `meta.empty_note` (e.g. R Dra: the catalogue has nothing around this very bright star) with the toggle off; no file: the control is not shown. The layer's info line quotes `meta.pm_convention` and `meta.fast_note`; the readout adds `pm_check` when present. Rows with `same_object_as` (another row of the same object) are not drawn; the main row's readout says "N catalogue entries for this object" and names them, and a row with `target` is marked "(the target)". By default only the 150 brightest rows in W2 are drawn, plus rows with a `pm_check` and rows within 30″ of the target; "show all" draws every row, and the count is shown (e.g. "152 of 1389").
Field names (`source_name, ra, dec, pmra, pmdec, w1mag, w2mag, ref_mjd`) are mapped in `normCatwise()`.

## Automatic candidates (F2)

When the field has candidates (`{key}/candidates.json` for gallery targets, `/api/candidates?key=` for fields
from the live server; PLAN.md section 17 F2 layout), the Overlays box shows "Automatic candidates" with the fixed
note "Automatic candidates are places to look at, not detection claims." Each candidate is a dashed ring at its
position in the current epoch (faint at the nearest epoch when it has none there) with a short label (`mover`,
`variable`, or `known: Gaia …` when `known` is true; a nearby Gaia star alone is only in the tooltip). The list shows id, label, bands, S/N and rate; clicking
one loads its positions as marks in the measure tool (and switches to its first band), from where refine and
report work as usual. The first two `validation` lines (the user-facing limits) are shown under the toggle; a closed "How candidates
are found" section shows `method`, `params`, `bands_searched`, the `rejected` counts and the other
`validation` lines as given. A candidate is labelled "known: Gaia DR3 <id>" only when `known` is true. When the live server answers 202 (candidates not computed yet) the toggle is shown faint with "Automatic candidates are being computed…" and the `bands_pending` list; 503 says they are not available right now with the server's message; and the page asks again every 5 s; 404 or no static file: the control is not shown.

## Flag names on spectrum points (F3)

Pointing at a spectrum point shows its wavelength, flux ± error, MJD, band, saturated/overflow/flagged state and,
when the spectrum carries them, `flags` (names of the FLAGS bits set in its aperture, "none" for `[]`). A closed
"Flags" section lists `meta.flag_legend` as given by the data (names seen in this spectrum first, with counts;
the others faint). Without `flag_legend` the section is hidden. The fixtures' legend texts are placeholders.

Spectrum points with `masked_fraction` show "masked N% of the measurement region" in the hover text, and
`meta.err_note` (the error-bar caveat from the empty-sky test) is shown with the aperture notes.

## Sky context panel (F5, Aladin Lite)

"More surveys (Aladin Lite)" is closed by default; Aladin Lite is loaded only when it is opened (more surveys; the static WISE image and CatWISE layers above are the primary WISE view), and never in
the embedded view. It shows the player's centre and field (orange box = this grid, ring = the target at the
current epoch) in a chosen survey: AllWISE color, AllWISE W2, unWISE color, NEOWISER color, 2MASS color, DSS2
color, Pan-STARRS DR1 color. If the script or WebGL2 is missing, one sentence says so and the page is unaffected.

- Third-party code: Aladin Lite 3.8.2 by CDS (Université de Strasbourg / CNRS), loaded as an ES module from
  `https://cdn.jsdelivr.net/npm/aladin-lite@3.8.2/dist/aladin.js`. Licence: the `LICENSE` file in the npm package
  is the GNU LGPL v3 (the package also ships `COPYING` and `COPYING.LESSER`; its `package.json` for 3.8.2 says
  "GPL-3", the later 3.9.2-beta says "LGPL-3.0-or-later"). Not bundled or modified.
- Survey ids are from Aladin's built-in list and the CDS MocServer. They are passed to Aladin as HiPS URLs, not
  ids, because the id resolver Aladin 3.8.2 calls (`alasky.unistra.fr`) does not resolve (checked 14 Nov 2026);
  the URLs are each survey's `hips_service_url` mirrors from its MocServer record, tried in order (IRSA first),
  and the first whose `properties` file answers is used. unWISE, NEOWISER and Pan-STARRS are only on CDS
  servers, which did not answer on 14 Nov 2026; the panel then says so.
- Aladin Lite itself also sends a start-up log request to `alasky.unistra.fr`; it fails without effect.

## Measure / mark (key `m`)

Turn measuring on, click the moving object in one frame, step to another frame and click it again (any number
of marks; drag a mark to move it, × deletes it). The panel fits a straight line of position against time over
all marks and shows the shift (″), the direction (compass word and position angle east of north), the time span
and the rate (″/yr, or ″/h when the marks are less than 2 days apart), with east and north components. The
uncertainty assumes each click is good to about one grid pixel (`grid.cdelt`, 3″): two marks give ±√2·pixel / Δt.
This runs in the browser only and works on the static gallery. Checked: Barnard D1, epochs 1 and 3 → 9.6 ± 4.3″/yr
north (catalogue 10.4″/yr); WISE 0855 W → 9.6 ± 4.3″/yr west (catalogue ~8″/yr).

### Refine and report (live server)

- "Refine on single exposures" (needs the live server and 2+ marks): `POST /api/track` with
  `{key, band, marks: [{ra, dec, mjd, band}]}`. The result table shows each exposure (time, RA, Dec, ±″,
  "image S/N" = `snr_image` (the draft's logSNR is log10 of it), "acceptance S/N" = min(`snr_fit`,
  `snr_calibrated`) (accepted at ≥ 5), "—" when missing, band, and status: accepted, "accepted · not in the MPC draft: <reason>"
  or "rejected: <reason>"; rejected rows faint) and the rate is fitted again from the accepted measurements.
  `nights[].night` is the start of a 24-hour UTC window.
- "Download report…" opens a window with fixed text (draft only, nothing is sent; MPC checker, form and rules
  links; three observations per night; station 288; pixel size and the server's validation text; no magnitudes).
  CSV is always available; "MPC draft (ADES PSV / XML)" (`format=ades` / `ades_xml`) only when the server says
  `regime: within_visit`, a 24-hour window has 3+ accepted measurements and `/api/health` does not report
  `ades_schema: false`; otherwise the reason is shown. Submitter name (required for ADES, "I. M. Lastname",
  checked by the server) and an optional temporary designation (up to 7 characters); no e-mail, no comment field.
  Files come from `GET /api/report?id=&format=csv|ades&submitter=&trksub=`, named by the server
  (Content-Disposition); server errors are shown as they are. The page never contacts the MPC.
- Field names are proposed, not final: `normTrack()` in `app.js` is the one place that maps the server's answer.
- Developer mock without a server: `?mocktrack=within` or `?mocktrack=between` reads
  `dev-fixtures/track_within.json` / `track_between.json` (SYNTHETIC) and downloads a clearly named MOCK file.

## API mode (`?api=`)

- Only the band on screen is requested (an `/api/band` request moves that band to the front of the server queue);
  bands that are already `ready` are fetched one after another. While a band is `pending` the manifest is polled
  every 3 s and `progress` is shown on the band button; bands load as they become ready.
- Failed requests (404, 504 timeout) are not cached, so they are retried the next time the band is selected.
- When all bands are built and the spectrum listed `meta.bands_pending`, the spectrum is requested again.
- The FOV limit comes from `/api/health` (`max_fov_deg`); a larger FOV shows the server's 400 message.
- Name box: `/api/resolve`. One of the five targets opens `?target=`; any other name opens its coordinates and
  carries `pmra`, `pmdec`, `ref_epoch` and `name` in the URL. These are passed on to `/api/manifest`, which then
  centres the grid on the moving object and returns `target_track` (key suffix `_pm…_e…`). Only if a manifest
  still has no `target_track` does the page propagate the proper motion itself.
- Epochs with `n_used` 0 in the band file are not requested (no file exists); they show "no data".
- The request log at the bottom of the page shows the status, time and size of every request.
