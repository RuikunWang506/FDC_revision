"""Persist Loris SKU English description and color overrides."""

from __future__ import annotations

from py.utilities.database_utils import get_mysql_connection


def normalize_storage_sku(sku: str) -> str:
    """Canonical SKU: spaces instead of dashes (e.g. T3446 L.BR)."""
    text = (sku or "").strip()
    if not text:
        return ""
    text = " ".join(text.replace("-", " ").split())
    parts = text.split(None, 1)
    if len(parts) == 2:
        return f"{parts[0].upper()} {parts[1].upper()}"
    return parts[0].upper()


def sku_key_variants(sku: str) -> list[str]:
    norm = normalize_storage_sku(sku)
    if not norm:
        return []
    dash = norm.replace(" ", "-")
    seen = set()
    out = []
    for item in (norm, dash, sku.strip().upper(), sku.strip()):
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def ensure_loris_sku_meta_table(cursor):
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS onlinedb.loris_sku_meta (
            sku VARCHAR(64) NOT NULL PRIMARY KEY,
            loris_desc TEXT NULL,
            loris_color VARCHAR(128) NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                       ON UPDATE CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )


def fetch_loris_sku_meta(skus: list[str]) -> dict[str, dict]:
    """Return {SKU: {loris_desc, loris_color}} for known SKUs."""
    cleaned = []
    seen = set()
    for s in skus:
        for variant in sku_key_variants(str(s).strip()):
            if variant not in seen:
                seen.add(variant)
                cleaned.append(variant)
    if not cleaned:
        return {}

    out = {}
    connection = get_mysql_connection()
    if not connection:
        return out

    try:
        cursor = connection.cursor(dictionary=True)
        ensure_loris_sku_meta_table(cursor)
        chunk_size = 400
        for i in range(0, len(cleaned), chunk_size):
            chunk = cleaned[i : i + chunk_size]
            placeholders = ",".join(["%s"] * len(chunk))
            cursor.execute(
                f"""
                SELECT sku, loris_desc, loris_color
                FROM onlinedb.loris_sku_meta
                WHERE sku IN ({placeholders})
                """,
                tuple(chunk),
            )
            for row in cursor.fetchall():
                key = normalize_storage_sku(str(row["sku"]).strip())
                if not key:
                    continue
                out[key] = {
                    "loris_desc": (row.get("loris_desc") or "").strip(),
                    "loris_color": (row.get("loris_color") or "").strip(),
                }
                out[key.upper()] = out[key]
        connection.commit()
    finally:
        if connection.is_connected():
            connection.close()
    return out


def upsert_loris_sku_meta(records: list[dict]):
    """
    Save loris_desc / loris_color from final template upload.

    records: [{sku, loris_desc, loris_color}, ...]
    """
    if not records:
        return 0

    connection = get_mysql_connection()
    if not connection:
        raise RuntimeError("Database connection failed")

    try:
        cursor = connection.cursor()
        ensure_loris_sku_meta_table(cursor)
        rows = []
        for rec in records:
            sku = normalize_storage_sku(str(rec.get("sku") or "").strip())
            if not sku:
                continue
            desc = str(rec.get("loris_desc") or "").strip() or None
            color = str(rec.get("loris_color") or "").strip() or None
            if not desc and not color:
                continue
            rows.append((sku, desc, color))

        if not rows:
            return 0

        cursor.executemany(
            """
            INSERT INTO onlinedb.loris_sku_meta (sku, loris_desc, loris_color)
            VALUES (%s, %s, %s)
            ON DUPLICATE KEY UPDATE
                loris_desc = COALESCE(VALUES(loris_desc), loris_desc),
                loris_color = COALESCE(VALUES(loris_color), loris_color)
            """,
            rows,
        )
        connection.commit()
        return len(rows)
    finally:
        if connection.is_connected():
            connection.close()
