"""gui/providers_panel.py — ENGINE & PROVIDERS — the panel that chooses what holds the conversation.

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
    C,
)
from gui.overlays import (
    _HudOverlay,
    _ScrollSafeCombo,
)

from gui.style import hud_font


class ProvidersOverlay(_HudOverlay):
    """Choose what runs the assistant: Gemini Live, or your own stack.

    WHAT THE FIRST VERSION GOT WRONG, AND WHY IT IS WORTH WRITING DOWN
        It listed the PROVIDERS, and a provider here covers thirteen services at
        once because they all speak the same protocol. That is the right shape
        for the code — it is why `llm_openai.py` is one file instead of thirteen
        — and it is the wrong shape for a person.

        Watched in use: somebody who wanted to run a model on their own machine
        opened the dropdown, saw a single entry reading "OpenAI-compatible", and
        concluded the feature was not there. They were right to. Nobody looking
        for Ollama searches for the name of a protocol, and "OpenAI" is the last
        word you expect to find under "run it locally".

        So the panel now lists what people are actually looking for — Ollama, LM
        Studio, Groq, DeepSeek — from each provider's own `catalog`. One file
        behind the scenes, thirteen recognisable names in front of them.

    THE OTHER TWO CHANGES
        It was 560 pixels of stacked dropdowns with no grouping, which reads as
        a form to be endured rather than three decisions to be made. It is 760
        wide now, and the three parts of a conversation are three numbered boxes
        that say what they do in plain words.

        And every field a provider declares was shown at once — download folders,
        stability weights, thread counts. A setting almost nobody changes costs
        more in the confusion it adds to the five that matter than it saves for
        the one person who wants it, so `advanced` fields sit behind MORE.

    WHAT IT COSTS WHEN NOBODY OPENS IT
        Nothing. The button is built with the rest of the SETUP drawer, but the
        `providers` package is imported inside `__init__` of THIS class — on the
        click. Same lesson as the wake-word check that spent a measured 2.104
        seconds importing `openwakeword` on the Qt thread and made the drawer
        look broken.
    """

    changed = pyqtSignal()          # emitted on APPLY when the engine moved
    # Emitted from a worker thread when something the machine had to be asked
    # for — the installed voices — has arrived. A signal rather than a direct
    # call because the worker must not touch a widget.
    _live_ready = pyqtSignal()
    # (provider id, [(value, label), ...]) from a worker that went and asked an
    # endpoint what it offers.
    _models_ready = pyqtSignal(str, list)
    _OW = 760                       # the window's own minimum is 820

    # Which provider fills each slot, and what the slot is FOR — in the words
    # somebody configuring it would use, not the words the code uses.
    _SLOTS = (
        ("stt", "①", "HEARING",  "turns what you say into words"),
        ("llm", "②", "THINKING", "understands, decides, uses your tools"),
        ("tts", "③", "SPEAKING", "reads the answer out loud"),
    )

    def __init__(self, parent=None):
        super().__init__(parent)
        # Imported HERE, on the click — never at startup. See the class docstring.
        from memory.config_manager import (get_coding_provider, get_pipeline,
                                           get_provider_config,
                                           get_provider_mode, get_sidecar_first,
                                           get_sidecar_provider)
        from providers import registry

        self._registry = registry
        self._get_cfg = get_provider_config
        # What the running engine was built from, before any edit here — see
        # _apply for why this cannot be read at APPLY time.
        self._opened_engine = self._engine_fingerprint()
        # Warm anything a provider can only learn by asking the machine, off
        # this thread. The installed voice list costs a PowerShell subprocess —
        # measured at 1.70 seconds — and the form asks for it on every rebuild,
        # so without this the panel freezes for most of two seconds every time
        # a dropdown is touched. Started here rather than at startup, because
        # nothing in this folder may run before somebody opens it.
        self._live_ready.connect(self._on_live_ready)
        self._models_ready.connect(self._on_models_ready)
        self._model_cache: dict[str, list] = {}
        self._fetching: set = set()
        for _kind in ("tts", "stt", "llm"):
            for _decl in registry.discover(_kind):
                try:
                    _mod = __import__(f"providers.{_decl['_module']}",
                                      fromlist=["prefetch"])
                    _pf = getattr(_mod, "prefetch", None)
                    if _pf:
                        _pf(self._live_ready.emit)
                except Exception:
                    pass
        self._edits: dict[str, dict] = {}       # provider id -> {key: (widget, Field)}
        self._status: dict[str, QLabel] = {}    # provider id -> status line
        self._more: dict[str, bool] = {}        # provider id -> advanced shown
        # Fields deliberately not drawn — the catalog already answered them, or
        # they are behind MORE. Tracked rather than asked of Qt: see _fields_into.
        self._hidden: dict[str, set] = {}

        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            ProvidersOverlay {{
                background: rgba(0, 6, 10, 249);
                border: 1px solid {C.BORDER_B};
                border-radius: 8px;
            }}
        """)
        self.setFixedWidth(self._OW)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(22, 18, 22, 18)
        outer.setSpacing(10)

        title = QLabel("🔌  ENGINE  &  PROVIDERS")
        title.setFont(hud_font(13, QFont.Weight.Bold))
        title.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        outer.addWidget(title)

        sub = QLabel("Choose what powers the conversation. "
                     "Gemini is the default and needs nothing set up here.")
        sub.setWordWrap(True)
        sub.setFont(hud_font(8))
        sub.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        outer.addWidget(sub)

        # ── Mode: two cards, not a dropdown ─────────────────────────────────
        # A dropdown hides the option you did not choose. This is the one
        # decision on the panel that changes everything below it, and both
        # answers deserve to be readable at the same time.
        self._mode = get_provider_mode()
        cards = QHBoxLayout(); cards.setSpacing(10)
        self._card_gemini = self._mode_card(
            "☁  GEMINI LIVE",
            "One connection carries your voice, its voice, the transcript and "
            "the tools together. The fastest option — about half a second to "
            "the first word.",
            "gemini")
        self._card_local = self._mode_card(
            "🖥  MY OWN SETUP",
            "Your choice of hearing, thinking and speaking. Can run entirely on "
            "this computer, with nothing leaving it. Slower: 1.5 to 4 seconds.",
            "pipeline")
        cards.addWidget(self._card_gemini)
        cards.addWidget(self._card_local)
        outer.addLayout(cards)

        # ── Body ────────────────────────────────────────────────────────────
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setStyleSheet(
            "QScrollArea { background: transparent; }"
            f"QScrollBar:vertical {{ background: {C.DARK}; width: 9px; "
            f"margin: 0; border-radius: 4px; }}"
            f"QScrollBar::handle:vertical {{ background: {C.BORDER_B}; "
            f"border-radius: 4px; min-height: 30px; }}"
            "QScrollBar::add-line, QScrollBar::sub-line { height: 0; }"
            "QScrollBar::add-page, QScrollBar::sub-page { background: none; }")
        self._scroll.setMinimumHeight(230)
        self._scroll.setMaximumHeight(360)
        outer.addWidget(self._scroll, 1)

        self._body = QWidget()
        self._body.setStyleSheet("background: transparent;")
        self._body_lay = QVBoxLayout(self._body)
        self._body_lay.setContentsMargins(0, 2, 8, 2)
        self._body_lay.setSpacing(10)
        self._scroll.setWidget(self._body)

        # Built BEFORE the body, because `_rebuild_body` writes into it — and
        # added to the layout after, so it still sits under the scroll area.
        self._footer = QLabel("")
        self._footer.setWordWrap(True)
        self._footer.setFont(hud_font(7))
        self._footer.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")

        # Every value below is a service id — "openai_compat@minimax" — so the
        # fallback, the coding model and ② THINKING can each be a different
        # service with settings of its own.
        self._pipe = {slot: self._normalise(sid, slot)
                      for slot, sid in dict(get_pipeline()).items()}
        self._sidecar = self._normalise(get_sidecar_provider(), "llm")
        self._coding = self._normalise(get_coding_provider(), "llm")
        self._sidecar_first = get_sidecar_first()
        self._rebuild_body()

        outer.addWidget(self._footer)

        row = QHBoxLayout(); row.setSpacing(8)
        apply_btn = QPushButton("▸  SAVE  &&  APPLY")     # && — a lone & is a Qt shortcut marker
        apply_btn.setFixedHeight(34)
        apply_btn.setFont(hud_font(9, QFont.Weight.Bold))
        apply_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        apply_btn.setStyleSheet(f"""
            QPushButton {{ background: {C.PRI_GHO}; color: {C.PRI};
                border: 1px solid {C.PRI_DIM}; border-radius: 4px; }}
            QPushButton:hover {{ background: {C.BORDER}; border-color: {C.PRI}; }}
        """)
        apply_btn.clicked.connect(self._apply)
        row.addWidget(apply_btn, 2)

        close = QPushButton("CLOSE")
        close.setFixedHeight(34)
        close.setFont(hud_font(9))
        close.setCursor(Qt.CursorShape.PointingHandCursor)
        close.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 4px; }}
            QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
        """)
        close.clicked.connect(self.hide)
        row.addWidget(close, 1)
        outer.addLayout(row)
        self._refresh_cards()

    # ── pieces ───────────────────────────────────────────────────────────────
    def _mode_card(self, title: str, body: str, value: str) -> QPushButton:
        b = QPushButton()
        b.setCheckable(True)
        b.setMinimumHeight(84)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.setText(f"{title}\n\n{body}")
        b.setFont(hud_font(7))
        b.clicked.connect(lambda _=0, v=value: self._set_mode(v))
        b.setProperty("modeValue", value)
        return b

    def _refresh_cards(self) -> None:
        for card in (self._card_gemini, self._card_local):
            on = card.property("modeValue") == self._mode
            card.setChecked(on)
            card.setStyleSheet(f"""
                QPushButton {{
                    text-align: left; padding: 10px 12px;
                    background: {C.PRI_GHO if on else 'transparent'};
                    color: {C.TEXT if on else C.TEXT_DIM};
                    border: 1px solid {C.PRI_DIM if on else C.BORDER};
                    border-radius: 5px;
                }}
                QPushButton:hover {{ border-color: {C.BORDER_B}; }}
            """)

    def _on_live_ready(self) -> None:
        """A machine-supplied list has arrived — redraw once so it appears."""
        if not self.isHidden():
            self._rebuild_body()

    # ── Asking an endpoint what it has, without being asked to ──────────────
    def _fetch_models(self, pid: str) -> None:
        """Go and get the model list in the background.

        Pressing LIST worked, and making somebody press it was still wrong: at
        the moment a local server is chosen, the one thing they need is the list
        of models on it, and the app can simply go and read it. A button whose
        only job is "now do the obvious next thing" is a button that should not
        exist.

        Off the interface thread, because it is a network call — a local server
        answers in milliseconds and a cloud one does not, and a panel that
        freezes while it finds out is worse than one that fills in a second
        later. Failure is silent: not reaching the endpoint yet is the normal
        state while somebody is still typing an address, and TEST is where a
        real answer about reachability belongs."""
        import threading

        if not pid or pid in self._fetching:
            return
        try:
            values = self._values(pid) or dict(self._get_cfg(pid))
        except Exception:
            values = dict(self._get_cfg(pid))
        if not values.get("base_url") and not values.get("api_key"):
            return
        self._fetching.add(pid)

        def work():
            items = []
            try:
                inst = self._registry.build(pid, values)
                if hasattr(inst, "models"):
                    items = [(m, m) for m in inst.models(timeout=6.0)]
                elif hasattr(inst, "voices"):
                    items = list(inst.voices())
            except Exception:
                items = []
            finally:
                self._fetching.discard(pid)
            if items:
                self._models_ready.emit(pid, items)

        threading.Thread(target=work, daemon=True).start()

    def _prepare(self, pid: str) -> None:
        """Let a provider get itself ready as soon as it is configured.

        Piper uses it to download the chosen voice. Generic on purpose: the
        panel asks whether the module has a `prepare`, and providers that do not
        need one are simply skipped — nothing here knows what a voice is."""
        import threading

        try:
            decl = self._registry.get_declaration(pid)
            mod = __import__(f"providers.{decl['_module']}", fromlist=["prepare"])
            fn = getattr(mod, "prepare", None)
        except Exception:
            fn = None
        if fn is None:
            return
        values = self._values(pid) or dict(self._get_cfg(pid))

        def report(ok, msg):
            colour = C.TEXT_DIM if ok is None else (C.GREEN if ok else C.RED)
            mark = "" if ok is None else ("✓  " if ok else "✕  ")
            QTimer.singleShot(0, lambda: self._say(pid, mark + msg, colour))

        threading.Thread(target=lambda: fn(values, report), daemon=True).start()

    def _on_models_ready(self, pid: str, items: list) -> None:
        self._model_cache[pid] = list(items)
        if self.isHidden():
            return
        key = "voice_id" if any(v != l for v, l in items) else "model"
        entry = self._edits.get(pid, {}).get(key)
        if entry is None:
            entry = self._edits.get(pid, {}).get("model")
        if entry is None or not isinstance(entry[0], QComboBox):
            return
        box = entry[0]
        keep = self._combo_value(box)
        box.clear()
        for value, label in items:
            box.addItem(str(label), str(value))
        # By value first; by label for a setting saved as a name before the
        # panel stored values, so it lands on the real entry instead of on text.
        j = box.findData(keep)
        if j < 0 and keep:
            j = box.findText(keep)
        if j >= 0:
            box.setCurrentIndex(j)
        elif keep:
            box.setCurrentText(keep)
        self._say(pid, f"{len(items)} model{'s' if len(items) != 1 else ''} "
                       f"found on this server — pick one above.", C.TEXT_DIM)

    def _set_mode(self, value: str) -> None:
        if value == self._mode:
            self._refresh_cards()
            return
        self._mode = value
        self._remember_edits()
        self._refresh_cards()
        QTimer.singleShot(0, self._rebuild_body)

    def _box(self, title_widgets: list) -> tuple[QWidget, QVBoxLayout]:
        """A bordered section. Grouping is the difference between three
        decisions and eleven controls."""
        w = QWidget()
        w.setStyleSheet(f"background: {C.PANEL}; border: 1px solid {C.BORDER}; "
                        f"border-radius: 5px;")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(12, 10, 12, 11)
        lay.setSpacing(5)
        for tw in title_widgets:
            lay.addWidget(tw)
        return w, lay

    def _label(self, text: str, size: int = 8, colour: str = None,
               bold: bool = False) -> QLabel:
        lb = QLabel(text)
        lb.setWordWrap(True)
        lb.setFont(hud_font(size,
                         QFont.Weight.Bold if bold else QFont.Weight.Normal))
        lb.setStyleSheet(f"color: {colour or C.TEXT_MED}; background: transparent; "
                         f"border: none;")
        return lb

    def _combo(self) -> QComboBox:
        box = _ScrollSafeCombo()
        box.setFont(hud_font(8))
        box.setFixedHeight(29)
        box.setStyleSheet(
            f"QComboBox {{ background: {C.DARK}; color: {C.TEXT}; "
            f"border: 1px solid {C.BORDER}; border-radius: 3px; padding: 3px 9px; }}"
            f"QComboBox:hover {{ border-color: {C.BORDER_B}; }}"
            f"QComboBox::drop-down {{ border: none; width: 18px; }}"
            f"QComboBox QAbstractItemView {{ background: {C.DARK}; color: {C.TEXT}; "
            f"selection-background-color: {C.PRI_GHO}; "
            f"border: 1px solid {C.BORDER_B}; padding: 2px; }}")
        return box

    def _line(self, text: str = "") -> QLineEdit:
        w = QLineEdit(text)
        w.setFont(hud_font(8))
        w.setFixedHeight(27)
        w.setStyleSheet(
            f"QLineEdit {{ background: {C.DARK}; color: {C.TEXT}; "
            f"border: 1px solid {C.BORDER}; border-radius: 3px; padding: 2px 9px; }}"
            f"QLineEdit:focus {{ border-color: {C.PRI_DIM}; }}")
        return w

    def _btn(self, text: str, slot, colour: str = None) -> QPushButton:
        b = QPushButton(text)
        b.setFixedHeight(25)
        b.setFont(hud_font(7, QFont.Weight.Bold))
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        col = colour or C.TEXT_MED
        b.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {col};
                border: 1px solid {C.BORDER}; border-radius: 3px; padding: 0 12px; }}
            QPushButton:hover {{ border-color: {C.BORDER_B}; color: {C.TEXT}; }}
        """)
        b.clicked.connect(slot)
        return b

    # ── catalog ──────────────────────────────────────────────────────────────
    def _catalog(self, kind: str) -> list[tuple[str, str, dict, dict]]:
        """(provider id, entry id, entry, declaration) for one slot.

        A provider with no catalog contributes itself as a single entry, so
        nothing has to declare one to appear."""
        out = []
        for decl in self._registry.discover(kind):
            entries = decl.get("catalog") or (
                {"id": decl["id"], "label": decl["label"],
                 "note": decl.get("hint", ""), "local": None, "values": {}},)
            for e in entries:
                out.append((decl["id"], e["id"], e, decl))
        return out

    def _current_entry(self, kind: str, pid: str) -> str:
        """Which catalog entry is selected.

        STORED, not derived. The first version worked it out by comparing the
        saved address against each entry's — which is elegant and wrong, and it
        showed up immediately: pick "Whisper on this computer", reopen the
        panel, and the box reads "— not set —" while the form beneath it is
        fully drawn. That entry's address is the empty string, so any config
        whose address had ever been touched matched nothing and fell through to
        no selection at all.

        The entry is a CHOICE somebody made, not a fact about the settings.
        Choices get stored. The derivation is kept only as a fallback for a
        config written before this key existed."""
        if not pid:
            return ""
        cfg = self._get_cfg(pid)
        saved = str(cfg.get("catalog") or "").strip()
        entries = [(eid, entry) for _p, eid, entry, _d in self._catalog(kind)
                   if _p == pid]
        if saved and any(eid == saved for eid, _e in entries):
            return saved

        # Written before the key existed: work it out once, from the address.
        best = ""
        for eid, entry in entries:
            vals = entry.get("values") or {}
            if not vals:
                best = best or eid            # a catch-all like "Something else"
                continue
            if all(str(cfg.get(k, "")).rstrip("/") == str(v).rstrip("/")
                   for k, v in vals.items() if k in ("base_url", "backend")):
                return eid
        return best or (entries[0][0] if len(entries) == 1 else "")

    def _sid(self, pid: str, eid: str) -> str:
        """The service id for one catalog row: "openai_compat@minimax", or the
        bare provider id for a provider without a catalog."""
        if not pid:
            return ""
        decl = self._registry.get_declaration(pid) or {}
        return f"{pid}@{eid}" if decl.get("catalog") and eid else pid

    def _parts(self, sid: str, kind: str) -> tuple[str, str]:
        """(provider id, catalog entry id) for a service id."""
        if not sid:
            return "", ""
        base, entry = self._registry.split_id(sid)
        if "@" not in sid:
            entry = self._current_entry(kind, base) or base
        return base, entry

    def _normalise(self, sid: str, kind: str) -> str:
        """A stored id in its current shape.

        config_manager converts every config that recorded which service it was
        for. What is left is a provider id with no record of that, from before
        the record existed: the service is worked out from the address once,
        and its settings come along to the new id."""
        if not sid or "@" in sid:
            return sid
        base, eid = self._parts(sid, kind)
        new = self._sid(base, eid)
        if new != sid and not self._get_cfg(new) and self._get_cfg(sid):
            from memory.config_manager import save_provider_config
            save_provider_config(new, dict(self._get_cfg(sid), catalog=eid))
        return new

    def _service_combo(self, kind: str, first: tuple, fmt) -> QComboBox:
        """A dropdown of every service for one slot. `first` is the row for
        "nothing chosen"; `fmt(entry, decl)` is the text for each service."""
        box = self._combo()
        box.addItem(*first)
        for pid, eid, entry, decl in self._catalog(kind):
            box.addItem(fmt(entry, decl), self._sid(pid, eid))
        return box

    def _select(self, box: QComboBox, sid: str) -> None:
        i = box.findData(sid) if sid else 0
        box.setCurrentIndex(max(0, i))

    def _apply_entry(self, kind: str, sid: str) -> None:
        """Store a service's shortcut values, the first time it is picked.

        Each service keeps its own settings now, so picking MiniMax again after
        trying OpenAI brings back the MiniMax model and key as they were left.
        Only a preset address is always rewritten — that one belongs to the
        service, not to the person.

        The open form's widgets are dropped afterwards, because the redraw that
        follows saves whatever they hold, and the redraw must not write one
        service's fields into another's settings."""
        from memory.config_manager import save_provider_config
        pid, eid = self._parts(sid, kind)
        stored = self._get_cfg(sid)
        for _p, _e, entry, _d in self._catalog(kind):
            if _p == pid and _e == eid:
                values = {k: v for k, v in (entry.get("values") or {}).items()
                          if k == "base_url" or k not in stored}
                values["catalog"] = eid
                save_provider_config(sid, values)
                break
        self._edits.pop(sid, None)

    # ── body ─────────────────────────────────────────────────────────────────
    def _clear(self, lay) -> None:
        while lay.count():
            item = lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
            elif item.layout() is not None:
                self._clear(item.layout())

    def _rebuild_body(self) -> None:
        # Keep whatever is in the boxes before they are destroyed.
        #
        # A redraw can be triggered by things the person did not do: a model
        # list arriving from a background fetch, a slot changing, the panel
        # settling after a click. Every one of those replaces the widgets, and
        # anything typed but not yet saved went with them — most visibly a model
        # name entered by hand, which came back empty on SAVE with no
        # indication that it had been dropped.
        self._remember_edits()
        self._clear(self._body_lay)
        self._edits.clear()
        self._status.clear()

        if self._mode == "pipeline":
            for slot, num, name, what in self._SLOTS:
                self._body_lay.addWidget(self._slot_box(slot, num, name, what))
        else:
            w, lay = self._box([])
            lay.addWidget(self._label(
                "Nothing to set up. Your voice goes to Gemini and comes back as "
                "speech over one connection, with the transcript and every tool "
                "call on the same wire.", 8, C.TEXT_DIM))
            lay.addWidget(self._label(
                "Pick MY OWN SETUP above to run it on your own machine, or "
                "through another company's model.", 7, C.TEXT_DIM))
            self._body_lay.addWidget(w)

        self._body_lay.addWidget(self._sidecar_box_widget())
        self._body_lay.addWidget(self._coding_box_widget())
        self._body_lay.addStretch(1)
        self._update_footer()

    def _slot_box(self, slot: str, num: str, name: str, what: str) -> QWidget:
        head = self._label(f"{num}  {name}", 9, C.PRI, bold=True)
        w, lay = self._box([head, self._label(what, 7, C.TEXT_DIM)])

        def fmt(entry, decl):
            label = entry["label"]
            if entry.get("local") is True:
                label = f"🖥  {label}"
            elif entry.get("local") is False:
                label = f"☁  {label}"
            else:
                label = f"    {label}"
            note = entry.get("note") or ""
            if not decl.get("_available", True):
                note = (note + " · needs install").strip(" ·")
            return f"{label}{'   —  ' + note if note else ''}"

        box = self._service_combo(slot, ("— not set —", ""), fmt)
        sid = self._pipe.get(slot, "")
        self._select(box, sid)
        box.currentIndexChanged.connect(
            lambda _i, s=slot, b=box: self._slot_changed(s, b))
        if sid:
            QTimer.singleShot(0, lambda p=sid: self._prepare(p))
        lay.addWidget(box)

        if sid:
            self._fields_into(lay, slot, sid)
            # If a list was already fetched, use it; if not, go and get one.
            if self._model_cache.get(sid):
                QTimer.singleShot(0, lambda p=sid: self._on_models_ready(
                    p, self._model_cache[p]))
            else:
                QTimer.singleShot(0, lambda p=sid: self._fetch_models(p))
        return w

    def _slot_changed(self, slot: str, box: QComboBox) -> None:
        sid = box.currentData() or ""
        self._remember_edits()
        self._pipe[slot] = sid
        if sid:
            # Applying the shortcut is the entire point of the catalog: pick
            # Ollama and the address is already right.
            self._apply_entry(slot, sid)
            self._fetch_models(sid)
        QTimer.singleShot(0, self._rebuild_body)

    def _fields_into(self, lay, slot: str, sid: str) -> None:
        from dataclasses import replace

        from providers.base import Field

        # Everything this form stores, shows and tests is keyed by the SERVICE
        # id, so two services of one provider never share a box or a setting.
        pid = sid
        base, eid = self._parts(sid, slot)
        decl = self._registry.get_declaration(base)
        if decl is None:
            return
        stored = dict(self._get_cfg(pid))
        entry = next((e for p, i, e, _ in self._catalog(slot)
                      if p == base and i == eid), {})
        is_local = entry.get("local") is True
        entry_values = entry.get("values") or {}
        # An entry that MENTIONS the address has answered it — including
        # answering it with "", which is what the local speech recogniser does
        # because it does not use one at all. Testing the value rather than the
        # presence of the key is what put an empty "API address" box under
        # "Whisper on this computer", inviting somebody to fill in a field that
        # backend never reads.
        custom = "base_url" not in entry_values

        # Three buckets, tracked explicitly.
        #
        # `isHidden()` is NOT a usable test for "I hid this on purpose": a widget
        # that has not been added to a layout yet is already hidden, so asking it
        # returned True for every field and the form came up completely empty.
        # Which of these a field lands in is a decision made here, so it is kept
        # here rather than inferred from Qt afterwards.
        shown: dict = {}        # drawn now
        carried: dict = {}      # kept for saving, never drawn — answered by the catalog
        advanced: list = []     # drawn only behind MORE

        for f in decl.get("needs", ()):
            if not isinstance(f, Field):
                continue
            # Do not show what the catalog entry already answered. The address
            # somebody did not have to type is one more thing to read and one
            # more thing to break; a key a local server never asks for is a box
            # that makes people think they need an account.
            if f.key == "base_url" and not custom:
                carried[f.key] = (self._line(
                    str(stored.get(f.key, f.default) or "")), f)
                continue
            if f.key == "api_key" and is_local:
                carried[f.key] = (self._line(str(stored.get(f.key, "") or "")), f)
                continue
            # The provider declares the key optional because local servers take
            # none — and that one declaration was printing "(optional)" and
            # "Local servers usually need none" under MiniMax and OpenAI, which
            # refuse every request without one.
            if f.key == "api_key" and entry.get("local") is False:
                f = replace(f, required=True,
                            hint=f"Needed — create one in your {entry['label']} account")
            if f.key == "backend":
                carried[f.key] = (self._line(
                    str(entry.get("values", {}).get("backend")
                        or stored.get(f.key, f.default) or "")), f)
                continue
            w = self._make_widget(pid, f, stored.get(f.key, f.default))
            if f.advanced:
                advanced.append((f, w))
            else:
                shown[f.key] = (w, f)

        for _key, (w, f) in shown.items():
            lay.addWidget(self._label(
                f.label + ("" if f.required else "   (optional)"), 7, C.TEXT_DIM))
            lay.addWidget(w)
            # A text box already shows its hint as placeholder text. Adding the
            # label as well printed every hint twice, which is how the panel
            # came to look careless.
            if f.hint and not isinstance(w, QLineEdit):
                lay.addWidget(self._label(f.hint, 7, C.TEXT_DIM))

        fields = dict(shown)
        fields.update(carried)
        self._hidden[pid] = set(carried) | {f.key for f, _w in advanced}

        # Buttons and status.
        status = self._label("", 7, C.TEXT_DIM)
        self._status[pid] = status

        brow = QHBoxLayout(); brow.setSpacing(6)
        brow.addWidget(self._btn("TEST", lambda _=0, p=pid: self._test(p), C.PRI))
        # Not "LIST" any more: the list arrives on its own when a service is
        # chosen. This is the button for when it did not — the server was
        # starting up, or a model has just been pulled.
        if any(k in shown for k in ("model", "voice_id", "voice")):
            brow.addWidget(self._btn("↻ REFRESH", lambda _=0, p=pid: self._list(p)))
        if advanced:
            brow.addWidget(self._btn(
                ("⌃ LESS" if self._more.get(pid) else "⌄ MORE"),
                lambda _=0, p=pid: self._toggle_more(p)))
        if not decl.get("_available", True) and decl.get("install"):
            brow.addWidget(self._btn(f"INSTALL {decl['install']}",
                                     lambda _=0, p=pid: self._install(p), C.ACC2))
        brow.addStretch(1)
        holder = QWidget()
        holder.setStyleSheet("background: transparent; border: none;")
        holder.setLayout(brow)
        lay.addWidget(holder)
        lay.addWidget(status)

        if self._more.get(pid):
            lay.addWidget(self._label("─" * 46, 7, C.BORDER))
            for f, w in advanced:
                lay.addWidget(self._label(f"{f.label}   (optional)", 7, C.TEXT_DIM))
                lay.addWidget(w)
                if f.hint and not isinstance(w, QLineEdit):
                    lay.addWidget(self._label(f.hint, 7, C.TEXT_DIM))
                fields[f.key] = (w, f)
                self._hidden[pid].discard(f.key)
        else:
            for f, w in advanced:
                fields[f.key] = (w, f)

        self._edits[pid] = fields

    def _make_widget(self, pid: str, f, value):
        if f.kind == "choice":
            w = self._combo()
            choices = list(self._live_choices(pid, f.key) or f.choices)
            if not f.required:
                w.addItem("— default —", "")
            for c in choices:
                w.addItem(str(c), str(c))
            w.setEditable(f.key in ("base_url", "model", "voice_id"))
            if w.isEditable():
                w.lineEdit().setStyleSheet(f"color: {C.TEXT}; background: transparent;")
            if value:
                j = w.findData(str(value))
                if j < 0:
                    w.addItem(str(value), str(value))
                    j = w.count() - 1
                w.setCurrentIndex(j)
                if w.isEditable():
                    w.setCurrentText(str(value))
            return w
        if f.kind == "bool":
            w = self._combo()
            w.addItem("no", "")
            w.addItem("yes", "1")
            w.setCurrentIndex(1 if str(value) in ("1", "True", "true") else 0)
            return w
        w = self._line(str(value if value is not None else ""))
        if f.kind == "secret":
            w.setEchoMode(QLineEdit.EchoMode.Password)
        if f.hint:
            w.setPlaceholderText(f.hint[:64])
        return w

    def _toggle_more(self, pid: str) -> None:
        self._more[pid] = not self._more.get(pid)
        self._remember_edits()
        QTimer.singleShot(0, self._rebuild_body)

    def _sidecar_box_widget(self) -> QWidget:
        head = self._label("⚡  BACKGROUND REQUESTS", 9, C.PRI, bold=True)
        w, lay = self._box([head])
        lay.addWidget(self._label(
            "Reading a document, looking at your screen, writing code — the "
            "assistant's own requests, not the conversation. These use Gemini's "
            "text quota, which runs out before the voice does. Sending them "
            "somewhere else keeps them working, and changes nothing you hear.",
            7, C.TEXT_DIM))

        # Read the stored choice BEFORE the box exists. The first version asked
        # the combo — and at this point that is the empty box being built, so
        # the answer was always "nothing chosen" and the form never appeared.
        sid = self._sidecar

        def fmt(entry, _decl):
            mark = "🖥" if entry.get("local") else (
                "☁" if entry.get("local") is False else "  ")
            return f"{mark}  Fall back to {entry['label']}"

        self._sidecar_combo = self._service_combo(
            "llm", ("Gemini only  —  the way it has always been", ""), fmt)
        self._select(self._sidecar_combo, sid)
        self._sidecar_combo.currentIndexChanged.connect(self._sidecar_changed)
        lay.addWidget(self._sidecar_combo)

        self._first_combo = self._combo()
        self._first_combo.addItem("Try Gemini first  —  faster", "after")
        self._first_combo.addItem(
            "Try the other one first  —  keeps more on your machine", "before")
        self._first_combo.setCurrentIndex(1 if self._sidecar_first else 0)
        lay.addWidget(self._first_combo)

        # In pipeline mode the model chosen for ② THINKING may answer these too,
        # and then there is nothing more to fill in. In Gemini mode there IS no
        # ② section, so a service picked here gets its own form, right here.
        if sid:
            if self._mode == "pipeline" and sid == self._pipe.get("llm"):
                lay.addWidget(self._label(
                    "Uses the model you chose for ② THINKING.", 7, C.TEXT_DIM))
            else:
                self._fields_into(lay, "llm", sid)
        else:
            lay.addWidget(self._label(
                "Left as it is, these keep using Gemini and stop working when "
                "its text quota runs out.", 7, C.TEXT_DIM))
        return w

    def _coding_box_widget(self) -> QWidget:
        """Which model dev_agent writes code with.

        Separate from background requests because it is the one job where the
        model makes the most difference and the requests are the longest — a
        whole source file at a time. Whatever is picked is tried FIRST for code,
        with Gemini behind it, so a service having a bad day still produces a
        project instead of nothing."""
        head = self._label("🛠  CODING", 9, C.PRI, bold=True)
        w, lay = self._box([head])
        lay.addWidget(self._label(
            "The model that plans, writes and fixes projects when you ask the "
            "assistant to build something. Saved to Desktop/JarvisProjects.",
            7, C.TEXT_DIM))

        sid = self._coding

        def fmt(entry, _decl):
            mark = "🖥" if entry.get("local") else (
                "☁" if entry.get("local") is False else "  ")
            star = "   ★ recommended" if entry.get("id") == "minimax" else ""
            return f"{mark}  Code with {entry['label']}{star}"

        self._coding_combo = self._service_combo(
            "llm", ("Same as background requests", ""), fmt)
        # The sponsor's model sits directly under the default, where it is the
        # first real choice anybody sees — not buried twelfth in the list.
        for i in range(self._coding_combo.count()):
            if (self._coding_combo.itemData(i) or "").endswith("@minimax"):
                text = self._coding_combo.itemText(i)
                data = self._coding_combo.itemData(i)
                self._coding_combo.removeItem(i)
                self._coding_combo.insertItem(1, text, data)
                break
        self._select(self._coding_combo, sid)
        self._coding_combo.currentIndexChanged.connect(self._coding_changed)
        lay.addWidget(self._coding_combo)

        if not sid:
            lay.addWidget(self._label(
                "Code is written by whatever answers background requests above.",
                7, C.TEXT_DIM))
        elif sid == self._sidecar:
            lay.addWidget(self._label(
                "Uses the settings under BACKGROUND REQUESTS.", 7, C.TEXT_DIM))
        elif self._mode == "pipeline" and sid == self._pipe.get("llm"):
            lay.addWidget(self._label(
                "Uses the model you chose for ② THINKING.", 7, C.TEXT_DIM))
        else:
            self._fields_into(lay, "llm", sid)
        return w

    def _sidecar_changed(self, _ix) -> None:
        sid = self._sidecar_combo.currentData() or ""
        self._remember_edits()
        self._sidecar = sid
        if sid:
            self._apply_entry("llm", sid)
            self._fetch_models(sid)
        QTimer.singleShot(0, self._rebuild_body)

    def _coding_changed(self, _ix) -> None:
        sid = self._coding_combo.currentData() or ""
        self._remember_edits()
        self._coding = sid
        if sid:
            self._apply_entry("llm", sid)
            self._fetch_models(sid)
        QTimer.singleShot(0, self._rebuild_body)

    def _update_footer(self) -> None:
        if self._mode != "pipeline":
            self._footer.setText(
                "Gemini Live. Nothing on this panel changes the conversation.")
            return
        missing = [name for slot, _n, name, _w in self._SLOTS
                   if not self._pipe.get(slot)]
        if "THINKING" in missing:
            self._footer.setText(
                "⚠  ② THINKING is not set — without it the assistant falls back "
                "to Gemini Live and says so in the log.")
        elif missing:
            words = " and ".join(f"{m.lower()}" for m in missing)
            self._footer.setText(
                f"Only {words} is missing — it will run, but you will type "
                f"instead of speak, or read instead of listen.")
        else:
            self._footer.setText(
                "All three set. SAVE & APPLY reconnects; the conversation is kept.")

    # ── actions ──────────────────────────────────────────────────────────────
    def _live_choices(self, pid: str, key: str) -> tuple:
        try:
            decl = self._registry.get_declaration(pid)
            mod = __import__(f"providers.{decl['_module']}", fromlist=["choices_for"])
            fn = getattr(mod, "choices_for", None)
            return tuple(fn(key)) if fn else ()
        except Exception:
            return ()

    @staticmethod
    def _combo_value(w: QComboBox) -> str:
        """What a dropdown stands for, as opposed to what it shows.

        A listed entry SHOWS a label and STANDS FOR its data — a voice's name
        and its ID. An editable box used to be read by its text alone, so
        picking "Gideon" from the ElevenLabs list saved the name where the ID
        belongs and every sentence failed with "invalid ID". Text that matches
        the selected entry's label is that entry, so its data is the value; any
        other text was typed, and is its own value.
        """
        if not w.isEditable():
            return str(w.currentData() or "")
        text = w.currentText().strip()
        index = w.currentIndex()
        if index >= 0 and text == w.itemText(index).strip():
            data = w.itemData(index)
            return str(data if data is not None else text).strip()
        return text

    def _values(self, pid: str) -> dict:
        out = {}
        hidden = self._hidden.get(pid, set())
        stored = self._get_cfg(pid)
        for key, (w, _f) in self._edits.get(pid, {}).items():
            if isinstance(w, QComboBox):
                value = self._combo_value(w)
            else:
                value = w.text().strip()
            # A field nobody can see must never blank out a setting that has
            # one. These widgets are rebuilt on every redraw, and a redraw that
            # raced a background fetch could recreate a hidden address as empty
            # and then save that over a working one — leaving a pipeline that
            # had been configured correctly pointing at nothing, with no visible
            # cause. Clearing a value is still possible; it just has to be done
            # in a box that is on screen.
            if not value and key in hidden and stored.get(key):
                continue
            out[key] = value
        return out

    def _remember_edits(self) -> None:
        """Keep what has been typed when the form is rebuilt.

        Without it, changing the speech engine wiped an API key somebody had
        just entered into the box above."""
        from memory.config_manager import save_provider_config
        for pid in list(self._edits):
            try:
                save_provider_config(pid, self._values(pid))
            except Exception:
                pass

    def _say(self, pid: str, text: str, colour: str) -> None:
        lb = self._status.get(pid)
        if lb is not None:
            lb.setText(text)
            lb.setStyleSheet(f"color: {colour}; background: transparent; border: none;")

    def _test(self, pid: str) -> None:
        # Read the boxes BEFORE letting Qt run. `processEvents` will happily
        # deliver a queued rebuild, which deletes the very widgets this is
        # about to read — and a deleted QWidget raises rather than returning
        # something wrong, so it takes the click down with it.
        values = self._values(pid)
        self._say(pid, "checking…", C.TEXT_DIM)
        QApplication.processEvents()
        try:
            inst = self._registry.build(pid, values)
            probe = getattr(inst, "probe", None)
            if probe is None:
                self._say(pid, "Nothing to check for this one.", C.TEXT_DIM)
                return
            ok, msg = probe()
        except Exception as e:
            self._say(pid, f"✕  {type(e).__name__}: {str(e)[:150]}", C.RED)
            return
        self._say(pid, ("✓  " if ok else "✕  ") + msg, C.GREEN if ok else C.ACC)
        # TEST is also where "can this model see?" gets answered — by asking the
        # endpoint, not the user. Write the answer back into the stored settings
        # so the assistant is told the truth about its own eyes.
        seen = (getattr(inst, "caps", {}) or {}).get("vision")
        if ok and seen is not None:
            from memory.config_manager import save_provider_config
            save_provider_config(pid, {"vision": "1" if seen else ""})
            entry = self._edits.get(pid, {}).get("vision")
            if entry is not None and isinstance(entry[0], QComboBox):
                entry[0].setCurrentIndex(1 if seen else 0)

    def _list(self, pid: str) -> None:
        values = self._values(pid)          # before processEvents — see _test
        self._say(pid, "reading…", C.TEXT_DIM)
        QApplication.processEvents()
        try:
            inst = self._registry.build(pid, values)
            if hasattr(inst, "models"):
                items, key = [(m, m) for m in inst.models()], "model"
            elif hasattr(inst, "voices"):
                items, key = list(inst.voices()), "voice_id"
            else:
                self._say(pid, "Nothing to list for this one.", C.TEXT_DIM)
                return
        except Exception as e:
            self._say(pid, f"✕  {type(e).__name__}: {str(e)[:150]}", C.RED)
            return

        entry = self._edits.get(pid, {}).get(key)
        if entry is None or not items:
            self._say(pid, f"✓  {len(items)} found." if items else "None found.",
                      C.GREEN if items else C.TEXT_DIM)
            return
        box = entry[0]
        if isinstance(box, QComboBox):
            keep = self._combo_value(box)
            box.clear()
            for value, label in items:
                box.addItem(str(label), str(value))
            j = box.findData(keep)
            if j < 0 and keep:
                j = box.findText(keep)
            box.setCurrentIndex(j if j >= 0 else 0)
        else:
            self._say(pid, f"✓  {len(items)} available: "
                           f"{', '.join(str(v) for v, _ in items[:8])}", C.GREEN)
            return
        self._say(pid, f"✓  {len(items)} available — pick one above.", C.GREEN)

    def _install(self, pid: str) -> None:
        """Fetch a provider's optional dependency, off the Qt thread — pip takes
        tens of seconds and a frozen window looks like a crashed one."""
        import subprocess
        import sys as _sys
        import threading

        decl = self._registry.get_declaration(pid) or {}
        pkg = decl.get("install")
        if not pkg:
            return
        self._say(pid, f"installing {pkg} — this can take a minute…", C.ACC2)

        def work():
            try:
                r = subprocess.run(
                    [_sys.executable, "-m", "pip", "install", pkg],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=900)
                good = r.returncode == 0
                tail = r.stdout.decode("utf-8", "replace").strip().splitlines()
                msg = ("✓  installed — close and reopen this panel"
                       if good else (tail[-1][:150] if tail else "pip failed"))
            except Exception as e:
                good, msg = False, f"{type(e).__name__}: {str(e)[:120]}"
            QTimer.singleShot(0, lambda: self._say(pid, msg,
                                                   C.GREEN if good else C.RED))

        threading.Thread(target=work, daemon=True).start()

    def _apply(self) -> None:
        from memory.config_manager import save_providers

        # ONE write, not five. See save_providers for what the five cost.
        creds = {sid: self._values(sid) for sid in list(self._edits)}

        save_providers(
            mode=self._mode,
            pipeline={"stt": self._pipe.get("stt", ""),
                      "llm": self._pipe.get("llm", ""),
                      "tts": self._pipe.get("tts", "")},
            creds=creds,
            sidecar=self._sidecar,
            sidecar_first=self._first_combo.currentData() == "before",
            coding=self._coding,
        )

        # Providers are cached by configuration; a changed setting must not go
        # on being served by an object built from the old one.
        try:
            self._registry.reset()
        except Exception:
            pass

        self.hide()
        # Background-request and coding settings take effect on the next call
        # with no reconnect. Only the conversation engine needs the session
        # rebuilt — and it does whenever anything it was BUILT from changed.
        #
        # Comparing only the mode and which service fills each slot is what
        # made APPLY look broken: a corrected model name, a new key or another
        # voice changed none of those, so nothing reconnected and the running
        # engine went on using the old settings until a restart. And the
        # "before" has to be taken when the panel opened — the form saves as
        # it is edited, so reading it here would compare the new settings with
        # themselves.
        if self._engine_fingerprint() != self._opened_engine:
            self.changed.emit()

    @staticmethod
    def _engine_fingerprint() -> str:
        """Everything the conversation engine is built from, as one string."""
        from memory.config_manager import (get_pipeline, get_provider_config,
                                           get_provider_mode)
        mode, pipe = get_provider_mode(), dict(get_pipeline())
        used = {sid: get_provider_config(sid) for sid in pipe.values() if sid} \
            if mode == "pipeline" else {}
        return json.dumps([mode, pipe, used], sort_keys=True, default=str)


