# Deploying the live server

The project has two parts:

- **The site** (`web/`): static files, published by `.github/workflows/pages.yml` to GitHub Pages. With no server
  it still shows the five gallery targets from `web/data/`.
- **The API server** (`server/`, `spx/`): a Python service in one Docker image (`Dockerfile`). It lists SPHEREx
  images at IRSA (SIA2), reads only the needed pixel rows from the public S3 bucket, and builds frames, spectra,
  candidates and MPC drafts on request. `.github/workflows/docker.yml` builds the image and checks it on every
  push (health, gallery catalogue, a bad band id).

The page finds the server through `web/config.js` (`window.SPX_API`). The Pages workflow writes that file from the
repository variable **`SPX_API_URL`**. After the server is up, set `SPX_API_URL` to its `https://…` address
(Settings → Secrets and variables → Actions → Variables) and run the Pages workflow again.

## Google Cloud Run (recommended)

Paste this into **Cloud Shell** (console.cloud.google.com, the `>_` button). It builds the image from the
repository's `Dockerfile` and starts one always-on instance near the data (S3 is in us-east-1, N. Virginia):

```sh
git clone https://github.com/Ayz345/Sci-Tech-SPHEREx-Blink && cd Sci-Tech-SPHEREx-Blink && gcloud run deploy spx-blink --source . --region us-east4 --memory 4Gi --cpu 1 --min-instances 1 --max-instances 1 --no-cpu-throttling --allow-unauthenticated --set-env-vars SPX_CACHE=/tmp/cache,SPX_CACHE_MAX_GB=1.5,SPX_MAX_FOV_DEG=0.11667
```

**Not tried yet**, and the flag names were not checked against the gcloud reference while this was written. To
check them first, run this; every flag should show up:

```sh
gcloud run deploy --help | grep -E -- "--(source|region|memory|cpu|min-instances|max-instances|no-cpu-throttling|allow-unauthenticated|set-env-vars)\b"
```

A wrong flag stops `gcloud` with "unrecognized arguments" before anything is deployed.

Why these settings:

| Setting | Why |
|---|---|
| one instance (`--min-instances 1 --max-instances 1`) | the band queue and the open fields live in memory; a second instance would build the same field again |
| CPU always on (`--no-cpu-throttling`) | bands are built by background threads after the request that queued them has returned |
| `--memory 4Gi` | Cloud Run's disk is memory. Process peak measured 689 MB with two 15′ fields open, plus the cache limit (1.5 GB) and headroom |
| `SPX_CACHE=/tmp/cache`, `SPX_CACHE_MAX_GB=1.5` | the cache lives in that memory; above 1.5 GB the oldest cutouts and fields are removed |
| `SPX_MAX_FOV_DEG=0.11667` | largest field 7′. `0.25` allows 15′: 30–70 s per band and 0.7–1.1 GB read per field (`handoff/results/fov_timing_v2.json`) |
| request timeout | `/api/band` waits up to 90 s for a band; Cloud Run's default request timeout is longer |

Check it at `https://<address>/api/health`:

- `ok` is true and `commit` is the deployed commit.
- `astrometry_validation.status` is `"complete"` (the MPC drafts carry the validation text).
- `max_fov_deg` is 0.11667.
- `cache.max_bytes` is 1500000000.

Then open `https://<address>/api/catwise?key=wise-0855`; it should return 200.

## Microsoft Azure (alternative, not tried)

Container Apps can run the same image from source. The command below is untested and its flags are not checked:

```sh
az containerapp up --name spx-blink --source . --location eastus --ingress external --target-port 8000 --env-vars SPX_CACHE=/tmp/cache SPX_CACHE_MAX_GB=1.5 SPX_MAX_FOV_DEG=0.11667
```

Then keep it at exactly one replica (min 1, max 1) with at least 4 GiB of memory, for the same reasons as above.

## Any other Docker host

```sh
docker build --build-arg SPX_COMMIT=$(git rev-parse --short HEAD) -t spx-blink .
docker run -p 8000:8000 -v spx-data:/data spx-blink
```

The image listens on `$PORT` (default 8000), runs as a non-root user, and keeps its cache in the volume `/data`.

## Settings (environment)

| Variable | Default (image) | Meaning |
|---|---|---|
| `PORT` | 8000 | port to listen on (set by Cloud Run and most hosts) |
| `SPX_CACHE` | `/data/cache` | cache folder: image lists, cutouts, built fields |
| `SPX_CACHE_MAX_GB` | 1.5 | size limit of cutouts and built fields; 0 = no limit |
| `SPX_MAX_FOV_DEG` | 0.11667 | largest field in degrees (0.25 = 15′) |
| `SPX_LIVE_LAYERS` | `IMAGE,FLAGS` | layers read for live fields |
| `SPX_COLLECTIONS` | `spherex_dr1,spherex_qr3` | SPHEREx collections searched (a new release is one more name) |
| `SPX_RATE_LIMIT` | 1 | 0 turns the request limits off |
| `SPX_NEW_FIELDS_PER_IP` | 10 | new fields per client address per 10 minutes |
| `SPX_MAX_ACTIVE_FIELDS` | 4 | fields building bands at once, server-wide |
| `SPX_MAX_BAND_WAITERS` | 64 | requests waiting for a band at once |
| `SPX_MAX_CUTOUT_SETS` | 2 | fields whose cutouts stay in memory |
| `SPX_MAX_WINDOWS_PER_FIELD` | 12 | user wavelength windows kept per field |
| `SPX_BACKGROUND_S3_WORKERS` | 8 | S3 connections for bands nobody is waiting for |
| `SPX_SIA_RETRY_BUDGET_S` | 25 | seconds of IRSA SIA2 retries before a 503 |
| `SPX_BAND_WAIT_S` | 90 | seconds a request waits for its band |
| `SPX_GALLERY` | `web/data` | the gallery folder (notes, CatWISE files) |
| `FORWARDED_ALLOW_IPS` | `*` | proxies trusted for the client address (request limits) |

Request limits per client address and minute: manifest 20, band 60, spectrum 20, candidates 30, track 6. Over a
limit the server answers 429 with `Retry-After`.

## Scripts

| Script | What it does |
|---|---|
| `scripts/precompute.py [--final]` | builds the gallery (`web/data/`); `--final` only from committed code |
| `scripts/make_wise_images.py` | AllWISE W1/W2 images on each gallery grid (IRSA's open S3 copy) |
| `scripts/make_catwise.py [--fetch]` | CatWISE2020 sources for the gallery (IRSA cone search) |
| `scripts/make_candidates.py` | automatic candidates for the gallery, from its frames |
| `scripts/acceptance.py [--own-server] [--static-only]` | checks the gallery and a server, item by item |
| `scripts/fov_timing.py [--write]` | cold timing of 15′ fields on a server of its own |
| `scripts/warm_demo.py --api <address>` | fills a server's cache with the demo fields before a presentation |
| `scripts/make_submit_xsd.py` | builds the ADES submit schema used to check MPC drafts (the image runs it) |
