from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_login import LoginManager
from flask_wtf.csrf import CSRFProtect

csrf = CSRFProtect()
login_manager = LoginManager()
# In-memory counts: gunicorn runs a single worker process (see gunicorn.conf.py).
limiter = Limiter(key_func=get_remote_address, storage_uri="memory://", default_limits=[])
