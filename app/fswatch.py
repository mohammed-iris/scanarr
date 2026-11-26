"""Linux file-change events (inotify) for the library and the Unsorted folder.

- The kernel tells us when files appear/move/disappear, so the library index stays current
  without rescans, and files dropped into Unsorted are noticed right away.
- Watching costs nothing and never wakes the HDD. Setting the watches up walks the folders once,
  so it's only started while the HDD is awake anyway (during housekeeping).
- Not used where events are unreliable (Windows, Docker Desktop shares, network mounts) or if
  anything fails: Scanarr then simply keeps scanning, exactly as before.
"""
import ctypes
import ctypes.util
import logging
import os
import select
import struct
import threading
import time

log = logging.getLogger("scanarr.fswatch")

IN_MODIFY, IN_CLOSE_WRITE, IN_MOVED_FROM, IN_MOVED_TO = 0x2, 0x8, 0x40, 0x80
IN_CREATE, IN_DELETE, IN_DELETE_SELF, IN_MOVE_SELF = 0x100, 0x200, 0x400, 0x800
IN_Q_OVERFLOW, IN_IGNORED, IN_ISDIR, IN_ONLYDIR = 0x4000, 0x8000, 0x40000000, 0x01000000
MASK = IN_CLOSE_WRITE | IN_MOVED_FROM | IN_MOVED_TO | IN_CREATE | IN_DELETE | IN_DELETE_SELF | IN_MOVE_SELF | IN_ONLYDIR
HDR = struct.Struct("iIII")

# filesystems where changes made by another machine/OS don't produce events here
UNRELIABLE_FS = {"9p", "fuse.grpcfuse", "virtiofs", "drvfs", "cifs", "smb3", "smbfs", "nfs", "nfs4",
                 "fuse.sshfs", "fakeowner", "fuse"}

STATE = {"active": False, "reason": "not started", "overflow": False, "watches": 0}


def fs_type(path):
    """Filesystem type of the mount holding path (from /proc/mounts)."""
    try:
        path = os.path.realpath(path)
        best, typ = "", None
        with open("/proc/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 3:
                    continue
                mnt = parts[1].replace("\\040", " ")
                if (path == mnt or path.startswith(mnt.rstrip("/") + "/")) and len(mnt) >= len(best):
                    best, typ = mnt, parts[2]
        return typ
    except OSError:
        return None


def usable(path):
    if os.name != "posix" or not os.path.isdir(path):
        return False, "not Linux" if os.name != "posix" else "folder missing"
    t = fs_type(path)
    if t in UNRELIABLE_FS or (t or "").startswith("fuse."):
        return False, f"{t} share - changes from other machines aren't reported, using scans"
    return True, t or "?"


class Watcher(threading.Thread):
    def __init__(self, roots, on_change, skip=lambda name: name.startswith((".", "@"))):
        super().__init__(daemon=True, name="fswatch")
        self.roots = [os.path.normpath(r) for r in roots]
        self.on_change = on_change          # on_change(kind, path) kind: "add" | "remove" | "dir" | "overflow"
        self.skip = skip
        self.wd = {}
        self.stop_flag = threading.Event()
        libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
        self._add = libc.inotify_add_watch
        self._add.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        self._rm = libc.inotify_rm_watch
        self.fd = libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        if self.fd < 0:
            raise OSError(ctypes.get_errno(), "inotify_init1 failed")

    def add_tree(self, top):
        stack = [top]
        while stack:
            d = stack.pop()
            w = self._add(self.fd, os.fsencode(d), MASK)
            if w < 0:
                err = ctypes.get_errno()
                if err == 28:  # ENOSPC: watch limit reached
                    raise OSError(err, "too many folders to watch (raise fs.inotify.max_user_watches)")
                continue
            self.wd[w] = d
            try:
                with os.scandir(d) as it:
                    stack += [e.path for e in it if e.is_dir(follow_symlinks=False) and not self.skip(e.name)]
            except OSError:
                pass

    def setup(self):
        for r in self.roots:
            if os.path.isdir(r):
                self.add_tree(r)
        STATE["watches"] = len(self.wd)

    def run(self):
        buf = b""
        while not self.stop_flag.is_set():
            r, _, _ = select.select([self.fd], [], [], 1.0)
            if not r:
                continue
            try:
                buf += os.read(self.fd, 65536)
            except BlockingIOError:
                continue
            except OSError as e:
                log.warning("file watching stopped: %s", e)
                break
            while len(buf) >= HDR.size:
                wd, mask, _cookie, ln = HDR.unpack_from(buf)
                if len(buf) < HDR.size + ln:
                    break
                name = buf[HDR.size:HDR.size + ln].rstrip(b"\0").decode("utf-8", "surrogateescape")
                buf = buf[HDR.size + ln:]
                try:
                    self._event(wd, mask, name)
                except Exception:
                    log.exception("file event")
        STATE["active"] = False
        try:
            os.close(self.fd)
        except OSError:
            pass

    def _event(self, wd, mask, name):
        if mask & IN_Q_OVERFLOW:
            STATE["overflow"] = True
            self.on_change("overflow", None)
            return
        d = self.wd.get(wd)
        if mask & IN_IGNORED:
            self.wd.pop(wd, None)
            return
        if d is None or not name or self.skip(name):
            return
        p = os.path.join(d, name)
        if mask & IN_ISDIR:
            if mask & (IN_CREATE | IN_MOVED_TO):
                try:
                    self.add_tree(p)
                except OSError as e:
                    log.warning("%s", e)
                    STATE["overflow"] = True
                self.on_change("dir", p)          # a moved-in folder: its files need indexing
            elif mask & (IN_DELETE | IN_MOVED_FROM):
                self.on_change("rmdir", p)
            return
        if mask & (IN_CLOSE_WRITE | IN_MOVED_TO | IN_CREATE):
            self.on_change("add", p)
        elif mask & (IN_DELETE | IN_MOVED_FROM):
            self.on_change("remove", p)

    def stop(self):
        self.stop_flag.set()


_cur = {"w": None, "roots": None}
_lock = threading.Lock()


def ensure(roots, on_change):
    """Start (or restart, if the folders changed) watching. Call while the HDD is awake.
    Returns True when events are active."""
    roots = sorted(os.path.normpath(r) for r in roots if r)
    with _lock:
        w = _cur["w"]
        if w and w.is_alive() and _cur["roots"] == roots and not STATE["overflow"]:
            return True
        if w:
            w.stop()
            _cur["w"] = None
        problems = [why for ok, why in (usable(r) for r in roots if os.path.isdir(r)) if not ok]
        if not roots or problems:
            STATE.update(active=False, reason=problems[0] if problems else "no folders")
            return False
        try:
            w = Watcher(roots, on_change)
            w.setup()
            w.start()
        except Exception as e:
            STATE.update(active=False, reason=str(e)[:200])
            log.info("file change events not available (%s) - using scans", e)
            return False
        _cur.update(w=w, roots=roots)
        STATE.update(active=True, reason=f"watching {len(w.wd)} folders", overflow=False, since=time.time())
        log.info("watching %d folders for changes", len(w.wd))
        return True


def stop():
    with _lock:
        if _cur["w"]:
            _cur["w"].stop()
        _cur.update(w=None, roots=None)
        STATE.update(active=False, reason="stopped")
