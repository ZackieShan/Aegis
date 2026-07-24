/* Single-app mode — lets Aegis mount ONE organizer domain per window.
 *
 * Loaded LAST (after wm.js + the app modules). When the URL carries
 * ?app=photos|cinema|music, we hide the Win98 desktop chrome (icon grid,
 * taskbar, Start menu), force every other app-window hidden, and open the
 * target window so it fills the frame (styled by body.single-app in
 * style.css). With no ?app= param this is a no-op, so the standalone app
 * keeps its full Win98 desktop. Aegis mounts three separate tools this way
 * (Photos / Movies & TV / Music), each in its own draggable Aegis window. */
(function () {
  "use strict";
  var app = null;
  try {
    app = new URLSearchParams(window.location.search).get("app");
  } catch (e) { return; }
  if (!app) return;

  var MAP = { photos: "winWizard", cinema: "winCinema", music: "winMusic" };
  var target = MAP[app];
  if (!target) return;

  var ALL = ["winWizard", "winExplore", "winCinema", "winMusic", "winAbout"];

  function boot() {
    document.body.classList.add("single-app", "app-" + app);
    ALL.forEach(function (id) {
      var el = document.getElementById(id);
      if (!el) return;
      if (id === target) el.classList.remove("hidden");
      else el.classList.add("hidden");
    });
    try {
      if (window.WM && typeof WM.open === "function") WM.open(target);
    } catch (e) { /* WM optional */ }
    // The wm:open event is the lazy-load hook every app module relies on
    // (config load, re-attach to a running scan). WM.open() can no-op in
    // single-app mode (taskbar/desktop chrome is hidden), which silently
    // skipped that hook — fire it directly; handlers are idempotent.
    try {
      var el = document.getElementById(target);
      if (el) el.dispatchEvent(new CustomEvent("wm:open"));
    } catch (e) { /* hook optional */ }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
