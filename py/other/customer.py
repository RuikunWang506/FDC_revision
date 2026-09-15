from flask import Blueprint, request, jsonify,redirect,session,render_template,url_for,current_app
import pyodbc
import re
import calendar
from datetime import datetime, date, timedelta
from py.utilities.auth import login_required
from py.utilities.setting import Config

# Define the blueprint
customer_bp = Blueprint('customer', __name__)

# Database configurations from settings
sql_server_config = Config.SQL_SERVER_CONFIG

LORIS_CUSTOMER_ID = '00868'
LORIS_CUSTOMER_SEARCH = 'loris'


def _is_loris_name_match(name, customer_id, search):
    """Avoid false positives such as FLORIST when searching for Loris."""
    name_l = (name or '').strip().lower()
    id_l = (customer_id or '').strip().lower()
    search_l = (search or '').strip().lower()
    if not search_l:
        return False
    if id_l == search_l or name_l == search_l:
        return True
    if search_l == 'loris' and 'florist' in name_l:
        return False
    if re.search(r'\bloris\b', name_l):
        return True
    if name_l.startswith(search_l):
        return True
    return search_l in id_l


def _connect_sql_server(database):
    return pyodbc.connect(
        f'DRIVER={sql_server_config["driver"]};SERVER={sql_server_config["server"]};'
        f'DATABASE={database};UID={sql_server_config["username"]};PWD={sql_server_config["password"]}'
    )


def resolve_customer_by_name(customer_name):
    """
    Find the best customer match across OMS databases.

    Returns dict with cus_id, cus_nm, database_origin or None.
    """
    search = (customer_name or '').strip()
    if not search:
        return None

    matches = []
    for database in ('omsdata_FDC', 'oms_EF'):
        try:
            conn = _connect_sql_server(database)
            cursor = conn.cursor()
            query = """
                SELECT
                    c.cus_nm,
                    c.cus_id,
                    RIGHT('0' + CAST(MONTH(DATEADD(DAY, CAST(o.ord_dt AS INT) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                    RIGHT('0' + CAST(DAY(DATEADD(DAY, CAST(o.ord_dt AS INT) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                    CAST(YEAR(DATEADD(DAY, CAST(o.ord_dt AS INT) - 81088, '20230101')) AS VARCHAR) AS formatted_ord_dt,
                    o.ord_num
                FROM customer c
                LEFT JOIN (
                    SELECT cus_id, MAX(ord_num) AS max_ord_num
                    FROM orders
                    GROUP BY cus_id
                ) AS max_orders ON c.cus_id = max_orders.cus_id
                LEFT JOIN orders o ON max_orders.cus_id = o.cus_id AND max_orders.max_ord_num = o.ord_num
                WHERE c.cus_nm LIKE ? OR c.cus_id LIKE ?
            """
            cursor.execute(query, ('%' + search + '%', '%' + search + '%'))
            for row in cursor.fetchall():
                if row[2] is not None and row[3] is not None:
                    candidate = {
                        'cus_nm': row[0],
                        'cus_id': str(row[1]).strip(),
                        'database_origin': database,
                    }
                    if _is_loris_name_match(candidate['cus_nm'], candidate['cus_id'], search):
                        matches.append(candidate)
            cursor.close()
            conn.close()
        except Exception:
            continue

    if not matches:
        return None

    search_lower = search.lower()

    def score(match):
        name = (match.get('cus_nm') or '').strip().lower()
        customer_id = (match.get('cus_id') or '').strip().lower()
        if name == search_lower or customer_id == search_lower:
            return 0
        if re.search(r'\bloris\b', name):
            return 1
        if name.startswith(search_lower) or customer_id.startswith(search_lower):
            return 2
        if search_lower in name or search_lower in customer_id:
            return 3
        return 4

    matches.sort(key=lambda m: (score(m), m.get('database_origin') != 'omsdata_FDC'))
    return matches[0]


def resolve_customer_by_id(customer_id):
    """
    Find a customer by exact ID across OMS databases.

    Returns dict with cus_id, cus_nm, database_origin or None.
    """
    lookup_id = (customer_id or '').strip()
    if not lookup_id:
        return None

    for database in ('omsdata_FDC', 'oms_EF'):
        try:
            conn = _connect_sql_server(database)
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT TOP 1 LTRIM(RTRIM(c.CUS_NM)) AS customer_name, LTRIM(RTRIM(c.CUS_ID)) AS customer_id
                FROM customer c
                WHERE LTRIM(RTRIM(c.CUS_ID)) = ?
                """,
                (lookup_id,),
            )
            row = cursor.fetchone()
            cursor.close()
            conn.close()
            if row:
                return {
                    'cus_nm': str(row[0]).strip() if row[0] is not None else lookup_id,
                    'cus_id': str(row[1]).strip() if row[1] is not None else lookup_id,
                    'database_origin': database,
                }
        except Exception:
            continue

    return None


def loris_customer_page_url():
    """Customer page deep link with Loris pre-selected."""
    return url_for('customer.customer_home', search_id=LORIS_CUSTOMER_ID)


OMS_ORDER_DATE_EXPR = (
    "DATEADD(DAY, CAST(ol.olg_dt AS INT) - 81088, '20230101')"
)
OMS_ORDER_DATE_FORMATTED = (
    "RIGHT('0' + CAST(MONTH(" + OMS_ORDER_DATE_EXPR + ") AS VARCHAR), 2) + '/' + "
    "RIGHT('0' + CAST(DAY(" + OMS_ORDER_DATE_EXPR + ") AS VARCHAR), 2) + '/' + "
    "CAST(YEAR(" + OMS_ORDER_DATE_EXPR + ") AS VARCHAR)"
)
NOT_INVOICED = 'not invoiced'


def format_oms_invoice_number(invs_num):
    """Match order lookup: blank/0 invoice numbers display as 'not invoiced'."""
    if invs_num is None:
        return NOT_INVOICED
    text = str(invs_num).strip()
    if not text or text.lower() in ('nan', 'none', 'nat', '0', NOT_INVOICED):
        return NOT_INVOICED
    try:
        number = int(float(text))
    except (TypeError, ValueError):
        return text
    if number <= 0:
        return NOT_INVOICED
    return str(number)


def order_lookup_amount(ord_amt=0, handl_fee=0):
    """Same order amount as /order_lookup: ORD_AMT + HANDL_FEE."""
    try:
        base = float(ord_amt or 0)
    except (TypeError, ValueError):
        base = 0.0
    try:
        handling = float(handl_fee or 0)
    except (TypeError, ValueError):
        handling = 0.0
    return round(base + handling, 2)


def _parse_mdy(date_text):
    try:
        return datetime.strptime(str(date_text), '%m/%d/%Y')
    except Exception:
        return None


def _iso_sort_date(date_text):
    parsed = _parse_mdy(date_text)
    return parsed.strftime('%Y-%m-%d') if parsed else ''


def _as_int(value, default=0):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _as_money(value):
    try:
        return round(float(value if value is not None else 0), 2)
    except (TypeError, ValueError):
        return 0.0


def _clean_text(value):
    if value is None:
        return ''
    return str(value).strip()


CUSTOMER_EDITABLE_FIELDS = {
    'customer_name': 'CUS_NM',
    'customer_type': 'CUS_TYPE',
    'phone_1': 'PHONE',
    'phone_2': 'PHONE_2',
    'phone_3': 'PHONE_3',
    'fax': 'PHONE_4',
    'email': 'EMAIL_ADR',
    'web': 'WEB_ADR',
    'billing_address_1': 'ADDRESS',
    'billing_address_2': 'ADDRESS2',
    'billing_city': 'CITY',
    'billing_state': 'STATE',
    'billing_zip': 'ZIP',
    'billing_country': 'COUNTRY',
    'shipping_address_1': 'SHP_ADDRESS',
    'shipping_address_2': 'SHP_ADDRESS2',
    'shipping_city': 'SHP_CITY',
    'shipping_state': 'SHP_STATE',
    'shipping_zip': 'SHP_ZIP',
    'shipping_country': 'SHP_COUNTRY',
    'ship_via': 'SHIP_DESC',
    'ship_type': 'SHIP_SPE',
    'sales_rep_1': 'SALES_NUM',
    'sales_rep_2': 'SALES_NUM2',
    'sales_rep_3': 'SALES_NUM3',
    'sales_rep_4': 'SALES_NUM4',
    'department': 'DEPT_NUM',
    'terms': 'TERM',
    'terms_description': 'TERM_DESC',
}
CUSTOMER_FIELD_MAX_LEN = 80


def customer_column_for_field(field):
    """Return a whitelisted OMS customer column, or None if the field is not editable."""
    key = _clean_text(field)
    return CUSTOMER_EDITABLE_FIELDS.get(key)


def normalize_customer_field_value(value, max_len=CUSTOMER_FIELD_MAX_LEN):
    text = '' if value is None else str(value).strip()
    if len(text) > max_len:
        raise ValueError(f'Value exceeds {max_len} characters')
    return text


def _subtract_months(dt, months):
    """Shift a date backward by whole calendar months."""
    if isinstance(dt, datetime):
        base = dt.date()
    else:
        base = dt
    year = base.year
    month = base.month - months
    while month <= 0:
        month += 12
        year -= 1
    day = min(base.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def past_orders_period_range(period_index=0, months_per_period=6):
    """Return inclusive start and exclusive end dates for a 6-month slice."""
    today = date.today()
    range_start = _subtract_months(today, (period_index + 1) * months_per_period)
    if period_index == 0:
        range_end = today + timedelta(days=1)
    else:
        range_end = _subtract_months(today, period_index * months_per_period)
    return range_start, range_end


def _format_period_label(range_start, range_end_exclusive):
    display_end = range_end_exclusive - timedelta(days=1)
    return f"{range_start.strftime('%m/%d/%Y')} – {display_end.strftime('%m/%d/%Y')}"


def fetch_past_orders_window(customer_id, database_origin, period_index=0, months_per_period=6):
    """Return unique orders for one half-year window, newest periods first."""
    if database_origin not in ('omsdata_FDC', 'oms_EF'):
        return [], {}, False, ''

    range_start, range_end = past_orders_period_range(period_index, months_per_period)
    period_label = _format_period_label(range_start, range_end)
    unique_order_numbers = {}
    customer_display_name = ''

    try:
        conn = _connect_sql_server(database_origin)
        cursor = conn.cursor()

        profile_query = """
            SELECT TOP 1 LTRIM(RTRIM(c.CUS_NM)) AS customer_name
            FROM customer c
            WHERE c.cus_id = ?
        """
        cursor.execute(profile_query, (customer_id,))
        profile_row = cursor.fetchone()
        if profile_row and profile_row[0]:
            customer_display_name = str(profile_row[0]).strip()

        query = f"""
            SELECT
                ol.ord_num,
                {OMS_ORDER_DATE_FORMATTED} AS formatted_olg_dt,
                ol.olg_dt
            FROM ord_log ol
            JOIN customer c ON ol.cus_id = c.cus_id
            WHERE c.cus_id = ?
              AND {OMS_ORDER_DATE_EXPR} >= ?
              AND {OMS_ORDER_DATE_EXPR} < ?
            ORDER BY ol.olg_dt DESC
        """
        cursor.execute(query, (customer_id, range_start, range_end))
        rows = cursor.fetchall()

        for row in rows:
            ord_num = row[0]
            formatted_date = row[1]
            if not ord_num or ord_num in unique_order_numbers:
                continue

            order_query = """
                SELECT ord_amt, handl_fee, misc_chg
                FROM orders
                WHERE ord_num = ?
            """
            cursor.execute(order_query, (ord_num,))
            order_row = cursor.fetchone()
            if order_row:
                ord_amt = order_row[0] or 0
                handl_fee = order_row[1] or 0
                misc_chg = order_row[2] or 0
                total = ord_amt + handl_fee + misc_chg
                unique_order_numbers[ord_num] = {
                    'date': formatted_date,
                    'ord_amt': ord_amt,
                    'handl_fee': handl_fee,
                    'misc_chg': misc_chg,
                    'total': total,
                }

        has_more_query = f"""
            SELECT TOP 1 1
            FROM ord_log ol
            JOIN customer c ON ol.cus_id = c.cus_id
            WHERE c.cus_id = ?
              AND {OMS_ORDER_DATE_EXPR} < ?
        """
        cursor.execute(has_more_query, (customer_id, range_start))
        has_more = cursor.fetchone() is not None

        cursor.close()
        conn.close()
    except Exception as exc:
        raise exc

    orders_list = [
        {
            'order_number': ord_num,
            'date': details['date'],
            'ord_amt': details['ord_amt'],
            'handl_fee': details['handl_fee'],
            'misc_chg': details['misc_chg'],
            'total': details['total'],
        }
        for ord_num, details in unique_order_numbers.items()
    ]

    orders_list.sort(
        key=lambda item: _parse_mdy(item.get('date')) or datetime.min,
        reverse=True,
    )

    customer_summary = {
        'customer_id': customer_id,
        'customer_name': customer_display_name or customer_id,
        'database': database_origin,
        'period_orders': len(orders_list),
        'period_revenue': round(
            sum(float(item.get('total', 0) or 0) for item in orders_list), 2
        ),
    }
    return orders_list, customer_summary, has_more, period_label


def fetch_unique_order_numbers(customer_id, database_origin):
    """Return unique order summary rows for a customer (same data as customer page)."""
    if database_origin not in ('omsdata_FDC', 'oms_EF'):
        return [], {}

    unique_order_numbers = {}
    customer_display_name = ''
    try:
        conn = _connect_sql_server(database_origin)
        cursor = conn.cursor()

        profile_query = """
            SELECT TOP 1 LTRIM(RTRIM(c.CUS_NM)) AS customer_name
            FROM customer c
            WHERE c.cus_id = ?
        """
        cursor.execute(profile_query, (customer_id,))
        profile_row = cursor.fetchone()
        if profile_row and profile_row[0]:
            customer_display_name = str(profile_row[0]).strip()

        query = f"""
            SELECT
                ol.ord_num,
                {OMS_ORDER_DATE_FORMATTED} AS formatted_olg_dt
            FROM ord_log ol
            JOIN customer c ON ol.cus_id = c.cus_id
            WHERE c.cus_id = ?
        """
        cursor.execute(query, (customer_id,))
        rows = cursor.fetchall()

        for row in rows:
            ord_num = row[0]
            formatted_date = row[1]
            if not ord_num or ord_num in unique_order_numbers:
                continue

            order_query = """
                SELECT ord_amt, handl_fee, misc_chg
                FROM orders
                WHERE ord_num = ?
            """
            cursor.execute(order_query, (ord_num,))
            order_row = cursor.fetchone()
            if order_row:
                ord_amt = order_row[0] or 0
                handl_fee = order_row[1] or 0
                misc_chg = order_row[2] or 0
                total = ord_amt + handl_fee + misc_chg
                unique_order_numbers[ord_num] = {
                    'date': formatted_date,
                    'ord_amt': ord_amt,
                    'handl_fee': handl_fee,
                    'misc_chg': misc_chg,
                    'total': total,
                }

        cursor.close()
        conn.close()
    except Exception as exc:
        raise exc

    unique_order_numbers_list = [
        {
            'order_number': ord_num,
            'date': details['date'],
            'ord_amt': details['ord_amt'],
            'handl_fee': details['handl_fee'],
            'misc_chg': details['misc_chg'],
            'total': details['total'],
        }
        for ord_num, details in unique_order_numbers.items()
    ]

    unique_order_numbers_list.sort(
        key=lambda item: _parse_mdy(item.get('date')) or datetime.min,
        reverse=True,
    )

    customer_summary = {
        'customer_id': customer_id,
        'customer_name': customer_display_name or customer_id,
        'database': database_origin,
        'total_orders': len(unique_order_numbers_list),
        'total_revenue': round(
            sum(float(item.get('total', 0) or 0) for item in unique_order_numbers_list), 2
        ),
    }
    return unique_order_numbers_list, customer_summary


def database_label(database):
    if database == 'omsdata_FDC':
        return 'FDC'
    if database == 'oms_EF':
        return 'E-Fashion'
    return database or ''


def sort_customer_rows(rows):
    """Sort customer search rows by name, then ID, then database."""
    return sorted(
        rows or [],
        key=lambda row: (
            (row.get('cus_nm') or '').lower(),
            (row.get('cus_id') or '').lower(),
            (row.get('database_origin') or ''),
        ),
    )


def fetch_customer_matches(search_term=None):
    """Return (with_orders, without_orders, errors) across OMS databases."""
    term = (search_term or '').strip()
    results = []
    null_results = []
    errors = []

    for database in ('omsdata_FDC', 'oms_EF'):
        try:
            conn = _connect_sql_server(database)
            cursor = conn.cursor()
            query = """
                SELECT
                    LTRIM(RTRIM(c.cus_nm)) AS cus_nm,
                    LTRIM(RTRIM(c.cus_id)) AS cus_id,
                    RIGHT('0' + CAST(MONTH(DATEADD(DAY, CAST(o.ord_dt AS INT) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                    RIGHT('0' + CAST(DAY(DATEADD(DAY, CAST(o.ord_dt AS INT) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                    CAST(YEAR(DATEADD(DAY, CAST(o.ord_dt AS INT) - 81088, '20230101')) AS VARCHAR) AS formatted_ord_dt,
                    o.ord_num
                FROM customer c
                LEFT JOIN (
                    SELECT cus_id, MAX(ord_num) AS max_ord_num
                    FROM orders
                    GROUP BY cus_id
                ) AS max_orders ON c.cus_id = max_orders.cus_id
                LEFT JOIN orders o ON max_orders.cus_id = o.cus_id AND max_orders.max_ord_num = o.ord_num
            """
            params = []
            if term:
                query += " WHERE c.cus_nm LIKE ? OR c.cus_id LIKE ?"
                like = '%' + term + '%'
                params = [like, like]
            query += " ORDER BY c.cus_nm"
            cursor.execute(query, params)
            for row in cursor.fetchall():
                item = {
                    'cus_nm': _clean_text(row[0]),
                    'cus_id': _clean_text(row[1]),
                    'formatted_ord_dt': _clean_text(row[2]) or '',
                    'max_ord_num': _clean_text(row[3]) if row[3] is not None else '',
                    'database_origin': database,
                    'source_label': database_label(database),
                }
                if item['formatted_ord_dt'] and item['max_ord_num']:
                    results.append(item)
                else:
                    null_results.append(item)
            cursor.close()
            conn.close()
        except Exception as exc:
            errors.append(f'{database}: {exc}')

    return sort_customer_rows(results), sort_customer_rows(null_results), errors


@customer_bp.route('/customer', methods=['GET'])
@login_required
def customer_home():
    current_user = session['username']
    search_id = request.args.get('search_id', '')  # Get search_id from query parameter
    return render_template('customer.html', current_user=current_user, search_id=search_id)


@customer_bp.route('/check_customer', methods=['POST'])
@login_required
def check_customer():
    current_user = session['username']
    data = request.get_json() or {}
    customer_name = data.get('customer_name', '')
    username_from_request = data.get('username')
    if username_from_request:
        current_user = username_from_request

    current_app.logger.info("***%s***__@@@search customer@@@__ %s", current_user, data)
    results, null_results, errors = fetch_customer_matches(customer_name)
    if errors and not results and not null_results:
        return jsonify({"success": False, "message": errors[0]})
    return jsonify({
        "success": True,
        "data": results,
        "null_data": null_results,
        "errors": errors,
    })


@customer_bp.route('/list_customers', methods=['POST'])
@login_required
def list_customers():
    current_user = session['username']
    data = request.get_json() or {}
    search_term = (data.get('customer_name') or data.get('q') or '').strip()
    username_from_request = data.get('username')
    if username_from_request:
        current_user = username_from_request

    current_app.logger.info("***%s***__@@@list customers@@@__ %s", current_user, search_term or '*')
    results, null_results, errors = fetch_customer_matches(search_term or None)
    if errors and not results and not null_results:
        return jsonify({"success": False, "message": errors[0]})
    return jsonify({
        "success": True,
        "data": results,
        "null_data": null_results,
        "errors": errors,
        "mode": "list",
    })

@customer_bp.route('/customer_details/<customer_id>', methods=['GET'])
@login_required
def customer_details(customer_id):
    current_user = session['username']
    # Get username from query parameter if available
    username_from_request = request.args.get('username')
    if username_from_request:
        current_user = username_from_request
        
    database_origin = request.args.get('db')  # Get the database origin from the query parameter
    results = []
    unique_order_numbers = {}
    unique_product_data = {}
    customer_display_name = ''
    customer_profile = {}
    current_app.logger.info("***%s***__@@@search customer id@@@__ %s in database %s", current_user, customer_id, database_origin)
    # Use the selected database
    databases = [database_origin] if database_origin in ['omsdata_FDC', 'oms_EF'] else []

    for database in databases:
        try:
            conn = pyodbc.connect(
                f'DRIVER={sql_server_config["driver"]};SERVER={sql_server_config["server"]};DATABASE={database};UID={sql_server_config["username"]};PWD={sql_server_config["password"]}'
            )
            cursor = conn.cursor()

            # Resolve rich customer master profile for info panel.
            profile_query = """
                SELECT TOP 1
                    LTRIM(RTRIM(c.CUS_ID)) AS customer_id,
                    LTRIM(RTRIM(c.CUS_NM)) AS customer_name,
                    LTRIM(RTRIM(c.CUS_TYPE)) AS customer_type,
                    LTRIM(RTRIM(c.PHONE)) AS phone_1,
                    LTRIM(RTRIM(c.PHONE_2)) AS phone_2,
                    LTRIM(RTRIM(c.PHONE_3)) AS phone_3,
                    LTRIM(RTRIM(c.PHONE_4)) AS fax,
                    LTRIM(RTRIM(c.EMAIL_ADR)) AS email,
                    LTRIM(RTRIM(c.WEB_ADR)) AS web,
                    LTRIM(RTRIM(c.ADDRESS)) AS billing_address_1,
                    LTRIM(RTRIM(c.ADDRESS2)) AS billing_address_2,
                    LTRIM(RTRIM(c.CITY)) AS billing_city,
                    LTRIM(RTRIM(c.STATE)) AS billing_state,
                    LTRIM(RTRIM(c.ZIP)) AS billing_zip,
                    LTRIM(RTRIM(c.COUNTRY)) AS billing_country,
                    LTRIM(RTRIM(c.SHP_ADDRESS)) AS shipping_address_1,
                    LTRIM(RTRIM(c.SHP_ADDRESS2)) AS shipping_address_2,
                    LTRIM(RTRIM(c.SHP_CITY)) AS shipping_city,
                    LTRIM(RTRIM(c.SHP_STATE)) AS shipping_state,
                    LTRIM(RTRIM(c.SHP_ZIP)) AS shipping_zip,
                    LTRIM(RTRIM(c.SHP_COUNTRY)) AS shipping_country,
                    LTRIM(RTRIM(c.SHIP_DESC)) AS ship_via,
                    LTRIM(RTRIM(c.SHIP_SPE)) AS ship_type,
                    LTRIM(RTRIM(c.SALES_NUM)) AS sales_rep_1,
                    LTRIM(RTRIM(c.SALES_NUM2)) AS sales_rep_2,
                    LTRIM(RTRIM(c.SALES_NUM3)) AS sales_rep_3,
                    LTRIM(RTRIM(c.SALES_NUM4)) AS sales_rep_4,
                    LTRIM(RTRIM(c.DEPT_NUM)) AS department,
                    LTRIM(RTRIM(c.TERM)) AS terms,
                    LTRIM(RTRIM(c.TERM_DESC)) AS terms_description,
                    CASE WHEN ISNULL(c.FAC_ACT, 1) = 1 THEN 'Active' ELSE 'Inactive' END AS status,
                    CASE
                        WHEN ISNULL(c.FIRST_DT, 0) = 0 THEN ''
                        ELSE RIGHT('0' + CAST(MONTH(DATEADD(DAY, CAST(c.FIRST_DT AS INT) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                             RIGHT('0' + CAST(DAY(DATEADD(DAY, CAST(c.FIRST_DT AS INT) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                             CAST(YEAR(DATEADD(DAY, CAST(c.FIRST_DT AS INT) - 81088, '20230101')) AS VARCHAR)
                    END AS customer_since
                FROM customer c
                WHERE c.cus_id = ?
            """
            cursor.execute(profile_query, (customer_id,))
            profile_row = cursor.fetchone()
            if profile_row:
                profile_columns = [col[0] for col in cursor.description]
                customer_profile = {
                    profile_columns[idx]: (str(profile_row[idx]).strip() if profile_row[idx] is not None else '')
                    for idx in range(len(profile_columns))
                }
                customer_display_name = customer_profile.get('customer_name', '') or customer_display_name

            query = f"""
                SELECT
                    ol.ord_num,
                    RIGHT('0' + CAST(MONTH(DATEADD(DAY, CAST(ol.olg_dt AS INT) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                    RIGHT('0' + CAST(DAY(DATEADD(DAY, CAST(ol.olg_dt AS INT) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                    CAST(YEAR(DATEADD(DAY, CAST(ol.olg_dt AS INT) - 81088, '20230101')) AS VARCHAR) AS formatted_olg_dt,
                    ol.prod_cd,
                    ol.order_qty,
                    ol.unit_prs,
                    ol.ut_desc,
                    ol.comm_ln,
                    ol.pc_unit,
                    o.ord_amt,
                    o.handl_fee,
                    o.misc_chg,
                    o.INVS_NUM,
                    o.PO_NUM
                FROM ord_log ol
                JOIN customer c ON ol.cus_id = c.cus_id
                LEFT JOIN orders o ON ol.ord_num = o.ord_num
                WHERE c.cus_id = ?
            """
            cursor.execute(query, (customer_id,))
            rows = cursor.fetchall()

            for row in rows:
                ord_num = row[0]
                formatted_date = row[1]
                prod_cd = _clean_text(row[2])
                order_qty = _as_int(row[3], 0)
                unit_prs = _as_money(row[4])
                description = _clean_text(row[5])
                line_num = _as_int(row[6], 0)
                per_box = _as_int(row[7], 0)
                invoice_number = format_oms_invoice_number(row[11])
                po_number = _clean_text(row[12])
                order_amount = order_lookup_amount(row[8], row[9])
                misc_chg = _as_money(row[10])
                ord_amt = _as_money(row[8])
                handl_fee = _as_money(row[9])

                results.append((ord_num, formatted_date, prod_cd, order_qty, unit_prs))

                if ord_num:
                    if ord_num not in unique_order_numbers:
                        unique_order_numbers[ord_num] = {
                            'date': formatted_date,
                            'ord_amt': ord_amt,
                            'handl_fee': handl_fee,
                            'misc_chg': misc_chg,
                            'order_amount': order_amount,
                            'total': order_amount,
                            'invoice_number': invoice_number,
                            'po_number': po_number,
                            'items': [],
                        }
                    unique_order_numbers[ord_num]['items'].append({
                        'line': line_num,
                        'sku': prod_cd,
                        'description': description,
                        'qty': order_qty,
                        'price': unit_prs,
                        'per_box': per_box,
                    })

                if prod_cd:
                    if prod_cd not in unique_product_data:
                        unique_product_data[prod_cd] = {'total_qty': 0, 'prices': set()}
                    unique_product_data[prod_cd]['total_qty'] += order_qty
                    unique_product_data[prod_cd]['prices'].add(unit_prs)

            cursor.close()
            conn.close()
        except Exception as e:
            return jsonify({"success": False, "message": str(e)})

    product_summary_rows = []
    for prod_cd, data in unique_product_data.items():
        prices = sorted(data['prices'])
        product_summary_rows.append({
            'sku': prod_cd,
            'total_qty': data['total_qty'],
            'prices': prices,
        })
    product_summary_rows.sort(key=lambda row: (-int(row.get('total_qty') or 0), str(row.get('sku') or '')))

    unique_order_numbers_list = []
    for ord_num, details in unique_order_numbers.items():
        invoice_number = details.get('invoice_number') or NOT_INVOICED
        items = details.get('items') or []
        items.sort(key=lambda item: (item.get('line') or 0, str(item.get('sku') or '')))
        unique_order_numbers_list.append({
            'order_number': ord_num,
            'date': details['date'],
            'date_sort': _iso_sort_date(details['date']),
            'ord_amt': details['ord_amt'],
            'handl_fee': details['handl_fee'],
            'misc_chg': details['misc_chg'],
            'order_amount': details.get('order_amount', details.get('total', 0)),
            'total': details.get('order_amount', details.get('total', 0)),
            'invoice_number': invoice_number,
            'po_number': details.get('po_number') or '',
            'invoiced': invoice_number != NOT_INVOICED,
            'invoice_lookup_url': (
                url_for(
                    'invoice_lookup.invoice_lookup',
                    invoice_number=invoice_number,
                    db=database_origin,
                )
                if invoice_number != NOT_INVOICED
                else ''
            ),
            'order_lookup_url': url_for('order_lookup.order_lookup', order_number=ord_num),
            'items': items,
        })

    unique_order_numbers_list.sort(
        key=lambda item: _parse_mdy(item.get('date')) or datetime.min,
        reverse=True,
    )

    order_dates = [_parse_mdy(item.get('date')) for item in unique_order_numbers_list]
    order_dates = [d for d in order_dates if d is not None]
    total_orders = len(unique_order_numbers_list)
    total_order_lines = len(results)
    total_skus = len(unique_product_data)
    total_qty = sum(item.get('total_qty', 0) for item in unique_product_data.values())
    total_revenue = round(sum(float(item.get('order_amount', 0) or 0) for item in unique_order_numbers_list), 2)
    avg_order_value = round((total_revenue / total_orders), 2) if total_orders else 0

    customer_summary = {
        'customer_id': customer_id,
        'customer_name': customer_display_name or customer_id,
        'database': database_origin,
        'total_orders': total_orders,
        'total_order_lines': total_order_lines,
        'total_skus': total_skus,
        'total_qty': total_qty,
        'total_revenue': total_revenue,
        'avg_order_value': avg_order_value,
        'first_order_date': min(order_dates).strftime('%m/%d/%Y') if order_dates else 'N/A',
        'last_order_date': max(order_dates).strftime('%m/%d/%Y') if order_dates else 'N/A',
    }

    return jsonify({
        "success": True,
        "data": "",
        "product_summary": "",
        "product_summary_rows": product_summary_rows,
        "unique_order_numbers": unique_order_numbers_list,
        "customer_summary": customer_summary,
        "customer_profile": customer_profile
    })


@customer_bp.route('/update_customer_field', methods=['POST'])
@login_required
def update_customer_field():
    data = request.get_json(silent=True) or {}
    customer_id = _clean_text(data.get('customer_id'))
    database = _clean_text(data.get('database') or data.get('db'))
    field = _clean_text(data.get('field'))
    column = customer_column_for_field(field)
    if not customer_id or database not in ('omsdata_FDC', 'oms_EF') or not column:
        return jsonify({'success': False, 'message': 'Invalid customer field update'}), 400
    try:
        value = normalize_customer_field_value(data.get('value'))
    except ValueError as exc:
        return jsonify({'success': False, 'message': str(exc)}), 400

    current_user = session.get('username') or 'unknown'
    try:
        conn = _connect_sql_server(database)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT TOP 1 LTRIM(RTRIM(CUS_ID)) FROM customer WHERE LTRIM(RTRIM(CUS_ID)) = ?",
            (customer_id,),
        )
        if not cursor.fetchone():
            cursor.close()
            conn.close()
            return jsonify({'success': False, 'message': 'Customer not found'}), 404
        cursor.execute(
            f"UPDATE customer SET {column} = ? WHERE LTRIM(RTRIM(CUS_ID)) = ?",
            (value, customer_id),
        )
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as exc:
        current_app.logger.error(
            "***{}***__@@@customer field update failed@@@__ {} {} {}: {}".format(
                current_user, database, customer_id, field, exc
            )
        )
        return jsonify({'success': False, 'message': 'Failed to save customer field'}), 500

    current_app.logger.info(
        "***{}***__@@@updated customer field@@@__ {} {} {} -> {}".format(
            current_user, database, customer_id, field, value
        )
    )
    return jsonify({'success': True, 'field': field, 'value': value, 'column': column})