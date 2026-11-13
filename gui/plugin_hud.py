"""
Universal Plugin HUD Display Host for JARVIS MARK LVII.
Provides a dedicated central display surface inside the main HUD for any plugin
to render interactive web applications (via QWebEngineView), declarative native
specifications, formatted text, and custom interfaces.
"""

from __future__ import annotations

import json
import logging
import threading
import webbrowser
from typing import Any, Callable, Dict, List, Optional, Union

from PyQt6.QtCore import QSize, Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QPixmap
from PyQt6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from gui.style import hud_font, mono_font
from gui.theme import C

logger = logging.getLogger("JARVIS.PluginHUD")

# Check PyQt6-WebEngine availability
try:
    from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineSettings
    from PyQt6.QtWebEngineWidgets import QWebEngineView
    HAVE_WEBENGINE = True
    _WEBENGINE_ERROR = ""
except Exception as _we_err:
    HAVE_WEBENGINE = False
    _WEBENGINE_ERROR = str(_we_err)
    logger.info("PyQt6-WebEngine not available: %s", _we_err)


if HAVE_WEBENGINE:
    class PluginWebPage(QWebEnginePage):
        """
        Secure WebEnginePage for embedded plugin applications.
        - Restricts arbitrary file:// filesystem access.
        - Allows local loopback navigation (127.0.0.1, localhost) and about:blank.
        - Intercepts external link clicks and opens them in the system browser.
        """

        def __init__(self, parent: Optional[QWidget] = None):
            super().__init__(parent)

        def acceptNavigationRequest(self, url: QUrl, nav_type: QWebEnginePage.NavigationType, is_main_frame: bool) -> bool:
            scheme = url.scheme().lower()
            host = url.host().lower()

            # Disallow file:// scheme access for security
            if scheme == "file":
                logger.warning("Blocked file:// navigation in Plugin HUD: %s", url.toString())
                return False

            # Allow about:blank and local loopback origins
            if scheme == "about" or (scheme in ("http", "https") and host in ("127.0.0.1", "localhost")):
                return True

            # If user explicitly clicked an external link, open in default system browser
            if nav_type == QWebEnginePage.NavigationType.NavigationTypeLinkClicked:
                try:
                    webbrowser.open(url.toString())
                except Exception as exc:
                    logger.warning("Failed to open external URL in browser: %s", exc)
                return False

            # Allow data: or blob: sub-resources if needed
            if scheme in ("data", "blob"):
                return True

            return False


class PluginHudHost(QWidget):
    """
    Dedicated host widget for universal plugin interfaces in the central HUD display.
    Occupies page index 3 in the main HUD QStackedWidget.
    """

    def __init__(self, on_close: Optional[Callable[[str], None]] = None, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setStyleSheet(f"background: {C.BG}; border: none;")

        self._on_close = on_close
        self._active_owner: Optional[str] = None
        self._active_title: str = ""
        self._active_type: str = "url"
        self._active_url: str = ""

        self._lock = threading.Lock()
        self._state_snapshot: Dict[str, Any] = {
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
            "has_webengine": HAVE_WEBENGINE,
        }

        self._web_view: Optional[QWebEngineView] = None
        self._init_ui()

    def _init_ui(self) -> None:
        root_v = QVBoxLayout(self)
        root_v.setContentsMargins(6, 4, 6, 6)
        root_v.setSpacing(4)

        # ── Header bar ───────────────────────────────────────────────────────
        self._hdr = QHBoxLayout()
        self._hdr.setContentsMargins(4, 2, 4, 4)
        self._hdr.setSpacing(8)

        self._title_lbl = QLabel("◈  PLUGIN INTERFACE")
        self._title_lbl.setFont(hud_font(9, QFont.Weight.Bold))
        self._title_lbl.setStyleSheet(f"color: {C.PRI}; background: transparent; letter-spacing: 1px;")
        self._hdr.addWidget(self._title_lbl)

        self._status_badge = QLabel("● READY")
        self._status_badge.setFont(hud_font(8, QFont.Weight.Bold))
        self._status_badge.setStyleSheet(
            f"color: {C.GREEN}; background: rgba(0, 255, 136, 0.12); "
            f"border: 1px solid {C.GREEN_D}; border-radius: 3px; padding: 1px 6px;"
        )
        self._hdr.addWidget(self._status_badge)

        self._hdr.addStretch(1)

        # Reload button
        self._reload_btn = QPushButton("↻ RELOAD")
        self._reload_btn.setFont(hud_font(8, QFont.Weight.Bold))
        self._reload_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._reload_btn.setStyleSheet(f"""
            QPushButton {{
                color: {C.TEXT_DIM}; background: transparent;
                border: 1px solid {C.BORDER}; border-radius: 3px; padding: 2px 8px;
            }}
            QPushButton:hover {{ color: {C.PRI}; border-color: {C.PRI}; background: {C.PRI_GHO}; }}
        """)
        self._reload_btn.clicked.connect(self._handle_reload)
        self._hdr.addWidget(self._reload_btn)

        # Close button
        self._close_btn = QPushButton("✕ CLOSE")
        self._close_btn.setFont(hud_font(8, QFont.Weight.Bold))
        self._close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._close_btn.setStyleSheet(f"""
            QPushButton {{
                color: {C.TEXT_DIM}; background: transparent;
                border: 1px solid {C.BORDER}; border-radius: 3px; padding: 2px 8px;
            }}
            QPushButton:hover {{ color: {C.RED}; border-color: {C.RED}; background: rgba(255, 51, 85, 0.1); }}
        """)
        self._close_btn.clicked.connect(self._handle_close_clicked)
        self._hdr.addWidget(self._close_btn)

        root_v.addLayout(self._hdr)

        # ── Stacked display surfaces ─────────────────────────────────────────
        self._stack = QStackedWidget()
        self._stack.setStyleSheet(f"background: {C.BG}; border: none;")

        # Page 0: Web Container (lazily populated with QWebEngineView)
        self._web_container = QWidget()
        self._web_v = QVBoxLayout(self._web_container)
        self._web_v.setContentsMargins(0, 0, 0, 0)
        self._web_v.setSpacing(0)
        self._stack.addWidget(self._web_container)

        # Page 1: Native Spec Container (QScrollArea for declarative cards/metrics)
        self._spec_scroll = QScrollArea()
        self._spec_scroll.setWidgetResizable(True)
        self._spec_scroll.setStyleSheet(f"background: {C.BG}; border: none;")
        self._spec_body = QWidget()
        self._spec_body.setStyleSheet(f"background: {C.BG};")
        self._spec_v = QVBoxLayout(self._spec_body)
        self._spec_v.setContentsMargins(12, 12, 12, 12)
        self._spec_v.setSpacing(10)
        self._spec_v.addStretch(1)
        self._spec_scroll.setWidget(self._spec_body)
        self._stack.addWidget(self._spec_scroll)

        # Page 2: Text Container
        self._text_edit = QTextEdit()
        self._text_edit.setReadOnly(True)
        self._text_edit.setFont(mono_font(9.5))
        self._text_edit.setStyleSheet(f"""
            QTextEdit {{
                background: {C.PANEL}; color: {C.TEXT};
                border: 1px solid {C.BORDER}; border-radius: 4px;
                padding: 10px; line-height: 1.4;
            }}
        """)
        self._stack.addWidget(self._text_edit)

        # Page 3: Fallback / Error Container
        self._error_cont = QWidget()
        err_v = QVBoxLayout(self._error_cont)
        err_v.setContentsMargins(20, 20, 20, 20)
        err_v.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._error_lbl = QLabel()
        self._error_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._error_lbl.setFont(hud_font(9.5))
        self._error_lbl.setStyleSheet(f"color: {C.TEXT_DIM}; line-height: 1.5;")
        err_v.addWidget(self._error_lbl)
        self._error_btn = QPushButton("OPEN IN SYSTEM BROWSER")
        self._error_btn.setFont(hud_font(8.5, QFont.Weight.Bold))
        self._error_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._error_btn.setStyleSheet(f"""
            QPushButton {{
                background: {C.PRI_GHO}; color: {C.PRI};
                border: 1px solid {C.PRI}; border-radius: 3px; padding: 6px 14px;
            }}
            QPushButton:hover {{ background: {C.PRI_DIM}; color: #fff; }}
        """)
        self._error_btn.setVisible(False)
        self._error_btn.clicked.connect(self._handle_open_external)
        err_v.addWidget(self._error_btn, alignment=Qt.AlignmentFlag.AlignCenter)
        self._stack.addWidget(self._error_cont)

        root_v.addWidget(self._stack, stretch=1)

    # ── Display Management (Qt Main Thread) ──────────────────────────────────

    def show_view(
        self,
        plugin_id: str,
        title: str,
        content: Any,
        content_type: str = "url",
        replace: bool = True,
    ) -> bool:
        """
        Display a plugin interface inside the central HUD.
        Must be executed on the Qt main thread.
        """
        pid = str(plugin_id or "").strip()
        c_type = str(content_type or "url").lower().strip()
        disp_title = str(title or pid.upper()).strip()

        # Ownership and Conflict check
        if self._active_owner is not None and self._active_owner != pid:
            if not replace:
                logger.info(
                    "Display request from '%s' rejected: view already owned by '%s' and replace=False",
                    pid, self._active_owner
                )
                return False
            # Cleanly teardown previous owner's interface before replacing
            logger.info("Plugin '%s' replacing active view of '%s'", pid, self._active_owner)
            self._cleanup_active_content()

        self._active_owner = pid
        self._active_title = disp_title
        self._active_type = c_type

        # Update header
        self._title_lbl.setText(f"◈  {disp_title[:44]}")
        self._set_status("● READY", C.GREEN)

        success = True
        try:
            if c_type == "url":
                success = self._render_url(str(content or ""))
            elif c_type == "html":
                success = self._render_html(str(content or ""))
            elif c_type == "spec":
                success = self._render_spec(content)
            elif c_type == "text":
                success = self._render_text(str(content or ""))
            else:
                self._render_error(f"Unsupported content type: {c_type}")
                success = False
        except Exception as exc:
            logger.exception("Error rendering plugin view '%s': %s", pid, exc)
            self._render_error(f"Render Error: {exc}")
            success = False

        self._update_snapshot(active=True, has_error=not success)
        return success

    def update_view(
        self,
        plugin_id: str,
        content: Any,
        content_type: Optional[str] = None,
    ) -> bool:
        """
        Update an existing active view without destroying the surface unnecessarily.
        """
        pid = str(plugin_id or "").strip()
        if self._active_owner != pid:
            logger.warning("Rejecting update: owner '%s' does not match '%s'", self._active_owner, pid)
            return False

        c_type = str(content_type or self._active_type).lower().strip()

        try:
            self._set_status("● UPDATING", C.PRI)

            if c_type == "url" and self._web_view is not None:
                new_url = str(content or "").strip()
                if new_url and new_url != self._active_url:
                    self._active_url = new_url
                    self._web_view.setUrl(QUrl(new_url))
            elif c_type == "html" and self._web_view is not None:
                self._web_view.setHtml(str(content or ""), QUrl("http://127.0.0.1/"))
            elif c_type == "spec":
                self._render_spec(content)
            elif c_type == "text":
                self._render_text(str(content or ""))
            elif self._web_view is not None and isinstance(content, str) and content.startswith("javascript:"):
                # Run dynamic JavaScript command in existing web page
                js_code = content[len("javascript:"):].strip()
                self._web_view.page().runJavaScript(js_code)

            self._set_status("● LIVE", C.GREEN)
            self._update_snapshot(active=True, has_error=False)
            return True
        except Exception as exc:
            logger.exception("Error updating plugin view '%s': %s", pid, exc)
            self._set_status("● ERROR", C.RED)
            return False

    def close_view(self, plugin_id: str) -> bool:
        """
        Close the active plugin view and return HUD to the avatar.
        Only the current owner or wildcard '*' (close button) can close the view.
        """
        pid = str(plugin_id or "").strip()
        if self._active_owner is None:
            return True

        if pid != "*" and self._active_owner != pid:
            logger.warning("Rejecting close: '%s' cannot close view owned by '%s'", pid, self._active_owner)
            return False

        closed_owner = self._active_owner
        self._cleanup_active_content()
        self._active_owner = None
        self._active_title = ""
        self._active_type = "url"
        self._active_url = ""

        self._update_snapshot(active=False)

        if self._on_close:
            self._on_close(closed_owner)

        return True

    def is_active(self) -> bool:
        """Whether a plugin view is currently active."""
        return self._active_owner is not None

    def get_state_snapshot(self) -> Dict[str, Any]:
        """Thread-safe state snapshot safe to call from any worker thread."""
        with self._lock:
            return dict(self._state_snapshot)

    def cleanup(self) -> None:
        """Safe teardown on application shutdown."""
        self._cleanup_active_content()
        if self._web_view is not None:
            try:
                self._web_view.setPage(None)
                self._web_view.deleteLater()
            except Exception:
                pass
            self._web_view = None

    # ── Renderers ────────────────────────────────────────────────────────────

    def _render_url(self, url_str: str) -> bool:
        url_clean = url_str.strip()
        self._active_url = url_clean

        if not HAVE_WEBENGINE:
            self._render_error(
                f"Embedded Web application display requires <b>PyQt6-WebEngine</b>.<br>"
                f"<span style='color:{C.TEXT};'>{url_clean}</span><br><br>"
                f"Install with: <code>pip install PyQt6-WebEngine</code>",
                can_open_external=True
            )
            return False

        self._ensure_web_view()
        if self._web_view is None:
            return False

        self._set_status("● LOADING", C.ACC2)
        self._web_view.setUrl(QUrl(url_clean))
        self._stack.setCurrentIndex(0)
        self._reload_btn.setVisible(True)
        return True

    def _render_html(self, html_content: str) -> bool:
        if not HAVE_WEBENGINE:
            # Fallback to rich text in QTextEdit
            self._text_edit.setHtml(html_content)
            self._stack.setCurrentIndex(2)
            self._reload_btn.setVisible(False)
            return True

        self._ensure_web_view()
        if self._web_view is None:
            return False

        self._set_status("● LOADING", C.ACC2)
        self._web_view.setHtml(html_content, QUrl("http://127.0.0.1/"))
        self._stack.setCurrentIndex(0)
        self._reload_btn.setVisible(True)
        return True

    def _render_text(self, text_content: str) -> bool:
        self._text_edit.setPlainText(text_content)
        self._stack.setCurrentIndex(2)
        self._reload_btn.setVisible(False)
        self._set_status("● READY", C.GREEN)
        return True

    def _render_spec(self, spec_data: Union[dict, list, str]) -> bool:
        """
        Declarative native UI specification builder.
        Constructs PyQt6 widgets dynamically without arbitrary code execution.
        """
        if isinstance(spec_data, str):
            try:
                spec_data = json.loads(spec_data)
            except Exception as e:
                self._render_error(f"Malformed specification JSON: {e}")
                return False

        # Clear existing spec widgets
        while self._spec_v.count() > 1:
            child = self._spec_v.takeAt(0)
            if child.widget():
                child.widget().deleteLater()

        items = spec_data.get("items", []) if isinstance(spec_data, dict) else (spec_data if isinstance(spec_data, list) else [])

        for item in items:
            widget = self._build_spec_element(item)
            if widget:
                self._spec_v.insertWidget(self._spec_v.count() - 1, widget)

        self._stack.setCurrentIndex(1)
        self._reload_btn.setVisible(False)
        self._set_status("● READY", C.GREEN)
        return True

    def _build_spec_element(self, element: dict) -> Optional[QWidget]:
        """Construct a native widget from a declarative element dict."""
        if not isinstance(element, dict):
            return None

        etype = (element.get("type") or "text").lower().strip()

        if etype in ("heading", "title"):
            lbl = QLabel(str(element.get("text", "")))
            size = float(element.get("size", 10.5))
            lbl.setFont(hud_font(size, QFont.Weight.Bold))
            color = element.get("color", C.PRI)
            lbl.setStyleSheet(f"color: {color}; background: transparent; letter-spacing: 0.8px;")
            return lbl

        elif etype in ("text", "label"):
            lbl = QLabel(str(element.get("text", "")))
            lbl.setWordWrap(True)
            lbl.setFont(hud_font(element.get("size", 9.0)))
            color = element.get("color", C.TEXT)
            lbl.setStyleSheet(f"color: {color}; background: transparent; line-height: 1.3;")
            return lbl

        elif etype in ("metric", "stat"):
            card = QFrame()
            card.setStyleSheet(f"""
                QFrame {{
                    background: {C.PANEL}; border: 1px solid {C.BORDER};
                    border-radius: 4px; padding: 8px 12px;
                }}
            """)
            h = QHBoxLayout(card)
            h.setContentsMargins(0, 0, 0, 0)
            v = QVBoxLayout()
            v.setSpacing(2)

            val_lbl = QLabel(str(element.get("value", "0")))
            val_lbl.setFont(hud_font(13, QFont.Weight.Bold))
            val_lbl.setStyleSheet(f"color: {element.get('color', C.PRI)};")
            v.addWidget(val_lbl)

            lbl = QLabel(str(element.get("label", "")).upper())
            lbl.setFont(hud_font(7.5, QFont.Weight.Bold))
            lbl.setStyleSheet(f"color: {C.TEXT_DIM}; letter-spacing: 0.8px;")
            v.addWidget(lbl)

            h.addLayout(v)
            if element.get("badge"):
                badge = QLabel(str(element["badge"]).upper())
                badge.setFont(hud_font(7.5, QFont.Weight.Bold))
                badge.setStyleSheet(f"""
                    color: {C.GREEN}; background: rgba(0, 255, 136, 0.12);
                    border: 1px solid {C.GREEN_D}; border-radius: 3px; padding: 2px 6px;
                """)
                h.addWidget(badge, alignment=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            return card

        elif etype == "progress":
            bar = QProgressBar()
            bar.setFixedHeight(12)
            bar.setTextVisible(False)
            bar.setRange(0, int(element.get("max", 100)))
            bar.setValue(int(element.get("value", 0)))
            bar.setStyleSheet(f"""
                QProgressBar {{
                    background: {C.PANEL}; border: 1px solid {C.BORDER}; border-radius: 3px;
                }}
                QProgressBar::chunk {{
                    background: {element.get('color', C.PRI)}; border-radius: 2px;
                }}
            """)
            return bar

        elif etype == "divider":
            line = QFrame()
            line.setFrameShape(QFrame.Shape.HLine)
            line.setStyleSheet(f"color: {C.BORDER}; max-height: 1px; margin: 4px 0;")
            return line

        elif etype == "table":
            headers = element.get("headers", [])
            rows = element.get("rows", [])
            table = QTableWidget(len(rows), len(headers))
            table.setHorizontalHeaderLabels([str(h) for h in headers])
            table.verticalHeader().setVisible(False)
            table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
            table.setStyleSheet(f"""
                QTableWidget {{
                    background: {C.PANEL}; color: {C.TEXT};
                    border: 1px solid {C.BORDER}; gridline-color: {C.BORDER_A};
                }}
                QHeaderView::section {{
                    background: {C.PANEL2}; color: {C.PRI};
                    border: 1px solid {C.BORDER}; padding: 4px; font-weight: bold;
                }}
            """)
            for r_idx, row in enumerate(rows):
                for c_idx, cell in enumerate(row):
                    item = QTableWidgetItem(str(cell))
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                    table.setItem(r_idx, c_idx, item)
            table.setFixedHeight(min(220, 36 + len(rows) * 28))
            return table

        elif etype in ("card", "container"):
            card = QFrame()
            card.setStyleSheet(f"""
                QFrame {{
                    background: {C.PANEL}; border: 1px solid {C.BORDER};
                    border-radius: 4px; padding: 10px;
                }}
            """)
            layout = QVBoxLayout(card)
            layout.setContentsMargins(8, 8, 8, 8)
            layout.setSpacing(6)
            for sub in element.get("items", []):
                sub_w = self._build_spec_element(sub)
                if sub_w:
                    layout.addWidget(sub_w)
            return card

        return None

    def _render_error(self, message: str, can_open_external: bool = False) -> None:
        self._error_lbl.setText(message)
        self._error_btn.setVisible(can_open_external)
        self._stack.setCurrentIndex(3)
        self._reload_btn.setVisible(False)
        self._set_status("● ATTENTION", C.ACC2)

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _ensure_web_view(self) -> None:
        if not HAVE_WEBENGINE:
            return
        if self._web_view is not None:
            return

        try:
            self._web_view = QWebEngineView()
            self._web_view.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
            self._web_view.setStyleSheet("background: #000; border: none;")

            # Configure security & features
            page = PluginWebPage(self._web_view)
            settings = page.settings()
            settings.setAttribute(QWebEngineSettings.WebAttribute.JavascriptCanAccessClipboard, True)
            settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
            settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, False)
            settings.setAttribute(QWebEngineSettings.WebAttribute.ScrollAnimatorEnabled, True)

            self._web_view.setPage(page)

            # Signal bindings for loading progress
            self._web_view.loadStarted.connect(lambda: self._set_status("● LOADING", C.ACC2))
            self._web_view.loadFinished.connect(self._on_web_load_finished)

            self._web_v.addWidget(self._web_view)
        except Exception as exc:
            logger.exception("Failed to initialize QWebEngineView: %s", exc)
            self._web_view = None

    def _on_web_load_finished(self, ok: bool) -> None:
        if ok:
            self._set_status("● LIVE", C.GREEN)
            self._update_snapshot(active=True, is_loading=False, has_error=False)
        else:
            self._set_status("● LOAD ERROR", C.RED)
            self._update_snapshot(active=True, is_loading=False, has_error=True, error_msg="Page failed to load")

    def _cleanup_active_content(self) -> None:
        if self._web_view is not None:
            try:
                self._web_view.stop()
                self._web_view.setUrl(QUrl("about:blank"))
            except Exception:
                pass
        self._text_edit.clear()
        while self._spec_v.count() > 1:
            child = self._spec_v.takeAt(0)
            if child.widget():
                child.widget().deleteLater()

    def _set_status(self, text: str, color: str) -> None:
        self._status_badge.setText(text)
        self._status_badge.setStyleSheet(f"""
            color: {color}; background: rgba(0, 0, 0, 0.3);
            border: 1px solid {color}; border-radius: 3px; padding: 1px 6px;
        """)

    def _update_snapshot(self, **kwargs) -> None:
        with self._lock:
            self._state_snapshot.update({
                "active": kwargs.get("active", self._active_owner is not None),
                "plugin_id": self._active_owner,
                "title": self._active_title,
                "content_type": self._active_type,
                "url": self._active_url,
                "is_visible": self._active_owner is not None,
                "has_error": kwargs.get("has_error", False),
                "error_message": kwargs.get("error_msg", ""),
                "owner": self._active_owner,
            })

    def _handle_reload(self) -> None:
        if self._active_type == "url" and self._web_view is not None:
            self._web_view.reload()

    def _handle_close_clicked(self) -> None:
        self.close_view("*")

    def _handle_open_external(self) -> None:
        if self._active_url:
            try:
                webbrowser.open(self._active_url)
            except Exception as e:
                logger.warning("Could not open URL: %s", e)
