import json
import os
import sys
import time

import pytest

WEBUI = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, WEBUI)

from common import config as config_mod  # noqa: E402

DATA = os.path.join(WEBUI, "tests", "data")
SAMPLE = os.path.join(DATA, "iftop-lo.jsonl")
PASSWORD = "correct horse battery"


def sample_lines():
    with open(SAMPLE) as f:
        return f.readlines()


@pytest.fixture
def cfg(tmp_path):
    return config_mod.from_dict({
        "interfaces": ["lo"],
        "datadir": str(tmp_path / "data"),
        "rundir": str(tmp_path / "run"),
        "web": {
            "users_db": str(tmp_path / "users" / "users.db"),
            "cookie_secure": False,
            "login_rate_limit": "3 per minute",
        },
    })


@pytest.fixture
def live_file(cfg):
    """Writes the last sample snapshot to live.json, with a fresh timestamp."""
    def write(snap=None, age=0):
        if snap is None:
            snap = json.loads(sample_lines()[-1])
        snap = dict(snap, timestamp=int(time.time()) - age)
        os.makedirs(cfg.rundir, exist_ok=True)
        tmp = cfg.live_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(snap, f)
        os.replace(tmp, cfg.live_path)
        return snap
    return write


@pytest.fixture
def app(cfg):
    from web import create_app
    from web.extensions import limiter

    app = create_app(cfg, secret_key="test-secret-key-0123456789", testing=True)
    app.config["WTF_CSRF_ENABLED"] = False
    limiter.reset()
    app.extensions["iftop_users"].add("alice", PASSWORD)
    return app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def logged_in(client):
    resp = client.post("/login", data={"username": "alice", "password": PASSWORD})
    assert resp.status_code == 302
    return client


@pytest.fixture
def fake_iftop(tmp_path):
    """A script that prints the sample lines and exits, like a short iftop run."""
    path = tmp_path / "fake-iftop"
    log = tmp_path / "fake-iftop.log"
    path.write_text(
        "#!/bin/sh\n"
        f"echo \"$@\" >> {log}\n"
        "echo 'interface: lo' >&2\n"
        f"cat {SAMPLE}\n"
    )
    path.chmod(0o755)
    return path, log

