/* The Overview layout of the dashboard (templates/overview.html, see the README): what the other scripts do not do.
   The tiles are filled by static/js/live_stats.js and their small graphs drawn by static/js/live_graphs.js.

   Details: each card of the Classic layout is in a section (a <details> element: the keyboard and screen readers
   know it), closed when the page loads unless this browser remembers it open. The open sections are kept in the
   browser's storage (localStorage, key "overview.open", their names comma separated). Blocked or absent storage
   (private browsing...) only means that they all start closed.

   Network: the totals of this hour, today, this month and all time, under the bandwidth graph. They are read from the
   bandwidth card of the Details (#top10, templates/top.html) each time the dashboard's own refresh fills it, every 10
   minutes: no request of their own. When vnstat has no totals to give, the card's sentence is shown instead. */
(function () {
  'use strict';

  var KEY = 'overview.open';
  var PERIODS = ['hour', 'day', 'month', 'alltime'];  // data-period of the rows of templates/top.html
  var NONE = '--';

  function recall() {
    try { return String(window.localStorage.getItem(KEY) || '').split(','); } catch (e) { return []; }
  }
  function remember(names) {
    try { window.localStorage.setItem(KEY, names.join(',')); } catch (e) { /* forgotten */ }
  }

  function setText(id, value) {
    var el = document.getElementById(id);
    if (el && el.textContent !== value) { el.textContent = value; }
  }

  function sections() {
    var all = Array.prototype.slice.call(document.querySelectorAll('.ov-section'));
    function name(s) { return s.getAttribute('data-section'); }
    var open = recall();
    all.forEach(function (s) {
      if (open.indexOf(name(s)) >= 0) { s.open = true; }
      s.addEventListener('toggle', function () {
        remember(all.filter(function (x) { return x.open; }).map(name));
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
