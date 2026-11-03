import os as _o, sys as _s  # paths relative to this package
PKG = _o.path.dirname(_o.path.dirname(_o.path.abspath(__file__)))
_s.path[:0] = [PKG + "/vendor", PKG + "/common", PKG + "/bridge", PKG + "/dashboard", PKG + "/tools"]
"""meshwatch_test.py: Ops lists the mesh's own nodes (OpenMANET alfred), their health (no-login status call) and
the radio links between them; the DECK status line and meshtoc check use the same data.
Records are built the way openmanetd builds them. With root and an alfred binary (ALFRED_BIN or alfred on PATH) a real
alfred server answers in a network namespace (IPv4 mode: the request and reply format is the same as on IPv6).
Usage: python3 -S meshwatch_test.py"""
import io, json, os, shutil, socket, struct, subprocess, sys, tempfile, threading, time
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer
import warnings; warnings.filterwarnings("ignore")
import meshwatch as mw

res = []
def check(n, c, x=""):
    res.append(bool(c)); print(("PASS  " if c else "FAIL  ") + n + ("" if c else "  " + str(x)[:500]), flush=True)


# ---------------- records the way openmanetd marshals them
def f_str(n, v):
    b = v.encode()
    return bytes([n << 3 | 2, len(b)]) + b
def f_var(n, x):
    out = bytes([n << 3])
    while True:
        c, x = x & 0x7F, x >> 7
        if x:
            out += bytes([c | 0x80])
        else:
            return out + bytes([c])
def f_msg(n, b):
    return bytes([n << 3 | 2, len(b)]) + b
def node_rec(mac, name, ip):
    return f_str(1, mac) + f_str(2, name) + f_str(3, ip)
def nbr_rec(mac, name, nbrs, ifmacs=()):
    b = f_str(1, mac) + f_str(2, name) + f_var(3, 5)
    for m, kbps, seen in nbrs:
        b += f_msg(4, f_str(1, m) + f_str(2, "wlh0") + f_var(4, seen) + f_var(5, kbps))
    for m in ifmacs:
        b += f_str(7, m)
    return b
M1, M1W, M2, M2W, M3W = "f2:00:00:00:00:01", "02:00:00:00:01:01", "f2:00:00:00:00:02", "02:00:00:00:02:02", "02:00:00:00:03:03"
P1, P2 = bytes.fromhex("f20000000001"), bytes.fromhex("f20000000002")
DATA = {
    (102, P1): node_rec(M1, "NODE_1", "10.41.0.1"),
    (102, P2): node_rec(M2, "NODE_2", "10.41.254.10"),
    (103, P1): nbr_rec(M1, "NODE_1", [(M2W, 7100, 300), (M3W, 900, 200)], [M1W]),
    (103, P2): nbr_rec(M2, "NODE_2", [(M1W, 7400, 400)], [M2W]),
}

# ---------------- parsing
nodes, links = mw.parse_records(DATA)
check("node list: names and IPs from the node records", [(n["name"], n["ip"]) for n in nodes] == [("NODE_1", "10.41.0.1"), ("NODE_2", "10.41.254.10")], nodes)
check("one link per node pair, named through each node's radio MACs", [(l["a"], l["b"]) for l in links] == [("NODE_1", "NODE_2")], links)
check("...the slower direction is the figure shown (7.1 not 7.4 Mbit/s)", links and links[0]["kbps"] == 7100, links)
check("a neighbor whose radio isn't any listed node is left out (no name to show)", len(links) == 1)
stale = dict(DATA); stale[(103, P1)] = nbr_rec(M1, "NODE_1", [(M2W, 7100, 60000)], [M1W]); stale[(103, P2)] = nbr_rec(M2, "NODE_2", [], [M2W])
check("a neighbor not heard for a minute is not a live link", mw.parse_records(stale)[1] == [])
check("protobuf reader stops quietly on junk", isinstance(mw.pb(b"\x0a\xff\xff\x01"), dict) and mw.parse_records({(102, P1): b"\xff" * 9}) == ([], []))
pkt = struct.pack("!BBHHH", 0, 0, 0, 1, 0)
for (t, pub), v in DATA.items():
    pkt += pub + struct.pack("!BBH", t, 1, len(v)) + v
got = {}; mw.parse_push(pkt, got)
check("alfred PUSH_DATA: every record read back", got == DATA)
got = {}; mw.parse_push(pkt[:-5], got)
check("a cut-off record is dropped, the whole ones kept", len(got) == 3, len(got))
check("grades: GOOD at 3, OK from 1, POOR below, NONE without a figure",
      [mw.grade(x) for x in (7.1, 3.0, 2.9, 1.0, 0.6, None)] == ["GOOD", "GOOD", "OK", "OK", "POOR", "NONE"])

# ---------------- the EUI-64 address alfred needs
T = tempfile.mkdtemp(prefix="mmb_meshwatch_")
open(T + "/if6", "w").write("fe80000000000000a1b2c3d4e5f60708 02 40 20 80     eth0\n"
                            "fe800000000000000201c2fffe030405 03 40 20 80     eth1\n"
                            "20010db8000000000201c2fffe030405 03 40 00 80     eth1\n")
mw.IF_INET6 = T + "/if6"
check("stable-privacy link-local is not what alfred answers", mw.eui64_lladdr("eth0") is None)
check("EUI-64 link-local found", mw.eui64_lladdr("eth1") == "fe80::201:c2ff:fe03:405", mw.eui64_lladdr("eth1"))
d, s, why = mw.alfred_query("eth0")
check("no EUI-64 address: no query, says why", not d and "EUI-64" in why, why)

# ---------------- node status, no login (the reply a 1.8.0 node gave)
REPLY = {"deviceInfo": {"hostname": "BCM2711-0a1b", "firmware": "OpenMANET 24.10 1.8.0"},
         "systemResources": {"uptime": "960.580s", "cpuLoadPercent": 1.3801757, "memoryTotalBytes": "1945145344",
                             "memoryUsedBytes": "142389248", "cpuTempCelsius": 45.764}}
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.path != mw.STATUS_PATH:
            self.send_response(404); self.end_headers(); return
        b = json.dumps(REPLY).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
srv = HTTPServer(("127.0.0.1", 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
port = srv.server_address[1]
import urllib.request as _u
_open = _u.urlopen
_u.urlopen = lambda req, timeout=None: _open(req.full_url.replace("10.41.254.10:8087", "127.0.0.1:%d" % port), data=req.data, timeout=timeout) \
    if "10.41.254.10" in req.full_url else _open(req, timeout=timeout)
h = mw.node_status("10.41.254.10")
check("node status: CPU, load, memory %, uptime, firmware", h == {"hostname": "BCM2711-0a1b", "firmware": "OpenMANET 24.10 1.8.0",
      "cpu_c": 45.8, "load_pct": 1.4, "mem_pct": 7.3, "uptime": 960}, h)
check("node status: a node that doesn't answer is None, not an error", mw.node_status("127.0.0.1", timeout=0.5) is None)

# ---------------- the watcher
calls = {"q": 0, "s": []}
answer = {"data": DATA, "servers": {"fe80::1"}}
def fake_query(iface):
    calls["q"] += 1
    return answer["data"], answer["servers"], ""
def fake_status(ip):
    calls["s"].append(ip)
    return {"cpu_c": 40.0 + len(calls["s"]), "load_pct": 1.0, "mem_pct": 5.0, "uptime": 100}
w = mw.MeshWatch("eth0", status_every=60, query=fake_query, status=fake_status)
w.poll(now=1000.0)
sn = w.snapshot()
check("watcher: both nodes, each with health", sorted(n["name"] for n in sn["nodes"]) == ["NODE_1", "NODE_2"] and all(n["health"] for n in sn["nodes"]), sn)
check("watcher: links kept", len(sn["links"]) == 1 and sn["state"]["ok"], sn)
w.poll(now=1030.0)
check("watcher: health asked once a minute, not every poll", len(calls["s"]) == 2, calls)
answer["data"], answer["servers"] = {}, set()
w.poll(now=1100.0)
sn = w.snapshot()
check("watcher: alfred silent once: nodes and last links kept, state says why", len(sn["nodes"]) == 2 and len(sn["links"]) == 1 and not sn["state"]["ok"], sn["state"])
w.poll(now=1000.0 + mw.FORGET_AFTER + 60)
check("watcher: a node gone from alfred for 15 minutes is dropped", w.snapshot()["nodes"] == [], w.snapshot()["nodes"])
w2 = mw.MeshWatch("eth0", query=lambda i: (_ for _ in ()).throw(RuntimeError("boom")), status=fake_status, every=0.05)
w2.start(); time.sleep(0.3)
check("watcher: an error inside never kills the thread, state says so", "boom" in w2.snapshot()["state"]["why"], w2.snapshot()["state"])

# ---------------- the dashboard with the mesh's list
import dashboard_server as ds
UP = {"10.41.0.1": 0.4, "10.41.254.10": 3.2, "10.41.254.20": 9.0}
ds.ping = lambda ip, timeout=1: UP.get(ip)
class Ev:
    def __init__(self): self.items = []
    def add(self, level, text, cat="system"): self.items.append((level, text, cat))
class Stub:
    errors = []
    def can_restart(self): return True
    def evaluate(self, m, now): return []
class FakeWatch:
    def __init__(self): self.snap = None
    def snapshot(self): return json.loads(json.dumps(self.snap))
def snap(nodes, links, ok=True):
    return {"state": {"ok": ok, "why": "", "servers": 2, "t": time.time()}, "links_age": 5.0,
            "nodes": [{"name": n, "ip": ip, "seen": time.time(), "health": {"cpu_c": 45.8, "load_pct": 1.4, "mem_pct": 7.3, "uptime": 960}}
                      for n, ip in nodes],
            "links": [{"a": a, "b": b, "kbps": k} for a, b, k in links]}
cfg = dict(ds.DEFAULTS, GPS="off", SERVER_HOST="127.0.0.1", SERVER_PORT="1", MESH_DISCOVERY="off",
           NODE_IP="10.41.0.1", NODE_NAME="node 1", MESH_STATUS_FILE=T + "/mesh_status.json")
c = ds.Collector(cfg, Ev(), alerts=Stub(), control=Stub())
c.meshwatch = FakeWatch()
c.meshwatch.snap = snap([("NODE_1", "10.41.0.1"), ("BCM2711-0a1b", "10.41.254.10")], [("BCM2711-0a1b", "NODE_1", 7100)])
DECK = {"iface_up": True, "mtu_ok": True}
def mesh(age=1):
    c.slow.clear()
    return c.mesh(DECK, {"running": True, "mesh_last_age": age})
m = mesh()
check("dashboard: a node named in deck.ini keeps its name, the rest come from the mesh",
      [(n["name"], n["ip"], n["source"]) for n in m["nodes"]] == [("node 1", "10.41.0.1", "named"), ("BCM2711-0a1b", "10.41.254.10", "mesh")], m["nodes"])
check("dashboard: health on every node the mesh lists", all(n["cpu_c"] == 45.8 and n["mem_pct"] == 7.3 for n in m["nodes"]), m["nodes"])
check("dashboard: link named the way the list names nodes, graded", m["links"] == [{"a": "BCM2711-0a1b", "b": "node 1", "mbps": 7.1, "grade": "GOOD"}], m["links"])
check("dashboard: tile 2 NODES, slowest 7.1", (m["tile"], m["text"], m["slowest_mbps"], m["nodes_up"], m["nodes_total"]) == ("ok", "2 NODES", 7.1, 2, 2), m)

def collect_events():
    c.events.items.clear()
    c.prev.pop("mesh_status_t", None)
    m = mesh()
    known = c.prev.get("mesh_known")
    # the same calls collect() makes after the mesh pass
    now_known = {n["ip"]: n["name"] for n in m["nodes"] if n.get("listed")}
    if known is not None:
        for ip, name in now_known.items():
            if ip not in known:
                c.events.add("ok", "%s joined the mesh (%s)" % (name, ip), "mesh")
    c.prev["mesh_known"] = now_known
    for n in m["nodes"]:
        c.changed("node:" + n["ip"], n["ok"], "%s answers again" % n["name"], "%s lost (not answering)" % n["name"], level_down="warn", cat="mesh")
    c.link_events(m["links"])
    c.write_mesh_status(m, time.time())
    return m, [e[1] for e in c.events.items]
src = open(PKG + "/dashboard/dashboard_server.py").read()
check("collect() makes the same join / lost / link calls", '"%s joined the mesh (%s)" % (name, ip)' in src and "self.link_events(mesh[\"links\"])" in src
      and "self.write_mesh_status(mesh, now)" in src)
collect_events()
c.meshwatch.snap = snap([("NODE_1", "10.41.0.1"), ("BCM2711-0a1b", "10.41.254.10"), ("NODE_3", "10.41.254.20")],
                        [("BCM2711-0a1b", "NODE_1", 7100), ("BCM2711-0a1b", "NODE_3", 2400)])
m, ev = collect_events()
check("event: a new node joins by itself, nothing typed", "NODE_3 joined the mesh (10.41.254.20)" in ev, ev)
check("event: its radio link comes up", any("Radio link BCM2711-0a1b ↔ NODE_3 up (2.4 Mbit/s OK)" == e for e in ev), ev)
c.meshwatch.snap["links"][1]["kbps"] = 600
m, ev = collect_events()
check("event: a link turning POOR, once", ev == ["Radio link BCM2711-0a1b ↔ NODE_3 POOR (0.6 Mbit/s)"], ev)
m, ev = collect_events()
check("...and not again while it stays POOR", ev == [], ev)
del UP["10.41.254.20"]; c.meshwatch.snap["links"] = c.meshwatch.snap["links"][:1]
m, ev = collect_events()
check("event: node lost and its link gone", "NODE_3 lost (not answering)" in ev and "Radio link BCM2711-0a1b ↔ NODE_3 gone" in ev, ev)
check("tile: 2 OF 3 NODES (amber), panel NODE LOST", (m["tile"], m["text"], m["state"]) == ("warn", "2 OF 3 NODES", "NODE LOST"), m)
st = json.load(open(T + "/mesh_status.json"))
check("summary file for the bridge", (st["nodes_total"], st["nodes_up"], st["links"], st["slowest_mbps"], st["lost"]) == (3, 2, 1, 7.1, ["NODE_3"]), st)
check("the alert rules can use the slowest link", "link_mbps" in ds.METRICS and '"link_mbps": mesh["slowest_mbps"]' in src)

# ---------------- DECK status line on the TAK map
import tak_mesh_bridge as tb
s1 = tb.mesh_summary({"MESH_STATUS_FILE": T + "/mesh_status.json"})
check("DECK remarks: mesh nodes and slowest link", s1 == "mesh 2 of 3 nodes, slowest 7.1 Mbit/s", s1)
json.dump(dict(st, nodes_up=3), open(T + "/ms2.json", "w"))
check("...all answering", tb.mesh_summary({"MESH_STATUS_FILE": T + "/ms2.json"}) == "mesh 3 nodes, slowest 7.1 Mbit/s")
json.dump(dict(st, now=time.time() - 600), open(T + "/ms3.json", "w"))
check("...an old file (Ops stopped) is ignored", tb.mesh_summary({"MESH_STATUS_FILE": T + "/ms3.json"}) == "")
check("...no file, or off: nothing", tb.mesh_summary({"MESH_STATUS_FILE": T + "/none.json"}) == "" and tb.mesh_summary({"MESH_STATUS_FILE": "off"}) == "")
tb.Stats.mesh_ready = True
line = tb.deck_status({"MESH_STATUS_FILE": T + "/mesh_status.json"})
check("the whole line uses it and stays plain ASCII", "mesh 2 of 3 nodes, slowest 7.1 Mbit/s" in line and line.isascii(), line)
check("without Ops the line still says mesh OK", "mesh OK" in tb.deck_status({"MESH_STATUS_FILE": T + "/none.json"}))

# ---------------- meshtoc check
import manet_mesh_bridge as tool
import configparser
cp = configparser.ConfigParser(); cp.read_string("[dashboard]\nMESH_IFACE = eth0\nMESH_STATUS_FILE = %s/mesh_status.json\n" % T)
tool.sh = lambda *a: (0, "Wired connection 1:eth0\nlo:lo") if a[:2] == ("nmcli", "-t") else (1, "")
buf = io.StringIO()
with redirect_stdout(buf):
    tool.check_mesh_list(cp)
out = buf.getvalue()
check("meshtoc check: no EUI-64 address -> a note (not a failure) with the exact fix", "Mesh node list" in out and "info" in out and "WARN" not in out and "FAIL" not in out and
      'sudo nmcli connection modify "Wired connection 1" ipv6.addr-gen-mode eui64' in out, out)
check("meshtoc check: shows the mesh summary", "2 of 3 nodes answer, 1 radio link(s), slowest 7.1 Mbit/s" in out, out)
buf = io.StringIO()
with redirect_stdout(buf):
    tool.check_mesh_list(configparser.ConfigParser())   # no [dashboard] section at all
check("meshtoc check: works without a [dashboard] section", "Mesh node list" in buf.getvalue(), buf.getvalue())
open(T + "/if6b", "w").write("fe800000000000000201c2fffe030405 03 40 20 80     eth0\n")
mw.IF_INET6 = T + "/if6b"
buf = io.StringIO()
with redirect_stdout(buf):
    tool.check_mesh_list(cp)
check("meshtoc check: with the address, OK", "can read the mesh's own node list" in buf.getvalue(), buf.getvalue())
install = open(PKG + "/install.sh").read()
check("install.sh switches the mesh port's connection to eui64 (only when IPv6 is on)",
      "ipv6.addr-gen-mode eui64" in install and "disabled|ignore" in install)

# ---------------- a real alfred server (root + alfred binary)
ALF = os.environ.get("ALFRED_BIN") or shutil.which("alfred")
if os.geteuid() != 0 or not ALF:
    print("(real alfred part skipped: needs root and an alfred binary, ALFRED_BIN=...)")
else:
    NS = "mmbalf"
    def ip(*a): return subprocess.run(["ip"] + list(a), capture_output=True)
    ip("netns", "del", NS); ip("link", "del", "mmbalf0")
    ip("netns", "add", NS); ip("link", "add", "mmbalf0", "type", "veth", "peer", "name", "mmbalf1")
    ip("link", "set", "mmbalf1", "netns", NS)
    ip("addr", "add", "10.99.7.5/24", "dev", "mmbalf0"); ip("link", "set", "mmbalf0", "up")
    nsx = ["ip", "netns", "exec", NS]
    subprocess.run(nsx + ["ip", "addr", "add", "10.99.7.1/24", "dev", "mmbalf1"]); subprocess.run(nsx + ["ip", "link", "set", "mmbalf1", "up"])
    subprocess.run(nsx + ["ip", "link", "set", "lo", "up"]); subprocess.run(nsx + ["ip", "route", "add", "224.0.0.0/4", "dev", "mmbalf1"])
    sock = T + "/alf.sock"
    a = subprocess.Popen(nsx + [ALF, "-i", "mmbalf1", "-m", "-b", "none", "-4", "239.0.0.66", "-u", sock], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(1.5)
        for t in (102, 103):                        # this server holds NODE_2's own records
            subprocess.run(nsx + [ALF, "-u", sock, "-s", str(t)], input=DATA[(t, P2)])
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.bind(("10.99.7.5", mw.ALFRED_PORT))
        data, servers = mw.collect(s, lambda p: s.sendto(p, ("10.99.7.1", mw.ALFRED_PORT)), seconds=2)
        s.close()
        nodes, links = mw.parse_records(data)
        check("real alfred: answers our request", servers == {"10.99.7.1"}, servers)
        check("real alfred: the node record it holds comes back", [(n["name"], n["ip"]) for n in nodes] == [("NODE_2", "10.41.254.10")], nodes)
        check("real alfred: the neighbor record too", sorted(t for t, _ in data) == [102, 103], list(data))
    finally:
        a.terminate(); a.wait(5); ip("netns", "del", NS); ip("link", "del", "mmbalf0")

srv.shutdown()
print("%d/%d passed" % (sum(res), len(res)))
sys.exit(0 if all(res) else 1)
