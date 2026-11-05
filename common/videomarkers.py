"""videomarkers.py - put every video stream on the mesh map as a tap-to-play marker, at the person streaming it.

Each stream on the deck's MediaMTX (live now, or on demand: a configured source the deck pulls when someone watches)
becomes a marker "VIDEO <stream>". Tapping it in ATAK or iTAK and choosing Play Video opens
rtsp://<deck mesh address>:8554/<stream>. iTAK plays video from a marker even on a mesh without internet; a feed typed
into iTAK by hand does not play there.

Where the marker goes (C2: the map shows where the camera is):
  1. VIDEO_OWNERS in deck.ini pairs a stream with a callsign      RAVEN21: RAVEN 21, drone1: EAGLE 6
  2. a mesh phone publishing to the deck: matched by its IP address
  3. stream name = callsign, ignoring spaces, dashes and case     RAVEN21 = RAVEN 21
  The marker sits a few metres from that person and follows them (sent again when they have moved 25 m, at most
  every 15 s). Remarks give their callsign, MGRS grid and how old the position is. Owner unknown, or no position
  for them yet: the marker sits by the deck and says so.

Markers go to the mesh only (ATAK SA multicast out the mesh port, never looped back to the bridge): the link is the
deck's mesh address, which server users can't reach. Sent when the list changes, when an owner moves, and every
REFRESH seconds; each marker goes stale after STALE seconds, so a deck that is switched off leaves nothing behind.
Standard library only.
"""
import base64
import hashlib
import math
import re
import socket
import threading
import time
from urllib.parse import unquote, urlsplit
from xml.sax.saxutils import escape, quoteattr

try:
    from deckcommon import to_mgrs
except ImportError:                    # pragma: no cover
    def to_mgrs(lat, lon, digits=5):
        return None

GROUP, PORT = "239.2.3.1", 6969       # ATAK mesh SA multicast
DECK_SPACING_M = 12.0                 # by the deck: 12 m north, then 12 m apart going east
OWNER_SPACING_M = 8.0                 # by a person: 8 m north of them (not on top of their own marker)
FOLLOW_M, FOLLOW_EVERY = 25.0, 15.0   # resend a marker when its owner moved this far, at most this often
OLD_POS = 300.0                       # an owner position older than this is shown as "last known"


def _ts(x):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(x))


def norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def marker_uid(host, path, tag="VIDEO"):
    """VIDEO = the mesh's marker (link to the deck); SVIDEO = the server's (link to the server's copy)."""
    h = re.sub(r"[^A-Za-z0-9-]", "", host or "deck") or "deck"
    return "DECK-%s-%s-%s" % (h, tag, re.sub(r"[^A-Za-z0-9_.-]", "_", path))


def place(lat, lon, i, spacing=DECK_SPACING_M):
    """The i-th marker near a point: `spacing` north of it, then i * `spacing` east."""
    dlat = spacing / 111320.0
    dlon = spacing / (111320.0 * max(0.05, math.cos(math.radians(lat))))
    return lat + dlat, lon + i * dlon


def metres(a, b):
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(b[1] - a[1]) / 2) ** 2
    return 2 * 6371000.0 * math.asin(min(1.0, math.sqrt(h)))


def parse_owners(text):
    """'RAVEN21: RAVEN 21, drone1: EAGLE 6' -> {'raven21': 'RAVEN 21', 'drone1': 'EAGLE 6'}"""
    out = {}
    for part in (text or "").split(","):
        k, sep, v = part.partition(":")
        if sep and k.strip() and v.strip():
            out[k.strip().lower()] = v.strip()
    return out


def build_event(uid, path, deck_ip, lat, lon, state, now, stale, rtsp_port=8554, remarks=""):
    """One marker. state: 'live' or 'on demand'. stale <= now marks it gone (phones drop it)."""
    url = "rtsp://%s:%d/%s" % (deck_ip, rtsp_port, path)
    entry = ('<ConnectionEntry networkTimeout="12000" uid=%s path=%s protocol="rtsp" bufferTime="-1" address=%s port="%d" '
             'roverPort="-1" rtspReliable="1" ignoreEmbeddedKLV="false" alias=%s/>'
             % (quoteattr(uid), quoteattr("/" + path), quoteattr(deck_ip), rtsp_port, quoteattr("VIDEO " + path)))
    say = "Live now" if state == "live" else "On demand: plays when its source is streaming"
    rem = " | ".join(x for x in (remarks, say, "tap Play Video", url) if x)
    return ('<event version="2.0" uid=%s type="a-f-G-E-S" how="m-g" time="%s" start="%s" stale="%s">'
            '<point lat="%.7f" lon="%.7f" hae="9999999.0" ce="9999999.0" le="9999999.0"/>'
            '<detail><contact callsign=%s/><__video uid=%s url=%s>%s</__video><remarks>%s</remarks></detail></event>'
            % (quoteattr(uid), _ts(now), _ts(now), _ts(stale), lat, lon, quoteattr("VIDEO " + path), quoteattr(uid),
               quoteattr(url), entry, escape(rem))).encode()


def on_demand_sources(conf):
    """{name: source URL} of the configured on-demand RTSP sources (the deck pulls them when someone watches)."""
    out = {}
    for name, c in (conf or {}).items():
        if name.startswith("~") or name in ("all_others", "all"):
            continue
        src = str(c.get("source") or "")
        if c.get("sourceOnDemand") and src.lower().startswith(("rtsp://", "rtsps://")):
            out[name] = src
    return out


def streams(paths, conf, upstream=None):
    """[(name, state)] to show: paths live now, plus on-demand sources whose source is streaming right now
    (upstream: {name: True/False/None} from UpstreamWatch; only True counts, so nobody sees a marker for a camera
    that isn't on). paths: the dashboard's MediaMTX path list. conf: the config API's paths."""
    out = {}
    for p in paths:
        if p.get("ready") and p.get("name"):
            out[p["name"]] = "live"
    for name in on_demand_sources(conf):
        if name not in out and (upstream or {}).get(name) is True:
            out[name] = "on demand"
    return sorted(out.items())


def rtsp_live(url, timeout=4.0):
    """Is anything streaming at this RTSP URL right now? One DESCRIBE (no video is pulled).
    True = 200 OK, False = the server answered no (404 and the like), None = no answer (server unreachable).
    Uses the user:password in the URL (Basic or Digest) and never logs it."""
    u = urlsplit(url)
    if u.scheme.lower() != "rtsp" or not u.hostname:
        return None
    user, pw = unquote(u.username or ""), unquote(u.password or "")
    host, port = u.hostname, u.port or 554
    uri = "rtsp://%s:%d%s" % (host if ":" not in host else "[%s]" % host, port, u.path or "/")
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.settimeout(timeout)
            f = s.makefile("rb")

            def ask(cseq, auth=""):
                s.sendall(("DESCRIBE %s RTSP/1.0\r\nCSeq: %d\r\nAccept: application/sdp\r\nUser-Agent: TAK MESH TOC\r\n%s\r\n"
                           % (uri, cseq, auth)).encode())
                status = f.readline(512).decode("latin-1")
                headers, n = {}, 0
                while n < 64:
                    line = f.readline(2048).decode("latin-1").rstrip("\r\n")
                    n += 1
                    if not line:
                        break
                    k, _, v = line.partition(":")
                    headers.setdefault(k.strip().lower(), []).append(v.strip())
                length = int((headers.get("content-length") or ["0"])[0] or 0)
                if 0 < length <= 65536:
                    f.read(length)
                parts = status.split()
                return (int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0), headers

            code, h = ask(1)
            if code == 401 and user:
                chal = " ".join(h.get("www-authenticate", []))
                dig = next((c for c in h.get("www-authenticate", []) if c.lower().startswith("digest")), None)
                if dig:
                    kv = dict(re.findall(r'(\w+)="?([^",]*)"?', dig[6:]))
                    ha1 = hashlib.md5(("%s:%s:%s" % (user, kv.get("realm", ""), pw)).encode()).hexdigest()
                    ha2 = hashlib.md5(("DESCRIBE:%s" % uri).encode()).hexdigest()
                    resp = hashlib.md5(("%s:%s:%s" % (ha1, kv.get("nonce", ""), ha2)).encode()).hexdigest()
                    auth = ('Authorization: Digest username="%s", realm="%s", nonce="%s", uri="%s", response="%s"\r\n'
                            % (user, kv.get("realm", ""), kv.get("nonce", ""), uri, resp))
                elif "basic" in chal.lower():
                    auth = "Authorization: Basic %s\r\n" % base64.b64encode(("%s:%s" % (user, pw)).encode()).decode()
                else:
                    return False
                code, _ = ask(2, auth)
            return code == 200
    except (OSError, ValueError):
        return None


class UpstreamWatch:
    """Background thread: every `every` seconds, ask each on-demand source whether it is streaming (rtsp_live).
    get() never blocks. Probes go to the source server (over Tailscale), never onto the mesh."""

    def __init__(self, every=20.0, probe=rtsp_live, log=None):
        self.every, self.probe = float(every), probe
        self.log = log or (lambda level, text: None)
        self.error = None                 # the check's own failure, said once (not every 20 s)
        self.sources, self.state = {}, {}
        self.lock = threading.Lock()
        self.thread = None

    def set_sources(self, sources):
        with self.lock:
            self.sources = dict(sources)
            for k in [k for k in self.state if k not in self.sources]:
                del self.state[k]
        if self.thread is None and sources:
            self.thread = threading.Thread(target=self._run, name="upstream", daemon=True)
            self.thread.start()

    def poll(self):
        with self.lock:
            todo = dict(self.sources)
        for name, url in todo.items():
            ok = self.probe(url)
            with self.lock:
                if name in self.sources:
                    self.state[name] = ok

    def _run(self):
        while True:
            try:
                self.poll()
                if self.error:
                    self.log("ok", "Video source check working again")
                    self.error = None
            except Exception as exc:     # never lose the thread, but say why server-stream markers stopped
                err = ("%s: %s" % (type(exc).__name__, exc))[:160]
                if err != self.error:
                    self.log("warn", "Video source check failed (%s): server-stream markers can't appear until it works" % err)
                self.error = err
            time.sleep(self.every)

    def get(self):
        with self.lock:
            return dict(self.state)


def find_owner(name, people, owners, publisher_ip=None):
    """The person streaming `name`, or (None, why). people: the bridge's [{"cs","uid","lat","lon","ts","ip","via"}]."""
    by_cs = {}
    for p in people or []:
        k = norm(p.get("cs"))
        if k and (k not in by_cs or p.get("ts", 0) > by_cs[k].get("ts", 0)):
            by_cs[k] = p
    want = (owners or {}).get(name.lower())
    if want:
        p = by_cs.get(norm(want))
        return (p, "paired in deck.ini") if p else (None, "%s (paired in deck.ini) has no position yet" % want)
    if publisher_ip:
        for p in sorted(people or [], key=lambda p: -p.get("ts", 0)):
            if p.get("ip") == publisher_ip:
                return p, "streaming from %s" % publisher_ip
    p = by_cs.get(norm(name))
    if p:
        return p, "stream name matches the callsign"
    return None, "streamer unknown: shown at the deck"


def plan(items, people, owners, publisher_ips, deck_pos, now, mgrs_digits=5):
    """Where each marker goes: [{"name","state","lat","lon","remarks","owner"}]. deck_pos may be None."""
    out, per_owner, at_deck = [], {}, 0
    for name, state in items:
        p, why = find_owner(name, people, owners, (publisher_ips or {}).get(name))
        if p:
            i = per_owner.get(p["uid"], 0); per_owner[p["uid"]] = i + 1
            lat, lon = place(p["lat"], p["lon"], i, OWNER_SPACING_M)
            grid = to_mgrs(p["lat"], p["lon"], mgrs_digits) or "%.5f %.5f" % (p["lat"], p["lon"])
            age = now - p.get("ts", now)
            when = "position now" if age < OLD_POS else "last known position %s" % time.strftime("%H:%MZ", time.gmtime(p["ts"]))
            out.append({"name": name, "state": state, "lat": lat, "lon": lon, "owner": p["cs"],
                        "remarks": "%s at %s (%s)" % (p["cs"], grid, when)})
        elif deck_pos:
            lat, lon = place(deck_pos[0], deck_pos[1], at_deck, DECK_SPACING_M); at_deck += 1
            out.append({"name": name, "state": state, "lat": lat, "lon": lon, "owner": None, "remarks": why})
    return out


class VideoMarkers:
    def __init__(self, host=None, refresh=180.0, stale=600.0, send=None, tag="VIDEO"):
        self.host = host or socket.gethostname()
        self.tag = tag
        self.refresh, self.stale = float(refresh), float(stale)
        self.send = send or self._send
        self.sig = None              # (deck_ip, ((name, state, owner), ...)) last sent
        self.last = 0.0              # last full send
        self.repeat = 0              # after a change, send once more on the next pass (multicast can drop one)
        self.shown = {}              # name -> state currently on phones
        self.sent_at = {}            # name -> (time, lat, lon) of its last send
        self.status = {"state": "starting", "count": 0, "last_sent": None, "located": 0}

    @staticmethod
    def _send(deck_ip, data):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(deck_ip))   # out the mesh port
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 0)   # not to the bridge: never reaches the server
            s.sendto(data, (GROUP, PORT))
        finally:
            s.close()

    def tick(self, now, markers, deck_ip, deck_pos=None, rtsp_port=8554):
        """markers: plan(...). deck_ip: the address in the markers' link (the deck's mesh address; for server-side
        markers, the server). Returns the changes as [(name, 'added'|'removed', state)]."""
        if not deck_ip:
            self.status = dict(self.status, state="no mesh address", count=0)
            return []
        names = {m["name"]: m["state"] for m in markers}
        if not markers and not self.shown:
            self.status = dict(self.status, state="on" if deck_pos else "waiting for a GPS position", count=0, located=0)
            return []
        sig = (deck_ip, tuple((m["name"], m["state"], m["owner"]) for m in markers))
        full = sig != self.sig or now - self.last >= self.refresh or self.repeat > 0
        moved = [m for m in markers if m["name"] in self.sent_at and now - self.sent_at[m["name"]][0] >= FOLLOW_EVERY
                 and metres(self.sent_at[m["name"]][1:], (m["lat"], m["lon"])) >= FOLLOW_M]
        if not full and not moved:
            return []
        changes = [(n, "added", s) for n, s in names.items() if self.shown.get(n) != s]
        gone = [n for n in self.shown if n not in names]
        changes += [(n, "removed", self.shown[n]) for n in gone]
        try:
            if full:
                for n in gone:                               # tell phones it's gone: stale now
                    lat, lon = (self.sent_at.get(n) or (0, 0, 0))[1:]
                    self.send(deck_ip, build_event(marker_uid(self.host, n, self.tag), n, deck_ip, lat, lon, "on demand", now, now, rtsp_port))
                    self.sent_at.pop(n, None)
            for m in (markers if full else moved):
                self.send(deck_ip, build_event(marker_uid(self.host, m["name"], self.tag), m["name"], deck_ip, m["lat"], m["lon"],
                                               m["state"], now, now + self.stale, rtsp_port, m["remarks"]))
                self.sent_at[m["name"]] = (now, m["lat"], m["lon"])
        except OSError as exc:
            self.status = dict(self.status, state="send failed: %s" % (exc.strerror or exc))
            return []
        if full:
            self.repeat = 1 if sig != self.sig else max(0, self.repeat - 1)
            self.sig, self.last = sig, now
            self.shown = dict(names)
        self.status = {"state": "on", "count": len(markers), "last_sent": now,
                       "located": sum(1 for m in markers if m["owner"])}
        return changes if full else []
