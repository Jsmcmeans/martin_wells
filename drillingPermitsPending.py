#!/usr/bin/env python3
"""
drillingPermitsPending.py
=========================
Downloads the three core pending-permit files from the Texas RRC
GoAnywhere MFT portal using a headless browser (Playwright).
The portal delivers .txt files that are actually ZIPs — this script
extracts them and deletes the ZIPs, leaving plain .txt files on disk.
"""

import asyncio
import re
import zipfile
from pathlib import Path
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

LINK_URL = "https://mft.rrc.texas.gov/link/0ad92a65-4212-49a1-98a7-d667a55fb497"
OUT_DIR  = Path("data/raw")

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
        txt_files = [n for n in zf.namelist() if n.endswith(".txt")]
        if not txt_files:
            raise RuntimeError(
                f"No .txt file found inside ZIP. Contents: {zf.namelist()}"
            )
        inner_name = txt_files[0]
        with zf.open(inner_name) as src, open(out_path, "wb") as dst:
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


async def find_and_download(page, key, pattern):
    """Scans pages for the target file, downloads it, extracts if zipped,
    and returns the resolved filename."""
    print(f"  Searching for '{key}' file...")

    # Always start from page 1
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

        best_match_name = None
        best_row_idx = -1

        for i in range(count):
            row = rows.nth(i)
            text = await row.text_content()
            if not text:
                continue
            m = pattern.search(text)
            if m:
                fname = m.group()
                if best_match_name is None or fname > best_match_name:
                    best_match_name = fname
                    best_row_idx = i

        if best_match_name:
            out_path = OUT_DIR / best_match_name

            if out_path.exists():
                print(f"    Already exists locally: '{best_match_name}'. Skipping.")
                return best_match_name

            row = rows.nth(best_row_idx)
            await row.scroll_into_view_if_needed()
            await row.hover()

            checkbox = row.locator(".ui-chkbox-box").first
            await checkbox.click()
            print(f"    Selected '{best_match_name}'")

            download_btn = page.locator("button", has_text="Download").first
            async with page.expect_download(timeout=120_000) as dl_info:
                await download_btn.click()

            dl = await dl_info.value
            await dl.save_as(out_path)
            print(f"    Saved to '{out_path}'")

            # Extract if the portal wrapped it in a ZIP
            extract_if_zipped(out_path)

            # Uncheck so next file can be selected cleanly
            await checkbox.click()

            return best_match_name

        # Try next page
        next_btn = page.locator(".ui-paginator-next").first
        if await next_btn.is_visible():
            btn_class = await next_btn.get_attribute("class") or ""
            if "ui-state-disabled" in btn_class:
                break
            await next_btn.click()
        else:
            break

    raise RuntimeError(f"Could not find any files matching the '{key}' pattern.")


async def download():
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

        await sort_table_newest_first(page)

        permit_filename = await find_and_download(page, "permit",   FILE_PATTERNS["permit"])
        await find_and_download(page, "wellbore", FILE_PATTERNS["wellbore"])
        await find_and_download(page, "latlong",  FILE_PATTERNS["latlong"])

        # Write marker for shell script
        marker_path = OUT_DIR / ".last_permit_pending"
        marker_path.write_text(permit_filename)
        print(f"  Wrote marker: '{marker_path}' → '{permit_filename}'")

        await browser.close()

    print("  All downloads complete.")


if __name__ == "__main__":
    asyncio.run(download())