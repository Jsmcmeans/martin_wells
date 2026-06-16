import asyncio
import zipfile
from pathlib import Path
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

LINK_URL  = "https://mft.rrc.texas.gov/link/8b375643-f251-40d0-936d-b16f21f38ded"
FILE_NAME = "Basemap317.zip"
OUT_DIR   = Path("data/raw")
ZIP_PATH  = OUT_DIR / "Basemap317.zip"   # portal delivers a zip; extracted to OUT_PATH

async def download():
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

        # Locate the Basemap317.zip row
        row = page.locator(f"tr:has-text('{FILE_NAME}')").first
        await row.scroll_into_view_if_needed()
        await row.hover()

        # Click the PrimeFaces visual checkbox wrapper
        checkbox = row.locator(".ui-chkbox-box").first
        await checkbox.wait_for(state="visible", timeout=10_000)
        await checkbox.click()
        print(f"  Selected '{FILE_NAME}'")

        # Click Download and capture the zip the portal delivers
        download_btn = page.locator("button", has_text="Download").first
        async with page.expect_download(timeout=60_000) as dl_info:
            await download_btn.click()

        dl = await dl_info.value
        await dl.save_as(ZIP_PATH)
        print(f"  Saved portal zip to '{ZIP_PATH}'")
        await browser.close()

    # --- EXTRACTION LOGIC ---
    print(f"  Extracting '{FILE_NAME}' directly to '{OUT_DIR}'...")
    
    with zipfile.ZipFile(ZIP_PATH, "r") as zf:
        zf.extractall(OUT_DIR)

    ZIP_PATH.unlink()   # remove the zip; OUT_DIR is the file the shell cares about
    print(f"  Extracted to '{OUT_DIR}'")

asyncio.run(download())