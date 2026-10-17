
import serial
import time

PORT = "/dev/ttyACM0"
BAUD = 115200

arduino = serial.Serial(PORT, BAUD, timeout=1)
time.sleep(2)  # Arduino may reset when USB opens
arduino.reset_input_buffer()

print("Connected to Arduino!")
print("F = Forward | B = Backward")
print("L = Left | R = Right | S = Stop")
print("Q = Quit")

try:
    while True:
        cmd = input("Command: ").strip().upper()

        if cmd == "Q":
            arduino.write(b"S\n")
            print("Stop command sent.")
            break

        if cmd not in ("F", "B", "L", "R", "S"):
            print("Invalid command. Use F/B/L/R/S/Q.")
            continue

        arduino.write((cmd + "\n").encode())
        arduino.flush()

        reply = arduino.readline().decode(
            "utf-8", errors="replace"
        ).strip()

        if reply:
            print("Arduino:", reply)
        else:
            print("No acknowledgement received.")

finally:
    try:
        arduino.write(b"S\n")
        arduino.flush()
        time.sleep(0.1)
    finally:
        arduino.close()
        print("Serial connection closed.")
