import os as _o, sys as _s  # paths relative to this package
PKG = _o.path.dirname(_o.path.dirname(_o.path.abspath(__file__)))
_s.path[:0] = [PKG + "/vendor", PKG + "/common", PKG + "/dashboard"]
"""wizard_test.py: the first-run setup wizard (Ops /setup), end to end, the way a new user would go through it.
Builds a real OpenTAKServer-style data package (a zip inside a zip, a CA, a server certificate that doesn't list the
address used, a password-protected client .p12, a .pref with the server address), runs a TLS server that demands
that client certificate and answers pings, then drives the wizard's API and its page in a real browser.
Needs openssl; the page part needs playwright."""
import io, json, os, shutil, socket, ssl, subprocess, sys, tempfile, threading, time, urllib.request, zipfile
import setup_wizard as wiz

res = []
def check(n, c, x=""):
    res.append(bool(c)); print(("PASS  " if c else "FAIL  ") + n + ("" if c else "  " + str(x)[:400]), flush=True)

W = tempfile.mkdtemp(prefix="mmb_wizard_")
HOME = W + "/home"; os.makedirs(HOME + "/manet_mesh_bridge")
def sh(*cmd, inp=None):
    r = subprocess.run(list(cmd), capture_output=True, input=inp, cwd=W)
    assert r.returncode == 0, (cmd, r.stderr.decode()[-400:])
    return r.stdout

# ---- 1. a CA, a server certificate (named "tak.lan", so 127.0.0.1 doesn't match it), a client certificate
PW = "s3cret-pw"
sh("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", "ca.key", "-out", "ca.pem", "-days", "30", "-subj", "/CN=Test TAK CA")
for who, cn in (("srv", "tak.lan"), ("cli", "DECK2")):
    sh("openssl", "req", "-newkey", "rsa:2048", "-nodes", "-keyout", who + ".key", "-out", who + ".csr", "-subj", "/CN=" + cn)
    sh("openssl", "x509", "-req", "-in", who + ".csr", "-CA", "ca.pem", "-CAkey", "ca.key", "-CAcreateserial", "-out", who + ".pem", "-days", "30")
sh("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", "rogue.key", "-out", "rogue.pem", "-days", "30", "-subj", "/CN=ROGUE")
sh("openssl", "pkcs12", "-export", "-legacy", "-in", "cli.pem", "-inkey", "cli.key", "-out", "DECK2.p12", "-passout", "pass:" + PW)
sh("openssl", "pkcs12", "-export", "-legacy", "-nokeys", "-in", "ca.pem", "-out", "truststore-root.p12", "-passout", "pass:atakatak")

# ---- 2. a TLS "TAK server": demands the client certificate from this CA, answers pings
srv_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)          # like a real TAK server: no Python-only "strict" checks
srv_ctx.load_verify_locations(W + "/ca.pem")
srv_ctx.load_cert_chain(W + "/srv.pem", W + "/srv.key"); srv_ctx.verify_mode = ssl.CERT_REQUIRED
ls = socket.socket(); ls.bind(("127.0.0.1", 0)); ls.listen(5); SPORT = ls.getsockname()[1]
peers, got = [], []
def serve():
    while True:
        try:
            c, _ = ls.accept()
            s = srv_ctx.wrap_socket(c, server_side=True)
        except (OSError, ssl.SSLError):
            continue
        peers.append(s.getpeercert()["subject"])
        try:
            s.settimeout(5)
            d = s.recv(65536)
            got.append(d)
            if b"t-x-c-t" in d:
                s.sendall(d)
        except OSError:
            pass
        s.close()
threading.Thread(target=serve, daemon=True).start()

def package(with_password_in_pref):
    pref = ('<?xml version="1.0" standalone="yes"?><preferences><preference version="1" name="cot_streams">'
            '<entry key="count" class="class java.lang.Integer">1</entry><entry key="description0" class="class java.lang.String">Test TAK</entry>'
            '<entry key="connectString0" class="class java.lang.String">127.0.0.1:%d:ssl</entry></preference>'
            '<preference version="1" name="com.atakmap.app_preferences">'
            '<entry key="caPassword" class="class java.lang.String">atakatak</entry>%s</preference></preferences>'
            % (SPORT, '<entry key="clientPassword" class="class java.lang.String">%s</entry>' % PW if with_password_in_pref else ""))
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as z:
        z.write(W + "/DECK2.p12", "5c2bfcae/DECK2.p12"); z.write(W + "/truststore-root.p12", "5c2bfcae/truststore-root.p12")
        z.writestr("5c2bfcae/preference.pref", pref); z.writestr("MANIFEST/manifest.xml", "<MissionPackageManifest/>")
    outer = io.BytesIO()
    with zipfile.ZipFile(outer, "w") as z:
        z.writestr("80b82869/TEST.zip", inner.getvalue()); z.writestr("MANIFEST/manifest.xml", "<MissionPackageManifest/>")
    return outer.getvalue()

# ---- 3. the pieces on their own
pkg = wiz.read_package(package(False))
check("package: the zip inside the zip is opened; certificate and CA found", pkg["client"][0] == "DECK2.p12" and pkg["ca"][0] == "truststore-root.p12", pkg)
check("package: server address read from the .pref", wiz.parse_connect(pkg["prefs"]["connect"]) == ("127.0.0.1", SPORT, "ssl"))
for bad, why in ((b"not a zip", "isn't a zip"), (b"", "isn't a zip")):
    try:
        wiz.read_package(bad); check("not a zip is refused", False)
    except wiz.WizardError as e:
        check("not a zip is refused with a plain message", why in str(e), e)
bomb = io.BytesIO()
with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as z:
    z.writestr("big.p12", b"\0" * (wiz.MAX_UNZIPPED + 10))
try:
    wiz.read_package(bomb.getvalue()); check("a zip bomb is refused", False)
except wiz.WizardError as e:
    check("a zip bomb is refused", "unpacks" in str(e), e)
evil = io.BytesIO()
with zipfile.ZipFile(evil, "w") as z:
    z.writestr("../../../../tmp/evil.p12", open(W + "/DECK2.p12", "rb").read())
check("a ../ path in the zip writes nothing anywhere (contents only)", wiz.read_package(evil.getvalue())["client"][0] == "evil.p12" and not os.path.exists("/tmp/evil.p12"))
try:
    wiz.unlock(pkg, ""); check("no password in the package: asks for it", False)
except wiz.WizardError as e:
    check("no password in the package: asks for it", "needs its password" in str(e), e)
try:
    wiz.unlock(pkg, "wrong"); check("wrong password: says so", False)
except wiz.WizardError as e:
    check("wrong password: says so", "wrong password" in str(e), e)
pems = wiz.unlock(pkg, PW)
check("right password: certificate, key and CA converted to PEM", pems["cert"].startswith(b"-----BEGIN CERTIFICATE") and b"PRIVATE KEY" in pems["key"] and b"BEGIN CERTIFICATE" in pems["ca"])
check("certificate details read (name, expiry)", pems["info"].get("name") == "DECK2" and pems["info"].get("until"), pems["info"])
check("password from the package is used when it has one", wiz.unlock(wiz.read_package(package(True)), "")["used_package_password"])
check("a typed password is reported as typed, not as from the package", wiz.unlock(pkg, PW)["used_package_password"] is False)
t = wiz.test_server("127.0.0.1", SPORT, pems)
check("server test: connects with the deck's certificate, server answers the ping", t["ok"] and t["answers"], t)
check("server test: the address isn't in the server's certificate, so the name check is turned off (and it says so)",
      t["hostname_check"] is False and "name check" in t["detail"], t)
check("the server saw the deck's certificate", any(("commonName", "DECK2") in x for p in peers for x in p), peers)
bad_pems = dict(pems, cert=open(W + "/rogue.pem", "rb").read(), key=open(W + "/rogue.key", "rb").read())   # not from this server's CA
rb = wiz.test_server("127.0.0.1", SPORT, dict(bad_pems, ca=pems["ca"]))
check("server test: a certificate this server didn't issue for a client is refused, with a reason", not rb["ok"] and rb["detail"], rb)
free = socket.socket(); free.bind(("127.0.0.1", 0)); fp = free.getsockname()[1]; free.close()
r = wiz.test_server("127.0.0.1", fp, pems)
check("server test: nothing listening -> 'can't reach', with a hint", not r["ok"] and "can't reach" in r["detail"], r)

# ---- 4. deck.ini: only the right lines change, comments stay, a backup is kept
ini = HOME + "/manet_mesh_bridge/deck.ini"
shutil.copy(PKG + "/config/deck.ini.example", ini)
before = open(ini).read()
ses = {"pems": pems, "test": t}
choices = {"host": "127.0.0.1", "port": SPORT, "callsign": "DECK2", "team": "Blue", "role": "Team Lead", "gps": False,
           "return_path": True, "dm_relay": False, "ops_local": True, "mesh_iface": None}
certs = HOME + "/certs"
out = wiz.finish(ses, choices, ini, certs)
after = open(ini).read()
import configparser
cp = configparser.ConfigParser(interpolation=None, strict=False); cp.read(ini)
check("deck.ini: server and the certificate lines switched on, as full paths (PyTAK doesn't know '~')",
      cp.get("bridge", "COT_URL") == "tls://127.0.0.1:%d" % SPORT and cp.get("bridge", "PYTAK_TLS_CLIENT_CERT") == certs + "/deck.pem", dict(cp["bridge"]))
check("deck.ini: name check off (the address isn't in the server's certificate)", cp.get("bridge", "PYTAK_TLS_DONT_CHECK_HOSTNAME") == "1")
check("deck.ini: callsign, team, role in [deck]", (cp.get("deck", "CALLSIGN"), cp.get("deck", "TEAM"), cp.get("deck", "ROLE")) == ("DECK2", "Blue", "Team Lead"))
check("deck.ini: Ops on this deck only", cp.get("dashboard", "LISTEN") == "127.0.0.1")
check("deck.ini: no mesh port chosen -> MESH_ADDR left as it was (auto)", cp.get("deck", "MESH_ADDR") == "auto")
lost = [l for l in before.splitlines() if l.startswith("#") and l not in after.splitlines()
        and not any(k in l for k in ("PYTAK_TLS_CLIENT_CERT", "PYTAK_TLS_CLIENT_KEY", "PYTAK_TLS_CLIENT_CAFILE", "PYTAK_TLS_DONT_CHECK_HOSTNAME"))]
check("deck.ini: every other comment line kept", not lost, lost[:3])
check("deck.ini: same number of lines (lines switched on, none added or lost)", len(after.splitlines()) == len(before.splitlines()), (len(before.splitlines()), len(after.splitlines())))
check("deck.ini: a backup of the old file, mode 600", open(out["backup"]).read() == before and oct(os.stat(out["backup"]).st_mode & 0o777) == "0o600")
check("deck.ini stays mode 600", oct(os.stat(ini).st_mode & 0o777) == "0o600")
check("certificates in ~/certs: 600 files in a 700 folder", all(oct(os.stat(certs + "/" + n).st_mode & 0o777) == "0o600" for n in ("deck.pem", "deck.key", "ca.pem"))
      and oct(os.stat(certs).st_mode & 0o777) == "0o700")
try:
    wiz.set_ini(ini, {"deck": {"CALLSIGN": "X\n[bridge]\nCOT_URL = evil"}}); check("line break refused", False)
except wiz.WizardError:
    check("a setting with a line break is refused", "evil" not in open(ini).read())
try:
    wiz.plan(ses, dict(choices, callsign="bad/name!"), certs); check("bad callsign refused", False)
except wiz.WizardError as e:
    check("a bad callsign is refused with a plain message", "callsign" in str(e))
added = wiz.set_ini(ini, {"voice": {"BRAND_NEW": "1"}, "nosuch": {"X": "1"}})
cp2 = configparser.ConfigParser(interpolation=None, strict=False); cp2.read(ini)
check("a key with no line yet is added inside its section; a missing section is added", cp2.get("voice", "BRAND_NEW") == "1" and cp2.get("nosuch", "X") == "1")
wiz.set_ini(ini, {"bridge": {"PYTAK_TLS_DONT_CHECK_HOSTNAME": None}})
cp3 = configparser.ConfigParser(interpolation=None, strict=False); cp3.read(ini)
check("None turns a live setting back into a comment", not cp3.has_option("bridge", "PYTAK_TLS_DONT_CHECK_HOSTNAME") and "# PYTAK_TLS_DONT_CHECK_HOSTNAME = 1" in open(ini).read())

# ---- 5. through Ops, as the page does it (HTTP): local only, then the whole flow
shutil.copy(PKG + "/config/deck.ini.example", ini)
s = socket.socket(); s.bind(("127.0.0.1", 0)); DP = s.getsockname()[1]; s.close()
with open(ini) as f:
    txt = f.read()
txt = txt.replace("[dashboard]\n", "[dashboard]\nPORT = %d\nSETUP_CERTS_DIR = %s\nEVENT_FILE = %s/ev.jsonl\nHISTORY_FILE = %s/h.jsonl\nGPS = off\n" % (DP, certs + "2", W, W), 1)
open(ini, "w").write(txt)
env = dict(os.environ, HOME=HOME, MMB_SETUP_NO_RESTART="1")      # never restart this machine's real services
dash = subprocess.Popen([sys.executable, PKG + "/dashboard/dashboard_server.py", "--config", ini, "--port", str(DP), "--listen", "127.0.0.1"],
                        env=env, stdout=open(W + "/dash.log", "w"), stderr=subprocess.STDOUT)
base = "http://127.0.0.1:%d" % DP
for _ in range(80):
    try: urllib.request.urlopen(base + "/healthz", timeout=1); break
    except Exception: time.sleep(0.2)
def call(method, path, body=None, raw=None, headers=None, host=None):
    h = {"X-Deck": "1"}; h.update(headers or {})
    data = None
    if raw is not None: data = raw; h["Content-Type"] = "application/zip"
    elif body is not None: data = json.dumps(body).encode(); h["Content-Type"] = "application/json"
    if host: h["Host"] = host
    req = urllib.request.Request(base + path, data=data, headers=h, method=method)
    try:
        r = urllib.request.urlopen(req, timeout=60); return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
try:
    code, _ = call("GET", "/setup", host="10.41.0.50:%d" % DP)
    check("setup refuses a request not addressed to the deck itself (403)", code == 403, code)
    code, body = call("POST", "/api/setup/package", raw=package(False), headers={"X-Deck": "0"})
    check("upload without the page's header is refused", code == 400, code)
    code, body = call("GET", "/setup")
    check("the page is served on the deck's own screen", code == 200 and b"TAK MESH TOC" in body and b"setup.js" in body, code)
    code, body = call("GET", "/api/setup/state"); st = json.loads(body)
    check("state: a fresh deck needs setup; facts, ports, teams, roles, current values", st["needed"] and st["facts"] and st["teams"] and st["current"]["callsign"], list(st))
    code, body = call("POST", "/api/setup/package", raw=package(False), headers={"X-File-Name": "TEST_CONFIG.zip"}); r = json.loads(body)
    check("upload: package read, asks for the password", r.get("need_password") is True, r)
    code, body = call("POST", "/api/setup/unlock", {"password": PW}); r = json.loads(body)
    check("unlock: certificate shown, server address filled in from the package", r["ok"] and r["name"] == "DECK2" and r["host"] == "127.0.0.1" and r["port"] == SPORT, r)
    code, body = call("POST", "/api/setup/test", {"host": "127.0.0.1", "port": SPORT}); r = json.loads(body)
    check("test connection through Ops: OK, server answers", r["ok"] and r["answers"], r)
    ch = dict(choices, callsign="DECK2")
    code, body = call("POST", "/api/setup/plan", {"choices": ch}); r = json.loads(body)
    check("plan: shows what will be written before anything is", r["ok"] and r["changes"]["bridge"]["COT_URL"].endswith(":%d" % SPORT) and not os.path.exists(certs + "2"), r)
    code, body = call("POST", "/api/setup/finish", {"choices": ch}); r = json.loads(body)
    check("finish: saved, with a backup; restart is automatic or the page asks for meshtoc restart", r["ok"] and r["restart"] in ("auto", "manual") and os.path.exists(r["backup"]), r)
    check("finish: certificates written to the folder Ops was told", os.path.exists(certs + "2/deck.pem") and os.path.exists(certs + "2/ca.pem"))
    code, body = call("GET", "/api/setup/helper"); h = json.loads(body)
    check("server helper: the install command names this deck's UID", h["uid"] in h["command"] and h["file"].endswith(".zip"), h)
    code, body = call("GET", "/api/setup/helper.zip")
    names = zipfile.ZipFile(io.BytesIO(body)).namelist()
    check("server helper zip: the installer and the helper, nothing else", "mesh_mailroute_src/install_server.sh" in names and "mesh_mailroute_src/mesh_mailroute.py" in names and len(names) <= 5, names)
    # the REAL bridge with that deck.ini, but with the certificate paths written "~/..." as the README and the
    # example show: it must expand them, connect with the deck's certificate and introduce itself
    t2 = open(ini).read().replace(certs + "2/", "~/" + os.path.basename(certs) + "2/")
    open(ini, "w").write(t2)
    check("(the deck.ini now uses ~/ paths, like the README)", "= ~/certs2/deck.pem" in t2)
    n0 = len(got)
    br = subprocess.Popen([sys.executable, "-S", PKG + "/bridge/tak_mesh_bridge.py", ini], env=env, stdout=open(W + "/bridge.log", "w"), stderr=subprocess.STDOUT)
    for _ in range(100):
        if any(b"DECK-" in x for x in got[n0:]):
            break
        time.sleep(0.2)
    br.terminate()
    try: br.wait(5)
    except subprocess.TimeoutExpired: br.kill()
    blog = open(W + "/bridge.log").read()
    check("the real bridge, with ~/ certificate paths: connects with the deck's certificate and introduces itself",
          any(b"DECK-" in x for x in got[n0:]) and "Resource not found" not in blog, blog[-600:])
    ev = open(HOME + "/dashboard_events.jsonl").read()        # deck.ini.example's EVENT_FILE (it comes later in [dashboard])
    check("an event says setup was saved", "Setup saved" in ev)

    # ---- 6. the page, in a real browser, step by step (if playwright is here)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sync_playwright = None
    if sync_playwright:
        dash.terminate(); dash.wait(10)                      # a fresh Ops, as on a first boot (no package in memory)
        shutil.copy(PKG + "/config/deck.ini.example", ini + ".fresh")
        txt = open(ini + ".fresh").read().replace("[dashboard]\n", "[dashboard]\nPORT = %d\nSETUP_CERTS_DIR = %s\nEVENT_FILE = %s/ev.jsonl\nHISTORY_FILE = %s/h.jsonl\nGPS = off\n" % (DP, certs + "3", W, W), 1)
        open(ini, "w").write(txt)
        open(W + "/TEST_CONFIG.zip", "wb").write(package(False))
        dash = subprocess.Popen([sys.executable, PKG + "/dashboard/dashboard_server.py", "--config", ini, "--port", str(DP), "--listen", "127.0.0.1"],
                                env=env, stdout=open(W + "/dash2.log", "w"), stderr=subprocess.STDOUT)
        for _ in range(80):
            try: urllib.request.urlopen(base + "/healthz", timeout=1); break
            except Exception: time.sleep(0.2)
        with sync_playwright() as pw:
            b = pw.chromium.launch()
            # a browser that kept 3.1's permanent "skip" still gets the wizard (3.1.2 forgets it)
            c0 = b.new_context(viewport={"width": 1024, "height": 600}); p0 = c0.new_page()
            p0.goto(base + "/healthz"); p0.evaluate("localStorage.setItem('deck.setup-later', '1')")
            p0.goto(base + "/"); p0.wait_for_url("**/setup", timeout=15000)
            check("page: a 'skip' kept by an older version doesn't stop the wizard", p0.url.endswith("/setup"))
            p0.click("#skip"); p0.wait_for_selector("#setupbar.on", timeout=15000)
            check("page: after Skip, Ops shows the 'not set up yet' banner with the button", p0.is_visible("#sb-go") and "SET UP THIS DECK" in p0.inner_text("#setupbar"))
            p0.wait_for_timeout(4500)
            check("page: ...and Skip holds for this browser session (no bounce back)", not p0.url.endswith("/setup"))
            p0.click("#sb-go"); p0.wait_for_url("**/setup", timeout=5000)
            check("page: the banner button opens the wizard", p0.url.endswith("/setup"))
            c0.close()
            js = open(PKG + "/dashboard/app.js").read()
            r = js[js.index("function render(d)"):]
            check("page: the wizard check runs before the panels draw (a panel error can't block it)", r.index("firstRun(d)") < r.index("banner(d)"))
            pg = b.new_page(viewport={"width": 1024, "height": 600})
            pg.goto(base + "/"); pg.wait_for_url("**/setup", timeout=15000)
            check("page: Ops opens the wizard by itself on a deck that isn't set up (new browser session)", pg.url.endswith("/setup"))
            pg.wait_for_selector("#facts .row >> nth=2")
            check("page step 1: the deck checks are listed", pg.locator("#facts .row").count() >= 5)
            pg.click("#next")
            check("page step 2: Next is off until a package is read", pg.is_disabled("#next"))
            pg.set_input_files("#pkgfile", W + "/TEST_CONFIG.zip")
            pg.wait_for_selector("#pwbox:not(.hidden)")
            pg.fill("#pw", PW); pg.click("#unlock")
            pg.wait_for_selector("#pkginfo:not(.hidden)")
            check("page step 2: certificate name and server address shown", "DECK2" in pg.inner_text("#pkgrows") and pg.input_value("#host") == "127.0.0.1")
            check("page step 2: a typed password isn't claimed to be from the package", "came with the package" not in pg.inner_text("#pkgnote"))
            pg.click("#test"); pg.wait_for_selector("#testres.ok", timeout=20000)
            check("page step 2: test connection OK", "[ OK ]" in pg.inner_text("#testres"))
            pg.screenshot(path=W + "/wizard_step2.png")
            pg.click("#next")
            check("page step 3: the mesh ports and 'Later' are offered", pg.locator("input[name=mesh]").count() >= 1)
            pg.click("#next")
            pg.fill("#cs", "DECK2"); pg.click("#swatches button[data-team=Blue]"); pg.select_option("#role", "Team Lead")
            pg.screenshot(path=W + "/wizard_step4.png")
            pg.click("#next"); pg.click("#next")
            check("page step 6: no DM relay -> nothing to install", pg.is_visible("#helper-off"))
            pg.click("#next"); pg.wait_for_selector("#plan .kv")
            plan_txt = pg.inner_text("#plan")
            check("page step 7: the plan lists what will be written", "COT_URL" in plan_txt and "DECK2" in plan_txt and "Blue" in plan_txt, plan_txt[:300])
            pg.screenshot(path=W + "/wizard_step7.png")
            pg.click("#next"); pg.wait_for_function("document.getElementById('fin-title').textContent !== 'Saving…'", timeout=20000)
            t = pg.inner_text("#fin-title")
            check("page: Save and restart -> saved, and since it can't restart here it shows the one command", t.startswith("Saved") and pg.is_visible("#restart"), t)
            check("page: the Save button is gone once saved; Open Ops is offered", not pg.is_visible("#next") and pg.is_visible("#toops"))
            cp4 = configparser.ConfigParser(interpolation=None, strict=False); cp4.read(ini)
            check("page: deck.ini really has the new server, callsign and team", cp4.get("bridge", "COT_URL").endswith(":%d" % SPORT) and cp4.get("deck", "CALLSIGN") == "DECK2" and cp4.get("deck", "TEAM") == "Blue")
            pg.set_viewport_size({"width": 390, "height": 844}); pg.wait_for_timeout(300)
            pg.set_viewport_size({"width": 1024, "height": 600}); pg.screenshot(path=W + "/wizard_done.png")
            for _ in range(60):                          # Ops re-reads deck.ini every few seconds
                if not any(c["title"] == "TAK server not set up yet" for c in json.loads(call("GET", "/api/status")[1]).get("setup", [])): break
                time.sleep(0.5)
            p3 = b.new_page(viewport={"width": 1024, "height": 600}); p3.goto(base + "/"); p3.wait_for_selector("#t-bridge .v"); p3.wait_for_timeout(5000)
            check("page: once set up, Ops stays put and the banner is gone", not p3.url.endswith("/setup") and not p3.is_visible("#setupbar"), p3.url)
            p3.close()
            pg.set_viewport_size({"width": 390, "height": 844}); pg.wait_for_timeout(300)
            check("page: no sideways scrolling at phone width", pg.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"))
            b.close()
    else:
        print("SKIP  page checks (no playwright)")
finally:
    dash.terminate()
    try: dash.wait(5)
    except subprocess.TimeoutExpired: dash.kill()

if os.environ.get("WIZARD_SHOTS"):                          # keep the screenshots (for review)
    for n in ("step2", "step4", "step7", "done"):
        if os.path.exists(W + "/wizard_%s.png" % n):
            shutil.copy(W + "/wizard_%s.png" % n, os.path.join(os.environ["WIZARD_SHOTS"], "wizard_%s.png" % n))
shutil.rmtree(W, ignore_errors=True)
print("%d/%d passed" % (sum(res), len(res)))
sys.exit(0 if all(res) else 1)
