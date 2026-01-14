import pandas as pd
import glob
import os

dropbox_path = r"C:\Users\stream\Dropbox\Pictures-China (1)\container catalog\Container\container INFO"

# Get most recent year
year_path = os.path.join(dropbox_path, "2026年")
container_path = os.path.join(year_path, "F26123")  # Use a known container

excel_file = os.path.join(container_path, "F26123PACKINGLIST.xlsx")

if os.path.exists(excel_file):
    df = pd.read_excel(excel_file, sheet_name='Packing List', header=None)
    
    # Print column structure
    print("Column count:", len(df.columns))
    print("\nFirst 10 rows:")
    for i in range(min(10, len(df))):
        print(f"\nRow {i}:")
        for j in range(len(df.columns)):
            print(f"  Col {j}: {repr(df.iloc[i, j])}")
else:
    print(f"File not found: {excel_file}")
    # List what containers exist
    if os.path.exists(year_path):
        containers = os.listdir(year_path)
        print(f"Containers in 2026: {containers[:5]}")
