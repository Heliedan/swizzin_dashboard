'use strict';
/* A small fake DOM to run static/js/live_stats.js and the /stats/disk poll of the dashboard in Node.js, with no
   npm package. Driven by tests/test_disk_alert_js.py, which sends on stdin, as JSON:
     page       the HTML the panel rendered, as a tree: {tag, attrs, children} or {text}
     live_stats the path of static/js/live_stats.js
     poll       the source of the diskusage() poll found in the page, or null
     socketio   false: socket.io did not load (no "io")
     steps      [{live: disks} | {ajax: data} | {timer: true}]: a live reading, the answer to the oldest pending
                /stats/disk request, or the pending timers firing
   and prints a list of snapshots of the page: after loading, then after each step.
   Inserting HTML (innerHTML...) throws: system data must only ever go in as text. */
const fs = require('fs');
const vm = require('vm');

let changes = [];  // every change made to the page during the current step: the node changed
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
    return node;
  }
  contains(node) {
    for (; node; node = node.parentNode) { if (node === this) { return true; } }
    return false;
  }
  querySelectorAll(selector) { return descendants(this).filter(matcher(selector)); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
}

function adopt(parent, node, index) {
  if (node.parentNode) { node.parentNode.removeChild(node); }
  added.push({ parent, cls: node.nodeType === 1 ? node.className : null, text: node.textContent });
  node.parentNode = parent;
  parent.childNodes.splice(index, 0, node);
  changes.push(parent);
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
  if ((m = /^\.([\w-]+)$/.exec(selector))) { return e => e.classList.contains(m[1]); }
  if ((m = /^\[([\w-]+)\$="([^"]*)"\]$/.exec(selector))) { return e => (e.getAttribute(m[1]) || '').endsWith(m[2]); }
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
const listeners = {}, socketHandlers = {}, pending = [], timers = [];
let requests = 0;

const document = {
  domain: '127.0.0.1',
  getElementById: id => descendants(root).find(e => e.id === id) || null,
  createElement: tag => new Element(tag),
  querySelector: selector => root.querySelector(selector),
  querySelectorAll: selector => root.querySelectorAll(selector),
  addEventListener: (event, f) => { (listeners[event] = listeners[event] || []).push(f); },
};

// jQuery, as much as the poll uses: $('#' + $.escapeSelector(mount) + 'diskfree'), .html(), .css(), .attr(),
// .addClass(), .removeClass(), $.ajax(). The mount is wrapped by escapeSelector so that it is found as written.
function $(selector) {
  const m = /^#\u0001([\s\S]*)\u0002(\w+)(?:\.([\w-]+))?$/.exec(selector);
  if (!m) { throw new Error('selector not supported by the fake jQuery: ' + selector); }
  const el = document.getElementById(m[1] + m[2]);
  const found = el && (!m[3] || el.classList.contains(m[3])) ? [el] : [];
  const api = {
    html(v) { found.forEach(e => { e.textContent = v; }); return api; },  // the poll only writes sizes and numbers
    css(k, v) { found.forEach(e => { e.style[k] = v; }); return api; },
    attr(k, v) { found.forEach(e => e.setAttribute(k, v)); return api; },
    addClass(c) { found.forEach(e => e.classList.add(...c.split(' '))); return api; },
    removeClass(c) { found.forEach(e => e.classList.remove(...c.split(' '))); return api; },
  };
  return api;
}
$.escapeSelector = s => '\u0001' + s + '\u0002';
$.ajax = options => { requests += 1; pending.push(options); };

const sandbox = { document, $, console, window: { location: { protocol: 'http:', port: '8333' } },
                  setTimeout: f => { timers.push(f); } };
if (input.socketio) {
  sandbox.io = { connect: () => ({ on: (event, f) => { socketHandlers[event] = f; }, emit: () => {} }) };
}
vm.createContext(sandbox);

function colours(el) {
  return el ? [...el.classes()].filter(c => c.startsWith('bg-') || c === 'disk-unavailable').sort() : null;
}

function snapshot() {
  const banner = document.getElementById('alerte-disque');
  const summary = document.getElementById('alerte-disque-resume');
  const card = document.getElementById('carte-disque');
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
    disks, liveMark: card ? card.hasAttribute('data-direct') : null,
    pendingRequests: pending.length, requests, timers: timers.length,
  };
  changes = [];
  added = [];
  return snap;
}

vm.runInContext(fs.readFileSync(input.live_stats, 'utf8'), sandbox, { filename: input.live_stats });
if (input.poll) { vm.runInContext(input.poll, sandbox, { filename: 'diskusage() of the dashboard' }); }
(listeners.DOMContentLoaded || []).forEach(f => f());

const out = [snapshot()];
input.steps.forEach(step => {
  if (step.live) {
    if (!socketHandlers.live) { throw new Error('no live handler: socket.io not connected'); }
    socketHandlers.live({ disks: step.live });
  } else if (step.ajax) {
    if (!pending.length) { throw new Error('no /stats/disk request is pending'); }
    pending.shift().success(step.ajax);
  } else if (step.timer) {
    timers.splice(0).forEach(f => f());
  }
  out.push(snapshot());
});
process.stdout.write(JSON.stringify(out));
