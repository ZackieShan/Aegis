// static/js/researchButton.js
//
// Deep Research, on the message box.
//
// In a chat there was no visible way to start Deep Research: the sidebar has
// an entry (behind the menu button on a phone), and the only thing on the
// message box was a typed command. This puts a button on the bar and an entry
// in "More tools". Both open the Deep Research window — the same one the
// sidebar opens — and carry over whatever is typed in the message box, so
// "type what you want looked into, tap the microscope, tap Start" is the whole
// of it.
//
// Nothing new happens behind it: the window, its settings and its reports are
// the existing ones (static/js/research/panel.js).

const $ = (id) => document.getElementById(id);
const BUTTONS = ['composer-research-btn', 'overflow-deepresearch-btn'];

/** Open (or bring back) the Deep Research window. */
function showWindow() {
  const panel = window.researchPanelModule;          // set by app.js: the one instance the sidebar uses
  if (panel && typeof panel.openPanel === 'function') { panel.openPanel(); return true; }
  const entry = $('tool-research-btn') || $('rail-research');
  if (entry && !(panel && panel.isOpen && panel.isOpen())) { entry.click(); return true; }
  return !!entry;
}

/** What is typed in the message box, if it is a request and not a command. */
function typedRequest() {
  const box = $('message');
  const text = box ? String(box.value || '').trim() : '';
  return text && !text.startsWith('/') ? text : '';
}

function openResearch() {
  const typed = typedRequest();
  if (!showWindow()) return;
  let tries = 0;
  const handOver = () => {
    const topic = $('research-query');
    if (!topic) { if (++tries < 40) setTimeout(handOver, 50); return; }
    let moved = false;
    if (typed && !topic.value.trim()) {
      topic.value = typed;
      topic.dispatchEvent(new Event('input', { bubbles: true }));
      const box = $('message');
      if (box) { box.value = ''; box.dispatchEvent(new Event('input', { bubbles: true })); }
      moved = true;
    }
    // With the topic already filled in, leave the keyboard down on a phone so
    // Start is in view; otherwise put the cursor where the typing goes.
    const phone = window.matchMedia && window.matchMedia('(pointer: coarse)').matches;
    if (moved && phone) { const start = $('research-start-btn'); if (start) start.scrollIntoView({ block: 'nearest' }); }
    else { try { topic.focus(); topic.setSelectionRange(topic.value.length, topic.value.length); } catch (_) { /* not focusable yet */ } }
  };
  handOver();
}

/** An administrator can take Deep Research away from an account; then there is no shortcut either. */
function allowed() {
  const forced = Array.isArray(window._forcedHiddenUI) ? window._forcedHiddenUI : [];
  if (forced.includes('tool-research') || forced.includes('research-btn')) return false;
  if (window._aegisResearchAllowed === false) return false;        // set by init.js from the account's privileges
  return true;
}

export function initResearchButton() {
  let wired = false;
  BUTTONS.forEach((id) => {
    const button = $(id);
    if (!button || button._wired) return;
    button._wired = true;
    wired = true;
    button.addEventListener('click', () => openResearch());
  });
  if (!wired) return;
  const sync = () => { const ok = allowed(); BUTTONS.forEach((id) => { const button = $(id); if (button) button.hidden = !ok; }); };
  sync();
  setTimeout(sync, 1500);          // account settings arrive a moment after the page
  setTimeout(sync, 5000);
}

export default { initResearchButton, openResearch };
