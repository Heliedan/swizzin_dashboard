/* Reboot required and pending updates (see the README): two pills after the title of At a glance, on the admin's
   Overview only (templates/overview/system_status.html, core/system_status.py).

   They come from /stats/system-status: when the page loads, then every 10 minutes. The panel works out the updates in
   the background (a few seconds, after apt or dpkg changed something): meanwhile it says so, and the page asks again
   every 5 seconds. A pill shows only when it has something to say: "Reboot required", "12 updates · 3 security"
   (orange when there are security updates), or what keeps the count from being known. A tap or a click opens its
   details (a <details> element: the keyboard and screen readers know it); opening one closes the other.
   A failed request hides them until the next one, a minute later. 401 (logged out) goes to the logout page, as the
   dashboard's other refreshes do. Text only, never HTML: package names and apt's messages come from the system. */
(function () {
  'use strict';

  var box = document.getElementById('ov-status');
  if (!box) { return; }  // another account than the admin: nothing to ask
  var EVERY = 600000, SOON = 5000, RETRY = 60000;  // ms
  var MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  var PILLS = ['reboot', 'updates'];

  function pad(n) { return (n < 10 ? '0' : '') + n; }
  function when(seconds) {  // "Nov 7, 06:12", in the browser's time zone
    var d = new Date(seconds * 1000);
    return MONTHS[d.getMonth()] + ' ' + d.getDate() + ', ' + pad(d.getHours()) + ':' + pad(d.getMinutes());
  }
  function count(n, one, many) { return n + ' ' + (n === 1 ? one : many); }
  function names(list, more) { return list.join(', ') + (more ? ' and ' + count(more, 'other', 'others') : ''); }

  // [text, details, level] of each pill, or null to hide it.
  function reboot(r) {
    if (!r || !r.required) { return null; }
    if (r.kernel) {
      return ['Reboot required', 'Kernel ' + r.kernel + ' is installed, and ' + r.running + ' is running.', 'warning'];
    }
    return ['Reboot required', r.packages && r.packages.length
      ? 'Asked for by ' + names(r.packages, r.more) + '.' : 'The system asks for a reboot.', 'warning'];
  }

  function updates(u) {
    if (!u) { return null; }
    if (u.state === 'checking') {
      return ['Checking for updates…', 'apt-get is working out the pending updates: a few seconds.', 'quiet'];
    }
    if (u.state === 'error') {
      return ['Updates unknown', 'apt-get could not tell: ' + u.reason + '.', 'quiet'];
    }
    if (u.state === 'no-lists') {
      return ['Updates unknown', 'apt has no package lists yet: apt update downloads them.', 'quiet'];
    }
    if (u.state !== 'ok') { return null; }  // off in swizzin.cfg, or no apt on this system
    var lists = u.lists ? 'Package lists from ' + when(u.lists) +
      (u.lists_old ? ' (' + count(u.lists_days, 'day', 'days') + ' ago): newer updates show once apt update runs.'
        : '.') : '';
    if (u.total > 0) {
      return [count(u.total, 'update', 'updates') + (u.security ? ' · ' + u.security + ' security' : ''),
              (u.security ? 'Security: ' + names(u.names, u.more) + '. ' : '') + lists,
              u.security ? 'warning' : 'quiet'];
    }
    if (u.lists_old) {
      return ['Package lists ' + count(u.lists_days, 'day', 'days') + ' old', 'No update in them. ' + lists, 'quiet'];
    }
    return null;  // up to date
  }

  function setText(id, value) {
    var el = document.getElementById(id);
    if (el.textContent !== value) { el.textContent = value; }
  }

  function show(name, pill) {
    var el = document.getElementById('ov-' + name);
    if (pill) {
      setText('ov-' + name + '-text', pill[0]);
      setText('ov-' + name + '-more', pill[1]);
      if (el.getAttribute('data-level') !== pill[2]) { el.setAttribute('data-level', pill[2]); }
    }
    if (!pill && el.open) { el.open = false; }  // when it shows again, its details stay closed
    if (el.hidden !== !pill) { el.hidden = !pill; }
    return !!pill;
  }

  function render(data) {
    var shown = show('reboot', reboot(data && data.reboot));
    shown = show('updates', updates(data && data.updates)) || shown;
    if (box.hidden !== !shown) { box.hidden = !shown; }
  }

  function refresh() {
    $.ajax({
      url: box.getAttribute('data-url'),
      dataType: 'json',
      statusCode: { 401: function () { window.location.replace(box.getAttribute('data-logout')); } },
      success: function (data) {
        render(data);
        setTimeout(refresh, data && data.updates && data.updates.checking ? SOON : EVERY);
      },
      error: function () {
        render(null);
        setTimeout(refresh, RETRY);
      }
    });
  }

  PILLS.forEach(function (name) {  // one pill open at a time: their details show at the same place
    var el = document.getElementById('ov-' + name);
    el.addEventListener('toggle', function () {
      if (!el.open) { return; }
      PILLS.forEach(function (other) {
        var o = document.getElementById('ov-' + other);
        if (o !== el && o.open) { o.open = false; }
      });
    });
  });
  refresh();
})();
