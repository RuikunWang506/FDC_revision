 Product Detail - API & Endpoints Reference

## Endpoints Overview

```
GET/POST /productdetail              → Main search page
GET      /productdetail/<search>     → Direct URL search
POST     /api/product/search         → AJAX API endpoint
```

## 1. Main Search Page Endpoint

### Route: `/productdetail`

**Methods**: GET, POST

**Description**: Main page for product detail search and display

#### GET Request
```
GET /productdetail
```
Returns: Empty search page

#### POST Request (Form Submission)
```
POST /productdetail
Content-Type: application/x-www-form-urlencoded

search_input=M1141
```

**Form Parameters:**
- `search_input` (string): SKU, Style, or comma-separated list

**Returns**: HTML page with search form and results

#### Response Structure
```html
<!DOCTYPE html>
<html>
  <head>
    <title>Product Detail</title>
    <!-- Bootstrap, Font Awesome, Custom Styles -->
  </head>
  <body>
    <!-- Search Section -->
    <!-- Info Box -->
    <!-- Product Cards -->
      <!-- Style Card (Parent) -->
      <!-- Color Cards (Children) -->
  </body>
</html>
```

---

## 2. URL Parameter Search Endpoint

### Route: `/productdetail/<search_input>`

**Methods**: GET

**Description**: Direct search via URL parameter

#### Request Examples
```
GET /productdetail/M1141
GET /productdetail/M1141%20BK
GET /productdetail/M1141,M1142
```

**URL Parameters:**
- `search_input` (string): Search term or comma-separated terms

**Returns**: HTML page with pre-populated search and results

#### Advantages
- Shareable URLs
- Bookmark-friendly
- Direct browser navigation

---

## 3. AJAX API Endpoint

### Route: `/api/product/search`

**Methods**: POST

**Description**: API endpoint for JavaScript/AJAX requests

#### Request
```javascript
POST /api/product/search
Content-Type: application/json
Authorization: Session Cookie (user must be logged in)

{
  "search_input": "M1141"
}
```

**Request Body:**
```json
{
  "search_input": "M1141"
}
```

#### Response - Success (200 OK)
```json
{
  "success": true,
  "data": [
    {
      "style": "M1141",
      "parent_info": {
        "style": "M1141",
        "container": "Style Description",
        "box_wt": 2.5,
        "pc_wt": 0.5,
        "len": 10,
        "wid": 8,
        "hei": 6,
        "case_qty": 50,
        "msrp": 19.99,
        "cost": 8.99,
        "dimensions": "10x8x6"
      },
      "colors": [
        {
          "color": "BK",
          "sku": "M1141 BK",
          "fdc": {
            "stock": 100,
            "sold": 20,
            "available": 80,
            "incoming": 50,
            "cost": 8.99,
            "msrp": 19.99,
            "last_activity": "01/15/2025",
            "create_dt": "12/20/2024"
          },
          "efas": {
            "stock": 75,
            "sold": 15,
            "available": 60,
            "incoming": 30,
            "cost": 8.99,
            "msrp": 19.99,
            "last_activity": "01/14/2025",
            "create_dt": "12/19/2024"
          },
          "totals": {
            "fdc": {
              "stock": 100,
              "sold": 20,
              "available": 80,
              "incoming": 50
            },
            "efas": {
              "stock": 75,
              "sold": 15,
              "available": 60,
              "incoming": 30
            },
            "combined": {
              "stock": 175,
              "sold": 35,
              "available": 140,
              "incoming": 80
            }
          }
        }
      ],
      "totals": {
        "fdc": {
          "stock": 100,
          "sold": 20,
          "available": 80,
          "incoming": 50
        },
        "efas": {
          "stock": 75,
          "sold": 15,
          "available": 60,
          "incoming": 30
        },
        "combined": {
          "stock": 175,
          "sold": 35,
          "available": 140,
          "incoming": 80
        }
      }
    }
  ]
}
```

#### Response - Error (400 Bad Request)
```json
{
  "success": false,
  "error": "Search input required"
}
```

#### Response - No Results (200 OK)
```json
{
  "success": false,
  "error": "No products found"
}
```

#### Response - Server Error (500 Internal Server Error)
```json
{
  "success": false,
  "error": "Error message here"
}
```

---

## API Usage Examples

### JavaScript/jQuery Example
```javascript
// AJAX search
function searchProduct() {
  const searchInput = document.getElementById('search_input').value;
  
  fetch('/api/product/search', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json'
    },
    body: JSON.stringify({
      search_input: searchInput
    })
  })
  .then(response => response.json())
  .then(data => {
    if (data.success) {
      console.log('Products found:', data.data);
      // Process and display data
    } else {
      console.error('Error:', data.error);
    }
  })
  .catch(error => console.error('Request failed:', error));
}
```

### Python/Requests Example
```python
import requests

url = 'http://localhost:5000/api/product/search'
headers = {'Content-Type': 'application/json'}
data = {'search_input': 'M1141'}

# Note: Must have authenticated session
response = requests.post(url, json=data, cookies=cookies)

if response.status_code == 200:
    result = response.json()
    if result['success']:
        products = result['data']
        print(f"Found {len(products)} styles")
    else:
        print(f"Error: {result['error']}")
```

### cURL Example
```bash
curl -X POST http://localhost:5000/api/product/search \
  -H "Content-Type: application/json" \
  -d '{"search_input": "M1141"}' \
  -b "session_cookie_value"
```

---

## Search Input Formats

### Single Style
```
M1141
```
Returns: All colors of style M1141

### Specific Color SKU
```
M1141 BK
```
Returns: Only black variant of M1141

### Multiple Styles
```
M1141, M1142
M1141, M1142, M1143
```
Returns: All entered styles with all their colors

### Partial Match
```
M11
```
Returns: All styles starting with M11

### Multiple Partial Matches
```
M11, F26
```
Returns: All styles matching either pattern

---

## Query Parameters

The search accepts comma-separated values and supports:

1. **Exact Matches**
   - `M1141` → Find style M1141 exactly
   - `M1141 BK` → Find SKU M1141 BK exactly

2. **Prefix Matches**
   - `M11` → Find all styles starting with M11
   - Automatically adds `%` suffix

3. **Case Insensitive**
   - `m1141` → Same as `M1141`
   - `m1141 bk` → Same as `M1141 BK`

4. **Wildcard Support**
   - `%set%` → Fuzzy search containing "set"
   - `%BK%` → Find items containing "BK"

---

## HTTP Status Codes

| Code | Meaning | Scenario |
|------|---------|----------|
| 200 | OK | Successful request, results found or empty |
| 400 | Bad Request | Missing search_input parameter |
| 401 | Unauthorized | User not logged in |
| 500 | Server Error | Database connection failed |

---

## Response Headers

```
Content-Type: application/json
Cache-Control: no-cache, no-store, must-revalidate
Pragma: no-cache
Expires: 0
```

---

## Rate Limiting

- No explicit rate limiting implemented
- Database connection pool limits apply
- Consider adding for production deployment

---

## Authentication

All endpoints require:
- Valid Flask session (user must be logged in)
- Cookie: `session` (Flask session ID)
- Decorator: `@login_required`

**Login endpoint**: `/login` (handled by auth_bp)

---

## CORS Support

- Not enabled by default
- Add Flask-CORS if cross-origin requests needed

---

## Data Validation

- Search input: Required, non-empty string
- Max search terms: Unlimited (but performance may degrade)
- Special characters: Allowed, safe via parameterized queries

---

## Database Queries

### Databases Queried
1. **FDC**: `omsdata_FDC`
2. **E_FAS**: `oms_EF`

### Tables Queried
- `dbo.inv`
- `dbo.inv_data`
- `inv_detl`
- `wm_skufile`

### Query Optimization
- Uses JOINS for efficiency
- Parameterized queries prevent SQL injection
- Indexes on PROD_CD, ALT_CD recommended

---

## Caching

- No server-side caching implemented
- Each request queries fresh database
- Consider Redis caching for high-traffic scenarios

---

## Logging

All requests logged via Flask logger:
- Database queries logged at DEBUG level
- Errors logged at ERROR level
- Check `Config.LOG_FILE` for logs

---

## Error Handling

### Common Errors

**"No products found"**
- Search term doesn't match any products
- Check spelling, try partial match

**"Database connection error"**
- SQL Server unreachable
- Check server status and credentials

**"Search input required"**
- API call missing `search_input` field
- POST body must include search term

**Unauthorized (401)**
- Session expired
- User not logged in
- Must access `/login` first

---

## Testing

### Manual Testing Steps
1. Navigate to `/productdetail`
2. Enter search term
3. Click Search button
4. Verify results display correctly

### API Testing with Postman
```
Method: POST
URL: http://localhost:5000/api/product/search
Headers: Content-Type: application/json
Body (JSON):
{
  "search_input": "M1141"
}
```

### Load Testing
```bash
# Using Apache Bench
ab -n 100 -c 10 \
  -H "Cookie: session=YOUR_SESSION_ID" \
  http://localhost:5000/productdetail
```

---

## Future API Enhancements

- [ ] Pagination support
- [ ] Sorting options
- [ ] Filter by database
- [ ] Price range filters
- [ ] Stock level filters
- [ ] Export to CSV/Excel
- [ ] Historical comparisons
- [ ] Bulk operations

---

## Version History

- **v1.0** (Dec 2025): Initial release
  - Basic search functionality
  - Dual database support
  - Style/Color organization
  - AJAX API endpoint

---

## Support & Questions

For endpoint questions:
1. Check this reference guide
2. Review template code for examples
3. Check Python code for implementation details
4. Review server logs for error details

**Documentation Files:**
- `PRODUCT_DETAIL_IMPLEMENTATION.md` - Overview
- `PRODUCT_DETAIL_DOCUMENTATION.md` - Detailed docs
- `PRODUCT_DETAIL_QUICK_REFERENCE.md` - Quick lookup

---

**Last Updated**: December 2025
