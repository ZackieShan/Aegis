// Hearing and speaking for the lite pages.
//
// Hearing is the home server's own when it has ears (Whisper): what is said
// goes to the house's server and nowhere else, and it works with the internet
// down. A phone's built-in dictation sends the sound to its maker, so it is
// used only when the server cannot hear. Listening stops by itself when the
// person stops talking.
//
// Speaking is in the voice the person chose (static/js/readAloud.js, shared with the
// full app): the phone's own voice, which starts at once and needs no network, or a
// voice recorded in the Studio and made on the home server — their own, if they like.
// Words in another language (a translation) are said by the phone's voice for that
// language where it has one.
//
//   listen({ lang, onText, onDone, onState })   start or stop taking dictation
//   speak(text, { lang, onDone, onState })       read text aloud; returns a function that stops it
//   canListen()                                  whether there is any way to hear on this device
import { api, toast, oops } from './lite.js';
import readAloud from '../js/readAloud.js';

const Recognition = typeof window !== 'undefined' ? (window.SpeechRecognition || window.webkitSpeechRecognition) : null;
let serverEars = null;          // null: not asked yet
let live = null;                // { recognition | recorder, onState }
let saying = null;              // { audio?, done }

async function askServerEars() {
  if (serverEars !== null) return serverEars;
  try {
    const answer = await fetch('/api/stt/stats', { credentials: 'same-origin' });
    const stats = answer.ok ? await answer.json() : {};
    serverEars = !!(stats.available && stats.provider !== 'browser');
  } catch (_) { serverEars = false; }
  return serverEars;
}

/** Whether this device can take dictation at all (so a page can leave the microphone out when it cannot). */
export async function canListen() {
  if (canRecord() && await askServerEars()) return true;
  return !!Recognition;
}

const canRecord = () => !!(typeof navigator !== 'undefined' && navigator.mediaDevices && navigator.mediaDevices.getUserMedia && typeof window !== 'undefined' && window.MediaRecorder);

// When the talking stops, so does the listening: after speech has been heard,
// this much quiet ends it. Before any speech, it waits longer for a start.
const QUIET_AFTER_SPEECH_MS = 1600;
const QUIET_BEFORE_SPEECH_MS = 7000;

/** Calls `onQuiet` once the person has spoken and then gone quiet (or never spoke). Returns a function that stops watching. */
function watchForQuiet(stream, onQuiet) {
  const Context = window.AudioContext || window.webkitAudioContext;
  if (!Context) return () => {};
  let context;
  try { context = new Context(); } catch (_) { return () => {}; }
  const analyser = context.createAnalyser();
  analyser.fftSize = 1024;
  context.createMediaStreamSource(stream).connect(analyser);
  const samples = new Uint8Array(analyser.fftSize);
  const began = Date.now();
  let spoke = false; let quietSince = Date.now(); let floor = 0.01;
  const timer = setInterval(() => {
    analyser.getByteTimeDomainData(samples);
    let sum = 0;
    for (let i = 0; i < samples.length; i += 1) { const v = (samples[i] - 128) / 128; sum += v * v; }
    const level = Math.sqrt(sum / samples.length);
    if (!spoke && Date.now() - began < 400) floor = Math.max(floor, level * 1.5);      // the room's own noise, measured at the start
    const loud = level > Math.max(0.025, floor * 2);
    if (loud) { spoke = true; quietSince = Date.now(); }
    const quietFor = Date.now() - quietSince;
    if ((spoke && quietFor > QUIET_AFTER_SPEECH_MS) || (!spoke && quietFor > QUIET_BEFORE_SPEECH_MS)) onQuiet();
  }, 100);
  return () => { clearInterval(timer); try { context.close(); } catch (_) { /* closed */ } };
}

export const isListening = () => !!live;

export function stopListening() {
  const was = live;
  live = null;
  if (!was) return;
  try { if (was.recognition) was.recognition.stop(); if (was.recorder && was.recorder.state !== 'inactive') was.recorder.stop(); } catch (_) { /* already stopped */ }
  if (was.onState) was.onState(false);
}

/**
 * Start taking dictation, or stop if already listening.
 * onText(text, final) is called as words arrive (final = the person stopped speaking);
 * onDone(text) once, with everything heard; onState(listening) for the button.
 */
export async function listen({ lang = '', onText = () => {}, onDone = () => {}, onState = () => {} } = {}) {
  if (live) { stopListening(); return; }
  stopSpeaking();
  const language = lang || navigator.language || 'en-US';
  const serverHears = canRecord() && await askServerEars();
  if (!serverHears && Recognition) {
    const recognition = new Recognition();
    recognition.lang = language; recognition.interimResults = true; recognition.continuous = false;
    let heard = '';
    recognition.onresult = (event) => { heard = Array.from(event.results).map((r) => r[0].transcript).join(' ').trim(); onText(heard, false); };
    recognition.onerror = (event) => { if (event.error !== 'aborted' && event.error !== 'no-speech') toast('The microphone could not be used.', { bad: true }); };
    recognition.onend = () => { const mine = live && live.recognition === recognition; if (mine) { live = null; onState(false); onDone(heard); } };
    live = { recognition, onState };
    onState(true);
    try { recognition.start(); } catch (_) { stopListening(); }
    return;
  }
  if (!serverHears) { toast('This device cannot take dictation here.', { bad: true }); return; }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
    const recorder = new MediaRecorder(stream);
    const chunks = [];
    let stopWatching = () => {};
    recorder.ondataavailable = (event) => { if (event.data.size) chunks.push(event.data); };
    recorder.onstop = async () => {
      stopWatching();
      stream.getTracks().forEach((track) => track.stop());
      if (live && live.recorder === recorder) { live = null; }
      onState(false);
      if (!chunks.length) { onDone(''); return; }
      try {
        const form = new FormData();
        form.append('file', new Blob(chunks, { type: recorder.mimeType || 'audio/webm' }), 'speech.webm');
        const answer = await api('/api/stt/transcribe', { method: 'POST', form });
        const text = String(answer.text || '').trim();
        onText(text, true);
        onDone(text);
      } catch (error) { oops(error); onDone(''); }
    };
    live = { recorder, onState };
    onState(true);
    recorder.start();
    stopWatching = watchForQuiet(stream, () => { if (live && live.recorder === recorder) stopListening(); });
    setTimeout(() => { if (live && live.recorder === recorder) stopListening(); }, 30000);   // nobody dictates for longer; do not leave it open
  } catch (_) { toast('The microphone could not be used. Check that this page may use it.', { bad: true }); }
}

export const isSpeaking = () => !!saying;

export function stopSpeaking() {
  const was = saying;
  saying = null;
  readAloud.stopAll();
  if (was && was.done) was.done();
}

/** Text as it should be said: no markdown marks, no link addresses, no table bars. */
export const sayable = readAloud.sayable;

/**
 * Read text aloud, in the voice this person chose. Returns a function that stops it.
 * onDone runs once, however it ends; onState is told how it is going (a recorded voice takes a while to get ready).
 * `otherLanguage` marks words that are not in the house's language: the phone's voice for `lang` says them when it has one.
 */
export function speak(text, { lang = '', onDone = () => {}, onState = () => {}, otherLanguage = false } = {}) {
  stopSpeaking();
  let finished = false;
  const done = () => { if (finished) return; finished = true; if (saying && saying.done === done) saying = null; onDone(); };
  saying = { done };
  (async () => {
    // A cloned voice was recorded speaking one language; words in another belong to the phone's voice for it.
    const voice = otherLanguage && await readAloud.deviceCanSpeak(lang) ? 'device' : '';
    if (finished) return;
    readAloud.read(String(text || '').slice(0, 6000), { lang, voice, onState: (state) => {
      if (finished) return;
      try { onState(state); } catch (_) { /* a screen that moved on */ }
      if (state.phase === 'failed') toast(state.message || 'It could not be read aloud.', { bad: true });
      if (state.phase === 'done' || state.phase === 'stopped' || state.phase === 'failed') done();
    } });
  })();
  return stopSpeaking;
}
