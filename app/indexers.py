"""Parallel search across Torznab indexers."""
import asyncio
import re
import xml.etree.ElementTree as ET
from urllib.parse import quote
from email.utils import parsedate_to_datetime

import httpx

TORZNAB_NS = "{http://torznab.com/schemas/2015/feed}"
CATS = {"all": "", "movies": "2000", "tv": "5000", "audio": "3000", "books": "7000",
        "pc": "4000", "console": "1000", "xxx": "6000", "other": "8000"}
TOP = {1000: "Console", 2000: "Movies", 3000: "Audio", 4000: "PC", 5000: "TV",
       6000: "XXX", 7000: "Books", 8000: "Other"}


KIND_OF = {"Movies": "movies", "TV": "tv", "Audio": "audio", "Books": "books", "PC": "software",
           "Console": "games", "XXX": "xxx", "Other": "other"}
EPISODE = re.compile(r"\bS\d{1,2}(E\d{1,3})?\b|\bseason\s?\d", re.I)


def guess_kind(title, category):
    k = KIND_OF.get(category, "other")
    if k == "other" and EPISODE.search(title):
        return "tv"
    return k


def top_category(values):
    """Map Torznab category ids (e.g. 2040, 5070) to a top-level name."""
    for v in values:
        try:
            n = int(v)
        except (TypeError, ValueError):
            continue
        if n < 100000 and (n // 1000) * 1000 in TOP:  # >=100000 are indexer-specific ids
            return TOP[(n // 1000) * 1000]
    return "Other"


def parse_torznab(xml_text, indexer_name):
    root = ET.fromstring(xml_text)
    err = root if root.tag == "error" else None
    if err is not None:
        raise RuntimeError(err.get("description") or "indexer error")
    results = []
    for item in root.iter("item"):
        try:
            results.append(_parse_item(item, indexer_name))
        except (ValueError, TypeError, AttributeError):
            continue  # one malformed result shouldn't hide the rest
    for r in results:
        r["kind"] = guess_kind(r["title"], r["category"])
    return results


def _num(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _parse_item(item, indexer_name):
    attrs = {a.get("name"): a.get("value") for a in item.findall(f"{TORZNAB_NS}attr")}
    cats = [c.text for c in item.findall("category")] + [
        a.get("value") for a in item.findall(f"{TORZNAB_NS}attr") if a.get("name") == "category"]
    enclosure = item.find("enclosure")
    link = item.findtext("link") or (enclosure.get("url") if enclosure is not None else "") or ""
    guid = item.findtext("guid") or ""
    infohash = (attrs.get("infohash") or "").lower()
    magnet = attrs.get("magneturl") or (guid if guid.startswith("magnet:") else "")
    if link.startswith("magnet:"):
        magnet, link = magnet or link, ""
    title = (item.findtext("title") or "").strip()
    if not magnet and len(infohash) == 40:
        # Build a magnet from the hash so qBit never has to reach the indexer's
        # download link (e.g. a Prowlarr link on localhost of another machine).
        magnet = f"magnet:?xt=urn:btih:{infohash}&dn={quote(title)}"
    size = item.findtext("size") or attrs.get("size") or (enclosure.get("length") if enclosure is not None else 0)
    pub = item.findtext("pubDate")
    try:
        pub_iso = parsedate_to_datetime(pub).isoformat() if pub else None
    except (TypeError, ValueError, IndexError):
        pub_iso = None
    seeders, peers = _num(attrs.get("seeders")), _num(attrs.get("peers"))
    if not title or not (magnet or link):
        raise ValueError("unusable item")
    return {
        "title": title,
        "indexer": item.findtext("prowlarrindexer") or indexer_name,
        "size": _num(size),
        "seeders": seeders,
        "leechers": max(peers - seeders, 0),
        "published": pub_iso,
        "download": magnet or link,
        "torrent_url": link,
        "category": top_category(cats),
        "infohash": infohash,
        "comments": item.findtext("comments") or "",
    }


# ---------------------------------------------------------------- junk flags + best pick
CAM = re.compile(r"\b(cam|camrip|hdcam|ts|hdts|telesync|tc|hdtc|telecine|scr|dvdscr|screener|hdrip\.?cam)\b", re.I)
QRANK = {"SD": 0, "720p": 1, "1080p": 2, "2160p": 3}
# smallest believable sizes (bytes) for a whole movie / one episode at each quality
MIN_SIZE = {"movies": {"2160p": 3e9, "1080p": 9e8, "720p": 4e8},
            "tv": {"2160p": 6e8, "1080p": 1.5e8, "720p": 8e7}}


def quality(title):
    t = title.lower()
    if re.search(r"2160p|\b4k\b|\buhd\b", t):
        return "2160p"
    for q in ("1080p", "720p"):
        if q in t:
            return q
    return "SD"


def annotate(results, prefs):
    """Adds quality, flags (cam / small / noseeds) and a score for the Best-pick badge."""
    import math
    pref = QRANK.get(prefs.get("quality", "1080p"), 2)
    for r in results:
        q = quality(r["title"])
        r["quality"] = q
        flags = []
        if CAM.search(r["title"].replace(".", " ").replace("-", " ")):
            flags.append("cam")
        single_ep = r["kind"] == "tv" and re.search(r"S\d{1,2}E\d{1,3}", r["title"], re.I)
        floor = MIN_SIZE["tv" if single_ep else "movies"].get(q) if r["kind"] in ("movies", "tv") else None
        if floor and r["size"] and r["size"] < floor and (r["kind"] == "movies" or single_ep):
            flags.append("small")
        if r["seeders"] == 0:
            flags.append("noseeds")
        r["flags"] = flags
        score = 30 - 12 * abs(QRANK[q] - pref) + min(10 * math.log10(1 + r["seeders"]), 30)
        limit = (prefs.get("max_gb_episode") if single_ep else prefs.get("max_gb_movie")) or 0
        if r["kind"] in ("movies", "tv") and limit and r["size"] > limit * 1024 ** 3 and (r["kind"] == "movies" or single_ep):
            score -= 20
        score -= 100 * ("cam" in flags) + 40 * ("small" in flags) + 30 * ("noseeds" in flags)
        r["score"] = round(score, 1)
    return results


# ---------------------------------------------------------------- indexer health
HEALTH = {}            # name -> stats
FAIL_LIMIT, SKIP_SECONDS = 3, 1800


def _record(name, ok, ms, err=None):
    import time
    h = HEALTH.setdefault(name, {"ok": 0, "fail": 0, "streak": 0, "last_ms": None, "last_error": None,
                                 "skip_until": 0, "last": None})
    h["last"], h["last_ms"] = time.time(), ms
    if ok:
        h["ok"] += 1
        h["streak"] = 0
        h["last_error"] = None
    else:
        h["fail"] += 1
        h["streak"] += 1
        h["last_error"] = err
        if h["streak"] >= FAIL_LIMIT:
            h["skip_until"] = time.time() + SKIP_SECONDS


def skipped(name):
    import time
    return HEALTH.get(name, {}).get("skip_until", 0) > time.time()


def reset_health(name=None):
    if name:
        HEALTH.pop(name, None)
    else:
        HEALTH.clear()


async def _search_one(client, idx, query, cat):
    params = {"t": "search", "q": query}
    if idx.get("apikey"):
        params["apikey"] = idx["apikey"]
    if CATS.get(cat):
        params["cat"] = CATS[cat]
    r = await client.get(idx["url"], params=params)
    r.raise_for_status()
    return parse_torznab(r.text, idx.get("name") or idx["url"])


async def search_all(indexers, query, cat="all", timeout=15):
    import time
    active = [i for i in indexers if i.get("enabled", True) and i.get("url")]
    errors, results = [], []
    for i in [i for i in active if skipped(i.get("name") or i["url"])]:
        errors.append({"indexer": i.get("name") or i["url"], "error": "skipped: failing repeatedly (auto-retry in 30 min)", "skipped": True})
    active = [i for i in active if not skipped(i.get("name") or i["url"])]

    async def timed(client, i):
        t0 = time.monotonic()
        try:
            out = await asyncio.wait_for(_search_one(client, i, query, cat), timeout)
            _record(i.get("name") or i["url"], True, int((time.monotonic() - t0) * 1000))
            return out
        except Exception as e:
            msg = "timed out" if isinstance(e, (asyncio.TimeoutError, httpx.TimeoutException)) else (str(e).split("\n")[0] or type(e).__name__)
            _record(i.get("name") or i["url"], False, int((time.monotonic() - t0) * 1000), msg[:200])
            raise RuntimeError(msg) from e

    async with httpx.AsyncClient(timeout=timeout + 5, follow_redirects=True) as client:
        outs = await asyncio.gather(*(timed(client, i) for i in active), return_exceptions=True)
    for idx, out in zip(active, outs):
        if isinstance(out, Exception):
            errors.append({"indexer": idx.get("name") or idx["url"], "error": str(out)[:200]})
        else:
            results.extend(out)
    # de-duplicate by infohash (keep the one with most seeders)
    seen = {}
    unique = []
    for r in sorted(results, key=lambda x: -x["seeders"]):
        h = r["infohash"]
        if h and h in seen:
            continue
        if h:
            seen[h] = True
        unique.append(r)
    return {"results": unique, "errors": errors}


_prowlarr_cache = {"ts": 0, "key": None, "data": []}


async def prowlarr_indexers(pcfg, max_age=300):
    """All enabled torrent indexers configured in Prowlarr, as Torznab endpoints."""
    import time
    url, key = (pcfg.get("url") or "").rstrip("/"), pcfg.get("api_key") or ""
    if not (url and key):
        return []
    c = _prowlarr_cache
    if c["key"] == (url, key) and time.time() - c["ts"] < max_age:
        return c["data"]
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(f"{url}/api/v1/indexer", headers={"X-Api-Key": key})
        if r.status_code == 401:
            raise RuntimeError("Prowlarr rejected the API key")
        r.raise_for_status()
    data = [{"name": i.get("name") or f"Indexer {i['id']}", "url": f"{url}/{i['id']}/api",
             "apikey": key, "enabled": True, "source": "prowlarr", "privacy": i.get("privacy")}
            for i in r.json() if i.get("enable") and i.get("protocol", "torrent") == "torrent"]
    c.update(ts=time.time(), key=(url, key), data=data)
    return data


def clear_prowlarr_cache():
    _prowlarr_cache.update(ts=0, key=None, data=[])


async def all_indexers(cfg):
    """Prowlarr indexers + any extra manual Torznab feeds. Returns (indexers, errors)."""
    errors = []
    try:
        found = await prowlarr_indexers(cfg.get("prowlarr") or {})
    except Exception as e:
        found = []
        errors.append({"indexer": "Prowlarr", "error": str(e)[:200]})
    extra = [dict(i, source="manual") for i in cfg.get("indexers", []) if i.get("url")]
    return found + extra, errors


async def test_indexer(idx):
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        params = {"t": "caps"}
        if idx.get("apikey"):
            params["apikey"] = idx["apikey"]
        r = await client.get(idx["url"], params=params)
        r.raise_for_status()
        root = ET.fromstring(r.text)
        if root.tag == "error":
            raise RuntimeError(root.get("description"))
        return True
