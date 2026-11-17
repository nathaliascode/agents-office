// Agents office: theme, language, view menu, modals, elapsed times and live updates.
(function () {
  "use strict";
  var settings = window.OFFICE || { language: "es", time: {} };
  var TEXT = settings.time;
  var root = document.documentElement;
  var isServed = location.protocol.indexOf("http") === 0;

  function stored(key, value) {
    try {
      if (value === undefined) return localStorage.getItem(key);
      localStorage.setItem(key, value);
    } catch (error) { /* private mode or blocked storage */ }
    return null;
  }

  // ---------------------------------------------------------------- theme
  var THEME_KEY = "office-theme";
  if (stored(THEME_KEY)) root.setAttribute("data-theme", stored(THEME_KEY));
  document.getElementById("theme-toggle").addEventListener("click", function () {
    var dark = root.getAttribute("data-theme")
      ? root.getAttribute("data-theme") === "dark"
      : matchMedia("(prefers-color-scheme: dark)").matches;
    var next = dark ? "light" : "dark";
    root.setAttribute("data-theme", next);
    stored(THEME_KEY, next);
  });

  // ------------------------------------------------------------- language
  var LANGUAGE_KEY = "office-language";
  function languageUrl(language, file) {
    if (isServed) return "?lang=" + language;
    return file || null;
  }
  function bindLanguage() {
    document.querySelectorAll(".language-option").forEach(function (button) {
      button.addEventListener("click", function () {
        var language = button.getAttribute("data-language");
        stored(LANGUAGE_KEY, language);
        var target = languageUrl(language, button.getAttribute("data-file"));
        if (target && language !== settings.language) location.href = target;
      });
    });
  }
  bindLanguage();
  if (isServed && stored(LANGUAGE_KEY) && stored(LANGUAGE_KEY) !== settings.language
      && location.search.indexOf("lang=") < 0) {
    location.replace("?lang=" + stored(LANGUAGE_KEY));
  }

  // --------------------------------------------------------------- modals
  function openModal(element) {
    var modal = document.getElementById(element.getAttribute("data-modal"));
    if (!modal) return;
    if (modal.showModal) modal.showModal(); else modal.setAttribute("open", "");
  }
  function bindModals() {
    document.querySelectorAll("[data-modal]").forEach(function (element) {
      element.addEventListener("click", function () { openModal(element); });
      element.addEventListener("keydown", function (event) {
        if (event.key === "Enter" || event.key === " ") { event.preventDefault(); openModal(element); }
      });
    });
    document.querySelectorAll("dialog").forEach(function (modal) {
      var close = modal.querySelector(".close");
      if (close) close.addEventListener("click", function () { modal.close(); });
      modal.addEventListener("click", function (event) { if (event.target === modal) modal.close(); });
    });
  }
  bindModals();

  // ------------------------------------------------------------ view menu
  var VIEW_KEY = "office-view";
  var currentView = "board";
  function showView(id) {
    var found = false;
    document.querySelectorAll(".view").forEach(function (view) {
      var match = view.getAttribute("data-view") === id;
      view.hidden = !match;
      found = found || match;
    });
    if (!found) { id = "board"; document.querySelector('[data-view="board"]').hidden = false; }
    currentView = id;
    document.querySelectorAll(".view-option").forEach(function (option) {
      var match = option.getAttribute("data-view") === id;
      option.classList.toggle("active", match);
      option.setAttribute("aria-selected", match ? "true" : "false");
      if (match) document.getElementById("view-menu-current").textContent = option.getAttribute("data-name");
    });
  }
  function closeMenu() {
    var list = document.getElementById("view-menu-list");
    var button = document.getElementById("view-menu-button");
    if (list) list.hidden = true;
    if (button) button.setAttribute("aria-expanded", "false");
  }
  function bindMenu() {
    var button = document.getElementById("view-menu-button");
    var list = document.getElementById("view-menu-list");
    button.addEventListener("click", function (event) {
      event.stopPropagation();
      var opening = list.hidden;
      list.hidden = !opening;
      button.setAttribute("aria-expanded", opening ? "true" : "false");
      if (opening) { var active = list.querySelector(".active"); if (active) active.focus(); }
    });
    document.querySelectorAll(".view-option").forEach(function (option) {
      option.addEventListener("click", function () {
        var id = option.getAttribute("data-view");
        showView(id);
        closeMenu();
        button.focus();
        stored(VIEW_KEY, id);
      });
    });
  }
  document.addEventListener("click", function (event) { if (!event.target.closest(".view-menu")) closeMenu(); });
  document.addEventListener("keydown", function (event) { if (event.key === "Escape") closeMenu(); });
  bindMenu();
  showView(document.querySelector(".view-menu").getAttribute("data-start") || "board");
  if (stored(VIEW_KEY)) showView(stored(VIEW_KEY));

  // -------------------------------------------------------- elapsed times
  function duration(minutes) {
    function text(key, value) { return TEXT[key].replace("{n}", value); }
    if (minutes < 1) return TEXT.under_a_minute;
    var days = Math.floor(minutes / 1440), hours = Math.floor(minutes % 1440 / 60), mins = Math.floor(minutes % 60);
    if (days) return text("days", days) + (hours ? " " + text("hours", hours) : "");
    if (hours) return text("hours", hours) + (mins ? " " + text("minutes", mins) : "");
    return text("minutes", mins);
  }
  function paintTimes() {
    var now = Date.now();
    document.querySelectorAll(".elapsed[data-since]").forEach(function (element) {
      var since = element.getAttribute("data-since");
      if (!since) { element.textContent = element.getAttribute("data-empty") || TEXT.no_records; return; }
      var moment = new Date(since).getTime();
      if (isNaN(moment)) return;
      var template = element.getAttribute("data-template") || TEXT[element.getAttribute("data-status")] || TEXT.other;
      element.textContent = template.replace("{d}", duration((now - moment) / 60000));
      element.title = since.replace("T", " ");
    });
  }
  paintTimes();
  setInterval(paintTimes, 60000);

  // --------------------------------------------------------------- usage
  document.addEventListener("click", function (event) {
    var meters = event.target.closest(".usage");
    if (!meters || !isServed) return;
    meters.classList.add("loading");
    fetch("/usage", { cache: "no-store" }).catch(function () {});
  });

  // ------------------------------------- walking between desk and table
  var reducedMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;
  function walk(figure, dx, dy) {
    var agent = figure.closest(".agent"), svg = figure.ownerSVGElement, marker = document.createComment("");
    var name = svg.querySelector('.seat-name[data-figure="' + figure.getAttribute("data-figure") + '"]');
    figure.parentNode.insertBefore(marker, figure);
    svg.appendChild(figure);
    figure.classList.add("in-transit");
    if (agent) agent.classList.add("walking");
    if (name) name.classList.add("hidden-name");
    var steps = [];
    for (var i = 0; i <= 10; i++) {
      var t = i / 10, hop = (i % 2 && i < 10) ? -7 : 0;
      steps.push({ transform: "translate(" + (dx * (1 - t)).toFixed(1) + "px," + (dy * (1 - t) + hop).toFixed(1) + "px)" });
    }
    figure.animate(steps, { duration: 1600, easing: "linear" }).onfinish = function () {
      marker.replaceWith(figure);
      figure.classList.remove("in-transit");
      if (name) name.classList.remove("hidden-name");
      if (agent) {
        agent.classList.remove("walking");
        agent.classList.add("arriving");
        setTimeout(function () { agent.classList.remove("arriving"); }, 1200);
      }
    };
  }
  function animateChanges(before, rowsBefore) {
    var view = document.querySelector(".view:not([hidden])");
    if (!view) return;
    view.querySelectorAll(".agent-row").forEach(function (row) {
      var key = row.getAttribute("data-modal");
      if (rowsBefore[key] !== undefined && rowsBefore[key] !== row.className) {
        row.classList.add("changed");
        setTimeout(function () { row.classList.remove("changed"); }, 2600);
      }
    });
    if (reducedMotion) return;
    view.querySelectorAll(".figure").forEach(function (figure) {
      var from = before[figure.getAttribute("data-figure")], to;
      if (!from) return;
      try { to = figure.getBBox(); } catch (error) { return; }
      var dx = from.x - to.x, dy = from.y - to.y;
      if (Math.abs(dx) + Math.abs(dy) >= 20) walk(figure, dx, dy);
    });
  }

  // ---------------------------------------------------------- live mode
  // When served by --watch, ask for changes and replace only the content.
  if (!isServed || !document.body.getAttribute("data-version")) return;
  var version = document.body.getAttribute("data-version");
  var interval = (+document.body.getAttribute("data-interval") || 10) * 1000;
  var busy = false;
  function snapshot() {
    var figures = {}, rows = {}, view = document.querySelector(".view:not([hidden])");
    if (view) {
      view.querySelectorAll(".figure").forEach(function (figure) {
        try { figures[figure.getAttribute("data-figure")] = figure.getBBox(); } catch (error) { /* hidden */ }
      });
      view.querySelectorAll(".agent-row").forEach(function (row) { rows[row.getAttribute("data-modal")] = row.className; });
    }
    return { figures: figures, rows: rows };
  }
  function apply(html, newVersion) {
    var page = new DOMParser().parseFromString(html, "text/html");
    if (page.body.getAttribute("data-code") !== document.body.getAttribute("data-code")) { location.reload(); return; }
    var before = snapshot();
    var openModalElement = document.querySelector("dialog[open]"), openId = openModalElement && openModalElement.id;
    if (openModalElement) openModalElement.close();
    var menuOpen = !document.getElementById("view-menu-list").hidden;
    document.querySelector("style").textContent = page.querySelector("style").textContent;
    document.querySelector("svg.defs").replaceWith(page.querySelector("svg.defs"));
    var usage = document.querySelector(".usage"), newUsage = page.querySelector(".usage");
    if (usage && newUsage) usage.replaceWith(newUsage);
    else if (newUsage) document.querySelector(".topbar h1").after(newUsage);
    document.querySelector(".view-menu").replaceWith(page.querySelector(".view-menu"));
    bindMenu();
    if (menuOpen) {
      document.getElementById("view-menu-list").hidden = false;
      document.getElementById("view-menu-button").setAttribute("aria-expanded", "true");
    }
    document.querySelector("main.main").replaceWith(page.querySelector("main.main"));
    version = newVersion;
    bindModals();
    showView(currentView);
    paintTimes();
    animateChanges(before.figures, before.rows);
    if (openId) { var again = document.getElementById(openId); if (again && again.showModal) again.showModal(); }
  }
  setInterval(function () {
    if (busy) return;
    busy = true;
    fetch("/version", { cache: "no-store" })
      .then(function (response) { return response.text(); })
      .then(function (newVersion) {
        newVersion = newVersion.trim();
        if (!newVersion || newVersion === version) return;
        return fetch("/?lang=" + settings.language, { cache: "no-store" })
          .then(function (response) { return response.text(); })
          .then(function (html) { apply(html, newVersion); });
      })
      .catch(function () {})
      .then(function () { busy = false; });
  }, interval);
})();
