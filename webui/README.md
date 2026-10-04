# iftop web interface

A web interface for iftop's JSON output. See `../PLAN.md` for the full plan;
this is stage 1, the live view.

Two processes:

- **collector** (root): runs `iftop -i <interface> -P`, reads its JSON lines
  and writes the latest snapshot to `<datadir>/live.json`. Restarts iftop if it
  exits.
- **web app** (unprivileged user, gunicorn): login, the live page and
  `/api/live`. Reads `live.json`; needs no access to the network interfaces.

## Requirements (Debian 13)

```sh
sudo apt install python3-flask python3-flask-login python3-flaskext.wtf \
    python3-flask-limiter gunicorn
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

```sh
sudo -u iftop-web flask --app wsgi user add alice      # asks for the password
sudo -u iftop-web flask --app wsgi user passwd alice   # also logs out alice's sessions
sudo -u iftop-web flask --app wsgi user del alice
sudo -u iftop-web flask --app wsgi user list
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

`GET /api/live` parameters:

| Parameter | Values | Default |
|---|---|---|
| `limit` | 1–1000 | 50 |
| `window` | averaging window: `2`, `10`, `40` (seconds) | `2` |
| `sort` | `total`, `sent`, `recv` | `total` |
| `filter` | words that must all appear in host, IP, port, service or protocol | none |

Rates are in bytes per second. `stale` is true when the snapshot is more than
10 seconds old.

## Tests

```sh
python3 -m pytest
```
