import asyncio
import shutil
import zipfile
from pathlib import Path
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

LINK_URL  = "https://mft.rrc.texas.gov/link/d551fb20-442e-4b67-84fa-ac3f23ecabb4"
FILE_NAME = "well317.zip"
OUT_DIR   = Path("data/raw")
OUT_PATH  = OUT_DIR / FILE_NAME

def extract_archive(zip_path: Path, target_base_dir: Path):
    """
    Dynamically unpacks the downloaded zip. Handles both nested zips 
    and flat folders to extract shapefiles into target_base_dir/well317.
    """
    print("  Extracting files dynamically...")
    temp_extract_dir = target_base_dir / "temp_unzip_stage"
    final_extract_dir = target_base_dir / zip_path.stem   # data/raw/well317
    
    # Clean up any legacy directory remnants
    if temp_extract_dir.exists():
        shutil.rmtree(temp_extract_dir)
    if final_extract_dir.exists():
        shutil.rmtree(final_extract_dir)
        
    temp_extract_dir.mkdir(parents=True, exist_ok=True)
    final_extract_dir.mkdir(parents=True, exist_ok=True)

    try:
        # 1. Extract outer container ZIP to a temporary staging area
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            zip_ref.extractall(temp_extract_dir)

        # 2. Check if the web portal wrapped it inside a nested ZIP
        nested_zips = list(temp_extract_dir.glob("*.zip"))

        if nested_zips:
            # Extract the actual inner zip containing shapefiles into final folder
            for nz in nested_zips:
                with zipfile.ZipFile(nz, 'r') as inner_ref:
                    inner_ref.extractall(final_extract_dir)
            print(f"  Successfully extracted nested archive to '{final_extract_dir}'")
        else:
            # If it wasn't nested, move contents directly to the final destination
            for item in temp_extract_dir.iterdir():
                shutil.move(str(item), final_extract_dir / item.name)
            print(f"  Successfully extracted archive to '{final_extract_dir}'")

    finally:
        # 3. Always clean up the temporary staging directory
        if temp_extract_dir.exists():
            shutil.rmtree(temp_extract_dir)


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
        async with page.expect_download(timeout=60_000) as dl_info:
            await download_btn.click()

        dl = await dl_info.value
        await dl.save_as(OUT_PATH)
        print(f"  Saved to '{OUT_PATH}'")
        await browser.close()
        
        # Unpack the downloaded zip file cleanly
        extract_archive(OUT_PATH, OUT_DIR)

asyncio.run(download())