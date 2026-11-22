// static/js/codeMode.js
//
// Code mode: the composer's third mode (Agent | Chat | Code) and its machine
// picker. In Code mode the assistant works as a coding agent, and the pill
// next to the attachments says which computer it is working on — this server
// unless another registered machine is picked for the chat.
//
// For everything else in the app Code mode IS agent mode (the stored mode
// stays 'agent', the Agent button keeps its `active` class), so the many
// places that ask "agent or chat?" need no change. The extra bit is `code` in
// the saved toggle state, shown through `mode-third` on the toggle.
//
// The machine is a per-chat choice. A new chat always starts on this server;
// the server remembers what a chat was pointed at (GET /api/workbench/machines),
// so coming back to a chat that was working on another PC shows that PC.

import Storage from './storage.js';

const API_BASE = window.location.origin;
const LOCAL = 'local';
const _choice = new Map();      // chat id -> machine name; '' = the chat that has no id yet
let _machines = null;
let _loading = null;
let _available = false;         // this account may use Code mode (administrators)
let _currentSession = () => '';
let _lastSession = null;
let _watch = null;

const $ = (id) => document.getElementById(id);

export function isOn() {
  const st = Storage.loadToggleState();
  return _available && st.mode === 'agent' && st.code === true;
}

function sessionId() {
  try { return _currentSession() || ''; } catch (_) { return ''; }
}

export function hostFor(id) {
  const key = id === undefined ? sessionId() : (id || '');
  if (_choice.has(key)) return _choice.get(key);
  return _choice.get('') || LOCAL;
}

/** What a message sent in Code mode carries. Empty when Code mode is off. */
export function sendFields(id) {
  if (!isOn()) return {};
  const key = id || sessionId();
  // A machine picked before the chat existed belongs to the chat it became.
  if (key && !_choice.has(key) && _choice.has('')) {
    _choice.set(key, _choice.get(''));
    _choice.delete('');
  }
  return { code_mode: 'true', code_host: hostFor(key) };
}

function labelFor(name) {
  if (!name || name === LOCAL) return 'This server';
  const found = (_machines || []).find((m) => m.name === name);
  return found ? found.label : name;
}

async function loadMachines(force = false) {
  if (_machines && !force) return _machines;
  if (_loading) return _loading;
  _loading = (async () => {
    try {
      const id = sessionId();
      const res = await fetch(`${API_BASE}/api/workbench/machines?session_id=${encodeURIComponent(id)}`, { credentials: 'same-origin' });
      if (!res.ok) throw new Error(String(res.status));
      const data = await res.json();
      _machines = Array.isArray(data.machines) ? data.machines : [];
      // What the server has for this chat wins over a guess, never over a pick made here.
      if (id && !_choice.has(id) && data.current && data.current.host && data.current.host !== LOCAL) {
        _choice.set(id, data.current.host);
      }
    } catch (_) {
      _machines = _machines || [{ name: LOCAL, label: 'This server', os: 'linux', shell: 'bash', builtin: true }];
    } finally {
      _loading = null;
    }
    return _machines;
  })();
  return _loading;
}

function closeMenu() {
  const menu = $('code-host-menu');
  const btn = $('code-host-btn');
  if (menu) menu.hidden = true;
  if (btn) btn.setAttribute('aria-expanded', 'false');
  document.removeEventListener('mousedown', onOutside, true);
  document.removeEventListener('keydown', onKey, true);
}

function onOutside(event) {
  const wrap = $('code-host-wrap');
  const menu = $('code-host-menu');
  if (wrap && wrap.contains(event.target)) return;
  if (menu && menu.contains(event.target)) return;
  closeMenu();
}

function onKey(event) {
  if (event.key === 'Escape') { closeMenu(); const btn = $('code-host-btn'); if (btn) btn.focus(); }
}

function pick(name) {
  _choice.set(sessionId(), name || LOCAL);
  closeMenu();
  syncPill();
}

function renderMenu() {
  const menu = $('code-host-menu');
  if (!menu) return;
  const current = hostFor();
  menu.textContent = '';
  const head = document.createElement('div');
  head.className = 'code-host-head';
  head.textContent = 'Work on';
  menu.appendChild(head);
  (_machines || []).forEach((m) => {
    const item = document.createElement('button');
    item.type = 'button';
    item.className = 'code-host-item' + (m.name === current ? ' active' : '');
    item.setAttribute('role', 'menuitemradio');
    item.setAttribute('aria-checked', String(m.name === current));
    const name = document.createElement('span');
    name.className = 'code-host-item-name';
    name.textContent = m.label;
    const about = document.createElement('span');
    about.className = 'code-host-item-about';
    about.textContent = m.builtin ? 'where Aegis runs' : [m.os === 'windows' ? 'Windows' : (m.os === 'macos' ? 'macOS' : 'Linux'), m.address].filter(Boolean).join(' · ');
    item.append(name, about);
    item.addEventListener('click', () => pick(m.name));
    menu.appendChild(item);
  });
  if ((_machines || []).length <= 1) {
    const hint = document.createElement('div');
    hint.className = 'code-host-hint';
    hint.textContent = 'No other machines yet. Ask Aegis to add one (for example “add my desktop at 192.168.1.20 as a host”), or type /hosts.';
    menu.appendChild(hint);
  }
}

async function openMenu() {
  const menu = $('code-host-menu');
  const btn = $('code-host-btn');
  if (!menu || !btn) return;
  await loadMachines(true);
  renderMenu();
  // The composer clips what is inside it, so the menu lives on <body> and is
  // placed from the button's position: above it, kept on screen.
  if (menu.parentElement !== document.body) document.body.appendChild(menu);
  menu.hidden = false;
  const at = btn.getBoundingClientRect();
  const width = menu.offsetWidth || 230;
  menu.style.left = `${Math.max(8, Math.min(at.left, window.innerWidth - width - 8))}px`;
  menu.style.bottom = `${Math.max(8, window.innerHeight - at.top + 8)}px`;
  btn.setAttribute('aria-expanded', 'true');
  document.addEventListener('mousedown', onOutside, true);
  document.addEventListener('keydown', onKey, true);
}

export function syncPill() {
  const wrap = $('code-host-wrap');
  const name = $('code-host-name');
  const btn = $('code-host-btn');
  if (!wrap) return;
  const on = isOn();
  wrap.style.display = on ? '' : 'none';
  if (!on) { closeMenu(); return; }
  const host = hostFor();
  if (name) name.textContent = labelFor(host);
  if (btn) {
    btn.classList.toggle('code-host-remote', host !== LOCAL);
    btn.title = host === LOCAL
      ? 'Code mode works on this server. Click to pick another machine.'
      : `Code mode works on ${labelFor(host)}: the shell and file tools act there. Click to change.`;
  }
}

/**
 * Pick the machine for the open chat by name (the /code command).
 * Resolves to the machine's label, or throws with the names that exist.
 */
export async function setHost(name) {
  const wanted = String(name || '').trim().toLowerCase();
  const machines = await loadMachines(true);
  const found = !wanted || ['local', 'host', 'server', 'this', 'this server'].includes(wanted)
    ? machines.find((m) => m.builtin)
    : machines.find((m) => m.name.toLowerCase() === wanted || m.label.toLowerCase() === wanted);
  if (!found) {
    throw new Error(`No machine called “${name}”. Known: ${machines.map((m) => (m.builtin ? 'local' : m.name)).join(', ')}.`);
  }
  _choice.set(sessionId(), found.name);
  syncPill();
  return found.label;
}

export function isAvailable() { return _available; }

/** Reflect the mode on the toggle. `on` = Code is the selected mode. */
export function applyMode(on) {
  const toggle = document.querySelector('.chat-input-right .mode-toggle');
  const codeBtn = $('mode-code-btn');
  if (toggle) toggle.classList.toggle('mode-third', !!on && _available);
  if (codeBtn) {
    codeBtn.classList.toggle('active', !!on && _available);
    codeBtn.setAttribute('aria-pressed', String(!!on && _available));
  }
  if (on && _available) loadMachines().then(syncPill);
  syncPill();
  watchSession(!!on && _available);
}

// The pill follows the chat that is open. There is no event for "another chat
// was opened", so look while Code mode is on (a string compare, nothing more).
function watchSession(on) {
  if (!on) { if (_watch) { clearInterval(_watch); _watch = null; } return; }
  if (_watch) return;
  _lastSession = sessionId();
  _watch = setInterval(() => {
    const now = sessionId();
    if (now === _lastSession) return;
    const cameFromNew = _lastSession === '' && _choice.has('');
    _lastSession = now;
    if (cameFromNew && now && !_choice.has(now)) {       // the new chat just got its id
      _choice.set(now, _choice.get(''));
      _choice.delete('');
      syncPill();
      return;
    }
    _choice.delete('');                                   // a pick for a chat that was never started
    if (now && !_choice.has(now)) loadMachines(true).then(syncPill); else syncPill();
  }, 1200);
}

/**
 * Wire the Code button and the picker.
 *   setMode(uiMode)       the toggle's own setter ('agent' | 'chat' | 'code')
 *   currentSession()      the id of the chat that is open ('' for a new one)
 */
export function initCodeMode({ setMode, currentSession }) {
  _currentSession = currentSession || _currentSession;
  const codeBtn = $('mode-code-btn');
  const hostBtn = $('code-host-btn');
  if (codeBtn) codeBtn.addEventListener('click', () => setMode('code'));
  if (hostBtn) {
    hostBtn.addEventListener('click', () => {
      const menu = $('code-host-menu');
      if (menu && !menu.hidden) closeMenu(); else openMenu();
    });
  }
  // Leaving agent mode by any route (a tool, an error fallback, the tour)
  // leaves Code mode too: those paths flip the buttons without the setter.
  const chatBtn = $('mode-chat-btn');
  if (chatBtn && typeof MutationObserver === 'function') {
    new MutationObserver(() => {
      if (!chatBtn.classList.contains('active')) return;
      const st = Storage.loadToggleState();
      if (st.code) { st.code = false; Storage.saveToggleState(st); }
      applyMode(false);
    }).observe(chatBtn, { attributes: true, attributeFilter: ['class'] });
  }
}

/** Called once it is known whether this account may use Code mode. */
export function setAvailable(yes) {
  _available = !!yes;
  const codeBtn = $('mode-code-btn');
  const toggle = document.querySelector('.chat-input-right .mode-toggle');
  if (codeBtn) codeBtn.style.display = _available ? '' : 'none';
  if (toggle) toggle.classList.toggle('mode-toggle-three', _available);
  const st = Storage.loadToggleState();
  if (!_available && st.code) { st.code = false; Storage.saveToggleState(st); }
  applyMode(_available && st.mode === 'agent' && st.code === true);
}

const codeMode = { isOn, isAvailable, hostFor, setHost, sendFields, applyMode, syncPill, initCodeMode, setAvailable };
try { window.aegisCodeMode = codeMode; } catch (_) { /* not in a browser */ }
export default codeMode;
