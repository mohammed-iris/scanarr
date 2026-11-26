"""Settings stored as JSON in CONFIG_DIR (default /config)."""
import copy
import json
import os
import threading

CONFIG_DIR = os.environ.get("CONFIG_DIR", "/config")
SETTINGS_FILE = os.path.join(CONFIG_DIR, "settings.json")

# Download types. Movies and TV are renamed; everything else keeps its original name.
KINDS = ["movies", "tv", "audio", "books", "software", "games", "xxx", "other"]
VIDEO_KINDS = ("movies", "tv")

DEFAULTS = {
    # All indexers you add in Prowlarr show up here automatically
    "prowlarr": {"url": "http://prowlarr:9696", "api_key": ""},
    # Optional extra Torznab feeds not in Prowlarr
    "indexers": [],  # [{"name": "", "url": "", "apikey": "", "enabled": True}]
    # api_key (qBittorrent 5.2+, Options > Web UI > API Key) or username/password
    "qbit": {"url": "http://qbittorrent:8080", "api_key": "", "username": "", "password": ""},
    # qBittorrent category per download type
    "categories": {k: k for k in KINDS},
    # Folders are read from qBittorrent (Options > Downloads):
    #   Default Save Path = Completed, "another path for incomplete torrents" = Temp.
    # Scanarr must see the same paths as qBit (same /data mount in Docker; same drive on Windows).
    # Sub-folders inside "completed"
    "subfolders": {
        "movies": "Movies", "tv": "TV", "audio": "Audio", "books": "Books",
        "software": "Software", "games": "Games", "xxx": "XXX", "other": "Other",
        "incoming": "Incoming",   # where qBit puts movie/TV downloads before renaming
        "unsorted": "Unsorted",   # drop files here by hand and Scanarr renames them
    },
    # If qBit reports paths differently from how Scanarr sees them (rarely needed)
    "path_map": {"from": "", "to": ""},
    "movie_folders": True,  # Movies/Title (Year)/Title (Year).mkv vs flat
    "tmdb_api_key": "",
    "opensubtitles": {"api_key": "", "username": "", "password": ""},
    "subtitle_languages": ["en"],
    # Search result preferences: used for junk flags and the "Best pick" badge
    "prefs": {"quality": "1080p", "max_gb_movie": 4, "max_gb_episode": 1.5,
              "hide_cam": True, "hide_suspicious": False},
    "indexer_timeout": 15,     # seconds; slow indexers are cut off so searches stay fast
    "ntfy_url": "",            # optional push notifications, e.g. https://ntfy.sh/your-secret-topic
    "watch_hours": 12,         # how often saved searches (watchlist) are re-checked
    "subtitle_retry_days": 7,
    "auto_remove": True,       # remove finished torrents from qBit (no seeding)
    "min_free_gb": 2,          # refuse downloads that would leave less than this free
    "disk_guard_gb": 10,       # pause downloads when Temp or Completed has less than this free (0 = off)
    # slower downloads during these hours (qBittorrent's own scheduler; e.g. keep the internet free in the evening)
    "speed_schedule": {"enabled": False, "start": "18:00", "end": "23:59", "days": "all", "dl_kbps": 500, "up_kbps": 20},
    "dead_hours": 6,           # no progress for this long = dead
    "dead_action": "flag",     # flag | remove
    "guard": True,
    "process_external": True,  # also rename/clean up torrents added directly in qBittorrent (no category)
    "poll_seconds": 30,        # how often qBit's API is checked (API only, no disk access)
    # HDD-friendly schedule
    "rename_mode": "immediate",      # immediate | window
    "rename_window": {"start": "02:00", "end": "06:00"},
    # Unsorted folder scan, saving late subtitles, library index refresh:
    #   awake = only when the HDD is already spinning (a download just finished) - never wakes it
    #   daily = at housekeeping_time (wakes the HDD once a day)   off = only on "Check now"
    "housekeeping": "awake",
    "housekeeping_time": "04:00",
    # Web UI login. Printed in the logs at every start (docker logs scanarr).
    "auth": {"username": "admin", "password": "", "secret": ""},
}

_lock = threading.Lock()
_cache = {"mtime": None, "data": None}  # settings are read from disk only when the file changes


def _merge(base, override):
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load():
    """Fresh copy of the settings (callers may modify it). Cached until settings.json changes."""
    with _lock:
        try:
            mtime = os.stat(SETTINGS_FILE).st_mtime_ns
        except FileNotFoundError:
            return copy.deepcopy(DEFAULTS)
        if _cache["mtime"] != (SETTINGS_FILE, mtime):
            try:
                with open(SETTINGS_FILE, encoding="utf-8") as f:
                    data = _merge(DEFAULTS, json.load(f))
            except ValueError:  # half-written or hand-edited badly: keep last good copy
                if _cache["data"] is not None:
                    return copy.deepcopy(_cache["data"])
                raise
            _cache.update(mtime=(SETTINGS_FILE, mtime), data=data)
        return copy.deepcopy(_cache["data"])


def save(data):
    with _lock:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        merged = _merge(DEFAULTS, data)
        tmp = SETTINGS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(merged, f, indent=2)
        os.replace(tmp, SETTINGS_FILE)
        _cache.update(mtime=None, data=None)
        return copy.deepcopy(merged)


def completed_dir(cfg):
    """cfg["folders"] is filled in from qBittorrent by folders.attach()."""
    return os.path.normpath(cfg["folders"]["completed"])


def kind_dir(cfg, kind):
    return os.path.join(completed_dir(cfg), cfg["subfolders"].get(kind, kind))


def map_path(p, pm):
    """Translate a path as qBittorrent sees it into the path Scanarr sees (Settings > Path map).
    Handles Windows paths (C:\\Scanarr\\completed) mapped into a container (/data/completed)."""
    src, dst = (pm or {}).get("from") or "", (pm or {}).get("to") or ""
    if not (p and src):
        return p
    norm = lambda x: x.replace("\\", "/").rstrip("/")
    pn, sn = norm(p), norm(src)
    if pn.lower() == sn.lower() or pn.lower().startswith(sn.lower() + "/"):
        rest = pn[len(sn):].lstrip("/")
        return os.path.join(dst, *rest.split("/")) if rest else dst
    return p


def unmap_path(p, pm):
    """The reverse: a path as Scanarr sees it -> the path qBittorrent understands (used when adding torrents)."""
    src, dst = (pm or {}).get("from") or "", (pm or {}).get("to") or ""
    if not (p and src and dst):
        return p
    pn, dn = p.replace("\\", "/").rstrip("/"), dst.replace("\\", "/").rstrip("/")
    if pn == dn or pn.startswith(dn + "/"):
        rest = pn[len(dn):].lstrip("/")
        sep = "\\" if "\\" in src else "/"
        base = src.rstrip("\\/")
        return base + (sep + rest.replace("/", sep) if rest else "")
    return p
