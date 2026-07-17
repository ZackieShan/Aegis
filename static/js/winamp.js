// winamp.js — the "Aegis Amp" Y2K easter egg: a classic-late-90s-skinned
// floating music player that drives the Studio's shared audio element
// (window.__studioAudio — the same one the Music Maker uses, so a song
// started in the Studio keeps playing in the Amp and vice versa).
//
// Enabled via Settings → Appearance ("Aegis Amp (Y2K skin)") or the /winamp
// slash command; state lives in localStorage 'aegis-winamp'. Fully
// self-contained: no dependencies on other app modules. The spectrum is a
// beat-flavored animation (no WebAudio graph — attaching a
// MediaElementSource would permanently re-route the shared element's audio).

const LS_ENABLED = 'aegis-winamp';
const LS_POS = 'aegis-winamp-pos';

const REDUCED_MOTION = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

const _audio = window.__studioAudio || (window.__studioAudio = new Audio());

let _win = null;
let _raf = 0;
let _marqueeTimer = 0;

function _fmt(s) {
  if (!isFinite(s) || s < 0) return '0:00';
  return `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, '0')}`;
}

function _styles() {
  if (document.getElementById('aegis-amp-styles')) return;
  const st = document.createElement('style');
  st.id = 'aegis-amp-styles';
  st.textContent = `
  #aegis-amp { position: fixed; z-index: 9200; width: 286px; user-select: none;
    font-family: 'JetBrains Mono', 'Lucida Console', monospace;
    background: linear-gradient(180deg, #3a3f4a 0%, #23262e 8%, #23262e 92%, #14161b 100%);
    border: 2px solid; border-color: #6a7080 #101216 #101216 #6a7080; border-radius: 3px;
    box-shadow: 3px 3px 0 rgba(0,0,0,.45), 0 12px 34px rgba(0,0,0,.5); color: #cfd4e0; }
  #aegis-amp * { box-sizing: border-box; }
  .amp-title { display: flex; align-items: center; height: 18px; padding: 0 4px; cursor: grab;
    background: linear-gradient(180deg, #2b4a8a 0%, #16264a 55%, #0d1730 100%);
    border-bottom: 1px solid #0a0d14; }
  .amp-title:active { cursor: grabbing; }
  .amp-title-text { flex: 1; text-align: center; font-size: 9px; letter-spacing: 3px;
    color: #d8c37a; text-shadow: 0 1px 0 #000; font-weight: 700; }
  .amp-title-btn { width: 13px; height: 13px; margin-left: 2px; padding: 0; font-size: 8px; line-height: 11px;
    color: #cfd4e0; background: linear-gradient(180deg,#4a4f5c,#23262e); cursor: pointer;
    border: 1px solid; border-color: #7a8090 #101216 #101216 #7a8090; }
  .amp-lcd { margin: 6px; padding: 6px 7px; background: #0a1408;
    border: 2px solid; border-color: #101216 #4a505e #4a505e #101216; border-radius: 2px; }
  .amp-lcd-top { display: flex; align-items: flex-end; gap: 8px; }
  .amp-time { font-size: 24px; line-height: 1; color: #2bff58; text-shadow: 0 0 6px rgba(43,255,88,.55);
    font-variant-numeric: tabular-nums; letter-spacing: 1px; }
  .amp-spectrum { flex: 1; height: 26px; image-rendering: pixelated; }
  .amp-marquee { margin-top: 5px; height: 12px; overflow: hidden; white-space: nowrap;
    font-size: 10px; color: #2bff58; text-shadow: 0 0 4px rgba(43,255,88,.4); }
  .amp-stats { display: flex; gap: 10px; margin-top: 3px; font-size: 8px; color: #1d9c3c; letter-spacing: 1px; }
  .amp-seek { width: 100%; margin: 0 0 2px; accent-color: #d8c37a; height: 10px; }
  .amp-body { padding: 0 6px 7px; }
  .amp-controls { display: flex; align-items: center; gap: 3px; margin-top: 4px; }
  .amp-btn { flex: 0 0 auto; width: 32px; height: 20px; font-size: 10px; line-height: 1; color: #12141a;
    background: linear-gradient(180deg, #b9bfcc 0%, #8a90a0 45%, #6f7585 50%, #9298a8 100%);
    border: 1px solid; border-color: #e6eaf2 #383c46 #383c46 #e6eaf2; border-radius: 2px; cursor: pointer; }
  .amp-btn:active { border-color: #383c46 #e6eaf2 #e6eaf2 #383c46;
    background: linear-gradient(180deg, #7f8595 0%, #a7adbc 100%); }
  .amp-vol { flex: 1; accent-color: #d8c37a; height: 10px; min-width: 40px; }
  .amp-vol-label { font-size: 8px; color: #8a90a0; letter-spacing: 1px; }
  @media (max-width: 480px) { #aegis-amp { width: 252px; } }
  `;
  document.head.appendChild(st);
}

/* ── spectrum: a beat-flavored fake analyser (16 bars, green→yellow→red) ── */
function _drawSpectrum(canvas) {
  const ctx = canvas.getContext('2d');
  const W = canvas.width, H = canvas.height, N = 16, bw = Math.floor(W / N);
  const t = performance.now() / 1000;
  ctx.clearRect(0, 0, W, H);
  for (let i = 0; i < N; i++) {
    // Stacked sines per band — low bands slower/taller, like a real analyser.
    const phase = t * (2.2 + i * 0.55) + i * 1.7;
    let v = 0.42 + 0.3 * Math.sin(phase) + 0.22 * Math.sin(phase * 1.9 + 2)
          + 0.12 * Math.sin(t * 9 + i * 3.1);
    v = Math.max(0.05, Math.min(1, v * (1.12 - i / (N * 1.6))));
    if (_audio.paused) v = 0.05;
    const bars = Math.round(v * (H / 3));
    for (let b = 0; b < bars; b++) {
      const y = H - (b + 1) * 3;
      const frac = b / (H / 3);
      ctx.fillStyle = frac > 0.78 ? '#ff3b30' : frac > 0.5 ? '#ffd23b' : '#2bff58';
      ctx.fillRect(i * bw + 1, y, bw - 2, 2);
    }
  }
}

function _startRaf() {
  if (_raf || REDUCED_MOTION) return;
  const loop = () => {
    _raf = 0;
    if (!_win) return;
    const c = _win.querySelector('.amp-spectrum');
    if (c) _drawSpectrum(c);
    if (!_audio.paused) _raf = requestAnimationFrame(loop);
  };
  _raf = requestAnimationFrame(loop);
}

function _title() {
  const meta = window.__studioAudioMeta;
  return (meta && meta.title && _audio.src)
    ? meta.title
    : 'AEGIS AMP · it really whips the llama’s ass · open Studio → Music to play a track';
}

function _sync() {
  if (!_win) return;
  _win.querySelector('.amp-time').textContent = _fmt(_audio.currentTime);
  const seek = _win.querySelector('.amp-seek');
  if (seek && !seek.matches(':active') && isFinite(_audio.duration) && _audio.duration > 0) {
    seek.value = String(Math.round((_audio.currentTime / _audio.duration) * 1000));
  }
  const mq = _win.querySelector('.amp-marquee-inner');
  if (mq && mq.dataset.title !== _title()) {
    mq.dataset.title = _title();
    mq.textContent = `*** ${_title()} *** ${_title()} ***`;
    mq.style.transform = 'translateX(0)';
    mq.dataset.off = '0';
  }
  const play = _win.querySelector('[data-amp="play"]');
  if (play) play.textContent = _audio.paused ? '▶' : '❚❚';
  if (!_audio.paused) _startRaf();
}

function _marqueeStep() {
  if (!_win) return;
  const mq = _win.querySelector('.amp-marquee-inner');
  if (mq && !REDUCED_MOTION && !_audio.paused) {
    let off = (parseFloat(mq.dataset.off || '0') + 1);
    const half = mq.scrollWidth / 2;
    if (half > 0 && off >= half) off = 0;
    mq.dataset.off = String(off);
    mq.style.transform = `translateX(${-off}px)`;
  }
}

function _wireAudio() {
  if (_audio.__ampWired) return;
  _audio.__ampWired = true;
  ['timeupdate', 'play', 'pause', 'ended', 'loadedmetadata'].forEach(ev =>
    _audio.addEventListener(ev, () => { if (_win) _sync(); }));
}

function _restorePos(el) {
  try {
    const p = JSON.parse(localStorage.getItem(LS_POS) || 'null');
    if (p && typeof p.x === 'number' && typeof p.y === 'number') {
      el.style.left = Math.max(0, Math.min(window.innerWidth - 100, p.x)) + 'px';
      el.style.top = Math.max(0, Math.min(window.innerHeight - 60, p.y)) + 'px';
      return;
    }
  } catch (_) {}
  el.style.right = '18px';
  el.style.bottom = '86px';
}

function _drag(el, handle) {
  let sx = 0, sy = 0, ox = 0, oy = 0, moving = false;
  handle.addEventListener('pointerdown', (e) => {
    if (e.target.closest('.amp-title-btn')) return;
    moving = true;
    const r = el.getBoundingClientRect();
    sx = e.clientX; sy = e.clientY; ox = r.left; oy = r.top;
    el.style.left = ox + 'px'; el.style.top = oy + 'px';
    el.style.right = 'auto'; el.style.bottom = 'auto';
    handle.setPointerCapture(e.pointerId);
  });
  handle.addEventListener('pointermove', (e) => {
    if (!moving) return;
    el.style.left = (ox + e.clientX - sx) + 'px';
    el.style.top = (oy + e.clientY - sy) + 'px';
  });
  handle.addEventListener('pointerup', () => {
    moving = false;
    try {
      const r = el.getBoundingClientRect();
      localStorage.setItem(LS_POS, JSON.stringify({ x: r.left, y: r.top }));
    } catch (_) {}
  });
}

function _build() {
  if (_win) return;
  _styles();
  _win = document.createElement('div');
  _win.id = 'aegis-amp';
  _win.setAttribute('role', 'region');
  _win.setAttribute('aria-label', 'Aegis Amp music player');
  _win.innerHTML = `
    <div class="amp-title">
      <span class="amp-title-text">A E G I S &nbsp; A M P</span>
      <button class="amp-title-btn" data-amp="min" title="Shade (hide everything but the title bar)">▁</button>
      <button class="amp-title-btn" data-amp="close" title="Close (turns the Amp off — /winamp brings it back)">✕</button>
    </div>
    <div class="amp-chrome">
      <div class="amp-lcd">
        <div class="amp-lcd-top">
          <span class="amp-time">0:00</span>
          <canvas class="amp-spectrum" width="150" height="26"></canvas>
        </div>
        <div class="amp-marquee"><span class="amp-marquee-inner" style="display:inline-block"></span></div>
        <div class="amp-stats"><span>192 KBPS</span><span>44 KHZ</span><span>STEREO</span></div>
      </div>
      <div class="amp-body">
        <input type="range" class="amp-seek" min="0" max="1000" value="0" aria-label="Seek">
        <div class="amp-controls">
          <button class="amp-btn" data-amp="prev" title="Previous">⏮</button>
          <button class="amp-btn" data-amp="play" title="Play / pause">▶</button>
          <button class="amp-btn" data-amp="stop" title="Stop">⏹</button>
          <button class="amp-btn" data-amp="next" title="Next">⏭</button>
          <span class="amp-vol-label">VOL</span>
          <input type="range" class="amp-vol" min="0" max="100" aria-label="Volume">
        </div>
      </div>
    </div>`;
  document.body.appendChild(_win);
  _restorePos(_win);
  _drag(_win, _win.querySelector('.amp-title'));

  _win.querySelector('[data-amp="close"]').addEventListener('click', () => api.disable());
  _win.querySelector('[data-amp="min"]').addEventListener('click', () => {
    const chrome = _win.querySelector('.amp-chrome');
    chrome.style.display = chrome.style.display === 'none' ? '' : 'none';
  });
  _win.querySelector('[data-amp="play"]').addEventListener('click', () => {
    if (!_audio.src) { _sync(); return; }
    if (_audio.paused) _audio.play().catch(() => {}); else _audio.pause();
  });
  _win.querySelector('[data-amp="stop"]').addEventListener('click', () => {
    _audio.pause();
    try { _audio.currentTime = 0; } catch (_) {}
    _sync();
  });
  // Prev/next belong to the Studio's track list when it has one; the Amp
  // asks it politely and no-ops otherwise (the shared element has no list).
  const jump = (dir) => window.dispatchEvent(new CustomEvent('aegis-amp-jump', { detail: { dir } }));
  _win.querySelector('[data-amp="prev"]').addEventListener('click', () => jump(-1));
  _win.querySelector('[data-amp="next"]').addEventListener('click', () => jump(1));
  const seek = _win.querySelector('.amp-seek');
  seek.addEventListener('input', () => {
    if (isFinite(_audio.duration) && _audio.duration > 0) {
      _audio.currentTime = (Number(seek.value) / 1000) * _audio.duration;
    }
  });
  const vol = _win.querySelector('.amp-vol');
  vol.value = String(Math.round((_audio.volume ?? 1) * 100));
  vol.addEventListener('input', () => { _audio.volume = Number(vol.value) / 100; });

  _wireAudio();
  _sync();
  _drawSpectrum(_win.querySelector('.amp-spectrum'));
  if (!REDUCED_MOTION) _marqueeTimer = setInterval(_marqueeStep, 50);
}

function _teardown() {
  clearInterval(_marqueeTimer);
  _marqueeTimer = 0;
  if (_raf) cancelAnimationFrame(_raf);
  _raf = 0;
  if (_win) { _win.remove(); _win = null; }
}

const api = {
  get enabled() { return !!_win; },
  enable() {
    localStorage.setItem(LS_ENABLED, 'on');
    _build();
    window.dispatchEvent(new CustomEvent('aegis-winamp-sync'));
  },
  disable() {
    localStorage.setItem(LS_ENABLED, 'off');
    _teardown();
    window.dispatchEvent(new CustomEvent('aegis-winamp-sync'));
  },
};
window.aegisAmp = api;

// Settings toggle + other tabs flip localStorage and fire this event.
window.addEventListener('aegis-winamp-change', (e) => {
  if (e.detail && e.detail.enabled) api.enable(); else api.disable();
});

if (localStorage.getItem(LS_ENABLED) === 'on') {
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => api.enable(), { once: true });
  } else {
    api.enable();
  }
}

export default api;
