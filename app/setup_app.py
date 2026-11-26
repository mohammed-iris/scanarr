"""Scanarr setup wizard (port 9700). Same on Windows (Docker Desktop), Linux and NAS.

With Docker access (the Docker socket mounted), it validates folders/ports with tiny helper containers,
deploys the stack and runs an end-to-end check. Without it (some NAS UIs block the socket), it still
validates what it can and gives you ONE compose file to paste into your Docker UI.
"""
import json
import os
import re
import secrets
import subprocess
import threading
import time

import httpx
from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from . import stack

app = FastAPI(title="Scanarr setup")
STATIC = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=STATIC), name="static")
PROJECT = "scanarr"
INDEXERS = [  # popular public indexers available in Prowlarr (definition names)
    {"id": "yts", "name": "YTS", "what": "Movies, small high-quality encodes", "default": True},
    {"id": "thepiratebay", "name": "The Pirate Bay", "what": "Everything (movies, TV, music, software)", "default": True},
    {"id": "Knaben", "name": "Knaben", "what": "Meta-search across many public sites", "default": True},
    {"id": "eztv", "name": "EZTV", "what": "TV episodes", "default": False},
    {"id": "1337x", "name": "1337x", "what": "Everything; often behind Cloudflare", "default": False},
    {"id": "limetorrents", "name": "LimeTorrents", "what": "Everything", "default": False},
    {"id": "nyaasi", "name": "Nyaa", "what": "Anime", "default": False},
]
LANGS = [("en", "English"), ("ml", "Malayalam"), ("ta", "Tamil"), ("hi", "Hindi"), ("ar", "Arabic"),
         ("te", "Telugu"), ("kn", "Kannada"), ("fr", "French"), ("es", "Spanish"), ("de", "German"),
         ("it", "Italian"), ("pt", "Portuguese"), ("ru", "Russian"), ("zh", "Chinese"), ("ja", "Japanese"),
         ("ko", "Korean"), ("tr", "Turkish"), ("ur", "Urdu"), ("bn", "Bengali"), ("id", "Indonesian")]


# ---------------------------------------------------------------- docker helpers
def docker(*args, input=None, timeout=120):
    try:
        p = subprocess.run(["docker", *args], input=input, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except FileNotFoundError:
        return 127, "", "docker CLI not found"
    except subprocess.TimeoutExpired:
        return 124, "", f"docker {' '.join(args[:2])} timed out"


_has = {"ok": None, "ts": 0.0}


def has_docker():
    if time.time() - _has["ts"] > 30:   # cached briefly: every check would otherwise ask Docker again
        _has.update(ok=os.path.exists("/var/run/docker.sock") and docker("version", "--format", "{{.Server.Version}}", timeout=15)[0] == 0,
                    ts=time.time())
    return _has["ok"]


_self = {"image": None, "id": None}


def self_image():
    if not _self["image"]:
        cid = os.environ.get("HOSTNAME", "")
        code, out, _ = docker("inspect", cid, "--format", "{{.Config.Image}}|{{.Id}}", timeout=15)
        if code == 0 and "|" in out:
            _self["image"], _self["id"] = out.split("|", 1)
        else:
            from .version import __version__
            _self["image"] = f"{stack.IMAGE}:{__version__}"     # e.g. host networking: hostname isn't the container
    return _self["image"]


_platform = {}


def platform():
    if _platform:
        return _platform
    code, out, _ = docker("info", "--format", "{{json .}}", timeout=20)
    info = json.loads(out) if code == 0 and out.startswith("{") else {}
    osname = info.get("OperatingSystem", "")
    kernel = info.get("KernelVersion", "")
    kind = "linux"
    if "Docker Desktop" in osname:
        kind = "windows" if ("microsoft" in kernel.lower() or "wsl" in kernel.lower() or info.get("OSType") == "linux") else "desktop"
    if kind == "linux" and host_exists("/volume1"):
        kind = "nas"
    _platform.update(kind=kind, os=osname or "unknown", arch=info.get("Architecture", ""),
                     cpus=info.get("NCPU"), mem=info.get("MemTotal"), docker=info.get("ServerVersion"),
                     ugreen="ugos" in osname.lower() or "ugreen" in (info.get("Name", "") or "").lower())
    _platform["desktop_prefix"] = desktop_prefix() if kind in ("windows", "desktop") else ""
    return _platform


def desktop_prefix():
    """Where Docker Desktop exposes C:\\ to the engine (WSL2 vs Hyper-V backends differ)."""
    for pre in ("/run/desktop/mnt/host", "/host_mnt"):
        code, out, _ = docker("run", "--rm", "--network", "none", "--mount", f"type=bind,source={pre}/c,target=/t,readonly",
                              "--entrypoint", "sh", self_image(), "-c", "ls /t | head -1", timeout=60)
        if code == 0 and out:
            return pre
    return "/run/desktop/mnt/host"


WIN = re.compile(r"^([A-Za-z]):[\\/](.*)$")


def engine_path(p):
    """Path as the Docker engine understands it. C:\\Scanarr\\temp -> /run/desktop/mnt/host/c/Scanarr/temp on Docker Desktop."""
    p = (p or "").strip()
    m = WIN.match(p)
    if m:
        pre = platform().get("desktop_prefix") or "/run/desktop/mnt/host"
        rest = m.group(2).replace("\\", "/").strip("/")
        return f"{pre}/{m.group(1).lower()}/{rest}".rstrip("/")
    return p.rstrip("/") or "/"


def host_exists(path):
    code, out, _ = docker("run", "--rm", "--network", "none", "--mount", f"type=bind,source={path},target=/t,readonly",
                          "--entrypoint", "true", self_image(), timeout=60)
    return code == 0


def helper(path, *cmd, readonly=False, timeout=90, stdin=None):
    """Run a command in a throwaway container with the host folder mounted at /t."""
    mount = f"type=bind,source={engine_path(path)},target=/t" + (",readonly" if readonly else "")
    args = ["run", "--rm", "--network", "none"] + (["-i"] if stdin is not None else []) + ["--user", "0:0", "--mount", mount,
                                                                       "--entrypoint", cmd[0], self_image(), *cmd[1:]]
    return docker(*args, input=stdin, timeout=timeout)


def existing_stack():
    code, out, _ = docker("ps", "-a", "--filter", f"label=com.docker.compose.project={PROJECT}",
                          "--format", "{{.Names}}|{{.State}}|{{.Image}}|{{.Label \"com.docker.compose.service\"}}", timeout=20)
    if code != 0 or not out:
        return None
    rows = [dict(zip(("name", "state", "image", "service"), l.split("|"))) for l in out.splitlines()]
    return {"containers": rows}


def answers_from_install():
    """Prefill Repair/Update from the existing init container (its env + mounts)."""
    code, out, _ = docker("ps", "-a", "--filter", f"label=com.docker.compose.project={PROJECT}",
                          "--filter", "label=com.docker.compose.service=init", "--format", "{{.ID}}", timeout=20)
    if code != 0 or not out:
        return None
    code, out, _ = docker("inspect", out.splitlines()[0], timeout=20)
    if code != 0:
        return None
    c = json.loads(out)[0]
    env = dict(e.split("=", 1) for e in c["Config"]["Env"] if "=" in e)
    a = stack.answers_from_env(env)
    for m in c.get("Mounts", []):
        src = _display_path(m.get("Source", ""))
        if m.get("Destination") == "/config-root":
            a["config"] = src
        elif m.get("Destination") == "/data/temp":
            a["temp"] = src
        elif m.get("Destination") == "/data/completed":
            a["completed"] = src
    a["scanarr_port"] = int(env.get("SCANARR_PUBLIC_PORT") or stack.SCANARR_PORT)
    a["torrent_port"] = int(env.get("SCANARR_TORRENT_PORT") or 0)
    a["upload_kbps"] = int(env.get("SCANARR_UPLOAD_KBPS") or 50)
    a["puid"], a["pgid"] = int(env.get("PUID") or 1000), int(env.get("PGID") or 1000)
    a["jellyfin_port"] = int(env.get("SCANARR_JELLYFIN_PORT") or 8096)
    return a


def _display_path(p):
    for pre in ("/run/desktop/mnt/host/", "/host_mnt/"):
        if p.startswith(pre):
            rest = p[len(pre):]
            drive, _, tail = rest.partition("/")
            return f"{drive.upper()}:\\" + tail.replace("/", "\\")
    return p


# ---------------------------------------------------------------- validation
def suggestions(kind):
    if kind in ("windows", "desktop"):
        return {"temp": "C:\\Scanarr\\temp", "completed": "C:\\Scanarr\\completed", "config": "C:\\Scanarr\\config"}
    if kind == "nas":
        ssd = "/volume2" if host_exists("/volume2") else "/volume1"
        return {"temp": f"{ssd}/scanarr/temp", "completed": "/volume1/media", "config": f"{ssd}/docker/scanarr"}
    return {"temp": "/srv/scanarr/temp", "completed": "/srv/scanarr/completed", "config": "/srv/scanarr/config"}


def path_format_problem(p, kind):
    if not p or not p.strip():
        return "Enter a folder."
    p = p.strip()
    if kind in ("windows", "desktop"):
        if not WIN.match(p):
            return "Use a full Windows path, e.g. C:\\Scanarr\\temp"
    elif not p.startswith("/"):
        return "Use a full path starting with /, e.g. /volume1/media"
    if re.search(r'[*?"<>|]', p) or "\n" in p:
        return "The path contains characters that aren't allowed."
    if p.rstrip("/\\") in ("", "/", "C:", "c:") or re.fullmatch(r"[A-Za-z]:[\\/]?", p):
        return "Pick a sub-folder, not the whole drive."
    if re.fullmatch(r"/volume\d+/?", p):
        return "Pick a folder inside the volume, e.g. /volume1/media - not the volume itself."
    if re.match(r"^/(etc|bin|sbin|usr|proc|sys|dev|boot|lib|var/lib/docker)(/|$)", p):
        return "That's a system folder - pick another place."
    return None


GB = 1024 ** 3


def check_folder(path, role, kind, other=None):
    """role: temp | completed | config. Returns {status: ok|warn|error, messages: [...], info: {...}}"""
    msgs, status = [], "ok"
    prob = path_format_problem(path, kind)
    if prob:
        return {"status": "error", "messages": [prob], "info": {}}
    if not has_docker():
        return {"status": "warn", "messages": ["Can't check this folder without Docker access - it will be checked when the stack starts."], "info": {}}
    # one throwaway container: create the folder (via the nearest existing parent) and test it
    ep = engine_path(path).rstrip("/")
    parent, rel = os.path.split(ep)
    # never create a top-level folder or drive: a mistyped /volume3 must not end up on the system disk
    pre = (platform().get("desktop_prefix") or "") if ep.startswith(("/run/desktop", "/host_mnt")) else ""
    parts = ep[len(pre):].strip("/").split("/")
    floor = pre + "/" + parts[0] if parts and parts[0] else "/"
    if parent == "" or len(parent) < len(floor):
        return {"status": "error", "messages": ["Pick a folder inside a drive or volume (for example /volume1/scanarr), not the top level."], "info": {}}
    for _ in range(6):
        code, out, err = docker("run", "--rm", "--network", "none", "--user", "0:0",
                                "--mount", f"type=bind,source={parent or '/'},target=/p", "--entrypoint", "sh", self_image(),
                                "-c", 'mkdir -p "/p/$1" && exec python -m app check "/p/$1"', "sh", rel, timeout=90)
        missing = code != 0 and not out and ("does not exist" in err or "no such file" in err.lower())
        if not missing or len(os.path.dirname(parent)) < len(floor):
            break
        parent, up = os.path.split(parent)
        rel = up + "/" + rel
    if code != 0 and not out:
        if missing:
            return {"status": "error", "messages": ["That location doesn't exist on this machine - check the drive/volume name."], "info": {}}
        return {"status": "error", "messages": [f"Can't create or open this folder: {err[-200:]}"], "info": {}}
    try:
        info = json.loads(out.splitlines()[-1])
    except (ValueError, IndexError):
        return {"status": "error", "messages": [f"Couldn't check this folder: {(err or out)[-200:]}"], "info": {}}
    if not info.get("writable"):
        return {"status": "error", "messages": [f"Not writable: {info.get('error')}"], "info": info}
    free = info.get("free") or 0
    need = {"temp": 20 * GB, "completed": 50 * GB, "config": 1 * GB}[role]
    if free < need:
        status = "warn"
        msgs.append(f"Only {free / GB:.0f} GB free - at least {need // GB} GB recommended.")
    else:
        msgs.append(f"{free / GB:,.0f} GB free of {info['total'] / GB:,.0f} GB.")
    if role == "completed" and info.get("entries"):
        msgs.append(f"Already has {info['entries']} items - fine, Scanarr only adds Movies/, TV/ ... folders.")
    if other:
        for orole, oinfo in other.items():
            if oinfo and oinfo.get("dev") == info.get("dev") and kind == "nas":
                if {role, orole} == {"temp", "completed"}:
                    msgs.append("Temp and Completed are on the same disk. On the NAS, Temp on the SSD keeps the HDD asleep while downloading.")
                    status = "warn" if status == "ok" else status
                if {role, orole} == {"config", "completed"}:
                    msgs.append("Config is on the same disk as Completed. Put it on the SSD so the apps never wake the HDD.")
                    status = "warn" if status == "ok" else status
    return {"status": status, "messages": msgs, "info": info}


def check_port(port, purpose, taken_by_us=()):
    try:
        port = int(port)
    except (TypeError, ValueError):
        return {"status": "error", "messages": ["Enter a number."]}
    if not 1024 <= port <= 65535:
        return {"status": "error", "messages": ["Use a port between 1024 and 65535."]}
    if port in (stack.WIZARD_PORT,) and purpose != "wizard":
        return {"status": "error", "messages": ["9700 is used by this setup wizard."]}
    if not has_docker():
        return {"status": "warn", "messages": ["Can't test without Docker access - will fail clearly at start if taken."]}
    if port in taken_by_us:
        return {"status": "ok", "messages": ["Used by your current Scanarr install - fine."]}
    proto = ["-p", f"{port}:{port}"] + (["-p", f"{port}:{port}/udp"] if purpose == "torrent" else [])
    code, _, err = docker("run", "--rm", *proto, "--entrypoint", "true", self_image(), timeout=60)
    if code != 0 and ("allocated" in err or "in use" in err or "bind" in err):
        return {"status": "error", "messages": [f"Port {port} is already in use on this machine."]}
    if code != 0:
        return {"status": "warn", "messages": [f"Couldn't test the port: {err[-150:]}"]}
    return {"status": "ok", "messages": [f"Port {port} is free."]}


def free_subnet():
    code, out, _ = docker("network", "ls", "-q", timeout=20)
    used = set()
    if code == 0 and out:
        code, out2, _ = docker("network", "inspect", *out.split(), "--format",
                               "{{.Name}}|{{range .IPAM.Config}}{{.Subnet}} {{end}}", timeout=30)
        for line in out2.splitlines():
            name, _, subs = line.partition("|")
            if name == f"{PROJECT}_scanarr":
                continue
            used |= set(subs.split())
    for cand in ("172.31.97.0/24", "172.31.98.0/24", "10.197.97.0/24", "10.198.97.0/24", "192.168.197.0/24"):
        if cand not in used:
            return cand
    return "172.31.99.0/24"


def validate_all(a, kind, live=True):
    errs = []
    errs += stack.password_problems(a.get("username"), a.get("password"))
    for role in ("temp", "completed", "config"):
        p = path_format_problem(a.get(role), kind)
        if p:
            errs.append(f"{role.title()} folder: {p}")
    paths = [os.path.normcase((a.get(r) or "").rstrip("/\\")) for r in ("temp", "completed", "config")]
    if len(set(paths)) < 3:
        errs.append("Temp, Completed and Config must be three different folders.")
    t, c = paths[0], paths[1]
    if t and c and (c.startswith(t + os.sep) or c.startswith(t + "/") or c.startswith(t + "\\")):
        errs.append("Completed can't be inside Temp.")
    ports = [int(a.get("scanarr_port") or 0), int(a.get("torrent_port") or 0)] + ([int(a.get("jellyfin_port") or 0)] if a.get("jellyfin") else [])
    if len(set(ports)) < len(ports):
        errs.append("Each port must be different.")
    for p in ports:
        if not 1024 <= p <= 65535:
            errs.append(f"Port {p} must be between 1024 and 65535.")
    if not a.get("indexers"):
        errs.append("Pick at least one indexer.")
    if a.get("tailscale") and not str(a.get("tailscale_authkey", "")).startswith("tskey-"):
        errs.append("Tailscale needs an auth key starting with tskey- (or untick Tailscale).")
    try:
        if not 1 <= int(a.get("upload_kbps") or 0) <= 100000:
            errs.append("Upload limit must be between 1 and 100000 KB/s.")
    except ValueError:
        errs.append("Upload limit must be a number.")
    return errs


# ---------------------------------------------------------------- API
@app.get("/api/ping")
def ping():
    return {"ok": True}


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC, "setup.html"))


@app.get("/api/env")
def env():
    d = has_docker()
    plat = platform() if d else {"kind": "unknown", "os": "unknown", "arch": ""}
    ex = existing_stack() if d else None
    return {"docker": d, "platform": plat, "existing": ex, "prefill": answers_from_install() if ex else None,
            "suggest": suggestions(plat["kind"]) if d else suggestions("linux"), "image": self_image() if d else stack.IMAGE,
            "indexers": INDEXERS, "languages": LANGS, "defaults": {**stack.DEFAULTS, "password": ""},
            "subnet": free_subnet() if d else stack.SUBNET}


@app.post("/api/check/folder")
def api_check_folder(p: dict = Body(...)):
    return check_folder(p.get("path"), p.get("role", "temp"), p.get("kind", "linux"), p.get("other"))


@app.post("/api/check/port")
def api_check_port(p: dict = Body(...)):
    ours = set()
    pre = answers_from_install() if has_docker() else None
    if pre:
        ours = {pre.get("scanarr_port"), pre.get("torrent_port"), pre.get("jellyfin_port")}
    return check_port(p.get("port"), p.get("purpose", "web"), ours)


@app.post("/api/check/login")
def api_check_login(p: dict = Body(...)):
    probs = stack.password_problems(p.get("username"), p.get("password"))
    pw = p.get("password") or ""
    score = min(4, (len(pw) >= 10) + (len(pw) >= 14) + bool(re.search(r"\d", pw)) + bool(re.search(r"[^A-Za-z0-9]", pw)) + (len(set(pw)) > 8))
    return {"status": "error" if probs else ("ok" if score >= 3 else "warn"), "messages": probs or
            (["Strong password."] if score >= 3 else ["OK, but longer with digits and symbols is safer."]), "score": score}


@app.get("/api/suggest")
def api_suggest():
    return {"password": stack.strong_password(), "topic": "scanarr-" + stack.random_key(12, "abcdefghijkmnpqrstuvwxyz23456789"),
            "torrent_port": 50000 + secrets.randbelow(10000)}


@app.post("/api/check/tmdb")
def api_check_tmdb(p: dict = Body(...)):
    key = (p.get("key") or "").strip()
    if not key:
        return {"status": "ok", "messages": ["Skipped - you can add it later in Scanarr's Settings."]}
    h, params = ({"Authorization": f"Bearer {key}"}, {}) if len(key) > 40 else ({}, {"api_key": key})
    try:
        r = httpx.get("https://api.themoviedb.org/3/configuration", headers=h, params=params, timeout=15)
        return {"status": "ok", "messages": ["Key works."]} if r.status_code == 200 else \
            {"status": "error", "messages": [f"TMDb rejected the key (HTTP {r.status_code})."]}
    except httpx.HTTPError as e:
        return {"status": "warn", "messages": [f"Couldn't reach TMDb ({e.__class__.__name__}) - check internet; you can continue."]}


@app.post("/api/check/opensubtitles")
def api_check_osub(p: dict = Body(...)):
    from .subtitles import OpenSubtitles
    key = (p.get("api_key") or "").strip()
    if not key:
        return {"status": "ok", "messages": ["Skipped - subtitles can be set up later in Scanarr."]}
    o = OpenSubtitles(key, p.get("username", ""), p.get("password", ""))
    try:
        if not p.get("username"):
            r = o.c.get("https://api.opensubtitles.com/api/v1/infos/formats")
            return {"status": "ok" if r.status_code == 200 else "error",
                    "messages": ["API key works. Add your username/password for more downloads per day."] if r.status_code == 200
                    else [f"OpenSubtitles rejected the API key (HTTP {r.status_code})."]}
        q = o.quota()
        return {"status": "ok", "messages": [f"Logged in - {q} downloads left today."]}
    except Exception as e:
        return {"status": "error", "messages": [f"OpenSubtitles: {str(e)[:150]}"]}
    finally:
        o.close()


@app.post("/api/check/ntfy")
def api_check_ntfy(p: dict = Body(...)):
    url = (p.get("url") or "").strip()
    if not url:
        return {"status": "ok", "messages": ["Skipped."]}
    if not re.match(r"^https?://[^/\s]+/[A-Za-z0-9_-]{6,}$", url):
        return {"status": "error", "messages": ["Use the topic URL, e.g. https://ntfy.sh/scanarr-k3j9x2q7"]}
    try:
        r = httpx.post(url, content="Scanarr setup: notifications work.".encode(), headers={"Title": "Scanarr"}, timeout=10)
        return {"status": "ok" if r.status_code < 400 else "error",
                "messages": ["Test sent - check your phone."] if r.status_code < 400 else [f"ntfy said HTTP {r.status_code}."]}
    except httpx.HTTPError as e:
        return {"status": "warn", "messages": [f"Couldn't reach ntfy ({e.__class__.__name__})."]}


@app.post("/api/compose", response_class=PlainTextResponse)
def api_compose(a: dict = Body(...)):
    kind = a.pop("_kind", "linux")
    errs = validate_all(a, kind)
    if errs:
        raise HTTPException(400, " ".join(errs))
    return stack.compose(_engine_answers(a, kind))


def _engine_answers(a, kind):
    b = {**stack.DEFAULTS, **a}
    if kind in ("windows", "desktop"):
        for r in ("temp", "completed", "config"):
            b[r] = engine_path(b[r])
    from .version import __version__
    b["version"] = os.environ.get("SCANARR_VERSION_TAG") or __version__   # pinned: the stack matches this wizard
    return b


# ---------------------------------------------------------------- install job
JOB = {"running": False, "steps": [], "done": False, "ok": None, "result": None}
_lock = threading.Lock()


def _step(name, status="run", detail=""):
    with _lock:
        for s in JOB["steps"]:
            if s["name"] == name:
                s.update(status=status, detail=detail)
                return
        JOB["steps"].append({"name": name, "status": status, "detail": detail})


def _install(a, kind, mode):
    try:
        b = _engine_answers(a, kind)
        text = stack.compose(b)
        _step("Prepare folders")
        for r in ("config", "temp", "completed"):
            res = check_folder(a[r], r, kind)          # same safety rules as the form (never creates a top-level folder)
            if res["status"] == "error":
                raise RuntimeError(f"{r} folder: {'; '.join(res['messages'])}")
        code, _, err = helper(b["config"], "sh", "-c", "mkdir -p /t/stack && cat > /t/stack/compose.yml && chmod 600 /t/stack/compose.yml",
                              stdin=text)
        if code != 0:
            raise RuntimeError(f"couldn't save the compose file: {err[-200:]}")
        _step("Prepare folders", "ok", "folders ready, compose file saved to Config/stack/compose.yml")
        _step("Download apps (images)")
        offline = []
        for img in sorted(set(re.findall(r"^\s+image: (\S+)", text, re.M))):
            _step("Download apps (images)", "run", img)
            code, _, err = docker("pull", img, timeout=900)
            if code != 0:
                if docker("image", "inspect", img, timeout=30)[0] == 0:
                    offline.append(img)            # no internet / registry down: the copy already here will do
                else:
                    raise RuntimeError(f"couldn't download {img}: {err[-200:]}")
        _step("Download apps (images)", "warn" if offline else "ok",
              f"couldn't refresh {', '.join(offline)} - using the copy already on this machine" if offline else "all images downloaded")
        _step("Start containers")
        path = "/tmp/scanarr-compose.yml"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # contains the login
        with os.fdopen(fd, "w") as f:
            f.write(text)
        code, out, err = docker("compose", "-p", PROJECT, "-f", path, "up", "-d", "--remove-orphans", timeout=900)
        if code != 0:
            raise RuntimeError(f"docker compose failed: {(err or out)[-400:]}")
        _step("Start containers", "ok", "qBittorrent, Prowlarr and Scanarr started")
        _step("Connect apps and test")
        res = _verify(a, b)
        _step("Connect apps and test", "ok" if res["ok"] else "warn", res["summary"])
        JOB.update(ok=res["ok"], result=res)
    except Exception as e:
        for s in JOB["steps"]:
            if s["status"] == "run":
                s.update(status="error", detail=str(e)[:500])
        JOB.update(ok=False, result={"error": str(e)[:500]})
    finally:
        JOB.update(running=False, done=True)


def _verify(a, b):
    """Join the stack network, log into Scanarr and wait for its first-start wiring + a test search."""
    me = os.environ.get("HOSTNAME", "")
    joined = docker("network", "connect", f"{PROJECT}_scanarr", me, timeout=30)[0] == 0
    port = int(a.get("scanarr_port") or stack.SCANARR_PORT)
    # inside the stack network by name; otherwise (e.g. wizard on host networking) via the published port
    bases = (["http://scanarr:9797"] if joined else []) + [f"http://127.0.0.1:{port}", f"http://host.docker.internal:{port}"]
    deadline = time.time() + 300
    c = httpx.Client(timeout=15)
    checks, base = {}, None
    while time.time() < deadline and not base:
        for b_ in bases:
            try:
                if c.post(f"{b_}/api/login", json={"username": a["username"], "password": a["password"]}, timeout=5).status_code == 200:
                    base = b_
                    break
            except httpx.HTTPError:
                pass
        if not base:
            time.sleep(3)
    if not base:
        return {"ok": False, "summary": "Scanarr didn't start within 5 minutes - see its logs.", "checks": checks}
    checks["scanarr"] = True
    rep = {}
    while time.time() < deadline:
        try:
            rep = c.get(f"{base}/api/setup-status").json()
            if rep.get("done"):
                break
        except (httpx.HTTPError, ValueError):
            pass
        c.post(f"{base}/api/process")
        time.sleep(4)
    checks["wiring"] = bool(rep.get("done"))
    hits = None
    try:
        r = c.get(f"{base}/api/search", params={"q": "2024", "cat": "all"}, timeout=90)
        hits = len(r.json().get("results", [])) if r.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        pass
    checks["search"] = hits
    if joined:
        docker("network", "disconnect", f"{PROJECT}_scanarr", me, timeout=30)
    ok = checks["wiring"]
    summ = ("Everything is connected." if ok else "Apps started, but the first-start wiring hasn't finished - Scanarr keeps retrying.")
    summ += f" Test search: {hits} results." if hits else " Test search returned no results (indexers may be blocked or still loading)."
    return {"ok": ok, "summary": summ, "checks": checks, "report": rep.get("report", {})}


@app.post("/api/install")
def api_install(a: dict = Body(...)):
    if JOB["running"]:
        raise HTTPException(409, "An install is already running.")
    kind = a.pop("_kind", "linux")
    mode = a.pop("_mode", "install")
    errs = validate_all(a, kind)
    if errs:
        raise HTTPException(400, " ".join(errs))
    if not has_docker():
        raise HTTPException(400, "No Docker access - use 'Copy compose file' instead.")
    JOB.update(running=True, steps=[], done=False, ok=None, result=None)
    threading.Thread(target=_install, args=(a, kind, mode), daemon=True).start()
    return {"ok": True}


@app.get("/api/job")
def api_job():
    return JOB


@app.post("/api/uninstall")
def api_uninstall(p: dict = Body(...)):
    if p.get("confirm") != "UNINSTALL":
        raise HTTPException(400, "Type UNINSTALL to confirm.")
    pre = answers_from_install()
    code, out, err = docker("compose", "-p", PROJECT, "down", "--remove-orphans", timeout=300)
    if code != 0:
        raise HTTPException(500, (err or out)[-300:])
    removed = ["containers and network"]
    if p.get("delete_config") and pre and pre.get("config"):
        code, _, err = helper(pre["config"], "sh", "-c", "rm -rf /t/qbittorrent /t/prowlarr /t/scanarr /t/stack /t/jellyfin /t/tailscale")
        removed.append("app settings (Config folder)" if code == 0 else f"settings NOT removed: {err[-150:]}")
    if p.get("delete_temp") and pre and pre.get("temp"):
        helper(pre["temp"], "sh", "-c", "rm -rf /t/* /t/.[!.]* 2>/dev/null; true")
        removed.append("unfinished downloads (Temp)")
    return {"ok": True, "removed": removed, "kept": "Your Completed folder (movies, TV, everything downloaded) is never touched."}


@app.post("/api/finish")
def api_finish():
    """Remove this setup wizard container (the stack keeps running)."""
    def later():
        time.sleep(2)
        docker("rm", "-f", os.environ.get("HOSTNAME", ""), timeout=60)
    threading.Thread(target=later, daemon=True).start()
    return {"ok": True}
