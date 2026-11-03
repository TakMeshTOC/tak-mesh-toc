import os as _o, sys as _s  # paths relative to this package
PKG = _o.path.dirname(_o.path.dirname(_o.path.abspath(__file__)))
_s.path[:0] = [PKG + "/vendor", PKG + "/common", PKG + "/dashboard"]
"""mesh_nodes_test.py: the Mesh link with several nodes, nodes found on the port, and mesh traffic as proof the mesh is up.
Pings, the neighbour table and the web-page check are faked, so this runs anywhere."""
import json, socket, subprocess, sys, tempfile, time, urllib.request
import dashboard_server as ds

res = []
def check(n, c, x=""):
    res.append(bool(c)); print(("PASS  " if c else "FAIL  ") + n + ("" if c else "  " + str(x)[:300]))

# ---- fakes: which addresses answer a ping, which serve a web page, what the kernel's tables say
UP, WEB = {}, set()
NEIGH = ""
ROUTE = ""
PINGS = []
def fake_ping(ip, timeout=1):
    PINGS.append(ip)
    if ip == "slow":
        time.sleep(1.0)
    return UP.get(ip)
def fake_run(cmd, timeout=2.5):
    if cmd[:3] == ["ip", "-4", "neigh"]: return 0, NEIGH
    if cmd[:3] == ["ip", "-4", "route"]: return 0, ROUTE
    return 1, ""
ds.ping = fake_ping
ds.run = fake_run
ds.tcp_check = lambda host, port, timeout=2.0: 1.0 if host in WEB else None
_which = ds.shutil.which
ds.shutil.which = lambda t: None if t == "nmcli" else _which(t)

class Ev:
    def __init__(self): self.items = []
    def add(self, level, text, cat="system"): self.items.append((level, text, cat))
class Stub:
    errors = []
    def can_restart(self): return True

def collector(**kw):
    kw.setdefault("MESH_DISCOVERY", "off")           # the mesh's own list (alfred) is tested in meshwatch_test.py
    cfg = dict(ds.DEFAULTS, GPS="off", SERVER_HOST="127.0.0.1", SERVER_PORT="1", **kw)
    return ds.Collector(cfg, Ev(), alerts=Stub(), control=Stub())
DECK = {"iface_up": True, "mtu_ok": True}
def mesh(c, deck=DECK, age=None):
    c.slow.clear()                                   # skip the 3 s ping cache
    return c.mesh(deck, {"running": True, "mesh_last_age": age})
SRV = {"established": True}
def act(c, m, rate=12):
    return c.activity({"running": True, "service": "active", "msgs_per_min": rate}, SRV, m)["state"]

# 1. two named nodes: node 1 answers, node 2 is switched off (tonight's case)
c = collector(NODE_IP="10.41.254.10, 10.41.254.20", NODE_NAME="node 1, node 2")
UP.clear(); UP["10.41.254.10"] = 0.3
m = mesh(c, age=2)
check("two nodes, one off: tile 1 OF 2 NODES (amber), panel NODE LOST", (m["tile"], m["text"], m["state"]) == ("warn", "1 OF 2 NODES", "NODE LOST"), m)
check("two nodes, one off: names kept in order, node 2 'not answering'",
      [(n["name"], n["ok"]) for n in m["nodes"]] == [("node 1", True), ("node 2", False)], m["nodes"])
check("two nodes, one off: node 1 is the nearest", [n["name"] for n in m["nodes"] if n["nearest"]] == ["node 1"])
check("two nodes, one off: TAK BRIDGE keeps RELAYING", act(c, m) == "hop")

UP["10.41.254.20"] = 0.6
m = mesh(c, age=2)
check("two nodes, both answer: tile 2 NODES (green)", (m["tile"], m["text"], m["state"]) == ("ok", "2 NODES", "LINK OK"), m)
UP.clear(); UP["10.41.254.10"] = 0.3

# 2. the cable moves to node 2 (node 1 still on, reached through the mesh: slower)
UP.clear(); UP["10.41.254.10"] = 3.1; UP["10.41.254.20"] = 0.4
m = mesh(c, age=1)
check("cable on node 2: nearest follows the fastest node", [n["name"] for n in m["nodes"] if n["nearest"]] == ["node 2"], m["nodes"])
check("cable on node 2: old fields point at the nearest", (m["node_name"], m["node_ip"], m["node_rtt_ms"]) == ("node 2", "10.41.254.20", 0.4), m)

# 3. no node answers a ping, but mesh traffic arrives
UP.clear()
m = mesh(c, age=4)
check("no ping but traffic: tile LINK OK in amber, panel TRAFFIC ONLY", (m["tile"], m["text"], m["state"]) == ("warn", "LINK OK", "TRAFFIC ONLY"), m)
check("no ping but traffic: TAK BRIDGE keeps RELAYING (no false DEGRADED)", act(c, m) == "hop")

# 4. no ping and no traffic (traffic 2 minutes old) -> NODE LOST, bridge DEGRADED
m = mesh(c, age=120)
check("no ping, no traffic: NODE LOST", (m["tile"], m["text"]) == ("bad", "NODE LOST"), m)
check("no ping, no traffic: TAK BRIDGE DEGRADED", act(c, m) == "alert")
m = mesh(c, age=None)
check("no ping, never any traffic: NODE LOST", m["tile"] == "bad")
c2 = collector(NODE_IP="10.41.254.10")
UP["10.41.254.10"] = 0.3
mm = c2.mesh(DECK, {"running": False, "mesh_last_age": 3}); c2.slow.clear(); UP.clear()
mm = c2.mesh(DECK, {"running": False, "mesh_last_age": 3})
check("a stopped bridge's old traffic time does not count", mm["tile"] == "bad", mm)

# 5. port down, MTU wrong
UP["10.41.254.10"] = 0.3
check("eth0 down: bad, PORT DOWN", mesh(c, {"iface_up": False, "mtu_ok": True}, 2)["state"] == "PORT DOWN")
check("node answers but MTU wrong: CHECK MTU", mesh(c, {"iface_up": True, "mtu_ok": False}, 2)["text"] == "CHECK MTU")

# 6. the old single-node setting still works, with its name
c = collector(NODE_IP="10.41.254.30", NODE_NAME="node 2")
UP.clear(); UP["10.41.254.30"] = 0.5
m = mesh(c, age=1)
check("old single NODE_IP + NODE_NAME still works", [(n["name"], n["ip"], n["ok"]) for n in m["nodes"]] == [("node 2", "10.41.254.30", True)], m["nodes"])
c = collector(NODE_IP="10.41.254.1, 10.41.254.2, 10.41.254.3", NODE_NAME="gate")
check("fewer names than nodes: the rest are numbered", [n for n, _ in c.nodes_cfg()] == ["gate", "node 2", "node 3"], c.nodes_cfg())

# 7. NODE_IP blank: nodes found on the port (only the ones with a settings page; phones are skipped; FAILED skipped)
c = collector(NODE_IP="")
NEIGH = ("10.41.254.20 FAILED\n10.41.0.20 lladdr 02:00:00:00:00:14 STALE\n"
         "10.41.254.10 lladdr 02:00:00:00:00:10 REACHABLE\n")
WEB.clear(); WEB.update({"10.41.254.10", "10.41.254.20"})
UP.clear(); UP["10.41.254.10"] = 0.3; UP["10.41.0.20"] = 5.0
m = mesh(c, age=1)
check("found: the node with a settings page, not the phone, not the FAILED entry",
      [(n["ip"], n["found"]) for n in m["nodes"]] == [("10.41.254.10", True)], m["nodes"])
check("found: LINK OK", m["tile"] == "ok")
NEIGH += "10.41.254.20 lladdr 11:22:33:44:55:66 REACHABLE\n"
UP["10.41.254.20"] = 2.0
mesh(c, age=1)
check("a new node is not looked for again until a minute has passed", len(m["nodes"]) == 1 and len(mesh(c, age=1)["nodes"]) == 1)
c._found_t -= 61
m = mesh(c, age=1)
check("after a minute the second node is found too", [n["ip"] for n in m["nodes"]] == ["10.41.254.10", "10.41.254.20"], m["nodes"])
ROUTE = "default via 10.41.0.1 dev eth0 proto dhcp\n"; WEB.add("10.41.0.1"); UP["10.41.0.1"] = 1.0
c._found_t -= 61
check("the gateway on the port counts too", "10.41.0.1" in [n["ip"] for n in mesh(c, age=1)["nodes"]])
ROUTE = ""

# 8. a found node that goes quiet: kept (not answering) for 10 minutes, then dropped
NEIGH = "10.41.254.10 lladdr 02:00:00:00:00:10 REACHABLE\n"
del UP["10.41.254.20"]
c._found_t -= 61
m = mesh(c, age=1)
check("a found node that goes quiet shows as not answering",
      ("10.41.254.20", False) in [(n["ip"], n["ok"]) for n in m["nodes"]] and m["tile"] == "warn", m["nodes"])
c._found["10.41.254.20"] -= 601; c._found["10.41.0.1"] -= 601; c._found_t -= 61
m = mesh(c, age=1)
check("...and is dropped after 10 minutes", [n["ip"] for n in m["nodes"]] == ["10.41.254.10"], m["nodes"])

# 9. nothing set, nothing found
c = collector(NODE_IP=""); NEIGH = ""
m = mesh(c, age=None)
check("nothing set or found, no traffic: NO NODE (amber), not red", (m["tile"], m["state"]) == ("warn", "NO NODE FOUND"), m)
check("nothing set or found but traffic: LINK OK", mesh(c, age=3)["text"] == "LINK OK")

# 10. pings run side by side: three slow nodes take about 1 s, not 3
c = collector(NODE_IP="slow, slow, slow")
t0 = time.time(); mesh(c, age=1); took = time.time() - t0
check("three nodes are pinged at the same time (%.1f s)" % took, took < 2.0, took)

# 11. events: one node going quiet is a warning, the whole link lost is red
c = collector(NODE_IP="10.41.254.10, 10.41.254.20", NODE_NAME="node 1, node 2")
UP.clear(); UP["10.41.254.10"] = 0.3; UP["10.41.254.20"] = 0.5
def tick(age):
    m = mesh(c, age=age)
    c.changed("mesh_up", m["tile"] != "bad", "Mesh link back", "Mesh link lost (no node answers, no mesh traffic)", cat="mesh")
    for n in m["nodes"]:
        c.changed("node:" + n["ip"], n["ok"], "%s answers again" % n["name"], "%s not answering" % n["name"], level_down="warn", cat="mesh")
tick(1); del UP["10.41.254.20"]; tick(1)
check("event: node 2 not answering (warning)", ("warn", "node 2 not answering", "mesh") in c.events.items, c.events.items)
check("event: no 'mesh link lost' while node 1 answers", not any("Mesh link lost" in e[1] for e in c.events.items))
UP.clear(); tick(300)
check("event: mesh link lost (red) when nothing answers and no traffic", ("bad", "Mesh link lost (no node answers, no mesh traffic)", "mesh") in c.events.items)
src = open(PKG + "/dashboard/dashboard_server.py").read()
check("the dashboard logs the same events", 'self.changed("mesh_up", mesh["tile"] != "bad"' in src and 'self.changed("node:" + n["ip"]' in src)

# 12. Setup checks list every node
snap = ds.mock_status("hop", Ev())
items = ds.setup_checks(dict(ds.DEFAULTS), snap, Stub(), Stub())
it = [i for i in items if i["title"] == "Mesh node answers"]
check("Setup: one line naming each node and whether it answers", it and "NODE_1 at 10.41.0.1 answers" in it[0]["detail"] and "NODE_2" in it[0]["detail"], it)
snap["mesh"]["nodes"] = []
check("Setup: nothing set or found says how to set NODE_IP",
      any(i["title"] == "Mesh nodes" and "NODE_IP" in i["fix"] for i in ds.setup_checks(dict(ds.DEFAULTS), snap, Stub(), Stub())))

# 13. the page: MESH + DECK lists the nodes (demo data, real browser)
try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None
if sync_playwright:
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    W = tempfile.mkdtemp(prefix="mmb_mesh_nodes_")
    open(W + "/d.ini", "w").write("[dashboard]\nPORT = %d\nLISTEN = 127.0.0.1\nEVENT_FILE = %s/ev.jsonl\nHISTORY_FILE = %s/h.jsonl\nGPS = off\n" % (port, W, W))
    p = subprocess.Popen([sys.executable, PKG + "/dashboard/dashboard_server.py", "--config", W + "/d.ini", "--mock", "hop",
                          "--port", str(port), "--listen", "127.0.0.1"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(60):
            try: urllib.request.urlopen("http://127.0.0.1:%d/healthz" % port, timeout=1); break
            except Exception: time.sleep(0.2)
        with sync_playwright() as pw:
            b = pw.chromium.launch(); pg = b.new_page(viewport={"width": 1280, "height": 720})
            pg.goto("http://127.0.0.1:%d/" % port); pg.wait_for_timeout(2500)
            rows, top = pg.inner_text("#d-rows"), pg.inner_text("#d-bluf")
            check("page: one-glance summary on top of MESH + DECK", "3 NODES" in top and "ALL OK" in top and "slowest link" in top, top)
            check("page: mesh traffic row", "mesh traffic" in rows, rows[:200])
            check("page: panel state from the server", pg.inner_text("#d-state") == "LINK OK")
            pg.route("**/api/status*", lambda r: r.fulfill(content_type="application/json", body=json.dumps(dict(
                json.loads(urllib.request.urlopen("http://127.0.0.1:%d/api/status" % port).read()),
                mesh=dict(json.loads(urllib.request.urlopen("http://127.0.0.1:%d/api/status" % port).read())["mesh"],
                          node_ok=False, tile="warn", text="LINK OK", state="TRAFFIC ONLY", traffic=True, traffic_age=4,
                          nodes=[{"name": "10.41.254.20", "ip": "10.41.254.20", "ok": False, "rtt_ms": None, "found": True, "nearest": False}])))))
            pg.reload(); pg.wait_for_timeout(2500)
            rows, top = pg.inner_text("#d-rows"), pg.inner_text("#d-bluf")
            check("page: found node shown by the end of its address", ".254.20" in top and "10.41.254.20" not in top, top)
            check("page: no ping but traffic says the mesh is up", "NO NODE ANSWERS" in top and "mesh is up" in rows and pg.inner_text("#d-state") == "TRAFFIC ONLY", (top, rows[:200]))
            b.close()
    finally:
        p.terminate()
else:
    print("SKIP  page checks (no playwright)")

print("%d/%d passed" % (sum(res), len(res)))
sys.exit(0 if all(res) else 1)
