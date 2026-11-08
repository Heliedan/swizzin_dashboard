/* Panel sign-ins (see the README): a section of the Details of the admin's Overview (templates/overview/signins.html,
   core/signins.py). Two short lists: the recent sign-ins (when, account, address), and the failed attempts (when, the
   name when it is an account, address, how many, and for how long the login protection then refused the address).

   They come from /signins when the section is opened, or when the page loads with it open, then every minute while it
   stays open: a closed section asks nothing. One request at a time. 401 (logged out) goes to the logout page, as the
   dashboard's other refreshes do; another failure says so, keeps the lists shown, and tries again a minute later. The
   address of this browser, as the panel sees it, is marked "your address" (other devices behind the same router share
   it). Times in the browser's time zone. Text only, never HTML. */
(function () {
  'use strict';

  var box = document.getElementById('ov-signins');
  if (!box) { return; }  // another account than the admin: nothing to ask
  var section = box.parentNode;  // the <details> of the section
  while (section && section.tagName !== 'DETAILS') { section = section.parentNode; }
  if (!section) { return; }
  var EVERY = 60000;  // ms
  var LONG = 20;  // characters: a longer address may break anywhere; an IPv4 one never does
  var MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  var asking = false, waiting = false, shown = null;

  function pad(n) { return (n < 10 ? '0' : '') + n; }
  function when(seconds) {  // "Nov 7, 06:12"; "Dec 30 2025, 21:04" in another year than this one
    var d = new Date(seconds * 1000);
    var year = d.getFullYear() !== new Date(Date.now()).getFullYear() ? ' ' + d.getFullYear() : '';
    return MONTHS[d.getMonth()] + ' ' + d.getDate() + year + ', ' + pad(d.getHours()) + ':' + pad(d.getMinutes());
  }
  function count(n, one, many) { return n + ' ' + (n === 1 ? one : many); }

  function setText(id, value) {
    var el = document.getElementById(id);
    if (el.textContent !== value) { el.textContent = value; }
    return el;
  }

  function cell(row, text, cls) {
    var td = document.createElement('td');
    td.textContent = text;
    if (cls) { td.className = cls; }
    row.appendChild(td);
    return td;
  }
  function more(td, text, cls) {  // a second line in a cell: "your address", "blocked 15 min"
    var span = document.createElement('span');
    span.className = cls;
    span.textContent = text;
    td.appendChild(span);
  }
  function address(row, value, yours) {  // a long IPv6 address may break anywhere (static/css/overview.css)
    var td = cell(row, value === null ? 'invalid address' : value, 'ov-signins-address' +
                  (value === null ? ' ov-signins-quiet' : value.length > LONG ? ' ov-signins-long' : ''));
    if (value !== null && value === yours) { more(td, 'your address', 'ov-signins-you'); }
  }

  function signin(r, yours) {
    var row = document.createElement('tr');
    cell(row, when(r.at), 'ov-signins-when');
    cell(row, r.user);
    address(row, r.address, yours);
    return row;
  }
  function failure(r, yours) {
    var row = document.createElement('tr');
    cell(row, when(r.last), 'ov-signins-when');
    cell(row, r.user === null ? 'unknown account' : r.user, r.user === null ? 'ov-signins-quiet' : '');
    address(row, r.address, yours);
    var tries = cell(row, '×' + r.count, 'ov-signins-count');
    if (r.blocked > 0) { more(tries, 'blocked ' + Math.ceil(r.blocked / 60) + '\u00a0min', 'ov-signins-blocked'); }
    return row;
  }

  function fill(list, rows, total, make, data) {
    var body = document.getElementById('ov-signins-' + list);
    while (body.firstChild) { body.removeChild(body.firstChild); }
    rows.forEach(function (r) { body.appendChild(make(r, data.your_address)); });
    document.getElementById('ov-signins-' + list + '-table').hidden = !rows.length;
    var period = data.on_disk ? 'in the last ' + count(data.days, 'day', 'days') : 'since the panel started';
    setText('ov-signins-' + list + '-note', !rows.length ? 'None ' + period + '.'
      : total > rows.length ? 'The ' + rows.length + ' most recent of ' + total + ' ' + period + '.' : '');
  }

  function render(data) {
    var key = JSON.stringify(data);
    if (key === shown) { return; }  // nothing new: the page stays as it is
    shown = key;
    document.getElementById('ov-signins-lists').hidden = false;  // hidden until the first answer
    fill('ok', data.signins, data.signins_total, signin, data);
    fill('failed', data.failures, data.failures_total, failure, data);
    setText('ov-signins-about', 'A sign-in is the first request of an account from an address after ' +
      count(data.session, 'minute', 'minutes') + ' without one: a page left open counts once. Failed attempts of ' +
      'the same name from the same address less than ' + count(data.group, 'minute', 'minutes') + ' apart make one ' +
      'line; a name that is not an account of the panel is never kept. ' + (data.on_disk
        ? 'Kept ' + count(data.days, 'day', 'days') + ' after their last request, ' + data.kept + ' of each at most, ' +
          'in a file only the panel can read.'
        : 'Kept in memory only: the panel forgets them when it restarts.'));
  }

  function say(text) {
    setText('ov-signins-message', text).hidden = !text;
  }

  function ask() {
    if (asking) { return; }
    asking = true;
    $.ajax({
      url: box.getAttribute('data-url'),
      dataType: 'json',
      statusCode: { 401: function () { window.location.replace(box.getAttribute('data-logout')); } },
      success: function (data) {
        asking = false;
        if (data && data.signins && data.failures) {
          say('');
          render(data);
        } else {
          say('The sign-ins could not be read: trying again in a minute.');
        }
        later();
      },
      error: function () {
        asking = false;
        say('The sign-ins could not be read: trying again in a minute.');
        later();
      }
    });
  }

  function later() {  // one timer at a time; when it ends, it asks only if the section is still open
    if (waiting || !section.open) { return; }
    waiting = true;
    setTimeout(function () {
      waiting = false;
      if (section.open) { ask(); }
    }, EVERY);
  }

  section.addEventListener('toggle', function () { if (section.open) { ask(); } });
  if (section.open) { ask(); }  // remembered open by static/js/overview.js
})();
