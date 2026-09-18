"""Help center content: schema, seed articles, CRUD."""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

_log = logging.getLogger(__name__)

from py.utilities.database_utils import get_mysql_connection
from py.utilities.help_seed import PATH_PREFIX_TO_SLUG, SEED_ARTICLES

HELP_CATEGORIES = (
    'getting_started',
    'page_guide',
    'terminology',
    'system_overview',
    'faq',
)

YETI_ONLY_SLUGS = frozenset({'db-dictionary'})

HELP_CATEGORY_ICONS = {
    'getting_started': '🚀',
    'page_guide': '📄',
    'terminology': '📖',
    'system_overview': '⚙️',
    'faq': '❓',
}

ENDPOINT_TO_SLUG = {
    'home': 'page-home',
    'item_lookup.item_lookup': 'page-sku-lookup',
    'product_detail.product_detail': 'page-product-detail',
    'product_detail.container_search': 'page-container-search',
    'boat_service.boat_service': 'page-boat',
    'master_audit.audit': 'page-audit',
    'lori_automation.loris_shipping': 'page-loris-shipping',
    'lori_automation.loris_fedex': 'page-loris',
    'lori_automation.lori_automation': 'page-loris',
    'log_center.display_user': 'page-log-center',
    'log_center.display_system': 'page-log-center',
    'log_center.display_error': 'page-log-center',
    'log_center.display_online': 'page-log-center',
    'log_center.display_visitor': 'page-log-center',
    'auth.manage_users': 'page-manage-users',
    'auth.user_settings': 'page-settings',
    'ai_sql.schema_dictionary_page': 'db-dictionary',
    'auth.external_service_log': 'page-external-log',
    'location_service.location_service': 'page-location-service',
    'location_service.location_service_menu': 'page-location-service',
    'mobile_lite.mobile_view': 'page-mobile-lite',
    'macy.macy_maker': 'page-macy',
    'master_image_viewer.china_directory': 'page-china-directory',
    'invoice_lookup.invoice_lookup': 'page-invoice-lookup',
    'customer.customer_home': 'page-customer',
    'customer.customer_details': 'page-customer',
    'customer.check_customer': 'page-customer',
    'customer.list_customers': 'page-customer',
    'customer.update_customer_field': 'page-customer',
    'order_lookup.order_lookup': 'page-order-lookup',
    'order_processor.order_processor': 'page-order-processor',
    'four_seller.four_seller_page': 'page-4seller',
    'four_seller_sales.four_seller_sales_page': 'page-4seller-sales',
    'four_seller_sales.four_seller_sales_finance_page': 'page-4seller-sales',
    'external_stock_check.external_stock_check_page': 'page-external-stock',
    'internal_stock_check.internal_stock_check_page': 'page-internal-stock',
    'inventory_manual_adjuster.inventory_manual_adjuster_page': 'page-inv-adjuster',
    'location_massive_audit.location_massive_update': 'page-location-update',
    'outofstock_check.outofstock_check': 'page-oosc',
    'dam_sync.dam_sync_home': 'page-dam',
    'image_upload.bulk_upload_page': 'page-image-upload',
    'master_image_viewer.master_image_viewer': 'page-master-images',
    'clientview.clientview_product_detail': 'page-client-portal',
    'bag_style_viewer.index': 'page-bag-style',
    'burlington_highlight.burlington_highlight': 'page-burlington-highlight',
    'v7_listcreator.index': 'page-v7',
    'mobile_scanner.mobile_scanner_page': 'page-scanner',
    'burlington_label_processor.burlington_label_processor_page': 'page-burlington-labels',
    'tradeshowtool.tradeshowtool_page': 'page-tradeshow',
    'print_center.print_center_page': 'page-print-center',
    'whatnot_live.hub': 'page-whatnot-live',
    'whatnot_live.control': 'page-whatnot-live',
    'whatnot_live.camera': 'page-whatnot-live',
    'warehousemap.warehouse': 'page-warehouse-map',
    'whmap.warehouse': 'page-warehouse-map',
    'pallet_detail.pallet_detail': 'page-pallet',
    'pallet_detail.pallet_detail_root': 'page-pallet',
    'vendor_app.vendor_app_home': 'page-vendor-app',
    'vendor_app.vendor_erwan': 'page-vendor-app',
    'box_price_checker.box_price_checker_page': 'page-box-price-checker',
    'mini_tools.mini_tools_page': 'page-mini-tools',
    'attendance_log.attendance_page': 'page-attendance-log',
    'bulletin.bulletin_page': 'page-home',
    'help.index': 'page-help-center',
    'help.article': None,
}

def get_help_slug_for_endpoint(endpoint: str | None) -> str | None:
    if not endpoint:
        return None
    return ENDPOINT_TO_SLUG.get(endpoint)


def get_help_slug_for_path(path: str | None) -> str | None:
    p = (path or '').split('?', 1)[0]
    if not p:
        return None
    for prefix, slug in PATH_PREFIX_TO_SLUG:
        if p == prefix:
            return slug
        if prefix.endswith('/'):
            if p.startswith(prefix):
                return slug
        elif p.startswith(prefix + '/'):
            return slug
    return None


def get_help_slug(endpoint: str | None = None, path: str | None = None) -> str | None:
    slug = get_help_slug_for_endpoint(endpoint)
    if slug:
        return slug
    return get_help_slug_for_path(path)


def _viewer_is_yeti() -> bool:
    try:
        from flask import has_request_context, session

        if not has_request_context():
            return False
        return (session.get('username') or '').strip().lower() == 'yeti'
    except Exception:
        return False


def _public_article(row: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if not row:
        return None
    slug = str(row.get('slug') or '')
    if slug in YETI_ONLY_SLUGS and not _viewer_is_yeti():
        return None
    return row


def category_label(category: str) -> str:
    from py.utilities.i18n import get_locale

    zh = get_locale() == 'zh'
    labels = {
        'getting_started': ('Getting started', '入门'),
        'page_guide': ('Page guides', '页面说明'),
        'terminology': ('Terminology', '术语'),
        'system_overview': ('Overview', '总览'),
        'faq': ('FAQ', '常见问题'),
    }
    pair = labels.get(category)
    if not pair:
        return category
    return pair[1] if zh else pair[0]


def apply_help_locale(row: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if not row:
        return row
    from py.utilities.i18n import get_locale

    out = dict(row)
    if get_locale() == 'zh':
        if out.get('title_zh'):
            out['title'] = out['title_zh']
        if out.get('body_html_zh'):
            out['body_html'] = out['body_html_zh']
    return out


def article_summary(body_html: str, limit: int = 140) -> str:
    text = re.sub(r'<[^>]+>', ' ', body_html or '')
    text = re.sub(r'\s+', ' ', text).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + '…'


def _conn():
    return get_mysql_connection()


def ensure_help_schema(cursor=None) -> bool:
    owns = cursor is None
    connection = None
    if owns:
        connection = _conn()
        if not connection:
            return False
        cursor = connection.cursor()
    try:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS help_articles (
                id INT AUTO_INCREMENT PRIMARY KEY,
                slug VARCHAR(120) NOT NULL UNIQUE,
                title VARCHAR(255) NOT NULL,
                category VARCHAR(64) NOT NULL DEFAULT 'page_guide',
                body_html MEDIUMTEXT,
                sort_order INT NOT NULL DEFAULT 100,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS help_user_notes (
                id INT AUTO_INCREMENT PRIMARY KEY,
                article_id INT NOT NULL,
                username VARCHAR(80) NOT NULL,
                body TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                INDEX (article_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        if owns and connection:
            connection.commit()
        for col, ddl in (
            ('title_zh', 'title_zh VARCHAR(255) NULL'),
            ('body_html_zh', 'body_html_zh MEDIUMTEXT NULL'),
        ):
            cursor.execute(f"SHOW COLUMNS FROM help_articles LIKE '{col}'")
            if not cursor.fetchone():
                cursor.execute(f'ALTER TABLE help_articles ADD COLUMN {ddl}')
        if owns and connection:
            connection.commit()
        return True
    except Exception:
        _log.exception('ensure_help_schema failed')
        return False
    finally:
        if owns:
            try:
                cursor.close()
            except Exception:
                pass
            try:
                if connection and connection.is_connected():
                    connection.close()
            except Exception:
                pass


def seed_help_articles_if_needed() -> None:
    connection = _conn()
    if not connection:
        return
    cursor = None
    try:
        cursor = connection.cursor()
        if not ensure_help_schema(cursor):
            return
        cursor.execute('SELECT COUNT(*) FROM help_articles')
        row = cursor.fetchone()
        _ = row
        for art in SEED_ARTICLES:
            cursor.execute(
                """
                INSERT INTO help_articles (slug, title, title_zh, category, body_html, body_html_zh, sort_order)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    title = VALUES(title),
                    title_zh = VALUES(title_zh),
                    category = VALUES(category),
                    body_html = VALUES(body_html),
                    body_html_zh = VALUES(body_html_zh),
                    sort_order = VALUES(sort_order)
                """,
                (
                    art['slug'],
                    art['title'],
                    art.get('title_zh') or art['title'],
                    art['category'],
                    art['body_html'],
                    art.get('body_html_zh') or art['body_html'],
                    art['sort_order'],
                ),
            )
        connection.commit()
    except Exception:
        _log.exception('seed_help_articles_if_needed failed')
        try:
            connection.rollback()
        except Exception:
            pass
    finally:
        try:
            if cursor:
                cursor.close()
        except Exception:
            pass
        try:
            if connection.is_connected():
                connection.close()
        except Exception:
            pass


def list_articles(category: Optional[str] = None, search: Optional[str] = None) -> list[dict[str, Any]]:
    connection = _conn()
    if not connection:
        return []
    cursor = None
    try:
        cursor = connection.cursor(dictionary=True)
        sql = 'SELECT id, slug, title, title_zh, category, body_html, body_html_zh, sort_order FROM help_articles'
        params: list[Any] = []
        clauses = []
        if category:
            clauses.append('category = %s')
            params.append(category)
        if search:
            clauses.append(
                '(title LIKE %s OR title_zh LIKE %s OR body_html LIKE %s OR body_html_zh LIKE %s OR slug LIKE %s)'
            )
            like = f'%{search}%'
            params.extend([like, like, like, like, like])
        if clauses:
            sql += ' WHERE ' + ' AND '.join(clauses)
        sql += ' ORDER BY sort_order ASC, title ASC'
        cursor.execute(sql, params)
        rows = [apply_help_locale(r) for r in (cursor.fetchall() or [])]
        return [r for r in rows if r and _public_article(r)]
    except Exception:
        return []
    finally:
        try:
            if cursor:
                cursor.close()
        except Exception:
            pass
        try:
            if connection.is_connected():
                connection.close()
        except Exception:
            pass


def get_article_by_slug(slug: str) -> Optional[dict[str, Any]]:
    if not slug:
        return None
    connection = _conn()
    if not connection:
        return None
    cursor = None
    try:
        cursor = connection.cursor(dictionary=True)
        cursor.execute(
            'SELECT id, slug, title, title_zh, category, body_html, body_html_zh, sort_order FROM help_articles WHERE slug = %s LIMIT 1',
            (slug,),
        )
        return _public_article(apply_help_locale(cursor.fetchone()))
    except Exception:
        return None
    finally:
        try:
            if cursor:
                cursor.close()
        except Exception:
            pass
        try:
            if connection.is_connected():
                connection.close()
        except Exception:
            pass


def list_sidebar_articles(current_slug: str = '') -> list[dict[str, Any]]:
    return list_articles()


def list_user_notes(article_id: int) -> list[dict[str, Any]]:
    connection = _conn()
    if not connection:
        return []
    cursor = None
    try:
        cursor = connection.cursor(dictionary=True)
        cursor.execute(
            'SELECT id, article_id, username, body, created_at FROM help_user_notes WHERE article_id = %s ORDER BY id DESC',
            (article_id,),
        )
        return list(cursor.fetchall() or [])
    except Exception:
        return []
    finally:
        try:
            if cursor:
                cursor.close()
        except Exception:
            pass
        try:
            if connection.is_connected():
                connection.close()
        except Exception:
            pass


def create_user_note(article_id: int, username: str, body: str) -> Optional[int]:
    body = (body or '').strip()
    if not body or not username:
        return None
    connection = _conn()
    if not connection:
        return None
    cursor = None
    try:
        cursor = connection.cursor()
        cursor.execute(
            'INSERT INTO help_user_notes (article_id, username, body) VALUES (%s, %s, %s)',
            (article_id, username, body[:4000]),
        )
        connection.commit()
        return cursor.lastrowid
    except Exception:
        try:
            connection.rollback()
        except Exception:
            pass
        return None
    finally:
        try:
            if cursor:
                cursor.close()
        except Exception:
            pass
        try:
            if connection.is_connected():
                connection.close()
        except Exception:
            pass
