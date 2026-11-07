#!/usr/bin/env bash
# uninstall.sh - remove TAK MESH TOC.
#
#   bash uninstall.sh --dry-run    show what it would do
#   bash uninstall.sh              stop and remove the services, the command, icons and full-screen autostart
#   bash uninstall.sh --purge      also delete ~/manet_mesh_bridge (deck.ini and the programs)
# Message logs (~/deck_chat_log.jsonl, ~/dashboard_*.jsonl, ~/bridge_*.json*) are always kept.
set -euo pipefail
BASE="${MANET_MESH_BRIDGE_HOME:-$HOME/manet_mesh_bridge}"
UNIT_DIR="${MANET_MESH_BRIDGE_UNIT_DIR:-/etc/systemd/system}"
BIN_LINK="${MANET_MESH_BRIDGE_BIN:-/usr/local/bin/manet_mesh_bridge}"
CMD_LINK="$(dirname "$BIN_LINK")/meshtoc"
DRY=0; PURGE=0
for a in "$@"; do case "$a" in --dry-run) DRY=1 ;; --purge) PURGE=1 ;; -h|--help) sed -n '2,7p' "$0"; exit 0 ;; *) echo "unknown option: $a"; exit 2 ;; esac; done
[ "$(id -u)" = "0" ] && { echo "Run as your normal user."; exit 1; }
SUDO=""; command -v sudo >/dev/null 2>&1 && SUDO="sudo"
act() { if [ "$DRY" = 1 ]; then printf '   [dry-run] %s\n' "$*"; else printf '   > %s\n' "$*"; "$@" || true; fi; }
DESK="$(xdg-user-dir DESKTOP 2>/dev/null || true)"; [ -n "$DESK" ] && [ "$DESK" != "$HOME" ] || DESK="$HOME/Desktop"
APPS="$HOME/.local/share/applications"
UNITS="manet-bridge manet-chat manet-dashboard manet-voice"

echo "== Stopping and removing the services"
act $SUDO systemctl disable --now manet_mesh_bridge.target
for u in $UNITS; do act $SUDO systemctl disable --now "$u"; act $SUDO rm -f "$UNIT_DIR/$u.service"; done
act $SUDO rm -f "$UNIT_DIR/manet_mesh_bridge.target"
act $SUDO systemctl daemon-reload
if command -v ufw >/dev/null 2>&1 && [ "$DRY" != 1 ]; then
  $SUDO ufw status numbered 2>/dev/null | grep -q 'MANET_MESH_BRIDGE voice' && act $SUDO ufw delete allow in on eth0 proto udp to any port 38801:38810
  $SUDO ufw status numbered 2>/dev/null | grep -q 'TAK MESH TOC files' && act $SUDO ufw delete allow in on eth0 proto tcp to any port 8098
fi
echo "== Removing icons and full-screen autostart"
act rm -f "$APPS/manet-chat.desktop" "$DESK/manet-chat.desktop" "$APPS/manet-dashboard-app.desktop" "$DESK/manet-dashboard-app.desktop" \
         "$HOME/.config/autostart/manet-dashboard.desktop" "$BASE/start_kiosk.sh"
if [ -f "$HOME/.config/labwc/autostart" ] && [ "$DRY" != 1 ]; then sed -i '\#manet_mesh_bridge/start_kiosk.sh#d' "$HOME/.config/labwc/autostart"; fi
echo "== Removing the meshtoc command (and its old name, manet_mesh_bridge)"
act $SUDO rm -f "$CMD_LINK" "$BIN_LINK"
if [ "$PURGE" = 1 ]; then
  echo "== Deleting $BASE"
  if [ "$DRY" != 1 ] && [ -d "$BASE/releases" ]; then chmod -R u+w "$BASE/releases"; fi
  act rm -rf "$BASE"
else
  echo "   kept $BASE (deck.ini and the programs). Delete it too: bash uninstall.sh --purge"
fi
echo "Done."
