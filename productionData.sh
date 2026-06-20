#!/usr/bin/env zsh
set -euo pipefail

# productionData.sh
# =================
# Downloads the Texas RRC Production Data Query (PDQ) dump and extracts
# county-specific production history to per-county Parquet files.
#
# Interactive: prompts the user to enter county names. Supports multiple
# counties (comma-separated). County names are resolved to RRC codes
# using the GP_COUNTY table inside the ZIP — partial matching is supported.
#
# Example:
#   ./productionData.sh
#   > Enter county name(s): Martin, Midland, Howard
#
# Output per county (in data/processed/):
#   {county}Production_wells.parquet    (API bridge table)
#   {county}Production_leases.parquet   (monthly volumes, 1993–present)
#
# Re-run idempotency:
#   - If PDQ_DSV.zip already exists, the download is skipped.
#   - If Parquets exist and are newer than the ZIP, parsing is skipped.
#   - To force a full refresh: rm data/raw/PDQ_DSV.zip data/raw/.last_pdq

# ── Configuration ──────────────────────────────────────────────────────────────
RAW_DIR="data/raw"
PROCESSED_DIR="data/processed"
ZIP_PATH="${RAW_DIR}/PDQ_DSV.zip"
MARKER_PATH="${RAW_DIR}/.last_pdq"

# ── Step 1: Set up directories ─────────────────────────────────────────────────
echo "[1/5] Setting up directories"
mkdir -p "$RAW_DIR" "$PROCESSED_DIR"

# ── Step 2: Download PDQ_DSV.zip ───────────────────────────────────────────────
echo "[2/5] Downloading PDQ_DSV.zip"
echo "  Source  : Texas RRC Production Data Query Dump"
echo "  Size    : ~5 GB compressed / 25+ GB uncompressed"
echo "  Updated : Last Saturday each month"

python productionData.py

if [[ ! -f "$MARKER_PATH" ]]; then
  echo "  Error: productionData.py did not write '${MARKER_PATH}'" >&2
  exit 1
fi

if [[ ! -f "$ZIP_PATH" ]]; then
  echo "  Error: '${ZIP_PATH}' was not created." >&2
  exit 1
fi

echo "  ZIP ready: '${ZIP_PATH}'"
echo ""

# ── Step 3: Select counties ───────────────────────────────────────────────────
echo "[3/5] County selection"
echo "  The PDQ dump is statewide. You can extract one or more counties."
echo "  Partial names are OK — 'martin' will match 'MARTIN'."
echo "  Enter 'list' to see all available counties."
echo ""

while true; do
  printf "  Enter county name(s) [comma-separated]: "
  read -r COUNTY_INPUT

  # Handle 'list' command
  if [[ "${COUNTY_INPUT:l}" == "list" ]]; then
    echo ""
    python parseProduction.py \
      --list-counties \
      --raw-dir "$RAW_DIR" \
      --processed-dir "$PROCESSED_DIR"
    echo ""
    continue
  fi

  # Validate non-empty
  if [[ -z "$COUNTY_INPUT" ]]; then
    echo "  No counties entered. Try again, or type 'list' to see options."
    continue
  fi

  break
done

echo ""
echo "  Selected: '${COUNTY_INPUT}'"
echo ""

# ── Step 4: Parse — filter to selected counties, write Parquets ──────────────
echo "[4/5] Parsing PDQ dump → per-county Parquets"
echo "  Streaming from inside the ZIP (no full extraction)."
echo "  OG_LEASE_CYCLE is the large table — expect 5-20 minutes."
echo ""

python parseProduction.py \
  --counties "$COUNTY_INPUT" \
  --raw-dir "$RAW_DIR" \
  --processed-dir "$PROCESSED_DIR"

# ── Step 5: Cleanup ──────────────────────────────────────────────────────────
echo ""
echo "[5/5] Cleanup"

ZIP_SIZE=$(du -h "$ZIP_PATH" 2>/dev/null | cut -f1)
echo "  PDQ_DSV.zip is ${ZIP_SIZE} and is no longer needed unless you plan to"
echo "  extract additional counties later."
echo ""
printf "  Delete PDQ_DSV.zip? [y/N]: "
read -r DELETE_INPUT

if [[ "${DELETE_INPUT:l}" == "y" || "${DELETE_INPUT:l}" == "yes" ]]; then
  rm -f "$ZIP_PATH" "$MARKER_PATH"
  echo "  Deleted '${ZIP_PATH}' and marker file."
else
  echo "  Keeping '${ZIP_PATH}'."
  echo "  To extract more counties later:  python parseProduction.py --counties \"OtherCounty\""
  echo "  To delete manually:              rm '${ZIP_PATH}' '${MARKER_PATH}'"
fi

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo "Pipeline complete."
echo "  Output directory: ${PROCESSED_DIR}/"
echo ""
echo "  Each county has two Parquets linked by (oil_gas_code, district_no, lease_no)."
echo "  Join wells → your existing well/permit data on api_number for geometry."