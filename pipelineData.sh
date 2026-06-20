#!/usr/bin/env zsh
set -euo pipefail

# pipelineData.sh
# ===============
# Downloads Texas RRC pipeline shapefiles by county and converts them
# to GeoParquet (WGS 84), filtering to active (In Service) pipelines only.
#
# Interactive: prompts for county names (comma-separated, partial match OK).
#
# Output per county (in data/processed/):
#   {county}Pipelines.parquet    (active pipelines, WGS 84)
#
# Example:
#   ./pipelineData.sh
#   > Enter county name(s): Martin, Midland, Howard
#
# Pipeline attributes include: operator name, system name, diameter,
# commodity type (crude, gas, etc.), system type, and mileage.
#
# Re-run idempotency:
#   - Already-extracted shapefiles are skipped during download.
#   - Existing Parquets newer than source shapefiles are skipped.

# ── Configuration ──────────────────────────────────────────────────────────────
RAW_DIR="data/raw"
PROCESSED_DIR="data/processed"
MANIFEST_PATH="${RAW_DIR}/.pipeline_manifest"

# ── Step 1: Set up directories ─────────────────────────────────────────────────
echo "[1/4] Setting up directories"
mkdir -p "$RAW_DIR" "$PROCESSED_DIR"

# ── Step 2: Select counties ───────────────────────────────────────────────────
echo "[2/4] County selection"
echo "  Pipeline shapefiles are per-county on the RRC portal."
echo "  Partial names are OK — 'martin' will match 'MARTIN'."
echo "  Enter 'list' to see all available counties."
echo ""

while true; do
  printf "  Enter county name(s) [comma-separated]: "
  read -r COUNTY_INPUT

  if [[ "${COUNTY_INPUT:l}" == "list" ]]; then
    echo ""
    python pipelineData.py --list-counties
    echo ""
    continue
  fi

  if [[ -z "$COUNTY_INPUT" ]]; then
    echo "  No counties entered. Try again, or type 'list' to see options."
    continue
  fi

  break
done

echo ""
echo "  Selected: '${COUNTY_INPUT}'"
echo ""

# ── Step 3: Download pipeline shapefiles ─────────────────────────────────────
echo "[3/4] Downloading pipeline shapefiles"
echo "  Source  : Texas RRC Pipeline Layers by County"
echo "  Updated : Twice a week"
echo ""

python pipelineData.py \
  --counties "$COUNTY_INPUT" \
  --raw-dir "$RAW_DIR"

if [[ ! -f "$MANIFEST_PATH" ]]; then
  echo "  Error: pipelineData.py did not write '${MANIFEST_PATH}'" >&2
  exit 1
fi

# ── Step 4: Convert to GeoParquet (NAD27 → WGS 84, active only) ─────────────
echo ""
echo "[4/4] Converting to GeoParquet (NAD27 → WGS 84, active pipelines only)"

CONVERTED=0
SKIPPED=0

while IFS=: read -r COUNTY_NAME FIPS_CODE; do
  # Build paths
  # camelCase county name for output filename
  CAMEL=$(python -c "
parts = '${COUNTY_NAME}'.strip().lower().split()
print(parts[0] + ''.join(p.capitalize() for p in parts[1:]))
")
  SHP_DIR="${RAW_DIR}/pipeline${FIPS_CODE}"
  SHP_PATH="${SHP_DIR}/pipe${FIPS_CODE}l.shp"
  PARQUET_PATH="${PROCESSED_DIR}/${CAMEL}Pipelines.parquet"

  echo ""
  echo "  ── ${COUNTY_NAME} County (FIPS ${FIPS_CODE}) ──"

  # Verify shapefile exists
  if [[ ! -f "$SHP_PATH" ]]; then
    echo "    Error: '${SHP_PATH}' not found — skipping." >&2
    SKIPPED=$((SKIPPED + 1))
    continue
  fi

  # Invalidate if source is newer
  if [[ -f "$PARQUET_PATH" ]] && [[ "$SHP_PATH" -nt "$PARQUET_PATH" ]]; then
    echo "    Source shapefile is newer — removing stale Parquet"
    rm -f "$PARQUET_PATH"
  fi

  if [[ -f "$PARQUET_PATH" ]]; then
    echo "    Skipping: '${PARQUET_PATH}' already exists."
    CONVERTED=$((CONVERTED + 1))
    continue
  fi

  echo "    Converting → '${PARQUET_PATH}'"
  echo "    Filter: STATUS_CD = 'I' (In Service only)"

  ogr2ogr \
    -f Parquet \
    -s_srs EPSG:4267 \
    -t_srs EPSG:4326 \
    -where "STATUS_CD = 'I'" \
    "$PARQUET_PATH" \
    "$SHP_PATH"

  if [[ -f "$PARQUET_PATH" ]]; then
    echo "    Done: '${PARQUET_PATH}' created."
    CONVERTED=$((CONVERTED + 1))
  else
    echo "    Error: ogr2ogr ran but '${PARQUET_PATH}' was not created." >&2
    SKIPPED=$((SKIPPED + 1))
  fi

done < "$MANIFEST_PATH"

# ── Summary ────────────────────────────────────────────────────────────────────
echo ""
echo "Pipeline complete."
echo "  Converted : ${CONVERTED}"
echo "  Skipped   : ${SKIPPED}"
echo "  Output    : ${PROCESSED_DIR}/"
echo ""

# List created files
while IFS=: read -r COUNTY_NAME FIPS_CODE; do
  CAMEL=$(python -c "
parts = '${COUNTY_NAME}'.strip().lower().split()
print(parts[0] + ''.join(p.capitalize() for p in parts[1:]))
")
  PARQUET_PATH="${PROCESSED_DIR}/${CAMEL}Pipelines.parquet"
  if [[ -f "$PARQUET_PATH" ]]; then
    SIZE=$(du -h "$PARQUET_PATH" | cut -f1)
    echo "  ${PARQUET_PATH}  (${SIZE})"
  fi
done < "$MANIFEST_PATH"