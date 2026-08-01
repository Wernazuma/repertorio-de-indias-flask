/* =====================================================================
   ARCA shell — theme switcher (persisted), nested dropdowns, mobile nav.
   The initial theme is applied inline in <head> (see base.html) to avoid a
   flash; this file wires the interactive controls.
   ===================================================================== */
(function () {
  "use strict";
  var THEMES = ["slate", "sepia", "verdigris", "cyanotype", "dark"];
  var STORE_KEY = "arca-theme";
  var docEl = document.documentElement;

  /* ---- theme switcher ---- */
  function applyTheme(name) {
    if (THEMES.indexOf(name) === -1) return;
    docEl.setAttribute("data-theme", name);
    try { localStorage.setItem(STORE_KEY, name); } catch (e) {}
    document.querySelectorAll(".swatch[data-theme]").forEach(function (b) {
      b.setAttribute("aria-pressed", b.dataset.theme === name ? "true" : "false");
    });
  }
  document.querySelectorAll(".swatch[data-theme]").forEach(function (b) {
    b.addEventListener("click", function () { applyTheme(b.dataset.theme); });
  });
  // sync pressed state with whatever theme is currently active
  (function () {
    var current = docEl.getAttribute("data-theme") || "slate";
    document.querySelectorAll(".swatch[data-theme]").forEach(function (b) {
      b.setAttribute("aria-pressed", b.dataset.theme === current ? "true" : "false");
    });
  })();

  /* ---- nested dropdowns (click toggles; hover opens on desktop) ---- */
  var root = document;
  function closeAll(except) {
    root.querySelectorAll(".menu > li.open").forEach(function (o) {
      if (o === except) return;
      o.classList.remove("open");
      var btn = o.querySelector(":scope > button");
      if (btn) btn.setAttribute("aria-expanded", "false");
    });
  }
  root.querySelectorAll(".menu > li.has-sub > button").forEach(function (btn) {
    var li = btn.parentElement;
    btn.addEventListener("click", function (e) {
      e.stopPropagation();
      var willOpen = !li.classList.contains("open");
      closeAll(li);
      li.classList.toggle("open", willOpen);
      btn.setAttribute("aria-expanded", willOpen ? "true" : "false");
    });
  });
  document.addEventListener("click", function () { closeAll(null); });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") closeAll(null);
  });

  var mq = window.matchMedia("(min-width: 761px)");
  root.querySelectorAll(".menu > li.has-sub").forEach(function (li) {
    li.addEventListener("mouseenter", function () { if (mq.matches) li.classList.add("open"); });
    li.addEventListener("mouseleave", function () { if (mq.matches) li.classList.remove("open"); });
  });

  /* ---- mobile hamburger ---- */
  var navInner = document.getElementById("navInner");
  var navToggle = document.getElementById("navToggle");
  if (navInner && navToggle) {
    navToggle.addEventListener("click", function (e) {
      e.stopPropagation();
      var open = navInner.classList.toggle("open");
      navToggle.setAttribute("aria-expanded", open ? "true" : "false");
    });
  }
})();
