#!/usr/bin/env zsh

# ── Configuration ──────────────────────────────────────────────────────────────
RAW_DIR="data/raw"
PROCESSED_DIR="data/processed"
MARKER_PATH="${RAW_DIR}/.last_martinSurvey"
EXTRACTED_DIR="${RAW_DIR}/surv317"

# Processed layer outputs
LINES_PARQUET="${PROCESSED_DIR}/martinSurvey_lines.parquet"
POLYS_PARQUET="${PROCESSED_DIR}/martinSurvey_polygons.parquet"

# ── Step 1: Set up directories ─────────────────────────────────────────────────
echo "[1/5] Setting up directories"
mkdir -p "$RAW_DIR" "$PROCESSED_DIR"

# ── Step 2: Download Martin County survey shapefile ────────────────────────────
# Selects surv317.zip from the RRC MFT portal via Playwright, handles
# idempotency, ZIP extraction, and writes the filename to .last_martinSurvey.
echo "[2/5] Downloading Martin County survey shapefile (surv317.zip)"

python martinSurvey.py

if [[ ! -f "$MARKER_PATH" ]]; then
  echo "  Error: Python script did not write '${MARKER_PATH}'" >&2
  exit 1
fi

FILE_NAME=$(cat "$MARKER_PATH")
ZIP_PATH="${RAW_DIR}/${FILE_NAME}"

if [[ ! -d "$EXTRACTED_DIR" ]]; then
  echo "  Error: '${EXTRACTED_DIR}' was not created." >&2
  exit 1
fi

echo "  Active file : '${ZIP_PATH}'"
echo "  Extracted to: '${EXTRACTED_DIR}'"

# ── Step 3: Convert survey lines → GeoParquet (NAD27 → WGS 84) ────────────────
echo "[3/5] Converting survey lines to GeoParquet (NAD27 → WGS 84)"

LINES_SHP="${EXTRACTED_DIR}/surv317l.shp"

# Invalidate output if source is newer than existing Parquet
if [[ -f "$LINES_PARQUET" ]] && [[ "$LINES_SHP" -nt "$LINES_PARQUET" ]]; then
  echo "  Source file is newer — removing stale lines Parquet"
  rm -f "$LINES_PARQUET"
fi

if [[ -f "$LINES_PARQUET" ]]; then
  echo "  Skipping: '${LINES_PARQUET}' already exists."
else
  ogr2ogr \
    -f Parquet \
    -s_srs EPSG:4267 \
    -t_srs EPSG:4326 \
    "$LINES_PARQUET" \
    "$LINES_SHP"

  if [[ -f "$LINES_PARQUET" ]]; then
    echo "  Done: '${LINES_PARQUET}' created."
  else
    echo "  Error: ogr2ogr ran but '${LINES_PARQUET}' was not created." >&2
    exit 1
  fi
fi

# ── Step 4: Convert survey polygons → GeoParquet (NAD27 → WGS 84) ─────────────
echo "[4/5] Converting survey polygons to GeoParquet (NAD27 → WGS 84)"

POLYS_SHP="${EXTRACTED_DIR}/surv317p.shp"

# Invalidate output if source is newer than existing Parquet
if [[ -f "$POLYS_PARQUET" ]] && [[ "$POLYS_SHP" -nt "$POLYS_PARQUET" ]]; then
  echo "  Source file is newer — removing stale polygons Parquet"
  rm -f "$POLYS_PARQUET"
fi

if [[ -f "$POLYS_PARQUET" ]]; then
  echo "  Skipping: '${POLYS_PARQUET}' already exists."
else
  ogr2ogr \
    -f Parquet \
    -s_srs EPSG:4267 \
    -t_srs EPSG:4326 \
    "$POLYS_PARQUET" \
    "$POLYS_SHP"

  if [[ -f "$POLYS_PARQUET" ]]; then
    echo "  Done: '${POLYS_PARQUET}' created."
  else
    echo "  Error: ogr2ogr ran but '${POLYS_PARQUET}' was not created." >&2
    exit 1
  fi
fi

# ── Step 5: Summary ────────────────────────────────────────────────────────────
echo "[5/5] Pipeline complete"
echo "  Raw zip          : ${ZIP_PATH}"
echo "  Extracted        : ${EXTRACTED_DIR}/"
echo "  Survey lines     : ${LINES_PARQUET}"
echo "  Survey polygons  : ${POLYS_PARQUET}"