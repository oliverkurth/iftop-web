"""Aggregates iftop samples into per-minute, per-hour and per-day buckets.

Each iftop line covers exactly one 2-second history slot, and its first rate
is that slot's bytes divided by 2, so the bytes of a sample are rate[0] * 2.

Counts are summed in memory and written every flush_interval seconds (10
by default; longer on flash storage) in one transaction, together with the
timestamp of the newest sample. All writes add to existing rows, and samples
not newer than the stored timestamp are skipped, so a crash loses at most the
unwritten samples and a restart or a replay never counts anything twice.

The interface totals of every sample go to a separate small database on
tmpfs every RAW_FLUSH_INTERVAL seconds, for the short charts. Host names and
services are only written when they change, and a host's last_seen at most
every HOST_SEEN_INTERVAL seconds.
"""

import logging
import os
import sqlite3
import time

from common import buckets
from common.ports import client_side, is_known_service
from common.schema import MIGRATIONS, PORT_FOLDED, PORT_NONE, RAW_KEEP, RAW_TABLE, SCHEMA, SCHEMA_VERSION

log = logging.getLogger("iftop-collector")

RESOLUTION = 2
FLUSH_INTERVAL = 10
RAW_FLUSH_INTERVAL = 10
HOST_SEEN_INTERVAL = buckets.HOUR
PURGE_INTERVAL = buckets.DAY

_ADD_SQL = """
INSERT INTO {table} ({key}, bytes_sent, bytes_recv, samples) VALUES ({marks}, ?, ?, ?)
ON CONFLICT DO UPDATE SET bytes_sent = bytes_sent + excluded.bytes_sent,
                          bytes_recv = bytes_recv + excluded.bytes_recv,
                          samples = samples + excluded.samples
"""


def _add_sql(table, key_columns):
    return _ADD_SQL.format(table=table, key=", ".join(key_columns), marks=", ".join("?" * len(key_columns)))


class StoreError(Exception):
    pass


def open_db(path):
    """Opens (and creates or checks) the history database for writing."""
    new = not os.path.exists(path)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.execute("PRAGMA journal_mode = DELETE")
    conn.execute("PRAGMA foreign_keys = ON")
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version == 0:
        conn.executescript("BEGIN;" + SCHEMA + f"PRAGMA user_version = {SCHEMA_VERSION}; COMMIT;")
    elif version > SCHEMA_VERSION:
        conn.close()
        raise StoreError(f"{path} has schema version {version}, newer than this collector ({SCHEMA_VERSION})")
    else:
        for v in range(version + 1, SCHEMA_VERSION + 1):
            log.info("upgrading %s to schema version %d", path, v)
            conn.executescript("BEGIN;" + MIGRATIONS[v] + f"PRAGMA user_version = {v}; COMMIT;")
    if new:
        # The web app runs as another user and only reads.
        os.chmod(path, 0o644)
    return conn


def open_recent_db(path):
    """Opens (and creates) the database of per-sample totals on tmpfs."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    new = not os.path.exists(path)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.execute("PRAGMA journal_mode = DELETE")
    conn.executescript(RAW_TABLE)
    if new:
        os.chmod(path, 0o644)
    return conn


class Store:
    def __init__(self, path, server_ports=(), ephemeral_port_min=32768,
                 minute_days=7, hour_days=365, flush_interval=FLUSH_INTERVAL, clock=time.time,
                 recent_path=None):
        self.path = path
        self.recent_path = recent_path
        self.recent = None
        self.server_ports = frozenset(server_ports)
        self.ephemeral_port_min = ephemeral_port_min
        self.minute_days = minute_days
        self.hour_days = hour_days
        self.flush_interval = flush_interval
        self.clock = clock
        self.conn = None
        self.session_id = None
        self.tuple_ids = {}
        self.last_ts = {}
        # What is already in the database, so unchanged rows aren't rewritten.
        self.written_hosts = {}     # (interface, addr) -> (name, last_seen)
        self.written_services = {}  # (proto, port) -> name
        self._clear_pending()
        self.pending_raw = []       # (interface, ts, sent, recv) per sample
        self.next_flush = 0
        self.next_raw_flush = 0
        self.next_purge = 0
        self.skipped = 0

    @classmethod
    def from_config(cls, cfg, **kwargs):
        kwargs.setdefault("flush_interval", cfg.flush_interval)
        kwargs.setdefault("recent_path", cfg.recent_path)
        return cls(cfg.db_path, cfg.server_ports, cfg.ephemeral_port_min,
                   cfg.retention.minute_days, cfg.retention.hour_days, **kwargs)

    def _clear_pending(self):
        self.pending = {}          # (minute, tuple key) -> [sent, recv, samples]
        self.pending_totals = {}   # (interface, minute) -> [sent, recv, samples]
        self.pending_hosts = {}    # (interface, addr) -> (name, last_seen)
        self.pending_services = {}  # (proto, port) -> name
        self.interface = None

    # ---- lifecycle ----

    def open(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self.conn = open_db(self.path)
        self.last_ts = dict(self.conn.execute("SELECT interface, last_ts FROM ingest_state"))
        now = int(self.clock())
        self.session_id = self.conn.execute(
            "INSERT INTO collector_sessions (started, last_heartbeat) VALUES (?, ?)", (now, now)).lastrowid
        if self.recent_path:
            self.recent = open_recent_db(self.recent_path)
        self.next_flush = self.clock() + self.flush_interval
        self.next_raw_flush = self.clock() + RAW_FLUSH_INTERVAL
        self.next_purge = 0
        return self

    def close(self):
        if self.conn is None:
            return
        try:
            self.flush_raw()
            self.flush()
        finally:
            self.conn.execute("UPDATE collector_sessions SET last_heartbeat = ?, stopped = ? WHERE id = ?",
                              (int(self.clock()), int(self.clock()), self.session_id))
            self.conn.close()
            self.conn = None
            if self.recent is not None:
                self.recent.close()
                self.recent = None

    # ---- ingesting samples ----

    def _endpoint_port(self, end, folded):
        if folded:
            return PORT_FOLDED
        return end.get("port") or PORT_NONE

    def tuple_key(self, interface, c):
        side = client_side(c["src"], c["dst"], self.server_ports, self.ephemeral_port_min)
        return (interface, int(c.get("protocol") or 0),
                c["src"].get("addr") or "", self._endpoint_port(c["src"], side == "src"),
                c["dst"].get("addr") or "", self._endpoint_port(c["dst"], side == "dst"))

    def add(self, snap):
        """Adds one iftop snapshot. Returns False if it was skipped as not newer."""
        interface = snap.get("interface") or ""
        ts = snap["timestamp"]
        if ts <= self.last_ts.get(interface, -1):
            self.skipped += 1
            if self.skipped <= 3 or self.skipped % 1000 == 0:
                log.warning("skipping sample at %d: not newer than the last one stored (%d so far)",
                            ts, self.skipped)
            return False
        self.last_ts[interface] = ts
        self.interface = interface
        minute = buckets.minute_start(ts)

        seen = set()
        for c in snap["connections"]:
            key = self.tuple_key(interface, c)
            row = self.pending.setdefault((minute, key), [0, 0, 0])
            row[0] += int(c["sent_rate"][0]) * RESOLUTION
            row[1] += int(c["recv_rate"][0]) * RESOLUTION
            # Several connections can fold into one key; count the sample once.
            if key not in seen:
                row[2] += 1
                seen.add(key)
            for end in (c["src"], c["dst"]):
                addr = end.get("addr")
                if addr:
                    old = self.pending_hosts.get((interface, addr))
                    name = end.get("host") or (old[0] if old else None)
                    self.pending_hosts[(interface, addr)] = (name, ts)
                port = end.get("port")
                if port and is_known_service(port, end.get("service")):
                    self.pending_services[(int(c.get("protocol") or 0), port)] = end["service"]

        totals = snap.get("totals", {})
        row = self.pending_totals.setdefault((interface, minute), [0, 0, 0])
        row[0] += int(totals.get("sent_rate", [0])[0]) * RESOLUTION
        row[1] += int(totals.get("recv_rate", [0])[0]) * RESOLUTION
        row[2] += 1
        self.pending_raw.append((interface, ts, int(totals.get("sent_rate", [0])[0]) * RESOLUTION,
                                 int(totals.get("recv_rate", [0])[0]) * RESOLUTION))
        return True

    def maybe_flush(self):
        now = self.clock()
        if now >= self.next_raw_flush:
            self.flush_raw()
        if now >= self.next_flush:
            self.flush()
        if now >= self.next_purge:
            self.purge()
            self.next_purge = now + PURGE_INTERVAL

    # ---- writing ----

    def _tuple_id(self, key, new_ids):
        tid = self.tuple_ids.get(key) or new_ids.get(key)
        if tid is None:
            self.conn.execute("INSERT OR IGNORE INTO tuples (interface, proto, src_addr, src_port, dst_addr, dst_port)"
                              " VALUES (?, ?, ?, ?, ?, ?)", key)
            tid = self.conn.execute("SELECT id FROM tuples WHERE interface = ? AND proto = ? AND src_addr = ?"
                                    " AND src_port = ? AND dst_addr = ? AND dst_port = ?", key).fetchone()[0]
            new_ids[key] = tid
        return tid

    @staticmethod
    def _roll_up(rows):
        """Adds minute rows {(minute, k): counts} into {level: {(bucket, k): counts}}."""
        levels = {"1m": {}, "1h": {}, "1d": {}}
        for (minute, k), counts in rows.items():
            for level, start in (("1m", minute), ("1h", buckets.hour_start(minute)),
                                 ("1d", buckets.day_start(minute))):
                acc = levels[level].setdefault((start, k), [0, 0, 0])
                for i in range(3):
                    acc[i] += counts[i]
        return levels

    def flush_raw(self):
        """Writes the per-sample totals to the database on tmpfs."""
        self.next_raw_flush = self.clock() + RAW_FLUSH_INTERVAL
        if self.recent is None:
            self.pending_raw = []
            return
        if not self.pending_raw:
            return
        conn = self.recent
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.executemany("INSERT OR REPLACE INTO totals_raw (interface, ts, bytes_sent, bytes_recv)"
                             " VALUES (?, ?, ?, ?)", self.pending_raw)
            for interface, ts in self.last_ts.items():
                conn.execute("DELETE FROM totals_raw WHERE interface = ? AND ts < ?", (interface, ts - RAW_KEEP))
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        self.pending_raw = []

    def _changed_hosts(self):
        """Pending hosts with a new name, or not marked as seen for an hour."""
        rows = []
        for (i, a), (name, seen) in self.pending_hosts.items():
            old = self.written_hosts.get((i, a))
            if old is None or (name and name != old[0]) or seen - old[1] >= HOST_SEEN_INTERVAL:
                rows.append((i, a, name, seen))
        return rows

    def flush(self):
        """Writes everything pending in one transaction."""
        self.next_flush = self.clock() + self.flush_interval
        if not self.pending_totals and not self.pending_hosts:
            self.conn.execute("UPDATE collector_sessions SET last_heartbeat = ? WHERE id = ?",
                              (int(self.clock()), self.session_id))
            return
        new_ids = {}
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            by_id = {(minute, self._tuple_id(key, new_ids)): counts
                     for (minute, key), counts in self.pending.items()}
            for level, rows in self._roll_up(by_id).items():
                conn.executemany(_add_sql(f"agg_{level}", ("bucket_start", "tuple_id")),
                                 [(b, tid, *counts) for (b, tid), counts in rows.items()])
            for level, rows in self._roll_up({(m, i): c for (i, m), c in self.pending_totals.items()}).items():
                conn.executemany(_add_sql(f"totals_{level}", ("interface", "bucket_start")),
                                 [(i, b, *counts) for (b, i), counts in rows.items()])
            hosts = self._changed_hosts()
            conn.executemany(
                "INSERT INTO hosts (interface, addr, name, last_seen) VALUES (?, ?, ?, ?)"
                " ON CONFLICT DO UPDATE SET name = coalesce(excluded.name, name),"
                " last_seen = max(last_seen, excluded.last_seen)", hosts)
            services = [(p, port, name) for (p, port), name in self.pending_services.items()
                        if self.written_services.get((p, port)) != name]
            conn.executemany(
                "INSERT INTO services (proto, port, name) VALUES (?, ?, ?)"
                " ON CONFLICT DO UPDATE SET name = excluded.name", services)
            conn.executemany(
                "INSERT INTO ingest_state (interface, last_ts) VALUES (?, ?)"
                " ON CONFLICT DO UPDATE SET last_ts = max(last_ts, excluded.last_ts)",
                list(self.last_ts.items()))
            conn.execute("UPDATE collector_sessions SET last_heartbeat = ?, interface = coalesce(?, interface)"
                         " WHERE id = ?", (int(self.clock()), self.interface, self.session_id))
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        # Only now are the new tuple ids, and what is written, valid.
        self.tuple_ids.update(new_ids)
        for i, a, name, seen in hosts:
            old = self.written_hosts.get((i, a))
            self.written_hosts[(i, a)] = (name or (old[0] if old else None), seen)
        for p, port, name in services:
            self.written_services[(p, port)] = name
        log.debug("wrote %d connection and %d totals rows", len(self.pending), len(self.pending_totals))
        self._clear_pending()

    def purge(self, now=None):
        """Deletes minute and hour buckets older than their retention period."""
        now = int(now if now is not None else self.clock())
        cutoffs = {"1m": now - self.minute_days * buckets.DAY, "1h": now - self.hour_days * buckets.DAY}
        deleted = 0
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            for level, cutoff in cutoffs.items():
                for table in (f"agg_{level}", f"totals_{level}"):
                    deleted += conn.execute(f"DELETE FROM {table} WHERE bucket_start < ?", (cutoff,)).rowcount
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        if deleted:
            log.info("purged %d rows older than the retention period", deleted)
        return deleted
