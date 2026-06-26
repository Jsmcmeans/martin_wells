#!/usr/bin/env python3
"""
parse_pending.py
================
Parses the three core Texas RRC pending-permit delimited text files
into GeoJSON suitable for ogr2ogr conversion to GeoParquet.

Files consumed (} delimited, "-quoted, with a header row):
  1. dp_drilling_permit_pending_yyyymmddhhmmss.txt   (52 columns)
  2. dp_wellbore_pending_yyyymmddhhmmss.txt
  3. dp_latlongs_pending_yyyymmddhhmmss.txt

Join path:
  permit  ──[UNIVERSAL_DOC_NO]──▶  wellbore
  wellbore ──[API_SEQUENCE_NUMBER]──▶  latlongs (LOCATION_TYPE = 'Surface')

Coordinates are NAD27; reprojection to WGS 84 is done by ogr2ogr.
"""

import argparse
import glob
import json
import sys
from pathlib import Path

# ── Lookup tables ────────────────────────────────────────────────────────────
FILING_PURPOSE_CODES = {
    "99": "Unknown",        "01": "New Drill",
    "07": "Reenter",        "09": "Field Transfer",
    "14": "Recompletion",   "15": "Reclass",
    "16": "Amended as Drilled BHL",
}

STATUS_CODES = {
    "P": "Pending Approval", "A": "Approved",   "W": "Withdrawn",
    "D": "Dismissed",        "E": "Denied",      "C": "Closed",
    "O": "Other",            "X": "Deleted",     "Z": "Cancelled",
}

CURRENT_STATE_CODES = {
    "APP": "Approved",       "ABT": "Aborted",         "REF": "Rejected",
    "WIP": "Work in Progress","FOP": "Field Operations","MPC": "Mapping Correction",
    "MPR": "Mapping Review", "HEA": "Hearing",          "LEG": "Legal Exam",
    "TEC": "Technical Exam", "DOC": "Docket Services",  "SWR": "SWR Hold",
    "NOA": "Notice of Application","PSA": "Public Sales","ENG": "Engineering",
    "DP_": "Drilling Permit","MAP": "Mapping",           "API": "API Verification",
    "CAN": "Cancelled",      "MCA": "MPC Cancel",        "MRS": "MPC Restore",
    "MRJ": "MPC Reject",     "MRI": "MPC Reinstatement", "WIT": "Withdrawn",
}


# ── Helpers ──────────────────────────────────────────────────────────────────
def resolve_file(raw_dir: Path, pattern: str) -> Path | None:
    matches = sorted(glob.glob(str(raw_dir / pattern)))
    return Path(matches[-1]) if matches else None


def read_delimited(path: Path, expected_min_cols: int) -> list[list]:
    """Read a }-delimited RRC file, stripping embedded double-quotes.

    The RRC now wraps every field value in double-quotes (e.g. '"317"', '"Y"',
    '"SURFACE"'). csv.reader with quotechar='"' does NOT strip these correctly
    because many values have a leading space before the opening quote, which
    prevents csv from recognising them as quoted fields. Instead we split each
    line on '}' and manually strip whitespace and surrounding double-quotes.

    The permit file also has a two-line header — the column name
    CASES_EXCEP_APPROVAL_CODE is split by a bare newline as CASES_EXCEP_A /
    PPROVAL_CODE. The second header fragment (6 fields) and any blank separator
    lines are silently dropped by the expected_min_cols guard.
    """
    rows = []
    with open(path, "r", encoding="latin-1") as fh:
        header_skipped = False
        for line in fh:
            parts = line.rstrip("\r\n").split("}")
            # Strip whitespace and surrounding double-quotes from every field
            row = [p.strip().strip('"') for p in parts]
            # Remove the trailing empty element produced by a trailing }
            while row and row[-1] == "":
                row.pop()
            if not header_skipped:
                header_skipped = True
                continue
            if len(row) < expected_min_cols:
                continue
            rows.append(row)
    return rows


def clean(val: str) -> str:
    # Strip whitespace and surrounding double-quotes.
    # read_delimited already strips quotes, but this handles any values
    # that arrive via other paths or future format changes.
    return val.strip().strip('"') if val else ""


def safe_float(val: str):
    val = val.strip().strip('"') if val else ""
    if not val:
        return None
    try:
        return float(val)
    except ValueError:
        return None


def flag(val: str) -> bool | None:
    v = clean(val).upper()
    if v == "Y":
        return True
    if v == "N":
        return False
    return None


# ── Field extractors ─────────────────────────────────────────────────────────
def extract_permit(row: list) -> dict:
    """
    Extract attributes from a dp_drilling_permit_pending row.
    Actual column layout (52 cols, 0-indexed) confirmed from header row:
      0-42  : original fields (unchanged)
      43    : OVERRIDE_FA_OP_CONSULT_YN  ← new
      44    : OVERRIDE_P5_DELINQUENT_YN  ← new
      45    : CASES_NO                   ← new
      46    : CASES_EXCEP_APPROVAL_CODE  ← new
      47    : APPROVED_BY                ← new
      48    : OPERATOR_NAME              (was 43)
      49    : OPERATOR_NUMBER            (was 44)
      50    : OPERATOR_PHONE             (was 45)
      51    : DISTRICT                   (was 46)
    """
    fp_code = clean(row[16])
    sc_code = clean(row[25])
    cs_code = clean(row[32])
    return {
        "swr38_abbr_notice":            clean(row[0]),
        "is_reapplied":                 flag(row[1]),
        "universal_doc_no":             clean(row[2]),
        "status_number":                clean(row[3]),
        "effective_date":               clean(row[4]) or None,
        "return_date":                  clean(row[5]) or None,
        "total_depth":                  safe_float(row[6]),
        "is_amendment":                 flag(row[7]),
        "swr_36_flag":                  flag(row[8]),
        "develop_minerals_flag":        flag(row[9]),
        "case_docket_no":               clean(row[10]),
        "final_protest_date":           clean(row[11]) or None,
        "status_seq_no":                clean(row[12]),
        "spud_date":                    clean(row[13]) or None,
        "expedite_flag":                flag(row[14]),
        "expedite_date_time":           clean(row[15]) or None,
        "filing_purpose_code":          fp_code,
        "filing_purpose":               FILING_PURPOSE_CODES.get(fp_code, fp_code),
        "surface_casing_date":          clean(row[17]) or None,
        "default_lease_name":           clean(row[18]),
        "default_well_number":          clean(row[19]),
        "default_vertical":             flag(row[20]),
        "default_horizontal":           flag(row[21]),   # note: typo in source ("HORIZANTAL")
        "default_sidetrack":            flag(row[22]),
        "locked_by":                    clean(row[23]),
        "default_directional":          flag(row[24]),
        "status_code":                  sc_code,
        "status":                       STATUS_CODES.get(sc_code, sc_code),
        "expiration_date":              clean(row[26]) or None,
        "walkin_contact_name":          clean(row[27]),
        "walkin_contact_phone":         clean(row[28]),
        "completion_code":              clean(row[29]),
        "swr_subsect_code":             clean(row[30]),
        "stat_date":                    clean(row[31]) or None,
        "current_state_code":           cs_code,
        "current_state":                CURRENT_STATE_CODES.get(cs_code, cs_code),
        "bridge_flag":                  clean(row[33]),
        "swr_list":                     clean(row[34]),
        "bridge_print_flag":            clean(row[35]),
        "has_discrepancy":              flag(row[36]),
        "submit_date":                  clean(row[37]) or None,
        "create_date":                  clean(row[38]) or None,
        "unique_address_number":        clean(row[39]),
        "dkt_suffix_code":              clean(row[40]),
        "dkt_examiner_code":            clean(row[41]),
        "reapplied_status_no":          clean(row[42]),
        # ── 5 new columns ───────────────────────────────────────────────
        "override_fa_op_consult":       flag(row[43]),
        "override_p5_delinquent":       flag(row[44]),
        "cases_no":                     clean(row[45]),
        "cases_excep_approval_code":    clean(row[46]),
        "approved_by":                  clean(row[47]),
        # ── shifted columns ─────────────────────────────────────────────
        "operator_name":                clean(row[48]),
        "operator_number":              clean(row[49]),
        "operator_phone":               clean(row[50]),
        "district":                     clean(row[51]) if len(row) > 51 else "",
    }


def extract_wellbore(row: list) -> dict:
    """Extract wellbore attributes including embedded surface coordinates.

    The wellbore file contains lat/lon columns directly:
      LAT_DEGREES_S  (index 10) / LONG_DEGREES_S (index 13) — surface location
      LAT_DEGREES    (index  1) / LONG_DEGREES   (index  4) — fallback

    These are decimal degrees (despite the DMS column name). We extract them
    here so the main join loop can use them as a fallback when the separate
    latlong file's API_SEQUENCE_NUMBER join fails (e.g. when the wellbore and
    latlong files were published on different days and don't overlap).
    """
    def _get(i): return row[i] if len(row) > i else ""

    # Prefer the explicit surface-suffixed columns; fall back to primary
    lat = safe_float(_get(10)) or safe_float(_get(1))
    lon = safe_float(_get(13)) or safe_float(_get(4))
    if lon is not None and lon > 0:
        lon = -lon  # Texas is western hemisphere — negate if stored unsigned

    return {
        "wb_api_sequence_number":   clean(_get(16)),
        "wb_nearest_town_distance": safe_float(_get(19)),
        "wb_nearest_town":          clean(_get(20)),
        "wb_county_code":           clean(_get(24)),
        "wb_surface_location_code": clean(_get(25)),
        "wb_wellbore_id":           clean(_get(26)),
        "wb_universal_doc_no":      clean(_get(28)),
        "wb_operator_name":         clean(_get(35)),
        "wb_operator_number":       clean(_get(36)),
        "wb_district":              clean(_get(38)),
        # Embedded surface coordinates — used as fallback when latlong join fails
        "wb_surf_latitude":         lat,
        "wb_surf_longitude":        lon,
    }


# ── Main pipeline ────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Parse pending-permit files → GeoJSON (surface, NAD27)"
    )
    parser.add_argument("--raw-dir", default="data/raw")
    parser.add_argument("--output", "-o",
                        default="data/processed/drillingPermitsPending.geojson")
    args = parser.parse_args()

    raw_dir     = Path(args.raw_dir)
    output_path = Path(args.output)

    # ── Discover files ───────────────────────────────────────────────────
    permit_file   = resolve_file(raw_dir, "dp_drilling_permit_pending_*.txt")
    wellbore_file = resolve_file(raw_dir, "dp_wellbore_pending_*.txt")
    latlong_file  = resolve_file(raw_dir, "dp_latlongs_pending_*.txt")

    missing = []
    if permit_file   is None: missing.append("dp_drilling_permit_pending_*.txt")
    if wellbore_file is None: missing.append("dp_wellbore_pending_*.txt")
    if latlong_file  is None: missing.append("dp_latlongs_pending_*.txt")

    if missing:
        print(f"Error: missing file(s) in '{raw_dir}':", file=sys.stderr)
        for m in missing:
            print(f"  - {m}", file=sys.stderr)
        sys.exit(1)

    print(f"  Permit file  : {permit_file.name}")
    print(f"  Wellbore file: {wellbore_file.name}")
    print(f"  LatLong file : {latlong_file.name}")

    # ── Read files ───────────────────────────────────────────────────────
    print("  Reading permit file ...")
    permit_rows = read_delimited(permit_file, 47)  # 47 = verified min from split header
    print(f"    {len(permit_rows):,} rows")

    print("  Reading wellbore file ...")
    wellbore_rows = read_delimited(wellbore_file, 29)
    print(f"    {len(wellbore_rows):,} rows")

    print("  Reading latlong file ...")
    latlong_rows = read_delimited(latlong_file, 4)
    print(f"    {len(latlong_rows):,} rows")

    # ── Build lookup: wellbore by UNIVERSAL_DOC_NO ───────────────────────
    wb_by_udn = {}
    for row in wellbore_rows:
        udn = clean(row[28])
        if udn:
            wb_by_udn[udn] = extract_wellbore(row)

    # ── Build lookup: surface latlongs by API_SEQUENCE_NUMBER ────────────
    ll_by_api = {}
    surface_ll_count = 0
    loc_type_counts = {}   # diagnostic — shows what LOCATION_TYPE values exist

    for row in latlong_rows:
        loc_type = clean(row[3])
        loc_type_counts[loc_type] = loc_type_counts.get(loc_type, 0) + 1
        if loc_type.lower() != "surface":
            continue
        api = clean(row[0])
        lat = safe_float(row[1])
        lon = safe_float(row[2])
        if api:
            if lon is not None and lon > 0:
                lon = -lon  # Texas is western hemisphere — negate if stored unsigned
            ll_by_api[api] = {"surf_latitude": lat, "surf_longitude": lon}
            surface_ll_count += 1

    print(f"  LOCATION_TYPE values found: {loc_type_counts}")
    print(f"  Surface lat/long entries  : {surface_ll_count:,}")
    print(f"  Wellbore lookup entries   : {len(wb_by_udn):,}")

    # ── Join and build features ──────────────────────────────────────────
    features = []
    with_geom    = 0
    geom_src_ll  = 0   # from latlong file join
    geom_src_wb  = 0   # from wellbore embedded coordinates (fallback)

    for row in permit_rows:
        props = extract_permit(row)
        udn   = props["universal_doc_no"]

        wb = wb_by_udn.get(udn, {})
        props.update(wb)

        api = wb.get("wb_api_sequence_number", "")
        ll  = ll_by_api.get(api, {})
        props.update(ll)

        # Primary: coordinates from the latlong file (explicitly labelled SURFACE)
        lon = ll.get("surf_longitude")
        lat = ll.get("surf_latitude")

        # Fallback: coordinates embedded in the wellbore file itself.
        # Used when the latlong file and wellbore file were published on
        # different days and their API_SEQUENCE_NUMBERs don't overlap.
        if lon is None or lat is None:
            lon = wb.get("wb_surf_longitude")
            lat = wb.get("wb_surf_latitude")
            if lon is not None and lat is not None:
                geom_src_wb += 1
        else:
            geom_src_ll += 1

        geometry = None
        if lon is not None and lat is not None:
            geometry = {"type": "Point", "coordinates": [lon, lat]}
            with_geom += 1

        features.append({
            "type": "Feature",
            "geometry": geometry,
            "properties": props,
        })

    # ── Write GeoJSON ────────────────────────────────────────────────────
    output_path.parent.mkdir(parents=True, exist_ok=True)

    geojson = {
        "type": "FeatureCollection",
        "name": "drillingPermitsPending",
        "crs": {
            "type": "name",
            "properties": {"name": "urn:ogc:def:crs:EPSG::4267"},  # NAD27
        },
        "features": features,
    }

    with open(output_path, "w", encoding="utf-8") as fh:
        json.dump(geojson, fh)

    without_geom = len(features) - with_geom
    print(f"  Features emitted    : {len(features):,}")
    print(f"    with geometry     : {with_geom:,}")
    print(f"      from latlong    : {geom_src_ll:,}  (latlong file join)")
    print(f"      from wellbore   : {geom_src_wb:,}  (wellbore embedded coords)")
    print(f"    without geometry  : {without_geom:,}")
    print(f"  Saved to '{output_path}'")


if __name__ == "__main__":
    main()