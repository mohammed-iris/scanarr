"""Setup wizard pieces: compose file, init (first run + Repair), bootstrap, disk guard, speed schedule."""
import json
import os

import pytest

from app import activity, bootstrap, config, processor, stack


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(config, "SETTINGS_FILE", str(tmp_path / "config" / "settings.json"))
    yield


def answers(**kw):
    a = dict(stack.DEFAULTS, username="iris", password="Tango-River-4471-Kite", temp="/srv/t", completed="/srv/c",
             config="/srv/cfg", indexers=["yts"], languages=["en"], torrent_port=51413)
    a.update(kw)
    return a


def init_env(a):
    e = {v: str(a[k]) for k, v in stack.ENV_KEYS.items() if a.get(k) is not None}
    e.update(SCANARR_INDEXERS=",".join(a["indexers"]), SCANARR_LANGUAGES=",".join(a["languages"]),
             SCANARR_TMDB_KEY=a.get("tmdb_api_key", ""))
    return e


def test_compose_is_valid_yaml_and_private(tmp_path):
    yaml = pytest.importorskip("yaml")
    text = stack.compose(answers(jellyfin=True, tailscale=True, tailscale_authkey="tskey-x"))
    d = yaml.safe_load(text)
    assert {"init", "qbittorrent", "prowlarr", "scanarr", "jellyfin", "tailscale"} <= set(d["services"])
    assert "ports" not in d["services"]["prowlarr"]
    assert all(":8080" not in p for p in d["services"]["qbittorrent"]["ports"])   # qBit web page not exposed
    assert d["services"]["scanarr"]["image"].endswith(":" + stack.DEFAULTS["version"])
    assert "docker.sock" not in text.split("  scanarr:")[1].split("\n\n")[0]


def test_init_first_run_then_repair_keeps_your_changes(tmp_path, monkeypatch):
    root, data = tmp_path / "cfg", tmp_path / "data"
    monkeypatch.setattr(stack, "_writable_as", lambda d, u, g: True)
    monkeypatch.setattr(stack, "_chown_tree", lambda *a: None)
    a = answers(tmdb_api_key="first")
    stack.init_from_env(str(root), init_env(a), str(data))
    sconf = root / "scanarr" / "settings.json"
    s = json.loads(sconf.read_text())
    assert s["stack"]["managed"] and s["tmdb_api_key"] == "first"
    # you change things in Scanarr afterwards
    s["tmdb_api_key"] = "changed-in-scanarr"
    s["min_free_gb"] = 7
    sconf.write_text(json.dumps(s))
    # Repair with the same wizard answers: your changes stay, setup is re-applied
    stack.init_from_env(str(root), init_env(a), str(data))
    s2 = json.loads(sconf.read_text())
    assert s2["tmdb_api_key"] == "changed-in-scanarr" and s2["min_free_gb"] == 7
    assert s2["stack"]["rev"] and not s2["stack"]["bootstrap_done"]
    # Repair after changing the key in the wizard: the wizard's new value wins
    stack.init_from_env(str(root), init_env(answers(tmdb_api_key="new-in-wizard")), str(data))
    assert json.loads(sconf.read_text())["tmdb_api_key"] == "new-in-wizard"
    # qBit/Prowlarr keys survive a Repair
    assert s2["qbit"]["api_key"] == s["qbit"]["api_key"] and s2["prowlarr"]["api_key"] == s["prowlarr"]["api_key"]


class FakeQ:
    def __init__(self, free=100 * 2**30):
        self.free, self.prefs, self.calls, self.tags = free, {}, [], {}

    def free_space(self):
        return self.free

    def control(self, action, hashes):
        self.calls.append((action, tuple(hashes)))

    def add_tag(self, h, t):
        self.tags[h] = t

    def remove_tag(self, h, t):
        self.tags.pop(h, None)

    def set_preferences(self, p):
        self.prefs.update(p)


def test_disk_guard_pauses_and_resumes(tmp_path):
    cfg = config.load()
    cfg["disk_guard_gb"] = 10
    q = FakeQ(free=3 * 2**30)
    ts = [{"hash": "a", "state": "downloading", "tags": ""}, {"hash": "b", "state": "stoppedUP", "tags": ""}]
    assert processor.disk_guard_pass(q, cfg, ts) == ["Completed"]
    assert q.calls == [("stop", ("a",))] and q.tags == {"a": processor.DISKFULL_TAG}
    # a little room isn't enough (no flapping) ...
    q.free, q.calls = 12 * 2**30, []
    ts[0].update(state="stoppedDL", tags=processor.DISKFULL_TAG)
    processor.disk_guard_pass(q, cfg, ts)
    assert q.calls == []
    # ... plenty of room resumes exactly the ones Scanarr paused
    q.free = 40 * 2**30
    processor.disk_guard_pass(q, cfg, ts)
    assert q.calls == [("start", ("a",))] and q.tags == {}


def test_speed_schedule_pushed_once(tmp_path):
    cfg = config.load()
    cfg["speed_schedule"] = {"enabled": True, "start": "18:30", "end": "23:00", "days": "weekdays", "dl_kbps": 500, "up_kbps": 20}
    q = FakeQ()
    assert processor.apply_speed_schedule(q, cfg)
    assert q.prefs["scheduler_enabled"] and q.prefs["schedule_from_hour"] == 18 and q.prefs["schedule_from_min"] == 30
    assert q.prefs["scheduler_days"] == 1 and q.prefs["alt_dl_limit"] == 500 * 1024
    assert not processor.apply_speed_schedule(q, cfg)          # unchanged: no extra calls
    cfg["speed_schedule"]["enabled"] = False
    assert processor.apply_speed_schedule(q, cfg) and q.prefs["scheduler_enabled"] is False


def test_bootstrap_waits_for_prowlarr_without_blocking(monkeypatch):
    cfg = config.load()
    cfg["stack"] = {"managed": True, "indexers": ["yts"], "rev": "r1"}
    config.save(cfg)
    cfg = config.load()

    class Q(FakeQ):
        def version(self):
            return "v5.3.1"

        def ensure_category(self, c):
            pass

        def close(self):
            pass

    q = Q()
    monkeypatch.setattr(bootstrap.QBit, "from_cfg", staticmethod(lambda c: q))
    calls = []

    def boom(cfg, wanted):
        calls.append(1)
        raise RuntimeError("Prowlarr has no indexer list yet - it needs internet access to download it")

    monkeypatch.setattr(bootstrap, "prowlarr_indexers", boom)
    bootstrap._next["prowlarr"] = 0
    assert bootstrap.run(cfg) is False and q.prefs["max_ratio"] == 0
    assert bootstrap.run(cfg) is False and len(calls) == 1          # retried later, not every cycle
    assert "internet" in bootstrap.report()["indexers"]["msg"]
    q.prefs.clear()
    bootstrap.run(cfg)
    assert "save_path" not in q.prefs          # qBit setup isn't repeated every cycle (only after a Repair)


def test_gateway_passes_app_cookies_cleanly_and_never_scanarrs(monkeypatch):
    import httpx
    from fastapi.testclient import TestClient
    from app import auth, main, proxy
    cfg = config.load()
    cfg["auth"]["password"] = "pw-123456789"
    cfg["qbit"]["url"] = "http://qbittorrent:8080"
    cfg["stack"] = {"managed": True}
    config.save(cfg)
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, stream=httpx.ByteStream(b"ok"), headers={"set-cookie": "QBT_SID_8080=new; path=/; HttpOnly"})

    monkeypatch.setitem(proxy._client, "c", httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    c = TestClient(main.app)
    tok = auth.make_token(config.load()["auth"])
    r = c.get("/qbit/images/x.svg", headers={"cookie": f"{auth.COOKIE}={tok}; QBT_SID_8080=abc; other=1",
                                             "origin": "http://nas:9797", "referer": "http://nas:9797/qbit/"})
    assert r.status_code == 200 and r.text == "ok"
    h = seen[-1].headers
    assert h["cookie"] == "QBT_SID_8080=abc; other=1"          # no leading space, no Scanarr session
    assert h["origin"] == "http://qbittorrent:8080"           # qBit's CSRF check passes
    assert "path=/qbit/" in r.headers["set-cookie"]
    assert c.get("/qbit/", headers={"cookie": "QBT_SID_8080=abc"}, follow_redirects=False).status_code in (302, 303, 307, 401)


def test_prowlarr_still_starting_is_retried_soon(monkeypatch):
    import httpx
    import time
    cfg = config.load()
    cfg["stack"] = {"managed": True, "indexers": ["yts"], "rev": "r2"}
    config.save(cfg)

    class Q(FakeQ):
        def version(self):
            return "v5.3.1"

        def ensure_category(self, c):
            pass

        def close(self):
            pass

    monkeypatch.setattr(bootstrap.QBit, "from_cfg", staticmethod(lambda c: Q()))

    def down(cfg, wanted):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(bootstrap, "prowlarr_indexers", down)
    bootstrap._next["prowlarr"] = 0
    bootstrap.run(config.load())
    assert bootstrap._next["prowlarr"] - time.time() <= bootstrap.STARTING_RETRY + 1
    assert "starting" in bootstrap.report()["indexers"]["msg"]


def test_api_keys_not_logged():
    import logging
    from app import main  # noqa: F401  (sets the levels)
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
