/*
 * voice-mode/index.js — hands-free "speak to your PC".
 *
 * The conversational loop the composer mic doesn't do: press to talk → the
 * recording is transcribed (local Whisper, /api/stt/transcribe) → dropped into
 * the composer and SENT automatically → the agent acts (incl. driving the
 * browser once the Browser MCP is connected) → optionally the reply is read
 * back aloud (window.aiTTSManager). Reuses the existing STT + TTS; this is just
 * the glue + a hands-free UI.
 *
 * Opened by #rail-voice or the /voice command. Toggling starts/stops listening.
 */

let _rec = null, _chunks = [], _stream = null, _listening = false, _busy = false;

function _el(tag, cls, html) { const e = document.createElement(tag); if (cls) e.className = cls; if (html != null) e.innerHTML = html; return e; }
function _speakReplies() { return localStorage.getItem('aegis_voice_speak') === '1'; }

function _styles() {
  if (document.getElementById('voice-mode-styles')) return;
  const s = _el('style'); s.id = 'voice-mode-styles';
  s.textContent = `
  #voice-mode-fab { position: fixed; right: 20px; bottom: 84px; z-index: 3500;
    display: flex; flex-direction: column; align-items: flex-end; gap: 8px; }
  #voice-mode-fab.hidden { display: none; }
  .vm-orb { width: 56px; height: 56px; border-radius: 50%; border: none; cursor: pointer;
    background: var(--red,#b45de0); color: #fff; display: flex; align-items: center; justify-content: center;
    box-shadow: 0 6px 20px rgba(0,0,0,.4); transition: transform .1s; }
  .vm-orb:hover { transform: scale(1.05); }
  .vm-orb.listening { background: #e5484d; animation: vm-pulse 1.2s ease-in-out infinite; }
  .vm-orb.busy { opacity: .7; cursor: default; }
  @keyframes vm-pulse { 0%,100% { box-shadow: 0 0 0 0 rgba(229,72,77,.5); } 50% { box-shadow: 0 0 0 14px rgba(229,72,77,0); } }
  .vm-status { background: var(--panel,#120a1c); color: var(--fg,#cbb8ec); border: 1px solid var(--border,#3a2657);
    border-radius: 10px; padding: 7px 12px; font-size: .78rem; max-width: 260px; box-shadow: 0 6px 20px rgba(0,0,0,.35); }
  .vm-row { display: flex; align-items: center; gap: 6px; margin-top: 6px; font-size: .72rem; opacity: .85; }
  .vm-row input { accent-color: var(--red,#b45de0); }
  .vm-close { background: none; border: none; color: var(--fg,#cbb8ec); opacity: .6; cursor: pointer; font-size: .72rem; }
  `;
  document.head.appendChild(s);
}

function _fab() { return document.getElementById('voice-mode-fab'); }
function _orb() { return document.getElementById('vm-orb'); }

function _build() {
  if (_fab()) return;
  _styles();
  const wrap = _el('div', 'hidden'); wrap.id = 'voice-mode-fab';
  wrap.innerHTML = `
    <div class="vm-status" id="vm-status">
      <div id="vm-msg">Voice mode — tap the mic and speak.</div>
      <div class="vm-row"><label><input type="checkbox" id="vm-speak"> Speak replies</label>
        <span style="flex:1"></span><button class="vm-close" id="vm-hide">hide</button></div>
    </div>
    <button class="vm-orb" id="vm-orb" title="Tap to talk">
      <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="2" width="6" height="12" rx="3"/><path d="M5 10a7 7 0 0 0 14 0"/><line x1="12" y1="19" x2="12" y2="22"/></svg>
    </button>`;
  document.body.appendChild(wrap);
  _orb().addEventListener('click', toggleListen);
  const sp = wrap.querySelector('#vm-speak'); sp.checked = _speakReplies();
  sp.addEventListener('change', () => localStorage.setItem('aegis_voice_speak', sp.checked ? '1' : '0'));
  wrap.querySelector('#vm-hide').addEventListener('click', hide);
}

function _status(msg) { const m = document.getElementById('vm-msg'); if (m) m.textContent = msg; }

async function _startRec() {
  if (!navigator.mediaDevices?.getUserMedia) { _status('Mic not available in this browser.'); return false; }
  try {
    _stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (e) { _status('Mic permission denied.'); return false; }
  _chunks = [];
  const mime = ['audio/webm;codecs=opus', 'audio/webm', 'audio/ogg'].find(m => window.MediaRecorder && MediaRecorder.isTypeSupported(m)) || '';
  _rec = new MediaRecorder(_stream, mime ? { mimeType: mime } : undefined);
  _rec.ondataavailable = (e) => { if (e.data && e.data.size) _chunks.push(e.data); };
  _rec.onstop = _onStop;
  _rec.start();
  _listening = true;
  _orb().classList.add('listening');
  _status('Listening… tap again when done.');
  return true;
}

function _stopRec() {
  _listening = false;
  _orb().classList.remove('listening');
  try { _rec && _rec.state !== 'inactive' && _rec.stop(); } catch {}
  try { _stream && _stream.getTracks().forEach(t => t.stop()); } catch {}
}

async function _onStop() {
  const blob = new Blob(_chunks, { type: (_rec && _rec.mimeType) || 'audio/webm' });
  if (!blob.size) { _status('Didn’t catch that — try again.'); return; }
  _busy = true; _orb().classList.add('busy'); _status('Transcribing…');
  try {
    const fd = new FormData(); fd.append('file', blob, 'audio.webm');
    const r = await fetch('/api/stt/transcribe', { method: 'POST', credentials: 'same-origin', body: fd });
    if (!r.ok) { const e = await r.json().catch(() => ({})); throw new Error(e.detail?.message || 'transcription failed'); }
    const text = ((await r.json()).text || '').trim();
    if (!text) { _status('Heard silence — try again.'); return; }
    _status('“' + text + '”');
    _send(text);
  } catch (e) { _status('✗ ' + e.message); }
  finally { _busy = false; _orb().classList.remove('busy'); }
}

function _send(text) {
  const input = document.getElementById('message');
  const form = document.getElementById('chat-form');
  if (!input || !form) { _status('Chat not ready.'); return; }
  input.value = text;
  input.dispatchEvent(new Event('input', { bubbles: true }));
  if (_speakReplies()) _armSpeakBack();
  try { form.requestSubmit(); } catch { form.dispatchEvent(new Event('submit', { cancelable: true, bubbles: true })); }
}

// Best-effort: watch the chat area for the reply, speak it once it settles.
function _armSpeakBack() {
  const area = document.querySelector('.chat-container') || document.querySelector('#chat-messages') || document.body;
  if (!area || !window.aiTTSManager) return;
  let lastText = '', timer = null, seen = false;
  const obs = new MutationObserver(() => {
    const nodes = area.querySelectorAll('.message, [data-role="assistant"], .assistant, .ai-message');
    const last = nodes[nodes.length - 1];
    if (!last) return;
    const txt = (last.innerText || '').trim();
    if (!txt) return;
    seen = true; lastText = txt;
    clearTimeout(timer);
    timer = setTimeout(() => {  // no updates for 1.4s → reply is done
      obs.disconnect();
      try { window.aiTTSManager.play(lastText.slice(0, 1200)); } catch {}
    }, 1400);
  });
  obs.observe(area, { childList: true, subtree: true, characterData: true });
  setTimeout(() => { if (!seen) obs.disconnect(); }, 30000);  // give up after 30s
}

export function toggleListen() {
  if (_busy) return;
  _listening ? _stopRec() : _startRec();
}
export function show() { _build(); _fab().classList.remove('hidden'); }
export function hide() { if (_listening) _stopRec(); const f = _fab(); if (f) f.classList.add('hidden'); }
export function toggle() { _build(); _fab().classList.contains('hidden') ? show() : hide(); }

const voiceModeModule = { show, hide, toggle, toggleListen };
export default voiceModeModule;
window.voiceModeModule = voiceModeModule;

function _wire() {
  const btn = document.getElementById('rail-voice');
  if (btn && !btn._vmWired) { btn._vmWired = true; btn.addEventListener('click', toggle); }
}
if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', _wire);
else _wire();
