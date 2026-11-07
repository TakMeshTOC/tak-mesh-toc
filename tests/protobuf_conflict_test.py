import os as _o, sys as _s  # paths relative to this package
PKG = _o.path.dirname(_o.path.dirname(_o.path.abspath(__file__)))
"""protobuf_conflict_test.py - another protobuf on this computer must not stop Deck Chat.

  * a broken google._upb (as left by a newer protobuf) on the path: Deck Chat still starts and serves its page,
    logs that protobuf decoding is off, and plain XML chat still works
  * with python3 -S (how the service runs it) the stray copy is never seen: protobuf decoding stays on
  * install.sh starts Chat with python3 -S, like the bridge
"""
import json, os, socket, subprocess, sys, tempfile, time, urllib.request
res = []
def check(n, c, x=""):
    res.append(bool(c)); print(("PASS  " if c else "FAIL  ") + n + ("" if c else "  " + str(x)[:300]))
def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p

W = tempfile.mkdtemp(prefix="mmb_pbconflict_")
# the stray copy goes in a user site-packages folder: seen by plain python3, skipped by python3 -S (like /usr/lib/...)
UB = os.path.join(W, "userbase")
USITE = subprocess.run([sys.executable, "-c", "import site; print(site.getusersitepackages())"],
                       env=dict(os.environ, PYTHONUSERBASE=UB), capture_output=True, text=True).stdout.strip()
bad = os.path.join(USITE, "google", "_upb"); os.makedirs(bad)
open(os.path.join(bad, "__init__.py"), "w").write("")
open(os.path.join(bad, "_message.py"), "w").write("from google.protobuf import message\nmessage.FrozenInstanceError\n")

def start(flag_s, port, tag):
    ini = os.path.join(W, tag + ".ini")
    open(ini, "w").write("[chat]\nUID = DECK-pb\nMESH_IFACE = nosuch0\nLISTEN = 127.0.0.1:%d\nHISTORY_FILE = off\n"
                         "BRIDGE_STATUS_FILE = %s/none.json\nSA_SEND = 0\nSA_GROUP = off\nGPS = off\n"
                         "BRIDGE_COPY_LISTEN = 127.0.0.1:%d\nDM_PORT = %d\n" % (port, W, free_port(), free_port()))
    env = dict(os.environ, PYTHONUSERBASE=UB, PYTHONWARNINGS="ignore"); env.pop("PYTHONPATH", None); env.pop("PYTHONNOUSERSITE", None)
    args = [sys.executable] + (["-S"] if flag_s else []) + [PKG + "/chat/deck_chat.py", "--config", ini]
    log = open(os.path.join(W, tag + ".log"), "w")
    return subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT, env=env), os.path.join(W, tag + ".log")

def info(port, secs=10):
    end = time.time() + secs
    while time.time() < end:
        try:
            return json.load(urllib.request.urlopen("http://127.0.0.1:%d/api/info" % port, timeout=1))
        except Exception:
            time.sleep(0.3)
    return None

p1 = free_port(); a, alog = start(False, p1, "plain")
i1 = info(p1)
txt = open(alog).read()
check("broken protobuf on the path: Deck Chat still starts and answers", i1 is not None, txt[-400:])
check("it says protobuf decoding is off, and why", "decoding OFF" in txt and "AttributeError" in txt, txt[-400:])
check("the page reports takproto off", i1 is not None and i1.get("takproto") is False, i1)
a.terminate(); a.wait(5)

p2 = free_port(); b, blog = start(True, p2, "dash_s")
i2 = info(p2)
check("with python3 -S (the service) the stray copy is never seen: decoding stays on", i2 is not None and i2.get("takproto") is True,
      (i2, open(blog).read()[-300:]))
b.terminate(); b.wait(5)

inst = open(PKG + "/install.sh").read()
check("install.sh starts Chat with python3 -S, like the bridge", '"$PY -S $CURL/chat/deck_chat.py' in inst)
print("%d/%d passed" % (sum(res), len(res)))
sys.exit(0 if all(res) else 1)
