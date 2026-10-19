"""Login and logout."""

from urllib.parse import urlsplit

from flask import Blueprint, current_app, flash, redirect, render_template, request, session, url_for
from flask_login import login_required, login_user, logout_user
from flask_wtf import FlaskForm
from wtforms import PasswordField, StringField
from wtforms.validators import DataRequired, Length

from .extensions import limiter

bp = Blueprint("auth", __name__)


class LoginForm(FlaskForm):
    username = StringField("User name", validators=[DataRequired(), Length(max=100)])
    password = PasswordField("Password", validators=[DataRequired(), Length(max=1000)])


def _safe_next(target):
    """Only allows redirects to a path on this site."""
    # Browsers (and Werkzeug) drop tabs and newlines from URLs, so "/\t/evil"
    # would become "//evil": refuse control characters and spaces outright.
    if (target and target.startswith("/") and not target.startswith("//") and "\\" not in target
            and not any(c <= " " or c == "\x7f" for c in target)):
        parts = urlsplit(target)
        if not parts.scheme and not parts.netloc:
            return target
    return url_for("live.index")


def _login_failed(response):
    # Successful logins redirect; everything else counts against the limit.
    return response.status_code != 302


@bp.route("/login", methods=["GET", "POST"])
@limiter.limit(lambda: current_app.config["LOGIN_RATE_LIMIT"], methods=["POST"], deduct_when=_login_failed)
def login():
    form = LoginForm()
    if form.validate_on_submit():
        user = current_app.extensions["iftop_users"].authenticate(form.username.data, form.password.data)
        if user is not None:
            session.clear()
            session.permanent = True
            login_user(user)
            return redirect(_safe_next(request.args.get("next")))
        flash("Wrong user name or password.")
        return render_template("login.html", form=form), 401
    return render_template("login.html", form=form)


@bp.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    session.clear()
    return redirect(url_for("auth.login"))
