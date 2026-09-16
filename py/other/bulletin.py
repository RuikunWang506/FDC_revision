"""Staff bulletin / file transfer station.

Public posts are visible to every signed-in user. Private posts are visible
to the author and optional recipients. Uploads live in transferstationFDC/.
"""
from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import shutil
import threading
import uuid
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from typing import Any, Dict, List, Optional

import bleach
from flask import (
    Blueprint,
    abort,
    jsonify,
    render_template,
    request,
    send_file,
    session,
)
from werkzeug.utils import secure_filename

from py.utilities.setting import login_required

bulletin_bp = Blueprint("bulletin", __name__)

_LOCK = threading.RLock()

MAX_FILE_BYTES = 80 * 1024 * 1024
MAX_FILES_PER_POST = 20
MAX_HTML_CHARS = 200_000
PREVIEW_COUNT = 2

BLOCKED_EXTENSIONS = {
    ".exe",
    ".bat",
    ".cmd",
    ".com",
    ".scr",
    ".pif",
    ".msi",
    ".js",
    ".jse",
    ".vbs",
    ".vbe",
    ".wsf",
    ".wsh",
    ".ps1",
    ".psm1",
    ".reg",
    ".jar",
    ".html",
    ".htm",
    ".shtml",
    ".xhtml",
    ".svg",
    ".php",
    ".asp",
    ".aspx",
    ".cgi",
}

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
DATA_IMAGE_EXT = {
    "png": ".png",
    "jpeg": ".jpg",
    "jpg": ".jpg",
    "gif": ".gif",
    "webp": ".webp",
}

ALLOWED_TAGS = [
    "p",
    "br",
    "div",
    "span",
    "b",
    "strong",
    "i",
    "em",
    "u",
    "s",
    "strike",
    "ul",
    "ol",
    "li",
    "h1",
    "h2",
    "h3",
    "h4",
    "blockquote",
    "a",
    "img",
    "font",
    "pre",
    "code",
    "hr",
    "table",
    "thead",
    "tbody",
    "tr",
    "th",
    "td",
]
ALLOWED_ATTRS = {
    "*": ["class"],
    "a": ["href", "target", "rel"],
    "img": ["src", "alt", "width", "height"],
    "font": ["face", "size", "color"],
    "td": ["colspan", "rowspan"],
    "th": ["colspan", "rowspan"],
}
ALLOWED_CSS = {
    "color",
    "background-color",
    "font-size",
    "font-family",
    "font-weight",
    "font-style",
    "text-decoration",
    "text-align",
    "line-height",
}
_DATA_IMG_RE = re.compile(
    r"(<img\b[^>]*?\bsrc=[\"'])data:image/(png|jpeg|jpg|gif|webp);base64,([A-Za-z0-9+/=\s]+)([\"'][^>]*>)",
    re.I,
)
_PLACEHOLDER_RE = re.compile(r"__FDCFILE[_:](\d+)__", re.I)
_HEX_ID_RE = re.compile(r"^[a-f0-9]{32}$")
_TAG_RE = re.compile(r"<[^>]+>")
_STYLE_ATTR_RE = re.compile(r"\sstyle=([\"'])(.*?)\1", re.I | re.S)


def station_root(root: Optional[Path] = None) -> Path:
    if root is not None:
        return Path(root)
    override = (os.environ.get("FDC_TRANSFERSTATION_DIR") or "").strip()
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[2] / "transferstationFDC"


def ensure_station(root: Optional[Path] = None) -> Path:
    base = station_root(root)
    (base / "files").mkdir(parents=True, exist_ok=True)
    return base


def _posts_path(root: Path) -> Path:
    return root / "posts.json"


def _read_store(root: Path) -> List[Dict[str, Any]]:
    path = _posts_path(root)
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(raw, dict):
        posts = raw.get("posts")
    else:
        posts = raw
    if not isinstance(posts, list):
        return []
    return [p for p in posts if isinstance(p, dict) and p.get("id")]


def _write_store(root: Path, posts: List[Dict[str, Any]]) -> None:
    path = _posts_path(root)
    payload = json.dumps({"posts": posts}, ensure_ascii=False, indent=2)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, path)


def _plain_preview(html: str, limit: int = 180) -> str:
    text = unescape(_TAG_RE.sub(" ", html or ""))
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _sanitize_style(value: str) -> str:
    kept = []
    for chunk in (value or "").split(";"):
        if ":" not in chunk:
            continue
        prop, val = chunk.split(":", 1)
        prop = prop.strip().lower()
        val = val.strip()
        if prop not in ALLOWED_CSS or not val:
            continue
        lowered = val.lower()
        if any(bad in lowered for bad in ("expression", "url(", "javascript:", "@import", "behavior")):
            continue
        val = val.replace('"', "").replace("'", "")
        kept.append(f"{prop}: {val}")
    return "; ".join(kept)


def sanitize_html(html: str) -> str:
    stashed: List[str] = []

    def stash(match: re.Match[str]) -> str:
        cleaned = _sanitize_style(match.group(2))
        if not cleaned:
            return ""
        stashed.append(cleaned)
        return f' data-fdc-style="{len(stashed) - 1}"'

    prepared = _STYLE_ATTR_RE.sub(stash, html or "")
    attrs = {key: list(val) for key, val in ALLOWED_ATTRS.items()}
    attrs.setdefault("*", []).append("data-fdc-style")
    cleaned = bleach.clean(
        prepared,
        tags=ALLOWED_TAGS,
        attributes=attrs,
        protocols=["http", "https", "mailto"],
        strip=True,
    )

    def restore(match: re.Match[str]) -> str:
        try:
            idx = int(match.group(1))
        except ValueError:
            return ""
        if idx < 0 or idx >= len(stashed):
            return ""
        return f' style="{stashed[idx]}"'

    cleaned = re.sub(r'\sdata-fdc-style="(\d+)"', restore, cleaned)
    if len(cleaned) > MAX_HTML_CHARS:
        cleaned = cleaned[:MAX_HTML_CHARS]
    return _scrub_img_sources(cleaned)


def _scrub_img_sources(html: str) -> str:
    def repl(match: re.Match[str]) -> str:
        tag = match.group(0)
        src_match = re.search(r"\bsrc=[\"']([^\"']+)[\"']", tag, re.I)
        if not src_match:
            return ""
        src = src_match.group(1)
        if src.startswith("/bulletin/files/") and ".." not in src:
            return tag
        return ""

    return re.sub(r"<img\b[^>]*>", repl, html, flags=re.I)


def _parse_recipients(raw: str, author: str) -> List[str]:
    author_key = (author or "").strip().lower()
    seen = {author_key} if author_key else set()
    out: List[str] = []
    for part in re.split(r"[,;\n]+", raw or ""):
        name = part.strip()
        if not name or len(name) > 64:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(name)
        if len(out) >= 30:
            break
    return out


def _norm(name: str) -> str:
    return (name or "").strip().lower()


def user_can_see(post: Dict[str, Any], username: str) -> bool:
    if (post.get("visibility") or "public") != "private":
        return True
    user = _norm(username)
    if not user:
        return False
    if _norm(str(post.get("author") or "")) == user:
        return True
    for rec in post.get("recipients") or []:
        if _norm(str(rec)) == user:
            return True
    return False


def user_can_delete(post: Dict[str, Any], username: str) -> bool:
    user = _norm(username)
    if not user:
        return False
    if user == "yeti":
        return True
    return _norm(str(post.get("author") or "")) == user


def list_visible_posts(
    username: str,
    view: str = "public",
    limit: Optional[int] = None,
    root: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    view = "private" if str(view).strip().lower() == "private" else "public"
    base = ensure_station(root)
    with _LOCK:
        posts = _read_store(base)
    visible = []
    for post in posts:
        vis = post.get("visibility") or "public"
        if vis != view:
            continue
        if not user_can_see(post, username):
            continue
        visible.append(post)
    visible.sort(key=lambda p: str(p.get("created_at") or ""), reverse=True)
    if limit is not None:
        return visible[: max(0, int(limit))]
    return visible


def preview_posts(username: str, root: Optional[Path] = None) -> List[Dict[str, Any]]:
    return list_visible_posts(username, view="public", limit=PREVIEW_COUNT, root=root)


def _blocked_name(filename: str) -> bool:
    ext = Path(filename or "").suffix.lower()
    return ext in BLOCKED_EXTENSIONS


def _is_image_name(filename: str) -> bool:
    return Path(filename or "").suffix.lower() in IMAGE_EXTENSIONS


def _save_bytes(post_dir: Path, original_name: str, data: bytes) -> Optional[Dict[str, Any]]:
    if not data or len(data) > MAX_FILE_BYTES:
        return None
    safe_original = (original_name or "file").replace("\\", "/").split("/")[-1].strip() or "file"
    if _blocked_name(safe_original):
        return None
    ext = Path(safe_original).suffix.lower()
    stored = f"{uuid.uuid4().hex}{ext}"
    dest = post_dir / stored
    dest.write_bytes(data)
    mime = mimetypes.guess_type(safe_original)[0] or "application/octet-stream"
    return {
        "stored_name": stored,
        "original_name": safe_original[:180],
        "mime": mime,
        "size": len(data),
        "is_image": _is_image_name(safe_original),
    }


def _extract_data_images(html: str, post_dir: Path, attachments: List[Dict[str, Any]]) -> str:
    def repl(match: re.Match[str]) -> str:
        if len(attachments) >= MAX_FILES_PER_POST:
            return match.group(1) + match.group(4)
        kind = match.group(2).lower()
        try:
            data = base64.b64decode(re.sub(r"\s+", "", match.group(3)), validate=False)
        except Exception:
            return match.group(1) + match.group(4)
        ext = DATA_IMAGE_EXT.get(kind, ".png")
        saved = _save_bytes(post_dir, f"pasted{ext}", data)
        if not saved:
            return match.group(1) + match.group(4)
        attachments.append(saved)
        url = f"/bulletin/files/{post_dir.name}/{saved['stored_name']}"
        return f"{match.group(1)}{url}{match.group(4)}"

    return _DATA_IMG_RE.sub(repl, html or "")


def create_post(
    author: str,
    html: str,
    visibility: str,
    recipients_raw: str,
    files: List[Any],
    root: Optional[Path] = None,
) -> Dict[str, Any]:
    author = (author or "").strip()
    if not author:
        raise ValueError("Sign in to post.")
    vis = "private" if str(visibility).strip().lower() == "private" else "public"
    recipients = _parse_recipients(recipients_raw, author) if vis == "private" else []
    post_id = uuid.uuid4().hex
    base = ensure_station(root)
    post_dir = base / "files" / post_id
    post_dir.mkdir(parents=True, exist_ok=True)
    attachments: List[Dict[str, Any]] = []

    html = _extract_data_images(html or "", post_dir, attachments)

    pending_slots: Dict[int, Optional[Dict[str, Any]]] = {}
    for idx, upload in enumerate(files or []):
        if len(attachments) >= MAX_FILES_PER_POST:
            break
        filename = getattr(upload, "filename", "") or "file"
        try:
            data = upload.read()
        except Exception:
            continue
        saved = _save_bytes(post_dir, filename, data)
        if not saved:
            continue
        pending_slots[idx] = saved
        attachments.append(saved)

    def replace_placeholder(match: re.Match[str]) -> str:
        idx = int(match.group(1))
        saved = pending_slots.get(idx)
        if not saved:
            return ""
        return f"/bulletin/files/{post_id}/{saved['stored_name']}"

    html = _PLACEHOLDER_RE.sub(replace_placeholder, html)
    html = sanitize_html(html)
    preview = _plain_preview(html)
    if not preview and not attachments:
        shutil.rmtree(post_dir, ignore_errors=True)
        raise ValueError("Write a note or attach a file.")

    post = {
        "id": post_id,
        "author": author,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "visibility": vis,
        "recipients": recipients,
        "html": html,
        "text_preview": preview,
        "attachments": attachments,
    }
    with _LOCK:
        posts = _read_store(base)
        posts.append(post)
        _write_store(base, posts)
    return post


def delete_post(post_id: str, username: str, root: Optional[Path] = None) -> bool:
    post_id = (post_id or "").strip().lower()
    if not _HEX_ID_RE.match(post_id):
        return False
    base = ensure_station(root)
    with _LOCK:
        posts = _read_store(base)
        found = None
        kept: List[Dict[str, Any]] = []
        for post in posts:
            if post.get("id") == post_id:
                found = post
            else:
                kept.append(post)
        if not found or not user_can_delete(found, username):
            return False
        _write_store(base, kept)
    post_dir = base / "files" / post_id
    if post_dir.is_dir():
        for child in post_dir.iterdir():
            try:
                child.unlink()
            except OSError:
                pass
        try:
            post_dir.rmdir()
        except OSError:
            pass
    return True


def resolve_attachment(
    post_id: str, stored_name: str, username: str, root: Optional[Path] = None
) -> Optional[Dict[str, Any]]:
    post_id = (post_id or "").strip().lower()
    stored_name = secure_filename(stored_name or "")
    if not _HEX_ID_RE.match(post_id) or not stored_name or ".." in stored_name:
        return None
    base = ensure_station(root)
    with _LOCK:
        posts = _read_store(base)
    post = next((p for p in posts if p.get("id") == post_id), None)
    if not post or not user_can_see(post, username):
        return None
    meta = None
    for att in post.get("attachments") or []:
        if att.get("stored_name") == stored_name:
            meta = att
            break
    if not meta:
        return None
    path = (base / "files" / post_id / stored_name).resolve()
    allowed = (base / "files" / post_id).resolve()
    if allowed not in path.parents and path.parent != allowed:
        return None
    if not path.is_file():
        return None
    return {"path": path, "meta": meta, "post": post}


def serialize_post(post: Dict[str, Any], username: str) -> Dict[str, Any]:
    post_id = post.get("id") or ""
    atts = []
    for att in post.get("attachments") or []:
        stored = att.get("stored_name") or ""
        if not stored:
            continue
        atts.append(
            {
                "name": att.get("original_name") or stored,
                "url": f"/bulletin/files/{post_id}/{stored}",
                "size": att.get("size") or 0,
                "is_image": bool(att.get("is_image")),
                "mime": att.get("mime") or "",
            }
        )
    return {
        "id": post_id,
        "author": post.get("author") or "",
        "created_at": post.get("created_at") or "",
        "visibility": post.get("visibility") or "public",
        "recipients": list(post.get("recipients") or []),
        "html": post.get("html") or "",
        "text_preview": post.get("text_preview") or _plain_preview(post.get("html") or ""),
        "attachments": atts,
        "can_delete": user_can_delete(post, username),
    }


def _current_user() -> str:
    return (session.get("username") or "").strip()


@bulletin_bp.route("/bulletin")
@login_required
def bulletin_page():
    view = "private" if (request.args.get("view") or "").strip().lower() == "private" else "public"
    user = _current_user()
    posts = [serialize_post(p, user) for p in list_visible_posts(user, view=view)]
    return render_template(
        "bulletin.html",
        current_user=user,
        bulletin_view=view,
        bulletin_posts=posts,
    )


@bulletin_bp.route("/bulletin/posts")
@login_required
def bulletin_list():
    view = "private" if (request.args.get("view") or "").strip().lower() == "private" else "public"
    user = _current_user()
    posts = [serialize_post(p, user) for p in list_visible_posts(user, view=view)]
    return jsonify({"ok": True, "view": view, "posts": posts})


@bulletin_bp.route("/bulletin/posts", methods=["POST"])
@login_required
def bulletin_create():
    user = _current_user()
    html = request.form.get("html") or ""
    visibility = request.form.get("visibility") or "public"
    recipients = request.form.get("recipients") or ""
    files = request.files.getlist("files")
    try:
        post = create_post(user, html, visibility, recipients, files)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception:
        return jsonify({"ok": False, "error": "Could not save this post."}), 500
    return jsonify({"ok": True, "post": serialize_post(post, user)})


@bulletin_bp.route("/bulletin/posts/<post_id>", methods=["DELETE"])
@login_required
def bulletin_delete(post_id: str):
    user = _current_user()
    if not delete_post(post_id, user):
        return jsonify({"ok": False, "error": "Could not delete that post."}), 403
    return jsonify({"ok": True})


@bulletin_bp.route("/bulletin/files/<post_id>/<stored_name>")
@login_required
def bulletin_file(post_id: str, stored_name: str):
    user = _current_user()
    resolved = resolve_attachment(post_id, stored_name, user)
    if not resolved:
        abort(404)
    meta = resolved["meta"]
    path = resolved["path"]
    download_name = meta.get("original_name") or path.name
    as_attachment = not bool(meta.get("is_image"))
    mimetype = meta.get("mime") or None
    return send_file(
        path,
        mimetype=mimetype,
        as_attachment=as_attachment,
        download_name=download_name,
        max_age=0,
        conditional=True,
    )
