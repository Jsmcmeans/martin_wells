#!/usr/bin/env python3
"""
check_pending_columns.py
Shows the full column header and first data row of the pending permit file.
Run from the martin-county-wells directory.
"""
import csv
import glob
from pathlib import Path

raw_dir = Path('data/raw')

for pattern, label in [
    ('dp_drilling_permit_pending_*.txt', 'PERMIT'),
    ('dp_wellbore_pending_*.txt',         'WELLBORE'),
    ('dp_latlongs_pending_*.txt',         'LATLONG'),
    ('dp_latlong_pending_*.txt',          'LATLONG'),
]:
    files = sorted(glob.glob(str(raw_dir / pattern)))
    if not files:
        continue

    path = files[-1]
    print(f'{"="*70}')
    print(f'{label}: {path}')
    print(f'{"="*70}')

    with open(path, 'r', encoding='latin-1') as f:
        raw_header = f.readline()
        raw_row1   = f.readline()
        raw_row2   = f.readline()

    # Count fields by splitting on }
    header_fields = raw_header.rstrip('\r\n').split('}')
    row1_fields   = raw_row1.rstrip('\r\n').split('}')
    row2_fields   = raw_row2.rstrip('\r\n').split('}') if raw_row2 else []

    print(f'Header columns : {len(header_fields)}')
    print(f'Row 1 fields   : {len(row1_fields)}')
    print(f'Row 2 fields   : {len(row2_fields)}')
    print()

    print('--- HEADER COLUMNS (index: name) ---')
    for i, col in enumerate(header_fields):
        print(f'  {i:3d}: {col}')
    print()

    print('--- ROW 1 VALUES (index: value) ---')
    for i, val in enumerate(row1_fields):
        col_name = header_fields[i] if i < len(header_fields) else '???'
        print(f'  {i:3d}: [{col_name}] = {repr(val[:60])}')
    print()