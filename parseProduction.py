#!/usr/bin/env python3
"""
parseProduction.py
==================
Extracts county-specific oil and gas production data from the Texas RRC
Production Data Query (PDQ) dump (PDQ_DSV.zip).

County selection:
  --counties "Martin, Midland, Howard"
    Resolves county names to RRC county codes using GP_COUNTY_DATA_TABLE.dsv
    inside the ZIP. Case-insensitive, partial-match supported.

  --list-counties
    Print all available counties from the ZIP and exit.

Three tables are read from the ZIP via streaming — no full extraction to disk:

  GP_COUNTY_DATA_TABLE.dsv            → county name/code lookup
  OG_WELL_COMPLETION_DATA_TABLE.dsv   → {county}Production_wells.parquet
  OG_LEASE_CYCLE_DATA_TABLE.dsv       → {county}Production_leases.parquet

Each selected county gets its own pair of Parquet files, named after the
county in camelCase (e.g. martinProduction_wells.parquet).

Join path at analysis time:
  {county}Production_leases
    ──[oil_gas_code + district_no + lease_no]──▶  {county}Production_wells
    ──[api_number]──▶  well317 or drillingPermits geometry

Usage:
    python parseProduction.py --list-counties
    python parseProduction.py --counties "Martin"
    python parseProduction.py --counties "Martin, Midland, Howard"
    python parseProduction.py --counties "Martin" --raw-dir data/raw --processed-dir data/processed
"""

import argparse
import csv
import io
import sys
import zipfile
from pathlib import Path

import pandas as pd

ZIP_FILENAME = "PDQ_DSV.zip"

GP_COUNTY_DSV       = "GP_COUNTY_DATA_TABLE.dsv"
WELL_COMPLETION_DSV = "OG_WELL_COMPLETION_DATA_TABLE.dsv"
LEASE_CYCLE_DSV     = "OG_LEASE_CYCLE_DATA_TABLE.dsv"

PROGRESS_INTERVAL = 500_000


# ── Streaming helper ──────────────────────────────────────────────────────────

def stream_dsv(zf: zipfile.ZipFile, dsv_name: str):
    """Yield one dict per data row from a }-delimited DSV file inside a ZIP."""
    with zf.open(dsv_name) as raw:
        text = io.TextIOWrapper(raw, encoding="latin-1", newline="")
        reader = csv.reader(text, delimiter="}")
        headers = None
        for row in reader:
            if headers is None:
                headers = [h.strip() for h in row]
                continue
            if len(row) != len(headers):
                continue
            yield {headers[i]: row[i].strip() for i in range(len(headers))}


def safe_int(val: str):
    val = val.strip() if val else ""
    if not val:
        return None
    try:
        return int(val)
    except ValueError:
        return None


def to_camel(name: str) -> str:
    """Convert a county name to camelCase for filenames.
    'MARTIN' → 'martin', 'SAN PATRICIO' → 'sanPatricio'
    """
    parts = name.strip().lower().split()
    if not parts:
        return "unknown"
    return parts[0] + "".join(p.capitalize() for p in parts[1:])


# ── County lookup ─────────────────────────────────────────────────────────────

def load_county_map(zf: zipfile.ZipFile) -> dict[str, dict]:
    """
    Read GP_COUNTY and return a dict keyed by uppercase county name:
      { "MARTIN": {"code": "317", "fips": "317", "district": "10", ...}, ... }
    """
    counties = {}
    for row in stream_dsv(zf, GP_COUNTY_DSV):
        name = row.get("COUNTY_NAME", "").strip().upper()
        if not name:
            continue
        counties[name] = {
            "code":          row.get("COUNTY_NO", "").strip(),
            "fips":          row.get("COUNTY_FIPS_CODE", "").strip(),
            "district_no":   row.get("DISTRICT_NO", "").strip(),
            "district_name": row.get("DISTRICT_NAME", "").strip(),
            "onshore":       row.get("ON_SHORE_FLAG", "").strip(),
        }
    return counties


def resolve_counties(
    county_map: dict[str, dict],
    user_input: str,
) -> list[tuple[str, str]]:
    """
    Resolve a comma-separated string of county names to (name, code) pairs.
    Case-insensitive. Supports partial matching (e.g. 'martin' matches 'MARTIN').
    Exits with an error if any name is ambiguous or not found.

    Returns list of (canonical_name, county_code) sorted alphabetically.
    """
    results = []
    for raw in user_input.split(","):
        query = raw.strip().upper()
        if not query:
            continue

        # Exact match first
        if query in county_map:
            results.append((query, county_map[query]["code"]))
            continue

        # Partial match: find all names that contain the query
        matches = [(name, info["code"]) for name, info in county_map.items()
                   if query in name]

        if len(matches) == 1:
            results.append(matches[0])
        elif len(matches) == 0:
            print(f"\nError: No county found matching '{raw.strip()}'.", file=sys.stderr)
            print("  Use --list-counties to see all available names.", file=sys.stderr)
            sys.exit(1)
        else:
            print(f"\nError: '{raw.strip()}' is ambiguous — matches {len(matches)} counties:",
                  file=sys.stderr)
            for name, code in sorted(matches):
                print(f"    {name} (code {code})", file=sys.stderr)
            print("  Be more specific.", file=sys.stderr)
            sys.exit(1)

    if not results:
        print("Error: No counties specified.", file=sys.stderr)
        sys.exit(1)

    # Deduplicate, preserving order
    seen = set()
    unique = []
    for name, code in results:
        if code not in seen:
            seen.add(code)
            unique.append((name, code))

    return sorted(unique)


# ── Table parsers ─────────────────────────────────────────────────────────────

def parse_well_completions(
    zf: zipfile.ZipFile,
    county_codes: set[str],
) -> tuple[dict[str, set], dict[str, list]]:
    """
    Stream OG_WELL_COMPLETION, keep rows whose API_COUNTY_CODE is in county_codes.

    Returns
    -------
    leases_by_county : { county_code: set of (oil_gas_code, district_no, lease_no) }
    records_by_county : { county_code: list of dicts }
    """
    leases_by_county:  dict[str, set]  = {c: set() for c in county_codes}
    records_by_county: dict[str, list] = {c: []    for c in county_codes}
    total_scanned = 0

    print(f"  Streaming '{WELL_COMPLETION_DSV}'...")

    for row in stream_dsv(zf, WELL_COMPLETION_DSV):
        total_scanned += 1
        county = row.get("API_COUNTY_CODE", "")
        if county not in county_codes:
            continue

        og   = row.get("OIL_GAS_CODE", "")
        dist = row.get("DISTRICT_NO", "")
        lse  = row.get("LEASE_NO", "")
        leases_by_county[county].add((og, dist, lse))

        api_county = county
        api_unique = row.get("API_UNIQUE_NO", "").zfill(5)
        api_number = f"{api_county}{api_unique}" if api_county and api_unique else None

        records_by_county[county].append({
            "oil_gas_code":           og,
            "district_no":            dist,
            "lease_no":               lse,
            "well_no":                row.get("WELL_NO", ""),
            "api_county_code":        api_county,
            "api_unique_no":          row.get("API_UNIQUE_NO", ""),
            "api_number":             api_number,
            "county_name":            row.get("COUNTY_NAME", ""),
            "district_name":          row.get("DISTRICT_NAME", ""),
            "well_root_no":           row.get("WELL_ROOT_NO", ""),
            "well_14b2_status_code":  row.get("WELL_14B2_STATUS_CODE", ""),
            "wellbore_location_code": row.get("WELLBORE_LOCATION_CODE", ""),
            "wellbore_shutin_dt":     row.get("WELLBORE_SHUTIN_DT", "") or None,
            "well_shutin_dt":         row.get("WELL_SHUTIN_DT", "") or None,
        })

    total_kept = sum(len(v) for v in records_by_county.values())
    total_leases = sum(len(v) for v in leases_by_county.values())
    print(f"    {total_scanned:,} rows scanned")
    print(f"    {total_kept:,} well completions kept across {len(county_codes)} counties")
    print(f"    {total_leases:,} unique leases identified")

    return leases_by_county, records_by_county


def parse_lease_cycles(
    zf: zipfile.ZipFile,
    all_target_leases: set,
    lease_to_county: dict,
) -> dict[str, list]:
    """
    Stream OG_LEASE_CYCLE and keep rows matching any target lease.
    Routes each row to the correct county bucket.

    Parameters
    ----------
    all_target_leases : set of (oil_gas_code, district_no, lease_no) across all counties
    lease_to_county : maps each lease key → county_code

    Returns
    -------
    records_by_county : { county_code: list of dicts }
    """
    records_by_county: dict[str, list] = {}
    scanned = 0
    kept    = 0

    print(f"  Streaming '{LEASE_CYCLE_DSV}' (large file — filtering as we read)...")

    for row in stream_dsv(zf, LEASE_CYCLE_DSV):
        scanned += 1
        key = (
            row.get("OIL_GAS_CODE", ""),
            row.get("DISTRICT_NO", ""),
            row.get("LEASE_NO", ""),
        )

        if key not in all_target_leases:
            if scanned % PROGRESS_INTERVAL == 0:
                print(f"    ... scanned {scanned:,} rows, kept {kept:,} so far")
            continue

        county = lease_to_county.get(key)
        if county is None:
            continue

        kept += 1
        cycle_ym = row.get("CYCLE_YEAR_MONTH", "")

        record = {
            "oil_gas_code":      key[0],
            "district_no":       key[1],
            "lease_no":          key[2],
            "cycle_year_month":  cycle_ym,
            "cycle_year":        cycle_ym[:4] if len(cycle_ym) >= 4 else None,
            "cycle_month":       cycle_ym[4:6] if len(cycle_ym) >= 6 else None,
            "oil_prod_vol_bbl":  safe_int(row.get("LEASE_OIL_PROD_VOL")),
            "oil_allow_bbl":     safe_int(row.get("LEASE_OIL_ALLOW")),
            "oil_ending_bal_bbl":safe_int(row.get("LEASE_OIL_ENDING_BAL")),
            "gas_prod_vol_mcf":  safe_int(row.get("LEASE_GAS_PROD_VOL")),
            "gas_allow_mcf":     safe_int(row.get("LEASE_GAS_ALLOW")),
            "cond_prod_vol_bbl": safe_int(row.get("LEASE_COND_PROD_VOL")),
            "cond_ending_bal_bbl":safe_int(row.get("LEASE_COND_ENDING_BAL")),
            "csgd_prod_vol_mcf": safe_int(row.get("LEASE_CSGD_PROD_VOL")),
            "lease_name":        row.get("LEASE_NAME", ""),
            "operator_name":     row.get("OPERATOR_NAME", ""),
            "field_name":        row.get("FIELD_NAME", ""),
            "prod_report_filed": row.get("PROD_REPORT_FILED_FLAG", ""),
        }

        if county not in records_by_county:
            records_by_county[county] = []
        records_by_county[county].append(record)

        if scanned % PROGRESS_INTERVAL == 0:
            print(f"    ... scanned {scanned:,} rows, kept {kept:,} so far")

    print(f"    {scanned:,} total rows scanned")
    print(f"    {kept:,} lease-cycle rows kept")
    return records_by_county


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Extract county-level production data from PDQ_DSV.zip"
    )
    parser.add_argument("--counties", type=str, default=None,
                        help='Comma-separated county names (e.g. "Martin, Midland")')
    parser.add_argument("--list-counties", action="store_true",
                        help="Print all available counties and exit")
    parser.add_argument("--raw-dir",       default="data/raw")
    parser.add_argument("--processed-dir", default="data/processed")
    args = parser.parse_args()

    raw_dir       = Path(args.raw_dir)
    processed_dir = Path(args.processed_dir)
    zip_path      = raw_dir / ZIP_FILENAME

    if not zip_path.exists():
        print(
            f"Error: '{zip_path}' not found.\n"
            "Run productionData.py (or productionData.sh) first.",
            file=sys.stderr,
        )
        sys.exit(1)

    # ── Open ZIP and load county map ─────────────────────────────────────────
    with zipfile.ZipFile(zip_path, "r") as zf:
        names = zf.namelist()

        if GP_COUNTY_DSV not in names:
            print(f"Error: '{GP_COUNTY_DSV}' not found in ZIP.", file=sys.stderr)
            sys.exit(1)

        print(f"  Loading county lookup from '{GP_COUNTY_DSV}'...")
        county_map = load_county_map(zf)
        print(f"  {len(county_map)} counties loaded.")

        # ── List mode ────────────────────────────────────────────────────────
        if args.list_counties:
            print(f"\n  {'County Name':<30}  {'Code':>5}  {'District':>8}  {'Onshore':>8}")
            print(f"  {'─' * 30}  {'─' * 5}  {'─' * 8}  {'─' * 8}")
            for name in sorted(county_map.keys()):
                info = county_map[name]
                print(f"  {name:<30}  {info['code']:>5}  {info['district_no']:>8}  "
                      f"{info['onshore']:>8}")
            print(f"\n  {len(county_map)} counties total.")
            return

        # ── Resolve user input ───────────────────────────────────────────────
        if not args.counties:
            print("Error: --counties is required (or use --list-counties).", file=sys.stderr)
            sys.exit(1)

        targets = resolve_counties(county_map, args.counties)

        print(f"\n  Selected counties:")
        for name, code in targets:
            print(f"    {name} (code {code})")
        print()

        county_codes = {code for _, code in targets}
        code_to_name = {code: name for name, code in targets}

        # ── Verify required DSV files ────────────────────────────────────────
        missing = [f for f in [WELL_COMPLETION_DSV, LEASE_CYCLE_DSV] if f not in names]
        if missing:
            print(f"Error: expected DSV files not found in ZIP:", file=sys.stderr)
            for f in missing:
                print(f"  - {f}", file=sys.stderr)
            sys.exit(1)

        processed_dir.mkdir(parents=True, exist_ok=True)

        # ── Pass 1: Well completions ─────────────────────────────────────────
        leases_by_county, well_records_by_county = parse_well_completions(zf, county_codes)

        # Build unified lease set and lease → county mapping
        all_target_leases = set()
        lease_to_county: dict = {}
        for county_code, lease_set in leases_by_county.items():
            for lease_key in lease_set:
                all_target_leases.add(lease_key)
                lease_to_county[lease_key] = county_code

        if not all_target_leases:
            print("Error: no well completions found for any selected county.", file=sys.stderr)
            sys.exit(1)

        # Write per-county well Parquets
        for county_code in sorted(county_codes):
            name       = code_to_name[county_code]
            prefix     = to_camel(name)
            wells_out  = processed_dir / f"{prefix}Production_wells.parquet"
            records    = well_records_by_county.get(county_code, [])
            if records:
                pd.DataFrame(records).to_parquet(wells_out, index=False)
                print(f"  Saved → '{wells_out}'  ({len(records):,} rows)")
            else:
                print(f"  Warning: no well completions for {name} (code {county_code})")

        print()

        # ── Pass 2: Lease cycles ─────────────────────────────────────────────
        lease_records_by_county = parse_lease_cycles(zf, all_target_leases, lease_to_county)

    # Write per-county lease Parquets
    print()
    for county_code in sorted(county_codes):
        name       = code_to_name[county_code]
        prefix     = to_camel(name)
        leases_out = processed_dir / f"{prefix}Production_leases.parquet"
        records    = lease_records_by_county.get(county_code, [])
        if records:
            df = pd.DataFrame(records)
            df.to_parquet(leases_out, index=False)
            print(f"  Saved → '{leases_out}'  ({len(df):,} rows)")
        else:
            print(f"  Warning: no lease-cycle data for {name} (code {county_code})")

    # ── Summary ──────────────────────────────────────────────────────────────
    print()
    print("  ── Summary ──")
    for county_code in sorted(county_codes):
        name   = code_to_name[county_code]
        prefix = to_camel(name)
        wells  = well_records_by_county.get(county_code, [])
        leases = lease_records_by_county.get(county_code, [])

        print(f"\n  {name} (code {county_code}):")
        print(f"    {prefix}Production_wells.parquet   : {len(wells):,} well completions")
        print(f"    {prefix}Production_leases.parquet  : {len(leases):,} lease-month records")

        if leases:
            df = pd.DataFrame(leases)
            min_ym = df["cycle_year_month"].dropna().min()
            max_ym = df["cycle_year_month"].dropna().max()
            n_oil  = (df["oil_gas_code"] == "O").sum()
            n_gas  = (df["oil_gas_code"] == "G").sum()
            print(f"    Date range                         : {min_ym} → {max_ym}")
            print(f"    Oil lease-months                   : {n_oil:,}")
            print(f"    Gas lease-months                   : {n_gas:,}")

    print()
    print("  Join example (DuckDB):")
    print("    SELECT l.cycle_year_month, l.lease_name, l.oil_prod_vol_bbl,")
    print("           w.api_number")
    print("    FROM read_parquet('{county}Production_leases.parquet') l")
    print("    JOIN read_parquet('{county}Production_wells.parquet') w")
    print("      USING (oil_gas_code, district_no, lease_no)")
    print("    ORDER BY l.cycle_year_month, l.oil_prod_vol_bbl DESC;")


if __name__ == "__main__":
    main()