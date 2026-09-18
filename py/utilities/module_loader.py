"""Optional Flask tool loading: one failed import must not take down the app.

Core (auth, home, health, i18n) stays eager in routes.py. Everything listed here
is imported with importlib inside try/except and recorded on app.extensions.
"""
from __future__ import annotations

import importlib
import traceback
from typing import Any, Dict, List, Optional, Tuple

from flask import Flask, render_template, request, url_for

# name must match Flask blueprint.name when kind=blueprint (used by module_loaded()).
OPTIONAL_MODULES: List[Dict[str, Any]] = [
    {
        "name": "dam_routes",
        "import_path": "py.utilities.dam_routes",
        "attr": "register_dam_routes",
        "kind": "registrar",
        "path_prefixes": ("/assets",),
        "tool_ids": (),
    },
    {
        "name": "label_maker",
        "import_path": "py.Toolkits.Label_Maker",
        "attr": "register_label_maker_routes",
        "kind": "registrar",
        "path_prefixes": (
            "/download_label_excel",
            "/download_label_pdf",
            "/download_label_pdf_4perpage",
            "/print_label_pdf_warehouse",
            "/ez_label_from_order_lookup",
            "/label_maker_from_order_lookup",
        ),
        "tool_ids": (),
    },
    {
        "name": "view_all",
        "import_path": "py.Toolkits.view_all_routes",
        "attr": "register_view_all_routes",
        "kind": "registrar",
        "path_prefixes": ("/list_routes",),
        "tool_ids": (),
    },
    {
        "name": "container",
        "import_path": "py.search.container",
        "attr": "container_bp",
        "kind": "blueprint",
        "path_prefixes": ("/container",),
        "tool_ids": (),
    },
    {
        "name": "item_lookup",
        "import_path": "py.search.item_lookup",
        "attr": "item_lookup_bp",
        "kind": "blueprint",
        "path_prefixes": ("/item_lookup", "/get_item_location"),
        "tool_ids": ("item_lookup",),
    },
    {
        "name": "product_detail",
        "import_path": "product_detail_module",
        "attr": "product_detail_bp",
        "kind": "blueprint",
        "path_prefixes": ("/product_detail", "/productdetail", "/container_search"),
        "tool_ids": ("fdcef_container_search",),
    },
    {
        "name": "outofstock_check",
        "import_path": "py.search.outofstock_check",
        "attr": "outofstock_check_bp",
        "kind": "blueprint",
        "path_prefixes": ("/oosc", "/wipe_location", "/wipe_sold_out_locations", "/get_style_skus"),
        "tool_ids": ("location_service",),
    },
    {
        "name": "mobile_lite",
        "import_path": "py.other.mobile_lite",
        "attr": "mobile_lite_bp",
        "kind": "blueprint",
        "path_prefixes": ("/mobile", "/update_dimensions", "/update_package", "/update_mobile_location"),
        "tool_ids": (),
    },
    {
        "name": "v7_listcreator",
        "import_path": "py.Toolkits.V7listcreator",
        "attr": "v7_listcreator_bp",
        "kind": "blueprint",
        "path_prefixes": ("/v7_listcreator",),
        "tool_ids": ("v7_listcreator",),
    },
    {
        "name": "lori_automation",
        "import_path": "py.loris.lori_automation",
        "attr": "lori_automation_bp",
        "kind": "blueprint",
        "url_prefix": "/lori_automation",
        "path_prefixes": ("/lori_automation",),
        "tool_ids": ("loris_fedex",),
    },
    {
        "name": "customer",
        "import_path": "py.other.customer",
        "attr": "customer_bp",
        "kind": "blueprint",
        "path_prefixes": ("/customer", "/check_customer", "/list_customers", "/customer_details", "/update_customer_field"),
        "tool_ids": ("customer",),
    },
    {
        "name": "order_processor",
        "import_path": "py.order.order_processor",
        "attr": "order_processor_bp",
        "kind": "blueprint",
        "path_prefixes": ("/order_processor",),
        "tool_ids": ("order_processor",),
    },
    {
        "name": "whmap",
        "import_path": "py.other.warehousemap",
        "attr": "warehousemap_bp",
        "kind": "blueprint",
        "path_prefixes": ("/warehousemap",),
        "tool_ids": ("warehousemap",),
    },
    {
        "name": "master_audit",
        "import_path": "py.other.master_audit",
        "attr": "master_audit_bp",
        "kind": "blueprint",
        "path_prefixes": ("/audit",),
        "tool_ids": ("audit",),
    },
    {
        "name": "order_lookup",
        "import_path": "py.order.order_search",
        "attr": "order_lookup_bp",
        "kind": "blueprint",
        "path_prefixes": ("/order_lookup",),
        "tool_ids": ("order_lookup",),
    },
    {
        "name": "invoice_lookup",
        "import_path": "py.order.invoice_lookup",
        "attr": "invoice_lookup_bp",
        "kind": "blueprint",
        "path_prefixes": ("/invoice_lookup",),
        "tool_ids": ("invoice_lookup",),
    },
    {
        "name": "smart_search",
        "import_path": "py.search.smart_search",
        "attr": "smart_search_bp",
        "kind": "blueprint",
        "path_prefixes": ("/smart_search", "/api/smart_search"),
        "tool_ids": ("smart_search",),
    },
    {
        "name": "box_price_checker",
        "import_path": "py.other.box_price_checker",
        "attr": "box_price_checker_bp",
        "kind": "blueprint",
        "path_prefixes": ("/box_price_checker",),
        "tool_ids": ("box_price_checker",),
    },
    {
        "name": "mini_tools",
        "import_path": "py.other.mini_tools",
        "attr": "mini_tools_bp",
        "kind": "blueprint",
        "path_prefixes": ("/mini_tools",),
        "tool_ids": ("mini_tools",),
    },
    {
        "name": "attendance_log",
        "import_path": "py.other.attendance_log",
        "attr": "attendance_log_bp",
        "kind": "blueprint",
        "path_prefixes": ("/attendance_log",),
        "tool_ids": ("attendance_log",),
    },
    {
        "name": "macy",
        "import_path": "py.order.macy_special",
        "attr": "macy_bp",
        "kind": "blueprint",
        "path_prefixes": ("/macy",),
        "tool_ids": (),
    },
    {
        "name": "mysql_connector",
        "import_path": "py.utilities.mysql_connector",
        "attr": "mysql_connector_bp",
        "kind": "blueprint",
        "path_prefixes": ("/update_location", "/delete_bulk_locations"),
        "tool_ids": (),
    },
    {
        "name": "external_stock_check",
        "import_path": "py.other.external_stock_check",
        "attr": "external_stock_check_bp",
        "kind": "blueprint",
        "path_prefixes": ("/external_stock_check",),
        "tool_ids": ("external_stock_check",),
    },
    {
        "name": "internal_stock_check",
        "import_path": "py.other.internal_stock_check",
        "attr": "internal_stock_check_bp",
        "kind": "blueprint",
        "path_prefixes": ("/internal_stock_check",),
        "tool_ids": ("internal_stock_check",),
    },
    {
        "name": "inventory_manual_adjuster",
        "import_path": "py.other.inventory_manual_adjuster",
        "attr": "inventory_manual_adjuster_bp",
        "kind": "blueprint",
        "path_prefixes": ("/inventory_manual_adjuster",),
        "tool_ids": ("inventory_manual_adjuster",),
    },
    {
        "name": "image_upload",
        "import_path": "py.utilities.image_upload_routes",
        "attr": "image_upload_bp",
        "kind": "blueprint",
        "path_prefixes": ("/image_upload",),
        "tool_ids": ("image_upload",),
    },
    {
        "name": "master_image_viewer",
        "import_path": "py.other.master_image_viewer",
        "attr": "master_image_viewer_bp",
        "kind": "blueprint",
        "path_prefixes": ("/master_image_viewer",),
        "tool_ids": ("master_image_viewer",),
    },
    {
        "name": "clientview",
        "import_path": "py.other.clientview",
        "attr": "clientview_bp",
        "kind": "blueprint",
        "path_prefixes": ("/clientview",),
        "tool_ids": ("clientview",),
    },
    {
        "name": "dam_sync",
        "import_path": "py.dam.dam_sync_routes",
        "attr": "dam_sync_bp",
        "kind": "blueprint",
        "path_prefixes": ("/dam",),
        "tool_ids": ("dam",),
    },
    {
        "name": "bag_style_viewer",
        "import_path": "py.other.bag_style_viewer",
        "attr": "bag_style_viewer_bp",
        "kind": "blueprint",
        "path_prefixes": ("/bag_style_viewer",),
        "tool_ids": ("bag_style_viewer",),
    },
    {
        "name": "location_massive_audit",
        "import_path": "py.search.Location_massive_audit",
        "attr": "location_massive_audit_bp",
        "kind": "blueprint",
        "path_prefixes": ("/location_massive_update",),
        "tool_ids": ("location_massive_update",),
    },
    {
        "name": "boat_service",
        "import_path": "py.Toolkits.boat",
        "attr": "boat_service_bp",
        "kind": "blueprint",
        "path_prefixes": ("/boat",),
        "tool_ids": ("boat",),
    },
    {
        "name": "location_service",
        "import_path": "py.search.location_service",
        "attr": "location_service_bp",
        "kind": "blueprint",
        "path_prefixes": ("/location_service", "/locations"),
        "tool_ids": ("location_service",),
    },
    {
        "name": "pallet_detail",
        "import_path": "py.utilities.pallet_detail",
        "attr": "pallet_detail_bp",
        "kind": "blueprint",
        "path_prefixes": ("/palletdetail", "/p/"),
        "tool_ids": (),
    },
    {
        "name": "burlington_highlight",
        "import_path": "py.other.burlington_highlight",
        "attr": "burlington_highlight_bp",
        "kind": "blueprint",
        "path_prefixes": ("/burlington_highlight",),
        "tool_ids": ("burlington_highlight",),
    },
    {
        "name": "mobile_scanner",
        "import_path": "py.other.mobile_scanner",
        "attr": "mobile_scanner_bp",
        "kind": "blueprint",
        "path_prefixes": ("/mobile_scanner",),
        "tool_ids": ("mobile_scanner",),
    },
    {
        "name": "burlington_label_processor",
        "import_path": "py.other.burlington_label_processor",
        "attr": "burlington_label_processor_bp",
        "kind": "blueprint",
        "path_prefixes": ("/burlington_label_processor",),
        "tool_ids": ("burlington_label_processor",),
    },
    {
        "name": "print_center",
        "import_path": "py.other.print_center",
        "attr": "print_center_bp",
        "kind": "blueprint",
        "path_prefixes": ("/print_center",),
        "tool_ids": ("print_center",),
    },
    {
        "name": "bulletin",
        "import_path": "py.other.bulletin",
        "attr": "bulletin_bp",
        "kind": "blueprint",
        "path_prefixes": ("/bulletin",),
        "tool_ids": ("bulletin",),
    },
    {
        "name": "whatnot_live",
        "import_path": "py.other.whatnot_live",
        "attr": "whatnot_live_bp",
        "kind": "blueprint",
        "path_prefixes": ("/whatnot_live", "/overlay"),
        "tool_ids": ("whatnot_live",),
    },
    {
        "name": "tradeshowtool",
        "import_path": "py.other.tradeshowtool",
        "attr": "tradeshowtool_bp",
        "kind": "blueprint",
        "path_prefixes": ("/tradeshowtool",),
        "tool_ids": ("tradeshowtool",),
    },
    {
        "name": "vendor_app",
        "import_path": "py.other.vendor_app",
        "attr": "vendor_app_bp",
        "kind": "blueprint",
        "path_prefixes": ("/vendor_app", "/vendor/erwan"),
        "tool_ids": ("vendor_app",),
    },
    {
        "name": "vendor_tiktok_processor",
        "import_path": "py.other.vendor_tiktok_processor",
        "attr": "vendor_tiktok_processor_bp",
        "kind": "blueprint",
        "path_prefixes": ("/vendor/tiktok",),
        "tool_ids": (),
    },
    {
        "name": "four_seller",
        "import_path": "py.other.4_seller",
        "attr": "four_seller_bp",
        "kind": "blueprint",
        "path_prefixes": ("/4seller",),
        "tool_ids": ("four_seller",),
    },
    {
        "name": "four_seller_sales",
        "import_path": "py.other.4seller_sales",
        "attr": "four_seller_sales_bp",
        "kind": "blueprint",
        "path_prefixes": ("/4seller/sales",),
        "tool_ids": (),
    },
    {
        "name": "mobile_app",
        "import_path": "py.other.mobile_app_api",
        "attr": "mobile_app_bp",
        "kind": "blueprint",
        "path_prefixes": ("/api/mobile",),
        "tool_ids": (),
    },
    {
        "name": "fdc_ai",
        "import_path": "py.utilities.fdc_ai",
        "attr": "fdc_ai_bp",
        "kind": "blueprint",
        "path_prefixes": ("/api/fdc-ai",),
        "tool_ids": (),
    },
    {
        "name": "ai_sql",
        "import_path": "py.ai.ai_routes",
        "attr": "ai_query_bp",
        "kind": "blueprint",
        "path_prefixes": ("/api/ai", "/user_settings/schema-dictionary"),
        "tool_ids": (),
    },
]


def _empty_state() -> Dict[str, Dict[str, Any]]:
    return {"loaded": {}, "failed": {}}


def get_module_state(app: Optional[Flask] = None) -> Dict[str, Dict[str, Any]]:
    if app is None:
        from flask import current_app

        app = current_app
    return app.extensions.setdefault("fdc_modules", _empty_state())


def _log(level: str, message: str) -> None:
    try:
        from py.utilities.logging_setup import log_event

        log_event(level, message)
    except Exception:
        print(f"[{level}] {message}")


def _record_loaded(app: Flask, spec: Dict[str, Any]) -> None:
    state = get_module_state(app)
    state["loaded"][spec["name"]] = {
        "path_prefixes": tuple(spec.get("path_prefixes") or ()),
        "tool_ids": tuple(spec.get("tool_ids") or ()),
    }
    state["failed"].pop(spec["name"], None)


def _record_failed(app: Flask, spec: Dict[str, Any], err: BaseException) -> None:
    tb = traceback.format_exc()
    state = get_module_state(app)
    state["failed"][spec["name"]] = {
        "error": f"{type(err).__name__}: {err}",
        "path_prefixes": tuple(spec.get("path_prefixes") or ()),
        "tool_ids": tuple(spec.get("tool_ids") or ()),
    }
    state["loaded"].pop(spec["name"], None)
    _log(
        "ERROR",
        f"Optional module '{spec['name']}' failed to load: {type(err).__name__}: {err}\n{tb}",
    )


def _best_prefix_len(path: str, prefixes: Tuple[str, ...]) -> int:
    best = 0
    for pfx in prefixes:
        if not pfx:
            continue
        if path == pfx or path.startswith(pfx + "/") or (pfx.endswith("/") and path.startswith(pfx)):
            best = max(best, len(pfx))
    return best


def failed_module_for_path(app: Flask, path: str) -> Optional[str]:
    """Return the failed module that owns this path, if a loaded module is not more specific."""
    state = get_module_state(app)
    failed_name = None
    failed_len = 0
    for name, info in (state.get("failed") or {}).items():
        n = _best_prefix_len(path, tuple(info.get("path_prefixes") or ()))
        if n > failed_len:
            failed_len = n
            failed_name = name
    loaded_len = 0
    for info in (state.get("loaded") or {}).values():
        loaded_len = max(loaded_len, _best_prefix_len(path, tuple(info.get("path_prefixes") or ())))
    if failed_name and failed_len >= loaded_len and failed_len > 0:
        return failed_name
    return None


def tool_id_available(app: Flask, tool_id: str) -> bool:
    """True if this dashboard tool is not owned by a failed-only optional module."""
    if not tool_id:
        return True
    state = get_module_state(app)
    owners_loaded = False
    owners_failed = False
    for info in (state.get("loaded") or {}).values():
        if tool_id in (info.get("tool_ids") or ()):
            owners_loaded = True
    for info in (state.get("failed") or {}).values():
        if tool_id in (info.get("tool_ids") or ()):
            owners_failed = True
    if not owners_loaded and not owners_failed:
        return True
    return owners_loaded


def module_is_loaded(app: Flask, name: str) -> bool:
    return name in (get_module_state(app).get("loaded") or {})


def register_optional_modules(app: Flask) -> None:
    get_module_state(app)
    for spec in OPTIONAL_MODULES:
        try:
            mod = importlib.import_module(spec["import_path"])
            obj = getattr(mod, spec["attr"])
            kind = spec.get("kind") or "blueprint"
            if kind == "registrar":
                obj(app)
            else:
                kwargs = {}
                prefix = spec.get("url_prefix")
                if prefix:
                    kwargs["url_prefix"] = prefix
                app.register_blueprint(obj, **kwargs)
            _record_loaded(app, spec)
        except Exception as err:
            _record_failed(app, spec, err)

    failed = list((get_module_state(app).get("failed") or {}).keys())
    if failed:
        _log("WARNING", f"App started with failed optional modules: {', '.join(failed)}")
    else:
        _log("info", f"All {len(OPTIONAL_MODULES)} optional modules loaded")


def inject_module_template_helpers(app: Flask) -> None:
    @app.context_processor
    def _fdc_module_helpers():
        state = get_module_state(app)
        loaded = state.get("loaded") or {}
        failed = state.get("failed") or {}

        def module_loaded(name: str) -> bool:
            return name in loaded

        def safe_url_for(endpoint: str, **values):
            try:
                return url_for(endpoint, **values)
            except Exception:
                return "#"

        return {
            "module_loaded": module_loaded,
            "safe_url_for": safe_url_for,
            "fdc_failed_modules": list(failed.keys()),
        }

    @app.before_request
    def _unavailable_optional_module():
        path = request.path or ""
        if path.startswith("/static"):
            return None
        name = failed_module_for_path(app, path)
        if not name:
            return None
        info = (get_module_state(app).get("failed") or {}).get(name) or {}
        return (
            render_template(
                "module_unavailable.html",
                module_name=name,
                module_error=info.get("error") or "import failed",
            ),
            503,
        )


def health_modules_payload(app: Flask) -> Dict[str, Any]:
    state = get_module_state(app)
    loaded = sorted((state.get("loaded") or {}).keys())
    failed_map = state.get("failed") or {}
    failed = [
        {"name": name, "error": (info or {}).get("error", "")}
        for name, info in sorted(failed_map.items())
    ]
    return {
        "loaded": loaded,
        "failed": failed,
        "loaded_count": len(loaded),
        "failed_count": len(failed),
    }
