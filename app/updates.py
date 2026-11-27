"""Update notice (GitHub releases, checked once a day) and the Update button (via the updater)."""
import logging
import os
import re
import sqlite3
import tempfile
import time
import zipfile

import httpx

from . import activity, config
from .version import REPO, __version__

log = logging.getLogger("scanarr.updates")
AGENT_URL = os.environ.get("SCANARR_AGENT_URL", "")
AGENT_TOKEN = os.environ.get("SCANARR_AGENT_TOKEN", "")


def _ver(v):
    m = re.match(r"v?(\d+)\.(\d+)\.(\d+)", str(v or ""))
    return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)


def newer(a, b):
    return _ver(a) > _ver(b)


def latest(force=False):
    """Newest release on GitHub, cached for a day (internet only)."""
    c = activity.kv_json("update_check") or {}
    if not force and time.time() - c.get("ts", 0) < 86400:
        return c
    try:
        r = httpx.get(f"https://api.github.com/repos/{REPO}/releases/latest", timeout=10,
                      headers={"Accept": "application/vnd.github+json", "User-Agent": "scanarr/" + __version__})
        if r.status_code == 200:
            d = r.json()
            c = {"ts": time.time(), "version": str(d.get("tag_name", "")).lstrip("v"),
                 "notes": (d.get("body") or "")[:4000], "url": d.get("html_url"), "published": d.get("published_at")}
        elif r.status_code == 404:
            c = {"ts": time.time(), "version": None}
        else:
            c = {**c, "ts": time.time() - 86400 + 3600, "error": f"GitHub said {r.status_code}"}   # retry in an hour
    except httpx.HTTPError as e:
        c = {**c, "ts": time.time() - 86400 + 3600, "error": str(e)[:120]}
    activity.kv_set_json("update_check", c)
    return c


def info(force=False):
    c = latest(force)
    return {"current": __version__, "latest": c.get("version"), "available": bool(c.get("version") and newer(c["version"], __version__)),
            "notes": c.get("notes", ""), "url": c.get("url"), "error": c.get("error"), "can_update": bool(AGENT_URL and AGENT_TOKEN)}


def backup_file():
    """Settings + history saved to Config/backups before an update (keeps the last 5)."""
    d = os.path.join(config.CONFIG_DIR, "backups")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, time.strftime(f"before-update-{__version__}-%Y%m%d-%H%M%S.zip"))
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        if os.path.exists(config.SETTINGS_FILE):
            z.write(config.SETTINGS_FILE, "settings.json")
        dbp = os.path.join(config.CONFIG_DIR, "scanarr.db")
        if os.path.exists(dbp):
            with tempfile.TemporaryDirectory() as td:
                tmp = os.path.join(td, "scanarr.db")
                src, dst = sqlite3.connect(dbp), sqlite3.connect(tmp)
                with dst:
                    src.backup(dst)
                src.close()
                dst.close()
                z.write(tmp, "scanarr.db")
    os.chmod(path, 0o600)
    old = sorted(f for f in os.listdir(d) if f.startswith("before-update-"))
    for f in old[:-5]:
        try:
            os.remove(os.path.join(d, f))
        except OSError:
            pass
    return path


def _agent(method, path, **kw):
    if not (AGENT_URL and AGENT_TOKEN):
        raise RuntimeError("Updates from Scanarr are off. Run the setup wizard again (Update), or turn on "
                           "'Allow updates from Scanarr' there.")
    r = httpx.request(method, AGENT_URL.rstrip("/") + path, headers={"X-Token": AGENT_TOKEN}, timeout=20, **kw)
    if r.status_code >= 400:
        try:
            msg = r.json().get("detail")
        except ValueError:
            msg = r.text[:200]
        raise RuntimeError(f"updater: {msg}")
    return r.json()


def start(version):
    b = backup_file()
    activity.log("Update", "update", b, f"backup saved, updating {__version__} -> {version}")
    return _agent("POST", "/update", json={"version": version})


def status():
    return _agent("GET", "/status")
