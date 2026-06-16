import asyncio
import zipfile
from pathlib import Path
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

LINK_URL  = "https://mft.rrc.texas.gov/link/5f07cc72-2e79-4df8-ade1-9aeb792e03fc"
FILE_NAME = "daf420.dat"
OUT_DIR   = Path("data/raw")
OUT_PATH  = OUT_DIR / FILE_NAME
ZIP_PATH  = OUT_DIR / "daf420.zip"   # portal delivers a zip; extracted to OUT_PATH

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

        # Locate the daf420.dat row
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

    # Extract the inner .dat file from the zip
    print(f"  Extracting '{FILE_NAME}' from zip...")
    with zipfile.ZipFile(ZIP_PATH, "r") as zf:
        dat_files = [f for f in zf.namelist() if f.endswith(".dat")]
        if not dat_files:
            raise RuntimeError(
                f"No .dat file found inside zip. Contents: {zf.namelist()}"
            )
        inner_name = dat_files[0]
        with zf.open(inner_name) as src, open(OUT_PATH, "wb") as dst:
            dst.write(src.read())

    ZIP_PATH.unlink()   # remove the zip; OUT_PATH is the file the shell cares about
    print(f"  Extracted to '{OUT_PATH}'")

asyncio.run(download())