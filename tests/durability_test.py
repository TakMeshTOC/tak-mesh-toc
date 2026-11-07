import os as _o, sys as _s  # paths relative to this package
PKG = _o.path.dirname(_o.path.dirname(_o.path.abspath(__file__)))
_s.path[:0] = [PKG + "/vendor", PKG + "/common", PKG + "/bridge", PKG + "/chat", PKG + "/dashboard"]
"""durability_test.py: the bridge's outbox is synced to disk, says honestly what the server confirmed, reports
failed disk writes, and the per-phone tables stay bounded under a flood of made-up UIDs.
Run it with python3 -S (as run_all.sh does) so only the bundled libraries are used."""
import asyncio, configparser, os, shutil, sys, tempfile, time, warnings
warnings.filterwarnings("ignore")
import tak_mesh_bridge as B

res = []
def check(n, c, x=""):
    res.append(bool(c)); print(("PASS  " if c else "FAIL  ") + n + ("" if c else "  " + str(x)[:300]))

W = tempfile.mkdtemp(prefix="mmb_durability_")
def cfg(**kw):
    cp = configparser.ConfigParser(); cp.read_dict({"bridge": dict({"STATUS_FILE": W + "/st.json"}, **kw)}); return cp["bridge"]
CHAT = b'<event version="2.0" uid="GeoChat.A.All Chat Rooms.%d" type="b-t-f" how="h-g-i-g-o"><detail/></event>'

# ---- 1. every kept chat is fsync'd before it is sent; the compacted file and its folder too
synced = []
real_fsync = os.fsync
os.fsync = lambda fd: synced.append(fd) or real_fsync(fd)
B.Outbox.setup(cfg())
synced.clear()
B.Outbox.keep(CHAT % 1)
check("a kept chat is synced to disk (fsync) before it is sent", len(synced) >= 1, synced)
synced.clear()
B.Outbox._compact()
check("compacting syncs the new file and its folder (2 fsyncs)", len(synced) >= 2, synced)
os.fsync = real_fsync
lines = open(B.Outbox.path).read().splitlines()
check("the chat is in the file", len(lines) == 1 and '"i": 1' in lines[0], lines)

# ---- 2. a failed disk write is counted and shown, and the chat is still sent
bad = W + "/no_such_dir/outbox.jsonl"
B.Outbox.setup(cfg(OUTBOX_FILE=bad))
B.Outbox.keep(CHAT % 2)
st = B.Outbox.status()
check("disk write failure is counted, with the reason", st["write_errors"] >= 1 and st["last_write_error"], st)
check("...and the chat is still on its way (in memory)", st["pending"] == 1, st)

# ---- 3. delivery: confirmed by a ping answer vs written only
B.Outbox.setup(cfg())
B.Link.interval, B.Link.pong_seen, B.Link.pings = 20, True, 1
B.Link.ping_uid = b"DECK-x-ping"
B.Outbox.keep(CHAT % 3); B.Outbox.sent(CHAT % 3)
B.Outbox.sent(b'<event uid="DECK-x-ping" atb-seq=7/>')
B.Outbox.confirm(7)
st = B.Outbox.status()
check("pings answered: counted as CONFIRMED", (st["confirmed"], st["sent_unconfirmed"], st["can_confirm"]) == (1, 0, True), st)
B.Link.interval = 0
B.Outbox.keep(CHAT % 4); B.Outbox.sent(CHAT % 4)
st = B.Outbox.status()
check("pings off: written counts as SENT, UNCONFIRMED (not confirmed)", (st["confirmed"], st["sent_unconfirmed"], st["can_confirm"]) == (1, 1, False), st)
B.Link.interval, B.Link.pong_seen, B.Link.pings = 20, False, 3
B.Outbox.keep(CHAT % 5); B.Outbox.sent(CHAT % 5)
st = B.Outbox.status()
check("server never answers pings: also SENT, UNCONFIRMED", st["sent_unconfirmed"] == 2 and st["can_confirm"] is False, st)
B.Link.interval, B.Link.pong_seen, B.Link.pings = 20, True, 1
B.Outbox.keep(CHAT % 6); B.Outbox.sent(CHAT % 6)
B.Outbox.no_pings()
check("no_pings() fallback is counted as unconfirmed too", B.Outbox.status()["sent_unconfirmed"] == 3)

# ---- 4. Ops shows it
import dashboard_server as ds
v = ds.outbox_view(dict(B.Outbox.status(), can_confirm=False), time.time())
check("Ops view carries confirmed / unconfirmed / can_confirm", v["sent_unconfirmed"] == 3 and v["can_confirm"] is False, v)
v = ds.outbox_view({"enabled": True, "write_errors": 2, "last_write_error": "No space left on device"}, time.time())
check("Ops view carries disk write failures", v["write_errors"] == 2 and "space" in v["last_write_error"], v)
js = open(PKG + "/dashboard/app.js").read()
check("Ops page shows 'not confirmed' and 'NOT SAVED TO DISK'", "not confirmed</b>" in js and "NOT SAVED TO DISK" in js)

# ---- 4b. while running (not only at a restart), a chat waiting over OUTBOX_HOURS for the server is dropped
B.Outbox.setup(cfg(OUTBOX_HOURS="1"))
B.Outbox.keep(CHAT % 21); B.Outbox.keep(CHAT % 22)
old_i = B.Outbox.by_data[CHAT % 21]
t, d = B.Outbox.pending[old_i]; B.Outbox.pending[old_i] = (t - 2 * 3600, d)     # pretend it waited 2 h
e0 = B.Outbox.expired
B.Outbox.expire()
check("running bridge drops a chat older than OUTBOX_HOURS, keeps the fresh one",
      old_i not in B.Outbox.pending and (CHAT % 22) in B.Outbox.by_data and B.Outbox.expired == e0 + 1, B.Outbox.status())
B.Outbox.setup(cfg(OUTBOX_HOURS="1"))
check("the dropped chat is gone from the file too (not resent after a restart)",
      (CHAT % 21) not in B.Outbox.by_data and (CHAT % 22) in B.Outbox.by_data)
src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bridge", "tak_mesh_bridge.py")).read()
check("expiry runs with every status write", "Outbox.expire()" in src.split("def snapshot(self):", 1)[1][:300])

# ---- 5. UID flood: tables stay under the ceiling, phones already known keep their place
table = {}
B.Shared.mark(table, "REAL-PHONE")
t0 = time.time()
for i in range(50000):
    B.Shared.mark(table, "FAKE-%d" % i)
    if i % 1000 == 0:
        B.Shared.mark(table, "REAL-PHONE")          # a real phone keeps talking during the flood
took = time.time() - t0
check("50,000 made-up UIDs: the mesh table stays at or under %d (%d)" % (B.MAX_UIDS, len(table)), len(table) <= B.MAX_UIDS, len(table))
check("...a phone that keeps talking is kept", "REAL-PHONE" in table)
check("...and it stays fast (%.2f s for 50,000)" % took, took < 10, took)
check("...the drops are counted", B.Stats.uid_capped > 0)

up = B.Uplink(cfg(UP_MIN_INTERVAL="3"), asyncio.Queue())
pos = b'<event uid="%s" type="a-f-G-U-C"><detail><contact endpoint="10.0.0.1:4242:tcp"/></detail></event>'
up.put(pos % b"REAL", b"REAL", b"a-f-G-U-C")
for i in range(30000):
    u = b"FAKE-%d" % i
    up.put(pos % u, u, b"a-f-G-U-C")
check("30,000 made-up UIDs: positions waiting stay at or under %d (%d)" % (B.MAX_UIDS, len(up.pend)), len(up.pend) <= B.MAX_UIDS, len(up.pend))
check("...the real phone's position is still waiting to go", b"REAL" in up.pend)
up.put(b'<event uid="c" type="b-t-f"><detail/></event>', b"c", b"b-t-f")
check("...and a chat still goes first", up._next().startswith(b'<event uid="c"'))
for i in range(15000):
    up.last[b"L%d" % i] = time.monotonic() - 1
up.pend[b"X"] = b"x"; up.ready[b"X"] = None; up.cooling.clear(); up.prio.clear()
while up.ready and len(up.last) <= 15000:
    up._next()
check("the 'last sent' table is capped too (%d)" % len(up.last), len(up.last) <= B.MAX_UIDS, len(up.last))

import deck_chat as dc
src = open(PKG + "/chat/deck_chat.py").read()
check("Chat's on-net list has a ceiling too", "len(self.roster) > 2000" in src)
unit = open(PKG + "/install.sh").read()
check("every service gets a memory ceiling (MemoryMax)", "MemoryMax=400M" in unit)

# ---- 6. Ops' own files (event log, trend history): a failure is reported once in the service log, never fatal
import io
err, real_err = io.StringIO(), sys.stderr
sys.stderr = err
ev = ds.Events(W + "/no_such_dir/ev.jsonl")
ev.add("ok", "one"); ev.add("ok", "two")
sys.stderr = real_err
check("event log on an unwritable path: events still shown on the page", [e["text"] for e in ev.recent()] == ["two", "one"])
check("...and the failure is reported once in the service log", err.getvalue().count("cannot write the event log") == 1, err.getvalue())
open(W + "/h.jsonl", "w").write('{"m": 1, "b": 0, "s": 1, "v": 0, "x": 2}\n{"m": 2, "b"')
h = ds.History(W + "/h.jsonl")
check("trend history with a half-written last line (power cut): the good lines load", h.minutes == {1: {"b": 0, "s": 1, "v": 0, "x": 2}}, h.minutes)

shutil.rmtree(W, ignore_errors=True)
print("%d/%d passed" % (sum(res), len(res)))
sys.exit(0 if all(res) else 1)
