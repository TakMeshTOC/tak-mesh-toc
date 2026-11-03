import os as _o, sys as _s  # paths relative to this package
PKG = _o.path.dirname(_o.path.dirname(_o.path.abspath(__file__)))
_s.path[:0] = [PKG + "/vendor", PKG + "/common", PKG + "/chat", PKG + "/dashboard"]
"""look_test.py - the one look (pro) in the dashboard and Deck Chat, and Deck Chat's Voice page.

  * deckcommon: a leftover THEME line gets a note; bundled fonts only
  * both servers: serve the pages in the pro look, serve the fonts, refuse anything else under /fonts/
  * no bunny, no theme switcher left anywhere (the bunny's 50 ms animation timer is gone: less CPU and heat)
  * check command: says a leftover THEME line is no longer used
  * Deck Chat /api/voice: not running / running / stale, from manet-voice's status file
  * in a browser (playwright, if installed): no script errors, fonts load, nothing scrolls sideways at phone width,
    short windows keep MESH + DECK visible, the Voice page shows talk groups, the talker, both sides and the log,
    the dashboard's voice row, and the VOICE tab's talking dot in Deck Chat
"""
import json, os, re, socket, subprocess, sys, time, urllib.request, urllib.error
import deckcommon as dcm

W = "/tmp/mmb_look_test"; os.makedirs(W, exist_ok=True)
res = []
def check(n, c, x=""):
    res.append(bool(c)); print(("PASS  " if c else "FAIL  ") + n + ("" if c else "  " + str(x)[:300]))

def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p

def get(url):
    try:
        r = urllib.request.urlopen(url, timeout=5)
        return r.status, r.headers.get("Content-Type", ""), r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type", ""), e.read()

def wait_up(url, secs=12):
    end = time.time() + secs
    while time.time() < end:
        try:
            urllib.request.urlopen(url, timeout=1); return True
        except Exception:
            time.sleep(0.3)
    return False

# ---- deckcommon and the sources ----------------------------------------------------------------
check("a leftover THEME line gets a note; blank does not", "no longer used" in (dcm.theme_note("green") or "") and not dcm.theme_note("") and not dcm.theme_note(None))
check("THEME is gone from deckcommon (no theme_name / page_theme / THEMES)", not any(hasattr(dcm, n) for n in ("theme_name", "page_theme", "THEMES")))
fonts = sorted(f for f in os.listdir(dcm.FONT_DIR) if f.endswith(".woff2"))
check("8 fonts bundled, with their licenses", len(fonts) == 8 and all(os.path.exists(os.path.join(dcm.FONT_DIR, "OFL-%s.txt" % n)) for n in ("Barlow-Condensed", "IBM-Plex-Sans", "IBM-Plex-Mono")), fonts)
check("font_file: bundled fonts only", dcm.font_file(fonts[0]) and not any(dcm.font_file(n) for n in ("../VERSION", "OFL-IBM-Plex-Sans.txt", "", "/etc/passwd", "..%2fVERSION")))
PAGES = (PKG + "/dashboard/index.html", PKG + "/chat/index.html", PKG + "/chat/voice.html")
check("every font the pages ask for is bundled", all(n in fonts for page in PAGES for n in re.findall(r"/fonts/([\w.-]+\.woff2)", open(page).read())))
src = {p: open(p).read() for p in PAGES + (PKG + "/dashboard/app.js", PKG + "/dashboard/dashboard_server.py", PKG + "/chat/deck_chat.py")}
check("bunny_sprite.js is gone", not os.path.exists(PKG + "/dashboard/bunny_sprite.js") and not any("bunny_sprite" in s for s in src.values()))
check("no animation timer in the dashboard (only the 1 s / 2 s pollers and the clock)",
      not re.search(r"setInterval\([^,]+,\s*(\d{1,3})\)", src[PKG + "/dashboard/app.js"]), re.findall(r"setInterval\([^)]*\)", src[PKG + "/dashboard/app.js"]))
check("no green or amber look left in the pages", not any(re.search(r'data-theme|#3dff7a|h-theme|id="theme"', s) for s in src.values()))
src_chat = open(PKG + "/chat/index.html").read()
check("Deck Chat: held/failed message ticks are amber/red", ".m .tick.warn { color: var(--amber); }" in src_chat and ".m .tick.bad { color: var(--red); }" in src_chat)
check("one stylesheet per page: no theme layer, no glow variable, no bunny left in the dashboard",
      not any(re.search(r'\[data-theme|--sh\b|bunny', src[p]) for p in (PKG + "/dashboard/index.html", PKG + "/chat/index.html", PKG + "/dashboard/app.js", PKG + "/dashboard/dashboard_server.py")))
check("Deck Chat: no /night, /green or /pro commands", not re.search(r"""['"]/(night|green|pro)['"]""", src[PKG + "/chat/index.html"]))
pages = {k: open(PKG + k).read() for k in ("/dashboard/index.html", "/chat/index.html", "/chat/voice.html")}
check("TAK MESH TOC: page titles and headers", "<title>TAK MESH TOC · Ops</title>" in pages["/dashboard/index.html"] and "<title>TAK MESH TOC · Chat</title>" in pages["/chat/index.html"]
      and "<title>TAK MESH TOC · Voice</title>" in pages["/chat/voice.html"] and all('TAK MESH TOC <span class="sub">' in v for v in pages.values()))
icons = [open(PKG + "/dashboard/icon.svg").read(), open(PKG + "/chat/icon.svg").read()]
check("TAK MESH TOC: icons in the slate / steel-blue look (no old green)", all("#121921" in i and "#6ea8d8" in i and "#3dff7a" not in i for i in icons))
check("the example config has no THEME line", not re.search(r"^\s*#?\s*THEME\s*=", open(PKG + "/config/deck.ini.example").read(), re.M))

# ---- dashboard server --------------------------------------------------------------------------
VS = W + "/voice_status.json"
def dashboard(extra=""):
    port = free_port(); ini = W + "/dash_%d.ini" % port
    open(ini, "w").write("[deck]\n%s\n[dashboard]\nPORT = %d\nLISTEN = 127.0.0.1\nVOICE_STATUS_FILE = %s\n" % (extra, port, VS))
    p = subprocess.Popen([sys.executable, PKG + "/dashboard/dashboard_server.py", "--config", ini, "--mock", "hop", "--port", str(port), "--listen", "127.0.0.1"],
                         stdout=open(W + "/dash_%d.log" % port, "w"), stderr=subprocess.STDOUT)
    return p, port, wait_up("http://127.0.0.1:%d/healthz" % port)

procs = []
p, dport, ok = dashboard("THEME = green"); procs.append(p)
def alert_dashboard():
    port = free_port(); ini = W + "/dash_a%d.ini" % port
    open(ini, "w").write("[dashboard]\nPORT = %d\nLISTEN = 127.0.0.1\nVOICE_STATUS_FILE = %s\n" % (port, VS))
    p = subprocess.Popen([sys.executable, PKG + "/dashboard/dashboard_server.py", "--config", ini, "--mock", "alert", "--port", str(port), "--listen", "127.0.0.1"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    wait_up("http://127.0.0.1:%d/healthz" % port); return p, port
pa, adport = alert_dashboard(); procs.append(pa)
check("dashboard starts (a leftover THEME line is harmless)", ok)
st, ct, body = get("http://127.0.0.1:%d/" % dport)
check("dashboard serves its page (bundled fonts, slate look)", st == 200 and b'<html lang="en">' in body and b"/fonts/ibm-plex-sans-400.woff2" in body, body[:120])
check("dashboard: /bunny_sprite.js is gone", get("http://127.0.0.1:%d/bunny_sprite.js" % dport)[0] == 404)
st, ct, body = get("http://127.0.0.1:%d/fonts/ibm-plex-sans-400.woff2" % dport)
check("dashboard serves a bundled font as font/woff2", st == 200 and ct == "font/woff2" and body[:4] == b"wOF2", (st, ct))
check("dashboard refuses other files under /fonts/", all(get("http://127.0.0.1:%d/fonts/%s" % (dport, n))[0] == 404 for n in ("OFL-IBM-Plex-Sans.txt", "..%2F..%2FVERSION", "nope.woff2")))

# ---- Deck Chat server + /api/voice -------------------------------------------------------------
if os.path.exists(VS):
    os.remove(VS)
cport = free_port()
open(W + "/chat.ini", "w").write("[deck]\nTHEME = amber\n[chat]\nUID = DECK-look\nMESH_LOCAL_ADDR = 127.0.0.1\nLISTEN = 127.0.0.1:%d\nHISTORY_FILE = %s/hist.jsonl\n"
                                 "BRIDGE_STATUS_FILE = %s/none.json\nGPS = off\nSA_SEND = 0\nVOICE_STATUS_FILE = %s\nSA_GROUP = off\n"
                                 "BRIDGE_COPY_LISTEN = 127.0.0.1:%d\nDM_PORT = %d\nVOICE_CONTROL = 127.0.0.1:%d\n" % (cport, W, W, VS, free_port(), free_port(), free_port()))
cp = subprocess.Popen([sys.executable, PKG + "/chat/deck_chat.py", "--config", W + "/chat.ini"], stdout=open(W + "/chat.log", "w"), stderr=subprocess.STDOUT)
procs.append(cp)
C = "http://127.0.0.1:%d" % cport
check("Deck Chat starts", wait_up(C + "/healthz"))
st, ct, body = get(C + "/")
check("Deck Chat serves its page with CHAT / VOICE tabs", st == 200 and b'<html lang="en">' in body and b'href="/voice"' in body, body[:120])
st, ct, body = get(C + "/voice")
check("Deck Chat serves the Voice page at /voice", st == 200 and "text/html" in ct and b"TALK GROUPS" in body, (st, ct))
st, ct, body = get(C + "/fonts/barlow-condensed-700.woff2")
check("Deck Chat serves a bundled font", st == 200 and ct == "font/woff2" and body[:4] == b"wOF2", (st, ct))
check("Deck Chat refuses other files under /fonts/", all(get(C + "/fonts/%s" % n)[0] == 404 for n in ("OFL-IBM-Plex-Mono.txt", "..%2F..%2FVERSION")))
v = json.loads(get(C + "/api/voice")[2])
check("/api/voice with no manet-voice: running false, with a reason", v.get("running") is False and "not running" in v.get("reason", ""), v)

def voice_status(talking=True):
    tg1 = {"name": "TG1", "port": 38801, "channel": "TG1", "bridged": True, "mumble_up": True, "in_channel": True, "rtt_ms": 41,
           "talking": {"name": "ALPHA-2", "via": "mesh", "secs": 4.2} if talking else None,
           "mesh": [{"name": "ALPHA-2", "ip": "10.41.0.20", "what": "ATAK phone", "talking": talking},
                    {"name": "BRAVO-1", "ip": "10.41.0.21", "what": "ATAK phone", "talking": False},
                    {"name": "10.41.254.10", "ip": "10.41.254.10", "what": "node or device", "talking": False}],
           "mumble": [{"name": "ops-iphone", "talking": False}, {"name": "<b>x</b>", "talking": False}],
           "log": [{"ts": time.time() - 30, "who": "ops-iphone", "via": "Mumble", "len": 3.1, "blocked": False},
                   {"ts": time.time() - 60, "who": "ops-iphone (blocked: ALPHA-2 talking)", "via": "Mumble", "len": 0, "blocked": True},
                   {"ts": time.time() - 90, "who": "BRAVO-1", "via": "mesh", "len": 6.4, "blocked": False}], "stats": {}}
    tg2 = {"name": "TG2", "port": 38803, "channel": "TG2", "bridged": True, "mumble_up": True, "in_channel": True, "rtt_ms": 40,
           "talking": None, "mesh": [], "mumble": [{"name": "watch-desk", "talking": False}], "log": [], "stats": {}}
    off = [{"name": "TG%d" % n, "port": 38799 + 2 * n, "channel": "TG%d" % n, "bridged": False, "mumble_up": False, "in_channel": False,
            "rtt_ms": None, "talking": None, "mesh": [], "mumble": [], "log": [], "stats": {}} for n in (3, 4, 5)]
    json.dump({"schema": 1, "ts": time.time(), "mesh_group": "239.192.41.1", "mumble": "100.64.0.9:64738", "groups": [tg1, tg2] + off}, open(VS, "w"))
voice_status()
v = json.loads(get(C + "/api/voice")[2])
check("/api/voice with a fresh status: running, 5 talk groups, the talker", v.get("running") is True and len(v["groups"]) == 5 and v["groups"][0]["talking"]["name"] == "ALPHA-2", v)
check("/api/voice does not pass on the Mumble server address", "100.64.0.9" not in json.dumps(v), list(v))
old = time.time() - 60; os.utime(VS, (old, old))
v = json.loads(get(C + "/api/voice")[2])
check("/api/voice with a stale status file: running false", v.get("running") is False, v)
voice_status()

# Listening and the bridge switches: the deck's own screen only. A request from another address is refused.
def other_ip():
    out = subprocess.run(["ip", "-4", "-o", "addr"], capture_output=True, text=True).stdout
    ips = [l.split()[3].split("/")[0] for l in out.splitlines() if " lo " not in l and len(l.split()) > 3]
    return ips[0] if ips else None
check("listen / bridge from the deck's screen with no voice service: a clear 503",
      get(C + "/api/voice/listen?tg=TG1")[0] == 503)
ip = other_ip()
if ip:
    rport = free_port()
    open(W + "/chat_r.ini", "w").write(open(W + "/chat.ini").read().replace("LISTEN = 127.0.0.1:%d" % cport, "LISTEN = %s:%d" % (ip, rport)).replace("hist.jsonl", "hist_r.jsonl")
        .replace("BRIDGE_COPY_LISTEN = ", "BRIDGE_COPY_LISTEN = 127.0.0.1:%d\n# was: " % free_port()).replace("DM_PORT = ", "DM_PORT = %d\n# was: " % free_port()))
    rp = subprocess.Popen([sys.executable, PKG + "/chat/deck_chat.py", "--config", W + "/chat_r.ini"], stdout=open(W + "/chat_r.log", "w"), stderr=subprocess.STDOUT)
    procs.append(rp)
    R = "http://%s:%d" % (ip, rport)
    wait_up(R + "/healthz"); voice_status()
    v = json.loads(get(R + "/api/voice")[2])
    check("from another device: the Voice page shows, but control is off", v.get("running") is True and v.get("control") is False, v.get("control"))
    check("from another device: listening is refused (403)", get(R + "/api/voice/listen?tg=TG1")[0] == 403)
    rq = urllib.request.Request(R + "/api/voice/bridge", b'{"tg":"TG2","on":true}', {"Content-Type": "application/json", "X-Deck": "1"})
    try:
        code = urllib.request.urlopen(rq, timeout=5).status
    except urllib.error.HTTPError as e:
        code = e.code
    check("from another device: the bridge switch is refused (403)", code == 403, code)
else:
    print("SKIP  other-device checks: no non-loopback address")

# ---- check command ------------------------------------------------------------------------------
src_check = open(PKG + "/tools/manet_mesh_bridge.py").read()
check("check command notes a leftover THEME line (source check)", "theme_note" in src_check and "You can delete that line" in src_check)

# ---- in a browser -------------------------------------------------------------------------------
try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None
if sync_playwright is None:
    print("SKIP  browser checks: playwright not installed")
else:
    with sync_playwright() as pw:
        br = pw.chromium.launch()
        def page(url, w=1280, h=720, wait=3500):
            ctx = br.new_context(viewport={"width": w, "height": h})
            pg = ctx.new_page(); errs = []
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
            pg.goto(url); pg.wait_for_timeout(wait)
            pg.evaluate("document.fonts.ready")
            return ctx, pg, errs
        ctx, pg, errs = page("http://127.0.0.1:%d/" % dport)
        info = pg.evaluate("""() => ({ t: document.documentElement.dataset.theme,
            canvas: document.querySelectorAll('canvas.bunny, #bunny, .bunny').length, rate: getComputedStyle(document.querySelector('.rate')).display,
            fonts: [...document.fonts].filter(f => f.status === 'loaded').map(f => f.family.replace(/"/g, '')),
            sw: document.documentElement.scrollWidth, bg: getComputedStyle(document.body).backgroundColor,
            ok: getComputedStyle(document.querySelector('#t-bridge')).getPropertyValue('--c').trim(),
            rows: [...document.querySelectorAll('#d-rows dt')].map(e => e.textContent),
            voice: (() => { const dt = [...document.querySelectorAll('#d-rows dt')].find(e => e.textContent === 'voice'); return dt ? dt.nextElementSibling.textContent : null; })() })""")
        check("dashboard: no script errors", not errs, errs)
        check("dashboard: no bunny, msg/min shown", info["canvas"] == 0 and info["rate"] != "none", info)
        check("dashboard: bundled fonts load (no internet)", {"Barlow Condensed", "IBM Plex Mono", "IBM Plex Sans"} <= set(info["fonts"]), info["fonts"])
        check("dashboard: slate background, green only for ok", info["bg"] == "rgb(12, 17, 23)" and info["ok"].lower() == "#4cc38a", info)
        check("dashboard: fits 1280 x 720 (Touch Display 2) without sideways scrolling", info["sw"] <= 1280, info["sw"])
        check("dashboard: voice row in MESH + DECK shows who is talking", info["voice"] and "ALPHA-2" in info["voice"] and "TG1" in info["voice"], info["rows"])
        ctx2, pg2, errs2 = page("http://127.0.0.1:%d/" % adport)
        sb = pg2.evaluate("(() => { const b = document.getElementById('h-tools'); return [b.className, getComputedStyle(b).color]; })()")
        check("dashboard: SETUP turns amber/red when setup has problems", ("lv-warn" in sb[0] and sb[1] == "rgb(227, 163, 59)") or ("lv-bad" in sb[0] and sb[1] == "rgb(240, 106, 99)"), sb)
        ctx2.close()
        pg.click("button[data-tab=mesh]"); pg.wait_for_timeout(1500)
        check("dashboard: MESH tab draws without errors", not errs, errs)
        mt = pg.evaluate("""() => ({ rows: document.querySelectorAll('#mt-nodes tr').length, links: document.querySelectorAll('#mt-links .lk').length,
            tabs: [...document.querySelectorAll('.tabs button')].map(b => b.textContent) })""")
        check("dashboard: MESH tab lists nodes and radio links; TRENDS is gone", mt["rows"] >= 3 and mt["links"] >= 1 and mt["tabs"] == ["VIDEO", "MESH"], mt)
        pg.screenshot(path=W + "/dashboard.png"); ctx.close()
        ctx, pg, errs = page("http://127.0.0.1:%d/" % dport, 390, 844)
        check("dashboard: no sideways scrolling at phone width", pg.evaluate("document.documentElement.scrollWidth") <= 390 and not errs, errs)
        ctx.close()
        for w, h in ((1280, 720), (1280, 560), (1024, 600), (1100, 500)):
            ctx, pg, errs = page("http://127.0.0.1:%d/" % dport, w, h, 2500)
            m = pg.evaluate("""() => { const r = id => document.getElementById(id).getBoundingClientRect();
                return { deck: document.getElementById('p-deck').clientHeight, deckBot: r('p-deck').bottom, logTop: r('log').top,
                         server: document.getElementById('p-server').clientHeight, docH: document.documentElement.scrollHeight }; }""")
            ok = m["deck"] >= 90 and m["server"] >= 90 and m["deckBot"] <= m["logTop"] + 1 and (h < 700 or m["docH"] <= h)
            check("dashboard %dx%d: mesh + deck and server link both visible, nothing overlaps%s" % (w, h, ", no page scroll" if h >= 700 else ""), ok and not errs, (m, errs))
            ctx.close()
        # Deck Chat (a fresh status: the file only counts as running for 10 s)
        voice_status()
        ctx, pg, errs = page(C + "/")
        info = pg.evaluate("""() => ({ t: document.documentElement.dataset.theme,
            rx: document.getElementById('roster').getBoundingClientRect().x, lx: document.getElementById('log').getBoundingClientRect().x,
            dot: !document.getElementById('vdot').classList.contains('hide'), sw: document.documentElement.scrollWidth })""")
        check("Deck Chat: no script errors, roster on the left", not errs and info["rx"] < info["lx"], (info, errs))
        check("Deck Chat: the VOICE tab shows a dot while someone is talking", info["dot"], info)
        voice_status(talking=False); pg.wait_for_timeout(2600)
        check("Deck Chat: the dot goes away when the channel is clear", pg.evaluate("document.getElementById('vdot').classList.contains('hide')"))
        voice_status()
        pg.click("#voicelink"); pg.wait_for_timeout(2000)
        check("Deck Chat: the VOICE tab opens the Voice page", pg.url.endswith("/voice"), pg.url)
        ctx.close()
        # Voice page
        voice_status()
        ctx, pg, errs = page(C + "/voice", 1280, 720, 2500)
        vi = pg.evaluate("""() => ({ tgs: [...document.querySelectorAll('.tg .nm')].map(e => e.textContent),
            sel: document.querySelector('.tg[aria-pressed=true] .nm').textContent,
            talk: document.querySelector('.tg.talking .st') ? document.querySelector('.tg.talking .st').textContent : '',
            off: [...document.querySelectorAll('.tg.off .st')].map(e => e.textContent),
            now: document.querySelector('.now.live .who') ? document.querySelector('.now.live .who').textContent : '',
            mesh: [...document.querySelectorAll('.box')][0].querySelectorAll('.person').length,
            mum: [...document.querySelectorAll('.box')][1].textContent,
            log: document.querySelectorAll('.lrow').length, blocked: document.querySelectorAll('.lrow.blocked').length,
            pills: [document.getElementById('pmesh').textContent, document.getElementById('pmum').textContent],
            down: getComputedStyle(document.getElementById('down')).display, sw: document.documentElement.scrollWidth,
            xss: document.querySelectorAll('.box b').length })""")
        check("Voice page: no script errors", not errs, errs)
        check("Voice page: five talk groups listed, the talking one selected", vi["tgs"] == ["TG1", "TG2", "TG3", "TG4", "TG5"] and vi["sel"] == "TG1", vi)
        check("Voice page: who is talking (list and the big banner)", "ALPHA-2" in vi["talk"] and vi["now"] == "ALPHA-2", vi)
        check("Voice page: unbridged talk groups say NOT BRIDGED", vi["off"] == ["NOT BRIDGED"] * 3, vi["off"])
        check("Voice page: who is on the mesh and on Mumble (plus this deck as the gateway)", vi["mesh"] == 3 and "ops-iphone" in vi["mum"] and "this deck" in vi["mum"], vi)
        check("Voice page: recent transmissions, blocked attempts marked", vi["log"] == 3 and vi["blocked"] == 1, vi)
        check("Voice page: header pills", vi["pills"][0].startswith("MESH VOICE") and "LINKED" in vi["pills"][1], vi["pills"])
        ctl = pg.evaluate("[document.getElementById('listen').disabled, document.getElementById('bridge').disabled, document.getElementById('bridge').getAttribute('aria-checked')]")
        check("Voice page: on the deck's screen, LISTEN and the switch work; TG1's switch shows ON", ctl == [False, False, "true"], ctl)
        pg.click("#listen"); pg.wait_for_timeout(1200)
        st_l = pg.evaluate("[document.getElementById('listen').getAttribute('aria-pressed'), document.getElementById('ctlmsg').hidden, document.getElementById('ctlmsg').textContent]")
        check("Voice page: LISTEN with no voice service says why and does not stay on", st_l[0] == "false" and not st_l[1] and "Can't listen" in st_l[2], st_l)
        check("Voice page: names are shown as text, never as HTML", vi["xss"] == 0)
        check("Voice page: fits 1280 x 720 without sideways scrolling", vi["sw"] <= 1280 and vi["down"] == "none", vi)
        pg.screenshot(path=W + "/voice.png")
        pg.click(".tg[data-tg=TG3]"); pg.wait_for_timeout(1300)
        t3 = pg.evaluate("() => ({ h: document.querySelector('.dhead h2').textContent, m: [...document.querySelectorAll('.box')][1].textContent })")
        check("Voice page: picking TG3 shows it (and stays picked across refreshes)", t3["h"] == "TG3" and "not bridged" in t3["m"], t3)
        old = time.time() - 60; os.utime(VS, (old, old)); pg.wait_for_timeout(1500)
        check("Voice page: a stopped gateway shows a warning", pg.evaluate("getComputedStyle(document.getElementById('down')).display") == "block"
              and "OFF" in pg.evaluate("document.getElementById('pmesh').textContent"))
        voice_status(); ctx.close()
        voice_status()
        ctx, pg, errs = page(C + "/voice", 390, 844, 2500)
        check("Voice page: no sideways scrolling at phone width", pg.evaluate("document.documentElement.scrollWidth") <= 390 and not errs, errs)
        pg.screenshot(path=W + "/voice_phone.png", full_page=True); ctx.close()
        ctx, pg, errs = page(C + "/", 390, 844)
        check("Deck Chat: no sideways scrolling at phone width", pg.evaluate("document.documentElement.scrollWidth") <= 390 and not errs, errs)
        ctx.close()
        br.close()

for p in procs:
    p.terminate()
for p in procs:
    try: p.wait(5)
    except Exception: p.kill()
print("%d/%d passed" % (sum(res), len(res)))
sys.exit(0 if all(res) else 1)
