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


def fetch_ocean_pos_by_style_prefix(conn, style_prefix: str) -> dict[str, list[str]]:
    """Return {STYLE: [PO, ...]} for open ocean POs matching a style prefix."""
    search_param = f"{style_prefix.strip()}%"
    by_style: dict[str, list[str]] = {}
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            SELECT RTRIM(pl.prod_cd) AS sku,
                   RTRIM(pl.pur_num) AS po
            FROM dbo.plog pl
            WHERE pl.prod_cd LIKE ?
            AND pl.hdl_chg = 0
            AND pl.log_qty > 0
            AND pl.pur_num IS NOT NULL
            AND LTRIM(RTRIM(pl.pur_num)) != ''
            AND pl.pur_num NOT IN (
                SELECT DISTINCT pur_num FROM plog WHERE prod_cd LIKE ? AND hdl_chg > 0
            )
            """,
            (search_param, search_param),
        )
        for sku, po in cursor.fetchall():
            sku_key = _norm_sku(sku)
            po_key = _norm_sku(po).upper()
            if not sku_key or not po_key:
                continue
            style = sku_key.split(" ", 1)[0].upper()
            pos = by_style.setdefault(style, [])
            if po_key not in pos:
                pos.append(po_key)
    finally:
        cursor.close()
    return by_style


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


def fetch_all_open_ocean_qty(conn) -> dict[str, int]:
    """Return {trimmed SKU: ocean qty} for every open plog line."""
    lookup: dict[str, int] = {}
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            SELECT RTRIM(pl.prod_cd) AS sku,
                   SUM(CASE WHEN pl.hdl_chg > 0 THEN pl.hdl_chg ELSE pl.log_qty END) AS qty
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
            """
        )
        for sku, qty in cursor.fetchall():
            key = _norm_sku(sku)
            if key:
                lookup[key] = int(qty or 0)
    finally:
        cursor.close()
    return lookup


def style_from_sku(sku: str) -> str:
    text = _norm_sku(sku).upper()
    if not text:
        return ""
    return text.split(" ", 1)[0]


def summarize_open_ocean_styles(conn, *, top_n: int | None = 20) -> dict[str, object]:
    """Distinct styles with open ocean qty, plus the largest styles."""
    by_style: dict[str, dict[str, int]] = {}
    for sku, qty in fetch_all_open_ocean_qty(conn).items():
        if qty <= 0:
            continue
        style = style_from_sku(sku)
        if not style:
            continue
        entry = by_style.setdefault(style, {"ocean_qty": 0, "sku_count": 0})
        entry["ocean_qty"] += qty
        entry["sku_count"] += 1
    ranked = sorted(by_style.items(), key=lambda kv: (-kv[1]["ocean_qty"], kv[0]))
    picked = ranked if not top_n else ranked[: max(1, int(top_n))]
    return {
        "style_count": len(by_style),
        "sku_count": sum(v["sku_count"] for v in by_style.values()),
        "total_ocean": sum(v["ocean_qty"] for v in by_style.values()),
        "styles": [
            {"style": style, "ocean_qty": bits["ocean_qty"], "sku_count": bits["sku_count"]}
            for style, bits in picked
        ],
        "truncated": bool(top_n) and len(ranked) > max(1, int(top_n)),
    }


def resolve_ocean_qty(conn, sku: str, fallback=0) -> int:
    """Single-SKU ocean qty with fallback when plog has no open lines."""
    sku_key = _norm_sku(sku)
    if not sku_key:
        return int(fallback or 0)
    lookup = fetch_ocean_qty_by_skus(conn, [sku_key])
    if sku_key in lookup:
        return lookup[sku_key]
    return int(fallback or 0)
