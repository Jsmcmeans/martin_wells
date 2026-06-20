#!/usr/bin/env python3
"""
pipelineData.py
===============
Downloads pipeline shapefiles by county from the Texas RRC GoAnywhere
MFT portal using a headless browser (Playwright).

File pattern: pipeline{FIPS}.zip (e.g. pipeline317.zip for Martin County)
Inside: pipe{FIPS}l.shp (pipeline lines) + sidecar files

Supports multiple counties in a single run. County names are resolved
to FIPS codes using an embedded lookup table.

The shell script (pipelineData.sh) handles ogr2ogr conversion to
GeoParquet with WGS 84 CRS and active-only filtering.

Note: Add the following constant to rrc_utils.py to keep URLs centralised:
    LINK_URL_PIPELINE = "https://mft.rrc.texas.gov/link/c7cbab0c-afe2-4f6f-91ae-e6ed7d3a7ab6"

Usage:
    python pipelineData.py --list-counties
    python pipelineData.py --counties "Martin"
    python pipelineData.py --counties "Martin, Midland, Howard"
"""

import argparse
import asyncio
import sys
from pathlib import Path
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

from rrc_utils import DOWNLOAD_TIMEOUT_MS, extract_archive

# ── Portal URL ────────────────────────────────────────────────────────────────
LINK_URL_PIPELINE = "https://mft.rrc.texas.gov/link/c7cbab0c-afe2-4f6f-91ae-e6ed7d3a7ab6"

OUT_DIR       = Path("data/raw")
MANIFEST_PATH = OUT_DIR / ".pipeline_manifest"

# ── Texas county → FIPS code lookup (from RRC OGA094, Appendix B) ─────────────
COUNTY_FIPS = {
    "ANDERSON": "001", "ANDREWS": "003", "ANGELINA": "005", "ARANSAS": "007",
    "ARCHER": "009", "ARMSTRONG": "011", "ATASCOSA": "013", "AUSTIN": "015",
    "BAILEY": "017", "BANDERA": "019", "BASTROP": "021", "BAYLOR": "023",
    "BEE": "025", "BELL": "027", "BEXAR": "029", "BLANCO": "031",
    "BORDEN": "033", "BOSQUE": "035", "BOWIE": "037", "BRAZORIA": "039",
    "BRAZOS": "041", "BREWSTER": "043", "BRISCOE": "045", "BROOKS": "047",
    "BROWN": "049", "BURLESON": "051", "BURNET": "053", "CALDWELL": "055",
    "CALHOUN": "057", "CALLAHAN": "059", "CAMERON": "061", "CAMP": "063",
    "CARSON": "065", "CASS": "067", "CASTRO": "069", "CHAMBERS": "071",
    "CHEROKEE": "073", "CHILDRESS": "075", "CLAY": "077", "COCHRAN": "079",
    "COKE": "081", "COLEMAN": "083", "COLLIN": "085", "COLLINGSWORTH": "087",
    "COLORADO": "089", "COMAL": "091", "COMANCHE": "093", "CONCHO": "095",
    "COOKE": "097", "CORYELL": "099", "COTTLE": "101", "CRANE": "103",
    "CROCKETT": "105", "CROSBY": "107", "CULBERSON": "109", "DALLAM": "111",
    "DALLAS": "113", "DAWSON": "115", "DEAF SMITH": "117", "DELTA": "119",
    "DENTON": "121", "DEWITT": "123", "DICKENS": "125", "DIMMIT": "127",
    "DONLEY": "129", "DUVAL": "131", "EASTLAND": "133", "ECTOR": "135",
    "EDWARDS": "137", "ELLIS": "139", "EL PASO": "141", "ERATH": "143",
    "FALLS": "145", "FANNIN": "147", "FAYETTE": "149", "FISHER": "151",
    "FLOYD": "153", "FOARD": "155", "FORT BEND": "157", "FRANKLIN": "159",
    "FREESTONE": "161", "FRIO": "163", "GAINES": "165", "GALVESTON": "167",
    "GARZA": "169", "GILLESPIE": "171", "GLASSCOCK": "173", "GOLIAD": "175",
    "GONZALES": "177", "GRAY": "179", "GRAYSON": "181", "GREGG": "183",
    "GRIMES": "185", "GUADALUPE": "187", "HALE": "189", "HALL": "191",
    "HAMILTON": "193", "HANSFORD": "195", "HARDEMAN": "197", "HARDIN": "199",
    "HARRIS": "201", "HARRISON": "203", "HARTLEY": "205", "HASKELL": "207",
    "HAYS": "209", "HEMPHILL": "211", "HENDERSON": "213", "HIDALGO": "215",
    "HILL": "217", "HOCKLEY": "219", "HOOD": "221", "HOPKINS": "223",
    "HOUSTON": "225", "HOWARD": "227", "HUDSPETH": "229", "HUNT": "231",
    "HUTCHINSON": "233", "IRION": "235", "JACK": "237", "JACKSON": "239",
    "JASPER": "241", "JEFF DAVIS": "243", "JEFFERSON": "245", "JIM HOGG": "247",
    "JIM WELLS": "249", "JOHNSON": "251", "JONES": "253", "KARNES": "255",
    "KAUFMAN": "257", "KENDALL": "259", "KENNEDY": "261", "KENT": "263",
    "KERR": "265", "KIMBLE": "267", "KING": "269", "KINNEY": "271",
    "KLEBERG": "273", "KNOX": "275", "LAMAR": "277", "LAMB": "279",
    "LAMPASAS": "281", "LA SALLE": "283", "LAVACA": "285", "LEE": "287",
    "LEON": "289", "LIBERTY": "291", "LIMESTONE": "293", "LIPSCOMB": "295",
    "LIVE OAK": "297", "LLANO": "299", "LOVING": "301", "LUBBOCK": "303",
    "LYNN": "305", "MCCULLOCH": "307", "MCLENNAN": "309", "MCMULLEN": "311",
    "MADISON": "313", "MARION": "315", "MARTIN": "317", "MASON": "319",
    "MATAGORDA": "321", "MAVERICK": "323", "MEDINA": "325", "MENARD": "327",
    "MIDLAND": "329", "MILAM": "331", "MILLS": "333", "MITCHELL": "335",
    "MONTAGUE": "337", "MONTGOMERY": "339", "MOORE": "341", "MORRIS": "343",
    "MOTLEY": "345", "NACOGDOCHES": "347", "NAVARRO": "349", "NEWTON": "351",
    "NOLAN": "353", "NUECES": "355", "OCHILTREE": "357", "OLDHAM": "359",
    "ORANGE": "361", "PALO PINTO": "363", "PANOLA": "365", "PARKER": "367",
    "PARMER": "369", "PECOS": "371", "POLK": "373", "POTTER": "375",
    "PRESIDIO": "377", "RAINS": "379", "RANDALL": "381", "REAGAN": "383",
    "REAL": "385", "RED RIVER": "387", "REEVES": "389", "REFUGIO": "391",
    "ROBERTS": "393", "ROBERTSON": "395", "ROCKWALL": "397", "RUNNELS": "399",
    "RUSK": "401", "SABINE": "403", "SAN AUGUSTINE": "405", "SAN JACINTO": "407",
    "SAN PATRICIO": "409", "SAN SABA": "411", "SCHLEICHER": "413", "SCURRY": "415",
    "SHACKELFORD": "417", "SHELBY": "419", "SHERMAN": "421", "SMITH": "423",
    "SOMERVELL": "425", "STARR": "427", "STEPHENS": "429", "STERLING": "431",
    "STONEWALL": "433", "SUTTON": "435", "SWISHER": "437", "TARRANT": "439",
    "TAYLOR": "441", "TERRELL": "443", "TERRY": "445", "THROCKMORTON": "447",
    "TITUS": "449", "TOM GREEN": "451", "TRAVIS": "453", "TRINITY": "455",
    "TYLER": "457", "UPSHUR": "459", "UPTON": "461", "UVALDE": "463",
    "VAL VERDE": "465", "VAN ZANDT": "467", "VICTORIA": "469", "WALKER": "471",
    "WALLER": "473", "WARD": "475", "WASHINGTON": "477", "WEBB": "479",
    "WHARTON": "481", "WHEELER": "483", "WICHITA": "485", "WILBARGER": "487",
    "WILLACY": "489", "WILLIAMSON": "491", "WILSON": "493", "WINKLER": "495",
    "WISE": "497", "WOOD": "499", "YOAKUM": "501", "YOUNG": "503",
    "ZAPATA": "505", "ZAVALA": "507",
}


def to_camel(name: str) -> str:
    """'MARTIN' → 'martin', 'SAN PATRICIO' → 'sanPatricio'"""
    parts = name.strip().lower().split()
    if not parts:
        return "unknown"
    return parts[0] + "".join(p.capitalize() for p in parts[1:])


def resolve_counties(user_input: str) -> list[tuple[str, str]]:
    """Resolve comma-separated county names to (canonical_name, fips_code) pairs.
    Case-insensitive, partial-match supported. Exits on ambiguity or no match."""
    results = []
    for raw in user_input.split(","):
        query = raw.strip().upper()
        if not query:
            continue

        if query in COUNTY_FIPS:
            results.append((query, COUNTY_FIPS[query]))
            continue

        matches = [(name, fips) for name, fips in COUNTY_FIPS.items()
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
            for name, fips in sorted(matches):
                print(f"    {name} (FIPS {fips})", file=sys.stderr)
            print("  Be more specific.", file=sys.stderr)
            sys.exit(1)

    # Deduplicate
    seen = set()
    unique = []
    for name, fips in results:
        if fips not in seen:
            seen.add(fips)
            unique.append((name, fips))
    return sorted(unique)


async def download_one(page, zip_name: str, out_dir: Path) -> bool:
    """Find a specific pipeline ZIP on the portal, download, and extract it."""
    out_path      = out_dir / zip_name
    extract_dir   = out_dir / zip_name.replace(".zip", "")

    if extract_dir.exists() and any(extract_dir.glob("*.shp")):
        print(f"    Already extracted — skipping.")
        return True

    # Navigate to first page
    first_btn = page.locator(".ui-paginator-first").first
    if await first_btn.is_visible():
        btn_class = await first_btn.get_attribute("class") or ""
        if "ui-state-disabled" not in btn_class:
            await first_btn.click()
            await page.wait_for_timeout(1000)

    # Search through pages for matching row
    target_row = None
    while True:
        await page.wait_for_timeout(1000)
        await page.wait_for_selector("tbody tr", state="attached", timeout=15_000)

        rows = page.locator("tbody tr")
        count = await rows.count()

        for i in range(count):
            text = await rows.nth(i).text_content()
            if text and zip_name in text:
                target_row = rows.nth(i)
                break

        if target_row is not None:
            break

        next_btn = page.locator(".ui-paginator-next").first
        if await next_btn.is_visible():
            btn_class = await next_btn.get_attribute("class") or ""
            if "ui-state-disabled" in btn_class:
                break
            await next_btn.click()
        else:
            break

    if target_row is None:
        print(f"    Error: '{zip_name}' not found on portal.", file=sys.stderr)
        return False

    try:
        await target_row.scroll_into_view_if_needed()
        await target_row.hover()

        checkbox = target_row.locator(".ui-chkbox-box").first
        await checkbox.wait_for(state="visible", timeout=10_000)
        await checkbox.click()
        print(f"    Selected '{zip_name}'")

        download_btn = page.locator("button", has_text="Download").first
        async with page.expect_download(timeout=DOWNLOAD_TIMEOUT_MS) as dl_info:
            await download_btn.click()

        dl = await dl_info.value
        await dl.save_as(out_path)
        print(f"    Saved to '{out_path}'")

        # Uncheck so next file can be selected cleanly
        await target_row.scroll_into_view_if_needed()
        await target_row.hover()
        checkbox = target_row.locator(".ui-chkbox-box").first
        await checkbox.wait_for(state="visible", timeout=10_000)
        await checkbox.click()

        # Extract the ZIP
        extract_archive(out_path, out_dir)
        return True

    except Exception as e:
        print(f"    FAILED: {e}", file=sys.stderr)
        return False


async def download(counties: list[tuple[str, str]], raw_dir: Path):
    """Download pipeline shapefiles for each county."""
    raw_dir.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(accept_downloads=True)
        page    = await context.new_page()

        print("  Opening RRC GoDrive page (Pipelines)...")
        await page.goto(LINK_URL_PIPELINE)

        try:
            await page.wait_for_load_state("networkidle", timeout=20_000)
        except PWTimeout:
            print("  Warning: page did not fully settle — continuing anyway")

        successes, failures = [], []

        for i, (name, fips) in enumerate(counties, 1):
            zip_name = f"pipeline{fips}.zip"
            print(f"\n  [{i}/{len(counties)}] {name} County → {zip_name}")
            ok = await download_one(page, zip_name, raw_dir)
            (successes if ok else failures).append((name, fips))

        await browser.close()

    # Write manifest for shell script
    manifest_lines = [f"{name}:{fips}" for name, fips in successes]
    MANIFEST_PATH.write_text("\n".join(manifest_lines) + "\n")
    print(f"\n  Manifest: {len(successes)} counties → '{MANIFEST_PATH}'")

    if failures:
        print(f"  Failures: {len(failures)}")
        for name, fips in failures:
            print(f"    ✗ {name} (FIPS {fips})")


def main():
    parser = argparse.ArgumentParser(
        description="Download pipeline shapefiles by county from Texas RRC"
    )
    parser.add_argument("--counties", type=str, default=None,
                        help='Comma-separated county names (e.g. "Martin, Midland")')
    parser.add_argument("--list-counties", action="store_true",
                        help="Print all available counties and exit")
    parser.add_argument("--raw-dir", default="data/raw")
    args = parser.parse_args()

    if args.list_counties:
        print(f"\n  {'County Name':<25}  {'FIPS':>5}")
        print(f"  {'─' * 25}  {'─' * 5}")
        for name in sorted(COUNTY_FIPS.keys()):
            print(f"  {name:<25}  {COUNTY_FIPS[name]:>5}")
        print(f"\n  {len(COUNTY_FIPS)} counties total.")
        return

    if not args.counties:
        print("Error: --counties is required (or use --list-counties).", file=sys.stderr)
        sys.exit(1)

    targets = resolve_counties(args.counties)
    print(f"  Selected counties:")
    for name, fips in targets:
        print(f"    {name} (FIPS {fips}) → pipeline{fips}.zip")

    asyncio.run(download(targets, Path(args.raw_dir)))


if __name__ == "__main__":
    main()