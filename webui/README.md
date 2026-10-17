# iftop web interface

A web interface for iftop's JSON output: a live view and the traffic history.
See `../PLAN.md` for the full plan; stages 1 to 3 are done.

Two processes:

- **collector** (root): runs `iftop -i <interface> -P`, reads its JSON lines,
  writes the latest snapshot to `<rundir>/live.json` (default
  `/run/iftop-collector`, a tmpfs) and adds the traffic to the history in
  `<datadir>/iftop.db`. Restarts iftop if it exits.
- **web app** (unprivileged user, gunicorn): login, the live and history
  pages, `/api/live` and `/api/agg`. Only reads the collector's files; needs
  no access to the network interfaces.

## Requirements (Debian 13)

```sh
sudo apt install python3-flask python3-flask-login python3-flaskext.wtf \
    python3-flask-limiter gunicorn libjs-chart.js
```

For the tests: `python3-pytest`. iftop itself must be built from this
repository (the JSON output mode), e.g. `make` in the top directory.

## Setup

All commands run from this `webui/` directory.

```sh
sudo mkdir -p /etc/iftop-web /var/lib/iftop-web
sudo cp config.example.toml /etc/iftop-web/config.toml   # then edit it

# Secret key for session cookies, readable by the web app's user only.
head -c 32 /dev/urandom | base64 | sudo tee /etc/iftop-web/secret_key >/dev/null
sudo chown iftop-web: /etc/iftop-web/secret_key && sudo chmod 600 /etc/iftop-web/secret_key

# The users database lives in a directory owned by the web app's user.
sudo install -d -o iftop-web -m 700 /var/lib/iftop-web/users
```

`iftop-web` stands for whatever unprivileged user runs the web app.

### TLS

Listening on a public address requires TLS. Either set `certfile` and
`keyfile` in `[web]` (they must be readable by the web app's user), or run
behind a TLS-terminating reverse proxy and set `insecure_http = true` and
`proxy_fix = true`. gunicorn refuses to start on a non-local address without
one of the two.

Over HTTPS, the login form also checks that the browser's `Referer` header
matches the site (Flask-WTF's default). Browsers send it; when testing with
curl, add `-e https://host:port/login`.

### Users

There is no default account and no self-signup.

With the Debian package installed, use the wrapper (as root):

```sh
sudo iftop-web-user add alice      # asks for the password
sudo iftop-web-user passwd alice   # also logs out alice's sessions
sudo iftop-web-user del alice
sudo iftop-web-user list
```

From the source tree, run Flask in this `webui/` directory; `--app wsgi`
loads `wsgi.py` from the current directory:

```sh
sudo -u iftop-web flask --app wsgi user add alice
```

Set `IFTOP_WEB_CONFIG` if the config is not at `/etc/iftop-web/config.toml`.

## Running

```sh
# Collector, as root (or with CAP_NET_RAW on iftop):
sudo python3 -m collector [-c /etc/iftop-web/config.toml] [-v]

# Web app:
sudo -u iftop-web gunicorn -c gunicorn.conf.py
```

The collector should start after the interface is up: iftop captures nothing
if it opens the interface at the moment it comes up.

To try the web app without capturing, feed recorded iftop output to the
collector:

```sh
python3 -m collector -c config.toml --stdin < tests/data/iftop-lo.jsonl
```

## Live page

Shows the connections with the most traffic, as many as fit in the window,
with two bars each: sent (src → dst) and received. The rest are summed in an
"others" row. Filtering, sorting and the limit are done by the server, so the
page stays fast with many connections. Click the scale to reset it.

"Client ports: folded" merges connections that differ only in the client's
(ephemeral) port into one row, shown with `:*` and a connection count. Which
side is the client is decided by the rules in `../PLAN.md` (stage 2), using
`server_ports` and `ephemeral_port_min` from the config. Folding happens
before sorting, so many small connections to one server can rank above a
single large one. The filter then matches the folded rows: a client port no
longer matches.

`GET /api/live` parameters:

| Parameter | Values | Default |
|---|---|---|
| `limit` | 1–1000 | 50 |
| `window` | averaging window: `2`, `10`, `40` (seconds) | `2` |
| `sort` | `total`, `sent`, `recv` | `total` |
| `filter` | words that must all appear in host, IP, port, service or protocol | none |
| `fold` | `1`: merge connections that differ only in the client port | `0` |
| `port` | port number or service name; matches either end | none |
| `proto` | `tcp`, `udp`, `icmp`, `icmpv6`, … or a protocol number | none |

Rates are in bytes per second. `stale` is true when the snapshot is more than
10 seconds old.

## Building the Debian packages

From the top directory:

```sh
debian/bin/build-deb
```

or, in a configured tree (`./bootstrap && ./configure`), `make deb`.

This runs `dpkg-buildpackage -us -uc -b` (unsigned, binary only) and appends
the git short sha to the package version, for example
`1.0~pre4-10+json1+git6784659`; `dirty` follows the sha if tracked files have
uncommitted changes. The build runs in a temporary copy of the working tree,
so the repository is left untouched; the `.deb`, `.buildinfo` and `.changes`
files are moved to the parent directory. Extra arguments are passed to
`dpkg-buildpackage`.

## History page

The collector stores the traffic per connection in minute, hour and day
buckets (in local time), with the client ports folded as described in
`../PLAN.md`. It writes every 10 seconds; stopping it writes what is left.
Minutes are kept for `minute_days` (default 7), hours for `hour_days`
(default 365), days for ever.

The page has tabs for the last minute, 5 minutes, hour, day, week, month and
year.
Each shows a chart of the interface's traffic over that window and the top
connections, or source or destination hosts, with their bytes and average
rate. The minute chart has a point for every 2-second sample, 5 minutes one
per 10 seconds; for these the collector keeps the samples' totals for an
hour. Times when
the collector was not running are breaks in the chart, and the summary says
how much of the window has data. A window covers whole buckets, so it can be
up to one bucket longer than its name: the summary shows the exact range.

Each row of the top list has a small chart of its traffic over the window,
and links to its detail view: the same page narrowed to that connection or
host (`/history?tuple=ID`, `?src=ADDR`, `?dst=ADDR`). The Port and Protocol
controls narrow any view further. Filters are kept in the page address, so a
view can be bookmarked.

`GET /api/agg` parameters:

| Parameter | Values | Default |
|---|---|---|
| `window` | `minute`, `5min`, `hour`, `day`, `week`, `month`, `year` | `hour` |
| `group` | `tuple` (connection), `src`, `dst` (host) | `tuple` |
| `sort` | `total`, `sent`, `recv` | `total` |
| `limit` | 1–500 | 20 |
| `tuple` | a connection id (`id` of a top list item) | none |
| `src`, `dst` | a source or destination address | none |
| `port` | port number or service name; matches either end | none |
| `proto` | `tcp`, `udp`, `icmp`, `icmpv6`, … or a protocol number | none |

Bytes are totals for the window; chart rates are bytes per second. Each item
has `series`, its rate per bucket at the times in `series.times` (null where
the collector wasn't running). `filter` describes the active filters with
names resolved. Invalid filter values give a 400 error.

## Tests

```sh
python3 -m pytest
```
