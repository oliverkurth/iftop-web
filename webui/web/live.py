"""Live view: the page and /api/live, which returns the top N connections."""

import json
import os
import threading
import time

from flask import Blueprint, current_app, jsonify, render_template, request
from flask_login import login_required

from common.ports import client_side
from .filters import PROTOCOLS, FilterError, endpoint_has_port, parse_port, parse_proto

bp = Blueprint("live", __name__)

# Index into iftop's rate lists, by averaging window in seconds.
WINDOWS = {2: 0, 10: 1, 40: 2}
SORTS = ("total", "sent", "recv")
DEFAULT_LIMIT = 50
MAX_LIMIT = 1000
# A snapshot older than this means the collector or iftop is not running.
STALE_AFTER = 10


class SnapshotCache:
    """Parses live.json only when the file has changed.

    The connection list with ephemeral ports folded is built on first use and
    kept with the snapshot, so it too is computed once per snapshot.
    """

    def __init__(self, path, server_ports=(), ephemeral_port_min=32768):
        self.path = path
        self.server_ports = frozenset(server_ports)
        self.ephemeral_port_min = ephemeral_port_min
        self.lock = threading.Lock()
        self.key = None
        self.snap = None
        self.folded = None

    def get(self, fold=False):
        """Returns (snapshot, connections), or (None, None) if there is no data."""
        try:
            st = os.stat(self.path)
        except FileNotFoundError:
            return None, None
        key = (st.st_mtime_ns, st.st_size, st.st_ino)
        with self.lock:
            if key != self.key:
                with open(self.path) as f:
                    snap = json.load(f)
                for c in snap.get("connections", []):
                    c["_search"] = _search_text(c)
                self.snap, self.key, self.folded = snap, key, None
            if not fold:
                return self.snap, self.snap.get("connections", [])
            if self.folded is None:
                self.folded = fold_connections(self.snap.get("connections", []),
                                               self.server_ports, self.ephemeral_port_min)
            return self.snap, self.folded


def _folded_endpoint(e):
    return {"addr": e.get("addr"), "host": e.get("host"), "port": None, "service": None, "folded": True}


def fold_connections(conns, server_ports=(), ephemeral_port_min=32768):
    """Merges connections that are equal once the client port is folded.

    Rates and totals are summed; "merged" is the number of connections in
    each row.
    """
    rows = {}
    for c in conns:
        side = client_side(c["src"], c["dst"], server_ports, ephemeral_port_min)
        src = _folded_endpoint(c["src"]) if side == "src" else c["src"]
        dst = _folded_endpoint(c["dst"]) if side == "dst" else c["dst"]
        key = (c.get("protocol"), src.get("addr"), src.get("port"), side == "src",
               dst.get("addr"), dst.get("port"), side == "dst")
        row = rows.get(key)
        if row is None:
            rows[key] = {
                "src": dict(src), "dst": dict(dst), "protocol": c.get("protocol"),
                "sent_rate": list(c["sent_rate"]), "recv_rate": list(c["recv_rate"]),
                "total_sent": c.get("total_sent") or 0, "total_recv": c.get("total_recv") or 0,
                "merged": 1,
            }
            continue
        for i in range(3):
            row["sent_rate"][i] += c["sent_rate"][i]
            row["recv_rate"][i] += c["recv_rate"][i]
        row["total_sent"] += c.get("total_sent") or 0
        row["total_recv"] += c.get("total_recv") or 0
        row["merged"] += 1
        # Prefer a resolved name if only some of the merged entries have one.
        for end, new in (("src", src), ("dst", dst)):
            if row[end].get("host") is None and new.get("host"):
                row[end]["host"] = new["host"]
    result = list(rows.values())
    for row in result:
        row["_search"] = _search_text(row)
    return result


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
        "merged": c.get("merged", 1),
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


def top_connections(conns, limit, window, sort, filter_text, port=None, proto=None):
    """Filters, sorts by the given window, and keeps the first `limit`.

    port is a number or a service name and matches either end; proto is a
    protocol number.
    """
    idx = WINDOWS[window]
    terms = filter_text.lower().split()
    matched = [c for c in conns
               if (not terms or _matches(c, terms))
               and (proto is None or c.get("protocol") == proto)
               and (port is None or endpoint_has_port(c["src"], port) or endpoint_has_port(c["dst"], port))]
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
    fold = request.args.get("fold") == "1"
    snap, conns = current_app.extensions["iftop_live"].get(fold)
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
    try:
        port = parse_port(request.args.get("port"))
        proto = parse_proto(request.args.get("proto"))
    except FilterError as e:
        return jsonify(error=str(e)), 400

    age = max(0, int(time.time()) - snap["timestamp"])
    result = {
        "timestamp": snap["timestamp"],
        "age": age,
        "stale": age > STALE_AFTER,
        "interface": snap.get("interface"),
        "window": window,
        "sort": sort,
        "fold": fold,
        "connection_count": len(snap.get("connections", [])),
        "totals": snap.get("totals", {}),
    }
    result.update(top_connections(conns, limit, window, sort, filter_text, port, proto))
    response = jsonify(result)
    response.headers["Cache-Control"] = "no-store"
    return response
