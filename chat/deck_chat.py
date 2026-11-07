#!/usr/bin/env python3
"""deck_chat.py - TAK MESH TOC Chat: ATAK chat (GeoChat) and DMs on the deck's own screen, mesh and server.

  send     GeoChat to the mesh (224.10.10.1:17012 on eth0). The bridge hears it too and forwards it to the server.
           With the mesh down it waits in an outbox; anything older than OUTBOX_HOLD_SECONDS is held for you,
           and anything still not sent after OUTBOX_EXPIRE_HOURS expires (kept in the log, never sent).
  receive  straight from the mesh, plus a copy from the bridge (127.0.0.1:17013) for replies from the server.
           Both are de-duplicated by message id.
  on net   callsigns heard in positions or chat in the last ROSTER_MINUTES.
  DMs      the deck's own position report advertises <eth0 ip>:4242:tcp, so mesh phones can DM it.
           To mesh phones: TCP to their advertised endpoint. To server users: via the bridge (LOCAL_SEND).
           Phone-to-phone DMs across mesh and server are relayed by the bridge, not by Chat.
  GPS      with a gpsd fix, the deck's position report and chats carry the live position.

Page: http://127.0.0.1:8090 (loopback only by default). Standard library plus the bundled takproto.
    python3 deck_chat.py --config ~/manet_mesh_bridge/deck.ini
    python3 deck_chat.py --send "radio check"      (one message, then exit)
"""
import argparse
import configparser
import fcntl
import json
import logging
import os
import re
import socket
import struct
import sys
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote
from xml.sax.saxutils import escape, quoteattr

# The bundled takproto (vendor/, the same copy the bridge uses) decodes TAK Protocol mesh packets, so the On net
# list also shows phones that send protobuf. Put first, so a different copy on this computer is never used instead.
_VENDOR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "vendor")
if os.path.isdir(_VENDOR) and _VENDOR not in sys.path:
    sys.path.insert(0, _VENDOR)
TAKPROTO_ERROR = ""
try:
    import takproto
except Exception as _exc:       # not just ImportError: a different protobuf on this computer breaks it with AttributeError
    takproto = None
    TAKPROTO_ERROR = "%s: %s" % (type(_exc).__name__, _exc)

VERSION = "1.5"                 # Chat itself; the suite version is suite_version()
HERE = os.path.dirname(os.path.abspath(__file__))
log = logging.getLogger("deck_chat")

DEFAULTS = {
    "CALLSIGN": "DECK",
    "UID": "",                              # blank = DECK-<hostname>, stays the same across restarts
    "MESH_LOCAL_ADDR": "auto",             # deck IP on the mesh side (eth0); auto = read eth0
    "MESH_IFACE": "eth0",
    "CHAT_GROUP": "224.10.10.1:17012",
    "SA_GROUP": "239.2.3.1:6969",          # position reports, for the roster ("off" to disable)
    "TTL": "1",
    "BRIDGE_COPY_LISTEN": "127.0.0.1:17013",
    "BRIDGE_STATUS_FILE": "~/bridge_status.json",
    "LISTEN": "127.0.0.1:8090",
    "HISTORY_FILE": "~/deck_chat_log.jsonl",
    "HISTORY_KEEP": "300",
    "MAX_SENDS_PER_10S": "6",
    "OUTBOX_HOLD_SECONDS": "600",          # queued longer than this = held until you confirm
    "OUTBOX_EXPIRE_HOURS": "12",           # still not sent after this = expired: never sent, kept in the log (0 = never)
    "ROSTER_MINUTES": "10",
    "SA_SEND": "1",                         # announce the deck on the mesh so phones can DM it
    "SA_INTERVAL": "60",
    "DM_PORT": "4242",                      # where mesh phones deliver direct messages (TCP; iTAK: UDP)
    "LOCAL_SEND": "127.0.0.1:17014",        # bridge port for DMs to people on the server
    "LAT": "0",
    "LON": "0",
    "GPS": "gpsd",                          # gpsd = live position from the GPS puck; off = use LAT/LON
    "GPS_HOST": "127.0.0.1:2947",
    "VOICE_STATUS_FILE": "~/voice_status.json",   # written by manet-voice, shown on the Voice page
    "VOICE_CONTROL": "127.0.0.1:17016",           # manet-voice's local control port: listen on deck, bridge on/off
    "SA_GPS_INTERVAL": "15",                # position reports this often while there is a GPS fix
    "SA_TYPE": "a-f-G-U-C",                 # the deck's map symbol (CoT type); keep it the same as BRIDGE_TYPE
    "SA_TEAM": "",                          # ATAK team color (e.g. Cyan); with SA_ROLE = team icon instead of a military symbol
    "SA_ROLE": "",                          # ATAK role, e.g. HQ; keep both the same as BRIDGE_TEAM / BRIDGE_ROLE
    "QUICK": "Radio check|Loud and clear|Roger|Copy|Wilco|Standby|Moving|Set|RTB",
}

CHAT_TYPE = "b-t-f"
ROOM_ALL = "All Chat Rooms"
MESH_HDR = b"\xbf\x01\xbf"
MAX_TEXT = 1000


# ---------------------------------------------------------------- helpers
def iso(t):
    dt = datetime.fromtimestamp(t, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (dt.microsecond // 1000)


def dtg(t):
    """Military date-time group in Zulu, e.g. 190315Z OCT 26."""
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%d%H%MZ %b %y").upper()


def parse_iso(s):
    try:
        s = s.strip().replace("Z", "+00:00")
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return None


def iface_ipv4(iface):
    """IPv4 address of an interface, or None (down, unplugged or no address). No external tools."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        res = fcntl.ioctl(s.fileno(), 0x8915, struct.pack("256s", iface[:15].encode()))  # SIOCGIFADDR
        return socket.inet_ntoa(res[20:24])
    except OSError:
        return None
    finally:
        s.close()


def hostport(s, default_host="127.0.0.1"):
    host, _, port = s.rpartition(":")
    return (host or default_host, int(port))


def proto_to_xml(data):
    """TAK protobuf mesh packet -> CoT XML bytes (same conversion the bridge does)."""
    if takproto is None or data[:3] != MESH_HDR:
        return None
    msg = takproto.parse_mesh(bytearray(data))
    if not msg.HasField("cotEvent"):
        return None
    e = msg.cotEvent
    d = e.detail
    parts = []
    if d.HasField("contact"):
        a = ""
        if d.contact.endpoint:
            a += " endpoint=%s" % quoteattr(d.contact.endpoint)
        if d.contact.callsign:
            a += " callsign=%s" % quoteattr(d.contact.callsign)
        parts.append("<contact%s/>" % a)
    if d.xmlDetail:
        parts.append(d.xmlDetail)
    xml = ('<event version="2.0" uid=%s type=%s time=%s start=%s stale=%s how=%s>'
           '<point lat="%r" lon="%r" hae="%r" ce="%r" le="%r"/><detail>%s</detail></event>') % (
        quoteattr(e.uid), quoteattr(e.type), quoteattr(iso(e.sendTime / 1000.0)),
        quoteattr(iso(e.startTime / 1000.0)), quoteattr(iso(e.staleTime / 1000.0)), quoteattr(e.how),
        e.lat, e.lon, e.hae, e.ce, e.le, "".join(parts))
    return xml.encode()


def to_xml(data):
    if data[:1] == b"\xbf":
        try:
            return proto_to_xml(data)
        except Exception:
            return None
    return data


def parse_chat(data):
    """Return a message dict for a GeoChat CoT event, else None."""
    data = to_xml(data)
    if not data or b"<event" not in data or b'type="' + CHAT_TYPE.encode() not in data:
        return None
    try:
        start = data.index(b"<event")
        end = data.rindex(b"</event>") + len(b"</event>")
        ev = ET.fromstring(data[start:end])
    except Exception:
        return None
    if not ev.get("type", "").startswith(CHAT_TYPE):
        return None
    det = ev.find("detail")
    if det is None:
        return None
    chat = det.find("__chat")
    rem = det.find("remarks")
    link = det.find("link")
    text = ((rem.text or "") if rem is not None else "").strip()
    if not text:
        return None
    callsign = room = room_id = sender_uid = msg_id = to_uid = ""
    if chat is not None:
        callsign = chat.get("senderCallsign", "")
        room = chat.get("chatroom", "")
        room_id = chat.get("id", "")
        msg_id = chat.get("messageId", "")
        grp = chat.find("chatgrp")
        if grp is not None:
            sender_uid = grp.get("uid0", "")
            to_uid = grp.get("uid1", "")
    if not sender_uid and link is not None:
        sender_uid = link.get("uid", "")
    uid = ev.get("uid", "")
    if not msg_id:
        msg_id = uid or ("h%x" % (hash(data) & 0xffffffff))
    if not callsign:
        if sender_uid:
            callsign = sender_uid
        elif uid.count(".") >= 2:
            callsign = uid.split(".")[1]
        else:
            callsign = "?"
    ts = None
    if rem is not None and rem.get("time"):
        ts = parse_iso(rem.get("time"))
    ts = ts or parse_iso(ev.get("time", "")) or time.time()
    return {"id": msg_id, "uid": uid, "from": callsign[:40], "from_uid": sender_uid,
            "room": room or ROOM_ALL, "room_id": room_id or room or ROOM_ALL,
            "text": text[:MAX_TEXT], "ts": ts, "to_uid": to_uid}


SA_UID = re.compile(rb'<event[^<>]*?\suid="([^"]+)"')
SA_TYPE = re.compile(rb'<event[^<>]*?\stype="([^"]+)"')
SA_CALL = re.compile(rb'<contact[^<>]*?\scallsign="([^"]+)"')
SA_EP = re.compile(rb'<contact[^<>]*?\sendpoint="([^"]+)"')


def parse_sa(data):
    """(uid, callsign, type, endpoint) from a position report, or None."""
    data = to_xml(data)
    if not data:
        return None
    mt, mu, mc = SA_TYPE.search(data), SA_UID.search(data), SA_CALL.search(data)
    if not (mt and mu and mc) or not mt.group(1).startswith(b"a-"):
        return None
    dec = lambda b: b.decode("utf-8", "replace")
    me = SA_EP.search(data)
    return dec(mu.group(1)), dec(mc.group(1))[:40], dec(mt.group(1)), dec(me.group(1)) if me else ""


def build_geochat(callsign, sender_uid, text, lat=0.0, lon=0.0, now=None, msg_id=None,
                  to_uid=None, to_callsign=None, via_server=False):
    """GeoChat CoT XML in the layout ATAK itself sends: 'All Chat Rooms', or a direct message
    when to_uid/to_callsign are given (chatroom = their callsign, id/uid1 = their UID). via_server
    adds <marti><dest callsign=...> so the TAK server delivers it to that one person."""
    now = now or time.time()
    msg_id = msg_id or str(uuid.uuid4())
    room = to_callsign or ROOM_ALL
    room_id = to_uid or ROOM_ALL
    uid = "GeoChat.%s.%s.%s" % (sender_uid, room_id, msg_id)
    marti = '<marti><dest callsign=%s/></marti>' % quoteattr(to_callsign) if (via_server and to_callsign) else ""
    t, stale = iso(now), iso(now + 86400)
    xml = (
        '<event version="2.0" uid=%s type="b-t-f" how="h-g-i-g-o" time=%s start=%s stale=%s>'
        '<point lat="%s" lon="%s" hae="9999999.0" ce="9999999.0" le="9999999.0"/>'
        '<detail>'
        '<__chat parent="RootContactGroup" groupOwner="false" messageId=%s chatroom=%s id=%s senderCallsign=%s>'
        '<chatgrp uid0=%s uid1=%s id=%s/>'
        '</__chat>'
        '<link uid=%s type="a-f-G-U-C" relation="p-p"/>'
        '<remarks source=%s to=%s time=%s>%s</remarks>%s'
        '</detail></event>'
    ) % (quoteattr(uid), quoteattr(t), quoteattr(t), quoteattr(stale),
         float(lat), float(lon),
         quoteattr(msg_id), quoteattr(room), quoteattr(room_id), quoteattr(callsign),
         quoteattr(sender_uid), quoteattr(room_id), quoteattr(room_id),
         quoteattr(sender_uid),
         quoteattr("BAO.F.ATAK." + sender_uid), quoteattr(room_id), quoteattr(t), escape(text), marti)
    return msg_id, xml.encode()


# ---- shared code: GPS/MGRS, team icon, the [deck] config section, suite version -> ../common/deckcommon.py
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "common"))
from deckcommon import (GpsWatch, to_mgrs, latlon_to_utm, TEAMS, ROLES, team_xml,  # noqa: E402,F401
                        apply_deck, suite_version, font_file)



def build_sa(callsign, uid, endpoint, lat=0.0, lon=0.0, interval=60.0, now=None, pos=None, live=False, etype="a-f-G-U-C", team=""):
    """The deck's own position report on the mesh: makes DECK a contact phones can DM.
    With no position set (0,0) it is sent already stale, so phones list the contact without
    drawing a marker at 0,0."""
    now = now or time.time()
    lat, lon = float(lat), float(lon)
    stale = now if (lat == 0.0 and lon == 0.0) else now + 3 * float(interval)
    how, hae, ce = "h-e", 9999999.0, 9999999.0
    if pos:                                       # GPS: live, or the last known spot sent already stale
        lat, lon, how = pos["lat"], pos["lon"], "m-g"
        hae = pos["alt"] if pos.get("alt") is not None else 9999999.0
        ce = pos["eph"] if pos.get("eph") is not None else 9999999.0
        stale = now + 3 * float(interval) if live else now
    xml = ('<event version="2.0" uid=%s type=%s how=%s time=%s start=%s stale=%s>'
           '<point lat="%s" lon="%s" hae="%s" ce="%s" le="9999999.0"/>'
           '<detail><contact callsign=%s endpoint=%s/>%s'
           '<takv device="deck" platform="TAK MESH TOC Chat" os="Linux" version=%s/></detail></event>') % (
        quoteattr(uid), quoteattr(etype or "a-f-G-U-C"), quoteattr(how), quoteattr(iso(now)), quoteattr(iso(now)), quoteattr(iso(stale)), lat, lon,
        hae, ce, quoteattr(callsign), quoteattr(endpoint), team, quoteattr(VERSION))
    return xml.encode()


# ---------------------------------------------------------------- core
class Chat:
    def __init__(self, cfg):
        self.cfg = cfg
        self.callsign = cfg["CALLSIGN"].strip()[:40] or "DECK"
        self.uid = cfg["UID"].strip() or "DECK-" + re.sub(r"[^A-Za-z0-9-]", "", socket.gethostname() or "deck")
        self.group = hostport(cfg["CHAT_GROUP"])
        self.ttl = int(cfg["TTL"])
        self.hold_after = float(cfg["OUTBOX_HOLD_SECONDS"])
        self.expire_after = max(0.0, float(cfg["OUTBOX_EXPIRE_HOURS"])) * 3600
        self.lock = threading.RLock()
        self.msgs = deque(maxlen=int(cfg["HISTORY_KEEP"]))
        self.by_id = {}
        self.outbox = []                 # message dicts waiting to go out (oldest first)
        self.outbox_error = ""           # why the sender itself failed (shown as OUTBOX ... STUCK), "" = fine
        self.bad_packets, self.last_bad = 0, ""   # packets a listener could not handle
        self.roster = {}                 # uid -> {"callsign", "last", "src"}
        self.seq = 0
        self.sends = deque()
        self.mesh_direct = None
        self.mesh_error = ""
        self.sa_listening = None
        self.copy_ok = None
        self.last_copy_rx = 0.0
        self.gps = None
        if cfg["GPS"].strip().lower() == "gpsd":
            h, _, p = cfg["GPS_HOST"].partition(":")
            self.gps = GpsWatch(h or "127.0.0.1", p or 2947)
        self.hist_path = os.path.expanduser(cfg["HISTORY_FILE"]) if cfg["HISTORY_FILE"].strip().lower() not in ("", "off") else None
        self._load_history()

    def local_addr(self):
        v = self.cfg["MESH_LOCAL_ADDR"].strip()
        if v and v.lower() != "auto":
            # a configured address counts only while it is really on this deck (node plugged in);
            # otherwise Chat is in server-only mode
            return v if addr_present(v) else None
        return iface_ipv4(self.cfg["MESH_IFACE"].strip() or "eth0")

    # -- history
    def _load_history(self):
        if not self.hist_path or not os.path.exists(self.hist_path):
            return
        try:
            with open(self.hist_path) as f:
                lines = f.readlines()[-4 * self.msgs.maxlen:]
        except OSError as exc:
            log.warning("Could not read history %s: %s", self.hist_path, exc)
            return
        for ln in lines:
            try:
                m = json.loads(ln)
            except ValueError:
                continue
            old = self.by_id.get(m.get("id"))
            if old is not None:          # a later line for the same message = status update
                relayed = bool(old.get("relayed") or m.get("relayed"))
                via = sorted(set(old.get("via", [])) | set(m.get("via", [])))
                old.update(m)
                old["relayed"], old["via"] = relayed, via
                continue
            self.seq += 1
            m["seq"] = self.seq
            self.msgs.append(m)
            self.by_id[m["id"]] = m
        for m in self.msgs:
            if m.get("mine") and m.get("status") in ("queued", "held"):
                m["status"] = "held"     # survived a restart: ask before sending it late
                m["confirmed"] = False
                self.outbox.append(m)
        log.info("Loaded %d messages from %s (%d waiting in the outbox)", len(self.msgs), self.hist_path, len(self.outbox))

    def _save(self, m):
        if not self.hist_path:
            return
        try:
            with open(self.hist_path, "a") as f:
                f.write(json.dumps({k: v for k, v in m.items() if k != "seq"}) + "\n")
            if os.path.getsize(self.hist_path) > 4000 * self.msgs.maxlen:
                with self.lock:
                    keep = [{k: v for k, v in x.items() if k != "seq"} for x in self.msgs]
                tmp = self.hist_path + ".tmp"
                with open(tmp, "w") as f:
                    for x in keep:
                        f.write(json.dumps(x) + "\n")
                os.replace(tmp, self.hist_path)
        except OSError as exc:
            log.warning("Could not write history: %s", exc)

    def _touch(self, m):
        """Mark a message changed so the page picks it up on the next poll. Call with the lock held."""
        self.seq += 1
        m["seq"] = self.seq

    # -- roster
    def heard(self, uid, callsign, src, endpoint="", from_ip=None):
        """Roster entry. src = 'mesh' | 'server'. A mesh endpoint like 10.41.x.y:4242:tcp means
        we can DM them directly; anyone seen via the server can be DMed through the bridge."""
        if not uid or uid == self.uid or not callsign:
            return
        now = time.time()
        local = self.local_addr()
        if src == "mesh" and local and endpoint.startswith(local + ":"):
            src, endpoint = "server", ""         # a server person the bridge pointed at this deck (DM relay)
        with self.lock:
            e = self.roster.get(uid, {})
            if src == "mesh" and from_ip and from_ip == local and e.get("server_ts"):
                src, endpoint = "server", ""     # our own bridge re-broadcast a server person's report
            e.update({"callsign": callsign, "last": now, "src": src})
            if src == "mesh" and endpoint and re.match(r"^\d+\.\d+\.\d+\.\d+:\d+:(tcp|udp)$", endpoint):
                e["mesh_ep"], e["mesh_ts"] = endpoint, now
            if src == "server":
                e["server_ts"] = now
            self.roster[uid] = e
            if len(self.roster) > 500:
                cutoff = time.time() - 3600
                self.roster = {k: v for k, v in self.roster.items() if v["last"] > cutoff}
                if len(self.roster) > 2000:          # a flood of made-up UIDs: keep the most recently heard
                    keep = sorted(self.roster.items(), key=lambda kv: kv[1]["last"])[-1800:]
                    self.roster = dict(keep)

    def roster_list(self):
        horizon = time.time() - 60 * float(self.cfg["ROSTER_MINUTES"])
        with self.lock:
            rows = [dict(v, uid=k) for k, v in self.roster.items() if v["last"] >= horizon]
        best = {}
        for r in sorted(rows, key=lambda r: r["last"]):   # one row per callsign, newest wins
            r["path"] = self._path(r, horizon)
            best[r["callsign"]] = r
        return sorted(best.values(), key=lambda r: -r["last"])

    @staticmethod
    def _path(e, horizon):
        """How a DM to this contact would travel: 'mesh' (direct), 'server' (via bridge) or None.
        Where they were heard last wins: a phone that moved from the mesh to the server (or back) switches at its
        next position report, not after ROSTER_MINUTES."""
        mesh_ts, srv_ts = e.get("mesh_ts", 0), e.get("server_ts", 0)
        mesh_ok = bool(e.get("mesh_ep")) and mesh_ts >= horizon
        if mesh_ok and mesh_ts >= srv_ts:
            return "mesh"
        if srv_ts:
            return "server"
        return "mesh" if mesh_ok else None

    def contact(self, who):
        """Find a roster entry by UID or callsign (case-insensitive)."""
        with self.lock:
            if who in self.roster:
                return dict(self.roster[who], uid=who)
            hits = [dict(v, uid=k) for k, v in self.roster.items() if v["callsign"].lower() == str(who).lower()]
        return max(hits, key=lambda r: r["last"]) if hits else None

    # -- receive
    def add(self, m, src):
        """Record a received message. src = 'mesh' | 'mesh-bridge' | 'server'."""
        save = None
        with self.lock:
            old = self.by_id.get(m["id"])
            if old is not None:
                if old.get("mine") and src == "mesh":
                    return False                 # our own multicast looping back on this deck
                changed = False
                if old.get("mine") and src == "mesh-bridge" and not old.get("relayed"):
                    old["relayed"] = True        # the bridge heard our message and forwarded it
                    changed = True
                    save = dict(old)
                if not old.get("mine") and src not in old.get("via", []):
                    old.setdefault("via", []).append(src)
                    if src == "server":          # came down from OTS = it started off the mesh
                        old["src"] = "server"
                    changed = True
                if changed:
                    self._touch(old)
            else:
                self.seq += 1
                is_dm = m.get("room") not in (ROOM_ALL, "")
                if m.get("from_uid") == self.uid:     # ours, sent another way (deck_chat.py --send)
                    m.update({"seq": self.seq, "src": "me", "via": [], "mine": True,
                              "status": "sent", "relayed": src == "mesh-bridge",
                              "thread": (m.get("to_uid") or m.get("room_id")) if is_dm else "ALL", "dm": is_dm})
                else:
                    to_me = is_dm and (self.uid in (m.get("room_id"), m.get("to_uid")) or m.get("room") == self.callsign)
                    m.update({"seq": self.seq, "src": "server" if src == "server" else "mesh",
                              "via": [src], "mine": False, "dm": bool(to_me),
                              "thread": (m.get("from_uid") or m.get("from")) if to_me else "ALL"})
                self.msgs.append(m)
                self.by_id[m["id"]] = m
                if len(self.by_id) > 4 * self.msgs.maxlen:
                    keep = {x["id"] for x in self.msgs}
                    self.by_id = {k: v for k, v in self.by_id.items() if k in keep}
                save = dict(m)
        if save is None:
            return False
        if old is None and not save.get("mine"):
            self.heard(save.get("from_uid"), save.get("from"), save["src"] if save["src"] == "server" else "mesh-chat")
            log.info("Chat [%s] %s: %s", src, save["from"], save["text"][:80])
        self._save(save)
        return old is None

    # -- send
    def send(self, text, to=None):
        """to = None for All Chat Rooms, or a contact's UID/callsign for a direct message."""
        text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", text or "").strip()
        if not text:
            return {"ok": False, "error": "empty message"}
        if len(text) > MAX_TEXT:
            return {"ok": False, "error": "message too long (%d characters max)" % MAX_TEXT}
        dm = None
        if to:
            c = self.contact(to)
            if c is None:
                return {"ok": False, "error": "no contact %s on the roster yet" % to}
            path = self._path(c, time.time() - 60 * float(self.cfg["ROSTER_MINUTES"]))
            if path is None:
                return {"ok": False, "error": "no way to reach %s yet (need their position report)" % c["callsign"]}
            dm = {"to_uid": c["uid"], "to": c["callsign"], "path": path, "endpoint": c.get("mesh_ep", "")}
        now = time.monotonic()
        limit = int(self.cfg["MAX_SENDS_PER_10S"])
        with self.lock:
            while self.sends and now - self.sends[0] > 10:
                self.sends.popleft()
            if len(self.sends) >= limit:
                return {"ok": False, "error": "slow down: %d messages per 10 s max (protects the radio link)" % limit}
            self.sends.append(now)
            m = {"id": str(uuid.uuid4()), "uid": "", "from": self.callsign, "from_uid": self.uid,
                 "room": ROOM_ALL, "room_id": ROOM_ALL, "text": text, "ts": time.time(),
                 "src": "me", "via": [], "mine": True, "relayed": False,
                 "status": "queued", "confirmed": True, "error": "", "thread": "ALL", "dm": False,
                 "path": "mesh-all"}
            if dm:
                m.update(dm)
                m.update({"room": dm["to"], "room_id": dm["to_uid"], "thread": dm["to_uid"], "dm": True})
            self.seq += 1
            m["seq"] = self.seq
            self.msgs.append(m)
            self.by_id[m["id"]] = m          # before sending: the loopback copy can arrive instantly
            self.outbox.append(m)
        self.flush()
        with self.lock:
            st, err = m["status"], m.get("error", "")
        if st != "sent":
            self._save(dict(m))
        return {"ok": True, "id": m["id"], "status": st, "note": err}

    def _transmit(self, m):
        """Send one message by its path. Returns '' or an error string."""
        local = self.local_addr()
        if not m.get("dm"):
            # All Chat Rooms goes over the mesh when the node is there (the bridge copies it to the server),
            # and straight to the server when it isn't (server-only mode). Decided at send time, so a message
            # queued while both were down goes whichever way comes back first.
            m["path"] = "mesh-all" if local else "server-all"
        path = m.get("path", "mesh-all")
        if path not in ("server", "server-all") and not local:
            return "mesh interface %s has no IP address (node unplugged?)" % self.cfg["MESH_IFACE"]
        text = m["text"]
        waited = time.time() - m["ts"]
        if waited > 60 or m.get("confirmed_late"):   # say it is late, so nobody reads it as current
            text = "[queued %s] %s" % (datetime.fromtimestamp(m["ts"], tz=timezone.utc).strftime("%H%MZ"), text)
        lat, lon = self.cfg["LAT"], self.cfg["LON"]
        if self.gps:
            live, pos = self.gps.position()
            if live:
                lat, lon = pos["lat"], pos["lon"]        # stamp chat with where the deck is
        _, xml = build_geochat(self.callsign, self.uid, text, lat, lon, msg_id=m["id"],
                               to_uid=m.get("to_uid"), to_callsign=m.get("to"), via_server=(path == "server"))
        if path == "mesh":
            return self._send_direct(m, xml, text, local)
        if path in ("server", "server-all"):
            return self._send_server(m, xml, text)
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            try:
                s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, self.ttl)
                s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)   # so the bridge on this deck hears it
                s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(local))
                s.sendto(xml, self.group)
            finally:
                s.close()
        except OSError as exc:
            return "send failed: %s" % exc
        m["sent_text"] = text
        log.info("Sent via %s: %s", local, text[:80])
        return ""

    def _send_direct(self, m, xml, text, local):
        """DM to a mesh phone: deliver to the endpoint from its position report (ATAK: ip:4242:tcp)."""
        c = self.contact(m["to_uid"]) or {}
        ep = c.get("mesh_ep") or m.get("endpoint", "")
        try:
            ip, port, proto = ep.split(":")
            port = int(port)
        except ValueError:
            return "no mesh address for %s" % m.get("to", "?")
        try:
            if proto == "udp":
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.bind((local, 0))
                s.sendto(xml, (ip, port))
                s.close()
            else:
                s = socket.create_connection((ip, port), timeout=4, source_address=(local, 0))
                s.sendall(xml)
                s.close()
        except OSError as exc:
            return "can't reach %s at %s:%d (%s)" % (m.get("to", "?"), ip, port, exc.strerror or exc)
        m["sent_text"], m["delivered"] = text, proto != "udp"
        log.info("DM to %s via mesh %s:%d: %s", m.get("to"), ip, port, text[:80])
        return ""

    def _send_server(self, m, xml, text):
        """DM to someone on the server: hand it to the bridge (server only, never the mesh)."""
        if not self.bridge_state()["running"]:
            if m.get("to"):
                return "bridge is down (needed to reach %s on the server)" % m["to"]
            return "no node (%s has no address) and the bridge is down: goes out when either is back" % self.cfg["MESH_IFACE"]
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.sendto(xml, hostport(self.cfg["LOCAL_SEND"]))
            s.close()
        except OSError as exc:
            return "bridge hand-off failed: %s" % exc
        m["sent_text"] = text
        if m.get("dm"):
            log.info("DM to %s via server: %s", m.get("to"), text[:80])
        else:
            log.info("All Chat Rooms via the server only (no mesh): %s", text[:80])
        return ""

    def flush(self):
        """Send what can go now. Oldest first; a held message does not block newer ones."""
        changed = []
        with self.lock:
            if self.expire_after:            # waited too long to still matter: expire it (kept in the log, never sent)
                now = time.time()
                for m in [m for m in self.outbox if m["status"] != "sending" and now - m["ts"] > self.expire_after]:
                    self.outbox.remove(m)
                    m["status"], m["error"] = "expired", "not sent: waited over %g h" % (self.expire_after / 3600)
                    self._touch(m)
                    changed.append(dict(m))
                if changed:
                    log.info("Outbox: %d message(s) not sent within %g h expired", len(changed), self.expire_after / 3600)
            pending = list(self.outbox)
        for m in pending:
            with self.lock:
                if m not in self.outbox or m["status"] == "sending":
                    continue                     # gone, or another thread is sending it right now
                if not m.get("confirmed") or (time.time() - m["ts"] > self.hold_after and m["status"] != "sending"
                                              and not m.get("confirmed_late")):
                    if m["status"] != "held":
                        m["status"], m["confirmed"] = "held", False
                        m["error"] = "waited too long; send or drop it"
                        self._touch(m)
                        changed.append(dict(m))
                    continue
                prev_status = m["status"]
                m["status"] = "sending"
            err = self._transmit(m)
            with self.lock:
                if err:
                    if m.get("error") != err or prev_status != "queued":
                        m["status"], m["error"] = "queued", err
                        self._touch(m)
                        changed.append(dict(m))
                    else:
                        m["status"] = "queued"
                    if m.get("path", "mesh-all") in ("mesh-all", "server-all"):
                        break                    # no way out for group chat right now: keep the rest in order
                    continue                     # one unreachable DM contact doesn't block the others
                m["status"], m["error"], m["sent_ts"] = "sent", "", time.time()
                self.outbox.remove(m)
                self._touch(m)
                changed.append(dict(m))
        for m in changed:
            self._save(m)

    def outbox_action(self, msg_id, action):
        with self.lock:
            m = self.by_id.get(msg_id)
            if m is None or m not in self.outbox:
                return {"ok": False, "error": "not in the outbox"}
            if action == "drop":
                self.outbox.remove(m)
                m["status"], m["error"] = "dropped", ""
                self._touch(m)
                snap = dict(m)
            elif action == "send":
                m["confirmed"], m["confirmed_late"] = True, True
                m["status"], m["error"] = "queued", ""
                self._touch(m)
                snap = None
            else:
                return {"ok": False, "error": "unknown action"}
        if snap:
            self._save(snap)
        else:
            self.flush()
        with self.lock:
            return {"ok": True, "status": m["status"], "note": m.get("error", "")}

    def since(self, after):
        with self.lock:
            return [dict(m) for m in self.msgs if m["seq"] > after], self.seq

    def bridge_state(self):
        p = os.path.expanduser(self.cfg["BRIDGE_STATUS_FILE"])
        try:
            st = json.load(open(p))
            age = time.time() - float(st.get("now", 0))
            return {"running": age < 10, "age": age}
        except Exception:
            return {"running": False, "age": None}

    def info(self):
        with self.lock:
            waiting = len(self.outbox)
            held = sum(1 for m in self.outbox if m["status"] == "held")
        return {"version": VERSION, "callsign": self.callsign, "uid": self.uid,
                "mesh_addr": self.local_addr(), "mesh_iface": self.cfg["MESH_IFACE"],
                "group": "%s:%d" % self.group,
                "mesh_direct": self.mesh_direct, "mesh_error": self.mesh_error,
                "sa_listening": self.sa_listening,
                "bridge_copy": {"listening": self.copy_ok,
                                "last_rx_age": (time.time() - self.last_copy_rx) if self.last_copy_rx else None},
                "bridge": self.bridge_state(),
                "outbox": {"waiting": waiting, "held": held, "error": getattr(self, "outbox_error", "")},
                "bad_packets": {"count": getattr(self, "bad_packets", 0), "last": getattr(self, "last_bad", "")},
                "roster": self.roster_list(), "roster_minutes": float(self.cfg["ROSTER_MINUTES"]),
                "takproto": takproto is not None,
                "gps": (lambda g: {"on": True, "mode": g["mode"], "fix_age": g["fix_age"], "gpsd": g["gpsd"]})(self.gps.snapshot()) if self.gps else {"on": False},
                "dm": {"port": int(self.cfg["DM_PORT"]), "listening": getattr(self, "dm_listening", None),
                       "sa_send": self.cfg["SA_SEND"].strip().lower() not in ("0", "off", "no", "false")},
                "quick": [q.strip() for q in self.cfg["QUICK"].split("|") if q.strip()][:12]}

    def export_text(self):
        """Plain-text log for an after-action review, times in Zulu."""
        msgs = {}
        order = []
        if self.hist_path and os.path.exists(self.hist_path):
            try:
                for ln in open(self.hist_path):
                    try:
                        m = json.loads(ln)
                    except ValueError:
                        continue
                    if m.get("id") not in msgs:
                        order.append(m.get("id"))
                    msgs[m.get("id")] = m
            except OSError:
                pass
        with self.lock:
            for m in self.msgs:
                if m["id"] not in msgs:
                    order.append(m["id"])
                msgs[m["id"]] = dict(m)
        rows = sorted((msgs[i] for i in order), key=lambda m: m.get("ts", 0))
        out = ["TAK MESH TOC CHAT LOG  %s  callsign %s  exported %s" % (self.uid, self.callsign, dtg(time.time())), ""]
        for m in rows:
            src = "ME" if m.get("mine") else ("SERVER" if m.get("src") == "server" else "MESH")
            note = ""
            if m.get("mine"):
                note = {"sent": " (relayed)" if m.get("relayed") else " (mesh)", "queued": " (NOT SENT: queued)",
                        "held": " (NOT SENT: held)", "dropped": " (NOT SENT: dropped)",
                        "expired": " (NOT SENT: expired)"}.get(m.get("status", "sent"), "")
            text = m.get("text", "").replace("\n", "\n" + " " * 21)
            who = m.get("from", "?")
            if m.get("dm"):
                who = "%s -> %s (DM)" % (who, m.get("to") or m.get("room") or "?") if m.get("mine") else "%s -> me (DM)" % who
            out.append("%-14s %-6s %s: %s%s" % (dtg(m.get("ts", 0))[:7], src, who, text, note))
        return "\n".join(out) + "\n"


def addr_present(ip):
    """True if this IPv4 address is on one of the deck's interfaces right now (binding to it only works then)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.bind((ip, 0))
        finally:
            s.close()
        return True
    except OSError:
        return False


# ---------------------------------------------------------------- listeners
def join_group(group, local):
    group_ip, port = group
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    except (AttributeError, OSError):
        pass
    s.bind((group_ip, port))
    s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                 struct.pack("4s4s", socket.inet_aton(group_ip), socket.inet_aton(local)))
    s.settimeout(5)
    return s


def group_listener(chat, group, handle, flag, name):
    """Join a multicast group on the mesh interface; rejoin if eth0 changes. flag = attribute on chat."""
    while True:
        local = chat.local_addr()
        if not local:
            setattr(chat, flag, False)
            if flag == "mesh_direct":
                chat.mesh_error = "no IP on %s" % chat.cfg["MESH_IFACE"]
            time.sleep(5)
            continue
        try:
            s = join_group(group, local)
        except OSError as exc:
            setattr(chat, flag, False)
            if flag == "mesh_direct":
                chat.mesh_error = str(exc)
            log.warning("%s listener could not start (%s); retrying in 30 s", name, exc)
            time.sleep(30)
            continue
        setattr(chat, flag, True)
        if flag == "mesh_direct":
            chat.mesh_error = ""
        log.info("Listening for %s on %s:%d via %s", name, group[0], group[1], local)
        while True:
            try:
                data, addr = s.recvfrom(65535)
            except socket.timeout:
                if chat.local_addr() != local:
                    log.info("Mesh address changed; rejoining %s", name)
                    break
                continue
            except OSError as exc:
                log.warning("%s listener error: %s", name, exc)
                break
            try:
                handle(data, addr)
            except Exception as exc:          # one bad packet must not stop the listener, but it is counted and said
                n = chat.bad_packets = getattr(chat, "bad_packets", 0) + 1
                chat.last_bad = ("%s on %s: %s: %s" % (time.strftime("%H:%M:%S"), name, type(exc).__name__, exc))[:200]
                if n <= 3 or n % 100 == 0:
                    log.warning("A %s packet couldn't be handled (%s: %s); %d so far", name, type(exc).__name__, exc, n)
        try:
            s.close()
        except OSError:
            pass
        setattr(chat, flag, False)
        time.sleep(2)


def bridge_copy_listener(chat):
    """Copies from tak_mesh_bridge.py (LOCAL_CHAT_COPY). Each packet starts with
    'src=mesh\\n' or 'src=server\\n' followed by the CoT XML."""
    addr = hostport(chat.cfg["BRIDGE_COPY_LISTEN"])
    while True:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.bind(addr)
            break
        except OSError as exc:
            chat.copy_ok = False
            log.warning("Bridge copy port %s:%d busy (%s); retrying in 15 s", addr[0], addr[1], exc)
            time.sleep(15)
    chat.copy_ok = True
    log.info("Listening for bridge chat copies on %s:%d", *addr)
    while True:
        try:
            data, peer = s.recvfrom(65535)
        except OSError:
            time.sleep(1)
            continue
        if peer[0] != "127.0.0.1":
            continue
        src = "server"
        if data.startswith(b"src="):
            head, _, data = data.partition(b"\n")
            src = "mesh-bridge" if head.strip() == b"src=mesh" else "server"
        chat.last_copy_rx = time.time()
        m = parse_chat(data)
        if m:
            chat.add(m, src)
        elif src == "server":
            r = parse_sa(data)
            if r:
                chat.heard(r[0], r[1], "server", r[3])


def dm_listener(chat):
    """TCP DM_PORT on the mesh address: mesh phones deliver direct messages to the deck here."""
    port = int(chat.cfg["DM_PORT"])
    while True:
        local = chat.local_addr()
        if not local:
            chat.dm_listening = False
            time.sleep(5)
            continue
        try:
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind((local, port))
            srv.listen(8)
            srv.settimeout(5)
        except OSError as exc:
            chat.dm_listening = False
            log.warning("DM port %s:%d unavailable (%s); retrying in 30 s", local, port, exc)
            time.sleep(30)
            continue
        chat.dm_listening = True
        log.info("Listening for direct messages on %s:%d (tcp)", local, port)
        while True:
            try:
                conn, peer = srv.accept()
            except socket.timeout:
                if chat.local_addr() != local:
                    break
                continue
            except OSError:
                break
            threading.Thread(target=dm_client, args=(chat, conn, peer), daemon=True).start()
        try:
            srv.close()
        except OSError:
            pass
        chat.dm_listening = False


def dm_client(chat, conn, peer):
    """Read CoT events from one phone's connection (it may send one and close, or keep it open)."""
    conn.settimeout(15)
    buf = b""
    try:
        while len(buf) < 262144:
            d = conn.recv(65536)
            if not d:
                break
            buf += d
            while b"</event>" in buf:
                i = buf.index(b"</event>") + len(b"</event>")
                ev, buf = buf[:i], buf[i:]
                m = parse_chat(ev)
                if m:
                    chat.add(m, "mesh")
                else:
                    r = parse_sa(ev)
                    if r:
                        chat.heard(r[0], r[1], "mesh", r[3])
    except OSError:
        pass
    finally:
        try:
            conn.close()
        except OSError:
            pass


def dm_udp_listener(chat):
    """UDP DM_PORT on the mesh address: iTAK sends direct messages by UDP to the endpoint in our position
    report, even though it says tcp. Same handling as dm_client; a repeated copy is ignored (same id)."""
    port = int(chat.cfg["DM_PORT"])
    while True:
        local = chat.local_addr()
        if not local:
            time.sleep(5)
            continue
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((local, port))
            s.settimeout(5)
        except OSError as exc:
            log.warning("DM port %s:%d (udp) unavailable (%s); retrying in 30 s", local, port, exc)
            time.sleep(30)
            continue
        log.info("Listening for direct messages on %s:%d (udp, for iTAK)", local, port)
        while True:
            try:
                d, _ = s.recvfrom(65536)
            except socket.timeout:
                if chat.local_addr() != local:
                    break
                continue
            except OSError:
                break
            if d[:1] == b"\xbf":
                try:
                    d = proto_to_xml(d) or b""
                except Exception:
                    d = b""
            j, k = d.find(b"<event"), d.rfind(b"</event>")
            if j < 0 or k < j:
                continue
            ev = d[j:k + len(b"</event>")]
            m = parse_chat(ev)
            if m:
                chat.add(m, "mesh")
            else:
                r = parse_sa(ev)
                if r:
                    chat.heard(r[0], r[1], "mesh", r[3])
        try:
            s.close()
        except OSError:
            pass


def sa_sender(chat):
    """Announce the deck on the mesh (contact + DM endpoint) every SA_INTERVAL seconds."""
    interval = max(15.0, float(chat.cfg["SA_INTERVAL"]))
    gps_interval = max(5.0, float(chat.cfg["SA_GPS_INTERVAL"]))
    group = hostport(chat.cfg["SA_GROUP"])
    last_sent, was_live = 0.0, None
    while True:
        local = chat.local_addr()
        live, pos = chat.gps.position() if chat.gps else (False, None)
        due = time.monotonic() - last_sent >= (gps_interval if live else interval)
        if local and (due or (was_live and not live)):
            xml = build_sa(chat.callsign, chat.uid, "%s:%s:tcp" % (local, chat.cfg["DM_PORT"]),
                           chat.cfg["LAT"], chat.cfg["LON"], gps_interval if live else interval, pos=pos, live=live,
                           etype=chat.cfg["SA_TYPE"].strip(), team=team_xml(chat.cfg["SA_TEAM"], chat.cfg["SA_ROLE"]))
            last_sent = time.monotonic()
            if chat.gps and live != was_live and (live or was_live):     # no "lost" line at startup
                log.info("GPS %s: DECK %s on mesh maps", "fix" if live else "fix lost",
                         "shown live" if live else "kept at last spot (stale)" if pos else "not shown")
            was_live = live
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
                s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, chat.ttl)
                s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(local))
                s.sendto(xml, group)
                s.close()
            except OSError as exc:
                log.debug("SA send failed: %s", exc)
        time.sleep(1)


def outbox_worker(chat):
    fails = 0
    while True:
        time.sleep(3)
        try:
            with chat.lock:
                busy = bool(chat.outbox)
            if busy:
                chat.flush()
            if chat.outbox_error:
                log.info("Outbox: sending works again")
            chat.outbox_error, fails = "", 0
        except Exception as exc:              # the sender itself failed: shown on the chat screen (OUTBOX ... STUCK)
            fails += 1
            chat.outbox_error = ("%s: %s" % (type(exc).__name__, exc))[:200]
            if fails <= 3 or fails % 100 == 0:
                log.warning("Outbox: sending failed (%s); %d times so far", chat.outbox_error, fails)


# ---------------------------------------------------------------- web
class Handler(BaseHTTPRequestHandler):
    chat = None
    allowed_hosts = set()
    server_version = "DeckChat/" + VERSION

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _host_ok(self):
        return (self.headers.get("Host") or "").lower() in self.allowed_hosts

    def _local(self):
        """The deck's own screen (loopback). Listening and bridge switches work from here only."""
        return self.client_address[0] in ("127.0.0.1", "::1", "::ffff:127.0.0.1")

    def _voice_conn(self, timeout):
        import http.client
        h, p = hostport(self.chat.cfg.get("VOICE_CONTROL", "127.0.0.1:17016"))
        return http.client.HTTPConnection(h, p, timeout=timeout)

    def _voice_listen(self, tg):
        """Relay manet-voice's live audio for one talk group (raw 16-bit 48 kHz mono) to this browser.
        Plain sockets, so a closed page is noticed at once and manet-voice lets go of it."""
        import select
        h, p = hostport(self.chat.cfg.get("VOICE_CONTROL", "127.0.0.1:17016"))
        try:
            up = socket.create_connection((h, p), timeout=5)
            up.sendall(("GET /listen?tg=%s HTTP/1.0\r\nHost: voice\r\n\r\n" % quote(tg)).encode())
            head = b""
            while b"\r\n\r\n" not in head and len(head) < 8192:
                b = up.recv(4096)
                if not b:
                    break
                head += b
        except OSError:
            return self._send(503, {"error": "voice service not answering"})
        head, _, rest = head.partition(b"\r\n\r\n")
        try:
            code = int(head.split(b" ", 2)[1])
        except (IndexError, ValueError):
            code = 502
        if code != 200:
            up.close()
            msg = rest[:2048] if rest.startswith(b"{") else {"error": "voice service answered %d" % code}
            return self._send(code if 400 <= code < 600 else 502, msg)
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            if rest:
                self.wfile.write(rest)
            while True:
                rd, _, _ = select.select([up, self.connection], [], [], 5)
                if self.connection in rd and self.connection.recv(1, socket.MSG_PEEK) == b"":
                    break                                # the page stopped listening
                if up in rd:
                    b = up.recv(8192)
                    if not b:
                        break
                    self.wfile.write(b)
        except (OSError, ValueError):
            pass
        finally:
            up.close()

    def do_GET(self):
        u = urlparse(self.path)
        if not self._host_ok():
            return self._send(403, {"error": "forbidden host"})
        if u.path in ("/", "/index.html"):
            try:
                html = open(os.path.join(HERE, "index.html"), "rb").read()
            except OSError:
                return self._send(500, "index.html missing", "text/plain")
            return self._send(200, html, "text/html; charset=utf-8")
        if u.path.startswith("/fonts/"):
            ff = font_file(u.path[len("/fonts/"):])
            if not ff:
                return self._send(404, {"error": "no such font"})
            with open(ff, "rb") as f:
                return self._send(200, f.read(), "font/woff2")
        if u.path in ("/voice", "/voice.html"):
            try:
                html = open(os.path.join(HERE, "voice.html"), "rb").read()
            except OSError:
                return self._send(500, "voice.html missing", "text/plain")
            return self._send(200, html, "text/html; charset=utf-8")
        if u.path == "/api/voice":
            path = os.path.expanduser(self.chat.cfg.get("VOICE_STATUS_FILE", "~/voice_status.json")) if self.chat else ""
            try:
                st = os.stat(path)
                with open(path, "rb") as f:
                    body = f.read(512 * 1024)
                age = time.time() - st.st_mtime
            except OSError:
                return self._send(200, {"running": False, "reason": "voice service not running (VOICE = off, or not installed)"})
            try:
                data = json.loads(body)
            except ValueError:
                data = None
            if not isinstance(data, dict):
                return self._send(200, {"running": False, "reason": "voice status unreadable"})
            data.pop("mumble", None)          # the Mumble server's (Tailscale) address stays on the deck
            data["running"] = age < 10
            data["control"] = data["running"] and self._local()
            data["age"] = round(age, 1)
            if not data["running"]:
                data["reason"] = "voice service stopped %d s ago" % age
            return self._send(200, data)
        if u.path == "/api/voice/listen":
            if not self._local():
                return self._send(403, {"error": "listening works on the deck's own screen only"})
            return self._voice_listen(parse_qs(u.query).get("tg", [""])[0][:16])
        if u.path == "/icon.svg":
            try:
                return self._send(200, open(os.path.join(HERE, "icon.svg"), "rb").read(), "image/svg+xml")
            except OSError:
                return self._send(404, {"error": "no icon"})
        if u.path == "/healthz":
            return self._send(200, {"ok": True})
        if u.path == "/api/messages":
            try:
                after = int(parse_qs(u.query).get("after", ["0"])[0])
            except ValueError:
                after = 0
            msgs, seq = self.chat.since(after)
            return self._send(200, {"seq": seq, "messages": msgs})
        if u.path == "/api/info":
            return self._send(200, self.chat.info())
        if u.path == "/api/export":
            name = "deck_chat_%s.txt" % datetime.now(timezone.utc).strftime("%Y%m%d_%H%MZ")
            return self._send(200, self.chat.export_text(), "text/plain; charset=utf-8",
                              {"Content-Disposition": 'attachment; filename="%s"' % name})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        u = urlparse(self.path)
        if not self._host_ok() or self.headers.get("X-Deck") != "1":
            return self._send(403, {"ok": False, "error": "forbidden"})
        try:
            n = min(int(self.headers.get("Content-Length", "0")), 16384)
            body = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("body must be a JSON object")
        except (ValueError, TypeError):
            return self._send(400, {"ok": False, "error": "bad request"})
        if u.path == "/api/send":
            res = self.chat.send(str(body.get("text", "")), to=(str(body["to"]) if body.get("to") else None))
        elif u.path == "/api/outbox":
            res = self.chat.outbox_action(str(body.get("id", "")), str(body.get("action", "")))
        elif u.path == "/api/voice/bridge":
            if not self._local():
                return self._send(403, {"ok": False, "error": "bridging is switched on the deck's own screen only"})
            try:
                c = self._voice_conn(5)
                c.request("POST", "/bridge", json.dumps({"tg": str(body.get("tg", ""))[:16], "on": bool(body.get("on"))}),
                          {"Content-Type": "application/json"})
                rr = c.getresponse()
                out = rr.read(4096)
                c.close()
                return self._send(rr.status, out)
            except OSError:
                return self._send(503, {"ok": False, "error": "voice service not answering"})
        else:
            return self._send(404, {"ok": False, "error": "not found"})
        return self._send(200 if res.get("ok") else 400, res)


def load_cfg(path):
    cp = configparser.ConfigParser(interpolation=None, strict=False)   # a setting written twice = the last one wins
    cp.optionxform = str
    cp.read_dict({"chat": DEFAULTS})
    if path:
        if not cp.read(os.path.expanduser(path), encoding="utf-8-sig"):   # utf-8-sig: ignore a Windows "BOM" marker
            log.warning("Config %s not found; using defaults", path)
    for note in apply_deck(cp, "chat", "chat", DEFAULTS):
        log.warning("Config: %s", note)
    return cp["chat"]


def main():
    ap = argparse.ArgumentParser(description="ATAK chat from the deck")
    ap.add_argument("--config", default=os.path.expanduser("~/manet_mesh_bridge/deck.ini"))
    ap.add_argument("--send", metavar="TEXT", help="send one message and exit")
    ap.add_argument("--to", metavar="CALLSIGN", help="with --send: direct message (needs the service's roster, so only\n"
                    "contacts reachable over the server work from the command line)")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if TAKPROTO_ERROR:
        log.warning("TAK Protocol (protobuf) decoding OFF: the bundled takproto did not load (%s). Usually another "
                    "protobuf on this computer; the service runs python3 -S to avoid that. XML chat still works.", TAKPROTO_ERROR)
    cfg = load_cfg(a.config)
    if a.send is not None:
        cfg["HISTORY_FILE"] = "off"          # the running service records it when it hears it
        chat = Chat(cfg)
        if a.to:
            chat.heard("cli-" + a.to, a.to, "server")   # assume reachable via the server by callsign
            c = chat.roster.pop("cli-" + a.to)
            chat.roster[a.to] = c
        res = chat.send(a.send, to=a.to)
        ok = res["ok"] and res.get("status") == "sent"
        print("sent" if ok else "NOT sent: " + (res.get("error") or res.get("note") or res.get("status", "")))
        sys.exit(0 if ok else 1)
    chat = Chat(cfg)
    if chat.gps:
        chat.gps.start()
        log.info("GPS: following gpsd at %s", cfg["GPS_HOST"])
    threading.Thread(target=group_listener, daemon=True,
                     args=(chat, chat.group, lambda d, a=None: (lambda m: m and chat.add(m, "mesh"))(parse_chat(d)),
                           "mesh_direct", "mesh chat")).start()
    if cfg["SA_GROUP"].strip().lower() not in ("", "off", "no", "0"):
        def on_sa(d, addr=None):
            r = parse_sa(d)
            if r:
                chat.heard(r[0], r[1], "mesh", r[3], from_ip=addr[0] if addr else None)
        threading.Thread(target=group_listener, daemon=True,
                         args=(chat, hostport(cfg["SA_GROUP"]), on_sa, "sa_listening", "position reports")).start()
    threading.Thread(target=bridge_copy_listener, args=(chat,), daemon=True).start()
    threading.Thread(target=outbox_worker, args=(chat,), daemon=True).start()
    threading.Thread(target=dm_listener, args=(chat,), daemon=True).start()
    threading.Thread(target=dm_udp_listener, args=(chat,), daemon=True).start()
    if cfg["SA_SEND"].strip().lower() not in ("0", "off", "no", "false") and cfg["SA_GROUP"].strip().lower() not in ("", "off"):
        threading.Thread(target=sa_sender, args=(chat,), daemon=True).start()
    host, port = hostport(cfg["LISTEN"])
    Handler.chat = chat
    Handler.allowed_hosts = {"127.0.0.1:%d" % port, "localhost:%d" % port}
    if host not in ("127.0.0.1", "localhost", "0.0.0.0", ""):
        Handler.allowed_hosts.add("%s:%d" % (host, port))
    httpd = ThreadingHTTPServer((host, port), Handler)
    log.info("Deck chat %s as %s (%s) on http://%s:%d", VERSION, chat.callsign, chat.uid, host, port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
