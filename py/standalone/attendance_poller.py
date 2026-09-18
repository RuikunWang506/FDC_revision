#!/usr/bin/env python3
"""Standalone office Wi-Fi attendance poller (separate NSSM process from fdcef).

Runs Netgear presence polls + phone Slack join/leave outside the Flask app so a
hung or crashed web service does not stop attendance updates.

Env (nssm-fdcef-environment.env):
  ATTENDANCE_EXTERNAL_POLLER=1   # tell fdcef to skip its in-process poller
  ATTENDANCE_POLL_SECONDS=30     # optional; also read from attendance_settings

NSSM (example):
  nssm install fdcef-attendance "C:\\path\\to\\python.exe" ^
    "C:\\FDC_FLASK_APP\\py\\standalone\\attendance_poller.py"
  nssm set fdcef-attendance AppDirectory C:\\FDC_FLASK_APP
  nssm start fdcef-attendance

Or use auto_run\\Install-AttendancePoller.ps1
"""
from __future__ import annotations

import os
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from py.utilities.nssm_env import load_nssm_env_file

load_nssm_env_file()

from py.utilities import attendance_store as store  # noqa: E402


def _log(msg: str) -> None:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[attendance-poller {stamp}] {msg}"
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        from py.utilities.logging_setup import log_event

        log_event("info", line)
    except Exception:
        pass


def _interval_seconds() -> int:
    try:
        settings = store.get_settings()
        return max(30, min(int(settings.get("poll_seconds") or 30), 600))
    except Exception:
        raw = (os.getenv("ATTENDANCE_POLL_SECONDS") or "30").strip()
        try:
            return max(30, min(int(raw), 600))
        except ValueError:
            return 30


def main() -> int:
    _log("starting standalone attendance poller")
    store.ensure_schema()
    failures = 0
    while True:
        interval = _interval_seconds()
        started = time.monotonic()
        try:
            result = store.run_poll(
                sweep=False,
                unpooled=True,
                immediate_absent=False,
                enrich_lan_names=False,
            )
            failures = 0
            _log(
                "poll ok complete={complete} source={source} devices={n} phone_events={pe}".format(
                    complete=bool(result.get("complete")),
                    source=result.get("source") or "?",
                    n=result.get("device_count"),
                    pe=result.get("phone_events"),
                )
            )
        except Exception as err:
            failures += 1
            _log(f"poll failed ({failures}): {type(err).__name__}: {err}")
            traceback.print_exc()
        elapsed = time.monotonic() - started
        sleep_for = max(5.0, float(interval) - elapsed)
        time.sleep(sleep_for)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        _log("stopped")
        raise SystemExit(0)
