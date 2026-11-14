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
  still follows the moving object.
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
  calibrated S/N `snr_calibrated` or "—", band, and status: accepted, "accepted · not in the MPC draft: <reason>"
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
