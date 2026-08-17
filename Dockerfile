FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive \
    CHROME_BIN=/usr/bin/chromium

# Chromium + a virtual display. Xvfb is what lets Chrome run NON-headless on a
# headless box: headless mode is the single most fingerprinted signal there is,
# so running real Chrome on a virtual display is most of why this service exists.
RUN apt-get update && apt-get install -y --no-install-recommends \
      chromium \
      xvfb \
      xauth \
      dumb-init \
      ca-certificates \
      fonts-liberation \
      fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/*

# `xauth` is listed explicitly because it is only a *Recommends* of `xvfb`, and
# --no-install-recommends therefore skips it. `xvfb-run` shells out to `xauth` to
# build its auth file, so without it the container exits immediately on start with
# "xauth: command not found" — a build that succeeds and a container that dies.
# Verified at image build time so the failure can't reach runtime again.
RUN command -v xauth >/dev/null && command -v xvfb-run >/dev/null

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .

RUN useradd -m -u 10001 scraper && chown -R scraper:scraper /app
USER scraper

EXPOSE 8895

ENTRYPOINT ["dumb-init", "--"]
CMD ["xvfb-run", "-a", "--server-args=-screen 0 1920x1080x24", \
     "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8895"]
