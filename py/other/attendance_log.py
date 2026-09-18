"""Office Wi-Fi attendance log: yeti/elaine only. Polls the R6700v2 for connected devices."""
from __future__ import annotations

import os
import subprocess
import threading
import time
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

from py.utilities import attendance_dns as dns_store
from py.utilities import attendance_store as store
from py.utilities.attendance_netgear import normalize_mac
from py.utilities.decorators import login_required
from py.utilities.logging_setup import log_user_action

attendance_log_bp = Blueprint("attendance_log", __name__)

ALLOWED_USERS = frozenset({"yeti", "elaine"})

_poller_started = False
_poll_lock = threading.Lock()
_poll_busy = False
_poll_started_mono = 0.0
_dns_poller_started = False
_dns_poll_lock = threading.Lock()
_POLL_STALE_SECONDS = 150
_ATTENDANCE_NSSM_DEFAULT = "fdcef-attendance"
_NSSM_CANDIDATES = (
    r"C:\nssm\nssm.exe",
    r"C:\NSSM\nssm.exe",
    r"C:\Tools\nssm\nssm.exe",
)


def _subprocess_no_window() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _nssm_exe_path() -> str:
    for path in _NSSM_CANDIDATES:
        if os.path.isfile(path):
            return path
    return ""


def attendance_nssm_service_name() -> str:
    return (os.environ.get("ATTENDANCE_NSSM_SERVICE") or _ATTENDANCE_NSSM_DEFAULT).strip() or _ATTENDANCE_NSSM_DEFAULT


def restart_attendance_nssm_service() -> Dict[str, Any]:
    """Restart only the standalone attendance poller NSSM service (not fdcef)."""
    if os.name != "nt":
        return {"ok": False, "error": "NSSM restart is only supported on Windows"}
    name = attendance_nssm_service_name()
    nssm = _nssm_exe_path()
    flags = _subprocess_no_window()
    detail = ""
    if nssm:
        try:
            proc = subprocess.run(
                [nssm, "restart", name],
                capture_output=True,
                text=True,
                timeout=60,
                creationflags=flags,
            )
            detail = ((proc.stdout or "") + (proc.stderr or "")).strip()
            if proc.returncode == 0:
                return {"ok": True, "service": name, "method": "nssm", "detail": detail or "restarted"}
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as err:
            detail = f"{type(err).__name__}: {err}"
    # Fallback: Windows Service Control Manager (works when service exists even without nssm on PATH).
    try:
        stop = subprocess.run(
            ["sc", "stop", name],
            capture_output=True,
            text=True,
            timeout=45,
            creationflags=flags,
        )
        time.sleep(2)
        start = subprocess.run(
            ["sc", "start", name],
            capture_output=True,
            text=True,
            timeout=45,
            creationflags=flags,
        )
        detail = ((stop.stdout or "") + (stop.stderr or "") + (start.stdout or "") + (start.stderr or "")).strip()
        if start.returncode == 0:
            return {"ok": True, "service": name, "method": "sc", "detail": detail or "restarted"}
        return {
            "ok": False,
            "service": name,
            "error": detail or f"sc start failed (code {start.returncode})",
            "nssm": nssm or "",
        }
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as err:
        return {
            "ok": False,
            "service": name,
            "error": detail or f"{type(err).__name__}: {err}",
            "nssm": nssm or "",
        }


def _username() -> str:
    return (session.get("username") or "").strip().lower()


def _allowed(username: Optional[str] = None) -> bool:
    return (username or _username()).strip().lower() in ALLOWED_USERS


def _jsonish() -> bool:
    if request.path.startswith("/attendance_log/api/"):
        return True
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return True
    try:
        return request.accept_mimetypes.best_match(["application/json", "text/html"]) == "application/json"
    except Exception:
        return False


def _deny():
    if _jsonish():
        return jsonify({"ok": False, "error": "Forbidden"}), 403
    flash("Attendance Log is limited to yeti and elaine.", "warning")
    return redirect(url_for("home"))


@attendance_log_bp.before_request
def _gate():
    if request.method == "OPTIONS":
        return None
    if request.endpoint == "attendance_log.static":
        return None
    if "username" not in session:
        if _jsonish():
            return jsonify({"ok": False, "login": True, "error": "Unauthorized"}), 401
        session["next"] = request.url
        return redirect(url_for("auth.login", next=request.path))
    if not _allowed():
        return _deny()
    return None


def start_attendance_poller(app) -> None:
    global _poller_started
    if _poller_started:
        return
    # Prefer a separate NSSM process (py/standalone/attendance_poller.py) when enabled.
    external = (os.environ.get("ATTENDANCE_EXTERNAL_POLLER") or "").strip().lower()
    if external in ("1", "true", "yes", "on"):
        try:
            app.logger.info(
                "attendance in-process poller disabled (ATTENDANCE_EXTERNAL_POLLER); "
                "use fdcef-attendance / attendance_poller.py"
            )
        except Exception:
            pass
        _poller_started = True
        return
    if app.debug and os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        return
    _poller_started = True

    def _loop():
        with app.app_context():
            while True:
                interval = 60
                try:
                    settings = store.get_settings()
                    interval = int(settings.get("poll_seconds") or 60)
                    result = run_background_poll()
                    if result.get("busy"):
                        try:
                            current_app.logger.warning(
                                "attendance poller skipped: previous poll still running"
                            )
                        except Exception:
                            pass
                except Exception:
                    try:
                        current_app.logger.exception("attendance poller failed")
                    except Exception:
                        pass
                time.sleep(max(30, min(interval, 600)))

    threading.Thread(target=_loop, name="attendance-poller", daemon=True).start()


@attendance_log_bp.record_once
def _start_poller(state):
    start_attendance_poller(state.app)
    start_dns_poller(state.app)


def run_background_poll(*, sweep: bool = False, immediate_absent: bool = False) -> Dict[str, Any]:
    """Background presence poll. Skips LAN-name enrichment so DNS/mDNS cannot freeze status."""
    global _poll_busy, _poll_started_mono
    now = time.monotonic()
    with _poll_lock:
        if _poll_busy and (now - _poll_started_mono) < _POLL_STALE_SECONDS:
            return {"ok": False, "error": "A poll is already running", "busy": True}
        # Allow a new cycle if the previous one looks wedged (SOAP/DNS hang).
        _poll_busy = True
        _poll_started_mono = now
    try:
        return store.run_poll(
            sweep=sweep,
            unpooled=True,
            immediate_absent=immediate_absent,
            enrich_lan_names=False,
        )
    finally:
        with _poll_lock:
            _poll_busy = False


def start_dns_poller(app) -> None:
    global _dns_poller_started
    if _dns_poller_started:
        return
    if app.debug and os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        return
    _dns_poller_started = True

    def _loop():
        with app.app_context():
            while True:
                interval = int(dns_store.DEFAULT_DNS_POLL_SECONDS)
                try:
                    if not _dns_poll_lock.acquire(blocking=False):
                        time.sleep(interval)
                        continue
                    try:
                        dns_store.ingest_dns_log()
                    finally:
                        _dns_poll_lock.release()
                except Exception:
                    try:
                        current_app.logger.exception("attendance DNS poller failed")
                    except Exception:
                        pass
                time.sleep(max(45, min(interval, 600)))

    threading.Thread(target=_loop, name="attendance-dns-poller", daemon=True).start()


def run_dns_ingest() -> Dict[str, Any]:
    if not _dns_poll_lock.acquire(blocking=False):
        return {"ok": False, "error": "DNS ingest already running", "busy": True}
    try:
        return dns_store.ingest_dns_log()
    finally:
        _dns_poll_lock.release()


@attendance_log_bp.route("/attendance_log")
@login_required
def attendance_page():
    log_user_action("opened Attendance Log", _username())
    try:
        store.ensure_schema()
    except Exception:
        pass
    return render_template(
        "attendance_log.html",
        current_user=session.get("username"),
    )


@attendance_log_bp.route("/attendance_log/api/summary")
@login_required
def api_summary():
    try:
        return jsonify({"ok": True, **store.summary_payload()})
    except Exception as err:
        return jsonify({"ok": False, "error": str(err)}), 500


@attendance_log_bp.route("/attendance_log/api/devices/<path:mac>")
@login_required
def api_device(mac: str):
    device = store.get_device(mac)
    if not device:
        return jsonify({"ok": False, "error": "Device not found"}), 404
    month = (request.args.get("month") or "").strip()
    start = (request.args.get("start") or "").strip()
    end = (request.args.get("end") or "").strip()
    chart = store.chart_payload(mac, month=month, start=start, end=end)
    return jsonify({"ok": True, "device": device, **chart})


@attendance_log_bp.route("/attendance_log/api/devices/<path:mac>", methods=["POST"])
@login_required
def api_update_device(mac: str):
    payload = request.get_json(silent=True) or {}
    try:
        device = store.update_device(mac, payload)
    except RuntimeError as err:
        return jsonify({"ok": False, "error": str(err)}), 500
    if not device:
        return jsonify({"ok": False, "error": "Device not found"}), 404
    log_user_action(f"updated attendance device {normalize_mac(mac)}", _username())
    return jsonify({"ok": True, "device": device})


@attendance_log_bp.route("/attendance_log/api/devices/<path:mac>", methods=["DELETE"])
@login_required
def api_delete_device(mac: str):
    return _delete_device_response(mac)


@attendance_log_bp.route("/attendance_log/api/devices/<path:mac>/remove", methods=["POST"])
@login_required
def api_remove_device(mac: str):
    return _delete_device_response(mac)


def _delete_device_response(mac: str):
    try:
        deleted = store.delete_device(mac)
    except RuntimeError as err:
        return jsonify({"ok": False, "error": str(err)}), 500
    if not deleted:
        return jsonify({"ok": False, "error": "Device not found"}), 404
    log_user_action(f"removed attendance device {normalize_mac(mac)}", _username())
    return jsonify({"ok": True})


@attendance_log_bp.route("/attendance_log/api/poll", methods=["POST"])
@login_required
def api_poll():
    payload = request.get_json(silent=True) or {}
    sweep = bool(payload.get("sweep"))
    # Manual UI refresh should drop devices missing from the router list immediately.
    immediate_absent = bool(payload.get("immediate_absent", True))
    try:
        result = run_background_poll(sweep=sweep, immediate_absent=immediate_absent)
    except RuntimeError as err:
        return jsonify({"ok": False, "error": str(err)}), 500
    return jsonify({"ok": True, **result})


@attendance_log_bp.route("/attendance_log/api/restart_poller", methods=["POST"])
@login_required
def api_restart_poller():
    """Restart NSSM service fdcef-attendance only (standalone Wi-Fi poller)."""
    result = restart_attendance_nssm_service()
    if result.get("ok"):
        log_user_action(
            f"restarted attendance NSSM service {result.get('service')}",
            _username(),
        )
        return jsonify(result)
    return jsonify(result), 500


@attendance_log_bp.route("/attendance_log/api/dns/clients")
@login_required
def api_dns_clients():
    try:
        return jsonify(
            {
                "ok": True,
                "clients": dns_store.list_dns_client_ips(),
                "meta": {
                    "last_poll": dns_store._get_meta("last_poll_local"),
                    "last_error": dns_store._get_meta("last_error"),
                    "password_set": dns_store.dns_ssh_settings()["password_set"],
                },
            }
        )
    except Exception as err:
        return jsonify({"ok": False, "error": str(err)}), 500


@attendance_log_bp.route("/attendance_log/api/dns/queries")
@login_required
def api_dns_queries():
    ip = (request.args.get("ip") or "").strip()
    try:
        hours = int(request.args.get("hours") or 24)
    except ValueError:
        hours = 24
    try:
        limit = int(request.args.get("limit") or 500)
    except ValueError:
        limit = 500
    try:
        return jsonify(dns_store.list_dns_queries_for_ip(ip, hours=hours, limit=limit))
    except Exception as err:
        return jsonify({"ok": False, "error": str(err), "rows": [], "total": 0}), 500


@attendance_log_bp.route("/attendance_log/api/dns/queries.csv")
@login_required
def api_dns_queries_csv():
    """Download DNS rows for the selected client IP (full window, up to 50k rows)."""
    ip = (request.args.get("ip") or "").strip()
    try:
        hours = int(request.args.get("hours") or 24)
    except ValueError:
        hours = 24
    if not ip:
        return jsonify({"ok": False, "error": "Select a client IP first"}), 400
    try:
        filename, csv_text = dns_store.dns_queries_csv_for_ip(ip, hours=hours, limit=50000)
    except Exception as err:
        return jsonify({"ok": False, "error": str(err)}), 500
    return Response(
        csv_text,
        mimetype="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


@attendance_log_bp.route("/attendance_log/api/dns/ingest", methods=["POST"])
@login_required
def api_dns_ingest():
    try:
        result = run_dns_ingest()
    except Exception as err:
        return jsonify({"ok": False, "error": str(err)}), 500
    status = 200 if result.get("ok") else 500
    return jsonify(result), status