/* The Overview layout of the dashboard (templates/overview.html, see the README): what the other scripts do not do.
   The tiles are filled by static/js/live_stats.js and their small graphs drawn by static/js/live_graphs.js.

   Details: each card is in a section (a <details> element: the keyboard and screen readers know it), open or closed
   by default as the panel renders it ("open" in core/overview_blocks.py), unless this browser remembers it otherwise.
   The browser's storage keeps what differs from the default, the sections' names comma separated: localStorage
   "overview.open", the sections opened among those closed by default, and "overview.closed", those closed among those
   open by default. A key is written only when it changes. Blocked or absent storage (private browsing...) only means
   that each one starts as it is by default.

   Network: the totals of this hour, today, this month and all time, under the bandwidth graph. They are read from the
   bandwidth card of the Details (#top10, templates/top.html) each time the dashboard's own refresh fills it, every 10
   minutes: no request of their own. When vnstat has no totals to give, the card's sentence is shown instead. */
(function () {
  'use strict';

  var OPENED = 'overview.open', CLOSED = 'overview.closed';  // localStorage
  var PERIODS = ['hour', 'day', 'month', 'alltime'];  // data-period of the rows of templates/top.html
  var NONE = '--';

  function recall(key) {
    try { return String(window.localStorage.getItem(key) || ''); } catch (e) { return ''; }
  }
  function remember(key, names) {
    var value = names.join(',');
    if (value === recall(key)) { return; }
    try { window.localStorage.setItem(key, value); } catch (e) { /* forgotten */ }
  }

  function setText(id, value) {
    var el = document.getElementById(id);
    if (el && el.textContent !== value) { el.textContent = value; }
  }

  function sections() {
    var all = Array.prototype.slice.call(document.querySelectorAll('.ov-section'));
    var byDefault = all.map(function (s) { return s.open; });  // as the panel rendered them
    function name(s) { return s.getAttribute('data-section'); }
    function changed(open) {  // the sections that are `open` (or not) against their default
      return all.filter(function (s, i) { return s.open === open && byDefault[i] !== open; }).map(name);
    }
    var opened = recall(OPENED).split(','), closed = recall(CLOSED).split(',');
    all.forEach(function (s, i) {
      s.open = byDefault[i] ? closed.indexOf(name(s)) < 0 : opened.indexOf(name(s)) >= 0;
      s.addEventListener('toggle', function () {
        remember(OPENED, changed(true));
        remember(CLOSED, changed(false));
      });
    });
  }

  // A row's cells: the period, then Down and Up (templates/top.html). Their text only: never HTML.
  function totals(card) {
    var found = false;
    PERIODS.forEach(function (p) {
      var row = card.querySelector('[data-period="' + p + '"]');
      var cells = row ? row.children : [];
      found = found || cells.length > 2;
      setText('ov-' + p + '-rx', cells.length > 2 ? cells[1].textContent.trim() : NONE);
      setText('ov-' + p + '-tx', cells.length > 2 ? cells[2].textContent.trim() : NONE);
    });
    var note = document.getElementById('ov-periods-note');
    if (note) {
      var sentence = found ? '' : card.textContent.replace(/\s+/g, ' ').trim();
      setText('ov-periods-note', sentence);
      note.hidden = !sentence;
    }
  }

  // At once: this file is loaded at the end of the page, after its elements, and before the dashboard's own
  // refreshes start (templates/index.html). Waiting for DOMContentLoaded could miss the first answer of /stats/vnstat.
  sections();
  var card = document.getElementById('top10');
  if (card && document.getElementById('ov-periods-note') && typeof MutationObserver !== 'undefined') {
    new MutationObserver(function () { totals(card); }).observe(card, { childList: true });
    if (card.children.length) { totals(card); }
  }
})();
