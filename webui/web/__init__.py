"""Flask web interface for iftop."""

import os
import secrets
from datetime import timedelta

from flask import Flask, current_app, jsonify, redirect, request, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

from common import config as config_mod
from .extensions import csrf, limiter, login_manager
from .live import SnapshotCache
from .users import UserStore, user_cli


def _create_secret(path):
    """Creates the secret key file if it does not exist yet (mode 0600)."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return
    with os.fdopen(fd, "w") as f:
        f.write(secrets.token_urlsafe(32) + "\n")


def _read_secret(path):
    try:
        _create_secret(path)
        with open(path, "rb") as f:
            secret = f.read().strip()
    except OSError as e:
        raise config_mod.ConfigError(f"cannot create or read secret_key_file {path}: {e}") from e
    if len(secret) < 16:
        raise config_mod.ConfigError(f"secret key in {path} is too short")
    return secret


def create_app(cfg=None, secret_key=None, testing=False):
    """Builds the app. cfg defaults to the config file named by $IFTOP_WEB_CONFIG."""
    if cfg is None:
        cfg = config_mod.load()
    web = cfg.web

    app = Flask(__name__)
    app.config.update(
        TESTING=testing,
        SECRET_KEY=secret_key or _read_secret(web.secret_key_file),
        PERMANENT_SESSION_LIFETIME=timedelta(minutes=web.session_timeout_minutes),
        SESSION_COOKIE_NAME="iftop_session",
        SESSION_COOKIE_SECURE=web.cookie_secure,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        LOGIN_RATE_LIMIT=web.login_rate_limit,
        IFTOP_CONFIG=cfg,
    )
    if web.proxy_fix:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    users = UserStore(web.users_db)
    users.init()
    app.extensions["iftop_users"] = users
    app.extensions["iftop_live"] = SnapshotCache(cfg.live_path, cfg.server_ports, cfg.ephemeral_port_min)

    csrf.init_app(app)
    limiter.init_app(app)
    login_manager.init_app(app)
    login_manager.session_protection = "strong"
    login_manager.user_loader(lambda session_id: current_app.extensions["iftop_users"].load_session(session_id))

    @login_manager.unauthorized_handler
    def unauthorized():
        if request.path.startswith("/api/"):
            return jsonify(error="login required"), 401
        return redirect(url_for("auth.login", next=request.full_path.rstrip("?")))

    @app.after_request
    def security_headers(response):
        h = response.headers
        h.setdefault("Content-Security-Policy",
                     "default-src 'self'; img-src 'self' data:; object-src 'none'; "
                     "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("Referrer-Policy", "same-origin")
        h.setdefault("X-Frame-Options", "DENY")
        if request.is_secure:
            h.setdefault("Strict-Transport-Security", "max-age=31536000")
        return response

    from . import auth, live
    app.register_blueprint(auth.bp)
    app.register_blueprint(live.bp)
    app.cli.add_command(user_cli)
    return app
