"""Persist Loris style-level English descriptions (OTB / lookup)."""

from __future__ import annotations

from py.utilities.database_utils import get_mysql_connection


def normalize_style(style: str) -> str:
    text = (style or "").strip().upper()
    if not text:
        return ""
    return " ".join(text.replace("-", " ").split()).split()[0]


def extract_style_code(value: str) -> str:
    """Style code from a style or SKU (e.g. T3446 L.BR -> T3446)."""
    text = (value or "").strip()
    if not text:
        return ""
    text = " ".join(text.replace("-", " ").split())
    return text.split()[0].upper()


def ensure_loris_style_meta_table(cursor):
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS onlinedb.loris_style_meta (
            style VARCHAR(32) NOT NULL PRIMARY KEY,
            loris_desc TEXT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                       ON UPDATE CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )


def fetch_loris_style_meta(styles: list[str]) -> dict[str, dict]:
    """Return {STYLE: {loris_desc}} for known styles."""
    cleaned = []
    seen = set()
    for s in styles:
        key = normalize_style(str(s).strip())
        if key and key not in seen:
            seen.add(key)
            cleaned.append(key)
    if not cleaned:
        return {}

    out = {}
    connection = get_mysql_connection()
    if not connection:
        return out

    try:
        cursor = connection.cursor(dictionary=True)
        ensure_loris_style_meta_table(cursor)
        chunk_size = 400
        for i in range(0, len(cleaned), chunk_size):
            chunk = cleaned[i : i + chunk_size]
            placeholders = ",".join(["%s"] * len(chunk))
            cursor.execute(
                f"""
                SELECT style, loris_desc
                FROM onlinedb.loris_style_meta
                WHERE style IN ({placeholders})
                """,
                tuple(chunk),
            )
            for row in cursor.fetchall():
                key = normalize_style(str(row["style"]).strip())
                if not key:
                    continue
                out[key] = {
                    "loris_desc": (row.get("loris_desc") or "").strip(),
                }
        connection.commit()
    finally:
        if connection.is_connected():
            connection.close()
    return out


def upsert_loris_style_meta(records: list[dict]) -> int:
    """
    Save loris_desc at style level.

    records: [{style, loris_desc}, ...]
    """
    if not records:
        return 0

    connection = get_mysql_connection()
    if not connection:
        raise RuntimeError("Database connection failed")

    try:
        cursor = connection.cursor()
        ensure_loris_style_meta_table(cursor)
        rows = []
        for rec in records:
            style = normalize_style(str(rec.get("style") or "").strip())
            if not style:
                style = extract_style_code(str(rec.get("style") or ""))
            if not style:
                continue
            desc = str(rec.get("loris_desc") or "").strip() or None
            if not desc:
                continue
            rows.append((style, desc))

        if not rows:
            return 0

        cursor.executemany(
            """
            INSERT INTO onlinedb.loris_style_meta (style, loris_desc)
            VALUES (%s, %s)
            ON DUPLICATE KEY UPDATE loris_desc = VALUES(loris_desc)
            """,
            rows,
        )
        connection.commit()
        return len(rows)
    finally:
        if connection.is_connected():
            connection.close()


def fetch_all_style_codes() -> list[str]:
    styles: set[str] = set()
    connection = get_mysql_connection()
    if not connection:
        return []
    try:
        cursor = connection.cursor()
        ensure_loris_style_meta_table(cursor)
        cursor.execute("SELECT style FROM onlinedb.loris_style_meta WHERE style IS NOT NULL")
        for row in cursor.fetchall():
            key = normalize_style(str(row[0]).strip())
            if key:
                styles.add(key)
        cursor.execute("SELECT SKU FROM onlinedb.loris WHERE SKU IS NOT NULL")
        for row in cursor.fetchall():
            key = normalize_style(str(row[0]).strip())
            if key:
                styles.add(key)
    except Exception:
        pass
    finally:
        if connection.is_connected():
            connection.close()
    return sorted(styles, key=lambda s: s.upper())
