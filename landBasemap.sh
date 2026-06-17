#!/usr/bin/env zsh

# ── Configuration ──────────────────────────────────────────────────────────────
RAW_DIR="data/raw"
PROCESSED_DIR="data/processed"
DATASET_ID="g3ex-37ca"
BASE_URL="https://data.texas.gov/resource/${DATASET_ID}.geojson"
GEOJSON_PATH="${RAW_DIR}/landSurveyWest.geojson"
PARQUET_PATH="${PROCESSED_DIR}/martinBasemap.parquet"
BATCH_SIZE=50000   # rows per request — well above typical dataset size

# ── Step 1: Set up directories ─────────────────────────────────────────────────
echo "[1/3] Setting up directories"
mkdir -p "$RAW_DIR" "$PROCESSED_DIR"

# ── Step 2: Download GeoJSON from Texas Open Data Portal (Socrata SODA API) ────
# The /resource/ endpoint caps at 1000 rows by default.
# $limit and $offset handle pagination; $order=:id ensures stable page order.
echo "[2/3] Downloading land survey polygons (GeoJSON)"

if [[ -f "$GEOJSON_PATH" ]]; then
  echo "  Skipping: '${GEOJSON_PATH}' already exists."
else
  # Fetch row count first so we know if pagination is needed
  ROW_COUNT=$(curl -sL --fail \
    "https://data.texas.gov/resource/${DATASET_ID}.json?\$select=count(*)&\$limit=1" \
    | grep -o '"count_":[0-9]*' | grep -o '[0-9]*')

  echo "  Dataset has ${ROW_COUNT} rows. Fetching in pages of ${BATCH_SIZE}..."

  # Write opening FeatureCollection bracket
  echo '{"type":"FeatureCollection","features":[' > "$GEOJSON_PATH"

  OFFSET=0
  FIRST_BATCH=true

  while true; do
    BATCH_FILE=$(mktemp /tmp/rrc_batch_XXXXXX.geojson)

    curl -sL --fail \
      "${BASE_URL}?\$limit=${BATCH_SIZE}&\$offset=${OFFSET}&\$order=:id" \
      -o "$BATCH_FILE"

    # Extract just the features array contents (strip outer FeatureCollection wrapper)
    FEATURES=$(python3 -c "
import json, sys
with open('${BATCH_FILE}') as f:
    fc = json.load(f)
features = fc.get('features', [])
if features:
    print(json.dumps(features)[1:-1])  # strip [ and ]
" 2>/dev/null)

    rm -f "$BATCH_FILE"

    if [[ -z "$FEATURES" ]]; then
      break  # no more rows
    fi

    # Add comma separator between batches
    if [[ "$FIRST_BATCH" == "true" ]]; then
      echo "$FEATURES" >> "$GEOJSON_PATH"
      FIRST_BATCH=false
    else
      echo ",$FEATURES" >> "$GEOJSON_PATH"
    fi

    OFFSET=$(( OFFSET + BATCH_SIZE ))

    # Stop if we've fetched all rows
    if (( OFFSET >= ROW_COUNT )); then
      break
    fi
  done

  # Close the FeatureCollection
  echo ']}' >> "$GEOJSON_PATH"

  if [[ -f "$GEOJSON_PATH" ]]; then
    echo "  Successfully downloaded '${GEOJSON_PATH}'."
  else
    echo "  Error: Download failed." >&2
    exit 1
  fi
fi

# ── Step 3: Convert GeoJSON → GeoParquet ───────────────────────────────────────
echo "[3/3] Converting to GeoParquet"

if [[ -f "$PARQUET_PATH" ]]; then
  echo "  Skipping: '${PARQUET_PATH}' already exists."
else
  # Socrata GeoJSON is already in WGS84 (EPSG:4326) — no reprojection needed
  ogr2ogr \
    -f Parquet \
    "$PARQUET_PATH" \
    "$GEOJSON_PATH"

  if [[ -f "$PARQUET_PATH" ]]; then
    echo "  Done: '${PARQUET_PATH}' created."
  else
    echo "  Error: ogr2ogr ran but '${PARQUET_PATH}' was not created." >&2
    exit 1
  fi
fi

echo ""
echo "Pipeline complete."
echo "  GeoJSON   : ${GEOJSON_PATH}"
echo "  GeoParquet: ${PARQUET_PATH}"