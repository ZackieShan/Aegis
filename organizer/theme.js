/* Theme-bridge — makes the organizer match Aegis when embedded.
 *
 * The organizer is served same-origin inside an Aegis iframe, so it can read
 * Aegis's live design tokens straight off the parent document and skin itself
 * to match. Behavior:
 *   - Embedded in normal Aegis  -> inherit Aegis's CSS variables (--bg, --fg,
 *     --panel, --border, --accent, --font-ui, radii) onto our own :root and
 *     add body.theme-aegis, which theme-aegis.css uses to replace the Win98
 *     look with Aegis's flat look. Tracks Aegis light/dark + custom themes.
 *   - Embedded while Aegis is in Win98 mode (root.theme-win98) -> stay Win98,
 *     which already matches the Aegis 98 desktop.
 *   - Standalone (no accessible parent) -> stay Win98.
 * It re-syncs whenever Aegis toggles its theme. Fully guarded: any failure
 * leaves the original Win98 styling untouched. */
(function () {
  "use strict";
  var TOKENS = ["--bg", "--fg", "--panel", "--border", "--accent",
                "--accent-warm", "--font-ui", "--radius-sm", "--radius-md",
                "--radius-lg"];

  function parentRoot() {
    try {
      if (window.parent && window.parent !== window) {
        return window.parent.document.documentElement;  // throws if cross-origin
      }
    } catch (e) { /* cross-origin */ }
    return null;
  }

  function apply() {
    var pr = parentRoot();
    var root = document.documentElement;
    var body = document.body;
    if (!body) return;
    if (!pr || pr.classList.contains("theme-win98")) {
      body.classList.remove("theme-aegis", "aegis-light");
      TOKENS.forEach(function (t) { root.style.removeProperty(t); });
      return;
    }
    try {
      var cs = window.parent.getComputedStyle(pr);
      TOKENS.forEach(function (t) {
        var v = cs.getPropertyValue(t);
        if (v && v.trim()) root.style.setProperty(t, v.trim());
      });
      body.classList.toggle("aegis-light", pr.classList.contains("light"));
      body.classList.add("theme-aegis");
    } catch (e) { /* leave Win98 look on any failure */ }
  }

  function boot() {
    apply();
    var pr = parentRoot();
    if (pr && window.MutationObserver) {
      try {
        new MutationObserver(apply).observe(
          pr, { attributes: true, attributeFilter: ["class"] });
      } catch (e) { /* observing optional */ }
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
