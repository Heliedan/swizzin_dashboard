// App Store page: search and filter, then installing or uninstalling an app in a live terminal.
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

  // --- installing and uninstalling -------------------------------------------------------
  var root = document.querySelector(".store");
  var base = root.getAttribute("data-urlbase") || "";
  var askPassword = root.getAttribute("data-ask-password") !== "no";  // STORE_ASK_PASSWORD in swizzin.cfg
  var $ = window.jQuery;
  var el = function (id) { return document.getElementById(id); };
  var MESSAGES = {
    "password": "Wrong password.",
    "busy": "Another installation or uninstallation is already running.",
    "blocked": "This application can only be installed over SSH.",
    "installed": "This application is already installed.",
    "unknown": "Swizzin does not know this application.",
    "invalid": "Swizzin does not know this application.",
    "not-controller": "Another window controls this installation: use Take control to type in it.",
    "control-lost": "Another window took control of this installation: use Take control to type in it again.",
    "unavailable": "The App Store helper cannot be reached (systemctl status swizzin-store).",
    "helper-down": "Lost the App Store helper; trying again…",
    "unreadable": "The App Store helper could not read Swizzin's files.",
    "input-lost": "What you typed could not be sent; try again.",
    "not-removable": "The App Store does not uninstall this application.",
    "not-installed": "This application is not installed.",
    "script-changed": "An update of Swizzin changed this app's removal since the App Store checked it: uninstall it " +
      "over SSH, or update the App Store.",
    "disabled": "Uninstalling from the App Store is turned off on this server.",
    "bad-ticket": "This confirmation had expired: it was renewed, confirm again.",
    "confirm-mismatch": "Type the application's name exactly as shown."
  };
  // the uninstaller's steps (store-helper/remove_runner.py), as the helper passes them on
  var STEPS = ["check", "stop", "copy", "remove", "fix", "verify"];
  var STEP_TEXT = {check: "Checking…", stop: "Stopping…", copy: "Saving a copy of its settings…",
    remove: "Removing…", fix: "Checking the result…", verify: "Checking the result…"};
  var STOPPABLE = ["check", "stop", "copy"];  // before Swizzin's remove script: Stop puts everything back
  var socket = null;
  var term = null;
  var fit = null;
  var job = null;  // the installation or uninstallation shown in the terminal (summary from the helper)
  var controller = false;
  var controlKey = null;  // kept in memory only: lets this tab type again after a reconnection
  var pending = null;  // {action, app}: asked for, not started yet
  var removal = null;  // the uninstallation being confirmed: {app, pretty, ticket}
  var retryTimer = null;  // the next attempt to reach the helper again

  try {
    job = JSON.parse(root.getAttribute("data-job") || "null");
  } catch (e) {
    job = null;
  }

  function running() {
    return job !== null && job.phase !== "done";
  }

  function removing() {
    return job !== null && job.action === "remove";
  }

  function setButtons() {
    Array.prototype.forEach.call(document.querySelectorAll(".store-install, .store-remove"), function (b) {
      b.disabled = running() || pending !== null;
      b.title = running() ? "Wait for the job in progress" : "";
    });
  }

  function showBanner() {
    el("store-banner").hidden = !running();
    if (running()) {
      el("store-banner-action").textContent = removing() ? "Uninstallation" : "Installation";
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
        if (controller && socket && !removing()) {  // nobody types into an uninstallation
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

  function steps() {  // where an uninstallation stands
    var list = el("store-steps");
    list.hidden = !removing();
    if (!removing()) {
      return;
    }
    if (job.phase === "done") {  // every step done, or left where it stopped
      if (job.result === "removed") {
        Array.prototype.forEach.call(list.querySelectorAll("[data-step]"), function (li) {
          li.className = "list-inline-item done";
        });
      }
      return;
    }
    var at = STEPS.indexOf(job.phase === "fix" ? "verify" : job.phase);
    Array.prototype.forEach.call(list.querySelectorAll("[data-step]"), function (li) {
      var i = STEPS.indexOf(li.getAttribute("data-step"));
      li.className = "list-inline-item" + (i < at ? " done" : (i === at ? " current" : ""));
    });
  }

  function controls() {
    var live = running();
    var rm = removing();
    el("store-reload").hidden = live;
    el("store-keys").hidden = !(live && controller && !rm);
    el("store-ctrl-c").hidden = !(live && controller && !rm);
    el("store-stop").hidden = !(live && controller && rm && STOPPABLE.indexOf(job.phase) !== -1);
    el("store-take").hidden = !(live && !controller);
    if (askPassword && (!live || controller)) {
      el("store-take-form").hidden = true;
    }
    var note = "";
    if (live && !controller) {
      note = rm ? "You are watching this uninstallation." : askPassword ?
        "You are watching. Take control (with your password) to answer the installer." :
        "You are watching. Take control to answer the installer.";
    } else if (live && rm) {
      note = STOPPABLE.indexOf(job.phase) !== -1 ?
        "Nothing is removed yet: Stop puts everything back as it was." :
        "Swizzin's remove script is running: it cannot be stopped. Closing this window does not stop it.";
    }
    el("store-terminal-note").textContent = note;
  }

  function openTerminal() {
    terminal();
    var what = removing() ? "Uninstalling " : "Installing ";
    var item = job ? card(job.app) : null;
    el("store-terminal-title").textContent = job ? what + (item ? item.querySelector(".store-name").textContent : job.app) :
      "Installation";
    el("store-remove-result").hidden = true;
    el("store-term").classList.remove("with-result");
    el("store-retry").hidden = true;
    steps();
    controls();
    $("#store-terminal").modal("show");
  }

  function card(app) {
    return document.querySelector('.store-item[data-app="' + app + '"]');
  }

  function result(done) {
    if (done.action === "remove") {
      if (done.result === "removed") {
        return ["Uninstalled ✓ — every check passed", "success"];
      }
      if (done.result === "incomplete") {
        return ["Partly uninstalled — see what is left below", "warning"];
      }
      if (done.result === "interrupted") {
        return ["Interrupted", "danger"];
      }
      return ["Not uninstalled — nothing was removed", "secondary"];
    }
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

  function setFoot(app, installed, text, kind) {
    var item = card(app);
    if (!item) {
      return;
    }
    item.setAttribute("data-installed", installed ? "yes" : "no");
    item.querySelector(".store-card").classList.toggle("installed", installed);
    var foot = item.querySelector(".store-foot");
    foot.textContent = "";
    var badge = document.createElement("span");
    badge.className = "badge badge-" + kind;
    badge.textContent = text;
    foot.appendChild(badge);
  }

  function showRemovalResult(done) {  // each check, the copy, and what to do next
    var list = el("store-remove-checks");
    list.textContent = "";
    (done.checks || []).forEach(function (c) {
      var li = document.createElement("li");
      li.className = c.ok ? "text-success" : "text-danger";
      li.textContent = (c.ok ? "✓ " : "✗ ") + (c.label || c.id) + (c.detail ? ": " + c.detail : "");
      list.appendChild(li);
    });
    el("store-remove-message").textContent = done.result === "failed" && done.message ?
      "Not uninstalled: " + done.message + ". Nothing was removed." :
      (done.result === "incomplete" ? "Some of it is still there: Try again finishes it (the settings copy is kept)." : "");
    el("store-remove-archive").textContent = done.archive || "";
    el("store-remove-archive-line").hidden = !done.archive;
    el("store-remove-result").hidden = false;
    el("store-term").classList.add("with-result");  // a smaller terminal: the checks and the buttons stay in sight
    resize();
    var again = el("store-retry");
    again.hidden = done.result !== "incomplete";
    again.setAttribute("data-app", done.app);
    again.setAttribute("data-pretty", done.app);
  }

  function finish(done) {  // the job ended: its result, and the page back to normal
    job = done;
    controller = false;
    var r = result(job);
    status(r[0], r[1]);
    if (job.action === "remove") {
      steps();
      showRemovalResult(job);
      if (job.result === "removed") {
        setFoot(job.app, false, "Uninstalled", "secondary");
      }
    } else if (job.result === "installed") {
      setFoot(job.app, true, "Installed", "success");
      showOpen(job.app);
    }
    el("store-reload").hidden = false;
    controls();
    setButtons();
  }

  function reattach() {  // the helper went away: try again until it answers, without stacking attempts
    if (retryTimer === null) {
      retryTimer = setTimeout(function () {
        retryTimer = null;
        if (running() && socket) {
          socket.emit("attach", controlKey ? {control_key: controlKey} : {});
        }
      }, 3000);
    }
  }

  function human(bytes) {
    var units = ["bytes", "KB", "MB", "GB"];
    var i = 0;
    while (bytes >= 1024 && i < units.length - 1) {
      bytes /= 1024;
      i++;
    }
    return (i === 0 ? bytes : bytes.toFixed(1)) + " " + units[i];
  }

  function fillPreview(p) {  // what uninstalling involves, as the helper sees it now
    var item = card(p.app);
    var attr = function (name) { return item ? item.getAttribute(name) || "" : ""; };
    removal.ticket = p.ticket;
    el("store-remove-removed").textContent = attr("data-removed") || "what is left of " + removal.pretty + ".";
    el("store-remove-kept").textContent = attr("data-kept");
    el("store-remove-users").textContent = (p.users || []).join(", ");
    el("store-remove-users-line").hidden = !(p.users && p.users.length > 1);
    el("store-remove-size").textContent = human(p.estimate || 0);
    el("store-remove-archives").textContent = p.archives || "";
    el("store-remove-copy-line").hidden = !(p.estimate > 0);
    el("store-remove-nocopy-line").hidden = p.estimate > 0;
    el("store-remove-partial").hidden = !p.partial;
    el("store-remove-affects").textContent = attr("data-affects");
    el("store-remove-affects-line").hidden = !attr("data-affects");
    el("store-remove-retry-line").hidden = !p.retry;
    el("store-remove-loading").hidden = true;
    el("store-remove-details").hidden = false;
    checkRemoveForm();
  }

  function checkRemoveForm() {
    el("store-remove-go").disabled = !(removal && removal.ticket && pending === null &&
      el("store-remove-confirm").value.trim().toLowerCase() === removal.app && el("store-remove-password").value !== "");
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
      if (msg.error === "no-job") {  // a helper that restarted has no job: the one shown was stopped
        if (running()) {
          el("store-terminal-error").textContent = "";
          finish({id: job.id, app: job.app, action: job.action, phase: "done", result: "interrupted", test: null});
          showBanner();
        }
        return;
      }
      var text = msg.error === "refused" ? "Not possible now: " + msg.message + "." :
        (MESSAGES[msg.error] || ("Error: " + msg.error));
      if (pending !== null && pending.action === "install") {  // it did not start: say it in the confirmation
        pending = null;
        setButtons();
        el("store-confirm-go").disabled = false;
        el("store-confirm-error").textContent = text;
        return;
      }
      if (removal !== null && (pending !== null || removal.ticket === null)) {  // the uninstallation did not start
        var asked = pending !== null;
        pending = null;
        setButtons();
        el("store-remove-loading").hidden = true;
        el("store-remove-error").textContent = text;
        if (asked && ["password", "confirm-mismatch", "bad-ticket", "busy"].indexOf(msg.error) !== -1) {
          removal.ticket = null;  // a fresh confirmation: the helper may have used the ticket
          el("store-remove-go").disabled = true;
          connect().emit("remove-preview", {app: removal.app});
        }
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
    if (msg.preview) {
      if (removal !== null && removal.app === msg.preview.app) {
        msg.preview.ticket = msg.ticket;
        fillPreview(msg.preview);
      }
    } else if (msg.started) {
      var modal = pending !== null && pending.action === "remove" ? "store-remove-modal" : "store-confirm";
      pending = null;
      removal = null;
      job = msg.started;
      controller = true;
      controlKey = msg.control_key;
      terminal().reset();
      el("store-open").hidden = true;
      status(removing() ? STEP_TEXT.check : "Installing…");
      controls();
      setButtons();
      closeModalThen(modal, openTerminal);
    } else if (msg.attached) {
      job = msg.attached;
      controller = msg.controller;
      if (msg.control_key) {
        controlKey = msg.control_key;
      }
      terminal().reset();  // the helper replays the output next
      el("store-open").hidden = true;
      if (removing()) {
        status(job.phase === "done" ? "" : STEP_TEXT[job.phase] || "Uninstalling…");
        steps();
      } else {
        status(job.phase === "test" ? "Checking…" : (job.phase === "done" ? "" : "Installing…"));
      }
      controls();
      resize();
    } else if (typeof msg.output === "string") {
      terminal().write(msg.output);
    } else if (msg.phase && removing() && STEP_TEXT[msg.phase]) {
      job.phase = msg.phase;
      status(STEP_TEXT[msg.phase]);
      steps();
      controls();
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

  // Bootstrap shows one window at a time, and ignores hide() while a window is still opening
  var modalOpen = {"store-confirm": false, "store-remove-modal": false, "store-terminal": false};
  Object.keys(modalOpen).forEach(function (id) {
    $("#" + id).on("shown.bs.modal", function () { modalOpen[id] = true; });
    $("#" + id).on("hide.bs.modal", function () { modalOpen[id] = false; });
  });

  function closeModalThen(id, next) {  // next() once that window is closed
    if (!el(id).classList.contains("show")) {
      next();
      return;
    }
    $("#" + id).one("hidden.bs.modal", next);
    if (modalOpen[id]) {
      $("#" + id).modal("hide");
    } else {  // still opening: close it as soon as it is open
      $("#" + id).one("shown.bs.modal", function () { $("#" + id).modal("hide"); });
    }
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
  $("#store-confirm").on("shown.bs.modal", function () {
    el(askPassword ? "store-confirm-password" : "store-confirm-go").focus();
  });

  el("store-confirm-form").addEventListener("submit", function (event) {
    event.preventDefault();
    pending = {action: "install", app: el("store-confirm-form").getAttribute("data-app")};
    el("store-confirm-go").disabled = true;
    el("store-confirm-error").textContent = "";
    setButtons();
    var request = {app: pending.app, rows: 30, cols: 100};
    if (askPassword) {
      request.password = el("store-confirm-password").value;
      el("store-confirm-password").value = "";
    }
    connect().emit("install", request);
  });

  // uninstalling: the buttons on the cards, the banner's and the terminal's Try again
  function openRemove(app, pretty) {
    removal = {app: app, pretty: pretty, ticket: null};
    el("store-remove-app").textContent = pretty;
    el("store-remove-name").textContent = app;
    el("store-remove-loading").hidden = false;
    el("store-remove-details").hidden = true;
    el("store-remove-error").textContent = "";
    el("store-remove-confirm").value = "";
    el("store-remove-password").value = "";
    el("store-remove-go").disabled = true;
    closeModalThen("store-terminal", function () {
      $("#store-remove-modal").modal("show");
      connect().emit("remove-preview", {app: app});
    });
  }

  document.addEventListener("click", function (event) {
    var b = event.target.closest ? event.target.closest(".store-remove") : null;
    if (b && !b.disabled) {
      openRemove(b.getAttribute("data-app"), b.getAttribute("data-pretty") || b.getAttribute("data-app"));
    }
  });
  $("#store-remove-modal").on("shown.bs.modal", function () { el("store-remove-cancel").focus(); });
  $("#store-remove-modal").on("hidden.bs.modal", function () {
    if (pending === null) {
      removal = null;  // a ticket that will not be used
    }
  });
  el("store-remove-confirm").addEventListener("input", checkRemoveForm);
  el("store-remove-password").addEventListener("input", checkRemoveForm);

  el("store-remove-form").addEventListener("submit", function (event) {
    event.preventDefault();
    checkRemoveForm();
    if (el("store-remove-go").disabled) {
      return;
    }
    if (!socket || !socket.connected) {  // never queued for later: socket.io would send it on reconnection
      el("store-remove-error").textContent = "Not connected to the panel right now: try again in a moment.";
      return;
    }
    pending = {action: "remove", app: removal.app};
    el("store-remove-go").disabled = true;
    el("store-remove-error").textContent = "";
    setButtons();
    socket.emit("remove", {app: removal.app, ticket: removal.ticket, confirm: el("store-remove-confirm").value,
      password: el("store-remove-password").value, rows: 30, cols: 100});
    el("store-remove-password").value = "";
  });

  el("store-stop").addEventListener("click", function () {  // the uninstaller puts everything back
    if (controller && socket && removing() && STOPPABLE.indexOf(job.phase) !== -1) {
      socket.emit("input", {data: "\u0003"});
      el("store-terminal-note").textContent = "Stopping: everything is being put back as it was…";
    }
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

  setButtons();
  showBanner();
  if (running()) {  // a job runs: follow it, so that the page learns when it ends
    connect();
  }
})();
