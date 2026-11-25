// The family screen: chat that does things, lists and plans, reminders,
// trackers, boards. Phone first, plain language, nothing about models or
// settings. Nobody picks a tool here: they say it, type it or show it, and the
// answer carries the door to whatever page fits.
import {
  api, h, add, clear, icon, iconButton, toast, oops, sheet, confirmSheet, field, when, md, plural,
} from './lite.js';
import { listen, isListening, speak, stopSpeaking, canListen } from './speech.js';
import { initReaderLinks } from '../js/readerLinks.js';
import working from '../js/working.js';
import readAloudLib from '../js/readAloud.js';

const app = document.getElementById('app');
const S = {
  me: null, counts: {}, due: [], view: 'chat', id: null,
  house: { home: '', units: 'us', language: 'en', keeper: '' },     // what is set once for the household
  today: null,                                                     // today at home: the forecast and tonight's sunset
  spoke: false,                                                    // the last message was spoken, so its answer is read aloud
  chat: { sid: null, busy: false, messages: [], abort: null },
  showThinking: true,                                              // a glimpse of what it is thinking while it works (this phone's choice)
  reading: { voices: [], voice: '', auto: false, reads_in: 'device' }, // the voice answers are read in, and whether by themselves (this person's choice)
};
try { S.showThinking = localStorage.getItem('aegis-home-thinking') !== '0'; } catch (_) { /* on, then */ }
const canSpeak = typeof window !== 'undefined' && ('speechSynthesis' in window || typeof Audio === 'function');
const SECTIONS = {
  chat: { label: 'Chat', icon: 'chat' }, lists: { label: 'Lists', icon: 'list' },
  reminders: { label: 'Reminders', icon: 'bell' }, trackers: { label: 'Trackers', icon: 'chart' },
  boards: { label: 'Boards', icon: 'board' },
};
// What is happening, in plain words, for every tool the family screen can reach.
const TOOL_LABEL = {
  manage_lists: 'Updating your list…', manage_notes: 'Writing that down…', manage_reminders: 'Setting a reminder…',
  manage_trackers: 'Logging that…', manage_boards: 'Pinning…', web_search: 'Looking that up…', web_fetch: 'Reading the page…',
  library_search: 'Checking the library…', library_browse: 'Looking along the shelves…', generate_image: 'Making a picture…',
  manage_memory: 'Making a note of that…', manage_kitchen: 'Setting the recipe out…', manage_calendar: 'Checking your calendar…',
  translate: 'Translating…', convert_units: 'Working it out…', map_position: 'Finding it on the map…', sun_moon: 'Checking the sun and moon…',
  date_math: 'Counting the days…', weather: 'Getting the forecast…', plan_outing: 'Putting the plan together…', plan_dinner: 'Setting it out…', identify_photo: 'Looking at the photo…', trigger_research: 'Starting a full report…',
  manage_research: 'Finding your reports…', search_chats: 'Looking through earlier chats…', ask_user: 'One question…',
  create_document: 'Writing it up…', edit_document: 'Changing it…', update_document: 'Rewriting it…', manage_documents: 'Finding the document…',
  manage_tasks: 'Setting that up to repeat…', manage_skills: 'Checking how I do that…', builtin_browser: 'Opening the page…',
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

// What this person is read to in: asked once at the start, and again whenever they change it.
readAloudLib.reading().then((mine) => { S.reading = mine; }).catch(() => {});

async function loadStart() {
  const start = await api('/api/home/start');
  S.me = start.me; S.counts = start.counts; S.due = start.due;
  S.house = { ...S.house, ...(start.house || {}) };
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
      clear(card, h('div', {}, 'Reminders and finished reports show up on this phone, even with the app closed.'),
        h('div', { class: 'line' }, act('Send a test', '', async () => { const r = await api('/api/push/test', { method: 'POST' }); toast(r.ok ? 'Sent — it should appear in a moment' : 'It could not be sent just now', { bad: !r.ok }); }),
          act('Turn off', 'ghost', pushOff)));
    } else if (state === 'off') {
      clear(card, h('div', {}, 'Get reminders, and word when a report is ready, on this phone — even with the app closed.'), h('div', {}, act('Turn on notifications', 'primary', async () => { await pushOn(); toast('Notifications are on'); })));
    } else if (state === 'blocked') {
      clear(card, h('div', { class: 'small muted' }, 'Notifications are blocked for this app. Allow them in your phone’s settings to get reminders here.'));
    } else {
      clear(card, h('div', { class: 'small muted' }, 'To get reminders on an iPhone, first add this page to your Home Screen (Share, then “Add to Home Screen”) and open it from there.'));
    }
  };
  draw();
  return card;
}

/** On or off, for this phone: a glimpse of what it is thinking while an answer is on its way. */
function thinkingRow() {
  const label = () => `Show what ${S.me.assistant} is thinking: ${S.showThinking ? 'on' : 'off'}`;
  const text = h('span', {}, label());
  const button = h('button', { class: 'btn block', type: 'button', style: { justifyContent: 'flex-start' }, 'aria-pressed': String(S.showThinking), onclick: () => {
    S.showThinking = !S.showThinking;
    try { localStorage.setItem('aegis-home-thinking', S.showThinking ? '1' : '0'); } catch (_) { /* this visit only, then */ }
    text.textContent = label();
    button.setAttribute('aria-pressed', String(S.showThinking));
    if (S.view === 'chat') drawChat();
  } }, icon('eye'), text);
  return button;
}

function accountSheet() {
  const row = (name, label, run) => h('button', { class: 'btn block', type: 'button', style: { justifyContent: 'flex-start' }, onclick: run }, icon(name), label);
  const s = sheet({
    title: S.me.name || S.me.username,
    body: h('div', { class: 'stack' },
      pushCard(),
      row('pot', 'Kitchen: recipes to cook from', () => { location.href = '/kitchen'; }),
      row('book', 'Reader: read from the library', () => { location.href = '/read'; }),
      row('globe', 'Translate: talk with someone in another language', () => { location.href = '/translate'; }),
      row('sun', 'Almanac: sun and moon, units, dates', () => { location.href = '/almanac'; }),
      row('search', 'My reports', () => { s.close(); myReports(); }),
      row('brain', `What ${S.me.assistant} knows about me`, () => { location.href = '/mind'; }),
      thinkingRow(),
      row('speaker', 'Reading aloud: the voice, and whether by itself', () => { s.close(); readingSheet(); }),
      S.me.full_app ? row('wrench', S.me.is_admin ? 'Switch to the workshop: the full app' : 'Open the full app', () => { location.href = '/switch/workshop'; }) : null,
      S.me.is_admin ? row('users', 'People and access', () => { location.href = '/admin'; }) : null,
      row('logout', 'Sign out', async () => { try { await api('/api/auth/logout', { method: 'POST' }); } catch (_) { /* leaving anyway */ } location.href = '/login'; })),
    actions: [{ label: 'Close', kind: 'ghost' }],
  });
  return s;
}

// ── chat ──────────────────────────────────────────────────────────────────
// One box. Say it, type it or show it; the answer comes back in the shape of
// the thing asked for — tiles for a sunset, a card for a mushroom, a list with
// its Open button — and carries the door to whatever page fits. Nobody picks a
// tool first.

// The words of a reply as a person should read them: no thinking, and none of the bracketed
// source numbers some models scatter about (where it came from is shown under the answer).
const clean = (text) => String(text || '').replace(/<think(?:ing)?>[\s\S]*?<\/think(?:ing)?>/gi, '').replace(/<think(?:ing)?>[\s\S]*$/i, '')
  .replace(/\s*【[^】]{0,40}】/g, '').trim();

function textOf(content) {
  if (typeof content === 'string') return content;
  if (Array.isArray(content)) return content.filter((p) => p && p.type === 'text').map((p) => p.text || '').join('\n');
  return '';
}

const input = h('textarea', { rows: '1', placeholder: 'Ask, tell or show me', 'aria-label': 'Message', enterkeyhint: 'send' });
const sendButton = h('button', { class: 'send', type: 'button', 'aria-label': 'Send' }, icon('send'));
const photoButton = h('button', { class: 'cam', type: 'button', 'aria-label': 'Show me a photo', title: 'Take or choose a photo' }, icon('camera'));
const micButton = h('button', { class: 'cam mic', type: 'button', 'aria-label': 'Speak', title: 'Speak instead of typing', hidden: true }, icon('mic'));
add(composer, h('div', { class: 'composer-stack' }, h('div', { class: 'composer-inner' }, photoButton, input, micButton, sendButton)));
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
  if (S.reading.auto) readAloudLib.unlock();              // inside the tap: the answer may then be read without another
  if (S.reading.auto || S.spoke) readAloudLib.warm();     // it will be read aloud: a recorded voice is got ready meanwhile
  input.value = ''; grow();
  send(text);
}

/** Put words in the box and send them: what a suggestion, a card's button or an offered choice does. */
function say(text) {
  if (S.reading.auto || S.spoke) { readAloudLib.unlock(); readAloudLib.warm(); }   // inside the tap: the answer may then be read without another
  if (S.chat.busy) { toast('One moment — I am still answering'); return; }
  go('chat');
  send(text);
}

// ── speaking and listening ────────────────────────────────────────────────

micButton.addEventListener('click', () => {
  if (S.chat.busy && !isListening()) { toast('One moment — I am still answering'); return; }
  const before = input.value ? `${input.value.trim()} ` : '';
  listen({
    onState: (on) => { micButton.classList.toggle('live', on); micButton.setAttribute('aria-label', on ? 'Stop listening' : 'Speak'); input.placeholder = on ? 'Listening…' : 'Ask, tell or show me'; },
    onText: (heard) => { input.value = before + heard; grow(); },
    onDone: (heard) => {
      if (!heard) return;
      input.value = before + heard; grow();
      S.spoke = true;                       // asked aloud: the answer is read aloud too
      submit();
    },
  });
});

function readAloud(message, button) {
  if (message.speaking) { stopSpeaking(); return; }
  const words = [clean(message.text), ...(message.cards || []).map(cardWords)].filter(Boolean).join('. ');
  if (!words) return;
  message.speaking = true;
  readAloudLib.unlock();                               // inside the tap: the sound may start a while later
  // The conversation is drawn again now and then (a new message, coming back to the page), and each drawing
  // makes the answer a new speaker: the one to change is the one on the screen now, not the one that was tapped.
  const speaker = () => (message.node && message.node.isConnected && message.node.querySelector('.say')) || (button && button.isConnected ? button : null);
  const shown = speaker();
  if (shown) clear(shown, icon('stop', 'sm'));
  // A recorded voice is made on the home server and takes a while: say how far along it is, under the answer.
  // The line is kept with the message, so that a new drawing of the answer carries it along (see bubble()).
  const line = h('div', { class: 'saying', role: 'status', hidden: true }, working.mark(), h('span', {}));
  message.saying = line;
  if (message.node && message.node.isConnected) message.node.append(line);
  speak(words, { lang: S.house.language || '',
    onState: (state) => {
      const getting = (state.phase === 'preparing' || state.phase === 'waiting') && state.kind === 'server';
      line.hidden = !getting;
      if (getting) line.lastChild.textContent = `${readAloudLib.describe(state)}…`;
    },
    onDone: () => {
      message.speaking = false;
      if (message.saying === line) message.saying = null;
      line.remove();
      const now = speaker();
      if (now) clear(now, icon('speaker', 'sm'));
    } });
}

/** The voice answers are read in, and whether they are read by themselves: this person's choice, kept with their account. */
function readingSheet() {
  const SAMPLE = 'This is how I sound when I read an answer to you.';
  const body = h('div', { class: 'stack' }, h('p', { class: 'muted small' }, 'Loading the voices…'));
  let playing = null;
  const box = sheet({ title: 'Reading aloud', body, actions: [{ label: 'Done', kind: 'primary' }], onclose: () => { if (playing) playing.stop(); } });
  const draw = async (fresh) => {
    const mine = await readAloudLib.reading(fresh);
    const deviceOk = await readAloudLib.deviceCanSpeak(S.house.language || '');
    S.reading = mine;
    const voices = (mine.voices || []).filter((v) => v.kind !== 'device' || deviceOk);
    const note = h('div', { class: 'small muted', role: 'status' });
    const hint = (voice) => {
      if (voice.kind === 'device') return 'Starts at once.';
      const pace = Number(voice.pace) || 0;
      const whose = voice.kind === 'recorded' ? (voice.mine ? 'Your own recording. ' : `Shared by ${voice.owner || 'someone at home'}. `) : '';
      return pace >= 1 ? `${whose}Starts after a moment.` : `${whose}Takes a little while to get ready before it starts.`;
    };
    const row = (voice) => {
      const on = mine.reads_in === voice.id;
      const hear = h('button', { class: 'chip', type: 'button', onclick: (event) => {
        event.stopPropagation();
        if (playing) { playing.stop(); return; }
        readAloudLib.unlock();
        hear.textContent = 'Stop';
        playing = readAloudLib.read(SAMPLE, { voice: voice.id, lang: S.house.language || '', onState: (state) => {
          if (state.phase === 'preparing' || state.phase === 'waiting') note.textContent = `${readAloudLib.describe(state)}…`;
          else if (state.phase === 'playing') note.textContent = '';
          else { playing = null; hear.textContent = 'Hear it'; note.textContent = state.phase === 'failed' ? (state.message || 'It could not be read aloud.') : ''; }
        } });
      } }, 'Hear it');
      return h('div', { class: `voice-choice${on ? ' on' : ''}`, role: 'radio', 'aria-checked': String(on), tabindex: '0', onclick: async () => {
        try { await readAloudLib.chooseReading({ voice: voice.id }); await draw(true); } catch (error) { toast(error.message || 'That could not be saved.', { bad: true }); }
      } },
        h('span', { class: 'dot' }), h('div', { class: 'grow' }, h('div', {}, voice.kind === 'device' ? 'This phone’s own voice' : voice.label), h('div', { class: 'small muted' }, hint(voice))), hear);
    };
    const auto = h('button', { class: 'btn block', type: 'button', style: { justifyContent: 'flex-start' }, 'aria-pressed': String(!!mine.auto), onclick: async () => {
      try { if (!mine.auto) readAloudLib.unlock(); await readAloudLib.chooseReading({ auto: !mine.auto }); await draw(true); } catch (error) { toast(error.message || 'That could not be saved.', { bad: true }); }
    } }, icon('speaker'), `Read every answer aloud: ${mine.auto ? 'on' : 'off'}`);
    clear(body,
      voices.length ? h('div', { class: 'stack tight', role: 'radiogroup', 'aria-label': 'Voice' }, voices.map(row))
        : h('p', { class: 'muted' }, 'This phone has no voice of its own, and no other voice has been set up yet.'),
      note,
      voices.length ? auto : null,
      h('p', { class: 'small muted' }, S.me.full_app
        ? 'A voice of your own is recorded in the full app: Studio, Music, Voice Lab. It is yours alone until you share it.'
        : `A voice of your own can be recorded in the Studio${S.house.keeper ? `: ask ${S.house.keeper}` : ''}.`));
  };
  draw(true).catch(oops);
  return box;
}

// ── photos ────────────────────────────────────────────────────────────────
// One tap: take or choose a photo and it is on its way. What kind of photo it
// is gets worked out here, not asked: a living thing or a track is identified
// against the library, print is read (and translated, and offered to the
// Kitchen when it is a recipe), anything else is described. Words already in
// the box go with it as the question. The card that comes back carries the
// corrections for when the sorting was wrong.

/** Ask the phone for a photo. Called inside the tap itself, or iOS refuses. */
function pickPhoto() {
  return new Promise((resolve) => {
    const chooser = h('input', { type: 'file', accept: 'image/*', style: { display: 'none' } });
    let done = false;
    const finish = (file) => { if (done) return; done = true; chooser.remove(); resolve(file || null); };
    chooser.addEventListener('change', () => finish(chooser.files && chooser.files[0]));
    chooser.addEventListener('cancel', () => finish(null));
    document.body.append(chooser);
    chooser.click();
  });
}

/** A phone photo is 12 megapixels; nothing here needs more than 2048 on a side. */
async function smaller(file, side = 2048) {
  try {
    if (!/^image\/(jpeg|png|webp)$/i.test(file.type || '') || file.size < 900 * 1024) return file;
    const bitmap = await createImageBitmap(file, { imageOrientation: 'from-image' });
    const scale = Math.min(1, side / Math.max(bitmap.width, bitmap.height));
    if (scale >= 1) { if (bitmap.close) bitmap.close(); return file; }
    const canvas = h('canvas', { width: String(Math.round(bitmap.width * scale)), height: String(Math.round(bitmap.height * scale)) });
    canvas.getContext('2d').drawImage(bitmap, 0, 0, canvas.width, canvas.height);
    if (bitmap.close) bitmap.close();
    const blob = await new Promise((resolve) => canvas.toBlob(resolve, 'image/jpeg', 0.9));
    if (!blob || blob.size >= file.size) return file;
    return new File([blob], `${(file.name || 'photo').replace(/\.[^.]+$/, '')}.jpg`, { type: 'image/jpeg' });
  } catch (_) { return file; }
}

async function uploadPhoto(file) {
  const form = new FormData();
  const photo = await smaller(file);
  form.append('files', photo, photo.name || 'photo.jpg');
  if (S.chat.sid) form.append('session_id', S.chat.sid);
  const sent = await api('/api/upload', { method: 'POST', form });
  const meta = sent && sent.files && sent.files[0];
  if (!meta || !meta.id) throw new Error('The photo did not upload. Please try again.');
  return { id: meta.id, name: meta.name, mime: meta.mime, size: meta.size };
}

photoButton.addEventListener('click', () => {
  if (S.chat.busy) { toast('One moment — I am still answering'); return; }
  pickPhoto().then((file) => {
    if (!file) return;
    const question = input.value.trim();
    input.value = ''; grow();
    lookAt({ file, question });
  });
});

/** Save a message into the chat, so the next question has it as context. */
function keep(role, content, metadata) {
  if (!S.chat.sid || !content) return;
  api(`/api/session/${encodeURIComponent(S.chat.sid)}/message`, { method: 'POST', body: { role, content, metadata } }).catch(() => {});
}

const WANT_WORDS = { text: 'It is a label or a page: read it', describe: 'Just describe it', living: 'It is a plant or an animal: identify it' };

/**
 * Send a photo to be looked at. `file` for a new photo, or `photo` ({ id, url })
 * to look again at one already sent; `want` is the correction, `question`
 * whatever the person said about it.
 */
async function lookAt({ file = null, photo = null, question = '', want = '' }) {
  go('chat');
  const url = photo ? photo.url : URL.createObjectURL(file);
  const asked = want ? WANT_WORDS[want] : (question || 'What is this?');
  S.chat.messages.push({ role: 'user', text: asked, images: photo && want ? [] : [url] });
  const reply = { role: 'assistant', text: '', status: file ? 'Sending the photo…' : 'Looking again…', cards: [], asked, retry: () => lookAt({ file, photo, question, want }) };
  S.chat.messages.push(reply);
  setBusy(true);
  drawChat();
  const started = Date.now();
  const timer = setInterval(() => {
    const seconds = (Date.now() - started) / 1000;
    reply.status = seconds < 6 ? 'Looking at the photo…' : seconds < 25 ? 'Working out what it is…' : seconds < 55 ? 'Checking it against the library…' : 'Still working. This one is taking a while…';
    updateWorking(reply);
  }, 1500);
  try {
    if (!S.chat.sid) S.chat.sid = await newSession();
    const meta = photo ? { id: photo.id } : await uploadPhoto(file);
    if (!photo) keep('user', asked, { attachments: [meta], source: 'identify' });
    else keep('user', asked, { source: 'identify' });
    const found = await api('/api/reference/identify', { method: 'POST', body: { file_id: meta.id, hint: question, session_id: S.chat.sid, want } });
    if (!found || !(found.card || found.markdown)) throw new Error('I could not make that out. Try a closer, sharper photo.');
    const card = found.card ? { ...found.card, photo_id: meta.id, photo_url: url, question } : null;
    if (card) reply.cards = [card]; else reply.text = found.markdown;
    reply.plain = found.markdown || '';
    keep('assistant', found.markdown || '', { source: 'identify', card });
  } catch (error) {
    failed(reply, error.message || '', error.status);
  } finally {
    clearInterval(timer);
    reply.status = null;
    setBusy(false);
    drawChat();
    if (S.spoke) { S.spoke = false; }
  }
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

// ── when it does not work ─────────────────────────────────────────────────
// The person gets a next step, and the one who runs the server can be told
// with one tap. What the server said is kept for them; it is never shown here.

function plainFailure(status, detail) {
  if (status === 429) return 'That is all the messages for today. There will be more tomorrow.';
  if (status === 403) return 'That is not switched on for you.';
  if (/no model|endpoint was removed|nothing is set up/i.test(detail || '')) return 'Nothing is set up to answer right now.';
  if (/photo|image|upload/i.test(detail || '') && detail.length < 120) return detail;
  return 'That did not work.';
}

function failed(reply, detail = '', status = 0) {
  reply.error = plainFailure(status, detail);
  reply.cause = String(detail || '').slice(0, 300) || (status ? `HTTP ${status}` : '');
}

async function tellKeeper(reply, button) {
  button.disabled = true;
  try {
    const told = await api('/api/home/problem', { method: 'POST', body: { asked: reply.asked || '', error: reply.cause || reply.error || '', session: S.chat.sid || '' } });
    reply.told = true;
    toast(told.anyone ? `Told ${S.house.keeper || 'them'} what you asked and that it did not work.` : 'Noted.');
    if (reply.node) fill(reply.node, reply);
  } catch (error) { button.disabled = false; oops(error); }
}

// ── while an answer is on its way ─────────────────────────────────────────
// The waiting line never stands still: a mark that moves, what is going on in
// plain words, how long it has been, a bar while the model reads (that wait can
// be measured), and — unless switched off — the latest line of what it is
// thinking. A screen that shows nothing for ten seconds looks broken.

function workingLine(message) {
  if (message.line && message.line.el) { updateWorking(message); return message.line.el; }
  const label = h('span', { class: 'working-label' });
  const secs = h('span', { class: 'working-secs' });
  const meter = working.bar();
  meter.el.hidden = true;
  const thought = h('div', { class: 'working-thought', hidden: true });
  const el = h('div', { class: 'working', role: 'status' }, h('div', { class: 'working-top' }, working.mark(), label, secs), meter.el, thought);
  message.line = { el, label, secs, meter, thought };
  if (!message.since) message.since = Date.now();
  updateWorking(message);
  return el;
}

function updateWorking(message) {
  const line = message.line;
  if (!line) return;
  line.label.textContent = message.status || '';
  const waited = Date.now() - (message.since || Date.now());
  line.secs.textContent = waited >= 3000 ? working.seconds(waited) : '';
  const reading = typeof message.reading === 'number';
  line.meter.el.hidden = !reading;
  if (reading) line.meter.set(message.reading);
  const glimpse = S.showThinking && message.thinkingNow ? working.lastThought(message.thinking) : '';
  line.thought.hidden = !glimpse;
  if (glimpse && line.thought.textContent !== glimpse) line.thought.textContent = glimpse;
}

// One clock for whatever is waiting: the seconds keep counting between events.
setInterval(() => {
  if (!S.chat.busy) return;
  const waiting = S.chat.messages[S.chat.messages.length - 1];
  if (waiting && waiting.status && waiting.line) updateWorking(waiting);
}, 500);

/** What was thought, for whoever wants to read it. */
function showThought(message) {
  sheet({ title: message.thoughtFor ? `Thought for ${working.seconds(message.thoughtFor * 1000) || '1 s'}` : 'What it was thinking',
    body: h('div', { class: 'thought-full' }, md(String(message.thinking || '').trim())), actions: [{ label: 'Close', kind: 'ghost' }] });
}

// ── an answer, drawn ──────────────────────────────────────────────────────

const TOOL_NOUN = { convert_units: 'worked', map_position: 'worked', sun_moon: 'worked', date_math: 'worked' };

function bubble(message) {
  if (message.role === 'user') {
    return h('div', { class: 'bubble me' }, (message.images || []).map((src) => h('img', { class: 'shot', src, alt: 'Your photo', loading: 'lazy' })), message.text);
  }
  const node = h('div', { class: `bubble them${message.error ? ' oops' : ''}${(message.cards || []).length ? ' wide' : ''}` });
  fill(node, message);
  return node;
}

/** Where the answer came from, in house words. */
function fromLine(message) {
  const sources = message.sources || [];
  const library = sources.filter((s) => s.kind === 'library');
  const web = sources.filter((s) => s.kind !== 'library');
  const used = message.used || new Set();
  const names = [];
  if (library.length) names.push('Home library');
  if (web.length) names.push('The web, today');
  if ([...used].some((tool) => TOOL_NOUN[tool])) names.push('Worked out');
  if (used.has('manage_memory') || used.has('search_chats')) names.push('From what you told me');
  if (!names.length) return null;
  const link = (source) => h('a', { class: 'chip', href: source.url, target: source.kind === 'library' ? null : '_blank', rel: source.kind === 'library' ? null : 'noopener noreferrer' },
    icon(source.kind === 'library' ? 'book' : 'external', 'sm'), String(source.title || 'Source').slice(0, 44));
  // a page a card already shows beside the thing it backs (a trip card's warnings) is not listed again
  const inCards = new Set((message.cards || []).flatMap((card) => (card.watch_for || []).map((item) => item.source && item.source.url)).filter(Boolean));
  const listed = [...library.slice(0, 3), ...web.slice(0, 3)].filter((source) => !inCards.has(source.url));
  return h('div', { class: 'from' },
    h('div', { class: 'small muted' }, 'From: ', names.join(' · ')),
    listed.length ? h('div', { class: 'chips' }, listed.map(link)) : null);
}

// The reply: any quiet notice, the words, the cards, a button for each thing
// it just touched, where it came from, and what can be done next.
function fill(node, message) {
  const text = message.error ? message.error : clean(message.text);
  const cards = message.cards || [];
  node.classList.toggle('wide', cards.length > 0);
  node.classList.toggle('oops', !!message.error);
  clear(node,
    (message.notes || []).map((note) => h('div', { class: 'note' }, icon('info', 'sm'), note)),
    text ? md(text) : (cards.length || message.status ? null : md('…')),
    cards.map((card) => drawCard(card, message)),
    (message.pictures || []).map((picture) => h('div', { class: 'made' },
      h('img', { src: picture.url, alt: picture.prompt || 'The picture', loading: 'lazy' }),
      h('div', { class: 'chips' },
        h('button', { class: 'chip', type: 'button', onclick: () => { input.value = 'Change the picture: '; grow(); input.focus(); } }, icon('edit', 'sm'), 'Change it'),
        (S.me.sections || []).includes('boards') ? h('button', { class: 'chip', type: 'button', onclick: () => { input.value = 'Pin that picture to my board called '; grow(); input.focus(); } }, icon('pin', 'sm'), 'Pin it to a board') : null))));
  if (message.asks && !message.answered) {
    node.append(h('div', { class: 'choices-row' }, message.asks.map((option) => h('button', { class: 'btn', type: 'button', onclick: () => { message.answered = true; say(option.label); } },
      option.label, option.description ? h('span', { class: 'small muted' }, option.description) : null))));
  }
  const links = (message.links || []).filter((l) => !text.includes(`(${l.href})`) && !cards.some((card) => card.href === l.href));
  if (links.length) {
    node.append(h('div', { class: 'chips', style: { marginTop: '8px' } }, links.map((l) => h('a', { class: 'chip accent', href: l.href }, /^(Follow along|Open)\b/.test(l.label) ? l.label : `Open ${l.label}`))));
  }
  const from = fromLine(message);
  if (from) node.append(from);
  if (message.offer && !message.error) {
    const offer = message.offer;
    node.append(offer.started
      ? h('div', { class: 'note' }, icon('clock', 'sm'), `I am working on the full report. I will tell you when it is ready (${offer.estimate}).`)
      : h('button', { class: 'btn block offer', type: 'button', onclick: (event) => digDeeper(message, event.currentTarget) }, icon('search', 'sm'),
        h('span', { class: 'grow' }, offer.label), h('span', { class: 'small muted' }, offer.estimate)));
  }
  if (message.error) {
    node.append(h('div', { class: 'chips', style: { marginTop: '10px' } },
      message.retry ? h('button', { class: 'chip accent', type: 'button', onclick: () => { if (!S.chat.busy) message.retry(); } }, icon('refresh', 'sm'), 'Try again') : null,
      S.house.keeper && !message.told ? h('button', { class: 'chip', type: 'button', onclick: (event) => tellKeeper(message, event.currentTarget) }, icon('user', 'sm'), `Tell ${S.house.keeper}`) : null,
      message.told ? h('span', { class: 'small muted' }, `${S.house.keeper} has been told.`) : null));
  } else if (!message.status && (text || cards.length) && canSpeak) {
    const speaker = h('button', { class: 'say', type: 'button', 'aria-label': 'Read it aloud', title: 'Read it aloud' }, icon(message.speaking ? 'stop' : 'speaker', 'sm'));
    speaker.addEventListener('click', () => readAloud(message, speaker));
    node.append(speaker);
  }
  if (S.showThinking && !message.status && !message.error && String(message.thinking || '').trim().length > 40) {
    node.append(h('button', { class: 'thought', type: 'button', title: 'See what it was thinking', onclick: () => showThought(message) },
      message.thoughtFor >= 1 ? `Thought for ${working.seconds(message.thoughtFor * 1000)}` : 'What it was thinking'));
  }
  if (message.saying) node.append(message.saying);      // it is being read aloud: the line that says how the voice is getting on
}

async function digDeeper(message, button) {
  button.disabled = true;
  try {
    await api('/api/research/start', { method: 'POST', body: { query: message.offer.topic } });
    message.offer.started = true;
    if (message.node) fill(message.node, message);
    toast('Started. You can carry on; I will tell you when it is ready.');
  } catch (error) { button.disabled = false; oops(error); }
}

// ── cards ─────────────────────────────────────────────────────────────────
// What a tool found, as the few things worth showing large. Each card carries
// its own next step.

const SKY_ICON = { sun: 'sun', moon: 'moon', 'cloud-sun': 'cloud-sun', cloud: 'cloud', rain: 'rain', snow: 'snow', storm: 'storm', fog: 'fog' };

const tiles = (list) => h('div', { class: 'card-tiles' }, (list || []).map((tile) => h('div', { class: 'card-tile' },
  h('div', { class: 'small muted' }, tile.label), h('div', { class: 'big' }, tile.icon ? icon(SKY_ICON[tile.icon] || 'cloud', 'sm') : null, tile.value))));

/** A card as a sentence, for reading aloud. */
function cardWords(card) {
  if (!card) return '';
  if (card.type === 'identification') return [card.name, card.sure && card.sure.words, card.hazard, card.lookalike ? `Lookalike: ${card.lookalike}` : ''].filter(Boolean).join('. ');
  if (card.type === 'trip') return (card.watch_for || []).length ? `Watch for: ${card.watch_for.map((item) => [item.name, item.text].filter(Boolean).join(': ')).join('. ')}` : '';
  if (card.type === 'reading') return card.translation ? card.translation.text : card.text;
  if (card.type === 'description') return card.translation ? `It says: ${card.translation.text}` : card.text;
  return '';
}

function correction(label, run) {
  return h('button', { class: 'chip', type: 'button', onclick: () => { if (S.chat.busy) toast('One moment — I am still answering'); else run(); } }, label);
}

function whereFound(card) {
  const where = h('input', { class: 'input', maxlength: '200', placeholder: 'On an oak stump, in October, by the creek…' });
  sheet({ title: 'Where and when did you find it?', body: field('It helps to narrow it down', where),
    actions: [{ label: 'Cancel', kind: 'ghost' }, { label: 'Look again', kind: 'primary', run: () => {
      if (!where.value.trim()) return false;
      lookAt({ photo: { id: card.photo_id, url: card.photo_url }, question: where.value.trim(), want: 'living' });
      return true;
    } }] });
}

async function startCooking(card, button) {
  button.disabled = true;
  try {
    const made = await api('/api/kitchen/recipes', { method: 'POST', body: { text: card.text } });
    const recipe = made.recipe || made;
    if (!recipe || !recipe.id) throw new Error('The Kitchen could not set that out.');
    location.href = `/kitchen?r=${encodeURIComponent(recipe.id)}`;
  } catch (error) { button.disabled = false; oops(error); }
}

function drawCard(card, message) {
  const again = (want) => lookAt({ photo: { id: card.photo_id, url: card.photo_url }, question: card.question || '', want });
  const canLookAgain = !!card.photo_id;
  if (card.type === 'weather' && card.days) {
    return h('div', { class: 'acard' },
      h('div', { class: 'acard-head' }, h('b', {}, card.title), h('span', { class: 'small muted' }, card.place)),
      h('div', { class: 'days' }, card.days.map((day) => h('div', { class: 'day' },
        h('span', { class: 'name' }, day.label), icon(SKY_ICON[day.icon] || 'cloud', 'sm'), h('span', { class: 'grow small muted' }, day.summary),
        day.rain && day.rain !== '0%' ? h('span', { class: 'small rain' }, day.rain) : null, h('span', { class: 'temps' }, day.high, h('span', { class: 'muted' }, ` ${day.low}`))))),
      (card.alerts || []).map((alert) => h('div', { class: 'alert' }, icon('alert', 'sm'), h('span', {}, h('b', {}, alert.event), alert.until ? ` ${alert.until}. ` : '. ', alert.text || ''))),
      card.source ? h('div', { class: 'tiny faint' }, card.source) : null);
  }
  if (card.type === 'weather') {
    return h('div', { class: 'acard' },
      h('div', { class: 'acard-head' }, icon(SKY_ICON[card.icon] || 'cloud', 'lg'), h('div', { class: 'grow' }, h('b', {}, `${card.title}: ${String(card.summary || '').toLowerCase()}`), h('div', { class: 'small muted' }, [card.place, card.date].filter(Boolean).join(' · ')))),
      tiles((card.tiles || []).filter((tile) => tile.label !== 'Wind')),
      (card.tiles || []).some((tile) => tile.label === 'Wind') ? h('div', { class: 'small muted' }, `Wind up to ${card.tiles.find((tile) => tile.label === 'Wind').value}`) : null,
      (card.parts || []).length ? h('div', { class: 'parts' }, card.parts.map((part) => h('div', { class: 'part' }, h('span', { class: 'small muted' }, part.label), icon(SKY_ICON[part.icon] || 'cloud', 'sm'), h('span', {}, part.text)))) : null,
      (card.alerts || []).map((alert) => h('div', { class: 'alert' }, icon('alert', 'sm'), h('span', {}, h('b', {}, alert.event), alert.until ? ` ${alert.until}. ` : '. ', alert.text || ''))),
      card.air ? h('div', { class: 'small muted' }, card.air) : null,
      h('div', { class: 'chips' },
        card.title !== 'Tomorrow' ? h('button', { class: 'chip', type: 'button', onclick: () => say('What is the weather tomorrow?') }, 'Tomorrow') : null,
        h('button', { class: 'chip', type: 'button', onclick: () => say('What is the weather this week?') }, 'The week'),
        h('button', { class: 'chip', type: 'button', onclick: () => { input.value = 'What is the weather in '; grow(); input.focus(); } }, 'Somewhere else?')),
      card.source ? h('div', { class: 'tiny faint' }, card.source) : null);
  }
  if (card.type === 'sun') {
    return h('div', { class: 'acard' },
      h('div', { class: 'acard-head' }, icon('sun', 'lg'), h('div', { class: 'grow' }, h('b', {}, card.title), h('div', { class: 'small muted' }, card.place))),
      tiles(card.tiles),
      card.moon ? h('div', { class: 'small muted' }, card.moon) : null,
      h('div', { class: 'chips' },
        h('button', { class: 'chip', type: 'button', onclick: () => say('When does it get dark tomorrow?') }, 'Another day'),
        h('button', { class: 'chip', type: 'button', onclick: () => { input.value = 'When does it get dark in '; grow(); input.focus(); } }, 'Somewhere else?')));
  }
  if (card.type === 'result') {
    return h('div', { class: 'acard result' }, h('div', { class: 'small muted' }, card.label), h('div', { class: 'huge' }, card.value),
      card.detail ? h('div', { class: 'small muted' }, card.detail) : null, card.note ? h('div', { class: 'small muted' }, card.note) : null);
  }
  if (card.type === 'translation') {
    const speaker = h('button', { class: 'chip accent', type: 'button' }, icon('speaker', 'sm'), 'Say it');
    speaker.addEventListener('click', () => { readAloudLib.unlock(); speak(card.text, { lang: card.to_code, otherLanguage: true }); });
    return h('div', { class: 'acard' },
      h('div', { class: 'small muted' }, `${card.from || 'Detected'} → ${card.to}`), h('div', { class: 'small' }, card.source),
      h('div', { class: 'huge said', lang: card.to_code || null }, card.text),
      h('div', { class: 'chips' }, speaker,
        h('a', { class: 'chip', href: `/translate?from=${encodeURIComponent(card.from_code || 'auto')}&to=${encodeURIComponent(card.to_code || '')}&talk=1` }, icon('swap', 'sm'), 'Keep talking')));
  }
  if (card.type === 'list') {
    return h('div', { class: 'acard' },
      h('div', { class: 'acard-head' }, icon('list', 'lg'), h('div', { class: 'grow' }, h('b', {}, card.title), h('div', { class: 'small muted' }, card.open ? `${card.open} to do` : 'Nothing left to do'))),
      (card.items || []).length ? h('ul', { class: 'plain' }, card.items.map((item) => h('li', {}, item)), card.more ? h('li', { class: 'muted' }, `and ${card.more} more`) : null) : null,
      h('div', { class: 'chips' }, h('a', { class: 'chip accent', href: card.href }, 'Open the list')));
  }
  if (card.type === 'identification') {
    const more = (card.more || []).length ? h('details', { class: 'more' }, h('summary', {}, 'How I can tell, and what else to check'),
      card.more.map((part) => [h('div', { class: 'part-label' }, part.label), md(part.text)])) : null;
    return h('div', { class: 'acard ident' },
      h('div', { class: 'acard-head' },
        card.photo && card.photo.url ? h('img', { class: 'ref', src: card.photo.url, alt: `Library photo of ${card.photo.title || card.name}`, loading: 'lazy' }) : null,
        h('div', { class: 'grow' }, h('div', { class: 'huge' }, card.name), card.latin ? h('div', { class: 'small muted latin' }, card.latin) : null,
          h('span', { class: `sure ${card.sure ? card.sure.level : 'low'}` }, card.sure ? card.sure.words : 'Not sure'))),
      card.hazard ? h('div', { class: 'alert' }, icon('alert', 'sm'), h('span', {}, card.hazard)) : null,
      card.lookalike ? h('div', { class: 'look' }, h('div', { class: 'part-label' }, 'Easily mistaken for'), card.lookalike) : null,
      card.library && card.library.url ? h('div', { class: 'chips' }, h('a', { class: 'chip accent', href: card.library.url }, icon('book', 'sm'), `Read about it: ${String(card.library.title).slice(0, 40)}`)) : null,
      more,
      canLookAgain ? h('div', { class: 'fixes' }, h('span', { class: 'small muted' }, 'Not right?'),
        correction('Add where I found it', () => whereFound(card)), correction('It is a label: read it', () => again('text')), correction('Just describe it', () => again('describe'))) : null);
  }
  if (card.type === 'reading') {
    const cook = card.recipe ? h('button', { class: 'chip accent', type: 'button' }, icon('pot', 'sm'), 'Start cooking') : null;
    if (cook) cook.addEventListener('click', () => startCooking(card, cook));
    return h('div', { class: 'acard' },
      card.summary ? h('div', { class: 'acard-head' }, icon('book', 'lg'), h('b', { class: 'grow' }, card.summary)) : null,
      card.translation ? [h('div', { class: 'part-label' }, `In ${card.translation.to} (it is written in ${card.translation.from})`), h('div', { class: 'read said' }, card.translation.text)] : null,
      h('details', { class: 'more', open: card.translation ? null : '' }, h('summary', {}, card.translation ? 'As written' : 'What it says'), h('div', { class: 'read' }, card.text || 'No words could be read.')),
      h('div', { class: 'chips' }, cook,
        (S.me.sections || []).includes('lists') && card.recipe ? h('button', { class: 'chip', type: 'button', onclick: () => say('Add the ingredients from that recipe to my shopping list') }, icon('list', 'sm'), 'Ingredients to my shopping list') : null),
      canLookAgain ? h('div', { class: 'fixes' }, h('span', { class: 'small muted' }, 'Not right?'),
        correction('It is a plant or an animal', () => again('living')), correction('Just describe it', () => again('describe'))) : null);
  }
  if (card.type === 'description') {
    const cook = card.recipe ? h('button', { class: 'chip accent', type: 'button' }, icon('pot', 'sm'), 'Start cooking') : null;
    if (cook) cook.addEventListener('click', () => startCooking({ text: card.words }, cook));
    return h('div', { class: 'acard' },
      card.translation ? [h('div', { class: 'part-label' }, `It says, in ${card.translation.to}`), h('div', { class: 'read said' }, card.translation.text)] : null,
      md(card.text || ''),
      card.words ? h('details', { class: 'more' }, h('summary', {}, card.translation ? `As written, in ${card.translation.from}` : 'The words on it'), h('div', { class: 'read' }, card.words)) : null,
      cook ? h('div', { class: 'chips' }, cook) : null,
      canLookAgain ? h('div', { class: 'fixes' }, h('span', { class: 'small muted' }, 'Not right?'),
        correction('It is a plant or an animal', () => again('living')), correction('It is a label: read it', () => again('text'))) : null);
  }
  if (card.type === 'recipe') {
    // A recipe set out in the Kitchen. When it was found for a request ("what can I make with…") it also
    // says which book it is from, what it still needs, and when to start.
    const needs = card.needs || [];
    return h('div', { class: 'acard' },
      h('div', { class: 'acard-head' }, icon('pot', 'lg'), h('div', { class: 'grow' }, h('b', {}, card.title),
        h('div', { class: 'small muted' }, [card.from ? `From ${card.from}` : '', card.serves, card.time].filter(Boolean).join(' · ')))),
      needs.length ? [h('div', { class: 'part-label' }, 'It also needs'), h('div', {}, needs.join(', '))] : null,
      card.added ? h('div', { class: 'note' }, icon('list', 'sm'), `On your ${card.added}: ${(card.added_items || []).join(', ')}`) : null,
      card.start ? h('div', { class: 'note' }, icon('clock', 'sm'), card.start) : null,
      h('div', { class: 'chips' }, h('a', { class: 'chip accent', href: card.href }, 'Start cooking'),
        (S.me.sections || []).includes('lists') && !card.added ? h('button', { class: 'chip', type: 'button', onclick: () => say(`Add the ingredients for ${card.title} to my shopping list`) },
          icon('list', 'sm'), needs.length ? 'Add what is missing to my shopping list' : 'Ingredients to my shopping list') : null,
        card.source_url ? h('a', { class: 'chip', href: card.source_url }, icon('book', 'sm'), 'The page it is from') : null));
  }
  if (card.type === 'trip') {
    const weather = card.weather;
    return h('div', { class: 'acard trip' },
      h('div', { class: 'acard-head' }, icon('target', 'lg'), h('div', { class: 'grow' }, h('b', {}, card.title), h('div', { class: 'small muted' }, [card.when, card.place].filter(Boolean).join(' · ')))),
      card.note ? h('div', { class: 'note' }, icon('info', 'sm'), card.note) : null,
      tiles(card.times),
      card.assumed ? h('div', { class: 'small muted' }, card.assumed) : null,
      weather ? h('div', { class: 'trip-line' }, icon(SKY_ICON[weather.icon] || 'cloud', 'sm'),
        h('span', {}, [String(weather.summary || ''), ...(weather.tiles || []).map((tile) => `${tile.label.toLowerCase()} ${tile.value}`)].filter(Boolean).join(', ')))
        : (card.no_forecast ? h('div', { class: 'note' }, icon('info', 'sm'), card.no_forecast) : null),
      ((weather && weather.alerts) || []).map((alert) => h('div', { class: 'alert' }, icon('alert', 'sm'), h('span', {}, h('b', {}, alert.event), alert.until ? ` ${alert.until}. ` : '. ', alert.text || ''))),
      (card.watch_for || []).length ? [h('div', { class: 'part-label' }, 'Watch for'), h('ul', { class: 'plain watch' }, card.watch_for.map((item) => h('li', {},
        h('b', {}, item.name), item.why ? h('span', { class: 'muted' }, ` · ${item.why}`) : null,
        item.text ? h('div', {}, item.text) : null,
        item.source && item.source.url ? h('a', { class: 'chip', href: item.source.url, title: item.source.title || '' }, icon('book', 'sm'), item.source.label || 'The page') : null)))] : null,
      card.list ? [h('div', { class: 'part-label' }, card.essentials ? 'What to pack: the usual essentials' : 'What to pack'),
        h('ul', { class: 'plain' }, (card.list.items || []).slice(0, 6).map((item) => h('li', {}, item)), (card.list.open || 0) > 6 ? h('li', { class: 'muted' }, `and ${card.list.open - 6} more`) : null)] : null,
      h('div', { class: 'chips' },
        card.list ? h('a', { class: 'chip accent', href: card.list.href }, icon('list', 'sm'), 'Open the packing list') : null,
        card.reminder ? h('a', { class: 'chip', href: '/home#reminders' }, icon('bell', 'sm'), `Reminder: ${card.reminder}`) : null,
        card.can_change_length ? h('button', { class: 'chip', type: 'button', onclick: () => { input.value = 'It will be about __ hours out and back.'; grow(); input.focus(); input.setSelectionRange(17, 19); } }, 'Change the length') : null),
      weather && weather.source ? h('div', { class: 'tiny faint' }, weather.source) : null);
  }
  if (card.type === 'notice') return h('div', { class: 'note' }, icon('info', 'sm'), card.text);
  return null;
}

// ── the first screen ──────────────────────────────────────────────────────

function todayCard() {
  const today = S.today || {};
  const weather = today.weather;
  const sun = today.sun;
  if (!weather && !sun) return null;
  const tile = (card, name) => ((card && card.tiles) || []).find((t) => t.label === name);
  const bits = [];
  if (weather) bits.push(h('span', {}, icon(SKY_ICON[weather.icon] || 'cloud', 'sm'), ` ${tile(weather, 'Now') ? `${tile(weather, 'Now').value} now · ` : ''}${String(weather.summary || '').toLowerCase()}, ${tile(weather, 'High') ? tile(weather, 'High').value : ''} / ${tile(weather, 'Low') ? tile(weather, 'Low').value : ''}`));
  if (sun && tile(sun, 'Sunset')) bits.push(h('span', {}, icon('sun', 'sm'), ` Sunset ${tile(sun, 'Sunset').value}`));
  const alert = weather && (weather.alerts || [])[0];
  return h('button', { class: 'today', type: 'button', 'aria-label': 'Today at home: open the forecast', onclick: () => say('What is the weather today?') },
    h('div', { class: 'small muted' }, `Today in ${String((weather || sun).place || 'your town').split(',')[0]}`), h('div', { class: 'line wrap' }, bits),
    alert ? h('div', { class: 'alert' }, icon('alert', 'sm'), h('span', {}, h('b', {}, alert.event), alert.until ? ` ${alert.until}` : '')) : null);
}

function drawChat() {
  drawHead(S.chat.messages.length ? S.me.assistant : greeting(), S.chat.messages.length ? '' : 'What can I help with?', null, [
    S.chat.messages.length ? iconButton('plus', 'New chat', newChat) : null,
    iconButton('clock', 'Earlier chats', earlierChats),
  ]);
  if (!S.chat.messages.length) {
    // Each line is a sentence that works when tapped — and shows that it can simply be asked.
    const has = (section) => (S.me.sections || []).includes(section);
    const ideas = [
      has('lists') ? ['list', 'Add milk and eggs to my shopping list'] : null,
      has('reminders') ? ['bell', 'Remind me to call Mom tomorrow at 10'] : null,
      S.house.home ? ['sun', 'When does it get dark tonight?'] : ['sun', 'How many days until Christmas?'],
      S.house.home ? ['cloud', 'Will it rain tomorrow?'] : null,
      ['pot', 'Walk me through a recipe for pancakes'],
      ['globe', 'How do I say “where is the pharmacy” in Spanish?'],
      ['book', 'Find me something to read about gardening'],
    ].filter(Boolean);
    clear(view, h('div', { class: 'screen stack' },
      todayCard(),
      S.due.length ? h('div', {}, h('div', { class: 'section-title' }, h('h2', {}, 'Coming up')), h('div', { class: 'card flush' }, S.due.map(dueRow))) : null,
      h('div', {}, h('div', { class: 'section-title' }, h('h2', {}, 'Just ask'))),
      h('div', { class: 'suggest' },
        h('button', { type: 'button', onclick: () => photoButton.click() }, icon('camera'), 'Show me a photo: a plant, a bug, a label, a recipe'),
        ideas.map(([name, text]) => h('button', { type: 'button', onclick: () => say(text.replace(/[“”]/g, '"')) }, icon(name), text)))));
    return;
  }
  const nodes = S.chat.messages.map((m) => {
    const node = bubble(m);
    m.node = node;
    return [node, m.status ? workingLine(m) : null];
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
  stopSpeaking();
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

/** A saved message as the screen shows it: its words, and the cards that came with it. */
function fromHistory(m) {
  const meta = m.metadata || {};
  const images = (meta.attachments || []).filter((a) => a && a.id && /^image\//.test(a.mime || '')).map((a) => `/api/upload/${encodeURIComponent(a.id)}?thumb=1`);
  if (m.role === 'user') return { role: 'user', text: textOf(m.content), images };
  const events = Array.isArray(meta.tool_events) ? meta.tool_events : [];
  const cards = [meta.card, ...events.map((event) => event && event.card)].filter((card) => card && card.type);
  const sources = Array.isArray(meta.web_sources) ? meta.web_sources : [];
  return { role: 'assistant', text: cards.some((card) => ['identification', 'reading', 'description'].includes(card.type)) ? '' : clean(textOf(m.content)),
    thinking: typeof meta.thinking === 'string' ? meta.thinking : '', thoughtFor: Number(meta.thinking_time) || 0,
    cards, sources, used: new Set(events.map((event) => event && event.tool).filter(Boolean)),
    pictures: events.filter((event) => event && event.image_url).map((event) => ({ url: event.image_url, prompt: event.image_prompt || '' })) };
}

async function openChat(sid) {
  try {
    const data = await api(`/api/history/${encodeURIComponent(sid)}`);
    S.chat = { sid, busy: false, abort: null, messages: (data.history || [])
      .filter((m) => m.role === 'user' || m.role === 'assistant').map(fromHistory)
      .filter((m) => m.text || (m.cards || []).length || (m.pictures || []).length) };
    go('chat');
    drawChat();
  } catch (error) { oops(error); }
}

async function newSession() {
  const chosen = await api('/api/default-chat');
  if (!chosen.model || !chosen.endpoint_id) throw new Error('Nothing is set up to answer right now.');
  const form = new FormData();
  form.append('name', ''); form.append('endpoint_id', chosen.endpoint_id); form.append('model', chosen.model); form.append('skip_validation', 'true');
  return (await api('/api/session', { method: 'POST', form })).id;
}

async function send(text) {
  S.chat.messages.push({ role: 'user', text, images: [] });
  const reply = { role: 'assistant', text: '', status: 'Reading your message…', since: Date.now(), thinking: '', thinkingNow: false, thoughtFor: 0,
    cards: [], notes: [], used: new Set(), asked: text, retry: () => send(text) };
  S.chat.messages.push(reply);
  setBusy(true);
  drawChat();
  let usedHome = false;
  let thinkingSince = 0;                                // when the stretch of thinking now going on began
  const thinkingEnds = () => { if (thinkingSince) { reply.thoughtFor += (Date.now() - thinkingSince) / 1000; thinkingSince = 0; } reply.thinkingNow = false; };
  const aloud = S.spoke;
  S.spoke = false;
  try {
    if (!S.chat.sid) S.chat.sid = await newSession();
    const form = new FormData();
    form.append('message', text); form.append('session', S.chat.sid); form.append('mode', 'agent');
    form.append('screen', 'home');                     // this screen draws an answer's card; one that does not gets it in words
    form.append('allow_web_search', 'true'); form.append('allow_bash', 'false'); form.append('think', 'off');
    S.chat.abort = new AbortController();
    const response = await fetch('/api/chat_stream', { method: 'POST', body: form, credentials: 'same-origin', headers: tzHeaders(), signal: S.chat.abort.signal });
    if (response.status === 401) { location.href = '/login'; return; }
    if (!response.ok || !response.body) {
      let detail = '';
      try { const d = await response.json(); detail = d.detail || d.error || ''; } catch (_) { /* no body */ }
      failed(reply, typeof detail === 'string' ? detail : '', response.status);
      return;
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
        else if (status && reply.status) updateWorking(reply);
        else if (reply.status) view.querySelector('.chat')?.append(workingLine(reply));
        const nearBottom = window.innerHeight + window.scrollY > document.body.scrollHeight - 260;
        if (nearBottom) window.scrollTo(0, document.body.scrollHeight);
      });
    };
    const handle = (event) => {
      if (errorNext || event.type === 'error' || (typeof event.status === 'number' && event.status >= 400)) {
        errorNext = false;
        failed(reply, String(event.error || event.detail || event.message || ''), typeof event.status === 'number' ? event.status : 0);
        return;
      }
      if (typeof event.delta === 'string') {
        reply.reading = null;
        if (event.thinking) {
          // What it is thinking: kept for whoever wants to read it, and its latest line shown while it thinks.
          if (!thinkingSince) thinkingSince = Date.now();
          reply.thinking += (reply.thinking && !reply.thinkingNow ? '\n\n' : '') + event.delta;
          reply.thinkingNow = true;
          if (!clean(reply.text)) reply.status = 'Thinking…';
        } else {
          thinkingEnds();
          reply.text += event.delta;
          if (clean(reply.text)) reply.status = null;
        }
      } else if (event.type === 'reading') {
        // How far the model has read: the one wait that can be measured, so it gets a real bar.
        const read = working.reading(event);
        if (read && !clean(reply.text)) {
          reply.reading = read.percent;
          reply.status = (event.round || 1) > 1 ? 'Reading what it found…' : 'Reading your message…';
        }
      } else if (event.type === 'notice') {
        // Waiting is said in the waiting line; the rest stays above the answer.
        if (event.code === 'waking') reply.status = `${event.text}…`;
        else if (event.text && !reply.notes.includes(event.text)) reply.notes.push(event.text);
      } else if (event.type === 'smart') {
        reply.status = `${event.title}…`;                 // a job with several steps: say what it is while it runs
        reply.job = event.key;
      } else if (event.type === 'tool_start') {
        thinkingEnds();
        reply.reading = null;
        reply.status = TOOL_LABEL[event.tool] || 'Working on it…';
        reply.used.add(event.tool);
        if (/^manage_(lists|notes|reminders|trackers|boards|kitchen)$/.test(event.tool || '')) usedHome = true;
      } else if (event.type === 'tool_output') {
        reply.status = clean(reply.text) ? null : 'Thinking…';
        if (event.card && event.card.type) {
          if (event.card.type === 'notice') { if (!reply.notes.includes(event.card.text)) reply.notes.push(event.card.text); } else reply.cards.push(event.card);
        }
        if (event.image_url) { reply.pictures = reply.pictures || []; reply.pictures.push({ url: event.image_url, prompt: event.image_prompt || '' }); }
        const found = String(event.output || '').matchAll(/\[([^\]]+)\]\((\/home#[a-z]+(?:\/[\w-]+)?|\/kitchen(?:\?r=[\w-]+)?)\)/g);
        for (const [, label, href] of found) {
          reply.links = reply.links || [];
          if (!reply.links.some((l) => l.href === href)) reply.links.push({ label, href });
        }
      } else if (event.type === 'web_sources') {
        reply.sources = [...(reply.sources || []), ...(Array.isArray(event.data) ? event.data : [])].filter((s, i, all) => s && s.url && all.findIndex((o) => o.url === s.url) === i);
      } else if (event.type === 'ask_user') {
        const asks = event.data || {};
        reply.asks = (asks.options || []).filter((o) => o && o.label).slice(0, 6).map((o) => ({ label: String(o.label), description: o.description ? String(o.description) : '' }));
      } else if (event.type === 'offer' && event.kind === 'report') {
        reply.offer = { label: event.label, estimate: event.estimate, topic: event.topic };
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
    if (!clean(reply.text) && !reply.error && !reply.cards.length && !(reply.pictures || []).length) reply.text = 'Done.';
  } catch (error) {
    if (error.name === 'AbortError') { if (!clean(reply.text) && !reply.cards.length) reply.text = 'Stopped.'; } else failed(reply, error.message || '');
  } finally {
    thinkingEnds();
    reply.status = null;
    reply.line = null;
    S.chat.abort = null;
    setBusy(false);
    drawChat();
    if (usedHome) loadStart().catch(() => {});
    if ((aloud || S.reading.auto) && !reply.error && !reply.speaking) readAloud(reply, reply.node ? reply.node.querySelector('.say') : null);
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

// ── full reports ──────────────────────────────────────────────────────────

async function myReports() {
  let found = [];
  try { found = (await api('/api/home/reports')).reports || []; } catch (error) { oops(error); return; }
  sheet({ title: 'My reports', body: h('div', { class: 'stack tight' },
    found.length ? found.map((report) => h('a', { class: 'btn block', href: report.href, style: { justifyContent: 'space-between', textDecoration: 'none' } },
      h('span', { class: 'clip' }, report.title), h('span', { class: 'small muted' }, when(report.finished))))
      : h('p', { class: 'muted' }, 'No reports yet. When a question deserves a long, sourced answer, a “Dig deeper” button appears under the reply.')),
  actions: [{ label: 'Close', kind: 'ghost' }] });
}

// ── what arrives while the app is open ────────────────────────────────────
// Reminders coming due, and work that finished meanwhile (a report). The same
// things reach the phone as notifications when the app is closed.

async function poll() {
  if (document.visibilityState !== 'visible') return;
  try {
    const data = await api('/api/tasks/notifications');
    const arrived = data.notifications || [];
    let reminders = 0;
    for (const n of arrived) {
      const id = String(n.task_id || '');
      if (id.startsWith('reminder-')) {
        reminders += 1;
        toast(`Reminder: ${n.task_name || n.body}`, { ms: 9000 });
        try { if ('Notification' in window && Notification.permission === 'granted') new Notification(n.task_name || 'Reminder', { body: n.body || '', tag: n.task_id }); } catch (_) { /* toast is enough */ }
      } else if (n.url && /^(report|problem|notice)-/.test(id)) {
        toast(`${n.task_name}${n.body ? `: ${String(n.body).slice(0, 80)}` : ''}`, { ms: 12000, bad: n.status === 'error', action: { label: 'Open', run: () => { location.href = n.url; } } });
      }
    }
    if (reminders) { await loadStart(); if (S.view === 'reminders') remindersView(); }
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
  initReaderLinks({ newTab: false });            // a source the assistant cites opens in the reader
  canListen().then((yes) => { micButton.hidden = !yes; }).catch(() => {});
  api('/api/home/today').then((today) => { S.today = today; if (S.view === 'chat' && !S.chat.messages.length) drawChat(); }).catch(() => {});
  window.addEventListener('hashchange', route);
  document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') { loadStart().then(() => { if (S.view === 'chat' && !S.chat.messages.length) drawChat(); }).catch(() => {}); poll(); } });
  setInterval(poll, 60000);
  await route();
  poll();
}());
