import pyodbc
import pandas as pd

from datetime import datetime, timedelta
from flask import Blueprint, request, render_template, session, current_app, jsonify, redirect, url_for, send_file
import io
import re

# Import helper to fetch locations for SKUs so we can populate Location server-side
from py.other.label_status import enrich_header_info
from py.search.item_lookup import fetch_locations
from py.utilities.setting import Config, login_required

order_lookup_bp = Blueprint('order_lookup', __name__)

# Database configurations from settings
sql_server_config = Config.SQL_SERVER_CONFIG


def get_recent_fdc_orders(limit=100):
    """Return the most recent FDC sales orders with customer names."""
    orders = []
    try:
        conn = pyodbc.connect(
            f'DRIVER={sql_server_config["driver"]};SERVER={sql_server_config["server"]};'
            f'DATABASE=omsdata_FDC;UID={sql_server_config["username"]};PWD={sql_server_config["password"]}'
        )
        cursor = conn.cursor()
        cursor.execute(
            f"""
            SELECT TOP {int(limit)}
                o.ord_num,
                LTRIM(RTRIM(c.cus_nm)) AS customer_name,
                RIGHT('0' + CAST(MONTH(DATEADD(DAY, CAST(o.ord_dt AS INT) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                RIGHT('0' + CAST(DAY(DATEADD(DAY, CAST(o.ord_dt AS INT) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                CAST(YEAR(DATEADD(DAY, CAST(o.ord_dt AS INT) - 81088, '20230101')) AS VARCHAR) AS order_date,
                o.INVS_NUM
            FROM dbo.orders o
            INNER JOIN dbo.customer c ON o.cus_id = c.cus_id
            WHERE o.ord_num >= 10000 AND o.ord_num <= 99999 AND o.ORD_DT IS NOT NULL
            ORDER BY o.ORD_DT DESC, o.ord_num DESC
            """
        )
        for row in cursor.fetchall():
            invs = row[3]
            try:
                invs_num = int(invs) if invs is not None else 0
            except (TypeError, ValueError):
                invs_num = 0
            invoiced = invs_num > 0
            orders.append({
                'order_number': str(row[0]).strip(),
                'customer_name': (row[1] or '').strip(),
                'order_date': (row[2] or '').strip(),
                'invoice_number': str(invs_num) if invoiced else '',
                'invoiced': invoiced,
            })
        cursor.close()
        conn.close()
    except Exception as e:
        current_app.logger.error(f"Error fetching recent FDC orders: {e}")
    return orders


def _clean_ship_field(val) -> str:
    s = str(val or "").strip()
    if not s or s.lower() in ("none", "nan", "n/a"):
        return ""
    return s


def fetch_order_ship_to_lines(order_number: str | int) -> tuple[list[str] | None, str | None]:
    """
    Ship-to lines matching order lookup display: SP_CUS_NM, street lines, city/state/zip.
    """
    order_number = str(order_number).strip()
    if len(order_number) == 5:
        database_name = "omsdata_FDC"
    elif len(order_number) == 4:
        database_name = "oms_EF"
    else:
        return None, "Invalid order number."

    try:
        conn = pyodbc.connect(
            f'DRIVER={sql_server_config["driver"]};SERVER={sql_server_config["server"]};'
            f'DATABASE={database_name};UID={sql_server_config["username"]};PWD={sql_server_config["password"]}'
        )
        cur = conn.cursor()
        cur.execute(
            """
            SELECT SP_CUS_NM, SP_ADR, SP_ADR_2, SP_ADR_3,
                   SP_ADR_CT, SP_ADR_ST, SP_ADR_ZP, SP_ADR_CN
            FROM dbo.orders
            WHERE ord_num = ?
            """,
            (order_number,),
        )
        row = cur.fetchone()
        cur.close()
        conn.close()
        if not row:
            return None, f"Order {order_number} not found."

        cus_nm = _clean_ship_field(row[0])
        adr1 = _clean_ship_field(row[1])
        adr2 = _clean_ship_field(row[2])
        adr3 = _clean_ship_field(row[3])
        city = _clean_ship_field(row[4])
        state = _clean_ship_field(row[5])
        zip_code = _clean_ship_field(row[6])
        country = _clean_ship_field(row[7])

        lines = []
        if cus_nm:
            lines.append(cus_nm)
        if adr1 and adr1.upper() != cus_nm.upper():
            lines.append(adr1)
        if adr2:
            lines.append(adr2)
        if adr3:
            lines.append(adr3)
        elif city or state or zip_code:
            city_line = city
            if state:
                city_line = f"{city_line}, {state}" if city_line else state
            if zip_code:
                city_line = f"{city_line} {zip_code}".strip() if city_line else zip_code
            if city_line:
                lines.append(city_line)
        if country:
            lines.append(country)

        if not lines:
            return None, "Ship-to address is empty for this order."
        return lines, None
    except Exception as e:
        current_app.logger.error("Error fetching ship-to for order %s: %s", order_number, e)
        return None, str(e)


def parse_boxes_from_notes(notes: str | None) -> int | None:
    """Extract total box count from notes like '1 box' or '30 boxes, L-1'."""
    if not notes:
        return None
    match = re.search(r"(\d+)\s*box(?:es)?\b", str(notes), re.IGNORECASE)
    if not match:
        return None
    try:
        count = int(match.group(1))
    except ValueError:
        return None
    return count if count > 0 else None


def line_box_count(qty, per_box) -> int:
    """Boxes for one line: ceil(qty / per_box); if per_box is 0, treat each unit as one box."""
    qty = int(qty or 0)
    per_box = int(per_box or 0)
    if qty <= 0:
        return 0
    if per_box > 0:
        return (qty + per_box - 1) // per_box
    return qty


def full_box_count(qty, per_box) -> int:
    """Full cartons only (floor); used for display and footer totals."""
    qty = int(qty or 0)
    per_box = int(per_box or 0)
    if qty <= 0:
        return 0
    if per_box > 0:
        return qty // per_box
    return qty


def loose_piece_count(qty, per_box) -> int:
    """Leftover pieces after full cartons."""
    qty = int(qty or 0)
    per_box = int(per_box or 0)
    if qty <= 0 or per_box <= 0:
        return 0
    return qty % per_box


def format_boxes_display(qty, per_box) -> str:
    """Two-line Boxes cell: full count, then (loose N) for order lookup."""
    full = full_box_count(qty, per_box)
    loose = loose_piece_count(qty, per_box)
    return (
        '<span class="order-boxes-cell">'
        f'<span class="order-boxes-full">{full}</span>'
        f'<span class="order-boxes-loose">(loose {loose})</span>'
        '</span>'
    )


def fetch_order_notes(order_number: str | int) -> str:
    """Load order notes from dbo.ntfile (same source as order lookup header)."""
    order_number = str(order_number).strip()
    if len(order_number) == 5:
        database_name = "omsdata_FDC"
    elif len(order_number) == 4:
        database_name = "oms_EF"
    else:
        return ""
    try:
        conn = pyodbc.connect(
            f'DRIVER={sql_server_config["driver"]};SERVER={sql_server_config["server"]};'
            f'DATABASE={database_name};UID={sql_server_config["username"]};PWD={sql_server_config["password"]}'
        )
        cur = conn.cursor()
        cur.execute(
            """
            SELECT TOP 1 NOTES
            FROM dbo.ntfile
            WHERE NT_NUM = ?
              AND NOTES IS NOT NULL
              AND LTRIM(RTRIM(NOTES)) <> ''
            ORDER BY LEN(NOTES) DESC
            """,
            (order_number,),
        )
        row = cur.fetchone()
        cur.close()
        conn.close()
        return str(row[0]).strip() if row and row[0] else ""
    except Exception as e:
        current_app.logger.error("Error fetching order notes for %s: %s", order_number, e)
        return ""


def fetch_in_warehouse_by_skus(skus):
    """Fetch combined in-warehouse quantity per SKU from FDC and E_FAS databases.

    In Warehouse follows the product detail metric: in_stock - order_qty.
    Returns a mapping keyed by lowercase, stripped SKU.
    """
    stock_by_sku = {}

    if not skus:
        return stock_by_sku

    # Normalize and de-duplicate input SKUs while preserving order.
    cleaned_skus = []
    seen = set()
    for sku in skus:
        try:
            normalized = sku.replace('\u00A0', ' ').strip()
        except Exception:
            normalized = str(sku).strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            cleaned_skus.append(normalized)

    if not cleaned_skus:
        return stock_by_sku

    # Compare against uppercase-trimmed SKUs to match SQL Server values robustly.
    search_values = [s.upper() for s in cleaned_skus]
    placeholders = ','.join(['?'] * len(search_values))

    query = f"""
        SELECT
            LTRIM(RTRIM(i.PROD_CD)) AS sku,
            CAST(ISNULL(id.in_stock, 0) - ISNULL(id.order_qty, 0) AS INT) AS in_warehouse
        FROM dbo.inv i
        LEFT JOIN dbo.inv_data id ON i.PROD_CD = id.prod_cd
        WHERE UPPER(LTRIM(RTRIM(i.PROD_CD))) IN ({placeholders})
    """

    for database_name in ('omsdata_FDC', 'oms_EF'):
        conn = None
        cursor = None
        try:
            conn = pyodbc.connect(
                f'DRIVER={sql_server_config["driver"]};SERVER={sql_server_config["server"]};DATABASE={database_name};UID={sql_server_config["username"]};PWD={sql_server_config["password"]}'
            )
            cursor = conn.cursor()
            cursor.execute(query, search_values)
            for row in cursor.fetchall():
                sku_value = row[0]
                qty_value = row[1] if row[1] is not None else 0
                if not sku_value:
                    continue
                key = str(sku_value).strip().lower()
                stock_by_sku[key] = int(stock_by_sku.get(key, 0)) + int(qty_value)
        except Exception as e:
            current_app.logger.error(f"Error fetching in-warehouse stock from {database_name}: {e}")
        finally:
            if cursor:
                cursor.close()
            if conn:
                conn.close()

    return stock_by_sku


def fetch_case_weights_by_skus(skus, database_name: str) -> dict[str, float]:
    """CASE_WT (lbs per case/box) from dbo.inv, keyed by uppercase trimmed SKU."""
    if not skus or not database_name:
        return {}

    cleaned = []
    seen = set()
    for sku in skus:
        try:
            key = str(sku).replace("\u00a0", " ").strip().upper()
        except Exception:
            key = str(sku).strip().upper()
        if key and key not in seen:
            seen.add(key)
            cleaned.append(key)

    if not cleaned:
        return {}

    placeholders = ",".join(["?"] * len(cleaned))
    weights: dict[str, float] = {}
    conn = None
    cursor = None
    try:
        conn = pyodbc.connect(
            f'DRIVER={sql_server_config["driver"]};SERVER={sql_server_config["server"]};'
            f'DATABASE={database_name};UID={sql_server_config["username"]};PWD={sql_server_config["password"]}'
        )
        cursor = conn.cursor()
        cursor.execute(
            f"""
            SELECT UPPER(LTRIM(RTRIM(PROD_CD))) AS sku, CASE_WT
            FROM dbo.inv
            WHERE UPPER(LTRIM(RTRIM(PROD_CD))) IN ({placeholders})
            """,
            cleaned,
        )
        for row in cursor.fetchall():
            sku_key = str(row[0] or "").strip().upper()
            try:
                wt = float(row[1]) if row[1] is not None else 0.0
            except (TypeError, ValueError):
                wt = 0.0
            if sku_key and wt > 0:
                weights[sku_key] = wt
    except Exception as e:
        current_app.logger.error(
            "Error fetching case weights from %s: %s", database_name, e
        )
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

    return weights


def fetch_masterbox_by_skus(skus, database_name: str) -> dict[str, dict]:
    """Masterbox L/W/H (inches) + CASE_WT from inv/inv_detl, keyed by uppercase SKU."""
    if not skus or not database_name:
        return {}

    cleaned = []
    seen = set()
    for sku in skus:
        try:
            key = str(sku).replace("\u00a0", " ").strip().upper()
        except Exception:
            key = str(sku).strip().upper()
        if key and key not in seen:
            seen.add(key)
            cleaned.append(key)
    if not cleaned:
        return {}

    placeholders = ",".join(["?"] * len(cleaned))
    out: dict[str, dict] = {}
    conn = None
    cursor = None
    try:
        conn = pyodbc.connect(
            f'DRIVER={sql_server_config["driver"]};SERVER={sql_server_config["server"]};'
            f'DATABASE={database_name};UID={sql_server_config["username"]};PWD={sql_server_config["password"]}'
        )
        cursor = conn.cursor()
        cursor.execute(
            f"""
            SELECT
                UPPER(LTRIM(RTRIM(i.PROD_CD))) AS sku,
                i.CASE_WT,
                idt.CASE_LEN,
                idt.CASE_WI,
                idt.CASE_HI,
                CAST(ISNULL(i.PC_CASE, 0) AS INT) AS case_qty
            FROM dbo.inv AS i
            LEFT JOIN dbo.inv_detl AS idt ON i.PROD_CD = idt.prod_cd
            WHERE UPPER(LTRIM(RTRIM(i.PROD_CD))) IN ({placeholders})
            """,
            cleaned,
        )
        for row in cursor.fetchall():
            sku_key = str(row[0] or "").strip().upper()
            if not sku_key:
                continue

            def _f(v):
                try:
                    return float(v) if v is not None else 0.0
                except (TypeError, ValueError):
                    return 0.0

            out[sku_key] = {
                "sku": sku_key,
                "weight": _f(row[1]),
                "length": _f(row[2]),
                "width": _f(row[3]),
                "height": _f(row[4]),
                "case_qty": int(row[5] or 0),
                "dim_unit": "INCH",
            }
    except Exception as e:
        current_app.logger.error(
            "Error fetching masterbox dims from %s: %s", database_name, e
        )
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()
    return out


def fetch_sku_inventory_by_skus(skus):
    """Fetch in-warehouse and sold/reserved per SKU from FDC and E_FAS (summed).

    in_warehouse = in_stock - order_qty (product detail "In Warehouse")
    sold_reserved = order_qty (product detail "Sold/reserved")
    Returns mapping keyed by lowercase, stripped SKU.
    """
    inventory_by_sku = {}

    if not skus:
        return inventory_by_sku

    cleaned_skus = []
    seen = set()
    for sku in skus:
        try:
            normalized = sku.replace('\u00A0', ' ').strip()
        except Exception:
            normalized = str(sku).strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            cleaned_skus.append(normalized)

    if not cleaned_skus:
        return inventory_by_sku

    search_values = [s.upper() for s in cleaned_skus]
    placeholders = ','.join(['?'] * len(search_values))

    query = f"""
        SELECT
            LTRIM(RTRIM(i.PROD_CD)) AS sku,
            CAST(ISNULL(id.in_stock, 0) - ISNULL(id.order_qty, 0) AS INT) AS in_warehouse,
            CAST(ISNULL(id.order_qty, 0) AS INT) AS sold_reserved
        FROM dbo.inv i
        LEFT JOIN dbo.inv_data id ON i.PROD_CD = id.prod_cd
        WHERE UPPER(LTRIM(RTRIM(i.PROD_CD))) IN ({placeholders})
    """

    for database_name in ('omsdata_FDC', 'oms_EF'):
        conn = None
        cursor = None
        try:
            conn = pyodbc.connect(
                f'DRIVER={sql_server_config["driver"]};SERVER={sql_server_config["server"]};DATABASE={database_name};UID={sql_server_config["username"]};PWD={sql_server_config["password"]}'
            )
            cursor = conn.cursor()
            cursor.execute(query, search_values)
            for row in cursor.fetchall():
                sku_value = row[0]
                if not sku_value:
                    continue
                key = str(sku_value).strip().lower()
                wh = int(row[1] if row[1] is not None else 0)
                sold = int(row[2] if row[2] is not None else 0)
                if key not in inventory_by_sku:
                    inventory_by_sku[key] = {"in_warehouse": 0, "sold_reserved": 0}
                inventory_by_sku[key]["in_warehouse"] += wh
                inventory_by_sku[key]["sold_reserved"] += sold
        except Exception as e:
            current_app.logger.error(
                f"Error fetching SKU inventory from {database_name}: {e}"
            )
        finally:
            if cursor:
                cursor.close()
            if conn:
                conn.close()

    return inventory_by_sku


def generate_order(order_number):
    global file_path
    try:
        # Determine the database and cursor based on the length of order_number
        if len(order_number) == 5:
            database_name = 'omsdata_FDC'
        elif len(order_number) == 4:
            database_name = 'oms_EF'
        else:
            print("Invalid input length. Please provide a 4 or 5-digit input.")
            return None, None, False, None, None, None

        conn = pyodbc.connect(
            f'DRIVER={sql_server_config["driver"]};SERVER={sql_server_config["server"]};DATABASE={database_name};UID={sql_server_config["username"]};PWD={sql_server_config["password"]}'
        )

        # Create a cursor object to execute the SQL query
        cursor = conn.cursor()

        # Define the SQL query (same as in your original code)
        query = """\
           WITH OrderNum AS (
                SELECT ? AS ord_num
            ),
            OrderInfo AS (
                SELECT O.ORD_NUM, O.CUS_ID, O.COMM_LN, O.UT_DESC, O.PROD_CD, O.ORDER_QTY, O.UNIT_PRS, O.PC_UNIT, C.CUS_NM
                FROM DBO.ORD_LOG O
                INNER JOIN DBO.CUSTOMER C ON O.CUS_ID = C.CUS_ID
                WHERE O.ORD_NUM = (SELECT ord_num FROM OrderNum)
            ),
            OrderDates AS (
                SELECT 
                    ord_num,
                    ord_dt AS order_date,
                    ship_dt AS ship_date,
                    can_dt AS cancel_date,
                    PO_NUM,
                    INVS_NUM,
                    ORD_AMT,
                    PAID_AMT,
                    TAXABLE_AMT,
                    INVS_TAX,
                    HANDL_FEE,
                    SHIP_CHG,
                    DISCOUNT,
                    MISC_CHG,
                    SALES_NUM,
                    SALES_NUM2,
                    TERM_DESC,
                    CLOSE_CD,
                    ORD_TYPE,
                    CUS_SHIP_CHG,
                    DISC_AMT,
                    STATUS,
                    SHIP_DESC,
                    TRK_NUM,
                    SP_ADR,
                    SP_ADR_2,
                    SP_ADR_3,
                    SP_ADR_CT,
                    SP_ADR_ST,
                    SP_ADR_ZP,
                    SP_ADR_CN,
                    SP_CUS_NM,
                    ATTN,
                    EMAIL_ADR,
                    PCK_AMT
                FROM dbo.orders
                WHERE ord_num = (SELECT ord_num FROM OrderNum)
            ),
            CustomerInfo AS (
                SELECT 
                    CUS_ID,
                    PRIORITY,
                    PHONE,
                    SHP_CITY,
                    SHP_STATE,
                    SHP_ZIP
                FROM dbo.CUSTOMER
                WHERE CUS_ID IN (SELECT DISTINCT CUS_ID FROM OrderInfo)
            )
            SELECT DISTINCT
                O.ORD_NUM AS order#, 
                O.CUS_NM AS Customer, 
                O.CUS_ID AS CUS#,
                CASE 
                    WHEN OD.INVS_NUM IS NULL OR OD.INVS_NUM = 0 THEN 'not invoiced'
                    ELSE CAST(OD.INVS_NUM AS VARCHAR)
                END AS invoice_num,
                OD.PO_NUM AS PO#, 
                OD.ORD_AMT AS order_amount,
                OD.PAID_AMT AS paid_amount,
                OD.TAXABLE_AMT AS taxable_amount,
                OD.INVS_TAX AS tax_amount,
                OD.HANDL_FEE AS handling_fee,
                OD.SHIP_CHG AS shipping_charge,
                OD.DISCOUNT AS discount,
                OD.MISC_CHG AS misc_charge,
                OD.CUS_SHIP_CHG AS customer_ship_charge,
                OD.DISC_AMT AS discount_amount,
                OD.PCK_AMT AS package_amount,
                OD.SALES_NUM AS salesman,
                OD.SALES_NUM2 AS salesman2,
                OD.TERM_DESC AS terms,
                OD.CLOSE_CD AS close_code,
                OD.ORD_TYPE AS order_type,
                OD.STATUS AS order_status,
                CI.PRIORITY AS priority,
                OD.SHIP_DESC AS ship_method,
                OD.TRK_NUM AS tracking_number,
                OD.SP_ADR AS ship_address_1,
                OD.SP_ADR_2 AS ship_address_2,
                OD.SP_ADR_3 AS ship_address_3,
                OD.SP_ADR_CT AS ship_to_city,
                OD.SP_ADR_ST AS ship_to_state,
                OD.SP_ADR_ZP AS ship_to_zip,
                OD.SP_ADR_CN AS ship_to_country,
                OD.SP_CUS_NM AS ship_customer_name,
                CI.PHONE AS phone,
                OD.ATTN AS attention,
                OD.EMAIL_ADR AS email,
                O.COMM_LN AS line, 
                O.PROD_CD AS SKU, 
                O.UT_DESC AS Descrip, 
                CAST(O.ORDER_QTY AS DECIMAL(10,0)) AS Qty, 
                CAST(O.UNIT_PRS AS DECIMAL (10,2)) AS price, 
                CAST(O.PC_UNIT AS DECIMAL(10,0)) AS per_box,
                RIGHT('0' + CAST(MONTH(DATEADD(DAY, DATEDIFF(DAY, '19000101', OD.order_date) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                            RIGHT('0' + CAST(DAY(DATEADD(DAY, DATEDIFF(DAY, '19000101', OD.order_date) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                            CAST(YEAR(DATEADD(DAY, DATEDIFF(DAY, '19000101', OD.order_date) - 81088, '20230101')) AS VARCHAR) AS order_date,

                            RIGHT('0' + CAST(MONTH(DATEADD(DAY, DATEDIFF(DAY, '19000101', OD.ship_date) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                            RIGHT('0' + CAST(DAY(DATEADD(DAY, DATEDIFF(DAY, '19000101', OD.ship_date) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                            CAST(YEAR(DATEADD(DAY, DATEDIFF(DAY, '19000101', OD.ship_date) - 81088, '20230101')) AS VARCHAR) AS ship_date,

                            CASE 
                                WHEN OD.cancel_date = 0 THEN ''
                                ELSE RIGHT('0' + CAST(MONTH(DATEADD(DAY, DATEDIFF(DAY, '19000101', OD.cancel_date) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                                     RIGHT('0' + CAST(DAY(DATEADD(DAY, DATEDIFF(DAY, '19000101', OD.cancel_date) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                                     CAST(YEAR(DATEADD(DAY, DATEDIFF(DAY, '19000101', OD.cancel_date) - 81088, '20230101')) AS VARCHAR)
                            END AS cancel_date
            FROM OrderInfo O
            INNER JOIN OrderDates OD ON O.ORD_NUM = OD.ord_num
            LEFT JOIN CustomerInfo CI ON O.CUS_ID = CI.CUS_ID;
            """
        
        # Create a Pandas DataFrame from the query results
        df = pd.read_sql_query(query, conn, params=(order_number,))

        if df.empty:
            cursor.close()
            conn.close()
            return None, None, False, None, None, None

        # Extract header information (same for all rows)
        # Calculate discount as percentage of package amount
        package_amount = df.iloc[0]['package_amount'] if pd.notna(df.iloc[0]['package_amount']) else 0
        discount_pct = df.iloc[0]['discount'] if pd.notna(df.iloc[0]['discount']) else 0
        calculated_discount = (discount_pct / 100) * package_amount if package_amount > 0 else 0
        
        # Calculate order amount as ord_amt + handl_fee
        base_order_amount = df.iloc[0]['order_amount'] if pd.notna(df.iloc[0]['order_amount']) else 0
        handling_fee = df.iloc[0]['handling_fee'] if pd.notna(df.iloc[0]['handling_fee']) else 0
        total_order_amount = base_order_amount + handling_fee
        
        header_info = {
            'order_number': str(df.iloc[0]['order#']),
            'customer': df.iloc[0]['Customer'],
            'customer_id': str(df.iloc[0]['CUS#']).strip(),
            'invoice_number': df.iloc[0]['invoice_num'] if (pd.notna(df.iloc[0]['invoice_num']) and str(df.iloc[0]['invoice_num']).strip() not in ['0', 'not invoiced']) else 'not invoiced',
            'po_number': df.iloc[0]['PO#'],
            'package_amount': f"${package_amount:,.2f}" if package_amount > 0 else 'N/A',
            'discount': f"${calculated_discount:,.2f}" if calculated_discount > 0 else 'N/A',
            'shipping_charge': f"${df.iloc[0]['shipping_charge']:,.2f}" if pd.notna(df.iloc[0]['shipping_charge']) else 'N/A',
            'handling_fee': f"${df.iloc[0]['handling_fee']:,.2f}" if pd.notna(df.iloc[0]['handling_fee']) else 'N/A',
            'tax_amount': f"${df.iloc[0]['tax_amount']:,.2f}" if pd.notna(df.iloc[0]['tax_amount']) else 'N/A',
            'order_amount': f"${total_order_amount:,.2f}" if total_order_amount > 0 else 'N/A',
            'paid_amount': f"${df.iloc[0]['paid_amount']:,.2f}" if pd.notna(df.iloc[0]['paid_amount']) else 'N/A',
            'taxable_amount': f"${df.iloc[0]['taxable_amount']:,.2f}" if pd.notna(df.iloc[0]['taxable_amount']) else 'N/A',
            'misc_charge': f"${df.iloc[0]['misc_charge']:,.2f}" if pd.notna(df.iloc[0]['misc_charge']) else 'N/A',
            'customer_ship_charge': f"${df.iloc[0]['customer_ship_charge']:,.2f}" if pd.notna(df.iloc[0]['customer_ship_charge']) else 'N/A',
            'discount_amount': f"${df.iloc[0]['discount_amount']:,.2f}" if pd.notna(df.iloc[0]['discount_amount']) else 'N/A',
            'salesman': df.iloc[0]['salesman'] or 'N/A',
            'salesman2': df.iloc[0]['salesman2'] or 'N/A',
            'terms': df.iloc[0]['terms'] or 'N/A',
            'close_code': str(df.iloc[0]['close_code']) if pd.notna(df.iloc[0]['close_code']) else 'N/A',
            'order_type': df.iloc[0]['order_type'] or 'N/A',
            'order_date': df.iloc[0]['order_date'],
            'ship_date': df.iloc[0]['ship_date'],
            'cancel_date': df.iloc[0]['cancel_date'],
            'order_status': str(df.iloc[0]['order_status']).strip() if pd.notna(df.iloc[0]['order_status']) and str(df.iloc[0]['order_status']).strip() else 'N/A',
            'priority': str(df.iloc[0]['priority']).strip() if pd.notna(df.iloc[0]['priority']) and str(df.iloc[0]['priority']).strip() else 'Normal',
            'ship_method': str(df.iloc[0]['ship_method']).strip() if pd.notna(df.iloc[0]['ship_method']) and str(df.iloc[0]['ship_method']).strip() else 'N/A',
            'tracking_number': str(df.iloc[0]['tracking_number']).strip() if pd.notna(df.iloc[0]['tracking_number']) and str(df.iloc[0]['tracking_number']).strip() else 'N/A',
            'ship_address_1': str(df.iloc[0]['ship_address_1']).strip() if pd.notna(df.iloc[0]['ship_address_1']) and str(df.iloc[0]['ship_address_1']).strip() else '',
            'ship_address_2': str(df.iloc[0]['ship_address_2']).strip() if pd.notna(df.iloc[0]['ship_address_2']) and str(df.iloc[0]['ship_address_2']).strip() else '',
            'ship_address_3': str(df.iloc[0]['ship_address_3']).strip() if pd.notna(df.iloc[0]['ship_address_3']) and str(df.iloc[0]['ship_address_3']).strip() else '',
            'ship_to_city': str(df.iloc[0]['ship_to_city']).strip() if pd.notna(df.iloc[0]['ship_to_city']) and str(df.iloc[0]['ship_to_city']).strip() else 'N/A',
            'ship_to_state': str(df.iloc[0]['ship_to_state']).strip() if pd.notna(df.iloc[0]['ship_to_state']) and str(df.iloc[0]['ship_to_state']).strip() else 'N/A',
            'ship_to_zip': str(df.iloc[0]['ship_to_zip']).strip() if pd.notna(df.iloc[0]['ship_to_zip']) and str(df.iloc[0]['ship_to_zip']).strip() else 'N/A',
            'ship_to_country': str(df.iloc[0]['ship_to_country']).strip() if pd.notna(df.iloc[0]['ship_to_country']) and str(df.iloc[0]['ship_to_country']).strip() else '',
            'ship_customer_name': str(df.iloc[0]['ship_customer_name']).strip() if pd.notna(df.iloc[0]['ship_customer_name']) and str(df.iloc[0]['ship_customer_name']).strip() else '',
            'phone': str(df.iloc[0]['phone']).strip() if pd.notna(df.iloc[0]['phone']) and str(df.iloc[0]['phone']).strip() else 'N/A',
            'attention': str(df.iloc[0]['attention']).strip() if pd.notna(df.iloc[0]['attention']) and str(df.iloc[0]['attention']).strip() else 'N/A',
            'email': str(df.iloc[0]['email']).strip() if pd.notna(df.iloc[0]['email']) and str(df.iloc[0]['email']).strip() else 'N/A'
        }

        # Attempt to fetch order notes from dbo.ntfile
        try:
            conn_notes = pyodbc.connect(
                f'DRIVER={sql_server_config["driver"]};SERVER={sql_server_config["server"]};DATABASE={database_name};UID={sql_server_config["username"]};PWD={sql_server_config["password"]}'
            )
            cur2 = conn_notes.cursor()
            # Some orders have multiple ntfile rows; pick the first non-empty note, favoring the longest
            cur2.execute(
                """
                SELECT TOP 1 NOTES
                FROM dbo.ntfile
                WHERE NT_NUM = ?
                  AND NOTES IS NOT NULL
                  AND LTRIM(RTRIM(NOTES)) <> ''
                ORDER BY LEN(NOTES) DESC
                """,
                (order_number,)
            )
            note_row = cur2.fetchone()
            header_info['notes'] = str(note_row[0]).strip() if note_row and note_row[0] else ''
            try:
                preview = header_info['notes'][:200] if header_info['notes'] else ''
            except Exception:
                pass
            cur2.close()
            conn_notes.close()
        except Exception as e:
            current_app.logger.error(f"Error fetching order notes for {order_number}: {e}")
            header_info['notes'] = ''

        # Close the cursor and connection
        cursor.close()
        conn.close()        # Create items DataFrame with item-specific columns including Location
        items_df = df[['line', 'SKU', 'Descrip', 'Qty', 'price', 'per_box']].copy()

        # Clean SKU values: remove trailing/leading whitespace and non-breaking spaces
        try:
            items_df['SKU'] = items_df['SKU'].astype(str).str.replace('\u00A0', ' ').str.strip()
        except Exception:
            # fallback simple strip
            items_df['SKU'] = items_df['SKU'].astype(str).str.strip()
        
        # Adjust price to list price by adding discount per unit
        discount_amount = df.iloc[0]['discount_amount'] if pd.notna(df.iloc[0]['discount_amount']) else 0
        items_df['price'] = items_df['price'] + (discount_amount / items_df['Qty'])
        
        # Use the same lookup pattern as in Item Lookup: pass lowercased SKUs to fetch_locations
        # Try several SKU variants to maximize matching with products.SKU values in MySQL
        try:
            skus_original = items_df['SKU'].astype(str).tolist()
            skus_lower = [s.lower() for s in skus_original]
            skus_stripped = [s.replace(' ', '') for s in skus_lower]
            skus_upper = [s.upper() for s in skus_original]

            locations = {}
            # Try variants in order: lower, original, stripped, upper
            variants = [skus_lower, skus_original, skus_stripped, skus_upper]
            for variant in variants:
                try:
                    if not variant:
                        continue
                    found = fetch_locations(variant)
                    # merge keys (fetch_locations lowercases keys internally)
                    for k, v in found.items():
                        if k not in locations or not locations[k]:
                            locations[k] = v
                except Exception as _e:
                    current_app.logger.debug(f"fetch_locations variant failed: {_e}")
                    continue
        except Exception as e:
            current_app.logger.error(f"Error preparing SKU variants for location lookup for order {order_number}: {e}")
            locations = {}

        # Log what we found for easier debugging
        try:
            if locations:
                sample = dict(list(locations.items())[:20])
        except Exception:
            pass

        # Map locations back to rows using lowercased SKU key (fallback to stripped/alnum handled in fetch_locations)
        try:
            items_df['Location'] = items_df['SKU'].astype(str).str.lower().map(locations).fillna('N/A')
        except Exception as e:
            current_app.logger.error(f"Error mapping locations into items_df for order {order_number}: {e}")
            items_df['Location'] = 'N/A'

        # Add In Warehouse stock (same metric as product detail "In Warehouse": in_stock - order_qty).
        try:
            stock_map = fetch_in_warehouse_by_skus(items_df['SKU'].astype(str).tolist())
            items_df['Stock'] = items_df['SKU'].astype(str).str.lower().map(stock_map).fillna(0).astype(int)
        except Exception as e:
            current_app.logger.error(f"Error mapping stock into items_df for order {order_number}: {e}")
            items_df['Stock'] = 0

        # Format Qty and per_box as integers (removing decimals)
        items_df['Qty'] = items_df['Qty'].fillna(0).astype(int)
        items_df['per_box'] = items_df['per_box'].fillna(0).astype(int)

        items_df['_boxes_for_weight'] = items_df.apply(
            lambda r: line_box_count(r['Qty'], r['per_box']), axis=1
        )
        items_df['Boxes'] = items_df.apply(
            lambda r: format_boxes_display(r['Qty'], r['per_box']), axis=1
        )
        items_total_qty = int(items_df['Qty'].sum())
        items_total_boxes = int(
            items_df.apply(
                lambda r: full_box_count(r['Qty'], r['per_box']), axis=1
            ).sum()
        )

        try:
            case_weights = fetch_case_weights_by_skus(
                items_df['SKU'].astype(str).tolist(), database_name
            )
            items_df['_line_weight'] = items_df.apply(
                lambda r: (
                    round(
                        case_weights.get(str(r['SKU']).strip().upper(), 0.0)
                        * int(r['_boxes_for_weight']),
                        2,
                    )
                    if case_weights.get(str(r['SKU']).strip().upper(), 0.0)
                    and int(r['_boxes_for_weight'])
                    else 0.0
                ),
                axis=1,
            )
            items_total_weight = round(float(items_df['_line_weight'].sum()), 2)
            items_df['Weight'] = items_df['_line_weight'].apply(
                lambda w: f"{w:.2f}" if w else ""
            )
            items_df = items_df.drop(columns=['_line_weight', '_boxes_for_weight'])
        except Exception as e:
            current_app.logger.error(
                "Error mapping weight into items_df for order %s: %s", order_number, e
            )
            items_df['Weight'] = ""
            items_total_weight = 0.0
            if '_boxes_for_weight' in items_df.columns:
                items_df = items_df.drop(columns=['_boxes_for_weight'])

        # Keep column order: Boxes before Location; Weight after Stock.
        items_df = items_df[
            [
                'line',
                'SKU',
                'Descrip',
                'Qty',
                'price',
                'per_box',
                'Boxes',
                'Location',
                'Stock',
                'Weight',
            ]
        ]
        
        # Check if ALL descriptions contain a dash "-" for label generation
        all_descriptions_have_dash = items_df['Descrip'].str.contains('-', na=False).all()
        
        # Add hyperlinks to SKU column
        items_df['SKU'] = items_df['SKU'].apply(
            lambda sku: f'<a href="https://www.fdcef.com/item_lookup/{sku}" target="_blank" style="color: #4a90e2; text-decoration: none; font-weight: 600;">{sku}</a>'
        )
        
        # Prepare the HTML content for items table
        full_table_html = items_df.to_html(classes='table table-bordered table-striped', escape=False, index=False, header=False)
        
        # Extract only the tbody content from the generated HTML
        import re
        tbody_match = re.search(r'<tbody[^>]*>(.*?)</tbody>', full_table_html, re.DOTALL)
        if tbody_match:
            items_html = tbody_match.group(1)
        else:
            items_html = full_table_html
        
        return (
            enrich_header_info(header_info),
            items_html,
            all_descriptions_have_dash,
            items_total_qty,
            items_total_boxes,
            items_total_weight,
        )

    except Exception as e:
        print(f"An error occurred: {str(e)}")
        return None, None, False, None, None, None


@order_lookup_bp.route("/order_lookup/<string:order_number>", methods=["GET"])
@order_lookup_bp.route("/order_lookup", methods=["GET", "POST"])
@login_required
def order_lookup(order_number=None):
    if request.method == "GET" and not order_number:
        query_order = (request.args.get("order_number") or "").strip()
        if query_order:
            return redirect(url_for("order_lookup.order_lookup", order_number=query_order))

    current_user = session['username']
    header_info = None
    items_html = None
    items_total_qty = None
    items_total_boxes = None
    items_total_weight = None
    all_descriptions_have_dash = False
    recent_fdc_orders = get_recent_fdc_orders(100)

    error_message = None
    po_number = None
    if request.method == "POST":
        # If searching by PO
        po_number = request.form.get("search_by_po", "").strip()
        if po_number:
            # Try to find order number by PO in both databases
            order_number = find_order_number_by_po(po_number)
            if not order_number:
                error_message = f"No order found for PO: {po_number}"
                return render_template("order_lookup.html", header_info=None, items_html=None, current_user=current_user, recent_fdc_orders=recent_fdc_orders, all_descriptions_have_dash=False, error_message=error_message, po_number=po_number)
            # else, continue as if searching by order number
        else:
            order_number = request.form.get("order_number", "").strip()

        if not order_number:
            return render_template("order_lookup.html", header_info=None, items_html=None, current_user=current_user, recent_fdc_orders=recent_fdc_orders, all_descriptions_have_dash=False, po_number=po_number)

        session['order_number'] = order_number
        return redirect(url_for("order_lookup.order_lookup", order_number=order_number))

    elif order_number:
        # Keep session aligned when navigating via URL (sidebar, links, prev/next).
        session['order_number'] = order_number

        if current_user == 'fdc':
            current_app.logger.info("User %s does not have permission to view order details", current_user)
            return render_template("order_lookup.html", header_info=None, items_html=None, current_user=current_user, recent_fdc_orders=recent_fdc_orders, all_descriptions_have_dash=False)
        
        (
            header_info,
            items_html,
            all_descriptions_have_dash,
            items_total_qty,
            items_total_boxes,
            items_total_weight,
        ) = generate_order(order_number)

        if items_html:
            current_app.logger.info("***%s***__@@@search order@@@__ %s",current_user, order_number)
        else:
            current_app.logger.info("No data found for order number: %s via URL", order_number)
            header_info = None
            items_html = None
            all_descriptions_have_dash = False
            items_total_qty = None
            items_total_boxes = None
            items_total_weight = None

    ez_default_boxes = None
    if header_info and header_info.get("notes"):
        ez_default_boxes = parse_boxes_from_notes(header_info.get("notes"))
    elif order_number:
        ez_default_boxes = parse_boxes_from_notes(fetch_order_notes(order_number))

    return render_template(
        "order_lookup.html",
        header_info=header_info,
        items_html=items_html,
        items_total_qty=items_total_qty,
        items_total_boxes=items_total_boxes,
        items_total_weight=items_total_weight,
        order_number=order_number,
        current_user=current_user,
        recent_fdc_orders=recent_fdc_orders,
        all_descriptions_have_dash=all_descriptions_have_dash,
        error_message=error_message,
        po_number=po_number,
        ez_default_boxes=ez_default_boxes,
    )

def find_order_number_by_po(po_number):
    # Try both databases for a matching PO
    try:
        for db_name, length in [('omsdata_FDC', 5), ('oms_EF', 4)]:
            conn = pyodbc.connect(
                f'DRIVER={sql_server_config["driver"]};SERVER={sql_server_config["server"]};DATABASE={db_name};UID={sql_server_config["username"]};PWD={sql_server_config["password"]}'
            )
            cursor = conn.cursor()
            cursor.execute("SELECT TOP 1 ord_num FROM orders WHERE PO_NUM = ? ORDER BY ORD_DT DESC", (po_number,))
            row = cursor.fetchone()
            cursor.close()
            conn.close()
            if row and row[0]:
                return str(row[0]).zfill(length)
    except Exception as e:
        current_app.logger.error(f"Error finding order by PO: {po_number}: {e}")
    return None


@order_lookup_bp.route('/debug_order_locations/<string:order_number>', methods=['GET'])
@login_required
def debug_order_locations(order_number):
    """Debug route: return locations mapping for SKUs in the given order number."""
    try:
        # Determine database based on order number length
        if len(order_number) == 5:
            database_name = 'omsdata_FDC'
        elif len(order_number) == 4:
            database_name = 'oms_EF'
        else:
            return jsonify({'error': 'Invalid order number format'}), 400

        conn = pyodbc.connect(
            f'DRIVER={sql_server_config["driver"]};SERVER={sql_server_config["server"]};DATABASE={database_name};UID={sql_server_config["username"]};PWD={sql_server_config["password"]}'
        )
        cursor = conn.cursor()
        cursor.execute("SELECT DISTINCT PROD_CD FROM DBO.ORD_LOG WHERE ORD_NUM = ?", (order_number,))
        rows = cursor.fetchall()
        cursor.close()
        conn.close()

        sku_list = [r[0] for r in rows if r and r[0]]
        # If no SKUs found, return empty
        if not sku_list:
            return jsonify({'skus': [], 'locations': {}})

        locations = fetch_locations([str(s) for s in sku_list])
        return jsonify({'skus': sku_list, 'locations': locations})
    except Exception as e:
        current_app.logger.error(f"Error in debug_order_locations for {order_number}: {e}")
        return jsonify({'error': str(e)}), 500

@order_lookup_bp.route("/update_order_description", methods=["POST"])
@login_required
def update_order_description():
    """Update the description (UT_DESC) for a specific order line"""
    try:
        data = request.get_json()
        order_number = data.get('order_number')
        line_number = data.get('line_number')
        sku = data.get('sku')
        new_description = data.get('description')
        
        if not all([order_number, line_number, new_description]):
            return {'success': False, 'message': 'Missing required fields'}, 400
        
        # Determine database based on order number length
        if len(order_number) == 5:
            database_name = 'omsdata_FDC'
        elif len(order_number) == 4:
            database_name = 'oms_EF'
        else:
            return {'success': False, 'message': 'Invalid order number format'}, 400
        
        # Connect to database
        conn = pyodbc.connect(
            f'DRIVER={sql_server_config["driver"]};SERVER={sql_server_config["server"]};DATABASE={database_name};UID={sql_server_config["username"]};PWD={sql_server_config["password"]}'
        )
        cursor = conn.cursor()
        
        # Update the description in ORD_LOG table
        update_query = """
            UPDATE DBO.ORD_LOG 
            SET UT_DESC = ?
            WHERE ORD_NUM = ? AND COMM_LN = ?
        """
        
        cursor.execute(update_query, (new_description, order_number, line_number))
        conn.commit()
        
        rows_affected = cursor.rowcount
        
        cursor.close()
        conn.close()
        
        if rows_affected > 0:
            current_user = session.get('username', 'unknown')
            current_app.logger.info(
                f"***{current_user}***__@@@updated description@@@__ Order: {order_number}, Line: {line_number}, SKU: {sku}, New Description: {new_description}"
            )
            return {'success': True, 'message': 'Description updated successfully'}, 200
        else:
            return {'success': False, 'message': 'No rows were updated'}, 404
            
    except Exception as e:
        current_app.logger.error(f"Error updating description: {str(e)}")
        return {'success': False, 'message': f'Database error: {str(e)}'}, 500


@order_lookup_bp.route("/update_order_qty", methods=["POST"])
@login_required
def update_order_qty():
    """Update the quantity (ORDER_QTY) for a specific order line"""
    try:
        data = request.get_json()
        order_number = data.get('order_number')
        line_number = data.get('line_number')
        sku = data.get('sku')
        new_qty = data.get('qty')
        
        if not all([order_number, line_number, new_qty]):
            return {'success': False, 'message': 'Missing required fields'}, 400
        
        # Determine database based on order number length
        if len(order_number) == 5:
            database_name = 'omsdata_FDC'
        elif len(order_number) == 4:
            database_name = 'oms_EF'
        else:
            return {'success': False, 'message': 'Invalid order number format'}, 400
        
        # Connect to database
        conn = pyodbc.connect(
            f'DRIVER={sql_server_config["driver"]};SERVER={sql_server_config["server"]};DATABASE={database_name};UID={sql_server_config["username"]};PWD={sql_server_config["password"]}'
        )
        cursor = conn.cursor()
        
        # Update the quantity in ORD_LOG table
        update_query = """
            UPDATE DBO.ORD_LOG 
            SET ORDER_QTY = ?
            WHERE ORD_NUM = ? AND COMM_LN = ?
        """
        
        cursor.execute(update_query, (new_qty, order_number, line_number))
        conn.commit()
        
        rows_affected = cursor.rowcount
        
        cursor.close()
        conn.close()
        
        if rows_affected > 0:
            current_user = session.get('username', 'unknown')
            current_app.logger.info(
                f"***{current_user}***__@@@updated qty@@@__ Order: {order_number}, Line: {line_number}, SKU: {sku}, New Qty: {new_qty}"
            )
            return {'success': True, 'message': 'Quantity updated successfully'}, 200
        else:
            return {'success': False, 'message': 'No rows were updated'}, 404
            
    except Exception as e:
        current_app.logger.error(f"Error updating quantity: {str(e)}")
        return {'success': False, 'message': f'Database error: {str(e)}'}, 500


@order_lookup_bp.route("/update_order_price", methods=["POST"])
@login_required
def update_order_price():
    """Update the price (UNIT_PRS) for a specific order line"""
    try:
        data = request.get_json()
        order_number = data.get('order_number')
        line_number = data.get('line_number')
        sku = data.get('sku')
        new_price = data.get('price')
        
        if not all([order_number, line_number, new_price]):
            return {'success': False, 'message': 'Missing required fields'}, 400
        
        # Determine database based on order number length
        if len(order_number) == 5:
            database_name = 'omsdata_FDC'
        elif len(order_number) == 4:
            database_name = 'oms_EF'
        else:
            return {'success': False, 'message': 'Invalid order number format'}, 400
        
        # Connect to database
        conn = pyodbc.connect(
            f'DRIVER={sql_server_config["driver"]};SERVER={sql_server_config["server"]};DATABASE={database_name};UID={sql_server_config["username"]};PWD={sql_server_config["password"]}'
        )
        cursor = conn.cursor()
        
        # Update the price in ORD_LOG table
        update_query = """
            UPDATE DBO.ORD_LOG 
            SET UNIT_PRS = ?
            WHERE ORD_NUM = ? AND COMM_LN = ?
        """
        
        cursor.execute(update_query, (new_price, order_number, line_number))
        conn.commit()
        
        rows_affected = cursor.rowcount
        
        cursor.close()
        conn.close()
        
        if rows_affected > 0:
            current_user = session.get('username', 'unknown')
            current_app.logger.info(
                f"***{current_user}***__@@@updated price@@@__ Order: {order_number}, Line: {line_number}, SKU: {sku}, New Price: {new_price}"
            )
            return {'success': True, 'message': 'Price updated successfully'}, 200
        else:
            return {'success': False, 'message': 'No rows were updated'}, 404
            
    except Exception as e:
        current_app.logger.error(f"Error updating price: {str(e)}")
        return {'success': False, 'message': f'Database error: {str(e)}'}, 500


@order_lookup_bp.route("/update_order_per_box", methods=["POST"])
@login_required
def update_order_per_box():
    """Update per-box quantity (PC_UNIT) for a specific order line."""
    try:
        data = request.get_json()
        order_number = data.get('order_number')
        line_number = data.get('line_number')
        sku = data.get('sku')
        new_per_box = data.get('per_box')

        if not all([order_number, line_number, new_per_box]):
            return {'success': False, 'message': 'Missing required fields'}, 400

        if len(order_number) == 5:
            database_name = 'omsdata_FDC'
        elif len(order_number) == 4:
            database_name = 'oms_EF'
        else:
            return {'success': False, 'message': 'Invalid order number format'}, 400

        conn = pyodbc.connect(
            f'DRIVER={sql_server_config["driver"]};SERVER={sql_server_config["server"]};DATABASE={database_name};UID={sql_server_config["username"]};PWD={sql_server_config["password"]}'
        )
        cursor = conn.cursor()

        update_query = """
            UPDATE DBO.ORD_LOG
            SET PC_UNIT = ?
            WHERE ORD_NUM = ? AND COMM_LN = ?
        """

        cursor.execute(update_query, (new_per_box, order_number, line_number))
        conn.commit()

        rows_affected = cursor.rowcount

        cursor.close()
        conn.close()

        if rows_affected > 0:
            current_user = session.get('username', 'unknown')
            current_app.logger.info(
                f"***{current_user}***__@@@updated per_box@@@__ Order: {order_number}, Line: {line_number}, SKU: {sku}, New PC_UNIT: {new_per_box}"
            )
            return {'success': True, 'message': 'Per box updated successfully'}, 200
        return {'success': False, 'message': 'No rows were updated'}, 404

    except Exception as e:
        current_app.logger.error(f"Error updating per_box: {str(e)}")
        return {'success': False, 'message': f'Database error: {str(e)}'}, 500


@order_lookup_bp.route("/update_order_ship_date", methods=["POST"])
@login_required
def update_order_ship_date():
    """Update order-level ship date (SHIP_DT) in dbo.orders."""
    try:
        data = request.get_json()
        order_number = data.get('order_number')
        ship_date = data.get('ship_date', '').strip()

        if not order_number or not ship_date:
            return {'success': False, 'message': 'Missing order number or ship date'}, 400

        # Validate date format as MM/DD/YYYY
        if not re.match(r'^\d{1,2}/\d{1,2}/\d{4}$', ship_date):
            return {'success': False, 'message': 'Ship date must be in MM/DD/YYYY format'}, 400

        try:
            datetime.strptime(ship_date, "%m/%d/%Y")
        except ValueError:
            return {'success': False, 'message': 'Invalid ship date'}, 400

        # Determine database based on order number length
        if len(order_number) == 5:
            database_name = 'omsdata_FDC'
        elif len(order_number) == 4:
            database_name = 'oms_EF'
        else:
            return {'success': False, 'message': 'Invalid order number format'}, 400

        conn = pyodbc.connect(
            f'DRIVER={sql_server_config["driver"]};SERVER={sql_server_config["server"]};DATABASE={database_name};UID={sql_server_config["username"]};PWD={sql_server_config["password"]}'
        )
        cursor = conn.cursor()

        # SHIP_DT is stored as a numeric encoded date in this schema.
        # Convert MM/DD/YYYY into the same numeric base used by generate_order.
        update_query = """
            UPDATE dbo.orders
            SET SHIP_DT = DATEDIFF(DAY, '20230101', CONVERT(date, ?, 101)) + 81088
            WHERE ORD_NUM = ?
        """

        cursor.execute(update_query, (ship_date, order_number))
        conn.commit()
        rows_affected = cursor.rowcount

        cursor.close()
        conn.close()

        if rows_affected > 0:
            current_user = session.get('username', 'unknown')
            current_app.logger.info(
                f"***{current_user}***__@@@updated ship date@@@__ Order: {order_number}, New Ship Date: {ship_date}"
            )
            return {'success': True, 'message': 'Ship date updated successfully'}, 200
        else:
            return {'success': False, 'message': 'No rows were updated'}, 404

    except Exception as e:
        current_app.logger.error(f"Error updating ship date: {str(e)}")
        return {'success': False, 'message': f'Database error: {str(e)}'}, 500


@order_lookup_bp.route('/debug_order_notes/<string:order_number>', methods=['GET'])
@login_required
def debug_order_notes(order_number):
    """Debug route to inspect dbo.ntfile NOTES for a given order number."""
    try:
        if len(order_number) == 5:
            database_name = 'omsdata_FDC'
        elif len(order_number) == 4:
            database_name = 'oms_EF'
        else:
            return jsonify({'error': 'Invalid order number format'}), 400

        conn = pyodbc.connect(
            f'DRIVER={sql_server_config["driver"]};SERVER={sql_server_config["server"]};DATABASE={database_name};UID={sql_server_config["username"]};PWD={sql_server_config["password"]}'
        )
        cursor = conn.cursor()
        cursor.execute("SELECT NT_NUM, NOTES FROM dbo.ntfile WHERE NT_NUM = ?", (order_number,))
        rows = cursor.fetchall()
        cursor.close()
        conn.close()

        notes_list = []
        for r in rows:
            nt = str(r[0]) if r and len(r) > 0 else ''
            n = str(r[1]).strip() if r and len(r) > 1 and r[1] is not None else ''
            notes_list.append({'nt_num': nt, 'notes': n})

        return jsonify({
            'database': database_name,
            'order_number': order_number,
            'count': len(notes_list),
            'results': notes_list
        })
    except Exception as e:
        current_app.logger.error(f"Error in debug_order_notes for {order_number}: {e}")
        return jsonify({'error': str(e)}), 500


@order_lookup_bp.route("/update_order_notes", methods=["POST"])
@login_required
def update_order_notes():
    """Update the order-level NOTES field in dbo.ntfile for the given order."""
    try:
        data = request.get_json()
        order_number = data.get('order_number')
        notes = data.get('notes', '')

        if not order_number:
            return {'success': False, 'message': 'Missing order number'}, 400

        # Determine database based on order number length
        if len(order_number) == 5:
            database_name = 'omsdata_FDC'
        elif len(order_number) == 4:
            database_name = 'oms_EF'
        else:
            return {'success': False, 'message': 'Invalid order number format'}, 400

        # Log the notes being saved for debugging
        try:
            current_app.logger.info(f"Saving notes for order {order_number}: {repr(notes[:100])} (length: {len(notes)})")
        except:
            pass

        conn = pyodbc.connect(
            f'DRIVER={sql_server_config["driver"]};SERVER={sql_server_config["server"]};DATABASE={database_name};UID={sql_server_config["username"]};PWD={sql_server_config["password"]}'
        )
        cursor = conn.cursor()

        # First check if a record exists for this order in ntfile
        cursor.execute("SELECT COUNT(*) FROM dbo.ntfile WHERE NT_NUM = ?", (order_number,))
        record_exists = cursor.fetchone()[0] > 0

        if record_exists:
            # Update existing record
            update_query = "UPDATE dbo.ntfile SET NOTES = ? WHERE NT_NUM = ?"
            cursor.execute(update_query, (notes, order_number))
        else:
            # Insert new record if it doesn't exist (NT_CD defaults to 0)
            insert_query = "INSERT INTO dbo.ntfile (NT_NUM, NT_CD, NOTES) VALUES (?, 0, ?)"
            cursor.execute(insert_query, (order_number, notes))
        
        conn.commit()
        rows_affected = cursor.rowcount

        cursor.close()
        conn.close()

        if rows_affected > 0:
            current_user = session.get('username', 'unknown')
            current_app.logger.info(f"***{current_user}***__@@@updated notes@@@__ Order: {order_number}, rows affected: {rows_affected}")
            return {'success': True, 'message': 'Order notes updated successfully'}, 200
        else:
            return {'success': False, 'message': 'No rows were updated'}, 404
    except Exception as e:
        current_app.logger.error(f"Error updating order notes: {str(e)}")
        return {'success': False, 'message': f'Database error: {str(e)}'}, 500


@order_lookup_bp.route("/bol_from_order_lookup", methods=["POST"])
@login_required
def bol_from_order_lookup():
    """Generate a fillable US Letter Bill of Lading PDF for the current order."""
    current_user = session.get("username", "unknown")
    payload = request.get_json(silent=True) or {}
    order_number = str(
        payload.get("order_number") or session.get("order_number") or ""
    ).strip()
    if not order_number:
        return jsonify({"ok": False, "error": "No order in session. Look up an order first."}), 400

    try:
        from py.other.bol_pdf import build_bol_pdf, fetch_order_bol_context

        ctx, err = fetch_order_bol_context(order_number)
        if err:
            return jsonify({"ok": False, "error": err}), 400

        current_app.logger.info(
            "***%s***__@@@create BOL@@@__ %s",
            current_user,
            order_number,
        )
        pdf_data = build_bol_pdf(ctx)
        filename = f"{order_number}_BOL.pdf"
        return send_file(
            io.BytesIO(pdf_data),
            download_name=filename,
            as_attachment=False,
            mimetype="application/pdf",
        )
    except Exception as e:
        current_app.logger.exception("BOL PDF failed for order %s", order_number)
        return jsonify({"ok": False, "error": str(e)}), 500


@order_lookup_bp.route("/order_lookup/<string:order_number>/ucl_bol", methods=["GET"])
@login_required
def ucl_bol_from_order_lookup(order_number):
    """Prefill the official UCL fillable cargo-receipt PDF for the order."""
    current_user = session.get("username", "unknown")
    order_number = str(order_number or "").strip()
    if not order_number:
        return jsonify({"ok": False, "error": "No order number."}), 400

    try:
        from py.other.bol_pdf import fetch_order_bol_context, fill_ucl_bol_pdf

        ctx, err = fetch_order_bol_context(order_number)
        if err:
            return jsonify({"ok": False, "error": err}), 400

        current_app.logger.info(
            "***%s***__@@@create UCL BOL@@@__ %s",
            current_user,
            order_number,
        )
        pdf_data = fill_ucl_bol_pdf(ctx)
        filename = f"{order_number}_UCL_BOL.pdf"
        return send_file(
            io.BytesIO(pdf_data),
            download_name=filename,
            as_attachment=False,
            mimetype="application/pdf",
        )
    except Exception as e:
        current_app.logger.exception("UCL BOL PDF failed for order %s", order_number)
        return jsonify({"ok": False, "error": str(e)}), 500


@order_lookup_bp.route("/eta_fashion_label_from_order_lookup", methods=["POST"])
@login_required
def eta_fashion_label_from_order_lookup():
    """ETA Fashion (00951) box labels: Letter 4-in-1 or horizontal 6×4 one per page."""
    from py.other.eta_fashion_label_pdf import (
        ETA_FASHION_CUSTOMER_ID,
        build_eta_fashion_label_pdf_4x6,
        build_eta_fashion_label_pdf_letter,
        fetch_order_customer_id,
    )

    current_user = session.get("username", "unknown")
    payload = request.get_json(silent=True) or {}
    order_number = str(
        payload.get("order_number") or session.get("order_number") or ""
    ).strip()
    if not order_number:
        return jsonify({"ok": False, "error": "No order in session. Look up an order first."}), 400

    try:
        total_boxes = int(payload.get("total_boxes"))
    except (TypeError, ValueError):
        total_boxes = 0
    if total_boxes < 1:
        return jsonify({"ok": False, "error": "Total boxes must be a positive number."}), 400

    customer_id, err = fetch_order_customer_id(order_number)
    if err:
        return jsonify({"ok": False, "error": err}), 400
    if customer_id != ETA_FASHION_CUSTOMER_ID:
        return jsonify(
            {"ok": False, "error": "Special Label is only available for ETA Fashion (00951)."}
        ), 403

    layout = str(payload.get("layout") or "letter").strip().lower()
    use_4x6 = layout in ("4x6", "4x6_label", "label_4x6")

    try:
        current_app.logger.info(
            "***%s***__@@@ETA Fashion special label@@@__ %s boxes=%s layout=%s",
            current_user,
            order_number,
            total_boxes,
            "4x6" if use_4x6 else "letter",
        )
        if use_4x6:
            pdf_data = build_eta_fashion_label_pdf_4x6(total_boxes)
            filename = f"{order_number}_eta_fashion_4x6_{total_boxes}boxes.pdf"
        else:
            pdf_data = build_eta_fashion_label_pdf_letter(total_boxes)
            filename = f"{order_number}_eta_fashion_letter_{total_boxes}boxes.pdf"
        return send_file(
            io.BytesIO(pdf_data),
            download_name=filename,
            as_attachment=False,
            mimetype="application/pdf",
        )
    except Exception as e:
        current_app.logger.exception("ETA Fashion label PDF failed for order %s", order_number)
        return jsonify({"ok": False, "error": str(e)}), 500


@order_lookup_bp.route("/order_lookup/gofo_availability", methods=["POST"])
@login_required
def gofo_availability():
    """Check GOFO Parcel/Ground availability via production Open API (Excel fallback)."""
    from py.utilities.gofo_api import check_parcel_and_ground, normalize_zip

    payload = request.get_json(silent=True) or {}
    order_number = str(
        payload.get("order_number") or session.get("order_number") or ""
    ).strip()
    if not order_number:
        return jsonify({"ok": False, "error": "No order number provided."}), 400

    header_info, *_rest = generate_order(order_number)
    if not header_info:
        return jsonify({"ok": False, "error": f"Order {order_number} not found."}), 404

    zip_code = normalize_zip(header_info.get("ship_to_zip"))
    if not zip_code or zip_code.upper() == "N/A":
        return jsonify({"ok": False, "error": "This order has no ship-to ZIP."}), 400

    city = header_info.get("ship_to_city") or ""
    state = header_info.get("ship_to_state") or ""
    country = header_info.get("ship_to_country") or ""
    phone = header_info.get("phone") or ""
    name = (
        header_info.get("ship_customer_name")
        or header_info.get("customer")
        or ""
    ).strip()
    adr1 = (header_info.get("ship_address_1") or "").strip()
    adr2 = (header_info.get("ship_address_2") or "").strip()
    street = adr2 or adr1

    current_user = session.get("username", "unknown")
    current_app.logger.info(
        "***%s***__@@@GOFO availability@@@__ order=%s zip=%s",
        current_user,
        order_number,
        zip_code,
    )

    result = check_parcel_and_ground(
        zip_code=zip_code,
        country=country,
        state=state if state != "N/A" else "",
        city=city if city != "N/A" else "",
        street=street,
        phone=phone if phone != "N/A" else "",
        name=name,
    )
    if not result.get("ok"):
        return jsonify(result), 502

    result["order_number"] = order_number
    return jsonify(result)


def _order_ship_context(header_info: dict) -> dict:
    city = header_info.get("ship_to_city") or ""
    state = header_info.get("ship_to_state") or ""
    country = header_info.get("ship_to_country") or ""
    phone = header_info.get("phone") or ""
    name = (
        header_info.get("ship_customer_name")
        or header_info.get("customer")
        or ""
    ).strip()
    adr1 = (header_info.get("ship_address_1") or "").strip()
    adr2 = (header_info.get("ship_address_2") or "").strip()
    return {
        "zip": header_info.get("ship_to_zip") or "",
        "city": "" if city == "N/A" else city,
        "state": "" if state == "N/A" else state,
        "country": country,
        "phone": "" if phone == "N/A" else phone,
        "name": name,
        "street": adr2 or adr1,
    }


def _style_from_sku(sku: str) -> str:
    """Best-effort style key from SKU when ALT_CD is missing (e.g. 'L1850 BK' -> 'L1850')."""
    text = str(sku or "").strip()
    if not text:
        return ""
    parts = text.split()
    if len(parts) >= 2:
        return parts[0].strip()
    return text


def _whole_box_suggestions(order_number: str) -> tuple[list[dict], str | None, int]:
    """Unique styles with total boxes + masterbox L/W/H/wt (inches)."""
    header_info, *_rest = generate_order(order_number)
    if not header_info:
        return [], f"Order {order_number} not found.", 0

    database_name = "omsdata_FDC" if len(str(order_number).strip()) == 5 else "oms_EF"
    try:
        conn = pyodbc.connect(
            f'DRIVER={sql_server_config["driver"]};SERVER={sql_server_config["server"]};'
            f'DATABASE={database_name};UID={sql_server_config["username"]};PWD={sql_server_config["password"]}'
        )
        cur = conn.cursor()
        cur.execute(
            """
            SELECT
                LTRIM(RTRIM(O.PROD_CD)) AS sku,
                CAST(O.ORDER_QTY AS INT) AS qty,
                CAST(O.PC_UNIT AS INT) AS per_box,
                LTRIM(RTRIM(ISNULL(i.ALT_CD, ''))) AS style,
                i.CASE_WT AS box_wt,
                idt.CASE_LEN AS master_len,
                idt.CASE_WI AS master_wid,
                idt.CASE_HI AS master_hei
            FROM DBO.ORD_LOG O
            LEFT JOIN dbo.inv i ON O.PROD_CD = i.PROD_CD
            LEFT JOIN dbo.inv_detl idt ON O.PROD_CD = idt.prod_cd
            WHERE O.ORD_NUM = ?
            ORDER BY O.COMM_LN
            """,
            (order_number,),
        )
        lines = cur.fetchall()
        cur.close()
        conn.close()
    except Exception as e:
        current_app.logger.exception("whole-box line fetch failed for %s", order_number)
        return [], str(e), 0

    def _f(v):
        try:
            return float(v) if v is not None else 0.0
        except (TypeError, ValueError):
            return 0.0

    # Aggregate by unique style
    by_style: dict[str, dict] = {}
    for row in lines:
        sku = str(row[0] or "").strip()
        qty = int(row[1] or 0)
        per_box = int(row[2] or 0)
        style = str(row[3] or "").strip()
        # Prefer ALT_CD style code; never keep color SKU as the style key
        style = _style_from_sku(style) if style else ""
        if not style:
            style = _style_from_sku(sku)
        if not style:
            style = sku or "UNKNOWN"
        box_count = line_box_count(qty, per_box)
        if box_count < 1:
            continue
        length = _f(row[5])
        width = _f(row[6])
        height = _f(row[7])
        weight = _f(row[4])
        entry = by_style.get(style)
        if not entry:
            by_style[style] = {
                "style": style,
                "sku": sku,
                "label": style,
                "qty": box_count,
                "length": round(length, 2),
                "width": round(width, 2),
                "height": round(height, 2),
                "weight": round(weight, 2),
            }
        else:
            entry["qty"] = int(entry.get("qty") or 0) + box_count
            # Keep first non-empty masterbox dims/wt
            if not entry.get("length") and length:
                entry["length"] = round(length, 2)
                entry["width"] = round(width, 2)
                entry["height"] = round(height, 2)
            if not entry.get("weight") and weight:
                entry["weight"] = round(weight, 2)
            if not entry.get("sku") and sku:
                entry["sku"] = sku

    boxes = list(by_style.values())
    total_boxes = sum(int(b.get("qty") or 0) for b in boxes)
    missing = [
        b["style"]
        for b in boxes
        if not (
            float(b.get("length") or 0) > 0
            and float(b.get("width") or 0) > 0
            and float(b.get("height") or 0) > 0
            and float(b.get("weight") or 0) > 0
        )
    ]
    note = None
    if missing:
        note = (
            "Missing masterbox L/W/H or box wt for style(s): "
            + ", ".join(missing[:12])
            + ("…" if len(missing) > 12 else "")
        )
    return boxes, note, total_boxes


def _mix_package_suggestion(order_number: str) -> dict:
    """Mix package: total boxes from label-maker notes logic; dims/weight blank."""
    notes = fetch_order_notes(order_number)
    total = parse_boxes_from_notes(notes)
    source = "notes"
    if not total:
        # Fallback: sum full boxes from order lines (same as order items footer)
        database_name = "omsdata_FDC" if len(str(order_number).strip()) == 5 else "oms_EF"
        try:
            conn = pyodbc.connect(
                f'DRIVER={sql_server_config["driver"]};SERVER={sql_server_config["server"]};'
                f'DATABASE={database_name};UID={sql_server_config["username"]};PWD={sql_server_config["password"]}'
            )
            cur = conn.cursor()
            cur.execute(
                """
                SELECT CAST(ORDER_QTY AS INT), CAST(PC_UNIT AS INT)
                FROM DBO.ORD_LOG
                WHERE ORD_NUM = ?
                """,
                (order_number,),
            )
            total = 0
            for row in cur.fetchall():
                total += full_box_count(row[0], row[1])
            cur.close()
            conn.close()
            source = "order_lines"
        except Exception:
            total = 0
            source = "none"
    if not total or total < 1:
        total = 1
    return {
        "total_boxes": int(total),
        "source": source,
        "dim_unit": "INCH",
        "boxes": [
            {
                "style": "",
                "sku": "",
                "label": "Mix package",
                "qty": int(total),
                "length": "",
                "width": "",
                "height": "",
                "weight": "",
            }
        ],
        "note": (
            f"Total boxes from order notes (label maker): {total}."
            if source == "notes"
            else f"Total full boxes from order lines: {total}."
            if source == "order_lines"
            else "Enter total boxes and package dimensions."
        ),
    }


@order_lookup_bp.route("/order_lookup/gofo_packages", methods=["GET", "POST"])
@login_required
def gofo_packages():
    """Load or save GOFO package dims/weight for a sales order (MySQL)."""
    from py.utilities.gofo_order_packages import load_packages, save_packages

    current_user = session.get("username", "unknown")
    if request.method == "GET":
        order_number = str(
            request.args.get("order_number") or session.get("order_number") or ""
        ).strip()
        if not order_number:
            return jsonify({"ok": False, "error": "No order number provided."}), 400
        try:
            saved = load_packages(order_number)
        except Exception as e:
            current_app.logger.exception("Load gofo packages failed")
            return jsonify({"ok": False, "error": str(e)}), 500
        whole_boxes, whole_note, whole_total = _whole_box_suggestions(order_number)
        mix_suggestion = _mix_package_suggestion(order_number)

        # Ignore stale whole-box saves that stored per-SKU rows instead of unique styles
        if saved and (saved.get("package_mode") or "").lower() == "whole":
            stale = False
            for box in saved.get("boxes") or []:
                style = str(box.get("style") or "").strip()
                label = str(box.get("label") or "").strip()
                sku = str(box.get("sku") or "").strip()
                key = style or label
                if not key or " " in key or (sku and key == sku and " " in sku):
                    stale = True
                    break
            if stale:
                saved = None

        return jsonify(
            {
                "ok": True,
                "order_number": order_number,
                "saved": saved,
                "whole_box_suggestion": {
                    "dim_unit": "INCH",
                    "boxes": whole_boxes,
                    "total_boxes": whole_total,
                    "note": whole_note,
                },
                "mix_package_suggestion": mix_suggestion,
            }
        )

    payload = request.get_json(silent=True) or {}
    order_number = str(
        payload.get("order_number") or session.get("order_number") or ""
    ).strip()
    if not order_number:
        return jsonify({"ok": False, "error": "No order number provided."}), 400
    try:
        saved = save_packages(
            order_number=order_number,
            package_mode=str(payload.get("package_mode") or "mix"),
            dim_unit=str(payload.get("dim_unit") or "INCH"),
            boxes=payload.get("boxes") or [],
            updated_by=current_user,
        )
    except Exception as e:
        current_app.logger.exception("Save gofo packages failed")
        return jsonify({"ok": False, "error": str(e)}), 500

    current_app.logger.info(
        "***%s***__@@@GOFO packages saved@@@__ order=%s mode=%s boxes=%s",
        current_user,
        order_number,
        saved.get("package_mode"),
        len(saved.get("boxes") or []),
    )
    return jsonify({"ok": True, **saved})


@order_lookup_bp.route("/order_lookup/gofo_check_price", methods=["POST"])
@login_required
def gofo_check_price():
    """Estimate GOFO Parcel/Ground price for entered boxes; persists package data."""
    from py.utilities.gofo_api import estimate_prices_for_boxes, normalize_zip
    from py.utilities.gofo_order_packages import save_packages

    payload = request.get_json(silent=True) or {}
    order_number = str(
        payload.get("order_number") or session.get("order_number") or ""
    ).strip()
    if not order_number:
        return jsonify({"ok": False, "error": "No order number provided."}), 400

    header_info, *_rest = generate_order(order_number)
    if not header_info:
        return jsonify({"ok": False, "error": f"Order {order_number} not found."}), 404

    ship = _order_ship_context(header_info)
    zip_code = normalize_zip(ship["zip"])
    if not zip_code or zip_code.upper() == "N/A":
        return jsonify({"ok": False, "error": "This order has no ship-to ZIP."}), 400

    boxes = payload.get("boxes") or []
    dim_unit = str(payload.get("dim_unit") or "INCH").upper()
    package_mode = str(payload.get("package_mode") or "mix").lower()
    current_user = session.get("username", "unknown")

    try:
        save_packages(
            order_number=order_number,
            package_mode=package_mode,
            dim_unit=dim_unit,
            boxes=boxes,
            updated_by=current_user,
        )
    except Exception as e:
        current_app.logger.exception("Save packages before price check failed")
        return jsonify({"ok": False, "error": f"Could not save packages: {e}"}), 500

    current_app.logger.info(
        "***%s***__@@@GOFO check price@@@__ order=%s zip=%s boxes=%s mode=%s",
        current_user,
        order_number,
        zip_code,
        len(boxes),
        package_mode,
    )

    result = estimate_prices_for_boxes(
        boxes=boxes,
        dim_unit=dim_unit,
        zip_code=zip_code,
        country=ship["country"],
        state=ship["state"],
        city=ship["city"],
        street=ship["street"],
        phone=ship["phone"],
        name=ship["name"],
    )
    if not result.get("ok"):
        return jsonify(result), 502

    result["order_number"] = order_number
    result["package_mode"] = package_mode
    result["saved"] = True
    return jsonify(result)

