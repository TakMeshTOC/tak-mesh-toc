"""setup_wizard.py: the first-run setup behind Ops' /setup page.

Does by itself what a new user would otherwise do by hand:
  * opens the OpenTAKServer / ATAK data package (zips inside zips), finds the deck's certificate, the server's CA
    and the server address, and converts the certificates to the PEM files the bridge reads (with openssl);
    the password comes from the package when it carries one, else the user types it
  * tests the TLS connection to the server with those certificates
  * lists the network ports, so the user can pick the mesh one
  * writes the choices into deck.ini (a backup first; comments and every other line kept) and ~/certs,
    then restarts the services
Nothing is written until finish(). Only the deck's own screen can reach it (dashboard_server checks that).
Standard library only, plus the system's openssl command.
"""
import configparser
import io
import os
import re
import shutil
import socket
import ssl
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
import zipfile

MAX_PACKAGE = 8 * 1024 * 1024        # a data package is a few KB; anything this big isn't one
MAX_UNZIPPED = 32 * 1024 * 1024      # and won't unpack to more than this (zip bombs)
MAX_DEPTH = 3                        # zips inside zips: OTS packages nest one level

import sys  # noqa: E402
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "common"))
from deckcommon import ROLES, TEAMS  # noqa: E402  the same lists the bridge and Chat accept
CALLSIGN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.\-]{0,31}$")
HOST_RE = re.compile(r"^[A-Za-z0-9.\-:\[\]]{1,253}$")


class WizardError(Exception):
    """Something the user can fix; the message says what, in plain words."""


# ------------------------------------------------------------------ the data package
def read_package(data):
    """The certificates and settings inside an OTS/ATAK data package. Zips inside zips are opened; only file
    contents are read (never written to disk under names from the zip). Returns
    {"client": (name, p12 bytes), "ca": (name, p12 bytes) or None, "prefs": {...}}."""
    if len(data) > MAX_PACKAGE:
        raise WizardError("that file is too big to be a data package")
    found, total = {}, [0]

    def walk(blob, depth):
        try:
            zf = zipfile.ZipFile(io.BytesIO(blob))
        except zipfile.BadZipFile:
            raise WizardError("that isn't a zip file. Use the data package (.zip) OpenTAKServer gives you for a device")
        for info in zf.infolist():
            if info.is_dir():
                continue
            total[0] += info.file_size
            if info.file_size > MAX_UNZIPPED or total[0] > MAX_UNZIPPED:
                raise WizardError("that file unpacks to far more than a data package would")
            name = os.path.basename(info.filename)
            low = name.lower()
            if low.endswith(".zip") and depth < MAX_DEPTH:
                walk(zf.read(info), depth + 1)
            elif low.endswith((".p12", ".pfx", ".pref")):
                found.setdefault(name, zf.read(info))

    walk(data, 0)
    p12s = {n: b for n, b in found.items() if n.lower().endswith((".p12", ".pfx"))}
    cas = sorted(n for n in p12s if n.lower().startswith(("truststore", "ca", "root")))
    clients = sorted(n for n in p12s if n not in cas)
    if not clients:
        raise WizardError("no device certificate (.p12) in that package. Make a certificate for this deck in "
                          "OpenTAKServer and download its data package")
    prefs = {}
    for n, b in found.items():
        if n.lower().endswith(".pref"):
            prefs.update(parse_prefs(b))
    return {"client": (clients[0], p12s[clients[0]]), "ca": (cas[0], p12s[cas[0]]) if cas else None, "prefs": prefs}


def parse_prefs(blob):
    """Server address and passwords from an ATAK .pref file (XML <entry key=...>)."""
    out = {}
    try:
        root = ET.fromstring(blob)
    except ET.ParseError:
        return out
    for e in root.iter("entry"):
        k, v = e.get("key", ""), (e.text or "").strip()
        if not v:
            continue
        if k == "connectString0":
            out["connect"] = v
        elif k in ("clientPassword", "clientPassword0"):
            out["client_password"] = v
        elif k in ("caPassword", "caPassword0"):
            out["ca_password"] = v
        elif k == "description0":
            out["description"] = v
    return out


def parse_connect(s):
    """'host:port:ssl' (ATAK's connectString) -> (host, port, proto), or None."""
    parts = (s or "").rsplit(":", 2)
    if len(parts) != 3 or not parts[1].isdigit():
        return None
    return parts[0], int(parts[1]), parts[2].lower()


def _openssl(args, password, stdin=None):
    env = dict(os.environ, MMB_P12_PASS=password or "")
    try:
        r = subprocess.run(["openssl"] + args + ["-passin", "env:MMB_P12_PASS"], input=stdin, capture_output=True,
                           env=env, timeout=20)
    except FileNotFoundError:
        raise WizardError("the openssl command is missing: sudo apt install openssl")
    return r.returncode, r.stdout, r.stderr.decode(errors="replace")


def p12_to_pem(p12, password, part):
    """part: 'cert' (the deck's certificate), 'key' (its private key, unencrypted) or 'ca'. Bytes of PEM.
    OpenSSL 3 needs -legacy for the older encryption most TAK servers use; older OpenSSL has no such flag."""
    args = {"cert": ["-clcerts", "-nokeys"], "key": ["-nocerts", "-nodes"], "ca": ["-nokeys"]}[part]
    with tempfile.TemporaryDirectory() as d:
        os.chmod(d, 0o700)
        src = os.path.join(d, "in.p12")
        with open(src, "wb") as f:
            f.write(p12)
        err = ""
        for legacy in (["-legacy"], []):
            rc, out, err = _openssl(["pkcs12"] + legacy + ["-in", src] + args, password)
            blocks = re.findall(rb"-----BEGIN [A-Z ]+-----.*?-----END [A-Z ]+-----\n?", out, re.S)
            if rc == 0 and blocks:
                return b"".join(b if b.endswith(b"\n") else b + b"\n" for b in blocks)   # PEM only, no "Bag Attributes" lines
        if re.search(r"mac verify|invalid password|bad decrypt", err, re.I):
            raise WizardError("wrong password for the certificate")
        raise WizardError("openssl could not read the certificate (%s)" % (err.strip().splitlines() or ["?"])[-1][:160])


def cert_info(pem):
    """{'subject': 'CN=...', 'until': 'Oct 26 ... 2027 GMT'} for a PEM certificate (best effort)."""
    try:
        r = subprocess.run(["openssl", "x509", "-noout", "-subject", "-enddate"], input=pem, capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    info = {}
    for line in r.stdout.decode(errors="replace").splitlines():
        if line.startswith("subject="):
            m = re.search(r"CN\s*=\s*([^,/]+)", line)
            info["name"] = m.group(1).strip() if m else line[8:].strip()
        elif line.startswith("notAfter="):
            info["until"] = line[9:].strip()
    return info


def unlock(pkg, password=""):
    """Convert the package's certificates. Tries the given password, then the package's own. Returns the PEMs."""
    tries = [p for p in (password, pkg["prefs"].get("client_password"), "atakatak") if p]
    last = None
    for used in dict.fromkeys(tries):
        try:
            cert = p12_to_pem(pkg["client"][1], used, "cert")
            key = p12_to_pem(pkg["client"][1], used, "key")
            break
        except WizardError as exc:
            last = exc
    else:
        raise WizardError("the certificate needs its password" if not password else str(last))
    ca = None
    if pkg["ca"]:
        for ca_pw in dict.fromkeys([pkg["prefs"].get("ca_password"), used, "atakatak"]):
            if not ca_pw:
                continue
            try:
                ca = p12_to_pem(pkg["ca"][1], ca_pw, "ca")
                break
            except WizardError:
                continue
        if ca is None:
            raise WizardError("could not open the server's CA certificate (%s)" % pkg["ca"][0])
    return {"cert": cert, "key": key, "ca": ca, "info": cert_info(cert), "used_package_password": used != password}


# ------------------------------------------------------------------ testing the server
def ping_event(uid):
    now = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
    return ('<?xml version="1.0"?><event version="2.0" uid="%s-setupping" type="t-x-c-t" how="h-g-i-g-o" time="%s" '
            'start="%s" stale="%s"><point lat="0" lon="0" hae="0" ce="9999999" le="9999999"/><detail/></event>'
            % (uid, now, now, now)).encode()


def test_server(host, port, pems, uid="DECK-setup", timeout=6.0):
    """TLS handshake with the deck's certificate, then one ping. Returns {'ok', 'hostname_check', 'answers', 'detail'}."""
    if not HOST_RE.match(host or "") or not (0 < int(port) < 65536):
        raise WizardError("that server address doesn't look right")
    with tempfile.TemporaryDirectory() as d:
        os.chmod(d, 0o700)
        paths = {}
        for k in ("cert", "key", "ca"):
            if pems.get(k):
                paths[k] = os.path.join(d, k + ".pem")
                with open(paths[k], "wb") as f:
                    f.write(pems[k])
        for check_name in (True, False):
            # the same kind of context the bridge's PyTAK makes (no Python 3.13 "strict" checks, which reject the
            # CA certificates many TAK servers make), so this test passes exactly when the bridge will connect
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            if paths.get("ca"):
                ctx.load_verify_locations(paths["ca"])
                ctx.check_hostname = check_name
            else:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            ctx.load_cert_chain(paths["cert"], paths["key"])
            try:
                raw = socket.create_connection((host, int(port)), timeout=timeout)
            except OSError as exc:
                return {"ok": False, "detail": "can't reach %s:%s (%s). Is Tailscale up, and the address right?"
                        % (host, port, exc.strerror or exc)}
            try:
                s = ctx.wrap_socket(raw, server_hostname=host)
            except ssl.SSLCertVerificationError as exc:
                raw.close()
                if check_name and "mismatch" in str(exc).lower():
                    continue                          # the server's certificate doesn't list this address: try without
                return {"ok": False, "detail": "the server's certificate isn't trusted by this package's CA (%s)" % exc.verify_message}
            except (ssl.SSLError, OSError) as exc:
                raw.close()
                return {"ok": False, "detail": "the server refused this deck's certificate (%s). Was it made on this server?" % exc}
            # In TLS 1.3 the server checks the deck's certificate AFTER the handshake looks done on this side, so a
            # refusal only shows up when reading. Send a ping and listen: an alert or a closed connection = refused.
            answers, closed = False, None
            try:
                s.settimeout(3.0)
                s.sendall(ping_event(uid))
                buf, end = b"", time.time() + 3.0
                while time.time() < end and b"t-x-c-t" not in buf:
                    chunk = s.recv(65536)
                    if not chunk:
                        closed = "the server closed the connection straight away"
                        break
                    buf += chunk
                answers = b"t-x-c-t" in buf
            except socket.timeout:
                pass                                  # still connected, just no ping answer: some servers don't answer
            except (ssl.SSLError, OSError) as exc:
                closed = str(getattr(exc, "reason", None) or exc)
            finally:
                s.close()
            if closed and not answers:
                return {"ok": False, "detail": "the server did not accept this deck's certificate (%s). Was this package "
                        "made on this server?" % closed}
            return {"ok": True, "hostname_check": check_name, "answers": answers,
                    "detail": "connected" + (", the server answers pings" if answers else "")
                    + ("" if check_name else ". The server's certificate doesn't list this address, so the name check "
                       "is turned off (PYTAK_TLS_DONT_CHECK_HOSTNAME = 1), the usual setup over Tailscale")}
    return {"ok": False, "detail": "could not connect"}


# ------------------------------------------------------------------ the deck itself
def run(cmd, timeout=5):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout
    except (OSError, subprocess.TimeoutExpired):
        return 127, ""


def interfaces():
    """[{'name','addr','prefix','carrier','internet'}] for each IPv4 port except loopback and VPNs."""
    rc, out = run(["ip", "-4", "-o", "addr", "show"])
    _, route = run(["ip", "-4", "route", "show", "default"])
    inet = set(re.findall(r"\bdev (\S+)", route or ""))
    res = []
    for line in (out or "").splitlines():
        m = re.match(r"\d+:\s+(\S+)\s+inet\s+([\d.]+)/(\d+)", line)
        if not m or m.group(1) == "lo" or m.group(1).startswith(("tailscale", "zt", "wg", "tun", "docker")):
            continue
        name = m.group(1)
        try:
            with open("/sys/class/net/%s/carrier" % name) as f:
                carrier = f.read().strip() == "1"
        except OSError:
            carrier = None
        res.append({"name": name, "addr": m.group(2), "prefix": int(m.group(3)), "carrier": carrier,
                    "internet": name in inet, "wireless": os.path.isdir("/sys/class/net/%s/wireless" % name)})
    return res


def facts(pkg_dir):
    """Step 1: is this deck ready? [{'level': ok|warn|bad|info, 'title', 'detail'}]"""
    rows = []
    def add(level, title, detail):
        rows.append({"level": level, "title": title, "detail": detail})
    try:
        with open("/proc/device-tree/model") as f:
            add("ok", "Computer", f.read().strip("\x00\n "))
    except OSError:
        add("info", "Computer", "not a Raspberry Pi (fine)")
    try:
        with open("/etc/os-release") as f:
            name = re.search(r'^PRETTY_NAME="?([^"\n]*)', f.read(), re.M)
        add("ok", "Operating system", name.group(1) if name else "Linux")
    except OSError:
        add("info", "Operating system", "unknown")
    libs = sorted(n.split("-")[0] + " " + n.split("-")[1].replace(".dist-info", "")
                  for n in os.listdir(os.path.join(pkg_dir, "vendor")) if n.endswith(".dist-info")) \
        if os.path.isdir(os.path.join(pkg_dir, "vendor")) else []
    add("ok" if libs else "bad", "Bundled libraries", ", ".join(libs) + " (no internet needed)" if libs else "missing: reinstall")
    add("ok" if shutil.which("openssl") else "bad", "openssl", "found (reads the certificates)" if shutil.which("openssl")
        else "missing: sudo apt install openssl")
    st = os.statvfs(os.path.expanduser("~"))
    free = st.f_bavail * st.f_frsize / 1e9
    add("ok" if free > 1 else "warn", "Free disk space", "%.0f GB" % free if free >= 10 else "%.1f GB" % free)
    try:
        socket.create_connection(("1.1.1.1", 443), timeout=2).close()
        add("ok", "Internet", "connected (used to reach the server)")
    except OSError:
        add("warn", "Internet", "none right now. The server is reached over the internet or a VPN")
    ifs = interfaces()
    add("ok" if ifs else "warn", "Network ports", ", ".join("%s %s%s" % (i["name"], i["addr"], " (internet)" if i["internet"] else "")
                                                            for i in ifs) or "none with an address")
    rc, out = run(["tailscale", "ip", "-4"])
    add("ok" if rc == 0 and out.strip() else "info", "Tailscale", "up" if rc == 0 and out.strip() else "not running (fine if you use another VPN)")
    try:
        socket.create_connection(("127.0.0.1", 2947), timeout=1).close()
        add("ok", "GPS (gpsd)", "found (optional)")
    except OSError:
        add("info", "GPS (gpsd)", "not found (optional: the deck can use a fixed position)")
    rc, out = run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"])
    if rc == 0:
        add("ok" if out.strip() == "yes" else "warn", "Clock", "synced" if out.strip() == "yes" else
            "not synced yet. TAK hides reports with a wrong time: connect to the internet or let GPS set it")
    return rows


# ------------------------------------------------------------------ writing deck.ini
def set_ini(path, changes):
    """changes = {section: {KEY: value or None}}. A key's live line is replaced; if only a commented example line
    exists ('# KEY = ...') it is switched on; else the line is added at the end of its section. None turns a
    live line back into a comment. Every other line, comment and blank line stays as it was. A backup is kept."""
    with open(path) as f:
        lines = f.read().splitlines()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = "%s.before-setup-%s" % (path, stamp)
    shutil.copy2(path, backup)
    os.chmod(backup, 0o600)
    for section, kv in changes.items():
        for key, value in kv.items():
            if value is not None and ("\n" in str(value) or "\r" in str(value)):
                raise WizardError("a setting can't contain a line break")
            lo, hi = _section(lines, section)
            if lo is None:
                if value is None:
                    continue
                lines += ["", "[%s]" % section, "%s = %s" % (key, value)]
                continue
            live = [i for i in range(lo + 1, hi) if re.match(r"^\s*%s\s*=" % re.escape(key), lines[i], re.I)]
            if live:
                for i in live:
                    lines[i] = "# %s = %s" % (key, lines[i].split("=", 1)[1].strip()) if value is None else "%s = %s" % (key, value)
                continue
            if value is None:
                continue
            example = [i for i in range(lo + 1, hi) if re.match(r"^\s*#\s*%s\s*=" % re.escape(key), lines[i], re.I)]
            if example:
                lines[example[0]] = "%s = %s" % (key, value)
                continue
            at = hi
            while at - 1 > lo and not lines[at - 1].strip():
                at -= 1
            lines.insert(at, "%s = %s" % (key, value))
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write("\n".join(lines) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return backup


def _section(lines, name):
    lo = None
    for i, line in enumerate(lines):
        m = re.match(r"^\s*\[([^\]]+)\]\s*$", line)
        if m:
            if lo is not None:
                return lo, i
            if m.group(1).strip().lower() == name.lower():
                lo = i
    return (lo, len(lines)) if lo is not None else (None, None)


def write_certs(pems, certs_dir):
    """deck.pem, deck.key and ca.pem in certs_dir (mode 700; files 600). Old files with other content are kept
    as .before-setup-<time>. Returns the paths as ~/... strings for deck.ini."""
    os.makedirs(certs_dir, exist_ok=True)
    os.chmod(certs_dir, 0o700)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = {}
    for k, name in (("cert", "deck.pem"), ("key", "deck.key"), ("ca", "ca.pem")):
        if not pems.get(k):
            continue
        p = os.path.join(certs_dir, name)
        if os.path.exists(p):
            with open(p, "rb") as f:
                if f.read() != pems[k]:
                    shutil.copy2(p, "%s.before-setup-%s" % (p, stamp))
        tmp = p + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(pems[k])
        os.chmod(tmp, 0o600)
        os.replace(tmp, p)
        out[k] = p
    return out


def plan(session, choices, certs_dir):
    """The deck.ini changes for these choices (also shown to the user before Finish)."""
    host, port = choices["host"], int(choices["port"])
    # full paths: PyTAK doesn't know "~" (the bridge expands it since 3.1, but a full path works everywhere)
    tilde = {k: os.path.abspath(os.path.join(certs_dir, n)) for k, n in (("cert", "deck.pem"), ("key", "deck.key"), ("ca", "ca.pem"))}
    bridge = {"COT_URL": "tls://%s:%d" % (host, port), "PYTAK_TLS_CLIENT_CERT": tilde["cert"],
              "PYTAK_TLS_CLIENT_KEY": tilde["key"], "PYTAK_TLS_CLIENT_CAFILE": tilde["ca"] if session.get("pems", {}).get("ca") else None,
              "PYTAK_TLS_CLIENT_PASSWORD": None,
              "PYTAK_TLS_DONT_CHECK_HOSTNAME": "1" if not session.get("test", {}).get("hostname_check", True) else None,
              "RETURN_PATH": "1" if choices.get("return_path", True) else "0",
              "DM_RELAY": "1" if choices.get("dm_relay") else "0"}
    callsign = (choices.get("callsign") or "").strip()
    if not CALLSIGN_RE.match(callsign):
        raise WizardError("a callsign is 1 to 32 letters, digits, spaces, dots, dashes or underscores")
    if choices.get("team") not in TEAMS or choices.get("role") not in ROLES:
        raise WizardError("pick a team colour and a role")
    deck = {"CALLSIGN": callsign, "TEAM": choices["team"], "ROLE": choices["role"],
            "GPS": "gpsd" if choices.get("gps") else "off"}
    changes = {"deck": deck, "bridge": bridge, "dashboard": {"LISTEN": "127.0.0.1" if choices.get("ops_local", True) else "auto"}}
    iface = choices.get("mesh_iface")
    if iface:
        match = [i for i in interfaces() if i["name"] == iface]
        if not match and not choices.get("mesh_addr"):
            raise WizardError("that network port has no address right now")
        deck["MESH_ADDR"] = (choices.get("mesh_addr") or "").strip() or "auto"   # auto follows the node's DHCP
        changes["chat"] = {"MESH_IFACE": iface}
        changes["dashboard"]["MESH_IFACE"] = iface
    return changes


def finish(session, choices, config_path, certs_dir):
    """Write ~/certs and deck.ini. Returns {'backup': path, 'changes': {...}}."""
    if not session.get("pems"):
        raise WizardError("the server step isn't done yet")
    changes = plan(session, choices, certs_dir)
    write_certs(session["pems"], certs_dir)
    backup = set_ini(config_path, changes)
    return {"backup": backup, "changes": changes}


def restart_later(delay=1.5):
    """Restart every service a moment after this request has been answered (this page's own server restarts too).
    False if this user can't use sudo without a password: then the page asks the user to run meshtoc restart."""
    if os.environ.get("MMB_SETUP_NO_RESTART"):        # tests: never restart this machine's services
        return False
    if run(["sudo", "-n", "true"])[0] != 0:
        return False
    subprocess.Popen(["sh", "-c", "sleep %s; sudo -n systemctl restart manet_mesh_bridge.target" % float(delay)],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    return True


def check_rows(pkg_dir, timeout=40):
    """`meshtoc check`, as rows for the page: [{'level','title','detail'}] plus the last line."""
    rc, out = run([os.environ.get("PYTHON", "python3"), os.path.join(pkg_dir, "tools", "manet_mesh_bridge.py"), "check"], timeout=timeout)
    rows, last = [], ""
    lv = {"OK": "ok", "WARN": "warn", "FAIL": "bad", "info": "info", "TODO": "warn"}
    for line in (out or "").splitlines():
        m = re.match(r"^\s*\[\s*(OK|WARN|FAIL|info|TODO)\s*\]\s+(.{26})\s?(.*)$", line)
        if m:
            rows.append({"level": lv[m.group(1)], "title": m.group(2).strip(), "detail": m.group(3).strip()})
        elif line.strip():
            last = line.strip()
    return {"rows": rows, "summary": last}


def helper_zip(pkg_dir, deck_uid):
    """The server helper (mesh_mailroute) as a zip, plus the one command to install it on the server."""
    src = os.path.join(pkg_dir, "server", "mesh_mailroute")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name in ("README.md", "install_server.sh", "uninstall_server.sh", "mesh_mailroute.py", "mesh_mailroute.ini.example"):
            p = os.path.join(src, name)
            if os.path.exists(p):
                z.write(p, "mesh_mailroute_src/" + name)
    fname = "mesh_mailroute_%s.zip" % re.sub(r"[^A-Za-z0-9_.-]", "_", deck_uid)
    cmd = "cd ~ && unzip -o %s && bash mesh_mailroute_src/install_server.sh --deck-uid %s" % (fname, deck_uid)
    return fname, buf.getvalue(), cmd


def deck_uid(cfg_path):
    """The deck's UID as the bridge will use it: [deck] UID, else DECK-<hostname>."""
    try:
        cp = configparser.ConfigParser(interpolation=None, strict=False)
        cp.read(cfg_path, encoding="utf-8-sig")
        uid = cp.get("deck", "UID", fallback="").strip()
    except Exception:
        uid = ""
    return uid or "DECK-" + socket.gethostname()

