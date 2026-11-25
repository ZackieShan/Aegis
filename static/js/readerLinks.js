// Links to the offline library open in the reader (/read), where they can be
// read comfortably, highlighted and kept — instead of in the library's own
// viewer on another port.
//
// A page that shows such links (a chat reply citing its sources) calls
// initReaderLinks() once. Which links count comes from the server
// (/api/reader/where): the library's public address, and the paths of the
// documents kept on this server (added ones, and the owner's own files).

let base = '';
let added = '/api/reference/added/';
let files = '/api/reference/files/';

/** The reader address for a link into the library, or null for any other link. */
export function readerHref(href) {
  try {
    const url = new URL(href, location.href);
    if (base && url.href.startsWith(`${base}/content/`)) return `/read?u=${encodeURIComponent(url.href)}`;
    if (url.origin === location.origin && (url.pathname.startsWith(added) || url.pathname.startsWith(files))) return `/read?u=${encodeURIComponent(url.pathname + url.hash)}`;
  } catch (_) { /* not a link */ }
  return null;
}

/** Send clicks on library links to the reader. `newTab`: keep this page open (the full app, mid-chat). */
export function initReaderLinks({ newTab = true } = {}) {
  fetch('/api/reader/where', { credentials: 'same-origin' })
    .then((response) => (response.ok ? response.json() : null))
    .then((where) => { if (where && where.library) { base = String(where.base || '').replace(/\/$/, ''); added = where.added || added; files = where.files || files; } })
    .catch(() => {});
  document.addEventListener('click', (event) => {
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const link = event.target && event.target.closest ? event.target.closest('a[href]') : null;
    if (!link || link.hasAttribute('download') || link.dataset.reader === 'off') return;
    const target = readerHref(link.getAttribute('href'));
    if (!target) return;
    event.preventDefault();
    event.stopPropagation();
    if (newTab) window.open(target, '_blank', 'noopener'); else location.href = target;
  }, true);
}
