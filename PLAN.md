# iftop web interface and traffic history — plan

Source: `spex.txt`. Builds on the JSON output mode on branch
`topic/okurth/json-output`, where `ui.c` writes one JSON object per line to
stdout every 2 seconds (`RESOLUTION`).

## Decisions

| Topic | Decision |
|---|---|
| Ephemeral ports | The client side's port is folded to `*`. Which side is the client is decided by rules (configured server ports, known services, low ports, then a threshold); see stage 2. |
| Host names | Resolved names are stored. Records are keyed by IP; the latest name per IP is kept in a `hosts` table and used for display and grouping. |
| Interfaces | One interface, set in config. Schema and collector are prepared for several. |
| Access | Listens on public interfaces; login required for everything; TLS required. |
| Packaging | Deferred. Run by hand, documented in `webui/README.md`. |

## Input: iftop JSON

Per line: `timestamp`, `interface`, `connections[]`, `totals`. Per connection:
`src`/`dst` (`host`, `port`, `service`), `protocol`, `sent_rate`/`recv_rate`
(bytes/s averaged over 2 s, 10 s and 40 s), `total_sent`/`total_recv`.

Constraints:

- **iftop must run with `-P`.** Otherwise `ui_common.c` sets ports to 0 and the
  port-based views in stage 3 are impossible.
- **Count bytes as `rate[0] × Δt`, not as deltas of `total_*`.** A
  connection's totals reset when it drops out of iftop's history and comes
  back. Δt is the gap between sample timestamps, capped at a few seconds.
- **Host names are resolved in the background.** iftop prints the IP until the
  lookup finishes, and a host's name can change. If records were keyed on the
  host string, one connection's traffic would be split between IP and name.
  The fix is a separate `addr` field (see stage 1).

## Architecture

```
iftop -P ... (root / CAP_NET_RAW)
   │ JSON lines on stdout
   ▼
collector.py  (one daemon; starts iftop and owns it; runs as root)
   ├── live snapshot  → <rundir>/live.json    (temp file + rename; /run tmpfs)
   └── aggregates     → <datadir>/iftop.db    (SQLite)         [stage 2]
   ▼
Flask app under gunicorn (unprivileged user, data directory read-only)
   ├── /login, /logout
   ├── /             live bar view, polls /api/live
   ├── /history/*    aggregated views                          [stage 2/3]
   └── /api/*        JSON endpoints for the pages
```

Why it is split this way:

- Packet capture needs root; the public web server does not run as root.
- Exactly one process reads iftop.
- If the web app restarts, no data is lost.

The users table goes in a separate `users.db` that the web app owns, so the web
app needs no write access to the collector's data.

Layout:

```
webui/
  collector/       collector.py, config handling, aggregation
  web/             Flask app, templates, static JS/CSS
  schema.sql
  config.example.toml
  tests/
  README.md
```

## Configuration (`config.toml`)

```toml
interfaces = ["eth0"]        # exactly one entry for now
iftop = "/usr/sbin/iftop"
iftop_args = []              # extra args, e.g. a BPF filter via -f
datadir = "/var/lib/iftop-web"
ephemeral_port_min = 32768   # Linux default range starts here
server_ports = []            # servers on high ports, e.g. [51820, 32400]

[retention]
minute_days = 7
hour_days = 365

[web]
bind = "0.0.0.0:8443"
certfile = "/etc/iftop-web/cert.pem"
keyfile = "/etc/iftop-web/key.pem"
insecure_http = false        # allow plain HTTP when not on localhost (TLS-terminating proxy)
session_timeout_minutes = 60
secret_key_file = "/etc/iftop-web/secret_key"
```

## Stage 1 — live view

1. **C change: separate `addr` and `host` in the JSON.** In `endpoint_json()`
   in `ui.c`, add `addr` (always the numeric IP) and make `host` the resolved
   name or `null` if it is not resolved yet. Today `host` holds the IP when
   there is no name. Test with the `-n` option both on and off.
2. **`collector.py` (minimal)**
   - Starts iftop with `-P -i <if>` (no `-n`, so names get resolved) and reads
     stdout line by line.
   - Validates each line, then writes `live.json` (temp file + rename).
   - Restarts iftop with backoff if it exits; logs iftop's stderr.
   - `--stdin` mode reads JSON lines from stdin, for tests and replays.
   - Shuts down cleanly on SIGTERM.
3. **Flask app**
   - Login with Flask-Login. Users are in `users.db`, passwords hashed with
     werkzeug scrypt. Every page and every `/api/*` route requires login.
   - Users are managed from the command line with `flask user add|passwd|del`.
     No self-signup and no default account.
   - CSRF protection (Flask-WTF) on the login form and any other form.
   - Session cookies are `Secure`, `HttpOnly` and `SameSite=Lax`, with a
     configurable timeout.
   - Failed logins are rate-limited per IP (Flask-Limiter).
   - `GET /api/live?limit=N&window=W&sort=S&filter=F` returns only the top N
     connections (see the live page), plus the snapshot age, so a dead
     collector shows up as stale. The collector parses `live.json` once per
     update; the web app caches the parsed snapshot by file mtime.
4. **Live page (`/`): top consumers only**
   - Shows only the top N connections, as many as fit on the screen. The page
     works out N from the viewport height divided by the row height, and
     recalculates it on resize.
   - Sorting, filtering and the limit are done on the server, so the response
     stays small however many connections iftop sees. Order: apply the filter,
     sort by the selected window (sent + received, sent, or received), then
     take the first N.
   - Footer: "showing N of M connections", plus one "others" row with the
     summed rates of the connections not shown, so the totals still add up.
   - Two horizontal bars per connection: sent and received. Each bar is a
     plain `<div>` whose width JS sets as a percentage of the scale maximum,
     with a short CSS transition. No chart library.
   - Bar scale works like iftop's: fixed steps (64 kbit, 128 kbit, 256 kbit,
     1 Mbit, …) that only grow, so bars don't jump when the busiest connection
     changes. Linear or log.
   - Label: `src:port ↔ dst:port`, protocol; IP as a tooltip.
   - Controls: averaging window (2 s, 10 s or 40 s), linear or log scale, sort
     order, text filter on host or port, and "Client ports: separate /
     folded".
   - Folding (`/api/live?fold=1`) merges connections that differ only in the
     client port, using the same rules as stage 2 (`common/ports.py`). It is
     done on the server before sorting and the limit, so many small
     connections to one server rank by their sum. Each folded row shows `:*`
     and how many connections it contains.
   - Totals row at the top; refreshes every 2 s. Polling pauses while the tab
     is hidden (`visibilitychange`).
   - Plain JS and CSS, no build step; static assets served locally (no CDN).
5. **gunicorn**
   - Config file reads `[web]` from `config.toml`.
   - Serves TLS from `certfile`/`keyfile`.
   - Refuses to bind to anything other than localhost without TLS unless
     `insecure_http = true`.
6. **Tests**
   - A recorded file of iftop JSON lines replayed through `--stdin`.
   - Flask test client: unauthenticated requests are rejected, login works,
     CSRF is enforced, and `/api/live` returns the expected data.
   - `/api/live`: limit, sort and filter are applied in the right order, and
     the "others" row plus the shown rows add up to the totals.

## Stage 2 — aggregation

1. **Key for each record:** `(interface, proto, src_addr, src_port, dst_addr,
   dst_port)`, with the client side's port folded to `*` so each connection
   doesn't become its own record.

   There is no single definition of an ephemeral port. IANA's range is
   49152–65535, Linux uses 32768–60999, FreeBSD 10000–65535, and old Windows
   1024–5000. Real servers also listen in those ranges, e.g. WireGuard on 51820
   or Plex on 32400. So a fixed threshold alone won't do. Instead the collector
   decides which side is the server and folds the other side's port. The first
   rule that applies wins:

   1. **Configured server port:** if one side's port is in `server_ports`, that
      side is the server.
   2. **Known service:** if exactly one side is a known service, that side is
      the server. "Known" means iftop reports a service name that differs from
      the port number (iftop reports the number itself when `/etc/services` has
      no entry).
   3. **Low port:** if one side is below 1024 and the other isn't, the low side
      is the server.
   4. **Threshold:** otherwise, if the higher port is ≥ `ephemeral_port_min`,
      the lower side is the server. If both ports are below the threshold, or
      they are equal, nothing is folded.

   Connections without ports (e.g. ICMP) are never folded. The rules only
   affect the stored key; the live view keeps the real ports.

   Possible later improvement: learn server ports from the data (a port seen
   with many different peer ports is a server). Add this only if the rules
   above fold the wrong side in practice.
2. **SQLite schema (`iftop.db`, `common/schema.py`)**
   - `tuples(id, interface, proto, src_addr, src_port, dst_addr, dst_port)`,
     unique on the key. Port 0 means the connection has no ports (e.g. ICMP),
     -1 that the port was folded (`*`).
   - `hosts(interface, addr, name, last_seen)`: latest resolved name per IP.
     A sample without a name doesn't overwrite a known one.
   - `services(proto, port, name)`: service label per known port.
   - `agg_1m`, `agg_1h`, `agg_1d`:
     `(bucket_start, tuple_id, bytes_sent, bytes_recv, samples)`, primary key
     `(bucket_start, tuple_id)`.
   - `totals_1m`, `totals_1h`, `totals_1d`: per-interface totals at the same
     resolutions, from iftop's interface totals. A bucket without a totals
     row had no data: the collector wasn't running.
   - `totals_raw(interface, ts, bytes_sent, bytes_recv)`: the interface
     totals of every 2-second sample, kept for an hour, for the charts of
     the minute and 5-minute tabs.
   - `ingest_state(interface, last_ts)`: newest sample stored.
   - `collector_sessions(id, interface, started, last_heartbeat, stopped)`.
   - 5-minute, week and month views are computed from these tables with
     queries; they get no tables of their own.
   - **Rollback journal, not WAL.** The web app opens the database read-only
     as another user. With WAL that fails whenever the collector is stopped,
     because the reader would have to create the shared-memory file.
3. **Writing data (`collector/store.py`)**
   - **Bytes per sample = `rate[0] × 2`.** iftop prints one line per 2-second
     history slot, and `rate[0]` is that slot's bytes divided by 2 even when
     the slot ran late. So this is exact, while `rate × Δt` would be wrong
     whenever the whole-second timestamps jitter.
   - Counts are summed in memory and written every 10 s in one transaction
     with `INSERT … ON CONFLICT DO UPDATE SET bytes = bytes + excluded.bytes`.
   - **No separate rollup step.** Each write adds the same counts to the
     minute, hour and day tables in that one transaction, so hours and days
     are always current and always agree with the minutes.
   - **Buckets in local time (`common/buckets.py`).** Days start at local
     midnight (23 or 25 hours on DST days). Hours follow the local UTC
     offset, so half-hour zones line up and the repeated hour on DST end
     stays two buckets.
4. **Restart safety**
   - Additive upserts, written in the same transaction as `ingest_state`: a
     crash loses at most ~10 s of data and never counts anything twice.
   - Ingest skips any sample whose timestamp is not later than the last one
     stored (safe for replays and overlap). A clock jumping backwards drops
     samples until it has caught up; this is logged.
   - A failed write is rolled back and retried at the next flush.
   - SIGTERM, or iftop exiting, triggers a final write.
   - `collector_sessions` records start, heartbeat (each write) and clean
     stop.
5. **Retention:** 1m kept `minute_days` (default 7), 1h kept `hour_days`
   (default 365), 1d kept for ever. The purge runs at startup and then daily
   inside the collector.
6. **Web pages (`web/history.py`)**
   - `/history` has tabs: minute, 5 min, hour, day, week, month. They are
     rolling windows ("the last hour"), made of the whole buckets that
     overlap the window, so a window is at most one bucket longer than its
     name. The page shows the exact range and how much of it has data.
   - Each tab shows a chart of the interface's sent and received rate, and a
     top-N table (bytes and average rate) for the window, grouped by
     connection, source host or destination host. The chart covers exactly
     the same range as the table.

     | Tab | Top list from | Chart points | Ticks |
     |---|---|---|---|
     | minute | 1m | every sample (2 s) | 10 s |
     | 5 min | 1m | 10 s | 1 min |
     | hour | 1m | 1 min | 10 min |
     | day | 1h | 5 min (from 1m) | 3 h |
     | week | 1h | 1 h | midnight |
     | month | 1d | 1 day | midnight |

   - Chart rates are bytes per second of collected time. A step without any
     data is a break in the line, not zero.
   - Charts use Chart.js from the Debian package `libjs-chart.js`, served by
     Flask at `/vendor/chart.min.js` (`[web] chart_js` in the config).
   - API: `/api/agg?window=…&group=tuple|src|dst&sort=total|sent|recv&limit=…`.
7. **Tests:** bucket boundaries (DST change days in Berlin, half-hour offset
   in Kolkata), all three resolutions adding up, a crash before the write
   followed by a restart and a replay, a failed write, name updates in
   `hosts`, retention, folding into the stored keys, and the API: windows,
   grouping, sorting, the "others" sum and gaps in the chart.

## Stage 3 — history and port selection

1. **Most-used tuples:** top-N tuples by bytes for a chosen window, each with a
   small history chart. Clicking one opens a detail page with its full history.
2. **Per source and per destination:** the same views grouped by source host
   or destination host (by name via `hosts`, IP shown as a tooltip). This is a
   `GROUP BY` over `tuples`; the stored data doesn't change.
3. **Port selection:** a filter on port or service (and protocol) on every
   history page and on the live page. Indexes on `tuples(src_port)`,
   `tuples(dst_port)`, `tuples(src_addr)` and `tuples(dst_addr)`.
4. **Speed:** if per-host queries on 1-year hourly data turn out slow, add
   per-host rollup tables then, not before.

## Later

- Several interfaces: one iftop reader per interface in the collector, plus an
  interface selector in the UI. The schema already supports it.
- Debian packaging: an `iftop-web` binary package, systemd units for the
  collector and gunicorn, a system user, `/var/lib/iftop-web`.

## Dependencies (Debian 13)

Runtime: `python3-flask`, `python3-flask-login`, `python3-flaskext.wtf`,
`python3-flask-limiter`, `gunicorn`, `libjs-chart.js` (stage 2).
Development: `python3-pytest`, `sqlite3` (CLI, optional).
iftop build: `build-essential`, `autoconf`, `automake`, `libpcap-dev`.
Python standard library covers the rest (`sqlite3`, `tomllib`, `json`,
`subprocess`).
