"""meshwatch.py - what the OpenMANET mesh says about itself, read without any login.

Two sources, both part of stock OpenMANET:
  alfred    The nodes gossip their name, IP and batman neighbor list (with link throughput) to each other over
            alfred (UDP 16962, IPv6 link-local). Any device on the mesh LAN may ask for it. alfred only answers an
            EUI-64 link-local address, so the deck's mesh port needs one (install.sh sets
            ipv6.addr-gen-mode = eui64 on its NetworkManager connection).
  status    Every node answers GetDashboardStatus on port 8087 without a login: CPU temperature, load, memory,
            uptime.
Standard library only. Nothing here changes anything on a node.
"""
import json
import random
import socket
import struct
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

ALFRED_PORT = 0x4242                  # 16962
NODE_TYPE, NEIGHBORS_TYPE = 102, 103  # OpenMANET's alfred data types (node info, batman neighbors)
STATUS_PATH = "/openmanet.dashboard.v1.DashboardService/GetDashboardStatus"
NEIGHBOR_MAX_AGE_MS = 15000           # a batman neighbor not heard for longer is not a live link
FORGET_AFTER = 900                    # a node gone from alfred this long is dropped from the list
IF_INET6 = "/proc/net/if_inet6"


# ----------------------------------------------------------------- protobuf, just enough to read alfred records
def _varint(b, i):
    r = s = 0
    while True:
        c = b[i]
        i += 1
        r |= (c & 0x7F) << s
        s += 7
        if not c & 0x80:
            return r, i


def pb(buf):
    """{field number: [values]}: varints as int, length-delimited as bytes. Stops quietly at anything odd."""
    out, i = {}, 0
    try:
        while i < len(buf):
            key, i = _varint(buf, i)
            f, wt = key >> 3, key & 7
            if wt == 0:
                v, i = _varint(buf, i)
            elif wt == 2:
                n, i = _varint(buf, i)
                v, i = bytes(buf[i:i + n]), i + n
            elif wt == 1:
                v, i = bytes(buf[i:i + 8]), i + 8
            elif wt == 5:
                v, i = bytes(buf[i:i + 4]), i + 4
            else:
                break
            out.setdefault(f, []).append(v)
    except IndexError:
        pass
    return out


def _txt(d, f):
    v = d.get(f)
    return v[0].decode("utf-8", "replace").strip() if v and isinstance(v[0], bytes) else ""


def _int(d, f):
    v = d.get(f)
    return v[0] if v and isinstance(v[0], int) else 0


# ----------------------------------------------------------------- alfred
def eui64_lladdr(iface):
    """The port's EUI-64 IPv6 link-local address (the kind alfred answers), or None."""
    try:
        with open(IF_INET6) as f:
            for line in f:
                p = line.split()
                if len(p) >= 6 and p[5] == iface and p[3] == "20":
                    a = bytes.fromhex(p[0])
                    if a[:2] == b"\xfe\x80" and a[11:13] == b"\xff\xfe":
                        return socket.inet_ntop(socket.AF_INET6, a)
    except OSError:
        pass
    return None


def parse_push(pkt, into):
    """Records from one alfred PUSH_DATA packet into {(type, publisher mac bytes): payload}."""
    i = 8                                     # tlv header (4) + transaction id and seqno (4)
    while i + 10 <= len(pkt):
        pub = bytes(pkt[i:i + 6])
        dtype, _ver, dlen = struct.unpack("!BBH", pkt[i + 6:i + 10])
        if i + 10 + dlen > len(pkt):
            break
        into[(dtype, pub)] = bytes(pkt[i + 10:i + 10 + dlen])
        i += 10 + dlen


def collect(sock, send, seconds=2.5):
    """Ask every alfred server for node and neighbor records. Returns (records, servers that answered)."""
    data, servers = {}, set()
    for t in (NODE_TYPE, NEIGHBORS_TYPE):
        send(struct.pack("!BBHBH", 2, 0, 3, t, random.randrange(65536)))   # ALFRED_REQUEST, version 0
    end = time.time() + seconds
    while True:
        left = end - time.time()
        if left <= 0:
            break
        sock.settimeout(min(0.5, left))
        try:
            pkt, addr = sock.recvfrom(65535)
        except socket.timeout:
            continue
        if len(pkt) < 4 or pkt[1] != 0:
            continue
        if pkt[0] in (0, 3):                  # PUSH_DATA or STATUS_TXEND: a server answering us
            servers.add(str(addr[0]).split("%")[0])
        if pkt[0] == 0:
            parse_push(pkt, data)
    return data, servers


def alfred_query(iface, seconds=2.5):
    """(records, servers, why-not). why-not is '' when the query could be sent."""
    src = eui64_lladdr(iface)
    if not src:
        return {}, set(), "the mesh port has no EUI-64 IPv6 address (run: meshtoc check)"
    try:
        idx = socket.if_nametoindex(iface)
    except OSError:
        return {}, set(), "no port %s" % iface
    s = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
    try:
        try:
            s.setsockopt(socket.SOL_SOCKET, 25, iface.encode())          # SO_BINDTODEVICE: the mesh port only
        except OSError:
            pass
        try:
            s.bind(("::", ALFRED_PORT))
        except OSError as exc:
            return {}, set(), "alfred port %d is busy (%s)" % (ALFRED_PORT, exc.strerror or exc)
        info = socket.inet_pton(socket.AF_INET6, src) + struct.pack("@I", idx)

        def send(pkt):
            s.sendmsg([pkt], [(socket.IPPROTO_IPV6, socket.IPV6_PKTINFO, info)], 0, ("ff02::1", ALFRED_PORT, 0, idx))
        data, servers = collect(s, send, seconds)
        return data, servers, ""
    except OSError as exc:
        return {}, set(), "alfred query failed (%s)" % (exc.strerror or exc)
    finally:
        s.close()


def parse_records(data):
    """alfred records -> (nodes [{name, ip, mac}], links [{a, b, kbps}]) with names, one entry per node pair."""
    owner = {}                                # any MAC of a node -> its name
    nodes = []
    for (t, pub), v in data.items():
        if t != NODE_TYPE:
            continue
        n = pb(v)
        name, ip, mac = _txt(n, 2), _txt(n, 3), _txt(n, 1).lower()
        if not ip:
            continue
        name = name or ip
        nodes.append({"name": name, "ip": ip, "mac": mac})
        owner[mac] = name
        owner[":".join("%02x" % b for b in pub)] = name
    reports = []
    for (t, pub), v in data.items():
        if t != NEIGHBORS_TYPE:
            continue
        n = pb(v)
        me = _txt(n, 2)
        for m in [_txt(n, 1)] + [x.decode("utf-8", "replace") for x in n.get(7, []) if isinstance(x, bytes)]:
            if m:
                owner.setdefault(m.lower(), me)
        me = owner.get(_txt(n, 1).lower(), me) or me
        reports.append((me, [pb(x) for x in n.get(4, []) if isinstance(x, bytes)]))
    pairs = {}
    for me, nbrs in reports:
        for l in nbrs:
            if _int(l, 4) > NEIGHBOR_MAX_AGE_MS:
                continue
            other = owner.get(_txt(l, 1).lower())
            if not other or not me or other == me:
                continue
            key = tuple(sorted((me, other)))
            kbps = _int(l, 5)
            old = pairs.get(key)
            pairs[key] = kbps if old is None else min(old, kbps)     # the slower direction is the honest figure
    links = [{"a": a, "b": b, "kbps": k} for (a, b), k in sorted(pairs.items())]
    return sorted(nodes, key=lambda x: x["name"].lower()), links


# ----------------------------------------------------------------- node status (no login)
def node_status(ip, timeout=2.0):
    """CPU temperature, load, memory and uptime from a node's own status call, or None."""
    req = urllib.request.Request("http://%s:8087%s" % (ip, STATUS_PATH), data=b"{}",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read(65536).decode("utf-8", "replace"))
    except Exception:
        return None
    sr = d.get("systemResources") or {}
    info = d.get("deviceInfo") or {}

    def num(v):
        try:
            return float(str(v).rstrip("s"))
        except (TypeError, ValueError):
            return None
    tot, used = num(sr.get("memoryTotalBytes")), num(sr.get("memoryUsedBytes"))
    cpu, load, up = num(sr.get("cpuTempCelsius")), num(sr.get("cpuLoadPercent")), num(sr.get("uptime"))
    return {"hostname": str(info.get("hostname", ""))[:64], "firmware": str(info.get("firmware", ""))[:64],
            "cpu_c": round(cpu, 1) if cpu is not None else None,
            "load_pct": round(load, 1) if load is not None else None,
            "mem_pct": round(100.0 * used / tot, 1) if tot and used is not None else None,
            "uptime": int(up) if up is not None else None}


def grade(mbps, good=3.0, poor=1.0):
    if mbps is None:
        return "NONE"
    return "GOOD" if mbps >= good else ("OK" if mbps >= poor else "POOR")


# ----------------------------------------------------------------- the watcher
class MeshWatch:
    """Background thread: every `every` seconds asks alfred who is in the mesh and how the links are,
    then asks each node for its health. snapshot() never blocks."""

    def __init__(self, iface, every=30.0, status_every=60.0, query=alfred_query, status=node_status):
        self.iface, self.every, self.status_every = iface, every, status_every
        self.query, self.status = query, status
        self.lock = threading.Lock()
        self.nodes = {}             # ip -> {name, ip, seen, health, health_t}
        self.links = []
        self.links_t = 0.0
        self.state = {"ok": False, "why": "starting", "servers": 0, "t": 0.0}

    def start(self):
        threading.Thread(target=self._run, name="meshwatch", daemon=True).start()
        return self

    def _run(self):
        while True:
            try:
                self.poll()
            except Exception as exc:            # never let the dashboard lose this thread
                with self.lock:
                    self.state = {"ok": False, "why": "error: %s" % exc, "servers": 0, "t": time.time()}
            time.sleep(self.every)

    def poll(self, now=None):
        data, servers, why = self.query(self.iface)
        now = time.time() if now is None else now
        nodes, links = parse_records(data) if data else ([], [])
        with self.lock:
            for n in nodes:
                cur = self.nodes.setdefault(n["ip"], {"health": None, "health_t": 0.0})
                cur.update(name=n["name"], ip=n["ip"], seen=now)
            for ip in [ip for ip, n in self.nodes.items() if now - n.get("seen", now) > FORGET_AFTER]:
                del self.nodes[ip]
            if servers:
                self.links, self.links_t = links, now
            self.state = {"ok": bool(servers), "servers": len(servers), "t": now,
                          "why": why or ("" if servers else "no alfred answer on %s" % self.iface)}
            due = [ip for ip, n in self.nodes.items() if now - n["health_t"] >= self.status_every]
        if due:
            with ThreadPoolExecutor(max_workers=min(8, len(due))) as ex:
                got = list(ex.map(self.status, due))
            with self.lock:
                for ip, h in zip(due, got):
                    if ip in self.nodes:
                        self.nodes[ip]["health"], self.nodes[ip]["health_t"] = h, now

    def snapshot(self):
        with self.lock:
            return {"state": dict(self.state),
                    "nodes": [dict(n, health=dict(n["health"]) if n.get("health") else None) for n in self.nodes.values()],
                    "links": [dict(l) for l in self.links],
                    "links_age": (time.time() - self.links_t) if self.links_t else None}
