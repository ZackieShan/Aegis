/*
 * control-center/index.js — the Control Center panel.
 *
 * One dashboard for every Aegis capability: live status + a one-click "try it".
 * Fetches /api/control-center (see src/control_center.py) and renders grouped
 * cards. Opened by #rail-control (wired here) or the /control slash command.
 *
 * "Try it" actions:
 *   command / chat  → drop the text in the composer and send it (slash commands
 *                     and plain prompts both dispatch through the normal send).
 *   panel           → click the target rail/tool button.
 */

import { trapFocus } from '../modalA11y.js';

const DOT = { ok: '#3ecf8e', warn: '#f5a623', off: '#8a7fa8', error: '#e5484d' };
const DOT_LABEL = { ok: 'working', warn: 'needs a step', off: 'not set up', error: 'error' };

let _built = false;
let _releaseFocus = null;

function _el(tag, cls, html) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (html != null) e.innerHTML = html;
  return e;
}

function _modal() { return document.getElementById('control-center-modal'); }

function _styles() {
  if (document.getElementById('control-center-styles')) return;
  const s = _el('style');
  s.id = 'control-center-styles';
  s.textContent = `
  #control-center-modal.hidden { display: none; }
  #control-center-modal {
    position: fixed; inset: 0; z-index: 4000; display: flex;
    align-items: center; justify-content: center;
    background: rgba(0,0,0,.55); backdrop-filter: blur(2px);
  }
  .cc-panel {
    width: min(920px, 94vw); max-height: 88vh; display: flex; flex-direction: column;
    background: var(--panel, #120a1c); color: var(--fg, #cbb8ec);
    border: 1px solid var(--border, #3a2657); border-radius: 14px;
    box-shadow: 0 20px 60px rgba(0,0,0,.5); overflow: hidden;
  }
  .cc-head { display: flex; align-items: center; gap: 12px; padding: 16px 20px;
    border-bottom: 1px solid var(--border, #3a2657); }
  .cc-head h2 { margin: 0; font-size: 1.1rem; font-weight: 600; }
  .cc-summary { font-size: .8rem; opacity: .7; }
  .cc-head .cc-spacer { flex: 1; }
  .cc-btn { background: transparent; color: var(--fg,#cbb8ec); border: 1px solid var(--border,#3a2657);
    border-radius: 8px; padding: 5px 10px; font-size: .78rem; cursor: pointer; }
  .cc-btn:hover { border-color: var(--red,#b45de0); }
  .cc-close { font-size: 1.3rem; line-height: 1; padding: 2px 8px; }
  .cc-body { padding: 8px 20px 20px; overflow-y: auto; }
  .cc-group-title { font-size: .72rem; text-transform: uppercase; letter-spacing: .08em;
    opacity: .55; margin: 18px 0 8px; }
  .cc-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 10px; }
  .cc-card { border: 1px solid var(--border,#3a2657); border-radius: 10px; padding: 11px 13px;
    display: flex; flex-direction: column; gap: 6px; background: rgba(255,255,255,.02); }
  .cc-card-top { display: flex; align-items: center; gap: 8px; }
  .cc-dot { width: 9px; height: 9px; border-radius: 50%; flex-shrink: 0; }
  .cc-name { font-size: .86rem; font-weight: 600; }
  .cc-detail { font-size: .74rem; opacity: .72; line-height: 1.35; min-height: 1.9em; }
  .cc-card-actions { display: flex; gap: 6px; margin-top: 2px; }
  .cc-try { background: var(--red,#b45de0); color: #fff; border: none; border-radius: 7px;
    padding: 4px 11px; font-size: .74rem; cursor: pointer; font-weight: 600; }
  .cc-try:hover { filter: brightness(1.08); }
  .cc-try.secondary { background: transparent; color: var(--fg,#cbb8ec); border: 1px solid var(--border,#3a2657); }
  .cc-loading { padding: 40px; text-align: center; opacity: .6; }
  `;
  document.head.appendChild(s);
}

function _build() {
  if (_built) return;
  _styles();
  const modal = _el('div', 'hidden');
  modal.id = 'control-center-modal';
  modal.innerHTML = `
    <div class="cc-panel">
      <div class="cc-head">
        <h2>Control Center</h2>
        <span class="cc-summary" id="cc-summary"></span>
        <span class="cc-spacer"></span>
        <button class="cc-btn" id="cc-refresh" title="Refresh status">↻ Refresh</button>
        <button class="cc-btn cc-close" id="cc-close" aria-label="Close">×</button>
      </div>
      <div class="cc-body" id="cc-body"><div class="cc-loading">Loading capabilities…</div></div>
    </div>`;
  document.body.appendChild(modal);
  modal.addEventListener('mousedown', (e) => { if (e.target === modal) close(); });
  modal.querySelector('#cc-close').addEventListener('click', close);
  modal.querySelector('#cc-refresh').addEventListener('click', _load);
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !modal.classList.contains('hidden')) close();
  });
  _built = true;
}

function _sendToChat(text) {
  const input = document.getElementById('message');
  const form = document.getElementById('chat-form');
  if (!input || !form) return false;
  input.value = text;
  input.dispatchEvent(new Event('input', { bubbles: true }));
  try { form.requestSubmit(); } catch { form.dispatchEvent(new Event('submit', { cancelable: true, bubbles: true })); }
  return true;
}

function _act(action) {
  if (!action || action.type === 'none') return;
  if (action.type === 'command' || action.type === 'chat') {
    close();
    setTimeout(() => _sendToChat(action.value), 60);
  } else if (action.type === 'panel') {
    close();
    setTimeout(() => document.getElementById(action.value)?.click(), 60);
  } else if (action.type === 'settings') {
    close();
    setTimeout(() => document.getElementById('rail-settings')?.click(), 60);
  }
}

// Direct in-panel action: call the endpoint, show the outcome on the card,
// then refresh so statuses (e.g. "loaded now") update. No chat round-trip —
// this is the "tuning belongs in the UI" surface.
async function _apiAction(action, btn, card) {
  const detail = card.querySelector('.cc-detail');
  const orig = btn.textContent;
  btn.disabled = true;
  btn.textContent = '…';
  try {
    const r = await fetch(action.value, { method: action.method || 'POST', credentials: 'same-origin' });
    const d = await r.json().catch(() => ({}));
    let msg;
    if (r.ok && d.ok !== false) {
      if (Array.isArray(d.applied)) {
        const parts = d.applied.map(a => a.new_ctx ? `${a.model} → ${Math.round(a.new_ctx / 1024)}K`
          : a.unchanged ? `${a.model} ✓` : `${a.model} skipped`);
        msg = '✓ ' + (parts.join(' · ') || 'nothing to tune');
      } else if (Array.isArray(d.unloaded)) {
        msg = d.unloaded.length ? `✓ freed: ${d.unloaded.join(', ')}` : '✓ nothing was loaded';
      } else {
        msg = '✓ ' + (d.note || 'done');
      }
    } else {
      msg = '✗ ' + (d.detail || d.error || ('HTTP ' + r.status));
    }
    if (detail) detail.textContent = msg;
    setTimeout(_load, 2500);
  } catch (e) {
    if (detail) detail.textContent = '✗ ' + e.message;
  } finally {
    btn.disabled = false;
    btn.textContent = orig;
  }
}

function _card(item) {
  const card = _el('div', 'cc-card');
  const top = _el('div', 'cc-card-top');
  const dot = _el('span', 'cc-dot');
  dot.style.background = DOT[item.status] || DOT.off;
  dot.title = DOT_LABEL[item.status] || '';
  top.appendChild(dot);
  top.appendChild(_el('span', 'cc-name', _esc(item.name)));
  card.appendChild(top);
  card.appendChild(_el('div', 'cc-detail', _esc(item.detail || '')));
  const hasMain = item.action && item.action.type !== 'none';
  const extras = Array.isArray(item.extra_actions) ? item.extra_actions : [];
  if (hasMain || extras.length) {
    const actions = _el('div', 'cc-card-actions');
    if (hasMain) {
      const label = (item.action.type === 'chat') ? 'Try it'
        : (item.action.type === 'panel' || item.action.type === 'settings') ? 'Open'
        : 'Run';
      const btn = _el('button', 'cc-try' + (item.status === 'off' ? ' secondary' : ''), label);
      btn.addEventListener('click', () => _act(item.action));
      actions.appendChild(btn);
    }
    extras.forEach(a => {
      const b = _el('button', 'cc-try secondary', _esc(a.label || 'Run'));
      if (a.type === 'api') b.addEventListener('click', () => _apiAction(a, b, card));
      else b.addEventListener('click', () => _act(a));
      actions.appendChild(b);
    });
    card.appendChild(actions);
  }
  return card;
}

function _esc(s) { return String(s == null ? '' : s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])); }

function _render(data) {
  const body = _modal().querySelector('#cc-body');
  body.innerHTML = '';
  const sum = data.summary || {};
  _modal().querySelector('#cc-summary').textContent =
    `${sum.ok || 0}/${sum.total || 0} working` + (sum.needs_attention ? ` · ${sum.needs_attention} need a step` : '');
  (data.groups || []).forEach(g => {
    body.appendChild(_el('div', 'cc-group-title', _esc(g.title)));
    const grid = _el('div', 'cc-grid');
    (g.items || []).forEach(i => grid.appendChild(_card(i)));
    body.appendChild(grid);
  });
  if (!(data.groups || []).length) {
    body.innerHTML = '<div class="cc-loading">No capabilities reported.</div>';
  }
}

async function _load() {
  const body = _modal().querySelector('#cc-body');
  body.innerHTML = '<div class="cc-loading">Loading capabilities…</div>';
  try {
    const r = await fetch('/api/control-center', { credentials: 'same-origin' });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    _render(await r.json());
  } catch (e) {
    body.innerHTML = `<div class="cc-loading">Couldn't load: ${_esc(e.message)}</div>`;
  }
}

export async function open() {
  _build();
  _modal().classList.remove('hidden');
  const panel = _modal().querySelector('.cc-panel');
  if (_releaseFocus) _releaseFocus();
  _releaseFocus = trapFocus(panel, { onEscape: close });
  await _load();
}
export function close() {
  const m = _modal();
  if (m) m.classList.add('hidden');
  if (_releaseFocus) { _releaseFocus(); _releaseFocus = null; }
}
export function isOpen() { const m = _modal(); return !!m && !m.classList.contains('hidden'); }
export function toggle() { isOpen() ? close() : open(); }

const controlCenterModule = { open, close, isOpen, toggle };
export default controlCenterModule;
window.controlCenterModule = controlCenterModule;

function _wire() {
  const btn = document.getElementById('rail-control');
  if (btn && !btn._ccWired) { btn._ccWired = true; btn.addEventListener('click', toggle); }
}
if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', _wire);
else _wire();
