#!/usr/bin/env python3
"""manet_voice.py - TAK MESH TOC voice: OpenMANET push-to-talk talk groups <-> Mumble channels.

  mesh -> Mumble   RTP/Opus passed through unchanged (both are Opus 48 kHz mono: no re-encoding, no delay).
  Mumble -> mesh   decoded and re-encoded the way OpenMANET sends it (20 ms frames, 32 kbps, FEC, payload 111).
  one talker       per talk group, until HANG_MS of silence (MAX_TALK_S at most); Mumble users who talk over
                   the mesh get "Channel busy".
  no echo, no backlog: audio never returns to its source, and late audio is dropped, never replayed.
  monitoring       every talk group port is watched; TALKGROUPS are bridged from the start, the rest can be
                   switched on from the Voice page. The page can also play one talk group on the deck.
  PTT box          a PTT box plugged into the deck (DECK_PTT = auto) talks on the talk group picked on the Voice
                   page (mesh, plus Mumble when bridged) and plays that talk group in its headset (deckptt.py).
Config: [voice] in deck.ini. Standard library plus the system's libopus.
"""
import argparse
import configparser
import json
import logging
import os
import queue
import random
import select
import selectors
import socket
import struct
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.join(HERE, "..", "common")]
import opuscodec                       # noqa: E402
from mumbleclient import MumbleClient  # noqa: E402
from deckptt import DeckPTT, DECK_NAME  # noqa: E402
try:
    from deckcommon import apply_deck, iface_ipv4, mesh_iface, is_auto, watch_mesh_addr  # noqa: E402
except ImportError:                    # running from a bare copy of voice/
    apply_deck = None

log = logging.getLogger("manet_voice")
PT_OPUS = 111
PRESENT_S = 15.0                       # seen within this long = "on the talk group" (RTCP comes every 5 s)
DEFAULTS = {
    "VOICE": "off",
    "MESH_GROUP": "239.192.41.1",      # OpenMANET's talk group address (all talk groups share it)
    "MESH_PORTS": "38801,38803,38805,38807,38809",   # OpenMANET talk groups 1-5: all are watched
    "TALKGROUPS": "38801:TG1",         # bridged from the start: port:Mumble channel, comma separated
    "MESH_ADDR": "",
    "MESH_TTL": "32",
    "MESH_LOOP": "0",                  # 1 only for tests on one machine
    "MESH_PAYLOAD_TYPES": "111,0",     # RTP labels accepted as Opus: 111 OpenMANET, 0 ATAK VX plugin
    "MUMBLE": "",                      # host:port, normally over Tailscale
    "MUMBLE_USER": "DECK-voice",
    "MUMBLE_PASSWORD": "",
    "MUMBLE_CERT_SHA256": "",
    "HANG_MS": "300",
    "MAX_TALK_S": "60",
    "STATUS_FILE": "~/voice_status.json",
    "BRIDGE_STATUS_FILE": "~/bridge_status.json",
    "BRIDGE_STATE_FILE": "~/voice_bridges.json",   # talk groups switched on/off on the Voice page
    "CONTROL": "127.0.0.1:17016",      # for Chat only: listen on deck, bridge on/off. Loopback; off = none
    "DECK_PTT": "auto",                # PTT box on the deck: auto = used whenever it is plugged in; off = never
    "DECK_PTT_TG": "",                 # its talk group until one is picked on the Voice page (blank = the first)
    "DECK_PTT_STATE_FILE": "~/voice_deckptt.json",   # the talk group picked on the Voice page
}
MAX_LISTENERS = 4
SILENCE = bytes(opuscodec.FRAME * 2)


def parse_rtp(pkt):
    """RTP packet -> (payload type, seq, timestamp, ssrc, marker, payload) or None."""
    if len(pkt) < 12 or (pkt[0] >> 6) != 2:
        return None
    cc, ext, pad = pkt[0] & 0x0F, pkt[0] & 0x10, pkt[0] & 0x20
    pt, marker = pkt[1] & 0x7F, bool(pkt[1] & 0x80)
    seq, ts, ssrc = struct.unpack(">HII", pkt[2:12])
    i = 12 + 4 * cc
    if ext:
        if len(pkt) < i + 4:
            return None
        i += 4 + 4 * struct.unpack(">H", pkt[i + 2:i + 4])[0]
    end = len(pkt) - (pkt[-1] if pad else 0)
    if i > end:
        return None
    return pt, seq, ts, ssrc, marker, pkt[i:end]


def build_rtp(seq, ts, ssrc, payload, marker=False):
    return struct.pack(">BBHII", 0x80, (0x80 if marker else 0) | PT_OPUS, seq & 0xFFFF, ts & 0xFFFFFFFF, ssrc) + payload


def hostport(s):
    h, _, p = s.strip().rpartition(":")
    return h.strip("[]"), int(p)


def tg_name(port):
    return "TG%d" % ((port - 38801) // 2 + 1) if 38801 <= port <= 38809 and port % 2 else str(port)


def parse_talkgroups(spec):
    """'38801:TG1, 38803:Ops' -> {38801: 'TG1', 38803: 'Ops'}"""
    out = {}
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        port, _, chan = part.partition(":")
        out[int(port)] = chan.strip() or tg_name(int(port))
    return out


class Names:
    """Mesh IP -> ATAK callsign, from the bridge's status file (a VX phone has the same IP as its ATAK)."""

    def __init__(self, path):
        self.path = os.path.expanduser(path)
        self.map, self.loaded = {}, 0.0

    def get(self, ip):
        if time.time() - self.loaded > 10:
            self.loaded = time.time()
            try:
                with open(self.path) as f:
                    peers = json.load(f).get("mesh_peers") or []
                self.map = {p["ip"]: p["cs"] for p in peers if p.get("ip") and p.get("cs")}
            except (OSError, ValueError, TypeError, KeyError):
                pass
        return self.map.get(ip)


class TalkGroup:
    def __init__(self, gw, port, channel, default):
        self.gw, self.port, self.channel, self.default = gw, port, channel, default
        self.name = tg_name(port)
        self.bridged = False           # set_bridged() switches it (and the Mumble connection) on and off
        self.mstop = threading.Event()
        self.ssrc = random.getrandbits(32)
        self.seq, self.ts = random.getrandbits(16), random.getrandbits(32)
        self.lock = threading.Lock()
        self.floor = None              # ("mesh", ssrc) | ("mumble", session) | ("deck", 0): the PTT box
        self.floor_name = self.floor_via = ""
        self.floor_last = self.floor_since = 0.0
        self.blocked = {}              # who -> until (held too long: ignored until they pause)
        self.busy_told = {}
        self.present = {}              # ip -> last seen
        self.ssrc_ip = {}
        self.mesh_seq = {}
        self.enc = None
        self.decoders = {}
        self.listeners = []            # queues of PCM for "listen on deck"
        self.ldec = {}                 # ssrc -> decoder, only while someone listens
        self.pcm = b""
        self.first_out = True
        self.client = None
        self.log = []                  # recent transmissions, newest first
        self.stats = {"mesh_rx": 0, "mesh_to_mumble": 0, "mumble_rx": 0, "mumble_to_mesh": 0, "dropped_busy": 0,
                      "mesh_lost": 0, "ignored_own": 0, "bad_rtp": 0, "transmissions": 0, "cut_long": 0,
                      "deck_rx": 0, "deck_to_mesh": 0, "deck_to_mumble": 0}

    # ---- floor
    def _take(self, who, name, via):
        now = time.monotonic()
        with self.lock:
            if self.floor is not None and now - self.floor_last > self.gw.hang:
                self._release_locked()
            until = self.blocked.get(who)
            if until is not None:
                if now < until:
                    self.blocked[who] = now + self.gw.hang      # still talking: stay blocked until a pause
                    return False
                del self.blocked[who]
            if self.floor is None:
                self.floor, self.floor_name, self.floor_via, self.floor_since = who, name, via, now
                self.first_out = True
                self.stats["transmissions"] += 1
                log.info("%s: %s talking (%s)", self.name, name, via)
            if self.floor != who:
                return False
            if now - self.floor_since > self.gw.max_talk:
                self.stats["cut_long"] += 1
                log.warning("%s: %s held the channel over %.0f s: released", self.name, name, self.gw.max_talk)
                self._release_locked(note="cut off after %.0f s" % self.gw.max_talk)
                self.blocked[who] = now + self.gw.hang
                return False
            self.floor_last = now
            return True

    def _release_locked(self, note=""):
        if self.floor is None:
            return
        kind = self.floor[0]
        dur = time.monotonic() - self.floor_since
        self._note(self.floor_name + (" (%s)" % note if note else ""), self.floor_via, dur)
        log.info("%s: %s done (%.1f s)", self.name, self.floor_name, dur)
        self.floor, self.floor_name, self.floor_via = None, "", ""
        self.pcm = b""
        if kind in ("mesh", "deck") and self.client and not self.client.closed.is_set():
            try:
                self.client.end_voice()
            except OSError:
                pass

    def _note(self, who, via, dur, blocked=False):
        self.log.insert(0, {"ts": time.time(), "who": who, "via": via, "len": round(dur, 1), "blocked": blocked})
        del self.log[30:]

    # ---- bridge on/off
    def set_bridged(self, on):
        with self.lock:
            if on == self.bridged:
                return
            self.bridged = on
            if on:
                self.enc = self.enc or opuscodec.Encoder()
                self.mstop = threading.Event()
                stop = self.mstop
            else:
                self.mstop.set()
                if self.floor and self.floor[0] == "mumble":
                    self._release_locked()
        if on:
            threading.Thread(target=self.mumble_loop, args=(stop,), daemon=True).start()
            log.info("%s: bridged to Mumble channel %s", self.name, self.channel)
        else:
            c, self.client = self.client, None
            if c:
                c.close()
            log.info("%s: no longer bridged to Mumble", self.name)

    # ---- listen on deck
    def feed(self, pcm, skip=None):
        for q in list(self.listeners):
            if q is skip:
                continue                            # the PTT box headset never hears its own voice
            try:
                q.put_nowait(pcm)
            except queue.Full:
                pass                                # a slow listener loses audio; voice never waits

    def tick(self):
        with self.lock:
            if self.floor is not None and time.monotonic() - self.floor_last > self.gw.hang:
                self._release_locked()

    # ---- mesh side
    def on_mesh(self, pkt, ip):
        r = parse_rtp(pkt)
        if not r:
            self.stats["bad_rtp"] += 1
            return
        pt, seq, ts, ssrc, marker, payload = r
        if ssrc == self.ssrc:
            self.stats["ignored_own"] += 1
            return
        if pt not in self.gw.accept_pt or not opuscodec.valid_packet(payload):
            self.stats["bad_rtp"] += 1
            return
        self.stats["mesh_rx"] += 1
        self.present[ip] = time.time()
        self.ssrc_ip[ssrc] = ip
        last = self.mesh_seq.get(ssrc)
        gap = ((seq - last) & 0xFFFF) - 1 if last is not None else 0
        self.mesh_seq[ssrc] = seq
        if 0 < gap < 50:
            self.stats["mesh_lost"] += gap
        name = self.gw.names.get(ip) or ip
        if not self._take(("mesh", ssrc), name, "mesh"):
            self.stats["dropped_busy"] += 1
            return
        if self.listeners:
            d = self.ldec.get(ssrc)
            if d is None:
                d = self.ldec[ssrc] = opuscodec.Decoder()
            try:
                self.feed(d.decode(payload))
            except opuscodec.OpusError:
                pass
        elif self.ldec:
            self.ldec.clear()
        c = self.client
        if not self.bridged or c is None or c.closed.is_set():
            return                                  # monitor only, or Mumble down: never queue voice
        try:
            if 0 < gap < 50:
                c._seq += 2 * gap
            c.send_voice(payload, frames=max(1, opuscodec.packet_samples(payload) // 480))
            self.stats["mesh_to_mumble"] += 1
        except OSError:
            pass

    def on_rtcp(self, pkt, ip):
        if len(pkt) >= 8 and (pkt[0] >> 6) == 2 and 200 <= pkt[1] <= 204:
            ssrc = struct.unpack(">I", pkt[4:8])[0]
            if ssrc != self.ssrc:
                self.present[ip] = time.time()

    # ---- Mumble side
    def on_mumble(self, session, name, opus, last):
        if not self.bridged:
            return
        self.stats["mumble_rx"] += 1
        if not self._take(("mumble", session), name, "Mumble"):
            self.stats["dropped_busy"] += 1
            now = time.monotonic()
            if now - self.busy_told.get(session, 0) > 5 and self.client:
                self.busy_told[session] = now
                if self.floor and self.floor[0] == "mesh":
                    self._note("%s (blocked: %s talking)" % (name, self.floor_name), "Mumble", 0, blocked=True)
                try:
                    self.client.text("Channel busy: %s is talking" % (self.floor_name or "someone"), session=session)
                except OSError:
                    pass
            return
        dec = self.decoders.get(session)
        if dec is None:
            dec = self.decoders[session] = opuscodec.Decoder()
        if opus:
            try:
                self.pcm += dec.decode(opus)
            except opuscodec.OpusError:
                return
        while len(self.pcm) >= opuscodec.FRAME * 2:
            frame, self.pcm = self.pcm[:opuscodec.FRAME * 2], self.pcm[opuscodec.FRAME * 2:]
            self._send_mesh(frame)
        if last:
            if self.pcm:
                self._send_mesh(self.pcm + bytes(opuscodec.FRAME * 2 - len(self.pcm)))
            with self.lock:
                self._release_locked()

    def _send_mesh(self, frame):
        if self.listeners:
            self.feed(frame)
        self._send_rtp(self.enc.encode(frame), "mumble_to_mesh")

    def _send_rtp(self, opus, stat):
        pkt = build_rtp(self.seq, self.ts, self.ssrc, opus, marker=self.first_out)
        self.first_out = False
        self.seq = (self.seq + 1) & 0xFFFF
        self.ts = (self.ts + opuscodec.FRAME) & 0xFFFFFFFF
        try:
            self.gw.tx.sendto(pkt, (self.gw.group, self.port))
            self.stats[stat] += 1
        except OSError as e:
            log.warning("%s: mesh send failed: %s", self.name, e)

    # ---- the PTT box on the deck
    def on_deck(self, frame, last=False):
        """One 20 ms frame (16-bit 48 kHz mono) from the PTT box mic; None with last=True when PTT is let go.
        Goes to the mesh, and to Mumble when bridged. False = someone else holds the channel."""
        if frame is not None:
            if not self._take(("deck", 0), DECK_NAME, "deck"):
                self.stats["dropped_busy"] += 1
                return False
            self.stats["deck_rx"] += 1
            if self.enc is None:
                self.enc = opuscodec.Encoder()
            opus = self.enc.encode(frame)
            if self.listeners:
                self.feed(frame, skip=self.gw.deck.monitor_q if self.gw.deck else None)
            self._send_rtp(opus, "deck_to_mesh")
            c = self.client
            if self.bridged and c is not None and not c.closed.is_set():
                try:
                    c.send_voice(opus, frames=2)
                    self.stats["deck_to_mumble"] += 1
                except OSError:
                    pass
        if last:
            with self.lock:
                if self.floor == ("deck", 0):
                    self._release_locked()
        return True

    # ---- Mumble connection, kept up
    def mumble_loop(self, stop):
        host, port = hostport(self.gw.cfg["MUMBLE"])
        delay = 2
        user = self.gw.cfg["MUMBLE_USER"] + ("" if self.gw.first_bridged == self.port else "-" + self.channel)
        while not self.gw.stop.is_set() and not stop.is_set():
            c = MumbleClient(host, port, user, self.gw.cfg["MUMBLE_PASSWORD"], self.channel,
                             pin_sha256=self.gw.cfg["MUMBLE_CERT_SHA256"], on_voice=self.on_mumble,
                             log=lambda m: log.warning("%s: %s", self.name, m))
            try:
                c.connect()
                if stop.is_set():
                    c.close()
                    return
                if c.channels.get(c.my_channel, "").lower() != self.channel.lower():
                    log.warning("%s: no Mumble channel %r yet (create it: voice/make_channel.py); waiting in %r",
                                self.name, self.channel, c.channels.get(c.my_channel, "Root"))
                self.client = c
                delay = 2
                log.info("%s: Mumble connected to %s:%d as %s in %s", self.name, host, port, user, c.channels.get(c.my_channel, "?"))
                while not c.closed.wait(1) and not self.gw.stop.is_set() and not stop.is_set():
                    if c.channels.get(c.my_channel, "").lower() != self.channel.lower() and \
                            any(n.lower() == self.channel.lower() for n in list(c.channels.values())):
                        c.join(self.channel)           # the channel was made while we waited: go in
            except Exception as e:
                log.warning("%s: Mumble: %s; retrying in %d s", self.name, e, delay)
            if self.client is c:
                self.client = None
            c.close()
            if stop.wait(delay) or self.gw.stop.is_set():
                return
            delay = min(delay * 2, 30)

    def status(self):
        now = time.time()
        c = self.client
        talking = None
        if self.floor is not None:
            talking = {"name": self.floor_name, "via": self.floor_via, "secs": round(time.monotonic() - self.floor_since, 1)}
        mesh = []
        for ip, ts in sorted(self.present.items()):
            if now - ts > PRESENT_S:
                continue
            cs = self.gw.names.get(ip)
            mesh.append({"name": cs or ip, "ip": ip, "what": ("ATAK phone" if cs else "node or device"),
                         "talking": bool(talking and talking["via"] == "mesh" and self.floor and
                                         self.ssrc_ip.get(self.floor[1]) == ip)})
        mumble = []
        if c and not c.closed.is_set():
            for s, u in list(c.users.items()):
                if s != c.session and u.get("channel") == c.my_channel:
                    mumble.append({"name": u.get("name", "?"), "talking": bool(talking and self.floor == ("mumble", s))})
        return {"name": self.name, "port": self.port, "channel": self.channel, "bridged": self.bridged,
                "default": self.default, "listeners": len(self.listeners),
                "mumble_up": bool(c and not c.closed.is_set()),
                "in_channel": bool(c and c.channels.get(c.my_channel, "").lower() == (self.channel or "").lower()),
                "rtt_ms": round(c.rtt_ms) if c and c.rtt_ms is not None else None,
                "talking": talking, "mesh": mesh, "mumble": mumble, "log": self.log[:12], "stats": dict(self.stats)}


class Gateway:
    def __init__(self, cfg):
        self.cfg = cfg
        self.group = cfg["MESH_GROUP"].strip()
        self.mesh_addr = cfg["MESH_ADDR"].strip()
        self.hang = int(cfg["HANG_MS"]) / 1000.0
        self.max_talk = float(cfg["MAX_TALK_S"])
        self.accept_pt = {int(x) for x in cfg["MESH_PAYLOAD_TYPES"].replace(" ", "").split(",") if x.isdigit()}
        self.names = Names(cfg["BRIDGE_STATUS_FILE"])
        self.stop = threading.Event()
        bridged = parse_talkgroups(cfg["TALKGROUPS"])
        ports = [int(p) for p in cfg["MESH_PORTS"].replace(" ", "").split(",") if p.isdigit()]
        for p in bridged:
            if p not in ports:
                ports.append(p)
        self.first_bridged = min(bridged) if bridged else None
        self.groups = [TalkGroup(self, p, bridged.get(p) or tg_name(p), p in bridged) for p in sorted(ports)]
        self.state_file = os.path.expanduser(cfg["BRIDGE_STATE_FILE"])
        self.wanted = {g.port: g.default for g in self.groups}
        try:
            with open(self.state_file) as f:
                saved = json.load(f)
            for g in self.groups:
                if isinstance(saved.get(str(g.port)), bool):
                    self.wanted[g.port] = saved[str(g.port)]
        except (OSError, ValueError, AttributeError):
            pass
        self.deck = None
        if cfg.get("DECK_PTT", "auto").strip().lower() not in ("off", "0", "no", "false", ""):
            self.deck = DeckPTT(self, cfg)
        self.sel = selectors.DefaultSelector()
        tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        tx.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(self.mesh_addr))
        tx.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, int(cfg["MESH_TTL"]))
        tx.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1 if cfg["MESH_LOOP"] == "1" else 0)
        self.tx = tx
        for g in self.groups:
            self.sel.register(self._rx(g.port), selectors.EVENT_READ, (g, "rtp"))
            self.sel.register(self._rx(g.port + 1), selectors.EVENT_READ, (g, "rtcp"))

    def _rx(self, port):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("", port))
        s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                     struct.pack("4s4s", socket.inet_aton(self.group), socket.inet_aton(self.mesh_addr)))
        s.setblocking(False)
        return s

    def mesh_loop(self):
        while not self.stop.is_set():
            for key, _ in self.sel.select(timeout=0.1):
                g, kind = key.data
                try:
                    pkt, src = key.fileobj.recvfrom(4096)
                except OSError:
                    continue
                (g.on_mesh if kind == "rtp" else g.on_rtcp)(pkt, src[0])
            for g in self.groups:
                g.tick()

    def write_status(self):
        path = os.path.expanduser(self.cfg["STATUS_FILE"])
        st = {"schema": 1, "ts": time.time(), "mesh_group": self.group, "mumble": self.cfg["MUMBLE"],
              "groups": [g.status() for g in self.groups],
              "deck_ptt": self.deck.status() if self.deck else {"enabled": False}}
        try:
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(st, f)
            os.replace(tmp, path)
        except OSError as e:
            log.debug("status file: %s", e)

    def status_loop(self):
        while not self.stop.wait(1):
            self.write_status()

    def set_bridged(self, name, on):
        g = next((x for x in self.groups if x.name.lower() == (name or "").lower()), None)
        if g is None:
            return None
        g.set_bridged(bool(on))
        self.wanted[g.port] = g.bridged
        try:
            tmp = self.state_file + ".tmp"
            with open(tmp, "w") as f:
                json.dump({str(p): v for p, v in self.wanted.items()}, f)
            os.replace(tmp, self.state_file)
        except OSError as e:
            log.warning("could not save the bridge switches to %s: %s", self.state_file, e)
        self.write_status()
        return g

    def run(self):
        for g in self.groups:
            if self.wanted[g.port]:
                g.set_bridged(True)
        threading.Thread(target=self.status_loop, daemon=True).start()
        if self.deck:
            threading.Thread(target=self.deck.run, daemon=True).start()
        if self.cfg["CONTROL"].strip().lower() not in ("", "off", "0", "no"):
            try:
                self.control = control_server(self)
                threading.Thread(target=self.control.serve_forever, daemon=True).start()
            except OSError as e:
                log.warning("control port %s: %s (no listening on deck, no bridge switches)", self.cfg["CONTROL"], e)
        self.mesh_loop()

    def close(self):
        self.stop.set()
        if self.deck:
            self.deck.close()
        if getattr(self, "control", None):
            self.control.shutdown()
            self.control.server_close()
        for g in self.groups:
            g.mstop.set()
            if g.client:
                g.client.close()
        for key in list(self.sel.get_map().values()):
            key.fileobj.close()
        self.tx.close()


def gone(sock):
    """True once the other end has closed (it never sends us anything, so readable = closed)."""
    try:
        r, _, _ = select.select([sock], [], [], 0)
        return bool(r) and sock.recv(1, socket.MSG_PEEK) == b""
    except (OSError, ValueError):
        return True


def control_server(gw):
    """Loopback HTTP for Chat: GET /listen?tg=TG1 (raw 16-bit 48 kHz mono, live), POST /bridge {tg, on},
    POST /deckptt {tg}: the talk group the PTT box talks on."""
    host, port = hostport(gw.cfg["CONTROL"])
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise OSError("CONTROL must be a loopback address (127.0.0.1:port)")

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, code, obj):
            b = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            u = urlparse(self.path)
            if u.path != "/listen":
                return self._json(404, {"error": "not found"})
            name = parse_qs(u.query).get("tg", [""])[0]
            g = next((x for x in gw.groups if x.name.lower() == name.lower()), None)
            if g is None:
                return self._json(404, {"error": "no talk group %s" % name[:16]})
            if sum(len(x.listeners) for x in gw.groups) >= MAX_LISTENERS:
                return self._json(429, {"error": "too many listeners"})
            q = queue.Queue(maxsize=50)                 # 1 s of audio at most
            g.listeners.append(q)
            log.info("%s: listening on deck", g.name)
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                idle = 0.0
                while not gw.stop.is_set():
                    try:
                        self.wfile.write(q.get(timeout=0.5))
                        idle = 0.0
                    except queue.Empty:
                        idle += 0.5
                        if gone(self.connection):        # the page closed or stopped listening
                            break
                        if idle >= 2:                   # keep-alive for anything in between
                            self.wfile.write(SILENCE)
                            idle = 0.0
                    self.wfile.flush()
            except (OSError, ValueError):
                pass
            finally:
                g.listeners.remove(q)
                log.info("%s: stopped listening on deck", g.name)

        def do_POST(self):
            path = urlparse(self.path).path
            if path == "/deckptt":
                if gw.deck is None:
                    return self._json(409, {"ok": False, "error": "the PTT box is off (DECK_PTT = off in [voice])"})
                try:
                    body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length", "0")), 4096)) or b"{}")
                    name = str(body["tg"])
                except (ValueError, KeyError, TypeError):
                    return self._json(400, {"ok": False, "error": "need {\"tg\": \"TG2\"}"})
                g = gw.deck.set_tg(name)
                if g is None:
                    return self._json(404, {"ok": False, "error": "no talk group %s" % name[:16]})
                gw.write_status()
                return self._json(200, {"ok": True, "tg": g.name})
            if path != "/bridge":
                return self._json(404, {"error": "not found"})
            try:
                body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length", "0")), 4096)) or b"{}")
                name, on = str(body["tg"]), body["on"]
                if not isinstance(on, bool):
                    raise ValueError
            except (ValueError, KeyError, TypeError):
                return self._json(400, {"ok": False, "error": "need {\"tg\": \"TG2\", \"on\": true}"})
            g = gw.set_bridged(name, on)
            if g is None:
                return self._json(404, {"ok": False, "error": "no talk group %s" % name[:16]})
            return self._json(200, {"ok": True, "tg": g.name, "bridged": g.bridged, "channel": g.channel})

    srv = ThreadingHTTPServer((host, port), H)
    srv.daemon_threads = True
    return srv


def load_cfg(path):
    cp = configparser.ConfigParser(interpolation=None, strict=False)
    cp.optionxform = str
    cp.read_dict({"voice": DEFAULTS})
    if path:
        cp.read(os.path.expanduser(path), encoding="utf-8-sig")
    if apply_deck:
        for note in apply_deck(cp, "voice", "voice", DEFAULTS):
            log.warning("Config: %s", note)
    out = dict(cp["voice"])
    if apply_deck:
        out["_MESH_IFACE"] = mesh_iface(cp, "voice")
    return out


def resolve_mesh_addr(cfg):
    """MESH_ADDR = auto: wait for the mesh port's address, use it, and restart (systemd starts Voice again) when
    the node hands out a new one. A fixed address is used as it is."""
    if not apply_deck or not is_auto(cfg["MESH_ADDR"]):
        return
    iface, told = cfg.get("_MESH_IFACE", "eth0"), False
    while True:
        ip = iface_ipv4(iface)
        if ip:
            break
        if not told:
            log.warning("MESH_ADDR auto: %s has no address yet (node unplugged or off?); waiting", iface)
            told = True
        time.sleep(3)
    cfg["MESH_ADDR"] = ip
    log.info("MESH_ADDR auto: %s on %s (followed if the node hands out a new one)", ip, iface)

    def changed(old, new):
        log.warning("Mesh address on %s is now %s (was %s): restarting Voice to follow it", iface, new, old)
        logging.shutdown()
        os._exit(3)
    watch_mesh_addr(iface, ip, changed)


def main():
    ap = argparse.ArgumentParser(description="OpenMANET talk groups <-> Mumble channels")
    ap.add_argument("--config", default=os.path.expanduser("~/manet_mesh_bridge/deck.ini"))
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_cfg(a.config)
    if cfg["VOICE"].strip().lower() not in ("1", "on", "yes", "true"):
        log.info("VOICE is off in [voice]: nothing to do (set VOICE = on, then meshtoc restart)")
        return
    if opuscodec.LOAD_ERROR:
        sys.exit(opuscodec.LOAD_ERROR)
    missing = [k for k in ("MESH_ADDR", "MUMBLE", "MESH_GROUP") if not cfg[k].strip()]
    if missing:
        sys.exit("[voice] needs %s" % ", ".join(missing))
    resolve_mesh_addr(cfg)
    if not cfg["MUMBLE_CERT_SHA256"].strip():
        log.warning("MUMBLE_CERT_SHA256 is empty: the Mumble server's certificate is not checked")
    g = Gateway(cfg)
    log.info("voice: mesh %s on %s, watching %s; bridging %s to Mumble %s", g.group, g.mesh_addr,
             ", ".join(t.name for t in g.groups), ", ".join("%s<->%s" % (t.name, t.channel) for t in g.groups if g.wanted[t.port]) or "nothing",
             cfg["MUMBLE"])
    try:
        g.run()
    except KeyboardInterrupt:
        g.close()


if __name__ == "__main__":
    main()
