#!/usr/bin/env python3
"""
drillingPermits.py
==================
Downloads the most recent monthly drilling permit master file from the
Texas RRC GoAnywhere MFT portal using a headless browser (Playwright).

File pattern: daf420.dat.mm-dd-yyyy (e.g. daf420.dat.05-31-2026)
Multiple monthly files may be listed — always downloads the most recent.
The portal wraps the file in a ZIP — this script extracts it and deletes
the ZIP, leaving the dated .dat file on disk.

The resolved filename is written to data/raw/.last_daf420 so the shell
pipeline can reference it dynamically.
"""

import asyncio
from pathlib import Path
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

from rrc_utils import (
    LINK_URL_DAF420,
    FILE_PATTERN_DAF420,
    DOWNLOAD_TIMEOUT_MS,
    parse_file_date,
    extract_dat_from_zip,
)

OUT_DIR     = Path("data/raw")
MARKER_PATH = OUT_DIR / ".last_daf420"


async def download():
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(accept_downloads=True)
        page    = await context.new_page()

        print("  Opening RRC GoDrive page...")
        await page.goto(LINK_URL_DAF420)

        try:
            await page.wait_for_load_state("networkidle", timeout=20_000)
        except PWTimeout:
            print("  Warning: page did not fully settle — continuing anyway")

        # ── Scan rows for all matching monthly files ─────────────────────
        rows = page.locator("tbody tr")
        count = await rows.count()

        matches = []
        for i in range(count):
            text = await rows.nth(i).text_content()
            if not text:
                continue
            m = FILE_PATTERN_DAF420.search(text)
            if m:
                matches.append((m.group(), i))

        if not matches:
            raise RuntimeError(
                "No daf420.dat.mm-dd-yyyy files found on the portal page."
            )

        # Sort descending by actual parsed date (NOT lexicographic)
        matches.sort(key=lambda x: parse_file_date(x[0]), reverse=True)
        most_recent_name, most_recent_idx = matches[0]
        out_path = OUT_DIR / most_recent_name
        zip_path = OUT_DIR / f"{most_recent_name}.zip"

        print(f"  Files found on server  : {len(matches)}")
        for name, _ in matches:
            marker = " ← most recent" if name == most_recent_name else ""
            print(f"    {name}{marker}")

        # ── Idempotency: skip if already downloaded ──────────────────────
        if out_path.exists():
            print(f"  Already exists locally: '{most_recent_name}'. Skipping.")
            MARKER_PATH.write_text(most_recent_name)
            await browser.close()
            return

        # ── Select and download ──────────────────────────────────────────
        row = rows.nth(most_recent_idx)
        await row.scroll_into_view_if_needed()
        await row.hover()

        checkbox = row.locator(".ui-chkbox-box").first
        await checkbox.wait_for(state="visible", timeout=10_000)
        await checkbox.click()
        print(f"  Selected '{most_recent_name}'")

        download_btn = page.locator("button", has_text="Download").first
        async with page.expect_download(timeout=DOWNLOAD_TIMEOUT_MS) as dl_info:
            await download_btn.click()

        dl = await dl_info.value
        await dl.save_as(zip_path)
        print(f"  Saved portal ZIP to '{zip_path}'")
        await browser.close()

    # ── Extract inner .dat file from ZIP ─────────────────────────────────
    print(f"  Extracting '{most_recent_name}' from ZIP...")
    extract_dat_from_zip(zip_path, out_path)
    print(f"  Extracted to '{out_path}'")

    # ── Write marker for shell script ────────────────────────────────────
    MARKER_PATH.write_text(most_recent_name)
    print(f"  Wrote marker: '{MARKER_PATH}' → '{most_recent_name}'")


if __name__ == "__main__":
    asyncio.run(download())
