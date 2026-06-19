#!/usr/bin/env zsh
set -euo pipefail

# ── Configuration ──────────────────────────────────────────────────────────────
RAW_DIR="data/raw"
PROCESSED_DIR="data/processed"
MARKER_PATH="${RAW_DIR}/.last_daf420"
GEOJSON_PATH="${PROCESSED_DIR}/drillingPermits.geojson"
CURRENT_PARQUET="${PROCESSED_DIR}/drillingPermits_current.parquet"
ACCUMULATED_PARQUET="${PROCESSED_DIR}/drillingPermits.parquet"

# ── Step 1: Set up directories ─────────────────────────────────────────────────
echo "[1/6] Setting up directories"
mkdir -p "$RAW_DIR" "$PROCESSED_DIR"

# ── Step 2: Download most recent monthly file ──────────────────────────────────
# Filename is dynamic (daf420.dat.mm-dd-yyyy).
# The Python script finds the most recent file on the portal, handles
# idempotency and ZIP extraction, and writes the filename to .last_daf420.
echo "[2/6] Downloading most recent daf420 monthly file"

python drillingPermits.py

if [[ ! -f "$MARKER_PATH" ]]; then
  echo "  Error: Python script did not write '${MARKER_PATH}'" >&2
  exit 1
fi

FILE_NAME=$(cat "$MARKER_PATH")
FILE_PATH="${RAW_DIR}/${FILE_NAME}"

if [[ ! -f "$FILE_PATH" ]]; then
  echo "  Error: '${FILE_PATH}' was not created." >&2
  exit 1
fi

echo "  Active file: '${FILE_PATH}'"

# ── Step 3: Parse fixed-width data to GeoJSON ─────────────────────────────────
echo "[3/6] Parsing ${FILE_NAME} → GeoJSON"

# Invalidate outputs if source file is newer than existing GeoJSON
if [[ -f "$GEOJSON_PATH" ]] && [[ "$FILE_PATH" -nt "$GEOJSON_PATH" ]]; then
  echo "  Source file is newer — removing stale GeoJSON and current Parquet"
  rm -f "$GEOJSON_PATH" "$CURRENT_PARQUET"
fi

if [[ -f "$GEOJSON_PATH" ]]; then
  echo "  Skipping: '${GEOJSON_PATH}' already exists."
else
  python parse_permits.py \
    --input  "$FILE_PATH" \
    --output "$GEOJSON_PATH"

  if [[ -f "$GEOJSON_PATH" ]]; then
    echo "  Done: '${GEOJSON_PATH}' created."
  else
    echo "  Error: parse_permits.py ran but '${GEOJSON_PATH}' was not created." >&2
    exit 1
  fi
fi

# ── Step 4: Convert GeoJSON → current month GeoParquet (NAD27 → WGS 84) ───────
echo "[4/6] Converting to GeoParquet (NAD27 → WGS 84)"

if [[ -f "$CURRENT_PARQUET" ]]; then
  echo "  Skipping: '${CURRENT_PARQUET}' already exists."
else
  ogr2ogr \
    -f Parquet \
    -s_srs EPSG:4267 \
    -t_srs EPSG:4326 \
    "$CURRENT_PARQUET" \
    "$GEOJSON_PATH"

  if [[ -f "$CURRENT_PARQUET" ]]; then
    echo "  Done: '${CURRENT_PARQUET}' created."
  else
    echo "  Error: ogr2ogr ran but '${CURRENT_PARQUET}' was not created." >&2
    exit 1
  fi
fi

# ── Step 5: Merge current month into accumulated GeoParquet ───────────────────
echo "[5/6] Merging into accumulated GeoParquet"

python mergePermits.py --processed-dir "$PROCESSED_DIR"

if [[ -f "$ACCUMULATED_PARQUET" ]]; then
  echo "  Done: '${ACCUMULATED_PARQUET}' updated."
else
  echo "  Error: mergePermits.py ran but '${ACCUMULATED_PARQUET}' was not created." >&2
  exit 1
fi

rm -f "$CURRENT_PARQUET"
echo "  Cleaned up intermediate: '${CURRENT_PARQUET}'"

# ── Step 6: Summary ────────────────────────────────────────────────────────────
echo "[6/6] Pipeline complete"
echo "  Raw data            : ${FILE_PATH}"
echo "  GeoJSON             : ${GEOJSON_PATH}"
echo "  Current Parquet     : ${CURRENT_PARQUET}"
echo "  Accumulated Parquet : ${ACCUMULATED_PARQUET}"