# syntax=docker/dockerfile:1.7
# ---------------------------------------------------------------------------
# Wave Hack GFS pipeline image
# ---------------------------------------------------------------------------
# Build context is expected to be the wavehack `output/` directory so that
# shared assets (scoring_rules.yaml / dashboard_template.html) can be COPY'd
# into the same tree layout the scripts assume:
#     /app/output/code/wavehack-pipeline/           (source code)
#     /app/output/data/scoring_rules.yaml
#     /app/output/html/dashboard_template.html
#
# Example (local):
#     docker build -f output/code/wavehack-pipeline/Dockerfile \
#                  -t wavehack-pipeline:latest \
#                  output/
#
# The image is Fargate-ready: entrypoint runs the full pipeline and drops
# artifacts under /output (mount a volume there or point ECS to an EFS/S3
# uploader sidecar).
# ---------------------------------------------------------------------------

ARG PYTHON_VERSION=3.14
ARG DEBIAN_RELEASE=bookworm

# ---------------------------------------------------------------------------
# Stage 1: builder — install uv, resolve dependencies into a virtualenv
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim-${DEBIAN_RELEASE} AS builder

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv

# cfgrib / eccodes native deps (build-time headers) + curl for uv install.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        build-essential \
        curl \
        ca-certificates \
        libeccodes0 \
        libeccodes-dev \
 && rm -rf /var/lib/apt/lists/*

# Install uv (astral). Fallback to pip in case the shell installer fails
# behind corporate proxies.
RUN pip install --upgrade pip \
 && pip install "uv>=0.5"

WORKDIR /build

# Copy dependency manifests first for layer caching.
COPY code/wavehack-pipeline/pyproject.toml code/wavehack-pipeline/uv.lock ./
COPY code/wavehack-pipeline/README.md ./README.md
COPY code/wavehack-pipeline/src ./src

# Resolve + install into /opt/venv. --frozen honours uv.lock exactly.
RUN uv sync --extra gfs --frozen --no-dev

# ---------------------------------------------------------------------------
# Stage 2: runtime — slim image with only runtime libs
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim-${DEBIAN_RELEASE} AS runtime

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Tokyo \
    PATH=/opt/venv/bin:$PATH \
    VIRTUAL_ENV=/opt/venv \
    OUTPUT_TARGET=local \
    OUTPUT_MOUNT_DIR=/output

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        libeccodes0 \
        ca-certificates \
        tzdata \
        bash \
 && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
 && echo $TZ > /etc/timezone \
 && rm -rf /var/lib/apt/lists/*

# Bring in the resolved virtualenv from the builder.
COPY --from=builder /opt/venv /opt/venv

# Recreate the wavehack `output/` tree layout expected by the scripts.
# ROOT.parent.parent == /app/output for generate_dashboard etc.
WORKDIR /app/output/code/wavehack-pipeline

# Shared assets outside the pipeline dir (need parent build context).
COPY data/scoring_rules.yaml       /app/output/data/scoring_rules.yaml
COPY html/dashboard_template.html  /app/output/html/dashboard_template.html

# Pipeline source (scripts / src / data / pyproject).
COPY code/wavehack-pipeline/ /app/output/code/wavehack-pipeline/

# Make run_all.sh executable and pre-create intermediate output dirs.
RUN chmod +x /app/output/code/wavehack-pipeline/scripts/run_all.sh \
 && mkdir -p /app/output/code/wavehack-pipeline/output \
             /app/output/data \
             /app/output/html \
             /output

# Non-root user for Fargate best-practice (Lambda ignores this).
RUN useradd --create-home --uid 10001 wavehack \
 && chown -R wavehack:wavehack /app /output
USER wavehack

# Default entrypoint runs the whole pipeline; can be overridden in Fargate
# TaskDefinition or `docker run … <cmd>` for one-off scripts.
ENTRYPOINT ["/app/output/code/wavehack-pipeline/scripts/run_all.sh"]
CMD []
