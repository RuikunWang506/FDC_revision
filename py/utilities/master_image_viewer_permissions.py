"""Per-group Master Image Viewer display options."""

from __future__ import annotations

from typing import Optional


def master_image_viewer_hide_path(username: Optional[str]) -> bool:
    """When True, hide file path (路径) in image cards and detail modal."""
    uname = (username or "").strip()
    if not uname or uname.lower() == "yeti":
        return False
    from py.utilities.auth import get_user_permissions

    perms = get_user_permissions(uname)
    return bool(perms.get("master_image_viewer_hide_path"))
