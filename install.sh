#!/usr/bin/env bash
# install.sh - install or upgrade TAK MESH TOC (bridge + Chat + Ops dashboard + voice) on the deck.
#
#   bash install.sh --dry-run        show what it would do, change nothing
#   bash install.sh                  install, or upgrade (your deck.ini is kept)
#   bash install.sh --yes            answer yes to every question
#   bash install.sh --callsign DECK  callsign for a NEW install (an existing deck.ini keeps its own)
#   bash install.sh --icon           desktop icons for TAK MESH TOC Ops and TAK MESH TOC Chat
#   bash install.sh --kiosk          open the dashboard full-screen at login
#   bash install.sh --force          reinstall the same version, or install an OLDER one on purpose
#
# Needs: Linux with systemd and python3 (3.9+). No internet: PyTAK, takproto and protobuf are bundled (vendor/).
# Layout:  ~/manet_mesh_bridge/deck.ini             the one config file (kept on upgrades, mode 600)
#          ~/manet_mesh_bridge/releases/<version>/   the programs, read-only
#          ~/manet_mesh_bridge/current -> releases/<version>      ~/manet_mesh_bridge/previous -> the one before
# Services: manet-bridge, manet-chat, manet-dashboard, manet-voice, grouped by manet_mesh_bridge.target.
# manet-voice only runs when VOICE = on in [voice]; otherwise it starts, sees that, and stops.
# It never touches Wi-Fi, Tailscale, MediaMTX, gpsd/chrony, the mesh nodes or the TAK server.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE="${MANET_MESH_BRIDGE_HOME:-$HOME/manet_mesh_bridge}"
UNIT_DIR="${MANET_MESH_BRIDGE_UNIT_DIR:-/etc/systemd/system}"
BIN_LINK="${MANET_MESH_BRIDGE_BIN:-/usr/local/bin/manet_mesh_bridge}"
CMD_LINK="$(dirname "$BIN_LINK")/meshtoc"          # the command's name since 3.0; manet_mesh_bridge keeps working
DRY=0; YES=0; ICON=0; KIOSK=0; FORCE=0; CALLSIGN=""
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1 ;;
    --yes|-y) YES=1 ;;
    --icon) ICON=1 ;;
    --kiosk) KIOSK=1 ;;
    --force) FORCE=1 ;;
    --callsign) shift; CALLSIGN="${1:-}" ;;
    -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
    *) echo "unknown option: $1 (try --help)"; exit 2 ;;
  esac
  shift
done
[ "$(id -u)" = "0" ] && { echo "Run this as your normal user (not root). It uses sudo only where needed."; exit 1; }
if [ -n "$CALLSIGN" ] && ! printf '%s' "$CALLSIGN" | grep -Eq '^[A-Za-z0-9_-]{1,20}$'; then
  echo "Callsign: letters, numbers, - and _ only, up to 20 characters"; exit 2
fi
SUDO=""; command -v sudo >/dev/null 2>&1 && SUDO="sudo"

say()  { printf '\n== %s\n' "$*"; }
note() { printf '   %s\n' "$*"; }
act()  { if [ "$DRY" = 1 ]; then printf '   [dry-run] %s\n' "$*"; else printf '   > %s\n' "$*"; "$@"; fi; }

NEW="$(awk 'NR==1{print $1}' "$SRC/VERSION")"
REL="$BASE/releases/$NEW"

say "TAK MESH TOC $NEW: checking this computer"
command -v systemctl >/dev/null || { echo "   systemd is required"; exit 1; }
PY="$(command -v python3 || true)"
[ -n "$PY" ] || { echo "   python3 is required (sudo apt install python3)"; exit 1; }
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' || { echo "   python3 is too old: 3.9 or newer is needed"; exit 1; }
( cd "$SRC" && sha256sum --quiet -c MANIFEST.sha256 ) || { echo "   This package's files don't match its MANIFEST (bad download?). Stopping."; exit 1; }
note "package files verified (MANIFEST.sha256)"
note "python: $PY ($("$PY" -c 'import platform; print(platform.python_version())'))"
# The bridge runs with "python3 -S": only the standard library and vendor/, never other copies of protobuf or
# cryptography installed on this computer (mixing versions breaks imports). Check exactly that here.
if PYTHONPATH="$SRC/vendor" "$PY" -S -W ignore -c 'import pytak, takproto' >/dev/null 2>&1; then
  note "bundled libraries load: $(PYTHONPATH="$SRC/vendor" "$PY" -S -W ignore -c 'from importlib.metadata import version as v; print("PyTAK", v("pytak"), "| takproto", v("takproto"), "| protobuf", v("protobuf"))' 2>/dev/null)"
else
  echo "   The bundled libraries (vendor/) don't load with this python. Stopping."; exit 1
fi
if command -v ss >/dev/null && ! systemctl is-active --quiet manet-bridge 2>/dev/null; then
  for p in 4242 4243 8080 8090; do
    if ss -ltnH "sport = :$p" 2>/dev/null | grep -q .; then
      note "NOTE: something already listens on TCP port $p (another TAK bridge or chat program?): see  ss -ltnp | grep :$p"
    fi
  done
fi

# ---------------------------------------------------------------- what is installed now
CUR=""
[ -f "$BASE/current/VERSION" ] && CUR="$(awk 'NR==1{print $1}' "$BASE/current/VERSION")"
if [ -n "$CUR" ]; then
  note "installed version: $CUR"
  if [ "$CUR" = "$NEW" ] && [ "$FORCE" = 0 ]; then
    UNITS_OK=1
    for f in manet-bridge.service manet-chat.service manet-dashboard.service manet-voice.service manet_mesh_bridge.target; do
      [ -f "$UNIT_DIR/$f" ] || UNITS_OK=0
    done
    if ( cd "$BASE/current" && sha256sum --quiet -c MANIFEST.sha256 ) >/dev/null 2>&1 && [ "$UNITS_OK" = 1 ] && [ -e "$BIN_LINK" ] && [ -e "$CMD_LINK" ]; then
      note "$NEW is already installed and its files are intact. Nothing to do (use --force to reinstall)."
      exit 0
    fi
    note "$NEW is installed but its files, services or command were changed or removed: repairing"
  elif [ "$(printf '%s\n%s\n' "$CUR" "$NEW" | sort -V | head -1)" = "$NEW" ] && [ "$CUR" != "$NEW" ] && [ "$FORCE" = 0 ]; then
    echo
    echo "   STOP: this package is $NEW but $CUR (newer) is installed."
    echo "   Installing it would downgrade the bridge, chat and dashboard. Nothing was changed."
    echo "   (To go back on purpose:  meshtoc rollback   or   bash install.sh --force)"
    exit 3
  fi
fi

# ---------------------------------------------------------------- 1. programs
say "1/6 Programs -> $REL"
if [ -d "$REL" ] && [ "$DRY" != 1 ]; then chmod -R u+w "$REL"; rm -rf "$REL"; fi
act mkdir -p "$REL"
if [ "$DRY" = 1 ]; then note "[dry-run] would copy the programs, vendor/, server/, docs and tests"
else
  ( cd "$SRC" && tar cf - VERSION MANIFEST.sha256 LICENSE THIRD_PARTY_NOTICES.md README.md CHANGES.md install.sh uninstall.sh \
      common bridge chat dashboard voice tools config vendor server tests ) | ( cd "$REL" && tar xf - )
  find "$REL" -name __pycache__ -prune -exec rm -rf {} +
  ( cd "$REL" && sha256sum --quiet -c MANIFEST.sha256 ) || { echo "   copy check failed"; exit 1; }
  chmod 755 "$REL/tools/manet_mesh_bridge.py" "$REL/bridge/tak_mesh_bridge.py" "$REL/chat/deck_chat.py" "$REL/dashboard/dashboard_server.py" "$REL/voice/manet_voice.py" "$REL/voice/make_channel.py"
  note "copied and verified $(wc -l < "$REL/MANIFEST.sha256") files"
fi

# ---------------------------------------------------------------- 2. config
say "2/6 Config -> $BASE/deck.ini"
MESHIF="eth0"
MESHIP="$(ip -4 -o addr show dev "$MESHIF" 2>/dev/null | sed -n 's/.*inet \([0-9.]*\).*/\1/p' | head -1 || true)"
if [ -f "$BASE/deck.ini" ]; then
  note "keeping your existing deck.ini"
  [ -n "$CALLSIGN" ] && note "(--callsign ignored because deck.ini exists; set CALLSIGN in [deck])"
elif [ "$DRY" = 1 ]; then
  note "[dry-run] would write deck.ini from the example (CALLSIGN=${CALLSIGN:-DECK}, MESH_ADDR=auto, now ${MESHIP:-none} on $MESHIF)"
else
  mkdir -p "$BASE"
  "$PY" - "$SRC/config/deck.ini.example" "$BASE/deck.ini" "${CALLSIGN:-DECK}" "${MESHIP:-}" <<'PY'
import os, re, sys
src, dst, cs, ip = sys.argv[1:5]
s = open(src).read()
s = re.sub(r"^CALLSIGN = .*$", "CALLSIGN = " + cs, s, count=1, flags=re.M)
fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
os.write(fd, s.encode()); os.close(fd)
print("   wrote %s (callsign %s, mesh address auto: %s)" % (dst, cs, ("now " + ip) if ip else "none yet, plug in the node"))
PY
  note "NEXT: open $BASE/deck.ini and set COT_URL (your TAK server) and the certificate lines in [bridge]"
fi
[ -f "$BASE/deck.ini" ] && act chmod 600 "$BASE/deck.ini"

# ---------------------------------------------------------------- 3. services
say "3/6 Services (manet-bridge, manet-chat, manet-dashboard, manet-voice; grouped by manet_mesh_bridge.target)"
U="$(id -un)"; CURL="$BASE/current"
mkunit() {   # mkunit NAME DESCRIPTION EXECSTART AFTER RESTARTSEC EXTRA_ENV [RESTART]
  cat <<EOF
# TAK MESH TOC (written by install.sh; removed by uninstall.sh)
[Unit]
Description=$2
After=network-online.target${4:+ $4}
Wants=network-online.target
PartOf=manet_mesh_bridge.target

[Service]
Type=simple
User=$U
WorkingDirectory=$HOME
Environment=PYTHONUNBUFFERED=1${6:+
Environment=$6}
ExecStart=$3
Restart=${7:-always}
RestartSec=$5
# A ceiling per service: if one ever leaks or is flooded, systemd restarts only that one and the deck stays usable.
# (Needs the kernel's memory accounting; where it is off, systemd ignores the line.)
MemoryMax=400M

[Install]
WantedBy=multi-user.target manet_mesh_bridge.target
EOF
}
TMPD="$(mktemp -d)"
mkunit manet-bridge "TAK MESH TOC: mesh <-> TAK server bridge" "$PY -S $CURL/bridge/tak_mesh_bridge.py $BASE/deck.ini" "tailscaled.service" 5 "" > "$TMPD/manet-bridge.service"
mkunit manet-chat "TAK MESH TOC: Chat (ATAK chat on the deck's screen)" "$PY $CURL/chat/deck_chat.py --config $BASE/deck.ini" "" 3 "" > "$TMPD/manet-chat.service"
mkunit manet-dashboard "TAK MESH TOC: Ops dashboard" "$PY $CURL/dashboard/dashboard_server.py --config $BASE/deck.ini" "" 3 "" > "$TMPD/manet-dashboard.service"
# voice: on-failure, so VOICE = off (a clean exit) leaves it stopped instead of restarting it every few seconds
mkunit manet-voice "TAK MESH TOC: voice (OpenMANET talk groups <-> Mumble)" "$PY $CURL/voice/manet_voice.py --config $BASE/deck.ini" "tailscaled.service" 5 "" on-failure > "$TMPD/manet-voice.service"
cat > "$TMPD/manet_mesh_bridge.target" <<EOF
# TAK MESH TOC: starts, stops and restarts the bridge, Chat, the Ops dashboard and voice together.
[Unit]
Description=TAK MESH TOC (bridge, Chat, Ops dashboard, voice)
Wants=manet-bridge.service manet-chat.service manet-dashboard.service manet-voice.service

[Install]
WantedBy=multi-user.target
EOF
for f in manet-bridge.service manet-chat.service manet-dashboard.service manet-voice.service manet_mesh_bridge.target; do
  if [ -f "$UNIT_DIR/$f" ] && ! cmp -s "$TMPD/$f" "$UNIT_DIR/$f"; then
    note "changes to $f:"
    diff "$UNIT_DIR/$f" "$TMPD/$f" | grep -E '^[<>] (ExecStart|After|User|WorkingDirectory|Environment|PartOf|WantedBy)' | sed 's/^/     /' || true
  fi
  act $SUDO install -m 644 "$TMPD/$f" "$UNIT_DIR/$f"
done
rm -rf "$TMPD"
# voice needs the system's libopus and, if UFW is on, the OpenMANET talk group ports open on the mesh side
if ! { ldconfig -p 2>/dev/null | grep -q 'libopus\.so\.0'; }; then note "voice: libopus not found. Before turning VOICE on: sudo apt install libopus0"; fi
# The firewall rule keeps its pre-3.0 comment ("MANET_MESH_BRIDGE voice"): uninstall.sh finds the rule by it on decks
# that were set up before the rename.
if command -v ufw >/dev/null 2>&1 && $SUDO ufw status 2>/dev/null | grep -q '^Status: active'; then
  if $SUDO ufw status 2>/dev/null | grep -q '38801:38810/udp on '"$MESHIF"; then note "voice: UFW already allows the talk group ports on $MESHIF"
  else act $SUDO ufw allow in on "$MESHIF" proto udp to any port 38801:38810 comment "MANET_MESH_BRIDGE voice (OpenMANET talk groups)"; fi
fi
# Ops lists the mesh's nodes from OpenMANET's own gossip (alfred), which only answers an EUI-64 IPv6 link-local address.
# Most decks use NetworkManager's "stable-privacy" kind, so switch the mesh port's connection to eui64 (once).
if command -v nmcli >/dev/null 2>&1; then
  MCONN="$(nmcli -t -f NAME,DEVICE connection show --active 2>/dev/null | awk -F: -v d="$MESHIF" '$2==d{print $1; exit}')"
  if [ -z "$MCONN" ]; then note "mesh: no active NetworkManager connection on $MESHIF; Ops lists only the nodes in NODE_IP until it has one"
  elif [ "$(nmcli -g ipv6.addr-gen-mode connection show "$MCONN" 2>/dev/null)" = "eui64" ]; then note "mesh: $MESHIF already has the IPv6 address the node list needs"
  elif nmcli -g ipv6.method connection show "$MCONN" 2>/dev/null | grep -qE '^(disabled|ignore)$'; then
    note "mesh: IPv6 is off on '$MCONN', so Ops lists only the nodes in NODE_IP (turn IPv6 on to see every node)"
  else
    note "mesh: giving $MESHIF the IPv6 address OpenMANET's node list answers (the port blinks once)"
    act $SUDO nmcli connection modify "$MCONN" ipv6.addr-gen-mode eui64
    act $SUDO nmcli connection up "$MCONN"
  fi
fi

# ---------------------------------------------------------------- 4. switch to the new version
say "4/6 Switch to $NEW and start"
if [ "$DRY" != 1 ]; then
  if [ -L "$BASE/current" ] && [ "$(readlink "$BASE/current")" != "releases/$NEW" ]; then ln -sfn "$(readlink "$BASE/current")" "$BASE/previous"; fi
  ln -sfn "releases/$NEW" "$BASE/current.tmp" && mv -Tf "$BASE/current.tmp" "$BASE/current"
  find "$REL" -type f -exec chmod a-w {} + ; find "$REL" -type d -exec chmod 555 {} +
else
  note "[dry-run] would point $BASE/current at releases/$NEW and make that folder read-only"
fi
act $SUDO systemctl daemon-reload
act $SUDO systemctl enable manet_mesh_bridge.target manet-bridge manet-chat manet-dashboard manet-voice
act $SUDO systemctl restart manet_mesh_bridge.target
note "started (relaying pauses for a few seconds during an upgrade)"

# ---------------------------------------------------------------- 5. the command
say "5/6 The meshtoc command"
[ -d "$(dirname "$BIN_LINK")" ] || act $SUDO mkdir -p "$(dirname "$BIN_LINK")"
act $SUDO ln -sfn "$CURL/tools/manet_mesh_bridge.py" "$BIN_LINK"
act $SUDO ln -sfn "$CURL/tools/manet_mesh_bridge.py" "$CMD_LINK"
note "try:  meshtoc status    meshtoc check    meshtoc help     (the old name manet_mesh_bridge works too)"

# ---------------------------------------------------------------- 6. icons and kiosk
say "6/6 Desktop icons, full-screen, tidy"
DESK="$(xdg-user-dir DESKTOP 2>/dev/null || true)"; [ -n "$DESK" ] && [ "$DESK" != "$HOME" ] || DESK="$HOME/Desktop"
APPS="$HOME/.local/share/applications"
BROWSER=""; for b in chromium-browser chromium; do command -v "$b" >/dev/null 2>&1 && { BROWSER="$b"; break; }; done
[ -z "$BROWSER" ] && BROWSER="chromium-browser"
icon() {   # icon FILE NAME COMMENT URL ICONPATH
  [ "$DRY" = 1 ] && { note "[dry-run] would write $APPS/$1 and $DESK/$1"; return; }
  mkdir -p "$APPS" "$DESK"
  printf '[Desktop Entry]\nType=Application\nName=%s\nComment=%s\nExec=%s --app=%s\nIcon=%s\nTerminal=false\nCategories=Network;\n' \
    "$2" "$3" "$BROWSER" "$4" "$5" > "$APPS/$1"
  if [ "$ICON" = 1 ] || [ -e "$DESK/$1" ]; then     # a desktop icon you deleted stays deleted (the menu entry stays)
    install -m 755 "$APPS/$1" "$DESK/$1"; note "icon: $2 (menu and desktop)"
  else
    note "icon: $2 (menu only)"
  fi
}
if [ "$ICON" = 1 ] || [ -f "$APPS/manet-dashboard-app.desktop" ]; then icon manet-dashboard-app.desktop "TAK MESH TOC Ops" "TAK MESH TOC status dashboard" "http://127.0.0.1:8080" "$CURL/dashboard/icon.svg"; fi
if [ "$ICON" = 1 ] || [ -f "$APPS/manet-chat.desktop" ]; then icon manet-chat.desktop "TAK MESH TOC Chat" "ATAK chat and voice from the deck" "http://127.0.0.1:8090" "$CURL/chat/icon.svg"; fi
if [ "$KIOSK" = 1 ] || [ -f "$HOME/.config/autostart/manet-dashboard.desktop" ]; then
  KS="$BASE/start_kiosk.sh"
  if [ "$DRY" = 1 ]; then note "[dry-run] would write $KS and point the login autostart at it"
  else
    cat > "$KS" <<EOF
#!/bin/sh
# Opens the dashboard full-screen once the dashboard service answers. (TAK MESH TOC)
for i in \$(seq 1 60); do
  python3 -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8080/healthz', timeout=1)" >/dev/null 2>&1 && break
  sleep 2
done
exec $BROWSER --kiosk --noerrdialogs --disable-infobars --disable-session-crashed-bubble --no-first-run \\
  --user-data-dir="\$HOME/.config/deck-kiosk" --autoplay-policy=no-user-gesture-required "http://127.0.0.1:8080/?kiosk=1"
EOF
    chmod 755 "$KS"
    mkdir -p "$HOME/.config/autostart"
    printf '[Desktop Entry]\nType=Application\nName=TAK MESH TOC Ops\nExec=%s\nX-GNOME-Autostart-enabled=true\n' "$KS" > "$HOME/.config/autostart/manet-dashboard.desktop"
    if [ -f "$HOME/.config/labwc/autostart" ] || [ -d "$HOME/.config/labwc" ]; then
      mkdir -p "$HOME/.config/labwc"; touch "$HOME/.config/labwc/autostart"
      sed -i '\#manet_mesh_bridge/start_kiosk.sh#d' "$HOME/.config/labwc/autostart"
      echo "$KS &" >> "$HOME/.config/labwc/autostart"
    fi
    note "full-screen dashboard at login: $KS"
  fi
fi
TARGS=(--keep "$NEW"); [ "$YES" = 1 ] && TARGS+=(--yes); [ "$DRY" = 1 ] && TARGS+=(--dry-run)
MANET_MESH_BRIDGE_HOME="$BASE" "$PY" "$SRC/tools/manet_mesh_bridge.py" tidy "${TARGS[@]}" || note "tidy had a problem; run  meshtoc tidy  later"

say "Done: TAK MESH TOC $NEW"
note "config:   $BASE/deck.ini      (after editing: meshtoc restart)"
note "check:    meshtoc check      (wait ~10 s for the bridge to connect)"
note "undo:     meshtoc rollback   (previous version)   or   bash $CURL/uninstall.sh   (remove)"
[ "$DRY" = 1 ] && note "That was a dry run: nothing was changed."
exit 0
