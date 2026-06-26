#!/usr/bin/env python3
from pathlib import Path
import glob

raw_dir = Path('data/raw')
files = sorted(glob.glob(str(raw_dir / 'dp_drilling_permit_pending_*.txt')))
if not files:
    print('No pending permit files found in data/raw/')
else:
    with open(files[-1], 'r', encoding='latin-1') as f:
        header = f.readline()
        row1   = f.readline()
        row2   = f.readline()
    print('File:', files[-1])
    print()
    print('Header (first 300 chars):')
    print(repr(header[:300]))
    print()
    print('Row 1 (first 300 chars):')
    print(repr(row1[:300]))
    print()
    print('Delimiter counts in header:')
    for ch, name in [('}','brace'), (',','comma'), ('|','pipe'), ('\t','tab')]:
        print('  ' + name + ': ' + str(header.count(ch)) + ' occurrences')