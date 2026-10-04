"""User accounts (users.db) and the `flask user ...` commands."""

import hashlib
import os
import sqlite3
import time

import click
from flask import current_app
from flask.cli import AppGroup
from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

MIN_PASSWORD_LENGTH = 10

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created       INTEGER NOT NULL
);
"""

# Verified against when the user does not exist, so a failed login takes as
# long for an unknown user as for a wrong password.
_DUMMY_HASH = generate_password_hash("not a password")


class User(UserMixin):
    def __init__(self, id, username, password_hash):
        self.id = id
        self.username = username
        self.password_hash = password_hash

    def get_id(self):
        # The session id includes a digest of the password hash, so changing
        # the password or deleting the user ends all existing sessions.
        return f"{self.id}.{_hash_tag(self.password_hash)}"


def _hash_tag(password_hash):
    return hashlib.sha256(password_hash.encode()).hexdigest()[:16]


class UserStore:
    def __init__(self, path):
        self.path = path

    def _connect(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def init(self):
        os.makedirs(os.path.dirname(self.path) or ".", mode=0o700, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)
        os.chmod(self.path, 0o600)

    def _row(self, sql, *params):
        conn = self._connect()
        try:
            return conn.execute(sql, params).fetchone()
        finally:
            conn.close()

    def get(self, user_id):
        row = self._row("SELECT id, username, password_hash FROM users WHERE id = ?", user_id)
        return User(*row) if row else None

    def get_by_name(self, username):
        row = self._row("SELECT id, username, password_hash FROM users WHERE username = ?", username)
        return User(*row) if row else None

    def load_session(self, session_id):
        """Returns the user for a session id from User.get_id(), or None."""
        user_id, _, tag = session_id.partition(".")
        if not user_id.isdigit():
            return None
        user = self.get(int(user_id))
        if user is None or tag != _hash_tag(user.password_hash):
            return None
        return user

    def authenticate(self, username, password):
        user = self.get_by_name(username)
        if user is None:
            check_password_hash(_DUMMY_HASH, password)
            return None
        return user if check_password_hash(user.password_hash, password) else None

    def add(self, username, password):
        with self._connect() as conn:
            conn.execute("INSERT INTO users (username, password_hash, created) VALUES (?, ?, ?)",
                         (username, generate_password_hash(password), int(time.time())))

    def set_password(self, username, password):
        with self._connect() as conn:
            cur = conn.execute("UPDATE users SET password_hash = ? WHERE username = ?",
                               (generate_password_hash(password), username))
            return cur.rowcount == 1

    def delete(self, username):
        with self._connect() as conn:
            return conn.execute("DELETE FROM users WHERE username = ?", (username,)).rowcount == 1

    def names(self):
        conn = self._connect()
        try:
            return [r["username"] for r in conn.execute("SELECT username FROM users ORDER BY username")]
        finally:
            conn.close()


def _store():
    return current_app.extensions["iftop_users"]


def _check_password(password):
    if len(password) < MIN_PASSWORD_LENGTH:
        raise click.ClickException(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
    return password


user_cli = AppGroup("user", help="Manage web interface users.")


@user_cli.command("add")
@click.argument("username")
@click.password_option()
def user_add(username, password):
    if _store().get_by_name(username):
        raise click.ClickException(f"user {username} already exists")
    _store().add(username, _check_password(password))
    click.echo(f"added user {username}")


@user_cli.command("passwd")
@click.argument("username")
@click.password_option()
def user_passwd(username, password):
    if not _store().set_password(username, _check_password(password)):
        raise click.ClickException(f"no such user: {username}")
    click.echo(f"changed password of {username}; existing sessions are logged out")


@user_cli.command("del")
@click.argument("username")
def user_del(username):
    if not _store().delete(username):
        raise click.ClickException(f"no such user: {username}")
    click.echo(f"deleted user {username}")


@user_cli.command("list")
def user_list():
    for name in _store().names():
        click.echo(name)
