// The memory inspector: everything Aegis keeps about the signed-in person —
// where each piece came from, every change ever made to it, and the means to
// correct or remove any of it.
import {
  api, h, add, clear, icon, iconButton, toast, oops, sheet, confirmSheet, field, toggle, when, ago, debounce, plural,
} from './lite.js';

const app = document.getElementById('app');
const view = h('div');
const state = {
  tab: 'overview', overview: null, memories: null, topics: null, topicId: null,
  facts: { q: '', category: '', source: '', sort: 'new' },
  history: { kind: '', by: '', target: '', entries: [], next: null },
};

const TABS = [
  ['overview', 'Overview'], ['profile', 'Profile'], ['facts', 'Facts'], ['topics', 'Topics'],
  ['conversations', 'Conversations'], ['history', 'History'], ['controls', 'Controls'],
];
const SECTIONS = [
  ['identity', 'About you'], ['people', 'People'], ['preferences', 'Preferences'], ['interests', 'Interests'],
  ['projects', 'Projects'], ['goals', 'Goals'], ['patterns', 'Patterns noticed'],
];
const SOURCE_LABEL = { auto: 'Learned from a chat', user: 'You wrote this', ai_agent: 'Saved by the assistant', unknown: 'Older entry' };
const BY_LABEL = { you: 'You', auto: 'Learned automatically', assistant: 'The assistant', tidy: 'Tidy-up', import: 'Import', undo: 'Undo', system: 'System' };

// ── shell ──────────────────────────────────────────────────────────────────

function shell() {
  const tabs = h('div', { class: 'tabs', role: 'tablist' }, TABS.map(([id, label]) => h('button', {
    class: 'tab', role: 'tab', type: 'button', 'aria-selected': String(state.tab === id), dataset: { tab: id },
    onclick: () => go(id),
  }, label)));
  clear(app,
    h('div', { class: 'topbar' },
      iconButton('back', 'Back', () => { if (document.referrer && history.length > 1) history.back(); else location.href = '/'; }),
      h('h1', {}, 'Your memory', h('span', { class: 'sub', id: 'who' }, 'Everything Aegis keeps about you'))),
    tabs, view);
}

function go(tab, extra = {}) {
  Object.assign(state, extra);
  state.tab = tab;
  if (tab !== 'topics') state.topicId = null;
  const hash = tab === 'topics' && state.topicId ? `#topics/${state.topicId}` : `#${tab}`;
  if (location.hash !== hash) history.replaceState(null, '', hash);
  document.querySelectorAll('.tab').forEach((t) => t.setAttribute('aria-selected', String(t.dataset.tab === tab)));
  render();
}

const RENDER = {
  overview: renderOverview, profile: renderProfile, facts: renderFacts, topics: renderTopics,
  conversations: renderConversations, history: renderHistory, controls: renderControls,
};

async function render() {
  clear(view, h('div', { class: 'loading' }, h('span', { class: 'spinner' })));
  try {
    await RENDER[state.tab]();
  } catch (error) {
    clear(view, h('div', { class: 'card empty' }, h('b', {}, 'Could not load this'), error.message));
  }
  window.scrollTo(0, 0);
}

async function loadOverview(force = false) {
  if (!state.overview || force) {
    state.overview = await api('/api/mind/overview');
    const who = document.getElementById('who');
    if (who) who.textContent = state.overview.account ? `Everything Aegis keeps about ${state.overview.account}` : 'Everything Aegis keeps about you';
  }
  return state.overview;
}

const origin = (o) => (o && o.name ? `“${o.name}”` : '');

// ── overview ───────────────────────────────────────────────────────────────

async function renderOverview() {
  const o = await loadOverview(true);
  const c = o.counts;
  const stat = (n, label, tab) => h('button', { class: 'stat', type: 'button', onclick: () => go(tab) }, h('b', {}, String(n)), h('span', {}, label));
  const interests = (o.profile.interests || []).map((i) => h('span', { class: 'chip accent' }, i.name));

  const input = h('input', { class: 'input', placeholder: 'Type a message, e.g. “any ideas for the weekend?”', maxlength: '500' });
  const out = h('div', { class: 'stack' });
  const show = async () => {
    clear(out, h('div', { class: 'loading' }, h('span', { class: 'spinner' })));
    try {
      const p = await api('/api/mind/preview', { method: 'POST', body: { message: input.value } });
      if (!p.blocks.length) { clear(out, h('p', { class: 'muted' }, p.note || 'Nothing would be given.')); return; }
      clear(out,
        h('p', { class: 'small muted' }, `About ${p.tokens_estimate.toLocaleString()} tokens. This is the complete list — nothing else about you is sent.`),
        p.blocks.map((b) => h('div', { class: 'block' }, h('div', { class: 'label' }, blockLabel(b.label)), h('div', { class: 'text' }, b.text))));
    } catch (error) { clear(out); oops(error); }
  };
  input.addEventListener('keydown', (event) => { if (event.key === 'Enter') show(); });

  clear(view,
    h('div', { class: 'stats' },
      stat(c.memories, c.memories === 1 ? 'fact' : 'facts', 'facts'), stat(c.topics, c.topics === 1 ? 'topic' : 'topics', 'topics'),
      stat(c.conversations, 'conversations', 'conversations'), stat(c.changes, 'changes logged', 'history')),
    h('div', { class: 'section-title' }, h('h2', {}, 'In short')),
    h('div', { class: 'card stack' },
      o.profile.empty
        ? h('p', { class: 'muted' }, 'Nothing is known about you yet. It fills in by itself as you chat — never in Nobody mode — and you can add things yourself under Profile or Facts.')
        : h('p', { class: 'profile-summary' }, o.profile.summary || 'A profile assembled from your facts; the written summary appears after the next few chats.'),
      interests.length ? h('div', { class: 'chips' }, interests) : null,
      h('div', { class: 'line wrap' },
        h('button', { class: 'btn sm', type: 'button', onclick: () => go('profile') }, 'Open profile'),
        o.last_change ? h('span', { class: 'small muted' }, `Last change ${ago(o.last_change)}`) : null)),
    h('div', { class: 'section-title' }, h('h2', {}, 'What Aegis is told about you')),
    h('div', { class: 'card stack' },
      h('p', { class: 'small muted' }, 'Before each reply the model is handed a short briefing built from your memory. See exactly what it would get:'),
      h('div', { class: 'line' }, h('div', { class: 'grow' }, input), h('button', { class: 'btn primary', type: 'button', onclick: show }, 'Show')),
      out),
    h('div', { class: 'section-title' }, h('h2', {}, 'Switches')),
    switchesCard(o));
}

function blockLabel(label) {
  return ({
    'saved memory: user profile': 'Your profile (every message)',
    'deep memory: recent conversations': 'Your latest conversations (every message)',
    'saved memory: pinned user facts': 'Pinned facts (every message)',
    'saved memory: retrieved context': 'Facts recalled for this message',
    'deep memory: topic notes': 'Topic notes recalled for this message',
    'deep memory: earlier conversations': 'Earlier conversations recalled for this message',
  })[label] || label;
}

function switchesCard(o) {
  const set = (key, label) => async (value, input) => {
    try {
      await api(`/api/prefs/${key}`, { method: 'PUT', body: { value } });
      state.overview.switches[key === 'memory_enabled' ? 'using' : 'learning'] = value;
      toast(`${label} ${value ? 'on' : 'off'}`);
    } catch (error) { input.checked = !value; oops(error); }
  };
  const row = (title, text, checked, handler) => h('div', { class: 'row' },
    h('div', { class: 'main' }, h('div', {}, title), h('div', { class: 'small muted' }, text)), toggle(checked, handler, title));
  return h('div', { class: 'card flush rows' },
    row('Use my memory', 'Give the model your profile and what it has learned. Off: it answers as if it had never met you. Nothing is deleted.',
      o.switches.using, set('memory_enabled', 'Using memory')),
    row('Keep learning', 'Pick up new facts, topic notes and conversation summaries in the background as you chat.',
      o.switches.learning, set('auto_memory', 'Learning')),
    h('div', { class: 'row' }, h('div', { class: 'main small muted' },
      'For a single chat that uses and leaves nothing, start it in Nobody mode (the eye on the welcome screen).')));
}

// ── profile ────────────────────────────────────────────────────────────────

async function renderProfile() {
  const o = await loadOverview(true);
  const p = o.profile;
  const source = p.empty ? ''
    : p.source === 'model' ? `Written from ${plural(p.memory_count, 'fact')} · updated ${ago(p.updated)}`
      : 'Put together directly from your facts — a written version follows after the next few chats';
  const rewrite = h('button', { class: 'btn sm', type: 'button', onclick: async (event) => {
    const button = event.currentTarget;
    button.disabled = true; button.textContent = 'Rewriting…';
    try {
      const r = await api('/api/mind/profile/rewrite', { method: 'POST' });
      toast(r.written_by_model ? 'Profile rewritten' : 'No model answered — showing your facts as they are');
      await renderProfile();
    } catch (error) { oops(error); button.disabled = false; button.textContent = 'Rewrite now'; }
  } }, 'Rewrite now');

  const blocks = SECTIONS.map(([key, label]) => {
    const items = key === 'interests' ? (p.interests || []).map((i) => ({ text: i.name, notes: (i.notes || []).join(' · ') }))
      : (p[key] || []).map((text) => ({ text }));
    return h('div', {},
      h('div', { class: 'section-title' }, h('h2', {}, label),
        key === 'patterns' ? h('span', { class: 'chip warn' }, 'inferred') : null,
        h('button', { class: 'btn sm ghost', type: 'button', onclick: () => editLine(key, label) }, icon('plus', 'sm'), 'Add')),
      items.length
        ? h('div', { class: 'card flush rows' }, items.map((item) => h('div', { class: 'pline' },
          h('div', { class: 'main' }, item.text, item.notes ? h('span', { class: 'notes' }, item.notes) : null),
          iconButton('edit', 'Change', () => editLine(key, label, item.text)),
          iconButton('trash', 'Remove', () => removeLine(key, item.text), 'danger'))))
        : h('div', { class: 'card small muted' }, key === 'patterns'
          ? 'Habits Aegis notices across several conversations show up here, marked as its own inference.'
          : 'Nothing here yet.'));
  });

  clear(view,
    h('div', { class: 'card stack' },
      p.summary ? h('p', { class: 'profile-summary' }, p.summary) : h('p', { class: 'muted' }, 'No summary yet.'),
      h('div', { class: 'line wrap' }, h('span', { class: 'small muted grow' }, source), rewrite)),
    h('p', { class: 'small muted', style: { margin: '12px 2px 0' } },
      'This is given to the model with every message. A line you change or add is kept word for word; a line you remove never comes back.'),
    blocks);
}

function editLine(section, label, existing = '') {
  const input = h('textarea', { class: 'input', rows: '2', maxlength: '200' });
  input.value = existing;
  sheet({
    title: existing ? `Change — ${label}` : `Add to ${label}`,
    body: field(section === 'interests' ? 'Name of the interest' : 'In your own words', input,
      existing ? 'Your wording replaces this line and is kept as you wrote it.' : 'Kept exactly as you write it.'),
    actions: [{ label: 'Cancel', kind: 'ghost' }, { label: 'Save', kind: 'primary', run: async () => {
      const text = input.value.trim();
      if (!text) return false;
      if (existing && text === existing) return true;
      await api('/api/mind/profile/line', { method: 'POST', body: existing
        ? { action: 'edit', section, text: existing, new_text: text } : { action: 'add', section, text } });
      toast('Saved');
      renderProfile();
      return true;
    } }],
  });
}

async function removeLine(section, text) {
  let related = [];
  try { related = (await api(`/api/mind/related?text=${encodeURIComponent(text)}`)).memories; } catch (_) { /* fine */ }
  const boxes = related.map((m) => {
    const box = h('input', { type: 'checkbox', value: m.id });
    box.checked = true;
    return h('label', { class: 'check' }, box, h('span', {}, m.text, h('span', { class: 'tiny muted', style: { display: 'block' } }, SOURCE_LABEL[m.source] || m.source)));
  });
  sheet({
    title: 'Remove from your profile',
    body: h('div', { class: 'stack' },
      h('p', {}, `“${text}”`),
      h('p', { class: 'small muted' }, 'It will not be put back, however the profile is rewritten.'),
      related.length ? h('div', {}, h('h3', {}, 'Also forget the facts behind it'),
        h('p', { class: 'small muted' }, 'Untick any you want to keep. A fact you keep can still be recalled when it is relevant.'), boxes) : null),
    actions: [{ label: 'Cancel', kind: 'ghost' }, { label: 'Remove', kind: 'danger solid', run: async () => {
      const forget = boxes.map((b) => b.querySelector('input')).filter((b) => b.checked).map((b) => b.value);
      const r = await api('/api/mind/profile/line', { method: 'POST', body: { action: 'remove', section, text, forget } });
      toast(r.forgotten ? `Removed, and ${plural(r.forgotten, 'fact')} forgotten` : 'Removed');
      state.memories = null;
      renderProfile();
    } }],
  });
}

// ── facts ──────────────────────────────────────────────────────────────────

async function renderFacts() {
  await loadOverview();
  if (!state.memories) state.memories = (await api('/api/mind/memories')).memories;
  const f = state.facts;
  const list = h('div');
  const categories = [...new Set(state.memories.map((m) => m.category))].sort();
  const chipRow = (values, current, pick, labelOf = (v) => v) => h('div', { class: 'chips' },
    [['', 'All'], ...values.map((v) => [v, labelOf(v)])].map(([value, label]) => h('button', {
      class: `chip${current === value ? ' on' : ''}`, type: 'button', onclick: () => { pick(value); renderFacts(); },
    }, label)));
  const search = h('input', { class: 'input', type: 'search', placeholder: 'Search what Aegis knows', value: f.q });
  search.addEventListener('input', debounce(() => { f.q = search.value; draw(); }, 150));
  const sort = h('select', { class: 'input', style: { width: 'auto' }, onchange: (event) => { f.sort = event.target.value; draw(); } },
    [['new', 'Newest'], ['used', 'Most used'], ['az', 'A–Z']].map(([v, l]) => { const o = h('option', { value: v }, l); o.selected = f.sort === v; return o; }));

  function draw() {
    const q = f.q.trim().toLowerCase();
    let items = state.memories.filter((m) => (!f.category || m.category === f.category) && (!f.source || m.source === f.source)
      && (!q || m.text.toLowerCase().includes(q) || (m.quote || '').toLowerCase().includes(q)));
    if (f.sort === 'used') items = [...items].sort((a, b) => b.uses - a.uses);
    else if (f.sort === 'az') items = [...items].sort((a, b) => a.text.localeCompare(b.text));
    clear(list, items.length
      ? h('div', { class: 'card flush rows' }, items.map(factRow))
      : h('div', { class: 'card empty' }, h('b', {}, state.memories.length ? 'Nothing matches' : 'No facts yet'),
        state.memories.length ? 'Try a different search or filter.' : 'Facts appear here as Aegis learns them, each with the chat it came from.'));
  }

  clear(view,
    h('div', { class: 'toolbar' },
      h('div', { class: 'search' }, icon('search'), search), sort,
      h('button', { class: 'btn primary', type: 'button', onclick: () => editFact() }, icon('plus', 'sm'), 'Add')),
    h('div', { class: 'stack tight', style: { marginBottom: '12px' } },
      chipRow(categories, f.category, (v) => { f.category = v; }),
      chipRow(['auto', 'user', 'ai_agent'], f.source, (v) => { f.source = v; }, (v) => ({ auto: 'Learned', user: 'Written by you', ai_agent: 'Saved by the assistant' })[v])),
    list);
  draw();
}

function factRow(m) {
  const meta = [h('span', { class: 'chip' }, m.category)];
  if (m.pinned) meta.push(h('span', { class: 'chip accent' }, 'pinned'));
  if (m.sensitive) meta.push(h('span', { class: 'chip warn' }, m.sensitive));
  let from = SOURCE_LABEL[m.source] || m.source;
  if (m.source === 'auto' && m.origin && m.origin.name) from = `Learned from ${origin(m.origin)}`;
  meta.push(h('span', {}, from), h('span', {}, when(m.learned)));
  if (m.updated) meta.push(h('span', {}, `changed ${when(m.updated)}`));
  if (m.mentions > 1) meta.push(h('span', {}, `said ${m.mentions}×`));
  if (m.uses) meta.push(h('span', {}, `used ${m.uses}×`));
  return h('div', { class: 'row fact' },
    h('div', { class: 'main' }, h('div', { class: 'pre' }, m.text),
      m.quote ? h('div', { class: 'quote' }, `You said: “${m.quote}”`) : null,
      h('div', { class: 'meta' }, meta)),
    h('div', { class: 'acts' },
      iconButton('clock', 'History of this fact', () => go('history', { history: { kind: '', by: '', target: m.id, entries: [], next: null } })),
      iconButton('edit', 'Change', () => editFact(m)),
      iconButton('trash', 'Forget', () => forgetFact(m), 'danger')));
}

function editFact(m = null) {
  const text = h('textarea', { class: 'input', rows: '3', maxlength: '1000' });
  text.value = m ? m.text : '';
  const category = h('select', { class: 'input' }, (state.overview.categories || ['fact']).map((c) => {
    const option = h('option', { value: c }, c);
    option.selected = c === (m ? m.category : 'fact');
    return option;
  }));
  const pinned = h('input', { type: 'checkbox' });
  pinned.checked = m ? m.pinned : false;
  sheet({
    title: m ? 'Change this fact' : 'Add a fact',
    body: h('div', { class: 'stack' },
      field('What should Aegis know?', text), field('Kind', category),
      h('label', { class: 'check' }, pinned, h('span', {}, 'Pin it', h('span', { class: 'tiny muted', style: { display: 'block' } },
        'Always given to the model, and never changed or removed by the automatic learner.')))),
    actions: [{ label: 'Cancel', kind: 'ghost' }, { label: 'Save', kind: 'primary', run: async () => {
      const body = { text: text.value.trim(), category: category.value, pinned: pinned.checked };
      if (!body.text) return false;
      const r = m ? await api(`/api/mind/memories/${m.id}`, { method: 'PUT', body }) : await api('/api/mind/memories', { method: 'POST', body });
      toast(r.duplicate ? 'Already known' : 'Saved');
      state.memories = null;
      renderFacts();
      return true;
    } }],
  });
}

async function forgetFact(m) {
  if (!await confirmSheet(`“${m.text}”`, { title: 'Forget this fact?', ok: 'Forget', danger: true })) return;
  try {
    await api(`/api/mind/memories/${m.id}`, { method: 'DELETE' });
    state.memories = state.memories.filter((x) => x.id !== m.id);
    toast('Forgotten', { action: { label: 'Undo', run: undoLatest } });
    renderFacts();
  } catch (error) { oops(error); }
}

async function undoLatest() {
  try {
    const latest = (await api('/api/mind/history?limit=1&kind=memory')).entries[0];
    if (latest && latest.can_undo) await api(`/api/mind/history/${latest.id}/undo`, { method: 'POST' });
    state.memories = null;
    toast('Restored');
    render();
  } catch (error) { oops(error); }
}

// ── topics ─────────────────────────────────────────────────────────────────

async function renderTopics() {
  await loadOverview();
  if (state.topicId) return renderTopic(state.topicId);
  state.topics = (await api('/api/mind/topics')).topics;
  const add_ = h('button', { class: 'btn primary', type: 'button', onclick: newTopic }, icon('plus', 'sm'), 'New topic');
  clear(view,
    h('div', { class: 'line', style: { marginBottom: '10px' } },
      h('p', { class: 'small muted grow' }, 'One file per subject in your life. Every chat that touches a subject adds dated notes to it; the summary is rewritten from the notes.'), add_),
    state.topics.length
      ? h('div', { class: 'card flush rows' }, state.topics.map((t) => h('div', { class: 'row tap', onclick: () => go('topics', { topicId: t.id }) },
        h('div', { class: 'main' },
          h('div', { class: 'line' }, h('h3', { class: 'grow clip' }, t.name), h('span', { class: 'chip' }, t.kind)),
          t.summary ? h('div', { class: 'small muted clamp2' }, t.summary) : null,
          h('div', { class: 'meta' }, h('span', {}, plural(t.note_count, 'note')),
            t.new_notes ? h('span', {}, `${t.new_notes} not yet in the summary`) : null, h('span', {}, `updated ${ago(t.updated)}`))))))
      : h('div', { class: 'card empty' }, h('b', {}, 'No topics yet'), 'They appear as you talk about the things you do — a hobby, a project, a person, your home.'));
  return null;
}

async function renderTopic(id) {
  const t = (await api(`/api/mind/topics/${encodeURIComponent(id)}`)).topic;
  const refresh = () => renderTopic(id);
  const act = (label, kind, run) => h('button', { class: `btn sm ${kind}`.trim(), type: 'button', onclick: run }, label);
  const list = (items) => h('ul', { style: { margin: 0, paddingLeft: '1.2em' } }, items.map((i) => h('li', {}, i)));
  const note = h('input', { class: 'input', placeholder: 'Add a note in your own words', maxlength: '280' });
  const addNote = async () => {
    if (note.value.trim().length < 5) return;
    try { await api(`/api/mind/topics/${id}/notes`, { method: 'POST', body: { text: note.value.trim() } }); toast('Note added'); refresh(); } catch (error) { oops(error); }
  };
  note.addEventListener('keydown', (event) => { if (event.key === 'Enter') addNote(); });
  const locked = (key) => (t.locked || []).includes(key);

  clear(view,
    h('div', { class: 'line', style: { marginBottom: '8px' } },
      h('button', { class: 'btn sm ghost', type: 'button', onclick: () => go('topics', { topicId: null }) }, icon('back', 'sm'), 'All topics')),
    h('div', { class: 'card stack' },
      h('div', { class: 'line wrap' }, h('h2', { class: 'grow' }, t.name), h('span', { class: 'chip' }, t.kind)),
      t.summary ? h('p', {}, t.summary) : h('p', { class: 'muted' }, 'No summary yet — it is written once there are a couple of notes.'),
      h('div', { class: 'small muted' }, t.digest_updated ? `Summary written ${ago(t.digest_updated)}` : '',
        locked('summary') ? ' · the summary is yours and is not rewritten' : '',
        t.new_notes ? ` · ${plural(t.new_notes, 'newer note')} not yet in it` : ''),
      h('div', { class: 'line wrap' },
        act('Edit', '', () => editTopic(t, refresh)),
        act('Rewrite from notes', '', async (event) => {
          const button = event.currentTarget;
          button.disabled = true; button.textContent = 'Rewriting…';
          try { await api(`/api/mind/topics/${id}/rewrite`, { method: 'POST', body: { unlock: true } }); toast('Rewritten'); } catch (error) { oops(error); }
          refresh();
        }),
        act('Merge into…', '', () => mergeTopic(t)),
        act('Delete', 'danger', async () => {
          if (!await confirmSheet(`“${t.name}” and its ${plural(t.note_count, 'note')} will be deleted.`, { title: 'Delete this topic?', ok: 'Delete', danger: true })) return;
          try { await api(`/api/mind/topics/${id}`, { method: 'DELETE' }); toast('Topic deleted'); go('topics', { topicId: null }); } catch (error) { oops(error); }
        }))),
    t.state.length ? [h('div', { class: 'section-title' }, h('h2', {}, 'Where things stand')), h('div', { class: 'card' }, list(t.state))] : null,
    t.open.length ? [h('div', { class: 'section-title' }, h('h2', {}, 'Open')), h('div', { class: 'card' }, list(t.open))] : null,
    (t.timeline || []).length ? [h('div', { class: 'section-title' }, h('h2', {}, 'Timeline')),
      h('div', { class: 'card' }, h('ul', { class: 'timeline' }, t.timeline.map((e) => h('li', {}, h('span', { class: 'when' }, e.when), e.text))))] : null,
    h('div', { class: 'section-title' }, h('h2', {}, 'Notes'), h('span', { class: 'count' }, String(t.notes.length))),
    h('div', { class: 'line', style: { marginBottom: '10px' } }, h('div', { class: 'grow' }, note), h('button', { class: 'btn', type: 'button', onclick: addNote }, 'Add')),
    h('div', { class: 'card flush rows' }, t.notes.map((n) => h('div', { class: 'row' },
      h('div', { class: 'main' }, h('div', { class: 'pre' }, n.text),
        h('div', { class: 'meta' }, h('span', {}, when(n.ts)),
          h('span', {}, n.by === 'you' ? 'You wrote this' : (n.origin && n.origin.name ? `From ${origin(n.origin)}` : 'Learned from a chat')))),
      iconButton('trash', 'Delete note', async () => {
        if (!await confirmSheet(`“${n.text}”`, { title: 'Delete this note?', ok: 'Delete', danger: true })) return;
        try { await api(`/api/mind/topics/${id}/notes/${n.id}`, { method: 'DELETE' }); toast('Note deleted — the summary will be rewritten without it'); refresh(); } catch (error) { oops(error); }
      }, 'danger')))));
  return null;
}

function newTopic() {
  const name = h('input', { class: 'input', maxlength: '60', placeholder: 'e.g. The garden' });
  const first = h('textarea', { class: 'input', rows: '2', maxlength: '280', placeholder: 'Something to start with (optional)' });
  sheet({
    title: 'New topic', body: h('div', { class: 'stack' }, field('Name', name), field('First note', first)),
    actions: [{ label: 'Cancel', kind: 'ghost' }, { label: 'Create', kind: 'primary', run: async () => {
      if (name.value.trim().length < 2) return false;
      const r = await api('/api/mind/topics', { method: 'POST', body: { name: name.value.trim(), note: first.value.trim() } });
      go('topics', { topicId: r.topic.id });
      return true;
    } }],
  });
}

function editTopic(t, done) {
  const name = h('input', { class: 'input', maxlength: '60', value: t.name });
  const kind = h('select', { class: 'input' }, (state.overview.topic_kinds || ['other']).map((k) => { const o = h('option', { value: k }, k); o.selected = k === t.kind; return o; }));
  const summary = h('textarea', { class: 'input', rows: '4', maxlength: '900' });
  summary.value = t.summary || '';
  const now = h('textarea', { class: 'input', rows: '4' });
  now.value = (t.state || []).join('\n');
  const open = h('textarea', { class: 'input', rows: '3' });
  open.value = (t.open || []).join('\n');
  const lines = (area) => area.value.split('\n').map((l) => l.trim()).filter(Boolean);
  sheet({
    title: 'Edit topic',
    body: h('div', { class: 'stack' }, field('Name', name), field('Kind', kind),
      field('Summary', summary, 'A summary you edit stays as you wrote it until you choose “Rewrite from notes”.'),
      field('Where things stand — one per line', now), field('Open — one per line', open)),
    actions: [{ label: 'Cancel', kind: 'ghost' }, { label: 'Save', kind: 'primary', run: async () => {
      await api(`/api/mind/topics/${t.id}`, { method: 'PUT', body: { name: name.value.trim(), kind: kind.value, summary: summary.value, state: lines(now), open: lines(open) } });
      toast('Saved');
      done();
    } }],
  });
}

function mergeTopic(t) {
  const others = (state.topics || []).filter((x) => x.id !== t.id);
  if (!others.length) { toast('There is no other topic to merge into'); return; }
  const select = h('select', { class: 'input' }, others.map((x) => h('option', { value: x.id }, x.name)));
  sheet({
    title: `Merge “${t.name}” into…`,
    body: h('div', { class: 'stack' }, field('Topic', select), h('p', { class: 'small muted' }, 'Its notes move over and this topic disappears. The other topic’s summary is rewritten from all the notes.')),
    actions: [{ label: 'Cancel', kind: 'ghost' }, { label: 'Merge', kind: 'primary', run: async () => {
      const r = await api(`/api/mind/topics/${t.id}/merge`, { method: 'POST', body: { into: select.value } });
      toast('Merged');
      go('topics', { topicId: r.topic.id });
    } }],
  });
}

// ── conversations ──────────────────────────────────────────────────────────

async function renderConversations() {
  const items = (await api('/api/mind/conversations')).conversations;
  clear(view,
    h('p', { class: 'small muted', style: { marginBottom: '10px' } },
      'A short digest of each chat, kept so a later chat can pick up where one left off. The three most recent are always in view of the model; older ones only when they bear on what you ask. Deleting a digest does not delete the chat.'),
    items.length
      ? h('div', { class: 'card flush rows' }, items.map((e) => h('div', { class: 'row' },
        h('div', { class: 'main' },
          h('h3', {}, e.title || 'Conversation'), h('div', { class: 'small' }, e.summary),
          (e.outcomes || []).length ? h('ul', { class: 'small muted', style: { margin: '4px 0 0', paddingLeft: '1.2em' } }, e.outcomes.map((o) => h('li', {}, o))) : null,
          h('div', { class: 'meta' }, h('span', {}, when(e.updated, { time: true })),
            (e.topics || []).map((t) => h('span', { class: 'chip' }, t)), e.chat_exists ? null : h('span', {}, 'chat since deleted'))),
        iconButton('trash', 'Delete digest', async () => {
          if (!await confirmSheet('The digest is removed from memory. The chat itself is not touched.', { title: 'Delete this digest?', ok: 'Delete', danger: true })) return;
          try { await api(`/api/mind/conversations/${encodeURIComponent(e.session_id)}`, { method: 'DELETE' }); toast('Deleted'); renderConversations(); } catch (error) { oops(error); }
        }, 'danger'))))
      : h('div', { class: 'card empty' }, h('b', {}, 'No conversations summarised yet'), 'A digest is written a few messages into a chat.'));
}

// ── history ────────────────────────────────────────────────────────────────

function describe(e) {
  const before = e.before || {};
  const after = e.after || {};
  const name = e.label ? `“${e.label}”` : '';
  const key = `${e.kind}.${e.op}`;
  const by = (e.cause || {}).by;
  const added = by === 'you' ? 'You added a fact' : by === 'assistant' ? 'The assistant saved a fact' : by === 'undo' ? 'Restored a fact' : 'Learned a fact';
  const gone = by === 'you' ? 'You deleted a fact' : by === 'tidy' ? 'Merged away a duplicate' : by === 'undo' ? 'Took a fact back out' : 'Forgot a fact';
  const T = {
    'memory.add': ['add', added, after.text], 'memory.delete': ['del', gone, before.text],
    'memory.pin': ['edit', 'Pinned a fact', after.text], 'memory.unpin': ['edit', 'Unpinned a fact', after.text],
    'topic.create': ['add', `Started topic ${name}`], 'topic.note_add': ['add', `Note on ${name}`, after.text],
    'topic.note_delete': ['del', `Removed a note from ${name}`, before.text], 'topic.digest': ['edit', `Rewrote the summary of ${name}`, after.summary],
    'topic.edit': ['edit', `Edited ${name}`], 'topic.delete': ['del', `Deleted topic ${name}`],
    'topic.merge': ['edit', `Merged “${before.name || ''}” into ${name}`],
    'episode.add': ['add', `Summarised a conversation: ${name}`, after.summary], 'episode.update': ['edit', `Updated the digest of ${name}`, after.summary],
    'episode.delete': ['del', `Deleted the digest of ${name}`],
    'profile.add_line': ['add', 'You added a profile line', after.text], 'profile.remove_line': ['del', 'You removed a profile line', before.text],
    'rules.blocked_kinds': ['edit', 'Changed what is never kept', (after.blocked_kinds || []).join(', ') || 'nothing blocked'],
  };
  if (key === 'memory.edit') {
    const same = before.text === after.text;
    return { tone: 'edit', title: same ? 'Recategorised a fact' : (by === 'you' ? 'You changed a fact' : by === 'undo' ? 'Put a fact back as it was' : 'Updated a fact'),
      body: same ? h('span', {}, `${after.text} — ${before.category} → ${after.category}`)
        : h('span', {}, h('span', { class: 'was' }, before.text), h('br'), after.text) };
  }
  if (key === 'profile.edit_line') {
    return { tone: 'edit', title: 'You changed a profile line', body: h('span', {}, h('span', { class: 'was' }, before.text), h('br'), after.text) };
  }
  const [tone, title, body] = T[key] || ['edit', `${e.kind} ${e.op}`, ''];
  return { tone, title, body: body ? h('span', {}, body) : null };
}

async function renderHistory(more = false) {
  const hs = state.history;
  if (!more) { hs.entries = []; hs.next = null; }
  const query = new URLSearchParams({ limit: '60' });
  if (hs.kind) query.set('kind', hs.kind);
  if (hs.by) query.set('by', hs.by);
  if (hs.target) query.set('target', hs.target);
  if (more && hs.next) query.set('before', String(hs.next));
  const data = await api(`/api/mind/history?${query}`);
  hs.entries = hs.entries.concat(data.entries);
  hs.next = data.next_before;
  const chips = (pairs, current, pick) => h('div', { class: 'chips' }, pairs.map(([value, label]) => h('button', {
    class: `chip${current === value ? ' on' : ''}`, type: 'button', onclick: () => { pick(value); renderHistory(); } }, label)));
  const dot = { add: 'plus', del: 'trash', edit: 'edit' };
  clear(view,
    h('p', { class: 'small muted', style: { marginBottom: '10px' } },
      `Every change to what Aegis keeps about you, newest first — ${data.total.toLocaleString()} so far. Changes to single facts can be undone.`),
    h('div', { class: 'stack tight', style: { marginBottom: '12px' } },
      hs.target ? h('div', { class: 'line' }, h('span', { class: 'chip on' }, 'One fact only'),
        h('button', { class: 'btn sm ghost', type: 'button', onclick: () => { hs.target = ''; renderHistory(); } }, 'Show everything')) : null,
      chips([['', 'Everything'], ['memory', 'Facts'], ['topic', 'Topics'], ['episode', 'Conversations'], ['profile', 'Profile'], ['rules', 'Rules']], hs.kind, (v) => { hs.kind = v; }),
      chips([['', 'By anyone'], ['you', 'You'], ['auto', 'Learned automatically'], ['assistant', 'The assistant'], ['tidy', 'Tidy-up']], hs.by, (v) => { hs.by = v; })),
    hs.entries.length
      ? h('div', { class: 'card flush rows' }, hs.entries.map((e) => {
        const d = describe(e);
        const by = (e.cause || {}).by || 'system';
        return h('div', { class: 'change' },
          h('div', { class: `dot ${d.tone}` }, icon(dot[d.tone], 'sm')),
          h('div', { class: 'grow' }, h('div', {}, d.title), d.body ? h('div', { class: 'small pre', style: { marginTop: '2px' } }, d.body) : null,
            h('div', { class: 'meta small muted', style: { marginTop: '3px' } },
              `${BY_LABEL[by] || by}${e.origin && e.origin.name ? ` · from ${origin(e.origin)}` : ''} · ${when(e.ts, { time: true })}`)),
          e.can_undo ? h('button', { class: 'btn sm', type: 'button', onclick: async () => {
            try { await api(`/api/mind/history/${e.id}/undo`, { method: 'POST' }); toast('Undone'); state.memories = null; renderHistory(); } catch (error) { oops(error); }
          } }, icon('undo', 'sm'), 'Undo') : null);
      }))
      : h('div', { class: 'card empty' }, h('b', {}, 'Nothing here'), 'Changes are logged from now on; what was stored before the log existed has no entries.'),
    hs.next ? h('div', { class: 'center', style: { marginTop: '12px' } }, h('button', { class: 'btn', type: 'button', onclick: () => renderHistory(true) }, 'Show older')) : null);
}

// ── controls ───────────────────────────────────────────────────────────────

async function renderControls() {
  const o = await loadOverview(true);
  const boxes = o.sensitive_kinds.map((k) => {
    const box = h('input', { type: 'checkbox', value: k.id });
    box.checked = o.rules.blocked_kinds.includes(k.id);
    return h('label', { class: 'check' }, box, h('span', {}, k.label));
  });
  const purge = h('input', { type: 'checkbox' });
  const saveRules = async () => {
    const blocked = boxes.map((b) => b.querySelector('input')).filter((b) => b.checked).map((b) => b.value);
    try {
      const r = await api('/api/mind/rules', { method: 'PUT', body: { blocked_kinds: blocked, purge: purge.checked } });
      toast(r.purged ? `Saved, and ${plural(r.purged, 'item')} deleted` : 'Saved');
      state.memories = null;
      renderControls();
    } catch (error) { oops(error); }
  };
  const removed = o.rules.profile_suppressed || [];
  clear(view,
    h('div', { class: 'section-title' }, h('h2', {}, 'Switches')), switchesCard(o),
    h('div', { class: 'section-title' }, h('h2', {}, 'Never keep')),
    h('div', { class: 'card stack' },
      h('p', { class: 'small muted' }, 'Kinds of information the learner must not store. It marks what it learns by kind and drops these; it is a model’s judgement, so check History now and then.'),
      h('div', {}, boxes),
      h('label', { class: 'check' }, purge, h('span', {}, 'Also delete what is already stored and marked as one of these kinds')),
      h('div', {}, h('button', { class: 'btn primary', type: 'button', onclick: saveRules }, 'Save'))),
    removed.length ? [h('div', { class: 'section-title' }, h('h2', {}, 'Removed from your profile for good')),
      h('div', { class: 'card' }, h('ul', { class: 'small', style: { margin: 0, paddingLeft: '1.2em' } }, removed.map((r) => h('li', {}, r))))] : null,
    h('div', { class: 'section-title' }, h('h2', {}, 'Take it with you')),
    h('div', { class: 'card stack' },
      h('p', { class: 'small muted' }, 'One file with everything: profile, facts with their evidence, topics and notes, conversation digests, your rules and the full change log.'),
      h('div', { class: 'line wrap' },
        h('a', { class: 'btn', href: '/api/mind/export?format=json', download: '' }, icon('download', 'sm'), 'Download as JSON'),
        h('a', { class: 'btn', href: '/api/mind/export?format=md', download: '' }, icon('download', 'sm'), 'Download as text'))),
    h('div', { class: 'section-title' }, h('h2', {}, 'Start over')),
    h('div', { class: 'card stack danger-zone' },
      h('p', { class: 'small muted' }, `Deletes every fact (${o.counts.memories}), your profile, all topics (${o.counts.topics}), conversation digests (${o.counts.conversations}), your rules and the change log. Your chats are not deleted. This cannot be undone.`),
      h('div', {}, h('button', { class: 'btn danger', type: 'button', onclick: forgetEverything }, 'Forget everything about me'))));
}

function forgetEverything() {
  const input = h('input', { class: 'input', placeholder: 'FORGET', autocomplete: 'off' });
  sheet({
    title: 'Forget everything about you?',
    body: h('div', { class: 'stack' }, h('p', { class: 'muted' }, 'To confirm, type FORGET.'), input),
    actions: [{ label: 'Cancel', kind: 'ghost' }, { label: 'Forget everything', kind: 'danger solid', run: async () => {
      if (input.value.trim() !== 'FORGET') { toast('Type FORGET to confirm', { bad: true }); return false; }
      const r = await api('/api/mind/everything?confirm=yes', { method: 'DELETE' });
      toast(`Done — ${plural(r.memories_removed, 'fact')} and everything else deleted`);
      state.memories = null; state.topics = null;
      go('overview');
      return true;
    } }],
  });
}

// ── start ──────────────────────────────────────────────────────────────────

function fromHash() {
  const [tab, id] = location.hash.replace(/^#/, '').split('/');
  if (RENDER[tab]) { state.tab = tab; state.topicId = tab === 'topics' && id ? decodeURIComponent(id) : null; }
}

fromHash();
shell();
loadOverview().catch(() => {});
render();
window.addEventListener('hashchange', () => { fromHash(); shell(); render(); });
add(document.body, []);
