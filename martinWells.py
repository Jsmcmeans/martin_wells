import asyncio
from pathlib import Path
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

from rrc_utils import LINK_URL_WELL317, DOWNLOAD_TIMEOUT_MS, extract_archive

FILE_NAME = "well317.zip"
OUT_DIR   = Path("data/raw")
OUT_PATH  = OUT_DIR / FILE_NAME


async def download():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(accept_downloads=True)
        page    = await context.new_page()

        print("  Opening RRC GoDrive page...")
        await page.goto(LINK_URL_WELL317)

        try:
            await page.wait_for_load_state("networkidle", timeout=20_000)
        except PWTimeout:
            print("  Warning: page did not fully settle — continuing anyway")

        # Locate the well317.zip row
        row = page.locator(f"tr:has-text('{FILE_NAME}')").first
        await row.scroll_into_view_if_needed()
        await row.hover()  # hover reveals the hidden checkbox in PrimeFaces tables

        # Click the PrimeFaces visual checkbox wrapper
        checkbox = row.locator(".ui-chkbox-box").first
        await checkbox.wait_for(state="visible", timeout=10_000)
        await checkbox.click()
        print(f"  Selected '{FILE_NAME}'")

        # Click Download and capture the file
        download_btn = page.locator("button", has_text="Download").first
        async with page.expect_download(timeout=DOWNLOAD_TIMEOUT_MS) as dl_info:
            await download_btn.click()

        dl = await dl_info.value
        await dl.save_as(OUT_PATH)
        print(f"  Saved to '{OUT_PATH}'")
        await browser.close()

        # Unpack the downloaded zip file cleanly
        extract_archive(OUT_PATH, OUT_DIR)


if __name__ == "__main__":
    asyncio.run(download())
