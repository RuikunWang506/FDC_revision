"""Mobile camera barcode scanner page + Excel export with 24h retention + async SKU resolve."""

import json
import re
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from typing import Optional, Tuple

import pandas as pd
import pyodbc
from flask import (
    Blueprint,
    abort,
    current_app,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from werkzeug.utils import secure_filename

from py.utilities.database_utils import get_mysql_connection
from py.utilities.setting import Config

mobile_scanner_bp = Blueprint("mobile_scanner", __name__)

EXPORT_RETENTION_SEC = 24 * 3600
SAFE_FILE_RE = re.compile(r"^[a-z0-9_]{1,32}_\d{8}T\d{6}Z_[a-f0-9]{8}\.xlsx$")

# Keep in sync with static/js/fdc_upc13_maps.js MATERIAL_LINE_MAP (two-digit material code → label).
MATERIAL_LINE_MAP = {
    "54": "WN",
}

_JS_COLOR_ROW_CACHE: Optional[dict] = None
_MERGED_COLOR_MAP_CACHE: Optional[dict] = None

_COLOR_MAP_MAX_KEYS = 2500
_COLOR_MAP_ABBREV_MAX_LEN = 64
_COLOR_MAP_DESC_MAX_LEN = 500


def _fdc_js_maps_path() -> Path:
    return Path(__file__).resolve().parent.parent.parent / "static" / "js" / "fdc_upc13_maps.js"


def _color_map_overlay_path() -> Path:
    return Path(current_app.root_path) / "instance" / "mobile_scanner_color_map.json"


def _normalize_color_code_key(raw) -> Optional[str]:
    if raw is None:
        return None
    d = re.sub(r"\D", "", str(raw).strip())
    if not d:
        return None
    try:
        n = int(d, 10)
    except ValueError:
        return None
    if n < 0 or n > 999:
        return None
    return str(n).zfill(3)


def invalidate_merged_color_map_cache() -> None:
    global _MERGED_COLOR_MAP_CACHE
    _MERGED_COLOR_MAP_CACHE = None


def _parse_js_color_objects() -> dict:
    """Rows from static/js/fdc_upc13_maps.js COLOR_MAP (abbrev + desc)."""
    global _JS_COLOR_ROW_CACHE
    if _JS_COLOR_ROW_CACHE is not None:
        return _JS_COLOR_ROW_CACHE
    out: dict = {}
    p = _fdc_js_maps_path()
    if p.is_file():
        text = p.read_text(encoding="utf-8", errors="replace")
        pat = re.compile(
            r"'(\d{3})'\s*:\s*\{\s*abbrev:\s*'([^']*)'\s*,\s*desc:\s*'([^']*)'\s*\}",
        )
        for m in pat.finditer(text):
            out[m.group(1)] = {"abbrev": m.group(2), "desc": m.group(3)}
    _JS_COLOR_ROW_CACHE = out
    return out


def _load_color_overlay_from_disk() -> dict:
    p = _color_map_overlay_path()
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _merge_color_map_rows(js: dict, overlay: dict) -> dict:
    out = {
        k: {"abbrev": str(v.get("abbrev", "")), "desc": str(v.get("desc", ""))}
        for k, v in js.items()
        if isinstance(v, dict)
    }
    if not overlay:
        return out
    for k, v in overlay.items():
        nk = _normalize_color_code_key(k)
        if nk is None or not isinstance(v, dict):
            continue
        out[nk] = {
            "abbrev": str(v.get("abbrev", "") or "")[:_COLOR_MAP_ABBREV_MAX_LEN],
            "desc": str(v.get("desc", "") or "")[:_COLOR_MAP_DESC_MAX_LEN],
        }
    return out


def get_merged_color_map_full() -> dict:
    """Defaults from JS merged with instance/mobile_scanner_color_map.json (overlay wins)."""
    global _MERGED_COLOR_MAP_CACHE
    if _MERGED_COLOR_MAP_CACHE is not None:
        return _MERGED_COLOR_MAP_CACHE
    js = _parse_js_color_objects()
    overlay = _load_color_overlay_from_disk()
    _MERGED_COLOR_MAP_CACHE = _merge_color_map_rows(js, overlay)
    return _MERGED_COLOR_MAP_CACHE


def get_color_abbrev_from_maps(color_id: str) -> str:
    nk = _normalize_color_code_key(color_id)
    if nk is None:
        return ""
    row = get_merged_color_map_full().get(nk)
    if isinstance(row, dict):
        return (row.get("abbrev") or "").strip()
    return ""


def _validate_color_map_payload(raw) -> Tuple[Optional[dict], list]:
    if not isinstance(raw, dict):
        return None, ["color_map must be a JSON object"]
    if len(raw) > _COLOR_MAP_MAX_KEYS:
        return None, [f"At most {_COLOR_MAP_MAX_KEYS} color codes allowed"]
    errors: list = []
    out: dict = {}
    for k, v in raw.items():
        nk = _normalize_color_code_key(k)
        if nk is None:
            errors.append(f"Invalid code key: {k!r}")
            continue
        if not isinstance(v, dict):
            errors.append(f"Code {nk}: entry must be an object")
            continue
        out[nk] = {
            "abbrev": str(v.get("abbrev", "") or "")[:_COLOR_MAP_ABBREV_MAX_LEN],
            "desc": str(v.get("desc", "") or "")[:_COLOR_MAP_DESC_MAX_LEN],
        }
    if errors:
        return None, errors[:40]
    return out, []


def _exports_dir():
    d = Path(current_app.root_path) / "instance" / "mobile_scanner_exports"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe_username(username):
    s = re.sub(r"[^a-zA-Z0-9]", "_", (username or "anon")).strip("_").lower()[:32]
    return s if s else "user"


def cleanup_old_exports():
    """Remove export files older than 24 hours."""
    d = _exports_dir()
    now = datetime.now(timezone.utc).timestamp()
    for p in d.iterdir():
        if not p.is_file():
            continue
        if p.suffix.lower() != ".xlsx":
            continue
        try:
            if now - p.stat().st_mtime > EXPORT_RETENTION_SEC:
                p.unlink(missing_ok=True)
        except OSError:
            pass


def _allowed_download(username: str, filename: str) -> bool:
    if not filename or not SAFE_FILE_RE.match(filename):
        return False
    prefix = _safe_username(username) + "_"
    return filename.startswith(prefix)


def _build_export_filename(username: str) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{_safe_username(username)}_{ts}_{uuid.uuid4().hex[:8]}.xlsx"


def validate_gtin13_check_digit(code13: str) -> bool:
    if len(code13) != 13 or not code13.isdigit():
        return False
    total = sum(int(code13[i]) * (1 if i % 2 == 0 else 3) for i in range(12))
    check = (10 - (total % 10)) % 10
    return check == int(code13[12])


def parse_fdc_upc13_digits(cleaned13: str):
    """Same layout as frontend: 1 + 2 factory + 2 material + 4 style + 3 color + check."""
    if len(cleaned13) != 13 or not cleaned13.isdigit():
        return None
    beginning = cleaned13[0]
    factory = cleaned13[1:3]
    material = cleaned13[3:5]
    style = cleaned13[5:9]
    color_id = cleaned13[9:12]
    check_ok = validate_gtin13_check_digit(cleaned13)
    mat_label = MATERIAL_LINE_MAP.get(material, material)
    color_abbrev = get_color_abbrev_from_maps(color_id)
    return {
        "cleaned": cleaned13,
        "beginning": beginning,
        "factory": factory,
        "material": material,
        "matLabel": mat_label,
        "style": style,
        "colorId": color_id,
        "colorAbbrev": color_abbrev,
        "checkOk": check_ok,
    }


def _normalize_upc_input(raw: str):
    if raw is None:
        return ""
    return re.sub(r"\D", "", str(raw).strip())


_MATCH_RANK = {
    "fuzzy_material": 0,
    "fuzzy_color_code_material": 0,
    "exact": 1,
    "style_color_token": 2,
    "sqlserver_style_color": 2,
    "fuzzy": 2,
    "fuzzy_color_code": 3,
    "style_wildcard": 4,
    "style_only": 9,
}


def _pick_best_sku(candidates: list) -> Optional[str]:
    if not candidates:
        return None
    ranked = sorted(
        candidates,
        key=lambda x: (
            _MATCH_RANK.get(x.get("match_type"), 5),
            len((x.get("sku") or "")),
        ),
    )
    return ranked[0]["sku"]


def _pick_top_ranked_skus(candidates: list) -> list:
    if not candidates:
        return []
    ranked = sorted(
        candidates,
        key=lambda x: (
            _MATCH_RANK.get(x.get("match_type"), 5),
            len((x.get("sku") or "")),
            (x.get("sku") or ""),
        ),
    )
    top_rank = _MATCH_RANK.get(ranked[0].get("match_type"), 5)
    out = []
    seen = set()
    for c in ranked:
        if _MATCH_RANK.get(c.get("match_type"), 5) != top_rank:
            break
        sku = (c.get("sku") or "").strip()
        if sku and sku not in seen:
            seen.add(sku)
            out.append(sku)
    return out


def fuzzy_find_skus(
    cursor,
    style: str,
    color_abbrev: str,
    color_id: str,
    material: str = "",
    mat_label: str = "",
    limit: int = 15,
):
    """Match products.SKU with style + color abbrev; prefer mapped material label (e.g. WN)."""
    style = (style or "").strip()
    color_abbrev = (color_abbrev or "").strip()
    color_id = (color_id or "").strip()
    material = (material or "").strip()
    mat_label = (mat_label or "").strip()
    candidates = []
    seen = set()

    if not style:
        return [], None

    like_style = f"%{style}%"
    prefixes = ["M", "m", "L", "l", "T", "t", "N", "n"]
    mapped_material = bool(material and mat_label and mat_label != material)

    # Exact tries: prefix + style + separator + color abbrev (e.g. M1116 GY)
    if color_abbrev:
        for p in prefixes:
            base = f"{p}{style}"
            for sep in (" ", ""):
                cand = f"{base}{sep}{color_abbrev}"
                cursor.execute(
                    "SELECT SKU, Style FROM products WHERE LOWER(TRIM(SKU)) = LOWER(TRIM(%s)) LIMIT 3",
                    (cand,),
                )
                for row in cursor.fetchall():
                    sku, st = row[0], row[1]
                    if sku and sku not in seen:
                        seen.add(sku)
                        candidates.append(
                            {"sku": sku, "style": st or "", "match_type": "exact"}
                        )

    def _append_fuzzy_rows(rows, match_type: str):
        for row in rows:
            sku, st = row[0], row[1]
            if sku and sku not in seen:
                seen.add(sku)
                candidates.append(
                    {"sku": sku, "style": st or "", "match_type": match_type}
                )

    lim = int(limit)

    def _fuzzy_two(like_second: str, match_type: str):
        sql = f"""
            SELECT DISTINCT SKU, Style FROM products
            WHERE
              (LOWER(SKU) LIKE LOWER(%s) OR LOWER(COALESCE(Style, '')) LIKE LOWER(%s))
              AND
              (LOWER(SKU) LIKE LOWER(%s) OR LOWER(COALESCE(Style, '')) LIKE LOWER(%s))
            ORDER BY CHAR_LENGTH(SKU) ASC
            LIMIT {lim}
        """
        cursor.execute(sql, (like_style, like_style, like_second, like_second))
        _append_fuzzy_rows(cursor.fetchall(), match_type)

    def _fuzzy_three(like_second: str, like_mat: str, match_type: str):
        sql = f"""
            SELECT DISTINCT SKU, Style FROM products
            WHERE
              (LOWER(SKU) LIKE LOWER(%s) OR LOWER(COALESCE(Style, '')) LIKE LOWER(%s))
              AND
              (LOWER(SKU) LIKE LOWER(%s) OR LOWER(COALESCE(Style, '')) LIKE LOWER(%s))
              AND
              (LOWER(SKU) LIKE LOWER(%s) OR LOWER(COALESCE(Style, '')) LIKE LOWER(%s))
            ORDER BY CHAR_LENGTH(SKU) ASC
            LIMIT {lim}
        """
        cursor.execute(
            sql,
            (
                like_style,
                like_style,
                like_second,
                like_second,
                like_mat,
                like_mat,
            ),
        )
        _append_fuzzy_rows(cursor.fetchall(), match_type)

    def _style_color_token_pass():
        if not color_abbrev:
            return
        # Explicit token patterns like "%3815 YL%" and "%3815YL%".
        patterns = [
            f"%{style} {color_abbrev}%",
            f"%{style}{color_abbrev}%",
            f"%{style}-{color_abbrev}%",
            f"%{style}_{color_abbrev}%",
        ]
        sql = f"""
            SELECT DISTINCT SKU, Style FROM products
            WHERE LOWER(SKU) LIKE LOWER(%s)
               OR LOWER(SKU) LIKE LOWER(%s)
               OR LOWER(SKU) LIKE LOWER(%s)
               OR LOWER(SKU) LIKE LOWER(%s)
            ORDER BY CHAR_LENGTH(SKU) ASC
            LIMIT {lim}
        """
        cursor.execute(sql, (patterns[0], patterns[1], patterns[2], patterns[3]))
        _append_fuzzy_rows(cursor.fetchall(), "style_color_token")

    def _style_wildcard_pass():
        # Mirror product_detail wildcard behavior (e.g. search %3305% -> T3305*).
        sql = f"""
            SELECT DISTINCT SKU, Style FROM products
            WHERE LOWER(SKU) LIKE LOWER(%s) OR LOWER(COALESCE(Style, '')) LIKE LOWER(%s)
            ORDER BY CHAR_LENGTH(SKU) ASC
            LIMIT {lim}
        """
        cursor.execute(sql, (f"%{style}%", f"%{style}%"))
        _append_fuzzy_rows(cursor.fetchall(), "style_wildcard")

    # Fuzzy: style + color abbrev (optionally + material label when 54→WN etc.)
    if color_abbrev:
        _style_color_token_pass()
        if mapped_material:
            _fuzzy_three(
                f"%{color_abbrev}%",
                f"%{mat_label}%",
                "fuzzy_material",
            )
        if not candidates:
            _fuzzy_two(f"%{color_abbrev}%", "fuzzy")
    if color_id and not candidates:
        like_color = f"%{color_id}%"
        if mapped_material:
            _fuzzy_three(like_color, f"%{mat_label}%", "fuzzy_color_code_material")
        if not candidates:
            _fuzzy_two(like_color, "fuzzy_color_code")

    # Product-detail-like wildcard pass before final fallback.
    if not candidates:
        _style_wildcard_pass()

    # Last resort: style substring only
    if not candidates:
        cursor.execute(
            f"""
            SELECT DISTINCT SKU, Style FROM products
            WHERE LOWER(SKU) LIKE LOWER(%s) OR LOWER(COALESCE(Style, '')) LIKE LOWER(%s)
            ORDER BY CHAR_LENGTH(SKU) ASC
            LIMIT {lim}
            """,
            (like_style, like_style),
        )
        for row in cursor.fetchall():
            sku, st = row[0], row[1]
            if sku and sku not in seen:
                seen.add(sku)
                candidates.append(
                    {"sku": sku, "style": st or "", "match_type": "style_only"}
                )

    best = _pick_best_sku(candidates)
    return candidates, best


def _locations_for_sku(cursor, sku: str) -> str:
    if not sku:
        return ""
    cursor.execute(
        """
        SELECT GROUP_CONCAT(DISTINCT Location ORDER BY Location SEPARATOR ', ')
        FROM products
        WHERE SKU = %s AND Location IS NOT NULL AND TRIM(Location) != ''
        """,
        (sku,),
    )
    row = cursor.fetchone()
    return (row[0] or "") if row else ""


def _locations_for_skus(cursor, skus: list) -> dict:
    out = {}
    for sku in skus:
        if not sku:
            continue
        out[sku] = _locations_for_sku(cursor, sku)
    return out


def _sqlserver_combined_available_by_sku(skus: list) -> dict:
    """Per SKU: FDC + E_FAS available (in_stock - order_qty), same join as product_detail."""
    skus = [str(s).strip() for s in (skus or []) if s and str(s).strip()]
    if not skus:
        return {}

    cfg = getattr(Config, "SQL_SERVER_CONFIG", None) or {}
    driver = cfg.get("driver")
    server = cfg.get("server")
    user = cfg.get("username")
    pwd = cfg.get("password")
    dbs = [cfg.get("database_fdc"), cfg.get("database_oms_ef")]
    if not driver or not server or not user or not pwd:
        return {}

    placeholders = ",".join("?" * len(skus))
    params = [s.upper() for s in skus]
    query = f"""
        SELECT UPPER(LTRIM(RTRIM(i.PROD_CD))) AS pc,
               COALESCE(
                   SUM(CAST(ISNULL(id.in_stock, 0) AS BIGINT) - CAST(ISNULL(id.order_qty, 0) AS BIGINT)),
                   0
               ) AS available
        FROM dbo.inv AS i
        LEFT JOIN dbo.inv_data AS id ON i.PROD_CD = id.Prod_cd
        WHERE UPPER(LTRIM(RTRIM(i.PROD_CD))) IN ({placeholders})
        GROUP BY UPPER(LTRIM(RTRIM(i.PROD_CD)))
    """

    totals_by_norm: dict[str, int] = defaultdict(int)
    for db in dbs:
        if not db:
            continue
        conn = None
        cur = None
        try:
            conn = pyodbc.connect(
                f"DRIVER={driver};SERVER={server};DATABASE={db};UID={user};PWD={pwd}",
                timeout=5,
            )
            cur = conn.cursor()
            cur.execute(query, params)
            for row in cur.fetchall():
                pc = (row[0] or "").strip().upper()
                av = int(row[1] or 0)
                totals_by_norm[pc] += av
        except Exception:
            continue
        finally:
            try:
                if cur is not None:
                    cur.close()
            except Exception:
                pass
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass

    return {s: int(totals_by_norm[s.upper()]) for s in skus}


def _sqlserver_style_color_candidates(style: str, color_abbrev: str, limit: int = 15) -> list:
    """Fallback to SQL Server inv table (product_detail-like source) for style/color token search."""
    style = (style or "").strip()
    color_abbrev = (color_abbrev or "").strip()
    if not style:
        return []

    cfg = getattr(Config, "SQL_SERVER_CONFIG", None) or {}
    driver = cfg.get("driver")
    server = cfg.get("server")
    user = cfg.get("username")
    pwd = cfg.get("password")
    dbs = [cfg.get("database_fdc"), cfg.get("database_oms_ef")]
    if not driver or not server or not user or not pwd:
        return []

    out = []
    seen = set()
    lim = max(1, int(limit))
    for db in dbs:
        if not db:
            continue
        conn = None
        cur = None
        try:
            conn = pyodbc.connect(
                f"DRIVER={driver};SERVER={server};DATABASE={db};UID={user};PWD={pwd}",
                timeout=5,
            )
            cur = conn.cursor()
            if color_abbrev:
                query = f"""
                    SELECT TOP {lim} i.PROD_CD
                    FROM dbo.inv AS i
                    WHERE (UPPER(i.PROD_CD) LIKE ? OR UPPER(i.ALT_CD) LIKE ?)
                      AND (UPPER(i.PROD_CD) LIKE ? OR UPPER(COALESCE(i.UNIT_COLOR, '')) LIKE ?)
                    ORDER BY LEN(i.PROD_CD), i.PROD_CD
                """
                cur.execute(
                    query,
                    f"%{style.upper()}%",
                    f"%{style.upper()}%",
                    f"%{style.upper()} {color_abbrev.upper()}%",
                    f"%{color_abbrev.upper()}%",
                )
            else:
                query = f"""
                    SELECT TOP {lim} i.PROD_CD
                    FROM dbo.inv AS i
                    WHERE UPPER(i.PROD_CD) LIKE ? OR UPPER(i.ALT_CD) LIKE ?
                    ORDER BY LEN(i.PROD_CD), i.PROD_CD
                """
                cur.execute(query, f"%{style.upper()}%", f"%{style.upper()}%")

            for row in cur.fetchall():
                sku = (row[0] or "").strip() if row else ""
                if sku and sku not in seen:
                    seen.add(sku)
                    out.append(sku)
        except Exception:
            continue
        finally:
            try:
                if cur is not None:
                    cur.close()
            except Exception:
                pass
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass
    return out


def _records_to_rows(records):
    rows = []
    for r in records:
        if not isinstance(r, dict):
            continue
        raw = (r.get("raw") or "").strip()
        res = r.get("resolved") if isinstance(r.get("resolved"), dict) else {}
        scan_time = (r.get("time_iso") or r.get("time_display") or "").strip()
        selected_sku = (res.get("selected_sku") or "").strip()
        matched_skus = res.get("matched_skus") if isinstance(res.get("matched_skus"), list) else []
        loc_map = res.get("locations_by_sku") if isinstance(res.get("locations_by_sku"), dict) else {}
        avail_map = (
            res.get("available_by_sku")
            if isinstance(res.get("available_by_sku"), dict)
            else {}
        )

        def _avail_for(sku_key: str) -> str:
            v = avail_map.get(sku_key)
            if v is None or v == "":
                return ""
            return str(v)

        if selected_sku:
            rows.append(
                {
                    "sku": selected_sku,
                    "upc": raw,
                    "available": _avail_for(selected_sku),
                    "loc": str(loc_map.get(selected_sku, res.get("locations", "")) or "").strip(),
                    "time": scan_time,
                }
            )
        elif matched_skus:
            for sku in matched_skus:
                sku_s = (sku or "").strip()
                rows.append(
                    {
                        "sku": sku_s,
                        "upc": raw,
                        "available": _avail_for(sku_s),
                        "loc": str(loc_map.get(sku_s, "") or "").strip(),
                        "time": scan_time,
                    }
                )
        else:
            ms = (res.get("matched_sku") or "").strip()
            rows.append(
                {
                    "sku": ms,
                    "upc": raw,
                    "available": _avail_for(ms) if ms else str(res.get("available", "") or ""),
                    "loc": (res.get("locations") or "").strip(),
                    "time": scan_time,
                }
            )
    return rows


def _canonical_upc_for_store(raw: str) -> str:
    d = re.sub(r"\D", "", str(raw or "").strip())
    if len(d) == 12:
        d = "0" + d
    return d


def _ensure_confirm_table(cursor) -> None:
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS mobile_scanner_upc_sku_confirmed (
            id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
            upc VARCHAR(32) NOT NULL,
            sku VARCHAR(128) NOT NULL,
            location VARCHAR(512) NOT NULL DEFAULT '',
            confirmed_by VARCHAR(128) NOT NULL DEFAULT '',
            confirmed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            PRIMARY KEY (id),
            UNIQUE KEY uq_mobile_scanner_upc (upc)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """
    )


def _save_confirmed_upc_sku(upc: str, sku: str, location: str, username: str) -> None:
    conn = get_mysql_connection()
    if conn is None:
        raise RuntimeError("Database unavailable")
    cur = None
    try:
        cur = conn.cursor()
        _ensure_confirm_table(cur)
        cur.execute(
            """
            INSERT INTO mobile_scanner_upc_sku_confirmed (upc, sku, location, confirmed_by)
            VALUES (%s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                sku = VALUES(sku),
                location = VALUES(location),
                confirmed_by = VALUES(confirmed_by),
                confirmed_at = CURRENT_TIMESTAMP
            """,
            (
                _canonical_upc_for_store(upc),
                str(sku or "").strip(),
                str(location or "").strip(),
                str(username or "").strip(),
            ),
        )
        conn.commit()
    finally:
        if cur is not None:
            try:
                cur.close()
            except Exception:
                pass
        try:
            conn.close()
        except Exception:
            pass


def _get_confirmed_upc_sku(upc: str) -> Optional[dict]:
    conn = get_mysql_connection()
    if conn is None:
        return None
    cur = None
    try:
        cur = conn.cursor()
        _ensure_confirm_table(cur)
        cur.execute(
            """
            SELECT upc, sku, location, confirmed_by, confirmed_at
            FROM mobile_scanner_upc_sku_confirmed
            WHERE upc = %s
            LIMIT 1
            """,
            (_canonical_upc_for_store(upc),),
        )
        row = cur.fetchone()
        if not row:
            return None
        return {
            "upc": (row[0] or "").strip(),
            "sku": (row[1] or "").strip(),
            "location": (row[2] or "").strip(),
            "confirmed_by": (row[3] or "").strip(),
            "confirmed_at": str(row[4]) if row[4] is not None else "",
        }
    except Exception:
        return None
    finally:
        if cur is not None:
            try:
                cur.close()
            except Exception:
                pass
        try:
            conn.close()
        except Exception:
            pass


@mobile_scanner_bp.route("/mobile_scanner")
def mobile_scanner_page():
    if "username" not in session:
        return redirect(url_for("auth.login"))
    cleanup_old_exports()
    return render_template(
        "mobile_scanner.html",
        current_user=session.get("username", ""),
    )


@mobile_scanner_bp.route("/mobile_scanner/color-map", methods=["GET"])
def mobile_scanner_color_map_get():
    """Merged COLOR_MAP (JS defaults + instance overlay) for UI + single source for resolve."""
    if "username" not in session:
        return jsonify({"success": False, "error": "Unauthorized"}), 401
    try:
        merged = get_merged_color_map_full()
        return jsonify({"success": True, "color_map": merged})
    except Exception as e:
        current_app.logger.exception("mobile_scanner color-map GET")
        return jsonify({"success": False, "error": str(e)}), 500


@mobile_scanner_bp.route("/mobile_scanner/color-map", methods=["POST"])
def mobile_scanner_color_map_post():
    """Persist editor table to instance/mobile_scanner_color_map.json (merged over JS on read)."""
    if "username" not in session:
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    data = request.get_json(force=True, silent=True) or {}
    raw_map = data.get("color_map")
    cleaned, errs = _validate_color_map_payload(raw_map)
    if cleaned is None:
        return jsonify({"success": False, "error": "; ".join(errs) or "Invalid payload"}), 400

    path = _color_map_overlay_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(cleaned, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        invalidate_merged_color_map_cache()
    except OSError as e:
        current_app.logger.exception("mobile_scanner color-map POST write")
        return jsonify({"success": False, "error": str(e)}), 500

    return jsonify({"success": True, "saved_codes": len(cleaned)})


@mobile_scanner_bp.route("/mobile_scanner/color-map", methods=["DELETE"])
def mobile_scanner_color_map_delete():
    """Remove server overlay; merged map falls back to JS defaults only."""
    if "username" not in session:
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    path = _color_map_overlay_path()
    try:
        path.unlink(missing_ok=True)
        invalidate_merged_color_map_cache()
    except OSError as e:
        current_app.logger.exception("mobile_scanner color-map DELETE")
        return jsonify({"success": False, "error": str(e)}), 500

    return jsonify({"success": True})


@mobile_scanner_bp.route("/mobile_scanner/resolve-upc", methods=["POST"])
def mobile_scanner_resolve_upc():
    """Fuzzy-match products.SKU from parsed 13-digit UPC (style + color abbrev)."""
    if "username" not in session:
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    data = request.get_json(force=True, silent=True) or {}
    cleaned = _normalize_upc_input(data.get("upc", ""))
    if len(cleaned) != 13:
        return jsonify({"success": False, "error": "UPC must be 13 digits"}), 400

    parsed = parse_fdc_upc13_digits(cleaned)
    if not parsed:
        return jsonify({"success": False, "error": "Invalid UPC"}), 400

    confirmed = _get_confirmed_upc_sku(cleaned)
    if confirmed and confirmed.get("sku"):
        sku = confirmed["sku"]
        loc = confirmed.get("location", "")
        avail_by_sku = _sqlserver_combined_available_by_sku([sku])
        av = avail_by_sku.get(sku, 0)
        return jsonify(
            {
                "success": True,
                "error": None,
                "upc": cleaned,
                "parsed": parsed,
                "matched_sku": sku,
                "selected_sku": sku,
                "matched_skus": [sku],
                "sku_candidates": [
                    {
                        "sku": sku,
                        "style": parsed["style"],
                        "match_type": "confirmed_table",
                    }
                ],
                "locations": loc,
                "locations_by_sku": {sku: loc},
                "available": av,
                "available_by_sku": avail_by_sku,
                "from_confirmed": True,
                "confirmed_by": confirmed.get("confirmed_by", ""),
                "confirmed_at": confirmed.get("confirmed_at", ""),
            }
        )

    conn = get_mysql_connection()
    if conn is None:
        return jsonify({"success": False, "error": "Database unavailable"}), 503

    candidates = []
    best = None
    matched_skus = []
    locations = ""
    locations_by_sku = {}
    available_by_sku = {}
    available_single = None
    err_msg = ""
    cursor = None
    try:
        cursor = conn.cursor()
        candidates, best = fuzzy_find_skus(
            cursor,
            parsed["style"],
            parsed["colorAbbrev"],
            parsed["colorId"],
            material=parsed["material"],
            mat_label=parsed["matLabel"],
            limit=15,
        )
        if not candidates:
            ss_skus = _sqlserver_style_color_candidates(
                parsed["style"],
                parsed["colorAbbrev"],
                limit=15,
            )
            for sku in ss_skus:
                candidates.append(
                    {
                        "sku": sku,
                        "style": parsed["style"],
                        "match_type": "sqlserver_style_color",
                    }
                )
            if ss_skus and not best:
                best = ss_skus[0]
        if not best:
            best = _pick_best_sku(candidates)
        matched_skus = _pick_top_ranked_skus(candidates)
        locations_by_sku = _locations_for_skus(cursor, matched_skus)
        available_by_sku = _sqlserver_combined_available_by_sku(matched_skus)
        if best:
            locations = _locations_for_sku(cursor, best)
            available_single = available_by_sku.get(best)
        elif matched_skus:
            available_single = available_by_sku.get(matched_skus[0])
    except Exception as e:
        current_app.logger.exception("mobile_scanner resolve-upc")
        err_msg = str(e)
    finally:
        if cursor is not None:
            try:
                cursor.close()
            except Exception:
                pass
        try:
            conn.close()
        except Exception:
            pass

    return jsonify(
        {
            "success": err_msg == "",
            "error": err_msg or None,
            "upc": cleaned,
            "parsed": parsed,
            "matched_sku": best,
            "matched_skus": matched_skus,
            "sku_candidates": candidates,
            "locations": locations,
            "locations_by_sku": locations_by_sku,
            "available": available_single if available_single is not None else "",
            "available_by_sku": available_by_sku,
        }
    )


@mobile_scanner_bp.route("/mobile_scanner/export", methods=["POST"])
def mobile_scanner_export():
    if "username" not in session:
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    cleanup_old_exports()
    data = request.get_json(force=True, silent=True) or {}
    records = data.get("records")
    if not isinstance(records, list) or len(records) == 0:
        return jsonify({"success": False, "error": "No rows to export"}), 400

    rows = _records_to_rows(reversed(records))
    if not rows:
        return jsonify({"success": False, "error": "No valid rows"}), 400

    df = pd.DataFrame(rows)
    if "available" in df.columns:
        df = df.rename(columns={"available": "Avail"})
    fname = _build_export_filename(session["username"])
    path = _exports_dir() / fname
    try:
        df.to_excel(path, index=False, engine="openpyxl")
    except Exception as e:
        current_app.logger.exception("mobile_scanner export failed")
        return jsonify({"success": False, "error": str(e)}), 500

    return jsonify(
        {
            "success": True,
            "filename": fname,
            "download_url": url_for(
                "mobile_scanner.mobile_scanner_download_export",
                filename=fname,
            ),
            "row_count": len(rows),
        }
    )


@mobile_scanner_bp.route("/mobile_scanner/confirm-upc-sku", methods=["POST"])
def mobile_scanner_confirm_upc_sku():
    if "username" not in session:
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    data = request.get_json(force=True, silent=True) or {}
    upc = _canonical_upc_for_store(data.get("upc", ""))
    sku = str(data.get("sku", "") or "").strip()
    location = str(data.get("location", "") or "").strip()
    if len(upc) != 13 or not upc.isdigit():
        return jsonify({"success": False, "error": "UPC must be 13 digits"}), 400
    if not sku:
        return jsonify({"success": False, "error": "SKU is required"}), 400

    try:
        _save_confirmed_upc_sku(
            upc=upc,
            sku=sku,
            location=location,
            username=session.get("username", ""),
        )
    except Exception as e:
        current_app.logger.exception("mobile_scanner confirm-upc-sku")
        return jsonify({"success": False, "error": str(e)}), 500

    return jsonify(
        {
            "success": True,
            "upc": upc,
            "sku": sku,
            "location": location,
        }
    )


@mobile_scanner_bp.route("/mobile_scanner/exports/list", methods=["GET"])
def mobile_scanner_exports_list():
    if "username" not in session:
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    cleanup_old_exports()
    user = _safe_username(session["username"])
    prefix = user + "_"
    items = []
    d = _exports_dir()
    for p in sorted(d.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if not p.is_file() or p.suffix.lower() != ".xlsx":
            continue
        name = p.name
        if not name.startswith(prefix):
            continue
        if not SAFE_FILE_RE.match(name):
            continue
        try:
            st = p.stat()
            items.append(
                {
                    "filename": name,
                    "size_bytes": st.st_size,
                    "created_iso": datetime.fromtimestamp(
                        st.st_mtime, tz=timezone.utc
                    ).isoformat(),
                    "download_url": url_for(
                        "mobile_scanner.mobile_scanner_download_export",
                        filename=name,
                    ),
                }
            )
        except OSError:
            continue

    return jsonify({"success": True, "files": items, "retention_hours": 24})


@mobile_scanner_bp.route("/mobile_scanner/exports/download/<filename>")
def mobile_scanner_download_export(filename):
    if "username" not in session:
        return redirect(url_for("auth.login"))

    fn = secure_filename(filename)
    if fn != filename:
        abort(404)
    if not _allowed_download(session["username"], fn):
        abort(404)

    path = _exports_dir() / fn
    if not path.is_file():
        abort(404)

    return send_file(
        path,
        as_attachment=True,
        download_name=fn,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        max_age=0,
    )
