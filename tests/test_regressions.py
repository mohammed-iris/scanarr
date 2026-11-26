"""One test per bug found in the full review."""
import asyncio
import os
import threading

import httpx
import pytest

from app import activity, config, folders, processor


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(config, "SETTINGS_FILE", str(tmp_path / "config" / "settings.json"))
    folders.clear()
    processor._backoff.clear()
    yield


MB = 2 ** 20


def mk(p, size):
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "wb") as f:
        f.truncate(size)


class Q:
    def __init__(self, root, torrents, files):
        self.root, self.torrents, self._files, self.deleted, self.tags = root, torrents, files, [], {}

    def preferences(self):
        return {"save_path": str(self.root / "completed"), "temp_path_enabled": True, "temp_path": str(self.root / "temp")}

    def list(self):
        return self.torrents

    def files(self, h):
        return self._files[h]

    def delete(self, h, delete_files=True):
        self.deleted.append((h, delete_files))

    def add_tag(self, h, t):
        self.tags.setdefault(h, []).append(t)

    def control(self, *a):
        pass

    def close(self):
        pass


class NoSubs:
    enabled = False


class NoTMDb:
    key = ""


# 1. wake() from another thread must wake the worker loop
def test_wake_from_thread_is_thread_safe():
    from app import main

    async def scenario():
        main._loop["loop"] = asyncio.get_running_loop()
        main._wake.clear()
        threading.Thread(target=main.wake, kwargs={"manual": True}).start()
        await asyncio.wait_for(main._wake.wait(), 2)
        return main._manual["flag"]
    assert asyncio.run(scenario()) is True
    main._manual["flag"] = False


# 2. OpenSubtitles: log in once, reuse the token; quota cached
def test_opensubtitles_logs_in_once(monkeypatch):
    from app import subtitles
    subtitles._SESSIONS.clear()
    calls = {"login": 0, "infos": 0}

    def handler(req):
        if req.url.path.endswith("/login"):
            calls["login"] += 1
            return httpx.Response(200, json={"token": "T", "base_url": "api.opensubtitles.com"})
        if req.url.path.endswith("/infos/user"):
            calls["infos"] += 1
            return httpx.Response(200, json={"data": {"remaining_downloads": 18}})
        return httpx.Response(404)
    real = httpx.Client
    monkeypatch.setattr(subtitles.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    for _ in range(5):  # e.g. five status-bar refreshes
        o = subtitles.OpenSubtitles("key", "user", "pw")
        assert o.quota() == 18
        o.close()
    assert calls == {"login": 1, "infos": 1}


# 3. torrents with another app's category are never touched
def test_other_apps_torrents_ignored(tmp_path):
    base = tmp_path / "completed"
    mk(base / "Movie.2020.1080p.mkv", 90 * MB)
    t = {"hash": "s1", "name": "Movie.2020.1080p", "save_path": str(base), "category": "radarr", "tags": "",
         "progress": 1, "state": "uploading"}
    qb = Q(tmp_path, [t], {"s1": [{"name": "Movie.2020.1080p.mkv", "size": 90 * MB}]})
    cfg = folders.attach(config.load(), qb)
    processor.completed_pass(qb, NoSubs(), NoTMDb(), cfg, {"movies": "movies", "tv": "tv"})
    assert (base / "Movie.2020.1080p.mkv").exists() and qb.deleted == []
    processor.guard_pass(qb, cfg, {"movies": "movies"})
    assert qb.tags == {}


# 4. a dismissed Unsorted item doesn't come back
def test_dismissed_unsorted_not_requeued(tmp_path):
    import time
    qb = Q(tmp_path, [], {})
    cfg = folders.attach(config.load(), qb)
    f = tmp_path / "completed/Unsorted/zzqx.mkv"
    mk(f, 80 * MB)
    os.utime(f, (time.time() - 600,) * 2)
    processor.unsorted_pass(NoSubs(), NoTMDb(), cfg)
    r = activity.reviews()
    assert len(r) == 1
    activity.set_review_status(r[0]["id"], "dismissed")
    processor.unsorted_pass(NoSubs(), NoTMDb(), cfg)
    assert activity.reviews() == []


# 5. a failed move doesn't leave temporary subtitle files behind
def test_failed_move_cleans_temp_subs(tmp_path, monkeypatch):
    src = tmp_path / "completed/Incoming/Film.2021.1080p.mkv"
    mk(src, 90 * MB)
    cfg = folders.attach(config.load(), Q(tmp_path, [], {}))
    cfg["subtitle_languages"] = ["en"]

    class Subs:
        enabled = True
        def fetch(self, lang, video, dest, **kw):
            open(dest, "w").write("x")
            return "ok"
    monkeypatch.setattr(processor.shutil, "move", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
    items = [{"src": str(src), "dst": str(tmp_path / "completed/Movies/Film (2021)/Film (2021).mkv"), "meta": {}}]
    with pytest.raises(OSError):
        processor.execute(items, "x", cfg, Subs())
    assert [p for p in os.listdir(src.parent) if "scanarr-part" in p] == []


# 6. an interrupted season-pack rename resumes instead of going to review
def test_interrupted_rename_resumes(tmp_path, monkeypatch):
    inc = tmp_path / "completed/Incoming/Show.S01"
    files = []
    for e in (1, 2):
        mk(inc / f"Show.S01E0{e}.1080p.mkv", 90 * MB)
        files.append({"name": f"Show.S01/Show.S01E0{e}.1080p.mkv", "size": 90 * MB})
    t = {"hash": "p1", "name": "Show.S01", "save_path": str(tmp_path / "completed/Incoming"), "category": "tv",
         "tags": "", "progress": 1, "state": "uploading"}
    qb = Q(tmp_path, [t], {"p1": files})
    cfg = folders.attach(config.load(), qb)
    # simulate: episode 1 was renamed, then the power went out
    ep1 = tmp_path / "completed/TV/Show/Season 01/Show S01E01.mkv"
    ep1.parent.mkdir(parents=True)
    os.replace(inc / "Show.S01E01.1080p.mkv", ep1)
    activity.library_add(str(ep1))
    files_now = [files[1]] + [dict(files[0])]
    monkeypatch.setattr(processor, "_ready", lambda *a: True)  # file 1 is gone from Incoming on purpose
    qb._files["p1"] = files_now
    assert processor.completed_pass(qb, NoSubs(), NoTMDb(), cfg, {"tv": "tv"}) == 1
    assert (tmp_path / "completed/TV/Show/Season 01/Show S01E02.mkv").exists() and activity.reviews() == []


# 7. one malformed indexer result doesn't hide the others
def test_malformed_item_skipped():
    from app.indexers import parse_torznab
    xml = """<rss xmlns:torznab="http://torznab.com/schemas/2015/feed"><channel>
      <item><title>Good 2020 1080p</title><size>2000000000</size><link>magnet:?xt=urn:btih:aaaa</link>
        <torznab:attr name="seeders" value="5"/></item>
      <item><title>Bad size</title><size>lots</size><link>magnet:?xt=urn:btih:bbbb</link>
        <torznab:attr name="seeders" value="n/a"/></item>
      <item><title></title><link></link></item></channel></rss>"""
    r = parse_torznab(xml, "x")
    assert [i["title"] for i in r] == ["Good 2020 1080p", "Bad size"] and r[1]["size"] == 0 and r[1]["seeders"] == 0


# 8. wrong passwords are rate-limited
def test_login_rate_limited():
    from fastapi.testclient import TestClient
    from app import auth, main
    main._fails.clear()
    a = auth.ensure(announce=False)
    c = TestClient(main.app)
    main.time.sleep = lambda s: None
    for _ in range(5):
        assert c.post("/api/login", json={"username": "admin", "password": "nope"}).status_code == 401
    assert c.post("/api/login", json={"username": "admin", "password": a["password"]}).status_code == 429
    main._fails.clear()


# 9a. a blank username can't lock you out
def test_blank_username_rejected():
    from fastapi.testclient import TestClient
    from app import auth, main
    main._fails.clear()
    a = auth.ensure(announce=False)
    c = TestClient(main.app)
    c.post("/api/login", json={"username": a["username"], "password": a["password"]})
    s = c.get("/api/settings").json()
    s["auth"]["username"] = "   "
    c.post("/api/settings", json=s)
    assert config.load()["auth"]["username"] == "admin"


# 9b. non-English notification titles are encoded, not garbled
def test_notification_header_encoding():
    from app.watch import _header
    assert _header("Dune") == "Dune"
    import base64
    enc = _header("லியோ")
    assert enc.startswith("=?UTF-8?B?") and base64.b64decode(enc[10:-2]).decode() == "லியோ"


# 9c. settings cache hands out copies (one caller's change can't leak into another's)
def test_config_cache_returns_copies():
    config.save(config.load())
    a = config.load()
    a["folders"] = {"completed": "/x"}
    a["prefs"]["quality"] = "720p"
    b = config.load()
    assert "folders" not in b and b["prefs"]["quality"] == "1080p"


# 10. Windows qBittorrent + Scanarr in a container: path map handles C:\ paths
def test_path_map_windows_to_container():
    pm = {"from": "C:\\Scanarr", "to": "/data"}
    assert config.map_path("C:\\Scanarr\\completed", pm) == os.path.join("/data", "completed")
    assert config.map_path("C:/Scanarr/completed/Incoming/", pm) == os.path.join("/data", "completed", "Incoming")
    assert config.map_path("c:\\scanarr\\temp", pm) == os.path.join("/data", "temp")
    assert config.map_path("/other/path", pm) == "/other/path"
    assert config.map_path("C:\\ScanarrX\\a", pm) == "C:\\ScanarrX\\a"


def test_paths_sent_to_qbit_are_translated_back():
    pm = {"from": "C:\\Scanarr", "to": "/data"}
    assert config.unmap_path("/data/completed/Incoming", pm) == "C:\\Scanarr\\completed\\Incoming"
    assert config.unmap_path("/data/temp", pm) == "C:\\Scanarr\\temp"
    assert config.unmap_path("/elsewhere", pm) == "/elsewhere"
    assert config.unmap_path("/data/completed/Audio", {}) == "/data/completed/Audio"
    # round trip
    assert config.map_path(config.unmap_path("/data/completed/TV", pm), pm) == os.path.join("/data", "completed", "TV")


def test_send_uses_qbit_paths(monkeypatch):
    from fastapi.testclient import TestClient
    from app import auth, main
    main._fails.clear()
    sent = {}

    class FakeQ:
        def add(self, url, cat, savepath=None, download_path=None):
            sent.update(savepath=savepath, temp=download_path)
        def free_space(self): return 10 ** 13
        def close(self): pass
    cfg0 = config.load()
    cfg0["path_map"] = {"from": "C:\\Scanarr", "to": "/data"}
    config.save(cfg0)

    def cf():
        c = config.load()
        c["folders"] = {"completed": "/data/completed", "temp": "/data/temp"}
        return c
    monkeypatch.setattr(main, "_qb", lambda: FakeQ())
    monkeypatch.setattr(main, "_cfg_with_folders", cf)
    monkeypatch.setattr(main.processor, "free_bytes", lambda p: 10 ** 13)
    a = auth.ensure(announce=False)
    c = TestClient(main.app)
    c.post("/api/login", json={"username": a["username"], "password": a["password"]})
    assert c.post("/api/send", json={"download": "magnet:x", "kind": "movies"}).status_code == 200
    assert sent == {"savepath": "C:\\Scanarr\\completed\\Incoming", "temp": "C:\\Scanarr\\temp"}


# big catch-ups run part by part and report progress
def test_unsorted_runs_in_batches(tmp_path, monkeypatch):
    import time
    monkeypatch.setattr(processor, "BATCH_ITEMS", 5)
    qb = Q(tmp_path, [], {})
    cfg = folders.attach(config.load(), qb)
    for i in range(12):
        f = tmp_path / f"completed/Unsorted/Some Movie {1990 + i} 1080p.mkv"
        mk(f, 80 * MB)
        os.utime(f, (time.time() - 600,) * 2)
    done = processor.unsorted_pass(NoSubs(), NoTMDb(), cfg)
    assert done + len(activity.reviews()) == 5
    assert processor._more["unsorted"] and activity.kv_json("unsorted_progress")["left"] == 7
    processor.unsorted_pass(NoSubs(), NoTMDb(), cfg)
    processor.unsorted_pass(NoSubs(), NoTMDb(), cfg)
    assert not processor._more["unsorted"] and activity.kv_json("unsorted_progress")["left"] == 0
    assert processor.PROGRESS == {}


def test_library_index_scale(tmp_path):
    """~16 TB worth of files: 3,000 movies + 20,000 episodes."""
    import time
    qb = Q(tmp_path, [], {})
    cfg = folders.attach(config.load(), qb)
    mv, tv = tmp_path / "completed/Movies", tmp_path / "completed/TV"
    for i in range(3000):
        d = mv / f"Film {i} (2001)"
        d.mkdir(parents=True)
        (d / f"Film {i} (2001).mkv").touch()
        (d / f"Film {i} (2001).en.srt").touch()
    for s in range(200):
        d = tv / f"Show {s}" / "Season 01"
        d.mkdir(parents=True)
        for e in range(100):
            (d / f"Show {s} S01E{e:02d}.mkv").touch()
    (mv / "@eaDir").mkdir()
    (mv / "@eaDir" / "thumb.mkv").touch()
    t = time.time()
    processor.library_scan(cfg)
    took = time.time() - t
    assert activity.kv_json("library_stats")["files"] == 23000
    t = time.time()
    for i in range(200):
        assert activity.library_find(str(mv / f"Film {i} (2001)"))
    assert time.time() - t < 1
    assert activity.library_find(str(mv / "Not There (1999)")) is None
    assert took < 30, took


def _wait(cond, t=3.0):
    import time
    end = time.time() + t
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_change_events_keep_index_current(tmp_path):
    from app import fswatch
    qb = Q(tmp_path, [], {})
    cfg = folders.attach(config.load(), qb)
    try:
        assert processor.start_watching(cfg), fswatch.STATE
        mv = tmp_path / "completed/Movies"
        d = mv / "Alien (1979)"
        d.mkdir()
        (d / "Alien (1979).mkv").write_bytes(b"x")
        assert _wait(lambda: activity.library_find(str(d)))
        (d / "Alien (1979).mkv").unlink()
        assert _wait(lambda: activity.library_find(str(d)) is None)
        # a whole folder moved in from elsewhere
        src = tmp_path / "elsewhere/Heat (1995)"
        src.mkdir(parents=True)
        (src / "Heat (1995).mp4").write_bytes(b"x")
        os.rename(src, mv / "Heat (1995)")
        assert _wait(lambda: activity.library_find(str(mv / "Heat (1995)")))
        # a removed folder takes its files out of the index
        import shutil
        shutil.rmtree(mv / "Heat (1995)")
        assert _wait(lambda: activity.library_find(str(mv / "Heat (1995)")) is None)
        # something dropped into Unsorted is noticed (but waits until it's been quiet a while)
        (tmp_path / "completed/Unsorted/x.mkv").write_bytes(b"x")
        assert _wait(lambda: processor._events["unsorted_dirty"])
        assert not processor.unsorted_due()
        import time
        assert processor.unsorted_due(time.time() + processor.STABLE_SECONDS + 10)
    finally:
        fswatch.stop()
        processor._events["unsorted_dirty"] = False


def test_unreliable_shares_fall_back_to_scans(monkeypatch, tmp_path):
    from app import fswatch
    monkeypatch.setattr(fswatch, "fs_type", lambda p: "9p")
    ok, why = fswatch.usable(str(tmp_path))
    assert not ok and "scans" in why
    assert fswatch.ensure([str(tmp_path)], lambda *a: None) is False and not fswatch.STATE["active"]


def test_rescan_skips_unchanged_folders(tmp_path, monkeypatch):
    import time
    qb = Q(tmp_path, [], {})
    cfg = folders.attach(config.load(), qb)
    mv = tmp_path / "completed/Movies"
    for i in range(50):
        d = mv / f"Film {i} (2001)"
        d.mkdir(parents=True)
        (d / f"Film {i} (2001).mkv").touch()
    old = time.time() - 3600
    for d in [mv, *mv.iterdir()]:
        os.utime(d, (old, old))
    processor.library_scan(cfg)
    calls = []
    real = os.scandir
    monkeypatch.setattr(processor.os, "scandir", lambda p: (calls.append(p), real(p))[1])
    processor.library_scan(cfg)
    assert activity.kv_json("library_stats")["files"] == 50
    assert all(not str(c).startswith(str(mv)) for c in calls), calls   # nothing in Movies re-read
    # a changed folder is re-read
    (mv / "Film 3 (2001)" / "Film 3 (2001) extra.mkv").touch()
    os.utime(mv / "Film 3 (2001)", (old + 10, old + 10))
    calls.clear()
    processor.library_scan(cfg)
    assert [c for c in calls if str(c).startswith(str(mv))] == [str(mv / "Film 3 (2001)")]
    assert activity.kv_json("library_stats")["files"] == 51
