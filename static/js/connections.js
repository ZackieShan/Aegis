// Settings → Integrations → Services: the outside services the assistant can use, and their keys.
//
// One set of keys serves the whole house. An administrator types a key in here; the server keeps it
// (encrypted, in its data folder) and never sends it back — this screen only ever sees whether a key
// is set and its last four characters. Everything shown comes from GET /api/connections.

const STYLE_ID = 'connections-style';
const CSS = `
.cx-intro { font-size: 12px; opacity: .72; line-height: 1.45; margin: 0 0 10px; }
.cx-group { font-size: 10px; letter-spacing: .08em; text-transform: uppercase; opacity: .55; margin: 14px 0 6px; }
.cx-item { border: 1px solid var(--border); border-radius: 8px; margin-bottom: 6px; background: color-mix(in srgb, var(--fg) 3%, transparent); }
.cx-head { display: flex; align-items: center; gap: 10px; width: 100%; padding: 10px 12px; background: none; border: 0; color: inherit;
  font: inherit; text-align: left; cursor: pointer; min-height: 44px; }
.cx-head:focus-visible { outline: 2px solid var(--accent, var(--red)); outline-offset: -2px; border-radius: 8px; }
.cx-name { font-size: 13px; font-weight: 600; flex: 1; min-width: 0; }
.cx-chip { font-size: 10px; padding: 2px 8px; border-radius: 999px; border: 1px solid var(--border); white-space: nowrap; }
.cx-chip[data-state="ready"] { color: var(--color-success, #3fb950); border-color: color-mix(in srgb, var(--color-success, #3fb950) 55%, transparent); }
.cx-chip[data-state="bad"] { color: var(--red, #f85149); border-color: color-mix(in srgb, var(--red, #f85149) 55%, transparent); }
.cx-chip[data-state="warn"] { color: var(--color-warning, #d29922); border-color: color-mix(in srgb, var(--color-warning, #d29922) 55%, transparent); }
.cx-chip[data-state="idle"] { opacity: .6; }
.cx-caret { opacity: .5; font-size: 10px; transition: transform .15s; }
.cx-item[data-open="true"] .cx-caret { transform: rotate(90deg); }
.cx-body { display: none; padding: 0 12px 12px; }
.cx-item[data-open="true"] .cx-body { display: block; }
.cx-does { font-size: 12px; line-height: 1.5; margin: 0 0 8px; }
.cx-facts { font-size: 11px; line-height: 1.5; opacity: .72; margin: 0 0 10px; }
.cx-facts b { font-weight: 600; opacity: .95; }
.cx-field { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 8px; margin-bottom: 8px; }
.cx-field label { font-size: 11px; min-width: 96px; opacity: .8; }
.cx-field input { flex: 1 1 200px; min-width: 0; }
.cx-note { font-size: 11px; opacity: .7; line-height: 1.45; margin: 0 0 8px; }
.cx-use { font-size: 11px; line-height: 1.5; margin: 0 0 8px; padding: 8px 10px; border-radius: 6px;
  background: color-mix(in srgb, var(--fg) 5%, transparent); }
.cx-use input { width: 84px; margin: 0 4px; }
.cx-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; }
.cx-actions .cx-spacer { flex: 1; }
.cx-btn { background: transparent; color: var(--accent, var(--red)); border: 1px solid color-mix(in srgb, var(--accent, var(--red)) 45%, var(--border));
  border-radius: 6px; padding: 6px 12px; font-size: 12px; cursor: pointer; min-height: 32px; }
.cx-btn[disabled] { opacity: .5; cursor: default; }
.cx-btn.cx-quiet { color: inherit; border-color: var(--border); opacity: .8; }
.cx-switch { display: inline-flex; align-items: center; gap: 6px; font-size: 11px; cursor: pointer; }
.cx-msg { font-size: 11px; line-height: 1.45; margin: 8px 0 0; min-height: 1em; }
.cx-msg[data-kind="ok"] { color: var(--color-success, #3fb950); }
.cx-msg[data-kind="bad"] { color: var(--red, #f85149); }
.cx-link { color: var(--accent, var(--red)); text-decoration: none; font-size: 11px; white-space: nowrap; }
.cx-link:hover { text-decoration: underline; }
@media (max-width: 520px) { .cx-field label { min-width: 100%; } }
`;

let started = false;
const open = new Set();          // ids of the services whose cards are open, kept across redraws

function make(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value;
    else if (key.startsWith('on') && typeof value === 'function') node.addEventListener(key.slice(2), value);
    else if (key === 'dataset') Object.assign(node.dataset, value);
    else node.setAttribute(key, value === true ? '' : String(value));
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return node;
}

async function call(method, path, body) {
  const options = { method, credentials: 'same-origin', headers: {} };
  if (body !== undefined) {
    options.headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  let data = null;
  try { data = await response.json(); } catch (_) { /* no body */ }
  if (!response.ok) {
    const said = data && (data.detail || data.error);
    throw new Error(typeof said === 'string' ? said : `The server answered ${response.status}.`);
  }
  return data;
}

export function state(service) {
  // What the chip on a card says, and how it is coloured.
  if (!service.on) return ['idle', 'Off'];
  if (service.needs_key && !service.has_key) return ['idle', 'Needs a key'];
  if (service.metered && service.metered.over) return ['warn', 'Limit reached'];
  if (service.tried && service.tried.ok === false) return ['bad', 'Not working'];
  if (!service.needs_key && !service.has_key) return ['ready', 'Ready, no key needed'];
  return ['ready', 'Ready'];
}

export function money(amount) {
  return '$' + Number(amount || 0).toFixed(2);
}

function usage(service, limitInput) {
  const use = service.metered;
  const parts = [`This month: ${use.requests.toLocaleString()} request${use.requests === 1 ? '' : 's'}`];
  if (use.price_per_1000) parts.push(`${money(use.cost)} beyond the free ${use.free.toLocaleString()}`);
  const line = make('div', { class: 'cx-use' }, parts.join(' · ') + '. ',
    make('label', {}, 'Stop at ', limitInput, ' a month'),
    make('span', { style: 'opacity:.7' }, ' (0 = no limit).'));
  if (use.price_per_1000 && use.limit > use.free) {
    const most = (use.limit - use.free) * use.price_per_1000 / 1000;
    line.append(make('div', { style: 'opacity:.7;margin-top:2px' }, `At this limit a month can cost up to ${money(most)}.`));
  } else if (use.price_per_1000 && use.limit && use.limit <= use.free) {
    line.append(make('div', { style: 'opacity:.7;margin-top:2px' }, 'At this limit it costs nothing: when the free requests are used up, searches fall back to the free provider until next month.'));
  } else if (use.price_per_1000 && !use.limit) {
    line.append(make('div', { style: 'opacity:.7;margin-top:2px' }, `No limit: every 1,000 requests beyond the free ones cost ${money(use.price_per_1000)}.`));
  }
  return line;
}

function card(service, redraw) {
  const [kind, words] = state(service);
  const item = make('div', { class: 'cx-item', dataset: { id: service.id, open: String(open.has(service.id)) } });
  const body = make('div', { class: 'cx-body', id: `cx-body-${service.id}` });
  const head = make('button', {
    class: 'cx-head', type: 'button', 'aria-expanded': String(open.has(service.id)), 'aria-controls': `cx-body-${service.id}`,
    onclick: () => {
      const now = item.dataset.open !== 'true';
      item.dataset.open = String(now);
      head.setAttribute('aria-expanded', String(now));
      if (now) open.add(service.id); else open.delete(service.id);
    },
  }, make('span', { class: 'cx-caret', 'aria-hidden': 'true', text: '▶' }), make('span', { class: 'cx-name', text: service.name }),
  make('span', { class: 'cx-chip', dataset: { state: kind }, text: words, title: (service.tried && service.tried.said) || '' }));

  const message = make('p', { class: 'cx-msg', role: 'status', 'aria-live': 'polite' });
  const say = (text, how) => { message.textContent = text || ''; message.dataset.kind = how || ''; };
  if (service.tried && service.tried.said) say(service.tried.said, service.tried.ok ? 'ok' : 'bad');

  body.append(make('p', { class: 'cx-does', text: service.does }));
  const facts = make('p', { class: 'cx-facts' });
  if (service.cost) facts.append(make('b', { text: 'Cost: ' }), service.cost + ' ');
  if (service.sends) facts.append(make('br'), make('b', { text: 'What leaves the house: ' }), service.sends);
  body.append(facts);

  const inputs = {};
  for (const field of service.fields) {
    const input = make('input', {
      class: 'settings-input', type: field.secret ? 'password' : 'text', autocomplete: 'off', spellcheck: 'false',
      id: `cx-${service.id}-${field.name}`, 'data-lpignore': 'true',
      placeholder: field.set ? `${field.shown} — saved. Type a new one to replace it.` : (field.placeholder || (field.optional ? 'Optional' : 'Paste it here')),
    });
    inputs[field.name] = input;
    const row = make('div', { class: 'cx-field' }, make('label', { for: input.id, text: field.label + (field.optional ? ' (optional)' : '') }), input);
    if (service.signup) row.append(make('a', { class: 'cx-link', href: service.signup, target: '_blank', rel: 'noopener noreferrer', text: 'Get one ↗' }));
    body.append(row);
    if (field.from === 'settings') {
      body.append(make('p', { class: 'cx-note', text: 'This one is still kept in the older place (the settings file, unencrypted). Paste it here and save to keep it encrypted instead.' }));
    } else if (field.from === 'environment') {
      body.append(make('p', { class: 'cx-note', text: 'This one comes from the server’s environment. A key saved here is used instead.' }));
    }
  }
  if (service.key_adds) body.append(make('p', { class: 'cx-note', text: service.key_adds }));

  let limitInput = null;
  if (service.metered) {
    limitInput = make('input', { class: 'settings-input', type: 'number', min: '0', step: '100', inputmode: 'numeric', value: String(service.metered.limit),
      'aria-label': `Most requests a month to ${service.name}` });
    body.append(usage(service, limitInput));
  }

  const busy = (on) => body.querySelectorAll('button, input').forEach((node) => { node.disabled = on; });
  const run = async (work) => {
    busy(true);
    try { await work(); } catch (problem) { say(problem.message || 'That did not work.', 'bad'); } finally { busy(false); }
  };

  const tryIt = async () => {
    say('Trying it…');
    const found = await call('POST', `/api/connections/${service.id}/try`);
    redraw(found.service);
  };
  const save = make('button', { class: 'cx-btn', type: 'button', text: 'Save', onclick: () => run(async () => {
    const fields = {};
    for (const [name, input] of Object.entries(inputs)) if (input.value.trim()) fields[name] = input.value.trim();
    const sent = {};
    if (Object.keys(fields).length) sent.fields = fields;
    if (limitInput && Number(limitInput.value) !== service.metered.limit) {
      const limit = Number(limitInput.value);
      if (!Number.isInteger(limit) || limit < 0) throw new Error('The monthly limit must be a whole number (0 for no limit).');
      sent.limit = limit;
    }
    if (!Object.keys(sent).length) { say('Nothing was changed.'); return; }
    const kept = await call('PUT', `/api/connections/${service.id}`, sent);
    if (sent.fields && kept.on && kept.ready) { Object.assign(service, kept); await tryIt(); } else redraw(kept, 'Saved.');
  }) });
  const tryButton = make('button', { class: 'cx-btn', type: 'button', text: 'Try it', disabled: !service.ready, onclick: () => run(tryIt) });
  const actions = make('div', { class: 'cx-actions' }, save, tryButton);
  if (service.fields.some((field) => field.set && field.from !== 'environment')) {
    actions.append(make('button', { class: 'cx-btn cx-quiet', type: 'button', text: 'Remove key', onclick: () => run(async () => {
      const sure = window.styledConfirm
        ? await window.styledConfirm(`Remove the key for ${service.name}? Everyone's assistant stops using it.`, { confirmText: 'Remove', danger: true })
        : window.confirm(`Remove the key for ${service.name}?`);
      if (!sure) return;
      redraw(await call('DELETE', `/api/connections/${service.id}`), 'Removed.');
    }) }));
  }
  const toggle = make('input', { type: 'checkbox', checked: service.on, onchange: () => run(async () => {
    redraw(await call('PUT', `/api/connections/${service.id}`, { on: toggle.checked }), toggle.checked ? 'Switched on.' : 'Switched off. Nothing is sent to it while it is off.');
  }) });
  actions.append(make('span', { class: 'cx-spacer' }), make('label', { class: 'cx-switch' }, toggle, 'On'));
  body.append(actions, message);
  item.append(head, body);
  item.say = say;
  return item;
}

function draw(root, data) {
  root.replaceChildren();
  const byGroup = new Map();
  for (const service of data.services || []) {
    if (!byGroup.has(service.group)) byGroup.set(service.group, []);
    byGroup.get(service.group).push(service);
  }
  const redraw = (service, words) => {
    const list = data.services || [];
    const at = list.findIndex((one) => one.id === service.id);
    if (at >= 0) list[at] = service;
    const old = root.querySelector(`.cx-item[data-id="${service.id}"]`);
    const fresh = card(service, redraw);
    if (words) fresh.say(words, 'ok');
    if (old) old.replaceWith(fresh);
    try { window.dispatchEvent(new CustomEvent('aegis-integrations-changed')); } catch (_) { /* older browsers */ }
  };
  for (const group of data.groups || []) {
    const services = byGroup.get(group.id);
    if (!services || !services.length) continue;
    root.append(make('div', { class: 'cx-group', text: group.label }));
    for (const service of services) root.append(card(service, redraw));
  }
}

export async function init() {
  const root = document.getElementById('connections-list');
  if (!root || started) return;
  started = true;
  if (!document.getElementById(STYLE_ID)) document.head.append(make('style', { id: STYLE_ID, text: CSS }));
  root.textContent = 'Loading…';
  try {
    draw(root, await call('GET', '/api/connections'));
  } catch (problem) {
    started = false;
    root.textContent = '';
    root.append(make('p', { class: 'cx-note', text: `The list of services could not be loaded. ${problem.message || ''}` }));
  }
}

export default { init, state, money };
