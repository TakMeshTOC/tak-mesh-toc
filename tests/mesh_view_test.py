"""mesh_view_test.py - what Ops gives another device on the mesh: everything except where the TAK server and the
deck's Tailscale are. Runs the real dashboard_server.py with demo data.   python3 mesh_view_test.py"""
import json, os, socket, subprocess, sys, time, urllib.request
PKG = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PKG + "/dashboard"); sys.path.insert(0, PKG + "/common")
import dashboard_server as D
res = []
def check(n, c, x=""):
    res.append(bool(c)); print(("PASS  " if c else "FAIL  ") + n + ("" if c else "  " + str(x)[:300]))

# the function itself: the addresses go wherever they appear, nothing else changes
st = {"server": {"host": "tak.example.net", "port": 8089, "tailscale_ip": "100.64.0.10", "tailscale_up": True, "reachable": True},
      "setup": [{"title": "Server TLS link", "detail": "tak.example.net:8089 established"}],
      "video": {"relay": {"server": "rtsp://tak.example.net:8554"}}, "mesh": {"nodes": [{"ip": "10.41.0.60"}]}, "host": "deck"}
out = D.status_for_mesh(st)
flat = json.dumps(out)
check("mesh view: the server's host and port and the Tailscale address are blank", out["server"]["host"] == "" and out["server"]["port"] is None and out["server"]["tailscale_ip"] == "")
check("mesh view: the addresses are gone from every text too (checks, video relay)", "tak.example.net" not in flat and "100.64.0.10" not in flat, flat)
check("mesh view: everything else is unchanged (link state, mesh nodes)", out["server"]["tailscale_up"] is True and out["server"]["reachable"] is True and out["mesh"] == st["mesh"] and out["host"] == "deck")
check("mesh view: the deck's own copy is not altered", st["server"]["host"] == "tak.example.net" and "tak.example.net" in st["setup"][0]["detail"])
check("mesh view: a status with no server part is passed through", D.status_for_mesh({"a": 1}) == {"a": 1})

# over HTTP, the real server with demo data: the deck's own screen sees them, a mesh device does not
s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
pr = subprocess.Popen([sys.executable, PKG + "/dashboard/dashboard_server.py", "--mock", "auto", "--port", str(port), "--listen", "127.0.0.1"],
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    for _ in range(40):
        try: urllib.request.urlopen("http://127.0.0.1:%d/healthz" % port, timeout=1); break
        except Exception: time.sleep(0.25)
    def status(host):
        rq = urllib.request.Request("http://127.0.0.1:%d/api/status" % port, headers={"Host": host})
        return urllib.request.urlopen(rq, timeout=5).read().decode()
    own = status("127.0.0.1:%d" % port); mesh = status("10.41.0.50:%d" % port)
    check("deck's own screen: server host and Tailscale address shown", json.loads(own)["local"] is True and "tak.example.net" in own and "100.64.0.10" in own)
    m = json.loads(mesh)
    check("another device: neither appears anywhere in the status", m["local"] is False and "tak.example.net" not in mesh and "100.64.0.10" not in mesh, mesh[:300])
    check("another device: the link state is still there", m["server"]["tile"] in ("ok", "warn", "bad") and m["server"]["tailscale_up"] is True and "tiles" in m)
finally:
    pr.terminate()
print("%d/%d passed" % (sum(res), len(res)))
sys.exit(0 if all(res) else 1)
