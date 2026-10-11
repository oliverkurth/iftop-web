"""Runs iftop, reads its JSON lines, publishes the latest snapshot and
stores the traffic history.

The snapshot is written to <datadir>/live.json via a temporary file and a
rename, so readers never see a partial file. The history goes to
<datadir>/iftop.db (see store.py).
"""

import argparse
import json
import logging
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time

from common import config as config_mod
from collector.store import Store, StoreError

log = logging.getLogger("iftop-collector")

BACKOFF_MIN = 1.0
BACKOFF_MAX = 60.0
# A run at least this long resets the restart backoff.
HEALTHY_RUN = 60.0


def _is_rates(value):
    return (isinstance(value, list) and len(value) == 3
            and all(isinstance(v, (int, float)) for v in value))


def _is_endpoint(value):
    return isinstance(value, dict) and "addr" in value and "port" in value


def parse_line(line):
    """Returns the snapshot in a line of iftop output, or None if invalid."""
    line = line.strip()
    if not line:
        return None
    try:
        snap = json.loads(line)
    except ValueError:
        return None
    if not (isinstance(snap, dict)
            and isinstance(snap.get("timestamp"), int)
            and isinstance(snap.get("connections"), list)
            and isinstance(snap.get("totals"), dict)):
        return None
    for c in snap["connections"]:
        if not (isinstance(c, dict)
                and _is_endpoint(c.get("src")) and _is_endpoint(c.get("dst"))
                and _is_rates(c.get("sent_rate")) and _is_rates(c.get("recv_rate"))):
            return None
    return snap


class LiveWriter:
    """Atomically replaces <datadir>/live.json with each new snapshot."""

    def __init__(self, path):
        self.path = path
        self.dir = os.path.dirname(path) or "."
        os.makedirs(self.dir, mode=0o755, exist_ok=True)

    def write(self, snap):
        fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=".live.", suffix=".tmp")
        try:
            # mkstemp creates the file 0600; the web app runs as another user.
            os.fchmod(fd, 0o644)
            with os.fdopen(fd, "w") as f:
                json.dump(snap, f, separators=(",", ":"))
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


def consume(lines, writer, store=None):
    """Publishes (and stores) every valid snapshot in lines. Returns the number published."""
    count = bad = 0
    for line in lines:
        snap = parse_line(line)
        if snap is None:
            if line.strip():
                bad += 1
                if bad <= 10 or bad % 1000 == 0:
                    log.warning("ignoring invalid line from iftop (%d so far): %.200s", bad, line.strip())
            continue
        writer.write(snap)
        if store is not None:
            store.add(snap)
            store.maybe_flush()
        count += 1
    return count


class Collector:
    def __init__(self, cfg, writer, store=None, backoff_min=BACKOFF_MIN, backoff_max=BACKOFF_MAX):
        self.cfg = cfg
        self.writer = writer
        self.store = store
        self.backoff_min = backoff_min
        self.backoff_max = backoff_max
        self.stop = threading.Event()
        self.proc = None

    def command(self):
        # -P: ports are needed for the port views; no -n: names are wanted.
        # Without an interface, iftop picks the first one that is running.
        iface = ["-i", self.cfg.interface] if self.cfg.interface else []
        return [self.cfg.iftop, *iface, "-P", *self.cfg.iftop_args]

    def request_stop(self, *_):
        self.stop.set()
        proc = self.proc
        if proc is not None and proc.poll() is None:
            proc.terminate()

    def _log_stderr(self, stream):
        for line in stream:
            line = line.rstrip()
            if line:
                log.info("iftop: %s", line)

    def run_once(self):
        cmd = self.command()
        log.info("starting %s", " ".join(cmd))
        try:
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                         stdin=subprocess.DEVNULL, text=True, bufsize=1)
        except OSError as e:
            log.error("cannot start iftop: %s", e)
            return
        err = threading.Thread(target=self._log_stderr, args=(self.proc.stderr,), daemon=True)
        err.start()
        try:
            consume(self.proc.stdout, self.writer, self.store)
        finally:
            if self.store is not None:
                self.store.flush()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
            err.join(timeout=1)
            log.info("iftop exited with status %s", self.proc.returncode)
            self.proc = None

    def run(self):
        delay = self.backoff_min
        while not self.stop.is_set():
            started = time.monotonic()
            self.run_once()
            if self.stop.is_set():
                break
            if time.monotonic() - started >= HEALTHY_RUN:
                delay = self.backoff_min
            log.info("restarting iftop in %.0f s", delay)
            self.stop.wait(delay)
            delay = min(delay * 2, self.backoff_max)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="collector", description=__doc__)
    parser.add_argument("-c", "--config", help=f"config file (default: ${config_mod.ENV_VAR} or {config_mod.DEFAULT_PATH})")
    parser.add_argument("--stdin", action="store_true", help="read iftop JSON lines from stdin instead of running iftop")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    try:
        cfg = config_mod.load(args.config)
    except config_mod.ConfigError as e:
        log.error("%s", e)
        return 2

    writer = LiveWriter(cfg.live_path)
    try:
        store = Store.from_config(cfg).open()
    except (StoreError, OSError, sqlite3.Error) as e:
        log.error("cannot open %s: %s", cfg.db_path, e)
        return 2

    try:
        if args.stdin:
            signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
            try:
                consume(sys.stdin, writer, store)
            except KeyboardInterrupt:
                pass
            return 0

        collector = Collector(cfg, writer, store)
        signal.signal(signal.SIGTERM, collector.request_stop)
        signal.signal(signal.SIGINT, collector.request_stop)
        collector.run()
        log.info("stopped")
        return 0
    finally:
        store.close()
