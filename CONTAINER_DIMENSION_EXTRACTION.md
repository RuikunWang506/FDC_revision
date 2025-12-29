# Container File Processing - Dimension Extraction Solution

## Overview
Successfully implemented carton dimension extraction from packing lists in the FDC Flask application. The solution reads Excel container files, extracts carton dimensions in L×W×H format, and maps them to product styles for CSV generation.

## Key Implementation Details

### 1. **Packing List Sheet Reading**
- **Header Row**: Row 6 (0-indexed) in Excel files, using `pd.read_excel(file, header=6)`
- **File Structure**: 
  - Rows 1-5: Metadata (company info, container details)
  - Row 6: Column headers (in Excel row 7)
  - Rows 7+: Data rows with product and dimension information
- **Sheet Name Fallback**: Tries 'Packing List', 'Placking List', or first sheet (index 0)
- **Validation**: Confirms headers contain 'STYLE', 'CTN', or 'MEASUREMENT' keywords

### 2. **Dimension Extraction Strategy**
**Selection Criteria:**
- Only rows where `QTY/CTN > 1` (excludes single-pack entries like qty=1)
- When multiple entries exist for same style/color, prefer highest `TOTAL` value
- Handles duplicate column names (pandas adds `.1`, `.2` suffixes)

**Dimension Parsing:**
- Source: Second "MEASUREMENT" column (carton dimensions, e.g., "22.8*18.5*28")
- Format: `Length*Width*Height` separated by asterisks
- Conversion: String split and float conversion for each component

**Example Flow:**
```
Input row:  'N2532 BK' with QTY/CTN=20, TOTAL=480, Measurement="22.8*18.5*28"
Output: {'N2532 BK': {'length': 22.8, 'width': 18.5, 'height': 28.0}}
```

### 3. **Style/Color Matching**
- **Key Format**: `"{STYLE_NO} {COLOR}"` (e.g., "N2532 BK")
- **Parsing**: Split "STYLE NO" column from right to separate style and color
- **Lookup**: Match System File tab entries (style + color) to packing list dimensions
- **Fallback**: Empty string if no dimension mapping found

### 4. **Column Identification**
Automatically locates required columns by keyword matching:
- **Style Column**: Contains "STYLE" in name
- **QTY/CTN Column**: Contains "QTY" AND "CTN" (first match)
- **TOTAL Column**: Exact match for "TOTAL"
- **Measurement Columns**: All columns containing "MEASUREMENT"
  - Uses first for unit dimensions (e.g., "11*4.5*7.5")
  - Uses second for carton dimensions (e.g., "22.8*18.5*28")

## Code Changes

### File: `py/search/container.py`

**Changes to `process_container_file()` function:**

1. **Packing List Reading** (Lines 82-104):
   ```python
   for sheet_name in ['Packing List', 'Placking List', 0]:
       temp_df = pd.read_excel(file, sheet_name=sheet_name, header=6)
       col_str = ' '.join([str(c).upper() for c in temp_df.columns])
       if 'STYLE' in col_str or 'CTN' in col_str or 'MEASUREMENT' in col_str:
           PackingList = temp_df
           break
   ```

2. **Dimension Extraction** (Lines 139-195):
   - Identifies all required columns (Style, QTY/CTN, TOTAL, Measurements)
   - Iterates through packing list rows
   - Filters: Only include rows where QTY/CTN > 1
   - Parses: Splits "L*W*H" format into separate values
   - Deduplication: Keeps highest TOTAL value per style/color
   - Logging: Detailed debug info for troubleshooting

3. **Mapping to System File** (Lines 197-208):
   - Matches System File tab entries to packing list dimensions
   - Populates case_length_list, case_width_list, case_height_list
   - Fallback to empty string if no match found

## Test Results

✓ **All 9 test styles successfully processed**
- System File: 9 unique style/color combinations
- Packing List: 11 data rows with multiple entries per style
- Extracted: 9 dimension mappings with correct L×W×H values
- Mapped: 100% success rate (all 9 styles matched and populated)

**Test Data Example:**
```
N2532 BK: Length=22.8, Width=18.5, Height=28.0 (QTY/CTN=20, TOTAL=480)
N2532 L.TN: Length=22.8, Width=18.5, Height=28.0 (QTY/CTN=8, TOTAL=192)
N2534 BK: Length=22.8, Width=18.5, Height=28.0 (QTY/CTN=20, TOTAL=480)
```

## Features

✅ **Robust Error Handling**
- Multiple sheet name fallbacks
- Column detection by keywords (flexible to order changes)
- Safe float conversion with try/except
- Detailed logging for debugging

✅ **Flexible Data Handling**
- Handles duplicate column names
- Supports missing measurements (fills with empty string)
- Trims whitespace from values
- Validates format before parsing

✅ **Preferred Row Selection**
- Excludes single-pack entries (QTY/CTN = 1)
- Uses highest TOTAL when multiple rows exist
- Stores only one dimension per style/color combination

## Logging

Debug logs generated during processing:
- Sheet identification success/failure
- Column identification (Style, QTY/CTN, TOTAL, Measurements)
- Each dimension stored (style, color, L, W, H, TOTAL, QTY/CTN)
- Final count of extracted dimensions
- Mapping results for each style

Example:
```
Successfully read PackingList from sheet Packing List with header=6
PackingList shape: (11, 13)
DEBUG: Stored N2532 BK: L=22.8, W=18.5, H=28.0 (QTY/CTN=20.0, TOTAL=480)
DEBUG: Final packing_dict with 9 entries
```

## CSV Output

The dimensions are now included in the generated CSV files:
- **step3 CSV**: Each row includes Case Length, Case Height, Case Width
- **step4 CSV**: PO-level summary with dimensions

## Future Enhancements

1. **Measurement Unit Handling**: Currently assumes inches; could support cm, mm
2. **Batch Processing**: Optimize for files with 100+ styles
3. **Validation Report**: Generate summary report of extracted vs expected dimensions
4. **Override Mechanism**: Allow manual dimension overrides if extraction fails
5. **Dimension History**: Track dimension changes across container uploads

## Troubleshooting

If dimensions aren't being extracted:

1. **Check Sheet Name**: Verify packing list sheet is named 'Packing List' or 'Placking List'
2. **Verify Header Row**: Ensure headers are in row 7 (0-indexed row 6)
3. **Validate Format**: Confirm measurements are "L*W*H" format (e.g., "22.8*18.5*28")
4. **QTY/CTN Filter**: Ensure main entries have QTY/CTN > 1 (single packs are skipped)
5. **Check Logs**: Review debug output for column identification issues

---

**Status**: ✓ COMPLETE AND TESTED
**Last Updated**: 2025-12-29
**Location**: `py/search/container.py` (process_container_file function)
