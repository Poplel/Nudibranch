FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
      libchromaprint-tools \
      ffmpeg \
      curl \
      unzip \
    && rm -rf /var/lib/apt/lists/*

# Deno: yt-dlp needs a JavaScript runtime to solve YouTube's signature / n challenge
# (the bundled yt-dlp-ejs solver from the [default] extra runs under Deno). Without it
# YouTube serves only image formats -> "Requested format is not available".
ENV DENO_INSTALL="/usr/local"
RUN curl -fsSL https://deno.land/install.sh | sh \
    && deno --version

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY nudibranch ./nudibranch

EXPOSE 8000
