/* Organizer — the vendored photo / cinema / music sub-app, mounted as THREE
 * separate Aegis tools (Photos, Movies & TV, Music). Each is its own draggable,
 * minimizable Aegis window hosting a single-app view of the organizer
 * (/organizer/?app=X, behind admin auth, proxied to a loopback subprocess), so
 * they can run and tile side by side via Aegis's own window manager. Each
 * iframe is built once and kept alive across minimize/restore so in-page state
 * (scans, plans, undo history) survives being hidden; a full close (✖) tears
 * that tool down. */
import * as Modals from './modalManager.js';
import { makeWindowDraggable } from './windowDrag.js';

const TOOLS = [
  {
    key: 'photos', title: 'Photos', src: '/organizer/?app=photos',
    railBtn: 'rail-photos', sidebarBtn: 'tool-photos-btn',
    icon: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;margin-right:6px"><rect x="3" y="5" width="18" height="15" rx="2"/><circle cx="12" cy="12.5" r="3.2"/><path d="M8 5l1.5-2h5L16 5"/></svg>',
  },
  {
    key: 'cinema', title: 'Movies & TV', src: '/organizer/?app=cinema',
    railBtn: 'rail-cinema', sidebarBtn: 'tool-cinema-btn',
    icon: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;margin-right:6px"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 4v16M17 4v16M3 9h4M17 9h4M3 15h4M17 15h4"/></svg>',
  },
  {
    key: 'music', title: 'Music', src: '/organizer/?app=music',
    railBtn: 'rail-music', sidebarBtn: 'tool-music-btn',
    icon: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;margin-right:6px"><path d="M9 18V5l10-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="16" cy="16" r="3"/></svg>',
  },
];

const _modals = {};  // key -> modal element

function _modalId(tool) { return 'organizer-' + tool.key + '-modal'; }

function _build(tool) {
  if (_modals[tool.key]) return _modals[tool.key];
  const m = document.createElement('div');
  m.id = _modalId(tool);
  m.className = 'modal hidden';
  m.innerHTML = `
    <div class="modal-content" style="width:min(1180px,92vw);height:86vh;max-width:94vw;max-height:90vh;padding:0;overflow:hidden;">
      <div class="modal-header">
        <h4>${tool.icon}${tool.title}</h4>
        <button class="close-btn" title="Close">✖</button>
      </div>
      <div class="modal-body" style="flex:1 1 auto;padding:0;margin:0;overflow:hidden;">
        <iframe title="${tool.title}"
          style="width:100%;height:100%;border:0;display:block;background:#008080;"></iframe>
      </div>
    </div>`;
  document.body.appendChild(m);
  m.querySelector('iframe').src = tool.src;  // lazy: not hit until opened
  m.querySelector('.close-btn')
    .addEventListener('click', () => Modals.close(_modalId(tool)));
  const content = m.querySelector('.modal-content');
  const header = m.querySelector('.modal-header');
  if (content && header) makeWindowDraggable(m, { content, header });
  try { Modals.injectMinimizeButton(m, _modalId(tool)); } catch (_) {}
  _modals[tool.key] = m;
  return m;
}

function _open(tool) {
  const m = _build(tool);
  m.classList.remove('hidden');
  Modals.register(_modalId(tool), {
    railBtnId: tool.railBtn,
    sidebarBtnId: tool.sidebarBtn,
    restoreFn: () => { m.classList.remove('hidden'); },
    closeFn: () => {
      if (_modals[tool.key]) { _modals[tool.key].remove(); delete _modals[tool.key]; }
      document.getElementById(tool.sidebarBtn)?.classList.remove('active');
    },
  });
}

function _toggle(tool) {
  const id = _modalId(tool);
  if (Modals.toggle(id)) return;                 // minimized → restore
  const m = _modals[tool.key];
  if (m && !m.classList.contains('hidden')) return;  // already open → no-op
  _open(tool);
}

TOOLS.forEach((tool) => {
  document.getElementById(tool.sidebarBtn)
    ?.addEventListener('click', () => _toggle(tool));
});
