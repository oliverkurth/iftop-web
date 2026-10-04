# gunicorn settings, taken from the [web] section of the iftop-web config
# ($IFTOP_WEB_CONFIG, default /etc/iftop-web/config.toml).
#
#   cd webui && gunicorn -c gunicorn.conf.py

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import config as config_mod  # noqa: E402

try:
    _cfg = config_mod.load()
    config_mod.check_web_bind(_cfg.web)
except config_mod.ConfigError as e:
    sys.exit(f"iftop-web: {e}")

wsgi_app = "wsgi:app"
bind = [_cfg.web.bind]
if _cfg.web.certfile and _cfg.web.keyfile:
    certfile = _cfg.web.certfile
    keyfile = _cfg.web.keyfile

# One process: login rate limits and the snapshot cache live in memory.
workers = 1
worker_class = "gthread"
threads = 8
accesslog = "-"
