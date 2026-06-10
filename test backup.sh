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
echo "[3/4] Decompressing"

RAW_ZIP="${FILE_PATH}"
OUT_DIR="$(dirname "${RAW_ZIP}")"          # e.g. data/raw
ROOT_NAME="$(basename "${RAW_ZIP}" .zip)"  # e.g. well317
UNZIP_DIR="${OUT_DIR}/${ROOT_NAME}"        # e.g. data/raw/well317

if [[ -d "$UNZIP_DIR" ]]; then
  echo "Skipping: '${UNZIP_DIR}' already exists"
else
  echo "Unzipping '${RAW_ZIP}' → '${UNZIP_DIR}'"
  unzip "${RAW_ZIP}" -d "${OUT_DIR}"

  if [[ -d "$UNZIP_DIR" ]]; then
    echo "Done: '${UNZIP_DIR}' created"
  else
    echo "Error: unzip ran but '${UNZIP_DIR}' was not created — check zip contents" >&2
    exit 1
  fi
fi

# ── Step 4: [TODO] ─────────────────────────────────────────────────────────────
echo "[4/4] ..."