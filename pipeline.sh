#!/usr/bin/env bash
#
# pipeline.sh — Download Martin County wells on demand, convert to GeoParquet.
#
# Usage:   ./pipeline.sh [YEAR]
# Example: ./pipeline.sh 2024
#
# Requires: bash, curl, gunzip, ogr2ogr (GDAL >= 3.5)
#
# This is a starter scaffold. Read the comments. Replace the [TODO] markers
# with the actual logic. Do not change the structure unless you have a reason.

set -euo pipefail

# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------

# The base directory where RRC stores the county-specific well shapefiles
BASE_URL="https://mft.rrc.texas.gov/link/d551fb20-442e-4b67-84fa-ac3f23ecabb4#"

# The specific file for Martin County (317)
FILE_NAME="well317.zip"

# The full download URL
URL="${BASE_URL}/${FILE_NAME}"

#!/bin/bash

RAW_DIR="data/raw"
FILE_PATH="${RAW_DIR}/${FILE_NAME}"

# Ensure destination directory exists
mkdir -p "$RAW_DIR"

# Example usage:
# curl -O "${URL}"

PROCESSED_DIR="data/processed"
RAW_SHP="${RAW_DIR}/${FILE_NAME}"
RAW_ZIP="${RAW_DIR}/${FILE_NAME%.zip}"
OUT_PARQUET="${PROCESSED_DIR}/storms_${YEAR}.parquet"

# -----------------------------------------------------------------------------
# Step 1: Set up directories
# -----------------------------------------------------------------------------

echo "[1/4] Setting up directories"
# [TODO] Use mkdir -p to create RAW_DIR and PROCESSED_DIR. Both should be
# safe to call even if the directories already exist.

# -----------------------------------------------------------------------------
# Step 2: Download the raw file
# -----------------------------------------------------------------------------

echo "[2/4] Downloading ${FILE_NAME}"
# [TODO] Use curl to download URL into RAW_GZ. Suggested flags:
#   -L       follow redirects
#   -o       write to a specific output file path
#   --fail   exit non-zero on HTTP errors (4xx/5xx)
#
# Skip the download if the file already exists (idempotency).

# Idempotency check: Skip if file already exists
if [ -f "$FILE_PATH" ]; then
    echo "File '${FILE_NAME}' already exists locally. Skipping download."
else
    # -L: Follow redirects
    # --fail: Exit non-zero on HTTP errors (e.g., 404)
    # -o: Save to path
    curl -L --fail -o "$FILE_PATH" "$URL"
    
    if [ $? -eq 0 ]; then
        echo "Successfully downloaded ${FILE_NAME}."
    else
        echo "Error: Failed to download. The link may have expired."
        exit 1
    fi
fi

# -----------------------------------------------------------------------------
# Step 3: Decompress
# -----------------------------------------------------------------------------

echo "[3/4] Decompressing"
# [TODO] Use gunzip to decompress RAW_GZ into RAW_CSV.
# The -k flag keeps the original .gz so the pipeline can rerun.
# Skip this step if RAW_CSV already exists.

#!/usr/bin/env zsh

# Derive paths from FILE_PATH
RAW_ZIP="${FILE_PATH}"
RAW_SHP="${FILE_PATH%.zip}.shp"
OUT_DIR="${FILE_PATH:h}"

# Skip if .shp already exists
if [[ -f "$RAW_SHP" ]]; then
  echo "Skipping: RAW_SHP already exists at '$RAW_SHP'"
else
  echo "Unzipping '$RAW_ZIP' → '$OUT_DIR'"
  unzip -k "$RAW_ZIP" -d "$OUT_DIR"

  # Confirm the .shp was produced
  if [[ -f "$RAW_SHP" ]]; then
    echo "Done: '$RAW_SHP' created"
  else
    echo "Error: unzip ran but '$RAW_SHP' not found — check the zip contents" >&2
    exit 1
  fi
fi
# -----------------------------------------------------------------------------
# Step 4: Convert CSV to GeoParquet
# -----------------------------------------------------------------------------

echo "[4/4] Converting to GeoParquet"
# [TODO] Use ogr2ogr to convert RAW_CSV into a GeoParquet file at OUT_PARQUET.
#
# The CSV uses BEGIN_LON / BEGIN_LAT for the storm start point. ogr2ogr can
# pick those up if you tell it the column names with -oo:
#
#   -oo X_POSSIBLE_NAMES=BEGIN_LON
#   -oo Y_POSSIBLE_NAMES=BEGIN_LAT
#
# The data is in WGS 84 (EPSG:4326). Set that explicitly with -a_srs.
#
# Use -f Parquet for the output format.
#
# Tip: ask your AI pair (see R1.3 prompts 4 and 6) for the exact ogr2ogr
# command, then verify the flags against `ogr2ogr --help` before running.

echo "Done. Output: ${OUT_PARQUET}"
echo "Open it in DuckDB:"
echo "  duckdb -c \"INSTALL spatial; LOAD spatial; SELECT COUNT(*) FROM read_parquet('${OUT_PARQUET}');\""
