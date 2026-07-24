/* Windows 98 desktop window manager - vanilla JS, no libraries.
 *
 * Manages every .app-window: drag by title bar, pointer-based resize from
 * all edges/corners, Win11-style edge snapping (left/right half, top = max)
 * with a preview outline, minimize/maximize/close via title-bar buttons,
 * taskbar buttons, z-order focus, viewport clamping, and localStorage
 * persistence of geometry per window id.
 */
"use strict";

const WM = (() => {
  const TASKBAR_H = 32;
  const EDGE = 6;            // px from screen edge that triggers snap preview
  const THRESH = 6;          // px of movement before a snapped window restores
  const STORE = "po.wm.";
  const RESIZE_DIRS = ["n", "s", "e", "w", "ne", "nw", "se", "sw"];

  let zTop = 100;
  const wins = new Map();    // id -> rec

  const desktop = document.getElementById("desktop");
  const taskButtons = document.getElementById("taskButtons");
  const snapPreview = document.getElementById("snapPreview");

  /* ---------------------------------------------------------- geometry */
  function workArea() {
    return { x: 0, y: 0, w: window.innerWidth, h: window.innerHeight - TASKBAR_H };
  }
  function getRect(el) {
    return {
      x: parseFloat(el.style.left) || 0,
      y: parseFloat(el.style.top) || 0,
      w: parseFloat(el.style.width) || el.offsetWidth,
      h: parseFloat(el.style.height) || el.offsetHeight,
    };
  }
  function setRect(el, r) {
    el.style.left = Math.round(r.x) + "px";
    el.style.top = Math.round(r.y) + "px";
    el.style.width = Math.round(r.w) + "px";
    el.style.height = Math.round(r.h) + "px";
  }
  function clampPos(el) {
    const wa = workArea();
    const r = getRect(el);
    r.x = Math.min(Math.max(r.x, -(r.w - 120)), Math.max(0, wa.w - 120));
    r.y = Math.min(Math.max(r.y, 0), Math.max(0, wa.h - 34));
    setRect(el, r);
  }
  function num(v, fallback) {
    const n = parseFloat(v);
    return Number.isFinite(n) ? n : fallback;
  }

  /* ---------------------------------------------------------- persistence */
  function save(rec) {
    try {
      localStorage.setItem(STORE + rec.id, JSON.stringify({
        ...getRect(rec.el), mode: rec.mode, prev: rec.prev,
        open: rec.open, min: rec.minimized,
      }));
    } catch (e) { /* storage unavailable */ }
  }
  function load(id) {
    try {
      const raw = localStorage.getItem(STORE + id);
      return raw ? JSON.parse(raw) : null;
    } catch (e) { return null; }
  }

  /* ---------------------------------------------------------- focus/z */
  function focus(rec) {
    if (!rec) return;
    zTop += 1;
    rec.el.style.zIndex = zTop;
    wins.forEach((r) => {
      const active = r === rec && !r.minimized;
      r.el.querySelector(".title-bar").classList.toggle("inactive", !active);
      if (r.taskBtn) r.taskBtn.classList.toggle("active", active);
    });
  }
  function topFocused() {
    let best = null, bestZ = -1;
    wins.forEach((r) => {
      if (r.open && !r.minimized) {
        const z = parseInt(r.el.style.zIndex || "0", 10);
        if (z > bestZ) { bestZ = z; best = r; }
      }
    });
    return best;
  }

  /* ---------------------------------------------------------- open/close */
  function ensureTaskBtn(rec) {
    if (rec.taskBtn) return;
    const b = document.createElement("div");
    b.className = "task-btn";
    b.innerHTML = `<span class="tb-icon">${rec.icon}</span><span class="ellipsis">${rec.task}</span>`;
    b.title = rec.task;
    b.addEventListener("click", () => {
      if (rec.minimized) { restore(rec); }
      else if (topFocused() === rec) { minimize(rec); }
      else { focus(rec); }
    });
    taskButtons.appendChild(b);
    rec.taskBtn = b;
  }

  function open(id) {
    const rec = wins.get(id);
    if (!rec) return;
    rec.open = true;
    rec.minimized = false;
    rec.el.classList.remove("hidden");
    ensureTaskBtn(rec);
    clampPos(rec.el);
    focus(rec);
    save(rec);
    try { rec.el.dispatchEvent(new CustomEvent("wm:open")); } catch (e) { /* ok */ }
  }
  function minimize(rec) {
    rec.minimized = true;
    rec.el.classList.add("hidden");
    rec.el.querySelector(".title-bar").classList.add("inactive");
    if (rec.taskBtn) rec.taskBtn.classList.remove("active");
    save(rec);
    const t = topFocused();
    if (t) focus(t);
  }
  function restore(rec) {
    rec.minimized = false;
    rec.el.classList.remove("hidden");
    focus(rec);
    save(rec);
  }
  function close(rec) { minimize(rec); }   // main apps: close == minimize

  function applyMode(rec) {
    const el = rec.el;
    el.classList.toggle("snapped", rec.mode !== "normal");
    const maxBtn = el.querySelector('.title-bar-controls button[aria-label="Maximize"],'
      + ' .title-bar-controls button[aria-label="Restore"]');
    if (maxBtn) maxBtn.setAttribute("aria-label", rec.mode === "max" ? "Restore" : "Maximize");
    if (rec.mode === "max") {
      setRect(el, workArea());
    } else if (rec.mode === "left" || rec.mode === "right") {
      const wa = workArea();
      setRect(el, { x: rec.mode === "left" ? 0 : Math.floor(wa.w / 2),
                    y: 0, w: Math.floor(wa.w / 2), h: wa.h });
    }
    save(rec);
  }
  function toggleMax(rec) {
    if (rec.mode === "max") {
      // restore to the geometry the window had before it was snapped/maximized
      rec.mode = "normal";
      if (rec.prev) setRect(rec.el, rec.prev);
      clampPos(rec.el);
      rec.el.classList.remove("snapped");
      const mb = rec.el.querySelector('.title-bar-controls button[aria-label="Restore"]');
      if (mb) mb.setAttribute("aria-label", "Maximize");
      save(rec);
      return;
    }
    if (rec.mode === "normal") rec.prev = getRect(rec.el);
    rec.mode = "max";
    applyMode(rec);
  }
  function snapTo(rec, zone) {
    if (rec.mode === "normal") rec.prev = getRect(rec.el);
    rec.mode = zone;   // 'left' | 'right' | 'max'
    applyMode(rec);
  }

  /* ---------------------------------------------------------- drag + snap */
  function snapZoneAt(cx, cy) {
    if (cy <= EDGE) return "max";
    if (cx <= EDGE) return "left";
    if (cx >= window.innerWidth - EDGE) return "right";
    return null;
  }
  function showPreview(zone) {
    if (!zone) { snapPreview.classList.add("hidden"); return; }
    const wa = workArea();
    let r;
    if (zone === "max") r = wa;
    else if (zone === "left") r = { x: 0, y: 0, w: Math.floor(wa.w / 2), h: wa.h };
    else r = { x: Math.floor(wa.w / 2), y: 0, w: Math.ceil(wa.w / 2), h: wa.h };
    snapPreview.style.left = r.x + "px";
    snapPreview.style.top = r.y + "px";
    snapPreview.style.width = r.w + "px";
    snapPreview.style.height = r.h + "px";
    snapPreview.classList.remove("hidden");
  }

  function wireDrag(rec, titleBar) {
    titleBar.addEventListener("pointerdown", (e) => {
      if (e.button !== 0) return;
      if (e.target.closest(".title-bar-controls")) return;
      focus(rec);
      const el = rec.el;
      const startRect = getRect(el);
      const wasMode = rec.mode;
      let grabX = e.clientX - startRect.x;
      let grabY = e.clientY - startRect.y;
      let restored = wasMode === "normal";
      let moved = false;
      try { titleBar.setPointerCapture(e.pointerId); } catch (err) { /* ok */ }
      document.body.classList.add("wm-drag");

      const onMove = (ev) => {
        const dx = ev.clientX - (startRect.x + grabX);
        const dy = ev.clientY - (startRect.y + grabY);
        if (!restored) {
          if (Math.abs(dx) + Math.abs(dy) < THRESH) return;
          // dragging a snapped/maximized window: restore previous size first,
          // keeping the cursor at the same relative spot on the title bar
          const prev = rec.prev || startRect;
          const ratio = startRect.w > 0 ? grabX / startRect.w : 0.5;
          rec.mode = "normal";
          el.classList.remove("snapped");
          const mb = el.querySelector('.title-bar-controls button[aria-label="Restore"]');
          if (mb) mb.setAttribute("aria-label", "Maximize");
          grabX = Math.round(prev.w * ratio);
          grabY = Math.min(grabY, 20);
          setRect(el, { x: ev.clientX - grabX, y: ev.clientY - grabY,
                        w: prev.w, h: prev.h });
          restored = true;
        }
        moved = true;
        const r = getRect(el);
        setRect(el, { ...r, x: ev.clientX - grabX, y: ev.clientY - grabY });
        clampPos(el);
        showPreview(snapZoneAt(ev.clientX, ev.clientY));
      };
      const onUp = (ev) => {
        titleBar.removeEventListener("pointermove", onMove);
        titleBar.removeEventListener("pointerup", onUp);
        titleBar.removeEventListener("pointercancel", onUp);
        document.body.classList.remove("wm-drag");
        const zone = moved ? snapZoneAt(ev.clientX, ev.clientY) : null;
        showPreview(null);
        if (zone) { snapTo(rec, zone); }
        else { clampPos(el); save(rec); }
      };
      titleBar.addEventListener("pointermove", onMove);
      titleBar.addEventListener("pointerup", onUp);
      titleBar.addEventListener("pointercancel", onUp);
    });
    titleBar.addEventListener("dblclick", (e) => {
      if (e.target.closest(".title-bar-controls")) return;
      toggleMax(rec);
    });
  }

  /* ---------------------------------------------------------- resize */
  function wireResize(rec, handle, dir) {
    handle.addEventListener("pointerdown", (e) => {
      if (e.button !== 0 || rec.mode !== "normal") return;
      e.preventDefault();
      focus(rec);
      const el = rec.el;
      const r0 = getRect(el);
      const x0 = e.clientX, y0 = e.clientY;
      const minW = rec.minW, minH = rec.minH;
      const wa = workArea();
      try { handle.setPointerCapture(e.pointerId); } catch (err) { /* ok */ }
      document.body.classList.add("wm-drag");

      const onMove = (ev) => {
        const dx = ev.clientX - x0, dy = ev.clientY - y0;
        let { x, y, w, h } = r0;
        if (dir.includes("e")) w = r0.w + dx;
        if (dir.includes("s")) h = r0.h + dy;
        if (dir.includes("w")) { w = r0.w - dx; x = r0.x + dx; }
        if (dir.includes("n")) { h = r0.h - dy; y = r0.y + dy; }
        if (w < minW) { if (dir.includes("w")) x -= (minW - w); w = minW; }
        if (h < minH) { if (dir.includes("n")) y -= (minH - h); h = minH; }
        w = Math.min(w, wa.w + 40);
        h = Math.min(h, wa.h + 40);
        setRect(el, { x, y, w, h });
      };
      const onUp = () => {
        handle.removeEventListener("pointermove", onMove);
        handle.removeEventListener("pointerup", onUp);
        handle.removeEventListener("pointercancel", onUp);
        document.body.classList.remove("wm-drag");
        clampPos(el);
        save(rec);
      };
      handle.addEventListener("pointermove", onMove);
      handle.addEventListener("pointerup", onUp);
      handle.addEventListener("pointercancel", onUp);
    });
  }

  /* ---------------------------------------------------------- init */
  function register(el) {
    const id = el.id;
    const rec = {
      id, el,
      task: el.dataset.task || id,
      icon: el.dataset.icon || "&#9634;",
      minW: num(el.dataset.minw, 320),
      minH: num(el.dataset.minh, 220),
      mode: "normal",
      prev: null,
      open: false,
      minimized: false,
      taskBtn: null,
    };
    wins.set(id, rec);

    // geometry: saved state wins, then data-def* defaults
    const saved = load(id) || {};
    const wa = workArea();
    const r = {
      x: num(saved.x, num(el.dataset.defx, 60)),
      y: num(saved.y, num(el.dataset.defy, 40)),
      w: num(saved.w, num(el.dataset.defw, 800)),
      h: num(saved.h, num(el.dataset.defh, 600)),
    };
    r.w = Math.max(rec.minW, Math.min(r.w, wa.w));
    r.h = Math.max(rec.minH, Math.min(r.h, wa.h));
    setRect(el, r);
    if (saved.prev && Number.isFinite(saved.prev.w)) rec.prev = saved.prev;
    if (saved.mode === "max" || saved.mode === "left" || saved.mode === "right") {
      rec.mode = saved.mode;
      applyMode(rec);
    }

    // resize handles
    RESIZE_DIRS.forEach((dir) => {
      const h = document.createElement("div");
      h.className = "rsz rsz-" + dir;
      el.appendChild(h);
      wireResize(rec, h, dir);
    });

    // title bar: drag + buttons
    const titleBar = el.querySelector(".title-bar");
    wireDrag(rec, titleBar);
    el.addEventListener("pointerdown", () => {
      if (!rec.minimized) focus(rec);
    }, true);
    el.querySelectorAll(".title-bar-controls button").forEach((b) => {
      b.addEventListener("click", (e) => {
        e.stopPropagation();
        const a = b.getAttribute("aria-label");
        if (a === "Minimize") minimize(rec);
        else if (a === "Maximize" || a === "Restore") toggleMax(rec);
        else if (a === "Close") close(rec);
      });
    });

    // initial visibility
    const shouldOpen = saved.open !== undefined ? !!saved.open : (id === "winWizard");
    if (shouldOpen) {
      open(id);
      if (saved.min) minimize(rec);
    } else {
      el.classList.add("hidden");
    }
  }

  /* ---------------------------------------------------------- chrome */
  function initDesktopIcons() {
    document.querySelectorAll(".desk-icon").forEach((icon) => {
      icon.addEventListener("click", () => {
        document.querySelectorAll(".desk-icon.selected")
          .forEach((i) => i.classList.remove("selected"));
        icon.classList.add("selected");
      });
      icon.addEventListener("dblclick", () => open(icon.dataset.app));
      icon.addEventListener("keydown", (e) => {
        if (e.key === "Enter") open(icon.dataset.app);
      });
    });
    desktop.addEventListener("pointerdown", (e) => {
      if (e.target === desktop) {
        document.querySelectorAll(".desk-icon.selected")
          .forEach((i) => i.classList.remove("selected"));
      }
    });
  }

  function initStartMenu() {
    const btn = document.getElementById("startBtn");
    const menu = document.getElementById("startMenu");
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      menu.classList.toggle("hidden");
      btn.classList.toggle("active", !menu.classList.contains("hidden"));
    });
    document.addEventListener("pointerdown", (e) => {
      if (!menu.classList.contains("hidden")
          && !e.target.closest("#startMenu") && !e.target.closest("#startBtn")) {
        menu.classList.add("hidden");
        btn.classList.remove("active");
      }
    });
    menu.querySelectorAll(".sm-item").forEach((item) => {
      item.addEventListener("click", () => {
        menu.classList.add("hidden");
        btn.classList.remove("active");
        open(item.dataset.app);
      });
    });
  }

  function initClock() {
    const el = document.getElementById("taskClock");
    const tick = () => {
      const d = new Date();
      el.textContent = String(d.getHours()).padStart(2, "0") + ":"
        + String(d.getMinutes()).padStart(2, "0");
    };
    tick();
    setInterval(tick, 5000);
  }

  window.addEventListener("resize", () => {
    wins.forEach((rec) => {
      if (rec.mode !== "normal") applyMode(rec);
      else if (rec.open && !rec.minimized) clampPos(rec.el);
    });
  });

  document.querySelectorAll(".app-window").forEach(register);
  initDesktopIcons();
  initStartMenu();
  initClock();

  const byId = (id, fn) => { const r = wins.get(id); if (r) fn(r); };
  return { open,
           minimize: (id) => byId(id, minimize),
           close: (id) => byId(id, close),
           focus: (id) => byId(id, focus),
           restore: (id) => byId(id, restore),
           isOpen: (id) => { const r = wins.get(id); return !!(r && r.open && !r.minimized); } };
})();
