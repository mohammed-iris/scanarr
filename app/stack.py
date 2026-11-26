"""The Scanarr stack: Scanarr + qBittorrent + Prowlarr (+ optional Jellyfin / Tailscale).

Used by:
  - the setup wizard, to turn your answers into ONE compose file;
  - the `init` container, which writes qBittorrent's and Prowlarr's settings before they first start
    (shared login, folders, API keys), so nothing needs to be set by hand.

Design: qBittorrent and Prowlarr live on a private Docker network and trust it. Their web pages are
not published to your LAN; Scanarr is the only way in (one login).
"""
import base64
import hashlib
import json
import os

from .version import __version__
import re
import secrets
import string
from xml.sax.saxutils import escape

IMAGE = os.environ.get("SCANARR_IMAGE", "ghcr.io/mohammed-iris/scanarr")
# Versions tested together. A Scanarr release updates these together.
QBIT_IMAGE = "lscr.io/linuxserver/qbittorrent:5.3.1"
PROWLARR_IMAGE = "lscr.io/linuxserver/prowlarr:3.1.0"
JELLYFIN_IMAGE = "lscr.io/linuxserver/jellyfin:latest"
TAILSCALE_IMAGE = "tailscale/tailscale:stable"

SUBNET = "172.31.97.0/24"  # private network the apps trust; the wizard picks another if this one is taken
QBIT_WEBUI_PORT = 8080      # inside the network only
PROWLARR_PORT = 9696        # inside the network only
SCANARR_PORT = 9797
WIZARD_PORT = 9700

DEFAULTS = {
    "username": "admin",
    "password": "",
    "temp": "",
    "completed": "",
    "config": "",
    "puid": 1000,
    "pgid": 1000,
    "tz": "UTC",
    "scanarr_port": SCANARR_PORT,
    "torrent_port": 0,          # 0 = pick a random high port
    "upload_kbps": 50,          # while downloading; seeding stops when done
    "indexers": ["yts", "thepiratebay", "Knaben"],
    "languages": ["en"],
    "tmdb_api_key": "",
    "opensubtitles": {"api_key": "", "username": "", "password": ""},
    "ntfy_url": "",
    "jellyfin": False,
    "jellyfin_port": 8096,
    "tailscale": False,
    "tailscale_authkey": "",
    "subnet": SUBNET,
    "updater": True,                 # allow "Update" from inside Scanarr (small helper with Docker access)
    "version": __version__,          # the stack uses the same version as the wizard that made it
}


# ---------------------------------------------------------------- secrets
def random_key(n=32, alphabet=string.ascii_letters + string.digits):
    return "".join(secrets.choice(alphabet) for _ in range(n))


def strong_password():
    """Readable but strong: 4 groups of 4, e.g. 'k3Qx-9vRt-Lm2p-Zw7a' (~95 bits)."""
    alpha = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "-".join(random_key(4, alpha) for _ in range(4))


def qbit_password_hash(password, salt=None):
    """qBittorrent's WebUI\\Password_PBKDF2 format (PBKDF2-HMAC-SHA512, 100k rounds)."""
    salt = salt or os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha512", password.encode(), salt, 100000, 64)
    return f"@ByteArray({base64.b64encode(salt).decode()}:{base64.b64encode(dk).decode()})"


def password_problems(username, password):
    out = []
    if not re.fullmatch(r"[A-Za-z0-9._-]{3,32}", username or ""):
        out.append("Username: 3-32 letters, digits, dot, dash or underscore.")
    if len(password or "") < 10:
        out.append("Password: at least 10 characters.")
    if password and username and username.lower() in password.lower():
        out.append("Password must not contain the username.")
    if password and len(set(password)) < 5:
        out.append("Password is too repetitive.")
    return out


# ---------------------------------------------------------------- config seeding (init container)
def qbit_conf(a, api_key):
    up = max(int(a.get("upload_kbps") or 0), 1)  # qBit: 0 would mean unlimited
    return f"""[BitTorrent]
Session\\DefaultSavePath=/data/completed
Session\\TempPath=/data/temp
Session\\TempPathEnabled=true
Session\\Port={int(a['torrent_port'])}
Session\\GlobalUPSpeedLimit={up}
Session\\GlobalMaxRatio=0
Session\\QueueingSystemEnabled=true
Session\\DisableAutoTMMByDefault=true

[LegalNotice]
Accepted=true

[Network]
PortForwardingEnabled=false

[Preferences]
Connection\\ResolvePeerCountries=true

[WebUI]
Address=*
Port={QBIT_WEBUI_PORT}
Username={a['username']}
Password_PBKDF2="{qbit_password_hash(a['password'])}"
APIKey={api_key}
AuthSubnetWhitelistEnabled=true
AuthSubnetWhitelist={a['subnet']}
HostHeaderValidation=false
ReverseProxySupportEnabled=true
CSRFProtection=true
ClickjackingProtection=false
ServerDomains=*
"""


def prowlarr_xml(api_key):
    return f"""<Config>
  <BindAddress>*</BindAddress>
  <Port>{PROWLARR_PORT}</Port>
  <UrlBase>/prowlarr</UrlBase>
  <EnableSsl>False</EnableSsl>
  <ApiKey>{escape(api_key)}</ApiKey>
  <AuthenticationMethod>External</AuthenticationMethod>
  <AuthenticationRequired>Enabled</AuthenticationRequired>
  <Branch>master</Branch>
  <LogLevel>info</LogLevel>
  <UpdateMechanism>Docker</UpdateMechanism>
  <InstanceName>Prowlarr</InstanceName>
</Config>
"""


# settings the wizard sets on every Repair (only if you entered them there)
WIZARD_OWNED = ("subtitle_languages", "tmdb_api_key", "opensubtitles", "ntfy_url")


def scanarr_settings(a, qbit_key, prowlarr_key):
    return {
        "prowlarr": {"url": f"http://prowlarr:{PROWLARR_PORT}/prowlarr", "api_key": prowlarr_key},
        "qbit": {"url": f"http://qbittorrent:{QBIT_WEBUI_PORT}", "api_key": qbit_key, "username": "", "password": ""},
        "auth": {"username": a["username"], "password": a["password"], "secret": ""},
        "subtitle_languages": a.get("languages") or ["en"],
        "tmdb_api_key": a.get("tmdb_api_key", ""),
        "opensubtitles": a.get("opensubtitles") or {},
        "ntfy_url": a.get("ntfy_url", ""),
        "stack": {"managed": True, "upload_kbps": a.get("upload_kbps", 50),
                  "indexers": a.get("indexers") or [], "bootstrap_done": False,
                  "jellyfin": bool(a.get("jellyfin")), "jellyfin_port": a.get("jellyfin_port"),
                  "scanarr_port": a.get("scanarr_port"), "torrent_port": a.get("torrent_port"),
                  "wizard_values": {"subtitle_languages": a.get("languages") or ["en"], "tmdb_api_key": a.get("tmdb_api_key", ""),
                                    "opensubtitles": a.get("opensubtitles") or {}, "ntfy_url": a.get("ntfy_url", "")}},
    }


def init_from_env(root="/config-root", env=None, data="/data"):
    """Runs once in the `init` container before qBittorrent and Prowlarr start.
    Writes their settings only if they don't exist yet (never overwrites your changes)."""
    env = env if env is not None else os.environ
    a = answers_from_env(env)
    report = []
    qconf = os.path.join(root, "qbittorrent", "qBittorrent", "qBittorrent.conf")
    pconf = os.path.join(root, "prowlarr", "config.xml")
    sconf = os.path.join(root, "scanarr", "settings.json")
    qkey = env.get("SCANARR_QBIT_KEY") or ("qbt_" + random_key(28))
    pkey = env.get("SCANARR_PROWLARR_KEY") or random_key(32, "0123456789abcdef")
    for d in (os.path.dirname(qconf), os.path.dirname(pconf), os.path.dirname(sconf), os.path.join(data, "temp"), os.path.join(data, "completed")):
        os.makedirs(d, exist_ok=True)
    if not os.path.exists(qconf):
        _write(qconf, qbit_conf(a, qkey))
        report.append("qBittorrent settings written")
    else:
        qkey = _read_qbit_key(qconf) or qkey
        report.append("qBittorrent settings already exist - kept")
    if not os.path.exists(pconf):
        _write(pconf, prowlarr_xml(pkey))
        report.append("Prowlarr settings written")
    else:
        m = re.search(r"<ApiKey>([^<]+)</ApiKey>", open(pconf, encoding="utf-8").read())
        pkey = m.group(1) if m else pkey
        report.append("Prowlarr settings already exist - kept")
    fresh = scanarr_settings(a, qkey, pkey)
    if not os.path.exists(sconf):
        _write(sconf, json.dumps(fresh, indent=2))
        report.append("Scanarr settings written")
    else:
        # Repair / changed answers in the wizard: apply what the wizard owns, keep everything else you set
        try:
            cur = json.load(open(sconf, encoding="utf-8"))
        except (OSError, ValueError):
            cur = {}
            os.replace(sconf, sconf + ".broken")
            report.append("Scanarr settings were unreadable - saved as settings.json.broken and rewritten")
        before = (cur.get("stack") or {}).get("wizard_values") or {}
        for k in WIZARD_OWNED:   # only what you changed in the wizard; changes made in Scanarr stay
            if k in fresh and fresh[k] != before.get(k, cur.get(k)):
                cur[k] = fresh[k]
        for k in ("prowlarr", "qbit"):   # keep the connection right (keys may have been regenerated)
            cur[k] = fresh[k]
        cur["stack"] = {**fresh["stack"], "rev": random_key(8)}
        cur["stack"]["wizard_values"] = {k: fresh[k] for k in WIZARD_OWNED if k in fresh}   # new rev: Scanarr re-applies qBit/Prowlarr setup
        _write(sconf, json.dumps(cur, indent=2))
        report.append("Scanarr settings updated from the wizard (your other settings kept)")
    # make sure every app can write its folders
    uid, gid = int(a["puid"]), int(a["pgid"])
    for d in (os.path.join(root, "qbittorrent"), os.path.join(root, "prowlarr"), os.path.join(root, "scanarr")):
        _chown_tree(d, uid, gid)
    for d in (os.path.join(data, "temp"), os.path.join(data, "completed")):
        if not _writable_as(d, uid, gid):
            # only change the owner when the apps couldn't write otherwise (never touches existing ACLs needlessly)
            try:
                os.chown(d, uid, gid)
                report.append(f"{d}: owner set to {uid}:{gid} so the apps can write")
            except OSError:
                pass
        if not _writable_as(d, uid, gid):
            raise SystemExit(f"ERROR: {d} is not writable for user {uid}:{gid}. Fix the folder permissions "
                             f"or run the setup wizard again and change the file owner.")
        report.append(f"{d} is writable for {uid}:{gid}")
    return report


def _writable_as(d, uid, gid):
    """Can user uid:gid create a file in d? Tested for real in a child process running as that user."""
    if os.name != "posix":
        return os.access(d, os.W_OK)
    pid = os.fork()
    if pid == 0:
        code = 1
        try:
            if os.getuid() == 0:
                os.setgroups([])
                os.setgid(gid)
                os.setuid(uid)
            probe = os.path.join(d, f".scanarr-write-test-{os.getpid()}")
            with open(probe, "w") as f:
                f.write("ok")
            os.remove(probe)
            code = 0
        except BaseException:
            pass
        os._exit(code)
    _, status = os.waitpid(pid, 0)
    return os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0


def _read_qbit_key(path):
    m = re.search(r"^APIKey=(\S+)", open(path, encoding="utf-8").read(), re.M)
    return m.group(1) if m else None


def _write(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def _chown_tree(d, uid, gid):
    for dirpath, dirs, files in os.walk(d):
        for p in [dirpath] + [os.path.join(dirpath, x) for x in files]:
            try:
                os.chown(p, uid, gid)
            except OSError:
                pass


# ---------------------------------------------------------------- answers <-> env / compose
ENV_KEYS = {  # answer key -> environment variable passed to the init container
    "username": "SCANARR_USERNAME", "password": "SCANARR_PASSWORD", "puid": "PUID", "pgid": "PGID",
    "tz": "TZ", "torrent_port": "SCANARR_TORRENT_PORT", "upload_kbps": "SCANARR_UPLOAD_KBPS",
    "subnet": "SCANARR_SUBNET", "tmdb_api_key": "SCANARR_TMDB_KEY", "ntfy_url": "SCANARR_NTFY_URL",
    "scanarr_port": "SCANARR_PUBLIC_PORT", "jellyfin_port": "SCANARR_JELLYFIN_PORT",
}


def answers_from_env(env):
    a = dict(DEFAULTS)
    for k, e in ENV_KEYS.items():
        if env.get(e) not in (None, ""):
            a[k] = env[e]
    for k in ("indexers", "languages"):
        v = env.get("SCANARR_" + k.upper())
        if v:
            a[k] = [x.strip() for x in v.split(",") if x.strip()]
    a["opensubtitles"] = {"api_key": env.get("SCANARR_OSUB_KEY", ""), "username": env.get("SCANARR_OSUB_USER", ""),
                          "password": env.get("SCANARR_OSUB_PASS", "")}
    a["jellyfin"] = env.get("SCANARR_JELLYFIN") == "1"
    a["updater"] = env.get("SCANARR_UPDATER", "1") == "1"
    a["tailscale"] = env.get("SCANARR_TAILSCALE") == "1"
    a["tailscale_authkey"] = env.get("SCANARR_TS_KEY", "")
    for k in ("puid", "pgid", "torrent_port", "scanarr_port", "jellyfin_port", "upload_kbps"):
        try:
            a[k] = int(a[k])
        except (TypeError, ValueError, KeyError):
            pass
    return a


def _q(v):
    """Quote a value for YAML safely."""
    return json.dumps(str(v))


def compose(a):
    """One self-contained compose file holding the whole setup."""
    a = {**DEFAULTS, **a}
    if not a.get("torrent_port"):
        a["torrent_port"] = 50000 + secrets.randbelow(10000)
    token = a.get("agent_token") or random_key(40)
    cfg, temp, done = a["config"].rstrip("/\\"), a["temp"].rstrip("/\\"), a["completed"].rstrip("/\\")
    j = lambda *p: "/".join([cfg.replace("\\", "/")] + list(p))
    img = f"{IMAGE}:{a.get('version') or 'latest'}"
    env_init = {
        "SCANARR_USERNAME": a["username"], "SCANARR_PASSWORD": a["password"],
        "PUID": a["puid"], "PGID": a["pgid"], "TZ": a["tz"],
        "SCANARR_TORRENT_PORT": a["torrent_port"], "SCANARR_UPLOAD_KBPS": a["upload_kbps"],
        "SCANARR_SUBNET": a["subnet"], "SCANARR_INDEXERS": ",".join(a["indexers"]),
        "SCANARR_LANGUAGES": ",".join(a["languages"]), "SCANARR_TMDB_KEY": a.get("tmdb_api_key", ""),
        "SCANARR_OSUB_KEY": a["opensubtitles"].get("api_key", ""), "SCANARR_OSUB_USER": a["opensubtitles"].get("username", ""),
        "SCANARR_OSUB_PASS": a["opensubtitles"].get("password", ""), "SCANARR_NTFY_URL": a.get("ntfy_url", ""),
        "SCANARR_PUBLIC_PORT": a["scanarr_port"], "SCANARR_JELLYFIN": "1" if a.get("jellyfin") else "0",
        "SCANARR_JELLYFIN_PORT": a.get("jellyfin_port", 8096),
        "SCANARR_UPDATER": "1" if a.get("updater") else "0",
        "SCANARR_TAILSCALE": "1" if a.get("tailscale") else "0", "SCANARR_TS_KEY": a.get("tailscale_authkey", ""),
    }
    data_vols = [f"      - {_q(temp + ':/data/temp')}", f"      - {_q(done + ':/data/completed')}"]
    lines = [
        "# Scanarr stack - generated by the Scanarr setup wizard.",
        "# Contains your login: keep this file private.",
        "# Open Scanarr at http://<this-machine>:" + str(a["scanarr_port"]),
        "name: scanarr",
        "",
        "networks:",
        "  scanarr:",
        "    ipam:",
        "      config:",
        f"        - subnet: {a['subnet']}",
        "",
        "services:",
        "  init:   # runs once: writes qBittorrent/Prowlarr/Scanarr settings if they don't exist yet",
        f"    image: {img}",
        "    command: [\"init\"]",
        "    user: \"0:0\"",
        "    environment:",
    ] + [f"      {k}: {_q(v)}" for k, v in env_init.items()] + [
        "    volumes:",
        f"      - {_q(cfg.replace(chr(92), '/') + ':/config-root')}",
    ] + data_vols + [
        "    restart: \"no\"",
        "",
        "  qbittorrent:",
        f"    image: {QBIT_IMAGE}",
        "    container_name: scanarr-qbittorrent",
        "    hostname: qbittorrent",
        "    depends_on: {init: {condition: service_completed_successfully}}",
        "    environment:",
        f"      PUID: {_q(a['puid'])}",
        f"      PGID: {_q(a['pgid'])}",
        f"      TZ: {_q(a['tz'])}",
        f"      WEBUI_PORT: \"{QBIT_WEBUI_PORT}\"",
        f"      TORRENTING_PORT: \"{a['torrent_port']}\"",
        "    ports:   # torrent traffic only - the web page is reached through Scanarr",
        f"      - \"{a['torrent_port']}:{a['torrent_port']}\"",
        f"      - \"{a['torrent_port']}:{a['torrent_port']}/udp\"",
        "    volumes:",
        f"      - {_q(j('qbittorrent') + ':/config')}",
    ] + data_vols + [
        "    networks: {scanarr: {aliases: [qbittorrent]}}",
        "    restart: unless-stopped",
        "",
        "  prowlarr:",
        f"    image: {PROWLARR_IMAGE}",
        "    container_name: scanarr-prowlarr",
        "    hostname: prowlarr",
        "    depends_on: {init: {condition: service_completed_successfully}}",
        "    environment:",
        f"      PUID: {_q(a['puid'])}",
        f"      PGID: {_q(a['pgid'])}",
        f"      TZ: {_q(a['tz'])}",
        "    volumes:",
        f"      - {_q(j('prowlarr') + ':/config')}",
        "    networks: {scanarr: {aliases: [prowlarr]}}",
        "    restart: unless-stopped",
        "",
        "  scanarr:",
        f"    image: {img}",
        "    container_name: scanarr",
        "    depends_on: {init: {condition: service_completed_successfully}}",
        "    user: " + _q(f"{a['puid']}:{a['pgid']}"),
        "    environment:",
        f"      TZ: {_q(a['tz'])}",
        "      CONFIG_DIR: \"/config\"",
        f"      SCANARR_USERNAME: {_q(a['username'])}   # change here + redeploy to reset the login (synced to qBittorrent)",
        f"      SCANARR_PASSWORD: {_q(a['password'])}",
    ] + ([f"      SCANARR_AGENT_TOKEN: {_q(token)}", "      SCANARR_AGENT_URL: \"http://updater:9701\""] if a.get("updater") else []) + [
        "    ports:",
        f"      - \"{a['scanarr_port']}:9797\"",
        "    volumes:",
        f"      - {_q(j('scanarr') + ':/config')}",
    ] + data_vols + [
        "    networks: [scanarr]",
        "    restart: unless-stopped",
    ]
    if a.get("updater"):
        sock = "/var/run/docker.sock"   # same path on Linux, NAS and Docker Desktop (Windows/Mac)
        lines += [
            "",
            "  updater:   # optional: lets the Update button in Scanarr install new versions (no ports, token-protected)",
            f"    image: {img}",
            "    container_name: scanarr-updater",
            "    command: [\"agent\"]",
            "    user: \"0:0\"",
            "    environment:",
            f"      SCANARR_AGENT_TOKEN: {_q(token)}",
            "      COMPOSE_FILE: \"/stack/compose.yml\"",
            "    volumes:",
            f"      - {_q(sock + ':/var/run/docker.sock')}",
            f"      - {_q(j('stack') + ':/stack')}",
            "    networks: [scanarr]",
            "    restart: unless-stopped",
        ]
    if a.get("jellyfin"):
        lines += [
            "",
            "  jellyfin:   # optional media player - library scans scheduled so the HDD can sleep",
            f"    image: {JELLYFIN_IMAGE}",
            "    container_name: scanarr-jellyfin",
            "    environment:",
            f"      PUID: {_q(a['puid'])}",
            f"      PGID: {_q(a['pgid'])}",
            f"      TZ: {_q(a['tz'])}",
            "    ports:",
            f"      - \"{a['jellyfin_port']}:8096\"",
            "    volumes:",
            f"      - {_q(j('jellyfin') + ':/config')}",
            f"      - {_q(done + ':/media:ro')}",
            "    networks: [scanarr]",
            "    restart: unless-stopped",
        ]
    if a.get("tailscale"):
        lines += [
            "",
            "  tailscale:   # optional: open Scanarr from anywhere at http://scanarr:9797 on your Tailscale network",
            f"    image: {TAILSCALE_IMAGE}",
            "    container_name: scanarr-tailscale",
            "    network_mode: \"service:scanarr\"   # shares Scanarr's network, so no router ports are opened",
            "    environment:",
            f"      TS_AUTHKEY: {_q(a.get('tailscale_authkey', ''))}",
            "      TS_HOSTNAME: \"scanarr\"",
            "      TS_STATE_DIR: \"/var/lib/tailscale\"",
            "      TS_USERSPACE: \"true\"",
            "    volumes:",
            f"      - {_q(j('tailscale') + ':/var/lib/tailscale')}",
            "    restart: unless-stopped",
        ]
    return "\n".join(lines) + "\n"
