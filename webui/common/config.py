"""Configuration shared by the collector and the web app (config.toml)."""

import os
import tomllib
from dataclasses import dataclass, field

DEFAULT_PATH = "/etc/iftop-web/config.toml"
ENV_VAR = "IFTOP_WEB_CONFIG"

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


class ConfigError(Exception):
    pass


@dataclass
class RetentionConfig:
    minute_days: int = 7
    hour_days: int = 365


@dataclass
class WebConfig:
    bind: str = "127.0.0.1:8443"
    certfile: str | None = None
    keyfile: str | None = None
    # Allow plain HTTP on a non-local address, e.g. behind a TLS proxy.
    insecure_http: bool = False
    # Trust X-Forwarded-For/-Proto from one proxy in front of the app.
    proxy_fix: bool = False
    cookie_secure: bool = True
    session_timeout_minutes: int = 60
    secret_key_file: str = "/etc/iftop-web/secret_key"
    users_db: str = "/var/lib/iftop-web/users/users.db"
    login_rate_limit: str = "5 per minute"


@dataclass
class Config:
    interfaces: list[str]
    iftop: str = "/usr/sbin/iftop"
    iftop_args: list[str] = field(default_factory=list)
    datadir: str = "/var/lib/iftop-web"
    ephemeral_port_min: int = 32768
    retention: RetentionConfig = field(default_factory=RetentionConfig)
    web: WebConfig = field(default_factory=WebConfig)

    @property
    def interface(self):
        return self.interfaces[0]

    @property
    def live_path(self):
        return os.path.join(self.datadir, "live.json")


def _build(cls, data, section):
    known = cls.__dataclass_fields__
    unknown = set(data) - set(known)
    if unknown:
        raise ConfigError(f"unknown key(s) in {section}: {', '.join(sorted(unknown))}")
    return cls(**data)


def from_dict(data):
    data = dict(data)
    retention = _build(RetentionConfig, data.pop("retention", {}), "[retention]")
    web = _build(WebConfig, data.pop("web", {}), "[web]")
    if "interfaces" not in data:
        raise ConfigError("'interfaces' is required")
    cfg = _build(Config, data, "top level")
    cfg.retention = retention
    cfg.web = web
    if not isinstance(cfg.interfaces, list) or len(cfg.interfaces) != 1:
        raise ConfigError("'interfaces' must list exactly one interface (only one is supported for now)")
    return cfg


def load(path=None):
    path = path or os.environ.get(ENV_VAR) or DEFAULT_PATH
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except OSError as e:
        raise ConfigError(f"cannot read config {path}: {e}") from e
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"invalid config {path}: {e}") from e
    return from_dict(data)


def bind_is_local(bind):
    """True if a gunicorn bind address only listens on loopback (or a unix socket)."""
    if bind.startswith("unix:"):
        return True
    host = bind.rsplit(":", 1)[0] if ":" in bind else bind
    return host.strip("[]") in LOCAL_HOSTS


def check_web_bind(web):
    """Raises ConfigError if the web app would serve plain HTTP publicly."""
    has_tls = bool(web.certfile and web.keyfile)
    if not has_tls and not bind_is_local(web.bind) and not web.insecure_http:
        raise ConfigError(
            f"refusing to bind to {web.bind} without TLS: set certfile and keyfile, "
            "or insecure_http = true if a TLS proxy is in front")
