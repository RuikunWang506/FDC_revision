"""In-transit (ocean) qty from plog — sums open PO lines, not inv_data.on_order_qty."""
from __future__ import annotations


def _norm_sku(sku) -> str:
    if sku is None:
        return ""
    return str(sku).replace("\r", "").replace("\n", "").replace("\t", "").strip()


PLOG_OCEAN_AGG_SUBQUERY = """
(
    SELECT RTRIM(pl.prod_cd) AS prod_cd,
           SUM(CASE WHEN pl.hdl_chg > 0 THEN pl.hdl_chg ELSE pl.log_qty END) AS ocean_qty
    FROM dbo.plog pl
    WHERE pl.hdl_chg = 0
      AND pl.log_qty > 0
      AND pl.pur_num NOT IN (
          SELECT DISTINCT pur_num
          FROM dbo.plog p2
          WHERE RTRIM(p2.prod_cd) = RTRIM(pl.prod_cd)
            AND p2.hdl_chg > 0
      )
    GROUP BY RTRIM(pl.prod_cd)
)
"""

PLOG_OCEAN_JOIN = f"LEFT JOIN {PLOG_OCEAN_AGG_SUBQUERY} ocean ON RTRIM(i.PROD_CD) = ocean.prod_cd"


def ocean_qty_sql_sum(column: str = "ocean.ocean_qty") -> str:
    return f"SUM(CAST(ISNULL({column}, 0) AS INT))"


def ocean_qty_sql_scalar(column: str = "ocean.ocean_qty") -> str:
    return f"CAST(ISNULL({column}, 0) AS INT)"


def fetch_ocean_qty_by_style_prefix(conn, style_prefix: str) -> dict[str, int]:
    """Return {trimmed SKU: ocean qty} for all SKUs matching a style prefix."""
    search_param = f"{style_prefix.strip()}%"
    lookup: dict[str, int] = {}
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            SELECT RTRIM(pl.prod_cd) AS sku,
                   SUM(CASE WHEN pl.hdl_chg > 0 THEN pl.hdl_chg ELSE pl.log_qty END) AS qty
            FROM dbo.plog pl
            WHERE pl.prod_cd LIKE ?
            AND pl.hdl_chg = 0
            AND pl.log_qty > 0
            AND pl.pur_num NOT IN (
                SELECT DISTINCT pur_num FROM plog WHERE prod_cd LIKE ? AND hdl_chg > 0
            )
            GROUP BY RTRIM(pl.prod_cd)
            """,
            (search_param, search_param),
        )
        for sku, qty in cursor.fetchall():
            lookup[_norm_sku(sku)] = int(qty or 0)
    finally:
        cursor.close()
    return lookup


def fetch_ocean_qty_by_skus(conn, skus: list[str], *, chunk_size: int = 400) -> dict[str, int]:
    """Return {trimmed SKU: ocean qty} for explicit SKU list."""
    normalized = sorted({_norm_sku(s) for s in skus if _norm_sku(s)})
    if not normalized:
        return {}

    lookup: dict[str, int] = {}
    cursor = conn.cursor()
    try:
        for start in range(0, len(normalized), chunk_size):
            chunk = normalized[start : start + chunk_size]
            placeholders = ",".join("?" * len(chunk))
            cursor.execute(
                f"""
                SELECT RTRIM(pl.prod_cd) AS sku,
                       SUM(CASE WHEN pl.hdl_chg > 0 THEN pl.hdl_chg ELSE pl.log_qty END) AS qty
                FROM dbo.plog pl
                WHERE RTRIM(pl.prod_cd) IN ({placeholders})
                AND pl.hdl_chg = 0
                AND pl.log_qty > 0
                AND pl.pur_num NOT IN (
                    SELECT DISTINCT pur_num
                    FROM plog p2
                    WHERE RTRIM(p2.prod_cd) = RTRIM(pl.prod_cd)
                      AND p2.hdl_chg > 0
                )
                GROUP BY RTRIM(pl.prod_cd)
                """,
                chunk,
            )
            for sku, qty in cursor.fetchall():
                lookup[_norm_sku(sku)] = int(qty or 0)
    finally:
        cursor.close()
    return lookup


def apply_ocean_qty_to_rows(
    conn,
    rows,
    *,
    sku_index: int = 0,
    ocean_index: int = 5,
):
    """Replace on_order_qty column values with plog-summed ocean qty per SKU."""
    if not rows:
        return rows
    skus = [_norm_sku(row[sku_index]) for row in rows]
    try:
        ocean_lookup = fetch_ocean_qty_by_skus(conn, skus)
    except Exception:
        return rows
    if not ocean_lookup:
        return rows
    updated = []
    for row in rows:
        values = list(row)
        sku = _norm_sku(values[sku_index])
        if sku in ocean_lookup:
            values[ocean_index] = ocean_lookup[sku]
        updated.append(tuple(values))
    return updated


def resolve_ocean_qty(conn, sku: str, fallback=0) -> int:
    """Single-SKU ocean qty with fallback when plog has no open lines."""
    sku_key = _norm_sku(sku)
    if not sku_key:
        return int(fallback or 0)
    lookup = fetch_ocean_qty_by_skus(conn, [sku_key])
    if sku_key in lookup:
        return lookup[sku_key]
    return int(fallback or 0)
