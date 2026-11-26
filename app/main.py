"""Scanarr: one search for all your Prowlarr indexers, send to qBittorrent,
clean renames + subtitles for movies and TV, per-type folders for everything else."""
import asyncio
import copy
import logging
import os
import time

import httpx
from contextlib import asynccontextmanager

from fastapi import Body, FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware

from . import activity, auth, config, folders, indexers, naming, processor, proxy, watch
from .qbit import QBit, QBitError

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("scanarr")

STATIC = os.path.join(os.path.dirname(__file__), "static")
_wake = asyncio.Event()
SECRET_MASK = "••••••••"
SECRETS = [("qbit", "password"), ("qbit", "api_key"), ("prowlarr", "api_key"),
           ("opensubtitles", "api_key"), ("opensubtitles", "password"), (None, "tmdb_api_key"),
           ("auth", "password")]
_status = {"last_run": None, "last_error": None}
_manual = {"flag": False}
_loop = {"loop": None}


def wake(manual=False):
    """Wake the background worker now. Safe to call from any thread (sync endpoints run in a thread pool)."""
    if manual:
        _manual["flag"] = True
    loop = _loop["loop"]
    if loop is not None and loop.is_running():
        loop.call_soon_threadsafe(_wake.set)


async def _worker():
    while True:
        cfg = config.load()
        manual, _manual["flag"] = _manual["flag"], False
        try:
            n = await asyncio.to_thread(processor.run_once, cfg, manual)
            _status.update(last_run=time.time(), last_error=None)
            if n:
                log.info("processed %d item(s)", n)
        except (QBitError, folders.FoldersUnavailable) as e:
            _status.update(last_run=time.time(), last_error=str(e))
            log.warning("%s", e)
        except Exception as e:
            _status.update(last_run=time.time(), last_error=str(e))
            log.exception("worker run failed")
        try:
            await watch.due(cfg)  # internet only
        except Exception:
            log.exception("watchlist check failed")
        try:
            await asyncio.wait_for(_wake.wait(), timeout=max(int(cfg.get("poll_seconds", 30)), 10))
        except asyncio.TimeoutError:
            pass
        _wake.clear()


@asynccontextmanager
async def lifespan(_app):
    auth.ensure(announce=True)
    _loop["loop"] = asyncio.get_running_loop()
    task = asyncio.create_task(_worker())
    yield
    task.cancel()


app = FastAPI(title="Scanarr", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=1024)  # ~4x smaller pages, nicer on mobile
app.mount("/static", StaticFiles(directory=STATIC), name="static")

OPEN_EXACT = {"/login", "/api/login"}


@app.middleware("http")
async def require_login(request: Request, call_next):
    p = request.url.path
    if p not in OPEN_EXACT and not p.startswith("/static/") and not auth.valid(request.cookies.get(auth.COOKIE)):
        if p.startswith("/api/"):
            return JSONResponse({"detail": "login required"}, status_code=401)
        return RedirectResponse("/login")
    return await call_next(request)


def _qb():
    return QBit.from_cfg(config.load()["qbit"])


def free_space(cfg, qb=None):
    """Free bytes per folder without touching the HDD: Completed from qBit's own report,
    Temp (SSD) directly."""
    out = {}
    own = qb is None
    qb = qb or _qb()
    try:
        out["completed"] = qb.free_space()
    except Exception:
        out["completed"] = None
    finally:
        if own:
            qb.close()
    t = cfg["folders"].get("temp")
    if t:
        try:
            out["temp"] = processor.free_bytes(t)
        except OSError:
            out["temp"] = None
    return out


def _qpath(cfg, p):
    """Scanarr path -> qBittorrent path (only differs when a path map is set)."""
    return config.unmap_path(p, cfg.get("path_map")) if p else p


def _cfg_with_folders():
    cfg = config.load()
    try:
        folders.attach(cfg)
    except folders.FoldersUnavailable as e:
        raise HTTPException(502, str(e))
    return cfg


# ---------------- single sign-on gateway to qBittorrent / Prowlarr ----------------
_METHODS = ["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"]


@app.api_route("/qbit", methods=_METHODS, include_in_schema=False)
@app.api_route("/qbit/{path:path}", methods=_METHODS, include_in_schema=False)
async def qbit_gateway(request: Request, path: str = ""):
    return await proxy.http_proxy("qbit", request, path)


@app.api_route("/prowlarr", methods=_METHODS, include_in_schema=False)
@app.api_route("/prowlarr/{path:path}", methods=_METHODS, include_in_schema=False)
async def prowlarr_gateway(request: Request, path: str = ""):
    return await proxy.http_proxy("prowlarr", request, path)


@app.websocket("/prowlarr/{path:path}")
async def prowlarr_ws(ws: WebSocket, path: str):
    await proxy.ws_proxy("prowlarr", ws, path)


@app.websocket("/qbit/{path:path}")
async def qbit_ws(ws: WebSocket, path: str):
    await proxy.ws_proxy("qbit", ws, path)


# ---------------- pages & login ----------------
@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC, "index.html"))


@app.get("/login")
def login_page():
    return FileResponse(os.path.join(STATIC, "login.html"))


_fails = {}  # client ip -> [timestamps of recent failed logins]


@app.post("/api/login")
def login(request: Request, payload: dict = Body(...)):
    ip = request.client.host if request.client else "?"
    now = time.time()
    recent = [t for t in _fails.get(ip, []) if now - t < 300]
    if len(recent) >= 5:
        raise HTTPException(429, "Too many wrong attempts - wait 5 minutes (the login is in the Scanarr log).")
    a = auth.check_login(payload.get("username"), payload.get("password"))
    if not a:
        _fails[ip] = recent + [now]
        time.sleep(1)  # slow down guessing
        raise HTTPException(401, "Wrong username or password")
    _fails.pop(ip, None)
    r = JSONResponse({"ok": True})
    r.set_cookie(auth.COOKIE, auth.make_token(a), max_age=auth.MAX_AGE, httponly=True, samesite="lax")
    return r


@app.post("/api/logout")
def logout():
    r = JSONResponse({"ok": True})
    r.delete_cookie(auth.COOKIE)
    return r


# ---------------- search ----------------
@app.get("/api/indexers")
async def list_indexers(refresh: bool = False):
    if refresh:
        indexers.clear_prowlarr_cache()
    idx, errors = await indexers.all_indexers(config.load())
    return {"indexers": [{"name": i["name"], "source": i["source"]} for i in idx], "errors": errors}


@app.get("/api/search")
async def search(q: str, cat: str = "all"):
    cfg = config.load()
    idx, errors = await indexers.all_indexers(cfg)
    if not idx:
        msg = errors[0]["error"] if errors else "No indexers yet. Add your Prowlarr URL and API key in Settings."
        raise HTTPException(400, msg)
    out = await indexers.search_all(idx, q.strip(), cat, timeout=float(cfg.get("indexer_timeout") or 15))
    indexers.annotate(out["results"], cfg.get("prefs") or {})
    out["errors"] = errors + out["errors"]
    return out


@app.get("/api/indexer-health")
async def indexer_health():
    import time as _t
    idx, errors = await indexers.all_indexers(config.load())
    out = []
    for i in idx:
        n = i["name"]
        h = indexers.HEALTH.get(n, {})
        tot = h.get("ok", 0) + h.get("fail", 0)
        status = ("skipped" if indexers.skipped(n) else "failing" if h.get("streak") else
                  "slow" if (h.get("last_ms") or 0) > 8000 else "ok" if tot else "unknown")
        out.append({"name": n, "status": status, "last_ms": h.get("last_ms"), "success": round(100 * h.get("ok", 0) / tot) if tot else None,
                    "searches": tot, "last_error": h.get("last_error"),
                    "skip_mins": max(0, int((h.get("skip_until", 0) - _t.time()) / 60)) if indexers.skipped(n) else 0})
    return {"indexers": out, "errors": errors}


@app.post("/api/indexer-health/reset")
def indexer_health_reset(payload: dict = Body(default={})):
    indexers.reset_health(payload.get("name"))
    return {"ok": True}


def _target_for(title, kind, cfg):
    i = naming.parse(title, "movie" if kind == "movies" else "episode")
    if kind == "movies":
        return naming.movie_name(i["title"] or title, i["year"]), naming.movie_target(
            config.kind_dir(cfg, "movies"), i["title"] or title, i["year"], ".mkv", cfg.get("movie_folders", True))
    if not i["episodes"]:
        return f"{naming.clean(i['title'] or title)} (season pack, each episode renamed)", \
            os.path.join(config.kind_dir(cfg, "tv"), naming.clean(i["title"] or title))
    return naming.episode_name(i["title"], i["season"] or 1, i["episodes"]), naming.episode_target(
        config.kind_dir(cfg, "tv"), i["title"], i["season"] or 1, i["episodes"], ".mkv")


@app.get("/api/preview")
def preview(title: str, kind: str = "movies"):
    """Clean name a release will get, where it lands, and whether you already have it."""
    cfg = config.load()
    try:
        folders.attach(cfg)
    except folders.FoldersUnavailable:
        cfg = None
    if kind not in config.VIDEO_KINDS:
        return {"name": None, "folder": config.kind_dir(cfg, kind) if cfg else None, "exists": None}
    if cfg is None:
        name, _ = _target_for(title, kind, {**config.load(), "folders": {"completed": "", "temp": ""}})
        return {"name": name, "folder": None, "exists": None}
    name, target = _target_for(title, kind, cfg)
    found = activity.library_find(target)  # SSD database lookup - never reads the HDD
    return {"name": name, "folder": os.path.dirname(target),
            "exists": os.path.relpath(found, config.completed_dir(cfg)) if found else None}


@app.post("/api/send")
def send(payload: dict = Body(...)):
    cfg = _cfg_with_folders()
    kind = payload.get("kind")
    if kind not in config.KINDS:
        raise HTTPException(400, f"kind must be one of {', '.join(config.KINDS)}")
    size = int(payload.get("size") or 0)
    if not payload.get("force"):
        need = size + float(cfg.get("min_free_gb") or 0) * 1024 ** 3
        fs = free_space(cfg)
        for label, key in (("Temp", "temp"), ("Completed", "completed")):
            free = fs.get(key)
            if free is not None:
                if free < need:
                    raise HTTPException(409, f"Not enough space in {label}: {free / 1024**3:.1f} GB free, "
                                             f"needs {need / 1024**3:.1f} GB (incl. {cfg.get('min_free_gb')} GB reserve)")
    cat = cfg["categories"].get(kind, kind)
    savepath = config.kind_dir(cfg, "incoming" if kind in config.VIDEO_KINDS else kind)
    qb = _qb()
    try:
        qb.add(payload["download"], cat, _qpath(cfg, savepath), _qpath(cfg, cfg["folders"]["temp"]) or None)
    except QBitError as e:
        raise HTTPException(502, str(e))
    finally:
        qb.close()
    return {"ok": True, "savepath": savepath}


# ---------------- peek (file list before downloading) ----------------
def _btih(download, infohash):
    import base64
    import re as _re
    h = (infohash or "").lower()
    if len(h) == 40:
        return h
    m = _re.search(r"urn:btih:([a-zA-Z0-9]+)", download or "")
    if m:
        v = m.group(1)
        if len(v) == 40:
            return v.lower()
        if len(v) == 32:
            return base64.b32decode(v.upper()).hex()
    return None


@app.post("/api/peek")
def peek_start(payload: dict = Body(...)):
    cfg = _cfg_with_folders()
    kind = payload.get("kind") if payload.get("kind") in config.KINDS else "other"
    h = _btih(payload.get("download"), payload.get("infohash"))
    if not h:
        raise HTTPException(400, "This result has no info hash, so it can't be peeked.")
    qb = _qb()
    try:
        existing = qb.info(h)
        if existing and processor.PEEK_TAG not in (existing.get("tags") or ""):
            raise HTTPException(409, "Already in qBittorrent.")
        if not existing:
            savepath = config.kind_dir(cfg, "incoming" if kind in config.VIDEO_KINDS else kind)
            qb.add(payload["download"], cfg["categories"].get(kind, kind), _qpath(cfg, savepath),
                   _qpath(cfg, cfg["folders"]["temp"]) or None, metadata_only=True, tags=processor.PEEK_TAG)
    except QBitError as e:
        raise HTTPException(502, str(e))
    finally:
        qb.close()
    return {"hash": h}


@app.get("/api/peek/{h}")
def peek_get(h: str, kind: str = "other"):
    qb = _qb()
    try:
        t = qb.info(h)
        if not t:
            raise HTTPException(404, "Torrent is gone (blocked or removed).")
        files = qb.files(h)
    except QBitError as e:
        raise HTTPException(502, str(e))
    finally:
        qb.close()
    if not files:
        return {"pending": True, "state": t.get("state"), "peers": t.get("num_seeds", 0)}
    items, warns = processor.peek_info(files, kind)
    return {"pending": False, "files": items, "warnings": warns, "name": t.get("name")}


@app.post("/api/peek/{h}/start")
def peek_go(h: str, payload: dict = Body(...)):
    cfg = _cfg_with_folders()
    kind = payload.get("kind") if payload.get("kind") in config.KINDS else "other"
    skip = [int(i) for i in payload.get("skip", [])]
    qb = _qb()
    try:
        t = qb.info(h)
        if not t:
            raise HTTPException(404, "Torrent is gone.")
        n_files = len(qb.files(h))
        if n_files and len(skip) >= n_files:
            raise HTTPException(400, "Select at least one file.")
        qb.file_prio(h, skip, 0)
        cat = cfg["categories"].get(kind, kind)
        if t.get("category") != cat:
            qb.set_category(h, cat)
            qb.set_location(h, _qpath(cfg, config.kind_dir(cfg, "incoming" if kind in config.VIDEO_KINDS else kind)))
        qb.remove_tag(h, processor.PEEK_TAG)
        qb.control("start", [h])
    except QBitError as e:
        raise HTTPException(502, str(e))
    finally:
        qb.close()
    return {"ok": True}


@app.post("/api/peek/{h}/cancel")
def peek_cancel(h: str):
    qb = _qb()
    try:
        t = qb.info(h)
        if t and processor.PEEK_TAG in (t.get("tags") or ""):
            qb.delete(h, delete_files=True)
    except QBitError as e:
        raise HTTPException(502, str(e))
    finally:
        qb.close()
    return {"ok": True}


# ---------------- downloads (qBittorrent) ----------------
@app.get("/api/downloads")
def downloads():
    cfg = config.load()
    rev = {v: k for k, v in cfg["categories"].items()}
    qb = _qb()
    try:
        ts, tr = qb.list(), qb.transfer()
    except Exception as e:
        raise HTTPException(502, str(e))
    finally:
        qb.close()
    keep = ("hash", "name", "size", "progress", "dlspeed", "upspeed", "num_seeds", "num_leechs",
            "eta", "state", "ratio", "added_on", "category", "tags", "save_path")
    return {
        "torrents": [{**{k: t.get(k) for k in keep}, "kind": rev.get(t.get("category"), "other")}
                     for t in ts if processor.PEEK_TAG not in (t.get("tags") or "")],
        "dl_speed": tr.get("dl_info_speed", 0), "up_speed": tr.get("up_info_speed", 0),
        "connection": tr.get("connection_status"),
    }


@app.post("/api/downloads/{action}")
def control(action: str, payload: dict = Body(...)):
    hashes = [h for h in payload.get("hashes", []) if h]
    if not hashes:
        raise HTTPException(400, "no torrents selected")
    qb = _qb()
    try:
        if action == "delete":
            qb.delete("|".join(hashes), delete_files=bool(payload.get("delete_files")))
        elif action in ("stop", "start", "recheck", "reannounce"):
            qb.control(action, hashes)
        else:
            raise HTTPException(400, "unknown action")
    except QBitError as e:
        raise HTTPException(502, str(e))
    finally:
        qb.close()
    return {"ok": True}


# ---------------- watchlist ----------------
@app.get("/api/watch")
def watch_list():
    return activity.watches()


@app.post("/api/watch")
def watch_create(payload: dict = Body(...)):
    q = (payload.get("query") or "").strip()
    if not q:
        raise HTTPException(400, "Search for something first.")
    wid = activity.watch_add(q, payload.get("cat") or "all", payload.get("filters") or {}, payload.get("seen") or [])
    return {"ok": True, "id": wid}


@app.post("/api/watch/{wid}/check")
async def watch_check(wid: int):
    w = activity.watch_get(wid)
    if not w:
        raise HTTPException(404, "not found")
    return {"new": await watch.check(w, config.load())}


@app.post("/api/watch/{wid}/clear")
def watch_clear(wid: int):
    activity.watch_update(wid, hits=[], checked=False)
    return {"ok": True}


@app.post("/api/watch/{wid}/delete")
def watch_remove(wid: int):
    activity.watch_delete(wid)
    return {"ok": True}


@app.post("/api/test/notify")
def test_notify():
    if not watch.notify(config.load(), "Scanarr test", "Notifications are working.", tags="white_check_mark"):
        raise HTTPException(400, "Set an ntfy URL first (or it couldn't be reached).")
    return {"ok": True}


# ---------------- review / renamed / activity ----------------
@app.get("/api/reviews")
def get_reviews():
    cfg = config.load()
    try:
        folders.attach(cfg)
        root = config.completed_dir(cfg)
    except folders.FoldersUnavailable:
        root = None
    out = []
    for r in activity.reviews():
        for it in r["items"]:
            it["dst_rel"] = os.path.relpath(it["dst"], root) if root and it.get("dst") else ""
            it["src_name"] = os.path.basename(it["src"])
        out.append(r)
    return out


@app.post("/api/reviews/{rid}/approve")
def approve(rid: int, payload: dict = Body(...)):
    try:
        processor.approve_review(rid, payload.get("targets", []), config.load())
    except processor.NeedsReview as e:
        raise HTTPException(409, e.args[0])
    except (ValueError, folders.FoldersUnavailable) as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.post("/api/reviews/{rid}/dismiss")
def dismiss(rid: int):
    activity.set_review_status(rid, "dismissed")
    return {"ok": True}


@app.get("/api/renames")
def get_renames():
    return activity.renames()


@app.post("/api/renames/{rid}/undo")
def undo(rid: int):
    try:
        processor.undo(rid)
    except (ValueError, OSError) as e:
        raise HTTPException(409, str(e))
    return {"ok": True}


@app.get("/api/renames/{rid}/subs")
def sub_options(rid: int, lang: str = "en"):
    osub, _ = processor.make_clients(config.load())
    try:
        if not osub.enabled:
            raise HTTPException(400, "Add your OpenSubtitles API key in Settings first.")
        return processor.subtitle_options(rid, lang, osub)
    except ValueError as e:
        raise HTTPException(404, str(e))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"OpenSubtitles: {e}"[:200])
    finally:
        osub.close()


@app.post("/api/renames/{rid}/subs")
def sub_replace(rid: int, payload: dict = Body(...)):
    osub, _ = processor.make_clients(config.load())
    try:
        dest = processor.subtitle_replace(rid, payload.get("lang", "en"), payload["file_id"], osub)
        return {"ok": True, "path": dest}
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(502, f"OpenSubtitles: {e}"[:200])
    finally:
        osub.close()


@app.get("/api/setup-status")
def setup_status():
    """For the setup wizard's final check (and the Apps panel): has the first-start wiring finished?"""
    from . import bootstrap
    st = config.load().get("stack") or {}
    return {"managed": bool(st.get("managed")), "done": bool(st.get("bootstrap_done")), "report": bootstrap.report()}


@app.get("/api/apps")
async def apps(request: Request):
    """Apps panel: qBittorrent/Prowlarr (opened through Scanarr), Jellyfin, versions, setup report, updates."""
    from . import bootstrap, fswatch, updates
    from .version import __version__
    cfg = config.load()
    st = cfg.get("stack") or {}
    host = request.url.hostname or "localhost"
    tg = proxy.targets(cfg) if st.get("managed") else {}

    def qbit_part():
        qb = _qb()
        try:
            v, tr = qb.version(), qb.transfer()
            return {"ok": True, "version": v, "connection": tr.get("connection_status"), "dht": tr.get("dht_nodes")}
        except Exception as e:
            return {"ok": False, "msg": str(e)[:120]}
        finally:
            qb.close()

    async def prowlarr_part():
        p = cfg["prowlarr"]
        if not p.get("api_key"):
            return {"ok": False, "msg": "not set up"}
        try:
            async with httpx.AsyncClient(timeout=8) as c:
                r = await c.get(p["url"].rstrip("/") + "/api/v1/system/status", headers={"X-Api-Key": p["api_key"]})
                r.raise_for_status()
                return {"ok": True, "version": r.json().get("version")}
        except Exception as e:
            return {"ok": False, "msg": str(e)[:120]}

    q, pr = await asyncio.gather(asyncio.to_thread(qbit_part), prowlarr_part())
    out = {
        "scanarr": {"version": __version__, "managed": bool(st.get("managed"))},
        "qbit": {**q, "open": "/qbit/" if "qbit" in tg else None, "torrent_port": st.get("torrent_port")},
        "prowlarr": {**pr, "open": "/prowlarr/" if "prowlarr" in tg else None},
        "jellyfin": ({"open": f"{request.url.scheme}://{host}:{st.get('jellyfin_port') or 8096}/"} if st.get("jellyfin") else None),
        "setup": {"done": bool(st.get("bootstrap_done")), "report": bootstrap.report()} if st.get("managed") else None,
        "update": await asyncio.to_thread(updates.info),
        "library": activity.kv_json("library_stats") or {},
        "events": {"active": fswatch.STATE["active"], "reason": fswatch.STATE["reason"]},
    }
    return out


@app.post("/api/update")
def update_start(body: dict = Body(...)):
    from . import updates
    v = str(body.get("version") or "").lstrip("v")
    try:
        return updates.start(v)
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    except httpx.HTTPError as e:
        raise HTTPException(502, f"updater not reachable: {e}")


@app.get("/api/update/status")
def update_status():
    from . import updates
    try:
        return updates.status()
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    except httpx.HTTPError:
        return {"job": {"running": True, "steps": [{"name": "Restarting", "status": "run", "detail": "Scanarr is being replaced…"}]}}


@app.get("/api/activity")
def get_activity():
    return activity.recent()


@app.post("/api/process")
def process_now():
    wake(manual=True)
    return {"ok": True}


# ---------------- health (status bar) ----------------
_health_cache = {"ts": 0, "val": None}
_disk_cache = {"ts": 0, "key": None, "val": None}  # free-space checks every 10 min, not every poll


def _progress():
    p = dict(processor.PROGRESS)
    u = activity.kv_json("unsorted_progress") or {}
    if not p and u.get("left"):
        p = {"job": "Sorting Unsorted", "waiting": True, "left": u["left"]}
    lib = activity.kv_json("library_stats") or {}
    from . import fswatch
    return {**p, "library_files": lib.get("files"), "library_ts": lib.get("ts"),
            "events": {"active": fswatch.STATE["active"], "reason": fswatch.STATE["reason"]}}


@app.get("/api/progress")
def progress():
    return _progress()


@app.get("/api/health")
async def health():
    if time.time() - _health_cache["ts"] < 20 and _health_cache["val"]:
        return _health_cache["val"]
    cfg = config.load()
    out = {"worker": _status, "progress": _progress(), "reviews": len(activity.reviews()), "subs_pending": activity.pending_subs_count(),
           "watch_hits": sum(len(w["hits"]) for w in activity.watches())}
    # Prowlarr
    try:
        found = await indexers.prowlarr_indexers(cfg["prowlarr"])
        out["prowlarr"] = {"ok": bool(found) or not cfg["prowlarr"].get("api_key"),
                           "msg": f"{len(found)} indexers" if cfg["prowlarr"].get("api_key") else "not set up",
                           "count": len(found), "names": [i["name"] for i in found]}
    except Exception as e:
        out["prowlarr"] = {"ok": False, "msg": str(e)[:120], "count": 0, "names": []}

    # qBit + folders + disk
    def qbit_part():
        res = {}
        qb = _qb()
        try:
            tr = qb.transfer()
            res["qbit"] = {"ok": True, "dl": tr.get("dl_info_speed", 0), "up": tr.get("up_info_speed", 0)}
            f = folders.from_qbit(cfg, qb=qb)
            res["folders"] = f
            res["disk"] = {k: v for k, v in free_space({"folders": f}, qb).items() if v is not None}
        except Exception as e:
            res.setdefault("qbit", {"ok": False, "msg": str(e)[:120]})
            res.setdefault("folders", None)
        finally:
            qb.close()
        return res
    out.update(await asyncio.to_thread(qbit_part))
    out["min_free_gb"] = cfg.get("min_free_gb")
    out["schedule"] = {"renaming_now": processor.in_window(cfg), "mode": cfg.get("rename_mode"),
                       "window": cfg.get("rename_window"), "housekeeping": cfg.get("housekeeping_time")}

    # OpenSubtitles quota
    def subs_part():
        osub, _ = processor.make_clients(cfg)
        try:
            if not osub.enabled:
                return {"ok": None, "msg": "no API key"}
            q = osub.quota()
            return {"ok": q is None or q > 0, "remaining": q,
                    "msg": f"{q} downloads left today" if q is not None else "logged out (add username/password)"}
        except Exception as e:
            return {"ok": False, "msg": str(e)[:120]}
        finally:
            osub.close()
    out["subs"] = await asyncio.to_thread(subs_part)
    _health_cache.update(ts=time.time(), val=out)
    return out


# ---------------- settings ----------------
def _mask(cfg):
    cfg = copy.deepcopy(cfg)
    for sect, key in SECRETS:
        d = cfg[sect] if sect else cfg
        if d.get(key):
            d[key] = SECRET_MASK
    cfg["auth"] = {k: v for k, v in cfg["auth"].items() if k != "secret"}
    for i in cfg["indexers"]:
        if i.get("apikey"):
            i["apikey"] = SECRET_MASK
    return cfg


@app.get("/api/settings")
def get_settings():
    return _mask(config.load())


@app.post("/api/settings")
def put_settings(new: dict = Body(...)):
    old = config.load()
    for sect, key in SECRETS:  # keep existing secrets when the UI sends back the mask
        nd, od = (new.setdefault(sect, {}), old[sect]) if sect else (new, old)
        if nd.get(key) == SECRET_MASK or (sect == "auth" and not nd.get(key)):
            nd[key] = od.get(key, "")
    new.setdefault("auth", {})["secret"] = old["auth"]["secret"]
    if not str(new["auth"].get("username") or "").strip():
        new["auth"]["username"] = old["auth"]["username"]  # never allow a blank username (lock-out)
    new["auth"]["username"] = str(new["auth"]["username"]).strip()
    old_keys = {i.get("url"): i.get("apikey", "") for i in old["indexers"]}
    for i in new.get("indexers", []):
        if i.get("apikey") == SECRET_MASK:
            i["apikey"] = old_keys.get(i.get("url"), "")
    new.pop("folders", None)
    saved = config.save(new)
    indexers.clear_prowlarr_cache()
    folders.clear()
    _health_cache["ts"] = 0
    wake()
    resp = JSONResponse(_mask(saved))
    if (saved["auth"]["password"], saved["auth"]["username"]) != (old["auth"]["password"], old["auth"]["username"]):
        auth.ensure(announce=True)  # print the new login to the log
        resp.set_cookie(auth.COOKIE, auth.make_token(saved["auth"]), max_age=auth.MAX_AGE,
                        httponly=True, samesite="lax")
    return resp


# ---------------- backup / restore ----------------
@app.get("/api/backup")
def backup():
    import io
    import sqlite3
    import tempfile
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        if not os.path.exists(config.SETTINGS_FILE):
            config.save(config.load())
        with open(config.SETTINGS_FILE, "rb") as f:
            z.writestr("settings.json", f.read())
        dbp = os.path.join(config.CONFIG_DIR, "scanarr.db")
        if os.path.exists(dbp):
            with tempfile.TemporaryDirectory() as td:  # consistent copy even while running
                tmp = os.path.join(td, "scanarr.db")
                src, dst = sqlite3.connect(dbp), sqlite3.connect(tmp)
                with dst:
                    src.backup(dst)
                src.close()
                dst.close()
                with open(tmp, "rb") as f:
                    z.writestr("scanarr.db", f.read())
        z.writestr("README.txt", "Scanarr backup: settings (including API keys and login) and history.\n"
                                 "Restore it in Scanarr > Settings > Backup. Keep it private.\n")
    name = time.strftime("scanarr-backup-%Y%m%d-%H%M.zip")
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.post("/api/restore")
async def restore(request: Request):
    import io
    import json as _json
    import zipfile
    data = await request.body()
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
        settings = _json.loads(z.read("settings.json"))
        db = z.read("scanarr.db") if "scanarr.db" in z.namelist() else None
    except (zipfile.BadZipFile, KeyError, ValueError):
        raise HTTPException(400, "That isn't a Scanarr backup file.")
    if db and not db.startswith(b"SQLite format 3"):
        raise HTTPException(400, "The history database in this backup is damaged.")
    old_auth = config.load()["auth"]
    config.save(settings)
    if db:
        activity.restore_from_bytes(db)
    folders.clear()
    indexers.clear_prowlarr_cache()
    _health_cache["ts"] = 0
    new_auth = auth.ensure(announce=True)
    relogin = (new_auth["username"], new_auth["password"]) != (old_auth["username"], old_auth["password"])
    resp = JSONResponse({"ok": True, "relogin": relogin})
    resp.set_cookie(auth.COOKIE, auth.make_token(new_auth), max_age=auth.MAX_AGE, httponly=True, samesite="lax")
    return resp


@app.post("/api/test/qbit")
def test_qbit():
    qb = _qb()
    try:
        v = qb.version()
        folders.clear()
        f = folders.from_qbit(config.load(), qb=qb)
        return {"ok": True, "version": v, "folders": f}
    except folders.FoldersUnavailable as e:
        return {"ok": True, "version": v, "folders": None, "warning": str(e)}
    except Exception as e:
        raise HTTPException(502, str(e))
    finally:
        qb.close()


@app.post("/api/test/prowlarr")
async def test_prowlarr():
    indexers.clear_prowlarr_cache()
    try:
        found = await indexers.prowlarr_indexers(config.load()["prowlarr"])
    except Exception as e:
        raise HTTPException(502, str(e)[:200])
    return {"ok": True, "indexers": [i["name"] for i in found]}


@app.post("/api/test/indexer")
async def test_indexer(payload: dict = Body(...)):
    cfg = config.load()
    idx = next((i for i in cfg["indexers"] if i.get("url") == payload.get("url")), None)
    if not idx:
        raise HTTPException(404, "Save the feed first, then test it.")
    try:
        await indexers.test_indexer(idx)
        return {"ok": True}
    except Exception as e:
        raise HTTPException(502, str(e)[:200])
