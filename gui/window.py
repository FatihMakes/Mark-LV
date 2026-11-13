"""gui/window.py — MainWindow — lays the HUD out and owns every signal the rest of the app talks to.

Split out of ui.py in Mark LVII; behaviour unchanged."""
from __future__ import annotations

import json
import math
import os
import platform
import random
import subprocess
import sys
import threading
import time
from pathlib import Path

import psutil

from PyQt6.QtCore import (
    QEasingCurve, QLineF, QMimeData, QObject, QParallelAnimationGroup, QPointF,
    QPoint, QPropertyAnimation, QRect, QRectF, QSize, QSizeF, Qt, QTimer,
    QUrl, pyqtSignal,
)
from PyQt6.QtGui import (
    QBrush, QColor, QConicalGradient, QDragEnterEvent, QDropEvent, QFont,
    QFontDatabase, QKeySequence, QLinearGradient, QPainter, QPainterPath,
    QPen, QPixmap, QRadialGradient, QShortcut,
)
from PyQt6.QtWidgets import (
    QApplication, QComboBox, QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QMainWindow, QPushButton, QScrollArea, QSizePolicy, QSplitter,
    QGraphicsScene, QGraphicsView, QGraphicsPixmapItem,
    QStackedWidget, QTextEdit, QVBoxLayout, QWidget, QProgressBar, QSlider,
)

from gui.theme import (
    API_FILE,
    APP_PROTOCOL,
    APP_VERSION,
    C,
    CONFIG_DIR,
    DEFAULT_UI_COLOR,
    HAVE_VIDEO,
    QAudioOutput,
    QGraphicsVideoItem,
    QMediaPlayer,
    _DEFAULT_H,
    _DEFAULT_W,
    _LEFT_W,
    _MIN_H,
    _MIN_W,
    _OS,
    _RIGHT_W,
    _read_full_config,
    apply_ui_accent,
    current_palette,
    retheme_all_widgets,
)
from gui.metrics import (
    _metrics,
)
from gui.hud import (
    HudCanvas,
)
from gui.widgets import (
    FileDropZone,
    LogWidget,
    MetricBar,
    _CameraPreview,
    _FILE_ICONS,
    _file_category,
    _fmt_size,
)
from gui.overlays import (
    AudioDeviceOverlay,
    ClipboardPanel,
    ConfirmBanner,
    CustomizeOverlay,
    MemoryOverlay,
    PluginManagerOverlay,
    PluginSettingsOverlay,
    RemoteKeyOverlay,
    SetupOverlay,
)
from gui.providers_panel import (
    ProvidersOverlay,
)
from gui.plugin_hud import PluginHudHost

from PyQt6.QtWidgets import QGridLayout

from gui.style import DISPLAY_CSS, btn_css, hud_font, px_font, scroll_css
from gui.widgets import (ArcLogo, CommandFrame, DataRow, HeaderBar, HoloPanel, IconButton,
                         PanelText, RingGauge, RoutineRow, Sparkline, StateChip, StatusStrip,
                         TaskRow)


class MainWindow(QMainWindow):
    _log_sig        = pyqtSignal(str)
    _state_sig      = pyqtSignal(str)
    _content_sig    = pyqtSignal(str, str)   # (title, text) — thread-safe content display
    _codejob_sig    = pyqtSignal(object)     # dev_agent job state — live coding panel
    _reconfig_sig   = pyqtSignal()           # trigger setup overlay from any thread
    _camera_sig     = pyqtSignal(bytes)      # show camera frame preview (small overlay)
    _cam_stream_sig = pyqtSignal(bool)       # True=start live stream, False=stop
    _cam_frame_sig  = pyqtSignal(bytes)      # live camera frame → HUD area
    _video_open_sig  = pyqtSignal(str, str, bool, str)  # video, title, muted, audio
    _image_open_sig  = pyqtSignal(str, str)   # still picture on the HUD: path, title
    _wake_btns_sig   = pyqtSignal()          # wake state resolved off-thread
    _video_close_sig = pyqtSignal()
    _video_mute_sig  = pyqtSignal(bool)
    _video_seek_sig  = pyqtSignal(float, bool)          # seconds, relative
    _video_pause_sig = pyqtSignal(bool)
    _clipboard_sig  = pyqtSignal(str)        # clipboard text changed (thread-safe)
    _confirm_sig    = pyqtSignal(str, str)   # (title, detail) — irreversible-action gate
    _confirm_hide_sig = pyqtSignal()
    _wake_dl_sig    = pyqtSignal(bool, str)  # wake-word install finished (ok, message)
    _quiz_sig       = pyqtSignal(str, object, object)  # (topic, questions, grader)
    _quiz_hide_sig  = pyqtSignal()
    _review_sig     = pyqtSignal(str, str, object, object)  # document review payload
    _plugin_hud_open_sig   = pyqtSignal(str, str, object, str, bool)  # (plugin_id, title, content, content_type, replace)
    _plugin_hud_update_sig = pyqtSignal(str, object, object)          # (plugin_id, content, content_type)
    _plugin_hud_close_sig  = pyqtSignal(str)                          # (plugin_id)

    def __init__(self, face_path: str):
        super().__init__()
        self._face_path = face_path

        # Load customization from config
        _cfg = _read_full_config()
        self._assistant_name: str = (_cfg.get("assistant_name") or "JARVIS").strip()
        _display = self._assistant_name.upper()

        # Apply the saved UI colour BEFORE panels/stylesheets are built
        _ui_color = (_cfg.get("ui_color") or "").strip()
        if _ui_color and _ui_color.lower() != DEFAULT_UI_COLOR:
            apply_ui_accent(_ui_color)

        self.setWindowTitle(f"{_display} — {APP_VERSION}")
        self.setMinimumSize(_MIN_W, _MIN_H)
        self.resize(_DEFAULT_W, _DEFAULT_H)

        screen = QApplication.primaryScreen().availableGeometry()
        self.move(
            (screen.width()  - _DEFAULT_W) // 2,
            (screen.height() - _DEFAULT_H) // 2,
        )

        self.on_text_command   = None
        self.on_remote_clicked = None   # callable: () -> (url, key) | None
        self.on_interrupt      = None   # callable: () -> None — stop JARVIS mid-speech
        self.on_voice_change   = None   # callable: () -> None — rebuild session with new voice
        self.on_audio_device_change = None  # callable: () -> None — reopen audio streams
        self._confirm_overlay  = None   # live ConfirmBanner, if one is on screen
        self.get_plugins       = None   # callable: () -> list[dict], set by JarvisLive
        self.get_plugin_settings = None # callable: () -> list[dict] settings schemas, set by JarvisLive
        self.on_wake_toggle    = None   # callable: (enable: bool) -> str, set by JarvisLive
        self.on_wake_manual    = None   # callable: () -> None — manual sleep/wake
        self.on_push_to_talk   = None   # callable: (enable: bool) -> str scope
        self.ptt_hold          = None   # callable: (held: bool) -> None — windowed chord
        self.wake_get_state    = None   # callable: () -> dict {enabled, awake, ready}
        self._muted            = False
        self._current_file: str | None = None
        self._remote_overlay: RemoteKeyOverlay | None = None
        self._customize_overlay: CustomizeOverlay | None = None

        central = QWidget()
        central.setStyleSheet(f"background: {C.BG};")
        self.setCentralWidget(central)

        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_header())

        body = QHBoxLayout()
        body.setContentsMargins(10, 10, 10, 10)
        body.setSpacing(10)

        self._left_panel = self._build_left_panel()
        body.addWidget(self._left_panel, stretch=0)

        # Center column: HUD + resizable content panel via QSplitter
        self.hud = HudCanvas(face_path, _display)
        self.hud.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._content_panel = self._build_content_panel()
        self._quiz_panel = self._build_quiz_panel()

        # Live camera container — replaces HUD when camera stream is active
        _cam_cont = QWidget()
        _cam_cont.setStyleSheet("background: #000308;")
        _cam_v = QVBoxLayout(_cam_cont)
        _cam_v.setContentsMargins(0, 0, 0, 0)
        _cam_v.setSpacing(0)
        _cam_hdr = QHBoxLayout()
        _cam_hdr.setContentsMargins(8, 5, 8, 5)
        _cam_title = QLabel("◈  CAMERA FEED")
        _cam_title.setFont(hud_font(8, QFont.Weight.Bold))
        _cam_title.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        _cam_hdr.addWidget(_cam_title)
        _cam_hdr.addStretch()
        _cam_x = QPushButton("✕  CLOSE")
        _cam_x.setFont(hud_font(8, QFont.Weight.Bold))
        _cam_x.setCursor(Qt.CursorShape.PointingHandCursor)
        _cam_x.setStyleSheet(f"""
            QPushButton {{
                color: {C.TEXT_DIM}; background: transparent;
                border: none; padding: 2px 6px;
            }}
            QPushButton:hover {{ color: {C.PRI}; }}
        """)
        _cam_x.clicked.connect(self.stop_camera_stream)
        _cam_hdr.addWidget(_cam_x)
        _cam_v.addLayout(_cam_hdr)
        self._cam_live_lbl = QLabel()
        self._cam_live_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._cam_live_lbl.setStyleSheet("background: transparent;")
        self._cam_live_lbl.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        _cam_v.addWidget(self._cam_live_lbl, stretch=1)

        # ── Video surface ────────────────────────────────────────────────
        # Built exactly like the camera page above and stacked beside it,
        # because it is the same idea: something takes the centre of the HUD
        # for a while and then gives it back. Sharing the stack rather than
        # inventing a second mechanism means the avatar, the camera and a video
        # can never be on screen at once.
        self._video_split = False        # is the sound a separate stream?
        self._video_auto_muted = False   # did JARVIS close the mic, or the user?
        # A plain flag rather than reading the widget. video_is_playing() is
        # called from plugin threads, and reading a widget's state from one is
        # not something to rely on; an attribute is.
        self._video_on = False
        self._video_cont = QWidget()
        self._video_cont.setStyleSheet(f"background: {C.BG};")
        _vid_v = QVBoxLayout(self._video_cont)
        _vid_v.setContentsMargins(8, 6, 8, 8)
        _vid_v.setSpacing(4)

        _vid_hdr = QHBoxLayout()
        self._video_title = QLabel("▶  VIDEO")
        self._video_title.setFont(hud_font(9, QFont.Weight.Bold))
        self._video_title.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        _vid_hdr.addWidget(self._video_title)
        _vid_hdr.addStretch()

        def _vid_btn(text: str) -> QPushButton:
            b = QPushButton(text)
            b.setFont(hud_font(8, QFont.Weight.Bold))
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setStyleSheet(f"""
                QPushButton {{
                    color: {C.TEXT_DIM}; background: transparent;
                    border: none; padding: 2px 6px;
                }}
                QPushButton:hover {{ color: {C.PRI}; }}
            """)
            return b

        # Muted is the default and the button says so, because a soundtrack
        # talking over JARVIS is the one way this feature could make the
        # assistant worse rather than better.
        self._video_mute_btn = _vid_btn("🔇  SOUND OFF")
        self._video_mute_btn.clicked.connect(self._toggle_video_mute)
        _vid_hdr.addWidget(self._video_mute_btn)

        _vid_x = _vid_btn("✕  CLOSE")
        _vid_x.clicked.connect(self.stop_video)
        _vid_hdr.addWidget(_vid_x)
        _vid_v.addLayout(_vid_hdr)

        # A GRAPHICS ITEM, NOT A QVideoWidget.
        #
        # QVideoWidget gets a native window of its own, and a native window
        # is painted by the compositor above every ordinary widget that
        # shares the screen with it — so the settings drawer opened
        # *underneath* the video and raise_() could not help, because the
        # two are not in the same painting order at all. A video item drawn
        # into a QGraphicsView goes through Qt's own painter like any other
        # widget, and anything laid over it stays over it.
        #
        # The surface is built whether or not Qt can play video, because a
        # STILL PICTURE needs a scene and a view and nothing else. A Qt
        # install without the multimedia plugins can still put a photo where
        # the avatar sits, and `generate_image` has been looking for somewhere
        # to show its own output since Mark LV.
        self._video_scene = QGraphicsScene(self)
        self._image_item = QGraphicsPixmapItem()
        self._image_item.setTransformationMode(
            Qt.TransformationMode.SmoothTransformation)
        self._image_item.setVisible(False)
        self._video_scene.addItem(self._image_item)
        self._video_widget = QGraphicsView(self._video_scene)
        self._video_widget.setStyleSheet("background: #000; border: none;")
        self._video_widget.setFrameShape(QGraphicsView.Shape.NoFrame)
        self._video_widget.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._video_widget.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._video_widget.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        _vid_v.addWidget(self._video_widget, stretch=1)

        if HAVE_VIDEO:
            self._video_item = QGraphicsVideoItem()
            self._video_scene.addItem(self._video_item)

            self._video_audio = QAudioOutput()
            self._video_audio.setMuted(True)
            self._video_player = QMediaPlayer()
            self._video_player.setVideoOutput(self._video_item)
            # Keep the picture filling the view as the window is resized.
            self._video_item.nativeSizeChanged.connect(self._fit_video)
            self._video_player.setAudioOutput(self._video_audio)
            self._video_player.errorOccurred.connect(self._on_video_error)
            # Where the video is, kept on the Qt thread so video_state() can be
            # read from any thread without touching the player.
            self._video_player.positionChanged.connect(self._on_video_position)
            self._video_player.durationChanged.connect(self._on_video_duration)
            self._video_player.playbackStateChanged.connect(self._on_video_playback)

            # A SECOND player, for sound that arrives separately.
            #
            # YouTube no longer serves a single stream carrying both picture and
            # sound — not for new uploads and not for old ones; checked against
            # three videos including the oldest on the site, and every one of
            # them offered zero combined formats. Picture and sound are two
            # URLs, so they are two players, started together and nudged back
            # into line by the timer below.
            #
            # A local file or a direct video URL still uses the first player
            # alone: one stream, one player, nothing to synchronise.
            self._video_sound = QMediaPlayer()
            self._video_sound_out = QAudioOutput()
            self._video_sound_out.setMuted(True)
            self._video_sound.setAudioOutput(self._video_sound_out)

            self._video_sync = QTimer(self)
            self._video_sync.setInterval(1000)
            self._video_sync.timeout.connect(self._sync_video_sound)
        else:
            # Pictures still work; only the moving ones are missing, and the
            # open path says so when one is actually asked for.
            self._video_item = None
            self._video_player = None
            self._video_audio = None
            self._video_sound = None
            self._video_sound_out = None
            self._video_sync = None

        # Universal Plugin HUD Display Host (Page 3 of stack)
        self._plugin_hud_host = PluginHudHost(on_close=self._on_plugin_hud_closed_by_ui)
        self._hud_cam_prev_index = 0

        # Stack: 0 = animated HUD, 1 = live camera, 2 = video, 3 = universal plugin display
        self._hud_cam_stack = QStackedWidget()
        self._hud_cam_stack.addWidget(self.hud)
        self._hud_cam_stack.addWidget(_cam_cont)
        self._hud_cam_stack.addWidget(self._video_cont)
        self._hud_cam_stack.addWidget(self._plugin_hud_host)

        self._center_split = QSplitter(Qt.Orientation.Vertical)
        self._center_split.setHandleWidth(10)
        self._center_split.setStyleSheet(f"""
            QSplitter::handle {{ background: transparent; }}
            QSplitter::handle:hover {{ background: {C.PRI_GHO}; }}
        """)
        self._center_split.addWidget(self._hud_cam_stack)
        self._center_split.addWidget(self._content_panel)
        self._center_split.addWidget(self._quiz_panel)
        self._center_split.setStretchFactor(0, 3)
        self._center_split.setStretchFactor(1, 1)
        self._center_split.setCollapsible(0, False)
        body.addWidget(self._center_split, stretch=5)

        self._right_panel = self._build_right_panel()
        body.addWidget(self._right_panel, stretch=0)

        root.addLayout(body, stretch=1)
        root.addWidget(self._build_footer())

        # Quick-access drawer (floating overlay, built after central widget layout is done)
        self._quick_drawer = self._build_quick_drawer()
        self._ctrl_drawer = self._build_controls_drawer()
        self._warm_wake_state()
        self._update_autostart_btn(self._check_autostart())
        from memory.config_manager import get_brief_enabled as _gbe
        self._update_brief_btn(_gbe())

        self._log._ai_name_lc = self._assistant_name.lower()

        self._clock_tmr = QTimer(self)
        self._clock_tmr.timeout.connect(self._tick_clock)
        self._clock_tmr.start(1000)
        self._tick_clock()

        # Metric update timer
        self._metric_tmr = QTimer(self)
        self._metric_tmr.timeout.connect(self._update_metrics)
        self._metric_tmr.start(2000)
        self._update_metrics()

        self._log_sig.connect(self._log.append_log)
        self._state_sig.connect(self._apply_state)
        self._content_sig.connect(self._show_content)
        self._codejob_sig.connect(self._show_code_job)
        self._reconfig_sig.connect(self._show_setup)
        self._camera_sig.connect(self._show_camera_frame)
        self._confirm_sig.connect(self._show_confirm_banner)
        self._confirm_hide_sig.connect(self._hide_confirm_banner)
        self._cam_stream_sig.connect(self._on_cam_stream)
        self._cam_frame_sig.connect(self._on_cam_frame)
        self._wake_btns_sig.connect(self._refresh_wake_btns)
        self._video_open_sig.connect(self._on_video_open)
        self._image_open_sig.connect(self._on_image_open)
        self._video_close_sig.connect(self._on_video_close)
        self._video_mute_sig.connect(self._on_video_mute)
        self._video_seek_sig.connect(self._on_video_seek)
        self._video_pause_sig.connect(self._on_video_pause)
        self._clipboard_sig.connect(self._show_clipboard_panel)
        self._wake_dl_sig.connect(self._on_wake_install_done)
        self._quiz_sig.connect(self._show_quiz)
        self._quiz_hide_sig.connect(self._hide_quiz)
        self._review_sig.connect(self._show_review)
        self._plugin_hud_open_sig.connect(self._on_plugin_hud_open)
        self._plugin_hud_update_sig.connect(self._on_plugin_hud_update)
        self._plugin_hud_close_sig.connect(self._on_plugin_hud_close)
        self._cam_stop = threading.Event()

        # Camera preview overlay (child of central widget, positioned in resizeEvent)
        self._cam_preview = _CameraPreview(self.centralWidget())

        # Clipboard panel (child of central widget, bottom-center)
        self._clipboard_panel = ClipboardPanel(self.centralWidget())
        self._clipboard_panel.action_requested.connect(self._on_clipboard_action)
        QApplication.clipboard().dataChanged.connect(self._on_clipboard_changed)

        self._overlay: SetupOverlay | None = None
        self._ready = self._check_config()
        if not self._ready:
            self._show_setup()

        sc_mute = QShortcut(QKeySequence("F4"), self)
        sc_mute.activated.connect(self._toggle_mute)
        sc_full = QShortcut(QKeySequence("F11"), self)
        sc_full.activated.connect(self._toggle_fullscreen)
        sc_intr = QShortcut(QKeySequence("Escape"), self)
        sc_intr.activated.connect(self._do_interrupt)

    def _show_camera_frame(self, img_bytes: bytes):
        """Slot — display camera preview overlay (main thread)."""
        self._cam_preview.show_frame(img_bytes)
        cw = self.centralWidget()
        pw = _CameraPreview._W
        ph = self._cam_preview.height()
        self._cam_preview.setGeometry(
            cw.width() - self._right_panel.width() - pw - 22,
            cw.height() - ph - 28,
            pw, ph,
        )

    # --- Live camera stream in HUD area ------------------------------------
    def _on_cam_stream(self, start: bool) -> None:
        if start:
            self._hud_cam_prev_index = self._hud_cam_stack.currentIndex()
            self._hud_cam_stack.setCurrentIndex(1)
        else:
            prev = getattr(self, "_hud_cam_prev_index", 0)
            if prev == 3 and getattr(self, "_plugin_hud_host", None) and self._plugin_hud_host.is_active():
                self._hud_cam_stack.setCurrentIndex(3)
            elif prev == 2 and self._video_on:
                self._hud_cam_stack.setCurrentIndex(2)
            else:
                self._hud_cam_stack.setCurrentIndex(0)
            self._cam_live_lbl.clear()

    def _on_cam_frame(self, data: bytes) -> None:
        px = QPixmap()
        px.loadFromData(data)
        if not px.isNull():
            w, h = self._cam_live_lbl.width(), self._cam_live_lbl.height()
            if w > 1 and h > 1:
                self._cam_live_lbl.setPixmap(
                    px.scaled(w, h,
                              Qt.AspectRatioMode.KeepAspectRatio,
                              Qt.TransformationMode.SmoothTransformation)
                )

    def start_camera_stream(self) -> None:
        self._cam_stop.clear()
        self._cam_stream_sig.emit(True)
        t = threading.Thread(target=self._cam_loop, daemon=True, name="cam-stream")
        t.start()

    def _cam_loop(self) -> None:
        try:
            import cv2
            # Reuse camera index detected by screen_processor (cached in api_keys.json)
            cam_idx = 0
            try:
                import json as _j
                cfg = _j.loads((CONFIG_DIR / "api_keys.json").read_text())
                cam_idx = int(cfg.get("camera_index", 0))
            except Exception:
                pass
            try:
                backend = cv2.CAP_DSHOW if _OS == "Windows" else cv2.CAP_ANY
            except AttributeError:
                backend = 0
            cap = cv2.VideoCapture(cam_idx, backend)
            if not cap.isOpened():
                cap = cv2.VideoCapture(0)
            if not cap.isOpened():
                return
            # warm-up frames
            for _ in range(5):
                cap.read()
            while not self._cam_stop.wait(0.033) and cap.isOpened():
                ret, frame = cap.read()
                if ret and frame is not None:
                    _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 65])
                    self._cam_frame_sig.emit(buf.tobytes())
            cap.release()
        except Exception as e:
            print(f"[Camera] Stream error: {e}")
        finally:
            self._cam_stream_sig.emit(False)

    def stop_camera_stream(self) -> None:
        self._cam_stop.set()

    # --- Video in the HUD area ---------------------------------------------
    #
    # Everything below runs on the Qt thread. Plugins and the assistant reach it
    # through the signals above, the same way every other panel is driven, so a
    # background thread never touches a widget.
    def _on_video_open(self, source: str, title: str, muted: bool,
                       audio_source: str = "") -> None:
        if not HAVE_VIDEO or not self._video_player:
            self._log.append_log("SYS: Video playback is not available in this Qt "
                           "install.")
            return
        # A video and the live camera cannot share the centre of the HUD.
        self._cam_stop.set()

        # A video's title is in its own language; left as written (see _show_content).
        self._video_title.setText(f"▶  {(title[:44] if title else 'VIDEO')}")
        self._video_split = bool(audio_source)
        self._video_kind = "video"
        self._video_pos_ms = self._video_dur_ms = 0
        self._video_paused = False
        # A picture may be on the surface; the two share it and only one can
        # be visible.
        self._image_item.setVisible(False)
        self._image_item.setPixmap(QPixmap())
        if self._video_item is not None:
            self._video_item.setVisible(True)
        self._video_mute_btn.setVisible(True)
        self._set_video_muted(bool(muted))

        url = (QUrl.fromLocalFile(source) if Path(source).exists()
               else QUrl(source))
        self._video_player.setSource(url)
        if self._video_split:
            self._video_sound.setSource(QUrl(audio_source))
        self._hud_cam_stack.setCurrentIndex(2)
        self._video_on = True
        # Re-run now that the video counts as playing: _set_video_muted ran
        # before this line and saw no video, so its mic check was a no-op.
        self._sync_mic_for_video()
        self._video_player.play()
        if self._video_split:
            self._video_sound.play()
            self._video_sync.start()

    def _on_image_open(self, path: str, title: str) -> None:
        """Put a still picture where the avatar sits.

        It shares the video's surface rather than adding a fourth page to the
        stack, for the reason the video shared the camera's: something takes
        the centre of the HUD for a while and then gives it back, and one
        mechanism for that cannot contradict itself. The close button, the
        stop command and the camera interlock all work on a picture already,
        because they were never about video.
        """
        pixmap = QPixmap(path)
        if pixmap.isNull():
            self._log.append_log(
                f"SYS: '{Path(path).name}' could not be shown — this Qt build "
                f"does not read that image format."
            )
            return

        self._cam_stop.set()            # the camera cannot share the centre
        if self._video_sync:
            self._video_sync.stop()
        for p in (self._video_player, self._video_sound):
            if p:
                p.stop()
                p.setSource(QUrl())
        self._video_split = False
        self._video_kind = "image"
        # Silence first: a video whose sound was on has the microphone closed,
        # and the picture replacing it must hand the microphone back.
        self._set_video_muted(True)
        self._video_mute_btn.setVisible(False)

        if self._video_item is not None:
            self._video_item.setVisible(False)
        self._image_item.setPixmap(pixmap)
        self._image_item.setVisible(True)
        self._video_title.setText(f"◧  {(title[:44] if title else 'PICTURE')}")
        self._hud_cam_stack.setCurrentIndex(2)
        self._video_on = True
        self._sync_mic_for_video()
        self._fit_video()

    def _fit_video(self, *_a) -> None:
        """Size whatever is on the surface to the panel, keeping its shape."""
        if not self._video_widget:
            return
        try:
            showing_image = self._image_item.isVisible()
            item = self._image_item if showing_image else self._video_item
            if item is None:
                return
            native = (QSizeF(self._image_item.pixmap().size()) if showing_image
                      else self._video_item.nativeSize())
            if native.isEmpty():
                return
            view = self._video_widget.viewport().size()
            scale = min(view.width() / native.width(),
                        view.height() / native.height())
            if showing_image:
                # A pixmap item has no setSize, so it is scaled instead — and
                # capped, because a thumbnail blown up to fill the panel looks
                # broken rather than large.
                item.setScale(min(scale, 2.0))
            else:
                item.setSize(QSizeF(native.width() * scale,
                                    native.height() * scale))
            self._video_scene.setSceneRect(item.sceneBoundingRect())
            self._video_widget.centerOn(item)
        except Exception:
            pass

    def _on_video_close(self) -> None:
        if self._video_sync:
            self._video_sync.stop()
        for p in (self._video_player, self._video_sound):
            if p:
                p.stop()
                p.setSource(QUrl())
        self._video_split = False
        self._video_on = False
        self._video_kind = ""
        self._image_item.setVisible(False)
        self._image_item.setPixmap(QPixmap())   # a full-resolution photo is MB
        if self._video_item is not None:
            self._video_item.setVisible(True)
        self._video_mute_btn.setVisible(True)
        if getattr(self, "_plugin_hud_host", None) and self._plugin_hud_host.is_active():
            self._hud_cam_stack.setCurrentIndex(3)
        else:
            self._hud_cam_stack.setCurrentIndex(0)
        self._sync_mic_for_video()      # gives the microphone back

    def _set_video_muted(self, muted: bool) -> None:
        """Silence whichever output is carrying the sound for this video."""
        muted = bool(muted)
        if self._video_audio:
            # When the sound is a separate stream this player has none, but
            # muting it too costs nothing and keeps the two paths identical.
            self._video_audio.setMuted(muted)
        if self._video_sound_out:
            self._video_sound_out.setMuted(muted)
        self._sync_video_mute_btn()
        self._sync_mic_for_video()

    def _on_video_mute(self, muted: bool) -> None:
        self._set_video_muted(muted)

    def _sync_mic_for_video(self) -> None:
        """Close the microphone while the video is making sound.

        JARVIS subtracts its OWN output from the microphone — that is what
        core/echo.py does — but a video plays through a different output
        entirely, so the guard has never heard of it and the assistant answers
        the film. There is no arrangement in which an open microphone and a
        loudspeaker in the same room do not do that.

        So the microphone closes for exactly as long as the sound is on, and
        opens again by itself the moment it goes off or the video is closed.
        Only if JARVIS closed it: a microphone the user muted themselves stays
        muted, and pressing the mute key during a video hands the decision back
        to them for good.
        """
        sound_on = bool(self._video_on and self._video_sound_out
                        and not self._video_sound_out.isMuted())
        if sound_on and not self._muted:
            self._video_auto_muted = True
            self._set_muted(True, "The video's sound is on — silence it, close "
                                  "it, or press F4 to talk. Typing still works.")
        elif not sound_on and self._video_auto_muted:
            self._video_auto_muted = False
            self._set_muted(False, "The video is quiet again.")

    def _sync_video_sound(self) -> None:
        """Keep the separate soundtrack in step with the picture.

        Two players started at the same moment do not stay together: they buffer
        independently, and measured on a real stream they were about half a
        second apart after nine seconds. So the sound is nudged back to the
        picture whenever it drifts far enough to hear — and only then, because
        correcting a smaller gap is itself audible.
        """
        if not (self._video_split and self._video_sound and self._video_player):
            return
        try:
            if self._video_player.playbackState() != QMediaPlayer.PlaybackState.PlayingState:
                return
            drift = self._video_sound.position() - self._video_player.position()
            if abs(drift) > 300:
                self._video_sound.setPosition(self._video_player.position())
        except Exception:
            pass

    def _sync_video_mute_btn(self) -> None:
        out = self._video_sound_out if self._video_split else self._video_audio
        muted = bool(out and out.isMuted())
        self._video_mute_btn.setText("🔇  SOUND OFF" if muted else "🔊  SOUND ON")

    def _toggle_video_mute(self) -> None:
        out = self._video_sound_out if self._video_split else self._video_audio
        if out:
            self._set_video_muted(not out.isMuted())

    def _on_video_error(self, *_a) -> None:
        err = ""
        try:
            err = self._video_player.errorString()
        except Exception:
            pass
        self._log.append_log(f"SYS: The video could not be played{(' — ' + err) if err else ''}.")
        self._on_video_close()

    def stop_video(self) -> None:
        self._video_close_sig.emit()

    def video_is_playing(self) -> bool:
        return bool(self._video_on)

    # ── position and pause, asked for by voice ───────────────────────────────
    # "Skip ahead 30 seconds", "go to 1:20", "pause it": the player knows exactly
    # where it is, so these are exact — unlike a seek sent as arrow keys to some
    # other application.

    def _on_video_position(self, ms: int) -> None:
        self._video_pos_ms = int(ms)

    def _on_video_duration(self, ms: int) -> None:
        self._video_dur_ms = int(ms)

    def _on_video_playback(self, state) -> None:
        self._video_paused = state == QMediaPlayer.PlaybackState.PausedState

    def _on_video_seek(self, seconds: float, relative: bool) -> None:
        p = self._video_player
        if not (HAVE_VIDEO and p and self._video_on and getattr(self, "_video_kind", "") == "video"):
            return
        target = p.position() + seconds * 1000 if relative else seconds * 1000
        duration = p.duration()
        if duration > 0:
            target = min(target, max(0, duration - 500))
        target = int(max(0, target))
        p.setPosition(target)
        if self._video_split and self._video_sound:
            self._video_sound.setPosition(target)
        self._video_pos_ms = target

    def _on_video_pause(self, paused: bool) -> None:
        if not (HAVE_VIDEO and self._video_player and self._video_on
                and getattr(self, "_video_kind", "") == "video"):
            return
        players = [self._video_player] + ([self._video_sound] if self._video_split and self._video_sound else [])
        for player in players:
            player.pause() if paused else player.play()
        if self._video_split and self._video_sync:
            if paused:
                self._video_sync.stop()
            else:
                self._video_sync.start()

    def video_state(self) -> dict:
        """What is on the display: kind ('video' | 'image' | ''), position and
        duration in seconds, paused. Safe to read from any thread."""
        kind = getattr(self, "_video_kind", "") if self._video_on else ""
        return {"kind": kind,
                "position": getattr(self, "_video_pos_ms", 0) / 1000.0,
                "duration": getattr(self, "_video_dur_ms", 0) / 1000.0,
                "paused": bool(getattr(self, "_video_paused", False))}

    # ------------------------------------------------------------------
    # Universal Plugin HUD Display Slots & State
    # ------------------------------------------------------------------
    def _on_plugin_hud_open(
        self,
        plugin_id: str,
        title: str,
        content: Any,
        content_type: str,
        replace: bool,
    ) -> None:
        """Slot — open or replace a plugin interface on the main central HUD."""
        if self._video_on:
            self._on_video_close()

        # If camera is currently streaming, let plugin prepare in background and set resume target
        if getattr(self, "_cam_live_lbl", None) and self._hud_cam_stack.currentIndex() == 1:
            self._hud_cam_prev_index = 3
            self._plugin_hud_host.show_view(plugin_id, title, content, content_type, replace=replace)
            return

        ok = self._plugin_hud_host.show_view(plugin_id, title, content, content_type, replace=replace)
        if ok:
            self._hud_cam_stack.setCurrentIndex(3)

    def _on_plugin_hud_update(
        self,
        plugin_id: str,
        content: Any,
        content_type: Optional[str],
    ) -> None:
        """Slot — update an existing plugin interface on the main central HUD."""
        self._plugin_hud_host.update_view(plugin_id, content, content_type)

    def _on_plugin_hud_close(self, plugin_id: str) -> None:
        """Slot — close a plugin interface and return the central HUD to the avatar."""
        self._plugin_hud_host.close_view(plugin_id)
        if not self._plugin_hud_host.is_active():
            if self._hud_cam_stack.currentIndex() == 3:
                self._hud_cam_stack.setCurrentIndex(0)

    def _on_plugin_hud_closed_by_ui(self, closed_owner: str) -> None:
        """Callback from host UI close button — restore the animated avatar."""
        if self._hud_cam_stack.currentIndex() == 3:
            self._hud_cam_stack.setCurrentIndex(0)

    def get_plugin_hud_state(self) -> dict:
        """Thread-safe snapshot of the central HUD display state."""
        if hasattr(self, "_plugin_hud_host") and self._plugin_hud_host:
            return self._plugin_hud_host.get_state_snapshot()
        return {
            "active": False,
            "plugin_id": None,
            "title": "",
            "content_type": None,
            "url": None,
            "is_visible": False,
            "is_loading": False,
            "has_error": False,
            "error_message": "",
            "owner": None,
            "has_webengine": False,
        }

    # ------------------------------------------------------------------
    # Icon generation — arc-reactor style, rendered with Pillow
    # ------------------------------------------------------------------
    @staticmethod
    def _build_jarvis_icon(out_path: Path) -> bool:
        """
        Render a JARVIS arc-reactor icon at 4× resolution and downsample
        for crisp results at all sizes. Saves a multi-res .ico to out_path.
        Returns True on success.
        """
        try:
            import math
            import PIL.Image
            import PIL.ImageDraw
            import PIL.ImageFilter
        except ImportError:
            return False

        CYAN   = (0, 212, 255)
        DIM    = (0, 100, 140)
        DARK   = (0, 6, 10)
        GLOW   = (0, 160, 200)
        WHITE  = (220, 240, 255)

        def _render(sz: int) -> PIL.Image.Image:
            S  = sz * 4                     # draw at 4× then downscale
            img = PIL.Image.new("RGBA", (S, S), (0, 0, 0, 0))
            d   = PIL.ImageDraw.Draw(img)
            cx = cy = S // 2

            # ── filled background circle ──────────────────────────────────
            R = S // 2 - 2
            d.ellipse([cx-R, cy-R, cx+R, cy+R], fill=(*DARK, 255))

            # ── outer border ring ─────────────────────────────────────────
            lw = max(2, S // 40)
            d.ellipse([cx-R, cy-R, cx+R, cy+R],
                      outline=(*CYAN, 220), width=lw)

            # ── mid decorative ring ───────────────────────────────────────
            R2 = int(R * 0.72)
            d.ellipse([cx-R2, cy-R2, cx+R2, cy+R2],
                      outline=(*DIM, 180), width=max(1, lw // 2))

            # ── 6 radial spokes (hex bolt) ────────────────────────────────
            R_inner = int(R * 0.30)
            R_outer = int(R * 0.62)
            spoke_w = max(1, S // 80)
            for i in range(6):
                angle = math.radians(i * 60 - 30)
                x1 = cx + int(R_inner * math.cos(angle))
                y1 = cy + int(R_inner * math.sin(angle))
                x2 = cx + int(R_outer * math.cos(angle))
                y2 = cy + int(R_outer * math.sin(angle))
                d.line([x1, y1, x2, y2], fill=(*GLOW, 200), width=spoke_w)

            # ── 6 tick marks on outer ring ────────────────────────────────
            for i in range(6):
                angle = math.radians(i * 60)
                for dr in range(lw * 2):
                    rx = (R - lw - dr)
                    d.point(
                        [cx + int(rx * math.cos(angle)),
                         cy + int(rx * math.sin(angle))],
                        fill=(*WHITE, 220),
                    )

            # ── inner glowing ring ────────────────────────────────────────
            Ri = int(R * 0.26)
            d.ellipse([cx-Ri, cy-Ri, cx+Ri, cy+Ri],
                      outline=(*CYAN, 255), width=max(2, lw))

            # ── bright glow soft blur applied before core ─────────────────
            # (draw a slightly larger cyan circle on a separate layer)
            glow_layer = PIL.Image.new("RGBA", (S, S), (0, 0, 0, 0))
            gd = PIL.ImageDraw.Draw(glow_layer)
            Rc = int(R * 0.13)
            gd.ellipse([cx-Rc*2, cy-Rc*2, cx+Rc*2, cy+Rc*2],
                       fill=(*CYAN, 110))
            glow_layer = glow_layer.filter(PIL.ImageFilter.GaussianBlur(S // 14))
            img = PIL.Image.alpha_composite(img, glow_layer)
            d   = PIL.ImageDraw.Draw(img)

            # ── core dot ──────────────────────────────────────────────────
            d.ellipse([cx-Rc, cy-Rc, cx+Rc, cy+Rc], fill=(*WHITE, 255))

            # ── downscale to target size ──────────────────────────────────
            return img.resize((sz, sz), PIL.Image.LANCZOS)

        try:
            sizes  = [256, 128, 64, 48, 32, 16]
            frames = [_render(s) for s in sizes]
            frames[0].save(
                out_path,
                format="ICO",
                append_images=frames[1:],
                sizes=[(s, s) for s in sizes],
            )
            return True
        except Exception as e:
            print(f"[Shortcut] ⚠️  Icon generation failed: {e}")
            return False

    @staticmethod
    def _create_lnk_windows(lnk: str, target: str, args: str,
                             work_dir: str, icon_loc: str) -> None:
        """
        Create a Windows .lnk shortcut WITHOUT launching PowerShell or cmd.
        Tries win32com (pywin32) first; falls back to wscript.exe + VBScript.
        wscript.exe is a GUI-mode host — it never opens a console window.
        """
        # ── Option 1: pywin32 (pure Python COM, zero subprocess) ──────────
        try:
            from win32com.client import Dispatch   # type: ignore
            sh = Dispatch("WScript.Shell")
            sc = sh.CreateShortCut(lnk)
            sc.TargetPath       = target
            sc.Arguments        = f'"{args}"'
            sc.WorkingDirectory = work_dir
            sc.Description      = "J.A.R.V.I.S AI Assistant"
            sc.IconLocation     = icon_loc
            sc.save()
            return
        except ImportError:
            pass

        # ── Option 2: wscript.exe + VBScript (always available on Windows,
        #    GUI-mode executable — never opens a console window) ────────────
        vbs = "\n".join([
            'Set ws = CreateObject("WScript.Shell")',
            f'Set sc = ws.CreateShortcut("{lnk}")',
            f'sc.TargetPath = "{target}"',
            f'sc.Arguments = Chr(34) & "{args}" & Chr(34)',
            f'sc.WorkingDirectory = "{work_dir}"',
            'sc.Description = "J.A.R.V.I.S AI Assistant"',
            f'sc.IconLocation = "{icon_loc}"',
            'sc.Save',
        ])
        import tempfile
        fd, tmp = tempfile.mkstemp(suffix=".vbs")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(vbs)
            proc = subprocess.Popen(
                ["wscript.exe", "/nologo", tmp],
                creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW,
            )
            proc.wait(timeout=10)
        finally:
            try:
                os.unlink(tmp)
            except Exception:
                pass

    @staticmethod
    def _get_desktop_dir() -> Path:
        """
        Resolve the user's REAL desktop directory instead of assuming
        ~/Desktop, which breaks when:
          • OneDrive "Known Folder Move" relocates the desktop
            (C:/Users/x/OneDrive/Desktop) — very common on Win 10/11;
          • the XDG desktop is localized on Linux (~/Masaüstü,
            ~/Schreibtisch, ~/Bureau, …).
        Falls back to ~/Desktop only as a last resort.
        """
        home = Path.home()
        _os = platform.system()

        if _os == "Windows":
            # ── 1) SHGetKnownFolderPath(FOLDERID_Desktop) — the canonical
            #       answer; follows OneDrive redirection. No dependencies. ──
            try:
                import ctypes
                from ctypes import wintypes

                class _GUID(ctypes.Structure):
                    _fields_ = [("Data1", wintypes.DWORD),
                                ("Data2", wintypes.WORD),
                                ("Data3", wintypes.WORD),
                                ("Data4", ctypes.c_ubyte * 8)]

                # FOLDERID_Desktop {B4BFCC3A-DB2C-424C-B029-7FE99A87C641}
                fid = _GUID(0xB4BFCC3A, 0xDB2C, 0x424C,
                            (ctypes.c_ubyte * 8)(0xB0, 0x29, 0x7F, 0xE9,
                                                 0x9A, 0x87, 0xC6, 0x41))
                buf = ctypes.c_wchar_p()
                if ctypes.windll.shell32.SHGetKnownFolderPath(
                        ctypes.byref(fid), 0, None, ctypes.byref(buf)) == 0:
                    p = Path(buf.value)
                    ctypes.windll.ole32.CoTaskMemFree(buf)
                    if p.is_dir():
                        return p
            except Exception:
                pass

            # ── 2) Registry: User Shell Folders (may contain %VARS%) ──────
            try:
                import winreg
                with winreg.OpenKey(
                        winreg.HKEY_CURRENT_USER,
                        r"Software\Microsoft\Windows\CurrentVersion"
                        r"\Explorer\User Shell Folders") as key:
                    val, _t = winreg.QueryValueEx(key, "Desktop")
                p = Path(os.path.expandvars(val))
                if p.is_dir():
                    return p
            except Exception:
                pass

        elif _os == "Linux":
            # ── xdg-user-dir honours localized names (~/Masaüstü, …) ──────
            try:
                out = subprocess.run(["xdg-user-dir", "DESKTOP"],
                                     capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5)
                p = Path(out.stdout.strip())
                if out.stdout.strip() and p != home and p.is_dir():
                    return p
            except Exception:
                pass
            try:
                cfg = home / ".config" / "user-dirs.dirs"
                for line in cfg.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line.startswith("XDG_DESKTOP_DIR"):
                        val = line.split("=", 1)[1].strip().strip('"')
                        p = Path(val.replace("$HOME", str(home)))
                        if p != home and p.is_dir():
                            return p
            except Exception:
                pass

        # macOS: ~/Desktop is always the real path (localization is
        # display-only). Everything else lands here as a last resort.
        return home / "Desktop"

    def _create_desktop_shortcut(self):
        """
        Create a desktop shortcut on Windows / macOS / Linux.
        Never opens a terminal, console, or PowerShell window on any platform.
        """
        import stat as _stat
        script  = Path(__file__).resolve().parent.parent / "main.py"
        python  = Path(sys.executable)
        desktop = self._get_desktop_dir()

        # Arc-reactor icon (.ico — also exported as .png for Linux/macOS)
        ico_path = Path(__file__).resolve().parent.parent / "config" / "jarvis.ico"
        if not ico_path.exists():
            self._build_jarvis_icon(ico_path)

        try:
            _os = platform.system()

            # ── Windows ───────────────────────────────────────────────────────
            if _os == "Windows":
                pythonw  = python.parent / "pythonw.exe"
                target   = str(pythonw if pythonw.exists() else python)
                lnk      = str(desktop / "J.A.R.V.I.S.lnk")
                icon_loc = str(ico_path) if ico_path.exists() else f"{target},0"
                self._create_lnk_windows(lnk, target, str(script),
                                         str(script.parent), icon_loc)

            # ── macOS — proper .app bundle (no Terminal window) ───────────────
            elif _os == "Darwin":
                app     = desktop / "J.A.R.V.I.S.app"
                mac_dir = app / "Contents" / "MacOS"
                res_dir = app / "Contents" / "Resources"
                mac_dir.mkdir(parents=True, exist_ok=True)
                res_dir.mkdir(exist_ok=True)

                # Launcher executable (bash — runs as background process,
                # macOS does NOT open Terminal for executables inside .app bundles)
                launcher = mac_dir / "JARVIS"
                launcher.write_text(
                    "#!/usr/bin/env bash\n"
                    f'cd "{script.parent}"\n'
                    f'exec "{python}" "{script}"\n'
                )
                launcher.chmod(launcher.stat().st_mode
                               | _stat.S_IEXEC | _stat.S_IXGRP | _stat.S_IXOTH)

                # Minimal Info.plist (required for .app recognition)
                (app / "Contents" / "Info.plist").write_text(
                    '<?xml version="1.0" encoding="UTF-8"?>\n'
                    '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
                    '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
                    '<plist version="1.0"><dict>\n'
                    '  <key>CFBundleExecutable</key><string>JARVIS</string>\n'
                    '  <key>CFBundleIdentifier</key>'
                    '<string>com.jarvis.assistant</string>\n'
                    '  <key>CFBundleName</key><string>J.A.R.V.I.S</string>\n'
                    '  <key>CFBundlePackageType</key><string>APPL</string>\n'
                    '  <key>CFBundleVersion</key><string>1.0</string>\n'
                    '</dict></plist>\n'
                )

                # Optional: copy icon as .icns (skip silently if Pillow is missing)
                try:
                    import PIL.Image
                    icns = res_dir / "AppIcon.icns"
                    PIL.Image.open(ico_path).save(icns, format="ICNS")
                    # Inject icon reference into plist
                    plist = app / "Contents" / "Info.plist"
                    txt = plist.read_text()
                    plist.write_text(
                        txt.replace(
                            '</dict></plist>',
                            '  <key>CFBundleIconFile</key>'
                            '<string>AppIcon</string>\n</dict></plist>\n',
                        )
                    )
                except Exception:
                    pass  # icon is optional

            # ── Linux — .desktop file (Terminal=false, no console) ────────────
            else:
                # Export .ico → .png for better desktop integration
                png_path = ico_path.with_suffix(".png")
                if not png_path.exists() and ico_path.exists():
                    try:
                        import PIL.Image
                        PIL.Image.open(ico_path).resize(
                            (256, 256), PIL.Image.LANCZOS
                        ).save(png_path, format="PNG")
                    except Exception:
                        png_path = ico_path  # fallback to .ico

                icon_line = f"Icon={png_path}\n" if png_path.exists() else ""
                desk = desktop / "J.A.R.V.I.S.desktop"
                desk.write_text(
                    "[Desktop Entry]\n"
                    "Name=J.A.R.V.I.S\n"
                    f"Exec={python} {script}\n"
                    f"Path={script.parent}\n"
                    "Type=Application\n"
                    "Terminal=false\n"
                    "Categories=Utility;\n"
                    + icon_line
                )
                desk.chmod(desk.stat().st_mode | 0o755)

            self._log.append_log("SYS: Desktop shortcut created.")
        except Exception as e:
            self._log.append_log(f"ERR: Shortcut failed — {e}")

    def _toggle_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_side_columns()
        cw = self.centralWidget()
        if self._overlay and self._overlay.isVisible():
            ow, oh = 460, 390
            self._overlay.setGeometry(
                (cw.width()  - ow) // 2,
                (cw.height() - oh) // 2,
                ow, oh,
            )
        if self._remote_overlay and self._remote_overlay.isVisible():
            ow, oh = RemoteKeyOverlay._OW, RemoteKeyOverlay._OH
            self._remote_overlay.setGeometry(
                (cw.width()  - ow) // 2,
                (cw.height() - oh) // 2,
                ow, oh,
            )
        if self._customize_overlay and self._customize_overlay.isVisible():
            ow, oh = CustomizeOverlay._OW, CustomizeOverlay._OH
            self._customize_overlay.setGeometry(
                (cw.width()  - ow) // 2,
                (cw.height() - oh) // 2,
                ow, oh,
            )
        # Camera preview — bottom-right corner of the center/HUD area
        pw = _CameraPreview._W
        ph = self._cam_preview.height() or _CameraPreview._H
        self._cam_preview.setGeometry(
            cw.width() - self._right_panel.width() - pw - 22,
            cw.height() - ph - 28,
            pw, ph,
        )
        # Clipboard panel — bottom-center
        if hasattr(self, '_clipboard_panel') and self._clipboard_panel.isVisible():
            self._position_clipboard_panel()
        # Quick drawer — reposition if open
        if hasattr(self, '_quick_drawer') and self._quick_drawer.isVisible():
            self._position_quick_drawer()
        if hasattr(self, '_ctrl_drawer') and self._ctrl_drawer.isVisible():
            self._position_ctrl_drawer()
        self._fit_video()

    def closeEvent(self, event):
        try:
            if hasattr(self, "_plugin_hud_host") and self._plugin_hud_host:
                self._plugin_hud_host.cleanup()
        except Exception:
            pass
        super().closeEvent(event)

    def _update_metrics(self):
        snap = _metrics.snapshot()

        cpu, mem = snap["cpu"], snap["mem"]
        self._g_cpu.set_value(cpu, f"{cpu:.0f}%")
        self._g_mem.set_value(mem, f"{mem:.0f}%")

        gpu = snap["gpu"]
        if gpu >= 0:
            self._g_gpu.set_value(gpu, f"{gpu:.0f}%")
        else:
            self._g_gpu.set_value(0, "N/A")

        tmp = snap["tmp"]
        if tmp >= 0:
            self._g_tmp.set_value(min(100.0, tmp), f"{tmp:.0f}°")
        else:
            self._g_tmp.set_value(0, "N/A")

        # NET in MB/s: the graph scales itself, the panel header carries the figure
        net = snap["net"]
        self._net_spark.push(net)
        rate = f"{net * 1024:.0f} KB/s" if net < 1.0 else f"{net:.1f} MB/s"
        self._net_panel.set_code(rate)
        self._row_net.set_value(rate)

        try:
            elapsed = time.time() - psutil.boot_time()
            h = int(elapsed // 3600)
            m = int((elapsed % 3600) // 60)
            self._row_uptime.set_value(f"{h:02d}:{m:02d}", "GREEN")
        except Exception:
            self._row_uptime.set_value("--:--", "GREEN")

        try:
            self._row_proc.set_value(str(len(psutil.pids())))
        except Exception:
            self._row_proc.set_value("--")

    def _build_header(self) -> QWidget:
        w = HeaderBar()
        w.setFixedHeight(58)
        self._header = w
        lay = QHBoxLayout(w)
        lay.setContentsMargins(14, 6, 14, 8)
        lay.setSpacing(10)
        lay.addWidget(ArcLogo())

        name_col = QVBoxLayout()
        name_col.setSpacing(0)
        display = self._assistant_name.upper()
        self._title_lbl = QLabel(display)
        self._title_lbl.setFont(px_font(19, QFont.Weight.Light, spacing=3.5))
        self._title_lbl.setStyleSheet(f"color: {C.WHITE}; background: transparent;")
        self._title_lbl.setToolTip(self._subtitle(display))
        name_col.addWidget(self._title_lbl)
        self._sub_lbl = QLabel(f"{APP_VERSION}  ·  PERSONAL AI SYSTEM")
        self._sub_lbl.setFont(px_font(9, QFont.Weight.DemiBold, spacing=2.2))
        self._sub_lbl.setStyleSheet(f"color: {C.PRI_DIM}; background: transparent;")
        name_col.addWidget(self._sub_lbl)
        lay.addLayout(name_col)
        lay.addSpacing(8)

        # Two drawers, and the split is by how often a thing is touched: the
        # ones you set once behind the gear, the everyday switches behind the
        # sliders. Separating them is what keeps each list short enough to
        # read at a glance.
        self._drawer_btn = IconButton("gear", 32)
        self._drawer_btn.setCheckable(True)
        self._drawer_btn.setToolTip("Setup — things you set once")
        self._drawer_btn.clicked.connect(self._toggle_drawer)
        lay.addWidget(self._drawer_btn)
        self._ctrl_btn = IconButton("sliders", 32)
        self._ctrl_btn.setCheckable(True)
        self._ctrl_btn.setToolTip("Controls — the everyday switches")
        self._ctrl_btn.clicked.connect(self._toggle_controls)
        lay.addWidget(self._ctrl_btn)
        lay.addStretch()

        self._state_chip = StateChip()
        lay.addWidget(self._state_chip)
        lay.addStretch()

        clock_col = QVBoxLayout()
        clock_col.setSpacing(0)
        self._clock_lbl = QLabel("00:00:00")
        self._clock_lbl.setFont(px_font(22, QFont.Weight.Light, mono=True, spacing=1))
        self._clock_lbl.setStyleSheet(f"color: {C.WHITE}; background: transparent;")
        self._clock_lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
        clock_col.addWidget(self._clock_lbl)
        self._date_lbl = QLabel("")
        self._date_lbl.setFont(px_font(9, QFont.Weight.DemiBold, spacing=1.6))
        self._date_lbl.setStyleSheet(f"color: {C.PRI_DIM}; background: transparent;")
        self._date_lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
        clock_col.addWidget(self._date_lbl)
        lay.addLayout(clock_col)
        lay.addSpacing(4)
        self._fs_btn = IconButton("expand", 32)
        self._fs_btn.setToolTip("Fullscreen  [F11]")
        self._fs_btn.clicked.connect(self._toggle_fullscreen)
        lay.addWidget(self._fs_btn)
        return w

    def _tick_clock(self):
        self._clock_lbl.setText(time.strftime("%H:%M:%S"))
        self._date_lbl.setText(time.strftime("%a %d %b %Y"))

    @staticmethod
    def _subtitle(display: str) -> str:
        return ("Just A Rather Very Intelligent System"
                if display.replace(".", "") == "JARVIS" else "Personal AI Assistant")

    def _fit_side_columns(self) -> None:
        """Side columns grow with the window, within limits, so the centre keeps
        the space a larger window was opened for."""
        W = self.width()
        if hasattr(self, "_left_panel"):
            self._left_panel.setFixedWidth(max(190, min(250, int(W * 0.19))))
        if hasattr(self, "_right_panel"):
            self._right_panel.setFixedWidth(max(290, min(400, int(W * 0.27))))
        # The network graph is the first thing to give up its space: below this
        # height the gauges and the task list need it more, and the rate itself
        # is still in STATUS.
        if hasattr(self, "_net_panel"):
            self._net_panel.setVisible(self.height() >= 820)

    def _refresh_identity_rows(self) -> None:
        """Engine and voice, read from config — on start and whenever they change."""
        try:
            from memory.config_manager import get_provider_mode, get_voice
            mode = str(get_provider_mode() or "gemini")
            engine = "GEMINI LIVE" if mode == "gemini" else f"LOCAL {mode.upper()}"
            voice = str(get_voice() or "--").upper()
        except Exception:
            engine, voice = "GEMINI LIVE", "--"
        self._engine_label = engine
        if hasattr(self, "_row_engine"):
            self._row_engine.set_value(engine, "PRI")
            self._row_voice.set_value(voice)
        self._refresh_status_strip()

    def _refresh_status_strip(self) -> None:
        if not hasattr(self, "_status_strip"):
            return
        mic = ("MIC · MUTED", "MUTED_C") if self._muted else ("MIC · LIVE", "GREEN")
        self._status_strip.set_items([
            (f"ENGINE · {getattr(self, '_engine_label', 'GEMINI LIVE')}", "GREEN"),
            mic,
            (f"PROTOCOL · {APP_PROTOCOL}", "PRI"),
        ])

    def _build_left_panel(self) -> QWidget:
        w = QWidget()
        w.setStyleSheet("background: transparent;")
        w.setFixedWidth(200)
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)

        sysp = HoloPanel("SYSTEMS", "LIVE")
        grid_w = QWidget()
        grid_w.setStyleSheet("background: transparent;")
        grid = QGridLayout(grid_w)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(2)
        self._g_cpu = RingGauge("CPU")
        self._g_mem = RingGauge("MEMORY")
        self._g_gpu = RingGauge("GPU")
        self._g_tmp = RingGauge("TEMP")
        for i, g in enumerate((self._g_cpu, self._g_mem, self._g_gpu, self._g_tmp)):
            grid.addWidget(g, i // 2, i % 2)
        sysp.body.addWidget(grid_w)
        lay.addWidget(sysp)

        self._net_panel = HoloPanel("NETWORK", "--")
        self._net_spark = Sparkline(samples=60, floor=0.25)
        self._net_panel.body.addWidget(self._net_spark)
        lay.addWidget(self._net_panel)

        stp = HoloPanel("STATUS", "")
        os_name = {"Windows": "WINDOWS", "Darwin": "MACOS", "Linux": "LINUX"}.get(_OS, _OS.upper())
        self._row_uptime = DataRow("UPTIME", "--:--", "GREEN")
        self._row_proc = DataRow("PROCESSES", "--")
        self._row_net = DataRow("NETWORK", "--")
        self._row_os = DataRow("PLATFORM", os_name)
        self._row_engine = DataRow("ENGINE", "--", "PRI")
        self._row_voice = DataRow("VOICE", "--")
        for row in (self._row_uptime, self._row_proc, self._row_net, self._row_os,
                    self._row_engine, self._row_voice):
            stp.body.addWidget(row)
        lay.addWidget(stp)

        # Background tasks (core/tasks). Read from the engine every two seconds —
        # an in-memory list, no disk — and hidden rows cost nothing to paint.
        self._tasks_panel = HoloPanel("TASKS", "")
        rows_box = QWidget()
        rows_box.setStyleSheet("background: transparent;")
        rows_lay = QVBoxLayout(rows_box)
        rows_lay.setContentsMargins(0, 0, 4, 0)
        rows_lay.setSpacing(6)
        self._task_rows = []
        for _ in range(6):
            row = TaskRow()
            row.clicked.connect(self._open_task)
            row.hide()
            rows_lay.addWidget(row)
            self._task_rows.append(row)
        self._tasks_empty = QLabel("No background tasks yet.")
        self._tasks_empty.setFont(px_font(10))
        self._tasks_empty.setWordWrap(True)
        self._tasks_empty.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        rows_lay.addWidget(self._tasks_empty)
        rows_lay.addStretch()
        # Scrolls rather than grows: the column must fit an 580-pixel-high
        # window, and a task list that pushes the gauges into each other is
        # worse than one that scrolls.
        scroll = QScrollArea()
        scroll.setWidget(rows_box)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }" + scroll_css())
        scroll.viewport().setStyleSheet("background: transparent;")
        scroll.setMinimumHeight(24)

        # Routines (core/routines) share the panel, switched from its header: the
        # column has no room for another panel, and both lists answer the same
        # question — what is the assistant doing without being asked.
        from PyQt6.QtWidgets import QStackedWidget
        r_box = QWidget()
        r_box.setStyleSheet("background: transparent;")
        r_lay = QVBoxLayout(r_box)
        r_lay.setContentsMargins(0, 0, 4, 0)
        r_lay.setSpacing(6)
        self._routine_rows = []
        for _ in range(12):
            row = RoutineRow()
            row.clicked.connect(self._open_routine)
            row.toggled.connect(self._toggle_routine)
            row.hide()
            r_lay.addWidget(row)
            self._routine_rows.append(row)
        self._routines_empty = QLabel("No routines yet. Ask for one by voice — "
                                      "\"every weekday at 08:30, tell me the weather\".")
        self._routines_empty.setFont(px_font(10))
        self._routines_empty.setWordWrap(True)
        self._routines_empty.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        r_lay.addWidget(self._routines_empty)
        r_lay.addStretch()
        r_scroll = QScrollArea()
        r_scroll.setWidget(r_box)
        r_scroll.setWidgetResizable(True)
        r_scroll.setFrameShape(QFrame.Shape.NoFrame)
        r_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        r_scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }" + scroll_css())
        r_scroll.viewport().setStyleSheet("background: transparent;")
        r_scroll.setMinimumHeight(24)

        self._tasks_stack = QStackedWidget()
        self._tasks_stack.setStyleSheet("background: transparent;")
        self._tasks_stack.addWidget(scroll)
        self._tasks_stack.addWidget(r_scroll)
        self._tasks_panel.body.addWidget(self._tasks_stack)

        self._panel_switch = QPushButton("ROUTINES")
        self._panel_switch.setFixedSize(76, 20)
        self._panel_switch.setFont(px_font(9, QFont.Weight.DemiBold))
        self._panel_switch.setCursor(Qt.CursorShape.PointingHandCursor)
        self._panel_switch.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._panel_switch.setStyleSheet(btn_css("dim", align="center"))
        self._panel_switch.clicked.connect(self._flip_tasks_panel)
        self._tasks_panel.add_header_widget(self._panel_switch)
        lay.addWidget(self._tasks_panel, stretch=1)
        self._tasks_tmr = QTimer(self)
        self._tasks_tmr.timeout.connect(self._refresh_tasks)
        self._tasks_tmr.start(2000)
        self._refresh_identity_rows()
        return w

    def _flip_tasks_panel(self) -> None:
        to_routines = self._tasks_stack.currentIndex() == 0
        self._tasks_stack.setCurrentIndex(1 if to_routines else 0)
        self._tasks_panel.set_title("ROUTINES" if to_routines else "TASKS")
        self._panel_switch.setText("TASKS" if to_routines else "ROUTINES")
        self._refresh_tasks()

    def _refresh_routines(self) -> None:
        import time as _time
        try:
            from core.routines import desktop
            eng = desktop.engine()
            items = eng.list() if eng else []
        except Exception:
            eng, items = None, []

        def when(r: dict) -> str:
            try:
                text = eng.describe(r)
            except Exception:
                text = ""
            if r["trigger"]["type"] == "time" and r.get("next_ts") and r.get("enabled"):
                text = f"next {_time.strftime('%a %H:%M', _time.localtime(r['next_ts']))} · {text}"
            return text

        def order(r: dict):
            timed = r["trigger"]["type"] == "time"
            return (not r.get("enabled"), timed, float(r.get("next_ts") or 0) if timed else 0.0)

        items.sort(key=order)
        for i, row in enumerate(self._routine_rows):
            r = dict(items[i], _when=when(items[i])) if i < len(items) else None
            row.set_routine(r)
            row.setVisible(r is not None)
        self._routines_empty.setVisible(not items)
        on = sum(1 for r in items if r.get("enabled"))
        self._tasks_panel.set_code(f"{on} ON" if items else "")

    def _toggle_routine(self, rid: str, enabled: bool) -> None:
        try:
            from core.routines import desktop
            eng = desktop.engine()
            if eng is None:
                return
            r = eng.update(rid, enabled=bool(enabled))
            if r.get("builtin") == "briefing":
                # The MORNING BRIEF switch in the drawer tells the same story.
                desktop.mirror_brief_switch(r["enabled"])
                self._update_brief_btn(r["enabled"])
        except Exception as e:
            self._log.append_log(f"SYS: Routine not changed — {e}")
        self._refresh_routines()

    def _open_routine(self, rid: str) -> None:
        import time as _time
        try:
            from core.routines import desktop
            eng = desktop.engine()
            r = eng.get(rid) if eng else None
        except Exception:
            eng, r = None, None
        if not r:
            return
        stamp = lambda ts: _time.strftime("%Y-%m-%d %H:%M", _time.localtime(ts))  # noqa: E731
        lines = [r["name"], "", f"When: {eng.describe(r)}"]
        if r["trigger"]["type"] == "time" and r.get("next_ts") and r.get("enabled"):
            lines.append(f"Next: {stamp(r['next_ts'])}")
        what = {"say": "Says", "prompt": "Carries out", "task": "Background task"}.get(r["kind"])
        if what and r.get("text"):
            lines.append(f"{what}: {r['text']}")
        for key, value in (r.get("options") or {}).items():
            if isinstance(value, list):
                lines.append(f"{key}:")
                lines += [f"  · {v}" for v in value]
            elif value not in ("", None):
                lines.append(f"{key}: {value}")
        state = "on" if r.get("enabled") else "off"
        lines.append(f"State: {state}{' · held back in quiet hours' if r.get('quiet') else ''}"
                     f" · ran {int(r.get('runs') or 0)} times")
        if r.get("last_run"):
            lines.append(f"Last: {stamp(r['last_run'])} — {r.get('last_result') or ''}")
        lines += ["", "Change it by voice — \"move the briefing to 08:30\", "
                      "\"turn the check-in off\", \"remind me every Friday at 17:00\"."]
        self._show_content("ROUTINE", "\n".join(lines)[:4000])

    def _refresh_tasks(self) -> None:
        if getattr(self, "_tasks_stack", None) is not None and self._tasks_stack.currentIndex() == 1:
            self._refresh_routines()
            return
        try:
            from core.tasks import desktop
            eng = desktop.engine()
            tasks = eng.list(12) if eng else []
        except Exception:
            tasks = []
        active = [t for t in tasks if t["status"] in ("running", "waiting_quota", "queued", "scheduled")]
        shown = (active + [t for t in tasks if t not in active])[:len(self._task_rows)]
        for i, row in enumerate(self._task_rows):
            task = shown[i] if i < len(shown) else None
            row.set_task(task)
            row.setVisible(task is not None)
        self._tasks_empty.setVisible(not shown)
        self._tasks_panel.set_code(f"{len(active)} ACTIVE" if active else "")

    def _open_task(self, tid: str) -> None:
        try:
            from core.tasks import desktop
            eng = desktop.engine()
            t = eng.get(tid) if eng else None
        except Exception:
            t = None
        if not t:
            return
        if t["status"] == "done":
            body = f"{t['title']}\n\n{(t.get('result') or '').strip()}"
            if t.get("report"):
                body += f"\n\n— Full report: {t['report']}"
        else:
            head = (f"{t['title']}\n\n{t['status']} · {t['used']['seconds'] / 60:.1f}/"
                    f"{t['budget']['minutes']} min · {t['used']['rounds']} rounds")
            if t.get("error"):
                head += f"\n{t['error']}"
            lines = [f"· {e['text']}" for e in (t.get("events") or [])[-14:]
                     if e.get("kind") in ("progress", "tool", "note", "state", "finding")]
            body = head + "\n\n" + "\n".join(lines)
        self._show_content("BACKGROUND TASK", body[:4000])

    def _build_right_panel(self) -> QWidget:
        w = QWidget()
        w.setStyleSheet("background: transparent;")
        w.setFixedWidth(300)
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)

        comm = HoloPanel("COMMUNICATIONS", "LOG")
        self._log = LogWidget()
        comm.body.addWidget(self._log)
        lay.addWidget(comm, stretch=1)

        cmd = HoloPanel("COMMAND", "INPUT")
        self._drop_zone = FileDropZone()
        self._drop_zone.file_selected.connect(self._on_file_selected)
        cmd.body.addWidget(self._drop_zone)

        self._file_hint = QLabel("No file loaded")
        self._file_hint.setFont(px_font(10))
        self._file_hint.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        self._file_hint.setWordWrap(True)
        cmd.body.addWidget(self._file_hint)

        cmd.body.addWidget(self._build_input_row())

        row = QHBoxLayout()
        row.setSpacing(8)
        self._mute_btn = IconButton("mic", 38, "MIC ACTIVE", "GREEN")
        self._mute_btn.setToolTip("Mute or unmute the microphone  [F4]")
        self._mute_btn.clicked.connect(self._toggle_mute)
        self._style_mute_btn()
        row.addWidget(self._mute_btn, 1)
        self._interrupt_btn = IconButton("stop", 38, "INTERRUPT", "MUTED_C")
        self._interrupt_btn.setToolTip("Stop the assistant mid-sentence  [ESC]")
        self._interrupt_btn.clicked.connect(self._do_interrupt)
        row.addWidget(self._interrupt_btn, 1)
        cmd.body.addLayout(row)
        lay.addWidget(cmd)
        return w

    def _build_quick_drawer(self) -> QWidget:
        """Floating panel under the gear: the things you set once."""
        self._BTN_PRI, self._BTN_DIM = btn_css("primary"), btn_css("dim")
        w = HoloPanel("SETUP", "", parent=self.centralWidget())
        w.setObjectName("QuickDrawer")
        w.hide()
        lay = w.body
        lay.setSpacing(5)

        def _btn(text, slot, style=None):
            b = QPushButton(text)
            b.setFixedHeight(30)
            b.setFont(px_font(11, QFont.Weight.DemiBold, spacing=1.2))
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setStyleSheet(style or self._BTN_DIM)
            b.clicked.connect(slot)
            lay.addWidget(b)
            return b

        _btn("◉  REMOTE CONTROL", self._open_remote, self._BTN_PRI)
        _btn("⊞  CREATE DESKTOP SHORTCUT", self._create_desktop_shortcut)
        self._autostart_btn = _btn("◉  AUTO-START: OFF", self._toggle_autostart)
        _btn("⚙  CUSTOMISE ASSISTANT", self._open_customize)
        _btn("🎧  AUDIO DEVICES", self._open_audio_devices)
        _btn("🧠  MEMORY", self._open_memory_panel)
        _btn("🧩  PLUGINS", self._open_plugin_manager)
        _btn("⚙  PLUGIN SETTINGS", self._open_plugin_settings)
        # The button is built here with the rest of the drawer; the panel behind
        # it — and the whole providers package — is imported on the click. The
        # drawer used to stutter for a measured 2.104 seconds because a state
        # check imported openwakeword while it was being built, and adding a
        # second thing that does that would undo the fix rather than repeat it.
        # "&&": a single & in a button label is Qt's shortcut marker, and it
        # was being drawn as an underscore — "ENGINE _PROVIDERS".
        _btn("🔌  ENGINE && PROVIDERS", self._open_providers)

        w.adjustSize()
        return w

    def _build_controls_drawer(self) -> QWidget:
        """The switches that get flicked every day, in their own panel."""
        w = HoloPanel("CONTROLS", "", parent=self.centralWidget())
        w.setObjectName("QuickDrawer")
        w.hide()
        lay = w.body
        lay.setSpacing(5)

        def _row(button, style=None):
            button.setFixedHeight(30)
            button.setFont(px_font(11, QFont.Weight.DemiBold, spacing=1.2))
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            if style:
                button.setStyleSheet(style)
            lay.addWidget(button)
            return button

        fs_btn = _row(QPushButton("⛶  FULLSCREEN  [F11]"), self._BTN_DIM)
        fs_btn.clicked.connect(self._toggle_fullscreen)

        self._brief_btn = _row(QPushButton())
        self._brief_btn.clicked.connect(self._toggle_brief)

        self._wake_btn = _row(QPushButton())
        self._wake_btn.clicked.connect(self._toggle_wake_word)
        self._wake_sleep_btn = _row(QPushButton())
        self._wake_sleep_btn.clicked.connect(self._tap_wake_manual)
        # Neutral until the real state lands from the background warm-up.
        self._wake_btn.setText("🎙  WAKE WORD")
        self._wake_btn.setStyleSheet(self._BTN_DIM)
        self._wake_sleep_btn.hide()

        self._ptt_btn = _row(QPushButton())
        self._ptt_btn.clicked.connect(self._toggle_ptt)
        self._refresh_talk_btns()

        self._hud_btn = _row(QPushButton())
        self._hud_btn.clicked.connect(self._toggle_hud_style)
        self._refresh_hud_btn()

        w.adjustSize()
        return w

    def _warm_wake_state(self) -> None:
        """Work out the wake-word state off the UI thread, once.

        It used to be resolved when the drawer opened, and that made opening the
        drawer take two seconds the first time — measured at 2.10s, all of it
        `import openwakeword` dragging in onnxruntime behind it. The button
        count had nothing to do with it; the Qt thread was simply waiting on an
        import. Afterwards the same check costs about a millisecond, so it only
        ever needed to happen somewhere other than in front of the user.
        """
        def work():
            try:
                self._wake_state()
            except Exception:
                pass
            self._wake_btns_sig.emit()
        threading.Thread(target=work, daemon=True, name="wake-state-warm").start()

    def _toggle_drawer(self, checked: bool):
        if checked:
            self._close_controls()
            self._position_quick_drawer()
            self._quick_drawer.show()
            self._quick_drawer.raise_()
        else:
            self._quick_drawer.hide()

    def _toggle_controls(self, checked: bool):
        if checked:
            self._close_setup()
            self._refresh_wake_btns()   # cheap now: the state was warmed at boot
            self._position_ctrl_drawer()
            self._ctrl_drawer.show()
            self._ctrl_drawer.raise_()
        else:
            self._ctrl_drawer.hide()

    def _close_setup(self):
        if hasattr(self, "_quick_drawer"):
            self._quick_drawer.hide()
        if hasattr(self, "_drawer_btn"):
            self._drawer_btn.setChecked(False)

    def _close_controls(self):
        if hasattr(self, "_ctrl_drawer"):
            self._ctrl_drawer.hide()
        if hasattr(self, "_ctrl_btn"):
            self._ctrl_btn.setChecked(False)

    def _place_drawer(self, drawer, left: int):
        _W = 250
        drawer.setFixedWidth(_W)
        drawer.adjustSize()
        top = self._header.height() + 2 if hasattr(self, "_header") else 60
        drawer.setGeometry(left, top, _W, drawer.sizeHint().height())

    def _position_quick_drawer(self):
        if hasattr(self, "_quick_drawer"):
            self._place_drawer(self._quick_drawer, 12)

    def _position_ctrl_drawer(self):
        # Under its own header button rather than under the other one's.
        if hasattr(self, "_ctrl_drawer"):
            left = 12
            try:
                left = max(12, self._ctrl_btn.mapTo(self.centralWidget(),
                                                    QPoint(0, 0)).x() - 8)
            except Exception:
                pass
            self._place_drawer(self._ctrl_drawer, left)

    def _build_input_row(self) -> QWidget:
        box = CommandFrame()
        row = QHBoxLayout(box)
        row.setContentsMargins(12, 0, 4, 0)
        row.setSpacing(6)
        prompt = QLabel("›")
        prompt.setFont(px_font(20, QFont.Weight.Light))
        prompt.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        row.addWidget(prompt)

        self._input = QLineEdit()
        self._input.setPlaceholderText("Type a command or question…")
        self._input.setFont(px_font(13))
        self._input.setStyleSheet(f"""
            QLineEdit {{
                background: transparent; color: {C.WHITE};
                border: none; padding: 0;
                selection-background-color: {C.PRI_GHO};
            }}
        """)
        self._input.returnPressed.connect(self._send)
        row.addWidget(self._input, stretch=1)

        send = IconButton("send", 32)
        send.setToolTip("Send  [Enter]")
        send.clicked.connect(self._send)
        row.addWidget(send)
        return box

    def _build_content_panel(self) -> QWidget:
        """
        Panel below the HUD — shows search results, news, briefings, reviews.
        Hidden by default; appears when show_content() is called.
        """
        w = HoloPanel("BRIEFING", "")
        w.setObjectName("ContentPanel")
        w.hide()
        # The panel paints its own title and time; these stand in for the
        # labels that used to hold them, so everything that sets them still does.
        self._content_title_lbl = PanelText(w, "title")
        self._content_ts_lbl = PanelText(w, "code")

        dismiss = IconButton("close", 22)
        dismiss.setToolTip("Dismiss")
        dismiss.clicked.connect(w.hide)
        w.add_header_widget(dismiss)

        self._content_display = QTextEdit()
        self._content_display.setReadOnly(True)
        self._content_display.setFont(px_font(13))
        self._content_display.setMinimumHeight(60)
        self._content_display.setFrameShape(QFrame.Shape.NoFrame)
        self._content_display.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self._content_display.setStyleSheet(f"""
            QTextEdit {{
                background: transparent;
                color: {C.TEXT};
                border: none;
                selection-background-color: {C.PRI_GHO};
            }}
        """ + scroll_css())
        w.body.addWidget(self._content_display)
        return w

    def _reveal_below_hud(self, panel: QWidget, want: int) -> None:
        """Show a panel under the HUD at a usable height.

        Checked every time, not only the first: showing the quiz gives the
        content panel a height of 0 while leaving it "visible", so the next
        search result, review or finished task landed in a panel nobody could
        see."""
        panel.show()
        split = self._center_split
        idx = split.indexOf(panel)
        sizes = split.sizes()
        if idx < 0 or sizes[idx] >= 60:
            return
        total = sum(sizes) or split.height()
        new = [0] * len(sizes)
        new[0] = max(total - want, 120)
        new[idx] = want
        split.setSizes(new)

    def _show_content(self, title: str, text: str):
        """Slot — runs on Qt main thread. Updates and shows the content panel."""
        import time as _time
        # The panel opens below the head, so the head looks down at it. It is a
        # tiny thing that answers "did that land?" before you read a word.
        self.hud.glance(0.0, -0.85, hold=1.3)
        # Only the app's own label is upper-cased ("NEWS — …"); what follows the
        # dash is a search or a name in the user's language, and str.upper()
        # applies English rules to it (Turkish "istanbul" -> "ISTANBUL").
        label, dash, rest = str(title or "").partition(" — ")
        self._content_title_lbl.setText((label.upper() + dash + rest)[:48])
        self._content_ts_lbl.setText(_time.strftime("%H:%M:%S"))
        self._content_display.setPlainText(text)
        self._content_display.moveCursor(
            self._content_display.textCursor().MoveOperation.Start
        )
        self._reveal_below_hud(self._content_panel, 220)

    # ── the coding agent, live ───────────────────────────────────────────────
    # The panel under the head shows a coding job while it runs: what was asked,
    # the plan, every file with its state, and the steps as they happen. Drawn
    # from a snapshot the job publishes on every step, so this slot never reads
    # anything a worker thread is still changing.
    _JOB_STATES = {
        "waiting": ("TEXT_DIM", "○"), "writing": ("PRI", "✎"),
        "done": ("GREEN", "✓"), "fixing": ("ACC2", "↻"),
        "skipped": ("TEXT_DIM", "–"), "failed": ("RED", "✕"),
        "broken": ("RED", "!"),
    }
    _JOB_PHASES = ("reading", "planning", "writing", "checking", "testing")

    def _show_code_job(self, job: dict):
        """Slot — Qt main thread. Draws the coding agent's progress."""
        import time as _time
        if not job:
            return
        e = self._esc
        col = lambda name: getattr(C, name, C.TEXT)
        status = job.get("status", "")
        finished = job.get("finished")
        took = (finished or _time.time()) - job.get("started", _time.time())

        # Phase strip: where in the job it is, readable at a glance.
        strip = []
        phases = list(job.get("phases") or self._JOB_PHASES)
        here = phases.index(status) if status in phases else (
            len(phases) if finished else -1)
        for i, ph in enumerate(phases):
            c = C.GREEN if i < here else (C.PRI if i == here else C.TEXT_DIM)
            mark = "●" if i <= here else "○"
            strip.append(f'<span style="color:{c};">{mark} {ph.upper()}</span>')
        if finished:
            fin = C.GREEN if status == "done" else (C.RED if status == "failed" else C.ACC2)
            strip.append(f'<span style="color:{fin};"><b>● {e(status.upper())}</b></span>')

        parts = [f'<div style="color:{C.TEXT}; font-family:Courier New;">',
                 f'<div style="color:{C.TEXT_DIM};">{"  ".join(strip)}'
                 f'&nbsp;&nbsp;·&nbsp;&nbsp;{took:.0f}s</div>',
                 f'<div style="margin-top:6px;"><span style="color:{C.TEXT_DIM};">'
                 f'REQUEST&nbsp;</span>{e(job.get("request", ""))}</div>']
        if job.get("summary"):
            parts.append(
                f'<div style="color:{C.WHITE}; border-left:2px solid {C.PRI};'
                f' padding-left:8px; margin:8px 0;">{e(job["summary"])}</div>')
        files = job.get("files") or {}
        if files:
            parts.append(f'<div style="color:{C.TEXT_DIM}; margin-top:4px;">FILES</div>')
            for path, f in files.items():
                cname, mark = self._JOB_STATES.get(f.get("state", ""), ("TEXT_DIM", "·"))
                verb = "new " if f.get("action") == "create" else ""
                parts.append(
                    f'<div><span style="color:{col(cname)};">{mark}</span>&nbsp;'
                    f'<span style="color:{C.WHITE};">{e(path)}</span>&nbsp;'
                    f'<span style="color:{C.TEXT_DIM};">{verb}{e(f.get("what", ""))[:110]}'
                    f'</span></div>')
        # Newest first, so the latest step sits under the plan and the whole
        # state of the job is visible without scrolling in a 220-pixel panel.
        steps = job.get("steps") or []
        if steps:
            parts.append(f'<div style="color:{C.TEXT_DIM}; margin-top:8px;">'
                         f'STEPS — newest first</div>')
            for t, msg in reversed(steps[-14:]):
                parts.append(f'<div><span style="color:{C.TEXT_DIM};">{float(t):6.1f}s'
                             f'</span>&nbsp;{e(msg)}</div>')
        parts.append("</div>")

        # Only the app's own label is upper-cased — the folder name after the
        # dash is the user's, in their language (see _show_content).
        title = (f"CODE AGENT — {job.get('folder', '')} · {job.get('model', '')}")[:48]
        first = self._content_title_lbl.text() != title
        self._content_title_lbl.setText(title)
        self._content_ts_lbl.setText(_time.strftime("%H:%M:%S"))
        bar = self._content_display.verticalScrollBar()
        keep = 0 if first else bar.value()
        self._content_display.setHtml("".join(parts))
        # setHtml jumps to the top; put the reader back where they were.
        bar.setValue(keep)
        if first:
            self.hud.glance(0.0, -0.85, hold=1.3)
        self._reveal_below_hud(self._content_panel, 260)

    # ── document review ──────────────────────────────────────────────────────
    # Rendered as rich text into the content panel that already exists, rather
    # than into a panel of its own. A review is read, not clicked, so QTextEdit
    # gives scrolling, selection and copy for nothing, and the HUD gains no
    # widget it has to lay out. Severity decides colour and order here because
    # that is presentation; the plugin supplies no styling and knows no palette,
    # which is also what lets a re-theme repaint a review correctly.

    # Severity is marked by a symbol and a colour, not by a word. The findings
    # themselves are in the user's language, and "[SERIOUS]" sitting inside a
    # Turkish sentence is the kind of seam this project tries not to have —
    # while translating the tag would mean a table per language, which is worse.
    # A shape carries it in every language, and shape plus colour still reads
    # for someone who cannot separate red from amber. What the marks mean
    # arrives the way everything else does: JARVIS says it out loud.
    _REVIEW_MARKS = {"serious": ("RED", "▲"), "caution": ("ACC2", "●"), "note": ("PRI_DIM", "·")}

    @staticmethod
    def _esc(s) -> str:
        return (str(s or "").replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace("\n", "<br>"))

    def _show_review(self, title: str, summary: str, findings, unclear):
        """Slot — Qt main thread. Lays a document review into the content panel."""
        e = self._esc
        parts = [f'<div style="color:{C.TEXT}; font-family:{DISPLAY_CSS}; font-size:13px;">']

        if summary:
            parts.append(
                f'<div style="color:{C.WHITE}; border-left:2px solid {C.PRI};'
                f' padding-left:8px; margin-bottom:10px;">{e(summary)}</div>')

        for f in (findings or []):
            key, mark = self._REVIEW_MARKS.get(f.get("severity"), ("PRI_DIM", "·"))
            colour = getattr(C, key)
            parts.append(f'<div style="margin-bottom:11px;">')
            parts.append(
                f'<span style="color:{colour}; font-weight:bold;">{mark}</span> '
                f'<span style="color:{C.WHITE}; font-weight:bold;">'
                f'{e(f.get("heading"))}</span>')
            if f.get("detail"):
                parts.append(f'<div style="margin-left:12px;">{e(f["detail"])}</div>')
            if f.get("quote"):
                # The document's own wording, visually separated from the
                # explanation so the two are never mistaken for each other.
                parts.append(
                    f'<div style="margin-left:12px; color:{C.TEXT_DIM};'
                    f' border-left:1px solid {C.BORDER}; padding-left:7px;">'
                    f'&ldquo;{e(f["quote"])}&rdquo;</div>')
            if f.get("suggestion"):
                parts.append(
                    f'<div style="margin-left:12px; color:{C.PRI};">'
                    f'&rarr; {e(f["suggestion"])}</div>')
            parts.append('</div>')

        if unclear:
            parts.append(
                f'<div style="margin-top:6px; border-top:1px solid {C.BORDER};'
                f' padding-top:7px; color:{C.TEXT_MED};">'
                'The document does not settle:</div>')
            for u in unclear:
                parts.append(
                    f'<div style="margin-left:12px; color:{C.TEXT_MED};">'
                    f'&middot; {e(u)}</div>')
        parts.append('</div>')

        import time as _time
        self.hud.glance(0.0, -0.85, hold=1.3)
        # Left as written, not upper-cased. The other content-panel titles are
        # the app's own English labels, but this one is the document's name in
        # the user's language, and str.upper() applies English casing rules to
        # it: Turkish "Sözleşmesi" comes back "SÖZLEŞMESI", having lost the
        # dotted capital İ. Python has no locale-aware upper to reach for, and
        # imposing one language's rules on all of them is the bug, not the fix.
        self._content_title_lbl.setText((title or "Document")[:48])
        self._content_ts_lbl.setText(_time.strftime("%H:%M:%S"))
        self._content_display.setHtml("".join(parts))
        self._content_display.moveCursor(
            self._content_display.textCursor().MoveOperation.Start)
        self._reveal_below_hud(self._content_panel, 260)

    # ── quiz panel ───────────────────────────────────────────────────────────
    # An interactive twin of the content panel. The plugin only ever hands over
    # questions; everything about asking, marking and reporting happens here,
    # and the finished result is pushed back into the conversation the same way
    # a dropped file is — as a message JARVIS reads and responds to. That keeps
    # the tool call short (it returns the moment the board is up) and leaves the
    # talking to the assistant, in the user's own language.

    def _quiz_btn(self, text: str, primary: bool = False) -> QPushButton:
        b = QPushButton(text)
        b.setFont(px_font(12, QFont.Weight.DemiBold if primary else QFont.Weight.Normal,
                          spacing=1.0 if primary else 0.0))
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.setMinimumHeight(30)
        b.setStyleSheet(btn_css("primary" if primary else "dim"))
        return b

    def _build_quiz_panel(self) -> QWidget:
        w = HoloPanel("QUIZ", "")
        w.setObjectName("QuizPanel")
        w.hide()
        self._quiz_title_lbl = PanelText(w, "title")
        self._quiz_count_lbl = PanelText(w, "code")

        quit_btn = IconButton("close", 22)
        quit_btn.setToolTip("Dismiss")
        quit_btn.clicked.connect(self._hide_quiz)
        w.add_header_widget(quit_btn)

        lay = w.body
        lay.setSpacing(8)

        self._quiz_q_lbl = QLabel("")
        self._quiz_q_lbl.setWordWrap(True)
        self._quiz_q_lbl.setFont(px_font(15))
        self._quiz_q_lbl.setStyleSheet(f"color: {C.WHITE}; background: transparent;")
        lay.addWidget(self._quiz_q_lbl)

        self._quiz_answers = QWidget()
        self._quiz_answers.setStyleSheet("background: transparent;")
        self._quiz_answers_lay = QVBoxLayout(self._quiz_answers)
        self._quiz_answers_lay.setContentsMargins(0, 2, 0, 0)
        self._quiz_answers_lay.setSpacing(5)
        lay.addWidget(self._quiz_answers)

        self._quiz_note_lbl = QLabel("")
        self._quiz_note_lbl.setWordWrap(True)
        self._quiz_note_lbl.setFont(px_font(12))
        self._quiz_note_lbl.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
        self._quiz_note_lbl.hide()
        lay.addWidget(self._quiz_note_lbl)

        foot = QHBoxLayout()
        foot.addStretch()
        self._quiz_next_btn = self._quiz_btn("NEXT  →", primary=True)
        self._quiz_next_btn.setFixedWidth(120)
        self._quiz_next_btn.clicked.connect(self._quiz_next)
        self._quiz_next_btn.hide()
        foot.addWidget(self._quiz_next_btn)
        lay.addLayout(foot)

        self._quiz = None
        return w

    def _show_quiz(self, topic: str, questions, grader=None):
        """Slot — Qt main thread. Puts a fresh quiz on the board."""
        if not questions:
            return
        self._quiz = {
            "topic": topic or "",
            "questions": list(questions),
            "grader": grader,
            "i": 0,
            "results": [],
            "answered": False,
        }
        self._quiz_title_lbl.setText((topic or "quiz").upper()[:48])
        self.hud.glance(0.0, -0.85, hold=1.3)
        self._reveal_below_hud(self._quiz_panel, 250)
        self._quiz_render()

    def _hide_quiz(self):
        self._quiz = None
        self._quiz_panel.hide()

    def _quiz_clear_answers(self):
        while self._quiz_answers_lay.count():
            item = self._quiz_answers_lay.takeAt(0)
            child = item.widget()
            if child is not None:
                child.setParent(None)
                child.deleteLater()

    def _quiz_render(self):
        q = self._quiz["questions"][self._quiz["i"]]
        n, total = self._quiz["i"] + 1, len(self._quiz["questions"])
        self._quiz_count_lbl.setText(f"{n} / {total}")
        self._quiz_q_lbl.setText(q.get("question", ""))
        self._quiz_note_lbl.hide()
        self._quiz_next_btn.hide()
        self._quiz["answered"] = False
        self._quiz_clear_answers()

        opts = q.get("options") or []
        if opts:
            for text in opts:
                b = self._quiz_btn("   " + text)
                b.clicked.connect(lambda _=False, t=text: self._quiz_submit(t))
                self._quiz_answers_lay.addWidget(b)
        else:
            row = QWidget(); row.setStyleSheet("background: transparent;")
            h = QHBoxLayout(row); h.setContentsMargins(0, 0, 0, 0); h.setSpacing(6)
            field = QLineEdit()
            field.setFont(hud_font(9))
            field.setPlaceholderText("your answer")
            field.setStyleSheet(f"""
                QLineEdit {{
                    background: {C.PANEL2}; color: {C.WHITE};
                    border: 1px solid {C.BORDER}; border-radius: 2px; padding: 4px 7px;
                }}
                QLineEdit:focus {{ border-color: {C.PRI_DIM}; }}
            """)
            send = self._quiz_btn("ANSWER", primary=True)
            send.setFixedWidth(90)
            field.returnPressed.connect(lambda: self._quiz_submit(field.text()))
            send.clicked.connect(lambda: self._quiz_submit(field.text()))
            h.addWidget(field, stretch=1)
            h.addWidget(send)
            self._quiz_answers_lay.addWidget(row)
            field.setFocus()

    def _quiz_submit(self, given: str):
        if self._quiz is None or self._quiz["answered"]:
            return
        self._quiz["answered"] = True
        q = self._quiz["questions"][self._quiz["i"]]
        grader = self._quiz.get("grader")
        verdict = None
        if callable(grader):
            try:
                verdict = grader(q, given)
            except Exception:
                verdict = None
        self._quiz["results"].append({
            "question": q.get("question", ""),
            "type": q.get("type", ""),
            "given": str(given or "").strip(),
            "answer": q.get("answer", ""),
            "correct": verdict,
        })

        for i in range(self._quiz_answers_lay.count()):
            wdg = self._quiz_answers_lay.itemAt(i).widget()
            if wdg is not None:
                wdg.setEnabled(False)

        if verdict is True:
            mark, colour = "✓  correct", C.GREEN
        elif verdict is False:
            mark, colour = "✕  " + str(q.get("answer", "")), C.RED
        else:
            # Open answers and near-miss gap-fills are JARVIS's to judge. Saying
            # so is honest; marking it wrong here would be a guess.
            mark, colour = "…  noted — I'll go over this one with you", C.ACC2
        note = q.get("note") or ""
        self._quiz_note_lbl.setText(mark + (("\n" + note) if note else ""))
        self._quiz_note_lbl.setStyleSheet(f"color: {colour}; background: transparent;")
        self._quiz_note_lbl.show()

        last = self._quiz["i"] >= len(self._quiz["questions"]) - 1
        self._quiz_next_btn.setText("FINISH  →" if last else "NEXT  →")
        self._quiz_next_btn.show()
        self._quiz_next_btn.setFocus()

    def _quiz_next(self):
        if self._quiz is None:
            return
        if self._quiz["i"] >= len(self._quiz["questions"]) - 1:
            self._quiz_finish()
        else:
            self._quiz["i"] += 1
            self._quiz_render()

    def _quiz_finish(self):
        if self._quiz is None:
            return
        topic = self._quiz["topic"]
        results = self._quiz["results"]
        right = sum(1 for r in results if r["correct"] is True)
        unsure = sum(1 for r in results if r["correct"] is None)
        total = len(results)
        self._quiz_panel.hide()
        self._quiz = None

        self._log.append_log(f"QUIZ: {topic or 'quiz'} — {right}/{total} correct")

        # Hand it back to JARVIS as a message, not as a tool return: the tool
        # call ended minutes ago. This is the same channel a dropped file uses.
        lines = [f"[QUIZ_DONE] topic={topic or 'general'} | "
                 f"auto-marked {right}/{total} correct"
                 + (f", {unsure} still need your marking" if unsure else "")]
        for i, r in enumerate(results, 1):
            state = ("correct" if r["correct"] is True
                     else "wrong" if r["correct"] is False else "NEEDS MARKING")
            lines.append(
                f"{i}. [{r['type']}] {r['question']} | they answered: "
                f"{r['given'] or '(blank)'} | expected: {r['answer']} | {state}")
        lines.append(
            "Mark every question flagged NEEDS MARKING yourself — accept an answer "
            "that means the same thing. Then tell them how they did in their own "
            "language: the score, what they got wrong and why, in a couple of "
            "sentences. Offer another round only if it fits. "
            "Remember something only if it would still matter next week — that they "
            "are working through a subject, or keep missing the same thing. A score "
            "from one session is not worth a memory, and a memory per quiz would "
            "bury the things that are.")
        msg = "\n".join(lines)
        if self.on_text_command:
            threading.Thread(target=self.on_text_command, args=(msg,), daemon=True).start()

    def _build_footer(self) -> QWidget:
        self._status_strip = StatusStrip()
        self._status_strip.set_right(
            "F4 MUTE   ·   F11 FULLSCREEN   ·   ESC INTERRUPT   ·   BY FATIHMAKES")
        self._refresh_status_strip()
        return self._status_strip

    def _on_file_selected(self, path: str):
        self._current_file = path
        p    = Path(path)
        cat  = _file_category(p)
        icon, _ = _FILE_ICONS.get(cat, _FILE_ICONS["unknown"])
        if cat == "folder":
            n = self._drop_zone._folder_files
            self._file_hint.setText(
                f"{icon}  {p.name}  ·  {n} files  ·  Tell {self._assistant_name} "
                f"what to change in it")
            self._log.append_log(f"FOLDER: {p.name} ({n} files) loaded")
            if self.on_text_command:
                msg = (
                    f"[FOLDER_UPLOADED] path={path} | name={p.name} | files={n} | "
                    f"Briefly tell the user the project folder '{p.name}' is loaded "
                    f"and that you can improve, fix or extend it with the coding "
                    f"model — ask what they want changed. When they say, call "
                    f"dev_agent with mode='improve' and their request as the "
                    f"description."
                )
                threading.Thread(target=self.on_text_command, args=(msg,),
                                 daemon=True).start()
            return
        size = _fmt_size(p.stat().st_size)
        self._file_hint.setText(f"{icon}  {p.name}  ·  {size}  ·  Tell {self._assistant_name} what to do with it")
        self._log.append_log(f"FILE: {p.name} ({size}) loaded")
        if self.on_text_command:
            msg = (
                f"[FILE_UPLOADED] path={path} | name={p.name} | "
                f"type={p.suffix.lstrip('.')} | size={size} | "
                f"Briefly tell the user you can see the file '{p.name}' "
                f"({size}) has been uploaded and ask what they'd like to do with it."
            )
            threading.Thread(target=self.on_text_command, args=(msg,), daemon=True).start()

    def notify_phone_connected(self) -> None:
        if self._remote_overlay and self._remote_overlay.isVisible():
            self._remote_overlay.mark_connected()

    def _open_remote(self):
        if not self.on_remote_clicked:
            self._log.append_log("SYS: Dashboard not running — remote unavailable.")
            return
        result = self.on_remote_clicked()
        if not result:
            self._log.append_log("SYS: Could not generate remote key.")
            return
        url    = result[0]
        key    = result[1]
        auto   = result[2] if len(result) >= 3 else ""
        manual = result[3] if len(result) >= 4 else url
        berr   = result[4] if len(result) >= 5 else ""
        if self._remote_overlay:
            self._remote_overlay._do_close()
        cw  = self.centralWidget()
        ow, oh = RemoteKeyOverlay._OW, RemoteKeyOverlay._OH
        ov  = RemoteKeyOverlay(url, key, auto_login_url=auto, manual_url=manual,
                               expiry_secs=600, parent=cw,
                               bind_error=berr)
        ov.set_new_key_callback(self.on_remote_clicked)
        ov.setGeometry(
            (cw.width()  - ow) // 2,
            (cw.height() - oh) // 2,
            ow, oh,
        )
        ov.closed.connect(lambda: setattr(self, '_remote_overlay', None))
        ov.show()
        self._remote_overlay = ov
        self._log.append_log(f"SYS: Remote key generated — manual: {manual or url}")

    # ── Auto-start ──────────────────────────────────────────────────────────────

    def _check_autostart(self) -> bool:
        """Returns True if auto-start is currently registered on this OS."""
        try:
            if _OS == "Windows":
                import winreg
                key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_READ)
                try:
                    winreg.QueryValueEx(key, "JARVIS_AI")
                    return True
                except FileNotFoundError:
                    return False
                finally:
                    winreg.CloseKey(key)
            elif _OS == "Darwin":
                return (Path.home() / "Library" / "LaunchAgents"
                        / "com.jarvis.assistant.plist").exists()
            else:
                return (Path.home() / ".config" / "autostart" / "jarvis.desktop").exists()
        except Exception:
            return False

    def _toggle_autostart(self):
        currently_on = self._check_autostart()
        try:
            script = str(Path(__file__).resolve().parent.parent / "main.py")
            if _OS == "Windows":
                import winreg
                reg = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_ALL_ACCESS)
                if currently_on:
                    winreg.DeleteValue(reg, "JARVIS_AI")
                else:
                    pythonw = Path(sys.executable).parent / "pythonw.exe"
                    exe = str(pythonw if pythonw.exists() else sys.executable)
                    winreg.SetValueEx(reg, "JARVIS_AI", 0, winreg.REG_SZ,
                                      f'"{exe}" "{script}"')
                winreg.CloseKey(reg)
            elif _OS == "Darwin":
                plist_dir = Path.home() / "Library" / "LaunchAgents"
                plist_dir.mkdir(parents=True, exist_ok=True)
                plist = plist_dir / "com.jarvis.assistant.plist"
                if currently_on:
                    plist.unlink(missing_ok=True)
                else:
                    plist.write_text(
                        '<?xml version="1.0" encoding="UTF-8"?>\n'
                        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
                        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
                        '<plist version="1.0"><dict>\n'
                        '  <key>Label</key><string>com.jarvis.assistant</string>\n'
                        '  <key>ProgramArguments</key><array>\n'
                        f'    <string>{sys.executable}</string>\n'
                        f'    <string>{script}</string>\n'
                        '  </array>\n'
                        '  <key>RunAtLoad</key><true/>\n'
                        '</dict></plist>\n'
                    )
            else:
                desk_dir = Path.home() / ".config" / "autostart"
                desk_dir.mkdir(parents=True, exist_ok=True)
                desk = desk_dir / "jarvis.desktop"
                if currently_on:
                    desk.unlink(missing_ok=True)
                else:
                    desk.write_text(
                        "[Desktop Entry]\n"
                        f"Name={self._assistant_name}\n"
                        f"Exec={sys.executable} {script}\n"
                        "Type=Application\nTerminal=false\n"
                        "X-GNOME-Autostart-enabled=true\n"
                    )
            enabled = not currently_on
            self._update_autostart_btn(enabled)
            self._log.append_log(
                f"SYS: Auto-start {'enabled' if enabled else 'disabled'}.")
        except Exception as e:
            self._log.append_log(f"ERR: Auto-start failed — {e}")

    def _update_autostart_btn(self, enabled: bool):
        if not hasattr(self, '_autostart_btn'):
            return
        self._autostart_btn.setText("◉  AUTO-START: ON" if enabled else "◉  AUTO-START: OFF")
        self._autostart_btn.setStyleSheet(btn_css("on" if enabled else "off"))

    def _toggle_brief(self):
        from memory.config_manager import get_brief_enabled, save_brief_enabled
        new_val = not get_brief_enabled()
        save_brief_enabled(new_val)
        self._update_brief_btn(new_val)
        # The briefing is a routine now (core/routines); the switch drives it.
        try:
            from core.routines import desktop as _routines
            _routines.set_builtin_enabled("briefing", new_val)
        except Exception as e:
            print(f"[Routines] briefing switch not mirrored: {e}")

    # ── Wake word settings ───────────────────────────────────────────────────

    def _wake_state(self) -> dict:
        """Combined state for the two wake-word buttons. Readiness is a cheap,
        deterministic on-disk check now (see core.wake_word.is_ready), so there
        is nothing to cache — the button never flickers to a stale value."""
        if self.wake_get_state:
            try:
                s = self.wake_get_state()
                return {"ready": bool(s.get("ready")),
                        "enabled": bool(s.get("enabled")),
                        "awake": bool(s.get("awake"))}
            except Exception:
                pass
        # Before JarvisLive has wired its callback (drawer built at startup).
        ready, enabled = False, False
        try:
            from core.wake_word import is_ready
            from memory.config_manager import get_wake_word_enabled
            ready, enabled = is_ready(), get_wake_word_enabled()
        except Exception:
            pass
        return {"ready": ready, "enabled": enabled, "awake": True}

    def _refresh_wake_btns(self):
        if not hasattr(self, '_wake_btn'):
            return
        st = self._wake_state()
        _on, _off = btn_css("on"), btn_css("off")
        self._wake_btn.setEnabled(True)
        if not st["ready"]:
            self._wake_btn.setText("⬇  WAKE WORD: DOWNLOAD")
            self._wake_btn.setStyleSheet(_off)
            self._wake_sleep_btn.hide()
        elif st["enabled"]:
            self._wake_btn.setText("🎙  WAKE WORD: ON")
            self._wake_btn.setStyleSheet(_on)
            self._wake_sleep_btn.show()
            self._wake_sleep_btn.setText("😴  SLEEP NOW" if st["awake"] else "👂  WAKE NOW")
            self._wake_sleep_btn.setStyleSheet(_off)
        else:
            self._wake_btn.setText("🎙  WAKE WORD: OFF")
            self._wake_btn.setStyleSheet(_off)
            self._wake_sleep_btn.hide()

    def _refresh_talk_btns(self):
        """Repaint the push-to-talk row from the saved setting."""
        if not hasattr(self, "_ptt_btn"):
            return
        from core.hotkey import chord_label
        from memory.config_manager import get_push_to_talk_enabled

        ptt = get_push_to_talk_enabled()
        self._ptt_btn.setText(f"🎚  PUSH-TO-TALK: {chord_label()}" if ptt
                              else "🎚  PUSH-TO-TALK: OFF")
        self._ptt_btn.setStyleSheet(btn_css("on" if ptt else "off"))
        self._ptt_btn.setToolTip(
            "Microphone stays closed until you hold the key — nothing is sent "
            "while you are not holding it." if ptt
            else "Hold a key to talk instead of streaming the mic continuously.")

    def _refresh_hud_btn(self):
        from memory.config_manager import get_hud_style
        face = get_hud_style() == "face"
        # Neither state is "off", so both read as active — this is a choice
        # between two things, not a switch with a disabled side.
        self._hud_btn.setText("🧑  HUD: ANIMATED FACE" if face
                              else "◉  HUD: REACTOR CORE")
        self._hud_btn.setStyleSheet(btn_css("choice"))
        self._hud_btn.setToolTip(
            "An animated head that speaks your words and shows what JARVIS is "
            "doing. Tap to switch to the reactor core."
            if face else
            "A 3-D reactor core that turns with the state and moves with your "
            "voice. Tap to switch to the animated head.")

    def _toggle_hud_style(self):
        """Swap the centrepiece. Both objects stay in memory, so the change is
        instant and switching back costs nothing."""
        from memory.config_manager import get_hud_style, save_hud_style
        want = "core" if get_hud_style() == "face" else "face"
        save_hud_style(want)
        try:
            self.hud.hud_style = want
            self.hud.update()
        except Exception:
            pass
        self._refresh_hud_btn()
        self._log.append_log(
            "SYS: HUD switched to the animated face." if want == "face"
            else "SYS: HUD switched to the reactor core.")

    def _toggle_ptt(self):
        from memory.config_manager import (get_push_to_talk_enabled,
                                           save_push_to_talk_enabled)
        want = not get_push_to_talk_enabled()
        save_push_to_talk_enabled(want)
        scope = None
        if self.on_push_to_talk:
            try:
                scope = self.on_push_to_talk(want)
            except Exception as e:
                self._log.append_log(f"ERR: Push-to-talk failed — {e}")
                save_push_to_talk_enabled(False)
                want = False
        self._apply_ptt_shortcut(want and scope != "global")
        self._refresh_talk_btns()

    def _apply_ptt_shortcut(self, needed: bool):
        """Bind the chord inside the window when no global hook is available.

        On macOS and Linux there is no dependency-free way to read global key
        state, so the chord is at least live whenever this window has focus.
        Qt gives no key-release for a QShortcut, so a press latches the mic open
        and a short timer closes it; held down, auto-repeat keeps pushing that
        timer out, which behaves like holding a key.
        """
        from PyQt6.QtGui import QKeySequence, QShortcut
        from core.hotkey import qt_sequence

        if not needed:
            sc = getattr(self, "_ptt_sc", None)
            if sc is not None:
                sc.setEnabled(False)
                self._ptt_sc = None
            self._ptt_hold(False)
            return
        if getattr(self, "_ptt_sc", None) is not None:
            return

        self._ptt_release = QTimer(self)
        self._ptt_release.setSingleShot(True)
        self._ptt_release.setInterval(420)
        self._ptt_release.timeout.connect(lambda: self._ptt_hold(False))

        def _press():
            self._ptt_hold(True)
            self._ptt_release.start()

        self._ptt_sc = QShortcut(QKeySequence(qt_sequence()), self)
        self._ptt_sc.setAutoRepeat(True)
        self._ptt_sc.activated.connect(_press)

    def _ptt_hold(self, held: bool):
        """Report a windowed press/release to whoever owns the microphone."""
        cb = getattr(self, "ptt_hold", None)
        if cb:
            try:
                cb(bool(held))
            except Exception:
                pass

    def _toggle_wake_word(self):
        st = self._wake_state()
        if not st["ready"]:
            # First time: download openwakeword + model in a worker thread.
            self._wake_btn.setText("⬇  DOWNLOADING… (one-time)")
            self._wake_btn.setEnabled(False)
            def _work():
                try:
                    from core.wake_word import install_and_download
                    ok, msg = install_and_download(
                        logger=lambda m: self._log_sig.emit(f"SYS: {m}"))
                except Exception as e:
                    ok, msg = False, str(e)
                if ok and self.on_wake_toggle:
                    try:
                        self.on_wake_toggle(True)   # auto-enable after a successful download
                    except Exception:
                        pass
                self._wake_dl_sig.emit(ok, msg)
            threading.Thread(target=_work, daemon=True).start()
            return
        # Already downloaded → just flip enabled/disabled through JarvisLive.
        if self.on_wake_toggle:
            try:
                self.on_wake_toggle(not st["enabled"])
            except Exception:
                pass
        self._refresh_wake_btns()

    def _on_wake_install_done(self, ok: bool, msg: str):
        self._log_sig.emit(f"SYS: {'Wake word ready.' if ok else 'Wake word setup failed: ' + msg}")
        self._refresh_wake_btns()

    def _tap_wake_manual(self):
        if self.on_wake_manual:
            try:
                self.on_wake_manual()
            except Exception:
                pass
        self._refresh_wake_btns()

    def _update_brief_btn(self, enabled: bool):
        if not hasattr(self, '_brief_btn'):
            return
        self._brief_btn.setText("☀  MORNING BRIEF: ON" if enabled else "☀  MORNING BRIEF: OFF")
        self._brief_btn.setStyleSheet(btn_css("on" if enabled else "off"))

    def _open_customize(self):
        cfg = _read_full_config()
        if self._customize_overlay:
            self._customize_overlay.hide()
        cw = self.centralWidget()
        ov = CustomizeOverlay(
            cfg.get("assistant_name", "JARVIS") or "JARVIS",
            cfg.get("user_name", ""),
            cfg.get("ui_color", "") or DEFAULT_UI_COLOR,
            cfg.get("voice_name", ""),
            parent=cw,
        )
        ow, oh = CustomizeOverlay._OW, CustomizeOverlay._OH
        oh = min(oh, cw.height() - 16)
        ov.setGeometry(
            (cw.width()  - ow) // 2,
            (cw.height() - oh) // 2,
            ow, oh,
        )
        ov.on_preview = self._preview_ui_color
        ov.saved.connect(self._apply_name_update)
        ov.show()
        self._customize_overlay = ov

    def _preview_ui_color(self, hex_color: str):
        """Live preview — paints the whole interface the new colour (does NOT write to config)."""
        old = current_palette()
        if apply_ui_accent(hex_color):
            retheme_all_widgets(old, current_palette())

    def _apply_name_update(self, name: str, user_name: str, ui_color: str = "",
                           voice: str = ""):
        """Update all name/theme-dependent UI elements and persist to config."""
        self._assistant_name = name.strip() or "JARVIS"
        display = self._assistant_name.upper()
        self.setWindowTitle(f"{display} — {APP_VERSION}")
        self._title_lbl.setText(display)
        self._title_lbl.setToolTip(self._subtitle(display))
        self._log._ai_name_lc = self._assistant_name.lower()
        self.hud._assistant_name = display

        color_changed = False
        if ui_color:
            old = current_palette()
            if apply_ui_accent(ui_color):
                # Live-paint the whole interface (panels, buttons, borders, HUD)
                retheme_all_widgets(old, current_palette())
                color_changed = old["PRI"] != C.PRI

        # Voice change → persist and, if it actually changed, rebuild the Live
        # session so the new voice takes effect (it's fixed at connect time).
        voice_changed = False
        if voice:
            from memory.config_manager import get_voice, save_voice
            if voice != get_voice():
                save_voice(voice)
                voice_changed = True

        try:
            from memory.config_manager import update_config
            fields = {"assistant_name": self._assistant_name, "user_name": user_name.strip()}
            if ui_color:
                fields["ui_color"] = ui_color.strip().lower()
            update_config(**fields)
            self._log.append_log(f"SYS: Identity updated — {display}")
            if color_changed:
                self._log.append_log(f"SYS: UI colour applied — {ui_color}")
            if voice_changed:
                self._log.append_log(f"SYS: Voice set — {voice}")
        except Exception as e:
            self._log.append_log(f"ERR: Config save failed — {e}")

        self._refresh_identity_rows()

        if voice_changed and self.on_voice_change:
            self.on_voice_change()

    def _centre_overlay(self, ov) -> None:
        """Place a floating overlay in the middle of the HUD and show it."""
        cw = self.centralWidget()
        ov.adjustSize()
        ov.setGeometry(
            max(0, (cw.width()  - ov.width())  // 2),
            max(0, (cw.height() - ov.height()) // 2),
            ov.width(), ov.height(),
        )
        ov.show()
        ov.raise_()

    # ── Audio devices ────────────────────────────────────────────────────────

    def _open_audio_devices(self):
        ov = AudioDeviceOverlay(parent=self.centralWidget())
        ov.picked.connect(self._on_audio_devices_applied)
        self._centre_overlay(ov)
        self._audio_overlay = ov            # keep a reference so it isn't GC'd

    def _on_audio_devices_applied(self):
        self._log.append_log("SYS: Audio devices updated.")
        if self.on_audio_device_change:
            self.on_audio_device_change()

    # ── Engine & providers ───────────────────────────────────────────────────

    def _open_providers(self):
        """Open the engine panel. Everything it needs is imported on this call.

        A failure here must not take the drawer with it — the folder can be
        deleted, which is the point of it being a folder, and the honest answer
        to that is a line in the log rather than a traceback.
        """
        try:
            ov = ProvidersOverlay(parent=self.centralWidget())
        except ImportError:
            self._log.append_log(
                "SYS: The providers folder is not installed — "
                "the assistant runs on Gemini Live.")
            return
        except Exception as e:
            self._log.append_log(f"SYS: Provider settings unavailable: {e}")
            return
        ov.changed.connect(self._on_providers_applied)
        self._centre_overlay(ov)
        self._providers_overlay = ov        # keep a reference so it isn't GC'd

    def _on_providers_applied(self):
        """Rebuild the session so the new engine takes over.

        Reuses the audio-device callback rather than adding a second one: both
        mean 'the conversation has to be rebuilt', main.py already keeps the
        transcript across such a rebuild through session resumption, and one
        path that is known to work is better than two that look alike."""
        self._refresh_identity_rows()
        self._log.append_log("SYS: Engine settings saved — reconnecting.")
        if self.on_audio_device_change:
            self.on_audio_device_change()

    # ── Memory panel ─────────────────────────────────────────────────────────

    def _open_memory_panel(self):
        ov = MemoryOverlay(parent=self.centralWidget())
        self._centre_overlay(ov)
        self._memory_overlay = ov

    # ── Irreversible-action confirmation ─────────────────────────────────────

    def _show_confirm_banner(self, title: str, detail: str):
        self._hide_confirm_banner()
        ov = ConfirmBanner(title, detail, parent=self.centralWidget())
        ov.answered.connect(self._on_confirm_answered)
        self._centre_overlay(ov)
        self._confirm_overlay = ov

    def _hide_confirm_banner(self):
        ov = getattr(self, "_confirm_overlay", None)
        if ov is not None:
            ov.hide()
            ov.deleteLater()
            self._confirm_overlay = None

    def _on_confirm_answered(self, accepted: bool):
        # Tear the banner down first: core.confirm.resolve() may be about to
        # shut the machine down, and a live widget mid-callback is not where you
        # want to be when that happens.
        self._hide_confirm_banner()
        try:
            from core.confirm import resolve
            resolve(bool(accepted))
        except Exception as e:
            self._log.append_log(f"ERR: Confirmation failed — {e}")

    def _open_plugin_manager(self):
        plugins = self.get_plugins() if self.get_plugins else []
        cw = self.centralWidget()
        ov = PluginManagerOverlay(plugins, parent=cw)
        ov.adjustSize()
        ov.setGeometry(
            (cw.width()  - ov.width())  // 2,
            (cw.height() - ov.height()) // 2,
            ov.width(), ov.height(),
        )
        ov.show()
        ov.raise_()
        self._plugin_manager_overlay = ov   # keep a reference so it isn't GC'd

    def _open_plugin_settings(self):
        sections = self.get_plugin_settings() if self.get_plugin_settings else []
        cw = self.centralWidget()
        ov = PluginSettingsOverlay(sections, parent=cw)
        ow = PluginSettingsOverlay._OW
        oh = min(560, cw.height() - 16)
        ov.setGeometry(
            (cw.width()  - ow) // 2,
            (cw.height() - oh) // 2,
            ow, oh,
        )
        ov.show()
        ov.raise_()
        self._plugin_settings_overlay = ov   # keep a reference so it isn't GC'd

    # ── Clipboard intelligence ───────────────────────────────────────────────────

    def _on_clipboard_changed(self):
        try:
            text = QApplication.clipboard().text().strip()
            if len(text) >= 10:
                self._clipboard_sig.emit(text)
        except Exception:
            pass

    def _show_clipboard_panel(self, text: str):
        self._clipboard_panel.show_clipboard(text)
        self._position_clipboard_panel()

    def _position_clipboard_panel(self):
        cw = self.centralWidget()
        pw = ClipboardPanel._W
        ph = self._clipboard_panel.sizeHint().height() or ClipboardPanel._H
        x = (cw.width() - pw) // 2
        y = cw.height() - ph - 6
        self._clipboard_panel.setGeometry(x, y, pw, ph)
        self._clipboard_panel.raise_()

    def _on_clipboard_action(self, cmd: str):
        if self.on_text_command:
            threading.Thread(target=self.on_text_command, args=(cmd,), daemon=True).start()

    # ────────────────────────────────────────────────────────────────────────────

    def _do_interrupt(self):
        if self.on_interrupt:
            self.on_interrupt()

    def _toggle_mute(self):
        # A deliberate press settles the question: whatever the video did, or is
        # about to do, the user has now said what they want.
        self._video_auto_muted = False
        self._set_muted(not self._muted)

    def _set_muted(self, muted: bool, note: str = ""):
        muted = bool(muted)
        if muted == self._muted:
            return
        self._muted = muted
        self.hud.muted = muted
        self._style_mute_btn()
        if muted:
            self._apply_state("MUTED")
            self._log.append_log("SYS: Microphone muted." + (f" {note}" if note else ""))
        else:
            self._apply_state("LISTENING")
            self._log.append_log("SYS: Microphone active." + (f" {note}" if note else ""))

    def _style_mute_btn(self):
        if self._muted:
            self._mute_btn.set_look("mic_off", "MIC MUTED", "MUTED_C")
        else:
            self._mute_btn.set_look("mic", "MIC ACTIVE", "GREEN")
        self._refresh_status_strip()

    def _send(self):
        txt = self._input.text().strip()
        if not txt: return
        self._input.clear()
        self._log.append_log(f"You: {txt}")
        if self.on_text_command:
            threading.Thread(target=self.on_text_command, args=(txt,), daemon=True).start()

    def _apply_state(self, state: str):
        self.hud.state    = state
        self.hud.speaking = (state == "SPEAKING")
        if hasattr(self, "_state_chip"):
            self._state_chip.set_state(state)

    def _check_config(self) -> bool:
        d = _read_full_config()
        if not d.get("os_system"):
            return False
        providers = d.get("providers") if isinstance(d.get("providers"), dict) else {}
        # A Gemini key, or a choice to run without one: a local engine needs no key.
        return bool(d.get("gemini_api_key") or d.get("local_only")
                    or str(providers.get("mode") or "").lower() == "pipeline")

    def _show_setup(self):
        ov = SetupOverlay(self.centralWidget())
        cw = self.centralWidget()
        ow, oh = 460, 440
        ov.setGeometry(
            (cw.width()  - ow) // 2,
            (cw.height() - oh) // 2,
            ow, oh,
        )
        ov.done.connect(self._on_setup_done)
        ov.show()
        self._overlay = ov

    def _on_setup_done(self, key: str, os_name: str):
        # Merged into the settings, never written over them. This used to replace
        # the whole file with these two keys, so re-entering a key after an
        # "API key invalid" error erased every other setting — the wake word, the
        # local engine, plugin credentials, the assistant's name.
        from memory.config_manager import update_config
        fields = {"os_system": os_name, "local_only": not key}
        if key:
            fields["gemini_api_key"] = key
        update_config(**fields)
        try:
            from core import gemini as _gemini
            _gemini.api_key(refresh=True)
        except Exception:
            pass
        if not key:
            QTimer.singleShot(400, self._open_providers)
        self._ready = True
        if self._overlay:
            self._overlay.hide()
            self._overlay = None
        self._apply_state("LISTENING")
        self._assistant_name = _read_full_config().get("assistant_name", "JARVIS") or "JARVIS"
        self._log.append_log(f"SYS: Initialised. OS={os_name.upper()}. {self._assistant_name} online.")


