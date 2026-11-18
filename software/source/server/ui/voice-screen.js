/* Display only the privacy-safe voice and screen projection. */
(() => {
  'use strict';
  const $ = id => document.getElementById('vs-' + id);
  const set = (id,value) => { if ($(id).textContent !== value) $(id).textContent = value; };
  const unavailable = 'Unavailable';
  const activity = {listening:'Receiving voice input', transcribing:'Transcribing voice input',
    processing:'Processing a request', error:'Runtime reported an error', unobserved:'No input activity reported'};
  const playback = {recent:'Audio client submitted audio to the output stream',
    buffered:'Audio sent · waiting for the output stream to drain',
    no_audio:'Speech generation ended without audio · nothing was delivered',
    stop_requested:'Stop requested · client cancellation unconfirmed',
    cancelled:'Speech cancelled · device stop unconfirmed', client_cancelled:'Audio client cancelled playback',
    drained:'Audio client reported playback drained',
    failed:'Audio client reported playback failure', disconnected:'Audio client disconnected · completion unknown',
    unconfirmed:'Speech prepared · playback unconfirmed', unobserved:'No recent playback report', unavailable};
  const capture = {fresh:'Running · recent frame reported', waiting:'Running · waiting for the first frame',
    stale:'Running · last frame is stale', stopped:'Background capture is stopped', error:'Capture status could not be read', unavailable};
  const synthesis = {gemini:'Gemini produced audio', local:'Local neural voice produced audio',
    sapi:'Windows speech produced audio', none:'No audio produced'};
  let snapshot, status = 'waiting', pending = false, change;
  const receipt = {queued:'Queued for the audio client', sent:'Sent · client application unconfirmed',
    applied:'Audio client applied the software input gate', failed:'Voice setting delivery or application failed',
    disconnected:'Audio client disconnected · application unknown', unconfirmed:'Audio client application unconfirmed'};

  function paint() {
    const ready = status === 'ok' && snapshot?.available === true;
    const observed = ready ? snapshot : {};
    const {listening:l = {}, speaking:s = {}, screen:v = {}} = observed;
    set('connection', ready ? 'HUD bridge connected' : status === 'waiting' ? 'Waiting for the bridge…' :
      status === 'disconnected' ? 'HUD bridge unavailable' : 'Voice and screen status unavailable');
    set('preference', typeof l.paused === 'boolean' ? (l.paused ? 'Paused for this session' : 'Allowed for this session') : unavailable);
    set('input', activity[l.activity] || unavailable);
    set('followup', typeof l.followup === 'boolean' ? (l.followup ? 'Open' : 'Closed') : unavailable);
    set('wake', Number.isFinite(l.wake_match_at) ? new Date(l.wake_match_at * 1000).toLocaleTimeString() :
      ready ? 'No match reported this session' : unavailable);
    set('playback', playback[s.playback] || unavailable);
    set('synthesis', s.degraded === true ? 'Temporarily unavailable · retry handled by FRIDAY' :
      s.degraded === false ? 'No cooldown reported' : unavailable);
    set('capture-setting', typeof v.background_enabled === 'boolean' ?
      (v.background_enabled ? 'Continuous capture configured' : 'Continuous capture disabled') : unavailable);
    set('capture', (capture[v.background] || unavailable) + (v.reported_issue ? ' · capture issue reported' : ''));
    set('history', Number.isFinite(v.history_seconds) ? (v.history_seconds > 0 ?
      `Configured for up to ${v.history_seconds} seconds in memory` : 'History disabled') : unavailable);
    set('request', v.request === 'observed' ? 'Screen request in progress' :
      v.request === 'unobserved' ? 'No tracked screen request' : unavailable);
    set('synthesis-result', synthesis[s.synthesis_result] || unavailable);
    set('frame-age', Number.isFinite(v.frame_age) ? `${v.frame_age.toFixed(1)} s` : unavailable);
    set('first-audio', Number.isFinite(s.first_audio_seconds) ? `${s.first_audio_seconds.toFixed(2)} s` : unavailable);
    const control = $('pause');
    control.hidden = !(ready && l.control_available && typeof l.paused === 'boolean');
    control.setAttribute('aria-disabled', String(pending));
    set('pause', pending ? 'Sending voice setting…' : l.paused ? 'Resume voice input' : 'Pause voice input');
    set('input-help', control.hidden ? 'Voice input control is unavailable.' :
      (receipt[l.client_receipt?.state] || receipt.unconfirmed) + '. ' +
      (l.client_receipt?.startup_check ? 'Devices are disabled for this startup check.' :
        'This session setting does not hardware-mute the microphone.'));
  }

  window.FieldVoiceScreen = {
    init(apply) {
      change = apply;
      $('pause').onclick = () => {
        if (pending || $('pause').hidden) return;
        change(!snapshot.listening.paused);
      };
      paint();
    },
    receive(data,nextStatus) {
      if (data && snapshot && data.sequence < snapshot.sequence) return;
      status = nextStatus;
      if (data) snapshot = data;
      paint();
    },
    playbackPending() {
      if (!snapshot?.speaking) return;
      snapshot.speaking.playback = 'unavailable';
      snapshot.speaking.first_audio_seconds = null;
      paint();
    },
    setting(isPending,result) {
      pending = isPending;
      if (result?.accepted && snapshot?.listening) {
        snapshot.listening.paused = result.paused;
        snapshot.listening.client_receipt = result.client_receipt;
        if (result.paused) snapshot.listening.followup = false;
      }
      if (isPending || result) {
        set('action-result', isPending ? 'Waiting for the server…' : result.message);
        $('action-result').dataset.error = String(!!result && !result.accepted);
      }
      paint();
    }
  };
})();
