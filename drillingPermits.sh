#!/usr/bin/env zsh

# ── Configuration ──────────────────────────────────────────────────────────────
FILE_NAME="daf420.dat"
RAW_DIR="data/raw"
PROCESSED_DIR="data/processed"
FILE_PATH="${RAW_DIR}/${FILE_NAME}"
GEOJSON_PATH="${PROCESSED_DIR}/drillingPermits.geojson"
PARQUET_PATH="${PROCESSED_DIR}/drillingPermits.parquet"

# ── Step 1: Set up directories ─────────────────────────────────────────────────
echo "[1/5] Setting up directories"
mkdir -p "$RAW_DIR" "$PROCESSED_DIR"

# ── Step 2: Download the raw file ──────────────────────────────────────────────
echo "[2/5] Downloading ${FILE_NAME}"

if [[ -f "$FILE_PATH" ]]; then
  echo " File '${FILE_NAME}' already exists locally. Skipping download."
else
  python drillingPermits.py

  if [[ -f "$FILE_PATH" ]]; then
    echo "Successfully downloaded ${FILE_NAME}."
  else
    echo "Error: '${FILE_PATH}' was not created." >&2
    exit 1
  fi
fi

# ── Step 3: Parse fixed-width data to GeoJSON ─────────────────────────────────
echo "[3/5] Parsing ${FILE_NAME} → GeoJSON"

if [[ -f "$GEOJSON_PATH" ]]; then
    echo "  Skipping: '${GEOJSON_PATH}' already exists."
else
    python parse_permits.py \
        --input "$FILE_PATH" \
        --output "$GEOJSON_PATH"

    if [[ -f "$GEOJSON_PATH" ]]; then
        echo "  Done: '${GEOJSON_PATH}' created."
    else
        echo "  Error: parse_permits.py ran but '${GEOJSON_PATH}' was not created." >&2
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
echo "  GeoJSON   : ${GEOJSON_PATH}"
echo "  GeoParquet: ${PARQUET_PATH}"