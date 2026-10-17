#!/usr/bin/env python3
"""AGV orchestrator: camera-only line following that drives an Arduino.

  camera  -> Vision (bird's-eye warp, line geometry, floor obstacles)
          -> LineController (targets: forward v, turn omega, emergency flag)
          -> MotionThread (fixed 50 Hz, jerk-limited S-curve per wheel)
          -> Arduino  ``M<left>,<right>``  (firmware v2, 1 s command timeout)
  telemetry -> status.json / frame.jpg for the web dashboard

The dashboard ON/OFF switch (control.json) gates the motors. The AGV always
starts OFF. See README_AGV.md for tuning.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import signal
import time
import traceback
from typing import Optional

import cv2
import numpy as np

from arduino import Arduino, ArduinoReport
from camera import Camera, CameraReport
from config import LOG_FILE, MANUAL_TURN_FRAC, OBST_ENABLED, SHOW_WINDOW, V_MAX
from control_state import MANUAL_STALE_S, ControlReader, write_enabled
from decision import LineController
from motion import MotionThread
from telemetry import (
    LOG_CAT_CAMERA, LOG_CAT_SYSTEM,
    SensorProbe, TelemetryPublisher, TelemetryState,
)
from vision import ObstacleResult, Vision, VisionResult

# ── Logging ──────────────────────────────────────────────────────────────
os.makedirs(os.path.dirname(LOG_FILE) or '.', exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger('agv')
logging.getLogger('werkzeug').setLevel(logging.WARNING)

FRAME_STALE_S = 0.5          # older than this = camera is not delivering
PUBLISH_PERIOD_S = 0.12      # dashboard refresh (frame + state)
FIRMWARE_WAIT_S = 3.0


# ── Helpers ────────────────────────────────────────────────────────────

def probe_to_dict(report, note: Optional[str] = None) -> dict:
    if isinstance(report, CameraReport):
        return SensorProbe(
            device='camera',
            port='CSI (IMX219 ribbon)' if report.pipeline else 'fake',
            status='OK' if report.frame_ok else ('NO_FRAMES' if report.opened else 'FAILED'),
            error=report.error,
            latency_ms=report.latency_ms,
        ).as_dict()
    if isinstance(report, ArduinoReport):
        return SensorProbe(
            device='arduino', port=report.port,
            status=('OK' if report.opened and not note
                    else 'ERROR' if report.opened else 'FAILED'),
            error=report.error or note, latency_ms=report.latency_ms,
        ).as_dict()
    return {}


# ── Orchestrator ────────────────────────────────────────────────────────

class AGV:
    """camera -> vision -> controller -> motion thread -> arduino."""

    def __init__(self) -> None:
        self.camera = Camera()
        self.arduino = Arduino()
        self.telemetry = TelemetryPublisher()
        self.vision = Vision()
        self.controller = LineController()
        self.motion = MotionThread(self.arduino)
        self.control = ControlReader()
        self.probes: list[dict] = []
        self.last_error: Optional[str] = None
        self.stop_requested = False
        self.stop_reason = 'main loop ended'
        self._last_seq = -1
        self._last_publish = 0.0
        self._enabled = False
        self._last: dict = {}
        self._vis = VisionResult()
        self._snap_t = 0.0
        self._dw: dict = {}
        self._snap_n = 0

    # ── startup ──────────────────────────────────────────────────────
    def probe(self) -> None:
        cam = self.camera.open()
        ard = self.arduino.connect()
        note = None
        if ard.opened:
            t_end = time.monotonic() + FIRMWARE_WAIT_S
            while time.monotonic() < t_end and not self.arduino.snapshot().protocol_ok:
                time.sleep(0.05)
            if not self.arduino.snapshot().protocol_ok:
                note = ('firmware v2 not detected - flash MotorControl.ino '
                        '(motors stay off)')
                log.error('[Arduino] %s', note)
        self.probes = [probe_to_dict(cam), probe_to_dict(ard, note)]

    def start(self) -> None:
        write_enabled(False)                    # never start driving on boot (speed kept)
        self.telemetry.restore_previous()
        self.telemetry.start()
        log.info('=' * 55)
        log.info('  AGV - camera line follower -> Arduino (dashboard OFF)')
        log.info('=' * 55)
        self.probe()
        log.info('[Probe] Camera:  %s', self.probes[0])
        log.info('[Probe] Arduino: %s', self.probes[1])
        self.motion.start()
        log.info('Running %s - Ctrl+C to stop',
                 'headlessly' if not SHOW_WINDOW else 'with local window')

    # ── shutdown ─────────────────────────────────────────────────────
    def stop(self) -> None:
        log.warning('Shutting down AGV (%s)...', self.stop_reason)
        self.motion.request_stop()                 # ramps to 0, then returns
        if self.motion.is_alive():
            self.motion.join(timeout=2.0)
        if self.arduino.isOpen():
            self.arduino.send('S', force=True)
            log.info('   Motors stopped.')
        self.camera.release()
        self.arduino.close()
        log.info('AGV shutdown complete.')
        self.telemetry.publish(
            TelemetryState(
                running=False, enabled=False,
                sensor_probes=self.probes, last_error=self.last_error,
                final_command='S',
            ),
            log_message=[
                (LOG_CAT_CAMERA, 'Camera disconnected (capture closed)'),
                (LOG_CAT_SYSTEM, f'AGV stopped ({self.stop_reason})'),
            ],
        )
        self.telemetry.stop()

    # ── main loop ────────────────────────────────────────────────────
    def tick(self) -> None:
        now = time.monotonic()
        info = self.camera.latest()
        frame_ok = info is not None and (now - info.monotonic) < FRAME_STALE_S
        if frame_ok and info.sequence == self._last_seq:
            time.sleep(0.004)                      # wait for a new frame
            return
        if not frame_ok:
            time.sleep(0.02)
        frame = info.image if frame_ok else None
        if frame_ok:
            self._last_seq = info.sequence

        vis = self.vision.analyze(frame, debug=True) if frame_ok else VisionResult()
        self._vis = vis

        ctl = self.control.state()
        self._snapshot_if_obstacle(now, frame, vis)
        enabled = ctl['enabled']
        speed_pct = ctl['max_speed_pct']
        # Dashboard master limit: every speed and turn target scales with it.
        scale = (speed_pct / 100.0) * 255.0 / V_MAX
        if enabled and not self._enabled:
            self.controller.reset()
            self.vision.reset_floor()       # operator re-armed: relearn the floor
        link_ok = self.arduino.link_alive()
        self.controller.speed_scale = scale
        _w = self.motion.snapshot()
        self.controller.moving = (enabled and ctl['mode'] == 'auto'
                                  and (abs(_w.left) + abs(_w.right)) > 80)
        enforce = ctl['obstacle_stop']
        d = self.controller.decide(vis.line, frame_ok,
                                   vis.obstacle if enforce else ObstacleResult(),
                                   link_ok, now)
        if not enforce and vis.obstacle.state != 'CLEAR':
            d = dataclasses.replace(
                d, status_text=d.status_text + f' [obstacle {vis.obstacle.state} ignored]')
        manual = ctl['mode'] == 'manual'
        if manual:
            d, v_t, w_t = self._manual_target(d, ctl, speed_pct, enabled)
            self.motion.set_target(v_t, w_t, d.emergency, enabled)
        else:
            v_t, w_t = d.v * scale, d.omega * scale
            self.motion.set_target(v_t, w_t, d.emergency, enabled)
        wheels = self.motion.snapshot()

        events = self._transitions(d, vis, enabled, frame_ok, ctl['mode'])
        self._enabled = enabled
        self._publish(now, frame, vis, d, wheels, enabled, link_ok, events,
                      speed_pct, v_t, w_t, ctl['mode'], enforce)

    def _publish(self, now, frame, vis, d, wheels, enabled, link_ok, events,
                 speed_pct, v_t, w_t, mode, enforce=True) -> None:
        if now - self._last_publish < PUBLISH_PERIOD_S and not events:
            return
        self._last_publish = now
        if frame is not None:
            out = frame.copy()
            self._draw_hud(out, vis, d, wheels, enabled, mode)
        else:
            out = np.zeros((720, 1280, 3), dtype=np.uint8)
            cv2.putText(out, 'NO CAMERA FRAME', (400, 360),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 0, 255), 3)
        line, obst = vis.line, vis.obstacle
        snap = self.arduino.snapshot()
        state = TelemetryState(
            running=True,
            camera_connected=self.camera.connected,
            camera_backend=self.camera.backend,
            stm32_connected=link_ok,
            stm32_ack_age_ms=self.arduino.ack_age_ms(),
            stm32_firmware_version=snap.firmware_version or 'unknown',
            camera_direction=d.direction.value,
            camera_command=d.camera_cmd,
            line_centroid_x=-1, line_centroid_y=-1,
            line_area=0, line_error_px=0,
            lidar_status=obst.state,                  # camera obstacle state
            lidar_connected=OBST_ENABLED,
            lidar_error=None if OBST_ENABLED else 'obstacle detection disabled',
            lidar_distance_mm=obst.distance_m * 1000.0,
            final_command=d.final_cmd,
            decision_reason=d.status_text,
            sensor_probes=self.probes,
            last_error=self.last_error,
            enabled=enabled,
            max_speed_pct=speed_pct,
            mode=mode,
            obstacle_enforced=enforce,
            track_state=d.track,
            steer_boost=d.boost,
            line_offset_m=line.offset_m,
            line_angle_deg=line.angle_deg,
            line_lookahead_m=line.lookahead_m,
            line_curvature=line.curvature,
            line_strips=line.strips_used,
            obstacle_state=obst.state,
            obstacle_distance_m=obst.distance_m,
            speed_target=v_t,
            omega_target=w_t,
            left_pwm=wheels.left,
            right_pwm=wheels.right,
            vision_ms=vis.ms,
        )
        self.telemetry.publish(state, frame=out, log_message=events or None)

    # ── HUD rendering ────────────────────────────────────────────────
    def _draw_hud(self, frame, vis: VisionResult, d, wheels, enabled, mode='auto') -> None:
        h, w = frame.shape[:2]
        line = vis.line
        if line.found:
            pts = self.vision.world_to_frame(list(line.curve), w, h)
            if len(pts) > 1:
                cv2.polylines(frame, [np.array(pts, np.int32)], False,
                              (0, 255, 0), 4, cv2.LINE_AA)
            if line.look_point is not None:
                lp = self.vision.world_to_frame([line.look_point], w, h)
                if lp:
                    cv2.circle(frame, lp[0], 14, (0, 0, 255), -1)
                    cv2.circle(frame, lp[0], 16, (255, 255, 255), 2)
        cv2.line(frame, (w // 2, 0), (w // 2, h), (255, 200, 0), 1)

        cv2.rectangle(frame, (0, 0), (w, 60), (0, 0, 0), -1)
        cv2.putText(frame, f'{d.final_cmd} | {d.status_text}', (10, 40),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.0, d.status_color, 2)
        tag, tag_col = (('OFF', (0, 0, 255)) if not enabled
                        else ('MANUAL', (0, 200, 255)) if mode == 'manual'
                        else ('ON', (0, 220, 0)))
        cv2.putText(frame, tag, (w - 40 - 28 * len(tag) - 100, 42), cv2.FONT_HERSHEY_SIMPLEX,
                    1.2, tag_col, 3, cv2.LINE_AA)
        ard_col = (0, 255, 0) if self.arduino.link_alive() else (0, 0, 255)
        cv2.circle(frame, (w - 40, 30), 14, ard_col, -1)

        info = (f'[{d.track} x{d.boost:.1f}]  off {line.offset_m:+.2f}m  ang {line.angle_deg:+.0f}deg  '
                f'look {line.lookahead_m:+.2f}m  curv {line.curvature:+.2f}  '
                f'strips {line.strips_used}   PWM L{wheels.left:+4d} R{wheels.right:+4d}')
        cv2.rectangle(frame, (0, 62), (w, 96), (0, 0, 0), -1)
        cv2.putText(frame, info, (10, 88), cv2.FONT_HERSHEY_SIMPLEX, 0.7,
                    (255, 255, 255), 2, cv2.LINE_AA)

        if vis.bev_debug is not None:
            bev = cv2.resize(vis.bev_debug, None, fx=0.8, fy=0.8)
            bh, bw = bev.shape[:2]
            frame[h - bh - 10:h - 10, w - bw - 10:w - 10] = bev
            cv2.rectangle(frame, (w - bw - 10, h - bh - 10), (w - 10, h - 10),
                          (255, 255, 255), 1)
            obst = vis.obstacle
            if obst.state != 'CLEAR':
                cv2.putText(frame, f'{obst.state} {obst.distance_m:.2f}m',
                            (w - bw - 10, h - bh - 18), cv2.FONT_HERSHEY_SIMPLEX,
                            0.8, (0, 0, 255) if obst.state == 'STOP' else (0, 200, 255),
                            2, cv2.LINE_AA)

    def _snapshot_if_obstacle(self, now, frame, vis) -> None:
        """Save the raw frame + detector views when an obstacle is flagged, so
        false stops can be diagnosed from real camera data (max 12, 3 s apart)."""
        if (frame is None or vis.obstacle.raw_state == 'CLEAR'
                or now - self._snap_t < 3.0 or self._snap_n >= 12):
            return
        self._snap_t, self._snap_n = now, self._snap_n + 1
        folder = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs', 'snapshots')
        os.makedirs(folder, exist_ok=True)
        stamp = time.strftime('%H%M%S')
        tag = f'{stamp}_{vis.obstacle.raw_state}_{vis.obstacle.distance_m:.2f}m'
        cv2.imwrite(os.path.join(folder, tag + '_frame.jpg'), frame)
        dbg = self.vision.debug_image()
        if dbg is not None:
            cv2.imwrite(os.path.join(folder, tag + '_views.png'), dbg)
        log.info('[Snapshot] obstacle %s saved to %s', vis.obstacle.raw_state, folder)

    def _manual_target(self, d, ctl, speed_pct, enabled):
        """Dashboard driving: hold-to-drive (deadman). The command goes through
        the same jerk limiter and the max-speed cap as automatic driving.
        Camera obstacle detection is NOT enforced here: the operator is."""
        age = time.time() - ctl['manual_ts']
        fresh = age < MANUAL_STALE_S
        fwd = ctl['manual_v'] if fresh else 0.0
        trn = ctl['manual_w'] if fresh else 0.0
        cap = speed_pct / 100.0 * 255.0
        v_t, w_t = fwd * cap, trn * cap * MANUAL_TURN_FRAC
        idle = fwd == 0.0 and trn == 0.0
        cmd = ('S' if idle else 'F' if fwd > 0 else 'B' if fwd < 0
               else 'R' if trn > 0 else 'L')
        text = ('MANUAL: hold a key / button to drive' if idle
                else f'MANUAL v={fwd:+.1f} turn={trn:+.1f}')
        d = dataclasses.replace(
            d, camera_cmd=cmd, final_cmd=cmd, v=v_t, omega=w_t,
            emergency=(idle or not fresh), status_text=text,
            status_color=(0, 200, 255))
        return d, v_t, w_t

    # ── state-transition events for the log ───────────────────────────
    def _stable(self, key: str, value, now: float, dwell: float = 0.6):
        """Return (previous_logged, value) once ``value`` has held for ``dwell``
        seconds and differs from what was last logged; else None. Stops a
        value that hovers at a threshold from flooding the log."""
        cur = self._dw.setdefault(key, {'v': value, 'since': now, 'logged': value})
        if value != cur['v']:
            cur['v'], cur['since'] = value, now
        if cur['v'] != cur['logged'] and now - cur['since'] >= dwell:
            prev, cur['logged'] = cur['logged'], cur['v']
            return prev, cur['v']
        return None

    def _transitions(self, d, vis, enabled, frame_ok, mode='auto') -> list[tuple[str, str]]:
        now = time.monotonic()
        events: list[tuple[str, str]] = []
        link = self.arduino.link_alive()
        for key, val, cat, fmt, dwell in (
            ('enabled', enabled, LOG_CAT_SYSTEM, lambda p, c: 'Motors ENABLED from dashboard' if c else 'Motors DISABLED from dashboard', 0.0),
            ('mode', mode, LOG_CAT_SYSTEM, lambda p, c: f'Mode: {p} -> {c}', 0.0),
            ('cam', frame_ok, LOG_CAT_CAMERA, lambda p, c: 'Camera frames OK' if c else 'Camera frames stale', 0.5),
            ('link', link, LOG_CAT_SYSTEM, lambda p, c: 'Arduino link up' if c else 'Arduino link DOWN', 0.5),
            ('obst', vis.obstacle.state, LOG_CAT_CAMERA, lambda p, c: f'Obstacle {p} -> {c} ({vis.obstacle.distance_m:.2f} m)', 0.5),
            ('dir', d.direction.value, LOG_CAT_CAMERA, lambda p, c: f'Line: {p} -> {c}', 0.8),
            ('cmd', d.final_cmd, LOG_CAT_SYSTEM, lambda p, c: f'Final command: {p} -> {c}', 0.8),
        ):
            ch = self._stable(key, val, now, dwell)
            if ch:
                events.append((cat, fmt(*ch)))
        # Tracking: only report real problems and recovery, not ON_LINE/CORRECTING.
        bad = d.track in ('STUCK', 'DRIFTING', 'OFF_LINE', 'ALIGNING')
        ch = self._stable('trackbad', d.track if bad else 'OK', now, 0.5)
        if ch:
            events.append((LOG_CAT_CAMERA,
                           f'Tracking: {ch[1]} (boost x{d.boost:.1f})' if ch[1] != 'OK'
                           else 'Tracking: back on the line / correcting normally'))
        return events


# ── Entry point ─────────────────────────────────────────────────────────

def main() -> None:
    agv = AGV()

    def _request_stop(signum, _frame) -> None:
        agv.stop_requested = True
        agv.stop_reason = 'stop requested (signal %d)' % signum

    signal.signal(signal.SIGINT, _request_stop)
    signal.signal(signal.SIGTERM, _request_stop)
    try:
        agv.start()
        while not agv.stop_requested:
            try:
                agv.tick()
            except Exception as exc:
                agv.last_error = f'{type(exc).__name__}: {exc}'
                log.error('[MainLoop] Unhandled exception - staying alive\n%s',
                          traceback.format_exc())
                # The motion thread sees stale targets and stops the wheels.
                time.sleep(0.5)
    finally:
        agv.stop()


if __name__ == '__main__':
    main()
