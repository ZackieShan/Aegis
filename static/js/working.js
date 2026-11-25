// Signs of life while the assistant is at work.
//
// An answer from a model that runs at home takes seconds, sometimes a minute,
// and for much of that time there is nothing to print yet: the model is reading
// the conversation, then thinking, then waiting on a tool. A screen that shows
// nothing through those stretches looks broken. This module is the one place
// both screens (the full app and the family screen) get what they show then:
//
//   mark()            three bars that move — the same mark wherever work is going on
//   seconds(ms)       "6 s", "1 min 5 s" — how long this step has taken
//   lastThought(text) the last line or two of what the model is thinking, tidied
//                     for a glance (the whole of it is one tap away)
//   reading(event)    how far the model has read, from the server's "reading"
//                     event, as { percent, label } or null when it is not worth a bar
//   bar(percent)      a thin progress bar for it
//
// The styles travel with the module (one <style>, added once) so the two screens
// cannot drift apart. Colours come from the page: the mark is drawn in
// currentColor, the accent from --working-accent, else the page's own accent.

const STYLE_ID = 'working-style';

const CSS = `
.working-mark { display: inline-flex; align-items: flex-end; gap: 2px; width: 13px; height: 12px; flex: 0 0 auto; vertical-align: -1px; color: var(--working-accent, var(--accent, var(--red, currentColor))); }
.working-mark i { display: block; width: 3px; height: 100%; border-radius: 1.5px; background: currentColor; transform-origin: 50% 100%; animation: working-bar 1.05s ease-in-out infinite; }
.working-mark i:nth-child(2) { animation-delay: .16s; }
.working-mark i:nth-child(3) { animation-delay: .32s; }
@keyframes working-bar { 0%, 100% { transform: scaleY(.28); opacity: .55; } 45% { transform: scaleY(1); opacity: 1; } }
.working-label { background: linear-gradient(90deg, currentColor 0%, currentColor 38%, color-mix(in srgb, currentColor 35%, transparent) 50%, currentColor 62%, currentColor 100%); background-size: 220% 100%; -webkit-background-clip: text; background-clip: text; -webkit-text-fill-color: transparent; animation: working-shimmer 2.2s linear infinite; }
@keyframes working-shimmer { from { background-position: 110% 0; } to { background-position: -110% 0; } }
.working-secs { font-variant-numeric: tabular-nums; opacity: .6; font-size: .92em; }
.working-bar { display: block; height: 3px; border-radius: 2px; background: color-mix(in srgb, currentColor 16%, transparent); overflow: hidden; }
.working-bar > i { display: block; height: 100%; width: 0; border-radius: 2px; background: var(--working-accent, var(--accent, var(--red, currentColor))); transition: width .9s linear; }
.working-thought { display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 2; overflow: hidden; font-style: italic; opacity: .72; overflow-wrap: anywhere; }
@media (prefers-reduced-motion: reduce) {
  .working-mark i { animation: working-fade 1.8s ease-in-out infinite; transform: scaleY(.7); }
  .working-label { animation: none; -webkit-text-fill-color: currentColor; background: none; }
  .working-bar > i { transition: none; }
  @keyframes working-fade { 0%, 100% { opacity: .45; } 50% { opacity: 1; } }
}
`;

/** Add the styles to the page, once. Safe to call from anywhere, any number of times. */
export function ensureStyles(doc = (typeof document !== 'undefined' ? document : null)) {
  if (!doc || doc.getElementById(STYLE_ID)) return;
  const style = doc.createElement('style');
  style.id = STYLE_ID;
  style.textContent = CSS;
  (doc.head || doc.documentElement).appendChild(style);
}

/** The markup of the moving mark, for code that builds HTML as a string. */
export const MARK_HTML = '<span class="working-mark" aria-hidden="true"><i></i><i></i><i></i></span>';

/** The moving mark as an element. */
export function mark(doc = document) {
  ensureStyles(doc);
  const span = doc.createElement('span');
  span.className = 'working-mark';
  span.setAttribute('aria-hidden', 'true');
  span.append(doc.createElement('i'), doc.createElement('i'), doc.createElement('i'));
  return span;
}

/** How long, for a person: "6 s", "59 s", "1 min 5 s". Nothing under a second. */
export function seconds(ms) {
  const s = Math.floor(Math.max(0, Number(ms) || 0) / 1000);
  if (s < 1) return '';
  if (s < 60) return `${s} s`;
  const rest = s % 60;
  return rest ? `${Math.floor(s / 60)} min ${rest} s` : `${Math.floor(s / 60)} min`;
}

// What a model writes to itself is not written to be read: headings, bullets,
// code fences, tool arguments, markup left over from its own format.
const NOISE = /<\/?(?:think(?:ing)?|thought)[^>]*>|<\|[^|>]*\|>|<[a-z_]*\|>|```+|^\s*[#>*-]+\s*|\*\*|__|`/gim;

/**
 * The end of the model's thinking so far, as a line or two a person can take in at a glance:
 * the last whole sentences that fit in `limit` characters, without the markup.
 * '' when there is nothing worth showing yet.
 */
export function lastThought(text, limit = 180) {
  const flat = String(text || '').replace(NOISE, ' ').replace(/\s+/g, ' ').trim();
  if (flat.length < 12) return '';
  if (flat.length <= limit) return flat;
  const tail = flat.slice(-limit);
  // Start at a sentence where there is one in the first part of the tail; otherwise at a word.
  const sentence = tail.search(/[.!?…:;]\s+\S/);
  if (sentence >= 0 && sentence < limit * 0.6) return tail.slice(sentence + 1).trim();
  const word = tail.indexOf(' ');
  return '…' + (word >= 0 ? tail.slice(word) : tail).trim();
}

/**
 * How far the model has read, from a server "reading" event ({done, total, cached}).
 * Counted over what it has to read now, not what it still had from the turn before.
 * null when there is too little to read for a bar to mean anything (under about half a second of it).
 */
export function reading(event, { least = 600 } = {}) {
  if (!event) return null;
  const total = Number(event.total) || 0;
  const cached = Math.min(Number(event.cached) || 0, total);
  const done = Math.min(Math.max(Number(event.done) || 0, cached), total);
  const toRead = total - cached;
  if (toRead < least) return null;
  const percent = Math.max(0, Math.min(100, Math.round(((done - cached) / toRead) * 100)));
  return { percent, toRead, label: `${percent}%` };
}

/** A thin progress bar; `set(percent)` moves it. */
export function bar(doc = document) {
  ensureStyles(doc);
  const track = doc.createElement('span');
  track.className = 'working-bar';
  const fill = doc.createElement('i');
  track.appendChild(fill);
  return { el: track, set(percent) { fill.style.width = `${Math.max(0, Math.min(100, Number(percent) || 0))}%`; } };
}

export default { ensureStyles, MARK_HTML, mark, seconds, lastThought, reading, bar };
