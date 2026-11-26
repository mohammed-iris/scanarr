"""End-to-end flow with a fake qBittorrent and real folders: temp -> completed -> rename."""
import os
import time

import pytest

from app import activity, config, folders, processor


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(config, "SETTINGS_FILE", str(tmp_path / "config" / "settings.json"))
    folders.clear()
    processor._backoff.clear()
    processor._housekeeping["day"] = None
    yield


class FakeQB:
    def __init__(self, root, torrents=(), files=None):
        self.root = root
        self.torrents, self._files = list(torrents), files or {}
        self.tags, self.deleted, self.stopped = {}, [], []

    def preferences(self):
        return {"save_path": str(self.root / "completed"), "temp_path_enabled": True,
                "temp_path": str(self.root / "temp")}

    def list(self):
        return [t for t in self.torrents if t["hash"] not in [d[0] for d in self.deleted]]

    def files(self, h):
        return self._files[h]

    def add_tag(self, h, tag):
        self.tags.setdefault(h, []).append(tag)
        for t in self.torrents:
            if t["hash"] == h:
                t["tags"] = ",".join(filter(None, [t.get("tags", ""), tag]))

    def delete(self, h, delete_files=True):
        self.deleted.append((h, delete_files))

    def control(self, action, hashes):
        self.stopped += hashes

    def close(self):
        pass


class NoSubs:
    enabled = False


class FakeSubs:
    """Records whether the ORIGINAL file existed when subtitles were fetched."""
    enabled = True

    def __init__(self):
        self.calls = []

    def fetch(self, lang, video, dest, **kw):
        self.calls.append((video, bool(video) and os.path.exists(video), kw.get("release"), kw.get("moviehash")))
        with open(dest, "w") as f:
            f.write("1\n00:00:01,000 --> 00:00:02,000\nhi\n")
        return "hash match: test"


class NoTMDb:
    key = ""


class TMDbNoMatch:
    key = "x"
    def movie(self, t, y): return None
    def show(self, t, y): return None


def mk(path, size):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        f.truncate(size)


def cfg_for(qb):
    return folders.attach(config.load(), qb)


MB = 2 ** 20
REV = {"movies": "movies", "tv": "tv", "audio": "audio"}


def movie_torrent(root, name="Inception.2010.1080p.BluRay.x264-GRP", extra=()):
    inc = root / "completed" / "Incoming"
    files = [{"name": f"{name}/{name}.mkv", "size": 200 * MB},
             {"name": f"{name}/Sample/sample.mkv", "size": 60 * MB},
             {"name": f"{name}/{name}.nfo", "size": 100}] + list(extra)
    for f in files:
        mk(inc / f["name"], f["size"])
    t = {"hash": "h1", "name": name, "save_path": str(inc), "category": "movies", "tags": "",
         "progress": 1, "state": "uploading"}
    return t, files


def test_folders_come_from_qbit(tmp_path):
    qb = FakeQB(tmp_path)
    cfg = cfg_for(qb)
    assert cfg["folders"] == {"completed": str(tmp_path / "completed"), "temp": str(tmp_path / "temp")}
    assert config.kind_dir(cfg, "movies") == str(tmp_path / "completed" / "Movies")


def test_movie_renamed_in_place_subs_fetched_first_torrent_removed(tmp_path):
    t, files = movie_torrent(tmp_path)
    qb = FakeQB(tmp_path, [t], {"h1": files})
    cfg = cfg_for(qb)
    cfg["subtitle_languages"] = ["en"]
    subs = FakeSubs()
    assert processor.completed_pass(qb, subs, NoTMDb(), cfg, REV) == 1
    movie = tmp_path / "completed/Movies/Inception (2010)/Inception (2010).mkv"
    assert movie.exists()
    assert sorted(os.listdir(movie.parent)) == ["Inception (2010).en.srt", "Inception (2010).mkv"]
    # subtitles searched on the original file, while it still had its release name
    video, existed, release, _ = subs.calls[0]
    assert existed and video.endswith("Inception.2010.1080p.BluRay.x264-GRP.mkv")
    assert release == "Inception.2010.1080p.BluRay.x264-GRP"
    # original name gone (renamed, not copied); torrent + junk removed; no seeding
    assert not (tmp_path / "completed/Incoming/Inception.2010.1080p.BluRay.x264-GRP/Inception.2010.1080p.BluRay.x264-GRP.mkv").exists()
    assert qb.deleted == [("h1", True)]
    assert activity.renames()[0]["dst"] == str(movie)


def test_waits_while_still_in_temp_or_incomplete(tmp_path):
    t, files = movie_torrent(tmp_path)
    t["save_path"] = str(tmp_path / "temp")  # qBit hasn't moved it yet
    qb = FakeQB(tmp_path, [t], {"h1": files})
    assert processor.completed_pass(qb, NoSubs(), NoTMDb(), cfg_for(qb), REV) == 0
    t2, files2 = movie_torrent(tmp_path)
    mk(tmp_path / "completed/Incoming" / files2[0]["name"], 10 * MB)  # still being written
    qb2 = FakeQB(tmp_path, [t2], {"h1": files2})
    assert processor.completed_pass(qb2, NoSubs(), NoTMDb(), cfg_for(qb2), REV) == 0
    assert qb2.deleted == []


def test_uncertain_goes_to_review_untouched_then_approve(tmp_path, monkeypatch):
    t, files = movie_torrent(tmp_path, name="xyz.1080p.WEB")
    qb = FakeQB(tmp_path, [t], {"h1": files})
    cfg = cfg_for(qb)
    processor.completed_pass(qb, NoSubs(), TMDbNoMatch(), cfg, REV)
    src = tmp_path / "completed/Incoming/xyz.1080p.WEB/xyz.1080p.WEB.mkv"
    assert src.exists() and qb.stopped == ["h1"] and qb.deleted == []
    r = activity.reviews()[0]
    assert r["hash"] == "h1" and len(r["items"]) == 1
    # user fixes the name in the UI
    monkeypatch.setattr(processor, "QBit", type("Q", (), {"from_cfg": staticmethod(lambda q: qb)}))
    monkeypatch.setattr(processor.folders, "attach", lambda c, qb_=None: (c.update(folders=cfg["folders"]) or c))
    processor.approve_review(r["id"], ["Movies/My Film (2021)/My Film (2021)"], config.load())
    assert (tmp_path / "completed/Movies/My Film (2021)/My Film (2021).mkv").exists()
    assert not src.exists() and qb.deleted == [("h1", True)]


def test_never_overwrites_existing(tmp_path):
    mk(tmp_path / "completed/Movies/Inception (2010)/Inception (2010).mkv", 5 * MB)
    t, files = movie_torrent(tmp_path)
    qb = FakeQB(tmp_path, [t], {"h1": files})
    processor.completed_pass(qb, NoSubs(), NoTMDb(), cfg_for(qb), REV)
    assert os.path.getsize(tmp_path / "completed/Movies/Inception (2010)/Inception (2010).mkv") == 5 * MB
    assert "already exists" in activity.reviews()[0]["reason"]


def test_undo_restores_original(tmp_path):
    t, files = movie_torrent(tmp_path)
    qb = FakeQB(tmp_path, [t], {"h1": files})
    cfg = cfg_for(qb)
    cfg["subtitle_languages"] = ["en"]
    processor.completed_pass(qb, FakeSubs(), NoTMDb(), cfg, REV)
    rid = activity.renames()[0]["id"]
    processor.undo(rid)
    orig = tmp_path / "completed/Incoming/Inception.2010.1080p.BluRay.x264-GRP/Inception.2010.1080p.BluRay.x264-GRP.mkv"
    assert orig.exists() and orig.with_suffix(".en.srt").exists()
    assert not (tmp_path / "completed/Movies/Inception (2010)").exists()  # empty folder cleaned up


def test_season_pack(tmp_path):
    inc = tmp_path / "completed/Incoming"
    name = "Fool.Night.S01.1080p.WEB.H264-SUGOI"
    files = []
    for e in (1, 2):
        rel = f"{name}/Fool.Night.S01E0{e}.1080p.WEB.H264-SUGOI.mkv"
        mk(inc / rel, 120 * MB)
        files.append({"name": rel, "size": 120 * MB})
    t = {"hash": "h2", "name": name, "save_path": str(inc), "category": "tv", "tags": "", "progress": 1, "state": "stalledUP"}
    qb = FakeQB(tmp_path, [t], {"h2": files})
    processor.completed_pass(qb, NoSubs(), NoTMDb(), cfg_for(qb), REV)
    assert sorted(os.listdir(tmp_path / "completed/TV/Fool Night/Season 01")) == [
        "Fool Night S01E01.mkv", "Fool Night S01E02.mkv"]


def test_other_types_kept_and_torrent_removed_without_files(tmp_path):
    d = tmp_path / "completed/Audio"
    mk(d / "Album/01.flac", 30 * MB)
    t = {"hash": "h3", "name": "Album", "save_path": str(d), "category": "audio", "tags": "", "progress": 1, "state": "uploading"}
    qb = FakeQB(tmp_path, [t], {"h3": [{"name": "Album/01.flac", "size": 30 * MB}]})
    processor.completed_pass(qb, NoSubs(), NoTMDb(), cfg_for(qb), REV)
    assert (d / "Album/01.flac").exists() and qb.deleted == [("h3", False)]


def test_guard_blocks_exe_in_movie(tmp_path):
    t = {"hash": "bad", "name": "New.Movie.2026.1080p", "category": "movies", "tags": "", "progress": 0.1, "state": "downloading"}
    qb = FakeQB(tmp_path, [t], {"bad": [{"name": "New.Movie/setup.exe", "size": 1000},
                                         {"name": "New.Movie/readme.txt", "size": 10}]})
    processor.guard_pass(qb, config.load(), REV)
    assert qb.deleted == [("bad", True)]
    assert activity.recent()[0]["status"] == "blocked"


def test_guard_allows_software(tmp_path):
    t = {"hash": "ok", "name": "Tool", "category": "software", "tags": "", "progress": 0.1}
    qb = FakeQB(tmp_path, [t], {"ok": [{"name": "Tool/setup.exe", "size": 1000}]})
    processor.guard_pass(qb, config.load(), {**REV, "software": "software"})
    assert qb.deleted == []


def test_dead_torrent_flagged_and_removed(tmp_path):
    old = time.time() - 7 * 3600
    t = {"hash": "d", "name": "Dead", "category": "movies", "tags": "", "progress": 0.0,
         "state": "stalledDL", "last_activity": old, "added_on": old}
    qb = FakeQB(tmp_path, [t], {})
    cfg = config.load()
    processor.dead_pass(qb, cfg, REV)
    assert qb.tags["d"] == [processor.DEAD_TAG]
    t["tags"] = ""
    cfg["dead_action"] = "remove"
    processor.dead_pass(qb, cfg, REV)
    assert qb.deleted == [("d", True)]


def test_unsorted_drop_folder(tmp_path):
    qb = FakeQB(tmp_path)
    cfg = cfg_for(qb)
    f = tmp_path / "completed/Unsorted/The.Bear.S02E03.1080p.WEB.mkv"
    mk(f, 80 * MB)
    old = time.time() - 600
    os.utime(f, (old, old))
    assert processor.unsorted_pass(NoSubs(), NoTMDb(), cfg) == 1
    assert (tmp_path / "completed/TV/The Bear/Season 02/The Bear S02E03.mkv").exists()


def test_subtitle_retry_queue(tmp_path):
    t, files = movie_torrent(tmp_path)
    qb = FakeQB(tmp_path, [t], {"h1": files})
    cfg = cfg_for(qb)
    cfg["subtitle_languages"] = ["en"]

    class NotYet(FakeSubs):
        def fetch(self, *a, **k): return None
    processor.completed_pass(qb, NotYet(), NoTMDb(), cfg, REV)
    assert activity.pending_subs_count() == 1
    activity._q("UPDATE subs_pending SET next_try=0")
    # retry: search by the stored hash, no file access; the find is parked on the SSD
    found = FakeSubs()
    processor.subs_search_pass(found, cfg)
    assert found.calls[0][0] is None and found.calls[0][3]  # no video path, hash supplied
    srt = tmp_path / "completed/Movies/Inception (2010)/Inception (2010).en.srt"
    assert not srt.exists() and len(activity.ready_subs()) == 1
    # next time the HDD is awake it's moved into place
    processor.subs_deliver_pass()
    assert srt.exists() and activity.pending_subs_count() == 0


def test_bundled_subtitle_used(tmp_path):
    name = "Inception.2010.1080p.BluRay.x264-GRP"
    t, files = movie_torrent(tmp_path, extra=[{"name": f"{name}/Subs/English.srt", "size": 500}])
    qb = FakeQB(tmp_path, [t], {"h1": files})
    cfg = cfg_for(qb)
    cfg["subtitle_languages"] = ["en"]
    processor.completed_pass(qb, NoSubs(), NoTMDb(), cfg, REV)
    assert (tmp_path / "completed/Movies/Inception (2010)/Inception (2010).en.srt").exists()


def test_torrent_added_directly_in_qbit_is_renamed(tmp_path):
    name = "The.Bear.S02E03.1080p.WEB.H264"
    base = tmp_path / "completed"  # qBit default save path, no category
    mk(base / f"{name}.mkv", 90 * MB)
    t = {"hash": "x1", "name": name, "save_path": str(base), "category": "", "tags": "", "progress": 1, "state": "uploading"}
    qb = FakeQB(tmp_path, [t], {"x1": [{"name": f"{name}.mkv", "size": 90 * MB}]})
    processor.completed_pass(qb, NoSubs(), NoTMDb(), cfg_for(qb), REV)
    assert (base / "TV/The Bear/Season 02/The Bear S02E03.mkv").exists()
    assert qb.deleted == [("x1", True)]


def test_external_non_video_left_in_place(tmp_path):
    base = tmp_path / "completed"
    mk(base / "Some Album/01.flac", 30 * MB)
    t = {"hash": "x2", "name": "Some Album", "save_path": str(base), "category": "", "tags": "", "progress": 1, "state": "uploading"}
    qb = FakeQB(tmp_path, [t], {"x2": [{"name": "Some Album/01.flac", "size": 30 * MB}]})
    processor.completed_pass(qb, NoSubs(), NoTMDb(), cfg_for(qb), REV)
    assert (base / "Some Album/01.flac").exists() and qb.deleted == [("x2", False)]


def test_external_disabled(tmp_path):
    base = tmp_path / "completed"
    mk(base / "Movie.2020.mkv", 90 * MB)
    t = {"hash": "x3", "name": "Movie.2020", "save_path": str(base), "category": "", "tags": "", "progress": 1, "state": "uploading"}
    qb = FakeQB(tmp_path, [t], {"x3": [{"name": "Movie.2020.mkv", "size": 90 * MB}]})
    cfg = cfg_for(qb)
    cfg["process_external"] = False
    processor.completed_pass(qb, NoSubs(), NoTMDb(), cfg, REV)
    assert (base / "Movie.2020.mkv").exists() and qb.deleted == []


def test_rename_window_and_housekeeping():
    import time as T
    cfg = config.load()
    at = lambda h, m: T.struct_time((2026, 11, 26, h, m, 0, 3, 330, 0))
    assert processor.in_window(cfg, at(15, 0))  # immediate by default
    cfg.update(rename_mode="window", rename_window={"start": "02:00", "end": "06:00"})
    assert processor.in_window(cfg, at(3, 0)) and not processor.in_window(cfg, at(15, 0))
    cfg["rename_window"] = {"start": "23:00", "end": "05:00"}  # crosses midnight
    assert processor.in_window(cfg, at(23, 30)) and processor.in_window(cfg, at(1, 0)) and not processor.in_window(cfg, at(12, 0))
    processor._housekeeping["day"] = None
    assert not processor.housekeeping_due(cfg, at(3, 0)) and processor.housekeeping_due(cfg, at(4, 10))


def test_window_defers_renaming(tmp_path, monkeypatch):
    t, files = movie_torrent(tmp_path)
    qb = FakeQB(tmp_path, [t], {"h1": files})
    monkeypatch.setattr(processor, "QBit", type("Q", (), {"from_cfg": staticmethod(lambda q: qb)}))
    monkeypatch.setattr(processor, "in_window", lambda c, now=None: False)
    monkeypatch.setattr(processor, "housekeeping_due", lambda c, now=None: False)
    assert processor.run_once(config.load()) == 0 and qb.deleted == []
    assert processor.run_once(config.load(), manual=True) == 1  # "Check now" overrides


def test_library_index_answers_without_disk(tmp_path):
    t, files = movie_torrent(tmp_path)
    qb = FakeQB(tmp_path, [t], {"h1": files})
    cfg = cfg_for(qb)
    processor.completed_pass(qb, NoSubs(), NoTMDb(), cfg, REV)
    target = os.path.join(config.kind_dir(cfg, "movies"), "Inception (2010)", "Inception (2010).mp4")
    assert activity.library_find(target)  # any extension matches, answered from the SSD database
    assert activity.library_find(os.path.join(config.kind_dir(cfg, "tv"), "Nope")) is None


def test_not_ready_backs_off_then_goes_to_review(tmp_path, monkeypatch):
    t, files = movie_torrent(tmp_path)
    os.remove(tmp_path / "completed/Incoming" / files[0]["name"])  # file vanished
    qb = FakeQB(tmp_path, [t], {"h1": files})
    cfg = cfg_for(qb)
    checks = {"n": 0}
    real = processor._ready
    monkeypatch.setattr(processor, "_ready", lambda *a: checks.__setitem__("n", checks["n"] + 1) or real(*a))
    processor.completed_pass(qb, NoSubs(), NoTMDb(), cfg, REV)
    processor.completed_pass(qb, NoSubs(), NoTMDb(), cfg, REV)  # within backoff: no disk check
    assert checks["n"] == 1
    for _ in range(processor.MAX_NOT_READY):
        processor._backoff.get("h1", {}).update(until=0)
        processor.completed_pass(qb, NoSubs(), NoTMDb(), cfg, REV)
    assert "aren't complete on disk" in activity.reviews()[0]["reason"]
    processor._backoff.clear()


def test_housekeeping_only_when_hdd_awake(tmp_path, monkeypatch):
    qb = FakeQB(tmp_path)
    monkeypatch.setattr(processor, "QBit", type("Q", (), {"from_cfg": staticmethod(lambda q: qb)}))
    ran = []
    monkeypatch.setattr(processor, "awake_housekeeping", lambda *a: ran.append(1) or 0)
    processor.run_once(config.load())              # nothing finished -> HDD left alone
    assert ran == []
    processor.run_once(config.load(), manual=True)  # Check now
    assert ran == [1]


def test_idle_cycle_never_touches_completed(tmp_path, monkeypatch):
    """With downloads in progress but nothing finished, a full cycle must not access the HDD."""
    downloading = {"hash": "d1", "name": "Some.Movie.2024.1080p", "category": "movies", "tags": "",
                   "progress": 0.4, "state": "downloading", "dlspeed": 1000, "save_path": str(tmp_path / "completed/Incoming")}
    qb = FakeQB(tmp_path, [downloading], {"d1": [{"name": "Some.Movie.2024.1080p.mkv", "size": 900 * MB}]})
    monkeypatch.setattr(processor, "QBit", type("Q", (), {"from_cfg": staticmethod(lambda q: qb)}))
    completed = str(tmp_path / "completed")
    touched = []
    import builtins
    import shutil as sh
    for mod, name in [(os, "stat"), (os, "listdir"), (os, "scandir"), (os, "walk"), (os.path, "exists"),
                      (os.path, "isfile"), (os.path, "isdir"), (os.path, "getsize"), (sh, "disk_usage"), (builtins, "open")]:
        real = getattr(mod, name)
        def spy(p=None, *a, _real=real, **k):
            if isinstance(p, (str, os.PathLike)) and str(p).startswith(completed):
                touched.append(str(p))
            return _real(p, *a, **k) if p is not None else _real(*a, **k)
        monkeypatch.setattr(mod, name, spy)
    activity.queue_sub(str(tmp_path / "completed/Movies/X (2020)/X (2020).mkv"), "en", {"hash": "abc"}, delay=0)
    processor.run_once(config.load())
    assert touched == []
