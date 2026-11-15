# SPHEREx blink API server. Built and smoke-tested by .github/workflows/docker.yml on every push (health, catwise).
#   docker build --build-arg SPX_COMMIT=$(git --git-dir=../SpaceApps.git rev-parse --short HEAD) -t spx-blink .
#   docker run -p 8000:8000 -v spx-data:/data spx-blink
# Run it near the data (S3 bucket nasa-irsa-spherex is in us-east-1). The static page (web/) is hosted
# separately and points at this server (DEPLOY.md). Settings: see DEPLOY.md; all can be overridden at run time.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SPX_CACHE=/data/cache \
    SPX_MAX_FOV_DEG=0.11667 \
    SPX_LIVE_LAYERS=IMAGE,FLAGS \
    FORWARDED_ALLOW_IPS=* \
    PORT=8000

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir numpy==2.3.3 scipy==1.16.2 astropy==7.1.0 requests==2.34.2 \
        fastapi==0.162.7 "uvicorn[standard]==0.54.0" lxml==6.0.2

COPY spx/ spx/
COPY server/ server/
COPY scripts/make_submit_xsd.py scripts/make_submit_xsd.py
# ADES submit schema for checking MPC drafts (downloads adesmaster.xml and the XSLT from the IAU repository)
RUN python scripts/make_submit_xsd.py
# results read at run time: wing threshold (R Dra), WISE 0855 notes, astrometry validation text in MPC drafts
COPY handoff/results/wing_validation_v2.json handoff/results/wise0855_detection_v2.json \
     handoff/results/astrometry_validation_v2.json handoff/results/
# the gallery (web/data): notes of the five targets, their catwise.json for /api/catwise (~28 MB)
COPY web/data/ web/data/

ARG SPX_COMMIT=unknown
ENV SPX_COMMIT=${SPX_COMMIT}

# not root; /data is the volume (cache of image lists, cutouts and built fields)
RUN useradd --create-home spx && mkdir -p /data/cache && chown -R spx /data /app/cache
USER spx
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/health' % os.environ.get('PORT', '8000'), timeout=4)"
# $PORT from the host (Fly, Render, ...), else 8000; proxy headers give the client IP for the request limits
CMD ["sh", "-c", "exec uvicorn server.app:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers"]
