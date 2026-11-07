import os as _o, sys as _s  # paths relative to this package
PKG = _o.path.dirname(_o.path.dirname(_o.path.abspath(__file__)))
_s.path[:0] = [PKG + "/vendor", PKG + "/common", PKG + "/bridge", PKG + "/chat", PKG + "/dashboard"]
"""file_relay_test.py - pictures, data packages and KML/KMZ from mesh phones reach the server; routes cross.

A fake phone (its ATAK file web server and its DM port for receipts) and a fake server file API (the
/Marti/sync/missionupload ATAK uses). Checks:
  * a picture: downloaded from the phone, SHA-256 checked, uploaded with the phone as creator, the notice goes on to
    the server with the server's link (no mesh link, no receipt request), and the phone gets a success receipt
  * the same file to several server people: one upload, one notice, a receipt for each copy
  * refused without touching the radio or the server: too big, wrong type, bad notice; the phone is told why and
    Ops' fault list has it ("files")
  * a file that doesn't match its SHA-256 and a server that refuses the upload: failed, shown, phone told
  * the phone's link names another address: the deck tries the address the notice came from
  * the DM relay and the mesh multicast path hand file notices to the file relay and never forward them raw
  * FILES = off: nothing happens; routes (b-m-r) are in the default return types
Run with python3 -S (as run_all.sh does)."""
import asyncio, configparser, hashlib, http.server, json, os, socket, sys, threading, time, warnings
from urllib.parse import urlparse, parse_qs
warnings.filterwarnings("ignore")
import tak_mesh_bridge as B

res = []
def check(n, c, x=""):
    res.append(bool(c)); print(("PASS  " if c else "FAIL  ") + n + ("" if c else "  " + str(x)[:300]))

# ---------------- fake phone: ATAK's file web server
FILES = {}                                   # "file id" -> bytes
class Phone(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        q = parse_qs(urlparse(self.path).query)
        d = FILES.get((q.get("file") or [""])[0])
        if d is None:
            self.send_response(404); self.end_headers(); return
        self.send_response(200); self.send_header("Content-Length", str(len(d))); self.end_headers(); self.wfile.write(d)
phone = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Phone); PPORT = phone.server_address[1]
threading.Thread(target=phone.serve_forever, daemon=True).start()

# ---------------- fake server file API
UPLOADS, TOOLS, WRAPPED, GETS = [], [], {}, []
SERVER = {"mode": "url"}                     # url | plain | fail | wrap | wrapbad
class Marti(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0); body = self.rfile.read(n)
        u = urlparse(self.path); q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if SERVER["mode"] == "fail":
            self.send_response(500); self.end_headers(); return
        b = self.headers.get("Content-Type", "").split("boundary=")[1].encode()
        part = body.split(b"--" + b)[1]
        head, _, data = part.partition(b"\r\n\r\n")
        UPLOADS.append({"path": u.path, "q": q, "head": head, "data": data[:-2]})
        reply = ("https://srv.example:8443/Marti/sync/content?hash=" + q.get("hash", "")) if SERVER["mode"] == "url" else "OK"
        if SERVER["mode"] in ("wrap", "wrapbad"):    # like OpenTAKServer with a bare picture: a new data package
            pkg = b"PK\x03\x04 wrapped " + data[:-2]
            h = hashlib.sha256(pkg).hexdigest()
            WRAPPED[h] = pkg if SERVER["mode"] == "wrap" else pkg + b"tampered"
            reply = "http://127.0.0.1:%d/Marti/api/sync/metadata/%s/tool" % (MPORT, h)
        self.send_response(200); self.end_headers(); self.wfile.write(reply.encode())
    def do_GET(self):
        GETS.append(self.path)
        if self.path.startswith("/webapp/"):          # a link that lands on the server's web page, not the file
            page = b"<!doctype html><html><body>OpenTAKServer</body></html>"
            self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page))); self.end_headers(); self.wfile.write(page); return
        h = self.path.split("/metadata/")[1].split("/")[0] if "/metadata/" in self.path else ""
        d = WRAPPED.get(h)
        if d is None and "/sync/content" in self.path:
            d = SERVED.get((parse_qs(urlparse(self.path).query).get("hash") or [""])[0])
        if d is None:
            self.send_response(404); self.end_headers(); return
        self.send_response(200); self.send_header("Content-Length", str(len(d))); self.end_headers(); self.wfile.write(d)
    def do_PUT(self):
        n = int(self.headers.get("Content-Length") or 0); TOOLS.append((self.path, self.rfile.read(n)))
        self.send_response(200); self.end_headers()
marti = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Marti); MPORT = marti.server_address[1]
threading.Thread(target=marti.serve_forever, daemon=True).start()

# ---------------- fake phone DM port: receipts (b-f-t-a) arrive here
ACKS = []
def dm_listener():
    s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); s.bind(("127.0.0.1", 0)); s.listen(20)
    def loop():
        while True:
            c, _ = s.accept(); buf = b""
            while True:
                d = c.recv(65536)
                if not d: break
                buf += d
            c.close(); ACKS.append(buf)
    threading.Thread(target=loop, daemon=True).start()
    return s.getsockname()[1]
DMPORT = dm_listener()

def listener():
    got = []
    s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); s.bind(("127.0.0.1", 0)); s.listen(20)
    def loop():
        while True:
            c, _ = s.accept(); buf = b""
            while True:
                d = c.recv(65536)
                if not d: break
                buf += d
            c.close(); got.append(buf)
    threading.Thread(target=loop, daemon=True).start()
    return s.getsockname()[1], got

def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p
import tempfile as _tf
TMP = _tf.mkdtemp(prefix="mmb_files_")
FPORT = free_port()
SERVED = {}                                   # server's own copies: sha256 -> bytes (/Marti/sync/content?hash=)

def cfg(**kw):
    cp = configparser.ConfigParser(interpolation=None)
    cp.read_dict({"bridge": dict({"COT_URL": "tcp://127.0.0.1:1", "MARTI_URL": "http://127.0.0.1:%d" % MPORT,
                                  "BRIDGE_UID": "DECK-files", "PYTAK_MULTICAST_LOCAL_ADDR": "127.0.0.1",
                                  "DM_RELAY": "1", "FILE_PORT": str(FPORT), "STATUS_FILE": TMP + "/st.json"}, **kw)})
    return cp["bridge"]

def notice(fid, data, name="photo.jpg", size=None, ack="ack-1", host="127.0.0.1", sha=None, uid="ANDROID-RAVEN21"):
    sha = sha or hashlib.sha256(data).hexdigest()
    return ('<event version="2.0" uid="ft-%s" type="b-f-t-r" how="h-e" time="2026-11-07T15:00:00Z" '
            'start="2026-11-07T15:00:00Z" stale="2026-11-07T15:05:00Z"><point lat="0" lon="0" hae="0" ce="9999999" '
            'le="9999999"/><detail><fileshare filename="%s" senderUrl="http://%s:%d/getfile?file=%s&amp;sender=RAVEN21" '
            'sizeInBytes="%d" sha256="%s" senderUid="%s" senderCallsign="RAVEN21" name="%s" peerHosted="true"/>'
            '<ackrequest uid="%s" ackrequested="true" tag="%s"/></detail></event>' % (
                fid, name, host, PPORT, fid, len(data) if size is None else size, sha, uid, name, ack, name)).encode()

async def drain(q, secs=3.0):
    out, end = [], time.time() + secs
    while time.time() < end:
        try:
            out.append(await asyncio.wait_for(q.get(), 0.2))
        except asyncio.TimeoutError:
            if out: break
    return out

async def settle(fr, secs=6.0):
    end = time.time() + secs
    while time.time() < end and (fr.q.qsize() or any(j["state"] in ("queued", "running") for j in fr.jobs.values())):
        await asyncio.sleep(0.1)
    await asyncio.sleep(0.6)                 # receipts go out on their own tasks

async def main():
    B.Peers.mesh_uid["ANDROID-RAVEN21"] = {"cs": "RAVEN21", "uid": "ANDROID-RAVEN21", "ip": "127.0.0.1",
                                           "port": DMPORT, "proto": "tcp", "ts": time.time()}
    q = asyncio.Queue()
    fr = B.FileRelay(cfg(), q)
    task = asyncio.create_task(fr.run()); await asyncio.sleep(0.1)

    # 1. a picture, end to end
    pic = os.urandom(300000); FILES["1"] = pic
    fr.offer(notice("1", pic), "127.0.0.1"); await settle(fr)
    out = await drain(q)
    check("picture uploaded once, bytes intact", len(UPLOADS) == 1 and UPLOADS[0]["data"] == pic, len(UPLOADS))
    up = UPLOADS[0] if UPLOADS else {"q": {}, "path": "", "head": b""}
    check("uploaded the way ATAK does: missionupload, hash, bare filename, phone as creator, field assetfile",
          up["path"] == "/Marti/sync/missionupload" and up["q"].get("hash") == hashlib.sha256(pic).hexdigest()
          and up["q"].get("filename") == "photo.jpg" and up["q"].get("creatorUid") == "ANDROID-RAVEN21"
          and b'name="assetfile"' in up["head"], up)
    check("marked private, like ATAK's sends to people", any(p.endswith("/tool") and b == b"private" for p, b in TOOLS), TOOLS)
    sv = out[0] if out else b""
    check("the notice goes on to the server with the server's link", len(out) == 1 and
          b'senderUrl="https://srv.example:8443/Marti/sync/content?hash=' in sv and b"getfile" not in sv, out)
    check("no mesh-only bits left (peerHosted, receipt request)", b"peerHosted" not in sv and b"ackrequest" not in sv, sv)
    check("still from the phone (sender, name, size, hash kept)", b'senderUid="ANDROID-RAVEN21"' in sv and
          b'name="photo.jpg"' in sv and ('sizeInBytes="%d"' % len(pic)).encode() in sv, sv)
    check("the phone gets a success receipt for its request", len(ACKS) == 1 and b'type="b-f-t-a"' in ACKS[0]
          and b'uid="ack-1"' in ACKS[0] and b'success="true"' in ACKS[0], ACKS)

    # 2. the same file to three server people: one upload, one notice, three receipts
    UPLOADS.clear(); ACKS.clear()
    p2 = os.urandom(5000); FILES["2"] = p2
    for a in ("a1", "a2", "a3"):
        fr.offer(notice("2", p2, ack=a), "127.0.0.1")
    await settle(fr); out = await drain(q)
    check("same file to 3 server people: one upload, one notice", len(UPLOADS) == 1 and len(out) == 1, (len(UPLOADS), len(out)))
    check("a receipt for each copy", sorted(x.split(b'<ackresponse uid="')[1].split(b'"')[0] for x in ACKS) == [b"a1", b"a2", b"a3"], ACKS)
    ACKS.clear(); fr.offer(notice("2", p2, ack="a4"), "127.0.0.1"); await asyncio.sleep(0.6)
    check("sent again after it's done: receipt at once, no second upload", len(UPLOADS) == 1 and len(ACKS) == 1
          and b'success="true"' in ACKS[0], (len(UPLOADS), ACKS))

    # 3. refused before anything moves
    B.Faults.areas.clear(); UPLOADS.clear(); ACKS.clear()
    fr.offer(notice("9", b"x", size=7 * 1024 * 1024, name="big.jpg", ack="big"), "127.0.0.1"); await asyncio.sleep(0.6)
    f = B.Faults.status().get("files", {})
    check("too big (7 MB, limit 5): refused, nothing downloaded or uploaded", not UPLOADS and f.get("active")
          and "too big: 7.0 MB" in f.get("last", "") and "big.jpg from RAVEN21" in f.get("last", ""), f)
    check("the phone is told why", len(ACKS) == 1 and b'success="false"' in ACKS[0] and b"too big" in ACKS[0], ACKS)
    ACKS.clear(); fr.offer(notice("8", b"MZ", name="tool.exe", ack="exe"), "127.0.0.1"); await asyncio.sleep(0.6)
    check("wrong type (.exe): refused, phone told", not UPLOADS and ACKS and b"file type not passed on" in ACKS[0], ACKS)
    ACKS.clear(); fr.offer(notice("7", b"abc", sha="nothex", ack="bad"), "127.0.0.1"); await asyncio.sleep(0.6)
    check("bad notice (SHA-256 not hex): refused", not UPLOADS and ACKS and b'success="false"' in ACKS[0], ACKS)
    check("refused count kept", fr.refused == 3, fr.refused)

    # 4. failures along the way
    ACKS.clear(); p5 = os.urandom(2000); FILES["5"] = p5
    fr.offer(notice("5", p5, sha=hashlib.sha256(b"something else").hexdigest(), ack="h"), "127.0.0.1"); await settle(fr)
    check("file doesn't match its SHA-256: not uploaded, phone told", not UPLOADS and ACKS and b"SHA-256" in ACKS[0], ACKS)
    SERVER["mode"] = "fail"; ACKS.clear(); p6 = os.urandom(2000); FILES["6"] = p6
    fr.offer(notice("6", p6, ack="s"), "127.0.0.1"); await settle(fr); out = await drain(q, 1)
    check("server refuses the upload: no notice to the server, shown in Ops, phone told", not out and ACKS
          and b"upload to the server failed" in ACKS[0] and "upload to the server failed" in B.Faults.status()["files"]["last"], (out, ACKS))
    SERVER["mode"] = "plain"; ACKS.clear(); p7 = os.urandom(1000); FILES["7b"] = p7
    fr.offer(notice("7b", p7, ack="p"), "127.0.0.1"); await settle(fr); out = await drain(q)
    check("server answers without a link: the standard content link is used",
          out and ('senderUrl="http://127.0.0.1:%d/Marti/sync/content?hash=%s"' % (MPORT, hashlib.sha256(p7).hexdigest())).encode() in out[0], out)
    SERVER["mode"] = "url"

    # 4b. the server re-packages a bare picture (OpenTAKServer does): the notice must carry that copy's hash and size
    UPLOADS.clear(); ACKS.clear(); GETS.clear(); pw = os.urandom(3000); FILES["w1"] = pw
    check("a zip kept as sent: no extra download from the server", not GETS, GETS)
    SERVER["mode"] = "wrap"
    fr.offer(notice("w1", pw, name="IMG_0042.jpg", ack="w"), "127.0.0.1"); await settle(fr); out = await drain(q)
    h = next(iter(WRAPPED)) if WRAPPED else ""
    sv = out[0] if out else b""
    check("re-packaged picture: notice has the server copy's SHA-256, size and .zip name", out and
          ('sha256="%s"' % h).encode() in sv and ('sizeInBytes="%d"' % len(WRAPPED.get(h, b""))).encode() in sv
          and b'filename="IMG_0042.zip"' in sv and ("/metadata/%s/tool" % h).encode() in sv, sv)
    check("...checked by fetching the server's copy once", len(GETS) == 1, GETS)
    check("...and the phone still gets its success receipt (for its own file)", ACKS and b'success="true"' in ACKS[0]
          and ('sha256="%s"' % hashlib.sha256(pw).hexdigest()).encode() in ACKS[0], ACKS)
    WRAPPED.clear(); SERVER["mode"] = "wrapbad"; ACKS.clear(); pb = os.urandom(1800); FILES["w2"] = pb
    fr.offer(notice("w2", pb, name="IMG_0043.png", ack="wb"), "127.0.0.1"); await settle(fr); out = await drain(q, 1)
    check("server copy doesn't match the hash it named: nothing sent, shown, phone told", not out and ACKS
          and b'success="false"' in ACKS[0] and "re-packaged" in B.Faults.status()["files"]["last"], (out, ACKS))
    SERVER["mode"] = "url"; WRAPPED.clear()

    # 5. the phone's link names an address the deck can't reach: the address it came from is tried
    UPLOADS.clear(); ACKS.clear(); p8 = os.urandom(4000); FILES["8b"] = p8
    fr.offer(notice("8b", p8, host="phone.invalid", ack="alt"), "127.0.0.1"); await settle(fr); out = await drain(q)
    check("link with an unreachable name: downloaded from the sender's address instead", len(UPLOADS) == 1 and out, ACKS)

    # 6. the paths into the file relay; never forwarded raw
    UPLOADS.clear(); p9 = os.urandom(3000); FILES["9b"] = p9
    relay = B.DMRelay(cfg(), q)
    await relay._one(notice("9b", p9, ack="dm"), ("127.0.0.1", 50000)); await settle(fr); out = await drain(q)
    check("DM relay: a file notice for a server person goes to the file relay (uploaded, link rewritten)",
          len(UPLOADS) == 1 and out and b"getfile" not in out[0], out)
    UPLOADS.clear(); p10 = os.urandom(3000); FILES["10"] = p10
    m2s = B.MeshToServer(cfg(), q)
    await m2s._forward(notice("10", p10, ack="mc", uid="ANDROID-RAVEN21"), ("127.0.0.1", 6969)); await settle(fr)
    out = await drain(q)
    check("mesh multicast: a file notice is carried, never forwarded with its mesh link",
          len(UPLOADS) == 1 and all(b"getfile" not in o for o in out) and len(out) == 1, out)

    # 6a. server people see mesh phones as reachable through the server, not at a mesh IP
    sa = (b'<event version="2.0" uid="ANDROID-EP1" type="a-f-G-U-C" how="m-g" time="2026-11-07T15:00:00Z" '
          b'start="2026-11-07T15:00:00Z" stale="2026-11-07T15:05:00Z"><point lat="38.5" lon="-104.5" hae="0" ce="9" le="9"/>'
          b'<detail><contact callsign="ECHO-1" endpoint="10.41.0.60:4242:tcp"/></detail></event>')
    q4 = asyncio.Queue(); m4 = B.MeshToServer(cfg(), q4)
    await m4._forward(sa, ("10.41.0.60", 6969)); up4 = await drain(q4, 1)
    check("mesh phone's report to the server: endpoint '*:-1:stcp' (reach me through the server), not its mesh IP",
          up4 and b'endpoint="*:-1:stcp"' in up4[0] and b"10.41.0.60" not in up4[0], up4)
    d4 = B.Peers.find_mesh(uid="ANDROID-EP1")
    check("...while the deck still knows its real mesh address (for DMs and files down)", d4 and d4["ip"] == "10.41.0.60"
          and d4["port"] == 4242, d4)
    q5 = asyncio.Queue(); m5 = B.MeshToServer(cfg(SERVER_ENDPOINT="keep"), q5)
    await m5._forward(sa.replace(b"ANDROID-EP1", b"ANDROID-EP2").replace(b"15:00:00Z", b"15:00:01Z"), ("10.41.0.60", 6969)); up5 = await drain(q5, 1)
    check("SERVER_ENDPOINT = keep: passed unchanged", up5 and b'endpoint="10.41.0.60:4242:tcp"' in up5[0], up5)

    # 6a2. a phone that moved from the mesh to the server: its server reports reach the mesh at once (not after the
    # 10 min loop guard), so mesh phones learn to reach it through the deck; the mesh's own echo still never does
    t0 = B.cot_time(sa)
    check("report time read as UTC seconds", t0 and abs(t0 - 1794150000.0) < 86400 * 400, t0)
    echo = sa.replace(b'endpoint="10.41.0.60:4242:tcp"', b'endpoint="*:-1:stcp"')
    moved = echo.replace(b'time="2026-11-07T15:00:00Z"', b'time="2026-11-07T15:02:00.500Z"')
    check("loop guard: the mesh phone's own report coming back from the server is not sent to the mesh",
          B.Shared.from_mesh(echo, b"ANDROID-EP1") is True)
    check("...but a NEWER report from it via the server (it moved to the server) goes to the mesh at once",
          B.Shared.from_mesh(moved, b"ANDROID-EP1") is False)
    SENT = []
    s2m3 = B.ServerToMesh(cfg(RETURN_PATH="1"), asyncio.Queue())
    s2m3._send = lambda d, e, u: SENT.append(u)
    st3 = asyncio.create_task(s2m3.run())
    await s2m3.rx_queue.put(echo); await s2m3.rx_queue.put(moved); await asyncio.sleep(1.5)
    check("through the real server -> mesh loop: the moved phone's new report is relayed, once", SENT == [b"ANDROID-EP1"], SENT)
    st3.cancel()

    # 6b. a real TLS server that wants the deck's certificate (as OpenTAKServer's 8443 does)
    import shutil, ssl, subprocess, tempfile
    if shutil.which("openssl"):
        W = tempfile.mkdtemp(prefix="mmb_files_tls_")
        def ossl(*a): subprocess.run(["openssl"] + list(a), cwd=W, check=True, capture_output=True)
        ossl("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", "ca.key", "-out", "ca.pem", "-days", "2", "-subj", "/CN=test-ca")
        for n in ("srv", "deck"):
            ossl("req", "-newkey", "rsa:2048", "-nodes", "-keyout", n + ".key", "-out", n + ".csr", "-subj", "/CN=" + ("127.0.0.1" if n == "srv" else "DECK"))
            open(W + "/%s.ext" % n, "w").write("subjectAltName=IP:127.0.0.1\n" if n == "srv" else "extendedKeyUsage=clientAuth\n")
            ossl("x509", "-req", "-in", n + ".csr", "-CA", "ca.pem", "-CAkey", "ca.key", "-CAcreateserial", "-out", n + ".pem", "-days", "2", "-extfile", n + ".ext")
        tls = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Marti)
        sc = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); sc.load_cert_chain(W + "/srv.pem", W + "/srv.key")
        sc.load_verify_locations(W + "/ca.pem"); sc.verify_mode = ssl.CERT_REQUIRED
        tls.socket = sc.wrap_socket(tls.socket, server_side=True)
        threading.Thread(target=tls.serve_forever, daemon=True).start()
        turl = "https://127.0.0.1:%d" % tls.server_address[1]
        UPLOADS.clear(); p11 = os.urandom(2500); FILES["11"] = p11
        q2 = asyncio.Queue(); fr2 = B.FileRelay(cfg(MARTI_URL=turl, PYTAK_TLS_CLIENT_CERT=W + "/deck.pem",
                                                   PYTAK_TLS_CLIENT_KEY=W + "/deck.key", PYTAK_TLS_CLIENT_CAFILE=W + "/ca.pem"), q2)
        t2 = asyncio.create_task(fr2.run()); await asyncio.sleep(0.1)
        fr2.offer(notice("11", p11, ack="tls"), "127.0.0.1"); await settle(fr2); out = await drain(q2)
        check("TLS file API with the deck's certificate: uploaded, notice sent", len(UPLOADS) == 1 and out, (UPLOADS, fr2.jobs))
        UPLOADS.clear(); FILES["12"] = os.urandom(1500)
        q3 = asyncio.Queue(); fr3 = B.FileRelay(cfg(MARTI_URL=turl, PYTAK_TLS_CLIENT_CAFILE=W + "/ca.pem"), q3)
        t3 = asyncio.create_task(fr3.run()); await asyncio.sleep(0.1)
        fr3.offer(notice("12", FILES["12"], ack="nocert"), "127.0.0.1"); await settle(fr3); out = await drain(q3, 1)
        check("without the deck's certificate the server refuses: failed and shown, nothing sent", not UPLOADS and not out
              and "upload to the server failed" in B.Faults.status()["files"]["last"], B.Faults.status().get("files"))
        # a phone with ATAK's secure file sharing: https on its own self-signed certificate (peerHosted)
        ossl("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", "ph.key", "-out", "ph.pem", "-days", "2", "-subj", "/CN=atak-phone")
        sphone = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Phone)
        pc = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); pc.load_cert_chain(W + "/ph.pem", W + "/ph.key")
        sphone.socket = pc.wrap_socket(sphone.socket, server_side=True)
        threading.Thread(target=sphone.serve_forever, daemon=True).start()
        SP = sphone.server_address[1]
        UPLOADS.clear(); ACKS.clear(); p13 = os.urandom(2200); FILES["13"] = p13
        n13 = notice("13", p13, ack="https").replace(b"http://127.0.0.1:%d/" % PPORT, b"https://127.0.0.1:%d/" % SP)
        fr.offer(n13, "127.0.0.1"); await settle(fr); out = await drain(q)
        check("phone serving over https on its own self-signed certificate (peerHosted): downloaded, uploaded",
              len(UPLOADS) == 1 and UPLOADS[0]["data"] == p13 and out and ACKS and b'success="true"' in ACKS[0], (ACKS, B.Faults.status().get("files")))
        UPLOADS.clear(); ACKS.clear(); FILES["14"] = os.urandom(1200)
        n14 = notice("14", FILES["14"], ack="nph").replace(b"http://127.0.0.1:%d/" % PPORT, b"https://127.0.0.1:%d/" % SP).replace(b' peerHosted="true"', b"")
        fr.offer(n14, "127.0.0.1"); await settle(fr)
        check("an https link that isn't phone-hosted is still certificate-checked (not trusted blindly)", not UPLOADS and ACKS
              and b'success="false"' in ACKS[0], ACKS)
        t2.cancel(); t3.cancel(); B.FileRelay.instance = fr
    else:
        print("SKIP  TLS checks: no openssl")

    # 8. server -> mesh: the deck fetches, caches and serves it on the mesh; the notice goes to the right phones
    import urllib.request as ur
    BPORT, BRAVO = listener()
    B.Peers.mesh_uid["ANDROID-BRAVO3"] = {"cs": "BRAVO-3", "uid": "ANDROID-BRAVO3", "ip": "127.0.0.1", "port": BPORT,
                                          "proto": "tcp", "ts": time.time()}
    B.Peers.mesh["bravo-3"] = B.Peers.mesh_uid["ANDROID-BRAVO3"]; B.Peers.mesh["raven21"] = B.Peers.mesh_uid["ANDROID-RAVEN21"]
    def down_notice(data, dest=None, name="overlay.zip", sha=None, size=None, uid="ANDROID-TOC1", host="127.0.0.1", ack="dack-1"):
        sha = sha or hashlib.sha256(data).hexdigest()
        marti = ("<marti>%s</marti>" % "".join('<dest callsign="%s"/>' % d for d in dest)) if dest else ""
        return ('<event version="2.0" uid="sft-%s" type="b-f-t-r" how="h-e" time="2026-11-07T15:00:00Z" start="2026-11-07T15:00:00Z" '
                'stale="2026-11-07T15:05:00Z"><point lat="0" lon="0" hae="0" ce="9999999" le="9999999"/><detail><fileshare filename="%s" '
                'senderUrl="http://%s:%d/Marti/sync/content?hash=%s" sizeInBytes="%d" sha256="%s" senderUid="%s" senderCallsign="TOC-1" '
                'name="%s"/><ackrequest uid="%s" ackrequested="true" tag="%s"/>%s</detail></event>' % (
                    sha[:8], name, host, MPORT, sha, len(data) if size is None else size, sha, uid, name, ack, name, marti)).encode()
    await asyncio.sleep(0.3)                  # the file server is up
    ACKS.clear(); GETS.clear()
    d1 = os.urandom(400000); h1 = hashlib.sha256(d1).hexdigest(); SERVED[h1] = d1
    fr.offer_down(down_notice(d1, dest=["RAVEN21"])); await settle(fr); await asyncio.sleep(0.5)
    n = ACKS[0] if ACKS else b""
    check("server -> mesh: the addressed phone gets the notice, the other doesn't", len(ACKS) == 1 and not BRAVO, (len(ACKS), len(BRAVO)))
    check("...with the link pointing at the deck, the server's addressing gone",
          ("senderUrl=\"http://127.0.0.1:%d/getfile?hash=%s" % (FPORT, h1)).encode() in n and b"<marti" not in n and b"/Marti/" not in n, n)
    check("...hash, size, name and receipt request kept", ('sha256="%s"' % h1).encode() in n and ('sizeInBytes="%d"' % len(d1)).encode() in n
          and b'filename="overlay.zip"' in n and b"ackrequest" in n, n)
    got = await asyncio.to_thread(lambda: ur.urlopen("http://127.0.0.1:%d/getfile?hash=%s" % (FPORT, h1), timeout=5).read())
    check("the phone can download it from the deck, bytes intact", got == d1, len(got))
    check("downloaded from the server once (deck certificate), kept in the cache folder",
          len(GETS) == 1 and os.path.isfile(os.path.join(TMP, "file_cache", h1)), (GETS, os.listdir(TMP)))
    import urllib.error
    def _nf():
        try:
            ur.urlopen("http://127.0.0.1:%d/getfile?hash=%s" % (FPORT, "0" * 64), timeout=5); return False
        except urllib.error.HTTPError as e:
            return e.code == 404
    nf = await asyncio.to_thread(_nf)
    check("the deck serves only cached files (anything else: 404)", nf)
    fr.offer_down(down_notice(d1, dest=["BRAVO-3"], ack="dack-2")); await asyncio.sleep(0.6)
    check("same file later for another phone: from the cache, no second server download", len(BRAVO) == 1 and len(GETS) == 1, (len(BRAVO), GETS))
    ACKS.clear(); BRAVO.clear()
    d2 = os.urandom(5000); SERVED[hashlib.sha256(d2).hexdigest()] = d2
    fr.offer_down(down_notice(d2, name="route_pkg.zip", ack="dack-3")); await settle(fr); await asyncio.sleep(0.5)
    check("sent to everyone on the server: every mesh phone gets the notice", len(ACKS) == 1 and len(BRAVO) == 1, (len(ACKS), len(BRAVO)))
    ACKS.clear(); BRAVO.clear()
    B.Peers.server_uid["ANDROID-BRAVO3"] = {"cs": "BRAVO-3", "uid": "ANDROID-BRAVO3", "ts": time.time() + 1}   # moved to the server
    d2b = os.urandom(4100); SERVED[hashlib.sha256(d2b).hexdigest()] = d2b
    fr.offer_down(down_notice(d2b, name="route_pkg2.zip", ack="dack-3b")); await settle(fr); await asyncio.sleep(0.5)
    check("for everyone: a phone that moved to the server isn't sent the mesh copy", len(ACKS) == 1 and not BRAVO, (len(ACKS), len(BRAVO)))
    del B.Peers.server_uid["ANDROID-BRAVO3"]
    ACKS.clear(); BRAVO.clear()
    d3 = os.urandom(3000); SERVED[hashlib.sha256(d3).hexdigest()] = d3
    fr.offer_down(down_notice(d3, dest=["RAVEN21"], host="tak.internal.invalid", ack="dack-4")); await settle(fr); await asyncio.sleep(0.5)
    check("server link names a host the deck can't resolve: the bridge's server address is used", len(ACKS) == 1, B.Faults.status().get("files_down"))
    # receipts back to the sender
    rq = asyncio.Queue(); relay2 = B.DMRelay(cfg(), rq)
    ack_ev = (b'<event version="2.0" uid="a1" type="b-f-t-a" how="m-g" time="t" start="t" stale="t"><point lat="0" lon="0" hae="0" '
              b'ce="9" le="9"/><detail><ackresponse uid="dack-1" senderUid="ANDROID-RAVEN21" success="true" tag="overlay.zip" reason="" '
              b'sha256="x" sizeInBytes="1"/></detail></event>')
    await relay2._one(ack_ev, ("127.0.0.1", 1)); a_out = await drain(rq, 1)
    check("a mesh phone's receipt goes on to the server person who sent the file", a_out and b'<dest callsign="TOC-1"/>' in a_out[0]
          and b'<dest uid="ANDROID-TOC1"/>' in a_out[0], a_out)
    await relay2._one(ack_ev.replace(b"dack-1", b"unknown-ack"), ("127.0.0.1", 1)); a_out = await drain(rq, 0.5)
    check("a receipt for nothing the deck carried is dropped", not a_out, a_out)
    # refused / failed / echo
    B.Faults.areas.clear(); ACKS.clear()
    fr.offer_down(down_notice(b"x", dest=["RAVEN21"], size=9 * 1024 * 1024, name="huge.zip")); await asyncio.sleep(0.3)
    check("too big for the mesh: refused, shown as 'File not passed to the mesh', no notice",
          not ACKS and "too big" in B.Faults.status().get("files_down", {}).get("last", ""), B.Faults.status())
    d4 = os.urandom(2000); SERVED[hashlib.sha256(d4).hexdigest()] = b"different bytes"
    fr.offer_down(down_notice(d4, dest=["RAVEN21"], ack="dack-5")); await settle(fr); await asyncio.sleep(0.3)
    check("server copy doesn't match its SHA-256: no notice to the mesh, shown", not ACKS and "SHA-256" in B.Faults.status()["files_down"]["last"])
    B.Shared.mark(B.Shared.mesh_uids, b"ANDROID-RAVEN21")
    d5 = os.urandom(1000); SERVED[hashlib.sha256(d5).hexdigest()] = d5
    fr.offer_down(down_notice(d5, uid="ANDROID-RAVEN21", ack="dack-6")); await settle(fr); await asyncio.sleep(0.3)
    check("a file a mesh phone sent (its echo from the server) never goes back to the mesh", not ACKS and not BRAVO, (ACKS, BRAVO))
    fr.offer_down(down_notice(d5, dest=["NOBODY-HERE"], uid="ANDROID-TOC1", ack="dack-7")); await asyncio.sleep(0.3)
    check("addressed only to people not on the mesh: nothing happens", not ACKS and not BRAVO)
    # through the real server -> mesh loop
    s2m_q = asyncio.Queue(); s2m2 = B.ServerToMesh(cfg(RETURN_PATH="1"), s2m_q); st2 = asyncio.create_task(s2m2.run())
    d6 = os.urandom(2500); SERVED[hashlib.sha256(d6).hexdigest()] = d6
    await s2m_q.put(down_notice(d6, dest=["BRAVO-3"], ack="dack-8")); await asyncio.sleep(0.3); await settle(fr); await asyncio.sleep(0.5)
    check("from the server stream: the notice is carried by the deck, never put on the mesh as it is",
          len(BRAVO) == 1 and b"/getfile?hash=" in BRAVO[0], BRAVO)
    st2.cancel()
    # iTAK names a data package with no extension ("server test"): passed on if it really is a zip
    B.Faults.areas.clear(); ACKS.clear(); BRAVO.clear()
    z = b"PK\x03\x04" + os.urandom(3000); SERVED[hashlib.sha256(z).hexdigest()] = z
    fr.offer_down(down_notice(z, dest=["RAVEN21"], name="server test", ack="dack-z")); await settle(fr); await asyncio.sleep(0.5)
    check("iTAK package with no extension ('server test') that is a zip: carried to the mesh", len(ACKS) == 1
          and b'filename="server test"' in ACKS[0] and not B.Faults.status().get("files_down"), (ACKS, B.Faults.status()))
    nz = b"just text, not a package"; SERVED[hashlib.sha256(nz).hexdigest()] = nz; ACKS.clear()
    fr.offer_down(down_notice(nz, dest=["RAVEN21"], name="notes", ack="dack-nz")); await settle(fr); await asyncio.sleep(0.3)
    check("no extension and not a zip: not passed to the mesh, shown", not ACKS
          and "not a data package" in B.Faults.status().get("files_down", {}).get("last", ""), B.Faults.status())
    ACKS.clear(); zm = b"PK\x03\x04" + os.urandom(2600); SERVED[hashlib.sha256(zm).hexdigest()] = zm
    fr.offer_down(down_notice(zm, dest=["RAVEN21"], name="Mom.MissionPackage.1", ack="dack-mp")); await settle(fr); await asyncio.sleep(0.5)
    check("iTAK package named 'Mom.MissionPackage.1' (a zip): carried to the mesh", len(ACKS) == 1
          and b'filename="Mom.MissionPackage.1"' in ACKS[0], (ACKS, B.Faults.status().get("files_down")))
    B.Faults.areas.clear(); ACKS.clear()
    fr.offer_down(down_notice(b"MZ\x90\x00", dest=["RAVEN21"], name="app.apk", ack="dack-apk")); await asyncio.sleep(0.3)
    check("other unknown types (.apk) still refused", not ACKS and "file type not passed on (app.apk)" in
          B.Faults.status().get("files_down", {}).get("last", ""), B.Faults.status())
    UPLOADS.clear(); ACKS.clear(); zu = b"PK\x03\x04" + os.urandom(2000); FILES["zu"] = zu
    fr.offer(notice("zu", zu, name="field pkg", ack="zu"), "127.0.0.1"); await settle(fr); await drain(q)
    check("mesh -> server, no extension but a zip: uploaded as 'field pkg.zip'", len(UPLOADS) == 1
          and UPLOADS[0]["q"].get("filename") == "field pkg.zip", UPLOADS)

    # iTAK: the notice's SHA-256 isn't the server copy's, but the link names the server copy's hash
    B.Faults.areas.clear(); ACKS.clear(); BRAVO.clear()
    zi = b"PK\x03\x04" + os.urandom(4000); hi = hashlib.sha256(zi).hexdigest(); SERVED[hi] = zi
    wrong = hashlib.sha256(b"what iTAK hashed").hexdigest()
    ni = down_notice(zi, dest=["RAVEN21"], name="server test", sha=wrong, ack="dack-i").replace(
        ("content?hash=%s" % wrong).encode(), ("content?hash=%s" % hi).encode())
    fr.offer_down(ni); await settle(fr); await asyncio.sleep(0.5)
    n = ACKS[0] if ACKS else b""
    check("iTAK notice hash differs, link names the server copy's hash: carried, notice corrected to the server copy",
          len(ACKS) == 1 and ('sha256="%s"' % hi).encode() in n and ('sizeInBytes="%d"' % len(zi)).encode() in n
          and not B.Faults.status().get("files_down"), (n, B.Faults.status()))
    got = await asyncio.to_thread(lambda: ur.urlopen("http://127.0.0.1:%d/getfile?hash=%s" % (FPORT, hi), timeout=5).read())
    check("...and the phone downloads exactly the server copy", got == zi)
    ACKS.clear(); zx = os.urandom(900); SERVED[hashlib.sha256(zx).hexdigest()] = b"neither"
    fr.offer_down(down_notice(zx, dest=["RAVEN21"], ack="dack-x")); await settle(fr); await asyncio.sleep(0.3)
    last = B.Faults.status().get("files_down", {}).get("last", "")
    check("matching neither hash: refused, and the error shows the notice, link and downloaded hashes",
          not ACKS and "notice " in last and "link " in last and "got " in last and "bytes" in last, last)

    # iTAK's link can land on a web page: the standard /Marti/sync/content?hash=<notice hash> is tried too
    B.Faults.areas.clear(); ACKS.clear(); GETS.clear()
    zw = b"PK\x03\x04" + os.urandom(1051); hw = hashlib.sha256(zw).hexdigest(); SERVED[hw] = zw
    nw = down_notice(zw, dest=["RAVEN21"], name="server test", ack="dack-w").replace(
        ("/Marti/sync/content?hash=%s" % hw).encode(), b"/webapp/datapackages")
    fr.offer_down(nw); await settle(fr); await asyncio.sleep(0.5)
    check("link that returns a web page: the deck gets the file by its hash from the standard server link instead",
          len(ACKS) == 1 and ('sha256="%s"' % hw).encode() in ACKS[0] and not B.Faults.status().get("files_down")
          and any(g.startswith("/webapp/") for g in GETS) and any(("content?hash=%s" % hw) in g for g in GETS),
          (GETS, B.Faults.status()))
    ACKS.clear(); zn = os.urandom(700)
    nn = down_notice(zn, dest=["RAVEN21"], ack="dack-n").replace(
        ("/Marti/sync/content?hash=%s" % hashlib.sha256(zn).hexdigest()).encode(), b"/webapp/x")
    fr.offer_down(nn); await settle(fr); await asyncio.sleep(0.3)
    check("web page and no copy by hash: refused and shown in Ops", not ACKS and
          B.Faults.status().get("files_down", {}).get("count", 0) >= 1, B.Faults.status())

    # cache expiry
    fr.cache_hours = 0.0001; await asyncio.sleep(0.5); fr._cache_trim()
    check("cache files go after FILE_CACHE_HOURS", not os.listdir(os.path.join(TMP, "file_cache")), os.listdir(os.path.join(TMP, "file_cache")))
    fr.cache_hours = 12

    # 7. off, routes, status, Ops label
    off = B.FileRelay(cfg(FILES="off"), asyncio.Queue())
    check("FILES = off: the relay doesn't run", off.on is False)
    B.FileRelay.instance = fr
    st = B.FileRelay.status()
    check("status for Ops: counts", st["enabled"] and st["sent"] >= 5 and st["refused"] == 3, st)
    s2m = B.ServerToMesh(cfg(RETURN_PATH="1"), asyncio.Queue())
    check("routes (b-m-r) cross to the mesh by default", b"b-m-r" in s2m.types, s2m.types)
    import dashboard_server as ds
    check("Ops labels the files faults", ds.FAULT_TEXT.get("files", ("", ""))[1] == "File not passed to the server"
          and ds.FAULT_TEXT.get("files_down", ("", ""))[1] == "File not passed to the mesh")
    src = open(PKG + "/bridge/tak_mesh_bridge.py").read()
    check("bridge status file carries the file counts", '"files": FileRelay.status()' in src)
    task.cancel()

asyncio.run(main())
print("%d/%d passed" % (sum(res), len(res)))
sys.exit(0 if all(res) else 1)
