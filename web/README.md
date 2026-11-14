# web/ — placeholder test page

A plain test page for checking the data pipeline and the server against data contract v1 (PLAN.md section 5).
This is **not the final design**. No build step, no dependencies.

## Run

```
python -m http.server 8765 --bind 127.0.0.1 -d web
```

| URL | Data source |
|---|---|
| `http://127.0.0.1:8765/?target=wise-0855` | static files under `web/data/` (default) |
| `http://127.0.0.1:8765/?data=dev-fixtures/data&target=fake-mover` | synthetic fixtures |
| `http://127.0.0.1:8765/?api=http://127.0.0.1:8000&target=wise-0855` | API (`/api/manifest`, `/api/band`, `/api/frame`, …) |
| `…&ra=133.79&dec=-7.24&fov=0.1` | free coordinates (API only) |

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
