// App Store page: search and filter, then installing an app in a live terminal.
// Nothing is kept in the browser's storage; every text is set with textContent.
(function () {
  "use strict";
  var search = document.getElementById("store-search");
  if (!search) {
    return;  // the helper is not running: the page only shows a message
  }

  // --- search and the All / Installed / Available filter --------------------------------
  var filter = "all";
  var buttons = Array.prototype.slice.call(document.querySelectorAll("[data-filter]"));

  function apply() {
    var query = search.value.trim().toLowerCase();
    var total = 0;
    Array.prototype.forEach.call(document.querySelectorAll(".store-section"), function (section) {
      var shown = 0;
      Array.prototype.forEach.call(section.querySelectorAll(".store-item"), function (item) {
        var installed = item.getAttribute("data-installed") === "yes";
        var available = !installed && item.getAttribute("data-available") !== "no";
        var visible = (!query || item.getAttribute("data-text").indexOf(query) !== -1) &&
          (filter === "all" || (filter === "installed" ? installed : available));
        item.hidden = !visible;
        if (visible) {
          shown++;
        }
      });
      section.querySelector(".store-count").textContent = shown;
      section.hidden = shown === 0;
      total += shown;
    });
    document.getElementById("store-empty").hidden = total !== 0;
  }

  search.addEventListener("input", apply);
  buttons.forEach(function (button) {
    button.addEventListener("click", function () {
      filter = button.getAttribute("data-filter");
      buttons.forEach(function (other) {
        other.classList.toggle("active", other === button);
        other.setAttribute("aria-pressed", other === button ? "true" : "false");
      });
      apply();
    });
  });

  // --- installing ----------------------------------------------------------------------------
  var root = document.querySelector(".store");
  var base = root.getAttribute("data-urlbase") || "";
  var askPassword = root.getAttribute("data-ask-password") !== "no";  // STORE_ASK_PASSWORD in swizzin.cfg
  var $ = window.jQuery;
  var el = function (id) { return document.getElementById(id); };
  var MESSAGES = {
    "password": "Wrong password.",
    "busy": "Another installation is already running.",
    "blocked": "This application can only be installed over SSH.",
    "installed": "This application is already installed.",
    "unknown": "Swizzin does not know this application.",
    "invalid": "Swizzin does not know this application.",
    "not-controller": "Another window controls this installation: use Take control to type in it.",
    "control-lost": "Another window took control of this installation: use Take control to type in it again.",
    "unavailable": "The App Store helper cannot be reached (systemctl status swizzin-store).",
    "helper-down": "Lost the App Store helper; trying again…",
    "unreadable": "The App Store helper could not read Swizzin's files.",
    "input-lost": "What you typed could not be sent; try again."
  };
  var socket = null;
  var term = null;
  var fit = null;
  var job = null;  // the installation shown in the terminal (summary from the helper)
  var controller = false;
  var controlKey = null;  // kept in memory only: lets this tab type again after a reconnection
  var pending = null;  // the app whose installation was just asked for
  var retry = null;  // timer of the next attempt to reach the helper again

  try {
    job = JSON.parse(root.getAttribute("data-job") || "null");
  } catch (e) {
    job = null;
  }

  function running() {
    return job !== null && job.phase !== "done";
  }

  function setInstallButtons() {
    Array.prototype.forEach.call(document.querySelectorAll(".store-install"), function (b) {
      b.disabled = running() || pending !== null;
      b.title = running() ? "Wait for the installation in progress" : "";
    });
  }

  function showBanner() {
    el("store-banner").hidden = !running();
    if (running()) {
      el("store-banner-app").textContent = job.app;
    }
  }

  var opened = false;

  function terminal() {  // what arrives before the window is shown waits in the terminal's buffer
    if (term === null) {
      term = new window.Terminal({
        cursorBlink: true, fontSize: 13, scrollback: 5000,
        fontFamily: "Menlo, Consolas, 'DejaVu Sans Mono', monospace",
        theme: {background: "#1e1e1e"}
      });
      fit = new window.FitAddon.FitAddon();
      term.loadAddon(fit);
      term.onData(function (data) {
        if (controller && socket) {
          socket.emit("input", {data: data});
        }
      });
    }
    return term;
  }

  function resize() {
    if (term === null || !el("store-terminal").classList.contains("show")) {
      return;
    }
    if (!opened) {  // opened only once visible: in a hidden window it cannot measure its characters
      term.open(el("store-term"));
      opened = true;
    }
    fit.fit();
    if (controller && socket) {
      socket.emit("resize", {rows: term.rows, cols: term.cols});
    }
  }

  function status(text, kind) {
    var badge = el("store-terminal-status");
    badge.textContent = text;
    badge.className = "badge ml-3 align-self-center badge-" + (kind || "info");
  }

  function controls() {
    var live = running();
    el("store-reload").hidden = live;
    el("store-keys").hidden = !(live && controller);
    el("store-ctrl-c").hidden = !(live && controller);
    el("store-take").hidden = !(live && !controller);
    if (askPassword && (!live || controller)) {
      el("store-take-form").hidden = true;
    }
    el("store-terminal-note").textContent = !live || controller ? "" : askPassword ?
      "You are watching. Take control (with your password) to answer the installer." :
      "You are watching. Take control to answer the installer.";
  }

  function openTerminal() {
    terminal();
    el("store-terminal-title").textContent = job ? "Installing " + job.app : "Installation";
    controls();
    $("#store-terminal").modal("show");
  }

  function card(app) {
    return document.querySelector('.store-item[data-app="' + app + '"]');
  }

  function result(done) {
    if (done.result === "installed" && done.test === "passed") {
      return ["Installed ✓ — checks passed", "success"];
    }
    if (done.result === "installed") {
      var item = card(done.app);
      if (item && item.getAttribute("data-check") === "unreliable") {  // see UNRELIABLE_CHECK in __init__.py
        return ["Installed — Swizzin's check cannot verify this app: open it to make sure it runs", "info"];
      }
      var later = done.retested ? ", also a minute later" : "";
      return [done.generic_test ? "Installed, but Swizzin's generic check failed" + later + ": open the app to check" :
        "Installed, but its checks failed" + later + ": read the terminal", "warning"];
    }
    if (done.result === "interrupted") {
      return ["Interrupted", "danger"];
    }
    return ["Not installed — read the terminal for the reason", "danger"];
  }

  function showOpen(app) {  // the app's page, built like the sidebar's links (scheme://host + baseurl)
    var item = card(app);
    var path = item && item.getAttribute("data-path");
    var link = el("store-open");
    link.hidden = !path;
    if (path) {
      var scheme = item.getAttribute("data-scheme") || window.location.protocol.replace(":", "");
      link.href = scheme + "://" + window.location.hostname + path;
      link.textContent = "Open " + item.querySelector(".store-name").textContent;
    }
  }

  function markInstalled(app) {
    var item = card(app);
    if (!item) {
      return;
    }
    item.setAttribute("data-installed", "yes");
    item.querySelector(".store-card").classList.add("installed");
    var foot = item.querySelector(".store-foot");
    foot.textContent = "";
    var badge = document.createElement("span");
    badge.className = "badge badge-success";
    badge.textContent = "Installed";
    foot.appendChild(badge);
  }

  function finish(done) {  // the installation ended: its result, and the page back to normal
    job = done;
    controller = false;
    var r = result(job);
    status(r[0], r[1]);
    if (job.result === "installed") {
      markInstalled(job.app);
      showOpen(job.app);
    }
    el("store-reload").hidden = false;
    controls();
    setInstallButtons();
  }

  function reattach() {  // the helper went away: try again until it answers, without stacking attempts
    if (retry === null) {
      retry = setTimeout(function () {
        retry = null;
        if (running() && socket) {
          socket.emit("attach", controlKey ? {control_key: controlKey} : {});
        }
      }, 3000);
    }
  }

  function onHelper(msg) {
    if (msg.control === "lost") {  // another window took control (its key replaced ours)
      controller = false;
      controlKey = null;
      el("store-terminal-error").textContent = MESSAGES["control-lost"];
      controls();
      return;
    }
    if (msg.ok === false) {
      if (msg.error === "no-job") {  // a helper that restarted has no installation: the one shown was stopped
        if (running()) {
          el("store-terminal-error").textContent = "";
          finish({id: job.id, app: job.app, phase: "done", result: "interrupted", test: null});
          showBanner();
        }
        return;
      }
      var text = MESSAGES[msg.error] || ("Error: " + msg.error);
      if (pending !== null) {  // the installation did not start: say it in the confirmation window
        pending = null;
        setInstallButtons();
        el("store-confirm-go").disabled = false;
        el("store-confirm-error").textContent = text;
        return;
      }
      el("store-terminal-error").textContent = text;
      if (msg.error === "not-controller") {  // control went to another window: show it
        controller = false;
        controls();
      }
      if ((msg.error === "helper-down" || msg.error === "unavailable") && running()) {
        reattach();
      }
      return;
    }
    el("store-terminal-error").textContent = "";
    if (msg.started) {
      pending = null;
      job = msg.started;
      controller = true;
      controlKey = msg.control_key;
      terminal().reset();
      el("store-open").hidden = true;
      status("Installing…");
      controls();
      setInstallButtons();
      closeConfirmThen(openTerminal);
    } else if (msg.attached) {
      job = msg.attached;
      controller = msg.controller;
      if (msg.control_key) {
        controlKey = msg.control_key;
      }
      terminal().reset();  // the helper replays the output next
      el("store-open").hidden = true;
      status(job.phase === "test" ? "Checking…" : (job.phase === "done" ? "" : "Installing…"));
      controls();
      resize();
    } else if (typeof msg.output === "string") {
      terminal().write(msg.output);
    } else if (msg.phase === "test") {
      job.phase = "test";
      status("Checking…");
    } else if (msg.phase === "retest") {  // the first checks failed: the helper checks again a bit later
      status("Checking again in " + msg.seconds + " s…");
    } else if (msg.done) {
      finish(msg.done);
    }
    showBanner();
  }

  function connect() {
    if (socket !== null) {
      return socket;
    }
    socket = window.io.connect(window.location.protocol + "//" + document.domain + ":" + window.location.port + "/store", {
      path: base + "/socket.io",
      auth: function (cb) {  // asked again at every (re)connection: the panel may have restarted
        fetch(base + "/store/token", {credentials: "same-origin", cache: "no-store"})
          .then(function (r) { return r.ok ? r.json() : {}; })
          .then(function (d) { cb({token: d.token || ""}); }, function () { cb({token: ""}); });
      }
    });
    socket.on("connect", function () {
      if (running()) {
        socket.emit("attach", controlKey ? {control_key: controlKey} : {});
      }
    });
    socket.on("helper", onHelper);
    return socket;
  }

  Array.prototype.forEach.call(document.querySelectorAll(".store-install"), function (b) {
    b.addEventListener("click", function () {
      el("store-confirm-app").textContent = b.getAttribute("data-pretty");
      el("store-confirm-command").textContent = "box install " + b.getAttribute("data-app");
      var note = card(b.getAttribute("data-app")).getAttribute("data-note");  // see NOTES in __init__.py
      el("store-confirm-note").textContent = note || "";
      el("store-confirm-note").hidden = !note;
      el("store-confirm-form").setAttribute("data-app", b.getAttribute("data-app"));
      el("store-confirm-error").textContent = "";
      if (askPassword) {
        el("store-confirm-password").value = "";
      }
      el("store-confirm-go").disabled = false;
      $("#store-confirm").modal("show");
    });
  });
  var confirmOpen = false;  // the confirmation window has finished opening (Bootstrap ignores hide() before that)
  $("#store-confirm").on("shown.bs.modal", function () {
    confirmOpen = true;
    el(askPassword ? "store-confirm-password" : "store-confirm-go").focus();
  });
  $("#store-confirm").on("hide.bs.modal", function () { confirmOpen = false; });

  function closeConfirmThen(next) {  // Bootstrap shows one window at a time: next() once the confirmation is closed
    if (!el("store-confirm").classList.contains("show")) {
      next();
      return;
    }
    $("#store-confirm").one("hidden.bs.modal", next);
    if (confirmOpen) {
      $("#store-confirm").modal("hide");
    } else {  // still opening: close it as soon as it is open
      $("#store-confirm").one("shown.bs.modal", function () { $("#store-confirm").modal("hide"); });
    }
  }

  el("store-confirm-form").addEventListener("submit", function (event) {
    event.preventDefault();
    pending = el("store-confirm-form").getAttribute("data-app");
    el("store-confirm-go").disabled = true;
    el("store-confirm-error").textContent = "";
    setInstallButtons();
    var request = {app: pending, rows: 30, cols: 100};
    if (askPassword) {
      request.password = el("store-confirm-password").value;
      el("store-confirm-password").value = "";
    }
    connect().emit("install", request);
  });

  el("store-banner-show").addEventListener("click", function () {
    openTerminal();
    var s = connect();
    if (s.connected) {
      s.emit("attach", controlKey ? {control_key: controlKey} : {});
    }  // otherwise the "connect" handler attaches
  });

  el("store-ctrl-c").addEventListener("click", function () {
    if (controller && socket) {
      socket.emit("input", {data: "\u0003"});
    }
    term.focus();
  });

  // keys a tablet keyboard lacks: whiptail menus want arrows, Tab and Esc
  var KEYS = {up: "A", down: "B", right: "C", left: "D"};
  Array.prototype.forEach.call(document.querySelectorAll("#store-keys [data-key]"), function (b) {
    b.addEventListener("click", function () {
      var k = b.getAttribute("data-key");
      var seq = KEYS[k] ? (term.modes.applicationCursorKeysMode ? "\u001bO" : "\u001b[") + KEYS[k] :
        {tab: "\t", enter: "\r", esc: "\u001b"}[k];
      if (controller && socket) {
        socket.emit("input", {data: seq});
      }
      term.focus();
    });
  });

  el("store-take").addEventListener("click", function () {
    if (!askPassword) {
      connect().emit("attach", {take: true});
      return;
    }
    el("store-take").hidden = true;
    el("store-take-form").hidden = false;
    el("store-take-password").focus();
  });

  if (askPassword) {
    el("store-take-form").addEventListener("submit", function (event) {
      event.preventDefault();
      connect().emit("attach", {take: true, password: el("store-take-password").value});
      el("store-take-password").value = "";
    });
  }

  el("store-close").addEventListener("click", function () {
    $("#store-terminal").modal("hide");
    showBanner();
  });
  el("store-reload").addEventListener("click", function () { window.location.reload(); });
  $("#store-terminal").on("shown.bs.modal", function () {
    resize();
    term.focus();
  });
  window.addEventListener("resize", resize);

  setInstallButtons();
  showBanner();
  if (running()) {  // an installation runs: follow it, so that the page learns when it ends
    connect();
  }
})();
