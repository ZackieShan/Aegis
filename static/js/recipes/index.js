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

// Library state
let _catalog = [];           // [{id,name,category,description,sample_input,expected_output,available,needs,preview,recipe}]
let _libCategory = 'All';
let _libSearch = '';
let _canAuthor = false;      // admin — gets the editor + build affordances

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
    border: 1px solid transparent; border-radius: var(--radius-sm, 8px); padding: 5px 8px; min-width: 180px;
  }
  .recipe-name-input:hover { border-color: var(--border, #333); }
  .recipe-name-input:focus { border-color: var(--accent, var(--red)); outline: none; }
  .recipe-btn {
    display: inline-flex; align-items: center; gap: 5px; cursor: pointer;
    font: 500 12.5px/1 inherit; color: var(--fg, #eee); background: var(--panel, #1b1b1b);
    border: 1px solid var(--border, #333); border-radius: var(--radius-sm, 8px); padding: 7px 11px;
  }
  .recipe-btn:hover { border-color: var(--accent, var(--red)); }
  .recipe-btn.primary { background: var(--accent, var(--red)); color: #fff; border-color: var(--accent, var(--red)); font-weight: 600; }
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
    border-radius: var(--radius-sm, 8px); padding: 7px 9px; display: flex; align-items: center; gap: 7px;
  }
  .palette-item:hover { border-color: var(--accent, var(--red)); transform: translateX(1px); }
  .palette-item .dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
  .palette-empty { font-size: 11px; opacity: 0.5; padding: 4px 2px; }
  .recipe-canvas-wrap { flex: 1; position: relative; overflow: auto; background:
    radial-gradient(circle, color-mix(in srgb, var(--fg, #eee) 9%, transparent) 1px, transparent 1px);
    background-size: 22px 22px; }
  #recipe-canvas { position: relative; width: 2400px; height: 1600px; }
  #recipe-edges { position: absolute; inset: 0; width: 100%; height: 100%; pointer-events: none; z-index: 1; }
  #recipe-edges path.wire { fill: none; stroke: var(--accent, var(--red)); stroke-width: 2.5; opacity: 0.75; }
  #recipe-edges path.wire-hit { fill: none; stroke: transparent; stroke-width: 14px; pointer-events: stroke; cursor: pointer; }
  #recipe-edges path.wire-temp { fill: none; stroke: var(--accent, var(--red)); stroke-width: 2.5; stroke-dasharray: 5 4; opacity: 0.9; }
  .recipe-node {
    position: absolute; z-index: 2; width: 210px; background: var(--panel, #1b1b1b);
    border: 1px solid var(--border, #333); border-radius: var(--radius-md, 12px); box-shadow: var(--shadow-md, 0 4px 14px rgba(0,0,0,0.35));
    font-size: 12px; color: var(--fg, #eee);
  }
  .recipe-node.sel { border-color: var(--accent, var(--red)); box-shadow: 0 0 0 1px var(--accent, var(--red)), var(--shadow-md, 0 4px 14px rgba(0,0,0,0.4)); }
  .recipe-node .node-head {
    display: flex; align-items: center; gap: 6px; padding: 7px 9px; cursor: grab;
    border-bottom: 1px solid var(--border, #333); border-radius: var(--radius-md, 12px) var(--radius-md, 12px) 0 0;
    background: color-mix(in srgb, var(--type-color, var(--accent, var(--red))) 16%, transparent);
  }
  .recipe-node .node-head:active { cursor: grabbing; }
  .recipe-node .node-type { font-weight: 700; text-transform: capitalize; }
  .recipe-node .node-id { font-size: 10px; opacity: 0.55; font-family: var(--mono, ui-monospace, monospace); }
  .recipe-node .node-del { margin-left: auto; cursor: pointer; opacity: 0.5; font-size: 14px; line-height: 1; padding: 0 2px; }
  .recipe-node .node-del:hover { opacity: 1; color: var(--color-error, #f66); }
  .recipe-node .node-body { padding: 8px 9px; display: flex; flex-direction: column; gap: 6px; }
  .recipe-node select, .recipe-node input, .recipe-node textarea {
    width: 100%; box-sizing: border-box; font: 11.5px/1.35 inherit; color: var(--fg, #eee);
    background: var(--bg, #111); border: 1px solid var(--border, #333); border-radius: var(--radius-sm, 8px); padding: 5px 6px;
  }
  .recipe-node textarea { resize: vertical; min-height: 46px; }
  .recipe-node .node-hint { font-size: 10px; opacity: 0.5; }
  .recipe-node .arg-row { display: flex; flex-direction: column; gap: 2px; }
  .recipe-node .arg-row label { font-size: 10px; opacity: 0.7; font-family: var(--mono, ui-monospace, monospace); }
  .node-port {
    position: absolute; width: 14px; height: 14px; border-radius: 50%;
    background: var(--bg, #111); border: 2px solid var(--accent, var(--red)); top: 12px; cursor: crosshair; z-index: 3;
  }
  .node-port.in { left: -8px; }
  .node-port.out { right: -8px; }
  .node-port:hover { background: var(--accent, var(--red)); }
  .node-port.hot { background: var(--accent, var(--red)); transform: scale(1.25); }
  .recipe-run {
    border-top: 1px solid var(--border, #333); padding: 9px 12px; display: flex; flex-direction: column;
    gap: 8px; max-height: 42%; overflow-y: auto; background: color-mix(in srgb, var(--panel, #1b1b1b) 40%, transparent);
  }
  .recipe-run-top { display: flex; gap: 8px; align-items: center; }
  .recipe-run-input { flex: 1; font: 12.5px/1.2 inherit; color: var(--fg, #eee);
    background: var(--bg, #111); border: 1px solid var(--border, #333); border-radius: var(--radius-sm, 8px); padding: 8px 10px; }
  .recipe-step { border: 1px solid var(--border, #333); border-radius: 7px; padding: 7px 9px; background: var(--bg, #111); }
  .recipe-step .step-head { display: flex; gap: 7px; align-items: center; font-size: 11px; margin-bottom: 3px; }
  .recipe-step .step-badge { font: 600 9.5px/1 inherit; text-transform: uppercase; letter-spacing: 0.04em;
    padding: 2px 6px; border-radius: 4px; background: color-mix(in srgb, var(--accent, var(--red)) 22%, transparent); }
  .recipe-step .step-label { font-family: var(--mono, ui-monospace, monospace); opacity: 0.7; }
  .recipe-step pre { margin: 0; white-space: pre-wrap; word-break: break-word; font: 11px/1.45 var(--mono, ui-monospace, monospace);
    max-height: 160px; overflow: auto; opacity: 0.92; }
  .recipe-final { border-color: var(--accent, var(--red)); }
  .recipe-empty-hint { position: absolute; top: 42%; left: 0; right: 0; text-align: center;
    opacity: 0.4; font-size: 13px; pointer-events: none; z-index: 0; }
  .recipe-menu { position: absolute; z-index: 40; background: var(--panel, #1b1b1b); border: 1px solid var(--border, #333);
    border-radius: var(--radius-md, 12px); box-shadow: var(--shadow-md, 0 8px 24px rgba(0,0,0,0.4)); padding: 5px; min-width: 200px; max-height: 320px; overflow-y: auto; }
  .recipe-menu button { display: block; width: 100%; text-align: left; cursor: pointer; font: 12px/1.3 inherit;
    color: var(--fg, #eee); background: transparent; border: 0; border-radius: var(--radius-sm, 8px); padding: 6px 8px; }
  .recipe-menu button:hover { background: color-mix(in srgb, var(--fg, #eee) 9%, transparent); }
  .recipe-menu .menu-empty { padding: 6px 8px; font-size: 11px; opacity: 0.5; }

  /* ── library front door ── */
  /* The class/id display rules below beat the UA [hidden]{display:none}, so
     make hidden win explicitly (buttons are inline-flex, views are flex). */
  #recipe-library-view[hidden], #recipe-editor-view[hidden],
  #recipe-run-panel[hidden], .recipe-btn[hidden] { display: none !important; }
  #recipe-library-view { flex: 1; display: flex; flex-direction: column; min-height: 0; }
  .lib-head { display: flex; align-items: center; gap: 10px; padding: 11px 16px;
    border-bottom: 1px solid var(--border, #333); flex-wrap: wrap; }
  .lib-search { flex: 1; min-width: 160px; font: 13px/1.2 inherit; color: var(--fg, #eee);
    background: var(--bg, #111); border: 1px solid var(--border, #333); border-radius: var(--radius-sm, 8px); padding: 8px 11px; }
  .lib-search:focus { border-color: var(--accent, var(--red)); outline: none; }
  .lib-cats { display: flex; gap: 6px; flex-wrap: wrap; }
  .lib-cat { cursor: pointer; font: 500 12px/1 inherit; color: var(--fg, #eee);
    background: transparent; border: 1px solid var(--border, #333); border-radius: 20px; padding: 6px 12px; }
  .lib-cat:hover { border-color: var(--accent, var(--red)); }
  .lib-cat.on { background: var(--accent, var(--red)); color: #fff; border-color: var(--accent, var(--red)); }
  #recipe-lib-grid { flex: 1; overflow-y: auto; padding: 16px;
    display: grid; grid-template-columns: repeat(auto-fill, minmax(276px, 1fr)); gap: 12px; align-content: start; }
  .lib-group { grid-column: 1 / -1; margin: 6px 0 -2px; font: 600 11px/1.2 inherit;
    text-transform: uppercase; letter-spacing: 0.08em; color: color-mix(in srgb, var(--fg, #eee) 45%, transparent); }
  .lib-group:first-child { margin-top: 0; }
  .lib-card { display: flex; flex-direction: column; gap: 7px; padding: 14px;
    background: var(--panel, #1b1b1b); border: 1px solid var(--border, #333); border-radius: var(--radius-md, 12px);
    min-height: 132px; }
  .lib-card.runnable { cursor: pointer; }
  .lib-card.runnable:hover { border-color: var(--accent, var(--red)); transform: translateY(-1px); }
  .lib-card h5 { margin: 0; font: 600 14px/1.25 inherit; color: var(--fg, #eee); }
  .lib-card p { margin: 0; font-size: 12px; line-height: 1.5; color: color-mix(in srgb, var(--fg, #eee) 70%, transparent); flex: 1; }
  .lib-card .card-foot { display: flex; align-items: center; gap: 8px; margin-top: 2px; }
  .lib-run-cta { font: 600 12px/1 inherit; color: var(--accent, var(--red)); display: inline-flex; align-items: center; gap: 4px; }
  .lib-chip { font: 600 10px/1 inherit; text-transform: uppercase; letter-spacing: 0.05em;
    padding: 3px 7px; border-radius: 5px; white-space: nowrap; }
  .lib-chip.needs { background: color-mix(in srgb, var(--warn, #e0a050) 18%, transparent); color: var(--warn, #e0a050); }
  .lib-chip.preview { background: color-mix(in srgb, var(--fg, #eee) 12%, transparent); color: color-mix(in srgb, var(--fg, #eee) 60%, transparent); }
  .lib-card.gated, .lib-card.preview { opacity: 0.92; }
  .lib-card.gated .card-foot, .lib-card.preview .card-foot { flex-wrap: wrap; }
  .lib-enable { cursor: pointer; font: 600 11.5px/1 inherit; color: #fff;
    background: var(--warn, #e0a050); border: 0; border-radius: var(--radius-sm, 8px); padding: 6px 10px; }
  .lib-enable[disabled] { opacity: 0.6; pointer-events: none; }
  .lib-note { font-size: 11px; opacity: 0.6; }

  /* run panel (one-click canned run) */
  #recipe-run-panel { flex: 1; display: flex; flex-direction: column; min-height: 0; padding: 16px; gap: 12px; overflow-y: auto; }
  .rp-back { align-self: flex-start; cursor: pointer; font: 500 12.5px/1 inherit; color: var(--fg, #eee);
    background: transparent; border: 0; opacity: 0.7; padding: 4px 0; }
  .rp-back:hover { opacity: 1; }
  .rp-title { font: 700 17px/1.2 inherit; margin: 0; }
  .rp-desc { font-size: 13px; line-height: 1.5; color: color-mix(in srgb, var(--fg, #eee) 70%, transparent); margin: 0; }
  .rp-label { font: 600 11px/1 inherit; text-transform: uppercase; letter-spacing: 0.06em; opacity: 0.6; }
  .rp-input { font: 13px/1.5 inherit; color: var(--fg, #eee); background: var(--bg, #111);
    border: 1px solid var(--border, #333); border-radius: var(--radius-sm, 8px); padding: 10px 12px; resize: vertical; min-height: 60px; }
  .rp-input:focus { border-color: var(--accent, var(--red)); outline: none; }
  .rp-expect { font-size: 12px; opacity: 0.6; }
  #recipe-run-panel .recipe-run { border-top: 0; padding: 0; background: transparent; max-height: none; }
  .rp-header-btn { display: none; }
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
        <button class="recipe-btn" id="recipe-to-library" hidden>← Library</button>
        <button class="recipe-btn" id="recipe-to-build" hidden title="Build your own recipe in the node editor">+ Build your own</button>
        <button class="close-btn" aria-label="Close recipes">✖</button>
      </div>

      <!-- Library front door — the default view -->
      <div id="recipe-library-view">
        <div class="lib-head">
          <input class="lib-search" id="recipe-lib-search" placeholder="Search recipes…" spellcheck="false" />
          <div class="lib-cats" id="recipe-lib-cats"></div>
        </div>
        <div id="recipe-lib-grid"></div>
        <div id="recipe-run-panel" hidden></div>
      </div>

      <!-- Editor — the authoring surface (admins) -->
      <div id="recipe-editor-view" hidden>
        <div class="recipe-toolbar">
          <input class="recipe-name-input" id="recipe-name" value="${_esc(_recipeName)}" spellcheck="false" />
          <button class="recipe-btn" id="recipe-new">New</button>
          <button class="recipe-btn" id="recipe-starters" title="Install or load ready-made starter recipes built for what you have">✨ Starters</button>
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
                <div style="margin-top:12px;opacity:0.7">New here? Click <b>✨ Starters</b> up top to install ready-made recipes or load one to tweak.</div>
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
      </div>
    </div>`;
  document.body.appendChild(modal);

  // Header + toolbar wiring
  modal.querySelector('.close-btn').addEventListener('click', close);
  modal.addEventListener('mousedown', (e) => { if (e.target === modal) return; });
  modal.querySelector('#recipe-name').addEventListener('input', (e) => { _recipeName = e.target.value; });
  modal.querySelector('#recipe-new').addEventListener('click', _newRecipe);
  modal.querySelector('#recipe-starters').addEventListener('click', _startersMenu);
  modal.querySelector('#recipe-open').addEventListener('click', _openMenu);
  modal.querySelector('#recipe-save').addEventListener('click', _save);
  modal.querySelector('#recipe-delete').addEventListener('click', _deleteCurrent);
  modal.querySelector('#recipe-run-btn').addEventListener('click', _run);
  // Library ↔ editor
  modal.querySelector('#recipe-to-library').addEventListener('click', _showLibrary);
  modal.querySelector('#recipe-to-build').addEventListener('click', () => _showEditor());
  modal.querySelector('#recipe-lib-search').addEventListener('input', (e) => {
    _libSearch = e.target.value.toLowerCase(); _renderLibrary();
  });

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
  // Non-text models (diffusion/video/vision/audio) can't drive a model node.
  if (/image|video|diffusion|vision|-vl\b|embed|whisper|tts|\bwan[0-9.]|\bltx|flux|sdxl|stable-diffusion/.test(s)) return 0;
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
  let best = '';
  for (const m of list) {
    if (_rankModel(m) <= 0) continue;
    if (!best || _rankModel(m) > _rankModel(best)) best = m;
  }
  return best || list[0];
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
  el.style.setProperty('--type-color', TYPE_COLORS[node.type] || 'var(--accent, var(--red))');

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

// Starter recipes: the backend generates a set tailored to what this install
// actually has (best available model + only the toolboxes that are connected),
// so a first-timer can install them into their saved list with one click or
// load any into the editor to tweak.
async function _startersMenu(e) {
  document.querySelectorAll('.recipe-menu').forEach(m => m.remove());
  // Capture the button rect BEFORE awaiting — e.currentTarget is nulled once
  // the async handler yields at the first await.
  const btn = e.currentTarget.getBoundingClientRect();
  let starters = [];
  try {
    const res = await fetch(`${API}/api/recipes/starters`, { credentials: 'same-origin' });
    if (res.ok) starters = (await res.json()).recipes || [];
  } catch (_) {}
  const menu = _el('div', 'recipe-menu');
  menu.style.left = btn.left + 'px';
  menu.style.top = (btn.bottom + 4) + 'px';
  if (!starters.length) {
    menu.appendChild(_el('div', 'menu-empty', 'No starters available — add a model in Settings first.'));
  } else {
    const all = _el('button');
    all.innerHTML = `<div><b>⬇ Install all ${starters.length} to my recipes</b></div>`
      + `<div style="font-size:10.5px;opacity:0.55">Saved so you can Open + Run them anytime</div>`;
    all.addEventListener('click', () => { menu.remove(); _installStarters(); });
    menu.appendChild(all);
    const sep = _el('div');
    sep.style.cssText = 'height:1px;background:var(--border,#333);opacity:.5;margin:4px 0';
    menu.appendChild(sep);
    starters.forEach(r => {
      const b = _el('button');
      b.innerHTML = `<div>${_esc(r.name)}</div>`
        + `<div style="font-size:10.5px;opacity:0.55">${_esc(r.description || '')}</div>`;
      b.addEventListener('click', () => {
        menu.remove();
        _loadGraph(r);
        _toast('Loaded into the editor — press ▶ Run, or Save to keep it.');
      });
      menu.appendChild(b);
    });
  }
  document.body.appendChild(menu);
  const off = (ev) => { if (!menu.contains(ev.target)) { menu.remove(); document.removeEventListener('mousedown', off); } };
  setTimeout(() => document.addEventListener('mousedown', off), 0);
}

async function _installStarters() {
  try {
    const res = await fetch(`${API}/api/recipes/starters/install`, {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' }, body: '{}',
    });
    if (!res.ok) { _toast('Install failed: ' + await _errText(res)); return; }
    const d = await res.json();
    const n = d.count || 0;
    const sk = (d.skipped || []).length;
    if (n) {
      _toast(`Installed ${n} starter recipe${n === 1 ? '' : 's'}${sk ? ` (${sk} already present)` : ''} — open them via “Open…”.`);
    } else {
      _toast(sk ? 'Starter recipes are already installed — see “Open…”.' : 'No starters to install.');
    }
  } catch (e) { _toast('Install failed: ' + e.message); }
}

async function _openMenu(e) {
  document.querySelectorAll('.recipe-menu').forEach(m => m.remove());
  // Read the button rect before awaiting — e.currentTarget is null after the
  // async handler yields.
  const btn = e.currentTarget.getBoundingClientRect();
  let list = [];
  try {
    const res = await fetch(`${API}/api/recipes`, { credentials: 'same-origin' });
    if (res.ok) list = (await res.json()).recipes || [];
  } catch (_) {}
  const menu = _el('div', 'recipe-menu');
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

// Load a full recipe graph object into the editor. `r.id` present → editing a
// saved recipe; absent (e.g. a starter template) → a fresh unsaved graph so
// Save creates a new one instead of overwriting.
function _loadGraph(r) {
  _newRecipe();
  _recipeId = r.id || null;
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
  const runInput = document.getElementById('recipe-run-input');
  if (runInput && r.run_example) runInput.value = r.run_example;
}

async function _load(id) {
  try {
    const res = await fetch(`${API}/api/recipes/${id}`, { credentials: 'same-origin' });
    if (!res.ok) { _toast('Could not open recipe.'); return; }
    _loadGraph(await res.json());
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
// ── library ────────────────────────────────────────────────────────────────
function _libView() { return document.getElementById('recipe-library-view'); }
function _editorView() { return document.getElementById('recipe-editor-view'); }
function _runPanel() { return document.getElementById('recipe-run-panel'); }

function _showLibrary() {
  _libView().hidden = false;
  _editorView().hidden = true;
  _runPanel().hidden = true;
  document.getElementById('recipe-lib-grid').style.display = '';
  _modal().querySelector('#recipe-to-library').hidden = true;
  _modal().querySelector('#recipe-to-build').hidden = !_canAuthor;
}

function _showEditor() {
  _libView().hidden = true;
  _editorView().hidden = false;
  _modal().querySelector('#recipe-to-library').hidden = false;
  _modal().querySelector('#recipe-to-build').hidden = true;
  // Lazily render the editor the first time it's shown.
  _renderPalette();
  _nodes.forEach(_renderNode);
  _drawEdges();
  _syncEmptyHint();
}

async function _loadCatalog() {
  try {
    const r = await fetch(`${API}/api/recipes/catalog`, { credentials: 'same-origin' });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const d = await r.json();
    _catalog = d.recipes || [];
    _canAuthor = !!d.can_author;
  } catch (e) {
    _catalog = [];
    console.warn('recipes catalog load failed', e);
  }
}

function _renderCats() {
  const wrap = document.getElementById('recipe-lib-cats');
  if (!wrap) return;
  const cats = ['All', ...Array.from(new Set(_catalog.map(r => r.category)))];
  wrap.replaceChildren();
  cats.forEach(c => {
    const b = _el('button', 'lib-cat' + (c === _libCategory ? ' on' : ''), c);
    b.type = 'button';
    b.addEventListener('click', () => { _libCategory = c; _renderLibrary(); });
    wrap.appendChild(b);
  });
}

function _matchesLib(r) {
  if (_libCategory !== 'All' && r.category !== _libCategory) return false;
  if (!_libSearch) return true;
  return (r.name + ' ' + r.description + ' ' + r.category).toLowerCase().includes(_libSearch);
}

function _renderLibrary() {
  _renderCats();
  const grid = document.getElementById('recipe-lib-grid');
  if (!grid) return;
  grid.style.display = '';
  _runPanel().hidden = true;
  grid.replaceChildren();

  const items = _catalog.filter(_matchesLib);
  if (!items.length) {
    grid.appendChild(_el('div', 'lib-note', _catalog.length
      ? 'Nothing matches that search.'
      : 'No recipes available — add a model in Settings first.'));
    return;
  }
  // Group by category (only when showing All).
  const groups = _libCategory === 'All'
    ? Array.from(new Set(items.map(r => r.category))).map(c => [c, items.filter(r => r.category === c)])
    : [[_libCategory, items]];

  for (const [cat, list] of groups) {
    if (_libCategory === 'All') grid.appendChild(_el('div', 'lib-group', cat));
    list.forEach(r => grid.appendChild(_libCard(r)));
  }
}

function _libCard(r) {
  const gated = !r.available && !r.preview && r.needs && r.needs.length;
  const card = _el('div', 'lib-card' + (r.available ? ' runnable' : (r.preview ? ' preview' : ' gated')));
  card.appendChild(_el('h5', null, r.name));
  card.appendChild(_el('p', null, r.description));
  const foot = _el('div', 'card-foot');

  if (r.available) {
    const cta = _el('span', 'lib-run-cta', '▶ Run');
    foot.appendChild(cta);
    card.addEventListener('click', () => _openRunPanel(r));
  } else if (r.preview) {
    foot.appendChild(_el('span', 'lib-chip preview', 'Coming soon'));
    if (r.preview_note) card.title = r.preview_note;
    const note = _el('span', 'lib-note', r.preview_note || 'Arrives with Automations.');
    foot.appendChild(note);
  } else if (gated) {
    const need = r.needs[0];
    foot.appendChild(_el('span', 'lib-chip needs', 'Needs ' + need.label));
    if (_canAuthor) {
      const btn = _el('button', 'lib-enable', 'Enable ' + need.label + ' →');
      btn.type = 'button';
      btn.addEventListener('click', (e) => { e.stopPropagation(); _enableToolbox(need, btn); });
      foot.appendChild(btn);
    } else {
      foot.appendChild(_el('span', 'lib-note', 'Ask an admin to enable these tools.'));
    }
  }
  card.appendChild(foot);
  return card;
}

async function _enableToolbox(need, btn) {
  btn.disabled = true;
  const orig = btn.textContent;
  btn.textContent = 'Enabling…';
  try {
    const r = await fetch(`${API}/api/recipes/toolbox/enable`, {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ toolbox: need.toolbox }),
    });
    const d = await r.json().catch(() => ({}));
    if (r.ok && d.ok) {
      _toast(`${need.label} tools ready — ${d.tools} tools.`);
      await _loadCatalog();
      _renderLibrary();
    } else {
      btn.disabled = false; btn.textContent = orig;
      _toast(d.message || d.detail || `Could not enable ${need.label}.`);
    }
  } catch (e) {
    btn.disabled = false; btn.textContent = orig;
    _toast('Enable failed: ' + e.message);
  }
}

function _openRunPanel(r) {
  const grid = document.getElementById('recipe-lib-grid');
  const panel = _runPanel();
  grid.style.display = 'none';
  panel.hidden = false;
  panel.replaceChildren();

  const back = _el('button', 'rp-back', '← All recipes');
  back.type = 'button';
  back.addEventListener('click', _showLibrary);
  panel.appendChild(back);
  panel.appendChild(_el('h3', 'rp-title', r.name));
  panel.appendChild(_el('p', 'rp-desc', r.description));

  panel.appendChild(_el('div', 'rp-label', 'Input'));
  const input = _el('textarea', 'rp-input');
  input.value = '';
  input.placeholder = r.sample_input || 'Type your input…';
  panel.appendChild(input);
  panel.appendChild(_el('div', 'rp-expect', 'You’ll get: ' + r.expected_output));

  const runBtn = _el('button', 'recipe-btn primary', '▶ Run');
  runBtn.type = 'button';
  runBtn.style.alignSelf = 'flex-start';
  panel.appendChild(runBtn);
  const out = _el('div', 'recipe-run');
  const outBody = _el('div');
  out.appendChild(outBody);
  panel.appendChild(out);

  const doRun = () => _runCanned(r, input.value.trim() || (input.placeholder.startsWith('Type') ? '' : input.placeholder), runBtn, outBody);
  runBtn.addEventListener('click', doRun);
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) doRun(); });
  setTimeout(() => input.focus(), 60);
}

async function _runCanned(r, runInput, btn, outBody) {
  btn.disabled = true;
  const orig = btn.textContent;
  btn.textContent = 'Running…';
  outBody.replaceChildren(_el('div', 'lib-note', 'Running ' + r.name + '…'));
  try {
    const res = await fetch(`${API}/api/recipes/catalog/${encodeURIComponent(r.id)}/run`, {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ input: runInput }),
    });
    const d = await res.json().catch(() => ({}));
    if (res.status === 409 && d.detail && d.detail.error === 'toolbox_disabled') {
      outBody.replaceChildren(_el('div', 'lib-note', d.detail.message + ' Enable it from the library.'));
      btn.disabled = false; btn.textContent = orig;
      return;
    }
    if (!res.ok || d.ok === false) {
      outBody.replaceChildren(_el('div', 'lib-note', '✗ ' + (d.error || (d.detail && d.detail.message) || d.detail || 'Run failed')));
      btn.disabled = false; btn.textContent = orig;
      return;
    }
    _renderRunResult(d, outBody);
  } catch (e) {
    outBody.replaceChildren(_el('div', 'lib-note', '✗ ' + e.message));
  }
  btn.disabled = false; btn.textContent = orig;
}

function _renderRunResult(d, outBody) {
  outBody.replaceChildren();
  (d.steps || []).forEach(s => {
    const step = _el('div', 'recipe-step');
    const head = _el('div', 'step-head');
    head.appendChild(_el('span', 'step-badge', s.type));
    if (s.label) head.appendChild(_el('span', 'step-label', s.label));
    step.appendChild(head);
    const pre = _el('pre'); pre.textContent = s.output || '';
    step.appendChild(pre);
    outBody.appendChild(step);
  });
  if (d.final) {
    const fin = _el('div', 'recipe-step recipe-final');
    const head = _el('div', 'step-head');
    head.appendChild(_el('span', 'step-badge', 'result'));
    fin.appendChild(head);
    const pre = _el('pre'); pre.textContent = d.final;
    fin.appendChild(pre);
    outBody.appendChild(fin);
  }
}

export async function open() {
  _build();
  await _loadBlocks();
  await _loadCatalog();
  _renderLibrary();
  _showLibrary();
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
