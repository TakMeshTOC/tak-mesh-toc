#!/usr/bin/env python3
"""dashboard_server.py - TAK MESH TOC Ops: the deck's status page and JSON feed.

Watches the bridge (its status file and service), the server link, MediaMTX video, the mesh nodes, GPS, voice and
the deck itself; keeps an event log, trends and alert rules; and lists what still needs setting up.
Read-only, except two controls (restart the bridge, snapshot a stream) that answer the deck's own screen only.
Standard library only.

    python3 dashboard_server.py --config ~/manet_mesh_bridge/deck.ini
    python3 dashboard_server.py --once           (one status snapshot as JSON)
    python3 dashboard_server.py --mock auto      (demo data cycling through every state; or hop, sleep, alert, down)
"""
import argparse
import configparser
import json
import math
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse, urlsplit
from urllib.request import urlopen

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import setup_wizard as wiz  # noqa: E402

DEFAULTS = {
    "PORT": "8080",
    "LISTEN": "auto",                 # auto = 127.0.0.1 + the mesh-side (eth0) address. Or a list: 127.0.0.1,10.41.0.50  (0.0.0.0 = everywhere)
    "MESH_IFACE": "eth0",
    "MTU_EXPECTED": "1460",
    "BRIDGE_SERVICE": "manet-bridge",
    "BRIDGE_STATUS_FILE": "~/bridge_status.json",
    "VOICE_STATUS_FILE": "~/voice_status.json",   # written by manet-voice: the voice row in MESH + DECK
    "BRIDGE_INI": "",                 # blank = the config file this dashboard was started with (deck.ini)
    "SERVER_HOST": "",                # blank = take it from COT_URL in [bridge]
    "SERVER_PORT": "",
    "MEDIAMTX_API": "http://127.0.0.1:9997",
    "MEDIAMTX_RTSP_PORT": "8554",
    "MEDIAMTX_WEBRTC_PORT": "8889",
    "MEDIAMTX_HLS_PORT": "8888",
    "DEFAULT_STREAM": "live",         # path offered in the player when the API is off
    "VIDEO_MARKERS": "mesh",          # mesh = each stream (live or on demand) as a tap-to-play marker on the mesh map; off = none
    "VIDEO_MARKER_REFRESH": "180",    # seconds between repeats (a phone that joins sees them within this time)
    "VIDEO_MARKER_STALE": "600",      # markers vanish this long after the deck stops sending (deck off or out of range)
    "VIDEO_MARKER_LAT": "",           # where to put them without a GPS fix (blank = wait for GPS)
    "VIDEO_SOURCE_CHECK": "20",       # seconds between "is it streaming?" checks of on-demand sources (marker only while live)
    "VIDEO_RELAY": "off",             # auto = copy mesh phones' streams up to the TAK server's MediaMTX (port 8554 on the
                                      # COT_URL host) and show them to server users as VIDEO markers; or rtsp://host:8554
    "VIDEO_RELAY_SEND": "127.0.0.1:17014",   # the bridge's LOCAL_SEND port (server-only) for the server-side markers
    "VIDEO_OWNERS": "",               # pair streams with callsigns: RAVEN21: RAVEN 21, drone1: EAGLE 6 (else: by IP, then by name)
    "VIDEO_MARKER_LON": "",
    "NODE_NAME": "mesh node",         # names for the NODE_IP addresses, in the same order (comma separated)
    "NODE_IP": "",                    # the mesh node(s) to ping, comma separated; blank = find them on MESH_IFACE
    "MESH_DISCOVERY": "alfred",       # alfred = also list every node the mesh itself knows (OpenMANET gossip, no login); off = NODE_IP only
    "MESH_STATUS_FILE": "~/mesh_status.json",   # mesh summary for the bridge's DECK status line
    "LINK_GOOD_MBPS": "3",            # radio link grades: GOOD at or above this ...
    "LINK_POOR_MBPS": "1",            # ... POOR below this, OK in between
    "EVENT_FILE": "~/dashboard_events.jsonl",
    "INTERNET_HOST": "1.1.1.1",
    "INTERNET_PORT": "443",
    "HISTORY_FILE": "~/dashboard_history.jsonl",
    "ALERTS": "on",                   # off = no alert rules at all. Rules: ALERT_1 = mesh_loss > 25 for 60 warn mesh
    "CONTROL": "local",               # local = restart/capture buttons work from the deck's own screen. off = read-only.
    "BRIDGE_UNIT": "",                # blank = BRIDGE_SERVICE
    "CAPTURE_DIR": "~/dashboard_captures",
    "MIN_FREE_MB": "500",             # refuse to snapshot when less than this is free
    "STALL_SECONDS": "4",
    "GPS": "gpsd",                    # gpsd = GPS tile from the GPS puck; off = no tile data
    "GPS_HOST": "127.0.0.1:2947",
    "MGRS_DIGITS": "5",               # 5 = 10-digit grid (1 m), 4 = 8-digit, 3 = 6-digit             # a live stream with no data for this long shows as STALLED (MediaMTX drops it after ~10 s)
}

START = time.time()


def load_config(path):
    # deck.ini: a setting written twice = use the last one; ignore a Windows "BOM" marker; "%" is a plain character
    cp = configparser.ConfigParser(interpolation=None, strict=False)
    cands = [path] if path else [os.path.expanduser("~/manet_mesh_bridge/deck.ini")]
    used = None
    for c in cands:
        if c and os.path.exists(c):
            cp.read(c, encoding="utf-8-sig")
            used = c
            break
    cfg = dict(DEFAULTS)
    notes = apply_deck(cp, "dashboard", "dashboard")
    for note in notes:
        sys.stderr.write("config: %s\n" % note)
    if cp.has_section("dashboard"):
        for k, v in cp["dashboard"].items():
            cfg[k.upper()] = v.strip()
    if not cfg["BRIDGE_INI"]:
        cfg["BRIDGE_INI"] = used or os.path.expanduser("~/manet_mesh_bridge/deck.ini")
    for k in ("BRIDGE_STATUS_FILE", "VOICE_STATUS_FILE", "BRIDGE_INI", "EVENT_FILE", "HISTORY_FILE", "CAPTURE_DIR", "MESH_STATUS_FILE"):
        cfg[k] = os.path.expanduser(cfg[k])
    if not cfg["BRIDGE_UNIT"]:
        cfg["BRIDGE_UNIT"] = cfg["BRIDGE_SERVICE"]
    cfg["_config_file"] = used or "(defaults)"
    return cfg


# ----------------------------------------------------------------- helpers
def run(cmd, timeout=2.5):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout
    except Exception:
        return 127, ""


def tcp_check(host, port, timeout=2.0):
    """Return connect time in ms, or None."""
    t0 = time.time()
    try:
        s = socket.create_connection((host, int(port)), timeout=timeout)
        s.close()
        return round((time.time() - t0) * 1000.0, 1)
    except Exception:
        return None


def icmp_ping(host, timeout=1):
    """Round-trip ms from one ICMP ping, or None. Never opens a TCP connection."""
    if not shutil.which("ping"):
        return None
    rc, out = run(["ping", "-c", "1", "-W", str(timeout), host], timeout=timeout + 2)
    m = re.search(r"time[=<]([\d.]+)\s*ms", out)
    return float(m.group(1)) if m else None


def age_since(ts, now):
    """Seconds from a timestamp in a status file to now; None if missing or unreadable."""
    try:
        return max(0.0, now - float(ts)) if ts else None
    except (TypeError, ValueError):
        return None


def link_view(lk, now):
    """The bridge's server-link health (the bridge status file's "link" section) for the SERVER LINK panel.
    None for an older bridge, so the panel just shows its usual rows."""
    if not isinstance(lk, dict) or "state" not in lk:
        return None
    def ago(ts):
        return age_since(ts, now)
    return {"state": lk.get("state"), "reason": lk.get("reason") or "",
            "answers_pings": bool(lk.get("answers_pings")), "rtt_ms": lk.get("rtt_ms"),
            "last_pong_age": ago(lk.get("last_pong_ts")), "ping_interval": lk.get("ping_interval") or 20,
            "timeout": lk.get("timeout") or 60, "since_age": ago(lk.get("since")),
            "starts": int(lk.get("starts") or 1), "prev_reason": lk.get("prev_reason") or "",
            "prev_down_age": ago(lk.get("prev_down_ts"))}


def dm_route_view(d, now):
    """The bridge's "dm_route": the DM self-test (a message to a made-up callsign must come back via the server)."""
    if not isinstance(d, dict) or d.get("state") in (None, "off"):
        return None
    def ago(ts):
        return age_since(ts, now)
    return {"state": d.get("state"), "rtt_ms": d.get("rtt_ms"), "since_age": ago(d.get("since")),
            "last_ok_age": ago(d.get("last_ok_ts")), "callsign": d.get("callsign") or "", "sent": d.get("sent", 0)}


# errors the bridge survived and counted (its Faults), as Ops shows them: area -> (level, what it means)
FAULT_TEXT = {
    "uplink": ("bad", "Mesh -> server failing"),
    "return": ("bad", "Server -> mesh failing"),
    "outbox_replay": ("bad", "Saved chats not resent after a restart"),
    "dm_check": ("warn", "DM route check failing"),
    "identity": ("warn", "Deck's own report to the server failing"),
    "itak_udp": ("warn", "Unreadable iTAK direct messages dropped"),
    "files": ("warn", "File not passed to the server"),
    "files_down": ("warn", "File not passed to the mesh"),
}


def fault_alerts(faults):
    """The bridge's active faults as alert-strip entries (raising the BRIDGE tile)."""
    out = []
    for area, f in sorted((faults or {}).items()):
        if not isinstance(f, dict) or not f.get("active"):
            continue
        level, label = FAULT_TEXT.get(area, ("warn", "Bridge: %s failing" % area))
        n = int(f.get("count") or 0)
        out.append({"id": "fault:" + area, "level": level, "tile": "bridge",
                    "text": "%s: %s" % (label, str(f.get("last") or "")[:120]),
                    "value": "%d %s" % (n, "dropped" if area == "itak_udp" else "error" if n == 1 else "errors")})
    return out


def outbox_view(d, now):
    """The bridge's "outbox": chats kept on disk until written to the server."""
    if not isinstance(d, dict) or not d.get("enabled"):
        return None
    def ago(ts):
        return age_since(ts, now)
    waiting, unconf = int(d.get("waiting") or 0), int(d.get("unconfirmed") or 0)
    oldest = ago(d.get("oldest_ts"))
    # a chat is "waiting" when not yet written, or written but not confirmed by a ping answer for over 90 s
    held = waiting + (unconf if (oldest or 0) > 90 and not waiting else 0)
    return {"pending": held, "oldest_age": oldest, "replayed": int(d.get("replayed") or 0),
            "expired": int(d.get("expired") or 0),
            "confirmed": int(d.get("confirmed") or 0), "sent_unconfirmed": int(d.get("sent_unconfirmed") or 0),
            "can_confirm": d.get("can_confirm", True) is not False,
            "write_errors": int(d.get("write_errors") or 0), "last_write_error": str(d.get("last_write_error") or "")[:200]}


def parse_ss_link(out, want):
    """(established, rtt_ms) for the connection to `want` ("host:port") in `ss -tinH state established` output."""
    lines = out.splitlines()
    for i, line in enumerate(lines):
        if line[:1].isspace():
            continue                       # an info line, not a socket line
        if want in line.split():
            info = lines[i + 1] if i + 1 < len(lines) and lines[i + 1][:1].isspace() else ""
            m = re.search(r"\brtt:([\d.]+)/", info)
            return True, (round(float(m.group(1)), 1) if m else None)
    return False, None


def ping(host, timeout=1):
    """Return round-trip ms from ping, or None. Falls back to a TCP connect if ping is unavailable."""
    if shutil.which("ping"):
        rc, out = run(["ping", "-c", "1", "-W", str(timeout), host], timeout=timeout + 2)
        m = re.search(r"time[=<]([\d.]+)\s*ms", out)
        if m:
            return float(m.group(1))
        if rc != 127:
            return None
    for port in (443, 80, 22):
        r = tcp_check(host, port, timeout=1.0)
        if r is not None:
            return r
    return None


def ntp_synced():
    """True/False from timedatectl (or chrony), None if neither is available."""
    if shutil.which("timedatectl"):
        rc, out = run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"])
        if rc == 0 and out.strip() in ("yes", "no"):
            return out.strip() == "yes"
    if shutil.which("chronyc"):
        rc, out = run(["chronyc", "tracking"])
        if rc == 0:
            return "Not synchronised" not in out and "Leap status     : Normal" in out
    return None


def iface_ip(iface):
    rc, out = run(["ip", "-4", "-o", "addr", "show", "dev", iface])
    m = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+)", out)
    return m.group(1) if m else None


def read_int(path):
    try:
        return int(open(path).read().strip())
    except Exception:
        return None


def read_text(path):
    try:
        return open(path).read().strip()
    except Exception:
        return None


def pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except PermissionError:
        return True
    except Exception:
        return False


def fmt_age(sec):
    if sec is None:
        return "never"
    sec = int(sec)
    if sec < 90:
        return "%ds ago" % sec
    if sec < 5400:
        return "%dm ago" % (sec // 60)
    return "%.1fh ago" % (sec / 3600.0)


def parse_cot_url(cfg):
    host, port = cfg.get("SERVER_HOST"), cfg.get("SERVER_PORT")
    if host and port:
        return host, int(port)
    cot = ""
    try:
        cp = configparser.ConfigParser(interpolation=None, strict=False)
        cp.read(cfg["BRIDGE_INI"], encoding="utf-8-sig")
        cot = cp["bridge"].get("COT_URL", "")
    except Exception:
        pass
    m = re.match(r"\w+://([^:/]+):(\d+)", cot or "")
    if m:
        return host or m.group(1), int(port or m.group(2))
    return host or "", int(port or 8089)          # no server configured yet: the panel says so


# ----------------------------------------------------------------- events
_warned = set()


class Disk:
    """Ops' own writes to the deck's disk (event log, trend history, mesh status). A failing write is shown in Ops
    as an alert, not only in journalctl: a disk gone read-only (a loose USB SSD) is otherwise invisible."""
    lock = threading.Lock()
    fails = {}               # what -> (since, message)

    @classmethod
    def fail(cls, key, msg):
        with cls.lock:
            if key not in cls.fails:
                cls.fails[key] = (time.time(), msg)

    @classmethod
    def ok(cls, key):
        with cls.lock:
            cls.fails.pop(key, None)

    @classmethod
    def failing(cls):
        with cls.lock:
            return sorted(cls.fails.values())[0] if cls.fails else None


STALE_AFTER = 10.0           # seconds without a fresh snapshot before the page says its data is old


def warn_once(key, msg):
    """Report a non-fatal problem once in the service log (journalctl -u manet-dashboard), not every time it repeats."""
    if key not in _warned:
        _warned.add(key)
        sys.stderr.write("dashboard: %s\n" % msg)


EVENT_FILE_KEEP = 5000                     # lines of ~/dashboard_events.jsonl kept (trimmed at each start)


class Events:
    def __init__(self, path):
        self.path = path
        self.items = deque(maxlen=400)
        self.lock = threading.Lock()
        try:
            with open(path) as f:
                lines = f.readlines()
            for line in lines[-200:]:
                try:
                    self.items.append(json.loads(line))
                except ValueError:
                    pass                           # a half-written last line after a power cut
            if len(lines) > EVENT_FILE_KEEP:       # keep the newest lines; the file never grows without limit
                tmp = path + ".tmp"
                with open(tmp, "w") as f:
                    f.writelines(lines[-EVENT_FILE_KEEP:])
                os.replace(tmp, path)
        except FileNotFoundError:
            pass                                   # first start
        except Exception as exc:
            warn_once("events-load", "cannot read the event log %s (%s); starting with an empty one" % (path, exc))

    def add(self, level, text, cat="system"):
        ev = {"t": time.time(), "level": level, "text": text, "cat": cat}
        with self.lock:
            self.items.append(ev)
        try:
            with open(self.path, "a") as f:
                f.write(json.dumps(ev) + "\n")
            Disk.ok("events-write")
        except Exception as exc:                   # the page still shows it; only the copy on disk is missing
            warn_once("events-write", "cannot write the event log %s (%s)" % (self.path, exc))
            Disk.fail("events-write", "cannot write the event log (%s)" % exc)

    def recent(self, n=100):
        with self.lock:
            return list(self.items)[-n:][::-1]


# ----------------------------------------------------------------- history (sparklines + 24 h strip)
LEVEL_NUM = {"ok": 0, "info": 0, "warn": 1, "bad": 2}
STRIP_MINUTES = 1440


class History:
    """Short-term samples for sparklines (kept in memory) and one line per minute for the
    24-hour strip (kept in a small file so it survives restarts)."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.spark = deque(maxlen=360)       # one sample per ~10 s = last hour
        self.ping = deque(maxlen=400)        # every node ping: (t, rtt|None)
        self.links = deque(maxlen=130)       # radio link speeds, one sample per ~30 s = last hour: (t, {"A|B": mbps})
        self.minutes = {}                    # minute index -> {"b","s","v","x"} worst level that minute
        self.cur_idx = None
        self.cur = None
        self.last_spark = 0.0
        self.writes = 0
        self._load()

    def _load(self):
        try:
            with open(self.path) as f:
                lines = f.readlines()
        except FileNotFoundError:
            return                                 # first start
        except Exception as exc:
            warn_once("history-load", "cannot read the trend history %s (%s); starting empty" % (self.path, exc))
            return
        keep = lines[-(STRIP_MINUTES + 60):]
        for line in keep:
            try:
                r = json.loads(line)
                self.minutes[int(r["m"])] = {k: int(r[k]) for k in ("b", "s", "v", "x")}
            except (ValueError, KeyError, TypeError):
                pass                               # a half-written or foreign line
        if len(lines) > STRIP_MINUTES * 2:
            try:
                with open(self.path, "w") as f:
                    f.writelines(keep)
            except Exception as exc:
                warn_once("history-trim", "cannot trim the trend history %s (%s)" % (self.path, exc))

    def add_ping(self, t, rtt):
        with self.lock:
            self.ping.append((round(t, 1), rtt))

    def add_links(self, t, speeds):
        with self.lock:
            if self.links and t - self.links[-1][0] < 29:
                return
            self.links.append((round(t, 1), dict(speeds)))

    def tick(self, now, levels):
        """levels: {"b":0..2, "s":..., "v":..., "x":...} for this moment."""
        idx = int(now // 60)
        with self.lock:
            if self.cur_idx is not None and idx != self.cur_idx and self.cur:
                self.minutes[self.cur_idx] = self.cur
                try:
                    with open(self.path, "a") as f:
                        f.write(json.dumps(dict(self.cur, m=self.cur_idx)) + "\n")
                    Disk.ok("history-write")
                except Exception as exc:           # the strip still works from memory until the next restart
                    warn_once("history-write", "cannot write the trend history %s (%s)" % (self.path, exc))
                    Disk.fail("history-write", "cannot write the trend history (%s)" % exc)
                for k in [k for k in self.minutes if k < idx - STRIP_MINUTES - 5]:
                    del self.minutes[k]
                self.cur = None
            self.cur_idx = idx
            if self.cur is None:
                self.cur = dict(levels)
            else:
                for k, v in levels.items():
                    self.cur[k] = max(self.cur.get(k, 0), v)

    def sample(self, now, **vals):
        if now - self.last_spark < 9.5:
            return
        self.last_spark = now
        with self.lock:
            self.spark.append(dict(vals, t=round(now, 1)))

    def export(self, now):
        with self.lock:
            sp = list(self.spark)
            ping = list(self.ping)[-300:]
            links = list(self.links)
            minutes = dict(self.minutes)
            if self.cur_idx is not None and self.cur:
                minutes[self.cur_idx] = self.cur
        out = {"now": now, "spark": {"step": 10, "t0": sp[0]["t"] if sp else now}, "ping": ping,
               "links": [[t, v] for t, v in links]}
        for k in ("msgs", "rtt", "temp", "kbps", "load", "srv"):
            out["spark"][k] = [x.get(k) for x in sp]
        end = int(now // 60)
        strip = {"step": 60, "t0": (end - STRIP_MINUTES + 1) * 60}
        for name, key in (("bridge", "b"), ("server", "s"), ("video", "v"), ("mesh", "x")):
            chars = []
            for m in range(end - STRIP_MINUTES + 1, end + 1):
                r = minutes.get(m)
                chars.append("-" if r is None else str(min(2, max(0, r.get(key, 0)))))
            strip[name] = "".join(chars)
        out["strip"] = strip
        return out


# ----------------------------------------------------------------- alert rules (set in [dashboard] of deck.ini)
METRICS = {
    "bridge_idle_min": "Bridge idle (minutes with no traffic)",
    "cpu_temp": "CPU temperature (C)",
    "mem_pct": "Memory used (%)",
    "mesh_rtt": "Node ping (ms)",
    "mesh_loss": "Node ping loss (%)",
    "server_rtt": "Server connect time (ms)",
    "link_mbps": "Slowest radio link (Mbit/s)",
}
TILES = ("none", "bridge", "server", "video", "mesh")
# Same format as in deck.ini:  metric  >|<  limit  [for SECONDS]  warn|bad  [tile]
DEFAULT_RULES = [
    "mesh_loss > 25 for 60 warn mesh",
    "cpu_temp > 80 for 60 warn",
    "mem_pct > 90 for 120 warn",
]


def parse_rule(text, rid):
    """'mesh_loss > 25 for 60 warn mesh' -> rule dict. Raises ValueError with a readable reason."""
    t = text.split()
    if len(t) < 3:
        raise ValueError("needs at least: metric > limit")
    metric, op = t[0], t[1]
    if metric not in METRICS:
        raise ValueError("unknown metric '%s' (use one of: %s)" % (metric, ", ".join(METRICS)))
    if op not in (">", "<"):
        raise ValueError("operator must be > or <")
    value = float(t[2])
    rest = t[3:]
    for_s = 0
    if len(rest) >= 2 and rest[0] == "for":
        for_s = max(0, min(3600, int(float(rest[1]))))
        rest = rest[2:]
    level = rest[0] if rest and rest[0] in ("warn", "bad") else "warn"
    tile = rest[1] if len(rest) > 1 and rest[1] in TILES else "none"
    return {"id": rid, "metric": metric, "op": op, "value": value, "for_s": for_s, "level": level, "tile": tile}


def rules_from_cfg(cfg):
    """ALERT_1 = ..., ALERT_2 = ... in [dashboard] replace the defaults. ALERTS = off turns them all off."""
    if cfg.get("ALERTS", "on").strip().lower() in ("off", "no", "0", "false"):
        return [], []
    keys = sorted([k for k in cfg if re.match(r"^ALERT_\d+$", k)], key=lambda k: int(k[6:]))
    src = [(k.lower(), cfg[k]) for k in keys if cfg[k].strip()] if keys else [("default_%d" % (i + 1), r) for i, r in enumerate(DEFAULT_RULES)]
    rules, errors = [], []
    for rid, text in src:
        try:
            rules.append(parse_rule(text, rid))
        except (ValueError, IndexError) as exc:
            errors.append("%s = %s: %s" % (rid.upper(), text, exc))
    return rules, errors


def rule_text(r):
    v = r["value"]
    v = int(v) if float(v).is_integer() else v
    return "%s %s %s" % (METRICS[r["metric"]].split(" (")[0], r["op"], v)


class Alerts:
    def __init__(self, cfg, events):
        self.events = events
        self.rules, self.errors = rules_from_cfg(cfg)
        self.since = {}        # rule id -> time the condition first became true
        self.active = {}       # rule id -> info
        for e in self.errors:
            sys.stderr.write("alert rule ignored: %s\n" % e)

    def describe(self):
        out = []
        for r in self.rules:
            d = dict(r, text=rule_text(r))
            out.append(d)
        return out

    def evaluate(self, metrics, now):
        active = {}
        for r in self.rules:
            val = metrics.get(r["metric"])
            hit = val is not None and ((val > r["value"]) if r["op"] == ">" else (val < r["value"]))
            if not hit:
                self.since.pop(r["id"], None)
                continue
            t0 = self.since.setdefault(r["id"], now)
            if now - t0 >= r["for_s"]:
                active[r["id"]] = {"id": r["id"], "level": r["level"], "tile": r["tile"], "text": rule_text(r),
                                   "value": round(val, 1)}
        for rid, a in active.items():
            if rid not in self.active:
                self.events.add(a["level"], "Alert: %s (now %s)" % (a["text"], a["value"]), "alert")
        for rid, a in self.active.items():
            if rid not in active:
                self.events.add("ok", "Alert cleared: %s" % a["text"], "alert")
        self.active = active
        return list(active.values())


# ----------------------------------------------------------------- controls (restart the bridge, snapshot)
SAFE_PATH = re.compile(r"^[A-Za-z0-9_\-./]{1,64}$")


def free_mb(path):
    try:
        os.makedirs(path, exist_ok=True)
        st = os.statvfs(path)
        return st.f_bavail * st.f_frsize / 1048576.0
    except Exception:
        return None


class Control:
    def __init__(self, cfg, events):
        self.cfg = cfg
        self.events = events
        self.enabled = cfg["CONTROL"].strip().lower() not in ("off", "no", "0", "false")
        self.cache = (0, None)
        self.last_restart = 0.0

    def can_restart(self):
        now = time.time()
        if now - self.cache[0] < 60:
            return self.cache[1]
        unit = self.cfg["BRIDGE_UNIT"]
        ok = False
        if self.enabled and shutil.which("systemctl") and shutil.which("sudo"):
            ok = run(["systemctl", "cat", unit])[0] == 0 and run(["sudo", "-n", "-l", "systemctl", "restart", unit])[0] == 0
        self.cache = (now, ok)
        return ok

    def caps(self):
        return {"control": self.enabled, "restart_bridge": self.can_restart(),
                "ffmpeg": bool(shutil.which("ffmpeg"))}

    def restart_bridge(self):
        if not self.enabled:
            return False, "controls are turned off (CONTROL = off)"
        if time.time() - self.last_restart < 10:
            return False, "just restarted; wait a few seconds"
        if not self.can_restart():
            return False, "not set up: run install.sh again and allow the restart permission"
        unit = self.cfg["BRIDGE_UNIT"]
        self.last_restart = time.time()
        rc, _ = run(["sudo", "-n", "systemctl", "restart", unit], timeout=25)
        if rc != 0:
            self.events.add("warn", "Restart of %s failed" % unit, "action")
            return False, "restart failed (see: journalctl -u %s)" % unit
        self.events.add("info", "Restarted %s from the dashboard" % unit, "action")
        return True, "restarted %s" % unit

    def snapshot(self, path):
        if not self.enabled:
            return False, "controls are turned off (CONTROL = off)"
        if not shutil.which("ffmpeg"):
            return False, "ffmpeg is not installed (sudo apt install ffmpeg)"
        if not path or not SAFE_PATH.match(path) or ".." in path:
            return False, "bad stream name"
        fm = free_mb(self.cfg["CAPTURE_DIR"])
        if fm is None or fm < float(self.cfg["MIN_FREE_MB"]):
            return False, "not enough free disk space"
        out = os.path.join(self.cfg["CAPTURE_DIR"], "snap_%s_%s.jpg" % (path.replace("/", "-"), time.strftime("%Y%m%d-%H%M%S")))
        rc, _ = run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-rtsp_transport", "tcp",
                     "-i", "rtsp://127.0.0.1:%s/%s" % (self.cfg["MEDIAMTX_RTSP_PORT"], path),
                     "-frames:v", "1", "-q:v", "2", out], timeout=15)
        if rc != 0 or not os.path.exists(out) or os.path.getsize(out) == 0:
            try:
                os.remove(out)
            except OSError:
                pass
            return False, "could not grab a frame (is '%s' live?)" % path
        self.events.add("info", "Snapshot saved: %s" % os.path.basename(out), "video")
        return True, os.path.basename(out)

    def captures(self):
        d = self.cfg["CAPTURE_DIR"]
        out = []
        try:
            names = [n for n in os.listdir(d) if n.endswith(".jpg")]
            names.sort(key=lambda n: os.path.getmtime(os.path.join(d, n)), reverse=True)
            for n in names[:30]:
                st = os.stat(os.path.join(d, n))
                out.append({"name": n, "size": st.st_size, "t": st.st_mtime})
        except OSError:
            pass
        return out


# ----------------------------------------------------------------- setup check
def server_unset(cfg):
    """Why the TAK server isn't set up yet (a fresh install), or None. Same rule as `meshtoc check`."""
    try:
        cp = configparser.ConfigParser(interpolation=None, strict=False)
        cp.read(cfg["BRIDGE_INI"], encoding="utf-8-sig")
        br = cp["bridge"]
    except (KeyError, OSError, configparser.Error, ValueError):
        return None                                # can't tell: show the normal checks
    cot = br.get("COT_URL", "").strip()
    if not cot or "tak.example.net" in cot:
        return "COT_URL is still the example"
    if cot.lower().startswith("tls://") and not br.get("PYTAK_TLS_CLIENT_CERT", "").strip():
        return "no certificate yet"
    return None


def setup_checks(cfg, snap, control, alerts):
    """Plain-language list of what is set up and what is not, each with the command that fixes it."""
    b, v, k, m, s = snap["bridge"], snap["video"], snap["deck"], snap["mesh"], snap["server"]
    unit = cfg["BRIDGE_UNIT"]
    c = []

    def add(ok, title, detail="", fix="", level=None):
        c.append({"ok": ok, "level": level or ("ok" if ok else "bad"), "title": title, "detail": detail, "fix": fix})

    unset = server_unset(cfg)
    if unset:                                      # a fresh install: one thing to do, not three red lines
        add(False, "TAK server not set up yet", unset,
            "in ~/manet_mesh_bridge/deck.ini [bridge] set COT_URL and the three PYTAK_TLS_ certificate lines, "
            "then: meshtoc restart   (README, 'Certificates')", "warn")
        add(False, "Bridge", "starts once the TAK server is set up", "", "info")
    else:
        add(b["service"] == "active", "Bridge service running", "systemd says: %s" % b["service"],
            "sudo systemctl restart %s   (log: journalctl -u %s -n 50)" % (unit, unit))
        if not b["has_status"]:
            add(False, "Bridge status file", "%s does not exist: the bridge is the old version" % cfg["BRIDGE_STATUS_FILE"],
                "bash install.sh   (say yes to the bridge step)", "warn")
        else:
            add(b["running"], "Bridge status file", "fresh" if b["running"] else "stale: the bridge stopped writing it",
                "sudo systemctl restart %s" % unit, None if b["running"] else "warn")
        add(s["established"], "Server TLS link", "%s:%s %s" % (s["host"], s["port"], "established" if s["established"] else "not connected"),
            "check Tailscale (tailscale status) and the bridge log: journalctl -u %s -n 50" % unit,
            None if s["established"] or not s["reachable"] else "warn")
    nodes = m.get("nodes") or []
    if not nodes:
        add(False, "Mesh nodes", "none set and none found on %s" % k["iface"],
            "set NODE_IP = <node IP>, <node IP> ... in [dashboard] of deck.ini", "warn")
    else:
        add(m["node_ok"], "Mesh node answers", "; ".join("%s at %s %s" % (n["name"], n["ip"], "answers" if n["ok"] else "not answering")
                                                          for n in nodes),
            "check the cable to the node and: ip addr show %s" % k["iface"], None if m["node_ok"] or not m.get("traffic") else "warn")
    add(k["mtu_ok"], "eth0 MTU", "%s (expected %s)" % (k["mtu"], k["mtu_expected"]),
        "sudo ip link set %s mtu %s   (until reboot; persist it in NetworkManager)" % (k["iface"], k["mtu_expected"]), "warn")
    if k["ntp_synced"] is None:
        add(True, "Clock", "sync state unknown (timedatectl not available)", "", "info")
    else:
        add(k["ntp_synced"], "Clock synced", "TAK marks positions stale when the clock is off",
            "needs internet once: sudo timedatectl set-ntp true", "warn")
    if v["api_ok"]:
        add(True, "MediaMTX status API", "on")
    elif v["running"]:
        add(False, "MediaMTX status API", "MediaMTX runs but the API is off, so streams are not listed",
            "bash install.sh   (MediaMTX step), or set 'api: yes' in ~/mediamtx/mediamtx.yml and restart MediaMTX", "warn")
    else:
        add(False, "MediaMTX running", "no answer on the RTSP port", "sudo systemctl restart mediamtx   (or: cd ~/mediamtx && ./mediamtx)", "warn")
    vm = v.get("markers") or {}
    if vm.get("state") == "on":
        add(True, "Video markers on the mesh", "%d stream(s) as tap-to-play markers, %d at the person streaming"
            % (vm.get("count", 0), vm.get("located", 0)))
    elif vm.get("state") not in (None, "off"):
        add(False, "Video markers on the mesh", vm["state"],
            "a GPS fix, or set VIDEO_MARKER_LAT / VIDEO_MARKER_LON in [dashboard]" if "GPS" in vm["state"] else "", "info")
    rl = v.get("relay") or {}
    if rl.get("state") == "on":
        lag = ", ".join("%s %.1f s" % (n, v) for n, v in sorted((rl.get("lag") or {}).items()))
        add(True, "Video to the server", "copying %d mesh stream(s) to %s%s%s" % (len(rl.get("relaying", [])), rl.get("server", ""),
            "; behind live: " + lag if lag else "",
            "; already live there, not copied: " + ", ".join(rl["conflicts"]) if rl.get("conflicts") else ""))
    elif rl.get("state") not in (None, "off"):
        add(False, "Video to the server", rl["state"], "sudo apt install ffmpeg" if "ffmpeg" in rl["state"] else "", "warn")
    add(bool(shutil.which("ffmpeg")), "ffmpeg (for Snap)", "", "sudo apt install ffmpeg", "info" if not shutil.which("ffmpeg") else None)
    can = control.can_restart()
    add(can, "Restart button permission", "" if can else "the Restart bridge button stays greyed out",
        "bash install.sh   (say yes to the restart permission)", "info" if not can else None)
    missing = [t for t in ("ip", "ss", "ping", "timedatectl") if not shutil.which(t)]
    add(not missing, "System tools", "missing: " + ", ".join(missing) if missing else "ip, ss, ping, timedatectl found",
        "sudo apt install iproute2 iputils-ping systemd" if missing else "", "warn" if missing else None)
    for e in alerts.errors:
        add(False, "Alert rule in deck.ini", e, "fix the ALERT_ line in [dashboard] of deck.ini, then: meshtoc restart", "warn")
    return c


# ----------------------------------------------------------------- collector
# ---- shared code: GPS/MGRS, team icon, the [deck] config section, suite version -> ../common/deckcommon.py
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "common"))
from meshwatch import MeshWatch, grade as link_grade  # noqa: E402
import videomarkers  # noqa: E402
import videorelay  # noqa: E402
from deckcommon import (GpsWatch, to_mgrs, latlon_to_utm, TEAMS, ROLES, team_xml,  # noqa: E402,F401
                        apply_deck, suite_version, font_file)


def _ago(sec):
    sec = max(0, int(sec))
    return "%d s ago" % sec if sec < 60 else "%d min ago" % (sec // 60) if sec < 3600 else "%d h ago" % (sec // 3600)


def gps_tile(g, digits=5):
    """GPS tile from a GpsWatch snapshot (approved design: fix state, MGRS grid, detail line)."""
    if g is None:
        return None
    dash = "-- -- ----- -----"
    if not g["gpsd"]:
        return {"level": "bad", "fix": "NO GPS", "grid": dash, "detail": "gpsd not running \u00b7 check the service", "mode": 0}
    if g["devices"] == 0:
        return {"level": "bad", "fix": "NO GPS", "grid": dash, "detail": "puck not detected \u00b7 check USB", "mode": 0}
    fresh = g["mode"] >= 2 and g["fix_age"] is not None and g["fix_age"] <= 10
    last = g.get("last")
    if fresh:
        grid = to_mgrs(g["lat"], g["lon"], digits) or "polar (no MGRS)"
        parts = []
        if g.get("used") is not None:
            parts.append("%d SAT" % g["used"])
        if g.get("eph") is not None:
            parts.append("\u00b1%d m" % round(g["eph"]))
        parts.append(_ago(g["fix_age"]))
        if g["mode"] == 3:
            parts.append("%d m" % round(g["alt"]) if g.get("alt") is not None else "alt n/a")
            return {"level": "ok", "fix": "3D FIX", "grid": grid, "detail": " \u00b7 ".join(parts), "mode": 3}
        parts.append("no alt")
        return {"level": "warn", "fix": "2D FIX", "grid": grid, "detail": " \u00b7 ".join(parts), "mode": 2}
    if last:
        return {"level": "warn", "fix": "NO FIX", "grid": to_mgrs(last["lat"], last["lon"], digits) or dash,
                "detail": "last fix %s \u00b7 STALE" % _ago(time.time() - last["ts"]), "mode": 0}
    seen = g.get("seen")
    return {"level": "warn", "fix": "NO FIX", "grid": dash,
            "detail": "searching \u00b7 %d sats seen" % seen if seen is not None else "searching for satellites", "mode": 0}


class Collector(threading.Thread):
    def __init__(self, cfg, events, history=None, alerts=None, control=None):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.events = events
        self.history = history or History(os.devnull)
        self.alerts = alerts or Alerts(cfg, events)
        self.control = control or Control(cfg, events)
        self.last_traffic = time.time()
        self.lock = threading.Lock()
        self.snapshot = {"booting": True, "now": time.time()}
        self.collect_error = None
        self.srv_host, self.srv_port = parse_cot_url(cfg)
        self.slow = {}              # cached slow checks: name -> (time, value)
        self.prev = {}              # previous values for change detection
        self.bytes_prev = {}        # stream path -> (time, bytes)
        self.pings = deque(maxlen=60)
        self.data_ts = {}           # stream path -> last time its byte counter moved
        self.gps = None
        if cfg.get("GPS", "gpsd").strip().lower() == "gpsd":
            h, _, p = cfg.get("GPS_HOST", "127.0.0.1:2947").partition(":")
            self.gps = GpsWatch(h or "127.0.0.1", p or 2947)
            self.gps.start()
        self.vmark = None
        if cfg.get("VIDEO_MARKERS", "mesh").strip().lower() == "mesh":
            self.vmark = videomarkers.VideoMarkers(refresh=float(cfg.get("VIDEO_MARKER_REFRESH") or 180),
                                                   stale=float(cfg.get("VIDEO_MARKER_STALE") or 600))
            self.upstream = videomarkers.UpstreamWatch(every=float(cfg.get("VIDEO_SOURCE_CHECK") or 20),
                                                       log=lambda level, text: self.events.add(level, text, "video"))
        self.relay = self.smark = None
        rel = cfg.get("VIDEO_RELAY", "off").strip()
        if rel.lower() not in ("", "off", "0", "no", "false"):
            if rel.lower() == "auto":
                rel = "rtsp://%s:%s" % (self.srv_host, cfg.get("MEDIAMTX_RTSP_PORT", "8554")) if self.srv_host else ""
            if rel.lower().startswith("rtsp://"):
                self.relay = videorelay.StreamRelay(rel, local="rtsp://127.0.0.1:%s" % cfg.get("MEDIAMTX_RTSP_PORT", "8554"),
                                                    log=lambda level, text: self.events.add(level, text, "video"))
                hp = cfg.get("VIDEO_RELAY_SEND", "127.0.0.1:17014").rpartition(":")
                dest = (hp[0] or "127.0.0.1", int(hp[2] or 17014))
                self.smark = videomarkers.VideoMarkers(refresh=float(cfg.get("VIDEO_MARKER_REFRESH") or 180),
                                                       stale=float(cfg.get("VIDEO_MARKER_STALE") or 600), tag="SVIDEO",
                                                       send=lambda host, data, dest=dest: self._to_server(dest, data))
        self.meshwatch = None
        if cfg.get("MESH_DISCOVERY", "alfred").strip().lower() == "alfred":
            self.meshwatch = MeshWatch(cfg.get("MESH_IFACE", "eth0")).start()

    # -- slow-check cache
    def cached(self, name, ttl, fn):
        now = time.time()
        t, v = self.slow.get(name, (0, None))
        if now - t >= ttl:
            v = fn()
            self.slow[name] = (now, v)
        return v

    def changed(self, key, value, up_text=None, down_text=None, level_up="ok", level_down="bad", cat="system"):
        """Log an event when a boolean flips (not on the first sample)."""
        old = self.prev.get(key)
        self.prev[key] = value
        if old is None or old == value:
            return
        if value and up_text:
            self.events.add(level_up, up_text, cat)
        elif not value and down_text:
            self.events.add(level_down, down_text, cat)

    # -- pieces
    def bridge(self):
        cfg = self.cfg
        st, age = None, None
        try:
            with open(cfg["BRIDGE_STATUS_FILE"]) as f:
                st = json.load(f)
            age = time.time() - float(st.get("now", 0))
        except Exception:
            st = None
        rc, out = run(["systemctl", "is-active", cfg["BRIDGE_SERVICE"]])
        service = out.strip() or ("unknown" if rc == 127 else "inactive")
        fresh = st is not None and age is not None and age < 8 and pid_alive(st.get("pid", 0))
        running = fresh
        pm = (st or {}).get("per_minute", {"up": {"total": 0}, "down": {"total": 0}, "total": 0})
        mesh = (st or {}).get("mesh", {})
        ret = (st or {}).get("return", {})
        srv = (st or {}).get("server", {})
        now = time.time()
        return {
            "service": service,
            "running": bool(running),
            "stale": bool(st is not None and not fresh and service == "active"),
            "has_status": st is not None,
            "pid": (st or {}).get("pid"),
            "uptime": (st or {}).get("uptime") if fresh else None,
            "started": (st or {}).get("started"),
            "msgs_per_min": pm.get("total", 0),
            "per_minute": pm,
            "mesh_events": mesh.get("events", 0),
            "mesh_last_age": (now - mesh["last_ts"]) if mesh.get("last_ts") else None,
            "mesh_last_from": mesh.get("last_from", ""),
            "mesh_skipped": mesh.get("skipped", 0),
            "mesh_failed": mesh.get("failed", 0),
            "faults": (st or {}).get("faults") or {} if fresh else {},
            "mesh_malformed": mesh.get("malformed", 0),
            "return_enabled": bool(ret.get("enabled")),
            "return_sent": ret.get("sent", 0),
            "return_last_age": (now - ret["last_ts"]) if ret.get("last_ts") else None,
            "server_rx": srv.get("rx_events", 0),
            "server_last_age": (now - srv["last_rx_ts"]) if srv.get("last_rx_ts") else None,
            "mesh_url": (st or {}).get("mesh_url", ""),
            "local_addr": (st or {}).get("local_addr", ""),
            "link": link_view((st or {}).get("link"), now) if st is not None else None,
            "dm_route": dm_route_view((st or {}).get("dm_route"), now),
            "outbox": outbox_view((st or {}).get("outbox"), now),
        }

    def link(self):
        """The bridge's own TLS connection to the server, read from the kernel (ss -tin):
        (established, rtt_ms). v4.2: no more test connections to the server port, which
        OpenTAKServer logged as an SSL error every 4 s."""
        rc, out = run(["ss", "-tinH", "state", "established"])
        return parse_ss_link(out, "%s:%d" % (self.srv_host, self.srv_port))

    def server(self, bridge):
        est, est_rtt = self.cached("srv_link", 2, self.link)
        # Only when the bridge link is down: an ICMP ping says whether the server box answers at all.
        ping_rtt = None if est else self.cached("srv_ping", 10, lambda: icmp_ping(self.srv_host) if self.srv_host else None)
        rtt = est_rtt if est else ping_rtt
        reachable = bool(est) or ping_rtt is not None
        ts_ip = iface_ip("tailscale0")
        if est:
            tile = "ok"
        elif reachable:
            tile = "warn"
        else:
            tile = "bad"
        return {
            "host": self.srv_host, "port": self.srv_port,
            "reachable": reachable, "rtt_ms": rtt,
            "established": bool(est),
            "last_rx_age": bridge["server_last_age"], "rx_events": bridge["server_rx"],
            "tailscale_ip": ts_ip, "tailscale_up": ts_ip is not None,
            "tile": tile,
            "link": bridge.get("link") if bridge.get("running") or (bridge.get("link") or {}).get("state") == "down" else None,
            "dm_route": bridge.get("dm_route") if bridge.get("running") else None,
            "outbox": bridge.get("outbox") if bridge.get("running") else None,
        }

    def mediamtx(self):
        cfg = self.cfg
        api = cfg["MEDIAMTX_API"].rstrip("/")
        paths, api_ok, err = [], False, None
        try:
            with urlopen(api + "/v3/paths/list", timeout=1.5) as r:
                data = json.loads(r.read().decode())
            api_ok = True
            now = time.time()
            for it in data.get("items", []):
                name = it.get("name", "")
                rx = it.get("inboundBytes", it.get("bytesReceived", 0)) or 0
                tx = it.get("outboundBytes", it.get("bytesSent", 0)) or 0
                prev = self.bytes_prev.get(name)
                kbps = None
                if prev and now > prev[0]:
                    kbps = max(0.0, (rx - prev[1]) * 8 / 1000.0 / (now - prev[0]))
                self.bytes_prev[name] = (now, rx)
                if prev is None or rx != prev[1] or not it.get("ready"):
                    self.data_ts[name] = now
                stalled = bool(it.get("ready")) and now - self.data_ts.get(name, now) >= float(cfg["STALL_SECONDS"])
                src = it.get("source") or {}
                tracks = it.get("tracks2") or it.get("tracks") or []
                codecs = []
                for t in tracks:
                    codecs.append(t.get("codec", t) if isinstance(t, dict) else str(t))
                paths.append({
                    "name": name, "ready": bool(it.get("ready")),
                    "source": src.get("type", ""), "source_id": src.get("id", ""),
                    "readers": len(it.get("readers") or []),
                    "kbps": round(kbps) if kbps is not None else None,
                    "bytes_in": rx, "bytes_out": tx, "codecs": codecs,
                    "since": it.get("readyTime"), "stalled": stalled,
                })
        except Exception as exc:
            err = str(exc)[:80]
        # Only knock on the RTSP port when the API is unavailable: every knock shows up in the MediaMTX log
        # as a connection, which buried real streams in the log every 4 seconds.
        rtsp = None if api_ok else self.cached("mtx_rtsp", 15, lambda: tcp_check("127.0.0.1", cfg["MEDIAMTX_RTSP_PORT"], 1.0))
        return {"api_ok": api_ok, "running": api_ok or rtsp is not None, "paths": paths, "error": err,
                "rtsp_port": int(cfg["MEDIAMTX_RTSP_PORT"]), "webrtc_port": int(cfg["MEDIAMTX_WEBRTC_PORT"]),
                "hls_port": int(cfg["MEDIAMTX_HLS_PORT"]), "default_stream": cfg["DEFAULT_STREAM"]}

    def mtx_conf(self):
        """{path name: its MediaMTX config} (to find on-demand sources), or {} when the config API is not readable."""
        try:
            with urlopen(self.cfg["MEDIAMTX_API"].rstrip("/") + "/v3/config/paths/list", timeout=1.5) as r:
                return {it.get("name", ""): it for it in json.loads(r.read().decode()).get("items", [])}
        except Exception:
            return {}

    SESSION_API = {"rtspSession": "rtspsessions", "rtspsSession": "rtspssessions", "rtmpConn": "rtmpconns",
                   "rtmpsConn": "rtmpsconns", "srtConn": "srtconns", "webRTCSession": "webrtcsessions"}

    def publisher_ip(self, kind, sid):
        """The IP a stream is being published from (a phone streaming straight to the deck), or None."""
        api = self.SESSION_API.get(kind)
        if not api or not sid:
            return None
        cache = self.__dict__.setdefault("_pub_ip", {})
        if sid not in cache:
            try:
                with urlopen("%s/v3/%s/get/%s" % (self.cfg["MEDIAMTX_API"].rstrip("/"), api, sid), timeout=1.5) as r:
                    addr = json.loads(r.read().decode()).get("remoteAddr", "")
                cache[sid] = addr.rsplit(":", 1)[0].strip("[]") or None
            except Exception:
                return None
        return cache[sid]

    @staticmethod
    def _to_server(dest, data):
        """Hand one CoT to the bridge's LOCAL_SEND port: it goes to the TAK server only, never onto the mesh."""
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.sendto(data, dest)
        finally:
            s.close()

    def server_videos(self, video, people, owners, pubs, pos, now):
        """Mesh phones' streams copied to the server, and their VIDEO markers for server users."""
        self.relay.set_wanted(videorelay.wanted(video["paths"]))
        u = urlsplit(self.relay.server)
        host, port = u.hostname or "", u.port or 8554
        markers = videomarkers.plan([(n, "live") for n in self.relay.ok(now)], people, owners, pubs, pos, now,
                                    int(self.cfg.get("MGRS_DIGITS", "5")))
        for name, what, _ in self.smark.tick(now, markers, host, pos, port):
            self.events.add("info", "Video marker %s for server users: VIDEO %s" % ("added" if what == "added" else "removed", name), "video")
        return dict(self.relay.status(), markers=dict(self.smark.status), server=videorelay.public_url(self.relay.server))

    def people(self):
        """Everyone's last position, from the bridge's status file (never put in the page's feed)."""
        try:
            with open(self.cfg["BRIDGE_STATUS_FILE"]) as f:
                return json.load(f).get("people") or []
        except (OSError, ValueError):
            return []

    def video_markers(self, video, deck, now):
        """Keep the mesh's VIDEO markers in step with the streams on this deck, each at the person streaming it
        (see common/videomarkers.py)."""
        off = {"state": "off", "count": 0, "last_sent": None, "located": 0}
        if not self.vmark and not self.relay:
            return off
        if not video.get("api_ok"):
            return dict(self.vmark.status if self.vmark else off, state="MediaMTX API not readable")
        pos = None
        if self.gps:
            _, last = self.gps.position()
            if last:
                pos = (last["lat"], last["lon"])
        if pos is None and self.cfg.get("VIDEO_MARKER_LAT") and self.cfg.get("VIDEO_MARKER_LON"):
            try:
                pos = (float(self.cfg["VIDEO_MARKER_LAT"]), float(self.cfg["VIDEO_MARKER_LON"]))
            except ValueError:
                pass
        pubs = {p["name"]: self.publisher_ip(p.get("source"), p.get("source_id")) for p in video["paths"] if p.get("ready")}
        people, owners = self.cached("people", 5, self.people), videomarkers.parse_owners(self.cfg.get("VIDEO_OWNERS"))
        if self.relay:
            self.relay_status = self.server_videos(video, people, owners, pubs, pos, now)
        if not self.vmark:
            return off
        conf = self.cached("mtx_conf", 60, self.mtx_conf)
        self.upstream.set_sources(videomarkers.on_demand_sources(conf))
        items = videomarkers.streams(video["paths"], conf, self.upstream.get())
        markers = videomarkers.plan(items, people, owners, pubs, pos, now, int(self.cfg.get("MGRS_DIGITS", "5")))
        for name, what, state in self.vmark.tick(now, markers, deck.get("iface_ip"), pos, int(self.cfg["MEDIAMTX_RTSP_PORT"])):
            m = next((m for m in markers if m["name"] == name), None)
            if what == "added":
                self.events.add("info", "Video marker on the mesh: VIDEO %s (%s, %s)" % (name, state, m["owner"] or "at the deck"), "video")
            else:
                self.events.add("info", "Video marker removed from the mesh: VIDEO %s" % name, "video")
        return dict(self.vmark.status)

    def deck(self):
        cfg = self.cfg
        iface = cfg["MESH_IFACE"]
        mtu = read_int("/sys/class/net/%s/mtu" % iface)
        expected = int(cfg["MTU_EXPECTED"])
        temp = read_int("/sys/class/thermal/thermal_zone0/temp")
        mem = {}
        try:
            for line in open("/proc/meminfo"):
                k, v = line.split(":")
                mem[k] = int(v.split()[0])
        except Exception:
            pass
        mem_pct = None
        if mem.get("MemTotal"):
            mem_pct = round(100.0 * (1 - mem.get("MemAvailable", 0) / mem["MemTotal"]))
        up = None
        try:
            up = float(open("/proc/uptime").read().split()[0])
        except Exception:
            pass
        internet = self.cached("internet", 10, lambda: tcp_check(cfg["INTERNET_HOST"], cfg["INTERNET_PORT"], 2.0)) is not None
        ntp = self.cached("ntp", 30, ntp_synced)
        throttled = self.cached("throttle", 30, lambda: (run(["vcgencmd", "get_throttled"])[1].strip().split("=")[-1] or None))
        return {
            "iface": iface, "iface_ip": iface_ip(iface),
            "iface_up": (read_text("/sys/class/net/%s/operstate" % iface) or "") in ("up", "unknown"),
            "mtu": mtu, "mtu_expected": expected, "mtu_ok": mtu == expected,
            "internet": internet,
            "cpu_temp": round(temp / 1000.0, 1) if temp is not None else None,
            "load": [round(x, 2) for x in os.getloadavg()],
            "cpus": os.cpu_count(), "mem_pct": mem_pct, "uptime": up,
            "throttled": throttled if throttled and throttled != "0x0" else None,
            "hostname": socket.gethostname(), "ntp_synced": ntp,
        }

    def nodes_cfg(self):
        """The nodes named in deck.ini as [(name, ip)]. NODE_IP may list several, NODE_NAME their names in the same order."""
        ips = [x.strip() for x in self.cfg["NODE_IP"].split(",") if x.strip()]
        names = [x.strip() for x in self.cfg["NODE_NAME"].split(",")]
        return [(names[i] if i < len(names) and names[i] else ("mesh node" if len(ips) == 1 else "node %d" % (i + 1)), ip)
                for i, ip in enumerate(ips)]

    def found_nodes(self):
        """NODE_IP blank: the mesh nodes this deck can see on MESH_IFACE (looked for once a minute).
        Candidates are the default gateway, the DHCP server and the neighbours on the port. A candidate counts as a mesh
        node when it serves a web page (the node's settings page); phones normally do not. A node found once is kept
        for 10 minutes after it last answered, so one that goes quiet shows as not answering instead of vanishing."""
        now = time.time()
        if not hasattr(self, "_found"):
            self._found, self._found_t, self._kind = {}, 0, {}
        if now - self._found_t >= 60:
            self._found_t = now
            iface = self.cfg["MESH_IFACE"]
            cand = []
            rc, out = run(["ip", "-4", "route", "show", "default", "dev", iface])
            if rc == 0:
                cand += re.findall(r"\bvia (\d+\.\d+\.\d+\.\d+)", out or "")
            if shutil.which("nmcli"):
                rc, out = run(["nmcli", "-g", "DHCP4.OPTION", "device", "show", iface])
                if rc == 0:
                    cand += re.findall(r"dhcp_server_identifier = (\d+\.\d+\.\d+\.\d+)", out or "")
            rc, out = run(["ip", "-4", "neigh", "show", "dev", iface])
            for line in (out or "").splitlines() if rc == 0 else []:
                f = line.split()
                if f and "lladdr" in f and f[-1] not in ("FAILED", "INCOMPLETE"):
                    cand.append(f[0])
            for ip in dict.fromkeys(cand):
                k = self._kind.get(ip)
                if k is None or now - k[1] > 600:
                    self._kind[ip] = k = (tcp_check(ip, 80, timeout=0.5) is not None, now)
                if k[0]:
                    self._found.setdefault(ip, now)
            for ip in [ip for ip, t in self._found.items() if now - t > 600]:
                del self._found[ip]
        return sorted(self._found, key=lambda ip: [int(x) for x in ip.split(".")])

    def mesh_view(self):
        """What the mesh itself reports (alfred + each node's status): {ip: {name, health}}, links, discovery state."""
        if not self.meshwatch:
            return {}, [], {"source": "off", "ok": False, "why": "MESH_DISCOVERY = off"}
        snap = self.meshwatch.snapshot()
        listed = {n["ip"]: n for n in snap["nodes"]}
        st = snap["state"]
        links = snap["links"] if snap["links_age"] is not None and snap["links_age"] < 120 else []
        return listed, links, {"source": "alfred", "ok": st["ok"], "why": st.get("why", ""), "servers": st.get("servers", 0)}

    def _ping_nodes(self):
        named = self.nodes_cfg()
        listed, _, _ = self.mesh_view()
        nodes = [(n, ip, "named") for n, ip in named]
        have = {ip for _, ip in named}
        for ip, n in sorted(listed.items(), key=lambda kv: kv[1]["name"].lower()):
            if ip not in have:
                nodes.append((n["name"], ip, "mesh"))
                have.add(ip)
        if not nodes:
            nodes = [(ip, ip, "port") for ip in self.found_nodes()]
        rtts = []
        if nodes:
            with ThreadPoolExecutor(max_workers=min(8, len(nodes))) as ex:
                rtts = list(ex.map(lambda n: ping(n[1]), nodes))
        now = time.time()
        res = []
        for (n, ip, src), r in zip(nodes, rtts):
            h = (listed.get(ip) or {}).get("health") or {}
            res.append({"name": n, "ip": ip, "ok": r is not None, "rtt_ms": r, "found": src == "port", "source": src,
                        "listed": ip in listed, "mesh_name": (listed.get(ip) or {}).get("name", ""), "nearest": False,
                        "cpu_c": h.get("cpu_c"), "load_pct": h.get("load_pct"), "mem_pct": h.get("mem_pct"), "uptime": h.get("uptime")})
        up = [x for x in res if x["ok"]]
        best = min(up, key=lambda x: x["rtt_ms"]) if up else None
        if best:
            best["nearest"] = True
        for x in up:
            if x["source"] == "port":
                self._found[x["ip"]] = now
        r = best["rtt_ms"] if best else None
        self.history.add_ping(now, r)
        self.pings.append(r)
        return res

    def mesh_links(self, nodes):
        """Radio links between nodes from alfred, named the way the node list names them, graded by throughput."""
        _, links, _ = self.mesh_view()
        disp = {x["mesh_name"]: x["name"] for x in nodes if x.get("mesh_name")}
        try:
            good, poor = float(self.cfg.get("LINK_GOOD_MBPS", 3)), float(self.cfg.get("LINK_POOR_MBPS", 1))
        except ValueError:
            good, poor = 3.0, 1.0
        out = []
        for l in links:
            mbps = round(l["kbps"] / 1000.0, 1) if l.get("kbps") else None
            out.append({"a": disp.get(l["a"], l["a"]), "b": disp.get(l["b"], l["b"]), "mbps": mbps,
                        "grade": link_grade(mbps, good, poor)})
        return out, good, poor

    def voice(self):
        """manet-voice's status, cut down to what the voice row shows. None = voice was never set up."""
        try:
            with open(self.cfg["VOICE_STATUS_FILE"]) as f:
                st = json.load(f)
            age = time.time() - os.path.getmtime(self.cfg["VOICE_STATUS_FILE"])
        except (OSError, ValueError):
            return None
        groups = []
        for g in st.get("groups") or []:
            t = g.get("talking")
            groups.append({"name": str(g.get("name", "?")), "bridged": bool(g.get("bridged")), "mumble_up": bool(g.get("mumble_up")),
                           "talking": {"name": str(t.get("name", "?")), "via": str(t.get("via", ""))} if isinstance(t, dict) else None,
                           "mesh": len(g.get("mesh") or []), "mumble": len(g.get("mumble") or [])})
        p = st.get("deck_ptt") if isinstance(st.get("deck_ptt"), dict) else {}
        ptt = {"enabled": bool(p.get("enabled")), "present": bool(p.get("present")), "tg": str(p.get("tg") or ""),
               "talking": bool(p.get("talking")), "error": str(p.get("error") or "")[:200]} if p.get("enabled") else None
        return {"running": age < 10, "groups": groups, "deck_ptt": ptt}

    def mesh(self, deck, bridge=None):
        """The mesh link: OK when any node answers a ping. If none answers but mesh traffic arrived in the last minute,
        the mesh is still up (TRAFFIC ONLY); NODE LOST only when there is neither."""
        nodes = self.cached("node", 3, self._ping_nodes)
        best = next((x for x in nodes if x["nearest"]), None)
        ok = best is not None
        t_age = bridge.get("mesh_last_age") if bridge and bridge.get("running") else None
        traffic = t_age is not None and t_age < 60
        last = list(self.pings)[-30:]
        got = [x for x in last if x is not None]
        loss = round(100.0 * (len(last) - len(got)) / len(last), 1) if last else None
        jit = None
        if len(got) > 1:
            jit = round(sum(abs(a - b) for a, b in zip(got, got[1:])) / (len(got) - 1), 2)
        if not deck["iface_up"]:
            tile, text, state = "bad", "NODE LOST", "PORT DOWN"
        elif ok:
            tile, text, state = ("ok", "LINK OK", "LINK OK") if deck["mtu_ok"] else ("warn", "CHECK MTU", "CHECK MTU")
        elif traffic:
            tile, text, state = "warn", "LINK OK", "TRAFFIC ONLY"
        elif nodes:
            tile, text, state = "bad", "NODE LOST", "NODE LOST"
        else:
            tile, text, state = "warn", "NO NODE", "NO NODE FOUND"
        links, good, poor = self.mesh_links(nodes)
        _, _, disc = self.mesh_view()
        n_up, n_all = sum(1 for x in nodes if x["ok"]), len(nodes)
        if n_all > 1 and deck["iface_up"] and ok and deck["mtu_ok"]:   # several nodes: say how many answer
            text = "%d NODES" % n_all if n_up == n_all else "%d OF %d NODES" % (n_up, n_all)
            if n_up < n_all:
                tile, state = "warn", "NODE LOST"
        speeds = [l["mbps"] for l in links if l["mbps"] is not None]
        slowest = min(speeds) if speeds else None
        first = best or (nodes[0] if nodes else None)
        return {"links": links, "link_good": good, "link_poor": poor, "slowest_mbps": slowest, "discovery": disc,
                "nodes_up": n_up, "nodes_total": n_all,
                "node_name": first["name"] if first else "mesh node", "node_ip": first["ip"] if first else "", "node_ok": ok,
                "node_rtt_ms": best["rtt_ms"] if best else None, "nodes": nodes, "tile": tile, "text": text, "state": state,
                "traffic": traffic, "traffic_age": t_age,
                "loss_pct": loss, "rtt_avg": round(sum(got) / len(got), 1) if got else None,
                "rtt_min": min(got) if got else None, "rtt_max": max(got) if got else None, "jitter": jit, "samples": len(last)}

    # -- bridge activity (relaying, idle, degraded, down)
    def activity(self, bridge, server, mesh):
        if not bridge["running"]:
            why = "service stopped" if bridge["service"] != "active" else "status file is stale"
            return {"state": "down", "speed": 1, "text": "Bridge is down (%s)" % why}
        if mesh["tile"] == "bad":
            return {"state": "alert", "speed": 1, "text": "Mesh link lost (no node answers and no mesh traffic)"}
        if not server["established"]:
            return {"state": "alert", "speed": 1, "text": "Bridge is up but not connected to the server"}
        r = bridge["msgs_per_min"]
        if r <= 0:
            return {"state": "sleep", "speed": 1, "text": "Connected, no traffic for the last minute"}
        return {"state": "hop", "speed": round(0.7 + min(r, 120) / 120.0 * 1.5, 2),
                "text": "Relaying %d messages per minute" % r}

    def link_events(self, links):
        """One event per change: a link turning POOR, recovering, appearing or going away."""
        cur = {l["a"] + " \u2194 " + l["b"]: l for l in links}
        old = self.prev.get("links")
        self.prev["links"] = {k: l["grade"] for k, l in cur.items()}
        if old is None:
            return
        for k, l in cur.items():
            g, was = l["grade"], old.get(k)
            sp = "%s Mbit/s" % l["mbps"] if l["mbps"] is not None else "no speed reported"
            if was is None:
                self.events.add("ok", "Radio link %s up (%s %s)" % (k, sp, g), "mesh")
            elif g == "POOR" and was != "POOR":
                self.events.add("warn", "Radio link %s POOR (%s)" % (k, sp), "mesh")
            elif was == "POOR" and g != "POOR":
                self.events.add("ok", "Radio link %s back to %s (%s)" % (k, g, sp), "mesh")
        for k in old:
            if k not in cur:
                self.events.add("warn", "Radio link %s gone" % k, "mesh")

    def write_mesh_status(self, mesh, now):
        """A small summary for the bridge (the DECK status line on the TAK map). Written atomically; best effort."""
        path = self.cfg.get("MESH_STATUS_FILE")
        if not path or path.lower() == "off":
            return
        if now - self.prev.get("mesh_status_t", 0) < 10:
            return
        self.prev["mesh_status_t"] = now
        out = {"now": now, "nodes_total": mesh["nodes_total"], "nodes_up": mesh["nodes_up"], "links": len(mesh["links"]),
               "slowest_mbps": mesh["slowest_mbps"], "lost": [n["name"] for n in mesh["nodes"] if not n["ok"]]}
        try:
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(out, f)
            os.replace(tmp, path)
            Disk.ok("mesh-status")
        except OSError as exc:
            warn_once("mesh-status", "cannot write %s (%s)" % (path, exc))
            Disk.fail("mesh-status", "cannot write the mesh status file (%s)" % exc)

    # -- one pass
    def collect(self):
        bridge = self.bridge()
        server = self.server(bridge)
        deck = self.deck()
        mesh = self.mesh(deck, bridge)
        video = self.mediamtx()
        act = self.activity(bridge, server, mesh)
        ready = [p for p in video["paths"] if p["ready"]]
        stalled = [p for p in ready if p.get("stalled")]
        if not video["running"]:
            vtile, vtext = "warn", "MEDIAMTX OFF"
        elif stalled:
            vtile, vtext = "warn", "%d STALLED" % len(stalled)
        elif ready:
            vtile, vtext = "ok", "%d LIVE" % len(ready)
        else:
            vtile, vtext = "ok", "IDLE"
        btile = {"hop": "ok", "sleep": "ok", "alert": "warn", "down": "bad"}[act["state"]]
        tiles = {
            "bridge": {"level": btile, "text": {"hop": "RELAYING", "sleep": "IDLE", "alert": "DEGRADED", "down": "DOWN"}[act["state"]]},
            "server": {"level": server["tile"], "text": {"ok": "CONNECTED", "warn": "NOT LINKED", "bad": "UNREACHABLE"}[server["tile"]]},
            "video": {"level": vtile, "text": vtext},
            "mesh": {"level": mesh["tile"], "text": mesh["text"]},
        }
        # ---- alert rules (set in the page) can raise tiles
        now = time.time()
        if bridge["running"] and bridge["msgs_per_min"] > 0:
            self.last_traffic = now
        metrics = {
            "bridge_idle_min": ((now - self.last_traffic) / 60.0) if bridge["running"] else None,
            "cpu_temp": deck["cpu_temp"], "mem_pct": deck["mem_pct"],
            "mesh_rtt": mesh["node_rtt_ms"], "mesh_loss": mesh["loss_pct"], "server_rtt": server["rtt_ms"],
            "link_mbps": mesh["slowest_mbps"],
        }
        active = self.alerts.evaluate(metrics, now)
        faults = fault_alerts(bridge.get("faults"))
        on = {a["id"][6:]: a for a in faults}
        for area, (level, label) in FAULT_TEXT.items():      # every known area, so the first fault is logged too
            self.changed("fault:" + area, area not in on, "%s: working again" % label,
                         on[area]["text"] if area in on else label, level_down=level, cat="bridge")
        active = active + faults
        voice = self.voice()
        ptt_err = (voice or {}).get("running") and ((voice or {}).get("deck_ptt") or {}).get("error") or ""
        self.changed("deck_ptt", not ptt_err, "PTT box working again", "PTT box: %s" % ptt_err, level_down="warn", cat="deck")
        if ptt_err:
            active.append({"id": "deck_ptt", "level": "warn", "tile": "deck", "text": "PTT box: %s" % ptt_err[:140], "value": ""})
        disk = Disk.failing()
        self.changed("disk_ok", disk is None, "Deck disk writable again",
                     "Deck disk not writable: %s" % (disk[1] if disk else ""), cat="deck")
        if disk:
            active.append({"id": "disk", "level": "bad", "tile": "deck", "text": "Deck disk not writable: %s" % disk[1][:140],
                           "value": "since " + time.strftime("%H:%M", time.localtime(disk[0]))})
        rank = {"ok": 0, "info": 0, "warn": 1, "bad": 2}
        for al in active:
            if al["tile"] in tiles:
                t = tiles[al["tile"]]
                if rank[al["level"]] > rank.get(t["level"], 0):
                    t["level"] = al["level"]
                t["alert"] = al["text"] if not t.get("alert") else t["alert"] + "; " + al["text"]
        # ---- history (strip + sparklines)
        self.history.tick(now, {"b": rank[tiles["bridge"]["level"]], "s": rank[tiles["server"]["level"]],
                                "v": rank[tiles["video"]["level"]], "x": rank[tiles["mesh"]["level"]]})
        kbps = sum(p["kbps"] or 0 for p in ready) if ready else 0
        self.history.sample(now, msgs=bridge["msgs_per_min"], rtt=mesh["node_rtt_ms"], temp=deck["cpu_temp"],
                            kbps=kbps, load=deck["load"][0], srv=server["rtt_ms"])
        # ---- events from changes
        self.changed("bridge_running", bridge["running"], "Bridge is running", "Bridge stopped or status went stale", cat="bridge")
        pid = bridge["pid"]
        if pid and self.prev.get("pid") and pid != self.prev["pid"]:
            self.events.add("warn", "Bridge restarted (new process %s)" % pid, "bridge")
        if pid:
            self.prev["pid"] = pid
        self.changed("srv_est", server["established"], "Server link established", "Server link lost", cat="server")
        self.changed("srv_reach", server["reachable"], "Server reachable again", "Server unreachable (no bridge link, no ping reply)", cat="server")
        self.changed("mesh_up", mesh["tile"] != "bad", "Mesh link back", "Mesh link lost (no node answers, no mesh traffic)", cat="mesh")
        known = self.prev.get("mesh_known")
        now_known = {n["ip"]: n["name"] for n in mesh["nodes"] if n.get("listed")}
        if known is not None:
            for ip, name in now_known.items():
                if ip not in known:
                    self.events.add("ok", "%s joined the mesh (%s)" % (name, ip), "mesh")
            for ip, name in known.items():
                if ip not in now_known:
                    self.events.add("info", "%s left the mesh's node list" % name, "mesh")
        self.prev["mesh_known"] = now_known
        for n in mesh["nodes"]:
            self.changed("node:" + n["ip"], n["ok"], "%s answers again" % n["name"], "%s lost (not answering)" % n["name"], level_down="warn", cat="mesh")
        self.link_events(mesh["links"])
        self.history.add_links(now, {l["a"] + "|" + l["b"]: l["mbps"] for l in mesh["links"]})
        self.write_mesh_status(mesh, now)
        self.changed("mtu_ok", deck["mtu_ok"], "MTU back to %s" % deck["mtu_expected"], "MTU is %s (expected %s)" % (deck["mtu"], deck["mtu_expected"]), level_down="warn", cat="mesh")
        if deck["ntp_synced"] is not None:
            self.changed("ntp", deck["ntp_synced"], "Clock synced", "Clock not synced", level_down="warn", cat="deck")
        self.changed("internet", deck["internet"], "Internet is back", "Internet lost", level_down="warn", cat="deck")
        self.changed("tailscale", server["tailscale_up"], "Tailscale is up", "Tailscale is down", level_down="warn", cat="server")
        self.changed("mtx", video["running"], "MediaMTX is running", "MediaMTX is not running", level_down="warn", cat="video")
        seen = set()
        for p in video["paths"]:
            seen.add(p["name"])
            self.changed("stream:" + p["name"], p["ready"], "Stream '%s' started" % p["name"], "Stream '%s' ended" % p["name"], level_down="info", cat="video")
            self.changed("stall:" + p["name"], not p.get("stalled"), "Stream '%s' moving again" % p["name"], "Stream '%s' stalled (live but no data)" % p["name"], level_down="warn", cat="video")
            self.changed("readers:" + p["name"], p["readers"] > 0, "Someone is watching '%s'" % p["name"], "Nobody watching '%s'" % p["name"], level_up="info", level_down="info", cat="video")
        for k in [k for k in self.prev if k.startswith("stream:") and k[7:] not in seen]:
            if self.prev[k]:
                self.events.add("info", "Stream '%s' ended" % k[7:], "video")
            self.prev[k] = False
        if self.prev.get("activity") != act["state"]:
            if self.prev.get("activity") is not None:
                self.events.add("info", "Bridge: %s" % act["text"], "bridge")
            self.prev["activity"] = act["state"]
        video["markers"] = self.video_markers(video, deck, now)
        video["relay"] = getattr(self, "relay_status", None) if self.relay else {"state": "off"}
        gps = gps_tile(self.gps.snapshot(), int(self.cfg.get("MGRS_DIGITS", "5"))) if self.gps else None
        if gps:
            self.changed("gps_fix", gps["mode"] >= 2, "GPS fix (%s)" % gps["fix"], "GPS fix lost", level_down="warn", cat="deck")
            self.changed("gps_dev", gps["fix"] != "NO GPS", "GPS puck connected", "GPS puck not available (%s)" % gps["detail"], level_down="warn", cat="deck")
        snap = {
            "gps": gps,
            "now": time.time(), "host": deck["hostname"], "deck_ip": deck["iface_ip"],
            "dashboard_uptime": time.time() - START, "mock": False,
            "tiles": tiles, "activity": act, "bridge": bridge, "server": server,
            "video": video, "deck": deck, "mesh": mesh, "voice": voice, "alerts": active,
            "caps": self.control.caps(), "events": self.events.recent(100),
        }
        try:
            snap["setup"] = setup_checks(self.cfg, snap, self.control, self.alerts)
        except Exception as exc:
            snap["setup"] = [{"ok": False, "level": "warn", "title": "Setup check failed", "detail": repr(exc)[:120], "fix": ""}]
        return snap

    def run(self):
        first = True
        while True:
            self.step(first)
            first = False
            time.sleep(2.0)

    def step(self, first=False):
        """One collection. A failure keeps the last snapshot, but get() then says it is old and why."""
        try:
            snap = self.collect()
            if first:
                self.events.add("info", "Dashboard started")
                snap["events"] = self.events.recent(100)
            with self.lock:
                self.snapshot = snap
            if self.collect_error:
                self.events.add("ok", "Ops data updating again", "system")
                self.collect_error = None
        except Exception as exc:                 # keep going no matter what
            err = repr(exc)[:160]
            sys.stderr.write("collector error: %s\n" % err)
            if not self.collect_error:
                self.events.add("bad", "Ops data not updating: %s" % err, "system")
            self.collect_error = err

    def get(self):
        """The latest snapshot. When it is older than STALE_AFTER (collecting failed or hung), a copy with an alert
        saying so, so the page can't show old data as if it were live."""
        with self.lock:
            snap = self.snapshot
        age = time.time() - float(snap.get("now") or 0)
        if snap.get("booting") or age < STALE_AFTER:
            return snap
        snap = dict(snap)
        why = self.collect_error or "collecting is stuck"
        snap["alerts"] = [{"id": "ops_stale", "level": "bad", "tile": "", "value": "%d s old" % age,
                           "text": "Ops data stale since %s: %s" % (time.strftime("%H:%M:%S", time.localtime(snap.get("now") or 0)), why)}] \
            + list(snap.get("alerts") or [])
        snap["stale"] = True
        return snap


# ----------------------------------------------------------------- mock data
SCENARIOS = ["hop", "hop-heavy", "sleep", "alert", "down"]


def mock_status(scenario, events):
    t = time.time()
    if scenario == "auto":
        scenario = SCENARIOS[int(((t - START) % (len(SCENARIOS) * 14)) // 14)]
    rate = {"hop": 34, "hop-heavy": 96, "sleep": 0, "alert": 0, "down": 0}[scenario]
    rate = int(rate + 6 * math.sin(t / 3)) if rate else 0
    up = {"position": int(rate * 0.7), "chat": int(rate * 0.1), "marker": int(rate * 0.05)}
    up["total"] = sum(up.values())
    down = {"position": int(rate * 0.12), "chat": int(rate * 0.03)}
    down["total"] = sum(down.values())
    down_state = scenario == "down"
    alert = scenario == "alert"
    running = not down_state
    state = "down" if down_state else "alert" if alert else "sleep" if rate == 0 else "hop"
    text = {"down": "Bridge is down (service stopped)", "alert": "Bridge is up but not connected to the server",
            "sleep": "Connected, no traffic for the last minute"}.get(state, "Relaying %d messages per minute" % (up["total"] + down["total"]))
    total = up["total"] + down["total"]
    speed = round(0.7 + min(total, 120) / 120.0 * 1.5, 2)
    ev = [
        {"t": t - 5, "level": "info", "text": "Stream 'test3' started", "cat": "video"},
        {"t": t - 21, "level": "info", "text": "Snapshot saved: snap_test3_20261017-221004.jpg", "cat": "video"},
        {"t": t - 61, "level": "ok", "text": "Server link established", "cat": "server"},
        {"t": t - 64, "level": "bad", "text": "Server link lost", "cat": "server"},
        {"t": t - 190, "level": "ok", "text": "Alert cleared: Node ping loss > 25", "cat": "alert"},
        {"t": t - 310, "level": "warn", "text": "Alert: Node ping loss > 25 (now 31.0)", "cat": "alert"},
        {"t": t - 380, "level": "warn", "text": "MTU is 1500 (expected 1460)", "cat": "mesh"},
        {"t": t - 900, "level": "info", "text": "Restarted manet-bridge from the dashboard", "cat": "action"},
        {"t": t - 3600, "level": "info", "text": "Dashboard started", "cat": "system"},
    ]
    if down_state:
        ev.insert(0, {"t": t - 3, "level": "bad", "text": "Bridge stopped or status went stale", "cat": "bridge"})
    if alert:
        ev.insert(0, {"t": t - 3, "level": "bad", "text": "Server link lost", "cat": "server"})
    alerts = []
    tiles_alert = {}
    if scenario == "alert":
        alerts = [{"id": "r2", "level": "warn", "tile": "mesh", "text": "Node ping loss > 25", "value": 31.0},
                  {"id": "r3", "level": "warn", "tile": "none", "text": "CPU temperature > 80", "value": 82.4}]
        tiles_alert = {"mesh": "Node ping loss > 25"}
    if scenario == "down":
        alerts = [{"id": "r1", "level": "bad", "tile": "bridge", "text": "Bridge idle > 5", "value": 7.3}]
        tiles_alert = {"bridge": "Bridge idle > 5"}
    streams = [
        {"name": "live", "ready": True, "source": "rtspSession", "readers": 2, "kbps": 0 if scenario == "alert" else 1180 + int(80 * math.sin(t)), "bytes_in": 91234567, "bytes_out": 123456789, "codecs": ["H264"], "since": None, "stalled": scenario == "alert"},
        {"name": "drone", "ready": False, "source": "", "readers": 0, "kbps": None, "bytes_in": 0, "bytes_out": 0, "codecs": [], "since": None, "stalled": False},
    ]
    sev = {"hop": "ok", "sleep": "ok", "alert": "warn", "down": "bad"}[state]
    out = {
        "now": t, "host": "deck", "deck_ip": "10.41.0.50", "dashboard_uptime": t - START, "mock": True, "scenario": scenario,
        "tiles": {
            "bridge": {"level": sev, "text": {"hop": "RELAYING", "sleep": "IDLE", "alert": "DEGRADED", "down": "DOWN"}[state]},
            "server": {"level": "bad" if alert else "ok", "text": "UNREACHABLE" if alert else "CONNECTED"},
            "video": {"level": "warn", "text": "1 STALLED"} if scenario == "alert" else {"level": "ok", "text": "1 LIVE"},
            "mesh": {"level": "warn" if scenario == "alert" else "ok", "text": "2 OF 3 NODES" if scenario == "alert" else "3 NODES"},
        },
        "activity": {"state": state, "speed": speed, "text": text},
        "alerts": alerts,
        "bridge": {"service": "inactive" if down_state else "active", "running": running, "stale": False, "has_status": True, "pid": 4242,
                   "uptime": None if down_state else 5 * 3600 + 1234, "started": t - 5000, "msgs_per_min": total,
                   "per_minute": {"up": up, "down": down, "total": total},
                   "mesh_events": 18234, "mesh_last_age": None if rate == 0 else 2.4, "mesh_last_from": "10.41.0.20",
                   "mesh_skipped": 0, "mesh_failed": 0, "return_enabled": True, "return_sent": 4120,
                   "return_last_age": None if rate == 0 else 6.1, "server_rx": 20311, "server_last_age": 1.8,
                   "mesh_url": "udp+ro://239.2.3.1:6969", "local_addr": "10.41.0.50"},
        "server": {"host": "tak.example.net", "port": 8089, "reachable": not alert, "rtt_ms": None if alert else 38.2,
                   "established": not alert and not down_state, "last_rx_age": 1.8, "rx_events": 20311,
                   "tailscale_ip": "100.64.0.10", "tailscale_up": True, "tile": "bad" if alert else "ok",
                   "link": None if alert else {"state": "up", "reason": "", "answers_pings": True, "rtt_ms": 41.7, "last_pong_age": 6,
                                               "ping_interval": 20, "timeout": 60, "since_age": 2 * 3600 + 14 * 60, "starts": 3,
                                               "prev_reason": "the server closed the connection", "prev_down_age": 2 * 3600 + 14 * 60}},
        "video": {"api_ok": True, "running": True, "paths": streams, "error": None, "rtsp_port": 8554,
                  "markers": {"state": "on", "count": 2, "last_sent": t - 40, "located": 1},
                  "webrtc_port": 8889, "hls_port": 8888, "default_stream": "live"},
        "deck": {"iface": "eth0", "iface_ip": "10.41.0.50", "iface_up": True, "mtu": 1460, "mtu_expected": 1460, "mtu_ok": True,
                 "internet": True, "cpu_temp": 52.1, "load": [0.42, 0.51, 0.47], "cpus": 4, "mem_pct": 31, "uptime": 86400 * 2 + 3600,
                 "throttled": None, "hostname": "deck", "ntp_synced": scenario != "alert"},
        "mesh": {"node_name": "NODE_1", "node_ip": "10.41.0.1", "node_ok": True, "node_rtt_ms": 2.1, "tile": "warn" if scenario == "alert" else "ok",
                 "text": "2 OF 3 NODES" if scenario == "alert" else "3 NODES", "state": "NODE LOST" if scenario == "alert" else "LINK OK",
                 "traffic": rate > 0, "traffic_age": None if rate == 0 else 2.4,
                 "nodes_up": 2 if scenario == "alert" else 3, "nodes_total": 3, "link_good": 3.0, "link_poor": 1.0,
                 "slowest_mbps": None if scenario == "alert" else round(2.4 + 0.3 * math.sin(t / 7), 1),
                 "discovery": {"source": "alfred", "ok": True, "why": "", "servers": 3},
                 "links": [{"a": "NODE_1", "b": "NODE_2", "mbps": 7.1, "grade": "GOOD"}] + ([] if scenario == "alert" else
                          [{"a": "NODE_2", "b": "NODE_3", "mbps": round(2.4 + 0.3 * math.sin(t / 7), 1), "grade": "OK"}]),
                 "nodes": [{"name": "NODE_1", "ip": "10.41.0.1", "ok": True, "rtt_ms": 2.1, "found": False, "nearest": True, "source": "named",
                            "listed": True, "mesh_name": "NODE_1", "cpu_c": 48.2, "load_pct": 2.1, "mem_pct": 8.0, "uptime": 101000},
                           {"name": "NODE_2", "ip": "10.41.254.10", "ok": True, "rtt_ms": 4.6, "found": False, "nearest": False, "source": "mesh",
                            "listed": True, "mesh_name": "NODE_2", "cpu_c": 45.8, "load_pct": 1.4, "mem_pct": 7.3, "uptime": 960},
                           {"name": "NODE_3", "ip": "10.41.254.20", "ok": scenario != "alert", "rtt_ms": None if scenario == "alert" else 9.8,
                            "found": False, "nearest": False, "source": "mesh", "listed": True, "mesh_name": "NODE_3",
                            "cpu_c": 51.0, "load_pct": 3.2, "mem_pct": 9.1, "uptime": 5400}],
                 "loss_pct": 31.0 if scenario == "alert" else 0.0, "rtt_avg": 2.3, "rtt_min": 1.6, "rtt_max": 4.8, "jitter": 0.6, "samples": 30},
        "voice": {"running": True, "groups": [
            {"name": "TG1", "bridged": True, "mumble_up": True, "mesh": 3, "mumble": 2,
             "talking": {"name": "ALPHA-2", "via": "mesh"} if scenario in ("hop", "hop-heavy") else None},
            {"name": "TG2", "bridged": False, "mumble_up": False, "mesh": 0, "mumble": 0, "talking": None}]},
        "caps": {"control": True, "restart_bridge": True, "ffmpeg": True},
        "events": ev,
    }
    for k, v in tiles_alert.items():
        out["tiles"][k]["alert"] = v
        out["tiles"][k]["level"] = "bad" if k == "bridge" else "warn"
    gps_states = {"hop": ("ok", "3D FIX", "12S UE 97314 90583", "9 SAT \u00b7 \u00b13 m \u00b7 1 s ago \u00b7 2100 m", 3),
                  "hop-heavy": ("warn", "2D FIX", "12S UE 97317 90579", "4 SAT \u00b7 \u00b118 m \u00b7 1 s ago \u00b7 no alt", 2),
                  "sleep": ("ok", "3D FIX", "12S UE 97314 90583", "8 SAT \u00b7 \u00b14 m \u00b7 1 s ago \u00b7 2100 m", 3),
                  "alert": ("warn", "NO FIX", "12S UE 97314 90583", "last fix 4 min ago \u00b7 STALE", 0),
                  "down": ("bad", "NO GPS", "-- -- ----- -----", "puck not detected \u00b7 check USB", 0)}
    lv, fx, gr, dt, md = gps_states[scenario]
    out["gps"] = {"level": lv, "fix": fx, "grid": gr, "detail": dt, "mode": md}
    out["setup"] = mock_setup(out)
    return out


class _MockCtl:
    def can_restart(self):
        return True


class _MockAlerts:
    errors = []


def mock_setup(snap):
    cfg = dict(DEFAULTS, BRIDGE_UNIT="manet-bridge")
    items = setup_checks(cfg, snap, _MockCtl(), _MockAlerts())
    for it in items:                       # demo data: show a fully set-up deck, whatever this machine has installed
        if it["title"] in ("System tools", "ffmpeg (for Snap)"):
            it.update(ok=True, level="ok", detail="found", fix="")
    return items


def mock_history():
    t = time.time()
    r = random_for(int(t // 600))
    n = 360
    t0 = t - (n - 1) * 10
    sp = {"step": 10, "t0": t0, "msgs": [], "rtt": [], "temp": [], "kbps": [], "load": [], "srv": []}
    for i in range(n):
        x = t0 + i * 10
        sp["msgs"].append(max(0, int(30 + 22 * math.sin(x / 90.0) + 10 * math.sin(x / 17.0))))
        sp["rtt"].append(None if (i % 97) == 50 else round(2.0 + 0.8 * math.sin(x / 40.0) + (r.random() * 0.6), 2))
        sp["temp"].append(round(50 + 5 * math.sin(x / 400.0) + r.random(), 1))
        sp["kbps"].append(int(1100 + 150 * math.sin(x / 25.0)) if (i % 120) < 100 else 0)
        sp["load"].append(round(0.4 + 0.2 * math.sin(x / 60.0), 2))
        sp["srv"].append(round(38 + 6 * math.sin(x / 55.0) + r.random() * 3, 1))
    ping = []
    for i in range(300):
        x = t - (300 - i) * 3.0
        v = None if i in (60, 61, 200) else round(2.0 + 0.9 * math.sin(x / 31.0) + r.random() * 0.8, 2)
        if 120 < i < 130:
            v = round(9 + r.random() * 6, 2)
        ping.append([round(x, 1), v])
    end = int(t // 60)
    strip = {"step": 60, "t0": (end - STRIP_MINUTES + 1) * 60}
    rows = {"bridge": [], "server": [], "video": [], "mesh": []}
    for m in range(end - STRIP_MINUTES + 1, end + 1):
        age = end - m
        for name in rows:
            c = "0"
            if age > 1380:
                c = "-"                          # dashboard was not running yet
            elif name == "server" and 300 < age < 312:
                c = "2"
            elif name == "mesh" and (600 < age < 606 or 90 < age < 95):
                c = "1"
            elif name == "bridge" and 700 < age < 703:
                c = "2"
            elif name == "bridge" and 701 < age < 720:
                c = "1"
            elif name == "video" and (age % 200) < 3:
                c = "1"
            rows[name].append(c)
    for name, v in rows.items():
        strip[name] = "".join(v)
    links = []
    for i in range(120):                     # one hour at 30 s: NODE_2 walks out and back on a range test
        x = t - (119 - i) * 30
        dip = max(0.0, 1 - abs(i - 70) / 25.0)
        links.append([round(x, 1), {"NODE_1|NODE_2": round(max(0.3, 7.1 - 6.6 * dip + r.random() * 0.4), 1),
                                    "NODE_2|NODE_3": round(2.4 + 0.4 * math.sin(x / 300.0) + r.random() * 0.2, 1)}])
    return {"now": t, "spark": sp, "ping": ping, "strip": strip, "links": links}


def random_for(seed):
    import random
    return random.Random(seed)


# ----------------------------------------------------------------- http
STATIC = {
    "index.html": "text/html; charset=utf-8",
    "app.js": "application/javascript; charset=utf-8",
    "charts.js": "application/javascript; charset=utf-8",
    "map.js": "application/javascript; charset=utf-8",
    "icon.svg": "image/svg+xml",
    "setup.js": "application/javascript; charset=utf-8",
}
CAPTURE_NAME = re.compile(r"^snap_[A-Za-z0-9_.\-]{1,80}\.jpg$")


class Handler(BaseHTTPRequestHandler):
    server_version = "manet-dashboard"
    wizard = {}             # the first-run wizard's state (one deck screen, one wizard): package, PEMs, test result
    collector = None
    events = None
    mock = None
    history = None
    alerts = None
    control = None
    cfg = None

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype, cache="no-store"):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj), "application/json")

    def _is_local(self):
        """Controls and captures only answer the deck's own screen (127.0.0.1 / localhost)."""
        if self.client_address[0] not in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
            return False
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
        if host not in ("127.0.0.1", "localhost", "::1"):
            return False          # blocks DNS-rebinding tricks
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).hostname not in ("127.0.0.1", "localhost", "::1"):
            return False
        return True

    def do_GET(self):
        u = urlparse(self.path)
        path = u.path
        name = path.lstrip("/") or "index.html"
        if name == "index.html":
            try:
                with open(os.path.join(HERE, name), "rb") as f:
                    html = f.read()
            except OSError:
                return self._send(404, "missing index.html\n", "text/plain")
            return self._send(200, html, STATIC[name])
        if name in STATIC:
            return self._file(name, STATIC[name])
        if path.startswith("/fonts/"):
            ff = font_file(path[len("/fonts/"):])
            if not ff:
                return self._send(404, "no such font\n", "text/plain")
            with open(ff, "rb") as f:
                return self._send(200, f.read(), "font/woff2", cache="max-age=86400")
        if path == "/healthz":
            return self._send(200, "ok\n", "text/plain")
        if path == "/setup" or path.startswith("/api/setup/"):
            return self._setup_get(path)
        if path == "/api/status":
            q = parse_qs(u.query)
            if Handler.mock:
                scenario = (q.get("scenario") or [Handler.mock])[0]
                data = mock_status(scenario, Handler.events)
                data["alerts"] = data.get("alerts", [])
            else:
                data = Handler.collector.get()
            data = dict(data)
            data["local"] = self._is_local()
            return self._json(data)
        if path == "/api/history":
            if Handler.mock:
                return self._json(mock_history())
            return self._json(Handler.history.export(time.time()))
        if path == "/api/alerts":
            return self._json({"rules": Handler.alerts.describe(), "errors": Handler.alerts.errors})
        if path == "/api/captures":
            if not self._is_local():
                return self._json({"files": [], "local_only": True})
            return self._json({"files": Handler.control.captures()})
        if path.startswith("/captures/"):
            return self._capture(path[len("/captures/"):])
        self._send(404, "not found\n", "text/plain")

    def _capture(self, name):
        if not self._is_local():
            return self._send(403, "captures are only available on the deck itself\n", "text/plain")
        if not CAPTURE_NAME.match(name):
            return self._send(404, "not found\n", "text/plain")
        fp = os.path.join(Handler.cfg["CAPTURE_DIR"], name)
        try:
            size = os.path.getsize(fp)
            f = open(fp, "rb")
        except OSError:
            return self._send(404, "not found\n", "text/plain")
        with f:
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition", 'inline; filename="%s"' % name)
            self.end_headers()
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except OSError:
                    break

    # -- first-run setup (setup_wizard.py): the deck's own screen only, like the controls
    def _setup_get(self, path):
        if not self._is_local():
            return self._send(403, "Setup only opens on the deck's own screen (http://127.0.0.1:%s/setup).\n" % Handler.cfg["PORT"], "text/plain")
        if path == "/setup":
            return self._file("setup.html", "text/html; charset=utf-8")
        cfgp = Handler.cfg["BRIDGE_INI"]
        if path == "/api/setup/state":
            ses = Handler.wizard
            cp = configparser.ConfigParser(interpolation=None, strict=False)
            try:
                cp.read(cfgp, encoding="utf-8-sig")
            except (OSError, configparser.Error):
                pass
            g = lambda sec, k, d="": cp.get(sec, k, fallback=d).strip()
            return self._json({"needed": bool(server_unset(Handler.cfg)), "config": cfgp, "facts": wiz.facts(PKG),
                               "interfaces": wiz.interfaces(), "teams": wiz.TEAMS, "roles": wiz.ROLES,
                               "uid": wiz.deck_uid(cfgp), "hostname": socket.gethostname(),
                               "current": {"callsign": g("deck", "CALLSIGN", "DECK"), "team": g("deck", "TEAM", "Cyan"),
                                           "role": g("deck", "ROLE", "HQ"), "gps": g("deck", "GPS", "off") == "gpsd",
                                           "mesh_addr": g("deck", "MESH_ADDR"), "return_path": g("bridge", "RETURN_PATH", "1") == "1",
                                           "dm_relay": g("bridge", "DM_RELAY", "0") == "1", "ops_local": g("dashboard", "LISTEN", "auto") == "127.0.0.1"},
                               "package": ses.get("summary")})
        if path == "/api/setup/check":
            return self._json(wiz.check_rows(PKG))
        if path == "/api/setup/helper.zip":
            name, blob, cmd = wiz.helper_zip(PKG, wiz.deck_uid(cfgp))
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", 'attachment; filename="%s"' % name)
            self.send_header("Content-Length", str(len(blob)))
            self.end_headers()
            return self.wfile.write(blob)
        if path == "/api/setup/helper":
            name, _, cmd = wiz.helper_zip(PKG, wiz.deck_uid(cfgp))
            return self._json({"file": name, "command": cmd, "uid": wiz.deck_uid(cfgp)})
        return self._send(404, "not found\n", "text/plain")

    def _setup_post(self, path, body):
        ses = Handler.wizard
        cfgp = Handler.cfg["BRIDGE_INI"]
        certs = os.path.expanduser(Handler.cfg.get("SETUP_CERTS_DIR") or "~/certs")
        try:
            if path == "/api/setup/unlock":
                if "package" not in ses:
                    raise wiz.WizardError("choose the data package first")
                return self._json(self._unlock(str(body.get("password") or "")))
            if path == "/api/setup/test":
                if "pems" not in ses:
                    raise wiz.WizardError("choose the data package first")
                ses["test"] = wiz.test_server(str(body.get("host") or "").strip(), int(body.get("port") or 0), ses["pems"], wiz.deck_uid(cfgp))
                return self._json(dict(ses["test"], ok=ses["test"]["ok"]))
            if path == "/api/setup/plan":
                return self._json({"ok": True, "changes": wiz.plan(ses, body.get("choices") or {}, certs)})
            if path == "/api/setup/finish":
                out = wiz.finish(ses, body.get("choices") or {}, cfgp, certs)
                Handler.events.add("ok", "Setup saved (backup: %s)" % os.path.basename(out["backup"]), "system")
                auto = False if Handler.mock else wiz.restart_later()
                return self._json({"ok": True, "backup": out["backup"], "restart": "auto" if auto else "manual"})
        except wiz.WizardError as exc:
            return self._json({"ok": False, "error": str(exc)}, 400)
        except (ValueError, TypeError, KeyError) as exc:
            return self._json({"ok": False, "error": "bad request (%s)" % exc}, 400)
        return self._json({"ok": False, "error": "not found"}, 404)

    def _unlock(self, password):
        ses = Handler.wizard
        try:
            pems = wiz.unlock(ses["package"], password)
        except wiz.WizardError as exc:
            return {"ok": False, "need_password": True, "error": str(exc)}
        ses["pems"] = {k: pems[k] for k in ("cert", "key", "ca")}
        pref = ses["package"]["prefs"]
        conn = wiz.parse_connect(pref.get("connect", ""))
        ses["summary"] = {"file": ses.get("file", ""), "client": ses["package"]["client"][0],
                          "ca": ses["package"]["ca"][0] if ses["package"]["ca"] else "",
                          "name": pems["info"].get("name", ""), "until": pems["info"].get("until", ""),
                          "host": conn[0] if conn else "", "port": conn[1] if conn else 8089,
                          "description": pref.get("description", ""), "password_from_package": pems["used_package_password"]}
        return dict(ses["summary"], ok=True)

    def _setup_package(self):
        """POST /api/setup/package: the raw .zip in the body."""
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > wiz.MAX_PACKAGE:
            return self._json({"ok": False, "error": "that file is empty or too big to be a data package"}, 400)
        data = self.rfile.read(n)
        try:
            Handler.wizard.clear()
            Handler.wizard["package"] = wiz.read_package(data)
            Handler.wizard["file"] = os.path.basename(self.headers.get("X-File-Name") or "")[:120]
        except wiz.WizardError as exc:
            return self._json({"ok": False, "error": str(exc)}, 400)
        return self._json(self._unlock(""))

    def do_POST(self):
        u = urlparse(self.path)
        if not self._is_local():
            return self._json({"ok": False, "error": "controls only work from the deck's own screen"}, 403)
        if u.path == "/api/setup/package":
            if self.headers.get("X-Deck") != "1" or not Handler.control.enabled:
                return self._json({"ok": False, "error": "bad request"}, 400)
            return self._setup_package()
        if self.headers.get("X-Deck") != "1" or "json" not in (self.headers.get("Content-Type") or ""):
            return self._json({"ok": False, "error": "bad request"}, 400)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 16384:
                raise ValueError("too large")
            body = json.loads(self.rfile.read(n).decode() or "{}")
            if not isinstance(body, dict):
                raise ValueError("bad body")
        except Exception:
            return self._json({"ok": False, "error": "bad request"}, 400)
        ctl = Handler.control
        if not ctl.enabled:
            return self._json({"ok": False, "error": "controls are turned off (CONTROL = off)"}, 403)
        mock = bool(Handler.mock)
        if u.path.startswith("/api/setup/"):
            return self._setup_post(u.path, body)
        if u.path == "/api/action":
            if body.get("action") != "restart_bridge":
                return self._json({"ok": False, "error": "unknown action"}, 400)
            if mock:
                return self._json({"ok": True, "message": "demo mode: nothing was restarted"})
            ok, msg = ctl.restart_bridge()
            return self._json({"ok": ok, "message" if ok else "error": msg}, 200 if ok else 409)
        if u.path == "/api/snapshot":
            if mock:
                return self._json({"ok": True, "message": "demo mode: no snapshot taken", "file": ""})
            ok, msg = ctl.snapshot(str(body.get("path") or ""))
            return self._json({"ok": ok, "file" if ok else "error": msg}, 200 if ok else 409)
        self._json({"ok": False, "error": "not found"}, 404)

    def _file(self, name, ctype):
        try:
            with open(os.path.join(HERE, name), "rb") as f:
                self._send(200, f.read(), ctype)
        except OSError:
            self._send(404, "missing %s\n" % name, "text/plain")


def serve_on(addr, port, retry=True):
    """Bind in a thread and keep retrying: the mesh-side address may not exist yet at boot."""
    def go():
        while True:
            try:
                httpd = ThreadingHTTPServer((addr, port), Handler)
            except OSError as exc:
                if not retry:
                    sys.stderr.write("cannot bind %s:%d: %s\n" % (addr, port, exc))
                    return
                time.sleep(5)
                continue
            sys.stderr.write("dashboard listening on http://%s:%d\n" % (addr, port))
            httpd.serve_forever()
            return
    th = threading.Thread(target=go, daemon=True)
    th.start()
    return th


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", help="path to deck.ini")
    ap.add_argument("--once", action="store_true", help="print one status snapshot and exit")
    ap.add_argument("--mock", nargs="?", const="auto", help="serve fake data: auto, " + ", ".join(SCENARIOS))
    ap.add_argument("--port", type=int)
    ap.add_argument("--listen", help="override LISTEN, e.g. 127.0.0.1")
    a = ap.parse_args()
    cfg = load_config(a.config)
    if a.port:
        cfg["PORT"] = str(a.port)
    if a.listen:
        cfg["LISTEN"] = a.listen
    events = Events(cfg["EVENT_FILE"] if not a.mock else os.devnull)
    Handler.events = events
    Handler.cfg = cfg
    history = History(cfg["HISTORY_FILE"] if not a.mock else os.devnull)
    alerts = Alerts(cfg, events)
    control = Control(cfg, events)
    Handler.history, Handler.alerts, Handler.control = history, alerts, control
    if a.once:
        c = Collector(cfg, events, history, alerts, control)
        c.collect()
        time.sleep(0.2)
        print(json.dumps(c.collect(), indent=2))
        return
    port = int(cfg["PORT"])
    if a.mock:
        Handler.mock = a.mock
    else:
        col = Collector(cfg, events, history, alerts, control)
        Handler.collector = col
        col.start()
    listen = cfg["LISTEN"]
    if listen == "auto":
        addrs = ["127.0.0.1"]
        ip = iface_ip(cfg["MESH_IFACE"])
        if ip:
            addrs.append(ip)
        elif not a.mock:
            sys.stderr.write("note: %s has no address yet; will keep checking\n" % cfg["MESH_IFACE"])
    else:
        addrs = [x.strip() for x in listen.split(",") if x.strip()]
    for ad in addrs:
        serve_on(ad, port)
    if listen == "auto" and not iface_ip(cfg["MESH_IFACE"]) and not a.mock:
        # watch for the mesh-side address to appear later (slow DHCP at boot)
        def late():
            while not iface_ip(cfg["MESH_IFACE"]):
                time.sleep(5)
            serve_on(iface_ip(cfg["MESH_IFACE"]), port)
        threading.Thread(target=late, daemon=True).start()
    sys.stderr.write("config: %s\n" % cfg["_config_file"])
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
