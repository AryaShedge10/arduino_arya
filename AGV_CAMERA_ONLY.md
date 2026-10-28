# Camera-only use

Two ways to run without the Arduino chassis or LiDAR:

- `python3 agv_cam_only.py`: camera + yellow-line detector + a local OpenCV
  window (`AGV_SHOW_WINDOW=0` for headless, `AGV_CAMERA_BACKEND=fake` for a
  synthetic source). No dashboard, no motors.
- `./agv_run.sh` (or the systemd services, see "Standalone mode" in
  `README_AGV.md`): the full pipeline in `agv_drive_arduino.py`. LiDAR is
  optional; the camera, detector and dashboard keep running without it. A
  missing Arduino link or camera frame ramps the motors to 0 for safety but
  does not stop frame capture or the dashboard.

(This file used to describe `agv_main.py` and an STM32 controller; neither
exists in this repo any more.)
