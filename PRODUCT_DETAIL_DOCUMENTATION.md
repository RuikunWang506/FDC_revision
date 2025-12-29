# Product Detail Feature Documentation

## Overview
The Product Detail feature provides a comprehensive view of products organized by **Style** and **Color variants**. It displays inventory data from both **FDC** and **E_FAS** databases with aggregated totals.

## File Locations

### Backend
- **Blueprint**: `py/search/product_detail.py`
- **Route**: `/productdetail`

### Frontend
- **Template**: `templates/product_detail.html`

### Routes Registration
- **File**: `routes.py` (line 24-25)
- Registered as: `product_detail_bp`

## Features

### 1. Search Functionality
- Search by **SKU** (exact or partial match)
- Search by **Style** (ALT_CD)
- Multiple searches: separate by commas (e.g., "M1141, M1142 BK")
- Case-insensitive search
- Both POST (form) and GET (URL parameter) methods supported

### 2. Data Organization

#### Style Card (Parent Level)
Displays information that applies to the entire style:
- **Style Number**: e.g., M1141
- **Container Description**
- **Box Weight** (CASE_WT)
- **Piece Weight** (UT_WT)
- **Dimensions**: Length × Width × Height
- **Case Quantity** (PC_CASE)
- **MSRP** (RETAIL_PRS)
- **Cost** (SALES_COST)

#### Style Totals (Aggregated)
Combined metrics across all colors:
- **Stock**: Total in_stock
- **Sold**: Total ORDER_QTY
- **Available**: Total (in_stock - order_qty)
- **Incoming**: Total on_order_qty

#### Color Variants (Child Level)
For each color within a style (e.g., M1141 BK, M1141 BR):
- **Color Name**
- **SKU** (PROD_CD)
- **FDC Database Data**:
  - Stock, Sold, Available, Incoming
- **E_FAS Database Data**:
  - Stock, Sold, Available, Incoming
- **Combined Total**: Sum of both databases

### 3. Database Integration

#### Queried Tables
- `dbo.inv` - Inventory master
- `dbo.inv_data` - Inventory levels
- `inv_detl` - Inventory details (dimensions, weights)
- `wm_skufile` - SKU file (for create_dt)

#### Databases
- **FDC**: `omsdata_FDC`
- **E_FAS**: `oms_EF`

#### Query Fields
```
SKU (PROD_CD)
Style (ALT_CD)
Container (DESCRIP)
Stock (in_stock)
Sold (ORDER_QTY)
Available (in_stock - order_qty)
Incoming (on_order_qty)
Cost (SALES_COST)
MSRP (RETAIL_PRS)
Case Qty (PC_CASE)
Box Weight (CASE_WT)
Piece Weight (UT_WT)
Color (UNIT_COLOR)
Length (ut_len)
Width (ut_wi)
Height (UT_hi)
Last Activity (LT_SL_DT)
Create Date (create_dt)
```

## Data Structure

### Python Data Structure

```python
{
    'style': 'M1141',
    'parent_info': {
        'style': 'M1141',
        'container': 'Description...',
        'box_wt': 2.5,
        'pc_wt': 0.5,
        'len': 10,
        'wid': 8,
        'hei': 6,
        'case_qty': 50,
        'msrp': 19.99,
        'cost': 8.99,
        'dimensions': '10x8x6'
    },
    'colors': [
        {
            'color': 'BK',
            'sku': 'M1141 BK',
            'fdc': {
                'stock': 100,
                'sold': 20,
                'available': 80,
                'incoming': 50,
                'cost': 8.99,
                'msrp': 19.99,
                'last_activity': '01/15/2025',
                'create_dt': '12/20/2024'
            },
            'efas': {
                'stock': 75,
                'sold': 15,
                'available': 60,
                'incoming': 30,
                'cost': 8.99,
                'msrp': 19.99,
                'last_activity': '01/14/2025',
                'create_dt': '12/19/2024'
            },
            'totals': {
                'fdc': {'stock': 100, 'sold': 20, 'available': 80, 'incoming': 50},
                'efas': {'stock': 75, 'sold': 15, 'available': 60, 'incoming': 30},
                'combined': {'stock': 175, 'sold': 35, 'available': 140, 'incoming': 80}
            }
        }
    ],
    'totals': {
        'fdc': {'stock': 100, 'sold': 20, 'available': 80, 'incoming': 50},
        'efas': {'stock': 75, 'sold': 15, 'available': 60, 'incoming': 30},
        'combined': {'stock': 175, 'sold': 35, 'available': 140, 'incoming': 80}
    }
}
```

## API Endpoints

### 1. Main Route
**GET/POST** `/productdetail`
- Display the search page
- Form submission for search

### 2. URL Parameter Route
**GET** `/productdetail/<search_input>`
- Direct URL search
- Example: `/productdetail/M1141`

### 3. AJAX Search API
**POST** `/api/product/search`
- Content-Type: application/json
- Request body: `{"search_input": "M1141"}`
- Response: JSON with product data

## HTML Features

### Layout Components
1. **Search Section**: Purple gradient header with search form
2. **Info Box**: Shows count of styles and color variants found
3. **Error Messages**: Displayed if search yields no results
4. **Empty State**: Shown when page first loads
5. **Style Cards**: Main container for each style
6. **Color Cards**: Grid layout showing each color variant

### Responsive Design
- **Desktop**: Full grid layout
- **Tablet**: Adjusted grid columns
- **Mobile**: Single column layout

### Color Indicators
- **Positive values**: Green text (available > 0)
- **Negative values**: Red text (available < 0)
- **Style color**: Purple/Gradient accents

## Key Functions

### `get_db_connections()`
Establishes connections to both FDC and E_FAS databases.

### `search_product_data(search_input)`
- Parses search input (comma-separated)
- Queries both databases
- Groups results by style
- Calculates aggregates
- Returns organized structure

### `group_by_style(results)`
- Organizes flat results into hierarchical structure
- Groups colors by style
- Calculates style-level and color-level totals
- Handles missing data gracefully

## UI Features

### Search Form
- Clear input placeholder
- Supports multiple searches
- Visual search icon
- Responsive layout

### Product Cards
- Hover effects
- Color-coded sections (FDC vs E_FAS)
- Clear data hierarchy
- Status indicators (🚢 for incoming)

### Information Display
- Formatted numbers
- Currency formatting for prices
- Dimension displays
- Status badges

## Error Handling
- Database connection failures logged
- No products found shows helpful message
- Graceful fallback for missing data fields
- SQL injection protection via parameterized queries

## Usage Examples

### Search Scenarios
1. **Single SKU**: "M1141" - Shows all colors of M1141
2. **Specific Color SKU**: "M1141 BK" - Shows specific color
3. **Multiple Styles**: "M1141, M1142" - Shows both styles
4. **Partial Match**: "M114" - Shows all styles starting with M114
5. **Wildcard Search**: "%set%" - Shows fuzzy matching (if supported)

## Security
- Login required via `@login_required` decorator
- SQL injection prevention with parameterized queries
- XSS protection via Jinja2 template escaping
- Session-based user tracking

## Future Enhancements
- Export to Excel/CSV
- Historical data trends
- Warehouse location information
- Pricing history
- Stock level alerts
- Bulk operations
- Advanced filters

## Testing
To test the feature:
1. Navigate to `/productdetail`
2. Enter a search term (e.g., "M1141")
3. View the organized results
4. Click on different sections to verify data display

## Troubleshooting

### No Results Found
- Check spelling of SKU/Style
- Verify products exist in database
- Try partial match

### Database Connection Error
- Verify SQL Server is running
- Check database credentials in Config
- Check network connectivity

### Missing Data Fields
- Some fields may be NULL in database
- Template handles missing data gracefully
- No errors displayed to user
