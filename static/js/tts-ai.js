// static/js/tts-ai.js
// Reading answers aloud in the full app.
//
// The reading itself — which voice, cutting an answer into pieces, preparing a slow voice
// ahead, the device's own voice — is static/js/readAloud.js, shared with the family screen.
// This file is what the rest of the app talks to (window.aiTTSManager, addAITTSButton):
//
//   a speaker under every answer      tap to hear it, tap again to stop; while a recorded voice
//                                      is being prepared it shows how far along it is
//   "Read answers aloud"              (the + menu, and Settings) reads every answer as it is written
//   the person's own voice             chosen in Settings → Appearance or in the Studio's Voice Lab;
//                                      it is theirs, not the house's (services/tts/reading.py)

import readAloud from './readAloud.js';
import { MARK_HTML, ensureStyles } from './working.js';

const ICON_SPEAKER = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><path d="M15.54 8.46a5 5 0 0 1 0 7.07"/><path d="M18.5 5.5a9 9 0 0 1 0 13"/></svg>';
const ICON_STOP = '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" stroke="none"><rect x="5" y="5" width="14" height="14" rx="2"/></svg>';

class AITTSManager {
    constructor() {
        this.available = false;        // there is some voice to read to this person with
        this.autoPlay = false;         // read every answer without being asked
        this.isPlaying = false;
        this._processing = false;      // a reading is being prepared or played
        this._provider = 'disabled';   // kept for older callers: 'disabled' | 'device' | 'server'
        this._reader = null;           // the reading going on now
        this._button = null;           // the button showing it
        this._streaming = false;
        this.checkAvailability();
    }

    /** Ask what this person can be read to in. Sets `available`, `autoPlay` and the page's can-read-aloud mark. */
    async checkAvailability() {
        try {
            const mine = await readAloud.reading(true);
            const server = mine.reads_in && mine.reads_in !== 'device';
            const device = await readAloud.deviceCanSpeak();
            this.available = !!(server || device);
            this._provider = server ? 'server' : (device ? 'device' : 'disabled');
            this.autoPlay = !!mine.auto && this.available;
        } catch (error) {
            console.error('Failed to check what can read aloud:', error);
            this.available = false;
            this._provider = 'disabled';
        }
        if (typeof document !== 'undefined' && document.body) {
            document.body.classList.toggle('can-read-aloud', this.available);
            document.dispatchEvent(new CustomEvent('aegis-reading-changed', { detail: { available: this.available, auto: this.autoPlay } }));
        }
        return this.available;
    }

    /** Switch reading every answer aloud on or off, for this person, wherever they sign in. */
    async setAuto(on) {
        this.autoPlay = !!on && this.available;
        if (on) readAloud.unlock();
        try { await readAloud.chooseReading({ auto: !!on }); } catch (error) { console.warn('Could not save the reading choice:', error); }
        document.dispatchEvent(new CustomEvent('aegis-reading-changed', { detail: { available: this.available, auto: this.autoPlay } }));
        return this.autoPlay;
    }

    /** Kept for callers that still pass rendered HTML or markdown: the words as they are said. */
    extractPlainText(content) { return readAloud.sayable(content); }

    _show(button, state) {
        const live = state.phase === 'preparing' || state.phase === 'playing' || state.phase === 'waiting';
        this._processing = live;
        this.isPlaying = state.phase === 'playing';
        if (!button) return;
        const status = button._status;
        if (!live) {
            button.innerHTML = ICON_SPEAKER;
            button.classList.remove('playing', 'loading');
            button.title = 'Read aloud';
            button.setAttribute('aria-label', 'Read aloud');
            if (status) { status.textContent = ''; status.hidden = true; }
            if (state.phase === 'failed' && state.message) {
                try { window.uiModule && window.uiModule.showToast ? window.uiModule.showToast(state.message) : console.warn(state.message); } catch (_) { /* no toast */ }
                if (status) { status.textContent = state.message; status.hidden = false; setTimeout(() => { if (status.textContent === state.message) { status.textContent = ''; status.hidden = true; } }, 6000); }
            }
            return;
        }
        const words = readAloud.describe(state);
        button.classList.toggle('playing', state.phase === 'playing');
        button.classList.toggle('loading', state.phase !== 'playing');
        // Playing: a square to stop it. Preparing or catching up: the mark that moves.
        const want = state.phase === 'playing' ? 'stop' : 'mark';
        if (button.dataset.shows !== want) { button.innerHTML = want === 'stop' ? ICON_STOP : MARK_HTML; button.dataset.shows = want; }
        button.title = state.phase === 'playing' ? 'Stop reading' : `${words}. Tap to stop`;
        button.setAttribute('aria-label', button.title);
        if (status) { const say = state.phase === 'playing' ? '' : words; status.textContent = say; status.hidden = !say; }
    }

    _begin(button, make) {
        this.stop();
        ensureStyles();
        const mine = { button };
        const reader = make((state) => {
            if (this._current !== mine) return;
            if (state.phase === 'done' || state.phase === 'stopped' || state.phase === 'failed') { this._current = null; this._reader = null; delete (mine.button || {}).dataset?.shows; }
            this._show(mine.button, state);
        });
        this._current = mine;
        this._reader = reader;
        this._processing = true;
        if (mine.button) this._show(mine.button, { phase: 'preparing', kind: this._provider === 'device' ? 'device' : 'server', ready: 0, total: 0, startsIn: 0 });
        return reader;
    }

    /** Read `text` aloud; `button` (when given) shows how it is going. */
    enqueue(text, button) {
        if (!this.available) return null;
        return this._begin(button || null, (onState) => readAloud.read(text, { onState }));
    }

    /** Read text aloud with no button (Voice Mode's spoken replies). Resolves when it has been said or stopped. */
    play(text) {
        if (!this.available) return Promise.reject(new Error('There is no voice to read aloud with'));
        return new Promise((resolve) => {
            this._begin(null, (onState) => readAloud.read(text, { onState: (state) => { onState(state); if (state.phase === 'done' || state.phase === 'stopped' || state.phase === 'failed') resolve(); } }));
        });
    }

    stop() {
        this._streaming = false;
        const was = this._current;
        this._current = null;
        this._reader = null;
        readAloud.stopAll();
        this._processing = false;
        this.isPlaying = false;
        if (was && was.button) { delete was.button.dataset.shows; this._show(was.button, { phase: 'stopped' }); }
    }

    // ── an answer read while it is still being written ──

    streamingStart() {
        if (!this.available || !this.autoPlay) return;
        readAloud.warm();                           // a recorded voice may have been put away: have it back by the first sentence
        this._begin(null, (onState) => readAloud.open({ onState }));
        this._streaming = true;                     // after _begin: starting a reading first stops whatever was going on
    }

    /** `text` is what the answer's current step has written so far (a step's text starts again from nothing). */
    streamingUpdate(text) {
        if (!this._streaming || !this._reader || !this._reader.feed) return;
        this._reader.feed(text);
    }

    /** A new step of the same answer begins (after a tool): its text starts from nothing again. */
    streamingNewRound() {
        if (this._streaming && this._reader) this._reader.consumed = 0;
    }

    /** The answer's own speaker now exists: let it show the reading that is already going on. */
    streamingAttachButton(button) {
        if (!this._current) return;
        this._current.button = button;
        if (this._reader) this._show(button, { phase: this._reader.phase, kind: this._reader.kind || 'server',
            ready: this._reader.queue.filter((p) => p.state === 'ready').length, total: this._reader.queue.length, startsIn: 0 });
    }

    streamingEnd(finalText) {
        if (!this._streaming) return;
        this._streaming = false;
        if (this._reader && this._reader.finish) this._reader.finish(finalText);
    }

    clearCache() { /* what was made lives with the page and the server; nothing to clear here */ }
}

// Create global AI TTS manager instance
window.aiTTSManager = new AITTSManager();

/**
 * The speaker for one answer. `getText` gives the words when it is tapped (the answer may be edited meanwhile).
 * Returns [button, status]: the status is the line beside it that says how a slow voice is getting on.
 */
export function readAloudButton(getText) {
    const button = document.createElement('button');
    button.className = 'ai-tts-button';
    button.type = 'button';
    button.title = 'Read aloud';
    button.setAttribute('aria-label', 'Read aloud');
    button.innerHTML = ICON_SPEAKER;
    const status = document.createElement('span');
    status.className = 'read-aloud-status';
    status.setAttribute('role', 'status');
    status.hidden = true;
    button._status = status;
    button.addEventListener('click', (event) => {
        event.stopPropagation();
        const manager = window.aiTTSManager;
        const mine = manager._current && manager._current.button === button;
        if (mine) { manager.stop(); return; }
        readAloud.unlock();                                  // this tap is what lets the sound start when it is ready
        const text = typeof getText === 'function' ? getText() : String(getText || '');
        if (!text || !text.trim()) return;
        manager.enqueue(text, button);
    });
    return [button, status];
}

/** Put a speaker on an answer that has its row of buttons but no speaker yet (answers drawn by older code paths). */
export function addAITTSButton(messageElement, text) {
    if (!messageElement || messageElement.querySelector('.ai-tts-button')) return;
    const actions = messageElement.querySelector('.msg-actions');
    if (!actions) return;
    const [button, status] = readAloudButton(() => messageElement.dataset.raw || text || '');
    actions.insertBefore(status, actions.firstChild);
    actions.insertBefore(button, actions.firstChild);
}

// Stop reading when the page goes away
window.addEventListener('beforeunload', () => {
    if (window.aiTTSManager) window.aiTTSManager.stop();
});

export { AITTSManager };

const ttsModule = { AITTSManager, addAITTSButton, readAloudButton };
export default ttsModule;
