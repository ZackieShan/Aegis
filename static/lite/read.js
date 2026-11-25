// The reader (/read): something from the offline library opened for reading,
// with a shelf, highlights and notes — and a way to hand what matters to
// Aegis's memory.
//
//   /read                       the shelf: search, keep reading, highlights, collections
//   /read?u=<library link>      open what a link points at
//   /read?b=<collection>&p=<path>[&page=N]
//   /read?c=<collection>        the documents inside a collection
//   /read?shelf=<Category/Shelf> what is filed on a shelf (or in a category)
//   /read?view=collections      every collection, by name
//   /read?view=highlights       everything highlighted
//
// An article arrives as HTML made safe on the server (an allow-list of tags
// and attributes); a document arrives as the text of its pages. A highlight is
// a range of characters within its container (the article, or one page):
// start, end, the words themselves and a little of what surrounds them, so it
// is found again even if the text around it changes.

import { api, h, add, clear, icon, iconButton, toast, oops, sheet, confirmSheet, field, toggle, when, debounce, plural } from './lite.js';

const app = document.getElementById('app');
const root = document.documentElement;
const COLORS = ['yellow', 'green', 'blue', 'pink'];
const S = { me: null, look: loadLook(), item: null, marks: [], total: 0, first: 0, last: 0, original: '', cleanup: [], loading: false, pending: null };

// ── look ───────────────────────────────────────────────────────────────────
function loadLook() {
  const look = { paper: 'auto', size: 18, font: 'serif', width: 'narrow', pictures: false };
  try { Object.assign(look, JSON.parse(localStorage.getItem('aegis-reader-look') || '{}')); } catch (_) { /* defaults */ }
  return look;
}
function applyLook() {
  root.setAttribute('data-paper', S.look.paper);
  root.style.setProperty('--read-size', `${S.look.size}px`);
  if (S.look.font === 'sans') root.setAttribute('data-font', 'sans'); else root.removeAttribute('data-font');
  if (S.look.width === 'wide') root.setAttribute('data-width', 'wide'); else root.removeAttribute('data-width');
  try { localStorage.setItem('aegis-reader-look', JSON.stringify(S.look)); } catch (_) { /* private window */ }
}

// ── getting around ─────────────────────────────────────────────────────────
function go(search, replace = false) {
  history[replace ? 'replaceState' : 'pushState']({}, '', `/read${search || ''}`);
  route();
}
function route() {
  S.cleanup.splice(0).forEach((undo) => { try { undo(); } catch (_) { /* gone already */ } });
  hideBar();
  S.item = null;
  app.className = 'page';
  window.scrollTo(0, 0);
  const q = new URLSearchParams(location.search);
  if (q.get('u') || q.get('b')) return openDocument(q);
  if (q.get('view') === 'highlights') return renderHighlights();
  if (q.get('c')) return renderCollection(q.get('c'));
  if (q.get('shelf')) return renderPlace(q.get('shelf'), q.get('q') || '');
  if (q.get('view') === 'collections') return renderAllCollections();
  return renderShelf(q.get('q') || '');
}
const homeHref = () => (S.me && S.me.home) || '/';
const loading = () => h('div', { class: 'loading' }, h('span', { class: 'spinner' }));
const linkTo = (search) => (event) => { event.preventDefault(); go(search); };

// ── the shelf ──────────────────────────────────────────────────────────────
function progressOf(item) {
  if (item.kind === 'manual') return item.pages ? Math.min(1, (item.page || 1) / item.pages) : 0;
  return item.position || 0;
}
function placeOf(item) {
  if (item.kind === 'manual') return item.pages ? `Page ${item.page || 1} of ${item.pages}` : 'Document';
  const done = Math.round((item.position || 0) * 100);
  return done >= 97 ? 'Finished' : (done > 2 ? `${done}% read` : 'Not started');
}
function docRow(item, refresh) {
  return h('div', { class: 'row tap doc-row', onclick: () => go(item.link.replace(/^\/read/, '')) },
    h('div', { class: 'main' },
      h('div', { class: 'title clamp2' }, item.title),
      h('div', { class: 'meta' }, h('span', {}, placeOf(item)), item.marks ? h('span', {}, plural(item.marks, 'highlight')) : null,
        item.opened ? h('span', {}, when(item.opened)) : null),
      h('div', { class: 'bar' }, h('i', { style: { width: `${Math.round(progressOf(item) * 100)}%` } }))),
    iconButton('bookmark', item.pinned ? 'Kept on your shelf — tap to stop keeping it' : 'Keep on your shelf', async (event) => {
      event.stopPropagation();
      try { await api(`/api/reader/items/${item.id}`, { method: 'PATCH', body: { pinned: !item.pinned } }); refresh(); } catch (error) { oops(error); }
    }, item.pinned ? 'on' : ''),
    iconButton('x', 'Take off your shelf', async (event) => {
      event.stopPropagation();
      const warn = item.marks ? `“${item.title}” and its ${plural(item.marks, 'highlight')} will be removed from your shelf. Anything you already gave to Aegis's memory stays there.` : `“${item.title}” will be removed from your shelf.`;
      if (!await confirmSheet(warn, { title: 'Take it off your shelf?', ok: 'Remove', danger: true })) return;
      try { await api(`/api/reader/items/${item.id}`, { method: 'DELETE' }); refresh(); } catch (error) { oops(error); }
    }));
}
function quoteRow(mark, { withTitle = true, onopen = null } = {}) {
  return h('div', { class: 'row tap', onclick: onopen || (() => go(`${mark.link.replace(/^\/read/, '')}#mark=${mark.id}`)) },
    h('div', { class: `main quote-row ${mark.color}` },
      h('div', { class: 'said clamp2' }, mark.quote),
      mark.note ? h('div', { class: 'mine clamp2' }, mark.note) : null,
      h('div', { class: 'meta' }, withTitle && mark.title ? h('span', { class: 'clip', style: { maxWidth: '60%' } }, mark.title) : null,
        mark.page ? h('span', {}, `Page ${mark.page}`) : null, mark.shared ? h('span', { class: 'chip accent' }, 'In memory') : null,
        h('span', {}, when(mark.created)))));
}

async function renderShelf(query) {
  const results = h('div');
  const input = h('input', { class: 'input', type: 'search', placeholder: 'Search the library, or paste a library link', value: query, 'aria-label': 'Search the library', enterkeyhint: 'search' });
  const form = h('form', { class: 'shelf-search', onsubmit: (event) => { event.preventDefault(); const q = input.value.trim(); go(q ? `?q=${encodeURIComponent(q)}` : ''); } },
    h('div', { class: 'search' }, icon('search'), input), h('button', { class: 'btn primary', type: 'submit' }, 'Search'));
  const body = h('div', {}, loading());
  clear(app,
    h('div', { class: 'topbar' }, h('a', { class: 'icon-btn', href: homeHref(), title: 'Back', 'aria-label': 'Back' }, icon('back')),
      h('h1', {}, 'Reader', h('span', { class: 'sub' }, 'Read what is in the library. Mark what matters.')),
      h('a', { class: 'icon-btn', href: '/kitchen', title: 'Kitchen: recipes to cook from', 'aria-label': 'Kitchen' }, icon('pot'))),
    form, results, body);
  if (query) return runSearch(query, results, body);
  const refresh = () => renderShelf('');
  let data;
  try { data = await api('/api/reader/shelf'); } catch (error) { clear(body, h('div', { class: 'card empty' }, h('b', {}, 'The shelf could not be loaded'), error.message)); return; }
  const parts = [];
  if (!data.library) parts.push(h('div', { class: 'card soft' }, 'The offline library is switched off on this server, so there is nothing to open yet.'));
  if (data.recent.length) parts.push(h('div', { class: 'section-title' }, h('h2', {}, 'Keep reading')), h('div', { class: 'card flush rows' }, data.recent.slice(0, 8).map((item) => docRow(item, refresh))));
  if (data.kept.length) parts.push(h('div', { class: 'section-title' }, h('h2', {}, 'Kept'), h('span', { class: 'count' }, String(data.kept.length))), h('div', { class: 'card flush rows' }, data.kept.map((item) => docRow(item, refresh))));
  if (data.highlights.length) {
    parts.push(h('div', { class: 'section-title' }, h('h2', {}, 'Highlights'), h('span', { class: 'count' }, String(data.highlight_count)),
      h('a', { class: 'btn ghost sm', href: '/read?view=highlights', onclick: linkTo('?view=highlights') }, 'See all')),
    h('div', { class: 'card flush rows' }, data.highlights.slice(0, 4).map((mark) => quoteRow(mark))));
  }
  if (!data.recent.length && !data.kept.length) {
    parts.push(h('div', { class: 'card empty' }, h('b', {}, 'Nothing on your shelf yet'),
      'Search above, or pick a collection below. Whatever you open is kept here with your place in it. Select text while you read to highlight it, add a note, or give it to Aegis to remember.'));
  }
  const shelves = h('div', {}, loading());
  parts.push(h('div', { class: 'section-title' }, h('h2', {}, 'The library, by shelf'),
    h('a', { class: 'btn ghost sm', href: '/read?view=collections', onclick: linkTo('?view=collections') }, 'By collection')), shelves);
  const intake = h('div');
  parts.push(intake);
  clear(body, parts);
  renderShelves(shelves);
  if (S.me && S.me.is_admin && data.library) renderIntake(intake, () => renderShelves(shelves));
}

// ── the library by shelf ───────────────────────────────────────────────────
const shelfHref = (place, q = '') => `?shelf=${encodeURIComponent(place)}${q ? `&q=${encodeURIComponent(q)}` : ''}`;
const holds = (collections, items) => [collections ? plural(collections, 'collection') : '', items ? `${items.toLocaleString()} ${items === 1 ? 'book or document' : 'books and documents'}` : ''].filter(Boolean).join(' · ');

// The categories. On a server whose library has no shelves yet (an older
// build), the plain list of collections is shown instead.
async function renderShelves(into) {
  let data;
  try { data = await api('/api/reader/shelves'); } catch (_) { return renderCollections(into); }
  const shown = data.categories.filter((category) => category.collection_count || category.items);
  if (!shown.length) { clear(into, h('div', { class: 'card empty' }, data.available ? 'The library is empty.' : 'The library is not answering right now.')); return; }
  clear(into, h('div', { class: 'card flush rows' }, shown.map((category) => h('a', { class: 'row tap', href: `/read${shelfHref(category.name)}`, style: { color: 'inherit', textDecoration: 'none' }, onclick: linkTo(shelfHref(category.name)) },
    h('div', { class: 'main' }, h('div', { class: 'title' }, category.name), category.about ? h('div', { class: 'meta' }, category.about) : null,
      h('div', { class: 'meta' }, holds(category.collection_count, category.items))),
    icon('forward')))));
}

function itemRow(item, withShelf = false) {
  const who = [item.author, item.dates ? `(${item.dates})` : ''].filter(Boolean).join(' ');
  return h('a', { class: 'row tap', href: `/read?u=${encodeURIComponent(item.url)}`, style: { color: 'inherit', textDecoration: 'none' }, onclick: linkTo(`?u=${encodeURIComponent(item.url)}`) },
    h('div', { class: 'main' }, h('div', { class: 'title' }, item.title),
      (who || item.form || withShelf) ? h('div', { class: 'meta' }, who ? h('span', {}, who) : null, item.form ? h('span', {}, item.form) : null, withShelf ? h('span', { class: 'chip' }, item.shelf_name) : null) : null,
      item.note ? h('div', { class: 'snippet clamp2' }, item.note) : null));
}

const collectionRow = (c) => h('a', { class: 'row tap', href: `/read?c=${encodeURIComponent(c.id)}`, style: { color: 'inherit', textDecoration: 'none' }, onclick: linkTo(`?c=${encodeURIComponent(c.id)}`) },
  h('div', { class: 'main' }, h('div', { class: 'title' }, c.title), c.summary ? h('div', { class: 'meta' }, c.summary) : null,
    h('div', { class: 'meta' }, c.documents ? h('span', {}, plural(c.documents, 'document')) : null, c.articles ? h('span', {}, `${c.articles.toLocaleString()} pages`) : null)),
  icon('forward'));

// One category (its shelves) or one shelf (what is on it). The box searches
// inside it: the words of the texts, and titles and authors.
async function renderPlace(place, query) {
  const parent = place.includes('/') ? place.split('/')[0] : '';
  const title = h('h1', {}, place.replace('/', ' › '));
  const input = h('input', { class: 'input', type: 'search', placeholder: 'Search inside this shelf', value: query, 'aria-label': 'Search inside this shelf', enterkeyhint: 'search' });
  const form = h('form', { class: 'shelf-search', onsubmit: (event) => { event.preventDefault(); go(shelfHref(place, input.value.trim())); } },
    h('div', { class: 'search' }, icon('search'), input), h('button', { class: 'btn primary', type: 'submit' }, 'Search'));
  const results = h('div');
  const body = h('div', {}, loading());
  clear(app, h('div', { class: 'topbar' }, h('a', { class: 'icon-btn', href: parent ? `/read${shelfHref(parent)}` : '/read', title: 'Back', 'aria-label': 'Back', onclick: linkTo(parent ? shelfHref(parent) : '') }, icon('back')), title),
    form, results, body);
  if (query) return runSearch(query, results, body, place);
  let offset = 0;
  let everything = false;          // also the books below the owner's rating cut
  const filter = h('input', { class: 'input', type: 'search', placeholder: 'Find by title or author', 'aria-label': 'Find by title or author' });
  const load = async (reset) => {
    if (reset) offset = 0;
    let data;
    try { data = await api(`/api/reader/shelves/contents?shelf=${encodeURIComponent(place)}&q=${encodeURIComponent(filter.value.trim())}&offset=${offset}${everything ? '&all=1' : ''}`); }
    catch (error) { clear(body, h('div', { class: 'card empty' }, h('b', {}, 'That shelf could not be opened'), error.message)); return; }
    const rows = data.items.map((item) => itemRow(item, data.is_category && !!filter.value.trim()));
    if (!reset) {
      add(body.querySelector('#item-rows'), rows);
      const old = body.querySelector('.more-pages');
      if (old) old.remove();
    } else {
      clear(title, data.name, data.about ? h('span', { class: 'sub' }, data.about) : null);
      const hadFocus = document.activeElement === filter;      // before the box is moved into the new list
      const parts = [];
      if (data.is_category && !filter.value.trim()) {
        let shelves = [];
        try { shelves = ((await api('/api/reader/shelves')).categories.find((category) => category.name === place) || { shelves: [] }).shelves; } catch (_) { /* shown without them */ }
        const filled = shelves.filter((shelf) => shelf.collections.length || shelf.items);
        if (filled.length) parts.push(h('div', { class: 'section-title' }, h('h2', {}, 'Shelves')),
          h('div', { class: 'card flush rows' }, filled.map((shelf) => h('a', { class: 'row tap', href: `/read${shelfHref(shelf.path)}`, style: { color: 'inherit', textDecoration: 'none' }, onclick: linkTo(shelfHref(shelf.path)) },
            h('div', { class: 'main' }, h('div', { class: 'title' }, shelf.name), shelf.about ? h('div', { class: 'meta' }, shelf.about) : null,
              h('div', { class: 'meta' }, holds(shelf.collections.length, shelf.items))), icon('forward')))));
      }
      if (data.collections.length) parts.push(h('div', { class: 'section-title' }, h('h2', {}, 'Whole collections here'), h('span', { class: 'count' }, String(data.collections.length))),
        h('div', { class: 'card flush rows' }, data.collections.map(collectionRow)));
      if (data.total || filter.value.trim()) {
        parts.push(h('div', { class: 'section-title' }, h('h2', {}, data.is_category && !filter.value.trim() ? 'Not on a shelf yet' : 'Books and documents'), h('span', { class: 'count' }, data.total.toLocaleString())),
          h('div', { class: 'search', style: { marginBottom: '8px' } }, icon('search'), filter),
          rows.length ? h('div', { class: 'card flush rows', id: 'item-rows' }, rows) : h('div', { class: 'card empty' }, 'Nothing here by that name.'));
      } else if (data.is_category) {
        parts.push(h('div', { class: 'search', style: { margin: '14px 0 8px' } }, icon('search'), filter));
      }
      if (!parts.length) parts.push(h('div', { class: 'card empty' }, h('b', {}, 'Nothing is filed here yet')));
      // Books the owner's cut leaves off the shelves: say so, and offer them.
      if (data.hidden || everything) parts.push(h('div', { class: 'meta', id: 'below-cut', style: { margin: '12px 2px' } },
        everything ? 'Showing everything, including the less-read books below your cut. '
          : `${data.hidden.toLocaleString()} less-read ${data.hidden === 1 ? 'book is' : 'books are'} below your cut and not shown. `,
        h('button', { class: 'btn ghost sm', type: 'button', onclick: () => { everything = !everything; load(true); } }, everything ? 'Hide them again' : 'Show them')));
      clear(body, parts);
      if (hadFocus) { filter.focus(); filter.setSelectionRange(filter.value.length, filter.value.length); }
    }
    offset += data.items.length;
    if (data.items.length && offset < data.total && body.querySelector('#item-rows')) body.append(h('div', { class: 'more-pages' }, h('button', { class: 'btn', type: 'button', onclick: () => load(false) }, `Show more (${(data.total - offset).toLocaleString()} left)`)));
  };
  filter.addEventListener('input', debounce(() => load(true), 250));
  load(true);
}

// Every collection by name — the library as its files, not as its shelves.
function renderAllCollections() {
  const list = h('div', {}, loading());
  clear(app, h('div', { class: 'topbar' }, h('a', { class: 'icon-btn', href: '/read', title: 'Back to the shelf', 'aria-label': 'Back', onclick: linkTo('') }, icon('back')),
    h('h1', {}, 'Collections', h('span', { class: 'sub' }, 'Everything the library holds, one collection at a time.'))), list);
  renderCollections(list);
}

// New files copied into the library folder on the server: look for them, read
// them in and put them on shelves (administrators).
async function renderIntake(into, done) {
  const line = h('div', { class: 'meta', style: { margin: '10px 2px' } });
  const button = h('button', { class: 'btn ghost sm', type: 'button' }, 'Look for new files');
  let timer = null;
  S.cleanup.push(() => clearTimeout(timer));
  const show = (state) => {
    const mine = (state.files && state.files.indexed) || 0;
    const waiting = state.waiting_for_model || 0;
    button.disabled = !!state.running;
    clear(line, state.running ? `Taking in new files: ${state.step || 'working'}…`
      : [mine ? `${mine.toLocaleString()} of your own files are in the library.` : 'Files you copy into the library folder on the server appear here once they are taken in.',
        waiting ? ` ${waiting.toLocaleString()} are filed by folder and title only so far.` : ''].join(''), ' ', button);
    if (state.running) timer = setTimeout(poll, 4000);
  };
  const poll = async () => {
    try {
      const state = await api('/api/reference/intake');
      show(state);
      if (!state.running && state.summary) {
        const s = state.summary;
        toast(s.busy ? 'A scan is already running on the server.' : `${plural(s.new || 0, 'new file')} taken in${s.failed ? `, ${s.failed} could not be read` : ''}.`);
        done();
      }
    } catch (_) { /* an older server without this: say nothing */ }
  };
  button.addEventListener('click', async () => {
    try { show(await api('/api/reference/intake', { method: 'POST', body: {} })); } catch (error) { oops(error); }
  });
  try { show(await api('/api/reference/intake')); add(into, line); } catch (_) { /* no intake on this server */ }
}

async function runSearch(query, results, body, place = '') {
  clear(body);
  if (/^(https?:\/\/\S+\/content\/|\/read\?|\/api\/reference\/(added|files)\/)/i.test(query)) return go(`?u=${encodeURIComponent(query)}`, true);
  clear(results, loading());
  try {
    // Two questions at once: which books are called that (the catalogue), and
    // where do the words appear (the texts). An older server answers only the second.
    let below = [];
    const [named, found] = await Promise.all([
      place ? api(`/api/reader/shelves/contents?shelf=${encodeURIComponent(place)}&q=${encodeURIComponent(query)}`).then((data) => data.items.slice(0, 8)).catch(() => [])
        : api(`/api/reader/find?q=${encodeURIComponent(query)}`).then((data) => { below = data.below_cut || []; return data.items; }).catch(() => []),
      api(`/api/reference/search?q=${encodeURIComponent(query)}&count=20${place ? `&shelf=${encodeURIComponent(place)}` : ''}`),
    ]);
    if (!found.results.length && !named.length && !below.length) {
      clear(results, h('div', { class: 'card empty' }, h('b', {}, found.available ? 'Nothing found' : 'The library is not answering'),
        found.available ? (place ? 'Try fewer or different words, or search the whole library from the first page.'
          : 'Try fewer or different words. Novels, poems and plays are searched from the Literature shelf.') : 'Try again in a moment.'));
      return;
    }
    const parts = [];
    if (named.length) parts.push(h('div', { class: 'section-title' }, h('h2', {}, 'Books and documents by that name'), h('span', { class: 'count' }, String(named.length))),
      h('div', { class: 'card flush rows' }, named.map((item) => itemRow(item, true))));
    else if (below.length) parts.push(h('div', { class: 'section-title' }, h('h2', {}, 'By that name, below your cut'), h('span', { class: 'count' }, String(below.length))),
      h('div', { class: 'card flush rows' }, below.map((item) => itemRow(item, true))));
    if (found.results.length) parts.push(h('div', { class: 'section-title' }, h('h2', {}, place ? 'Inside this shelf' : 'In the library'), h('span', { class: 'count' }, String(found.results.length))),
      h('div', { class: 'card flush rows' }, found.results.map((hit) => h('a', { class: 'row tap hit', href: `/read?u=${encodeURIComponent(hit.url)}`, style: { color: 'inherit', textDecoration: 'none' }, onclick: linkTo(`?u=${encodeURIComponent(hit.url)}`) },
        h('div', { class: 'main' }, h('div', { class: 'title' }, hit.title),
          h('div', { class: 'meta' }, h('span', {}, hit.by ? `${hit.by}${hit.dates ? ` (${hit.dates})` : ''}` : hit.source), hit.kind === 'manual' ? h('span', { class: 'chip' }, 'Document') : null,
            hit.shelf_name && !place ? h('span', { class: 'chip' }, hit.shelf_name) : null),
          hit.snippet ? h('div', { class: 'snippet clamp2' }, hit.snippet) : null)))));
    clear(results, parts);
  } catch (error) { clear(results, h('div', { class: 'card empty' }, h('b', {}, 'The search did not work'), error.message)); }
}

async function renderCollections(into) {
  try {
    const data = await api('/api/reader/collections');
    if (!data.collections.length) { clear(into, h('div', { class: 'card empty' }, data.available ? 'The library is empty.' : 'The library is not answering right now.')); return; }
    const filter = h('input', { class: 'input', type: 'search', placeholder: `Find a collection (${data.collections.length})`, 'aria-label': 'Find a collection' });
    const list = h('div', { class: 'card flush rows', style: { marginTop: '8px' } });
    const draw = () => {
      const words = filter.value.trim().toLowerCase();
      const shown = data.collections.filter((c) => !words || `${c.title} ${c.summary} ${c.id}`.toLowerCase().includes(words));
      clear(list, shown.length ? shown.map((c) => h('a', { class: 'row tap', href: `/read?c=${encodeURIComponent(c.id)}`, style: { color: 'inherit', textDecoration: 'none' }, onclick: linkTo(`?c=${encodeURIComponent(c.id)}`) },
        h('div', { class: 'main' }, h('div', { class: 'title' }, c.title), c.summary ? h('div', { class: 'meta' }, c.summary) : null,
          h('div', { class: 'meta' }, c.documents ? h('span', {}, plural(c.documents, 'document')) : null, c.articles ? h('span', {}, `${c.articles.toLocaleString()} pages`) : null)),
        icon('forward'))) : h('div', { class: 'empty' }, 'No collection by that name.'));
    };
    filter.addEventListener('input', debounce(draw, 120));
    clear(into, filter, list);
    draw();
  } catch (error) { clear(into, h('div', { class: 'card empty' }, error.message)); }
}

// The documents inside a collection (manuals, added documents), and its front page.
async function renderCollection(id) {
  const list = h('div', {}, loading());
  const filter = h('input', { class: 'input', type: 'search', placeholder: 'Find a document by its title', 'aria-label': 'Find a document' });
  const title = h('h1', {}, 'Collection');
  clear(app, h('div', { class: 'topbar' }, h('a', { class: 'icon-btn', href: '/read', title: 'Back to the shelf', 'aria-label': 'Back', onclick: linkTo('') }, icon('back')), title),
    h('div', { class: 'line wrap', style: { marginBottom: '10px' } },
      h('a', { class: 'btn', href: `/read?b=${encodeURIComponent(id)}`, onclick: linkTo(`?b=${encodeURIComponent(id)}`) }, icon('book'), 'Open its front page'),
      h('div', { class: 'grow search' }, icon('search'), filter)), list);
  api('/api/reader/collections').then((data) => {
    const found = data.collections.find((c) => c.id === id);
    if (found) clear(title, found.title, found.summary ? h('span', { class: 'sub' }, found.summary) : null);
  }).catch(() => {});
  let offset = 0;
  const load = async (reset) => {
    if (reset) { offset = 0; clear(list, loading()); }
    try {
      const data = await api(`/api/reader/collections/${encodeURIComponent(id)}/documents?q=${encodeURIComponent(filter.value.trim())}&offset=${offset}`);
      const rows = data.documents.map((doc) => h('a', { class: 'row tap', href: `/read?b=${encodeURIComponent(id)}&p=${encodeURIComponent(doc.path)}`, style: { color: 'inherit', textDecoration: 'none' },
        onclick: linkTo(`?b=${encodeURIComponent(id)}&p=${encodeURIComponent(doc.path)}`) },
      h('div', { class: 'main' }, h('div', { class: 'title' }, doc.title), h('div', { class: 'meta' }, plural(doc.pages, 'page')))));
      if (reset) {
        if (!data.total) {
          clear(list, h('div', { class: 'card empty' }, filter.value.trim() ? 'No document with those words in its title.'
            : 'This collection is made of pages rather than documents. Open its front page, or search the library from the shelf.'));
          return;
        }
        clear(list, h('div', { class: 'section-title' }, h('h2', {}, 'Documents'), h('span', { class: 'count' }, data.total.toLocaleString())), h('div', { class: 'card flush rows', id: 'doc-rows' }, rows));
      } else {
        add(list.querySelector('#doc-rows'), rows);
        const old = list.querySelector('.more-pages');
        if (old) old.remove();
      }
      offset += data.documents.length;
      if (offset < data.total) list.append(h('div', { class: 'more-pages' }, h('button', { class: 'btn', type: 'button', onclick: () => load(false) }, `Show more (${(data.total - offset).toLocaleString()} left)`)));
    } catch (error) { clear(list, h('div', { class: 'card empty' }, error.message)); }
  };
  filter.addEventListener('input', debounce(() => load(true), 250));
  load(true);
}

// ── everything highlighted ─────────────────────────────────────────────────
async function renderHighlights() {
  const body = h('div', {}, loading());
  clear(app, h('div', { class: 'topbar' }, h('a', { class: 'icon-btn', href: '/read', title: 'Back to the shelf', 'aria-label': 'Back', onclick: linkTo('') }, icon('back')),
    h('h1', {}, 'Highlights'), h('a', { class: 'btn sm', href: '/api/reader/marks/export', download: 'highlights.md' }, icon('download', 'sm'), 'Save as a file')), body);
  try {
    const { marks } = await api('/api/reader/marks');
    if (!marks.length) { clear(body, h('div', { class: 'card empty' }, h('b', {}, 'No highlights yet'), 'Select text while you read, then tap a colour.')); return; }
    const groups = new Map();
    marks.forEach((mark) => { if (!groups.has(mark.item)) groups.set(mark.item, []); groups.get(mark.item).push(mark); });
    clear(body, [...groups.values()].map((group) => [
      h('div', { class: 'section-title' }, h('h2', { class: 'clip' }, group[0].title || 'Document'), h('span', { class: 'count' }, String(group.length))),
      h('div', { class: 'card flush rows' }, group.sort((a, b) => (a.page - b.page) || (a.start - b.start)).map((mark) => quoteRow(mark, { withTitle: false }))),
    ]));
  } catch (error) { clear(body, h('div', { class: 'card empty' }, error.message)); }
}

// ── reading ────────────────────────────────────────────────────────────────
async function openDocument(q) {
  clear(app, loading());
  let data;
  try { data = await api(`/api/reader/open?${q.toString()}`); } catch (error) { data = { problem: error.message, original: '' }; }
  if (data.problem) {
    clear(app, h('div', { class: 'topbar' }, h('a', { class: 'icon-btn', href: '/read', title: 'Back to the shelf', 'aria-label': 'Back', onclick: linkTo('') }, icon('back')), h('h1', {}, 'Reader')),
      h('div', { class: 'card empty' }, h('b', {}, 'This cannot be shown here'), data.problem,
        data.original ? h('div', { style: { marginTop: '14px' } }, h('a', { class: 'btn primary', href: data.original, target: '_blank', rel: 'noopener' }, icon('external'), "Open it in the library's own viewer")) : null));
    return;
  }
  S.item = data.item;
  S.marks = data.marks || [];
  S.original = data.content.original || '';
  S.wanted = new URLSearchParams(location.hash.slice(1)).get('mark') || '';     // a highlight to go straight to
  history.replaceState({}, '', S.item.link);          // the address that reopens it, wherever the link came from
  document.title = `${S.item.title} — Reader`;
  renderReading(data);
}

const marksOn = (page) => S.marks.filter((mark) => (mark.page || 0) === page);

function pageNode(page) {
  const text = h('div', { class: 'text', dataset: { page: String(page.n) } },
    page.blocks.map((block) => h(block.k === 'h' ? 'h3' : 'p', { class: block.k === 'li' ? 'item' : null }, block.t)));
  const show = h('button', { class: 'btn ghost sm', type: 'button' }, 'Show the page');
  const node = h('section', { class: 'sheet-page', id: `page-${page.n}`, dataset: { n: String(page.n) } },
    h('div', { class: 'page-head' }, h('span', {}, `Page ${page.n}`), show), text,
    page.blocks.length ? null : h('p', { class: 'no-text' }, 'No text was found on this page — it is a picture or a diagram.'));
  const picture = (on) => {
    const there = node.querySelector('.page-picture');
    if (on && !there) node.insertBefore(h('img', { class: 'page-picture', loading: 'lazy', alt: `Page ${page.n} as printed`, src: `/api/reader/items/${S.item.id}/pages/${page.n}/image` }), text);
    if (!on && there) there.remove();
    show.textContent = on ? 'Hide the page' : 'Show the page';
  };
  show.addEventListener('click', () => picture(!node.querySelector('.page-picture')));
  if (S.look.pictures || !page.blocks.length) picture(true);
  paintAll(text, marksOn(page.n));
  return node;
}

function renderReading(data) {
  const item = S.item;
  const where = h('span', {}, placeOf(item));
  const pin = iconButton('bookmark', 'Keep on your shelf', async () => {
    try { S.item = { ...S.item, ...(await api(`/api/reader/items/${item.id}`, { method: 'PATCH', body: { pinned: !S.item.pinned } })) }; pin.classList.toggle('on', S.item.pinned); toast(S.item.pinned ? 'Kept on your shelf' : 'No longer kept'); } catch (error) { oops(error); }
  }, item.pinned ? 'on' : '');
  const bar = h('div', { class: 'readbar' },
    h('a', { class: 'icon-btn', href: '/read', title: 'Back to the shelf', 'aria-label': 'Back to the shelf', onclick: linkTo('') }, icon('back')),
    h('div', { class: 'where' }, h('b', { class: 'clip' }, item.title), where),
    pin,
    h('button', { class: 'icon-btn aa', type: 'button', title: 'Text size, font and paper', 'aria-label': 'Text size, font and paper', onclick: lookSheet }, 'Aa'),
    iconButton('highlight', 'Your highlights in this', marksSheet),
    iconButton('more', 'More', moreSheet));
  const reading = h('article', { class: 'reading' });
  const more = h('div', { class: 'more-pages' });
  app.className = 'page reading-page';

  if (item.kind === 'article') {
    const text = h('div', { class: 'text', dataset: { page: '0' } });
    text.innerHTML = data.content.html;                 // made safe on the server: an allow-list of tags and attributes
    text.querySelectorAll('a[data-kind="web"]').forEach((link) => { link.target = '_blank'; link.rel = 'noopener noreferrer'; });
    text.querySelectorAll('h2, h3').forEach((heading, index) => { heading.id = `sec-${index}`; });
    reading.append(h('h1', { class: 'doc-title' }, item.title), text);
    paintAll(text, marksOn(0));
  } else {
    S.total = data.content.total;
    S.first = data.content.pages.length ? data.content.pages[0].n : 1;
    S.last = data.content.pages.length ? data.content.pages[data.content.pages.length - 1].n : 0;
    if (S.first > 1) reading.append(h('div', { class: 'more-pages' }, h('button', { class: 'btn', type: 'button', onclick: (event) => loadPages('up', event.currentTarget.parentNode) }, icon('up', 'sm'), 'Earlier pages')));
    data.content.pages.forEach((page) => reading.append(pageNode(page)));
  }
  clear(app, bar, reading, more);

  // links inside the text, and a tap on a highlight
  reading.addEventListener('click', (event) => {
    const mark = event.target.closest('mark.hl');
    const selection = window.getSelection();
    if (mark && (!selection || selection.isCollapsed)) { event.preventDefault(); const found = S.marks.find((m) => m.id === mark.dataset.mark); if (found) markSheet(found); return; }
    const link = event.target.closest('a[data-kind="library"]');
    if (link) { event.preventDefault(); go(new URL(link.href, location.href).search); }
  });

  // where they are: restore it, then keep it
  const place = () => {
    if (item.kind === 'manual') {
      let current = S.item.page || 1;
      for (const node of reading.querySelectorAll('.sheet-page')) { if (node.getBoundingClientRect().top <= window.innerHeight * 0.4) current = Number(node.dataset.n); else break; }
      return { page: current };
    }
    const room = document.documentElement.scrollHeight - window.innerHeight;
    return { position: room > 0 ? Math.min(1, Math.max(0, window.scrollY / room)) : 0 };
  };
  const save = debounce(() => { api(`/api/reader/items/${item.id}`, { method: 'PATCH', body: place() }).catch(() => {}); }, 1500);
  let lastY = 0;
  let settled = false;
  const onScroll = () => {
    const y = window.scrollY;
    if (y > lastY + 12 && y > 160) bar.classList.add('away'); else if (y < lastY - 12 || y < 160) bar.classList.remove('away');
    lastY = y;
    if (!settled) return;
    Object.assign(S.item, place());
    where.textContent = placeOf(S.item);
    save();
  };
  const leave = () => { if (settled) fetch(`/api/reader/items/${item.id}`, { method: 'PATCH', keepalive: true, credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(place()) }).catch(() => {}); };
  window.addEventListener('scroll', onScroll, { passive: true });
  window.addEventListener('pagehide', leave);
  S.cleanup.push(() => { leave(); window.removeEventListener('scroll', onScroll); window.removeEventListener('pagehide', leave); document.title = 'Reader — Aegis'; });

  const settle = () => {
    if (S.wanted && showMark(S.wanted)) return;
    if (item.kind === 'manual') { const node = document.getElementById(`page-${S.item.page}`); if (node && S.item.page > S.first) node.scrollIntoView(); } else if (item.position > 0.01) window.scrollTo(0, item.position * (document.documentElement.scrollHeight - window.innerHeight));
  };
  requestAnimationFrame(() => { settle(); setTimeout(() => { settled = true; }, 400); });

  if (item.kind === 'manual') {
    const watcher = new IntersectionObserver((entries) => { if (entries.some((entry) => entry.isIntersecting)) loadPages('down', more); }, { rootMargin: '900px' });
    watcher.observe(more);
    S.cleanup.push(() => watcher.disconnect());
    if (S.last >= S.total) more.append(h('span', { class: 'muted small' }, 'End of the document'));
  }
}

async function loadPages(direction, holder) {
  if (S.loading || S.item.kind !== 'manual') return;
  const first = direction === 'down' ? S.last + 1 : Math.max(1, S.first - 6);
  const count = direction === 'down' ? 6 : S.first - first;
  if (count < 1 || (direction === 'down' && first > S.total)) return;
  S.loading = true;
  try {
    const data = await api(`/api/reader/items/${S.item.id}/pages?first=${first}&count=${count}`);
    const reading = app.querySelector('.reading');
    if (!reading) return;
    if (direction === 'down') {
      data.pages.forEach((page) => reading.append(pageNode(page)));
      if (data.pages.length) S.last = data.pages[data.pages.length - 1].n;
      if (S.last >= S.total || !data.pages.length) clear(holder, h('span', { class: 'muted small' }, 'End of the document'));
    } else {
      const before = document.documentElement.scrollHeight;
      const anchor = holder.nextSibling;
      data.pages.forEach((page) => reading.insertBefore(pageNode(page), anchor));
      if (data.pages.length) S.first = data.pages[0].n;
      if (S.first <= 1) holder.remove();
      window.scrollBy(0, document.documentElement.scrollHeight - before);       // stay on the page they were reading
    }
  } catch (error) { oops(error); } finally { S.loading = false; }
}

// ── highlights: finding the words, painting them ───────────────────────────
function textNodes(container) {
  const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT);
  const nodes = [];
  let node;
  while ((node = walker.nextNode())) nodes.push(node);
  return nodes;
}

// Where a highlight is in its container now. The saved range is used when the
// words are still there; otherwise the words are looked for (white space
// counted loosely, the nearest match with the right surroundings preferred).
function locate(container, mark) {
  const full = container.textContent;
  if (full.slice(mark.start, mark.end) === mark.quote) return { start: mark.start, end: mark.end };
  const squash = (text) => String(text || '').replace(/\s+/g, ' ');
  const wanted = squash(mark.quote).trim();
  if (!wanted) return null;
  let flat = '';
  const at = [];
  let spaced = false;
  for (let i = 0; i < full.length; i += 1) {
    const space = /\s/.test(full[i]);
    if (space && spaced) continue;
    spaced = space;
    flat += space ? ' ' : full[i];
    at.push(i);
  }
  const before = squash(mark.prefix).slice(-14);
  const after = squash(mark.suffix).slice(0, 14);
  let best = -1;
  let bestScore = -Infinity;
  for (let found = flat.indexOf(wanted); found >= 0; found = flat.indexOf(wanted, found + 1)) {
    let score = -Math.abs(at[found] - mark.start) / Math.max(full.length, 1);
    if (before && flat.slice(0, found).endsWith(before)) score += 2;
    if (after && flat.slice(found + wanted.length).startsWith(after)) score += 2;
    if (score > bestScore) { best = found; bestScore = score; }
  }
  return best < 0 ? null : { start: at[best], end: at[best + wanted.length - 1] + 1 };
}

const BETWEEN_CELLS = new Set(['TABLE', 'THEAD', 'TBODY', 'TFOOT', 'TR', 'UL', 'OL', 'DL']);
function paint(container, mark) {
  const range = locate(container, mark);
  if (!range) return false;
  let position = 0;
  for (const node of textNodes(container)) {
    const from = position;
    const to = position + node.data.length;
    position = to;
    if (to <= range.start) continue;
    if (from >= range.end) break;
    let piece = node;
    const head = Math.max(range.start, from) - from;
    const length = Math.min(range.end, to) - Math.max(range.start, from);
    if (length <= 0) continue;
    if (head > 0) piece = piece.splitText(head);
    if (length < piece.data.length) piece.splitText(length);
    if (!piece.data.trim() && BETWEEN_CELLS.has(piece.parentNode.nodeName)) continue;      // white space between table cells or list items
    const wrap = h('mark', { class: `hl ${mark.color}${mark.note ? ' noted' : ''}`, dataset: { mark: mark.id } });
    piece.parentNode.insertBefore(wrap, piece);
    wrap.append(piece);
  }
  return true;
}
function paintAll(container, marks) {
  [...marks].sort((a, b) => a.start - b.start).forEach((mark) => { mark.lost = !paint(container, mark); });
}
function unpaint(id) {
  document.querySelectorAll(`mark.hl[data-mark="${id}"]`).forEach((wrap) => { const parent = wrap.parentNode; wrap.replaceWith(...wrap.childNodes); parent.normalize(); });
}
function restyle(mark) {
  document.querySelectorAll(`mark.hl[data-mark="${mark.id}"]`).forEach((wrap) => { wrap.className = `hl ${mark.color}${mark.note ? ' noted' : ''}`; });
}
function showMark(id) {
  const wrap = document.querySelector(`mark.hl[data-mark="${id}"]`);
  if (!wrap) return false;
  wrap.scrollIntoView({ block: 'center' });
  document.querySelectorAll(`mark.hl[data-mark="${id}"]`).forEach((part) => { part.classList.remove('flash'); void part.offsetWidth; part.classList.add('flash'); });
  return true;
}

// What is selected, as a highlight-to-be. A selection that runs past its page
// (or the article) is cut at the end of it.
function capture() {
  const selection = window.getSelection();
  if (!selection || selection.isCollapsed || !selection.rangeCount) return null;
  const range = selection.getRangeAt(0).cloneRange();
  const from = range.startContainer.nodeType === 1 ? range.startContainer : range.startContainer.parentElement;
  const container = from && from.closest('.reading .text');
  if (!container) return null;
  if (!container.contains(range.endContainer)) range.setEnd(container, container.childNodes.length);
  const picked = range.toString();
  if (!picked.trim()) return null;
  const lead = document.createRange();
  lead.selectNodeContents(container);
  lead.setEnd(range.startContainer, range.startOffset);
  const start = lead.toString().length + (picked.length - picked.trimStart().length);
  const end = start + picked.trim().length;
  const full = container.textContent;
  return { page: Number(container.dataset.page || 0), start, end, quote: full.slice(start, end), prefix: full.slice(Math.max(0, start - 40), start), suffix: full.slice(end, end + 40), container };
}

async function addMark(extra = {}) {
  const picked = S.pending || capture();
  if (!picked) { toast('Select some text first'); return null; }
  if (picked.quote.length > 6000) { toast('That is too much for one highlight — mark a shorter passage', { bad: true }); return null; }
  const { container, ...body } = picked;
  try {
    const mark = await api(`/api/reader/items/${S.item.id}/marks`, { method: 'POST', body: { ...body, color: S.look.color || 'yellow', ...extra } });
    S.marks.push(mark);
    paint(container, mark);
    window.getSelection().removeAllRanges();
    hideBar();
    return mark;
  } catch (error) { oops(error); return null; }
}

// ── the bar that appears over a selection ──────────────────────────────────
let bar = null;
function hideBar() { if (bar) { bar.remove(); bar = null; } S.pending = null; }
function showBar() {
  if (bar) return;
  // A press on the bar must not drop the selection. (Cancelling mousedown does
  // that on a phone too, where it follows the tap; cancelling touchstart would
  // also cancel the click.) What was selected is kept in S.pending regardless.
  bar = h('div', { class: 'selbar', role: 'toolbar', 'aria-label': 'Highlight', onmousedown: (event) => event.preventDefault() },
    COLORS.map((color) => h('button', { class: `dot ${color}`, type: 'button', title: `Highlight in ${color}`, 'aria-label': `Highlight in ${color}`,
      onclick: () => { S.look.color = color; applyLook(); addMark({ color }); } })),
    h('span', { class: 'sep' }),
    h('button', { class: 'act', type: 'button', title: 'Highlight it and add a note', onclick: async () => { const mark = await addMark(); if (mark) markSheet(mark, true); } }, icon('edit', 'sm'), 'Note'),
    h('button', { class: 'act', type: 'button', title: 'Highlight it and give it to Aegis to remember', onclick: async () => { const mark = await addMark(); if (mark) rememberSheet(mark); } }, icon('brain', 'sm'), 'Remember'),
    iconButton('more', 'More', () => {
      const picked = S.pending || capture();
      if (!picked) return;
      const done = sheet({ title: 'With the selected text', onclose: () => { if (!capture()) hideBar(); }, body: h('div', { class: 'stack' },
        h('button', { class: 'btn block', type: 'button', onclick: () => { done.close(); cook({ text: picked.quote }); } }, icon('pot'), 'Cook from this (set it out as a recipe)'),
        h('button', { class: 'btn block', type: 'button', onclick: async () => { try { await navigator.clipboard.writeText(picked.quote); toast('Copied'); } catch (_) { toast('Copying is not allowed here', { bad: true }); } done.close(); } }, icon('copy'), 'Copy')) });
    }));
  document.body.append(bar);
}
document.addEventListener('selectionchange', debounce(() => {
  if (!S.item) return;
  const picked = capture();
  if (picked) { S.pending = picked; showBar(); } else if (!document.querySelector('.scrim')) hideBar();
}, 220));

// ── sheets ─────────────────────────────────────────────────────────────────
function colorDots(current, onpick) {
  const row = h('div', { class: 'dots' });
  const draw = (chosen) => clear(row, COLORS.map((color) => h('button', { class: `dot ${color}`, type: 'button', 'aria-label': color, 'aria-pressed': String(color === chosen), onclick: () => { onpick(color); draw(color); } })));
  draw(current);
  return row;
}

function markSheet(mark, fresh = false) {
  const note = h('textarea', { class: 'input', placeholder: 'Your note (optional)', rows: '3' });
  note.value = mark.note || '';
  let color = mark.color;
  const state = h('div', { class: 'small muted' }, mark.shared ? `In your memory under “${mark.shared_topic}”.` : null);
  const save = async () => {
    if (note.value.trim() === (mark.note || '') && color === mark.color) return;
    Object.assign(mark, await api(`/api/reader/marks/${mark.id}`, { method: 'PATCH', body: { note: note.value, color } }));
    restyle(mark);
  };
  sheet({
    title: fresh ? 'Add a note' : 'Highlight',
    body: h('div', { class: 'stack' }, h('div', { class: `said ${mark.color}` }, mark.quote), note, colorDots(color, (picked) => { color = picked; }), state,
      mark.lost ? h('div', { class: 'small muted' }, 'These words are no longer on the page as they were when you marked them.') : null),
    actions: [
      { label: 'Delete', kind: 'ghost danger', run: async () => { await api(`/api/reader/marks/${mark.id}`, { method: 'DELETE' }); S.marks = S.marks.filter((m) => m.id !== mark.id); unpaint(mark.id); toast('Highlight removed'); } },
      { label: mark.shared ? 'Remember again' : 'Remember this', run: async () => { await save(); rememberSheet(mark); } },
      { label: 'Save', kind: 'primary', run: save },
    ],
  });
}

// Hand a highlight to Aegis's memory: a note in one of the person's topics.
async function rememberSheet(mark) {
  const topic = h('input', { class: 'input', type: 'text', maxlength: '60', placeholder: S.item ? S.item.title.slice(0, 60) : 'A topic', list: 'reader-topics' });
  const options = h('datalist', { id: 'reader-topics' });
  api('/api/mind/topics').then((data) => clear(options, (data.topics || []).map((t) => h('option', { value: t.name })))).catch(() => {});
  sheet({
    title: 'Remember this',
    body: h('div', { class: 'stack' }, h('div', { class: `said ${mark.color}` }, mark.quote), mark.note ? h('div', { class: 'small muted' }, `Your note: ${mark.note}`) : null,
      field('File it under', h('div', {}, topic, options), 'A topic in your memory. Leave it empty to file it under the name of what you are reading.'),
      h('p', { class: 'small muted' }, 'Aegis keeps the passage, your note and where it came from, and can bring it up in later conversations. You can see, change or delete it under “What Aegis knows about me”.')),
    actions: [{ label: 'Cancel', kind: 'ghost' }, { label: 'Remember', kind: 'primary', run: async () => {
      const result = await api(`/api/reader/marks/${mark.id}/remember`, { method: 'POST', body: { topic: topic.value.trim() } });
      Object.assign(mark, result.mark);
      toast(result.added ? `Kept in your memory under “${result.topic.name}”` : `Already in your memory under “${result.topic.name}”`, { action: { label: 'See it', run: () => { location.href = '/mind'; } } });
    } }],
  });
}

function marksSheet() {
  const here = [...S.marks].sort((a, b) => (a.page - b.page) || (a.start - b.start));
  const panel = sheet({
    title: `Highlights in this (${here.length})`,
    body: h('div', { class: 'stack' },
      here.length ? h('div', { class: 'card flush rows' }, here.map((mark) => quoteRow(mark, { withTitle: false, onopen: () => {
        panel.close();
        if (showMark(mark.id)) return;
        if (S.item.kind === 'manual' && mark.page) go(`${mark.link.replace(/^\/read/, '')}#mark=${mark.id}`); else markSheet(mark);
      } }))) : h('p', { class: 'muted' }, 'Nothing highlighted here yet. Select text, then tap a colour.'),
      h('div', { class: 'line wrap' },
        here.length ? h('a', { class: 'btn sm', href: `/api/reader/marks/export?item=${S.item.id}`, download: 'highlights.md' }, icon('download', 'sm'), 'Save as a file') : null,
        h('a', { class: 'btn sm ghost', href: '/read?view=highlights', onclick: (event) => { panel.close(); linkTo('?view=highlights')(event); } }, 'All my highlights'))),
  });
}

function lookSheet() {
  const sizeLabel = h('span', { class: 'mono' }, `${S.look.size}`);
  const size = (by) => { S.look.size = Math.max(14, Math.min(30, S.look.size + by)); sizeLabel.textContent = String(S.look.size); applyLook(); };
  const seg = (key, choices) => {
    const row = h('div', { class: 'seg' });
    const draw = () => clear(row, choices.map(([value, label]) => h('button', { type: 'button', 'aria-pressed': String(S.look[key] === value), onclick: () => { S.look[key] = value; applyLook(); draw(); } }, label)));
    draw();
    return row;
  };
  sheet({
    title: 'Reading',
    body: h('div', { class: 'stack' },
      field('Text size', h('div', { class: 'steps' }, h('button', { class: 'btn', type: 'button', 'aria-label': 'Smaller', onclick: () => size(-1) }, 'A−'), sizeLabel, h('button', { class: 'btn', type: 'button', 'aria-label': 'Larger', onclick: () => size(1) }, 'A+'))),
      field('Paper', seg('paper', [['auto', 'Auto'], ['light', 'Light'], ['sepia', 'Sepia'], ['dark', 'Dark']])),
      field('Type', seg('font', [['serif', 'Serif'], ['sans', 'Sans']])),
      field('Column', seg('width', [['narrow', 'Narrow'], ['wide', 'Wide']])),
      S.item && S.item.kind === 'manual' ? h('label', { class: 'line' }, toggle(S.look.pictures, (on) => { S.look.pictures = on; applyLook(); toast(on ? 'Pages will show as printed from the next one you open' : 'Pages will show as text'); }, 'Show each page as printed'),
        h('span', {}, 'Show each page as printed', h('span', { class: 'tiny faint', style: { display: 'block' } }, 'The picture of the page above its text — for figures and tables.'))) : null),
    actions: [{ label: 'Done', kind: 'primary' }],
  });
}

function moreSheet() {
  const item = S.item;
  const rows = [];
  const panel = { close: () => {} };
  const row = (name, label, run, href = null) => rows.push(href
    ? h('a', { class: 'btn block', href, target: '_blank', rel: 'noopener' }, icon(name), label)
    : h('button', { class: 'btn block', type: 'button', onclick: () => { panel.close(); run(); } }, icon(name), label));
  if (item.kind === 'manual') {
    row('search', `Go to a page (1–${item.pages})`, () => {
      const number = h('input', { class: 'input', type: 'number', min: '1', max: String(item.pages), value: String(S.item.page || 1), inputmode: 'numeric' });
      sheet({ title: 'Go to page', body: number, actions: [{ label: 'Cancel', kind: 'ghost' }, { label: 'Go', kind: 'primary', run: () => {
        const wanted = Math.max(1, Math.min(item.pages, parseInt(number.value, 10) || 1));
        const there = document.getElementById(`page-${wanted}`);
        if (there) there.scrollIntoView(); else go(`${item.link.replace(/^\/read/, '').replace(/&page=\d+/, '')}&page=${wanted}`);
      } }] });
    });
  } else {
    const headings = [...app.querySelectorAll('.reading .text h2, .reading .text h3')].filter((node) => node.textContent.trim());
    if (headings.length >= 3) row('list', 'Contents', () => {
      const list = sheet({ title: 'Contents', body: h('div', { class: 'contents' }, headings.map((node) => h('a', { href: `#${node.id}`, class: node.tagName === 'H3' ? 'sub' : '', onclick: (event) => { event.preventDefault(); list.close(); node.scrollIntoView(); } }, node.textContent.trim()))) });
    });
  }
  row('pot', item.kind === 'manual' ? 'Cook from this page' : 'Cook from this (set it out as a recipe)', () => cook({ url: `${item.link.replace(/&page=\d+/, '')}${item.kind === 'manual' ? `&page=${S.item.page || 1}` : ''}` }));
  if (S.original) row('external', item.kind === 'manual' ? 'Open the original document' : "Open in the library's own viewer", null, S.original);
  row('trash', 'Take off my shelf', async () => {
    if (!await confirmSheet(S.marks.length ? `This also removes your ${plural(S.marks.length, 'highlight')} in it.` : 'You can always open it again.', { title: 'Take it off your shelf?', ok: 'Remove', danger: true })) return;
    try { await api(`/api/reader/items/${item.id}`, { method: 'DELETE' }); S.item = null; go(''); } catch (error) { oops(error); }
  });
  Object.assign(panel, sheet({ title: item.title, body: h('div', { class: 'stack' }, rows) }));
}

// Set something out as a recipe in the kitchen (a model reads it: this takes a little while).
async function cook(body) {
  hideBar();
  const wait = sheet({ title: 'Setting the recipe out…', body: h('div', { class: 'stack' }, h('div', { class: 'loading' }, h('span', { class: 'spinner' })),
    h('p', { class: 'muted center' }, 'Reading it, working out the measures and breaking it into steps. This can take a minute.')) });
  try {
    const recipe = await api('/api/kitchen/recipes', { method: 'POST', body: { source_title: S.item ? S.item.title : '', source_url: S.item ? S.item.link : '', ...body } });
    location.href = recipe.link;
  } catch (error) { wait.close(); oops(error); }
}

// ── start ──────────────────────────────────────────────────────────────────
window.addEventListener('popstate', route);
(async () => {
  applyLook();
  try { S.me = await api('/api/me'); } catch (_) { S.me = null; }
  route();
})();
