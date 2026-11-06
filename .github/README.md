<p align="center">
  <img src="images/banner.png" alt="TAK MESH TOC - Field Communications" width="100%">
</p>

<p align="center">
  <b>A portable TAK operations center for a mesh network.</b><br>
  One small Linux computer joins ATAK and iTAK phones on a mesh radio network to your TAK server:
  positions, chat, markers, DMs, push-to-talk voice and live video, both ways. The phones on the mesh need no
  internet, no server account and no certificate.
</p>

<p align="center">
  <img alt="License: GPL-3.0" src="https://img.shields.io/badge/license-GPL--3.0-2b6cb0">
  <img alt="Tested on Raspberry Pi 4" src="https://img.shields.io/badge/tested%20on-Raspberry%20Pi%204-c8a96e">
  <img alt="Works with OpenTAKServer" src="https://img.shields.io/badge/works%20with-OpenTAKServer-3c8c5a">
  <img alt="Phones need no internet" src="https://img.shields.io/badge/phones-no%20internet%20needed-555555">
</p>

---

## How it works

<p align="center"><img src="images/architecture.png" alt="How TAK MESH TOC works: the mesh, the deck and the server" width="100%"></p>

ATAK and iTAK phones talk to each other on the mesh with TAK's normal multicast. The **deck** (a Raspberry Pi wired to a mesh
node) carries that traffic to your TAK server over one TLS link and brings the server's traffic back onto the mesh.
People on the server see the mesh phones, and the mesh phones see them. Only the deck needs a link to the server;
if that link drops, the mesh keeps working on its own and the deck holds chats until it is back.

## What you get

| Part | What it does |
|---|---|
| **Bridge** | Mesh &#8644; server: positions, chat, markers and drawings, private DMs both ways, the deck's own GPS position |
| **Chat** | ATAK chat on the deck's own screen, mesh and server, including DMs, quick keys and report forms |
| **Ops** | A status dashboard: server link, every mesh node with its health and radio link speed (found by itself), traffic, video, GPS, voice and alerts, plus a setup wizard |
| **Video** | Every stream on the deck shows on the TAK map as a VIDEO marker at the person streaming: tap it, Play Video (works in iTAK on a mesh with no internet). Optionally, mesh phones' streams are copied to your server so server users can watch them too |
| **Voice** *(optional)* | Push-to-talk talk groups on the mesh &#8644; Mumble channels on your server |
| **`meshtoc`** | One command for check, logs, restart and rollback, with a watchdog for a frozen deck |

<p align="center">
  <img src="images/ops_dashboard.png" alt="TAK MESH TOC Ops dashboard" width="49%">
  <img src="images/chat.png" alt="TAK MESH TOC Chat" width="49%">
</p>
<p align="center">
  <img src="images/voice.png" alt="TAK MESH TOC Voice: mesh talk groups and Mumble channels" width="70%">
</p>

## Quick start

1. Download **`TAK_MESH_TOC.zip`** from [Releases](../../../releases) and copy it to the deck.
2. On the deck, as your normal user:
   ```
   unzip TAK_MESH_TOC.zip && cd TAK_MESH_TOC
   bash install.sh --icon
   ```
3. Open **http://127.0.0.1:8080** (or the TAK MESH TOC Ops icon). The setup wizard walks you through the rest.

**You need:** a Linux computer with systemd and Python 3.9 or newer (tested on Raspberry Pi 4 with Raspberry Pi OS
12), a mesh that passes ATAK multicast (tested with OpenMANET HaLow), and a TAK server plus a data package for the
deck (tested with OpenTAKServer 1.7). Everything it needs is bundled, so installing needs no internet. The deck needs a
link to your TAK server (for example Tailscale over Wi-Fi or a hotspot); the phones on the mesh need none.

The full guide (install, setup, every setting, DMs, voice, troubleshooting) is in the [README](../README.md).

## License

Copyright (C) 2026 Alejandro Rocha. TAK MESH TOC is free software under the
[GNU General Public License v3.0 or later](../LICENSE). Bundled libraries keep their own licenses: see
[THIRD_PARTY_NOTICES](../THIRD_PARTY_NOTICES.md).

TAK MESH TOC is an independent project. It is not affiliated with or endorsed by the TAK Product Center, OpenTAKServer
or OpenMANET.
