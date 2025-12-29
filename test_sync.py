#!/usr/bin/env python
import sys
import os
import pandas as pd

sys.path.insert(0, 'c:\\FDC_FLASK_APP')
os.chdir('c:\\FDC_FLASK_APP')

from app import app

GOOGLE_SHEETS_URL = "https://docs.google.com/spreadsheets/d/14I8AvXbZM1_O7RXg8EdOSOpFxQ1aU6tGftjhRruKNhE/export?format=csv"

with app.app_context():
    from py.Toolkits.boat import scan_dropbox_containers, add_multiple_containers_to_sheets
    
    # Scan Dropbox for new files
    dropbox_containers = scan_dropbox_containers()
    print(f'✓ Found {len(dropbox_containers)} total containers in Dropbox')
    
    # Get existing containers from sheets
    try:
        df_current = pd.read_csv(GOOGLE_SHEETS_URL)
        current_containers = set(df_current['FDC container'].dropna().tolist())
        print(f'✓ Found {len(current_containers)} existing containers in Google Sheets')
    except Exception as e:
        print(f'✗ Error fetching from Google Sheets: {e}')
        current_containers = set()
    
    # Find new containers to add
    containers_in_dropbox = set(dropbox_containers.keys())
    new_containers = sorted(list(containers_in_dropbox - current_containers))
    
    print(f'\n✓ New containers to add: {len(new_containers)}')
    if new_containers:
        print('  Latest 10 new containers:')
        for c in new_containers[-10:]:
            print(f'    {c}')
    
    # Add them to sheets
    if new_containers:
        added = add_multiple_containers_to_sheets(new_containers)
        print(f'\n✓ Added {len(added)} new containers to Google Sheets')
        if added:
            print('  Added:')
            for c in added[-5:]:
                print(f'    {c}')
    else:
        print('\nℹ No new containers to add')
