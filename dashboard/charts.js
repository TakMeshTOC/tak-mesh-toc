/* charts.js - tiny canvas charts for the dashboard (no libraries, works offline).
   Charts.spark(canvas, values, color, opts)   small trend line for a tile
   Charts.line(canvas, opts)                    labelled line graph with gaps and loss ticks
   Charts.strip(canvas, strip)                  24-hour up/down history strip; returns a hit-test helper
   Colors come from the page's CSS variables, so every theme works. */
(function () {
  'use strict';
  var Charts = {};

  function css(name, fallback) {
    var v = getComputedStyle(document.documentElement).getPropertyValue(name);
    return (v && v.trim()) || fallback;
  }
  function prep(canvas) {
    var dpr = window.devicePixelRatio || 1;
    var w = canvas.clientWidth, h = canvas.clientHeight;
    if (!w || !h) return null;
    var bw = Math.round(w * dpr), bh = Math.round(h * dpr);
    if (canvas.width !== bw || canvas.height !== bh) { canvas.width = bw; canvas.height = bh; }
    var ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    return { ctx: ctx, w: w, h: h };
  }
  function nice(max) {
    if (!(max > 0)) return 1;
    var p = Math.pow(10, Math.floor(Math.log(max) / Math.LN10)), f = max / p;
    return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10) * p;
  }
  function fmtNum(v) { return v === 0 ? '0' : v >= 100 ? String(Math.round(v)) : v >= 10 ? v.toFixed(0) : v.toFixed(1); }

  /* ---- sparkline ---- */
  Charts.spark = function (canvas, vals, color, opts) {
    var p = prep(canvas); if (!p) return;
    opts = opts || {};
    var ctx = p.ctx, w = p.w, h = p.h, pad = 3;
    var nums = (vals || []).filter(function (v) { return v != null; });
    if (nums.length < 2) {
      ctx.strokeStyle = css('--faint', '#144'); ctx.setLineDash([3, 3]); ctx.beginPath();
      ctx.moveTo(0, h / 2); ctx.lineTo(w, h / 2); ctx.stroke(); return;
    }
    var lo = opts.min != null ? opts.min : Math.min.apply(null, nums);
    var hi = Math.max.apply(null, nums);
    if (opts.min == null && hi - lo < 1e-9) { lo -= 1; hi += 1; }
    if (hi - lo < 1e-9) hi = lo + 1;
    var n = vals.length;
    function X(i) { return pad + (w - 2 * pad) * i / (n - 1); }
    function Y(v) { return h - pad - (h - 2 * pad) * (v - lo) / (hi - lo); }
    ctx.lineWidth = 1.5; ctx.lineJoin = 'round'; ctx.strokeStyle = color; ctx.fillStyle = color;
    var seg = [];
    function flush() {
      if (seg.length > 1) {
        ctx.globalAlpha = 0.16; ctx.beginPath(); ctx.moveTo(X(seg[0][0]), h - pad);
        seg.forEach(function (s) { ctx.lineTo(X(s[0]), Y(s[1])); });
        ctx.lineTo(X(seg[seg.length - 1][0]), h - pad); ctx.closePath(); ctx.fill();
        ctx.globalAlpha = 1; ctx.beginPath();
        seg.forEach(function (s, k) { if (k) ctx.lineTo(X(s[0]), Y(s[1])); else ctx.moveTo(X(s[0]), Y(s[1])); });
        ctx.stroke();
      }
      seg = [];
    }
    vals.forEach(function (v, i) { if (v == null) flush(); else seg.push([i, v]); });
    flush();
    var lastI = -1; for (var i = n - 1; i >= 0; i--) { if (vals[i] != null) { lastI = i; break; } }
    if (lastI >= 0) { ctx.beginPath(); ctx.arc(X(lastI), Y(vals[lastI]), 2.2, 0, 6.3); ctx.fill(); }
  };

  /* ---- labelled line graph ----
     opts: { points:[[t, v|null],...], color, ymin, unit, lossColor, windowSec, now }
     or several lines: { series:[{points, color}], hlines:[{v, color}] } (hlines: dashed thresholds) */
  Charts.line = function (canvas, opts) {
    var p = prep(canvas); if (!p) return null;
    var ctx = p.ctx, w = p.w, h = p.h;
    var left = 34, right = 4, top = 6, bottom = 14;
    var series = opts.series || [{ points: opts.points || [], color: opts.color }];
    var pts = [].concat.apply([], series.map(function (s) { return s.points || []; }));
    var color = opts.color || css('--plot', '#3f7');
    var line = css('--line', '#143'), dim = css('--dim', '#284'), red = css('--red', '#f44');
    var vals = pts.map(function (q) { return q[1]; }).filter(function (v) { return v != null; });
    (opts.hlines || []).forEach(function (l) { vals.push(l.v); });
    var ymin = opts.ymin != null ? opts.ymin : 0;
    var ymax = opts.ymax != null ? opts.ymax : nice(Math.max.apply(null, vals.concat([1])) * 1.1);
    ctx.font = '10px ' + css('--mono', 'monospace');
    ctx.textBaseline = 'middle';
    // grid + y labels
    for (var g = 0; g <= 2; g++) {
      var yy = top + (h - top - bottom) * g / 2, val = ymax - (ymax - ymin) * g / 2;
      ctx.strokeStyle = line; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(left, yy + .5); ctx.lineTo(w - right, yy + .5); ctx.stroke();
      ctx.fillStyle = dim; ctx.textAlign = 'right'; ctx.fillText(fmtNum(val), left - 4, yy);
    }
    if (!pts.length) { ctx.fillStyle = dim; ctx.textAlign = 'center'; ctx.fillText('collecting data...', (left + w) / 2, h / 2); return null; }
    var t0 = Math.min.apply(null, pts.map(function (q) { return q[0]; }));
    var t1 = opts.now || Math.max.apply(null, pts.map(function (q) { return q[0]; }));
    if (opts.windowSec) t0 = Math.max(t0, t1 - opts.windowSec);
    var span = Math.max(1, t1 - t0);
    function X(t) { return left + (w - left - right) * (t - t0) / span; }
    function Y(v) { return top + (h - top - bottom) * (1 - (Math.min(v, ymax) - ymin) / (ymax - ymin)); }
    // thresholds as dashed lines
    (opts.hlines || []).forEach(function (l) {
      var y = Math.round(Y(l.v)) + .5;
      ctx.strokeStyle = l.color; ctx.lineWidth = 1; ctx.setLineDash([4, 4]);
      ctx.beginPath(); ctx.moveTo(left, y); ctx.lineTo(w - right, y); ctx.stroke(); ctx.setLineDash([]);
    });
    // lines with gaps
    series.forEach(function (s) {
      ctx.strokeStyle = s.color || color; ctx.lineWidth = 1.5; ctx.lineJoin = 'round';
      var open = false;
      ctx.beginPath();
      (s.points || []).forEach(function (q) {
        if (q[0] < t0) return;
        if (q[1] == null) { open = false; return; }
        if (!open) { ctx.moveTo(X(q[0]), Y(q[1])); open = true; } else ctx.lineTo(X(q[0]), Y(q[1]));
      });
      ctx.stroke();
    });
    // lost samples as red ticks on the baseline (single line only)
    ctx.strokeStyle = red; ctx.lineWidth = 2;
    if (!opts.series) pts.forEach(function (q) {
      if (q[0] >= t0 && q[1] == null && opts.lossTicks !== false) { var x = X(q[0]); ctx.beginPath(); ctx.moveTo(x, h - bottom - 1); ctx.lineTo(x, h - bottom - 9); ctx.stroke(); }
    });
    // x labels
    ctx.fillStyle = dim; ctx.textBaseline = 'alphabetic'; ctx.textAlign = 'left';
    ctx.fillText('-' + fmtSpan(span), left, h - 2);
    ctx.textAlign = 'right'; ctx.fillText('now', w - right, h - 2);
    return { ymax: ymax };
  };
  function fmtSpan(s) { return s >= 5400 ? Math.round(s / 3600) + 'h' : s >= 90 ? Math.round(s / 60) + 'm' : Math.round(s) + 's'; }

  /* ---- 24 h strip ---- */
  var ROWS = [['bridge', 'BRIDGE'], ['server', 'SERVER'], ['video', 'VIDEO'], ['mesh', 'MESH']];
  Charts.strip = function (canvas, strip) {
    var p = prep(canvas); if (!p || !strip) return null;
    var ctx = p.ctx, w = p.w, h = p.h;
    var labelW = 50, plotW = Math.max(40, w - labelW), rowH = 9, gap = 3, top = 1;
    var n = strip.bridge.length, per = Math.max(1, Math.ceil(n / plotW)), cols = Math.ceil(n / per);
    var cw = plotW / cols;
    var colors = { '0': css('--ok', '#3f7'), '1': css('--amber', '#fb0'), '2': css('--red', '#f44'), '-': css('--line', '#143') };
    ctx.font = '9px ' + css('--mono', 'monospace'); ctx.textBaseline = 'middle'; ctx.textAlign = 'left';
    var state = { labelW: labelW, cw: cw, per: per, cols: cols, n: n, strip: strip, rowH: rowH, gap: gap, top: top };
    ROWS.forEach(function (r, ri) {
      var y = top + ri * (rowH + gap), s = strip[r[0]];
      ctx.fillStyle = css('--dim', '#284'); ctx.globalAlpha = 1; ctx.fillText(r[1], 0, y + rowH / 2);
      for (var c = 0; c < cols; c++) {
        var worst = '-', seen = false;
        for (var k = c * per; k < Math.min(n, (c + 1) * per); k++) {
          var ch = s.charAt(k);
          if (ch === '-') continue;
          seen = true; if (worst === '-' || ch > worst) worst = ch;
        }
        ctx.fillStyle = colors[seen ? worst : '-'];
        ctx.globalAlpha = (seen && worst === '0') ? 0.55 : 1;
        ctx.fillRect(labelW + c * cw, y, Math.max(1, cw - (cw > 2.5 ? 0.6 : 0)), rowH);
      }
    });
    ctx.globalAlpha = 1;
    // hour ticks
    var yb = top + ROWS.length * (rowH + gap);
    ctx.fillStyle = css('--dim', '#284'); ctx.textBaseline = 'top';
    [['-24h', 0], ['-18h', .25], ['-12h', .5], ['-6h', .75]].forEach(function (t) {
      ctx.textAlign = t[1] === 0 ? 'left' : 'center'; ctx.fillText(t[0], labelW + plotW * t[1], yb);
    });
    ctx.textAlign = 'right'; ctx.fillText('now', labelW + plotW, yb);
    canvas._strip = state;
    return state;
  };
  /* Which minute and levels are under x,y (canvas pixels)? */
  Charts.stripHit = function (canvas, x, y) {
    var s = canvas._strip; if (!s) return null;
    if (x < s.labelW) return null;
    var col = Math.floor((x - s.labelW) / s.cw); if (col < 0 || col >= s.cols) return null;
    var minute = Math.min(s.n - 1, col * s.per + Math.floor(s.per / 2));
    var out = { t: s.strip.t0 + minute * 60, span: s.per, levels: {} };
    ROWS.forEach(function (r) { out.levels[r[0]] = s.strip[r[0]].charAt(minute); });
    return out;
  };

  window.Charts = Charts;
})();
