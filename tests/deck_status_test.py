import os as _o, sys as _s  # paths relative to this package
PKG = _o.path.dirname(_o.path.dirname(_o.path.abspath(__file__)))
_s.path[:0] = [PKG + "/vendor", PKG + "/common", PKG + "/bridge", PKG + "/chat", PKG + "/dashboard"]
"""DECK_STATUS: the deck's health (CPU temperature, internet link, mesh, uptime, version) in the remarks of the
position report the bridge sends the server. Fake /proc and /sys files; checks the report stays valid CoT.
Usage: python3 -S deck_status_test.py"""
import os, sys, tempfile, time, re
import warnings; warnings.filterwarnings("ignore")
import tak_mesh_bridge as t

res = []
def check(n, c, x=""):
    res.append(bool(c)); print(("PASS  " if c else "FAIL  ") + n + ("" if c else "  " + str(x)[:400]), flush=True)

D = tempfile.mkdtemp(prefix="mmb_status_")
def put(name, text):
    p = os.path.join(D, name); open(p, "w").write(text); return p
t.THERMAL = put("temp", "52345\n")
t.PROC_UPTIME = put("uptime", "22320.55 80000.00\n")
ROUTE_HDR = "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
t.PROC_ROUTE = put("route", ROUTE_HDR + "eth0\t0000290A\t00000000\t0001\t0\t0\t100\t0000FFFF\t0\t0\t0\n"
                   "wlan0\t00000000\t01486E64\t0003\t0\t0\t600\t00000000\t0\t0\t0\n")
WIRELESS_HDR = "Inter-| sta-|   Quality        |   Discarded packets\n face | tus | link level noise |  nwid  crypt\n"
t.PROC_WIRELESS = put("wireless", WIRELESS_HDR + " wlan0: 0000   49.  -61.  -256        0      0\n")
t.MeshAddr.iface = "eth0"
t.Stats.mesh_ready = True

st = t.deck_status({})
check("status line has CPU temperature", "CPU 52C" in st, st)
check("...the internet link and its Wi-Fi signal", "WAN wlan0 -61 dBm" in st, st)
check("...mesh state", "mesh OK" in st, st)
check("...uptime", "up 6h12m" in st, st)
check("...and the version", "TAK MESH TOC " + t.suite_version() in st, st)
check("plain ASCII (every TAK client shows it)", st.isascii(), st)

t.Stats.mesh_ready = False
check("node unplugged: says server-only", "mesh not joined (server-only)" in t.deck_status({}))
t.PROC_ROUTE = put("route2", ROUTE_HDR + "eth0\t00000000\t01002A0A\t0003\t0\t0\t100\t00000000\t0\t0\t0\n"
                   "eth1\t00000000\t0101A8C0\t0003\t0\t0\t200\t00000000\t0\t0\t0\n")
check("the mesh port is never taken for the internet link; a wired link has no signal",
      "WAN eth1" in t.deck_status({}) and "dBm" not in t.deck_status({}) and "eth0" not in t.deck_status({}), t.deck_status({}))
t.PROC_ROUTE = put("route3", ROUTE_HDR)
check("no internet route: no WAN part", "WAN" not in t.deck_status({}), t.deck_status({}))
t.THERMAL = os.path.join(D, "missing"); t.PROC_UPTIME = os.path.join(D, "missing")
st2 = t.deck_status({})
check("unreadable sensors are skipped, never an error", "CPU" not in st2 and "up " not in st2 and "TAK MESH TOC" in st2, st2)
check("DECK_STATUS = off: nothing", t.deck_status({"DECK_STATUS": "off"}) == "")

cfg = {"BRIDGE_UID": "DECK-st", "BRIDGE_CALLSIGN": "DECK"}
x = t.bridge_identity(cfg, pos={"lat": 1.0, "lon": 2.0, "alt": 3.0, "eph": 4.0}, live=True, status="CPU 52C | mesh OK & <fine>")
check("position report carries the status in <remarks>", b"<remarks>CPU 52C | mesh OK &amp; &lt;fine&gt;</remarks>" in x, x[-200:])
check("...and is still one valid CoT event", t.bad_event(x) is None, t.bad_event(x))
check("...same identity as before (uid, callsign, endpoint)", b'uid="DECK-st"' in x and b'callsign="DECK" endpoint="*:-1:stcp"' in x)
check("no status: no remarks element (unchanged report)", b"<remarks" not in t.bridge_identity(cfg))
print("%d/%d passed" % (sum(res), len(res)))
sys.exit(0 if all(res) else 1)
