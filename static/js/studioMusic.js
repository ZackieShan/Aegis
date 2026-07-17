/**
 * Studio → Music: the Music Maker.
 *
 * Three sections, all controls visible:
 *   1. Song Composer — the audio maker panel (tags, lyrics, cover reference,
 *      seconds, seed, model) rendered by studioCreate with the kind locked.
 *   2. Tracks — a real player over everything in the library's Music section:
 *      play/pause per row, prev/next, seek, auto-advance. The <audio> element
 *      lives OUTSIDE the modal DOM, so a song keeps playing while you browse
 *      other tabs or close the Studio; the bar just re-attaches on re-open.
 *   3. Voice Lab — pick/preview the app's speaking voice and clone your own
 *      (record ~10s, name it, save) without hunting through Settings.
 */

import { renderCreateTab } from './studioCreate.js';

let _tracks = [];
let _current = -1;            // index into _tracks of the loaded track
let _host = null;

// The player outlives the Studio modal on purpose — music keeps playing.
const _audio = window.__studioAudio || (window.__studioAudio = new Audio());
_audio.preload = 'metadata';

let _rec = null, _recChunks = [], _recStream = null, _recTimer = null;
let _previewUrl = null, _previewAudio = null;

function _el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

async function _json(url, opts) {
  const r = await fetch(url, { credentials: 'same-origin', ...opts });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(d.detail?.message || d.detail || d.error || `Request failed (${r.status})`);
  return d;
}

function _esc(s) { return String(s == null ? '' : s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])); }

function _fmtTime(s) {
  if (!isFinite(s) || s < 0) return '0:00';
  const m = Math.floor(s / 60), sec = Math.floor(s % 60);
  return `${m}:${String(sec).padStart(2, '0')}`;
}

function _styles() {
  if (document.getElementById('studio-music-styles')) return;
  const st = _el('style'); st.id = 'studio-music-styles';
  st.textContent = `
  .music-section { margin-top: 22px; }
  .music-section h3 { margin: 0 0 4px; font-size: .95rem; }
  .music-sub { font-size: .76rem; opacity: .65; margin-bottom: 10px; }
  .music-tracks { display: flex; flex-direction: column; gap: 4px; }
  .music-row { display: flex; align-items: center; gap: 10px; padding: 7px 10px;
    border: 1px solid var(--border,#3a2657); border-radius: 9px; background: rgba(255,255,255,.02); }
  .music-row.playing { border-color: var(--accent, var(--red,#b45de0)); }
  .music-play { width: 30px; height: 30px; border-radius: 50%; border: none; cursor: pointer; flex-shrink: 0;
    background: var(--accent, var(--red,#b45de0)); color: #fff; font-size: .8rem; line-height: 1; }
  .music-title { flex: 1; min-width: 0; font-size: .82rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .music-meta { font-size: .7rem; opacity: .55; flex-shrink: 0; }
  .music-bar { position: sticky; bottom: 0; margin-top: 12px; display: flex; align-items: center; gap: 10px;
    padding: 10px 12px; border: 1px solid var(--border,#3a2657); border-radius: 11px;
    background: var(--panel,#120a1c); box-shadow: 0 -4px 16px rgba(0,0,0,.25); }
  .music-bar-title { flex: 1; min-width: 0; font-size: .78rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .music-bar button { background: transparent; border: 1px solid var(--border,#3a2657); color: var(--fg,#cbb8ec);
    border-radius: 7px; padding: 4px 9px; cursor: pointer; font-size: .8rem; }
  .music-bar button.primary { background: var(--accent, var(--red,#b45de0)); color: #fff; border: none; }
  .music-seek { flex: 2; accent-color: var(--accent, var(--red,#b45de0)); min-width: 120px; }
  .music-time { font-size: .7rem; opacity: .6; font-variant-numeric: tabular-nums; }
  .voice-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 8px; }
  .voice-row select, .voice-row input[type=text] { background: var(--input-bg, var(--panel,#120a1c));
    color: var(--fg,#cbb8ec); border: 1px solid var(--border,#3a2657); border-radius: 7px; padding: 5px 9px; font-size: .8rem; }
  .voice-row button { background: transparent; border: 1px solid var(--border,#3a2657); color: var(--fg,#cbb8ec);
    border-radius: 7px; padding: 5px 11px; cursor: pointer; font-size: .78rem; }
  .voice-row button.rec { border-color: #e5484d; color: #e5484d; }
  .voice-row button.rec.recording { background: #e5484d; color: #fff; animation: music-pulse 1.1s infinite; }
  @keyframes music-pulse { 50% { opacity: .6; } }
  .voice-status { font-size: .74rem; opacity: .7; min-height: 1.1em; }
  `;
  document.head.appendChild(st);
}

// ── player ────────────────────────────────────────────────────────────────────
function _trackTitle(t) {
  const p = (t.prompt || t.caption || '').trim();
  return p ? (p.length > 90 ? p.slice(0, 90) + '…' : p) : t.filename;
}

function _playIndex(i) {
  if (i < 0 || i >= _tracks.length) return;
  const t = _tracks[i];
  if (_current === i && !_audio.paused) { _audio.pause(); _syncUI(); return; }
  if (_current !== i) {
    _audio.src = t.url;
    _current = i;
    window.__studioAudioMeta = { title: _trackTitle(t), id: t.id };
  }
  _audio.play().catch(() => {});
  _syncUI();
}

function _syncUI() {
  if (!_host || !_host.isConnected) return;
  _host.querySelectorAll('.music-row').forEach((row, i) => {
    row.classList.toggle('playing', i === _current);
    const btn = row.querySelector('.music-play');
    if (btn) btn.textContent = (i === _current && !_audio.paused) ? '❚❚' : '▶';
  });
  const bar = _host.querySelector('.music-bar');
  if (!bar) return;
  const meta = window.__studioAudioMeta;
  // A loaded track stays "now playing" even if it's no longer in the list
  // (deleted / list re-sorted) — _current may be -1 while audio still plays.
  bar.querySelector('.music-bar-title').textContent =
    (meta && _audio.src) ? meta.title : 'Nothing playing — pick a track above.';
  bar.querySelector('.music-toggle').textContent = _audio.paused ? '▶' : '❚❚';
  // Refresh seek/time immediately (not only on timeupdate) so a re-rendered
  // bar reflects a PAUSED track instead of showing 0:00 until it next plays.
  const seek = bar.querySelector('.music-seek');
  const time = bar.querySelector('.music-time');
  if (seek && !seek.matches(':active') && isFinite(_audio.duration) && _audio.duration > 0) {
    seek.value = String(Math.round((_audio.currentTime / _audio.duration) * 1000));
  }
  if (time) time.textContent = `${_fmtTime(_audio.currentTime)} / ${_fmtTime(_audio.duration)}`;
}

function _wireAudioEvents() {
  // Re-wired on each render; use a marker so we never stack listeners.
  if (_audio.__studioWired) return;
  _audio.__studioWired = true;
  _audio.addEventListener('timeupdate', () => {
    if (!_host || !_host.isConnected) return;
    const seek = _host.querySelector('.music-seek');
    const time = _host.querySelector('.music-time');
    if (seek && !seek.matches(':active') && isFinite(_audio.duration) && _audio.duration > 0) {
      seek.value = String(Math.round((_audio.currentTime / _audio.duration) * 1000));
    }
    if (time) time.textContent = `${_fmtTime(_audio.currentTime)} / ${_fmtTime(_audio.duration)}`;
  });
  _audio.addEventListener('ended', () => {
    if (_current >= 0 && _current + 1 < _tracks.length) _playIndex(_current + 1);
    else _syncUI();
  });
  _audio.addEventListener('play', _syncUI);
  _audio.addEventListener('pause', _syncUI);
}

async function _renderTracks(section) {
  section.replaceChildren();
  section.appendChild(_el('h3', '', 'Tracks'));
  section.appendChild(_el('div', 'music-sub',
    'Every song in your Studio. Playback keeps going while you browse other tabs.'));
  let items = [];
  try {
    // limit is server-validated to <=100 — a bigger ask is a 422, not more rows.
    const d = await _json('/api/gallery/library?kind=music&sort=recent&limit=100');
    items = d.items || [];
  } catch (e) {
    section.appendChild(_el('div', 'movie-empty', 'Could not load tracks: ' + e.message));
    return;
  }
  _tracks = items;
  // If the persistent player already has a track loaded, recover its index.
  const meta = window.__studioAudioMeta;
  _current = meta ? _tracks.findIndex(t => t.id === meta.id) : -1;

  if (!items.length) {
    section.appendChild(_el('div', 'movie-empty',
      'No songs yet — compose one above and it lands here.'));
    return;
  }
  const list = _el('div', 'music-tracks');
  items.forEach((t, i) => {
    const row = _el('div', 'music-row');
    const play = _el('button', 'music-play', '▶');
    play.type = 'button';
    play.title = 'Play / pause';
    play.addEventListener('click', () => _playIndex(i));
    row.appendChild(play);
    const title = _el('div', 'music-title', _trackTitle(t));
    title.title = t.prompt || t.filename;
    row.appendChild(title);
    const when = (t.created_at || '').slice(0, 10);
    row.appendChild(_el('div', 'music-meta', [t.model, when].filter(Boolean).join(' · ')));
    list.appendChild(row);
  });
  section.appendChild(list);

  // Now-playing bar
  const bar = _el('div', 'music-bar');
  const prev = _el('button', '', '⏮'); prev.type = 'button'; prev.title = 'Previous';
  prev.addEventListener('click', () => { if (_current > 0) _playIndex(_current - 1); });
  const toggle = _el('button', 'primary music-toggle', '▶'); toggle.type = 'button'; toggle.title = 'Play / pause';
  toggle.addEventListener('click', () => {
    // If a track is loaded (even one no longer in the list), resume it —
    // only fall back to track 0 when the player is truly empty.
    if (!_audio.src && _current < 0 && _tracks.length) { _playIndex(0); return; }
    if (_audio.paused) _audio.play().catch(() => {}); else _audio.pause();
    _syncUI();
  });
  const next = _el('button', '', '⏭'); next.type = 'button'; next.title = 'Next';
  next.addEventListener('click', () => { if (_current + 1 < _tracks.length) _playIndex(_current + 1); });
  bar.appendChild(prev); bar.appendChild(toggle); bar.appendChild(next);
  bar.appendChild(_el('div', 'music-bar-title', ''));
  const seek = document.createElement('input');
  seek.type = 'range'; seek.min = '0'; seek.max = '1000'; seek.value = '0';
  seek.className = 'music-seek';
  seek.addEventListener('input', () => {
    if (isFinite(_audio.duration) && _audio.duration > 0) {
      _audio.currentTime = (Number(seek.value) / 1000) * _audio.duration;
    }
  });
  bar.appendChild(seek);
  bar.appendChild(_el('span', 'music-time', '0:00 / 0:00'));
  section.appendChild(bar);
  _wireAudioEvents();
  _syncUI();
}

// ── voice lab ─────────────────────────────────────────────────────────────────
function _voiceStatus(section, msg, isErr) {
  const s = section.querySelector('.voice-status');
  if (s) { s.textContent = msg || ''; s.style.color = isErr ? 'var(--accent-error, #e5484d)' : ''; }
}

async function _previewVoice(section, voice) {
  try {
    _voiceStatus(section, 'Synthesizing preview…');
    const r = await fetch('/api/tts/synthesize', {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: 'This is how I sound. Every word rendered locally, on your machine.', voice }),
    });
    if (!r.ok) { const d = await r.json().catch(() => ({})); throw new Error(d.detail?.message || `Preview failed (${r.status})`); }
    const blob = await r.blob();
    // Stop the previous preview BEFORE revoking its URL (revoking a playing
    // blob URL kills its audio mid-word) and keep a ref so it isn't GC'd.
    if (_previewAudio) { try { _previewAudio.pause(); } catch (_) {} }
    const oldUrl = _previewUrl;
    _previewUrl = URL.createObjectURL(blob);
    _previewAudio = new Audio(_previewUrl);
    _previewAudio.play().catch(() => {});
    if (oldUrl) URL.revokeObjectURL(oldUrl);
    _voiceStatus(section, 'Playing preview.');
  } catch (e) {
    _voiceStatus(section, 'Preview failed: ' + e.message + ' (is a TTS provider enabled in Settings → Text to Speech?)', true);
  }
}

function _stopRecording() {
  try { _rec && _rec.state !== 'inactive' && _rec.stop(); } catch (_) {}
  try { _recStream && _recStream.getTracks().forEach(t => t.stop()); } catch (_) {}
  clearTimeout(_recTimer);
}

async function _renderVoices(section) {
  section.replaceChildren();
  section.appendChild(_el('h3', '', 'Voice Lab'));
  section.appendChild(_el('div', 'music-sub',
    'The voice Aegis speaks with — preview any voice, or clone your own from a '
    + '~10 second recording (cloned voices need the Chatterbox engine; see the '
    + 'engine guide). Provider on/off lives in Settings → Text to Speech.'));

  // Built-in voices: pick + preview
  const pickRow = _el('div', 'voice-row');
  const sel = document.createElement('select');
  sel.id = 'music-voice-select';
  pickRow.appendChild(sel);
  const prev = _el('button', '', '▶ Preview'); prev.type = 'button';
  prev.addEventListener('click', () => _previewVoice(section, sel.value));
  pickRow.appendChild(prev);
  const setDefault = _el('button', '', 'Set as default'); setDefault.type = 'button';
  setDefault.title = 'Make this the voice used for read-aloud and Voice Mode replies';
  setDefault.addEventListener('click', async () => {
    try {
      const cur = await _json('/api/auth/settings');
      cur.tts_voice = sel.value;
      await _json('/api/auth/settings', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(cur),
      });
      _voiceStatus(section, `Default voice set to ${sel.value}.`);
    } catch (e) {
      const msg = /admin/i.test(e.message)
        ? 'Saving the default voice needs an admin account — an admin can set it in Settings → Text to Speech.'
        : 'Could not save: ' + e.message;
      _voiceStatus(section, msg, true);
    }
  });
  pickRow.appendChild(setDefault);
  section.appendChild(pickRow);

  try {
    const d = await _json('/api/tts/voices');
    const voices = d.voices || [];
    sel.replaceChildren();
    voices.forEach(v => {
      const id = typeof v === 'string' ? v : (v.id || v.name || '');
      if (!id) return;
      const o = document.createElement('option');
      o.value = id;
      o.textContent = typeof v === 'string' ? v : (v.label || v.name || id);
      sel.appendChild(o);
    });
    if (!voices.length) {
      const o = document.createElement('option');
      o.value = ''; o.textContent = 'No voices — enable a TTS provider in Settings';
      sel.appendChild(o);
    }
  } catch (_) { /* leave the empty select */ }

  // Cloned voices: list + delete
  const clonedWrap = _el('div');
  section.appendChild(clonedWrap);
  const refreshCloned = async () => {
    clonedWrap.replaceChildren();
    try {
      const d = await _json('/api/tts/my-voices');
      (d.voices || []).forEach(v => {
        const name = typeof v === 'string' ? v : (v.name || v.id || '');
        if (!name) return;
        const row = _el('div', 'voice-row');
        row.appendChild(_el('span', 'music-meta', `🎙 ${name} (cloned · ready)`));
        // Test synthesizes with this voice — the backend auto-routes cloned
        // voices to the Chatterbox endpoint, whatever the saved provider is.
        const test = _el('button', '', '▶ Test'); test.type = 'button';
        test.title = 'Hear this voice (the first playback takes ~20s while the voice engine warms up)';
        test.addEventListener('click', () => _previewVoice(section, name));
        row.appendChild(test);
        const use = _el('button', '', 'Use this voice'); use.type = 'button';
        use.title = 'Make this the voice for read-aloud and Voice Mode replies';
        use.addEventListener('click', async () => {
          try {
            const cur = await _json('/api/auth/settings');
            cur.tts_voice = name;
            await _json('/api/auth/settings', {
              method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(cur),
            });
            _voiceStatus(section, `Aegis now speaks as "${name}".`);
          } catch (e) {
            const msg = /admin/i.test(e.message)
              ? 'Setting the default voice needs an admin account — an admin can pick it in Settings → Text to Speech.'
              : 'Could not save: ' + e.message;
            _voiceStatus(section, msg, true);
          }
        });
        row.appendChild(use);
        const del = _el('button', '', 'Delete'); del.type = 'button';
        del.addEventListener('click', async () => {
          try {
            await _json('/api/tts/my-voices/' + encodeURIComponent(name), { method: 'DELETE' });
            refreshCloned();
          } catch (e) { _voiceStatus(section, 'Delete failed: ' + e.message, true); }
        });
        row.appendChild(del);
        clonedWrap.appendChild(row);
      });
    } catch (_) { /* cloning engine may not be set up — fine */ }
  };
  refreshCloned();

  // Clone recorder: name + record ≤15s + save
  const recRow = _el('div', 'voice-row');
  const nameIn = document.createElement('input');
  nameIn.type = 'text';
  nameIn.placeholder = 'New voice name (e.g. Me)';
  nameIn.maxLength = 40;
  recRow.appendChild(nameIn);
  const recBtn = _el('button', 'rec', '● Record ~10s'); recBtn.type = 'button';
  recRow.appendChild(recBtn);
  section.appendChild(recRow);
  section.appendChild(_el('div', 'voice-status', ''));

  recBtn.addEventListener('click', async () => {
    if (_rec && _rec.state === 'recording') { _stopRecording(); return; }
    const name = nameIn.value.trim();
    if (!name) { _voiceStatus(section, 'Name the voice first.', true); return; }
    if (!navigator.mediaDevices?.getUserMedia) { _voiceStatus(section, 'Microphone not available in this browser.', true); return; }
    try {
      _recStream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (_) { _voiceStatus(section, 'Microphone permission denied.', true); return; }
    _recChunks = [];
    const mime = ['audio/webm;codecs=opus', 'audio/webm', 'audio/ogg'].find(m => window.MediaRecorder && MediaRecorder.isTypeSupported(m)) || '';
    _rec = new MediaRecorder(_recStream, mime ? { mimeType: mime } : undefined);
    _rec.ondataavailable = (e) => { if (e.data && e.data.size) _recChunks.push(e.data); };
    _rec.onstop = async () => {
      recBtn.classList.remove('recording');
      recBtn.textContent = '● Record ~10s';
      const blob = new Blob(_recChunks, { type: (_rec && _rec.mimeType) || 'audio/webm' });
      if (blob.size < 8000) { _voiceStatus(section, 'Too short — read a couple of sentences.', true); return; }
      try {
        _voiceStatus(section, 'Converting and saving your sample…');
        const fd = new FormData();
        fd.append('name', name);
        fd.append('file', blob, 'sample.webm');
        const r = await fetch('/api/tts/my-voices', { method: 'POST', credentials: 'same-origin', body: fd });
        const d = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(d.detail?.message || d.detail || `Save failed (${r.status})`);
        const secs = d.seconds ? ` (${d.seconds}s sample)` : '';
        _voiceStatus(section,
          `✓ Voice "${name}" saved${secs} and ready — nothing else to do. `
          + `Press its ▶ Test button to hear it (the very first playback takes ~20s while the voice engine warms up; after that it's fast), `
          + `then "Use this voice" to make Aegis speak as you.`);
        nameIn.value = '';
        refreshCloned();
      } catch (e) {
        _voiceStatus(section, 'Save failed: ' + e.message, true);
      }
    };
    _rec.start();
    recBtn.classList.add('recording');
    recBtn.textContent = '■ Stop';
    _voiceStatus(section, 'Recording — read a couple of natural sentences, then Stop (auto-stops at 15s).');
    _recTimer = setTimeout(_stopRecording, 15000);
  });
}

// ── tab entry ─────────────────────────────────────────────────────────────────
export async function renderMusicTab(host) {
  if (!host) return;
  _styles();
  _host = host;
  host.replaceChildren();

  const composer = _el('div', 'music-section');
  host.appendChild(composer);

  const tracks = _el('div', 'music-section');
  host.appendChild(tracks);

  const voices = _el('div', 'music-section');
  host.appendChild(voices);

  await renderCreateTab(composer, 'audio');
  await _renderTracks(tracks);
  await _renderVoices(voices);
}

export function stopMusicTab() {
  // Leave the audio playing on purpose — only stop the recorder.
  _stopRecording();
}

export default { renderMusicTab, stopMusicTab };
