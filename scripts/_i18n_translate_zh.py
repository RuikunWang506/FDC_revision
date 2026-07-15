"""Translate zh.json entries that still match English."""
import json
import sys
import time
from pathlib import Path

from deep_translator import GoogleTranslator

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(r"c:\FDC_FLASK_APP\locales")
EN_PATH = ROOT / "en.json"
ZH_PATH = ROOT / "zh.json"

en = json.loads(EN_PATH.read_text(encoding="utf-8"))
zh = json.loads(ZH_PATH.read_text(encoding="utf-8"))
tr = GoogleTranslator(source="en", target="zh-CN")

todo = [k for k, v in en.items() if zh.get(k) == v]
print("todo", len(todo), flush=True)

for i, key in enumerate(todo):
    text = en[key]
    try:
        zh[key] = tr.translate(text)
    except Exception as exc:
        print("fail", key, exc, flush=True)
        zh[key] = text
    if (i + 1) % 10 == 0:
        ZH_PATH.write_text(
            json.dumps(dict(sorted(zh.items())), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print("saved", i + 1, "/", len(todo), flush=True)
        time.sleep(0.15)

ZH_PATH.write_text(
    json.dumps(dict(sorted(zh.items())), ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)
print("finished", len(todo), flush=True)
