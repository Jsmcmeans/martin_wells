#!/usr/bin/env python3
"""
drillingPermitsPending.py
=========================
Downloads the three core pending-permit files from the Texas RRC
GoAnywhere MFT portal using a headless browser (Playwright).
The portal delivers .txt files that are actually ZIPs — this script
extracts them and deletes the ZIPs, leaving plain .txt files on disk.

The table is scanned once to identify the best (newest) match per
file type, then each file is downloaded in sequence.
"""

import asyncio
import re
import zipfile
from pathlib import Path
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

from rrc_utils import LINK_URL_PENDING, DOWNLOAD_TIMEOUT_MS, validate_zip_entries

OUT_DIR = Path("data/raw")

FILE_PATTERNS = {
    "permit":   re.compile(r"dp_drilling_permit_pending_\d+\.txt"),
    "wellbore": re.compile(r"dp_wellbore_pending_\d+\.txt"),
    "latlong":  re.compile(r"dp_latlongs?_pending_\d+\.txt"),
}


def extract_if_zipped(out_path: Path) -> None:
    """If the downloaded file is a ZIP (PK magic bytes), extract the inner
    .txt and delete the ZIP. Leaves out_path as a plain text file."""
    with open(out_path, "rb") as f:
        magic = f.read(2)

    if magic != b"PK":
        return  # Already plain text — nothing to do

    zip_path = out_path.with_suffix(".zip")
    out_path.rename(zip_path)

    with zipfile.ZipFile(zip_path, "r") as zf:
        validate_zip_entries(zf)
        txt_files = [n for n in zf.namelist() if n.endswith(".txt")]
        if not txt_files:
            raise RuntimeError(
                f"No .txt file found inside ZIP. Contents: {zf.namelist()}"
            )
        with zf.open(txt_files[0]) as src, open(out_path, "wb") as dst:
            dst.write(src.read())

    zip_path.unlink()
    print(f"    Extracted from ZIP → '{out_path.name}'")


async def sort_table_newest_first(page):
    """Clicks the 'Last Modified' column header to sort descending."""
    print("  Sorting table by 'Last Modified' (newest first)...")
    await page.wait_for_selector("th", state="attached", timeout=15_000)
    header = page.locator("th", has_text=re.compile(r"Last Modified", re.IGNORECASE)).first

    if not await header.is_visible():
        print("  Warning: 'Last Modified' column header not visible. Proceeding without sorting.")
        return

    for i in range(3):
        aria_sort = await header.get_attribute("aria-sort")
        html_content = await header.inner_html()
        if aria_sort == "descending" or "triangle-1-s" in html_content:
            print("  Table sorted newest-first successfully.")
            return
        print(f"    Clicking header to sort (attempt {i+1})...")
        await header.click()
        await page.wait_for_timeout(2000)

    print("  Warning: Reached max clicks without confirming descending sort. Proceeding anyway.")


async def scan_all_pages(page) -> dict:
    """Single pass through all paginated table pages.
    Returns {key: best_filename} for each pattern in FILE_PATTERNS.
    Filenames contain a yyyymmddhhmmss timestamp, so lexicographic '>'
    correctly identifies the newest file without date parsing.
    """
    best: dict[str, str | None] = {key: None for key in FILE_PATTERNS}

    # Start from page 1
    first_page_btn = page.locator(".ui-paginator-first").first
    if await first_page_btn.is_visible():
        btn_class = await first_page_btn.get_attribute("class") or ""
        if "ui-state-disabled" not in btn_class:
            await first_page_btn.click()
            await page.wait_for_timeout(1000)

    while True:
        await page.wait_for_timeout(1000)
        await page.wait_for_selector("tbody tr", state="attached", timeout=15_000)

        rows = page.locator("tbody tr")
        count = await rows.count()

        for i in range(count):
            text = await rows.nth(i).text_content()
            if not text:
                continue
            for key, pattern in FILE_PATTERNS.items():
                m = pattern.search(text)
                if m:
                    fname = m.group()
                    if best[key] is None or fname > best[key]:
                        best[key] = fname

        next_btn = page.locator(".ui-paginator-next").first
        if await next_btn.is_visible():
            btn_class = await next_btn.get_attribute("class") or ""
            if "ui-state-disabled" in btn_class:
                break
            await next_btn.click()
        else:
            break

    return best


async def download_file(page, filename: str) -> None:
    """Navigate the table to locate filename, download it, and extract if zipped."""
    out_path = OUT_DIR / filename

    if out_path.exists():
        print(f"    Already exists locally: '{filename}'. Skipping.")
        return

    # Navigate to page 1 and find the matching row
    first_page_btn = page.locator(".ui-paginator-first").first
    if await first_page_btn.is_visible():
        btn_class = await first_page_btn.get_attribute("class") or ""
        if "ui-state-disabled" not in btn_class:
            await first_page_btn.click()
            await page.wait_for_timeout(1000)

    target_row = None
    while True:
        await page.wait_for_timeout(1000)
        await page.wait_for_selector("tbody tr", state="attached", timeout=15_000)

        rows = page.locator("tbody tr")
        count = await rows.count()

        for i in range(count):
            text = await rows.nth(i).text_content()
            if text and filename in text:
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
        raise RuntimeError(f"Could not find row for '{filename}' in the portal table.")

    await target_row.scroll_into_view_if_needed()
    await target_row.hover()

    checkbox = target_row.locator(".ui-chkbox-box").first
    await checkbox.click()
    print(f"    Selected '{filename}'")

    download_btn = page.locator("button", has_text="Download").first
    async with page.expect_download(timeout=DOWNLOAD_TIMEOUT_MS) as dl_info:
        await download_btn.click()

    dl = await dl_info.value
    await dl.save_as(out_path)
    print(f"    Saved to '{out_path}'")

    extract_if_zipped(out_path)

    # Uncheck so next file can be selected cleanly
    await checkbox.click()


async def download():
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(accept_downloads=True)
        page    = await context.new_page()

        print("  Opening RRC GoDrive page...")
        await page.goto(LINK_URL_PENDING)

        try:
            await page.wait_for_load_state("networkidle", timeout=20_000)
        except PWTimeout:
            print("  Warning: page did not fully settle — continuing anyway")

        await sort_table_newest_first(page)

        # ── Single scan pass to identify all target files ─────────────────
        print("  Scanning table for all target files...")
        file_map = await scan_all_pages(page)

        missing = [k for k, v in file_map.items() if v is None]
        if missing:
            raise RuntimeError(
                f"Could not find files for patterns: {missing}"
            )

        for key, fname in file_map.items():
            print(f"    {key:<10}: '{fname}'")

        # ── Download each file ────────────────────────────────────────────
        for key, filename in file_map.items():
            print(f"\n  [{key}] Downloading '{filename}'...")
            await download_file(page, filename)

        # Write marker for shell script (permit file is the primary reference)
        marker_path = OUT_DIR / ".last_permit_pending"
        marker_path.write_text(file_map["permit"])
        print(f"\n  Wrote marker: '{marker_path}' → '{file_map['permit']}'")

        await browser.close()

    print("  All downloads complete.")


if __name__ == "__main__":
    asyncio.run(download())
