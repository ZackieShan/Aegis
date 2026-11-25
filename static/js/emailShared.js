const API_BASE = window.location.origin;

export function emailAccountQuery(prefix = '&') {
  const accountId = window.__aegisActiveEmailAccount || '';
  if (!accountId) return '';
  const lead = prefix === '?' ? '?' : '&';
  return `${lead}account_id=${encodeURIComponent(accountId)}`;
}

export function emailApiUrl(path, params = {}) {
  const url = new URL(`${API_BASE}${path}`);
  const accountId = window.__aegisActiveEmailAccount || '';
  if (accountId) url.searchParams.set('account_id', accountId);
  Object.entries(params || {}).forEach(([key, value]) => {
    if (value === undefined || value === null || value === '') return;
    url.searchParams.set(key, String(value));
  });
  return url.toString();
}

// ── the set of accounts changed ─────────────────────────────────────────────
//
// The email screens keep the selected account in memory (and in
// window.__aegisActiveEmailAccount, which emailApiUrl puts on every request).
// Accounts are added, edited, deleted and re-defaulted from seven places in
// Settings; none of them told the email screens. So after deleting an account
// the inbox went on asking for it — every folder, list and unread check came
// back 404 — and a newly added account was never switched to: "I can't remove
// accounts, and adding one with a good app password doesn't work".
//
// Rather than touch each of those places (and miss the next one), watch for
// the request itself: any successful change under /api/email/accounts fires
// this event, and the email screens reload their accounts.
export const EMAIL_ACCOUNTS_CHANGED = 'aegis-email-accounts-changed';

export function notifyEmailAccountsChanged(detail = {}) {
  try { window.dispatchEvent(new CustomEvent(EMAIL_ACCOUNTS_CHANGED, { detail })); } catch (_) { /* no window */ }
}

// An account id the server no longer knows (it answered 404 for it): ask for
// a reload once per id, however many requests were in flight with it.
const _reportedMissing = new Set();
export function reportMissingEmailAccount(accountId) {
  const id = String(accountId || '');
  if (!id || _reportedMissing.has(id)) return false;
  _reportedMissing.add(id);
  notifyEmailAccountsChanged({ missing: id });
  return true;
}

(function watchAccountChanges() {
  try {
    if (window.__aegisEmailAccountWatch || typeof window.fetch !== 'function') return;
    window.__aegisEmailAccountWatch = true;
    const original = window.fetch;
    window.fetch = function watchedFetch(input, init) {
      const result = original.apply(this, arguments);
      try {
        const url = typeof input === 'string' ? input : ((input && input.url) || '');
        const method = String((init && init.method) || (input && input.method) || 'GET').toUpperCase();
        const path = url.replace(/^https?:\/\/[^/]+/, '').split('?')[0];
        if (method !== 'GET' && /^\/api\/email\/accounts(\/|$)/.test(path) && !/\/accounts\/test$/.test(path)) {
          result.then((res) => { if (res && res.ok) notifyEmailAccountsChanged({ method, path }); }).catch(() => {});
        }
      } catch (_) { /* never get in the way of the request */ }
      return result;
    };
  } catch (_) { /* not in a browser */ }
})();
