# Deploying the live server

The project has two parts:

- **The site** (`web/`): static files, published by `.github/workflows/pages.yml` to GitHub Pages. With no server
  it still shows the five gallery targets from `web/data/`.
- **The API server** (`server/`, `spx/`): a Python service in one Docker image (`Dockerfile`). It lists SPHEREx
  images from its own index of the public S3 bucket (IRSA SIA2 for anything newer; see "Our image index" below),
  reads only the needed pixel rows from that bucket, and builds frames, spectra,
  candidates and MPC drafts on request. `.github/workflows/docker.yml` builds the image and checks it on every
  push (health, gallery catalogue, a bad band id).

The page finds the server through `web/config.js` (`window.SPX_API`). The Pages workflow writes that file from the
repository variable **`SPX_API_URL`**. After the server is up, set `SPX_API_URL` to its `https://…` address
(Settings → Secrets and variables → Actions → Variables) and run the Pages workflow again.

## Google Cloud Run (recommended)

**Tried** on 15 Nov 2026: the live server runs this way (us-east4). Paste this into **Cloud Shell**
(console.cloud.google.com, the `>_` button). It builds the image from the repository's `Dockerfile` and starts one
always-on instance near the data (S3 is in us-east-1, N. Virginia):

```sh
git clone https://github.com/Ayz345/Sci-Tech-SPHEREx-Blink && cd Sci-Tech-SPHEREx-Blink && gcloud run deploy spx-blink --source . --region us-east4 --memory 4Gi --cpu 2 --concurrency 1000 --min-instances 1 --max-instances 1 --no-cpu-throttling --allow-unauthenticated --set-env-vars SPX_CACHE=/tmp/cache,SPX_CACHE_MAX_GB=1.5,SPX_MAX_FOV_DEG=0.11667,SPX_SIA_RETRY_BUDGET_S=60,SPX_SIA_MAX_ATTEMPTS=100,SPX_MAX_BAND_WAITERS=800,SPX_MAX_ACTIVE_FIELDS=8,SPX_NEW_FIELDS_PER_IP=40,SPX_PREBUILD_FIELDS=0,SPX_COMMIT=$(git rev-parse --short HEAD)
```

`SPX_COMMIT` puts the deployed commit into `/api/health` (a build from source has no git folder in the image;
without it health says `unknown`).

What a new Google Cloud project needs, in the order it came up the first time:

1. **A billing account linked to the project.** Without it: `FAILED_PRECONDITION … UREQ_PROJECT_BILLING_NOT_FOUND`.
   Link one under Billing (the free trial credit is enough; nothing is charged while it lasts).
2. **The Cloud Build API.** The first `gcloud run deploy --source` asks to enable it: answer `y`.
3. **Permission for the build account.** A new project fails with
   `PERMISSION_DENIED: Build failed because the default service account is missing required IAM permissions`.
   Give the Compute Engine default service account the builder role (Google: Cloud Run docs, "Build service
   account"), and the service-usage role the error message names; wait a few minutes; deploy again:

   ```sh
   PROJECT=$(gcloud config get-value project)
   NUMBER=$(gcloud projects describe $PROJECT --format='value(projectNumber)')
   gcloud projects add-iam-policy-binding $PROJECT --member="serviceAccount:$NUMBER-compute@developer.gserviceaccount.com" --role="roles/run.builder"
   gcloud projects add-iam-policy-binding $PROJECT --member="serviceAccount:$NUMBER-compute@developer.gserviceaccount.com" --role="roles/serviceusage.serviceUsageConsumer"
   ```
4. **Later deployments** (new code): `cd ~/Sci-Tech-SPHEREx-Blink && git pull`, then the same `gcloud run deploy …`
   command as above (without the `git clone … && cd … &&` part).

Why these settings:

| Setting | Why |
|---|---|
| one instance (`--min-instances 1 --max-instances 1`) | the band queue and the open fields live in memory; a second instance would build the same field again |
| CPU always on (`--no-cpu-throttling`) | bands are built by background threads after the request that queued them has returned |
| `--cpu 2` | two threads build bands: one for bands a page asked for, one for the background queue (with `SPX_PREBUILD_FIELDS=0`, only the bands the candidates tab asks for). On one vCPU they share a core, so the band someone waits for slows down while the other is built (until 15 Nov the service ran with `--cpu 1`). `/api/health` → `cpu` reads 4 CPUs and quota 4.0 on this service (15 Nov, 2 h after start, so not the start-up CPU boost, which ends 10 s after start). `cpu` is what the container's cgroup reports; the setting itself is what `gcloud run services describe spx-blink --region us-east4 --format="value(spec.template.spec.containers[0].resources.limits.cpu)"` shows |
| `SPX_MAX_ACTIVE_FIELDS=8`, `SPX_NEW_FIELDS_PER_IP=40` | a new field is refused (429) only when 8 fields have a band someone is waiting for. Bands in the background queue do not count: until 15 Nov they did, and four opened fields kept every new one out for minutes while nothing was being built. Visitors on one school or home network share an address, and moving around the sky opens a new field at every stop, so 40 new fields per address per 10 minutes, not 10 |
| `--concurrency 1000` | Cloud Run's maximum. When that many requests are open, Cloud Run itself answers 429 "The request was aborted after waiting too long for an available instance", without CORS (the page sees "Failed to fetch"). Opening a free field sends ~70 requests at once (bands, and frames waiting for their band, each up to 90 s) plus polling, so 250 was used up by 3–4 visitors on 15 Nov (Cloud Run log 15:20–15:25 UTC, no crash, no memory kill). Waiting requests hold no thread, so 1000 is cheap; `/api/health` → `in_flight` shows the count |
| `SPX_MAX_BAND_WAITERS=800` | the server's own limit on requests waiting for a band (frames count too). Above it the server answers 429 itself, with CORS and a "try again" sentence, before Cloud Run's limit is reached |
| `SPX_PREBUILD_FIELDS=0` | no bands built ahead: only bands a page asks for (the candidates tab asks for its field's). Set on 15 Nov while the server was under load. The code default is 2: bands built ahead for the two newest fields |
| `--memory 4Gi` | Cloud Run's disk is memory. Process peak measured 689 MB with two 15′ fields open, plus the cache limit (1.5 GB) and headroom |
| `SPX_CACHE=/tmp/cache`, `SPX_CACHE_MAX_GB=1.5` | the cache lives in that memory; above 1.5 GB the oldest cutouts and fields are removed |
| `SPX_MAX_FOV_DEG=0.11667` | largest field 7′. `0.25` allows 15′: 30–70 s per band and 0.7–1.1 GB read per field (`handoff/results/fov_timing_v2.json`) |
| request timeout | `/api/band` waits up to 90 s for a band; Cloud Run's default request timeout is longer |
| `SPX_SIA_RETRY_BUDGET_S=60`, `SPX_SIA_MAX_ATTEMPTS=100` | on 15 Nov IRSA answered 6 of 30 SIA2 queries (502s in 1–3 s, answers in 23–24 s). Simulated on those answers: both image lists of a field arrive 96% of the time with 60 s / 20 attempts, 70% with the defaults (25 s / 10); median 31 s, 90th percentile 54 s. When IRSA answers far less (a few percent later that day), 20 attempts ran out in ~40 s; 100 leaves the 60 s budget as the only limit |

Check it at `https://<address>/api/health`:

- `ok` is true and `commit` is the deployed commit.
- `astrometry_validation.status` is `"complete"` (the MPC drafts carry the validation text).
- `max_fov_deg` is 0.11667.
- `cache.max_bytes` is 1500000000.
- Under load, read `in_flight` (requests open now, highest in 5 minutes, by path), `event_loop_lag` (how late a 1 s
  timer fires: every answer waits that long), `memory` (process and container MB, `cache_fs_used_mb` = the
  in-memory `/tmp`), `cpu`, `started_utc` / `uptime_s` (a restart shows as a new start time) and `gaia`
  (`paused_s` > 0 while the archive is not asked).

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

## Our image index (`server/index/`)

**What it is.** `server/index/spx_index.npz` (22.5 MB) lists every Level 2 image of SPHEREx DR1 and QR3 in the
public bucket: S3 key, detector, the TAN part of the file's WCS (`CRVAL`, `PC`, `CRPIX`, `CDELT`) and the exposure
times. It holds 1,628,230 images taken from 24 April 2025 to 5 October 2026, read from the bucket on 15 November
2026 while IRSA's SIA2 and TAP services answered 502. With it a free field's image list comes from the server
itself. How it was made and checked: `handoff/results/methods_v2.md` ("Image lists: our own index of the bucket")
and `handoff/results/s3index_check.json`.

**In the image.** The Dockerfile copies `server/`, so the file is at `/app/server/index/spx_index.npz`; the
Docker workflow checks that it is loaded and that a free field opens from it.

**How the server uses it**, for each collection:

1. A list still in the cache is kept: the gallery's seed (`web/data/_sia/`) or an earlier IRSA answer.
2. Otherwise the index's list is written into the same cache file, and IRSA is not asked for it.
3. In the background, IRSA SIA2 is asked once, for at most 20 s, for exposures after the index's last one
   (5 October 2026). If it answers, they are added to the cached list; if not, nothing waits for it.
4. A collection the index does not cover is asked of IRSA as before.

The manifest field `image_list` says where the list came from (the tool's About tab shows it).

**Setting.** `SPX_INDEX`: a `.npz` file, or a folder whose `.npz` files are all loaded (DR1 and QR3 may be
separate files). Default `server/index`. `off` = no index: image lists come from IRSA SIA2 only, as before.

**Health.** `/api/health` → `image_index`: `files`, `rows` (1628230), `built_utc`, `mjd_max`, `last_exposure`
(`2026-10-05`); `{"loading": true}` while it loads, `null` when there is no index.

**Cost.** The index is loaded in a background thread when the server starts (the log says "image index … loaded in X s"; image lists asked for meanwhile wait for it, health shows `{"loading": true}`): 0.33 s locally, and the process grows by 159 MB (the arrays are
132 MB). A position is answered in 0.01 s, and in 1.1 s at the north ecliptic pole (270.0 +66.5), where 30,512
images cover it. Downloads stay capped per pass and band there, as for any field.

**Rebuilding it** (a new release, or files added to the bucket):

```sh
gh workflow run s3index.yml                     # "Image index of the bucket": 20 parallel parts
gh run download <run id> -D cache/s3index       # one folder per part; the merge reads them all
python -m server.s3index cache/s3index server/index/spx_index.npz
python scripts/check_s3index.py --write         # handoff/results/s3index_check.json
```

On 15 November 2026 the run took 86 s: 20 parts of 49–78 s each. The merge checks that every S3 key is rebuilt
exactly and that the compact encoding stays within 0.2″ in position, 0.1 s in time and 1e-3 in the PC matrix.
For a new release, add its bucket prefix to `RELEASES` in `scripts/build_s3_index.py` and its collection to
`COLLECTIONS` in `server/s3index.py` first. Commit the new file and deploy.

## Settings (environment)

| Variable | Default (image) | Meaning |
|---|---|---|
| `PORT` | 8000 | port to listen on (set by Cloud Run and most hosts) |
| `SPX_CACHE` | `/data/cache` | cache folder: image lists, cutouts, built fields |
| `SPX_CACHE_MAX_GB` | 1.5 | size limit of cutouts and built fields; 0 = no limit |
| `SPX_MAX_FOV_DEG` | 0.11667 | largest field in degrees (0.25 = 15′) |
| `SPX_LIVE_LAYERS` | `IMAGE,FLAGS` | layers read for live fields |
| `SPX_COLLECTIONS` | `spherex_dr1,spherex_qr3` | SPHEREx collections searched (a new release is one more name) |
| `SPX_INDEX` | `server/index` | our image index: a `.npz` file or a folder of them; `off` = image lists from IRSA SIA2 only |
| `SPX_RATE_LIMIT` | 1 | 0 turns the request limits off |
| `SPX_NEW_FIELDS_PER_IP` | 40 | new fields per client address per 10 minutes |
| `SPX_MAX_ACTIVE_FIELDS` | 8 | fields with a band someone is waiting for (queued at a request in the last 3 minutes, or being built for one), server-wide; above it a new field gets 429. Bands built ahead do not count |
| `SPX_PREBUILD_FIELDS` | 2 | bands nobody asked for yet are built ahead for this many of the newest fields; 0 = none (the candidates tab still asks for its field's bands; the command above sets 0) |
| `SPX_MAX_BAND_WAITERS` | 64 | requests waiting for a band at once, frames included (the command above sets 800) |
| `SPX_MAX_CUTOUT_SETS` | 2 | fields whose cutouts stay in memory |
| `SPX_MAX_WINDOWS_PER_FIELD` | 12 | user wavelength windows kept per field |
| `SPX_BACKGROUND_S3_WORKERS` | 8 | S3 connections for bands nobody is waiting for |
| `SPX_SIA_RETRY_BUDGET_S` | 25 | seconds of IRSA SIA2 retries before a 503 (the command above sets 60) |
| `SPX_SIA_MAX_ATTEMPTS` | 10 | IRSA SIA2 attempts per image list (the command above sets 100) |
| `SPX_THREADS` | 32 | threads for blocking work (opening a field holds one while IRSA is retried) |
| `SPX_GAIA_PAUSE_S` | 300 | after a Gaia query fails, the archive is not asked again for this many seconds (cached answers are still read; one log line per field, no traceback); 0 = always ask |
| `SPX_GAIA_BUDGET_S` | 15 | seconds one Gaia query may take in all; gallery targets read their `gaia.json` and never ask |
| `SPX_BAND_WAIT_S` | 90 | seconds a request waits for its band |
| `SPX_GALLERY` | `web/data` | the gallery folder (notes, CatWISE files) |
| `FORWARDED_ALLOW_IPS` | `*` | proxies trusted for the client address (request limits) |

Request limits per client address and minute: manifest 20, band 60, spectrum 20, candidates 30, track 6. Over a
limit the server answers 429 with `Retry-After`.

## Scripts

| Script | What it does |
|---|---|
| `scripts/precompute.py [--final]` | builds the gallery (`web/data/`); `--final` only from committed code |
| `scripts/precompute.py --sia-seed-only` | rewrites `web/data/_sia/`: the targets' image lists the server starts with, so they open while IRSA is down |
| `scripts/make_wise_images.py` | AllWISE W1/W2 images on each gallery grid (IRSA's open S3 copy) |
| `scripts/make_catwise.py [--fetch]` | CatWISE2020 sources for the gallery (IRSA cone search) |
| `scripts/make_candidates.py` | automatic candidates for the gallery, from its frames |
| `scripts/acceptance.py [--own-server] [--static-only]` | checks the gallery and a server, item by item |
| `scripts/fov_timing.py [--write]` | cold timing of 15′ fields on a server of its own |
| `scripts/warm_demo.py --api <address>` | fills a server's cache with the demo fields before a presentation |
| `scripts/make_submit_xsd.py` | builds the ADES submit schema used to check MPC drafts (the image runs it) |
| `scripts/build_s3_index.py --out <folder> [--slice i/n]` | reads the pointing and time cards of every Level 2 file in the public bucket (one range request each); one chunk per week plan. The workflow "Image index of the bucket" runs it in 20 parts |
| `python -m server.s3index [chunks] [out] [--split]` | merges the chunks into the index the server loads (`server/index/spx_index.npz`) and checks the encoding |
| `scripts/check_s3index.py [--write]` | checks the index: size, load time, memory, encoding error, the effect of the left-out distortion, the gallery's image lists against IRSA's |
