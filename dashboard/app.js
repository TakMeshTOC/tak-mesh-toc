/* app.js - the dashboard page logic. Polls /api/status (every 2 s), /api/history (10 s). */
(function () {
  'use strict';
  var $ = function (id) { return document.getElementById(id); };
  var q = location.search || '';
  var params = {};
  q.replace(/^\?/, '').split('&').forEach(function (kv) { if (kv) { var p = kv.split('='); params[decodeURIComponent(p[0])] = decodeURIComponent(p[1] || ''); } });
  var lastOk = 0, data = null, hist = null, tick = 0;
  var activeTab = 'video';

  /* ---------------- helpers ---------------- */
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
  function pad(n) { return (n < 10 ? '0' : '') + n; }
  function hms(t) { var d = new Date(t * 1000); return pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds()); }
  function age(s) { if (s == null) return 'never'; s = Math.round(s); if (s < 90) return s + 's ago'; if (s < 5400) return Math.round(s / 60) + 'm ago'; return (s / 3600).toFixed(1) + 'h ago'; }
  function dur(s) { if (s == null) return '-'; s = Math.round(s); var d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60); return (d ? d + 'd ' : '') + (h || d ? h + 'h ' : '') + m + 'm'; }
  function row(k, v, cls) { return '<dt>' + esc(k) + '</dt><dd class="' + (cls || '') + '">' + v + '</dd>'; }
  function store(k, v) { try { if (v === undefined) return localStorage.getItem('deck.' + k); localStorage.setItem('deck.' + k, v); } catch (e) { return null; } }
  function css(n) { return getComputedStyle(document.documentElement).getPropertyValue(n).trim(); }
  function getJSON(url, ok, bad) {
    var x = new XMLHttpRequest();
    x.open('GET', url, true); x.timeout = 5000;
    x.onload = function () { try { ok(JSON.parse(x.responseText)); } catch (e) { if (window.console) console.error(url, e); if (bad) bad(); } };
    x.onerror = x.ontimeout = function () { if (bad) bad(); };
    x.send();
  }
  function post(url, body, cb) {
    var x = new XMLHttpRequest();
    x.open('POST', url, true); x.timeout = 40000;
    x.setRequestHeader('Content-Type', 'application/json'); x.setRequestHeader('X-Deck', '1');
    x.onload = function () { var r = {}; try { r = JSON.parse(x.responseText); } catch (e) { r = { ok: false, error: 'bad reply' }; } cb(r); };
    x.onerror = x.ontimeout = function () { cb({ ok: false, error: 'no answer from the dashboard' }); };
    x.send(JSON.stringify(body));
  }
  function canControl() { return !!(data && data.local && data.caps && data.caps.control); }

  /* ---------------- colors (one look: pro) ---------------- */
  function colors() { return { green: css('--ok'), amber: css('--amber'), red: css('--red') }; }

  /* ---------------- clock ---------------- */
  function clock() {
    var d = new Date();
    $('clock').innerHTML = d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()) + ' <b>' + pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds()) + '</b>';
    $('stale').style.display = (Date.now() - lastOk > 7000 && lastOk) ? 'inline' : 'none';
  }

  /* ---------------- tabs ---------------- */
  function showTab(name, save) {
    if (name === 'trends') name = 'mesh';                   /* the MESH tab replaced TRENDS in 3.1.8 */
    if (['video', 'mesh'].indexOf(name) < 0) name = 'video';
    activeTab = name;
    Array.prototype.forEach.call(document.querySelectorAll('.tabs button'), function (b) { b.setAttribute('aria-selected', b.getAttribute('data-tab') === name ? 'true' : 'false'); });
    ['video', 'mesh'].forEach(function (t) { $('tab-' + t).hidden = t !== name; });
    if (save) store('tab', name);
    if (name === 'mesh') { if (data) meshTab(data); setTimeout(drawCharts, 30); }
    if (name === 'video') renderCenterState();
    renderCenterState();
  }
  Array.prototype.forEach.call(document.querySelectorAll('.tabs button'), function (b) { b.addEventListener('click', function () { showTab(b.getAttribute('data-tab'), true); }); });

  /* ---------------- tiles, banner and sparklines ---------------- */
  function tile(id, t) {
    var e = $('t-' + id);
    e.className = 'tile lv-' + t.level + (t.alert ? ' has-alert' : '');
    e.querySelector('.v').textContent = t.text;
    e.querySelector('.sub').textContent = t.alert ? '▲ ' + t.alert : '';
  }
  function gpsTile(g) {
    var e = $('t-gps');
    if (!e) return;
    if (!g) { e.className = 'tile gps lv-info'; $('g-fix').textContent = 'OFF'; $('g-grid').textContent = '-- -- ----- -----'; $('g-det').textContent = 'GPS not set up'; return; }
    e.className = 'tile gps lv-' + g.level;
    $('g-fix').textContent = g.fix;
    $('g-grid').textContent = g.grid || '-- -- ----- -----';
    $('g-det').textContent = g.detail || '';
  }
  function banner(d) {
    var el = $('alerts'), al = d.alerts || [];
    if (!al.length) { el.className = ''; el.innerHTML = ''; return; }
    var worst = al.some(function (a) { return a.level === 'bad'; }) ? 'bad' : 'warn';
    el.className = 'on lv-' + worst;
    el.innerHTML = al.map(function (a) { return '<span class="lv-' + a.level + '" style="color:var(--c)"><b>' + (a.level === 'bad' ? 'ALERT' : 'WARN') + '</b>' + esc(a.text) + ' (' + esc(a.value) + ')</span>'; }).join('');
  }
  function sparks() {
    if (!hist || !hist.spark) return;
    var sp = hist.spark, N = 90;
    function tail(a) { return (a || []).slice(-N); }
    var map = { bridge: ['msgs', 0], server: ['srv', null], video: ['kbps', 0], mesh: ['rtt', 0] };
    Object.keys(map).forEach(function (k) {
      var el = $('t-' + k), cv = el.querySelector('canvas.sp');
      Charts.spark(cv, tail(sp[map[k][0]]), getComputedStyle(el).getPropertyValue('--c').trim() || css('--ok'), { min: map[k][1] });
    });
  }

  /* ---------------- bridge panel ---------------- */
  function bridgePanel(d) {
    var b = d.bridge, bn = d.activity;
    var lvl = { hop: 'ok', sleep: 'ok', alert: 'warn', down: 'bad' }[bn.state];
    $('b-state').textContent = { hop: 'RELAYING', sleep: 'SLEEPING', alert: 'DEGRADED', down: 'DOWN' }[bn.state];
    $('p-bridge').className = 'panel grow lv-' + lvl;
    $('b-say').textContent = bn.text;
    $('b-rate').textContent = b.msgs_per_min;
    var pm = b.per_minute || { up: {}, down: {} }, max = 1;
    ['position', 'chat', 'marker', 'other'].forEach(function (k) { max = Math.max(max, (pm.up || {})[k] || 0, (pm.down || {})[k] || 0); });
    var names = { position: 'positions', chat: 'chat', marker: 'markers', other: 'other' };
    function dirRows(dir, title) {
      var h = '<div class="dirhead">' + title + '  (' + ((pm[dir] || {}).total || 0) + ')</div>';
      ['position', 'chat', 'marker', 'other'].forEach(function (k) {
        var n = (pm[dir] || {})[k] || 0;
        if (!n && k === 'other') return;
        h += '<span class="lab">' + names[k] + '</span><span class="bar"><i style="width:' + Math.round(100 * n / max) + '%"></i></span><span class="num">' + n + '</span>';
      });
      return h;
    }
    $('b-flow').innerHTML = dirRows('up', 'MESH &rarr; SERVER, LAST 60 s') + (b.return_enabled ? dirRows('down', 'SERVER &rarr; MESH, LAST 60 s') : '');
    var r = '';
    if (b.service !== 'active') r += row('service', esc(b.service), 'lv-bad');
    r += row('uptime', dur(b.uptime));
    r += row('last mesh msg', b.mesh_last_age == null ? 'nothing yet' : age(b.mesh_last_age) + (b.mesh_last_from ? '<span class="ip"> (' + esc(b.mesh_last_from) + ')</span>' : ''));
    r += row('forwarded', b.mesh_events.toLocaleString() + ' &uarr;' + (b.return_enabled ? ' ' + b.return_sent.toLocaleString() + ' &darr;' : ''));
    if (b.mesh_failed) r += row('convert failures', b.mesh_failed, 'lv-warn');
    if (b.mesh_malformed) r += row('malformed (dropped)', b.mesh_malformed, 'lv-warn');
    if (b.mesh_skipped) r += row('skipped (no takproto)', b.mesh_skipped, 'lv-warn');
    if (!b.has_status) r += row('status file', 'missing: restart the bridge once', 'lv-warn');
    $('b-rows').innerHTML = r;
  }

  /* ---------------- server panel ---------------- */
  function serverPanel(d) {
    var s = d.server, lvl = s.tile, L = s.link, silent = false;
    // the bridge's pings say whether the server really answers (a dead link can still look ESTABLISHED)
    if (L && L.state === 'up' && L.answers_pings && L.last_pong_age != null && L.last_pong_age > 2 * L.ping_interval) { silent = true; if (lvl === 'ok') lvl = 'warn'; }
    $('s-state').textContent = silent ? 'NO ANSWER' : { ok: 'CONNECTED', warn: 'NOT LINKED', bad: 'UNREACHABLE' }[lvl];
    $('p-server').className = 'panel lv-' + lvl;
    var r = '';
    r += row('TLS link', s.established ? '<b>ESTABLISHED</b>' : 'not connected', s.established ? '' : (s.reachable ? 'lv-warn' : 'lv-bad'));
    if (L) {
      if (L.state === 'down') r += row('server answers', 'reconnecting: ' + esc(L.reason), 'lv-warn');
      else if (!L.answers_pings) r += row('server answers', 'no ping answers (watchdog off)', '');
      else if (silent) r += row('server answers', '<b>NONE for ' + Math.round(L.last_pong_age) + ' s</b> (reconnects at ' + L.timeout + ')', 'lv-warn');
      else if (L.rtt_ms != null) r += row('server answers', 'yes, <b>' + L.rtt_ms + ' ms</b> round trip', '');
      else r += row('server answers', 'waiting for the first answer', '');
      if (L.state !== 'down' && L.since_age != null) r += row('connected', (L.since_age < 120 ? Math.round(L.since_age) + ' s' + (L.starts > 1 ? ' <span class="ip">(just reconnected)</span>' : '') : 'for ' + dur(L.since_age)), L.since_age < 120 && L.starts > 1 ? 'lv-warn' : '');
      if (L.starts > 1) { r += row('reconnects', (L.starts - 1) + ' since boot, last ' + age(L.prev_down_age)); if (L.prev_reason) r += row('last cause', esc(L.prev_reason)); }
    }
    var D = s.dm_route;   // a message to a made-up callsign must come back through the server
    if (D) {
      if (D.state === 'ok') r += row('DM route', 'OK' + (D.rtt_ms != null ? ', <b>' + D.rtt_ms + ' ms</b>' : ''), '');
      else if (D.state === 'broken') r += row('DM route', '<b>BROKEN</b> for ' + dur(D.since_age || 0) + ' (mesh_mailroute on the server?)', 'lv-warn');
      else r += row('DM route', 'checking' + (D.sent ? ' (' + D.sent + ' sent, none back yet)' : '…'), '');
    }
    var O = s.outbox;     // chats kept on disk until written to the server
    if (O && O.pending) r += row('chats waiting', '<b>' + O.pending + '</b>, oldest ' + dur(O.oldest_age || 0) + ' (sent when the link is back)', 'lv-warn');
    else if (O && O.replayed) r += row('chats waiting', 'none (' + O.replayed + ' sent after the restart)', '');
    if (O && !O.can_confirm) r += row('chat delivery', 'sent, <b>not confirmed</b>: the server does not answer pings' + (O.sent_unconfirmed ? ' (' + O.sent_unconfirmed + ' so far)' : ''), 'lv-warn');
    if (O && O.write_errors) r += row('chat outbox', '<b>NOT SAVED TO DISK</b> (' + O.write_errors + 'x): ' + esc(O.last_write_error), 'lv-bad');
    r += row('server', s.host ? esc(s.host) + ':' + s.port : 'shown on the deck only', 'nw');
    r += row('reachable', s.reachable ? 'yes, ' + s.rtt_ms + ' ms' : 'NO', s.reachable ? '' : 'lv-bad');
    r += row('from server', (s.last_rx_age == null ? 'nothing yet' : age(s.last_rx_age)) + ' (' + s.rx_events.toLocaleString() + ')');
    r += row('tailscale', s.tailscale_up ? 'up' + (s.tailscale_ip ? ', ' + esc(s.tailscale_ip) : '') : 'DOWN', (s.tailscale_up ? '' : 'lv-warn ') + 'nw');
    $('s-rows').innerHTML = r;
  }

  /* ---------------- mesh + deck panel ---------------- */
  function deckPanel(d) {
    var m = d.mesh, k = d.deck;
    $('d-state').textContent = m.state || { ok: 'LINK OK', warn: 'CHECK LINK', bad: 'NODE LOST' }[m.tile];
    $('p-deck').className = 'panel grow lv-' + m.tile;
    var r = '', nodes = m.nodes || [];
    $('d-bluf').innerHTML = bluf(m);
    $('d-bluf').hidden = !nodes.length;
    if (!nodes.length) r += row('mesh nodes', 'none set or found (NODE_IP in deck.ini)', 'lv-warn');
    if ('traffic_age' in m) r += row('mesh traffic', m.traffic_age == null ? 'none yet' : age(m.traffic_age) + (m.traffic && !m.node_ok ? ', mesh is up' : ''),
      m.traffic ? '' : (m.tile === 'bad' ? 'lv-bad' : 'lv-warn'));
    if (m.loss_pct != null) r += row('loss / jitter', m.loss_pct + '% / ' + (m.jitter == null ? '-' : m.jitter + ' ms'), m.loss_pct > 10 ? 'lv-warn' : '');
    r += voiceRow(d.voice);
    r += row(k.iface, (k.iface_up ? 'up' : 'DOWN') + (k.iface_ip ? ', ' + esc(k.iface_ip) : ''), (k.iface_up ? '' : 'lv-bad ') + 'nw');
    r += row('MTU', k.mtu == null ? '?' : k.mtu + (k.mtu_ok ? ' ok' : ' (expect ' + k.mtu_expected + ')'), k.mtu_ok ? '' : 'lv-warn');
    r += row('internet', k.internet ? 'ok' : 'none', k.internet ? '' : 'lv-warn');
    if (k.ntp_synced != null) r += row('clock', k.ntp_synced ? 'synced' : 'NOT synced', k.ntp_synced ? '' : 'lv-warn');
    r += row('cpu temp', '<canvas class="rsp" id="cpu-sp"></canvas>' + (k.cpu_temp == null ? '-' : k.cpu_temp + ' C'), k.cpu_temp != null && k.cpu_temp > 75 ? 'lv-warn' : '');
    r += row('load / mem', k.load[0] + ' / ' + (k.mem_pct == null ? '-' : k.mem_pct + '%'));
    r += row('deck uptime', dur(k.uptime));
    if (k.throttled) r += row('power/thermal', 'flags ' + esc(k.throttled), 'lv-warn');
    $('d-rows').innerHTML = r;
    if (hist && hist.spark) Charts.spark($('cpu-sp'), (hist.spark.temp || []).slice(-90), css('--plot'), {});
  }
  $('d-bluf').addEventListener('click', function (e) { if (e.target.closest && e.target.closest('button')) showTab('mesh', true); });

  /* the one-glance mesh summary at the top of MESH + DECK */
  function bluf(m) {
    var nodes = m.nodes || [], up = nodes.filter(function (n) { return n.ok; }), lost = nodes.filter(function (n) { return !n.ok; });
    var lvl = !up.length ? 'bad' : lost.length ? 'warn' : 'ok';
    var big = !up.length ? (m.traffic ? 'NO NODE ANSWERS' : 'NODE LOST') : nodes.length === 1 ? '1 NODE \u00b7 OK' :
      lost.length ? up.length + ' OF ' + nodes.length + ' NODES' : nodes.length + ' NODES \u00b7 ALL OK';
    var near = up.filter(function (n) { return n.nearest; })[0], links = m.links || [], s = [];
    var slow = null;
    links.forEach(function (l) { if (l.mbps != null && (!slow || l.mbps < slow.mbps)) slow = l; });
    if (slow) s.push('slowest link ' + slow.mbps + ' Mbit/s ' + slow.grade);
    else if (nodes.length > 1) s.push('no radio links reported');
    if (near) s.push(esc(nodeLabel(near)) + ' ' + near.rtt_ms + ' ms');
    var h = '<div class="bluf lv-' + lvl + '"><button type="button">MESH TAB &rsaquo;</button><div class="big">' + big + '</div><div class="s">' + s.join(' \u00b7 ') + '</div>';
    lost.forEach(function (n) { h += '<div class="lost">' + esc(nodeLabel(n)) + ' lost \u00b7 not answering</div>'; });
    return h + '</div>';
  }

  /* ---------------- MESH tab ---------------- */
  function pct(v) { return v == null ? '-' : Math.round(v) + '%'; }
  function meshTab(d) {
    if (activeTab !== 'mesh' || !d.mesh) return;
    var m = d.mesh, nodes = m.nodes || [], links = m.links || [], disc = m.discovery || {};
    $('mt-src').textContent = disc.source === 'alfred' ? (disc.ok ? "from the mesh's own list (alfred) \u00b7 " + nodes.length : 'mesh list unavailable: ' + (disc.why || '?'))
                                                       : 'from NODE_IP in deck.ini \u00b7 ' + nodes.length;
    var h = '<tr><th>NODE</th><th>IP</th><th>PING</th><th>CPU</th><th>LOAD</th><th>MEM</th><th>UP</th><th>STATUS</th></tr>';
    nodes.forEach(function (n) {
      var lv = n.ok ? 'ok' : 'warn', old = n.ok ? '' : ' class="dimc" title="last known"';
      h += '<tr class="lv-' + lv + '"><td>' + esc(nodeLabel(n)) + (n.nearest && nodes.length > 1 ? '<span class="tag">NEAREST</span>' : '') + '</td><td>' + esc(n.ip) + '</td>' +
        '<td>' + (n.ok ? n.rtt_ms + ' ms' : '-') + '</td><td' + old + '>' + (n.cpu_c == null ? '-' : Math.round(n.cpu_c) + 'C') + '</td>' +
        '<td' + old + '>' + pct(n.load_pct) + '</td><td' + old + '>' + pct(n.mem_pct) + '</td><td' + old + '>' + (n.uptime == null ? '-' : dur(n.uptime)) + '</td>' +
        '<td class="st">' + (n.ok ? 'OK' : 'LOST') + '</td></tr>';
    });
    $('mt-nodes').innerHTML = nodes.length ? h : '<tr><td class="dimc">no nodes found yet</td></tr>';
    $('mt-grades').textContent = 'HaLow throughput \u00b7 GOOD \u2265' + m.link_good + ' \u00b7 OK ' + m.link_poor + '\u2013' + m.link_good + ' \u00b7 POOR <' + m.link_poor + ' Mbit/s';
    var top = Math.max(10, m.link_good * 2);
    $('mt-links').innerHTML = links.length ? links.map(function (l) {
      var lv = { GOOD: 'ok', OK: 'warn', POOR: 'bad' }[l.grade] || 'info';
      var w = l.mbps == null ? 0 : Math.min(100, Math.round(100 * l.mbps / top));
      return '<div class="lk lv-' + lv + '"><span>' + esc(l.a) + ' \u2194 ' + esc(l.b) + '</span><span class="bar"><i style="width:' + w + '%"></i></span><span class="num">' +
        (l.mbps == null ? 'speed not reported' : l.mbps + ' Mbit/s ' + l.grade) + '</span></div>';
    }).join('') : '<div class="none">' + (nodes.length > 1 ? 'no radio links reported yet' : 'one node: no radio links to show') + '</div>';
  }

  /* a node found on the port (not named in deck.ini) is shown by the end of its address, e.g. .254.20 */
  function nodeLabel(n) { return n.found ? '.' + n.ip.split('.').slice(2).join('.') : n.name; }

  /* voice row: who is talking on which talk group (manet-voice; the Voice page in Chat has the detail) */
  function voiceRow(v) {
    if (!v) return '';
    if (!v.running) return row('voice', 'not running');
    var talk = v.groups.filter(function (g) { return g.talking; });
    var br = v.groups.filter(function (g) { return g.bridged; });
    var down = br.filter(function (g) { return !g.mumble_up; });
    if (talk.length) return row('voice', talk.map(function (g) {
      return '<b>&#9679; ' + esc(g.talking.name) + '</b> ' + esc(g.name) + (g.talking.via === 'Mumble' ? ' (Mumble)' : '');
    }).join(', '), 'lv-ok');
    if (!br.length) return row('voice', 'watching, not bridged');
    if (down.length) return row('voice', 'clear \u00b7 Mumble DOWN (' + down.map(function (g) { return esc(g.name); }).join(', ') + ')', 'lv-warn');
    var on = br.reduce(function (n, g) { return n + g.mesh + g.mumble; }, 0);
    return row('voice', 'clear \u00b7 ' + br.map(function (g) { return esc(g.name); }).join(', ') + ' \u2194 Mumble' + (on ? ' \u00b7 ' + on + ' on' : ''));
  }

  /* ---------------- video: streams, 1-up / 2-up, snapshot, record ---------------- */
  var mode = 'webrtc', two = false, active = 0;
  var slots = [{ path: '', playing: false }, { path: '', playing: false }];
  var filesOpen = false;

  function pathUrl(path) { return path.split('/').map(encodeURIComponent).join('/'); }
  function playerUrl(path) {
    if (!path || !data) return '';
    var port = mode === 'hls' ? data.video.hls_port : data.video.webrtc_port;
    return location.protocol + '//' + location.hostname + ':' + port + '/' + pathUrl(path) + (mode === 'hls' ? '/' : '');
  }
  function curPath() { var s = slots[active]; return (data && !data.video.api_ok && $('v-manual').value) ? $('v-manual').value.trim() : s.path; }
  function buildPlayers() {
    var box = $('v-players'); box.className = 'players' + (two ? ' two' : '');
    var html = '';
    for (var i = 0; i < (two ? 2 : 1); i++) {
      var s = slots[i];
      html += '<div class="player' + (two && i === active ? ' active' : '') + '" data-i="' + i + '">';
      if (s.playing && s.path) { var r = rotOf(s.path); html += '<span class="cap">' + esc(s.path) + '</span><span class="chips"><button type="button" class="rot fs" data-fs="' + i + '" aria-pressed="false" title="Full screen, keeping the rotation (Esc to leave)">&#9974;</button><button type="button" class="rot" data-rot="' + i + '" aria-pressed="' + (r !== 0) + '" title="Rotate this stream 90 degrees (remembered per stream; the deck screen only)">&#10227; ' + r + '&deg;</button></span><iframe title="stream ' + (i + 1) + '" allow="autoplay" src="' + esc(playerUrl(s.path)) + '"></iframe>'; }
      else html += '<div class="hint" data-hint="' + i + '"></div>';
      html += '</div>';
    }
    box.innerHTML = html;
    hints(); fitRot();
  }
  // rotate (display only): a stream sent sideways, e.g. a phone worn on the chest, is turned upright on this screen
  function rotOf(path) { var r = parseInt(store('rot:' + path) || '0', 10); return [0, 90, 180, 270].indexOf(r) >= 0 ? r : 0; }
  function fitRot() {
    Array.prototype.forEach.call(document.querySelectorAll('#v-players .player'), function (p) {
      var f = p.querySelector('iframe'), s = slots[+p.getAttribute('data-i')]; if (!f || !s) return;
      var r = rotOf(s.path), w = p.clientWidth, h = p.clientHeight, side = r === 90 || r === 270;
      f.className = r ? 'turned' : '';
      f.style.width = r ? (side ? h : w) + 'px' : ''; f.style.height = r ? (side ? w : h) + 'px' : '';
      f.style.transform = r ? 'translate(-50%, -50%) rotate(' + r + 'deg)' : '';
    });
  }
  window.addEventListener('resize', fitRot);
  document.addEventListener('fullscreenchange', function () {
    Array.prototype.forEach.call(document.querySelectorAll('#v-players .fs'), function (b) { b.setAttribute('aria-pressed', document.fullscreenElement === b.closest('.player')); });
    setTimeout(fitRot, 50);
  });
  function hints() {
    if (!data) return;
    var v = data.video, paths = v.paths || [];
    for (var i = 0; i < (two ? 2 : 1); i++) {
      var el = document.querySelector('[data-hint="' + i + '"]'); if (!el) continue;
      var s = slots[i], cur = paths.filter(function (p) { return p.name === s.path; })[0];
      var pre = two ? 'Screen ' + (i + 1) + ': ' : '';
      if (s.stopped) el.innerHTML = pre + 'Stopped. Press <b>Play</b> to watch again.';
      else if (!v.api_ok && v.running) el.innerHTML = pre + 'Status API is off, so the list is empty.<br>Type a stream path above and press <b>Play</b>.';
      else if (s.path && cur && cur.ready) el.innerHTML = pre + 'Press <b>Play</b> to watch <b>' + esc(cur.name) + '</b>';
      else el.innerHTML = pre + 'Pick a live stream' + (two ? ' for this screen' : '') + ', then press <b>Play</b>';
    }
  }
  function play(i) {
    if (!data) return;
    if (i == null) i = active;
    if (!data.video.api_ok) slots[i].path = $('v-manual').value.trim() || slots[i].path;
    if (!slots[i].path) return;
    slots[i].playing = true; slots[i].stopped = false; store('sel' + i, slots[i].path);
    buildPlayers(); renderStreams();
  }
  function stopAll() { slots.forEach(function (s) { s.playing = false; s.stopped = true; }); buildPlayers(); }
  function renderStreams() {
    var v = data.video, paths = v.paths || [], html = '';
    paths.forEach(function (p) {
      var info = [];
      if (p.codecs && p.codecs.length) info.push(p.codecs.join('+'));
      if (p.ready && p.kbps != null) info.push(p.kbps + ' kbps');
      var used = slots.slice(0, two ? 2 : 1).map(function (s, i) { return s.path === p.name ? (two ? (i + 1) : '▶') : ''; }).join('');
      html += '<button type="button" class="stream ' + (p.stalled ? 'lv-warn' : p.ready ? 'lv-ok' : 'off') + '" data-n="' + esc(p.name) + '" aria-pressed="' + (used !== '') + '">' +
        '<span class="dot"></span><span class="nm">' + esc(p.name) + (used ? '  [' + used + ']' : '') + '</span>' +
        '<span class="meta">' + esc(info.join('  ')) + '</span>' +
        '<span class="meta' + (p.stalled ? ' stall' : '') + '">' + (p.stalled ? 'STALLED' : p.ready ? (p.readers ? 'watching ' + p.readers : 'live') : 'idle') + '</span></button>';
    });
    if (!paths.length) html = '<div class="empty">' + (v.running ? 'No streams yet.' : 'MediaMTX is not running.') + ' Publish from OpenTAK ICU to <b>rtsp://' + esc(data.deck_ip || 'deck') + ':' + v.rtsp_port + '/test3</b></div>';
    $('v-list').innerHTML = html;
  }
  function videoPanel(d) {
    var v = d.video, paths = v.paths || [];
    var live = paths.filter(function (p) { return p.ready; }).length;
    $('p-center').style.setProperty('--c', v.running ? 'var(--g)' : 'var(--amber)');
    renderStreams();
    var manual = !v.api_ok;
    $('v-manual').hidden = !manual;
    if (manual && !$('v-manual').value) $('v-manual').value = store('manual') || v.default_stream;
    // choose default streams once
    slots.forEach(function (s, i) {
      if (s.path) return;
      var saved = store('sel' + i);
      if (saved && paths.some(function (p) { return p.name === saved; })) s.path = saved;
      else {
        var liveOnes = paths.filter(function (p) { return p.ready; });
        var pick = liveOnes[i] || liveOnes[0] || paths[i] || paths[0];
        if (pick) s.path = pick.name;
      }
    });
    if (!document.querySelector('#v-players .player')) buildPlayers(); else hints();
    // snapshot / record availability
    var caps = d.caps || {}, ok = canControl() && caps.ffmpeg;
    var title = !d.local ? 'Only works on the deck\'s own screen' : !caps.control ? 'Controls are off (CONTROL = off)' : !caps.ffmpeg ? 'ffmpeg is not installed on the deck' : '';
    var p0 = curPath();
    var curLive = paths.filter(function (p) { return p.name === p0 && p.ready; }).length > 0;
    $('b-snap').disabled = !ok || !curLive; $('b-snap').title = title || 'Save a still from the selected stream';
    renderCenterState();
  }
  var noteLock = 0;
  function setNote(msg, err, html) {
    var n = $('v-note'); n.className = 'note' + (err ? ' err' : '');
    if (html) n.innerHTML = msg; else n.textContent = msg;
  }
  function flashNote(msg, err, html) { setNote(msg, err, html); noteLock = Date.now(); setTimeout(function () { if (Date.now() - noteLock >= 5900) { noteLock = 0; setNote('', false); } }, 6000); }
  function renderCenterState() {
    if (!data) return;
    var t = '';
    if (activeTab === 'video') { var l = (data.video.paths || []).filter(function (p) { return p.ready; }).length; t = !data.video.running ? 'MEDIAMTX OFF' : l + ' LIVE'; }
    else { var mm = data.mesh || {}; t = (mm.nodes_total || 0) + ' NODES \u00b7 ' + (mm.links || []).length + ((mm.links || []).length === 1 ? ' LINK' : ' LINKS'); }
    $('c-state').textContent = t;
  }
  $('b-play').addEventListener('click', function () { play(); });
  $('b-stop').addEventListener('click', stopAll);
  function setMode(m) { mode = m; $('m-webrtc').setAttribute('aria-pressed', m === 'webrtc'); $('m-hls').setAttribute('aria-pressed', m === 'hls'); if (slots.some(function (s) { return s.playing; })) buildPlayers(); }
  $('m-webrtc').addEventListener('click', function () { setMode('webrtc'); });
  $('m-hls').addEventListener('click', function () { setMode('hls'); });
  $('m-two').addEventListener('click', function () {
    two = !two; $('m-two').setAttribute('aria-pressed', two); active = 0;
    if (two && !slots[1].path && data) { var other = (data.video.paths || []).filter(function (p) { return p.name !== slots[0].path; })[0]; if (other) slots[1].path = other.name; }
    buildPlayers(); if (data) renderStreams();
    if (two) flashNote('2-up doubles the video traffic on the mesh. Tap a screen, then pick its stream.', false);
  });
  $('v-players').addEventListener('click', function (e) {
    var fb = e.target.closest ? e.target.closest('.fs') : null;
    if (fb) {
      // full screen the whole player (not the video inside it), so the rotation comes along
      var pl = fb.closest('.player');
      try { if (document.fullscreenElement === pl) document.exitFullscreen(); else pl.requestFullscreen(); } catch (err) {}
      e.stopPropagation(); return;
    }
    var rb = e.target.closest ? e.target.closest('.rot') : null;
    if (rb) {
      var s = slots[+rb.getAttribute('data-rot')]; if (!s || !s.path) return;
      var r = (rotOf(s.path) + 90) % 360; store('rot:' + s.path, String(r));
      rb.innerHTML = '&#10227; ' + r + '&deg;'; rb.setAttribute('aria-pressed', r !== 0); fitRot();
      e.stopPropagation(); return;
    }
    var p = e.target.closest ? e.target.closest('.player') : null; if (!p || !two) return;
    active = +p.getAttribute('data-i');
    Array.prototype.forEach.call(document.querySelectorAll('#v-players .player'), function (x) { x.className = 'player' + (+x.getAttribute('data-i') === active ? ' active' : ''); });
  });
  $('v-list').addEventListener('click', function (e) {
    var b = e.target.closest ? e.target.closest('.stream') : null; if (!b) return;
    var chosen = b.getAttribute('data-n');
    if (two && slots[1 - active].path === chosen) slots[1 - active].path = slots[active].path;   // swap instead of showing the same stream twice
    slots[active].path = chosen;
    if (two && slots[1 - active].playing) play(1 - active);
    play(active);
  });
  $('b-snap').addEventListener('click', function () {
    var p = curPath(); if (!p) return;
    flashNote('Taking a snapshot...', false);
    post('/api/snapshot', { path: p }, function (r) {
      if (r.ok && r.file) flashNote('Saved <a href="/captures/' + encodeURIComponent(r.file) + '" target="_blank" rel="noopener" style="color:var(--g)">' + esc(r.file) + '</a>', false, true);
      else flashNote(r.error || r.message || 'failed', !r.ok);
      if (filesOpen) loadFiles();
    });
  });
  function loadFiles() {
    var el = $('v-files');
    getJSON('/api/captures', function (r) {
      if (r.local_only) { el.innerHTML = '<span>Files are only listed on the deck\'s own screen.</span><span></span>'; return; }
      var f = r.files || [];
      el.innerHTML = f.length ? f.map(function (x) {
        return '<a href="/captures/' + encodeURIComponent(x.name) + '" target="_blank" rel="noopener">' + esc(x.name) + '</a><span>' + (x.size / 1048576).toFixed(1) + ' MB, ' + age(Date.now() / 1000 - x.t) + '</span>';
      }).join('') : '<span>No snapshots yet.</span><span></span>';
    });
  }
  $('b-files').addEventListener('click', function () {
    filesOpen = !filesOpen; $('b-files').setAttribute('aria-pressed', filesOpen); $('v-files').hidden = !filesOpen;
    if (filesOpen) loadFiles();
  });

  /* ---------------- trends ---------------- */
  var stripState = null;
  function drawCharts() {
    if (!hist || activeTab !== 'mesh') return;
    var sp = hist.spark, col = css('--plot');
    var ping = (hist.ping || []);
    var info = Charts.line($('g-ping'), { points: ping, color: col, windowSec: 900, ymin: 0 });
    var m = data && data.mesh;
    $('g-ping-t').textContent = 'PING // ' + (m && m.node_ok ? nodeLabel((m.nodes || []).filter(function (n) { return n.nearest; })[0] || { name: m.node_name }).toUpperCase() : 'NODE') + ', 15 MIN';
    $('g-ping-s').innerHTML = m && m.samples ? ('avg <b>' + (m.rtt_avg == null ? '-' : m.rtt_avg) + '</b> max <b>' + (m.rtt_max == null ? '-' : m.rtt_max) + '</b> jitter <b>' + (m.jitter == null ? '-' : m.jitter) + '</b> loss <b>' + m.loss_pct + '%</b>') : '';
    linkGraph(m);
    stripState = Charts.strip($('strip'), hist.strip);
  }
  var LINK_COLORS = ['--plot', '#b48ee0', '#7fd6d6', '#e07fb4', '#c9d3dd'];   /* not green/amber/red: those mean grades */
  function linkGraph(m) {
    var L = hist.links || [], keys = [];
    L.forEach(function (s) { Object.keys(s[1]).forEach(function (k) { if (keys.indexOf(k) < 0) keys.push(k); }); });
    var series = keys.map(function (k, i) {
      var c = LINK_COLORS[i % LINK_COLORS.length];
      return { name: k, color: c.charAt(0) === '-' ? css(c) : c,
               points: L.map(function (s) { return [s[0], s[1][k] == null ? null : s[1][k]]; }) };
    });
    var good = m ? m.link_good : 3, poor = m ? m.link_poor : 1;
    Charts.line($('g-link'), { series: series, ymin: 0, windowSec: 3600, now: hist.now,
                              hlines: [{ v: good, color: css('--amber') }, { v: poor, color: css('--red') }] });
    $('g-link-s').innerHTML = series.length ? series.map(function (s) { return '<b style="color:' + s.color + '">' + esc(s.name.replace('|', ' \u2194 ')) + '</b>'; }).join(' \u00b7 ') +
      ' \u00b7 dashed: OK ' + good + ', POOR ' + poor : 'no link data yet';
  }
  function stripInfo(e) {
    var cv = $('strip'), r = cv.getBoundingClientRect();
    var x = (e.touches && e.touches[0] ? e.touches[0].clientX : e.clientX) - r.left, y = (e.touches && e.touches[0] ? e.touches[0].clientY : e.clientY) - r.top;
    var h = Charts.stripHit(cv, x, y); if (!h) return;
    var nm = { '0': 'ok', '1': 'warn', '2': 'bad', '-': 'no data' }, L = h.levels, ago = Math.round((Date.now() / 1000 - h.t) / 60);
    var d = new Date(h.t * 1000);
    $('g-strip-s').textContent = pad(d.getHours()) + ':' + pad(d.getMinutes()) + (ago >= 60 ? ' (-' + Math.floor(ago / 60) + 'h ' + (ago % 60) + 'm)' : ' (-' + ago + 'm)') +
      '  bridge ' + nm[L.bridge] + ', server ' + nm[L.server] + ', video ' + nm[L.video] + ', mesh ' + nm[L.mesh];
  }
  $('strip').addEventListener('pointermove', stripInfo);
  $('strip').addEventListener('pointerdown', stripInfo);

  /* ---------------- event log with filters ---------------- */
  var logFilter = store('logf') || 'all', logSearch = '', logSig = '', expanded = {};
  var LVL = { ok: 'OK  ', info: 'INFO', warn: 'WARN', bad: 'BAD ' };
  function setFilter(f) {
    logFilter = f; store('logf', f);
    Array.prototype.forEach.call(document.querySelectorAll('.chipbtn'), function (b) { b.setAttribute('aria-pressed', b.getAttribute('data-f') === f ? 'true' : 'false'); });
    logSig = ''; if (data) logPanel(data);
  }
  Array.prototype.forEach.call(document.querySelectorAll('.chipbtn'), function (b) { b.addEventListener('click', function () { setFilter(b.getAttribute('data-f')); }); });
  $('l-search').addEventListener('input', function () { logSearch = this.value.trim().toLowerCase(); logSig = ''; if (data) logPanel(data); });
  $('l-rows').addEventListener('click', function (e) {
    var r = e.target.closest ? e.target.closest('.ev') : null; if (!r) return;
    var k = r.getAttribute('data-k'); expanded[k] = !expanded[k]; logSig = ''; logPanel(data);
  });
  function logPanel(d) {
    var ev = d.events || [];
    var shown = ev.filter(function (e) {
      var cat = e.cat || 'system';
      if (logFilter === 'warn' && !(e.level === 'warn' || e.level === 'bad')) return false;
      if (['bridge', 'server', 'mesh', 'video', 'alert'].indexOf(logFilter) >= 0 && cat !== logFilter) return false;
      if (logSearch && (e.text + ' ' + cat).toLowerCase().indexOf(logSearch) < 0) return false;
      return true;
    });
    var sig = logFilter + '|' + logSearch + '|' + ev.length + '|' + (ev[0] ? ev[0].t : 0) + '|' + Object.keys(expanded).filter(function (k) { return expanded[k]; }).join(',');
    $('l-count').textContent = shown.length + (shown.length === ev.length ? '' : ' of ' + ev.length) + ' events';
    if (sig === logSig) return;
    logSig = sig;
    var box = $('l-rows'), top = box.scrollTop;
    box.innerHTML = shown.map(function (e) {
      var k = String(e.t), open = expanded[k], dt = new Date(e.t * 1000);
      return '<div class="ev lv-' + e.level + '" data-k="' + esc(k) + '" title="Click for details"><span class="t">' + hms(e.t) + '</span><span class="lvl">' + (LVL[e.level] || 'INFO') + '</span><span class="tx">' + esc(e.text) + '</span>' +
        (open ? '<span class="det">' + dt.getFullYear() + '-' + pad(dt.getMonth() + 1) + '-' + pad(dt.getDate()) + ' ' + hms(e.t) + ', ' + age(d.now - e.t) + ', ' + esc(e.cat || 'system') + '</span>' : '') + '</div>';
    }).join('') || '<span class="empty">' + (ev.length ? 'Nothing matches this filter.' : 'Nothing yet.') + '</span>';
    box.scrollTop = top;
  }

  /* ---------------- setup dialog: what is not set up yet, alert rules (read-only), restart bridge ---------------- */
  var confirmTimer = 0;
  function openSetup() {
    $('tools').className = 'on'; $('t-close').focus();
    renderSetup();
    getJSON('/api/alerts', function (r) {
      var rules = r.rules || [];
      $('r-list').innerHTML = rules.length ? rules.map(function (x) {
        return '<li><span class="lv-' + x.level + '" style="color:var(--c)">' + (x.level === 'bad' ? 'RED ' : 'AMBER ') + '</span>' + esc(x.text) +
          (x.for_s ? ' for ' + x.for_s + ' s' : '') + (x.tile !== 'none' ? ', colors the ' + esc(x.tile) + ' tile' : ', banner only') + '</li>';
      }).join('') : '<li>No alert rules (ALERTS = off).</li>';
    });
  }
  function closeSetup() { $('tools').className = ''; $('h-tools').focus(); }
  $('h-tools').addEventListener('click', openSetup);
  $('t-close').addEventListener('click', closeSetup);
  $('tools').addEventListener('click', function (e) { if (e.target === $('tools')) closeSetup(); });
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape' && $('tools').className === 'on') closeSetup(); });

  function setupBadge(d) {
    var items = d.setup || [];
    var bad = items.filter(function (c) { return !c.ok && c.level === 'bad'; }).length, warn = items.filter(function (c) { return !c.ok && c.level === 'warn'; }).length;
    var b = $('h-tools');
    b.textContent = bad + warn ? 'SETUP ' + (bad + warn) : 'SETUP OK';
    b.className = 'hbtn' + (bad ? ' lv-bad' : warn ? ' lv-warn' : '');
  }
  function renderSetup() {
    if (!data) return;
    var items = data.setup || [];
    var order = { bad: 0, warn: 1, info: 2, ok: 3 };
    items = items.slice().sort(function (a, b) { return (a.ok - b.ok) || (order[a.level] - order[b.level]); });
    $('su-list').innerHTML = items.map(function (c) {
      var lvl = c.ok ? 'ok' : c.level;
      return '<div class="chk lv-' + lvl + '"><span class="mk">' + (c.ok ? 'OK' : lvl === 'info' ? '--' : lvl === 'warn' ? '!!' : 'XX') + '</span>' +
        '<div><b>' + esc(c.title) + '</b>' + (c.detail ? '<span class="dt"> ' + esc(c.detail) + '</span>' : '') +
        (!c.ok && c.fix ? '<code>' + esc(c.fix) + '</code>' : '') + '</div></div>';
    }).join('');
    var caps = data.caps || {}, local = data.local;
    $('c-note').textContent = !local ? 'The restart button only works on the deck\'s own screen.' :
      !caps.control ? 'Controls are turned off (CONTROL = off in [dashboard] of deck.ini).' :
      !caps.restart_bridge ? 'Restart permission is not set up (see the list above).' :
      'Asks once more before it does anything. Relaying pauses for a few seconds.';
    $('c-bridge').disabled = !(local && caps.control && caps.restart_bridge);
  }
  $('c-bridge').addEventListener('click', function () {
    var btn = $('c-bridge');
    if (btn.classList.contains('confirm')) {
      clearTimeout(confirmTimer); btn.classList.remove('confirm'); btn.textContent = 'Restart bridge';
      $('c-msg').className = 'note'; $('c-msg').textContent = 'Working...';
      post('/api/action', { action: 'restart_bridge' }, function (r) { $('c-msg').textContent = r.ok ? (r.message || 'done') : (r.error || 'failed'); $('c-msg').className = 'note' + (r.ok ? '' : ' err'); });
    } else {
      btn.classList.add('confirm'); btn.textContent = 'Really restart? Click again';
      confirmTimer = setTimeout(function () { btn.classList.remove('confirm'); btn.textContent = 'Restart bridge'; }, 5000);
    }
  });

  /* ---------------- render + poll ---------------- */
  function render(d) {
    data = d;
    if (d.tiles) try { firstRun(d); } catch (e) { if (window.console) console.error(e); }   /* first, so a panel error can't stop it */
    $('host').textContent = d.host ? '  ' + d.host + (d.deck_ip ? ' ' + d.deck_ip : '') : '';
    $('mock').style.display = d.mock ? 'inline' : 'none';
    ['bridge', 'server', 'video', 'mesh'].forEach(function (k) { tile(k, d.tiles[k]); });
    gpsTile(d.gps);
    banner(d); bridgePanel(d); serverPanel(d); deckPanel(d); meshTab(d); videoPanel(d); logPanel(d);
    sparks();
    setupBadge(d);
    if ($('tools').className === 'on') renderSetup();
  }
  /* a deck whose TAK server isn't set up yet opens the setup wizard (on its own screen; "skip" stays here) */
  /* the banner shows on every refresh while the server isn't set up; "skip" only lasts until the browser is closed */
  var firstRunDone = false;
  function firstRun(d) {
    if (d.mock) return;
    var unset = (d.setup || []).some(function (c) { return c.title === 'TAK server not set up yet'; });
    $('setupbar').className = unset ? 'on' : '';
    $('sb-go').style.display = d.local ? '' : 'none';
    $('sb-say').textContent = d.local ? 'connect it to your TAK server: about 2 minutes, step by step'
                                      : 'open Ops on the deck\'s own screen to run setup';
    if (firstRunDone) return;
    firstRunDone = true;
    $('su-wizard').style.display = d.local ? '' : 'none';
    try { localStorage.removeItem('deck.setup-later'); } catch (e) { /* older versions kept "skip" for good */ }
    var later = null;
    try { later = sessionStorage.getItem('deck.setup-later'); } catch (e) { /* private window */ }
    if (unset && d.local && later !== '1') location.href = '/setup';
  }
  function pollStatus() {
    var x = new XMLHttpRequest();
    x.open('GET', '/api/status' + q, true); x.timeout = 4000;
    x.onload = function () { try { render(JSON.parse(x.responseText)); lastOk = Date.now(); } catch (e) { if (window.console) console.error(e); } };
    x.send();
  }
  function pollHistory() {
    getJSON('/api/history', function (h) { hist = h; sparks(); drawCharts(); renderCenterState(); });
  }
  clock(); setInterval(clock, 1000);
  pollStatus(); setInterval(pollStatus, 2000);
  pollHistory(); setInterval(pollHistory, 10000);
  window.addEventListener('resize', function () { drawCharts(); sparks(); });

  var startTab = params.tab || store('tab') || 'video';
  showTab(startTab, false);
  if (params.cam === '2up') { two = true; $('m-two').setAttribute('aria-pressed', 'true'); }
  if (params.setup === '1') setTimeout(openSetup, 900);

  /* ---------------- kiosk niceties ---------------- */
  var idle;
  function wake() { document.body.classList.remove('kiosk-idle'); clearTimeout(idle); idle = setTimeout(function () { document.body.classList.add('kiosk-idle'); }, 4000); }
  document.addEventListener('mousemove', wake); wake();
  document.addEventListener('dblclick', function (e) {
    if (e.target.closest && e.target.closest('.player, #strip, .dlg')) return;
    try { if (!document.fullscreenElement) document.documentElement.requestFullscreen(); else document.exitFullscreen(); } catch (err) {}
  });
})();
