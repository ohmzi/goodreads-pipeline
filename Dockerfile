# Playwright's own image, so Chromium and every shared library it needs are
# already present and version-matched. Installing Chromium by hand on a slim
# base is the usual way this goes wrong.
FROM mcr.microsoft.com/playwright/python:v1.49.1-jammy

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# Without DEBIAN_FRONTEND the build hangs indefinitely rather than failing:
# xvfb's dependency chain pulls in packages with debconf prompts (x11-common,
# fontconfig), and dpkg then sits in `--configure --pending` waiting for input
# that never arrives. It looks exactly like a slow download and is not one.
ENV DEBIAN_FRONTEND=noninteractive

# xvfb + x11vnc give the Goodreads login somewhere to happen: there is no
# password flow left to automate, so a human needs a real browser. noVNC's
# static client is served by the app itself over its authenticated WebSocket
# bridge, so websockify is deliberately NOT installed — x11vnc stays on
# loopback and no VNC port is ever exposed.
RUN apt-get update && apt-get install -y --no-install-recommends \
        -o Dpkg::Options::="--force-confdef" \
        -o Dpkg::Options::="--force-confold" \
        xvfb \
        x11vnc \
        novnc \
        procps \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY docker ./docker
RUN chmod +x /app/docker/start-vnc.sh

RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8090

# start-vnc.sh is best-effort and always exits 0, so a headless failure can
# never stop the service from coming up.
CMD ["/bin/bash", "-c", "bash /app/docker/start-vnc.sh; exec uvicorn app.main:app --host 0.0.0.0 --port 8090"]
