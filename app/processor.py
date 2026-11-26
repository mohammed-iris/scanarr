"""Background worker.

Flow: qBittorrent downloads into TEMP, moves finished files to COMPLETED, then Scanarr:
  - movies/TV: fetches subtitles for the original file, renames (moves within COMPLETED) to a
    clean name, removes the torrent and leftover junk. Unsure? -> "Needs review", nothing touched.
  - other types: already in their folder; torrent is removed so nothing seeds.
Also: fake-file guard, dead-torrent cleanup, subtitle retries, and an "Unsorted" drop folder.
"""
import logging
import os
import re
import shutil
import time

from . import activity, config, folders, naming
from .qbit import QBit
from .subtitles import OpenSubtitles, os_hash

log = logging.getLogger("scanarr.processor")

DONE_TAG, REVIEW_TAG, CHECKED_TAG, DEAD_TAG = "scanarr-done", "scanarr-review", "scanarr-checked", "scanarr-dead"
PEEK_TAG = "scanarr-peek"
ARCHIVE_EXT = {".rar", ".zip", ".7z", ".r00", ".001", ".tar", ".gz"}
PEEK_MAX_AGE = 1800  # abandoned peeks are removed after 30 min
VIDEO_EXT = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".ts", ".wmv", ".mpg", ".mpeg", ".webm"}
DANGEROUS_EXT = {".exe", ".scr", ".lnk", ".bat", ".cmd", ".com", ".pif", ".msi", ".vbs", ".vbe",
                 ".js", ".jse", ".wsf", ".ps1", ".hta", ".jar", ".apk", ".dll"}
MIN_VIDEO = 50 * 1024 * 1024  # smaller videos are samples/extras
STABLE_SECONDS = 120          # files in Unsorted must be untouched this long
LANG_TOKENS = {
    "en": {"en", "eng", "english"}, "ml": {"ml", "mal", "malayalam"},
    "ta": {"ta", "tam", "tamil"}, "hi": {"hi", "hin", "hindi"},
    "ar": {"ar", "ara", "arabic"}, "fr": {"fr", "fre", "fra", "french"},
    "es": {"es", "spa", "spanish"}, "de": {"de", "ger", "deu", "german"},
}
_last_error = {}
_backoff = {}   # torrent hash -> {"n": failed checks, "until": timestamp}; avoids re-reading the HDD every poll
MAX_NOT_READY = 6


class NeedsReview(Exception):
    pass


def _tokens(s):
    return set(re.split(r"[^a-z]+", s.lower()))


def _tags(t):
    return {x.strip() for x in (t.get("tags") or "").split(",") if x.strip()}


def map_path(p, pm):
    return config.map_path(p, pm)


def _abs(base, rel):
    return os.path.normpath(os.path.join(base, *rel.split("/")))


def _under(path, root):
    path, root = os.path.normcase(os.path.abspath(path)), os.path.normcase(os.path.abspath(root))
    return path == root or path.startswith(root.rstrip("\\/") + os.sep)


def is_video(name, size=None):
    ok = os.path.splitext(name)[1].lower() in VIDEO_EXT and "sample" not in os.path.basename(name).lower()
    return ok and (size is None or size >= MIN_VIDEO)


def free_bytes(path):
    p = os.path.abspath(path)
    while not os.path.exists(p) and os.path.dirname(p) != p:
        p = os.path.dirname(p)
    return shutil.disk_usage(p).free


# ---------------------------------------------------------------- planning
def _bundled_subs(video_rel, all_files, langs):
    """.srt files shipped inside the torrent that belong to this video."""
    found = {}
    vdir = os.path.dirname(video_rel)
    vstem = os.path.splitext(os.path.basename(video_rel))[0].lower()
    n_videos = sum(1 for f in all_files if is_video(f["name"], f["size"]))
    for f in all_files:
        name = f["name"]
        if not name.lower().endswith(".srt"):
            continue
        sdir, sbase = os.path.dirname(name), os.path.basename(name).lower()
        belongs = sbase.startswith(vstem) or (
            n_videos == 1 and (sdir == vdir or os.path.dirname(sdir) == vdir))
        if not belongs:
            continue
        toks = _tokens(sbase.replace(vstem, " ")) | _tokens(sdir.split("/")[-1] if sdir != vdir else "")
        for lang in langs:
            if lang not in found and toks & LANG_TOKENS.get(lang, {lang}):
                found[lang] = name
    return found


def plan_videos(label, rel_files, kind, cfg, tmdb):
    """rel_files: [{"name": relative path, "size": bytes}]. Returns list of plan items or raises NeedsReview
    (with the best-guess items attached so the user only has to correct them)."""
    videos = [f for f in rel_files if is_video(f["name"])]
    big = [f for f in videos if f["size"] >= MIN_VIDEO]
    videos = big or videos[:1]
    if not videos:
        raise NeedsReview("no video file found", [])
    hint = "movie" if kind == "movies" else "episode"
    tinfo = naming.parse(label, hint)
    items, problems = [], []
    for f in videos:
        ext = os.path.splitext(f["name"])[1].lower()
        finfo = naming.parse(os.path.basename(f["name"]), hint)
        meta = {"release": os.path.splitext(os.path.basename(f["name"]))[0]}
        if kind == "movies":
            info = tinfo if len(videos) == 1 and tinfo.get("title") else finfo
            title = info.get("title") or finfo.get("title")
            year = info.get("year") or finfo.get("year")
            match = None
            if title and tmdb.key:
                try:
                    match = tmdb.movie(title, year)
                except Exception as e:
                    log.warning("TMDb lookup failed: %s", e)
                    match = "error"
            if match and match != "error":
                title, year = match["title"], match["year"]
            if not title:
                problems.append("couldn't read a title")
            elif tmdb.key and match is None:
                problems.append(f"no TMDb match for '{title}'")
            elif not year:
                problems.append(f"no year for '{title}'")
            target = naming.movie_target(config.kind_dir(cfg, "movies"), title or label, year, ext,
                                         cfg.get("movie_folders", True))
            meta.update(query=title, year=year)
        else:
            show = finfo.get("title") or tinfo.get("title")
            season = finfo.get("season") or tinfo.get("season") or 1
            eps = finfo.get("episodes") or (tinfo.get("episodes") if len(videos) == 1 else [])
            match = None
            if show and tmdb.key:
                try:
                    match = tmdb.show(show, finfo.get("year") or tinfo.get("year"))
                except Exception as e:
                    log.warning("TMDb lookup failed: %s", e)
                    match = "error"
            if match and match != "error":
                show = match["title"]
            if not show:
                problems.append("couldn't read a show name")
            elif not eps:
                problems.append(f"no episode number in {os.path.basename(f['name'])}")
            elif tmdb.key and match is None:
                problems.append(f"no TMDb match for '{show}'")
            target = naming.episode_target(config.kind_dir(cfg, "tv"), show or label, season, eps or [0], ext)
            meta.update(query=show, season=season, episode=(eps or [None])[0])
        items.append({"src_rel": f["name"], "dst": target, "meta": meta})
    if problems:
        raise NeedsReview("; ".join(dict.fromkeys(problems)), items)
    return items


# ---------------------------------------------------------------- executing
def execute(items, label, cfg, osub, all_files=None, base=""):
    """Move each src -> dst safely, fetching subtitles for the ORIGINAL file first.
    items: [{"src": abs, "dst": abs, "meta": {...}, "src_rel"?: rel}]"""
    completed = config.completed_dir(cfg)
    langs = cfg.get("subtitle_languages") or []
    # 1) validate everything before touching anything
    seen = set()
    todo = []
    for it in items:
        if not os.path.isfile(it["src"]):
            if os.path.isfile(it["dst"]) and activity.library_find(it["dst"]):
                continue  # renamed in an earlier, interrupted run: nothing left to do for this one
            raise NeedsReview(f"file missing: {os.path.basename(it['src'])}", items)
        if not _under(it["dst"], completed):
            raise NeedsReview(f"target outside the Completed folder: {it['dst']}", items)
        key = os.path.normcase(it["dst"])
        if os.path.exists(it["dst"]) or key in seen:
            raise NeedsReview(f"already exists: {os.path.relpath(it['dst'], completed)}", items)
        seen.add(key)
        todo.append(it)
    results = []
    for it in todo:
        src, dst, meta = it["src"], it["dst"], it.get("meta") or {}
        notes, sub_files = [], []
        stem_src, stem_dst = os.path.splitext(src)[0], os.path.splitext(dst)[0]
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        # 2) subtitles BEFORE renaming: shipped in the torrent, else OpenSubtitles by file hash + release name
        bundled = _bundled_subs(it["src_rel"], all_files, langs) if all_files and it.get("src_rel") else {}
        staged = {}
        for lang in langs:
            if lang in bundled:
                staged[lang] = (_abs(base, bundled[lang]), "bundled", False)
                continue
            if not osub.enabled:
                notes.append(f"{lang}: no OpenSubtitles key")
                continue
            tmp = f"{stem_src}.{lang}.scanarr-part"
            try:
                got = osub.fetch(lang, src, tmp, **_sub_kw(meta))
            except Exception as e:
                got, err = None, str(e)[:80]
                notes.append(f"{lang}: error {err}")
            if got:
                staged[lang] = (tmp, got, True)
            else:
                if not any(n.startswith(f"{lang}: error") for n in notes):
                    notes.append(f"{lang}: not found yet, will retry daily")
                if "hash" not in meta:  # remember the file hash now, so retries never need to read the HDD
                    try:
                        meta["hash"] = os_hash(src)
                    except (OSError, ValueError):
                        pass
                activity.queue_sub(dst, lang, meta)
        # 3) rename the video (a move inside the same volume = instant)
        try:
            shutil.move(src, dst)
        except Exception:
            for path, _how, temp in staged.values():  # don't leave half-done subtitle files behind
                if temp and os.path.exists(path):
                    os.remove(path)
            raise
        # 4) put subtitles next to it
        for lang, (path, how, temp) in staged.items():
            sdst = f"{stem_dst}.{lang}.srt"
            try:
                shutil.move(path, sdst) if temp else shutil.copy2(path, sdst)
                sub_files.append(sdst)
                notes.append(f"{lang}: {how}")
            except OSError as e:
                notes.append(f"{lang}: couldn't save ({e})")
        activity.add_rename(label, src, dst, sub_files)
        activity.library_add(dst)
        activity.log(label, "renamed", dst, "; ".join(notes))
        results.append(dst)
    return results


def _sub_kw(meta):
    kw = {"query": meta.get("query"), "release": meta.get("release")}
    if meta.get("season") is not None and meta.get("episode") is not None:
        kw.update(season=meta["season"], episode=meta["episode"])
    else:
        kw["year"] = meta.get("year")
    return kw


def _meta_for(path, cfg=None):
    """Search hints for a renamed file, from its clean name."""
    base = os.path.splitext(os.path.basename(path))[0]
    m = re.match(r"^(.*) S(\d{2})E(\d{2,3})", base)
    if m:
        return {"query": m.group(1), "season": int(m.group(2)), "episode": int(m.group(3))}
    m = re.match(r"^(.*) \((\d{4})\)$", base)
    return {"query": m.group(1), "year": int(m.group(2))} if m else {"query": base}


def subtitle_options(rid, lang, osub):
    r = activity.get_rename(rid)
    if not r or r["undone"] or not os.path.exists(r["dst"]):
        raise ValueError("file not found")
    meta = _meta_for(r["dst"])
    meta["release"] = os.path.splitext(os.path.basename(r["src"]))[0]
    try:
        meta["hash"] = os_hash(r["dst"])
    except (OSError, ValueError):
        pass
    results = osub.search(lang, moviehash=meta.get("hash"), **_sub_kw(meta))
    out = []
    for s_ in results[:20]:
        a = s_.get("attributes", {})
        out.append({"file_id": a["files"][0]["file_id"], "release": a.get("release") or a["files"][0].get("file_name"),
                    "downloads": a.get("download_count"), "hash_match": bool(a.get("moviehash_match")),
                    "machine": bool(a.get("ai_translated") or a.get("machine_translated")),
                    "hi": bool(a.get("hearing_impaired")), "fps": a.get("fps")})
    return out


def subtitle_replace(rid, lang, file_id, osub):
    r = activity.get_rename(rid)
    if not r or r["undone"] or not os.path.exists(r["dst"]):
        raise ValueError("file not found")
    dest = f"{os.path.splitext(r['dst'])[0]}.{lang}.srt"
    content = osub.download(file_id)
    tmp = dest + ".scanarr-part"
    with open(tmp, "wb") as f:
        f.write(content)
    os.replace(tmp, dest)
    subs = r["subs"] if dest in r["subs"] else r["subs"] + [dest]
    activity.set_rename_subs(rid, subs)
    activity.log(os.path.basename(r["dst"]), "subtitles", dest, f"{lang}: replaced by your choice")
    return dest


def undo(rid):
    r = activity.get_rename(rid)
    if not r or r["undone"]:
        raise ValueError("nothing to undo")
    if not os.path.exists(r["dst"]):
        raise ValueError("renamed file no longer exists")
    if os.path.exists(r["src"]):
        raise ValueError("original path is occupied")
    os.makedirs(os.path.dirname(r["src"]), exist_ok=True)
    shutil.move(r["dst"], r["src"])
    for s in r["subs"]:
        if os.path.exists(s):
            lang = s.rsplit(".", 2)[-2]
            shutil.move(s, f"{os.path.splitext(r['src'])[0]}.{lang}.srt")
    _cleanup_empty(os.path.dirname(r["dst"]))
    activity.move_subs_queue(r["dst"], r["src"])
    activity.library_remove(r["dst"])
    activity.mark_undone(rid)
    activity.log(r["label"], "undone", r["src"], f"restored from {os.path.basename(r['dst'])}")


def _cleanup_empty(d, stop_at=None):
    try:
        while d and os.path.isdir(d) and not os.listdir(d) and d != stop_at:
            os.rmdir(d)
            d = os.path.dirname(d)
    except OSError:
        pass


# ---------------------------------------------------------------- qBittorrent passes
def guard_pass(qb, cfg, rev, torrents=None, removed=None):
    """Block movie/TV torrents that contain executables (classic fake-release malware)."""
    removed = removed if removed is not None else set()
    if not cfg.get("guard", True):
        return removed
    for t in torrents if torrents is not None else qb.list():
        if CHECKED_TAG in _tags(t) or DONE_TAG in _tags(t) or PEEK_TAG in _tags(t):
            continue  # peeks are shown to you with warnings; checked again once you start them
        known = rev.get(t.get("category"))
        if known and known not in config.VIDEO_KINDS:
            continue  # software etc. may legitimately contain .exe
        if not known and not (cfg.get("process_external", True) and is_external(t, rev)):
            continue
        files = qb.files(t["hash"])
        if not files:  # metadata not downloaded yet
            continue
        if not known and external_kind(t, files) not in config.VIDEO_KINDS:
            qb.add_tag(t["hash"], CHECKED_TAG)
            continue
        bad = [f["name"] for f in files if os.path.splitext(f["name"])[1].lower() in DANGEROUS_EXT]
        if bad:
            qb.delete(t["hash"], delete_files=True)
            removed.add(t["hash"])
            activity.log(t["name"], "blocked", "", f"fake release, contains {os.path.basename(bad[0])} - removed")
            _notify(cfg, "Scanarr blocked a fake download", t["name"], "warning")
            log.warning("blocked %s (%s)", t["name"], bad[0])
        else:
            qb.add_tag(t["hash"], CHECKED_TAG)
    return removed


def peek_info(files, kind):
    """File list + warnings for the Peek dialog."""
    out, warns = [], []
    for i, f in enumerate(files):
        ext = os.path.splitext(f["name"])[1].lower()
        item = {"id": f.get("index", i), "name": f["name"], "size": f["size"],
                "video": is_video(f["name"]), "danger": ext in DANGEROUS_EXT,
                "archive": ext in ARCHIVE_EXT, "sample": "sample" in f["name"].lower()}
        out.append(item)
    if any(f["danger"] for f in out) and kind in config.VIDEO_KINDS:
        warns.append("Contains a program file (" + next(os.path.basename(f["name"]) for f in out if f["danger"]) +
                     "). A movie or show never needs one: this is almost certainly fake or malware.")
    if any(f["archive"] for f in out) and kind in config.VIDEO_KINDS:
        warns.append("Video is packed in an archive (.rar/.zip). Often password-protected or fake.")
    if any("password" in f["name"].lower() for f in out):
        warns.append("Mentions a password - usually a scam.")
    if kind in config.VIDEO_KINDS and not any(f["video"] and not f["sample"] for f in out):
        warns.append("No video file found.")
    return out, warns


def peek_cleanup(qb, torrents=None, removed=None):
    removed = removed if removed is not None else set()
    now = time.time()
    for t in torrents if torrents is not None else qb.list():
        if t["hash"] in removed:
            continue
        if PEEK_TAG in _tags(t) and now - (t.get("added_on") or now) > PEEK_MAX_AGE:
            qb.delete(t["hash"], delete_files=True)
            removed.add(t["hash"])
    return removed


def dead_pass(qb, cfg, rev, torrents=None, removed=None):
    removed = removed if removed is not None else set()
    hours = float(cfg.get("dead_hours") or 0)
    if hours <= 0:
        return removed
    now = time.time()
    for t in torrents if torrents is not None else qb.list():
        if t["hash"] in removed or t.get("category") not in rev or t.get("progress", 0) >= 1 or DEAD_TAG in _tags(t) or PEEK_TAG in _tags(t):
            continue
        if t.get("state") not in ("stalledDL", "metaDL", "forcedMetaDL", "downloading"):
            continue
        if t.get("state") == "downloading" and (t.get("dlspeed") or 0) > 0:
            continue
        idle_since = max(t.get("last_activity") or 0, t.get("added_on") or 0)
        if now - idle_since < hours * 3600:
            continue
        if cfg.get("dead_action") == "remove":
            qb.delete(t["hash"], delete_files=True)
            removed.add(t["hash"])
            activity.log(t["name"], "dead", "", f"no progress for {hours:g}h - removed")
        else:
            qb.add_tag(t["hash"], DEAD_TAG)
            activity.log(t["name"], "dead", "", f"no progress for {hours:g}h - flagged")
    return removed


def _ready(t, files, base, cfg):
    """Finished AND moved out of temp AND every wanted file on disk at full size."""
    if t.get("progress", 0) < 1 or t.get("state") in ("moving", "checkingUP", "checkingDL", "checkingResumeData"):
        return False
    temp = cfg["folders"].get("temp")
    if temp and _under(base, temp):
        return False
    for f in files:
        if f.get("priority", 1) == 0:
            continue
        p = _abs(base, f["name"])
        if not os.path.isfile(p) or os.path.getsize(p) != f["size"]:
            return False
    return True


def completed_pass(qb, osub, tmdb, cfg, rev, torrents=None, removed=()):
    done = 0
    for t in torrents if torrents is not None else qb.list():
        if t["hash"] in removed:
            continue
        tags = _tags(t)
        if DONE_TAG in tags or REVIEW_TAG in tags or PEEK_TAG in tags or t.get("progress", 0) < 1:
            continue
        if not rev.get(t.get("category")) and not (cfg.get("process_external", True) and is_external(t, rev)):
            continue
        bo = _backoff.get(t["hash"])
        if bo and time.time() < bo["until"]:
            continue
        files = [f for f in qb.files(t["hash"]) if f.get("priority", 1) != 0]
        kind = kind_of(t, files, cfg, rev)
        if not kind:
            continue
        base = map_path(t.get("save_path") or "", cfg.get("path_map") or {})
        if t.get("state") in ("moving", "checkingUP", "checkingDL", "checkingResumeData"):
            continue  # qBit is still busy; no disk checks until it's done
        if not _ready(t, files, base, cfg):
            n = (bo or {}).get("n", 0) + 1
            if n >= MAX_NOT_READY:  # stop checking the disk; let the user decide
                _backoff.pop(t["hash"], None)
                activity.add_review(t["name"], t["hash"], kind or "other",
                                    "finished in qBittorrent but the files aren't complete on disk", [])
                activity.log(t["name"], "review", base, "files not found on disk")
                qb.add_tag(t["hash"], REVIEW_TAG)
            else:
                _backoff[t["hash"]] = {"n": n, "until": time.time() + min(60 * 2 ** n, 3600)}
            continue
        _backoff.pop(t["hash"], None)
        try:
            if kind in config.VIDEO_KINDS:
                items = plan_videos(t["name"], files, kind, cfg, tmdb)
                for it in items:
                    it["src"] = _abs(base, it["src_rel"])
                execute(items, t["name"], cfg, osub, files, base)
                if cfg.get("auto_remove", True):
                    qb.delete(t["hash"], delete_files=True)  # videos already moved out; clears junk
                else:
                    qb.add_tag(t["hash"], DONE_TAG)
            else:
                activity.log(t["name"], "completed", t.get("content_path") or base,
                             "added in qBit, left where it was saved" if kind == "external-other" else kindlabel(kind))
                if cfg.get("auto_remove", True):
                    qb.delete(t["hash"], delete_files=False)  # keep the files, stop seeding
                else:
                    qb.add_tag(t["hash"], DONE_TAG)
            done += 1
        except NeedsReview as e:
            items = e.args[1] if len(e.args) > 1 else []
            for it in items:
                it.setdefault("src", _abs(base, it.get("src_rel", "")))
            activity.add_review(t["name"], t["hash"], kind, e.args[0], items)
            activity.log(t["name"], "review", "", e.args[0])
            _notify(cfg, "Scanarr: needs your review", f"{t['name']}: {e.args[0]}", "eyes")
            qb.add_tag(t["hash"], REVIEW_TAG)
            try:
                qb.control("stop", [t["hash"]])  # no seeding while it waits
            except Exception:
                pass
        except Exception as e:
            log.exception("failed processing %s", t.get("name"))
            msg = str(e)[:300]
            _backoff[t["hash"]] = {"n": 0, "until": time.time() + 3600}  # retry in an hour, not every poll
            if _last_error.get(t["hash"]) != msg:
                _last_error[t["hash"]] = msg
                activity.log(t["name"], "error", "", msg)
    return done


def external_kind(t, files):
    """Type for a torrent added directly in qBit (no Scanarr category), guessed from its files."""
    vids = [f for f in files if is_video(f["name"], f["size"])]
    if not vids:
        return "external-other"
    names = " ".join([t.get("name", "")] + [f["name"] for f in vids])
    return "tv" if re.search(r"S\d{1,2}E\d{1,3}|\bS\d{1,2}\b|season\s?\d|\d{1,2}x\d{2}", names, re.I) else "movies"


def is_external(t, rev):
    """Added directly in qBit by you: no category at all. Torrents with ANOTHER app's category are never touched."""
    return not t.get("category") and t.get("category") not in rev


def kind_of(t, files, cfg, rev):
    k = rev.get(t.get("category"))
    if k:
        return k
    if cfg.get("process_external", True) and is_external(t, rev):
        return external_kind(t, files)
    return None


def _notify(cfg, title, msg, tags):
    from .watch import notify
    notify(cfg, title, msg, tags)


def kindlabel(k):
    return {"audio": "Audio", "books": "Books", "software": "Software", "games": "Games",
            "xxx": "XXX", "other": "Other"}.get(k, k)


# ---------------------------------------------------------------- Unsorted drop folder
def unsorted_pass(osub, tmdb, cfg):
    root = config.kind_dir(cfg, "unsorted")
    if not os.path.isdir(root):
        return 0
    done, now = 0, time.time()
    for dirpath, _dirs, names in os.walk(root):
        for n in names:
            p = os.path.join(dirpath, n)
            try:
                st = os.stat(p)
            except OSError:
                continue
            if not is_video(n, st.st_size) or now - st.st_mtime < STABLE_SECONDS or activity.review_exists_for(p):
                continue
            kind = "tv" if re.search(r"S\d{1,2}E\d{1,3}|\d{1,2}x\d{2}", n, re.I) else "movies"
            rel = os.path.relpath(p, root).replace(os.sep, "/")
            try:
                items = plan_videos(os.path.splitext(n)[0], [{"name": rel, "size": st.st_size}], kind, cfg, tmdb)
                for it in items:
                    it["src"] = p
                execute(items, n, cfg, osub)
                _cleanup_empty(dirpath, stop_at=root)
                done += 1
            except NeedsReview as e:
                items = e.args[1] if len(e.args) > 1 else []
                for it in items:
                    it["src"] = p
                if not items:
                    continue
                activity.add_review(n, "", kind, e.args[0], items)
                activity.log(n, "review", "", e.args[0])
    return done


# ---------------------------------------------------------------- subtitle retries
def _staged_dir():
    d = os.path.join(config.CONFIG_DIR, "subs-staged")  # on the SSD with the rest of Scanarr's data
    os.makedirs(d, exist_ok=True)
    return d


def subs_search_pass(osub, cfg):
    """Network only: search with the stored file hash + name and park finds on the SSD.
    The HDD isn't touched until it's awake anyway (see subs_deliver_pass)."""
    if not osub.enabled:
        return
    days = float(cfg.get("subtitle_retry_days") or 7)
    for s_ in activity.due_subs():
        meta, lang = s_["meta"], s_["lang"]
        kw = _sub_kw(meta)
        staged = os.path.join(_staged_dir(), f"{s_['id']}.{lang}.srt")
        try:
            got = osub.fetch(lang, None, staged, moviehash=meta.get("hash"), **kw)
        except Exception as e:
            log.info("subtitle retry failed for %s: %s", s_["video"], e)
            got = None
        if got:
            activity.sub_ready(s_["id"], staged)
            activity.log(os.path.basename(s_["video"]), "subtitles", "", f"{lang}: found ({got}), saving when the HDD is next awake")
        elif time.time() - s_["first_try"] > days * 86400:
            activity.drop_sub(s_["id"])
            activity.log(os.path.basename(s_["video"]), "subtitles", "", f"{lang}: gave up after {days:g} days")
        else:
            activity.sub_retry_later(s_["id"])


def subs_deliver_pass():
    """HDD: move subtitles found earlier next to their videos."""
    for s_ in activity.ready_subs():
        video, lang, staged = s_["video"], s_["lang"], s_["ready_path"]
        dest = f"{os.path.splitext(video)[0]}.{lang}.srt"
        try:
            if os.path.exists(video) and not os.path.exists(dest) and os.path.exists(staged):
                shutil.move(staged, dest)
                activity.log(os.path.basename(video), "subtitles", dest, f"{lang}: saved")
            elif os.path.exists(staged):
                os.remove(staged)
        except OSError as e:
            log.warning("couldn't save subtitle for %s: %s", video, e)
            continue
        activity.drop_sub(s_["id"])


def library_scan(cfg):
    """Refresh the SSD index of Movies/TV (used for 'already in library'). Only runs while the HDD is awake."""
    paths = []
    for k in config.VIDEO_KINDS:
        root = config.kind_dir(cfg, k)
        for dirpath, _d, names in os.walk(root):
            paths += [os.path.join(dirpath, n) for n in names if is_video(n)]
    activity.library_replace(paths)


def awake_housekeeping(osub, tmdb, cfg):
    """Everything that needs the HDD but isn't urgent. Runs only when the HDD is already spinning
    (a download just finished), on "Check now", or at the daily time if you chose that."""
    done = unsorted_pass(osub, tmdb, cfg)
    subs_deliver_pass()
    today = time.strftime("%Y-%m-%d")
    if activity.kv_get("library_scan_day") != today:
        library_scan(cfg)
        activity.kv_set("library_scan_day", today)
    return done


# ---------------------------------------------------------------- main loop body
def make_clients(cfg):
    o = cfg.get("opensubtitles") or {}
    return (OpenSubtitles(o.get("api_key", ""), o.get("username", ""), o.get("password", "")),
            naming.TMDb(cfg.get("tmdb_api_key")))


def _mins(hhmm):
    try:
        h, m = str(hhmm).split(":")
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        return None


def in_window(cfg, now=None):
    """True when renaming is allowed right now."""
    if cfg.get("rename_mode", "immediate") != "window":
        return True
    w = cfg.get("rename_window") or {}
    a, b = _mins(w.get("start")), _mins(w.get("end"))
    if a is None or b is None or a == b:
        return True
    t = now if now is not None else time.localtime()
    cur = t.tm_hour * 60 + t.tm_min
    return a <= cur < b if a < b else (cur >= a or cur < b)  # window may cross midnight


_housekeeping = {"day": None}


def housekeeping_due(cfg, now=None):
    t = now if now is not None else time.localtime()
    at = _mins(cfg.get("housekeeping_time"))
    if at is None:
        return False
    today = time.strftime("%Y-%m-%d", t)
    if _housekeeping["day"] == today or t.tm_hour * 60 + t.tm_min < at:
        return False
    return True


def run_once(cfg, manual=False):
    """One cycle. The HDD is only touched when it must be:
    - a download finished (qBit has just woken the HDD to move it there), or
    - you pressed "Check now" / approved a review / pressed Undo, or
    - the daily housekeeping time, if you chose "daily" instead of "when the HDD is awake"."""
    osub, tmdb = make_clients(cfg)
    done = 0
    try:
        qb = QBit.from_cfg(cfg["qbit"])
        try:
            folders.attach(cfg, qb)              # qBit API
            rev = {v: k for k, v in cfg["categories"].items()}
            torrents = qb.list()                 # qBit API - fetched once, shared by every pass
            removed = guard_pass(qb, cfg, rev, torrents)
            dead_pass(qb, cfg, rev, torrents, removed)
            peek_cleanup(qb, torrents, removed)
            subs_search_pass(osub, cfg)          # internet + SSD
            if manual or in_window(cfg):
                done += completed_pass(qb, osub, tmdb, cfg, rev, torrents, removed)  # HDD only if something finished
            mode = cfg.get("housekeeping", "awake")
            if manual or (mode != "off" and done > 0):
                done += awake_housekeeping(osub, tmdb, cfg)        # HDD is awake right now anyway
            elif mode == "daily" and housekeeping_due(cfg):
                done += awake_housekeeping(osub, tmdb, cfg)
                _housekeeping["day"] = time.strftime("%Y-%m-%d")
        finally:
            qb.close()
    finally:
        osub.close()
    return done


def approve_review(rid, targets, cfg):
    """targets: list of destination paths relative to Completed (user-edited), one per item."""
    folders.attach(cfg)
    r = activity.get_review(rid)
    if not r or r["status"] != "pending":
        raise ValueError("review not found")
    completed = config.completed_dir(cfg)
    items = r["items"]
    if len(targets) != len(items):
        raise ValueError("wrong number of names")
    for it, rel in zip(items, targets):
        rel = rel.strip().lstrip("/\\")
        ext = os.path.splitext(it["src"])[1].lower()
        if not rel.lower().endswith(ext):
            rel += ext
        it["dst"] = os.path.normpath(os.path.join(completed, *re.split(r"[\\/]", rel)))
    osub, _ = make_clients(cfg)
    try:
        execute(items, r["label"], cfg, osub)
    finally:
        osub.close()
    activity.set_review_status(rid, "done")
    if r["hash"] and cfg.get("auto_remove", True):
        qb = QBit.from_cfg(cfg["qbit"])
        try:
            qb.delete(r["hash"], delete_files=True)
        except Exception as e:
            log.warning("couldn't remove torrent after review: %s", e)
        finally:
            qb.close()
