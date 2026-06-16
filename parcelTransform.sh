#!/usr/bin/env zsh

# ── Configuration ──────────────────────────────────────────────────────────────
FILE_NAME="stratmap25-landparcels_48317_martin_202505.shp"
RAW_DIR="data/raw/MartinParcels/shp"
PROCESSED_DIR="data/processed"
FILE_PATH="${RAW_DIR}/${FILE_NAME}"
PARQUET_PATH="${PROCESSED_DIR}/martinParcels.parquet"

#Check if files exist and then convert shapefile to parquet with ogr2ogr, reprojecting to WGS 84 (EPSG:4326)
if [[ -f "$PARQUET_PATH" ]]; then
  echo "Skipping: '${PARQUET_PATH}' already exists"
else
 
  if [[ -z "$FILE_PATH" ]]; then
    echo "Error: No *s.shp found in '${RAW_DIR}'" >&2
    echo "  Available files:" >&2
    ls "$RAW_DIR" >&2
    exit 1
  fi
 
  echo "  Source: '${FILE_PATH}'"
 
  #Convert shapefile to parquet with ogr2ogr, reprojecting to WGS 84 (EPSG:4326)
  ogr2ogr -f "Parquet" "${PARQUET_PATH}" "${FILE_PATH}" -t_srs EPSG:4326
 
  if [[ -f "$PARQUET_PATH" ]]; then
    echo "Done: '${PARQUET_PATH}' created"
  else
    echo "Error: ogr2ogr ran but '${PARQUET_PATH}' was not created" >&2
    exit 1
  fi
fi