#!/usr/bin/env bash
# install_sim.sh - the installer, upgrades, rollback, tidy and uninstall, end to end, with no internet and without
# touching this computer's real services: systemctl and sudo are replaced by small fakes (tests/fakesys_bin) that keep
# unit files under a temporary folder and really run the services. pip, curl, wget and apt are traps: any attempt to
# download something is recorded and fails the test.
# Run as a normal user (the installer refuses root):   bash tests/install_sim.sh [package dir] [work dir]
# The package dir must have its MANIFEST.sha256 (a built release).
set -uo pipefail
PKG="$(cd "${1:-$(dirname "$0")/..}" && pwd)"
T="${2:-/tmp/mmb_install_sim}"
[ "$(id -u)" = 0 ] && { echo "run as a normal user"; exit 2; }
[ -f "$PKG/MANIFEST.sha256" ] || { echo "$PKG has no MANIFEST.sha256 (build it first)"; exit 2; }
for p in "$T"/fakesys/state/*.pid; do [ -f "$p" ] && kill -- -"$(cat "$p")" 2>/dev/null; done
chmod -R u+w "$T" 2>/dev/null; rm -rf "$T"; mkdir -p "$T/home/Downloads" "$T/fakesys/bin"
cp "$PKG"/tests/fakesys_bin/* "$T/fakesys/bin/"
export FAKESYS="$T/fakesys" HOME="$T/home" PATH="$T/fakesys/bin:$PATH"
export MANET_MESH_BRIDGE_UNIT_DIR="$FAKESYS/etc/systemd/system" MANET_MESH_BRIDGE_BIN="$FAKESYS/usr/local/bin/manet_mesh_bridge"
mkdir -p "$MANET_MESH_BRIDGE_UNIT_DIR"
BASE="$HOME/manet_mesh_bridge"; CMD="$FAKESYS/usr/local/bin/manet_mesh_bridge"; NEWCMD="$FAKESYS/usr/local/bin/meshtoc"
PASS=0; FAILN=0
check() { if eval "$2"; then echo "PASS  $1"; PASS=$((PASS+1)); else echo "FAIL  $1"; FAILN=$((FAILN+1)); fi; }
sver() { python3 -c "import json;print(json.load(open('$HOME/bridge_status.json')).get('suite_version',''))" 2>/dev/null; }
fresh() { f="$HOME/bridge_status.json"; [ -f "$f" ] && [ $(( $(date +%s) - $(stat -c %Y "$f") )) -lt 6 ]; }
waitver() { for i in $(seq 1 60); do fresh && [ "$(sver)" = "$1" ] && return 0; sleep 0.5; done; return 1; }
stopall() { for p in "$FAKESYS"/state/*.pid; do [ -f "$p" ] && kill -- -"$(cat "$p")" 2>/dev/null; done; sleep 1; }
NEWV="$(awk 'NR==1{print $1}' "$PKG/VERSION")"
UPV="$NEWV.1"

# a small TAK server stand-in: accepts the bridge, answers its pings, records the rest
SRVPORT="$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1])')"
python3 - "$SRVPORT" "$T/server_rx.log" <<'PY' &
import socket, sys, threading
port, log = int(sys.argv[1]), sys.argv[2]
ls = socket.socket(); ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); ls.bind(("127.0.0.1", port)); ls.listen(20)
def serve(c):
    buf = b""
    while True:
        try:
            d = c.recv(65536)
        except OSError:
            return
        if not d:
            return
        buf += d
        while b"</event>" in buf:
            ev, buf = buf.split(b"</event>", 1); ev += b"</event>"
            if b'type="t-x-c-t"' in ev:
                c.sendall(ev)
            else:
                open(log, "ab").write(ev + b"\n")
while True:
    c, _ = ls.accept(); threading.Thread(target=serve, args=(c,), daemon=True).start()
PY
SRVPID=$!
# the upgrade package: same files, next version number
UP="$T/pkgs/MANET_MESH_BRIDGE_v$UPV"; mkdir -p "$T/pkgs"; cp -r "$PKG" "$UP"; chmod -R u+w "$UP"
find "$UP" -name __pycache__ -prune -exec rm -rf {} +     # left behind if the tests ran from this folder
echo "$UPV" > "$UP/VERSION"; ( cd "$UP" && find . -type f ! -name MANIFEST.sha256 | sed 's#^\./##' | LC_ALL=C sort | xargs sha256sum > MANIFEST.sha256 )
CUR="$T/pkgs/MANET_MESH_BRIDGE_v$NEWV"; cp -r "$PKG" "$CUR"; chmod -R u+w "$CUR"

echo "### A. --dry-run changes nothing"
( cd "$CUR" && bash install.sh --dry-run > "$T/dry.log" 2>&1 ); r=$?
check "dry run exits 0" '[ $r = 0 ]'
check "dry run created nothing (no folder, no services, no command)" '[ ! -e "$BASE" ] && [ -z "$(ls "$MANET_MESH_BRIDGE_UNIT_DIR")" ] && [ ! -e "$CMD" ]'

echo "### B. fresh install, offline"
( cd "$CUR" && bash install.sh --yes --callsign SIM-1 > "$T/install.log" 2>&1 ); r=$?
check "install exits 0" '[ $r = 0 ]'
check "bundled libraries were checked and load" 'grep -q "bundled libraries load: PyTAK 7.6" "$T/install.log"'
check "deck.ini written from the example, mode 600, callsign SIM-1" '[ "$(stat -c %a "$BASE/deck.ini")" = 600 ] && grep -q "^CALLSIGN = SIM-1" "$BASE/deck.ini"'
check "it says what to set next (server and certificate)" 'grep -q "set COT_URL" "$T/install.log"'
check "release folder is read-only" '! touch "$BASE/releases/$NEWV/x" 2>/dev/null'
check "bridge service runs python -S (bundled libraries only)" 'grep -q "^ExecStart=.*python3 -S .*/current/bridge/tak_mesh_bridge.py" "$MANET_MESH_BRIDGE_UNIT_DIR/manet-bridge.service"'
check "chat service runs python -S too (another protobuf on the computer can't break it)" 'grep -q "^ExecStart=.*python3 -S .*/current/chat/deck_chat.py" "$MANET_MESH_BRIDGE_UNIT_DIR/manet-chat.service"'
check "four services and the group are enabled" 'for u in manet-bridge.service manet-chat.service manet-dashboard.service manet-voice.service manet_mesh_bridge.target; do [ -f "$FAKESYS/state/$u.enabled" ] || exit 1; done'
check "voice service: in the group, restarts only on failure (VOICE = off stays stopped)" 'grep -q "^Wants=.*manet-voice.service" "$MANET_MESH_BRIDGE_UNIT_DIR/manet_mesh_bridge.target" && grep -q "^Restart=on-failure" "$MANET_MESH_BRIDGE_UNIT_DIR/manet-voice.service" && grep -q "^Restart=always" "$MANET_MESH_BRIDGE_UNIT_DIR/manet-chat.service"'
check "deck.ini has a [voice] section, off" 'grep -q "^\[voice\]" "$BASE/deck.ini" && grep -q "^VOICE = off" "$BASE/deck.ini"'
check "the command is installed" '[ -L "$CMD" ] && "$CMD" version | grep -qx "$NEWV"'
check "the meshtoc command is installed too, same program" '[ -L "$NEWCMD" ] && "$NEWCMD" version | grep -qx "$NEWV" && [ "$(readlink "$NEWCMD")" = "$(readlink "$CMD")" ]'
# a brand-new deck before the server is set: one TODO, not three failures
sleep 3; "$NEWCMD" check > "$T/check_fresh.log" 2>&1; rcf=$?
check "fresh install: check shows one TODO for the TAK server, and no FAIL lines" 'grep -q "\[TODO\] TAK server" "$T/check_fresh.log" && ! grep -q "\[FAIL\]" "$T/check_fresh.log"'
check "fresh install: check ends with what is left to do (and exits non-zero)" 'grep -q "One thing left to set up: your TAK server" "$T/check_fresh.log" && [ $rcf != 0 ]'
# point it at the stand-in server and the loopback "mesh", as a user would edit deck.ini
python3 - "$BASE/deck.ini" "$SRVPORT" <<'PY'
import re, sys
p, port = sys.argv[1], sys.argv[2]
s = open(p).read()
s = re.sub(r"^COT_URL = .*$", "COT_URL = tcp://127.0.0.1:%s" % port, s, count=1, flags=re.M)
s = re.sub(r"^MESH_ADDR = .*$", "MESH_ADDR = 127.0.0.1", s, count=1, flags=re.M)
s = s.replace("PORT = 8080", "PORT = 18080").replace("LISTEN = 127.0.0.1:8090", "LISTEN = 127.0.0.1:18090")
open(p, "w").write(s)
PY
"$CMD" restart > /dev/null 2>&1
check "the bridge runs this version and writes its status" 'waitver "$NEWV"'
sleep 3
check "the server got the deck's identity (SIM-1)" 'grep -q "callsign=\"SIM-1\"" "$T/server_rx.log"'
"$CMD" check > "$T/check1.log" 2>&1; r=$?
check "manet_mesh_bridge check passes (no FAIL)" '[ $r = 0 ] && ! grep -q "\[FAIL\]" "$T/check1.log"'
check "check says voice is off" 'grep -q "Service manet-voice .*off (VOICE = off" "$T/check1.log"'
check "check shows the bundled libraries and the server link" 'grep -q "Bundled libraries .*PyTAK 7.6.1" "$T/check1.log" && grep -q "Server link .*answers pings" "$T/check1.log"'
check "no download was attempted" '[ ! -s "$FAKESYS/network_attempts.log" ]'

echo "### C. same version again"
( cd "$CUR" && bash install.sh --yes > "$T/again.log" 2>&1 ); r=$?
check "reinstalling the same version does nothing" '[ $r = 0 ] && grep -q "Nothing to do" "$T/again.log"'

echo "### D. upgrade"
cp "$BASE/deck.ini" "$T/deckini_before"
echo zip > "$HOME/Downloads/MANET_MESH_BRIDGE_v$UPV.zip"; echo zip > "$HOME/Downloads/MANET_MESH_BRIDGE_v0.9.zip"; echo zip > "$HOME/Downloads/TAK_MESH_TOC_v0.8.zip"
mkdir -p "$HOME/MANET_MESH_BRIDGE_v0.9"
( cd "$UP" && bash install.sh --yes > "$T/upgrade.log" 2>&1 ); r=$?
check "upgrade exits 0, deck.ini unchanged" '[ $r = 0 ] && cmp -s "$T/deckini_before" "$BASE/deck.ini"'
check "running $UPV, previous -> $NEWV" 'waitver "$UPV" && [ "$(readlink "$BASE/previous")" = "releases/$NEWV" ]'
check "tidy removed the old zip and folder, kept this version's zip" '[ ! -e "$HOME/Downloads/MANET_MESH_BRIDGE_v0.9.zip" ] && [ ! -e "$HOME/Downloads/TAK_MESH_TOC_v0.8.zip" ] && [ ! -e "$HOME/MANET_MESH_BRIDGE_v0.9" ] && [ -f "$HOME/Downloads/MANET_MESH_BRIDGE_v$UPV.zip" ]'

echo "### E. rollback and forward"
"$CMD" rollback > /dev/null 2>&1
check "rollback runs $NEWV again" 'waitver "$NEWV"'
"$CMD" rollback > /dev/null 2>&1
check "and forward to $UPV" 'waitver "$UPV"'

echo "### F. downgrade refused"
( cd "$CUR" && bash install.sh --yes > "$T/down.log" 2>&1 ); r=$?
check "installing the older package stops (exit 3) and changes nothing" '[ $r = 3 ] && [ "$(readlink "$BASE/current")" = "releases/$UPV" ]'

echo "### G. a damaged package is refused"
BAD="$T/pkgs/bad"; cp -r "$UP" "$BAD"; chmod -R u+w "$BAD"; echo "# changed" >> "$BAD/bridge/tak_mesh_bridge.py"
( cd "$BAD" && bash install.sh --yes --force > "$T/bad.log" 2>&1 ); r=$?
check "install stops on a MANIFEST mismatch, nothing changed" '[ $r != 0 ] && grep -q "MANIFEST" "$T/bad.log" && [ "$(readlink "$BASE/current")" = "releases/$UPV" ]'

echo "### H. a program file changed after install is noticed"
chmod u+w "$BASE/releases/$UPV/chat" "$BASE/releases/$UPV/chat/deck_chat.py"; echo "# x" >> "$BASE/releases/$UPV/chat/deck_chat.py"
"$CMD" check > "$T/check2.log" 2>&1; r=$?
check "check reports the changed file (exit 1)" '[ $r = 1 ] && grep -q "Program files .*chat/deck_chat.py" "$T/check2.log"'
( cd "$UP" && bash install.sh --yes > "$T/repair.log" 2>&1 )
"$CMD" check > "$T/check3.log" 2>&1; r=$?
check "install.sh repairs it" '[ $r = 0 ] && grep -q "repairing" "$T/repair.log"'

echo "### I. uninstall"
( bash "$BASE/current/uninstall.sh" > "$T/uninstall.log" 2>&1 ); r=$?
stopall
check "services, group and command removed; deck.ini and programs kept" '[ $r = 0 ] && [ -z "$(ls "$MANET_MESH_BRIDGE_UNIT_DIR")" ] && [ ! -e "$CMD" ] && [ ! -e "$NEWCMD" ] && [ -f "$BASE/deck.ini" ]'
check "message logs kept" '[ -f "$HOME/bridge_status.json" ]'
( cd "$UP" && bash install.sh --yes > "$T/reinstall.log" 2>&1 )
check "install after uninstall keeps the old deck.ini" 'cmp -s "$T/deckini_before" "$BASE/deck.ini" && waitver "$UPV"'
( bash "$BASE/current/uninstall.sh" --purge > "$T/purge.log" 2>&1 )
stopall
check "--purge deletes ~/manet_mesh_bridge too" '[ ! -e "$BASE" ]'
check "no download was attempted at any point" '[ ! -s "$FAKESYS/network_attempts.log" ]'

kill $SRVPID 2>/dev/null
echo; echo "$PASS passed, $FAILN failed"
[ "$FAILN" = 0 ]
