// Settings → Appearance → "Reading aloud": the voice this person is read to in, and whether
// every answer is read by itself. The choice is kept with the account (PUT /api/tts/reading),
// so it is the same on the phone and at the desk. Voices recorded in the Studio show here once
// they are this person's to use: their own, or one its owner shared with the household.

import readAloud from './readAloud.js';

const SAMPLE = 'This is how I sound when I read an answer to you.';

/** What to say under the picker about the chosen voice: how soon it starts. */
export function noteFor(voice) {
  if (!voice) return '';
  if (voice.kind === 'device') return 'Starts at once. Nothing leaves this device.';
  const pace = Number(voice.pace) || 0;
  const whose = voice.kind === 'recorded' ? (voice.mine ? 'Your recording. ' : (voice.owner ? `Recorded by ${voice.owner}. ` : 'A recorded voice. ')) : '';
  if (pace >= 1) return `${whose}Made on the home server; it keeps up with itself, so it starts after a moment.`;
  if (pace > 0) {
    const each = Math.max(1, Math.round(10 / pace));
    return `${whose}Made on the home server, which takes about ${each} s for every 10 s of speech here, so an answer is prepared ahead before it starts. A second listen is instant.`;
  }
  return `${whose}Made on the home server.`;
}

export function initReadingSettings() {
  const card = document.getElementById('reading-card');
  const select = document.getElementById('reading-voice');
  const preview = document.getElementById('reading-preview');
  const note = document.getElementById('reading-note');
  const auto = document.getElementById('reading-auto');
  if (!card || !select) return;
  let voices = [];
  let previewing = null;

  const say = (text, bad = false) => { if (note) { note.textContent = text; note.classList.toggle('bad', !!bad); } };
  const chosenVoice = () => voices.find((v) => v.id === select.value) || null;

  async function fill(fresh = false) {
    const mine = await readAloud.reading(fresh);
    const deviceOk = await readAloud.deviceCanSpeak();
    voices = (mine.voices || []).filter((v) => v.kind !== 'device' || deviceOk);
    select.replaceChildren();
    // "" = whatever the house uses: offered when that is something this person can be read to in.
    const house = mine.house && voices.some((v) => v.id === mine.house) ? mine.house : '';
    if (house) { const option = document.createElement('option'); option.value = ''; option.textContent = `The house voice (${house})`; select.appendChild(option); }
    voices.forEach((voice) => {
      const option = document.createElement('option');
      option.value = voice.id;
      option.textContent = voice.kind === 'recorded' ? `${voice.label}${voice.mine ? ' (your recording)' : ' (shared with you)'}` : voice.label;
      select.appendChild(option);
    });
    if (!select.options.length) {
      const option = document.createElement('option'); option.value = ''; option.textContent = 'No voice on this device or the server'; select.appendChild(option);
      select.disabled = true; if (preview) preview.disabled = true; if (auto) auto.disabled = true;
      say('This device has no voice of its own, and none is set up on the server. A voice can be recorded in the Studio (Music → Voice Lab).');
      return;
    }
    select.disabled = false; if (preview) preview.disabled = false; if (auto) auto.disabled = false;
    const want = mine.voice && [...select.options].some((o) => o.value === mine.voice) ? mine.voice : (house ? '' : mine.reads_in);
    select.value = [...select.options].some((o) => o.value === want) ? want : select.options[0].value;
    if (auto) auto.checked = !!mine.auto;
    say(noteFor(chosenVoice() || voices.find((v) => v.id === mine.reads_in)));
  }

  select.addEventListener('change', async () => {
    try {
      await readAloud.chooseReading({ voice: select.value });
      say(noteFor(chosenVoice()));
      if (window.aiTTSManager) window.aiTTSManager.checkAvailability();
    } catch (error) { say(error.message || 'That could not be saved.', true); fill(true); }
  });
  if (auto) auto.addEventListener('change', async () => {
    if (window.aiTTSManager) await window.aiTTSManager.setAuto(auto.checked);
    else { try { await readAloud.chooseReading({ auto: auto.checked }); } catch (_) { /* shown on the next open */ } }
  });
  if (preview) preview.addEventListener('click', () => {
    if (previewing) { previewing.stop(); return; }
    readAloud.unlock();
    const voice = chosenVoice();
    preview.textContent = 'Stop';
    previewing = readAloud.read(SAMPLE, { voice: select.value || '', onState: (state) => {
      if (state.phase === 'preparing' || state.phase === 'waiting') say(readAloud.describe(state) + '…');
      else if (state.phase === 'playing') say(noteFor(voice));
      else { previewing = null; preview.textContent = 'Hear it'; say(state.phase === 'failed' ? (state.message || 'It could not be read aloud.') : noteFor(voice), state.phase === 'failed'); }
    } });
  });

  // Fill when Settings is opened (the list is cheap, but a new recording should show without a reload).
  document.addEventListener('aegis-voices-changed', () => fill(true));
  document.addEventListener('aegis-reading-changed', () => { if (auto && window.aiTTSManager) auto.checked = !!window.aiTTSManager.autoPlay; });
  const opener = document.getElementById('user-bar-settings');
  if (opener) opener.addEventListener('click', () => fill(true));
  fill();
}

export default { initReadingSettings, noteFor };
