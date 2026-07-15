/* pyRunner.worker.js — Pyodide off the main thread (MODULE worker).
 *
 * Python Run used to execute Pyodide on the UI thread, so any long/infinite
 * loop (every game has one) froze the whole tab AND disarmed the timeout —
 * the Promise.race timer needs the very event loop the WASM was blocking.
 * In a worker the tab stays responsive and the main thread can terminate()
 * a runaway script cold.
 *
 * Module worker importing pyodide.mjs (the documented worker pattern; the
 * classic importScripts shim path is flakier). Pyodide is vendored at
 * /static/lib/pyodide (no CDN: the app CSP is connect-src 'self', and
 * offline installs must still run code).
 *
 * CRITICAL: this worker script is served with its OWN CSP carrying
 * 'wasm-unsafe-eval' (see SecurityHeadersMiddleware). The app-wide script-src
 * omits it, which silently blocks WebAssembly.compile — that was why
 * in-browser Python appeared to hang forever. A network-fetched worker uses
 * its own response CSP rather than inheriting the page's, so the wasm
 * capability is scoped to just this file.
 *
 * Protocol: {id, code} in → {id, status:'running'} then
 * {id, stdout, stderr} | {id, error} out.
 */
import { loadPyodide } from '/static/lib/pyodide/pyodide.mjs';

let pyReady = null;

function ensurePyodide() {
  if (!pyReady) pyReady = loadPyodide({ indexURL: '/static/lib/pyodide/' });
  return pyReady;
}

self.onmessage = async (e) => {
  const { id, code } = e.data || {};
  if (typeof code !== 'string') return;
  let py;
  try {
    py = await ensurePyodide();
  } catch (err) {
    self.postMessage({ id, error: 'Failed to load the Python runtime: ' + ((err && err.message) || err) });
    return;
  }
  self.postMessage({ id, status: 'running' });

  const wrapper = `
import sys, io
_stdout = io.StringIO()
_stderr = io.StringIO()
sys.stdout = _stdout
sys.stderr = _stderr
try:
    exec(compile(${JSON.stringify(code)}, "<canvas>", "exec"), {"__name__": "__main__"})
except SystemExit:
    pass
except BaseException:
    import traceback
    _stderr.write(traceback.format_exc(limit=8))
finally:
    sys.stdout = sys.__stdout__
    sys.stderr = sys.__stderr__
(_stdout.getvalue(), _stderr.getvalue())
`;

  try {
    const result = await py.runPythonAsync(wrapper);
    const arr = result.toJs ? result.toJs() : result;
    if (result.destroy) result.destroy();
    self.postMessage({ id, stdout: arr[0] || '', stderr: arr[1] || '' });
  } catch (err) {
    self.postMessage({ id, error: String((err && err.message) || err) });
  }
};
