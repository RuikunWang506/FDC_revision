"""
User groups: dashboard + mail permissions are stored per group; users belong to one group.
Only schema + seed helpers here; ACL logic stays in dashboard_permissions + auth.get_user_permissions.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

from mysql.connector import Error

# Default groups (sort_order). Permissions NULL = full access for dashboard + mail.
DEFAULT_GROUPS: List[Tuple[str, str, int]] = [
    ("Yeti", "yeti", 0),
    ("Admin", "admin", 5),
    ("Superadmin", "superadmin", 10),
    ("Manager", "manager", 20),
    ("Client", "client", 25),
    ("Warehouse", "warehouse", 30),
    ("Visitor", "visitor", 40),
]

# Same tools as legacy TK password (ucandoit) portal access.
CLIENT_GROUP_PERMISSIONS: Dict[str, Any] = {
    "tools": ["clientview", "master_image_viewer"],
    "mail_account_ids": [],
    "client_product_detail_restrict": True,
    "client_product_detail_sections": [],
}

CLIENT_PORTAL_USERS: Dict[str, str] = {
    "xingxing": "ucandoit",
    "mydepot": "LA8888",
}

# Lowercase usernames -> slug (after groups exist)
PRESET_USER_SLUGS: Dict[str, str] = {
    "yeti": "yeti",
    "xj": "superadmin",
    "ken": "superadmin",
    "jennifer": "superadmin",
    "yetimaster": "superadmin",
    "dennis": "superadmin",
    "ting": "manager",
    "rose": "manager",
    "jiayi": "manager",
    "helen": "manager",
    "renaldo": "warehouse",
    "daniel": "warehouse",
    "kenny": "visitor",
    "erwan": "visitor",
    "xingxing": "client",
    "mydepot": "client",
}

PROTECTED_DELETE_SLUGS = frozenset({"yeti"})


def slugify(name: str) -> str:
    s = (name or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-") or "group"


def ensure_user_groups_schema(connection) -> None:
    """Create user_groups table and users.user_group_id if missing."""
    cur = connection.cursor()
    try:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS user_groups (
                id INT AUTO_INCREMENT PRIMARY KEY,
                name VARCHAR(128) NOT NULL,
                slug VARCHAR(64) NOT NULL,
                permissions JSON NULL,
                sort_order INT NOT NULL DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE KEY uq_user_groups_slug (slug)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        cur.execute(
            """
            SELECT COUNT(*) FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'users'
              AND COLUMN_NAME = 'user_group_id'
            """
        )
        row = cur.fetchone()
        if row and row[0] == 0:
            cur.execute(
                "ALTER TABLE users ADD COLUMN user_group_id INT NULL DEFAULT NULL"
            )
            try:
                cur.execute(
                    "ALTER TABLE users ADD INDEX idx_users_user_group_id (user_group_id)"
                )
            except Error:
                pass
        connection.commit()
    finally:
        try:
            cur.close()
        except Exception:
            pass


def seed_user_groups_if_empty(connection) -> None:
    """Insert default groups on first deploy; assign preset users only at that time."""
    cur = connection.cursor(dictionary=True)
    try:
        cur.execute("SELECT COUNT(*) AS c FROM user_groups")
        n = (cur.fetchone() or {}).get("c", 0)
        if n:
            return

        ins = connection.cursor()
        try:
            for name, slug, order in DEFAULT_GROUPS:
                ins.execute(
                    """
                    INSERT INTO user_groups (name, slug, permissions, sort_order)
                    VALUES (%s, %s, NULL, %s)
                    """,
                    (name, slug, order),
                )
            connection.commit()
        finally:
            ins.close()

        cur.execute("SELECT id, slug FROM user_groups")
        slug_id = {r["slug"]: r["id"] for r in cur.fetchall()}

        u2 = connection.cursor()
        try:
            cur.execute("SELECT id, username FROM users")
            for row in cur.fetchall():
                uid = row["id"]
                uname = str(row.get("username") or "").strip()
                key = uname.lower()
                target_slug = PRESET_USER_SLUGS.get(key)
                if not target_slug or target_slug not in slug_id:
                    continue
                gid = slug_id[target_slug]
                u2.execute(
                    "UPDATE users SET user_group_id = %s WHERE id = %s",
                    (gid, uid),
                )
            connection.commit()
        finally:
            u2.close()

        vid = slug_id.get("visitor")
        if vid:
            u3 = connection.cursor()
            try:
                u3.execute(
                    "UPDATE users SET user_group_id = %s WHERE user_group_id IS NULL",
                    (vid,),
                )
                connection.commit()
            finally:
                u3.close()
    finally:
        cur.close()


def seed_client_portal_group(connection) -> None:
    """
    Ensure Client group + portal users exist (xingxing, mydepot).
    Safe to run on every app start; only inserts missing rows.
    """
    perms_json = json.dumps(CLIENT_GROUP_PERMISSIONS)
    cur = connection.cursor(dictionary=True)
    ins = connection.cursor()
    try:
        cur.execute("SELECT id FROM user_groups WHERE slug = %s LIMIT 1", ("client",))
        row = cur.fetchone()
        if row:
            gid = int(row["id"])
            ins.execute(
                "UPDATE user_groups SET permissions = %s WHERE id = %s",
                (perms_json, gid),
            )
        else:
            ins.execute(
                """
                INSERT INTO user_groups (name, slug, permissions, sort_order)
                VALUES (%s, %s, %s, %s)
                """,
                ("Client", "client", perms_json, 25),
            )
            gid = int(ins.lastrowid)
        connection.commit()

        for username, password in CLIENT_PORTAL_USERS.items():
            cur.execute("SELECT id, user_group_id FROM users WHERE username = %s", (username,))
            existing = cur.fetchone()
            if existing:
                ins.execute(
                    "UPDATE users SET user_group_id = %s, password = %s WHERE id = %s",
                    (gid, password, existing["id"]),
                )
            else:
                ins.execute(
                    """
                    INSERT INTO users (username, password, email, created_at, user_group_id)
                    VALUES (%s, %s, NULL, NOW(), %s)
                    """,
                    (username, password, gid),
                )
        connection.commit()
    finally:
        try:
            ins.close()
        except Exception:
            pass
        cur.close()


def backfill_null_user_groups(connection) -> None:
    """New column / new users: assign Visitor when group is missing."""
    cur = connection.cursor(dictionary=True)
    try:
        cur.execute("SELECT id FROM user_groups WHERE slug = 'visitor' LIMIT 1")
        row = cur.fetchone()
        if not row:
            return
        vid = row["id"]
        u = connection.cursor()
        try:
            u.execute(
                "UPDATE users SET user_group_id = %s WHERE user_group_id IS NULL",
                (vid,),
            )
            connection.commit()
        finally:
            u.close()
    finally:
        cur.close()


def fetch_groups_list(connection) -> List[Dict[str, Any]]:
    cur = connection.cursor(dictionary=True)
    try:
        cur.execute(
            "SELECT id, name, slug, permissions, sort_order FROM user_groups ORDER BY sort_order ASC, name ASC"
        )
        return list(cur.fetchall())
    finally:
        cur.close()


def parse_permissions_ui(raw: Any) -> Dict[str, Any]:
    parsed: dict = {}
    if raw:
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
        except json.JSONDecodeError:
            parsed = {}
    if not isinstance(parsed, dict):
        parsed = {}
    tools = parsed.get("tools")
    mail_ids = parsed.get("mail_account_ids")
    cpd_sections = parsed.get("client_product_detail_sections")
    return {
        "raw": parsed,
        "dashboard_restrict": "tools" in parsed,
        "mail_restrict": "mail_account_ids" in parsed,
        "tools_sel": tools if isinstance(tools, list) else [],
        "mail_sel": _int_list(mail_ids),
        "client_pd_restrict": bool(parsed.get("client_product_detail_restrict")),
        "client_pd_sections_sel": cpd_sections if isinstance(cpd_sections, list) else [],
        "customer_purchase_customer_id": str(
            parsed.get("customer_purchase_customer_id") or ""
        ).strip(),
        "master_image_viewer_customer_purchase_only": bool(
            parsed.get("master_image_viewer_customer_purchase_only")
        ),
        "client_product_detail_customer_purchase_only": bool(
            parsed.get("client_product_detail_customer_purchase_only")
        ),
        "master_image_viewer_hide_path": bool(
            parsed.get("master_image_viewer_hide_path")
        ),
        "log_viewer_allowed": bool(parsed.get("log_viewer_allowed")),
    }


def _int_list(raw: Any) -> List[int]:
    out: List[int] = []
    for x in raw or []:
        try:
            out.append(int(x))
        except (TypeError, ValueError):
            continue
    return out


def group_member_counts(connection) -> Dict[int, int]:
    cur = connection.cursor()
    try:
        cur.execute(
            "SELECT user_group_id, COUNT(*) FROM users WHERE user_group_id IS NOT NULL GROUP BY user_group_id"
        )
        return {int(r[0]): int(r[1]) for r in cur.fetchall() if r[0] is not None}
    finally:
        cur.close()
