/* Top processes, a section of the Details of Overview (templates/overview/processes.html, see the README). While the
   section is open and the browser tab shows, the page asks /stats/processes (core/processes.py) at once, then every
   5 seconds, and fills two lists: the 5 programs that used the most CPU, and the 5 that use the most memory. Closed,
   or the tab hidden, it asks nothing: the panel reads no process for it. The admin sees every program, another
   account its own. The names come from the system: text only, never HTML. A failed request says so and asks again a
   little later; 401 (logged out) goes to the logout page, as the dashboard's other refreshes do. */
(function () {
  'use strict';

  var box = document.getElementById('ov-processes');
  if (!box) { return; }  // TOP_PROCESSES = False in swizzin.cfg: no section
  var section = box.parentNode;  // its <details>
  var EVERY = 5000, RETRY = 15000;  // ms
  var LISTS = ['cpu', 'memory'];
  var MESSAGES = {
    'no-account': 'This server has no Linux account named like yours: the panel cannot tell which programs are yours.',
    unreadable: 'The panel cannot read the processes of this server (/proc).'
  };
  var run = 0;  // counts the starts: a timer or an answer of an earlier one does nothing
  var running = false;

  function setText(el, value) {
    if (el.textContent !== value) { el.textContent = value; }
  }
  function say(text) { setText(document.getElementById('ov-processes-message'), text || ''); }
  function count(n, one, many) { return n + ' ' + (n === 1 ? one : many); }
  function number(n) { return typeof n === 'number' && isFinite(n) ? Math.min(Math.max(n, 0), 100) : 0; }
  function percent(n) { return n > 0 && n < 0.05 ? '<0.1%' : n.toFixed(1) + '%'; }

  function cell(tr, cls) {
    var td = document.createElement('td');
    td.className = cls;
    tr.appendChild(td);
    return td;
  }
  function span(parent, cls, text) {
    var s = document.createElement('span');
    s.className = cls;
    s.textContent = text;
    parent.appendChild(s);
    return s;
  }

  // One program: its name, its account, its CPU, its memory (and share of the memory), and a bar that repeats the
  // number the list is sorted by (hidden from screen readers: the number is in the text).
  function row(p, list) {
    var tr = document.createElement('tr');
    cell(tr, 'ov-processes-name').textContent = String(p.name);
    cell(tr, 'ov-processes-user').textContent = String(p.user);
    cell(tr, 'ov-processes-number').textContent = percent(number(p.cpu));
    var memory = cell(tr, 'ov-processes-number');
    span(memory, 'ov-processes-amount', String(p.memory));
    if (typeof p.memory_share === 'number') { span(memory, 'ov-processes-share', percent(number(p.memory_share))); }
    var bar = cell(tr, 'ov-processes-bar');
    bar.setAttribute('aria-hidden', 'true');
    var value = number(list === 'cpu' ? p.cpu : p.memory_share);
    var level = span(span(bar, 'ov-processes-track', ''), 'ov-processes-fill', '');
    level.style.width = value + '%';
    level.style.minWidth = value > 0 ? '2px' : '0';
    return tr;
  }

  function fill(list, rows, empty) {
    var body = document.getElementById('ov-processes-' + list);
    var table = document.getElementById('ov-processes-' + list + '-table');
    var note = document.getElementById('ov-processes-' + list + '-empty');
    while (body.firstChild) { body.removeChild(body.firstChild); }
    rows.forEach(function (p) { body.appendChild(row(p, list)); });
    table.hidden = !rows.length;
    setText(note, rows.length ? '' : empty);
    note.hidden = !!rows.length || !empty;
  }

  function rows(list) {
    return Array.isArray(list) ? list.filter(function (p) { return p && typeof p === 'object'; }) : [];
  }

  function show(data) {
    var info = document.getElementById('ov-processes-info');
    if (!data || data.state !== 'ok') {
      LISTS.forEach(function (list) { fill(list, [], ''); });
      setText(info, '');
      say(data && Object.prototype.hasOwnProperty.call(MESSAGES, data.state) ? MESSAGES[data.state]
        : 'The panel sent an answer this page does not know: reload the page.');
      return;
    }
    var own = !!data.own;
    var seconds = Math.max(1, Math.round(Number(data.seconds) || 0)) + ' s';
    var cores = Math.max(1, Math.round(Number(data.cores) || 1));
    var counted = Math.max(0, Math.round(Number(data.count) || 0));
    fill('cpu', rows(data.cpu), own ? 'None of your programs used the processor in the last ' + seconds + '.'
      : 'No program used the processor in the last ' + seconds + '.');
    fill('memory', rows(data.memory), own ? 'You have no program running.' : 'No program to show.');
    // "7 processes. CPU: share of the whole processor over the last 5 s (4 cores: a program that keeps one busy
    // shows 25.0%)." The difference with top, which would say 100% for that program.
    var busy = cores > 1 ? ': a program that keeps one busy shows ' + (100 / cores).toFixed(1) + '%' : '';
    var which = own ? count(counted, 'process of yours', 'processes of yours') : count(counted, 'process', 'processes');
    setText(info, which + '. CPU: share of the whole processor over the last ' + seconds + ' (' +
      count(cores, 'core', 'cores') + busy + ').');
    say(data.hidden ? 'This server hides the processes of other accounts from the panel (/proc is mounted with ' +
      'hidepid): ' + (own ? 'yours cannot show.' : 'only the panel\'s own show.') : '');
  }

  function later(delay, id) {
    setTimeout(function () { if (id === run && running) { ask(id); } }, delay);
  }

  function ask(id) {
    $.ajax({
      url: box.getAttribute('data-url'),
      dataType: 'json',
      statusCode: { 401: function () { window.location.replace(box.getAttribute('data-logout')); } },
      success: function (data) {
        if (id !== run) { return; }
        show(data);
        later(EVERY, id);
      },
      error: function (xhr) {
        if (id !== run || (xhr && xhr.status === 401)) { return; }
        say('The panel did not answer: trying again in a moment.');
        later(RETRY, id);
      }
    });
  }

  // Start or stop asking, as the section and the browser tab say.
  function update() {
    var wanted = section.open && !document.hidden;
    if (wanted === running) { return; }
    running = wanted;
    run += 1;
    if (wanted) {
      say('Reading the processes\u2026');
      ask(run);
    }
  }

  section.addEventListener('toggle', update);
  document.addEventListener('visibilitychange', update);
  update();  // open already when the browser remembers it so (static/js/overview.js, loaded before)
})();
