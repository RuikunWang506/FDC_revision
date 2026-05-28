"""Per-group visibility for sections on /clientview/product_detail."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

CLIENT_PRODUCT_DETAIL_SECTIONS: List[Dict[str, str]] = [
    {"id": "item_history", "title": "Item History"},
    {"id": "inventory_adjustment", "title": "Inventory Manual Adjustment"},
    {"id": "purchase_history", "title": "Purchase History"},
    {"id": "ocean", "title": "Ocean"},
]

SECTION_IDS = frozenset(s["id"] for s in CLIENT_PRODUCT_DETAIL_SECTIONS)


def client_pd_section_visible(
    username: Optional[str],
    section_id: str,
    perms: Optional[Dict[str, Any]] = None,
) -> bool:
    """
    When client_product_detail_restrict is set on the group, only listed sections show.
    Missing restrict key (NULL / unrestricted group) → all sections visible.
    """
    if section_id not in SECTION_IDS:
        return False
    uname = (username or "").strip()
    if uname.lower() == "yeti":
        return True
    if perms is None:
        from py.utilities.auth import get_user_permissions

        perms = get_user_permissions(uname)
    if not perms:
        return True
    if "client_product_detail_restrict" not in perms:
        return True
    if not perms.get("client_product_detail_restrict"):
        return True
    sections = perms.get("client_product_detail_sections")
    if not isinstance(sections, list):
        return False
    return section_id in sections


def build_client_pd_section_flags(
    username: Optional[str],
    perms: Optional[Dict[str, Any]] = None,
) -> Dict[str, bool]:
    return {
        s["id"]: client_pd_section_visible(username, s["id"], perms=perms)
        for s in CLIENT_PRODUCT_DETAIL_SECTIONS
    }


def enforce_client_pd_section_api(section_id: str):
    """Return Flask response tuple if forbidden, else None."""
    from flask import jsonify, session

    username = session.get("username")
    if not username:
        return None
    if client_pd_section_visible(username, section_id):
        return None
    return (
        jsonify(
            success=False,
            error="You do not have access to this product detail section.",
        ),
        403,
    )
