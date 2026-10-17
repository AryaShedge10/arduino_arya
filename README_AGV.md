# AGV: camera line follower + Arduino

Camera (IMX219, ~1 m high, ~45 deg down) -> `vision.py` -> `decision.py` -> `motion.py` -> Arduino (`M<left>,<right>`).

## Run
1. Flash the firmware once (stops the motors on reset):
   `arduino-cli compile --fqbn arduino:avr:uno MotorControl && arduino-cli upload -p /dev/ttyACM0 --fqbn arduino:avr:uno MotorControl`
2. `./agv_run.sh` (restart `web.py` too if it was already running), open `http://<jetson>:5000`.
3. The AGV always starts **OFF**. Press **START** on the dashboard to enable the motors; **STOP** turns them off (bounded ramp, ~0.25 s).
   START asks for the dashboard password; STOP never does. The password is `AGV_DASHBOARD_PASSWORD`, else the first line of `.dashboard_password` (generated on first run; edit the file to change it, then restart `web.py`). 5 wrong guesses lock START for 30 s.
4. **Max speed** slider (10-100 %): master limit that scales every other speed (line following, curves, caution, obstacle slow, turning, search). Applies immediately, ramped by the jerk limiter; kept across restarts.

## Safety layers
- Dashboard OFF / no camera frame / stalled vision / Arduino ACK missing -> emergency (bounded) ramp to 0.
- Firmware: no valid command for 1 s -> motors ramp to 0. Firmware also slew-limits PWM itself.
- Obstacle STOP (camera) -> emergency stop. Obstacle SLOW -> half speed.
- Without firmware v2 (`ACK2` replies) the AGV refuses to drive.

## Measure first, then tune (all via `AGV_*` env vars, see `config.py`)
| Setting | Meaning |
|---|---|
| `AGV_CAM_HEIGHT_M`, `AGV_CAM_TILT_DEG` | camera pose (drives the bird's-eye warp and every distance) |
| `AGV_ROVER_WIDTH_M` | obstacle corridor width |
| `AGV_MIN_PWM` | PWM where the wheels just start to turn (stiction) |
| `AGV_V_MAX`, `AGV_V_MIN` | speed range (start low, e.g. 100/60) |
| `AGV_STEER_GAIN`, `AGV_OMEGA_MAX` | steering strength |
| `AGV_MAX_ACCEL`, `AGV_MAX_JERK` | smoothness (PWM/s^2, PWM/s^3) |
| `AGV_OBST_STOP_M`, `AGV_OBST_SLOW_M`, `AGV_OBST_DELTA_E` | obstacle distances and sensitivity |
| `AGV_RECOVERY=0` | disable the slow search when the line is lost |

Check the dashboard BEV inset (bottom right of the video): a straight line on the floor must look straight and vertical, and obstacle distances must match a tape measure. If not, fix height/tilt first.

## Limits
Obstacle detection compares the floor in front of the rover with its own colour: it can miss floor-coloured or very low objects and needs steady lighting. Start with the corridor clear.

## Yellow-line detection (how it works)
- Colour test in CIELAB, not fixed HSV: enough chroma, hue near yellow (`AGV_LINE_HUE_DEG`, `_TOL_DEG`), and at least `AGV_LINE_HUE_MARGIN_DEG` closer to yellow than the median floor pixel. Measured on the real tape (pale masking tape on a wood desk): tape Lab hue ~102 / HSV S ~85, wood hue ~80 / H 18. Cream walls and pink lights give no mask.
- Colour tracking: once the line is found, the tape's colour *relative to the floor* is remembered; if a colour cast later breaks the hue test, the line is followed by that colour (up to `AGV_TAPE_MODEL_TTL_S`). A cast present from the very first frame is not recoverable: fix the lighting or white balance (`AGV_CAMERA_WB_MODE`).
- Blob choice: long (`AGV_LINE_MIN_LENGTH_M`) and elongated (`AGV_LINE_MIN_ELONGATION`) shapes only, preferring the one near where the line just was. Round yellow objects are ignored.
- A sudden lighting step can show as an obstacle STOP; pressing OFF then ON relearns the floor.

## Steering and PWM (what the controller does)
- **Pure pursuit:** the turn is the curvature that reaches the point `AGV_LOOKAHEAD_M` ahead, and the differential PWM is `omega = kappa * v * wheel_base / 2`. The turn therefore scales with speed, which keeps the loop stable from creeping to full speed (the old fixed-gain law weaved and lost the line at high speed in simulation). Set `AGV_WHEEL_BASE_M` to the real centre-to-centre wheel distance.
- **Near-offset term** (`AGV_K_OFFSET_CURV`) pulls it onto the line; **trim** (`AGV_KI_OFFSET`) slowly cancels a weaker motor / steady drift. Both back off automatically above the tuned speed.
- **Turn priority at full PWM:** if speed + turn would exceed 255, forward speed is reduced instead of clipping one wheel.
- `AGV_STEER_MODE=p` restores the old fixed-gain law.
- Simulation results (25 cm start offset, 100 ms camera delay, +-8 % motor mismatch): late tracking error 0.4-0.7 cm on straights, about 4 cm on 25 cm-amplitude S-curves, no weaving. Real numbers depend on your chassis: measure the wheel base and `AGV_MIN_PWM` and re-tune on the floor.

## Not turning / drifting off the line (what is checked)
- **Minimum turn authority:** a skid-steer chassis does not turn until the wheels differ by a minimum amount; at a low max-speed setting the pursuit turn alone can be about 1 PWM level, i.e. nothing. The controller now grows a minimum wheel difference (`AGV_TURN_FLOOR`, up to 40 levels) only when needed.
- **Tracking monitor** (dashboard "Line tracking"): while the wheels are driving it watches whether the error to the line shrinks. `ON THE LINE`, `CORRECTING`, `NOT TURNING - BOOSTING` (error not shrinking: turn boosted up to x3 and speed reduced), `DRIFTING OFF - BOOSTING` (error growing), `OFF THE LINE` (far off for 2.5 s). It ignores curved lines and overshoot (crossing the line means the turn is strong enough) and only judges while driving.
- **Pivot to align:** if the heading is more than 45 deg off, or the lookahead point is more than 0.6 m off, it pivots in place first, then arcs back onto the line.
- **Obstacle stop switch:** the dashboard checkbox turns the camera obstacle stop off for bench tests (password needed to turn it OFF). With it enforced, people/feet/boxes in the 0.6 m zone stop the rover by design.
- **Snapshots:** when an obstacle is flagged, `logs/snapshots/` gets the raw frame and the detector views (max 12 per run) for diagnosis.
- Simulation (skid-steer chassis that needs 0.35 rad/s before it turns): the old logic never turned (error stayed 25 cm); the new logic gets back onto the line (about 2-7 cm late error). On an ideal chassis it is unchanged or better.

## Heading from the whole shape, and wide pieces
- The line's heading and position now come from the **principal axis of the whole yellow shape** whenever the line is straight or only gently curved (accurate to a fraction of a degree on diagonal pieces; per-strip centroids under-read them by 5-8 degrees). Strongly curved lines (|curvature| > 0.35 1/m) still use the strip curve fit. With fewer than 3 valid strips the axis is used too, so a short wide piece no longer reads as "straight ahead".
- Width is judged **perpendicular to the line** (area / length), not per image row, and the limit is `AGV_LINE_MAX_WIDTH_M` = 0.45 m (was 0.30), so 30 cm paper pieces and 5-15 cm tape both qualify. A shape wider than that, or not elongated (`AGV_LINE_MIN_ELONGATION`), is rejected.
- **Pivot to align** turns toward the line's *direction* (sign of its heading angle), not toward the sideways offset: a pivot fixes the heading, then the arc steering closes the offset.
