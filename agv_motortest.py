"""Motor / encoder bench test: Arduino + dashboard only.

No camera, no vision, no line following. Use it to test each motor driver,
the encoders and the PID from the dashboard's "Motor test" tab without
starting the whole project:

    ./agv_motortest.sh

It cannot run together with the full AGV (agv_run.sh): both need the Arduino's
serial port. Motors start OFF; sign in on the dashboard, press "Enable motor
test", then use the sliders / buttons. Everything stops when the browser
stops sending (deadman), on STOP, or on Ctrl+C.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import time

from arduino import Arduino
from config import LOG_FILE, TICKS_PER_M, WHEEL_DIAMETER_M, WHEEL_MAX_MPS
from control_state import ControlReader, write_enabled
from manual import manual_command
from motion import MotionThread
from telemetry import (
    LOG_CAT_STM32, LOG_CAT_SYSTEM, SensorProbe, TelemetryPublisher,
    TelemetryState,
)

log = logging.getLogger('agv.motortest')
PERIOD_S = 0.05          # 20 Hz control read; the motion thread runs at 50 Hz
PUBLISH_S = 0.12
FIRMWARE_WAIT_S = 3.0


def _agv_running() -> bool:
    me = os.getpid()
    for pid in filter(str.isdigit, os.listdir('/proc')):
        if int(pid) == me:
            continue
        try:
            with open(f'/proc/{pid}/cmdline', 'rb') as fh:
                cmd = fh.read().replace(b'\0', b' ').decode(errors='replace')
        except OSError:
            continue
        if 'agv_drive_arduino.py' in cmd:
            return True
    return False


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    if _agv_running():
        print('The full AGV (agv_drive_arduino.py) is running and owns the Arduino.\n'
              'Use its dashboard Motor test tab, or stop it first.', file=sys.stderr)
        return 2
    stop = {'now': False}
    signal.signal(signal.SIGINT, lambda *_: stop.update(now=True))
    signal.signal(signal.SIGTERM, lambda *_: stop.update(now=True))

    write_enabled(False)                      # always start with the motors OFF
    control = ControlReader()
    arduino = Arduino()
    telemetry = TelemetryPublisher()
    telemetry.restore_previous()
    telemetry.start()
    report = arduino.connect()
    note = None
    if report.opened:
        t_end = time.monotonic() + FIRMWARE_WAIT_S
        while time.monotonic() < t_end and not arduino.snapshot().protocol_ok:
            time.sleep(0.05)
        if not arduino.snapshot().protocol_ok:
            note = 'firmware not detected - flash MotorControl.ino (motors stay off)'
    probes = [SensorProbe(device='arduino', port=report.port,
                          status='OK' if report.opened and not note else
                          'ERROR' if report.opened else 'FAILED',
                          error=report.error or note,
                          latency_ms=report.latency_ms).as_dict()]
    motion = MotionThread(arduino)
    motion.start()
    log.info('Motor test ready (no camera). Dashboard: sign in -> Motor test tab.')
    telemetry.publish(TelemetryState(running=True), log_message=[
        (LOG_CAT_SYSTEM, 'Motor test tool started (no camera, no line following)')])

    last_pub = 0.0
    enc_zero_seen = time.time()
    enabled_prev = False
    last_error = None
    try:
        while not stop['now']:
            t0 = time.monotonic()
            ctl = control.state()
            if ctl['enc_zero_ts'] > enc_zero_seen:
                enc_zero_seen = ctl['enc_zero_ts']
                arduino.zero_encoders()
            enabled = ctl['enabled']
            m = manual_command(ctl, ctl['max_speed_pct'])
            # Only the dashboard's manual / test commands drive in this tool.
            manual = ctl['mode'] == 'manual'
            if not manual:
                m.idle, m.v, m.omega, m.raw = True, 0.0, 0.0, False
            motion.set_closed_loop(ctl['closed_loop'] and not m.raw)
            motion.set_raw_pwm(m.raw)
            motion.set_target(m.v, m.omega, m.idle or not m.fresh or not manual, enabled)
            wheels = motion.snapshot()
            snap = arduino.snapshot()
            link = arduino.link_alive()
            events = []
            if enabled != enabled_prev:
                events.append((LOG_CAT_SYSTEM, 'Motors ENABLED from dashboard' if enabled
                               else 'Motors DISABLED from dashboard'))
                enabled_prev = enabled
            now = time.monotonic()
            if now - last_pub >= PUBLISH_S or events:
                last_pub = now
                state = TelemetryState(
                    running=True, camera_connected=False, camera_backend='none (motor test)',
                    stm32_connected=link, stm32_ack_age_ms=arduino.ack_age_ms(),
                    stm32_firmware_version=snap.firmware_version or 'unknown',
                    camera_direction='UNAVAILABLE', camera_command=m.cmd, final_command=m.cmd,
                    decision_reason='MOTOR TEST (no camera): ' + (
                        m.text if manual else 'switch to MANUAL to test'),
                    sensor_probes=probes, last_error=last_error,
                    enabled=enabled, mode=ctl['mode'], obstacle_enforced=False,
                    max_speed_pct=ctl['max_speed_pct'], speed_target=m.v, omega_target=m.omega,
                    left_pwm=wheels.left, right_pwm=wheels.right,
                    stm32_encoder_left=snap.encoder_left, stm32_encoder_right=snap.encoder_right,
                    wheel_closed_loop=wheels.closed, closed_loop_wanted=ctl['closed_loop'],
                    fw_closed_ready=arduino.closed_loop_ready(),
                    wheel_tgt_l_mps=wheels.left_mps, wheel_tgt_r_mps=wheels.right_mps,
                    wheel_meas_l_mps=snap.meas_ticks_left / TICKS_PER_M,
                    wheel_meas_r_mps=snap.meas_ticks_right / TICKS_PER_M,
                    wheel_applied_l=snap.applied_left, wheel_applied_r=snap.applied_right,
                    enc_fault_l=bool(snap.fault_bits & 1), enc_fault_r=bool(snap.fault_bits & 2),
                    wheel_max_mps=WHEEL_MAX_MPS, wheel_diameter_m=WHEEL_DIAMETER_M,
                    enc_rel_l=snap.encoder_left - snap.enc_zero_left,
                    enc_rel_r=snap.encoder_right - snap.enc_zero_right,
                    ticks_per_m=TICKS_PER_M,
                )
                telemetry.publish(state, log_message=events or None)
            time.sleep(max(0.0, PERIOD_S - (time.monotonic() - t0)))
    finally:
        log.info('Stopping motors...')
        motion.request_stop()
        motion.join(timeout=2.0)
        if arduino.isOpen():
            arduino.send('S', force=True)
        arduino.close()
        write_enabled(False)
        telemetry.publish(TelemetryState(running=False), log_message=[
            (LOG_CAT_SYSTEM, 'Motor test tool stopped')])
        telemetry.stop()
    return 0


if __name__ == '__main__':
    sys.exit(main())
