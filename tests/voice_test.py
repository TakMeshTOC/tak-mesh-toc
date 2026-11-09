"""voice_test.py - manet-voice against a real Mumble server, several talk groups at once.

  fake OpenMANET nodes : RTP/Opus PT 111 (and VX-style PT 0), 20 ms frames, multicast on loopback
  real Mumble server   : mumble-server on 127.0.0.1:64739 with a server password, channels TG1 + TG2
  Mumble users         : alice in TG1, bob in TG2 (our own client, as the iPhone/desktop Mumble apps would)
Skipped (exit 0) when mumble-server or libopus is not installed.
argv: [package dir]
"""
import json, math, os, shutil, socket, struct, subprocess, sys, tempfile, threading, time

PKG = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), ".."))
sys.path[:0] = [PKG + "/voice", PKG + "/common"]
import opuscodec as oc
if oc.LOAD_ERROR or not shutil.which("mumble-server"):
    print("SKIP  voice_test: needs mumble-server and libopus (%s)" % (oc.LOAD_ERROR or "no mumble-server"))
    sys.exit(0)
import manet_voice as mv
from mumbleclient import MumbleClient

res = []
def check(n, c, x=""):
    res.append(bool(c)); print(("PASS  " if c else "FAIL  ") + n + ("" if c else "  " + str(x)[:300])); sys.stdout.flush()

def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0)); return s.getsockname()[1]
GROUP, MPORT, SPW, SUPW = "239.255.41.9", free_port(), "Mumble", "su-test-pw"
W = tempfile.mkdtemp(prefix="voicetest_")
os.chmod(W, 0o777)
# mumble-server will not run as root: as root, run it as the user who called sudo (or "nobody")
RUNAS = ["runuser", "-u", os.environ.get("SUDO_USER") or "nobody", "--"] if os.geteuid() == 0 else []

def tone(freq, ms, amp=9000):
    return b"".join(struct.pack("<h", int(amp * math.sin(2 * math.pi * freq * i / 48000))) for i in range(48 * ms))

def peak(pcm, freqs=(440, 660, 880, 1000)):
    pcm = pcm[:96000]; pcm = pcm[:len(pcm) - len(pcm) % 2]
    s = struct.unpack("<%dh" % (len(pcm) // 2), pcm)
    out = {}
    for f in freqs:
        k = 2 * math.cos(2 * math.pi * f / 48000); a = b = 0.0
        for x in s:
            a, b = x + k * a - b, a
        out[f] = a * a + b * b - k * a * b
    return max(out, key=out.get) if s else None

# ---- Mumble server
ini = W + "/murmur.ini"
open(ini, "w").write("database=%s/m.sqlite\nlogfile=%s/m.log\npidfile=%s/m.pid\nport=%d\nhost=127.0.0.1\nserverpassword=%s\n"
                     "users=50\nbandwidth=72000\nwelcometext=test\nautobanAttempts=0\n" % (W, W, W, MPORT, SPW))
def start_murmur():
    subprocess.run(RUNAS + ["mumble-server", "-ini", ini], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        if not os.path.exists(W + "/m.pid"):
            time.sleep(0.1); continue
        try:
            socket.create_connection(("127.0.0.1", MPORT), timeout=0.2).close(); return True
        except OSError:
            time.sleep(0.1)
    return False
subprocess.run(RUNAS + ["mumble-server", "-ini", ini, "-supw", SUPW], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
import atexit
def _stop_murmur():
    try:
        pid = int(open(W + "/m.pid").read().strip())
    except (OSError, ValueError):
        return
    for sig in (15, 15, 15, 9):                 # mumble-server sometimes hangs while shutting down
        try:
            os.kill(pid, sig)
        except OSError:
            return
        for _ in range(10):
            time.sleep(0.1)
            try: os.kill(pid, 0)
            except OSError: return
atexit.register(_stop_murmur)
check("Mumble server starts", start_murmur(), open(W + "/m.log").read()[-400:] if os.path.exists(W + "/m.log") else "")
env = dict(os.environ, MUMBLE_SUPERUSER_PASSWORD=SUPW)
out = "".join(subprocess.run([sys.executable, PKG + "/voice/make_channel.py", ch, "127.0.0.1", str(MPORT)], env=env,
                             capture_output=True, text=True).stdout for ch in ("TG1", "TG2"))
check("make_channel.py creates TG1 and TG2 as SuperUser", out.count("Created channel") == 2, out)

def server_pin():
    import hashlib, ssl
    c = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT); c.check_hostname = False; c.verify_mode = ssl.CERT_NONE
    with c.wrap_socket(socket.create_connection(("127.0.0.1", MPORT))) as s:
        return hashlib.sha256(s.getpeercert(True)).hexdigest()
PIN = server_pin()

# ---- fake mesh: talk on a port, hear every port
class Mesh:
    def __init__(self, ports):
        self.got = {p: [] for p in ports}
        self.tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.tx.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton("127.0.0.1"))
        self.tx.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
        for p in ports:
            rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); rx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            rx.bind(("", p)); rx.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, struct.pack("4s4s", socket.inet_aton(GROUP), socket.inet_aton("127.0.0.1")))
            rx.settimeout(0.2)
            threading.Thread(target=self._rx, args=(rx, p), daemon=True).start()
    def _rx(self, rx, port):
        while True:
            try: p, _ = rx.recvfrom(4096)
            except socket.timeout: continue
            except OSError: return
            r = mv.parse_rtp(p)
            if r and r[3] in GW_SSRCS:           # only the gateway's streams, not our fake phones
                self.got[port].append(r)
    def clear(self):
        for v in self.got.values(): v.clear()
    def talk(self, port, pcm, ssrc=0xF1000001, pt=111):
        enc = oc.Encoder(); seq = 100; ts = 5000; t0 = time.monotonic()
        for k in range(len(pcm) // 1920):
            pkt = bytearray(mv.build_rtp(seq, ts, ssrc, enc.encode(pcm[k * 1920:(k + 1) * 1920]), marker=(k == 0)))
            pkt[1] = (pkt[1] & 0x80) | pt
            self.tx.sendto(bytes(pkt), (GROUP, port)); seq += 1; ts += 960
            time.sleep(max(0, t0 + (k + 1) * 0.02 - time.monotonic()))
    def rtcp(self, port, ssrc=0xF1000001):
        self.tx.sendto(struct.pack(">BBHI", 0x80, 200, 6, ssrc) + bytes(20), (GROUP, port + 1))

# ---- the gateway
GW_SSRCS = set()
STATUS, BSTATUS = W + "/voice_status.json", W + "/bridge_status.json"
json.dump({"mesh_peers": [{"cs": "ALPHA-1", "ip": "127.0.0.1"}]}, open(BSTATUS, "w"))
cfg = dict(mv.DEFAULTS, VOICE="on", MESH_GROUP=GROUP, MESH_PORTS="38801,38803,38805", TALKGROUPS="38801:TG1, 38803:TG2",
           MESH_ADDR="127.0.0.1", MESH_LOOP="1", MUMBLE="127.0.0.1:%d" % MPORT, MUMBLE_PASSWORD=SPW, MUMBLE_CERT_SHA256=PIN,
           MAX_TALK_S="3", STATUS_FILE=STATUS, BRIDGE_STATUS_FILE=BSTATUS, CONTROL="127.0.0.1:%d" % free_port(),
           BRIDGE_STATE_FILE=W + "/voice_bridges.json", DECK_PTT="off")   # a real PTT box on a test deck stays out of it
mesh = Mesh([38801, 38803, 38805])
gw = mv.Gateway(cfg)
threading.Thread(target=gw.run, daemon=True).start()
tg = {g.name: g for g in gw.groups}
GW_SSRCS.update(g.ssrc for g in gw.groups)
def ready(g):
    c = g.client
    return c is not None and c.synced.is_set() and c.channels.get(c.my_channel) == g.channel
deadline = time.time() + 15
while time.time() < deadline and not (ready(tg["TG1"]) and ready(tg["TG2"])):
    time.sleep(0.2)
check("gateway: one Mumble login per bridged talk group, each in its channel (TG1, TG2)", ready(tg["TG1"]) and ready(tg["TG2"]))
check("gateway: TG3 is watched but not bridged (no Mumble login)", not tg["TG3"].bridged and tg["TG3"].client is None)

# ---- Mumble users
class User:
    def __init__(self, name, chan):
        self.pcm, self.who, self.texts, self.dec = b"", [], [], {}
        self.c = MumbleClient("127.0.0.1", MPORT, name, SPW, chan, pin_sha256=PIN, on_voice=self.on_voice,
                              on_text=lambda s, m: self.texts.append(m))
        self.c.connect(); time.sleep(0.4)
    def on_voice(self, session, name, opus, last):
        self.who.append(self.c.users.get(session, {}).get("name", "?"))
        if opus:
            d = self.dec.setdefault(session, oc.Decoder()); self.pcm += d.decode(opus)
    def talk(self, pcm):
        enc = oc.Encoder(); n = len(pcm) // 1920; t0 = time.monotonic()
        for k in range(n):
            self.c.send_voice(enc.encode(pcm[k * 1920:(k + 1) * 1920]), last=(k == n - 1))
            time.sleep(max(0, t0 + (k + 1) * 0.02 - time.monotonic()))
    def reset(self):
        self.pcm, self.who, self.texts = b"", [], []
alice, bob = User("alice", "TG1"), User("bob", "TG2")
time.sleep(1)
names1 = [u["name"] for s, u in alice.c.users.items() if u.get("channel") == alice.c.my_channel]
names2 = [u["name"] for s, u in bob.c.users.items() if u.get("channel") == bob.c.my_channel]
check("Mumble: DECK-voice sits in TG1, DECK-voice-TG2 in TG2", "DECK-voice" in names1 and "DECK-voice-TG2" in names2, (names1, names2))

# 1. mesh -> Mumble, per talk group
mesh.talk(38801, tone(440, 1500)); time.sleep(0.8)
check("TG1 mesh -> Mumble: alice hears 440 Hz (%.2f s)" % (len(alice.pcm) / 96000), peak(alice.pcm) == 440 and 1.3 < len(alice.pcm) / 96000 < 1.8)
check("TG1 audio stays out of TG2: bob hears nothing", len(bob.pcm) == 0, len(bob.pcm))
alice.reset(); bob.reset()
mesh.talk(38803, tone(660, 1000)); time.sleep(0.8)
check("TG2 mesh -> Mumble: bob hears 660 Hz", peak(bob.pcm) == 660 and len(bob.pcm) > 80000, len(bob.pcm))
check("TG2 audio stays out of TG1: alice hears nothing", len(alice.pcm) == 0, len(alice.pcm))

# 2. Mumble -> mesh, per talk group
time.sleep(0.5); mesh.clear()
alice.talk(tone(880, 1000)); time.sleep(0.8)
g1 = mesh.got[38801]
pcm = b"".join(oc.Decoder().decode(r[5]) for r in g1) if g1 else b""
check("TG1 Mumble -> mesh: the node hears 880 Hz on port 38801 (%d packets)" % len(g1), peak(pcm) == 880 and len(g1) >= 45,
      (len(g1), tg["TG1"].stats, tg["TG1"].floor, alice.c.tx_voice, alice.c.session, alice.c.my_channel, tg["TG1"].client.users))
check("TG1 Mumble -> mesh: PT 111, one stream, seq +1 / ts +960, marker first",
      g1 and {r[0] for r in g1} == {111} and len({r[3] for r in g1}) == 1 and g1[0][4] and
      all(((b[1] - a[1]) & 0xFFFF) == 1 and ((b[2] - a[2]) & 0xFFFFFFFF) == 960 for a, b in zip(g1, g1[1:])))
check("TG1 Mumble -> mesh: nothing leaks onto 38803 or 38805", not mesh.got[38803] and not mesh.got[38805])
check("no echo: the gateway ignored its own stream coming back", tg["TG1"].stats["ignored_own"] >= 45, tg["TG1"].stats)
mesh.clear(); time.sleep(0.4)
bob.talk(tone(440, 800)); time.sleep(0.8)
check("TG2 Mumble -> mesh: bob is heard on port 38803 only", len(mesh.got[38803]) >= 35 and not mesh.got[38801], {p: len(v) for p, v in mesh.got.items()})

# 3. both talk groups busy at the same time: independent floors
alice.reset(); bob.reset(); time.sleep(0.5)
t = threading.Thread(target=mesh.talk, args=(38801, tone(440, 1200), 0xF1000001)); t.start()
mesh.talk(38803, tone(660, 1200), 0xF2000002); t.join(); time.sleep(0.8)
check("TG1 and TG2 at once: alice gets 440 Hz and bob gets 660 Hz", peak(alice.pcm) == 440 and peak(bob.pcm) == 660, (len(alice.pcm), len(bob.pcm)))

# 4. one talker per talk group: the mesh talks, alice talks over it
time.sleep(0.6); mesh.clear(); alice.reset()
t = threading.Thread(target=mesh.talk, args=(38801, tone(440, 2000))); t.start()
time.sleep(0.4); alice.talk(tone(880, 800)); t.join(); time.sleep(0.6)
check("floor: alice's audio did not go onto the mesh while a phone held TG1", not mesh.got[38801], len(mesh.got[38801]))
check("floor: alice got 'Channel busy: ALPHA-1 is talking'", any("busy" in x.lower() and "ALPHA-1" in x for x in alice.texts), alice.texts)
st = json.load(open(STATUS)); s1 = next(g for g in st["groups"] if g["name"] == "TG1")
check("floor: the blocked attempt is in TG1's log", any(l["blocked"] and "alice" in l["who"] for l in s1["log"]), s1["log"][:3])
time.sleep(0.6); mesh.clear()
alice.talk(tone(660, 600)); time.sleep(0.6)
check("floor: released after the hang time; alice reaches the mesh again", len(mesh.got[38801]) > 20, len(mesh.got[38801]))

# 5. VX plugin labels Opus as payload type 0
alice.reset(); time.sleep(0.5)
mesh.talk(38801, tone(440, 800), 0xF3000003, pt=0); time.sleep(0.6)
check("VX-style Opus (payload type 0) is heard in Mumble", peak(alice.pcm) == 440 and len(alice.pcm) > 48000, len(alice.pcm))

# 6. stuck transmit: cut off after MAX_TALK_S, then free again
time.sleep(0.6); alice.reset(); cut0 = tg["TG1"].stats["cut_long"]
mesh.talk(38801, tone(440, 4000), 0xF4000004); time.sleep(0.6)
check("stuck PTT: cut off after MAX_TALK_S (3 s): about 3 s reached Mumble (%.1f s)" % (len(alice.pcm) / 96000),
      tg["TG1"].stats["cut_long"] == cut0 + 1 and 2.6 < len(alice.pcm) / 96000 < 3.4)
mesh.clear(); alice.talk(tone(660, 600)); time.sleep(0.6)
check("stuck PTT: after it stops, the talk group is free again", len(mesh.got[38801]) > 20, len(mesh.got[38801]))

# 7. TG3 is not bridged: watched only
time.sleep(0.5); alice.reset(); bob.reset()
t = threading.Thread(target=mesh.talk, args=(38805, tone(440, 1500), 0xF5000005)); t.start()
mesh.rtcp(38803, 0xF6000006); time.sleep(1.2)
st = json.load(open(STATUS)); g = {x["name"]: x for x in st["groups"]}
check("status: TG3 shows ALPHA-1 talking via mesh, bridged false", g["TG3"]["talking"] and g["TG3"]["talking"]["name"] == "ALPHA-1"
      and g["TG3"]["bridged"] is False, g["TG3"])
t.join(); time.sleep(0.5)
check("TG3 audio never reaches Mumble", len(alice.pcm) == 0 and len(bob.pcm) == 0 and tg["TG3"].stats.get("mesh_to_mumble") == 0)
st = json.load(open(STATUS)); g = {x["name"]: x for x in st["groups"]}
check("status: RTCP alone puts ALPHA-1 on TG2's mesh list (callsign from the bridge)",
      any(m["name"] == "ALPHA-1" and m["what"] == "ATAK phone" for m in g["TG2"]["mesh"]), g["TG2"]["mesh"])
check("status: TG1 lists alice on Mumble, Mumble up, in channel, RTT measured",
      [m["name"] for m in g["TG1"]["mumble"]] == ["alice"] and g["TG1"]["mumble_up"] and g["TG1"]["in_channel"], g["TG1"])
check("status: schema 1, five fields per log entry, newest first",
      st["schema"] == 1 and all(set(l) == {"ts", "who", "via", "len", "blocked"} for l in g["TG1"]["log"]) and
      g["TG1"]["log"] == sorted(g["TG1"]["log"], key=lambda l: -l["ts"]))
check("status: the Mumble password and cert pin are not in the status file", SPW not in json.dumps(st).replace('"Mumble"', "") and PIN not in json.dumps(st))

# 8. wrong cert pin is refused
try:
    MumbleClient("127.0.0.1", MPORT, "eve", SPW, "TG1", pin_sha256="00" * 32).connect(); refused = False
except Exception as e:
    refused = "certificate" in str(e).lower() or "pin" in str(e).lower() or "fingerprint" in str(e).lower(); err = e
check("a server with the wrong certificate is refused (pinning)", refused)
try:
    MumbleClient("127.0.0.1", MPORT, "eve", "wrong", "TG1").connect(); bad = False
except Exception:
    bad = True
check("a wrong server password is refused", bad)

# 8b. listen on deck, bridge switches; directly and through Deck Chat
import http.client, urllib.request, urllib.error
CTL = cfg["CONTROL"]
class Listen:
    def __init__(self, tg, hostport=CTL, path="/listen?tg="):
        self.pcm, self.status, self.stop = b"", None, False
        h, p = hostport.split(":")
        self.c = http.client.HTTPConnection(h, int(p), timeout=5)
        self.c.request("GET", path + tg); self.sock = self.c.sock; self.r = self.c.getresponse(); self.status = self.r.status
        if self.status == 200:
            threading.Thread(target=self._rd, daemon=True).start()
    def _rd(self):
        try:
            while not self.stop:
                b = self.r.read1(4096)
                if not b: return
                self.pcm += b
        except OSError:
            pass
    def close(self):
        self.stop = True
        try: self.sock.shutdown(socket.SHUT_RDWR)
        except OSError: pass
        self.c.close()
def voiced(pcm):        # the audio part, without keep-alive silence
    return b"".join(pcm[i:i + 1920] for i in range(0, len(pcm) - 1919, 1920) if any(pcm[i:i + 1920]))

time.sleep(0.6)
l1 = Listen("TG1"); time.sleep(0.3)
check("listen: manet-voice serves TG1 audio on its loopback control port", l1.status == 200 and len(tg["TG1"].listeners) == 1)
mesh.talk(38801, tone(440, 1000)); time.sleep(0.5)
v = voiced(l1.pcm)
check("listen: a mesh phone on TG1 is heard on the deck (440 Hz, %.2f s)" % (len(v) / 96000), peak(v) == 440 and 0.8 < len(v) / 96000 < 1.3, len(v))
l1.pcm = b""; time.sleep(0.4)
alice.talk(tone(880, 800)); time.sleep(0.5)
check("listen: a Mumble user on TG1 is heard on the deck too (880 Hz)", peak(voiced(l1.pcm)) == 880, len(l1.pcm))
l1.close(); time.sleep(2.6)
check("listen: closing the page frees the listener (found by the 2 s keep-alive)", len(tg["TG1"].listeners) == 0, len(tg["TG1"].listeners))
l3 = Listen("TG3"); mesh.talk(38805, tone(660, 800), 0xF7000007); time.sleep(0.5)
check("listen: an unbridged talk group (TG3) can be heard too", peak(voiced(l3.pcm)) == 660, len(l3.pcm)); l3.close(); time.sleep(1.2)
ls = [Listen("TG1") for _ in range(4)]; extra = Listen("TG2")
check("listen: at most 4 listeners (the 5th gets 429)", all(x.status == 200 for x in ls) and extra.status == 429, [x.status for x in ls] + [extra.status])
for x in ls: x.close()
extra.close(); time.sleep(1.2)
check("listen: an unknown talk group is a 404", Listen("TG9").status == 404)

# Deck Chat in front of it
CHPORT = free_port()
open(W + "/chat.ini", "w").write("[chat]\nUID = DECK-vt\nMESH_LOCAL_ADDR = 127.0.0.1\nLISTEN = 127.0.0.1:%d\nHISTORY_FILE = %s/hist.jsonl\n"
    "BRIDGE_STATUS_FILE = %s/none.json\nGPS = off\nSA_SEND = 0\nVOICE_STATUS_FILE = %s\nVOICE_CONTROL = %s\n"
    "BRIDGE_COPY_LISTEN = 127.0.0.1:%d\nDM_PORT = %d\nLOCAL_SEND = 127.0.0.1:%d\nSA_GROUP = off\n" % (CHPORT, W, W, STATUS, CTL, free_port(), free_port(), free_port()))
chat = subprocess.Popen([sys.executable, PKG + "/chat/deck_chat.py", "--config", W + "/chat.ini"], stdout=open(W + "/chat.log", "w"), stderr=subprocess.STDOUT)
atexit.register(chat.kill)        # never leave it holding ports, even if a check above crashes
CH = "http://127.0.0.1:%d" % CHPORT
for _ in range(60):
    try: urllib.request.urlopen(CH + "/healthz", timeout=1); break
    except Exception: time.sleep(0.2)
def chat_post(path, obj, deck=True):
    rq = urllib.request.Request(CH + path, json.dumps(obj).encode(), {"Content-Type": "application/json", **({"X-Deck": "1"} if deck else {})})
    try:
        with urllib.request.urlopen(rq, timeout=10) as rr: return rr.status, json.loads(rr.read())
    except urllib.error.HTTPError as e: return e.code, json.loads(e.read() or b"{}")
av = json.loads(urllib.request.urlopen(CH + "/api/voice").read())
check("Deck Chat: /api/voice says the deck's screen may control voice", av["running"] and av["control"] is True and "mumble" not in av)
lc = Listen("TG1", "127.0.0.1:%d" % CHPORT, "/api/voice/listen?tg=")
mesh.talk(38801, tone(440, 800)); time.sleep(0.5)
check("Deck Chat: listening through Deck Chat works (440 Hz)", lc.status == 200 and peak(voiced(lc.pcm)) == 440, (lc.status, len(lc.pcm))); lc.close()
check("Deck Chat: a bridge switch without the page's X-Deck header is refused", chat_post("/api/voice/bridge", {"tg": "TG3", "on": True}, deck=False)[0] == 403)

# switch TG3 on before its Mumble channel exists, then make the channel
st_, js = chat_post("/api/voice/bridge", {"tg": "TG3", "on": True})
check("switch: TG3 on (through Deck Chat)", st_ == 200 and js.get("ok") and js.get("bridged") is True, (st_, js))
deadline = time.time() + 15
while time.time() < deadline and not (tg["TG3"].client and tg["TG3"].client.synced.is_set()): time.sleep(0.2)
time.sleep(1.2)
g3 = next(g for g in json.load(open(STATUS))["groups"] if g["name"] == "TG3")
check("switch: TG3 logs in to Mumble as DECK-voice-TG3; no channel TG3 yet, and the status says so",
      g3["bridged"] and g3["mumble_up"] and not g3["in_channel"] and tg["TG3"].client.username == "DECK-voice-TG3", g3)
check("switch: remembered in the state file", json.load(open(W + "/voice_bridges.json")).get("38805") is True)
subprocess.run([sys.executable, PKG + "/voice/make_channel.py", "TG3", "127.0.0.1", str(MPORT)], env=env, capture_output=True)
deadline = time.time() + 8
while time.time() < deadline and not ready(tg["TG3"]): time.sleep(0.2)
check("switch: once channel TG3 is made, the gateway goes into it by itself", ready(tg["TG3"]))
dave = User("dave", "TG3"); time.sleep(0.5)
mesh.talk(38805, tone(660, 800), 0xF8000008); time.sleep(0.6)
check("switch: TG3 now reaches Mumble (dave hears 660 Hz)", peak(dave.pcm) == 660, len(dave.pcm))
# switch TG2 off: nothing crosses any more
st_, js = chat_post("/api/voice/bridge", {"tg": "TG2", "on": False})
for _ in range(25):
    time.sleep(0.2)
    names2 = [u["name"] for s_, u in bob.c.users.items() if u.get("channel") == bob.c.my_channel]
    if "DECK-voice-TG2" not in names2: break
check("switch: TG2 off: the gateway leaves Mumble channel TG2", st_ == 200 and "DECK-voice-TG2" not in names2, names2)
bob.reset(); mesh.clear(); mesh.talk(38803, tone(660, 600)); bob.talk(tone(440, 600)); time.sleep(0.6)
check("switch: TG2 off: nothing crosses either way", len(bob.pcm) == 0 and not mesh.got[38803], (len(bob.pcm), len(mesh.got[38803])))
gw2 = mv.Gateway(dict(cfg, CONTROL="off"))
check("switch: a restarted gateway keeps the switches (TG1 on, TG2 off, TG3 on)", gw2.wanted == {38801: True, 38803: False, 38805: True}, gw2.wanted)
gw2.close()
st_, js = chat_post("/api/voice/bridge", {"tg": "TG2", "on": True})
deadline = time.time() + 15
while time.time() < deadline and not ready(tg["TG2"]): time.sleep(0.2)
bob.reset(); time.sleep(0.3); mesh.talk(38803, tone(660, 600)); time.sleep(0.6)
check("switch: TG2 back on works again", peak(bob.pcm) == 660, len(bob.pcm))
chat_post("/api/voice/bridge", {"tg": "TG3", "on": False}); time.sleep(0.5)
check("switch: TG3 off again; bad requests are refused", not tg["TG3"].bridged and chat_post("/api/voice/bridge", {"tg": "TG7", "on": True})[0] == 404)

# the Voice page in a browser: LISTEN ON DECK plays it (the level meter moves)
try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None
if sync_playwright:
    with sync_playwright() as pw:
        br = pw.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
        pg = br.new_page(viewport={"width": 1280, "height": 720}); errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.goto(CH + "/voice"); pg.wait_for_timeout(1500)
        pg.click(".tg[data-tg=TG1]"); pg.wait_for_timeout(300)
        pg.click("#listen"); pg.wait_for_timeout(800)
        on = pg.evaluate("document.getElementById('listen').getAttribute('aria-pressed')") == "true" and len(tg["TG1"].listeners) == 1
        t = threading.Thread(target=mesh.talk, args=(38801, tone(440, 1500))); t.start()
        lv = 0
        for _ in range(12):
            pg.wait_for_timeout(100); lv = max(lv, pg.evaluate("parseFloat(document.getElementById('lvl').style.width) || 0"))
        t.join()
        badge = pg.evaluate("document.querySelector('.tg[data-tg=TG1] .badge') ? document.querySelector('.tg[data-tg=TG1] .badge').textContent : ''")
        check("Voice page: LISTEN ON DECK opens the stream, the meter moves while a phone talks, TG1 says LISTENING",
              on and lv > 5 and "LISTENING" in badge and not errs, (on, lv, badge, errs, len(tg["TG1"].listeners),
              pg.evaluate("[document.getElementById('listen').disabled, document.getElementById('listen').getAttribute('aria-pressed'), document.getElementById('ctlnote').textContent]")))
        pg.click("#listen"); pg.wait_for_timeout(2600)
        check("Voice page: STOP LISTENING closes the stream", len(tg["TG1"].listeners) == 0 and not errs, (len(tg["TG1"].listeners), errs))
        pg.click(".tg[data-tg=TG3]"); pg.wait_for_timeout(300); pg.click("#bridge"); pg.wait_for_timeout(1500)
        check("Voice page: the BRIDGE TO MUMBLE switch turns TG3 on", tg["TG3"].bridged and pg.evaluate("document.getElementById('bridge').getAttribute('aria-checked')") == "true")
        pg.click("#bridge"); pg.wait_for_timeout(1500)
        check("Voice page: and off again", not tg["TG3"].bridged)
        br.close()
else:
    print("SKIP  Voice page browser checks: playwright not installed")
chat.terminate()

# 9. Mumble server restart: both talk groups come back
_stop_murmur(); time.sleep(1.5)
check("Mumble down: the gateway notices on both talk groups", not tg["TG1"].client and not tg["TG2"].client or
      all(x.client is None or x.client.closed.is_set() for x in (tg["TG1"], tg["TG2"])))
rx0, f0 = tg["TG1"].stats["mesh_rx"], tg["TG1"].stats["mesh_to_mumble"]
mesh.talk(38801, tone(440, 300))
check("Mumble down: mesh audio is dropped, never queued", tg["TG1"].stats["mesh_rx"] - rx0 >= 14 and tg["TG1"].stats["mesh_to_mumble"] == f0)
start_murmur()
deadline = time.time() + 40
while time.time() < deadline and not (ready(tg["TG1"]) and ready(tg["TG2"])):
    time.sleep(0.3)
check("Mumble back: both talk groups reconnect and rejoin their channels", ready(tg["TG1"]) and ready(tg["TG2"]))
carol = User("carol", "TG2"); time.sleep(0.5)
mesh.talk(38803, tone(660, 600)); time.sleep(0.6)
check("Mumble back: TG2 audio flows again", peak(carol.pcm) == 660, len(carol.pcm))

# 10. manet_mesh_bridge check, voice part (with this gateway and server)
import contextlib, io, importlib.util
spec = importlib.util.spec_from_file_location("mmb", PKG + "/tools/manet_mesh_bridge.py"); mmb = importlib.util.module_from_spec(spec); spec.loader.exec_module(mmb)
def run_check(pin):
    ini = W + "/check.ini"
    open(ini, "w").write("[voice]\nVOICE = on\nMUMBLE = 127.0.0.1:%d\nMUMBLE_CERT_SHA256 = %s\nSTATUS_FILE = %s\n" % (MPORT, pin, STATUS))
    mmb.CONFIG = ini; mmb.results.clear(); buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        mmb.check_voice(mmb.load_cfg())
    return buf.getvalue()
out = run_check(PIN)
check("check: libopus found, server reachable, pin matches, TG1 + TG2 linked", "certificate matches" in out and out.count("<-> Mumble channel") == 2
      and "[FAIL]" not in out.replace("\033[31m", "") and "FAIL" not in out, out)
out = run_check("")
check("check: no pin = a warning that prints the server's fingerprint to paste", "not pinned" in out and PIN in out, out)
out = run_check("ab" * 32)
check("check: the wrong pin is a FAIL", "FAIL" in out and "pins another one" in out, out)

# 11. VOICE = off: the service exits quietly
ini2 = W + "/deck.ini"; open(ini2, "w").write("[voice]\nVOICE = off\n")
r = subprocess.run([sys.executable, PKG + "/voice/manet_voice.py", "--config", ini2], capture_output=True, text=True, timeout=10)
check("VOICE = off: manet-voice exits 0 and says how to turn it on", r.returncode == 0 and "VOICE = on" in r.stderr, r.stderr[-300:])

gw.close()
_stop_murmur()
print("%d/%d passed" % (sum(res), len(res)))
os._exit(0 if all(res) else 1)
