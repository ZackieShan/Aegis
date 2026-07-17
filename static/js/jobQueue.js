/**
 * Queue panel — what's running, how far along, and what's next.
 *
 * Covers every slow thing: renders, films, recipe automations, research. Only
 * media renders contend for the GPU, but one honest "what is this machine doing"
 * list is where people look, so they all land here.
 */

let _timer = null;
let _host = null;

const KIND_LABEL = {
  video: 'Video',
  image: 'Image',
  image_edit: 'Edit',
  audio: 'Song',
  movie: 'Film',
  recipe: 'Automation',
  research: 'Research',
};

function _el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

function _ago(ts) {
  if (!ts) return '';
  const s = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  return `${Math.floor(s / 3600)}h`;
}

async function _fetchQueue() {
  const r = await fetch('/api/queue', { credentials: 'same-origin' });
  if (!r.ok) throw new Error(`Queue unavailable (${r.status})`);
  return r.json();
}

async function _cancel(id) {
  try {
    await fetch(`/api/queue/${encodeURIComponent(id)}/cancel`, {
      method: 'POST', credentials: 'same-origin',
    });
  } catch (_) {}
  _refresh();
}

async function _clear() {
  try {
    await fetch('/api/queue/clear', { method: 'POST', credentials: 'same-origin' });
  } catch (_) {}
  _refresh();
}

function _row(job) {
  const live = !['done', 'error', 'cancelled'].includes(job.status);
  const row = _el('div', `q-row q-${job.status}`);

  const kind = _el('span', 'q-kind', KIND_LABEL[job.kind] || job.kind);
  if (job.gpu) kind.classList.add('q-kind-gpu');
  row.appendChild(kind);

  const mid = _el('div', 'q-mid');
  mid.appendChild(_el('div', 'q-title', job.title || '(untitled)'));

  const sub = _el('div', 'q-sub');
  if (job.status === 'running') {
    sub.textContent = job.detail
      || (job.progress != null ? `${Math.round(job.progress * 100)}%` : 'running…');
  } else if (job.status === 'queued') {
    // position is 0 for the head of the queue — "next up" reads better than "#1".
    sub.textContent = job.position === 0 ? 'next up' : `#${(job.position ?? 0) + 1} in line`;
  } else if (job.status === 'error') {
    sub.textContent = job.error || 'failed';
  } else if (job.status === 'cancelled') {
    sub.textContent = 'cancelled';
  } else {
    sub.textContent = `done ${_ago(job.finished)} ago`;
  }
  mid.appendChild(sub);

  if (job.status === 'running' && job.progress != null) {
    const bar = _el('div', 'q-bar');
    const fill = _el('div', 'q-bar-fill');
    fill.style.width = `${Math.round(job.progress * 100)}%`;
    bar.appendChild(fill);
    mid.appendChild(bar);
  }
  row.appendChild(mid);

  const acts = _el('div', 'q-acts');
  if (job.status === 'done' && job.result_url) {
    const a = document.createElement('a');
    a.className = 'q-link';
    a.href = job.result_url;
    a.target = '_blank';
    a.textContent = 'Open';
    acts.appendChild(a);
  }
  if (live) {
    const x = _el('button', 'q-cancel', 'Cancel');
    x.type = 'button';
    x.addEventListener('click', () => _cancel(job.id));
    acts.appendChild(x);
  }
  row.appendChild(acts);
  return row;
}

function _render(data) {
  if (!_host) return;
  _host.replaceChildren();

  const head = _el('div', 'q-head');
  head.appendChild(_el('h2', '', 'Queue'));
  head.appendChild(_el('div', 'admin-toggle-sub',
    'Everything slow, in one place — renders, films, automations and research. '
    + 'Media renders run one at a time; they share a single GPU.'));
  _host.appendChild(head);

  if (data.gpu_busy && data.gpu_job) {
    const note = _el('div', 'q-gpu-note');
    note.textContent =
      `GPU busy — ${KIND_LABEL[data.gpu_job.kind] || data.gpu_job.kind}: `
      + `“${data.gpu_job.title}”. Chat replies on a small CPU model until it finishes, `
      + `so the render survives.`;
    _host.appendChild(note);
  }

  const jobs = data.jobs || [];
  if (!jobs.length) {
    _host.appendChild(_el('div', 'q-empty', 'Nothing running. Generate an image or video and it shows up here.'));
    return;
  }

  const live = jobs.filter(j => !['done', 'error', 'cancelled'].includes(j.status));
  const past = jobs.filter(j => ['done', 'error', 'cancelled'].includes(j.status));

  const list = _el('div', 'q-list');
  live.forEach(j => list.appendChild(_row(j)));
  _host.appendChild(list);

  if (past.length) {
    const h = _el('div', 'q-past-head');
    h.appendChild(_el('span', '', 'Recent'));
    const clear = _el('button', 'memory-toolbar-btn', 'Clear');
    clear.type = 'button';
    clear.addEventListener('click', _clear);
    h.appendChild(clear);
    _host.appendChild(h);

    const plist = _el('div', 'q-list');
    past.forEach(j => plist.appendChild(_row(j)));
    _host.appendChild(plist);
  }
}

async function _refresh() {
  if (!_host) return;
  try {
    _render(await _fetchQueue());
  } catch (e) {
    _host.replaceChildren(_el('div', 'q-empty', e.message));
  }
}

export function renderQueueTab(host) {
  _host = host;
  _refresh();
  clearInterval(_timer);
  // Cheap poll — the panel is only mounted while its tab is open, and
  // stopQueueTab() tears this down on the way out.
  _timer = setInterval(_refresh, 2000);
}

export function stopQueueTab() {
  clearInterval(_timer);
  _timer = null;
  _host = null;
}

export default { renderQueueTab, stopQueueTab };
