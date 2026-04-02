# FDC Clipboard Product Helper (Chrome Extension)

This extension watches clipboard text while you browse, and if it finds something that looks like your style/SKU format (example: `T3305`, `M1141`, `EN1346 GY`), it shows a floating helper.

Click **Search Style** or **Search SKU** to open:

`https://www.fdcef.com/product_detail?search=<candidate>`

## Install

1. Open Chrome and go to `chrome://extensions`.
2. Turn on **Developer mode**.
3. Click **Load unpacked**.
4. Select folder:
   - `chrome_extension/fdc-product-clipboard`

## Use

1. Copy a style/SKU on any web page.
2. If it matches expected format, the helper appears in the bottom-right.
3. Click **Search Style** to search the detected style (example: `EN1346`).
4. Click **Search SKU** to search the full SKU text (example: `EN1346 GY`).

## Notes

- Clipboard reading is best when the tab is active/focused.
- If your browser blocks clipboard access on a page, copy once and refocus the tab.
- FDC pages still use your normal website authentication/session.
- The popup is suppressed when background validation finds `No products found matching your search.` for the detected value.
