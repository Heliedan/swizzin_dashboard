// App Store page: search and the All / Installed / Available filter. Text only, nothing is sent.
(function () {
  var search = document.getElementById("store-search");
  if (!search) {
    return;  // the helper is not running: the page only shows a message
  }
  var filter = "all";
  var buttons = Array.prototype.slice.call(document.querySelectorAll("[data-filter]"));

  function apply() {
    var query = search.value.trim().toLowerCase();
    var total = 0;
    Array.prototype.forEach.call(document.querySelectorAll(".store-section"), function (section) {
      var shown = 0;
      Array.prototype.forEach.call(section.querySelectorAll(".store-item"), function (item) {
        var installed = item.getAttribute("data-installed") === "yes";
        var visible = (!query || item.getAttribute("data-text").indexOf(query) !== -1) &&
          (filter === "all" || (filter === "installed") === installed);
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
})();
