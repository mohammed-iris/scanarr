"""OpenSubtitles.com REST API (v1) client + file hash."""
import os
import re
import time
import struct

import httpx

API = "https://api.opensubtitles.com/api/v1"
UA = "Scanarr v0.1"


def os_hash(path):
    """OpenSubtitles hash: size + 64-bit sum of first and last 64 KiB."""
    chunk = 65536
    size = os.path.getsize(path)
    if size < chunk * 2:
        raise ValueError("file too small for OpenSubtitles hash")
    h = size
    with open(path, "rb") as f:
        for offset in (0, size - chunk):
            f.seek(offset)
            buf = f.read(chunk)
            for (v,) in struct.iter_unpack("<Q", buf):
                h = (h + v) & 0xFFFFFFFFFFFFFFFF
    return f"{h:016x}"


_SESSIONS = {}   # (api key, username) -> {"token", "base", "ts", "quota", "quota_ts"}: log in rarely
TOKEN_TTL, QUOTA_TTL = 12 * 3600, 600


def _tokens(s):
    return {t for t in re.split(r"[^a-z0-9]+", s.lower()) if len(t) > 1}


class OpenSubtitles:
    def __init__(self, api_key, username="", password=""):
        self.key, self.user, self.pw = api_key, username, password
        self.base = API
        self.token = None
        self.c = httpx.Client(timeout=30, follow_redirects=True,
                              headers={"Api-Key": api_key, "User-Agent": UA,
                                       "Accept": "application/json"})

    @property
    def enabled(self):
        return bool(self.key)

    def close(self):
        self.c.close()

    def _session(self):
        return _SESSIONS.setdefault((self.key, self.user, self.pw), {})

    def login(self, force=False):
        """Log in at most every 12 h (OpenSubtitles limits logins), sharing the token across requests."""
        if not (self.user and self.pw):
            return
        sess = self._session()
        if not force and sess.get("token") and time.time() - sess["ts"] < TOKEN_TTL:
            self.token, self.base = sess["token"], sess["base"]
            return
        r = self.c.post(f"{API}/login", json={"username": self.user, "password": self.pw})
        r.raise_for_status()
        d = r.json()
        self.token = d.get("token")
        if d.get("base_url"):
            self.base = f"https://{d['base_url']}/api/v1"
        sess.update(token=self.token, base=self.base, ts=time.time())

    def search(self, lang, path=None, query=None, year=None, season=None, episode=None, release=None,
               moviehash=None):
        params = {"languages": lang}
        if moviehash:  # stored earlier - no need to read the file (keeps the HDD asleep)
            params["moviehash"] = moviehash
        elif path:
            try:
                params["moviehash"] = os_hash(path)
            except (OSError, ValueError):
                pass
        if query:
            params["query"] = query.lower()
        if season is not None and episode is not None:
            params.update(type="episode", season_number=season, episode_number=episode)
        elif year:
            params.update(type="movie", year=year)
        # API wants alphabetically sorted params, otherwise it redirects
        r = self.c.get(f"{self.base}/subtitles", params=sorted(params.items()))
        r.raise_for_status()
        data = r.json().get("data") or []

        rel_tokens = _tokens(release or "")

        def score(s):
            a = s.get("attributes", {})
            names = " ".join([a.get("release") or ""] + [f.get("file_name") or "" for f in a.get("files", [])])
            overlap = len(rel_tokens & _tokens(names)) / (len(rel_tokens) or 1)
            return (bool(a.get("moviehash_match")), round(overlap, 1), not a.get("ai_translated"),
                    not a.get("machine_translated"), a.get("download_count") or 0)
        data = [s for s in data if s.get("attributes", {}).get("files")]
        return sorted(data, key=score, reverse=True)

    def download(self, file_id):
        if self.token is None and self.user:
            self.login()
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        r = self.c.post(f"{self.base}/download", json={"file_id": file_id}, headers=headers)
        if r.status_code == 401 and self.user:  # token expired early: log in again once
            self.login(force=True)
            r = self.c.post(f"{self.base}/download", json={"file_id": file_id},
                            headers={"Authorization": f"Bearer {self.token}"})
        if r.status_code == 406:
            raise RuntimeError("OpenSubtitles daily download limit reached")
        r.raise_for_status()
        d = r.json()
        if d.get("remaining") is not None:
            self._session().update(quota=d["remaining"], quota_ts=time.time())
        link = d["link"]
        return httpx.get(link, timeout=30, follow_redirects=True).content

    def quota(self):
        """Remaining downloads today (needs login). Cached for 10 minutes."""
        sess = self._session()
        if sess.get("quota_ts") and time.time() - sess["quota_ts"] < QUOTA_TTL:
            return sess.get("quota")
        if self.token is None and self.user:
            self.login()
        if not self.token:
            return None
        r = self.c.get(f"{self.base}/infos/user", headers={"Authorization": f"Bearer {self.token}"})
        r.raise_for_status()
        q = (r.json().get("data") or {}).get("remaining_downloads")
        sess.update(quota=q, quota_ts=time.time())
        return q

    def fetch(self, lang, video, dest_srt, moviehash=None, **kw):
        """Find the best subtitle for this exact file (hash + release name) and write it to dest_srt.
        Call it on the ORIGINAL downloaded file, before renaming. Returns description or None."""
        results = self.search(lang, path=video, moviehash=moviehash, **kw)
        if not results:
            return None
        best = results[0]["attributes"]
        content = self.download(best["files"][0]["file_id"])
        with open(dest_srt, "wb") as f:
            f.write(content)
        return f"{'hash match' if best.get('moviehash_match') else 'title match'}: {best.get('release', '')[:80]}"
