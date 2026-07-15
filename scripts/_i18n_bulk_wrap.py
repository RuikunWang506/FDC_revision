"""Extract UI strings from HTML templates and bulk-wrap with te()."""
from __future__ import annotations

import json
import re
import hashlib
from pathlib import Path

ROOT = Path(r"c:\FDC_FLASK_APP")
LOCALES = ROOT / "locales"
TEMPLATE_DIRS = [ROOT / "templates", ROOT / "product_detail_module"]
SKIP_FILES = {"_lang_switch.html", "_lang_switch_styles.html", "_lang_bar_standalone.html", "navbar.html", "footer.html"}

ATTR_PAT = re.compile(
    r'\b(placeholder|aria-label|title|alt)="([^"]{2,120})"',
    re.IGNORECASE,
)
TEXT_PAT = re.compile(r">([^<{%][^<]{1,120}?)<")
JINJA_PAT = re.compile(r"\{\{.*?\}\}|\{%.*?%\}")
SCRIPT_STYLE_SPLIT = re.compile(r"(<(?:script|style)\b[^>]*>.*?</(?:script|style)>)", re.IGNORECASE | re.DOTALL)


def is_english_phrase(s: str) -> bool:
    s = s.strip()
    if not s or len(s) < 2:
        return False
    if JINJA_PAT.search(s):
        return False
    if "{{" in s or "}}" in s or "{%" in s:
        return False
    if re.search(r"https?://|\.css|\.js|\.png|\.svg|fa[srb]?-|bi-|data-|#[0-9a-f]{3,8}", s, re.I):
        return False
    if re.fullmatch(r"[\d\s\W]+", s):
        return False
    if s.strip() in ("&times;", "×", "✕", "✖"):
        return False
    letters = sum(c.isalpha() for c in s)
    if letters < 2:
        return False
    ascii_letters = sum(c.isascii() and c.isalpha() for c in s)
    return ascii_letters / max(letters, 1) > 0.85


def slug_key(text: str) -> str:
    h = hashlib.md5(text.encode("utf-8")).hexdigest()[:8]
    base = re.sub(r"[^a-z0-9]+", "_", text.lower().strip())[:40].strip("_")
    return f"auto.{base or 'text'}_{h}"


def load_json(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    with path.open(encoding="utf-8") as fh:
        data = json.load(fh)
    return {str(k): str(v) for k, v in data.items() if v is not None}


def save_json(path: Path, data: dict[str, str]) -> None:
    ordered = dict(sorted(data.items(), key=lambda kv: kv[0]))
    with path.open("w", encoding="utf-8") as fh:
        json.dump(ordered, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def collect_phrases(html: str) -> set[str]:
    phrases: set[str] = set()
    parts = SCRIPT_STYLE_SPLIT.split(html)
    for i, part in enumerate(parts):
        if i % 2 == 1:
            continue
        for m in TEXT_PAT.finditer(part):
            raw = re.sub(r"\s+", " ", m.group(1)).strip()
            if is_english_phrase(raw):
                phrases.add(raw)
        for m in ATTR_PAT.finditer(part):
            raw = m.group(2).strip()
            if is_english_phrase(raw):
                phrases.add(raw)
    return phrases


def already_wrapped(text: str, phrase: str) -> bool:
    return f"te('{phrase}')" in text or f'te("{phrase}")' in text


def wrap_phrase(text: str, phrase: str) -> str:
    if already_wrapped(text, phrase):
        return text
    esc = phrase.replace("\\", "\\\\").replace("'", "\\'")
    # Text between tags
    text = text.replace(f">{phrase}<", f">{{{{ te('{esc}') }}}}<")
    # Attributes
    for attr in ("placeholder", "aria-label", "title", "alt"):
        text = text.replace(f'{attr}="{phrase}"', f'{attr}="{{{{ te(\'{esc}\') }}}}"')
    return text


def main() -> None:
    en_path = LOCALES / "en.json"
    zh_path = LOCALES / "zh.json"
    en = load_json(en_path)
    zh = load_json(zh_path)
    en_values = {v: k for k, v in en.items() if v}

    all_phrases: set[str] = set()
    files: list[Path] = []
    for base in TEMPLATE_DIRS:
        for path in base.rglob("*.html"):
            if path.name in SKIP_FILES:
                continue
            files.append(path)
            all_phrases |= collect_phrases(path.read_text(encoding="utf-8"))

    new_keys = 0
    for phrase in sorted(all_phrases, key=len, reverse=True):
        if phrase in en_values:
            continue
        key = slug_key(phrase)
        while key in en:
            key = key + "_x"
        en[key] = phrase
        zh[key] = phrase  # placeholder — translate below
        en_values[phrase] = key
        new_keys += 1

    print(f"Collected {len(all_phrases)} phrases, added {new_keys} new keys")

  # Manual zh overrides for common auto keys (batch translate placeholders)
    zh_updates = {
        "Reset Password": "重置密码",
        "New Password": "新密码",
        "Confirm New Password": "确认新密码",
        "Back to Login": "返回登录",
        "Reset password for user:": "重置用户密码：",
        "Search": "搜索",
        "Submit": "提交",
        "Cancel": "取消",
        "Save": "保存",
        "Clear": "清除",
        "Close": "关闭",
        "Loading": "加载中",
        "Delete": "删除",
        "Edit": "编辑",
        "Upload": "上传",
        "Download": "下载",
        "Refresh": "刷新",
        "Export": "导出",
        "Import": "导入",
        "Filter": "筛选",
        "Apply": "应用",
        "Add": "添加",
        "Remove": "移除",
        "Yes": "是",
        "No": "否",
        "OK": "确定",
        "Error": "错误",
        "Success": "成功",
        "Warning": "警告",
        "Please wait": "请稍候",
        "No results found": "未找到结果",
        "Select": "选择",
        "All": "全部",
        "None": "无",
        "Total": "合计",
        "Status": "状态",
        "Actions": "操作",
        "Details": "详情",
        "Name": "名称",
        "Date": "日期",
        "Quantity": "数量",
        "Price": "价格",
        "Customer": "客户",
        "Order": "订单",
        "Invoice": "发票",
        "SKU": "SKU",
        "Style": "款式",
        "Color": "颜色",
        "Size": "尺码",
        "Location": "库位",
        "Warehouse": "仓库",
        "Container": "集装箱",
        "Pallet": "托盘",
        "Shipping": "发货",
        "Password": "密码",
        "Username": "用户名",
        "Login": "登录",
        "Logout": "退出",
        "Settings": "设置",
        "Home": "首页",
        "Next": "下一步",
        "Previous": "上一步",
        "Back": "返回",
    }
    for en_text, zh_text in zh_updates.items():
        key = en_values.get(en_text)
        if key:
            zh[key] = zh_text

    for key, en_text in list(en.items()):
        if key.startswith("auto.") and zh.get(key) == en_text:
            # Keep English as fallback where no manual zh
            pass

    save_json(en_path, en)
    save_json(zh_path, zh)

    wrapped_files = 0
    phrases_sorted = sorted(en_values.keys(), key=len, reverse=True)
    for path in files:
        original = path.read_text(encoding="utf-8")
        text = original
        for phrase in phrases_sorted:
            if phrase in text:
                text = wrap_phrase(text, phrase)
        if text != original:
            path.write_text(text, encoding="utf-8")
            wrapped_files += 1
            print("wrap", path.relative_to(ROOT))

    print(f"Wrapped {wrapped_files} template files")


if __name__ == "__main__":
    main()
