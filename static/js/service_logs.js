/* The Logs buttons of Service Info, in the Overview layout (the admin only, see the README). A click asks the panel
   for the last lines of that service's journal (POST /apps/logs, core/service_logs.py) and shows them in the panel
   under the row (templates/overview/services.html), with what systemd says of the service: running or not, since
   when, and its automatic restarts. Refresh asks again; Close hides the panel and gives the focus back to the button.
   Nothing is asked before a click, nothing is kept, and the lines only ever go in as text. One read at a time: the
   buttons say they are unavailable meanwhile. */
(function () {
  'use strict';

  var panel = document.getElementById('ov-logs');
  if (!panel) { return; }
  var title = document.getElementById('ov-logs-title');
  var stateLine = document.getElementById('ov-logs-state');
  var message = document.getElementById('ov-logs-message');
  var lines = document.getElementById('ov-logs-lines');
  var refresh = document.getElementById('ov-logs-refresh');
  var close = document.getElementById('ov-logs-close');
  var buttons = Array.prototype.slice.call(document.querySelectorAll('button[data-logs]'));
  var MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  var STATES = { active: 'Running', reloading: 'Reloading', activating: 'Starting', deactivating: 'Stopping',
                 inactive: 'Stopped', failed: 'Failed' };
  var RESTARTS = 'Restarts that systemd made by itself (Restart= in the service), since the service was last ' +
                 'started by hand or since the server started';
  var current = null;  // the button whose logs the panel shows
  var busy = false;

  function words(text) { return String(text).replace(/\s+/g, ' ').trim(); }
  function pad(n) { return (n < 10 ? '0' : '') + n; }

  // "Nov 7, 02:20" in the browser's time zone, with the year when it is not this one.
  function when(seconds) {
    var d = new Date(seconds * 1000);
    var text = MONTHS[d.getMonth()] + ' ' + d.getDate();
    if (d.getFullYear() !== new Date(Date.now()).getFullYear()) { text += ' ' + d.getFullYear(); }
    return text + ', ' + pad(d.getHours()) + ':' + pad(d.getMinutes());
  }

  function known(state) { return !!state && typeof state === 'object' && state.load !== 'not-found'; }

  // "sonarr.service \u00b7 Running since Nov 7, 02:20 \u00b7 Automatic restarts: 4"
  function describe(state, unit) {
    if (!state || typeof state !== 'object') { return unit + ' \u00b7 systemd did not say how it is'; }
    if (!known(state)) { return unit + ' \u00b7 systemd does not know this service'; }
    var text = Object.prototype.hasOwnProperty.call(STATES, state.active) ? STATES[state.active] : 'State unknown';
    if (state.active === 'active' && state.sub && state.sub !== 'running') { text = 'Active (' + state.sub + ')'; }
    if (typeof state.since === 'number' && /^(active|reloading|inactive|failed)$/.test(state.active)) {
      text += ' since ' + when(state.since);
    }
    if (state.active === 'failed' && state.result && state.result !== 'success') { text += ' (' + state.result + ')'; }
    if (typeof state.restarts === 'number') { text += ' \u00b7 Automatic restarts: ' + state.restarts; }
    return unit + ' \u00b7 ' + text;
  }

  function say(text) {  // a live region: always there, empty when there is nothing to say
    message.textContent = text || '';
  }

  function setBusy(on) {
    busy = on;
    buttons.concat([refresh]).forEach(function (b) {
      if (on) {
        b.setAttribute('aria-disabled', 'true');
        b.classList.add('disabled');
      } else {
        b.removeAttribute('aria-disabled');
        b.classList.remove('disabled');
      }
    });
  }

  function show(data) {
    var found = data && Array.isArray(data.lines) ?
      data.lines.filter(function (l) { return typeof l === 'string'; }) : [];
    var unit = data && typeof data.unit === 'string' ? data.unit : '';
    stateLine.textContent = describe(data && data.state, unit);
    stateLine.title = data && known(data.state) && typeof data.state.restarts === 'number' ? RESTARTS : '';
    say(data && typeof data.message === 'string' ? data.message : '');
    lines.textContent = found.join('\n');
    lines.hidden = !found.length;
    if (typeof lines.scrollHeight === 'number') { lines.scrollTop = lines.scrollHeight; }  // the newest in view
  }

  function load(button) {
    if (busy) { return; }
    var name = button.getAttribute('data-logs');
    setBusy(true);
    say('Reading the journal\u2026');
    $.ajax({
      type: 'POST',
      url: panel.getAttribute('data-url'),
      data: JSON.stringify({ application: name }),
      contentType: 'application/json',
      dataType: 'json',
      statusCode: {
        401: function () { window.location.replace(panel.getAttribute('data-logout')); }
      },
      success: function (data) {
        setBusy(false);
        if (current === button) { show(data); }
      },
      error: function (xhr) {
        setBusy(false);
        if (current !== button || (xhr && xhr.status === 401)) { return; }
        var status = xhr && xhr.status;
        say(status === 403 ? 'Only the admin can read the logs.'
          : status === 404 ? 'This service is no longer in Service Info: reload the page.'
          : 'The panel did not answer: try again.');
      }
    });
  }

  function open(button) {
    if (busy) { return; }
    if (current && current !== button) { current.setAttribute('aria-expanded', 'false'); }
    current = button;
    button.setAttribute('aria-expanded', 'true');
    title.textContent = words(button.textContent);  // "Logs of Sonarr"
    stateLine.textContent = '';
    stateLine.title = '';
    lines.textContent = '';
    lines.hidden = true;
    panel.hidden = false;
    if (panel.focus) { panel.focus(); }
    load(button);
  }

  buttons.forEach(function (button) {
    button.addEventListener('click', function () { open(button); });
  });
  refresh.addEventListener('click', function () {
    if (current) { load(current); }
  });
  close.addEventListener('click', function () {
    var button = current;
    current = null;
    panel.hidden = true;
    if (button) {
      button.setAttribute('aria-expanded', 'false');
      if (button.focus) { button.focus(); }
    }
  });
})();
