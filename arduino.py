"""Arduino motor-controller driver.

Mirrors the small slice of the AGV project's ``STM32`` interface that
``agv_main.py`` consumes (``send``, ``isOpen``, ``ack_age_ms``,
``link_alive``, ``connected``, ``close``) so the same orchestrator code
can drive an Arduino over USB-serial instead of an STM32 over UART.

Protocol
--------
The Arduino firmware (matching the user's existing ``control.py``):

- Accepts a single ASCII byte + ``\\n`` as a direction command.
- One of: ``F`` (forward), ``B`` (backward), ``L`` (left), ``R``
  (right), ``S`` (stop). ``Q`` is a quit sentinel (not sent by the
  AGV — it stops motors cleanly with ``S`` first).
- Replies with a one-line status string such as
  ``ACK:R -> drive(+1,-1)`` or ``STATE LEFT_DIR(D8)=HIGH ...``. Lines
  starting with ``ACK:`` count as link-alive.

The driver is intentionally simple: the AGV publishes a decision every
~50 ms (the camera runs at 60 fps but the orchestrator throttles
command sends to ``ARDUINO_CMD_HZ``), and the Arduino executes them
directly. There's no speed byte, no encoder telemetry, and no firmware
version — the STM32 fields that the dashboard exposes will show
zero/empty.
"""

from __future__ import annotations

import dataclasses
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import List, Optional

import serial

from config import (
    ARDUINO_ACK_TIMEOUT_MS, ARDUINO_BAUD, ARDUINO_CMD_HZ, ARDUINO_PORT,
    MIN_PWM, PID_KD, PID_KI, PID_KP, TICKS_PER_M, WHEEL_MAX_MPS,
)

log = logging.getLogger('agv.arduino')

VALID_DIRECTIONS = ('F', 'B', 'L', 'R', 'S')


@dataclass
class ArduinoReport:
    port: str
    opened: bool
    error: Optional[str] = None
    latency_ms: float = 0.0


@dataclass
class ArduinoState:
    connected: bool = False
    running: bool = False
    # Last time we received a non-empty line from the Arduino.
    last_ack_monotonic: float = 0.0
    # Last time a write() to the Arduino returned without raising.
    # Cheap Arduino sketches don't always reply to every command, so
    # a fresh write is itself a "link is alive" signal.
    last_write_success_monotonic: float = 0.0
    last_cmd: Optional[str] = None
    last_error: Optional[str] = None
    # True once the firmware has answered with an ``ACK2`` line, i.e. it
    # speaks the M<left>,<right> protocol. Without it the AGV refuses to
    # drive (old firmware would just stop on every ``M`` command).
    protocol_ok: bool = False
    applied_left: int = 0
    applied_right: int = 0
    # Plain-text log of the most recent replies (capped). Surfaced on
    # the dashboard so the user can see what the Arduino actually said.
    recent_replies: List[str] = field(default_factory=list)
    # Fixed placeholders so the AGV telemetry schema stays compatible
    # with the STM32 dashboard rendering. The Arduino doesn't expose
    # encoders or firmware version, so we report 0 / "".
    encoder_left: int = 0
    encoder_right: int = 0
    encoder_delta_left: int = 0
    encoder_delta_right: int = 0
    firmware_version: str = ''
    # Firmware v3 (encoders + closed-loop speed). ``speed_ok`` = the ACK
    # carries the extra fields; ``gains_ok`` = the PID gains were accepted
    # since the last firmware boot. Closed loop needs both.
    speed_ok: bool = False
    gains_ok: bool = False
    gains_ts: float = 0.0              # monotonic time the G line was last sent
    meas_ticks_left: float = 0.0       # measured wheel speed, ticks/s
    meas_ticks_right: float = 0.0
    tgt_ticks_left: float = 0.0        # speed targets the firmware holds
    tgt_ticks_right: float = 0.0
    pwm_target_left: int = 0           # PID output before the slew limiter
    pwm_target_right: int = 0
    fw_mode: str = 'P'                 # P = raw PWM, V = closed-loop speed
    fault_bits: int = 0                # 1 = left, 2 = right encoder not moving
    enc_zero_left: int = 0             # tick counts at the last "zero" (display only)
    enc_zero_right: int = 0


def gains_command() -> str:
    """G line with the PID gains converted to firmware units (per tick/s)."""
    kp = PID_KP / TICKS_PER_M
    ki = PID_KI / TICKS_PER_M
    kd = PID_KD / TICKS_PER_M
    kff = (255.0 - MIN_PWM) / (WHEEL_MAX_MPS * TICKS_PER_M)
    return 'G%d,%d,%d,%d,%d' % (round(kp * 1000), round(ki * 1000),
                                round(kd * 1000), round(kff * 1000), round(MIN_PWM))


class Arduino:
    """Thread-safe serial driver for the Arduino motor controller."""

    def __init__(self, port: Optional[str] = None,
                 baud: Optional[int] = None) -> None:
        self.port = port or ARDUINO_PORT
        self.baud = baud or ARDUINO_BAUD
        self._serial: Optional[serial.Serial] = None
        self._state = ArduinoState()
        self._lock = threading.Lock()
        self._io_lock = threading.Lock()   # serialises serial writes
        self._last_motor: tuple = (None, None)
        self._last_motor_monotonic: float = 0.0
        self._last_warn: float = 0.0
        self._reader: Optional[threading.Thread] = None
        # Last time we actually sent a byte. Used to throttle sends.
        self._last_send_monotonic: float = 0.0

    # ── context manager ──────────────────────────────────────────────
    def __enter__(self) -> 'Arduino':
        self.connect()
        return self

    def __exit__(self, *_args) -> None:
        self.close()

    # ── lifecycle ────────────────────────────────────────────────────
    def connect(self) -> ArduinoReport:
        t0 = time.monotonic()
        try:
            self._open_serial()
            now = time.monotonic()
            with self._lock:
                self._state.running = True
                self._state.connected = True
                self._state.last_ack_monotonic = now
                self._state.last_write_success_monotonic = now
            self._reader = threading.Thread(
                target=self._reader_loop, daemon=True, name='arduino-reader',
            )
            self._reader.start()
            log.info('Arduino connected on %s @ %d baud', self.port, self.baud)
            return ArduinoReport(
                self.port, True,
                latency_ms=round((time.monotonic() - t0) * 1000, 1),
            )
        except Exception as exc:
            log.error('Cannot connect Arduino on %s: %s', self.port, exc)
            with self._lock:
                self._state.connected = False
                self._state.running = False
                self._state.last_error = f'{type(exc).__name__}: {exc}'
            return ArduinoReport(
                self.port, False,
                error=f'{type(exc).__name__}: {exc}',
                latency_ms=round((time.monotonic() - t0) * 1000, 1),
            )

    def _open_serial(self) -> None:
        """Open (or re-open) the serial port and wait for the bootloader
        to hand off to the sketch. Safe to call multiple times — the
        caller is responsible for closing the previous handle first."""
        # Arduino auto-resets when the USB serial port opens (DTR
        # toggle), so the bootloader runs for a beat before the sketch
        # starts. Wait it out before letting the reader thread start
        # chewing bytes.
        self._serial = serial.Serial(self.port, self.baud, timeout=1)
        time.sleep(2)
        try:
            self._serial.reset_input_buffer()
            self._serial.reset_output_buffer()
        except Exception:
            pass

    def _reopen_serial(self) -> bool:
        """Best-effort reopen of the serial port after a USB hiccup.

        The Arduino sometimes drops the USB connection briefly (e.g.
        when another process opens the port, or when the cable is
        jostled). ``pyserial`` raises ``SerialException`` for a few
        read calls until the device reappears. We close the dead
        handle and try to re-open with a short backoff.
        """
        try:
            if self._serial is not None:
                try:
                    self._serial.close()
                except Exception:
                    pass
                self._serial = None
        except Exception:
            pass
        try:
            self._open_serial()
            log.info('[Arduino] port re-opened after disconnect')
            with self._lock:
                self._state.protocol_ok = False
                self._state.speed_ok = self._state.gains_ok = False
                self._state.gains_ts = 0.0
                self._state.connected = True
                self._state.last_ack_monotonic = time.monotonic()
                self._state.last_write_success_monotonic = time.monotonic()
            return True
        except Exception as exc:
            with self._lock:
                self._state.connected = False
                self._state.last_error = f'reopen: {exc}'
            return False

    def close(self) -> None:
        with self._lock:
            self._state.running = False
        # Best-effort: stop the motors before closing.
        try:
            if self._serial and self._serial.is_open:
                self._serial.write(b'S\n')
                self._serial.flush()
        except Exception:
            pass
        if self._reader:
            self._reader.join(timeout=2)
            self._reader = None
        if self._serial:
            try:
                self._serial.close()
            except Exception:
                pass
            self._serial = None
        with self._lock:
            self._state.connected = False

    def isOpen(self) -> bool:
        return self._serial is not None and self._serial.is_open

    # ── command output ───────────────────────────────────────────────
    def send(self, cmd: str, speed: Optional[str] = None,
             force: bool = False) -> bool:
        """Send a single-char direction command. ``speed`` is ignored
        (the Arduino firmware has no speed byte).

        ``force=True`` bypasses the throttle and same-direction drop —
        used by the periodic keepalive re-send so the link stays
        marked alive during a long stable heading.
        """
        if not self.isOpen():
            return False
        if cmd not in VALID_DIRECTIONS:
            log.warning('[Arduino] ignoring invalid command %r', cmd)
            return False
        now = time.monotonic()
        with self._lock:
            if not force:
                if cmd == self._state.last_cmd:
                    return True
                min_interval = 1.0 / max(ARDUINO_CMD_HZ, 1.0)
                if (now - self._last_send_monotonic) < min_interval:
                    return True
        try:
            payload = (cmd + '\n').encode('ascii')
            assert self._serial is not None
            with self._io_lock:
                self._serial.write(payload)
                self._serial.flush()
            with self._lock:
                self._state.last_cmd = cmd
                self._last_send_monotonic = now
                # A successful write proves the cable + USB stack are
                # good. Cheap Arduino sketches don't ACK every command
                # but they always accept the byte, so this is the
                # primary link-alive signal.
                self._state.last_write_success_monotonic = now
                self._state.connected = True
            return True
        except (serial.SerialException, OSError, IOError, TypeError, ValueError) as exc:
            log.warning('[Arduino] write failed: %s', exc)
            with self._lock:
                self._state.connected = False
                self._state.last_error = f'{type(exc).__name__}: {exc}'
            return False

    def send_motors(self, left: int, right: int, force: bool = False) -> bool:
        """Send signed wheel PWM (-255..255). Re-sent at least every 100 ms
        so the firmware's command timeout stays fed."""
        if not self.isOpen():
            return False
        left = max(-255, min(255, int(left)))
        right = max(-255, min(255, int(right)))
        now = time.monotonic()
        if (not force and (left, right) == self._last_motor
                and (now - self._last_motor_monotonic) < 0.1):
            return True
        try:
            with self._io_lock:
                assert self._serial is not None
                self._serial.write(f'M{left},{right}\n'.encode('ascii'))
                self._serial.flush()
            self._last_motor = (left, right)
            self._last_motor_monotonic = now
            with self._lock:
                self._state.last_write_success_monotonic = now
            return True
        except (serial.SerialException, OSError, IOError, AssertionError,
                TypeError, ValueError) as exc:
            if now - self._last_warn > 2.0:         # 50 Hz loop: do not spam
                self._last_warn = now
                log.warning('[Arduino] motor write failed: %s', exc)
            with self._lock:
                self._state.connected = False
                self._state.last_error = f'{type(exc).__name__}: {exc}'
            return False

    def zero_encoders(self) -> None:
        """Make the relative tick counters start from 0 here (the firmware keeps
        counting; only the dashboard's "since zero" numbers move)."""
        with self._lock:
            self._state.enc_zero_left = self._state.encoder_left
            self._state.enc_zero_right = self._state.encoder_right

    def closed_loop_ready(self) -> bool:
        """Firmware v3 answered and accepted the PID gains."""
        with self._lock:
            return (self._state.connected and self._state.protocol_ok
                    and self._state.speed_ok and self._state.gains_ok)

    def send_speed(self, left_ticks: int, right_ticks: int,
                   force: bool = False) -> bool:
        """Send wheel speed targets in encoder ticks/s (closed loop)."""
        if not self.isOpen():
            return False
        left, right = int(left_ticks), int(right_ticks)
        now = time.monotonic()
        key = ('V', left, right)
        if (not force and key == self._last_motor
                and (now - self._last_motor_monotonic) < 0.1):
            return True
        try:
            with self._io_lock:
                assert self._serial is not None
                self._serial.write(f'V{left},{right}\n'.encode('ascii'))
                self._serial.flush()
            self._last_motor = key
            self._last_motor_monotonic = now
            with self._lock:
                self._state.last_write_success_monotonic = now
            return True
        except (serial.SerialException, OSError, IOError, AssertionError,
                TypeError, ValueError) as exc:
            if now - self._last_warn > 2.0:
                self._last_warn = now
                log.warning('[Arduino] speed write failed: %s', exc)
            with self._lock:
                self._state.connected = False
                self._state.last_error = f'{type(exc).__name__}: {exc}'
            return False

    def _send_gains(self) -> None:
        try:
            with self._io_lock:
                if self._serial is not None:
                    self._serial.write((gains_command() + '\n').encode('ascii'))
                    self._serial.flush()
        except (serial.SerialException, OSError, IOError, TypeError, ValueError):
            pass

    # ── state access ─────────────────────────────────────────────────
    def snapshot(self) -> ArduinoState:
        with self._lock:
            # Return a copy so the caller can read it without lock.
            return dataclasses.replace(
                self._state, recent_replies=list(self._state.recent_replies))

    def ack_age_ms(self) -> float:
        with self._lock:
            return (time.monotonic() - self._state.last_ack_monotonic) * 1000.0

    def write_age_ms(self) -> float:
        with self._lock:
            return (time.monotonic() - self._state.last_write_success_monotonic) * 1000.0

    def link_alive(self) -> bool:
        """Alive only if the firmware itself answered recently.

        A successful ``write()`` only proves the OS accepted the bytes; a
        frozen or unplugged Arduino would still pass that test. The v2
        firmware sends an ``ACK2`` line at least every 200 ms, so a missing
        ACK is a real signal. (The age math is inlined: ``_lock`` is not
        reentrant, so do not call ``ack_age_ms()`` from here.)
        """
        with self._lock:
            if not (self._state.connected and self._state.protocol_ok):
                return False
            age = (time.monotonic() - self._state.last_ack_monotonic) * 1000.0
            return age <= ARDUINO_ACK_TIMEOUT_MS

    # ── compatibility shims (mirror STM32 attributes) ────────────────
    @property
    def connected(self) -> bool:
        with self._lock:
            return self._state.connected

    @property
    def encoder_left(self) -> int:
        with self._lock:
            return self._state.encoder_left

    @property
    def encoder_right(self) -> int:
        with self._lock:
            return self._state.encoder_right

    @property
    def encoder_delta_left(self) -> int:
        with self._lock:
            return self._state.encoder_delta_left

    @property
    def encoder_delta_right(self) -> int:
        with self._lock:
            return self._state.encoder_delta_right

    @property
    def firmware_version(self) -> str:
        with self._lock:
            return self._state.firmware_version

    @property
    def last_cmd(self) -> Optional[str]:
        with self._lock:
            return self._state.last_cmd

    @property
    def recent_replies(self) -> List[str]:
        with self._lock:
            return list(self._state.recent_replies)

    # ── internal helpers ─────────────────────────────────────────────
    def _reader_loop(self) -> None:
        """Drain the Arduino's replies in the background.

        Self-healing: when the USB device hiccups (``SerialException``,
        ``OSError``) — which happens whenever a DTR toggle resets the
        Arduino, the cable is jostled, or another process briefly
        opens the port — we DO NOT exit. The thread keeps running,
        closes the dead handle, and tries to reopen with a short
        backoff. As soon as the device reappears we resume reading
        and the link is alive again.

        Without this, a single USB blip would silently kill the link:
        no more ACKs, the watchdog fires forever, and the rover
        refuses to move even though the camera is happily producing
        F / L / R commands.
        """
        backoff = 0.2
        while True:
            with self._lock:
                if not self._state.running:
                    return
            ser = self._serial
            if ser is None or not ser.is_open:
                # The handle is gone (USB dropped and the first reopen
                # failed). Keep retrying with backoff until the device is
                # back; nothing else will reopen it.
                time.sleep(backoff)
                backoff = min(backoff * 2, 3.0)
                if self._reopen_serial():
                    backoff = 0.2
                continue
            try:
                raw = ser.readline()
            except (serial.SerialException, OSError, IOError, TypeError, ValueError) as exc:
                log.warning('[Arduino] read failed: %s — reopening', exc)
                with self._lock:
                    self._state.connected = False
                    self._state.last_error = f'{type(exc).__name__}: {exc}'
                if self._reopen_serial():
                    backoff = 0.2
                else:
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 5.0)
                continue
            if not raw:
                continue
            try:
                line = raw.decode('utf-8', errors='replace').strip()
            except Exception:
                line = repr(raw)
            if not line:
                continue
            send_gains = False
            with self._lock:
                st = self._state
                st.last_ack_monotonic = time.monotonic()
                st.connected = True
                if line.startswith('ACK2'):
                    parts = line.split()
                    st.protocol_ok = True
                    st.firmware_version = 'v2'
                    try:
                        st.pwm_target_left = int(parts[1])
                        st.pwm_target_right = int(parts[2])
                        st.applied_left = int(parts[3])
                        st.applied_right = int(parts[4])
                    except (IndexError, ValueError):
                        pass
                    if len(parts) >= 13:                  # firmware v3
                        try:
                            st.meas_ticks_left = float(parts[5])
                            st.meas_ticks_right = float(parts[6])
                            st.encoder_left = int(parts[7])
                            st.encoder_right = int(parts[8])
                            st.fw_mode = parts[9]
                            st.tgt_ticks_left = float(parts[10])
                            st.tgt_ticks_right = float(parts[11])
                            st.fault_bits = int(parts[12])
                            st.speed_ok = True
                            st.firmware_version = 'v3'
                            now_m = time.monotonic()
                            if not st.gains_ok and now_m - st.gains_ts > 1.0:
                                st.gains_ts = now_m      # (re)send until accepted
                                send_gains = True
                        except ValueError:
                            pass
                elif line.startswith('GAINS ok'):
                    st.gains_ok = True
                elif line.startswith('READY'):
                    # (Re)boot: the firmware lost our gains and any V target.
                    st.protocol_ok = line.startswith('READY2')
                    st.speed_ok = st.gains_ok = False
                    st.gains_ts = 0.0
                st.recent_replies.append(line)
                if len(st.recent_replies) > 20:
                    st.recent_replies = st.recent_replies[-20:]
            if send_gains:
                self._send_gains()
            backoff = 0.2  # healthy read — reset the backoff
