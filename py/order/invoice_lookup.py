import pyodbc
import pandas as pd
from datetime import datetime
from flask import Blueprint, request, render_template, session, current_app


def _request_client_ip() -> str:
    xf = (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
    return xf or (request.remote_addr or "")
from py.utilities.setting import Config, login_required

invoice_lookup_bp = Blueprint('invoice_lookup', __name__)

# Database configurations from settings
sql_server_config = Config.SQL_SERVER_CONFIG


def _erp_time_display(log_time) -> str:
    """Decode ERP time (centiseconds since midnight) to HH:MM:SS."""
    if log_time is None:
        return ""
    try:
        total = int(log_time)
    except (TypeError, ValueError):
        return ""
    if total <= 0:
        return ""
    seconds = total // 100
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def invoice_database_candidates(invoice_number, preferred_db=None):
    """OMS invoice numbers are not 4/5 digits only; EF uses 3-digit and 6-digit values too."""
    number = str(invoice_number or "").strip()
    dbs = []
    if preferred_db in ("omsdata_FDC", "oms_EF"):
        dbs.append(preferred_db)
    if len(number) == 5:
        fallback = ("omsdata_FDC", "oms_EF")
    else:
        fallback = ("oms_EF", "omsdata_FDC")
    for database_name in fallback:
        if database_name not in dbs:
            dbs.append(database_name)
    return dbs


def generate_invoice(invoice_number, preferred_db=None):
    """Query invoice data from the database"""
    invoice_number = str(invoice_number or "").strip()
    if not invoice_number or invoice_number in ("0", "not invoiced"):
        return None, None

    last_error = None
    for database_name in invoice_database_candidates(invoice_number, preferred_db):
        try:
            result = _generate_invoice_from_db(invoice_number, database_name)
            if result:
                return result
        except Exception as e:
            last_error = e
            current_app.logger.error(
                "Error fetching invoice %s from %s: %s", invoice_number, database_name, e
            )
    if last_error is None:
        current_app.logger.warning("Invoice %s not found in OMS databases", invoice_number)
    return None, None


def _generate_invoice_from_db(invoice_number, database_name):
    conn = pyodbc.connect(
        f'DRIVER={sql_server_config["driver"]};SERVER={sql_server_config["server"]};DATABASE={database_name};UID={sql_server_config["username"]};PWD={sql_server_config["password"]}'
    )
    cursor = conn.cursor()
    try:

        # Query invoice header information with order number
        invoice_query = """
            SELECT 
                i.INVS_NUM,
                i.INVS_CD,
                i.CUS_ID,
                c.CUS_NM,
                i.INVS_AMT,
                i.PAID_AMT,
                i.INVS_TAX,
                i.TAX_RATE,
                i.MISC_CHG,
                i.HANDL_FEE,
                i.DISCOUNT,
                i.PAID_BY,
                i.SALES_NUM,
                i.SALES_NUM2,
                i.INVS_COST,
                i.PROCESSOR,
                i.CHK_NUM,
                i.INVS_TYPE,
                i.WHS_NUM,
                i.TAXABLE_AMT,
                i.TERMS_DAY,
                i.TERM_DESC,
                i.SALES_NUM3,
                i.SALES_NUM4,
                i.BUSINESS_TYPE,
                RIGHT('0' + CAST(MONTH(DATEADD(DAY, CAST(i.INVS_DT AS INT) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                RIGHT('0' + CAST(DAY(DATEADD(DAY, CAST(i.INVS_DT AS INT) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                CAST(YEAR(DATEADD(DAY, CAST(i.INVS_DT AS INT) - 81088, '20230101')) AS VARCHAR) AS invoice_date,
                RIGHT('0' + CAST(MONTH(DATEADD(DAY, CAST(i.PROC_DT AS INT) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                RIGHT('0' + CAST(DAY(DATEADD(DAY, CAST(i.PROC_DT AS INT) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                CAST(YEAR(DATEADD(DAY, CAST(i.PROC_DT AS INT) - 81088, '20230101')) AS VARCHAR) AS process_date,
                o.ord_num,
                i.INVS_TM
            FROM invoice i
            INNER JOIN CUSTOMER c ON i.CUS_ID = c.CUS_ID
            LEFT JOIN orders o ON i.INVS_NUM = o.INVS_NUM
            WHERE i.INVS_NUM = ?
        """
        
        cursor.execute(invoice_query, (invoice_number,))
        invoice_row = cursor.fetchone()
        
        if not invoice_row:
            return None

        invoice_date = invoice_row[25] or ""
        invoice_time = _erp_time_display(invoice_row[28])
        if invoice_date and invoice_time:
            invoice_date = f"{invoice_date} {invoice_time}"

        # Extract header information
        header_info = {
            'invoice_number': str(invoice_row[0]),
            'invoice_code': str(invoice_row[1]),
            'customer_id': str(invoice_row[2]).strip(),
            'customer_name': invoice_row[3].strip() if invoice_row[3] else 'N/A',
            'invoice_amount': f"${invoice_row[4]:,.2f}" if invoice_row[4] else 'N/A',
            'paid_amount': f"${invoice_row[5]:,.2f}" if invoice_row[5] else 'N/A',
            'invoice_tax': f"${invoice_row[6]:,.2f}" if invoice_row[6] else 'N/A',
            'tax_rate': f"{invoice_row[7]:.2f}%" if invoice_row[7] else 'N/A',
            'misc_charge': f"${invoice_row[8]:,.2f}" if invoice_row[8] else 'N/A',
            'handling_fee': f"${invoice_row[9]:,.2f}" if invoice_row[9] else 'N/A',
            'discount': f"{invoice_row[10]:.2f}%" if invoice_row[10] else 'N/A',
            'paid_by': str(invoice_row[11]) if invoice_row[11] else 'N/A',
            'salesman': invoice_row[12] or 'N/A',
            'salesman2': invoice_row[13] or 'N/A',
            'invoice_cost': f"${invoice_row[14]:,.2f}" if invoice_row[14] else 'N/A',
            'processor': invoice_row[15] or 'N/A',
            'check_number': str(invoice_row[16]) if invoice_row[16] else 'N/A',
            'invoice_type': invoice_row[17] or 'N/A',
            'warehouse': invoice_row[18] or 'N/A',
            'taxable_amount': f"${invoice_row[19]:,.2f}" if invoice_row[19] else 'N/A',
            'terms_days': str(invoice_row[20]) if invoice_row[20] else 'N/A',
            'terms_description': invoice_row[21] or 'N/A',
            'salesman3': invoice_row[22] or 'N/A',
            'salesman4': invoice_row[23] or 'N/A',
            'business_type': str(invoice_row[24]) if invoice_row[24] else 'N/A',
            'invoice_date': invoice_date or 'N/A',
            'process_date': invoice_row[26],
            'order_number': str(invoice_row[27]) if invoice_row[27] else 'N/A',
            'database': database_name,
        }

        # Query invoice items
        items_query = """
            SELECT 
                il.COMM_LN,
                il.PROD_CD,
                il.UT_DESC,
                il.PROD_QTY,
                il.UNIT_PRS,
                il.DISCOUNT,
                (il.PROD_QTY * il.UNIT_PRS) AS line_total
            FROM invt_log il
            WHERE il.INVS_NUM = ?
            ORDER BY il.COMM_LN
        """
        
        cursor.execute(items_query, (invoice_number,))
        items_rows = cursor.fetchall()
        
        if items_rows:
            items_data = []
            for row in items_rows:
                items_data.append({
                    'line': row[0],
                    'sku': row[1],
                    'description': row[2] or '',
                    'quantity': int(row[3]) if row[3] else 0,
                    'unit_price': f"${row[4]:,.2f}" if row[4] else '$0.00',
                    'discount': f"{row[5]:.2f}%" if row[5] else '0.00%',
                    'line_total': f"${row[6]:,.2f}" if row[6] else '$0.00'
                })
            
            # Convert to HTML table
            items_df = pd.DataFrame(items_data)
            items_html = items_df.to_html(
                index=False,
                classes="table table-bordered table-striped",
                table_id="items_table",
                escape=False
            )
        else:
            items_html = None

        return header_info, items_html
    finally:
        try:
            cursor.close()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass


def _preferred_invoice_db():
    db = (request.args.get("db") or request.form.get("db") or "").strip()
    return db if db in ("omsdata_FDC", "oms_EF") else None


@invoice_lookup_bp.route("/invoice_lookup", methods=["GET", "POST"])
@invoice_lookup_bp.route("/invoice_lookup/<string:invoice_number>", methods=["GET"])
@login_required
def invoice_lookup(invoice_number=None):
    """Invoice lookup route"""
    preferred_db = _preferred_invoice_db()
    current_user = session['username']
    header_info = None
    items_html = None
    error_message = None

    if request.method == "POST":
        invoice_number = request.form.get("invoice_number", "").strip()
        if invoice_number:
            current_app.logger.info(
                f"***{current_user}***__@@@searched invoice@@@ {invoice_number} ip={_request_client_ip()}"
            )
            header_info, items_html = generate_invoice(invoice_number, preferred_db=preferred_db)
            
            if not header_info:
                error_message = f"Invoice {invoice_number} not found"
                current_app.logger.warning(f"Invoice {invoice_number} not found")
    
    elif invoice_number:
        # URL path-based lookup (e.g., /invoice_lookup/33623)
        current_app.logger.info(
            f"***{current_user}***__@@@invoice via URL@@@ {invoice_number} ip={_request_client_ip()}"
        )
        header_info, items_html = generate_invoice(invoice_number, preferred_db=preferred_db)
        
        if not header_info:
            error_message = f"Invoice {invoice_number} not found"
            current_app.logger.warning(f"Invoice {invoice_number} not found via URL path")
    
    else:
        # Check for query parameter (e.g., /invoice_lookup?invoice_number=33623)
        invoice_number = request.args.get('invoice_number', '').strip()
        if invoice_number:
            current_app.logger.info(
                f"***{current_user}***__@@@invoice via query@@@ {invoice_number} ip={_request_client_ip()}"
            )
            header_info, items_html = generate_invoice(invoice_number, preferred_db=preferred_db)
            
            if not header_info:
                error_message = f"Invoice {invoice_number} not found"
                current_app.logger.warning(f"Invoice {invoice_number} not found via query param")

    return render_template(
        "invoice_lookup.html",
        header_info=header_info,
        items_html=items_html,
        invoice_number=invoice_number,
        current_user=current_user,
        error_message=error_message
    )
