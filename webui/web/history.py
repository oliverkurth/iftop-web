"""History: the page, /api/agg, and Chart.js from the libjs-chart.js package.

Windows are rolling ("the last hour") and made of whole buckets: a window
covers every bucket that overlaps it, so it is at least as long as asked
and at most one bucket longer. The response gives the exact range, and the
chart covers the same range as the top list.

A filter (one connection, a source or destination host, a port or service,
a protocol) narrows everything to the matching connections; the history
page uses it for the detail view of one connection or host.
"""

import os
import sqlite3
import time

from flask import Blueprint, abort, current_app, jsonify, render_template, request, send_file
from flask_login import login_required

from common import buckets
from common.schema import PORT_FOLDED, PORT_NONE
from .filters import FilterError, parse_port, parse_proto, proto_name

bp = Blueprint("history", __name__)

RESOLUTION = 2
M, H, D = buckets.MINUTE, buckets.HOUR, buckets.DAY

# window: (length, level for the top list, chart level, chart step)
# "raw" is the per-sample interface totals, kept for an hour.
WINDOWS = {
    "minute": (M, "1m", "raw", RESOLUTION),
    "5min": (5 * M, "1m", "raw", 10),
    "hour": (H, "1m", "1m", M),
    "day": (D, "1h", "1m", 5 * M),
    "week": (7 * D, "1h", "1h", H),
    "month": (30 * D, "1d", "1d", D),
    "year": (365 * D, "1d", "1d", D),
}
# With a filter the chart comes from the per-connection tables, whose finest
# resolution is a minute.
FILTERED_CHART = {
    "minute": ("1m", M), "5min": ("1m", M), "hour": ("1m", M), "day": ("1m", 5 * M),
    "week": ("1h", H), "month": ("1d", D), "year": ("1d", D),
}
LEVEL_STEP = {"1m": M, "1h": H, "1d": D}
GROUPS = ("tuple", "src", "dst")
# ORDER BY expressions; {p} is the table alias holding the sums.
SORTS = {"total": "{p}bytes_sent + {p}bytes_recv", "sent": "{p}bytes_sent", "recv": "{p}bytes_recv"}
DEFAULT_LIMIT = 20
MAX_LIMIT = 500


class NoHistory(Exception):
    pass


def connect(path):
    if not os.path.exists(path):
        raise NoHistory()
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _endpoint(addr, port, host, service):
    end = {"addr": addr or None, "host": host}
    if port == PORT_FOLDED:
        end.update(port=None, service=None, folded=True)
    elif port == PORT_NONE:
        end.update(port=None, service=None)
    else:
        end.update(port=port, service=service)
    return end


def _window_start(now, length, level):
    return buckets.bucket_start(now - length, LEVEL_STEP[level])


class Filter:
    """Restricts a query to the matching connections (rows of `tuples`, alias t)."""

    def __init__(self, tuple_id=None, src=None, dst=None, port=None, proto=None):
        self.tuple_id = tuple_id
        self.src = src
        self.dst = dst
        self.port = port        # int, or a lower-case service name
        self.proto = proto      # protocol number

    @property
    def active(self):
        return any(v is not None for v in (self.tuple_id, self.src, self.dst, self.port, self.proto))

    def sql(self, conn):
        """Returns (" AND ..." clause, params)."""
        conds, params = [], []
        if self.tuple_id is not None:
            conds.append("t.id = ?")
            params.append(self.tuple_id)
        if self.src is not None:
            conds.append("t.src_addr = ?")
            params.append(self.src)
        if self.dst is not None:
            conds.append("t.dst_addr = ?")
            params.append(self.dst)
        if self.proto is not None:
            conds.append("t.proto = ?")
            params.append(self.proto)
        if self.port is not None:
            ports = [self.port] if isinstance(self.port, int) else self._service_ports(conn)
            if ports:
                marks = ", ".join("?" * len(ports))
                conds.append(f"(t.src_port IN ({marks}) OR t.dst_port IN ({marks}))")
                params += ports + ports
            else:
                conds.append("0")      # an unknown service matches nothing
        return "".join(" AND " + c for c in conds), params

    def _service_ports(self, conn):
        sql = "SELECT DISTINCT port FROM services WHERE lower(name) = ?"
        params = [self.port]
        if self.proto is not None:
            sql += " AND proto = ?"
            params.append(self.proto)
        return [r[0] for r in conn.execute(sql, params)]

    def describe(self, conn):
        """The filter as shown on the page, with names resolved."""
        result = {"tuple": None, "src": None, "dst": None, "port": self.port,
                  "proto": None if self.proto is None else proto_name(self.proto)}
        if self.tuple_id is not None:
            r = conn.execute(f"""SELECT {_TUPLE_COLUMNS} FROM tuples t {_TUPLE_JOINS} WHERE t.id = ?""",
                             (self.tuple_id,)).fetchone()
            result["tuple"] = _tuple_item(r) if r else {"id": self.tuple_id, "unknown": True}
        for side in ("src", "dst"):
            addr = getattr(self, side)
            if addr is not None:
                r = conn.execute("SELECT name FROM hosts WHERE addr = ? ORDER BY last_seen DESC LIMIT 1",
                                 (addr,)).fetchone()
                result[side] = {"addr": addr, "host": r[0] if r else None}
        return result


_TUPLE_COLUMNS = """t.id, t.interface, t.proto, t.src_addr, t.src_port, t.dst_addr, t.dst_port,
    hs.name AS src_host, hd.name AS dst_host, ss.name AS src_service, sd.name AS dst_service"""
_TUPLE_JOINS = """LEFT JOIN hosts hs ON hs.interface = t.interface AND hs.addr = t.src_addr
    LEFT JOIN hosts hd ON hd.interface = t.interface AND hd.addr = t.dst_addr
    LEFT JOIN services ss ON ss.proto = t.proto AND ss.port = t.src_port
    LEFT JOIN services sd ON sd.proto = t.proto AND sd.port = t.dst_port"""


def _tuple_item(r):
    return {
        "id": r["id"],
        "interface": r["interface"],
        "proto": proto_name(r["proto"]),
        "protocol": r["proto"],
        "src": _endpoint(r["src_addr"], r["src_port"], r["src_host"], r["src_service"]),
        "dst": _endpoint(r["dst_addr"], r["dst_port"], r["dst_host"], r["dst_service"]),
    }


def _group_key(group):
    """SQL expression identifying an item of the group."""
    return "a.tuple_id" if group == "tuple" else f"t.{group}_addr"


def top(conn, level, start, group, sort, limit, where="", params=()):
    """Top items in agg_<level> from bucket `start` on, plus the sum of the rest."""
    inner_order = SORTS[sort].replace("{p}bytes_sent", "sum(a.bytes_sent)").replace("{p}bytes_recv", "sum(a.bytes_recv)")
    outer_order = SORTS[sort].format(p="x.")
    if group == "tuple":
        select, joins = _TUPLE_COLUMNS, _TUPLE_JOINS
        group_by = "a.tuple_id"
    else:
        col = f"{group}_addr"
        select = f"t.interface, t.{col} AS addr, h.name AS host"
        joins = f"LEFT JOIN hosts h ON h.interface = t.interface AND h.addr = t.{col}"
        group_by = f"t.interface, t.{col}"

    rows = conn.execute(f"""
        SELECT {select}, x.bytes_sent, x.bytes_recv FROM (
            SELECT min(a.tuple_id) AS any_tuple, sum(a.bytes_sent) AS bytes_sent, sum(a.bytes_recv) AS bytes_recv
            FROM agg_{level} a JOIN tuples t ON t.id = a.tuple_id
            WHERE a.bucket_start >= ?{where}
            GROUP BY {group_by}
            ORDER BY {inner_order} DESC
            LIMIT ?
        ) x JOIN tuples t ON t.id = x.any_tuple {joins}
        ORDER BY {outer_order} DESC""", (start, *params, limit)).fetchall()

    total = conn.execute(f"""
        SELECT count(*) AS n, coalesce(sum(s), 0) AS bytes_sent, coalesce(sum(r), 0) AS bytes_recv FROM (
            SELECT sum(a.bytes_sent) AS s, sum(a.bytes_recv) AS r
            FROM agg_{level} a JOIN tuples t ON t.id = a.tuple_id
            WHERE a.bucket_start >= ?{where} GROUP BY {group_by})""", (start, *params)).fetchone()

    items = []
    for r in rows:
        if group == "tuple":
            item = _tuple_item(r)
        else:
            item = {"interface": r["interface"], group: {"addr": r["addr"] or None, "host": r["host"]}}
        item["bytes_sent"] = r["bytes_sent"]
        item["bytes_recv"] = r["bytes_recv"]
        items.append(item)

    shown_sent = sum(i["bytes_sent"] for i in items)
    shown_recv = sum(i["bytes_recv"] for i in items)
    return {
        "items": items,
        "shown": len(items),
        "count": total["n"],
        "others": {
            "count": total["n"] - len(items),
            "bytes_sent": total["bytes_sent"] - shown_sent,
            "bytes_recv": total["bytes_recv"] - shown_recv,
        },
    }


def _coverage(conn, level, start):
    """{bucket_start: samples} of the interface totals: buckets with any data."""
    return dict(conn.execute(f"""SELECT bucket_start, sum(samples) FROM totals_{level}
                                 WHERE bucket_start >= ? GROUP BY bucket_start""", (start,)))


def totals(conn, level, start, where="", params=()):
    """Bytes in the window (of the matching connections, with a filter) and
    how many seconds of it the collector saw."""
    row = conn.execute(f"""
        SELECT coalesce(sum(samples), 0) FROM totals_{level} WHERE bucket_start >= ?""", (start,)).fetchone()
    covered = row[0] * RESOLUTION
    if where:
        sent, recv = conn.execute(f"""
            SELECT coalesce(sum(a.bytes_sent), 0), coalesce(sum(a.bytes_recv), 0)
            FROM agg_{level} a JOIN tuples t ON t.id = a.tuple_id
            WHERE a.bucket_start >= ?{where}""", (start, *params)).fetchone()
    else:
        sent, recv = conn.execute(f"""
            SELECT coalesce(sum(bytes_sent), 0), coalesce(sum(bytes_recv), 0)
            FROM totals_{level} WHERE bucket_start >= ?""", (start,)).fetchone()
    return {"bytes_sent": sent, "bytes_recv": recv, "covered_seconds": covered}


def _raw_rows(recent_path, start):
    """Per-sample interface totals, from the collector's database on tmpfs."""
    if not recent_path or not os.path.exists(recent_path):
        return []
    try:
        conn = sqlite3.connect(f"file:{recent_path}?mode=ro", uri=True, timeout=10)
        try:
            return conn.execute("""
                SELECT ts, sum(bytes_sent), sum(bytes_recv), count(*)
                FROM totals_raw WHERE ts >= ? GROUP BY ts ORDER BY ts""", (start,)).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return []


def chart_samples(recent_path, start):
    """One point per iftop sample, with a break where samples are missing."""
    points = []
    prev = None
    for ts, s, r, n in _raw_rows(recent_path, start):
        if prev is not None and ts - prev > 2 * RESOLUTION + 1:
            points.append([prev + RESOLUTION, None, None])
        points.append([ts, s / RESOLUTION / n, r / RESOLUTION / n])
        prev = ts
    return {"step": RESOLUTION, "from": start, "points": points}


def _points(sums, coverage, start, end, step):
    """Rates per step from {bucket: [sent, recv]} and {bucket: samples}.

    A step without any samples (collector not running) is None, not zero.
    """
    by_step = {}
    for b, n in coverage.items():
        acc = by_step.setdefault(buckets.bucket_start(b, step), [0, 0, 0])
        acc[2] += n
    for b, (s, r) in sums.items():
        acc = by_step.setdefault(buckets.bucket_start(b, step), [0, 0, 0])
        acc[0] += s
        acc[1] += r
    points = []
    for t in buckets.steps(start, end, step):
        s, r, n = by_step.get(t, (0, 0, 0))
        if n:
            seconds = n * RESOLUTION
            points.append([t, s / seconds, r / seconds])
        else:
            points.append([t, None, None])
    return points


def chart(conn, level, step, start, end, recent_path=None):
    """Average rates of the interface per step."""
    if level == "raw" and step == RESOLUTION:
        return chart_samples(recent_path, start)
    if level == "raw":
        rows = _raw_rows(recent_path, start)
    else:
        rows = conn.execute(f"""
            SELECT bucket_start, sum(bytes_sent), sum(bytes_recv), sum(samples)
            FROM totals_{level} WHERE bucket_start >= ? GROUP BY bucket_start""", (start,)).fetchall()
    sums = {b: (s, r) for b, s, r, n in rows}
    coverage = {b: n for b, s, r, n in rows}
    return {"step": step, "from": start, "points": _points(sums, coverage, start, end, step)}


def chart_filtered(conn, level, step, start, end, where, params):
    """Average rates of the matching connections per step."""
    sums = {b: (s, r) for b, s, r in conn.execute(f"""
        SELECT a.bucket_start, sum(a.bytes_sent), sum(a.bytes_recv)
        FROM agg_{level} a JOIN tuples t ON t.id = a.tuple_id
        WHERE a.bucket_start >= ?{where} GROUP BY a.bucket_start""", (start, *params))}
    return {"step": step, "from": start, "points": _points(sums, _coverage(conn, level, start), start, end, step)}


def series(conn, level, start, end, group, items, where="", params=()):
    """Adds each item's total rate per bucket of `level` (the small charts).

    Returns the bucket times shared by all items.
    """
    step = LEVEL_STEP[level]
    times = buckets.steps(start, end, step)
    if not items:
        return times
    keys = [i["id"] if group == "tuple" else i[group]["addr"] for i in items]
    key = _group_key(group)
    marks = ", ".join("?" * len(keys))
    per_item = {}
    for b, k, s, r in conn.execute(f"""
            SELECT a.bucket_start, {key}, sum(a.bytes_sent), sum(a.bytes_recv)
            FROM agg_{level} a JOIN tuples t ON t.id = a.tuple_id
            WHERE a.bucket_start >= ?{where} AND {key} IN ({marks})
            GROUP BY a.bucket_start, {key}""", (start, *params, *keys)):
        per_item.setdefault(k, {})[b] = (s, r)
    coverage = _coverage(conn, level, start)
    for item, k in zip(items, keys):
        points = _points(per_item.get(k, {}), coverage, start, end, step)
        item["series"] = [None if p[1] is None else p[1] + p[2] for p in points]
    return times


def query(conn, window, group="tuple", sort="total", limit=DEFAULT_LIMIT, now=None, filt=None,
          recent_path=None):
    now = int(now if now is not None else time.time())
    filt = filt or Filter()
    where, params = filt.sql(conn)
    length, top_level, chart_level, step = WINDOWS[window]
    start = _window_start(now, length, top_level)
    result = {"window": window, "group": group, "sort": sort, "now": now, "from": start, "to": now,
              "filter": filt.describe(conn)}
    result["totals"] = totals(conn, top_level, start, where, params)
    result.update(top(conn, top_level, start, group, sort, limit, where, params))
    if filt.active:
        chart_level, step = FILTERED_CHART[window]
        result["chart"] = chart_filtered(conn, chart_level, step, start, now, where, params)
    else:
        result["chart"] = chart(conn, chart_level, step, start, now, recent_path)
    result["series"] = {"step": LEVEL_STEP[top_level],
                        "times": series(conn, top_level, start, now, group, result["items"], where, params)}
    return result


@bp.route("/history")
@login_required
def page():
    return render_template("history.html", windows=list(WINDOWS))


def _filter_from_request(args):
    tuple_id = args.get("tuple", "").strip()
    if tuple_id and not tuple_id.isdigit():
        raise FilterError(f"invalid connection id: {tuple_id}")
    src, dst = (args.get(k, "").strip()[:100] or None for k in ("src", "dst"))
    return Filter(int(tuple_id) if tuple_id else None, src, dst,
                  parse_port(args.get("port")), parse_proto(args.get("proto")))


@bp.route("/api/agg")
@login_required
def api_agg():
    window = request.args.get("window", "hour")
    if window not in WINDOWS:
        window = "hour"
    group = request.args.get("group", "tuple")
    if group not in GROUPS:
        group = "tuple"
    sort = request.args.get("sort", "total")
    if sort not in SORTS:
        sort = "total"
    try:
        limit = max(1, min(MAX_LIMIT, int(request.args.get("limit", DEFAULT_LIMIT))))
    except ValueError:
        limit = DEFAULT_LIMIT
    try:
        filt = _filter_from_request(request.args)
    except FilterError as e:
        return jsonify(error=str(e)), 400
    cfg = current_app.config["IFTOP_CONFIG"]
    try:
        conn = connect(cfg.db_path)
    except NoHistory:
        return jsonify(error="no history yet: is the collector running?"), 503
    try:
        result = query(conn, window, group, sort, limit, filt=filt, recent_path=cfg.recent_path)
    finally:
        conn.close()
    response = jsonify(result)
    response.headers["Cache-Control"] = "no-store"
    return response


@bp.route("/vendor/chart.min.js")
@login_required
def chart_js():
    path = current_app.config["IFTOP_CONFIG"].web.chart_js
    if not os.path.isfile(path):
        abort(404)
    return send_file(path, mimetype="text/javascript", max_age=86400)
