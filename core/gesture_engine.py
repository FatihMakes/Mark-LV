"""Opt-in webcam hand tracking for MARK LVI.

Safety defaults:
- Camera starts only after an explicit user command.
- Gesture control stops on loss of camera / worker exception.
- Pinch is a hold-to-drag; releasing the pinch releases the mouse.
- Destructive file actions are intentionally not mapped to gestures.
"""
from __future__ import annotations

import threading
import time


class GestureEngine:
    def __init__(self, on_status=None, on_gesture=None):
        self.on_status = on_status or (lambda _s: None)
        self.on_gesture = on_gesture or (lambda _s: None)
        self._stop = threading.Event()
        self._thread = None
        self._lock = threading.Lock()
        self._running = False
        self._dragging = False
        self._last_action = 0.0
        self._last_y = None
        self._last_x = None
        self._screen = None

    @property
    def running(self):
        with self._lock:
            return self._running

    def start(self, camera_index=0):
        with self._lock:
            if self._running:
                return False, "Gesture control is already running."
            self._stop.clear()
            self._running = True
        self._thread = threading.Thread(
            target=self._loop, args=(int(camera_index),),
            daemon=True, name="mark-lvi-gesture-control"
        )
        self._thread.start()
        return True, "Gesture control starting. Keep your hand visible to the camera."

    def stop(self):
        self._stop.set()
        self._release_mouse()
        with self._lock:
            was_running = self._running
            self._running = False
        return was_running

    def _emit(self, text):
        try:
            self.on_gesture(str(text))
        except Exception:
            pass

    def _release_mouse(self):
        if self._dragging:
            try:
                import pyautogui
                pyautogui.mouseUp()
            except Exception:
                pass
            self._dragging = False

    def _cooldown(self, seconds=0.65):
        now = time.monotonic()
        if now - self._last_action < seconds:
            return False
        self._last_action = now
        return True

    @staticmethod
    def _distance(a, b):
        return ((a.x - b.x) ** 2 + (a.y - b.y) ** 2) ** 0.5

    @staticmethod
    def _finger_up(hand, tip, pip):
        return hand.landmark[tip].y < hand.landmark[pip].y

    def _loop(self, camera_index):
        cap = None
        try:
            import cv2
            import pyautogui
            try:
                import mediapipe as mp
            except Exception as exc:
                raise RuntimeError(
                    "MediaPipe is not installed. Install it with: "
                    "python -m pip install mediapipe"
                ) from exc

            pyautogui.FAILSAFE = True
            pyautogui.PAUSE = 0.01
            self._screen = pyautogui.size()
            backend = cv2.CAP_DSHOW if hasattr(cv2, "CAP_DSHOW") else 0
            cap = cv2.VideoCapture(camera_index, backend)
            if not cap.isOpened():
                cap.release()
                cap = cv2.VideoCapture(0)
            if not cap.isOpened():
                raise RuntimeError("Could not open the webcam. Check camera permissions.")

            self.on_status("ACTIVE — webcam hand tracking")
            hands_api = mp.solutions.hands
            with hands_api.Hands(
                static_image_mode=False, max_num_hands=2,
                model_complexity=0, min_detection_confidence=0.65,
                min_tracking_confidence=0.6
            ) as tracker:
                while not self._stop.is_set():
                    ok, frame = cap.read()
                    if not ok:
                        time.sleep(0.03)
                        continue
                    frame = cv2.flip(frame, 1)
                    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                    result = tracker.process(rgb)
                    if result.multi_hand_landmarks:
                        hand = result.multi_hand_landmarks[0]
                        self._process_hand(hand, pyautogui)
                    else:
                        self._release_mouse()
                    time.sleep(0.008)
        except Exception as exc:
            self.on_status("ERROR — " + str(exc)[:180])
            self._emit("Gesture control stopped: " + str(exc))
        finally:
            self._release_mouse()
            if cap is not None:
                cap.release()
            with self._lock:
                self._running = False
            if not self._stop.is_set():
                self.on_status("STOPPED")
            else:
                self.on_status("STOPPED")

    def _process_hand(self, hand, pyautogui):
        lm = hand.landmark
        thumb, index = lm[4], lm[8]
        middle, ring, pinky = lm[12], lm[16], lm[20]
        index_up = self._finger_up(hand, 8, 6)
        middle_up = self._finger_up(hand, 12, 10)
        ring_up = self._finger_up(hand, 16, 14)
        pinky_up = self._finger_up(hand, 20, 18)
        pinch = self._distance(thumb, index) < 0.055

        # Index-finger pointing moves the pointer. Pinching holds the mouse
        # button down, so moving while pinched drags; opening releases it.
        if index_up and not middle_up:
            sx, sy = self._screen
            x = max(0, min(sx - 1, int(index.x * sx)))
            y = max(0, min(sy - 1, int(index.y * sy)))
            pyautogui.moveTo(x, y, duration=0)
            if pinch and not self._dragging:
                pyautogui.mouseDown()
                self._dragging = True
                self._emit("PINCH — drag started")
            elif not pinch and self._dragging:
                pyautogui.mouseUp()
                self._dragging = False
                self._emit("RELEASE — drag ended")
            return
        self._release_mouse()

        # Two raised fingers: vertical hand motion scrolls the active window.
        if index_up and middle_up and not ring_up and not pinky_up:
            y = (index.y + middle.y) / 2
            if self._last_y is not None:
                delta = self._last_y - y
                if abs(delta) > 0.012:
                    pyautogui.scroll(max(-5, min(5, int(delta * 100))))
            self._last_y = y
            return
        self._last_y = None

        # Open palm: safe media pause/play toggle with cooldown.
        if index_up and middle_up and ring_up and pinky_up:
            if self._cooldown(1.2):
                pyautogui.press("playpause")
                self._emit("OPEN PALM — media play/pause")
            return

        # Thumb up: volume up; thumb down: volume down.
        thumb_up = thumb.y < lm[3].y
        other_folded = not index_up and not middle_up and not ring_up and not pinky_up
        if other_folded and thumb_up and self._cooldown(0.35):
            pyautogui.press("volumeup")
            self._emit("THUMBS UP — volume up")
        elif other_folded and thumb.y > lm[3].y and self._cooldown(0.35):
            pyautogui.press("volumedown")
            self._emit("THUMBS DOWN — volume down")

        # Horizontal swipe: switch browser/app tabs. Require a deliberate
        # displacement and a cooldown so a stationary hand does not repeat.
        x = index.x
        if self._last_x is not None:
            dx = x - self._last_x
            if abs(dx) > 0.18 and self._cooldown(0.9):
                pyautogui.hotkey("ctrl", "shift", "tab") if dx < 0 else pyautogui.hotkey("ctrl", "tab")
                self._emit("SWIPE — previous/next tab")
        self._last_x = x
