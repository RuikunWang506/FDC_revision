from flask import (
    Blueprint,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from py.utilities.help_content import (
    HELP_CATEGORIES,
    HELP_CATEGORY_ICONS,
    article_summary,
    category_label,
    create_user_note,
    get_article_by_slug,
    get_help_slug,
    list_articles,
    list_sidebar_articles,
    list_user_notes,
    seed_help_articles_if_needed,
)
from py.utilities.setting import login_required

help_bp = Blueprint('help', __name__, url_prefix='/help')

_schema_ready = False


def _ensure_ready():
    global _schema_ready
    if not _schema_ready:
        seed_help_articles_if_needed()
        _schema_ready = True


def _can_write_help() -> bool:
    username = (session.get('username') or '').strip().lower()
    return username == 'yeti'


def _staff_ok() -> bool:
    try:
        from py.other.clientview import user_hides_staff_nav_links

        if user_hides_staff_nav_links(session.get('username')):
            return False
    except Exception:
        pass
    return True


def _help_article_url(slug):
    return url_for('help.article', slug=slug)


@help_bp.app_context_processor
def inject_help_nav():
    endpoint = request.endpoint or ''
    slug = get_help_slug(endpoint, request.path)
    if slug:
        page_url = url_for('help.article', slug=slug)
    elif endpoint and endpoint not in ('help.index', 'help.article', 'auth.login', 'auth.logout', 'static'):
        page_url = url_for('help.index', q=endpoint)
    else:
        page_url = url_for('help.index')
    show = bool(session.get('username')) and _staff_ok()
    return {
        'help_page_slug': slug,
        'help_context_url': page_url,
        'show_help_nav': show,
        'show_online_nav': show,
        'help_article_url': _help_article_url,
    }


@help_bp.route('')
@help_bp.route('/')
@login_required
def index():
    if not _staff_ok():
        return redirect(url_for('home'))
    _ensure_ready()
    search = (request.args.get('q') or '').strip()
    category = (request.args.get('category') or '').strip() or None
    articles = list_articles(category=category, search=search or None)
    for art in articles:
        art['summary'] = article_summary(art.get('body_html') or '')
    popular = list_articles(category='getting_started')[:4] or articles[:6]
    return render_template(
        'help_center.html',
        articles=articles,
        popular=popular,
        search=search,
        active_category=category,
        categories=HELP_CATEGORIES,
        category_icons=HELP_CATEGORY_ICONS,
        category_label=category_label,
        current_user=session.get('username'),
    )


@help_bp.route('/article/<slug>')
@login_required
def article(slug):
    if not _staff_ok():
        return redirect(url_for('home'))
    _ensure_ready()
    row = get_article_by_slug(slug)
    if not row:
        return render_template(
            'help_center.html',
            articles=[],
            popular=[],
            search='',
            active_category=None,
            categories=HELP_CATEGORIES,
            category_icons=HELP_CATEGORY_ICONS,
            category_label=category_label,
            not_found=True,
            current_user=session.get('username'),
        ), 404
    notes = list_user_notes(row['id'])
    sidebar = list_sidebar_articles(row['slug'])
    sidebar_groups = []
    grouped = {}
    for item in sidebar:
        grouped.setdefault(item.get('category') or 'page_guide', []).append(item)
    for cat in HELP_CATEGORIES:
        items = grouped.get(cat) or []
        if items:
            sidebar_groups.append({'category': cat, 'label': category_label(cat), 'articles': items})
    return render_template(
        'help_article.html',
        article=row,
        notes=notes,
        sidebar=sidebar,
        sidebar_groups=sidebar_groups,
        category_label=category_label,
        can_write=_can_write_help(),
        current_user=session.get('username'),
    )


@help_bp.route('/page/<path:page_key>')
@login_required
def page_help(page_key):
    slug = get_help_slug(page_key, '/' + page_key.lstrip('/'))
    if slug:
        return redirect(url_for('help.article', slug=slug))
    return redirect(url_for('help.index', q=page_key))


@help_bp.route('/api/notes', methods=['POST'])
@login_required
def add_note():
    if not _can_write_help():
        return jsonify({'ok': False, 'error': 'forbidden'}), 403
    payload = request.get_json(silent=True) or {}
    article_id = payload.get('article_id')
    body = (payload.get('body') or '').strip()
    try:
        article_id = int(article_id)
    except (TypeError, ValueError):
        return jsonify({'ok': False, 'error': 'article_id required'}), 400
    note_id = create_user_note(article_id, session.get('username') or '', body)
    if not note_id:
        return jsonify({'ok': False, 'error': 'could not save'}), 400
    return jsonify({'ok': True, 'id': note_id})
