// win98.js — "Aegis 98": the full Windows 98 desktop easter egg.
//
// Turns Aegis into a 1998 desktop: teal desktop with double-clickable program
// icons on the welcome screen, a taskbar with a Start menu and clock, DOS-blue
// code blocks, and every tool renamed to its most-1999 alter ego (Aegis
// Instant Messenger, AegisWire, Aegis Encarta 99, ...). The chrome itself
// (bevels, title bars, scrollbars) lives in static/win98.css, scoped under
// the `theme-win98` class that theme.js applyWin98() manages.
//
// Enabled via the Theme popup ("Aegis 98" preset), Settings → Appearance, or
// /win98. State is simply "the active theme is win98" — this module reacts to
// the 'aegis-win98-theme' event theme.js fires, and its enable()/disable()
// switch the theme (stashing the previous one in localStorage for restore).

import themeModule from './theme.js';

const LS_PREV = 'aegis-win98-prev';

const REDUCED_MOTION = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
const COARSE_POINTER = window.matchMedia && window.matchMedia('(pointer: coarse)').matches;

let _active = false;
let _clockTimer = 0;
let _observer = null;
// Aborts the document-level listeners added by _buildTaskbar so disable()
// doesn't leak them (and re-enable doesn't stack duplicates).
let _docAbort = null;

/* ── Pixel-flavored program icons (32×32) ── */
const I = {
  aim: `<svg viewBox="0 0 32 32" xmlns="http://www.w3.org/2000/svg" aria-hidden="true"><circle cx="16" cy="7.5" r="4.5" fill="#ffcf00" stroke="#000"/><path d="M8 28 L13.5 18 L11.5 13 L20.5 13 L18.5 19 L24 28 L19 28 L15.8 22.5 L13 28 Z" fill="#ffcf00" stroke="#000"/></svg>`,
  wire: `<svg viewBox="0 0 32 32" xmlns="http://www.w3.org/2000/svg" aria-hidden="true"><circle cx="16" cy="16" r="11.5" fill="#7fd10a" stroke="#2e5c00" stroke-width="2"/><circle cx="16" cy="16" r="8" fill="#c8f070"/><g stroke="#7fd10a" stroke-width="2"><line x1="16" y1="8" x2="16" y2="24"/><line x1="8" y1="16" x2="24" y2="16"/><line x1="10.3" y1="10.3" x2="21.7" y2="21.7"/><line x1="21.7" y1="10.3" x2="10.3" y2="21.7"/></g></svg>`,
  word: `<svg viewBox="0 0 32 32" xmlns="http://www.w3.org/2000/svg" aria-hidden="true"><rect x="7" y="3.5" width="18" height="25" fill="#fff" stroke="#404040"/><path d="M10 11 L12.8 24 L16 15.5 L19.2 24 L22 11" stroke="#1045a8" stroke-width="2.6" fill="none"/></svg>`,
  express: `<svg viewBox="0 0 32 32" xmlns="http://www.w3.org/2000/svg" aria-hidden="true"><rect x="3.5" y="8.5" width="20" height="14" fill="#fff" stroke="#000"/><path d="M3.5 8.5 L13.5 16.5 L23.5 8.5" fill="none" stroke="#000"/><path d="M19 24.5 h8.5 M27.5 24.5 l-3.4 -3.4 M27.5 24.5 l-3.4 3.4" stroke="#1084d0" stroke-width="2.4" fill="none"/></svg>`,
  brain: `<svg viewBox="0 0 32 32" xmlns="http://www.w3.org/2000/svg" aria-hidden="true"><rect x="4.5" y="4.5" width="23" height="16" fill="#c0c0c0" stroke="#000"/><rect x="6.5" y="6.5" width="19" height="12" fill="#008080"/><path d="M12 13.5 q1.6 -3.4 4 -1 q2.4 -2.4 4 1 q0.6 2.8 -4 2.8 q-4.6 0 -4 -2.8" fill="#ff9ecb" stroke="#b0447e" stroke-width=".8"/><rect x="12" y="22.5" width="8" height="3" fill="#c0c0c0" stroke="#000"/><rect x="8.5" y="26" width="15" height="2.5" fill="#808080" stroke="#000"/></svg>`,
  pad: `<svg viewBox="0 0 32 32" xmlns="http://www.w3.org/2000/svg" aria-hidden="true"><rect x="8" y="4.5" width="16" height="23" fill="#fff" stroke="#000"/><rect x="8" y="4.5" width="16" height="3.5" fill="#4aa3e0" stroke="#000"/><g stroke="#9cb6d4" stroke-width="1.4"><line x1="10.5" y1="12" x2="21.5" y2="12"/><line x1="10.5" y1="16" x2="21.5" y2="16"/><line x1="10.5" y1="20" x2="18.5" y2="20"/></g></svg>`,
  sched: `<svg viewBox="0 0 32 32" xmlns="http://www.w3.org/2000/svg" aria-hidden="true"><rect x="4.5" y="6" width="23" height="21" fill="#fff" stroke="#000"/><rect x="4.5" y="6" width="23" height="5" fill="#c03030" stroke="#000"/><rect x="9" y="3" width="2.4" height="5" fill="#404040"/><rect x="20.6" y="3" width="2.4" height="5" fill="#404040"/><g stroke="#c8c8c8"><line x1="4.5" y1="16" x2="27.5" y2="16"/><line x1="4.5" y1="21" x2="27.5" y2="21"/><line x1="12.2" y1="11" x2="12.2" y2="27"/><line x1="19.8" y1="11" x2="19.8" y2="27"/></g></svg>`,
  encarta: `<svg viewBox="0 0 32 32" xmlns="http://www.w3.org/2000/svg" aria-hidden="true"><circle cx="16" cy="16" r="10.5" fill="#2060c0" stroke="#000"/><path d="M9 12.5 q4 3.4 7 0 q4 -3.4 7.4 1" stroke="#50c878" fill="none" stroke-width="2.4"/><path d="M8.5 20 q5 -2.6 9 0 q3.4 2 6 .4" stroke="#50c878" fill="none" stroke-width="2"/><ellipse cx="16" cy="16" rx="14" ry="5" fill="none" stroke="#ffd21e" stroke-width="1.6" transform="rotate(-18 16 16)"/></svg>`,
  wizard: `<svg viewBox="0 0 32 32" xmlns="http://www.w3.org/2000/svg" aria-hidden="true"><rect x="3.5" y="4" width="9.5" height="7.5" fill="#ffd21e" stroke="#000"/><rect x="19" y="20.5" width="9.5" height="7.5" fill="#ffd21e" stroke="#000"/><path d="M13 7.8 h6.5 v12.7" fill="none" stroke="#000" stroke-width="1.6"/><path d="M24 4.5 l1.3 3.2 3.2 1.3 -3.2 1.3 -1.3 3.2 -1.3 -3.2 -3.2 -1.3 3.2 -1.3 Z" fill="#e040fb" stroke="#000" stroke-width=".8"/></svg>`,
  netduel: `<svg viewBox="0 0 32 32" xmlns="http://www.w3.org/2000/svg" aria-hidden="true"><rect x="2.5" y="7.5" width="12.5" height="10" fill="#c0c0c0" stroke="#000"/><rect x="4" y="9" width="9.5" height="7" fill="#1045a8"/><rect x="17" y="7.5" width="12.5" height="10" fill="#c0c0c0" stroke="#000"/><rect x="18.5" y="9" width="9.5" height="7" fill="#308030"/><path d="M17.5 20 l-3 4.5 h3 l-3 5" stroke="#ffd21e" stroke-width="2" fill="none"/></svg>`,
  install: `<svg viewBox="0 0 32 32" xmlns="http://www.w3.org/2000/svg" aria-hidden="true"><path d="M5.5 5.5 h17.5 l3.5 3.5 v17.5 h-21 Z" fill="#1045a8" stroke="#000"/><rect x="10" y="5.5" width="10" height="7.5" fill="#c0d4f0" stroke="#000"/><rect x="16" y="7" width="2.6" height="4.5" fill="#1045a8"/><rect x="9" y="17" width="14" height="9.5" fill="#fff" stroke="#000"/><g stroke="#9cb6d4" stroke-width="1.3"><line x1="11" y1="20" x2="21" y2="20"/><line x1="11" y1="23.5" x2="21" y2="23.5"/></g></svg>`,
  devmgr: `<svg viewBox="0 0 32 32" xmlns="http://www.w3.org/2000/svg" aria-hidden="true"><g stroke="#808080" stroke-width="2"><line x1="11" y1="3" x2="11" y2="8"/><line x1="16" y1="3" x2="16" y2="8"/><line x1="21" y1="3" x2="21" y2="8"/><line x1="11" y1="24" x2="11" y2="29"/><line x1="16" y1="24" x2="16" y2="29"/></g><rect x="7" y="7" width="18" height="18" fill="#308030" stroke="#000"/><rect x="11.5" y="11.5" width="9" height="9" fill="#c0c0c0" stroke="#000"/><circle cx="24.5" cy="24.5" r="6" fill="#ffd21e" stroke="#000"/><rect x="23.5" y="20.8" width="2" height="5" fill="#000"/><rect x="23.5" y="27" width="2" height="1.8" fill="#000"/></svg>`,
  tasksched: `<svg viewBox="0 0 32 32" xmlns="http://www.w3.org/2000/svg" aria-hidden="true"><rect x="5" y="4.5" width="15" height="23" fill="#fff" stroke="#000"/><rect x="9.5" y="2.5" width="6" height="4" fill="#c0c0c0" stroke="#000"/><g stroke="#404040" stroke-width="1.4"><line x1="8" y1="12" x2="17" y2="12"/><line x1="8" y1="16" x2="17" y2="16"/><line x1="8" y1="20" x2="14" y2="20"/></g><circle cx="23.5" cy="21.5" r="6.5" fill="#fff" stroke="#000"/><path d="M23.5 17.5 v4 h3.2" stroke="#c03030" fill="none" stroke-width="1.8"/></svg>`,
  display: `<svg viewBox="0 0 32 32" xmlns="http://www.w3.org/2000/svg" aria-hidden="true"><rect x="4" y="4.5" width="24" height="17.5" fill="#c0c0c0" stroke="#000"/><rect x="6" y="6.5" width="20" height="13.5" fill="#008080"/><rect x="8" y="8.5" width="7.5" height="4.5" fill="#e040fb"/><rect x="16.5" y="8.5" width="7.5" height="4.5" fill="#ffd21e"/><rect x="8" y="14" width="7.5" height="4.5" fill="#1084d0"/><rect x="16.5" y="14" width="7.5" height="4.5" fill="#50c878"/><rect x="11.5" y="23.5" width="9" height="2.5" fill="#c0c0c0" stroke="#000"/><rect x="8" y="27" width="16" height="2.2" fill="#808080" stroke="#000"/></svg>`,
  ctrlpanel: `<svg viewBox="0 0 32 32" xmlns="http://www.w3.org/2000/svg" aria-hidden="true"><rect x="4.5" y="4.5" width="23" height="23" fill="#c0c0c0" stroke="#000"/><g stroke="#404040" stroke-width="2"><line x1="10" y1="8" x2="10" y2="24.5"/><line x1="16" y1="8" x2="16" y2="24.5"/><line x1="22" y1="8" x2="22" y2="24.5"/></g><rect x="7.5" y="11" width="5" height="4.5" fill="#1084d0" stroke="#000"/><rect x="13.5" y="18" width="5" height="4.5" fill="#1084d0" stroke="#000"/><rect x="19.5" y="9" width="5" height="4.5" fill="#1084d0" stroke="#000"/></svg>`,
  flag: `<svg viewBox="0 0 32 32" xmlns="http://www.w3.org/2000/svg" aria-hidden="true"><rect x="5" y="5" width="10" height="10" fill="#d03434"/><rect x="17" y="5" width="10" height="10" fill="#3ca43c"/><rect x="5" y="17" width="10" height="10" fill="#3060c8"/><rect x="17" y="17" width="10" height="10" fill="#e8c020"/></svg>`,
};

/* ── The most-1999 version of every Aegis tool ── */
function _click(sel) { const el = document.querySelector(sel); if (el) el.click(); return !!el; }
const APPS = [
  { id: 'aim',       title: 'Aegis Instant Messenger', icon: I.aim,       launch: () => _click('#sidebar-new-chat-btn'), desktop: true },
  { id: 'wire',      title: 'AegisWire',               icon: I.wire,      launch: () => _click('#tool-gallery-btn'),     desktop: true },
  { id: 'brain',     title: 'My Brain',                icon: I.brain,     launch: () => _click('#tool-memory-btn'),      desktop: true },
  { id: 'word',      title: 'Aegis Word 97',           icon: I.word,      launch: () => _click('#tool-library-btn'),     desktop: true },
  { id: 'express',   title: 'Aegis Express',           icon: I.express,   launch: () => _click('#email-section-title'),  desktop: true },
  { id: 'pad',       title: 'AegisPad',                icon: I.pad,       launch: () => _click('#tool-notes-btn'),       desktop: true },
  { id: 'sched',     title: 'Aegis Schedule+',         icon: I.sched,     launch: () => _click('#tool-calendar-btn'),    desktop: true },
  { id: 'encarta',   title: 'Aegis Encarta 99',        icon: I.encarta,   launch: () => _click('#tool-research-btn'),    desktop: true },
  { id: 'wizard',    title: 'Recipe Wizard 98',        icon: I.wizard,    launch: () => _click('#tool-recipes-btn'),     desktop: true },
  { id: 'netduel',   title: 'Browser Wars',            icon: I.netduel,   launch: () => _click('#tool-compare-btn'),     desktop: true },
  { id: 'install',   title: 'Aegis InstallShield',     icon: I.install,   launch: () => _click('#tool-cookbook-btn') },
  { id: 'devmgr',    title: 'Device Manager',          icon: I.devmgr,    launch: () => _click('#tool-engine-btn') },
  { id: 'tasksched', title: 'Task Scheduler',          icon: I.tasksched, launch: () => _click('#tool-tasks-btn') },
  { id: 'display',   title: 'Display Properties',      icon: I.display,   launch: () => _click('#tool-theme-btn') },
  { id: 'ctrlpanel', title: 'Control Panel',           icon: I.ctrlpanel, launch: () => _click('#user-bar-settings') },
];

/* Window-title parody map, keyed by the header's original text. Covers both
   the static modals in index.html and dynamically-built ones (observer). */
const TITLES = {
  'Brain': 'My Brain',
  'Studio': 'AegisWire',
  'Notes': 'AegisPad',
  'Calendar': 'Aegis Schedule+',
  'Tasks': 'Task Scheduler',
  'Library': 'Aegis Word 97',
  'Documents': 'Aegis Word 97',
  'Recipes': 'Recipe Wizard 98',
  'Cookbook': 'Aegis InstallShield',
  'Engine': 'Device Manager',
  'Deep Research': 'Aegis Encarta 99',
  'Settings': 'Control Panel',
  'Theme': 'Display Properties',
  'Email': 'Aegis Express',
  'Inbox': 'Aegis Express',
  'Compare': 'Browser Wars',
};

/* ── DOS-blue code palette (QBasic/EDIT.COM on CGA) ──
   applyColors() writes --hl-* as INLINE styles on <html>, which beat any
   stylesheet — so the DOS palette must be re-asserted inline after every
   theme apply while win98 is active. */
const DOS_HL = {
  '--hl-bg': '#0000a8', '--hl-fg': '#ffffff', '--hl-keyword': '#ffff55',
  '--hl-string': '#55ffff', '--hl-comment': '#55ff55', '--hl-function': '#ffffff',
  '--hl-number': '#ff55ff', '--hl-builtin': '#55ffff', '--hl-variable': '#ffffff',
  '--hl-params': '#c0c0c0',
};
function _applyDosPalette() {
  const s = document.documentElement.style;
  for (const [k, v] of Object.entries(DOS_HL)) s.setProperty(k, v);
}

/* ── Startup chime — an original synth chord, no 1998 samples were harmed ── */
function _chime() {
  try {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return;
    const ctx = new Ctx();
    const master = ctx.createGain();
    master.gain.value = 0.11;
    master.connect(ctx.destination);
    // E♭ major, rising and shimmering — the spirit of a late-90s boot.
    const notes = [[311.13, 0], [392.0, 0.12], [466.16, 0.24], [622.25, 0.38]];
    for (const [freq, at] of notes) {
      const t = ctx.currentTime + at;
      const o = ctx.createOscillator();
      o.type = 'triangle';
      o.frequency.value = freq;
      const g = ctx.createGain();
      g.gain.setValueAtTime(0.0001, t);
      g.gain.exponentialRampToValueAtTime(0.6, t + 0.06);
      g.gain.exponentialRampToValueAtTime(0.0001, t + 2.1);
      o.connect(g); g.connect(master);
      o.start(t); o.stop(t + 2.2);
    }
    setTimeout(() => { try { ctx.close(); } catch (_) {} }, 3000);
  } catch (_) {}
}

/* ── Boot splash ── */
function _splash() {
  if (REDUCED_MOTION || document.getElementById('win98-splash')) return;
  const el = document.createElement('div');
  el.id = 'win98-splash';
  el.setAttribute('aria-hidden', 'true');
  el.innerHTML = `
    <div class="w98-splash-logo">Aegis<span class="w98-splash-98">98</span></div>
    <div class="w98-splash-bar"></div>
    <div class="w98-splash-sub">Starting Aegis 98...</div>`;
  document.body.appendChild(el);
  setTimeout(() => { el.style.opacity = '0'; }, 1500);
  setTimeout(() => el.remove(), 1900);
}

/* ── Desktop ── */
function _buildDesktop() {
  const container = document.getElementById('chat-container');
  if (!container || document.getElementById('win98-desktop')) return;
  const desk = document.createElement('div');
  desk.id = 'win98-desktop';
  desk.setAttribute('role', 'group');
  desk.setAttribute('aria-label', 'Aegis 98 desktop');
  const icons = document.createElement('div');
  icons.className = 'w98-icons';
  for (const app of APPS.filter(a => a.desktop)) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'w98-icon';
    btn.dataset.app = app.id;
    btn.setAttribute('aria-label', app.title);
    btn.innerHTML = `${app.icon}<span class="w98-icon-label">${app.title}</span>`;
    if (COARSE_POINTER) {
      btn.addEventListener('click', () => app.launch());
    } else {
      btn.addEventListener('click', (e) => {
        icons.querySelectorAll('.w98-icon.selected').forEach(i => i.classList.remove('selected'));
        btn.classList.add('selected');
        e.stopPropagation();
      });
      btn.addEventListener('dblclick', () => app.launch());
      btn.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); app.launch(); } });
    }
    icons.appendChild(btn);
  }
  desk.appendChild(icons);
  desk.addEventListener('click', () => {
    icons.querySelectorAll('.w98-icon.selected').forEach(i => i.classList.remove('selected'));
  });
  container.appendChild(desk);
}

/* ── Taskbar + Start menu ── */
function _buildTaskbar() {
  if (document.getElementById('win98-taskbar')) return;
  const bar = document.createElement('div');
  bar.id = 'win98-taskbar';
  bar.setAttribute('role', 'toolbar');
  bar.setAttribute('aria-label', 'Aegis 98 taskbar');
  bar.innerHTML = `
    <button type="button" id="win98-start-btn" aria-haspopup="menu" aria-expanded="false">${I.flag}<span>Start</span></button>
    <span class="w98-taskbar-divider" aria-hidden="true"></span>
    <span id="win98-clock" title="1998 forever"></span>`;
  document.body.appendChild(bar);

  const menu = document.createElement('div');
  menu.id = 'win98-startmenu';
  menu.setAttribute('role', 'menu');
  menu.style.display = 'none';
  const items = APPS.map(app =>
    `<div class="w98-sm-item" role="menuitem" tabindex="0" data-app="${app.id}">${app.icon}<span>${app.title}</span></div>`
  ).join('');
  menu.innerHTML = `
    <div class="w98-sm-banner" aria-hidden="true">Aegis&nbsp;98</div>
    <div class="w98-sm-items">
      ${items}
      <div class="w98-sm-sep" role="separator"></div>
      <div class="w98-sm-item" role="menuitem" tabindex="0" data-egg="bonzi"><span class="w98-sm-emoji">🦍</span><span>Bonzi Buddy</span></div>
      <div class="w98-sm-item" role="menuitem" tabindex="0" data-egg="winamp"><span class="w98-sm-emoji">🎛️</span><span>Aegis Amp</span></div>
      <div class="w98-sm-sep" role="separator"></div>
      <div class="w98-sm-item" role="menuitem" tabindex="0" data-action="shutdown"><span class="w98-sm-emoji">🔌</span><span>Shut Down...</span></div>
    </div>`;
  document.body.appendChild(menu);

  const startBtn = bar.querySelector('#win98-start-btn');
  const toggleMenu = (open) => {
    const on = open !== undefined ? open : menu.style.display === 'none';
    menu.style.display = on ? 'flex' : 'none';
    startBtn.classList.toggle('open', on);
    startBtn.setAttribute('aria-expanded', on ? 'true' : 'false');
  };
  startBtn.addEventListener('click', (e) => { e.stopPropagation(); toggleMenu(); });
  _docAbort = new AbortController();
  document.addEventListener('click', (e) => {
    if (menu.style.display !== 'none' && !menu.contains(e.target)) toggleMenu(false);
  }, { signal: _docAbort.signal });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') toggleMenu(false); },
    { signal: _docAbort.signal });

  menu.addEventListener('click', (e) => {
    const item = e.target.closest('.w98-sm-item');
    if (!item) return;
    toggleMenu(false);
    const app = APPS.find(a => a.id === item.dataset.app);
    if (app) { app.launch(); return; }
    if (item.dataset.egg === 'bonzi' && window.bonziBuddy) {
      window.bonziBuddy.enabled ? window.bonziBuddy.disable() : window.bonziBuddy.enable();
      return;
    }
    if (item.dataset.egg === 'winamp' && window.aegisAmp) {
      window.aegisAmp.enabled ? window.aegisAmp.disable() : window.aegisAmp.enable();
      return;
    }
    if (item.dataset.action === 'shutdown') _shutdownDialog();
  });
  menu.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') {
      const item = e.target.closest('.w98-sm-item');
      if (item) { e.preventDefault(); item.click(); }
    }
  });

  _tickClock();
  _clockTimer = setInterval(_tickClock, 30000);
}

function _tickClock() {
  const el = document.getElementById('win98-clock');
  if (!el) return;
  const d = new Date();
  let h = d.getHours();
  const ampm = h >= 12 ? 'PM' : 'AM';
  h = h % 12 || 12;
  el.textContent = `${h}:${String(d.getMinutes()).padStart(2, '0')} ${ampm}`;
}

/* ── Shut Down dialog ── */
function _shutdownDialog() {
  if (document.getElementById('win98-shutdown')) return;
  const overlay = document.createElement('div');
  overlay.id = 'win98-shutdown';
  overlay.innerHTML = `
    <div class="w98-sd-box" role="dialog" aria-label="Shut Down Aegis 98">
      <div class="w98-sd-title">Shut Down Aegis 98</div>
      <div class="w98-sd-body">
        <div>What do you want Aegis to do?</div>
        <label><input type="radio" name="w98sd" value="restore" checked> Return to the future (previous theme)</label>
        <label><input type="radio" name="w98sd" value="reload"> Restart Aegis 98</label>
        <label><input type="radio" name="w98sd" value="stay"> Stay in 1998</label>
      </div>
      <div class="w98-sd-btns">
        <button type="button" data-sd="ok">OK</button>
        <button type="button" data-sd="cancel">Cancel</button>
      </div>
    </div>`;
  document.body.appendChild(overlay);
  overlay.addEventListener('click', (e) => { if (e.target === overlay) overlay.remove(); });
  overlay.querySelector('[data-sd="cancel"]').addEventListener('click', () => overlay.remove());
  overlay.querySelector('[data-sd="ok"]').addEventListener('click', () => {
    const v = (overlay.querySelector('input[name="w98sd"]:checked') || {}).value;
    overlay.remove();
    if (v === 'restore') api.disable();
    else if (v === 'reload') window.location.reload();
  });
}

/* ── Window-title parody renaming ── */
function _retitleIn(root) {
  const headers = root.querySelectorAll ? root.querySelectorAll('.modal-header h2, .modal-header h3, .modal-header h4') : [];
  headers.forEach(h => {
    if (h.dataset.w98Orig !== undefined) return;
    const textNodes = Array.from(h.childNodes).filter(n => n.nodeType === 3 && n.textContent.trim());
    if (!textNodes.length) return;
    const current = textNodes.map(n => n.textContent).join('').trim();
    const parody = TITLES[current];
    if (!parody) return;
    h.dataset.w98Orig = current;
    textNodes.forEach((n, i) => { n.textContent = i === textNodes.length - 1 ? parody : ''; });
  });
}
function _restoreTitles() {
  document.querySelectorAll('[data-w98-orig]').forEach(h => {
    const textNodes = Array.from(h.childNodes).filter(n => n.nodeType === 3);
    if (textNodes.length) textNodes[textNodes.length - 1].textContent = h.dataset.w98Orig;
    delete h.dataset.w98Orig;
  });
}
function _startObserver() {
  if (_observer) return;
  _observer = new MutationObserver(muts => {
    for (const m of muts) {
      for (const n of m.addedNodes) {
        if (n.nodeType !== 1) continue;
        // Cheap filter first: chat streaming adds thousands of deep nodes —
        // only class-matched nodes or top-level (body-child) additions get
        // the querySelector treatment.
        if (n.matches?.('.modal, .modal-content') ||
            (m.target === document.body && n.querySelector?.('.modal-header'))) {
          _retitleIn(n);
        }
      }
    }
  });
  _observer.observe(document.body, { childList: true, subtree: true });
}

/* ── Activate / deactivate: react to the theme state (no theme changes here) ── */
function _activate() {
  _applyDosPalette();
  if (_active) return;
  _active = true;
  const boot = () => { _buildDesktop(); _buildTaskbar(); _retitleIn(document); _startObserver(); };
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot, { once: true });
  } else boot();
}
function _deactivate() {
  if (!_active) return;
  _active = false;
  clearInterval(_clockTimer);
  _clockTimer = 0;
  if (_observer) { _observer.disconnect(); _observer = null; }
  if (_docAbort) { _docAbort.abort(); _docAbort = null; }
  ['win98-desktop', 'win98-taskbar', 'win98-startmenu', 'win98-shutdown', 'win98-splash']
    .forEach(id => document.getElementById(id)?.remove());
  _restoreTitles();
  // Re-derive the incoming theme's syntax colors: applyColors() may have run
  // while the DOS palette ping was still re-asserting itself (the theme-win98
  // class drops only at the end of save()), leaving stale inline --hl-* vars.
  try {
    const saved = themeModule.getSaved();
    if (saved && saved.colors) themeModule.applyColors(saved.colors);
  } catch (_) {}
}

/* ── Theme switching (the enable/disable user intent) ── */
function _applyOpts(saved) {
  // Re-apply the non-color parts of a stored theme snapshot.
  try {
    themeModule.applyFontDensity(saved.font, saved.density);
    themeModule.applyBgPattern(saved.bgPattern || 'none');
    themeModule.applyBgEffectColor(saved.bgEffectColor || '');
    themeModule.applyBgEffectIntensity(saved.bgEffectIntensity !== undefined ? saved.bgEffectIntensity : 1);
    themeModule.applyBgEffectSize(saved.bgEffectSize !== undefined ? saved.bgEffectSize : 1);
    themeModule.applyFrostedGlass(!!saved.frosted);
  } catch (_) {}
}

const api = {
  get enabled() { return document.documentElement.classList.contains('theme-win98'); },
  enable() {
    if (this.enabled) { _activate(); return; }
    // (The outgoing theme is stashed to 'aegis-win98-prev' by theme.js
    // save() — every enable path funnels through it.)
    _splash();
    _chime();
    const colors = themeModule.THEMES.win98;
    themeModule.applyColors(colors);
    _applyOpts({ bgPattern: 'none' });
    // save() flips the theme-win98 class via applyWin98 and fires the event
    // this module reacts to (build desktop, DOS palette, retitle).
    themeModule.save('win98', colors, { bgPattern: 'none' });
    themeModule.initThemeUI();
  },
  disable() {
    if (!this.enabled) return;
    let prev = null;
    try { prev = JSON.parse(localStorage.getItem(LS_PREV) || 'null'); } catch (_) {}
    if (!prev || !prev.colors || prev.name === 'win98') {
      prev = { name: 'aurora', colors: themeModule.THEMES.aurora, bgPattern: 'constellations' };
    }
    themeModule.applyColors(prev.colors);
    _applyOpts(prev);
    themeModule.save(prev.name, prev.colors, {
      font: prev.font, density: prev.density, bgPattern: prev.bgPattern,
      bgEffectColor: prev.bgEffectColor, bgEffectIntensity: prev.bgEffectIntensity,
      bgEffectSize: prev.bgEffectSize, frosted: prev.frosted,
    });
    themeModule.initThemeUI();
  },
};
window.aegis98 = api;

// theme.js fires this on every applyWin98() — including redundant re-asserts
// while active (so the DOS palette survives any applyColors overwrite).
window.addEventListener('aegis-win98-theme', (e) => {
  if (e.detail && e.detail.enabled) _activate(); else _deactivate();
});

// Settings → Appearance egg toggle (shared data-egg-key convention).
window.addEventListener('aegis-win98-change', (e) => {
  if (e.detail && e.detail.enabled) api.enable(); else api.disable();
});

// Boot: the early-paint script already put theme-win98 on <html> if the saved
// theme is win98 — build the desktop as soon as the DOM allows.
if (document.documentElement.classList.contains('theme-win98')) {
  _activate();
}

export default api;
