#!/usr/bin/env python3
"""meshtoc: one command for TAK MESH TOC. (Its old name, manet_mesh_bridge, still works.)

  meshtoc status            version, the services, bridge traffic, voice, GPS
  meshtoc check             full health check: files intact, services, config, certificates, links
                            between the programs, server link, DM route, clock, watchdog, voice
  meshtoc version           the installed version
  meshtoc restart           restart everything (bridge, chat, dashboard, voice)
  meshtoc start | stop      start / stop everything
  meshtoc logs [bridge|chat|dashboard|voice] [-n 50]
  meshtoc rollback          go back to the previous installed version (and forward again)
  meshtoc tidy [--dry-run] [--yes]
                            remove old releases (keeps the current one and the one before it, for
                            rollback), old unzipped package folders and old package zips in Downloads.
                            Lists everything and asks first. Never touches deck.ini or logs.
"""
import configparser
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time

REAL = os.path.realpath(__file__)
RELEASE = os.path.dirname(os.path.dirname(REAL))            # .../releases/<version>
sys.path.insert(0, os.path.join(RELEASE, "common"))
from deckcommon import apply_deck, suite_version, theme_note, mesh_iface, iface_ipv4  # noqa: E402
from meshwatch import eui64_lladdr  # noqa: E402

NAME = "TAK MESH TOC"
HOME = os.path.expanduser("~")
BASE = os.environ.get("MANET_MESH_BRIDGE_HOME", os.path.join(HOME, "manet_mesh_bridge"))
CONFIG = os.path.join(BASE, "deck.ini")
UNITS = {"bridge": "manet-bridge", "chat": "manet-chat", "dashboard": "manet-dashboard", "voice": "manet-voice"}
TARGET = "manet_mesh_bridge.target"
SYSTEMCTL = os.environ.get("MANET_MESH_BRIDGE_SYSTEMCTL", "systemctl")
PYTAK_TESTED = "7.6."

GREEN, AMBER, RED, DIM, END = ("\033[32m", "\033[33m", "\033[31m", "\033[2m", "\033[0m") if sys.stdout.isatty() else ("",) * 5
results = []


def line(level, what, detail=""):
    tag = {"ok": GREEN + " OK " + END, "warn": AMBER + "WARN" + END, "fail": RED + "FAIL" + END, "info": DIM + "info" + END,
           "todo": AMBER + "TODO" + END}[level]
    print(" [%s] %-26s %s" % (tag, what, detail))
    results.append(level)


def sh(*cmd):
    try:
        r = subprocess.run(list(cmd), capture_output=True, text=True, timeout=15)
        return r.returncode, (r.stdout or "").strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)


def unit_prop(unit, prop):
    return sh(SYSTEMCTL, "show", unit, "-p", prop, "--value")[1]


def load_cfg():
    cp = configparser.ConfigParser(interpolation=None, strict=False)   # same rules as the programs
    cp.optionxform = str
    if not cp.read(CONFIG, encoding="utf-8-sig"):
        return None
    return cp


def config_duplicates(path):
    """Settings written more than once in the same section: [(section, key, [line numbers])]. The programs use the
    last one; `check` says so, so a double paste never goes unnoticed."""
    seen, section = {}, None
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as f:
            for n, raw in enumerate(f, 1):
                s = raw.strip()
                if not s or s[0] in "#;":
                    continue
                if s.startswith("[") and s.endswith("]"):
                    section = s[1:-1].strip()
                    continue
                if section and ("=" in s or ":" in s):
                    key = s.split("=" if "=" in s else ":", 1)[0].strip().upper()
                    seen.setdefault((section, key), []).append(n)
    except OSError:
        return []
    return [(sec, key, lines) for (sec, key), lines in seen.items() if len(lines) > 1]


def sec(cp, name, key, default=""):
    if not cp.has_section(name):
        return default
    for o in cp.options(name):
        if o.upper() == key.upper():
            return cp.get(name, o).strip()
    return default


def port_of(v):
    m = re.search(r":(\d+)\s*$", v or "")
    return int(m.group(1)) if m else None


def bridge_status():
    cp = load_cfg()
    p = os.path.expanduser(sec(cp, "bridge", "STATUS_FILE", "~/bridge_status.json") if cp else "~/bridge_status.json")
    try:
        with open(p) as f:
            st = json.load(f)
        return st, time.time() - os.path.getmtime(p)
    except (OSError, ValueError):
        return None, None


def vendored_version(dist):
    """Version of a library bundled in this release (vendor/<dist>-<version>.dist-info), or None."""
    vd = os.path.join(RELEASE, "vendor")
    try:
        for n in os.listdir(vd):
            m = re.match(r"^%s-([0-9][^-]*)\.dist-info$" % re.escape(dist), n, re.I)
            if m:
                return m.group(1)
    except OSError:
        pass
    return None


def gpsd_report(host):
    h, _, p = (host or "127.0.0.1:2947").partition(":")
    try:
        with socket.create_connection((h or "127.0.0.1", int(p or 2947)), timeout=2) as s:
            s.settimeout(3)
            s.sendall(b'?WATCH={"enable":true,"json":true}\n')
            f = s.makefile("r")
            end = time.time() + 3
            tpv = None
            while time.time() < end:
                ln = f.readline()
                if not ln:
                    break
                try:
                    m = json.loads(ln)
                except ValueError:
                    continue
                if m.get("class") == "TPV":
                    tpv = m
                    break
            return True, tpv
    except OSError:
        return False, None


# ------------------------------------------------------------------------------------------ check
def check_files():
    man = os.path.join(RELEASE, "MANIFEST.sha256")
    if not os.path.exists(man):
        line("fail", "Program files", "MANIFEST.sha256 missing in " + RELEASE)
        return
    bad, n = [], 0
    for ln in open(man):
        h, _, rel = ln.strip().partition("  ")
        if not rel:
            continue
        n += 1
        try:
            with open(os.path.join(RELEASE, rel), "rb") as f:
                if hashlib.sha256(f.read()).hexdigest() != h:
                    bad.append(rel)
        except OSError:
            bad.append(rel + " (missing)")
    if bad:
        line("fail", "Program files", "%d of %d changed: %s. Reinstall with install.sh --force" % (len(bad), n, ", ".join(bad[:4])))
    else:
        line("ok", "Program files", "%d files match version %s (nothing overwrote them)" % (n, suite_version()))
    pv = vendored_version("pytak")
    if pv is None:
        line("fail", "PyTAK", "not bundled in this release (vendor/ missing): reinstall")
    elif not pv.startswith(PYTAK_TESTED):
        line("warn", "PyTAK", "%s bundled; this release is tested with %sx" % (pv, PYTAK_TESTED))
    else:
        tp = vendored_version("takproto")
        line("ok", "Bundled libraries", "PyTAK %s, takproto %s, protobuf %s" % (pv, tp or "-", vendored_version("protobuf") or "-"))


EXAMPLE_HOST = "tak.example.net"           # the placeholder COT_URL in deck.ini.example


def server_unset(cp):
    """Why the TAK server isn't set up yet (a fresh install), or None when it is.
    One missing setting would otherwise show as three failures: no certificate, bridge stopped, no status."""
    if cp is None:
        return None
    cot = sec(cp, "bridge", "COT_URL")
    if not cot or EXAMPLE_HOST in cot:
        return "COT_URL is still the example"
    if cot.lower().startswith("tls://") and not sec(cp, "bridge", "PYTAK_TLS_CLIENT_CERT"):
        return "no certificate yet"
    return None


def voice_on(cp):
    return cp is not None and sec(cp, "voice", "VOICE", "off").lower() in ("1", "on", "yes", "true")


def check_units():
    cur = os.path.join(BASE, "current")
    cp = load_cfg()
    for prog, unit in UNITS.items():
        execs = unit_prop(unit, "ExecStart")
        active = unit_prop(unit, "ActiveState")
        enabled = sh(SYSTEMCTL, "is-enabled", unit)[1]
        if not execs:
            line("fail", "Service " + unit, "not installed")
            continue
        if cur not in execs and BASE not in execs:
            line("fail", "Service " + unit, "runs a program outside %s (changed by hand?). Run install.sh again" % BASE)
            continue
        if prog == "voice" and not voice_on(cp):
            line("info", "Service " + unit, "off (VOICE = off in [voice]; see README, 'Voice')")
            continue
        if prog == "bridge" and active != "active" and server_unset(cp):
            line("info", "Service " + unit, "waits for the server settings (see TODO below)")
            continue
        lvl = "ok" if active == "active" else "fail"
        line(lvl, "Service " + unit, "%s, %s at boot" % (active or "?", "starts" if enabled == "enabled" else "does NOT start"))
    lvl = "ok" if sh(SYSTEMCTL, "is-enabled", TARGET)[1] == "enabled" else "warn"
    line(lvl, "Group " + TARGET, "enabled" if lvl == "ok" else "not enabled (sudo systemctl enable " + TARGET + ")")


def check_certs(cp):
    """TLS to the server: the certificate files exist, only you can read the private key, and a .p12 can be read."""
    why = server_unset(cp)
    if why:
        line("todo", "TAK server", "not set up yet (%s). In %s [bridge] set COT_URL and the three PYTAK_TLS_ "
             "certificate lines, then: meshtoc restart  (README, 'Certificates')" % (why, CONFIG))
        return
    cot = sec(cp, "bridge", "COT_URL")
    if not cot.lower().startswith("tls://"):
        line("info", "Server link", "%s (not TLS: no certificate needed)" % (cot or "COT_URL not set"))
        return
    cert, key = sec(cp, "bridge", "PYTAK_TLS_CLIENT_CERT"), sec(cp, "bridge", "PYTAK_TLS_CLIENT_KEY")
    if not cert:
        line("fail", "Certificate", "COT_URL is tls:// but PYTAK_TLS_CLIENT_CERT is not set in [bridge]")
        return
    for what, p in (("Certificate", cert), ("Private key", key)):
        if not p:
            continue
        p = os.path.expanduser(p)
        if not os.path.exists(p):
            line("fail", what, "%s does not exist" % p)
            continue
        mode = os.stat(p).st_mode & 0o777
        secret = what == "Private key" or p.lower().endswith(".p12")
        if secret and mode & 0o077:
            line("warn", what, "%s is readable by other users (mode %o): chmod 600 %s" % (p, mode, p))
        else:
            line("ok", what, "%s (mode %o)" % (p, mode))
    if cert.lower().endswith(".p12"):
        # The bridge uses only its bundled libraries (python3 -S), so it reads PEM files, not .p12. Convert once.
        base = os.path.splitext(os.path.expanduser(cert))[0]
        line("fail", "Certificate (.p12)", "convert it to PEM once:  openssl pkcs12 -legacy -in %s.p12 -clcerts -nokeys -out %s.pem  "
             "and  openssl pkcs12 -legacy -in %s.p12 -nocerts -nodes -out %s.key  then chmod 600 %s.key and set "
             "PYTAK_TLS_CLIENT_CERT = %s.pem  PYTAK_TLS_CLIENT_KEY = %s.key  in [bridge] (see README, 'Certificates')"
             % (base, base, base, base, base, base, base))


def check_config():
    if not os.path.exists(CONFIG):
        line("fail", "Config", CONFIG + " missing")
        return None
    mode = os.stat(CONFIG).st_mode & 0o777
    line("ok" if mode & 0o077 == 0 else "warn", "Config file", "%s (mode %o%s)" % (CONFIG, mode, "" if mode & 0o077 == 0 else ": run chmod 600 on it"))
    cp = load_cfg()
    if cp is None:
        line("fail", "Config", "can't read " + CONFIG)
        return None
    for section, key, lines in config_duplicates(CONFIG):    # not "sec": that name is the helper used below
        line("warn", "Config [%s]" % section, "%s is set %d times (lines %s): the last one is used"
             % (key, len(lines), ", ".join(map(str, lines))))
    if not cp.has_section("deck"):
        line("warn", "Config [deck]", "missing: callsign/UID/symbol are set separately per program")
    for name in ("bridge", "chat", "dashboard", "voice"):
        probe = configparser.ConfigParser(interpolation=None, strict=False)
        probe.optionxform = str
        probe.read(CONFIG, encoding="utf-8-sig")
        notes = apply_deck(probe, name, name)
        for n in notes:
            line("warn", "Config [%s]" % name, n + ". Delete that line")
    cs, uid = sec(cp, "deck", "CALLSIGN", "DECK"), sec(cp, "deck", "UID") or "DECK-" + re.sub(r"[^A-Za-z0-9-]", "", socket.gethostname())
    line("ok", "Deck identity", "callsign %s, UID %s, symbol %s%s" % (
        cs, uid, sec(cp, "deck", "MAP_TYPE", "a-f-G-U-C"),
        (", team %s/%s" % (sec(cp, "deck", "TEAM"), sec(cp, "deck", "ROLE") or "Team Member")) if sec(cp, "deck", "TEAM") or sec(cp, "deck", "ROLE") else ""))
    if not (sec(cp, "deck", "TEAM") or sec(cp, "deck", "ROLE")):
        line("warn", "Deck team", "none: iTAK shows the deck as a marker, not a person to message. Set TEAM and ROLE in [deck] "
             "(e.g. TEAM = Cyan, ROLE = HQ), then meshtoc restart")
    if theme_note(sec(cp, "deck", "THEME")):
        line("info", "Look", theme_note(sec(cp, "deck", "THEME")) + ". You can delete that line from [deck]")
    mesh = sec(cp, "deck", "MESH_ADDR") or sec(cp, "bridge", "PYTAK_MULTICAST_LOCAL_ADDR")
    if (mesh or "").strip().lower() == "auto":
        iface = sec(cp, "deck", "MESH_IFACE") or sec(cp, "chat", "MESH_IFACE") or sec(cp, "dashboard", "MESH_IFACE") or "eth0"
        rc, out = sh("ip", "-4", "-o", "addr", "show", "dev", iface)
        m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)/", out or "") if rc == 0 else None
        if m:
            line("ok", "Mesh address", "auto: %s on %s (followed if the node hands out a new one)" % (m.group(1), iface))
        else:
            line("warn", "Mesh address", "auto: %s has no address right now (node unplugged? server-only until it is back)" % iface)
    elif not re.match(r"^\d+\.\d+\.\d+\.\d+$", mesh or ""):
        line("fail", "Mesh address", "MESH_ADDR in [deck] must be auto or this deck's IP on the mesh port (now %r)" % mesh)
    else:
        rc, out = sh("ip", "-4", "-o", "addr", "show")
        if rc == 0 and out and (mesh + "/") not in out:
            line("warn", "Mesh address", "%s is not on any interface right now (node unplugged, or the node gave the deck "
                 "a new address? MESH_ADDR = auto in [deck] follows it)" % mesh)
        else:
            line("ok", "Mesh address", mesh)
    check_certs(cp)
    pairs = (("chat copies", sec(cp, "bridge", "LOCAL_CHAT_COPY"), sec(cp, "chat", "BRIDGE_COPY_LISTEN", "127.0.0.1:17013")),
             ("DMs to the server", sec(cp, "bridge", "LOCAL_SEND"), sec(cp, "chat", "LOCAL_SEND", "127.0.0.1:17014")))
    for what, b, c in pairs:
        if not b or b.lower() in ("off", "0"):
            line("warn", "Bridge <-> chat " + what, "off in [bridge]: Chat won't get server traffic")
        elif port_of(b) != port_of(c):
            line("fail", "Bridge <-> chat " + what, "ports differ: [bridge] %s vs [chat] %s" % (b, c))
        else:
            line("ok", "Bridge <-> chat " + what, "port %s" % port_of(b))
    bi = os.path.expanduser(sec(cp, "dashboard", "BRIDGE_INI", CONFIG))
    if os.path.realpath(bi) != os.path.realpath(CONFIG):
        line("warn", "Dashboard -> config", "BRIDGE_INI points at %s, not deck.ini" % bi)
    dm = sec(cp, "bridge", "DM_RELAY", "0")
    line("ok" if dm == "1" else "info", "Mesh <-> server DMs", "relay on (port %s)" % sec(cp, "bridge", "DM_RELAY_PORT", "4243") if dm == "1" else "relay off (DM_RELAY)")
    return cp


def check_system():
    rc, out = sh("timedatectl", "show", "-p", "NTPSynchronized", "--value")
    if rc == 0 and out in ("yes", "no"):
        line("ok" if out == "yes" else "warn", "Clock", "synced" if out == "yes" else
             "NOT synced: TAK hides reports with a wrong time (internet, or chrony with GPS)")
    rc, out = sh(SYSTEMCTL, "show", "-p", "RuntimeWatchdogUSec", "--value")
    if rc == 0 and out:
        on = out not in ("0", "infinity", "")
        line("ok" if on else "info", "Hardware watchdog", ("on (%s): a frozen deck reboots itself" % out) if on else
             "off (optional: see README, 'Watchdog')")


def check_runtime(cp):
    st, age = bridge_status()
    if st is None and server_unset(cp):
        line("info", "Bridge status", "starts once the TAK server is set up")
    elif st is None:
        line("fail", "Bridge status", "no status file: bridge not running? (meshtoc logs bridge)")
    elif age > 10:
        line("fail", "Bridge status", "last written %.0f s ago: bridge stuck or stopped (meshtoc logs bridge)" % age)
    else:
        v = st.get("suite_version")
        if v != suite_version():
            line("fail", "Bridge version", "running bridge says %s, installed is %s: meshtoc restart" % (v or "?", suite_version()))
        else:
            line("ok", "Bridge running", "version %s, pid %s" % (v, st.get("pid")))
        lk = st.get("link") or {}
        if lk.get("state") == "down":
            line("warn", "Server link", "reconnecting: %s" % (lk.get("reason") or "?"))
        elif lk.get("rtt_ms") is not None:
            line("ok", "Server link", "the server answers pings (%s ms)" % lk.get("rtt_ms"))
        srv = st.get("server") or {}
        mesh = st.get("mesh") or {}
        line("info", "Bridge traffic", "mesh events %s, server events %s" % (mesh.get("events", "?"), srv.get("rx_events", "?")))
        dr = st.get("dm_route") or {}
        if dr.get("state") == "ok":
            line("ok", "DM route", "mesh_mailroute on the server answers (%s ms)" % dr.get("rtt_ms"))
        elif dr.get("state") == "broken":
            line("warn", "DM route", "BROKEN: server -> mesh DMs are not delivered. Is mesh_mailroute running on the server?")
        elif dr.get("state") == "checking":
            line("info", "DM route", "checking (first answer takes up to a minute)")
        ob = st.get("outbox") or {}
        if ob.get("enabled") and (ob.get("waiting") or 0) > 0:
            line("warn", "Chats waiting", "%s chat(s) held for the server (sent when the link is back)" % ob.get("waiting"))
    if cp is not None and sec(cp, "deck", "GPS", "off").lower() == "gpsd":
        ok, tpv = gpsd_report(sec(cp, "deck", "GPS_HOST", "127.0.0.1:2947"))
        if not ok:
            line("warn", "GPS (gpsd)", "gpsd not answering (sudo systemctl status gpsd)")
        elif not tpv or int(tpv.get("mode") or 0) < 2:
            line("warn", "GPS (gpsd)", "gpsd up, no fix yet")
        else:
            line("ok", "GPS (gpsd)", "%sD fix" % tpv.get("mode"))


def mumble_fingerprint(hostport):
    """SHA-256 of the Mumble server's certificate (what MUMBLE_CERT_SHA256 pins), or (None, error)."""
    import ssl
    h, _, p = (hostport or "").strip().rpartition(":")
    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
        with ctx.wrap_socket(socket.create_connection((h.strip("[]"), int(p)), timeout=4)) as t:
            return hashlib.sha256(t.getpeercert(True)).hexdigest(), None
    except (OSError, ValueError) as exc:
        return None, str(exc) or exc.__class__.__name__


def voice_status_file(cp):
    p = os.path.expanduser(sec(cp, "voice", "STATUS_FILE", "~/voice_status.json") if cp else "~/voice_status.json")
    try:
        with open(p) as f:
            return json.load(f), time.time() - os.path.getmtime(p)
    except (OSError, ValueError):
        return None, None


def check_voice(cp):
    if not voice_on(cp):
        return
    sys.path.insert(0, os.path.join(RELEASE, "voice"))
    try:
        import opuscodec
        err = opuscodec.LOAD_ERROR
    except Exception as exc:                      # pragma: no cover
        err = str(exc)
    line("fail" if err else "ok", "Voice: libopus", err or "found")
    mum = sec(cp, "voice", "MUMBLE")
    if not re.search(r":\d+$", mum):
        line("fail", "Voice: Mumble server", "set MUMBLE = <server Tailscale address>:64738 in [voice]")
    else:
        fp, e = mumble_fingerprint(mum)
        pin = sec(cp, "voice", "MUMBLE_CERT_SHA256").replace(":", "").lower()
        if fp is None:
            line("fail", "Voice: Mumble server", "can't reach %s (%s). Tailscale up? mumble-server running? UFW allows 64738 on tailscale0?" % (mum, e))
        elif not pin:
            line("warn", "Voice: Mumble certificate", "not pinned. If this is your server, add to [voice]:  MUMBLE_CERT_SHA256 = %s" % fp)
        elif pin != fp:
            line("fail", "Voice: Mumble certificate", "the server's certificate is %s, deck.ini pins another one. A new server certificate? "
                 "Then update MUMBLE_CERT_SHA256. If not, someone may be in the middle" % fp)
        else:
            line("ok", "Voice: Mumble server", "reachable, certificate matches the pin")
    st, age = voice_status_file(cp)
    viface = mesh_iface(cp, "voice")
    if (st is None or age > 10) and not iface_ipv4(viface):
        line("info", "Voice: gateway", "waiting for the mesh (%s has no address yet; it starts when the node is plugged in)" % viface)
    elif st is None or age > 10:
        line("fail", "Voice: gateway", "not running (meshtoc logs voice)")
    else:
        for g in st.get("groups") or []:
            if not g.get("bridged"):
                continue
            if not g.get("mumble_up"):
                line("fail", "Voice: %s" % g.get("name"), "not connected to Mumble (wrong MUMBLE_PASSWORD? meshtoc logs voice)")
            elif not g.get("in_channel"):
                line("warn", "Voice: %s" % g.get("name"), "connected, but Mumble has no channel %r: make it with voice/make_channel.py %s" % (g.get("channel"), g.get("channel")))
            else:
                line("ok", "Voice: %s" % g.get("name"), "port %s <-> Mumble channel %r, %d on the mesh, %d on Mumble" % (
                    g.get("port"), g.get("channel"), len(g.get("mesh") or []), len(g.get("mumble") or [])))
        p = st.get("deck_ptt") if isinstance(st.get("deck_ptt"), dict) else {}
        if p.get("enabled"):
            if p.get("error"):
                line("warn", "Voice: PTT box", p["error"])
            elif p.get("present"):
                line("ok", "Voice: PTT box", "plugged in (sound card %s), talks on %s" % (p.get("card") or "?", p.get("tg") or "?"))
            else:
                line("info", "Voice: PTT box", "not plugged in (talks on %s when it is)" % (p.get("tg") or "?"))
    if sec(cp, "voice", "DECK_PTT", "auto").lower() not in ("off", "0", "no", "false"):
        import grp
        import pwd
        me = pwd.getpwuid(os.getuid()).pw_name
        try:
            mine = {g.gr_name for g in grp.getgrall() if me in g.gr_mem} | {grp.getgrgid(pwd.getpwnam(me).pw_gid).gr_name}
        except (KeyError, OSError):
            mine = set()
        need = [g for g in ("input", "audio") if g not in mine and any(x.gr_name == g for x in grp.getgrall())]
        if need:
            line("warn", "Voice: PTT box", "%s is not in group %s: run install.sh again (or sudo usermod -aG %s %s), then meshtoc restart"
                 % (me, " + ".join(need), ",".join(need), me))
        elif not (shutil.which("arecord") and shutil.which("aplay")):
            line("warn", "Voice: PTT box", "arecord/aplay not found: sudo apt install alsa-utils")
    rc, out = sh("ufw", "status")
    if rc == 0 and "Status: active" in out and "38801" not in out:
        line("warn", "Voice: firewall", "UFW is on and blocks the talk groups: sudo ufw allow in on eth0 proto udp to any port 38801:38810")


def nm_connection(iface):
    """The NetworkManager connection on this port, or ''."""
    rc, out = sh("nmcli", "-t", "-f", "NAME,DEVICE", "connection", "show", "--active")
    for ln in out.splitlines() if rc == 0 else []:
        name, _, dev = ln.rpartition(":")
        if dev == iface:
            return name.replace("\\:", ":")
    return ""


def check_mesh_list(cp):
    """Ops lists the mesh's nodes from OpenMANET's alfred, which only answers an EUI-64 IPv6 address."""
    if cp is not None and sec(cp, "dashboard", "MESH_DISCOVERY", "alfred").lower() != "alfred":
        return
    iface = mesh_iface(cp, "dashboard") if cp is not None else "eth0"
    if eui64_lladdr(iface):
        line("ok", "Mesh node list", "%s can read the mesh's own node list (alfred)" % iface)
    else:
        conn = nm_connection(iface)
        fix = ('sudo nmcli connection modify "%s" ipv6.addr-gen-mode eui64 && sudo nmcli connection up "%s"' % (conn, conn)
               if conn else "run install.sh again, or give %s an EUI-64 IPv6 address" % iface)
        # info, not a warning: Ops still works from NODE_IP, and a deck with IPv6 off on purpose shouldn't fail its check
        line("info", "Mesh node list", "%s has no EUI-64 IPv6 address, so Ops lists only the nodes in NODE_IP. Fix: %s" % (iface, fix))
    p = os.path.expanduser(sec(cp, "dashboard", "MESH_STATUS_FILE", "~/mesh_status.json") if cp is not None else "~/mesh_status.json")
    try:
        with open(p) as f:
            m = json.load(f)
        if time.time() - float(m.get("now", 0)) < 120:
            sl = m.get("slowest_mbps")
            line("info", "Mesh", "%s of %s nodes answer, %s radio link(s)%s" % (
                m.get("nodes_up"), m.get("nodes_total"), m.get("links"), ", slowest %.1f Mbit/s" % sl if sl is not None else ""))
    except (OSError, ValueError, TypeError):
        pass


def cmd_check():
    print("%s %s  (%s)" % (NAME, suite_version(), RELEASE))
    check_files()
    check_units()
    cp = check_config()
    check_system()
    check_runtime(cp)
    check_mesh_list(cp)
    check_voice(cp)
    fails, warns, todos = results.count("fail"), results.count("warn"), results.count("todo")
    if todos and not fails and not warns:
        print("\nInstalled. One thing left to set up: your TAK server (the TODO above).")
    else:
        print("\n%s" % ("All good." if not fails and not warns else "%d problem(s), %d warning(s)." % (fails, warns))
              + (" Still to set up: the TAK server (TODO)." if todos else ""))
    return 1 if fails or todos else 0


def cmd_status():
    print("%s %s" % (NAME, suite_version()))
    for prog, unit in UNITS.items():
        a = unit_prop(unit, "ActiveState") or "?"
        since = unit_prop(unit, "ActiveEnterTimestamp")
        print("  %-10s %-9s %s" % (prog, a, ("since " + since) if a == "active" and since else ""))
    st, age = bridge_status()
    if st:
        print("  bridge     status %.0f s old, mesh events %s, server events %s" % (
            age, (st.get("mesh") or {}).get("events", "?"), (st.get("server") or {}).get("rx_events", "?")))
    cp = load_cfg()
    if cp is not None and sec(cp, "deck", "GPS", "off").lower() == "gpsd":
        ok, tpv = gpsd_report(sec(cp, "deck", "GPS_HOST", "127.0.0.1:2947"))
        print("  gps        %s" % ("gpsd down" if not ok else ("no fix" if not tpv or int(tpv.get("mode") or 0) < 2 else "%sD fix" % tpv["mode"])))
    if voice_on(cp):
        vs, vage = voice_status_file(cp)
        if vs is None or vage > 10:
            print("  voice      gateway not running")
        else:
            talk = ["%s: %s" % (g["name"], g["talking"]["name"]) for g in vs.get("groups") or [] if g.get("talking")]
            br = [g for g in vs.get("groups") or [] if g.get("bridged")]
            print("  voice      %s; %d/%d talk group(s) linked to Mumble" % (
                "talking " + ", ".join(talk) if talk else "channel clear", sum(1 for g in br if g.get("mumble_up")), len(br)))
    print("  config     %s" % CONFIG)
    return 0


def cmd_ctl(action):
    rc = subprocess.call(["sudo", SYSTEMCTL, action, TARGET])
    if rc == 0:
        print("%s: done" % action)
    return rc


def cmd_logs(args):
    which = [a for a in args if a in UNITS]
    n = "50"
    if "-n" in args:
        i = args.index("-n")
        n = args[i + 1] if i + 1 < len(args) else n
    cmd = ["journalctl", "--no-pager", "-n", n]
    for w in which or UNITS:
        cmd += ["-u", UNITS[w]]
    return subprocess.call(cmd)


def cmd_rollback():
    prev = os.path.join(BASE, "previous")
    if not os.path.islink(prev) or not os.path.isdir(prev):
        print("No previous version to go back to.")
        return 1
    cur = os.path.join(BASE, "current")
    a, b = os.readlink(cur), os.readlink(prev)
    for link, dest in ((cur, b), (prev, a)):
        tmp = link + ".tmp"
        if os.path.lexists(tmp):
            os.remove(tmp)
        os.symlink(dest, tmp)
        os.replace(tmp, link)
    print("current -> %s (was %s)" % (b, a))
    return cmd_ctl("restart")


VER_DIR = re.compile(r"^(?:TAK_MESH_TOC|MANET_MESH_BRIDGE)_v(\d+(?:\.\d+)*)$")      # the package's name before 3.0 too
VER_ZIP = re.compile(r"^(?:TAK_MESH_TOC|MANET_MESH_BRIDGE)_v(\d+(?:\.\d+)*)(?: ?\(\d+\))?\.zip$")


def _size(p):
    if os.path.isfile(p) or os.path.islink(p):
        return os.path.getsize(p) if os.path.isfile(p) else 0
    t = 0
    for root, _, files in os.walk(p):
        for n in files:
            try:
                t += os.path.getsize(os.path.join(root, n))
            except OSError:
                pass
    return t


def _rm(p):
    if os.path.isdir(p) and not os.path.islink(p):
        for root, dirs, _ in os.walk(p):           # releases are read-only: make them writable first
            os.chmod(root, 0o755)
            for d in dirs:
                try:
                    os.chmod(os.path.join(root, d), 0o755)
                except OSError:
                    pass
        shutil.rmtree(p)
    else:
        os.remove(p)


def tidy_plan(keep_version=None):
    """[(path, why)] that tidy would remove -- nothing is changed here."""
    keep = {keep_version} if keep_version else set()
    for link in ("current", "previous"):
        lp = os.path.join(BASE, link)
        if os.path.islink(lp):
            keep.add(os.path.basename(os.readlink(lp)))
    cur = os.path.islink(os.path.join(BASE, "current")) and os.path.basename(os.readlink(os.path.join(BASE, "current")))
    current_ver = keep_version or cur
    out = []
    rel = os.path.join(BASE, "releases")
    if os.path.isdir(rel):
        for v in sorted(os.listdir(rel)):
            if v not in keep:
                out.append((os.path.join(rel, v), "old release %s (current and previous are kept)" % v))
    for d in (HOME, os.path.join(HOME, "Downloads")):
        if not os.path.isdir(d):
            continue
        names = sorted(os.listdir(d))
        cur_zips = [n for n in names if VER_ZIP.match(n) and VER_ZIP.match(n).group(1) == current_ver]
        mine = [z for z in ("TAK_MESH_TOC_v%s.zip" % current_ver, "MANET_MESH_BRIDGE_v%s.zip" % current_ver) if z in cur_zips]
        keep_zip = mine[0] if mine else (cur_zips[0] if cur_zips else None)
        for n in names:
            p = os.path.join(d, n)
            m, z = VER_DIR.match(n), VER_ZIP.match(n)
            if m and m.group(1) != current_ver and os.path.isdir(p):
                out.append((p, "unzipped package %s (the installed copy lives in releases/)" % m.group(1)))
            elif z and n != keep_zip:
                out.append((p, "old package zip" if z.group(1) != current_ver else "duplicate download of this version"))
    return out


def cmd_tidy(args):
    dry, yes = "--dry-run" in args, "--yes" in args
    keep = None
    if "--keep" in args:
        i = args.index("--keep")
        keep = args[i + 1] if i + 1 < len(args) else None
    remove = tidy_plan(keep)
    if not remove:
        print("   tidy: nothing left behind by older versions")
        return 0
    total = 0
    print("   tidy: these are left over from older versions:")
    for p, why in remove:
        sz = _size(p); total += sz
        print("     remove  %-60s %7.1f MB  %s" % (p.replace(HOME, "~"), sz / 1e6, why))
    print("   kept: deck.ini, current + previous release, logs, the current package zip/folder")
    if dry:
        print("   [dry-run] would remove %d item(s) (%.1f MB)" % (len(remove), total / 1e6))
        return 0
    if not yes:
        if not sys.stdin.isatty():
            print("   skipped (not asked: run  meshtoc tidy  yourself)")
            return 0
        ans = input("   Remove these? [y/N] ").strip().lower()
        if ans not in ("y", "yes"):
            print("   kept everything")
            return 0
    for p, _ in remove:
        try:
            _rm(p)
        except OSError as exc:
            print("   could not remove %s: %s" % (p, exc))
    print("   tidy: removed %d item(s), %.1f MB freed" % (len(remove), total / 1e6))
    return 0


def main():
    a = sys.argv[1:] or ["status"]
    c = a[0]
    if c in ("-h", "--help", "help"):
        print(__doc__.strip())
        return 0
    if c == "version":
        print(suite_version())
        return 0
    if c == "check":
        return cmd_check()
    if c == "status":
        return cmd_status()
    if c in ("restart", "start", "stop"):
        return cmd_ctl(c)
    if c == "logs":
        return cmd_logs(a[1:])
    if c == "rollback":
        return cmd_rollback()
    if c == "tidy":
        return cmd_tidy(a[1:])
    print("unknown command %r (meshtoc help)" % c)
    return 2


if __name__ == "__main__":
    sys.exit(main())
