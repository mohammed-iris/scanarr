"""Updater: the only always-on part with Docker access, and it's optional.

- Reachable only inside the stack's private network (no ports published) and only with the token
  the setup wizard generated (Scanarr has the same token).
- Does one thing: move the stack to a newer Scanarr release, using the qBittorrent/Prowlarr versions
  that release was tested with. Saves the old compose file first and rolls back automatically if
  Scanarr doesn't come up healthy.
- Never touches your folders.
"""
import hmac
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time

import httpx
from fastapi import Body, FastAPI, Header, HTTPException

from . import stack
from .version import __version__

log = logging.getLogger("scanarr.agent")
app = FastAPI(title="Scanarr updater", docs_url=None, redoc_url=None, openapi_url=None)

COMPOSE = os.environ.get("COMPOSE_FILE", "/stack/compose.yml")
PROJECT = "scanarr"
TOKEN = os.environ.get("SCANARR_AGENT_TOKEN", "")
SCANARR_URL = os.environ.get("SCANARR_URL", "http://scanarr:9797")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
JOB = {"running": False, "steps": [], "ok": None, "from": None, "to": None, "ts": None}
_lock = threading.Lock()


def _auth(token):
    if not TOKEN or not token or not hmac.compare_digest(token, TOKEN):
        raise HTTPException(401, "bad token")


def docker(*args, timeout=600):
    try:
        p = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", "timed out"


def compose(*args, timeout=900):
    return docker("compose", "-p", PROJECT, "-f", COMPOSE, *args, timeout=timeout)


def current_versions(text=None):
    text = text if text is not None else open(COMPOSE, encoding="utf-8").read()
    imgs = re.findall(r"^\s+image: (\S+)", text, re.M)
    mine = next((i for i in imgs if i.startswith(stack.IMAGE + ":")), "")
    return {"scanarr": mine.rsplit(":", 1)[-1] if mine else None,
            "qbittorrent": next((i for i in imgs if "qbittorrent" in i), None),
            "prowlarr": next((i for i in imgs if "prowlarr" in i), None)}


def rewrite(text, new_tag, pins):
    """New compose text: Scanarr image -> new tag, qBit/Prowlarr -> the versions that release was tested with."""
    text = re.sub(rf"(^\s+image: ){re.escape(stack.IMAGE)}:\S+", rf"\g<1>{stack.IMAGE}:{new_tag}", text, flags=re.M)
    for key in ("qbittorrent", "prowlarr"):
        img = pins.get(key)
        if img and re.fullmatch(r"[\w./:-]+", img):
            text = re.sub(rf"(^\s+image: )\S*{key}\S*", rf"\g<1>{img}", text, flags=re.M)
    return text


def pull(img):
    code, _, err = docker("pull", img)
    if code and docker("image", "inspect", img, timeout=30)[0] != 0:
        raise RuntimeError(f"couldn't download {img}: {err[-200:]}")


def _step(name, status="run", detail=""):
    for s in JOB["steps"]:
        if s["name"] == name:
            s.update(status=status, detail=detail)
            return
    JOB["steps"].append({"name": name, "status": status, "detail": detail})


def healthy(timeout=240):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if httpx.get(f"{SCANARR_URL}/login", timeout=5).status_code == 200:
                code, out, _ = docker("inspect", "scanarr", "--format", "{{.State.Status}}", timeout=20)
                if code == 0 and out == "running":
                    return True
        except httpx.HTTPError:
            pass
        time.sleep(4)
    return False


def run_update(tag):
    old_text = open(COMPOSE, encoding="utf-8").read()
    old = current_versions(old_text)
    JOB.update(running=True, steps=[], ok=None, ts=time.time(), **{"from": old["scanarr"], "to": tag})
    backup = f"{COMPOSE}.before-{old['scanarr'] or 'unknown'}"
    try:
        img = f"{stack.IMAGE}:{tag}"
        _step("Download Scanarr " + tag)
        pull(img)
        _step("Download Scanarr " + tag, "ok")

        _step("Check tested versions")
        code, out, err = docker("run", "--rm", "--network", "none", img, "versions", timeout=120)
        try:
            pins = json.loads(out.splitlines()[-1])
        except (ValueError, IndexError):
            raise RuntimeError(f"the new version didn't start ({err[-200:]})") from None
        _step("Check tested versions", "ok", f"qBittorrent {pins.get('qbittorrent')}, Prowlarr {pins.get('prowlarr')}")

        for key in ("qbittorrent", "prowlarr"):
            if pins.get(key) and pins[key] != old.get(key):
                _step(f"Download {key}")
                pull(pins[key])
                _step(f"Download {key}", "ok", pins[key])

        _step("Switch over")
        shutil.copy2(COMPOSE, backup)
        new_text = rewrite(old_text, tag, pins)
        with open(COMPOSE, "w", encoding="utf-8") as f:
            f.write(new_text)
        os.chmod(COMPOSE, 0o600)
        code, out, err = compose("up", "-d", "qbittorrent", "prowlarr", "scanarr")   # the updater itself stays as is
        if code or not healthy():
            raise RuntimeError("the new version didn't come up healthy" + (f": {(err or out)[-300:]}" if code else ""))
        _step("Switch over", "ok", f"now on {tag}")
        JOB.update(ok=True)
    except Exception as e:
        for s in JOB["steps"]:
            if s["status"] == "run":
                s.update(status="error", detail=str(e)[:400])
        if os.path.exists(backup) and open(COMPOSE, encoding="utf-8").read() != old_text:
            _step("Roll back", "run", f"returning to {old['scanarr']}")
            shutil.copy2(backup, COMPOSE)
            code, out, err = compose("up", "-d", "qbittorrent", "prowlarr", "scanarr")
            _step("Roll back", "ok" if code == 0 and healthy(180) else "error",
                  f"back on {old['scanarr']}" if code == 0 else (err or out)[-300:])
        JOB.update(ok=False)
        log.warning("update to %s failed: %s", tag, e)
    finally:
        JOB.update(running=False)


@app.get("/ping")
def ping():
    return {"ok": True}


@app.get("/status")
def status(x_token: str = Header(None)):
    _auth(x_token)
    try:
        cur = current_versions()
    except OSError as e:
        raise HTTPException(500, f"compose file not readable: {e}")
    return {"agent": __version__, "current": cur, "job": JOB}


@app.post("/update")
def update(body: dict = Body(...), x_token: str = Header(None)):
    _auth(x_token)
    tag = str(body.get("version") or "").lstrip("v")
    if not VERSION_RE.match(tag):
        raise HTTPException(400, "version must look like 1.2.3")
    with _lock:
        if JOB["running"]:
            raise HTTPException(409, "an update is already running")
        JOB["running"] = True
    threading.Thread(target=run_update, args=(tag,), daemon=True).start()
    return {"ok": True}
