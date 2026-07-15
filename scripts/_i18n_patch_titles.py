"""Wrap static HTML title tags with te() for i18n."""
import re
from pathlib import Path

ROOT = Path(r"c:\FDC_FLASK_APP")
DIRS = [ROOT / "templates", ROOT / "product_detail_module"]

TITLE_MAP = {
    "Order Lookup": "page.order_lookup.title",
    "Order Processor": "page.order_processor.title",
    "Invoice Lookup": "page.invoice_lookup.title",
    "Item Lookup": "page.item_lookup.title",
    "Customer Search Advanced": "page.customer.title",
    "External Stock Check": "page.external_stock_check.title",
    "Internal Stock Check": "page.internal_stock_check.title",
    "4Seller Stock": "page.four_seller.title",
    "Massive Location Update": "page.location_massive_update.title",
    "Out of Stock Check": "page.oosc.title",
    "DAM Sync Control Panel": "page.dam_sync.title",
    "Image Upload System - FDC Management": "page.image_upload.title",
    "Master Image Viewer 主图查看器 - FDC": "page.master_image_viewer.title",
    "Master Image Viewer": "page.master_image_viewer.title",
    "Bag Style Viewer": "page.bag_style_viewer.title",
    "Burlington Highlight": "page.burlington_highlight.title",
    "V7_listcreator": "page.v7_listcreator.title",
    "洛瑞丝庄园 - Shipping Management": "page.loris_shipping.title",
    "洛瑞丝庄园": "page.loris_fedex.title",
    "Mobile Barcode Scanner": "page.mobile_scanner.title",
    "Mobile Lite Search": "page.mobile_lite.title",
    "Burlington Label Processor — FDC": "page.burlington_label_processor.title",
    "Container Hub": "page.container_hub.title",
    "Upload Container File": "page.container_upload.title",
    "Warehouse Map": "page.warehouse_map.title",
    "Master Audit": "page.audit.title",
    "Log Viewer": "page.log_viewer.title",
    "Location Service": "page.location_service.title",
    "User Settings": "page.user_settings.title",
    "Reset Password": "page.reset_password.title",
    "Vendor App": "page.vendor_app.title",
    "Vendor - Erwan": "page.vendor_erwan.title",
    "TikTok Vendor": "page.vendor_tiktok.title",
    "Macy Label Maker": "page.macy.title",
    "China FTP Directory 中国FTP目录 - FDC": "page.china_directory.title",
    "TK Access - Password Required": "page.tk_password.title",
    "Bulk Image Upload - FDC Image Management": "page.bulk_upload.title",
    "Update Email": "page.update_email.title",
    "User Management — FDCEF": "page.manage_users.title",
    "Product Detail": "page.product_detail.title",
}

pat = re.compile(r"<title>(?P<body>.*?)</title>", re.IGNORECASE | re.DOTALL)

for base in DIRS:
    for path in base.rglob("*.html"):
        text = path.read_text(encoding="utf-8")
        original = text

        def repl(match):
            body = match.group("body").strip()
            if "{{" in body or "{%" in body:
                if " - {{" in body:
                    prefix = body.split(" - {{", 1)[0].strip()
                    suffix = " - {{" + body.split(" - {{", 1)[1]
                    key = TITLE_MAP.get(prefix)
                    if key:
                        return f"<title>{{{{ _('{key}') }}}}{suffix}</title>"
                return match.group(0)
            key = TITLE_MAP.get(body)
            if key:
                return f"<title>{{{{ _('{key}') }}}}</title>"
            return f"<title>{{{{ te('{body}') }}}}</title>"

        text = pat.sub(repl, text, count=1)
        if text != original:
            path.write_text(text, encoding="utf-8")
            print("titles", path.relative_to(ROOT))
