from flask import Blueprint, render_template, request, session, redirect, url_for, flash

from product_detail_module.product_detail import (
    search_product_data_with_history,
    fetch_style_images,
)
from py.other.master_image_viewer import TK_PASSWORD


tiktok_bp = Blueprint("tiktok", __name__, url_prefix="/tiktok")


def _has_tiktok_access():
    current_user = session.get("username")
    internal_user = bool(current_user)
    tk_user = session.get("tk_user", False)

    if not internal_user and not tk_user:
        return False, current_user

    if tk_user:
        session["internal_user"] = False
    else:
        session["internal_user"] = True

    return True, current_user


@tiktok_bp.route("/", methods=["GET"])
def tiktok_home():
    has_access, current_user = _has_tiktok_access()
    return render_template(
        "tiktok.html",
        current_user=current_user if has_access else None,
    )


@tiktok_bp.route("/tk_password", methods=["POST"])
def tiktok_password():
    password = request.form.get("password")
    next_url = request.form.get("next", "")

    if password == TK_PASSWORD:
        session["tk_user"] = True
        session["internal_user"] = False
        flash("Access granted for TK user.", "info")
    else:
        flash("Incorrect password. Please try again.", "danger")

    if next_url:
        return redirect(next_url)
    return redirect(url_for("tiktok.tiktok_home"))


@tiktok_bp.route("/tk_product_detail", methods=["GET", "POST"])
@tiktok_bp.route("/tk_product_detail/<string:search_input>", methods=["GET"])
def tiktok_product_detail(search_input=None):
    has_access, current_user = _has_tiktok_access()
    if not has_access:
        return redirect(url_for("tiktok.tiktok_home"))

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

    if search_input:
        product_data = search_product_data_with_history(search_input)
        if not product_data:
            error_message = "No products found matching your search."
        else:
            for style_group in product_data:
                first_sku = style_group["colors"][0]["sku"] if style_group["colors"] else None
                style_group["images"] = fetch_style_images(style_group["style"], first_sku)
                style_group["total_colors"] = len(style_group["colors"])

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
        is_tiktok_view=True,
    )
