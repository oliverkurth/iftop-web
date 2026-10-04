import json
import re

import pytest

from conftest import PASSWORD, sample_lines
from web import create_app


# ---- authentication ----

def test_pages_require_login(client, live_file):
    live_file()
    resp = client.get("/")
    assert resp.status_code == 302
    assert resp.headers["Location"].startswith("/login?next=/")
    resp = client.get("/api/live")
    assert resp.status_code == 401
    assert resp.get_json() == {"error": "login required"}


def test_login_and_logout(client):
    resp = client.post("/login?next=/", data={"username": "alice", "password": PASSWORD})
    assert resp.status_code == 302 and resp.headers["Location"] == "/"
    assert client.get("/").status_code == 200
    assert client.post("/logout").status_code == 302
    assert client.get("/").status_code == 302


def test_wrong_password(client):
    resp = client.post("/login", data={"username": "alice", "password": "wrong password"})
    assert resp.status_code == 401
    assert b"Wrong user name or password" in resp.data
    resp = client.post("/login", data={"username": "nobody", "password": PASSWORD})
    assert resp.status_code == 401
    assert client.get("/").status_code == 302


@pytest.mark.parametrize("target", ["//evil.example/", "https://evil.example/", "/\\evil.example"])
def test_no_open_redirect(client, target):
    resp = client.post("/login", query_string={"next": target},
                       data={"username": "alice", "password": PASSWORD})
    assert resp.headers["Location"] == "/"


def test_failed_logins_are_rate_limited(client):
    for _ in range(3):
        assert client.post("/login", data={"username": "alice", "password": "nope nope"}).status_code == 401
    resp = client.post("/login", data={"username": "alice", "password": PASSWORD})
    assert resp.status_code == 429


def test_successful_logins_are_not_counted(client):
    for _ in range(5):
        assert client.post("/login", data={"username": "alice", "password": PASSWORD}).status_code == 302
        client.post("/logout")


def test_csrf_is_enforced(app):
    app.config["WTF_CSRF_ENABLED"] = True
    client = app.test_client()
    resp = client.post("/login", data={"username": "alice", "password": PASSWORD})
    assert resp.status_code == 400

    page = client.get("/login").get_data(as_text=True)
    token = re.search(r'name="csrf_token" type="hidden" value="([^"]+)"', page).group(1)
    resp = client.post("/login", data={"username": "alice", "password": PASSWORD, "csrf_token": token})
    assert resp.status_code == 302
    # Logging out needs the token too.
    assert client.post("/logout").status_code == 400


def test_password_change_ends_sessions(app, logged_in):
    assert logged_in.get("/").status_code == 200
    app.extensions["iftop_users"].set_password("alice", "another long password")
    assert logged_in.get("/").status_code == 302


def test_session_cookie_flags(cfg, tmp_path):
    cfg.web.cookie_secure = True
    app = create_app(cfg, secret_key="test-secret-key-0123456789", testing=True)
    app.config["WTF_CSRF_ENABLED"] = False
    app.extensions["iftop_users"].add("bob", PASSWORD)
    resp = app.test_client().post("/login", data={"username": "bob", "password": PASSWORD})
    cookie = resp.headers["Set-Cookie"]
    assert "iftop_session=" in cookie
    for flag in ("Secure", "HttpOnly", "SameSite=Lax", "Expires="):
        assert flag in cookie


def test_security_headers(client):
    resp = client.get("/login")
    assert "default-src 'self'" in resp.headers["Content-Security-Policy"]
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["X-Frame-Options"] == "DENY"


def test_user_cli(app):
    runner = app.test_cli_runner()
    result = runner.invoke(args=["user", "add", "carol"], input="short\nshort\n")
    assert result.exit_code != 0 and "at least" in result.output
    result = runner.invoke(args=["user", "add", "carol"], input="a long password\na long password\n")
    assert result.exit_code == 0, result.output
    result = runner.invoke(args=["user", "add", "carol"], input="a long password\na long password\n")
    assert "already exists" in result.output
    assert runner.invoke(args=["user", "list"]).output.split() == ["alice", "carol"]
    result = runner.invoke(args=["user", "passwd", "carol"], input="new long password\nnew long password\n")
    assert result.exit_code == 0, result.output
    assert app.extensions["iftop_users"].authenticate("carol", "new long password")
    assert runner.invoke(args=["user", "del", "carol"]).exit_code == 0
    assert runner.invoke(args=["user", "del", "carol"]).exit_code != 0


# ---- /api/live ----

def _all_sum(snap, kind, idx):
    return sum(c[f"{kind}_rate"][idx] for c in snap["connections"])


def test_no_data_yet(logged_in):
    resp = logged_in.get("/api/live")
    assert resp.status_code == 503


def test_live_top_n(logged_in, live_file):
    snap = live_file()
    resp = logged_in.get("/api/live?limit=10&window=2&sort=total")
    assert resp.status_code == 200
    assert resp.headers["Cache-Control"] == "no-store"
    data = resp.get_json()
    assert data["count"] == len(snap["connections"]) > 10
    assert data["shown"] == 10 and len(data["connections"]) == 10
    assert data["matched"] == data["count"]
    assert data["others"]["count"] == data["count"] - 10
    assert data["stale"] is False and data["interface"] == "lo"

    totals = [c["sent_rate"][0] + c["recv_rate"][0] for c in data["connections"]]
    assert totals == sorted(totals, reverse=True)
    # The shown rows are the largest ones.
    all_totals = sorted((c["sent_rate"][0] + c["recv_rate"][0] for c in snap["connections"]), reverse=True)
    assert totals == all_totals[:10]
    # Shown rows plus "others" add up to all connections.
    for kind in ("sent", "recv"):
        shown = sum(c[f"{kind}_rate"][0] for c in data["connections"])
        assert shown + data["others"][kind] == _all_sum(snap, kind, 0)


@pytest.mark.parametrize("window,idx", [(2, 0), (10, 1), (40, 2)])
@pytest.mark.parametrize("sort", ["sent", "recv", "total"])
def test_live_sort_by_window(logged_in, live_file, window, idx, sort):
    live_file()
    data = logged_in.get(f"/api/live?limit=1000&window={window}&sort={sort}").get_json()
    key = {
        "sent": lambda c: c["sent_rate"][idx],
        "recv": lambda c: c["recv_rate"][idx],
        "total": lambda c: c["sent_rate"][idx] + c["recv_rate"][idx],
    }[sort]
    values = [key(c) for c in data["connections"]]
    assert values == sorted(values, reverse=True)
    assert data["window"] == window and data["sort"] == sort
    assert data["others"] == {"count": 0, "sent": 0, "recv": 0}


def test_live_filter_before_limit(logged_in, live_file):
    snap = live_file()
    udp = [c for c in snap["connections"] if c["protocol"] == 17]
    assert udp, "sample data should contain UDP traffic"
    data = logged_in.get("/api/live?limit=1&filter=UDP domain").get_json()
    assert data["matched"] == len(udp)
    assert data["shown"] == 1
    assert data["connections"][0]["proto"] == "udp"
    assert data["others"]["count"] == len(udp) - 1

    data = logged_in.get("/api/live?filter=http-alt").get_json()
    assert data["matched"] > 0
    assert all("http-alt" in (c["src"]["service"], c["dst"]["service"]) for c in data["connections"])

    data = logged_in.get("/api/live?filter=no-such-host").get_json()
    assert data["matched"] == 0 and data["connections"] == []


def test_live_bad_params_fall_back(logged_in, live_file):
    live_file()
    data = logged_in.get("/api/live?limit=abc&window=7&sort=evil").get_json()
    assert data["window"] == 2 and data["sort"] == "total"
    assert data["shown"] == min(50, data["count"])
    data = logged_in.get("/api/live?limit=0").get_json()
    assert data["shown"] == 1


def test_live_stale(logged_in, live_file):
    live_file(age=60)
    data = logged_in.get("/api/live").get_json()
    assert data["stale"] is True and data["age"] >= 60


def test_live_cache_reloads_changed_file(logged_in, live_file):
    snap = json.loads(sample_lines()[-1])
    live_file(snap)
    assert logged_in.get("/api/live").get_json()["count"] == len(snap["connections"])
    live_file(dict(snap, connections=snap["connections"][:3]))
    assert logged_in.get("/api/live").get_json()["count"] == 3


def test_live_page(logged_in):
    page = logged_in.get("/").get_data(as_text=True)
    assert 'data-api="/api/live"' in page
    assert "live.js" in page
    assert logged_in.get("/static/live.js").status_code == 200
