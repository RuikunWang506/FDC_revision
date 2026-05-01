"""Mobile camera barcode scanner page + Excel export with 24h retention + async SKU resolve."""

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from typing import Optional, Tuple

import pandas as pd
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
    "fuzzy": 2,
    "fuzzy_color_code": 3,
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
            WHERE LOWER(SKU) LIKE LOWER(%s) AND LOWER(SKU) LIKE LOWER(%s)
            ORDER BY CHAR_LENGTH(SKU) ASC
            LIMIT {lim}
        """
        cursor.execute(sql, (like_style, like_second))
        _append_fuzzy_rows(cursor.fetchall(), match_type)

    def _fuzzy_three(like_second: str, like_mat: str, match_type: str):
        sql = f"""
            SELECT DISTINCT SKU, Style FROM products
            WHERE LOWER(SKU) LIKE LOWER(%s) AND LOWER(SKU) LIKE LOWER(%s)
              AND LOWER(SKU) LIKE LOWER(%s)
            ORDER BY CHAR_LENGTH(SKU) ASC
            LIMIT {lim}
        """
        cursor.execute(
            sql,
            (like_style, like_second, like_mat),
        )
        _append_fuzzy_rows(cursor.fetchall(), match_type)

    # Fuzzy: style + color abbrev (optionally + material label when 54→WN etc.)
    if color_abbrev:
        if mapped_material:
            _fuzzy_three(
                f"%{color_abbrev}%",
                f"%{mat_label}%",
                "fuzzy_material",
            )
        if not candidates:
            _fuzzy_two(f"%{color_abbrev}%", "fuzzy")
    elif color_id:
        like_color = f"%{color_id}%"
        if mapped_material:
            _fuzzy_three(like_color, f"%{mat_label}%", "fuzzy_color_code_material")
        if not candidates:
            _fuzzy_two(like_color, "fuzzy_color_code")

    # Last resort: style substring only
    if not candidates:
        cursor.execute(
            f"""
            SELECT DISTINCT SKU, Style FROM products
            WHERE LOWER(SKU) LIKE LOWER(%s)
            ORDER BY CHAR_LENGTH(SKU) ASC
            LIMIT {lim}
            """,
            (like_style,),
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


def _records_to_rows(records):
    rows = []
    for r in records:
        if not isinstance(r, dict):
            continue
        p = r.get("parsed") if isinstance(r.get("parsed"), dict) else None
        raw = (r.get("raw") or "").strip()
        chk = ""
        if p is not None:
            chk = "Y" if p.get("checkOk") else "N"
        res = r.get("resolved") if isinstance(r.get("resolved"), dict) else {}
        cand_list = res.get("sku_candidates") or []
        if isinstance(cand_list, list):
            cand_str = " | ".join(
                str(x.get("sku") if isinstance(x, dict) else x) for x in cand_list[:20]
            )
        else:
            cand_str = ""
        rows.append(
            {
                "scan_time": r.get("time_iso") or r.get("time_display") or "",
                "raw_barcode": raw,
                "is_fdc_13_digit_upc": "Y" if p else "N",
                "matched_sku": res.get("matched_sku") or "",
                "sku_candidates": cand_str,
                "location": res.get("locations") or "",
                "resolve_status": res.get("status") or "",
                "resolve_error": res.get("error") or "",
                "beginning_1": p.get("beginning", "") if p else "",
                "factory_2": p.get("factory", "") if p else "",
                "material_code_2": p.get("material", "") if p else "",
                "material_label": p.get("matLabel", "") if p else "",
                "style_4": p.get("style", "") if p else "",
                "color_code_3": p.get("colorId", "") if p else "",
                "color_abbrev": p.get("colorAbbrev", "") if p else "",
                "color_description": p.get("colorDesc", "") if p else "",
                "check_digit_ok": chk,
                "summary_line": res.get("matched_sku") or "",
            }
        )
    return rows


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

    conn = get_mysql_connection()
    if conn is None:
        return jsonify({"success": False, "error": "Database unavailable"}), 503

    candidates = []
    best = None
    locations = ""
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
        if best:
            locations = _locations_for_sku(cursor, best)
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
            "sku_candidates": candidates,
            "locations": locations,
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
