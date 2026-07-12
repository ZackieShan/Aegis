/*
 * modalA11y.js — shared accessibility helper for self-contained modals.
 *
 * The Control Center / Canvas / Recipes panels each render their own overlay
 * and manage open/close, but none trapped focus, restored focus on close, or
 * exposed themselves as dialogs to assistive tech. This wraps that in two
 * calls:
 *
 *   const release = trapFocus(panelEl, { onEscape: close });
 *   // ...later, in your close():
 *   release();
 *
 * `panelEl` is the inner dialog element (not the full-screen backdrop). It is
 * tagged role="dialog" aria-modal="true", focus is moved inside, Tab is
 * cycled within it, Escape calls onEscape, and the previously-focused element
 * is restored when release() runs.
 */

const _FOCUSABLE = [
  'a[href]', 'button:not([disabled])', 'input:not([disabled])',
  'select:not([disabled])', 'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(',');

function _focusable(el) {
  return Array.from(el.querySelectorAll(_FOCUSABLE))
    .filter(n => n.offsetParent !== null || n === document.activeElement);
}

export function trapFocus(panelEl, opts = {}) {
  if (!panelEl) return () => {};
  const prevFocus = document.activeElement;

  if (!panelEl.hasAttribute('role')) panelEl.setAttribute('role', 'dialog');
  panelEl.setAttribute('aria-modal', 'true');
  if (!panelEl.hasAttribute('tabindex')) panelEl.setAttribute('tabindex', '-1');

  // Move focus inside — the first focusable control, else the panel itself.
  const first = _focusable(panelEl)[0];
  try { (first || panelEl).focus({ preventScroll: true }); } catch (_) {}

  const onKey = (e) => {
    if (e.key === 'Escape' && typeof opts.onEscape === 'function') {
      e.preventDefault();
      opts.onEscape();
      return;
    }
    if (e.key !== 'Tab') return;
    // Don't hijack Tab inside a textarea — code editors (the Canvas) use Tab
    // for indentation. Let the textarea's own handler run.
    const ae = document.activeElement;
    if (ae && ae.tagName === 'TEXTAREA') return;
    const items = _focusable(panelEl);
    if (!items.length) { e.preventDefault(); panelEl.focus(); return; }
    const firstEl = items[0], lastEl = items[items.length - 1];
    if (e.shiftKey && document.activeElement === firstEl) {
      e.preventDefault(); lastEl.focus();
    } else if (!e.shiftKey && document.activeElement === lastEl) {
      e.preventDefault(); firstEl.focus();
    } else if (!panelEl.contains(document.activeElement)) {
      // Focus escaped the dialog (e.g. after a re-render) — pull it back.
      e.preventDefault(); firstEl.focus();
    }
  };
  document.addEventListener('keydown', onKey, true);

  let released = false;
  return function release() {
    if (released) return;
    released = true;
    document.removeEventListener('keydown', onKey, true);
    panelEl.removeAttribute('aria-modal');
    // Restore focus to whatever opened the modal.
    if (prevFocus && typeof prevFocus.focus === 'function') {
      try { prevFocus.focus({ preventScroll: true }); } catch (_) {}
    }
  };
}

export default { trapFocus };
