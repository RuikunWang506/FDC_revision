import io
import pyodbc
import pandas as pd
import streamlit as st
from datetime import date, timedelta

from py.utilities.setting import Config


st.set_page_config(page_title="FDC OMS Dashboard", page_icon="📦", layout="wide")


SQL_CFG = Config.SQL_SERVER_CONFIG
DB_OPTIONS = {
    "FDC": SQL_CFG["database_fdc"],
    "E_FAS": SQL_CFG["database_oms_ef"],
}


@st.cache_resource(show_spinner=False)
def get_sql_connection(database_name: str):
    conn_str = (
        f"DRIVER={SQL_CFG['driver']};"
        f"SERVER={SQL_CFG['server']};"
        f"DATABASE={database_name};"
        f"UID={SQL_CFG['username']};"
        f"PWD={SQL_CFG['password']}"
    )
    return pyodbc.connect(conn_str)


def run_query(database_name: str, query: str, params=None) -> pd.DataFrame:
    conn = get_sql_connection(database_name)
    try:
        cur = conn.cursor()
    except pyodbc.ProgrammingError:
        # Recover if a cached connection got closed or dropped.
        get_sql_connection.clear()
        conn = get_sql_connection(database_name)
        cur = conn.cursor()
    cur.execute(query, params or ())
    if cur.description is None:
        return pd.DataFrame()
    columns = [d[0] for d in cur.description]
    rows = cur.fetchall()
    return pd.DataFrame.from_records(rows, columns=columns)


def run_multi_db_query(query: str, params=None, selected_db: str = "Both") -> pd.DataFrame:
    frames = []
    targets = [selected_db] if selected_db in DB_OPTIONS else list(DB_OPTIONS.keys())
    for db_alias in targets:
        db_name = DB_OPTIONS[db_alias]
        try:
            df = run_query(db_name, query, params)
            if not df.empty:
                df.insert(0, "Database", db_alias)
            frames.append(df)
        except Exception as exc:
            st.warning(f"{db_alias} query failed: {exc}")
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def safe_count(table_name: str, selected_db: str = "Both") -> pd.DataFrame:
    return run_multi_db_query(f"SELECT COUNT(*) AS row_count FROM dbo.[{table_name}]", selected_db=selected_db)


def to_excel_bytes(df: pd.DataFrame) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Sheet1")
    return buf.getvalue()


st.title("FDC SQL Server Operations Dashboard")
st.caption("Using SQL Server databases from SQL_connector settings")

try:
    test_db_name = DB_OPTIONS["FDC"]
    run_query(test_db_name, "SELECT 1 AS ok")
    st.success(f"Connected to SQL Server {SQL_CFG['server']} ({test_db_name})")
except Exception as exc:
    st.error("SQL Server connection failed.")
    st.exception(exc)
    st.stop()


view = st.sidebar.radio(
    "Choose a view",
    [
        "Recent Orders",
        "Read-only SQL",
    ],
)


if view == "Recent Orders":
    default_start = date.today() - timedelta(days=7)
    start_date = st.date_input("Start date filter", value=default_start)
    st.caption("Orders are combined from FDC + E_FAS, filtered on/after Start date.")

    if isinstance(start_date, tuple):
        start_date = start_date[0]
    start_date_sql = start_date.strftime("%Y-%m-%d")

    query = f"""
        SELECT
            RIGHT('0' + CAST(MONTH(DATEADD(DAY, CAST(o.ord_dt AS INT) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
            RIGHT('0' + CAST(DAY(DATEADD(DAY, CAST(o.ord_dt AS INT) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
            CAST(YEAR(DATEADD(DAY, CAST(o.ord_dt AS INT) - 81088, '20230101')) AS VARCHAR) AS OrderDate,
            o.ord_num AS OrderNumber,
            c.cus_nm AS Customer,
            o.ord_amt AS OrderAmount,
            o.handl_fee AS HandlingFee,
            o.misc_chg AS MiscCharge,
            (o.ord_amt + o.handl_fee + o.misc_chg) AS OrderTotal
        FROM dbo.orders o
        JOIN dbo.customer c ON o.cus_id = c.cus_id
        WHERE o.ord_dt >= (DATEDIFF(day, '20230101', CAST(? AS DATE)) + 81088)
        ORDER BY o.ord_dt DESC
    """
    df = run_multi_db_query(query, (start_date_sql,), "Both")
    if "Database" in df.columns:
        df = df.drop(columns=["Database"])
    st.subheader("Order Details")
    st.dataframe(df, height=500)

    if not df.empty:
        st.download_button(
            "⬇ Orders Excel",
            data=to_excel_bytes(df),
            file_name="recent_orders.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    if not df.empty:
        sku_summary_q = """
            SELECT
                ol.prod_cd AS SKU,
                ol.ord_num AS OrderNumber,
                CAST(ISNULL(ol.order_qty, 0) AS INT) AS OrderQty,
                c.cus_nm AS CustomerName
            FROM dbo.orders o
            JOIN dbo.ord_log ol ON o.ord_num = ol.ord_num
            JOIN dbo.customer c ON o.cus_id = c.cus_id
            WHERE o.ord_dt >= (DATEDIFF(day, '20230101', CAST(? AS DATE)) + 81088)
        """
        sku_lines = run_multi_db_query(sku_summary_q, (start_date_sql,), "Both")

        st.subheader("SKU Totals (with Orders)")
        if sku_lines.empty:
            st.info("No SKU rows found for this date range.")
        else:
            sku_lines["OrderNumber"] = sku_lines["OrderNumber"].astype(str)
            sku_lines["CustomerName"] = sku_lines["CustomerName"].astype(str)
            sku_lines["OrderQty"] = pd.to_numeric(sku_lines["OrderQty"], errors="coerce").fillna(0).astype(int)

            summary_df = (
                sku_lines.groupby(["SKU"], as_index=False)
                .agg(
                    TotalOrderQty=("OrderQty", "sum"),
                    Orders=("OrderNumber", lambda s: ", ".join(sorted(set(s.tolist())))),
                    Customers=("CustomerName", lambda s: ", ".join(sorted(set(s.tolist())))),
                )
                .sort_values(["TotalOrderQty", "SKU"], ascending=[False, True])
            )
            st.dataframe(summary_df, height=500)

            st.download_button(
                "⬇ SKU Summary Excel",
                data=to_excel_bytes(summary_df),
                file_name="recent_orders_sku_summary.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )

        order_num = st.text_input("Order Number drilldown", placeholder="Paste order number")
        if order_num:
            detail_q = """
                SELECT TOP 500
                    ol.ord_num AS OrderNumber,
                    ol.prod_cd AS SKU,
                    ol.order_qty AS Qty,
                    ol.unit_prs AS UnitPrice,
                    i.alt_cd AS Style,
                    i.descrip AS Description
                FROM dbo.ord_log ol
                LEFT JOIN dbo.inv i ON i.prod_cd = ol.prod_cd
                WHERE ol.ord_num = ?
                ORDER BY ol.prod_cd
            """
            details = run_multi_db_query(detail_q, (order_num,), "Both")
            if "Database" in details.columns:
                details = details.drop(columns=["Database"])
            st.dataframe(details, height=400)


elif view == "Read-only SQL":
    run_db = st.selectbox("Run against", ["FDC", "E_FAS"], index=0)
    sql_text = st.text_area("SQL", value="SELECT TOP 50 * FROM dbo.orders ORDER BY ord_dt DESC", height=180)
    st.caption("Only read-only SQL is allowed.")

    if st.button("Run query"):
        normalized = f" {sql_text.strip().lower()} "
        allowed = normalized.strip().startswith(("select", "with", "exec sp_help", "exec sp_columns"))
        denied_keywords = (
            " insert ",
            " update ",
            " delete ",
            " drop ",
            " alter ",
            " truncate ",
            " create ",
            " replace ",
            " grant ",
            " revoke ",
            " merge ",
        )
        if (not allowed) or any(k in normalized for k in denied_keywords):
            st.error("Blocked. Only read-only SQL is allowed.")
        else:
            try:
                result_df = run_query(DB_OPTIONS[run_db], sql_text)
                st.dataframe(result_df, height=500)
                st.success(f"Returned {len(result_df)} rows")
            except Exception as exc:
                st.exception(exc)