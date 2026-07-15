/**
 * Movie maker — stitch generated clips into one film (Studio → Movie).
 *
 * The clips are AI-generated, so this is deliberately not a timeline editor:
 * you pick clips, drag them into order, trim the heads and tails, and render.
 * Anything finer and you'd re-generate the clip instead.
 */

const API = '';

let _clips = [];      // the film, in order: {name, url, prompt, start, end, duration}
let _library = [];    // gallery videos available to add
let _building = false;
let _pollTimer = null;

function _el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

function _fmt(s) {
  if (!isFinite(s)) return '—';
  const m = Math.floor(s / 60), r = (s % 60);
  return m ? `${m}:${r.toFixed(1).padStart(4, '0')}` : `${r.toFixed(1)}s`;
}

// Total runtime of the film as currently arranged, honouring trims.
function _totalDuration() {
  return _clips.reduce((t, c) => {
    const start = c.start ?? 0;
    const end = c.end ?? c.duration ?? 0;
    return t + Math.max(0, end - start);
  }, 0);
}

async function _json(url, opts) {
  const r = await fetch(url, { credentials: 'same-origin', ...opts });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(d.detail || d.error || `Request failed (${r.status})`);
  return d;
}

async function _loadLibrary() {
  try {
    const d = await _json(`${API}/api/movie/clips`);
    _library = d.clips || [];
  } catch (e) {
    _library = [];
    throw e;
  }
}

// Durations come from the server (it has ffmpeg); without them we can't offer
// meaningful trim bounds.
async function _probe(clip) {
  if (clip.duration != null) return clip;
  try {
    const d = await _json(`${API}/api/movie/probe`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: clip.name }),
    });
    clip.duration = d.duration;
    clip.width = d.width;
    clip.height = d.height;
    clip.has_audio = d.has_audio;
  } catch (_) {
    clip.duration = null;
  }
  return clip;
}

function _renderPicker(host) {
  const wrap = _el('div', 'movie-picker');
  if (!_library.length) {
    wrap.appendChild(_el('div', 'movie-empty',
      'No video clips in the Studio yet — generate one with /video first.'));
    return wrap;
  }
  _library.forEach(item => {
    const card = _el('button', 'movie-pick-card');
    card.type = 'button';
    card.title = item.prompt || item.name;
    const v = document.createElement('video');
    v.src = item.url;
    v.muted = true;
    v.preload = 'metadata';
    v.addEventListener('mouseenter', () => { v.play().catch(() => {}); });
    v.addEventListener('mouseleave', () => { v.pause(); v.currentTime = 0; });
    card.appendChild(v);
    card.appendChild(_el('span', 'movie-pick-label', item.prompt || item.name));
    card.addEventListener('click', async () => {
      const clip = { ...item, start: null, end: null, duration: null };
      _clips.push(clip);
      _render(host);
      await _probe(clip);
      _render(host);
    });
    wrap.appendChild(card);
  });
  return wrap;
}

function _renderTimeline(host) {
  const list = _el('div', 'movie-timeline');
  if (!_clips.length) {
    list.appendChild(_el('div', 'movie-empty',
      'Your film is empty — click clips below to add them. They play in this order.'));
    return list;
  }

  _clips.forEach((c, i) => {
    const row = _el('div', 'movie-row');
    row.draggable = true;
    row.dataset.index = String(i);

    row.appendChild(_el('span', 'movie-row-num', String(i + 1)));

    const v = document.createElement('video');
    v.className = 'movie-row-thumb';
    v.src = c.url;
    v.muted = true;
    v.preload = 'metadata';
    row.appendChild(v);

    const mid = _el('div', 'movie-row-mid');
    mid.appendChild(_el('div', 'movie-row-title', c.prompt || c.name));
    const meta = _el('div', 'movie-row-meta');
    meta.textContent = c.duration != null
      ? `${_fmt(c.duration)}${c.has_audio ? ' · audio' : ''}`
      : 'reading…';
    mid.appendChild(meta);
    row.appendChild(mid);

    // Trim. Empty means "use the whole clip" — that's why they're nullable
    // rather than defaulted to 0/duration.
    const trim = _el('div', 'movie-row-trim');
    [['start', 'from'], ['end', 'to']].forEach(([key, label]) => {
      const inp = document.createElement('input');
      inp.type = 'number';
      inp.step = '0.1';
      inp.min = '0';
      if (c.duration != null) inp.max = String(c.duration);
      inp.placeholder = label;
      inp.className = 'movie-trim-input';
      inp.value = c[key] ?? '';
      inp.title = key === 'start' ? 'Trim the head (seconds)' : 'Trim the tail (seconds)';
      inp.addEventListener('change', () => {
        const v2 = inp.value === '' ? null : Number(inp.value);
        c[key] = (v2 == null || isNaN(v2)) ? null : v2;
        _render(host);
      });
      trim.appendChild(inp);
    });
    row.appendChild(trim);

    const acts = _el('div', 'movie-row-acts');
    const up = _el('button', 'movie-icon-btn', '↑');
    up.title = 'Move earlier';
    up.disabled = i === 0;
    up.addEventListener('click', () => {
      [_clips[i - 1], _clips[i]] = [_clips[i], _clips[i - 1]];
      _render(host);
    });
    const down = _el('button', 'movie-icon-btn', '↓');
    down.title = 'Move later';
    down.disabled = i === _clips.length - 1;
    down.addEventListener('click', () => {
      [_clips[i + 1], _clips[i]] = [_clips[i], _clips[i + 1]];
      _render(host);
    });
    const del = _el('button', 'movie-icon-btn', '✕');
    del.title = 'Remove from film';
    del.addEventListener('click', () => { _clips.splice(i, 1); _render(host); });
    acts.append(up, down, del);
    row.appendChild(acts);

    // Drag to reorder — the arrows stay for keyboard/accessibility.
    row.addEventListener('dragstart', e => {
      e.dataTransfer.setData('text/plain', String(i));
      row.classList.add('dragging');
    });
    row.addEventListener('dragend', () => row.classList.remove('dragging'));
    row.addEventListener('dragover', e => { e.preventDefault(); row.classList.add('drop-target'); });
    row.addEventListener('dragleave', () => row.classList.remove('drop-target'));
    row.addEventListener('drop', e => {
      e.preventDefault();
      row.classList.remove('drop-target');
      const from = Number(e.dataTransfer.getData('text/plain'));
      if (isNaN(from) || from === i) return;
      const [moved] = _clips.splice(from, 1);
      _clips.splice(i, 0, moved);
      _render(host);
    });

    list.appendChild(row);
  });
  return list;
}

async function _build(host, statusEl) {
  if (_building || !_clips.length) return;
  _building = true;
  _render(host);
  const title = (document.getElementById('movie-title')?.value || '').trim();
  try {
    const body = {
      title,
      clips: _clips.map(c => ({ name: c.name, start: c.start, end: c.end })),
    };
    const d = await _json(`${API}/api/movie/build`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    _poll(d.job_id, host);
  } catch (e) {
    _building = false;
    _render(host);
    const s = document.getElementById('movie-status');
    if (s) { s.textContent = e.message; s.className = 'movie-status movie-status-err'; }
  }
}

function _poll(jobId, host) {
  clearInterval(_pollTimer);
  _pollTimer = setInterval(async () => {
    let job = null;
    try {
      const d = await _json(`${API}/api/queue`);
      job = (d.jobs || []).find(j => j.id === jobId);
    } catch (_) { return; }
    const s = document.getElementById('movie-status');
    if (!job) return;
    if (job.status === 'done') {
      clearInterval(_pollTimer);
      _building = false;
      _render(host);
      const s2 = document.getElementById('movie-status');
      if (s2) {
        s2.className = 'movie-status movie-status-ok';
        s2.textContent = '';
        s2.appendChild(_el('span', '', 'Film ready — it\'s in your Studio. '));
        const a = document.createElement('a');
        a.href = job.result_url;
        a.textContent = 'Open';
        a.target = '_blank';
        s2.appendChild(a);
      }
      window.dispatchEvent(new CustomEvent('gallery-refresh', { detail: { source: 'movie' } }));
    } else if (job.status === 'error') {
      clearInterval(_pollTimer);
      _building = false;
      _render(host);
      const s2 = document.getElementById('movie-status');
      if (s2) { s2.className = 'movie-status movie-status-err'; s2.textContent = job.error || 'Build failed'; }
    } else if (s) {
      s.className = 'movie-status';
      s.textContent = job.detail || 'Stitching…';
    }
  }, 1200);
}

function _render(host) {
  if (!host) return;
  host.replaceChildren();

  const head = _el('div', 'movie-head');
  head.appendChild(_el('h2', '', 'Movie maker'));
  head.appendChild(_el('div', 'admin-toggle-sub',
    'Stitch your generated clips into one film. Drag to reorder, trim the heads '
    + 'and tails, and render. Clips of different sizes or framerates are matched '
    + 'automatically; a locked style keeps the look consistent across shots.'));
  host.appendChild(head);

  host.appendChild(_renderTimeline(host));

  const bar = _el('div', 'movie-bar');
  const total = _el('span', 'movie-total');
  total.textContent = _clips.length
    ? `${_clips.length} clip${_clips.length > 1 ? 's' : ''} · ${_fmt(_totalDuration())}`
    : '';
  bar.appendChild(total);
  bar.appendChild(_el('span', 'grow'));

  const title = document.createElement('input');
  title.id = 'movie-title';
  title.type = 'text';
  title.className = 'media-input';
  title.placeholder = 'Film title (optional)';
  bar.appendChild(title);

  const build = _el('button', 'confirm-btn confirm-btn-primary',
    _building ? 'Building…' : 'Build movie');
  build.type = 'button';
  build.disabled = _building || !_clips.length;
  build.addEventListener('click', () => _build(host));
  bar.appendChild(build);
  host.appendChild(bar);

  const status = _el('div', 'movie-status');
  status.id = 'movie-status';
  host.appendChild(status);

  const pickHead = _el('h2', 'movie-sub', 'Add clips');
  host.appendChild(pickHead);
  host.appendChild(_renderPicker(host));
}

export async function renderMovieTab(host) {
  if (!host) return;
  host.replaceChildren(_el('div', 'movie-empty', 'Loading clips…'));
  try {
    await _loadLibrary();
  } catch (e) {
    host.replaceChildren(_el('div', 'movie-empty', 'Could not load clips: ' + e.message));
    return;
  }
  _render(host);
}

export function stopMovieTab() {
  clearInterval(_pollTimer);
  _pollTimer = null;
}

export default { renderMovieTab, stopMovieTab };
