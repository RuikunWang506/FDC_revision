"""Client portal (formerly /tiktok): login + shortcuts to image viewer and product lookup."""

from flask import (
    Blueprint,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from product_detail_module.product_detail import (
    fetch_style_images,
    search_product_data_with_history,
)
from py.utilities.auth import check_credentials, get_user_permissions, update_last_login
from py.utilities.client_product_detail_permissions import build_client_pd_section_flags
from py.utilities.customer_purchase_access import (
    FEATURE_CLIENT_PRODUCT_DETAIL,
    build_portal_home_context,
    customer_purchase_limit_enabled,
    customer_purchase_no_data_message,
    customer_purchase_notice,
    customer_purchase_not_purchased_message,
    filter_product_data_for_customer,
)
from py.utilities.dashboard_permissions import CLIENT_PORTAL_TOOL_IDS, tool_visible_for_user

clientview_bp = Blueprint("clientview", __name__, url_prefix="/clientview")

CLIENTVIEW_TOOL_ID = "clientview"

def is_client_portal_only_user(username: str) -> bool:
    """Users restricted to client portal tools only (no full dashboard)."""
    perms = get_user_permissions(username)
    tools = perms.get("tools")
    if not isinstance(tools, list) or not tools:
        return False
    visible = {t for t in tools if tool_visible_for_user(perms, username, t)}
    return bool(visible) and visible <= set(CLIENT_PORTAL_TOOL_IDS)


def client_portal_home_url():
    return url_for("home")


def user_can_access_clientview(username: str) -> bool:
    if not username:
        return False
    perms = get_user_permissions(username)
    return tool_visible_for_user(perms, username, CLIENTVIEW_TOOL_ID)


def user_has_portal_tool_access(username: str) -> bool:
    """Any client-portal tool (master image viewer and/or client product detail)."""
    if not username:
        return False
    perms = get_user_permissions(username)
    for tool_id in CLIENT_PORTAL_TOOL_IDS:
        if tool_visible_for_user(perms, username, tool_id):
            return True
    return False


def _has_clientview_access():
    username = session.get("username")
    if not username:
        return False, None
    if user_can_access_clientview(username):
        session["internal_user"] = True
        return True, username
    return False, None


@clientview_bp.route("/", methods=["GET"])
def clientview_home():
    """Legacy /clientview entry — use main home with allowed tool cards."""
    return redirect(url_for("home"))


@clientview_bp.route("/login", methods=["POST"])
def clientview_login():
    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    next_url = (request.form.get("next") or "").strip() or url_for("home")

    if not next_url.startswith("/"):
        next_url = url_for("home")

    if not username or not password:
        flash("请输入用户名和密码 / Enter username and password.", "danger")
        return redirect(url_for("home"))

    if not check_credentials(username, password):
        flash("用户名或密码错误 / Invalid username or password.", "danger")
        return redirect(url_for("home"))

    if not user_has_portal_tool_access(username):
        flash(
            "此账号没有客户门户权限，请联系管理员。/ "
            "This account does not have client portal access.",
            "warning",
        )
        return redirect(url_for("home"))

    session["username"] = username
    session["logged_in"] = True
    session["user_permissions"] = get_user_permissions(username)
    session.pop("tk_user", None)
    session["internal_user"] = True
    update_last_login(username)
    return redirect(next_url)


@clientview_bp.route("/logout", methods=["GET", "POST"])
def clientview_logout():
    session.pop("username", None)
    session.pop("logged_in", None)
    session.pop("user_permissions", None)
    session.pop("tk_user", None)
    flash("已退出登录 / Logged out.", "info")
    return redirect(url_for("home"))


@clientview_bp.route("/product_detail", methods=["GET", "POST"])
@clientview_bp.route("/product_detail/<string:search_input>", methods=["GET"])
def clientview_product_detail(search_input=None):
    has_access, current_user = _has_clientview_access()
    if not has_access:
        flash("请先登录 / Please sign in first.", "warning")
        return redirect(url_for("home"))

    product_data = None
    error_message = None
    history_data = []
    adjustment_data = []
    purchase_data = []

    if request.method == "POST":
        search_input = request.form.get("search_input", "").strip()
    elif request.method == "GET" and search_input:
        search_input = search_input.strip()
    else:
        search_input = request.args.get("search_id", "").strip()
        if not search_input:
            search_input = request.args.get("search", "").strip()
        if not search_input:
            search_input = ""

    customer_purchase_msg = customer_purchase_notice(
        current_user, FEATURE_CLIENT_PRODUCT_DETAIL
    )

    if search_input:
        limits_on = customer_purchase_limit_enabled(
            current_user, FEATURE_CLIENT_PRODUCT_DETAIL
        )
        raw_product_data = search_product_data_with_history(search_input)
        if limits_on:
            if not raw_product_data:
                error_message = customer_purchase_no_data_message()
            else:
                product_data = filter_product_data_for_customer(
                    current_user,
                    raw_product_data,
                    FEATURE_CLIENT_PRODUCT_DETAIL,
                )
                if not product_data:
                    error_message = customer_purchase_not_purchased_message()
                else:
                    for style_group in product_data:
                        first_sku = (
                            style_group["colors"][0]["sku"]
                            if style_group["colors"]
                            else None
                        )
                        style_group["images"] = fetch_style_images(
                            style_group["style"], first_sku
                        )
                        style_group["total_colors"] = len(
                            style_group["colors"]
                        )
        elif not raw_product_data:
            error_message = "No products found matching your search."
        else:
            product_data = raw_product_data
            for style_group in product_data:
                first_sku = (
                    style_group["colors"][0]["sku"]
                    if style_group["colors"]
                    else None
                )
                style_group["images"] = fetch_style_images(
                    style_group["style"], first_sku
                )
                style_group["total_colors"] = len(style_group["colors"])

    pd_sections = build_client_pd_section_flags(current_user)
    portal_ctx = build_portal_home_context(current_user)

    return render_template(
        "product_detail.html",
        product_data=product_data,
        search_input=search_input,
        current_user=current_user,
        error_message=error_message,
        history_data=history_data,
        adjustment_data=adjustment_data,
        purchase_data=purchase_data,
        show_navbar=False,
        portal_client_nav=True,
        is_tiktok_view=True,
        pd_sections=pd_sections,
        pd_show_item_history=pd_sections.get("item_history", False),
        pd_show_inventory_adjustment=pd_sections.get("inventory_adjustment", False),
        pd_show_purchase_history=pd_sections.get("purchase_history", False),
        pd_show_ocean=pd_sections.get("ocean", False),
        customer_purchase_notice=customer_purchase_msg,
        **portal_ctx,
    )
