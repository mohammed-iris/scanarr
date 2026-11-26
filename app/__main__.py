"""One image, several jobs:

  serve    the Scanarr app (default)                     port 9797
  setup    the setup wizard                              port 9700
  init     one-time: write qBittorrent/Prowlarr/Scanarr settings before they start
  agent    optional updater (only if you allow it Docker access)
  versions print the app version and the tested qBittorrent/Prowlarr images (used by updates)
"""
import json
import os
import sys


def main(argv):
    mode = argv[1] if len(argv) > 1 else "serve"
    if mode == "serve":
        import uvicorn
        uvicorn.run("app.main:app", host="0.0.0.0", port=int(os.environ.get("SCANARR_PORT", "9797")),
                    proxy_headers=True, forwarded_allow_ips="*")
    elif mode == "setup":
        import uvicorn
        uvicorn.run("app.setup_app:app", host="0.0.0.0", port=int(os.environ.get("SETUP_PORT", "9700")))
    elif mode == "agent":
        import uvicorn
        uvicorn.run("app.agent:app", host="0.0.0.0", port=9701)
    elif mode == "init":
        from . import stack
        try:
            for line in stack.init_from_env():
                print("init:", line, flush=True)
        except SystemExit as e:
            print(e, file=sys.stderr, flush=True)
            return 1
        print("init: done", flush=True)
    elif mode == "versions":
        from . import stack
        from .version import __version__
        print(json.dumps({"scanarr": __version__, "qbittorrent": stack.QBIT_IMAGE, "prowlarr": stack.PROWLARR_IMAGE}))
    elif mode == "health":  # Docker HEALTHCHECK: whichever web server this container runs must answer
        import urllib.request
        for port, path in ((os.environ.get("SCANARR_PORT", "9797"), "/login"), (os.environ.get("SETUP_PORT", "9700"), "/api/ping"), ("9701", "/ping")):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=4)
                return 0
            except Exception:
                continue
        return 1
    elif mode == "check":  # used by the setup wizard's helper containers (folder validation)
        from . import setup_checks
        print(json.dumps(setup_checks.folder_report(argv[2] if len(argv) > 2 else "/t")))
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
