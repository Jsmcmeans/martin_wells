#!/usr/bin/env zsh
set -euo pipefail

# ── Configuration ──────────────────────────────────────────────────────────────
RAW_DIR="data/raw"
PROCESSED_DIR="data/processed"
GEOJSON_PATH="${PROCESSED_DIR}/drillingPermitsPending.geojson"
PARQUET_PATH="${PROCESSED_DIR}/drillingPermitsPending.parquet"

# ── Step 1: Set up directories ─────────────────────────────────────────────────
echo "[1/5] Setting up directories"
mkdir -p "$RAW_DIR" "$PROCESSED_DIR"

# ── Step 2: Download the raw file ──────────────────────────────────────────────
# Downloads 3 files: dp_drilling_permit_pending_*, dp_wellbore_pending_*,
# dp_latlongs_pending_*. Filenames are dynamic (yyyymmddhhmmss timestamp).
# The Python script finds the most recent files on the server, handles
# idempotency, and writes the permit filename to .last_permit_pending.
echo "[2/5] Downloading most recent drilling permits pending file"

python drillingPermitsPending.py

# Read the filename the Python script resolved for the main permit file
LAST_FILE_PATH="${RAW_DIR}/.last_permit_pending"
if [[ ! -f "$LAST_FILE_PATH" ]]; then
    echo "  Error: Python script did not write .last_permit_pending" >&2
    exit 1
fi

FILE_NAME=$(cat "$LAST_FILE_PATH")
FILE_PATH="${RAW_DIR}/${FILE_NAME}"

if [[ ! -f "$FILE_PATH" ]]; then
    echo "  Error: '${FILE_PATH}' was not created." >&2
    exit 1
fi
echo "  Active permit file: '${FILE_PATH}'"

# Verify companion files exist using zsh glob arrays (avoids ls parsing antipattern)
WELLBORE_FILES=("${RAW_DIR}"/dp_wellbore_pending_*.txt(N))
if [[ ${#WELLBORE_FILES[@]} -eq 0 ]]; then
    echo "  Error: No dp_wellbore_pending_*.txt found in '${RAW_DIR}'" >&2
    exit 1
fi
WELLBORE_FILE="${WELLBORE_FILES[-1]}"
echo "  Active wellbore file: '${WELLBORE_FILE}'"

# Handles both dp_latlong_pending_* and dp_latlongs_pending_* naming
LATLONG_FILES=("${RAW_DIR}"/dp_latlong{,s}_pending_*.txt(N))
if [[ ${#LATLONG_FILES[@]} -eq 0 ]]; then
    echo "  Error: No dp_latlong(s)_pending_*.txt found in '${RAW_DIR}'" >&2
    exit 1
fi
LATLONG_FILE="${LATLONG_FILES[-1]}"
echo "  Active latlong file : '${LATLONG_FILE}'"

# ── Step 3: Parse delimited data to GeoJSON ────────────────────────────────────
echo "[3/5] Parsing pending permit files → GeoJSON"

# Invalidate outputs if the permit source file is newer than existing GeoJSON
if [[ -f "$GEOJSON_PATH" ]] && [[ "$FILE_PATH" -nt "$GEOJSON_PATH" ]]; then
    echo "  Source file is newer — removing stale GeoJSON and Parquet"
    rm -f "$GEOJSON_PATH" "$PARQUET_PATH"
fi

if [[ -f "$GEOJSON_PATH" ]]; then
    echo "  Skipping: '${GEOJSON_PATH}' already exists."
else
    python parse_pending.py \
        --raw-dir "$RAW_DIR" \
        --output "$GEOJSON_PATH"

    if [[ -f "$GEOJSON_PATH" ]]; then
        echo "  Done: '${GEOJSON_PATH}' created."
    else
        echo "  Error: parse_pending.py ran but '${GEOJSON_PATH}' was not created." >&2
        exit 1
    fi
fi

# ── Step 4: Convert GeoJSON → GeoParquet (NAD27 → WGS 84) ─────────────────────
echo "[4/5] Converting to GeoParquet (NAD27 → WGS 84)"

if [[ -f "$PARQUET_PATH" ]]; then
    echo "  Skipping: '${PARQUET_PATH}' already exists."
else
    ogr2ogr \
        -f Parquet \
        -s_srs EPSG:4267 \
        -t_srs EPSG:4326 \
        "$PARQUET_PATH" \
        "$GEOJSON_PATH"

    if [[ -f "$PARQUET_PATH" ]]; then
        echo "  Done: '${PARQUET_PATH}' created."
    else
        echo "  Error: ogr2ogr ran but '${PARQUET_PATH}' was not created." >&2
        exit 1
    fi
fi

# ── Step 5: Summary ───────────────────────────────────────────────────────────
echo "[5/5] Pipeline complete"
echo "  Raw data  : ${FILE_PATH}"
echo "  Wellbore  : ${WELLBORE_FILE}"
echo "  LatLong   : ${LATLONG_FILE}"
echo "  GeoJSON   : ${GEOJSON_PATH}"
echo "  GeoParquet: ${PARQUET_PATH}"