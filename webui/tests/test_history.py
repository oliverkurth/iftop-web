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
    store = Store(cfg.db_path).open()
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
        return history.query(conn, window, now=NOW, **kw)
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
    store = Store(cfg.db_path).open()
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
    data = logged_in.get("/api/agg?window=year&group=x&sort=y&limit=z").get_json()
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
    store = Store(cfg.db_path).open()
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
    store = Store(cfg.db_path).open()
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
    store = Store(cfg.db_path).open()
    for t in range(NOW - 300, NOW + 1, 2):
        store.add(snap(t, [https(100, 1000)]))
    store.close()
    chart = q(cfg, "5min")["chart"]
    assert chart["step"] == 10
    assert all(b - a == 10 for a, b in zip([p[0] for p in chart["points"]], [p[0] for p in chart["points"]][1:]))
    full = [p for p in chart["points"] if p[1] is not None]
    assert full and all(p[1:] == [100.0, 1000.0] for p in full)


def test_old_database_without_samples_table(cfg):
    """The web app keeps working until the collector has upgraded the database."""
    store = Store(cfg.db_path).open()
    store.add(snap(NOW - 10, [https()]))
    store.close()
    conn = sqlite3.connect(cfg.db_path)
    conn.execute("DROP TABLE totals_raw")
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()
    data = q(cfg, "minute")
    assert data["chart"]["points"] == [] and data["count"] == 1
