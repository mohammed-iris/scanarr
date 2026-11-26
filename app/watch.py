"""Watchlist: saved searches re-checked every few hours (internet only, never the HDD).
New matching releases are listed in the app and optionally pushed to your phone. Nothing downloads by itself."""
import logging
import time

import httpx

from . import activity, indexers

log = logging.getLogger("scanarr.watch")


def notify(cfg, title, message, tags="mag"):
    url = (cfg.get("ntfy_url") or "").strip()
    if not url:
        return False
    try:
        r = httpx.post(url, content=message.encode("utf-8"), timeout=10,
                       headers={"Title": _header(title), "Tags": tags})
        return r.status_code < 400
    except Exception as e:
        log.warning("notification failed: %s", e)
        return False


def _header(text):
    """HTTP headers are ASCII-only; ntfy understands RFC 2047 for anything else (e.g. Tamil or Arabic titles)."""
    import base64
    try:
        text.encode("ascii")
        return text
    except UnicodeEncodeError:
        return "=?UTF-8?B?" + base64.b64encode(text.encode("utf-8")).decode() + "?="


def matches(r, f):
    if f.get("quality") and r.get("quality") not in f["quality"]:
        return False
    if f.get("hide_cam", True) and "cam" in r.get("flags", []):
        return False
    if f.get("hide_small") and "small" in r.get("flags", []):
        return False
    if r.get("seeders", 0) < int(f.get("min_seeds") or 0):
        return False
    if f.get("hidden_cats") and r.get("category") in f["hidden_cats"]:
        return False
    words = (f.get("must") or "").lower().split()
    return all(w in r["title"].lower() for w in words)


async def check(w, cfg):
    idx, _errors = await indexers.all_indexers(cfg)
    out = await indexers.search_all(idx, w["query"], w["cat"] or "all", timeout=float(cfg.get("indexer_timeout") or 15))
    results = indexers.annotate(out["results"], cfg.get("prefs") or {})
    seen = set(w["seen"])
    new = [r for r in results if r.get("infohash") and r["infohash"] not in seen and matches(r, w["filters"])]
    hits = w["hits"] + [{k: r.get(k) for k in ("title", "size", "seeders", "leechers", "indexer", "category", "kind",
                                               "quality", "flags", "score", "download", "infohash", "published")}
                        for r in new]
    activity.watch_update(w["id"], seen=seen | {r["infohash"] for r in results if r.get("infohash")}, hits=hits)
    if new:
        best = max(new, key=lambda r: r.get("score", 0))
        activity.log(w["query"], "watch", "", f"{len(new)} new: {best['title']}")
        notify(cfg, f"Scanarr: {len(new)} new for '{w['query']}'", best["title"])
    return len(new)


async def due(cfg):
    hours = float(cfg.get("watch_hours") or 12)
    n = 0
    for w in activity.watches():
        if time.time() - (w["last_check"] or 0) >= hours * 3600:
            try:
                n += await check(w, cfg)
            except Exception as e:
                log.warning("watch check failed for %s: %s", w["query"], e)
                activity.watch_update(w["id"])  # try again next period, not every poll
    return n
