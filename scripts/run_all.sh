#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Wave Hack GFS pipeline — full run entrypoint.
#
# Steps:
#   1. Fetch GFS Wave forecast (0..168h, 4 workers)
#   2. Fetch JMA tide table
#   3. Generate scored forecast
#   4. Generate dashboard.json (+ dashboard.html)
#   5. (Optional) upload dashboard.json to Supabase Storage / S3
#   6. Copy /app/output/data/* into the mounted volume /output
#
# Env vars:
#   OUTPUT_TARGET       local | supabase | s3   (default: local)
#   OUTPUT_MOUNT_DIR    where to copy artefacts on the host (default: /output)
#   POINTS_FILE         points master JSON (default: data/miyazaki_points.json)
#                         例: data/all_points.json で 151pt 全国モード
#   GFS_HOURS_START     forecast hour start (default: 0)
#   GFS_HOURS_END       forecast hour end   (default: 168)
#   GFS_WORKERS         parallel NOAA downloader workers (default: 4)
#   SKIP_TIDE           1 to skip tide fetch
#   SKIP_LLM            1 to skip LLM comment generation (default: 1)
# ---------------------------------------------------------------------------
set -euo pipefail

PIPELINE_DIR="/app/output/code/wavehack-pipeline"
WAVEHACK_OUTPUT="/app/output"
MOUNT_DIR="${OUTPUT_MOUNT_DIR:-/output}"

POINTS_FILE="${POINTS_FILE:-data/miyazaki_points.json}"
HOURS_START="${GFS_HOURS_START:-0}"
HOURS_END="${GFS_HOURS_END:-168}"
WORKERS="${GFS_WORKERS:-4}"
SKIP_TIDE="${SKIP_TIDE:-0}"

log() { printf '[run_all %s] %s\n' "$(date -u +%H:%M:%SZ)" "$*"; }

cd "${PIPELINE_DIR}"
mkdir -p "${WAVEHACK_OUTPUT}/data" "${WAVEHACK_OUTPUT}/html" "${PIPELINE_DIR}/output"

# uv is not required at runtime — we already have /opt/venv on PATH.
PY="${VIRTUAL_ENV:-/opt/venv}/bin/python"

log "Step 1/4: fetch_gfs_wave (points=${POINTS_FILE}, hours=${HOURS_START}..${HOURS_END}, workers=${WORKERS})"
"${PY}" scripts/fetch_gfs_wave.py \
    --points "${POINTS_FILE}" \
    --hours "${HOURS_START}" "${HOURS_END}" \
    --workers "${WORKERS}"

if [[ "${SKIP_TIDE}" != "1" ]]; then
  log "Step 2/4: fetch_tide"
  "${PY}" scripts/fetch_tide.py --points "${POINTS_FILE}"
else
  log "Step 2/4: fetch_tide SKIPPED"
fi

log "Step 3/4: generate_forecast --source gfs (points=${POINTS_FILE})"
"${PY}" scripts/generate_forecast.py --source gfs --points "${POINTS_FILE}"

log "Step 4/4: generate_dashboard"
"${PY}" scripts/generate_dashboard.py

# ---------------------------------------------------------------------------
# Emit artefacts.
# ---------------------------------------------------------------------------
DASHBOARD_JSON="${WAVEHACK_OUTPUT}/data/dashboard.json"

if [[ ! -f "${DASHBOARD_JSON}" ]]; then
  log "ERROR: dashboard.json not produced at ${DASHBOARD_JSON}"
  exit 1
fi

log "Copying artefacts to ${MOUNT_DIR}"
mkdir -p "${MOUNT_DIR}/data" "${MOUNT_DIR}/html"
cp -f "${DASHBOARD_JSON}" "${MOUNT_DIR}/data/dashboard.json"
# forecast.json は wavehack-api の /points /areas /forecast/:id が使う
if [[ -f "${PIPELINE_DIR}/output/forecast.json" ]]; then
  cp -f "${PIPELINE_DIR}/output/forecast.json" "${MOUNT_DIR}/data/forecast.json"
fi
if [[ -f "${WAVEHACK_OUTPUT}/html/dashboard.html" ]]; then
  cp -f "${WAVEHACK_OUTPUT}/html/dashboard.html" "${MOUNT_DIR}/html/dashboard.html"
fi

# Optional remote upload — see scripts/upload_output.py for the switch matrix.
TARGET="${OUTPUT_TARGET:-local}"
if [[ "${TARGET}" != "local" ]]; then
  log "Uploading dashboard.json (target=${TARGET})"
  "${PY}" scripts/upload_output.py --file "${DASHBOARD_JSON}" --target "${TARGET}"
fi

log "Done."
