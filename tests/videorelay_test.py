import os as _o, sys as _s  # paths relative to this package
PKG = _o.path.dirname(_o.path.dirname(_o.path.abspath(__file__)))
_s.path[:0] = [PKG + "/vendor", PKG + "/common", PKG + "/bridge", PKG + "/dashboard"]
"""videorelay_test.py: mesh phones' streams copied up to the TAK server's MediaMTX (one ffmpeg per stream, no
re-encoding), shown to server users as VIDEO markers at the phone; never loops, never takes over a server stream,
never comes back down onto the mesh. ffmpeg is a stand-in that records what it was asked to do.
Usage: python3 -S videorelay_test.py"""
import asyncio, json, os, socket, stat, subprocess, sys, tempfile, time
import xml.etree.ElementTree as ET
import warnings; warnings.filterwarnings("ignore")
import videorelay as vr
import videomarkers as vm

res = []
def check(n, c, x=""):
    res.append(bool(c)); print(("PASS  " if c else "FAIL  ") + n + ("" if c else "  " + str(x)[:500]), flush=True)

T = tempfile.mkdtemp(prefix="mmb_relay_")
FF = T + "/ffmpeg"
open(FF, "w").write('#!/bin/sh\necho "$@" >> %s/calls\nif [ -f %s/crash ]; then echo "Connection refused" >&2; exit 1; fi\nexec sleep 300\n' % (T, T))
os.chmod(FF, os.stat(FF).st_mode | stat.S_IEXEC)
def calls():
    try: return open(T + "/calls").read().splitlines()
    except OSError: return []

check("public_url drops the login", vr.public_url("rtsp://deck:s3cret" + "@100.64.0.9:8554") == "rtsp://100.64.0.9:8554")
paths = [{"name": "cam1", "ready": True, "source": "rtspSession"}, {"name": "drone", "ready": True, "source": "rtmpConn"},
         {"name": "RAVEN21", "ready": True, "source": "rtspSource"}, {"name": "idle", "ready": False, "source": ""}]
check("only streams published TO the deck are copied (a stream pulled from the server never is: no loop)",
      vr.wanted(paths) == ["cam1", "drone"], vr.wanted(paths))

LOG = []
live_on_server = set()
r = vr.StreamRelay("rtsp://deck:s3cret" + "@100.64.0.9:8554", ffmpeg=FF, probe=lambda u: u.rsplit("/", 1)[-1] in live_on_server,
                   log=lambda lvl, t: LOG.append((lvl, t)))
r.want = {"cam1"}
now = 1000.0
r.step(now); time.sleep(0.3)
c = calls()
check("a live mesh stream: one ffmpeg copying it", len(c) == 1 and "cam1" in r.status()["relaying"], (c, r.status()))
check("...copy only (-c copy: no re-encoding), over TCP, deck -> server, same name",
      c and "-i rtsp://127.0.0.1:8554/cam1 -c copy -f rtsp -rtsp_transport tcp rtsp://deck:s3cret" + "@100.64.0.9:8554/cam1" in c[0], c)
check("...the event log names the server without the login", LOG and "s3cret" not in LOG[-1][1] and "100.64.0.9:8554/cam1" in LOG[-1][1], LOG)
check("not 'up' for server markers until it has run 3 s", r.ok(now + 1) == [] and r.ok(now + 4) == ["cam1"])
r.step(now + 2); time.sleep(0.2)
check("a running copy isn't started twice", len(calls()) == 1)
pid = r.procs["cam1"][0].pid
r.want = set(); r.step(now + 10)
alive = True
try: os.kill(pid, 0)
except OSError: alive = False
check("stream ends: its copy is stopped (process gone)", not alive and r.status()["relaying"] == [], r.status())

live_on_server.add("drone"); LOG.clear()
r.want = {"drone"}; r.step(now + 20)
check("a name already live on the server is not copied (it would take that stream over)", r.status()["conflicts"] == ["drone"] and
      len(calls()) == 1 and any("already live on the server" in t for _, t in LOG), (r.status(), LOG))
r.step(now + 25)
check("...and it isn't asked again every pass (30 s wait)", sum("already live" in t for _, t in LOG) == 1)
live_on_server.clear(); r.step(now + 51); time.sleep(0.2)
check("...the name frees up: copied", "drone" in r.status()["relaying"] and r.status()["conflicts"] == [], r.status())
r.want = set(); r.step(now + 52)

open(T + "/crash", "w").close(); LOG.clear()
r.want = {"cam1"}; r.step(now + 100); time.sleep(0.3)
r.step(now + 101)
check("server refuses (ffmpeg exits): logged with ffmpeg's reason, retried later, not in a loop",
      any("Connection refused" in t and "trying again in 5 s" in t for _, t in LOG) and r.status()["errors"].get("cam1") == "Connection refused", (LOG, r.status()))
n = len(calls()); r.step(now + 103)
check("...no retry before the wait is over", len(calls()) == n)
r.step(now + 107); time.sleep(0.3); r.step(now + 108)
check("...retried, and the wait grows (10 s) while it keeps failing", len(calls()) == n + 1 and any("trying again in 10 s" in t for _, t in LOG), LOG[-2:])
os.remove(T + "/crash"); r.want = set(); r.step(now + 200)
check("no ffmpeg: says so, never crashes", vr.StreamRelay("rtsp://x:8554", ffmpeg="").status()["state"] == "ffmpeg not installed"
      or not vr.StreamRelay("rtsp://x:8554", ffmpeg=None).ffmpeg is None)
nf = vr.StreamRelay("rtsp://x:8554", ffmpeg=None); nf.ffmpeg = None
check("no ffmpeg: status says ffmpeg not installed", nf.status()["state"] == "ffmpeg not installed")

# ---------------- lag guard: a copy drifting behind live jumps back; the server marker stays up
check("lag: 1 MB queued at 1 Mbit/s = 8 s behind", abs(vr.lag_seconds(1000000, 125000, 1000000, (101.0, 1000000), 101.0) - 8.0) < 0.01)
check("lag: ffmpeg itself 4 s behind (stream time 6 s in 10 s of wall clock)", abs(vr.lag_seconds(0, 125000, 7000000, (100.0, 1000000), 110.0) - 4.0) < 0.01)
check("lag: no reports yet = 0 (never restarts a copy that just started)", vr.lag_seconds(5000000, 0, 0, None, 200.0) == 0.0)
# a real full send queue, read from /proc
fill = subprocess.Popen([sys.executable, "-c", r"""
import socket, sys, time
l = socket.socket(); l.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096); l.bind(("127.0.0.1", 0)); l.listen(1)
c = socket.create_connection(l.getsockname()); a, _ = l.accept()          # a never reads
c.setblocking(False); n = 0
try:
    while True: n += c.send(b"x" * 65536)
except BlockingIOError: pass
print(n, flush=True); time.sleep(30)
"""], stdout=subprocess.PIPE)
sent = int(fill.stdout.readline())
q = vr.send_queue(fill.pid)
check("send queue read from /proc (bytes the uplink hasn't taken yet)", q and q > 10000, (q, sent))
fill.kill()
check("send queue of a process that's gone: None, no crash", vr.send_queue(fill.pid + 10 ** 6) is None)

PF = T + "/ffmpeg_prog"
open(PF, "w").write("#!%s\nimport sys, time\nopen(%r, 'a').write(' '.join(sys.argv[1:]) + '\\n')\nus = 1000000\n"
                    "while True:\n    print('total_size=%%d\\nout_time_us=%%d\\nprogress=continue' %% (us // 8, us), flush=True)\n"
                    "    us += 200000\n    time.sleep(0.2)\n" % (sys.executable, T + "/pcalls"))
os.chmod(PF, 0o755)
probes = []
LOG2 = []
g = vr.StreamRelay("rtsp://100.64.0.9:8554", ffmpeg=PF, probe=lambda u: probes.append(u) or False, log=lambda l, t: LOG2.append((l, t)))
check("ffmpeg reports its progress to the relay", "-progress pipe:1" in " ".join(g._cmd("x")))
g.queue = lambda pid: 0
g.want = {"cam1"}
t = 5000.0
g.step(t); time.sleep(0.8); g.step(t + 25)
check("keeping up (nothing queued): left alone, lag shown", len(open(T + "/pcalls").read().splitlines()) == 1 and
      g.status()["lag"].get("cam1", 9) < 1, g.status())
g.queue = lambda pid: 2000000                            # 2 MB waiting at 1 Mbit/s = 16 s behind
pid1 = g.procs["cam1"][0].pid
np = len(probes)
g.step(t + 26); time.sleep(0.5)
alive = True
try: os.kill(pid1, 0)
except OSError: alive = False
check("16 s behind: the backlog is dropped and the copy restarts at once", not alive and len(open(T + "/pcalls").read().splitlines()) == 2, LOG2)
check("...without the 'already live on the server' check (that's our own copy)", len(probes) == np)
check("...the event log says why, with the camera's bitrate", any("behind live" in x and "1.0 Mbit/s" in x for _, x in LOG2), LOG2)
check("...the server marker stays up through it", g.ok(t + 26.1) == ["cam1"])
g.step(t + 30)
check("a fresh copy isn't restarted again before it has run 20 s", len(open(T + "/pcalls").read().splitlines()) == 2)
time.sleep(0.5); g.step(t + 50); time.sleep(0.5); g.step(t + 75); time.sleep(0.5)
check("third jump in 10 minutes: a warning to lower the camera's bitrate", any(l == "warn" and "keeps falling behind" in x and
      "Lower the camera" in x for l, x in LOG2), LOG2[-1:])
g.want = set(); g.step(t + 100)
check("stream ends: copy stopped, lag cleared", g.status()["relaying"] == [] and "cam1" not in g.status()["lag"] and g.ok(t + 101) == [])
g.stop_all()

# ---------------- the dashboard: server-side markers, through the bridge's server-only port
import dashboard_server as ds
class Ev:
    def __init__(self): self.items = []
    def add(self, level, text, cat="system"): self.items.append((level, text, cat))
class Stub:
    errors = []
    def can_restart(self): return True
rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); rx.bind(("127.0.0.1", 0)); rx.settimeout(2)
cfg = dict(ds.DEFAULTS, GPS="off", SERVER_HOST="100.64.0.9", SERVER_PORT="8089", MESH_DISCOVERY="off", VIDEO_RELAY="auto",
           VIDEO_RELAY_SEND="127.0.0.1:%d" % rx.getsockname()[1])
c = ds.Collector(cfg, Ev(), alerts=Stub(), control=Stub())
check("VIDEO_RELAY = auto: the server is the COT_URL host, port 8554", c.relay and c.relay.server == "rtsp://100.64.0.9:8554", c.relay and c.relay.server)
check("off by default (it uses uplink data)", ds.DEFAULTS["VIDEO_RELAY"] == "off" and
      ds.Collector(dict(cfg, VIDEO_RELAY="off"), Ev(), alerts=Stub(), control=Stub()).relay is None)
class FakeRelay:
    server = "rtsp://deck:s3cret" + "@100.64.0.9:8554"
    wanted = None
    def set_wanted(self, n): self.wanted = n
    def ok(self, now=None): return ["cam1"]
    def status(self): return {"state": "on", "relaying": ["cam1"], "conflicts": [], "errors": {}}
c.relay = FakeRelay()
c.vmark.send = lambda ip, d: None
c.mtx_conf = lambda: {}
c.people = lambda: [{"cs": "EAGLE 6", "uid": "e6", "lat": 39.55, "lon": -104.95, "ts": time.time(), "ip": "10.41.0.50", "via": "mesh"}]
c.publisher_ip = lambda kind, sid: "10.41.0.50" if sid == "s1" else None
video = {"api_ok": True, "paths": [{"name": "cam1", "ready": True, "source": "rtspSession", "source_id": "s1"}]}
c.video_markers(video, {"iface_ip": "10.41.0.60"}, time.time())
data = rx.recv(65535)
e = ET.fromstring(data)
v = e.find("detail/__video")
check("server-side marker goes to the bridge's server-only port", e.get("uid").endswith("-SVIDEO-cam1"), e.get("uid"))
check("...its link is the server's copy, without the login", v.get("url") == "rtsp://100.64.0.9:8554/cam1" and b"s3cret" not in data, v.get("url"))
check("...at the phone streaming it (matched by its address on the mesh)", "EAGLE 6 at" in e.find("detail/remarks").text and
      abs(float(e.find("point").get("lat")) - (39.55 + 8 / 111320.0)) < 1e-5, e.find("detail/remarks").text)
check("the relay is told which streams are live on the deck", c.relay.wanted == ["cam1"])
st = c.relay_status
check("status for Ops: relaying, server without login, server markers on", st["relaying"] == ["cam1"] and st["server"] == "rtsp://100.64.0.9:8554"
      and st["markers"]["state"] == "on", st)
c2 = ds.Collector(dict(cfg, VIDEO_MARKERS="off"), Ev(), alerts=Stub(), control=Stub()); c2.relay = FakeRelay()
c2.people = c.people; c2.publisher_ip = c.publisher_ip
c2.video_markers(video, {"iface_ip": "10.41.0.60"}, time.time())
check("the relay works with mesh markers turned off too", c2.relay.wanted == ["cam1"] and rx.recv(65535))
snap = ds.mock_status("hop", Ev()); snap["video"]["relay"] = st
check("SETUP: 'Video to the server' line", any(i["title"] == "Video to the server" and i["ok"] for i in ds.setup_checks(dict(ds.DEFAULTS), snap, Stub(), Stub())))

# ---------------- the bridge: what Ops sends to the server never comes back down onto the mesh
import tak_mesh_bridge as tb
async def bridge_part():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    q = asyncio.Queue()
    task = asyncio.create_task(tb.LocalSend({"LOCAL_SEND": "udp://127.0.0.1:%d" % port}, q).run())
    await asyncio.sleep(0.3)
    u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); u.sendto(data, ("127.0.0.1", port)); u.close()
    got = await asyncio.wait_for(q.get(), 2)
    task.cancel()
    return got
tb.Outbox.path = None
got = asyncio.run(bridge_part())
uid = e.get("uid").encode()
check("bridge: the server-side marker is queued for the server", got == data)
check("bridge: and marked so the server's echo is never relayed back onto the mesh", tb.Shared.recent(tb.Shared.mesh_uids, uid))
print("%d/%d passed" % (sum(res), len(res)))
sys.exit(0 if all(res) else 1)
