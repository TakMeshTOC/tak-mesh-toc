"""deckptt.py - the PTT box plugged into the deck: talk on a talk group from the deck, hear it in the headset.

The PTT box is a USB sound card (CM108 family) whose PTT line is wired to the card's volume-down button, so Linux
sees two things for one USB device: an input device that sends KEY_VOLUMEDOWN, and an ALSA sound card. Both are
found by their USB path, so a second USB sound card on the deck is never mixed up with the box.

  press    the button is grabbed (the desktop never sees it, so the deck's volume never changes); the boom mic is
           sent to the chosen talk group: the mesh, and its Mumble channel when that talk group is bridged
  headset  plays the chosen talk group all the time, like a radio (never the deck's own voice)
  plug     found within a few seconds of plugging in; unplugging just shows NOT PLUGGED IN

Audio goes through arecord/aplay (alsa-utils): 16-bit 48 kHz mono, 20 ms frames, like the rest of manet-voice.
Standard library only.
"""
import errno
import fcntl
import json
import logging
import os
import queue
import re
import select
import shlex
import struct
import subprocess
import threading
import time

log = logging.getLogger("manet_voice")

EV_KEY = 1
KEY_VOLUMEDOWN = 114
EVIOCGRAB = 0x40044590                     # _IOW('E', 0x90, int)
EVENT = struct.Struct("llHHi")             # struct input_event: timeval, type, code, value
LONG_BITS = struct.calcsize("l") * 8       # the KEY= bitmap in /proc/bus/input/devices is in kernel longs
FRAME_BYTES = 960 * 2                      # 20 ms of 16-bit 48 kHz mono
DECK_NAME = "DECK (PTT box)"
CAPTURE = "arecord -q -D {dev} -t raw -f S16_LE -r 48000 -c 1"
PLAY = "aplay -q -D {dev} -t raw -f S16_LE -r 48000 -c 1"


def parse_input_devices(text):
    """/proc/bus/input/devices -> [{"name", "phys", "event", "keys": set of key codes}]"""
    out = []
    for block in text.split("\n\n"):
        d = {"name": "", "phys": "", "event": "", "keys": set()}
        for line in block.splitlines():
            if line.startswith("N: Name="):
                d["name"] = line[8:].strip().strip('"')
            elif line.startswith("P: Phys="):
                d["phys"] = line[8:].strip()
            elif line.startswith("H: Handlers="):
                m = re.search(r"\bevent(\d+)\b", line)
                d["event"] = "event" + m.group(1) if m else ""
            elif line.startswith("B: KEY="):
                words = line[7:].split()
                bits = LONG_BITS                                  # one word per kernel "long", lowest keys last
                for i, w in enumerate(reversed(words)):
                    try:
                        v = int(w, 16)
                    except ValueError:
                        continue
                    for b in range(bits):
                        if v >> b & 1:
                            d["keys"].add(i * bits + b)
        if d["event"]:
            out.append(d)
    return out


def parse_cards(text):
    """/proc/asound/cards -> [{"index", "id", "name", "usb"}] (usb = "usb-0000:01:00.0-1.3" when on USB)"""
    out, lines = [], text.splitlines()
    for i, line in enumerate(lines):
        m = re.match(r"\s*(\d+)\s+\[(\S+)\s*\]:\s*(.*)", line)
        if not m:
            continue
        more = lines[i + 1].strip() if i + 1 < len(lines) else ""
        u = re.search(r"\bat (usb-\S+?),", more + ",")
        out.append({"index": int(m.group(1)), "id": m.group(2), "name": (m.group(3) + " " + more).strip(),
                    "usb": u.group(1) if u else ""})
    return out


def find_box(input_text, cards_text, match, key=KEY_VOLUMEDOWN):
    """The PTT box: an input device with the PTT key whose name matches, plus the sound card on the same USB port.
    -> (event name, ALSA card id, input name) or None"""
    words = [w.strip().lower() for w in match.split(",") if w.strip()]
    cards = parse_cards(cards_text)
    for d in parse_input_devices(input_text):
        if key not in d["keys"] or not any(w in d["name"].lower() for w in words):
            continue
        usb = d["phys"].split("/")[0]
        card = next((c for c in cards if usb and c["usb"] == usb), None)
        if card is None:                     # no USB path to go by: a card with the same name will do
            card = next((c for c in cards if d["name"].lower() in c["name"].lower()), None)
        if card:
            return d["event"], card["id"], d["name"]
    return None


class DeckPTT:
    """One PTT box on the deck. gw: the Gateway (talk groups, status). Runs in its own thread."""

    def __init__(self, gw, cfg, proc_input="/proc/bus/input/devices", proc_cards="/proc/asound/cards",
                 devdir="/dev/input", grab=True):
        self.gw, self.cfg = gw, cfg
        self.proc_input, self.proc_cards, self.devdir, self.grab = proc_input, proc_cards, devdir, grab
        self.match = cfg.get("DECK_PTT_MATCH", "C-Media,USB PnP Sound,USB Audio Device")
        self.capture_cmd = cfg.get("DECK_PTT_CAPTURE", CAPTURE)
        self.play_cmd = cfg.get("DECK_PTT_PLAY", PLAY)
        self.state_file = os.path.expanduser(cfg.get("DECK_PTT_STATE_FILE", "~/voice_deckptt.json"))
        self.lock = threading.Lock()
        self.present = False
        self.card = self.event = self.box_name = ""
        self.error = ""                      # shown in Ops; "" when all is well
        self.keyed = False
        self.talking = False                 # keyed and holding the floor
        self.busy = ""                       # keyed but someone else holds the floor
        self.presses = 0
        self.monitor_q = queue.Queue(maxsize=25)    # 0.5 s of headset audio at most: never a backlog
        self.mon_group = None
        self.tg = self._load_tg()
        self.stop = threading.Event()
        self._fd = None
        self._cap = self._play = None

    # ---- the chosen talk group, remembered across restarts
    def _load_tg(self):
        names = [g.name for g in self.gw.groups]
        try:
            with open(self.state_file) as f:
                tg = json.load(f).get("tg")
            if tg in names:
                return tg
        except (OSError, ValueError, AttributeError):
            pass
        want = (self.cfg.get("DECK_PTT_TG") or "").strip()
        return want if want in names else (names[0] if names else "")

    def group(self):
        return next((g for g in self.gw.groups if g.name == self.tg), None)

    def set_tg(self, name):
        g = next((x for x in self.gw.groups if x.name.lower() == (name or "").lower()), None)
        if g is None:
            return None
        with self.lock:
            if self.talking:
                old = self.group()
                if old:
                    old.on_deck(None, last=True)
                self.talking = False
            self.tg = g.name
            self._attach_monitor()
        try:
            tmp = self.state_file + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"tg": self.tg}, f)
            os.replace(tmp, self.state_file)
        except OSError as e:
            log.warning("PTT box: could not save the talk group to %s: %s", self.state_file, e)
        log.info("PTT box: talks on %s", self.tg)
        return g

    # ---- headset: the chosen talk group, all the time
    def _attach_monitor(self):
        g = self.group() if self.present else None
        if self.mon_group is g:
            return
        if self.mon_group is not None:
            try:
                self.mon_group.listeners.remove(self.monitor_q)
            except ValueError:
                pass
        self.mon_group = g
        if g is not None:
            g.listeners.append(self.monitor_q)

    def _player(self, proc):
        while not self.stop.is_set() and proc.poll() is None:
            try:
                pcm = self.monitor_q.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                proc.stdin.write(pcm)
                proc.stdin.flush()
            except (OSError, ValueError):
                return

    # ---- plug / unplug
    def _scan(self):
        try:
            with open(self.proc_input) as f:
                inp = f.read()
            with open(self.proc_cards) as f:
                cards = f.read()
        except OSError:
            return None
        return find_box(inp, cards, self.match)

    def _open(self, found):
        event, card, name = found
        path = os.path.join(self.devdir, event)
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        except PermissionError:
            self._fail("no permission to read the PTT button (%s): run install.sh again, then meshtoc restart" % path)
            return False
        except OSError as e:
            self._fail("cannot open the PTT button %s: %s" % (path, e.strerror or e))
            return False
        if self.grab:
            try:
                fcntl.ioctl(fd, EVIOCGRAB, 1)
            except OSError as e:
                log.warning("PTT box: could not take the button over (%s); the desktop may also see it as volume-down", e)
        dev = "plughw:CARD=%s,DEV=0" % card
        try:
            cap = subprocess.Popen(shlex.split(self.capture_cmd.format(dev=dev)), stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, bufsize=0)
            play = subprocess.Popen(shlex.split(self.play_cmd.format(dev=dev)), stdin=subprocess.PIPE,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, bufsize=0)
        except OSError as e:
            os.close(fd)
            self._fail("cannot start the sound card (%s): sudo apt install alsa-utils" % (e.strerror or e))
            return False
        with self.lock:
            self._fd, self._cap, self._play = fd, cap, play
            self.present, self.card, self.event, self.box_name, self.error = True, card, event, name, ""
            self._attach_monitor()
        threading.Thread(target=self._player, args=(play,), daemon=True).start()
        log.info("PTT box plugged in: %s (button %s, sound card %s); talks on %s", name, event, card, self.tg)
        return True

    def _fail(self, msg):
        if msg != self.error:
            log.warning("PTT box: %s", msg)
        self.error = msg

    def _close(self, why=""):
        with self.lock:
            if self.talking:
                g = self.group()
                if g:
                    g.on_deck(None, last=True)
            self.keyed = self.talking = False
            self.busy = ""
            fd, cap, play = self._fd, self._cap, self._play
            self._fd = self._cap = self._play = None
            was = self.present
            self.present = False
            self._attach_monitor()
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        for p in (cap, play):
            if p is None:
                continue
            try:
                p.kill()
                p.wait(2)
            except (OSError, subprocess.TimeoutExpired):
                pass
        if was:
            log.info("PTT box unplugged%s", (" (%s)" % why) if why else "")

    # ---- button + mic
    def _on_key(self, value):
        if value == 1 and not self.keyed:
            self.keyed = True
            self.presses += 1
        elif value == 0 and self.keyed:
            self.keyed = False
            with self.lock:
                g = self.group()
                if self.talking and g:
                    g.on_deck(None, last=True)
                self.talking, self.busy = False, ""

    def _on_frame(self, frame):
        if not self.keyed:
            return
        with self.lock:
            g = self.group()
            if g is None:
                return
            ok = g.on_deck(frame)
            self.talking = ok
            self.busy = "" if ok else (g.floor_name or "someone")

    def _loop_once(self, timeout=0.2):
        fd, cap = self._fd, self._cap
        r, _, _ = select.select([fd, cap.stdout], [], [], timeout)
        if fd in r:
            try:
                data = os.read(fd, EVENT.size * 32)
            except BlockingIOError:
                data = b""
            except OSError as e:
                if e.errno in (errno.ENODEV, errno.EIO):
                    return "unplugged"
                raise
            if not data:
                if not os.path.exists(os.path.join(self.devdir, self.event)):
                    return "unplugged"
                time.sleep(0.05)                        # end of file but still there: never spin
            for i in range(0, len(data) - EVENT.size + 1, EVENT.size):
                _, _, typ, code, value = EVENT.unpack_from(data, i)
                if typ == EV_KEY and code == KEY_VOLUMEDOWN:
                    self._on_key(value)
        if cap.stdout in r:
            self._buf = getattr(self, "_buf", b"") + (os.read(cap.stdout.fileno(), FRAME_BYTES * 4) or b"")
            if cap.poll() is not None and not self._buf:
                err = (cap.stderr.read() or b"").decode("utf-8", "replace").strip().splitlines()
                return "sound card stopped" + (": " + err[-1][:160] if err else "")
            while len(self._buf) >= FRAME_BYTES:
                frame, self._buf = self._buf[:FRAME_BYTES], self._buf[FRAME_BYTES:]
                self._on_frame(frame)
        elif cap.poll() is not None:
            err = (cap.stderr.read() or b"").decode("utf-8", "replace").strip().splitlines()
            return "sound card stopped" + (": " + err[-1][:160] if err else "")
        return None

    def run(self):
        while not self.stop.is_set():
            if not self.present:
                found = self._scan()
                if not found:
                    if self.error and not self.error.startswith(("no permission", "cannot")):
                        self.error = ""
                    self.stop.wait(2)
                    continue
                if not self._open(found):
                    self.stop.wait(5)
                    continue
                self._buf = b""
            try:
                why = self._loop_once()
            except Exception as e:                       # never let the box take the voice service down
                why = "error: %s" % e
            if why:
                self._close(why)
                if why.startswith(("sound card", "error")):
                    self._fail(why)
                self.stop.wait(2)
        self._close()

    def close(self):
        self.stop.set()

    def status(self):
        return {"enabled": True, "present": self.present, "tg": self.tg, "talking": self.talking, "keyed": self.keyed,
                "busy": self.busy, "error": self.error, "card": self.card, "name": self.box_name, "presses": self.presses}
