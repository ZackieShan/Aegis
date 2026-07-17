/*
 * modelSettings.js — per-model chat generation settings, remembered per model.
 *
 * A small popover off the model picker lets you set, for the CURRENT model:
 *   - Thinking: Auto (engine default) / On / Off  — the fix for reasoning
 *     leaking into replies, now a per-message toggle (llama.cpp honors
 *     chat_template_kwargs.enable_thinking per request).
 *   - Temperature: default, or a custom value.
 *   - Max response length (tokens): default, or a custom cap.
 *
 * Settings persist in localStorage keyed by model id and ride along with each
 * chat send (chat.js reads sendFields(); the backend applies them for that
 * turn only). Thinking/temperature apply to local models; the backend ignores
 * a thinking override on models that don't support it.
 */

const LS_KEY = 'aegis-model-settings';

function _all() {
  try { return JSON.parse(localStorage.getItem(LS_KEY) || '{}') || {}; }
  catch (_) { return {}; }
}
function _save(map) {
  try { localStorage.setItem(LS_KEY, JSON.stringify(map)); } catch (_) {}
}
function _get(modelId) {
  if (!modelId) return {};
  const m = _all();
  return (m && typeof m[modelId] === 'object' && m[modelId]) || {};
}
function _set(modelId, cfg) {
  if (!modelId) return;
  const m = _all();
  // Drop empty configs so "all defaults" doesn't linger as an override.
  if (!cfg || (cfg.think == null && cfg.temperature == null && cfg.maxTokens == null)) {
    delete m[modelId];
  } else {
    m[modelId] = cfg;
  }
  _save(m);
}

function _curModel() { return window.__aegisCurrentModel || ''; }

// Fields to append to a chat send's FormData for `modelId` (only overrides).
export function sendFields(modelId) {
  const c = _get(modelId);
  const out = {};
  if (c.think === true) out.think = 'on';
  else if (c.think === false) out.think = 'off';
  if (typeof c.temperature === 'number') out.gen_temperature = c.temperature;
  if (typeof c.maxTokens === 'number') out.gen_max_tokens = c.maxTokens;
  return out;
}

function _hasOverride(modelId) {
  const c = _get(modelId);
  return c.think != null || c.temperature != null || c.maxTokens != null;
}

function _el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

function _styles() {
  if (document.getElementById('model-settings-styles')) return;
  const s = _el('style'); s.id = 'model-settings-styles';
  s.textContent = `
  #model-settings-btn { background: transparent; border: none; color: var(--fg,#cbb8ec); cursor: pointer;
    padding: 3px 5px; border-radius: 6px; opacity: .68; display: inline-flex; align-items: center; position: relative; }
  #model-settings-btn:hover { opacity: 1; background: rgba(255,255,255,.06); }
  #model-settings-btn.has-override { opacity: 1; color: var(--accent, var(--red,#b45de0)); }
  #model-settings-btn .ms-dot { position: absolute; top: 1px; right: 1px; width: 6px; height: 6px; border-radius: 50%;
    background: var(--accent, var(--red,#b45de0)); display: none; }
  #model-settings-btn.has-override .ms-dot { display: block; }
  #model-settings-pop.hidden { display: none; }
  #model-settings-pop { position: absolute; z-index: 4300; width: 288px; padding: 12px 13px;
    background: var(--panel,#140b20); color: var(--fg,#cbb8ec); border: 1px solid var(--border,#3a2657);
    border-radius: 12px; box-shadow: 0 14px 40px rgba(0,0,0,.5); font-size: .82rem; }
  #model-settings-pop h4 { margin: 0 0 2px; font-size: .82rem; }
  .ms-model { font-size: .72rem; opacity: .6; margin-bottom: 10px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .ms-row { margin-bottom: 12px; }
  .ms-label { display: flex; justify-content: space-between; align-items: center; font-size: .76rem; margin-bottom: 5px; }
  .ms-label .ms-val { opacity: .7; font-variant-numeric: tabular-nums; }
  .ms-seg { display: flex; border: 1px solid var(--border,#3a2657); border-radius: 8px; overflow: hidden; }
  .ms-seg button { flex: 1; background: transparent; color: var(--fg,#cbb8ec); border: none; padding: 6px 4px;
    font-size: .76rem; cursor: pointer; }
  .ms-seg button + button { border-left: 1px solid var(--border,#3a2657); }
  .ms-seg button.active { background: var(--accent, var(--red,#b45de0)); color: #fff; font-weight: 600; }
  .ms-slider { display: flex; align-items: center; gap: 8px; }
  .ms-slider input[type=range] { flex: 1; accent-color: var(--accent, var(--red,#b45de0)); }
  .ms-slider input[type=number] { width: 74px; background: var(--input-bg, var(--panel,#0c0715)); color: var(--fg,#cbb8ec);
    border: 1px solid var(--border,#3a2657); border-radius: 6px; padding: 4px 6px; font-size: .78rem; }
  .ms-hint { font-size: .68rem; opacity: .5; margin-top: 3px; }
  .ms-foot { display: flex; justify-content: space-between; align-items: center; margin-top: 4px; }
  .ms-reset { background: transparent; border: none; color: var(--fg,#cbb8ec); opacity: .6; cursor: pointer;
    font-size: .72rem; text-decoration: underline; padding: 0; }
  .ms-reset:hover { opacity: 1; }
  `;
  document.head.appendChild(s);
}

let _pop = null;

function _closePop() {
  if (_pop) { _pop.remove(); _pop = null; }
  document.removeEventListener('mousedown', _onOutside, true);
  document.removeEventListener('keydown', _onKey, true);
}
function _onOutside(e) {
  if (_pop && !_pop.contains(e.target) && e.target.id !== 'model-settings-btn'
      && !e.target.closest('#model-settings-btn')) _closePop();
}
function _onKey(e) { if (e.key === 'Escape') _closePop(); }

function _seg(row, label, valueText, options, current, onPick) {
  const l = _el('div', 'ms-label');
  l.appendChild(_el('span', '', label));
  if (valueText) l.appendChild(_el('span', 'ms-val', valueText));
  row.appendChild(l);
  const seg = _el('div', 'ms-seg');
  options.forEach(([val, lbl]) => {
    const b = _el('button', current === val ? 'active' : '', lbl);
    b.type = 'button';
    b.addEventListener('click', () => onPick(val));
    seg.appendChild(b);
  });
  row.appendChild(seg);
}

function _renderPop() {
  const modelId = _curModel();
  const cfg = { ..._get(modelId) };
  _pop.replaceChildren();
  _pop.appendChild(_el('h4', '', 'Model settings'));
  _pop.appendChild(_el('div', 'ms-model', modelId || 'No model selected'));

  const commit = () => { _set(modelId, cfg); refreshButton(); _renderPop(); };

  // Thinking
  const r1 = _el('div', 'ms-row');
  _seg(r1, 'Thinking', '', [['auto', 'Auto'], ['on', 'On'], ['off', 'Off']],
    cfg.think === true ? 'on' : cfg.think === false ? 'off' : 'auto',
    (v) => { cfg.think = v === 'on' ? true : v === 'off' ? false : null; commit(); });
  r1.appendChild(_el('div', 'ms-hint', 'Off stops reasoning models from showing their thought process. Auto uses the model default.'));
  _pop.appendChild(r1);

  // Temperature
  const r2 = _el('div', 'ms-row');
  const hasTemp = typeof cfg.temperature === 'number';
  _seg(r2, 'Temperature', hasTemp ? cfg.temperature.toFixed(2) : 'default',
    [['default', 'Default'], ['custom', 'Custom']],
    hasTemp ? 'custom' : 'default',
    (v) => { cfg.temperature = v === 'custom' ? (hasTemp ? cfg.temperature : 0.7) : null; commit(); });
  if (hasTemp) {
    const sl = _el('div', 'ms-slider');
    const range = document.createElement('input');
    range.type = 'range'; range.min = '0'; range.max = '2'; range.step = '0.05';
    range.value = String(cfg.temperature);
    range.addEventListener('input', () => {
      cfg.temperature = Number(range.value);
      const valEl = r2.querySelector('.ms-val');
      if (valEl) valEl.textContent = cfg.temperature.toFixed(2);
    });
    range.addEventListener('change', commit);
    sl.appendChild(range);
    r2.appendChild(sl);
    r2.appendChild(_el('div', 'ms-hint', 'Lower = focused and deterministic; higher = more creative and varied.'));
  }
  _pop.appendChild(r2);

  // Max response length
  const r3 = _el('div', 'ms-row');
  const hasMax = typeof cfg.maxTokens === 'number';
  _seg(r3, 'Max response length', hasMax ? `${cfg.maxTokens} tok` : 'default',
    [['default', 'Default'], ['custom', 'Custom']],
    hasMax ? 'custom' : 'default',
    (v) => { cfg.maxTokens = v === 'custom' ? (hasMax ? cfg.maxTokens : 2048) : null; commit(); });
  if (hasMax) {
    const sl = _el('div', 'ms-slider');
    const num = document.createElement('input');
    num.type = 'number'; num.min = '64'; num.max = '32768'; num.step = '128';
    num.value = String(cfg.maxTokens);
    num.addEventListener('change', () => {
      let v = parseInt(num.value, 10);
      if (isNaN(v)) v = 2048;
      cfg.maxTokens = Math.max(64, Math.min(32768, v));
      num.value = String(cfg.maxTokens);
      commit();
    });
    sl.appendChild(num);
    sl.appendChild(_el('span', 'ms-hint', 'tokens'));
    r3.appendChild(sl);
  }
  _pop.appendChild(r3);

  // Footer
  const foot = _el('div', 'ms-foot');
  const reset = _el('button', 'ms-reset', 'Reset to defaults');
  reset.type = 'button';
  reset.addEventListener('click', () => { _set(modelId, null); refreshButton(); _renderPop(); });
  foot.appendChild(reset);
  _pop.appendChild(foot);
}

function _openPop(btn) {
  _styles();
  _pop = _el('div', ''); _pop.id = 'model-settings-pop';
  document.body.appendChild(_pop);
  _renderPop();
  // Anchor above the button (the composer sits at the bottom of the screen).
  const r = btn.getBoundingClientRect();
  const w = 288;
  let left = Math.min(r.left, window.innerWidth - w - 10);
  left = Math.max(10, left);
  _pop.style.left = left + 'px';
  _pop.style.bottom = (window.innerHeight - r.top + 8) + 'px';
  setTimeout(() => {
    document.addEventListener('mousedown', _onOutside, true);
    document.addEventListener('keydown', _onKey, true);
  }, 0);
}

export function refreshButton() {
  const btn = document.getElementById('model-settings-btn');
  if (!btn) return;
  btn.classList.toggle('has-override', _hasOverride(_curModel()));
}

// Called by modelPicker when the current model changes.
export function onModelChanged() {
  refreshButton();
  if (_pop) _renderPop();
}

export function initModelSettings() {
  const wrap = document.getElementById('model-picker-wrap');
  const picker = document.getElementById('model-picker-btn');
  if (!wrap || !picker || document.getElementById('model-settings-btn')) return;
  _styles();
  const btn = _el('button', '', '');
  btn.id = 'model-settings-btn';
  btn.type = 'button';
  btn.title = 'Model settings (thinking, temperature, response length)';
  btn.setAttribute('aria-label', 'Model settings');
  btn.innerHTML = `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="4" y1="21" x2="4" y2="14"/><line x1="4" y1="10" x2="4" y2="3"/><line x1="12" y1="21" x2="12" y2="12"/><line x1="12" y1="8" x2="12" y2="3"/><line x1="20" y1="21" x2="20" y2="16"/><line x1="20" y1="12" x2="20" y2="3"/><line x1="1" y1="14" x2="7" y2="14"/><line x1="9" y1="8" x2="15" y2="8"/><line x1="17" y1="16" x2="23" y2="16"/></svg><span class="ms-dot"></span>`;
  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    if (_pop) { _closePop(); return; }
    _openPop(btn);
  });
  picker.insertAdjacentElement('afterend', btn);
  refreshButton();
}

const api = { initModelSettings, sendFields, refreshButton, onModelChanged };
window.modelSettings = api;

// Self-init: the model picker markup is static in index.html, so the button
// can mount as soon as the DOM is ready. modelPicker publishes the current
// model + calls onModelChanged(); this just needs the wrap to exist.
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', initModelSettings, { once: true });
} else {
  initModelSettings();
}

export default api;
