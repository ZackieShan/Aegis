/*
 * enginePanel.js — the Engine context editor.
 *
 * A UI for the thing you used to do with `/engine set <model> <tokens>`: a
 * table of every tunable llama.cpp model with its CURRENT context window, an
 * editable box, and Set / Auto buttons. Backed by the existing endpoints
 * (/api/engine/status + /api/engine/tune); llama-swap hot-reloads the change on
 * the model's next load. Bigger context = more KV-cache VRAM, so raising it is
 * a deliberate choice — Auto picks the largest size that fits your GPU safely.
 */

import { trapFocus } from './modalA11y.js';

let _modal = null;
let _release = null;

function _el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

async function _json(url, opts) {
  const r = await fetch(url, { credentials: 'same-origin', ...opts });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(d.detail || d.error || `Request failed (${r.status})`);
  return d;
}

function _fmt(n) {
  if (!n) return '—';
  return n >= 1024 && n % 1024 === 0 ? (n / 1024) + 'K' : String(n);
}

function _styles() {
  if (document.getElementById('engine-panel-styles')) return;
  const s = _el('style'); s.id = 'engine-panel-styles';
  s.textContent = `
  #engine-panel-modal.hidden { display: none; }
  #engine-panel-modal { position: fixed; inset: 0; z-index: 4200; display: flex; align-items: center;
    justify-content: center; background: rgba(0,0,0,.55); backdrop-filter: blur(2px); }
  .ep-panel { width: min(720px, 95vw); max-height: 88vh; display: flex; flex-direction: column;
    background: var(--panel,#140b20); color: var(--fg,#cbb8ec); border: 1px solid var(--border,#3a2657);
    border-radius: 14px; box-shadow: 0 20px 60px rgba(0,0,0,.5); overflow: hidden; }
  .ep-head { display: flex; align-items: center; gap: 10px; padding: 13px 16px; border-bottom: 1px solid var(--border,#3a2657); }
  .ep-head h2 { margin: 0; font-size: 1rem; flex: 1; }
  .ep-hw { font-size: .74rem; opacity: .7; }
  .ep-close { background: transparent; border: none; color: var(--fg,#cbb8ec); font-size: 1.1rem; cursor: pointer; opacity: .7; }
  .ep-close:hover { opacity: 1; }
  .ep-body { padding: 12px 16px; overflow: auto; }
  .ep-note { font-size: .74rem; opacity: .66; margin-bottom: 12px; line-height: 1.5; }
  .ep-loaded { font-size: .72rem; opacity: .7; margin-bottom: 8px; }
  .ep-row { display: grid; grid-template-columns: 1fr auto auto auto; gap: 10px; align-items: center;
    padding: 9px 0; border-top: 1px solid var(--border,#3a2657); }
  .ep-row:first-of-type { border-top: none; }
  .ep-name { min-width: 0; }
  .ep-name b { font-size: .84rem; word-break: break-all; }
  .ep-sub { font-size: .7rem; opacity: .6; margin-top: 2px; }
  .ep-rec { font-size: .7rem; }
  .ep-rec a { color: var(--accent, var(--red,#b45de0)); cursor: pointer; text-decoration: underline; }
  .ep-num { width: 92px; background: var(--input-bg, var(--panel,#0c0715)); color: var(--fg,#cbb8ec);
    border: 1px solid var(--border,#3a2657); border-radius: 7px; padding: 5px 8px; font-size: .82rem;
    font-variant-numeric: tabular-nums; }
  .ep-btn { background: transparent; color: var(--fg,#cbb8ec); border: 1px solid var(--border,#3a2657);
    border-radius: 7px; padding: 5px 12px; font-size: .78rem; cursor: pointer; }
  .ep-btn.primary { background: var(--accent, var(--red,#b45de0)); color: #fff; border: none; font-weight: 600; }
  .ep-btn:disabled { opacity: .5; cursor: default; }
  .ep-partial { font-size: .7rem; opacity: .6; }
  .ep-foot { display: flex; align-items: center; gap: 10px; padding: 11px 16px; border-top: 1px solid var(--border,#3a2657); }
  .ep-status { flex: 1; font-size: .76rem; opacity: .85; min-height: 1.1em; }
  .ep-status.err { color: #e5484d; opacity: 1; }
  .ep-status.ok { color: #3ecf8e; opacity: 1; }
  `;
  document.head.appendChild(s);
}

function _setStatus(msg, kind) {
  const el = _modal && _modal.querySelector('.ep-status');
  if (el) { el.textContent = msg || ''; el.className = 'ep-status' + (kind ? ' ' + kind : ''); }
}

async function _setContext(model, ctx, rowEl) {
  if (!ctx || ctx < 512) { _setStatus('Enter a context of at least 512 tokens.', 'err'); return; }
  _setStatus(`Setting ${model} to ${_fmt(ctx)}…`);
  try {
    const d = await _json('/api/engine/tune', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model, context: ctx }),
    });
    _setStatus(`✓ ${model}: ${_fmt(d.old_ctx)} → ${_fmt(d.new_ctx)} — applies on the model's next load.`, 'ok');
    const sub = rowEl && rowEl.querySelector('.ep-sub');
    if (sub) sub.textContent = `now ${_fmt(d.new_ctx)} · reloads on next use`;
  } catch (e) {
    _setStatus('Failed: ' + e.message, 'err');
  }
}

async function _render() {
  const body = _modal.querySelector('.ep-body');
  body.replaceChildren(_el('div', 'ep-note', 'Loading engine status…'));
  let d;
  try {
    d = await _json('/api/engine/status');
  } catch (e) {
    body.replaceChildren(_el('div', 'ep-note', 'Engine status unavailable — is the llama-swap engine configured? ' + e.message));
    return;
  }
  const vram = d.vram_mb ? (d.vram_mb / 1024).toFixed(0) + ' GB VRAM' : 'no GPU';
  const ram = d.ram_mb ? ' · ' + (d.ram_mb / 1024).toFixed(0) + ' GB RAM' : '';
  const hw = _modal.querySelector('.ep-hw');
  if (hw) hw.textContent = vram + ram;

  body.replaceChildren();
  body.appendChild(_el('div', 'ep-note',
    'Set each model\'s token window. Bigger = longer memory but more VRAM for the KV cache — '
    + 'Auto picks the largest size that fits your GPU safely. Changes hot-reload on the model\'s next load.'));
  if ((d.running || []).length) {
    body.appendChild(_el('div', 'ep-loaded', 'Loaded in VRAM now: ' + d.running.join(', ')
      + ' — a context change takes effect after it reloads.'));
  }

  const models = d.models || [];
  if (!models.length) {
    body.appendChild(_el('div', 'ep-note', 'No tunable models in the llama-swap config.'));
    return;
  }
  models.forEach(m => {
    const row = _el('div', 'ep-row');
    const trained = m.n_ctx_train || m.trained_ctx;
    const name = _el('div', 'ep-name');
    name.appendChild(_el('b', '', m.model));
    name.appendChild(_el('div', 'ep-sub', `now ${_fmt(m.current_ctx)}`
      + (trained ? ` · trained for ${_fmt(trained)}` : '')));
    row.appendChild(name);

    // Recommendation (only for full-offload models the tuner can size)
    const rec = _el('div', 'ep-rec');
    if (m.recommended && m.recommended !== m.current_ctx) {
      const a = _el('a', '', `use rec ${_fmt(m.recommended)}`);
      a.title = m.note || 'recommended for your GPU';
      a.addEventListener('click', () => { input.value = String(m.recommended); });
      rec.appendChild(a);
    } else if (m.recommended) {
      rec.appendChild(_el('span', 'ep-partial', '✓ optimal'));
    } else if (m.cpu_moe) {
      const span = _el('span', 'ep-partial', 'experts in RAM — room to grow');
      span.title = m.reason || '';
      rec.appendChild(span);
    } else {
      const span = _el('span', 'ep-partial', 'set manually');
      span.title = m.reason || '';
      rec.appendChild(span);
    }
    row.appendChild(rec);

    const input = document.createElement('input');
    input.type = 'number'; input.className = 'ep-num';
    input.min = '512'; input.max = '1048576'; input.step = '4096';
    input.value = String(m.current_ctx);
    input.title = 'Context window in tokens (512 – 1048576)';
    row.appendChild(input);

    const set = _el('button', 'ep-btn primary', 'Set');
    set.type = 'button';
    set.addEventListener('click', () => {
      const v = Math.max(512, Math.min(1048576, parseInt(input.value, 10) || 0));
      input.value = String(v);
      _setContext(m.model, v, row);
    });
    row.appendChild(set);
    body.appendChild(row);
  });
}

function _close() {
  if (_release) { try { _release(); } catch (_) {} _release = null; }
  if (_modal) { _modal.classList.add('hidden'); }
}

function _build() {
  if (_modal) return;
  _styles();
  _modal = _el('div', 'hidden'); _modal.id = 'engine-panel-modal';
  _modal.innerHTML = `
    <div class="ep-panel">
      <div class="ep-head">
        <h2>Engine — context windows</h2>
        <span class="ep-hw"></span>
        <button class="ep-close" aria-label="Close">×</button>
      </div>
      <div class="ep-body"></div>
      <div class="ep-foot">
        <span class="ep-status"></span>
        <button class="ep-btn" id="ep-autoall" type="button" title="Set every model to the largest size that fits your GPU">Auto-tune all</button>
        <button class="ep-btn" id="ep-free" type="button" title="Unload engine models to free VRAM now">Free VRAM</button>
      </div>
    </div>`;
  document.body.appendChild(_modal);
  _modal.addEventListener('mousedown', (e) => { if (e.target === _modal) _close(); });
  _modal.querySelector('.ep-close').addEventListener('click', _close);
  _modal.querySelector('#ep-autoall').addEventListener('click', async (e) => {
    e.target.disabled = true;
    _setStatus('Auto-tuning every model to your GPU…');
    try {
      const d = await _json('/api/engine/tune', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
      });
      const n = (d.applied || []).filter(a => a.new_ctx).length;
      _setStatus(`✓ Auto-tuned ${n} model${n === 1 ? '' : 's'}.`, 'ok');
      await _render();
    } catch (err) { _setStatus('Auto-tune failed: ' + err.message, 'err'); }
    e.target.disabled = false;
  });
  _modal.querySelector('#ep-free').addEventListener('click', async (e) => {
    e.target.disabled = true;
    _setStatus('Freeing engine VRAM…');
    try {
      const d = await _json('/api/engine/unload', { method: 'POST' });
      const what = (d.unloaded || []).length ? d.unloaded.join(', ') : 'nothing was loaded';
      _setStatus(`✓ VRAM freed (${what}). Models reload on next use.`, 'ok');
    } catch (err) { _setStatus('Unload failed: ' + err.message, 'err'); }
    e.target.disabled = false;
  });
}

export function open() {
  _build();
  _modal.classList.remove('hidden');
  _setStatus('');
  _release = trapFocus(_modal.querySelector('.ep-panel'), { onEscape: _close });
  _render();
}

const api = { open };
window.enginePanel = api;
export default api;
