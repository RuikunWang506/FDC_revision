"""Set html lang attribute to {{ locale }} on all templates."""
from pathlib import Path

ROOT = Path(r"c:\FDC_FLASK_APP")
patterns = [
    '<html lang="en">',
    "<html lang='en'>",
    '<html lang="en" ',
]
replacement = '<html lang="{{ locale }}">'

for path in ROOT.rglob("*.html"):
    if "node_modules" in path.parts:
        continue
    text = path.read_text(encoding="utf-8")
    original = text
    for pat in patterns:
        if pat.endswith(" "):
            text = text.replace(pat, '<html lang="{{ locale }}" ')
        else:
            text = text.replace(pat, replacement)
    if text != original:
        path.write_text(text, encoding="utf-8")
        print("updated", path.relative_to(ROOT))
