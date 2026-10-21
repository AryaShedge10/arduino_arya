/* _cam_tab.js — bring the C270 feed tab to life.
 *
 * The browser fetches /cam/mjpeg (multipart/x-mixed-replace) directly
 * via the <img> tag, so the only JS work is the live status pill +
 * dot + frame-age counter. We poll /cam/status every 1s while the tab
 * is visible and stop polling while it's hidden — there's no point
 * asking for FPS the user can't see.
 */
(function() {
  const $ = (id) => document.getElementById(id);

  function fmtAge(ms) {
    if (ms == null || isNaN(ms)) return '—';
    if (ms < 1000) return ms + ' ms';
    return (ms / 1000).toFixed(1) + ' s';
  }

  function setChip(state) {
    const chip = $('cam-status-chip');
    if (!chip) return;
    chip.classList.remove('cam-pending', 'cam-live', 'cam-stale');
    if (state === 'live') { chip.classList.add('cam-live'); chip.textContent = 'live'; }
    else if (state === 'stale') { chip.classList.add('cam-stale'); chip.textContent = 'stale'; }
    else { chip.classList.add('cam-pending'); chip.textContent = 'offline'; }
  }

  async function refresh() {
    const tab = $('tab-cam');
    if (!tab || tab.hidden) return;
    try {
      const r = await fetch('/cam/status', { cache: 'no-store' });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const s = await r.json();

      // Authoritative: connected AND fresh in the last 2 s.
      const connected = !!s.connected;
      const age = s.age_ms;
      const fresh = connected && age != null && age < 2000;

      $('cam-dev').textContent    = s.device || '—';
      $('cam-res').textContent    = s.width && s.height ? `${s.width}×${s.height}` : '—';
      $('cam-fps').textContent    = s.fps != null ? s.fps.toFixed(1) : '—';
      $('cam-age').textContent    = connected ? fmtAge(age) : '—';
      $('cam-frames').textContent = s.frame_seq != null ? String(s.frame_seq) : '—';
      $('cam-err').textContent    = s.last_error || '—';

      const dot  = $('cam-dot');
      const img  = $('cam-frame');
      const off  = $('cam-offline');
      const msg  = $('cam-offline-msg');

      if (fresh) {
        dot.classList.add('live'); dot.classList.remove('stale');
        $('cam-status').textContent = 'streaming';
        setChip('live');
        // Use a cache-buster URL only when we transition from offline
        // to live; the server already Cache-Control: no-caches the
        // stream so subsequent reloads stay current.
        if (img.dataset.live !== '1') {
          img.src = '/cam/mjpeg?ts=' + Date.now();
          img.alt = 'USB camera feed (live)';
          img.dataset.live = '1';
          off.classList.add('hidden');
        }
      } else if (connected) {
        dot.classList.remove('live'); dot.classList.add('stale');
        $('cam-status').textContent = 'stale';
        setChip('stale');
      } else {
        dot.classList.remove('live', 'stale');
        $('cam-status').textContent = s.last_error ? 'error' : 'offline';
        setChip(s.last_error ? 'stale' : 'pending');
        if (img.dataset.live === '1') {
          img.dataset.live = '0';
          img.src = '';
          img.alt = 'USB camera feed (offline)';
          off.classList.remove('hidden');
          msg.textContent = s.last_error || 'Re-plug the C270 or check /cam/status.';
        }
      }
    } catch (e) {
      $('cam-status').textContent = 'no dashboard';
      setChip('stale');
    }
  }

  let pollTimer = null;
  function startPolling() {
    if (pollTimer) return;
    refresh();
    pollTimer = setInterval(refresh, 1000);
  }
  function stopPolling() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  }

  // Watch the tab visibility via a small observer; clicks on the tab
  // button flip hidden via the existing tab-show code, so we listen
  // for the click and check hidden state.
  const btn = $('tabbtn-cam');
  const tab = $('tab-cam');
  if (btn && tab) {
    btn.addEventListener('click', () => {
      // The dashboard's tab switch code runs first; let the click settle.
      setTimeout(() => {
        if (tab.hidden) stopPolling();
        else startPolling();
      }, 0);
    });
    if (!tab.hidden) startPolling();
  }
  // Eager kick so the dot lights up even before the user clicks the tab.
  if (!tab || !tab.hidden) startPolling();
})();
