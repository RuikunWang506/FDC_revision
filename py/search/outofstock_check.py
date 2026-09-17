from flask import Blueprint, request, jsonify, session, redirect, url_for, current_app
from py.utilities.database_utils import get_mysql_connection, get_sql_server_connection_fdc
from py.utilities.ocean_qty import PLOG_OCEAN_JOIN, ocean_qty_sql_sum
from py.utilities.setting import login_required

outofstock_check_bp = Blueprint('outofstock_check', __name__)

_FDC_STOCK_CHUNK = 400


def get_db_connection():
    """Establishes a connection to the MySQL database."""
    return get_mysql_connection()

def get_fdc_connection():
    """Establishes a connection to the SQL Server FDC database."""
    return get_sql_server_connection_fdc()


def select_sold_out_located_styles(located_styles, stock_rows):
    """Return styles that had a warehouse location and are now sold out.

    A style qualifies when FDC available (in_stock - order_qty) is <= 0 and it
    was once received (stock or sold is non-zero). Styles missing from FDC are skipped.
    """
    located = {(s or "").strip() for s in located_styles if (s or "").strip()}
    sold_out = []
    seen = set()
    for row in stock_rows or []:
        style = (row[0] or "").strip() if row else ""
        if not style or style not in located or style in seen:
            continue
        stock = int(row[1] or 0)
        sold = int(row[2] or 0)
        available = int(row[3] or 0)
        if available <= 0 and (stock != 0 or sold != 0):
            sold_out.append(style)
            seen.add(style)
    return sold_out


def _fetch_located_styles(mysql_conn):
    cursor = mysql_conn.cursor()
    try:
        cursor.execute(
            """
            SELECT DISTINCT Style
            FROM products
            WHERE Location IS NOT NULL AND Location != ''
            ORDER BY Style
            """
        )
        return [(row[0] or "").strip() for row in cursor.fetchall() if row and (row[0] or "").strip()]
    finally:
        cursor.close()


def _fetch_fdc_stock_for_styles(fdc_conn, style_list):
    if not style_list:
        return []
    cursor = fdc_conn.cursor()
    try:
        rows = []
        for i in range(0, len(style_list), _FDC_STOCK_CHUNK):
            chunk = style_list[i : i + _FDC_STOCK_CHUNK]
            placeholders = ",".join(["?" for _ in chunk])
            stock_query = f"""
                SELECT
                    i.ALT_CD as Style,
                    SUM(CAST(ISNULL(id.in_stock, 0) AS INT)) as Stock,
                    SUM(CAST(ISNULL(id.order_qty, 0) AS INT)) as Sold,
                    SUM(CAST(ISNULL(id.in_stock, 0) - ISNULL(id.order_qty, 0) AS INT)) as Available
                FROM dbo.inv AS i
                LEFT JOIN dbo.inv_data AS id ON i.PROD_CD = id.Prod_cd
                WHERE i.ALT_CD IN ({placeholders})
                GROUP BY i.ALT_CD
            """
            cursor.execute(stock_query, chunk)
            rows.extend(cursor.fetchall() or [])
        return rows
    finally:
        cursor.close()


def _clear_locations_for_styles(mysql_conn, styles):
    if not styles:
        return 0
    cursor = mysql_conn.cursor()
    try:
        updated = 0
        for i in range(0, len(styles), _FDC_STOCK_CHUNK):
            chunk = styles[i : i + _FDC_STOCK_CHUNK]
            placeholders = ",".join(["%s" for _ in chunk])
            cursor.execute(
                f"""
                UPDATE products
                SET Location = '', Update_Date = NOW()
                WHERE Style IN ({placeholders})
                  AND Location IS NOT NULL AND Location != ''
                """,
                tuple(chunk),
            )
            updated += cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
        mysql_conn.commit()
        return updated
    except Exception:
        mysql_conn.rollback()
        raise
    finally:
        cursor.close()


def wipe_sold_out_locations(actor="cron", log_fn=None):
    """Clear MySQL product locations for styles that were received and are now sold out."""
    def _log(message):
        if log_fn:
            try:
                log_fn(message)
            except Exception:
                pass

    mysql_conn = None
    fdc_conn = None
    try:
        mysql_conn = get_db_connection()
        if mysql_conn is None:
            raise RuntimeError("Failed to connect to MySQL")
        located = _fetch_located_styles(mysql_conn)
        if not located:
            result = {"success": True, "style_count": 0, "rows_updated": 0, "styles": []}
            _log("OOS location wipe: no located styles")
            return result

        fdc_conn = get_fdc_connection()
        if fdc_conn is None:
            raise RuntimeError("Failed to connect to FDC")
        stock_rows = _fetch_fdc_stock_for_styles(fdc_conn, located)
        to_wipe = select_sold_out_located_styles(located, stock_rows)
        rows_updated = _clear_locations_for_styles(mysql_conn, to_wipe) if to_wipe else 0
        result = {
            "success": True,
            "style_count": len(to_wipe),
            "rows_updated": rows_updated,
            "styles": to_wipe,
        }
        _log(
            f"OOS location wipe actor={actor} styles={len(to_wipe)} rows={rows_updated}"
        )
        try:
            current_app.logger.info(
                "***{actor}***___@@@Wiped Sold-Out Locations@@@__ styles={count} rows={rows}".format(
                    actor=actor,
                    count=len(to_wipe),
                    rows=rows_updated,
                )
            )
        except Exception:
            pass
        return result
    finally:
        if fdc_conn is not None:
            try:
                fdc_conn.close()
            except Exception:
                pass
        if mysql_conn is not None:
            try:
                mysql_conn.close()
            except Exception:
                pass


@outofstock_check_bp.route('/oosc', methods=['GET'])
@login_required
def outofstock_check():
    """Legacy OOS page — sold-out location cleanup now runs from Location Service."""
    return redirect(url_for("location_massive_audit.location_massive_update"))


@outofstock_check_bp.route('/wipe_sold_out_locations', methods=['POST'])
@login_required
def wipe_sold_out_locations_route():
    """Manual bulk wipe for styles that were received and are now sold out."""
    actor = session.get("username", "Unknown")
    try:
        result = wipe_sold_out_locations(actor=actor)
        return jsonify(
            {
                "success": True,
                "message": (
                    f"Cleared locations for {result['style_count']} sold-out style(s) "
                    f"({result['rows_updated']} records)."
                    if result["style_count"]
                    else "No sold-out located styles to clear."
                ),
                "style_count": result["style_count"],
                "rows_updated": result["rows_updated"],
                "styles": result["styles"],
            }
        )
    except Exception as e:
        current_app.logger.error(f"Error wiping sold-out locations: {str(e)}")
        return jsonify({"success": False, "message": f"An error occurred: {str(e)}"}), 500

@outofstock_check_bp.route('/wipe_location', methods=['POST'])
@login_required
def wipe_location():
    """Wipe location for a given style"""
    try:
        data = request.get_json()
        style = data.get('style', '').strip()
        
        if not style:
            return jsonify({'success': False, 'message': 'Style is required'}), 400
        
        mysql_conn = get_db_connection()
        if mysql_conn is None:
            return jsonify({'success': False, 'message': 'Failed to connect to database'}), 500
        
        cursor = mysql_conn.cursor()
        
        # Update all products with this style to have empty location
        update_query = """
            UPDATE products 
            SET Location = '', Update_Date = NOW()
            WHERE Style = %s
        """
        cursor.execute(update_query, (style,))
        mysql_conn.commit()
        
        rows_affected = cursor.rowcount
        
        cursor.close()
        mysql_conn.close()
        
        if rows_affected > 0:
            current_app.logger.info(f"***{session.get('username', 'Unknown')}***___@@@Wiped Location@@@__ Style: {style}, Rows: {rows_affected}")
            return jsonify({
                'success': True, 
                'message': f'Location wiped for style {style} ({rows_affected} records updated)'
            })
        else:
            return jsonify({
                'success': False, 
                'message': f'No records found for style {style}'
            }), 404
        
    except Exception as e:
        current_app.logger.error(f"Error wiping location: {str(e)}")
        return jsonify({'success': False, 'message': f'An error occurred: {str(e)}'}), 500


@outofstock_check_bp.route('/get_style_skus', methods=['GET'])
@login_required
def get_style_skus():
    """Return SKU and Location list for a given style (lazy-loaded)."""
    try:
        style = request.args.get('style', '').strip()
        if not style:
            return jsonify({'error': 'Style parameter is required'}), 400

        mysql_conn = get_db_connection()
        if mysql_conn is None:
            return jsonify({'error': 'Database connection failed'}), 500

        cursor = mysql_conn.cursor(dictionary=True)
        query = "SELECT SKU, Location FROM products WHERE Style = %s ORDER BY SKU"
        cursor.execute(query, (style,))
        rows = cursor.fetchall()

        # If no SKUs, return empty
        sku_list = [r['SKU'] for r in rows] if rows else []

        # Fetch stock info from FDC SQL Server for these SKUs
        sku_data_map = {}
        if sku_list:
            try:
                fdc_conn = get_fdc_connection()
                if fdc_conn:
                    fdc_cursor = fdc_conn.cursor()
                    # Query PROD_CD rows that begin with the style code (covers variations like spaces/dashes)
                    like_pattern = style + '%'
                    sku_stock_query = f"""
                        SELECT i.PROD_CD as PROD_CD,
                            SUM(CAST(ISNULL(id.in_stock, 0) AS INT)) as Stock,
                            SUM(CAST(ISNULL(id.order_qty, 0) AS INT)) as Sold,
                            SUM(CAST(ISNULL(id.in_stock, 0) - ISNULL(id.order_qty, 0) AS INT)) as Available,
                            {ocean_qty_sql_sum()} as Ocean
                        FROM dbo.inv AS i
                        LEFT JOIN dbo.inv_data AS id ON i.PROD_CD = id.Prod_cd
                        {PLOG_OCEAN_JOIN}
                        WHERE i.PROD_CD LIKE ?
                        GROUP BY i.PROD_CD
                    """
                    fdc_cursor.execute(sku_stock_query, (like_pattern,))
                    stock_rows = fdc_cursor.fetchall()
                    # Build map keyed by normalized PROD_CD (upper, no spaces/dashes)
                    def norm(s):
                        return (s or '').upper().replace(' ', '').replace('-', '')

                    for r in stock_rows:
                        prod = r[0]
                        key = norm(prod)
                        sku_data_map[key] = {
                            'Stock': int(r[1]) if r[1] is not None else 0,
                            'Sold': int(r[2]) if r[2] is not None else 0,
                            'Available': int(r[3]) if r[3] is not None else 0,
                            'Ocean': int(r[4]) if r[4] is not None else 0,
                            'PROD_CD': prod
                        }
                    fdc_cursor.close()
                    fdc_conn.close()
            except Exception as e:
                current_app.logger.error(f"Error fetching SKU stock data from FDC: {e}")

        # Merge MySQL rows with stock map using normalization (remove spaces/dashes)
        merged = []
        def norm(s):
            return (s or '').upper().replace(' ', '').replace('-', '')

        for r in rows:
            sku = r.get('SKU')
            loc = r.get('Location')
            key = norm(sku)
            stock_info = sku_data_map.get(key)

            # fallback: if exact normalized SKU not found, try using first token (e.g., style only)
            if not stock_info and sku and ' ' in sku:
                first = norm(sku.split(' ')[0])
                stock_info = sku_data_map.get(first)

            if not stock_info:
                stock_info = {'Stock': 0, 'Sold': 0, 'Available': 0, 'Ocean': 0}

            merged.append({
                'SKU': sku,
                'Location': loc,
                'Stock': stock_info['Stock'],
                'Sold': stock_info['Sold'],
                'Available': stock_info['Available'],
                'Ocean': stock_info['Ocean']
            })

        cursor.close()
        mysql_conn.close()

        return jsonify({'skus': merged})
    except Exception as e:
        current_app.logger.error(f"Error getting SKUs for style {style}: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500
