/*
 * canvas/index.js — the Code Canvas.
 *
 * An artifact-style editor: code appears in an editable panel, you tweak it by
 * hand OR tell the AI what to change ("add error handling") and it rewrites it
 * in place, and you Run it. The inline-AI analogue of the document editor, for
 * code. Backend: /api/canvas/generate + /api/canvas/edit (src/canvas.py); Run
 * reuses codeRunner. Opened by #rail-canvas, the /canvas command, or open()/generate().
 */

import { runPython, runJavaScript, runHTML, runServer } from '../codeRunner.js';
import { trapFocus } from '../modalA11y.js';

const LANGS = ['python', 'javascript', 'html', 'bash', 'typescript', 'json', 'css', 'text'];
let _undo = [];
let _releaseFocus = null;

function _el(tag, cls, html) { const e = document.createElement(tag); if (cls) e.className = cls; if (html != null) e.innerHTML = html; return e; }
function _modal() { return document.getElementById('code-canvas-modal'); }
function _ed() { return document.getElementById('canvas-editor'); }
function _langSel() { return document.getElementById('canvas-lang'); }
function _out() { return document.getElementById('canvas-output'); }
function _esc(s) { return String(s == null ? '' : s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])); }

function _styles() {
  if (document.getElementById('code-canvas-styles')) return;
  const s = _el('style'); s.id = 'code-canvas-styles';
  s.textContent = `
  #code-canvas-modal.hidden { display: none; }
  #code-canvas-modal { position: fixed; inset: 0; z-index: 4100; display: flex; align-items: center;
    justify-content: center; background: rgba(0,0,0,.55); backdrop-filter: blur(2px); }
  .cv-panel { width: min(1000px, 95vw); height: min(88vh, 900px); display: flex; flex-direction: column;
    background: var(--panel,#120a1c); color: var(--fg,#cbb8ec); border: 1px solid var(--border,#3a2657);
    border-radius: var(--radius-lg, 16px); box-shadow: var(--shadow-lg, 0 20px 60px rgba(0,0,0,.5)); overflow: hidden; }
  .cv-head { display: flex; align-items: center; gap: 10px; padding: 12px 16px; border-bottom: 1px solid var(--border,#3a2657); }
  .cv-head h2 { margin: 0; font-size: 1rem; font-weight: 600; }
  .cv-head .cv-spacer { flex: 1; }
  .cv-btn { background: transparent; color: var(--fg,#cbb8ec); border: 1px solid var(--border,#3a2657);
    border-radius: var(--radius-sm, 8px); padding: 5px 11px; font-size: .78rem; cursor: pointer; }
  .cv-btn:hover { border-color: var(--red,#b45de0); }
  .cv-btn.primary { background: var(--red,#b45de0); color: #fff; border: none; font-weight: 600; }
  .cv-btn:disabled { opacity: .5; cursor: default; }
  #canvas-lang { background: var(--panel,#120a1c); color: var(--fg,#cbb8ec); border: 1px solid var(--border,#3a2657);
    border-radius: var(--radius-sm, 8px); padding: 4px 8px; font-size: .78rem; }
  .cv-body { flex: 1; display: flex; flex-direction: column; min-height: 0; }
  #canvas-editor { flex: 1; width: 100%; box-sizing: border-box; resize: none; border: none; outline: none;
    background: var(--code-bg, var(--panel,#0c0715)); color: var(--code-fg, var(--fg,#e6dcff)); padding: 14px 16px; font-family: var(--mono, 'JetBrains Mono', 'Fira Code', ui-monospace, monospace);
    font-size: 13px; line-height: 1.55; tab-size: 4; white-space: pre; overflow: auto; }
  .cv-ai { display: flex; gap: 8px; align-items: center; padding: 10px 14px; border-top: 1px solid var(--border,#3a2657); }
  #canvas-ai-input { flex: 1; background: var(--input-bg, var(--panel,#0c0715)); color: var(--fg,#cbb8ec); border: 1px solid var(--border,#3a2657);
    border-radius: var(--radius-sm, 8px); padding: 8px 12px; font-size: .82rem; outline: none; }
  #canvas-ai-input:focus { border-color: var(--red,#b45de0); }
  .cv-note { font-size: .74rem; opacity: .72; padding: 0 14px 8px; min-height: 1em; }
  #canvas-output:empty { display: none; }
  #canvas-output { max-height: 34%; overflow: auto; border-top: 1px solid var(--border,#3a2657); }
  .cv-spin { display: inline-block; width: 13px; height: 13px; border: 2px solid var(--border,#3a2657);
    border-top-color: var(--red,#b45de0); border-radius: 50%; animation: cv-rot .7s linear infinite; vertical-align: -2px; }
  @keyframes cv-rot { to { transform: rotate(360deg); } }
  `;
  document.head.appendChild(s);
}

function _build() {
  if (_modal()) return;
  _styles();
  const modal = _el('div', 'hidden'); modal.id = 'code-canvas-modal';
  modal.innerHTML = `
    <div class="cv-panel">
      <div class="cv-head">
        <h2>Code Canvas</h2>
        <select id="canvas-lang" title="Language">${LANGS.map(l => `<option value="${l}">${l}</option>`).join('')}</select>
        <span class="cv-spacer"></span>
        <button class="cv-btn primary" id="canvas-run">▶ Run</button>
        <button class="cv-btn" id="canvas-undo" title="Undo last AI edit" disabled>↶ Undo</button>
        <button class="cv-btn" id="canvas-copy">Copy</button>
        <button class="cv-btn" id="canvas-download">Download</button>
        <button class="cv-btn" id="canvas-close" aria-label="Close">×</button>
      </div>
      <div class="cv-body">
        <textarea id="canvas-editor" spellcheck="false" placeholder="Write or paste code — or ask the AI below to build something."></textarea>
      </div>
      <div class="cv-note" id="canvas-note"></div>
      <div class="cv-ai">
        <input id="canvas-ai-input" placeholder="Ask AI to edit — e.g. 'add error handling', 'make it a class', 'add a CLI'">
        <button class="cv-btn primary" id="canvas-ai-send">Ask AI</button>
      </div>
      <div id="canvas-output"></div>
    </div>`;
  document.body.appendChild(modal);
  modal.addEventListener('mousedown', (e) => { if (e.target === modal) close(); });
  modal.querySelector('#canvas-close').addEventListener('click', close);
  modal.querySelector('#canvas-run').addEventListener('click', _run);
  modal.querySelector('#canvas-copy').addEventListener('click', _copy);
  modal.querySelector('#canvas-download').addEventListener('click', _download);
  modal.querySelector('#canvas-undo').addEventListener('click', _undoEdit);
  modal.querySelector('#canvas-ai-send').addEventListener('click', _askAI);
  modal.querySelector('#canvas-ai-input').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); _askAI(); }
  });
  // Tab inserts spaces instead of leaving the editor.
  _ed().addEventListener('keydown', (e) => {
    if (e.key === 'Tab') {
      e.preventDefault();
      const ta = e.target, s = ta.selectionStart, en = ta.selectionEnd;
      ta.value = ta.value.slice(0, s) + '    ' + ta.value.slice(en);
      ta.selectionStart = ta.selectionEnd = s + 4;
    }
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && _modal() && !_modal().classList.contains('hidden') &&
        document.activeElement !== _ed() && document.activeElement !== modal.querySelector('#canvas-ai-input')) close();
  });
}

function _setLang(lang) {
  if (!lang) return;
  const sel = _langSel();
  const l = ({ js: 'javascript', ts: 'typescript', py: 'python', sh: 'bash' })[lang] || lang;
  if ([...sel.options].some(o => o.value === l)) sel.value = l;
}

function _note(msg, spinning) {
  _modal().querySelector('#canvas-note').innerHTML = (spinning ? '<span class="cv-spin"></span> ' : '') + _esc(msg || '');
}

function _pushUndo() { _undo.push(_ed().value); if (_undo.length > 30) _undo.shift(); _modal().querySelector('#canvas-undo').disabled = _undo.length === 0; }
function _undoEdit() { if (!_undo.length) return; _ed().value = _undo.pop(); _modal().querySelector('#canvas-undo').disabled = _undo.length === 0; _note('Reverted.'); }

async function _askAI() {
  const instruction = _modal().querySelector('#canvas-ai-input').value.trim();
  if (!instruction) return;
  const sendBtn = _modal().querySelector('#canvas-ai-send');
  sendBtn.disabled = true; _note('Thinking…', true);
  try {
    const r = await fetch('/api/canvas/edit', {
      method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ code: _ed().value, instruction, language: _langSel().value }),
    });
    const d = await r.json().catch(() => ({}));
    if (!d.ok) { _note('✗ ' + (d.error || 'edit failed')); return; }
    _pushUndo();
    _ed().value = d.code;
    if (d.language) _setLang(d.language);
    _modal().querySelector('#canvas-ai-input').value = '';
    _note((d.explanation || 'Updated.') + `  ·  ${d.model || ''}`);
  } catch (e) { _note('✗ ' + e.message); }
  finally { sendBtn.disabled = false; }
}

function _run() {
  const code = _ed().value, lang = _langSel().value, out = _out();
  out.innerHTML = '';
  try {
    if (lang === 'python') runPython(code, out);
    else if (lang === 'javascript' || lang === 'typescript') runJavaScript(code, out);
    else if (lang === 'html') runHTML(code, out);
    else runServer(code, out, lang);
  } catch (e) { out.innerHTML = `<pre class="code-runner-pre">Run failed: ${_esc(e.message)}</pre>`; }
}

function _copy() {
  const v = _ed().value;
  (navigator.clipboard ? navigator.clipboard.writeText(v) : Promise.reject()).then(() => _note('Copied.')).catch(() => _note('Copy failed.'));
}
function _download() {
  const ext = ({ python: 'py', javascript: 'js', typescript: 'ts', html: 'html', bash: 'sh', json: 'json', css: 'css' })[_langSel().value] || 'txt';
  const blob = new Blob([_ed().value], { type: 'text/plain' });
  const a = _el('a'); a.href = URL.createObjectURL(blob); a.download = 'canvas.' + ext; a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

export function open(code, language) {
  _build(); _undo = [];
  if (code != null) _ed().value = code;
  if (language) _setLang(language);
  _modal().querySelector('#canvas-undo').disabled = true;
  _note('');
  _modal().classList.remove('hidden');
  if (_releaseFocus) _releaseFocus();
  _releaseFocus = trapFocus(_modal().querySelector('.cv-panel'), { onEscape: close });
  setTimeout(() => _ed().focus(), 40);
}

export async function generate(prompt, language) {
  _build(); _undo = [];
  _ed().value = ''; _note('Generating…', true);
  if (language) _setLang(language);
  _modal().classList.remove('hidden');
  if (_releaseFocus) _releaseFocus();
  _releaseFocus = trapFocus(_modal().querySelector('.cv-panel'), { onEscape: close });
  try {
    const r = await fetch('/api/canvas/generate', {
      method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ prompt, language: language || '' }),
    });
    const d = await r.json().catch(() => ({}));
    if (!d.ok) { _note('✗ ' + (d.error || 'generation failed')); return; }
    _ed().value = d.code; if (d.language) _setLang(d.language);
    _note((d.explanation || 'Generated.') + `  ·  ${d.model || ''}`);
  } catch (e) { _note('✗ ' + e.message); }
}

export function close() {
  const m = _modal();
  if (m) m.classList.add('hidden');
  if (_releaseFocus) { _releaseFocus(); _releaseFocus = null; }
}
export function isOpen() { const m = _modal(); return !!m && !m.classList.contains('hidden'); }

const canvasModule = { open, close, isOpen, generate };
export default canvasModule;
window.canvasModule = canvasModule;

function _wire() {
  const btn = document.getElementById('rail-canvas');
  if (btn && !btn._cvWired) { btn._cvWired = true; btn.addEventListener('click', () => isOpen() ? close() : open()); }
}
if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', _wire);
else _wire();
