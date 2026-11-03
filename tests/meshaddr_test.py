import os as _o, sys as _s  # paths relative to this package
PKG = _o.path.dirname(_o.path.dirname(_o.path.abspath(__file__)))
_s.path[:0] = [PKG + "/vendor", PKG + "/common", PKG + "/bridge", PKG + "/chat", PKG + "/dashboard", PKG + "/voice"]
"""MESH_ADDR = auto: the deck uses whatever address its mesh port has and follows it when the node hands out a new
one (an OpenWrt node forgets its leases when it reboots). Unit checks always; with root, the real bridge runs on a
dummy interface whose address is taken away, given back and changed under it.
Usage: python3 meshaddr_test.py"""
import configparser, os, socket, subprocess, sys, threading, time
import deckcommon as dc

res = []
def check(n, c, x=""):
    res.append(bool(c)); print(("PASS  " if c else "FAIL  ") + n + ("" if c else "  " + str(x)[:500]), flush=True)

# ---------------- unit
cp = configparser.ConfigParser(interpolation=None, strict=False)
cp.read_string("[deck]\nMESH_ADDR = auto\n[bridge]\nPYTAK_MULTICAST_LOCAL_ADDR = 10.41.0.60\n[chat]\nMESH_IFACE = eth9\n")
dc.apply_deck(cp, "bridge", "bridge"); dc.apply_deck(cp, "voice", "voice")
check("[deck] MESH_ADDR = auto reaches the bridge", cp.get("bridge", "PYTAK_MULTICAST_LOCAL_ADDR") == "auto")
check("...and Voice", cp.get("voice", "MESH_ADDR") == "auto")
cp2 = configparser.ConfigParser(interpolation=None, strict=False)
cp2.read_string("[deck]\nMESH_ADDR =\n[bridge]\nPYTAK_MULTICAST_LOCAL_ADDR = 10.41.0.60\n")
dc.apply_deck(cp2, "bridge", "bridge")
check("a blank [deck] MESH_ADDR keeps the bridge's own address", cp2.get("bridge", "PYTAK_MULTICAST_LOCAL_ADDR") == "10.41.0.60")
check("mesh port taken from [chat] when the program has none", dc.mesh_iface(cp, "bridge") == "eth9")
cp3 = configparser.ConfigParser(); cp3.read_string("[voice]\nMESH_IFACE = eth3\n[chat]\nMESH_IFACE = eth9\n")
check("a program's own MESH_IFACE wins", dc.mesh_iface(cp3, "voice") == "eth3")
check("no MESH_IFACE anywhere = eth0", dc.mesh_iface(configparser.ConfigParser(), "bridge") == "eth0")
check("iface_ipv4: lo is 127.0.0.1, a missing port is None", dc.iface_ipv4("lo") == "127.0.0.1" and dc.iface_ipv4("nosuch0") is None)
check("is_auto ignores case and spaces", dc.is_auto(" Auto ") and not dc.is_auto("10.41.0.5") and not dc.is_auto(""))
seen = []
dc.watch_mesh_addr("lo", "127.0.0.1", lambda a, b: seen.append((a, b)), every=0.2); time.sleep(0.8)
check("watcher stays quiet while the address is unchanged", not seen, seen)
ev = threading.Event()
dc.watch_mesh_addr("lo", "10.9.9.9", lambda a, b: (seen.append((a, b)), ev.set()), every=0.2)
check("watcher reports a different address once", ev.wait(3) and seen == [("10.9.9.9", "127.0.0.1")], seen)
seen2 = []
dc.watch_mesh_addr("nosuch0", "10.1.1.1", lambda a, b: seen2.append(b), every=0.2); time.sleep(0.8)
check("an address that goes away is not a change (node unplugged)", not seen2, seen2)

import manet_voice as mv
vc = {"MESH_ADDR": "auto", "_MESH_IFACE": "lo"}
vt = threading.Thread(target=mv.resolve_mesh_addr, args=(vc,), daemon=True); vt.start(); vt.join(5)
check("Voice: auto resolves to the mesh port's address", vc["MESH_ADDR"] == "127.0.0.1", vc)
vc2 = {"MESH_ADDR": "10.41.0.7", "_MESH_IFACE": "lo"}; mv.resolve_mesh_addr(vc2)
check("Voice: a fixed address is used as it is", vc2["MESH_ADDR"] == "10.41.0.7")
cp4 = configparser.ConfigParser(interpolation=None, strict=False)
cp4.read_string(open(PKG + "/config/deck.ini.example").read())
check("deck.ini.example: MESH_ADDR = auto", cp4.get("deck", "MESH_ADDR").strip() == "auto")

# ---------------- the real bridge on a dummy port (root: needs to add an interface)
if os.geteuid() != 0 or subprocess.run(["ip", "link", "add", "mtdummy0", "type", "veth", "peer", "name", "mtdummy1"],
                                      capture_output=True).returncode:
    print("(real-bridge part skipped: needs root to add a test interface)")
else:
    W = "/tmp/mmb_meshaddr"; os.makedirs(W, exist_ok=True)
    def ip(*a): subprocess.run(["ip"] + list(a), capture_output=True)
    ip("link", "set", "mtdummy1", "up"); ip("link", "set", "mtdummy0", "up"); ip("link", "set", "mtdummy0", "multicast", "on")
    srv = socket.socket(); srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); srv.bind(("127.0.0.1", 0)); srv.listen(5)
    port = srv.getsockname()[1]
    def serve():
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            threading.Thread(target=lambda c=c: [None for _ in iter(lambda: c.recv(65536), b"")], daemon=True).start()
    threading.Thread(target=serve, daemon=True).start()
    open(W + "/d.ini", "w").write("[deck]\nCALLSIGN = DECK\nUID = DECK-ma\nMESH_ADDR = auto\nGPS = off\n[chat]\nMESH_IFACE = mtdummy0\n"
                                  "[bridge]\nCOT_URL = tcp://127.0.0.1:%d\nSTATUS_FILE = %s/st.json\nOUTBOX_FILE = off\nDM_RELAY = 1\n"
                                  "LOCAL_CHAT_COPY = off\nPING_INTERVAL = 0\n" % (port, W))
    def start():
        log = open(W + "/b.log", "w")
        return subprocess.Popen([sys.executable, "-S", PKG + "/bridge/tak_mesh_bridge.py", W + "/d.ini"], stdout=log,
                                stderr=subprocess.STDOUT, env=dict(os.environ, PYTHONWARNINGS="ignore"))
    def logtxt(): return open(W + "/b.log").read()
    def wait(cond, t=15):
        end = time.time() + t
        while time.time() < end:
            if cond(): return True
            time.sleep(0.3)
        return cond()
    try:
        # 1. no address yet: server-only, then it appears -> restart
        p = start()
        check("no address yet: the bridge says so and keeps the server side up",
              wait(lambda: "has no address yet" in logtxt() and "Waiting for the mesh" in logtxt()), logtxt()[-800:])
        check("...and doesn't listen on the mesh in the meantime", "Listening on udp" not in logtxt(), logtxt()[-600:])
        ip("addr", "add", "10.99.0.5/24", "dev", "mtdummy0")
        check("address appears: the bridge restarts to use it (exit 3)", wait(lambda: p.poll() is not None) and p.returncode == 3,
              (p.poll(), logtxt()[-600:]))
        check("...and says why", "is now 10.99.0.5 (was none)" in logtxt(), logtxt()[-600:])
        # 2. start with the address: uses it everywhere
        p = start()
        check("auto: the bridge uses the port's address", wait(lambda: "Mesh address auto: 10.99.0.5 on mtdummy0" in logtxt()), logtxt()[-800:])
        check("...listens on the mesh via it", wait(lambda: "Listening on udp+ro://239.2.3.1:6969 via 10.99.0.5" in logtxt()), logtxt()[-800:])
        check("...and the DM relay listens on it", wait(lambda: "DM relay listening on 10.99.0.5:4243" in logtxt()), logtxt()[-800:])
        # 3. the address goes away (node unplugged): no restart
        ip("addr", "del", "10.99.0.5/24", "dev", "mtdummy0"); time.sleep(8)
        check("address gone (node unplugged): no restart", p.poll() is None, logtxt()[-600:])
        # 4. it comes back the same: still no restart
        ip("addr", "add", "10.99.0.5/24", "dev", "mtdummy0"); time.sleep(8)
        check("same address back: no restart", p.poll() is None, logtxt()[-600:])
        # 5. the node hands out a new one: restart onto it
        ip("addr", "flush", "dev", "mtdummy0"); ip("addr", "add", "10.99.0.9/24", "dev", "mtdummy0")
        check("new address: the bridge restarts to follow it (exit 3)", wait(lambda: p.poll() is not None) and p.returncode == 3,
              (p.poll(), logtxt()[-600:]))
        check("...naming old and new", "is now 10.99.0.9 (was 10.99.0.5)" in logtxt(), logtxt()[-600:])
        p = start()
        check("after the restart it runs on the new address", wait(lambda: "Listening on udp+ro://239.2.3.1:6969 via 10.99.0.9" in logtxt()),
              logtxt()[-800:])
        check("no tracebacks", "Traceback" not in logtxt(), logtxt()[-800:])
        p.terminate(); p.wait(5)
        # 6. a fixed address is never followed
        open(W + "/f.ini", "w").write(open(W + "/d.ini").read().replace("MESH_ADDR = auto", "MESH_ADDR = 10.99.0.9"))
        log = open(W + "/b.log", "w")
        p = subprocess.Popen([sys.executable, "-S", PKG + "/bridge/tak_mesh_bridge.py", W + "/f.ini"], stdout=log, stderr=subprocess.STDOUT)
        wait(lambda: "Listening on udp" in logtxt())
        ip("addr", "flush", "dev", "mtdummy0"); ip("addr", "add", "10.99.0.20/24", "dev", "mtdummy0"); time.sleep(8)
        check("fixed MESH_ADDR: no auto restart", p.poll() is None and "restarting the bridge" not in logtxt(), logtxt()[-600:])
        p.terminate(); p.wait(5)
    finally:
        ip("link", "del", "mtdummy0"); srv.close()
print("%d/%d passed" % (sum(res), len(res)))
sys.exit(0 if all(res) else 1)
