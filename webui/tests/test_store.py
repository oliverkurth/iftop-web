import os
import sqlite3

import pytest

from collector.store import Store, StoreError, open_db
from common import buckets
from common.schema import PORT_FOLDED, PORT_NONE, SCHEMA_VERSION

T0 = 1_790_000_040   # a minute boundary
assert T0 % 60 == 0


def conn_(src_port, dst_port, sent, recv, *, src="10.0.0.1", dst="10.0.0.2", proto=6,
          src_host=None, dst_host="server", src_service=None, dst_service=None):
    return {
        "src": {"addr": src, "host": src_host, "port": src_port,
                "service": src_service or (None if src_port is None else str(src_port))},
        "dst": {"addr": dst, "host": dst_host, "port": dst_port,
                "service": dst_service or (None if dst_port is None else str(dst_port))},
        "protocol": proto,
        "sent_rate": [sent, sent, sent], "recv_rate": [recv, recv, recv],
        "total_sent": 0, "total_recv": 0,
    }


def snap(ts, conns, interface="eth0"):
    sent = sum(c["sent_rate"][0] for c in conns)
    recv = sum(c["recv_rate"][0] for c in conns)
    return {"timestamp": ts, "interface": interface, "connections": conns,
            "totals": {"sent_rate": [sent, sent, sent], "recv_rate": [recv, recv, recv]}}


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "iftop.db")


@pytest.fixture
def store(db_path):
    s = Store(db_path).open()
    yield s
    if s.conn is not None:
        s.close()


def rows(path, sql, *params):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def sums(path, level):
    return rows(path, f"SELECT coalesce(sum(bytes_sent), 0), coalesce(sum(bytes_recv), 0), "
                      f"coalesce(sum(samples), 0) FROM agg_{level}")[0]


def https(sent=100, recv=1000, client_port=40001, **kw):
    return conn_(client_port, 443, sent, recv, dst_service="https", **kw)


# ---- counting ----

def test_bytes_are_rate_times_two(store, db_path):
    store.add(snap(T0 + 2, [https(100, 1000)]))
    store.add(snap(T0 + 4, [https(50, 10)]))
    store.flush()
    assert rows(db_path, "SELECT bucket_start, bytes_sent, bytes_recv, samples FROM agg_1m") == \
        [(T0, 300, 2020, 2)]
    assert rows(db_path, "SELECT interface, bucket_start, bytes_sent, bytes_recv, samples FROM totals_1m") == \
        [("eth0", T0, 300, 2020, 2)]


def test_samples_land_in_their_minute(store, db_path):
    store.add(snap(T0 + 58, [https()]))
    store.add(snap(T0 + 60, [https()]))
    store.add(snap(T0 + 119, [https()]))
    store.flush()
    assert rows(db_path, "SELECT bucket_start, samples FROM agg_1m ORDER BY 1") == [(T0, 1), (T0 + 60, 2)]


def test_hours_and_days_add_up_to_minutes(store, db_path):
    # Three hours of samples, flushed in several batches.
    for i, t in enumerate(range(T0, T0 + 3 * 3600, 2)):
        store.add(snap(t, [https(i % 7, i % 11), conn_(53000, 53, 3, 5, proto=17, dst_service="domain")]))
        if i % 500 == 0:
            store.flush()
    store.flush()
    assert sums(db_path, "1m") == sums(db_path, "1h") == sums(db_path, "1d")
    hours = {buckets.hour_start(t) for t in range(T0, T0 + 3 * 3600, 2)}
    assert {r[0] for r in rows(db_path, "SELECT DISTINCT bucket_start FROM agg_1h")} == hours
    for level in ("1m", "1h", "1d"):
        assert rows(db_path, f"SELECT sum(samples) FROM totals_{level}") == [(3 * 3600 // 2,)]


# ---- keys and folding ----

def test_client_ports_are_folded(store, db_path):
    store.add(snap(T0, [https(10, 10, 40001), https(20, 20, 40002), https(30, 30, 40003)]))
    store.add(snap(T0 + 2, [https(1, 1, 40004)]))
    store.flush()
    assert rows(db_path, "SELECT interface, proto, src_addr, src_port, dst_addr, dst_port FROM tuples") == \
        [("eth0", 6, "10.0.0.1", PORT_FOLDED, "10.0.0.2", 443)]
    # Three connections folded into one row count as one sample.
    assert rows(db_path, "SELECT bytes_sent, samples FROM agg_1m") == [(122, 2)]


def test_server_ports_and_no_ports(db_path):
    store = Store(db_path, server_ports=[51820]).open()
    store.add(snap(T0, [
        conn_(51820, 50000, 1, 1, proto=17),          # configured server port
        conn_(2000, 3000, 1, 1),                       # both below the threshold
        conn_(None, None, 1, 1, proto=1),              # ICMP
    ]))
    store.close()
    keys = rows(db_path, "SELECT proto, src_port, dst_port FROM tuples ORDER BY proto, src_port")
    assert keys == [(1, PORT_NONE, PORT_NONE), (6, 2000, 3000), (17, 51820, PORT_FOLDED)]


def test_interfaces_are_kept_apart(store, db_path):
    store.add(snap(T0, [https()], interface="eth0"))
    store.add(snap(T0, [https()], interface="wg0"))
    store.flush()
    assert rows(db_path, "SELECT interface FROM tuples ORDER BY 1") == [("eth0",), ("wg0",)]
    assert rows(db_path, "SELECT interface FROM totals_1m ORDER BY 1") == [("eth0",), ("wg0",)]


# ---- restart safety ----

def test_crash_loses_only_unwritten_samples_and_replay_counts_once(db_path):
    samples = [snap(T0 + 2 * i, [https(10, 20)]) for i in range(10)]

    a = Store(db_path).open()
    for s in samples[:5]:
        a.add(s)
    a.flush()
    for s in samples[5:]:
        a.add(s)
    a.conn.close()      # crash: the last five samples were never written
    assert sums(db_path, "1m") == (100, 200, 5)

    b = Store(db_path).open()
    added = [b.add(s) for s in samples]
    b.close()
    assert added == [False] * 5 + [True] * 5
    assert sums(db_path, "1m") == sums(db_path, "1h") == sums(db_path, "1d") == (200, 400, 10)

    # Replaying everything again after a clean stop changes nothing.
    c = Store(db_path).open()
    assert not any(c.add(s) for s in samples)
    c.close()
    assert sums(db_path, "1m") == (200, 400, 10)


class FailingConn:
    """Wraps a connection and fails the first write to the totals tables."""

    def __init__(self, conn):
        self.conn = conn
        self.failed = False

    def executemany(self, sql, params):
        if "totals_" in sql and not self.failed:
            self.failed = True
            raise sqlite3.OperationalError("disk I/O error")
        return self.conn.executemany(sql, params)

    def __getattr__(self, name):
        return getattr(self.conn, name)


def test_failed_write_is_rolled_back_and_retried(store, db_path):
    store.add(snap(T0, [https(10, 20)]))
    real = store.conn
    store.conn = FailingConn(real)
    with pytest.raises(sqlite3.OperationalError):
        store.flush()
    assert sums(db_path, "1m") == (0, 0, 0)
    assert store.tuple_ids == {}            # ids from the rolled-back transaction are not kept
    store.flush()
    store.conn = real
    assert sums(db_path, "1m") == (20, 40, 1)
    assert rows(db_path, "SELECT count(*) FROM tuples") == [(1,)]


def test_flush_interval(db_path):
    now = [1000.0]
    store = Store(db_path, flush_interval=10, clock=lambda: now[0]).open()
    store.add(snap(T0, [https()]))
    store.maybe_flush()          # the first call also purges, but writes nothing yet
    assert sums(db_path, "1m")[2] == 0
    now[0] += 10
    store.maybe_flush()
    assert sums(db_path, "1m")[2] == 1
    store.close()


# ---- names, services, retention, sessions ----

def test_host_names(store, db_path):
    def name():
        return rows(db_path, "SELECT name FROM hosts WHERE addr = '10.0.0.1'")[0][0]

    store.add(snap(T0, [https(src_host=None)]))
    store.flush()
    assert name() is None
    store.add(snap(T0 + 2, [https(src_host="laptop")]))
    store.flush()
    assert name() == "laptop"
    store.add(snap(T0 + 4, [https(src_host=None)]))      # pending again: keep the name
    store.flush()
    assert name() == "laptop"
    store.add(snap(T0 + 6, [https(src_host="laptop.lan")]))
    store.flush()
    assert name() == "laptop.lan"
    assert rows(db_path, "SELECT last_seen FROM hosts WHERE addr = '10.0.0.1'") == [(T0 + 6,)]


def test_only_known_services_are_stored(store, db_path):
    store.add(snap(T0, [https(), conn_(41000, 8123, 1, 1)]))
    store.flush()
    assert rows(db_path, "SELECT proto, port, name FROM services") == [(6, 443, "https")]


def test_purge(db_path):
    day = buckets.DAY
    now = T0 + 400 * day
    store = Store(db_path, minute_days=7, hour_days=365, clock=lambda: now).open()
    for t in (now - 366 * day, now - 364 * day, now - 8 * day, now - 6 * day):
        store.add(snap(t, [https()]))
    store.flush()
    store.purge()
    store.close()
    assert [r[0] for r in rows(db_path, "SELECT bucket_start FROM agg_1m ORDER BY 1")] == \
        [now - 6 * day - (now - 6 * day) % 60]
    assert len(rows(db_path, "SELECT * FROM agg_1h")) == 3
    assert len(rows(db_path, "SELECT * FROM totals_1h")) == 3
    assert len(rows(db_path, "SELECT * FROM agg_1d")) == 4     # days are kept for ever


def test_sessions(db_path):
    now = [5000.0]
    store = Store(db_path, clock=lambda: now[0]).open()
    store.add(snap(T0, [https()]))
    now[0] += 30
    store.flush()
    assert rows(db_path, "SELECT interface, started, last_heartbeat, stopped FROM collector_sessions") == \
        [("eth0", 5000, 5030, None)]
    now[0] += 30
    store.close()
    assert rows(db_path, "SELECT last_heartbeat, stopped FROM collector_sessions") == [(5060, 5060)]


# ---- database file ----

def test_database_is_readable_by_others_and_not_wal(store, db_path):
    assert os.stat(db_path).st_mode & 0o777 == 0o644
    assert rows(db_path, "PRAGMA journal_mode") == [("delete",)]
    assert rows(db_path, "PRAGMA user_version") == [(SCHEMA_VERSION,)]


def test_newer_schema_is_refused(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    conn.close()
    with pytest.raises(StoreError, match="newer"):
        open_db(db_path)


def test_upgrade_from_version_1(db_path):
    store = Store(db_path).open()
    store.add(snap(T0, [https()]))
    store.close()
    conn = sqlite3.connect(db_path)
    conn.execute("DROP TABLE totals_raw")
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()

    store = Store(db_path).open()
    store.add(snap(T0 + 2, [https()]))
    store.close()
    assert rows(db_path, "PRAGMA user_version") == [(SCHEMA_VERSION,)]
    assert rows(db_path, "SELECT ts FROM totals_raw") == [(T0 + 2,)]
    assert sums(db_path, "1m")[2] == 2         # the old data is kept


def test_samples_are_kept_for_an_hour(store, db_path):
    for t in range(T0, T0 + 7200 + 1, 600):
        store.add(snap(t, [https(10, 20)]))
    store.flush()
    assert [r[0] for r in rows(db_path, "SELECT ts FROM totals_raw ORDER BY ts")] == \
        list(range(T0 + 3600, T0 + 7200 + 1, 600))
    assert rows(db_path, "SELECT bytes_sent, bytes_recv FROM totals_raw LIMIT 1") == [(20, 40)]
