"""OTB Lookup — SKU/UPC search with Loris pricing and purchase history."""

from __future__ import annotations

import io
from datetime import datetime

import pandas as pd
import pyodbc

from py.loris.loris_500_upc import (
    load_500_upc_rows,
    normalize_sku,
    normalize_upc,
    update_upcs_by_sku,
)
from py.loris.loris_sku_meta import (
    fetch_loris_sku_meta,
    normalize_storage_sku,
    sku_key_variants,
    upsert_loris_sku_meta,
)
from py.loris.loris_style_meta import (
    extract_style_code,
    fetch_all_style_codes,
    fetch_loris_style_meta,
    normalize_style,
    upsert_loris_style_meta,
)
from py.loris.otb_setup_sheet import COLOR_DICT
from py.utilities.database_utils import get_mysql_connection
from py.utilities.setting import Config

LORIS_CUS_ID = "00868"
DEFAULT_RECENT_LIMIT = 50
DEFAULT_STYLE_LIMIT = 10

LOOKUP_HEADERS = [
    "SKU",
    "UPC",
    "Color",
    "Style",
    "Description",
    "fdc_desc",
    "OTB Offered",
    "Purchase History",
]

STYLE_HEADERS = [
    "Style",
    "Description",
    "fdc_desc",
    "OTB Offered",
]

SKU_SHEET_NAME = "SKUs"
STYLE_SHEET_NAME = "Styles"
LEGACY_SHEET_NAME = "OTB Lookup"

FDC_BAG_STYLE_VIEWER_BASE = "https://www.fdcef.com/bag_style_viewer"


def _fdc_style_link(style_code: str) -> str:
    style = (style_code or "").strip().upper()
    if not style:
        return ""
    return (
        f"{FDC_BAG_STYLE_VIEWER_BASE}?view=grid&container_q=&stock_filter="
        f"&desc_filter=&color_filter=&style_q={style}"
    )


def _meta_for_sku(meta_map: dict, sku: str) -> dict:
    for key in sku_key_variants(sku):
        hit = meta_map.get(key) or meta_map.get(key.upper())
        if hit:
            return hit
    return {}


def _sql_server_connection():
    cfg = Config.SQL_SERVER_CONFIG
    try:
        return pyodbc.connect(
            f"DRIVER={cfg['driver']};SERVER={cfg['server']};"
            f"DATABASE=omsdata_FDC;UID={cfg['username']};PWD={cfg['password']}"
        )
    except pyodbc.Error:
        return None


def _mysql_connection():
    return get_mysql_connection()


def _build_upc_maps():
    rows, _, stats = load_500_upc_rows()
    by_sku = {}
    by_upc = {}
    if not stats.get("found"):
        return by_sku, by_upc
    for row in rows:
        sku = normalize_sku(row.get("sku"))
        upc = normalize_upc(row.get("upc"))
        if sku and upc:
            for key in sku_key_variants(sku):
                by_sku[key.upper()] = upc
        if upc:
            by_upc[upc] = sku
    return by_sku, by_upc


def _sku_candidates(sku: str) -> list[str]:
    return sku_key_variants(sku)


def _resolve_query_to_skus(query: str, by_sku: dict, by_upc: dict) -> list[str]:
    raw = (query or "").strip()
    if not raw:
        return []

    upc_key = normalize_upc(raw)
    if upc_key and upc_key in by_upc:
        sku = normalize_sku(by_upc[upc_key])
        norm = normalize_storage_sku(sku)
        return [norm] if norm else []

    upper = raw.upper()
    if upper in by_sku:
        return [normalize_storage_sku(upper)]

    exact = _fetch_inv_exact(upper)
    if exact:
        return [exact]

    norm = normalize_storage_sku(raw)
    return [norm] if norm else []


def _fetch_inv_exact(prod_cd: str) -> str | None:
    connection = _sql_server_connection()
    if not connection:
        return None
    try:
        cursor = connection.cursor()
        for candidate in _sku_candidates(prod_cd):
            cursor.execute(
                "SELECT TOP 1 RTRIM(PROD_CD) FROM inv WHERE RTRIM(PROD_CD) = ?",
                (candidate,),
            )
            row = cursor.fetchone()
            if row and row[0]:
                return normalize_storage_sku(str(row[0]).strip())
        return None
    except Exception:
        return None
    finally:
        try:
            connection.close()
        except Exception:
            pass


def _fetch_skus_by_style(style: str, limit: int = DEFAULT_STYLE_LIMIT) -> tuple[list[str], int]:
    """Return SKU prod_cds for an internal bag style (ALT_CD), capped at limit."""
    style_key = (style or "").strip().upper()
    if not style_key:
        return [], 0

    connection = _sql_server_connection()
    if not connection:
        return [], 0
    try:
        cursor = connection.cursor()
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM inv
            WHERE RTRIM(ALT_CD) = ?
            """,
            (style_key,),
        )
        total_row = cursor.fetchone()
        total = int(total_row[0]) if total_row and total_row[0] is not None else 0
        if not total:
            return [], 0

        cursor.execute(
            f"""
            SELECT TOP {int(limit)} RTRIM(PROD_CD)
            FROM inv
            WHERE RTRIM(ALT_CD) = ?
            ORDER BY RTRIM(PROD_CD)
            """,
            (style_key,),
        )
        skus = [str(row[0]).strip() for row in cursor.fetchall() if row and row[0]]
        return skus, total
    except Exception:
        return [], 0
    finally:
        try:
            connection.close()
        except Exception:
            pass


def _fetch_purchased_skus_by_style(style: str) -> tuple[list[str], int]:
    """Return SKUs for a style that Loris (00868) has purchased; total = all inv SKUs for style."""
    style_key = (style or "").strip().upper()
    if not style_key:
        return [], 0

    connection = _sql_server_connection()
    if not connection:
        return [], 0
    try:
        cursor = connection.cursor()
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM inv
            WHERE RTRIM(ALT_CD) = ?
            """,
            (style_key,),
        )
        total_row = cursor.fetchone()
        total = int(total_row[0]) if total_row and total_row[0] is not None else 0
        if not total:
            return [], 0

        cursor.execute(
            """
            SELECT DISTINCT RTRIM(ol.prod_cd)
            FROM ord_log ol
            INNER JOIN inv i ON RTRIM(i.PROD_CD) = RTRIM(ol.prod_cd)
            WHERE ol.cus_id = ?
              AND RTRIM(i.ALT_CD) = ?
              AND ol.prod_cd IS NOT NULL
              AND LTRIM(RTRIM(ol.prod_cd)) <> ''
            ORDER BY RTRIM(ol.prod_cd)
            """,
            (LORIS_CUS_ID, style_key),
        )
        skus = [normalize_storage_sku(str(row[0]).strip()) for row in cursor.fetchall() if row and row[0]]
        skus = [s for s in skus if s]
        return skus, total
    except Exception:
        return [], 0
    finally:
        try:
            connection.close()
        except Exception:
            pass

def _fetch_inv_rows_bulk(skus: list[str]) -> dict[str, dict]:
    candidates = []
    sku_for_candidate = {}
    for sku in skus:
        for cand in _sku_candidates(sku):
            if cand not in sku_for_candidate:
                candidates.append(cand)
                sku_for_candidate[cand] = normalize_storage_sku(sku).upper()

    if not candidates:
        return {}

    connection = _sql_server_connection()
    if not connection:
        return {}
    out = {}
    try:
        cursor = connection.cursor()
        chunk_size = 200
        for i in range(0, len(candidates), chunk_size):
            chunk = candidates[i : i + chunk_size]
            placeholders = ",".join("?" for _ in chunk)
            cursor.execute(
                f"""
                SELECT RTRIM(PROD_CD), RTRIM(ALT_CD), RTRIM(UNIT_COLOR), RTRIM(DESCRIP)
                FROM inv
                WHERE RTRIM(PROD_CD) IN ({placeholders})
                """,
                chunk,
            )
            for row in cursor.fetchall():
                prod_cd = row[0] or ""
                prod_key = normalize_storage_sku(prod_cd).upper()
                orig_key = sku_for_candidate.get(prod_cd.upper()) or sku_for_candidate.get(prod_cd.strip())
                key = orig_key or prod_key
                if key not in out:
                    out[key] = {
                        "prod_cd": prod_cd,
                        "bag_style": row[1] or "",
                        "unit_color": row[2] or "",
                        "descrip": row[3] or "",
                    }
                if prod_key not in out:
                    out[prod_key] = out[key]
        return out
    except Exception:
        return out
    finally:
        try:
            connection.close()
        except Exception:
            pass


def _fetch_purchase_prices_bulk(skus: list[str]) -> dict[str, list[float]]:
    candidates = []
    sku_for_candidate = {}
    for sku in skus:
        for cand in _sku_candidates(sku):
            if cand not in sku_for_candidate:
                candidates.append(cand)
                sku_for_candidate[cand] = normalize_storage_sku(sku).upper()

    if not candidates:
        return {}

    connection = _sql_server_connection()
    if not connection:
        return {}
    grouped: dict[str, set[float]] = {}
    try:
        cursor = connection.cursor()
        chunk_size = 200
        for i in range(0, len(candidates), chunk_size):
            chunk = candidates[i : i + chunk_size]
            placeholders = ",".join("?" for _ in chunk)
            cursor.execute(
                f"""
                SELECT RTRIM(prod_cd), unit_prs
                FROM ord_log
                WHERE cus_id = ? AND RTRIM(prod_cd) IN ({placeholders})
                  AND unit_prs IS NOT NULL
                """,
                (LORIS_CUS_ID, *chunk),
            )
            for row in cursor.fetchall():
                prod_cd = str(row[0] or "").strip().upper()
                key = sku_for_candidate.get(prod_cd, prod_cd)
                try:
                    price = round(float(row[1]), 2)
                except (TypeError, ValueError):
                    continue
                grouped.setdefault(key, set()).add(price)
        return {k: sorted(v) for k, v in grouped.items()}
    except Exception:
        return {k: sorted(v) for k, v in grouped.items()}
    finally:
        try:
            connection.close()
        except Exception:
            pass


def _fetch_otb_prices_by_styles(styles: list[str]) -> dict[str, str]:
    keys = []
    seen = set()
    for style in styles:
        key = normalize_style(style)
        if key and key not in seen:
            seen.add(key)
            keys.append(key)

    connection = _mysql_connection()
    if not connection or not keys:
        return {}
    raw_prices = {}
    try:
        cursor = connection.cursor(dictionary=True)
        chunk_size = 400
        for i in range(0, len(keys), chunk_size):
            chunk = keys[i : i + chunk_size]
            placeholders = ",".join(["%s"] * len(chunk))
            cursor.execute(
                f"SELECT SKU, listprice FROM onlinedb.loris WHERE SKU IN ({placeholders})",
                tuple(chunk),
            )
            for row in cursor.fetchall():
                sku_key = normalize_style(str(row["SKU"]).strip())
                if sku_key and row.get("listprice") is not None:
                    raw_prices[sku_key] = str(row["listprice"]).strip()
    except Exception:
        pass
    finally:
        if connection.is_connected():
            connection.close()

    formatted = {}
    for style in styles:
        key = normalize_style(style)
        raw = raw_prices.get(key, "")
        if not raw:
            formatted[key] = ""
            continue
        parts = []
        for piece in raw.split(","):
            piece = piece.strip()
            if not piece:
                continue
            try:
                parts.append(f"${float(piece):.2f}")
            except ValueError:
                parts.append(piece)
        formatted[key] = ", ".join(parts)
    return formatted


def _fetch_style_descriptions_bulk(styles: list[str]) -> dict[str, str]:
    cleaned = []
    seen = set()
    for style in styles:
        text = (style or "").strip()
        if text and text not in seen:
            seen.add(text)
            cleaned.append(text)
    if not cleaned:
        return {}

    connection = _mysql_connection()
    if not connection:
        return {}
    out = {}
    try:
        cursor = connection.cursor()
        chunk_size = 400
        for i in range(0, len(cleaned), chunk_size):
            chunk = cleaned[i : i + chunk_size]
            placeholders = ",".join(["%s"] * len(chunk))
            cursor.execute(
                f"""
                SELECT TRIM(style) AS sty, TRIM(description) AS descr
                FROM onlinedb.description
                WHERE style IN ({placeholders})
                """,
                tuple(chunk),
            )
            for sty, descr in cursor.fetchall():
                if sty and descr:
                    out[str(sty).strip().upper()] = str(descr).strip()
    except Exception:
        pass
    finally:
        if connection.is_connected():
            connection.close()
    return out


def _lookup_upc(sku_key: str, by_sku: dict) -> str:
    for key in sku_key_variants(sku_key):
        upc = by_sku.get(key.upper())
        if upc:
            return upc
    return ""


def _enrich_skus(skus: list[str], by_sku: dict) -> list[dict]:
    inv_map = _fetch_inv_rows_bulk(skus)
    price_map = _fetch_purchase_prices_bulk(skus)

    style_codes = []
    for sku in skus:
        norm = normalize_storage_sku(sku)
        inv = inv_map.get(norm.upper()) or inv_map.get(sku.upper(), {})
        style = inv.get("bag_style") or extract_style_code(norm or sku)
        if style:
            style_codes.append(style)
    unique_styles = list(dict.fromkeys(style_codes))
    style_descs = _fetch_style_descriptions_bulk(unique_styles)
    style_meta_map = fetch_loris_style_meta(unique_styles)
    otb_by_style = _fetch_otb_prices_by_styles(unique_styles)

    meta_keys = []
    for sku in skus:
        meta_keys.extend(sku_key_variants(sku))
    sku_meta_map = fetch_loris_sku_meta(list(dict.fromkeys(meta_keys)))

    items = []
    for sku in skus:
        norm_sku = normalize_storage_sku(sku)
        sku_meta = _meta_for_sku(sku_meta_map, norm_sku or sku)
        inv = inv_map.get(norm_sku.upper()) or inv_map.get(sku.upper(), {})
        bag_style = inv.get("bag_style", "")
        style_key = normalize_style(bag_style or extract_style_code(norm_sku or sku))
        fdc_style_desc = style_descs.get(style_key, "")
        style_meta = style_meta_map.get(style_key, {})

        loris_desc = (style_meta.get("loris_desc") or "").strip()

        norm_key = norm_sku.upper()
        purchase = price_map.get(norm_key, []) or price_map.get(sku.upper(), [])
        otb_price = otb_by_style.get(style_key, "")
        display_sku = normalize_storage_sku(inv.get("prod_cd") or norm_sku or sku)
        items.append({
            "sku": display_sku,
            "upc": _lookup_upc(display_sku, by_sku),
            "color": _resolve_color(display_sku, sku_meta, inv.get("unit_color", "")),
            "style": style_key,
            "description": loris_desc,
            "fdc_desc": fdc_style_desc,
            "fdc_style_desc": fdc_style_desc,
            "fdc_style_link": _fdc_style_link(style_key),
            "bag_style": bag_style or style_key,
            "otb_price": otb_price,
            "purchase_prices": [f"${p:.2f}" for p in purchase],
        })
    return items


def _resolve_color(sku: str, meta: dict, unit_color: str) -> str:
    saved = (meta.get("loris_color") or "").strip()
    if saved:
        return saved
    code = (unit_color or "").strip().upper()
    if code in COLOR_DICT:
        mapped = COLOR_DICT[code]
        return mapped.title() if mapped.isupper() else mapped
    return unit_color or ""


def _display_sku(prod_cd: str, query_sku: str) -> str:
    return normalize_storage_sku(prod_cd or query_sku)


def fetch_recent_purchased_skus(limit: int = DEFAULT_RECENT_LIMIT) -> list[str]:
    connection = _sql_server_connection()
    if not connection:
        return []
    try:
        cursor = connection.cursor()
        cursor.execute(
            f"""
            SELECT TOP {int(limit)} RTRIM(prod_cd)
            FROM (
                SELECT RTRIM(prod_cd) AS prod_cd, MAX(olg_dt) AS last_dt
                FROM ord_log
                WHERE cus_id = ? AND prod_cd IS NOT NULL AND LTRIM(RTRIM(prod_cd)) <> ''
                GROUP BY RTRIM(prod_cd)
            ) t
            ORDER BY last_dt DESC
            """,
            (LORIS_CUS_ID,),
        )
        return [str(row[0]).strip() for row in cursor.fetchall() if row and row[0]]
    except Exception:
        return []
    finally:
        try:
            connection.close()
        except Exception:
            pass


def _filter_purchased_items(items: list[dict]) -> list[dict]:
    return [item for item in items if item.get("purchase_prices")]


def lookup_otb(
    query: str | None = None,
    limit: int = DEFAULT_RECENT_LIMIT,
    style_limit: int = DEFAULT_STYLE_LIMIT,
    hide_unpurchased: bool = False,
) -> dict:
    by_sku, by_upc = _build_upc_maps()
    query = (query or "").strip()
    style_code = None
    style_total = 0
    mode = "recent"
    skus: list[str] = []

    if query:
        upc_key = normalize_upc(query)
        if upc_key and upc_key in by_upc:
            sku = normalize_sku(by_upc[upc_key])
            skus = [sku.upper()] if sku else []
            mode = "search"
        elif query.upper() in by_sku:
            skus = [query.upper()]
            mode = "search"
        else:
            exact = _fetch_inv_exact(query.upper())
            if exact:
                skus = [exact]
                mode = "search"
            else:
                if hide_unpurchased:
                    style_skus, style_total = _fetch_purchased_skus_by_style(query)
                else:
                    style_skus, style_total = _fetch_skus_by_style(query, limit=style_limit)
                if style_total > 0:
                    skus = style_skus
                    style_code = query.upper()
                    mode = "style"
                else:
                    skus = _sku_candidates(query)
                    mode = "search"
    else:
        skus = fetch_recent_purchased_skus(limit)

    if not skus:
        return {
            "mode": mode,
            "query": query,
            "style": style_code,
            "style_total": style_total,
            "items": [],
            "count": 0,
        }

    items = _enrich_skus(skus, by_sku)
    if hide_unpurchased:
        items = _filter_purchased_items(items)

    result = {
        "mode": mode,
        "query": query,
        "items": items,
        "count": len(items),
        "hide_unpurchased": hide_unpurchased,
    }
    if style_code:
        result["style"] = style_code
        result["style_total"] = style_total
    return result


def fetch_all_lookup_skus() -> list[str]:
    """All known SKU keys from sku meta, 500 UPC, and legacy sku-level loris rows."""
    skus: set[str] = set()

    connection = _mysql_connection()
    if connection:
        try:
            cursor = connection.cursor()
            cursor.execute("SELECT sku FROM onlinedb.loris_sku_meta WHERE sku IS NOT NULL")
            for row in cursor.fetchall():
                text = normalize_storage_sku(str(row[0]).strip())
                if text:
                    skus.add(text)
            cursor.execute(
                "SELECT SKU FROM onlinedb.loris WHERE SKU LIKE '% %' OR SKU LIKE '%-%'"
            )
            for row in cursor.fetchall():
                text = normalize_storage_sku(str(row[0]).strip())
                if text:
                    skus.add(text)
        except Exception:
            pass
        finally:
            if connection.is_connected():
                connection.close()

    by_sku, _ = _build_upc_maps()
    for key in by_sku.keys():
        norm = normalize_storage_sku(key)
        if norm:
            skus.add(norm)

    return sorted(skus, key=lambda s: s.upper())


def migrate_otb_data_to_style_level(cursor) -> int:
    """Move SKU-level OTB prices and descriptions to style-level storage."""
    from py.loris.loris_style_meta import ensure_loris_style_meta_table

    ensure_loris_style_meta_table(cursor)
    moved = 0

    cursor.execute("SELECT SKU, listprice FROM onlinedb.loris")
    loris_rows = list(cursor.fetchall())
    for raw_sku, price in loris_rows:
        raw = str(raw_sku).strip()
        if not raw or (" " not in raw and "-" not in raw):
            continue
        style = extract_style_code(raw)
        if not style:
            continue
        cursor.execute("SELECT listprice FROM onlinedb.loris WHERE SKU = %s", (style,))
        if not cursor.fetchone():
            cursor.execute(
                "INSERT INTO onlinedb.loris (SKU, listprice) VALUES (%s, %s)",
                (style, price),
            )
        cursor.execute("DELETE FROM onlinedb.loris WHERE SKU = %s", (raw,))
        moved += 1

    cursor.execute(
        """
        SELECT sku, loris_desc
        FROM onlinedb.loris_sku_meta
        WHERE loris_desc IS NOT NULL AND TRIM(loris_desc) <> ''
        """
    )
    for raw_sku, desc in cursor.fetchall():
        style = extract_style_code(str(raw_sku))
        text = str(desc or "").strip()
        if not style or not text:
            continue
        cursor.execute(
            """
            INSERT INTO onlinedb.loris_style_meta (style, loris_desc)
            VALUES (%s, %s)
            ON DUPLICATE KEY UPDATE
                loris_desc = COALESCE(NULLIF(loris_desc, ''), VALUES(loris_desc))
            """,
            (style, text),
        )
        cursor.execute(
            "UPDATE onlinedb.loris_sku_meta SET loris_desc = NULL WHERE sku = %s",
            (raw_sku,),
        )
        moved += 1

    return moved


def backfill_otb_sku_keys() -> int:
    """Migrate dash-variant SKU keys and consolidate data to style level."""
    connection = _mysql_connection()
    if not connection:
        return 0
    migrated = 0
    try:
        cursor = connection.cursor()
        cursor.execute("SELECT SKU FROM onlinedb.loris WHERE SKU LIKE '%-%'")
        loris_rows = [row[0] for row in cursor.fetchall() if row and row[0]]
        cursor.execute("SELECT sku FROM onlinedb.loris_sku_meta WHERE sku LIKE '%-%'")
        meta_rows = [row[0] for row in cursor.fetchall() if row and row[0]]
        seen = set()
        for raw in loris_rows + meta_rows:
            norm = normalize_storage_sku(str(raw).strip())
            if not norm or norm in seen:
                continue
            seen.add(norm)
            _backfill_sku_keys(cursor, norm)
            if norm != str(raw).strip():
                migrated += 1
        migrated += migrate_otb_data_to_style_level(cursor)
        connection.commit()
    finally:
        if connection.is_connected():
            connection.close()
    return migrated


def _item_to_export_row(item: dict) -> dict:
    upc = normalize_upc(item.get("upc", ""))
    return {
        "SKU": str(item.get("sku", "") or ""),
        "UPC": upc,
        "Color": item.get("color", ""),
        "Style": item.get("style", ""),
        "Description": item.get("description", ""),
        "fdc_desc": item.get("fdc_desc", ""),
        "OTB Offered": item.get("otb_price", ""),
        "Purchase History": ", ".join(item.get("purchase_prices") or []),
    }


def _build_style_export_rows(items: list[dict]) -> list[dict]:
    by_style: dict[str, dict] = {}
    for item in items:
        style = normalize_style(item.get("style") or "")
        if not style or style in by_style:
            continue
        by_style[style] = {
            "Style": style,
            "Description": item.get("description", ""),
            "fdc_desc": item.get("fdc_desc", ""),
            "OTB Offered": item.get("otb_price", ""),
        }

    extra_styles = fetch_all_style_codes()
    style_meta_map = fetch_loris_style_meta(extra_styles)
    otb_map = _fetch_otb_prices_by_styles(extra_styles)
    fdc_descs = _fetch_style_descriptions_bulk(extra_styles)

    for style in extra_styles:
        if style in by_style:
            continue
        meta = style_meta_map.get(style, {})
        by_style[style] = {
            "Style": style,
            "Description": (meta.get("loris_desc") or "").strip(),
            "fdc_desc": fdc_descs.get(style, ""),
            "OTB Offered": otb_map.get(style, ""),
        }

    return [by_style[key] for key in sorted(by_style.keys())]


def export_lookup_workbook() -> io.BytesIO:
    """Export Styles sheet (style-level data) + SKUs sheet (SKU rows with inherited style fields)."""
    backfill_otb_sku_keys()
    skus = fetch_all_lookup_skus()
    by_sku, _ = _build_upc_maps()
    items = _enrich_skus(skus, by_sku) if skus else []
    sku_rows = [_item_to_export_row(item) for item in items]
    style_rows = _build_style_export_rows(items)

    df_styles = pd.DataFrame(style_rows, columns=STYLE_HEADERS, dtype=object)
    df_skus = pd.DataFrame(sku_rows, columns=LOOKUP_HEADERS, dtype=object)

    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df_styles.to_excel(writer, index=False, sheet_name=STYLE_SHEET_NAME)
        df_skus.to_excel(writer, index=False, sheet_name=SKU_SHEET_NAME)
    output.seek(0)
    return output


def _normalize_header(name) -> str:
    return str(name or "").strip().lower().replace("_", " ")


def _parse_otb_offered_for_db(value) -> str | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return None
    parts = []
    for piece in text.split(","):
        piece = piece.strip().lstrip("$")
        if not piece:
            continue
        try:
            parts.append(f"{float(piece):.2f}")
        except ValueError:
            parts.append(piece)
    return ", ".join(parts) if parts else None


def _cell_str(value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = str(value).strip()
    return "" if text.lower() == "nan" else text


def _backfill_sku_keys(cursor, normalized_sku: str):
    """Migrate dash-variant SKU rows to space-normalized key in loris + meta."""
    if not normalized_sku:
        return
    for alt in sku_key_variants(normalized_sku):
        if alt == normalized_sku:
            continue
        cursor.execute("SELECT listprice FROM onlinedb.loris WHERE SKU = %s", (alt,))
        old_loris = cursor.fetchone()
        if old_loris:
            cursor.execute("SELECT listprice FROM onlinedb.loris WHERE SKU = %s", (normalized_sku,))
            if cursor.fetchone():
                cursor.execute("DELETE FROM onlinedb.loris WHERE SKU = %s", (alt,))
            else:
                cursor.execute(
                    "UPDATE onlinedb.loris SET SKU = %s WHERE SKU = %s",
                    (normalized_sku, alt),
                )
        cursor.execute(
            "SELECT loris_desc, loris_color FROM onlinedb.loris_sku_meta WHERE sku = %s",
            (alt,),
        )
        old_meta = cursor.fetchone()
        if old_meta:
            cursor.execute(
                "SELECT sku FROM onlinedb.loris_sku_meta WHERE sku = %s",
                (normalized_sku,),
            )
            if cursor.fetchone():
                cursor.execute("DELETE FROM onlinedb.loris_sku_meta WHERE sku = %s", (alt,))
            else:
                cursor.execute(
                    "UPDATE onlinedb.loris_sku_meta SET sku = %s WHERE sku = %s",
                    (normalized_sku, alt),
                )


def _import_styles_dataframe(df: pd.DataFrame, cursor) -> tuple[int, int]:
    """Import Styles sheet -> loris listprice (by style) + loris_style_meta."""
    if df.empty:
        return 0, 0

    col_map = {_normalize_header(c): c for c in df.columns}

    def col(*names):
        for name in names:
            hit = col_map.get(_normalize_header(name))
            if hit is not None:
                return hit
        return None

    style_col = col("style")
    if not style_col:
        raise ValueError(f'Styles sheet must contain a "Style" column. Found: {list(df.columns)}')

    desc_col = col("description")
    otb_col = col("otb offered", "otb offered price", "listprice")

    updated_loris = 0
    style_records = []

    for _, row in df.iterrows():
        style = normalize_style(_cell_str(row.get(style_col)))
        if not style:
            continue

        listprice = _parse_otb_offered_for_db(row.get(otb_col)) if otb_col else None
        if listprice is not None:
            cursor.execute(
                """
                INSERT INTO onlinedb.loris (SKU, listprice)
                VALUES (%s, %s)
                ON DUPLICATE KEY UPDATE listprice = VALUES(listprice)
                """,
                (style, listprice),
            )
            updated_loris += 1

        desc = _cell_str(row.get(desc_col)) if desc_col else ""
        if desc:
            style_records.append({"style": style, "loris_desc": desc})

    updated_meta = upsert_loris_style_meta(style_records) if style_records else 0
    return updated_loris, updated_meta


def _import_skus_dataframe(df: pd.DataFrame, cursor) -> tuple[int, int, int, int]:
    """Import SKUs sheet -> sku color meta + style-level desc/OTB + UPC."""
    if df.empty:
        return 0, 0, 0, 0

    col_map = {_normalize_header(c): c for c in df.columns}

    def col(*names):
        for name in names:
            hit = col_map.get(_normalize_header(name))
            if hit is not None:
                return hit
        return None

    sku_col = col("sku")
    if not sku_col:
        raise ValueError(f'SKUs sheet must contain a "SKU" column. Found: {list(df.columns)}')

    style_col = col("style")
    upc_col = col("upc")
    color_col = col("color")
    desc_col = col("description")
    otb_col = col("otb offered", "otb offered price", "listprice")

    updated_loris = 0
    skipped = 0
    sku_meta_records = []
    style_records = []
    upc_updates = []

    for _, row in df.iterrows():
        raw_sku = _cell_str(row.get(sku_col))
        if not raw_sku:
            skipped += 1
            continue

        sku = normalize_storage_sku(raw_sku)
        _backfill_sku_keys(cursor, sku)
        style = normalize_style(_cell_str(row.get(style_col)) if style_col else "")
        if not style:
            style = extract_style_code(sku)

        listprice = _parse_otb_offered_for_db(row.get(otb_col)) if otb_col else None
        if listprice is not None and style:
            cursor.execute(
                """
                INSERT INTO onlinedb.loris (SKU, listprice)
                VALUES (%s, %s)
                ON DUPLICATE KEY UPDATE listprice = VALUES(listprice)
                """,
                (style, listprice),
            )
            updated_loris += 1

        desc = _cell_str(row.get(desc_col)) if desc_col else ""
        if desc and style:
            style_records.append({"style": style, "loris_desc": desc})

        color = _cell_str(row.get(color_col)) if color_col else ""
        if color:
            sku_meta_records.append({
                "sku": sku,
                "loris_desc": "",
                "loris_color": color,
            })

        upc = normalize_upc(row.get(upc_col)) if upc_col else ""
        if upc:
            upc_updates.append({"sku": sku, "upc": upc})

    updated_style_meta = upsert_loris_style_meta(style_records) if style_records else 0
    updated_sku_meta = upsert_loris_sku_meta(sku_meta_records) if sku_meta_records else 0
    updated_upc = 0
    if upc_updates:
        try:
            _, _, updated_upc = update_upcs_by_sku(upc_updates)
        except FileNotFoundError:
            pass

    return updated_loris, updated_style_meta + updated_sku_meta, updated_upc, skipped


def import_lookup_workbook(upload_file) -> dict:
    """
    Import two-sheet OTB lookup workbook (Styles + SKUs).

    Styles sheet: Style, Description, fdc_desc, OTB Offered
    SKUs sheet: SKU, UPC, Color, Style, Description, fdc_desc, OTB Offered, Purchase History

    Legacy single-sheet files (SKU column only) are still supported.
    """
    backfill_otb_sku_keys()

    xl = pd.ExcelFile(upload_file)
    sheet_map = {name.lower(): name for name in xl.sheet_names}

    styles_name = sheet_map.get(STYLE_SHEET_NAME.lower())
    skus_name = sheet_map.get(SKU_SHEET_NAME.lower()) or sheet_map.get(LEGACY_SHEET_NAME.lower())

    if not styles_name and not skus_name:
        if len(xl.sheet_names) == 1:
            skus_name = xl.sheet_names[0]
        else:
            raise ValueError(
                f'Workbook must contain "{STYLE_SHEET_NAME}" and/or "{SKU_SHEET_NAME}" sheets. '
                f"Found: {xl.sheet_names}"
            )

    connection = _mysql_connection()
    if not connection:
        raise RuntimeError("Database connection failed")

    updated_loris = 0
    updated_meta = 0
    updated_upc = 0
    skipped = 0
    row_count = 0

    try:
        cursor = connection.cursor()
        if styles_name:
            df_styles = pd.read_excel(xl, sheet_name=styles_name)
            row_count += len(df_styles)
            loris_n, meta_n = _import_styles_dataframe(df_styles, cursor)
            updated_loris += loris_n
            updated_meta += meta_n

        if skus_name:
            df_skus = pd.read_excel(xl, sheet_name=skus_name)
            row_count += len(df_skus)
            loris_n, meta_n, upc_n, skip_n = _import_skus_dataframe(df_skus, cursor)
            updated_loris += loris_n
            updated_meta += meta_n
            updated_upc += upc_n
            skipped += skip_n

        connection.commit()
    finally:
        if connection.is_connected():
            connection.close()

    return {
        "updated_loris": updated_loris,
        "updated_meta": updated_meta,
        "updated_upc": updated_upc,
        "skipped": skipped,
        "row_count": row_count,
    }


def default_lookup_export_filename() -> str:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"otb_lookup_{stamp}.xlsx"
