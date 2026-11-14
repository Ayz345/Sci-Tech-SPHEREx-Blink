# SPHEREx blink API server.
#   docker build --build-arg SPX_COMMIT=$(git --git-dir=../SpaceApps.git rev-parse --short HEAD) -t spx-blink .
#   docker run -p 8000:8000 -v spx-cache:/data/cache spx-blink
# Run it near the data (S3 bucket nasa-irsa-spherex is in us-east-1). The static page (web/) is hosted
# separately and points at this server with ?api=https://<host>.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SPX_CACHE=/data/cache \
    SPX_MAX_FOV_DEG=0.25 \
    SPX_LIVE_LAYERS=IMAGE,FLAGS

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir numpy==2.3.3 scipy==1.16.2 astropy==7.1.0 requests==2.34.2 \
        fastapi==0.162.7 "uvicorn[standard]==0.54.0"

COPY spx/ spx/
COPY server/ server/
# threshold for meta.wing_bands (R Dra), read at run time
COPY handoff/results/wing_validation_v2.json handoff/results/wing_validation_v2.json

ARG SPX_COMMIT=unknown
ENV SPX_COMMIT=${SPX_COMMIT}

RUN useradd --create-home spx && mkdir -p /data/cache && chown -R spx /data
USER spx
VOLUME ["/data/cache"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4)"
CMD ["uvicorn", "server.app:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
