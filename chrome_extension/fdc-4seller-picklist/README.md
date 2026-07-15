# FDC 4Seller Picklist Locations (Chrome Extension)

Adds warehouse **Location** data to 4Seller picklists, shown between **库存SKU** and **数量**.

## How it works

1. Open the 4Seller picklist PDF in your browser.
2. Click the extension icon → **Load Picklist**.
3. Review the table preview with locations from FDC.
4. Click **Export to PDF** → a new tab opens with the formatted picklist.
5. Click **Print / Save as PDF** in that tab to save the final PDF.

## Install

1. Open Chrome → `chrome://extensions`
2. Enable **Developer mode**
3. Click **Load unpacked**
4. Select folder: `chrome_extension/fdc-4seller-picklist`

Or download the zip from **Warehouse Map** → **4Seller Extension**.

## Notes

- SKUs like `N1223 BK` are merged correctly even when split across PDF text tokens.
- Location lookup uses `https://www.fdcef.com/4seller/picklist-locations`.
- Export uses a print-ready HTML page so Chinese headers (`库存SKU`, `数量`) render correctly.
