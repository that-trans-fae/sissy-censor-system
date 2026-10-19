FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_ROOT_USER_ACTION=ignore

WORKDIR /app

# ffmpeg decodes and re-encodes video (plain files, HLS and DASH).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
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

EXPOSE 8080 8082
ENV CENSOR_CONFIG=/config/config.yaml

# No stream_large_bodies: mitmproxy applies it after the addon's
# responseheaders hook, which would stream large images past the censor. The
# addon streams every body it doesn't need to inspect instead.
CMD ["mitmdump", "-s", "/app/censor.py", "--listen-port", "8080", \
     "--set", "confdir=/root/.mitmproxy"]
