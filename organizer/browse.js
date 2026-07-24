/* browse.js — Win98 modal folder picker for the media-organizer desktop.
 *
 * ============================ PUBLIC CONTRACT ============================
 *
 *   window.BrowseDialog.pick(currentPath) -> Promise<string|null>
 *
 *     currentPath : optional starting folder, e.g. "D:\\Movies". Missing or
 *                   invalid values open the picker at the drive list.
 *     resolves    : the chosen absolute folder path when the user clicks OK,
 *                   or null on cancel (Cancel button, title-bar X, Escape).
 *     Only one picker can be open at a time; calling pick() while one is
 *     already open resolves immediately with null.
 *
 *   Zero-wiring option: any <button data-browse-target="inputId"> anywhere
 *   on the page is auto-wired by this file — clicking it opens the picker
 *   seeded with that input's value and writes the picked path back into the
 *   input (a "change" event is dispatched so pref listeners pick it up).
 *   music.js owners: add data-browse-target to a button OR call pick()
 *   yourself; both are supported. No other integration is required.
 *
 * ============================ BACKEND CONTRACT ===========================
 *
 *   GET /api/browse?path=<p>   (server.py, JSON only, directories only)
 *
 *     200 -> {path, parent, dirs:[{name, path, hidden}], drives:[{name,path}]}
 *            path="" means "drive list" (dirs empty, drives filled).
 *            parent="" means "up goes to the drive list".
 *     403 -> access denied; payload still carries path/parent/drives so the
 *            picker can navigate up instead of getting stuck.
 *     400 -> rejected path (UNC / drive-relative / not absolute), {error}.
 *     404 -> not a directory, {error}.
 */
"use strict";

window.BrowseDialog = (function () {
  const $ = (id) => document.getElementById(id);
  let resolver = null;    // active promise resolver, null when closed
  let curPath = "";       // folder currently listed ("" = drive list)
  let selected = null;    // highlighted subfolder/drive path, or null

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"]/g,
      (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  }

  function setMsg(t) { $("browseMsg").textContent = t || ""; }

  function isOpen() { return resolver !== null; }

  async function nav(path) {
    setMsg("Reading folder…");
    let st, data;
    try {
      const r = await fetch("api/browse?path=" + encodeURIComponent(path || ""));
      data = await r.json().catch(() => ({}));
      st = r.status;
    } catch (e) {
      setMsg("Server unreachable.");
      return;
    }
    if (st === 403) {
      // Access denied: stay where we can, offer the way back up.
      setMsg(data.error || "Access denied.");
      if (data.parent !== undefined && data.parent !== null) nav(data.parent);
      return;
    }
    if (st !== 200) {
      const msg = data.error || ("Cannot open " + path);
      if (path) {          // bad start path -> fall back to the drive list
        await nav("");
        setMsg(msg);
      } else {
        setMsg(msg);
      }
      return;
    }
    curPath = data.path || "";
    selected = null;
    render(data);
    setMsg("");
  }

  function render(data) {
    $("browsePath").value = curPath;
    // drive dropdown
    const sel = $("browseDrives");
    sel.innerHTML = '<option value="">Drives…</option>';
    (data.drives || []).forEach((d) => sel.add(new Option(d.name, d.path)));
    if (_isDriveRoot(curPath)) sel.value = curPath;
    // up button: disabled only at the drive list itself
    $("browseUp").disabled = (curPath === "");
    // list rows: drives at the root, subfolders otherwise
    const list = $("browseList");
    list.innerHTML = "";
    const rows = curPath === ""
      ? (data.drives || []).map((d) => ({ name: d.name, path: d.path, drive: true }))
      : (data.dirs || []);
    if (!rows.length) {
      list.innerHTML = '<div class="browse-empty">(no subfolders)</div>';
      return;
    }
    rows.forEach((d) => {
      const row = document.createElement("div");
      row.className = "browse-row" + (d.hidden ? " is-hidden" : "") +
        (d.drive ? " is-drive" : "");
      row.innerHTML = `<span class="browse-ico">${d.drive ? "&#128190;" : "&#128193;"}</span>` +
        `<span class="browse-name">${esc(d.name)}</span>` +
        (d.hidden ? '<span class="browse-hid">hidden</span>' : "");
      row.title = d.path + (d.hidden ? " (hidden)" : "");
      row.addEventListener("click", () => selectRow(row, d.path));
      row.addEventListener("dblclick", () => nav(d.path));
      list.appendChild(row);
    });
  }

  function _isDriveRoot(p) { return /^[A-Za-z]:[\\/]?$/.test(p || ""); }

  function selectRow(row, path) {
    selected = path;
    $("browseList").querySelectorAll(".browse-row.selected")
      .forEach((r) => r.classList.remove("selected"));
    row.classList.add("selected");
    $("browsePath").value = path;
  }

  function open(startAt) {
    if (isOpen()) return Promise.resolve(null);
    $("browseOverlay").classList.remove("hidden");
    nav((startAt || "").trim());
    setTimeout(() => $("browseList").focus(), 0);
    return new Promise((res) => { resolver = res; });
  }

  function close(result) {
    if (!isOpen()) return;
    const res = resolver;
    resolver = null;
    $("browseOverlay").classList.add("hidden");
    res(result);
  }

  function wireDom() {
    $("browseOk").addEventListener("click", () =>
      close(selected || curPath || null));
    $("browseCancel").addEventListener("click", () => close(null));
    $("browseClose").addEventListener("click", () => close(null));
    $("browseUp").addEventListener("click", async () => {
      if (curPath === "") return;
      let st, data;
      try {
        const r = await fetch("api/browse?path=" + encodeURIComponent(curPath));
        data = await r.json().catch(() => ({}));
        st = r.status;
      } catch (e) { setMsg("Server unreachable."); return; }
      if (st === 200 || st === 403) nav(data.parent || "");
    });
    $("browseDrives").addEventListener("change", () => {
      const v = $("browseDrives").value;
      if (v) nav(v);
    });
    $("browsePath").addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); nav($("browsePath").value.trim()); }
    });
    $("browseList").addEventListener("keydown", (e) => {
      if (e.key === "Enter" && selected) { e.preventDefault(); nav(selected); }
      if (e.key === "Backspace" && curPath !== "") { e.preventDefault(); $("browseUp").click(); }
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && isOpen()) { e.stopPropagation(); close(null); }
    }, true);
    // zero-wiring buttons: <button data-browse-target="inputId">
    document.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-browse-target]");
      if (!btn) return;
      const input = $(btn.getAttribute("data-browse-target"));
      if (!input) return;
      e.preventDefault();
      pick(input.value).then((p) => {
        if (p) {
          input.value = p;
          input.dispatchEvent(new Event("change", { bubbles: true }));
        }
      });
    });
  }

  function pick(currentPath) { return open(currentPath); }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", wireDom);
  } else {
    wireDom();
  }

  return { pick, isOpen };
})();
