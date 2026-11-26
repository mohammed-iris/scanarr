"""Folder checks run inside a short-lived helper container that has the folder mounted at /t.
Works the same on Windows (Docker Desktop), Linux and NAS, because Docker resolves the host path."""
import os
import shutil


def folder_report(path="/t"):
    out = {"exists": os.path.isdir(path), "writable": False, "free": None, "total": None,
           "uid": None, "gid": None, "dev": None, "entries": None, "error": None}
    try:
        os.makedirs(path, exist_ok=True)
        st = os.stat(path)
        out.update(exists=True, uid=st.st_uid, gid=st.st_gid, dev=st.st_dev)
        du = shutil.disk_usage(path)
        out.update(free=du.free, total=du.total)
        probe = os.path.join(path, ".scanarr-write-test")
        with open(probe, "w") as f:
            f.write("ok")
        os.remove(probe)
        out["writable"] = True
        out["entries"] = len([n for n in os.listdir(path) if not n.startswith(".")])
    except OSError as e:
        out["error"] = str(e)
    return out
