"""
routes/pages.py — HTML page routes.
"""
from flask import Blueprint, redirect, render_template, url_for

from core.config import VALID_ZONES

bp = Blueprint("pages", __name__)


@bp.route("/")
def index():
    return redirect(url_for("pages.home_page"))


@bp.route("/home")
def home_page():
    return render_template("home.html", active_page="home")


@bp.route("/zone/<int:zone_id>")
def zone_detail_page(zone_id):
    if zone_id not in VALID_ZONES:
        return redirect(url_for("pages.home_page"))
    return render_template("zone_detail.html", active_page="home", zone_id=zone_id)


@bp.route("/zones")
def zones_page():
    return redirect(url_for("pages.home_page"))


@bp.route("/testing")
def testing_page():
    return render_template("testing.html", active_page="testing")


@bp.route("/analytics")
def analytics_page():
    return render_template("analytics.html", active_page="analytics")


@bp.route("/hardware")
def hardware_page():
    return render_template("hardware.html", active_page="hardware")
