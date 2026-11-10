# Changes

## 3.1.25: mic icon on the deck
- **VOICE page:** the mic on the PTT BOX pill and the talk group badge showed as an empty box on the deck (it has no
  emoji font). The page now draws the mic itself, in the pill's colour. Nothing else changes.
- Tests: `deckptt_test.py` checks the page has no emoji left and that both mic icons are drawn.

## 3.1.24: voice without the mesh node
- **Mumble starts at once, without the mesh node.** Voice used to wait for the node to give the deck an address, so
  with no node plugged in there was no voice at all, not even to Mumble. Now bridged talk groups link to Mumble
  straight away and the PTT box on the deck works over the server link alone. When the node is plugged in, the mesh
  joins the talk groups by itself (no restart); a new address from the node still restarts Voice to follow it.
- **VOICE page:** MESH VOICE · NO NODE and a plain NO MESH NODE note (Mumble only) instead of "voice service stopped
  ... s ago"; the PTT box row says "Mumble only", or warns when its talk group isn't on Mumble ("nobody would hear
  you"). `meshtoc check` says the same.
- **Ops and Chat icons get their own browser profile** (`~/.config/tmt-ops-browser`, `~/.config/tmt-chat-browser`),
  like the full-screen kiosk already had. A damaged profile in your everyday Chromium ("Profile error occurred")
  no longer pops up over Ops and Chat. Rewritten by install.sh; nothing else changes.
- **Fix:** the PTT BOX row (an empty box) showed on the VOICE page when voice wasn't running or the PTT box was off.
- Tests: `nomesh_test.py` (no node, plug in, unplug, page, check), `meshaddr_test.py` (no wait), `deckptt_test.py`
  (the row is really hidden).

## 3.1.23: PTT box on the deck
- **The deck operator can talk.** Plug a USB PTT box into the deck (a CM108 sound card with PTT wired to its
  volume-down button, a tactical headset and PTT on it). Key it and the deck talks on the talk group picked on the
  VOICE page: the mesh, plus that talk group's Mumble channel when it is bridged. It shows as "DECK (PTT box)", via
  DECK, like any talker, and one talker per talk group still holds: keyed while someone talks = BUSY, nothing sent.
- **The headset plays that talk group all the time**, like a radio, and never the deck's own voice.
- **The button is taken over**, so it never turns the deck's volume down. The box and its sound card are paired by
  their USB port, so another USB sound card on the deck is never mixed up with it. Found within a few seconds of
  plugging in; unplugging (even while keyed) frees the channel and just shows NOT PLUGGED IN.
- **VOICE page:** a PTT BOX pill in the header (READY / TALKING / BUSY / NOT PLUGGED IN), a badge on its talk group,
  and a PTT BOX TALKS ON row (deck screen only, like the other switches). The pick is remembered (~/voice_deckptt.json).
- **Ops:** only real problems show (e.g. no permission to read the button). `meshtoc check` says whether the box is
  plugged in and whether the deck user is in groups input + audio.
- **install.sh** adds the deck user to groups input and audio and checks for alsa-utils (arecord/aplay).
- New `[voice]` settings: `DECK_PTT = auto` (off = never), `DECK_PTT_TG`, `DECK_PTT_STATE_FILE`.
- Tests: `deckptt_test.py` (finding the box, mesh + Mumble, headset, busy, talk group switch, not bridged, unplug
  while keyed, Voice page). `voice_test.py` keeps a real PTT box out of its gateway.

## 3.1.22: phones that move between the mesh and the server
- **No 10-minute wait after a phone moves.** The loop guard (anything heard on the mesh in the last 10 min is never
  sent back to the mesh) also held back a phone that had just moved from the mesh to the server: its new reports and
  its files didn't reach the mesh, so mesh phones kept its old mesh address and their files to it went nowhere, and
  its files to mesh phones were dropped. The guard now compares report times: a report newer than anything that
  phone sent on the mesh is the phone itself, now on the server, and goes through at once. The mesh's own echo
  (same time) is still never sent back.
- **Deck Chat: the DM route follows the phone.** ON NET showed "DM·mesh" for up to 10 min after a phone moved to the
  server; now where it was heard last wins, at its next position report.
- A file sent to everyone isn't sent to a mesh address of a phone that moved to the server.
- The log says why a file from the server isn't put on the mesh: "not for the mesh (sent from the mesh)",
  "(for X, not heard on the mesh)", "(sent to everyone, no mesh phones heard)".
- Tests: `outbox_test.py` (DM route follows the phone), `file_relay_test.py` (moved phone, loop guard, times).

## 3.1.21: server people reach mesh phones through the server
- **Mesh phones are shown to the server as "reach me through the server"** (contact endpoint `*:-1:stcp`) instead
  of their mesh IP, which nobody on the server side can reach. iTAK saw the mesh IP, took the mesh phone for a
  local peer and offered to host a file itself (`/getFile?file=0...`), which the deck can't reach. Now server apps
  send files and DMs through the server, which the deck carries. The deck still keeps each phone's real mesh
  address for delivering to it. `SERVER_ENDPOINT = keep` passes the address unchanged (the old way).
- **iTAK package names:** "Mom.MissionPackage.1" is passed on like a name with no extension: only if the file
  really is a data package (zip). Other unknown types (.apk, .docx, ...) are still refused.
- `file_relay_test.py`: the endpoint as the server sees it, `keep`, and the MissionPackage name.

## 3.1.20: iTAK files to the mesh, take 2
- **Fix:** the link in iTAK's file notice can lead to a server web page instead of the file (the deck got a 1 KB
  HTML page). After the notice's link, the deck now also tries the standard TAK download by hash,
  `/Marti/sync/content?hash=<SHA-256>`, on the server the bridge uses, which is how OpenTAKServer serves stored
  packages.
- A web page is never taken for the file ("a web page, not the file"); the next link is tried.
- When every link fails, the log lists each link path tried and what came back (paths only).
- `file_relay_test.py`: a link that returns a web page; no copy anywhere.

## 3.1.19: iTAK files to the mesh
- **Fix:** a file from iTAK to mesh phones was refused ("download didn't match its SHA-256"): iTAK's notice can
  carry a different SHA-256 than the copy OpenTAKServer stored and names in the download link. The server copy is
  now accepted when it matches the hash the link names (the link came from the server over the deck's own TLS
  connection), and the notice to the mesh phone is corrected to that copy's SHA-256 and size so ATAK accepts it.
  Matching neither is still refused.
- The error now shows the notice's, the link's and the downloaded SHA-256, the size and the content type.
- A server reply sent gzip-compressed is unpacked before the check.
- `file_relay_test.py`: the iTAK case, and the detailed error.

## 3.1.18: iTAK data packages
- **Fix:** iTAK names a data package without a file extension ("server test" instead of "server test.zip"), so a
  file from iTAK was refused as "file type not passed on". A file with no extension is now passed on, both ways,
  when it really is a data package (a zip; checked after download), and uploaded to the server as "<name>.zip".
  Anything else with no extension is still refused and shown in Ops.
- README: `~/file_cache/` in "Files on the deck". `link_test.py` cleans the bridge's file_cache folder too.
- `file_relay_test.py`: an iTAK package with no extension both ways; a non-zip with no extension is refused.

## 3.1.17: files from the server to the field
- **Pictures, data packages and KML/KMZ from the server reach mesh phones.** The server's notice links to the
  server's copy, which mesh phones can't reach. The deck downloads it with its certificate (from the link, or the
  server address the bridge already uses), checks its SHA-256, keeps it in a small cache (`FILE_CACHE_HOURS` 12,
  `FILE_CACHE_MB` 200, in `file_cache` next to the status file) and serves it on its own mesh address
  (`FILE_PORT`, 8098; cached files only). The notice goes, with the link changed to the deck and the server's
  addressing removed, to the mesh phones it was sent to, or to every mesh phone if it was sent to everyone.
  A mesh phone's receipt goes back to the server person who sent it.
- Same guardrails as the other way: `FILE_MAX_MB`, `FILE_TYPES`, one file at a time; a file already in the cache
  goes to more phones without downloading it again; a file that came from the mesh never goes back down.
  Problems show in Ops only: **"File not passed to the mesh"**.
- **Fix: bare pictures to the server.** OpenTAKServer stores a lone .jpg/.png/.kml as a new data package with its
  own SHA-256 and size. The deck now reads that copy once and sends the notice with the server copy's hash, size
  and .zip name, so receiving phones accept the download.
- Installer: port 8098 in its "already in use" check; with UFW on, opens 8098/tcp on the mesh port only
  (uninstall removes it).
- `file_relay_test.py` (52 checks) covers both directions, the cache, receipts, echo and the re-packaged picture.

## 3.1.16: files from phones with secure file sharing
- **Fix:** a phone with ATAK's secure file sharing on serves its files over https with its own self-signed
  certificate, and the deck refused that download ("couldn't download it from the phone ... CERTIFICATE_VERIFY_FAILED").
  Like ATAK itself, the deck now doesn't check the certificate of a phone-hosted (peerHosted) file; the file's SHA-256
  from the notice still has to match. Any other https link is still fully checked.
- `file_relay_test.py`: a phone serving over https on a self-signed certificate; a non-phone https link with a bad
  certificate is still refused.

## 3.1.15: files from the field, routes to the field
- **Pictures, data packages and KML/KMZ from mesh phones reach the server.** ATAK shares a file by sending a notice
  with a link to the phone, which server people can't reach. The deck now does what ATAK does with a server: it
  downloads the file from the phone over the mesh, checks its SHA-256, uploads it to the server's file API with the
  deck's certificate (POST /Marti/sync/missionupload, the phone as creator) and passes the notice on with the
  server's link. The phone gets a delivery receipt from the deck, or a failure with the reason.
- **Guardrails, in the background:** up to `FILE_MAX_MB` (5), only `FILE_TYPES` (zip, jpg, jpeg, png, kml, kmz), one
  file at a time, the same file sent to several people uploaded once. A file notice is never passed on with its
  mesh-only link. Nothing new on screen: a refused or failed file shows in Ops' fault list ("File not passed to the
  server"). `FILES = off` turns it off; `MARTI_URL` overrides https://<COT_URL host>:8443.
- **A file from the field reaches everyone on the server:** a phone can't say which server person it picked (they
  all reach it through the deck). Needs `DM_RELAY = 1`.
- **Routes cross to the mesh:** `b-m-r` is in the default `RETURN_TYPES`.
- New test `file_relay_test.py` (fake phone, fake server file API, a real TLS server that wants the deck's
  certificate). `big_test.py`: a 50 MB package notice is read whole, refused as too big and shown in Ops.

## 3.1.14: old messages expire on their own
- **Unsent chat expires.** Deck Chat messages still not sent after `OUTBOX_EXPIRE_HOURS` (new, default 12; 0 = never)
  are marked **EXPIRED**: never sent, struck through in the list, kept in the log, and marked "NOT SENT: expired" in
  the export. They don't come back after a restart. No new buttons; SEND / DROP on held messages work as before.
- **The bridge's outbox expires while running.** Chats waiting for the server longer than `OUTBOX_HOURS` (default now
  12, was 24) are dropped as the bridge runs, not only at its next restart.
- **Deck Chat runs with python3 -S,** like the bridge: only the bundled libraries, so another protobuf installed on
  the computer can't break it. Started by hand next to such a copy, Chat now keeps running (TAK Protocol decoding off,
  with a warning) instead of crashing at start.
- New test `protobuf_conflict_test.py`; `outbox_test.py`, `durability_test.py` and `install_sim.sh` cover the rest.

## 3.1.13: failures you can see
From an audit of every catch-all error handler (53 of them): the ones that could hide a real failure now show it in
Ops, not only in journalctl. No new features or settings.
- **Bridge errors show in Ops.** Errors the bridge survives are counted per area with the last reason: mesh -> server,
  server -> mesh, the saved-chat replay, the DM route check, the deck's own report, unreadable iTAK DMs. While one
  keeps happening it is in Ops' alert strip (red for the traffic paths and the replay, amber for the rest), raises
  the BRIDGE tile, and the event log notes it starting and "working again". The journal gets the first 3 of each and
  then every 100th, so a repeating error can't flood it.
- **The saved-chat replay is retried.** If resending the outbox after a restart failed, it used to give up until the
  next restart. Now it retries (5 s, growing to 5 min) and never sends a chat twice.
- **Ops says when its own data is old.** If collecting fails or hangs, the page shows "Ops data stale since HH:MM:SS"
  with the reason instead of old data that looks live.
- **"Deck disk not writable"** is an Ops alert when Ops can't write its files (a USB SSD gone read-only), and clears
  when writes work again.
- **Server-stream marker check:** if the check itself fails, the event log says so once (it was silent).
- **Chat:** a packet that breaks message handling is counted and logged (it was invisible). If the sender itself
  fails, the OUTBOX pill turns red: **OUTBOX n · STUCK**, with the reason in its tooltip.
- New test `faults_test.py` forces each failure and checks it shows; `outbox_test.py` covers the chat ones.

## 3.1.12: lag guard for mesh video on the server
- **The server copy no longer drifts behind live.** When the camera sends more than the deck's uplink carries, video
  queued up and the server copy fell further and further behind. Now each copy's lag is measured (bytes waiting in
  its send queue at the camera's bitrate, plus any time ffmpeg itself fell behind); past 5 s the deck drops the
  backlog and restarts the copy at once, so server users jump back to live (iTAK may need Play again). The server
  marker stays up throughout. A copy runs at least 20 s before it can be restarted.
- 3 jumps in 10 minutes: the event log warns that the camera's bitrate (shown) is more than the uplink carries.
- Ops SETUP shows how far behind live each server copy is.
- README: keep the camera under about 70% of your upload speed (720p at 1-1.5 Mbit/s suits most cell links).

## 3.1.11: mesh video on the server
- **Mesh phones' streams reach server users.** With `VIDEO_RELAY = auto` in `[dashboard]`, every stream a mesh phone
  sends to the deck is copied up to the TAK server's MediaMTX under the same name (one ffmpeg per stream, no
  re-encoding): the deck's uplink carries it once however many server users watch. About 0.5-0.7 GB per hour per camera.
- **Server users get a VIDEO marker at the phone**, linking to the server's copy (Play Video). Sent through the
  bridge's server-only port and marked so the server's echo never comes back onto the mesh. Its own ID (SVIDEO), so
  it never collides with the mesh marker for the same stream. Appears once the copy has run 3 s; goes when it stops.
- **Safe by design.** Only streams published TO the deck are copied (never one it pulls from the server: no loops).
  A name already live on the server is left alone (copying would take it over) and the event log says so. A refused
  copy is logged with the reason and retried after 5 s, waiting longer each time up to 60 s. Logins never appear in
  markers, logs or the page. Copies stop with Ops.
- The server must accept the deck publishing: on OpenTAKServer, add the deck's Tailscale address to
  `OTS_IP_WHITELIST`. Needs ffmpeg (install.sh notes if it's missing). Off by default.
- Ops SETUP: "Video to the server" line. Works with the mesh markers on or off.
- New test `videorelay_test.py`.

## 3.1.10: a VIDEO marker means a live camera
- **On-demand streams get a marker only while they are actually streaming.** For a source the deck pulls when
  someone watches (like a server stream), the deck asks the source every 20 s whether it is live: one RTSP DESCRIBE,
  no video pulled, over the server link, never on the mesh. The marker appears within about 20 s of the camera
  starting and goes within about 20 s of it stopping. Before, it showed all the time.
- No answer, a refused login or not checked yet = no marker: the map never shows a camera that isn't on.
- Uses the login in the source URL (Basic or Digest; with Digest the password never crosses the network). The check
  runs in its own thread, so Ops never waits on the server. `VIDEO_SOURCE_CHECK = 20` in `[dashboard]`.

## 3.1.9: video on the map, at the person streaming
- **VIDEO markers on the mesh.** Every stream on the deck's MediaMTX (live now, or an on-demand source the deck pulls
  from the server) shows on the mesh map as **VIDEO <stream>**. Tap it, then **Play Video**: no feed to type on any
  phone. Works in ATAK, and in **iTAK on a mesh without internet**, where a hand-typed feed never even connects.
- **At the person streaming, not the deck.** The marker sits 8 m from them and follows them (resent when they have
  moved 25 m, at most every 15 s). Remarks give their callsign, MGRS grid, and "last known position HH:MMZ" when the
  report is over 5 minutes old. Whose stream: `VIDEO_OWNERS` in `[dashboard]` (`RAVEN21: RAVEN 21`), else a mesh
  phone streaming straight to the deck (by its address), else stream name = callsign (spaces and case ignored).
  Unknown: by the deck, saying why.
- Mesh only: the link is the deck's mesh address, so markers never go to the server (sent out the mesh port, not
  looped back to the bridge). Sent on change and every 3 minutes; they vanish 10 minutes after the deck stops
  sending, and at once when a stream ends. `VIDEO_MARKERS = off` turns them off.
- The bridge keeps everyone's last position (mesh and server) for this, in its status file only.
- Ops SETUP shows the video markers; the event log notes each marker going on or off the mesh.
- README, iTAK: use the VIDEO markers; Safari plays any deck stream with sound at `http://<deck>:8888/<stream>`.
- New test `videomarkers_test.py` (with root: a real send reaches a phone on the mesh port and not the deck itself).

## 3.1.8: Ops sees the whole mesh
- **Ops finds the mesh nodes by itself.** It reads the node list OpenMANET's nodes share with each other (alfred), with
  no login, so a node added to the mesh shows up without editing `deck.ini`. Nodes named in `NODE_IP` keep your names.
  Before, Ops only saw nodes listed in `NODE_IP` or ones the deck happened to have talked to, and listing any node
  in `NODE_IP` turned the search off.
- **Node health, no password:** each node's CPU temperature, load, memory and uptime, from the node's own status call.
- **Radio links:** the throughput of every link between nodes (batman-adv's own figure, the slower direction),
  graded GOOD / OK / POOR (`LINK_GOOD_MBPS = 3`, `LINK_POOR_MBPS = 1`). New alert metric `link_mbps` (slowest link).
- **New MESH tab** in place of TRENDS: node table, radio links, link speed over the last hour, node ping, 24-hour
  history. The MESH + DECK panel opens with a one-line summary (`3 NODES · ALL OK`, slowest link, any node lost).
  The messages-per-minute graph is gone (the TAK BRIDGE tile has it); CPU temperature is a sparkline in the panel.
- The MESH LINK tile counts nodes (`3 NODES`, `2 OF 3 NODES` in amber when one stops answering).
- Event log: a node joining the mesh, a node lost, a radio link coming up, turning POOR, recovering or going away.
  Once per change.
- **DECK's report carries the deck's health** in its remarks, for server users who tap DECK on the map:
  `CPU 52C | WAN wlan0 -61 dBm | mesh 2 nodes, slowest 7.1 Mbit/s | up 6h12m | TAK MESH TOC 3.1.8`.
  `DECK_STATUS = off` in `[bridge]` leaves it out.
- `install.sh` sets the mesh port's NetworkManager connection to `ipv6.addr-gen-mode eui64` (alfred only answers that
  kind of IPv6 address; the port blinks once). Skipped when IPv6 is off on that connection.
- `meshtoc check`: a "Mesh node list" line (with the exact fix if the address is missing) and the mesh summary;
  Voice says "waiting for the mesh" instead of failing when no node is plugged in.
- New tests: `meshwatch_test.py` (with root and an alfred binary, a real alfred server answers), `deck_status_test.py`.

## 3.1.7: the deck follows its mesh address
- **`MESH_ADDR = auto`** in `[deck]`: the deck uses whatever address its mesh port (`MESH_IFACE`, usually eth0) has.
  The bridge and Voice look it up at start and check every 5 s; when the node hands out a different address (an
  OpenWrt node forgets its leases when it reboots), they restart themselves on the new one and log why. Before,
  the bridge kept using the old address and nothing reached the mesh ("Network is unreachable") until someone
  noticed. An address that goes away (node unplugged) is not a change: the deck waits server-only, as before.
  No address at start = server-only until it appears. A fixed MESH_ADDR works as before.
- New installs and the setup wizard write `MESH_ADDR = auto`. `meshtoc check` shows the address auto found, and
  suggests auto when a fixed address has gone missing.
- `[deck] MESH_ADDR = auto` no longer breaks Voice (it took "auto" as an address).
- Updates no longer put back desktop icons you deleted; the menu entries are always kept. README: use the menu or
  the taskbar if the desktop asks "Execute?" for each launch.
- New test `meshaddr_test.py` (with root: the real bridge on a test port whose address is removed, restored and
  changed under it).

## 3.1.6: server DMs reach iTAK on the mesh
- **DMs from server phones to an iPhone on the mesh now show in iTAK.** The bridge delivered them, but iTAK silently
  drops a direct message that still carries the server's addressing (`<marti><dest .../></marti>`); ATAK ignores it.
  The bridge now removes that block from the copy it hands to the mesh phone. It still uses it to pick the
  recipient. Nothing else in the message changes; broadcasts, positions and markers are untouched.
- README, iTAK section: iOS sends iTAK's position reports over cellular when the mesh Wi-Fi has no internet, so turn
  Cellular Data and Wi-Fi Assist off on the mesh; and close iTAK's server connection before moving it to the mesh.
- `itak_udp_dm_test.py`: 7 more checks (the server DM arrives without `<marti>`, otherwise byte for byte the same).

## 3.1.5: iTAK direct messages from the mesh
- **DMs sent by iTAK on the mesh now arrive.** iTAK sends direct messages by UDP to the address in a contact's
  position report, even when that address says tcp, and sends each one twice. The deck only listened on TCP, so iTAK's
  DMs to the deck (port 4242) and to server people through the DM relay (port 4243) were silently lost, while ATAK's
  worked. Deck Chat and the DM relay now also listen on UDP on the same ports, take XML and TAK protobuf, and deliver
  each message once (the relay remembers each message for 10 minutes). The TCP path is unchanged.
- README, iTAK section: iOS needs Local Network permission for iTAK, or the iPhone never appears on the mesh; on the
  mesh iTAK sends DMs but not All Chat Rooms.
- New test `itak_udp_dm_test.py`; it fails on 3.1.4.

## 3.1.4: copyright line
- README and THIRD_PARTY_NOTICES name the copyright holder. The license is unchanged (GPL-3.0-or-later).
  No program changes.

## 3.1.3: a phone coming back from the server is seen at once
- **A phone that moves from the server to the mesh reaches the server straight away.** Before, the bridge ignored
  every mesh report from a phone whose server position it had put on the mesh in the last 10 minutes, so a phone
  that walked back into mesh range after using cellular was invisible to the server for up to 10 minutes, and got no
  server -> mesh DMs. The bridge now recognises its copies of server reports by the phone's UID and the report's
  time: a copy coming back (from this deck or another deck on the mesh) is still never sent up, a phone's new report
  is. Reports without a time stamp use the old rule.
- README: a phone connected to the server directly gets its DMs from the server, not through the deck.
- New test `echo_test.py` (real bridge, fake server, phones and a second deck on the mesh); it fails on 3.1.2.

## 3.1.2: the wizard always finds a new deck
- **Ops shows "THIS DECK ISN'T SET UP YET" with a SET UP THIS DECK button** on every refresh until the TAK server is
  set up. Found on the first fresh-user install: opening Ops from the icon showed the dashboard, not the wizard.
- The check that opens the wizard now runs before Ops draws its panels, so an error in a panel can't stop it.
- "Skip" in the wizard only lasts until the browser is closed. 3.1 and 3.1.1 kept it for good in the browser,
  so a wipe of the deck didn't clear it; 3.1.2 erases that old setting.

## 3.1.1: certificate paths with ~ work
- **The bridge now expands `~` in the certificate paths** (`PYTAK_TLS_CLIENT_CERT`, `_KEY`, `_CAFILE`). PyTAK opens
  them exactly as written, so `~/certs/deck.pem`, the form shown in deck.ini.example and the README, made the bridge
  stop at start with "Resource not found", while `meshtoc check` (which does expand `~`) said the files were fine.
  Found by the first real fresh-user install with the setup wizard. The wizard now also writes full paths.
- A test now starts the real bridge with `~/` certificate paths and checks it connects to a TLS server with the
  deck's certificate. It fails on 3.1.

## 3.1: the setup wizard
- **Set up a new deck from the screen, no terminal.** On a deck whose TAK server isn't set up yet, Ops opens
  **TAK MESH TOC // SETUP**: check the deck, choose the OpenTAKServer data package, pick the mesh port, callsign,
  team and role, choose features, get the server helper for DMs, then save and restart.
  - **The data package is handled for you:** zips inside zips are opened, the certificates are converted to the PEM
    files the bridge reads (with the system's openssl), and the server address is read from the package.
  - **The password is asked only if needed:** the one in the package is used when it carries one.
  - **Test connection** uses the deck's certificate exactly the way the bridge will. It says when the server's
    certificate doesn't list the address you use (the usual case over Tailscale) and turns the name check off for
    you. It also catches a server that refuses this deck's certificate.
  - **Nothing is written until the last step**, which shows every deck.ini line it will change. deck.ini keeps all
    its comments, and the old one is kept as `deck.ini.before-setup-<time>`. Certificates go to `~/certs` (folder
    700, files 600).
  - **Restarts the services itself** where the user can use sudo without a password (the default on Raspberry Pi
    OS); otherwise it shows `meshtoc restart`. Then it runs the full check.
  - **Only the deck's own screen can open it**, like the restart button. The package size is capped, zip bombs are
    refused, and nothing from the zip is written to disk under its own name.
- **Run setup again** any time from Ops' SETUP dialog. SKIP TO OPS leaves the wizard.
- README: the wizard first; setup by hand stays, now noting OpenTAKServer's zip inside a zip.

## 3.0.3: clearer first-time setup, tidier code
- **A new deck shows one TODO, not three failures.** Until the TAK server is set up, `meshtoc check` shows one
  `[TODO] TAK server` line saying exactly what to set in deck.ini, and ends with "One thing left to set up". Ops'
  SETUP page shows one amber "TAK server not set up yet" item. The bridge is shown as waiting, not failed. Once the
  server is set up, the normal checks come back.
- **Ops reports problems with its own files.** If it can't read or write its event log or trend history, it says
  so once in its service log (`journalctl -u manet-dashboard`) instead of nothing. It still never stops over it.
- **A guard test for the two PyTAK patches** (big server events, a closed connection, socket tuning, the outbox),
  so a PyTAK upgrade that breaks them fails the tests instead of failing in the field.
- **Code tidy-up:** shorter file headers and class notes, old names and version labels out of comments, two
  out-of-date comments corrected, one helper instead of three copies.

## 3.0.2: honest delivery, safer outbox, bounded memory, tests that run anywhere
From two outside reviews (Gemini and ChatGPT) of 3.0.1.
- **Chats on their way to the server are synced to disk** (fsync) before they are sent, and the outbox file is
  replaced safely when it is tidied up, so a power cut can't lose them. If the disk write ever fails, the chat is
  still sent and Ops shows **NOT SAVED TO DISK** with the reason.
- **Delivery is reported honestly.** A chat counts as confirmed only when the server answers a later ping. If pings
  are off, or the server never answers them, Ops says chats are **sent, not confirmed** instead of treating
  "written" as delivered.
- **Memory stays bounded under a flood of made-up UIDs** (a buggy app or a hostile device): the bridge's per-phone
  tables stop at 10,000, phones already talking keep their place, and the cleanup runs at most once a second
  (before, it re-scanned the whole table on every packet once it passed 4,096). Chat's on-net list stops at 2,000.
- **Every service has a memory ceiling** (`MemoryMax=400M`): if one ever leaks, only that one restarts. It needs
  the kernel's memory accounting; where that is off, systemd ignores it.
- **Chat now uses the bundled takproto**, the same copy as the bridge. Before, it only decoded TAK Protocol
  (protobuf) mesh packets if takproto happened to be installed on the computer, so on a deck without it the
  **On net** list missed phones that send their positions in protobuf. Found by running the tests on a clean computer.
- **Tests run on a clean computer.** Tests that load the bridge's code now use the bundled PyTAK, takproto and
  protobuf (not copies installed on the computer, which can be different versions), run with `python3 -S` so that
  can't come back, no longer need BeautifulSoup, and make their own temporary folders.

## 3.0.1: more than one mesh node
- **Ops no longer tracks just one node.** `NODE_IP` can list several nodes (`NODE_NAME` gives their names in the
  same order), and when it is blank Ops finds the nodes on the mesh port by itself (the gateway, the DHCP server and the
  neighbours that serve a settings page). The mesh link is OK when any node answers; MESH + DECK lists each node, marks
  the nearest (fastest) one, and shows a node that went quiet as "not answering" in amber.
- **Mesh traffic counts.** If no node answers a ping but mesh traffic arrived in the last minute, the Mesh tile stays
  LINK OK (amber, TRAFFIC ONLY) and TAK BRIDGE keeps RELAYING. NODE LOST, and DEGRADED on the bridge, now mean no
  pings and no traffic. Before, moving the cable to another node showed NODE LOST and DEGRADED while the bridge was
  working fine.
- The event log notes each node going quiet or coming back, and the mesh link as a whole being lost or back.

## 3.0: TAK MESH TOC
- **New name: TAK MESH TOC**, a portable TAK operations center for a mesh network. It was MANET_MESH_BRIDGE.
  - The pages say **TAK MESH TOC // OPS** (the dashboard), **TAK MESH TOC // CHAT** and the VOICE page, with new icons in
    the same slate and steel-blue look: a mesh with a command post in the middle for Ops, a chat bubble of mesh nodes
    for Chat. The desktop icons are **TAK MESH TOC Ops** and **TAK MESH TOC Chat**.
  - The command is now **`meshtoc`** (`meshtoc check`, `meshtoc restart`, ...). `manet_mesh_bridge` still works.
  - The package is `TAK_MESH_TOC_v<version>.zip`. `meshtoc tidy` tidies old packages under either name.
  - Deck Chat is now **TAK MESH TOC Chat**: in ATAK's device info the deck's chat shows as "TAK MESH TOC Chat", and the bridge
    as "TAK MESH TOC".
- **Nothing to redo on an installed deck:** the folder (`~/manet_mesh_bridge`), `deck.ini`, the services
  (`manet-bridge`, `manet-chat`, `manet-dashboard`, `manet-voice`), voice settings and the Mumble pin all stay. The
  installer adds the `meshtoc` command and renames the desktop icons.

## 2.2.3: polish
- **Code comments:** version tags like "1.0.7:" and "used to..." removed from the code (that history lives here in
  CHANGES); old file names (`bridge.ini`, `dashboard.ini`, `deck_chat.ini`) replaced with the sections of `deck.ini`.
- **Pages:** Deck Chat's tooltips and help say "server" instead of "server/family"; the dashboard's alert and control
  notes point at `[dashboard]` in `deck.ini`.
- **Defaults:** Deck Chat and the dashboard read `~/manet_mesh_bridge/deck.ini` when started without `--config`
  (the services always pass it, so installed decks are unchanged).
- **One stylesheet per page:** the dashboard's and Deck Chat's styles were the old green look with the pro look layered
  on top. They are now merged into one stylesheet each (about a third shorter), with the theme layer, the glow
  variable and the last bunny styles gone. Checked element by element in a browser at several screen sizes: every
  page looks the same as before. The dashboard's status feed calls the bridge state `activity` instead of `bunny`.
- **Status colours back:** the dashboard's SETUP button turns amber or red when setup has problems, and in Deck
  Chat a QUEUED message's tick is amber and a HELD one red. (The pro look had turned both grey by accident.)
- **README:** a "who uses what" table for voice, the install example no longer names an old version, the test
  counts and needs are current.

## 2.2.2: tidy
- `tests/voice_test.py`: when run as root, the lab Mumble server runs as the user who called sudo (or `nobody`),
  instead of a fixed user name.
- A demo-data comment in the dashboard reworded. No change to what any program does.

## 2.2.1: voice docs, from real use
- **README, Voice:** how server-side ATAK phones use VX. They join the talk group's **Mumble** channel over
  Tailscale (a multicast talk group never crosses Tailscale). VX only trusts a Mumble server that uses the
  OpenTAKServer certificate; with Mumble's own certificate it says "missing or incompatible certificate". The README
  now has the commands to give Mumble the OTS certificate, and says to re-pin on the deck afterwards. Tested on a
  real server: VX on a server-side Android talks with the mesh through the deck.
- **README:** the Mumble config path on current packages is `/etc/mumble/mumble-server.ini`.
- No program changes.

## 2.2: voice, and one look
- **Voice (new, optional, off by default):** the `manet-voice` service carries OpenMANET's push-to-talk talk groups to
  Mumble channels on your server and back. ATAK phones on the mesh (VX plugin) and Mumble users (iPhone, PC) talk to
  each other. A one-talk-group prototype was field-tested on two OpenMANET nodes with VX phones and an
  iPhone on Mumble; this multi-talk-group version is lab-tested against a real Mumble server.
  - One talker per talk group, a "Channel busy" message for a Mumble user who talks over the mesh, a 60 s limit on a
    stuck transmit, no echo, no backlog.
  - TG1 is bridged by default; TG2–TG5 each have a **BRIDGE TO MUMBLE** switch on the VOICE page, linking to the
    Mumble channel of the same name. Remembered across restarts. Several talk groups can be bridged at once.
  - **LISTEN ON DECK:** play one talk group through the deck's speaker, with volume and a level meter.
  - Listening and the switches work on the deck's own screen only; from other devices the page is read-only.
  - Mumble over Tailscale with the server password and a pinned certificate. `check` prints the fingerprint to pin.
  - Standard library plus the system's libopus. Our own small Mumble client; no pip packages.
  - Accepts the VX plugin's Opus, which it labels RTP payload type 0.
- **Deck Chat has a VOICE page** (a tab next to CHAT): talk groups, who is talking, who is on each one (mesh and
  Mumble), recent transmissions, LISTEN ON DECK and the bridge switches. The VOICE tab shows a dot while someone is
  talking.
- **Dashboard:** a voice row in MESH + DECK (who is talking, or clear / Mumble down).
- **One look: pro.** The green and amber looks, the look button, `/night` `/green` `/pro` and `THEME` are gone. The
  bunny is gone too, and with it the dashboard's animation timer that redrew it 20 times a second, all day: less CPU
  and heat on the deck. A leftover `THEME` line is harmless; `check` says it can be deleted.
- **Installer:** installs `manet-voice` (it stays stopped while `VOICE = off`), opens UDP 38801-38810 on eth0 if UFW
  is on, and says if libopus is missing. `uninstall.sh` removes both.
- **The bridge** writes the mesh phones' IP addresses to its status file, so the VOICE page shows callsigns, not IPs.
- **Tests:** new `voice_test.py` (62 checks against a real Mumble server: audio both ways per talk group, two talk
  groups at once, busy floor, stuck transmit, VX payload type, certificate pinning, wrong password, server restart,
  listening on the deck, the bridge switches, the VOICE page in a browser, the `check` lines). `theme_test.py` is now
  `look_test.py` (60 checks, including the VOICE page and deck-screen-only controls).

## 2.1.1: dashboard fix
- **Fixed:** in a window shorter than full screen, the dashboard squeezed MESH + DECK down to a few pixels, so it could
  not be seen or scrolled (both looks, worst in pro). The server link and mesh + deck panels now share the column and
  each scrolls inside, and a very short window scrolls the whole page instead. Full screen on the deck looks the same.
- **Tests:** `theme_test.py` checks both looks at four window sizes (44 checks); the new checks fail on 2.1.

## 2.1 "TAK MESH BRIDGE PRO": the pro look
- **A third look, `pro`,** for the dashboard and Deck Chat: slate background, no glow, condensed headings, data in a
  fixed-width font, green only for "ok", steel blue for what is selected. Same information, same controls.
  - Dashboard: the bunny makes way for a large msg/min number.
  - Deck Chat: the roster moves to the left; messages carry a MESH or SERVER tag.
- **`THEME` in `[deck]`** (`green`, `amber` or `pro`) sets the look every screen opens in. The page's button (now
  GREEN / NIGHT / PRO) changes it for one browser. An unknown value opens green, and `check` warns about it.
- **Fonts bundled** in `common/fonts/` (SIL Open Font License), so the pro look needs no internet.
- **Tests:** new `tests/theme_test.py` (36 checks, including the classic look staying unchanged).

## 2.0: iTAK support, from real use
- **mesh_mailroute 2.1:** each mesh phone also gets a mail rule for its device ID. iTAK addresses DMs by device ID
  (ATAK uses callsigns), so with helper 2.0 iTAK's DMs to mesh phones were dropped at the server. Tested with an iTAK-style
  DM; helper 2.0 fails that test.
- **The deck has a team by default:** the example config sets `TEAM = Cyan` and `ROLE = HQ`. iTAK only lists the deck
  as a person you can message when it has a team. `manet_mesh_bridge check` warns when none is set.
- **README:** a new "iTAK (iPhone) users" section covers four things:
  - the deck's team
  - reconnecting after a callsign change (the server keeps the first name it saw)
  - device-ID DMs
  - logins for video in iTAK's stream entry, with how to read MediaMTX's "invalid SETUP path"

## 1.0.10: first public release
**MANET_MESH_BRIDGE** began as a private bridge for one deck, developed and field-tested through versions 1.0 to
1.0.9. This first public release is that code, cleaned up for anyone to install.

**Fixed**
- **Startup freeze:** a bridge started with more than 99 chats waiting in the outbox froze at startup (1.0.9). The
  outbox is now replayed once the sender is running.
- **Repeated chats:** a server that never answers pings no longer gets the same chats again on every restart.

**Installing and running**
- **Installs offline.** PyTAK 7.6.1, takproto 3.0.1 and protobuf 6.33.6 are bundled in `vendor/`.
  - The bridge runs with `python3 -S`, which uses only those libraries, never other copies installed on the computer.
    Mixing versions was found to break imports.
- **New names:** the command `manet_mesh_bridge`, the folder `~/manet_mesh_bridge`, and the services `manet-bridge`,
  `manet-chat` and `manet-dashboard`, grouped by `manet_mesh_bridge.target`.
- **`manet_mesh_bridge check` checks more:** the bundled libraries, the certificate files and their permissions
  (with `.p12` conversion help), the server link round trip, the clock and the hardware watchdog.
- **The installer:**
  - re-checks that the bundled libraries load
  - notes other programs already using the deck's ports
  - repairs missing services or a missing command, instead of saying "nothing to do"

**Configuration**
- No personal defaults:
  - **Server:** comes from `COT_URL` only.
  - **Mesh node:** `NODE_IP`, or else the gateway on the mesh port.
  - **Example config:** written for a new user.
- **New defaults:** `RETURN_PATH = 1`, and `DM_RELAY = 0` until the server helper is installed.

**Project files**
- **Server helper:** `server/mesh_mailroute` (version 2.0) is included. It refuses to run without the deck's UID,
  instead of guessing it.
- **License:** GPL-3.0-or-later, with third-party notices.

**Tests**
- New: `tests/privacy_test.py` and `tests/install_sim.sh`.

## Before 1.0.10 (private versions, summarized)
- **1.0.9:**
  - DM route check: a test message every minute through the server helper.
  - Outbox: chats and DMs are kept until the server has confirmed them, and resent after a lost link.
- **1.0.8:**
  - The mesh packet check accepts what OpenTAKServer accepts. Padding and junk are cut off, and multi-event packets
    are split.
  - The bridge refuses an untested PyTAK version.
- **1.0.7:** server-only mode, and switching by itself when a mesh node is plugged in.
- **1.0.6:**
  - A forgiving `deck.ini`: a setting written twice, a Windows BOM, `%` in values.
  - Malformed mesh packets are dropped at the deck.
- **1.0.5:**
  - The server link watchdog (pings and reconnects).
  - Uplink priority, with the newest position per phone.
  - A fair return path.
  - Server link rows on the dashboard.
- **1.0.4:** hardening found by fuzzing:
  - regex limits
  - server events over 64 KB
  - Deck Chat input checks
  - a GPS reader that survives bad data
- **1.0 to 1.0.3:**
  - One package with one config file, replacing three separate programs.
  - Read-only releases and rollback.
  - DMs across the bridge, GPS position, and Deck Chat.
