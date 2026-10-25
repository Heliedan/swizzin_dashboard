/* Updates the CPU Info, RAM Info and Disk Info cards every second ("live" event of the websocket).
   Same element ids and, for CPU and RAM, same thresholds (75 % / 90 %) as the panel's original scripts
   (templates/macros.html).
   "Disk almost full" banner: the disks take the colour of their alert level, with the
   DISK_WARN_PERCENT and DISK_CRIT_PERCENT thresholds of swizzin.cfg. See the README. */
(function () {
  'use strict';

  function levelClass(pct) { return pct > 90 ? 'bg-danger' : (pct > 75 ? 'bg-warning' : 'bg-success'); }

  // cls: a forced colour (disks), else the one of the 75 % / 90 % thresholds (CPU, RAM).
  function setColour(el, pct, cls) {
    if (!el) { return; }
    el.classList.remove('bg-success', 'bg-warning', 'bg-danger', 'bg-secondary', 'disk-unavailable');
    el.classList.add(cls || levelClass(pct));
  }

  function setBar(el, pct, cls) {
    if (!el) { return; }
    var p = Math.max(0, Math.min(100, Number(pct) || 0));
    el.style.width = p + '%';
    el.setAttribute('aria-valuenow', Math.trunc(p));
    setColour(el, p, cls);
  }

  // Colour of a disk: the one of its alert level, computed by the server with DISK_WARN_PERCENT and
  // DISK_CRIT_PERCENT (swizzin.cfg). Same thresholds and same colours as the banner, not the 75 % / 90 % scale.
  function diskColour(level) {
    return level === 'critical' ? 'bg-danger' : (level === 'warning' ? 'bg-warning' : 'bg-success');
  }

  function setText(id, value) {
    var el = document.getElementById(id);
    if (el) { el.textContent = value; }
  }

  function perCore(values) {
    var box = document.getElementById('cpupercore');
    if (!box) { return; }
    if (box.children.length !== values.length) {  // builds the "Core n" rows once
      box.textContent = '';
      values.forEach(function (v, i) {
        var row = document.createElement('div');
        row.className = 'd-flex align-items-center small mb-1';
        var name = document.createElement('span');
        name.style.width = '4.5em';
        name.textContent = 'Core ' + (i + 1);
        var frame = document.createElement('div');
        frame.className = 'progress flex-grow-1 mx-2';
        frame.style.height = '8px';
        var b = document.createElement('div');
        b.className = 'progress-bar bg-success';
        b.setAttribute('role', 'progressbar');
        frame.appendChild(b);
        var val = document.createElement('span');
        val.className = 'text-right';
        val.style.width = '3.5em';
        row.appendChild(name); row.appendChild(frame); row.appendChild(val);
        box.appendChild(row);
      });
    }
    values.forEach(function (v, i) {
      var row = box.children[i];
      setBar(row.children[1].firstChild, v);
      row.children[2].textContent = v + '%';
    });
  }

  function cpu(c) {
    setText('cpuused', c.used);
    setText('cpuidle', c.idle);
    setText('cpucores', c.cores);
    setText('cpupercent', c.used);
    setText('cpumodel', c.model + (c.freq ? ' · ' + c.freq : ''));
    setBar(document.getElementById('cpuprogress'), c.used);
    setColour(document.getElementById('cpuindicator'), Number(c.used));
    var ci = document.getElementById('cpuindicator'); if (ci) { ci.title = 'CPU : ' + c.used + ' %'; }
    perCore(c.percore);
    setText('cpuuser', c.user);
    setText('cpusystem', c.system);
    setText('cpuiowait', c.iowait);
    setText('cpusteal', c.steal);
  }

  function ram(r) {
    setText('ramused', r.ramused);
    setText('ramfree', r.ramfree);
    setText('ramtotal', r.ramtotal);
    setText('rampercent', r.perutil);
    setBar(document.getElementById('ramprogress'), r.perutil);
    setColour(document.getElementById('ramindicator'), Number(r.perutil));
    var ri = document.getElementById('ramindicator'); if (ri) { ri.title = 'RAM : ' + r.perutil + ' %'; }
  }

  function setDiskState(m, unavailable) {  // "UNAVAILABLE" badge above the disk's name, % line hidden
    var cell = document.getElementById(m + 'diskused');
    var name = cell && cell.parentElement ? cell.parentElement.firstElementChild : null;
    var badge = name ? name.querySelector('.disk-badge') : null;
    if (unavailable && name && !badge) {  // above the disk's name
      badge = document.createElement('div');
      badge.className = 'disk-badge mb-1';
      var label = document.createElement('span');
      label.className = 'badge badge-danger';
      label.textContent = 'UNAVAILABLE';
      badge.appendChild(label);
      name.insertBefore(badge, name.firstChild);
    }
    if (!unavailable && badge) { badge.parentNode.removeChild(badge); }
    var pct = document.getElementById(m + 'diskpercent');  // "87.00%": the line is emptied, not hidden (keeps the outline)
    if (pct) {
      if (unavailable) { pct.textContent = ''; }
      if (pct.nextSibling && pct.nextSibling.nodeType === 3) { pct.nextSibling.nodeValue = unavailable ? '' : '%'; }
    }
  }

  function disks(d) {
    // Disks shown on the page but missing from the data (unmounted since the page loaded): unavailable.
    var card = document.getElementById('disk-card');
    if (card) {
      // 1st live reading received: the /stats/disk poll of every minute (templates/macros.html) stops.
      if (!card.hasAttribute('data-live')) { card.setAttribute('data-live', ''); }
      Array.prototype.forEach.call(card.querySelectorAll('[id$="diskused"]'), function (el) {
        var m = el.id.slice(0, -'diskused'.length);
        if (!(m in d)) { d[m] = { unavailable: true }; }
      });
    }
    Object.keys(d).forEach(function (m) {
      var x = d[m];
      if (x.unavailable) {  // unreadable disk (unplugged, network share lost...): no stale numbers
        ['diskused', 'diskfree', 'disktotal'].forEach(function (k) { setText(m + k, ''); });
        ['diskread', 'diskwrite'].forEach(function (k) { setText(m + k, '—'); });
        var b = document.getElementById(m + 'diskprogress');
        if (b) { b.style.width = '0%'; b.setAttribute('aria-valuenow', 0); }
        setDiskState(m, true);
        // The dots tell how full a disk is: an unreadable disk has no colour but the ⊘ symbol (grey, struck through).
        var p = document.getElementById(m + 'indicator');
        if (p) { p.classList.remove('bg-success', 'bg-warning', 'bg-danger', 'bg-secondary'); p.classList.add('disk-unavailable'); p.title = m + ' : unavailable'; }
        return;
      }
      setDiskState(m, false);
      setText(m + 'diskused', x.diskused);
      setText(m + 'diskfree', x.diskfree);
      setText(m + 'disktotal', x.disktotal);
      setText(m + 'diskpercent', x.perutil);
      setText(m + 'diskread', x.read || '—');  // no speeds without a /dev/ device (ZFS, NFS...)
      setText(m + 'diskwrite', x.write || '—');
      setBar(document.getElementById(m + 'diskprogress'), x.perutil, diskColour(x.alert));
      var ind = document.getElementById(m + 'indicator');
      setColour(ind, Number(x.perutil), diskColour(x.alert));
      if (ind) { ind.title = m + ' : ' + x.perutil + ' %'; }  // on hover: how full it is, in plain text
    });
  }

  // "Disk almost full" banner at the top of the dashboard, one box per disk of the Disk Info card
  // that reached a threshold (warning in orange, critical in red). It follows each reading:
  // it appears and disappears live. The mount path comes from the system (spaces, special characters):
  // it is never inserted as HTML, only through textContent.
  // Screen readers: during a download, the free space of a line changes nearly every second. So the
  // visible lines are not a "live" region. Only the hidden summary #disk-alert-summary (role="status"
  // aria-live="polite", templates/index.html) is read: it names only the disks in alert and their level, it
  // changes only when a disk enters an alert, leaves it or changes level. Nothing is touched without a change.
  // An unreadable disk (UNAVAILABLE) leaves the banner, but how full it is is unknown: it may
  // still be full (dead network share), so the summary never says it no longer is.
  function alerts(d) {
    var banner = document.getElementById('disk-alert');
    var summary = document.getElementById('disk-alert-summary');
    var card = document.getElementById('disk-card');
    if (!banner || !card) { return; }  // no Disk Info card for this user: no banner either
    var lines = [], unreadable = [];
    Object.keys(d).forEach(function (m) {
      var x = d[m];
      if (!card.contains(document.getElementById(m + 'diskused'))) { return; }  // disk missing from the card
      if (x.unavailable) { unreadable.push(m); return; }
      if (x.alert !== 'warning' && x.alert !== 'critical') { return; }
      var pct = Math.trunc(Number(x.perutil) || 0);  // truncated: "90%" never shows below the threshold of 90
      lines.push({ m: m, level: x.alert, pct: pct,
                   text: 'Disk almost full: ' + m + ' is ' + pct + '% used (' + x.diskfree + ' free)' });
    });
    lines.sort(function (a, b) { return b.pct - a.pct; });  // the fullest disk first
    lines.forEach(function (l, i) {
      // text-break: a long path with no space wraps instead of running out of the screen on a phone.
      var cls = 'alert mt-3 mb-0 text-break ' + (l.level === 'critical' ? 'alert-danger' : 'alert-warning');
      var item = banner.children[i];
      if (!item) {
        item = document.createElement('div');
        item.className = cls;
        item.textContent = l.text;
        banner.appendChild(item);
        return;
      }
      if (item.className !== cls) { item.className = cls; }
      if (item.textContent !== l.text) { item.textContent = l.text; }
    });
    while (banner.children.length > lines.length) {  // disks back below the threshold, unmounted or unreadable
      banner.removeChild(banner.lastElementChild);
    }
    if (summary) {
      var sentence = summarize(lines, unreadable, summary.textContent);
      if (summary.textContent !== sentence) { summary.textContent = sentence; }
    }
  }

  // Summary for screen readers: the red disks then the orange ones, each in the order of their paths (the order
  // of how full they are would change without any level changing). Neither percentage nor free space. Empty as
  // long as no disk has been in alert; when the last one leaves, it says so, except for unreadable disks:
  // those are named, without saying that they are no longer full.
  function summarize(lines, unreadable, previous) {
    var levels = { critical: [], warning: [] };
    lines.forEach(function (l) { levels[l.level].push(l.m); });
    var parts = [];
    if (levels.critical.length) { parts.push('critical: ' + levels.critical.sort().join(', ')); }
    if (levels.warning.length) { parts.push('warning: ' + levels.warning.sort().join(', ')); }
    if (parts.length) { return 'Disk almost full, ' + parts.join('; '); }
    if (!previous) { return ''; }
    if (unreadable.length) {
      return 'No readable disk is almost full anymore. Unavailable: ' + unreadable.sort().join(', ') + '.';
    }
    return 'No disk is almost full anymore.';
  }

  document.addEventListener('DOMContentLoaded', function () {
    var stack = document.querySelector('.live-stack');
    if (!stack || typeof io === 'undefined') { return; }
    var base = stack.getAttribute('data-urlbase') || '';
    var socket = io.connect(window.location.protocol + '//' + document.domain + ':' + window.location.port + '/websocket',
                            { path: base + '/socket.io' });
    socket.on('live', function (d) {
      if (d.cpu) { cpu(d.cpu); }
      if (d.ram) { ram(d.ram); }
      if (d.disks) { disks(d.disks); alerts(d.disks); }
    });
  });
})();
