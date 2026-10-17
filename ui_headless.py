"""
Headless UI shim for Mark-LV — replaces ui.py when running on a server
without display. Provides the same JarvisUI interface but all GUI
operations are no-ops or log to console. Audio still works via
sounddevice; wake word via openwakeword.
"""

import sys
import time
import threading
import logging

log = logging.getLogger("jarvis.headless")

class _HeadlessRoot:
    """Stub for ui.root.mainloop() — nothing to run."""

    def mainloop(self):
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass

    def quit(self):
        pass

class JarvisUI:
    """
    Drop-in replacement for ui.JarvisUI. All visual methods are no-ops
    or log to console. The assistant keeps running without a display.
    """

    def __init__(self, face_path: str = "face.png", size=None):
        self._face_path = face_path
        self._root = _HeadlessRoot()
        self._assistant_name = "JARVIS"
        self._muted = False
        self._current_file = None

        # Callbacks — set by JarvisLive.__init__
        self.on_push_to_talk = None
        self.on_text_command = None
        self.on_remote_clicked = None
        self.on_interrupt = None
        self.on_voice_change = None
        self.on_audio_device_change = None
        self.on_wake_toggle = None
        self.on_wake_manual = None
        self.on_wake_install = None
        self.ptt_hold = None
        self.request_say = None
        self.wake_is_ready = None
        self.wake_get_state = None

        # API key handling — headless reads from config/api_keys.json directly
        from memory.config_manager import load_api_keys
        cfg = load_api_keys()
        key = cfg.get("gemini_api_key", "") if isinstance(cfg, dict) else ''
        self._api_key = key if isinstance(key, str) and key.strip() else ''
        self._ready = bool(self._api_key)

        print("[JARVIS] Headless UI initialised — assistant ready.")

    # ── Properties used by JarvisLive ────────────────────────────────────

    @property
    def root(self):
        return self._root

    @property
    def muted(self):
        return self._muted

    @muted.setter
    def muted(self, value):
        self._muted = bool(value)

    @property
    def current_file(self):
        return self._current_file

    @current_file.setter
    def current_file(self, value):
        self._current_file = value

    # ── API key ──────────────────────────────────────────────────────────

    def wait_for_api_key(self):
        """Headless: key is already in config, no GUI to wait for."""
        if not self._api_key:
            print("[JARVIS] No Gemini API key found in config/api_keys.json")
            print("Get one free at https://aistudio.google.com/app/apikey")
            print("Then run: python main.py again")
            sys.exit(78)
        print(f"[JARVIS] API key loaded ({len(self._api_key)} chars)")

    def prompt_reconfig(self):
        """Headless: just warn on console."""
        print("[JARVIS] API key invalid — check config/api_keys.json")
        raise SystemExit(78)

    # ── State & logging (these DO matter — they drive console output) ────

    def write_log(self, text: str):
        """Log to console — this is how the headless assistant reports."""
        print(f"[JARVIS] {text}", flush=True)

    def set_state(self, state: str):
        """State changes are visible in the log."""
        states = {
            "LISTENING": "Listening",
            "SPEAKING": "Speaking",
            "THINKING": "Thinking",
            "SLEEPING": "Sleeping",
        }
        label = states.get(state, state)
        print(f"[JARVIS] State: {label}", flush=True)

    def set_audio_level(self, level: float):
        """Audio level — show as a simple bar in console."""
        bar_len = int(level * 30)
        bar = "#" * bar_len + "." * (30 - bar_len)
        print(f"\r[JARVIS] {bar} {level:.0%}", end="", flush=True)

    def push_visemes(self, frames, hop, at):
        """No-op — visemes are for the avatar."""
        pass

    # ── Camera / content (no-ops for headless) ───────────────────────────

    def start_camera_stream(self):
        print("[JARVIS] Camera stream requested (headless — skipped)")

    def stop_camera_stream(self):
        pass

    def show_content(self, label: str, content: str):
        """Print content to console instead of HUD panel."""
        print(f"\n[JARVIS] {label}")
        print(content)
        print()

    # ── Phone / dashboard (no-ops for headless) ──────────────────────────

    def notify_phone_connected(self):
        print("[JARVIS] Phone dashboard connected (headless)")

    # ── Confirmation (headless: fail closed) ────────────────────────────

    def show_confirm(self, title: str, detail: str):
        """No authenticated approval UI exists: reject, clearing the core gate."""
        raise PermissionError('Headless confirmation unavailable; action denied')

    def hide_confirm(self):
        pass

    # ── Plugin UI hooks (no-ops) ─────────────────────────────────────────

    def get_plugins(self):
        return []

    def get_plugin_settings(self):
        return {}