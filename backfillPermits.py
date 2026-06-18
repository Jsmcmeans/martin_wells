#!/usr/bin/env python3
"""
backfill_permits.py
===================
Downloads the 12 most recent month-end drilling permit master files from
the Texas RRC GoAnywhere MFT portal using a headless browser (Playwright).

- Files already present locally are skipped (idempotent)
- Failed downloads are logged to data/raw/.backfill_failures and skipped
- Successfully downloaded filenames are written oldest-first to
  data/raw/.backfill_manifest so the shell pipeline processes them in
  the correct chronological order for merging

Run via backfill_permits.sh — not intended for standalone use.
"""

import asyncio
import re
import zipfile
from datetime import datetime
from pathlib import Path
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

LINK_URL     = "https://mft.rrc.texas.gov/link/f5dfea9c-bb39-4a5e-a44e-fb522e088cba"
FILE_PATTERN = re.compile(r"daf420\.dat\.(\d{2}-\d{2}-\d{4})")
OUT_DIR      = Path("data/raw")
MONTHS       = 12
MANIFEST_PATH  = OUT_DIR / ".backfill_manifest"
FAILURES_PATH  = OUT_DIR / ".backfill_failures"


def parse_file_date(filename: str) -> datetime:
    m = FILE_PATTERN.search(filename)
    if not m:
        raise ValueError(f"Cannot parse date from: {filename}")
    return datetime.strptime(m.group(1), "%m-%d-%Y")


async def download_one(page, filename: str, row_idx: int) -> bool:
    """Select, download, and extract one file. Returns True on success."""
    out_path = OUT_DIR / filename
    zip_path = OUT_DIR / f"{filename}.zip"

    if out_path.exists():
        print(f"    Already exists locally — skipping.")
        return True

    try:
        rows = page.locator("tbody tr")
        row  = rows.nth(row_idx)
        await row.scroll_into_view_if_needed()
        await row.hover()

        checkbox = row.locator(".ui-chkbox-box").first
        await checkbox.wait_for(state="visible", timeout=10_000)
        await checkbox.click()

        download_btn = page.locator("button", has_text="Download").first
        async with page.expect_download(timeout=180_000) as dl_info:
            await download_btn.click()

        dl = await dl_info.value
        await dl.save_as(zip_path)

        # Extract inner .dat file
        with zipfile.ZipFile(zip_path, "r") as zf:
            dat_files = [f for f in zf.namelist() if FILE_PATTERN.search(f)] \
                     or [f for f in zf.namelist() if not f.endswith(".zip")]
            if not dat_files:
                raise RuntimeError(f"No .dat file inside ZIP. Contents: {zf.namelist()}")
            with zf.open(dat_files[0]) as src, open(out_path, "wb") as dst:
                dst.write(src.read())

        zip_path.unlink()
        print(f"    Extracted → '{out_path.name}'")

        # Uncheck so next file can be selected cleanly
        await row.scroll_into_view_if_needed()
        await row.hover()
        checkbox = row.locator(".ui-chkbox-box").first
        await checkbox.wait_for(state="visible", timeout=10_000)
        await checkbox.click()

        return True

    except Exception as e:
        print(f"    FAILED: {e}")
        if zip_path.exists():
            zip_path.unlink()
        # Uncheck if checkbox was left selected
        try:
            rows = page.locator("tbody tr")
            row  = rows.nth(row_idx)
            await row.hover()
            checkbox = row.locator(".ui-chkbox-box").first
            await checkbox.click()
        except Exception:
            pass
        return False


async def backfill():
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(accept_downloads=True)
        page    = await context.new_page()

        print("  Opening RRC GoDrive page...")
        await page.goto(LINK_URL)

        try:
            await page.wait_for_load_state("networkidle", timeout=20_000)
        except PWTimeout:
            print("  Warning: page did not fully settle — continuing anyway")

        # Scan all rows for matching files
        rows  = page.locator("tbody tr")
        count = await rows.count()

        matches = []
        for i in range(count):
            text = await rows.nth(i).text_content()
            if not text:
                continue
            m = FILE_PATTERN.search(text)
            if m:
                matches.append((m.group(), i))

        if not matches:
            raise RuntimeError("No daf420.dat.mm-dd-yyyy files found on portal page.")

        # Sort descending by date, take the N most recent
        matches.sort(key=lambda x: parse_file_date(x[0]), reverse=True)
        targets = matches[:MONTHS]

        print(f"  {len(matches)} total files found — targeting {MONTHS} most recent:")
        for name, _ in targets:
            print(f"    {name}")
        print()

        # Download oldest → newest (correct chronological order for merging)
        targets_oldest_first = list(reversed(targets))
        successes, failures = [], []

        for i, (filename, row_idx) in enumerate(targets_oldest_first, 1):
            print(f"  [{i:02d}/{MONTHS}] {filename}")
            ok = await download_one(page, filename, row_idx)
            (successes if ok else failures).append(filename)

        await browser.close()

    # Write manifest: oldest first = correct merge order
    MANIFEST_PATH.write_text("\n".join(successes))
    print(f"\n  Manifest   : {len(successes)} files → '{MANIFEST_PATH}'")

    if failures:
        FAILURES_PATH.write_text("\n".join(failures))
        print(f"  Failures   : {len(failures)} files → '{FAILURES_PATH}'")
        for f in failures:
            print(f"    ✗ {f}")
    else:
        if FAILURES_PATH.exists():
            FAILURES_PATH.unlink()
        print("  Failures   : none")


if __name__ == "__main__":
    asyncio.run(backfill())