"""Minimal qBittorrent WebUI API v2 client."""
import httpx


class QBitError(Exception):
    pass


class QBit:
    def __init__(self, url, username="", password="", api_key="", timeout=20):
        self.base = url.rstrip("/")
        self.username, self.password = username, password
        headers = {"Referer": self.base}
        if api_key:  # qBittorrent 5.2+: stateless Bearer auth, no login round-trip
            headers["Authorization"] = f"Bearer {api_key}"
        self.c = httpx.Client(timeout=timeout, headers=headers)
        self._logged_in = bool(api_key)
        self.api_key = api_key

    @classmethod
    def from_cfg(cls, q):
        return cls(q["url"], q.get("username", ""), q.get("password", ""), q.get("api_key", ""))

    def close(self):
        self.c.close()

    def login(self):
        r = self.c.post(f"{self.base}/api/v2/auth/login",
                        data={"username": self.username, "password": self.password})
        if r.status_code != 200 or r.text.strip() != "Ok.":
            raise QBitError(f"qBittorrent login failed ({r.status_code}: {r.text.strip()[:80]})")
        self._logged_in = True

    def _req(self, method, path, **kw):
        if not self._logged_in:
            self.login()
        r = self.c.request(method, f"{self.base}/api/v2/{path}", **kw)
        if r.status_code in (401, 403):
            if self.api_key:
                raise QBitError("qBittorrent rejected the API key (needs qBittorrent 5.2+)")
            self.login()
            r = self.c.request(method, f"{self.base}/api/v2/{path}", **kw)
        if r.status_code >= 400:
            raise QBitError(f"{path}: HTTP {r.status_code} {r.text[:120]}")
        return r

    def version(self):
        return self._req("GET", "app/version").text

    def ensure_category(self, name):
        try:
            self._req("POST", "torrents/createCategory", data={"category": name, "savePath": ""})
        except QBitError as e:
            if "409" not in str(e):
                raise

    def add(self, url, category, savepath=None, download_path=None, metadata_only=False, tags=None):
        self.ensure_category(category)
        data = {"urls": url, "category": category}
        if metadata_only:  # fetch the file list, then stop (nothing is downloaded)
            data["stopCondition"] = "MetadataReceived"
        if tags:
            data["tags"] = tags
        if savepath:  # Scanarr decides the folder (manual mode for this torrent)
            data.update(savepath=savepath, autoTMM="false")
        if download_path:  # keep unfinished data in Temp (qBit 4.4+)
            data.update(useDownloadPath="true", downloadPath=download_path)
        r = self._req("POST", "torrents/add", data=data)
        if r.text.strip().lower().startswith("fail"):
            raise QBitError("qBittorrent rejected the torrent")

    def completed(self, category):
        return self._req("GET", "torrents/info",
                         params={"filter": "completed", "category": category}).json()

    def list(self, categories=None):
        ts = self._req("GET", "torrents/info").json()
        return [t for t in ts if not categories or t.get("category") in categories]

    def preferences(self):
        return self._req("GET", "app/preferences").json()

    def set_preferences(self, prefs):
        import json
        self._req("POST", "app/setPreferences", data={"json": json.dumps(prefs)})

    def free_space(self):
        """Free space qBit reports for its Default Save Path (it checks this itself anyway),
        so Scanarr never has to touch the HDD to know it."""
        st = self._req("GET", "sync/maindata", params={"rid": 0}).json().get("server_state") or {}
        return st.get("free_space_on_disk")

    def transfer(self):
        return self._req("GET", "transfer/info").json()

    def files(self, h):
        return self._req("GET", "torrents/files", params={"hash": h}).json()

    def add_tag(self, h, tag):
        self._req("POST", "torrents/addTags", data={"hashes": h, "tags": tag})

    def info(self, h):
        ts = self._req("GET", "torrents/info", params={"hashes": h}).json()
        return ts[0] if ts else None

    def file_prio(self, h, ids, prio):
        if ids:
            self._req("POST", "torrents/filePrio", data={"hash": h, "id": "|".join(map(str, ids)), "priority": prio})

    def set_category(self, h, cat):
        self.ensure_category(cat)
        self._req("POST", "torrents/setCategory", data={"hashes": h, "category": cat})

    def set_location(self, h, path):
        self._req("POST", "torrents/setLocation", data={"hashes": h, "location": path})

    def remove_tag(self, h, tag):
        self._req("POST", "torrents/removeTags", data={"hashes": h, "tags": tag})

    def control(self, action, hashes):
        """action: stop|start|recheck|reannounce. Falls back to v4 names (pause/resume)."""
        data = {"hashes": "|".join(hashes)}
        try:
            self._req("POST", f"torrents/{action}", data=data)
        except QBitError as e:
            legacy = {"stop": "pause", "start": "resume"}.get(action)
            if legacy and "404" in str(e):
                self._req("POST", f"torrents/{legacy}", data=data)
            else:
                raise

    def delete(self, h, delete_files=True):
        self._req("POST", "torrents/delete",
                  data={"hashes": h, "deleteFiles": "true" if delete_files else "false"})
