#!/usr/bin/env python3
"""
rrc_utils.py — Shared utilities for the Martin County Wells pipeline.

Centralises portal URLs, download constants, ZIP safety validation,
and common extraction routines used across drillingPermits.py,
backfillPermits.py, martinWells.py, martinSurvey.py, and
drillingPermitsPending.py.
"""

import re
import shutil
import zipfile
from datetime import datetime
from pathlib import Path

# ── Portal URLs ───────────────────────────────────────────────────────────────
LINK_URL_DAF420  = "https://mft.rrc.texas.gov/link/f5dfea9c-bb39-4a5e-a44e-fb522e088cba"
LINK_URL_PENDING = "https://mft.rrc.texas.gov/link/0ad92a65-4212-49a1-98a7-d667a55fb497"
LINK_URL_WELL317 = "https://mft.rrc.texas.gov/link/d551fb20-442e-4b67-84fa-ac3f23ecabb4"
LINK_URL_SURV317 = "https://mft.rrc.texas.gov/link/7bee2b33-4d7c-45a6-b3bc-61215f214c3c"

# ── Constants ─────────────────────────────────────────────────────────────────
DOWNLOAD_TIMEOUT_MS = 180_000
FILE_PATTERN_DAF420 = re.compile(r"daf420\.dat\.(\d{2}-\d{2}-\d{4})")


# ── Date parsing ──────────────────────────────────────────────────────────────
def parse_file_date(filename: str) -> datetime:
    """Parse the mm-dd-yyyy suffix from a daf420 filename into a datetime.
    mm-dd-yyyy is NOT lexicographically sortable, so we parse properly.
    """
    m = FILE_PATTERN_DAF420.search(filename)
    if not m:
        raise ValueError(f"Cannot parse date from filename: {filename}")
    return datetime.strptime(m.group(1), "%m-%d-%Y")


# ── ZIP safety ────────────────────────────────────────────────────────────────
def validate_zip_entries(zf: zipfile.ZipFile) -> None:
    """Raise RuntimeError if any ZIP entry uses an absolute path or '..' (ZipSlip)."""
    for name in zf.namelist():
        p = Path(name)
        if p.is_absolute() or ".." in p.parts:
            raise RuntimeError(f"Unsafe ZIP entry (path traversal): {name!r}")


# ── ZIP extraction helpers ────────────────────────────────────────────────────
def extract_dat_from_zip(zip_path: Path, out_path: Path) -> None:
    """Extract the inner .dat file from a daf420 portal ZIP and delete the ZIP."""
    with zipfile.ZipFile(zip_path, "r") as zf:
        validate_zip_entries(zf)
        dat_files = [f for f in zf.namelist() if FILE_PATTERN_DAF420.search(f)] \
                 or [f for f in zf.namelist() if not f.endswith(".zip")]
        if not dat_files:
            raise RuntimeError(
                f"No .dat file found inside ZIP. Contents: {zf.namelist()}"
            )
        with zf.open(dat_files[0]) as src, open(out_path, "wb") as dst:
            dst.write(src.read())
    zip_path.unlink()


def extract_archive(zip_path: Path, target_base_dir: Path) -> None:
    """Unpack a shapefile ZIP into target_base_dir/<zip_stem>/.
    Handles flat ZIPs and portal-wrapped nested ZIPs.
    """
    temp_extract_dir  = target_base_dir / "temp_unzip_stage"
    final_extract_dir = target_base_dir / zip_path.stem

    if temp_extract_dir.exists():
        shutil.rmtree(temp_extract_dir)
    if final_extract_dir.exists():
        shutil.rmtree(final_extract_dir)

    temp_extract_dir.mkdir(parents=True, exist_ok=True)
    final_extract_dir.mkdir(parents=True, exist_ok=True)

    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            validate_zip_entries(zf)
            zf.extractall(temp_extract_dir)

        nested_zips = list(temp_extract_dir.glob("*.zip"))
        if nested_zips:
            for nz in nested_zips:
                with zipfile.ZipFile(nz, "r") as inner_ref:
                    validate_zip_entries(inner_ref)
                    inner_ref.extractall(final_extract_dir)
            print(f"  Successfully extracted nested archive to '{final_extract_dir}'")
        else:
            for item in temp_extract_dir.iterdir():
                shutil.move(str(item), final_extract_dir / item.name)
            print(f"  Successfully extracted archive to '{final_extract_dir}'")
    finally:
        if temp_extract_dir.exists():
            shutil.rmtree(temp_extract_dir)
