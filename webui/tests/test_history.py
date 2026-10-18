import os
import sqlite3
import time

import pytest

from collector.store import Store
from common import buckets
from test_store import conn_, https, snap
from web import history

NOW = 1_790_003_630        # 30 s into a minute


@pytest.fixture
def filled(cfg):
    """An hour of samples ending at NOW, with a 10-minute gap in the middle."""
    store = Store.from_config(cfg).open()
    for t in range(NOW - 3600, NOW + 1, 2):
        if NOW - 1800 <= t < NOW - 1200:
            continue                                   # collector not running
        store.add(snap(t, [
            https(100, 1000, src_host="laptop"),
            https(10, 20, src="10.0.0.5", dst="10.0.0.9", dst_host=None),
            conn_(41000, 53, 5, 5, proto=17, dst="10.0.0.53", dst_host="dns", dst_service="domain"),
        ]))
    store.close()
    return cfg


def q(cfg, window, **kw):
    conn = history.connect(cfg.db_path)
    try:
        return history.query(conn, window, now=NOW, recent_path=cfg.recent_path, **kw)
    finally:
        conn.close()


def test_hour_window_top_list(filled):
    data = q(filled, "hour")
    assert data["from"] == buckets.minute_start(NOW - 3600)
    assert data["count"] == 3 and data["shown"] == 3
    first = data["items"][0]
    assert first["src"] == {"addr": "10.0.0.1", "host": "laptop", "port": None, "service": None, "folded": True}
    assert first["dst"] == {"addr": "10.0.0.2", "host": "server", "port": 443, "service": "https"}
    assert first["proto"] == "tcp"
    samples = len([t for t in range(NOW - 3600, NOW + 1, 2) if not NOW - 1800 <= t < NOW - 1200])
    assert first["bytes_sent"] == 200 * samples and first["bytes_recv"] == 2000 * samples
    assert data["totals"]["covered_seconds"] == 2 * samples
    assert data["totals"]["bytes_sent"] == 230 * samples
    assert data["others"] == {"count": 0, "bytes_sent": 0, "bytes_recv": 0}


def test_limit_and_others(filled):
    data = q(filled, "hour", limit=1)
    all_ = q(filled, "hour")
    assert data["shown"] == 1 and data["others"]["count"] == 2
    for k in ("bytes_sent", "bytes_recv"):
        assert data["items"][0][k] + data["others"][k] == sum(i[k] for i in all_["items"])


@pytest.mark.parametrize("sort,key", [
    ("total", lambda i: i["bytes_sent"] + i["bytes_recv"]),
    ("sent", lambda i: i["bytes_sent"]),
    ("recv", lambda i: i["bytes_recv"]),
])
def test_sort(filled, sort, key):
    items = q(filled, "hour", sort=sort)["items"]
    values = [key(i) for i in items]
    assert values == sorted(values, reverse=True)


def test_group_by_host(filled):
    data = q(filled, "hour", group="dst")
    assert [i["dst"] for i in data["items"]] == [
        {"addr": "10.0.0.2", "host": "server"},
        {"addr": "10.0.0.9", "host": None},
        {"addr": "10.0.0.53", "host": "dns"},
    ]
    data = q(filled, "hour", group="src")
    # 10.0.0.1 has two connections (https and dns); they are summed.
    assert data["count"] == 2
    assert data["items"][0]["src"] == {"addr": "10.0.0.1", "host": "laptop"}
    assert data["items"][0]["bytes_sent"] == q(filled, "hour")["items"][0]["bytes_sent"] + \
        q(filled, "hour")["items"][2]["bytes_sent"]


def test_chart_has_gaps_where_there_was_no_data(filled):
    chart = q(filled, "hour")["chart"]
    assert chart["step"] == 60
    points = {p[0]: p for p in chart["points"]}
    gap = [t for t in points if NOW - 1800 <= t and t + 60 <= NOW - 1200]
    assert gap and all(points[t][1] is None for t in gap)
    # Rates are bytes per covered second: a full minute averages to the sample rate.
    full = buckets.minute_start(NOW - 2400)
    assert points[full][1] == 115 and points[full][2] == 1025


def test_short_windows_are_whole_minutes(filled):
    data = q(filled, "minute")
    assert data["from"] == buckets.minute_start(NOW - 60)
    assert 60 <= data["to"] - data["from"] < 120


@pytest.mark.parametrize("window", list(history.WINDOWS))
def test_all_windows(filled, window):
    data = q(filled, window)
    assert data["window"] == window
    assert data["count"] == 3
    assert data["chart"]["points"]
    assert data["from"] <= NOW - history.WINDOWS[window][0]


# ---- API and page ----

def test_api_requires_login(client):
    assert client.get("/api/agg").status_code == 401
    assert client.get("/history").status_code == 302


def test_api_without_history(logged_in):
    assert logged_in.get("/api/agg").status_code == 503


def test_api(logged_in, cfg):
    store = Store.from_config(cfg).open()
    now = int(time.time())
    for t in range(now - 120, now, 2):
        store.add(snap(t, [https(10, 20)]))
    store.close()
    resp = logged_in.get("/api/agg?window=5min&group=tuple&sort=recv&limit=5")
    assert resp.status_code == 200
    assert resp.headers["Cache-Control"] == "no-store"
    data = resp.get_json()
    assert data["window"] == "5min" and data["sort"] == "recv"
    assert data["items"][0]["bytes_recv"] == 40 * 60
    # Unknown values fall back to the defaults.
    data = logged_in.get("/api/agg?window=decade&group=x&sort=y&limit=z").get_json()
    assert (data["window"], data["group"], data["sort"]) == ("hour", "tuple", "total")


def test_history_page(logged_in):
    page = logged_in.get("/history").get_data(as_text=True)
    assert 'data-api="/api/agg"' in page
    assert "/vendor/chart.min.js" in page and "history.js" in page and "format.js" in page


def test_chart_js(logged_in, cfg, tmp_path):
    cfg.web.chart_js = str(tmp_path / "missing.js")
    assert logged_in.get("/vendor/chart.min.js").status_code == 404
    path = tmp_path / "chart.min.js"
    path.write_text("window.Chart = function () {};")
    cfg.web.chart_js = str(path)
    resp = logged_in.get("/vendor/chart.min.js")
    assert resp.status_code == 200
    assert resp.mimetype == "text/javascript"


def test_reading_while_the_collector_writes(logged_in, cfg):
    store = Store.from_config(cfg).open()
    now = int(time.time())
    store.add(snap(now - 10, [https()]))
    store.flush()
    assert logged_in.get("/api/agg?window=minute").get_json()["count"] == 1
    store.add(snap(now - 8, [https(src="10.0.0.7")]))
    store.flush()
    assert logged_in.get("/api/agg?window=minute").get_json()["count"] == 2
    store.close()


@pytest.mark.parametrize("window", list(history.WINDOWS))
def test_chart_covers_the_same_range_as_the_top_list(filled, window):
    data = q(filled, window)
    chart = data["chart"]
    assert chart["from"] == data["from"]
    times = [p[0] for p in chart["points"]]
    assert times and min(times) >= data["from"] and max(times) <= NOW


def test_chart_ranges_grow_with_the_window(filled):
    spans = [NOW - q(filled, w)["from"] for w in history.WINDOWS]
    assert spans == sorted(spans) and len(set(spans)) == len(spans)


def test_minute_chart_has_a_point_per_sample(cfg):
    store = Store.from_config(cfg).open()
    times = [NOW - 80 + 2 * i for i in range(10)] + [NOW - 30 + 2 * i for i in range(10)]
    for t in times:
        store.add(snap(t, [https(100, 1000)]))
    store.close()
    chart = q(cfg, "minute")["chart"]
    assert chart["step"] == 2
    data_points = [p for p in chart["points"] if p[1] is not None]
    assert [p[0] for p in data_points] == [t for t in times if t >= chart["from"]]
    assert all(p[1:] == [100, 1000] for p in data_points)
    # The 32 s without samples is a break in the line.
    gaps = [p for p in chart["points"] if p[1] is None]
    assert len(gaps) == 1 and NOW - 62 < gaps[0][0] < NOW - 30


def test_5min_chart_has_10_second_steps(cfg):
    store = Store.from_config(cfg).open()
    for t in range(NOW - 300, NOW + 1, 2):
        store.add(snap(t, [https(100, 1000)]))
    store.close()
    chart = q(cfg, "5min")["chart"]
    assert chart["step"] == 10
    assert all(b - a == 10 for a, b in zip([p[0] for p in chart["points"]], [p[0] for p in chart["points"]][1:]))
    full = [p for p in chart["points"] if p[1] is not None]
    assert full and all(p[1:] == [100.0, 1000.0] for p in full)


def test_without_the_samples_database(cfg):
    """After a reboot /run is empty: the short charts are empty until new samples arrive."""
    store = Store.from_config(cfg).open()
    store.add(snap(NOW - 10, [https()]))
    store.close()
    os.unlink(cfg.recent_path)
    data = q(cfg, "minute")
    assert data["chart"]["points"] == [] and data["count"] == 1


def test_samples_come_from_the_database_in_rundir(cfg):
    store = Store.from_config(cfg).open()
    store.add(snap(NOW - 10, [https(100, 1000)]))
    store.close()
    assert os.path.dirname(cfg.recent_path) == cfg.rundir
    points = [p for p in q(cfg, "minute")["chart"]["points"] if p[1] is not None]
    assert points == [[NOW - 10, 100.0, 1000.0]]


# ---- stage 3: filters, detail views, small charts ----

def qf(cfg, window="hour", **filt):
    conn = history.connect(cfg.db_path)
    try:
        return history.query(conn, window, now=NOW, filt=history.Filter(**filt), recent_path=cfg.recent_path)
    finally:
        conn.close()


def tuple_id(cfg, dst):
    conn = history.connect(cfg.db_path)
    try:
        return conn.execute("SELECT id FROM tuples WHERE dst_addr = ?", (dst,)).fetchone()[0]
    finally:
        conn.close()


def test_no_filter_is_described_as_empty(filled):
    assert q(filled, "hour")["filter"] == {"tuple": None, "src": None, "dst": None, "port": None, "proto": None}


def test_filter_by_connection(filled):
    tid = tuple_id(filled, "10.0.0.2")
    all_ = q(filled, "hour")
    data = qf(filled, tuple_id=tid)
    assert data["count"] == 1 and data["items"][0]["id"] == tid
    desc = data["filter"]["tuple"]
    assert desc["src"]["host"] == "laptop" and desc["src"]["folded"]
    assert desc["dst"] == {"addr": "10.0.0.2", "host": "server", "port": 443, "service": "https"}
    # The summary counts only this connection, over the same covered time.
    item = all_["items"][0]
    assert data["totals"]["bytes_sent"] == item["bytes_sent"]
    assert data["totals"]["covered_seconds"] == all_["totals"]["covered_seconds"]
    # The chart is this connection's rate, from the minute buckets.
    chart = data["chart"]
    assert chart["step"] == 60
    full = buckets.minute_start(NOW - 2400)
    assert {p[0]: p for p in chart["points"]}[full][1:] == [100, 1000]


def test_unknown_connection(filled):
    data = qf(filled, tuple_id=99999)
    assert data["count"] == 0 and data["filter"]["tuple"] == {"id": 99999, "unknown": True}


def test_filter_by_host(filled):
    data = qf(filled, src="10.0.0.1")
    assert data["count"] == 2
    assert {i["dst"]["addr"] for i in data["items"]} == {"10.0.0.2", "10.0.0.53"}
    assert data["filter"]["src"] == {"addr": "10.0.0.1", "host": "laptop"}
    data = qf(filled, dst="10.0.0.53")
    assert [i["proto"] for i in data["items"]] == ["udp"]
    assert data["filter"]["dst"] == {"addr": "10.0.0.53", "host": "dns"}


@pytest.mark.parametrize("filt,dsts", [
    ({"port": 443}, {"10.0.0.2", "10.0.0.9"}),
    ({"port": "https"}, {"10.0.0.2", "10.0.0.9"}),
    ({"port": "domain"}, {"10.0.0.53"}),
    ({"port": 53, "proto": 6}, set()),
    ({"proto": 17}, {"10.0.0.53"}),
    ({"port": "no-such-service"}, set()),
    ({"src": "10.0.0.1", "port": 443}, {"10.0.0.2"}),
])
def test_filter_by_port_and_protocol(filled, filt, dsts):
    data = qf(filled, **filt)
    assert {i["dst"]["addr"] for i in data["items"]} == dsts
    assert data["count"] == len(dsts)


def test_filtered_chart_has_gaps_where_there_was_no_data(filled):
    chart = qf(filled, port=443)["chart"]
    points = {p[0]: p for p in chart["points"]}
    gap = [t for t in points if NOW - 1800 <= t and t + 60 <= NOW - 1200]
    assert gap and all(points[t][1] is None for t in gap)
    full = buckets.minute_start(NOW - 2400)
    assert points[full][1:] == [110, 1020]


def test_series_for_each_item(filled):
    data = q(filled, "hour")
    times = data["series"]["times"]
    assert data["series"]["step"] == 60 and times[0] == data["from"]
    for item in data["items"]:
        assert len(item["series"]) == len(times)
    by_time = dict(zip(times, data["items"][0]["series"]))
    assert by_time[buckets.minute_start(NOW - 2400)] == 1100        # sent + received rate
    gap = [t for t in times if NOW - 1800 <= t and t + 60 <= NOW - 1200]
    assert gap and all(by_time[t] is None for t in gap)


def test_series_for_hosts(filled):
    conn = history.connect(filled.db_path)
    try:
        hosts = history.query(conn, "hour", group="dst", now=NOW)
    finally:
        conn.close()
    assert [len(i["series"]) for i in hosts["items"]] == [len(hosts["series"]["times"])] * 3
    assert dict(zip(hosts["series"]["times"], hosts["items"][0]["series"]))[
        buckets.minute_start(NOW - 2400)] == 1100


def test_year_window(filled):
    data = q(filled, "year")
    assert data["chart"]["step"] == buckets.DAY
    assert NOW - data["from"] >= 365 * buckets.DAY
    assert data["count"] == 3


@pytest.mark.parametrize("query_string,message", [
    ("tuple=abc", "invalid connection id"),
    ("port=70000", "invalid port"),
    ("port=a%20b", "invalid service name"),
    ("proto=xyz", "unknown protocol"),
])
def test_api_rejects_bad_filters(logged_in, cfg, query_string, message):
    Store.from_config(cfg).open().close()
    resp = logged_in.get("/api/agg?" + query_string)
    assert resp.status_code == 400
    assert message in resp.get_json()["error"]


def test_api_filters(logged_in, cfg):
    store = Store.from_config(cfg).open()
    now = int(time.time())
    for t in range(now - 120, now, 2):
        store.add(snap(t, [https(10, 20), conn_(41000, 53, 1, 1, proto=17, dst="10.0.0.53", dst_service="domain")]))
    store.close()
    data = logged_in.get("/api/agg?window=5min&port=DOMAIN&proto=udp").get_json()
    assert data["count"] == 1 and data["filter"]["port"] == "domain" and data["filter"]["proto"] == "udp"
    tid = data["items"][0]["id"]
    data = logged_in.get(f"/api/agg?window=5min&tuple={tid}").get_json()
    assert data["filter"]["tuple"]["dst"]["service"] == "domain"


def test_pages_have_port_and_protocol_controls(logged_in):
    for path in ("/", "/history"):
        page = logged_in.get(path).get_data(as_text=True)
        assert 'id="port"' in page and 'id="proto"' in page
    page = logged_in.get("/history").get_data(as_text=True)
    assert 'data-window="year"' in page and 'id="filter-banner"' in page
