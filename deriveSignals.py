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
import sys
import warnings
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import geopandas as gpd
from shapely.geometry import Polygon

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

    Two known data-quality issues are handled here:

    1. Format shift in parse_permits.py. The upstream parser slices each
       8-char raw date as CCYYMMDD ('20260528' → '2026-05-28'), but the
       RRC daf420 master file actually emits some date fields as YYMMDD??
       ('26052800' = YY=26, MM=05, DD=28, then two trailing junk chars).
       parse_permits.py mis-slices these into 'YYYY-DD-00' where the YYYY
       is really (YY year + MM month) glued together. Detect and reconstruct:
           '2605-28-00' → '2026-05-28'
       Correctly-formatted CCYYMMDD records pass through unchanged.

       Long-term fix is upstream in parse_permits.py — see BUILD docs.

    2. Year typos (e.g. '2408-06-20' instead of '2024-06-20') overflow
       pandas datetime[ns] resolution during arithmetic. Coerce out-of-range
       years (outside [1900, 2100]) to NaT.
    """
    if not isinstance(s, pd.Series):
        s = pd.Series(s)
    if len(s) == 0:
        return pd.to_datetime(s, errors="coerce")

    if s.dtype == object or pd.api.types.is_string_dtype(s):
        s_str = s.astype(str).str.strip()

        # Repair (1): 'YYYY-DD-00' (where YYYY = YY+MM glued together) → 'YYYY-MM-DD'.
        # Captures: (1)=YY year, (2)=MM month, (3)=DD day; prepend '20' century.
        s_str = s_str.str.replace(
            r"^(\d{2})(\d{2})-(\d{2})-00$",
            r"20\1-\2-\3",
            regex=True,
        )

        # Repair (2): year filter — anything outside [1900, 2100] is a typo
        years = pd.to_numeric(s_str.str.slice(0, 4), errors="coerce")
        in_range = years.between(1900, 2100, inclusive="both").fillna(False)
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


def _filter_to_county(gdf, fips):
    """Try every county-code column we know about, return first hit."""
    candidates = [
        "permit_county_code", "county_code", "onshore_county",
        "wb_county_code", "api_county_code",
    ]
    for col in candidates:
        if col in gdf.columns:
            mask = gdf[col].astype(str).str.strip() == fips
            if mask.any():
                return gdf[mask].copy(), col
    return gdf.iloc[0:0].copy(), None


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


def enrich_wells_with_trend(wells_gdf, prod_wells_df, lease_trend_df):
    """Join lease-level trend onto wells via the prod_wells (api_number) bridge."""
    out = wells_gdf.copy()
    out["prod_trend_class"] = "unknown"
    out["prod_trend_pct"] = np.nan

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
    bridge = (prod_wells_df.merge(lease_trend_df, on=keys, how="left")
              [["api_number", "prod_trend_class", "prod_trend_pct"]]
              .dropna(subset=["api_number"])
              .drop_duplicates("api_number"))

    out = out.drop(columns=["prod_trend_class", "prod_trend_pct"]).merge(
        bridge, on="api_number", how="left"
    )
    out["prod_trend_class"] = out["prod_trend_class"].fillna("unknown")
    return out


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

    if "pipeline_proximity_class" in enriched.columns:
        prox_dist = enriched["pipeline_proximity_class"].value_counts().to_dict()
        if prox_dist:
            print("  Pipeline proximity:")
            for k, v in prox_dist.items():
                print(f"    {str(k):<10} {v:>5,}")

    # ── Hexgrid ──────────────────────────────────────────────────────────────
    print(f"\n[5/7] Building H3 hexgrid (resolution {H3_RESOLUTION})")
    hexgrid = build_hexgrid(enriched)
    print(f"  {len(hexgrid):,} hexes with at least one permit")

    # ── Production trend → wells ─────────────────────────────────────────────
    print("\n[6/7] Computing production trend & enriching wells")
    lease_trend = compute_lease_trend(prod_leases_df)
    print(f"  {len(lease_trend):,} leases with trend computed")

    if wells_gdf is not None:
        wells_enriched = enrich_wells_with_trend(wells_gdf, prod_wells_df, lease_trend)
        trend_dist = wells_enriched["prod_trend_class"].value_counts().to_dict()
        print("  Well trend distribution:")
        for k, v in trend_dist.items():
            print(f"    {k:<12} {v:>5,}")
    else:
        wells_enriched = gpd.GeoDataFrame(geometry=[], crs=WGS84)

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
    out_ops      = processed / "martinSignals_operators.parquet"

    enriched.to_parquet(out_permits)
    print(f"  → {out_permits}  ({len(enriched):,} rows)")

    if len(wells_enriched) > 0:
        wells_enriched.to_parquet(out_wells)
        print(f"  → {out_wells}  ({len(wells_enriched):,} rows)")

    if len(hexgrid) > 0:
        hexgrid.to_parquet(out_hex)
        print(f"  → {out_hex}  ({len(hexgrid):,} rows)")

    operators.to_parquet(out_ops)
    print(f"  → {out_ops}  ({len(operators):,} rows)")

    # Web-side outputs
    write_operators_json(web, operators)
    print(f"  → {web / 'operators.json'}")

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