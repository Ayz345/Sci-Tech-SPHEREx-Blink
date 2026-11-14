# Sci-Tech: SPHEREx Blink

A web tool for the NASA Space Apps Challenge 2026,
["Planet X and SPHEREx"](https://www.spaceappschallenge.org/2026/challenges/planet-x-and-spherex/).

Give it a sky position (RA, Dec) or an object name. It collects the SPHEREx images of that patch of sky taken at
different times, cleans and stacks them into one frame per visit, and plays the frames one after another like a
short film, so that objects that move or change brightness stand out. Click a point to get its infrared spectrum
from the same exposures.

The model is [WiseView](http://byw.tools/wiseview), which the challenge names as inspiration; what SPHEREx adds is
the spectrum. The tool does not claim any discovery. It makes looking easier.

![Five challenge targets, one frame per visit](handoff/results/five_targets_v2.png)

## Status (14 November 2026)

- Works end to end on a local machine: page, API server, processing code, tests.
- The five challenge targets are prebuilt under `web/data/` and open without a server.
- Not deployed yet. `Dockerfile` is written but has not been built or run.
- The page in `web/` is a plain functional test page. The final visual design is being made separately and is
  not in this repository yet.

## What it does

| Challenge item | In the tool |
|---|---|
| Enter RA/Dec, see the images as a short video | Blink player: play/pause, speed, frame slider with dates, arrow keys |
| Five test targets | Prebuilt gallery (Barnard's Star, Epsilon Indi B, WISE 0855, RW Aurigae, R Draconis) and the live path |
| Coordinates in the URL, embedding | `?ra=&dec=&fov=` or `?target=`; the URL carries the whole view; `?embed=1` and "copy embed code" |
| Easy update with new data releases | The list of collections is one setting (`SPX_COLLECTIONS`); `scripts/precompute.py` rebuilds the gallery |
| Co-add per detector | One frame per visit and detector band (D1 to D6), plus custom wavelength windows |
| Add or subtract images from different times | Blink, difference (one visit minus the mean of the others) and sum |
| Field of view | 3′ and 7′ on the live server |
| Other telescopes | Gaia DR3 stars moved to each visit's date with their proper motion; link to WiseView |
| Metadata, contamination flags | Exposure count and dates per frame, coverage map, map of the masked fraction |
| Spectrum | Click a point: flux against wavelength from the single exposures, and against time |
| All 102 bands | False colour from three bands, spectrum, light curve |

Also in the tool:

- **Measure.** Mark a moving object in two or more frames; the page fits its shift, direction and rate.
- **Moving-object report.** The server measures the marked object again on the single exposures and offers the
  positions as CSV, and for Solar System objects as a draft in the Minor Planet Center's ADES format. It is a
  draft only: nothing is sent anywhere, the file is only downloaded.

## Run it

Python 3.13 was used. From the repository root:

```
python -m pip install -r requirements.txt

# the page (gallery works with this alone)
python -m http.server 8765 --bind 127.0.0.1 -d web

# the API server (free coordinates, name lookup, spectrum at a clicked point, reports)
uvicorn server.app:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8765/?target=wise-0855`. The page looks for the server named in `web/config.js`; without
one it says so and keeps the gallery. Page details and URL parameters: [`web/README.md`](web/README.md).

The moving-object report needs the ADES submission schema once: `python scripts/make_submit_xsd.py`.

Rebuild the gallery from the archive: `python scripts/precompute.py` (all five targets).

### When a new SPHEREx data release arrives

1. Add the new collection name to the list: set `SPX_COLLECTIONS` (for example
   `SPX_COLLECTIONS=spherex_dr2,spherex_dr1`) or edit `COLLECTIONS` in `spx/access.py`. Nothing else in the code
   names a release.
2. Restart the server. A new collection is asked from IRSA on the next request. Image lists are cached per
   collection for 7 days and built frames are kept on disk, so delete `cache/sia/` and `cache/fields/` to rebuild
   positions that were already built.
3. Rebuild the gallery: `python scripts/precompute.py`. Each target's `provenance.json` records the collections,
   the exposure counts and the code version it was built from, and the page shows the date range of the data.

## How it works

```
browser (web/, static)                      server (server/, Python)
  manifest ───────────────────────────────►  IRSA SIA2 image list → visits, bands
  band, frame ────────────────────────────►  byte-range reads from the public S3 bucket → spx.access → spx.coadd → cache
  gaia, spectrum, resolve, track, report ─►  Gaia DR3 / spx.photometry / CDS Sesame / spx.astrometry, spx.mpc
  no server: the same file names under web/data/ (five targets)
```

1. **List.** One SIA2 query to IRSA returns every SPHEREx image that covers the position.
2. **Pixels.** Each Level 2 file is about 66 MB. The server reads only the header and the rows it needs with HTTP
   range requests from the public bucket `nasa-irsa-spherex` (image, flags, variance).
3. **Frames.** Exposures are grouped into visits, masked with the FLAGS layer, background-subtracted, resampled
   onto one grid that is fixed on the sky (3″ pixels, north up) and combined with a clipped inverse-variance mean
   per visit and band. The grid does not follow the target, so a moving object changes place between frames.
4. **Display.** All frames of a band share one brightness scale, so blinking does not flicker.

Methods and measured numbers: [`handoff/results/methods_v2.md`](handoff/results/methods_v2.md).

## Layout

| Path | Content |
|---|---|
| `spx/` | Processing: archive access, co-add, display scale, photometry, single-exposure astrometry, ADES writer, targets |
| `server/` | FastAPI server and the build pipeline |
| `web/` | Static page; `web/data/` is the prebuilt gallery |
| `scripts/` | Gallery build, acceptance checks, small tools |
| `tests/` | Offline tests, and network tests that run with `SPX_NETWORK=1` |
| `handoff/` | First exploration prototype and the result files and figures of the quality work |
| `probes/` | The first measurements of the archive's speed and file layout |
| `docs/acceptance/` | Acceptance report of the gallery build |

## Tests

```
python -m pip install -r requirements-test.txt
python -m pytest -q tests
python scripts/acceptance.py --static-only
```

The acceptance report for the released gallery is `docs/acceptance/acceptance_20261114T104157Z.json`: 105 items
passed and 3 failed. The three are cold-cache timings for a field near the ecliptic pole, measured from a home
connection far from the data (image list 5.0 s and 5.4 s against a limit of 5 s; first band 52.8 s against 30 s).

## Limits

- SPHEREx pixels are 6.15″. Zooming in the page is cosmetic and labelled so. There is no deconvolution and no
  AI sharpening; nothing is shown that is not in the data.
- **R Draconis** is saturated in every exposure. Its brightness change is shown from the wings of the star's
  image and is labelled "indicative only, not a formal detection of variability".
- **Barnard's Star** has a saturated core; its spectrum points are lower limits.
- **RW Aurigae** is measured as variable in all six detector bands against control stars in the same images;
  two of the bands (D3, D4) rest on a single wavelength bin. Most of its points use pixels recovered from
  overflow, which are less accurate than clean ones.
- **WISE 0855** is faint: it is seen in a custom 4.3–5.0 µm window, not in the short bands. The source seen there
  at short wavelengths is a fixed background star (Gaia DR3 5756956644387831296), and the page says so.
- Frames built from fewer than 5 exposures are marked low coverage and skipped by default.
- The difference view leaves alignment residuals around bright stars.
- Moving-object report: positions were checked against JPL Horizons on asteroids (433) Eros and
  (31) Euphrosyne (numbers in `handoff/results/astrometry_validation_v2.json`). The observation **time is not
  independently verified**. Station code 288 is SPHEREx; submitting archival measurements under it is between
  the user and the Minor Planet Center.

## Data, resources and tools

Every source used is listed here, as the Space Apps rules ask.

**NASA data**

- SPHEREx Level 2 spectral images, releases DR1 and QR3, from the NASA/IPAC Infrared Science Archive (IRSA):
  <https://irsa.ipac.caltech.edu/Missions/spherex.html>. Image lists come from IRSA's SIA2 service; pixels from
  the public bucket `nasa-irsa-spherex`. Flag and saturation definitions are from the SPHEREx Explanatory
  Supplement: <https://irsa.ipac.caltech.edu/data/SPHEREx/docs/SPHEREx_Expsupp.pdf>.

  > This publication makes use of data products from the Spectro-Photometer for the History of the Universe,
  > Epoch of Reionization and Ices Explorer (SPHEREx), which is a joint project of the Jet Propulsion Laboratory
  > and the California Institute of Technology, and is funded by the National Aeronautics and Space
  > Administration.

- IRSA SPHEREx tutorial notebooks: <https://caltech-ipac.github.io/irsa-tutorials/spherex/>.
- JPL Horizons, for checking the measured asteroid positions (tests only): <https://ssd.jpl.nasa.gov/horizons/>.
- CatWISE2020 position and AllWISE W2 magnitude of WISE 0855; proper motion from Luhman & Esplin (2016).

**Space agency partner data and other sources**

- Gaia DR3 (ESA), through the ESA Gaia archive, for the star overlay, the astrometry of the test targets and the
  deblending of neighbours.

  > This work has made use of data from the European Space Agency (ESA) mission Gaia
  > (https://www.cosmos.esa.int/gaia), processed by the Gaia Data Processing and Analysis Consortium (DPAC,
  > https://www.cosmos.esa.int/web/gaia/dpac/consortium). Funding for the DPAC has been provided by national
  > institutions, in particular the institutions participating in the Gaia Multilateral Agreement.

- Name lookup: the Sesame name resolver of the CDS, Strasbourg (<https://cds.unistra.fr/>).
- Minor Planet Center: observatory codes, reporting rules and the ADES format; IAU ADES master files and schema
  (<https://github.com/IAU-ADES/ADES-Master>), downloaded when needed and not stored in this repository.
- WiseView (<http://byw.tools/wiseview>), as the model for the player.

**Software**

| Package | Licence |
|---|---|
| NumPy, SciPy | BSD |
| Astropy, Uvicorn, lxml, HTTPX, PyVO (tests only) | BSD-3-Clause |
| Requests | Apache-2.0 |
| FastAPI, pytest | MIT |
| iau-ades (tests only) | see the package |
| Matplotlib, Pillow (optional, figures only) | Matplotlib licence, MIT-CMU |

The page uses no third-party JavaScript, fonts or images.

## Use of AI tools

The Space Apps rules ask teams to say where and how AI tools were used (the rules call them "Super Intelligence
(SI)" tools).

- **Code, tests and documentation** in this repository were written with Claude Code (Anthropic), working under
  the team's direction. The team set the goals, made the product decisions and approved each release. Every
  commit carries a `Co-Authored-By: Claude` line in its message.
- **Images.** No image in the tool or in this repository is AI-generated or AI-enhanced. Every sky image is
  computed from SPHEREx data by the code in `spx/`.
- **Data.** No data is AI-generated. Every number on the page is measured or computed from the archive data;
  the result files under `handoff/results/` are written by the tests and scripts in this repository.

## Licence

The code and documentation of this project are under the [MIT licence](LICENSE). The SPHEREx, Gaia and other
catalogue data belong to their sources and keep their own terms; the prebuilt frames under `web/data/` are
derived from public SPHEREx data.
