#!/usr/bin/env zsh
set -euo pipefail

# deriveSignals.sh
# ================
# Computes investment-intelligence derived layers for the Martin County
# Operator Activity Map.
#
# Reads from data/processed/ and writes:
#   data/processed/martinSignals_permits.parquet
#   data/processed/martinSignals_wells.parquet
#   data/processed/martinSignals_hexgrid.parquet
#   data/processed/martinSignals_operators.parquet
#   data/web/operators.json
#   data/web/martin_meta.json
#
# Re-run idempotency:
#   - Outputs are unconditionally regenerated each run (they're cheap to
#     compute and depend on multiple upstream files whose timestamps would
#     all need cross-checking — simpler to just rebuild).
#   - To preserve a previous run, copy the parquets aside first.

# ── Configuration ──────────────────────────────────────────────────────────────
PROCESSED_DIR="data/processed"
WEB_DIR="data/web"

# ── Step 1: Set up directories ─────────────────────────────────────────────────
echo "[1/3] Setting up directories"
mkdir -p "$PROCESSED_DIR" "$WEB_DIR"

# ── Step 2: Verify required inputs exist ───────────────────────────────────────
echo "[2/3] Verifying required inputs"

REQUIRED=(
    "${PROCESSED_DIR}/drillingPermits.parquet"
)
OPTIONAL=(
    "${PROCESSED_DIR}/drillingPermitsPending.parquet"
    "${PROCESSED_DIR}/well317s.parquet"
    "${PROCESSED_DIR}/martinPipelines.parquet"
    "${PROCESSED_DIR}/martinProduction_leases.parquet"
    "${PROCESSED_DIR}/martinProduction_wells.parquet"
)

MISSING=0
for f in "${REQUIRED[@]}"; do
    if [[ ! -f "$f" ]]; then
        echo "  ERROR: required input missing: '$f'" >&2
        MISSING=$((MISSING + 1))
    else
        echo "  OK    : $f"
    fi
done

if [[ $MISSING -gt 0 ]]; then
    echo "" >&2
    echo "  Run drillingPermits.sh first to produce drillingPermits.parquet." >&2
    exit 1
fi

for f in "${OPTIONAL[@]}"; do
    if [[ -f "$f" ]]; then
        echo "  OK    : $f"
    else
        echo "  WARN  : $f not found — corresponding signals will be null/empty"
    fi
done

# ── Step 3: Run derivation ─────────────────────────────────────────────────────
echo ""
echo "[3/3] Running deriveSignals.py"
python deriveSignals.py \
    --processed-dir "$PROCESSED_DIR" \
    --web-dir "$WEB_DIR"

# ── Summary ────────────────────────────────────────────────────────────────────
echo ""
echo "Pipeline complete."
echo "  Outputs:"
for f in \
    "${PROCESSED_DIR}/martinSignals_permits.parquet" \
    "${PROCESSED_DIR}/martinSignals_wells.parquet" \
    "${PROCESSED_DIR}/martinSignals_hexgrid.parquet" \
    "${PROCESSED_DIR}/martinSignals_operators.parquet" \
    "${WEB_DIR}/operators.json" \
    "${WEB_DIR}/martin_meta.json"
do
    if [[ -f "$f" ]]; then
        SIZE=$(du -h "$f" 2>/dev/null | cut -f1)
        echo "    $f  ($SIZE)"
    fi
done