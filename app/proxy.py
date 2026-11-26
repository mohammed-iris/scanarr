"""Single sign-on gateway: /qbit/... and /prowlarr/... are passed through to the private apps.

qBittorrent and Prowlarr sit on a private Docker network and trust it (set up by the wizard), so the
only way to reach them is through Scanarr, behind your one Scanarr login. Nothing is stored or
rewritten except what's needed for the apps to work under a sub-path.
"""
import asyncio
import logging
from urllib.parse import urlsplit

import httpx
from fastapi import Request, WebSocket
from starlette.responses import RedirectResponse, Response, StreamingResponse

from . import auth, config

log = logging.getLogger("scanarr.proxy")

HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
       "transfer-encoding", "upgrade", "host", "content-length"}
_client = {"c": None}


def _http():
    if _client["c"] is None:
        _client["c"] = httpx.AsyncClient(timeout=httpx.Timeout(60, connect=5), follow_redirects=False)
    return _client["c"]


def targets(cfg=None):
    """name -> (prefix, upstream origin, keep_prefix). Only for stacks set up by the wizard."""
    cfg = cfg or config.load()
    out = {}
    q = urlsplit(cfg["qbit"].get("url") or "")
    if q.scheme and q.netloc:
        out["qbit"] = ("/qbit", f"{q.scheme}://{q.netloc}", False)
    p = urlsplit(cfg["prowlarr"].get("url") or "")
    if p.scheme and p.netloc and p.path.rstrip("/") == "/prowlarr":
        out["prowlarr"] = ("/prowlarr", f"{p.scheme}://{p.netloc}", True)   # Prowlarr runs with UrlBase=/prowlarr
    return out


async def http_proxy(name, request: Request, path: str):
    t = targets().get(name)
    if not t:
        return Response("Not available: this app isn't part of a Scanarr-managed stack.", status_code=404)
    prefix, origin, keep = t
    if path == "" and not request.url.path.endswith("/"):
        return RedirectResponse(prefix + "/", status_code=307)
    up_path = (prefix + "/" + path) if keep else ("/" + path)
    url = origin + up_path + (("?" + request.url.query) if request.url.query else "")
    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP and k.lower() != "cookie"}
    # pass the app's own cookies, never Scanarr's session cookie
    cookies = [c.strip() for c in request.headers.get("cookie", "").split(";")]
    cookies = [c for c in cookies if c and not c.startswith(auth.COOKIE + "=")]
    if cookies:
        headers["cookie"] = "; ".join(cookies)
    for h in ("origin", "referer"):  # qBittorrent's CSRF check compares these with its own address
        if h in headers:
            headers[h] = origin + (up_path if h == "referer" else "")
    headers["x-forwarded-prefix"] = prefix
    safe = request.method in ("GET", "HEAD", "OPTIONS")
    up = None
    for attempt in (1, 2):   # GETs are retried once: a stale pooled connection or an app restart shouldn't show an error
        try:
            up = await _http().send(_http().build_request(request.method, url, headers=headers,
                                                         content=None if safe else request.stream()), stream=True)
            break
        except httpx.HTTPError as e:
            log.warning("%s proxy %s %s failed (try %d): %r", name, request.method, up_path, attempt, e)
            if not safe or attempt == 2:
                return Response(f"{name} is not reachable right now ({e.__class__.__name__}). It may still be starting.",
                                status_code=502)
            await asyncio.sleep(0.5)
    out_headers = []
    for k, v in up.headers.multi_items():
        lk = k.lower()
        if lk in HOP:
            continue
        if lk == "location":
            if v.startswith(origin):
                v = v[len(origin):]
            if v.startswith("/") and not keep and not v.startswith(prefix):
                v = prefix + v
        if lk == "set-cookie" and not keep:
            v = v.replace("path=/;", f"path={prefix}/;").replace("Path=/;", f"Path={prefix}/;")
        out_headers.append((k, v))

    async def body():
        try:
            async for chunk in up.aiter_raw():
                yield chunk
        finally:
            await up.aclose()
    resp = StreamingResponse(body(), status_code=up.status_code)
    resp.raw_headers = [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in out_headers]
    return resp


async def ws_proxy(name, ws: WebSocket, path: str):
    """WebSockets (Prowlarr's live updates). Starlette's HTTP middleware doesn't see these, so check login here."""
    if not auth.valid(ws.cookies.get(auth.COOKIE)):
        await ws.close(code=4401)
        return
    t = targets().get(name)
    if not t:
        await ws.close(code=4404)
        return
    import websockets
    prefix, origin, keep = t
    up_path = (prefix + "/" + path) if keep else ("/" + path)
    url = origin.replace("http", "ws", 1) + up_path + (("?" + ws.url.query) if ws.url.query else "")
    await ws.accept()
    try:
        async with websockets.connect(url, open_timeout=10, max_size=None) as up:
            async def a2b():
                while True:
                    m = await ws.receive()
                    if m["type"] == "websocket.disconnect":
                        await up.close()
                        return
                    await up.send(m.get("text") if m.get("text") is not None else m.get("bytes"))

            async def b2a():
                async for m in up:
                    if isinstance(m, bytes):
                        await ws.send_bytes(m)
                    else:
                        await ws.send_text(m)
            done, pending = await asyncio.wait([asyncio.create_task(a2b()), asyncio.create_task(b2a())],
                                               return_when=asyncio.FIRST_COMPLETED)
            for p in pending:
                p.cancel()
    except Exception as e:
        log.debug("websocket proxy closed: %s", e)
    finally:
        try:
            await ws.close()
        except Exception:
            pass
