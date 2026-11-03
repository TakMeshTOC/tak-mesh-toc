"""TAK MESH TOC: the one-config-file behaviour, end to end.
  - deckcommon.apply_deck: [deck] wins, real conflicts reported, built-in defaults silent, MESH_ADDR auto reaches every program
  - ONE deck.ini runs the REAL bridge, Deck Chat and dashboard:
      bridge identity to a fake TAK server and chat's mesh position report carry the SAME
      callsign / UID / symbol / team from [deck]; a conflicting [bridge] value is ignored and logged;
      chat's own report is not forwarded (DECK once on the server); dashboard reads [bridge] + [deck];
      the status file carries the suite version
Usage: python3 suite_test.py [work dir]"""
import configparser, json, os, re, socket, stat, subprocess, sys, threading, time
HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
sys.path.insert(0, PKG + "/common")
import deckcommon as dk

W = sys.argv[1] if len(sys.argv) > 1 else "/tmp/manet_mesh_bridge_suite_test"
os.makedirs(W, exist_ok=True)
res = []
def check(n, c, x=""):
    res.append(bool(c)); print(("PASS  " if c else "FAIL  ") + n + ("" if c else "  " + str(x)[:400]), flush=True)
def wait(cond, t=15.0):
    end = time.time() + t
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.2)
    return False

# ------------------------------------------------------------------ 1. apply_deck unit checks
cp = configparser.ConfigParser(interpolation=None); cp.optionxform = str
cp.read_string("[deck]\nCALLSIGN = RAVEN\nMESH_ADDR = auto\nGPS = off\n[bridge]\nBRIDGE_CALLSIGN = OTHER\nPYTAK_MULTICAST_LOCAL_ADDR = 10.41.0.60\n[chat]\nCALLSIGN = DECK\n")
nb = dk.apply_deck(cp, "bridge", "bridge")
check("unit: [deck] callsign wins in [bridge]", cp.get("bridge", "BRIDGE_CALLSIGN") == "RAVEN")
check("unit: the conflict is reported", any("BRIDGE_CALLSIGN = OTHER ignored" in n for n in nb), nb)
check("unit: MESH_ADDR = auto reaches the bridge (it resolves it from the mesh port)", cp.get("bridge", "PYTAK_MULTICAST_LOCAL_ADDR") == "auto")
nc = dk.apply_deck(cp, "chat", "chat", {"CALLSIGN": "DECK", "MESH_LOCAL_ADDR": "auto"})
check("unit: chat value equal to its built-in default is replaced silently", cp.get("chat", "CALLSIGN") == "RAVEN" and not nc, nc)
check("unit: MESH_ADDR = auto reaches chat", cp.get("chat", "MESH_LOCAL_ADDR") == "auto")
nd = dk.apply_deck(cp, "dashboard", "dashboard")
check("unit: dashboard gets GPS only (no callsign keys)", cp.get("dashboard", "GPS") == "off" and not cp.has_option("dashboard", "BRIDGE_CALLSIGN"))
cp2 = configparser.ConfigParser(); cp2.read_string("[bridge]\nBRIDGE_CALLSIGN = X\n")
check("unit: no [deck] = nothing changes", dk.apply_deck(cp2, "bridge", "bridge") == [] and cp2.get("bridge", "BRIDGE_CALLSIGN") == "X")
check("unit: team icon", dk.team_xml("cyan", "hq") == '<__group name="Cyan" role="HQ"/>' and dk.team_xml("", "") == "")
check("unit: suite version read from VERSION", dk.suite_version() == open(PKG + "/VERSION").read().split()[0])

# ------------------------------------------------------------------ 2. one deck.ini, three real programs
srv = socket.socket(); srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); srv.bind(("127.0.0.1", 0)); srv.listen(5)
sport = srv.getsockname()[1]
server_got = []
def serve():
    while True:
        try:
            c, _ = srv.accept()
        except OSError:
            return
        def rd(c=c):
            while True:
                try:
                    d = c.recv(65536)
                except OSError:
                    return
                if not d:
                    return
                server_got.append(d)
        threading.Thread(target=rd, daemon=True).start()
threading.Thread(target=serve, daemon=True).start()

mesh_sa = []
ms = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
ms.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); ms.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
ms.bind(("239.2.3.1", 6969))
ms.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, socket.inet_aton("239.2.3.1") + socket.inet_aton("127.0.0.1"))
ms.settimeout(0.3)
def mesh_rx():
    while True:
        try:
            mesh_sa.append(ms.recvfrom(65535)[0])
        except socket.timeout:
            continue
        except OSError:
            return
threading.Thread(target=mesh_rx, daemon=True).start()

status = W + "/bridge_status.json"
for f in (status, W + "/hist.jsonl", W + "/ev.jsonl", W + "/dhist.jsonl"):
    if os.path.exists(f):
        os.remove(f)
ini = W + "/deck.ini"
open(ini, "w").write(f"""[deck]
CALLSIGN = RAVEN
UID = RAVEN-7
MESH_ADDR = 127.0.0.1
MAP_TYPE = a-f-G-U-C-I
TEAM = Cyan
ROLE = HQ
GPS = off

[bridge]
COT_URL = tcp://127.0.0.1:{sport}
BRIDGE_CALLSIGN = OTHER
STATUS_FILE = {status}
LOCAL_CHAT_COPY = udp://127.0.0.1:17013
LOCAL_SEND = udp://127.0.0.1:17014

[chat]
LISTEN = 127.0.0.1:8791
HISTORY_FILE = {W}/hist.jsonl
BRIDGE_STATUS_FILE = {status}
DM_PORT = 4252

[dashboard]
PORT = 8792
LISTEN = 127.0.0.1
BRIDGE_INI = {ini}
BRIDGE_STATUS_FILE = {status}
EVENT_FILE = {W}/ev.jsonl
HISTORY_FILE = {W}/dhist.jsonl
GPS = gpsd
""")
env = dict(os.environ, PYTHONWARNINGS="ignore")
procs = [subprocess.Popen([sys.executable, "-S", PKG + "/bridge/tak_mesh_bridge.py", ini], stdout=open(W + "/b.log", "w"), stderr=subprocess.STDOUT, env=env),
         subprocess.Popen([sys.executable, PKG + "/chat/deck_chat.py", "--config", ini], stdout=open(W + "/c.log", "w"), stderr=subprocess.STDOUT, env=env)]
try:
    def server_text():
        return b"".join(server_got).decode("utf-8", "replace")
    ok = wait(lambda: 'uid="RAVEN-7"' in server_text(), 20)
    check("bridge identifies to the server with [deck] UID", ok, server_text()[:300])
    ident = re.search(r'<event[^>]*uid="RAVEN-7".*?</event>', server_text(), re.S)
    ident = ident.group(0) if ident else ""
    check("bridge identity: callsign from [deck], not [bridge]'s OTHER", 'callsign="RAVEN"' in ident and "OTHER" not in ident, ident[:300])
    check("bridge identity: symbol from [deck]", 'type="a-f-G-U-C-I"' in ident, ident[:200])
    check("bridge identity: team icon from [deck]", '<__group name="Cyan" role="HQ"/>' in ident, ident[-200:])
    ok = wait(lambda: any(b'uid="RAVEN-7"' in d for d in mesh_sa), 20)
    sa = next((d.decode() for d in mesh_sa if b'uid="RAVEN-7"' in d), "")
    check("chat's mesh report uses the SAME UID", ok, [d[:120] for d in mesh_sa[:3]])
    check("chat's mesh report: same callsign, symbol, team", 'callsign="RAVEN"' in sa and 'type="a-f-G-U-C-I"' in sa and '<__group name="Cyan" role="HQ"/>' in sa, sa[:400])
    check("chat's mesh report: DM endpoint on MESH_ADDR", "127.0.0.1:4252:tcp" in sa, sa[:400])
    time.sleep(3)
    check("DECK once on the server: chat's own report not forwarded", "4252:tcp" not in server_text(), server_text()[-400:])
    blog = open(W + "/b.log").read()
    check("bridge logs the ignored [bridge] value", "BRIDGE_CALLSIGN = OTHER ignored" in blog, blog[-600:])
    check("bridge logs the suite version", "TAK MESH TOC %s" % dk.suite_version() in blog, blog[:400])
    ok = wait(lambda: os.path.exists(status), 10)
    st = json.load(open(status)) if ok else {}
    check("status file carries the suite version", st.get("suite_version") == dk.suite_version(), st.get("suite_version"))
    clog = open(W + "/c.log").read()
    check("chat: no spurious override warnings (defaults only)", "ignored" not in clog, clog[-500:])
    d = subprocess.run([sys.executable, PKG + "/dashboard/dashboard_server.py", "--config", ini, "--once"], capture_output=True, text=True, timeout=60, env=env)
    check("dashboard runs on the shared deck.ini", d.returncode == 0 and d.stdout.strip().startswith("{"), d.stderr[-400:])
    check("dashboard: [deck] GPS overrides its own and says so", "[dashboard] GPS = gpsd ignored" in d.stderr, d.stderr[-300:])
    snap = json.loads(d.stdout) if d.returncode == 0 else {}
    txt = json.dumps(snap)
    check("dashboard: server port read from [bridge] COT_URL", str(sport) in txt, txt[:300])
    check("dashboard: no GPS tile data ([deck] GPS = off beat [dashboard] GPS = gpsd)", "gps" in snap and snap["gps"] is None, json.dumps(snap.get("gps"))[:200])
finally:
    for p in procs:
        p.terminate()
    for p in procs:
        try:
            p.wait(5)
        except Exception:
            p.kill()

print("\n%d/%d passed" % (sum(res), len(res)))
sys.exit(0 if all(res) else 1)
