"""Load nssm-fdcef-environment.env into os.environ (same rules as app.py)."""
from __future__ import annotations

import os


def load_nssm_env_file(*, reload_empty: bool = True) -> None:
    """Apply KEY=VALUE lines from nssm-fdcef-environment.env.

    Variables already set in os.environ are kept, unless reload_empty is True and
    the current value is blank (so filling in nssm-fdcef-environment.env works without restart).
    """
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    path = os.path.join(root, "nssm-fdcef-environment.env")
    if not os.path.isfile(path):
        return
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for raw in handle:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key, val = key.strip(), val.strip()
                if not key:
                    continue
                if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                    val = val[1:-1]
                current = os.environ.get(key)
                if key not in os.environ:
                    os.environ[key] = val
                elif reload_empty and not str(current or "").strip():
                    os.environ[key] = val
    except OSError:
        pass
