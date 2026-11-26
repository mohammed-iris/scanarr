"""SQLite store: activity log, rename history (for undo), review queue, subtitle retries."""
import json
import os
import sqlite3
import threading
import time

from . import config

_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS activity(id INTEGER PRIMARY KEY, ts REAL, torrent TEXT, status TEXT,
    target TEXT, message TEXT);
CREATE TABLE IF NOT EXISTS renames(id INTEGER PRIMARY KEY, ts REAL, label TEXT, src TEXT, dst TEXT,
    subs TEXT, undone INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS reviews(id INTEGER PRIMARY KEY, ts REAL, label TEXT, hash TEXT, kind TEXT,
    reason TEXT, items TEXT, status TEXT DEFAULT 'pending');
CREATE TABLE IF NOT EXISTS subs_pending(id INTEGER PRIMARY KEY, video TEXT, lang TEXT, meta TEXT,
    attempts INTEGER DEFAULT 0, next_try REAL, first_try REAL, ready_path TEXT);
CREATE TABLE IF NOT EXISTS library(key TEXT PRIMARY KEY, path TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS dircache(path TEXT PRIMARY KEY, mtime INTEGER, seen REAL, files TEXT, subdirs TEXT);
CREATE TABLE IF NOT EXISTS watch(id INTEGER PRIMARY KEY, query TEXT, cat TEXT, filters TEXT, created REAL,
    last_check REAL, seen TEXT, hits TEXT);
"""


_local = threading.local()
_ready_paths = set()
_gen = [0]  # bumped when the database file is replaced (restore), so every thread reconnects


def _conn():
    """One connection per thread, created once; schema set up once per database file."""
    path = os.path.join(config.CONFIG_DIR, "scanarr.db")
    if getattr(_local, "gen", None) != _gen[0]:
        for old in getattr(_local, "conns", {}).values():
            try:
                old.close()
            except Exception:
                pass
        _local.conns, _local.gen = {}, _gen[0]
    c = getattr(_local, "conns", {}).get(path)
    if c is not None and os.path.exists(path):
        return c
    os.makedirs(config.CONFIG_DIR, exist_ok=True)
    c = sqlite3.connect(path, timeout=15)
    c.row_factory = sqlite3.Row
    if path not in _ready_paths or not os.path.exists(path):
        c.execute("PRAGMA journal_mode=WAL")  # readers don't block the background writer
        c.executescript(SCHEMA)
        try:  # databases created by an earlier version
            c.execute("ALTER TABLE subs_pending ADD COLUMN ready_path TEXT")
        except sqlite3.OperationalError:
            pass
        _ready_paths.add(path)
    if not hasattr(_local, "conns"):
        _local.conns = {}
    _local.conns[path] = c
    return c


def restore_from_bytes(blob):
    """Replace the whole database with a backup, safely, while the app keeps running."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        tmp = os.path.join(td, "restore.db")
        with open(tmp, "wb") as f:
            f.write(blob)
        src = sqlite3.connect(tmp)
        try:
            with _lock:
                live = _conn()
                src.backup(live)   # online backup API: consistent, WAL-aware
                live.commit()
        finally:
            src.close()
    reset_connections()


def reset_connections():
    """After a restore replaces the database file: every thread reconnects on its next query."""
    with _lock:
        _gen[0] += 1
        _ready_paths.clear()


def _q(sql, args=(), one=False):
    with _lock, _conn() as c:
        cur = c.execute(sql, args)
        if sql.lstrip().upper().startswith(("SELECT",)):
            rows = [dict(r) for r in cur.fetchall()]
            return (rows[0] if rows else None) if one else rows
        return cur.lastrowid


# activity
def log(torrent, status, target="", message=""):
    _q("INSERT INTO activity(ts, torrent, status, target, message) VALUES(?,?,?,?,?)",
       (time.time(), torrent, status, target, message))


def recent(limit=300):
    return _q("SELECT * FROM activity ORDER BY id DESC LIMIT ?", (limit,))


# renames (undo)
def add_rename(label, src, dst, subs):
    return _q("INSERT INTO renames(ts, label, src, dst, subs) VALUES(?,?,?,?,?)",
              (time.time(), label, src, dst, json.dumps(subs)))


def renames(limit=300):
    rows = _q("SELECT * FROM renames ORDER BY id DESC LIMIT ?", (limit,))
    for r in rows:
        r["subs"] = json.loads(r["subs"] or "[]")
    return rows


def get_rename(rid):
    r = _q("SELECT * FROM renames WHERE id=?", (rid,), one=True)
    if r:
        r["subs"] = json.loads(r["subs"] or "[]")
    return r


def set_rename_subs(rid, subs):
    _q("UPDATE renames SET subs=? WHERE id=?", (json.dumps(subs), rid))


def mark_undone(rid):
    _q("UPDATE renames SET undone=1 WHERE id=?", (rid,))


def library_has(dst):
    return _q("SELECT id FROM renames WHERE dst=? AND undone=0", (dst,), one=True) is not None


# review queue
def add_review(label, h, kind, reason, items):
    existing = _q("SELECT id FROM reviews WHERE hash=? AND status='pending'", (h,), one=True) if h else None
    if existing:
        return existing["id"]
    return _q("INSERT INTO reviews(ts, label, hash, kind, reason, items) VALUES(?,?,?,?,?,?)",
              (time.time(), label, h, kind, reason, json.dumps(items)))


def reviews(status="pending"):
    rows = _q("SELECT * FROM reviews WHERE status=? ORDER BY id DESC", (status,))
    for r in rows:
        r["items"] = json.loads(r["items"])
    return rows


def get_review(rid):
    r = _q("SELECT * FROM reviews WHERE id=?", (rid,), one=True)
    if r:
        r["items"] = json.loads(r["items"])
    return r


def set_review_status(rid, status):
    _q("UPDATE reviews SET status=? WHERE id=?", (status, rid))


def review_exists_for(path):
    """True if a pending OR dismissed review covers this file (Unsorted folder), so it isn't re-queued."""
    rows = _q("SELECT items FROM reviews WHERE status IN ('pending','dismissed')")
    return any(any(i.get("src") == path for i in json.loads(r["items"])) for r in rows)


# subtitle retries
def queue_sub(video, lang, meta, delay=86400):
    if _q("SELECT id FROM subs_pending WHERE video=? AND lang=?", (video, lang), one=True):
        return
    now = time.time()
    _q("INSERT INTO subs_pending(video, lang, meta, next_try, first_try) VALUES(?,?,?,?,?)",
       (video, lang, json.dumps(meta), now + delay, now))


def due_subs():
    """Subtitles still to search for (network only - no disk)."""
    rows = _q("SELECT * FROM subs_pending WHERE ready_path IS NULL AND next_try<=?", (time.time(),))
    for r in rows:
        r["meta"] = json.loads(r["meta"] or "{}")
    return rows


def sub_retry_later(sid, delay=86400):
    _q("UPDATE subs_pending SET attempts=attempts+1, next_try=? WHERE id=?", (time.time() + delay, sid))


def drop_sub(sid):
    _q("DELETE FROM subs_pending WHERE id=?", (sid,))


def move_subs_queue(old_video, new_video):
    _q("UPDATE subs_pending SET video=? WHERE video=?", (new_video, old_video))


def pending_subs_count():
    return _q("SELECT COUNT(*) AS n FROM subs_pending", one=True)["n"]


def sub_ready(sid, staged_path):
    _q("UPDATE subs_pending SET ready_path=? WHERE id=?", (staged_path, sid))


def ready_subs():
    """Subtitles already downloaded to the SSD, waiting for the HDD to be awake."""
    rows = _q("SELECT * FROM subs_pending WHERE ready_path IS NOT NULL")
    for r in rows:
        r["meta"] = json.loads(r["meta"] or "{}")
    return rows


# library index (kept on the SSD so "already in library" never has to read the HDD)
def _key(path):
    return os.path.splitext(os.path.normcase(os.path.normpath(path)))[0]


def library_add(path):
    _q("INSERT OR REPLACE INTO library(key, path, ts) VALUES(?,?,?)", (_key(path), path, time.time()))


def library_remove(path):
    _q("DELETE FROM library WHERE key=?", (_key(path),))


def library_replace(paths):
    with _lock, _conn() as c:
        c.execute("DELETE FROM library")
        c.executemany("INSERT OR REPLACE INTO library(key, path, ts) VALUES(?,?,?)",
                      [(_key(p), p, time.time()) for p in paths])


def library_remove_under(folder):
    f = os.path.normcase(os.path.normpath(folder)).rstrip("\\/") + os.sep
    _q("DELETE FROM library WHERE key >= ? AND key < ?", (f, f + "\U0010ffff"))


def library_count():
    r = _q("SELECT COUNT(*) AS n FROM library", one=True)
    return r["n"] if r else 0


# per-folder listing cache: rescans only re-read folders whose modified time changed
def dircache_all():
    return {r["path"]: (r["mtime"], r["seen"], json.loads(r["files"]), json.loads(r["subdirs"]))
            for r in _q("SELECT * FROM dircache")}


def dircache_replace(entries):
    with _lock, _conn() as c:
        c.execute("DELETE FROM dircache")
        c.executemany("INSERT INTO dircache(path, mtime, seen, files, subdirs) VALUES(?,?,?,?,?)",
                      [(p, m, seen, json.dumps(f), json.dumps(sd)) for p, (m, seen, f, sd) in entries.items()])


def library_find(target):
    """Exact file (any extension) or, for a folder target, anything inside it."""
    k = _key(target)
    r = _q("SELECT path FROM library WHERE key=?", (k,), one=True)
    if r:
        return r["path"]
    folder = os.path.normcase(os.path.normpath(target)).rstrip("\\/") + os.sep
    # range on the primary key (uses the index; LIKE would read the whole table)
    r = _q("SELECT path FROM library WHERE key >= ? AND key < ? LIMIT 1", (folder, folder + "\U0010ffff"), one=True)
    return r["path"] if r else None


def kv_get(k, default=None):
    r = _q("SELECT v FROM kv WHERE k=?", (k,), one=True)
    return r["v"] if r else default


def kv_set(k, v):
    _q("INSERT OR REPLACE INTO kv(k, v) VALUES(?,?)", (k, str(v)))


# watchlist
def _wrow(r):
    if r:
        r["filters"] = json.loads(r["filters"] or "{}")
        r["seen"] = json.loads(r["seen"] or "[]")
        r["hits"] = json.loads(r["hits"] or "[]")
    return r


def watch_add(query, cat, filters, seen):
    return _q("INSERT INTO watch(query, cat, filters, created, last_check, seen, hits) VALUES(?,?,?,?,?,?,?)",
              (query, cat, json.dumps(filters), time.time(), time.time(), json.dumps(sorted(set(seen))), "[]"))


def watches():
    return [_wrow(r) for r in _q("SELECT * FROM watch ORDER BY id DESC")]


def watch_get(wid):
    return _wrow(_q("SELECT * FROM watch WHERE id=?", (wid,), one=True))


def watch_update(wid, seen=None, hits=None, checked=True):
    w = watch_get(wid)
    if not w:
        return
    _q("UPDATE watch SET seen=?, hits=?, last_check=? WHERE id=?",
       (json.dumps(sorted(set(seen if seen is not None else w["seen"]))),
        json.dumps(hits if hits is not None else w["hits"]),
        time.time() if checked else w["last_check"], wid))


def watch_delete(wid):
    _q("DELETE FROM watch WHERE id=?", (wid,))


def kv_json(k, default=None):
    v = kv_get(k)
    try:
        return json.loads(v) if v else default
    except ValueError:
        return default


def kv_set_json(k, v):
    kv_set(k, json.dumps(v))
