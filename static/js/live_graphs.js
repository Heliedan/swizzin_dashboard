/* Live graphs of the dashboard: the download and upload speed of the default interface (Network Info card) and
   the CPU used (CPU Info card), over the last 5 minutes. They are drawn from the websocket events the page already
   receives, one per second: "speed" (swizzin.py) and "live" (core/custom/live_stats.py). Nothing is stored: the
   graphs start empty when the page loads. static/js/live_stats.js hands its websocket over with
   LiveGraphs.attach(socket): no other connection is opened. See the README.

   Drawing: one SVG per graph (templates/live_graph.html), 300 units wide, one per second, and 100 high. It stretches
   to the card (preserveAspectRatio="none"), so nothing here reads the size of the page. Each reading only rewrites
   the "d" of the existing paths, and the texts that changed.
   Time: each reading is placed at the time it arrived (Date.now(), which keeps counting while a tablet sleeps).
   No reading for more than GAP seconds, or a lost connection, cuts the line: the gap shows, as long as it lasted. */
(function () {
  'use strict';

  var WINDOW = 300;  // seconds shown: the last 5 minutes
  var GAP = 2.5;  // seconds without a reading after which the line is cut (one reading missed does not cut it)
  var MAX_READINGS = 2 * WINDOW;  // memory bound, should readings ever come faster than one per second
  var MIN_TOP = 1024;  // bytes per second: the bandwidth scale never goes below 1 KiB/s
  // The tops of the bandwidth scale, in its unit: the highest reading of the window fills 2/3 of the height or more.
  var STEPS = [1, 1.5, 2, 3, 4, 5, 6, 8, 10, 15, 20, 30, 40, 50, 60, 80, 100, 150, 200, 300, 400, 500, 600, 800, 1000];

  // Texts shown by this file. The units are those of the speed display (GetHumanReadableBi in core/util.py).
  var TEXT = {
    units: ['B/s', 'KiB/s', 'MiB/s', 'GiB/s', 'TiB/s', 'PiB/s'],
    percent: '%',
    none: '--',  // a value that could not be read
  };

  function round(v) { return Math.round(v * 10) / 10; }  // a tenth of a second, a thousandth of the height

  // "12.34 MiB/s" (as the server writes it) -> bytes per second, or null.
  function bytes(text) {
    var m = /^(\d+(?:\.\d+)?) (\w+\/s)$/.exec(String(text));
    var unit = m ? TEXT.units.indexOf(m[2]) : -1;
    return unit < 0 ? null : Number(m[1]) * Math.pow(1024, unit);
  }

  function percent(text) {
    var v = (typeof text === 'number' || typeof text === 'string') && text !== '' ? Number(text) : NaN;
    return isFinite(v) ? Math.max(0, Math.min(100, v)) : null;
  }

  // Bandwidth: the scale goes up to the next step (STEPS) of the unit of the highest reading of the window.
  var BANDWIDTH_SCALE = {
    top: function (max) {
      var v = Math.max(max, MIN_TOP), unit = 0;
      while (v >= 1024 && unit < TEXT.units.length - 1) { v /= 1024; unit += 1; }
      for (var i = 0; i < STEPS.length; i++) {
        if (v <= STEPS[i]) { return STEPS[i] * Math.pow(1024, unit); }
      }
      return Math.pow(1024, unit + 1);  // above 1000 of a unit: 1 of the next one
    },
    label: function (v) {
      var unit = 0;
      while (v >= 1024 && unit < TEXT.units.length - 1) { v /= 1024; unit += 1; }
      return Math.round(v * 100) / 100 + ' ' + TEXT.units[unit];  // "2.5 MiB/s", "512 KiB/s"
    },
  };

  var CPU_SCALE = {  // always 0 to 100 %
    top: function () { return 100; },
    label: function (v) { return v + TEXT.percent; },
  };

  // One graph: the element #id of the page, one series per key (its paths #id-key-area and #id-key-line, its current
  // value #id-key-now if the legend has one), and the scale labels #id-top and #id-middle.
  function Graph(id, keys, scale) {
    function get(suffix) { return document.getElementById(id + '-' + suffix); }
    this.scale = scale;
    this.series = keys.map(function (k) { return { area: get(k + '-area'), line: get(k + '-line'), now: get(k + '-now') }; });
    this.top = get('top');
    this.middle = get('middle');
    this.readings = [];  // {t: ms, v: [a value or null per series], cut: true if the line does not join the previous}
    this.texts = [];  // the values as received, for the legend
    this.cutNext = false;  // the connection was lost: the next reading starts a new line
  }

  Graph.prototype.push = function (values, texts, t) {
    var r = this.readings, last = r[r.length - 1];
    if (last && t < last.t) { r.length = 0; last = null; }  // the computer's clock went back: start again
    r.push({ t: t, v: values, cut: this.cutNext || !last || t - last.t > GAP * 1000 });
    this.cutNext = false;
    this.texts = texts;
    this.draw(t);
  };

  // Keeps the readings of the window, and one before it that the line goes on from, so that it reaches the left edge.
  Graph.prototype.trim = function (now) {
    var r = this.readings, edge = now - WINDOW * 1000;
    while (r.length && r[0].t < edge && (r.length < 2 || r[1].t <= edge || r[1].cut)) { r.shift(); }
    if (r.length > MAX_READINGS) { r.splice(0, r.length - MAX_READINGS); }
  };

  Graph.prototype.draw = function (now) {
    var r = this.readings;
    if (r.length && now < r[r.length - 1].t) { now = r[r.length - 1].t; }
    this.trim(now);
    if (document.hidden) { return; }  // nothing to paint in a hidden tab: drawn again when it is shown
    var max = 0;
    r.forEach(function (x) { x.v.forEach(function (v) { if (v !== null && v > max) { max = v; } }); });
    var top = this.scale.top(max), self = this;
    this.series.forEach(function (s, i) {
      var p = paths(r, i, now, top);
      setPath(s.area, p.area);
      setPath(s.line, p.line);
      var text = self.texts[i];
      setText(s.now, typeof text === 'string' && text ? text : TEXT.none);
    });
    if (r.length) {
      setText(this.top, this.scale.label(top));
      setText(this.middle, this.scale.label(top / 2));
    }
  };

  // The "d" of series i: a line, and the area under it, one piece per stretch of readings without a cut or a hole.
  function paths(readings, i, now, top) {
    var line = '', area = '', open = false, lastX = 0;
    readings.forEach(function (x) {
      var v = x.v[i];
      if (v === null) { close(); return; }
      var px = round(WINDOW - (now - x.t) / 1000), py = round(100 - Math.min(v / top, 1) * 100);
      if (!open || x.cut) {
        close();
        line += 'M' + px + ' ' + py;
        area += 'M' + px + ' 100L' + px + ' ' + py;
        open = true;
      } else {
        line += 'L' + px + ' ' + py;
        area += 'L' + px + ' ' + py;
      }
      lastX = px;
    });
    close();
    return { line: line, area: area };

    function close() {
      if (open) { area += 'L' + lastX + ' 100Z'; }
      open = false;
    }
  }

  function setPath(el, d) {
    if (el && (el.getAttribute('d') || '') !== d) { el.setAttribute('d', d); }
  }

  function setText(el, text) {
    if (el && el.textContent !== text) { el.textContent = text; }
  }

  // An error in a graph must not stop the live cards: socket.io would not call the handlers after this one, nor
  // read the rest of a batch of events (long polling).
  function guarded(f) {
    return function (d) {
      try { f(d); } catch (e) { console.error('live graphs:', e); }
    };
  }

  function attach(socket) {
    var cpu = document.getElementById('cpu-graph') ? new Graph('cpu-graph', ['used'], CPU_SCALE) : null;
    var net = document.getElementById('net-graph') ? new Graph('net-graph', ['rx', 'tx'], BANDWIDTH_SCALE) : null;
    var graphs = [cpu, net].filter(Boolean);
    if (cpu) {
      socket.on('live', guarded(function (d) {
        if (d && d.cpu) { cpu.push([percent(d.cpu.used)], [], Date.now()); }
      }));
    }
    if (net) {  // the admin only, like the speed table of the card
      socket.on('speed', guarded(function (d) {
        if (d) { net.push([bytes(d.rx), bytes(d.tx)], [d.rx, d.tx], Date.now()); }
      }));
    }
    socket.on('disconnect', function () { graphs.forEach(function (g) { g.cutNext = true; }); });
    document.addEventListener('visibilitychange', guarded(function () {
      if (!document.hidden) { graphs.forEach(function (g) { g.draw(Date.now()); }); }
    }));
  }

  window.LiveGraphs = { attach: attach };
})();
