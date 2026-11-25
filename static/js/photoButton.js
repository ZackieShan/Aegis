// static/js/photoButton.js
//
// The camera button in the message box.
//
// Taking or choosing a photo used to be two taps deep behind an unlabelled
// arrow ("More tools"), so on a phone there was no visible way to give Aegis a
// picture. This puts it on the bar: tap the camera, take or choose a photo,
// then say what to do with it — identify what it shows, or add it to the
// message and ask about it.
//
// Nothing new happens behind it: "identify" is the same flow as /identify,
// "ask about it" is an ordinary attachment.

import fileHandlerModule from './fileHandler.js';
import { identifyPhoto } from './slashCommands.js';

const $ = (id) => document.getElementById(id);
const ICONS = {
  camera: '<path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z"/><circle cx="12" cy="13" r="4"/>',
  picture: '<rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="8.5" cy="9.5" r="1.5"/><path d="M21 16l-5-5L5 20"/>',
  clip: '<path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/>',
};
const svg = (name) => `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONS[name]}</svg>`;

// A phone or tablet: "Take a photo" opens the camera there. On a desktop the
// same input only opens the file chooser, so the entry is left out.
const hasCamera = () => (window.matchMedia && window.matchMedia('(pointer: coarse)').matches) || (navigator.maxTouchPoints || 0) > 0;
// An administrator can hide attaching or identifying for an account; the
// photo button offers only what the menu next to it offers.
const offered = (id) => { const el = $(id); return !!el && !el.hidden && getComputedStyle(el).display !== 'none'; };

/** Ask the device for files. Must be called inside the tap itself (iOS). */
function choose({ camera = false, images = true, many = false } = {}) {
  return new Promise((resolve) => {
    const input = document.createElement('input');
    input.type = 'file';
    if (images) input.accept = 'image/*';
    if (camera) input.setAttribute('capture', 'environment');
    if (many) input.multiple = true;
    input.style.display = 'none';
    let done = false;
    const finish = (files) => { if (done) return; done = true; input.remove(); resolve(files || []); };
    input.addEventListener('change', () => finish(Array.from(input.files || [])));
    input.addEventListener('cancel', () => finish([]));
    document.body.appendChild(input);
    input.click();
  });
}

let menu = null;
function closeMenu() {
  if (!menu) return;
  menu.remove();
  menu = null;
  const button = $('composer-photo-btn');
  if (button) button.setAttribute('aria-expanded', 'false');
  document.removeEventListener('mousedown', outside, true);
  document.removeEventListener('keydown', escape, true);
}
function outside(event) { if (menu && !menu.contains(event.target) && !event.target.closest('#composer-photo-btn')) closeMenu(); }
function escape(event) { if (event.key === 'Escape') { closeMenu(); const button = $('composer-photo-btn'); if (button) button.focus(); } }

function openMenu(button) {
  const canAttach = offered('overflow-attach-btn');
  const canIdentify = offered('overflow-identify-btn');
  const entries = [];
  if (hasCamera()) entries.push(['camera', 'Take a photo', () => choose({ camera: true }).then((files) => gotPhoto(files[0], canIdentify, canAttach))]);
  entries.push(['picture', 'Choose a photo', () => choose({}).then((files) => gotPhoto(files[0], canIdentify, canAttach))]);
  if (canAttach) entries.push(['clip', 'Attach files', () => choose({ images: false, many: true }).then((files) => { if (files.length) fileHandlerModule.addFiles(files); })]);

  menu = document.createElement('div');
  menu.className = 'photo-menu';
  menu.setAttribute('role', 'menu');
  menu.setAttribute('aria-label', 'Add a photo');
  entries.forEach(([icon, label, run]) => {
    const item = document.createElement('button');
    item.type = 'button';
    item.className = 'photo-menu-item';
    item.setAttribute('role', 'menuitem');
    item.innerHTML = svg(icon);
    item.appendChild(document.createTextNode(label));
    item.addEventListener('click', () => { closeMenu(); run().catch(() => {}); });      // the chooser opens inside this tap
    menu.appendChild(item);
  });
  // The message box clips what is inside it, so the menu lives on <body>,
  // placed above the button and kept on screen.
  document.body.appendChild(menu);
  const at = button.getBoundingClientRect();
  menu.style.left = `${Math.max(8, Math.min(at.left, window.innerWidth - menu.offsetWidth - 8))}px`;
  menu.style.bottom = `${Math.max(8, window.innerHeight - at.top + 8)}px`;
  button.setAttribute('aria-expanded', 'true');
  document.addEventListener('mousedown', outside, true);
  document.addEventListener('keydown', escape, true);
}

function attach(file) {
  fileHandlerModule.addFiles([file]).then(() => {
    const box = $('message');
    if (box) { if (!box.value.trim()) box.placeholder = 'Ask about the photo…'; box.focus(); }
  }).catch(() => {});
}

/** A photo has been taken or chosen: what should happen with it? */
function gotPhoto(file, canIdentify, canAttach) {
  if (!file) return;
  if (!canIdentify) { if (canAttach) attach(file); return; }
  if (!canAttach) { identifyPhoto(file, '').catch(() => {}); return; }

  const scrim = document.createElement('div');
  scrim.className = 'photo-scrim';
  const url = URL.createObjectURL(file);
  const close = () => { scrim.remove(); URL.revokeObjectURL(url); document.removeEventListener('keydown', onKey, true); };
  const onKey = (event) => { if (event.key === 'Escape') close(); };
  scrim.innerHTML = `
    <div class="photo-sheet" role="dialog" aria-modal="true" aria-label="What to do with this photo">
      <img class="photo-preview" alt="The photo you picked">
      <div class="photo-title">What would you like to do with it?</div>
      <button type="button" class="photo-choice primary" data-do="identify">
        <b>Identify it</b>
        <span>Name the plant, mushroom, insect, animal sign or object, with lookalikes and what the library says. Also reads labels and pages.</span>
      </button>
      <input type="text" class="photo-hint" maxlength="200" placeholder="Where and when you found it (optional)" aria-label="Where and when you found it (optional)">
      <button type="button" class="photo-choice" data-do="ask">
        <b>Ask about it</b>
        <span>Add it to your message, then type your question.</span>
      </button>
      <button type="button" class="photo-cancel" data-do="cancel">Cancel</button>
    </div>`;
  scrim.querySelector('.photo-preview').src = url;
  scrim.addEventListener('click', (event) => {
    const choice = event.target.closest('[data-do]');
    if (!choice && event.target !== scrim) return;
    const what = choice ? choice.dataset.do : 'cancel';
    const hint = scrim.querySelector('.photo-hint').value.trim();
    close();
    if (what === 'identify') identifyPhoto(file, hint).catch(() => {});
    if (what === 'ask') attach(file);
  });
  document.addEventListener('keydown', onKey, true);
  document.body.appendChild(scrim);
}

/** Wire the camera button. Safe to call once the message box exists. */
export function initPhotoButton() {
  const button = $('composer-photo-btn');
  if (!button || button._wired) return;
  button._wired = true;
  button.addEventListener('click', () => {
    if (menu) { closeMenu(); return; }
    if (!offered('overflow-attach-btn') && !offered('overflow-identify-btn')) return;
    openMenu(button);
  });
  // Nothing to offer (both hidden for this account): no button either.
  const sync = () => { button.style.display = (offered('overflow-attach-btn') || offered('overflow-identify-btn')) ? '' : 'none'; };
  sync();
  setTimeout(sync, 1500);          // account settings arrive a moment after the page
}

export default { initPhotoButton };
