#!/usr/bin/env python3
"""
deriveSignals.py
================
Computes investment-intelligence derived layers for the Martin County
Operator Activity Map.

Reads existing pipeline outputs from data/processed/ and produces:
    data/processed/martinSignals_permits.parquet     (Point, WGS 84)
    data/processed/martinSignals_wells.parquet       (Point, WGS 84)
    data/processed/martinSignals_hexgrid.parquet     (Polygon, WGS 84)
    data/processed/martinSignals_operators.parquet   (non-spatial)
    data/web/operators.json                          (frontend filter source)
    data/web/martin_meta.json                        (pipeline metadata)

Signal classes (per permit):
    pending_approval        — from pending file (status = P)
    approved_unspud         — recent issue date, no spud
    recently_drilled        — spud within last 90 days
    drilled_no_completion   — has spud, no final_update (older than 90 days)
    historical              — everything else

Run after the standard pipeline scripts have populated data/processed/.

Usage:
    python deriveSignals.py
    python deriveSignals.py --processed-dir data/processed --web-dir data/web
"""

import argparse
import json
import re
import sys
import warnings
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import geopandas as gpd
from shapely.geometry import Polygon
from shapely.ops import unary_union

# Quiet a handful of harmless GeoPandas/Shapely deprecation chatter
warnings.filterwarnings("ignore", category=UserWarning, module="geopandas")
warnings.filterwarnings("ignore", category=FutureWarning, module="pandas")

try:
    import h3
except ImportError:
    print("ERROR: h3 not installed. Add `h3>=4.0` to requirements.txt", file=sys.stderr)
    sys.exit(1)

# h3 v4 (which we require) renamed the core API. Bind to local names so
# the rest of the script reads cleanly regardless of internal renames.
if not hasattr(h3, "latlng_to_cell"):
    print("ERROR: h3 v4+ required. `pip install --upgrade 'h3>=4.0'`", file=sys.stderr)
    sys.exit(1)

H3_CELL_FOR = lambda lat, lng, res: h3.latlng_to_cell(lat, lng, res)
H3_BOUNDARY_FOR = h3.cell_to_boundary  # returns [(lat, lng), ...]


# ── Configuration ────────────────────────────────────────────────────────────
MARTIN_FIPS = "317"
RECENT_DAYS = 90
NEW_ENTRANT_LOOKBACK_DAYS = 365 + 90  # silence prior to recent activity
H3_RESOLUTION = 7                     # ~5 km² hexes
NEAR_WELL_RADIUS_MI = 1.0
TREND_GROW_THRESHOLD = 0.20
TREND_DECLINE_THRESHOLD = -0.20

WGS84 = "EPSG:4326"
UTM14N = "EPSG:32614"                 # Local projection for accurate distance
METERS_PER_MILE = 1609.344
NEAR_WELL_RADIUS_M = NEAR_WELL_RADIUS_MI * METERS_PER_MILE


# ── Tiny helpers ─────────────────────────────────────────────────────────────
def _today():
    return pd.Timestamp(datetime.now(timezone.utc).date())


def _parse_dates(s):
    """Parse to datetime, with auto-repair for malformed RRC source format.

    Handles two data-quality issues produced by the daf420 format mismatch:

    1. YYMMDD?? mis-slicing. parse_permits.py assumed CCYYMMDD but many
       records use YYMMDD?? (2-digit year + month + day + 2 trailing bytes
       that vary: '00', '20', etc.). After parse_permits.py slices them as
       4+2+2, the resulting ISO-shaped string has a wrong century:
           '1406-01-20'  → repair → '2014-06-01'
           '2504-11-20'  → repair → '2025-04-11'
           '2605-28-00'  → repair → '2026-05-28'
       Detection: valid ISO pattern (YYYY-MM-DD) with year outside [1900,2035].
       Repair: strip dashes → 8 raw digits → YYMMDD re-slice → prepend century
       via Y2K window (YY > 50 → 19xx, YY ≤ 50 → 20xx).

    2. Genuine year typos (e.g. '2408-06-20'). After repair attempt, any
       remaining out-of-range years are coerced to NaT.

    parse_permits.py now applies the same year-range guard at parse time,
    so newly generated parquets will not contain these strings. This function
    remains the downstream safety net for already-generated files.
    """
    if not isinstance(s, pd.Series):
        s = pd.Series(s)
    if len(s) == 0:
        return pd.to_datetime(s, errors="coerce")

    if s.dtype == object or pd.api.types.is_string_dtype(s):
        s_str = s.astype(str).str.strip()

        # Detect ISO-shaped strings with out-of-range years
        iso_mask = s_str.str.match(r"^\d{4}-\d{2}-\d{2}$", na=False)
        if iso_mask.any():
            years = pd.to_numeric(s_str.str.slice(0, 4), errors="coerce")
            bad_year = iso_mask & (~years.between(1900, 2035, inclusive="both").fillna(False))

            if bad_year.any():
                # Strip dashes → 8-char raw digits → YYMMDD?? re-slice
                raw8   = s_str[bad_year].str.replace("-", "", regex=False)
                yy     = raw8.str[0:2]
                mm2    = raw8.str[2:4]
                dd2    = raw8.str[4:6]
                yr2    = pd.to_numeric(yy,  errors="coerce")
                month2 = pd.to_numeric(mm2, errors="coerce")
                day2   = pd.to_numeric(dd2, errors="coerce")
                valid  = (month2.between(1, 12) & day2.between(1, 31)).fillna(False)
                century = yr2.apply(
                    lambda y: "19" if pd.notna(y) and y > 50 else "20"
                )
                repaired = (century + yy + "-" + mm2 + "-" + dd2).where(valid, None)
                s_str = s_str.copy()
                s_str[bad_year] = repaired

        # Nullify any remaining out-of-range years (genuine typos)
        years2 = pd.to_numeric(s_str.str.slice(0, 4), errors="coerce")
        in_range = years2.between(1900, 2035, inclusive="both").fillna(False)
        s = s_str.where(in_range, None)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        return pd.to_datetime(s, errors="coerce")


def _proximity_bucket(d_mi):
    if pd.isna(d_mi):
        return None
    if d_mi < 0.5:
        return "near"
    if d_mi < 2.0:
        return "medium"
    return "far"


def _find_api_column(gdf):
    """RRC well shapefiles use one of several names for the API field."""
    candidates = ["api_number", "API_NUMBER", "API_NUM", "API_NO", "API", "APINUM"]
    cols_upper = {c.upper(): c for c in gdf.columns}
    for cand in candidates:
        if cand in gdf.columns:
            return cand
        if cand.upper() in cols_upper:
            return cols_upper[cand.upper()]
    return None


def _normalize_api(series, width=8):
    """API in RRC well shapefile is sometimes stored as a number, sometimes
    a string, sometimes with leading zeros stripped. Coerce to fixed-width
    zero-padded string to match the production_wells `api_number` format."""
    s = series.astype(str).str.strip()
    # Drop any decimal trailers from float→str conversions
    s = s.str.replace(r"\.0+$", "", regex=True)
    # Keep only the first run of digits (some files include suffixes)
    s = s.str.extract(r"(\d+)", expand=False)
    s = s.fillna("").str.zfill(width)
    s = s.where(s != "0" * width, None)
    return s


def _coerce_mixed_objects(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Coerce object columns with mixed date/string values to plain strings.

    When pd.concat() joins accumulated permits (Date-typed columns, read from
    GeoParquet) with pending permits (string-typed columns from parse_pending),
    some columns become object dtype containing a mix of Python date objects
    and plain strings. PyArrow rejects these on write with:
        'object of type <class str> cannot be converted to int'
    because it sees the column as Date32 (stored as int32) but can't convert
    the string values. Convert any such column to clean ISO-string values.
    """
    out = gdf.copy()
    for col in out.columns:
        if col == "geometry" or out[col].dtype != object:
            continue
        sample = out[col].dropna().head(20)
        if sample.empty:
            continue
        # If any value is NOT already a string, the column is mixed-type
        if any(not isinstance(v, str) for v in sample):
            out[col] = out[col].apply(
                lambda x: (
                    x.isoformat() if hasattr(x, "isoformat")
                    else (
                        None if x is None or (isinstance(x, float) and pd.isna(x))
                        else str(x) if str(x) not in ("NaT", "None", "nan", "NaN")
                        else None
                    )
                )
            )
    return gpd.GeoDataFrame(out, geometry="geometry", crs=gdf.crs)


def _filter_to_county(gdf, fips):
    """Try every county-code column we know about, return first hit.

    The pending permits parquet stores county codes with embedded double-quote
    characters (e.g. '"317"' instead of '317') because the source file format
    has changed. Strip both whitespace and quotes before comparing.
    Also adds 'district' as a fallback via the known Martin County district
    codes (08 / 8A / 7C) in case no county-code column is populated.
    """
    candidates = [
        "permit_county_code", "county_code", "onshore_county",
        "wb_county_code", "api_county_code",
    ]
    for col in candidates:
        if col in gdf.columns:
            # Strip whitespace AND embedded quote characters
            cleaned = gdf[col].astype(str).str.strip().str.strip("\"'")
            mask = cleaned == fips
            if mask.any():
                return gdf[mask].copy(), col

    # Fallback: filter by district code for Martin County (District 08)
    if "district" in gdf.columns:
        mask = gdf["district"].astype(str).str.strip().isin(["08", "8", "010", "10"])
        if mask.any():
            return gdf[mask].copy(), "district"

    return gdf.iloc[0:0].copy(), None


# ── Survey geometry imputation ───────────────────────────────────────────────
def _parse_abstract_num(s: str):
    """Extract the integer abstract number from a surface_abstract field.
    Returns None for '0' (not a valid abstract key) or unparseable values.
    """
    if not isinstance(s, str):
        return None
    m = re.search(r"\d+", s.strip())
    if not m:
        return None
    n = int(m.group())
    return str(n) if n > 0 else None


def _reconstruct_survey(surf_block: str, surf_survey: str) -> str:
    """Reconstruct the full survey / grantor name from the two split fields.

    The daf420 fixed-width layout writes the block designation in chars
    254–263 (10 chars) and the survey name in chars 264–318 (55 chars).
    For T&P-type blocks the tier ('T2N') fills the start of the block field
    and the survey name bleeds in:
        surface_block  = 'T2N    T&'
        surface_survey = 'P RR CO / DUNN, L O   64'
        → reconstructed: 'T&P RR CO'
    """
    b = str(surf_block  or "").strip()
    s = str(surf_survey or "").strip()
    # Text after the first whitespace-delimited token = start of survey name
    m = re.search(r"\S+\s+(.*)", b)
    prefix = m.group(1).strip() if m else ""
    # Strip secondary-operator suffix ("/ DUNN, L O") and trailing section refs
    s_clean = re.sub(r"\s*/.*$", "", s)        # drop "/ secondary name"
    s_clean = re.sub(r"\s+\d+\s*$", "", s_clean).strip()  # drop trailing number
    return (prefix + s_clean).strip()


def _block_tier(surf_block: str) -> str:
    """Extract the tier code (e.g. 'T2N', 'T1N') from surface_block."""
    m = re.match(r"^(\S+)", str(surf_block or "").strip())
    return m.group(1) if m else ""


def impute_survey_geometry(
    permits_gdf: gpd.GeoDataFrame,
    abstract_gdf,
    block_gdf,
    survey_gdf,
) -> gpd.GeoDataFrame:
    """Impute approximate geometry for approved_unspud permits lacking RT-14 coords.

    Three-tier fallback:
      1. Abstract centroid  (~0.5 mi) — joins on ABSTRACT_L = 'A-{num}'
      2. Block-tier centroid (~2-5 mi) — groups blocks by (survey, tier),
                                          e.g. all 'T2N' blocks of 'T&P RR CO'
      3. Survey centroid    (~5-15 mi) — centroid of the matched survey polygon

    Adds / updates:
      geometry_source  'exact' | 'abstract_centroid' | 'block_centroid' |
                       'survey_centroid' | None
    """
    out = permits_gdf.copy()

    if "geometry_source" not in out.columns:
        out["geometry_source"] = None

    # Mark permits that already have valid geometry as exact
    exact_mask = out.geometry.notna() & ~out.geometry.is_empty
    out.loc[exact_mask, "geometry_source"] = "exact"

    needs_geom = ~exact_mask
    if not needs_geom.any():
        return out

    # ── Pre-build centroid lookups ────────────────────────────────────────────

    # Abstract: key = abstract number string e.g. '4' → centroid Point
    abs_map = {}
    if abstract_gdf is not None and len(abstract_gdf) > 0:
        for _, row in abstract_gdf.iterrows():
            label = str(row.get("ABSTRACT_L", "") or "")
            m = re.match(r"A-(\d+)$", label.strip())
            if m:
                abs_map[m.group(1)] = row.geometry.centroid

    # Block: key = (tier e.g. 'T2N', survey fragment e.g. 'T&P') → centroid
    # We union all matching block polygons and take the centroid of the union
    # so we represent the whole tier band, not just one block.
    block_tier_map = {}   # (tier, survey_key) → [Polygon, ...]
    if block_gdf is not None and len(block_gdf) > 0:
        for _, row in block_gdf.iterrows():
            blo = str(row.get("LEVEL2_BLO", "") or "")
            sur = str(row.get("LEVEL1_SUR", "") or "")
            tier_m = re.search(r"(T\d+[NS])", blo)
            if not tier_m:
                continue
            tier = tier_m.group(1)
            # Use first 3+ char token of survey name as a loose key
            sur_key = next(
                (w for w in sur.split() if len(w) >= 3
                 and w.upper() not in ("THE", "AND", "FOR")),
                ""
            )
            key = (tier, sur_key.upper())
            block_tier_map.setdefault(key, []).append(row.geometry)
    # Compute union centroids once
    block_tier_centroids = {
        k: unary_union(polys).centroid
        for k, polys in block_tier_map.items()
        if polys
    }

    # Survey: key = LEVEL1_SUR string → centroid Point
    survey_map = {}
    if survey_gdf is not None and len(survey_gdf) > 0:
        for _, row in survey_gdf.iterrows():
            sur = str(row.get("LEVEL1_SUR", "") or "").strip()
            if sur:
                survey_map[sur] = row.geometry.centroid

    # ── Apply imputation ──────────────────────────────────────────────────────
    n_abstract = n_block = n_survey = 0

    for idx in out.index[needs_geom]:
        row      = out.loc[idx]
        surf_abs = str(row.get("surface_abstract", "") or "")
        surf_blk = str(row.get("surface_block",    "") or "")
        surf_sur = str(row.get("surface_survey",   "") or "")

        # Tier 1: abstract centroid
        abs_num = _parse_abstract_num(surf_abs)
        if abs_num and abs_num in abs_map:
            out.at[idx, "geometry"] = abs_map[abs_num]
            out.at[idx, "geometry_source"] = "abstract_centroid"
            n_abstract += 1
            continue

        # Tier 2: block-tier centroid
        tier = _block_tier(surf_blk)
        full_survey = _reconstruct_survey(surf_blk, surf_sur)
        if tier:
            # Build a loose key from the first meaningful token of survey name
            survey_key = next(
                (w for w in full_survey.split() if len(w) >= 3
                 and w.upper() not in ("THE", "AND", "FOR")),
                ""
            ).upper()
            match_key = (tier, survey_key)
            if match_key in block_tier_centroids:
                out.at[idx, "geometry"] = block_tier_centroids[match_key]
                out.at[idx, "geometry_source"] = "block_centroid"
                n_block += 1
                continue
            # Looser: any block group matching the tier (ignoring survey)
            tier_only = [
                c for (t, _), c in block_tier_centroids.items() if t == tier
            ]
            if tier_only:
                out.at[idx, "geometry"] = unary_union(tier_only).centroid
                out.at[idx, "geometry_source"] = "block_centroid"
                n_block += 1
                continue

        # Tier 3: survey centroid
        if full_survey and full_survey in survey_map:
            out.at[idx, "geometry"] = survey_map[full_survey]
            out.at[idx, "geometry_source"] = "survey_centroid"
            n_survey += 1
            continue
        # Tier 3 fuzzy: first meaningful token match
        if full_survey:
            token = next(
                (w for w in full_survey.split() if len(w) >= 3
                 and w.upper() not in ("THE", "AND", "FOR")),
                ""
            )
            if token:
                matches = [
                    c for k, c in survey_map.items()
                    if token.upper() in k.upper()
                ]
                if matches:
                    out.at[idx, "geometry"] = unary_union(matches).centroid
                    out.at[idx, "geometry_source"] = "survey_centroid"
                    n_survey += 1
                    continue

    total_imputed = n_abstract + n_block + n_survey
    if total_imputed > 0:
        print(f"  Imputed {total_imputed:,} permit geometries from survey description:")
        if n_abstract: print(f"    abstract_centroid : {n_abstract:>3}")
        if n_block:    print(f"    block_centroid    : {n_block:>3}")
        if n_survey:   print(f"    survey_centroid   : {n_survey:>3}")

    return gpd.GeoDataFrame(out, geometry="geometry", crs=permits_gdf.crs)


# ── Step 1: signal classification ────────────────────────────────────────────
def classify_signals(permits_gdf, today, is_pending):
    """Vectorized classification of permits into one signal_class per row."""
    n = len(permits_gdf)
    if n == 0:
        return pd.Series([], dtype="object")

    if is_pending:
        return pd.Series(["pending_approval"] * n, index=permits_gdf.index)

    cutoff = today - pd.Timedelta(days=RECENT_DAYS)

    def col_or_nat(name):
        if name in permits_gdf.columns:
            return _parse_dates(permits_gdf[name])
        return pd.Series([pd.NaT] * n, index=permits_gdf.index)

    issued = col_or_nat("permit_issued_date")
    spud   = col_or_nat("spud_date")
    final  = col_or_nat("final_update")

    # Layer in increasing specificity — later assignments win
    out = pd.Series(["historical"] * n, index=permits_gdf.index, dtype="object")
    out[(issued >= cutoff) & spud.isna()] = "approved_unspud"
    out[spud.notna() & final.isna() & (spud < cutoff)] = "drilled_no_completion"
    out[spud.notna() & (spud >= cutoff)] = "recently_drilled"
    return out


# ── Step 2: spatial enrichment (pipeline distance, well density) ─────────────
def add_pipeline_proximity(permits_gdf, pipelines_gdf):
    """Add pipeline_dist_mi + pipeline_proximity_class."""
    out = permits_gdf.copy()
    if pipelines_gdf is None or len(pipelines_gdf) == 0 or len(out) == 0:
        out["pipeline_dist_mi"] = np.nan
        out["pipeline_proximity_class"] = None
        return out

    # Project to UTM 14N for accurate Euclidean distance
    permits_proj   = out.to_crs(UTM14N)
    pipelines_proj = pipelines_gdf.to_crs(UTM14N)

    # sjoin_nearest can return >1 row per input on exact ties; dedupe.
    nearest = gpd.sjoin_nearest(
        permits_proj[["geometry"]],
        pipelines_proj[["geometry"]],
        how="left",
        distance_col="_dist_m",
    )
    nearest = nearest[~nearest.index.duplicated(keep="first")]

    dist_mi = nearest["_dist_m"] / METERS_PER_MILE
    out["pipeline_dist_mi"] = dist_mi.values
    out["pipeline_proximity_class"] = dist_mi.apply(_proximity_bucket).values
    return out


def add_well_density(permits_gdf, wells_gdf):
    """Add wells_within_1mi — count of existing wells inside the 1-mile buffer."""
    out = permits_gdf.copy()
    if wells_gdf is None or len(wells_gdf) == 0 or len(out) == 0:
        out["wells_within_1mi"] = 0
        return out

    permits_proj = out.to_crs(UTM14N)
    wells_proj   = wells_gdf.to_crs(UTM14N)

    bufs = permits_proj.copy()
    bufs["geometry"] = permits_proj.buffer(NEAR_WELL_RADIUS_M)

    joined = gpd.sjoin(
        bufs[["geometry"]],
        wells_proj[["geometry"]],
        how="left",
        predicate="contains",
    )
    counts = joined.groupby(joined.index)["index_right"].apply(
        lambda s: int(s.notna().sum())
    )
    out["wells_within_1mi"] = counts.reindex(out.index).fillna(0).astype(int).values
    return out


# ── Step 3: H3 hexgrid ───────────────────────────────────────────────────────
def build_hexgrid(permits_gdf):
    """Bin permits into H3 hexes and compute per-hex aggregates."""
    if len(permits_gdf) == 0:
        return gpd.GeoDataFrame(
            columns=["h3_id", "permit_count", "operator_count", "dominant_operator", "geometry"],
            geometry="geometry",
            crs=WGS84,
        )

    g = permits_gdf[permits_gdf.geometry.notna() & ~permits_gdf.geometry.is_empty].copy()
    if len(g) == 0:
        return gpd.GeoDataFrame(
            columns=["h3_id", "permit_count", "operator_count", "dominant_operator", "geometry"],
            geometry="geometry",
            crs=WGS84,
        )

    g["h3_id"] = [H3_CELL_FOR(pt.y, pt.x, H3_RESOLUTION) for pt in g.geometry]
    g["_one"] = 1  # sentinel for counting via pivot_table

    # Counts by signal class — pivot then merge
    sig_counts = g.pivot_table(
        index="h3_id",
        columns="signal_class",
        values="_one",
        aggfunc="sum",
        fill_value=0,
    )
    sig_counts.columns = [f"count_{c}" for c in sig_counts.columns]

    # Overall + operator metrics
    grouped = g.groupby("h3_id")
    op_col = "operator_name" if "operator_name" in g.columns else None
    agg = pd.DataFrame({
        "permit_count":   grouped.size(),
    })
    if op_col:
        agg["operator_count"] = grouped[op_col].nunique()
        agg["dominant_operator"] = grouped[op_col].agg(
            lambda s: s.dropna().value_counts().index[0] if s.dropna().size else None
        )
    else:
        agg["operator_count"] = 0
        agg["dominant_operator"] = None

    merged = agg.join(sig_counts, how="left").fillna(0)

    # Cast count columns to int
    for c in merged.columns:
        if c.startswith("count_") or c in ("permit_count", "operator_count"):
            merged[c] = merged[c].astype(int)

    # ── Recency-weighted activity score ───────────────────────────────────────
    # The hexgrid fill encodes a single quantitative measure (sequential color
    # ramp) per cartographic best practice — NOT categorical signal class, which
    # is already handled by the cluster dots and individual permit dots.
    #
    # Fresh activity (pending, approved-unspud, recently-drilled) is weighted 3x;
    # settled-but-still-recent activity (drilled-no-completion, historical —
    # all permits in this dataset are from the last ~12 months) is weighted 1x.
    # This makes hexes with new capital deployment glow brightest while still
    # showing where any permitting has occurred over the trailing year.
    def _col(name):
        return merged[name] if name in merged.columns else 0

    fresh = (
        _col("count_pending_approval")
        + _col("count_approved_unspud")
        + _col("count_recently_drilled")
    )
    settled = (
        _col("count_drilled_no_completion")
        + _col("count_historical")
    )
    merged["activity_score"] = (3 * fresh + 1 * settled).astype(int)
    merged["fresh_count"] = fresh.astype(int) if hasattr(fresh, "astype") else int(fresh)

    # Build hex polygons. h3 v4 cell_to_boundary returns ((lat, lng), …).
    def cell_to_polygon(cell_id):
        boundary = H3_BOUNDARY_FOR(cell_id)
        return Polygon([(lng, lat) for lat, lng in boundary])

    merged = merged.reset_index()
    merged["geometry"] = merged["h3_id"].apply(cell_to_polygon)
    return gpd.GeoDataFrame(merged, geometry="geometry", crs=WGS84)


# ── Step 4: production trend (per-lease, propagated to wells) ────────────────
def compute_lease_trend(leases_df):
    """Per-lease 12-month vs prior-12-month BOE delta + classification.

    Anchored at the most recent month present in the data, not wall-clock,
    so the trend is stable across re-runs and not skewed by RRC lag.
    """
    if leases_df is None or len(leases_df) == 0:
        return pd.DataFrame(columns=[
            "oil_gas_code", "district_no", "lease_no",
            "boe_recent_12mo", "boe_prior_12mo",
            "prod_trend_pct", "prod_trend_class",
        ])

    df = leases_df.copy()
    df["ym"] = pd.to_datetime(df["cycle_year_month"], format="%Y%m", errors="coerce")
    df["oil"] = pd.to_numeric(df.get("oil_prod_vol_bbl"), errors="coerce").fillna(0)
    df["gas"] = pd.to_numeric(df.get("gas_prod_vol_mcf"), errors="coerce").fillna(0)
    df["boe"] = df["oil"] + df["gas"] / 6.0  # standard 6 Mcf = 1 BOE

    anchor = df["ym"].max()
    if pd.isna(anchor):
        return pd.DataFrame(columns=[
            "oil_gas_code", "district_no", "lease_no",
            "boe_recent_12mo", "boe_prior_12mo",
            "prod_trend_pct", "prod_trend_class",
        ])

    cutoff_12 = anchor - pd.DateOffset(months=12)
    cutoff_24 = anchor - pd.DateOffset(months=24)
    keys = ["oil_gas_code", "district_no", "lease_no"]

    recent = (df[df["ym"] > cutoff_12]
              .groupby(keys)["boe"].sum()
              .rename("boe_recent_12mo"))
    prior  = (df[(df["ym"] > cutoff_24) & (df["ym"] <= cutoff_12)]
              .groupby(keys)["boe"].sum()
              .rename("boe_prior_12mo"))
    trend = pd.concat([recent, prior], axis=1).fillna(0).reset_index()

    def classify(row):
        r, p = row["boe_recent_12mo"], row["boe_prior_12mo"]
        if r == 0 and p == 0:           return "inactive"
        if p == 0 and r > 0:            return "new"
        delta = (r - p) / p
        if delta > TREND_GROW_THRESHOLD:    return "growing"
        if delta < TREND_DECLINE_THRESHOLD: return "declining"
        return "flat"

    trend["prod_trend_class"] = trend.apply(classify, axis=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        trend["prod_trend_pct"] = (
            (trend["boe_recent_12mo"] - trend["boe_prior_12mo"])
            / trend["boe_prior_12mo"].replace(0, np.nan)
        )
    return trend


def build_lease_descriptors(leases_df):
    """One row per lease key with descriptive names (operator, lease, field).

    The lease-cycle table repeats these per month; we take the most recent
    non-blank value per lease so the well popup can show who operates it and
    what field it's in. Returns a DataFrame keyed by oil_gas_code+district_no+lease_no.
    """
    keys = ["oil_gas_code", "district_no", "lease_no"]
    if leases_df is None or len(leases_df) == 0:
        return pd.DataFrame(columns=keys + ["lease_name", "operator_name", "field_name"])

    df = leases_df.copy()
    # Sort so the most recent cycle is last, then keep last non-null per key
    if "cycle_year_month" in df.columns:
        df = df.sort_values("cycle_year_month")

    desc_cols = [c for c in ["lease_name", "operator_name", "field_name"] if c in df.columns]
    if not desc_cols:
        return pd.DataFrame(columns=keys + ["lease_name", "operator_name", "field_name"])

    # Replace blanks with NaN so "last valid" skips them
    for c in desc_cols:
        df[c] = df[c].replace("", np.nan)

    agg = df.groupby(keys)[desc_cols].agg("last").reset_index()
    return agg


def enrich_wells_with_trend(wells_gdf, prod_wells_df, lease_trend_df, lease_desc_df=None):
    """Join lease-level production data onto wells via the prod_wells bridge.

    Adds both the trend classification AND descriptive attributes so the well
    popup is informative. The well shapefile (well317s) is geometry-only —
    operator, lease, field, status, and BOE all come from the production tables
    joined here on api_number.

    Columns added:
        prod_trend_class, prod_trend_pct        (trajectory)
        boe_recent_12mo, boe_prior_12mo         (magnitude)
        prod_operator_name, prod_lease_name     (descriptive — from leases table)
        prod_field_name                         (descriptive — from leases table)
        well_14b2_status                        (descriptive — from wells table)
    """
    out = wells_gdf.copy()
    out["prod_trend_class"] = "unknown"
    out["prod_trend_pct"] = np.nan
    out["boe_recent_12mo"] = np.nan
    out["boe_prior_12mo"] = np.nan
    out["prod_operator_name"] = None
    out["prod_lease_name"] = None
    out["prod_field_name"] = None
    out["well_14b2_status"] = None

    if prod_wells_df is None or lease_trend_df is None or len(lease_trend_df) == 0:
        return out

    api_col = _find_api_column(out)
    if api_col is None:
        print("  WARN: no API column found on wells layer — trend skipped",
              file=sys.stderr)
        return out

    print(f"  Using API column '{api_col}' on well317s.parquet")
    out["api_number"] = _normalize_api(out[api_col])

    keys = ["oil_gas_code", "district_no", "lease_no"]

    # Start from prod_wells (carries api_number + 14B2 status + lease keys),
    # join the per-lease trend (BOE magnitude + trajectory),
    # then join the per-lease descriptors (operator/lease/field names).
    merged_bridge = prod_wells_df.merge(lease_trend_df, on=keys, how="left")
    if lease_desc_df is not None and len(lease_desc_df) > 0:
        merged_bridge = merged_bridge.merge(lease_desc_df, on=keys, how="left")

    bridge_cols = ["api_number", "prod_trend_class", "prod_trend_pct",
                   "boe_recent_12mo", "boe_prior_12mo"]

    rename_map = {
        "operator_name":         "prod_operator_name",
        "lease_name":            "prod_lease_name",
        "field_name":            "prod_field_name",
        "well_14b2_status_code": "well_14b2_status",
    }
    for src, dst in rename_map.items():
        if src in merged_bridge.columns:
            merged_bridge[dst] = merged_bridge[src]
            bridge_cols.append(dst)

    bridge = (merged_bridge[bridge_cols]
              .dropna(subset=["api_number"])
              .drop_duplicates("api_number"))

    drop_existing = [c for c in bridge.columns if c != "api_number" and c in out.columns]
    out = out.drop(columns=drop_existing).merge(bridge, on="api_number", how="left")
    out["prod_trend_class"] = out["prod_trend_class"].fillna("unknown")
    return out


def build_production_hexgrid(wells_enriched):
    """Bin production-enriched wells into H3 hexes — BOE magnitude + trend.

    This is a SEPARATE layer from the permit hexgrid. Where the permit hexgrid
    answers "where is new capital being committed?", this answers "where is
    production actually coming from, and is it growing or declining?"

    Per-hex measures:
        boe_recent_12mo   sum of recent-12-month BOE across wells in the hex
        boe_prior_12mo    sum of prior-12-month BOE (for trajectory context)
        well_count        number of producing wells in the hex
        prod_trend_majority  majority-vote trend among wells (growing/declining/…)
        boe_score         log-scaled 0–100 magnitude for the choropleth ramp

    The fill encodes boe_score (magnitude). Trajectory lives in the popup.
    """
    empty_cols = ["h3_id", "well_count", "boe_recent_12mo", "boe_prior_12mo",
                  "prod_trend_majority", "boe_score", "geometry"]
    if wells_enriched is None or len(wells_enriched) == 0:
        return gpd.GeoDataFrame(columns=empty_cols, geometry="geometry", crs=WGS84)

    g = wells_enriched[
        wells_enriched.geometry.notna() & ~wells_enriched.geometry.is_empty
    ].copy()

    # Only wells that actually have production data contribute to this grid
    if "boe_recent_12mo" in g.columns:
        g = g[g["boe_recent_12mo"].notna() & (g["boe_recent_12mo"] > 0)].copy()

    if len(g) == 0:
        return gpd.GeoDataFrame(columns=empty_cols, geometry="geometry", crs=WGS84)

    g["h3_id"] = [H3_CELL_FOR(pt.y, pt.x, H3_RESOLUTION) for pt in g.geometry]

    grouped = g.groupby("h3_id")
    agg = pd.DataFrame({
        "well_count":      grouped.size(),
        "boe_recent_12mo": grouped["boe_recent_12mo"].sum(),
        "boe_prior_12mo":  grouped["boe_prior_12mo"].sum()
                           if "boe_prior_12mo" in g.columns else 0,
    })

    # Majority-vote trend class per hex (ignore 'unknown' if real signal exists)
    def majority_trend(s):
        vc = s[s != "unknown"].value_counts()
        if len(vc) > 0:
            return vc.index[0]
        return "unknown"

    if "prod_trend_class" in g.columns:
        agg["prod_trend_majority"] = grouped["prod_trend_class"].agg(majority_trend)
    else:
        agg["prod_trend_majority"] = "unknown"

    # Dominant operator per hex (most producing wells)
    if "prod_operator_name" in g.columns:
        agg["dominant_operator"] = grouped["prod_operator_name"].agg(
            lambda s: s.dropna().value_counts().index[0] if s.dropna().size else None
        )

    agg = agg.reset_index()

    # ── Magnitude → log-scaled 0–100 score ────────────────────────────────────
    # Production is heavily right-skewed (a few giant leases dominate), so a
    # raw-linear ramp would make all but the top hex invisible. log1p compresses
    # the long tail so mid-tier producers remain distinguishable.
    boe = agg["boe_recent_12mo"].clip(lower=0)
    log_boe = np.log1p(boe)
    max_log = log_boe.max()
    agg["boe_score"] = (
        (log_boe / max_log * 100).round().astype(int) if max_log > 0 else 0
    )

    # Build hex polygons (same convention as permit hexgrid)
    def cell_to_polygon(cell_id):
        boundary = H3_BOUNDARY_FOR(cell_id)
        return Polygon([(lng, lat) for lat, lng in boundary])

    agg["geometry"] = agg["h3_id"].apply(cell_to_polygon)
    agg["well_count"] = agg["well_count"].astype(int)
    agg["boe_recent_12mo"] = agg["boe_recent_12mo"].round().astype("int64")
    if "boe_prior_12mo" in agg.columns:
        agg["boe_prior_12mo"] = agg["boe_prior_12mo"].round().astype("int64")

    return gpd.GeoDataFrame(agg, geometry="geometry", crs=WGS84)


# ── Step 5: operator rollup ──────────────────────────────────────────────────
def build_operator_rollup(permits):
    """Per-operator activity rollup. Non-spatial. Pandas DataFrame, not GDF."""
    if len(permits) == 0 or "operator_name" not in permits.columns:
        return pd.DataFrame(columns=[
            "operator_name", "permit_count", "recent_count",
            "new_entrant", "avg_permit_to_spud_days",
        ])

    today = _today()
    cutoff_recent = today - pd.Timedelta(days=RECENT_DAYS)
    cutoff_history = today - pd.Timedelta(days=NEW_ENTRANT_LOOKBACK_DAYS)

    df = permits.copy()
    df["_issued"] = _parse_dates(df.get("permit_issued_date"))
    df["_spud"]   = _parse_dates(df.get("spud_date"))
    df["_lag"]    = (df["_spud"] - df["_issued"]).dt.days

    by_op = df.groupby("operator_name", dropna=False)

    rollup = pd.DataFrame({
        "permit_count": by_op.size(),
        "recent_count": by_op["_issued"].apply(lambda s: int((s >= cutoff_recent).sum())),
    })

    # Signal-class counts
    df["_one"] = 1
    sig = df.pivot_table(
        index="operator_name", columns="signal_class",
        values="_one", aggfunc="sum", fill_value=0,
    )
    sig.columns = [f"count_{c}" for c in sig.columns]
    rollup = rollup.join(sig, how="left").fillna(0)
    for c in rollup.columns:
        if c.startswith("count_") or c in ("permit_count", "recent_count"):
            rollup[c] = rollup[c].astype(int)

    # New entrant: recent activity, but nothing in the prior historical window
    hist_active = set(
        df[(df["_issued"] >= cutoff_history) & (df["_issued"] < cutoff_recent)]
        ["operator_name"].dropna().unique()
    )
    rollup["had_historical_activity"] = rollup.index.isin(hist_active)
    rollup["new_entrant"] = (
        (rollup["recent_count"] > 0) & (~rollup["had_historical_activity"])
    )

    # Average permit-to-spud lag (days)
    lag = df[df["_lag"].notna()].groupby("operator_name")["_lag"].mean()
    rollup["avg_permit_to_spud_days"] = rollup.index.map(lag).round(1)

    # Districts touched
    if "permit_district" in df.columns:
        rollup["districts"] = by_op["permit_district"].apply(
            lambda s: sorted(set(x for x in s.dropna() if x))
        )

    return rollup.reset_index()


# ── Step 6: write metadata ───────────────────────────────────────────────────
def write_meta(web_dir, fips, layers, signal_dist, bbox):
    meta = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "fips": fips,
        "county": "Martin",
        "bbox": list(bbox) if bbox is not None else None,
        "layer_counts": layers,
        "signal_class_distribution": signal_dist,
        "config": {
            "recent_days": RECENT_DAYS,
            "new_entrant_lookback_days": NEW_ENTRANT_LOOKBACK_DAYS,
            "h3_resolution": H3_RESOLUTION,
            "near_well_radius_mi": NEAR_WELL_RADIUS_MI,
        },
    }
    (web_dir / "martin_meta.json").write_text(json.dumps(meta, indent=2))


def write_operators_json(web_dir, rollup_df):
    """Trim and write the operator rollup as JSON for the frontend filter UI."""
    if "districts" in rollup_df.columns:
        rollup_df = rollup_df.copy()
        rollup_df["districts"] = rollup_df["districts"].apply(
            lambda x: list(x) if isinstance(x, (list, tuple, set)) else []
        )
    # Replace NaN with None so JSON is clean
    payload = json.loads(rollup_df.to_json(orient="records"))
    (web_dir / "operators.json").write_text(json.dumps(payload, indent=2))


# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--processed-dir", default="data/processed")
    parser.add_argument("--web-dir",       default="data/web")
    parser.add_argument("--fips",          default=MARTIN_FIPS)
    args = parser.parse_args()

    processed = Path(args.processed_dir)
    web = Path(args.web_dir)
    web.mkdir(parents=True, exist_ok=True)

    today = _today()
    print(f"Anchor date: {today.date()}")
    print(f"Recent window: {RECENT_DAYS} days")
    print()

    # ── Load permits ─────────────────────────────────────────────────────────
    print("[1/7] Loading & filtering permits")
    permits_path = processed / "drillingPermits.parquet"
    if not permits_path.exists():
        print(f"  ERROR: '{permits_path}' not found. Run drillingPermits.sh first.",
              file=sys.stderr)
        sys.exit(1)

    all_permits = gpd.read_parquet(permits_path)
    martin_permits, hit_col = _filter_to_county(all_permits, args.fips)
    print(f"  {len(all_permits):,} statewide → {len(martin_permits):,} Martin "
          f"(filter via '{hit_col}')")

    pending_path = processed / "drillingPermitsPending.parquet"
    if pending_path.exists():
        all_pending = gpd.read_parquet(pending_path)
        martin_pending, hit_col_p = _filter_to_county(all_pending, args.fips)
        print(f"  {len(all_pending):,} statewide pending → "
              f"{len(martin_pending):,} Martin (filter via '{hit_col_p}')")
    else:
        martin_pending = gpd.GeoDataFrame(geometry=[], crs=WGS84)
        print("  No pending file — skipping pending permits")

    # ── Signal classification ────────────────────────────────────────────────
    print("\n[2/7] Classifying permit signals")
    martin_permits["signal_class"] = classify_signals(martin_permits, today, is_pending=False)
    martin_permits["is_pending"]   = False
    if len(martin_pending) > 0:
        martin_pending["signal_class"] = classify_signals(martin_pending, today, is_pending=True)
        martin_pending["is_pending"]   = True

    # Union accumulated + pending. They have different schemas, so we
    # outer-concat and let missing columns be NaN.
    if len(martin_pending) > 0:
        combined = pd.concat([martin_permits, martin_pending], ignore_index=True, sort=False)
        combined = gpd.GeoDataFrame(combined, geometry="geometry", crs=WGS84)
    else:
        combined = martin_permits.copy()

    print("  Signal distribution:")
    dist = combined["signal_class"].value_counts().to_dict()
    for k, v in dist.items():
        print(f"    {k:<24} {v:>5,}")

    # ── Load context layers ──────────────────────────────────────────────────
    print("\n[3/7] Loading context layers (pipelines, wells, production)")

    pipelines_path = processed / "martinPipelines.parquet"
    wells_path     = processed / "well317s.parquet"
    prod_leases_path = processed / "martinProduction_leases.parquet"
    prod_wells_path  = processed / "martinProduction_wells.parquet"

    pipelines_gdf  = gpd.read_parquet(pipelines_path)  if pipelines_path.exists() else None
    wells_gdf      = gpd.read_parquet(wells_path)      if wells_path.exists()     else None
    prod_leases_df = pd.read_parquet(prod_leases_path) if prod_leases_path.exists() else None
    prod_wells_df  = pd.read_parquet(prod_wells_path)  if prod_wells_path.exists()  else None

    for name, obj in [
        ("Pipelines",          pipelines_gdf),
        ("Wells",              wells_gdf),
        ("Production leases",  prod_leases_df),
        ("Production wells",   prod_wells_df),
    ]:
        if obj is None:
            print(f"  SKIP: {name} not found — corresponding signals will be null")
        else:
            print(f"  {name:<20} {len(obj):>7,} rows")

    # ── Spatial enrichment of permits ────────────────────────────────────────
    print("\n[4/7] Spatial enrichment (pipeline proximity + well density)")
    geom_count_before = combined.geometry.notna().sum()
    print(f"  Permits with geometry: {geom_count_before:,}/{len(combined):,}")

    enriched = add_pipeline_proximity(combined, pipelines_gdf)
    enriched = add_well_density(enriched, wells_gdf)

    # ── Survey geometry imputation ────────────────────────────────────────────
    # For approved_unspud and pending_approval permits that lack RT-14 surface
    # location records, impute an approximate location from the legal
    # description (abstract number, block tier, survey name).
    abstract_path_s = processed / "martinSurvey_lyr_abstract.parquet"
    block_path_s    = processed / "martinSurvey_lyr_block.parquet"
    survey_path_s   = processed / "martinSurvey_lyr_survey.parquet"
    abstract_s = gpd.read_parquet(abstract_path_s) if abstract_path_s.exists() else None
    block_s    = gpd.read_parquet(block_path_s)    if block_path_s.exists()    else None
    survey_s   = gpd.read_parquet(survey_path_s)   if survey_path_s.exists()   else None

    if any(x is not None for x in [abstract_s, block_s, survey_s]):
        enriched = impute_survey_geometry(enriched, abstract_s, block_s, survey_s)

    geom_after = enriched.geometry.notna().sum()
    print(f"  Permits with geometry after imputation: {geom_after:,}/{len(enriched):,}")

    if "pipeline_proximity_class" in enriched.columns:
        prox_dist = enriched["pipeline_proximity_class"].value_counts().to_dict()
        if prox_dist:
            print("  Pipeline proximity:")
            for k, v in prox_dist.items():
                print(f"    {str(k):<10} {v:>5,}")

    # ── Signal priority — controls tippecanoe drop order at low zoom ────────────
    # When tippecanoe thins features at low zoom, it drops from the bottom of
    # the sort. By ordering descending on signal_priority, historical permits
    # (priority 1) are dropped first and pending/approved (priority 4-5) always
    # survive to the lowest zoom tile. This ensures every signal class is
    # visible county-wide even when zoomed out.
    SIGNAL_PRIORITY = {
        "pending_approval":      5,
        "approved_unspud":       4,
        "recently_drilled":      3,
        "drilled_no_completion": 2,
        "historical":            1,
    }
    enriched["signal_priority"] = (
        enriched["signal_class"].map(SIGNAL_PRIORITY).fillna(0).astype(int)
    )

    # ── Hexgrid ──────────────────────────────────────────────────────────────
    print(f"\n[5/7] Building H3 hexgrid (resolution {H3_RESOLUTION})")
    hexgrid = build_hexgrid(enriched)
    print(f"  {len(hexgrid):,} hexes with at least one permit")
    if len(hexgrid) > 0 and "activity_score" in hexgrid.columns:
        scores = hexgrid["activity_score"]
        qs = scores.quantile([0.2, 0.4, 0.6, 0.8, 1.0]).astype(int).tolist()
        print(f"  Activity score range: {int(scores.min())}–{int(scores.max())}  "
              f"(quintile breaks: {qs})")

    # ── Production trend → wells ─────────────────────────────────────────────
    print("\n[6/7] Computing production trend & enriching wells")
    lease_trend = compute_lease_trend(prod_leases_df)
    print(f"  {len(lease_trend):,} leases with trend computed")

    lease_desc = build_lease_descriptors(prod_leases_df)

    if wells_gdf is not None:
        wells_enriched = enrich_wells_with_trend(
            wells_gdf, prod_wells_df, lease_trend, lease_desc
        )
        trend_dist = wells_enriched["prod_trend_class"].value_counts().to_dict()
        print("  Well trend distribution:")
        for k, v in trend_dist.items():
            print(f"    {k:<12} {v:>5,}")
        n_named = wells_enriched["prod_operator_name"].notna().sum() \
            if "prod_operator_name" in wells_enriched.columns else 0
        print(f"  Wells with operator name: {n_named:,}")
    else:
        wells_enriched = gpd.GeoDataFrame(geometry=[], crs=WGS84)

    # ── Production hexgrid (separate layer — BOE magnitude + trend) ───────────
    print("  Building production hexgrid (BOE magnitude + trajectory)")
    prod_hexgrid = build_production_hexgrid(wells_enriched)
    if len(prod_hexgrid) > 0:
        bsc = prod_hexgrid["boe_score"]
        print(f"  {len(prod_hexgrid):,} production hexes  "
              f"|  BOE score range {int(bsc.min())}–{int(bsc.max())}")
        tmaj = prod_hexgrid["prod_trend_majority"].value_counts().to_dict()
        print(f"  Hex trend majority: {tmaj}")
    else:
        print("  No production hexes (no wells with BOE data)")

    # ── Operator rollup ──────────────────────────────────────────────────────
    print("\n[7/7] Operator rollup")
    operators = build_operator_rollup(enriched)
    n_new = int(operators["new_entrant"].sum()) if "new_entrant" in operators.columns else 0
    print(f"  {len(operators):,} operators  |  {n_new} new entrants")

    # ── Write outputs ────────────────────────────────────────────────────────
    print("\nWriting outputs…")
    out_permits  = processed / "martinSignals_permits.parquet"
    out_wells    = processed / "martinSignals_wells.parquet"
    out_hex      = processed / "martinSignals_hexgrid.parquet"
    out_prod_hex = processed / "martinSignals_hexgrid_production.parquet"
    out_ops      = processed / "martinSignals_operators.parquet"

    enriched = _coerce_mixed_objects(enriched)
    enriched.to_parquet(out_permits)
    print(f"  → {out_permits}  ({len(enriched):,} rows)")

    if len(wells_enriched) > 0:
        wells_enriched.to_parquet(out_wells)
        print(f"  → {out_wells}  ({len(wells_enriched):,} rows)")

    if len(hexgrid) > 0:
        hexgrid.to_parquet(out_hex)
        print(f"  → {out_hex}  ({len(hexgrid):,} rows)")

    if len(prod_hexgrid) > 0:
        prod_hexgrid.to_parquet(out_prod_hex)
        print(f"  → {out_prod_hex}  ({len(prod_hexgrid):,} rows)")

    operators.to_parquet(out_ops)
    print(f"  → {out_ops}  ({len(operators):,} rows)")

    # Web-side outputs
    write_operators_json(web, operators)
    print(f"  → {web / 'operators.json'}")

    # Signal class cluster centroids for low-zoom map display.
    # Groups permits by signal_class + rounded coordinate (1 km precision) so
    # stacked imputed centroids collapse to a single dot per cluster.
    # Excludes historical — too many, not the point of the low-zoom view.
    # Output: one GeoJSON feature per signal class per geographic cluster,
    # colored by class and labeled with count.
    non_hist = enriched[
        enriched.geometry.notna()
        & ~enriched.geometry.is_empty
        & (enriched["signal_class"] != "historical")
    ].copy()
    non_hist["_lat_r"] = non_hist.geometry.y.round(2)
    non_hist["_lon_r"] = non_hist.geometry.x.round(2)

    centroid_features = []
    for (cls, lat_r, lon_r), grp in non_hist.groupby(
        ["signal_class", "_lat_r", "_lon_r"], sort=False
    ):
        cluster_centroid = unary_union(grp.geometry.values).centroid
        centroid_features.append({
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [round(cluster_centroid.x, 4), round(cluster_centroid.y, 4)],
            },
            "properties": {
                "signal_class": cls,
                "count": len(grp),
            },
        })

    centroids_path = web / "signal_centroids.json"
    with open(centroids_path, "w") as f:
        json.dump({"type": "FeatureCollection", "features": centroid_features}, f)
    print(f"  → {centroids_path}  ({len(centroid_features)} cluster dots)")

    # Bounding box from enriched permits + wells
    bbox_layers = [enriched, wells_enriched]
    bbox = None
    for layer in bbox_layers:
        if len(layer) > 0 and layer.geometry.notna().any():
            tb = layer.total_bounds  # (minx, miny, maxx, maxy)
            if bbox is None:
                bbox = tb
            else:
                bbox = (
                    min(bbox[0], tb[0]), min(bbox[1], tb[1]),
                    max(bbox[2], tb[2]), max(bbox[3], tb[3]),
                )

    write_meta(
        web_dir=web,
        fips=args.fips,
        layers={
            "permits_signals": int(len(enriched)),
            "wells_signals":   int(len(wells_enriched)),
            "hexgrid":         int(len(hexgrid)),
            "operators":       int(len(operators)),
        },
        signal_dist=dist,
        bbox=bbox,
    )
    print(f"  → {web / 'martin_meta.json'}")

    print("\nDone.")


if __name__ == "__main__":
    main()