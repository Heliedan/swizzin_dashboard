'use strict';
/* A small fake DOM to run the dashboard's live scripts (static/js/live_graphs.js, static/js/live_stats.js), the
   Overview layout's (static/js/overview.js), the layout menu of static/js/swizzin.js, and the dashboard's polls in
   Node.js, with no npm package. Driven by tests/test_disk_alert_js.py, tests/test_bandwidth_js.py,
   tests/test_live_graphs_js.py and tests/test_overview_js.py, which send on stdin, as JSON:
     page       the HTML the panel rendered, as a tree: {tag, attrs, children} or {text}
     scripts    the paths of the scripts to run, in the order of the page
     poll       the source of a poll found in the page (diskusage(), vnstat_top10()), or null
     socketio   false: socket.io did not load (no "io")
     watch      ids of elements whose text the snapshots give (optional)
     attrs      [id, attribute] pairs whose value the snapshots give (optional)
     storage    the browser's localStorage: absent (none), {key: value} (works, starts with these), "throws" (blocked:
                any use throws), "full" (reads find nothing, writes throw)
     steps      a list of steps, each one of:
                {live: disks}           a live reading with these disks only
                {event: name, data: d}  any websocket event ("speed", "live"...); "disconnect" takes no data
                {ajax: data}            the answer to the oldest pending request
                {fail: true}            the failure of the oldest pending request
                {timer: true}           the pending timers firing
                {tab: id}               a click on a tab of the page (Bootstrap's tabs)
                {scroll: [id, top]}     the user scrolling an element
                {hidden: true|false}    the browser tab hidden or shown again ("visibilitychange")
                {click: id}             a click on that element (a disabled button gets none, as in a browser)
                {clickOn: selector}     a click on the first element that matches a simple selector
                {summary: name}         a click on the <summary> of <details data-section="name">: it opens or closes
                {batch: [steps]}        several steps, with one snapshot after the last one
                a step can also carry {at: ms}: the clock (Date.now()) when it happens
   and prints a list of snapshots of the page: after loading, then after each step.
   Inserting HTML (innerHTML...) throws: system data must only ever go in as text. The one exception is the HTML
   that the panel renders for #top10 (templates/top.html, escaped by Jinja), which the vnstat_top10() poll inserts
   with jQuery's .html(): the test gives it already parsed, as {tree}. A script that writes to console.error fails
   the run: the live graphs report their own errors there instead of throwing.
   As in a browser, a <details> element fires "toggle" when it opens or closes, and a MutationObserver is told about
   the children added to or removed from what it observes, both once the script that changed them has run: here, at
   the end of the loading and of each step. */
const fs = require('fs');
const vm = require('vm');

let changes = [];  // every change made to the page during the current step: the node changed
let mutated = new Set();  // the elements whose children changed, for the MutationObservers
let toggled = [];  // the <details> opened or closed, whose "toggle" event is due
let added = [];  // every node added during the current step, as it was when added: {parent, cls, text}

class Text {
  constructor(value) { this.nodeType = 3; this.value = value; this.parentNode = null; }
  get nodeValue() { return this.value; }
  set nodeValue(v) { this.value = String(v); changes.push(this); }
  get textContent() { return this.value; }
  get nextSibling() { return sibling(this); }
}

class Element {
  constructor(tag, attrs) {
    this.nodeType = 1;
    this.tagName = tag.toUpperCase();
    this.attrs = new Map(Object.entries(attrs || {}));
    this.childNodes = [];
    this.parentNode = null;
    this.style = {};
    this.scrollTop = 0;
    this.listeners = {};
    const el = this;
    this.classList = {
      add(...names) { const s = el.classes(); names.forEach(n => s.add(n)); el.setAttribute('class', [...s].join(' ')); },
      remove(...names) { const s = el.classes(); names.forEach(n => s.delete(n)); el.setAttribute('class', [...s].join(' ')); },
      contains(name) { return el.classes().has(name); },
    };
  }
  classes() { return new Set((this.getAttribute('class') || '').split(/\s+/).filter(Boolean)); }
  getAttribute(name) { return this.attrs.has(name) ? this.attrs.get(name) : null; }
  setAttribute(name, value) { this.attrs.set(name, String(value)); changes.push(this); }
  hasAttribute(name) { return this.attrs.has(name); }
  removeAttribute(name) { this.attrs.delete(name); changes.push(this); }
  get id() { return this.getAttribute('id') || ''; }
  get className() { return this.getAttribute('class') || ''; }
  set className(v) { this.setAttribute('class', v); }
  get title() { return this.getAttribute('title') || ''; }
  set title(v) { this.setAttribute('title', v); }
  get hidden() { return this.hasAttribute('hidden'); }
  set hidden(v) { if (v) { this.setAttribute('hidden', ''); } else { this.removeAttribute('hidden'); } }
  get open() { return this.hasAttribute('open'); }
  set open(v) {  // <details>: "toggle" follows, once the script has run
    if (!!v === this.open) { return; }
    if (v) { this.setAttribute('open', ''); } else { this.removeAttribute('open'); }
    toggled.push(this);
  }
  get children() { return this.childNodes.filter(n => n.nodeType === 1); }
  get firstChild() { return this.childNodes[0] || null; }
  get firstElementChild() { return this.children[0] || null; }
  get lastElementChild() { const c = this.children; return c[c.length - 1] || null; }
  get parentElement() { return this.parentNode instanceof Element ? this.parentNode : null; }
  get nextSibling() { return sibling(this); }
  get textContent() { return this.childNodes.map(n => n.textContent).join(''); }
  set textContent(v) {  // one change, as for a browser
    this.childNodes.forEach(n => { n.parentNode = null; });
    this.childNodes = [];
    if (String(v) !== '') {
      const text = new Text(String(v));
      text.parentNode = this;
      this.childNodes.push(text);
    }
    changes.push(this);
    mutated.add(this);
  }
  get innerHTML() { throw new Error('innerHTML is not available in these tests'); }
  set innerHTML(v) { throw new Error('HTML inserted with innerHTML: ' + v); }
  set outerHTML(v) { throw new Error('HTML inserted with outerHTML: ' + v); }
  insertAdjacentHTML(where, v) { throw new Error('HTML inserted with insertAdjacentHTML: ' + v); }
  appendChild(node) { return adopt(this, node, this.childNodes.length); }
  insertBefore(node, ref) { return adopt(this, node, ref ? this.childNodes.indexOf(ref) : this.childNodes.length); }
  removeChild(node) {
    const i = this.childNodes.indexOf(node);
    if (i < 0) { throw new Error('removeChild: not a child'); }
    this.childNodes.splice(i, 1);
    node.parentNode = null;
    changes.push(this);
    mutated.add(this);
    return node;
  }
  contains(node) {
    for (; node; node = node.parentNode) { if (node === this) { return true; } }
    return false;
  }
  querySelectorAll(selector) { return descendants(this).filter(matcher(selector)); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  addEventListener(event, f) { (this.listeners[event] = this.listeners[event] || []).push(f); }
}

function adopt(parent, node, index) {
  if (node.parentNode) { node.parentNode.removeChild(node); }
  added.push({ parent, cls: node.nodeType === 1 ? node.className : null, text: node.textContent });
  node.parentNode = parent;
  parent.childNodes.splice(index, 0, node);
  changes.push(parent);
  mutated.add(parent);
  return node;
}

function sibling(node) {
  const p = node.parentNode;
  return p ? p.childNodes[p.childNodes.indexOf(node) + 1] || null : null;
}

function descendants(el) {
  const out = [];
  el.childNodes.forEach(n => { if (n.nodeType === 1) { out.push(n, ...descendants(n)); } });
  return out;
}

function matcher(selector) {  // only the selectors the scripts use: an unknown one fails the test
  let m;
  if ((m = /^(\.[\w-]+)+$/.exec(selector))) {
    const names = selector.slice(1).split('.');
    return e => names.every(n => e.classList.contains(n));
  }
  if ((m = /^\[([\w-]+)\$="([^"]*)"\]$/.exec(selector))) { return e => (e.getAttribute(m[1]) || '').endsWith(m[2]); }
  // tag, [attribute], [attribute=value] or [attribute="value"], and a tag with one of them: button[data-layout]
  if ((m = /^([a-z]+)?(?:\[([\w-]+)(?:=(?:"([^"]*)"|([\w-]+)))?\])?$/.exec(selector)) && (m[1] || m[2])) {
    const value = m[3] !== undefined ? m[3] : m[4];
    return e => (!m[1] || e.tagName === m[1].toUpperCase()) &&
      (!m[2] || (e.hasAttribute(m[2]) && (value === undefined || e.getAttribute(m[2]) === value)));
  }
  throw new Error('selector not supported by the fake DOM: ' + selector);
}

function build(node) {
  if ('text' in node) { return new Text(node.text); }
  const el = new Element(node.tag, node.attrs);
  node.children.forEach(c => adopt(el, build(c), el.childNodes.length));
  return el;
}

const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const root = build(input.page);
changes = [];  // the snapshots count what the scripts change, not the building of the page
added = [];
const listeners = {}, socketHandlers = {}, pending = [], timers = [];
let requests = 0, connected = false, clock = 0, reloads = 0;
const sent = [];  // every request of the page: {type, url, data, contentType}
let tabsShown = [];  // the tabs shown through jQuery's .tab('show') during the current step

const document = {
  domain: '127.0.0.1',
  hidden: false,
  getElementById: id => descendants(root).find(e => e.id === id) || null,
  createElement: tag => new Element(tag),
  querySelector: selector => root.querySelector(selector),
  querySelectorAll: selector => root.querySelectorAll(selector),
  addEventListener: (event, f) => { (listeners[event] = listeners[event] || []).push(f); },
};

// Bootstrap 4's tab('show') on a tab link: the other tabs of its .nav and the other panes of its .tab-content are
// no longer active; this one and its pane (href="#pane") are. No fade: the panel's tabs have none.
function showTab(link) {
  let nav = link.parentNode;
  while (nav && !(nav.classList && nav.classList.contains('nav'))) { nav = nav.parentNode; }
  nav.querySelectorAll('.nav-link').forEach(l => { l.classList.remove('active'); l.setAttribute('aria-selected', 'false'); });
  link.classList.add('active');
  link.setAttribute('aria-selected', 'true');
  const pane = document.getElementById(link.getAttribute('href').slice(1));
  pane.parentNode.children.forEach(p => p.classList.remove('active'));
  pane.classList.add('active');
  tabsShown.push(link.id);
}

// jQuery, as much as the polls and static/js/swizzin.js use: $('#' + $.escapeSelector(mount) + 'diskfree'), $('#id'),
// $('button[data-layout]'), $(element), $(function), .html(), .css(), .attr(), .removeAttr(), .addClass(),
// .removeClass(), .tab('show'), .each(), .on(), .click(), $.ajax(). A mount is wrapped by escapeSelector so that it is
// found as written. $(function) runs it when the page is loaded, before the DOMContentLoaded listeners.
const ready = [];
function $(selector) {
  let found;
  if (typeof selector === 'function') {
    ready.push(selector);
    return null;
  } else if (selector instanceof Element) {
    found = [selector];
  } else if (/^[a-z]*\[/.test(selector)) {
    found = document.querySelectorAll(selector);
  } else if (/^#[\w-]+$/.test(selector)) {
    const el = document.getElementById(selector.slice(1));
    found = el ? [el] : [];
  } else {
    const m = /^#\u0001([\s\S]*)\u0002(\w+)(?:\.([\w-]+))?$/.exec(selector);
    if (!m) { throw new Error('selector not supported by the fake jQuery: ' + selector); }
    const el = document.getElementById(m[1] + m[2]);
    found = el && (!m[3] || el.classList.contains(m[3])) ? [el] : [];
  }
  const api = {
    html(v) {
      found.forEach(e => {
        if (v !== null && typeof v === 'object' && v.tree) {  // the panel's HTML for #top10, parsed by the test
          e.textContent = '';
          v.tree.children.forEach(c => adopt(e, build(c), e.childNodes.length));
        } else {
          e.textContent = v;  // the disk poll only writes sizes and numbers
        }
      });
      return api;
    },
    css(k, v) { found.forEach(e => { e.style[k] = v; }); return api; },
    attr(k, v) {
      if (v === undefined) { return found.length ? found[0].getAttribute(k) : undefined; }
      found.forEach(e => e.setAttribute(k, v));
      return api;
    },
    removeAttr(k) { found.forEach(e => e.removeAttribute(k)); return api; },
    each(f) { found.forEach((e, i) => f.call(e, i, e)); return api; },
    on(event, f) { found.forEach(e => e.addEventListener(event, f)); return api; },
    click(f) { return api.on('click', f); },
    addClass(c) { found.forEach(e => e.classList.add(...c.split(' '))); return api; },
    removeClass(c) { found.forEach(e => e.classList.remove(...c.split(' '))); return api; },
    tab(action) {
      if (action !== 'show') { throw new Error('tab() action not supported by the fake jQuery: ' + action); }
      found.forEach(showTab);
      return api;
    },
  };
  return api;
}
$.escapeSelector = s => '\u0001' + s + '\u0002';
$.ajax = options => {
  requests += 1;
  pending.push(options);
  sent.push({ type: options.type || 'GET', url: options.url, data: options.data === undefined ? null : options.data,
              contentType: options.contentType || null });
};

class FakeDate extends Date { static now() { return clock; } }  // the clock of the steps ({at: ms})
const fakeConsole = Object.assign(Object.create(console), {
  error: (...args) => { process.exitCode = 1; console.error('console.error in the page:', ...args); },
});
// The MutationObservers of the page: told about the children added to or removed from what they observe.
const observers = [];
class FakeMutationObserver {
  constructor(callback) { this.callback = callback; }
  observe(target, options) {
    if (!options || !options.childList || options.subtree) { throw new Error('only {childList: true} is observed'); }
    observers.push({ observer: this, target });
  }
}
const sandbox = { document, $, console: fakeConsole, Date: FakeDate, MutationObserver: FakeMutationObserver,
                  window: { location: { protocol: 'http:', port: '8333', reload: () => { reloads += 1; } } },
                  setTimeout: f => { timers.push(f); } };
if (input.socketio) {
  // Several handlers per event, as socket.io allows: live_stats.js and live_graphs.js both listen to "live".
  sandbox.io = { connect: () => {
    connected = true;
    return { on: (event, f) => { (socketHandlers[event] = socketHandlers[event] || []).push(f); }, emit: () => {} };
  } };
}
// localStorage, as the input says: none, a working one, one that throws at any use (blocked), or a full one.
const stored = input.storage && typeof input.storage === 'object' ? Object.assign({}, input.storage) : null;
if (stored) {
  sandbox.window.localStorage = {
    getItem: k => (Object.prototype.hasOwnProperty.call(stored, k) ? stored[k] : null),
    setItem: (k, v) => { stored[k] = String(v); },
    removeItem: k => { delete stored[k]; },
  };
} else if (input.storage === 'throws') {
  Object.defineProperty(sandbox.window, 'localStorage', { get() { throw new Error('SecurityError: storage blocked'); } });
} else if (input.storage === 'full') {
  sandbox.window.localStorage = { getItem: () => null, setItem: () => { throw new Error('QuotaExceededError'); },
                                  removeItem: () => {} };
}
vm.createContext(sandbox);

function colours(el) {
  return el ? [...el.classes()].filter(c => c.startsWith('bg-') || c === 'disk-unavailable').sort() : null;
}

function object(pairs) {  // Object.fromEntries, missing from node 10 (Ubuntu 20.04)
  const out = {};
  pairs.forEach(([k, v]) => { out[k] = v; });
  return out;
}

// Each live graph (.live-graph-plot, static/js/live_graphs.js) and the elements whose id starts with its own: the
// "d" of its paths, the text of the others, and how many of them were changed or added during the step.
function graphs() {
  const out = {};
  root.querySelectorAll('.live-graph-plot').forEach(plot => {
    const own = descendants(root).filter(e => e.id.startsWith(plot.id + '-'));
    const mine = n => n === plot || own.some(e => e.contains(n)) || plot.contains(n);
    out[plot.id] = {
      d: object(own.filter(e => e.tagName === 'PATH').map(e => [e.id, e.getAttribute('d')])),
      text: object(own.filter(e => e.tagName !== 'PATH' && e.tagName !== 'BUTTON').map(e => [e.id, e.textContent])),
      buttons: object(own.filter(e => e.tagName === 'BUTTON').map(e => [e.id, {
        type: e.getAttribute('type'), pressed: e.getAttribute('aria-pressed'),
        disabled: e.getAttribute('aria-disabled'), title: e.getAttribute('title') }])),
      changes: changes.filter(mine).length,
      added: added.filter(a => mine(a.parent)).length,
    };
  });
  return out;
}

function snapshot() {
  const banner = document.getElementById('disk-alert');
  const summary = document.getElementById('disk-alert-summary');
  const card = document.getElementById('disk-card');
  const text = id => { const e = document.getElementById(id); return e ? e.textContent : null; };
  const disks = {};
  (card ? card.querySelectorAll('[id$="diskused"]') : []).forEach(el => {
    const m = el.id.slice(0, -'diskused'.length);
    const bar = document.getElementById(m + 'diskprogress');
    disks[m] = { used: text(m + 'diskused'), free: text(m + 'diskfree'), percent: text(m + 'diskpercent'),
                 read: text(m + 'diskread'), write: text(m + 'diskwrite'),
                 bar: colours(bar), width: bar ? bar.style.width || '' : null,
                 badge: colours(document.getElementById(m + 'indicator')) };
  });
  const snap = {
    banner: banner && {
      role: banner.getAttribute('role'), live: banner.getAttribute('aria-live'), hidden: banner.hidden,
      lines: banner.childNodes.map(n => n.nodeType === 1
        ? { cls: n.className, text: n.textContent, elements: n.children.length } : { stray: n.textContent }),
    },
    bannerChanges: banner ? changes.filter(n => banner.contains(n)).length : 0,
    bannerAdded: banner ? added.filter(a => a.parent === banner).map(a => [a.cls, a.text]) : [],
    summary: summary && { role: summary.getAttribute('role'), live: summary.getAttribute('aria-live'),
                          cls: summary.className, text: summary.textContent },
    summaryChanges: summary ? changes.filter(n => summary.contains(n)).length : 0,
    disks, liveMark: card ? card.hasAttribute('data-live') : null,
    pendingRequests: pending.length, requests, timers: timers.length,
    watch: object((input.watch || []).map(id => [id, text(id)])),
    attrs: object((input.attrs || []).map(([id, name]) => {
      const e = document.getElementById(id);
      return [id + ' ' + name, e ? e.getAttribute(name) : undefined];
    })),
    sections: object(root.querySelectorAll('details').map(d => [d.getAttribute('data-section'), d.open])),
    sent: sent.slice(), reloads,
    graphs: graphs(),
    storage: stored ? Object.assign({}, stored) : null,
  };
  const top10 = document.getElementById('top10');
  if (top10) {  // the bandwidth card: its tabs, its panes, and its text outside them (a message)
    snap.top10 = {
      tabs: top10.querySelectorAll('.nav-link').map(a => ({ id: a.id, active: a.classList.contains('active'),
                                                             selected: a.getAttribute('aria-selected') })),
      panes: top10.querySelectorAll('.tab-pane').map(p => {
        const entries = p.querySelectorAll('.bw-entry');  // <tbody>: its first row, its first cell (the date)
        return { id: p.id, active: p.classList.contains('active'), scrollTop: p.scrollTop, entries: entries.length,
                 first: entries.length ? entries[0].children[0].children[0].textContent : null };
      }),
      text: top10.textContent.replace(/\s+/g, ' ').trim(),
      tabsShown,
    };
  }
  tabsShown = [];
  changes = [];
  added = [];
  return snap;
}

// What a browser does once the script that changed the page has run: "toggle" for each <details> opened or closed,
// then the MutationObservers, each told once.
function settle() {
  toggled.splice(0).forEach(d => (d.listeners.toggle || []).forEach(f => f({ type: 'toggle', target: d })));
  const changed = [...mutated];
  mutated.clear();
  const told = new Set();
  observers.forEach(({ observer, target }) => {
    if (changed.includes(target) && !told.has(observer)) { told.add(observer); observer.callback([], observer); }
  });
}

mutated.clear();  // the building of the page is no mutation
input.scripts.forEach(path => vm.runInContext(fs.readFileSync(path, 'utf8'), sandbox, { filename: path }));
if (input.poll) { vm.runInContext(input.poll, sandbox, { filename: 'diskusage() of the dashboard' }); }
ready.forEach(f => f.call(document));
(listeners.DOMContentLoaded || []).forEach(f => f());
settle();

function send(event, data) {  // a websocket event, to every handler of the page; an event nobody listens to is lost
  if (!connected) { throw new Error('no ' + event + ' handler: socket.io not connected'); }
  (socketHandlers[event] || []).forEach(f => f(data));
}

function play(step) {
  if ('at' in step) { clock = step.at; }
  if (step.batch) {
    step.batch.forEach(play);
  } else if (step.live) {
    if (!socketHandlers.live) { throw new Error('no live handler: socket.io not connected'); }
    send('live', { disks: step.live });
  } else if (step.event) {
    send(step.event, step.data);
  } else if (step.click || step.clickOn || step.summary) {
    let el;
    if (step.summary) {
      const d = root.querySelectorAll('details').find(x => x.getAttribute('data-section') === step.summary);
      el = d && d.children.find(c => c.tagName === 'SUMMARY');
    } else {
      el = step.click ? document.getElementById(step.click) : root.querySelector(step.clickOn);
    }
    if (!el) { throw new Error('nothing to click: ' + JSON.stringify(step)); }
    if (!el.hasAttribute('disabled')) {
      const event = { type: 'click', target: el, stopPropagation: () => {}, preventDefault: () => {} };
      (el.listeners.click || []).forEach(f => f.call(el, event));
      if (el.tagName === 'SUMMARY') { el.parentNode.open = !el.parentNode.open; }  // what a browser does then
    }
  } else if ('hidden' in step) {
    document.hidden = step.hidden;
    (listeners.visibilitychange || []).forEach(f => f());
  } else if (step.ajax) {
    if (!pending.length) { throw new Error('no request is pending'); }
    pending.shift().success(step.ajax);
  } else if (step.fail) {  // a network error or an HTTP error other than 401: jQuery calls no success()
    if (!pending.length) { throw new Error('no request is pending'); }
    const options = pending.shift();
    if (options.error) { options.error(); }
  } else if (step.timer) {
    timers.splice(0).forEach(f => f());
  } else if (step.tab) {
    showTab(document.getElementById(step.tab));
  } else if (step.scroll) {
    document.getElementById(step.scroll[0]).scrollTop = step.scroll[1];
  }
}

const out = [snapshot()];
input.steps.forEach(step => {
  play(step);
  settle();
  out.push(snapshot());
});
process.stdout.write(JSON.stringify(out));
