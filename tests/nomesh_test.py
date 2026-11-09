"""nomesh_test.py - voice with no mesh node plugged in: Mumble at once (bridged talk groups, PTT box), the mesh
joins in by itself when the node is plugged in, and the page says which.
Built on deckptt_test's fakes:

  fake PTT box   /proc/bus/input/devices + /proc/asound/cards written by the test, the button = a FIFO that gets
                 Linux input events, the mic = a script that sends a 1000 Hz tone in real time, the headset = a
                 script that writes what it is given to a file
  fake mesh      RTP/Opus on loopback multicast (a phone on TG1 talks 440 Hz)
  Mumble         mumble-server on loopback: alice in TG1, bob in TG2
Skipped (exit 0) when mumble-server or libopus is not installed.
argv: [package dir]
"""
import json, math, os, shutil, socket, struct, subprocess, sys, tempfile, threading, time, urllib.request, urllib.error

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

def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0)); return s.getsockname()[1]
GROUP, MPORT, SPW, SUPW = "239.255.41.11", free_port(), "Mumble", "su-test-pw"
P1, P2, P3 = 38801, 38803, 38805
W = tempfile.mkdtemp(prefix="nomesh_")
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

# ---- the gateway, started with NO mesh node (MESH_ADDR = auto, the port has no address)
FAKE_IP = {"ip": None}
mv.iface_ipv4 = lambda iface: FAKE_IP["ip"]
WATCH = []
mv.watch_mesh_addr = lambda iface, cur, cb, every=5.0: WATCH.append((iface, cur))
mv.apply_deck = mv.apply_deck or (lambda *a: [])
STATUS, BSTATUS = W + "/voice_status.json", W + "/bridge_status.json"
json.dump({"mesh_peers": [{"cs": "ALPHA-1", "ip": "127.0.0.1"}]}, open(BSTATUS, "w"))
CTL = "127.0.0.1:%d" % free_port()
cfg = dict(mv.DEFAULTS, VOICE="on", MESH_GROUP=GROUP, MESH_PORTS="%d,%d,%d" % (P1, P2, P3),
           TALKGROUPS="%d:TG1, %d:TG2" % (P1, P2), MESH_ADDR="auto", MESH_LOOP="1", MUMBLE="127.0.0.1:%d" % MPORT,
           MUMBLE_PASSWORD=SPW, MUMBLE_CERT_SHA256=PIN, STATUS_FILE=STATUS, BRIDGE_STATUS_FILE=BSTATUS, CONTROL=CTL,
           BRIDGE_STATE_FILE=W + "/voice_bridges.json", DECK_PTT_STATE_FILE=W + "/voice_deckptt.json", _MESH_IFACE="ethX",
           DECK_PTT_CAPTURE="%s %s {dev} %s" % (sys.executable, MIC, DEVLOG), DECK_PTT_PLAY="%s %s %s" % (sys.executable, EAR, HEADSET))
mv.resolve_mesh_addr(cfg)
check("no node: resolve_mesh_addr does not wait (MESH_ADDR empty, auto noted)", cfg["MESH_ADDR"] == "" and cfg.get("_MESH_AUTO") == "1", cfg["MESH_ADDR"])
gw = mv.Gateway(cfg)
gw.deck = dp.DeckPTT(gw, cfg, proc_input=PROC_IN, proc_cards=PROC_CARDS, devdir=DEV, grab=False)
mv.follow_mesh(gw, every=0.5)
threading.Thread(target=gw.run, daemon=True).start()
tg = {g.name: g for g in gw.groups}
GW_SSRCS.update(g.ssrc for g in gw.groups)
def ready(g):
    c = g.client
    return c is not None and c.synced.is_set() and c.channels.get(c.my_channel) == g.channel
deadline = time.time() + 15
while time.time() < deadline and not (ready(tg["TG1"]) and ready(tg["TG2"])):
    time.sleep(0.2)
def stt():
    gw.write_status(); return json.load(open(STATUS))
check("no node: Mumble is up at once (TG1 + TG2 in their channels)", ready(tg["TG1"]) and ready(tg["TG2"]))
check("no node: status says mesh waiting on ethX", stt()["mesh_state"] == "waiting" and stt()["mesh_iface"] == "ethX", stt().get("mesh_state"))

class User:
    def __init__(self, name, chan):
        self.pcm, self.dec = b"", {}
        self.c = MumbleClient("127.0.0.1", MPORT, name, SPW, chan, pin_sha256=PIN, on_voice=self.on_voice)
        self.c.connect(); time.sleep(0.4)
    def on_voice(self, session, name, opus, last):
        if opus:
            self.pcm += self.dec.setdefault(session, oc.Decoder()).decode(opus)
    def talk(self, pcm):
        enc = oc.Encoder(); n = len(pcm) // 1920; t0 = time.monotonic()
        for k in range(n):
            self.c.send_voice(enc.encode(pcm[k * 1920:(k + 1) * 1920]), last=(k == n - 1))
            time.sleep(max(0, t0 + (k + 1) * 0.02 - time.monotonic()))
alice = User("alice", "TG1")
def wait(cond, secs=6):
    end = time.time() + secs
    while time.time() < end:
        if cond(): return True
        time.sleep(0.1)
    return cond()

# PTT box on Mumble alone
plug()
check("no node: the PTT box is found", wait(lambda: stt()["deck_ptt"]["present"]), stt()["deck_ptt"])
time.sleep(0.5); alice.pcm = b""; mesh.clear()
key(1); time.sleep(1.2); key(0); time.sleep(0.8)
check("no node: PTT box -> Mumble TG1 (alice hears 1000 Hz), nothing on the mesh",
      peak(alice.pcm) == 1000 and len(alice.pcm) > 80000 and not mesh.got[P1], (len(alice.pcm), len(mesh.got[P1])))
check("no node: the transmission is logged", tg["TG1"].log and tg["TG1"].log[0]["who"] == dp.DECK_NAME)
h0 = os.path.getsize(HEADSET) if os.path.exists(HEADSET) else 0
time.sleep(0.4); alice.talk(tone(660, 1000)); time.sleep(0.8)
ear = open(HEADSET, "rb").read()[h0:]
check("no node: Mumble -> headset (660 Hz), no crash sending to a mesh that isn't there",
      peak(voiced(ear)) == 660 and tg["TG1"].stats["mumble_to_mesh"] == 0 and tg["TG1"].floor is None, (len(ear), tg["TG1"].stats))

# the node is plugged in
FAKE_IP["ip"] = "127.0.0.1"
check("plug the node in: the mesh joins within seconds, status up, then watched for a new address",
      wait(lambda: gw.tx is not None) and stt()["mesh_state"] == "up" and wait(lambda: WATCH == [("ethX", "127.0.0.1")], 3), (gw.tx, WATCH))
time.sleep(0.5); alice.pcm = b""
mesh.talk(P1, tone(440, 1200)); time.sleep(0.8)
check("node in: mesh -> Mumble works (alice hears 440 Hz)", peak(alice.pcm) == 440, len(alice.pcm))
mesh.clear(); time.sleep(0.4); key(1); time.sleep(1.0); key(0); time.sleep(0.6)
check("node in: PTT box -> mesh and Mumble", peak(mesh.pcm(P1)) == 1000, len(mesh.got[P1]))
mesh.clear(); time.sleep(0.3); alice.talk(tone(660, 800)); time.sleep(0.6)
check("node in: Mumble -> mesh", peak(mesh.pcm(P1)) == 660, len(mesh.got[P1]))

# the node is unplugged (its address goes away): status back to waiting, Mumble keeps going
FAKE_IP["ip"] = None
check("unplug the node: status says waiting again", stt()["mesh_state"] == "waiting")
alice.pcm = b""; key(1); time.sleep(1.0); key(0); time.sleep(0.6)
check("node out: PTT box -> Mumble still works", peak(alice.pcm) == 1000, len(alice.pcm))
FAKE_IP["ip"] = "127.0.0.1"

# Voice page, with no node
FAKE_IP["ip"] = None
CHPORT = free_port()
open(W + "/chat.ini", "w").write("[chat]\nUID = DECK-nm\nMESH_LOCAL_ADDR = 127.0.0.1\nLISTEN = 127.0.0.1:%d\nHISTORY_FILE = %s/hist.jsonl\n"
    "BRIDGE_STATUS_FILE = %s/none.json\nGPS = off\nSA_SEND = 0\nVOICE_STATUS_FILE = %s\nVOICE_CONTROL = %s\n"
    "BRIDGE_COPY_LISTEN = 127.0.0.1:%d\nDM_PORT = %d\nLOCAL_SEND = 127.0.0.1:%d\nSA_GROUP = off\n" % (CHPORT, W, W, STATUS, CTL, free_port(), free_port(), free_port()))
chat = subprocess.Popen([sys.executable, PKG + "/chat/deck_chat.py", "--config", W + "/chat.ini"], stdout=open(W + "/chat.log", "w"), stderr=subprocess.STDOUT)
atexit.register(chat.kill)
CH = "http://127.0.0.1:%d" % CHPORT
for _ in range(60):
    try: urllib.request.urlopen(CH + "/healthz", timeout=1); break
    except Exception: time.sleep(0.2)
try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None
if sync_playwright:
    with sync_playwright() as pw:
        br = pw.chromium.launch(); pg = br.new_page(viewport={"width": 1280, "height": 760}); errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.goto(CH + "/voice"); pg.wait_for_timeout(1800)
        txt = lambda sel: pg.evaluate("(document.querySelector(%r)||{}).textContent||''" % sel)
        vis = lambda sel: pg.evaluate("getComputedStyle(document.querySelector(%r)).display !== 'none'" % sel)
        check("page, no node: NO MESH NODE note, MESH VOICE · NO NODE, Mumble LINKED, no 'stopped' banner",
              vis("#nomesh") and "NO MESH NODE" in txt("#nomesh") and "NO NODE" in txt("#pmesh") and "LINKED" in txt("#pmum")
              and not vis("#down") and not errs, (txt("#nomesh"), txt("#pmesh"), txt("#pmum"), errs))
        pg.click('.tg[data-tg="TG1"]'); pg.wait_for_timeout(300)
        check("page, no node: PTT row says Mumble only", "Mumble only" in txt("#pttval"), txt("#pttval"))
        pg.click('.tg[data-tg="TG3"]'); pg.wait_for_timeout(300); pg.click("#pttbtn"); pg.wait_for_timeout(1500)
        check("page, no node: on TG3 (not bridged) the row warns nobody would hear", "nobody would hear you" in txt("#pttval"), txt("#pttval"))
        FAKE_IP["ip"] = "127.0.0.1"; pg.wait_for_timeout(1800)
        check("page, node back: the note goes away", not vis("#nomesh") and "NO NODE" not in txt("#pmesh"), (txt("#pmesh"),))
        br.close()
else:
    print("SKIP  Voice page browser checks: playwright not installed")
chat.terminate()

# meshtoc check line
import contextlib, io, importlib.util
spec = importlib.util.spec_from_file_location("mmb", PKG + "/tools/manet_mesh_bridge.py"); mmb = importlib.util.module_from_spec(spec); spec.loader.exec_module(mmb)
FAKE_IP["ip"] = None; gw.write_status()
ini = W + "/check.ini"
open(ini, "w").write("[voice]\nVOICE = on\nMUMBLE = 127.0.0.1:%d\nMUMBLE_CERT_SHA256 = %s\nSTATUS_FILE = %s\nDECK_PTT = off\n" % (MPORT, PIN, STATUS))
mmb.CONFIG = ini; mmb.results.clear(); buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    mmb.check_voice(mmb.load_cfg())
out = buf.getvalue()
check("meshtoc check: says Mumble only / no mesh node, and TG1 linked", "no mesh node" in out and "Mumble only" in out and "TG1" in out and "FAIL" not in out, out)

gw.close()
_stop_murmur()
print("%d/%d passed" % (sum(res), len(res)))
os._exit(0 if all(res) else 1)
