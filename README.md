# iftop-web

A fork of the classic [iftop](https://www.ex-parrot.com/~pdw/iftop/) by Paul
Warren and Chris Lightfoot, extended with a web interface and a data
collector, so you can see not only which connections use your bandwidth
right now, but also which used it over the last hour, day, week or year.

It is forked from Debian's git repository for iftop
([salsa.debian.org/debian/iftop](https://salsa.debian.org/debian/iftop)),
so it builds on iftop 1.0pre4 with Debian's packaging and patches, and keeps
Debian's full history (see [Updates from Debian](#updates-from-debian)).

## What is different from iftop

- **No interactive terminal display.** The ncurses UI is replaced by JSON
  output: every two seconds, `iftop-json` writes one line with all current
  connections, their rates averaged over 2, 10 and 40 seconds, and the
  interface totals. If you want the classic terminal UI, use the regular
  `iftop`; `iftop-json` can be installed next to it.
- **A collector** runs `iftop-json`, keeps the latest snapshot in memory
  (on tmpfs), and stores the traffic per connection in an SQLite database
  by minute, hour and day. Client ports are folded (`*`), so the history
  counts traffic per service rather than per connection attempt. It is
  restart-safe and written with flash storage in mind, e.g. on a Raspberry
  Pi with an SD card.
- **A web interface** (Flask, under gunicorn) with:
  - a **live view**: the connections with the most traffic as bars for sent
    and received data, updated every two seconds, with filters for host,
    port, service and protocol;
  - a **history view**: tabs for the last minute, 5 minutes, hour, day,
    week, month and year, each with a chart of the interface's traffic and
    the top connections, source or destination hosts, with a small trend
    chart per entry and a detail view for every connection or host.

  All pages require a login. It listens on localhost by default and
  requires TLS on other addresses.

```
iftop-json ──JSON lines──▶ collector ──▶ live.json (tmpfs)  ──▶ web interface
 (packet capture)                    └─▶ iftop.db (SQLite)  ──▶ (Flask, gunicorn)
```

## Getting started

The project builds two Debian packages:

- **`iftop-json`**: the iftop program with JSON output
  (`/usr/sbin/iftop-json`).
- **`iftop-web`**: the collector and the web interface, as two systemd
  services.

It is developed and tested on Debian 13 (trixie), including on a Raspberry
Pi 4.

See **[webui/README.md](webui/README.md)** for building the packages,
installing and setting up the web interface (users, TLS), running it from the
source tree, and the details of the live and history pages and their APIs.
After installing the packages, `/usr/share/doc/iftop-web/README.Debian`
covers the first steps on the system.

## Repository layout

| Path | Contents |
|---|---|
| `*.c`, `*.h`, `configure.ac`, `Makefile.am` | iftop, with the display replaced by JSON output (`ui.c`) |
| `jsondump/` | a small library for writing JSON from C |
| `webui/` | the collector, the web interface and their tests (Python) |
| `debian/` | the Debian packaging, systemd units and `build-deb` |
| `PLAN.md` | the design and its history, including measurements and open ideas |

## Updates from Debian

This repository is a fork of Debian's iftop repository, not a copy: Debian's
`master` branch is part of its history. Updates from Debian, such as new
patches or packaging fixes, can therefore be merged as usual:

```sh
git remote add debian https://salsa.debian.org/debian/iftop.git
git fetch debian
git merge debian/master
```

Changes to files this fork leaves alone merge without trouble. Expect
conflicts where both sides changed the same files: `ui.c` (the display,
replaced by JSON output), `configure.ac` (no ncurses), `Makefile.am` (changed
source files), and
in `debian/` mainly `control`, `rules`, `changelog` and `patches/series`. A
new Debian patch for the ncurses display may not apply any more and can then
be dropped, as `support-multi-gigabit-interfaces.patch` was.

## License

iftop is free software under the GNU General Public License, version 2 or
later. `jsondump/` is under the GPL version 2 (© VMware, Inc.), and the web
interface and collector are under the GPL version 2 or later. See
[`COPYING`](COPYING) and [`debian/copyright`](debian/copyright) for details.

## Credits

iftop was written by Paul Warren and Chris Lightfoot, with contributions
from many others; see [`AUTHORS`](AUTHORS) and [`ChangeLog`](ChangeLog).
The Debian packaging this fork builds on is maintained by Markus Koschany,
with earlier work by the people listed in `debian/copyright`.

This fork was built with the help of AI: most of its code and documentation
(the JSON output, the collector, the web interface, the packaging and the
tests) was written with Anthropic's Claude, through Claude Code, directed by
the maintainer. Commits made this way say so in a `Co-Authored-By` line,
except `8797ca6` (the JSON output), which was made the same way but lacks
the line.
