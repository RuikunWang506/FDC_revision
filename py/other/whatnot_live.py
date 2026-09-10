"""Whatnot Live: control panel, one OBS overlay, phone camera publish, Chrome-extension ingest."""
from __future__ import annotations

import io
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
import zipfile
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Optional

from flask import (
    Blueprint,
    Response,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from py.utilities.database_utils import (
    get_sql_server_connection_fdc,
    get_sql_server_connection_oms_ef,
)
from py.utilities import lan_detection
from py.utilities.logging_setup import log_user_action
from py.utilities.setting import login_required

whatnot_live_bp = Blueprint("whatnot_live", __name__)

ALLOWED_USERS = frozenset({"yeti", "elaine"})
MSRP_MULTIPLIER = 3
DEFAULT_STARTING_BID = 25
DEFAULT_INCREMENT = 5
CONDITION_ALWAYS = "NEW"
BRAND_TITLE = "CALYX FASHION"
TICKER_DEFAULT = "100% AUTHENTIC  •  FAST SHIPPING  •  THANK YOU FOR SUPPORTING CALYX!"
LIVE_ID_RE = re.compile(
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    re.I,
)
EXTENSION_DIR = Path(__file__).resolve().parents[2] / "chrome_extension" / "fdc-whatnot-live"
LIVE_PERSIST_KEYS = ("live_url", "live_id", "dashboard_url", "live_url_input")
LIVE_PERSIST_NAME = "whatnot_live_last.json"

PUBLIC_VIEW_ENDPOINTS = frozenset(
    {
        "whatnot_live.overlay",
        "whatnot_live.overlay_alias",
        "whatnot_live.camera",
        "whatnot_live.stream",
        "whatnot_live.ingest",
        "whatnot_live.public_state",
        "whatnot_live.webrtc_signal",
    }
)

_lock = threading.Condition()
_seq = 0
_state: Dict[str, Any] = {}
_webrtc: Dict[str, Any] = {}


def _empty_state() -> Dict[str, Any]:
    return {
        "sku": "",
        "style": "",
        "color": "",
        "condition": CONDITION_ALWAYS,
        "msrp": 0.0,
        "msrp_overlay": 0.0,
        "starting_bid": DEFAULT_STARTING_BID,
        "bid_increment": DEFAULT_INCREMENT,
        "available": 1,
        "bid": 0.0,
        "bidder": "",
        "time_left": None,
        "viewers": 0,
        "live": False,
        "show_live": False,
        "ticker": TICKER_DEFAULT,
        "brand": BRAND_TITLE,
        "popup": None,
        "source": "",
        "live_url": "",
        "live_id": "",
        "dashboard_url": "",
        "live_url_input": "",
        "listing_title": "",
        "listing_category": "",
        "show_title": "",
        "gross_sales": 0.0,
        "estimated_orders": 0,
        "dimensions": "",
        "weight": "",
        "updated_at": time.time(),
        "seq": 0,
    }


def _empty_slot() -> Dict[str, Any]:
    return {
        "offer": None,
        "answer": None,
        "pub_ice": [],
        "view_ice": [],
        "need_offer": False,
        "viewer_seq": 0,
    }


def _empty_webrtc() -> Dict[str, Any]:
    return {
        "slots": {"main": _empty_slot(), "preview": _empty_slot()},
        "publisher": False,
        "audio_level": 0.0,
        "has_mic": False,
        "audio_at": 0.0,
        "local_live": False,
        "local_at": 0.0,
        "updated_at": 0.0,
    }


def _slot_name(raw: Any) -> str:
    name = str(raw or "main").strip().lower()
    return "preview" if name in {"preview", "control"} else "main"


def _slot(name: str) -> Dict[str, Any]:
    slots = _webrtc.setdefault("slots", {})
    if name not in slots or not isinstance(slots.get(name), dict):
        slots[name] = _empty_slot()
    else:
        for key, value in _empty_slot().items():
            slots[name].setdefault(key, value)
    return slots[name]


def _webrtc_public() -> Dict[str, Any]:
    main = _slot("main")
    preview = _slot("preview")
    return {
        "ok": True,
        "offer": main.get("offer"),
        "answer": main.get("answer"),
        "pub_ice": list(main.get("pub_ice") or []),
        "view_ice": list(main.get("view_ice") or []),
        "need_offer": bool(main.get("need_offer")),
        "viewer_seq": int(main.get("viewer_seq") or 0),
        "preview_offer": preview.get("offer"),
        "preview_answer": preview.get("answer"),
        "preview_pub_ice": list(preview.get("pub_ice") or []),
        "preview_view_ice": list(preview.get("view_ice") or []),
        "preview_need_offer": bool(preview.get("need_offer")),
        "preview_viewer_seq": int(preview.get("viewer_seq") or 0),
        "publisher": bool(main.get("offer") or preview.get("offer") or _webrtc.get("publisher")),
        "audio_level": float(_webrtc.get("audio_level") or 0),
        "has_mic": bool(_webrtc.get("has_mic")),
        "audio_at": _webrtc.get("audio_at") or 0,
        "local_live": bool(_webrtc.get("local_live")),
        "local_at": _webrtc.get("local_at") or 0,
        "updated_at": _webrtc.get("updated_at") or 0,
    }


def _ensure() -> None:
    global _state, _webrtc
    if not _state:
        _state = _empty_state()
    else:
        for key, value in _empty_state().items():
            _state.setdefault(key, value)
    if not _webrtc:
        _webrtc = _empty_webrtc()
    else:
        if "slots" not in _webrtc:
            old = dict(_webrtc)
            _webrtc.update(_empty_webrtc())
            main = _slot("main")
            main["offer"] = old.get("offer")
            main["answer"] = old.get("answer")
            main["pub_ice"] = list(old.get("pub_ice") or [])
            main["view_ice"] = list(old.get("view_ice") or [])
        for key, value in _empty_webrtc().items():
            if key == "slots":
                continue
            _webrtc.setdefault(key, value)
        _slot("main")
        _slot("preview")
    if not str(_state.get("live_id") or "").strip():
        saved = _read_persisted_live()
        if saved.get("live_id"):
            for key in LIVE_PERSIST_KEYS:
                _state[key] = saved.get(key) or ""


def _snapshot() -> Dict[str, Any]:
    _ensure()
    with _lock:
        data = deepcopy(_state)
        data["seq"] = _seq
        now = time.time()
        local_ok = bool(_webrtc.get("local_live")) and (
            now - float(_webrtc.get("local_at") or 0)
        ) < 8
        rtc_ok = bool(_webrtc.get("publisher") and _webrtc.get("offer"))
        data["camera_live"] = local_ok or rtc_ok
        viewers = int(data.get("viewers") or 0)
        bid = float(data.get("bid") or 0)
        data["stream_live"] = bool(data.get("show_live") or viewers > 0 or bid > 0)
    return data


def _publish(patch: Optional[Dict[str, Any]] = None, popup: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    global _seq
    _ensure()
    with _lock:
        if patch:
            for key, value in patch.items():
                if key == "condition":
                    _state["condition"] = CONDITION_ALWAYS
                    continue
                if key in _state:
                    _state[key] = value
        _state["condition"] = CONDITION_ALWAYS
        _state["brand"] = BRAND_TITLE
        if popup:
            popup = dict(popup)
            popup.setdefault("ts", time.time())
            _state["popup"] = popup
        _state["updated_at"] = time.time()
        _seq += 1
        _state["seq"] = _seq
        data = deepcopy(_state)
        _lock.notify_all()
    return data


def _username() -> str:
    return (session.get("username") or "").strip().lower()


def _allowed(username: Optional[str] = None) -> bool:
    return (username or _username()).strip().lower() in ALLOWED_USERS


def _deny_html():
    flash("Whatnot Live is limited to yeti and elaine for now.", "warning")
    return redirect(url_for("home"))


def _deny_json():
    return jsonify({"ok": False, "error": "Forbidden"}), 403


def _ingest_token_ok() -> bool:
    expected = (os.getenv("WHATNOT_LIVE_INGEST_TOKEN") or "").strip()
    if not expected:
        return False
    got = (
        (request.headers.get("X-Whatnot-Token") or "").strip()
        or (request.args.get("token") or "").strip()
    )
    if request.is_json:
        body = request.get_json(silent=True) or {}
        got = got or str(body.get("token") or "").strip()
    return got == expected


def _cors(resp: Response) -> Response:
    origin = request.headers.get("Origin") or "*"
    resp.headers["Access-Control-Allow-Origin"] = origin
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Whatnot-Token"
    resp.headers["Access-Control-Max-Age"] = "600"
    return resp


def _money(value: Any) -> float:
    try:
        return round(float(value or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def _intish(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _clean_sku(value: Any) -> str:
    return " ".join(str(value or "").replace("\u00a0", " ").split()).upper()


def _parse_live_link(raw: Any) -> Dict[str, str]:
    """Accept a Whatnot share/dashboard URL and keep the live UUID + canonical page."""
    text = str(raw or "").strip()
    if not text:
        return {"live_url": "", "live_id": ""}
    match = LIVE_ID_RE.search(text)
    live_id = (match.group(1) or "").lower() if match else ""
    if not live_id:
        return {"live_url": text if text.startswith("http") else "", "live_id": "", "dashboard_url": ""}
    return {
        "live_id": live_id,
        "live_url": f"https://www.whatnot.com/live/{live_id}",
        "dashboard_url": f"https://www.whatnot.com/dashboard/live/{live_id}",
    }


def _persist_path() -> Optional[Path]:
    try:
        root = Path(current_app.instance_path)
    except RuntimeError:
        return None
    root.mkdir(parents=True, exist_ok=True)
    return root / LIVE_PERSIST_NAME


def _read_persisted_live() -> Dict[str, str]:
    path = _persist_path()
    if path is None or not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict) or not str(data.get("live_id") or "").strip():
        return {}
    return {key: str(data.get(key) or "").strip() for key in LIVE_PERSIST_KEYS}


def _write_persisted_live(state: Optional[Dict[str, Any]] = None) -> None:
    path = _persist_path()
    if path is None:
        return
    snap = state or _snapshot()
    payload = {key: str(snap.get(key) or "").strip() for key in LIVE_PERSIST_KEYS}
    try:
        if not payload.get("live_id"):
            if path.exists():
                path.unlink()
            return
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except Exception as exc:
        try:
            current_app.logger.warning("whatnot_live persist failed: %s", exc)
        except Exception:
            pass


def _clear_live_fields() -> Dict[str, str]:
    return {key: "" for key in LIVE_PERSIST_KEYS}


def _whatnot_live_http_status(url: str) -> Optional[int]:
    text = str(url or "").strip()
    if not text.startswith("http"):
        return None
    req = urllib.request.Request(
        text,
        method="GET",
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=6) as resp:
            return int(getattr(resp, "status", 200) or 200)
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except Exception:
        return None


def _drop_dead_live() -> bool:
    """Clear the saved show if Whatnot returns 404. Keep it on any other status/network error."""
    snap = _snapshot()
    url = snap.get("dashboard_url") or snap.get("live_url") or snap.get("live_url_input") or ""
    if not url:
        return False
    status = _whatnot_live_http_status(str(url))
    if status != 404:
        return False
    _publish(_clear_live_fields())
    _write_persisted_live()
    return True


def _fmt_dim(value: float) -> str:
    if value >= 10:
        return f"{value:.0f}"
    text = f"{value:.1f}".rstrip("0").rstrip(".")
    return text or "0"


def _lookup_sku(sku: str) -> Optional[Dict[str, Any]]:
    sku = _clean_sku(sku)
    if not sku:
        return None
    like = sku if sku.endswith("%") else f"{sku}%"
    query = """
        SELECT TOP 1
            LTRIM(RTRIM(i.PROD_CD)) AS sku,
            LTRIM(RTRIM(i.ALT_CD)) AS style,
            LTRIM(RTRIM(ISNULL(i.UNIT_COLOR, ''))) AS color,
            CAST(ISNULL(i.RETAIL_PRS, 0) AS FLOAT) AS msrp,
            CAST(ISNULL(idt.ut_len, 0) AS FLOAT) AS len,
            CAST(ISNULL(idt.ut_wi, 0) AS FLOAT) AS wid,
            CAST(ISNULL(idt.UT_hi, 0) AS FLOAT) AS hei,
            CAST(ISNULL(i.UT_WT, 0) AS FLOAT) AS weight
        FROM dbo.inv AS i
        LEFT JOIN dbo.inv_detl AS idt ON i.PROD_CD = idt.prod_cd
        WHERE i.PROD_CD = ? OR i.PROD_CD LIKE ? OR i.ALT_CD = ?
        ORDER BY CASE WHEN i.PROD_CD = ? THEN 0 WHEN i.ALT_CD = ? THEN 1 ELSE 2 END, i.PROD_CD
    """
    params = (sku, like, sku, sku, sku)
    for connector in (get_sql_server_connection_fdc, get_sql_server_connection_oms_ef):
        conn = None
        try:
            conn = connector()
            cursor = conn.cursor()
            cursor.execute(query, params)
            row = cursor.fetchone()
            cursor.close()
            if not row:
                continue
            msrp = _money(row[3])
            len_v, wid_v, hei_v = float(row[4] or 0), float(row[5] or 0), float(row[6] or 0)
            dims = ""
            if len_v or wid_v or hei_v:
                dims = f"{_fmt_dim(len_v)}×{_fmt_dim(wid_v)}×{_fmt_dim(hei_v)}"
            wt = float(row[7] or 0)
            return {
                "sku": str(row[0] or sku).strip(),
                "style": str(row[1] or "").strip(),
                "color": str(row[2] or "").strip(),
                "msrp": msrp,
                "msrp_overlay": round(msrp * MSRP_MULTIPLIER, 2),
                "dimensions": dims,
                "weight": f"{wt:g} lb" if wt else "",
                "condition": CONDITION_ALWAYS,
            }
        except Exception as exc:
            current_app.logger.warning("whatnot_live sku lookup failed: %s", exc)
        finally:
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass
    return None


def _popup_from_event(kind: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    kind = (kind or "").strip().lower().replace("-", "_")
    if kind in {"sold", "sale"}:
        return {"type": "sold", "title": "SOLD!", "subtitle": "THANK YOU!"}
    if kind in {"high_bid", "highbid", "bid"}:
        amount = _money(payload.get("bid") or payload.get("amount") or payload.get("price"))
        return {
            "type": "high_bid",
            "title": "HIGH BID!",
            "subtitle": f"${amount:,.0f}" if amount else "NEW BID",
            "amount": amount,
        }
    if kind in {"next", "next_item", "nextitem"}:
        return {"type": "next_item", "title": "NEXT ITEM", "subtitle": ">> STAY TUNED!"}
    if kind in {"special", "special_price", "deal"}:
        amount = _money(payload.get("price") or payload.get("amount") or payload.get("bid"))
        subtitle = f"${amount:,.0f} TODAY ONLY!" if amount else "TODAY ONLY!"
        return {"type": "special", "title": "SPECIAL PRICE!", "subtitle": subtitle, "amount": amount}
    return None


def _jsonish() -> bool:
    if request.is_json:
        return True
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return True
    try:
        if request.accept_mimetypes.best_match(["application/json", "text/html"]) == "application/json":
            return True
    except Exception:
        pass
    return False


def _unauthorized_json():
    return jsonify({"ok": False, "login": True, "error": "Unauthorized"}), 401


def _public_live_url(path: str) -> str:
    origin = str(current_app.config.get("PUBLIC_APP_ORIGIN") or "https://www.fdcef.com").rstrip("/")
    return origin + "/" + path.lstrip("/")


def _connection_live_url(path: str) -> str:
    if lan_detection.host_is_private_lan(request.host) or lan_detection.client_ip_is_private_lan(request):
        return request.url_root.rstrip("/") + "/" + path.lstrip("/")
    return _public_live_url(path)


@whatnot_live_bp.before_request
def _gate():
    if request.method == "OPTIONS":
        return _cors(Response(status=204))
    ep = request.endpoint or ""
    if ep == "whatnot_live.camera":
        return None
    if ep in PUBLIC_VIEW_ENDPOINTS:
        return None
    if ep == "whatnot_live.static":
        return None
    if "username" not in session:
        if _jsonish():
            return _unauthorized_json()
        session["next"] = request.url
        return redirect(url_for("auth.login", next=request.path))
    if not _allowed():
        if _jsonish():
            return jsonify({"ok": False, "login": True, "error": "Forbidden"}), 403
        return _deny_html()
    return None


@whatnot_live_bp.route("/whatnot_live")
@whatnot_live_bp.route("/whatnotlive")
@login_required
def hub():
    log_user_action("opened Whatnot Live hub", _username())
    snap = _snapshot()
    return render_template(
        "whatnot_live.html",
        current_user=session.get("username"),
        state=snap,
        camera_url=_connection_live_url("/whatnot_live/camera"),
        overlay_url=_connection_live_url("/whatnot_live/overlay"),
        control_url=url_for("whatnot_live.control", _external=True),
    )


@whatnot_live_bp.route("/whatnot_live/control")
@login_required
def control():
    log_user_action("opened Whatnot Live control panel", _username())
    dropped_live = _drop_dead_live()
    return render_template(
        "whatnot_live_control.html",
        current_user=session.get("username"),
        state=_snapshot(),
        camera_url=_connection_live_url("/whatnot_live/camera"),
        overlay_url=_connection_live_url("/whatnot_live/overlay"),
        overlay_preview_url=_connection_live_url("/whatnot_live/overlay?preview=1"),
        dropped_live=dropped_live,
    )


@whatnot_live_bp.route("/whatnot_live/camera")
def camera():
    if _username():
        log_user_action("opened Whatnot Live camera page", _username())
    return render_template(
        "whatnot_live_camera.html",
        current_user=session.get("username"),
        state=_snapshot(),
    )


@whatnot_live_bp.route("/whatnot_live/overlay")
def overlay():
    preview = str(request.args.get("preview") or "").strip() in {"1", "true", "yes"}
    graphics_only = str(request.args.get("nortc") or request.args.get("graphics") or "").strip() in {
        "1",
        "true",
        "yes",
    }
    remote = str(request.args.get("remote") or request.args.get("iphone") or "").strip() in {
        "1",
        "true",
        "yes",
    }
    return render_template(
        "whatnot_live_overlay.html",
        preview=preview,
        remote=remote,
        with_camera=not graphics_only,
        state=_snapshot(),
        stream_url=url_for("whatnot_live.stream"),
        state_url=url_for("whatnot_live.public_state"),
        webrtc_url=url_for("whatnot_live.webrtc_signal"),
    )


@whatnot_live_bp.route("/overlay")
def overlay_alias():
    return overlay()


@whatnot_live_bp.route("/whatnot_live/state")
@login_required
def control_state():
    return jsonify({"ok": True, "state": _snapshot()})


@whatnot_live_bp.route("/whatnot_live/public_state")
def public_state():
    resp = jsonify({"ok": True, "state": _snapshot()})
    return _cors(resp)


@whatnot_live_bp.route("/whatnot_live/stream")
def stream():
    last = _intish(request.args.get("last"), 0)

    def generate():
        nonlocal last
        yield "retry: 1500\n\n"
        while True:
            payload = None
            with _lock:
                _ensure()
                if _seq <= last:
                    _lock.wait(timeout=12)
                if _seq > last:
                    last = _seq
                    payload = json.dumps(_snapshot(), default=str)
            if payload:
                yield f"id: {last}\ndata: {payload}\n\n"
            else:
                yield ":\n\n"

    resp = Response(generate(), mimetype="text/event-stream")
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["X-Accel-Buffering"] = "no"
    resp.headers["Connection"] = "keep-alive"
    return _cors(resp)


def _apply_ingest(body: Dict[str, Any], source: str = "ingest") -> Dict[str, Any]:
    patch: Dict[str, Any] = {"source": source}
    prev = _snapshot()
    sku = _clean_sku(body.get("sku") or body.get("product") or "")
    lookup = None
    if sku:
        if sku != _clean_sku(prev.get("sku")):
            lookup = _lookup_sku(sku)
            if lookup:
                patch.update(lookup)
            else:
                patch["sku"] = sku
        else:
            patch["sku"] = sku
    title = body.get("title") or body.get("listing_title") or body.get("product_title")
    if title not in (None, ""):
        patch["listing_title"] = str(title).strip()[:240]
    category = body.get("category") or body.get("listing_category")
    if category not in (None, ""):
        patch["listing_category"] = str(category).strip()[:80]
    show_title = body.get("show_title")
    if show_title not in (None, ""):
        patch["show_title"] = str(show_title).strip()[:160]
    if "gross_sales" in body:
        patch["gross_sales"] = _money(body.get("gross_sales"))
    if "estimated_orders" in body:
        patch["estimated_orders"] = max(0, _intish(body.get("estimated_orders"), 0))
    for key in ("color", "style", "ticker", "bidder"):
        if body.get(key) not in (None, ""):
            patch[key] = str(body.get(key)).strip()
    if "bid" in body or "current_bid" in body:
        patch["bid"] = _money(body.get("bid", body.get("current_bid")))
    if "starting_bid" in body:
        patch["starting_bid"] = _money(body.get("starting_bid"))
    if "bid_increment" in body or "increment" in body:
        patch["bid_increment"] = _money(body.get("bid_increment", body.get("increment")))
    if "available" in body:
        patch["available"] = max(0, _intish(body.get("available"), 1))
    if "viewers" in body:
        patch["viewers"] = max(0, _intish(body.get("viewers"), 0))
    if "time_left" in body or "seconds" in body:
        raw = body.get("time_left", body.get("seconds"))
        patch["time_left"] = None if raw in (None, "") else max(0, _intish(raw, 0))
    if "live" in body:
        patch["live"] = bool(body.get("live"))
    if "show_live" in body:
        patch["show_live"] = bool(body.get("show_live"))
    elif "live" in body:
        patch["show_live"] = bool(body.get("live")) and (
            _intish(body.get("viewers"), _intish(patch.get("viewers"), 0)) > 0
            or _money(body.get("bid", body.get("current_bid"))) > 0
        )
    if "msrp_overlay" in body:
        patch["msrp_overlay"] = _money(body.get("msrp_overlay"))
    elif "msrp" in body and not lookup:
        raw_msrp = _money(body.get("msrp"))
        patch["msrp"] = raw_msrp
        patch["msrp_overlay"] = round(raw_msrp * MSRP_MULTIPLIER, 2)
    live_raw = body.get("live_url") or body.get("dashboard_url") or body.get("url") or body.get("whatnot_url") or body.get("live_id")
    if live_raw:
        parsed = _parse_live_link(live_raw)
        new_id = str(parsed.get("live_id") or "").lower()
        prev_id = str(_snapshot().get("live_id") or "").lower()
        probe_url = parsed.get("dashboard_url") or parsed.get("live_url") or str(live_raw)
        if new_id and new_id != prev_id and not body.get("_live_checked"):
            status = _whatnot_live_http_status(probe_url)
            if status == 404:
                parsed = {}
        if parsed.get("live_id") or parsed.get("live_url"):
            parsed["live_url_input"] = str(body.get("live_url_input") or live_raw).strip()
            patch.update(parsed)

    popup = None
    event_kind = str(body.get("event") or body.get("type") or body.get("alert") or "").strip()
    prev_title = str(prev.get("listing_title") or "").strip()
    new_title = str(patch.get("listing_title") or "").strip()
    title_changed = bool(new_title and prev_title and new_title != prev_title)
    if title_changed:
        if "bid" not in body and "current_bid" not in body:
            patch["bid"] = 0
            patch["bidder"] = ""
            patch["time_left"] = None
    if event_kind:
        popup = _popup_from_event(event_kind, body)
    elif body.get("sold") is True:
        popup = _popup_from_event("sold", body)
    elif title_changed:
        popup = _popup_from_event("next_item", body)
    elif "bid" in patch and patch["bid"] > _money(prev.get("bid")) and patch["bid"] > 0:
        popup = _popup_from_event("high_bid", {"bid": patch["bid"]})
    state = _publish(patch, popup=popup)
    if any(key in patch for key in LIVE_PERSIST_KEYS):
        _write_persisted_live(state)
    return state


@whatnot_live_bp.route("/whatnot_live/ingest", methods=["POST", "OPTIONS"])
def ingest():
    if request.method == "OPTIONS":
        return _cors(Response(status=204))
    if not _ingest_token_ok():
        return _cors(jsonify({"ok": False, "error": "Invalid ingest token"})), 401
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return _cors(jsonify({"ok": False, "error": "JSON object required"})), 400
    state = _apply_ingest(body, source="extension")
    return _cors(jsonify({"ok": True, "state": state}))


@whatnot_live_bp.route("/whatnot_live/load_sku", methods=["POST"])
@login_required
def load_sku():
    body = request.get_json(silent=True) or {}
    sku = _clean_sku(body.get("sku") or "")
    if not sku:
        return jsonify({"ok": False, "error": "Enter a SKU"}), 400
    found = _lookup_sku(sku)
    if not found:
        return jsonify({"ok": False, "error": f"SKU {sku} not found in FDC / E_FAS"}), 404
    patch = dict(found)
    if body.get("reset_bid"):
        patch["bid"] = 0
        patch["bidder"] = ""
        patch["time_left"] = None
    state = _publish(patch)
    log_user_action(f"Whatnot Live loaded SKU {found['sku']}", _username())
    return jsonify({"ok": True, "state": state})


@whatnot_live_bp.route("/whatnot_live/update", methods=["POST"])
@login_required
def update_state():
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return jsonify({"ok": False, "error": "JSON object required"}), 400
    live_raw = body.get("live_url") or body.get("dashboard_url") or body.get("url") or body.get("whatnot_url")
    if live_raw:
        parsed = _parse_live_link(live_raw)
        probe_url = parsed.get("dashboard_url") or parsed.get("live_url") or str(live_raw).strip()
        status = _whatnot_live_http_status(probe_url)
        if status == 404:
            snap = _snapshot()
            same = parsed.get("live_id") and parsed.get("live_id") == str(snap.get("live_id") or "").lower()
            if same or not snap.get("live_id"):
                _publish(_clear_live_fields())
                _write_persisted_live()
                snap = _snapshot()
            return jsonify({
                "ok": False,
                "error": "That Whatnot live URL is gone (404).",
                "status": 404,
                "state": snap,
            }), 404
        body = dict(body)
        body["live_url_input"] = str(live_raw).strip()
        body["_live_checked"] = True
    state = _apply_ingest(body, source=_username() or "control")
    return jsonify({"ok": True, "state": state})


@whatnot_live_bp.route("/whatnot_live/alert", methods=["POST"])
@login_required
def trigger_alert():
    body = request.get_json(silent=True) or {}
    popup = _popup_from_event(str(body.get("type") or ""), body)
    if not popup:
        return jsonify({"ok": False, "error": "Unknown alert type"}), 400
    state = _publish(popup=popup)
    return jsonify({"ok": True, "state": state})


@whatnot_live_bp.route("/whatnot_live/clear_popup", methods=["POST"])
@login_required
def clear_popup():
    _ensure()
    with _lock:
        _state["popup"] = None
    return jsonify({"ok": True, "state": _publish()})


@whatnot_live_bp.route("/whatnot_live/webrtc", methods=["GET", "POST", "OPTIONS"])
def webrtc_signal():
    _ensure()
    if request.method == "OPTIONS":
        return _cors(Response(status=204))
    if request.method == "GET":
        with _lock:
            payload = _webrtc_public()
        return _cors(jsonify(payload))
    body = request.get_json(silent=True) or {}
    action = str(body.get("action") or "").strip().lower()
    slot_name = _slot_name(body.get("slot"))
    with _lock:
        slot = _slot(slot_name)
        if action in {"reset", "hangup"}:
            if str(body.get("slot") or "").strip():
                _webrtc.setdefault("slots", {})[slot_name] = _empty_slot()
            else:
                _webrtc.update(_empty_webrtc())
        elif action == "viewer_hello":
            slot["answer"] = None
            slot["view_ice"] = []
            slot["pub_ice"] = []
            slot["offer"] = None
            slot["need_offer"] = True
            slot["viewer_seq"] = int(slot.get("viewer_seq") or 0) + 1
            _webrtc["updated_at"] = time.time()
        elif action == "offer":
            slot["offer"] = body.get("sdp")
            slot["answer"] = None
            slot["pub_ice"] = []
            slot["view_ice"] = []
            slot["need_offer"] = False
            _webrtc["publisher"] = True
            _webrtc["updated_at"] = time.time()
        elif action == "answer":
            slot["answer"] = body.get("sdp")
            _webrtc["updated_at"] = time.time()
        elif action == "ice":
            cand = body.get("candidate")
            role = str(body.get("role") or "pub").strip().lower()
            bucket = "pub_ice" if role in {"pub", "publisher", "camera"} else "view_ice"
            if cand:
                slot.setdefault(bucket, []).append(cand)
                if len(slot[bucket]) > 40:
                    slot[bucket] = slot[bucket][-40:]
            _webrtc["updated_at"] = time.time()
        elif action == "audio":
            try:
                level = float(body.get("level") or 0)
            except (TypeError, ValueError):
                level = 0.0
            _webrtc["audio_level"] = max(0.0, min(1.0, level))
            _webrtc["has_mic"] = bool(body.get("has_mic", True))
            _webrtc["audio_at"] = time.time()
        elif action == "local":
            live = bool(body.get("live", True))
            _webrtc["local_live"] = live
            _webrtc["local_at"] = time.time() if live else 0.0
            _webrtc["has_mic"] = bool(body.get("has_mic", False)) if live else False
            if "level" in body:
                try:
                    level = float(body.get("level") or 0)
                except (TypeError, ValueError):
                    level = 0.0
                _webrtc["audio_level"] = max(0.0, min(1.0, level))
                _webrtc["audio_at"] = time.time()
            _webrtc["updated_at"] = time.time()
        else:
            return _cors(jsonify({"ok": False, "error": "Unknown WebRTC action"})), 400
        payload = _webrtc_public()
    return _cors(jsonify(payload))


@whatnot_live_bp.route("/whatnot_live/extension.zip")
@login_required
def extension_zip():
    if not EXTENSION_DIR.is_dir():
        return jsonify({"ok": False, "error": "Extension folder missing"}), 404
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in EXTENSION_DIR.rglob("*"):
            if not path.is_file() or path.name == ".DS_Store":
                continue
            # Files at zip root so Load unpacked sees manifest.json immediately.
            zf.write(path, path.relative_to(EXTENSION_DIR).as_posix())
    buf.seek(0)
    return Response(
        buf.getvalue(),
        mimetype="application/zip",
        headers={"Content-Disposition": "attachment; filename=fdc-whatnot-live.zip"},
    )
