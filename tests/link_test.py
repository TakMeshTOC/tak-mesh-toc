"""Server link + flow tests against the REAL bridge (fake OTS that answers pings like OTS does, fake mesh):
  L  server link: quiet drop -> exits at once, no CPU burn, reason recorded, restart counted;
     silent dropout (no answers) -> reconnect within LINK_TIMEOUT; a server that never answers pings -> no false
     reconnects; round-trip time reported; pings never leak onto the mesh or into traffic counters
  U  uplink: one report per phone per UP_MIN_INTERVAL, always the newest; chats skip the line; a 30 s stall leaves
     only a small stale backlog; UP_MIN_INTERVAL = 0 forwards everything like before
  F  return path: 100 server people all reach the mesh, in turn; chats from the server are not starved
Usage: python3 link_test.py [package dir] [work dir]"""
import shutil
import json, os, socket, struct, subprocess, sys, threading, time
PKG = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
W = sys.argv[2] if len(sys.argv) > 2 else "/tmp/mmb_link_test"
os.makedirs(W, exist_ok=True)
T = 'time="2026-10-22T18:00:00Z" start="2026-10-22T18:00:00Z" stale="2099-01-01T00:00:00Z"'
res = []
def check(n, c, x=""):
    res.append(bool(c)); print(("PASS  " if c else "FAIL  ") + n + ("" if c else "  " + str(x)[:400]), flush=True)
def wait(cond, t=20, step=0.1):
    end = time.time() + t
    while time.time() < end:
        if cond():
            return True
        time.sleep(step)
    return False


class FakeOTS:
    """Accepts the bridge, splits events, answers t-x-c-t pings by echoing them (what OTS 1.7 does)."""
    def __init__(self, pong=True, rcvbuf=None):
        self.s = socket.socket(); self.s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if rcvbuf:      # small window, set before listen so it is negotiated: little data "in flight", like a stalled cell link
            self.s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
        self.rcvbuf = rcvbuf
        self.s.bind(("127.0.0.1", 0)); self.s.listen(5); self.port = self.s.getsockname()[1]
        self.pong, self.conns, self.events, self.lock = pong, [], [], threading.Lock()
        self.paused = threading.Event()
        threading.Thread(target=self._acc, daemon=True).start()
    def _acc(self):
        while True:
            try:
                c, _ = self.s.accept()
            except OSError:
                return
            if not self.rcvbuf:
                c.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 65536)
            self.conns.append(c); threading.Thread(target=self._rd, args=(c,), daemon=True).start()
    def _rd(self, c):
        buf = b""
        while True:
            while self.paused.is_set():
                time.sleep(0.05)
            try:
                d = c.recv(65536)
            except OSError:
                return
            if not d:
                return
            buf += d
            while b"</event>" in buf:
                ev, _, buf = buf.partition(b"</event>"); ev += b"</event>"
                if b'type="t-x-c-t"' in ev:
                    if self.pong and not self.paused.is_set():
                        try:
                            c.sendall(ev)
                        except OSError:
                            pass
                    with self.lock:
                        self.events.append((time.time(), ev))
                    continue
                with self.lock:
                    self.events.append((time.time(), ev))
    def push(self, data):
        for c in list(self.conns):
            try:
                c.sendall(data)
            except OSError:
                pass
    def drop_all(self):
        for c in list(self.conns):
            try:
                c.shutdown(socket.SHUT_RDWR); c.close()
            except OSError:
                pass
        self.conns.clear()
    def take(self):
        with self.lock:
            e = self.events[:]; self.events.clear(); return e
    def count(self, needle):
        with self.lock:
            return sum(1 for _, e in self.events if needle in e)


mc = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
mc.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton("127.0.0.1"))
mc.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
heard, hl = [], threading.Lock()
def listen(port, grp):
    ml = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    ml.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); ml.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    ml.bind(("", port))
    ml.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, struct.pack("4s4s", socket.inet_aton(grp), socket.inet_aton("127.0.0.1")))
    while True:
        d, _ = ml.recvfrom(70000)
        with hl:
            heard.append((time.time(), d))
threading.Thread(target=listen, args=(6969, "239.2.3.1"), daemon=True).start()
threading.Thread(target=listen, args=(17012, "224.10.10.1"), daemon=True).start()

def sa(uid, cs, seq, ep="10.41.0.50:4242:tcp"):
    return ('<event version="2.0" uid="%s" type="a-f-G-U-C" how="m-g" %s><point lat="38.1" lon="-104.1" hae="1" ce="9" le="9"/>'
            '<detail><contact callsign="%s" endpoint="%s"/><remarks>seq=%d t=%.6f</remarks></detail></event>' % (uid, T, cs, ep, seq, time.time())).encode()
def chat(uid, cs, text):
    mid = "m-%s-%s" % (uid, text)
    return ('<event version="2.0" uid="GeoChat.%s.All Chat Rooms.%s" type="b-t-f" how="h-g-i-g-o" %s><point lat="38" lon="-104" hae="0" ce="9" le="9"/>'
            '<detail><__chat parent="RootContactGroup" groupOwner="false" messageId="%s" chatroom="All Chat Rooms" id="All Chat Rooms" senderCallsign="%s">'
            '<chatgrp uid0="%s" uid1="All Chat Rooms" id="All Chat Rooms"/></__chat><link uid="%s" type="a-f-G-U-C" relation="p-p"/>'
            '<remarks source="BAO.F.ATAK.%s" to="All Chat Rooms" time="2026-10-22T18:00:00Z">%s t=%.6f</remarks></detail></event>'
            % (uid, mid, T, mid, cs, uid, uid, uid, text, time.time())).encode()
def stamp(ev):
    i = ev.find(b" t=")
    return float(ev[i + 3:ev.find(b"<", i)]) if i >= 0 else None
def seq_of(ev):
    i = ev.find(b"seq=")
    return int(ev[i + 4:ev.find(b" ", i)]) if i >= 0 else None
def cpu_s(pid):
    f = open("/proc/%d/stat" % pid).read().split(")")[-1].split()
    return (int(f[11]) + int(f[12])) / os.sysconf("SC_CLK_TCK")


class Bridge:
    def __init__(self, srv, tag, extra=""):
        self.w = W + "/" + tag; os.makedirs(self.w, exist_ok=True)
        for f in os.listdir(self.w):
            p = os.path.join(self.w, f)
            shutil.rmtree(p) if os.path.isdir(p) else os.remove(p)    # the bridge's file_cache folder too
        self.ini = self.w + "/deck.ini"; self.status = self.w + "/status.json"; self.logf = self.w + "/b.log"
        open(self.ini, "w").write(f"""[deck]
CALLSIGN = DECK
UID = DECK-linktest
MESH_ADDR = 127.0.0.1
GPS = off
[bridge]
COT_URL = tcp://127.0.0.1:{srv.port}
STATUS_FILE = {self.status}
RETURN_PATH = 1
MESH_CHAT_URL = udp+ro://224.10.10.1:17012
{extra}
""")
        self.start()
    def start(self):
        self.p = subprocess.Popen([sys.executable, "-S", PKG + "/bridge/tak_mesh_bridge.py", self.ini], stdout=open(self.logf, "a"),
                                  stderr=subprocess.STDOUT, env=dict(os.environ, PYTHONWARNINGS="ignore"))
        wait(lambda: os.path.exists(self.logf) and open(self.logf).read().count("Listening on udp+ro://239.2.3.1:6969") >= self.n_starts(), 25)
        time.sleep(0.5)
    def n_starts(self):
        return getattr(self, "_n", 0)
    def restart(self):
        self._n = self.n_starts() + 1
        self.start()
    def alive(self):
        return self.p.poll() is None
    def st(self):
        try:
            return json.load(open(self.status))
        except (OSError, ValueError):
            return {}
    def stop(self):
        if self.alive():
            self.p.terminate()
            try:
                self.p.wait(5)
            except Exception:
                self.p.kill()
    def log(self):
        return open(self.logf).read()

# ===================================================================== L: server link
print("== L. server link", flush=True)
srv = FakeOTS(); b = Bridge(srv, "L", "PING_INTERVAL = 3\nLINK_TIMEOUT = 20")
b._n = 1
ok = wait(lambda: (b.st().get("link") or {}).get("answers_pings") is True, 15)
lk = b.st().get("link") or {}
check("L1 bridge pings the server and sees the answers", ok, lk)
check("L2 round-trip time reported (%s ms)" % lk.get("rtt_ms"), isinstance(lk.get("rtt_ms"), (int, float)) and 0 <= lk["rtt_ms"] < 500, lk)
check("L3 link state 'up' with a connected-since time", lk.get("state") == "up" and lk.get("since", 0) > 0, lk)
time.sleep(4)
with hl:
    leak = [d for _, d in heard if b"t-x-c-t" in d]
check("L4 pings never go onto the mesh", not leak, leak[:1])
check("L5 pings/answers are not counted as server traffic", (b.st().get("server") or {}).get("rx_events") == 0, b.st().get("server"))
c0 = cpu_s(b.p.pid); srv.drop_all(); t0 = time.time()
gone = wait(lambda: not b.alive(), 10)
check("L6 quiet mesh, server drops the link: bridge ends the connection at once (%.1f s, not up to 5 min)" % (time.time() - t0), gone and time.time() - t0 < 3)
lk = b.st().get("link") or {}
check("L7 ...status says down and why", lk.get("state") == "down" and "closed" in lk.get("reason", ""), lk)
b.restart()
lk = b.st().get("link") or {}
check("L8 after the restart: reconnect counted (starts 2) and the previous reason kept", lk.get("starts") == 2 and "closed" in lk.get("prev_reason", ""), lk)
# silent dropout: the server keeps the socket open but stops reading and answering
wait(lambda: (b.st().get("link") or {}).get("answers_pings") is True, 15)
srv.paused.set(); t0 = time.time()
for i in range(30):
    mc.sendto(sa("ANDROID-q%02d" % (i % 3), "Q", i), ("239.2.3.1", 6969)); time.sleep(0.2)
gone = wait(lambda: not b.alive(), 40)
dt = time.time() - t0
check("L9 silent dropout: bridge reconnects within LINK_TIMEOUT (%.0f s; timeout 20 s)" % dt, gone and dt < 28, b.log()[-400:])
check("L10 ...and says why", "no answer from the server" in (b.st().get("link") or {}).get("reason", "") or "no answer" in b.log(), (b.st().get("link") or {}))
srv.paused.clear(); b.stop()
# a server that never answers pings (not OTS): no false alarms
srv2 = FakeOTS(pong=False); b2 = Bridge(srv2, "L2", "PING_INTERVAL = 2\nLINK_TIMEOUT = 20")
time.sleep(28)
check("L11 server that never answers pings: no false reconnect in 28 s, watchdog off", b2.alive() and (b2.st().get("link") or {}).get("answers_pings") is False, b2.log()[-300:])
check("L12 ...and the log says so once", b2.log().count("does not answer pings") == 1)
b2.stop()

# ===================================================================== U: uplink
print("== U. uplink", flush=True)
srv = FakeOTS(); b = Bridge(srv, "U", "UP_MIN_INTERVAL = 3\nPING_INTERVAL = 0")
srv.take(); last = {}
t0 = time.time(); k = 0
chat_sent = []
while time.time() - t0 < 12:
    for i in range(10):
        k += 1; mc.sendto(sa("ANDROID-u%02d" % i, "U-%02d" % i, k), ("239.2.3.1", 6969)); last["ANDROID-u%02d" % i] = k
    if int(time.time() - t0) in (4, 8) and len(chat_sent) < 2 and (not chat_sent or time.time() - chat_sent[-1][1] > 2):
        txt = "uchat%d" % len(chat_sent); chat_sent.append((txt, time.time())); mc.sendto(chat("ANDROID-u00", "U-00", txt), ("224.10.10.1", 17012))
    time.sleep(0.5)
time.sleep(4)
ev = srv.take()
per = {}
for ts, e in ev:
    if b"ANDROID-u" in e and b'type="a-f' in e and b"seq=" in e:
        u = e.split(b'uid="')[1].split(b'"')[0].decode(); per.setdefault(u, []).append(seq_of(e))
counts = sorted(len(v) for v in per.values())
check("U1 10 phones x 24 reports each -> 4-6 per phone reach the server (got %s)" % counts, len(per) == 10 and all(4 <= c <= 6 for c in counts), counts)
check("U2 the last one per phone is its newest report", all(max(per[u]) == last[u] for u in per), {u: (max(v), last[u]) for u, v in per.items()})
lat = []
for txt, ts in chat_sent:
    arr = [t for t, e in ev if txt.encode() in e]
    lat.append(arr[0] - ts if arr else 99)
check("U3 chats skip the line (%.2f s)" % max(lat), max(lat) < 0.5, lat)
st = b.st().get("uplink") or {}
check("U4 status shows what was held back (coalesced %s)" % st.get("coalesced"), st.get("coalesced", 0) > 100, st)
# stall: a fresh bridge on a link with a small window (what a stalled cell path looks like)
b.stop(); srv = FakeOTS(rcvbuf=4096); b = Bridge(srv, "U2", "UP_MIN_INTERVAL = 3\nPING_INTERVAL = 0")
srv.take(); srv.paused.set(); t0 = time.time(); k = 0
stall_chat = None
while time.time() - t0 < 30:
    for i in range(25):
        k += 1; mc.sendto(sa("ANDROID-s%02d" % i, "S-%02d" % i, k), ("239.2.3.1", 6969))
    if stall_chat is None and time.time() - t0 > 15:
        stall_chat = "stallchat"; mc.sendto(chat("ANDROID-s00", "S-00", stall_chat), ("224.10.10.1", 17012))
    time.sleep(2)
resume = time.time(); srv.paused.clear()
time.sleep(8)
ev = srv.take()
pos = [(ts, e) for ts, e in ev if b"ANDROID-s" in e and b'type="a-f' in e and b"seq=" in e]
old = [e for ts, e in pos if ts - stamp(e) > 5]
check("U5 30 s stall, 25 phones: after it the server gets a small stale backlog (%d old reports of %d sent; 1.0.4: all 375)" % (len(old), k), len(old) < 100, len(old))
newest = {}
for ts, e in pos:
    newest[e.split(b'uid="')[1].split(b'"')[0]] = e
fresh = [u for u, e in newest.items() if seq_of(e) > k - 25 * 2]
check("U6 ...and every phone's latest report (from the last ~4 s) arrives within 8 s (%d of 25)" % len(fresh), len(fresh) == 25)
check("U7 the chat sent during the stall arrives", any(stall_chat.encode() in e for _, e in ev))
check("U8 bridge stayed up", b.alive())
b.stop()
# off switch
srv = FakeOTS(); b = Bridge(srv, "U0", "UP_MIN_INTERVAL = 0\nPING_INTERVAL = 0")
srv.take()
for k in range(40):
    mc.sendto(sa("ANDROID-z", "Z", k), ("239.2.3.1", 6969)); time.sleep(0.05)
time.sleep(2)
check("U9 UP_MIN_INTERVAL = 0: every report forwarded, as before (%d/40)" % srv.count(b"ANDROID-z"), srv.count(b"ANDROID-z") == 40)
b.stop()

# ===================================================================== F: fair return path
print("== F. return path", flush=True)
srv = FakeOTS(); b = Bridge(srv, "F", "PING_INTERVAL = 0\nRETURN_MAX_PER_SEC = 5")
with hl:
    heard.clear()
t0 = time.time(); n = 0; chat_t = None
while time.time() - t0 < 40:
    tick = time.time()
    for j in range(100):
        n += 1; srv.push(sa("ANDROID-f%03d" % j, "F-%03d" % j, n, "*:-1:stcp"))
    if chat_t is None and time.time() - t0 > 15:
        chat_t = time.time(); srv.push(chat("ANDROID-f000", "F-000", "fairchat"))
    time.sleep(max(0, 10 - (time.time() - tick)))
time.sleep(3)
with hl:
    got = [(ts, d) for ts, d in heard if d.startswith(b'<event version="2.0" uid="ANDROID-f') and b"<contact" in d]
    ch = [ts for ts, d in heard if b"fairchat" in d]
people = {d.split(b'uid="')[1].split(b'"')[0] for _, d in got}
check("F1 100 server people, 5/s cap: all of them reach the mesh within 40 s (%d)" % len(people), len(people) == 100, len(people))
check("F2 the cap still holds (%d sent in ~43 s)" % len(got), len(got) <= 5 * 44, len(got))
check("F3 a server chat during the flood reaches the mesh quickly (%.2f s)" % ((ch[0] - chat_t) if ch else 99), ch and ch[0] - chat_t < 1.5)
newest_ok = True
for u in list(people)[:20]:
    mine = sorted((ts, seq_of(d)) for ts, d in got if b'uid="%s"' % u in d)
    newest_ok = newest_ok and all(b2 > a2 for (_, a2), (_, b2) in zip(mine, mine[1:]))
check("F4 each person's updates on the mesh only move forward (newest wins)", newest_ok)
b.stop()

print("\n%d/%d passed" % (sum(res), len(res)))
sys.exit(0 if all(res) else 1)
