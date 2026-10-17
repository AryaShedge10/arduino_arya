agv_main.py already handles missing hardware gracefully:

- No LiDAR (/dev/ttyUSB0 missing) → decision.py:265-275 sets status to CAMERA ONLY | LiDAR unavailable and the detector keeps running.
- No STM32 (/dev/ttyTHS1 missing or no ACK) → decision.py:278-283 forces final_command = 'S' purely as a safety stop, but the camera, detector, and HUD are completely independent of that flag.
- Camera + detector + HUD all keep working. The "STM32 WATCHDOG" text in the top status bar is just a status string — it doesn't stop frames from being captured, detected, or published.
