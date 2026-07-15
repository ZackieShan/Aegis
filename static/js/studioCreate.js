/**
 * Studio → Create: generate images and video where the results live.
 *
 * The full flow without leaving the Studio: describe it (✨ can rewrite rough
 * intent into a diffusion-ready scene), pick model/style/duration, generate —
 * images resolve inline, videos hand off to the Queue tab, and everything
 * lands in Photos where the Movie tab can pick it up.
 */

let _kind = 'video';          // 'image' | 'video' — video first: it's the flagship
let _models = { image: [], video: [] };
let _styles = [];
let _activeStyle = '';
let _busy = false;

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

async function _loadOptions() {
  const [img, vid, sty] = await Promise.allSettled([
    _json('/api/image/models'),
    _json('/api/video/models'),
    _json('/api/styles'),
  ]);
  _models.image = img.status === 'fulfilled' ? (img.value.models || []) : [];
  _models.video = vid.status === 'fulfilled' ? (vid.value.models || []) : [];
  _styles = sty.status === 'fulfilled' ? (sty.value.styles || []) : [];
  _activeStyle = sty.status === 'fulfilled' ? (sty.value.active || '') : '';
}

function _status(msg, cls) {
  const s = document.getElementById('create-status');
  if (!s) return;
  s.className = 'movie-status' + (cls ? ` ${cls}` : '');
  s.replaceChildren();
  if (msg) s.appendChild(typeof msg === 'string' ? document.createTextNode(msg) : msg);
}

async function _enhance() {
  const ta = document.getElementById('create-prompt');
  const btn = document.getElementById('create-enhance');
  const rough = (ta?.value || '').trim();
  if (!rough || !btn || btn.disabled) return;
  btn.disabled = true;
  const orig = btn.textContent;
  btn.textContent = 'Enhancing…';
  try {
    const d = await _json('/api/image/enhance-prompt', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ prompt: rough, kind: _kind }),
    });
    if (d.ok && d.prompt) {
      // Write the draft FIRST, and re-look-up the textarea: every _render()
      // rebuilds it from the draft, and programmatic .value writes don't fire
      // the input listener — without both, toggling Image/Video (even while
      // this request was in flight) silently reverts to the rough prompt and
      // Generate submits text the user isn't looking at.
      window.__createPromptDraft = d.prompt;
      const live = document.getElementById('create-prompt');
      if (live) live.value = d.prompt;
      _status(`Rewritten by ${d.model} — edit freely, then Generate.`);
    } else {
      _status(d.error || 'Enhance failed', 'movie-status-err');
    }
  } catch (e) {
    _status('Enhance failed: ' + e.message, 'movie-status-err');
  } finally {
    btn.disabled = false;
    btn.textContent = orig;
  }
}

async function _generate(host) {
  if (_busy) return;
  const prompt = (document.getElementById('create-prompt')?.value || '').trim();
  if (!prompt) { _status('Describe what you want first.', 'movie-status-err'); return; }

  const model = document.getElementById('create-model')?.value || '';
  const style = document.getElementById('create-style')?.value || '';
  const seedRaw = document.getElementById('create-seed')?.value || '';
  const body = { prompt };
  if (model) body.model = model;
  // '' = let the backend fall back to the active style; 'none' disables it.
  if (style) body.style = style;
  if (seedRaw !== '' && !isNaN(Number(seedRaw))) body.seed = Number(seedRaw);

  _busy = true;
  const btn = document.getElementById('create-go');
  if (btn) { btn.disabled = true; btn.textContent = 'Generating…'; }

  try {
    if (_kind === 'video') {
      const dur = Number(document.getElementById('create-duration')?.value || 5);
      body.duration = Math.max(1, Math.min(16, isNaN(dur) ? 5 : dur));
      const d = await _json('/api/video/generate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      _status(`Queued on ${d.model} — ${d.duration_s}s at ${d.fps}fps.`);
      // The render is now the Queue tab's story; take the user to it.
      document.querySelector('#gallery-modal .gallery-tab[data-tab="queue"]')?.click();
    } else {
      const size = document.getElementById('create-size')?.value || '';
      if (size) body.size = size;
      _status('Rendering… the first run also loads the model, give it a minute.');
      const d = await _json('/api/image/generate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (d.error) { _status(d.error, 'movie-status-err'); return; }
      const wrap = _el('span');
      wrap.appendChild(document.createTextNode('Done — saved to Photos. '));
      const a = document.createElement('a');
      a.href = d.image_url; a.target = '_blank'; a.textContent = 'Open';
      wrap.appendChild(a);
      _status(wrap, 'movie-status-ok');
      const img = document.getElementById('create-result');
      if (img) { img.src = d.image_url; img.style.display = ''; }
      window.dispatchEvent(new CustomEvent('gallery-refresh', { detail: { source: 'studio-create' } }));
    }
  } catch (e) {
    _status(e.message, 'movie-status-err');
  } finally {
    _busy = false;
    if (btn) { btn.disabled = false; btn.textContent = 'Generate'; }
  }
}

function _fillModelSelect() {
  const sel = document.getElementById('create-model');
  if (!sel) return;
  sel.replaceChildren();
  const auto = document.createElement('option');
  auto.value = '';
  auto.textContent = '— auto —';
  sel.appendChild(auto);
  (_models[_kind] || []).forEach(m => {
    const o = document.createElement('option');
    o.value = m.model;
    o.textContent = m.endpoint === 'comfyui' ? `${m.model} (ComfyUI)` : m.model;
    sel.appendChild(o);
  });
}

function _render(host) {
  host.replaceChildren();

  const head = _el('div', 'movie-head');
  head.appendChild(_el('h2', '', 'Create'));
  head.appendChild(_el('div', 'admin-toggle-sub',
    'Generate images and clips right here. Describe the scene — not the task: '
    + '"a lone rider crossing red desert at dusk, painted anime style" beats '
    + '"make me a movie trailer" (the model will literally draw a screen). '
    + 'The ✨ button rewrites rough ideas for you.'));
  host.appendChild(head);

  // Type toggle
  const pills = _el('div', 'create-pills');
  [['video', 'Video'], ['image', 'Image']].forEach(([k, label]) => {
    const b = _el('button', 'create-pill' + (k === _kind ? ' active' : ''), label);
    b.type = 'button';
    b.addEventListener('click', () => { _kind = k; _render(host); });
    pills.appendChild(b);
  });
  host.appendChild(pills);

  // Prompt + enhance
  const ta = document.createElement('textarea');
  ta.id = 'create-prompt';
  ta.className = 'create-prompt';
  ta.rows = 3;
  ta.placeholder = _kind === 'video'
    ? 'A lone rider crossing a red-rock desert at dusk, long shadows, slow tracking shot, hand-painted anime style with soft watercolor skies…'
    : 'A weathered cowboy portrait at golden hour, warm rim light, painterly anime style, shallow depth of field…';
  ta.value = window.__createPromptDraft || '';
  ta.addEventListener('input', () => { window.__createPromptDraft = ta.value; });
  host.appendChild(ta);

  const enhanceRow = _el('div', 'create-row');
  const enh = _el('button', 'memory-toolbar-btn', '✨ Enhance prompt');
  enh.id = 'create-enhance';
  enh.type = 'button';
  enh.title = 'Rewrite rough intent into a scene description the model can actually draw (local utility model)';
  enh.addEventListener('click', _enhance);
  enhanceRow.appendChild(enh);
  host.appendChild(enhanceRow);

  // Options grid
  const grid = _el('div', 'create-grid');
  const opt = (label, el) => {
    const w = _el('label', 'create-opt');
    w.appendChild(_el('span', 'create-opt-label', label));
    w.appendChild(el);
    grid.appendChild(w);
  };

  const modelSel = document.createElement('select');
  modelSel.id = 'create-model';
  modelSel.className = 'settings-select';
  opt('Model', modelSel);

  const styleSel = document.createElement('select');
  styleSel.id = 'create-style';
  styleSel.className = 'settings-select';
  const oDefault = document.createElement('option');
  oDefault.value = '';
  oDefault.textContent = _activeStyle ? `Active style (${_activeStyle})` : 'No style';
  styleSel.appendChild(oDefault);
  if (_activeStyle) {
    const oNone = document.createElement('option');
    oNone.value = 'none';
    oNone.textContent = 'No style';
    styleSel.appendChild(oNone);
  }
  _styles.forEach(s => {
    const o = document.createElement('option');
    o.value = s.name;
    o.textContent = s.name;
    styleSel.appendChild(o);
  });
  opt('Style', styleSel);

  if (_kind === 'video') {
    const dur = document.createElement('input');
    dur.type = 'number';
    dur.id = 'create-duration';
    dur.className = 'media-input';
    dur.min = '1'; dur.max = '16'; dur.step = '0.5'; dur.value = '5';
    dur.title = 'Seconds. Practical max ≈10s on LTX (24fps), ≈16s on Wan (16fps) — the engine clamps to 257 frames.';
    opt('Seconds', dur);
  } else {
    const size = document.createElement('select');
    size.id = 'create-size';
    size.className = 'settings-select';
    ['768x768', '512x512', '1024x1024', '1024x576'].forEach(s => {
      const o = document.createElement('option');
      o.value = s; o.textContent = s;
      size.appendChild(o);
    });
    opt('Size', size);
  }

  const seed = document.createElement('input');
  seed.type = 'number';
  seed.id = 'create-seed';
  seed.className = 'media-input';
  seed.placeholder = 'random';
  seed.title = 'Lock a seed to keep one consistent look across prompts (styles can also carry one)';
  opt('Seed', seed);

  host.appendChild(grid);

  // Go + status + inline image result
  const bar = _el('div', 'movie-bar');
  bar.appendChild(_el('span', 'grow'));
  const go = _el('button', 'confirm-btn confirm-btn-primary', 'Generate');
  go.id = 'create-go';
  go.type = 'button';
  go.addEventListener('click', () => _generate(host));
  bar.appendChild(go);
  host.appendChild(bar);

  const status = _el('div', 'movie-status');
  status.id = 'create-status';
  host.appendChild(status);

  const img = document.createElement('img');
  img.id = 'create-result';
  img.className = 'create-result';
  img.style.display = 'none';
  host.appendChild(img);

  _fillModelSelect();
}

export async function renderCreateTab(host) {
  if (!host) return;
  host.replaceChildren(_el('div', 'movie-empty', 'Loading models…'));
  try {
    await _loadOptions();
  } catch (_) { /* selects degrade to auto/none */ }
  _render(host);
}

export default { renderCreateTab };
