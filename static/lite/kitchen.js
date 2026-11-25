// The kitchen (/kitchen): recipes set out for cooking from.
//
//   /kitchen            the cookbook
//   /kitchen?r=<id>     one recipe: for today (modern measures), as written, and step by step
//
// The server keeps each recipe as numbers (an amount in a unit, with
// millilitres and grams where they are known); this page turns them into
// words, so doubling a recipe or switching to metric is arithmetic here.

import { api, h, clear, icon, iconButton, toast, oops, sheet, confirmSheet, field, plural } from './lite.js';

const app = document.getElementById('app');
const S = { me: null, recipe: null, prefs: {}, timers: new Map(), tick: null, lock: null };
const go = (search, replace = false) => { history[replace ? 'replaceState' : 'pushState']({}, '', `/kitchen${search || ''}`); route(); };
const loading = () => h('div', { class: 'loading' }, h('span', { class: 'spinner' }));

// ── amounts as words ───────────────────────────────────────────────────────
const GLYPHS = [[0.125, '⅛'], [0.25, '¼'], [1 / 3, '⅓'], [0.375, '⅜'], [0.5, '½'], [0.625, '⅝'], [2 / 3, '⅔'], [0.75, '¾'], [0.875, '⅞']];
const PLURAL = { cup: 'cups', pint: 'pints', quart: 'quarts', gallon: 'gallons', ounce: 'ounces', pound: 'pounds', teaspoon: 'teaspoons', tablespoon: 'tablespoons', 'fluid ounce': 'fluid ounces' };
const METRIC_UNITS = new Set(['gram', 'kilogram', 'millilitre', 'litre']);
function amountText(amount) {
  if (!amount) return '';
  const whole = Math.floor(amount + 1e-9);
  const rest = amount - whole;
  if (rest < 0.06) return whole ? String(whole) : 'a little';
  if (rest > 0.94) return String(whole + 1);
  const glyph = GLYPHS.reduce((best, pair) => (Math.abs(pair[0] - rest) < Math.abs(best[0] - rest) ? pair : best))[1];
  return whole ? `${whole} ${glyph}` : glyph;
}
function kitchenText(amount, unit) {
  const said = amountText(amount);
  if (!unit || !said) return said;
  return `${said} ${amount > 1 ? (PLURAL[unit] || unit) : unit}`;
}
function rounded(value) {
  const step = value < 20 ? 1 : (value < 250 ? 5 : 10);
  return String(Math.round(value / step) * step);
}
function metricText(ml, grams) {
  if (grams) return grams >= 1000 ? `${(grams / 1000).toFixed(1).replace('.0', '')} kg` : `${rounded(grams)} g`;
  if (ml) return ml >= 1000 ? `${(ml / 1000).toFixed(1).replace('.0', '')} l` : `${rounded(ml)} ml`;
  return '';
}
// What to show for an ingredient at a scale, in the chosen units.
function ingredientText(ingredient, scale, units) {
  const amount = ingredient.amount ? ingredient.amount * scale : 0;
  if (!amount) return { amount: '', item: scale === 1 ? (ingredient.as_written || ingredient.item) : ingredient.item, loose: true };
  const kitchen = kitchenText(amount, ingredient.unit);
  const metric = metricText(ingredient.ml ? ingredient.ml * scale : 0, ingredient.grams ? ingredient.grams * scale : 0);
  const alreadyMetric = METRIC_UNITS.has(ingredient.unit);
  // A spoonful is measured with a spoon in any kitchen: millilitres when metric is asked for, nothing beside it otherwise.
  const spoon = ingredient.unit === 'teaspoon' || ingredient.unit === 'tablespoon';
  let said = kitchen;
  if (units === 'metric' && metric) said = spoon ? metricText(ingredient.ml * scale, 0) : metric;
  else if (units === 'both' && metric && !alreadyMetric && !spoon) said = `${kitchen} (${metric})`;
  else if (alreadyMetric && metric) said = metric;
  return { amount: said, item: ingredient.item + (ingredient.prep ? `, ${ingredient.prep}` : '') };
}
const ovenText = (oven) => (oven.f_to ? `${oven.f}–${oven.f_to} °F · ${oven.c}–${oven.c_to} °C` : `${oven.f} °F · ${oven.c} °C`);
const minutesText = (step) => (step.minutes_to ? `${step.minutes}–${step.minutes_to} min` : `${step.minutes} min`);

// ── what this browser remembers about a recipe ─────────────────────────────
function loadPrefs(id) {
  const prefs = { scale: 1, units: 'both', done: [], step: 0 };
  try { Object.assign(prefs, JSON.parse(localStorage.getItem(`aegis-kitchen-${id}`) || '{}')); } catch (_) { /* defaults */ }
  return prefs;
}
function savePrefs() {
  try { localStorage.setItem(`aegis-kitchen-${S.recipe.id}`, JSON.stringify(S.prefs)); } catch (_) { /* private window */ }
}

function route() {
  stopCooking(true);
  const q = new URLSearchParams(location.search);
  window.scrollTo(0, 0);
  if (q.get('r')) return renderRecipe(q.get('r'), q.get('cook') === '1');
  return renderCookbook();
}

// ── the cookbook ───────────────────────────────────────────────────────────
async function renderCookbook() {
  const body = h('div', {}, loading());
  clear(app,
    h('div', { class: 'topbar' }, h('a', { class: 'icon-btn', href: (S.me && S.me.home) || '/', title: 'Back', 'aria-label': 'Back' }, icon('back')),
      h('h1', {}, 'Kitchen', h('span', { class: 'sub' }, 'Recipes set out for cooking — old ones translated for today.')),
      h('a', { class: 'icon-btn', href: '/read', title: 'Reader: find recipes in the library', 'aria-label': 'Reader' }, icon('book'))),
    h('button', { class: 'btn primary block', type: 'button', onclick: addSheet }, icon('plus'), 'Add a recipe'), body);
  try {
    const { recipes } = await api('/api/kitchen/recipes');
    if (!recipes.length) {
      clear(body, h('div', { class: 'card empty', style: { marginTop: '14px' } }, h('b', {}, 'Your cookbook is empty'),
        'Paste a recipe, or give a link to one. It is kept as written, set out in modern measures, and broken into steps you can follow one at a time. ',
        'Old recipes work too: “butter the size of an egg” and “a quick oven” are translated, and marked as estimates.',
        h('div', { style: { marginTop: '12px' } }, h('a', { class: 'btn', href: '/read?q=recipe' }, icon('book'), 'Find a recipe in the library'))));
      return;
    }
    const filter = recipes.length > 6 ? h('input', { class: 'input', type: 'search', placeholder: `Find a recipe (${recipes.length})`, style: { marginTop: '12px' } }) : null;
    const list = h('div', { class: 'card flush rows', style: { marginTop: '12px' } });
    const draw = () => {
      const words = filter ? filter.value.trim().toLowerCase() : '';
      const shown = recipes.filter((r) => !words || `${r.title} ${r.source_title} ${r.year}`.toLowerCase().includes(words))
        .sort((a, b) => (b.favourite - a.favourite) || (b.created - a.created));
      clear(list, shown.length ? shown.map((r) => h('a', { class: 'row tap recipe-card', href: r.link, style: { color: 'inherit', textDecoration: 'none' }, onclick: (event) => { event.preventDefault(); go(`?r=${r.id}`); } },
        h('div', { class: 'main' }, h('div', { class: 'title' }, r.favourite ? h('span', { class: 'star' }, '★ ') : null, r.title),
          h('div', { class: 'meta' }, r.year ? h('span', {}, r.year) : null, r.ingredient_count ? h('span', {}, plural(r.ingredient_count, 'ingredient')) : null,
            h('span', {}, plural(r.step_count, 'step')), r.source_title ? h('span', { class: 'clip', style: { maxWidth: '55%' } }, r.source_title) : null)),
        icon('forward'))) : h('div', { class: 'empty' }, 'No recipe by that name.'));
    };
    if (filter) filter.addEventListener('input', draw);
    clear(body, filter, list);
    draw();
  } catch (error) { clear(body, h('div', { class: 'card empty', style: { marginTop: '14px' } }, error.message)); }
}

function waiting() {
  return sheet({ title: 'Setting the recipe out…', body: h('div', { class: 'stack' }, loading(),
    h('p', { class: 'muted center' }, 'Reading it, working out the measures and breaking it into steps. This can take a minute.')) });
}

function addSheet() {
  const text = h('textarea', { class: 'input', rows: '8', placeholder: 'Paste the recipe here, exactly as it is written…' });
  const link = h('input', { class: 'input', type: 'url', placeholder: 'https://… or a link from the library', inputmode: 'url' });
  const title = h('input', { class: 'input', type: 'text', maxlength: '160', placeholder: 'Leave empty to use the recipe’s own name' });
  sheet({
    title: 'Add a recipe',
    body: h('div', { class: 'stack' }, field('The recipe', text), field('…or a link to it', link, 'A web page (when the server is online), or a page from the offline library.'), field('What to call it', title)),
    actions: [{ label: 'Cancel', kind: 'ghost' }, { label: 'Set it out', kind: 'primary', run: async () => {
      const body = { title: title.value.trim() };
      if (text.value.trim().length >= 20) body.text = text.value; else if (link.value.trim()) body.url = link.value.trim();
      else { toast('Paste the recipe, or give a link to it', { bad: true }); return false; }
      const wait = waiting();
      try { const recipe = await api('/api/kitchen/recipes', { method: 'POST', body }); wait.close(); go(`?r=${recipe.id}`); } catch (error) { wait.close(); oops(error); return false; }
      return true;
    } }],
  });
}

// ── one recipe ─────────────────────────────────────────────────────────────
async function renderRecipe(id, cookNow) {
  clear(app, loading());
  let recipe;
  try { recipe = await api(`/api/kitchen/recipes/${encodeURIComponent(id)}`); } catch (error) {
    clear(app, h('div', { class: 'topbar' }, h('a', { class: 'icon-btn', href: '/kitchen', 'aria-label': 'Back', onclick: (event) => { event.preventDefault(); go(''); } }, icon('back')), h('h1', {}, 'Kitchen')),
      h('div', { class: 'card empty' }, h('b', {}, 'That recipe is not here'), error.message));
    return;
  }
  S.recipe = recipe;
  S.prefs = loadPrefs(recipe.id);
  document.title = `${recipe.title} — Kitchen`;
  const data = recipe.data || {};
  const ingredients = data.ingredients || [];
  const steps = data.steps || [];
  const yearSaid = recipe.year && ((recipe.source_title || '').includes(recipe.year) || recipe.title.includes(recipe.year));
  const about = [yearSaid ? '' : recipe.year, data.makes, recipe.source_title].filter(Boolean).join(' · ');
  const star = iconButton('star', 'Favourite', async () => {
    try { Object.assign(S.recipe, await api(`/api/kitchen/recipes/${recipe.id}`, { method: 'PATCH', body: { favourite: !S.recipe.favourite } })); star.classList.toggle('on', S.recipe.favourite); } catch (error) { oops(error); }
  }, recipe.favourite ? 'on' : '');
  const view = h('div');
  let tab = 'today';
  const seg = (choices, current, onpick) => {
    const row = h('div', { class: 'seg' });
    const draw = (chosen) => clear(row, choices.map(([value, label]) => h('button', { type: 'button', 'aria-pressed': String(value === chosen), onclick: () => { onpick(value); draw(value); } }, label)));
    draw(current);
    return row;
  };

  const drawToday = () => {
    const { scale, units } = S.prefs;
    const list = h('div', { class: 'card flush rows' }, ingredients.map((ingredient, index) => {
      const said = ingredientText(ingredient, scale, units);
      const box = h('input', { type: 'checkbox', 'aria-label': ingredient.item });
      box.checked = S.prefs.done.includes(index);
      const differs = ingredient.as_written && !said.loose && ingredient.as_written.toLowerCase() !== `${said.amount} ${said.item}`.toLowerCase();
      const row = h('label', { class: `ingredient${box.checked ? ' done' : ''}` }, box,
        h('span', { class: 'what' }, ingredient.estimate && said.amount ? h('span', { class: 'about', title: 'An estimate: the recipe gives this as a judgement, not a measure' }, '≈ ') : null,
          said.amount ? h('b', {}, `${said.amount} `) : null, said.item,
          differs && (ingredient.estimate || scale !== 1) ? h('span', { class: 'old' }, `as written: “${ingredient.as_written}”`) : null,
          ingredient.note ? h('span', { class: 'why' }, ingredient.note) : null));
      box.addEventListener('change', () => {
        S.prefs.done = box.checked ? [...new Set([...S.prefs.done, index])] : S.prefs.done.filter((i) => i !== index);
        row.classList.toggle('done', box.checked);
        savePrefs();
      });
      return row;
    }));
    clear(view,
      h('div', { class: 'controls' },
        seg([[0.5, '½×'], [1, '1×'], [2, '2×'], [3, '3×']], scale, (value) => { S.prefs.scale = value; savePrefs(); drawToday(); }),
        seg([['us', 'Cups & spoons'], ['metric', 'Metric'], ['both', 'Both']], units, (value) => { S.prefs.units = value; savePrefs(); drawToday(); })),
      ingredients.length ? [h('div', { class: 'section-title' }, h('h2', {}, 'Ingredients'), h('span', { class: 'count' }, String(ingredients.length)),
        S.prefs.done.length ? h('button', { class: 'btn ghost sm', type: 'button', onclick: () => { S.prefs.done = []; savePrefs(); drawToday(); } }, 'Untick all') : null), list] : null,
      steps.length ? [h('div', { class: 'section-title' }, h('h2', {}, 'Method'), h('span', { class: 'count' }, String(steps.length))),
        h('div', { class: 'card flush rows method' }, steps.map((step) => h('div', { class: 'step' }, h('div', { class: 'grow' }, step.do,
          (step.minutes || step.oven) ? h('div', { class: 'chips', style: { marginTop: '6px' } }, step.minutes ? h('span', { class: 'chip' }, icon('clock', 'sm'), minutesText(step)) : null,
            step.oven ? h('span', { class: 'chip warn' }, `Oven ${ovenText(step.oven)}`) : null) : null,
          step.as_written ? h('span', { class: 'old' }, `“${step.as_written}”`) : null))))] : null,
      (data.notes || []).length ? [h('div', { class: 'section-title' }, h('h2', {}, 'Good to know')), h('div', { class: 'card' }, h('ul', { class: 'notes' }, data.notes.map((note) => h('li', {}, note))))] : null,
      h('p', { class: 'tiny faint', style: { marginTop: '14px' } }, data.by_model
        ? '≈ marks an estimate. Amounts in grams come from the usual weight of a cup of that ingredient; treat them as close, not exact. The original wording is under “As written”.'
        : 'This recipe has not been set out by a model yet, so it is shown a sentence at a time. Choose “Set it out again” in the menu when a model is available.'));
  };
  const drawWritten = () => clear(view,
    h('div', { class: 'card', style: { marginTop: '12px' } }, h('div', { class: 'as-written' }, recipe.original)),
    recipe.source_url ? h('p', { class: 'small', style: { marginTop: '10px' } }, 'From ', h('a', { href: recipe.source_url, ...(recipe.source_url.startsWith('http') ? { target: '_blank', rel: 'noopener noreferrer' } : {}) }, recipe.source_title || 'the source')) : (recipe.source_title ? h('p', { class: 'small muted', style: { marginTop: '10px' } }, `From ${recipe.source_title}`) : null));
  const draw = () => (tab === 'today' ? drawToday() : drawWritten());

  clear(app,
    h('div', { class: 'topbar' }, h('a', { class: 'icon-btn', href: '/kitchen', title: 'Back to the cookbook', 'aria-label': 'Back', onclick: (event) => { event.preventDefault(); go(''); } }, icon('back')),
      h('h1', {}, recipe.title, about ? h('span', { class: 'sub' }, about) : null), star, iconButton('more', 'More', () => moreSheet(recipe))),
    steps.length ? h('button', { class: 'btn primary block', type: 'button', onclick: () => startCooking(S.prefs.step < steps.length ? S.prefs.step : 0) }, icon('pot'),
      S.prefs.step > 0 && S.prefs.step < steps.length ? `Carry on cooking (step ${S.prefs.step + 1} of ${steps.length})` : 'Start cooking — one step at a time') : null,
    h('div', { style: { marginTop: '12px' } }, seg([['today', 'For today'], ['written', 'As written']], tab, (value) => { tab = value; draw(); })),
    view);
  draw();
  if (cookNow && steps.length) startCooking(0);
}

function moreSheet(recipe) {
  const panel = { close: () => {} };
  const row = (name, label, run) => h('button', { class: 'btn block', type: 'button', onclick: () => { panel.close(); run(); } }, icon(name), label);
  Object.assign(panel, sheet({ title: recipe.title, body: h('div', { class: 'stack' },
    row('edit', 'Rename', () => {
      const title = h('input', { class: 'input', type: 'text', maxlength: '160', value: recipe.title });
      sheet({ title: 'Rename', body: title, actions: [{ label: 'Cancel', kind: 'ghost' }, { label: 'Save', kind: 'primary', run: async () => { await api(`/api/kitchen/recipes/${recipe.id}`, { method: 'PATCH', body: { title: title.value } }); renderRecipe(recipe.id); } }] });
    }),
    row('repeat', 'Set it out again', async () => {
      const wait = waiting();
      try { await api(`/api/kitchen/recipes/${recipe.id}/redo`, { method: 'POST' }); wait.close(); toast('Set out again'); renderRecipe(recipe.id); } catch (error) { wait.close(); oops(error); }
    }),
    h('a', { class: 'btn block', href: `/api/kitchen/recipes/${recipe.id}/text`, download: `${recipe.title.replace(/[^\w -]+/g, '').trim() || 'recipe'}.md` }, icon('download'), 'Save as a text file'),
    row('trash', 'Delete', async () => {
      if (!await confirmSheet(`“${recipe.title}” will be removed from your cookbook.`, { title: 'Delete this recipe?', ok: 'Delete', danger: true })) return;
      try { await api(`/api/kitchen/recipes/${recipe.id}`, { method: 'DELETE' }); try { localStorage.removeItem(`aegis-kitchen-${recipe.id}`); } catch (_) { /* fine */ } go(''); } catch (error) { oops(error); }
    })) }));
}

// ── follow along ───────────────────────────────────────────────────────────
// The ingredients a step uses: the ones it names.
function needsFor(step, ingredients) {
  const said = ` ${step.do.toLowerCase()} ${(step.as_written || '').toLowerCase()} `;
  return ingredients.filter((ingredient) => {
    const item = (ingredient.item || '').toLowerCase().replace(/\(.*?\)/g, '').trim();
    if (!item) return false;
    const head = item.split(/[\s,]+/).filter((word) => word.length > 2).pop() || item;
    return said.includes(item) || new RegExp(`\\b${head.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}s?\\b`).test(said);
  });
}

function beep() {
  try {
    const audio = new (window.AudioContext || window.webkitAudioContext)();
    [0, 0.35, 0.7].forEach((at) => {
      const tone = audio.createOscillator();
      const gain = audio.createGain();
      tone.frequency.value = 880;
      gain.gain.setValueAtTime(0.25, audio.currentTime + at);
      gain.gain.exponentialRampToValueAtTime(0.001, audio.currentTime + at + 0.25);
      tone.connect(gain).connect(audio.destination);
      tone.start(audio.currentTime + at);
      tone.stop(audio.currentTime + at + 0.26);
    });
    setTimeout(() => audio.close(), 1500);
  } catch (_) { /* no sound here */ }
  try { if (navigator.vibrate) navigator.vibrate([300, 150, 300, 150, 300]); } catch (_) { /* no vibration */ }
}
const clock = (seconds) => { const s = Math.max(0, Math.ceil(seconds)); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`; };

async function keepAwake(on) {
  try {
    if (on && 'wakeLock' in navigator && !S.lock) { S.lock = await navigator.wakeLock.request('screen'); S.lock.addEventListener('release', () => { S.lock = null; }); }
    if (!on && S.lock) { await S.lock.release(); S.lock = null; }
  } catch (_) { S.lock = null; }
}
document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible' && document.querySelector('.cook')) keepAwake(true); });

function stopCooking(silent = false) {
  const open = document.querySelector('.cook');
  if (open) open.remove();
  keepAwake(false);
  try { window.speechSynthesis.cancel(); } catch (_) { /* no speech here */ }
  if (!silent && S.recipe) renderRecipe(S.recipe.id);
}

function startCooking(at) {
  const recipe = S.recipe;
  const steps = recipe.data.steps || [];
  const ingredients = recipe.data.ingredients || [];
  let index = Math.max(0, Math.min(at, steps.length - 1));
  let aloud = !!S.prefs.aloud;
  const count = h('div', { class: 'count' });
  const dots = h('div', { class: 'cook-dots' });
  const body = h('div', { class: 'cook-body' });
  const running = h('div', { class: 'running' });
  const back = h('button', { class: 'btn', type: 'button' }, icon('back'), 'Back');
  const next = h('button', { class: 'btn primary', type: 'button' });
  const speak = iconButton('speaker', 'Read each step aloud', () => { aloud = !aloud; S.prefs.aloud = aloud; savePrefs(); speak.classList.toggle('on', aloud); if (aloud) say(); else try { window.speechSynthesis.cancel(); } catch (_) { /* fine */ } }, aloud ? 'on' : '');
  const say = () => {
    try { window.speechSynthesis.cancel(); if (aloud) window.speechSynthesis.speak(new SpeechSynthesisUtterance(steps[index].do)); } catch (_) { /* no speech here */ }
  };

  const drawRunning = () => clear(running, [...S.timers.entries()].map(([step, timer]) => {
    const left = (timer.ends - Date.now()) / 1000;
    return h('button', { class: `chip${left <= 0 ? ' over' : ''}`, type: 'button', title: 'Go to this step', onclick: () => { index = step; draw(); } }, `Step ${step + 1} · ${left <= 0 ? 'time is up' : clock(left)}`);
  }));
  const tick = () => {
    for (const [step, timer] of S.timers) {
      if (!timer.rang && timer.ends <= Date.now()) { timer.rang = true; beep(); toast(`Step ${step + 1}: time is up`, { ms: 8000 }); }
    }
    drawRunning();
    const shown = body.querySelector('.timer .clock');
    const mine = S.timers.get(index);
    if (shown && mine) { const left = (mine.ends - Date.now()) / 1000; shown.textContent = left <= 0 ? '0:00' : clock(left); shown.parentNode.classList.toggle('over', left <= 0); }
    if (!S.timers.size && S.tick) { clearInterval(S.tick); S.tick = null; }
  };
  const timerFor = (step) => {
    const mine = S.timers.get(index);
    const shown = h('span', { class: 'clock' }, mine ? clock((mine.ends - Date.now()) / 1000) : clock(step.minutes * 60));
    const button = h('button', { class: 'btn', type: 'button' }, mine ? 'Stop' : `Start ${minutesText(step)} timer`);
    button.addEventListener('click', () => {
      if (S.timers.has(index)) S.timers.delete(index); else { S.timers.set(index, { ends: Date.now() + step.minutes * 60000, rang: false }); if (!S.tick) S.tick = setInterval(tick, 500); }
      draw();
    });
    return h('div', { class: `timer${mine && mine.ends <= Date.now() ? ' over' : ''}` }, icon('clock'), shown, button);
  };

  function draw() {
    const step = steps[index];
    S.prefs.step = index;
    savePrefs();
    count.textContent = `Step ${index + 1} of ${steps.length}`;
    clear(dots, steps.map((_, i) => h('i', { class: i < index ? 'past' : (i === index ? 'now' : '') })));
    const needs = needsFor(step, ingredients);
    clear(body,
      h('div', { class: 'cook-step' }, step.do),
      needs.length ? h('div', { class: 'cook-needs' }, needs.map((ingredient) => { const said = ingredientText(ingredient, S.prefs.scale, S.prefs.units); return h('span', { class: 'chip accent' }, [ingredient.estimate && said.amount ? '≈ ' : '', said.amount, ' ', said.item].join('').trim()); })) : null,
      (step.minutes || step.oven) ? h('div', { class: 'cook-tools' }, step.minutes ? timerFor(step) : null, step.oven ? h('span', { class: 'chip warn', style: { fontSize: '.95rem', padding: '7px 13px' } }, `Oven ${ovenText(step.oven)}${step.oven.word ? ` (“${step.oven.word}”)` : ''}`) : null) : null,
      step.as_written ? h('div', { class: 'cook-old' }, `As written: “${step.as_written}”`) : null);
    back.disabled = index === 0;
    clear(next, index === steps.length - 1 ? 'Finished' : 'Next', index === steps.length - 1 ? icon('check') : icon('forward'));
    drawRunning();
    say();
  }
  back.addEventListener('click', () => { if (index > 0) { index -= 1; draw(); } });
  next.addEventListener('click', () => {
    if (index < steps.length - 1) { index += 1; draw(); return; }
    S.prefs.step = 0; S.prefs.done = []; savePrefs();
    S.timers.clear();
    stopCooking();
    toast('Done — enjoy it');
  });

  const screen = h('div', { class: 'cook', role: 'dialog', 'aria-label': 'Follow along' },
    h('div', { class: 'cook-top' }, iconButton('x', 'Stop cooking', () => stopCooking()), count, speak,
      iconButton('list', 'Ingredients', () => sheet({ title: 'Ingredients', body: h('div', { class: 'card flush rows' }, ingredients.map((ingredient) => { const said = ingredientText(ingredient, S.prefs.scale, S.prefs.units); return h('div', { class: 'row' }, h('div', { class: 'main' }, said.amount ? h('b', {}, `${ingredient.estimate ? '≈ ' : ''}${said.amount} `) : null, said.item)); })), actions: [{ label: 'Close', kind: 'primary' }] }))),
    dots, running, body, h('div', { class: 'cook-nav' }, back, next));
  screen.addEventListener('keydown', (event) => { if (event.key === 'ArrowRight') next.click(); if (event.key === 'ArrowLeft') back.click(); });
  let touchX = null;
  screen.addEventListener('touchstart', (event) => { touchX = event.touches[0].clientX; }, { passive: true });
  screen.addEventListener('touchend', (event) => {
    if (touchX === null) return;
    const moved = event.changedTouches[0].clientX - touchX;
    touchX = null;
    if (Math.abs(moved) > 70) (moved < 0 ? next : back).click();
  }, { passive: true });
  document.body.append(screen);
  keepAwake(true);
  if (S.timers.size && !S.tick) S.tick = setInterval(tick, 500);
  draw();
}

// ── start ──────────────────────────────────────────────────────────────────
window.addEventListener('popstate', route);
(async () => {
  try {
    S.me = await api('/api/me');
    // People who use the simple home screen get its look here too; everyone else the default.
    const look = (S.me && S.me.experience) || {};
    if (S.me && S.me.home === '/home') {
      if (look.theme) document.documentElement.setAttribute('data-theme', look.theme);
      if (/^#[0-9a-f]{6}$/i.test(look.accent || '')) document.documentElement.style.setProperty('--accent', look.accent);
    }
  } catch (_) { S.me = null; }
  route();
})();
