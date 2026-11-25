/**
 * Studio → Create: one place to make a picture, a video, a song or a voice.
 *
 * The Studio used to spread making things over three tabs (pictures under
 * "Image", video under "Movie", songs under "Music"), each a form of raw
 * controls that sent you off to the Queue tab and left the result for you to
 * find. This is the front door instead:
 *
 *   1. pick what to make;
 *   2. describe it — everything else has a sensible default, and the rest is
 *      under "More options";
 *   3. press the button and stay here: the work shows its progress in place,
 *      and the result appears where you are, with what you can do next
 *      (animate it, change it, make another, open it, save it).
 *
 * Nothing new happens on the server: the same endpoints as the older makers
 * (/api/image/generate, /api/image/edit, /api/video/generate,
 * /api/audio/generate, /api/tts/synthesize), the shared job status
 * (/api/video/status/{id}) and the queue (/api/queue).
 *
 * State lives in this module, so it survives switching tabs and closing the
 * Studio: each kind keeps its own draft, and work in progress keeps being
 * followed.
 */

const KINDS = [
  { key: 'image', label: 'Picture', icon: '<rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="8.5" cy="9.5" r="1.5"/><path d="M21 16l-5-5L5 20"/>' },
  { key: 'video', label: 'Video', icon: '<rect x="3" y="5" width="14" height="14" rx="2"/><path d="M17 10l4-2.5v9L17 14"/>' },
  { key: 'audio', label: 'Song', icon: '<path d="M9 18V5l11-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="17" cy="16" r="3"/>' },
  { key: 'voice', label: 'Voice', icon: '<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3"/>' },
];
const COPY = {
  image: { ask: 'Describe the picture', hint: 'A lighthouse on a cliff at sunset, waves below, oil painting', go: 'Make the picture', noun: 'picture' },
  edit: { ask: 'Describe the change', hint: 'Make it winter, with snow on the roof', go: 'Change the picture', noun: 'picture' },
  video: { ask: 'Describe the video', hint: 'Waves rolling onto a rocky shore at dawn, slow camera pan', go: 'Make the video', noun: 'video' },
  animate: { ask: 'Describe how it should move', hint: 'The clouds drift, the water ripples, slow zoom in', go: 'Animate the picture', noun: 'video' },
  audio: { ask: 'What kind of song?', hint: 'Upbeat folk, banjo and fiddle, warm female vocals', go: 'Make the song', noun: 'song' },
  voice: { ask: 'What should it say?', hint: 'Good morning! Coffee is ready and it is going to be a sunny day.', go: 'Speak it', noun: 'recording' },
};
const SHAPES = {
  image: [['square', 'Square', [768, 768], [1024, 1024]], ['wide', 'Wide', [1024, 576], [1344, 768]], ['tall', 'Tall', [576, 1024], [768, 1344]]],
  video: [['wide', 'Wide', [832, 480]], ['tall', 'Tall', [480, 832]], ['square', 'Square', [624, 624]]],
};
const LENGTHS = { video: [[3, '3 s'], [5, '5 s'], [8, '8 s']], audio: [[30, '30 s'], [60, '1 min'], [120, '2 min'], [180, '3 min']] };
const EDIT_MODEL = /edit|inpaint|fill/i;
const KEEP_RESULTS = 12;

const S = {
  kind: 'image',
  drafts: {
    image: { prompt: '', shape: 'square', shapeChosen: false, big: false, style: '', model: '', negative: '', steps: '', seed: '' },
    video: { prompt: '', shape: 'wide', seconds: 5, style: '', model: '', seed: '' },
    audio: { prompt: '', lyrics: '', seconds: 60, model: '', seed: '' },
    voice: { prompt: '', voice: '' },
  },
  source: null,          // {id, url, prompt}: a picture to change (image) or animate (video)
  sourceFor: '',         // 'image' | 'video'
  options: null,         // models, styles, voices — loaded once per open
  loading: null,
  jobs: [],              // newest first; running and finished
  open: {},              // which "More options" are open, per kind
  host: null,
  hooks: {},
  timer: null,
  gpu: null,
};

// ── small DOM helpers ───────────────────────────────────────────────────────
function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value;
    else if (key.startsWith('on') && typeof value === 'function') node.addEventListener(key.slice(2), value);
    else if (value === true) node.setAttribute(key, '');
    else node.setAttribute(key, value);
  }
  for (const child of children.flat(3)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}
const svg = (inner, size = 18) => {
  const holder = document.createElement('span');
  holder.className = 'sh-icon';
  holder.innerHTML = `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${inner}</svg>`;
  return holder;
};
/** append(), minus the nothings (append(null) writes the word "null"). */
const put = (parent, ...children) => { children.flat(3).forEach((child) => { if (child !== null && child !== undefined && child !== false) parent.append(child); }); return parent; };
const clock = (seconds) => { const s = Math.max(0, Math.round(seconds)); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`; };

/** What went wrong, in words — never "[object Object]". */
function sayError(data, status) {
  const detail = data && (data.detail ?? data.error ?? data.message);
  if (typeof detail === 'string' && detail.trim()) return detail.trim();
  if (Array.isArray(detail)) return detail.map((d) => (d && (d.msg || d.message)) || String(d)).join('; ');
  if (detail && typeof detail === 'object') return detail.message || detail.msg || JSON.stringify(detail);
  if (status === 403) return 'This account is not allowed to make media.';
  if (status === 401) return 'You are signed out. Reload the page and sign in again.';
  return `That did not work (${status || 'no answer from the server'}).`;
}
async function call(url, options) {
  let response;
  try { response = await fetch(url, { credentials: 'same-origin', ...options }); } catch (_) { throw new Error('The server did not answer. Check the connection and try again.'); }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(sayError(data, response.status));
  return data;
}
const post = (url, body) => call(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });

// ── what the server offers ──────────────────────────────────────────────────
async function loadOptions(force = false) {
  if (S.options && !force) return S.options;
  if (S.loading) return S.loading;
  S.loading = (async () => {
    const settle = async (promise, fallback) => { try { return await promise; } catch (_) { return fallback; } };
    const [image, video, audio, styles, voices, mine] = await Promise.all([
      settle(call('/api/image/models'), {}), settle(call('/api/video/models'), {}), settle(call('/api/audio/models'), {}),
      settle(call('/api/styles'), {}), settle(call('/api/tts/voices'), {}), settle(call('/api/tts/my-voices'), {}),
    ]);
    const voiceList = [];
    const seen = new Set();
    const addVoice = (voice, cloned) => {
      const id = typeof voice === 'string' ? voice : (voice && (voice.id || voice.name || voice.voice));
      if (!id || seen.has(id)) return;
      seen.add(id);
      voiceList.push({ id, label: (typeof voice === 'object' && (voice.label || voice.name)) || id, cloned });
    };
    (mine.voices || []).forEach((voice) => addVoice(voice, true));
    (voices.voices || []).forEach((voice) => addVoice(voice, false));
    const options = {
      models: { image: image.models || [], video: video.models || [], audio: audio.models || [] },
      styles: styles.styles || [], activeStyle: styles.active || '', voices: voiceList, notes: {},
    };
    // What each model is and what it is for (the same notes as the chat model picker).
    const ids = [...new Set([...options.models.image, ...options.models.video, ...options.models.audio].map((m) => m.model).filter(Boolean))];
    if (ids.length) {
      const found = await settle(post('/api/model-notes/lookup', { models: ids }), {});
      options.notes = found.notes || {};
    }
    S.options = options;
    S.loading = null;
    return options;
  })();
  return S.loading;
}

function mode() {                       // which form is showing: the kind, or its "from a picture" variant
  if (S.kind === 'image' && S.source && S.sourceFor === 'image') return 'edit';
  if (S.kind === 'video' && S.source && S.sourceFor === 'video') return 'animate';
  return S.kind;
}
function modelsFor(which) {
  const options = S.options || { models: { image: [], video: [], audio: [] } };
  if (which === 'edit') return options.models.image.filter((m) => EDIT_MODEL.test(m.model));
  if (which === 'image') return options.models.image.filter((m) => !EDIT_MODEL.test(m.model));
  if (which === 'animate') return options.models.video.filter((m) => (m.i2v !== undefined ? m.i2v : /(?:^|[/\-_.])(?:ltx[0-9.]*|i2v)(?:$|[/\-_.])/i.test(m.model)));
  if (which === 'video') return options.models.video;
  if (which === 'audio') return options.models.audio;
  return [];
}
const modelLabel = (m) => m.label || (m.endpoint === 'comfyui' ? `${m.model} (ComfyUI)` : m.model);

// ── jobs: following the work, showing the result ────────────────────────────
function addJob(job) {
  S.jobs.unshift({ key: `j${Date.now()}${Math.random().toString(16).slice(2, 6)}`, status: 'working', started: Date.now(), progress: null, detail: '', ...job });
  const finished = S.jobs.filter((j) => j.status !== 'working');
  finished.slice(KEEP_RESULTS).forEach((old) => { if (old.result && old.result.blob) URL.revokeObjectURL(old.result.url); S.jobs.splice(S.jobs.indexOf(old), 1); });
  watch();
  drawJobs();
  const card = S.host && S.host.querySelector(`[data-key="${S.jobs[0].key}"]`);
  if (card && card.scrollIntoView) card.scrollIntoView({ behavior: 'smooth', block: 'nearest' });       // on a phone it starts below the form
  return S.jobs[0];
}
function finish(job, result, error) {
  job.status = error ? (error === 'cancelled' ? 'cancelled' : 'failed') : 'done';
  job.error = error && error !== 'cancelled' ? error : '';
  job.result = result || null;
  job.took = (Date.now() - job.started) / 1000;
  if (result && !result.blob) window.dispatchEvent(new CustomEvent('gallery-refresh', { detail: { source: 'studio-home' } }));
  drawJobs();
  drawGo();
}
const working = () => S.jobs.filter((job) => job.status === 'working');

/** One pass over everything in progress. */
async function poll() {
  const active = working();
  if (!active.length) { clearInterval(S.timer); S.timer = null; return; }
  let queue = null;
  try { queue = await call('/api/queue'); } catch (_) { queue = null; }
  if (queue) S.gpu = queue.gpu_busy ? queue.gpu_job : null;
  for (const job of active) {
    if (job.jobId) {
      try {
        const state = await call(`/api/video/status/${encodeURIComponent(job.jobId)}`);
        const status = String(state.status || '');
        if (typeof state.progress === 'number') job.progress = state.progress > 1 ? state.progress / 100 : state.progress;
        job.queueId = state.queue_id || job.queueId;
        if (status === 'done') finish(job, { type: job.kind === 'audio' ? 'audio' : (job.kind === 'video' ? 'video' : 'image'), url: state.video_url, id: state.image_id });
        else if (status === 'error') finish(job, null, state.error || 'It failed without saying why.');
        else if (status === 'canceled' || status === 'cancelled') finish(job, null, 'cancelled');
        else job.detail = status === 'queued' ? 'Waiting for its turn' : 'Working';
      } catch (error) {
        job.misses = (job.misses || 0) + 1;                       // the server restarting loses its job list
        if (job.misses >= 5) finish(job, null, 'Lost track of it. If it finished, it is in the Gallery.');
      }
    } else if (queue) {
      // A picture is made inside one long request; the queue still says how far along it is.
      const mine = (queue.jobs || []).find((q) => q.kind === 'image' && (q.status === 'running' || q.status === 'queued'));
      if (mine) { job.queueId = mine.id; if (typeof mine.progress === 'number') job.progress = mine.progress; job.detail = mine.status === 'queued' ? 'Waiting for its turn' : (mine.detail || 'Working'); }
    }
  }
  drawJobs(true);
}
function watch() { if (!S.timer && working().length) S.timer = setInterval(poll, 2000); }

async function cancel(job) {
  job.cancelling = true;
  drawJobs(true);
  try {
    if (job.jobId) await call(`/api/video/cancel/${encodeURIComponent(job.jobId)}`, { method: 'POST' });
    else if (job.queueId) await call(`/api/queue/${encodeURIComponent(job.queueId)}/cancel`, { method: 'POST' });
    if (job.abort) job.abort.abort();
  } catch (_) { /* the next poll says what happened */ }
  poll();
}

// ── sending ─────────────────────────────────────────────────────────────────
function sizeFor(kind, draft) {
  const row = (SHAPES[kind] || []).find((shape) => shape[0] === draft.shape) || SHAPES[kind][0];
  return kind === 'image' && draft.big ? row[3] : row[2];
}
function styleValue(draft) { return draft.style || ''; }           // '' = the active style on the server, 'none' = no style

async function make() {
  const which = mode();
  const draft = S.drafts[S.kind];
  const prompt = (draft.prompt || '').trim();
  if (!prompt) { say(`${COPY[which].ask} first.`, true); S.host && S.host.querySelector('.sh-prompt') && S.host.querySelector('.sh-prompt').focus(); return; }
  say('');
  const seed = draft.seed !== '' && draft.seed !== undefined && !Number.isNaN(Number(draft.seed)) ? Number(draft.seed) : undefined;
  const title = prompt.length > 90 ? `${prompt.slice(0, 88)}…` : prompt;
  // The model picked under More options counts only if it can do THIS job: one
  // chosen for making pictures is not one for changing a picture.
  const model = modelsFor(which).some((m) => m.model === draft.model) ? draft.model : '';
  const again = { kind: S.kind, draft: { ...draft }, source: S.source && S.sourceFor === S.kind ? { ...S.source } : null };

  try {
    if (which === 'voice') {
      const job = addJob({ kind: 'voice', title, again, detail: 'Speaking' });
      try {
        const response = await fetch('/api/tts/synthesize', { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text: prompt, voice: draft.voice || undefined }) });
        if (!response.ok) { const data = await response.json().catch(() => ({})); throw new Error(sayError(data, response.status)); }
        const blob = await response.blob();
        if (!blob.size) throw new Error('No sound came back.');
        const kind = /mpeg|mp3/.test(blob.type) ? 'mp3' : (/ogg|opus/.test(blob.type) ? 'ogg' : 'wav');
        finish(job, { type: 'audio', url: URL.createObjectURL(blob), blob: true, name: `voice.${kind}` });
      } catch (error) { finish(job, null, error.message); }
      return;
    }
    if (which === 'image') {
      const [width, height] = sizeFor('image', draft);
      const body = { prompt };
      const known = S.options || { styles: [], activeStyle: '' };
      const style = known.styles.find((s) => s.name === (draft.style || known.activeStyle));
      if (draft.shapeChosen || !(style && style.size) || draft.style === 'none') body.size = `${width}x${height}`;      // a style's own size stands unless a shape was picked
      if (model) body.model = model;
      if (styleValue(draft)) body.style = styleValue(draft);
      if (seed !== undefined) body.seed = seed;
      if (draft.negative && draft.negative.trim()) body.negative_prompt = draft.negative.trim();
      if (draft.steps !== '' && !Number.isNaN(Number(draft.steps))) body.steps = Math.max(1, Math.min(100, Math.round(Number(draft.steps))));
      const abort = new AbortController();
      const job = addJob({ kind: 'image', title, again, abort, detail: 'Starting — the first one also loads the model' });
      try {
        const data = await call('/api/image/generate', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), signal: abort.signal });
        if (data.error) throw new Error(sayError(data));
        job.model = data.image_model || '';
        finish(job, { type: 'image', url: data.image_url, id: data.image_id });
      } catch (error) {
        finish(job, null, error.name === 'AbortError' || /aborted/i.test(error.message) ? 'cancelled' : error.message);
      }
      return;
    }
    // Everything else is queued on the server and followed by its job id.
    let started;
    if (which === 'edit') {
      const body = { prompt, image_id: S.source.id };
      if (model) body.model = model;
      if (styleValue(draft)) body.style = styleValue(draft);
      if (seed !== undefined) body.seed = seed;
      started = await post('/api/image/edit', body);
      addJob({ kind: 'image', title, again, jobId: started.job_id, model: started.model, detail: 'Waiting for its turn', from: S.source.url });
    } else if (which === 'video' || which === 'animate') {
      const body = { prompt, duration: Math.max(1, Math.min(16, Number(draft.seconds) || 5)) };
      if (model) body.model = model;
      if (styleValue(draft)) body.style = styleValue(draft);
      if (seed !== undefined) body.seed = seed;
      if (which === 'animate') body.image_id = S.source.id;
      else { const [width, height] = sizeFor('video', draft); body.width = width; body.height = height; }
      started = await post('/api/video/generate', body);
      addJob({ kind: 'video', title, again, jobId: started.job_id, model: started.model, detail: 'Waiting for its turn', from: which === 'animate' ? S.source.url : '' });
    } else if (which === 'audio') {
      const body = { tags: prompt, lyrics: (draft.lyrics || '').trim(), seconds: Math.max(10, Math.min(600, Number(draft.seconds) || 60)) };
      if (model) body.model = model;
      if (seed !== undefined) body.seed = seed;
      started = await post('/api/audio/generate', body);
      addJob({ kind: 'audio', title, again, jobId: started.job_id, model: started.model, detail: 'Waiting for its turn' });
    }
    if (S.source && (which === 'edit' || which === 'animate')) { S.source = null; S.sourceFor = ''; draw(); }
  } catch (error) {
    say(error.message, true);
  } finally {
    drawGo();
  }
}

// ── drawing ─────────────────────────────────────────────────────────────────
function say(message, bad = false) {
  const line = S.host && S.host.querySelector('.sh-say');
  if (!line) return;
  line.textContent = message || '';
  line.className = `sh-say${bad ? ' bad' : ''}`;
  line.hidden = !message;
}
function chips(choices, current, onpick, label) {
  const row = el('div', { class: 'sh-chips', role: 'group', 'aria-label': label });
  choices.forEach(([value, text]) => row.append(el('button', { type: 'button', class: `sh-chip${value === current ? ' on' : ''}`, 'aria-pressed': String(value === current), onclick: () => onpick(value) }, text)));
  return row;
}
function field(label, control, hint) {
  return el('label', { class: 'sh-field' }, el('span', { class: 'sh-label' }, label), control, hint ? el('span', { class: 'sh-hint' }, hint) : null);
}

function drawGo() {
  const button = S.host && S.host.querySelector('.sh-go');
  if (!button) return;
  const which = mode();
  const busy = which === 'voice' ? working().some((job) => job.kind === 'voice') : false;
  button.disabled = busy;
  button.textContent = busy ? 'Working…' : COPY[which].go;
}

function resultView(job) {
  const result = job.result;
  const open = result.id && S.hooks.openItem ? el('button', { type: 'button', class: 'sh-act', onclick: () => S.hooks.openItem(result.id) }, 'Open') : null;
  const save = el('a', { class: 'sh-act', href: result.url, download: result.name || '' }, 'Save');
  const reuse = el('button', { type: 'button', class: 'sh-act', onclick: () => useAgain(job) }, 'Use these words again');
  const actions = el('div', { class: 'sh-acts' });
  let preview;
  if (result.type === 'image') {
    preview = el('img', { class: 'sh-preview', src: result.url, alt: job.title, loading: 'lazy', onclick: () => (open ? open.click() : window.open(result.url, '_blank')) });
    if (result.id) {
      if (modelsFor('animate').length) actions.append(el('button', { type: 'button', class: 'sh-act strong', onclick: () => startFrom({ id: result.id, url: result.url, prompt: job.title }, 'video') }, 'Animate it'));
      if (modelsFor('edit').length) actions.append(el('button', { type: 'button', class: 'sh-act strong', onclick: () => startFrom({ id: result.id, url: result.url, prompt: job.title }, 'image') }, 'Change it'));
    }
  } else if (result.type === 'video') {
    preview = el('video', { class: 'sh-preview', src: result.url, controls: true, loop: true, playsinline: true, preload: 'metadata' });
  } else {
    preview = el('audio', { class: 'sh-audio', src: result.url, controls: true, preload: 'metadata' });
  }
  put(actions, open, save, reuse);
  return [preview, actions];
}

function jobCard(job) {
  const kind = KINDS.find((k) => k.key === job.kind) || KINDS[0];
  const card = el('div', { class: `sh-job ${job.status}`, 'data-key': job.key });
  const head = el('div', { class: 'sh-job-head' }, svg(kind.icon, 16), el('span', { class: 'sh-job-title' }, job.title));
  card.append(head);
  if (job.status === 'working') {
    const elapsed = (Date.now() - job.started) / 1000;
    const percent = typeof job.progress === 'number' ? Math.max(2, Math.min(100, Math.round(job.progress * 100))) : null;
    card.append(
      el('div', { class: `sh-bar${percent === null ? ' unknown' : ''}` }, el('i', { style: percent === null ? '' : `width:${percent}%` })),
      el('div', { class: 'sh-job-line' },
        el('span', {}, [job.cancelling ? 'Stopping…' : (job.detail || 'Working'), percent !== null ? ` · ${percent}%` : '', ` · ${clock(elapsed)}`].join('')),
        (job.jobId || job.queueId || job.abort) && !job.cancelling ? el('button', { type: 'button', class: 'sh-act', onclick: () => cancel(job) }, 'Cancel') : null));
  } else if (job.status === 'done' && job.result) {
    put(card, resultView(job));
    head.append(el('span', { class: 'sh-job-took' }, clock(job.took || 0)));
  } else {
    card.append(el('div', { class: 'sh-job-line' },
      el('span', { class: job.status === 'failed' ? 'sh-bad' : '' }, job.status === 'cancelled' ? 'Cancelled.' : job.error),
      el('button', { type: 'button', class: 'sh-act', onclick: () => useAgain(job) }, 'Try again')),
    el('button', { type: 'button', class: 'sh-x', title: 'Dismiss', 'aria-label': 'Dismiss', onclick: () => { S.jobs.splice(S.jobs.indexOf(job), 1); drawJobs(); } }, '×'));
  }
  return card;
}

function drawJobs(light = false) {
  const holder = S.host && S.host.querySelector('.sh-jobs');
  if (!holder) return;
  if (light) {
    // A tick of the clock: touch only the cards that are still working, so a
    // playing video or song is never rebuilt under the listener.
    working().forEach((job) => { const old = holder.querySelector(`[data-key="${job.key}"]`); if (old) old.replaceWith(jobCard(job)); });
    drawBusy();
    return;
  }
  holder.replaceChildren();
  if (S.jobs.length) holder.append(el('div', { class: 'sh-section' }, working().length ? 'In progress and recent' : 'Recent'));
  S.jobs.forEach((job) => holder.append(jobCard(job)));
  drawBusy();
}
function drawBusy() {
  const note = S.host && S.host.querySelector('.sh-gpu');
  if (!note) return;
  const others = S.gpu && !working().some((job) => job.queueId && S.gpu.id === job.queueId);
  note.hidden = !others;
  if (others) note.textContent = `The graphics card is busy with “${(S.gpu.title || S.gpu.kind || 'another job').slice(0, 60)}”. Yours starts when it is done.`;
}

function useAgain(job) {
  if (!job.again) return;
  S.kind = job.again.kind;
  S.drafts[S.kind] = { ...S.drafts[S.kind], ...job.again.draft };
  S.source = job.again.source;
  S.sourceFor = job.again.source ? S.kind : '';
  draw();
  const box = S.host && S.host.querySelector('.sh-prompt');
  if (box) { box.focus(); box.scrollIntoView({ block: 'center', behavior: 'smooth' }); }
}
function startFrom(source, kind) {
  S.source = source;
  S.sourceFor = kind;
  S.kind = kind;
  draw();
  const box = S.host && S.host.querySelector('.sh-prompt');
  if (box) { box.focus(); box.scrollIntoView({ block: 'center', behavior: 'smooth' }); }
}

function moreOptions(which, draft, redraw) {
  const parts = [];
  const models = modelsFor(which);
  if (which !== 'voice') {
    const select = el('select', { class: 'sh-input', onchange: (event) => { draft.model = event.target.value; redraw(); } },
      el('option', { value: '' }, 'Automatic (recommended)'), models.map((m) => el('option', { value: m.model, selected: m.model === draft.model }, modelLabel(m))));
    const picked = models.some((m) => m.model === draft.model) ? draft.model : '';
    const note = picked && S.options.notes[picked];
    parts.push(field('Model', select, note ? [note.summary, note.use ? ` Best for: ${note.use}.` : ''].join('') : 'Automatic uses the one this server is set up to prefer.'));
  }
  if (which === 'image') {
    parts.push(field('Bigger picture', el('label', { class: 'sh-check' }, el('input', { type: 'checkbox', checked: draft.big, onchange: (event) => { draft.big = event.target.checked; } }), 'Larger and sharper — takes about twice as long')));
    parts.push(field('Leave out', el('input', { class: 'sh-input', type: 'text', value: draft.negative, placeholder: 'blurry, text, extra fingers', oninput: (event) => { draft.negative = event.target.value; } }), 'Things you do not want in the picture.'));
    parts.push(field('Steps', el('input', { class: 'sh-input', type: 'number', min: '1', max: '100', value: draft.steps, placeholder: 'automatic', oninput: (event) => { draft.steps = event.target.value; } }), 'More steps: slower, sometimes finer. Leave empty unless you know the model.'));
  }
  if (which === 'video' || which === 'animate') {
    parts.push(field('Exact length (seconds)', el('input', { class: 'sh-input', type: 'number', min: '1', max: '16', step: '0.5', value: String(draft.seconds), oninput: (event) => { draft.seconds = Number(event.target.value) || 5; } }), 'Up to 16. Longer clips take much longer to make.'));
  }
  if (which === 'audio') {
    parts.push(field('Exact length (seconds)', el('input', { class: 'sh-input', type: 'number', min: '10', max: '600', step: '5', value: String(draft.seconds), oninput: (event) => { draft.seconds = Number(event.target.value) || 60; } })));
  }
  if (which !== 'voice') {
    parts.push(field('Seed', el('input', { class: 'sh-input', type: 'number', value: draft.seed, placeholder: 'random', oninput: (event) => { draft.seed = event.target.value; } }), 'The same words and the same seed give the same result. Leave empty for something new each time.'));
  }
  return parts;
}

function draw() {
  const host = S.host;
  if (!host) return;
  const which = mode();
  const draft = S.drafts[S.kind];
  const copy = COPY[which];
  const options = S.options;
  const redraw = () => draw();

  const picker = el('div', { class: 'sh-kinds', role: 'tablist', 'aria-label': 'What to make' }, KINDS.map((kind) => el('button', {
    type: 'button', role: 'tab', class: `sh-kind${kind.key === S.kind ? ' on' : ''}`, 'aria-selected': String(kind.key === S.kind),
    onclick: () => { S.kind = kind.key; draw(); },
  }, svg(kind.icon, 22), el('span', {}, kind.label))));

  const form = el('div', { class: 'sh-form' });
  if (!options) {
    host.replaceChildren(el('div', { class: 'sh-wrap' }, el('div', { class: 'sh-lead' }, 'What would you like to make?'), picker,
      el('div', { class: 'sh-form sh-loading' }, 'Checking what this server can make…'), el('div', { class: 'sh-gpu', hidden: true }), el('div', { class: 'sh-jobs' })));
    drawJobs();
    return;
  }
  const available = which === 'voice' ? (options ? options.voices.length > 0 : true) : (options ? modelsFor(which).length > 0 : true);

  if (S.source && (which === 'edit' || which === 'animate')) {
    form.append(el('div', { class: 'sh-source' }, el('img', { src: S.source.url, alt: 'The picture you are starting from' }),
      el('div', { class: 'sh-source-text' }, el('b', {}, which === 'edit' ? 'Changing this picture' : 'Animating this picture'),
        el('span', {}, which === 'edit' ? 'Say what should be different. The original is kept.' : 'Say how it should move. The picture is the first frame.')),
      el('button', { type: 'button', class: 'sh-act', onclick: () => { S.source = null; S.sourceFor = ''; draw(); } }, 'Start from nothing instead')));
  }

  if (options && !available) {
    const what = { image: 'pictures', edit: 'changing pictures', video: 'videos', animate: 'animating pictures', audio: 'songs', voice: 'voices' }[which];
    form.append(el('div', { class: 'sh-none' }, el('b', {}, `Nothing is set up for ${what} on this server yet.`),
      el('span', {}, which === 'voice' ? 'A voice is chosen under Settings → Voice.' : 'A model for it is added under More → Settings (the model library).'),
      which === 'edit' || which === 'animate' ? el('button', { type: 'button', class: 'sh-act', onclick: () => { S.source = null; S.sourceFor = ''; draw(); } }, 'Back') : null));
  } else {
    const prompt = el('textarea', { class: 'sh-prompt', rows: which === 'voice' ? '4' : '3', placeholder: copy.hint, 'aria-label': copy.ask, oninput: (event) => { draft.prompt = event.target.value; } });
    prompt.value = draft.prompt || '';
    prompt.addEventListener('keydown', (event) => { if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') make(); });
    const improve = which !== 'audio' && which !== 'voice' ? el('button', { type: 'button', class: 'sh-act', title: 'Rewrite a rough idea into a fuller description', onclick: async (event) => {
      const button = event.currentTarget;
      const rough = prompt.value.trim();
      if (!rough) { say(`${copy.ask} first.`, true); return; }
      button.disabled = true; button.textContent = 'Improving…';
      try {
        const better = await post('/api/image/enhance-prompt', { prompt: rough, kind: S.kind === 'video' ? 'video' : 'image' });
        if (better.ok && better.prompt) { draft.prompt = better.prompt; prompt.value = better.prompt; say('Rewritten — change anything you like, then make it.'); } else say(better.error || 'It could not be improved just now.', true);
      } catch (error) { say(error.message, true); } finally { button.disabled = false; button.textContent = '✨ Improve my description'; }
    } }, '✨ Improve my description') : null;
    put(form, el('div', { class: 'sh-ask' }, copy.ask), prompt, improve);

    if (which === 'audio') {
      const lyrics = el('textarea', { class: 'sh-prompt', rows: '4', placeholder: '[Verse]\nMorning light on the kitchen floor…\n\n[Chorus]\n…', 'aria-label': 'Lyrics (optional)', oninput: (event) => { draft.lyrics = event.target.value; } });
      lyrics.value = draft.lyrics || '';
      form.append(field('Lyrics (optional)', lyrics, 'Leave empty for music without singing. [Verse] and [Chorus] lines shape the song.'));
    }
    if (which === 'voice' && options) {
      form.append(field('Voice', el('select', { class: 'sh-input', onchange: (event) => { draft.voice = event.target.value; } },
        el('option', { value: '' }, 'The usual voice'), options.voices.map((voice) => el('option', { value: voice.id, selected: voice.id === draft.voice }, voice.cloned && !/your|cloned/i.test(voice.label) ? `${voice.label} (your recording)` : voice.label)))));
    }
    if (which === 'image' || which === 'video') {
      form.append(el('div', { class: 'sh-row' }, el('span', { class: 'sh-label' }, 'Shape'),
        chips(SHAPES[which].map((shape) => [shape[0], shape[1]]), draft.shape, (value) => { draft.shape = value; draft.shapeChosen = true; redraw(); }, 'Shape')));
    }
    if (LENGTHS[S.kind] && which !== 'voice') {
      form.append(el('div', { class: 'sh-row' }, el('span', { class: 'sh-label' }, 'Length'),
        chips(LENGTHS[S.kind], LENGTHS[S.kind].some(([value]) => value === Number(draft.seconds)) ? Number(draft.seconds) : null, (value) => { draft.seconds = value; redraw(); }, 'Length')));
    }
    if (options && which !== 'audio' && which !== 'voice' && options.styles.length) {
      const active = options.activeStyle;
      const styles = [['', active ? `Usual (${active})` : 'None'], ...(active ? [['none', 'None']] : []), ...options.styles.filter((style) => style.name !== active).map((style) => [style.name, style.name])];
      form.append(el('div', { class: 'sh-row' }, el('span', { class: 'sh-label' }, 'Look'), chips(styles, draft.style || '', (value) => { draft.style = value; redraw(); }, 'Look')));
    }
    if (options) {
      const more = el('details', { class: 'sh-more', open: !!S.open[which] }, el('summary', {}, 'More options'), el('div', { class: 'sh-more-body' }, moreOptions(which, draft, redraw)));
      more.addEventListener('toggle', () => { S.open[which] = more.open; });
      form.append(more);
    }
    form.append(el('div', { class: 'sh-say', hidden: true, role: 'status' }),
      el('div', { class: 'sh-go-row' }, el('button', { type: 'button', class: 'sh-go', onclick: make }, copy.go),
        which === 'audio' ? el('button', { type: 'button', class: 'sh-act', onclick: () => S.hooks.openTab && S.hooks.openTab('music') }, 'Covers, narration and voice recording →') : null,
        which === 'video' ? el('button', { type: 'button', class: 'sh-act', onclick: () => S.hooks.openTab && S.hooks.openTab('movie') }, 'Join clips into a film →') : null));
  }

  host.replaceChildren(el('div', { class: 'sh-wrap' },
    el('div', { class: 'sh-lead' }, 'What would you like to make?'), picker, form,
    el('div', { class: 'sh-gpu', hidden: true }), el('div', { class: 'sh-jobs' })));
  drawGo();
  drawJobs();
}

// ── the interface for the Studio window ─────────────────────────────────────
/** Draw the Create screen into `host`. `hooks.openItem(id)` opens a finished
 *  item in the gallery; `hooks.openTab(name)` switches to another Studio tab. */
export function renderStudioHome(host, hooks = {}) {
  S.host = host;
  S.hooks = hooks;
  const before = S.options ? JSON.stringify([S.options.models, S.options.styles, S.options.activeStyle, S.options.voices]) : '';
  draw();                                   // at once, with what is known
  loadOptions(true).then((options) => {
    if (S.host !== host) return;
    // Lists that did not change are not worth taking the cursor out of the box for.
    const typing = host.contains(document.activeElement) && /^(TEXTAREA|INPUT|SELECT)$/.test(document.activeElement.tagName);
    if (!before || (!typing && before !== JSON.stringify([options.models, options.styles, options.activeStyle, options.voices]))) draw();
  });
  watch();
  if (working().length) poll();
}

/** Start from a picture in the gallery: change it (`'image'`) or animate it (`'video'`). */
export function setStudioSource(source, kind) {
  if (!source || !source.id) { S.source = null; S.sourceFor = ''; return; }
  S.source = { id: source.id, url: source.url, prompt: source.prompt || '' };
  S.sourceFor = kind === 'video' ? 'video' : 'image';
  S.kind = S.sourceFor;
}

/** The Studio window closed: stop redrawing. Work in progress goes on being
 *  followed, so it is here when the Studio is opened again. */
export function leaveStudioHome() { S.host = null; }

export default { renderStudioHome, setStudioSource, leaveStudioHome };
