# Multi-stage Dockerfile for BetBot.
# One image, three runtime modes selected by CMD: worker, api, mcp.

# ---- Builder: install dependencies into a clean venv ------------------------
FROM python:3.11-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Build deps (psycopg2 needs libpq-dev to compile if the wheel isn't usable)
RUN apt-get update \
 && apt-get install -y --no-install-recommends gcc libpq-dev \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip \
 && /opt/venv/bin/pip install -r requirements.txt


# ---- Runtime: minimal layer with only what's needed ------------------------
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH=/app

# libpq for psycopg2 runtime, tini for proper signal handling (Ctrl-C, SIGTERM)
RUN apt-get update \
 && apt-get install -y --no-install-recommends libpq5 tini \
 && rm -rf /var/lib/apt/lists/*

# Non-root user for safety
RUN useradd --create-home --shell /bin/bash betbot

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=betbot:betbot . /app

# Ensure /app/data exists and is owned by betbot — docker named volume mounted
# here needs to be writable for tennis ELO ratings, ML calibrator, etc.
RUN mkdir -p /app/data && chown -R betbot:betbot /app/data

USER betbot

# tini reaps zombies and forwards signals correctly
ENTRYPOINT ["/usr/bin/tini", "--"]

# Default = worker (scheduler). Override via `command:` in docker-compose.
CMD ["python", "-m", "betbot.main"]
