"""Fail if en.json and zh.json keys are not aligned."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EN = ROOT / "locales" / "en.json"
ZH = ROOT / "locales" / "zh.json"


def main() -> int:
    en = json.loads(EN.read_text(encoding="utf-8"))
    zh = json.loads(ZH.read_text(encoding="utf-8"))
    missing_zh = sorted(set(en) - set(zh))
    extra_zh = sorted(set(zh) - set(en))
    if missing_zh or extra_zh:
        if missing_zh:
            print("Missing in zh.json:")
            for key in missing_zh:
                print(f"  {key}")
        if extra_zh:
            print("Extra in zh.json:")
            for key in extra_zh:
                print(f"  {key}")
        return 1
    print(f"Locale keys match ({len(en)} keys).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
