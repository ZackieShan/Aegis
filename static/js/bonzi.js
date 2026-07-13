// bonzi.js — the Bonzi Buddy easter egg. A purple gorilla from 1999 who
// supervises your AI chats. Enabled via Settings → Appearance ("Bonzi Buddy")
// or the /bonzi slash command; state lives in localStorage 'aegis-bonzi'.
//
// Sprite/animation data is the classic MS Agent character converted by
// clippy.js (see ACKNOWLEDGMENTS.md); this file is a small self-contained
// player for that format: frames with durations, up to `overlayCount`
// stacked sprite layers, weighted random branching, and exit branches for
// graceful interruption. No dependencies on other app modules — the chat
// integration goes through window.bonziBuddy?.on*() optional hooks.

const BASE = '/static/agents/bonzi';
const LS_ENABLED = 'aegis-bonzi';
const LS_POS = 'aegis-bonzi-pos';
const LS_OPTS = 'aegis-bonzi-opts';
const FRAME_W = 200, FRAME_H = 160;

const REDUCED_MOTION = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

/* ── Quips ─────────────────────────────────────────────────────────── */

const QUIPS = {
  greet: [
    "It's me, BONZI! Your buddy is BACK, baby!",
    "Did you miss me? I've been living in a ZIP file since 2004.",
    "Hello friend! I see you upgraded from dial-up. Fancy!",
    "I'm legally required to say I no longer collect your data. Aegis made me sign something.",
    "Reporting for duty! I'll supervise the AI. Somebody has to.",
  ],
  reply: [
    "Ooh, great answer! I taught it everything it knows.",
    "I could've told you that. But nooo, everyone trusts the 'language model'.",
    "Fascinating! I understood at least four of those words.",
    "Not bad! Back in my day we just installed a toolbar for this.",
    "The AI did the work, but I provided the moral support.",
    "Beautiful. I'm putting this on the fridge.",
  ],
  replyCode: [
    "Code! I was written in Visual Basic 6, you know. True art.",
    "Ah, code. I'd review it, but my last commit was in 1999.",
    "Looks correct to me! I say that about all code.",
  ],
  replyLong: [
    "That's a LOT of words. Want me to pretend I read them all?",
    "Whew! An essay! I skimmed it. Ten out of ten.",
  ],
  think: [
    "The AI is thinking. I used to do that... allegedly.",
    "Crunching the numbers! Well, THEY are. I'm just vibing.",
    "Working hard! Not me though. I'm decorative.",
  ],
  click: [
    "Hey! I'm walking here!",
    "You clicked me! This is the most attention I've had in 20 years.",
    "Careful! I bruise like a banana. It's a whole thing.",
    "Yes? I was in the middle of doing absolutely nothing.",
  ],
  dblclick: [
    "OOH OOH AH AH! ...Sorry. Force of habit.",
    "You rang?! Twice?!",
  ],
  fact: [
    "Did you know? I was downloaded over 40 million times. Your antivirus remembers.",
    "Fun fact: in 1999 I could sing, tell jokes, and collect personal data. Now I only do two of those. Progress!",
    "Did you know? The average person blinks 15 times a minute. I blink exactly when my sprite sheet says so.",
    "Fun fact: purple gorillas have no natural predators, except Task Manager.",
    "Did you know? Everything this app does stays on your machine. In my era we called that 'a missed opportunity'.",
    "Fun fact: I am 200 by 160 pixels of pure charisma.",
    "Did you know? If you tell me to go away I actually go away now. Character growth!",
  ],
  bye: [
    "Fine! I'll go back into the settings... I mean, the void.",
    "Goodbye, friend! I'll be watching. Wait — no I won't. Legally I won't.",
    "Off to 1999 I go. Tell the AI it still owes me royalties.",
  ],
};

function pick(arr) { return arr[Math.floor(Math.random() * arr.length)]; }

/* ── Sprite animator (clippy.js agent-data player) ─────────────────── */

class BonziAnimator {
  constructor(stage, data, sounds) {
    this._data = data;
    this._sounds = sounds;
    this._layers = [];
    this._timer = null;
    this._gen = 0;
    this._exiting = false;
    this._current = null;
    this._onDone = null;
    for (let i = 0; i < (data.overlayCount || 1); i++) {
      const el = document.createElement('div');
      el.className = 'bonzi-layer';
      stage.appendChild(el);
      this._layers.push(el);
    }
  }

  has(name) { return !!this._data.animations[name]; }
  get playing() { return this._timer !== null; }

  play(name, opts = {}) {
    const anim = this._data.animations[name];
    if (!anim) { if (opts.onDone) opts.onDone(false); return; }
    this._cancelTimer();
    const gen = ++this._gen;
    this._current = anim;
    this._exiting = false;
    this._onDone = opts.onDone || null;
    this._allowSound = opts.sound !== false;
    // Several agent animations (the Look* family, Searching) are closed
    // branching loops that can only reach their final frame through
    // exitBranch pointers — without a watchdog they literally never end
    // (clippy.js shipped a 5s default timeout for the same reason).
    if (opts.timeout) {
      this._watchdog = setTimeout(() => { if (gen === this._gen) this._exiting = true; }, opts.timeout);
    }
    this._step(0, gen);
  }

  // Follow exitBranch pointers at the next frame advance so the animation
  // winds down through its authored exit path instead of freezing mid-pose.
  stopGraceful() { this._exiting = true; }

  stopNow(rest = true) {
    this._cancelTimer();
    this._gen++;
    this._current = null;
    this._onDone = null;
    if (rest) this.rest();
  }

  rest() {
    const anim = this._data.animations.RestPose;
    if (anim && anim.frames[0]) this._draw(anim.frames[0]);
  }

  _cancelTimer() {
    if (this._timer) { clearTimeout(this._timer); this._timer = null; }
    if (this._watchdog) { clearTimeout(this._watchdog); this._watchdog = null; }
  }

  _step(idx, gen) {
    if (gen !== this._gen || !this._current) return;
    const frames = this._current.frames;
    const frame = frames[idx];
    if (!frame) { this._finish(gen); return; }
    this._draw(frame);
    if (frame.sound && this._allowSound) this._playSound(frame.sound);
    this._timer = setTimeout(() => {
      this._timer = null;
      const next = this._nextIndex(frame, idx);
      if (next >= frames.length) this._finish(gen);
      else this._step(next, gen);
    }, frame.duration || 100);
  }

  _nextIndex(frame, idx) {
    if (this._exiting && frame.exitBranch !== undefined) return frame.exitBranch;
    if (frame.branching) {
      let rnd = Math.random() * 100;
      for (const b of frame.branching.branches) {
        if (rnd <= b.weight) return b.frameIndex;
        rnd -= b.weight;
      }
    }
    return idx + 1;
  }

  _finish(gen) {
    if (gen !== this._gen) return;
    this._cancelTimer();
    this._current = null;
    const cb = this._onDone;
    this._onDone = null;
    if (cb) cb(true);
  }

  _draw(frame) {
    // Frames without an images array hold the previous pose (agent format).
    if (!frame.images) return;
    this._layers.forEach((layer, i) => {
      const img = frame.images[i];
      if (img) {
        layer.style.display = 'block';
        layer.style.backgroundPosition = `-${img[0]}px -${img[1]}px`;
      } else {
        layer.style.display = 'none';
      }
    });
  }

  _playSound(key) {
    const audio = this._sounds[key];
    if (!audio) return;
    try { audio.currentTime = 0; audio.volume = 0.4; audio.play().catch(() => {}); } catch (_) {}
  }
}

/* ── Bonzi himself ─────────────────────────────────────────────────── */

const IDLE_POOL = [
  'Idle1_1', 'Idle1_2', 'Idle1_3', 'Idle1_4', 'Idle1_5', 'Idle1_6',
  'Blink', 'Blink', 'LookLeft', 'LookRight', 'LookUp', 'LookDown',
];
const REACT_POOL = ['Explain', 'Congratulate', 'Pleased', 'Acknowledge', 'GestureLeft', 'GestureUp'];
const TRICK_POOL = ['Wave', 'Congratulate', 'GetAttention', 'Greet', 'Pleased', 'Surprised'];

class BonziBuddy {
  constructor() {
    this.enabled = false;
    this._root = null;
    this._anim = null;
    this._bubble = null;
    this._state = 'hidden';
    this._idleTimer = null;
    this._bubbleTimers = [];
    this._lastReact = 0;
    this._menu = null;
    this._loading = null;
    // Bumped on every enable()/disable() so stale async continuations
    // (asset load, Hide fallback timers) can't act on a newer lifecycle.
    this._epoch = 0;
    this._onResize = null;
    this._utter = null;
    this._opts = { sounds: true, voice: false };
    try { Object.assign(this._opts, JSON.parse(localStorage.getItem(LS_OPTS) || '{}')); } catch (_) {}
  }

  /* ── lifecycle ── */

  async enable(persist = true) {
    if (this.enabled) return;
    this.enabled = true;
    const epoch = ++this._epoch;
    if (persist) localStorage.setItem(LS_ENABLED, 'on');
    try {
      await this._load();
    } catch (e) {
      if (epoch === this._epoch) this.enabled = false;
      console.warn('Bonzi failed to load his assets:', e);
      return;
    }
    if (!this.enabled || epoch !== this._epoch) return; // disabled while loading
    if (this._root) this._unmount(); // re-enabled mid-Hide: discard the leaving instance
    this._mount();
    this._state = 'entering';
    if (REDUCED_MOTION) {
      this._anim.rest();
      this._toIdle();
      this._say(pick(QUIPS.greet));
    } else {
      this._anim.play('Show', {
        sound: this._opts.sounds,
        onDone: () => {
          this._say(pick(QUIPS.greet), { voice: true });
          this._toIdle();
        },
      });
    }
  }

  disable(persist = true) {
    if (!this.enabled) return;
    this.enabled = false;
    const epoch = ++this._epoch;
    if (persist) localStorage.setItem(LS_ENABLED, 'off');
    this._closeMenu();
    this._clearIdle();
    this._clearBubble();
    this._stopVoice();
    if (!this._root || !this._anim) { this._unmount(); return; }
    this._state = 'leaving';
    this._say(pick(QUIPS.bye));
    const finish = () => { if (epoch === this._epoch) this._unmount(); };
    if (REDUCED_MOTION || !this._anim.has('Hide')) {
      setTimeout(finish, 1200);
    } else {
      this._anim.stopNow(false);
      this._anim.play('Hide', { sound: this._opts.sounds, onDone: finish });
      setTimeout(finish, 4000);
    }
  }

  toggle() { this.enabled ? this.disable() : this.enable(); }

  async _load() {
    if (this._data) return;
    if (!this._loading) {
      this._loading = (async () => {
        // (re-thrown to enable(); the catch below just clears the cache so a
        // transient failure doesn't brick Bonzi for the whole page session)
        const res = await fetch(`${BASE}/agent.json`);
        if (!res.ok) throw new Error(`agent.json HTTP ${res.status}`);
        const data = await res.json();
        await new Promise((resolve, reject) => {
          const img = new Image();
          img.onload = resolve;
          img.onerror = () => reject(new Error('sprite sheet failed to load'));
          img.src = `${BASE}/map.png`;
        });
        const sounds = {};
        (data.sounds || []).forEach(name => { sounds[name] = new Audio(`${BASE}/sounds/${name}.mp3`); });
        this._data = data;
        this._soundBank = sounds;
      })();
      this._loading.catch(() => { this._loading = null; });
    }
    await this._loading;
  }

  _mount() {
    if (this._root) return;
    const root = document.createElement('div');
    root.className = 'bonzi-container';
    root.setAttribute('aria-hidden', 'true');
    const stage = document.createElement('div');
    stage.className = 'bonzi-stage';
    stage.title = 'Bonzi Buddy — click me! (right-click for options)';
    const bubble = document.createElement('div');
    bubble.className = 'bonzi-bubble';
    bubble.hidden = true;
    root.appendChild(bubble);
    root.appendChild(stage);
    document.body.appendChild(root);
    this._root = root;
    this._bubble = bubble;
    this._anim = new BonziAnimator(stage, this._data, this._soundBank);
    this._restorePos();
    this._wireInput(stage);
  }

  _unmount() {
    this._state = 'hidden';
    this._closeMenu();
    if (this._onResize) { window.removeEventListener('resize', this._onResize); this._onResize = null; }
    if (this._anim) this._anim.stopNow(false);
    if (this._root) this._root.remove();
    this._root = null;
    this._bubble = null;
    this._anim = null;
  }

  /* ── idle behavior ── */

  _toIdle() {
    if (!this.enabled) return;
    this._state = 'idle';
    // A graceful exit may still be winding down; its completion callback
    // draws the rest pose, so don't stomp the exit frames from here.
    if (!this._anim.playing) this._anim.rest();
    this._scheduleIdle();
  }

  _scheduleIdle() {
    this._clearIdle();
    if (REDUCED_MOTION) return;
    this._idleTimer = setTimeout(() => this._idleTick(), 9000 + Math.random() * 14000);
  }

  _idleTick() {
    if (!this.enabled || this._state !== 'idle' || document.hidden || this._anim.playing) {
      this._scheduleIdle();
      return;
    }
    // The Look* idles are closed loops that only end via their exitBranch
    // path (which already returns the gaze) — the timeout forces that exit.
    this._anim.play(pick(IDLE_POOL), {
      sound: false,
      timeout: 7000,
      onDone: () => { if (this._state === 'idle') this._anim.rest(); },
    });
    this._scheduleIdle();
  }

  _clearIdle() { if (this._idleTimer) { clearTimeout(this._idleTimer); this._idleTimer = null; } }

  /* ── chat hooks (called from chat.js; all optional-chained there) ── */

  onThinking() {
    if (!this.enabled || this._state === 'leaving' || REDUCED_MOTION) return;
    if (this._state === 'thinking') return;
    this._state = 'thinking';
    if (Math.random() < 0.25) this._say(pick(QUIPS.think));
    const loop = () => {
      if (this._state !== 'thinking') {
        // The graceful exit just finished; settle into the rest pose now.
        if (this._state === 'idle' && this._anim && !this._anim.playing) this._anim.rest();
        return;
      }
      this._anim.play('Searching', { sound: false, onDone: () => loop() });
    };
    this._anim.play('Think', { sound: false, onDone: () => loop() });
  }

  onStreamIdle() {
    if (!this.enabled) return;
    if (this._state === 'thinking') {
      this._anim.stopGraceful();
      this._toIdle();
    }
  }

  onReplyDone(text) {
    if (!this.enabled || this._state === 'leaving') return;
    const now = Date.now();
    if (now - this._lastReact < 8000) { this.onStreamIdle(); return; }
    this._lastReact = now;
    this._state = 'reacting';
    let pool = QUIPS.reply;
    if (text && /```/.test(text)) pool = Math.random() < 0.6 ? QUIPS.replyCode : QUIPS.reply;
    else if (text && text.length > 2500 && Math.random() < 0.5) pool = QUIPS.replyLong;
    this._say(pick(pool), { voice: true });
    if (REDUCED_MOTION) { this._toIdle(); return; }
    this._anim.stopNow(false);
    this._anim.play(pick(REACT_POOL), { sound: false, timeout: 8000, onDone: () => this._toIdle() });
  }

  /* ── tricks & facts ── */

  tellFact() {
    if (!this.enabled) return;
    this._say(pick(QUIPS.fact), { voice: true });
    if (!REDUCED_MOTION && this._state !== 'leaving') {
      this._state = 'reacting';
      this._anim.stopNow(false);
      this._anim.play('Explain', { sound: false, timeout: 8000, onDone: () => this._toIdle() });
    }
  }

  doTrick() {
    if (!this.enabled || REDUCED_MOTION) return;
    this._state = 'reacting';
    this._anim.stopNow(false);
    this._anim.play(pick(TRICK_POOL), { sound: this._opts.sounds, timeout: 8000, onDone: () => this._toIdle() });
  }

  /* ── speech bubble & voice ── */

  _say(text, opts = {}) {
    if (!this._bubble) return;
    this._clearBubble();
    const bubble = this._bubble;
    bubble.hidden = false;
    bubble.textContent = '';
    // Flip the bubble below Bonzi when he's dragged near the top edge.
    const rect = this._root.getBoundingClientRect();
    bubble.classList.toggle('bonzi-bubble-below', rect.top < 130);
    let i = 0;
    const typeTimer = setInterval(() => {
      i += 2;
      bubble.textContent = text.slice(0, i);
      if (i >= text.length) clearInterval(typeTimer);
    }, 24);
    this._bubbleTimers.push(typeTimer);
    const hold = Math.min(9000, 2800 + text.length * 45);
    this._bubbleTimers.push(setTimeout(() => {
      clearInterval(typeTimer);
      bubble.hidden = true;
    }, hold));
    if (opts.voice) this._speak(text);
  }

  _clearBubble() {
    this._bubbleTimers.forEach(t => { clearTimeout(t); clearInterval(t); });
    this._bubbleTimers = [];
    if (this._bubble) this._bubble.hidden = true;
  }

  _speak(text) {
    if (!this._opts.voice || !('speechSynthesis' in window)) return;
    // Don't talk over the app's own TTS playback.
    if (window.aiTTSManager && (window.aiTTSManager.isPlaying || window.aiTTSManager._processing)) return;
    try {
      if (speechSynthesis.speaking || speechSynthesis.pending) {
        // Only cut off our own previous quip — other modules (browser-TTS
        // playback, voice previews) share the global speechSynthesis queue.
        if (!this._utter) return;
        speechSynthesis.cancel();
      }
      const u = new SpeechSynthesisUtterance(text);
      u.pitch = 0.15; // the classic unsettling baritone
      u.rate = 0.95;
      u.volume = 0.9;
      u.onend = u.onerror = () => { if (this._utter === u) this._utter = null; };
      this._utter = u;
      speechSynthesis.speak(u);
    } catch (_) {}
  }

  _stopVoice() {
    try {
      if (this._utter && 'speechSynthesis' in window) {
        speechSynthesis.cancel();
        this._utter = null;
      }
    } catch (_) {}
  }

  /* ── input: click / double-click / drag / context menu ── */

  _wireInput(stage) {
    let downAt = null, moved = false, offX = 0, offY = 0;
    let clickTimer = null;

    stage.addEventListener('pointerdown', (e) => {
      if (e.button !== 0) return;
      const rect = this._root.getBoundingClientRect();
      downAt = { x: e.clientX, y: e.clientY };
      moved = false;
      offX = e.clientX - rect.left;
      offY = e.clientY - rect.top;
      stage.setPointerCapture(e.pointerId);
    });

    stage.addEventListener('pointermove', (e) => {
      if (!downAt) return;
      if (!moved && Math.hypot(e.clientX - downAt.x, e.clientY - downAt.y) < 6) return;
      moved = true;
      this._root.classList.add('bonzi-dragging');
      this._moveTo(e.clientX - offX, e.clientY - offY);
    });

    stage.addEventListener('pointerup', (e) => {
      if (!downAt) return;
      downAt = null;
      this._root.classList.remove('bonzi-dragging');
      if (moved) { this._savePos(); return; }
      // Distinguish single vs double click without firing both.
      if (clickTimer) {
        clearTimeout(clickTimer);
        clickTimer = null;
        this._onDblClick();
      } else {
        clickTimer = setTimeout(() => { clickTimer = null; this._onClick(); }, 280);
      }
    });

    stage.addEventListener('pointercancel', () => {
      downAt = null;
      this._root.classList.remove('bonzi-dragging');
    });

    stage.addEventListener('contextmenu', (e) => {
      e.preventDefault();
      this._openMenu(e.clientX, e.clientY);
    });

    this._onResize = () => { if (this._root) this._clampToViewport(); };
    window.addEventListener('resize', this._onResize);
  }

  _onClick() {
    if (!this.enabled || this._state === 'leaving') return;
    this._say(pick(QUIPS.click), { voice: true });
    if (!REDUCED_MOTION) {
      this._state = 'reacting';
      this._anim.stopNow(false);
      this._anim.play(pick(['Wave', 'Greet', 'Surprised', 'Pleased']), { sound: false, timeout: 8000, onDone: () => this._toIdle() });
    }
  }

  _onDblClick() {
    if (!this.enabled || this._state === 'leaving') return;
    this._say(pick(QUIPS.dblclick), { voice: true });
    if (!REDUCED_MOTION) {
      this._state = 'reacting';
      this._anim.stopNow(false);
      this._anim.play('GetAttention', { sound: this._opts.sounds, timeout: 8000, onDone: () => this._toIdle() });
    }
  }

  /* ── position ── */

  _moveTo(x, y) {
    // Use the rendered size, not FRAME_W/H — the mobile media query scales
    // the container with a transform, which shrinks the visual box.
    const rect = this._root.getBoundingClientRect();
    const w = rect.width || FRAME_W;
    const h = rect.height || FRAME_H;
    this._root.style.left = Math.max(0, Math.min(window.innerWidth - w, x)) + 'px';
    this._root.style.top = Math.max(0, Math.min(window.innerHeight - h, y)) + 'px';
    this._root.style.right = 'auto';
    this._root.style.bottom = 'auto';
  }

  _savePos() {
    if (!this._root) return;
    const rect = this._root.getBoundingClientRect();
    try { localStorage.setItem(LS_POS, JSON.stringify({ x: rect.left, y: rect.top })); } catch (_) {}
  }

  _restorePos() {
    let pos = null;
    try { pos = JSON.parse(localStorage.getItem(LS_POS) || 'null'); } catch (_) {}
    if (pos && typeof pos.x === 'number' && typeof pos.y === 'number') {
      this._moveTo(pos.x, pos.y);
    }
  }

  _clampToViewport() {
    const rect = this._root.getBoundingClientRect();
    if (rect.right > window.innerWidth || rect.bottom > window.innerHeight) {
      this._moveTo(rect.left, rect.top);
    }
  }

  /* ── context menu ── */

  _openMenu(x, y) {
    this._closeMenu();
    const menu = document.createElement('div');
    menu.className = 'bonzi-menu';
    const items = [
      { label: '🎩 Do a trick', act: () => this.doTrick() },
      { label: '💡 Tell me a fact', act: () => this.tellFact() },
      { label: (this._opts.sounds ? '🔊' : '🔇') + ' Sounds: ' + (this._opts.sounds ? 'on' : 'off'), act: () => this._setOpt('sounds', !this._opts.sounds) },
      { label: (this._opts.voice ? '🗣️' : '🤐') + ' Voice: ' + (this._opts.voice ? 'on' : 'off'), act: () => this._setOpt('voice', !this._opts.voice) },
      { label: '👋 Goodbye, Bonzi', act: () => this.disable() },
    ];
    items.forEach(({ label, act }) => {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'bonzi-menu-item';
      btn.textContent = label;
      btn.addEventListener('click', () => { this._closeMenu(); act(); });
      menu.appendChild(btn);
    });
    document.body.appendChild(menu);
    const mw = menu.offsetWidth, mh = menu.offsetHeight;
    menu.style.left = Math.min(x, window.innerWidth - mw - 8) + 'px';
    menu.style.top = Math.min(y, window.innerHeight - mh - 8) + 'px';
    this._menu = menu;
    this._menuDismiss = (e) => {
      if (e.type === 'keydown' && e.key !== 'Escape') return;
      if (e.type === 'pointerdown' && menu.contains(e.target)) return;
      this._closeMenu();
    };
    // Deferred so the opening right-click doesn't instantly dismiss it.
    setTimeout(() => {
      document.addEventListener('pointerdown', this._menuDismiss, true);
      document.addEventListener('keydown', this._menuDismiss, true);
    }, 0);
  }

  _closeMenu() {
    if (this._menu) { this._menu.remove(); this._menu = null; }
    if (this._menuDismiss) {
      document.removeEventListener('pointerdown', this._menuDismiss, true);
      document.removeEventListener('keydown', this._menuDismiss, true);
      this._menuDismiss = null;
    }
  }

  _setOpt(key, val) {
    this._opts[key] = val;
    try { localStorage.setItem(LS_OPTS, JSON.stringify(this._opts)); } catch (_) {}
    if (key === 'voice' && val) this._say('Ahem. AHEM. Testing, testing. Oh, this is going to be great.', { voice: true });
    if (key === 'sounds' && val && this._soundBank) {
      const first = Object.values(this._soundBank)[0];
      if (first) { try { first.currentTime = 0; first.volume = 0.4; first.play().catch(() => {}); } catch (_) {} }
    }
  }
}

/* ── boot ──────────────────────────────────────────────────────────── */

const bonzi = new BonziBuddy();
window.bonziBuddy = bonzi;

window.addEventListener('aegis-bonzi-change', (e) => {
  if (e.detail && e.detail.enabled) bonzi.enable(false);
  else bonzi.disable(false);
});

if (localStorage.getItem(LS_ENABLED) === 'on') {
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => bonzi.enable(false));
  } else {
    bonzi.enable(false);
  }
}
