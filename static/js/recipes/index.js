/**
 * recipes/index.js — Recipes: a visual node editor for tool + model workflows.
 *
 * A recipe is a directed graph. You drop nodes onto a canvas and wire them:
 *   input  → emits the run-time input text
 *   tool   → calls a toolbox MCP tool (osint_*, market_*, ts_*)
 *   model  → runs a prompt through a local model
 *   output → collects the final result
 *
 * Node config values (tool args, model prompts) can reference upstream node
 * outputs with {{nodeId}} and the run input with {{input}}. Execution happens
 * server-side (src/recipes.py); this module is just the editor + runner UI.
 *
 * Self-contained: builds its own modal, injects its own CSS, and talks to
 * /api/recipes. Toggled by #tool-recipes-btn (and #rail-recipes via app.js).
 */

import { trapFocus } from '../modalA11y.js';

const API = ''; // same-origin
const MODAL_ID = 'recipes-modal';

let _built = false;
let _releaseFocus = null;
let _blocks = { models: [], tools: [] };   // building blocks from /api/recipes/tools
let _nodes = [];                            // {id, type, x, y, config}
let _edges = [];                            // {from, to}
let _recipeId = null;
let _recipeName = 'Untitled recipe';
let _nodeSeq = 0;
let _running = false;

// ── DOM helpers ──────────────────────────────────────────────────────────────
function _el(tag, cls, txt) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (txt != null) e.textContent = txt;
  return e;
}
function _esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}
function _modal() { return document.getElementById(MODAL_ID); }
function _canvas() { return document.getElementById('recipe-canvas'); }
function _edgeLayer() { return document.getElementById('recipe-edges'); }

// ── CSS ──────────────────────────────────────────────────────────────────────
function _injectStyles() {
  if (document.getElementById('recipes-styles')) return;
  const css = `
  #${MODAL_ID} .modal-content {
    width: min(1180px, 96vw); height: min(88vh, 900px);
    display: flex; flex-direction: column; background: var(--bg, #111);
    padding: 0; overflow: hidden;
  }
  .recipe-toolbar {
    display: flex; align-items: center; gap: 8px; padding: 8px 12px;
    border-bottom: 1px solid var(--border, #333); flex-wrap: wrap;
  }
  .recipe-name-input {
    font: 600 14px/1.2 inherit; background: transparent; color: var(--fg, #eee);
    border: 1px solid transparent; border-radius: 6px; padding: 5px 8px; min-width: 180px;
  }
  .recipe-name-input:hover { border-color: var(--border, #333); }
  .recipe-name-input:focus { border-color: var(--accent, #6cf); outline: none; }
  .recipe-btn {
    display: inline-flex; align-items: center; gap: 5px; cursor: pointer;
    font: 500 12.5px/1 inherit; color: var(--fg, #eee); background: var(--panel, #1b1b1b);
    border: 1px solid var(--border, #333); border-radius: 6px; padding: 7px 11px;
  }
  .recipe-btn:hover { border-color: var(--accent, #6cf); }
  .recipe-btn.primary { background: var(--accent, #6cf); color: #04121f; border-color: var(--accent, #6cf); font-weight: 600; }
  .recipe-btn.primary:hover { filter: brightness(1.08); }
  .recipe-btn[disabled] { opacity: 0.5; pointer-events: none; }
  .recipe-body { flex: 1; display: flex; min-height: 0; }
  .recipe-palette {
    width: 190px; flex-shrink: 0; border-right: 1px solid var(--border, #333);
    padding: 10px; overflow-y: auto; display: flex; flex-direction: column; gap: 6px;
    background: color-mix(in srgb, var(--panel, #1b1b1b) 55%, transparent);
  }
  .recipe-palette h5 {
    margin: 8px 0 2px; font: 600 10.5px/1.2 inherit; text-transform: uppercase;
    letter-spacing: 0.06em; color: color-mix(in srgb, var(--fg, #eee) 45%, transparent);
  }
  .palette-item {
    text-align: left; cursor: pointer; font: 500 12px/1.2 inherit; color: var(--fg, #eee);
    background: var(--panel, #1b1b1b); border: 1px solid var(--border, #333);
    border-radius: 6px; padding: 7px 9px; display: flex; align-items: center; gap: 7px;
  }
  .palette-item:hover { border-color: var(--accent, #6cf); transform: translateX(1px); }
  .palette-item .dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
  .palette-empty { font-size: 11px; opacity: 0.5; padding: 4px 2px; }
  .recipe-canvas-wrap { flex: 1; position: relative; overflow: auto; background:
    radial-gradient(circle, color-mix(in srgb, var(--fg, #eee) 9%, transparent) 1px, transparent 1px);
    background-size: 22px 22px; }
  #recipe-canvas { position: relative; width: 2400px; height: 1600px; }
  #recipe-edges { position: absolute; inset: 0; width: 100%; height: 100%; pointer-events: none; z-index: 1; }
  #recipe-edges path.wire { fill: none; stroke: var(--accent, #6cf); stroke-width: 2.5; opacity: 0.75; }
  #recipe-edges path.wire-hit { fill: none; stroke: transparent; stroke-width: 14px; pointer-events: stroke; cursor: pointer; }
  #recipe-edges path.wire-temp { fill: none; stroke: var(--accent, #6cf); stroke-width: 2.5; stroke-dasharray: 5 4; opacity: 0.9; }
  .recipe-node {
    position: absolute; z-index: 2; width: 210px; background: var(--panel, #1b1b1b);
    border: 1px solid var(--border, #333); border-radius: 9px; box-shadow: 0 4px 14px rgba(0,0,0,0.35);
    font-size: 12px; color: var(--fg, #eee);
  }
  .recipe-node.sel { border-color: var(--accent, #6cf); box-shadow: 0 0 0 1px var(--accent, #6cf), 0 4px 14px rgba(0,0,0,0.4); }
  .recipe-node .node-head {
    display: flex; align-items: center; gap: 6px; padding: 7px 9px; cursor: grab;
    border-bottom: 1px solid var(--border, #333); border-radius: 9px 9px 0 0;
    background: color-mix(in srgb, var(--type-color, #6cf) 16%, transparent);
  }
  .recipe-node .node-head:active { cursor: grabbing; }
  .recipe-node .node-type { font-weight: 700; text-transform: capitalize; }
  .recipe-node .node-id { font-size: 10px; opacity: 0.55; font-family: ui-monospace, monospace; }
  .recipe-node .node-del { margin-left: auto; cursor: pointer; opacity: 0.5; font-size: 14px; line-height: 1; padding: 0 2px; }
  .recipe-node .node-del:hover { opacity: 1; color: var(--color-error, #f66); }
  .recipe-node .node-body { padding: 8px 9px; display: flex; flex-direction: column; gap: 6px; }
  .recipe-node select, .recipe-node input, .recipe-node textarea {
    width: 100%; box-sizing: border-box; font: 11.5px/1.35 inherit; color: var(--fg, #eee);
    background: var(--bg, #111); border: 1px solid var(--border, #333); border-radius: 5px; padding: 5px 6px;
  }
  .recipe-node textarea { resize: vertical; min-height: 46px; }
  .recipe-node .node-hint { font-size: 10px; opacity: 0.5; }
  .recipe-node .arg-row { display: flex; flex-direction: column; gap: 2px; }
  .recipe-node .arg-row label { font-size: 10px; opacity: 0.7; font-family: ui-monospace, monospace; }
  .node-port {
    position: absolute; width: 14px; height: 14px; border-radius: 50%;
    background: var(--bg, #111); border: 2px solid var(--accent, #6cf); top: 12px; cursor: crosshair; z-index: 3;
  }
  .node-port.in { left: -8px; }
  .node-port.out { right: -8px; }
  .node-port:hover { background: var(--accent, #6cf); }
  .node-port.hot { background: var(--accent, #6cf); transform: scale(1.25); }
  .recipe-run {
    border-top: 1px solid var(--border, #333); padding: 9px 12px; display: flex; flex-direction: column;
    gap: 8px; max-height: 42%; overflow-y: auto; background: color-mix(in srgb, var(--panel, #1b1b1b) 40%, transparent);
  }
  .recipe-run-top { display: flex; gap: 8px; align-items: center; }
  .recipe-run-input { flex: 1; font: 12.5px/1.2 inherit; color: var(--fg, #eee);
    background: var(--bg, #111); border: 1px solid var(--border, #333); border-radius: 6px; padding: 8px 10px; }
  .recipe-step { border: 1px solid var(--border, #333); border-radius: 7px; padding: 7px 9px; background: var(--bg, #111); }
  .recipe-step .step-head { display: flex; gap: 7px; align-items: center; font-size: 11px; margin-bottom: 3px; }
  .recipe-step .step-badge { font: 600 9.5px/1 inherit; text-transform: uppercase; letter-spacing: 0.04em;
    padding: 2px 6px; border-radius: 4px; background: color-mix(in srgb, var(--accent, #6cf) 22%, transparent); }
  .recipe-step .step-label { font-family: ui-monospace, monospace; opacity: 0.7; }
  .recipe-step pre { margin: 0; white-space: pre-wrap; word-break: break-word; font: 11px/1.45 ui-monospace, monospace;
    max-height: 160px; overflow: auto; opacity: 0.92; }
  .recipe-final { border-color: var(--accent, #6cf); }
  .recipe-empty-hint { position: absolute; top: 42%; left: 0; right: 0; text-align: center;
    opacity: 0.4; font-size: 13px; pointer-events: none; z-index: 0; }
  .recipe-menu { position: absolute; z-index: 40; background: var(--panel, #1b1b1b); border: 1px solid var(--border, #333);
    border-radius: 8px; box-shadow: 0 8px 24px rgba(0,0,0,0.4); padding: 5px; min-width: 200px; max-height: 320px; overflow-y: auto; }
  .recipe-menu button { display: block; width: 100%; text-align: left; cursor: pointer; font: 12px/1.3 inherit;
    color: var(--fg, #eee); background: transparent; border: 0; border-radius: 5px; padding: 6px 8px; }
  .recipe-menu button:hover { background: color-mix(in srgb, var(--fg, #eee) 9%, transparent); }
  .recipe-menu .menu-empty { padding: 6px 8px; font-size: 11px; opacity: 0.5; }
  `;
  const style = _el('style');
  style.id = 'recipes-styles';
  style.textContent = css;
  document.head.appendChild(style);
}

// ── Modal construction ───────────────────────────────────────────────────────
function _build() {
  if (_built) return;
  _injectStyles();
  const modal = _el('div', 'modal hidden');
  modal.id = MODAL_ID;
  modal.innerHTML = `
    <div class="modal-content" role="dialog" aria-label="Recipes">
      <div class="modal-header" style="padding:8px 12px;border-bottom:1px solid var(--border,#333)">
        <h4 style="margin:0;margin-right:auto;display:flex;align-items:center;gap:7px">
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="4" width="7" height="6" rx="1.5"/><rect x="2" y="14" width="7" height="6" rx="1.5"/><rect x="15" y="9" width="7" height="6" rx="1.5"/><path d="M9 7h3a2 2 0 0 1 2 2v1"/><path d="M9 17h3a2 2 0 0 0 2-2v-1"/></svg>
          Recipes
        </h4>
        <button class="close-btn" aria-label="Close recipes">✖</button>
      </div>
      <div class="recipe-toolbar">
        <input class="recipe-name-input" id="recipe-name" value="${_esc(_recipeName)}" spellcheck="false" />
        <button class="recipe-btn" id="recipe-new">New</button>
        <button class="recipe-btn" id="recipe-example" title="Load a ready-made example graph">✨ Example</button>
        <button class="recipe-btn" id="recipe-open">Open…</button>
        <button class="recipe-btn" id="recipe-save">Save</button>
        <span style="flex:1"></span>
        <button class="recipe-btn" id="recipe-delete" title="Delete this recipe">Delete</button>
      </div>
      <div class="recipe-body">
        <div class="recipe-palette" id="recipe-palette"></div>
        <div class="recipe-canvas-wrap">
          <div id="recipe-canvas">
            <svg id="recipe-edges"></svg>
            <div class="recipe-empty-hint" id="recipe-empty-hint">
              <div style="font-weight:600;margin-bottom:8px">Build a tool + model workflow</div>
              <div style="text-align:left;display:inline-block;line-height:1.9;opacity:0.85">
                1. Click a block in the left palette to drop a node.<br>
                2. Drag from a node's right dot ● to another's left dot to wire them.<br>
                3. Type a run input below and hit ▶ Run.
              </div>
              <div style="margin-top:12px;opacity:0.7">New here? Click <b>✨ Example</b> up top to load a working graph.</div>
            </div>
          </div>
        </div>
      </div>
      <div class="recipe-run">
        <div class="recipe-run-top">
          <input class="recipe-run-input" id="recipe-run-input" placeholder="Run input (available as {{input}} to every node)…" spellcheck="false" />
          <button class="recipe-btn primary" id="recipe-run-btn">▶ Run</button>
        </div>
        <div id="recipe-run-output"></div>
      </div>
    </div>`;
  document.body.appendChild(modal);

  // Header + toolbar wiring
  modal.querySelector('.close-btn').addEventListener('click', close);
  modal.addEventListener('mousedown', (e) => { if (e.target === modal) return; });
  modal.querySelector('#recipe-name').addEventListener('input', (e) => { _recipeName = e.target.value; });
  modal.querySelector('#recipe-new').addEventListener('click', _newRecipe);
  modal.querySelector('#recipe-example').addEventListener('click', _exampleMenu);
  modal.querySelector('#recipe-open').addEventListener('click', _openMenu);
  modal.querySelector('#recipe-save').addEventListener('click', _save);
  modal.querySelector('#recipe-delete').addEventListener('click', _deleteCurrent);
  modal.querySelector('#recipe-run-btn').addEventListener('click', _run);

  // Esc closes (only while this modal is the visible one)
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !_modal().classList.contains('hidden')) {
      // Let an open recipe-menu close first
      const menu = document.querySelector('.recipe-menu');
      if (menu) { menu.remove(); return; }
      close();
    }
  });

  _built = true;
}

// ── Palette ──────────────────────────────────────────────────────────────────
const TYPE_COLORS = { input: '#5be6b0', output: '#ffd166', model: '#9d7cff', tool: '#7cc4ff', branch: '#ff9d5b', loop: '#4dd0e1' };

function _renderPalette() {
  const p = document.getElementById('recipe-palette');
  if (!p) return;
  p.innerHTML = '';
  const add = (label, color, onClick) => {
    const b = _el('button', 'palette-item');
    const dot = _el('span', 'dot'); dot.style.background = color;
    b.appendChild(dot); b.appendChild(_el('span', null, label));
    b.addEventListener('click', onClick);
    p.appendChild(b);
  };
  const h = (t) => p.appendChild(_el('h5', null, t));

  h('Flow');
  add('Input', TYPE_COLORS.input, () => _addNode('input'));
  add('Output', TYPE_COLORS.output, () => _addNode('output'));
  add('Branch (if)', TYPE_COLORS.branch, () => _addNode('branch', { condition: { kind: 'contains', value: '' } }));
  add('Loop (refine)', TYPE_COLORS.loop, () => _addNode('loop', { max_iters: 3, until: { kind: '', value: '' } }));

  h('Models');
  if (_blocks.models.length) {
    _blocks.models.forEach(m => add(m, TYPE_COLORS.model, () => _addNode('model', { model: m })));
  } else {
    p.appendChild(_el('div', 'palette-empty', 'No models — add one in Settings.'));
  }

  h('Tools');
  if (_blocks.tools.length) {
    _blocks.tools.forEach(t => add(t.name, TYPE_COLORS.tool, () => _addNode('tool', { tool: t.name })));
  } else {
    p.appendChild(_el('div', 'palette-empty', 'No toolbox tools connected.'));
  }
}

// Rough ranking of how reliably a LOCAL model calls tools (newer instruct
// models call tools; older ones like llama-pro hallucinate). Used to pick a
// sensible default model so a recipe doesn't silently fake tool output.
function _rankModel(m) {
  const s = String(m || '').toLowerCase();
  if (/qwen|firefunction|command-?r|hermes/.test(s)) return 5;
  if (/gemma-?([3-9]|1[0-9])|gemma4/.test(s)) return 5;
  if (/llama-?[34]|mistral|mixtral/.test(s)) return 4;
  if (/phi-?[3-9]|granite|nemotron/.test(s)) return 3;
  if (/llama-?pro|llama-?2|vicuna|alpaca|orca/.test(s)) return 1;
  return 2;
}
function _bestModel() {
  const list = _blocks.models || [];
  if (!list.length) return '';
  let best = list[0];
  for (const m of list) if (_rankModel(m) > _rankModel(best)) best = m;
  return best;
}

// ── Nodes ────────────────────────────────────────────────────────────────────
function _newId() {
  let id;
  do { id = 'n' + (++_nodeSeq); } while (_nodes.some(n => n.id === id));
  return id;
}

function _addNode(type, cfg = {}) {
  const wrap = _canvas().parentElement;
  // Drop near the current scroll viewport, staggered so nodes don't stack.
  const baseX = (wrap ? wrap.scrollLeft : 0) + 80 + (_nodes.length % 5) * 30;
  const baseY = (wrap ? wrap.scrollTop : 0) + 70 + (_nodes.length % 5) * 30;
  const node = { id: _newId(), type, x: baseX, y: baseY, config: { ...cfg } };
  _nodes.push(node);
  _renderNode(node);
  _syncEmptyHint();
  _drawEdges();
  return node;
}

function _removeNode(id) {
  _nodes = _nodes.filter(n => n.id !== id);
  _edges = _edges.filter(e => e.from !== id && e.to !== id);
  const el = document.getElementById('rnode-' + id);
  if (el) el.remove();
  _syncEmptyHint();
  _drawEdges();
}

function _renderNode(node) {
  const canvas = _canvas();
  let el = document.getElementById('rnode-' + node.id);
  if (el) el.remove();
  el = _el('div', 'recipe-node');
  el.id = 'rnode-' + node.id;
  el.style.left = node.x + 'px';
  el.style.top = node.y + 'px';
  el.style.setProperty('--type-color', TYPE_COLORS[node.type] || '#6cf');

  const head = _el('div', 'node-head');
  head.appendChild(_el('span', 'node-type', node.type));
  head.appendChild(_el('span', 'node-id', '#' + node.id));
  const del = _el('span', 'node-del', '×');
  del.title = 'Delete node';
  del.addEventListener('click', (e) => { e.stopPropagation(); _removeNode(node.id); });
  head.appendChild(del);
  el.appendChild(head);

  const body = _el('div', 'node-body');
  _renderNodeBody(node, body);
  el.appendChild(body);

  // Ports (input node has no in-port; output node has no out-port)
  if (node.type !== 'input') {
    const pin = _el('div', 'node-port in'); pin.dataset.port = 'in'; pin.dataset.node = node.id;
    el.appendChild(pin);
  }
  if (node.type !== 'output') {
    const pout = _el('div', 'node-port out'); pout.dataset.port = 'out'; pout.dataset.node = node.id;
    pout.addEventListener('pointerdown', (e) => _startWire(e, node.id));
    el.appendChild(pout);
  }

  _makeDraggable(el, head, node);
  canvas.appendChild(el);
}

function _renderNodeBody(node, body) {
  body.innerHTML = '';
  if (node.type === 'input') {
    const hint = _el('div', 'node-hint', 'Emits the run input. Reference it anywhere as {{input}}.');
    body.appendChild(hint);
    const lbl = _el('input');
    lbl.placeholder = 'Label (optional)';
    lbl.value = node.config.label || '';
    lbl.addEventListener('input', (e) => { node.config.label = e.target.value; });
    body.appendChild(lbl);
  } else if (node.type === 'output') {
    body.appendChild(_el('div', 'node-hint', 'Collects everything wired into it as the final result.'));
  } else if (node.type === 'branch') {
    node.config.condition = node.config.condition || { kind: 'contains', value: '' };
    const c = node.config.condition;
    body.appendChild(_el('div', 'node-hint', 'If the incoming text matches, data passes through; otherwise everything downstream is skipped.'));
    const sel = _el('select');
    [['contains', 'contains'], ['not_contains', 'does NOT contain'], ['regex', 'matches regex'],
     ['nonempty', 'is not empty'], ['empty', 'is empty']].forEach(([v, lab]) => {
      const o = _el('option', null, lab); o.value = v; if (v === c.kind) o.selected = true; sel.appendChild(o);
    });
    const val = _el('input'); val.placeholder = 'text to match'; val.value = c.value || '';
    val.addEventListener('input', (e) => { c.value = e.target.value; });
    const _syncVal = () => { val.style.display = (c.kind === 'nonempty' || c.kind === 'empty') ? 'none' : ''; };
    sel.addEventListener('change', (e) => { c.kind = e.target.value; _syncVal(); });
    _syncVal();
    body.appendChild(sel); body.appendChild(val);
  } else if (node.type === 'loop') {
    node.config.until = node.config.until || { kind: '', value: '' };
    body.appendChild(_el('div', 'node-hint', 'Runs the model repeatedly, feeding each result back as {{prev}} to refine it.'));
    const sel = _el('select');
    _blocks.models.forEach(m => { const o = _el('option', null, m); o.value = m; if (m === node.config.model) o.selected = true; sel.appendChild(o); });
    if (!node.config.model) node.config.model = _bestModel();
    sel.addEventListener('change', (e) => { node.config.model = e.target.value; });
    body.appendChild(sel);
    const ta = _el('textarea');
    ta.placeholder = 'Instruction. Use {{prev}} for the previous result, {{input}} for the run input.';
    ta.value = node.config.prompt || '';
    ta.addEventListener('input', (e) => { node.config.prompt = e.target.value; });
    body.appendChild(ta);
    const row = _el('div', 'arg-row'); row.appendChild(_el('label', null, 'max iterations (1–8)'));
    const mi = _el('input'); mi.type = 'number'; mi.min = '1'; mi.max = '8'; mi.value = node.config.max_iters || 3;
    mi.addEventListener('input', (e) => { node.config.max_iters = parseInt(e.target.value) || 3; });
    row.appendChild(mi); body.appendChild(row);
    const u = node.config.until;
    const stopRow = _el('div', 'arg-row'); stopRow.appendChild(_el('label', null, 'stop early when output…'));
    const usel = _el('select');
    [['', '(never — run all iterations)'], ['contains', 'contains'], ['regex', 'matches regex'], ['nonempty', 'is not empty']]
      .forEach(([v, lab]) => { const o = _el('option', null, lab); o.value = v; if (v === u.kind) o.selected = true; usel.appendChild(o); });
    const uval = _el('input'); uval.placeholder = 'text / regex'; uval.value = u.value || '';
    uval.addEventListener('input', (e) => { u.value = e.target.value; });
    const _syncU = () => { uval.style.display = (!u.kind || u.kind === 'nonempty') ? 'none' : ''; };
    usel.addEventListener('change', (e) => { u.kind = e.target.value; _syncU(); });
    _syncU();
    stopRow.appendChild(usel); stopRow.appendChild(uval); body.appendChild(stopRow);
  } else if (node.type === 'model') {
    const sel = _el('select');
    _blocks.models.forEach(m => {
      const o = _el('option', null, m); o.value = m;
      if (m === node.config.model) o.selected = true;
      sel.appendChild(o);
    });
    if (!node.config.model) node.config.model = _bestModel();
    sel.addEventListener('change', (e) => { node.config.model = e.target.value; });
    body.appendChild(sel);
    const ta = _el('textarea');
    ta.placeholder = 'Prompt. Use {{input}} and {{nodeId}} to pull in upstream outputs.';
    ta.value = node.config.prompt || '';
    ta.addEventListener('input', (e) => { node.config.prompt = e.target.value; });
    body.appendChild(ta);
  } else if (node.type === 'tool') {
    const sel = _el('select');
    _blocks.tools.forEach(t => {
      const o = _el('option', null, t.name); o.value = t.name;
      if (t.name === node.config.tool) o.selected = true;
      sel.appendChild(o);
    });
    if (!node.config.tool && _blocks.tools[0]) node.config.tool = _blocks.tools[0].name;
    sel.addEventListener('change', (e) => {
      node.config.tool = e.target.value;
      node.config.args = {};
      _renderNodeBody(node, body);   // re-render arg rows for the new tool
      _drawEdges();
    });
    body.appendChild(sel);
    const meta = _blocks.tools.find(t => t.name === node.config.tool);
    if (meta && meta.description) {
      const d = _el('div', 'node-hint', meta.description);
      body.appendChild(d);
    }
    node.config.args = node.config.args || {};
    (meta ? meta.params : []).forEach(param => {
      const row = _el('div', 'arg-row');
      row.appendChild(_el('label', null, param));
      const inp = _el('input');
      inp.placeholder = '{{input}} or a literal value';
      inp.value = node.config.args[param] != null ? node.config.args[param] : '';
      inp.addEventListener('input', (e) => { node.config.args[param] = e.target.value; });
      row.appendChild(inp);
      body.appendChild(row);
    });
    if (!meta || !meta.params.length) {
      body.appendChild(_el('div', 'node-hint', 'This tool takes no arguments.'));
    }
  }
}

// ── Dragging nodes ───────────────────────────────────────────────────────────
function _makeDraggable(el, handle, node) {
  handle.addEventListener('pointerdown', (e) => {
    if (e.target.classList.contains('node-del')) return;
    e.preventDefault();
    const startX = e.clientX, startY = e.clientY;
    const ox = node.x, oy = node.y;
    handle.setPointerCapture(e.pointerId);
    const move = (ev) => {
      node.x = Math.max(0, ox + (ev.clientX - startX));
      node.y = Math.max(0, oy + (ev.clientY - startY));
      el.style.left = node.x + 'px';
      el.style.top = node.y + 'px';
      _drawEdges();
    };
    const up = () => {
      handle.removeEventListener('pointermove', move);
      handle.removeEventListener('pointerup', up);
    };
    handle.addEventListener('pointermove', move);
    handle.addEventListener('pointerup', up, { once: true });
  });
}

// ── Wiring (edges) ───────────────────────────────────────────────────────────
function _portCenter(nodeId, which) {
  const nodeEl = document.getElementById('rnode-' + nodeId);
  if (!nodeEl) return null;
  const port = nodeEl.querySelector(`.node-port.${which}`);
  if (!port) return null;
  // Content-relative coords (independent of scroll) via offset chain.
  const x = nodeEl.offsetLeft + port.offsetLeft + port.offsetWidth / 2;
  const y = nodeEl.offsetTop + port.offsetTop + port.offsetHeight / 2;
  return { x, y };
}

function _wirePath(x1, y1, x2, y2) {
  const dx = Math.max(40, Math.abs(x2 - x1) * 0.5);
  return `M ${x1} ${y1} C ${x1 + dx} ${y1}, ${x2 - dx} ${y2}, ${x2} ${y2}`;
}

function _startWire(e, fromId) {
  e.preventDefault();
  e.stopPropagation();
  const wrap = _canvas().parentElement;
  const canvasRect = _canvas().getBoundingClientRect();
  const svg = _edgeLayer();
  const temp = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  temp.setAttribute('class', 'wire-temp');
  svg.appendChild(temp);
  const start = _portCenter(fromId, 'out');
  let hotPort = null;

  const move = (ev) => {
    const mx = ev.clientX - canvasRect.left + 0; // canvas is not transformed; rect already accounts for scroll
    const my = ev.clientY - canvasRect.top + 0;
    temp.setAttribute('d', _wirePath(start.x, start.y, mx, my));
    const tgt = document.elementFromPoint(ev.clientX, ev.clientY);
    const port = tgt && tgt.closest ? tgt.closest('.node-port.in') : null;
    if (hotPort && hotPort !== port) hotPort.classList.remove('hot');
    hotPort = port;
    if (hotPort) hotPort.classList.add('hot');
  };
  const up = (ev) => {
    document.removeEventListener('pointermove', move);
    document.removeEventListener('pointerup', up);
    if (hotPort) hotPort.classList.remove('hot');
    temp.remove();
    const tgt = document.elementFromPoint(ev.clientX, ev.clientY);
    const port = tgt && tgt.closest ? tgt.closest('.node-port.in') : null;
    if (port) {
      const toId = port.dataset.node;
      _connect(fromId, toId);
    }
  };
  document.addEventListener('pointermove', move);
  document.addEventListener('pointerup', up);
}

function _connect(fromId, toId) {
  if (fromId === toId) return;
  if (_edges.some(e => e.from === fromId && e.to === toId)) return;
  // Reject a connection that would create a cycle (from is reachable from to).
  if (_reaches(toId, fromId)) { _toast('That wire would create a loop.'); return; }
  _edges.push({ from: fromId, to: toId });
  _drawEdges();
}

function _reaches(start, target) {
  const seen = new Set();
  const stack = [start];
  while (stack.length) {
    const cur = stack.pop();
    if (cur === target) return true;
    if (seen.has(cur)) continue;
    seen.add(cur);
    _edges.filter(e => e.from === cur).forEach(e => stack.push(e.to));
  }
  return false;
}

function _drawEdges() {
  const svg = _edgeLayer();
  if (!svg) return;
  // Keep any in-progress temp wire; rebuild the committed ones.
  svg.querySelectorAll('path.wire, path.wire-hit').forEach(p => p.remove());
  _edges.forEach((edge, i) => {
    const a = _portCenter(edge.from, 'out');
    const b = _portCenter(edge.to, 'in');
    if (!a || !b) return;
    const d = _wirePath(a.x, a.y, b.x, b.y);
    const hit = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    hit.setAttribute('class', 'wire-hit');
    hit.setAttribute('d', d);
    hit.style.pointerEvents = 'stroke';
    hit.addEventListener('click', () => { _edges.splice(i, 1); _drawEdges(); });
    hit.addEventListener('mouseenter', () => hit.previousSibling && (hit.previousSibling.style.opacity = '1'));
    svg.appendChild(hit);
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('class', 'wire');
    path.setAttribute('d', d);
    svg.insertBefore(path, hit);
  });
}

function _syncEmptyHint() {
  const hint = document.getElementById('recipe-empty-hint');
  if (hint) hint.style.display = _nodes.length ? 'none' : '';
}

// ── Persistence ──────────────────────────────────────────────────────────────
function _graph() {
  return {
    id: _recipeId || undefined,
    name: _recipeName || 'Untitled recipe',
    nodes: _nodes.map(n => ({ id: n.id, type: n.type, x: n.x, y: n.y, config: n.config })),
    edges: _edges.map(e => ({ from: e.from, to: e.to })),
  };
}

async function _save() {
  if (!_nodes.length) { _toast('Add at least one node first.'); return; }
  try {
    const res = await fetch(`${API}/api/recipes`, {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(_graph()),
    });
    if (!res.ok) { _toast('Save failed: ' + (await _errText(res))); return; }
    const saved = await res.json();
    _recipeId = saved.id;
    _toast('Saved.');
  } catch (e) { _toast('Save failed: ' + e.message); }
}

async function _deleteCurrent() {
  if (!_recipeId) { _newRecipe(); return; }
  if (!confirm('Delete this recipe?')) return;
  try {
    await fetch(`${API}/api/recipes/${_recipeId}`, { method: 'DELETE', credentials: 'same-origin' });
  } catch (_) {}
  _newRecipe();
  _toast('Deleted.');
}

function _newRecipe() {
  _recipeId = null;
  _recipeName = 'Untitled recipe';
  _nodes = []; _edges = []; _nodeSeq = 0;
  const canvas = _canvas();
  canvas.querySelectorAll('.recipe-node').forEach(n => n.remove());
  const nameInput = document.getElementById('recipe-name');
  if (nameInput) nameInput.value = _recipeName;
  document.getElementById('recipe-run-output').innerHTML = '';
  _drawEdges();
  _syncEmptyHint();
}

// Small menu of ready-made starter graphs so a first-time user sees a working
// recipe instead of a blank canvas.
function _exampleMenu(e) {
  document.querySelectorAll('.recipe-menu').forEach(m => m.remove());
  const menu = _el('div', 'recipe-menu');
  const btn = e.currentTarget.getBoundingClientRect();
  menu.style.left = btn.left + 'px';
  menu.style.top = (btn.bottom + 4) + 'px';
  const items = [
    ['🕵  Domain dossier', 'OSINT: whois + DNS → risk brief', _tmplDomainDossier],
    ['📈  Analyst debate', 'Market: data → value/growth/contrarian → portfolio manager', _tmplAnalystDebate],
  ];
  items.forEach(([label, sub, fn]) => {
    const b = _el('button');
    b.innerHTML = `<div>${label}</div><div style="font-size:10.5px;opacity:0.55">${sub}</div>`;
    b.addEventListener('click', () => { menu.remove(); fn(); });
    menu.appendChild(b);
  });
  document.body.appendChild(menu);
  const off = (ev) => { if (!menu.contains(ev.target)) { menu.remove(); document.removeEventListener('mousedown', off); } };
  setTimeout(() => document.addEventListener('mousedown', off), 0);
}

function _mk(type, x, y, config) {
  const n = { id: _newId(), type, x, y, config: config || {} };
  _nodes.push(n);
  return n;
}
function _finishTemplate(name, runValue) {
  _recipeName = name;
  const nameInput = document.getElementById('recipe-name');
  if (nameInput) nameInput.value = _recipeName;
  const runInput = document.getElementById('recipe-run-input');
  if (runInput && runValue) runInput.value = runValue;
  _nodes.forEach(_renderNode);
  _drawEdges();
  _syncEmptyHint();
}

// OSINT: run input → whois + DNS → model risk brief → output.
function _tmplDomainDossier() {
  _newRecipe();
  const toolNames = _blocks.tools.map(t => t.name);
  const model = _bestModel();
  const inNode = _mk('input', 60, 150, { label: 'Domain' });
  const outNode = _mk('output', 880, 150, {});
  const parents = [];
  if (toolNames.includes('osint_whois') || toolNames.includes('osint_dns')) {
    if (toolNames.includes('osint_whois')) {
      const w = _mk('tool', 330, 60, { tool: 'osint_whois', args: { target: '{{input}}' } });
      _edges.push({ from: inNode.id, to: w.id }); parents.push(w.id);
    }
    if (toolNames.includes('osint_dns')) {
      const d = _mk('tool', 330, 240, { tool: 'osint_dns', args: { domain: '{{input}}', record_type: 'A' } });
      _edges.push({ from: inNode.id, to: d.id }); parents.push(d.id);
    }
    const m = _mk('model', 610, 150, { model, prompt: 'Write a short risk brief on this domain from the recon above.' });
    parents.forEach(p => _edges.push({ from: p, to: m.id }));
    _edges.push({ from: m.id, to: outNode.id });
  } else {
    const m = _mk('model', 400, 150, { model, prompt: 'Summarize the following in 3 bullet points:\n\n{{input}}' });
    _edges.push({ from: inNode.id, to: m.id });
    _edges.push({ from: m.id, to: outNode.id });
  }
  _finishTemplate('Domain dossier (example)', 'example.com');
}

// Market "hedge fund" pattern (inspired by ai-hedge-fund, but fully local):
// ticker → market data tools → three investor-persona model nodes → a
// portfolio-manager node that weighs them → output. Persona prompts carry no
// {{refs}}, so the engine auto-prepends each node's upstream data as context.
function _tmplAnalystDebate() {
  _newRecipe();
  const toolNames = _blocks.tools.map(t => t.name);
  const model = _bestModel();
  const inNode = _mk('input', 40, 230, { label: 'Ticker' });
  const dataNodes = [];
  if (toolNames.includes('market_analyze')) {
    const a = _mk('tool', 280, 110, { tool: 'market_analyze', args: { symbol: '{{input}}' } });
    _edges.push({ from: inNode.id, to: a.id }); dataNodes.push(a.id);
  }
  if (toolNames.includes('market_fundamentals')) {
    const f = _mk('tool', 280, 350, { tool: 'market_fundamentals', args: { symbol: '{{input}}' } });
    _edges.push({ from: inNode.id, to: f.id }); dataNodes.push(f.id);
  }
  if (!dataNodes.length) {
    // Market toolbox not connected — degrade to a single analyst on raw input.
    const a = _mk('model', 320, 230, { model, prompt: 'Give a bull and bear case for this ticker:\n\n{{input}}' });
    const out = _mk('output', 620, 230, {});
    _edges.push({ from: inNode.id, to: a.id });
    _edges.push({ from: a.id, to: out.id });
    _finishTemplate('Analyst debate (needs Market toolbox)', 'NVDA');
    return;
  }
  const personas = [
    ['Value investor', 110, 'You are a disciplined value investor in the Buffett/Graham tradition. Using the market data above, judge whether this is a quality business at a fair price. Cite valuation (P/E, P/B, FCF), margins, and balance-sheet health. End with your call: BULLISH, BEARISH, or NEUTRAL, and a one-line reason.'],
    ['Growth investor', 230, 'You are a growth investor in the Cathie Wood/Peter Lynch tradition. Using the market data above, judge the growth story: revenue/earnings growth, momentum, and narrative. Weigh upside against the price paid. End with your call: BULLISH, BEARISH, or NEUTRAL, and a one-line reason.'],
    ['Contrarian / risk', 350, 'You are a contrarian risk manager in the Burry/Taleb tradition. Using the market data above, stress-test the bull case: overvaluation, debt, volatility, crowding, and tail risks. End with your call: BULLISH, BEARISH, or NEUTRAL, and a one-line reason.'],
  ];
  const personaIds = [];
  personas.forEach(([label, y, prompt]) => {
    const p = _mk('model', 560, y, { model, prompt, label });
    dataNodes.forEach(d => _edges.push({ from: d, to: p.id }));
    personaIds.push(p.id);
  });
  const pm = _mk('model', 860, 230, {
    model, label: 'Portfolio manager',
    prompt: 'You are the portfolio manager. Three analysts gave their views above. Weigh them, note where they agree and disagree, and give a final call — BUY, HOLD, or SELL — with a confidence (low/medium/high) and a 2-sentence rationale. This is educational analysis, not investment advice.',
  });
  personaIds.forEach(id => _edges.push({ from: id, to: pm.id }));
  const outNode = _mk('output', 1140, 230, {});
  _edges.push({ from: pm.id, to: outNode.id });
  _finishTemplate('Analyst debate (example)', 'NVDA');
}

async function _openMenu(e) {
  document.querySelectorAll('.recipe-menu').forEach(m => m.remove());
  let list = [];
  try {
    const res = await fetch(`${API}/api/recipes`, { credentials: 'same-origin' });
    if (res.ok) list = (await res.json()).recipes || [];
  } catch (_) {}
  const menu = _el('div', 'recipe-menu');
  const btn = e.currentTarget.getBoundingClientRect();
  menu.style.left = btn.left + 'px';
  menu.style.top = (btn.bottom + 4) + 'px';
  if (!list.length) {
    menu.appendChild(_el('div', 'menu-empty', 'No saved recipes yet.'));
  } else {
    list.forEach(r => {
      const b = _el('button', null, `${r.name}  ·  ${r.node_count} node${r.node_count === 1 ? '' : 's'}`);
      b.addEventListener('click', () => { menu.remove(); _load(r.id); });
      menu.appendChild(b);
    });
  }
  document.body.appendChild(menu);
  const off = (ev) => { if (!menu.contains(ev.target)) { menu.remove(); document.removeEventListener('mousedown', off); } };
  setTimeout(() => document.addEventListener('mousedown', off), 0);
}

async function _load(id) {
  try {
    const res = await fetch(`${API}/api/recipes/${id}`, { credentials: 'same-origin' });
    if (!res.ok) { _toast('Could not open recipe.'); return; }
    const r = await res.json();
    _newRecipe();
    _recipeId = r.id;
    _recipeName = r.name || 'Untitled recipe';
    document.getElementById('recipe-name').value = _recipeName;
    _nodes = (r.nodes || []).map(n => ({
      id: n.id, type: n.type,
      x: typeof n.x === 'number' ? n.x : 80, y: typeof n.y === 'number' ? n.y : 80,
      config: n.config || {},
    }));
    _edges = (r.edges || []).map(e => ({ from: e.from, to: e.to }));
    // Keep the id counter ahead of loaded ids like "n7".
    _nodes.forEach(n => { const m = /^n(\d+)$/.exec(n.id); if (m) _nodeSeq = Math.max(_nodeSeq, +m[1]); });
    _nodes.forEach(_renderNode);
    _drawEdges();
    _syncEmptyHint();
  } catch (e) { _toast('Open failed: ' + e.message); }
}

// ── Running ──────────────────────────────────────────────────────────────────
async function _run() {
  if (_running) return;
  if (!_nodes.length) { _toast('Nothing to run — add some nodes.'); return; }
  _running = true;
  const runBtn = document.getElementById('recipe-run-btn');
  const out = document.getElementById('recipe-run-output');
  runBtn.disabled = true; runBtn.textContent = '… Running';
  out.innerHTML = '<div class="node-hint" style="opacity:.7">Running the graph…</div>';
  try {
    const body = { recipe: _graph(), input: (document.getElementById('recipe-run-input').value || '') };
    const res = await fetch(`${API}/api/recipes/run`, {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({ ok: false, error: 'bad response' }));
    _renderRun(data);
  } catch (e) {
    out.innerHTML = `<div class="recipe-step"><pre>Run failed: ${_esc(e.message)}</pre></div>`;
  } finally {
    _running = false; runBtn.disabled = false; runBtn.textContent = '▶ Run';
  }
}

function _renderRun(data) {
  const out = document.getElementById('recipe-run-output');
  out.innerHTML = '';
  if (!data.ok) {
    out.innerHTML = `<div class="recipe-step"><pre>${_esc(data.error || 'Run failed')}</pre></div>`;
    return;
  }
  (data.steps || []).forEach(s => {
    const step = _el('div', 'recipe-step');
    const head = _el('div', 'step-head');
    head.appendChild(_el('span', 'step-badge', s.type));
    head.appendChild(_el('span', 'step-label', s.label || s.id));
    step.appendChild(head);
    const pre = _el('pre', null, s.output || '');
    step.appendChild(pre);
    out.appendChild(step);
  });
  if (data.final != null && data.final !== '') {
    const fin = _el('div', 'recipe-step recipe-final');
    const head = _el('div', 'step-head');
    head.appendChild(_el('span', 'step-badge', 'final'));
    fin.appendChild(head);
    fin.appendChild(_el('pre', null, data.final));
    out.appendChild(fin);
  }
}

// ── Misc ─────────────────────────────────────────────────────────────────────
async function _errText(res) {
  try { const j = await res.json(); return j.detail || j.error || res.statusText; }
  catch (_) { return res.statusText; }
}
function _toast(msg) {
  try {
    if (window.uiModule && window.uiModule.showToast) { window.uiModule.showToast(msg); return; }
  } catch (_) {}
  console.log('[recipes]', msg);
}

async function _loadBlocks() {
  try {
    const res = await fetch(`${API}/api/recipes/tools`, { credentials: 'same-origin' });
    if (res.ok) {
      const d = await res.json();
      _blocks = { models: d.models || [], tools: d.tools || [] };
    }
  } catch (_) {}
}

// ── Open / close ─────────────────────────────────────────────────────────────
export async function open() {
  _build();
  await _loadBlocks();
  _renderPalette();
  // Re-render open nodes so freshly-loaded models/tools populate their selects.
  _nodes.forEach(_renderNode);
  _drawEdges();
  _syncEmptyHint();
  _modal().classList.remove('hidden');
  const panel = _modal().querySelector('.modal-content');
  if (_releaseFocus) _releaseFocus();
  _releaseFocus = trapFocus(panel, { onEscape: close });
}
export function close() {
  const m = _modal();
  if (m) m.classList.add('hidden');
  document.querySelectorAll('.recipe-menu').forEach(x => x.remove());
  if (_releaseFocus) { _releaseFocus(); _releaseFocus = null; }
}
export function isOpen() {
  const m = _modal();
  return !!m && !m.classList.contains('hidden');
}
export function toggle() { isOpen() ? close() : open(); }

const recipesModule = { open, close, isOpen, toggle };
export default recipesModule;
window.recipesModule = recipesModule;

// Wire the sidebar tool button (the rail button delegates to it via app.js).
function _wireButtons() {
  const btn = document.getElementById('tool-recipes-btn');
  if (btn && !btn._recipesWired) {
    btn._recipesWired = true;
    btn.addEventListener('click', toggle);
  }
}
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', _wireButtons);
} else {
  _wireButtons();
}
