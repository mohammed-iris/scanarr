import os
from pathlib import Path

import pytest

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _cfgdir(tmp_path, monkeypatch):
    from app import config
    monkeypatch.setattr(config, "CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(config, "SETTINGS_FILE", str(tmp_path / "config" / "settings.json"))


# ---------- Torznab parsing (real Prowlarr output) ----------
@pytest.mark.parametrize("name,count,indexer", [("yts", 108, "YTS"), ("tpb", 50, "The Pirate Bay"),
                                                ("knaben", 98, "Knaben")])
def test_parse_prowlarr_feeds(name, count, indexer):
    from app.indexers import parse_torznab
    res = parse_torznab((FIX / f"{name}.xml").read_text(encoding="utf-8"), name)
    assert len(res) == count
    assert all(r["download"].startswith("magnet:?xt=urn:btih:") for r in res)
    assert all(r["size"] > 0 and len(r["infohash"]) == 40 for r in res)
    assert res[0]["indexer"] == indexer


# ---------- clean names ----------
@pytest.mark.parametrize("release,expected", [
    ("Inception.2010.1080p.BluRay.x264.DTS-FGT", "Inception (2010)"),
    ("The Slightest Touch (2026) 720p WEBRip x264 -YTS", "The Slightest Touch (2026)"),
    ("Â¡Ya llega! (2023) 720p WEBRip x264 -YTS", "¡Ya llega! (2023)"),
    ("Oppenheimer.2023.2160p.WEB-DL.DDP5.1.Atmos.HDR.H.265-FLUX", "Oppenheimer (2023)"),
])
def test_movie_names(release, expected):
    from app import naming
    i = naming.parse(release, "movie")
    assert naming.movie_name(i["title"], i["year"]) == expected


@pytest.mark.parametrize("release,expected", [
    ("Tracker.2024.S04E01.Thats.the.Job.1080p.DSNP.WEB-DL.ENG.ITA.DDP5.1.H264-TBK", "Tracker S04E01"),
    ("Fool Night S01E11 1080p WEB H264-SUGOI", "Fool Night S01E11"),
    ("The.Hunting.Wives.S02E04.XviD-AFG.avi", "The Hunting Wives S02E04"),
    ("Show.Name.S01E01E02.720p.HDTV.x264", "Show Name S01E01E02"),
])
def test_episode_names(release, expected):
    from app import naming
    i = naming.parse(release, "episode")
    assert naming.episode_name(i["title"], i["season"], i["episodes"]) == expected


def test_illegal_chars_removed():
    from app import naming
    assert naming.movie_name('Mission: Impossible?', 1996) == "Mission Impossible (1996)"


def test_opensubtitles_hash_of_zero_file(tmp_path):
    from app.subtitles import os_hash
    p = tmp_path / "z.bin"
    with open(p, "wb") as f:
        f.truncate(1_000_000)
    assert os_hash(p) == f"{1_000_000:016x}"  # all-zero data -> hash equals size


def client():
    from fastapi.testclient import TestClient
    from app import auth, main
    a = auth.ensure(announce=False)
    c = TestClient(main.app)
    assert c.post("/api/login", json={"username": a["username"], "password": a["password"]}).status_code == 200
    return c


def test_login_required_and_credentials_logged(capsys):
    from fastapi.testclient import TestClient
    from app import auth, main
    c = TestClient(main.app)
    assert c.get("/api/settings").status_code == 401
    assert c.get("/", follow_redirects=False).headers["location"] == "/login"
    a = auth.ensure(announce=True)
    out = capsys.readouterr().out
    assert a["password"] in out and "username: admin" in out
    assert c.post("/api/login", json={"username": "admin", "password": "wrong"}).status_code == 401
    assert c.post("/api/login", json={"username": "admin", "password": a["password"]}).status_code == 200
    assert c.get("/api/settings").json()["auth"]["password"] == main.SECRET_MASK


def test_settings_secrets_masked():
    from app import main
    c = client()
    s = c.get("/api/settings").json()
    s["qbit"]["password"] = "hunter2"
    s["indexers"] = [{"name": "YTS", "url": "http://x/5/api", "apikey": "abc", "enabled": True}]
    out = c.post("/api/settings", json=s).json()
    assert out["qbit"]["password"] == main.SECRET_MASK and out["indexers"][0]["apikey"] == main.SECRET_MASK
    c.post("/api/settings", json=out)  # sending the mask back keeps the real values
    from app import config
    real = config.load()
    assert real["qbit"]["password"] == "hunter2" and real["indexers"][0]["apikey"] == "abc"


def test_categories_mapped():
    from collections import Counter
    from app.indexers import parse_torznab
    c = Counter(r["category"] for r in parse_torznab((FIX / "tpb.xml").read_text(encoding="utf-8"), "tpb"))
    assert c == {"TV": 38, "Audio": 9, "XXX": 3}


# ---------- Prowlarr discovery, qBit API key, type routing ----------
def test_qbit_api_key_uses_bearer_and_skips_login():
    from app.qbit import QBit
    qb = QBit.from_cfg({"url": "http://q:9090", "api_key": "qbt_abc", "username": "", "password": ""})
    assert qb.c.headers["Authorization"] == "Bearer qbt_abc" and qb._logged_in
    qb.close()


def test_prowlarr_indexers_discovered(monkeypatch):
    import asyncio
    import httpx
    from app import indexers

    def handler(req):
        assert req.headers["X-Api-Key"] == "K"
        return httpx.Response(200, json=[
            {"id": 5, "name": "YTS", "enable": True, "protocol": "torrent"},
            {"id": 9, "name": "Off", "enable": False, "protocol": "torrent"},
            {"id": 7, "name": "Usenet", "enable": True, "protocol": "usenet"}])
    real = httpx.AsyncClient
    monkeypatch.setattr(indexers.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    indexers.clear_prowlarr_cache()
    found = asyncio.run(indexers.prowlarr_indexers({"url": "http://prowlarr:9696/", "api_key": "K"}))
    assert [(i["name"], i["url"], i["apikey"]) for i in found] == [("YTS", "http://prowlarr:9696/5/api", "K")]


@pytest.mark.parametrize("title,cat,kind", [
    ("Some Album 2026 FLAC", "Audio", "audio"), ("Linux ISO", "PC", "software"),
    ("Show.S02E03.1080p", "Other", "tv"), ("Random thing", "Other", "other"),
    ("Inception 2010", "Movies", "movies")])
def test_kind_guess(title, cat, kind):
    from app.indexers import guess_kind
    assert guess_kind(title, cat) == kind


def test_send_routes_non_video_to_type_folder(monkeypatch):
    from app import main
    sent = {}

    class FakeQ:
        def add(self, url, cat, savepath=None, download_path=None):
            sent.update(url=url, cat=cat, savepath=savepath, temp=download_path)
        def close(self): pass
    from app import config
    def cfgf():
        c = config.load()
        c["folders"] = {"completed": "/data/torrents", "temp": "/data/torrents/incomplete"}
        return c
    monkeypatch.setattr(main, "_qb", lambda: FakeQ())
    monkeypatch.setattr(main, "_cfg_with_folders", cfgf)
    monkeypatch.setattr(main.processor, "free_bytes", lambda p: 10**13)
    c = client()
    c.post("/api/send", json={"download": "magnet:x", "kind": "audio", "size": 100})
    assert sent == {"url": "magnet:x", "cat": "audio", "savepath": "/data/torrents/Audio",
                    "temp": "/data/torrents/incomplete"}
    c.post("/api/send", json={"download": "magnet:y", "kind": "movies"})
    assert sent["cat"] == "movies" and sent["savepath"] == "/data/torrents/Incoming"
    # disk check
    monkeypatch.setattr(main.processor, "free_bytes", lambda p: 1024**3)
    r = c.post("/api/send", json={"download": "magnet:z", "kind": "movies", "size": 5 * 1024**3})
    assert r.status_code == 409 and "Not enough space" in r.json()["detail"]
    assert c.post("/api/send", json={"download": "magnet:z", "kind": "movies", "size": 5 * 1024**3,
                                     "force": True}).status_code == 200


def test_saving_settings_keeps_you_logged_in():
    c = client()
    s = c.get("/api/settings").json()
    assert c.post("/api/settings", json=s).status_code == 200
    assert c.get("/api/settings").status_code == 200  # session still valid
    s["auth"]["password"] = "new-pass-456"
    assert c.post("/api/settings", json=s).status_code == 200
    assert c.get("/api/settings").status_code == 200  # new cookie issued
    from app import config
    assert config.load()["auth"]["password"] == "new-pass-456"


def test_backup_and_restore_roundtrip():
    from app import activity, config
    c = client()
    activity.log("x", "renamed", "/a", "b")
    r = c.get("/api/backup")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    blob = r.content
    s = c.get("/api/settings").json()
    s["tmdb_api_key"] = "changed"
    c.post("/api/settings", json=s)
    activity.log("y", "renamed", "/c", "d")
    r = c.post("/api/restore", content=blob, headers={"content-type": "application/zip"})
    assert r.status_code == 200
    assert config.load()["tmdb_api_key"] == "" and [a["torrent"] for a in activity.recent()] == ["x"]
    assert c.get("/api/settings").status_code == 200  # still logged in
    assert c.post("/api/restore", content=b"nope").status_code == 400


def test_junk_flags_and_best_score():
    from app.indexers import annotate
    rs = annotate([
        {"title": "Film 2024 1080p WEB-DL", "size": 2.5e9, "seeders": 50, "kind": "movies"},
        {"title": "Film 2024 HDCAM 1080p", "size": 2e9, "seeders": 500, "kind": "movies"},
        {"title": "Film 2024 2160p WEB", "size": 9e8, "seeders": 80, "kind": "movies"},
        {"title": "Film 2024 720p", "size": 9e8, "seeders": 0, "kind": "movies"},
    ], {"quality": "1080p", "max_gb_movie": 4})
    assert rs[1]["flags"] == ["cam"] and "small" in rs[2]["flags"] and "noseeds" in rs[3]["flags"]
    assert max(rs, key=lambda r: r["score"])["title"] == "Film 2024 1080p WEB-DL"


def test_indexer_health_skips_after_failures():
    import asyncio
    from app import indexers
    indexers.reset_health()
    for _ in range(indexers.FAIL_LIMIT):
        indexers._record("Bad", False, 15000, "timed out")
    assert indexers.skipped("Bad")
    out = asyncio.run(indexers.search_all([{"name": "Bad", "url": "http://127.0.0.1:1/api"}], "x"))
    assert out["errors"][0]["skipped"]
    indexers.reset_health("Bad")
    assert not indexers.skipped("Bad")


def test_peek_info_warnings():
    from app.processor import peek_info
    files, warns = peek_info([{"name": "Movie/Movie.2024.1080p.mkv.exe", "size": 2e6},
                              {"name": "Movie/password.txt", "size": 10}], "movies")
    assert files[0]["danger"] and any("program file" in w for w in warns) and any("password" in w for w in warns)
    files, warns = peek_info([{"name": "Show.S01E01.mkv", "size": 3e8}, {"name": "Show.S01E02.mkv", "size": 3e8}], "tv")
    assert warns == [] and all(f["video"] for f in files)


def test_watchlist_finds_only_new_matching_releases(monkeypatch):
    import asyncio
    from app import activity, indexers, watch
    feed = [{"title": "Dune Part Three 2026 2160p WEB", "size": 1.5e10, "seeders": 40, "kind": "movies",
             "category": "Movies", "indexer": "YTS", "download": "magnet:?xt=urn:btih:" + "a" * 40, "infohash": "a" * 40},
            {"title": "Dune Part Three 2026 HDCAM", "size": 2e9, "seeders": 900, "kind": "movies",
             "category": "Movies", "indexer": "TPB", "download": "magnet:?xt=urn:btih:" + "b" * 40, "infohash": "b" * 40}]

    async def fake_all(cfg): return [{"name": "x", "url": "u"}], []
    async def fake_search(idx, q, cat, timeout=15): return {"results": [dict(r) for r in feed], "errors": []}
    monkeypatch.setattr(indexers, "all_indexers", fake_all)
    monkeypatch.setattr(indexers, "search_all", fake_search)
    pushed = []
    monkeypatch.setattr(watch, "notify", lambda cfg, t, m, tags="": pushed.append(t))
    wid = activity.watch_add("dune part three", "movies", {"quality": ["2160p"], "hide_cam": True}, seen=[])
    assert asyncio.run(watch.check(activity.watch_get(wid), {})) == 1   # 2160p yes, CAM filtered
    assert asyncio.run(watch.check(activity.watch_get(wid), {})) == 0   # already seen
    assert len(activity.watch_get(wid)["hits"]) == 1 and pushed and "1 new" in pushed[0]
