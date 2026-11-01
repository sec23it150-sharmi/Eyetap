"""
gaze_cursor.py - offline iris-tracking gaze cursor using MediaPipe Face Mesh.

Compatible with mediapipe >= 1.0 (uses the new Tasks API).
Automatically downloads the face_landmarker.task model (~30 MB) on first run.

Usage (standalone test):
    python gaze_cursor.py [--cam 0] [--calibrate] [--smooth 0.12]

API (imported by eyetap_main.py / gaze_executor.py):
    gc = GazeCursor(cam_index=0, smooth=0.12)
    gc.calibrate()          # optional - runs 9-point calibration overlay
    gc.start()              # begins background thread
    x, y = gc.get_point()  # current gaze screen position in pixels
    gc.stop()
"""
from __future__ import annotations

import json
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Optional, Tuple

# Force UTF-8 output on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import cv2
import numpy as np

# ---------------------------------------------------------------------------
# Face Mesh landmark indices (same for old and new MediaPipe API)
# Left  iris centre = 468   Right iris centre = 473
# ---------------------------------------------------------------------------
L_IRIS_CTR = 468
R_IRIS_CTR = 473

CALIB_FILE  = Path(__file__).parent / "calibration.json"
MODEL_FILE  = Path(__file__).parent / "face_landmarker.task"
MODEL_URL   = ("https://storage.googleapis.com/mediapipe-models/"
               "face_landmarker/face_landmarker/float16/1/face_landmarker.task")

CALIB_DOTS = [          # 9 normalised (x, y) screen positions
    (0.1, 0.1), (0.5, 0.1), (0.9, 0.1),
    (0.1, 0.5), (0.5, 0.5), (0.9, 0.5),
    (0.1, 0.9), (0.5, 0.9), (0.9, 0.9),
]


# ---------------------------------------------------------------------------
# MediaPipe helpers (new Tasks API, mediapipe >= 1.0)
# ---------------------------------------------------------------------------

def _ensure_model() -> str:
    """Download face_landmarker.task if not already present; return its path."""
    if MODEL_FILE.exists():
        return str(MODEL_FILE)
    print(f"[GazeCursor] Downloading face landmarker model (~30 MB) …")
    print(f"             from {MODEL_URL}")
    urllib.request.urlretrieve(MODEL_URL, MODEL_FILE)
    print(f"[GazeCursor] Model saved to {MODEL_FILE}")
    return str(MODEL_FILE)


def _create_landmarker(model_path: str):
    """Create a MediaPipe FaceLandmarker (new Tasks API)."""
    import mediapipe as mp
    from mediapipe.tasks import python as mp_python
    from mediapipe.tasks.python import vision as mp_vision

    base_opts = mp_python.BaseOptions(model_asset_path=model_path)
    opts = mp_vision.FaceLandmarkerOptions(
        base_options=base_opts,
        num_faces=1,
        min_face_detection_confidence=0.5,
        min_face_presence_confidence=0.5,
        min_tracking_confidence=0.5,
    )
    return mp_vision.FaceLandmarker.create_from_options(opts)


def _extract_iris(frame, landmarker) -> Optional[Tuple[float, float]]:
    """
    Run face landmark detection on one BGR frame.
    Returns the normalised (0–1) average iris centre (x, y), or None.
    """
    import mediapipe as mp

    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    mp_img = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
    result = landmarker.detect(mp_img)

    if not result.face_landmarks:
        return None

    lm = result.face_landmarks[0]
    # Average of left-iris (468) and right-iris (473) centres
    lx, ly = lm[L_IRIS_CTR].x, lm[L_IRIS_CTR].y
    rx, ry = lm[R_IRIS_CTR].x, lm[R_IRIS_CTR].y
    return float((lx + rx) / 2), float((ly + ry) / 2)


# ---------------------------------------------------------------------------
# Screen size helper
# ---------------------------------------------------------------------------

def _screen_size() -> Tuple[int, int]:
    try:
        import pyautogui
        return pyautogui.size()
    except Exception:
        return 1920, 1080


# ---------------------------------------------------------------------------
# GazeCursor
# ---------------------------------------------------------------------------

class GazeCursor:
    """
    Background thread that tracks gaze via webcam and updates the OS cursor.

    Parameters
    ----------
    cam_index : int
        OpenCV camera index (0 = built-in webcam).
    smooth : float
        Exponential moving-average factor (0 < α ≤ 1).
        Lower = smoother but laggier. 0.12 is a good starting point.
    move_mouse : bool
        If True, physically moves the OS cursor to the gaze point.
    """

    def __init__(self, cam_index: int = 0, smooth: float = 0.12,
                 move_mouse: bool = True):
        self.cam_index  = cam_index
        self.smooth     = smooth
        self.move_mouse = move_mouse

        self._lock      = threading.Lock()
        self._gaze      = (0.5, 0.5)          # normalised, starts at centre
        self._sw, self._sh = _screen_size()
        self._stop_evt  = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._cal: Optional[dict] = None       # calibration affine coefficients

        self._load_calibration()

    # ------------------------------------------------------------------ API

    def get_point(self) -> Tuple[int, int]:
        """Return current gaze position in screen pixels."""
        with self._lock:
            nx, ny = self._gaze
        return self._to_screen(nx, ny)

    def start(self) -> None:
        """Start background tracking thread."""
        self._stop_evt.clear()
        self._thread = threading.Thread(target=self._track_loop, daemon=True)
        self._thread.start()
        print("[GazeCursor] Tracking started.")

    def stop(self) -> None:
        """Stop background tracking thread."""
        self._stop_evt.set()
        if self._thread:
            self._thread.join(timeout=3)
        print("[GazeCursor] Tracking stopped.")

    def calibrate(self) -> None:
        """
        Show a full-screen calibration overlay with 9 dots.
        The user fixates each dot; the iris positions are recorded and
        an affine mapping is fitted and saved to calibration.json.
        """
        model_path = _ensure_model()
        cap = cv2.VideoCapture(self.cam_index)
        if not cap.isOpened():
            print("[GazeCursor] WARNING: cannot open webcam for calibration.")
            return

        landmarker = _create_landmarker(model_path)
        sw, sh = self._sw, self._sh

        win = "EyeTap Calibration"
        cv2.namedWindow(win, cv2.WND_PROP_FULLSCREEN)
        cv2.setWindowProperty(win, cv2.WND_PROP_FULLSCREEN,
                              cv2.WINDOW_FULLSCREEN)

        eye_samples: list = []
        screen_dots: list = []

        for dot_idx, (sx, sy) in enumerate(CALIB_DOTS):
            dot_px, dot_py = int(sx * sw), int(sy * sh)
            collected: list = []
            deadline = time.time() + 2.5

            while time.time() < deadline:
                ok, frame = cap.read()
                if not ok:
                    continue
                frame = cv2.flip(frame, 1)
                iris = _extract_iris(frame, landmarker)

                # Build overlay
                canvas = np.zeros((sh, sw, 3), dtype=np.uint8)
                for i, (dx, dy) in enumerate(CALIB_DOTS):
                    col = (0, 255, 0) if i == dot_idx else (60, 60, 60)
                    cv2.circle(canvas, (int(dx * sw), int(dy * sh)), 18, col, -1)

                elapsed = 2.5 - (deadline - time.time())
                angle   = int(360 * elapsed / 2.5)
                cv2.ellipse(canvas, (dot_px, dot_py), (28, 28), -90, 0, angle,
                            (0, 200, 255), 3)
                remaining = int(deadline - time.time()) + 1
                cv2.putText(canvas,
                            f"Look at the GREEN dot  ({dot_idx+1}/9)  {remaining}s",
                            (sw // 2 - 260, sh - 60),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.9, (200, 200, 200), 2)
                cv2.imshow(win, canvas)
                cv2.waitKey(1)

                if iris is not None and time.time() > deadline - 1.5:
                    collected.append(iris)

            if collected:
                eye_samples.append(np.mean(collected, axis=0))
                screen_dots.append([sx, sy])

        cap.release()
        cv2.destroyAllWindows()

        if len(eye_samples) < 4:
            print("[GazeCursor] Calibration failed – too few samples.")
            return

        # Least-squares affine fit: screen_coord = A * [ex, ey, 1]
        eye_pts = np.array(eye_samples, dtype=np.float64)
        scr_pts = np.array(screen_dots,  dtype=np.float64)
        A_aug   = np.hstack([eye_pts, np.ones((len(eye_pts), 1))])
        cx, *_ = np.linalg.lstsq(A_aug, scr_pts[:, 0], rcond=None)
        cy, *_ = np.linalg.lstsq(A_aug, scr_pts[:, 1], rcond=None)
        cal = {"cx": cx.tolist(), "cy": cy.tolist()}
        with open(CALIB_FILE, "w") as f:
            json.dump(cal, f, indent=2)
        self._cal = cal
        print(f"[GazeCursor] Calibration saved to {CALIB_FILE}")

    # ------------------------------------------------------------ internals

    def _load_calibration(self) -> None:
        if CALIB_FILE.exists():
            try:
                with open(CALIB_FILE) as f:
                    self._cal = json.load(f)
                print(f"[GazeCursor] Loaded calibration from {CALIB_FILE}")
            except Exception:
                pass

    def _apply_calibration(self, nx: float, ny: float) -> Tuple[float, float]:
        if self._cal is None:
            return nx, ny
        cx, cy = self._cal["cx"], self._cal["cy"]
        sx = cx[0] * nx + cx[1] * ny + cx[2]
        sy = cy[0] * nx + cy[1] * ny + cy[2]
        return float(np.clip(sx, 0.0, 1.0)), float(np.clip(sy, 0.0, 1.0))

    def _to_screen(self, nx: float, ny: float) -> Tuple[int, int]:
        x = int(np.clip(nx * self._sw, 0, self._sw - 1))
        y = int(np.clip(ny * self._sh, 0, self._sh - 1))
        return x, y

    def _track_loop(self) -> None:
        try:
            model_path = _ensure_model()
        except Exception as e:
            print(f"[GazeCursor] ERROR downloading model: {e}")
            return

        cap = cv2.VideoCapture(self.cam_index)
        if not cap.isOpened():
            print(f"[GazeCursor] ERROR: cannot open webcam index {self.cam_index}")
            return

        try:
            landmarker = _create_landmarker(model_path)
        except Exception as e:
            print(f"[GazeCursor] ERROR creating face landmarker: {e}")
            cap.release()
            return

        α = self.smooth
        ema_x, ema_y = 0.5, 0.5   # start at screen centre

        pg = None
        if self.move_mouse:
            try:
                import pyautogui
                pg = pyautogui
            except ImportError:
                pass

        print("[GazeCursor] Face landmarker loaded. Gaze tracking active.")

        while not self._stop_evt.is_set():
            ok, frame = cap.read()
            if not ok:
                time.sleep(0.01)
                continue

            frame = cv2.flip(frame, 1)    # mirror so left/right feel natural
            iris  = _extract_iris(frame, landmarker)

            if iris is not None:
                raw_x, raw_y   = iris
                cal_x, cal_y   = self._apply_calibration(raw_x, raw_y)
                ema_x = α * cal_x + (1 - α) * ema_x
                ema_y = α * cal_y + (1 - α) * ema_y

                with self._lock:
                    self._gaze = (ema_x, ema_y)

                if pg:
                    sx, sy = self._to_screen(ema_x, ema_y)
                    try:
                        pg.moveTo(sx, sy, duration=0)
                    except Exception:
                        pass

        cap.release()


# ---------------------------------------------------------------------------
# Standalone test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Test gaze cursor")
    ap.add_argument("--cam",       type=int,   default=0)
    ap.add_argument("--calibrate", action="store_true")
    ap.add_argument("--smooth",    type=float, default=0.12)
    args = ap.parse_args()

    gc = GazeCursor(cam_index=args.cam, smooth=args.smooth, move_mouse=True)
    if args.calibrate:
        gc.calibrate()
    gc.start()
    print("Gaze cursor running. Press Ctrl+C to stop.")
    try:
        while True:
            x, y = gc.get_point()
            print(f"\rGaze → ({x:4d}, {y:4d})", end="", flush=True)
            time.sleep(0.05)
    except KeyboardInterrupt:
        pass
    finally:
        gc.stop()
