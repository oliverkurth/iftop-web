"""Live view: the page and /api/live, which returns the top N connections."""

import json
import os
import threading
import time

from flask import Blueprint, current_app, jsonify, render_template, request
from flask_login import login_required

bp = Blueprint("live", __name__)

# Index into iftop's rate lists, by averaging window in seconds.
WINDOWS = {2: 0, 10: 1, 40: 2}
SORTS = ("total", "sent", "recv")
DEFAULT_LIMIT = 50
MAX_LIMIT = 1000
# A snapshot older than this means the collector or iftop is not running.
STALE_AFTER = 10

PROTOCOLS = {1: "icmp", 6: "tcp", 17: "udp", 58: "icmpv6", 132: "sctp"}


class SnapshotCache:
    """Parses live.json only when the file has changed."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.key = None
        self.snap = None

    def get(self):
        try:
            st = os.stat(self.path)
        except FileNotFoundError:
            return None
        key = (st.st_mtime_ns, st.st_size, st.st_ino)
        with self.lock:
            if key != self.key:
                with open(self.path) as f:
                    snap = json.load(f)
                for c in snap.get("connections", []):
                    c["_search"] = _search_text(c)
                self.snap, self.key = snap, key
            return self.snap


def _endpoint_text(e):
    parts = [e.get("host"), e.get("addr")]
    if e.get("port") is not None:
        parts += [str(e["port"]), e.get("service")]
    return " ".join(p for p in parts if p)


def _search_text(c):
    proto = PROTOCOLS.get(c.get("protocol"), str(c.get("protocol")))
    return f"{_endpoint_text(c['src'])} {_endpoint_text(c['dst'])} {proto}".lower()


def _matches(c, terms):
    return all(t in c["_search"] for t in terms)


def _sort_key(sort, idx):
    if sort == "sent":
        return lambda c: c["sent_rate"][idx]
    if sort == "recv":
        return lambda c: c["recv_rate"][idx]
    return lambda c: c["sent_rate"][idx] + c["recv_rate"][idx]


def _public(c):
    return {
        "src": c["src"],
        "dst": c["dst"],
        "protocol": c.get("protocol"),
        "proto": PROTOCOLS.get(c.get("protocol"), str(c.get("protocol"))),
        "sent_rate": c["sent_rate"],
        "recv_rate": c["recv_rate"],
        "total_sent": c.get("total_sent"),
        "total_recv": c.get("total_recv"),
    }


def _int_arg(name, default, lo, hi):
    try:
        value = int(request.args.get(name, default))
    except ValueError:
        value = default
    return max(lo, min(hi, value))


def top_connections(snap, limit, window, sort, filter_text):
    """Filters, sorts by the given window, and keeps the first `limit`."""
    idx = WINDOWS[window]
    terms = filter_text.lower().split()
    conns = snap.get("connections", [])
    matched = [c for c in conns if _matches(c, terms)] if terms else list(conns)
    matched.sort(key=_sort_key(sort, idx), reverse=True)
    shown, rest = matched[:limit], matched[limit:]
    return {
        "connections": [_public(c) for c in shown],
        "shown": len(shown),
        "matched": len(matched),
        "count": len(conns),
        "others": {
            "count": len(rest),
            "sent": sum(c["sent_rate"][idx] for c in rest),
            "recv": sum(c["recv_rate"][idx] for c in rest),
        },
    }


@bp.route("/")
@login_required
def index():
    return render_template("live.html")


@bp.route("/api/live")
@login_required
def api_live():
    snap = current_app.extensions["iftop_live"].get()
    if snap is None:
        return jsonify(error="no data yet: is the collector running?"), 503

    limit = _int_arg("limit", DEFAULT_LIMIT, 1, MAX_LIMIT)
    window = _int_arg("window", 2, 2, 40)
    if window not in WINDOWS:
        window = 2
    sort = request.args.get("sort", "total")
    if sort not in SORTS:
        sort = "total"
    filter_text = request.args.get("filter", "")[:200]

    age = max(0, int(time.time()) - snap["timestamp"])
    result = {
        "timestamp": snap["timestamp"],
        "age": age,
        "stale": age > STALE_AFTER,
        "interface": snap.get("interface"),
        "window": window,
        "sort": sort,
        "totals": snap.get("totals", {}),
    }
    result.update(top_connections(snap, limit, window, sort, filter_text))
    response = jsonify(result)
    response.headers["Cache-Control"] = "no-store"
    return response
