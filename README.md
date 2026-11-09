# TAK MESH TOC

**A portable TAK operations center for a mesh network.** TAK MESH TOC connects ATAK phones on a mesh radio network
(for example OpenMANET HaLow) to a TAK server, through one small Linux computer: the "deck". It carries positions,
chat, markers, DMs and push-to-talk voice between the mesh and the server, and shows the whole picture on the
deck's own screen.

The phones on the mesh need no internet, no server account and no certificate. They talk ATAK's normal mesh
multicast, and the deck carries their traffic to the server and back. Phones connected to the server see the
mesh phones, and the mesh phones see them.

```
 ATAK phones ))) mesh nodes (e.g. OpenMANET HaLow) ── cable ── DECK ── Wi-Fi / cellular / VPN ── TAK server
                                                         (TAK MESH TOC)                       (e.g. OpenTAKServer)
```

TAK MESH TOC was called MANET_MESH_BRIDGE before version 3.0. Installed decks upgrade in place: the folder
(`~/manet_mesh_bridge`), the services and `deck.ini` stay as they are, and the old command name still works.

It is four programs that run as services on the deck:

| Program | What it does | Service | Page |
|---|---|---|---|
| **Bridge** | Mesh ↔ server: positions, chat, markers, routes and drawings; private DMs both ways; files (pictures, packages, overlays) both ways; the deck's own identity and GPS position | `manet-bridge` | none |
| **Chat** (TAK MESH TOC Chat) | ATAK chat on the deck's own screen, mesh and server, including DMs | `manet-chat` | http://127.0.0.1:8090 |
| **Ops** (TAK MESH TOC Ops) | Status dashboard: server link, mesh nodes and radio links, traffic, video, GPS, voice, and alerts | `manet-dashboard` | http://127.0.0.1:8080 |
| **Voice** (optional, off by default) | OpenMANET push-to-talk talk groups ↔ Mumble channels on your server | `manet-voice` | Chat's **VOICE** page, http://127.0.0.1:8090/voice |

## What you need
- **The deck.** Any Linux computer with systemd and Python 3.9 or newer, with two network connections: one wired
  to a mesh node, one to the server. Tested on a Raspberry Pi 4 and Pi 5 with Raspberry Pi OS 12 (Bookworm).
- **A mesh** whose nodes pass ATAK multicast (239.2.3.1:6969 and 224.10.10.1:17012). Tested with OpenMANET HaLow nodes.
- **A TAK server**, plus a client certificate for the deck (the same data package you'd give a phone). Tested with
  OpenTAKServer 1.7. Everything also works with the official TAK Server, except DMs from server phones to mesh
  phones, which need the OpenTAKServer helper described below.
- **No internet on the deck.** The libraries it needs are bundled.

## Install
On the deck, as your normal user (not root):
```
unzip TAK_MESH_TOC.zip && cd TAK_MESH_TOC
bash install.sh --dry-run          # shows every step, changes nothing
bash install.sh --icon             # --icon: desktop icons; --kiosk: the Ops dashboard full-screen at login
```
Ops and Chat are always in the main menu. `--icon` also puts them on the desktop; delete those and later updates
leave them deleted. If the desktop asks "Execute?" every time you open one, use the menu (or add them to the
taskbar's launcher) instead.
### Setup: the wizard (recommended)
Open **http://127.0.0.1:8080** in the deck's browser (or click the TAK MESH TOC Ops icon). On a new deck Ops opens
**TAK MESH TOC // SETUP**, seven steps:
1. **Check the deck.**
2. **Server:** choose the data package (`.zip`) you made for this deck in OpenTAKServer, the same kind you'd give a
   phone. The wizard opens it (zips inside zips too), converts the certificates, reads the server address and tests
   the connection. It asks for the certificate password only if the package doesn't carry it.
3. **Mesh port:** usually `eth0`, or Later (server only).
4. **Callsign, team and role.**
5. **Features:** server people on the mesh, DMs across, GPS, who can open Ops.
6. **Server helper** for DMs, if you turned them on.
7. **Save and restart:** writes `deck.ini` (the old one is kept as a backup) and `~/certs`, restarts, and runs the full check.

Setup opens only on the deck's own screen. Run it again any time: Ops › SETUP › RUN SETUP AGAIN.

### Setup by hand
Or open `~/manet_mesh_bridge/deck.ini` and set the two things only you know:
1. **Your server:** `COT_URL` in `[bridge]`, plus the certificate lines (see Certificates).
2. **The deck's mesh address:** nothing to do. `MESH_ADDR = auto` in `[deck]` uses whatever address `eth0` has and
   follows it (3.1.7 or newer). To pin one instead, set the IP there and give the deck a static lease on the node.

Then:
```
meshtoc restart
meshtoc check
```
`check` tests everything and says what to fix: files, services, config, certificates, the server link, the mesh
address, GPS, the clock, and DMs.

## Certificates
The setup wizard does all of this for you. By hand: the deck connects to the server like a phone, with its own
client certificate. Make one for the deck on your
server (in OpenTAKServer, the same way as for a phone) and copy the data package `.zip` to the deck. OpenTAKServer
puts a second zip inside the first: unzip that one too. Inside it you'll
find the deck's certificate (`.p12`) and the server's CA (often `truststore-*.p12`). The bridge uses PEM files, so
convert them once. `openssl` asks for the `.p12` password each time:
```
mkdir -p ~/certs && chmod 700 ~/certs
openssl pkcs12 -legacy -in deck.p12 -clcerts -nokeys -out ~/certs/deck.pem
openssl pkcs12 -legacy -in deck.p12 -nocerts -nodes  -out ~/certs/deck.key
openssl pkcs12 -legacy -in truststore.p12 -nokeys    -out ~/certs/ca.pem
chmod 600 ~/certs/*
```
If your openssl says `-legacy` is unknown, leave it out. Then put these lines in `[bridge]`:
```
COT_URL = tls://<your server>:8089
PYTAK_TLS_CLIENT_CERT = ~/certs/deck.pem
PYTAK_TLS_CLIENT_KEY = ~/certs/deck.key
PYTAK_TLS_CLIENT_CAFILE = ~/certs/ca.pem
```
If you reach the server by a VPN address that isn't in its certificate, also add `PYTAK_TLS_DONT_CHECK_HOSTNAME = 1`.

## Direct messages (OpenTAKServer)
DMs from **mesh phones to server people** work through the deck. DMs from **server people to mesh phones** also need a
small helper on the OpenTAKServer computer.

The reason: OpenTAKServer keeps a mailbox only for phones connected to it, and mesh phones reach it only through the
deck. The helper files each mesh phone's DMs into the deck's mailbox, and the deck delivers them to that one phone.

1. Copy `server/mesh_mailroute` to the server, and install it there:
   `cd mesh_mailroute && bash install_server.sh`
   Its README explains what it does and doesn't do, and how to undo it.
2. On the deck, set `DM_RELAY = 1` in `[bridge]`, then run `meshtoc restart`.
3. Within a minute, `check` and the dashboard show **DM route: OK**. The deck proves that path works with a test
   message every minute, and shows **BROKEN** if it stops.

**A phone on the mesh and the server at the same time** (for example Tailscale or cellular on the phone, plus the
mesh) gets its DMs **from the server, over its own connection**, not through the deck: the helper sees it connected
and leaves it alone. If a server DM to a mesh phone doesn't arrive, check that phone isn't still connected to the
server. Use one or the other.

## iTAK (iPhone) users
iTAK works, but it differs from ATAK in ways that silently break things. Lessons from real use:

1. **The deck needs a team.** iTAK only lists something as a person you can message if its report carries a team
   (`TEAM` and `ROLE` in `[deck]`; the example config sets `Cyan` / `HQ`). Without a team, iTAK shows the deck as a
   shared marker or an unknown (yellow clover) icon. `meshtoc check` warns about this.
2. **Set the callsign before connecting, or reconnect afterwards.** OpenTAKServer names each phone's DM mailbox from
   the first position report on the connection. An iPhone often connects as "iPhone" before its callsign is set, and
   the server keeps that name until the phone reconnects. DMs addressed to the real callsign are then silently dropped.
   - **Fix:** after changing a callsign, toggle the server connection in iTAK off and on.
   - **Check it on the server:** `sudo rabbitmqctl -q list_queues name consumers`. The callsign should appear with 1
     consumer.
3. **iTAK addresses DMs by device ID, ATAK by callsign.** `server/mesh_mailroute` 2.1 or newer handles both. With 2.0,
   iTAK's DMs to mesh phones are dropped at the server.
4. **Video needs a login in iTAK's stream entry.** If your MediaMTX checks logins (OpenTAKServer's setup does), enter an
   OpenTAKServer username and password in the stream's **Credentials** fields, not in the address. Keep the stream
   name's capitals exactly. Without a login, iTAK's player is refused with a 401, retries in an RTSP form MediaMTX
   rejects, and MediaMTX logs `invalid SETUP path ... VLC fails a request`. iTAK itself only says "error".
   - **To see what's happening:** MediaMTX may log to a file instead of the system journal. The `logFile:` line in
     `mediamtx.yml` says where. A connection that never appears there means iTAK can't reach the server: check the
     address (copy it from `tailscale ip -4` on the server) and that the iPhone's VPN is on.
5. **iTAK on the mesh needs Local Network permission.** iPhone Settings → Privacy & Security → Local Network → turn
   **iTAK** on, then reopen iTAK. Without it, iOS blocks all of iTAK's mesh traffic and shows no error: the iPhone
   never appears on the mesh.
6. **On the mesh, iTAK sends DMs but not All Chat Rooms.** iTAK sends its position and direct messages over the mesh,
   but in testing it sent nothing when posting to All Chat Rooms, so no mesh phone, the deck or the server sees it.
   iPhone users on the mesh use DMs; All Chat Rooms works from iTAK when it is connected to the server directly.
   iTAK sends DMs by UDP (and each one twice) even to an endpoint that says tcp; the deck takes both (3.1.5 or newer).
   iTAK also drops a DM that still carries the server's `<marti>` addressing; the bridge removes it (3.1.6 or newer).
7. **Turn Cellular Data off on the mesh.** The mesh Wi-Fi has no internet, so iOS keeps cellular as its main network
   and sends iTAK's position reports there, where they vanish: the iPhone sees everyone, but nobody sees it. Turn off
   Cellular Data (or use Airplane Mode with Wi-Fi on) and Wi-Fi Assist (Settings → Cellular), then reconnect to the
   node. Also close iTAK's server connection before moving it to the mesh: until the server notices the old
   connection is gone, server DMs for that iPhone wait in its old server mailbox instead of coming over the mesh.
8. **Video: use the VIDEO markers.** iTAK won't play a video feed typed in by hand while the phone is on a mesh with
   no internet (it doesn't even try to connect). It does play video from a map marker: the deck puts every stream
   it has on the mesh map as **VIDEO <stream>**, at the person streaming it (it follows them; remarks give their
   callsign and MGRS grid). Tap the marker, then **Play Video**. Name a stream after your callsign (RAVEN21 for
   RAVEN 21) or pair them with `VIDEO_OWNERS` in `[dashboard]`; a phone streaming straight to the deck is matched by
   its address. Safari also plays any deck stream, with sound: `http://<deck mesh address>:8888/<stream>`.
9. **Mesh video on the server.** With `VIDEO_RELAY = auto` in `[dashboard]`, a stream a mesh phone sends to the
   deck is copied up to the TAK server, and server users get a **VIDEO <stream>** marker at that phone, linking to
   the server's copy (Play Video, as above). First let the deck publish on the server: on OpenTAKServer, add the
   deck's Tailscale address to `OTS_IP_WHITELIST`. It costs uplink data (about 0.5-0.7 GB per hour per camera);
   Ops' SETUP tab shows what is being copied and how far behind live it is. If your uplink can't keep up, the copy
   jumps back to live whenever it falls 5 s behind (tap Play again); keep the camera under about 70% of your upload
   speed (720p at 1-1.5 Mbit/s suits most cell links).

## Voice
OpenMANET nodes have push-to-talk voice built in: talk groups on the mesh. The deck can carry those talk groups to a
**Mumble** server, so people on the server side (the Mumble app on a phone or PC, over Tailscale) talk with the mesh
phones. It works like the bridge does for chat: mesh audio goes to Mumble unchanged (both are Opus), and Mumble audio
goes onto the mesh the way OpenMANET sends it.

- **One talker per talk group.** Whoever starts first has it until they pause. A Mumble user who talks over a mesh
  phone gets a "Channel busy" message, and the attempt shows on the VOICE page. Nobody holds a channel for more than a
  minute (`MAX_TALK_S`).
- **No echo, no backlog.** Audio never goes back where it came from. Late or blocked audio is dropped, never replayed.
- **The VOICE page** (a tab in Chat) shows every talk group: who is talking, who is on it (mesh and Mumble), and
  recent transmissions. The Ops dashboard has a voice row in MESH + DECK.
- **TG1 is bridged by default; TG2–TG5 have a switch.** Flip **BRIDGE TO MUMBLE** on the VOICE page and that talk
  group links to the Mumble channel of the same name (TG2 → "TG2"). The switches are remembered across restarts.
  All five talk groups are always watched, bridged or not.
- **LISTEN ON DECK** plays the selected talk group (mesh phones and Mumble users) through the deck's speaker or
  headphones, with a volume slider and a level meter. One talk group at a time. Nothing is recorded.
- **PTT box on the deck:** plug a USB PTT box (a CM108 sound card with PTT wired to its volume-down button, a
  tactical headset and PTT on it) into the deck and the deck operator can talk. Key it and the deck talks on the talk
  group picked with **PTT BOX TALKS ON** (the mesh, plus its Mumble channel when bridged), shown as "DECK (PTT box)".
  The headset plays that talk group all the time and never your own voice. The button is taken over, so it never
  changes the deck's volume. Found within a few seconds of plugging in; the header pill says READY, TALKING, BUSY or
  NOT PLUGGED IN, and only real problems reach Ops. `DECK_PTT = off` in `[voice]` turns it off.
- **Deck screen only:** listening, the switches and the PTT box talk group work on the deck's own screen. From another
  device the VOICE page is read-only, so nobody on the mesh can change your bridging or pull an audio stream over the radio.

**Set up, once** (details in `[voice]` of `deck.ini`):
1. **Mesh nodes:** turn on OpenMANET's voice. It is off by default. In `/etc/openmanetd/config.yml` on each node set
   `comms:` `enable: true` and `controlSource: web`, then restart openmanetd.
2. **The node's wired port must be in the mesh bridge.** On some node images, `eth0` (the deck's cable) sits in its own
   network with an address that clashes with the mesh's. The deck then never hears the talk groups. Put `eth0` into
   the same bridge as `bat0` (in LuCI: Network → Interfaces → the mesh bridge → Devices).
3. **Mumble server** on the TAK server, reachable over Tailscale only, with a server password:
   ```
   sudo apt install mumble-server && sudo ufw allow in on tailscale0 to any port 64738 && sudo ufw deny 64738
   ```
   The config is `/etc/mumble/mumble-server.ini` (`/etc/mumble-server.ini` on older packages). Set `serverpassword=`
   there, then `sudo systemctl restart mumble-server` and set a SuperUser password:
   `sudo mumble-server -ini /etc/mumble/mumble-server.ini -supw <password>`.
   - **For VX on server-side ATAK phones,** give Mumble your OpenTAKServer's certificate, so VX trusts it (with
     Mumble's own self-signed certificate, VX says "missing or incompatible certificate"). Copy the OTS server
     certificate, its key and the CA where Mumble can read them:
     ```
     sudo install -o root -g mumble-server -m 644 ~/ots/ca/certs/opentakserver/opentakserver.pem /etc/mumble/ots-mumble.pem && sudo install -o root -g mumble-server -m 640 ~/ots/ca/certs/opentakserver/opentakserver.nopass.key /etc/mumble/ots-mumble.key && sudo install -o root -g mumble-server -m 644 ~/ots/ca/ca.pem /etc/mumble/ots-ca.pem
     ```
     Then add these three lines **right after `serverpassword=`** (not at the end of the file: anything below the
     `[Ice]` section is ignored), and restart Mumble:
     ```
     sslCert=/etc/mumble/ots-mumble.pem
     sslKey=/etc/mumble/ots-mumble.key
     sslCA=/etc/mumble/ots-ca.pem
     ```
     The server's fingerprint changes: Mumble apps ask once to accept it, and the deck needs its new pin (step 5).
     The same happens if OTS ever makes a new server certificate: `check` on the deck says so and prints the new one.
4. **One permanent channel per talk group you'll bridge** (asks for the SuperUser password; run it for TG2 etc. too):
   `python3 ~/manet_mesh_bridge/current/voice/make_channel.py TG1 <server Tailscale IP>`
   A talk group switched on before its channel exists shows NO MUMBLE CHANNEL, and goes in by itself once it's made.
5. **On the deck:** in `[voice]`, set `MUMBLE`, `MUMBLE_PASSWORD` and `VOICE = on`, then `meshtoc restart`.
   Run `meshtoc check`: it prints the server's certificate fingerprint. Paste it into `MUMBLE_CERT_SHA256`
   and restart again, so the deck only ever talks to your server.

**Who uses what:**

| Who | App | Connects to | Network setting |
|---|---|---|---|
| ATAK phones on the mesh | VX plugin | **Multicast** `239.192.41.1`, port `38801` (TG1), protocol **RTP** | **`wlan0`, not Auto** |
| ATAK phones on the server (Tailscale) | VX plugin | **Mumble**: the server's Tailscale address, port 64738, the server password, channel `TG1` | Tailscale or Auto |
| iTAK and others | Mumble app (Mumla on Android) | the same Mumble server, password and channel | |

- **Talk group ports:** TG1 38801, TG2 38803, TG3 38805, TG4 38807, TG5 38809. Each one's RTCP reports use the next
  port up, so UFW on the deck opens 38801–38810 on `eth0`.
- **VX on Auto** may send voice out over mobile data, and nobody on the mesh hears it.
- **VX on the server** needs Mumble to use the OTS certificate (step 3), and ATAK connected with its OTS certificate.
- **A multicast talk group never works over Tailscale,** whatever the Network setting: Tailscale doesn't carry
  multicast. Multicast is for phones on the mesh, Mumble for phones on the server; the deck joins the two.
- **VX labels its Opus audio** as RTP payload type 0 instead of 111. The deck accepts both (`MESH_PAYLOAD_TYPES`).

## Everyday commands
| Command | Does |
|---|---|
| `meshtoc status` | version, the services, bridge traffic, voice, GPS |
| `meshtoc check` | the full check (exit code 1 if something is wrong) |
| `meshtoc restart` | restart them all, after editing `deck.ini` |
| `meshtoc logs bridge -n 50` | logs (`bridge`, `chat`, `dashboard`, `voice`, or all) |
| `meshtoc rollback` | back to the previous version; run it again to go forward |
| `meshtoc tidy` | remove old releases, zips and unzipped folders |

The command's old name, `manet_mesh_bridge`, does the same thing.

## How it behaves
- **No mesh node plugged in?** The deck runs server-only: Chat talks to the server, and the deck shows on server
  maps. It switches to mesh + server by itself when a node is plugged in.
- **The node rebooted and gave the deck a new address?** With `MESH_ADDR = auto` the bridge and Voice notice within
  a few seconds and restart on the new address. With a fixed `MESH_ADDR`, the deck stops reaching the mesh until it
  gets that address back, so give it a static lease on the node (an OpenWrt node forgets its leases when it reboots).
- **Several mesh nodes?** Ops lists every node the mesh itself knows about (OpenMANET shares this list between its
  nodes; Ops reads it with no login), so a node you add shows up by itself. The **MESH** tab shows each node's ping,
  CPU temperature, load, memory and uptime, the speed of every radio link (GOOD 3 Mbit/s and up, OK 1-3, POOR below 1;
  change with `LINK_GOOD_MBPS` / `LINK_POOR_MBPS`), link speed over the last hour, and the 24-hour history. The
  MESH + DECK panel shows the one-line summary. A node joining, a node lost and a link turning POOR each go in the
  event log once. Name nodes yourself with `NODE_IP = 10.41.0.1, 10.41.254.10` and `NODE_NAME = node 1, node 2`
  (your names win). The installer gives the mesh port the IPv6 address the node list needs; `meshtoc check` says
  if it's missing.
- **A node drops off?** The tile turns amber (`1 OF 2 NODES`) while any node still answers. If no node answers a ping
  but mesh traffic is still arriving, the tile stays LINK OK (amber) and the bridge keeps RELAYING: NODE LOST means
  no pings and no traffic.
- **Server users tap DECK on the map** to see the deck's health in its remarks: CPU temperature, internet link and
  Wi-Fi signal, mesh nodes and the slowest radio link, uptime and version (`DECK_STATUS = off` in `[bridge]` leaves
  it out).
- **Server link drops?** The bridge notices within about a minute, even if the connection never closed, and
  reconnects. Chats and DMs on their way to the server are kept on disk and sent when the link is back.
- **Busy or slow link?** Chats, markers and DMs always go first. Position reports send only the newest one per phone,
  so the server never gets a stale backlog.
- **Broken packets** from the mesh are dropped at the deck, and counted on the dashboard. Packets the server would
  accept are repaired (extra padding cut off, several events split).
- **Phones never see themselves echoed back**, and the return path to the mesh is rate-limited (`RETURN_MAX_PER_SEC`).

## Security
- **The mesh is trusted.** Anything sent to the ATAK multicast groups on the mesh goes to your server under the deck's
  identity, the same trust ATAK multicast always has. Protect the mesh itself: use encryption on the radio links
  (for example WPA3/SAE on HaLow) and keep strangers off it.
- **Ops and Chat** listen on the deck itself. The dashboard can also listen on the mesh port
  (read-only from other devices). The restart and snapshot controls work only from the deck's own screen. Don't
  forward these ports to the internet.
- **Files only you can read:** `deck.ini` (mode 600) and your certificate keys. `check` warns if a key is readable
  by others.
- **Voice:** the Mumble server listens on Tailscale only and has a password. The deck pins its certificate
  (`MUMBLE_CERT_SHA256`), so it never sends voice to anything else. Mesh voice itself is as open as the mesh: anyone on
  the mesh can listen to and talk on the talk groups.
- **Never share these:** the deck's certificate files, `deck.ini`, and the data package.

## Optional, recommended
- **Hardware watchdog:** if the deck ever freezes completely, it reboots itself about 14 s later.
  ```
  sudo mkdir -p /etc/systemd/system.conf.d && printf '[Manager]\nRuntimeWatchdogSec=14\nRebootWatchdogSec=2min\n' | sudo tee /etc/systemd/system.conf.d/watchdog.conf && sudo systemctl daemon-reexec
  ```
- **The clock:** TAK hides reports with a wrong time. Give the deck internet once, or use chrony with the GPS.
- **GPS:** with a GPS receiver and `gpsd`, set `GPS = gpsd` in `[deck]`. The deck then shows live on every map, and
  the dashboard gets a GPS tile.
- **Video:** with MediaMTX on the deck, phones and drones can stream to it, and the dashboard shows the streams.

## Files on the deck
```
~/manet_mesh_bridge/deck.ini                 the config (mode 600)
~/manet_mesh_bridge/current -> releases/<v>  the programs, read-only
~/manet_mesh_bridge/previous                 the version before (for rollback)
~/bridge_status.json  ~/bridge_outbox.jsonl  ~/deck_chat_log.jsonl  ~/dashboard_events.jsonl  ~/dashboard_history.jsonl
~/voice_status.json  ~/voice_bridges.json   voice: who is talking, which talk groups are switched on (nothing is recorded)
~/voice_deckptt.json                         the talk group the PTT box on the deck talks on
~/mesh_status.json                           Ops' mesh summary for the DECK status line
~/file_cache/                                files from the server waiting for mesh phones (12 h, 200 MB at most)
```

## Upgrade, roll back, remove
- **Upgrade:** unzip the new version and run `bash install.sh`. Your `deck.ini` is kept.
  - The installer refuses to downgrade, and refuses a damaged package.
- **Roll back:** `meshtoc rollback`.
- **Remove:** `bash ~/manet_mesh_bridge/current/uninstall.sh`. Add `--purge` to delete the config too. Message logs
  are always kept.

## Tests
`bash tests/run_all.sh` runs every test suite (about 800 checks) on a Linux PC or VM, not on a live deck.
- **What runs:** the real programs, against stand-ins for the phones, the mesh and the server, including a copy of
  OpenTAKServer's own stream reader. Voice runs against a real Mumble server. The suite also has fuzzing, a
  network-namespace cable test, browser tests of every page, and an install/upgrade/rollback simulation with no
  internet.
- **What it needs:** a few suites need Chromium/playwright, root, mumble-server and libopus, or a local RabbitMQ.
  They're skipped, with the reason, when those are missing.

## Limits
- One deck per server identity. Two decks need two certificates and two different `UID`s.
- Files (pictures, data packages, KML/KMZ, up to 5 MB) cross both ways. A file from a mesh phone reaches everyone
  on the server (a phone can't say which server person it picked); a file from the server reaches the mesh phones it
  was sent to, or every mesh phone if it was sent to everyone (each phone downloads its own copy over the radio).
- Voice: one deck per Mumble server user name (`MUMBLE_USER`). Mumble users talk to the whole channel; Mumble
  whispers and private calls don't reach the mesh.
- Server → mesh DMs need OpenTAKServer and the helper.
- A phone connected to the server directly gets server DMs that way, not over the mesh (see Direct messages).
- Tested on Raspberry Pi OS 12 with OpenTAKServer 1.7 and ATAK-CIV. Other setups should work, but haven't been tried.

## License
Copyright (C) 2026 Alejandro Rocha

GPL-3.0-or-later. See `LICENSE`. Bundled libraries keep their own licenses: see `THIRD_PARTY_NOTICES.md`.
No warranty. **Not for life-safety use.**
