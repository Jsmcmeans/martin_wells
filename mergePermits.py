#!/usr/bin/env python3
"""
merge_permits.py
================
Merges drillingPermits_current.parquet (current month, produced by
drillingPermits.sh) into drillingPermits.parquet (accumulated history).

Run this after drillingPermits.sh completes each month.

Deduplication  : permit_number
Conflict rule  : prefer the record with geometry; ties go to newest month
Backup         : drillingPermits.parquet → drillingPermits_prev.parquet
                 (single rolling backup — previous backup is overwritten)

Usage:
    python merge_permits.py
    python merge_permits.py --processed-dir data/processed
"""

import argparse
import shutil
import sys
from pathlib import Path

import geopandas as gpd
import pandas as pd


def main():
    parser = argparse.ArgumentParser(
        description="Merge current month GeoParquet into accumulated GeoParquet"
    )
    parser.add_argument(
        "--processed-dir",
        default="data/processed",
        help="Directory containing the parquet files (default: data/processed)",
    )
    args = parser.parse_args()

    processed_dir  = Path(args.processed_dir)
    current_path   = processed_dir / "drillingPermits_current.parquet"
    accumulated_path = processed_dir / "drillingPermits.parquet"
    backup_path    = processed_dir / "drillingPermits_prev.parquet"

    # ── Validate ─────────────────────────────────────────────────────────
    if not current_path.exists():
        print(
            f"Error: '{current_path}' not found.\n"
            f"Run drillingPermits.sh first to produce the current month's data.",
            file=sys.stderr,
        )
        sys.exit(1)

    # ── Load current month ───────────────────────────────────────────────
    print(f"  Reading current month: '{current_path}'...")
    new_gdf = gpd.read_parquet(current_path)
    new_geom_count = new_gdf.geometry.notna().sum()
    print(f"    {len(new_gdf):,} records  |  {new_geom_count:,} with geometry")

    # ── Merge with accumulated if it exists ──────────────────────────────
    if accumulated_path.exists():
        print(f"  Loading accumulated: '{accumulated_path}'...")
        existing_gdf = gpd.read_parquet(accumulated_path)
        prev_total = len(existing_gdf)
        prev_geom  = existing_gdf.geometry.notna().sum()
        print(f"    {prev_total:,} records  |  {prev_geom:,} with geometry")

        # Back up before overwriting
        shutil.copy2(accumulated_path, backup_path)
        print(f"  Backed up → '{backup_path}'")

        # Concat new month first so ties in dedup go to the newest data
        combined = pd.concat([new_gdf, existing_gdf], ignore_index=True)

        # Warn about records with missing permit_number before deduplication
        null_pn = combined["permit_number"].isna() | (
            combined["permit_number"].astype(str).str.strip() == ""
        )
        if null_pn.any():
            print(
                f"  Warning: {null_pn.sum():,} records with missing permit_number "
                f"— these will not be deduplicated correctly",
                file=sys.stderr,
            )

        # Flag geometry: non-null and non-empty
        combined["_has_geom"] = (
            combined.geometry.notna() & ~combined.geometry.is_empty
        )

        # Within each permit_number, sort geometry records to the top
        combined = combined.sort_values(
            ["permit_number", "_has_geom"],
            ascending=[True, False],
        )

        # Keep first row per permit_number:
        #   → geometry-having record if one exists
        #   → newest month's record if neither or both have geometry
        combined = combined.drop_duplicates(subset=["permit_number"], keep="first")
        combined = combined.drop(columns=["_has_geom"])

        merged_gdf = gpd.GeoDataFrame(
            combined, geometry="geometry", crs=new_gdf.crs
        )

    else:
        print("  No existing accumulated file — using current month as initial dataset.")
        merged_gdf = new_gdf
        prev_total = 0

    # ── Write accumulated ────────────────────────────────────────────────
    merged_gdf.to_parquet(accumulated_path)

    total        = len(merged_gdf)
    with_geom    = merged_gdf.geometry.notna().sum()
    without_geom = total - with_geom
    net_new      = total - prev_total

    print()
    print(f"  Accumulated result:")
    print(f"    Total permits : {total:,}  ({net_new:+,} vs previous)")
    print(f"    With geometry : {with_geom:,}")
    print(f"    Without geom  : {without_geom:,}")
    print(f"  Saved → '{accumulated_path}'")


if __name__ == "__main__":
    main()