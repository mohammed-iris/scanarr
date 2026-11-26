"""Release-name parsing and clean target names (Title (Year) / Show S01E01)."""
import os
import time
import re

import httpx
from guessit import guessit

ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def fix_mojibake(s):
    """Repair UTF-8 text that was decoded as Latin-1 (e.g. 'Â¡Ya llega!')."""
    if any(c in s for c in "ÃÂâ"):
        try:
            return s.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    return s


WIN_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                *(f"LPT{i}" for i in range(1, 10))}


def clean(s):
    s = ILLEGAL.sub("", fix_mojibake(str(s)))
    s = re.sub(r"\s+", " ", s).strip().rstrip(". ") or "Unknown"
    return s + "_" if s.upper() in WIN_RESERVED else s


def parse(name, kind=None):
    """kind: 'movie' or 'episode' hint (from the qBit category)."""
    opts = {"type": kind} if kind in ("movie", "episode") else {}
    g = guessit(name, opts)
    ep = g.get("episode")
    if isinstance(ep, int):
        ep = [ep]
    year = g.get("year")
    if isinstance(year, list):  # e.g. "(2015-2026)"
        year = year[0]
    season = g.get("season")
    if isinstance(season, list):
        season = season[0]
    return {
        "type": g.get("type"),
        "title": g.get("title"),
        "year": year,
        "season": season,
        "episodes": ep or [],
        "language": g.get("subtitle_language") or g.get("language"),
    }


_TMDB_CACHE = {}   # (kind, title, year) -> (timestamp, result); shared across cycles
TMDB_TTL = 7 * 86400


class TMDb:
    def __init__(self, key):
        self.key = (key or "").strip()

    def _cached(self, kind, title, year, fn):
        k = (kind, (title or "").lower(), year)
        hit = _TMDB_CACHE.get(k)
        if hit and time.time() - hit[0] < TMDB_TTL:
            return hit[1]
        val = fn()
        if len(_TMDB_CACHE) > 2000:
            _TMDB_CACHE.clear()
        _TMDB_CACHE[k] = (time.time(), val)
        return val

    def _get(self, path, params):
        headers = {}
        params = dict(params)
        if len(self.key) > 40:  # v4 read access token
            headers["Authorization"] = f"Bearer {self.key}"
        else:
            params["api_key"] = self.key
        r = httpx.get(f"https://api.themoviedb.org/3/{path}", params=params,
                      headers=headers, timeout=15)
        r.raise_for_status()
        return r.json().get("results") or []

    def movie(self, title, year):
        if not self.key or not title:
            return None
        return self._cached("movie", title, year, lambda: self._movie(title, year))

    def _movie(self, title, year):
        params = {"query": title}
        if year:
            params["year"] = year
        res = self._get("search/movie", params)
        if not res and year:  # year in release name is sometimes off by one
            res = self._get("search/movie", {"query": title})
        if not res:
            return None
        m = res[0]
        y = (m.get("release_date") or "")[:4]
        return {"title": m.get("title") or title, "year": int(y) if y.isdigit() else year}

    def show(self, title, year):
        if not self.key or not title:
            return None
        return self._cached("tv", title, year, lambda: self._show(title, year))

    def _show(self, title, year):
        params = {"query": title}
        if year:
            params["first_air_date_year"] = year
        res = self._get("search/tv", params) or self._get("search/tv", {"query": title})
        if not res:
            return None
        return {"title": res[0].get("name") or title}


def movie_name(title, year):
    t = clean(title)
    return f"{t} ({year})" if year else t


def episode_name(show, season, episodes):
    eps = "".join(f"E{e:02d}" for e in episodes)
    return f"{clean(show)} S{season:02d}{eps}"


def movie_target(root, title, year, ext, folders=True):
    base = movie_name(title, year)
    return os.path.join(root, base, base + ext) if folders else os.path.join(root, base + ext)


def episode_target(root, show, season, episodes, ext):
    return os.path.join(root, clean(show), f"Season {season:02d}",
                        episode_name(show, season, episodes) + ext)
