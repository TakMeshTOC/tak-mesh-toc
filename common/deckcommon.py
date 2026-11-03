"""deckcommon: code shared by the bridge, Chat and Ops.

GPS (gpsd) and MGRS, the ATAK team icon, the shared [deck] settings, the bundled fonts and the suite version.
Standard library only.
"""
import json as _json
import math as _math
import os as _os
import re as _re
import socket as _socket
import struct as _struct
import threading as _threading
import time as _time
from xml.sax.saxutils import quoteattr as _quoteattr

_HERE = _os.path.dirname(_os.path.abspath(__file__))


def suite_version():
    """The suite's version from the VERSION file next to common/ ('?' if missing)."""
    try:
        with open(_os.path.join(_HERE, "..", "VERSION")) as f:
            return f.read().split()[0].strip() or "?"
    except (OSError, IndexError):
        return "?"


# ---- the shared [deck] section ----------------------------------------------------------------
# Settings that MUST be the same in more than one program live once, in [deck]. Each program gets
# them under its own old name. A different value in a program's own section is ignored (and
# reported), so the deck can never show up as two contacts or with two symbols again.
#   [deck] key   -> bridge key                  chat key           dashboard key
DECK_KEYS = {
    "CALLSIGN":  {"bridge": "BRIDGE_CALLSIGN", "chat": "CALLSIGN"},
    "UID":       {"bridge": "BRIDGE_UID", "chat": "UID"},
    "MAP_TYPE":  {"bridge": "BRIDGE_TYPE", "chat": "SA_TYPE"},
    "TEAM":      {"bridge": "BRIDGE_TEAM", "chat": "SA_TEAM"},
    "ROLE":      {"bridge": "BRIDGE_ROLE", "chat": "SA_ROLE"},
    "MESH_ADDR": {"bridge": "PYTAK_MULTICAST_LOCAL_ADDR", "chat": "MESH_LOCAL_ADDR", "voice": "MESH_ADDR"},
    "GPS":       {"bridge": "GPS", "chat": "GPS", "dashboard": "GPS"},
    "GPS_HOST":  {"bridge": "GPS_HOST", "chat": "GPS_HOST", "dashboard": "GPS_HOST"},
}


def _find(cp, section, key):
    """The option name in section matching key (any case), or None."""
    if not cp.has_section(section):
        return None
    for o in cp.options(section):
        if o.upper() == key.upper():
            return o
    return None


def apply_deck(cp, section, program, defaults=None):
    """Copy [deck] values into `section` of ConfigParser `cp` under `program`'s key names.
    Returns a list of notes about values set in `section` that [deck] overrode (values equal to the
    program's built-in `defaults` were never set by the user, so they are replaced silently).
    No [deck] = no change."""
    notes = []
    if not cp.has_section("deck"):
        return notes
    if not cp.has_section(section):
        cp.add_section(section)
    for dkey, names in DECK_KEYS.items():
        target = names.get(program)
        src = _find(cp, "deck", dkey)
        if not target or src is None:
            continue
        val = cp.get("deck", src, raw=True).strip()
        if dkey == "MESH_ADDR" and program == "bridge" and val == "":
            continue                      # blank [deck] MESH_ADDR: keep [bridge]'s own (auto reaches the bridge too)
        own = _find(cp, section, target)
        if own is not None:
            old = cp.get(section, own, raw=True).strip()
            if old != val and old != str((defaults or {}).get(target, "\0")).strip():
                notes.append("[%s] %s = %s ignored: [deck] %s = %s is used" % (section, target, old, dkey, val))
            cp.remove_option(section, own)
        cp.set(section, target, val)
    return notes


# ---- the deck's address on the mesh -----------------------------------------------------------
# MESH_ADDR = auto: use whatever address the mesh port (MESH_IFACE, usually eth0) has right now, and follow it when
# the node hands out a new one (a node reboot can do that). The bridge and Voice resolve it at start and restart
# themselves when it changes; Chat looks it up live.
def iface_ipv4(iface):
    """IPv4 address of a network interface, or None (down, unplugged or no address). No external tools."""
    import fcntl
    s = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
    try:
        res = fcntl.ioctl(s.fileno(), 0x8915, _struct.pack("256s", iface[:15].encode()))   # SIOCGIFADDR
        return _socket.inet_ntoa(res[20:24])
    except OSError:
        return None
    finally:
        s.close()


def mesh_iface(cp, section):
    """The mesh port's name: MESH_IFACE in `section`, else [deck], [chat] or [dashboard], else eth0."""
    for sec in (section, "deck", "chat", "dashboard"):
        o = _find(cp, sec, "MESH_IFACE")
        if o is not None and cp.get(sec, o, raw=True).strip():
            return cp.get(sec, o, raw=True).strip()
    return "eth0"


def is_auto(value):
    return (value or "").strip().lower() == "auto"


def watch_mesh_addr(iface, current, on_change, every=5.0):
    """Background thread: call on_change(old, new) once when `iface` has an address different from `current`
    (None = it had none). An address that goes away (node unplugged) is not a change: the old one may come back."""
    def loop():
        while True:
            _time.sleep(every)
            now = iface_ipv4(iface)
            if now and now != current:
                on_change(current, now)
                return
    t = _threading.Thread(target=loop, name="mesh-addr-watch", daemon=True)
    t.start()
    return t


# ---- page look -----------------------------------------------------------------------------------
# There is one look (pro). An old THEME line in [deck] does nothing; `check` says it can be deleted.
def theme_note(value):
    """A note for a leftover THEME setting, else None."""
    if (value or "").strip():
        return "THEME = %s is no longer used: there is one look now (pro)" % value.strip()
    return None


FONT_DIR = _os.path.join(_HERE, "fonts")
_FONTS = frozenset(n for n in (_os.listdir(FONT_DIR) if _os.path.isdir(FONT_DIR) else ()) if n.endswith(".woff2"))


def font_file(name):
    """The full path of a bundled font for the pro look, or None. Only names in common/fonts/ (no paths)."""
    return _os.path.join(FONT_DIR, name) if name in _FONTS else None


# ---- ATAK team icon ----------------------------------------------------------------------------
TEAMS = ("White", "Yellow", "Orange", "Magenta", "Red", "Maroon", "Purple", "Dark Blue", "Blue", "Cyan",
         "Teal", "Green", "Dark Green", "Brown")
ROLES = ("Team Member", "Team Lead", "HQ", "Sniper", "Medic", "Forward Observer", "RTO", "K9")


def team_xml(team, role):
    """ATAK team color + role: phones draw a team icon (like their own) instead of a military symbol.
    Both blank = no team (military symbol from the CoT type)."""
    team, role = (team or "").strip(), (role or "").strip()
    if not team and not role:
        return ""
    team = next((t for t in TEAMS if t.lower() == team.lower()), "Cyan")
    role = next((r for r in ROLES if r.lower() == role.lower()), "Team Member")
    return '<__group name=%s role=%s/>' % (_quoteattr(team), _quoteattr(role))


# ---- GPS (gpsd) + MGRS -------------------------------------------------------------------------

_A = 6378137.0                     # WGS84
_F = 1 / 298.257223563
_E2 = _F * (2 - _F)
_EP2 = _E2 / (1 - _E2)
_K0 = 0.9996
_BANDS = "CDEFGHJKLMNPQRSTUVWX"    # 8 degree latitude bands from 80S (X = 72-84N)
_COLS = ("ABCDEFGH", "JKLMNPQR", "STUVWXYZ")
_ROWS = "ABCDEFGHJKLMNPQRSTUV"


def _utm_zone(lat, lon):
    z = int((lon + 180) // 6) + 1
    if z > 60:
        z = 60
    if 56 <= lat < 64 and 3 <= lon < 12:
        z = 32                                          # Norway
    if 72 <= lat < 84:                                  # Svalbard
        if 0 <= lon < 9:
            z = 31
        elif 9 <= lon < 21:
            z = 33
        elif 21 <= lon < 33:
            z = 35
        elif 33 <= lon < 42:
            z = 37
    return z


def latlon_to_utm(lat, lon):
    """(zone, easting m, northing m) on WGS84."""
    z = _utm_zone(lat, lon)
    lon0 = _math.radians((z - 1) * 6 - 180 + 3)
    phi, lam = _math.radians(lat), _math.radians(lon)
    n = _A / _math.sqrt(1 - _E2 * _math.sin(phi) ** 2)
    t = _math.tan(phi) ** 2
    c = _EP2 * _math.cos(phi) ** 2
    a = _math.cos(phi) * (lam - lon0)
    e4, e6 = _E2 * _E2, _E2 * _E2 * _E2
    m = _A * ((1 - _E2 / 4 - 3 * e4 / 64 - 5 * e6 / 256) * phi
              - (3 * _E2 / 8 + 3 * e4 / 32 + 45 * e6 / 1024) * _math.sin(2 * phi)
              + (15 * e4 / 256 + 45 * e6 / 1024) * _math.sin(4 * phi)
              - (35 * e6 / 3072) * _math.sin(6 * phi))
    east = _K0 * n * (a + (1 - t + c) * a ** 3 / 6 + (5 - 18 * t + t * t + 72 * c - 58 * _EP2) * a ** 5 / 120) + 500000.0
    north = _K0 * (m + n * _math.tan(phi) * (a * a / 2 + (5 - t + 9 * c + 4 * c * c) * a ** 4 / 24
                                             + (61 - 58 * t + t * t + 600 * c - 330 * _EP2) * a ** 6 / 720))
    if lat < 0:
        north += 10000000.0
    return z, east, north


def to_mgrs(lat, lon, digits=5):
    """MGRS grid like '13S DD 12345 67890' (digits per axis: 1-5; truncated, the MGRS convention).
    None outside 80S-84N (polar areas use UPS, not supported here)."""
    if lat is None or lon is None or not (-80 <= lat < 84) or not (-180 <= lon <= 180):
        return None
    z, e, n = latlon_to_utm(lat, lon)
    band = _BANDS[min(int((lat + 80) // 8), len(_BANDS) - 1)]
    col = _COLS[(z - 1) % 3][int(e // 100000) - 1]
    row = _ROWS[(int(n // 100000) + (5 if z % 2 == 0 else 0)) % 20]
    div = 10 ** (5 - digits)
    ee = int(e % 100000) // div
    nn = int(n % 100000) // div
    return "%d%s %s%s %0*d %0*d" % (z, band, col, row, digits, ee, digits, nn)


class GpsWatch(_threading.Thread):
    """Follows gpsd (JSON on 127.0.0.1:2947). snapshot() never blocks and never raises."""

    def __init__(self, host="127.0.0.1", port=2947):
        super().__init__(daemon=True)
        self.host, self.port = host, int(port)
        self.lock = _threading.Lock()
        self.s = {"gpsd": False, "devices": None, "mode": 0, "lat": None, "lon": None, "alt": None,
                  "eph": None, "used": None, "seen": None, "fix_ts": None, "last": None}

    def run(self):
        while True:
            try:
                with _socket.create_connection((self.host, self.port), timeout=5) as sock:
                    sock.settimeout(10)
                    sock.sendall(b'?WATCH={"enable":true,"json":true}\n')
                    f = sock.makefile("r", encoding="utf-8", errors="replace")
                    with self.lock:
                        self.s["gpsd"] = True
                    for line in f:
                        try:
                            self._msg(_json.loads(line))
                        except Exception:     # one bad message is skipped; it can never stop the reader
                            continue
            except Exception:                 # gpsd gone or the link broke: reconnect in 5 s
                pass
            with self.lock:
                self.s.update({"gpsd": False, "mode": 0})
            _time.sleep(5)

    @staticmethod
    def _num(v, lo=None, hi=None):
        """A finite number (optionally within lo..hi), else None. Rejects lists, text, NaN, inf, True/False."""
        if isinstance(v, bool) or not isinstance(v, (int, float, str)):
            return None
        try:
            x = float(v)
        except ValueError:
            return None
        if not _math.isfinite(x) or (lo is not None and x < lo) or (hi is not None and x > hi):
            return None
        return x

    def _msg(self, m):
        """Apply one gpsd JSON message. Anything malformed is ignored as a whole, never half-applied."""
        if not isinstance(m, dict):
            return
        cls = m.get("class")
        num = self._num
        if cls == "DEVICES":
            devs = m.get("devices")
            n = len(devs) if isinstance(devs, list) else 0
            with self.lock:
                self.s["devices"] = n
        elif cls == "DEVICE":
            with self.lock:
                s = self.s
                if m.get("activated") == 0 or m.get("activated") == "":
                    s["devices"] = max(0, (s["devices"] or 1) - 1)
                elif s["devices"] in (None, 0):
                    s["devices"] = 1
        elif cls == "SKY":
            # some receivers send SKY reports with only DOPs and no satellite list: keep the last counts
            sats = m.get("satellites")
            sats = [x for x in sats if isinstance(x, dict)] if isinstance(sats, list) else []
            used = num(m["uSat"], 0, 1000) if "uSat" in m else (sum(1 for x in sats if x.get("used")) if sats else None)
            seen = num(m["nSat"], 0, 1000) if "nSat" in m else (len(sats) if sats else None)
            with self.lock:
                if used is not None:
                    self.s["used"] = int(used)
                if seen is not None:
                    self.s["seen"] = int(seen)
        elif cls == "TPV":
            mode = num(m.get("mode") or 0, 0, 3)
            if mode is None:
                return
            mode = int(mode)
            upd = {"mode": mode}
            lat, lon = num(m.get("lat"), -90, 90), num(m.get("lon"), -180, 180)
            if mode >= 2 and lat is not None and lon is not None:
                alt = num(m.get("altMSL", m.get("alt"))) if mode == 3 else None
                eph = num(m.get("eph"), 0)
                if eph is None:
                    ex, ey = num(m.get("epx"), 0), num(m.get("epy"), 0)
                    if ex is not None and ey is not None:
                        eph = max(ex, ey)
                now = _time.time()
                upd.update({"lat": lat, "lon": lon, "alt": alt, "eph": eph, "fix_ts": now,
                            "last": {"lat": lat, "lon": lon, "alt": alt, "eph": eph, "ts": now, "mode": mode}})
            with self.lock:
                self.s.update(upd)

    def snapshot(self):
        with self.lock:
            out = dict(self.s)
        out["fix_age"] = (_time.time() - out["fix_ts"]) if out.get("fix_ts") else None
        return out

    def position(self, max_age=10.0):
        """(live, pos): live=True and the current fix if fresh, else (False, last known fix or None)."""
        s = self.snapshot()
        last = s.get("last")
        if s["mode"] >= 2 and s["fix_age"] is not None and s["fix_age"] <= max_age and last:
            return True, last
        return False, last
