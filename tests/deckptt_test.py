"""deckptt_test.py - the PTT box on the deck (voice/deckptt.py) inside a real manet-voice gateway and Mumble server.

  fake PTT box   /proc/bus/input/devices + /proc/asound/cards written by the test, the button = a FIFO that gets
                 Linux input events, the mic = a script that sends a 1000 Hz tone in real time, the headset = a
                 script that writes what it is given to a file
  fake mesh      RTP/Opus on loopback multicast (a phone on TG1 talks 440 Hz)
  Mumble         mumble-server on loopback: alice in TG1, bob in TG2
Skipped (exit 0) when mumble-server or libopus is not installed.
argv: [package dir]
"""
import json, math, os, re, shutil, socket, struct, subprocess, sys, tempfile, threading, time, urllib.request, urllib.error

PKG = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), ".."))
sys.path[:0] = [PKG + "/voice", PKG + "/common", PKG + "/dashboard"]
import opuscodec as oc
import deckptt as dp

res = []
def check(n, c, x=""):
    res.append(bool(c)); print(("PASS  " if c else "FAIL  ") + n + ("" if c else "  " + str(x)[:400])); sys.stdout.flush()

# ---------------------------------------------------------------- 1. finding the box (no audio needed)
B = dp.LONG_BITS
def keybits(*codes):
    words = {}
    for c in codes:
        words[c // B] = words.get(c // B, 0) | (1 << (c % B))
    return " ".join("%x" % words.get(i, 0) for i in range(max(words), -1, -1))
BOX = ('I: Bus=0003 Vendor=0d8c Product=0012 Version=0100\nN: Name="C-Media Electronics Inc. USB PnP Sound Device"\n'
       'P: Phys=usb-0000:01:00.0-1.3/input3\nS: Sysfs=/devices/x\nU: Uniq=\nH: Handlers=kbd event7 \n'
       'B: PROP=0\nB: EV=13\nB: KEY=%s\nB: MSC=10\n' % keybits(113, 114, 115))
KBD = ('I: Bus=0003 Vendor=046d Product=c31c\nN: Name="Logitech USB Keyboard"\nP: Phys=usb-0000:01:00.0-1.1/input1\n'
       'H: Handlers=sysrq kbd event3 \nB: KEY=%s\n' % keybits(1, 30, 114, 115))
OTHER = ('I: Bus=0003\nN: Name="C-Media Electronics Inc. USB PnP Sound Device"\nP: Phys=usb-0000:01:00.0-1.4/input3\n'
         'H: Handlers=kbd event9 \nB: KEY=%s\n' % keybits(113, 114, 115))
CARDS = (" 0 [vc4hdmi0       ]: vc4-hdmi - vc4-hdmi-0\n                      vc4-hdmi-0\n"
         " 2 [Device         ]: USB-Audio - USB PnP Sound Device\n"
         "                      C-Media Electronics Inc. USB PnP Sound Device at usb-0000:01:00.0-1.3, full speed\n"
         " 3 [Device_1       ]: USB-Audio - USB PnP Sound Device\n"
         "                      C-Media Electronics Inc. USB PnP Sound Device at usb-0000:01:00.0-1.4, full speed\n")
M = "C-Media,USB PnP Sound,USB Audio Device"
check("find: the box = the input device with KEY_VOLUMEDOWN + the sound card on the same USB port",
      dp.find_box(KBD + "\n" + BOX, CARDS, M) == ("event7", "Device", "C-Media Electronics Inc. USB PnP Sound Device"),
      dp.find_box(KBD + "\n" + BOX, CARDS, M))
check("find: two identical sound cards: each button is paired with its own card by USB path",
      dp.find_box(OTHER, CARDS, M)[:2] == ("event9", "Device_1"), dp.find_box(OTHER, CARDS, M))
check("find: a keyboard's volume-down key is never taken for the PTT box", dp.find_box(KBD, CARDS, M) is None)
check("find: no PTT key = not the box; no sound card = not the box",
      dp.find_box(BOX.replace("B: KEY=%s" % keybits(113, 114, 115), "B: KEY=%s" % keybits(113, 115)), CARDS, M) is None and
      dp.find_box(BOX, CARDS.split(" 2 [")[0], M) is None)
check("find: KEY bitmap parsing keeps 113/114/115", {113, 114, 115} <= dp.parse_input_devices(BOX)[0]["keys"])

if oc.LOAD_ERROR or not shutil.which("mumble-server"):
    print("SKIP  deckptt_test live part: needs mumble-server and libopus (%s)" % (oc.LOAD_ERROR or "no mumble-server"))
    print("%d/%d passed" % (sum(res), len(res)))
    sys.exit(0 if all(res) else 1)
import manet_voice as mv
from mumbleclient import MumbleClient

_vp = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "chat", "voice.html"), encoding="utf-8").read()
_emo = [c for c in _vp if ord(c) > 0xFFFF or ord(c) == 0xFE0E] + re.findall(r"&#(?:1[2-9]\d{4}|65038);|\\1F[0-9A-Fa-f]{3}", _vp)
check("voice.html: no emoji (the deck has no emoji font, they show as empty boxes)", not _emo, _emo[:5])

def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0)); return s.getsockname()[1]
GROUP, MPORT, SPW, SUPW = "239.255.41.10", free_port(), "Mumble", "su-test-pw"
P1, P2, P3 = 38801, 38803, 38805
W = tempfile.mkdtemp(prefix="deckptt_")
os.chmod(W, 0o777)
RUNAS = ["runuser", "-u", os.environ.get("SUDO_USER") or "nobody", "--"] if os.geteuid() == 0 else []

def tone(freq, ms, amp=9000):
    return b"".join(struct.pack("<h", int(amp * math.sin(2 * math.pi * freq * i / 48000))) for i in range(48 * ms))
def power(pcm, f):
    pcm = pcm[:96000]; pcm = pcm[:len(pcm) - len(pcm) % 2]
    s = struct.unpack("<%dh" % (len(pcm) // 2), pcm)
    k = 2 * math.cos(2 * math.pi * f / 48000); a = b = 0.0
    for x in s:
        a, b = x + k * a - b, a
    return a * a + b * b - k * a * b
def peak(pcm, freqs=(440, 660, 1000)):
    return max(freqs, key=lambda f: power(pcm, f)) if len(pcm) > 1000 else None
def voiced(pcm):
    """only the 20 ms frames that are not silence"""
    return b"".join(pcm[i:i + 1920] for i in range(0, len(pcm) - 1919, 1920) if max(abs(x) for x in struct.unpack("<960h", pcm[i:i + 1920])) > 300)

# ---- Mumble server with TG1 + TG2
ini = W + "/murmur.ini"
open(ini, "w").write("database=%s/m.sqlite\nlogfile=%s/m.log\npidfile=%s/m.pid\nport=%d\nhost=127.0.0.1\nserverpassword=%s\n"
                     "users=50\nbandwidth=72000\nwelcometext=test\nautobanAttempts=0\n" % (W, W, W, MPORT, SPW))
subprocess.run(RUNAS + ["mumble-server", "-ini", ini, "-supw", SUPW], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
subprocess.run(RUNAS + ["mumble-server", "-ini", ini], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
import atexit
def _stop_murmur():
    try:
        pid = int(open(W + "/m.pid").read().strip())
    except (OSError, ValueError):
        return
    for sig in (15, 15, 15, 9):
        try: os.kill(pid, sig)
        except OSError: return
        for _ in range(10):
            time.sleep(0.1)
            try: os.kill(pid, 0)
            except OSError: return
atexit.register(_stop_murmur)
for _ in range(100):
    try: socket.create_connection(("127.0.0.1", MPORT), timeout=0.2).close(); break
    except OSError: time.sleep(0.1)
env = dict(os.environ, MUMBLE_SUPERUSER_PASSWORD=SUPW)
for ch in ("TG1", "TG2"):
    subprocess.run([sys.executable, PKG + "/voice/make_channel.py", ch, "127.0.0.1", str(MPORT)], env=env, capture_output=True)
def server_pin():
    import hashlib, ssl
    c = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT); c.check_hostname = False; c.verify_mode = ssl.CERT_NONE
    with c.wrap_socket(socket.create_connection(("127.0.0.1", MPORT))) as s:
        return hashlib.sha256(s.getpeercert(True)).hexdigest()
PIN = server_pin()

# ---- fake mesh
GW_SSRCS = set()
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
            if r and r[3] in GW_SSRCS:
                self.got[port].append(r)
    def clear(self):
        for v in self.got.values(): v.clear()
    def pcm(self, port):
        d = oc.Decoder(); return b"".join(d.decode(r[5]) for r in self.got[port])
    def talk(self, port, pcm, ssrc=0xF1000001):
        enc = oc.Encoder(); seq = 100; ts = 5000; t0 = time.monotonic()
        for k in range(len(pcm) // 1920):
            self.tx.sendto(mv.build_rtp(seq, ts, ssrc, enc.encode(pcm[k * 1920:(k + 1) * 1920]), marker=(k == 0)), (GROUP, port))
            seq += 1; ts += 960
            time.sleep(max(0, t0 + (k + 1) * 0.02 - time.monotonic()))
mesh = Mesh([P1, P2, P3])

# ---- fake PTT box
PROC_IN, PROC_CARDS, DEV, HEADSET = W + "/input_devices", W + "/cards", W + "/dev", W + "/headset.raw"
os.makedirs(DEV)
open(PROC_IN, "w").write(KBD); open(PROC_CARDS, "w").write(CARDS)
MIC = W + "/mic.py"
open(MIC, "w").write("import math,struct,sys,time\nf=b''.join(struct.pack('<h',int(9000*math.sin(2*math.pi*1000*i/48000))) for i in range(960))\n"
                     "t0=time.monotonic();k=0\nopen(sys.argv[2],'a').write(sys.argv[1]+'\\n')\nwhile True:\n"
                     "  sys.stdout.buffer.write(f);sys.stdout.flush();k+=1;time.sleep(max(0,t0+k*0.02-time.monotonic()))\n")
EAR = W + "/ear.py"
open(EAR, "w").write("import sys\nout=open(sys.argv[1],'ab',buffering=0)\nwhile True:\n  b=sys.stdin.buffer.read1(4096)\n  if not b: break\n  out.write(b)\n")
DEVLOG = W + "/devices.log"
def plug():
    if not os.path.exists(DEV + "/event7"):
        os.mkfifo(DEV + "/event7")
    global btn
    btn = os.open(DEV + "/event7", os.O_RDWR)           # keep a writer open, like a real input device
    open(PROC_IN, "w").write(KBD + "\n" + BOX)
def unplug():
    open(PROC_IN, "w").write(KBD)
    try: os.close(btn)
    except OSError: pass
    os.unlink(DEV + "/event7")
def key(v):
    os.write(btn, dp.EVENT.pack(int(time.time()), 0, dp.EV_KEY, dp.KEY_VOLUMEDOWN, v) + dp.EVENT.pack(int(time.time()), 0, 0, 0, 0))

# ---- the gateway, with the PTT box pointed at the fakes
STATUS, BSTATUS = W + "/voice_status.json", W + "/bridge_status.json"
json.dump({"mesh_peers": [{"cs": "ALPHA-1", "ip": "127.0.0.1"}]}, open(BSTATUS, "w"))
CTL = "127.0.0.1:%d" % free_port()
cfg = dict(mv.DEFAULTS, VOICE="on", MESH_GROUP=GROUP, MESH_PORTS="%d,%d,%d" % (P1, P2, P3),
           TALKGROUPS="%d:TG1, %d:TG2" % (P1, P2), MESH_ADDR="127.0.0.1", MESH_LOOP="1", MUMBLE="127.0.0.1:%d" % MPORT,
           MUMBLE_PASSWORD=SPW, MUMBLE_CERT_SHA256=PIN, STATUS_FILE=STATUS, BRIDGE_STATUS_FILE=BSTATUS, CONTROL=CTL,
           BRIDGE_STATE_FILE=W + "/voice_bridges.json", DECK_PTT_STATE_FILE=W + "/voice_deckptt.json",
           DECK_PTT_CAPTURE="%s %s {dev} %s" % (sys.executable, MIC, DEVLOG), DECK_PTT_PLAY="%s %s %s" % (sys.executable, EAR, HEADSET))
def make_gw():
    g = mv.Gateway(cfg)
    g.deck = dp.DeckPTT(g, cfg, proc_input=PROC_IN, proc_cards=PROC_CARDS, devdir=DEV, grab=False)
    return g
gw = make_gw()
threading.Thread(target=gw.run, daemon=True).start()
tg = {g.name: g for g in gw.groups}
GW_SSRCS.update(g.ssrc for g in gw.groups)
def ready(g):
    c = g.client
    return c is not None and c.synced.is_set() and c.channels.get(c.my_channel) == g.channel
deadline = time.time() + 15
while time.time() < deadline and not (ready(tg["TG1"]) and ready(tg["TG2"])):
    time.sleep(0.2)
check("gateway up, TG1 + TG2 in Mumble", ready(tg["TG1"]) and ready(tg["TG2"]))
def st():
    gw.write_status(); return json.load(open(STATUS))["deck_ptt"]
def wait(cond, secs=6):
    end = time.time() + secs
    while time.time() < end:
        if cond(): return True
        time.sleep(0.1)
    return cond()
check("not plugged in: status says so, no error", wait(lambda: st()["enabled"] and not st()["present"] and not st()["error"]), st())

class User:
    def __init__(self, name, chan):
        self.pcm, self.dec = b"", {}
        self.c = MumbleClient("127.0.0.1", MPORT, name, SPW, chan, pin_sha256=PIN, on_voice=self.on_voice)
        self.c.connect(); time.sleep(0.4)
    def on_voice(self, session, name, opus, last):
        if opus:
            self.pcm += self.dec.setdefault(session, oc.Decoder()).decode(opus)
alice, bob = User("alice", "TG1"), User("bob", "TG2")

# 2. plug in
plug()
check("plug in: found within a few seconds, talks on TG1 (the first), card + name known",
      wait(lambda: st()["present"]) and st()["tg"] == "TG1" and st()["card"] == "Device", st())
check("plug in: mic and headset opened on that card (plughw:CARD=Device,DEV=0)",
      wait(lambda: "plughw:CARD=Device,DEV=0" in open(DEVLOG).read()) if os.path.exists(DEVLOG) else False)

# 3. headset hears the talk group (a phone on TG1)
time.sleep(0.5); h0 = os.path.getsize(HEADSET) if os.path.exists(HEADSET) else 0
mesh.talk(P1, tone(440, 1200)); time.sleep(0.6)
ear = open(HEADSET, "rb").read()[h0:]
check("headset: hears the phone on TG1 (440 Hz, %.1f s)" % (len(voiced(ear)) / 96000), peak(voiced(ear)) == 440 and len(voiced(ear)) > 80000, len(ear))

# 4. key up: mesh + Mumble get the mic, the headset does not
time.sleep(0.5); mesh.clear(); alice.pcm = b""; h0 = os.path.getsize(HEADSET)
key(1); time.sleep(0.6)
mid = st()
time.sleep(0.9); key(0); time.sleep(0.8)
got = mesh.pcm(P1)
check("PTT: status TALKING on TG1 while keyed; the talk group shows DECK (PTT box) via deck",
      mid["talking"] and mid["keyed"] and any((g["talking"] or {}).get("name") == dp.DECK_NAME and g["talking"]["via"] == "deck"
                                               for g in json.load(open(STATUS))["groups"] if g["name"] == "TG1") or
      (mid["talking"] and tg["TG1"].stats["deck_rx"] > 0), mid)
check("PTT: the mesh hears the mic on TG1 (1000 Hz, %d packets, one stream, marker first)" % len(mesh.got[P1]),
      peak(got) == 1000 and len(mesh.got[P1]) >= 50 and len({r[3] for r in mesh.got[P1]}) == 1 and mesh.got[P1][0][4],
      (len(mesh.got[P1]), tg["TG1"].stats))
check("PTT: Mumble TG1 hears it (alice, 1000 Hz, %.1f s); TG2 does not" % (len(alice.pcm) / 96000),
      peak(alice.pcm) == 1000 and len(alice.pcm) > 96000 and power(bob.pcm, 1000) < 1e6, (len(alice.pcm), len(bob.pcm)))
ear = open(HEADSET, "rb").read()[h0:]
check("PTT: the headset never plays the deck's own voice", power(voiced(ear), 1000) < 1e6 if voiced(ear) else True, len(voiced(ear)))
check("PTT: nothing leaks to TG2/TG3 on the mesh", not mesh.got[P2] and not mesh.got[P3])
log1 = tg["TG1"].log[0] if tg["TG1"].log else {}
check("let go: channel free again, the transmission is logged (DECK (PTT box), deck, ~1.5 s)",
      tg["TG1"].floor is None and log1.get("who") == dp.DECK_NAME and log1.get("via") == "deck" and 1.0 < log1.get("len", 0) < 2.5
      and not st()["talking"], (tg["TG1"].floor, log1, st()))

# 5. busy: a phone talks, the deck keys over it and waits
mesh.clear(); alice.pcm = b""
t = threading.Thread(target=mesh.talk, args=(P1, tone(440, 1800))); t.start()
time.sleep(0.4); key(1); time.sleep(0.6)
b = st()
key(0); t.join(); time.sleep(0.6)
check("busy: keyed while a phone talks: BUSY with the talker's name, the deck is not sent",
      b["busy"] == "ALPHA-1" and not b["talking"] and not mesh.got[P1] and peak(alice.pcm) == 440, (b, len(mesh.got[P1])))

# 6. pick TG2 through the control port (what the Voice page's button calls)
def ctl(path, obj):
    rq = urllib.request.Request("http://%s%s" % (CTL, path), json.dumps(obj).encode(), {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(rq, timeout=5) as r: return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e: return e.code, json.loads(e.read() or b"{}")
c, j = ctl("/deckptt", {"tg": "TG2"})
check("pick TG2: accepted, saved, status follows", c == 200 and j.get("tg") == "TG2" and st()["tg"] == "TG2" and
      json.load(open(W + "/voice_deckptt.json"))["tg"] == "TG2", (c, j))
check("pick: an unknown talk group is a 404, a bad body a 400", ctl("/deckptt", {"tg": "TG9"})[0] == 404 and ctl("/deckptt", {"x": 1})[0] == 400)
time.sleep(0.5); mesh.clear(); bob.pcm = b""; alice.pcm = b""; h0 = os.path.getsize(HEADSET)
mesh.talk(P2, tone(660, 1000)); time.sleep(0.6)
ear = open(HEADSET, "rb").read()[h0:]
check("headset follows: now hears TG2 (660 Hz)", peak(voiced(ear)) == 660, len(ear))
mesh.clear(); time.sleep(0.4); bob.pcm = alice.pcm = b""           # bob just heard the 660 Hz phone: start clean
key(1); time.sleep(1.2); key(0); time.sleep(0.8)
check("PTT on TG2: mesh port %d and bob hear it, TG1 and alice do not" % P2,
      peak(mesh.pcm(P2)) == 1000 and peak(bob.pcm) == 1000 and not mesh.got[P1] and power(alice.pcm, 1000) < 1e6,
      ({p: len(v) for p, v in mesh.got.items()}, len(bob.pcm), len(alice.pcm)))

# 7. a talk group that is not bridged: mesh only
ctl("/deckptt", {"tg": "TG3"}); mesh.clear(); alice.pcm = bob.pcm = b""; time.sleep(0.3)
key(1); time.sleep(1.0); key(0); time.sleep(0.6)
check("TG3 (not bridged): the mesh hears the deck, nobody on Mumble does",
      peak(mesh.pcm(P3)) == 1000 and tg["TG3"].stats["deck_to_mumble"] == 0 and not alice.pcm and not bob.pcm,
      (len(mesh.got[P3]), tg["TG3"].stats))
ctl("/deckptt", {"tg": "TG1"})

# 8. unplug while keyed, then plug back in
key(1); time.sleep(0.5)
unplug()
check("unplug while keyed: NOT PLUGGED IN within seconds, the channel is freed, no error",
      wait(lambda: not st()["present"]) and wait(lambda: tg["TG1"].floor is None, 2) and not st()["error"] and not st()["talking"],
      (st(), tg["TG1"].floor))
check("unplug: the headset stops listening to TG1", wait(lambda: gw.deck.monitor_q not in tg["TG1"].listeners, 3))
plug()
check("plug back in: found again, still TG1", wait(lambda: st()["present"]) and st()["tg"] == "TG1", st())
mesh.clear(); time.sleep(0.3); key(1); time.sleep(0.8); key(0); time.sleep(0.5)
check("plug back in: PTT works again", peak(mesh.pcm(P1)) == 1000, len(mesh.got[P1]))

# 9. errors reach Ops; the talk group survives a restart
gw.deck.error = "no permission to read the PTT button (/dev/input/event7): run install.sh again, then meshtoc restart"
gw.write_status()
import types
import dashboard_server as ds
v = ds.Collector.voice(types.SimpleNamespace(cfg={"VOICE_STATUS_FILE": STATUS}))
check("Ops: the PTT box error is in the voice status the dashboard reads", v["deck_ptt"]["error"].startswith("no permission") and v["deck_ptt"]["enabled"], v)
gw.deck.error = ""
ctl("/deckptt", {"tg": "TG2"})
d2 = dp.DeckPTT(gw, cfg, proc_input=PROC_IN, proc_cards=PROC_CARDS, devdir=DEV, grab=False)
check("restart: a new PTT box object talks on the saved talk group (TG2)", d2.tg == "TG2", d2.tg)
off = mv.Gateway(dict(cfg, DECK_PTT="off", CONTROL="off", MESH_PORTS="%d" % 38807, TALKGROUPS="%d:TG4" % 38807))
check("DECK_PTT = off: no PTT box, status says enabled false", off.deck is None)
off.close()

# 10. the Voice page shows it (deck screen) and its button picks the talk group
CHPORT = free_port()
open(W + "/chat.ini", "w").write("[chat]\nUID = DECK-pt\nMESH_LOCAL_ADDR = 127.0.0.1\nLISTEN = 127.0.0.1:%d\nHISTORY_FILE = %s/hist.jsonl\n"
    "BRIDGE_STATUS_FILE = %s/none.json\nGPS = off\nSA_SEND = 0\nVOICE_STATUS_FILE = %s\nVOICE_CONTROL = %s\n"
    "BRIDGE_COPY_LISTEN = 127.0.0.1:%d\nDM_PORT = %d\nLOCAL_SEND = 127.0.0.1:%d\nSA_GROUP = off\n" % (CHPORT, W, W, STATUS, CTL, free_port(), free_port(), free_port()))
chat = subprocess.Popen([sys.executable, PKG + "/chat/deck_chat.py", "--config", W + "/chat.ini"], stdout=open(W + "/chat.log", "w"), stderr=subprocess.STDOUT)
atexit.register(chat.kill)
CH = "http://127.0.0.1:%d" % CHPORT
for _ in range(60):
    try: urllib.request.urlopen(CH + "/healthz", timeout=1); break
    except Exception: time.sleep(0.2)
rq = urllib.request.Request(CH + "/api/voice/deckptt", json.dumps({"tg": "TG1"}).encode(), {"Content-Type": "application/json"})
try:
    urllib.request.urlopen(rq, timeout=5); code = 200
except urllib.error.HTTPError as e:
    code = e.code
check("Chat: picking the PTT talk group without the page's X-Deck header is refused", code == 403 and st()["tg"] == "TG2")
try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None
if sync_playwright:
    with sync_playwright() as pw:
        br = pw.chromium.launch()
        pg = br.new_page(viewport={"width": 1280, "height": 760}); errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.goto(CH + "/voice"); pg.wait_for_timeout(1500)
        txt = lambda sel: pg.evaluate("(document.querySelector(%r)||{}).textContent||''" % sel)
        check("Voice page: header pill PTT BOX · TG2 · READY, badge on TG2's card",
              "PTT BOX · TG2 · READY" in txt("#pptt") and "PTT BOX" in txt('.tg[data-tg="TG2"]') and "PTT BOX" not in txt('.tg[data-tg="TG1"]') and not errs,
              (txt("#pptt"), errs))
        mic = pg.evaluate("""() => { const b = document.querySelector('.tg[data-tg="TG2"] .pbadge .mic'), p = document.querySelector('#pptt');
            const pb = getComputedStyle(p, '::before'), bb = b && getComputedStyle(b), m = s => (s.maskImage || s.webkitMaskImage || '');
            return {pill: m(pb).includes('svg') && parseFloat(pb.width) > 4 && pb.backgroundColor === getComputedStyle(p).color,
                    badge: !!b && m(bb).includes('svg') && b.getBoundingClientRect().width > 4 && bb.backgroundColor === getComputedStyle(b.parentNode).color}; }""")
        check("Voice page: the mic icon is drawn by the page (pill + badge, in the text colour), no emoji font needed",
              mic["pill"] and mic["badge"], mic)
        pg.click('.tg[data-tg="TG1"]'); pg.wait_for_timeout(300)
        check("Voice page: on TG1 the row offers PTT BOX: TALK ON TG1", "TALK ON TG1" in txt("#pttbtn") and "plugged in" in txt("#pttval"), (txt("#pttbtn"), txt("#pttval")))
        pg.click("#pttbtn"); pg.wait_for_timeout(1800)
        check("Voice page: the button moves the PTT box to TG1 (pressed, pill follows)",
              st()["tg"] == "TG1" and "PTT BOX TALKS ON TG1" in txt("#pttbtn") and "TG1" in txt("#pptt"), (st()["tg"], txt("#pttbtn")))
        key(1); pg.wait_for_timeout(1500)
        check("Voice page: keyed: pill TALKING, the talk group shows DECK (PTT box) TALKING",
              "TALKING" in txt("#pptt") and "DECK (PTT box)" in txt("#dbody"), (txt("#pptt"), txt("#dbody")[:120]))
        key(0); pg.wait_for_timeout(300)
        pg.screenshot(path=W + "/voice_ptt.png")
        vis = lambda sel: pg.evaluate("getComputedStyle(document.querySelector(%r)).display !== 'none'" % sel)
        gw.deck.stop.set(); real_status = gw.deck.status
        gw.deck.status = lambda: {"enabled": False}; gw.write_status(); pg.wait_for_timeout(1500)
        check("Voice page: DECK_PTT off: no PTT row and no pill (really hidden, not just marked)", not vis("#pttrow") and not vis("#pptt"),
              (vis("#pttrow"), vis("#pptt")))
        gw.deck.status = real_status; gw.deck.stop.clear(); threading.Thread(target=gw.deck.run, daemon=True).start()
        pg.wait_for_timeout(1500)
        unplug(); pg.wait_for_timeout(4500)
        check("Voice page: unplugged: NOT PLUGGED IN", "NOT PLUGGED IN" in txt("#pptt") and not errs, (txt("#pptt"), errs))
        br.close()
else:
    print("SKIP  Voice page browser checks: playwright not installed")
chat.terminate()
gw.close()
_stop_murmur()
print("%d/%d passed" % (sum(res), len(res)))
os._exit(0 if all(res) else 1)
