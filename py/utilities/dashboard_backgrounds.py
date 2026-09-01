"""Dashboard wallpaper catalog and per-user background preference."""

from __future__ import annotations

import ipaddress
import os
import re
import socket
from datetime import datetime
from io import BytesIO
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

DEFAULT_DASHBOARD_BG = "rocker-sta"
CUSTOM_BG_ID = "custom"
MAX_SOURCE_BYTES = 12 * 1024 * 1024
MAX_CROPPED_BYTES = 4 * 1024 * 1024
MAX_SOURCE_EDGE = 2560
ALLOWED_UPLOAD_TYPES = {
    "image/jpeg",
    "image/jpg",
    "image/png",
    "image/webp",
    "image/gif",
}

DASHBOARD_BACKGROUNDS: Dict[str, Dict[str, str]] = {
    "rocker-sta": {
        "id": "rocker-sta",
        "file": "rocker-sta-qr8Ci2tzyR8.jpg",
        "label": "City high-rises",
        "photographer": "Rocker Sta",
        "photographer_url": (
            "https://unsplash.com/@starocker?utm_source=unsplash"
            "&utm_medium=referral&utm_content=creditCopyText"
        ),
        "photo_url": (
            "https://unsplash.com/photos/"
            "birds-eye-view-photography-of-concrete-high-rise-buildings-qr8Ci2tzyR8"
            "?utm_source=unsplash&utm_medium=referral&utm_content=creditCopyText"
        ),
        "date": "August 25, 2026",
    },
    "josh-williams": {
        "id": "josh-williams",
        "file": "josh-williams-bg.jpg",
        "label": "Night desert",
        "photographer": "Josh Williams",
        "photographer_url": (
            "https://unsplash.com/@joshwi?utm_source=unsplash"
            "&utm_medium=referral&utm_content=creditCopyText"
        ),
        "photo_url": (
            "https://unsplash.com/photos/"
            "star-trails-arc-over-desert-buttes-at-night-NMEnVWulWLQ"
            "?utm_source=unsplash&utm_medium=referral&utm_content=creditCopyText"
        ),
        "date": "June 3, 2026",
    },
    "luca-bravo": {
        "id": "luca-bravo",
        "file": "luca-bravo-zAjdgNXsMeg-unsplash.jpg",
        "label": "Mountain lake",
        "photographer": "Luca Bravo",
        "photographer_url": (
            "https://unsplash.com/@lucabravo?utm_source=unsplash"
            "&utm_medium=referral&utm_content=creditCopyText"
        ),
        "photo_url": (
            "https://unsplash.com/photos/"
            "scenic-view-of-mountains-during-dawn-zAjdgNXsMeg"
            "?utm_source=unsplash&utm_medium=referral&utm_content=creditCopyText"
        ),
        "date": "May 1, 2026",
    },
}

_schema_ready = False


def _app_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _safe_username(username: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", (username or "").strip())[:64]
    return cleaned or "user"


def custom_background_dir() -> str:
    path = os.path.join(_app_root(), "instance", "user_backgrounds")
    os.makedirs(path, exist_ok=True)
    return path


def custom_background_path(username: str) -> str:
    return os.path.join(custom_background_dir(), f"{_safe_username(username)}.jpg")


def custom_background_exists(username: str) -> bool:
    path = custom_background_path(username)
    try:
        return os.path.isfile(path) and os.path.getsize(path) > 32
    except OSError:
        return False


def _format_date(mtime: float | None = None) -> str:
    stamp = datetime.fromtimestamp(mtime) if mtime else datetime.now()
    return stamp.strftime("%B %d, %Y").replace(" 0", " ")


def describe_custom_background(username: str) -> Dict[str, str]:
    path = custom_background_path(username)
    mtime = 0
    try:
        if os.path.isfile(path):
            mtime = int(os.path.getmtime(path))
    except OSError:
        mtime = 0
    return {
        "id": CUSTOM_BG_ID,
        "file": "",
        "url": f"/user_dashboard_background?v={mtime}",
        "label": "Your photo",
        "photographer": "You",
        "photographer_url": "",
        "photo_url": "",
        "date": _format_date(mtime or None),
    }


def list_dashboard_backgrounds(username: Optional[str] = None) -> List[Dict[str, str]]:
    items = [dict(item) for item in DASHBOARD_BACKGROUNDS.values()]
    if username and custom_background_exists(username):
        items.insert(0, describe_custom_background(username))
    return items


def resolve_dashboard_background(bg_id: Optional[str]) -> Dict[str, str]:
    key = (bg_id or "").strip()
    if key not in DASHBOARD_BACKGROUNDS:
        key = DEFAULT_DASHBOARD_BG
    return dict(DASHBOARD_BACKGROUNDS[key])


def _ensure_schema() -> None:
    global _schema_ready
    if _schema_ready:
        return

    from py.utilities.database_utils import get_mysql_connection

    conn = get_mysql_connection()
    if not conn:
        return
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            SELECT COUNT(*) FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'users'
              AND COLUMN_NAME = 'dashboard_background'
            """
        )
        if int(cursor.fetchone()[0] or 0) == 0:
            cursor.execute(
                "ALTER TABLE users ADD COLUMN dashboard_background VARCHAR(64) NULL DEFAULT NULL"
            )
            conn.commit()
        _schema_ready = True
    except Exception:
        pass
    finally:
        try:
            cursor.close()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass


def get_dashboard_background(username: Optional[str]) -> Dict[str, str]:
    uname = (username or "").strip()
    if not uname:
        return resolve_dashboard_background(DEFAULT_DASHBOARD_BG)

    cache = None
    try:
        from flask import g, has_request_context

        if has_request_context():
            cache = getattr(g, "_fdc_dashboard_bg", None)
            if cache is None:
                cache = {}
                g._fdc_dashboard_bg = cache
            if uname in cache:
                return cache[uname]
    except Exception:
        cache = None

    _ensure_schema()
    from py.utilities.database_utils import get_mysql_connection

    bg_id = DEFAULT_DASHBOARD_BG
    conn = None
    cursor = None
    try:
        conn = get_mysql_connection()
        if conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT dashboard_background FROM users WHERE username = %s",
                (uname,),
            )
            row = cursor.fetchone()
            if row and row[0]:
                bg_id = str(row[0]).strip()
    except Exception:
        bg_id = DEFAULT_DASHBOARD_BG
    finally:
        try:
            if cursor is not None:
                cursor.close()
        except Exception:
            pass
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass

    if bg_id == CUSTOM_BG_ID and custom_background_exists(uname):
        resolved = describe_custom_background(uname)
    else:
        resolved = resolve_dashboard_background(bg_id)
    if cache is not None:
        cache[uname] = resolved
    return resolved


def _cache_background(username: str, resolved: Dict[str, str]) -> None:
    try:
        from flask import g, has_request_context

        if has_request_context():
            cache = getattr(g, "_fdc_dashboard_bg", None)
            if cache is None:
                cache = {}
                g._fdc_dashboard_bg = cache
            cache[username] = resolved
    except Exception:
        pass


def save_dashboard_background(username: str, bg_id: str) -> Dict[str, Any]:
    uname = (username or "").strip()
    key = (bg_id or "").strip()
    if key == CUSTOM_BG_ID and custom_background_exists(uname):
        resolved = describe_custom_background(uname)
    else:
        resolved = resolve_dashboard_background(key)
    if not uname:
        return {"success": False, "error": "Not signed in", "background": resolved}

    _ensure_schema()
    from py.utilities.database_utils import get_mysql_connection

    conn = None
    cursor = None
    try:
        conn = get_mysql_connection()
        if not conn:
            return {"success": False, "error": "Database unavailable", "background": resolved}
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE users SET dashboard_background = %s WHERE username = %s",
            (resolved["id"], uname),
        )
        conn.commit()
        _cache_background(uname, resolved)
        return {"success": True, "background": resolved}
    except Exception as exc:
        return {"success": False, "error": str(exc), "background": resolved}
    finally:
        try:
            if cursor is not None:
                cursor.close()
        except Exception:
            pass
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass


def _jpeg_from_image(image) -> bytes:
    from PIL import Image

    rgb = image.convert("RGB")
    width, height = rgb.size
    longest = max(width, height)
    if longest > MAX_SOURCE_EDGE:
        scale = MAX_SOURCE_EDGE / float(longest)
        rgb = rgb.resize(
            (max(1, int(width * scale)), max(1, int(height * scale))),
            Image.LANCZOS,
        )
    buf = BytesIO()
    rgb.save(buf, format="JPEG", quality=86, optimize=True)
    return buf.getvalue()


def image_bytes_to_jpeg(raw: bytes) -> bytes:
    from PIL import Image, ImageOps

    if not raw or len(raw) > MAX_SOURCE_BYTES:
        raise ValueError("Image is missing or larger than 12 MB.")
    image = Image.open(BytesIO(raw))
    image = ImageOps.exif_transpose(image)
    image.load()
    return _jpeg_from_image(image)


def jpeg_data_url(jpeg_bytes: bytes) -> str:
    import base64

    return "data:image/jpeg;base64," + base64.b64encode(jpeg_bytes).decode("ascii")


def _host_is_public(host: str, port: int) -> bool:
    if not host or host.lower() in {"localhost", "metadata.google.internal"}:
        return False
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            return False
    return True


def fetch_public_image_jpeg(url: str) -> bytes:
    import requests

    text = (url or "").strip()
    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https") or parsed.username or parsed.password:
        raise ValueError("Enter an http or https image URL.")
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if not _host_is_public(host, port):
        raise ValueError("That URL is not allowed.")

    headers = {"User-Agent": "FDC-Flask-App/1.0 (dashboard background)", "Accept": "image/*"}
    with requests.get(text, headers=headers, timeout=10, stream=True, allow_redirects=True) as resp:
        if resp.status_code >= 400:
            raise ValueError(f"Could not download image ({resp.status_code}).")
        final = urlparse(resp.url)
        final_port = final.port or (443 if final.scheme == "https" else 80)
        if final.scheme not in ("http", "https") or not _host_is_public(final.hostname or "", final_port):
            raise ValueError("That URL is not allowed.")
        declared = int(resp.headers.get("Content-Length") or 0)
        if declared > MAX_SOURCE_BYTES:
            raise ValueError("Image is larger than 12 MB.")
        chunks: list[bytes] = []
        total = 0
        for chunk in resp.iter_content(64 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > MAX_SOURCE_BYTES:
                raise ValueError("Image is larger than 12 MB.")
            chunks.append(chunk)
    return image_bytes_to_jpeg(b"".join(chunks))


def save_custom_background(username: str, raw: bytes) -> Dict[str, Any]:
    uname = (username or "").strip()
    if not uname:
        return {"success": False, "error": "Not signed in"}
    if not raw or len(raw) > MAX_CROPPED_BYTES:
        return {"success": False, "error": "Cropped image is missing or too large."}
    try:
        jpeg = image_bytes_to_jpeg(raw)
    except Exception as exc:
        return {"success": False, "error": str(exc) or "Could not read that image."}

    path = custom_background_path(uname)
    tmp_path = path + ".tmp"
    try:
        with open(tmp_path, "wb") as handle:
            handle.write(jpeg)
        os.replace(tmp_path, path)
    except OSError as exc:
        try:
            if os.path.isfile(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        return {"success": False, "error": f"Could not save image: {exc}"}

    result = save_dashboard_background(uname, CUSTOM_BG_ID)
    result["background"] = describe_custom_background(uname)
    return result
