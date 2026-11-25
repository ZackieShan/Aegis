// Reading an answer aloud, on either screen.
//
// Two kinds of voice read here. A device's own voice (the phone's, the browser's) starts at
// once and needs nothing from the server. A voice on the server — one recorded in the Studio,
// so an answer can be read in a person's own voice — has to be made first, and a cloned voice
// is slow: measured on the home server, a second of speech takes about two to make.
//
// So an answer is not sent off whole. It is cut into pieces of a sentence or two; the pieces are
// made in order and played back to back; and the reading starts when enough is ready that it
// will not run dry before the end (at once, for a voice that keeps up with itself). While it
// prepares, the screen is told how far along it is, so the wait is shown and not just endured.
//
//   reading()                 what this person can be read to in, and what they chose
//   chooseReading({voice, auto})
//   read(text, {onState})     read it; returns a reader: { stop() }
//   open({onState})           the same for an answer still being written: reader.feed(textSoFar),
//                             reader.finish(wholeText)
//   stopAll(), isReading(), unlock()   (unlock from a tap lets sound start later without another)
//
// onState gets { phase, ready, total, startsIn, kind }:
//   phase   'preparing' → 'playing' ⇄ 'waiting' → 'done' | 'stopped' | 'failed'
//   kind    'device' or 'server'

const CHARS_PER_SECOND = 16;      // English read aloud, as measured with the cloned voice
const START_CAP_MS = 40000;       // do not wait much longer than this for a first sound, whatever the pace
const START_GRACE_S = 8;          // …unless an unbroken reading is this close: then waiting for it is the better trade
const LONG_SENTENCE = 160;        // a sentence longer than this is cut at a comma near its middle
// A piece is about a sentence. Small pieces are what let the reading start on time: what is ready
// is counted in whole pieces, and with big ones the count jumps past the moment it could have begun.
const FIRST_PIECE = 120;          // a short first piece, so a voice that keeps up starts quickly
const PIECE = 170;
// a twentieth of a second of silence: played on a tap, it lets later sound start by itself
// A twentieth of a second of silence (a WAV, as base64). Playing it inside a tap is what lets a phone play the
// real sound later without another tap.
const SILENCE = 'UklGRrQBAABXQVZFZm10IBAAAAABAAEAQB8AAEAfAAABAAgAZGF0YZABAACAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICA';
let silenceUrl = '';

/** The silent clip as a blob: address. The policy this app sends lets sound come from the app itself and from
 *  blob: addresses only, so the same clip as a data: address is refused and unlocks nothing. */
function silence() {
  if (!silenceUrl) {
    const raw = atob(SILENCE);
    const bytes = new Uint8Array(raw.length);
    for (let i = 0; i < raw.length; i += 1) bytes[i] = raw.charCodeAt(i);
    silenceUrl = URL.createObjectURL(new Blob([bytes], { type: 'audio/wav' }));
  }
  return silenceUrl;
}

let chosen = null;                // { at, data } — what the server last said about this person's reading
let current = null;               // the reader that is reading now
let player = null;                // one <audio> for every piece: a sound a tap allowed stays allowed
const made = new Map();           // "voice|text" → { url, seconds } for this page's lifetime

/** Text as it should be said: no markdown marks, no code, no link addresses, no table rules. */
export function sayable(text) {
  return String(text || '')
    .replace(/<think(?:ing)?(?:\s[^>]*)?>[\s\S]*?<\/think(?:ing)?>/gi, ' ')
    .replace(/<think(?:ing)?(?:\s[^>]*)?>[\s\S]*$/i, ' ')         // thinking that is still going on is not read out
    .replace(/```[\s\S]*?```/g, ' ')
    .replace(/```[\s\S]*$/g, ' ')                       // a code block still being written
    .replace(/!\[[^\]]*\]\([^)]*\)/g, ' ')
    .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
    .replace(/https?:\/\/\S*?(?=[.,;:!?)\]]*(?:\s|$))/g, ' ')    // a bare address is not read out; the full stop after it stays
    .replace(/\(\s*\)|\[\s*\]/g, ' ')                     // brackets left empty by an address taken out
    .replace(/^[\s|:-]{3,}$/gm, ' ')                    // the rule under a table's heading
    .replace(/^\s*[-*•]\s+/gm, '')
    .replace(/^#+\s*/gm, '')
    .replace(/\s*\n\s*/g, '. ')                         // a line ends a thought when it is read out
    .replace(/[*_`|>]+/g, ' ')
    .replace(/\s+([.,!?;:])/g, '$1')                    // "dry ." where a mark was taken out
    .replace(/([.!?…:;])(?:\s*\.)+/g, '$1')             // "…end.. Next" from the line breaks above
    .replace(/\s+/g, ' ')
    .replace(/^[.\s]+/, '')
    .trim();
}

/** Where sentences end in `text`: the index just past each closing mark. "1." and "Dr." do not end one. */
function sentenceEnds(text) {
  const ends = [];
  const mark = /[.!?…]+["')\]]?(?=\s|$)/g;
  let found;
  while ((found = mark.exec(text)) !== null) {
    const upTo = text.slice(0, found.index);
    const word = (upTo.match(/(\S+)$/) || ['', ''])[1];
    if (found[0][0] === '.' && (/^\d{1,3}$/.test(word) || /^(?:[A-Za-z]|Mr|Mrs|Ms|Dr|St|vs|etc|e\.g|i\.e|No|approx)$/i.test(word))) continue;
    ends.push(found.index + found[0].length);
  }
  return ends;
}

/**
 * Cut what is to be said into pieces: a short one first, then a sentence or two at a time.
 * With `whole` false, words after the last full sentence are held back (the answer is still being written);
 * returns { pieces, used } where `used` is how many characters of `said` the pieces cover.
 */
export function pieces(said, { whole = true, first = FIRST_PIECE, size = PIECE, started = false } = {}) {
  const text = String(said || '');
  const out = [];
  let from = 0;
  let pending = '';
  const push = () => { const piece = pending.trim(); if (piece) out.push(piece); pending = ''; };
  const limit = () => (out.length === 0 && !started ? first : size);
  const take = (sentence) => {
    let rest = sentence;
    // A long sentence is two pieces, cut at the comma (or semicolon, or dash) nearest its middle.
    // The opening sentence is cut sooner: a short first piece is what lets a quick voice start quickly.
    if (rest.length > (out.length === 0 && !pending && !started ? first : LONG_SENTENCE) && rest.length <= size * 2) {
      const middle = rest.length / 2;
      let best = -1;
      for (const found of rest.matchAll(/[,;:—–]\s/g)) { if (best < 0 || Math.abs(found.index - middle) < Math.abs(best - middle)) best = found.index; }
      if (best > 30 && best < rest.length - 30) {
        if (pending) push();
        out.push(rest.slice(0, best + 1).trim());
        rest = rest.slice(best + 1);
      }
    }
    while (rest.length > size) {                              // one endless sentence: break at a comma or a space
      let cut = rest.lastIndexOf(', ', size);
      if (cut < size * 0.5) cut = rest.lastIndexOf(' ', size);
      if (cut < size * 0.5) cut = size;
      if (pending) push();
      out.push(rest.slice(0, cut + 1).trim());
      rest = rest.slice(cut + 1);
    }
    if (pending && pending.length + rest.length + 1 > limit()) push();
    pending = pending ? `${pending} ${rest.trim()}` : rest.trim();
    if (pending.length >= limit() * 0.6) push();
  };
  for (const end of sentenceEnds(text)) {
    take(text.slice(from, end));
    from = end;
  }
  let used = from;
  if (whole && from < text.length) { take(text.slice(from)); used = text.length; }
  if (pending) { if (whole) push(); else { used -= pending.length; pending = ''; } }
  // A last scrap ("Done!") rides with the piece before it rather than being made on its own.
  if (whole && out.length > 1 && out[out.length - 1].length < 25 && out[out.length - 2].length + out[out.length - 1].length < size + 30) {
    const scrap = out.pop();
    out[out.length - 1] = `${out[out.length - 1]} ${scrap}`;
  }
  return { pieces: out, used: Math.max(0, used) };
}

/**
 * How much speech must be ready for the rest to arrive in time, piece by piece.
 * A piece can be played only once it is wholly made, so the test is not "is the total enough" but, for each
 * piece still to come: will it be finished by the time the reading reaches it? `pieces` are the lengths in
 * seconds of the pieces not yet made, in order; `progress` is how long the first of them has been in the making.
 */
export function needed({ pace, pieces: lengths = [], progress = 0 }) {
  const speed = pace > 0 ? pace : 0.5;
  let making = -Math.max(0, progress);       // seconds of work from now until piece k is made
  let before = 0;                            // seconds of speech in the pieces ahead of it, still to be made
  let need = 0;
  for (const length of lengths) {
    making += length / speed;
    need = Math.max(need, making - before);
    before += length;
  }
  return Math.max(0, need);
}

/**
 * Start now, or keep preparing? `ready` is the seconds of speech made and not yet played; `pieces` the lengths
 * of those still to make (or `rest`, their total, when the lengths are not known). Reading starts when what is
 * ready will last until each later piece is made — or when nothing is left to make, or when the wait has reached
 * its cap and an unbroken reading is not just a few seconds off.
 */
export function plan({ pace, ready, rest = 0, pieces: lengths = null, progress = 0, waitedMs = 0, capMs = START_CAP_MS, more = false }) {
  const list = lengths || (rest > 0 ? [rest] : []);
  if (ready <= 0) return 'wait';
  if (!list.length && !more) return 'start';
  const speed = pace > 0 ? pace : 0.5;
  if (ready >= needed({ pace: speed, pieces: list, progress })) return 'start';
  if (waitedMs < capMs) return 'wait';
  // Past the cap. A reading that would start unbroken within a few seconds is worth those seconds;
  // one that is further off starts now and pauses where it must.
  if (waitedMs >= capMs + START_GRACE_S * 1000) return 'start';
  return startsIn({ pace: speed, ready, pieces: list, progress, capMs: Infinity }) <= START_GRACE_S ? 'wait' : 'start';
}

/**
 * How long until the reading starts, in seconds, as far as can be told. Every second of waiting is a second
 * less of making ahead of each piece, so the wait is simply what is needed less what is ready.
 */
export function startsIn({ pace, ready, rest = 0, pieces: lengths = null, progress = 0, waitedMs = 0, capMs = START_CAP_MS }) {
  const speed = pace > 0 ? pace : 0.5;
  const list = lengths || (rest > 0 ? [rest] : []);
  const wait = Math.max(0, needed({ pace: speed, pieces: list, progress }) - Math.max(0, ready));
  const untilCap = (capMs - waitedMs) / 1000;
  if (wait <= START_GRACE_S) return wait;                       // close enough that the cap gives way to it
  return Math.max(0, Math.min(wait, untilCap));
}

// ── what this person is read to in ─────────────────────────────────────────

/** { voices, voice, auto, reads_in } from the server; kept for a minute. */
export async function reading(fresh = false) {
  if (!fresh && chosen && Date.now() - chosen.at < 60000) return chosen.data;
  try {
    const answer = await fetch('/api/tts/reading', { credentials: 'same-origin' });
    if (!answer.ok) throw new Error(String(answer.status));
    chosen = { at: Date.now(), data: await answer.json() };
  } catch (_) {
    chosen = { at: Date.now(), data: { voices: [{ id: 'device', label: "This device's own voice", kind: 'device' }], voice: '', auto: false, reads_in: 'device' } };
  }
  return chosen.data;
}

/** Save the choice ({ voice } and/or { auto }); returns what the server now says. Throws with the server's words when refused. */
export async function chooseReading(patch) {
  const answer = await fetch('/api/tts/reading', { method: 'PUT', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(patch) });
  const body = await answer.json().catch(() => ({}));
  if (!answer.ok) throw new Error((body && (body.detail && (body.detail.message || body.detail))) || 'That could not be saved.');
  chosen = null;
  made.clear();
  return reading(true);
}

let warmedAt = 0;

/**
 * The answer on its way will be read aloud: have the voice loaded by the time its first sentence is written.
 * A recorded voice is put away on the home server after a few idle minutes, and getting it out again takes five
 * to ten seconds, which would otherwise be added to the wait for the first sound. Call it as the question is
 * sent. It does nothing for a device's own voice, and the server does nothing if the voice spoke a moment ago.
 */
export async function warm() {
  if (Date.now() - warmedAt < 60000) return false;
  warmedAt = Date.now();
  try {
    const mine = await reading();
    if (!mine || !mine.reads_in || mine.reads_in === 'device') return false;
    const answer = await fetch('/api/tts/warm', { method: 'POST', credentials: 'same-origin' });
    return answer.ok;
  } catch (_) { return false; }
}

// ── a device's own voice ───────────────────────────────────────────────────

const synth = () => (typeof window !== 'undefined' && 'speechSynthesis' in window ? window.speechSynthesis : null);

async function deviceVoice(lang) {
  const engine = synth();
  if (!engine) return null;
  let voices = engine.getVoices();
  if (!voices.length) {                                  // some browsers fill the list a moment after the page loads
    await new Promise((resolve) => {
      const timer = setTimeout(resolve, 700);
      engine.addEventListener('voiceschanged', () => { clearTimeout(timer); resolve(); }, { once: true });
    });
    voices = engine.getVoices();
  }
  if (!voices.length) return null;
  const want = String(lang || navigator.language || 'en-US').toLowerCase();
  const base = want.split(/[-_]/)[0];
  return voices.find((v) => v.lang.toLowerCase() === want) || voices.find((v) => v.lang.toLowerCase().split(/[-_]/)[0] === base && v.default)
    || voices.find((v) => v.lang.toLowerCase().split(/[-_]/)[0] === base) || voices.find((v) => v.default) || voices[0];
}

/** Whether this device has a voice of its own at all. */
export async function deviceCanSpeak(lang) { return !!(await deviceVoice(lang)); }

// ── the reader ─────────────────────────────────────────────────────────────

function theAudio() {
  if (!player) { player = new Audio(); player.preload = 'auto'; }
  return player;
}

/** Call from a tap (a button, Send): sound may then start later without another tap, as phones require. */
export function unlock() {
  try {
    // A reading that has already sounded has been allowed to; and its player must not be handed another clip,
    // whose ending would be taken for the end of the piece it is on.
    if (current && (current.started || current.phase === 'playing')) return;
    const audio = theAudio();
    audio.src = silence();
    const playing = audio.play();
    if (playing && playing.catch) playing.catch(() => {});
  } catch (_) { /* no sound on this device */ }
}

export const isReading = () => !!current;

export function stopAll() { if (current) current.stop(); }

class Reader {
  constructor({ voice = '', lang = '', onState = () => {}, kind = '' } = {}) {
    this.asked = voice;              // a preview of this voice; '' = the person's own choice
    this.lang = lang;
    this.onState = onState;
    this.kind = kind;                // 'device' | 'server', settled on the first piece
    this.queue = [];                 // { text, state: 'todo'|'making'|'ready'|'failed', url, seconds }
    this.at = 0;                     // the piece playing, or to be played next
    this.phase = 'preparing';
    this.finished = false;           // no more text is coming
    this.consumed = 0;               // characters of the sayable text already cut into pieces
    this.pace = 0;                   // speech seconds per second of work, measured as pieces arrive
    this.known = 0;                  // what this voice has managed before: all there is to go by until its first piece arrives
    this.began = Date.now();
    this.abort = new AbortController();
    this.working = false;
    this.started = false;
    this.timer = null;
  }

  /** The answer so far (it is still being written): whole sentences that are new are queued. */
  feed(textSoFar) {
    if (this.phase === 'stopped' || this.phase === 'failed' || this.finished) return;
    const said = sayable(textSoFar);
    if (said.length <= this.consumed) return;
    const cut = pieces(said.slice(this.consumed), { whole: false, started: this.queue.length > 0 });
    this.consumed += cut.used;
    this.add(cut.pieces);
  }

  /** No more is coming; `wholeText` (when given) is the finished answer. */
  finish(wholeText) {
    if (this.phase === 'stopped' || this.phase === 'failed' || this.finished) return;
    if (wholeText !== undefined) {
      const said = sayable(wholeText);
      if (said.length > this.consumed) this.add(pieces(said.slice(this.consumed), { whole: true, started: this.queue.length > 0 }).pieces);
      this.consumed = said.length;
    }
    this.finished = true;
    if (!this.queue.length) { this.end('done'); return; }
    this.step();
  }

  add(list) {
    for (const text of list) if (text && text.trim()) this.queue.push({ text: text.trim(), state: 'todo', url: '', seconds: 0 });
    if (list.length) this.step();
  }

  tell() {
    const ready = this.queue.filter((p) => p.state === 'ready').length;
    const numbers = this.numbers();
    try {
      this.onState({ phase: this.phase, kind: this.kind || 'server', ready, total: this.queue.length, piece: this.at,
        startsIn: this.phase === 'preparing' && this.kind === 'server' ? Math.round(startsIn({ ...numbers, waitedMs: Date.now() - this.began })) : 0,
        pace: this.pace });
    } catch (_) { /* a screen that has gone away */ }
  }

  numbers() {
    let ready = 0; let chars = 0; let secs = 0;
    const toMake = [];
    this.queue.forEach((piece, index) => {
      if (piece.state === 'ready') { if (index >= this.at) ready += piece.seconds; chars += piece.text.length; secs += piece.seconds; } else if (piece.state !== 'failed') toMake.push(piece);
    });
    const rate = secs > 1 ? chars / secs : CHARS_PER_SECOND;            // how fast this voice speaks, from what it has said so far
    const making = toMake.find((piece) => piece.state === 'making');
    return { pace: this.pace || this.known, ready, pieces: toMake.map((piece) => piece.text.length / rate),
      progress: making && making.since ? (Date.now() - making.since) / 1000 : 0 };
  }

  async step() {
    if (this.phase === 'stopped' || this.phase === 'failed' || this.phase === 'done') return;
    if (!this.timer) this.timer = setInterval(() => { if (this.phase === 'preparing') { this.maybeStart(); this.tell(); } }, 1000);
    if (!this.kind) {
      // A preview of the device's own voice, or a person who chose it, never asks the server.
      if (this.asked === 'device') this.kind = 'device';
      else if (!this.asked) { const mine = await reading(); this.kind = mine.reads_in === 'device' ? 'device' : 'server'; this.learn(mine, mine.reads_in); }
      else { this.kind = 'server'; reading().then((mine) => this.learn(mine, this.asked)).catch(() => {}); }
      if (this.phase === 'stopped') return;
    }
    if (this.kind === 'device') { this.speakOnDevice(); return; }
    this.tell();
    this.make();
    this.maybeStart();
  }

  /** What this voice has managed before, so that the wait is told truthfully before its first piece arrives
   *  (without it, every reading began by saying "about 40 s", whatever the voice). */
  learn(mine, voice) {
    const found = ((mine && mine.voices) || []).find((v) => v.id === voice);
    this.known = found && Number(found.pace) > 0 ? Number(found.pace) : 0;
  }

  /** Make the pieces, one at a time, in order. */
  async make() {
    if (this.working) return;
    this.working = true;
    try {
      for (;;) {
        const piece = this.queue.find((p) => p.state === 'todo');
        if (!piece || this.phase === 'stopped' || this.phase === 'failed') break;
        piece.state = 'making';
        piece.since = Date.now();
        const key = `${this.asked}|${piece.text}`;
        const kept = made.get(key);
        if (kept) { piece.url = kept.url; piece.seconds = kept.seconds; piece.state = 'ready'; this.afterPiece(); continue; }
        const began = Date.now();
        let answer;
        try {
          answer = await fetch('/api/tts/say', { method: 'POST', credentials: 'same-origin', signal: this.abort.signal,
            headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text: piece.text, voice: this.asked }) });
        } catch (error) {
          if (error && error.name === 'AbortError') return;
          answer = null;
        }
        if (this.phase === 'stopped') return;
        if (answer && answer.status === 409) { this.toDevice(); return; }           // nothing on the server for this person
        if (!answer || !answer.ok) {
          piece.state = 'failed';
          // Before anything has been heard, the device's own voice can still read it; halfway through, a change of voice would be worse than stopping.
          if (!this.started && await deviceCanSpeak(this.lang)) { this.toDevice(); return; }
          this.end('failed', answer && answer.status === 403 ? 'That voice is not yours to use.' : 'The voice could not be made just now.');
          return;
        }
        const blob = await answer.blob();
        if (this.phase === 'stopped') return;
        piece.url = URL.createObjectURL(blob);
        piece.seconds = Number(answer.headers.get('X-Speech-Seconds')) || piece.text.length / CHARS_PER_SECOND;
        const took = (Date.now() - began) / 1000;
        const fresh = Number(answer.headers.get('X-Took-Seconds')) >= 0.5;
        // The first piece after the engine has been idle includes the seconds it took to load: it is not
        // allowed to make the voice look much slower than it has been measured to be.
        const known = Number(answer.headers.get('X-Pace')) || 0;
        if (fresh && took > 0.3) { const now = piece.seconds / took; this.pace = this.pace ? this.pace * 0.6 + now * 0.4 : Math.max(now, known * 0.8); }
        else if (!this.pace) this.pace = known;
        piece.state = 'ready';
        made.set(key, { url: piece.url, seconds: piece.seconds });
        if (made.size > 300) made.delete(made.keys().next().value);
        this.afterPiece();
      }
    } finally { this.working = false; }
  }

  afterPiece() {
    if (this.phase === 'waiting') this.playNext();
    else this.maybeStart();
    this.tell();
  }

  maybeStart() {
    if (this.started || this.kind !== 'server' || this.phase !== 'preparing') return;
    const first = this.queue[0];
    if (!first || first.state !== 'ready') return;
    const numbers = this.numbers();
    const allMade = this.finished && this.queue.every((p) => p.state === 'ready' || p.state === 'failed');
    if (allMade || plan({ ...numbers, waitedMs: Date.now() - this.began, more: !this.finished }) === 'start') {
      this.started = true;
      this.playNext();
    }
  }

  playNext() {
    if (this.phase === 'stopped' || this.phase === 'failed') return;
    const piece = this.queue[this.at];
    if (!piece) {
      if (this.finished) this.end('done'); else { this.phase = 'waiting'; this.tell(); }
      return;
    }
    if (piece.state === 'failed') { this.at += 1; this.playNext(); return; }
    if (piece.state !== 'ready') { this.phase = 'waiting'; this.tell(); return; }
    const audio = theAudio();
    audio.onended = () => { if (current === this) { this.at += 1; this.playNext(); } };
    audio.onerror = () => { if (current === this) { this.at += 1; this.playNext(); } };
    audio.src = piece.url;
    this.phase = 'playing';
    this.tell();
    const playing = audio.play();
    if (playing && playing.catch) playing.catch(() => { if (current === this) this.end('failed', 'Tap the speaker to hear it: this device needs a tap before it plays sound.'); });
  }

  toDevice() {
    this.kind = 'device';
    this.abort.abort();
    this.queue.forEach((p) => { if (p.state !== 'ready') p.state = 'todo'; });
    this.speakOnDevice();
  }

  async speakOnDevice() {
    if (this.speaking) return;
    this.speaking = true;
    const voice = await deviceVoice(this.lang);
    if (this.phase === 'stopped') return;
    const engine = synth();
    if (!engine || !voice) { this.end('failed', 'There is no voice on this device to read it with.'); return; }
    this.started = true;
    const next = () => {
      if (current !== this || this.phase === 'stopped') return;
      const piece = this.queue[this.at];
      if (!piece) {
        if (this.finished) { this.end('done'); return; }
        this.phase = 'waiting'; this.tell();
        this.speaking = false;                      // more text may still come: step() calls back in
        return;
      }
      const said = new SpeechSynthesisUtterance(piece.text);
      said.voice = voice; said.lang = voice.lang;
      said.onend = () => { this.at += 1; next(); };
      said.onerror = (event) => { if (event && (event.error === 'interrupted' || event.error === 'canceled')) return; this.at += 1; next(); };
      this.phase = 'playing';
      this.tell();
      engine.speak(said);
    };
    next();
  }

  end(phase, message = '') {
    if (this.phase === 'done' || this.phase === 'stopped' || this.phase === 'failed') return;
    this.phase = phase;
    this.message = message;
    if (this.timer) { clearInterval(this.timer); this.timer = null; }
    if (current === this) current = null;
    try { this.onState({ phase, kind: this.kind || 'server', ready: this.queue.filter((p) => p.state === 'ready').length, total: this.queue.length, piece: this.at, startsIn: 0, message }); } catch (_) { /* gone */ }
  }

  stop() {
    if (this.phase === 'done' || this.phase === 'stopped' || this.phase === 'failed') return;
    try { this.abort.abort(); } catch (_) { /* nothing in flight */ }
    if (this.kind === 'device') { try { const engine = synth(); if (engine) engine.cancel(); } catch (_) { /* gone */ } }
    else if (player) { try { player.onended = null; player.onerror = null; player.pause(); } catch (_) { /* gone */ } }
    this.end('stopped');
  }
}

/** Open a reader for an answer that is still being written. Whatever was reading stops. */
export function open(options = {}) {
  stopAll();
  const reader = new Reader(options);
  current = reader;
  return reader;
}

/** Read `text` aloud. Returns the reader ({ stop() }). */
export function read(text, options = {}) {
  const reader = open(options);
  const said = sayable(text).slice(0, 12000);
  if (!said) { reader.finished = true; reader.end('done'); return reader; }
  reader.consumed = said.length;
  reader.finished = true;
  reader.add(pieces(said, { whole: true }).pieces);
  return reader;
}

/** What a reader's state is called on a button or a line of text. */
export function describe(state) {
  if (!state) return '';
  if (state.phase === 'preparing') {
    if (state.kind === 'device') return 'Starting';
    const wait = state.startsIn > 1 ? `, about ${state.startsIn} s` : '';
    return state.total > 1 ? `Getting the voice ready (${state.ready} of ${state.total})${wait}` : `Getting the voice ready${wait}`;
  }
  if (state.phase === 'waiting') return 'Catching up';
  if (state.phase === 'playing') return 'Reading aloud';
  if (state.phase === 'failed') return state.message || 'It could not be read aloud.';
  return '';
}

export default { sayable, pieces, needed, plan, startsIn, reading, chooseReading, warm, deviceCanSpeak, unlock, isReading, stopAll, open, read, describe };
