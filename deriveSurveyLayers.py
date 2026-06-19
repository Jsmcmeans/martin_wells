#!/usr/bin/env python3
"""
deriveSurveyLayers.py — Dissolve martinSurvey_polygons.parquet into
four separate hierarchy layers and save each as GeoParquet.

Run with --inspect first to see field names and samples, then set the
FIELD MAP below and run normally.

Usage:
    python deriveSurveyLayers.py --inspect   # print fields and exit
    python deriveSurveyLayers.py             # create all four layers
"""

import sys
import argparse
import geopandas as gpd
import pandas as pd

# ── Configuration ──────────────────────────────────────────────────────────────
PROCESSED_DIR  = "data/processed"
POLYGONS_IN    = f"{PROCESSED_DIR}/martinSurvey_polygons.parquet"

# Map each hierarchy level to its field name(s) in the parquet.
FIELD_MAP = {
    "survey":  "LEVEL1_SUR",   # original grantor/survey name   (e.g. "T&P RR CO")
    "block":   "LEVEL2_BLO",   # block number within a survey   (e.g. "37 T2N", "A")
    "section": "LEVEL3_SUR",   # section number within a block  (e.g. "138")
    # abstract = individual polygons keyed by ABSTRACT_N / ABSTRACT_L (no dissolve)
    # LEVEL4_SUR (patentee subdivision) is 48.8% populated — not a reliable boundary level
}

# Output paths
OUT = {
    "survey":   f"{PROCESSED_DIR}/martinSurvey_lyr_survey.parquet",
    "block":    f"{PROCESSED_DIR}/martinSurvey_lyr_block.parquet",
    "section":  f"{PROCESSED_DIR}/martinSurvey_lyr_section.parquet",
    "abstract": f"{PROCESSED_DIR}/martinSurvey_lyr_abstract.parquet",
}
# ──────────────────────────────────────────────────────────────────────────────


def inspect(gdf: gpd.GeoDataFrame):
    """Print field names, types, null counts and sample values then exit."""
    print(f"\n{'─'*60}")
    print(f"  {len(gdf):,} rows   CRS: {gdf.crs}")
    print(f"{'─'*60}")
    for col in gdf.columns:
        if col == "geometry":
            continue
        non_null = gdf[col].notna().sum()
        pct      = non_null / len(gdf) * 100
        samples  = gdf[col].dropna().unique()[:4].tolist()
        print(f"  {col:<20}  {str(gdf[col].dtype):<10}  "
              f"{non_null:>6,}/{len(gdf):,} non-null ({pct:5.1f}%)   "
              f"e.g. {samples}")
    print(f"{'─'*60}\n")
    print("Set FIELD_MAP at the top of this script, then re-run without --inspect.\n")


def dissolve_layer(gdf: gpd.GeoDataFrame, by: list[str], label: str) -> gpd.GeoDataFrame:
    """Dissolve polygons by the given fields, keeping only rows where all fields are set."""
    mask = pd.Series(True, index=gdf.index)
    for col in by:
        mask &= gdf[col].notna() & (gdf[col].astype(str).str.strip() != "")

    subset = gdf[mask].copy()
    print(f"  {label}: {mask.sum():,} rows → dissolving by {by} …", end=" ", flush=True)
    dissolved = subset.dissolve(by=by, as_index=False)
    # Keep only the grouping columns + geometry (drop area/perimeter/internal IDs)
    keep_cols = by + ["geometry"]
    dissolved = dissolved[keep_cols]
    print(f"{len(dissolved):,} polygons")
    return dissolved


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--inspect", action="store_true",
                        help="Print field info and exit without writing files")
    args = parser.parse_args()

    print(f"Reading {POLYGONS_IN} …")
    gdf = gpd.read_parquet(POLYGONS_IN)

    if args.inspect:
        inspect(gdf)
        return

    # ── Validate field map ─────────────────────────────────────────────────────
    missing = [k for k, v in FIELD_MAP.items() if v is None]
    if missing:
        print(f"\nError: FIELD_MAP entries not set: {missing}")
        print("Run with --inspect to discover field names, then fill in FIELD_MAP.\n")
        sys.exit(1)

    for level, field in FIELD_MAP.items():
        if field and field not in gdf.columns:
            print(f"\nError: field '{field}' (mapped to '{level}') not found in parquet.")
            print(f"Available fields: {[c for c in gdf.columns if c != 'geometry']}\n")
            sys.exit(1)

    # ── Layer 1: Abstract (individual polygons, no dissolve) ───────────────────
    print(f"\nLayer 1 — Abstract: {len(gdf):,} polygons (no dissolve needed)")
    gdf.to_parquet(OUT["abstract"], index=False)
    print(f"  Saved: {OUT['abstract']}")

    # ── Layer 2: Section ───────────────────────────────────────────────────────
    by_section = [FIELD_MAP["survey"], FIELD_MAP["block"], FIELD_MAP["section"]]
    # Drop block from grouping if survey doesn't use blocks (no block field populated)
    by_section = [f for f in by_section if f is not None]
    section_gdf = dissolve_layer(gdf, by_section, "Section")
    section_gdf.to_parquet(OUT["section"], index=False)
    print(f"  Saved: {OUT['section']}")

    # ── Layer 3: Block ─────────────────────────────────────────────────────────
    by_block = [FIELD_MAP["survey"], FIELD_MAP["block"]]
    block_gdf = dissolve_layer(gdf, by_block, "Block")
    block_gdf.to_parquet(OUT["block"], index=False)
    print(f"  Saved: {OUT['block']}")

    # ── Layer 4: Survey ────────────────────────────────────────────────────────
    survey_gdf = dissolve_layer(gdf, [FIELD_MAP["survey"]], "Survey")
    survey_gdf.to_parquet(OUT["survey"], index=False)
    print(f"  Saved: {OUT['survey']}")

    print(f"\nDone. Four layers written to {PROCESSED_DIR}/\n")
    for name, path in OUT.items():
        print(f"  {name:<10} → {path}")


if __name__ == "__main__":
    main()