// The almanac (/almanac): exact answers worked out on the server — units,
// map positions, the sun and the moon, dates. No model is asked and nothing
// leaves the house; the same code answers the assistant in a chat.
//
//   /almanac                     the four tools
//   /almanac?convert=5 ft 10 in  opened with something to convert
//   /almanac#sky                 opened at one of them (convert, where, sky, dates)

import { api, h, clear, icon, toast, oops, debounce, field } from './lite.js';

const app = document.getElementById('app');
const S = { me: null, home: null, places: 0, units: [] };
const homeHref = () => (S.me && S.me.home) || '/';
const el = {};

const input = (attrs) => h('input', { class: 'input', type: 'text', autocomplete: 'off', autocapitalize: 'off', spellcheck: 'false', ...attrs });
const answer = () => h('div', { class: 'tb-answer', 'aria-live': 'polite' });
const waiting = (node) => { node.classList.add('waiting'); };
const said = (node, ...children) => { node.classList.remove('waiting'); clear(node, ...children); };
const trouble = (node, error) => said(node, h('div', { class: 'tb-error' }, error && error.message ? error.message : String(error)));
const lines = (text) => String(text || '').split('\n').filter((line) => line.trim());

function card(id, name, title, hint, ...body) {
  return h('section', { class: 'card tb-card', id }, h('h2', {}, icon(name), title), hint ? h('p', { class: 'muted tb-hint' }, hint) : null, ...body);
}

// Each tool asks again only for the newest thing typed.
function latest() {
  let turn = 0;
  return async (work, node) => {
    const mine = ++turn;
    waiting(node);
    try { const data = await work(); if (mine === turn) return data; } catch (error) { if (mine === turn) trouble(node, error); }
    return null;
  };
}

// ── convert ────────────────────────────────────────────────────────────────
const askConvert = latest();
async function convert() {
  const amount = el.amount.value.trim();
  if (!amount) { said(el.converted); return; }
  const data = await askConvert(() => api('/api/almanac/convert', { method: 'POST', body: { amount, to: el.unit.value.trim() } }), el.converted);
  if (!data) return;
  clear(el.units, (data.choices || []).map((unit) => h('option', { value: unit.symbol }, unit.name)));
  const also = (data.also || []).slice(1);
  said(el.converted,
    h('div', { class: 'tb-small' }, `${data.from} =`),
    h('div', { class: 'tb-big' }, data.answer),
    also.length ? h('div', { class: 'tb-also' }, also.map((item) => h('span', { class: 'chip' }, item.text))) : null,
    data.note ? h('div', { class: 'tb-note' }, data.note) : null);
}

function convertCard() {
  el.amount = input({ placeholder: '5 ft 10 in,  72 F,  30 mpg,  2 cups', 'aria-label': 'Amount, with its unit', inputmode: 'text' });
  el.unit = input({ placeholder: 'cm  (or leave empty)', 'aria-label': 'Convert to', list: 'tb-units' });
  el.units = h('datalist', { id: 'tb-units' });
  el.converted = answer();
  const run = debounce(convert, 260);
  el.amount.addEventListener('input', run);
  el.unit.addEventListener('input', run);
  return card('convert', 'swap', 'Convert', 'Any measurement into other units. Leave “to” empty for the usual ones.',
    h('div', { class: 'tb-pair' }, field('Amount', el.amount), field('to', el.unit)), el.units, el.converted);
}

// ── where ──────────────────────────────────────────────────────────────────
const askWhere = latest();
async function where() {
  const from = el.from.value.trim();
  const to = el.to.value.trim();
  if (!from && !to) { said(el.found); return; }
  const body = from && to ? { from, to } : { place: from || to };
  const data = await askWhere(() => api('/api/almanac/place', { method: 'POST', body }), el.found);
  if (!data) return;
  const [head, ...rest] = lines(data.text);
  if (data.place) {
    const place = data.place;
    const height = place.elevation_m == null ? null : `${place.elevation_m.toLocaleString()} m (${Math.round(place.elevation_m / 0.3048).toLocaleString()} ft)`;
    said(el.found, h('div', { class: 'tb-head' }, place.label),
      h('dl', { class: 'tb-facts' },
        fact('Lat, long', place.decimal, true), fact('Deg min sec', place.dms, true), fact('UTM', place.utm, true), fact('MGRS', place.mgrs, true),
        height ? fact('Height', height) : null, place.zone ? fact('Clock', place.zone) : null),
      place.note ? h('div', { class: 'tb-note' }, place.note) : null,
      h('div', { class: 'tb-actions' }, h('button', { class: 'btn sm', type: 'button', onclick: () => saveHome(body.place) }, icon('home'), 'Save as my home')));
    return;
  }
  said(el.found, h('div', { class: 'tb-head' }, head), rest.map((line) => h('div', { class: /^Bearings are true/.test(line) || /^(Took|Read|That) /.test(line) ? 'tb-note' : 'tb-line' }, line.trim())));
}

// A label and its value; tapping a value copies it (for pasting into a map or a radio log).
function fact(label, value, copies = false) {
  const shown = h('dd', copies ? { class: 'copies', title: 'Tap to copy', onclick: async () => {
    try { await navigator.clipboard.writeText(value); toast('Copied'); } catch (_) { /* no clipboard on plain http: it can still be selected */ }
  } } : {}, value);
  return [h('dt', {}, label), shown];
}

async function saveHome(place) {
  try {
    const data = await api('/api/almanac/home', { method: 'PUT', body: { place } });
    S.home = data.home;
    toast(`Home is now ${S.home.label}`);
    homeLine();
    sky();
  } catch (error) { oops(error); }
}

async function forgetHome() {
  try { await api('/api/almanac/home', { method: 'DELETE' }); S.home = null; homeLine(); toast('Home position forgotten'); } catch (error) { oops(error); }
}

function homeLine() {
  if (!el.home) return;
  clear(el.home, S.home
    ? [icon('home'), h('span', {}, `Home: ${S.home.label}`), h('button', { class: 'btn ghost sm', type: 'button', onclick: forgetHome }, 'Forget')]
    : [icon('home'), h('span', {}, 'No home position saved yet. Look a place up above and press “Save as my home”.')]);
  if (el.skyPlace) el.skyPlace.placeholder = S.home ? `${S.home.label} (home)` : 'A town or coordinates';
}

function whereCard() {
  el.from = input({ placeholder: S.places ? 'Tulsa, OK   or   40.7484, -73.9857   or   18TWL8563211326' : '40.7484, -73.9857   or   18TWL8563211326', 'aria-label': 'A place' });
  el.to = input({ placeholder: 'A second place: how far, which way', 'aria-label': 'A second place' });
  el.found = answer();
  el.home = h('div', { class: 'tb-home muted' });
  const run = debounce(where, 420);
  el.from.addEventListener('input', run);
  el.to.addEventListener('input', run);
  return card('where', 'globe', 'Where',
    S.places ? 'A town, or coordinates in any notation: latitude and longitude, UTM or MGRS.' : 'Coordinates in any notation: latitude and longitude, UTM or MGRS. (Town names need the place list: aegis-almanac places fetch.)',
    h('div', { class: 'stack tight' }, field('Place', el.from), field('to (optional)', el.to)), el.found, el.home);
}

// ── sun and moon ───────────────────────────────────────────────────────────
const askSky = latest();
async function sky() {
  if (!el.sky) return;
  const place = el.skyPlace.value.trim();
  if (!place && !S.home) { said(el.sky, h('div', { class: 'muted' }, 'Type a place, or save a home position, to see its sunrise and sunset.')); return; }
  const body = { place, date: el.skyDate.value || 'today' };
  if (el.week.checked) body.days = 7;
  const data = await askSky(() => api('/api/almanac/sky', { method: 'POST', body }), el.sky);
  if (!data) return;
  if (el.week.checked || !data.summary) { said(el.sky, h('pre', { class: 'tb-table' }, data.text)); return; }
  const day = data.summary;
  const moon = day.moon;
  const tile = (label, value, sub = null) => h('div', { class: 'tb-tile' }, h('span', {}, label), h('b', {}, value), sub ? h('small', {}, sub) : null);
  let sun;
  if (day.state === 'up') sun = [tile('Sun', 'Up all day', 'midnight sun'), tile('Highest', day.noon)];
  else if (day.state === 'down') sun = [tile('Sun', 'Does not rise', 'polar night'), tile('Twilight', day.first_light ? `${day.first_light} – ${day.last_light}` : 'none')];
  else if (day.sunset_first) sun = [tile('Sunset', day.sunset, 'from the evening before'), tile('Sunrise', day.sunrise, 'still up at midnight')];
  else sun = [tile('Sunrise', day.sunrise || '—', day.first_light ? `first light ${day.first_light}` : null),
    tile('Sunset', day.sunset || '—', day.last_light ? `last light ${day.last_light}` : null)];
  const until = moon.until ? `, up until ${moon.until} next day` : '';
  const times = moon.rise && moon.set ? (moon.set_first ? `rises ${moon.rise}${until} (last night’s set at ${moon.set})` : `rises ${moon.rise}, sets ${moon.set}`)
    : moon.rise ? `rises ${moon.rise}${until}` : moon.set ? `sets ${moon.set}` : (moon.state === 'up' ? 'up all day' : moon.state === 'down' ? 'below the horizon all day' : '');
  const detail = lines(data.text).slice(1).filter((line) => /^At \d|twilight|fully dark|^Times are on your clock|^Took that/i.test(line));
  said(el.sky,
    h('div', { class: 'tb-small' }, `${day.label} · ${day.date} · times: ${day.zone}`),
    h('div', { class: 'tb-tiles' }, sun),
    day.state ? null : h('div', { class: 'tb-line' }, `${day.daylight} of daylight · sun highest at ${day.noon}`),
    h('div', { class: 'tb-moon' }, moonFace(moon.lit / 100, moon.waxing !== day.south),
      h('div', {}, h('b', {}, `${moon.name}, ${moon.lit}% lit`), h('div', { class: 'muted' }, times))),
    h('div', { class: 'tb-note' }, 'Coming: ', moon.next.map((item) => `${item.name.toLowerCase()} ${item.when}`).join(' · ')),
    detail.map((line) => h('div', { class: 'tb-note' }, line.trim())),
    h('div', { class: 'tb-note' }, 'For a level horizon; good to a minute or two.'));
}

// The moon as it looks: the lit part on the right while it grows (seen from north of the equator), on the left as it shrinks.
function moonFace(lit, litOnRight) {
  const NS = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(NS, 'svg');
  svg.setAttribute('viewBox', '0 0 48 48'); svg.setAttribute('class', 'tb-moonface'); svg.setAttribute('aria-hidden', 'true');
  const disc = document.createElementNS(NS, 'circle');
  disc.setAttribute('cx', '24'); disc.setAttribute('cy', '24'); disc.setAttribute('r', '21'); disc.setAttribute('class', 'dark');
  svg.appendChild(disc);
  const fraction = Math.max(0, Math.min(1, lit));
  if (fraction > 0.005) {
    const bulge = (21 * Math.abs(1 - 2 * fraction)).toFixed(2);
    const shape = document.createElementNS(NS, 'path');
    // the right half-circle, then back along the line between light and dark (an ellipse seen edge-on)
    shape.setAttribute('d', `M24 3 A21 21 0 0 1 24 45 A${bulge} 21 0 0 ${fraction > 0.5 ? 1 : 0} 24 3 Z`);
    shape.setAttribute('class', 'lit');
    if (!litOnRight) shape.setAttribute('transform', 'translate(48 0) scale(-1 1)');
    svg.appendChild(shape);
  }
  return svg;
}

function skyCard() {
  el.skyPlace = input({ 'aria-label': 'Place' });
  el.skyDate = h('input', { class: 'input', type: 'date', 'aria-label': 'Date' });
  el.week = h('input', { type: 'checkbox' });
  el.sky = answer();
  const run = debounce(sky, 420);
  el.skyPlace.addEventListener('input', run);
  el.skyDate.addEventListener('change', sky);
  el.week.addEventListener('change', sky);
  return card('sky', 'sun', 'Sun and moon', 'Sunrise, sunset, first and last light, and the moon — for any place and day.',
    h('div', { class: 'tb-pair' }, field('Place', el.skyPlace), field('Date', el.skyDate)),
    h('label', { class: 'tb-check' }, el.week, 'Show a week'), el.sky);
}

// ── dates ──────────────────────────────────────────────────────────────────
const askDates = latest();
async function dates() {
  const date = el.date.value.trim() || 'today';
  const second = el.second.value.trim();
  if (!el.date.value.trim() && !second) { said(el.dated); return; }
  const moving = /^[-+]?\s*\d+\s*[a-z]/i.test(second) && !/^\d{1,2}\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)/i.test(second);
  const body = !second ? { date } : (moving ? { date, add: second } : { date, to: second });
  const data = await askDates(() => api('/api/almanac/dates', { method: 'POST', body }), el.dated);
  if (!data) return;
  const [head, ...rest] = lines(data.text);
  const colon = head.indexOf(': ');
  const asked = colon > 0 ? head.slice(0, colon) : '';
  const [big, ...more] = (colon > 0 ? head.slice(colon + 2) : head).split(' — ');
  said(el.dated, asked ? h('div', { class: 'tb-small' }, `${asked}:`) : null, h('div', { class: 'tb-big' }, big.replace(/\.$/, '')),
    more.length ? h('div', { class: 'tb-line' }, more.join(' — ')) : null, rest.map((line) => h('div', { class: 'tb-note' }, line)));
}

function datesCard() {
  el.date = input({ placeholder: 'today,  March 12 2027,  next Friday', 'aria-label': 'A date' });
  el.second = input({ placeholder: 'another date — or 90 days, 6 weeks, 10 business days', 'aria-label': 'A second date, or how far to move' });
  el.dated = answer();
  const run = debounce(dates, 320);
  el.date.addEventListener('input', run);
  el.second.addEventListener('input', run);
  return card('dates', 'calendar', 'Dates', 'The days between two dates, a date some time on, or what day of the week a date falls on.',
    h('div', { class: 'stack tight' }, field('Date', el.date), field('to another date, or move it by', el.second)), el.dated);
}

// ── start ──────────────────────────────────────────────────────────────────
function render() {
  clear(app,
    h('div', { class: 'topbar' }, h('a', { class: 'icon-btn', href: homeHref(), title: 'Back', 'aria-label': 'Back' }, icon('back')),
      h('h1', {}, 'Almanac', h('span', { class: 'sub' }, 'Exact answers, worked out here. Works with no internet.'))),
    convertCard(), whereCard(), skyCard(), datesCard(),
    h('p', { class: 'muted tb-foot' }, 'You can also just ask in a chat: “how many cups in 2 litres”, “how far is it from Tulsa to Wichita”, ',
      '“when does it get dark tomorrow”, “what is 90 days from today”.'));
  homeLine();
  const asked = new URLSearchParams(location.search);
  if (asked.get('convert')) { el.amount.value = asked.get('convert'); convert(); }
  if (asked.get('place')) { el.from.value = asked.get('place'); where(); }
  if (asked.get('date')) { el.date.value = asked.get('date'); dates(); }
  sky();
  const target = location.hash && document.getElementById(location.hash.slice(1));
  if (target) target.scrollIntoView();
}

(async () => {
  try { S.me = await api('/api/me'); } catch (_) { S.me = null; }
  try {
    const data = await api('/api/almanac');
    S.home = data.home; S.places = data.places || 0; S.units = data.units || [];
  } catch (error) {
    clear(app, h('div', { class: 'card empty' }, h('b', {}, 'The almanac could not be opened'),
      /404|Not Found/i.test(error.message) ? 'The app needs restarting before this page works.' : error.message));
    return;
  }
  render();
})();
