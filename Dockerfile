# Portfolio Manager — always-on service
FROM python:3.11-slim

WORKDIR /app

# System deps for pandas/numpy wheels
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Python deps first (layer cache)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code
COPY portfolio/ portfolio/
COPY run.py config.json requirements.txt portfolio_server.py build_bundle.sh ./

# Bundled Forge modules (built on the dev Mac by build_bundle.sh)
COPY vendor/forge/ vendor/forge/

# Runtime data dir — mount a Railway volume here for persistence
RUN mkdir -p /app/data

ENV FORGE_ROOT=/app/vendor/forge
ENV PYTHONUNBUFFERED=1

EXPOSE 5000

HEALTHCHECK --interval=60s --timeout=10s --start-period=30s \
    CMD curl -fs http://localhost:5000/health || exit 1

CMD ["python", "portfolio_server.py"]
