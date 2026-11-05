"""videorelay.py - copy mesh phones' video streams up to the TAK server's MediaMTX, so server users can watch them.

A phone on the mesh that streams to the deck (rtsp://<deck>:8554/<name>, from ATAK, ICU, a drone app ...) becomes
<server>:8554/<name> too: one ffmpeg per stream copies it (no re-encoding) while it is live. However many server users
watch, the deck's uplink carries it once. Ops then puts a VIDEO marker for it on the server side (see videomarkers.py),
at the phone's position, pointing at the server's copy.

Only streams PUBLISHED to the deck are copied; a stream the deck pulls from the server (an on-demand source) never is,
so nothing loops. A name already live on the server is left alone (copying it would take over someone else's stream).
The server must accept the deck publishing: whitelist the deck's server-side address (OpenTAKServer: OTS_IP_WHITELIST)
or put a login in the URL. Off by default (it uses uplink data: roughly 0.5-0.7 GB per hour per camera).

Lag guard: when the camera sends more than the uplink carries, video queues up and the server copy drifts further
and further behind live. Each copy's lag is measured (bytes still waiting in its send queue at its bitrate, plus any
time ffmpeg itself fell behind); past LAG_MAX seconds the copy is restarted, dropping the backlog, so server users
jump back to live. The server marker stays up throughout. Repeated jumps get a warning: lower the camera's bitrate.
Standard library + ffmpeg.
"""
import atexit
import os
import shutil
import signal
import subprocess
import threading
import time
from urllib.parse import urlsplit, urlunsplit

from videomarkers import rtsp_live

LAG_MAX = 5.0          # seconds behind live before the copy jumps back
LAG_MIN_RUN = 20.0     # a copy runs at least this long before it may be restarted for lag
LAG_WARN = (3, 600)    # this many jumps in this many seconds: say the uplink can't carry the camera

PUBLISHED = {"rtspSession", "rtspsSession", "rtmpConn", "rtmpsConn", "srtConn", "webRTCSession"}


def public_url(url):
    """The URL without any user:password (for markers and logs)."""
    u = urlsplit(url)
    host = u.hostname or ""
    if ":" in host:
        host = "[%s]" % host
    return urlunsplit((u.scheme, host + (":%d" % u.port if u.port else ""), u.path, "", ""))


def send_queue(pid):
    """Bytes waiting in a process's TCP send queues (the largest; Linux /proc), or None."""
    inodes = set()
    try:
        for fd in os.listdir("/proc/%d/fd" % pid):
            try:
                link = os.readlink("/proc/%d/fd/%s" % (pid, fd))
            except OSError:
                continue
            if link.startswith("socket:["):
                inodes.add(link[8:-1])
    except OSError:
        return None
    best = None
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(table) as f:
                next(f, None)
                for line in f:
                    c = line.split()
                    if len(c) > 9 and c[9] in inodes:
                        q = int(c[4].split(":")[0], 16)
                        best = q if best is None else max(best, q)
        except OSError:
            continue
    return best


class Progress:
    """Reads ffmpeg's -progress lines in a thread (so its pipe never fills): bytes sent, stream time, bitrate."""

    def __init__(self, stream):
        self.lock = threading.Lock()
        self.size = self.out_us = 0
        self.first = None            # (wall clock, stream time) at the first report
        self.last = None
        threading.Thread(target=self._read, args=(stream,), daemon=True).start()

    def _read(self, stream):
        cur = {}
        try:
            for raw in stream:
                k, _, v = raw.decode("ascii", "replace").strip().partition("=")
                cur[k] = v
                if k != "progress":
                    continue
                try:
                    size, us = int(cur.get("total_size", "0")), int(cur.get("out_time_us", "0"))
                except ValueError:
                    continue
                now = time.time()
                with self.lock:
                    self.size, self.out_us = size, us
                    if self.first is None and us > 0:
                        self.first = (now, us)
                    self.last = now
        except (OSError, ValueError):
            pass

    def get(self):
        with self.lock:
            return self.size, self.out_us, self.first


def lag_seconds(queued, size, out_us, first, now):
    """How far behind live a copy is: its send queue at its bitrate, plus any time ffmpeg itself fell behind."""
    if not first or out_us <= 0:
        return 0.0
    bps = size / (out_us / 1e6) if out_us > 0 else 0
    q = (queued or 0) / bps if bps > 0 else 0.0
    behind = (now - first[0]) - (out_us - first[1]) / 1e6
    return q + max(0.0, behind)


def wanted(paths):
    """Names of streams live on the deck because someone publishes them to it."""
    return sorted(p["name"] for p in paths if p.get("ready") and p.get("source") in PUBLISHED and p.get("name"))


class StreamRelay:
    def __init__(self, server, local="rtsp://127.0.0.1:8554", ffmpeg=None, probe=rtsp_live, every=2.0, log=None):
        self.server = server.rstrip("/")
        self.local = local.rstrip("/")
        self.ffmpeg = ffmpeg or shutil.which("ffmpeg")
        self.probe, self.every = probe, float(every)
        self.queue = send_queue
        self.log = log or (lambda level, text: None)
        self.lock = threading.Lock()
        self.want = set()
        self.procs = {}          # name -> (Popen, started)
        self.backoff = {}        # name -> (next try, wait)
        self.conflicts = set()
        self.errors = {}
        self.up = {}             # name -> copying since (kept across lag restarts: the server marker stays up)
        self.jumps = {}          # name -> [times it jumped back to live]
        self.lag = {}            # name -> seconds behind live (last check)
        self.thread = None
        atexit.register(self.stop_all)      # no copies left running after Ops exits

    # ---- the dashboard tells us what is live; status() never blocks
    def set_wanted(self, names):
        with self.lock:
            self.want = set(names)
        if self.thread is None:
            self.thread = threading.Thread(target=self._run, name="videorelay", daemon=True)
            self.thread.start()

    def ok(self, now=None):
        """Streams being copied for at least 3 s (their server copy is up): these get server-side markers."""
        now = time.time() if now is None else now
        with self.lock:
            return sorted(n for n, (p, t0, _) in self.procs.items() if p.poll() is None and now - self.up.get(n, t0) >= 3)

    def status(self):
        if not self.ffmpeg:
            return {"state": "ffmpeg not installed", "relaying": [], "conflicts": [], "errors": {}}
        with self.lock:
            return {"state": "on", "relaying": sorted(n for n, (p, _, _) in self.procs.items() if p.poll() is None),
                    "conflicts": sorted(self.conflicts), "errors": dict(self.errors),
                    "lag": {n: round(v, 1) for n, v in self.lag.items()},
                    "jumps": {n: len(v) for n, v in self.jumps.items() if v}}

    # ---- the worker
    def _run(self):
        while True:
            try:
                self.step()
            except Exception as exc:                       # never lose the thread
                self.log("warn", "Video relay: %s" % exc)
            time.sleep(self.every)

    def _cmd(self, name):
        return [self.ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-nostats", "-progress", "pipe:1",
                "-rtsp_transport", "tcp",
                "-i", "%s/%s" % (self.local, name), "-c", "copy", "-f", "rtsp", "-rtsp_transport", "tcp",
                "%s/%s" % (self.server, name)]

    def _stop(self, name, why, quiet=False):
        p, _, _ = self.procs.pop(name)
        if p.poll() is None:
            try:
                os.killpg(p.pid, signal.SIGTERM)
                p.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except OSError:
                    pass
        if not quiet:
            self.log("info", "Video relay stopped: %s (%s)" % (name, why))
            self.up.pop(name, None)
            self.lag.pop(name, None)

    def step(self, now=None):
        now = time.time() if now is None else now
        if not self.ffmpeg:
            return
        with self.lock:
            want = set(self.want)
        for name in [n for n in self.procs if n not in want]:
            with self.lock:
                self._stop(name, "stream ended")
        for name in list(self.conflicts):
            if name not in want:
                self.conflicts.discard(name)
        for name in sorted(want):
            with self.lock:
                cur = self.procs.get(name)
            if cur and cur[0].poll() is None:
                if self._check_lag(name, cur, now):
                    self._start(name, now)                      # straight back: no wait, no "already live" check
                continue
            if cur:                                           # ffmpeg ended while the stream is still live
                p, t0, _ = cur
                err = (p.stderr.read().decode(errors="replace").strip().splitlines() or ["exit %s" % p.returncode])[-1][:200] if p.stderr else ""
                with self.lock:
                    self.procs.pop(name, None)
                    self.up.pop(name, None)
                    self.errors[name] = err
                wait = 5.0 if now - t0 > 60 else min(60.0, (self.backoff.get(name, (0, 2.5))[1]) * 2)
                self.backoff[name] = (now + wait, wait)
                self.log("warn", "Video relay for %s stopped (%s); trying again in %d s" % (name, err or "no reason given", wait))
                continue
            if now < self.backoff.get(name, (0, 0))[0]:
                continue
            if self.probe("%s/%s" % (self.server, name)) is True:
                if name not in self.conflicts:
                    self.log("warn", "Video relay: %s is already live on the server; not copying it (would take it over)" % name)
                self.conflicts.add(name)
                self.backoff[name] = (now + 30, 30)
                continue
            self.conflicts.discard(name)
            if self._start(name, now):
                self.log("ok", "Video relay started: %s -> %s/%s" % (name, public_url(self.server), name))

    def _start(self, name, now):
        try:
            p = subprocess.Popen(self._cmd(name), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, start_new_session=True)
        except OSError as exc:
            self.errors[name] = str(exc)
            self.backoff[name] = (now + 60, 60)
            return False
        with self.lock:
            self.procs[name] = (p, now, Progress(p.stdout))
            self.up.setdefault(name, now)
            self.errors.pop(name, None)
        return True

    def _check_lag(self, name, cur, now):
        """True when this copy is too far behind live and should jump back (it has then been stopped)."""
        p, t0, prog = cur
        size, out_us, first = prog.get()
        lag = lag_seconds(self.queue(p.pid), size, out_us, first, time.time())
        self.lag[name] = lag
        if lag <= LAG_MAX or now - t0 < LAG_MIN_RUN:
            return False
        with self.lock:
            self._stop(name, "lag", quiet=True)
        j = [t for t in self.jumps.get(name, []) if now - t < LAG_WARN[1]] + [now]
        self.jumps[name] = j
        mbit = size * 8 / (out_us / 1e6) / 1e6 if out_us > 0 else 0
        if len(j) >= LAG_WARN[0]:
            self.log("warn", "Video relay for %s keeps falling behind (%d jumps in %d min): the camera sends %.1f Mbit/s, "
                     "more than the uplink carries. Lower the camera's bitrate or resolution." % (name, len(j), LAG_WARN[1] // 60, mbit))
            self.jumps[name] = []
        else:
            self.log("info", "Video relay for %s was %.0f s behind live (uplink slower than the camera's %.1f Mbit/s); "
                     "jumped back to live" % (name, lag, mbit))
        return True

    def stop_all(self):
        with self.lock:
            for name in list(self.procs):
                self._stop(name, "Ops stopping")
