/* _cam_tab.js — bring the C270 feed tab to life.
 *
 * The browser fetches /cam/mjpeg (multipart/x-mixed-replace) directly
 * via the <img> tag, so the only JS work is the live status pill +
 * dot + frame-age counter, plus one chip in the summary strip.
 *
 * /cam/status is polled every 1s whether or not the tab is open: the
 * strip chip is the only place the operator sees this camera while
 * they work in another tab, so the poll has to keep running. The tab's
 * own fields (resolution, FPS, frame age, the stream <img>) are only
 * touched when the tab is visible.
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

  // Summary-strip chip for the USB camera. The AGV pipeline's own camera
  // has a chip next to this one (sum-cam) fed by telemetry; this one is fed
  // from /cam/status so it stays honest when the AGV itself isn't running.
  function setStrip(s, connected, fresh) {
    if (!$('sum-usbcam')) return;
    const txt = fresh ? 'OK' : connected ? 'STALE' : 'NONE';
    const cls = fresh ? 'good' : connected ? 'warn' : 'bad';
    chip('sum-usbcam', txt, cls);
    $('sum-usbcam').title = [
      'USB camera',
      s.device || 'no device',
      s.width && s.height ? `${s.width}×${s.height}` : '',
      s.fps != null ? s.fps.toFixed(1) + ' fps' : '',
      connected ? 'frame age ' + fmtAge(s.age_ms) : 'not streaming',
      s.last_error || '',
    ].filter(Boolean).join(' · ');
  }

  function setStripDown() {
    if (!$('sum-usbcam')) return;
    chip('sum-usbcam', 'DOWN', 'bad');
    $('sum-usbcam').title = 'USB camera · /cam/status did not answer';
  }

  async function refresh() {
    const tab = $('tab-cam');
    const visible = !!tab && !tab.hidden;
    try {
      const r = await fetch('/cam/status', { cache: 'no-store' });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const s = await r.json();

      // Authoritative: connected AND fresh in the last 2 s.
      const connected = !!s.connected;
      const age = s.age_ms;
      const fresh = connected && age != null && age < 2000;

      setStrip(s, connected, fresh);
      if (!visible) return;

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
      setStripDown();
      if (!visible) return;
      $('cam-status').textContent = 'no dashboard';
      setChip('stale');
    }
  }

  // One timer for the page. It used to be stopped while the tab was hidden,
  // which would have frozen the strip chip at whatever the camera was doing
  // when the operator last looked at it.
  refresh();
  setInterval(refresh, 1000);
})();
