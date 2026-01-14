import pandas as pd
import glob
import os
import sys

# Set encoding
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

# Correct Dropbox path
dropbox_path = r"C:\Users\stream\Dropbox\Pictures-China (1)\container catalog\Container\container INFO"

print(f"Checking path: {dropbox_path}")
print(f"Path exists: {os.path.exists(dropbox_path)}")

if os.path.exists(dropbox_path):
    # Find all year folders
    year_folders = sorted([d for d in os.listdir(dropbox_path) if os.path.isdir(os.path.join(dropbox_path, d))], reverse=True)
    print(f"Year folders found: {len(year_folders)}")
    
    if year_folders:
        year_path = os.path.join(dropbox_path, year_folders[0])
        print(f"Using year: {year_folders[0]}")
        
        # Find container folders (F/FT prefix)
        try:
            contents = os.listdir(year_path)
            container_dirs = sorted([d for d in contents if os.path.isdir(os.path.join(year_path, d)) and d[0] in ['F', 'f']])
            
            if container_dirs:
                # Take first container
                container_name = container_dirs[0]
                container_dir = os.path.join(year_path, container_name)
                print(f"Using container: {container_name}")
                
                # Find Excel files
                excel_files = glob.glob(os.path.join(container_dir, "*.xlsx"))
                if excel_files:
                    excel_file = excel_files[0]
                    print(f"Reading: {os.path.basename(excel_file)}")
                    
                    # Read sheet names
                    try:
                        xls = pd.ExcelFile(excel_file)
                        print(f"Sheet names: {xls.sheet_names}")
                        
                        # Try to read packing list
                        for sheet_name in ['Packing List', 'Placking List', 'System File']:
                            try:
                                df = pd.read_excel(excel_file, sheet_name=sheet_name, header=None)
                                print(f"\n=== Sheet: {sheet_name} ===")
                                print(f"Shape: {df.shape}")
                                print(f"\nFirst 12 rows (col 0-7):")
                                for i in range(min(12, len(df))):
                                    row_data = []
                                    for j in range(min(8, len(df.columns))):
                                        val = df.iloc[i, j]
                                        row_data.append(str(val)[:15] if pd.notna(val) else '')
                                    print(f"Row {i}: {row_data}")
                                break
                            except Exception as e:
                                pass
                    except Exception as e:
                        print(f"Error: {e}")
        except Exception as e:
            print(f"Error checking: {e}")
