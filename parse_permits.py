#!/usr/bin/env python3
"""
parse_permits.py
================
Parses the Texas RRC daf420.dat fixed-width drilling-permit master file
into GeoJSON suitable for ogr2ogr conversion to GeoParquet.

Geometry is derived from Record Type 14 (GIS Surface Location) only.
Coordinates are stored in NAD27 decimal degrees; the shell pipeline
handles reprojection to WGS 84 via ogr2ogr.

Usage:
    python parse_permits.py [--input data/raw/daf420.dat]
                            [--output data/processed/drillingPermits.geojson]
"""

import argparse
import json
import sys
from pathlib import Path

# ── Record-type constants ────────────────────────────────────────────────────
RT_ROOT = "01"
RT_PERMIT = "02"
RT_FIELD = "03"
RT_GIS_SURFACE = "14"
RT_GIS_BOTTOM = "15"

RECORD_LEN = 510  # bytes per record

# ── Lookup tables ────────────────────────────────────────────────────────────
TYPE_APPLICATION_CODES = {
    "01": "Drill",
    "02": "Deepen (Below Casing)",
    "03": "Deepen (Within Casing)",
    "04": "Plug Back",
    "05": "Other",
    "06": "Amended Drill",
    "07": "Re-enter",
    "08": "Sidetrack",
    "09": "Field Transfer",
    "10": "Amended Prior to 1977",
    "11": "Drill Directional/Sidetrack",
    "12": "Drill Horizontal",
    "13": "Sidetrack Horizontal",
    "14": "Recompletion",
    "15": "Reclass",
}

STATUS_CODES = {
    "P": "Pending Approval",
    "A": "Approved",
    "W": "Withdrawn",
    "D": "Dismissed",
    "E": "Denied",
    "C": "Closed",
    "O": "Other",
    "X": "Deleted",
    "Z": "Cancelled",
}

WELL_STATUS_CODES = {
    "A": "Long String Casing",
    "B": "Conductor Casing",
    "C": "BHP / Cathodic Protection",
    "D": "Dry Hole",
    "F": "Circ Prod String Casing",
    "G": "Drive Pipe",
    "H": "P&A Dry Hole Letter",
    "I": "Temp Survey",
    "J": "Liner",
    "K": "P&A Sulphur Core Test",
    "L": "Plug Back",
    "M": "Core Test (P&A)",
    "N": "Intermediate String Casing",
    "O": "Plug Dry Hole (Oil)",
    "P": "Plug Dry Hole (Gas)",
    "Q": "Plug Fresh Water",
    "R": "Plug Stat Test",
    "S": "Plug Dry Hole (Explorate)",
    "T": "P&A Exploratory Test",
    "U": "Unsuccessful",
    "V": "Plug Uranium Test",
    "W": "Final Completion",
    "X": "Lignite Exploration (P&A)",
    "Y": "Side 1 Multi Completion",
    "Z": "Unperfed Completion",
    "1": "Side 1 Commingle",
}

DISTRICT_CODES = {
    "01": "01",  "02": "02",  "03": "03",  "04": "04",
    "05": "05",  "06": "06",  "07": "6E",  "08": "7B",
    "09": "7C",  "10": "08",  "11": "8A",  "12": "8B",
    "13": "09",  "14": "10",
}


# ── Helpers ──────────────────────────────────────────────────────────────────
def strip_field(record: str, start: int, length: int) -> str:
    """Extract and strip a fixed-width field.  *start* is 1-based POS."""
    idx = start - 1
    return record[idx : idx + length].strip()


def parse_date(raw: str) -> str | None:
    """Convert CCYYMMDD → ISO 8601 date string, or None if blank/zeros."""
    raw = raw.strip()
    if not raw or raw == "0" * len(raw):
        return None
    if len(raw) == 8:
        return f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]}"
    return raw  # fallback


def parse_implied_decimal(raw: str, integer_digits: int, decimal_digits: int):
    """Parse COBOL PIC 9(n)V9(m) implied-decimal field → float."""
    raw = raw.strip()
    if not raw or raw == "0" * len(raw):
        return None
    try:
        total = integer_digits + decimal_digits
        raw = raw.zfill(total)
        return float(f"{raw[:integer_digits]}.{raw[integer_digits:]}")
    except (ValueError, IndexError):
        return None


def safe_int(raw: str):
    """Return an int or None."""
    raw = raw.strip()
    if not raw or raw == "0" * len(raw):
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def flag(raw: str) -> bool | None:
    """Interpret Y/N flags."""
    v = raw.strip().upper()
    if v == "Y":
        return True
    if v == "N":
        return False
    return None


# ── Per-record-type parsers ──────────────────────────────────────────────────
def parse_root(rec: str) -> dict:
    return {
        "status_number":        strip_field(rec, 3, 7),
        "status_seq_number":    strip_field(rec, 10, 2),
        "county_code":          strip_field(rec, 12, 3),
        "lease_name":           strip_field(rec, 15, 32),
        "district_code":        strip_field(rec, 47, 2),
        "district":             DISTRICT_CODES.get(strip_field(rec, 47, 2), strip_field(rec, 47, 2)),
        "operator_number":      strip_field(rec, 49, 6),
        "app_received_date":    parse_date(strip_field(rec, 59, 8)),
        "operator_name":        strip_field(rec, 67, 32),
        "status_of_app":        STATUS_CODES.get(strip_field(rec, 101, 1), strip_field(rec, 101, 1)),
        "root_permit_number":   strip_field(rec, 113, 7),
        "issue_date":           parse_date(strip_field(rec, 120, 8)),
        "withdrawn_date":       parse_date(strip_field(rec, 128, 8)),
        "walkthrough_flag":     flag(strip_field(rec, 136, 1)),
        "well_number":          strip_field(rec, 157, 6),
        "ecap_filing_flag":     strip_field(rec, 183, 1),
    }


def parse_permit(rec: str) -> dict:
    ta_code = strip_field(rec, 68, 2)
    ws_code = strip_field(rec, 172, 1)
    return {
        "permit_number":            strip_field(rec, 5, 7),
        "permit_seq_number":        strip_field(rec, 12, 2),
        "permit_county_code":       strip_field(rec, 14, 3),
        "permit_lease_name":        strip_field(rec, 17, 32),
        "permit_district_code":     strip_field(rec, 49, 2),
        "permit_district":          DISTRICT_CODES.get(strip_field(rec, 49, 2), strip_field(rec, 49, 2)),
        "permit_well_number":       strip_field(rec, 51, 6),
        "permit_total_depth":       safe_int(strip_field(rec, 57, 5)),
        "permit_operator_number":   strip_field(rec, 62, 6),
        "type_application_code":    ta_code,
        "type_application":         TYPE_APPLICATION_CODES.get(ta_code, ta_code),
        "other_explanation":        strip_field(rec, 70, 30),
        "onshore_county":           strip_field(rec, 121, 3),
        "received_date":            parse_date(strip_field(rec, 124, 8)),
        "permit_issued_date":       parse_date(strip_field(rec, 132, 8)),
        "permit_amended_date":      parse_date(strip_field(rec, 140, 8)),
        "permit_extended_date":     parse_date(strip_field(rec, 148, 8)),
        "permit_spud_date":         parse_date(strip_field(rec, 156, 8)),
        "surface_casing_date":      parse_date(strip_field(rec, 164, 8)),
        "well_status_code":         ws_code,
        "well_status":              WELL_STATUS_CODES.get(ws_code, ws_code),
        "well_status_date":         parse_date(strip_field(rec, 173, 8)),
        "permit_expired_date":      parse_date(strip_field(rec, 181, 8)),
        "permit_cancelled_date":    parse_date(strip_field(rec, 189, 8)),
        "cancellation_reason":      strip_field(rec, 197, 30),
        "p12_filed":                flag(strip_field(rec, 227, 1)),
        "substandard_acreage":      flag(strip_field(rec, 228, 1)),
        "rule_36":                  flag(strip_field(rec, 229, 1)),
        "h9_filed":                 flag(strip_field(rec, 230, 1)),
        "rule_37_case_number":      strip_field(rec, 231, 7),
        "rule_38_docket_number":    strip_field(rec, 238, 7),
        "location_format_flag":     strip_field(rec, 245, 1),
        "surface_section":          strip_field(rec, 246, 8),
        "surface_block":            strip_field(rec, 254, 10),
        "surface_survey":           strip_field(rec, 264, 55),
        "surface_abstract":         strip_field(rec, 319, 6),
        "surface_acres":            parse_implied_decimal(strip_field(rec, 328, 8), 6, 2),
        "surface_miles_from_city":  parse_implied_decimal(strip_field(rec, 336, 6), 4, 2),
        "surface_dir_from_city":    strip_field(rec, 342, 6),
        "surface_nearest_city":     strip_field(rec, 348, 13),
        "final_update":             parse_date(strip_field(rec, 474, 8)),
        "cancelled_flag":           strip_field(rec, 482, 1),
        "spud_in_flag":             flag(strip_field(rec, 483, 1)),
        "directional_well":         flag(strip_field(rec, 484, 1)),
        "sidetrack_well":           flag(strip_field(rec, 485, 1)),
        "horizontal_well":          flag(strip_field(rec, 496, 1)),
        "duplicate_permit":         flag(strip_field(rec, 497, 1)),
        "nearest_lease_line":       strip_field(rec, 498, 7),
        "api_number":               strip_field(rec, 505, 8),
    }


def parse_gis_surface(rec: str) -> dict:
    """Record 14: longitude PIC 9(5)V9(7) at POS 3-14,
    latitude PIC 9(5)V9(7) at POS 15-26.
    Stored unsigned — longitude must be negated for Texas (Western Hemisphere).
    """
    lon = parse_implied_decimal(strip_field(rec, 3, 12), 5, 7)
    lat = parse_implied_decimal(strip_field(rec, 15, 12), 5, 7)
    if lon is not None and lon != 0:
        lon = -abs(lon)  # Texas is in the Western Hemisphere
    if lon == 0 and lat == 0:
        lon, lat = None, None
    return {"surf_longitude": lon, "surf_latitude": lat}


# ── File reader (handles both newline-delimited and blocked) ─────────────────
def read_records(path: Path):
    """Yield exactly 510-char records regardless of file format."""
    with open(path, "r", encoding="latin-1") as fh:
        first_line = fh.readline()
        fh.seek(0)

        # Heuristic: if the first line is roughly record-length, treat as
        # newline-delimited; otherwise treat as continuous blocked data.
        if len(first_line.rstrip("\n\r")) <= RECORD_LEN + 5:
            for line in fh:
                line = line.rstrip("\n\r")
                if len(line) == 0:
                    continue
                yield line.ljust(RECORD_LEN)
        else:
            buf = fh.read()
            for i in range(0, len(buf), RECORD_LEN):
                chunk = buf[i : i + RECORD_LEN]
                if len(chunk) < RECORD_LEN:
                    break
                yield chunk


# ── Main pipeline ────────────────────────────────────────────────────────────
def build_features(input_path: Path):
    counts = {}
    current_root = {}
    current_permit = None
    current_surface_gis = {}
    features = []
    pending_permits = []  # permits waiting for potential GIS record

    def flush_permit():
        """Emit the current permit (+ its root + GIS coords) as a feature."""
        nonlocal current_permit, current_surface_gis
        if current_permit is None:
            return
        props = {}
        props.update(current_root)
        props.update(current_permit)
        props.update(current_surface_gis)

        lon = current_surface_gis.get("surf_longitude")
        lat = current_surface_gis.get("surf_latitude")
        geometry = None
        if lon is not None and lat is not None:
            geometry = {"type": "Point", "coordinates": [lon, lat]}

        features.append({
            "type": "Feature",
            "geometry": geometry,
            "properties": props,
        })
        current_permit = None

    def flush_all_pending():
        """Flush all pending permits from the previous root group."""
        nonlocal current_permit, current_surface_gis, pending_permits
        for pm, gis in pending_permits:
            current_permit = pm
            current_surface_gis = gis
            flush_permit()
        pending_permits = []

    for rec in read_records(input_path):
        rt = rec[0:2]
        counts[rt] = counts.get(rt, 0) + 1

        if rt == RT_ROOT:
            # Flush any permits accumulated under the previous root
            flush_all_pending()
            current_root = parse_root(rec)
            current_surface_gis = {}

        elif rt == RT_PERMIT:
            # When we encounter a new permit, save the previous one
            if current_permit is not None:
                pending_permits.append((current_permit, current_surface_gis.copy()))
            current_permit = parse_permit(rec)
            current_surface_gis = {}

        elif rt == RT_GIS_SURFACE:
            current_surface_gis = parse_gis_surface(rec)

        # All other record types are currently skipped

    # Final flush
    if current_permit is not None:
        pending_permits.append((current_permit, current_surface_gis.copy()))
    flush_all_pending()

    return features, counts


def main():
    parser = argparse.ArgumentParser(
        description="Parse daf420.dat → GeoJSON (surface locations, NAD27)"
    )
    parser.add_argument(
        "--input", "-i",
        default="data/raw/daf420.dat",
        help="Path to the raw daf420.dat file",
    )
    parser.add_argument(
        "--output", "-o",
        default="data/processed/drillingPermits.geojson",
        help="Output GeoJSON path",
    )
    args = parser.parse_args()

    input_path = Path(args.input)
    output_path = Path(args.output)

    if not input_path.exists():
        print(f"Error: input file not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    output_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"  Parsing '{input_path}' ...")
    features, counts = build_features(input_path)

    geojson = {
        "type": "FeatureCollection",
        "name": "drillingPermits",
        "crs": {
            "type": "name",
            "properties": {"name": "urn:ogc:def:crs:EPSG::4267"},  # NAD27
        },
        "features": features,
    }

    with open(output_path, "w", encoding="utf-8") as fh:
        json.dump(geojson, fh)

    with_geom = sum(1 for f in features if f["geometry"] is not None)
    without_geom = len(features) - with_geom

    print(f"  Record counts by type: {dict(sorted(counts.items()))}")
    print(f"  Features emitted    : {len(features):,}")
    print(f"    with geometry     : {with_geom:,}")
    print(f"    without geometry  : {without_geom:,}")
    print(f"  Saved to '{output_path}'")


if __name__ == "__main__":
    main()