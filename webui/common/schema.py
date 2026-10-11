"""SQLite schema of the traffic history (<datadir>/iftop.db).

The collector writes it; the web app opens it read-only. It uses a rollback
journal, not WAL: a WAL database cannot be opened read-only by another user
while the collector is stopped.

Ports in `tuples`: 0 means the connection has no ports (e.g. ICMP), -1 that
the port was folded (the client's ephemeral port, shown as "*").
"""

SCHEMA_VERSION = 2

PORT_NONE = 0
PORT_FOLDED = -1

LEVELS = ("1m", "1h", "1d")

# How long the per-sample interface totals are kept, for the short charts.
RAW_KEEP = 3600

# Per-sample interface totals (one row per 2-second iftop sample).
RAW_TABLE = """
CREATE TABLE totals_raw (
    interface  TEXT NOT NULL,
    ts         INTEGER NOT NULL,
    bytes_sent INTEGER NOT NULL,
    bytes_recv INTEGER NOT NULL,
    PRIMARY KEY (interface, ts)
) WITHOUT ROWID;
"""

# Statements that bring a database from version N-1 to N.
MIGRATIONS = {
    2: RAW_TABLE,
}

SCHEMA = """
CREATE TABLE tuples (
    id        INTEGER PRIMARY KEY,
    interface TEXT NOT NULL,
    proto     INTEGER NOT NULL,
    src_addr  TEXT NOT NULL,
    src_port  INTEGER NOT NULL,
    dst_addr  TEXT NOT NULL,
    dst_port  INTEGER NOT NULL,
    UNIQUE (interface, proto, src_addr, src_port, dst_addr, dst_port)
);

-- Latest resolved name per address.
CREATE TABLE hosts (
    interface TEXT NOT NULL,
    addr      TEXT NOT NULL,
    name      TEXT,
    last_seen INTEGER NOT NULL,
    PRIMARY KEY (interface, addr)
) WITHOUT ROWID;

-- Service names of known ports (as reported by iftop from /etc/services).
CREATE TABLE services (
    proto INTEGER NOT NULL,
    port  INTEGER NOT NULL,
    name  TEXT NOT NULL,
    PRIMARY KEY (proto, port)
) WITHOUT ROWID;

-- Timestamp of the newest sample stored, so replayed samples are skipped.
CREATE TABLE ingest_state (
    interface TEXT PRIMARY KEY,
    last_ts   INTEGER NOT NULL
) WITHOUT ROWID;

CREATE TABLE collector_sessions (
    id             INTEGER PRIMARY KEY,
    interface      TEXT,
    started        INTEGER NOT NULL,
    last_heartbeat INTEGER NOT NULL,
    stopped        INTEGER
);
"""

# One table per resolution for connections and for the interface totals.
# "samples" counts the 2-second iftop samples in the bucket; a bucket without
# a totals row had no data (collector not running).
for _level in LEVELS:
    SCHEMA += f"""
CREATE TABLE agg_{_level} (
    bucket_start INTEGER NOT NULL,
    tuple_id     INTEGER NOT NULL REFERENCES tuples (id),
    bytes_sent   INTEGER NOT NULL,
    bytes_recv   INTEGER NOT NULL,
    samples      INTEGER NOT NULL,
    PRIMARY KEY (bucket_start, tuple_id)
) WITHOUT ROWID;

CREATE TABLE totals_{_level} (
    interface    TEXT NOT NULL,
    bucket_start INTEGER NOT NULL,
    bytes_sent   INTEGER NOT NULL,
    bytes_recv   INTEGER NOT NULL,
    samples      INTEGER NOT NULL,
    PRIMARY KEY (interface, bucket_start)
) WITHOUT ROWID;
"""

SCHEMA += RAW_TABLE
