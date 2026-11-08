/* Customize, on the Overview layout of the dashboard (templates/overview.html, see the README): each account may move
   the blocks of the page within their zone, hide or show them, and put a section of a column of Details in the other
   column. The panel renders the page in the account's order (core/layouts.py); this script makes the editor and sends
   what it gives. The editor is made from the blocks listed on the Customize button (data-blocks): those of this page
   only, in their order, each with its label, its zone, its zone by default, whether it is hidden and whether it is
   hidden by default, and their default order. A block added to core/overview_blocks.py is listed there like the
   others: nothing to write here for it.
   The editor opens under the heading of At a glance: one list per zone, each block with Move earlier, Move later, Hide
   (a toggle: aria-pressed; a block hidden by default starts pressed) and, for a section of a column of Details, a
   button to the other column (none for the sections across the whole width of Details). Nothing changes on the page
   before Done, which sends the arrangement (POST, JSON) and loads the page again in the new order; Cancel closes the
   editor and forgets the changes; Reset to default puts the default arrangement in the editor, for Done to keep. The
   focus stays on the button pressed, and a status region says where the block went ("CPU: position 2 of 4 in
   Tiles."). After Done, the page that loads again gives the focus back to Customize and says that it is saved (the
   browser's sessionStorage carries that over; blocked or absent, the page just loads).
   Built with the DOM, the text in textContent: never HTML. */
(function () {
  'use strict';

  var button = document.getElementById('ov-customize');
  if (!button) { return; }
  var data;
  try { data = JSON.parse(button.getAttribute('data-blocks')); } catch (e) { data = null; }
  if (!data || !Array.isArray(data.blocks) || !data.blocks.length || !Array.isArray(data.zones)) { return; }

  var KEY = 'overview.arranged';  // sessionStorage: "1" while the page loads again after Done
  var NAMES = { glance: 'Heading of At a glance', tiles: 'Tiles', row: 'Under the tiles',
                left: 'Details, left column', right: 'Details, right column', wide: 'Details, full width' };
  var COLUMNS = { left: 'right', right: 'left' };  // as OTHER_COLUMN of core/layouts.py
  var heading = button.parentNode;
  var blocks = {};  // id: the block, as listed on the button
  var items = {};  // id: its line in the editor
  var parts = {};  // id: the buttons of its line, and its "Hidden" tag
  var lists = {};  // zone: its list in the editor
  var zones = [];  // the zones of the editor, in the order of the page
  var editor = null, message = null, actions = {};
  var busy = false;

  data.blocks.forEach(function (b) {
    if (b && typeof b.id === 'string' && data.zones.indexOf(b.zone) >= 0 && data.zones.indexOf(b.home) >= 0) {
      blocks[b.id] = b;
    }
  });

  function name(zone) { return Object.prototype.hasOwnProperty.call(NAMES, zone) ? NAMES[zone] : zone; }
  function children(list) { return Array.prototype.slice.call(list.children); }
  function label(li) { return blocks[li.getAttribute('data-block')].label; }
  function zoneOf(li) { return li.parentNode.getAttribute('data-zone'); }
  function isHidden(li) { return li.hasAttribute('data-hidden'); }
  function focus(el) { if (el && el.focus) { el.focus(); } }

  function el(tag, cls, text, id) {
    var e = document.createElement(tag);
    if (cls) { e.className = cls; }
    if (id) { e.setAttribute('id', id); }
    if (text) { e.textContent = text; }
    return e;
  }

  function control(id, cls, text, onClick) {
    var b = el('button', 'btn btn-sm ' + cls, text, id);
    b.setAttribute('type', 'button');
    b.addEventListener('click', function () {
      if (!busy) { onClick(); }
    });
    return b;
  }

  var status = el('p', 'sr-only', '', 'ov-arrange-status');  // always there: a live region must exist before it speaks
  status.setAttribute('role', 'status');
  status.setAttribute('aria-live', 'polite');
  heading.appendChild(status);

  function say(text) {  // the same words twice are still said: a no-break space makes them new
    status.textContent = text && text === status.textContent ? text + '\u00a0' : text || '';
  }

  // Where each block is: {zone: [ids]} and the hidden ones, as listed on the button (the account's arrangement), or
  // the default arrangement: each block in its own zone, in the order of core/overview_blocks.py, those hidden by
  // default hidden.
  function saved() {
    var where = {}, hidden = {};
    data.blocks.forEach(function (b) {
      if (blocks[b.id] === b) {
        (where[b.zone] = where[b.zone] || []).push(b.id);
        if (b.hidden) { hidden[b.id] = true; }
      }
    });
    return { where: where, hidden: hidden };
  }
  function byDefault() {
    var where = {}, hidden = {};
    (Array.isArray(data.order) ? data.order : []).forEach(function (id) {
      if (Object.prototype.hasOwnProperty.call(blocks, id)) {
        (where[blocks[id].home] = where[blocks[id].home] || []).push(id);
        if (blocks[id].default_hidden === true) { hidden[id] = true; }
      }
    });
    return { where: where, hidden: hidden };
  }

  // What the editor shows, as /layout/arrange takes it: every block in the order of the page, what differs from the
  // default (the blocks hidden among those shown by default, the blocks shown among those hidden by default), and the
  // sections in the other column than their own.
  function arrangement(state) {
    var order = [], hidden = [], shown = [], other = [];
    zones.forEach(function (zone) {
      var ids = state ? state.where[zone] || [] : children(lists[zone]).map(function (li) {
        return li.getAttribute('data-block');
      });
      ids.forEach(function (id) {
        var off = state ? !!state.hidden[id] : isHidden(items[id]), offByDefault = blocks[id].default_hidden === true;
        order.push(id);
        if (off && !offByDefault) { hidden.push(id); }
        if (!off && offByDefault) { shown.push(id); }
        if (zone !== blocks[id].home) { other.push(id); }
      });
    });
    return { order: order, hidden: hidden, shown: shown, other_column: other };
  }
  function same(a, b) { return JSON.stringify(a) === JSON.stringify(b); }

  function setHidden(li, on) {
    var p = parts[li.getAttribute('data-block')];
    if (on) { li.setAttribute('data-hidden', ''); } else { li.removeAttribute('data-hidden'); }
    p.tag.hidden = !on;
    p.hide.setAttribute('aria-pressed', on ? 'true' : 'false');
  }

  // The buttons of each line, from where it is: Move earlier at the top and Move later at the bottom say they do
  // nothing (aria-disabled, so that the focus stays on them), the column button names the other column.
  function update() {
    zones.forEach(function (zone) {
      var lines = children(lists[zone]);
      lines.forEach(function (li, i) {
        var p = parts[li.getAttribute('data-block')];
        if (p.earlier) { p.earlier.setAttribute('aria-disabled', i === 0 ? 'true' : 'false'); }
        if (p.later) { p.later.setAttribute('aria-disabled', i === lines.length - 1 ? 'true' : 'false'); }
        if (p.column) {
          p.column.textContent = 'To the ' + COLUMNS[zone];
          p.column.setAttribute('aria-label', 'Move ' + label(li) + ' to the ' + COLUMNS[zone] + ' column');
        }
      });
    });
  }

  function place(state) {
    zones.forEach(function (zone) {
      (state.where[zone] || []).forEach(function (id) { lists[zone].appendChild(items[id]); });
    });
    Object.keys(items).forEach(function (id) { setHidden(items[id], !!state.hidden[id]); });
    update();
  }

  function position(li) {
    var lines = children(li.parentNode);
    return label(li) + ': position ' + (lines.indexOf(li) + 1) + ' of ' + lines.length + ' in ' + name(zoneOf(li)) +
      '.';
  }

  // The neighbour moves, not the line: the button pressed never leaves the page, and keeps the focus.
  function move(li, step) {
    var list = li.parentNode, lines = children(list), i = lines.indexOf(li), j = i + step;
    if (j < 0 || j >= lines.length) {
      say(label(li) + ' is already ' + (step < 0 ? 'first' : 'last') + ' in ' + name(zoneOf(li)) + '.');
      return;
    }
    if (step < 0) { list.insertBefore(lines[j], li.nextSibling); } else { list.insertBefore(lines[j], li); }
    update();
    say(position(li));
  }

  function toColumn(li) {
    lists[COLUMNS[zoneOf(li)]].appendChild(li);  // at the end of the other column
    update();
    focus(parts[li.getAttribute('data-block')].column);  // moved: the browser took the focus away
    say(position(li));
  }

  function toggle(li) {
    setHidden(li, !isHidden(li));
    say(label(li) + (isHidden(li) ? ': hidden.' : ': shown.'));
  }

  // A line of the editor: the block's name, then its buttons, each naming the block for a screen reader.
  function line(b, movable) {
    var id = 'ov-arrange-' + b.id, p = {};
    var li = el('li', 'ov-arrange-item', '', id);
    li.setAttribute('data-block', b.id);
    var text = el('span', 'ov-arrange-label', b.label);
    p.tag = el('span', 'ov-arrange-tag', 'Hidden');
    p.tag.setAttribute('aria-hidden', 'true');  // the Hide button says it (aria-pressed)
    p.tag.hidden = true;
    text.appendChild(p.tag);
    li.appendChild(text);
    var tools = el('span', 'ov-arrange-tools');
    if (movable) {
      [['earlier', '\u2191', -1], ['later', '\u2193', 1]].forEach(function (m) {
        p[m[0]] = control(id + '-' + m[0], 'btn-secondary ov-arrange-move', m[1], function () { move(li, m[2]); });
        p[m[0]].setAttribute('aria-label', 'Move ' + b.label + ' ' + m[0]);
        p[m[0]].setAttribute('title', 'Move ' + m[0]);
        tools.appendChild(p[m[0]]);
      });
    }
    p.hide = control(id + '-hide', 'btn-secondary ov-arrange-hide', 'Hide', function () { toggle(li); });
    p.hide.setAttribute('aria-label', 'Hide ' + b.label);
    p.hide.setAttribute('aria-pressed', 'false');
    tools.appendChild(p.hide);
    if (COLUMNS[b.home]) {
      p.column = control(id + '-column', 'btn-secondary ov-arrange-column', '', function () { toColumn(li); });
      tools.appendChild(p.column);
    }
    li.appendChild(tools);
    parts[b.id] = p;
    return li;
  }

  function build() {
    var count = {};
    Object.keys(blocks).forEach(function (id) { count[blocks[id].home] = (count[blocks[id].home] || 0) + 1; });
    data.zones.forEach(function (zone) {  // a zone with blocks; a column too when the other one has some
      var any = Object.keys(blocks).some(function (id) {
        var b = blocks[id];
        return b.zone === zone || b.home === zone || (!!COLUMNS[zone] && b.home === COLUMNS[zone]);
      });
      if (any) { zones.push(zone); }
    });
    editor = el('section', 'card ov-arrange', '', 'ov-arrange');
    editor.setAttribute('aria-labelledby', 'ov-arrange-title');
    editor.setAttribute('tabindex', '-1');
    editor.hidden = true;
    var head = el('div', 'card-header ov-arrange-head');
    head.appendChild(el('h2', 'ov-card-title', 'Customize Overview', 'ov-arrange-title'));
    var bar = el('div', 'ov-arrange-actions');
    actions.done = control('ov-arrange-done', 'btn-primary', 'Done', done);
    actions.cancel = control('ov-arrange-cancel', 'btn-secondary', 'Cancel', cancel);
    actions.reset = control('ov-arrange-reset', 'btn-secondary', 'Reset to default', reset);
    bar.appendChild(actions.done);
    bar.appendChild(actions.cancel);
    bar.appendChild(actions.reset);
    head.appendChild(bar);
    editor.appendChild(head);
    var body = el('div', 'card-body');
    body.appendChild(el('p', 'ov-arrange-help', 'Move, hide or show the blocks of this page, for your account. ' +
                                                'Nothing changes on the page before Done.'));
    message = el('p', 'ov-arrange-message', '', 'ov-arrange-message');
    body.appendChild(message);
    var grid = el('div', 'ov-arrange-zones');
    zones.forEach(function (zone) {
      var box = el('div', 'ov-arrange-zone');
      box.appendChild(el('h3', 'ov-arrange-zone-title', name(zone), 'ov-arrange-zone-' + zone));
      var list = el('ul', 'ov-arrange-list', '', 'ov-arrange-list-' + zone);
      list.setAttribute('data-zone', zone);
      list.setAttribute('aria-labelledby', 'ov-arrange-zone-' + zone);
      lists[zone] = list;
      box.appendChild(list);
      grid.appendChild(box);
    });
    body.appendChild(grid);
    editor.appendChild(body);
    Object.keys(blocks).forEach(function (id) {
      var b = blocks[id];
      items[id] = line(b, !!COLUMNS[b.home] || count[b.home] > 1);
    });
    heading.parentNode.insertBefore(editor, heading.nextSibling);
    button.setAttribute('aria-controls', 'ov-arrange');
  }

  function setBusy(on) {
    busy = on;
    [actions.done, actions.cancel, actions.reset].forEach(function (b) {
      b.setAttribute('aria-disabled', on ? 'true' : 'false');
    });
  }

  function open() {
    if (!editor) { build(); }
    place(saved());
    message.textContent = '';
    editor.hidden = false;
    button.setAttribute('aria-expanded', 'true');
    focus(editor);
  }

  function close(words) {
    editor.hidden = true;
    button.setAttribute('aria-expanded', 'false');
    focus(button);
    say(words);
  }

  function cancel() {
    close(same(arrangement(), arrangement(saved())) ? '' : 'Changes discarded.');
  }

  function reset() {
    place(byDefault());
    say('Default arrangement in the editor: Done keeps it.');
  }

  function done() {
    var now = arrangement();
    if (same(now, arrangement(saved()))) {
      close('Nothing changed.');
      return;
    }
    setBusy(true);
    message.textContent = 'Saving\u2026';
    $.ajax({
      type: 'POST',
      url: button.getAttribute('data-url'),
      data: JSON.stringify(same(now, arrangement(byDefault())) ? { reset: true } : now),
      contentType: 'application/json',
      dataType: 'json',
      statusCode: {
        401: function () { window.location.replace(button.getAttribute('data-logout')); }
      },
      success: function () {
        try { window.sessionStorage.setItem(KEY, '1'); } catch (e) { /* the page loads all the same */ }
        window.location.reload();
      },
      error: function (xhr) {
        if (xhr && xhr.status === 401) { return; }
        setBusy(false);
        message.textContent = 'Not saved: try again.';
        say('Not saved: try again.');
      }
    });
  }

  button.addEventListener('click', function () {
    if (busy) { return; }
    if (editor && !editor.hidden) { cancel(); } else { open(); }
  });
  button.setAttribute('aria-expanded', 'false');
  button.hidden = false;

  var arranged = false;  // the page loads again after Done
  try {
    arranged = window.sessionStorage.getItem(KEY) === '1';
    if (arranged) { window.sessionStorage.removeItem(KEY); }
  } catch (e) { arranged = false; }
  if (arranged) {
    focus(button);
    setTimeout(function () { say('Your arrangement of Overview is saved.'); }, 500);  // once the page has settled
  }
})();
