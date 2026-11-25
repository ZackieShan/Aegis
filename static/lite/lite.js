// Shared helpers for the standalone pages (/home, /mind, /admin).
// Plain ES module: no build step, no dependencies, nothing inline (the
// Content-Security-Policy allows scripts from this origin only).

export async function api(path, { method = 'GET', body, form, signal } = {}) {
  const options = { method, credentials: 'same-origin', headers: {}, signal };
  if (form) {
    options.body = form;
  } else if (body !== undefined) {
    options.headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(body);
  }
  try {
    const offset = -new Date().getTimezoneOffset();
    options.headers['x-tz-offset'] = String(offset);
    options.headers['x-tz-name'] = Intl.DateTimeFormat().resolvedOptions().timeZone || '';
  } catch (_) { /* timezone is a nicety */ }
  const response = await fetch(path, options);
  if (response.status === 401) {
    location.href = '/login';
    throw new Error('Signed out');
  }
  const text = await response.text();
  let data = {};
  try { data = text ? JSON.parse(text) : {}; } catch (_) { data = { raw: text }; }
  if (!response.ok) {
    const detail = data.detail || data.error || data.message;
    const problem = new Error(typeof detail === 'string' ? detail : `Something went wrong (${response.status})`);
    problem.status = response.status;
    throw problem;
  }
  return data;
}

// h('div', {class: 'card', onclick: fn}, child, 'text', [more…])
export function h(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value;
    else if (key === 'dataset') Object.assign(node.dataset, value);
    else if (key === 'style' && typeof value === 'object') Object.assign(node.style, value);
    else if (key.startsWith('on') && typeof value === 'function') node.addEventListener(key.slice(2), value);
    else if (value === true) node.setAttribute(key, '');
    else node.setAttribute(key, value);
  }
  add(node, children);
  return node;
}

export function add(node, children) {
  for (const child of [].concat(children)) {
    if (child === null || child === undefined || child === false) continue;
    if (Array.isArray(child)) add(node, child);
    else node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

export function clear(node, ...children) {
  node.replaceChildren();
  return add(node, children);
}

const ICONS = {
  back: 'M15 18l-6-6 6-6', plus: 'M12 5v14M5 12h14', check: 'M5 12.5l4.5 4.5L19 7.5', x: 'M6 6l12 12M18 6L6 18',
  trash: 'M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13M10 11v6M14 11v6',
  edit: 'M4 20h4L19 9l-4-4L4 16v4zM13.5 6.5l4 4',
  search: 'M11 4a7 7 0 100 14 7 7 0 000-14zM20 20l-3.9-3.9',
  bell: 'M6 16V11a6 6 0 1112 0v5l2 2H4l2-2zM10 20a2 2 0 004 0',
  list: 'M9 6h11M9 12h11M9 18h11M4.5 6h.01M4.5 12h.01M4.5 18h.01',
  chart: 'M4 20V10M10 20V4M16 20v-7M22 20H2',
  board: 'M4 4h7v9H4zM13 4h7v5h-7zM13 11h7v9h-7zM4 15h7v5H4z',
  chat: 'M4 5h16v11H9l-5 4V5z', user: 'M12 12a4 4 0 100-8 4 4 0 000 8zM4 21c0-4 3.6-6 8-6s8 2 8 6',
  users: 'M9 11a3.5 3.5 0 100-7 3.5 3.5 0 000 7zM2 20c0-3.3 3-5 7-5s7 1.7 7 5M17 11a3 3 0 100-6M22 19c0-2.4-1.6-3.9-4-4.6',
  shield: 'M12 3l8 3v6c0 4.6-3.2 8-8 9-4.8-1-8-4.4-8-9V6l8-3z',
  clock: 'M12 21a9 9 0 100-18 9 9 0 000 18zM12 7v5l3.5 2',
  more: 'M5 12h.01M12 12h.01M19 12h.01', pin: 'M9 4h6l-1 6 3 3H7l3-3-1-6zM12 13v7',
  undo: 'M9 14L4 9l5-5M4 9h10a6 6 0 010 12h-3', download: 'M12 4v11M7 11l5 5 5-5M5 20h14',
  eye: 'M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12zM12 15a3 3 0 100-6 3 3 0 000 6z',
  sparkle: 'M12 3l1.9 5.6L19.5 10.5l-5.6 1.9L12 18l-1.9-5.6L4.5 10.5l5.6-1.9L12 3zM19 16l.7 2.3L22 19l-2.3.7L19 22l-.7-2.3L16 19l2.3-.7L19 16z',
  settings: 'M12 15a3 3 0 100-6 3 3 0 000 6zM19.4 13.5a7.6 7.6 0 000-3l2-1.5-2-3.4-2.3 1a7.6 7.6 0 00-2.6-1.5L14 2.5h-4l-.5 2.6a7.6 7.6 0 00-2.6 1.5l-2.3-1-2 3.4 2 1.5a7.6 7.6 0 000 3l-2 1.5 2 3.4 2.3-1a7.6 7.6 0 002.6 1.5l.5 2.6h4l.5-2.6a7.6 7.6 0 002.6-1.5l2.3 1 2-3.4-2-1.5z',
  send: 'M4 12l16-8-6 16-2.5-6.5L4 12z', image: 'M4 5h16v14H4zM4 16l4.5-4.5 3.5 3.5 3-3 5 5M9 9.5h.01',
  link: 'M10 14a4 4 0 005.7 0l3-3a4 4 0 00-5.7-5.7l-1 1M14 10a4 4 0 00-5.7 0l-3 3a4 4 0 005.7 5.7l1-1',
  calendar: 'M5 6h14v14H5zM5 10h14M9 3v4M15 3v4', target: 'M12 21a9 9 0 100-18 9 9 0 000 18zM12 16a4 4 0 100-8 4 4 0 000 8zM12 12h.01',
  home: 'M4 11l8-7 8 7v9h-5v-6H9v6H4v-9z', book: 'M5 4h11a3 3 0 013 3v13H8a3 3 0 01-3-3V4zM5 17a3 3 0 013-3h11',
  brain: 'M9 4a3 3 0 00-3 3 3 3 0 00-2 5 3 3 0 002 5 3 3 0 006 0V7a3 3 0 00-3-3zM15 4a3 3 0 013 3 3 3 0 012 5 3 3 0 01-2 5 3 3 0 01-6 0',
  lock: 'M6 11h12v9H6zM9 11V8a3 3 0 016 0v3', logout: 'M10 5H5v14h5M15 8l4 4-4 4M19 12H9',
  merge: 'M6 4v6a6 6 0 006 6h6M6 20v-4M15 13l3 3-3 3', camera: 'M4 8h3l2-3h6l2 3h3v11H4zM12 17a3.5 3.5 0 100-7 3.5 3.5 0 000 7z',
  heart: 'M12 20s-7-4.6-7-10a4 4 0 017-2.6A4 4 0 0119 10c0 5.4-7 10-7 10z', repeat: 'M17 3l3 3-3 3M4 11V9a3 3 0 013-3h13M7 21l-3-3 3-3M20 13v2a3 3 0 01-3 3H4',
  info: 'M12 21a9 9 0 100-18 9 9 0 000 18zM12 11v6M12 7.5h.01', filter: 'M4 5h16l-6 8v6l-4-2v-4L4 5z',
  bookmark: 'M7 4h10v16l-5-3.5L7 20V4z', copy: 'M9 9h10v11H9zM5 15V4h10', forward: 'M9 6l6 6-6 6',
  external: 'M14 5h5v5M19 5l-8 8M11 7H5v12h12v-6', up: 'M12 19V5M6 11l6-6 6 6',
  pot: 'M5 11h14v5a4 4 0 01-4 4H9a4 4 0 01-4-4v-5zM3 11h18M9 7V5M12 7V4M15 7V5',
  star: 'M12 4l2.5 5 5.5.8-4 3.9.9 5.5-4.9-2.6-4.9 2.6.9-5.5-4-3.9 5.5-.8L12 4z',
  speaker: 'M5 10v4h3l4 4V6l-4 4H5zM16 9a4 4 0 010 6M18.5 6.5a8 8 0 010 11',
  highlight: 'M4 20h7M14 4l6 6-8 8H8l-2-2v-4l8-8zM12 6l6 6', menu: 'M4 7h16M4 12h16M4 17h16',
  mic: 'M12 3a3 3 0 00-3 3v6a3 3 0 006 0V6a3 3 0 00-3-3zM6 11a6 6 0 0012 0M12 17v4M9 21h6',
  swap: 'M7 4L3 8l4 4M3 8h14M17 20l4-4-4-4M21 16H7', sun: 'M12 16.5a4.5 4.5 0 100-9 4.5 4.5 0 000 9zM12 2.5v2.2M12 19.3v2.2M2.5 12h2.2M19.3 12h2.2M5.3 5.3l1.6 1.6M17.1 17.1l1.6 1.6M5.3 18.7l1.6-1.6M17.1 6.9l1.6-1.6', stop: 'M7 7h10v10H7z',
  globe: 'M12 21a9 9 0 100-18 9 9 0 000 18zM3 12h18M12 3c2.5 2.6 3.8 5.6 3.8 9s-1.3 6.4-3.8 9c-2.5-2.6-3.8-5.6-3.8-9S9.5 5.6 12 3z',
  cloud: 'M7 18a4 4 0 01-.5-7.97A5.5 5.5 0 0117 8.5a4.5 4.5 0 010 9.5H7z',
  'cloud-sun': 'M12 2.5v2M5.3 5.3l1.4 1.4M2.5 12h2M16.3 8a4.3 4.3 0 00-8 1.6M9 20.5a3.5 3.5 0 01-.4-6.98A5 5 0 0118 13a3.8 3.8 0 010 7.5H9z',
  rain: 'M7 15a4 4 0 01-.5-7.97A5.5 5.5 0 0117 5.5a4.5 4.5 0 010 9.5H7zM8 18l-1 3M12 18l-1 3M16 18l-1 3',
  snow: 'M7 15a4 4 0 01-.5-7.97A5.5 5.5 0 0117 5.5a4.5 4.5 0 010 9.5H7zM8 19h.01M12 19h.01M16 19h.01M10 21.5h.01M14 21.5h.01',
  storm: 'M7 15a4 4 0 01-.5-7.97A5.5 5.5 0 0117 5.5a4.5 4.5 0 010 9.5H7zM13 15l-3 4h3l-2 3',
  fog: 'M7 13a4 4 0 01-.5-7.97A5.5 5.5 0 0117 3.5a4.5 4.5 0 010 9.5H7zM4 17h16M6 21h12',
  moon: 'M20 14.5A8 8 0 019.5 4a8 8 0 1010.5 10.5z',
  wind: 'M3 9h10a3 3 0 10-3-3M3 14h15a3 3 0 11-3 3M3 19h6',
  alert: 'M12 4l9 16H3L12 4zM12 10v5M12 17.5h.01',
  refresh: 'M20 11a8 8 0 10-2.3 6.3M20 5v6h-6',
  wrench: 'M14.7 6.3a4 4 0 00-5.2 5.2L4 17l3 3 5.5-5.5a4 4 0 005.2-5.2l-2.7 2.7-2.5-2.5 2.2-3.2z',
};

export function icon(name, extra = '') {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('class', `i ${extra}`.trim());
  svg.setAttribute('aria-hidden', 'true');
  const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  path.setAttribute('d', ICONS[name] || ICONS.info);
  svg.append(path);
  return svg;
}

export function iconButton(name, label, onclick, extra = '') {
  return h('button', { class: `icon-btn ${extra}`.trim(), type: 'button', title: label, 'aria-label': label, onclick }, icon(name));
}

// ── toasts ────────────────────────────────────────────────────────────────
let toastHost = null;
export function toast(message, { bad = false, action = null, ms = 3200 } = {}) {
  if (!toastHost) toastHost = document.body.appendChild(h('div', { class: 'toasts', role: 'status' }));
  const node = h('div', { class: `toast${bad ? ' bad' : ''}` }, message);
  if (action) node.append(h('button', { type: 'button', onclick: () => { node.remove(); action.run(); } }, action.label));
  toastHost.append(node);
  setTimeout(() => node.remove(), action ? Math.max(ms, 6000) : ms);
}
export const oops = (error) => toast(error && error.message ? error.message : String(error), { bad: true });

// ── sheets ────────────────────────────────────────────────────────────────
export function sheet({ title, body, actions = [], onclose = null }) {
  const scrim = h('div', { class: 'scrim' });
  const close = () => { scrim.remove(); document.removeEventListener('keydown', onkey); if (onclose) onclose(); };
  const onkey = (event) => { if (event.key === 'Escape') close(); };
  const buttons = actions.map((a) => h('button', {
    class: `btn ${a.kind || ''}`.trim(), type: 'button',
    onclick: async (event) => {
      const button = event.currentTarget;
      button.disabled = true;
      try {
        const keep = a.run ? await a.run() : undefined;
        if (keep !== false) close();
      } catch (error) { oops(error); } finally { button.disabled = false; }
    },
  }, a.label));
  const panel = h('div', { class: 'sheet', role: 'dialog', 'aria-modal': 'true' },
    title ? h('h2', {}, title) : null, body, buttons.length ? h('div', { class: 'actions' }, buttons) : null);
  scrim.append(panel);
  scrim.addEventListener('mousedown', (event) => { if (event.target === scrim) close(); });
  document.addEventListener('keydown', onkey);
  document.body.append(scrim);
  const first = panel.querySelector('input, textarea, select');
  if (first) setTimeout(() => first.focus(), 60);
  return { close, panel };
}

export function confirmSheet(message, { title = 'Are you sure?', ok = 'Yes', danger = false } = {}) {
  return new Promise((resolve) => {
    let answered = false;
    sheet({
      title, body: h('p', { class: 'muted' }, message),
      actions: [{ label: 'Cancel', kind: 'ghost' },
        { label: ok, kind: danger ? 'danger solid' : 'primary', run: () => { answered = true; resolve(true); } }],
      onclose: () => { if (!answered) resolve(false); },
    });
  });
}

export function field(label, control, hint = null) {
  return h('label', { class: 'field' }, h('span', {}, label), control, hint ? h('span', { class: 'tiny faint' }, hint) : null);
}

export function toggle(checked, onchange, label = '') {
  const input = h('input', { type: 'checkbox', 'aria-label': label, onchange: (event) => onchange(event.target.checked, event.target) });
  input.checked = !!checked;
  return h('span', { class: 'switch' }, input, h('i'));
}

// ── time ──────────────────────────────────────────────────────────────────
export function when(value, { time = false } = {}) {
  if (!value) return '';
  const date = typeof value === 'number' ? new Date(value * (value < 1e12 ? 1000 : 1)) : new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  const now = new Date();
  const startOf = (d) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  const days = Math.round((startOf(date) - startOf(now)) / 86400000);
  const clock = date.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  let day;
  if (days === 0) day = 'Today';
  else if (days === -1) day = 'Yesterday';
  else if (days === 1) day = 'Tomorrow';
  else if (days > 1 && days < 7) day = date.toLocaleDateString([], { weekday: 'long' });
  else day = date.toLocaleDateString([], { month: 'short', day: 'numeric', year: date.getFullYear() === now.getFullYear() ? undefined : 'numeric' });
  return time ? `${day}, ${clock}` : day;
}

export function ago(value) {
  if (!value) return '';
  const then = typeof value === 'number' ? value * (value < 1e12 ? 1000 : 1) : new Date(value).getTime();
  const seconds = Math.max(0, (Date.now() - then) / 1000);
  if (seconds < 60) return 'just now';
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} h ago`;
  if (seconds < 86400 * 7) return `${Math.floor(seconds / 86400)} d ago`;
  return when(value);
}

// ── markdown, safely ──────────────────────────────────────────────────────
// Builds DOM nodes; never assigns innerHTML. Enough for chat replies:
// paragraphs, headings, lists, code, tables, quotes, bold/italic, links.
function inline(text, into) {
  const pattern = /(`[^`\n]+`)|(\*\*[^*\n]+\*\*)|(\*[^*\n]+\*|_[^_\n]+_)|(!?\[[^\]\n]*\]\([^)\s]+\))|(https?:\/\/[^\s<)]+[^\s<).,;:!?])/g;
  let last = 0;
  let match;
  while ((match = pattern.exec(text))) {
    if (match.index > last) into.append(text.slice(last, match.index));
    const token = match[0];
    if (match[1]) into.append(h('code', {}, token.slice(1, -1)));
    else if (match[2]) inline(token.slice(2, -2), into.appendChild(h('strong')));
    else if (match[3]) inline(token.slice(1, -1), into.appendChild(h('em')));
    else if (match[4]) {
      const parts = /^(!?)\[([^\]]*)\]\(([^)\s]+)\)$/.exec(token);
      const href = parts[3];
      const safe = /^(https?:\/\/|\/(?!\/)|#)/i.test(href);
      if (!safe) into.append(parts[2]);
      else if (parts[1] && /^(\/api\/|https:\/\/)/.test(href)) into.append(h('img', { src: href, alt: parts[2], loading: 'lazy', style: { maxWidth: '100%', borderRadius: '10px' } }));
      else into.append(h('a', { href, ...(href.startsWith('http') ? { target: '_blank', rel: 'noopener noreferrer' } : {}) }, parts[2] || href));
    } else into.append(h('a', { href: token, target: '_blank', rel: 'noopener noreferrer' }, token));
    last = match.index + token.length;
  }
  if (last < text.length) into.append(text.slice(last));
  return into;
}

export function md(source) {
  const root = h('div', { class: 'md' });
  const lines = String(source || '').replace(/\r\n?/g, '\n').split('\n');
  let i = 0;
  const isTableRow = (line) => /^\s*\|.*\|\s*$/.test(line);
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) { i += 1; continue; }
    const fence = /^\s*```/.exec(line);
    if (fence) {
      const code = [];
      i += 1;
      while (i < lines.length && !/^\s*```/.test(lines[i])) { code.push(lines[i]); i += 1; }
      i += 1;
      root.append(h('pre', {}, h('code', {}, code.join('\n'))));
      continue;
    }
    const heading = /^(#{1,6})\s+(.*)$/.exec(line);
    if (heading) { inline(heading[2], root.appendChild(h('h4'))); i += 1; continue; }
    if (/^\s*([-*_])\s*\1\s*\1[\s\-*_]*$/.test(line)) { root.append(h('hr')); i += 1; continue; }
    if (isTableRow(line) && i + 1 < lines.length && /^\s*\|?[\s:|-]+\|[\s:|-]*$/.test(lines[i + 1])) {
      const cells = (row) => row.trim().replace(/^\||\|$/g, '').split('|').map((c) => c.trim());
      const table = h('table');
      const head = table.appendChild(h('tr'));
      cells(line).forEach((c) => inline(c, head.appendChild(h('th'))));
      i += 2;
      while (i < lines.length && isTableRow(lines[i])) {
        const row = table.appendChild(h('tr'));
        cells(lines[i]).forEach((c) => inline(c.replace(/<br\s*\/?>/gi, ' '), row.appendChild(h('td'))));
        i += 1;
      }
      root.append(table);
      continue;
    }
    if (/^\s*>\s?/.test(line)) {
      const quote = [];
      while (i < lines.length && /^\s*>\s?/.test(lines[i])) { quote.push(lines[i].replace(/^\s*>\s?/, '')); i += 1; }
      inline(quote.join(' '), root.appendChild(h('blockquote')));
      continue;
    }
    const bullet = /^(\s*)([-*•]|\d+[.)])\s+(.*)$/.exec(line);
    if (bullet) {
      const ordered = /\d/.test(bullet[2]);
      const list = root.appendChild(h(ordered ? 'ol' : 'ul'));
      while (i < lines.length) {
        const item = /^(\s*)([-*•]|\d+[.)])\s+(.*)$/.exec(lines[i]);
        if (!item) {
          if (lines[i].trim() && /^\s{2,}/.test(lines[i]) && list.lastChild) { list.lastChild.append(' '); inline(lines[i].trim(), list.lastChild); i += 1; continue; }
          break;
        }
        const li = list.appendChild(h('li'));
        const box = /^\[( |x|X)\]\s+(.*)$/.exec(item[3]);
        if (box) { li.append(box[1] === ' ' ? '☐ ' : '☑ '); inline(box[2], li); } else inline(item[3], li);
        i += 1;
      }
      continue;
    }
    const paragraph = [line];
    i += 1;
    while (i < lines.length && lines[i].trim() && !/^\s*(```|#{1,6}\s|>|[-*•]\s|\d+[.)]\s)/.test(lines[i]) && !isTableRow(lines[i])) {
      paragraph.push(lines[i]);
      i += 1;
    }
    const p = root.appendChild(h('p'));
    paragraph.forEach((part, index) => { if (index) p.append(h('br')); inline(part, p); });
  }
  return root;
}

export const debounce = (fn, ms = 250) => {
  let timer = null;
  return (...args) => { clearTimeout(timer); timer = setTimeout(() => fn(...args), ms); };
};

export const plural = (n, one, many) => `${n} ${n === 1 ? one : (many || `${one}s`)}`;
