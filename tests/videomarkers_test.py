import os as _o, sys as _s  # paths relative to this package
PKG = _o.path.dirname(_o.path.dirname(_o.path.abspath(__file__)))
_s.path[:0] = [PKG + "/vendor", PKG + "/common", PKG + "/bridge", PKG + "/dashboard"]
"""videomarkers_test.py: the deck's streams as tap-to-play VIDEO markers on the mesh (the format iTAK plays from a
marker's Play Video on a mesh without internet). With root, a real send goes out a test port and is NOT looped back
to programs on the deck (the bridge must never forward these to the server).
Usage: python3 -S videomarkers_test.py"""
import json, math, os, re, socket, struct, subprocess, sys, time
import xml.etree.ElementTree as ET
import warnings; warnings.filterwarnings("ignore")
import videomarkers as vm

res = []
def check(n, c, x=""):
    res.append(bool(c)); print(("PASS  " if c else "FAIL  ") + n + ("" if c else "  " + str(x)[:500]), flush=True)

# ---------------- one marker
now = 1_800_000_000.0
x = vm.build_event(vm.marker_uid("My deck!", "RAVEN21"), "RAVEN21", "10.41.0.60", 39.5, -105.0, "on demand", now, now + 600)
e = ET.fromstring(x)
v = e.find("detail/__video"); ce = v.find("ConnectionEntry")
check("marker: valid XML, plain ASCII", x.isascii())
import tak_mesh_bridge as tb
check("...and one valid CoT event for TAK clients", tb.bad_event(x) is None, tb.bad_event(x))
check("...uid from the deck's name and the stream", e.get("uid") == "DECK-Mydeck-VIDEO-RAVEN21", e.get("uid"))
check("...callsign VIDEO <stream>", e.find("detail/contact").get("callsign") == "VIDEO RAVEN21")
check("...the link iTAK plays (url) and ATAK's connection entry", v.get("url") == "rtsp://10.41.0.60:8554/RAVEN21" and
      (ce.get("protocol"), ce.get("address"), ce.get("port"), ce.get("path")) == ("rtsp", "10.41.0.60", "8554", "/RAVEN21"), x)
check("...the format the field test proved (a-f-G-E-S, __video uid + url around a ConnectionEntry)",
      e.get("type") == "a-f-G-E-S" and v.get("uid") == e.get("uid"))
check("...remarks say on demand and how to play", "On demand" in e.find("detail/remarks").text and "Play Video" in e.find("detail/remarks").text)
check("...stale 10 minutes later", e.get("stale") == "2027-01-15T08:10:00.000Z" and e.get("time") == "2027-01-15T08:00:00.000Z", (e.get("time"), e.get("stale")))
odd = vm.build_event("U", 'a"b<c>&d', "10.41.0.60", 1, 2, "live", now, now)
check("odd stream names are escaped (still valid)", tb.bad_event(odd) is None, odd)

# ---------------- placement: 12 m north of the deck, then 12 m apart going east
def dist(a, b):
    R = 6371000.0; p1, p2 = math.radians(a[0]), math.radians(b[0])
    return 2 * R * math.asin(math.sqrt(math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(b[1] - a[1]) / 2) ** 2))
d0, d1 = vm.place(39.5, -105.0, 0), vm.place(39.5, -105.0, 1)
check("first marker 12 m north of the deck (not on top of DECK)", abs(dist((39.5, -105.0), d0) - 12) < 0.3 and d0[0] > 39.5, d0)
check("next one 12 m east of it", abs(dist(d0, d1) - 12) < 0.3 and d1[1] > d0[1], dist(d0, d1))

# ---------------- which streams
paths = [{"name": "live", "ready": True}, {"name": "RAVEN21", "ready": False}, {"name": "drone", "ready": False}]
conf = {"RAVEN21": {"source": "rtsp://user:" + "pw" + "@" + "100.64.0.9:8554/RAVEN21", "sourceOnDemand": True},
        "drone": {"source": "publisher"}, "all_others": {"source": "publisher"},
        "~^cam(.*)$": {"source": "rtsp://x/$G1", "sourceOnDemand": True},
        "live": {"source": "rtsp://x/live", "sourceOnDemand": True},
        "pull": {"source": "rtsp://x/pull", "sourceOnDemand": False}}
check("on-demand source not streaming (or not checked yet): no marker", vm.streams(paths, conf) == [("live", "live")] and
      vm.streams(paths, conf, {"RAVEN21": False}) == [("live", "live")] and vm.streams(paths, conf, {"RAVEN21": None}) == [("live", "live")])
st = vm.streams(paths, conf, {"RAVEN21": True})
check("on-demand source streaming right now: marker", st == [("RAVEN21", "on demand"), ("live", "live")], st)
check("only RTSP on-demand sources are checked", vm.on_demand_sources(conf) == {"RAVEN21": conf["RAVEN21"]["source"], "live": "rtsp://x/live"})
check("a publish-only path with nobody publishing is left out (nothing to play)", "drone" not in dict(st))
check("regex and catch-all entries are left out", not any(n.startswith("~") or n == "all_others" for n, _ in st))
check("no config API: live streams only", vm.streams(paths, {}) == [("live", "live")])
check("the source's login never reaches the marker", (b"pw" + b"@") not in vm.build_event("U", "RAVEN21", "10.41.0.60", 1, 2, "on demand", now, now))

# ---------------- "is it streaming?": one RTSP DESCRIBE, Basic or Digest login, no video
import threading, hashlib as _h, base64 as _b
from socketserver import ThreadingTCPServer, StreamRequestHandler
SEEN = []
class RTSP(StreamRequestHandler):
    def handle(self):
        while True:
            req, hdr = self.rfile.readline().decode(), {}
            if not req:
                return
            while True:
                l = self.rfile.readline().decode().rstrip("\r\n")
                if not l:
                    break
                k, _, v = l.partition(":"); hdr[k.strip().lower()] = v.strip()
            SEEN.append((req, dict(hdr)))
            path = req.split()[1].rsplit("/", 1)[-1]
            cseq, mode = hdr.get("cseq", "1"), path.split("_")[0]
            auth = hdr.get("authorization", "")
            ok_basic = auth == "Basic " + _b.b64encode(b"user:s3cret").decode()
            if mode == "digest" and auth.startswith("Digest"):
                kv = dict(re.findall(r'(\w+)="([^"]*)"', auth))
                ha1 = _h.md5(b"user:MediaMTX:s3cret").hexdigest(); ha2 = _h.md5(("DESCRIBE:" + kv["uri"]).encode()).hexdigest()
                good = kv.get("response") == _h.md5(("%s:abc123:%s" % (ha1, ha2)).encode()).hexdigest()
            else:
                good = ok_basic
            if mode in ("basic", "digest") and not good:
                ch = 'Basic realm="MediaMTX"' if mode == "basic" else 'Digest realm="MediaMTX", nonce="abc123"'
                self.wfile.write(("RTSP/1.0 401 Unauthorized\r\nCSeq: %s\r\nWWW-Authenticate: %s\r\n\r\n" % (cseq, ch)).encode()); continue
            if path.endswith("off"):
                self.wfile.write(("RTSP/1.0 404 Not Found\r\nCSeq: %s\r\n\r\n" % cseq).encode()); continue
            sdp = "v=0\r\nm=video 0 RTP/AVP 96\r\n"
            self.wfile.write(("RTSP/1.0 200 OK\r\nCSeq: %s\r\nContent-Type: application/sdp\r\nContent-Length: %d\r\n\r\n%s"
                              % (cseq, len(sdp), sdp)).encode())
ThreadingTCPServer.allow_reuse_address = True
rs = ThreadingTCPServer(("127.0.0.1", 0), RTSP); rs.daemon_threads = True
threading.Thread(target=rs.serve_forever, daemon=True).start()
RP = rs.server_address[1]
U = lambda path, cred="user:s3cret@": "rtsp://%s127.0.0.1:%d/%s" % (cred, RP, path)
check("streaming, no login needed: live", vm.rtsp_live(U("open_on", "")) is True)
check("not streaming (404): not live", vm.rtsp_live(U("open_off", "")) is False)
check("Basic login: live", vm.rtsp_live(U("basic_on")) is True)
SEEN.clear()
check("Digest login: live", vm.rtsp_live(U("digest_on")) is True)
check("...the password never goes over the wire with Digest", SEEN and not any("s3cret" in json.dumps(x) for x in SEEN), SEEN)
check("Digest login, stream off: not live", vm.rtsp_live(U("digest_off")) is False)
check("wrong password: not live (never a false marker)", vm.rtsp_live(U("digest_on", "user:nope@")) is False and vm.rtsp_live(U("basic_on", "user:nope@")) is False)
check("only DESCRIBE is ever sent (no video pulled)", all(r.startswith("DESCRIBE ") for r, _ in SEEN), SEEN)
s0 = socket.socket(); s0.bind(("127.0.0.1", 0)); dead = s0.getsockname()[1]; s0.close()
check("server unreachable: unknown (no marker)", vm.rtsp_live("rtsp://127.0.0.1:%d/x" % dead, timeout=1) is None)
check("not an rtsp URL: unknown", vm.rtsp_live("http://127.0.0.1/x") is None)
uw = vm.UpstreamWatch(every=999, probe=lambda u: u.endswith("_on"))
uw.thread = "no thread in this test"
uw.set_sources({"A": U("open_on", ""), "B": U("open_off", "")}); uw.poll()
check("watcher: results per stream", uw.get() == {"A": True, "B": False}, uw.get())
uw.set_sources({"A": U("open_on", "")})
check("watcher: a source taken out of the config is forgotten", uw.get() == {"A": True}, uw.get())

# ---------------- whose stream, and where its marker goes
FAT = {"cs": "RAVEN 21", "uid": "phone-uid", "lat": 39.6, "lon": -104.9, "ts": now - 20, "ip": "", "via": "server"}
MESH = {"cs": "EAGLE 6", "uid": "eagle-uid", "lat": 39.55, "lon": -104.95, "ts": now - 5, "ip": "10.41.0.50", "via": "mesh"}
people = [FAT, MESH]
p, why = vm.find_owner("RAVEN21", people, {})
check("owner by name: RAVEN21 = RAVEN 21 (spaces and case ignored)", p is FAT and "name" in why, why)
p, why = vm.find_owner("cam1", people, {}, publisher_ip="10.41.0.50")
check("owner by IP: a mesh phone publishing straight to the deck", p is MESH and "10.41.0.50" in why, why)
p, why = vm.find_owner("RAVEN21", people, vm.parse_owners("RAVEN21: EAGLE 6, drone1 : X"), publisher_ip="1.2.3.4")
check("a pairing in deck.ini wins over name and IP", p is MESH and "deck.ini" in why, why)
check("VIDEO_OWNERS parsing", vm.parse_owners("RAVEN21: RAVEN 21, drone1 : EAGLE 6,bad, :x") == {"raven21": "RAVEN 21", "drone1": "EAGLE 6"})
p, why = vm.find_owner("RAVEN21", people, {"raven21": "GHOST 1"})
check("paired but that person has no position yet: unknown, says so", p is None and "GHOST 1" in why and "no position" in why, why)
check("nobody matches: unknown", vm.find_owner("random", people, {})[0] is None)
old = dict(FAT, ts=now - 30, cs="RAVEN 21"); newer = dict(FAT, ts=now - 2, lat=40.0, uid="phone-new")
check("two reports for one callsign: the newest position is used", vm.find_owner("RAVEN21", [old, newer], {})[0] is newer)
mk = vm.plan([("RAVEN21", "on demand"), ("cam1", "live"), ("random", "live")], people, {}, {"cam1": "10.41.0.50"},
             (39.5, -105.0), now)
m = {x["name"]: x for x in mk}
check("marker 8 m from the streamer (not on top of their own marker)", abs(vm.metres((FAT["lat"], FAT["lon"]), (m["RAVEN21"]["lat"], m["RAVEN21"]["lon"])) - 8) < 0.3, m["RAVEN21"])
check("remarks: their callsign and MGRS grid, position fresh", m["RAVEN21"]["remarks"].startswith("RAVEN 21 at 13S ") and "position now" in m["RAVEN21"]["remarks"], m["RAVEN21"]["remarks"])
check("the mesh publisher's stream sits at EAGLE 6", m["cam1"]["owner"] == "EAGLE 6")
check("unknown streamer: at the deck, 12 m north, says why", m["random"]["owner"] is None and "unknown" in m["random"]["remarks"] and
      abs(vm.metres((39.5, -105.0), (m["random"]["lat"], m["random"]["lon"])) - 12) < 0.3, m["random"])
stale_p = dict(FAT, ts=now - 3600)
r = vm.plan([("RAVEN21", "live")], [stale_p], {}, {}, None, now)[0]["remarks"]
check("an old position says 'last known position HH:MMZ'", "last known position %s" % time.strftime("%H:%MZ", time.gmtime(now - 3600)) in r, r)
check("no deck GPS: streams with a located owner still show; unknown ones wait",
      [x["name"] for x in vm.plan([("RAVEN21", "live"), ("random", "live")], people, {}, {}, None, now)] == ["RAVEN21"])
two = vm.plan([("RAVEN21", "live"), ("raven-21", "live")], people, {}, {}, None, now)
check("two streams from one person sit side by side", vm.metres((two[0]["lat"], two[0]["lon"]), (two[1]["lat"], two[1]["lon"])) > 7, two)

# ---------------- when it sends
sent = []
w = vm.VideoMarkers(host="deck", refresh=180, stale=600, send=lambda ip, data: sent.append((ip, ET.fromstring(data))))
pos = (39.5, -105.0)
st2 = [("RAVEN21", "on demand"), ("live", "live")]
def P(t, who=people):
    return vm.plan(st2, who, {}, {}, pos, t)
ch = w.tick(now, P(now), "10.41.0.60", pos)
check("first pass: every marker sent, out the mesh address", len(sent) == 2 and all(ip == "10.41.0.60" for ip, _ in sent), sent)
check("...reported as added (for the event log)", sorted(ch) == [("RAVEN21", "added", "on demand"), ("live", "added", "live")], ch)
check("...RAVEN21's marker is at RAVEN 21, with the grid in its remarks",
      any(e.get("uid").endswith("RAVEN21") and abs(float(e.find("point").get("lat")) - 39.6) < 0.001 and "RAVEN 21 at 13S" in e.find("detail/remarks").text
          for _, e in sent), [(e.find("point").attrib, e.find("detail/remarks").text) for _, e in sent])
sent.clear(); w.tick(now + 2, P(now + 2), "10.41.0.60", pos)
check("next pass: sent once more (a radio mesh can drop one)", len(sent) == 2, len(sent))
sent.clear(); w.tick(now + 4, P(now + 4), "10.41.0.60", pos); w.tick(now + 100, P(now + 100), "10.41.0.60", pos)
check("then quiet: nothing between refreshes (no flooding)", sent == [], len(sent))
w.tick(now + 182, P(now + 182), "10.41.0.60", pos)
check("refresh after 3 minutes keeps them alive (stale moves 10 min ahead)", len(sent) == 2 and
      sent[0][1].get("stale") == vm._ts(now + 182 + 600), [s[1].get("stale") for s in sent])
check("a stream on for an hour keeps its marker: 20 refreshes, none missed",
      all(len(w.tick(now + 182 + 180 * k, P(now + 182 + 180 * k), "10.41.0.60", pos)) == 0 for k in range(1, 21)) and w.last == now + 182 + 180 * 20)
t0 = w.last
walk = lambda d: [dict(FAT, lat=FAT["lat"] + d / 111320.0, ts=t0), MESH]
sent.clear(); w.tick(t0 + 20, P(t0 + 20, walk(10)), "10.41.0.60", pos)
check("streamer walks 10 m: no resend (under 25 m)", sent == [], len(sent))
w.tick(t0 + 25, P(t0 + 25, walk(30)), "10.41.0.60", pos)
check("walks 30 m: only their marker is resent, at the new spot", len(sent) == 1 and sent[0][1].get("uid").endswith("RAVEN21") and
      abs(float(sent[0][1].find("point").get("lat")) - (FAT["lat"] + (30 + 8) / 111320.0)) < 1e-5, [s[1].get("uid") for s in sent])
sent.clear(); w.tick(t0 + 30, P(t0 + 30, walk(60)), "10.41.0.60", pos)
check("keeps walking: at most one resend every 15 s", sent == [], len(sent))
w.tick(t0 + 41, P(t0 + 41, walk(60)), "10.41.0.60", pos)
check("...then follows again", len(sent) == 1, len(sent))
sent.clear(); t2 = now + 8000
ch = w.tick(t2, vm.plan([("live", "live")], people, {}, {}, pos, t2), "10.41.0.60", pos)
gone = [s for s in sent if s[1].get("uid").endswith("RAVEN21")]
check("a stream that goes away: its marker is sent stale right away, where it was (phones drop it now)",
      gone and gone[0][1].get("stale") == vm._ts(t2) and ("RAVEN21", "removed", "on demand") in ch, (ch, [g[1].get("stale") for g in gone]))
check("...the rest stay", any(s[1].get("uid").endswith("live") and s[1].get("stale") == vm._ts(t2 + 600) for s in sent))
sent.clear()
check("live <-> on demand change is a change (sent at once)", w.tick(t2 + 5, vm.plan([("live", "on demand")], people, {}, {}, pos, t2 + 5), "10.41.0.60", pos) and len(sent) == 1)
sent.clear(); w2 = vm.VideoMarkers(host="deck", send=lambda ip, d: sent.append(d))
w2.tick(now, P(now), None, pos)
check("no mesh address (node unplugged): nothing sent, says why", sent == [] and w2.status["state"] == "no mesh address", w2.status)
w2.tick(now, vm.plan([("random", "live")], people, {}, {}, None, now), "10.41.0.60", None)
check("unknown streamer and no deck GPS: nothing sent, says waiting for GPS", sent == [] and "GPS" in w2.status["state"], w2.status)
w3 = vm.VideoMarkers(host="deck", send=lambda ip, d: (_ for _ in ()).throw(OSError(101, "Network is unreachable")))
check("a send error is reported, never raised", w3.tick(now, P(now), "10.41.0.60", pos) == [] and "send failed" in w3.status["state"], w3.status)

# ---------------- the bridge remembers where people are
tb.Peers.pos.clear()
tb.Peers.saw_position(b'<event version="2.0" uid="phone-uid" type="a-f-G-U-C"><point lat="39.6" lon="-104.9" hae="0"/>'
                      b'<detail><contact callsign="RAVEN 21" endpoint="*:-1:stcp"/></detail></event>', "server")
tb.Peers.saw_position(b'<event version="2.0" uid="ip1" type="a-f-G-U-C"><point lat="39.55" lon="-104.95" hae="0"/>'
                      b'<detail><contact callsign="EAGLE 6" endpoint="10.41.0.50:4242:tcp"/></detail></event>', "mesh")
tb.Peers.saw_position(b'<event uid="z" type="a-f-G-U-C"><point lat="0" lon="0"/><detail><contact callsign="NOWHERE"/></detail></event>', "mesh")
pp = {d["cs"]: d for d in tb.Peers.pos.values()}
check("bridge: server and mesh people with positions; a mesh phone's IP from its endpoint",
      set(pp) == {"RAVEN 21", "EAGLE 6"} and pp["EAGLE 6"]["ip"] == "10.41.0.50" and pp["RAVEN 21"]["via"] == "server", pp)
check("bridge: a 0,0 position is ignored (no fix)", "NOWHERE" not in pp)
bsrc = open(PKG + "/bridge/tak_mesh_bridge.py").read()
check("bridge: both directions record positions, and the status file carries them",
      'Peers.saw_position(data, "mesh")' in bsrc and 'Peers.saw_position(data, "server")' in bsrc and '"people": [dict(d)' in bsrc)
tb.Peers.POS_MAX = 3
for i in range(10):
    tb.Peers.saw_position(b'<event uid="u%d" type="a-f-G-U-C"><point lat="1" lon="1"/><detail><contact callsign="C%d"/></detail></event>' % (i, i), "mesh")
check("bridge: the table is capped (oldest forgotten)", len(tb.Peers.pos) == 3 and "u9" in tb.Peers.pos)
tb.Peers.POS_MAX = 500

# ---------------- the dashboard
import dashboard_server as ds
class Ev:
    def __init__(self): self.items = []
    def add(self, level, text, cat="system"): self.items.append((level, text, cat))
class Stub:
    errors = []
    def can_restart(self): return True
class Gps:
    def position(self, max_age=10.0): return False, {"lat": 39.5, "lon": -105.0}
c = ds.Collector(dict(ds.DEFAULTS, GPS="off", SERVER_HOST="127.0.0.1", SERVER_PORT="1", MESH_DISCOVERY="off"), Ev(), alerts=Stub(), control=Stub())
got = []
c.vmark.send = lambda ip, d: got.append(d)
c.gps = Gps()
c.mtx_conf = lambda: conf
c.people = lambda: people
c.upstream.state = {"RAVEN21": True, "live": True}
c.upstream.thread = "no thread in this test"
video = {"api_ok": True, "paths": paths}
st1 = c.video_markers(video, {"iface_ip": "10.41.0.60"}, now)
check("dashboard: markers on, 2 streams, sent", st1["state"] == "on" and st1["count"] == 2 and len(got) == 2, st1)
check("dashboard: event log says which markers went on the mesh",
      ("info", "Video marker on the mesh: VIDEO RAVEN21 (on demand, RAVEN 21)", "video") in c.events.items, c.events.items)
check("dashboard: 1 of 2 at the person streaming", st1["located"] == 1, st1)
check("dashboard: everyone's positions stay out of the page's feed", "people" not in json.dumps(ds.mock_status("hop", Ev())["bridge"]))
snap = ds.mock_status("hop", Ev())
items = ds.setup_checks(dict(ds.DEFAULTS), snap, Stub(), Stub())
check("SETUP lists the video markers", any(i["title"] == "Video markers on the mesh" and i["ok"] for i in items))
off = ds.Collector(dict(ds.DEFAULTS, GPS="off", SERVER_HOST="127.0.0.1", SERVER_PORT="1", MESH_DISCOVERY="off", VIDEO_MARKERS="off"),
                   Ev(), alerts=Stub(), control=Stub())
check("VIDEO_MARKERS = off: nothing", off.vmark is None and off.video_markers(video, {"iface_ip": "10.41.0.60"}, now)["state"] == "off")
c.gps = None; c.vmark = vm.VideoMarkers(send=lambda ip, d: got.append(d)); c.cfg = dict(c.cfg, VIDEO_MARKER_LAT="39.4", VIDEO_MARKER_LON="-105.1")
check("no GPS: the fixed VIDEO_MARKER_LAT/LON position is used", c.video_markers(video, {"iface_ip": "10.41.0.60"}, now)["state"] == "on")
src = open(PKG + "/dashboard/dashboard_server.py").read()
check("collect() runs it every pass", 'video["markers"] = self.video_markers(video, deck, now)' in src)

# ---------------- a real send (root): out the mesh port, not back to programs on the deck
if os.geteuid() != 0 or subprocess.run(["ip", "link", "add", "mmbvm0", "type", "veth", "peer", "name", "mmbvm1"], capture_output=True).returncode:
    print("(real multicast part skipped: needs root to add a test port)")
else:
    NS = "mmbvmns"
    def ip(*a): subprocess.run(["ip"] + list(a), capture_output=True)
    try:
        ip("netns", "add", NS); ip("link", "set", "mmbvm1", "netns", NS)
        ip("addr", "add", "10.99.9.5/24", "dev", "mmbvm0"); ip("link", "set", "mmbvm0", "up"); ip("link", "set", "mmbvm0", "multicast", "on")
        nsx = ["ip", "netns", "exec", NS]
        for cmd in (["ip", "addr", "add", "10.99.9.7/24", "dev", "mmbvm1"], ["ip", "link", "set", "mmbvm1", "up"], ["ip", "link", "set", "lo", "up"],
                    ["ip", "link", "set", "mmbvm1", "multicast", "on"]):
            subprocess.run(nsx + cmd)
        recv = ("import socket,struct,sys;s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);"
                "s.bind(('',6969));s.setsockopt(socket.IPPROTO_IP,socket.IP_ADD_MEMBERSHIP,struct.pack('4s4s',socket.inet_aton('239.2.3.1'),socket.inet_aton(sys.argv[1])));"
                "s.settimeout(4);d=s.recv(65535);print(d.decode()[:200])")
        phone = subprocess.Popen(nsx + [sys.executable, "-c", recv, "10.99.9.7"], stdout=subprocess.PIPE, text=True)
        local = subprocess.Popen([sys.executable, "-c", recv, "10.99.9.5"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        time.sleep(1.0)
        real = vm.VideoMarkers(host="deck")
        tt = time.time()
        real.tick(tt, vm.plan([("RAVEN21", "on demand")], [], {}, {}, (39.5, -105.0), tt), "10.99.9.5", (39.5, -105.0))
        po, _ = phone.communicate(timeout=8); lo_out, lo_err = local.communicate(timeout=8)
        check("real send: a phone on the mesh port receives the marker", "DECK-deck-VIDEO-RAVEN21" in po, po)
        check("real send: NOT looped back to the deck itself (the bridge never sees it, so never sends it to the server)",
              "VIDEO" not in lo_out and "timed out" in lo_err, (lo_out, lo_err[-120:]))
    finally:
        ip("netns", "del", NS); ip("link", "del", "mmbvm0")

print("%d/%d passed" % (sum(res), len(res)))
sys.exit(0 if all(res) else 1)
