// The administrator's console: who has an account here, what each person can
// do, what the app is like for them, and what has happened to the accounts.
// It shows how much each person has stored, never what.
import {
  api, h, add, clear, icon, iconButton, toast, oops, sheet, confirmSheet, field, toggle, when, ago, debounce, plural,
} from './lite.js';

const app = document.getElementById('app');
const view = h('div');
const state = {
  tab: 'people', overview: null, person: null, find: '',
  draft: null,      // what was changed on the open person's page and is not saved yet
  models: null,     // what GET /api/models answered, once a person's page has asked
  activity: { user: '', limit: 200, entries: [] },
  household: null,  // what GET /api/admin/household answered
  review: null,     // what GET /api/admin/review answered
};

const TABS = [['people', 'People'], ['household', 'Household'], ['roles', 'Roles'], ['activity', 'Activity'], ['review', 'Review']];
const MODES = [
  ['simple', 'Family screen', 'Chat that does things, lists, reminders, trackers and boards, with Kitchen, Reader, Translate and the Almanac a tap away. Plain language, nothing to set up.'],
  ['full', 'Full app', 'The larger app, with notes, calendar, Studio and documents. Models, the engine and every connection stay with administrators and are not shown.'],
];
const MODE_LABEL = { simple: 'Family screen', full: 'Full app' };
const THEME_LABEL = { auto: 'Match their device', light: 'Light', dark: 'Dark' };
const TEXT_KEYS = ['display_name', 'assistant_name', 'tone'];
const APP_WORDS = {
  mode: 'family screen or full app', display_name: 'name shown', assistant_name: 'name of the assistant', tone: 'how it talks to them',
  sections: 'family screen sections', theme: 'light or dark', accent: 'colour', allow_full_app: 'opening the full app', hidden_ui: 'hidden parts',
};
const SERVER_NOTE = 'No role can give access to the server, its files or other computers. That is for administrators only.';

// ── shell ──────────────────────────────────────────────────────────────────

function shell() {
  const tabs = h('div', { class: 'tabs', role: 'tablist' }, TABS.map(([id, label]) => h('button', {
    class: 'tab', role: 'tab', type: 'button', 'aria-selected': String(state.tab === id), dataset: { tab: id },
    onclick: () => go(id, { person: null }),
  }, label)));
  clear(app,
    h('div', { class: 'topbar' },
      iconButton('back', 'Back', () => { if (document.referrer && history.length > 1) history.back(); else location.href = '/'; }),
      h('h1', {}, 'People and access', h('span', { class: 'sub' }, 'Who can sign in, and what they can do'))),
    tabs, view);
}

function hashFor() {
  if (state.tab === 'people' && state.person) return `#people/${encodeURIComponent(state.person)}`;
  if (state.tab === 'activity' && state.activity.user) return `#activity/${encodeURIComponent(state.activity.user)}`;
  return `#${state.tab}`;
}

async function go(tab, extra = {}) {
  if (unsaved() && !await confirmSheet('Some of what you changed on this page is not saved yet.', { title: 'Leave without saving?', ok: 'Leave' })) return;
  state.draft = null;
  Object.assign(state, extra);
  state.tab = tab;
  if (tab !== 'people') state.person = null;
  const hash = hashFor();
  if (location.hash !== hash) history.replaceState(null, '', hash);
  document.querySelectorAll('.tab').forEach((t) => t.setAttribute('aria-selected', String(t.dataset.tab === tab)));
  await render();
}

const LOAD = { people: loadPeople, household: loadHousehold, roles: () => loadOverview(true), activity: loadActivity, review: loadReview };
let drawn = 0;        // the newest render() wins when tabs are tapped quickly
let rendering = 0;
let asked = 0;        // the newest answer from the server wins

async function render() {
  const mine = ++drawn;
  rendering += 1;
  clear(view, h('div', { class: 'loading' }, h('span', { class: 'spinner' })));
  try {
    await LOAD[state.tab]();
    if (mine === drawn) draw();
  } catch (error) {
    if (mine === drawn) clear(view, h('div', { class: 'card empty' }, h('b', {}, 'Could not load this'), error.message));
  } finally { rendering -= 1; }
  if (mine === drawn) window.scrollTo(0, 0);
}

async function loadOverview(force = false) {
  if (state.overview && !force) return state.overview;
  const mine = ++asked;
  const data = await api('/api/admin/overview');
  if (mine === asked || !state.overview) state.overview = data;
  return state.overview;
}

// After every change: fetch everything again and redraw where the
// administrator is — no spinner, no jump to the top.
async function refresh() {
  const tab = drawn;
  const mine = asked + 1;
  try { await loadOverview(true); } catch (error) { oops(error); return; }
  if (tab === drawn && mine === asked && !rendering) redraw();
}

function draw() {
  if (state.tab === 'roles') drawRoles();
  else if (state.tab === 'household') drawHousehold();
  else if (state.tab === 'activity') drawActivity();
  else if (state.tab === 'review') drawReview();
  else if (state.person) drawPerson();
  else drawPeople();
}

// Draw again without losing the place: the control in use keeps the focus.
function redraw() {
  const active = document.activeElement;
  const key = active && view.contains(active) && active.dataset ? active.dataset.focus : '';
  const caret = key && typeof active.selectionStart === 'number' ? [active.selectionStart, active.selectionEnd] : null;
  draw();
  if (!key) return;
  const next = [...view.querySelectorAll('[data-focus]')].find((node) => node.dataset.focus === key);
  if (!next || next.disabled) return;
  next.focus({ preventScroll: true });
  if (caret) { try { next.setSelectionRange(caret[0], caret[1]); } catch (_) { /* not a text box */ } }
}

// ── small pieces ───────────────────────────────────────────────────────────

const nameOf = (u) => u.display_name || u.username;
const userPath = (username) => `/api/admin/users/${encodeURIComponent(username)}`;
const roleNamed = (name) => state.overview.roles.find((r) => r.name === name) || null;
const tidy = (text) => String(text || '').replace(/\s+/g, ' ').trim();
// A note of a few lines stays a note of a few lines (the server tidies it the same way).
const tidyNote = (text) => String(text || '').split(/\r\n|\r|\n/).map((line) => line.replace(/[^\S\n]+/g, ' ').trim())
  .join('\n').replace(/\n{3,}/g, '\n\n').trim();
const cleaned = (key, value) => (key === 'tone' ? tidyNote(value) : tidy(value));
const titled = (text) => String(text || '').replace(/(^|[^a-zA-Z])([a-z])/g, (_, before, letter) => before + letter.toUpperCase());
const same = (a, b) => (Array.isArray(a) && Array.isArray(b) ? a.length === b.length && a.every((x) => b.includes(x)) : a === b);
const stored = (u) => `${plural(u.counts.chats, 'chat')} · ${plural(u.counts.memories, 'memory', 'memories')}`;
const keyed = (control, key) => { (control.matches('.switch') ? control.firstChild : control).dataset.focus = key; return control; };

function draftFor(u) {
  if (!state.draft || state.draft.username !== u.username) {
    state.draft = { username: u.username, role: null, model: null, experience: {}, numbers: {} };
  }
  return state.draft;
}

function unsaved() {
  const d = state.draft;
  return !!d && (d.role !== null || d.model !== null || Object.keys(d.experience).length > 0 || Object.keys(d.numbers).length > 0);
}

function select(pairs, current, attrs = {}) {
  return h('select', { class: 'input', ...attrs }, pairs.map(([value, label]) => {
    const option = h('option', { value }, label);
    option.selected = value === current;
    return option;
  }));
}

// A setting: what it is and what it means on the left, its control on the right.
function setting(title, text, control, ...below) {
  return h('div', { class: 'row' },
    h('div', { class: 'main' }, h('div', {}, title), text ? h('div', { class: 'small muted' }, text) : null, below), control);
}

// A caption over something that is not a single control (a field() without the <label>).
const group = (label, ...content) => h('div', { class: 'field', role: 'group', 'aria-label': label }, h('span', {}, label), content);

// Large either-or choices; real radio buttons underneath.
function choices(name, options, current, onpick) {
  const box = h('div', { class: 'choices', role: 'radiogroup' }, options.map(([value, title, text]) => {
    const radio = h('input', { type: 'radio', name, value, dataset: { focus: `${name}-${value}` } });
    radio.checked = value === current;
    radio.addEventListener('change', () => {
      box.querySelectorAll('.choice').forEach((c) => c.classList.toggle('on', c.contains(radio)));
      onpick(value);
    });
    return h('label', { class: `choice${value === current ? ' on' : ''}` }, radio, h('b', {}, title), h('span', {}, text));
  }));
  return box;
}

// Tick boxes for a catalogue of {id, label, description}; answers with the ticked ids in its order.
function ticks(items, chosen, onchange, { grid = false, key = '' } = {}) {
  const boxes = items.map((item) => {
    const box = h('input', { type: 'checkbox', value: item.id, dataset: key ? { focus: `${key}-${item.id}` } : {} });
    box.checked = chosen.includes(item.id);
    return box;
  });
  boxes.forEach((box) => box.addEventListener('change', () => onchange(boxes.filter((b) => b.checked).map((b) => b.value))));
  return h('div', { class: grid ? 'ticks grid' : 'ticks' }, items.map((item, i) => h('label', { class: 'check' }, boxes[i],
    h('span', {}, item.label, item.description ? h('span', { class: 'tiny muted hint' }, item.description) : null))));
}

// A password box with an eye, to check what was typed before telling the person.
function secretBox() {
  const input = h('input', { class: 'input', type: 'password', autocomplete: 'new-password', autocapitalize: 'none', autocorrect: 'off', spellcheck: 'false' });
  const eye = iconButton('eye', 'Show what you typed', () => {
    const show = input.type === 'password';
    input.type = show ? 'text' : 'password';
    eye.setAttribute('aria-pressed', String(show));
  });
  eye.setAttribute('aria-pressed', 'false');
  const digits = (on) => {
    if (on) { input.setAttribute('inputmode', 'numeric'); input.setAttribute('pattern', '[0-9]*'); input.setAttribute('autocomplete', 'off'); }
    else { input.removeAttribute('inputmode'); input.removeAttribute('pattern'); input.setAttribute('autocomplete', 'new-password'); }
  };
  return { input, digits, node: h('div', { class: 'secret' }, input, eye) };
}

// In a sheet, Enter in a one-line box does what its last button does.
function submitOnEnter(panel) {
  panel.addEventListener('keydown', (event) => {
    if (event.key !== 'Enter' || event.isComposing || !event.target.matches('input:not([type="checkbox"]):not([type="radio"])')) return;
    const buttons = panel.querySelectorAll('.actions .btn');
    if (buttons.length) { event.preventDefault(); buttons[buttons.length - 1].click(); }
  });
}

function passwordRules() {
  const policy = state.overview.policy || {};
  const low = policy.pin_min || 4;
  const high = policy.pin_max || 12;
  return {
    pin: `${low}–${high} digits`,
    pinHint: `${low} to ${high} digits, not a repeat or a run like 1111 or 1234.`,
    passwordHint: `At least ${policy.password_min_length || 8} characters.`,
    lock: 'After five wrong tries the account locks for a growing wait.',
  };
}

// ── people ─────────────────────────────────────────────────────────────────

async function loadPeople() {
  await loadOverview(true);
  if (state.person) loadModels();
}

const avatar = (u, extra = '') => h('span', { class: `avatar ${extra}`.trim(), 'aria-hidden': 'true' }, ([...nameOf(u).trim()][0] || '?').toUpperCase());

function tagsFor(u) {
  const tags = [u.is_admin
    ? h('span', { class: 'chip accent' }, icon('shield', 'sm'), u.role_label || 'Administrator')
    : h('span', { class: 'chip' }, u.role_label || u.role)];
  if (u.username === state.overview.me) tags.push(h('span', { class: 'chip' }, 'you'));
  if (u.disabled) tags.push(h('span', { class: 'chip danger' }, 'switched off'));
  if (u.allow_pin) tags.push(h('span', { class: 'chip' }, 'PIN'));
  if (u.two_factor) tags.push(h('span', { class: 'chip ok' }, '2-step'));
  return tags;
}

// Sign-ins are only counted from when this console arrived: an account that has
// plainly been used is not called "never signed in".
function lastSeen(u) {
  if (u.last_sign_in) return h('span', {}, h('span', { class: 'lead' }, 'Signed in '), ago(u.last_sign_in));
  const used = u.username === state.overview.me || u.counts.chats > 0 || u.counts.memories > 0;
  return h('span', {}, used ? 'Last sign-in not recorded' : 'Never signed in');
}

function drawPeople() {
  const o = state.overview;
  const list = h('div');
  const searchable = o.users.length > 6;
  if (!searchable) state.find = '';
  const paint = () => {
    const q = state.find.trim().toLowerCase();
    const shown = o.users.filter((u) => !q || `${u.display_name} ${u.username} ${u.role_label}`.toLowerCase().includes(q));
    clear(list, shown.length
      ? h('div', { class: 'card flush rows people' },
        h('div', { class: 'row person head', 'aria-hidden': 'true' },
          h('span'), h('span', {}, 'Person'), h('span', {}, 'Role'), h('span', {}, 'Last sign-in'), h('span', {}, 'Stored'), h('span')),
        shown.map(personRow))
      : h('div', { class: 'card empty' }, h('b', {}, 'Nobody matches'), 'Try another name.'));
  };
  const find = h('input', { class: 'input', type: 'search', placeholder: 'Find a person', 'aria-label': 'Find a person', value: state.find });
  find.addEventListener('input', debounce(() => { state.find = find.value; paint(); }, 150));
  clear(view,
    h('div', { class: 'line wrap', style: { marginBottom: '10px' } },
      h('p', { class: 'small muted grow' }, 'Everyone with an account. Open a person to change their role, what the app is like for them, and how they sign in.'),
      h('button', { class: 'btn primary', type: 'button', onclick: addPerson }, icon('plus', 'sm'), 'Add person')),
    searchable ? h('div', { class: 'search', style: { marginBottom: '10px' } }, icon('search'), find) : null,
    o.policy && o.policy.signup_enabled ? h('div', { class: 'card soft notice', style: { marginBottom: '10px' } }, icon('info'),
      h('span', {}, 'Open signup is on: anyone who reaches the sign-in page can make their own account, as a Member. It can be switched off in the main app, under Admin → Registration.')) : null,
    list,
    h('p', { class: 'small muted', style: { margin: '12px 2px 0' } }, 'This console shows how much each person has stored — never what.'),
    removedCard());
  paint();
}

function personRow(u) {
  return h('a', {
    class: `row tap person${u.disabled ? ' off' : ''}`, href: `#people/${encodeURIComponent(u.username)}`,
    onclick: (event) => {
      if (event.metaKey || event.ctrlKey || event.shiftKey || event.button) return;     // a new tab or window was asked for
      event.preventDefault();
      go('people', { person: u.username });
    },
  },
    avatar(u),
    h('div', { class: 'who' }, h('h3', { class: 'clip' }, nameOf(u)), h('div', { class: 'small muted clip' }, `@${u.username}`)),
    h('div', { class: 'chips tags' }, tagsFor(u)),
    h('div', { class: 'facts small muted' }, lastSeen(u), h('span', {}, stored(u))),
    icon('back', 'sm go'));
}

function addPerson() {
  const o = state.overview;
  const rules = passwordRules();
  const username = h('input', { class: 'input', maxlength: '32', autocomplete: 'off', autocapitalize: 'none', autocorrect: 'off', spellcheck: 'false', placeholder: 'e.g. nina' });
  const shown = h('input', { class: 'input', maxlength: '40', autocomplete: 'off', placeholder: 'e.g. Nina' });
  const role = select(o.roles.map((r) => [r.name, r.label]), o.roles.some((r) => r.name === 'household') ? 'household' : 'member');
  const about = h('span');
  const describeRole = () => { const picked = roleNamed(role.value); about.textContent = picked ? picked.description : ''; };
  role.addEventListener('change', describeRole);
  describeRole();
  const secret = secretBox();
  const secretField = field('Password', secret.node, rules.passwordHint);
  let pin = false;
  const usePin = (on) => {
    pin = on;
    secret.digits(on);
    secretField.firstElementChild.textContent = on ? 'PIN' : 'Password';
    secretField.lastElementChild.textContent = on ? rules.pinHint : rules.passwordHint;
  };
  const s = sheet({
    title: 'Add a person',
    body: h('div', { class: 'stack' },
      field('Username', username, 'What they type to sign in: letters, digits, dots, dashes or underscores.'),
      field('Name shown (optional)', shown, 'What they are called on screen and by the assistant.'),
      field('Role', role, about),
      h('div', { class: 'line top' },
        h('div', { class: 'grow' }, h('div', {}, 'Use a short PIN instead of a password'),
          h('div', { class: 'small muted' }, `${rules.pin}. ${rules.lock}`)),
        toggle(false, usePin, 'Use a short PIN instead of a password')),
      secretField),
    actions: [{ label: 'Cancel', kind: 'ghost' }, { label: 'Add person', kind: 'primary', run: async () => {
      if (!username.value.trim()) { username.focus(); return false; }
      if (!secret.input.value) { secret.input.focus(); return false; }
      const r = await api('/api/admin/users', { method: 'POST', body: {
        username: username.value.trim(), display_name: shown.value.trim(), role: role.value, password: secret.input.value, allow_pin: pin,
      } });
      toast(`${nameOf(r.user)} added`);
      go('people', { person: r.user.username });
      return true;
    } }],
  });
  s.panel.classList.add('form');
  submitOnEnter(s.panel);
}

// ── one person ─────────────────────────────────────────────────────────────

const block = (order, title, ...content) => h('section', { class: 'block', style: { order: String(order) } },
  h('div', { class: 'section-title' }, h('h2', {}, title)), content);

function drawPerson() {
  const o = state.overview;
  const u = o.users.find((x) => x.username === state.person);
  if (!u) {
    state.person = null;
    state.draft = null;
    history.replaceState(null, '', '#people');
    toast('That account is not there any more', { bad: true });
    drawPeople();
    return;
  }
  const me = u.username === o.me;
  clear(view,
    h('div', { class: 'line', style: { marginBottom: '8px' } },
      h('button', { class: 'btn sm ghost', type: 'button', onclick: () => go('people', { person: null }) }, icon('back', 'sm'), 'Everyone')),
    h('div', { class: 'line top' }, avatar(u, 'lg'),
      h('div', { class: 'grow stack tight' }, h('h2', { class: 'name' }, nameOf(u)),
        h('div', { class: 'bits small muted' }, h('span', {}, `@${u.username}`), lastSeen(u), h('span', {}, stored(u))),
        h('div', { class: 'chips' }, tagsFor(u)))),
    h('div', { class: 'cols' },
      h('div', { class: 'col' }, roleBlock(u, me), appBlock(u), modelBlock(u)),
      h('div', { class: 'col' }, abilitiesBlock(u), signInBlock(u, me), me ? null : removeBlock(u))));
}

function roleBlock(u, me) {
  const o = state.overview;
  const d = draftFor(u);
  const pairs = o.roles.map((r) => [r.name, r.label]);
  if (!roleNamed(u.role)) pairs.unshift([u.role, u.role_label || u.role]);
  const pick = select(pairs, d.role || u.role, { 'aria-label': 'Role', dataset: { focus: 'role' } });
  const about = h('p', { class: 'small muted' });
  const save = h('button', { class: 'btn primary', type: 'button', dataset: { focus: 'role-save' } }, 'Save');
  const sync = () => {
    const picked = roleNamed(pick.value);
    about.textContent = picked ? picked.description : '';
    d.role = pick.value === u.role ? null : pick.value;
    save.disabled = !d.role;
  };
  pick.addEventListener('change', sync);
  save.addEventListener('click', async () => {
    const to = pick.value;
    const picked = roleNamed(to);
    const who = nameOf(u);
    if (to === 'admin' && !await confirmSheet(`${who} will be able to do everything: use the server itself, open this page and change every account, yours included.`,
      { title: `Make ${who} an administrator?`, ok: 'Make administrator' })) return;
    if (me && u.is_admin && to !== 'admin' && !await confirmSheet('You will stop being an administrator and this page will close for you. Only another administrator can give it back.',
      { title: 'Give up being an administrator?', ok: 'Change my role', danger: true })) return;
    save.disabled = true;
    try {
      await api(userPath(u.username), { method: 'PUT', body: { role: to } });
      d.role = null;
      if (me && to !== 'admin') { location.href = '/'; return; }
      toast(`${who} now has the role ${picked ? picked.label : to}`);
      await refresh();
    } catch (error) { oops(error); sync(); }
  });
  sync();
  return block(1, 'Role', h('div', { class: 'card stack' },
    h('div', { class: 'line' }, h('div', { class: 'grow' }, pick), save),
    about,
    u.is_admin ? h('p', { class: 'small muted' }, 'An administrator cannot be given another role while they are the only one.') : null));
}

function appBlock(u) {
  const o = state.overview;
  const d = draftFor(u).experience;
  const now = (key) => (key in d ? d[key] : u.experience[key]);
  const save = h('button', { class: 'btn primary', type: 'button', dataset: { focus: 'app-save' } }, 'Save');
  const undo = h('button', { class: 'btn ghost', type: 'button', onclick: () => { Object.keys(d).forEach((key) => delete d[key]); redraw(); } }, 'Undo changes');
  const note = h('span', { class: 'small muted grow' });
  const sync = () => {
    const dirty = Object.keys(d).length > 0;
    save.disabled = !dirty;
    undo.hidden = !dirty;
    note.textContent = dirty ? 'Not saved yet' : '';
  };
  // Only what differs from what they have now is kept, and only that is sent:
  // whatever is left alone goes on following their role.
  const set = (key, value, again = false) => {
    if (same(TEXT_KEYS.includes(key) ? cleaned(key, value) : value, u.experience[key])) delete d[key]; else d[key] = value;
    if (again) redraw(); else sync();
  };
  const line = (key, attrs) => {
    const input = h('input', { class: 'input', autocomplete: 'off', dataset: { focus: `app-${key}` }, ...attrs, value: now(key) || '' });
    input.addEventListener('input', () => set(key, input.value));
    return input;
  };
  const tone = h('textarea', { class: 'input', rows: '3', maxlength: '600', dataset: { focus: 'app-tone' },
    placeholder: 'Not technical. Keep it warm and short, one step at a time.' });
  tone.value = now('tone') || '';
  const count = h('span', {}, `${tone.value.length} / 600`);
  tone.addEventListener('input', () => { set('tone', tone.value); count.textContent = `${tone.value.length} / 600`; });

  const mode = now('mode');
  let extra;
  if (mode === 'simple') {
    const hue = now('accent') || '';
    const swatch = h('input', { class: 'swatch', type: 'color', 'aria-label': 'Colour', value: hue || '#6d4aff', dataset: { focus: 'app-accent' } });
    const hueText = h('span', { class: 'small muted grow' }, hue || 'The default colour');
    const plainHue = h('button', { class: 'btn sm ghost', type: 'button', onclick: () => set('accent', '', true) }, 'Use the default');
    plainHue.hidden = !hue;
    swatch.addEventListener('input', () => { set('accent', swatch.value); hueText.textContent = swatch.value; plainHue.hidden = false; });
    extra = [
      group('On their family screen, besides chat',
        ticks(o.sections, now('sections') || [], (ids) => set('sections', ids), { grid: true, key: 'app-section' })),
      field('Light or dark', select(o.themes.map((t) => [t, THEME_LABEL[t] || t]), now('theme'),
        { dataset: { focus: 'app-theme' }, onchange: (event) => set('theme', event.target.value) })),
      group('Colour', h('div', { class: 'line' }, swatch, hueText, plainHue)),
    ];
  } else {
    const hidden = now('hidden_ui') || [];
    const other = hidden.filter((id) => !o.hideable_ui.some((part) => part.id === id));
    extra = group('Hide these parts of the app for them',
      ticks(o.hideable_ui, hidden, (ids) => set('hidden_ui', [...ids, ...other]), { grid: true, key: 'app-hide' }),
      h('span', {}, 'This only tidies their screen. What they are able to do is set under “What they can do”.'));
  }

  save.addEventListener('click', async () => {
    const sent = { ...d };
    const body = {};
    Object.entries(sent).forEach(([key, value]) => { body[key] = TEXT_KEYS.includes(key) ? cleaned(key, value) : value; });
    save.disabled = true;
    try {
      await api(userPath(u.username), { method: 'PUT', body: { experience: body } });
      Object.keys(sent).forEach((key) => { if (d[key] === sent[key]) delete d[key]; });
      toast('Saved');
      await refresh();
    } catch (error) { oops(error); sync(); }
  });
  sync();

  const allow = mode !== 'simple' ? null : setting('May also open the full app',
    'Lets them switch to the full app when they want to. It comes without the server\'s controls: no models, engine or connections. Off: the family screen is all they see.',
    keyed(toggle(u.experience.allow_full_app, async (checked, input) => {
      try {
        await api(userPath(u.username), { method: 'PUT', body: { experience: { allow_full_app: checked } } });
        toast(checked ? `${nameOf(u)} may open the full app` : `${nameOf(u)} stays on the family screen`);
        await refresh();
      } catch (error) { input.checked = !checked; oops(error); }
    }, 'May also open the full app'), 'app-allow'));

  const role = roleNamed(u.role);
  const roleMode = role && role.experience ? role.experience.mode : null;
  const pinned = (u.experience_set || []).includes('mode') && !('mode' in d) && roleMode
    ? h('div', { class: 'small muted' }, 'Chosen for this person. ',
      h('button', { class: 'link', type: 'button', dataset: { focus: 'app-mode-role' }, onclick: async (event) => {
        const button = event.currentTarget;
        button.disabled = true;
        try {
          await api(userPath(u.username), { method: 'PUT', body: { experience: { mode: null } } });
          toast(`${nameOf(u)} gets what the role gives: ${MODE_LABEL[roleMode] || roleMode}`);
          await refresh();
        } catch (error) { button.disabled = false; oops(error); }
      } }, `Follow their role again (${MODE_LABEL[roleMode] || roleMode})`))
    : null;

  return block(2, 'Their app', h('div', { class: 'card flush rows' },
    h('div', { class: 'stack pad' },
      choices('app-mode', MODES, mode, (value) => set('mode', value, true)),
      pinned,
      field('Name shown', line('display_name', { maxlength: '40', placeholder: titled(u.username) }), 'What they are called on screen and by the assistant.'),
      field('What the assistant is called', line('assistant_name', { maxlength: '30', placeholder: 'Aegis' })),
      field('How should it talk to them?', tone, ['The assistant keeps this in mind in every chat, so write nothing they should not read. ', count]),
      extra,
      h('div', { class: 'line wrap' }, note, undo, save)),
    allow));
}

const sayValue = (p, value) => (p.kind === 'switch' ? (value ? 'on' : 'off') : (Number(value) ? `${value} a day` : 'no limit'));

function abilitiesBlock(u) {
  const o = state.overview;
  if (u.is_admin) {
    return block(3, 'What they can do', h('div', { class: 'card notice' }, icon('shield'),
      h('div', {}, h('div', {}, 'Administrators can do everything.'),
        h('div', { class: 'small muted' }, 'That includes the server itself, its files and other computers. To limit this person, give them another role.'))));
  }
  const role = roleNamed(u.role);
  const own = u.privilege_overrides || {};
  const rows = o.privileges.filter((p) => !p.admin_only).map((p) => abilityRow(u, role, p, p.key in own));
  const groups = ((role && role.tools) || []).map((id) => o.tool_groups.find((g) => g.id === id)).filter(Boolean);
  rows.push(h('div', { class: 'row' }, h('div', { class: 'main' },
    h('div', {}, 'What the assistant may do for them'),
    groups.length ? h('ul', { class: 'small muted plain' }, groups.map((g) => h('li', {}, g.label)))
      : h('div', { class: 'small muted' }, 'It does not keep lists, remember things or use a calendar for them.'),
    h('div', { class: 'small muted' }, `This comes with their role${role ? `, ${role.label}` : ''}. `,
      h('button', { class: 'link', type: 'button', onclick: () => openRole(u.role) }, 'Change this in Roles')))));
  return block(3, 'What they can do', h('div', { class: 'card flush rows' }, rows));
}

function abilityRow(u, role, p, own) {
  const value = u.privileges[p.key];
  const fromRole = role && role.privileges ? role.privileges[p.key] : undefined;
  const send = (next) => api(userPath(u.username), { method: 'PUT', body: { privileges: { [p.key]: next } } });
  const mark = !own ? null : h('div', { class: 'meta' }, h('span', { class: 'chip accent' }, 'set for this person'),
    fromRole === undefined ? null : h('button', { class: 'link', type: 'button', dataset: { focus: `role-${p.key}` }, onclick: async (event) => {
      const button = event.currentTarget;
      button.disabled = true;
      try {
        await send(fromRole);
        delete draftFor(u).numbers[p.key];
        toast(`${p.label}: back to the role’s setting`);
        await refresh();
      } catch (error) { button.disabled = false; oops(error); }
    } }, `Use the role’s setting (${sayValue(p, fromRole)})`));

  if (p.kind === 'switch') {
    return setting(p.label, p.description, keyed(toggle(value === true, async (checked, input) => {
      try {
        await send(checked);
        toast(`${p.label}: ${checked ? 'on' : 'off'}`);
        await refresh();
      } catch (error) { input.checked = !checked; oops(error); }
    }, p.label), `can-${p.key}`), mark);
  }
  if (p.kind !== 'number') return null;

  // A number is typed, so it waits for Save (or Enter) rather than acting at once.
  const numbers = draftFor(u).numbers;
  const saved = Number(value) || 0;
  const input = h('input', { class: 'input num', type: 'number', min: '0', step: '1', inputmode: 'numeric', 'aria-label': p.label,
    dataset: { focus: `can-${p.key}` }, value: p.key in numbers ? numbers[p.key] : String(saved) });
  const save = h('button', { class: 'btn sm', type: 'button' }, 'Save');
  const read = () => (/^\d{1,9}$/.test(input.value.trim()) ? Number(input.value.trim()) : null);
  const sync = () => {
    const n = read();
    if (n === saved) delete numbers[p.key]; else numbers[p.key] = input.value;
    save.disabled = n === null || n === saved;
  };
  const commit = async () => {
    const n = read();
    if (n === null) { toast('Type a whole number, 0 or more', { bad: true }); return; }
    if (n === saved) return;
    save.disabled = true;
    try {
      await send(n);
      delete numbers[p.key];
      toast(`${p.label}: ${sayValue(p, n)}`);
      await refresh();
    } catch (error) { oops(error); sync(); }
  };
  input.addEventListener('input', sync);
  input.addEventListener('keydown', (event) => { if (event.key === 'Enter') commit(); });
  save.addEventListener('click', commit);
  sync();
  return setting(p.label, p.key === 'max_messages_per_day' ? '0 = no limit' : p.description, h('div', { class: 'line' }, input, save), mark);
}

function signInBlock(u, me) {
  const rules = passwordRules();
  const who = nameOf(u);
  const flip = (body, message) => async (checked, input) => {
    try {
      await api(userPath(u.username), { method: 'PUT', body: body(checked) });
      message(checked);
      await refresh();
    } catch (error) { input.checked = !checked; oops(error); }
  };
  const again = () => { const fresh = state.overview.users.find((x) => x.username === u.username); if (fresh) newPassword(fresh, me); };
  const seen = u.last_sign_in ? when(u.last_sign_in, { time: true }) : lastSeen(u);
  return block(4, 'Sign-in', h('div', { class: 'card flush rows' },
    h('div', { class: 'row acts' },
      h('div', { class: 'main' }, h('div', {}, u.allow_pin ? 'Password or PIN' : 'Password'),
        h('div', { class: 'small muted' }, 'Nobody can see the one in use. You can only set a new one.')),
      h('button', { class: 'btn sm', type: 'button', onclick: () => newPassword(u, me) }, icon('lock', 'sm'), u.allow_pin ? 'Set a new password / PIN' : 'Set a new password')),
    setting('Allow a short PIN', `${rules.pin} instead of a password. ${rules.lock} This decides what can be set from now on; it does not change the one they have.`,
      keyed(toggle(u.allow_pin, flip((on) => ({ allow_pin: on }), (on) => toast(
        on ? 'A PIN is allowed now. Set one to start using it.' : 'No new PINs. One they already have works until a new password is set.',
        { action: { label: on ? 'Set a PIN' : 'Set a password', run: again } })), 'Allow a short PIN'), 'pin')),
    h('div', { class: 'row acts' },
      h('div', { class: 'main' }, h('div', {}, 'Signed-in devices'),
        h('div', { class: 'small muted' }, me ? 'Ends your sessions on every other device. You stay signed in here.'
          : `Ends every session on all their devices. They sign in again with their password${u.allow_pin ? ' or PIN' : ''}.`)),
      h('button', { class: 'btn sm', type: 'button', onclick: () => signOutEverywhere(u, me) }, icon('logout', 'sm'), 'Sign out everywhere')),
    me ? null : setting('Account switched off', 'They cannot sign in and are signed out everywhere. Nothing they stored is deleted.',
      keyed(toggle(u.disabled, flip((on) => ({ disabled: on }), (on) => toast(on ? `${who}’s account is switched off` : `${who}’s account is back on`)),
        'Account switched off'), 'off')),
    h('div', { class: 'row' }, h('div', { class: 'main stack tight' },
      h('dl', { class: 'kv' },
        h('dt', {}, 'Created'), h('dd', {}, u.created ? when(u.created) : 'Not recorded'),
        h('dt', {}, 'Last sign-in'), h('dd', {}, seen),
        h('dt', {}, 'Sign-ins'), h('dd', {}, String(u.sign_ins || 0)),
        h('dt', {}, '2-step sign-in'), h('dd', {}, u.two_factor ? 'On' : 'Off')),
      h('div', {}, h('button', { class: 'link small', type: 'button',
        onclick: () => go('activity', { activity: { ...state.activity, user: u.username, limit: 200 } }) }, me ? 'See your activity' : 'See their activity'))))));
}

function newPassword(u, me) {
  const rules = passwordRules();
  const who = nameOf(u);
  const secret = secretBox();
  const secretField = field('New password', secret.node, rules.passwordHint);
  const kinds = [['PIN', true], ['Password', false]].map(([label, on]) => h('button', { class: 'chip', type: 'button', onclick: () => use(on) }, label));
  let pin = false;
  const s = sheet({
    title: `New password${u.allow_pin ? ' or PIN' : ''} for ${me ? 'yourself' : who}`,
    body: h('div', { class: 'stack' },
      u.allow_pin ? h('div', { class: 'chips' }, kinds) : null,
      secretField,
      h('p', { class: 'small muted' }, me ? 'You will be signed out everywhere, here too, and sign in again with the new one.'
        : 'They are signed out everywhere and sign in again with the new one. Tell them what it is: it cannot be looked up later.')),
    actions: [{ label: 'Cancel', kind: 'ghost' }, { label: 'Set password', kind: 'primary', run: async () => {
      if (!secret.input.value) { secret.input.focus(); return false; }
      await api(`${userPath(u.username)}/password`, { method: 'POST', body: { password: secret.input.value } });
      if (me) { location.href = '/login'; return true; }
      toast(`New ${pin ? 'PIN' : 'password'} set for ${who}`);
      await refresh();
      return true;
    } }],
  });
  const submit = s.panel.querySelector('.actions .btn.primary');
  function use(on) {
    pin = on;
    secret.digits(on);
    kinds[0].classList.toggle('on', on);
    kinds[1].classList.toggle('on', !on);
    secretField.firstElementChild.textContent = on ? 'New PIN' : 'New password';
    secretField.lastElementChild.textContent = on ? rules.pinHint : rules.passwordHint;
    submit.textContent = on ? 'Set PIN' : 'Set password';
  }
  use(!!u.allow_pin);
  submitOnEnter(s.panel);
}

async function signOutEverywhere(u, me) {
  const who = nameOf(u);
  if (!me && !await confirmSheet(`${who} will have to sign in again on every device.`, { title: `Sign ${who} out everywhere?`, ok: 'Sign out' })) return;
  try {
    const r = await api(`${userPath(u.username)}/sign-out`, { method: 'POST' });
    if (me) toast(r.ended ? `Signed out everywhere else — ${plural(r.ended, 'session')} ended` : 'You are not signed in anywhere else');
    else toast(r.ended ? `Signed out everywhere — ${plural(r.ended, 'session')} ended` : `${who} was not signed in anywhere`);
    await refresh();
  } catch (error) { oops(error); }
}

// ── the default model ──────────────────────────────────────────────────────

// {items: [{endpoint_id, endpoint_name, models: […]}]}, each model a string or an object with an id.
function modelChoices(data) {
  const found = [];
  ((data && data.items) || []).forEach((item) => {
    if (!item || !item.endpoint_id || (item.model_type || 'llm') !== 'llm') return;       // not the image generators
    const names = item.models_display || [];
    (item.models || []).forEach((model, i) => {
      const id = typeof model === 'string' ? model : (model && model.id);
      if (!id) return;
      found.push({ endpoint_id: String(item.endpoint_id), endpoint: item.endpoint_name || 'Models', model: String(id),
        label: (model && model.name) || names[i] || String(id) });
    });
  });
  return found;
}

function loadModels() {
  if (state.models && !state.models.failed) return;
  state.models = { loading: true };
  api('/api/models')
    .then((data) => { state.models = { list: modelChoices(data) }; })
    .catch(() => { state.models = { failed: true }; })
    .then(() => { if (state.tab === 'people' && state.person && state.overview && !rendering) redraw(); });
}

function modelBlock(u) {
  const d = draftFor(u);
  const current = u.default_model || {};
  const models = state.models || { loading: true };
  const content = [
    h('p', { class: 'small muted' }, 'The model their chats start with. The shared default is the one set for everyone.'),
    h('p', {}, h('span', { class: 'small muted' }, 'Now: '), current.model ? h('span', { class: 'mono' }, current.model) : 'Follows the shared default'),
  ];
  if (models.loading) content.push(h('p', { class: 'small muted' }, 'Fetching the list of models…'));
  else if (models.failed) content.push(h('p', { class: 'small muted' }, 'The list of models could not be loaded, so this cannot be changed right now.'));
  else {
    const list = models.list;
    const mine = list.findIndex((c) => c.endpoint_id === String(current.endpoint_id || '') && c.model === current.model);
    const saved = !current.model ? '' : (mine < 0 ? 'gone' : String(mine));
    const pick = h('select', { class: 'input', 'aria-label': 'Default model', dataset: { focus: 'model' } }, h('option', { value: '' }, 'Follow the shared default'));
    if (saved === 'gone') pick.append(h('option', { value: 'gone' }, `${current.model} (not in the list right now)`));
    const byEndpoint = new Map();
    list.forEach((c, i) => {
      if (!byEndpoint.has(c.endpoint_id)) byEndpoint.set(c.endpoint_id, h('optgroup', { label: c.endpoint }));
      byEndpoint.get(c.endpoint_id).append(h('option', { value: String(i) }, c.label));
    });
    add(pick, [...byEndpoint.values()]);
    pick.value = d.model !== null ? d.model : saved;
    if (pick.selectedIndex < 0) pick.value = saved;
    const save = h('button', { class: 'btn primary', type: 'button', dataset: { focus: 'model-save' } }, 'Save');
    const sync = () => { d.model = pick.value === saved ? null : pick.value; save.disabled = d.model === null; };
    pick.addEventListener('change', sync);
    save.addEventListener('click', async () => {
      const choice = pick.value === '' ? null : list[Number(pick.value)];
      if (pick.value !== '' && !choice) return;
      save.disabled = true;
      try {
        await api(userPath(u.username), { method: 'PUT', body: { default_model: choice ? { endpoint_id: choice.endpoint_id, model: choice.model } : {} } });
        d.model = null;
        toast(choice ? `Default model: ${choice.label}` : 'Follows the shared default');
        await refresh();
      } catch (error) { oops(error); sync(); }
    });
    sync();
    content.push(h('div', { class: 'line' }, h('div', { class: 'grow' }, pick), save));
    if (!list.length) content.push(h('p', { class: 'small muted' }, 'No models are listed right now. If the model server is still starting, reload this page in a minute.'));
  }
  return block(5, 'Default model', h('div', { class: 'card stack' }, content));
}

// ── removing an account ────────────────────────────────────────────────────

function removeBlock(u) {
  return block(6, 'Remove', h('div', { class: 'card stack danger-zone' },
    h('p', { class: 'small muted' }, `Deletes the account: ${nameOf(u)} can no longer sign in and is signed out everywhere. You choose whether what they stored goes too. To stop them for a while, switch the account off instead.`),
    h('div', {}, h('button', { class: 'btn danger', type: 'button', onclick: () => removePerson(u) }, icon('trash', 'sm'), 'Delete this account'))));
}

function removePerson(u) {
  const who = nameOf(u);
  const input = h('input', { class: 'input', placeholder: u.username, autocomplete: 'off', autocapitalize: 'none', autocorrect: 'off', spellcheck: 'false' });
  const erase = h('input', { type: 'checkbox' });
  const note = h('span', { class: 'tiny muted hint' });
  const say = () => {
    note.textContent = erase.checked
      ? 'Their chats, memory, lists, reminders, trackers, boards, notes and files are erased now. This cannot be undone.'
      : 'It stays on the server where nobody can reach it, listed under Deleted accounts on the People page. You can erase it there later, and nobody new can take this username until you do.';
  };
  erase.addEventListener('change', say);
  say();
  const s = sheet({
    title: `Delete ${who}’s account?`,
    body: h('div', { class: 'stack' },
      h('p', { class: 'muted' }, `${who} has ${stored(u)}.`),
      h('label', { class: 'check' }, erase, h('span', {}, 'Also erase everything they stored', h('br'), note)),
      field(`To confirm, type their username: ${u.username}`, input)),
    actions: [{ label: 'Cancel', kind: 'ghost' }, { label: 'Delete account', kind: 'danger solid', run: async () => {
      if (input.value.trim().toLowerCase() !== u.username) { toast(`Type ${u.username} to confirm`, { bad: true }); input.focus(); return false; }
      const r = await api(`${userPath(u.username)}?data=${erase.checked ? 'erase' : 'keep'}`, { method: 'DELETE' });
      if (erase.checked && !r.name_free) toast(`${who}’s account is deleted, but not everything could be erased. See Deleted accounts.`, { bad: true, ms: 7000 });
      else if (erase.checked) toast(`${who}’s account and everything they stored are deleted`);
      else toast(r.name_free ? `${who}’s account is deleted` : `${who}’s account is deleted. What they stored is under Deleted accounts.`, { ms: 6000 });
      state.draft = null;
      go('people', { person: null });
      return true;
    } }],
  });
  s.panel.classList.add('form');
  submitOnEnter(s.panel);
}

// ── what deleted accounts left behind ──────────────────────────────────────

function leftText(left) {
  if (!left) return 'Could not be counted just now';
  const parts = [
    left.chats ? plural(left.chats, 'chat') : '', left.memories ? plural(left.memories, 'memory', 'memories') : '',
    left.notes ? plural(left.notes, 'note or list', 'notes and lists') : '', left.files ? plural(left.files, 'file') : '',
    left.other ? plural(left.other, 'other item') : '',
  ].filter(Boolean);
  return parts.length ? parts.join(' · ') : 'Nothing that can be counted';
}

function removedCard() {
  const removed = state.overview.removed || [];
  if (!removed.length) return null;
  return h('section', { class: 'block', style: { marginTop: '22px' } },
    h('div', { class: 'section-title' }, h('h2', {}, 'Deleted accounts')),
    h('p', { class: 'small muted', style: { marginBottom: '10px' } },
      'These accounts are gone, but what they stored is still on the server. Nobody can reach it, and nobody new can take the username until you erase it or free the name.'),
    h('div', { class: 'card flush rows' }, removed.map((r) => h('div', { class: 'row acts' },
      h('div', { class: 'main' },
        h('div', {}, `@${r.username}`),
        h('div', { class: 'small muted' }, [r.at ? `Deleted ${when(r.at).replace(/^(Today|Yesterday)/, (word) => word.toLowerCase())}` : 'Deleted',
          r.by ? `by ${r.by}` : ''].filter(Boolean).join(' ')),
        h('div', { class: 'small muted' }, leftText(r.left)),
        (r.erase_failed || []).length ? h('div', { class: 'small', style: { color: 'var(--danger)' } },
          `Erasing was tried and could not finish (${r.erase_failed.join(', ')}). Try again.`) : null),
      h('div', { class: 'line wrap' },
        h('button', { class: 'btn sm danger', type: 'button', onclick: () => eraseLeft(r) }, icon('trash', 'sm'), 'Erase what they stored'),
        h('button', { class: 'btn sm', type: 'button', onclick: () => freeName(r) }, 'Free the name'))))));
}

async function eraseLeft(r) {
  if (!await confirmSheet(`Everything @${r.username} stored is erased for good: ${leftText(r.left)}. The username becomes free again.`,
    { title: `Erase what @${r.username} stored?`, ok: 'Erase', danger: true })) return;
  try {
    const done = await api(`/api/admin/removed/${encodeURIComponent(r.username)}/erase`, { method: 'POST' });
    if (done.name_free) toast(`Everything @${r.username} stored is erased`);
    else toast(`Not everything could be erased (${(done.failed || []).join(', ') || 'some of it'}). Try again.`, { bad: true, ms: 7000 });
    await refresh();
  } catch (error) { oops(error); }
}

async function freeName(r) {
  if (!await confirmSheet(`Nothing is erased. The next account you create with the username “${r.username}” gets everything listed here: ${leftText(r.left)}. Do this only to give the same person their account back.`,
    { title: `Free the name “${r.username}”?`, ok: 'Free the name' })) return;
  try {
    await api(`/api/admin/removed/${encodeURIComponent(r.username)}`, { method: 'DELETE' });
    toast(`“${r.username}” can be used again. A new account with that name gets the old data.`, { ms: 6000 });
    await refresh();
  } catch (error) { oops(error); }
}

// ── roles ──────────────────────────────────────────────────────────────────

function drawRoles() {
  const o = state.overview;
  clear(view,
    h('div', { class: 'line wrap', style: { marginBottom: '10px' } },
      h('p', { class: 'small muted grow' }, 'A role is a starting point: what people with it can do, and which app they get. You can still change things for one person on their own page.'),
      h('button', { class: 'btn primary', type: 'button', onclick: newRole }, icon('plus', 'sm'), 'New role')),
    h('div', { class: 'card soft notice', style: { marginBottom: '10px' } }, icon('lock'), h('span', {}, SERVER_NOTE)),
    h('div', { class: 'roles' }, o.roles.map(roleCard)));
}

function roleCard(role) {
  const o = state.overview;
  const fixed = role.locked || role.name === 'admin';
  const mode = (role.experience || {}).mode === 'simple' ? 'simple' : 'full';
  const groups = (role.tools || []).map((id) => o.tool_groups.find((g) => g.id === id)).filter(Boolean);
  const kind = !role.builtin ? h('span', { class: 'chip' }, 'your own')
    : (role.customized ? h('span', { class: 'chip warn' }, 'changed from the defaults') : null);
  return h('div', { class: 'card stack' },
    h('div', { class: 'line wrap' }, h('h3', { class: 'grow' }, role.label), kind,
      h('span', { class: 'small muted' }, plural(role.in_use || 0, 'person', 'people'))),
    role.description ? h('p', { class: 'small muted' }, role.description) : null,
    h('div', { class: 'chips' }, h('span', { class: 'chip accent' }, MODE_LABEL[mode]), groups.map((g) => h('span', { class: 'chip' }, g.label))),
    fixed ? h('p', { class: 'small muted' }, 'Everything. Cannot be changed.')
      : h('div', { class: 'line wrap' },
        h('button', { class: 'btn sm', type: 'button', onclick: () => editRole(role) }, icon('edit', 'sm'), 'Edit'),
        role.builtin && role.customized ? h('button', { class: 'btn sm', type: 'button', onclick: () => resetRole(role) }, icon('undo', 'sm'), 'Back to defaults') : null,
        role.builtin ? null : h('button', { class: 'btn sm danger', type: 'button', onclick: () => deleteRole(role) }, icon('trash', 'sm'), 'Delete')));
}

async function openRole(name) {
  await go('roles');
  const role = state.tab === 'roles' && state.overview ? roleNamed(name) : null;
  if (role && !role.locked && role.name !== 'admin') editRole(role);
}

function newRole() {
  const o = state.overview;
  const input = h('input', { class: 'input', maxlength: '32', autocomplete: 'off', autocapitalize: 'none', autocorrect: 'off', spellcheck: 'false', placeholder: 'e.g. kids' });
  const from = select(o.roles.filter((r) => !r.locked && r.name !== 'admin').map((r) => [r.name, r.label]), 'member');
  const s = sheet({
    title: 'New role',
    body: h('div', { class: 'stack' },
      field('Name', input, 'Lowercase letters, digits and dashes: 2 to 32 of them, starting with a letter. It cannot be changed later.'),
      field('Start as a copy of', from, 'Everything about it can be changed in the next step.')),
    actions: [{ label: 'Cancel', kind: 'ghost' }, { label: 'Next', kind: 'primary', run: () => {
      const name = input.value.trim().toLowerCase().replace(/\s+/g, '-');
      if (!/^[a-z][a-z0-9_-]{1,31}$/.test(name)) { toast('Use 2 to 32 lowercase letters, digits or dashes, starting with a letter', { bad: true }); input.focus(); return false; }
      if (roleNamed(name)) { toast('There is already a role with that name', { bad: true }); input.focus(); return false; }
      const words = name.replace(/[-_]+/g, ' ');
      editRole(roleNamed(from.value) || {}, { name, label: words.charAt(0).toUpperCase() + words.slice(1) });
      return true;
    } }],
  });
  submitOnEnter(s.panel);
}

// The editor for a role. With `fresh` it makes a new one, starting as a copy of `role`.
function editRole(role, fresh = null) {
  const o = state.overview;
  const name = fresh ? fresh.name : role.name;
  const label = h('input', { class: 'input', maxlength: '40', autocomplete: 'off', value: fresh ? fresh.label : (role.label || name) });
  const description = h('textarea', { class: 'input', rows: '2', maxlength: '300', placeholder: 'Who is this role for?' });
  description.value = fresh ? '' : (role.description || '');
  // Everything the role holds is sent back, also what has no control here, so
  // saving never quietly drops a setting. Server access is never part of it.
  const can = { ...(role.privileges || {}) };
  o.privileges.filter((p) => p.admin_only).forEach((p) => { delete can[p.key]; });
  const numbers = {};
  const rows = o.privileges.filter((p) => !p.admin_only).map((p) => {
    if (p.kind === 'switch') return setting(p.label, p.description, toggle(can[p.key] === true, (on) => { can[p.key] = on; }, p.label));
    if (p.kind !== 'number') return null;
    numbers[p.key] = h('input', { class: 'input num', type: 'number', min: '0', step: '1', inputmode: 'numeric', 'aria-label': p.label, value: String(Number(can[p.key]) || 0) });
    return setting(p.label, p.key === 'max_messages_per_day' ? '0 = no limit' : p.description, numbers[p.key]);
  });
  const before = role.experience || {};
  let tools = [...(role.tools || [])];
  let mode = before.mode === 'simple' ? 'simple' : 'full';
  let sections = [...(before.sections || o.sections.map((x) => x.id))];
  const s = sheet({
    title: fresh ? `New role “${name}”` : `Edit “${role.label || name}”`,
    body: h('div', { class: 'stack' },
      field('Name shown', label),
      field('Description', description),
      h('h3', {}, 'What people with this role can do'),
      h('div', { class: 'card flush rows' }, rows),
      h('h3', {}, 'What the assistant may do for them'),
      ticks(o.tool_groups, tools, (ids) => { tools = ids; }),
      h('h3', {}, 'The app they start with'),
      choices('role-mode', MODES, mode, (value) => { mode = value; }),
      group('On the simple home screen, besides chat', ticks(o.sections, sections, (ids) => { sections = ids; }, { grid: true })),
      h('p', { class: 'small muted' }, 'Both can be changed for one person on their own page.'),
      h('div', { class: 'card soft notice' }, icon('lock'), h('span', {}, SERVER_NOTE))),
    actions: [{ label: 'Cancel', kind: 'ghost' }, { label: fresh ? 'Create role' : 'Save', kind: 'primary', run: async () => {
      for (const [key, input] of Object.entries(numbers)) {
        if (!/^\d{1,9}$/.test(input.value.trim())) { toast('Type a whole number, 0 or more', { bad: true }); input.focus(); return false; }
        can[key] = Number(input.value.trim());
      }
      const experience = { mode, sections };
      if (!fresh) ['theme', 'allow_full_app'].forEach((key) => { if (key in before) experience[key] = before[key]; });
      await api(`/api/admin/roles/${encodeURIComponent(name)}`, { method: 'PUT', body: {
        label: tidy(label.value) || name, description: tidy(description.value), privileges: can, tools, experience,
      } });
      toast(fresh ? `Role “${tidy(label.value) || name}” created` : 'Role saved');
      await refresh();
      return true;
    } }],
  });
  s.panel.classList.add('form');
  // The form is mostly switches: on a touch screen, keep the sheet from putting
  // the keyboard over them by focusing the name box as it opens.
  if (window.matchMedia('(pointer: coarse)').matches) { label.disabled = true; setTimeout(() => { label.disabled = false; }, 90); }
}

async function resetRole(role) {
  if (!await confirmSheet(`“${role.label}” goes back to its built-in settings, for everyone who has it.`, { title: 'Back to defaults?', ok: 'Back to defaults' })) return;
  try {
    await api(`/api/admin/roles/${encodeURIComponent(role.name)}`, { method: 'DELETE' });
    toast(`“${role.label}” is back to its defaults`);
    await refresh();
  } catch (error) { oops(error); }
}

async function deleteRole(role) {
  if (!await confirmSheet(`The role “${role.label}” will be deleted.`, { title: 'Delete this role?', ok: 'Delete', danger: true })) return;
  try {
    await api(`/api/admin/roles/${encodeURIComponent(role.name)}`, { method: 'DELETE' });
    toast(`Role “${role.label}” deleted`);
    await refresh();
  } catch (error) { oops(error); }
}

// ── household ──────────────────────────────────────────────────────────────
// Set once, used by everyone: where home is, how things are measured, the
// language read here, and which outside sources may be asked.

async function loadHousehold() {
  await loadOverview();
  state.household = await api('/api/admin/household');
}

async function saveHousehold(change, said) {
  try {
    state.household = await api('/api/admin/household', { method: 'PUT', body: change });
    if (said) toast(said);
  } catch (error) { oops(error); }
  if (state.tab === 'household') redraw();
}

function drawHousehold() {
  const hh = state.household;
  const town = h('input', { class: 'input', maxlength: '120', autocapitalize: 'words', autocomplete: 'off', dataset: { focus: 'hh-home' },
    placeholder: hh.home ? 'Change to another town' : 'Town, with its state or country', 'aria-label': 'Home town' });
  const setHome = () => {
    const place = town.value.trim();
    if (!place) { toast('Type the town first', { bad: true }); town.focus(); return; }
    saveHousehold({ home: place }, 'Home saved');
  };
  town.addEventListener('keydown', (event) => { if (event.key === 'Enter' && !event.isComposing) setHome(); });
  const today = h('div', { class: 'small muted' });
  if (hh.home && hh.live.weather) {
    add(today, 'Checking the forecast there…');
    api('/api/almanac/weather', { method: 'POST', body: { place: `${hh.home.lat}, ${hh.home.lon}`, when: 'today' } }).then((found) => {
      const card = found.card;
      if (!card || !card.tiles) { clear(today, found.text || 'No forecast just now.'); return; }
      const tile = (name) => (card.tiles.find((t) => t.label === name) || {}).value;
      clear(today, `Today there: ${[card.summary, tile('High') ? `high ${tile('High')}` : '', tile('Low') ? `low ${tile('Low')}` : ''].filter(Boolean).join(', ')}. ${card.source || ''}`);
    }).catch(() => clear(today, 'The forecast could not be checked just now.'));
  }
  const languages = (hh.language_choices || []).map((row) => [row.code, row.name]);
  if (!languages.some(([code]) => code === hh.language)) languages.unshift([hh.language, hh.language_name || hh.language]);
  clear(view,
    h('p', { class: 'small muted', style: { marginBottom: '10px' } }, 'Set once, used by everyone here. Nobody is asked where they are or which units they want again.'),
    h('div', { class: 'stack' },
      h('div', { class: 'card stack' },
        h('h3', {}, 'Home'),
        h('p', { class: 'small muted' }, 'Where “here” is — for sunset, the forecast and anything else that needs a place. A town is enough. A person can still save a different home of their own on the Almanac page.'),
        hh.home ? h('div', { class: 'row' },
          h('div', { class: 'main' }, h('div', {}, hh.home.label), h('div', { class: 'small muted' }, [hh.home.zone, hh.home.decimal].filter(Boolean).join(' · ')), today),
          h('button', { class: 'btn sm', type: 'button', onclick: async () => {
            if (await confirmSheet('Sunset, the forecast and “near here” will ask for a place again.', { title: 'Remove the home position?', ok: 'Remove', danger: true })) saveHousehold({ home: '' }, 'Home removed');
          } }, 'Remove')) : null,
        h('div', { class: 'line' }, h('div', { class: 'grow' }, town), h('button', { class: 'btn primary', type: 'button', onclick: setHome }, hh.home ? 'Change' : 'Save'))),
      h('div', { class: 'card stack' },
        h('h3', {}, 'Units'),
        h('p', { class: 'small muted' }, 'How temperatures, distances and amounts are given when nobody asks for something else.'),
        choices('hh-units', hh.unit_choices.map((u) => [u.id, u.label, u.words]), hh.units, (value) => saveHousehold({ units: value }, 'Units saved'))),
      h('div', { class: 'card stack' },
        h('h3', {}, 'Language'),
        h('p', { class: 'small muted' }, 'The language read here. A sign, label or page photographed in any other language is translated into it.'),
        select(languages, hh.language, { 'aria-label': 'Household language', dataset: { focus: 'hh-language' }, style: { maxWidth: '320px' },
          onchange: (event) => saveHousehold({ language: event.target.value }, 'Language saved') })),
      h('div', { class: 'card stack' },
        h('h3', {}, 'Live information'),
        h('p', { class: 'small muted' }, 'Outside services Aegis may ask, each with its own switch. Everything else here works with no internet at all.'),
        h('div', { class: 'rows' }, (hh.sources || []).map((source) => setting(source.label,
          `${source.about} From ${source.who}. ${source.covers}.`,
          keyed(toggle(source.on, (on) => saveHousehold({ live: { [source.id]: on } }, `${source.label} ${on ? 'on' : 'off'}`), source.label), `hh-live-${source.id}`)))),
        h('div', { class: 'card soft notice' }, icon('lock'), h('span', {}, `What is sent: ${(hh.sources && hh.sources[0] && hh.sources[0].sends) || 'only the position, rounded.'} When the internet is down, or a switch is off, the answer says so and nothing is guessed.`)),
        h('p', { class: 'small muted' }, 'Services you run yourself — Home Assistant, a feed reader, anything with an API — are connected in the full app under Settings, Integrations. The assistant can then read and control them when an administrator asks.'))));
}

// ── the daily review ───────────────────────────────────────────────────────
// Each night the house's own model rates the day's chats. This page turns it on,
// says whose chats are read, and shows what it found. The words of a chat are
// shown only for the administrator's own chats; the server sends nobody else's.

const PROBLEM = {
  from_memory: 'From memory', unsupported: 'Unsupported facts', wrong_tool: 'Wrong tool', tool_failed: 'Tool failure passed over',
  not_done: 'Not done', claimed_done: 'Said done, was not', misread: 'Misread', refused: 'Refused or hedged', too_long: 'Too long',
  unclear: 'Unclear', asked_back: 'Asked instead of looking', had_to_repeat: 'Had to repeat', stopped: 'Stopped', redone: 'Asked again',
  slow: 'Slow', no_reply: 'No reply',
};
const KIND = { setting: 'A setting', assistant: 'The assistant', tool: 'A tool', speed: 'Speed' };
const NOTIFY = [['problems', 'Only for problems'], ['always', 'Every day'], ['never', 'Never']];
let reviewPoll = 0;

async function loadReview() {
  await loadOverview();
  state.review = await api('/api/admin/review');
  watchReview();
}

// While a review is running, ask again every few seconds so the page follows it.
function watchReview() {
  clearTimeout(reviewPoll);
  if (!state.review || !state.review.status.running) return;
  reviewPoll = setTimeout(async () => {
    if (state.tab !== 'review') return;
    try { state.review = await api('/api/admin/review'); } catch (_) { /* the next look may work */ }
    if (state.tab === 'review' && !rendering) redraw();
    watchReview();
  }, 3000);
}

async function saveReview(change, said) {
  try {
    state.review = await api('/api/admin/review', { method: 'PUT', body: change });
    if (said) toast(said);
  } catch (error) { oops(error); }
  if (state.tab === 'review') redraw();
}

async function reviewNow(day) {
  try {
    await api('/api/admin/review/run', { method: 'POST', body: { day } });
    toast('Started. It takes a few minutes.');
    state.review = await api('/api/admin/review');
  } catch (error) { oops(error); }
  if (state.tab === 'review') redraw();
  watchReview();
}

async function openReview(day) {
  try { state.review.review = await api(`/api/admin/review/${day}`); } catch (error) { oops(error); return; }
  if (state.tab === 'review') { redraw(); document.getElementById('review-day')?.scrollIntoView({ behavior: 'smooth', block: 'start' }); }
}

const dayWords = (day) => new Date(`${day}T12:00:00`).toLocaleDateString(undefined, { weekday: 'long', month: 'long', day: 'numeric' });
const scoreWords = (score) => (score === null || score === undefined ? 'Not rated' : `${Number(score).toFixed(1)} out of 5`);
const ratingClass = (rating) => (rating >= 4 ? 'ok' : rating === 3 ? 'warn' : rating ? 'danger' : '');
const problemChips = (problems, codes) => Object.entries(problems || {}).map(([code, count]) =>
  h('span', { class: 'chip', title: codes[code] || '' }, `${PROBLEM[code] || code}${count > 1 ? ` × ${count}` : ''}`));

// Times are shown on the house's clock, which is the one the review keeps — not the clock of the device in hand.
const houseClock = (rv, options) => (rv.zone ? { ...options, timeZone: rv.zone } : options);

function reviewExchange(one, rv) {
  const at = one.at ? new Date(`${one.at}Z`).toLocaleTimeString(undefined, houseClock(rv, { hour: 'numeric', minute: '2-digit' })) : '';
  const facts = [one.who, rv.agents[one.agent] || one.agent, at, one.seconds ? `${Math.round(one.seconds)} s` : '',
    (one.tools || []).length ? `used ${one.tools.join(', ')}` : 'no tools'].filter(Boolean).join(' · ');
  const body = h('div', { class: 'main' });
  if (one.mine && one.asked) add(body, h('div', { class: 'clamp2' }, `“${one.asked}”`));
  add(body, h('div', { class: 'meta' }, facts));
  if ((one.problems || []).length) add(body, h('div', { class: 'chips', style: { marginTop: '6px' } }, one.problems.map((code) =>
    h('span', { class: 'chip', title: rv.codes[code] || '' }, PROBLEM[code] || code))));
  if (one.mine && one.note) add(body, h('div', { class: 'small', style: { marginTop: '6px' } }, one.note));
  if (one.mine && one.fix) add(body, h('div', { class: 'small muted' }, `What would help: ${one.fix}`));
  if (one.mine && (one.errors || []).length) add(body, h('div', { class: 'tiny muted mono' }, one.errors.join(' · ')));
  if (!one.mine && one.tool) add(body, h('div', { class: 'small muted' }, `A tool that fits: ${one.tool}`));
  return h('div', { class: 'row' }, h('span', { class: `chip ${ratingClass(one.rating)} rating`, 'aria-label': one.rating ? `Rated ${one.rating} out of 5` : 'Not rated' },
    one.rating ? String(one.rating) : '–'), body);
}

function reviewOfDay(rv) {
  const review = rv.review;
  if (!review) {
    return h('div', { class: 'card empty', id: 'review-day' }, h('b', {}, 'No review yet'),
      rv.settings.on ? 'The first one is made tonight. Or review yesterday now, with the button above.' : 'Turn the daily review on, or review a day now with the button above.');
  }
  const t = review.totals;
  const table = (rows, name) => h('div', { class: 'rows' }, rows.map((row) => h('div', { class: 'row' },
    h('div', { class: 'main' }, h('div', {}, name(row)), h('div', { class: 'meta' }, plural(row.exchanges, 'exchange'),
      row.needs_a_look ? ` · ${row.needs_a_look} need${row.needs_a_look === 1 ? 's' : ''} a look` : '')),
    h('b', {}, row.score === null ? '–' : Number(row.score).toFixed(1)))));
  const poor = review.exchanges.filter((one) => one.rating && one.rating <= 3);
  const rest = review.exchanges.filter((one) => !one.rating || one.rating > 3);
  const card = h('div', { class: 'stack', id: 'review-day' },
    h('div', { class: 'card stack' },
      h('h3', {}, dayWords(review.day)),
      h('div', { class: 'stats' },
        h('div', { class: 'stat' }, h('b', {}, t.score === null ? '–' : Number(t.score).toFixed(1)), h('span', {}, 'out of 5')),
        h('div', { class: 'stat' }, h('b', {}, String(t.exchanges)), h('span', {}, `exchange${t.exchanges === 1 ? '' : 's'} in ${plural(t.chats, 'chat')}`)),
        h('div', { class: 'stat' }, h('b', {}, String(t.needs_a_look)), h('span', {}, t.needs_a_look === 1 ? 'needs a look' : 'need a look'))),
      h('p', { class: 'small muted' }, `Rated 5: ${t.by_rating['5']} · 4: ${t.by_rating['4']} · 3: ${t.by_rating['3']} · 2: ${t.by_rating['2']} · 1: ${t.by_rating['1']}`
        + (t.rated < t.exchanges ? ` · not rated: ${t.exchanges - t.rated}` : '') + (t.left_out ? ` · left out: ${t.left_out}` : '')
        + (review.jobs && review.jobs.ran ? ` · background jobs: ${review.jobs.ran} ran, ${review.jobs.failed} failed` : '')
        + ` · reviewed by ${review.model}`)));
  if ((review.improve || []).length) {
    add(card, h('div', { class: 'card stack' }, h('h3', {}, 'What to change'),
      h('div', { class: 'rows' }, review.improve.map((item) => h('div', { class: 'row' },
        h('div', { class: 'main' }, h('div', {}, item.what), h('div', { class: 'small muted' }, item.why),
          h('div', { class: 'meta' }, KIND[item.kind] || item.kind, ` · ${plural(item.count, 'exchange')}`)))))));
  }
  if ((review.fixes || []).length) {
    add(card, h('div', { class: 'card stack' }, h('h3', {}, 'From your own chats'),
      h('p', { class: 'small muted' }, 'What the reviewer thinks would have helped, worst first. Only you see this.'),
      h('div', { class: 'rows' }, review.fixes.map((fix) => h('div', { class: 'row' },
        h('span', { class: `chip ${ratingClass(fix.rating)} rating` }, String(fix.rating)),
        h('div', { class: 'main' }, h('div', {}, fix.fix), fix.asked ? h('div', { class: 'small muted clamp2' }, `“${fix.asked}”`) : null))))));
  }
  if (Object.keys(review.problems || {}).length) {
    add(card, h('div', { class: 'card stack' }, h('h3', {}, 'What went wrong'), h('div', { class: 'chips' }, problemChips(review.problems, rv.codes))));
  }
  add(card, h('div', { class: 'card stack' }, h('h3', {}, 'By assistant'), table(review.agents, (row) => row.label)));
  if ((review.people || []).length > 1) add(card, h('div', { class: 'card stack' }, h('h3', {}, 'By person'), table(review.people, (row) => row.label)));
  if ((review.models || []).length > 1) add(card, h('div', { class: 'card stack' }, h('h3', {}, 'By model'), table(review.models, (row) => row.id || 'Not recorded')));
  if (poor.length) add(card, h('div', { class: 'card stack' }, h('h3', {}, 'Needs a look'), h('div', { class: 'rows' }, poor.map((one) => reviewExchange(one, rv)))));
  if (rest.length) {
    add(card, h('details', { class: 'card' }, h('summary', {}, `The other ${plural(rest.length, 'exchange')}`),
      h('div', { class: 'rows', style: { marginTop: '8px' } }, rest.map((one) => reviewExchange(one, rv)))));
  }
  return card;
}

function drawReview() {
  const rv = state.review;
  const s = rv.settings;
  const running = rv.status.running;
  const next = rv.next ? new Date(rv.next) : null;
  const times = [];
  for (let hour = 0; hour < 24; hour += 1) for (const minute of ['00', '30']) times.push(`${String(hour).padStart(2, '0')}:${minute}`);
  if (!times.includes(s.time)) times.push(s.time);
  const said = running ? `${rv.status.said || 'Working…'}`
    : rv.status.error ? `The last run failed: ${rv.status.error}`
    : !s.on ? 'Off. Nothing is read until it is turned on, or a day is reviewed with a button below.'
    : next ? `Next: ${next.toLocaleString(undefined, houseClock(rv, { weekday: 'long', hour: 'numeric', minute: '2-digit' }))}, for the day before.` : '';
  clear(view,
    h('p', { class: 'small muted', style: { marginBottom: '10px' } }, 'Once a day the house’s own model reads the day’s chats and rates how the assistant did: what it got right, what went wrong, and what to change. It all happens on this server.'),
    h('div', { class: 'stack' },
      h('div', { class: 'card stack' },
        h('h3', {}, 'Daily review'),
        h('div', { class: 'rows' },
          setting('Review the chats every day', 'The day before is reviewed at the time below. It gives way the moment anyone uses the app and carries on afterwards.',
            keyed(toggle(s.on, (on) => saveReview({ on }, on ? 'The daily review is on' : 'The daily review is off'), 'Review the chats every day'), 'rv-on')),
          setting('At', rv.zone ? `On this house’s clock (${rv.zone.replace(/_/g, ' ')}).` : 'On the server’s clock.',
            select(times.sort().map((time) => [time, new Date(`2000-01-01T${time}`).toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })]), s.time,
              { 'aria-label': 'Time of the daily review', dataset: { focus: 'rv-time' }, onchange: (event) => saveReview({ time: event.target.value }, 'Time saved') })),
          setting('Tell the administrators', 'A notification on the phones that have them turned on: only when something needs a look, every day, or never.',
            select(NOTIFY, s.notify, { 'aria-label': 'When to tell the administrators', dataset: { focus: 'rv-notify' },
              onchange: (event) => saveReview({ notify: event.target.value }, 'Saved') }))),
        h('p', { class: 'small muted', role: 'status' }, said),
        h('div', { class: 'line', style: { flexWrap: 'wrap' } },
          h('button', { class: 'btn', type: 'button', disabled: running, onclick: () => reviewNow('yesterday') }, 'Review yesterday now'),
          h('button', { class: 'btn', type: 'button', disabled: running, onclick: () => reviewNow('today') }, 'Review today so far'))),
      h('div', { class: 'card stack' },
        h('h3', {}, 'Whose chats'),
        h('p', { class: 'small muted' }, 'An administrator’s own chats keep a short excerpt of what was asked and the reviewer’s note, shown only to that administrator. Everyone else’s are kept as figures and categories only: how it went, never what was said.'),
        h('div', { class: 'rows' }, rv.people.map((person) => setting(person.name, [person.admin ? 'Administrator' : '', person.family ? 'Family screen' : 'Full app'].filter(Boolean).join(' · '),
          keyed(toggle(person.included, (on) => saveReview({ skip: rv.people.filter((p) => (p.username === person.username ? !on : !p.included)).map((p) => p.username) },
            on ? `${person.name}’s chats are reviewed` : `${person.name}’s chats are left out`), `Review ${person.name}’s chats`), `rv-who-${person.username}`))))),
      reviewOfDay(rv),
      rv.history.length > 1 ? h('div', { class: 'card stack' }, h('h3', {}, 'Other days'),
        h('div', { class: 'rows' }, rv.history.filter((day) => !rv.review || day.day !== rv.review.day).map((day) => h('button', { class: 'row tap', type: 'button', onclick: () => openReview(day.day) },
          h('div', { class: 'main' }, h('div', {}, dayWords(day.day)), h('div', { class: 'meta' }, `${plural(day.exchanges, 'exchange')}${day.needs_a_look ? ` · ${day.needs_a_look} to look at` : ''}`)),
          h('b', {}, day.score === null ? '–' : Number(day.score).toFixed(1)))))) : null));
}

// ── activity ───────────────────────────────────────────────────────────────

async function loadActivity() {
  await loadOverview();
  const a = state.activity;
  const query = new URLSearchParams({ limit: String(a.limit) });
  if (a.user) query.set('user', a.user);
  a.entries = (await api(`/api/admin/activity?${query}`)).entries || [];
}

// The server writes the detail of a change with its own key names; say it in words.
function plainDetail(detail) {
  const can = {};
  state.overview.privileges.forEach((p) => { can[p.key] = p.label.toLowerCase(); });
  return detail.split('; ').map((part) => {
    const found = /^(privileges|experience) (.+)$/.exec(part);
    if (!found) return part;
    const words = found[2].split(', ').map((key) => (found[1] === 'privileges' ? can[key] : APP_WORDS[key]) || key);
    return `${found[1] === 'privileges' ? 'what they can do' : 'their app'} (${words.join(', ')})`;
  }).join('; ');
}

function describe(e) {
  const detail = e.detail ? String(e.detail) : '';
  const more = detail ? `: ${plainDetail(detail)}` : '';
  const named = detail ? `“${detail}” ` : '';
  const ended = /^(\d+) sessions?$/.exec(detail);
  const known = {
    sign_in: ['check', 'ok', 'Signed in'],
    sign_in_failed: ['x', 'warn', !e.user ? 'Sign-in attempt with an unknown name' : (detail === 'wrong second factor' ? 'Wrong 2-step code' : 'Wrong password')],
    sign_in_locked: ['lock', 'bad', `Locked out after repeated wrong passwords${detail ? ` (${detail})` : ''}`],
    sign_in_refused: ['lock', 'bad', 'Tried to sign in to a switched-off account'],
    password_changed: ['lock', 'edit', 'Changed their password'],
    password_reset: ['lock', 'edit', 'Password reset by an administrator'],
    account_created: ['plus', 'ok', `Account created${more}`],
    account_changed: ['edit', 'edit', `Account changed${more}`],
    account_deleted: ['trash', 'bad', `Account deleted${more}`],
    data_erased: ['trash', 'bad', `What the deleted account stored was erased${detail ? ` (${detail})` : ''}`],
    name_freed: ['undo', 'edit', 'Name of the deleted account freed for a new account'],
    signed_out_everywhere: ['logout', 'edit', `Signed out everywhere${ended ? `: ${plural(Number(ended[1]), 'session')} ended` : more}`],
    role_changed: ['shield', 'edit', `Role ${named}changed`],
    role_reset: ['undo', 'edit', `Role ${named}reset to its defaults`],
    role_deleted: ['trash', 'bad', `Role ${named}deleted`],
    household_changed: ['home', 'edit', `Household settings changed${detail ? ` (${detail})` : ''}`],
  };
  const [symbol, tone, text] = known[e.event] || ['info', '', String(e.event || 'Something happened')];
  return { symbol, tone, text };
}

function drawActivity() {
  const o = state.overview;
  const a = state.activity;
  const names = o.users.map((u) => u.username);
  if (a.user && !names.includes(a.user)) names.push(a.user);
  const show = (change) => go('activity', { activity: { ...a, ...change } });
  const pick = select([['', 'Everyone'], ...names.map((n) => [n, n])], a.user,
    { 'aria-label': 'Whose activity', style: { width: 'auto', maxWidth: '100%' }, onchange: (event) => show({ user: event.target.value, limit: 200 }) });
  clear(view,
    h('p', { class: 'small muted', style: { marginBottom: '10px' } }, 'Sign-ins and changes to accounts and roles, newest first.'),
    h('div', { class: 'line wrap', style: { marginBottom: '10px' } }, pick,
      a.entries.length ? h('span', { class: 'small muted' }, a.entries.length >= a.limit ? `The latest ${a.limit.toLocaleString()}` : plural(a.entries.length, 'entry', 'entries')) : null),
    a.entries.length
      ? h('div', { class: 'card flush rows' }, a.entries.map((e) => {
        const d = describe(e);
        const who = e.user || (e.event === 'sign_in_locked' ? 'an unknown name' : '');
        return h('div', { class: 'event' },
          h('div', { class: `dot ${d.tone}`.trim() }, icon(d.symbol, 'sm')),
          h('div', { class: 'grow' },
            h('div', { class: 'text' }, who ? [h('span', { class: 'who' }, who), ' — '] : null, d.text),
            h('div', { class: 'small muted', style: { marginTop: '3px' } },
              [e.by ? `by ${e.by}` : '', e.ip ? `from ${e.ip}` : '', when(e.ts, { time: true })].filter(Boolean).join(' · '))));
      }))
      : h('div', { class: 'card empty' }, h('b', {}, 'Nothing yet'), 'Sign-ins and changes made here are logged from now on.'),
    a.entries.length >= a.limit && a.limit < 1000
      ? h('div', { class: 'center', style: { marginTop: '12px' } }, h('button', { class: 'btn', type: 'button', onclick: () => show({ limit: 1000 }) }, 'Show older')) : null);
}

// ── start ──────────────────────────────────────────────────────────────────

function fromHash() {
  const [tab, id] = location.hash.replace(/^#/, '').split('/');
  if (!TABS.some(([known]) => known === tab)) return;
  let name = '';
  try { name = id ? decodeURIComponent(id) : ''; } catch (_) { /* not a name */ }
  state.tab = tab;
  state.person = tab === 'people' && name ? name : null;
  if (tab === 'activity') state.activity.user = name;
}

window.addEventListener('beforeunload', (event) => { if (unsaved()) { event.preventDefault(); event.returnValue = ''; } });
fromHash();
shell();
render();
window.addEventListener('hashchange', () => { fromHash(); state.draft = null; shell(); render(); });
