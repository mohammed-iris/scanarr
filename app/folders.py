"""Read Temp/Completed folders from qBittorrent so they're only configured in one place."""
import os
import time

from .qbit import QBit

_cache = {"ts": 0, "key": None, "val": None}


class FoldersUnavailable(Exception):
    pass


def _map(p, pm):
    from .config import map_path
    p = map_path(p, pm)
    return os.path.normpath(p) if p else ""


def from_qbit(cfg, max_age=60, qb=None):
    key = (cfg["qbit"].get("url"), cfg["qbit"].get("api_key"), cfg["qbit"].get("username"))
    if _cache["key"] == key and time.time() - _cache["ts"] < max_age:
        return _cache["val"]
    own = qb is None
    qb = qb or QBit.from_cfg(cfg["qbit"])
    try:
        p = qb.preferences()
    except Exception as e:
        raise FoldersUnavailable(f"can't read folders from qBittorrent: {e}") from e
    finally:
        if own:
            qb.close()
    pm = cfg.get("path_map") or {}
    val = {"completed": _map(p.get("save_path", ""), pm),
           "temp": _map(p.get("temp_path", ""), pm) if p.get("temp_path_enabled") else ""}
    if not val["completed"]:
        raise FoldersUnavailable("qBittorrent has no Default Save Path set")
    _cache.update(ts=time.time(), key=key, val=val)
    return val


def attach(cfg, qb=None):
    """Return cfg with cfg["folders"] filled from qBittorrent."""
    cfg["folders"] = from_qbit(cfg, qb=qb)
    return cfg


def clear():
    _cache.update(ts=0, key=None, val=None)
