// static/js/codeRunner.js

import * as uiModule from './ui.js';

/**
 * In-browser code runner for Python (Pyodide), JavaScript, and HTML
 */

// Python runs in a dedicated Web Worker (static/js/pyRunner.worker.js) with a
// locally-vendored Pyodide — see runPython for why (freeze-proofing + CSP).
let pyWorker = null;
let pyRunSeq = 0;

/**
 * Get or create an output panel below the <pre> element
 */
function getOrCreatePanel(pre) {
  let panel = pre.nextElementSibling;
  if (panel && panel.classList.contains('code-runner-output')) {
    panel.innerHTML = '';
    panel.style.display = 'block';
    return panel;
  }
  panel = document.createElement('div');
  panel.className = 'code-runner-output';
  pre.parentNode.insertBefore(panel, pre.nextSibling);
  return panel;
}

/**
 * Show a loading message in the panel
 */
function showLoading(panel, msg) {
  panel.innerHTML = `<div class="code-runner-loading">${msg}</div>`;
}

/**
 * Show output text in the panel
 */
function showOutput(panel, text, isError) {
  const el = document.createElement('pre');
  el.className = isError ? 'code-runner-pre code-runner-error' : 'code-runner-pre';
  el.textContent = text;
  panel.innerHTML = '';
  panel.appendChild(el);
  // Copy button — visible labeled pill at the top-right of the panel
  // itself (no separate footer / divider, no tiny icon corner).
  if (text) {
    const cbtn = document.createElement('button');
    cbtn.type = 'button';
    cbtn.className = 'code-runner-copy-inline';
    cbtn.innerHTML = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;margin-right:4px;"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>Copy';
    cbtn.addEventListener('click', (e) => {
      e.stopPropagation();
      e.preventDefault();
      let ok = false;
      try {
        const ta = document.createElement('textarea');
        ta.value = text;
        ta.style.cssText = 'position:fixed;left:0;top:0;width:1px;height:1px;opacity:0;';
        document.body.appendChild(ta);
        ta.focus();
        ta.select();
        ta.setSelectionRange(0, text.length);
        ok = document.execCommand && document.execCommand('copy');
        ta.remove();
      } catch (_) {}
      if (!ok && navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(text).then(() => {
          if (uiModule.showToast) uiModule.showToast('Copied');
          cbtn.textContent = 'Copied!';
          setTimeout(() => { cbtn.innerHTML = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;margin-right:4px;"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>Copy'; }, 1500);
        }).catch(() => { if (uiModule.showToast) uiModule.showToast('Copy failed'); });
        return;
      }
      if (uiModule.showToast) uiModule.showToast(ok ? 'Copied' : 'Copy failed');
      const orig = cbtn.innerHTML;
      cbtn.textContent = ok ? 'Copied!' : 'Copy failed';
      setTimeout(() => { cbtn.innerHTML = orig; }, 1500);
    });
    // Button lives directly in the panel — no wrapping bar. The panel is
    // position:relative so the button can sit absolute-top-right of it.
    panel.appendChild(cbtn);
  }
  if (isError) {
    setTimeout(() => { if (panel) panel.style.display = 'none'; }, 7000);
  }
}

/**
 * Legacy absolute-positioned copy button — replaced by the inline bar in
 * showOutput. Kept here as no-op so any earlier callers don't crash.
 */
function addCopyBtn_unused(panel, text) {
  if (!text) return;
  const btn = document.createElement('button');
  btn.type = 'button';  // Default <button> type is 'submit' — explicit "button" avoids any accidental form submission.
  btn.className = 'code-runner-copy';
  btn.title = 'Copy output';
  btn.innerHTML = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>';
  btn.addEventListener('click', async (e) => {
    e.stopPropagation();
    e.preventDefault();
    // Synchronous copy via a hidden textarea + execCommand — this is the
    // single most reliable path across browsers / non-secure contexts /
    // mobile Firefox. Run BEFORE any async navigator.clipboard attempt so
    // the user-gesture context is preserved.
    let ok = false;
    try {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.cssText = 'position:fixed;left:0;top:0;width:1px;height:1px;opacity:0;';
      document.body.appendChild(ta);
      ta.focus();
      ta.select();
      ta.setSelectionRange(0, text.length);
      ok = document.execCommand && document.execCommand('copy');
      ta.remove();
    } catch (_) {}
    // As a backup, also try the modern clipboard API (won't hurt if the
    // legacy path already copied).
    if (!ok && navigator.clipboard && window.isSecureContext) {
      try { await navigator.clipboard.writeText(text); ok = true; } catch (_) {}
    }
    if (uiModule && uiModule.showToast) {
      uiModule.showToast(ok ? 'Copied' : 'Copy failed');
    }
    const _orig = btn.innerHTML;
    btn.innerHTML = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>';
    btn.classList.add('copied');
    setTimeout(() => { btn.innerHTML = _orig; btn.classList.remove('copied'); }, 1500);
  });
  panel.prepend(btn);
}

/**
 * Add a collapse/close button to the panel.
 * Disabled \u2014 the run-output panel is now closed via the unified Code\u2194Run
 * toggle in the editor footer, so a separate X was redundant + cluttered.
 */
function addCloseBtn(_panel) { /* no-op */ }

function getPyWorker() {
  if (!pyWorker) pyWorker = new Worker('/static/js/pyRunner.worker.js', { type: 'module' });
  return pyWorker;
}

function killPyWorker() {
  if (pyWorker) {
    try { pyWorker.terminate(); } catch (_) {}
    pyWorker = null;
  }
}

// Display/GUI libraries that can't exist in the browser runtime — catch them
// before the run so the user gets a pointer instead of a stack trace.
const PY_GUI_RE = /^\s*(?:import|from)\s+(pygame|tkinter|turtle|kivy|PyQt\d*|PySide\d*)\b/m;

const PY_TIMEOUT_MS = 30000;

/**
 * Run Python code via Pyodide in a Web Worker.
 *
 * The worker (with a locally-vendored Pyodide — the app CSP is
 * connect-src 'self', and offline installs must still run code) keeps long
 * or infinite loops off the UI thread: the tab stays responsive, the
 * timeout actually fires, and Stop can terminate() a runaway script cold.
 */
export function runPython(code, panel) {
  const gui = code.match(PY_GUI_RE);
  if (gui) {
    showOutput(panel,
      `This program needs a display (${gui[1]}), and the in-browser Python runtime has none.\n` +
      'For games and UIs, regenerate as a single self-contained HTML file (set the language to HTML) — ' +
      'those render and run right in the panel.', true);
    addCloseBtn(panel);
    return;
  }

  panel.innerHTML = '';
  const status = document.createElement('div');
  status.className = 'code-runner-loading';
  status.textContent = pyWorker ? 'Running…' : 'Loading Python runtime (~13 MB, served locally, first run only)…';
  const stopBtn = document.createElement('button');
  stopBtn.type = 'button';
  stopBtn.className = 'code-runner-copy-inline';
  stopBtn.textContent = '■ Stop';
  panel.append(status, stopBtn);

  const worker = getPyWorker();
  const id = ++pyRunSeq;
  let settled = false;
  let timer = null;

  const finish = (fn) => {
    if (settled) return;
    settled = true;
    clearTimeout(timer);
    worker.removeEventListener('message', onMsg);
    worker.removeEventListener('error', onErr);
    fn();
    addCloseBtn(panel);
  };

  timer = setTimeout(() => finish(() => {
    killPyWorker();
    showOutput(panel,
      `Execution timed out (${PY_TIMEOUT_MS / 1000} s) — the Python worker was stopped; the app is unaffected.\n` +
      "Infinite game/render loops can't run here: for games, regenerate as an HTML file instead.", true);
  }), PY_TIMEOUT_MS);

  stopBtn.addEventListener('click', () => finish(() => {
    killPyWorker();
    showOutput(panel, 'Stopped.', true);
  }));

  const onMsg = (e) => {
    const d = e.data || {};
    if (d.id !== id) return;
    if (d.status === 'running') { status.textContent = 'Running…'; return; }
    finish(() => {
      if (d.error) showOutput(panel, d.error, true);
      else if (d.stderr) showOutput(panel, d.stderr, true);
      else showOutput(panel, d.stdout || '(no output)', false);
    });
  };
  const onErr = (e) => finish(() => {
    killPyWorker();
    showOutput(panel, 'Python runtime error: ' + ((e && e.message) || 'worker failed to start'), true);
  });

  worker.addEventListener('message', onMsg);
  worker.addEventListener('error', onErr);
  worker.postMessage({ id, code });
}

/**
 * Stage HTML on the server for the sandboxed run iframe. A srcdoc/blob
 * iframe inherits the app's nonce CSP (which blocks the generated page's
 * inline scripts), so run content is served from /api/canvas/preview/{token},
 * where the middleware applies a CSP `sandbox` policy instead: opaque
 * origin, scripts allowed, no cookies or API reach.
 */
async function stagePreview(html) {
  const r = await fetch('/api/canvas/preview', {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ code: html }),
  });
  const d = await r.json().catch(() => ({}));
  if (!r.ok || !d.url) throw new Error(d.detail || ('HTTP ' + r.status));
  return d.url;
}

/**
 * Run JavaScript code in a hidden sandboxed iframe, console output captured.
 */
export async function runJavaScript(code, panel) {
  showLoading(panel, 'Running...');

  const wrappedCode = `
<!DOCTYPE html><html><body><script>
var _logs = [];
console.log = function() { _logs.push([].map.call(arguments, function(a) { try { return typeof a === 'object' ? JSON.stringify(a) : String(a); } catch(e) { return String(a); } }).join(' ')); };
console.warn = function() { _logs.push('[warn] ' + [].map.call(arguments, String).join(' ')); };
console.error = function() { _logs.push('[error] ' + [].map.call(arguments, String).join(' ')); };
try {
  var _timer = setTimeout(function() { parent.postMessage({error:'Execution timed out (10 s)'},'*'); }, 10000);
  ${code.replace(/<\/script>/gi, '<\\/script>')}
  clearTimeout(_timer);
  parent.postMessage({logs: _logs}, '*');
} catch(e) {
  parent.postMessage({error: e.toString()}, '*');
}
<\/script></body></html>`;

  let url;
  try {
    url = await stagePreview(wrappedCode);
  } catch (e) {
    showOutput(panel, 'Could not stage the run: ' + e.message, true);
    addCloseBtn(panel);
    return;
  }

  const iframe = document.createElement('iframe');
  iframe.style.display = 'none';
  iframe.sandbox = 'allow-scripts';
  document.body.appendChild(iframe);

  let settled = false;
  const cleanup = () => {
    if (iframe.parentNode) iframe.remove();
  };

  const failsafe = setTimeout(() => {
    if (!settled) {
      settled = true;
      showOutput(panel, 'Execution timed out (10 s)', true);
      addCloseBtn(panel);
      cleanup();
    }
  }, 15000);

  const onMessage = (e) => {
    if (e.source !== iframe.contentWindow) return;
    if (settled) return;
    settled = true;
    clearTimeout(failsafe);
    window.removeEventListener('message', onMessage);

    const data = e.data;
    panel.innerHTML = '';
    if (data.error) {
      showOutput(panel, data.error, true);
    } else if (data.logs && data.logs.length > 0) {
      showOutput(panel, data.logs.join('\n'), false);
    } else {
      showOutput(panel, '(no output)', false);
    }
    addCloseBtn(panel);
    cleanup();
  };

  window.addEventListener('message', onMessage);
  iframe.src = url;
}

/**
 * Run code server-side via POST /api/shell/exec
 */
export async function runServer(code, panel, lang) {
  showLoading(panel, 'Running on server...');
  // Base64-encode the script so newlines survive the shell quoting intact.
  // JSON.stringify turns \n into literal \\n which python3 -c sees as backslash-n;
  // base64 avoids every quoting/escaping pitfall.
  const b64 = btoa(unescape(encodeURIComponent(code)));
  var command;
  if (lang === 'python' || lang === 'py') {
    command = `python3 -c "import base64; exec(base64.b64decode('${b64}').decode('utf-8'))"`;
  } else {
    command = `python3 -c "import base64, subprocess, sys; sys.exit(subprocess.run(['bash','-c',base64.b64decode('${b64}').decode('utf-8')]).returncode)"`;
  }
  try {
    var res = await fetch('/api/shell/exec', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ command: command }),
    });
    var data = await res.json();
    panel.innerHTML = '';
    if (data.stderr && data.stderr.trim()) {
      showOutput(panel, data.stderr, true);
      if (data.stdout && data.stdout.trim()) {
        var stdoutEl = document.createElement('pre');
        stdoutEl.className = 'code-runner-pre';
        stdoutEl.textContent = data.stdout;
        panel.appendChild(stdoutEl);
      }
    } else if (data.stdout && data.stdout.trim()) {
      showOutput(panel, data.stdout, false);
    } else {
      showOutput(panel, '(no output)' + (data.exit_code ? ' — exit code ' + data.exit_code : ''), !data.exit_code ? false : true);
    }
    if (data.exit_code && data.exit_code !== 0) {
      var exitEl = document.createElement('div');
      exitEl.style.cssText = 'font-size:0.75rem;opacity:0.5;padding:2px 8px;';
      exitEl.textContent = 'Exit code: ' + data.exit_code;
      panel.appendChild(exitEl);
    }
  } catch (e) {
    showOutput(panel, 'Execution failed: ' + e.message, true);
  }
  addCloseBtn(panel);
}

/**
 * Run HTML live in a visible sandboxed iframe inside the output panel —
 * games and UIs play right where the code is, with an "open in window"
 * escape hatch for more room. The page is served from the canvas preview
 * endpoint (opaque-origin CSP sandbox), so its scripts run but it can't
 * touch cookies or the API.
 */
export async function runHTML(code, panel) {
  showLoading(panel, 'Preparing preview…');

  let url;
  try {
    url = await stagePreview(code);
  } catch (e) {
    showOutput(panel, 'Could not stage the preview: ' + e.message, true);
    addCloseBtn(panel);
    return;
  }

  panel.innerHTML = '';
  const openBtn = document.createElement('button');
  openBtn.type = 'button';
  openBtn.className = 'code-runner-copy-inline';
  openBtn.textContent = 'Open in window ↗';
  openBtn.title = 'Run in a bigger separate window';
  openBtn.addEventListener('click', () => {
    // noopener severs window.opener so the run page can't reach back into the
    // app (belt-and-suspenders — the preview is already served opaque-origin).
    const win = window.open(url, '_blank', 'noopener,width=920,height=720,menubar=no,toolbar=no,location=no,status=no');
    if (!win && uiModule.showToast) uiModule.showToast('Popup blocked — allow popups for this site.');
  });

  const iframe = document.createElement('iframe');
  iframe.className = 'code-runner-frame';
  iframe.sandbox = 'allow-scripts allow-pointer-lock';
  iframe.src = url;
  iframe.style.cssText = 'width:100%;height:480px;border:1px solid rgba(128,128,128,0.3);border-radius:10px;background:#fff;display:block;';
  panel.append(iframe, openBtn);
  addCloseBtn(panel);
  // Click-to-focus so keyboard games work without a hint; autofocus attempt
  // is best-effort (sandboxed cross-origin content controls its own focus).
  setTimeout(() => { try { iframe.focus(); } catch (_) {} }, 250);
}

/**
 * Main entry point — called when a Run button is clicked
 */
export function run(btn) {
  const code = btn.getAttribute('data-code');
  const lang = (btn.getAttribute('data-lang') || '').toLowerCase();
  if (!code) return;

  const pre = btn.closest('pre');
  if (!pre) return;

  const panel = getOrCreatePanel(pre);

  if (lang === 'bash' || lang === 'sh' || lang === 'shell' || lang === 'zsh') {
    runServer(code, panel, 'bash');
  } else if (lang === 'python' || lang === 'py') {
    runServer(code, panel, 'python');
  } else if (lang === 'javascript' || lang === 'js') {
    runJavaScript(code, panel);
  } else if (lang === 'html') {
    runHTML(code, panel);
  }
}

const codeRunnerModule = { run, runPython, runJavaScript, runHTML, runServer };
export default codeRunnerModule;
