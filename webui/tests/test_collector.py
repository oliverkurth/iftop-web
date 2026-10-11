import json
import os
import subprocess
import sys
import threading

from conftest import SAMPLE, WEBUI, sample_lines
from collector.collector import Collector, LiveWriter, consume, parse_line


def test_parse_line_accepts_iftop_output():
    for line in sample_lines():
        snap = parse_line(line)
        assert snap is not None
        assert snap["interface"] == "lo"


def test_parse_line_rejects_garbage():
    assert parse_line("") is None
    assert parse_line("not json") is None
    assert parse_line("[1, 2]") is None
    assert parse_line('{"timestamp": 1}') is None
    snap = json.loads(sample_lines()[-1])
    snap["connections"][0]["sent_rate"] = [1, 2]
    assert parse_line(json.dumps(snap)) is None


def test_consume_keeps_last_valid_snapshot(tmp_path):
    path = tmp_path / "live.json"
    lines = sample_lines()
    count = consume(lines[:2] + ["garbage\n"] + lines[2:] + ["{\n"], LiveWriter(str(path)))
    assert count == len(lines)
    assert json.loads(path.read_text()) == json.loads(lines[-1])
    # Readable by the web app's user, and no temporary files left behind.
    assert path.stat().st_mode & 0o777 == 0o644
    assert os.listdir(tmp_path) == ["live.json"]


def test_stdin_mode(tmp_path, cfg):
    config = tmp_path / "config.toml"
    config.write_text(f'interfaces = ["lo"]\ndatadir = "{cfg.datadir}"\n')
    with open(SAMPLE) as f:
        subprocess.run([sys.executable, "-m", "collector", "-c", str(config), "--stdin"],
                       stdin=f, cwd=WEBUI, check=True, timeout=30)
    with open(cfg.live_path) as f:
        assert json.load(f) == json.loads(sample_lines()[-1])


def test_runs_iftop_with_ports_and_restarts_it(cfg, fake_iftop):
    script, log = fake_iftop
    cfg.iftop = str(script)
    cfg.iftop_args = ["-f", "not port 22"]
    collector = Collector(cfg, LiveWriter(cfg.live_path), backoff_min=0.01, backoff_max=0.01)

    def stop_after_restarts():
        while not log.exists() or len(log.read_text().splitlines()) < 3:
            pass
        collector.request_stop()

    t = threading.Thread(target=stop_after_restarts)
    t.start()
    collector.run()
    t.join()

    runs = log.read_text().splitlines()
    assert len(runs) >= 3
    assert runs[0] == "-i lo -P -f not port 22"
    with open(cfg.live_path) as f:
        assert json.load(f) == json.loads(sample_lines()[-1])


def test_missing_iftop_binary_is_retried(cfg, tmp_path):
    cfg.iftop = str(tmp_path / "does-not-exist")
    collector = Collector(cfg, LiveWriter(cfg.live_path), backoff_min=0.01, backoff_max=0.01)
    attempts = []
    real_run_once = collector.run_once

    def run_once():
        attempts.append(1)
        real_run_once()
        if len(attempts) == 3:
            collector.request_stop()

    collector.run_once = run_once
    collector.run()
    assert len(attempts) == 3
    assert not os.path.exists(cfg.live_path)


def test_no_interface_lets_iftop_choose(cfg):
    cfg.interfaces = []
    assert Collector(cfg, LiveWriter(cfg.live_path)).command() == [cfg.iftop, "-P"]


def _history_sums(path):
    import sqlite3
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT sum(bytes_sent), sum(bytes_recv), sum(samples) FROM totals_1m").fetchone()
    finally:
        conn.close()


def test_stdin_mode_stores_history_once(tmp_path, cfg):
    config = tmp_path / "config.toml"
    config.write_text(f'interfaces = ["lo"]\ndatadir = "{cfg.datadir}"\n')
    expected_sent = sum(json.loads(line)["totals"]["sent_rate"][0] * 2 for line in sample_lines())
    for _ in range(2):
        with open(SAMPLE) as f:
            subprocess.run([sys.executable, "-m", "collector", "-c", str(config), "--stdin"],
                           stdin=f, cwd=WEBUI, check=True, timeout=30)
        # The second replay finds every sample already stored.
        assert _history_sums(cfg.db_path) == (expected_sent, 0, len(sample_lines()))


def test_stopping_iftop_writes_the_history(cfg, fake_iftop):
    from collector.store import Store

    script, log = fake_iftop
    cfg.iftop = str(script)
    store = Store.from_config(cfg).open()
    collector = Collector(cfg, LiveWriter(cfg.live_path), store, backoff_min=0.01, backoff_max=0.01)
    collector.run_once()        # the fake iftop prints the sample and exits
    # Nothing was due for writing yet, but iftop exiting flushes everything.
    assert _history_sums(cfg.db_path)[2] == len(sample_lines())
    store.close()
