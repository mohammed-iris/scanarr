"""First-start wiring for a stack created by the setup wizard (settings["stack"]["managed"]).

Runs inside Scanarr's worker on every cycle until it has fully succeeded, so it doesn't matter which
container starts first or if one restarts: it simply retries. Everything is idempotent.

  qBittorrent: folders, Manual mode, low upload, stop seeding, categories, password in sync
  Prowlarr:    add the indexers you picked in the wizard
  Report:      what was done and what failed, shown in Scanarr (Apps) and to the wizard
"""
import hashlib
import logging
import time

import httpx

from . import activity, config
from .qbit import QBit

log = logging.getLogger("scanarr.bootstrap")


def _fingerprint(username, password):
    return hashlib.sha256(f"{username}\0{password}".encode()).hexdigest()[:16]


def _report(step, ok, msg):
    rep = activity.kv_json("bootstrap_report", {})
    rep[step] = {"ok": ok, "msg": msg, "ts": time.time()}
    activity.kv_set_json("bootstrap_report", rep)


def report():
    return activity.kv_json("bootstrap_report", {})


def qbit_setup(cfg, qb):
    st = cfg.get("stack") or {}
    up = max(int(st.get("upload_kbps") or 50), 1) * 1024
    qb.set_preferences({
        "save_path": "/data/completed", "temp_path_enabled": True, "temp_path": "/data/temp",
        "auto_tmm_enabled": False,
        "up_limit": up,                                     # low upload while downloading
        "max_ratio_enabled": True, "max_ratio": 0, "max_ratio_act": 0,   # stop seeding when finished
        "queueing_enabled": True,
        **({"listen_port": int(st["torrent_port"])} if st.get("torrent_port") else {}),
    })
    for kind in config.KINDS:
        qb.ensure_category(cfg["categories"].get(kind, kind))
    _report("qbittorrent", True, f"folders set, upload limited to {up // 1024} KB/s, seeding stops when done")


def sync_password(cfg, qb):
    """Keep qBittorrent's own login equal to Scanarr's (used only if someone reaches qBit directly)."""
    a = cfg["auth"]
    fp = _fingerprint(a["username"], a["password"])
    if activity.kv_get("qbit_login_fp") == fp:
        return False
    qb.set_preferences({"web_ui_username": a["username"], "web_ui_password": a["password"]})
    activity.kv_set("qbit_login_fp", fp)
    _report("login", True, "qBittorrent login matches Scanarr's")
    return True


def prowlarr_indexers(cfg, wanted):
    p = cfg["prowlarr"]
    base, key = p["url"].rstrip("/"), p["api_key"]
    h = {"X-Api-Key": key}
    with httpx.Client(timeout=60) as c:
        have = c.get(f"{base}/api/v1/indexer", headers=h)
        have.raise_for_status()
        existing = {i.get("definitionName", "").lower() for i in have.json()} | {i.get("name", "").lower() for i in have.json()}
        todo = [w for w in wanted if w.lower() not in existing]
        if not todo:
            _report("indexers", True, f"{len(existing) and len(have.json())} indexers ready")
            return []
        try:
            schema = c.get(f"{base}/api/v1/indexer/schema", headers=h, timeout=180)  # without internet Prowlarr needs ~100 s to fall back to its saved list
            schema.raise_for_status()
        except httpx.TimeoutException:
            raise RuntimeError("Prowlarr is still downloading its indexer list - it needs internet access") from None
        if not schema.json():
            raise RuntimeError("Prowlarr has no indexer list yet - it needs internet access to download it")
        profiles = c.get(f"{base}/api/v1/appprofile", headers=h).json()
        prof = profiles[0]["id"] if profiles else 1
        by_name = {}
        for s in schema.json():
            by_name.setdefault(s.get("definitionName", "").lower(), s)
            by_name.setdefault(s.get("name", "").lower(), s)
        added, missing, failed = [], [], []
        for w in todo:
            s = by_name.get(w.lower())
            if not s:
                missing.append(w)
                continue
            s = dict(s, enable=True, appProfileId=prof)
            r = c.post(f"{base}/api/v1/indexer", params={"forceSave": "true"}, headers=h, json=s, timeout=150)
            (added if r.status_code < 300 else failed).append(s.get("name", w))
        msg = f"added: {', '.join(added) or 'none'}"
        if missing:
            msg += f"; not found in Prowlarr: {', '.join(missing)}"
        if failed:
            msg += f"; failed: {', '.join(failed)}"
        _report("indexers", not failed, msg)
        if failed and not added:
            raise RuntimeError(msg)
        return added


_next = {"prowlarr": 0.0}
RETRY_SECONDS = 120   # don't hold up the worker: a Prowlarr that's up but can't finish is retried every 2 minutes
STARTING_RETRY = 15   # Prowlarr not answering yet (still starting): try again soon


def run(cfg):
    """Returns True once everything is done. Safe to call every cycle."""
    st = cfg.get("stack") or {}
    if not st.get("managed"):
        return True
    qb = QBit.from_cfg(cfg["qbit"])
    try:
        try:
            qb.version()
        except Exception as e:
            _report("qbittorrent", False, f"waiting for qBittorrent ({str(e)[:80]})")
            return False
        sync_password(cfg, qb)  # every cycle: cheap no-op unless the login changed
        if st.get("bootstrap_done"):
            return True
        rev = str(st.get("rev") or "1")
        if activity.kv_get("bootstrap_qbit") != rev:     # once per install / Repair
            qbit_setup(cfg, qb)
            activity.kv_set("bootstrap_qbit", rev)
    finally:
        qb.close()
    if time.time() < _next["prowlarr"]:
        return False
    try:
        prowlarr_indexers(cfg, st.get("indexers") or [])
    except Exception as e:
        starting = isinstance(e, (httpx.ConnectError, httpx.RemoteProtocolError, httpx.ReadError)) or \
            (isinstance(e, httpx.HTTPStatusError) and e.response.status_code in (502, 503))
        _next["prowlarr"] = time.time() + (STARTING_RETRY if starting else RETRY_SECONDS)
        msg = str(e) if isinstance(e, RuntimeError) else f"waiting for Prowlarr ({str(e)[:80]})"
        _report("indexers", False, ("waiting for Prowlarr to finish starting" if starting else msg[:200] + " - retrying every 2 minutes"))
        return False
    new = config.load()
    new.setdefault("stack", {})["bootstrap_done"] = True
    config.save(new)
    _report("done", True, "setup complete")
    log.info("stack bootstrap complete")
    return True
