#!/usr/bin/env python3
"""
productionData.py
=================
Downloads PDQ_DSV.zip (Production Data Query Dump) from the Texas RRC
GoAnywhere MFT portal using a headless browser (Playwright).

The file is a single ZIP (~5 GB compressed / 25+ GB uncompressed) containing
16 }-delimited .dsv tables covering all Texas oil & gas production from 1993
to current. Updated the last Saturday of each month.

The GoAnywhere portal wraps downloads in an outer ZIP — this script detects
and unwraps the nesting automatically (runs every time, even if the download
itself was skipped by the idempotency check).

Note: Add the following constant to rrc_utils.py to keep URLs centralised:
    LINK_URL_PDQ = "https://mft.rrc.texas.gov/link/1f5ddb8d-329a-4459-b7f8-177b4f5ee60d"

The resolved filename is written to data/raw/.last_pdq so the shell
pipeline can reference it and detect staleness.
"""

import asyncio
import zipfile
from pathlib import Path
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

# ── Portal constants ──────────────────────────────────────────────────────────
# Defined locally until rrc_utils.py is updated to include LINK_URL_PDQ.
LINK_URL_PDQ        = "https://mft.rrc.texas.gov/link/1f5ddb8d-329a-4459-b7f8-177b4f5ee60d"
FILE_NAME           = "PDQ_DSV.zip"

# 60-minute timeout — the file is ~5 GB compressed.
DOWNLOAD_TIMEOUT_MS = 3_600_000

OUT_DIR     = Path("data/raw")
OUT_PATH    = OUT_DIR / FILE_NAME
MARKER_PATH = OUT_DIR / ".last_pdq"


def ensure_unwrapped():
    """Detect and unwrap the GoAnywhere portal ZIP wrapper if present.

    The portal delivers a ZIP whose sole content is another ZIP (same name)
    containing the actual .dsv files. This function checks every time —
    even on idempotent re-runs — so a previously downloaded but still-wrapped
    file gets fixed automatically.
    """
    if not OUT_PATH.exists():
        return

    print("  Checking for nested ZIP (portal wrapper)...")
    with zipfile.ZipFile(OUT_PATH, "r") as zf:
        contents = zf.namelist()
        has_dsv   = any(n.endswith(".dsv") for n in contents)
        inner_zip = [n for n in contents if n.endswith(".zip")]

    if has_dsv:
        print("  ZIP contains DSV files directly — OK.")
        return

    if inner_zip:
        inner_name = inner_zip[0]
        portal_path = OUT_PATH.with_suffix(".portal.zip")
        OUT_PATH.rename(portal_path)
        print(f"  Portal-wrapped ZIP detected — extracting '{inner_name}'...")

        with zipfile.ZipFile(portal_path, "r") as outer:
            with outer.open(inner_name) as src, open(OUT_PATH, "wb") as dst:
                # Stream in chunks to avoid loading 5 GB into memory
                while True:
                    chunk = src.read(8 * 1024 * 1024)  # 8 MB chunks
                    if not chunk:
                        break
                    dst.write(chunk)

        portal_path.unlink()
        print(f"  Unwrapped → '{OUT_PATH}'")
    else:
        print(f"  Warning: ZIP contains neither .dsv files nor an inner .zip: {contents[:5]}")


async def download():
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # ── Idempotency ───────────────────────────────────────────────────────────
    if OUT_PATH.exists() and MARKER_PATH.exists():
        print(f"  Skipping download: '{OUT_PATH}' already exists.")
    else:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(accept_downloads=True)
            page    = await context.new_page()

            print("  Opening RRC GoDrive page (PDQ)...")
            await page.goto(LINK_URL_PDQ)

            try:
                await page.wait_for_load_state("networkidle", timeout=20_000)
            except PWTimeout:
                print("  Warning: page did not fully settle — continuing anyway")

            # ── Locate the PDQ_DSV.zip row ────────────────────────────────────
            row = page.locator(f"tr:has-text('{FILE_NAME}')").first
            if not await row.is_visible():
                raise RuntimeError(
                    f"'{FILE_NAME}' row not found on portal page. "
                    "The filename may have changed — check the portal manually."
                )

            await row.scroll_into_view_if_needed()
            await row.hover()

            checkbox = row.locator(".ui-chkbox-box").first
            await checkbox.wait_for(state="visible", timeout=10_000)
            await checkbox.click()
            print(f"  Selected '{FILE_NAME}'")

            # ── Download (large file — 60-min timeout) ───────────────────────
            print(f"  Downloading '{FILE_NAME}' (~5 GB — this will take several minutes)...")
            download_btn = page.locator("button", has_text="Download").first
            async with page.expect_download(timeout=DOWNLOAD_TIMEOUT_MS) as dl_info:
                await download_btn.click()

            dl = await dl_info.value
            await dl.save_as(OUT_PATH)
            print(f"  Saved to '{OUT_PATH}'")
            await browser.close()

    # ── Always check for portal wrapper (even on skipped downloads) ──────────
    ensure_unwrapped()

    MARKER_PATH.write_text(FILE_NAME)
    print(f"  Wrote marker: '{MARKER_PATH}' → '{FILE_NAME}'")


if __name__ == "__main__":
    asyncio.run(download())