"""History: the page, /api/agg, and Chart.js from the libjs-chart.js package.

Windows are rolling ("the last hour") and made of whole buckets: a window
covers every bucket that overlaps it, so it is at least as long as asked
and at most one bucket longer. The response gives the exact range, and the
chart covers the same range as the top list.
"""

import os
import sqlite3
import time

from flask import Blueprint, abort, current_app, jsonify, render_template, request, send_file
from flask_login import login_required

from common import buckets
from common.schema import PORT_FOLDED, PORT_NONE
from .live import PROTOCOLS

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


def top(conn, level, start, group, sort, limit):
    """Top items in agg_<level> from bucket `start` on, plus the sum of the rest."""
    inner_order = SORTS[sort].replace("{p}bytes_sent", "sum(a.bytes_sent)").replace("{p}bytes_recv", "sum(a.bytes_recv)")
    outer_order = SORTS[sort].format(p="x.")
    if group == "tuple":
        select = """t.interface, t.proto, t.src_addr, t.src_port, t.dst_addr, t.dst_port,
                    hs.name AS src_host, hd.name AS dst_host, ss.name AS src_service, sd.name AS dst_service"""
        joins = """LEFT JOIN hosts hs ON hs.interface = t.interface AND hs.addr = t.src_addr
                   LEFT JOIN hosts hd ON hd.interface = t.interface AND hd.addr = t.dst_addr
                   LEFT JOIN services ss ON ss.proto = t.proto AND ss.port = t.src_port
                   LEFT JOIN services sd ON sd.proto = t.proto AND sd.port = t.dst_port"""
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
            WHERE a.bucket_start >= ?
            GROUP BY {group_by}
            ORDER BY {inner_order} DESC
            LIMIT ?
        ) x JOIN tuples t ON t.id = x.any_tuple {joins}
        ORDER BY {outer_order} DESC""", (start, limit)).fetchall()

    total = conn.execute(f"""
        SELECT count(*) AS n, coalesce(sum(s), 0) AS bytes_sent, coalesce(sum(r), 0) AS bytes_recv FROM (
            SELECT sum(a.bytes_sent) AS s, sum(a.bytes_recv) AS r
            FROM agg_{level} a JOIN tuples t ON t.id = a.tuple_id
            WHERE a.bucket_start >= ? GROUP BY {group_by})""", (start,)).fetchone()

    items = []
    for r in rows:
        item = {"interface": r["interface"], "bytes_sent": r["bytes_sent"], "bytes_recv": r["bytes_recv"]}
        if group == "tuple":
            item["proto"] = PROTOCOLS.get(r["proto"], str(r["proto"]))
            item["protocol"] = r["proto"]
            item["src"] = _endpoint(r["src_addr"], r["src_port"], r["src_host"], r["src_service"])
            item["dst"] = _endpoint(r["dst_addr"], r["dst_port"], r["dst_host"], r["dst_service"])
        else:
            item[group] = {"addr": r["addr"] or None, "host": r["host"]}
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


def totals(conn, level, start):
    row = conn.execute(f"""
        SELECT coalesce(sum(bytes_sent), 0), coalesce(sum(bytes_recv), 0), coalesce(sum(samples), 0)
        FROM totals_{level} WHERE bucket_start >= ?""", (start,)).fetchone()
    return {"bytes_sent": row[0], "bytes_recv": row[1], "covered_seconds": row[2] * RESOLUTION}


def _raw_rows(conn, start):
    try:
        return conn.execute("""
            SELECT ts, sum(bytes_sent), sum(bytes_recv), count(*)
            FROM totals_raw WHERE ts >= ? GROUP BY ts ORDER BY ts""", (start,)).fetchall()
    except sqlite3.OperationalError:
        return []       # schema version 1: the collector has not been upgraded yet


def chart_samples(conn, start):
    """One point per iftop sample, with a break where samples are missing."""
    points = []
    prev = None
    for ts, s, r, n in _raw_rows(conn, start):
        if prev is not None and ts - prev > 2 * RESOLUTION + 1:
            points.append([prev + RESOLUTION, None, None])
        points.append([ts, s / RESOLUTION / n, r / RESOLUTION / n])
        prev = ts
    return {"step": RESOLUTION, "from": start, "points": points}


def chart(conn, level, step, start, end):
    """Average rates per step; None where there is no data at all."""
    if level == "raw" and step == RESOLUTION:
        return chart_samples(conn, start)
    if level == "raw":
        rows = _raw_rows(conn, start)
    else:
        rows = conn.execute(f"""
            SELECT bucket_start, sum(bytes_sent), sum(bytes_recv), sum(samples)
            FROM totals_{level} WHERE bucket_start >= ? GROUP BY bucket_start""", (start,)).fetchall()
    sums = {}
    for b, s, r, n in rows:
        acc = sums.setdefault(buckets.bucket_start(b, step), [0, 0, 0])
        acc[0] += s
        acc[1] += r
        acc[2] += n
    points = []
    for t in buckets.steps(start, end, step):
        s, r, n = sums.get(t, (0, 0, 0))
        if n:
            seconds = n * RESOLUTION
            points.append([t, s / seconds, r / seconds])
        else:
            points.append([t, None, None])
    return {"step": step, "from": start, "points": points}


def query(conn, window, group="tuple", sort="total", limit=DEFAULT_LIMIT, now=None):
    now = int(now if now is not None else time.time())
    length, top_level, chart_level, step = WINDOWS[window]
    start = _window_start(now, length, top_level)
    result = {"window": window, "group": group, "sort": sort, "now": now, "from": start, "to": now}
    result["totals"] = totals(conn, top_level, start)
    result.update(top(conn, top_level, start, group, sort, limit))
    result["chart"] = chart(conn, chart_level, step, start, now)
    return result


@bp.route("/history")
@login_required
def page():
    return render_template("history.html", windows=list(WINDOWS))


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
        conn = connect(current_app.config["IFTOP_CONFIG"].db_path)
    except NoHistory:
        return jsonify(error="no history yet: is the collector running?"), 503
    try:
        result = query(conn, window, group, sort, limit)
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
