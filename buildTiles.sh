#!/usr/bin/env zsh
set -euo pipefail

# buildTiles.sh
# =============
# Converts GeoParquet outputs into PMTiles for the Martin County Operator
# Activity Map. PMTiles serve directly from any static CDN — no tile server,
# no warm-up, range-requestable.
#
# Pipeline per layer:
#     GeoParquet  ──ogr2ogr──▶  GeoJSONSeq  ──tippecanoe──▶  PMTiles
#
# Re-run idempotency:
#   - PMTiles are rebuilt only when the source parquet is newer than the
#     existing PMTiles file.
#   - To force a rebuild: `rm data/tiles/*.pmtiles` then re-run.
#
# Requirements:
#   - tippecanoe >= 2.0    (brew install tippecanoe — supports -o foo.pmtiles)
#   - ogr2ogr (GDAL)       (already a project dep)

# ── Configuration ──────────────────────────────────────────────────────────────
PROCESSED_DIR="data/processed"
TILES_DIR="data/tiles"
TMP_DIR="data/tmp/geojsonl"

# ── Step 0: Sanity checks ─────────────────────────────────────────────────────
echo "[0/9] Verifying toolchain"
command -v ogr2ogr >/dev/null 2>&1 || {
    echo "  ERROR: ogr2ogr (GDAL) not on PATH" >&2; exit 1; }
command -v tippecanoe >/dev/null 2>&1 || {
    echo "  ERROR: tippecanoe not on PATH. brew install tippecanoe" >&2; exit 1; }

TIPPECANOE_VERSION=$(tippecanoe --version 2>&1 | head -1)
echo "  ogr2ogr     : OK"
echo "  tippecanoe  : ${TIPPECANOE_VERSION}"

mkdir -p "$TILES_DIR" "$TMP_DIR"

# ── Helper: build one PMTiles layer ───────────────────────────────────────────
# Args: name source_parquet min_zoom max_zoom [extra tippecanoe flags...]
build_layer() {
    local name="$1"
    local source="$2"
    local min_z="$3"
    local max_z="$4"
    shift 4

    local geojsonl="${TMP_DIR}/${name}.geojsonl"
    local pmtiles="${TILES_DIR}/${name}.pmtiles"

    echo ""
    echo "  ── ${name} ──"

    if [[ ! -f "$source" ]]; then
        echo "    SKIP: source '$source' not found"
        return 0
    fi

    # Idempotency: rebuild if source is newer
    if [[ -f "$pmtiles" ]] && [[ ! "$source" -nt "$pmtiles" ]]; then
        SIZE=$(du -h "$pmtiles" | cut -f1)
        echo "    SKIP: '$pmtiles' is up to date ($SIZE)"
        return 0
    fi

    # Step 1: GeoParquet → GeoJSONSeq (line-delimited streams well into tippecanoe)
    echo "    Step 1: $source → GeoJSONSeq"
    rm -f "$geojsonl"
    ogr2ogr -f GeoJSONSeq "$geojsonl" "$source"

    if [[ ! -s "$geojsonl" ]]; then
        echo "    ERROR: ogr2ogr produced empty output. Check source." >&2
        return 1
    fi

    # Step 2: GeoJSONSeq → PMTiles
    echo "    Step 2: GeoJSONSeq → PMTiles (Z${min_z}-${max_z})"
    rm -f "$pmtiles"
    tippecanoe \
        -o "$pmtiles" \
        -l "$name" \
        --minimum-zoom="$min_z" \
        --maximum-zoom="$max_z" \
        --force \
        --quiet \
        "$@" \
        "$geojsonl"

    if [[ ! -f "$pmtiles" ]]; then
        echo "    ERROR: tippecanoe produced no output." >&2
        return 1
    fi

    SIZE=$(du -h "$pmtiles" | cut -f1)
    echo "    OK: $pmtiles ($SIZE)"
}

# ── Build all layers ──────────────────────────────────────────────────────────
# Per-layer zoom + tippecanoe knobs:
#   - Permits: drop densest at low zooms (so the map isn't a solid dot field)
#   - Wells: same
#   - Pipelines: simplify aggressively at low zooms
#   - Hexgrid: low max-zoom (it's a heatmap, not a precision layer)
#   - Survey layers: held to higher zooms (reference-only, off by default)
#   - Abstracts: finest granularity — high zoom only (Z11+)
#   - Parcels: heaviest layer — high zoom only (Z12+)

echo ""
echo "[1/9] permits_signals (points)"
build_layer "permits_signals" "${PROCESSED_DIR}/martinSignals_permits.parquet" 6 14 \
    --drop-densest-as-needed \
    --order-descending-by=signal_priority \
    --extend-zooms-if-still-dropping

echo ""
echo "[2/9] wells_signals (points)"
build_layer "wells_signals" "${PROCESSED_DIR}/martinSignals_wells.parquet" 8 14 \
    --drop-densest-as-needed \
    --extend-zooms-if-still-dropping

echo ""
echo "[3/9] pipelines (lines)"
build_layer "pipelines" "${PROCESSED_DIR}/martinPipelines.parquet" 6 14 \
    --simplification=10

echo ""
echo "[4/9] hexgrid (polygons — heatmap)"
build_layer "hexgrid" "${PROCESSED_DIR}/martinSignals_hexgrid.parquet" 5 12

echo ""
echo "[5/9] survey_block (polygons — reference)"
build_layer "survey_block" "${PROCESSED_DIR}/martinSurvey_lyr_block.parquet" 8 14 \
    --coalesce-densest-as-needed

echo ""
echo "[6/9] survey_section (polygons — reference)"
build_layer "survey_section" "${PROCESSED_DIR}/martinSurvey_lyr_section.parquet" 8 14 \
    --coalesce-densest-as-needed

echo ""
echo "[7/9] survey_abstract (polygons — reference, high zoom only)"
build_layer "survey_abstract" "${PROCESSED_DIR}/martinSurvey_lyr_abstract.parquet" 11 14 \
    --coalesce-densest-as-needed

echo ""
echo "[8/9] parcels (polygons — reference, high zoom only)"
build_layer "parcels" "${PROCESSED_DIR}/martinParcels.parquet" 12 14 \
    --coalesce-densest-as-needed

# ── Cleanup ───────────────────────────────────────────────────────────────────
echo ""
echo "[9/9] Cleanup"
rm -rf "$TMP_DIR"
echo "  Removed intermediate GeoJSONL files"

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "Tile build complete."
echo "  Output: ${TILES_DIR}/"
echo ""

TOTAL_BYTES=0
for f in "${TILES_DIR}"/*.pmtiles(N); do
    SIZE=$(du -h "$f" | cut -f1)
    BYTES=$(du -k "$f" | cut -f1)
    TOTAL_BYTES=$((TOTAL_BYTES + BYTES))
    printf "  %-40s %s\n" "${f:t}" "$SIZE"
done

TOTAL_MB=$((TOTAL_BYTES / 1024))
echo ""
echo "  Total: ~${TOTAL_MB} MB"
echo ""
echo "  Next: copy to your Next.js site:"
echo "    cp ${TILES_DIR}/*.pmtiles  <maplify.dev>/public/tiles/"
echo "    cp data/web/*.json         <maplify.dev>/public/data/"