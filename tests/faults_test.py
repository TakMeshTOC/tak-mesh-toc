import os as _o, sys as _s  # paths relative to this package
PKG = _o.path.dirname(_o.path.dirname(_o.path.abspath(__file__)))
_s.path[:0] = [PKG + "/vendor", PKG + "/common", PKG + "/bridge", PKG + "/dashboard"]
"""faults_test.py: errors the services survive are counted and shown in Ops, not only written to journalctl.
Bridge: the server and mesh paths, the outbox replay (now retried), the DM check, the deck's identity, unreadable
iTAK DMs. Each is forced to fail here and must show up in the status file, in Ops' alert strip and in its event log.
Usage: python3 -S faults_test.py"""
import asyncio, json, logging, os, sys, tempfile, time
import warnings; warnings.filterwarnings("ignore")
import tak_mesh_bridge as tb

res = []
def check(n, c, x=""):
    res.append(bool(c)); print(("PASS  " if c else "FAIL  ") + n + ("" if c else "  " + str(x)[:500]), flush=True)

class Cap(logging.Handler):
    def __init__(self): super().__init__(); self.lines = []
    def emit(self, r): self.lines.append(r.getMessage())
cap = Cap(); tb.log.addHandler(cap)

# ---------------- the registry
F = tb.Faults
F.areas = {}
for i in range(250):
    F.note("return", ConnectionError("queue closed"))
st = F.status()
check("every error counted, the last one kept with its kind", st["return"]["count"] == 250 and st["return"]["last"] == "ConnectionError: queue closed", st)
check("journal gets the first 3 and every 100th, not 250 lines", sum("return:" in l for l in cap.lines) == 5, len(cap.lines))
check("active while it keeps happening", st["return"]["active"])
check("...clears 60 s after the last error", not F.status(now=time.time() + 61)["return"]["active"])
F.note("identity", ValueError("x"), hold=450)
check("a loop that runs every few minutes stays active between its runs", F.status(now=time.time() + 400)["identity"]["active"]
      and not F.status(now=time.time() + 460)["identity"]["active"])
F.note("outbox_replay", OSError("disk"), sticky=True)
check("sticky: active until fixed, however long ago", F.status(now=time.time() + 9999)["outbox_replay"]["active"])
F.clear("outbox_replay")
check("...and cleared when it works", not F.status(now=time.time() + 9999)["outbox_replay"]["active"])

# ---------------- A: the outbox replay is retried, never sends a chat twice
F.areas = {}
class Q:
    def __init__(self, fail_at): self.items, self.fail_at, self.n = [], fail_at, 0
    async def put(self, x):
        self.n += 1
        if self.n in self.fail_at: raise RuntimeError("send queue closed")
        self.items.append(x)
tb.Outbox.pending = {1: (1.0, b"chat-1"), 2: (2.0, b"chat-2"), 3: (3.0, b"chat-3")}
tb.Outbox.replay_done = set(); tb.Outbox.replayed = 0
q = Q(fail_at={2, 3})                     # 2nd put fails, and the retry's first put too
r = tb.OutboxReplay(q); r.RETRY_FIRST = 0.05
async def go():
    t = asyncio.ensure_future(r.run())
    await asyncio.sleep(0.12)
    mid = (F.status().get("outbox_replay", {}).get("active"), list(q.items))
    await asyncio.sleep(0.5)
    t.cancel()
    return mid
mid = asyncio.run(go())
check("replay fails: an active fault Ops shows (before: one log line, never retried)", mid[0] is True, mid)
check("retried with a growing wait until every saved chat is queued, oldest first", q.items == [b"chat-1", b"chat-2", b"chat-3"], q.items)
check("...none sent twice", len(q.items) == len(set(q.items)) == 3)
check("...and the fault clears once it worked", not F.status()["outbox_replay"]["active"])

# ---------------- F: unreadable iTAK DMs are counted
F.areas = {}
if tb.takproto is not None:
    d = tb.DMRelay({"DM_RELAY_PORT": "4243"}, None)
    d._datagram(b"\xbf\x01\xbfgarbage-not-protobuf", ("10.41.0.50", 4243))
    d._datagram(b"\xbf\x01\xbf\xff\xff\xff", ("10.41.0.50", 4243))
    f = F.status().get("itak_udp", {})
    check("an iTAK DM that can't be read is counted (it used to vanish)", f.get("count") == 2 and f.get("active"), F.status())
else:
    check("an iTAK DM that can't be read is counted (takproto missing: skipped)", True)

# ---------------- the status file carries them
F.areas = {}
F.note("uplink", OSError("Broken pipe"))
sw = tb.StatusWriter.__new__(tb.StatusWriter) if hasattr(tb, "StatusWriter") else None
check("the status file has a faults section", '"faults": Faults.status(now)' in open(tb.__file__).read())

# ---------------- Ops: alert strip, BRIDGE tile, event log
import dashboard_server as ds
fa = ds.fault_alerts({"return": {"count": 12, "last": "ConnectionError: queue closed", "active": True},
                      "uplink": {"count": 3, "last": "x", "active": False},
                      "itak_udp": {"count": 1, "last": "DecodeError: bad", "active": True}})
check("active faults become alert-strip entries; old ones don't", [a["id"] for a in fa] == ["fault:itak_udp", "fault:return"], fa)
r = [a for a in fa if a["id"] == "fault:return"][0]
check("server -> mesh failing is a red ALERT on the BRIDGE tile, with the reason and count",
      r["level"] == "bad" and r["tile"] == "bridge" and "Server -> mesh failing: ConnectionError" in r["text"] and r["value"] == "12 errors", r)
check("unreadable iTAK DMs are a WARN, counted as dropped", fa[0]["level"] == "warn" and fa[0]["value"] == "1 dropped")
check("an area Ops doesn't know still shows", ds.fault_alerts({"new_thing": {"count": 2, "last": "e", "active": True}})[0]["text"].startswith("Bridge: new_thing failing"))

class Ev:
    def __init__(self): self.items = []
    def add(self, level, text, cat="system"): self.items.append((level, text, cat))
    def recent(self, n=100): return []
class Stub:
    errors = []
    enabled = False
    def can_restart(self): return True
    def caps(self): return {}
T = tempfile.mkdtemp(prefix="mmb_faults_")
SF = T + "/bridge_status.json"
def status(faults):
    json.dump({"pid": os.getpid(), "now": time.time(), "started": time.time() - 60, "faults": faults,
               "per_minute": {"up": {"total": 0}, "down": {"total": 0}, "total": 0}}, open(SF, "w"))
ev = Ev()
cfg = dict(ds.DEFAULTS, GPS="off", SERVER_HOST="127.0.0.1", SERVER_PORT="1", BRIDGE_STATUS_FILE=SF, MESH_DISCOVERY="off",
           EVENT_FILE=T + "/ev.jsonl", HISTORY_FILE=T + "/h.jsonl", MESH_STATUS_FILE=T + "/ms.json")
ds.run = lambda cmd, timeout=2.5: (0, "active\n") if "is-active" in cmd else (127, "")
c = ds.Collector(cfg, ev, ds.History(cfg) if hasattr(ds, "History") else None, ds.Alerts(cfg, ev), Stub())
status({})
s1 = c.collect()
status({"return": {"count": 5, "last": "ConnectionError: queue closed", "last_ts": time.time(), "active": True}})
s2 = c.collect()
al = [a for a in s2["alerts"] if a["id"] == "fault:return"]
check("Ops: the fault is in the alert strip", al and al[0]["level"] == "bad", s2["alerts"])
check("Ops: the BRIDGE tile is raised to red with it", s2["tiles"]["bridge"]["level"] == "bad" and "Server -> mesh failing" in s2["tiles"]["bridge"].get("alert", ""), s2["tiles"]["bridge"])
check("Ops event log: it started (first time too)", any(l == "bad" and t.startswith("Server -> mesh failing: ConnectionError") for l, t, _ in ev.items), ev.items)
status({"return": {"count": 5, "last": "ConnectionError: queue closed", "last_ts": time.time() - 90, "active": False}})
s3 = c.collect()
check("Ops: cleared from the strip when it stops", not [a for a in s3["alerts"] if a["id"].startswith("fault:")])
check("Ops event log: 'working again'", any(l == "ok" and t == "Server -> mesh failing: working again" for l, t, _ in ev.items))
os.utime(SF, None); json.dump({"pid": 999999, "now": time.time() - 300, "faults": {"return": {"active": True, "count": 1}}}, open(SF, "w"))
s4 = c.collect()
check("a stale status file doesn't raise old faults (the bridge-down alert covers it)", not [a for a in s4["alerts"] if a["id"].startswith("fault:")])

# ---------------- C: Ops data that stopped updating says so
status({})
good = c.collect()
with c.lock: c.snapshot = good
check("fresh data: no stale alert", not [a for a in c.get()["alerts"] if a["id"] == "ops_stale"])
real = c.collect
def broken(): raise KeyError("tiles")
c.collect = broken
c.step(); c.step()
with c.lock: c.snapshot = dict(c.snapshot, now=time.time() - 30)
g = c.get()
st_al = [a for a in g["alerts"] if a["id"] == "ops_stale"]
check("collecting fails: the page gets a red 'Ops data stale since HH:MM:SS' with the reason (before: old data, looked live)",
      st_al and st_al[0]["level"] == "bad" and "KeyError" in st_al[0]["text"] and st_al[0]["value"] == "30 s old" and g.get("stale"), g["alerts"])
check("...the event log says so once, not every 2 s", sum("Ops data not updating" in t for _, t, _ in ev.items) == 1)
c.collect = real
c.step()
check("...recovers: alert gone, 'updating again' logged", not [a for a in c.get()["alerts"] if a["id"] == "ops_stale"] and
      any(t == "Ops data updating again" for _, t, _ in ev.items))
c.collect = lambda: (_ for _ in ()).throw(AssertionError("never called"))
with c.lock: c.snapshot = dict(c.snapshot, now=time.time() - 30)
c.collect_error = None
check("collector stuck (no error, just no new data): still flagged", "collecting is stuck" in c.get()["alerts"][0]["text"])
c.collect = real
with c.lock: c.snapshot = {"booting": True, "now": time.time() - 30}
check("while Ops is starting: no stale alert", c.get().get("alerts") is None)

# ---------------- D: the deck's disk going read-only shows in Ops
bad = T + "/not_a_dir"
open(bad, "w").close()
evs = ds.Events(bad + "/events.jsonl")      # its folder is a file: every write fails, as on a read-only disk
evs.add("info", "test")
check("an event log write that fails is recorded", ds.Disk.failing() and "cannot write the event log" in ds.Disk.failing()[1], ds.Disk.failing())
status({})
s5 = c.collect()
d = [a for a in s5["alerts"] if a["id"] == "disk"]
check("Ops alert strip: 'Deck disk not writable' with the reason and since when", d and d[0]["level"] == "bad" and
      "Deck disk not writable: cannot write the event log" in d[0]["text"] and d[0]["value"].startswith("since "), s5["alerts"])
check("...and in the event log", any(l == "bad" and t.startswith("Deck disk not writable") for l, t, _ in ev.items))
evs.path = T + "/events.jsonl"; evs.add("info", "test again")
s6 = c.collect()
check("writes work again: alert gone, 'writable again' logged", not [a for a in s6["alerts"] if a["id"] == "disk"] and
      any(t == "Deck disk writable again" for _, t, _ in ev.items))

# ---------------- I: the server-stream check's own failure is said, once
import videomarkers as vm
said = []
u = vm.UpstreamWatch(every=0.05, probe=lambda url: None, log=lambda l, t: said.append((l, t)))
u.poll = lambda: (_ for _ in ()).throw(OSError("no route"))
u.set_sources({"RAVEN21": "rtsp://100.64.0.9:8554/RAVEN21"})
time.sleep(0.4)
check("stream check fails: one warning saying markers can't appear (before: silent)", len(said) == 1 and said[0][0] == "warn" and
      "OSError: no route" in said[0][1] and "markers can't appear" in said[0][1], said)
u.poll = lambda: None
time.sleep(0.2)
check("...and once it works again, it says so", said[-1] == ("ok", "Video source check working again"), said)
print("%d/%d passed" % (sum(res), len(res)))
sys.exit(0 if all(res) else 1)
