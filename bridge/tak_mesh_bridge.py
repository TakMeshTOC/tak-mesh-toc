#!/usr/bin/env python3
"""tak_mesh_bridge.py - the TAK MESH TOC bridge: ATAK traffic on a mesh network <-> a TAK server.

Runs on the deck, wired to a mesh node on one port and reaching the TAK server on another (Wi-Fi, cellular, VPN).

  mesh -> server   CoT from the mesh (SA 239.2.3.1:6969, chat 224.10.10.1:17012) over one TLS connection.
                   Protobuf is converted to XML; broken packets are dropped or repaired the way OTS would.
  server -> mesh   positions, chats, markers and drawings (RETURN_PATH), rate-limited, never echoed back.
  identity         introduces itself first on every connection (DECK-<hostname>), with GPS when it has a fix.
  DMs              private DMs both ways (DM_RELAY, with server/mesh_mailroute), self-tested every 60 s.
  server link      pings the server and reconnects when answers stop; chats wait on disk until confirmed.
  status           a JSON status file every 2 s for Ops and `meshtoc check`.

Settings: config/deck.ini.example. Run:  python3 -S tak_mesh_bridge.py deck.ini
(-S: only the standard library and the bundled vendor/ libraries.)
"""
import asyncio
import base64
import configparser
import json
import logging
import os
import re
import socket
import sys
import time
from collections import OrderedDict, deque
import heapq
import xml.etree.ElementTree as ET
from urllib.parse import urlparse

# Bundled libraries (vendor/: PyTAK, takproto, protobuf) when run as "python3 -S" (how the service runs it).
_VENDOR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "vendor")
if sys.flags.no_site and os.path.isdir(_VENDOR) and _VENDOR not in sys.path:
    sys.path.insert(0, _VENDOR)

import warnings  # noqa: E402
warnings.filterwarnings("ignore", message=".*aiohttp.*")       # PyTAK's optional extras: not used by the bridge
warnings.filterwarnings("ignore", message=".*cryptography.*")
import pytak  # noqa: E402

# --- TAK protobuf (v1 mesh) -> CoT XML ------------------------------------
# Newer ATAK builds can send Mesh SA as TAK Protocol v1 protobuf (datagrams
# starting 0xbf 0x01 0xbf). The TAK server stream here expects XML, so convert.
try:
    import takproto
except ImportError:          # bridge still works for XML-only senders
    takproto = None
from datetime import datetime, timezone
from xml.sax.saxutils import quoteattr, escape as xml_escape

MESH_HDR = b"\xbf\x01\xbf"



def _ts(ms):
    dt = datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (dt.microsecond // 1000)


def proto_kind(data):
    """What a 0xbf mesh datagram is: 'event' (a CoT event: convert and forward), 'control' (TAK Protocol
    version announcement, no CoT inside: nothing to forward) or 'unknown' (not TAK Protocol v1 mesh)."""
    if data[:3] != MESH_HDR:
        return "unknown"
    msg = takproto.parse_mesh(bytearray(data))
    if msg.HasField("cotEvent"):
        return "event"
    if msg.HasField("takControl"):
        return "control"
    return "unknown"


def proto_to_xml(data):
    """Return CoT XML bytes for a protobuf mesh datagram, or None if it can't be parsed."""
    if data[:3] != MESH_HDR:
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
    if d.HasField("group"):
        parts.append("<__group name=%s role=%s/>" % (quoteattr(d.group.name), quoteattr(d.group.role)))
    if d.HasField("status"):
        parts.append("<status battery=%s/>" % quoteattr(str(d.status.battery)))
    if d.HasField("takv"):
        parts.append("<takv device=%s platform=%s os=%s version=%s/>" % (
            quoteattr(d.takv.device), quoteattr(d.takv.platform),
            quoteattr(d.takv.os), quoteattr(d.takv.version)))
    if d.HasField("track"):
        parts.append("<track speed=%s course=%s/>" % (
            quoteattr(repr(d.track.speed)), quoteattr(repr(d.track.course))))
    if d.xmlDetail:
        parts.append(d.xmlDetail)
    xml = (
        '<event version="2.0" uid=%s type=%s time=%s start=%s stale=%s how=%s>'
        '<point lat="%r" lon="%r" hae="%r" ce="%r" le="%r"/><detail>%s</detail></event>'
    ) % (quoteattr(e.uid), quoteattr(e.type), quoteattr(_ts(e.sendTime)),
         quoteattr(_ts(e.startTime)), quoteattr(_ts(e.staleTime)), quoteattr(e.how),
         e.lat, e.lon, e.hae, e.ce, e.le, "".join(parts))
    return xml.encode()


log = logging.getLogger("tak_mesh_bridge")

# PyTAK reads server events with readuntil("</event>"), capped by asyncio's 64 KB limit, so one big route or
# drawing would end the bridge. Raise the cap to 4 MB and skip anything bigger instead of crashing.
SERVER_READ_LIMIT = 4 * 1024 * 1024
MESH_MAX_DATAGRAM = 65507                 # the largest UDP packet: bigger events cannot go onto the mesh
_pytak_readcot = pytak.RXWorker.readcot
_oversize = {"n": 0}


async def _readcot_big(self):
    r = self.reader
    if hasattr(r, "readuntil") and getattr(r, "_limit", SERVER_READ_LIMIT) < SERVER_READ_LIMIT:
        r._limit = SERVER_READ_LIMIT
    try:
        cot = await _pytak_readcot(self)
        if cot is None and hasattr(r, "at_eof") and r.at_eof():
            # PyTAK would retry the closed socket forever (a full CPU core, no reconnect). End this connection
            # instead: the process exits and systemd starts a fresh one 5 s later.
            Link.fail("the server closed the connection")
        return cot
    except asyncio.LimitOverrunError as exc:
        n = exc.consumed + (len(b"</event>") if "is found" in str(exc) else 0)
        await r.readexactly(n)                # throw the oversized event away, keep the connection
        _oversize["n"] += 1
        if _oversize["n"] <= 3 or _oversize["n"] % 100 == 0:
            log.warning("Skipped an event from the server bigger than %d MB (%d so far)",
                        SERVER_READ_LIMIT // 1048576, _oversize["n"])
        return None


pytak.RXWorker.readcot = _readcot_big

# Sending: keep little unsent data in the socket (Uplink holds the newest positions instead, so a stalled link
# catches up with fresh data), and let the kernel drop a link that stops answering (silent cell/VPN dropout).
_pytak_send = pytak.TXWorker.send_data
LINK_SETTINGS = {"timeout": 60.0}


def tune_server_socket(writer):
    try:
        sock = writer.get_extra_info("socket")
        if sock is None:
            return
        sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, "TCP_NOTSENT_LOWAT", 25), 4096)
        sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, "TCP_USER_TIMEOUT", 18), int(LINK_SETTINGS["timeout"] * 1000))
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        for opt, val in (("TCP_KEEPIDLE", 30), ("TCP_KEEPINTVL", 10), ("TCP_KEEPCNT", 3)):
            if hasattr(socket, opt):
                sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, opt), val)
        tr = getattr(writer, "transport", None)
        if tr is not None:
            tr.set_write_buffer_limits(high=4096)
    except (OSError, AttributeError, ValueError) as exc:
        log.warning("Could not tune the server socket: %s", exc)


async def _send_tuned(self, data):
    if not getattr(self, "_atb_tuned", False):
        self._atb_tuned = True
        tune_server_socket(self.writer)
    r = await _pytak_send(self, data)
    Outbox.sent(data)                             # written to the server socket: no longer waiting
    return r


pytak.TXWorker.send_data = _send_tuned


def _boot_id():
    try:
        with open("/proc/sys/kernel/random/boot_id") as f:
            return f.read().strip()
    except OSError:
        return ""


class Link:
    """Health of the connection to the TAK server. Everything runs on one asyncio loop."""
    ping_uid = b""
    interval = 20.0
    timeout = 60.0
    seq = 0
    sent = {}                 # ping seq -> monotonic send time
    pings = 0
    pongs = 0
    pong_seen = False
    last_pong = 0.0           # monotonic
    last_pong_wall = 0.0
    rtt_ms = None
    connected_ts = 0.0        # wall clock: this connection came up
    state = "connecting"      # connecting | up | down
    reason = ""
    starts = 1                # bridge starts since the deck booted (each one = a new server connection)
    prev_reason = ""          # why the previous connection ended
    prev_down_ts = 0.0
    status_writer = None

    @classmethod
    def setup(cls, cfg):
        cls.interval = float(cfg.get("PING_INTERVAL", "20"))
        cls.timeout = max(20.0, float(cfg.get("LINK_TIMEOUT", "60")))
        LINK_SETTINGS["timeout"] = cls.timeout
        cls.ping_uid = own_uid(cfg) + b"-ping"

    @classmethod
    def ping_xml(cls, seq):
        now = time.time()
        return ('<event version="2.0" uid=%s type="t-x-c-t" how="h-g-i-g-o" time=%s start=%s stale=%s>'
                '<point lat="0" lon="0" hae="0" ce="9999999" le="9999999"/><detail><remarks>atb-seq=%d</remarks></detail></event>'
                % (quoteattr(cls.ping_uid.decode()), quoteattr(_ts(now * 1000)), quoteattr(_ts(now * 1000)),
                   quoteattr(_ts((now + 30) * 1000)), seq)).encode()

    @classmethod
    def is_pong(cls, data):
        return isinstance(data, (bytes, bytearray)) and b'type="t-x-c-t' in data and cls.ping_uid in data

    @classmethod
    def on_pong(cls, data):
        now = time.monotonic()
        m = re.search(rb"atb-seq=(\d+)", data)
        t = cls.sent.get(int(m.group(1))) if m else None
        if t is not None:
            cls.rtt_ms = round((now - t) * 1000, 1)
        if m:
            Outbox.confirm(int(m.group(1)))
        if not cls.pong_seen:
            log.info("Server answers pings: link watchdog on (reconnects after %.0f s without an answer)", cls.timeout)
        cls.pong_seen = True
        cls.pongs += 1
        cls.last_pong, cls.last_pong_wall = now, time.time()

    @classmethod
    def fail(cls, reason):
        """End this connection: record why (the dashboard shows it), then raise so the process exits."""
        cls.state, cls.reason = "down", reason
        log.warning("Server link down: %s. Exiting so systemd reconnects in 5 s.", reason)
        if cls.status_writer is not None:
            try:
                cls.status_writer.write_once()
            except Exception:                     # best effort: the process is exiting either way
                pass
        raise ConnectionResetError(reason)


class LinkWatch:
    """Ping the server every PING_INTERVAL s (OTS answers ATAK pings), measure the round trip, and
    reconnect when the answers stop (a link that died without closing). If the server never answers pings,
    the watchdog stays off and only the TCP timeout applies."""

    def __init__(self, cfg, tx_queue):
        self.q = tx_queue

    async def run(self, _=-1):
        if Link.interval <= 0:
            log.info("Server pings off (PING_INTERVAL = 0)")
            while True:
                await asyncio.sleep(3600)
        next_ping, told = time.monotonic() + 2, False
        while True:
            await asyncio.sleep(1.0)
            now = time.monotonic()
            if now >= next_ping:
                Link.seq += 1
                Link.sent[Link.seq] = now
                for k in [k for k in Link.sent if k < Link.seq - 10]:
                    del Link.sent[k]
                try:
                    self.q.put_nowait(Link.ping_xml(Link.seq))
                    Link.pings += 1
                except asyncio.QueueFull:
                    pass                          # the link is backed up; the watchdog below still counts
                next_ping = now + Link.interval
            if Link.pong_seen and now - Link.last_pong > Link.timeout:
                Link.fail("no answer from the server for %.0f s" % (now - Link.last_pong))
            if not Link.pong_seen and Link.pings >= 3 and not told:
                told = True
                log.info("The server does not answer pings: watchdog off (the %.0f s TCP timeout still applies)", Link.timeout)
                Outbox.no_pings()


def bad_event(data):
    """Why this packet is NOT one complete, readable CoT event, or None if it is fine.
    OTS reads the deck's stream as text and splits it at "</event>": bytes that are not valid UTF-8 break its reader,
    and an event that is cut off glues onto the next one, so that good event is lost too. Checking here means a bad
    packet is dropped alone, at the deck. Good packets are forwarded byte for byte, unchanged."""
    if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        return "contains a DOCTYPE"               # ATAK never sends one; refuse rather than parse it
    try:
        root = ET.fromstring(bytes(data).decode("utf-8"))
    except UnicodeDecodeError:
        return "not valid UTF-8 text"
    except (ET.ParseError, ValueError):
        return "not complete XML (cut off or broken)"
    if root.tag != "event":
        return "not a CoT <event>"
    return None


def split_events(data):
    """Cut a packet the way OTS cuts its stream, at each "</event>". Returns (good events, problem or None).

    A clean single event is returned unchanged. Junk after the last event is dropped, several events are split, and a
    cut-off event in front of a good one is removed. Only the broken pieces are lost, never a good event.
    """
    why = bad_event(data)
    if why is None:
        return [data], None
    if why == "contains a DOCTYPE":
        return [], why
    good, bad, end, start = [], None, b"</event>", 0
    data = bytes(data)
    while True:
        i = data.find(end, start)
        if i < 0:
            break
        piece = data[start:i + len(end)].strip(b" \t\r\n\x00")
        start = i + len(end)
        w = bad_event(piece)
        if w is not None:                 # a cut-off event glued onto a good one: keep the good one (its own "<event")
            k = piece.rfind(b"<event", 1)
            if k > 0 and bad_event(piece[k:]) is None:
                good.append(piece[k:])
            bad = bad or w
        else:
            good.append(piece)
    tail = data[start:].strip(b" \t\r\n\x00")
    if not good:
        return [], why
    if bad is None and tail:
        bad = "junk after the event (cut off)"
    elif bad is None:
        bad = "padding after the event (cut off)" if len(good) == 1 else None
    return good, bad


class Outbox:
    """Chats/DMs bound for the server, kept on disk (fsync'd) until confirmed.

    Confirmed = a ping written after the chat was answered (TCP keeps order). With pings off or never answered, a
    written chat counts as "sent, unconfirmed" and Ops says so. After a restart, anything unconfirmed is resent oldest
    first; ATAK drops duplicates. A failed disk write is reported on Ops, and the chat is still sent.
    File: OUTBOX_FILE, one JSON line per chat and per delivery. Limits: OUTBOX_HOURS (12), OUTBOX_MAX (5000).
    """
    path = None
    hours = 12.0
    maxn = 5000
    pending = {}        # id -> (wall time, bytes)
    by_data = {}        # bytes -> id
    written = {}        # id -> ping seq that must be answered to confirm it (None: no ping written since)
    seq = 0
    lines = 0
    loaded = replayed = expired = 0
    confirmed = unconfirmed_done = 0    # delivered with a ping answer / written only (pings off or never answered)
    write_errors = 0
    last_write_error = ""

    @classmethod
    def setup(cls, cfg):
        st = cfg.get("STATUS_FILE", "~/bridge_status.json").strip()
        default = "off" if st.lower() in ("off", "0", "no", "") else \
            os.path.join(os.path.dirname(os.path.expanduser(st)), "bridge_outbox.jsonl")
        p = cfg.get("OUTBOX_FILE", default).strip()
        cls.path = None if p.lower() in ("off", "0", "no", "") else os.path.expanduser(p)
        cls.hours = max(0.1, float(cfg.get("OUTBOX_HOURS", "12")))
        cls.maxn = max(10, int(cfg.get("OUTBOX_MAX", "5000")))
        cls.pending, cls.by_data, cls.written, cls.seq, cls.lines = {}, {}, {}, 0, 0
        cls.loaded = cls.replayed = cls.expired = 0
        cls.replay_done = set()
        cls.confirmed = cls.unconfirmed_done = cls.write_errors = 0
        cls.last_write_error = ""
        if cls.path is None:
            log.info("Outbox OFF (OUTBOX_FILE = off): chats are not kept across a lost link")
            return
        cls._load()

    @classmethod
    def _load(cls):
        recs, done = {}, set()
        try:
            with open(cls.path, "rb") as f:
                for raw in f:
                    try:
                        r = json.loads(raw)
                    except ValueError:
                        continue
                    if "ok" in r:
                        done.add(r["ok"])
                    elif "i" in r and "d" in r:
                        try:
                            recs[int(r["i"])] = (float(r.get("t", 0)), base64.b64decode(r["d"]))
                        except (ValueError, TypeError):
                            continue
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.warning("Outbox: cannot read %s (%s); starting empty", cls.path, exc)
        now = time.time()
        live = {i: v for i, v in recs.items() if i not in done}
        old = [i for i, (t, _) in live.items() if now - t > cls.hours * 3600]
        for i in old:
            del live[i]
        cls.expired = len(old)
        newest = sorted(live, key=lambda i: live[i][0])[-cls.maxn:]
        cls.expired += len(live) - len(newest)
        cls.pending = {i: live[i] for i in newest}
        cls.by_data = {d: i for i, (t, d) in cls.pending.items()}
        cls.seq = max(list(recs) + [0])
        cls.loaded = len(cls.pending)
        cls._compact()
        if cls.loaded or cls.expired:
            log.info("Outbox: %d chat(s) still waiting for the server from the last run%s", cls.loaded,
                     (", %d too old (over %.0f h) dropped" % (cls.expired, cls.hours)) if cls.expired else "")

    @classmethod
    def _compact(cls):
        try:
            tmp = cls.path + ".tmp"
            with open(tmp, "w") as f:
                for i in sorted(cls.pending, key=lambda i: cls.pending[i][0]):
                    t, d = cls.pending[i]
                    f.write(json.dumps({"i": i, "t": t, "d": base64.b64encode(d).decode()}) + "\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, cls.path)
            cls._sync_dir()
            cls.lines = len(cls.pending)
        except OSError as exc:
            cls._write_failed(exc)

    @classmethod
    def _sync_dir(cls):
        """After a rename, sync the folder too, so the new name survives a power cut."""
        try:
            fd = os.open(os.path.dirname(os.path.abspath(cls.path)), os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            pass

    @classmethod
    def _write_failed(cls, exc):
        cls.write_errors += 1
        cls.last_write_error = str(exc)[:200]
        log.warning("Outbox: cannot write %s (%s)", cls.path, exc)

    @classmethod
    def _append(cls, rec, sync=False):
        """Add one line. sync=True (a new chat) waits until it is on the disk itself, not just in memory."""
        try:
            with open(cls.path, "a") as f:
                f.write(json.dumps(rec) + "\n")
                if sync:
                    f.flush()
                    os.fsync(f.fileno())
            cls.lines += 1
            if cls.lines > 4 * cls.maxn:
                cls._compact()
            return True
        except OSError as exc:
            cls._write_failed(exc)
            return False

    @classmethod
    def keep(cls, data):
        """Remember a chat/DM that is about to be queued for the server. Anything else is ignored."""
        if cls.path is None or b'type="b-t-f' not in data:
            return
        d = bytes(data)
        if d in cls.by_data:
            return
        cls.seq += 1
        cls.pending[cls.seq] = (time.time(), d)
        cls.by_data[d] = cls.seq
        cls._append({"i": cls.seq, "t": time.time(), "d": base64.b64encode(d).decode()}, sync=True)
        if len(cls.pending) > cls.maxn:           # too many waiting: forget the oldest
            i = min(cls.pending, key=lambda k: cls.pending[k][0])
            cls.by_data.pop(cls.pending.pop(i)[1], None)
            cls.expired += 1

    @classmethod
    def expire(cls):
        """While running too, not only at start: drop chats that waited over OUTBOX_HOURS for the server."""
        if cls.path is None or not cls.pending:
            return
        cut = time.time() - cls.hours * 3600
        old = [i for i, (t, _) in cls.pending.items() if t < cut]
        for i in old:
            cls.by_data.pop(cls.pending.pop(i)[1], None)
            cls.written.pop(i, None)
        if old:
            cls.expired += len(old)
            cls._compact()
            log.info("Outbox: %d chat(s) waited over %.0f h for the server; dropped", len(old), cls.hours)

    @classmethod
    def _done(cls, i, confirmed=True):
        t_d = cls.pending.pop(i, None)
        if t_d is not None:
            cls.by_data.pop(t_d[1], None)
            if confirmed:
                cls.confirmed += 1
            else:
                cls.unconfirmed_done += 1
        cls.written.pop(i, None)
        cls._append({"ok": i})

    @classmethod
    def sent(cls, data):
        """Called for everything written to the server socket (see _send_tuned)."""
        if cls.path is None or not isinstance(data, (bytes, bytearray)):
            return
        if Link.ping_uid and b"atb-seq=" in data and Link.ping_uid in data:
            m = re.search(rb"atb-seq=(\d+)", data)           # one of our pings went out after these chats
            if m:
                seq = int(m.group(1))
                for i, wm in cls.written.items():
                    if wm is None:
                        cls.written[i] = seq
            return
        i = cls.by_data.get(bytes(data))
        if i is None:
            return
        if Link.interval <= 0 or (not Link.pong_seen and Link.pings >= 3):
            cls._done(i, confirmed=False)         # no pings to confirm with: written is the best we know
        else:
            cls.written[i] = None

    @classmethod
    def no_pings(cls):
        """The server never answers pings: nothing can confirm more than "written", so count what was written."""
        for i in list(cls.written):
            cls._done(i, confirmed=False)

    @classmethod
    def confirm(cls, seq):
        """A ping was answered: every chat written before that ping has reached the server."""
        for i in [i for i, wm in cls.written.items() if wm is not None and wm <= seq]:
            cls._done(i)

    @classmethod
    async def replay(cls, tx_queue):
        """At start: queue what was still waiting, oldest first (after the identity, so OTS knows who sends).
        Runs as its own task (OutboxReplay), never before the sender starts: PyTAK's send queue holds 100, so
        putting more than that before anything drains it would wait forever (the bridge used to hang at start that way)."""
        if not cls.pending:
            return
        done = getattr(cls, "replay_done", None)
        if done is None:
            done = cls.replay_done = set()
        for i in sorted(cls.pending, key=lambda k: cls.pending[k][0]):
            if i in done:                         # already queued by an earlier try: never sent twice
                continue
            await tx_queue.put(cls.pending[i][1])
            done.add(i)
            cls.replayed += 1
        log.info("Outbox: %d waiting chat(s) sent again", cls.replayed)

    @classmethod
    def status(cls):
        if cls.path is None:
            return {"enabled": False}
        waiting = [t for i, (t, _) in cls.pending.items() if i not in cls.written]
        unconf = [cls.pending[i][0] for i in cls.written if i in cls.pending]
        return {"enabled": True, "pending": len(cls.pending), "waiting": len(waiting), "unconfirmed": len(unconf),
                "oldest_ts": min(waiting + unconf, default=None), "oldest_waiting_ts": min(waiting, default=None),
                "replayed": cls.replayed, "expired": cls.expired, "file": cls.path,
                "confirmed": cls.confirmed, "sent_unconfirmed": cls.unconfirmed_done,
                "can_confirm": not (Link.interval <= 0 or (not Link.pong_seen and Link.pings >= 3)),
                "write_errors": cls.write_errors, "last_write_error": cls.last_write_error}


class OutboxReplay:
    """Sends the outbox's waiting chats once the sender is running. Never returns (PyTAK stops everything when a
    task ends)."""

    def __init__(self, tx_queue):
        self.tx_queue = tx_queue

    RETRY_FIRST, RETRY_MAX = 5.0, 300.0

    async def run(self, _=-1):
        wait = self.RETRY_FIRST
        while True:                               # until every saved chat is queued; then sleep forever
            try:
                await Outbox.replay(self.tx_queue)
                if Faults.areas.get("outbox_replay", {}).get("sticky"):
                    log.info("Outbox: replay worked on a later try")
                Faults.clear("outbox_replay")
                break
            except Exception as exc:
                Faults.note("outbox_replay", exc, sticky=True)
                await asyncio.sleep(wait)
                wait = min(self.RETRY_MAX, wait * 2)
        while True:
            await asyncio.sleep(3600)


class DMSelfTest:
    """Proves the server half of the DM path works.

    Every DM_SELFTEST_INTERVAL s, sends a tiny DM to a made-up callsign. OTS routes it like any DM, mesh_mailroute
    on the server puts it in the deck's mailbox, and OTS sends it back down this connection. OTS never echoes a
    connection's own traffic, so an answer proves the helper is alive. No answer in two intervals = broken.
    """
    callsign = ""
    uid = b""
    interval = 60.0
    state = "off"             # off | checking | ok | broken
    since = 0.0               # wall clock: this state began
    sent = returned = 0
    seq = 0
    out = {}                  # seq -> monotonic send time
    rtt_ms = None
    last_ok_ts = 0.0

    @classmethod
    def setup(cls, cfg):
        cs = cfg.get("DM_SELFTEST", "DECK-selftest").strip()
        on = dm_relay_on(cfg) and cs.lower() not in ("off", "0", "no", "")
        cls.callsign = cs if on else ""
        cls.uid = own_uid(cfg) + b"-selftest"
        cls.interval = max(15.0, float(cfg.get("DM_SELFTEST_INTERVAL", "60")))
        cls.state, cls.since = ("checking", time.time()) if on else ("off", 0.0)
        cls.out, cls.seq, cls.sent, cls.returned, cls.rtt_ms, cls.last_ok_ts = {}, 0, 0, 0, None, 0.0

    @classmethod
    def xml(cls, seq):
        now = time.time()
        return ('<event version="2.0" uid=%s type="t-x-atb-st" how="h-g-i-g-o" time=%s start=%s stale=%s>'
                '<point lat="0" lon="0" hae="0" ce="9999999" le="9999999"/><detail><marti><dest callsign=%s/></marti>'
                '<remarks>atb-st=%d</remarks></detail></event>'
                % (quoteattr(cls.uid.decode()), quoteattr(_ts(now * 1000)), quoteattr(_ts(now * 1000)),
                   quoteattr(_ts((now + 120) * 1000)), quoteattr(cls.callsign), seq)).encode()

    @classmethod
    def is_mine(cls, data):
        return bool(cls.callsign) and isinstance(data, (bytes, bytearray)) and b"atb-st=" in data and cls.uid in data

    @classmethod
    def on_return(cls, data):
        m = re.search(rb"atb-st=(\d+)", data)
        t0 = cls.out.pop(int(m.group(1)), None) if m else None
        cls.returned += 1
        cls.last_ok_ts = time.time()
        if t0 is not None:
            cls.rtt_ms = round((time.monotonic() - t0) * 1000, 1)
        if cls.state != "ok":
            log.info("DM route OK: a message to %s came back through the server (%s ms)%s", cls.callsign,
                     cls.rtt_ms, " after being broken" if cls.state == "broken" else "")
            cls.state, cls.since = "ok", time.time()

    @classmethod
    def status(cls):
        return {"state": cls.state, "callsign": cls.callsign, "since": cls.since, "rtt_ms": cls.rtt_ms,
                "last_ok_ts": cls.last_ok_ts, "sent": cls.sent, "returned": cls.returned, "interval": cls.interval}

    def __init__(self, cfg, tx_queue):
        self.tx_queue = tx_queue

    async def run(self, _=-1):
        cls = DMSelfTest
        if not cls.callsign:
            return await asyncio.sleep(1e9)       # never return: PyTAK stops everything when a task ends
        log.info("DM route check ON: a message to %s every %.0f s must come back through the server",
                 cls.callsign, cls.interval)
        while True:
            await asyncio.sleep(cls.interval)
            try:
                if Link.state != "up":
                    continue
                now = time.monotonic()
                overdue = [q for q, t0 in cls.out.items() if now - t0 > 2 * cls.interval]
                if overdue and cls.state != "broken":
                    log.warning("DM route BROKEN: %d message(s) to %s did not come back in %.0f s. Server -> mesh "
                                "DMs are not being delivered. Is mesh_mailroute v2 running on the server?",
                                len(overdue), cls.callsign, 2 * cls.interval)
                    cls.state, cls.since = "broken", time.time()
                for q in [q for q, t0 in cls.out.items() if now - t0 > 10 * cls.interval]:
                    del cls.out[q]
                cls.seq += 1
                cls.out[cls.seq] = time.monotonic()
                cls.sent += 1
                await self.tx_queue.put(cls.xml(cls.seq))
            except Exception as exc:
                Faults.note("dm_check", exc, hold=2.5 * cls.interval)


class Uplink:
    """What goes up to the server, in order.

    Chats, markers and alerts go first. Positions are kept newest-per-phone and sent at most once per UP_MIN_INTERVAL
    per phone, so a slow link sends fresh positions instead of a backlog. O(log n) per report: `ready` in arrival
    order, `cooling` a heap by the time each phone may send again.
    """

    def __init__(self, cfg, tx_queue):
        self.q = tx_queue
        self.min_interval = float(cfg.get("UP_MIN_INTERVAL", "3"))
        self.low = 4
        self.prio = deque()
        self.pend = {}            # uid -> newest report
        self.ready = OrderedDict()   # uid -> None, may send now, oldest first
        self.cooling = []         # heap of (monotonic when it may send, uid)
        self.cool_set = set()
        self.last = {}            # uid -> monotonic it was last sent
        self.wake = asyncio.Event()

    def put(self, data, uid, etype):
        if self.min_interval > 0 and uid and etype.startswith(b"a-") and b'endpoint="' in data:
            if uid in self.pend:
                Stats.up_coalesced += 1   # still waiting: just keep the newer one
            elif len(self.pend) >= MAX_UIDS:
                Stats.up_dropped += 1     # a flood of made-up UIDs: phones already waiting keep their place
                Stats.uid_capped += 1
                return
            elif uid not in self.cool_set:
                due = self.last.get(uid, -1e9) + self.min_interval
                if due <= time.monotonic():
                    self.ready[uid] = None
                else:
                    heapq.heappush(self.cooling, (due, uid))
                    self.cool_set.add(uid)
            self.pend[uid] = data
        else:
            self.prio.append(data)
            if len(self.prio) > 2000:
                self.prio.popleft()
                Stats.up_dropped += 1
        self.wake.set()

    def _next(self):
        if self.prio:
            return self.prio.popleft()
        now = time.monotonic()
        while self.cooling and self.cooling[0][0] <= now:
            _, uid = heapq.heappop(self.cooling)
            self.cool_set.discard(uid)
            if uid in self.pend:
                self.ready[uid] = None
        while self.ready:
            uid, _ = self.ready.popitem(last=False)
            data = self.pend.pop(uid, None)
            if data is None:
                continue
            self.last[uid] = now
            if len(self.last) > 4096 and (len(self.last) > MAX_UIDS or now - getattr(self, "_swept", 0) >= 1.0):
                self._swept = now
                for k in [k for k, v in self.last.items() if now - v > 600 and k not in self.pend]:
                    del self.last[k]
                cap_oldest(self.last, MAX_UIDS)
            return data
        return None

    async def run(self, _=-1):
        while True:
            try:
                if self.q.qsize() >= self.low:
                    await asyncio.sleep(0.02)     # the server link is busy: keep holding the newest here
                    continue
                item = self._next()
                if item is not None:
                    self.q.put_nowait(item)
                    continue
                self.wake.clear()
                timeout = 0.25
                if self.cooling:
                    timeout = max(0.01, min(timeout, self.cooling[0][0] - time.monotonic()))
                try:
                    await asyncio.wait_for(self.wake.wait(), timeout)
                except asyncio.TimeoutError:
                    pass
            except Exception as exc:              # never end: PyTAK stops everything when a task ends
                Faults.note("uplink", exc)
                await asyncio.sleep(1)

    def waiting(self):
        return len(self.prio) + len(self.pend)


DEFAULT_MESH_URL = "udp+ro://239.2.3.1:6969"
DEFAULT_CHAT_URL = "udp+ro://224.10.10.1:17012"
CHAT_PREFIX = b"b-t-f"


UID_RE = re.compile(rb'<event[^<>]*?\suid="([^"]+)"')
TYPE_RE = re.compile(rb'<event[^<>]*?\stype="([^"]+)"')
TIME_RE = re.compile(rb'<event[^<>]*?\stime="([^"]+)"')


def classify(etype):
    """Bucket a CoT type for the status counters."""
    if etype.startswith(CHAT_PREFIX):
        return "chat"
    if etype.startswith(b"a-"):
        return "position"
    if etype.startswith((b"b-m-p", b"u-d-")):
        return "marker"
    return "other"


class Faults:
    """Errors a loop survives, kept where Ops can see them (not only in journalctl): per area, how many, the last one
    and when. "Active" = happened in the last ACTIVE_S seconds, or a sticky one not yet cleared. The log gets the first
    three of each area and then every 100th, so a fault repeating every second can't flood the journal."""
    ACTIVE_S = 60.0
    areas = {}

    @classmethod
    def note(cls, area, exc, sticky=False, hold=None):
        """hold: seconds it stays active after this error (a loop that runs every few minutes needs longer)."""
        f = cls.areas.setdefault(area, {"count": 0, "last": "", "last_ts": 0.0, "sticky": False, "hold": cls.ACTIVE_S})
        f["hold"] = max(cls.ACTIVE_S, hold or 0)
        f["count"] += 1
        f["last"] = ("%s: %s" % (type(exc).__name__, exc))[:200]
        f["last_ts"] = time.time()
        f["sticky"] = f["sticky"] or sticky
        if f["count"] <= 3 or f["count"] % 100 == 0:
            log.warning("%s: %s (%d so far)", area, f["last"], f["count"])

    @classmethod
    def clear(cls, area):
        """It works now: no longer active (the count and the last error stay for the record)."""
        if area in cls.areas:
            cls.areas[area]["sticky"] = False
            cls.areas[area]["hold"] = 0.0

    @classmethod
    def status(cls, now=None):
        now = time.time() if now is None else now
        return {a: {"count": f["count"], "last": f["last"], "last_ts": f["last_ts"],
                    "active": f["sticky"] or now - f["last_ts"] < f.get("hold", cls.ACTIVE_S)} for a, f in cls.areas.items()}


class Stats:
    """Counters for the dashboard status file. Everything runs on one asyncio loop,
    so plain class attributes are safe."""
    started = time.time()
    mesh_events = 0
    mesh_last_ts = 0.0
    mesh_last_from = ""
    mesh_skipped = 0
    mesh_failed = 0
    mesh_control = 0          # TAK Protocol announcements (no CoT inside): dropped on purpose, not "skipped"
    mesh_repaired = 0         # packets with padding/junk cut off or several events split (good events kept)
    mesh_malformed = 0        # packets that were not one complete, readable event: dropped at the deck
    mesh_ready = False        # the deck's mesh address exists and the mesh listener is joined
    mesh_wait_since = 0.0     # wall clock when the bridge started waiting for the mesh (0 = not waiting)
    return_enabled = False
    return_sent = 0
    return_last_ts = 0.0
    server_rx = 0
    server_last_rx_ts = 0.0
    up_coalesced = 0          # position reports replaced by a newer one before they were sent
    up_dropped = 0
    uid_capped = 0            # entries dropped from per-phone tables at the MAX_UIDS ceiling (a UID flood)
    return_coalesced = 0      # server positions replaced by a newer one before going onto the mesh
    return_dropped = 0
    recent = deque()            # (monotonic time, "up"|"down", category) for the last 60 s

    @classmethod
    def note(cls, direction, cat):
        now = time.monotonic()
        cls.recent.append((now, direction, cat))
        cls._trim(now)

    @classmethod
    def _trim(cls, now):
        while cls.recent and now - cls.recent[0][0] > 60.0:
            cls.recent.popleft()

    @classmethod
    def per_minute(cls):
        cls._trim(time.monotonic())
        out = {"up": {}, "down": {}}
        for _, direction, cat in cls.recent:
            out[direction][cat] = out[direction].get(cat, 0) + 1
        for d in ("up", "down"):
            out[d]["total"] = sum(out[d].values())
        out["total"] = out["up"]["total"] + out["down"]["total"]
        return out


MAX_UIDS = 10000        # hard ceiling on per-phone tables: a device making up a new UID per packet can't eat the RAM


def cap_oldest(table, maxn, when=lambda v: v):
    """Keep `table` (key -> time, or a value `when` reads a time from) at most maxn entries, dropping the oldest.
    Returns how many were dropped."""
    extra = len(table) - maxn
    if extra <= 0:
        return 0
    extra += maxn // 10                   # make room for a while, so this doesn't run on every new UID
    for k, _ in heapq.nsmallest(extra, table.items(), key=lambda kv: when(kv[1])):
        del table[k]
    Stats.uid_capped += extra
    return extra


def cot_time(data):
    """An event's time="..." as UTC seconds, or None. Accepts 2026-11-07T15:00:00Z, with or without fractions."""
    m = TIME_RE.search(data)
    if not m:
        return None
    s = m.group(1).decode(errors="replace").strip()
    mm = re.match(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(\.\d+)?Z$", s)
    if not mm:
        return None
    try:
        dt = datetime(*[int(x) for x in mm.groups()[:6]], tzinfo=timezone.utc)
    except ValueError:
        return None
    return dt.timestamp() + (float(mm.group(7)) if mm.group(7) else 0.0)


class Shared:
    """State shared by both directions (loop prevention)."""
    mesh_uids = {}      # uid -> last time seen ON THE MESH (never send back to the mesh)
    mesh_times = {}     # uid -> newest report time (UTC seconds) heard from it ON THE MESH
    injected_uids = {}  # uid -> last time WE put it on the mesh (fallback for events with no time stamp)
    server_copies = {}  # (uid, time stamp) -> when the server sent us that exact report
    TTL = 600.0

    @classmethod
    def saw_server(cls, data, uid):
        """Remember a report the server sent us, by its UID and creation time."""
        m = TIME_RE.search(data)
        if m:
            cls.mark(cls.server_copies, (uid, m.group(1)))

    @classmethod
    def is_echo(cls, data, uid):
        """A report heard on the mesh that is a copy of one the server already sent us (put back on the mesh by
        this deck or another one) - not a phone talking. A phone's own new report has a new time stamp, so a phone
        that just moved from the server to the mesh is forwarded right away."""
        m = TIME_RE.search(data)
        if m is None:
            return cls.recent(cls.injected_uids, uid)
        return cls.recent(cls.server_copies, (uid, m.group(1)))
    _swept = {}         # id(table) -> when it was last cleaned up

    @classmethod
    def mark(cls, table, uid):
        now = time.monotonic()
        table[uid] = now
        if len(table) > 4096 and (len(table) > MAX_UIDS or now - cls._swept.get(id(table), 0) >= 1.0):
            cls._swept[id(table)] = now           # at most once a second (each sweep looks at every entry)
            for k in [k for k, v in table.items() if now - v > cls.TTL]:
                del table[k]
            cap_oldest(table, MAX_UIDS)

    @classmethod
    def recent(cls, table, uid):
        t = table.get(uid)
        return t is not None and (time.monotonic() - t) < cls.TTL

    @classmethod
    def saw_mesh(cls, data, uid):
        """A report heard on the mesh: remember the phone, and the newest report time it sent there."""
        cls.mark(cls.mesh_uids, uid)
        t = cot_time(data)
        if t is not None and t > cls.mesh_times.get(uid, 0.0):
            cls.mesh_times[uid] = t
            if len(cls.mesh_times) > MAX_UIDS:
                cls.mesh_times.clear()

    @classmethod
    def from_mesh(cls, data, uid):
        """True for a server report that is only the mesh's own traffic coming back (never sent to the mesh again).
        A report NEWER than anything that phone sent on the mesh is the phone itself, now on the server (it moved
        over), and goes to the mesh at once; before, it waited out the 10 min loop guard, so mesh phones kept its old
        mesh address and their DMs and files to it went nowhere."""
        if not cls.recent(cls.mesh_uids, uid):
            return False
        t, last = cot_time(data), cls.mesh_times.get(uid)
        return t is None or last is None or t <= last


class LocalCopy:
    """Copies of chat messages for deck_chat.py on this deck (LOCAL_CHAT_COPY). Never raises."""
    dest = None
    sock = None
    sent = 0

    @classmethod
    def setup(cls, cfg):
        v = cfg.get("LOCAL_CHAT_COPY", "off").strip()
        if v.lower() in ("", "off", "0", "no"):
            return
        u = urlparse(v)
        if u.hostname not in ("127.0.0.1", "localhost") or not u.port:
            log.warning("LOCAL_CHAT_COPY must be udp://127.0.0.1:<port>; ignoring %r", v)
            return
        cls.dest = ("127.0.0.1", u.port)
        cls.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        log.info("Chat copies for deck chat -> %s:%d", *cls.dest)

    @classmethod
    def send(cls, src, data):
        if cls.dest is None:
            return
        try:
            cls.sock.sendto(b"src=" + src + b"\n" + bytes(data), cls.dest)
            cls.sent += 1
        except OSError:
            pass                                  # deck chat not running: nothing to do


class LocalSend:
    """LOCAL_SEND = udp://127.0.0.1:<port>: Chat hands CoT (direct messages to people who are
    only on the server) to the bridge, which sends it to the server ONLY, never onto the mesh.
    Accepts datagrams from 127.0.0.1 only. Default off."""

    def __init__(self, cfg, tx_queue):
        self.cfg = cfg
        self.tx_queue = tx_queue

    async def run(self, _=-1):
        u = urlparse(self.cfg.get("LOCAL_SEND", "off"))
        q = self.tx_queue

        class Proto(asyncio.DatagramProtocol):
            def datagram_received(self, data, addr):
                if addr[0] != "127.0.0.1" or b"<event" not in data or len(data) > 65000:
                    return
                try:
                    mu = UID_RE.search(data)
                    if mu:                    # server only: never relayed back onto the mesh when the server echoes it
                        Shared.mark(Shared.mesh_uids, mu.group(1))
                    Outbox.keep(data)
                    q.put_nowait(data)
                    Stats.note("up", classify(TYPE_RE.search(data).group(1)) if TYPE_RE.search(data) else "other")
                except asyncio.QueueFull:
                    log.warning("LOCAL_SEND: server queue full, dropped one message")

        loop = asyncio.get_running_loop()
        await loop.create_datagram_endpoint(Proto, local_addr=("127.0.0.1", u.port))
        log.info("Accepting server-only messages from Chat on 127.0.0.1:%d", u.port)
        while True:
            await asyncio.sleep(3600)


def own_uid(cfg):
    """The bridge/deck UID (same default as Chat: DECK-<hostname>)."""
    return UID_RE.search(bridge_identity(cfg)).group(1)


# ---- shared code: GPS/MGRS, team icon, the [deck] config section, suite version -> ../common/deckcommon.py
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "common"))
from deckcommon import (GpsWatch, to_mgrs, latlon_to_utm, TEAMS, ROLES, team_xml,  # noqa: E402,F401
                        apply_deck, suite_version, iface_ipv4, mesh_iface, is_auto, watch_mesh_addr)


class MeshAddr:
    """[deck] MESH_ADDR = auto: the bridge uses the mesh port's current address. If the node hands out a new
    one (a node reboot can), the bridge restarts itself and systemd starts it again on the new address.
    No address yet (node unplugged or off) = server-only, the same as a fixed address that isn't there."""
    auto = False
    missing = False
    iface = "eth0"

    @classmethod
    def setup(cls, parser, cfg):
        cls.iface = mesh_iface(parser, "bridge")
        if not is_auto(cfg.get("PYTAK_MULTICAST_LOCAL_ADDR", "")):
            return
        cls.auto = True
        ip = iface_ipv4(cls.iface)
        if ip:
            log.info("Mesh address auto: %s on %s (followed if the node hands out a new one)", ip, cls.iface)
        else:
            log.warning("Mesh address auto: %s has no address yet (node unplugged or off?). Server-only until it has one",
                        cls.iface)
        cls.missing = ip is None
        cfg["PYTAK_MULTICAST_LOCAL_ADDR"] = ip or "127.0.0.1"   # placeholder: the mesh readers wait while missing

        def changed(old, new):
            log.warning("Mesh address on %s is now %s (was %s): restarting the bridge to follow it",
                        cls.iface, new, old or "none")
            logging.shutdown()
            os._exit(3)                           # systemd restarts it; the outbox is already on disk
        watch_mesh_addr(cls.iface, ip, changed)



# ---- the deck's own health, in its position report (DECK_STATUS) -------------------------------------
# Server users tap DECK on the map and see how the deck is doing. TAK has no standard field for temperature or
# signal, so the readings go in <remarks>, which every TAK client shows. Each reading is skipped if it can't be read.
THERMAL = "/sys/class/thermal/thermal_zone0/temp"
PROC_ROUTE = "/proc/net/route"
PROC_WIRELESS = "/proc/net/wireless"
PROC_UPTIME = "/proc/uptime"


def _read(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return ""


def wan_status(mesh_if=""):
    """'WAN wlan0 -61 dBm' (Wi-Fi), 'WAN eth1' (wired or modem), or '' when there is no default route."""
    iface = ""
    for line in _read(PROC_ROUTE).splitlines()[1:]:
        f = line.split()
        if len(f) > 7 and f[1] == "00000000" and f[7] == "00000000" and f[0] != mesh_if:
            iface = f[0]
            break
    if not iface:
        return ""
    for line in _read(PROC_WIRELESS).splitlines()[2:]:
        name, _, rest = line.partition(":")
        if name.strip() == iface:
            f = rest.split()
            try:
                level = float(f[2])
                return "WAN %s %d dBm" % (iface, round(level if level < 0 else level - 256))
            except (IndexError, ValueError):
                return "WAN %s" % iface
    return "WAN %s" % iface


def mesh_summary(cfg):
    """'mesh 2 nodes, slowest 7.1 Mbit/s' from the dashboard's mesh summary file (Ops writes it; no file, no summary)."""
    path = os.path.expanduser(cfg.get("MESH_STATUS_FILE", "~/mesh_status.json").strip() or "off")
    if path == "off":
        return ""
    try:
        with open(path) as f:
            m = json.load(f)
        if time.time() - float(m.get("now", 0)) > 120 or int(m.get("nodes_total", 0)) < 1:
            return ""
        up, total = int(m.get("nodes_up", 0)), int(m["nodes_total"])
        out = "mesh %d node%s" % (total, "" if total == 1 else "s") if up == total else "mesh %d of %d nodes" % (up, total)
        if m.get("slowest_mbps") is not None:
            out += ", slowest %.1f Mbit/s" % float(m["slowest_mbps"])
        return out
    except (OSError, ValueError, TypeError, KeyError):
        return ""


def deck_status(cfg):
    """One line of deck health for <remarks>, or '' when DECK_STATUS = off."""
    if cfg.get("DECK_STATUS", "on").strip().lower() in ("0", "off", "no", "false"):
        return ""
    parts = []
    try:
        parts.append("CPU %.0fC" % (int(_read(THERMAL).strip()) / 1000.0))
    except ValueError:
        pass
    wan = wan_status(MeshAddr.iface)
    if wan:
        parts.append(wan)
    parts.append(mesh_summary(cfg) or ("mesh OK" if Stats.mesh_ready else "mesh not joined (server-only)"))
    try:
        up = int(float(_read(PROC_UPTIME).split()[0]))
        parts.append("up %dh%02dm" % (up // 3600, up % 3600 // 60))
    except (IndexError, ValueError):
        pass
    parts.append("TAK MESH TOC %s" % suite_version())
    return " | ".join(parts)


def bridge_identity(cfg, now=None, pos=None, live=False, status=""):
    """The bridge's own CoT: tells OpenTAKServer who this connection is (see IDENTIFY above).
    With a GPS position (GPS = gpsd) DECK shows on server phones' maps: live while the fix is fresh,
    sent already stale at the last known spot when the fix is lost (phones grey it out)."""
    now = time.time() if now is None else now
    host = re.sub(r"[^A-Za-z0-9-]", "", socket.gethostname() or "deck") or "deck"
    uid = cfg.get("BRIDGE_UID", "").strip() or "DECK-" + host
    callsign = cfg.get("BRIDGE_CALLSIGN", "").strip() or "DECK"
    visible = cfg.get("BRIDGE_VISIBLE", "0").strip().lower() in ("1", "true", "yes", "on")
    interval = float(cfg.get("IDENTIFY_INTERVAL", "300"))
    stale = now + 2 * interval if visible else now
    try:
        lat, lon = float(cfg.get("BRIDGE_LAT", "0")), float(cfg.get("BRIDGE_LON", "0"))
    except ValueError:
        lat, lon = 0.0, 0.0
    etype = cfg.get("BRIDGE_TYPE", "a-f-G-U-C").strip() or "a-f-G-U-C"
    how, hae, ce = "h-e", 9999999.0, 9999999.0
    if pos:
        lat, lon, how = pos["lat"], pos["lon"], "m-g"
        hae = pos["alt"] if pos.get("alt") is not None else 9999999.0
        ce = pos["eph"] if pos.get("eph") is not None else 9999999.0
        stale = now + 3 * float(cfg.get("GPS_INTERVAL", "15")) if live else now
    xml = ('<event version="2.0" uid=%s type=%s how=%s time=%s start=%s stale=%s>'
           '<point lat="%s" lon="%s" hae="%s" ce="%s" le="9999999.0"/>'
           '<detail><contact callsign=%s endpoint="*:-1:stcp"/>%s'
           '<takv device="deck" platform="TAK MESH TOC" os="Linux" version="2"/>%s'
           '</detail></event>') % (
        quoteattr(uid), quoteattr(etype), quoteattr(how), quoteattr(_ts(now * 1000)), quoteattr(_ts(now * 1000)),
        quoteattr(_ts(stale * 1000)), lat, lon, hae, ce, quoteattr(callsign), team_xml(cfg.get("BRIDGE_TEAM", ""), cfg.get("BRIDGE_ROLE", "")),
        "<remarks>%s</remarks>" % xml_escape(status) if status else "")
    return xml.encode()


CALL_RE = re.compile(rb'<contact\b[^<>]*?\scallsign="([^"]+)"')
EPV_RE = re.compile(rb'<contact\b[^<>]*?\sendpoint="([^"]*)"')
EP_RE = re.compile(rb'(<contact\b[^<>]*?\sendpoint=")([^"]*)(")')
CONTACT_RE = re.compile(rb'<contact\b')
DEST_CS_RE = re.compile(rb'<dest\b[^<>]*?\scallsign="([^"]+)"')
DEST_UID_RE = re.compile(rb'<dest\b[^<>]*?\suid="([^"]+)"')
CHATROOM_RE = re.compile(rb'<__chat\b[^<>]*?\schatroom="([^"]+)"')
UID1_RE = re.compile(rb'<chatgrp\b[^<>]*?\suid1="([^"]+)"')
EVENT_UID_RE = re.compile(rb'<event\b[^<>]*?\suid="([^"]+)"')
MESH_EP_RE = re.compile(rb"^(\d+\.\d+\.\d+\.\d+):(\d+):(tcp|udp)$")
POINT_RE = re.compile(rb'<point\b[^<>]*?\slat="(-?[0-9.]+)"[^<>]*?\slon="(-?[0-9.]+)"')


class Peers:
    """Who is where, from position reports: mesh phones (with their DM endpoint) and server people."""
    mesh = {}        # callsign.lower() -> {"cs", "uid", "ip", "port", "proto", "ts"}
    mesh_uid = {}    # uid -> same dict
    server = {}      # callsign.lower() -> {"cs", "uid", "ts"}
    server_uid = {}
    pos = {}         # uid -> {"cs", "uid", "lat", "lon", "ts", "ip", "via"}: last position of everyone (Ops: VIDEO markers)
    KEEP = 1800.0
    POS_MAX = 500

    @classmethod
    def saw_position(cls, data, via):
        """Remember where a person is (any a-... report with a contact and a real point)."""
        mc, mu, mp = CALL_RE.search(data), UID_RE.search(data), POINT_RE.search(data)
        if not (mc and mu and mp):
            return
        try:
            lat, lon = float(mp.group(1)), float(mp.group(2))
        except ValueError:
            return
        if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
            return
        me = EPV_RE.search(data)
        ep = MESH_EP_RE.match(me.group(1)) if me else None
        uid = mu.group(1).decode(errors="replace")
        cls.pos[uid] = {"cs": mc.group(1).decode(errors="replace"), "uid": uid, "lat": round(lat, 7), "lon": round(lon, 7),
                        "ts": time.time(), "ip": ep.group(1).decode() if ep else "", "via": via}
        if len(cls.pos) > cls.POS_MAX:                     # forget the oldest
            for k in sorted(cls.pos, key=lambda k: cls.pos[k]["ts"])[:len(cls.pos) - cls.POS_MAX]:
                del cls.pos[k]

    @classmethod
    def saw_mesh(cls, data):
        mc, me, mu = CALL_RE.search(data), EPV_RE.search(data), UID_RE.search(data)
        if not (mc and me and mu):
            return
        ep = MESH_EP_RE.match(me.group(1))
        if not ep:
            return
        d = {"cs": mc.group(1).decode(errors="replace"), "uid": mu.group(1).decode(errors="replace"),
             "ip": ep.group(1).decode(), "port": int(ep.group(2)), "proto": ep.group(3).decode(), "ts": time.time()}
        cls.mesh[d["cs"].lower()] = d
        cls.mesh_uid[d["uid"]] = d

    @classmethod
    def saw_server(cls, data):
        mc, mu = CALL_RE.search(data), UID_RE.search(data)
        if not (mc and mu):
            return
        d = {"cs": mc.group(1).decode(errors="replace"), "uid": mu.group(1).decode(errors="replace"), "ts": time.time()}
        cls.server[d["cs"].lower()] = d
        cls.server_uid[d["uid"]] = d

    @classmethod
    def _fresh(cls, d):
        return d if d and time.time() - d["ts"] < cls.KEEP else None

    @classmethod
    def find_mesh(cls, callsign=None, uid=None):
        return cls._fresh(cls.mesh.get((callsign or "").lower())) or cls._fresh(cls.mesh_uid.get(uid or ""))

    @classmethod
    def moved_to_server(cls, d):
        """A mesh phone heard on the server more recently than on the mesh (it moved over)."""
        s = cls.server_uid.get(d.get("uid", ""))
        return bool(s and s["ts"] > d["ts"])

    @classmethod
    def find_server(cls, callsign=None, uid=None):
        return cls._fresh(cls.server.get((callsign or "").lower())) or cls._fresh(cls.server_uid.get(uid or ""))


def dm_relay_on(cfg):
    return cfg.get("DM_RELAY", "0").strip().lower() in ("1", "true", "yes", "on")


def dm_relay_endpoint(cfg):
    """b"<deck mesh ip>:<DM_RELAY_PORT>:tcp", or None if the mesh address isn't configured."""
    local = cfg.get("PYTAK_MULTICAST_LOCAL_ADDR", "").strip()
    if not local or local == "0.0.0.0":
        return None
    return ("%s:%d:tcp" % (local, int(cfg.get("DM_RELAY_PORT", "4243")))).encode()


def point_contact_at(data, ep):
    """Rewrite a server person's contact endpoint so mesh phones DM them through the deck.
    Only "reach me through the server" endpoints (or none) are changed; a real IP endpoint is kept."""
    m = EP_RE.search(data)
    if m:
        val = m.group(2)
        if val.startswith(b"*:") or b"stcp" in val or not val:
            return data[:m.start(2)] + ep + data[m.end(2):]
        return data
    c = CONTACT_RE.search(data)
    if c:
        return data[:c.end()] + b' endpoint="' + ep + b'"' + data[c.end():]
    return data


def server_endpoint(data):
    """A mesh phone's position report, as the server sees it: contact endpoint "*:-1:stcp" (reach me through the
    server) instead of its mesh IP, which nobody on the server side can reach. Only a real IP endpoint is changed."""
    m = EP_RE.search(data)
    if m and MESH_EP_RE.match(m.group(2)):
        return data[:m.start(2)] + b"*:-1:stcp" + data[m.end(2):]
    return data


def add_dest(data, callsign):
    """Make sure a CoT carries <marti><dest callsign=...> so OTS delivers it to that person only."""
    if DEST_CS_RE.search(data):
        return data
    tag = b'<marti><dest callsign=' + quoteattr(callsign).encode() + b'/></marti>'
    i = data.rfind(b"</detail>")
    if i < 0:
        return None
    return data[:i] + tag + data[i:]


class DMRelay:
    """TCP listener on the deck's mesh address (DM_RELAY_PORT): mesh phones deliver DMs for server
    people here (the bridge pointed those contacts at the deck). Each DM goes to the server only,
    addressed to its recipient."""

    def __init__(self, cfg, tx_queue):
        self.cfg = cfg
        self.tx_queue = tx_queue
        self.port = int(cfg.get("DM_RELAY_PORT", "4243"))
        self.host = cfg.get("PYTAK_MULTICAST_LOCAL_ADDR", "").strip()
        self.seen = {}           # event uid -> when relayed (drops repeated copies for 10 min)
        self.udp = None

    def _datagram(self, data, peer):
        """One UDP datagram (iTAK sends DMs by UDP to our endpoint even though it says tcp)."""
        if data[:1] == b"\xbf":
            try:
                data = (proto_to_xml(data) if takproto is not None else None) or b""
            except Exception as exc:              # unreadable: dropped, but counted where Ops shows it
                Faults.note("itak_udp", exc)
                data = b""
        j, k = data.find(b"<event"), data.rfind(b"</event>")
        if j >= 0 and k > j:
            asyncio.ensure_future(self._one(data[j:k + len(b"</event>")], peer))

    async def _client(self, reader, writer):
        peer = writer.get_extra_info("peername")
        buf = b""
        try:
            while len(buf) < 262144:
                try:
                    d = await asyncio.wait_for(reader.read(65536), 15)
                except asyncio.TimeoutError:
                    break
                if not d:
                    break
                buf += d
                while b"</event>" in buf:
                    i = buf.index(b"</event>") + len(b"</event>")
                    ev, buf = buf[:i], buf[i:]
                    j = ev.find(b"<event")
                    if j >= 0:
                        await self._one(ev[j:], peer)
        finally:
            writer.close()

    async def _one(self, ev, peer):
        mt = TYPE_RE.search(ev)
        if mt and mt.group(1).startswith(b"b-f-t-r"):
            if FileRelay.instance is not None:    # a picture/package for someone on the server
                FileRelay.instance.offer(ev, peer[0] if peer else "")
            return
        if mt and mt.group(1).startswith(b"b-f-t-a"):
            out = FileRelay.instance.ack_up(ev) if FileRelay.instance is not None else None
            if out is not None:                   # a mesh phone got a file from the server: tell the sender
                await self.tx_queue.put(out)
            return
        if not mt or not mt.group(1).startswith(CHAT_PREFIX):
            return                                # only direct chat (and file notices, above) is relayed
        mu = EVENT_UID_RE.search(ev)
        if mu:                                    # iTAK sends each DM twice over UDP: relay it once
            now = time.monotonic()
            for k in [k for k, t in self.seen.items() if now - t > 600]:
                del self.seen[k]
            if mu.group(1) in self.seen:
                return
            self.seen[mu.group(1)] = now
        room = CHATROOM_RE.search(ev)
        uid1 = UID1_RE.search(ev)
        to = Peers.find_server(room.group(1).decode(errors="replace") if room else None,
                               uid1.group(1).decode(errors="replace") if uid1 else None)
        if to is None:
            log.info("DM relay: no server person for %s from %s; dropped",
                     (room.group(1) if room else b"?").decode(errors="replace"), peer[0] if peer else "?")
            Stats.dm_dropped = getattr(Stats, "dm_dropped", 0) + 1
            return
        out = add_dest(ev, to["cs"])
        if out is None:
            return
        why = bad_event(out)
        if why:                                   # same check for DMs a mesh phone hands the deck directly
            Stats.mesh_malformed += 1
            log.warning("DM relay: dropped a malformed DM from %s: %s", peer[0] if peer else "?", why)
            return
        Outbox.keep(out)
        await self.tx_queue.put(out)
        Stats.dm_up = getattr(Stats, "dm_up", 0) + 1
        Stats.note("up", "chat")
        log.info("DM relay: mesh %s -> server %s", peer[0] if peer else "?", to["cs"])

    async def run(self, _=-1):
        while True:
            try:
                if self.udp is None:             # UDP first: a failed bind leaves no TCP server holding the port
                    relay = self

                    class _Udp(asyncio.DatagramProtocol):
                        def datagram_received(self, data, addr):
                            relay._datagram(data, addr)
                    self.udp, _ = await asyncio.get_running_loop().create_datagram_endpoint(
                        _Udp, local_addr=(self.host, self.port))
                server = await asyncio.start_server(self._client, self.host, self.port)
                log.info("DM relay listening on %s:%d tcp+udp (mesh phones -> server people)", self.host, self.port)
                self._told = False
                async with server:
                    await server.serve_forever()
            except OSError as exc:
                if not getattr(self, "_told", False):
                    log.warning("DM relay port %s:%d unavailable (%s); waiting for the mesh, retrying every 5 s",
                                self.host, self.port, exc)
                    self._told = True
                await asyncio.sleep(5)


def strip_marti(data):
    """Remove the server's addressing (<marti><dest .../></marti>) from a DM handed to a mesh phone.
    iTAK drops a direct DM that still carries it (ATAK ignores it); on the mesh it means nothing.
    Plain string search, no regex: linear on any input. An unclosed <marti> is left alone."""
    data, out, i = bytes(data), [], 0
    while True:
        j = data.find(b"<marti", i)
        if j < 0 or data[j + 6:j + 7] not in (b">", b" ", b"/", b"\t", b"\n", b"\r"):
            if j < 0:
                break
            out.append(data[i:j + 6]); i = j + 6
            continue
        gt = data.find(b">", j)
        if gt < 0:
            break
        if data[gt - 1:gt] == b"/":
            end = gt + 1                                  # <marti/>
        else:
            k = data.find(b"</marti>", gt)
            if k < 0:
                break
            end = k + len(b"</marti>")
        out.append(data[i:j]); i = end
    out.append(data[i:])
    return b"".join(out)


async def deliver_to_mesh(target, data):
    """Send one CoT straight to a mesh phone's DM endpoint. Returns '' or an error."""
    data = strip_marti(data)
    try:
        if target["proto"] == "udp":
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.sendto(bytes(data), (target["ip"], target["port"]))
            s.close()
            return ""
        r, w = await asyncio.wait_for(asyncio.open_connection(target["ip"], target["port"]), 4)
        w.write(bytes(data))
        await asyncio.wait_for(w.drain(), 4)
        w.close()
        return ""
    except (OSError, asyncio.TimeoutError) as exc:
        return str(exc) or exc.__class__.__name__


FILESHARE_RE = re.compile(rb'<fileshare\s[^<>]*>')
ACKREQ_RE = re.compile(rb'<ackrequest\s[^<>]*>(?:\s*</ackrequest>)?')
HASH_IN_URL_RE = re.compile(r"(?:/metadata/|[?&]hash=)([0-9a-fA-F]{64})")


def tag_attr(tag, name):
    """One attribute of one XML tag (bytes in, str out, entities undone), or ''."""
    m = re.search(rb'\s' + name.encode() + rb'\s*=\s*(["\'])(.*?)\1', tag, re.S)
    if not m:
        return ""
    v = m.group(2).decode(errors="replace")
    return v.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&apos;", "'").replace("&amp;", "&")


def tls_context(cfg):
    """An ssl context with the deck's own certificate (the PYTAK_TLS_ lines), for the server's file API. Built the
    way PyTAK builds its own (so whatever works for the CoT link works here): no TLS 1.0/1.1, and no "strict"
    certificate checks a newer Python would add, which many TAK server CAs don't meet."""
    import ssl
    ca = (cfg.get("PYTAK_TLS_CLIENT_CAFILE") or "").strip() or None
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.options |= ssl.OP_NO_TLSv1 | ssl.OP_NO_TLSv1_1
    if ca:
        ctx.load_verify_locations(ca)
    else:
        ctx.load_default_certs()
    cert, key = (cfg.get("PYTAK_TLS_CLIENT_CERT") or "").strip(), (cfg.get("PYTAK_TLS_CLIENT_KEY") or "").strip()
    if cert:
        pw = (cfg.get("PYTAK_TLS_CLIENT_PASSWORD") or "").replace("%%", "%") or None
        ctx.load_cert_chain(cert, key or None, pw)
    if (cfg.get("PYTAK_TLS_DONT_CHECK_HOSTNAME") or "").strip().lower() in ("1", "true", "yes", "on"):
        ctx.check_hostname = False
    if (cfg.get("PYTAK_TLS_DONT_VERIFY") or "").strip().lower() in ("1", "true", "yes", "on"):
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def marti_base(cfg):
    """The server's file API: MARTI_URL, or https://<COT_URL host>:8443 (what ATAK uses)."""
    v = (cfg.get("MARTI_URL") or "").strip().rstrip("/")
    if v:
        return v
    host = urlparse(cfg.get("COT_URL", "")).hostname or ""
    return "https://%s:8443" % host if host else ""


class FileRelay:
    """Pictures, data packages and KML/KMZ a mesh phone shares with people on the server.

    ATAK doesn't send the file itself: it sends a small notice (b-f-t-r) with a link to the file on the phone
    (http://<phone>:8080/getfile...). Server people can't reach that link, so the deck does what ATAK does with a
    server: it downloads the file over the mesh (up to FILE_MAX_MB, only FILE_TYPES), checks its SHA-256, uploads it
    to the server's file API with the deck's certificate, and sends the notice on with the link changed to the
    server's copy. One file at a time, in order. The phone gets a delivery receipt (b-f-t-a) from the deck, or a
    failure with the reason. A phone can't say which server person a file is for (they all reach it through the
    deck), so the server gets it for everyone. Problems show in Ops (fault area "files"); nothing else is shown.

    The other way (server -> mesh) is the mirror image: a notice from the server links to the server's copy, which
    mesh phones can't reach. The deck downloads it with its certificate, keeps it in a small cache (FILE_CACHE_HOURS,
    FILE_CACHE_MB), serves it on its own mesh address (FILE_PORT) and hands the notice, with the link changed to the
    deck, to the mesh phone(s) it is addressed to (all mesh phones if it isn't addressed). A mesh phone's receipt
    comes back to the deck (it reaches server people through the deck) and goes on to the sender. Problems: fault
    area "files_down".
    """
    instance = None
    DONE_KEEP = 600.0

    def __init__(self, cfg, tx_queue):
        self.cfg = cfg
        self.tx_queue = tx_queue
        self.on = (cfg.get("FILES", "on") or "").strip().lower() not in ("0", "off", "no", "false")
        self.max_bytes = int(float(cfg.get("FILE_MAX_MB", "5")) * 1024 * 1024)
        self.types = tuple("." + t.strip().lower().lstrip(".") for t in
                           cfg.get("FILE_TYPES", "zip,jpg,jpeg,png,kml,kmz").split(",") if t.strip())
        self.marti = marti_base(cfg)
        self.ctx = None
        u = own_uid(cfg)
        self.uid = u.decode(errors="replace") if isinstance(u, bytes) else str(u)
        self.q = None
        self.jobs = {}            # (sender uid, sha256) -> job
        self.up = self.refused = self.failed = 0
        self.down = self.down_refused = self.down_failed = 0
        self.host = (cfg.get("PYTAK_MULTICAST_LOCAL_ADDR") or "").strip()
        self.port = int(cfg.get("FILE_PORT", "8098"))
        st = cfg.get("STATUS_FILE", "~/bridge_status.json").strip()
        base = os.path.dirname(os.path.expanduser(st)) if st.lower() not in ("off", "0", "no", "") else \
            os.path.expanduser("~")
        self.cache_dir = os.path.expanduser((cfg.get("FILE_CACHE") or "").strip() or os.path.join(base, "file_cache"))
        self.cache_hours = max(0.1, float(cfg.get("FILE_CACHE_HOURS", "12")))
        self.cache_bytes = int(float(cfg.get("FILE_CACHE_MB", "200")) * 1024 * 1024)
        self.cache = {}           # sha256 -> {"path", "name", "size", "ts"}
        self.down_acks = {}       # receipt uid -> (sender uid, sender callsign, time): mesh receipts back to the sender
        if self.on and not self.marti:
            log.warning("Files: no server address (COT_URL or MARTI_URL); files from mesh phones are not passed on")
            self.on = False
        FileRelay.instance = self

    # -- the notice
    def offer(self, ev, peer_ip):
        """A file notice from a mesh phone (b-f-t-r). Never forwarded as it is: its link only works on the mesh."""
        if not self.on or self.q is None:
            return
        tag = FILESHARE_RE.search(ev)
        if not tag:
            return
        t = tag.group(0)
        f = {k: tag_attr(t, k) for k in ("filename", "name", "senderUrl", "sha256", "sizeInBytes", "senderUid",
                                         "senderCallsign", "peerHosted")}
        ar = ACKREQ_RE.search(ev)
        ack = tag_attr(ar.group(0), "uid") if ar else ""
        f["sha256"] = f["sha256"].lower()
        who = f["senderCallsign"] or f["senderUid"] or peer_ip or "?"
        label = "%s from %s" % (os.path.basename(f["filename"] or f["name"] or "file"), who)
        now = time.time()
        for k in [k for k, j in self.jobs.items() if j["state"] in ("done", "failed") and now - j["ts"] > self.DONE_KEEP]:
            del self.jobs[k]
        key = (f["senderUid"], f["sha256"])
        job = self.jobs.get(key)
        if job:                                   # the same file again (one copy per server person): one upload
            if ack:
                job["acks"].append(ack)
            if job["state"] == "done":
                self._ack_soon(job, True, "")
            elif job["state"] == "failed":
                self._ack_soon(job, False, job["reason"])
            return
        job = {"f": f, "ev": ev, "peer": peer_ip, "acks": [ack] if ack else [], "state": "queued", "ts": now,
               "label": label, "reason": ""}
        why = self._check(f)
        if why:
            self.refused += 1
            self._fail(job, why)
            return
        self.jobs[key] = job
        try:
            self.q.put_nowait(key)
        except asyncio.QueueFull:
            self._fail(job, "too many files waiting; send it again later")

    def _check(self, f):
        if not (f["senderUrl"] and f["sha256"] and f["senderUid"]):
            return "incomplete file notice"
        if not re.fullmatch(r"[0-9a-f]{64}", f["sha256"]):
            return "bad SHA-256 in the notice"
        if not f["senderUrl"].lower().startswith(("http://", "https://")):
            return "unsupported link"
        try:
            size = int(f["sizeInBytes"] or 0)
        except ValueError:
            size = 0
        if size > self.max_bytes:
            return "too big: %.1f MB (limit %g MB)" % (size / 1048576.0, self.max_bytes / 1048576.0)
        name = os.path.basename(f["filename"] or f["name"] or "").lower()
        ext = os.path.splitext(name)[1]
        if ".zip" in self.types and (not ext or re.fullmatch(r"\.\d+", ext) or "missionpackage" in name):
            f["_need_zip"] = "1"                  # iTAK's package names ("server test", "Mom.MissionPackage.1"):
            return ""                             # allowed only if it IS a zip (checked after download)
        if not name.endswith(self.types):
            return "file type not passed on (%s); allowed: %s" % (name or "no name", " ".join(self.types))
        return ""

    @staticmethod
    def _zip_ok(f, data):
        """A file with no extension is only passed on if it is a data package (a zip)."""
        if f.get("_need_zip") and not data.startswith(b"PK\x03\x04"):
            raise RuntimeError("named like a data package, but not a data package (zip)")

    def _fail(self, job, why):
        job["state"], job["reason"], job["ts"] = "failed", why, time.time()
        self.failed += 1
        Faults.note("files", RuntimeError("%s: %s" % (job["label"], why)))
        log.warning("Files: %s not passed to the server: %s", job["label"], why)
        self._ack_soon(job, False, why)

    # -- the work, one file at a time
    async def run(self, _=-1):
        self.q = asyncio.Queue(maxsize=int(self.cfg.get("FILE_QUEUE", "20")))
        if self.on:
            log.info("Files ON: pictures, data packages and overlays between mesh phones and the server (%s, max %g MB, "
                     "%s; mesh side http://%s:%d)", self.marti, self.max_bytes / 1048576.0, " ".join(self.types),
                     self.host, self.port)
            self._cache_load()
            asyncio.ensure_future(self._serve())
        while True:
            key = await self.q.get()
            job = self.jobs.get(key)
            if job is None:
                continue
            job["state"] = "running"
            if job.get("dir") == "down":
                await self._run_down(job)
                continue
            try:
                data = await asyncio.wait_for(asyncio.to_thread(self._fetch, job), 180)
                self._zip_ok(job["f"], data)
                url = await asyncio.wait_for(asyncio.to_thread(self._upload, job, data), 180)
                fix = await asyncio.wait_for(asyncio.to_thread(self._server_copy, job, url), 180)
                await self.tx_queue.put(self._server_notice(job, url, fix))
                job["state"], job["ts"] = "done", time.time()
                self.up += 1
                Stats.note("up", "other")
                log.info("Files: %s (%d bytes) -> server", job["label"], len(data))
                self._ack_soon(job, True, "")
            except Exception as exc:              # any failure: shown in Ops, the phone is told
                self._fail(job, (str(exc) or exc.__class__.__name__)[:160])

    def _fetch(self, job):
        """Download from the phone over the mesh: its link, or the same link at the address it came from.
        A phone serving its own file over https (ATAK's secure file sharing, port 8443) uses its own self-signed
        certificate; like ATAK itself, the deck doesn't check that certificate for a phone-hosted (peerHosted) file.
        The SHA-256 from the notice is what proves the file is the one that was sent."""
        import hashlib, ssl, urllib.request
        f, urls = job["f"], [job["f"]["senderUrl"]]
        ctx = None
        if f["senderUrl"].lower().startswith("https://"):
            if f.get("peerHosted", "").lower() == "true":
                ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            else:
                if self.ctx is None:
                    self.ctx = tls_context(self.cfg)
                ctx = self.ctx
        if job["peer"]:
            u = urlparse(f["senderUrl"])
            alt = u._replace(netloc="%s%s" % (job["peer"], (":%d" % u.port) if u.port else "")).geturl()
            if alt not in urls:
                urls.append(alt)
        last = None
        for url in urls:
            try:
                with urllib.request.urlopen(url, timeout=20, context=ctx) as r:
                    data = r.read(self.max_bytes + 1)
                if len(data) > self.max_bytes:
                    raise RuntimeError("too big (over %g MB)" % (self.max_bytes / 1048576.0))
                if hashlib.sha256(data).hexdigest() != f["sha256"]:
                    raise RuntimeError("download didn't match its SHA-256")
                return data
            except Exception as exc:
                last = exc
        raise RuntimeError("couldn't download it from the phone: %s" % last)

    def _upload(self, job, data):
        """Upload the way ATAK does (POST /Marti/sync/missionupload, field "assetfile"). Returns the download link."""
        import urllib.request, urllib.parse, uuid as _uuid
        f = job["f"]
        if self.ctx is None and self.marti.startswith("https"):
            self.ctx = tls_context(self.cfg)
        name = re.sub(r'[\\/:*?"<>|\r\n]', "_", os.path.basename(f["filename"] or f["name"] or "file")) or "file"
        if f.get("_need_zip"):
            name += ".zip"                        # a data package named without an extension
        q = urllib.parse.urlencode({"hash": f["sha256"], "filename": name, "creatorUid": f["senderUid"]})
        b = "----tmt%s" % _uuid.uuid4().hex
        ctype = {".zip": "application/x-zip-compressed", ".kml": "application/vnd.google-earth.kml+xml",
                 ".kmz": "application/vnd.google-earth.kmz", ".png": "image/png"}.get(os.path.splitext(name)[1].lower(),
                                                                                   "image/jpeg" if name.lower().endswith((".jpg", ".jpeg")) else "application/octet-stream")
        body = (("--%s\r\nContent-Disposition: form-data; name=\"assetfile\"; filename=\"%s\"\r\nContent-Type: %s\r\n\r\n"
                 % (b, name, ctype)).encode() + data + ("\r\n--%s--\r\n" % b).encode())
        req = urllib.request.Request(self.marti + "/Marti/sync/missionupload?" + q, data=body, method="POST",
                                     headers={"Content-Type": "multipart/form-data; boundary=" + b})
        try:
            with urllib.request.urlopen(req, timeout=60, context=self.ctx) as r:
                reply = r.read(4096).decode(errors="replace").strip()
        except Exception as exc:
            raise RuntimeError("upload to the server failed: %s" % exc)
        try:                                       # private, like ATAK's sends to people (best effort)
            t = urllib.request.Request(self.marti + "/Marti/api/sync/metadata/%s/tool" % f["sha256"], data=b"private",
                                       method="PUT", headers={"Content-Type": "text/plain"})
            urllib.request.urlopen(t, timeout=15, context=self.ctx).close()
        except Exception:
            pass
        if reply.lower().startswith(("http://", "https://")) and len(reply) < 2000 and not any(c in reply for c in ' <>"'):
            return reply
        return self.marti + "/Marti/sync/content?hash=" + f["sha256"]

    def _server_copy(self, job, url):
        """A bare picture or KML isn't stored as sent: the server wraps it in a data package (.zip) with its own
        SHA-256 and size. If the link the server gave names another hash, fetch that copy once, check it, and
        return what the notice must say instead ({} = the server kept the file as sent)."""
        import hashlib, urllib.request
        m = HASH_IN_URL_RE.search(url)
        if not m or m.group(1).lower() == job["f"]["sha256"]:
            return {}
        want = m.group(1).lower()
        if self.ctx is None and url.lower().startswith("https"):
            self.ctx = tls_context(self.cfg)
        try:
            with urllib.request.urlopen(url, timeout=60, context=self.ctx if url.lower().startswith("https") else None) as r:
                data = r.read(2 * self.max_bytes + 65536)
        except Exception as exc:
            raise RuntimeError("the server re-packaged it, but its copy couldn't be read: %s" % exc)
        if hashlib.sha256(data).hexdigest() != want:
            raise RuntimeError("the server's re-packaged copy didn't match its own SHA-256")
        base = os.path.splitext(os.path.basename(job["f"]["filename"] or job["f"]["name"] or "file"))[0]
        base = re.sub(r"[^A-Za-z0-9._-]", "_", base).strip("._") or "file"
        log.info("Files: the server re-packaged %s as %s.zip (%d bytes)", job["label"], base, len(data))
        return {"sha256": want, "sizeInBytes": str(len(data)), "filename": base + ".zip"}

    @staticmethod
    def _set_attr(tag, name, value):
        new = b" " + name.encode() + b"=" + quoteattr(value).encode()
        pat = rb'\s' + name.encode() + rb'\s*=\s*(["\']).*?\1'
        if re.search(pat, tag, re.S):
            return re.sub(pat, lambda _m: new, tag, count=1, flags=re.S)
        return tag[:-2] + new + b"/>" if tag.endswith(b"/>") else tag[:-1] + new + b">"

    def _server_notice(self, job, url, fix=None):
        """The phone's notice with the link pointing at the server's copy (and, if the server re-packaged the file,
        that copy's hash, size and .zip name); no receipt asked (the deck gave it)."""
        ev = job["ev"]
        tag = FILESHARE_RE.search(ev)
        t = tag.group(0)
        t2 = self._set_attr(t, "senderUrl", url)
        for k, v in (fix or {}).items():
            t2 = self._set_attr(t2, k, v)
        t2 = re.sub(rb'\speerHosted\s*=\s*(["\']).*?\1', b"", t2, flags=re.S)
        ev = ev[:tag.start()] + t2 + ev[tag.end():]
        return ACKREQ_RE.sub(b"", ev)

    # -- server -> mesh
    def offer_down(self, ev):
        """A file notice from the server. Never put on the mesh as it is: its link is the server's."""
        if not self.on or self.q is None:
            return
        tag = FILESHARE_RE.search(ev)
        if not tag:
            return
        t = tag.group(0)
        f = {k: tag_attr(t, k) for k in ("filename", "name", "senderUrl", "sha256", "sizeInBytes", "senderUid",
                                         "senderCallsign", "peerHosted")}
        f["sha256"] = f["sha256"].lower()
        key = (f["senderUid"], f["sha256"])
        what = "%s from %s" % (os.path.basename(f["filename"] or f["name"] or "file"),
                               f["senderCallsign"] or f["senderUid"] or "server")
        up = self.jobs.get(key)
        if up is not None and up.get("dir") != "down":
            log.info("Files: %s: not for the mesh (the deck carried it up from the mesh)", what)
            return                                # our own upload coming back: it came from the mesh
        if Shared.from_mesh(ev, f["senderUid"].encode()):
            log.info("Files: %s: not for the mesh (sent from the mesh)", what)
            return                                # sent by a mesh phone: not for the mesh again
        names = [m.decode(errors="replace") for m in DEST_CS_RE.findall(ev)]
        uids = [m.decode(errors="replace") for m in DEST_UID_RE.findall(ev)]
        if names or uids:
            targets = []
            for n in names:
                d = Peers.find_mesh(callsign=n)
                if d and d not in targets:
                    targets.append(d)
            for u in uids:
                d = Peers.find_mesh(uid=u)
                if d and d not in targets:
                    targets.append(d)
            if not targets:
                log.info("Files: %s: not for the mesh (for %s, not heard on the mesh)", what, ",".join(names + uids))
                return                            # addressed to people who aren't on the mesh
        else:
            targets = [d for d in list(Peers.mesh_uid.values()) if Peers._fresh(d)   # for everyone: all mesh phones,
                       and not Peers.moved_to_server(d)]                             # not ones now on the server
            if not targets:
                log.info("Files: %s: not for the mesh (sent to everyone, no mesh phones heard)", what)
                return
        who = f["senderCallsign"] or f["senderUid"] or "server"
        label = "%s from %s" % (os.path.basename(f["filename"] or f["name"] or "file"), who)
        ar = ACKREQ_RE.search(ev)
        if ar:
            a = tag_attr(ar.group(0), "uid")
            if a:
                self.down_acks[a] = (f["senderUid"], f["senderCallsign"], time.time())
                if len(self.down_acks) > 2000:
                    for k in sorted(self.down_acks, key=lambda k: self.down_acks[k][2])[:500]:
                        del self.down_acks[k]
        job = self.jobs.get(key)
        now = time.time()
        if job and job.get("dir") == "down" and job["state"] != "failed":
            new = [d for d in targets if d not in job["targets"]]
            job["targets"] += new
            if job["state"] == "done" and new:    # already in the cache: just tell the new people
                asyncio.ensure_future(self._deliver_down(job, new))
            return
        job = {"dir": "down", "f": f, "ev": ev, "targets": targets, "state": "queued", "ts": now, "label": label,
               "reason": "", "acks": [], "peer": ""}
        why = self._check(f)
        if why:
            self.down_refused += 1
            self._fail_down(job, why)
            return
        self.jobs[key] = job
        try:
            self.q.put_nowait(key)
        except asyncio.QueueFull:
            self._fail_down(job, "too many files waiting")

    def _fail_down(self, job, why):
        job["state"], job["reason"], job["ts"] = "failed", why, time.time()
        self.down_failed += 1
        Faults.note("files_down", RuntimeError("%s: %s" % (job["label"], why)))
        log.warning("Files: %s not passed to the mesh: %s", job["label"], why)

    async def _run_down(self, job):
        try:
            if job["f"]["sha256"] not in self.cache:
                data = await asyncio.wait_for(asyncio.to_thread(self._fetch_server, job), 180)
                self._zip_ok(job["f"], data)
                await asyncio.to_thread(self._cache_put, job, data)
            job["state"], job["ts"] = "done", time.time()
            self.down += 1
            Stats.note("down", "other")
            log.info("Files: %s -> mesh (%d phone(s))", job["label"], len(job["targets"]))
            await self._deliver_down(job, list(job["targets"]))
        except Exception as exc:                  # shown in Ops; the mesh phones just never get the notice
            self._fail_down(job, (str(exc) or exc.__class__.__name__)[:160])

    def _fetch_server(self, job):
        """Download the server's copy with the deck's certificate: its link, or the same link on the server
        address the bridge uses (a link can name a host only the sender's network knows).
        The copy must match the notice's SHA-256, or else the SHA-256 the link itself names (content?hash=... /
        metadata/<hash>): iTAK's notice can carry a different hash than the copy the server stored and named in the
        link. The link came from the server over the deck's own TLS connection, so its hash is the one to trust;
        the notice is then corrected to it. Matching neither = refused, with both hashes in the error."""
        import gzip, hashlib, urllib.request
        f = job["f"]
        if not f["senderUrl"].lower().startswith(("http://", "https://")):
            raise RuntimeError("unsupported link")
        urls = [f["senderUrl"]]
        mine = urlparse(self.marti)
        u = urlparse(f["senderUrl"])
        if mine.hostname and u.hostname != mine.hostname:
            urls.append(u._replace(netloc="%s%s" % (mine.hostname, (":%d" % u.port) if u.port else "")).geturl())
        # the standard TAK file API on the server the bridge uses, by hash: works whatever link the sender's app wrote
        # (iTAK's link isn't always one the deck can use)
        lm = HASH_IN_URL_RE.search(f["senderUrl"])
        for h in [f["sha256"]] + ([lm.group(1).lower()] if lm else []):
            std = "%s/Marti/sync/content?hash=%s" % (self.marti, h)
            if std not in urls:
                urls.append(std)
        last, tried = None, []
        for url in urls:
            try:
                ctx = None
                if url.lower().startswith("https"):
                    if self.ctx is None:
                        self.ctx = tls_context(self.cfg)
                    ctx = self.ctx
                with urllib.request.urlopen(url, timeout=60, context=ctx) as r:
                    data = r.read(self.max_bytes + 1)
                    ctype = r.headers.get("Content-Type", "?")
                    if (r.headers.get("Content-Encoding") or "").lower() == "gzip":
                        data = gzip.decompress(data)
                if ctype.lower().startswith("text/html"):
                    raise RuntimeError("a web page (%d bytes), not the file" % len(data))
                if len(data) > self.max_bytes:
                    raise RuntimeError("too big (over %g MB)" % (self.max_bytes / 1048576.0))
                got = hashlib.sha256(data).hexdigest()
                if got == f["sha256"]:
                    return data
                m = HASH_IN_URL_RE.search(f["senderUrl"])
                if m and got == m.group(1).lower():
                    log.info("Files: %s: the notice's SHA-256 (%s...) isn't the server copy's; the link names %s..., "
                             "which matches: using the server's", job["label"], f["sha256"][:12], got[:12])
                    job["fix"] = {"sha256": got, "sizeInBytes": str(len(data))}
                    f["sha256"] = got
                    return data
                raise RuntimeError("download didn't match its SHA-256 (notice %s..., link %s..., got %s..., %d bytes, %s)"
                                   % (f["sha256"][:12], (m.group(1)[:12] if m else "none"), got[:12], len(data), ctype))
            except Exception as exc:
                last = exc
                tried.append("%s -> %s" % (urlparse(url).path + ("?" + urlparse(url).query[:20] if urlparse(url).query else ""),
                                           str(exc)[:90]))
        log.info("Files: %s: tried %s", job["label"], " | ".join(tried))
        raise RuntimeError("couldn't download it from the server: %s" % last)

    # cache: <sha256> files in FILE_CACHE, gone after FILE_CACHE_HOURS, oldest first past FILE_CACHE_MB
    def _cache_load(self):
        try:
            os.makedirs(self.cache_dir, mode=0o700, exist_ok=True)
            for n in os.listdir(self.cache_dir):
                p = os.path.join(self.cache_dir, n)
                if re.fullmatch(r"[0-9a-f]{64}", n) and os.path.isfile(p):
                    self.cache[n] = {"path": p, "name": n + ".zip", "size": os.path.getsize(p),
                                     "ts": os.path.getmtime(p)}
                elif n.endswith(".tmp"):
                    os.remove(p)
            self._cache_trim()
        except OSError as exc:
            Faults.note("files_down", RuntimeError("file cache %s: %s" % (self.cache_dir, exc)))

    def _cache_put(self, job, data):
        f = job["f"]
        os.makedirs(self.cache_dir, mode=0o700, exist_ok=True)
        p = os.path.join(self.cache_dir, f["sha256"])
        with open(p + ".tmp", "wb") as fh:
            fh.write(data)
        os.replace(p + ".tmp", p)
        name = re.sub(r'[\\/:*?"<>|\r\n]', "_", os.path.basename(f["filename"] or f["name"] or "")) or (f["sha256"] + ".zip")
        if f.get("_need_zip"):
            name += ".zip"
        self.cache[f["sha256"]] = {"path": p, "name": name, "size": len(data), "ts": time.time()}
        self._cache_trim()

    def _cache_trim(self):
        now = time.time()
        for h, c in sorted(self.cache.items(), key=lambda kv: kv[1]["ts"]):
            over = sum(x["size"] for x in self.cache.values()) > self.cache_bytes
            if now - c["ts"] > self.cache_hours * 3600 or over:
                try:
                    os.remove(c["path"])
                except OSError:
                    pass
                del self.cache[h]

    async def _deliver_down(self, job, targets):
        """The notice to each mesh phone, with the link pointing at the deck."""
        f = job["f"]
        c = self.cache.get(f["sha256"])
        if c is None:
            return
        from urllib.parse import quote
        url = "http://%s:%d/getfile?hash=%s&name=%s" % (self.host, self.port, f["sha256"], quote(c["name"]))
        ev = job["ev"]
        tag = FILESHARE_RE.search(ev)
        t2 = self._set_attr(tag.group(0), "senderUrl", url)
        for k, v in (job.get("fix") or {}).items():   # the server copy's hash and size (see _fetch_server)
            t2 = self._set_attr(t2, k, v)
        t2 = re.sub(rb'\speerHosted\s*=\s*(["\']).*?\1', b"", t2, flags=re.S)
        out = ev[:tag.start()] + t2 + ev[tag.end():]
        for d in targets:
            err = await deliver_to_mesh(d, out)    # deliver_to_mesh strips the server's <marti> addressing
            if err:
                Faults.note("files_down", RuntimeError("%s: can't reach %s: %s" % (job["label"], d.get("cs", "?"), err)))

    async def _serve(self):
        """The deck's file server on its mesh address: GET /getfile?hash=<sha256> for files in the cache only."""
        while True:
            try:
                server = await asyncio.start_server(self._http, self.host, self.port)
                log.info("Files: serving cached server files to the mesh on %s:%d", self.host, self.port)
                self._told = False
                async with server:
                    await server.serve_forever()
            except OSError as exc:
                if not getattr(self, "_told", False):
                    log.warning("Files: port %s:%d unavailable (%s); retrying every 5 s", self.host, self.port, exc)
                    self._told = True
                await asyncio.sleep(5)

    async def _http(self, reader, writer):
        try:
            line = await asyncio.wait_for(reader.readline(), 10)
            while True:                            # skip the headers
                h = await asyncio.wait_for(reader.readline(), 10)
                if h in (b"\r\n", b"\n", b""):
                    break
            parts = line.decode("latin-1", "replace").split()
            meth, path = (parts[0], parts[1]) if len(parts) >= 2 else ("", "")
            from urllib.parse import parse_qs
            q = parse_qs(urlparse(path).query)
            h = (q.get("hash") or q.get("file") or [""])[0].lower()
            c = self.cache.get(h) if re.fullmatch(r"[0-9a-f]{64}", h) else None
            if meth not in ("GET", "HEAD") or not urlparse(path).path.startswith("/getfile") or c is None \
                    or not os.path.exists(c["path"]):
                writer.write(b"HTTP/1.0 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                await writer.drain()
                return
            writer.write(("HTTP/1.0 200 OK\r\nContent-Type: application/octet-stream\r\nContent-Length: %d\r\n"
                          "Content-Disposition: attachment; filename=%s\r\nConnection: close\r\n\r\n"
                          % (c["size"], quoteattr(c["name"]))).encode())
            if meth == "GET":
                with open(c["path"], "rb") as fh:
                    while True:
                        b = fh.read(65536)
                        if not b:
                            break
                        writer.write(b)
                        await writer.drain()
            await writer.drain()
        except (OSError, asyncio.TimeoutError, ValueError):
            pass
        finally:
            writer.close()

    def ack_up(self, ev):
        """A mesh phone's receipt (b-f-t-a) for a file from the server: on to the server person who sent it."""
        r = re.search(rb'<ackresponse\s[^<>]*>', ev)
        a = tag_attr(r.group(0), "uid") if r else ""
        info = self.down_acks.get(a)
        if not info:
            return None
        out = add_dest(ev, info[1]) if info[1] else None
        if out is not None and info[0] and not DEST_UID_RE.search(out):
            i = out.rfind(b"</marti>")
            if i > 0:
                out = out[:i] + b"<dest uid=" + quoteattr(info[0]).encode() + b"/>" + out[i:]
        return out

    # -- receipts back to the phone
    def _ack_soon(self, job, ok, reason):
        acks, job["acks"] = job["acks"], []
        if acks:
            asyncio.ensure_future(self._ack_safe(job, acks, ok, reason))

    async def _ack_safe(self, job, acks, ok, reason):
        try:
            await self._ack(job, acks, ok, reason)
        except Exception as exc:                  # a receipt that can't be built or sent: shown, never fatal
            Faults.note("files", RuntimeError("receipt for %s: %s" % (job["label"], exc)))

    async def _ack(self, job, acks, ok, reason):
        f = job["f"]
        target = Peers.find_mesh(uid=f["senderUid"]) or (
            {"ip": job["peer"], "port": 4242, "proto": "tcp"} if job["peer"] else None)
        if target is None:
            return
        now = datetime.now(timezone.utc)
        t0 = now.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (now.microsecond // 1000)
        st = datetime.fromtimestamp(now.timestamp() + 60, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        for a in acks:
            xml = ('<event version="2.0" uid=%s type="b-f-t-a" how="m-g" time="%s" start="%s" stale="%s">'
                   '<point lat="0" lon="0" hae="0" ce="9999999" le="9999999"/><detail>'
                   '<ackresponse uid=%s senderUid=%s success="%s" tag=%s reason=%s sha256=%s sizeInBytes=%s/>'
                   '</detail></event>') % (
                quoteattr("FileAck-%s-%s" % (self.uid, a)), t0, t0, st, quoteattr(a), quoteattr(self.uid),
                "true" if ok else "false", quoteattr(f["name"] or f["filename"]),
                quoteattr("" if ok else "deck: " + reason), quoteattr(f["sha256"]), quoteattr(f["sizeInBytes"] or "0"))
            err = await deliver_to_mesh(target, xml.encode())
            if err:
                log.info("Files: receipt to %s failed: %s", f["senderCallsign"] or target["ip"], err)

    @classmethod
    def status(cls):
        s = cls.instance
        if s is None or not s.on:
            return {"enabled": False}
        return {"enabled": True, "sent": s.up, "refused": s.refused, "failed": s.failed,
                "down": s.down, "down_refused": s.down_refused, "down_failed": s.down_failed,
                "cached": len(s.cache), "waiting": s.q.qsize() if s.q else 0}


class Identity:
    """Re-send the bridge identity every IDENTIFY_INTERVAL seconds. Never raises.
    With GPS: every GPS_INTERVAL seconds while there is a fix (DECK moves on server maps); once when
    the fix is lost (last spot, already stale), then every IDENTIFY_INTERVAL again."""

    def __init__(self, cfg, tx_queue, gps=None):
        self.cfg = cfg
        self.tx_queue = tx_queue
        self.gps = gps
        self.interval = max(30.0, float(cfg.get("IDENTIFY_INTERVAL", "300")))
        self.gps_interval = max(5.0, float(cfg.get("GPS_INTERVAL", "15")))

    async def run(self, _=-1):
        last_sent, was_live = time.monotonic(), None
        while True:
            await asyncio.sleep(self.gps_interval if self.gps else self.interval)
            try:
                live, pos = self.gps.position() if self.gps else (False, None)
                due = time.monotonic() - last_sent >= self.interval
                if live or due or (was_live and not live):
                    await self.tx_queue.put(bridge_identity(self.cfg, pos=pos, live=live, status=deck_status(self.cfg)))
                    last_sent = time.monotonic()
                    if self.gps and live != was_live and (live or was_live):   # no "lost" line at startup
                        log.info("GPS %s: DECK %s on server maps", "fix" if live else "fix lost",
                                 "shown live" if live else "kept at last spot (stale)" if pos else "not shown")
                was_live = live
            except Exception as exc:
                Faults.note("identity", exc, hold=1.5 * (self.gps_interval if self.gps else self.interval))


class MeshToServer:
    """Read datagrams from the mesh multicast group, queue them for the server."""

    def __init__(self, cfg, tx_queue, key="MESH_URL", default=None):
        self.cfg = cfg
        self.tx_queue = tx_queue
        self.key = key
        self.default = default or DEFAULT_MESH_URL
        self.dedupe = float(cfg.get("DEDUPE_SECONDS", "0.5"))
        self.recent = {}          # payload hash -> last seen time
        self.my_uid = own_uid(cfg)
        self.sent = 0
        self.skipped = 0
        self.uplink = None
        # SERVER_ENDPOINT = stcp (default): server people see mesh phones as "reach me through the server", not at a
        # mesh IP they can't reach. Then their apps send DMs and files through the server (which the deck carries),
        # instead of trying the phone directly or hosting a file only the mesh can't fetch. keep = pass it unchanged.
        self.server_ep = (cfg.get("SERVER_ENDPOINT", "stcp") or "").strip().lower() not in ("keep", "off", "0", "no")

    async def open_reader(self):
        """Join the mesh group. If the deck's mesh address isn't there yet (node unplugged or off), wait
        and try again every 3 s instead of exiting; the server side keeps running meanwhile (server-only mode)."""
        url = urlparse(self.cfg.get(self.key, self.default))
        local = (self.cfg.get("PYTAK_MULTICAST_LOCAL_ADDR", "0.0.0.0"), 0)
        local_addrs = self.cfg.get("PYTAK_MULTICAST_LOCAL_ADDRS")
        ttl = self.cfg.get("PYTAK_MULTICAST_TTL", 1)
        told = False
        while True:
            try:
                if MeshAddr.missing:
                    raise OSError("%s has no address yet" % MeshAddr.iface)
                reader, _ = await pytak.create_udp_client(url, local, ttl, local_addrs)
                if reader is None:
                    raise OSError("could not open mesh reader for %s" % url.geturl())
                break
            except OSError as exc:
                if self.key == "MESH_URL":
                    Stats.mesh_ready = False
                    Stats.mesh_wait_since = Stats.mesh_wait_since or time.time()
                if not told:
                    log.warning("Waiting for the mesh: can't listen on %s via %s (%s). Node unplugged or off? "
                                "The server side is running; retrying every 3 s.", url.geturl(), local[0], exc)
                    told = True
                await asyncio.sleep(3)
        if self.key == "MESH_URL":
            if told:
                log.info("Mesh is here: switching from server-only to mesh + server")
            Stats.mesh_ready, Stats.mesh_wait_since = True, 0.0
        log.info("Listening on %s via %s", url.geturl(), local[0])
        return reader

    def _is_dupe(self, data):
        if self.dedupe <= 0:
            return False
        now = time.monotonic()
        key = hash(data)
        last = self.recent.get(key)
        self.recent[key] = now
        if len(self.recent) > 2048:        # cheap cleanup
            cutoff = now - max(self.dedupe, 5)
            self.recent = {k: v for k, v in self.recent.items() if v > cutoff}
        return last is not None and (now - last) < self.dedupe

    async def run(self, _=-1):
        reader = await self.open_reader()
        while True:
            data, addr = await reader.recv()
            if not data:
                continue
            # TAK protobuf frames start with 0xbf 0x01 0xbf: convert them to XML.
            if data[:1] == b"\xbf":
                converted, kind = None, "no-takproto"
                if takproto is not None:
                    try:
                        kind = proto_kind(data)
                        if kind == "event":
                            converted = proto_to_xml(data)
                    except Exception as exc:
                        kind = "failed"
                        self.failed = getattr(self, "failed", 0) + 1
                        Stats.mesh_failed += 1
                        if self.failed <= 5 or self.failed % 100 == 0:
                            log.warning("protobuf convert failed (%d) from %s: %r", self.failed, addr[0], exc)
                if kind == "control":
                    Stats.mesh_control += 1
                    if Stats.mesh_control in (1, 1000):
                        log.info("TAK Protocol announcement from %s (protocol version info, no CoT inside): "
                                 "not forwarded, counted as control (%d so far)", addr[0], Stats.mesh_control)
                    continue
                if converted is None:
                    self.skipped += 1
                    Stats.mesh_skipped += 1
                    if self.skipped <= 3 or self.skipped in (100, 1000):
                        why = {"no-takproto": "takproto not installed",
                               "unknown": "not TAK Protocol v1 mesh (first bytes %s)" % data[:6].hex(),
                               "failed": "could not be decoded"}.get(kind, kind)
                        log.warning("Skipped a TAK-protobuf datagram from %s (%d bytes): %s; %d skipped so far",
                                    addr[0], len(data), why, self.skipped)
                    continue
                data = converted
            if b"<event" not in data:
                log.warning("Dropped non-CoT datagram from %s (%d bytes) on %s",
                            addr[0], len(data), self.key)
                continue
            events, why = split_events(data)
            if not events:
                Stats.mesh_malformed += 1
                if Stats.mesh_malformed <= 3 or Stats.mesh_malformed % 100 == 0:
                    log.warning("Dropped a malformed packet from %s (%d bytes): %s; %d so far",
                                addr[0], len(data), why, Stats.mesh_malformed)
                continue
            if why or len(events) > 1:            # repaired: padding/junk cut off, or several events split
                Stats.mesh_repaired += 1
                if Stats.mesh_repaired <= 3 or Stats.mesh_repaired % 100 == 0:
                    log.info("Repaired a packet from %s (%d bytes): %d good event(s) kept%s; %d repaired so far",
                             addr[0], len(data), len(events), (", " + why) if why else "", Stats.mesh_repaired)
            for data in events:
                await self._forward(data, addr)

    async def _forward(self, data, addr):
        """One good event from the mesh: on to the server."""
        if b'type="b-f-t-r' in data[:600]:
            if FileRelay.instance is not None:    # its link only works on the mesh: the deck carries the file
                FileRelay.instance.offer(data, addr[0] if addr else "")
            return
        if self.key == "MESH_CHAT_URL":
            log.info("Chat datagram from %s (%d bytes) -> server", addr[0], len(data))
        if self._is_dupe(data):
            return
        m = UID_RE.search(data)
        uid = m.group(1) if m else None
        if uid is not None:
            if Shared.is_echo(data, uid):
                return            # a server report put back on the mesh (by this deck or another): not a phone
            if uid == self.my_uid:
                return            # Chat's own mesh position report: the server knows the deck
                                    # from the bridge identity (reach-me-via-server), keep it that way
            Shared.saw_mesh(data, uid)
        if b"<contact" in data and b'type="a-' in data:
            Peers.saw_mesh(data)             # remember this phone's DM address
            Peers.saw_position(data, "mesh")
            if self.server_ep:
                data = server_endpoint(data)
        Outbox.keep(data)                     # chats are kept on disk until written to the server
        if self.uplink is not None:
            mt1 = TYPE_RE.search(data)
            self.uplink.put(data, uid, mt1.group(1) if mt1 else b"")
        else:
            await self.tx_queue.put(data)
        if b'type="b-t-f' in data:
            LocalCopy.send(b"mesh", data)
        self.sent += 1
        Stats.mesh_events += 1
        Stats.mesh_last_ts = time.time()
        Stats.mesh_last_from = addr[0]
        mt0 = TYPE_RE.search(data)
        Stats.note("up", classify(mt0.group(1)) if mt0 else "other")
        if self.sent % 100 == 1:
            log.info("Forwarded %d events (last from %s)", self.sent, addr[0])


class ServerToMesh:
    """Reads the server stream; optionally puts selected events onto the mesh.

    RETURN_PATH turns it on (deck.ini.example sets 1). Only CoT types in RETURN_TYPES (positions, chats, markers,
    routes, drawings by default), at most RETURN_MAX_PER_SEC, and never for a UID heard on the mesh (no echo).
    """

    def __init__(self, cfg, rx_queue):
        self.rx_queue = rx_queue
        self.enabled = cfg.get("RETURN_PATH", "0").strip().lower() in ("1", "true", "yes", "on")
        Stats.return_enabled = self.enabled
        self.types = tuple(t.strip().encode() for t in
                           cfg.get("RETURN_TYPES", "a-f-,b-t-f,b-m-p,b-m-r,u-d-").split(",") if t.strip())
        self.max_per_sec = float(cfg.get("RETURN_MAX_PER_SEC", "5"))
        url = urlparse(cfg.get("RETURN_URL", "udp://239.2.3.1:6969"))
        self.dest = (url.hostname, url.port)
        curl = urlparse(cfg.get("RETURN_CHAT_URL", "udp://224.10.10.1:17012"))
        self.chat_dest = (curl.hostname, curl.port)
        self.local = cfg.get("PYTAK_MULTICAST_LOCAL_ADDR", "0.0.0.0")
        self.ttl = int(cfg.get("PYTAK_MULTICAST_TTL", 1))
        self.sock = None
        self.sent = 0
        ident = bridge_identity(cfg)
        my_uid = re.escape(UID_RE.search(ident).group(1))
        my_cs = re.escape(re.search(rb'callsign="([^"]+)"', ident).group(1))
        self.deck_pats = [re.compile(rb'<dest[^>]*callsign="%s"' % my_cs), re.compile(rb'<dest[^>]*uid="%s"' % my_uid),
                          re.compile(rb'uid1="%s"' % my_uid), re.compile(rb'chatroom="%s"' % my_cs)]
        self._window = 0.0
        self._count = 0
        self.dm_relay = dm_relay_on(cfg)
        self.relay_ep = dm_relay_endpoint(cfg) if self.dm_relay else None
        if self.dm_relay and not self.relay_ep:
            log.warning("DM_RELAY needs PYTAK_MULTICAST_LOCAL_ADDR; mesh -> server DMs stay off")
        self.dm_seen = {}                         # (event uid, target) -> time, so a DM goes out once
        self.dm_down = 0
        self.dm_hold = float(cfg.get("DM_HOLD_SECONDS", "180"))
        self.pending = []                         # (time, data): DMs for mesh phones not heard yet
        self._last_retry = time.monotonic()
        # Fair return path. Chats/markers/drawings go first; server people's positions are kept
        # newest-per-person and sent in turn (whoever went longest without an update), within the rate cap.
        self.prio = deque()                       # (monotonic, data, etype, uid)
        self.sa_pend = {}                         # uid -> (data, etype)
        self.sa_wait = {}                         # uid -> monotonic it started waiting
        self.sa_last = {}                         # uid -> monotonic it was last sent to the mesh
        self.tokens = max(1.0, self.max_per_sec)
        self._refill = time.monotonic()
        self._pump_task = None

    def _for_deck(self, data):
        """True for chat addressed to the bridge/deck itself (DECK is a contact on phones now)."""
        return any(p.search(data) for p in self.deck_pats)

    def _open(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        if ipaddress_is_multicast(self.dest[0]):
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, self.ttl)
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 0)
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                         socket.inet_aton(self.local))
        self.sock = s
        log.info("Return path ON: server -> %s:%s (types %s, max %s/s)",
                 self.dest[0], self.dest[1],
                 [t.decode() for t in self.types], self.max_per_sec)

    def _allowed_rate(self):
        now = time.monotonic()
        if now - self._window >= 1.0:
            self._window, self._count = now, 0
        self._count += 1
        return self._count <= self.max_per_sec

    @staticmethod
    def _dm_targets(data):
        names = [m.decode(errors="replace") for m in DEST_CS_RE.findall(data)]
        uids = [m.decode(errors="replace") for m in DEST_UID_RE.findall(data)]
        targets = []
        for n in names:
            t = Peers.find_mesh(callsign=n)
            if t and t not in targets:
                targets.append(t)
        for u in uids:
            t = Peers.find_mesh(uid=u)
            if t and t not in targets:
                targets.append(t)
        return targets, names + uids

    async def _dm_down(self, data, held=False):
        """Deliver a server DM to the mesh phone(s) it is addressed to. True = done (or nothing to do)."""
        mu = UID_RE.search(data)
        euid = mu.group(1) if mu else b""
        targets, who = self._dm_targets(data)
        if not targets:
            if not held:
                # e.g. right after the deck starts: the DM waited in DECK's mailbox and arrives before
                # that phone's first position report. Hold it until the phone is heard.
                if len(self.pending) >= 200:
                    self.pending.pop(0)
                self.pending.append((time.monotonic(), data))
                log.info("DM for %s: not heard on the mesh yet; holding it up to %.0f s (never broadcast)",
                         ",".join(who) or "?", self.dm_hold)
            return False
        now = time.monotonic()
        if len(self.dm_seen) > 2048:
            self.dm_seen = {k: v for k, v in self.dm_seen.items() if now - v < 600}
            cap_oldest(self.dm_seen, MAX_UIDS)
        for t in targets:
            key = (euid, t["uid"])
            if key in self.dm_seen and now - self.dm_seen[key] < 600:
                continue                          # OTS filed one copy per mesh recipient: send each once
            self.dm_seen[key] = now
            err = await deliver_to_mesh(t, data)
            if err:
                log.warning("DM for %s: can't reach %s:%d (%s)", t["cs"], t["ip"], t["port"], err)
                continue
            self.dm_down += 1
            Stats.note("down", "chat")
            log.info("DM relay: server -> mesh %s (%s)%s", t["cs"], t["ip"], " (was held)" if held else "")
        return True

    async def _retry_pending(self):
        self._last_retry = time.monotonic()
        if not self.pending:
            return
        keep = []
        for t0, data in self.pending:
            if await self._dm_down(data, held=True):
                continue
            if time.monotonic() - t0 > self.dm_hold:
                who = ",".join(self._dm_targets(data)[1]) or "?"
                log.info("DM for %s: still not heard on the mesh after %.0f s; dropped", who, self.dm_hold)
                continue
            keep.append((t0, data))
        self.pending = keep

    async def run(self, _=-1):
        if self.enabled:
            try:
                self._open()
            except OSError as exc:                # no mesh address yet: _send opens it once the mesh is there
                self.sock = None
                log.info("Return path: waiting for the mesh (%s)", exc)
            self._pump_task = asyncio.ensure_future(self._pump())
        else:
            log.info("Return path OFF (set RETURN_PATH = 1 to send server markers to the mesh)")
        while True:
            if self.pending and time.monotonic() - self._last_retry > 2:
                await self._retry_pending()
            try:
                data = await asyncio.wait_for(self.rx_queue.get(), 2)   # always drain, or the queue fills
            except asyncio.TimeoutError:
                await self._retry_pending()
                continue
            if Link.is_pong(data):
                Link.on_pong(data)                # our own ping answered: link health, not traffic
                continue
            if DMSelfTest.is_mine(data):
                DMSelfTest.on_return(data)        # our DM-route probe came back: the server-side DM path works
                continue
            Stats.server_rx += 1
            Stats.server_last_rx_ts = time.time()
            if isinstance(data, (bytes, bytearray)) and b'type="b-f-t-r' in data[:600]:
                if FileRelay.instance is not None:  # a file for the mesh: the deck carries it (its link is the server's)
                    FileRelay.instance.offer_down(bytes(data))
                continue
            if self.dm_relay and isinstance(data, (bytes, bytearray)) and b'type="b-t-f' in data \
                    and (DEST_CS_RE.search(data) or DEST_UID_RE.search(data)) and not self._for_deck(data):
                await self._dm_down(bytes(data))  # a DM for a mesh phone: to that phone only
                continue
            if isinstance(data, (bytes, bytearray)) and b'type="b-t-f' in data:
                LocalCopy.send(b"server", data)   # Chat hears server replies even if RETURN_PATH is off
            elif isinstance(data, (bytes, bytearray)) and b'type="a-' in data and b"<contact" in data:
                Peers.saw_server(data)
                Peers.saw_position(data, "server")
                LocalCopy.send(b"server", data)   # positions of server-side people: Chat roster / DMs
            if isinstance(data, (bytes, bytearray)):
                mu = UID_RE.search(data)
                if mu:
                    Shared.saw_server(data, mu.group(1))   # so its echo (from any deck) isn't sent back up
            if not self.enabled or not isinstance(data, (bytes, bytearray)):
                continue
            mt = TYPE_RE.search(data)
            mu = UID_RE.search(data)
            if not mt or not mu:
                continue
            etype, uid = mt.group(1), mu.group(1)
            if not etype.startswith(self.types):
                continue
            if etype.startswith(CHAT_PREFIX) and self._for_deck(data):
                continue                          # a direct message to the deck: Chat only, not the mesh
            if Shared.from_mesh(data, uid):
                continue                          # originated on the mesh
            now = time.monotonic()
            if etype.startswith(b"a-") and b"<contact" in data:
                if uid in self.sa_pend:
                    Stats.return_coalesced += 1
                else:
                    self.sa_wait[uid] = now
                self.sa_pend[uid] = (bytes(data), etype)
            else:
                self.prio.append((now, bytes(data), etype, uid))
                while len(self.prio) > 500:
                    self.prio.popleft()
                    Stats.return_dropped += 1

    def _next(self):
        now = time.monotonic()
        while self.prio:
            t0, data, etype, uid = self.prio.popleft()
            if now - t0 <= 30:
                return data, etype, uid
            Stats.return_dropped += 1             # waited over 30 s behind the rate cap: too old to matter
        best = None
        for uid, t0 in self.sa_wait.items():
            key = (self.sa_last.get(uid, -1e9), t0)
            if best is None or key < best[0]:
                best = (key, uid)
        if best is None:
            return None
        uid = best[1]
        data, etype = self.sa_pend.pop(uid)
        del self.sa_wait[uid]
        self.sa_last[uid] = now
        if len(self.sa_last) > 4096:
            for k in [k for k, v in self.sa_last.items() if now - v > 600]:
                del self.sa_last[k]
        return data, etype, uid

    async def _pump(self):
        """Send queued return-path events at up to RETURN_MAX_PER_SEC, smoothly. Never ends."""
        cap = max(1.0, self.max_per_sec)
        while True:
            try:
                await asyncio.sleep(0.05)
                now = time.monotonic()
                self.tokens = min(cap, self.tokens + (now - self._refill) * self.max_per_sec)
                self._refill = now
                while self.max_per_sec > 0 and self.tokens >= 1:
                    item = self._next()
                    if item is None:
                        break
                    self.tokens -= 1
                    self._send(*item)
            except Exception as exc:
                Faults.note("return", exc)
                await asyncio.sleep(1)

    def _send(self, data, etype, uid):
        if Shared.from_mesh(data, uid):
            return                                # heard on the mesh while it waited
        if self.sock is None:                     # started without the mesh; try again at most every 3 s
            now = time.monotonic()
            if now - getattr(self, "_open_tried", 0) < 3:
                return
            self._open_tried = now
            try:
                self._open()
            except OSError:
                self.sock = None
                return                            # still no mesh: server traffic simply isn't relayed onto it
        Shared.mark(Shared.injected_uids, uid)
        try:
            dest = self.chat_dest if etype.startswith(CHAT_PREFIX) else self.dest
            out = bytes(data)
            if self.relay_ep and etype.startswith(b"a-") and b"<contact" in out:
                out = point_contact_at(out, self.relay_ep)   # mesh phones DM server people via the deck
            if len(out) > MESH_MAX_DATAGRAM:
                self.too_big = getattr(self, "too_big", 0) + 1
                if self.too_big <= 3 or self.too_big % 100 == 0:
                    log.info("Not sent to the mesh: %s is %d bytes, bigger than one mesh packet (%d so far)",
                             uid.decode(errors="replace"), len(out), self.too_big)
                return
            self.sock.sendto(out, dest)
        except OSError as exc:
            log.warning("Return path send failed: %s", exc)
            return
        self.sent += 1
        Stats.return_sent += 1
        Stats.return_last_ts = time.time()
        Stats.note("down", classify(etype))
        if self.sent % 100 == 1:
            log.info("Sent %d server events to the mesh (last uid %s)",
                     self.sent, uid.decode(errors="replace"))


class StatusWriter:
    """Write a small JSON status file every 2 s for the dashboard.

    This task must never return or raise: PyTAK stops everything when any task ends.
    """

    def __init__(self, cfg):
        self.cfg = cfg
        p = cfg.get("STATUS_FILE", "~/bridge_status.json").strip()
        self.path = None if p.lower() in ("off", "0", "no", "") else os.path.expanduser(p)
        self.boot_id = _boot_id()
        self.uplink = None
        Link.status_writer = self
        if self.path:                             # count reconnects since boot, remember why the last one ended
            try:
                with open(self.path) as f:
                    prev = json.load(f)
                pl = prev.get("link") or {}
                if self.boot_id and prev.get("boot_id") == self.boot_id:
                    Link.starts = int(pl.get("starts", 0)) + 1
                    Link.prev_reason = pl.get("reason") or ("bridge stopped or restarted" if pl else "")
                    Link.prev_down_ts = float(prev.get("now", 0))
            except (OSError, ValueError, TypeError, AttributeError):
                pass

    def snapshot(self):
        c = self.cfg
        now = time.time()
        Outbox.expire()                           # runs with every status write (every few seconds)
        return {
            "schema": 1,
            "pid": os.getpid(),
            "suite_version": suite_version(), "pytak": pytak_version(),
            "started": Stats.started,
            "now": now,
            "uptime": now - Stats.started,
            "cot_url": c.get("COT_URL", ""),
            "mesh_url": c.get("MESH_URL", DEFAULT_MESH_URL),
            "chat_url": c.get("MESH_CHAT_URL", DEFAULT_CHAT_URL),
            "local_addr": c.get("PYTAK_MULTICAST_LOCAL_ADDR", ""),
            "mesh": {"events": Stats.mesh_events, "last_ts": Stats.mesh_last_ts,
                     "last_from": Stats.mesh_last_from, "skipped": Stats.mesh_skipped,
                     "failed": Stats.mesh_failed, "control": Stats.mesh_control,
                     "malformed": Stats.mesh_malformed, "repaired": Stats.mesh_repaired, "ready": Stats.mesh_ready,
                     "waiting_since": Stats.mesh_wait_since},
            "return": {"enabled": Stats.return_enabled, "sent": Stats.return_sent,
                       "last_ts": Stats.return_last_ts},
            "server": {"rx_events": Stats.server_rx, "last_rx_ts": Stats.server_last_rx_ts},
            "per_minute": Stats.per_minute(),
            "boot_id": self.boot_id,
            "dm_route": DMSelfTest.status(), "outbox": Outbox.status(), "files": FileRelay.status(),
            "faults": Faults.status(now),
            "link": {"state": Link.state, "reason": Link.reason, "since": Link.connected_ts,
                     "ping_interval": Link.interval, "timeout": Link.timeout,
                     "pings": Link.pings, "pongs": Link.pongs, "answers_pings": Link.pong_seen,
                     "rtt_ms": Link.rtt_ms, "last_pong_ts": Link.last_pong_wall,
                     "starts": Link.starts, "prev_reason": Link.prev_reason, "prev_down_ts": Link.prev_down_ts},
            "uplink": {"min_interval": self.uplink.min_interval if self.uplink else 0,
                       "coalesced": Stats.up_coalesced, "dropped": Stats.up_dropped, "uid_capped": Stats.uid_capped,
                       "waiting": self.uplink.waiting() if self.uplink else 0},
            "return_fair": {"coalesced": Stats.return_coalesced, "dropped": Stats.return_dropped},
            # mesh phones' callsign by IP (voice: a VX phone sends from its ATAK's address)
            "mesh_peers": [{"cs": d["cs"], "ip": d["ip"]} for d in list(Peers.mesh.values()) if now - d["ts"] < Peers.KEEP][:200],
            # everyone's last position (Ops puts each VIDEO marker at the person streaming it)
            "people": [dict(d) for d in list(Peers.pos.values()) if now - d["ts"] < Peers.KEEP][:Peers.POS_MAX],
        }

    def write_once(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.snapshot(), f)
        os.replace(tmp, self.path)

    async def run(self, _=-1):
        if self.path:
            log.info("Status file for the dashboard: %s", self.path)
        warned = False
        while True:
            if self.path:
                try:
                    self.write_once()
                except Exception as exc:          # never let status writing stop the bridge
                    if not warned:
                        log.warning("Could not write status file %s: %s", self.path, exc)
                        warned = True
            await asyncio.sleep(2.0)


def ipaddress_is_multicast(host):
    try:
        return 224 <= int(host.split(".")[0]) <= 239
    except (ValueError, AttributeError):
        return False


PYTAK_TESTED = (7, 6)       # every test of this release ran on PyTAK 7.6.x (7.6.1)


_PYTAK_VERSION = None


def pytak_version():
    """The PyTAK version actually imported (looked up once; it can't change while running)."""
    global _PYTAK_VERSION
    if _PYTAK_VERSION is None:
        try:
            from importlib.metadata import version
            _PYTAK_VERSION = version("pytak")
        except Exception:
            _PYTAK_VERSION = getattr(pytak, "__version__", "") or ""
    return _PYTAK_VERSION


def pytak_version_guard(cfg):
    """The bridge reaches into PyTAK (its reader, sender and TLS settings). Older versions are missing what it uses
    (7.5 crashes at start); a newer one may have changed it. Stop with a clear message instead of failing in an odd
    way. PYTAK_ANY_VERSION = 1 in [bridge] skips this check (at your own risk)."""
    v = pytak_version()
    try:
        major_minor = tuple(int(x) for x in v.split(".")[:2])
    except ValueError:
        major_minor = None
    if major_minor == PYTAK_TESTED:
        log.info("PyTAK %s (tested version)", v)
        return
    fix = "reinstall TAK MESH TOC (its vendor/ folder has the tested PyTAK)"
    if cfg.get("PYTAK_ANY_VERSION", "0").strip().lower() in ("1", "on", "yes", "true"):
        log.warning("PyTAK %s is not the tested 7.6.x; starting anyway (PYTAK_ANY_VERSION = 1)", v or "?")
        return
    sys.exit("PyTAK %s is installed, but this bridge is tested only with PyTAK 7.6.x. Fix:  %s  then  "
             "meshtoc restart   (or set PYTAK_ANY_VERSION = 1 under [bridge] to try it anyway)" % (v or "?", fix))


def _pytak_reads_percent_specially():
    """PyTAK re-reads the TLS settings through its own settings reader, which treats "%" as special. Ask this
    PyTAK version directly (so a future version that stops doing it is not broken by the fix below)."""
    try:
        probe = configparser.ConfigParser(interpolation=None)
        probe["t"] = {"PYTAK_TLS_CLIENT_CERT": "a%%b"}
        return pytak.client_functions.get_tls_config(probe["t"]).get("PYTAK_TLS_CLIENT_CERT") == "a%b"
    except Exception:
        return False


def protect_tls_percent(cfg):
    """A TLS password like Tak%2024 must reach PyTAK unchanged: write each "%" as "%%", which PyTAK's reader turns
    back into "%". Only for the PYTAK_TLS_ settings, and only if this PyTAK needs it."""
    keys = [k for k in cfg if k.upper().startswith("PYTAK_TLS_") and "%" in (cfg.get(k) or "")]
    if keys and _pytak_reads_percent_specially():
        for k in keys:
            cfg[k] = cfg.get(k).replace("%", "%%")


TLS_PATH_KEYS = ("PYTAK_TLS_CLIENT_CERT", "PYTAK_TLS_CLIENT_KEY", "PYTAK_TLS_CLIENT_CAFILE")


def expand_tls_paths(cfg):
    """~/certs/deck.pem -> /home/<user>/certs/deck.pem. PyTAK opens these paths as written and doesn't know '~', so
    the paths in deck.ini.example, the README and the setup wizard only work once expanded here."""
    for k in TLS_PATH_KEYS:
        v = (cfg.get(k) or "").strip()
        if v.startswith("~"):
            cfg[k] = os.path.expanduser(v)


async def main(path):
    # deck.ini: a setting written twice = use the last one; ignore a Windows "BOM" marker; "%" is a plain character
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    if not parser.read(path, encoding="utf-8-sig"):
        sys.exit("Could not read config file: %s" % path)
    for note in apply_deck(parser, "bridge", "bridge"):
        log.warning("Config: %s", note)
    cfg = parser["bridge"]
    log.info("TAK MESH TOC %s, bridge starting (config %s)", suite_version(), path)
    MeshAddr.setup(parser, cfg)
    pytak_version_guard(cfg)
    expand_tls_paths(cfg)
    protect_tls_percent(cfg)
    if takproto is not None:
        log.info("takproto %s loaded: TAK Protocol (protobuf) mesh traffic is converted and forwarded",
                 getattr(takproto, "__version__", "?"))
    else:
        log.warning("takproto NOT installed: protobuf mesh traffic is skipped (fix: %s -m pip install takproto)",
                    sys.executable)

    # CLITool opens the connection to the TAK server (COT_URL) and runs the
    # TX worker that drains tx_queue into it. Its RX side just discards.
    LocalCopy.setup(cfg)
    Link.setup(cfg)
    Outbox.setup(cfg)
    DMSelfTest.setup(cfg)
    tool = pytak.CLITool(cfg)
    await tool.setup()
    Link.state, Link.connected_ts = "up", time.time()   # setup() opened the TCP connection
    identify = cfg.get("IDENTIFY", "1").strip().lower() not in ("0", "off", "no", "false")
    if identify:
        # Queued first, so it's the first CoT OTS sees. A dropped link ends the process and systemd restarts it,
        # so every connection starts with it.
        await tool.tx_queue.put(bridge_identity(cfg))
        log.info("Identifying to the server as %s", UID_RE.search(bridge_identity(cfg)).group(1).decode())
    tool.add_task(OutboxReplay(tool.tx_queue))      # chats still waiting from before the last lost link

    uplink = Uplink(cfg, tool.tx_queue)
    tool.add_task(uplink)
    bridge = MeshToServer(cfg, tool.tx_queue)
    bridge.uplink = uplink
    tool.add_task(bridge)
    if cfg.get("MESH_CHAT_URL", DEFAULT_CHAT_URL).strip().lower() not in ("off", "0", "no", ""):
        chat_in = MeshToServer(cfg, tool.tx_queue, "MESH_CHAT_URL", DEFAULT_CHAT_URL)
        chat_in.uplink = uplink
        tool.add_task(chat_in)
    tool.add_task(LinkWatch(cfg, tool.tx_queue))
    tool.add_task(DMSelfTest(cfg, tool.tx_queue))
    tool.add_task(ServerToMesh(cfg, tool.rx_queue))
    files = FileRelay(cfg, tool.tx_queue)
    if files.on:
        tool.add_task(files)
    if dm_relay_on(cfg):
        if dm_relay_endpoint(cfg):
            tool.add_task(DMRelay(cfg, tool.tx_queue))
        else:
            log.warning("DM_RELAY = 1 but PYTAK_MULTICAST_LOCAL_ADDR is not set; DM relay off")
    sw = StatusWriter(cfg)
    sw.uplink = uplink
    tool.add_task(sw)
    gps = None
    if cfg.get("GPS", "off").strip().lower() == "gpsd":
        host, _, port = cfg.get("GPS_HOST", "127.0.0.1:2947").partition(":")
        gps = GpsWatch(host or "127.0.0.1", port or 2947)
        gps.start()
        log.info("GPS: following gpsd at %s:%s (DECK's position for server maps)", host or "127.0.0.1", port or 2947)
    if identify:
        tool.add_task(Identity(cfg, tool.tx_queue, gps))
    ls = cfg.get("LOCAL_SEND", "off").strip()
    if ls.lower() not in ("", "off", "0", "no"):
        if urlparse(ls).hostname in ("127.0.0.1", "localhost") and urlparse(ls).port:
            tool.add_task(LocalSend(cfg, tool.tx_queue))
        else:
            log.warning("LOCAL_SEND must be udp://127.0.0.1:<port>; ignoring %r", ls)
    await tool.run()


if __name__ == "__main__":
    # PyTAK sets up logging its own way, which would hide this script's INFO lines from the
    # journal. Give the bridge its own handler so "Identifying...", "Return path ON" etc. show.
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("[%(levelname)s] bridge: %(message)s"))
    log.addHandler(_h)
    log.setLevel(logging.INFO)
    log.propagate = False
    if len(sys.argv) != 2:
        sys.exit("usage: tak_mesh_bridge.py deck.ini")
    asyncio.run(main(sys.argv[1]))
