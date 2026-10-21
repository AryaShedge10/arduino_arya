"""
Real-time face RECOGNITION for an external USB webcam (e.g. Logitech C270 HD).

Uses only OpenCV (YuNet for detection + SFace for recognition). The two small
model files (~40 MB total) are downloaded automatically on first run.

Install:
    pip install opencv-python numpy

Usage:
    python face_recognize.py --list                     # find your camera index
    python face_recognize.py enroll --name Alice --camera 1
    python face_recognize.py enroll --name Bob   --camera 1
    python face_recognize.py run --camera 1             # recognize people live
    python face_recognize.py people                     # show enrolled people
    python face_recognize.py remove --name Bob          # delete someone

Enroll controls:  SPACE = capture a sample (turn your head slightly between
                  shots), q/ESC = finish.
Run controls:     q/ESC = quit, s = save snapshot.
"""

import argparse
import os
import pickle
import platform
import sys
import time
import urllib.request

import cv2
import numpy as np

MODEL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "faces_db.pkl")

BASE = "https://github.com/opencv/opencv_zoo/raw/main/models"
MODELS = {
    "detector": (
        "face_detection_yunet_2023mar.onnx",
        f"{BASE}/face_detection_yunet/face_detection_yunet_2023mar.onnx",
    ),
    "recognizer": (
        "face_recognition_sface_2021dec.onnx",
        f"{BASE}/face_recognition_sface/face_recognition_sface_2021dec.onnx",
    ),
}

# SFace's recommended cosine-similarity threshold. Higher = stricter
# (fewer false matches, more "Unknown"). Tune with --threshold.
DEFAULT_THRESHOLD = 0.363


# ---------------------------------------------------------------- helpers ---
def ensure_model(key):
    filename, url = MODELS[key]
    os.makedirs(MODEL_DIR, exist_ok=True)
    path = os.path.join(MODEL_DIR, filename)
    if not os.path.exists(path):
        print(f"Downloading {filename} ...")
        try:
            urllib.request.urlretrieve(url, path)
        except Exception as e:
            if os.path.exists(path):
                os.remove(path)
            sys.exit(f"Download failed: {e}\nDownload it manually from:\n  {url}\n"
                     f"and place it in: {MODEL_DIR}")
    return path


def open_camera(index, width, height):
    system = platform.system()
    backend = (cv2.CAP_DSHOW if system == "Windows"
               else cv2.CAP_V4L2 if system == "Linux" else cv2.CAP_ANY)
    cap = cv2.VideoCapture(index, backend)
    if not cap.isOpened():
        return None
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
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


def load_db():
    if os.path.exists(DB_PATH):
        with open(DB_PATH, "rb") as f:
            return pickle.load(f)
    return {}


def save_db(db):
    with open(DB_PATH, "wb") as f:
        pickle.dump(db, f)


class FaceEngine:
    def __init__(self, frame_size, threshold):
        self.detector = cv2.FaceDetectorYN.create(
            ensure_model("detector"), "", frame_size,
            score_threshold=0.7, nms_threshold=0.3, top_k=50)
        self.recognizer = cv2.FaceRecognizerSF.create(ensure_model("recognizer"), "")
        self.threshold = threshold

    def detect(self, frame):
        h, w = frame.shape[:2]
        self.detector.setInputSize((w, h))
        _, faces = self.detector.detect(frame)
        return [] if faces is None else faces

    def embed(self, frame, face):
        aligned = self.recognizer.alignCrop(frame, face)
        return self.recognizer.feature(aligned)

    def identify(self, feature, db):
        """Return (name, score). name is None if nobody matches."""
        best_name, best_score = None, -1.0
        for name, samples in db.items():
            for ref in samples:
                score = self.recognizer.match(
                    feature, ref, cv2.FaceRecognizerSF_FR_COSINE)
                if score > best_score:
                    best_name, best_score = name, score
        if best_score >= self.threshold:
            return best_name, best_score
        return None, best_score


# --------------------------------------------------------------- commands ---
def cmd_enroll(args):
    cap = open_camera(args.camera, args.width, args.height)
    if cap is None:
        sys.exit(f"Could not open camera {args.camera}. Try --list.")

    engine = FaceEngine((args.width, args.height), args.threshold)
    db = load_db()
    samples = []
    print(f"Enrolling '{args.name}'. Press SPACE to capture "
          f"(aim for {args.samples}), q/ESC to finish.")

    while len(samples) < args.samples:
        ok, frame = cap.read()
        if not ok:
            break
        faces = engine.detect(frame)
        display = frame.copy()
        for f in faces:
            x, y, w, h = f[:4].astype(int)
            cv2.rectangle(display, (x, y), (x + w, y + h), (0, 255, 0), 2)

        status = ("Ready - press SPACE" if len(faces) == 1
                  else "Need exactly ONE face in view")
        cv2.putText(display, f"{args.name}: {len(samples)}/{args.samples}  |  {status}",
                    (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 200, 255), 2)
        cv2.imshow("Enroll", display)

        key = cv2.waitKey(1) & 0xFF
        if key in (ord("q"), 27):
            break
        if key == 32:  # SPACE
            if len(faces) == 1:
                samples.append(engine.embed(frame, faces[0]))
                print(f"  captured {len(samples)}/{args.samples}")
            else:
                print("  skipped: need exactly one face in the frame")

    cap.release()
    cv2.destroyAllWindows()

    if samples:
        db.setdefault(args.name, []).extend(samples)
        save_db(db)
        print(f"Saved {len(samples)} samples for '{args.name}' "
              f"(total {len(db[args.name])}).")
    else:
        print("No samples captured; nothing saved.")


def cmd_run(args):
    db = load_db()
    if not db:
        print("Warning: no one is enrolled yet - everyone will show as Unknown.\n"
              "Enroll with: python face_recognize.py enroll --name YourName")

    cap = open_camera(args.camera, args.width, args.height)
    if cap is None:
        sys.exit(f"Could not open camera {args.camera}. Try --list.")

    engine = FaceEngine((args.width, args.height), args.threshold)
    print("Running. Press q/ESC to quit, s to save a snapshot.")

    prev, fps = time.time(), 0.0
    while True:
        ok, frame = cap.read()
        if not ok:
            print("Failed to read frame.")
            break

        for f in engine.detect(frame):
            x, y, w, h = f[:4].astype(int)
            name, score = engine.identify(engine.embed(frame, f), db) if db else (None, 0)
            known = name is not None
            color = (0, 255, 0) if known else (0, 0, 255)
            label = f"{name} ({score:.2f})" if known else "Unknown"
            cv2.rectangle(frame, (x, y), (x + w, y + h), color, 2)
            cv2.putText(frame, label, (x, max(y - 8, 15)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2)

        now = time.time()
        fps = 0.9 * fps + 0.1 / max(now - prev, 1e-6)
        prev = now
        cv2.putText(frame, f"FPS: {fps:.1f}", (10, 30),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 200, 255), 2)
        cv2.imshow("Face Recognition - C270 HD", frame)

        key = cv2.waitKey(1) & 0xFF
        if key in (ord("q"), 27):
            break
        if key == ord("s"):
            fn = f"snapshot_{int(time.time())}.jpg"
            cv2.imwrite(fn, frame)
            print(f"Saved {fn}")

    cap.release()
    cv2.destroyAllWindows()


def cmd_people(_args):
    db = load_db()
    if not db:
        print("No one enrolled yet.")
    for name, s in db.items():
        print(f"  {name}: {len(s)} samples")


def cmd_remove(args):
    db = load_db()
    if args.name in db:
        del db[args.name]
        save_db(db)
        print(f"Removed '{args.name}'.")
    else:
        print(f"'{args.name}' not found.")


# ------------------------------------------------------------------- main ---
def main():
    p = argparse.ArgumentParser(description="Webcam face recognition")
    p.add_argument("--list", action="store_true", help="list cameras and exit")
    sub = p.add_subparsers(dest="cmd")

    def common(sp):
        sp.add_argument("--camera", type=int, default=0)
        sp.add_argument("--width", type=int, default=1280)
        sp.add_argument("--height", type=int, default=720)
        sp.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                        help="match strictness (default 0.363; higher = stricter)")

    e = sub.add_parser("enroll", help="add a person")
    e.add_argument("--name", required=True)
    e.add_argument("--samples", type=int, default=8)
    common(e)

    r = sub.add_parser("run", help="recognize live")
    common(r)

    sub.add_parser("people", help="list enrolled people")
    rm = sub.add_parser("remove", help="delete a person")
    rm.add_argument("--name", required=True)

    args = p.parse_args()
    if args.list:
        return list_cameras()
    if args.cmd is None:
        p.print_help()
        return
    {"enroll": cmd_enroll, "run": cmd_run,
     "people": cmd_people, "remove": cmd_remove}[args.cmd](args)


if __name__ == "__main__":
    main()
