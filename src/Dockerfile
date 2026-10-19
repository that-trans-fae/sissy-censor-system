FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_ROOT_USER_ACTION=ignore

WORKDIR /app

# ffmpeg decodes and re-encodes video (plain files, HLS and DASH). The VA-API
# drivers (AMD through Mesa, Intel's two) let it use a GPU that compose
# passes in (video.hwaccel); NVENC needs nothing here, since the NVIDIA
# runtime mounts the host driver's libraries.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg mesa-va-drivers \
       $(if [ "$(dpkg --print-architecture)" = amd64 ]; then echo intel-media-va-driver i965-va-driver; fi) \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

# Bake model weights in so the container runs offline. The github.com
# release-download URL redirects to a login page for scripted clients, so
# fetch through the API asset endpoint and pin the checksum.
ARG NUDENET_640M_ASSET=176832019
ARG NUDENET_640M_SHA256=04fe3d77980780c1f8297dc6d7f942fd5b3abe6942a188f742a85241e4f634eb
RUN python - <<PY
import hashlib, os, urllib.request
req = urllib.request.Request(
    "https://api.github.com/repos/notAI-tech/NudeNet/releases/assets/${NUDENET_640M_ASSET}",
    headers={"Accept": "application/octet-stream"},
)
data = urllib.request.urlopen(req).read()
digest = hashlib.sha256(data).hexdigest()
assert digest == "${NUDENET_640M_SHA256}", f"640m.onnx checksum mismatch: {digest}"
os.makedirs("/app/models", exist_ok=True)
open("/app/models/640m.onnx", "wb").write(data)
PY

COPY censor_proxy/ censor_proxy/
COPY censor.py .

# What the host needs, copied out by the installer and by scs-update after
# each update: the compose file, the updaters and the browser icon. The icon
# comes from docs/brand through the "brand" build context (compose.yaml and
# tools/release.sh pass it; by hand: --build-context brand=../docs/brand).
COPY dist/ dist/
COPY --from=brand browser-icon.svg dist/

EXPOSE 8080 8082
ENV CENSOR_CONFIG=/config/config.yaml

# Healthy once the proxy answers on its health host with the models loaded:
# scs-update waits for this before keeping an update, and goes back to the
# old image if it never comes. start-period covers loading the models.
HEALTHCHECK --interval=60s --timeout=10s --start-period=180s --start-interval=5s --retries=3 \
    CMD ["python", "-m", "censor_proxy.updates", "health"]

# The release, set by tools/release.sh; last so a new version rebuilds no
# other layer.
ARG SCS_VERSION=dev
ENV SCS_VERSION=$SCS_VERSION
LABEL org.opencontainers.image.title="Sissy Censor System" \
      org.opencontainers.image.version=$SCS_VERSION

# No stream_large_bodies: mitmproxy applies it after the addon's
# responseheaders hook, which would stream large images past the censor. The
# addon streams every body it doesn't need to inspect instead.
# The CA lives outside /root so the service can run as an unprivileged user
# (compose.yaml sets one); the directory is bind-mounted from ./mitmproxy.
CMD ["mitmdump", "-s", "/app/censor.py", "--listen-port", "8080", \
     "--set", "confdir=/mitmproxy"]
