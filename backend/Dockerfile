FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
      libchromaprint-tools \
      ffmpeg \
      curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# yt-dlp needs a JavaScript runtime to solve YouTube's signature / n challenge (the bundled
# yt-dlp-ejs solver from the [default] extra runs under it). Without a runtime yt-dlp serves
# only image formats -> "Requested format is not available". The `deno` extra installs the
# `deno` PyPI binary redistribution onto PATH so yt-dlp's runtime search reliably finds it
# (the old curl deno.land install wasn't being detected at runtime). Verify it resolves.
RUN deno --version

COPY nudibranch ./nudibranch

EXPOSE 8000
