"""
Real-time object detection on the USB webcam (Logitech C270 HD).

Uses Ultralytics YOLOv8n (COCO 80-class). The .pt weights are downloaded
automatically by ultralytics on first import (cached in
/home/deep/arduino/face_recognition/models/yolov8n.pt).

Install:
    pip install ultralytics    # already installed on this Jetson

Usage:
    python detect_objects.py --list                          # find camera index
    python detect_objects.py run --device /dev/video0 --width 640 --height 480
    python detect_objects.py run --device /dev/video0 --device-id 0   # GPU
    python detect_objects.py smoke                           # synthetic frame
    python detect_objects.py warmup                          # build tensorrt engine

Controls: q/ESC = quit, s = save snapshot.
"""

import argparse
import os
import platform
import sys
import time
import urllib.request

import cv2
import numpy as np

MODEL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")
YOLO_FILENAME = "yolov8n.pt"
# Official ultralytics release URL — ultralytics itself also fetches this on first import.
YOLO_URL = (
    "https://github.com/ultralytics/assets/releases/download/v8.3.0/" + YOLO_FILENAME
)

# A small, stable palette — cycles per class id. BGR tuples for cv2.
_PALETTE = np.array(
    [
        (56, 56, 255), (151, 157, 255), (31, 112, 255), (29, 178, 255),
        (49, 210, 207), (10, 249, 72),  (23, 204, 146), (134, 219, 61),
        (52, 147, 26),  (187, 212, 0),  (168, 153, 44), (255, 194, 0),
        (255, 175, 0),  (229, 109, 0),  (255, 0, 0),     (194, 0, 0),
        (133, 0, 0),    (166, 56, 0),   (255, 0, 170),  (255, 0, 200),
        (199, 0, 199),  (170, 0, 102),  (108, 84, 198),  (87, 67, 250),
        (0, 0, 255),    (0, 100, 255),  (0, 150, 255),  (0, 200, 255),
    ],
    dtype=np.int32,
)


# ---------------------------------------------------------------- helpers ---
def ensure_yolo_weights():
    """Make sure the YOLOv8n weights are present locally.

    Ultralytics also auto-downloads on first YOLO() call, but that's a black
    box. We shadow it here with a curl-friendly fetcher so a partial download
    fails noisily instead of getting cached as a silent stub.
    """
    os.makedirs(MODEL_DIR, exist_ok=True)
    path = os.path.join(MODEL_DIR, YOLO_FILENAME)
    if os.path.exists(path) and os.path.getsize(path) > 5_000_000:
        return path  # ~6.5 MB is the real YOLOv8n size; anything smaller is bogus.
    print(f"Fetching {YOLO_FILENAME} ...")
    try:
        import subprocess
        subprocess.run(
            ["curl", "-fL", "--retry", "2", "-o", path, YOLO_URL],
            check=True,
        )
    except Exception as e:
        # Fall back to urllib; ultralytics will retry on its own if this also fails.
        urllib.request.urlretrieve(YOLO_URL, path)
        if os.path.getsize(path) < 5_000_000:
            raise RuntimeError(
                f"Downloaded YOLO weights look truncated ({os.path.getsize(path)} B)"
            )
    return path


def open_camera(device, width, height):
    """Open a video source.

    ``device`` may be either a cv2 integer index (0, 1, ...) or a path string
    like '/dev/video0'. Using a path lets two scripts grab different nodes
    (e.g. /dev/video0 for the C270, /dev/video2 for an IMX219) without
    fighting over the indexing the kernel hands each one.
    """
    if isinstance(device, str) and device.startswith("/dev/video"):
        cap = cv2.VideoCapture(device, cv2.CAP_V4L2)
    else:
        system = platform.system()
        backend = (cv2.CAP_DSHOW if system == "Windows"
                   else cv2.CAP_V4L2 if system == "Linux" else cv2.CAP_ANY)
        cap = cv2.VideoCapture(int(device), backend)

    if not cap.isOpened():
        return None
    cap.set(cv2.CAP_PROP_FOURCC,    cv2.VideoWriter_fourcc(*'MJPG'))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH,  width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    cap.set(cv2.CAP_PROP_FPS, 30)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    return cap


def list_cameras(max_index=6):
    print("Scanning for cameras...")
    found = False
    for i in range(max_index):
        cap = open_camera(i, 640, 480)
        if cap is not None:
            ok, frame = cap.read()
            if ok:
                h, w = frame.shape[:2]
                print(f"  Camera index {i}: working ({w}x{h})")
                found = True
            cap.release()
    if not found:
        print("  No cameras found. Check the USB connection / permissions.")


class ObjectDetector:
    """Thin wrapper around ultralytics.YOLO with a stable interface."""

    def __init__(self, weights_path: str, device: str = 'cpu', conf: float = 0.35):
        from ultralytics import YOLO  # noqa: WPS433 (deliberately late import)
        self.model = YOLO(weights_path)
        self.device = device          # 'cpu' | '0' | 'cuda:0'
        self.conf = conf
        self.class_names = self.model.names

    def infer(self, frame: np.ndarray):
        """Run on a single BGR frame. Return a list of dicts:
            { 'class_id': int, 'name': str, 'conf': float, 'xyxy': (x1,y1,x2,y2) }
        """
        results = self.model.predict(
            source=frame,
            imgsz=frame.shape[0],   # run at native resolution; the C270 is 480p
            device=self.device,
            conf=self.conf,
            verbose=False,
        )
        detections = []
        if not results:
            return detections
        boxes = results[0].boxes
        if boxes is None or len(boxes) == 0:
            return detections
        xyxy = boxes.xyxy.cpu().numpy().astype(int)
        conf = boxes.conf.cpu().numpy()
        cls  = boxes.cls.cpu().numpy().astype(int)
        for (x1, y1, x2, y2), c, k in zip(xyxy, conf, cls):
            detections.append({
                'class_id': int(k),
                'name':     self.class_names[int(k)],
                'conf':     float(c),
                'xyxy':     (int(x1), int(y1), int(x2), int(y2)),
            })
        return detections


def draw_detections(frame, detections, fps):
    """Draw detections + FPS onto a copy of the frame in-place."""
    for d in detections:
        x1, y1, x2, y2 = d['xyxy']
        color = tuple(int(v) for v in _PALETTE[d['class_id'] % len(_PALETTE)])
        cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
        label = f"{d['name']} {d['conf']:.2f}"
        # Background bar for the label so it stays legible.
        (tw, th), baseline = cv2.getTextSize(
            label, cv2.FONT_HERSHEY_SIMPLEX, 0.55, 1)
        ty1 = max(0, y1 - th - baseline - 4)
        ty2 = y1
        cv2.rectangle(frame, (x1, ty1), (x1 + tw + 4, ty2), color, -1)
        cv2.putText(frame, label, (x1 + 2, y1 - baseline - 2),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1,
                    cv2.LINE_AA)

    h, w = frame.shape[:2]
    cv2.rectangle(frame, (0, 0), (260, 50), (0, 0, 0), -1)
    cv2.putText(frame, f"YOLOv8n | {len(detections)} obj | FPS {fps:4.1f}",
                (10, 32), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 230, 255), 2)


# --------------------------------------------------------------- commands ---
def cmd_run(args):
    cap = open_camera(args.device, args.width, args.height)
    if cap is None:
        sys.exit(
            f"Could not open {args.device!r}. "
            "Re-plug the USB cable, then run with --list to confirm."
        )

    weights = ensure_yolo_weights()
    detector = ObjectDetector(weights, device=args.device_id, conf=args.conf)
    print(
        f"Running YOLOv8n on {args.device} ({args.width}x{args.height}), "
        f"device={args.device_id}, conf>={args.conf}. "
        f"q/ESC = quit, s = snapshot."
    )

    prev, fps = time.time(), 0.0
    while True:
        ok, frame = cap.read()
        if not ok:
            print("Failed to read frame.")
            break

        detections = detector.infer(frame)
        now = time.time()
        fps = 0.9 * fps + 0.1 / max(now - prev, 1e-6)
        prev = now
        draw_detections(frame, detections, fps)
        cv2.imshow("Object Detection - C270 HD", frame)

        key = cv2.waitKey(1) & 0xFF
        if key in (ord("q"), 27):
            break
        if key == ord("s"):
            fn = f"snapshot_{int(time.time())}.jpg"
            cv2.imwrite(fn, frame)
            print(f"Saved {fn}")

    cap.release()
    cv2.destroyAllWindows()


def cmd_smoke(args):
    """Run a single inference on a synthetic frame to validate the module."""
    weights = ensure_yolo_weights()
    detector = ObjectDetector(weights, device=args.device_id, conf=args.conf)

    img = (np.random.rand(480, 640, 3) * 255).astype(np.uint8)
    # Throw in some rectangles so the network has something to bite on.
    for _ in range(5):
        x, y = np.random.randint(20, 600), np.random.randint(20, 460)
        cv2.rectangle(img, (x, y), (x + 80, y + 80), (0, 255, 0), 2)

    t0 = time.time()
    detections = detector.infer(img)
    dt_ms = (time.time() - t0) * 1000
    print(f"smoke OK: {len(detections)} detections in {dt_ms:.0f}ms "
          f"(device={args.device_id})")


def cmd_warmup(_args):
    """Run a small inference loop to JIT compile caches / build TensorRT.

    On a Jetson with CUDA+Torch+TensorRT available, ultralytics's first run
    is dominated by TRT engine construction. This shortens that surprise.
    """
    weights = ensure_yolo_weights()
    detector = ObjectDetector(weights, device=_args.device_id, conf=_args.conf)
    img = (np.random.rand(480, 640, 3) * 255).astype(np.uint8)
    for i in range(3):
        t0 = time.time()
        detector.infer(img)
        print(f"warmup {i + 1}/3: {(time.time() - t0) * 1000:.0f} ms")
    print("warmup complete")


# ------------------------------------------------------------------- main ---
def main():
    p = argparse.ArgumentParser(description="USB-C270 object detection")
    p.add_argument("--list", action="store_true", help="list cameras and exit")
    sub = p.add_subparsers(dest="cmd")

    def common(sp):
        sp.add_argument("--width",      type=int,   default=640)
        sp.add_argument("--height",     type=int,   default=480)
        sp.add_argument("--device-id",  default="cpu",
                        help="'cpu' or GPU index like '0'")
        sp.add_argument("--conf",       type=float, default=0.35,
                        help="confidence threshold (0..1)")

    r = sub.add_parser("run", help="detect live")
    r.add_argument("--device", default="/dev/video0",
                   help="cv2 index (0,1,...) or /dev/videoX path (default /dev/video0)")
    common(r)

    s = sub.add_parser("smoke", help="infer on a synthetic frame")
    common(s)

    w = sub.add_parser("warmup", help="pre-build inference caches")
    common(w)

    args = p.parse_args()
    if args.list:
        return list_cameras()
    if args.cmd is None:
        p.print_help()
        return
    {"run": cmd_run, "smoke": cmd_smoke, "warmup": cmd_warmup}[args.cmd](args)


if __name__ == "__main__":
    main()
