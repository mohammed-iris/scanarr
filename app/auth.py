"""Simple login for the web UI. Credentials are printed to the log at every start,
so `docker logs scanarr` shows them if you forget."""
import hashlib
import hmac
import logging
import os
import secrets
import time

from . import config

log = logging.getLogger("scanarr")
COOKIE = "scanarr_session"
MAX_AGE = 30 * 86400


def ensure(announce=True):
    cfg = config.load()
    a = cfg["auth"]
    changed = False
    env_user, env_pass = os.environ.get("SCANARR_USERNAME"), os.environ.get("SCANARR_PASSWORD")
    if env_user:
        a["username"] = env_user
    if env_pass:
        a["password"] = env_pass
    if not a.get("password"):
        a["password"] = secrets.token_urlsafe(9)
        changed = True
    if not a.get("secret"):
        a["secret"] = secrets.token_hex(32)
        changed = True
    if changed or env_user or env_pass:
        config.save(cfg)
    if announce:
        bar = "=" * 60
        print(f"\n{bar}\n  Scanarr web login\n    username: {a['username']}\n    password: {a['password']}\n"
              f"  (change it in Settings > Login, or set SCANARR_USERNAME / SCANARR_PASSWORD)\n{bar}\n",
              flush=True)
    return a


def _sig(a, user, exp):
    # password is part of the key, so changing it logs everyone out
    key = (a["secret"] + a["password"]).encode()
    return hmac.new(key, f"{user}|{exp}".encode(), hashlib.sha256).hexdigest()


def make_token(a):
    exp = int(time.time()) + MAX_AGE
    return f"{a['username']}|{exp}|{_sig(a, a['username'], exp)}"


def valid(token):
    if not token:
        return False
    try:
        user, exp, sig = token.split("|")
        a = config.load()["auth"]
        return (user == a["username"] and int(exp) > time.time()
                and hmac.compare_digest(sig, _sig(a, user, int(exp))))
    except (ValueError, KeyError):
        return False


def check_login(username, password):
    a = config.load()["auth"]
    ok_u = hmac.compare_digest((username or "").encode(), a["username"].encode())
    ok_p = hmac.compare_digest((password or "").encode(), a["password"].encode())
    return a if ok_u and ok_p else None
