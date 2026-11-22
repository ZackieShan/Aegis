// The simple home screen: chat, lists and plans, reminders, trackers, boards.
// Phone first, plain language, nothing about models or settings.
import {
  api, h, add, clear, icon, iconButton, toast, oops, sheet, confirmSheet, field, when, md, plural,
} from './lite.js';

const app = document.getElementById('app');
const S = {
  me: null, counts: {}, due: [], view: 'chat', id: null,
  chat: { sid: null, busy: false, messages: [], abort: null },
};
const SECTIONS = {
  chat: { label: 'Chat', icon: 'chat' }, lists: { label: 'Lists', icon: 'list' },
  reminders: { label: 'Reminders', icon: 'bell' }, trackers: { label: 'Trackers', icon: 'chart' },
  boards: { label: 'Boards', icon: 'board' },
};
const TOOL_LABEL = {
  manage_lists: 'Updating your list…', manage_notes: 'Writing that down…', manage_reminders: 'Setting a reminder…',
  manage_trackers: 'Logging that…', manage_boards: 'Pinning…', web_search: 'Looking that up…', web_fetch: 'Reading…',
  library_search: 'Checking the library…', generate_image: 'Making a picture…', manage_memory: 'Making a note of that…',
  manage_calendar: 'Checking your calendar…',
};
const view = h('div', { id: 'view' });
const head = h('div', { class: 'hello' });
const nav = h('nav', { class: 'nav', 'aria-label': 'Sections' });
const composer = h('div', { class: 'composer' });

const tzHeaders = () => {
  try {
    return { 'x-tz-offset': String(-new Date().getTimezoneOffset()), 'x-tz-name': Intl.DateTimeFormat().resolvedOptions().timeZone || '' };
  } catch (_) { return {}; }
};

// ── frame ─────────────────────────────────────────────────────────────────

async function loadStart() {
  const start = await api('/api/home/start');
  S.me = start.me; S.counts = start.counts; S.due = start.due;
  const root = document.documentElement;
  root.setAttribute('data-theme', S.me.theme || 'auto');
  if (S.me.accent) root.style.setProperty('--accent', S.me.accent); else root.style.removeProperty('--accent');
  try { localStorage.setItem('aegis-home-look', JSON.stringify({ theme: S.me.theme || 'auto', accent: S.me.accent || '' })); } catch (_) { /* fine */ }
  drawNav();
}

function sections() {
  return ['chat', ...Object.keys(SECTIONS).filter((k) => k !== 'chat' && (S.me.sections || []).includes(k))];
}

function drawNav() {
  const badge = { reminders: S.counts.overdue || 0 };
  clear(nav, h('div', { class: 'nav-inner' }, sections().map((key) => h('button', {
    type: 'button', 'aria-current': S.view === key ? 'page' : null, onclick: () => go(key),
  }, icon(SECTIONS[key].icon), SECTIONS[key].label, badge[key] ? h('span', { class: 'dot' }, String(badge[key])) : null))));
}

function greeting() {
  const hour = new Date().getHours();
  const part = hour < 5 ? 'Hello' : hour < 12 ? 'Good morning' : hour < 18 ? 'Good afternoon' : 'Good evening';
  return S.me.name ? `${part}, ${S.me.name}` : part;
}

function drawHead(title, sub = '', back = null, actions = []) {
  clear(head,
    back ? iconButton('back', 'Back', back) : null,
    h('h1', {}, title, sub ? h('span', { class: 'sub' }, sub) : null),
    actions,
    h('button', { class: 'avatar', type: 'button', 'aria-label': 'Your account', onclick: accountSheet }, (S.me.name || S.me.username || '?').slice(0, 1).toUpperCase()));
}

function go(viewName, id = null) {
  const hash = id ? `#${viewName}/${id}` : `#${viewName}`;
  if (location.hash === hash) route(); else location.hash = hash;
}

async function route() {
  const [name, id] = location.hash.replace(/^#/, '').split('/');
  S.view = sections().includes(name) ? name : 'chat';
  S.id = id ? decodeURIComponent(id) : null;
  document.body.classList.toggle('on-chat', S.view === 'chat');
  composer.hidden = S.view !== 'chat';
  drawNav();
  try {
    await VIEWS[S.view]();
  } catch (error) {
    clear(view, h('div', { class: 'screen' }, h('div', { class: 'card empty' }, h('b', {}, 'That did not load'), error.message)));
  }
  if (S.view !== 'chat') window.scrollTo(0, 0);
}

// ── notifications on this phone ───────────────────────────────────────────

const keyBytes = (text) => {
  const raw = atob((text + '='.repeat((4 - (text.length % 4)) % 4)).replace(/-/g, '+').replace(/_/g, '/'));
  return Uint8Array.from(raw, (c) => c.charCodeAt(0));
};

async function pushState() {
  if (!('serviceWorker' in navigator) || !('PushManager' in window) || !('Notification' in window)) return 'unsupported';
  if (Notification.permission === 'denied') return 'blocked';
  try {
    const registration = await navigator.serviceWorker.getRegistration('/');
    return registration && await registration.pushManager.getSubscription() ? 'on' : 'off';
  } catch (_) { return 'off'; }
}

async function pushOn() {
  const registration = await navigator.serviceWorker.register('/home-sw.js', { scope: '/' });
  await navigator.serviceWorker.ready;
  if (await Notification.requestPermission() !== 'granted') {
    throw new Error('Notifications were not allowed. You can allow them for this app in your phone’s settings.');
  }
  const { key } = await api('/api/push/key');
  const subscription = await registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: keyBytes(key) });
  await api('/api/push/subscribe', { method: 'POST', body: subscription.toJSON() });
  await api('/api/push/test', { method: 'POST' });
}

async function pushOff() {
  const registration = await navigator.serviceWorker.getRegistration('/');
  const subscription = registration ? await registration.pushManager.getSubscription() : null;
  if (!subscription) return;
  try { await api('/api/push/unsubscribe', { method: 'POST', body: { endpoint: subscription.endpoint } }); } catch (_) { /* gone either way */ }
  await subscription.unsubscribe();
}

function pushCard() {
  const card = h('div', { class: 'card stack tight' }, h('div', { class: 'small muted' }, 'Checking notifications…'));
  const draw = async () => {
    const state = await pushState();
    const act = (label, kind, run) => h('button', { class: `btn sm ${kind}`.trim(), type: 'button', onclick: async (event) => {
      event.currentTarget.disabled = true;
      try { await run(); } catch (error) { oops(error); }
      draw();
    } }, label);
    if (state === 'on') {
      clear(card, h('div', {}, 'Reminders show up on this phone, even with the app closed.'),
        h('div', { class: 'line' }, act('Send a test', '', async () => { const r = await api('/api/push/test', { method: 'POST' }); toast(r.ok ? 'Sent — it should appear in a moment' : 'It could not be sent just now', { bad: !r.ok }); }),
          act('Turn off', 'ghost', pushOff)));
    } else if (state === 'off') {
      clear(card, h('div', {}, 'Get reminders on this phone, even with the app closed.'), h('div', {}, act('Turn on notifications', 'primary', async () => { await pushOn(); toast('Notifications are on'); })));
    } else if (state === 'blocked') {
      clear(card, h('div', { class: 'small muted' }, 'Notifications are blocked for this app. Allow them in your phone’s settings to get reminders here.'));
    } else {
      clear(card, h('div', { class: 'small muted' }, 'To get reminders on an iPhone, first add this page to your Home Screen (Share, then “Add to Home Screen”) and open it from there.'));
    }
  };
  draw();
  return card;
}

function accountSheet() {
  const row = (name, label, run) => h('button', { class: 'btn block', type: 'button', style: { justifyContent: 'flex-start' }, onclick: run }, icon(name), label);
  const s = sheet({
    title: S.me.name || S.me.username,
    body: h('div', { class: 'stack' },
      (S.me.sections || []).includes('reminders') ? pushCard() : null,
      row('brain', `What ${S.me.assistant} knows about me`, () => { location.href = '/mind'; }),
      S.me.full_app ? row('settings', 'Open the full app', () => { location.href = '/?full=1'; }) : null,
      S.me.is_admin ? row('users', 'People and access', () => { location.href = '/admin'; }) : null,
      row('logout', 'Sign out', async () => { try { await api('/api/auth/logout', { method: 'POST' }); } catch (_) { /* leaving anyway */ } location.href = '/login'; })),
    actions: [{ label: 'Close', kind: 'ghost' }],
  });
  return s;
}

// ── chat ──────────────────────────────────────────────────────────────────

const clean = (text) => String(text || '').replace(/<think(?:ing)?>[\s\S]*?<\/think(?:ing)?>/gi, '').replace(/<think(?:ing)?>[\s\S]*$/i, '').trim();

function textOf(content) {
  if (typeof content === 'string') return content;
  if (Array.isArray(content)) return content.filter((p) => p && p.type === 'text').map((p) => p.text || '').join('\n');
  return '';
}

const input = h('textarea', { rows: '1', placeholder: 'Ask, or tell me what you need', 'aria-label': 'Message', enterkeyhint: 'send' });
const sendButton = h('button', { class: 'send', type: 'button', 'aria-label': 'Send' }, icon('send'));
add(composer, h('div', { class: 'composer-inner' }, input, sendButton));
const grow = () => { input.style.height = 'auto'; input.style.height = `${Math.min(input.scrollHeight, 140)}px`; };
input.addEventListener('input', grow);
// Tell the server someone is typing, so background work (learning from the
// last chat) holds off and the next reply is not kept waiting.
let lastBeat = 0;
input.addEventListener('input', () => {
  if (Date.now() - lastBeat < 4000) return;
  lastBeat = Date.now();
  fetch('/api/activity/heartbeat', { method: 'POST', credentials: 'same-origin' }).catch(() => {});
});
input.addEventListener('keydown', (event) => {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); submit(); }
});
sendButton.addEventListener('click', () => (S.chat.busy ? stop() : submit()));

function submit() {
  const text = input.value.trim();
  if (!text || S.chat.busy) return;
  input.value = ''; grow();
  send(text);
}

function setBusy(busy) {
  S.chat.busy = busy;
  clear(sendButton, icon(busy ? 'x' : 'send'));
  sendButton.setAttribute('aria-label', busy ? 'Stop' : 'Send');
}

async function stop() {
  if (S.chat.abort) S.chat.abort.abort();
  if (S.chat.sid) { try { await api(`/api/chat/stop/${S.chat.sid}`, { method: 'POST' }); } catch (_) { /* already finished */ } }
}

function bubble(message) {
  if (message.role === 'user') return h('div', { class: 'bubble me' }, message.text);
  const node = h('div', { class: `bubble them${message.error ? ' oops' : ''}` });
  fill(node, message);
  return node;
}

// The reply, plus a button for each thing it just touched (a list, the
// reminders, a tracker, a board) so it is one tap away.
function fill(node, message) {
  const text = message.error ? message.error : clean(message.text);
  clear(node, md(text || '…'));
  const links = (message.links || []).filter((l) => !text.includes(`(${l.href})`));
  if (links.length) {
    node.append(h('div', { class: 'chips', style: { marginTop: '8px' } }, links.map((l) => h('a', { class: 'chip accent', href: l.href }, `Open ${l.label}`))));
  }
}

function drawChat() {
  drawHead(S.chat.messages.length ? S.me.assistant : greeting(), S.chat.messages.length ? '' : 'What can I help with?', null, [
    S.chat.messages.length ? iconButton('plus', 'New chat', newChat) : null,
    iconButton('clock', 'Earlier chats', earlierChats),
  ]);
  if (!S.chat.messages.length) {
    const ideas = [
      ['list', 'Add milk and eggs to my shopping list'], ['bell', 'Remind me to call Mom tomorrow at 10'],
      ['target', 'Help me plan a birthday party'], ['board', 'Find ideas for a cozy reading nook'],
    ].filter(([name]) => name === 'target' || (S.me.sections || []).includes({ list: 'lists', bell: 'reminders', board: 'boards' }[name]));
    clear(view, h('div', { class: 'screen stack' },
      S.due.length ? h('div', {}, h('div', { class: 'section-title' }, h('h2', {}, 'Coming up')), h('div', { class: 'card flush' }, S.due.map(dueRow))) : null,
      h('div', {}, h('div', { class: 'section-title' }, h('h2', {}, 'Try saying'))),
      h('div', { class: 'suggest' }, ideas.map(([name, text]) => h('button', { type: 'button', onclick: () => { input.value = text; grow(); input.focus(); } }, icon(name), text)))));
    return;
  }
  const nodes = S.chat.messages.map((m) => {
    const node = bubble(m);
    m.node = node;
    return [node, m.status ? h('div', { class: 'working' }, h('span', { class: 'spinner' }), m.status) : null];
  });
  clear(view, h('div', { class: 'chat' }, nodes));
  window.scrollTo(0, document.body.scrollHeight);
}

function dueRow(r) {
  const tick = h('button', { class: 'tick', type: 'button', 'aria-label': 'Done', onclick: async () => {
    tick.classList.add('on');
    try { await api(`/api/home/reminders/${r.id}`, { method: 'PUT', body: { done: true } }); await loadStart(); if (S.view === 'chat' && !S.chat.messages.length) drawChat(); } catch (error) { oops(error); }
  } }, icon('check', 'sm'));
  return h('div', { class: 'due' }, tick,
    h('div', { class: 'grow' }, h('div', {}, r.text), h('div', { class: `small ${r.overdue ? 'late' : 'muted'}` }, when(r.due, { time: true }))));
}

function newChat() {
  if (S.chat.busy) return;
  S.chat = { sid: null, busy: false, messages: [], abort: null };
  drawChat();
}

async function earlierChats() {
  let sessions = [];
  try { sessions = await api('/api/sessions'); } catch (error) { oops(error); return; }
  sessions = (Array.isArray(sessions) ? sessions : []).filter((s) => !s.archived && (s.message_count || 0) > 0)
    .sort((a, b) => String(b.last_message_at || b.updated_at || '').localeCompare(String(a.last_message_at || a.updated_at || ''))).slice(0, 40);
  const box = sheet({ title: 'Earlier chats', body: h('div', { class: 'stack tight' },
    sessions.length ? sessions.map((s) => h('button', { class: 'btn block', type: 'button', style: { justifyContent: 'space-between' }, onclick: async () => { box.close(); await openChat(s.id); } },
      h('span', { class: 'clip' }, s.name || 'Chat'), h('span', { class: 'small muted' }, when(s.last_message_at || s.updated_at))))
      : h('p', { class: 'muted' }, 'No earlier chats yet.')), actions: [{ label: 'Close', kind: 'ghost' }] });
}

async function openChat(sid) {
  try {
    const data = await api(`/api/history/${encodeURIComponent(sid)}`);
    S.chat = { sid, busy: false, abort: null, messages: (data.history || [])
      .filter((m) => m.role === 'user' || m.role === 'assistant')
      .map((m) => ({ role: m.role, text: m.role === 'assistant' ? clean(textOf(m.content)) : textOf(m.content) }))
      .filter((m) => m.text) };
    go('chat');
    drawChat();
  } catch (error) { oops(error); }
}

async function newSession() {
  const chosen = await api('/api/default-chat');
  if (!chosen.model || !chosen.endpoint_id) throw new Error('Nothing is set up to answer yet. Ask whoever runs this for you.');
  const form = new FormData();
  form.append('name', ''); form.append('endpoint_id', chosen.endpoint_id); form.append('model', chosen.model); form.append('skip_validation', 'true');
  return (await api('/api/session', { method: 'POST', form })).id;
}

async function send(text) {
  S.chat.messages.push({ role: 'user', text });
  const reply = { role: 'assistant', text: '', status: 'Thinking…' };
  S.chat.messages.push(reply);
  setBusy(true);
  drawChat();
  let usedHome = false;
  try {
    if (!S.chat.sid) S.chat.sid = await newSession();
    const form = new FormData();
    form.append('message', text); form.append('session', S.chat.sid); form.append('mode', 'agent');
    form.append('allow_web_search', 'true'); form.append('allow_bash', 'false'); form.append('think', 'off');
    S.chat.abort = new AbortController();
    const response = await fetch('/api/chat_stream', { method: 'POST', body: form, credentials: 'same-origin', headers: tzHeaders(), signal: S.chat.abort.signal });
    if (response.status === 401) { location.href = '/login'; return; }
    if (!response.ok || !response.body) {
      let detail = '';
      try { const d = await response.json(); detail = d.detail || d.error || ''; } catch (_) { /* no body */ }
      throw new Error(typeof detail === 'string' && detail ? detail : 'That did not go through.');
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let errorNext = false;
    let frame = 0;
    const paint = () => {
      if (frame) return;
      frame = requestAnimationFrame(() => {
        frame = 0;
        if (reply.node) fill(reply.node, reply);
        const status = view.querySelector('.working');
        if (status && !reply.status) status.remove();
        else if (status && reply.status) status.lastChild.textContent = reply.status;
        else if (reply.status) view.querySelector('.chat')?.append(h('div', { class: 'working' }, h('span', { class: 'spinner' }), reply.status));
        const nearBottom = window.innerHeight + window.scrollY > document.body.scrollHeight - 260;
        if (nearBottom) window.scrollTo(0, document.body.scrollHeight);
      });
    };
    const handle = (event) => {
      if (errorNext || event.type === 'error' || (typeof event.status === 'number' && event.status >= 400)) {
        errorNext = false;
        reply.error = 'Sorry — something went wrong while I was working on that. Please try again.';
        return;
      }
      if (typeof event.delta === 'string') {
        if (!event.thinking) { reply.text += event.delta; if (clean(reply.text)) reply.status = null; }
      } else if (event.type === 'tool_start') {
        reply.status = TOOL_LABEL[event.tool] || 'Working on it…';
        if (/^manage_(lists|notes|reminders|trackers|boards)$/.test(event.tool || '')) usedHome = true;
      } else if (event.type === 'tool_output') {
        reply.status = 'Thinking…';
        const found = String(event.output || '').matchAll(/\[([^\]]+)\]\((\/home#[a-z]+(?:\/[\w-]+)?)\)/g);
        for (const [, label, href] of found) {
          reply.links = reply.links || [];
          if (!reply.links.some((l) => l.href === href)) reply.links.push({ label, href });
        }
      }
      paint();
    };
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop();
      for (const line of lines) {
        if (line.startsWith('event: error')) { errorNext = true; continue; }
        if (!line.startsWith('data: ')) continue;
        const payload = line.slice(6).trim();
        if (payload === '[DONE]') continue;
        try { handle(JSON.parse(payload)); } catch (_) { /* a partial or foreign line */ }
      }
    }
    if (!clean(reply.text) && !reply.error) reply.text = 'Done.';
  } catch (error) {
    if (error.name === 'AbortError') { if (!clean(reply.text)) reply.text = 'Stopped.'; } else reply.error = error.message || 'That did not go through. Please try again.';
  } finally {
    reply.status = null;
    S.chat.abort = null;
    setBusy(false);
    drawChat();
    if (usedHome) loadStart().catch(() => {});
  }
}

// ── lists ─────────────────────────────────────────────────────────────────

async function listsView() {
  if (S.id) return listDetail(S.id);
  const all = (await api('/api/home/lists')).lists;
  drawHead('Lists', all.length ? `${plural(S.counts.open_items || all.reduce((n, l) => n + l.open, 0), 'thing')} to do` : '');
  const tile = (l) => h('button', { class: 'tile', type: 'button', onclick: () => go('lists', l.id) },
    h('h3', { class: 'clamp2' }, l.title),
    h('div', { class: 'peek' }, l.items.filter((i) => !i.done).slice(0, 3).map((i) => h('div', { class: 'clip' }, i.text))),
    h('div', { class: 'left' }, l.open ? `${l.open} left` : (l.done ? 'All done' : 'Empty')));
  const group = (title, items) => (items.length ? [h('div', { class: 'section-title' }, h('h2', {}, title)), h('div', { class: 'tiles' }, items.map(tile))] : null);
  clear(view, h('div', { class: 'screen' },
    h('div', { class: 'addbar' },
      h('button', { class: 'btn primary', type: 'button', onclick: () => newList('list') }, icon('plus', 'sm'), 'New list'),
      h('button', { class: 'btn', type: 'button', onclick: () => newList('plan') }, icon('target', 'sm'), 'New plan')),
    all.length ? [group('Lists', all.filter((l) => l.kind !== 'plan')), group('Plans', all.filter((l) => l.kind === 'plan'))]
      : h('div', { class: 'card empty' }, h('b', {}, 'No lists yet'), 'Start one here, or just say “add milk to my shopping list” in Chat.')));
  return null;
}

function newList(kind) {
  const name = h('input', { class: 'input', maxlength: '120', placeholder: kind === 'plan' ? 'e.g. Emma’s birthday' : 'e.g. Groceries' });
  sheet({
    title: kind === 'plan' ? 'New plan' : 'New list', body: field('Name', name),
    actions: [{ label: 'Cancel', kind: 'ghost' }, { label: 'Create', kind: 'primary', run: async () => {
      if (!name.value.trim()) return false;
      const made = (await api('/api/home/lists', { method: 'POST', body: { title: name.value.trim(), kind } })).list;
      go('lists', made.id);
      return true;
    } }],
  });
}

async function listDetail(id) {
  let list = (await api(`/api/home/lists/${id}`)).list;
  const body = h('div');
  const entry = h('input', { class: 'input', placeholder: 'Add something', maxlength: '300', enterkeyhint: 'done' });
  const addIt = async () => {
    const text = entry.value.trim();
    if (!text) return;
    entry.value = '';
    try { list = (await api(`/api/home/lists/${id}/items`, { method: 'POST', body: { texts: text.split(',').map((t) => t.trim()).filter(Boolean) } })).list; draw(); entry.focus(); } catch (error) { oops(error); }
  };
  entry.addEventListener('keydown', (event) => { if (event.key === 'Enter') addIt(); });
  const row = (item) => {
    const tick = h('button', { class: `tick${item.done ? ' on' : ''}`, type: 'button', 'aria-label': item.done ? 'Not done' : 'Done', onclick: async () => {
      try { list = (await api(`/api/home/lists/${id}/items/${item.id}`, { method: 'PUT', body: { done: !item.done } })).list; draw(); } catch (error) { oops(error); }
    } }, icon('check', 'sm'));
    return h('div', { class: `item${item.done ? ' done' : ''}` }, tick, h('div', { class: 'text' }, item.text),
      iconButton('x', 'Remove', async () => {
        try { list = (await api(`/api/home/lists/${id}/items/${item.id}`, { method: 'DELETE' })).list; draw(); } catch (error) { oops(error); }
      }));
  };
  function draw() {
    drawHead(list.title, list.items.length ? `${list.open} left · ${list.done} done` : '', () => go('lists'), [iconButton('more', 'More', menu)]);
    const open = list.items.filter((i) => !i.done);
    const done = list.items.filter((i) => i.done);
    clear(body,
      open.length ? h('div', { class: 'card flush' }, open.map(row)) : h('div', { class: 'card empty' }, list.items.length ? h('b', {}, 'All done') : h('b', {}, 'Nothing on it yet'), 'Type above to add something.'),
      done.length ? [h('div', { class: 'section-title' }, h('h2', {}, 'Done'), h('button', { class: 'btn sm ghost', type: 'button', onclick: async () => {
        try { list = (await api(`/api/home/lists/${id}/clear-done`, { method: 'POST' })).list; draw(); } catch (error) { oops(error); }
      } }, 'Clear')), h('div', { class: 'card flush' }, done.map(row))] : null);
    loadStart().catch(() => {});
  }
  function menu() {
    const title = h('input', { class: 'input', value: list.title, maxlength: '120' });
    sheet({ title: 'This list', body: h('div', { class: 'stack' }, field('Name', title),
      h('button', { class: 'btn danger block', type: 'button', onclick: async () => {
        if (!await confirmSheet(`“${list.title}” and everything on it will be deleted.`, { title: 'Delete this list?', ok: 'Delete', danger: true })) return;
        try { await api(`/api/home/lists/${id}`, { method: 'DELETE' }); document.querySelectorAll('.scrim').forEach((n) => n.remove()); toast('Deleted'); await loadStart(); go('lists'); } catch (error) { oops(error); }
      } }, icon('trash', 'sm'), 'Delete this list')),
    actions: [{ label: 'Cancel', kind: 'ghost' }, { label: 'Save', kind: 'primary', run: async () => {
      if (title.value.trim() && title.value.trim() !== list.title) { list = (await api(`/api/home/lists/${id}`, { method: 'PUT', body: { title: title.value.trim() } })).list; draw(); }
    } }] });
  }
  clear(view, h('div', { class: 'screen' }, h('div', { class: 'addline' }, entry, h('button', { class: 'btn primary', type: 'button', onclick: addIt }, 'Add')), body));
  draw();
  return null;
}

// ── reminders ─────────────────────────────────────────────────────────────

const pad = (n) => String(n).padStart(2, '0');
const dateValue = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const timeValue = (d) => `${pad(d.getHours())}:${pad(d.getMinutes())}`;
const REPEAT = { none: 'Does not repeat', daily: 'Every day', weekly: 'Every week', monthly: 'Every month', yearly: 'Every year' };

async function remindersView() {
  const all = (await api('/api/home/reminders')).reminders;
  drawHead('Reminders', all.length ? plural(all.length, 'reminder') : '');
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const dayOf = (r) => { const d = new Date(r.due); d.setHours(0, 0, 0, 0); return Math.round((d - today) / 86400000); };
  const groups = [['Overdue', all.filter((r) => r.overdue)], ['Today', all.filter((r) => !r.overdue && dayOf(r) === 0)],
    ['Tomorrow', all.filter((r) => dayOf(r) === 1)], ['Later', all.filter((r) => dayOf(r) > 1)]];
  const row = (r) => {
    const tick = h('button', { class: 'tick', type: 'button', 'aria-label': 'Done', onclick: async (event) => {
      event.stopPropagation();
      tick.classList.add('on');
      try { const out = (await api(`/api/home/reminders/${r.id}`, { method: 'PUT', body: { done: true } })).reminder; toast(out.done ? 'Done' : `Done — next ${when(out.due, { time: true })}`); await loadStart(); remindersView(); } catch (error) { oops(error); }
    } }, icon('check', 'sm'));
    return h('div', { class: 'due', style: { cursor: 'pointer' }, onclick: () => reminderSheet(r) }, tick,
      h('div', { class: 'grow' }, h('div', {}, r.text),
        h('div', { class: `small ${r.overdue ? 'late' : 'muted'}` }, when(r.due, { time: true }), r.repeat !== 'none' ? ` · ${REPEAT[r.repeat].toLowerCase()}` : '')));
  };
  clear(view, h('div', { class: 'screen' },
    h('div', { class: 'addbar' }, h('button', { class: 'btn primary', type: 'button', onclick: () => reminderSheet() }, icon('plus', 'sm'), 'New reminder')),
    all.length ? groups.filter(([, items]) => items.length).map(([title, items]) => [
      h('div', { class: 'section-title' }, h('h2', { class: title === 'Overdue' ? 'late' : '' }, title)), h('div', { class: 'card flush' }, items.map(row))])
      : h('div', { class: 'card empty' }, h('b', {}, 'No reminders'), 'Add one here, or say “remind me to call Mom tomorrow at 10” in Chat.')));
}

function reminderSheet(existing = null) {
  const start = existing ? new Date(existing.due) : new Date(Date.now() + 3600000);
  if (!existing) start.setMinutes(Math.ceil(start.getMinutes() / 5) * 5, 0, 0);
  const text = h('input', { class: 'input', maxlength: '300', placeholder: 'What should I remind you of?', value: existing ? existing.text : '' });
  const date = h('input', { class: 'input', type: 'date', value: dateValue(start) });
  const time = h('input', { class: 'input', type: 'time', value: timeValue(start) });
  const repeat = h('select', { class: 'input' }, Object.entries(REPEAT).map(([value, label]) => { const o = h('option', { value }, label); o.selected = (existing ? existing.repeat : 'none') === value; return o; }));
  const set = (d) => { date.value = dateValue(d); time.value = timeValue(d); };
  const quick = [
    ['In an hour', () => new Date(Date.now() + 3600000)],
    ['This evening', () => { const d = new Date(); if (d.getHours() >= 18) d.setDate(d.getDate() + 1); d.setHours(18, 0, 0, 0); return d; }],
    ['Tomorrow morning', () => { const d = new Date(); d.setDate(d.getDate() + 1); d.setHours(9, 0, 0, 0); return d; }],
  ];
  const due = () => new Date(`${date.value}T${time.value || '09:00'}`);
  const actions = [{ label: 'Cancel', kind: 'ghost' }];
  if (existing) {
    actions.push({ label: 'Delete', kind: 'danger', run: async () => { await api(`/api/home/reminders/${existing.id}`, { method: 'DELETE' }); toast('Reminder removed'); await loadStart(); remindersView(); } });
  }
  actions.push({ label: 'Save', kind: 'primary', run: async () => {
    if (!text.value.trim()) { toast('What should it say?', { bad: true }); return false; }
    if (Number.isNaN(due().getTime())) { toast('Pick a day and a time', { bad: true }); return false; }
    const body = { text: text.value.trim(), due: due().toISOString(), repeat: repeat.value };
    if (existing) await api(`/api/home/reminders/${existing.id}`, { method: 'PUT', body });
    else await api('/api/home/reminders', { method: 'POST', body });
    toast(`I’ll remind you ${when(due(), { time: true })}`);
    await loadStart();
    if (S.view === 'reminders') remindersView();
    return true;
  } });
  sheet({ title: existing ? 'Reminder' : 'New reminder', body: h('div', { class: 'stack' }, field('Remind me to', text),
    h('div', { class: 'chips' }, quick.map(([label, make]) => h('button', { class: 'chip', type: 'button', onclick: () => set(make()) }, label))),
    h('div', { class: 'line' }, h('div', { class: 'grow' }, field('Day', date)), h('div', { class: 'grow' }, field('Time', time))),
    field('Repeat', repeat)), actions });
}

// ── trackers ──────────────────────────────────────────────────────────────

function bars(series, tracker, tall = false) {
  const peak = Math.max(...series.map((d) => d.value), tracker.goal || 0, tracker.kind === 'scale' ? 5 : 1);
  return h('div', { class: `bars${tall ? ' tall' : ''}`, role: 'img', 'aria-label': `Last ${series.length} days` },
    series.map((d) => h('i', { class: d.value ? 'on' : '', title: `${d.day}: ${d.value}`, style: { height: `${Math.max(d.value / peak, 0.06) * 100}%` } })));
}

function trackerCard(t, refresh) {
  const log = async (body) => { try { await api(`/api/home/trackers/${t.id}/log`, { method: 'POST', body }); refresh(); } catch (error) { oops(error); } };
  let control;
  if (t.kind === 'check') {
    control = h('button', { class: `bigdone${t.done_today ? ' on' : ''}`, type: 'button', onclick: () => log(t.done_today ? { value: 0 } : {}) },
      icon('check', 'sm'), t.done_today ? 'Done today' : 'Mark done today');
  } else if (t.kind === 'scale') {
    control = h('div', { class: 'scale', role: 'group', 'aria-label': 'Today, 1 to 5' }, [1, 2, 3, 4, 5].map((n) => h('button', { class: t.today === n ? 'on' : '', type: 'button', onclick: () => log({ value: n }) }, String(n))));
  } else {
    const amount = h('input', { class: 'input', type: 'number', inputmode: 'decimal', step: 'any', placeholder: 'Amount', style: { width: '110px' } });
    control = h('div', { class: 'stack tight' },
      t.goal ? h('div', { class: 'progress' }, h('i', { style: { width: `${Math.min(t.today / t.goal, 1) * 100}%` } })) : null,
      h('div', { class: 'line' },
        h('button', { class: 'btn', type: 'button', onclick: () => log({ value: 1 }) }, '+1'), amount,
        h('button', { class: 'btn', type: 'button', onclick: () => { if (amount.value) log({ value: Number(amount.value) }); } }, 'Add')));
  }
  const unit = t.unit ? ` ${t.unit}` : '';
  const today = t.kind === 'check' ? '' : t.kind === 'scale' ? (t.today ? `Today: ${t.today} of 5` : 'Not rated today')
    : `Today: ${t.today}${t.goal ? ` of ${t.goal}` : ''}${unit}`;
  return h('div', { class: 'card tracker' },
    h('div', { class: 'line' }, h('button', { class: 'btn ghost grow', type: 'button', style: { justifyContent: 'flex-start', padding: '0', minHeight: '0' }, onclick: () => go('trackers', t.id) }, h('h3', {}, t.name)),
      t.streak > 1 ? h('span', { class: 'chip accent' }, `${t.streak} days in a row`) : null),
    today ? h('div', { class: 'small muted' }, today) : null, control, bars(t.series, t));
}

async function trackersView() {
  if (S.id) return trackerDetail(S.id);
  const draw = async () => {
    const all = (await api('/api/home/trackers')).trackers;
    drawHead('Trackers', all.length ? `${all.filter((t) => t.done_today).length} of ${all.length} done today` : '');
    clear(view, h('div', { class: 'screen' },
      h('div', { class: 'addbar' }, h('button', { class: 'btn primary', type: 'button', onclick: () => trackerSheet(draw) }, icon('plus', 'sm'), 'New tracker')),
      all.length ? h('div', { class: 'stack' }, all.map((t) => trackerCard(t, draw)))
        : h('div', { class: 'card empty' }, h('b', {}, 'Nothing tracked yet'), 'Follow a habit, an amount or how you feel, day by day. Or say “track my water, 8 glasses a day” in Chat.')));
  };
  await draw();
  return null;
}

function trackerSheet(done) {
  const name = h('input', { class: 'input', maxlength: '60', placeholder: 'e.g. Water, Walk, Mood' });
  const unit = h('input', { class: 'input', maxlength: '20', placeholder: 'glasses, km, pages…' });
  const goal = h('input', { class: 'input', type: 'number', inputmode: 'decimal', step: 'any', placeholder: 'optional' });
  const extra = h('div', { class: 'line', hidden: true }, h('div', { class: 'grow' }, field('Counted in', unit)), h('div', { class: 'grow' }, field('Daily goal', goal)));
  let kind = 'check';
  const kinds = [['check', 'Done or not', 'Took vitamins, went for a walk'], ['number', 'An amount', 'Glasses of water, steps, pages read'], ['scale', 'A rating from 1 to 5', 'Mood, sleep, energy']];
  const choices = kinds.map(([value, title, example]) => {
    const radio = h('input', { type: 'radio', name: 'kind', value });
    radio.checked = value === kind;
    const node = h('label', { class: `choice${value === kind ? ' on' : ''}` }, radio, h('span', {}, title, h('span', { class: 'small muted', style: { display: 'block' } }, example)));
    radio.addEventListener('change', () => { kind = value; choices.forEach((c) => c.classList.toggle('on', c === node)); extra.hidden = kind !== 'number'; });
    return node;
  });
  sheet({ title: 'New tracker', body: h('div', { class: 'stack' }, field('What do you want to follow?', name), choices, extra),
    actions: [{ label: 'Cancel', kind: 'ghost' }, { label: 'Start', kind: 'primary', run: async () => {
      if (!name.value.trim()) return false;
      await api('/api/home/trackers', { method: 'POST', body: { name: name.value.trim(), kind, unit: unit.value.trim(), goal: goal.value ? Number(goal.value) : null } });
      await loadStart();
      done();
      return true;
    } }] });
}

async function trackerDetail(id) {
  const draw = async () => {
    const t = (await api(`/api/home/trackers/${id}?days=30`)).tracker;
    drawHead(t.name, t.streak > 1 ? `${t.streak} days in a row` : '', () => go('trackers'), [iconButton('trash', 'Delete tracker', async () => {
      if (!await confirmSheet(`“${t.name}” and its whole history will be deleted.`, { title: 'Delete this tracker?', ok: 'Delete', danger: true })) return;
      try { await api(`/api/home/trackers/${id}`, { method: 'DELETE' }); toast('Deleted'); await loadStart(); go('trackers'); } catch (error) { oops(error); }
    })]);
    const unit = t.unit ? ` ${t.unit}` : '';
    clear(view, h('div', { class: 'screen stack' }, trackerCard(Object.assign({}, t, { series: t.series.slice(-14) }), draw),
      h('div', { class: 'card stack' }, h('h3', {}, 'Last 30 days'), bars(t.series, t, true),
        h('div', { class: 'small muted' }, t.kind === 'check' ? `${t.series.filter((d) => d.value).length} days done` : `This week: ${t.week_total}${unit}${t.week_average ? ` · ${t.week_average} a day on days logged` : ''}`)),
      t.entries.length ? h('div', {}, h('div', { class: 'section-title' }, h('h2', {}, 'Entries')),
        h('div', { class: 'card flush rows' }, t.entries.map((e) => h('div', { class: 'row' },
          h('div', { class: 'main' }, h('div', {}, t.kind === 'check' ? 'Done' : `${e.value}${t.kind === 'scale' ? ' of 5' : unit}`, e.note ? ` — ${e.note}` : ''), h('div', { class: 'meta' }, when(`${e.day}T12:00:00`))),
          iconButton('x', 'Remove entry', async () => { try { await api(`/api/home/trackers/${id}/entries/${e.id}`, { method: 'DELETE' }); draw(); } catch (error) { oops(error); } }))))) : null));
  };
  await draw();
  return null;
}

// ── boards ────────────────────────────────────────────────────────────────

async function boardsView() {
  if (S.id) return boardDetail(S.id);
  const all = (await api('/api/home/boards')).boards;
  drawHead('Boards', all.length ? plural(all.length, 'board') : '');
  const mosaic = (b) => {
    if (!b.covers.length) return h('div', { class: 'mosaic one' }, h('div', { class: 'blank' }, icon('board', 'lg')));
    const shown = b.covers.length >= 4 ? b.covers.slice(0, 4) : b.covers.slice(0, 1);
    return h('div', { class: `mosaic${shown.length === 1 ? ' one' : ''}` }, shown.map((src) => h('img', { src, alt: '', loading: 'lazy' })));
  };
  clear(view, h('div', { class: 'screen' },
    h('div', { class: 'addbar' }, h('button', { class: 'btn primary', type: 'button', onclick: newBoard }, icon('plus', 'sm'), 'New board')),
    all.length ? h('div', { class: 'boards' }, all.map((b) => h('button', { class: 'board', type: 'button', onclick: () => go('boards', b.id) }, mosaic(b), h('h3', { class: 'clip' }, b.name), h('div', { class: 'small muted' }, plural(b.pin_count, 'pin')))))
      : h('div', { class: 'card empty' }, h('b', {}, 'No boards yet'), 'Collect pictures, links and notes around an idea — a room, a party, recipes to try.')));
  return null;
}

function newBoard() {
  const name = h('input', { class: 'input', maxlength: '80', placeholder: 'e.g. Patio ideas' });
  sheet({ title: 'New board', body: field('Name', name), actions: [{ label: 'Cancel', kind: 'ghost' }, { label: 'Create', kind: 'primary', run: async () => {
    if (!name.value.trim()) return false;
    const made = (await api('/api/home/boards', { method: 'POST', body: { name: name.value.trim() } })).board;
    await loadStart();
    go('boards', made.id);
    return true;
  } }] });
}

async function boardDetail(id) {
  const file = h('input', { type: 'file', accept: 'image/*', multiple: true, hidden: true });
  const draw = async () => {
    const b = (await api(`/api/home/boards/${id}`)).board;
    drawHead(b.name, plural(b.pin_count, 'pin'), () => go('boards'), [iconButton('more', 'More', () => boardMenu(b, draw))]);
    const pin = (p) => h('button', { class: `pin ${p.kind}`, type: 'button', onclick: () => pinSheet(p, b, draw) },
      p.has_image ? h('img', { src: p.thumb, alt: p.title || '', loading: 'lazy', style: p.width ? { aspectRatio: `${p.width} / ${p.height}` } : {} }) : null,
      (p.title || p.site || (!p.has_image && p.note)) ? h('div', { class: 'cap' }, p.kind === 'note' && !p.title ? p.note : (p.title || p.note.slice(0, 120)), p.site ? h('span', { class: 'site' }, p.site) : null) : null);
    clear(view, h('div', { class: 'screen' }, file,
      h('div', { class: 'addbar' },
        h('button', { class: 'btn primary', type: 'button', onclick: () => file.click() }, icon('camera', 'sm'), 'Photo'),
        h('button', { class: 'btn', type: 'button', onclick: () => linkSheet(id, draw) }, icon('link', 'sm'), 'Link'),
        h('button', { class: 'btn', type: 'button', onclick: () => noteSheet(id, draw) }, icon('edit', 'sm'), 'Note'),
        h('button', { class: 'btn', type: 'button', onclick: () => { input.value = `Find me ideas for “${b.name}” and pin the best ones to my ${b.name} board`; go('chat'); grow(); input.focus(); } }, icon('sparkle', 'sm'), 'Find ideas')),
      b.pins.length ? h('div', { class: 'pins' }, b.pins.map(pin)) : h('div', { class: 'card empty' }, h('b', {}, 'Nothing pinned yet'), 'Add a photo, paste a link, or write a note.')));
  };
  file.addEventListener('change', async () => {
    const files = [...file.files];
    file.value = '';
    for (const picked of files) {
      const form = new FormData();
      form.append('file', picked);
      try { await api(`/api/home/boards/${id}/pins/upload`, { method: 'POST', form }); } catch (error) { oops(error); }
    }
    if (files.length) { toast(files.length > 1 ? `${files.length} photos pinned` : 'Photo pinned'); draw(); }
  });
  await draw();
  return null;
}

function linkSheet(id, done) {
  const url = h('input', { class: 'input', type: 'url', inputmode: 'url', placeholder: 'Paste a web address', autocapitalize: 'off' });
  sheet({ title: 'Pin a link', body: h('div', { class: 'stack' }, field('Link', url, 'The page’s name and picture are fetched for you when the internet is reachable.')),
    actions: [{ label: 'Cancel', kind: 'ghost' }, { label: 'Pin', kind: 'primary', run: async () => {
      if (!url.value.trim()) return false;
      await api(`/api/home/boards/${id}/pins`, { method: 'POST', body: { url: url.value.trim() } });
      toast('Pinned');
      done();
      return true;
    } }] });
}

function noteSheet(id, done) {
  const text = h('textarea', { class: 'input', rows: '4', maxlength: '2000', placeholder: 'Write a note' });
  sheet({ title: 'Pin a note', body: field('Note', text), actions: [{ label: 'Cancel', kind: 'ghost' }, { label: 'Pin', kind: 'primary', run: async () => {
    if (!text.value.trim()) return false;
    await api(`/api/home/boards/${id}/pins`, { method: 'POST', body: { text: text.value.trim() } });
    done();
    return true;
  } }] });
}

function pinSheet(p, board, done) {
  const title = h('input', { class: 'input', maxlength: '160', value: p.title || '', placeholder: 'Title' });
  const note = h('textarea', { class: 'input', rows: '3', maxlength: '2000', placeholder: 'Note' });
  note.value = p.note || '';
  sheet({ body: h('div', { class: 'stack' },
    p.has_image ? h('img', { class: 'pinbig', src: p.image, alt: p.title || '' }) : null,
    p.url ? h('a', { class: 'btn block', href: p.url, target: '_blank', rel: 'noopener noreferrer' }, icon('link', 'sm'), `Open ${p.site || 'link'}`) : null,
    field('Title', title), field('Note', note)),
  actions: [
    { label: 'Delete', kind: 'danger', run: async () => { await api(`/api/home/pins/${p.id}`, { method: 'DELETE' }); toast('Pin removed'); done(); } },
    { label: 'Close', kind: 'ghost' },
    { label: 'Save', kind: 'primary', run: async () => {
      if (title.value !== (p.title || '') || note.value !== (p.note || '')) { await api(`/api/home/pins/${p.id}`, { method: 'PUT', body: { title: title.value, note: note.value } }); done(); }
    } }] });
}

function boardMenu(b, done) {
  const name = h('input', { class: 'input', maxlength: '80', value: b.name });
  sheet({ title: 'This board', body: h('div', { class: 'stack' }, field('Name', name),
    h('button', { class: 'btn danger block', type: 'button', onclick: async () => {
      if (!await confirmSheet(`“${b.name}” and its ${plural(b.pin_count, 'pin')} will be deleted.`, { title: 'Delete this board?', ok: 'Delete', danger: true })) return;
      try { await api(`/api/home/boards/${b.id}`, { method: 'DELETE' }); document.querySelectorAll('.scrim').forEach((n) => n.remove()); toast('Deleted'); await loadStart(); go('boards'); } catch (error) { oops(error); }
    } }, icon('trash', 'sm'), 'Delete this board')),
  actions: [{ label: 'Cancel', kind: 'ghost' }, { label: 'Save', kind: 'primary', run: async () => {
    if (name.value.trim() && name.value.trim() !== b.name) { await api(`/api/home/boards/${b.id}`, { method: 'PUT', body: { name: name.value.trim() } }); done(); }
  } }] });
}

// ── reminders arriving while the app is open ──────────────────────────────

async function poll() {
  if (document.visibilityState !== 'visible') return;
  try {
    const data = await api('/api/tasks/notifications');
    const arrived = (data.notifications || []).filter((n) => String(n.task_id || '').startsWith('reminder-'));
    for (const n of arrived) {
      toast(`Reminder: ${n.task_name || n.body}`, { ms: 9000 });
      try { if ('Notification' in window && Notification.permission === 'granted') new Notification(n.task_name || 'Reminder', { body: n.body || '', tag: n.task_id }); } catch (_) { /* toast is enough */ }
    }
    if (arrived.length) { await loadStart(); if (S.view === 'reminders') remindersView(); }
  } catch (_) { /* try again next time */ }
}

// ── start ─────────────────────────────────────────────────────────────────

const VIEWS = { chat: async () => drawChat(), lists: listsView, reminders: remindersView, trackers: trackersView, boards: boardsView };

(async function boot() {
  try {
    await loadStart();
  } catch (error) {
    clear(app, h('div', { class: 'screen' }, h('div', { class: 'card empty' }, h('b', {}, 'Could not open your home screen'), error.message)));
    return;
  }
  clear(app, head, view, composer, nav);
  window.addEventListener('hashchange', route);
  document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') { loadStart().then(() => { if (S.view === 'chat' && !S.chat.messages.length) drawChat(); }).catch(() => {}); poll(); } });
  setInterval(poll, 60000);
  await route();
  poll();
}());
