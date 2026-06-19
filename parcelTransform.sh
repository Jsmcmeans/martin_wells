#!/usr/bin/env zsh
set -euo pipefail

# ── Configuration ──────────────────────────────────────────────────────────────
RAW_DIR="data/raw/MartinParcels/shp"
PROCESSED_DIR="data/processed"
PARQUET_PATH="${PROCESSED_DIR}/martinParcels.parquet"

mkdir -p "$PROCESSED_DIR"

# Discover the most recent parcel shapefile dynamically.
# The filename encodes a release date (e.g. stratmap25-landparcels_48317_martin_202505.shp)
# so we glob and take the last (lexicographically greatest = newest release).
SHP_FILES=("${RAW_DIR}"/stratmap25-landparcels_48317_martin_*.shp(N))
if [[ ${#SHP_FILES[@]} -eq 0 ]]; then
  echo "Error: No stratmap25-landparcels_48317_martin_*.shp found in '${RAW_DIR}'" >&2
  echo "  Available files:" >&2
  ls "$RAW_DIR" >&2
  exit 1
fi
FILE_PATH="${SHP_FILES[-1]}"

echo "  Source: '${FILE_PATH}'"

# Invalidate output if source shapefile is newer than existing Parquet
if [[ -f "$PARQUET_PATH" ]] && [[ "$FILE_PATH" -nt "$PARQUET_PATH" ]]; then
  echo "  Source file is newer — removing stale Parquet"
  rm -f "$PARQUET_PATH"
fi

if [[ -f "$PARQUET_PATH" ]]; then
  echo "Skipping: '${PARQUET_PATH}' already exists"
else
  ogr2ogr -f "Parquet" "${PARQUET_PATH}" "${FILE_PATH}" -t_srs EPSG:4326

  if [[ -f "$PARQUET_PATH" ]]; then
    echo "Done: '${PARQUET_PATH}' created"
  else
    echo "Error: ogr2ogr ran but '${PARQUET_PATH}' was not created" >&2
    exit 1
  fi
fi
