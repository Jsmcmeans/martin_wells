#!/usr/bin/env zsh
set -euo pipefail

# backfill_permits.sh
# ===================
# One-time script to download the 12 most recent month-end drilling permit
# master files and merge them all into the accumulated drillingPermits.parquet.
#
# Idempotent — already-downloaded files and already-parsed GeoJSONs/Parquets
# are skipped, so the script can be safely re-run after a partial failure.
#
# Output files kept per month (in data/processed/):
#   drillingPermits_mm-yyyy.geojson
#   drillingPermits_mm-yyyy.parquet
#
# Final accumulated output:
#   data/processed/drillingPermits.parquet

# ── Configuration ──────────────────────────────────────────────────────────────
RAW_DIR="data/raw"
PROCESSED_DIR="data/processed"
MANIFEST_PATH="${RAW_DIR}/.backfill_manifest"
CURRENT_PARQUET="${PROCESSED_DIR}/drillingPermits_current.parquet"
ACCUMULATED_PARQUET="${PROCESSED_DIR}/drillingPermits.parquet"

# ── Step 1: Set up directories ─────────────────────────────────────────────────
echo "[1/4] Setting up directories"
mkdir -p "$RAW_DIR" "$PROCESSED_DIR"

# ── Step 2: Download 12 most recent monthly files ──────────────────────────────
echo "[2/4] Downloading 12 most recent monthly files"
python backfillPermits.py

if [[ ! -f "$MANIFEST_PATH" ]]; then
  echo "  Error: backfillPermits.py did not write '${MANIFEST_PATH}'" >&2
  exit 1
fi

# Load manifest into array (one filename per line, oldest first)
MONTHS=("${(@f)$(cat "$MANIFEST_PATH")}")

if [[ ${#MONTHS[@]} -eq 0 ]]; then
  echo "  Error: Manifest is empty — no files to process." >&2
  exit 1
fi

echo "  ${#MONTHS[@]} files ready to process."

# ── Step 3: Parse, convert, and merge each month ──────────────────────────────
echo ""
echo "[3/4] Parsing → converting → merging (${#MONTHS[@]} months, oldest first)"

SKIPPED=()
PROCESSED=()

for i in {1..${#MONTHS[@]}}; do
  FILE_NAME="${MONTHS[$i]}"
  FILE_PATH="${RAW_DIR}/${FILE_NAME}"

  # Derive mm-yyyy label: daf420.dat.05-31-2026 → 05-2026
  DATE_SUFFIX="${FILE_NAME:11}"            # "05-31-2026"
  MONTH_LABEL="${DATE_SUFFIX:0:2}-${DATE_SUFFIX:6:4}"  # "05-2026"

  GEOJSON_PATH="${PROCESSED_DIR}/drillingPermits_${MONTH_LABEL}.geojson"
  DATED_PARQUET="${PROCESSED_DIR}/drillingPermits_${MONTH_LABEL}.parquet"

  echo ""
  echo "  ── [${i}/${#MONTHS[@]}] ${FILE_NAME} ──"

  # Validate raw file exists
  if [[ ! -f "$FILE_PATH" ]]; then
    echo "    Raw file missing — skipping." >&2
    SKIPPED+=("$FILE_NAME")
    continue
  fi

  # Parse fixed-width → GeoJSON
  if [[ -f "$GEOJSON_PATH" ]]; then
    echo "    GeoJSON exists — skipping parse."
  else
    echo "    Parsing → '${GEOJSON_PATH:t}'"
    python parse_permits.py --input "$FILE_PATH" --output "$GEOJSON_PATH"
    if [[ ! -f "$GEOJSON_PATH" ]]; then
      echo "    Error: parse failed. Skipping month." >&2
      SKIPPED+=("$FILE_NAME")
      continue
    fi
  fi

  # Convert GeoJSON → dated Parquet (NAD27 → WGS 84)
  if [[ -f "$DATED_PARQUET" ]]; then
    echo "    Dated Parquet exists — skipping conversion."
  else
    echo "    Converting → '${DATED_PARQUET:t}'"
    ogr2ogr \
      -f Parquet \
      -s_srs EPSG:4267 \
      -t_srs EPSG:4326 \
      "$DATED_PARQUET" \
      "$GEOJSON_PATH"
    if [[ ! -f "$DATED_PARQUET" ]]; then
      echo "    Error: ogr2ogr failed. Skipping month." >&2
      SKIPPED+=("$FILE_NAME")
      continue
    fi
  fi

  # Copy to current parquet slot and merge into accumulated
  echo "    Merging into accumulated parquet..."
  cp "$DATED_PARQUET" "$CURRENT_PARQUET"
  python mergePermits.py --processed-dir "$PROCESSED_DIR"
  rm -f "$CURRENT_PARQUET"

  PROCESSED+=("$FILE_NAME")
done

# ── Step 4: Summary ────────────────────────────────────────────────────────────
echo ""
echo "[4/4] Backfill complete"
echo "  Months processed : ${#PROCESSED[@]}"
echo "  Months skipped   : ${#SKIPPED[@]}"

if [[ ${#SKIPPED[@]} -gt 0 ]]; then
  echo "  Skipped months:"
  for f in "${SKIPPED[@]}"; do
    echo "    ✗ ${f}"
  done
fi

if [[ -f "$ACCUMULATED_PARQUET" ]]; then
  echo "  Accumulated file : ${ACCUMULATED_PARQUET}"
fi

if [[ -f "${RAW_DIR}/.backfill_failures" ]]; then
  echo "  Download failures: ${RAW_DIR}/.backfill_failures"
fi