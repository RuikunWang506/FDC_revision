 Product Detail - Quick Reference

## What is Product Detail?
A new feature that shows comprehensive product information organized by **Style** (parent) and **Color** (children), with inventory data from both FDC and E_FAS databases.

## Accessing the Feature
- **URL**: `http://yourserver/productdetail`
- **Search form**: Enter SKU or Style name
- **Multiple searches**: Separate by commas (e.g., "M1141, M1142")

## Data Layout

### Level 1: Style (Parent Card)
Shows information common to all colors of a style:
```
M1141
├─ Container: [Description]
├─ Box Weight: 2.5 lbs
├─ Piece Weight: 0.5 lbs
├─ Dimensions: 10 × 8 × 6
├─ Case Qty: 50
├─ MSRP: $19.99
├─ Cost: $8.99
└─ Style Totals (All Colors Combined):
   ├─ Stock: 175
   ├─ Sold: 35
   ├─ Available: 140
   └─ Incoming: 80 🚢
```

### Level 2: Color (Child Cards)
Shows details for each color variant:
```
M1141 BK (Color Card)
├─ FDC Database:
│  ├─ Stock: 100
│  ├─ Sold: 20
│  ├─ Available: 80
│  └─ Incoming: 50 🚢
├─ E_FAS Database:
│  ├─ Stock: 75
│  ├─ Sold: 15
│  ├─ Available: 60
│  └─ Incoming: 30 🚢
└─ Combined Total:
   ├─ Stock: 175
   ├─ Sold: 35
   ├─ Available: 140
   └─ Incoming: 80 🚢
```

## Key Features

✓ **Dual Database Support**: Shows FDC and E_FAS data side-by-side  
✓ **Aggregated Totals**: Style-level and color-level summaries  
✓ **Multiple Searches**: Search by SKU, Style, or both  
✓ **Responsive Design**: Works on desktop, tablet, and mobile  
✓ **Color-coded Metrics**: Positive (green) and negative (red) values  
✓ **Visual Hierarchy**: Parent/child organization with clear sections  

## Search Examples

| Search | Result |
|--------|--------|
| `M1141` | All colors of style M1141 |
| `M1141 BK` | Only black variant of M1141 |
| `M1141, M1142` | Both styles M1141 and M1142 |
| `M11` | All styles starting with M11 |

## Database Fields Displayed

### From Style (ALT_CD):
- Box Weight (CASE_WT)
- Piece Weight (UT_WT)
- Length, Width, Height (ut_len, ut_wi, UT_hi)
- Case Quantity (PC_CASE)
- MSRP (RETAIL_PRS)
- Cost (SALES_COST)

### From Each Color (PROD_CD):
- Stock (in_stock)
- Sold (ORDER_QTY)
- Available (in_stock - order_qty)
- Incoming (on_order_qty)

## File Structure

```
FDC_FLASK_APP/
├── py/search/
│   └── product_detail.py          (Backend blueprint with functions)
├── templates/
│   └── product_detail.html        (Frontend template)
└── routes.py                      (Blueprint registration)
```

## Backend Functions

### `search_product_data(search_input)`
- Input: Search string (e.g., "M1141, M1142")
- Process: Queries both databases, groups by style/color
- Output: Organized nested data structure

### `group_by_style(results)`
- Input: Flat list of results from database
- Process: Groups by style, aggregates totals
- Output: Hierarchical structure for template

## Template Variables

```jinja2
product_data[]
├── style: String (e.g., "M1141")
├── parent_info: Dict
│   ├── box_wt, pc_wt, len, wid, hei
│   ├── case_qty, msrp, cost
│   └── container
├── colors[]: Array of color objects
│   ├── color: String (e.g., "BK")
│   ├── sku: String (e.g., "M1141 BK")
│   ├── fdc: Dict with stock/sold/available/incoming
│   ├── efas: Dict with stock/sold/available/incoming
│   └── totals: Dict with fdc/efas/combined totals
└── totals: Dict with fdc/efas/combined totals
```

## Color Visual Indicators

- **Green Text**: Available quantity > 0 (healthy stock)
- **Red Text**: Available quantity < 0 (oversold/issue)
- **Blue Text**: Combined totals (aggregated across databases)

## Missing Data Handling

If a field is not available in the database:
- Shows "N/A" in gray text
- Does not break the page
- Gracefully skips the display

## Performance Notes

- Queries both databases in parallel
- Efficient grouping and aggregation in Python
- Large result sets (100+ styles) display smoothly
- Database connections properly closed

## Security

- Requires login (session-based authentication)
- SQL injection protected (parameterized queries)
- XSS protected (Jinja2 template escaping)
- CSRF protected (Flask default)

## Common Issues & Solutions

| Issue | Solution |
|-------|----------|
| No results | Check spelling, try partial search |
| Slow loading | Database connection may be slow, try smaller searches |
| Missing data | Fields may be NULL in database, show as "N/A" |
| Connection error | Check SQL Server status and credentials |

## Integration Points

- Uses existing Config for database credentials
- Uses existing login_required decorator
- Uses existing navbar.html template
- Bootstrap and jQuery for styling/interaction
- Font Awesome for icons

## Example URLs

- Search page: `/productdetail`
- Direct search: `/productdetail/M1141`
- AJAX API: `POST /api/product/search` (JSON)

## Next Steps / Enhancement Ideas

1. Add export to Excel
2. Add price history chart
3. Add warehouse location details
4. Add bulk update capabilities
5. Add favorites/bookmarks
6. Add stock level alerts
