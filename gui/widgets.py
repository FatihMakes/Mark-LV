"""gui/widgets.py — Small building blocks of the HUD: metric bars, the activity log, file drop, camera preview.

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
    QBrush, QColor, QConicalGradient, QCursor, QDragEnterEvent, QDropEvent, QFont,
    QFontDatabase, QKeySequence, QLinearGradient, QPainter, QPainterPath,
    QPen, QPixmap, QRadialGradient, QShortcut,
)
from PyQt6.QtWidgets import (
    QApplication, QComboBox, QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QMainWindow, QMenu, QPushButton, QScrollArea, QSizePolicy, QSplitter,
    QGraphicsScene, QGraphicsView, QGraphicsPixmapItem,
    QStackedWidget, QTextEdit, QVBoxLayout, QWidget, QProgressBar, QSlider,
)

from gui.theme import (
    C,
    _OS,
    qcol,
)

from collections import deque

from PyQt6.QtGui import QPolygonF, QTextBlockFormat, QTextCharFormat

from gui.style import (blend, chamfer, hud_font, px_font, scroll_css, with_alpha)


class MetricBar(QWidget):

    def __init__(self, label: str, color: str = C.PRI, parent=None):
        super().__init__(parent)
        self._label = label
        self._color = color
        self._value = 0.0       # 0–100
        self._text  = "--"
        self.setFixedHeight(38)
        self.setMinimumWidth(80)

    def set_value(self, pct: float, text: str):
        v = max(0.0, min(100.0, pct))
        if v == self._value and text == self._text:
            return          # unchanged — skip the repaint
        self._value = v
        self._text  = text
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        if not p.isActive():
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W, H = self.width(), self.height()

        p.setBrush(QBrush(qcol(C.PANEL2)))
        p.setPen(QPen(qcol(C.BORDER_A), 1))
        p.drawRoundedRect(QRectF(1, 1, W - 2, H - 2), 4, 4)

        bar_h   = 4
        bar_y   = H - bar_h - 5
        bar_w   = W - 12
        bar_x   = 6
        fill_w  = int(bar_w * self._value / 100)

        p.setBrush(QBrush(qcol(C.BAR_BG)))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(QRectF(bar_x, bar_y, bar_w, bar_h), 2, 2)

        if self._value > 85:
            bar_col = qcol(C.RED)
        elif self._value > 65:
            bar_col = qcol(C.ACC)
        else:
            bar_col = qcol(self._color)

        if fill_w > 0:
            p.setBrush(QBrush(bar_col))
            p.drawRoundedRect(QRectF(bar_x, bar_y, fill_w, bar_h), 2, 2)

        p.setFont(hud_font(7, QFont.Weight.Bold))
        p.setPen(QPen(qcol(C.TEXT_DIM), 1))
        p.drawText(QRectF(8, 5, 50, 14), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, self._label)

        p.setFont(hud_font(9, QFont.Weight.Bold))
        p.setPen(QPen(bar_col if self._text != "--" else qcol(C.TEXT_DIM), 1))
        p.drawText(QRectF(0, 4, W - 6, 16), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, self._text)

        p.end()

class LogWidget(QTextEdit):
    """The activity log, laid out as a transcript.

    Every line still arrives as the plain "Speaker: text" string it always has
    — through append_log, from any thread — so nothing that writes to the log
    or listens to it changes. What changed is the reading: the speaker becomes
    a small label with the time beside it, and the words are typed out beneath
    in the speaker's colour."""

    _sig = pyqtSignal(str)
    # The short all-caps prefixes the app itself writes, shown as the label.
    _TAGS = {"sys": "SYS", "err": "ERR", "file": "FILE", "quiz": "QUIZ"}

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        # Cap scrollback so an hours-long session can't grow the document
        # without bound. Each line is two blocks now (label, then words), so
        # the cap is twice the old one-block-per-line figure.
        self.document().setMaximumBlockCount(1200)
        self.document().setDocumentMargin(4)
        self.setFont(px_font(13))
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setStyleSheet(f"""
            QTextEdit {{
                background: transparent;
                color: {C.TEXT};
                border: none;
                selection-background-color: {C.PRI_GHO};
            }}
        """ + scroll_css())
        self._queue: list[str] = []
        self._typing  = False
        self._text    = ""
        self._pos     = 0
        self._tag     = "sys"
        self._fmt     = QTextCharFormat()
        self._ai_name_lc = "jarvis"   # updated when assistant name changes
        self._tmr = QTimer(self)
        self._tmr.timeout.connect(self._step)
        self._sig.connect(self._enqueue)

    def append_log(self, text: str):
        self._sig.emit(text)

    def _enqueue(self, text: str):
        self._queue.append(text)
        if not self._typing:
            self._next()

    def _split(self, text: str) -> tuple[str, str, str]:
        """(tag, label, words) for one log line."""
        head, sep, rest = text.partition(":")
        tl = text.lower()
        if tl.startswith("you:"):
            return "you", "YOU", rest.strip()
        if sep and (tl.startswith(f"{self._ai_name_lc}:") or tl.startswith("jarvis:")):
            return "ai", head.strip().upper(), rest.strip()
        key = head.strip().lower()
        if sep and key in self._TAGS:
            tag = key if key in ("err", "file") else "sys"
            return tag, self._TAGS[key], rest.strip()
        # Anything else keeps the old rule: an error if it says so.
        return ("err" if "err" in tl else "sys"), "SYS", text.strip()

    @staticmethod
    def _colours(tag: str) -> tuple[QColor, QColor]:
        """(label colour, words colour) for a tag."""
        return {
            "you":  (qcol(C.WHITE), qcol(C.WHITE)),
            "ai":   (qcol(C.PRI), qcol(C.TEXT)),
            "err":  (qcol(C.RED), qcol(C.RED)),
            "file": (qcol(C.GREEN), qcol(C.TEXT)),
            # SYS lines are the bulk of the log, so they sit back. TEXT_MED
            # follows the theme and drops the contrast to a level you read past.
            "sys":  (qcol(C.TEXT_DIM), qcol(C.TEXT_MED)),
        }.get(tag, (qcol(C.TEXT_DIM), qcol(C.TEXT)))

    def _next(self):
        if not self._queue:
            self._typing = False
            return
        self._typing = True
        tag, label, words = self._split(self._queue.pop(0))
        self._tag, self._text, self._pos = tag, words, 0
        label_col, text_col = self._colours(tag)

        cur = self.textCursor()
        cur.movePosition(cur.MoveOperation.End)
        head = QTextBlockFormat()
        if self.document().isEmpty():
            cur.setBlockFormat(head)
        else:
            head.setTopMargin(9)
            cur.insertBlock(head)
        lf = QTextCharFormat()
        lf.setFont(px_font(10, QFont.Weight.DemiBold, spacing=1.6))
        lf.setForeground(QBrush(label_col))
        cur.insertText(label, lf)
        sf = QTextCharFormat()
        sf.setFont(px_font(10, mono=True))
        sf.setForeground(QBrush(qcol(C.TEXT_DIM)))
        cur.insertText("   " + time.strftime("%H:%M:%S"), sf)
        body = QTextBlockFormat()
        body.setTopMargin(1)
        cur.insertBlock(body)
        self._fmt = QTextCharFormat()
        self._fmt.setFont(px_font(13))
        self._fmt.setForeground(QBrush(text_col))
        self.setTextCursor(cur)
        self.ensureCursorVisible()
        self._tmr.start(6)

    def _step(self):
        if self._pos < len(self._text):
            cur = self.textCursor()
            cur.movePosition(cur.MoveOperation.End)
            cur.insertText(self._text[self._pos], self._fmt)
            self.setTextCursor(cur)
            self.ensureCursorVisible()
            self._pos += 1
        else:
            self._tmr.stop()
            QTimer.singleShot(20, self._next)

_FILE_ICONS = {
    "image":   ("🖼", "#00d4ff"), "video":   ("🎬", "#ff6b00"),
    "audio":   ("🎵", "#cc44ff"), "pdf":     ("📄", "#ff4444"),
    "word":    ("📝", "#4488ff"), "excel":   ("📊", "#44bb44"),
    "code":    ("💻", "#ffcc00"), "archive": ("📦", "#ff8844"),
    "pptx":    ("📊", "#ff6622"), "text":    ("📃", "#aaaaaa"),
    "data":    ("🔧", "#88ddff"), "unknown": ("📎", "#888888"),
    "folder":  ("📁", "#ffcc00"),
}
_EXT_TO_CAT = {
    **dict.fromkeys(["jpg","jpeg","png","gif","webp","bmp","tiff","svg","ico"], "image"),
    **dict.fromkeys(["mp4","avi","mov","mkv","wmv","flv","webm","m4v"],         "video"),
    **dict.fromkeys(["mp3","wav","ogg","m4a","aac","flac","wma","opus"],        "audio"),
    **dict.fromkeys(["pdf"],                                                     "pdf"),
    **dict.fromkeys(["doc","docx"],                                              "word"),
    **dict.fromkeys(["xls","xlsx","ods"],                                        "excel"),
    **dict.fromkeys(["ppt","pptx"],                                              "pptx"),
    **dict.fromkeys(["py","js","ts","jsx","tsx","html","css","java","c","cpp",
                     "cs","go","rs","rb","php","swift","kt","sh","sql","lua"],   "code"),
    **dict.fromkeys(["zip","rar","tar","gz","7z","bz2","xz"],                   "archive"),
    **dict.fromkeys(["txt","md","rst","log"],                                    "text"),
    **dict.fromkeys(["csv","tsv","json","xml"],                                  "data"),
}

def _file_category(path: Path) -> str:
    if path.is_dir():
        return "folder"
    return _EXT_TO_CAT.get(path.suffix.lower().lstrip("."), "unknown")

def _fmt_size(size: int) -> str:
    if   size < 1024:    return f"{size} B"
    elif size < 1024**2: return f"{size/1024:.1f} KB"
    elif size < 1024**3: return f"{size/1024**2:.1f} MB"
    else:                return f"{size/1024**3:.1f} GB"


# Folders nobody means when they say "this project": dependencies, build output,
# version control. The same list dev_agent skips, so the count on the card is
# the count of files the coding model will actually look at.
_SKIP_DIRS = {".git", ".hg", ".svn", "node_modules", "venv", ".venv", "env",
              "__pycache__", ".mypy_cache", ".pytest_cache", "dist", "build",
              ".idea", ".vscode", ".next", "target", ".jarvis_backup"}


def _count_files(folder: Path, cap: int = 5000) -> int:
    """Files in a project folder, stopping at `cap` — a whole drive dropped by
    mistake must not freeze the window while it is counted."""
    import os
    n = 0
    for _root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        n += len(files)
        if n >= cap:
            return cap
    return n


class FileDropZone(QWidget):
    file_selected = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(46)
        self._current_file: str | None = None
        self._folder_files = 0
        self._hovering  = False
        self._drag_over = False
        self._dash_offset = 0.0
        self._anim_tmr = QTimer(self)
        self._anim_tmr.timeout.connect(self._animate)
        self._anim_tmr.start(40)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self._canvas = _DropCanvas(self)
        layout.addWidget(self._canvas)

    def _animate(self):
        # The marching-ants dashed border is only meaningful while the user is
        # hovering or dragging a file over the zone. When idle, skip the repaint
        # entirely instead of redrawing the whole zone 25×/s forever — that idle
        # repaint held the GIL and stole time from the audio/response threads.
        if not (self._hovering or self._drag_over):
            return
        self._dash_offset = (self._dash_offset + 0.8) % 20
        self._canvas.update()

    def dragEnterEvent(self, e: QDragEnterEvent):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
            self._drag_over = True; self._canvas.update()

    def dragLeaveEvent(self, e):
        self._drag_over = False; self._canvas.update()

    def dropEvent(self, e: QDropEvent):
        self._drag_over = False
        urls = e.mimeData().urls()
        if urls:
            path = urls[0].toLocalFile()
            if Path(path).is_file() or Path(path).is_dir():
                self._set_file(path)
        self._canvas.update()

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._browse()

    def enterEvent(self, e):
        self._hovering = True; self._canvas.update()

    def leaveEvent(self, e):
        self._hovering = False; self._canvas.update()

    def current_file(self) -> str | None:
        return self._current_file

    def clear_file(self):
        self._current_file = None; self._canvas.update()

    def _browse(self):
        # Qt has no single dialog that picks "a file or a folder", so the click
        # asks which — a folder is what the coding model works on.
        menu = QMenu(self)
        menu.setStyleSheet(
            f"QMenu {{ background: {C.DARK}; color: {C.TEXT}; "
            f"border: 1px solid {C.BORDER_B}; }}"
            f"QMenu::item {{ padding: 6px 18px; }}"
            f"QMenu::item:selected {{ background: {C.PRI_GHO}; }}")
        pick_file = menu.addAction("📄  Choose a file…")
        pick_dir = menu.addAction("📁  Choose a folder…  (for coding)")
        chosen = menu.exec(QCursor.pos())
        if chosen is pick_dir:
            path = QFileDialog.getExistingDirectory(
                self, "Select a project folder for JARVIS", str(Path.home()))
            if path:
                self._set_file(path)
            return
        if chosen is not pick_file:
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Select a file for JARVIS", str(Path.home()),
            "All Files (*.*);;"
            "Images (*.jpg *.jpeg *.png *.gif *.webp *.bmp *.svg);;"
            "Documents (*.pdf *.docx *.txt *.md *.pptx);;"
            "Data (*.csv *.xlsx *.json *.xml);;"
            "Code (*.py *.js *.ts *.html *.css *.java *.cpp *.go);;"
            "Audio (*.mp3 *.wav *.ogg *.m4a *.aac *.flac);;"
            "Video (*.mp4 *.avi *.mov *.mkv *.wmv *.webm);;"
            "Archives (*.zip *.rar *.tar *.gz *.7z)",
        )
        if path:
            self._set_file(path)

    def _set_file(self, path: str):
        self._current_file = path
        self._folder_files = _count_files(Path(path)) if Path(path).is_dir() else 0
        self._canvas.update()
        self.file_selected.emit(path)


class _DropCanvas(QWidget):
    """The drop zone as one slim row: an icon and a line of text — or, once a
    file is loaded, its name and size with a ✕ at the right end to clear it."""

    def __init__(self, zone: FileDropZone):
        super().__init__(zone)
        self._z = zone

    def paintEvent(self, _):
        p = QPainter(self)
        if not p.isActive():
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        z = self._z
        W, H = self.width(), self.height()

        if z._current_file:
            edge, fill = qcol(C.GREEN), blend(C.GREEN, 0.07)
        elif z._drag_over:
            edge, fill = qcol(C.PRI), blend(C.PRI, 0.14)
        elif z._hovering:
            edge, fill = blend(C.PRI, 0.75), blend(C.PRI, 0.08)
        else:
            edge, fill = blend(C.PRI, 0.38), blend(C.PRI, 0.04)
        p.setBrush(QBrush(fill))
        pen = QPen(edge, 1, Qt.PenStyle.SolidLine if z._current_file else Qt.PenStyle.DashLine)
        pen.setDashOffset(z._dash_offset)
        p.setPen(pen)
        p.drawPolygon(chamfer(QRectF(0.5, 0.5, W - 1, H - 1), 7))

        if z._current_file:
            self._paint_file(p, W, H)
        elif z._drag_over:
            self._paint_drag_over(p, W, H)
        else:
            self._paint_idle(p, W, H, z._hovering)
        p.end()

    def _paint_idle(self, p, W, H, hover):
        cy = H / 2
        p.setPen(QPen(qcol(C.PRI) if hover else blend(C.PRI, 0.8), 1.6))
        p.drawLine(QLineF(14, cy, 26, cy))
        p.drawLine(QLineF(20, cy - 6, 20, cy + 6))
        p.setFont(px_font(11, QFont.Weight.DemiBold, spacing=1.4))
        p.setPen(QPen(qcol(C.TEXT) if hover else qcol(C.TEXT_MED)))
        p.drawText(QRectF(36, 0, W - 44, H), Qt.AlignmentFlag.AlignVCenter,
                   p.fontMetrics().elidedText("DROP A FILE OR FOLDER  ·  OR CLICK",
                                              Qt.TextElideMode.ElideRight, int(W - 44)))

    def _paint_drag_over(self, p, W, H):
        p.setFont(px_font(12, QFont.Weight.DemiBold, spacing=2))
        p.setPen(QPen(qcol(C.PRI)))
        p.drawText(QRectF(0, 0, W, H), Qt.AlignmentFlag.AlignCenter, "RELEASE TO LOAD")

    def _paint_file(self, p, W, H):
        path = Path(self._z._current_file)
        cat = _file_category(path)
        icon, icon_col = _FILE_ICONS.get(cat, _FILE_ICONS["unknown"])
        try:
            size_str = _fmt_size(path.stat().st_size)
        except OSError:
            size_str = "?"
        ext_str = path.suffix.upper().lstrip(".") or "FILE"
        if cat == "folder":
            n = self._z._folder_files
            ext_str, size_str = "FOLDER", f"{n}{'+' if n >= 5000 else ''} files"

        p.setFont(QFont("Segoe UI Emoji", 13) if _OS == "Windows" else QFont("Arial", 13))
        p.setPen(QPen(qcol(icon_col)))
        p.drawText(QRectF(8, 0, 26, H), Qt.AlignmentFlag.AlignCenter, icon)

        tw = W - 40 - 34
        p.setFont(px_font(12, QFont.Weight.DemiBold))
        p.setPen(QPen(qcol(C.WHITE)))
        name = p.fontMetrics().elidedText(path.name, Qt.TextElideMode.ElideMiddle, int(tw))
        p.drawText(QRectF(40, 2, tw, H / 2 - 1),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom, name)
        p.setFont(px_font(10, mono=True))
        p.setPen(QPen(qcol(C.TEXT_DIM)))
        p.drawText(QRectF(40, H / 2 + 1, tw, H / 2 - 3),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop, f"{ext_str}  ·  {size_str}")

        # the ✕ that clears it (mousePressEvent treats the last 34 px as this)
        p.setPen(QPen(with_alpha(C.RED, 0.8), 1.6))
        xc, yc = W - 17, H / 2
        p.drawLine(QLineF(xc - 5, yc - 5, xc + 5, yc + 5))
        p.drawLine(QLineF(xc + 5, yc - 5, xc - 5, yc + 5))

    def mousePressEvent(self, e):
        z = self._z
        if z._current_file and e.pos().x() > self.width() - 34:
            z.clear_file()
        else:
            z.mousePressEvent(e)


class _CameraPreview(QWidget):
    """Floating overlay that briefly shows what the camera captured."""

    _W, _H = 244, 188

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            _CameraPreview {{
                background: rgba(0, 6, 10, 242);
                border: 1px solid {C.PRI};
                border-radius: 6px;
            }}
        """)
        self.setFixedWidth(self._W)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 5, 6, 6)
        lay.setSpacing(4)

        hdr = QHBoxLayout()
        title = QLabel("◈  VISUAL INPUT")
        title.setFont(hud_font(7, QFont.Weight.Bold))
        title.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        hdr.addWidget(title)
        hdr.addStretch()
        close_btn = QPushButton("✕")
        close_btn.setFixedSize(16, 16)
        close_btn.setFont(hud_font(8))
        close_btn.setStyleSheet(
            f"color: {C.TEXT_DIM}; background: transparent; border: none;"
        )
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.clicked.connect(self.hide)
        hdr.addWidget(close_btn)
        lay.addLayout(hdr)

        self._img_lbl = QLabel()
        self._img_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._img_lbl.setStyleSheet("background: transparent;")
        lay.addWidget(self._img_lbl)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)

        self.hide()

    def show_frame(self, img_bytes: bytes) -> None:
        px = QPixmap()
        px.loadFromData(img_bytes)
        if not px.isNull():
            max_w = self._W - 12
            scaled = px.scaled(
                max_w, 160,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self._img_lbl.setPixmap(scaled)
            self._img_lbl.setFixedSize(scaled.width(), scaled.height())
            self.adjustSize()
        self.show()
        self.raise_()
        self._timer.start(6_000)   # auto-dismiss after 6 s


# ═════════════════════════════════════════════════════════════════════════════
#  Mark LVII building blocks
# ═════════════════════════════════════════════════════════════════════════════
# All painted rather than styled: a stylesheet cannot cut a corner or light an
# edge, and painting reads the palette at paint time, so every one of these
# re-themes live with the accent picker at no cost.

class HoloPanel(QWidget):
    """A glass "screen": a translucent panel with two cut corners, lit edges and
    a header strip carrying a title and a short code. Children go in `body`;
    small buttons can sit at the right of the header (add_header_widget)."""

    HEADER = 30

    def __init__(self, title: str = "", code: str = "", parent=None, pad: int = 10):
        super().__init__(parent)
        self._title, self._code = str(title or ""), str(code or "")
        self._header_widgets: list[QWidget] = []
        self.body = QVBoxLayout(self)
        top = self.HEADER + 5 if title else pad
        self.body.setContentsMargins(pad + 2, top, pad + 2, pad + 2)
        self.body.setSpacing(6)

    def set_title(self, text: str) -> None:
        self._title = str(text or "")
        self.update()

    def set_code(self, text: str) -> None:
        self._code = str(text or "")
        self.update()

    def add_header_widget(self, w: QWidget) -> None:
        w.setParent(self)
        self._header_widgets.append(w)
        w.show()
        self._place_header()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._place_header()

    def _header_right(self) -> float:
        x = self.width() - 14
        for w in self._header_widgets:
            if not w.isHidden():
                x -= w.width() + 6
        return x

    def _place_header(self) -> None:
        x = self.width() - 14
        for w in reversed(self._header_widgets):
            if w.isHidden():
                continue
            x -= w.width()
            w.move(int(x), int((self.HEADER - w.height()) / 2 + 2))
            x -= 6

    def paintEvent(self, _):
        p = QPainter(self)
        if not p.isActive():
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W, H = self.width(), self.height()
        cut = 11
        g = QLinearGradient(0, 0, 0, H)
        g.setColorAt(0.0, blend(C.PRI, 0.085))
        g.setColorAt(0.18, blend(C.PRI, 0.045))
        g.setColorAt(1.0, blend(C.PRI, 0.025))
        p.setBrush(QBrush(g))
        p.setPen(QPen(blend(C.PRI, 0.24), 1))
        p.drawPolygon(chamfer(QRectF(0.5, 0.5, W - 1, H - 1), cut))
        # lit edges: the two chamfers and a short run along from each
        p.setPen(QPen(blend(C.PRI, 0.85), 1.6))
        p.drawLine(QLineF(0.5, cut + 0.5, cut + 0.5, 0.5))
        p.drawLine(QLineF(cut + 0.5, 0.5, cut + 44, 0.5))
        p.drawLine(QLineF(W - 0.5, H - cut - 0.5, W - cut - 0.5, H - 0.5))
        p.drawLine(QLineF(W - cut - 0.5, H - 0.5, W - cut - 34, H - 0.5))
        if self._title:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(qcol(C.ACC)))
            p.drawRect(QRectF(12, 11, 3, 10))
            right = self._header_right()
            code_w = 0.0
            if self._code:
                p.setFont(px_font(10, mono=True))
                code_w = p.fontMetrics().horizontalAdvance(self._code) + 8
                p.setPen(QPen(blend(C.PRI, 0.5)))
                p.drawText(QRectF(right - code_w, 6, code_w, 20),
                           Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, self._code)
            p.setFont(px_font(11, QFont.Weight.DemiBold, spacing=1.8))
            p.setPen(QPen(qcol(C.PRI)))
            room = max(10.0, right - code_w - 26)
            p.drawText(QRectF(21, 6, room, 20), Qt.AlignmentFlag.AlignVCenter,
                       p.fontMetrics().elidedText(self._title, Qt.TextElideMode.ElideRight, int(room)))
            lg = QLinearGradient(12, 0, W - 12, 0)
            lg.setColorAt(0.0, with_alpha(C.PRI, 0.55))
            lg.setColorAt(0.6, with_alpha(C.PRI, 0.12))
            lg.setColorAt(1.0, with_alpha(C.PRI, 0.0))
            p.setPen(QPen(QBrush(lg), 1))
            p.drawLine(QLineF(12, self.HEADER, W - 12, self.HEADER))
        p.end()


class PanelText:
    """Stands in for a QLabel whose words a HoloPanel now paints in its header,
    so code written against the label — setText, text — keeps working."""

    def __init__(self, panel: HoloPanel, which: str = "title"):
        self._panel, self._which = panel, which

    def setText(self, text) -> None:
        (self._panel.set_title if self._which == "title" else self._panel.set_code)(text)

    def text(self) -> str:
        return self._panel._title if self._which == "title" else self._panel._code

    def setStyleSheet(self, *_a) -> None:
        pass

    def setFont(self, *_a) -> None:
        pass


class RingGauge(QWidget):
    """A 270° dial: graduations, a track, the value arc with a soft glow, the
    figure in the middle and the label in the gauge's open mouth."""

    def __init__(self, label: str, parent=None):
        super().__init__(parent)
        self._label = label
        self._v, self._text = 0.0, "--"
        self.setMinimumSize(70, 74)

    def set_value(self, pct: float, text: str) -> None:
        v = max(0.0, min(100.0, float(pct)))
        if v == self._v and text == self._text:
            return
        self._v, self._text = v, text
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        if not p.isActive():
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W, H = self.width(), self.height()
        r = min(W - 4, H - 4) / 2
        cx, cy = W / 2, H / 2
        na = self._text in ("N/A", "--")
        col = (blend(C.PRI, 0.5) if na else qcol(C.RED) if self._v > 85
               else qcol(C.ACC) if self._v > 65 else qcol(C.PRI))
        maj, mnr = [], []
        for i in range(37):
            a = math.radians(225 - i * 7.5)
            ca, sa = math.cos(a), -math.sin(a)
            if i % 6 == 0:
                maj.append(QLineF(cx + ca * r * 0.80, cy + sa * r * 0.80, cx + ca * r * 0.98, cy + sa * r * 0.98))
            else:
                mnr.append(QLineF(cx + ca * r * 0.90, cy + sa * r * 0.90, cx + ca * r * 0.98, cy + sa * r * 0.98))
        p.setPen(QPen(blend(C.PRI, 0.40), 1.2))
        p.drawLines(maj)
        p.setPen(QPen(blend(C.PRI, 0.16), 1))
        p.drawLines(mnr)
        ar = r * 0.70
        box = QRectF(cx - ar, cy - ar, ar * 2, ar * 2)
        track = QPen(blend(C.PRI, 0.13), 5)
        track.setCapStyle(Qt.PenCapStyle.FlatCap)
        p.setPen(track)
        p.drawArc(box, 225 * 16, -270 * 16)
        span = int(-270 * 16 * self._v / 100)
        if span:
            glow = QPen(with_alpha(col, 0.22), 11)
            glow.setCapStyle(Qt.PenCapStyle.FlatCap)
            p.setPen(glow)
            p.drawArc(box, 225 * 16, span)
            arc = QPen(col, 5)
            arc.setCapStyle(Qt.PenCapStyle.FlatCap)
            p.setPen(arc)
            p.drawArc(box, 225 * 16, span)
        p.setPen(QPen(blend(C.PRI, 0.25), 1))
        ir = r * 0.52
        p.drawEllipse(QRectF(cx - ir, cy - ir, ir * 2, ir * 2))
        p.setFont(px_font(max(10, r * 0.30), QFont.Weight.DemiBold, mono=True))
        p.setPen(QPen(qcol(C.TEXT_DIM) if na else qcol(C.WHITE)))
        p.drawText(QRectF(cx - r, cy - r * 0.24, r * 2, r * 0.46), Qt.AlignmentFlag.AlignCenter, self._text)
        p.setFont(px_font(max(8, r * 0.16), QFont.Weight.DemiBold, spacing=1.4))
        p.setPen(QPen(blend(col, 0.9)))
        p.drawText(QRectF(cx - r, cy + r * 0.66, r * 2, r * 0.3), Qt.AlignmentFlag.AlignCenter, self._label)
        p.end()


class Sparkline(QWidget):
    """A scrolling line graph over a faint grid. The scale follows the largest
    recent value but never drops below `floor`, so an idle line stays low
    instead of magnifying noise to full height."""

    def __init__(self, samples: int = 60, floor: float = 0.25, parent=None):
        super().__init__(parent)
        self._vals: deque = deque(maxlen=samples)
        self._floor = floor
        self.setMinimumHeight(48)

    def push(self, value: float) -> None:
        self._vals.append(max(0.0, float(value)))
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        if not p.isActive():
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W, H = self.width(), self.height()
        p.setPen(QPen(blend(C.PRI, 0.10), 1))
        for i in range(1, 4):
            p.drawLine(QLineF(0, H * i / 4, W, H * i / 4))
        for i in range(1, 8):
            p.drawLine(QLineF(W * i / 8, 0, W * i / 8, H))
        vals = list(self._vals)
        if len(vals) < 2:
            p.end()
            return
        top = max(self._floor, max(vals)) * 1.15
        n = self._vals.maxlen or len(vals)
        x0 = W * (n - len(vals)) / max(1, n - 1)
        pts = [QPointF(x0 + W * i / max(1, n - 1), H - 3 - (H - 8) * (v / top)) for i, v in enumerate(vals)]
        path = QPainterPath(pts[0])
        for pt in pts[1:]:
            path.lineTo(pt)
        area = QPainterPath(path)
        area.lineTo(pts[-1].x(), H)
        area.lineTo(pts[0].x(), H)
        area.closeSubpath()
        g = QLinearGradient(0, 0, 0, H)
        g.setColorAt(0, with_alpha(C.PRI, 0.35))
        g.setColorAt(1, with_alpha(C.PRI, 0.0))
        p.fillPath(area, QBrush(g))
        p.setPen(QPen(qcol(C.PRI), 1.5))
        p.drawPath(path)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(qcol(C.WHITE)))
        p.drawEllipse(pts[-1], 2.5, 2.5)
        p.end()


class DataRow(QWidget):
    """KEY ........ value — one fact per row."""

    def __init__(self, key: str, value: str = "--", colour: str | None = None, parent=None):
        super().__init__(parent)
        self._key, self._value, self._colour = key, value, colour
        self.setFixedHeight(20)

    def set_value(self, value: str, colour: str | None = None) -> None:
        if value == self._value and colour == self._colour:
            return
        self._value, self._colour = value, colour
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        if not p.isActive():
            return
        W, H = self.width(), self.height()
        p.setFont(px_font(10, QFont.Weight.DemiBold, spacing=1.2))
        p.setPen(QPen(qcol(C.TEXT_DIM)))
        kw = p.fontMetrics().horizontalAdvance(self._key) + 6
        p.drawText(QRectF(0, 0, W, H), Qt.AlignmentFlag.AlignVCenter, self._key)
        p.setFont(px_font(11, QFont.Weight.Medium, mono=True))
        room = max(10, int(W - kw - 12))
        value = p.fontMetrics().elidedText(self._value, Qt.TextElideMode.ElideRight, room)
        vw = p.fontMetrics().horizontalAdvance(value) + 6
        p.setPen(QPen(qcol(_pal(self._colour) or C.TEXT)))
        p.drawText(QRectF(0, 0, W, H), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, value)
        p.setPen(QPen(blend(C.PRI, 0.22), 1, Qt.PenStyle.DotLine))
        if W - vw > kw:
            p.drawLine(QLineF(kw, H / 2 + 3, W - vw, H / 2 + 3))
        p.end()


def paint_icon(p: QPainter, kind: str, r: QRectF, col: QColor) -> None:
    """Line icons drawn with the painter, so they look the same on every OS —
    emoji do not, and a missing glyph is a box."""
    cx, cy = r.center().x(), r.center().y()
    s = min(r.width(), r.height()) / 2
    pen = QPen(col, 1.6)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    if kind == "gear":
        pts = []
        for i in range(32):
            a = math.radians(i * 11.25 - 11.25)
            rad = s * (0.88 if (i % 4) in (1, 2) else 0.62)
            pts.append(QPointF(cx + math.cos(a) * rad, cy + math.sin(a) * rad))
        p.drawPolygon(QPolygonF(pts))
        p.drawEllipse(QPointF(cx, cy), s * 0.26, s * 0.26)
    elif kind == "sliders":
        for i, k in enumerate((0.3, 0.7, 0.45)):
            y = cy - s * 0.55 + i * s * 0.55
            p.drawLine(QLineF(cx - s * 0.8, y, cx + s * 0.8, y))
            p.setBrush(QBrush(col))
            p.drawEllipse(QPointF(cx - s * 0.8 + k * s * 1.6, y), 2.4, 2.4)
            p.setBrush(Qt.BrushStyle.NoBrush)
    elif kind == "expand":
        a, b = s * 0.75, s * 0.32
        for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
            x, y = cx + sx * a, cy + sy * a
            p.drawLine(QLineF(x, y, x - sx * b, y))
            p.drawLine(QLineF(x, y, x, y - sy * b))
    elif kind in ("mic", "mic_off"):
        path = QPainterPath()
        path.addRoundedRect(QRectF(cx - s * 0.28, cy - s * 0.85, s * 0.56, s * 1.05), s * 0.28, s * 0.28)
        p.drawPath(path)
        p.drawArc(QRectF(cx - s * 0.55, cy - s * 0.55, s * 1.1, s * 1.0), 200 * 16, 140 * 16)
        p.drawLine(QLineF(cx, cy + s * 0.45, cx, cy + s * 0.85))
        if kind == "mic_off":
            p.drawLine(QLineF(cx - s * 0.8, cy - s * 0.8, cx + s * 0.8, cy + s * 0.8))
    elif kind == "stop":
        p.drawRect(QRectF(cx - s * 0.45, cy - s * 0.45, s * 0.9, s * 0.9))
    elif kind == "send":
        p.drawPolygon(QPolygonF([QPointF(cx - s * 0.6, cy - s * 0.7), QPointF(cx + s * 0.75, cy),
                                 QPointF(cx - s * 0.6, cy + s * 0.7), QPointF(cx - s * 0.3, cy)]))
    elif kind == "plus":
        p.drawLine(QLineF(cx - s * 0.6, cy, cx + s * 0.6, cy))
        p.drawLine(QLineF(cx, cy - s * 0.6, cx, cy + s * 0.6))
    elif kind == "close":
        p.drawLine(QLineF(cx - s * 0.5, cy - s * 0.5, cx + s * 0.5, cy + s * 0.5))
        p.drawLine(QLineF(cx + s * 0.5, cy - s * 0.5, cx - s * 0.5, cy + s * 0.5))
    elif kind == "sound":
        p.drawPolygon(QPolygonF([QPointF(cx - s * 0.8, cy - s * 0.3), QPointF(cx - s * 0.4, cy - s * 0.3),
                                 QPointF(cx, cy - s * 0.7), QPointF(cx, cy + s * 0.7),
                                 QPointF(cx - s * 0.4, cy + s * 0.3), QPointF(cx - s * 0.8, cy + s * 0.3)]))
        p.drawArc(QRectF(cx - s * 0.3, cy - s * 0.5, s * 0.8, s * 1.0), -60 * 16, 120 * 16)


class IconButton(QPushButton):
    """A painted button: chamfered frame, a line icon, and optional words.

    setText/text are kept so code that labels a button keeps working; the
    words are painted, never drawn by Qt's own button style."""

    def __init__(self, kind: str, size: int = 32, text: str = "", colour: str | None = None,
                 parent=None):
        super().__init__(parent)
        self._kind, self._label, self._colour = kind, str(text or ""), colour
        self._compact = not text
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        if self._compact:
            self.setFixedSize(size, size)
        else:
            self.setFixedHeight(size)
            self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setStyleSheet("QPushButton { background: transparent; border: none; }")

    def set_look(self, kind: str | None = None, text: str | None = None,
                 colour: str | None = None) -> None:
        if kind is not None:
            self._kind = kind
        if text is not None:
            self._label = str(text)
        self._colour = colour
        self.update()

    def setText(self, text) -> None:          # noqa: N802 - Qt naming
        self._label = str(text or "")
        self.update()

    def text(self) -> str:
        return self._label

    def enterEvent(self, e):
        super().enterEvent(e)
        self.update()

    def leaveEvent(self, e):
        super().leaveEvent(e)
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        if not p.isActive():
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W, H = self.width(), self.height()
        col = qcol(_pal(self._colour) or C.PRI)
        if not self.isEnabled():
            col = qcol(C.TEXT_DIM)
        hot = self.underMouse() or self.isChecked() or self.isDown()
        p.setBrush(QBrush(blend(col, 0.20 if self.isDown() else 0.15 if hot else 0.06)))
        p.setPen(QPen(blend(col, 0.8 if hot else 0.42), 1))
        p.drawPolygon(chamfer(QRectF(0.5, 0.5, W - 1, H - 1), 6))
        if self._compact:
            paint_icon(p, self._kind, QRectF(W * 0.26, H * 0.26, W * 0.48, H * 0.48), col)
        else:
            paint_icon(p, self._kind, QRectF(11, (H - 15) / 2, 15, 15), col)
            p.setFont(px_font(11, QFont.Weight.DemiBold, spacing=1.6))
            p.setPen(QPen(col))
            room = max(10, W - 44)
            p.drawText(QRectF(34, 0, room, H), Qt.AlignmentFlag.AlignVCenter,
                       p.fontMetrics().elidedText(self._label, Qt.TextElideMode.ElideRight, room))
        p.end()


class ArcLogo(QWidget):
    """The small arc-reactor mark at the left of the header."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(34, 34)

    def paintEvent(self, _):
        p = QPainter(self)
        if not p.isActive():
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        cx = cy = 17
        g = QRadialGradient(cx, cy, 16)
        g.setColorAt(0, with_alpha(C.PRI, 0.55))
        g.setColorAt(1, with_alpha(C.PRI, 0.0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(g))
        p.drawEllipse(QPointF(cx, cy), 16, 16)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(qcol(C.PRI), 1.6))
        p.drawEllipse(QPointF(cx, cy), 13, 13)
        p.setPen(QPen(blend(C.PRI, 0.7), 2.2))
        for i in range(10):
            a = math.radians(i * 36 + 90)
            p.drawLine(QLineF(cx + math.cos(a) * 7.5, cy + math.sin(a) * 7.5,
                              cx + math.cos(a) * 10.5, cy + math.sin(a) * 10.5))
        p.setBrush(QBrush(qcol(C.WHITE)))
        p.setPen(QPen(qcol(C.PRI), 1))
        p.drawEllipse(QPointF(cx, cy), 4, 4)
        p.end()


class StateChip(QWidget):
    """What the assistant is doing, in the middle of the header."""

    _COLOURS = {"LISTENING": "GREEN", "SPEAKING": "ACC", "THINKING": "ACC2",
                "PROCESSING": "ACC2", "MUTED": "MUTED_C"}

    def __init__(self, parent=None):
        super().__init__(parent)
        self._state = "INITIALISING"
        self.setFixedSize(196, 34)

    def set_state(self, state: str) -> None:
        state = str(state or "").upper()
        if state != self._state:
            self._state = state
            self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        if not p.isActive():
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W, H = self.width(), self.height()
        col = qcol(getattr(C, self._COLOURS.get(self._state, "PRI")))
        p.setBrush(QBrush(blend(col, 0.10)))
        p.setPen(QPen(blend(col, 0.55), 1))
        p.drawPolygon(chamfer(QRectF(0.5, 0.5, W - 1, H - 1), 9, ("tl", "tr", "br", "bl")))
        g = QRadialGradient(22, H / 2, 10)
        g.setColorAt(0, with_alpha(col, 0.9))
        g.setColorAt(1, with_alpha(col, 0.0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(g))
        p.drawEllipse(QPointF(22, H / 2), 10, 10)
        p.setBrush(QBrush(col))
        p.drawEllipse(QPointF(22, H / 2), 3.5, 3.5)
        p.setFont(px_font(12, QFont.Weight.DemiBold, spacing=2.6))
        p.setPen(QPen(col))
        p.drawText(QRectF(38, 0, W - 44, H), Qt.AlignmentFlag.AlignVCenter,
                   p.fontMetrics().elidedText(self._state, Qt.TextElideMode.ElideRight, int(W - 44)))
        p.end()


class HeaderBar(QWidget):
    """The header's ground: a faint wash, and a bright rule along the bottom
    that is brightest in the middle, under the state chip."""

    def paintEvent(self, _):
        p = QPainter(self)
        if not p.isActive():
            return
        W, H = self.width(), self.height()
        g = QLinearGradient(0, 0, 0, H)
        g.setColorAt(0, blend(C.PRI, 0.06))
        g.setColorAt(1, qcol(C.BG))
        p.fillRect(self.rect(), QBrush(g))
        lg = QLinearGradient(0, 0, W, 0)
        lg.setColorAt(0.0, with_alpha(C.PRI, 0.05))
        lg.setColorAt(0.5, with_alpha(C.PRI, 0.9))
        lg.setColorAt(1.0, with_alpha(C.PRI, 0.05))
        p.setPen(QPen(QBrush(lg), 1))
        p.drawLine(QLineF(0, H - 1.5, W, H - 1.5))
        p.setPen(QPen(qcol(C.PRI), 2))
        p.drawLine(QLineF(W / 2 - 70, H - 1.5, W / 2 + 70, H - 1.5))
        p.end()


class StatusStrip(QWidget):
    """The footer: status dots with labels on the left, one line on the right."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(26)
        self._items: list[tuple[str, str]] = []
        self._right = ""

    def set_items(self, items) -> None:
        items = [(str(a), str(b)) for a, b in items]
        if items != self._items:
            self._items = items
            self.update()

    def set_right(self, text: str) -> None:
        self._right = str(text or "")
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        if not p.isActive():
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W, H = self.width(), self.height()
        p.fillRect(self.rect(), blend(C.PRI, 0.03))
        p.setPen(QPen(blend(C.PRI, 0.2), 1))
        p.drawLine(QLineF(0, 0.5, W, 0.5))
        p.setFont(px_font(9.5, QFont.Weight.DemiBold, spacing=1.4))
        fm = p.fontMetrics()
        right_w = fm.horizontalAdvance(self._right) + 14
        x = 14.0
        for label, colour in self._items:
            w = fm.horizontalAdvance(label)
            if x + w + 11 > W - right_w - 10:
                break
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(qcol(_pal(colour) or C.PRI)))
            p.drawEllipse(QPointF(x + 3, H / 2), 2.6, 2.6)
            p.setPen(QPen(qcol(C.TEXT_MED)))
            p.drawText(QRectF(x + 11, 0, w + 4, H), Qt.AlignmentFlag.AlignVCenter, label)
            x += w + 34
        p.setPen(QPen(qcol(C.TEXT_DIM)))
        p.drawText(QRectF(0, 0, W - 14, H), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                   self._right)
        p.end()


def _pal(colour):
    """A palette name ("GREEN") or a hex value ("#00ff88") -> a hex value.
    Names are resolved at paint time, so they follow a live re-theme."""
    if isinstance(colour, str) and colour and not colour.startswith("#"):
        return getattr(C, colour, None)
    return colour


class CommandFrame(QWidget):
    """The frame the command line sits in; it lights up while the line has focus."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(40)
        app = QApplication.instance()
        if app is not None:
            app.focusChanged.connect(self._on_focus)

    def _on_focus(self, *_a):
        try:
            self.update()
        except RuntimeError:        # the C++ object is already gone at shutdown
            pass

    def paintEvent(self, _):
        p = QPainter(self)
        if not p.isActive():
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W, H = self.width(), self.height()
        fw = QApplication.focusWidget()
        focus = fw is not None and self.isAncestorOf(fw)
        p.setBrush(QBrush(blend(C.PRI, 0.10 if focus else 0.06)))
        p.setPen(QPen(blend(C.PRI, 0.95 if focus else 0.5), 1))
        p.drawPolygon(chamfer(QRectF(0.5, 0.5, W - 1, H - 1), 8))
        p.end()


class RoutineRow(QWidget):
    """One routine in the ROUTINES list: the name, when it next happens, and an
    on/off switch at the right. The switch turns it on or off; anywhere else on
    the row shows the routine in the content panel."""

    clicked = pyqtSignal(str)
    toggled = pyqtSignal(str, bool)
    SWITCH_W = 30

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(40)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._routine: dict | None = None

    def set_routine(self, routine: dict | None) -> None:
        self._routine = routine
        self.update()

    def mousePressEvent(self, e):
        r = self._routine
        if not r or e.button() != Qt.MouseButton.LeftButton:
            return
        if e.position().x() >= self.width() - self.SWITCH_W - 6:
            self.toggled.emit(r["id"], not r.get("enabled"))
        else:
            self.clicked.emit(r["id"])

    def enterEvent(self, e):
        super().enterEvent(e)
        self.update()

    def leaveEvent(self, e):
        super().leaveEvent(e)
        self.update()

    def paintEvent(self, _):
        r = self._routine
        p = QPainter(self)
        if not p.isActive() or not r:
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W = self.width()
        on = bool(r.get("enabled"))
        col = qcol(C.GREEN if on else C.TEXT_DIM)
        if self.underMouse():
            p.fillRect(self.rect(), blend(C.PRI, 0.08))

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(col))
        p.drawEllipse(QPointF(5, 11), 3, 3)

        sx = W - self.SWITCH_W
        p.setPen(QPen(blend(col, 0.75 if on else 0.45), 1))
        p.setBrush(QBrush(blend(col, 0.22 if on else 0.05)))
        p.drawRoundedRect(QRectF(sx + 0.5, 4.5, self.SWITCH_W - 1, 13), 6.5, 6.5)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(col))
        p.drawEllipse(QRectF(sx + self.SWITCH_W - 12 if on else sx + 3, 7, 9, 8))

        p.setFont(px_font(11, QFont.Weight.DemiBold))
        p.setPen(QPen(qcol(C.WHITE if on else C.TEXT_DIM)))
        room = max(10, int(W - 14 - self.SWITCH_W - 8))
        p.drawText(QRectF(14, 1, room, 20), Qt.AlignmentFlag.AlignVCenter,
                   p.fontMetrics().elidedText(r.get("name") or "", Qt.TextElideMode.ElideRight, room))

        p.setFont(px_font(10))
        p.setPen(QPen(qcol(C.TEXT_DIM)))
        p.drawText(QRectF(14, 21, W - 14, 16), Qt.AlignmentFlag.AlignVCenter,
                   p.fontMetrics().elidedText(r.get("_when") or "", Qt.TextElideMode.ElideRight, int(W - 14)))
        p.end()


class TaskRow(QWidget):
    """One background task in the TASKS panel: a status dot, the title, how much
    of its budget it has used, and the last thing it reported. Click to see it."""

    clicked = pyqtSignal(str)
    _COLOURS = {"running": "PRI", "queued": "TEXT_DIM", "scheduled": "ACC2",
                "waiting_quota": "ACC", "done": "GREEN", "failed": "RED", "cancelled": "TEXT_DIM"}

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(46)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._task: dict | None = None

    def set_task(self, task: dict | None) -> None:
        self._task = task
        self.update()

    def mousePressEvent(self, e):
        if self._task and e.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self._task["id"])

    def enterEvent(self, e):
        super().enterEvent(e)
        self.update()

    def leaveEvent(self, e):
        super().leaveEvent(e)
        self.update()

    def _state_text(self, t: dict) -> str:
        s = t["status"]
        used = t["used"]["seconds"] / 60
        if s == "running":
            return f"{used:.0f}/{t['budget']['minutes']}m"
        if s == "scheduled":
            return time.strftime("%H:%M", time.localtime(t.get("not_before") or 0))
        return {"waiting_quota": "WAIT", "queued": "QUEUED", "done": "DONE",
                "failed": "FAILED", "cancelled": "STOPPED"}.get(s, s.upper())

    def paintEvent(self, _):
        t = self._task
        p = QPainter(self)
        if not p.isActive() or not t:
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W, H = self.width(), self.height()
        col = qcol(getattr(C, self._COLOURS.get(t["status"], "PRI")))
        if self.underMouse():
            p.fillRect(self.rect(), blend(C.PRI, 0.08))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(col))
        p.drawEllipse(QPointF(5, 10), 3, 3)

        p.setFont(px_font(10, mono=True))
        state = self._state_text(t)
        sw = p.fontMetrics().horizontalAdvance(state) + 4
        p.setPen(QPen(col))
        p.drawText(QRectF(W - sw, 1, sw, 18), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, state)

        p.setFont(px_font(11, QFont.Weight.DemiBold))
        p.setPen(QPen(qcol(C.WHITE)))
        room = max(10, int(W - 16 - sw - 6))
        p.drawText(QRectF(14, 1, room, 18), Qt.AlignmentFlag.AlignVCenter,
                   p.fontMetrics().elidedText(t.get("title") or "", Qt.TextElideMode.ElideRight, room))

        budget = max(1.0, t["budget"]["minutes"] * 60.0)
        frac = 1.0 if t["status"] == "done" else min(1.0, t["used"]["seconds"] / budget)
        p.fillRect(QRectF(14, 22, W - 14, 3), blend(C.PRI, 0.12))
        if frac > 0:
            p.fillRect(QRectF(14, 22, (W - 14) * frac, 3), col)

        last = ""
        for ev in reversed(t.get("events") or []):
            if ev.get("kind") in ("progress", "tool", "note", "finding") or t["status"] in ("failed", "waiting_quota"):
                last = ev.get("text") or ""
                break
        p.setFont(px_font(10))
        p.setPen(QPen(qcol(C.TEXT_DIM)))
        p.drawText(QRectF(14, 27, W - 14, 16), Qt.AlignmentFlag.AlignVCenter,
                   p.fontMetrics().elidedText(last, Qt.TextElideMode.ElideRight, int(W - 14)))
        p.end()
