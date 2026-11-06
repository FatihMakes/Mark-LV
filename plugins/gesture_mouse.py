"""
ULTRON gesture mouse — opt-in webcam hand tracking.

Say "start gesture mouse" to begin. The camera is opened only after that
explicit request. Say "stop gesture mouse" to stop and release any held button.

Controls:
  • Open hand: move the pointer.
  • Pinch thumb + index finger: hold left mouse button (click/drag).
  • Release the pinch: release the left mouse button.
  • Closed fist: pause pointer movement.

This plugin uses MediaPipe Tasks locally. Camera frames are not uploaded to
Gemini or any remote service.
"""
from __future__ import annotations

import threading
import time
import urllib.request
from pathlib import Path

PLUGIN = {
    "name": "gesture_mouse",
    "description": (
        "Start, stop, or check the camera-based hand gesture mouse. "
        "Use only when the user explicitly asks to start/enable gesture mouse, "
        "stop/disable it, or asks whether it is running. Do not start the camera "
        "for ordinary requests."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "command": {
                "type": "STRING",
                "enum": ["start", "stop", "status"],
                "description": "start the gesture mouse, stop it, or check status",
            }
        },
        "required": ["command"],
    },
    "behavior": "NON_BLOCKING",
    "tier": "extra",
}

_MODEL_URL = (
    "https://storage.googleapis.com/mediapipe-models/hand_landmarker/"
    "hand_landmarker/float16/1/hand_landmarker.task"
)
_MODEL_PATH = Path(__file__).resolve().parent.parent / "models" / "hand_landmarker.task"
_LOCK = threading.RLock()
_STOP = threading.Event()
_THREAD: threading.Thread | None = None
_STATUS = "stopped"


def _log(player, message: str) -> None:
    if player is not None:
        try:
            player.write_log("GESTURE: " + message)
        except Exception:
            pass


def _ensure_model() -> Path:
    if _MODEL_PATH.is_file() and _MODEL_PATH.stat().st_size > 100_000:
        return _MODEL_PATH
    _MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp = _MODEL_PATH.with_suffix(".download")
    request = urllib.request.Request(
        _MODEL_URL, headers={"User-Agent": "Mark-LVI-GestureMouse/1.0"}
    )
    with urllib.request.urlopen(request, timeout=30) as response, temp.open("wb") as out:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            out.write(chunk)
    if temp.stat().st_size < 100_000:
        temp.unlink(missing_ok=True)
        raise RuntimeError("The hand-tracking model download was incomplete.")
    temp.replace(_MODEL_PATH)
    return _MODEL_PATH


def _gesture_loop(player=None) -> None:
    global _STATUS
    cap = None
    landmarker = None
    mouse = None
    button_down = False
    try:
        import cv2
        import pyautogui
        import mediapipe as mp

        if not hasattr(mp, "tasks"):
            raise RuntimeError(
                "This MediaPipe build does not include the Tasks API. "
                "Upgrade it with: python -m pip install -U mediapipe"
            )

        model_path = _ensure_model()
        BaseOptions = mp.tasks.BaseOptions
        VisionRunningMode = mp.tasks.vision.RunningMode
        HandLandmarker = mp.tasks.vision.HandLandmarker
        HandLandmarkerOptions = mp.tasks.vision.HandLandmarkerOptions
        options = HandLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=str(model_path)),
            running_mode=VisionRunningMode.VIDEO,
            num_hands=1,
            min_hand_detection_confidence=0.55,
            min_hand_presence_confidence=0.55,
            min_tracking_confidence=0.55,
        )
        landmarker = HandLandmarker.create_from_options(options)

        camera_index = 0
        cap = cv2.VideoCapture(camera_index, cv2.CAP_DSHOW if hasattr(cv2, "CAP_DSHOW") else 0)
        if not cap.isOpened():
            raise RuntimeError("The webcam could not be opened. Check camera permissions.")

        pyautogui.FAILSAFE = True
        pyautogui.PAUSE = 0.01
        screen_w, screen_h = pyautogui.size()
        smooth_x = smooth_y = None
        last_pinch = False
        started_at = time.monotonic()
        frame_id = 0

        with _LOCK:
            _STATUS = "running"
        _log(player, "Hand tracking is active. Open palm moves; pinch clicks/drags; fist pauses.")

        while not _STOP.is_set():
            ok, frame = cap.read()
            if not ok or frame is None:
                time.sleep(0.03)
                continue
            frame = cv2.flip(frame, 1)
            h, w = frame.shape[:2]
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
            timestamp_ms = int((time.monotonic() - started_at) * 1000)
            result = landmarker.detect_for_video(image, timestamp_ms)
            frame_id += 1

            if not result.hand_landmarks:
                if button_down:
                    pyautogui.mouseUp()
                    button_down = False
                last_pinch = False
                continue

            lm = result.hand_landmarks[0]
            # Fingertip and knuckle landmarks (MediaPipe 21-point hand model).
            thumb = lm[4]
            index_tip, index_pip = lm[8], lm[6]
            middle_tip, middle_pip = lm[12], lm[10]
            ring_tip, ring_pip = lm[16], lm[14]
            pinky_tip, pinky_pip = lm[20], lm[18]

            pinch_distance = ((thumb.x - index_tip.x) ** 2 +
                              (thumb.y - index_tip.y) ** 2) ** 0.5
            pinch_now = pinch_distance < 0.045
            index_up = index_tip.y < index_pip.y
            middle_up = middle_tip.y < middle_pip.y
            ring_up = ring_tip.y < ring_pip.y
            pinky_up = pinky_tip.y < pinky_pip.y
            open_palm = index_up and middle_up and ring_up and pinky_up
            fist = not index_up and not middle_up and not ring_up and not pinky_up

            # Map the index tip into a central screen region and smooth jitter.
            target_x = int((0.08 + index_tip.x * 0.84) * screen_w)
            target_y = int((0.08 + index_tip.y * 0.84) * screen_h)
            if smooth_x is None:
                smooth_x, smooth_y = target_x, target_y
            else:
                smooth_x = int(smooth_x * 0.68 + target_x * 0.32)
                smooth_y = int(smooth_y * 0.68 + target_y * 0.32)

            if open_palm and not fist:
                pyautogui.moveTo(smooth_x, smooth_y, duration=0)
            elif not fist and not pinch_now:
                # Keep the pointer steady in neutral poses; no accidental clicks.
                pass

            if pinch_now and not last_pinch:
                pyautogui.mouseDown()
                button_down = True
            elif not pinch_now and last_pinch and button_down:
                pyautogui.mouseUp()
                button_down = False
            last_pinch = pinch_now

            # A closed fist is an explicit pause gesture; release drag immediately.
            if fist and button_down:
                pyautogui.mouseUp()
                button_down = False
                last_pinch = False

    except Exception as exc:
        _log(player, f"Stopped: {exc}")
    finally:
        try:
            if button_down and mouse is not None:
                mouse.mouseUp()
        except Exception:
            pass
        try:
            if button_down:
                import pyautogui
                pyautogui.mouseUp()
        except Exception:
            pass
        if cap is not None:
            try:
                cap.release()
            except Exception:
                pass
        if landmarker is not None:
            try:
                landmarker.close()
            except Exception:
                pass
        with _LOCK:
            _STATUS = "stopped"
        _STOP.clear()
        _log(player, "Gesture mouse stopped; webcam released.")


def run(parameters: dict, player=None, session_memory=None) -> str:
    global _THREAD, _STATUS
    command = str((parameters or {}).get("command", "status")).strip().lower()
    if command not in {"start", "stop", "status"}:
        return "Choose start, stop, or status."
    with _LOCK:
        active = _THREAD is not None and _THREAD.is_alive()
        if command == "status":
            return "Gesture mouse is running." if active else "Gesture mouse is stopped."
        if command == "stop":
            if not active:
                _STATUS = "stopped"
                return "Gesture mouse is already stopped."
            _STOP.set()
            return "Stopping gesture mouse and releasing the webcam."
        if active:
            return "Gesture mouse is already running."
        _STOP.clear()
        _STATUS = "starting"
        _THREAD = threading.Thread(
            target=_gesture_loop, args=(player,), name="gesture-mouse", daemon=True
        )
        _THREAD.start()
    _log(player, "Starting webcam hand tracking. Say 'stop gesture mouse' to turn it off.")
    return "Starting gesture mouse. The webcam will activate now; say stop gesture mouse to turn it off."
