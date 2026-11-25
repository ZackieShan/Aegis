// The translator (/translate): type, speak or photograph something and read
// (or hear) it in another language. Everything is done by the server's own
// translation model; nothing leaves the house.
//
//   /translate                  the translator
//   /translate?to=es&text=…     opened with something to translate
//
// Long text arrives a paragraph at a time (the server answers a line of JSON
// per paragraph), so the first lines can be read while the rest is done.

import { api, h, clear, icon, iconButton, toast, oops, confirmSheet, ago } from './lite.js';

const app = document.getElementById('app');
const S = { me: null, languages: [], maxChars: 20000, from: 'auto', to: 'es', last: null, busy: false, speaking: null, listening: null, serverEars: false };
const KEY = 'aegis-translate-langs';
const homeHref = () => (S.me && S.me.home) || '/';
const nameOf = (code) => { const found = S.languages.find((l) => l.code === code); return found ? found.name : code; };

function remember() { try { localStorage.setItem(KEY, JSON.stringify({ from: S.from, to: S.to })); } catch (_) { /* private window */ } }
function recall() {
  try { const kept = JSON.parse(localStorage.getItem(KEY) || '{}'); if (kept.to) S.to = kept.to; if (kept.from) S.from = kept.from; } catch (_) { /* defaults */ }
  const asked = new URLSearchParams(location.search);
  if (asked.get('to')) S.to = asked.get('to');
  if (asked.get('from')) S.from = asked.get('from');
}

// ── the page ───────────────────────────────────────────────────────────────
const el = {};

function picker(withDetect) {
  const select = h('select', { class: 'input', 'aria-label': withDetect ? 'Translate from' : 'Translate into' });
  if (withDetect) select.append(h('option', { value: 'auto' }, 'Detect language'));
  const label = (l) => (l.native && l.native !== l.name ? `${l.name} — ${l.native}` : l.name);
  const common = h('optgroup', { label: 'Most used' }, S.languages.filter((l) => l.common).map((l) => h('option', { value: l.code }, label(l))));
  const rest = h('optgroup', { label: 'All languages' }, S.languages.filter((l) => !l.common).map((l) => h('option', { value: l.code }, label(l))));
  select.append(common, rest);
  return select;
}

function render() {
  el.from = picker(true);
  el.to = picker(false);
  el.from.value = S.from; if (el.from.value !== S.from) { S.from = 'auto'; el.from.value = 'auto'; }
  el.to.value = S.to; if (el.to.value !== S.to) { S.to = 'es'; el.to.value = 'es'; }
  el.from.addEventListener('change', () => { S.from = el.from.value; remember(); });
  el.to.addEventListener('change', () => { S.to = el.to.value; remember(); });

  el.text = h('textarea', { placeholder: 'Type or paste something to translate', dir: 'auto', rows: '3', 'aria-label': 'Text to translate', autocapitalize: 'sentences' });
  el.count = h('span', { class: 'count' });
  el.mic = iconButton('mic', 'Speak', toggleListening);
  el.photo = h('input', { type: 'file', accept: 'image/*', capture: 'environment', hidden: true, onchange: () => { const file = el.photo.files[0]; el.photo.value = ''; if (file) translatePhoto(file); } });
  el.go = h('button', { class: 'btn primary block tr-go', type: 'button', onclick: () => translate() }, 'Translate');
  el.out = h('div');
  el.history = h('div', { class: 'tr-history' });

  const grow = () => { el.text.style.height = 'auto'; el.text.style.height = `${Math.min(el.text.scrollHeight + 2, window.innerHeight * 0.46)}px`; };
  const count = () => {
    const n = el.text.value.length;
    el.count.textContent = n > S.maxChars * 0.8 ? `${n.toLocaleString()} / ${S.maxChars.toLocaleString()}` : '';
    el.count.classList.toggle('over', n > S.maxChars);
  };
  el.text.addEventListener('input', () => { grow(); count(); });
  el.text.addEventListener('keydown', (event) => { if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) { event.preventDefault(); translate(); } });
  el.grow = () => { grow(); count(); };

  clear(app,
    h('div', { class: 'topbar' }, h('a', { class: 'icon-btn', href: homeHref(), title: 'Back', 'aria-label': 'Back' }, icon('back')),
      h('h1', {}, 'Translate', h('span', { class: 'sub' }, 'Type, speak or photograph it. Works with no internet.'))),
    h('div', { class: 'tr-langs' }, el.from, iconButton('swap', 'Swap the two languages', swap), el.to),
    h('div', { class: 'card tr-box' }, el.text,
      h('div', { class: 'tr-tools' }, el.mic, iconButton('camera', 'Translate the words in a photo', () => el.photo.click()),
        iconButton('x', 'Clear', () => { el.text.value = ''; el.grow(); clear(el.out); S.last = null; el.text.focus(); }), el.count, el.photo)),
    el.go, el.out, el.history);
  if (!canListen()) el.mic.hidden = true;
  loadHistory();
}

// ── translating ────────────────────────────────────────────────────────────
function resultCard(meta, picture = null) {
  const text = h('div', { class: 'text waiting', dir: 'auto', 'aria-live': 'polite' }, 'Translating…');
  const label = h('div', { class: 'label' }, meta);
  const speak = iconButton('speaker', 'Read it aloud', () => speakResult(speak));
  const tools = h('div', { class: 'tr-tools' }, speak,
    iconButton('copy', 'Copy', async () => { try { await navigator.clipboard.writeText(S.last ? S.last.text : ''); toast('Copied'); } catch (_) { toast('Could not copy', { bad: true }); } }),
    h('button', { class: 'btn sm', type: 'button', title: 'Swap the languages and answer', onclick: reply }, icon('swap'), 'Reply'));
  tools.hidden = true;
  clear(el.out, h('div', { class: 'card tr-out' }, picture ? h('img', { class: 'tr-photo', src: picture, alt: 'The photo being translated' }) : null, label, text, tools));
  return { text, label, tools };
}

function showResult(card, result, fromPhoto = false) {
  S.last = result;
  card.text.classList.remove('waiting');
  card.text.textContent = result.text;
  card.text.setAttribute('lang', result.target);
  const from = result.source_name ? `${result.source_name}${result.detected ? ' (detected)' : ''}` : (fromPhoto ? 'From the photo' : 'Language not recognised');
  clear(card.label, `${from} → ${result.target_name}`, result.same_language ? ' · already in that language' : '');
  card.tools.hidden = false;
}

async function translate() {
  const text = el.text.value;
  if (S.busy || !text.trim()) { if (!text.trim()) el.text.focus(); return; }
  if (text.length > S.maxChars) { toast(`That is over ${S.maxChars.toLocaleString()} characters. Translate it in pieces.`, { bad: true }); return; }
  stopSpeaking();
  S.busy = true; el.go.disabled = true;
  const card = resultCard(`${S.from === 'auto' ? 'Detecting language' : nameOf(S.from)} → ${nameOf(S.to)}`);
  try {
    const response = await fetch('/api/translate/stream', { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text, to: S.to, from: S.from }) });
    if (response.status === 401) { location.href = '/login'; return; }
    if (!response.ok || !response.body) {
      let detail = ''; try { detail = (await response.json()).detail || ''; } catch (_) { /* no body */ }
      throw new Error(typeof detail === 'string' && detail ? detail : 'The translator is not answering right now.');
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let pending = ''; let soFar = ''; let done = null;
    const take = (line) => {
      if (!line.trim()) return;
      const item = JSON.parse(line);
      if (item.error) throw new Error(item.error);
      if (item.done) { done = item; return; }
      soFar += item.text;
      card.text.classList.remove('waiting');
      card.text.textContent = soFar;
      if (item.of > 1) clear(card.label, `Translating… part ${item.part} of ${item.of}`);
    };
    for (;;) {
      const { value, done: finished } = await reader.read();
      pending += decoder.decode(value || new Uint8Array(), { stream: !finished });
      const lines = pending.split('\n');
      pending = lines.pop();
      lines.forEach(take);
      if (finished) { take(pending); break; }
    }
    if (!done) throw new Error('The translation was cut short. Try again.');
    showResult(card, done);
    loadHistory();
  } catch (error) {
    clear(el.out, h('div', { class: 'card empty' }, h('b', {}, 'That could not be translated'), error.message || String(error)));
  } finally { S.busy = false; el.go.disabled = false; }
}

async function translatePhoto(file) {
  if (S.busy) return;
  stopSpeaking();
  S.busy = true; el.go.disabled = true;
  const picture = URL.createObjectURL(file);
  const card = resultCard(`Reading the photo → ${nameOf(S.to)}`, picture);
  try {
    const form = new FormData();
    form.append('file', await shrink(file));
    form.append('to', S.to);
    form.append('from', S.from);
    showResult(card, await api('/api/translate/photo', { method: 'POST', form }), true);
    loadHistory();
  } catch (error) {
    clear(el.out, h('div', { class: 'card empty' }, h('b', {}, 'That photo could not be translated'), error.message || String(error)));
  } finally { S.busy = false; el.go.disabled = false; }
}

// A phone photo is several megabytes; the words are just as readable at a
// quarter of the size, and it is sent and read much faster.
async function shrink(file, longest = 1600) {
  try {
    const bitmap = await createImageBitmap(file);
    const scale = Math.min(1, longest / Math.max(bitmap.width, bitmap.height));
    if (scale === 1 && file.size < 1500000) return file;
    const canvas = document.createElement('canvas');
    canvas.width = Math.round(bitmap.width * scale); canvas.height = Math.round(bitmap.height * scale);
    canvas.getContext('2d').drawImage(bitmap, 0, 0, canvas.width, canvas.height);
    const blob = await new Promise((resolve) => canvas.toBlob(resolve, 'image/jpeg', 0.88));
    return blob ? new File([blob], 'photo.jpg', { type: 'image/jpeg' }) : file;
  } catch (_) { return file; }
}

function swap() {
  const from = S.from === 'auto' ? (S.last && S.last.source) || '' : S.from;
  if (!from) { toast('Translate something first, or choose the language to swap with.'); return; }
  S.from = S.to; S.to = from;
  el.from.value = S.from; el.to.value = S.to;
  if (el.to.value !== S.to) { S.to = from.split('-')[0]; el.to.value = S.to; }
  remember();
  if (S.last && S.last.text) { el.text.value = S.last.text; el.grow(); clear(el.out); S.last = null; }
}
function reply() { swap(); el.text.value = ''; el.grow(); el.text.focus(); }

// ── hearing it ─────────────────────────────────────────────────────────────
function stopSpeaking() {
  try { window.speechSynthesis && window.speechSynthesis.cancel(); } catch (_) { /* not there */ }
  if (S.speaking && S.speaking.audio) { S.speaking.audio.pause(); }
  if (S.speaking && S.speaking.button) clear(S.speaking.button, icon('speaker'));
  S.speaking = null;
}
async function speakResult(button) {
  if (S.speaking) { stopSpeaking(); return; }
  if (!S.last || !S.last.text) return;
  const text = S.last.text.slice(0, 2500);
  const lang = S.last.target;
  S.speaking = { button };
  clear(button, icon('stop'));
  const finish = () => { if (S.speaking && S.speaking.button === button) { clear(button, icon('speaker')); S.speaking = null; } };
  // The phone's own voice for that language, when it has one: instant, and with the right accent.
  const synth = window.speechSynthesis;
  const voices = synth ? synth.getVoices() : [];
  const base = lang.split('-')[0].toLowerCase();
  const voice = voices.find((v) => v.lang.toLowerCase() === lang.toLowerCase()) || voices.find((v) => v.lang.toLowerCase().split(/[-_]/)[0] === base);
  if (synth && voice) {
    const said = new SpeechSynthesisUtterance(text);
    said.voice = voice; said.lang = voice.lang; said.onend = finish; said.onerror = finish;
    synth.speak(said);
    return;
  }
  try {
    const response = await fetch('/api/tts/synthesize', { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text }) });
    if (!response.ok) throw new Error('No voice is set up on the server.');
    const audio = new Audio(URL.createObjectURL(await response.blob()));
    if (!S.speaking || S.speaking.button !== button) return;
    S.speaking.audio = audio;
    audio.onended = finish; audio.onerror = finish;
    await audio.play();
  } catch (error) { finish(); toast(error.message || 'Could not read it aloud', { bad: true }); }
}

// ── speaking to it ─────────────────────────────────────────────────────────
const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
const canListen = () => !!Recognition || (S.serverEars && !!navigator.mediaDevices && !!window.MediaRecorder);

function stopListening() {
  const live = S.listening; S.listening = null;
  el.mic.classList.remove('live');
  if (!live) return;
  try { if (live.recognition) live.recognition.stop(); if (live.recorder && live.recorder.state !== 'inactive') live.recorder.stop(); } catch (_) { /* already stopped */ }
}
async function toggleListening() {
  if (S.listening) { stopListening(); return; }
  const lang = S.from === 'auto' ? (navigator.language || 'en-US') : S.from;
  if (Recognition) {
    const recognition = new Recognition();
    recognition.lang = lang; recognition.interimResults = true; recognition.continuous = false;
    const before = el.text.value ? `${el.text.value.trim()} ` : '';
    recognition.onresult = (event) => { el.text.value = before + Array.from(event.results).map((r) => r[0].transcript).join(' '); el.grow(); };
    recognition.onerror = (event) => { if (event.error !== 'aborted' && event.error !== 'no-speech') toast('The microphone could not be used.', { bad: true }); };
    recognition.onend = () => { const mine = S.listening && S.listening.recognition === recognition; stopListening(); if (mine && el.text.value.trim()) translate(); };
    S.listening = { recognition }; el.mic.classList.add('live');
    try { recognition.start(); } catch (_) { stopListening(); }
    return;
  }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    const recorder = new MediaRecorder(stream);
    const chunks = [];
    recorder.ondataavailable = (event) => { if (event.data.size) chunks.push(event.data); };
    recorder.onstop = async () => {
      stream.getTracks().forEach((track) => track.stop());
      el.mic.classList.remove('live');
      if (!chunks.length) return;
      try {
        const form = new FormData();
        form.append('file', new Blob(chunks, { type: recorder.mimeType || 'audio/webm' }), 'speech.webm');
        const heard = await api('/api/stt/transcribe', { method: 'POST', form });
        if (heard.text) { el.text.value = `${el.text.value ? `${el.text.value.trim()} ` : ''}${heard.text.trim()}`; el.grow(); translate(); }
      } catch (error) { oops(error); }
    };
    S.listening = { recorder }; el.mic.classList.add('live');
    recorder.start();
    setTimeout(() => { if (S.listening && S.listening.recorder === recorder) stopListening(); }, 30000);
  } catch (_) { toast('The microphone could not be used.', { bad: true }); }
}

// ── what was translated lately ─────────────────────────────────────────────
async function loadHistory() {
  let data;
  try { data = await api('/api/translate/history?limit=20'); } catch (_) { return; }
  if (!data.items.length) { clear(el.history); return; }
  const row = (item) => h('div', { class: 'row tap', onclick: () => {
    el.text.value = item.text || ''; el.grow();
    if (item.source) { S.from = item.source; el.from.value = S.from; if (el.from.value !== S.from) { S.from = 'auto'; el.from.value = 'auto'; } }
    S.to = item.target; el.to.value = S.to; remember();
    const card = resultCard('');
    showResult(card, { text: item.result, source: item.source, source_name: item.source_name, target: item.target, target_name: item.target_name, detected: '' }, item.kind === 'photo');
    window.scrollTo({ top: 0, behavior: 'smooth' });
  } },
  h('div', { class: 'main' }, h('div', { class: 'title clamp2', dir: 'auto' }, item.result),
    item.text ? h('div', { class: 'said clamp2', dir: 'auto' }, item.text) : null,
    h('div', { class: 'meta' }, h('span', {}, `${item.source_name || '?'} → ${item.target_name}`), item.kind === 'photo' ? h('span', { class: 'chip' }, 'Photo') : null, h('span', {}, ago(item.at)))),
  iconButton('x', 'Forget this one', async (event) => { event.stopPropagation(); try { await api(`/api/translate/history/${item.id}`, { method: 'DELETE' }); loadHistory(); } catch (error) { oops(error); } }));
  clear(el.history,
    h('div', { class: 'section-title' }, h('h2', {}, 'Lately'),
      h('button', { class: 'btn ghost sm', type: 'button', onclick: async () => {
        if (!(await confirmSheet('Forget everything you have translated here?', { title: 'Clear the list', ok: 'Forget it all', danger: true }))) return;
        try { await api('/api/translate/history', { method: 'DELETE' }); loadHistory(); } catch (error) { oops(error); }
      } }, 'Clear')),
    h('div', { class: 'card flush rows' }, data.items.map(row)));
}

// ── start ──────────────────────────────────────────────────────────────────
(async () => {
  try { S.me = await api('/api/me'); } catch (_) { S.me = null; }
  try {
    const data = await api('/api/translate/languages');
    S.languages = data.languages; S.maxChars = data.max_chars || S.maxChars;
  } catch (error) {
    clear(app, h('div', { class: 'card empty' }, h('b', {}, 'The translator could not be opened'), error.message));
    return;
  }
  try { const ears = await fetch('/api/stt/stats', { credentials: 'same-origin' }); if (ears.ok) { const stats = await ears.json(); S.serverEars = !!(stats.available && stats.provider !== 'browser'); } } catch (_) { /* no server ears */ }
  recall();
  render();
  const asked = new URLSearchParams(location.search).get('text');
  if (asked) { el.text.value = asked; el.grow(); translate(); }
  if (window.speechSynthesis) window.speechSynthesis.getVoices();      // some browsers load their voices only once asked
})();
