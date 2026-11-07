"""Large but LEGITIMATE traffic through the real bridge + Deck Chat, to prove the 1.0.4 scan limit restricts nothing:
routes / shapes near the 64 KB UDP limit, data-package (file share) announcements, pretty-printed XML, escaped
characters, a long ATAK chat, the server->mesh return path, and the over-64 KB case. Then the attack packet, timed.
Usage: python3 big_test.py [package dir] [work dir]"""
import json, os, socket, struct, subprocess, sys, threading, time, urllib.request
PKG = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
W = sys.argv[2] if len(sys.argv) > 2 else "/tmp/mmb_big_test"
os.makedirs(W, exist_ok=True)
res = []
def check(n, c, x=""):
    res.append(bool(c)); print(("PASS  " if c else "FAIL  ") + n + ("" if c else "  " + str(x)[:400]), flush=True)
def wait(cond, t=10):
    end = time.time() + t
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.1)
    return False

# fake TAK server: records everything, can push to the bridge
srv = socket.socket(); srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); srv.bind(("127.0.0.1", 0)); srv.listen(5)
sport = srv.getsockname()[1]; got = bytearray(); conns = []; lk = threading.Lock()
def serve():
    while True:
        try:
            c, _ = srv.accept()
        except OSError:
            return
        conns.append(c)
        def rd(c=c):
            while True:
                try:
                    d = c.recv(262144)
                except OSError:
                    return
                if not d:
                    return
                with lk:
                    got.extend(d)
        threading.Thread(target=rd, daemon=True).start()
threading.Thread(target=serve, daemon=True).start()
def server_has(b):
    with lk:
        return b in got

# mesh listener (what mesh phones would hear on 239.2.3.1:6969)
heard = []
ml = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
ml.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
ml.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
ml.bind(("", 6969))
ml.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, struct.pack("4s4s", socket.inet_aton("239.2.3.1"), socket.inet_aton("127.0.0.1")))
def listen():
    while True:
        try:
            d, _ = ml.recvfrom(70000)
        except OSError:
            return
        heard.append(d)
threading.Thread(target=listen, daemon=True).start()

status = W + "/status.json"
for f in (status, W + "/hist.jsonl"):
    if os.path.exists(f):
        os.remove(f)
ini = W + "/deck.ini"
open(ini, "w").write(f"""[deck]
CALLSIGN = DECK
UID = DECK-big
MESH_ADDR = 127.0.0.1
GPS = off
[bridge]
COT_URL = tcp://127.0.0.1:{sport}
STATUS_FILE = {status}
RETURN_PATH = 1
RETURN_TYPES = a-,b-,u-
LOCAL_CHAT_COPY = udp://127.0.0.1:17013
LOCAL_SEND = udp://127.0.0.1:17014
[chat]
LISTEN = 127.0.0.1:8791
HISTORY_FILE = {W}/hist.jsonl
BRIDGE_STATUS_FILE = {status}
DM_PORT = off
SA_SEND = 0
""")
env = dict(os.environ, PYTHONWARNINGS="ignore")
bp = subprocess.Popen([sys.executable, "-S", PKG + "/bridge/tak_mesh_bridge.py", ini], stdout=open(W + "/b.log", "w"), stderr=subprocess.STDOUT, env=env)
cp = subprocess.Popen([sys.executable, PKG + "/chat/deck_chat.py", "--config", ini], stdout=open(W + "/c.log", "w"), stderr=subprocess.STDOUT, env=env)
mc = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
mc.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton("127.0.0.1"))
mc.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
def mesh(d, grp=("239.2.3.1", 6969)):
    mc.sendto(d, grp)
T = 'time="2026-10-22T18:00:00Z" start="2026-10-22T18:00:00Z" stale="2099-01-01T00:00:00Z"'

def route(uid, n):
    pts = "".join('<link uid="%s-wp%d" callsign="WP %d" type="b-m-p-w" point="38.%06d,-104.%06d"/>' % (uid, i, i, i, i) for i in range(n))
    return ('<event version="2.0" uid="%s" type="b-m-r" how="h-e" %s><point lat="38.1" lon="-104.1" hae="0" ce="9" le="9"/>'
            '<detail><contact callsign="Big Route"/>%s<__routeinfo/><link_attr color="-1" method="Driving" direction="Infil" routetype="Primary" order="Ascending Check Points"/>'
            '<remarks>long route</remarks></detail></event>' % (uid, T, pts)).encode()

try:
    wait(lambda: os.path.exists(status), 25)
    wait(lambda: "Listening on udp+ro://239.2.3.1:6969" in open(W + "/b.log").read(), 20)
    time.sleep(1)

    # 1. a ~62 KB route from a mesh phone (near the UDP ceiling)
    n = 400
    while len(route("ROUTE-big", n + 10)) < 62000:
        n += 10
    r = route("ROUTE-big", n)
    mesh(r)
    check("62 KB route (%d waypoints, %d bytes) from the mesh reaches the server" % (r.count(b"<link uid"), len(r)),
          wait(lambda: server_has(b'uid="ROUTE-big"')) and wait(lambda: server_has(b'ROUTE-big-wp%d"' % (r.count(b"<link uid") - 1))),
          "uid seen=%s, server bytes=%d, tail=%r" % (server_has(b'uid="ROUTE-big"'), len(got), bytes(got[-200:])))
    # 2. a big drawn shape (polygon) with many vertices
    verts = "".join('<link point="38.%05d,-104.%05d"/>' % (i, i) for i in range(1800))
    shp = ('<event version="2.0" uid="SHAPE-big" type="u-d-f" how="h-e" %s><point lat="38" lon="-104" hae="0" ce="9" le="9"/>'
           '<detail>%s<contact callsign="Big Shape"/><strokeColor value="-1"/><remarks/></detail></event>' % (T, verts)).encode()
    mesh(shp)
    check("%d KB polygon (1800 vertices) reaches the server, last vertex intact" % (len(shp) // 1000),
          wait(lambda: server_has(b'uid="SHAPE-big"')) and wait(lambda: server_has(b'38.01799,-104.01799')))
    # 3. data package (file share) announcement: the package itself is fetched over HTTP; this CoT just points at it
    fs = ('<event version="2.0" uid="FS-1" type="b-f-t-r" how="h-e" %s><point lat="0" lon="0" hae="0" ce="9" le="9"/><detail>'
          '<fileshare filename="Mission_Package_Sector_7_Overlays_and_Imagery_2026-10-22.zip" name="Sector 7 package" '
          'senderCallsign="ALPHA 1-5" senderUid="ANDROID-mesh1" senderUrl="http://10.41.0.20:8080/getfile?file=/sdcard/atak/tools/datapackage/transfer/abc123/Mission_Package_Sector_7.zip&amp;sender=ALPHA%%201-5" '
          'sha256="%s" sizeInBytes="52428800"/><ackrequest ackrequested="true" tag="Sector 7 package" uid="ack-1"/></detail></event>' % (T, "a" * 64)).encode()
    mesh(fs)
    # (3.1.15: a file notice is never forwarded as it is, its link only works on the mesh; the deck reads the whole
    # notice and carries files up to FILE_MAX_MB itself. This one is 50 MB, so it's refused and shown in Ops.)
    def files_fault():
        try:
            f = (json.load(open(status)).get("faults") or {}).get("files") or {}
            return "too big: 50.0 MB" in f.get("last", "") and "Mission_Package_Sector_7_Overlays_and_Imagery_2026-10-22.zip" in f.get("last", "")
        except (OSError, ValueError):
            return False
    check("data-package (file share) announcement for a 50 MB package is read whole: refused as too big, shown in Ops, not sent raw",
          wait(files_fault, 15) and not server_has(b'sizeInBytes="52428800"'))
    # 4. pretty-printed XML (newlines/tabs inside tags) and escaped characters in a callsign
    pp = ('<event\n\tversion="2.0"\n\tuid="ANDROID-pretty"\n\ttype="a-f-G-U-C"\n\thow="m-g"\n\t%s>\n<point lat="38.2" lon="-104.2" hae="0" ce="9" le="9"/>\n'
          '<detail>\n\t<contact\n\t\tcallsign="Q&amp;A &lt;1&gt;"\n\t\tendpoint="10.41.0.77:4242:tcp"/>\n</detail>\n</event>' % T.replace(" ", "\n\t")).encode()
    mesh(pp)
    check("pretty-printed SA with escaped callsign reaches the server", wait(lambda: server_has(b'uid="ANDROID-pretty"')))
    def peers_has():
        try:
            return "ANDROID-pretty" in open(status).read()
        except OSError:
            return False
    # 5. the longest chat ATAK would send (ATAK has no hard cap; 4000 chars is far past normal use)
    long_txt = ("Long msg " * 445)[:4000]
    chat = ('<event version="2.0" uid="GeoChat.ANDROID-mesh1.All Chat Rooms.m1" type="b-t-f" how="h-g-i-g-o" %s>'
            '<point lat="38" lon="-104" hae="0" ce="9" le="9"/><detail><__chat parent="RootContactGroup" groupOwner="false" '
            'messageId="m1" chatroom="All Chat Rooms" id="All Chat Rooms" senderCallsign="ALPHA 1-5"><chatgrp uid0="ANDROID-mesh1" '
            'uid1="All Chat Rooms" id="All Chat Rooms"/></__chat><link uid="ANDROID-mesh1" type="a-f-G-U-C" relation="p-p"/>'
            '<remarks source="BAO.F.ATAK.ANDROID-mesh1" to="All Chat Rooms" time="2026-10-22T18:00:00Z">%s</remarks></detail></event>' % (T, long_txt)).encode()
    mesh(chat, ("224.10.10.1", 17012))
    def chat_ok():
        try:
            return long_txt[-50:] in urllib.request.urlopen("http://127.0.0.1:8791/api/messages?after=0", timeout=2).read().decode()
        except Exception:
            return False
    check("4000-character chat shows in Deck Chat in full", wait(chat_ok))
    check("...and reaches the server in full", wait(lambda: server_has(long_txt[-50:].encode())))
    # 6. server -> mesh: a big route from a server-side user is relayed onto the mesh
    rs = route("ROUTE-srv", 450)
    for c in conns:
        c.sendall(rs)
    check("%d KB route from the server is relayed onto the mesh intact" % (len(rs) // 1000),
          wait(lambda: any(b'uid="ROUTE-srv"' in h and b'ROUTE-srv-wp449"' in h for h in heard)))
    # 7. bigger than one mesh packet from the server (83 KB), then absurdly big (5 MB): the bridge must stay up
    def server_then_small(blob, tag):
        for c in list(conns):
            try:
                c.sendall(blob)
            except OSError:
                pass
        time.sleep(2)
        small = ('<event version="2.0" uid="ANDROID-%s" type="a-f-G-U-C" how="m-g" %s><point lat="38" lon="-104" hae="0" ce="9" le="9"/>'
                 '<detail><contact callsign="%s"/></detail></event>' % (tag, T, tag)).encode()
        for c in list(conns):
            try:
                c.sendall(small)
            except OSError:
                pass
        return wait(lambda: any(("ANDROID-" + tag).encode() in h for h in heard), 15)
    nconn = len(conns)
    huge = route("ROUTE-huge", 900)
    ok = server_then_small(huge, "after83k")
    log = open(W + "/b.log").read()
    check("%d KB event from the server: bridge stays up, same connection, next event still relayed" % (len(huge) // 1000),
          ok and bp.poll() is None and len(conns) == nconn, log[-500:])
    check("...and the log says it was too big for one mesh packet", "bigger than one mesh packet" in log, log[-300:])
    giant = route("ROUTE-giant", 50000)
    ok = server_then_small(giant, "after5m")
    log = open(W + "/b.log").read()
    check("%.1f MB event from the server: skipped, bridge stays up, next event still relayed" % (len(giant) / 1e6),
          ok and bp.poll() is None and len(conns) == nconn and "Skipped an event from the server bigger than 4 MB" in log, log[-500:])
    # 8. the attack packet: time until the next real position report gets through
    t0 = time.time()
    mesh(b"<event " * 9000)
    ok_sa = ('<event version="2.0" uid="ANDROID-after" type="a-f-G-U-C" how="m-g" %s><point lat="38" lon="-104" hae="0" ce="9" le="9"/>'
             '<detail><contact callsign="AFTER" endpoint="10.41.0.9:4242:tcp"/></detail></event>' % T).encode()
    mesh(ok_sa)
    wait(lambda: server_has(b'uid="ANDROID-after"'), 30)
    dt = time.time() - t0
    print("   attack packet then a real report: real report reached the server after %.2f s" % dt, flush=True)
    check("real traffic is not held up by the attack packet (< 0.5 s)", dt < 0.5, "%.2f s" % dt)
    check("bridge and Deck Chat still running", bp.poll() is None and cp.poll() is None)
    print("   bridge counters:", json.dumps(json.load(open(status)).get("mesh")), flush=True)
finally:
    for p in (bp, cp):
        p.terminate()
    for p in (bp, cp):
        try:
            p.wait(5)
        except Exception:
            p.kill()
print("\n%d/%d passed" % (sum(res), len(res)))
sys.exit(0 if all(res) else 1)
