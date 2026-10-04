import pytest

from common import config as config_mod


def test_example_config_loads():
    cfg = config_mod.load(__file__.replace("tests/test_config.py", "config.example.toml"))
    assert cfg.interface == "eth0"
    config_mod.check_web_bind(cfg.web)


def test_at_most_one_interface():
    with pytest.raises(config_mod.ConfigError):
        config_mod.from_dict({"interfaces": ["eth0", "eth1"]})
    assert config_mod.from_dict({}).interface is None
    assert config_mod.from_dict({"interfaces": []}).interface is None


def test_unknown_keys_are_rejected():
    with pytest.raises(config_mod.ConfigError, match="bnid"):
        config_mod.from_dict({"interfaces": ["eth0"], "web": {"bnid": "x"}})


@pytest.mark.parametrize("bind,local", [
    ("127.0.0.1:8443", True),
    ("localhost:8000", True),
    ("[::1]:8443", True),
    ("unix:/run/iftop-web.sock", True),
    ("0.0.0.0:8443", False),
    ("[::]:8443", False),
    ("192.0.2.1:80", False),
])
def test_bind_is_local(bind, local):
    assert config_mod.bind_is_local(bind) is local


def test_public_bind_requires_tls():
    web = config_mod.WebConfig(bind="0.0.0.0:8443")
    with pytest.raises(config_mod.ConfigError, match="without TLS"):
        config_mod.check_web_bind(web)
    config_mod.check_web_bind(config_mod.WebConfig(bind="0.0.0.0:8443", insecure_http=True))
    config_mod.check_web_bind(config_mod.WebConfig(bind="0.0.0.0:8443", certfile="c", keyfile="k"))
    config_mod.check_web_bind(config_mod.WebConfig(bind="127.0.0.1:8000"))
