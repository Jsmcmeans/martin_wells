#!/usr/bin/env zsh

# ── Configuration ──────────────────────────────────────────────────────────────
FILE_NAME="well317.zip"
RAW_DIR="data/raw"
PROCESSED_DIR="data/processed"
FILE_PATH="${RAW_DIR}/${FILE_NAME}"

# ── Step 1: Set up directories ─────────────────────────────────────────────────
echo "[1/4] Setting up directories"
mkdir -p "$RAW_DIR" "$PROCESSED_DIR"

# ── Step 2: Download the raw file ──────────────────────────────────────────────
echo "[2/4] Downloading ${FILE_NAME}"

if [[ -f "$FILE_PATH" ]] && file "$FILE_PATH" | grep -q "Zip archive"; then
  echo "File '${FILE_NAME}' already exists locally. Skipping download."
else
  # Remove any pre-existing invalid file (e.g. a prior HTML error response)
  [[ -f "$FILE_PATH" ]] && rm "$FILE_PATH"

  # RRC portal (GoAnywhere MFT) is a JS-rendered SPA — Playwright drives a
  # real headless browser to handle the download. See martinWells.py.
  python martinWells.py

  if file "$FILE_PATH" | grep -q "Zip archive"; then
    echo "Successfully downloaded ${FILE_NAME}."
  else
    echo "Error: Downloaded file is not a valid zip: $(file --brief "$FILE_PATH")" >&2
    rm -f "$FILE_PATH"
    exit 1
  fi
fi

# ── Step 3: Decompress ─────────────────────────────────────────────────────────
#Removed because it is handles in python script

# ── Step 4: [TODO] ─────────────────────────────────────────────────────────────
echo "[4/4] ..."