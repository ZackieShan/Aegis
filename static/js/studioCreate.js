/**
 * Studio maker panels: generate images, clips, and songs where the results live.
 *
 * One rendering engine, three explicit makers — the Image tab, the Music tab's
 * composer, and the Movie tab's clip maker each call renderCreateTab(host, kind)
 * with their kind locked, so every control for that medium is visible in that
 * tab (no mode pills to discover). Legacy no-lock mode keeps the pills.
 *
 * The full flow without leaving the Studio: describe it (✨ can rewrite rough
 * intent into a diffusion-ready scene), pick model/style/size/steps/seed,
 * generate — images resolve inline, videos and songs hand off to the Queue tab,
 * and everything lands in the Media tab where the Movie Maker can pick it up.
 *
 * DOM NOTE: several maker hosts can exist at once (hidden tabs keep their
 * DOM), so every lookup is scoped to the active host via _$() — never
 * document.getElementById, which would find a stale twin in another tab.
 */

let _kind = 'video';          // 'image' | 'video' | 'audio' — video first: it's the flagship
let _lock = null;             // when set, the panel is pinned to one kind (maker mode)
let _host = null;             // the host element of the most recent render
let _models = { image: [], video: [], audio: [] };
let _styles = [];
let _activeStyle = '';
let _busy = false;
let _source = null;           // {id, url, prompt} — a Photos still to animate/stylize
let _songRef = null;          // {name, seconds, label} staged upload | {last:true} — cover mode

/** Hand a Photos still to a maker tab (the Animate/Stylize buttons).
 *  Call before switching to the tab — renderCreateTab reads it. */
export function setCreateSource(source, kind) {
  _source = source && source.id ? source : null;
  if (kind === 'image' || kind === 'video') _kind = kind;
}

const _EDIT_MODEL_RE = /edit|inpaint|fill/i;

// Guards against overlapping async renders: each renderCreateTab call takes a
// token; a render that lost the race (user already clicked another maker tab)
// aborts instead of stomping _host/_kind for the visible panel.
let _renderGen = 0;

function _$(id, root) {
  const scope = root || _host;
  return scope ? scope.querySelector('#' + id) : document.getElementById(id);
}

function _statusIn(root, msg, cls) {
  const s = _$('create-status', root);
  if (!s) return;
  s.className = 'movie-status' + (cls ? ` ${cls}` : '');
  s.replaceChildren();
  if (msg) s.appendChild(typeof msg === 'string' ? document.createTextNode(msg) : msg);
}

function _kindModels() {
  const all = _models[_kind] || [];
  if (_kind === 'audio') return all;
  if (!_source) {
    // Editing-only models can't generate from scratch — hide them until a
    // source photo is attached.
    return _kind === 'image' ? all.filter(m => !_EDIT_MODEL_RE.test(m.model)) : all;
  }
  if (_kind === 'video') {
    return all.filter(m => m.i2v !== undefined ? m.i2v : /(?:^|[/\-_.])(?:ltx[0-9.]*|i2v)(?:$|[/\-_.])/i.test(m.model));
  }
  return all.filter(m => _EDIT_MODEL_RE.test(m.model));
}

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
  const [img, vid, aud, sty] = await Promise.allSettled([
    _json('/api/image/models'),
    _json('/api/video/models'),
    _json('/api/audio/models'),
    _json('/api/styles'),
  ]);
  _models.image = img.status === 'fulfilled' ? (img.value.models || []) : [];
  _models.video = vid.status === 'fulfilled' ? (vid.value.models || []) : [];
  _models.audio = aud.status === 'fulfilled' ? (aud.value.models || []) : [];
  _styles = sty.status === 'fulfilled' ? (sty.value.styles || []) : [];
  _activeStyle = sty.status === 'fulfilled' ? (sty.value.active || '') : '';
}

function _status(msg, cls) {
  _statusIn(_host, msg, cls);
}

async function _enhance(host, kind) {
  // Bound to the ORIGINATING panel: the user can switch maker tabs while the
  // rewrite is in flight, and feedback must not land in the other tab.
  const ta = _$('create-prompt', host);
  const btn = _$('create-enhance', host);
  const rough = (ta?.value || '').trim();
  if (!rough || !btn || btn.disabled) return;
  btn.disabled = true;
  const orig = btn.textContent;
  btn.textContent = 'Enhancing…';
  try {
    const d = await _json('/api/image/enhance-prompt', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ prompt: rough, kind }),
    });
    if (d.ok && d.prompt) {
      // Write the draft FIRST, and re-look-up the textarea in this host: every
      // _render() rebuilds it from the draft, and programmatic .value writes
      // don't fire the input listener — without both, a re-render while this
      // request was in flight silently reverts to the rough prompt and
      // Generate submits text the user isn't looking at.
      window.__createPromptDraft = d.prompt;
      const live = _$('create-prompt', host);
      if (live) live.value = d.prompt;
      _statusIn(host, `Rewritten by ${d.model} — edit freely, then Generate.`);
    } else {
      _statusIn(host, d.error || 'Enhance failed', 'movie-status-err');
    }
  } catch (e) {
    _statusIn(host, 'Enhance failed: ' + e.message, 'movie-status-err');
  } finally {
    btn.disabled = false;
    btn.textContent = orig;
  }
}

async function _generate(host, kind) {
  // Everything below is bound to the ORIGINATING panel (host) and the kind it
  // was rendered with: the user can switch maker tabs mid-generate, and both
  // the field reads and the completion output must stay in this panel.
  if (_busy) {
    _statusIn(host, 'Another generation is already running — wait for it to finish.', 'movie-status-err');
    return;
  }
  const prompt = (_$('create-prompt', host)?.value || '').trim();
  if (!prompt) { _statusIn(host, 'Describe what you want first.', 'movie-status-err'); return; }

  const model = _$('create-model', host)?.value || '';
  const style = _$('create-style', host)?.value || '';
  const seedRaw = _$('create-seed', host)?.value || '';
  const body = { prompt };
  if (model) body.model = model;
  // '' = let the backend fall back to the active style; 'none' disables it.
  if (style) body.style = style;
  if (seedRaw !== '' && !isNaN(Number(seedRaw))) body.seed = Number(seedRaw);

  _busy = true;
  const btn = _$('create-go', host);
  if (btn) { btn.disabled = true; btn.textContent = 'Generating…'; }

  try {
    if (kind === 'audio') {
      const durRaw = (_$('create-duration', host)?.value || '').trim();
      const payload = {
        tags: prompt,
        lyrics: (_$('create-lyrics', host)?.value || '').trim(),
        seed: body.seed,
        model: body.model,
      };
      // An empty Seconds with a reference means "match the reference" — the
      // backend probes the track's real length; only send an explicit value.
      if (durRaw !== '' && !isNaN(Number(durRaw))) {
        payload.seconds = Math.max(10, Math.min(600, Number(durRaw)));
      } else if (!_songRef) {
        payload.seconds = 60;
      }
      if (_songRef) {
        if (_songRef.last) payload.reference_id = 'last';
        else payload.reference_name = _songRef.name;
      }
      const d = await _json('/api/audio/generate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      _statusIn(host, `Queued on ${d.model} — a ${Math.round(d.seconds)}s ${d.cover ? 'cover' : (d.has_lyrics ? 'song' : 'instrumental')} lands here when it finishes.`);
      window.dispatchEvent(new CustomEvent('studio-job-queued', { detail: { kind: 'audio' } }));
      document.querySelector('#gallery-modal .gallery-tab[data-tab="queue"]')?.click();
    } else if (kind === 'video') {
      const dur = Number(_$('create-duration', host)?.value || 5);
      body.duration = Math.max(1, Math.min(16, isNaN(dur) ? 5 : dur));
      if (_source) body.image_id = _source.id;
      const d = await _json('/api/video/generate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      _statusIn(host, `Queued on ${d.model} — ${d.duration_s}s at ${d.fps}fps.`);
      // The render is now the Queue tab's story; take the user to it.
      document.querySelector('#gallery-modal .gallery-tab[data-tab="queue"]')?.click();
    } else if (_source) {
      // Stylize the source photo — an async instruction edit, queued like a
      // video render (the first run swaps in + cold-loads the edit model,
      // which can take minutes; a held request would just time out).
      body.image_id = _source.id;
      const d = await _json('/api/image/edit', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      _statusIn(host, `Queued on ${d.model} — the edited photo lands in Media when it finishes.`);
      document.querySelector('#gallery-modal .gallery-tab[data-tab="queue"]')?.click();
    } else {
      const size = _$('create-size', host)?.value || '';
      if (size) body.size = size;
      const stepsRaw = (_$('create-steps', host)?.value || '').trim();
      if (stepsRaw !== '' && !isNaN(Number(stepsRaw))) {
        body.steps = Math.max(1, Math.min(100, Math.round(Number(stepsRaw))));
      }
      const negative = (_$('create-negative', host)?.value || '').trim();
      if (negative) body.negative_prompt = negative;
      _statusIn(host, 'Rendering… the first run also loads the model, give it a minute.');
      const d = await _json('/api/image/generate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (d.error) { _statusIn(host, d.error, 'movie-status-err'); return; }
      const wrap = _el('span');
      wrap.appendChild(document.createTextNode('Done — saved to Media. '));
      const a = document.createElement('a');
      a.href = d.image_url; a.target = '_blank'; a.textContent = 'Open';
      wrap.appendChild(a);
      _statusIn(host, wrap, 'movie-status-ok');
      const img = _$('create-result', host);
      if (img) { img.src = d.image_url; img.style.display = ''; }
      window.dispatchEvent(new CustomEvent('gallery-refresh', { detail: { source: 'studio-create' } }));
    }
  } catch (e) {
    _statusIn(host, e.message, 'movie-status-err');
  } finally {
    _busy = false;
    if (btn) { btn.disabled = false; btn.textContent = 'Generate'; }
  }
}

function _fillModelSelect() {
  const sel = _$('create-model');
  if (!sel) return;
  sel.replaceChildren();
  const auto = document.createElement('option');
  auto.value = '';
  auto.textContent = '— auto —';
  sel.appendChild(auto);
  _kindModels().forEach(m => {
    const o = document.createElement('option');
    o.value = m.model;
    o.textContent = m.label || (m.endpoint === 'comfyui' ? `${m.model} (ComfyUI)` : m.model);
    sel.appendChild(o);
  });
}

const _MAKER_COPY = {
  image: ['Image Maker',
    'Every image control in one place: describe the scene, pick a model and '
    + 'style, set size / steps / seed / negative prompt, and Generate. The ✨ '
    + 'button rewrites rough ideas into something a diffusion model can draw.'],
  video: ['Clip Maker',
    'Generate video clips here — they land in Media and can be stitched into '
    + 'a film below. Describe the scene and motion, not the task; the ✨ button '
    + 'rewrites rough ideas for you.'],
  audio: ['Song Composer',
    'Write a song: style tags up top, optional lyrics below ([Verse]/[Chorus] '
    + 'markers steer the structure — empty lyrics means instrumental). Add a '
    + 'reference track to make a cover that follows its melody.'],
};

function _render(host) {
  _host = host;
  host.replaceChildren();

  // Everything this panel's buttons do is bound to the host + kind it was
  // rendered with — never to the module state at click time, which may belong
  // to another maker tab by then.
  const kindNow = _kind;
  const locked = !!_lock;
  const [title, blurb] = locked
    ? _MAKER_COPY[_kind]
    : ['Create',
      'Generate images and clips right here. Describe the scene — not the task: '
      + '"a lone rider crossing red desert at dusk, painted anime style" beats '
      + '"make me a movie trailer" (the model will literally draw a screen). '
      + 'The ✨ button rewrites rough ideas for you.'];

  const head = _el('div', 'movie-head');
  head.appendChild(_el('h2', '', title));
  head.appendChild(_el('div', 'admin-toggle-sub', blurb));
  host.appendChild(head);

  // Type toggle — legacy unlocked mode only; maker tabs pin the kind.
  if (!locked) {
    const pills = _el('div', 'create-pills');
    [['video', 'Video'], ['image', 'Image'], ['audio', 'Song']].forEach(([k, label]) => {
      const b = _el('button', 'create-pill' + (k === _kind ? ' active' : ''), label);
      b.type = 'button';
      // Bump the render token so an in-flight maker render can't stomp this.
      b.addEventListener('click', () => { _kind = k; _renderGen++; _render(host); });
      pills.appendChild(b);
    });
    host.appendChild(pills);
  }

  // Source photo (set by the Photos view's Animate/Stylize buttons): the
  // prompt now describes what to DO to this image — motion for video,
  // an edit instruction for image. Songs take no source photo.
  if (_source && _kind !== 'audio') {
    const strip = _el('div', 'create-source');
    const thumb = document.createElement('img');
    thumb.className = 'create-source-thumb';
    thumb.src = _source.url;
    thumb.alt = 'Source photo';
    strip.appendChild(thumb);
    const label = _el('div', 'create-source-label',
      _kind === 'video'
        ? 'Animating this photo — describe the motion below.'
        : 'Stylizing this photo — describe the change below.');
    strip.appendChild(label);
    const clear = _el('button', 'gallery-detail-back', '✕ Clear');
    clear.type = 'button';
    clear.title = 'Generate from scratch instead';
    clear.addEventListener('click', () => { _source = null; _render(host); });
    strip.appendChild(clear);
    host.appendChild(strip);
  }

  // Prompt + enhance
  const ta = document.createElement('textarea');
  ta.id = 'create-prompt';
  ta.className = 'create-prompt';
  ta.rows = 3;
  ta.placeholder = _kind === 'audio'
    ? 'dreamy indie pop, female vocals, 100 BPM, warm acoustic guitar, soft drums, nostalgic summer feel…'
    : _source
      ? (_kind === 'video'
        ? 'subtle camera push-in, natural motion, cinematic…'
        : 'turn it into a hand-painted watercolor illustration, soft pastel palette…')
      : (_kind === 'video'
        ? 'A lone rider crossing a red-rock desert at dusk, long shadows, slow tracking shot, hand-painted anime style with soft watercolor skies…'
        : 'A weathered cowboy portrait at golden hour, warm rim light, painterly anime style, shallow depth of field…');
  ta.value = window.__createPromptDraft || '';
  ta.addEventListener('input', () => { window.__createPromptDraft = ta.value; });
  host.appendChild(ta);

  // Song lyrics — optional; empty means instrumental. [Verse]/[Chorus]
  // section markers steer the structure.
  if (_kind === 'audio') {
    const ly = document.createElement('textarea');
    ly.id = 'create-lyrics';
    ly.className = 'create-prompt';
    ly.rows = 5;
    ly.placeholder = 'Lyrics (optional — leave empty for an instrumental)\n[Verse 1]\n…\n[Chorus]\n…';
    ly.value = window.__createLyricsDraft || '';
    ly.addEventListener('input', () => { window.__createLyricsDraft = ly.value; });
    host.appendChild(ly);

    // Cover mode: hand ACE-Step a reference track — the new song follows its
    // melody/structure/feel (with your tags + lyrics on top).
    const refRow = _el('div', 'create-source');
    const refLabel = _el('div', 'create-source-label',
      _songRef
        ? (_songRef.last ? 'Covering your latest Studio song.' : `Covering: ${_songRef.label} (${_songRef.seconds ? Math.round(_songRef.seconds) + 's' : 'length unknown'})`)
        : 'No reference — composing from scratch. Add a track to make a cover.');
    refRow.appendChild(refLabel);
    if (_songRef) {
      const clear = _el('button', 'gallery-detail-back', '✕ Clear');
      clear.type = 'button';
      clear.addEventListener('click', () => { _songRef = null; _render(host); });
      refRow.appendChild(clear);
    } else {
      const useLast = _el('button', 'gallery-detail-back', 'Latest song');
      useLast.type = 'button';
      useLast.title = 'Cover the newest track in your Studio';
      useLast.addEventListener('click', () => { _songRef = { last: true }; _render(host); });
      refRow.appendChild(useLast);
      const up = _el('button', 'gallery-detail-back', 'Upload track…');
      up.type = 'button';
      const fileIn = document.createElement('input');
      fileIn.type = 'file';
      fileIn.accept = '.mp3,.wav,.flac,.ogg,.opus,.m4a,audio/*';
      fileIn.style.display = 'none';
      fileIn.addEventListener('change', async () => {
        const f = fileIn.files && fileIn.files[0];
        if (!f) return;
        up.disabled = true; up.textContent = 'Uploading…';
        try {
          const fd = new FormData();
          fd.append('file', f, f.name);
          const r = await fetch('/api/audio/reference', { method: 'POST', credentials: 'same-origin', body: fd });
          const d = await r.json().catch(() => ({}));
          if (!r.ok) throw new Error(d.detail || `Upload failed (${r.status})`);
          _songRef = { name: d.name, seconds: d.seconds, label: f.name };
          _render(host);  // the duration input reads _songRef.seconds
        } catch (e) {
          _status('Reference upload failed: ' + e.message, 'movie-status-err');
          up.disabled = false; up.textContent = 'Upload track…';
        }
      });
      up.addEventListener('click', () => fileIn.click());
      refRow.appendChild(up);
      refRow.appendChild(fileIn);
    }
    host.appendChild(refRow);
  }

  if (_kind !== 'audio') {
    const enhanceRow = _el('div', 'create-row');
    const enh = _el('button', 'memory-toolbar-btn', '✨ Enhance prompt');
    enh.id = 'create-enhance';
    enh.type = 'button';
    enh.title = 'Rewrite rough intent into a scene description the model can actually draw (local utility model)';
    enh.addEventListener('click', () => _enhance(host, kindNow));
    enhanceRow.appendChild(enh);
    host.appendChild(enhanceRow);
  }

  // Negative prompt — image only, full-width above the options grid.
  if (_kind === 'image' && !_source) {
    const neg = document.createElement('input');
    neg.type = 'text';
    neg.id = 'create-negative';
    neg.className = 'create-prompt create-negative';
    neg.placeholder = 'Negative prompt (optional) — what to avoid: blurry, extra fingers, watermark, text…';
    neg.title = 'Things the model should steer away from';
    neg.value = window.__createNegativeDraft || '';
    neg.addEventListener('input', () => { window.__createNegativeDraft = neg.value; });
    host.appendChild(neg);
  }

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

  if (_kind !== 'audio') {
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
  }

  if (_kind === 'audio') {
    const dur = document.createElement('input');
    dur.type = 'number';
    dur.id = 'create-duration';
    dur.className = 'media-input';
    dur.min = '10'; dur.max = '600'; dur.step = '5';
    // Covers default to the reference's length (the model truncates/pads the
    // reference to the target). _render rebuilds this input, so the value
    // must come from state — a direct .value write would be wiped.
    if (_songRef && _songRef.seconds) {
      dur.value = String(Math.round(Math.min(600, _songRef.seconds)));
    } else if (_songRef) {
      dur.value = '';
      dur.placeholder = 'reference length';
    } else {
      dur.value = '60';
    }
    dur.title = 'Song length in seconds — ACE-Step writes full structure (intro/verse/chorus) into whatever you give it. Empty with a reference = match the reference.';
    opt('Seconds', dur);
  } else if (_kind === 'video') {
    const dur = document.createElement('input');
    dur.type = 'number';
    dur.id = 'create-duration';
    dur.className = 'media-input';
    dur.min = '1'; dur.max = '16'; dur.step = '0.5'; dur.value = '5';
    dur.title = 'Seconds. Practical max ≈10s on LTX/HunyuanVideo (24fps), ≈16s on Wan (16fps) — the engine clamps to 257 frames.';
    opt('Seconds', dur);
  } else if (!_source) {
    // Edits keep the source photo's dimensions — a size picker would lie.
    const size = document.createElement('select');
    size.id = 'create-size';
    size.className = 'settings-select';
    ['768x768', '512x512', '1024x1024', '1024x576'].forEach(s => {
      const o = document.createElement('option');
      o.value = s; o.textContent = s;
      size.appendChild(o);
    });
    opt('Size', size);

    const steps = document.createElement('input');
    steps.type = 'number';
    steps.id = 'create-steps';
    steps.className = 'media-input';
    steps.min = '1'; steps.max = '100'; steps.step = '1';
    steps.placeholder = 'auto';
    steps.title = 'Sampling steps — more = slower but cleaner. Leave empty for the model default (step-distilled models like Rapid want 8).';
    opt('Steps', steps);
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
  go.addEventListener('click', () => _generate(host, kindNow));
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

/** Render the maker panel. `lockKind` pins it to 'image' | 'video' | 'audio'
 *  (maker mode: no pills, kind-specific heading); omit for the legacy
 *  pill-switcher panel. */
export async function renderCreateTab(host, lockKind) {
  if (!host) return;
  const gen = ++_renderGen;
  const lock = (lockKind === 'image' || lockKind === 'video' || lockKind === 'audio') ? lockKind : null;
  const kind = lock || _kind;
  host.replaceChildren(_el('div', 'movie-empty', 'Loading models…'));
  try {
    await _loadOptions();
  } catch (_) { /* selects degrade to auto/none */ }
  // A newer maker render started while our model lists loaded — that one owns
  // the module state now; rendering here would repaint a hidden tab with the
  // wrong kind and steal _host from the visible panel.
  if (gen !== _renderGen) return;
  _lock = lock;
  _kind = kind;
  _render(host);
}

export default { renderCreateTab };
