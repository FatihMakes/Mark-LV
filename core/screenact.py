"""
core/screenact.py — the hands. The twin of core/screenview.py, which is the eyes.

THE ONE RULE THIS FILE EXISTS TO ENFORCE
    A click lands on an ELEMENT, not on a coordinate a model made up.

    That sentence is the difference between a screen agent that works and one
    that occasionally buys something. A language model asked "where is the send
    button" answers with two numbers, and those two numbers are a guess made
    from a downscaled picture. When the guess is off by forty pixels it does not
    fail — it clicks whatever is forty pixels away, reports success, and there
    is no undo for a click.

    So the model's job is reduced to naming what it wants. Finding it is
    screenview's job, and it does that from the accessibility tree, which knows
    the exact rectangle. This module then acts on that rectangle. A raw
    coordinate can still be used — the vision fallback has to put one somewhere
    — but it arrives as a Point that must declare where it came from, and every
    result says which it was. Nothing here accepts an anonymous pair of numbers.

WHY PERCEPTION AND ACTUATION ARE SEPARATE FILES BUT THE SAME PAIR
    They share one contract and it is easy to break: every rectangle, every
    click, every drag in this project is in LOGICAL coordinates, the space the
    mouse is driven in, never the physical pixels a screenshot is made of.
    screenview converts on the way out. This file assumes that and converts
    nothing, which is why there is no scale factor anywhere below.

    Keeping them apart is what makes perception free of side effects: look() can
    be called in a loop, in a test, on a timer, and nothing moves.

WHAT IT REFUSES TO DO, AND WHY REFUSING IS THE FEATURE
    * A disabled control. Clicking one does nothing and the agent then believes
      it did something.
    * A control that is off screen. It needs scrolling into view first, and
      clicking where it "would be" hits whatever is actually there.
    * A control with no real area.
    * Typing into a field the operating system marks as secret. Not because a
      label said "password" — labels are in every language and can lie — but
      because the platform itself flagged it. This one is unconditional: there
      is no parameter that turns it off.
    * Anything at all, on a system that does not permit it. Wayland and an
      ungranted macOS both accept synthetic input and silently discard it, so
      here it is an honest failure instead of a click that never happened.

EVERY RESULT IS AN OBSERVATION, NOT AN INTENTION
    An Act says what was attempted, whether it went, and what could be checked.
    `ok` means the input was delivered. It does NOT mean the application did
    what you hoped — nothing at this layer can know that, and pretending
    otherwise is exactly the false success core/outcome.py was built to end.
    Whether the goal advanced is the executor's question, answered by looking
    again.

NO LANGUAGE, ANYWHERE
    Key names are ASCII identifiers from the pyautogui vocabulary plus aliases
    for what people actually say. Modifier names are mapped per platform, so
    "ctrl+s" is Command+S on a Mac without any caller knowing. Text is sent as
    CHARACTERS through each platform's Unicode input, never as key presses,
    because a key-press typist produces what a US keyboard produces and most of
    the world does not use one.
"""
from __future__ import annotations

import os
import platform as _platform
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional, Union

from core.lazyimport import LazyModule, installed
from core.screenview import Element

_HAS_PYAUTOGUI = installed("pyautogui")
_HAS_PYPERCLIP = installed("pyperclip")


def _configure_pyautogui(module: Any) -> None:
    # Slamming the mouse into the top-left corner aborts whatever is running.
    # It is the only stop control that needs no software to be listening, which
    # makes it the only one that still works when everything else is wedged.
    module.FAILSAFE = True
    # Settling is this module's job and it does it deliberately, per action.
    # A blanket pause would be paid by every keystroke of a long paste.
    module.PAUSE = 0.0


pyautogui = LazyModule("pyautogui", _configure_pyautogui)
pyperclip = LazyModule("pyperclip")


# ---------------------------------------------------------------------------
# Timing.
#
# These are small on purpose. The executor waits for the SCREEN to settle after
# an action, which is a measurement; these are only the millisecond-scale
# waits that the input stack itself needs, and making them generous here would
# be paying twice.
# ---------------------------------------------------------------------------
MOVE_S = 0.16          # cursor travel, long enough for hover states to fire
HOVER_S = 0.05         # let a hover-activated control notice the cursor
AFTER_CLICK_S = 0.05
AFTER_KEY_S = 0.03
CLIPBOARD_S = 0.07     # the clipboard is asynchronous on every platform
ASCII_INTERVAL = 0.012


# ---------------------------------------------------------------------------
# Targets
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Point:
    """A bare coordinate, and a statement of where it came from.

    `origin` is not decoration. When something clicks the wrong thing, the
    first question is whether the coordinate was read from the accessibility
    tree or guessed by a model, and a log that cannot answer that is a log that
    cannot be debugged. It also lets a caller apply a stricter policy to
    guessed points than to known ones without inspecting types.

    Accepted origins:
        "element"   the centre of a real control (built by from_element)
        "vision"    a model looked at a marked-up screenshot and chose a box
        "user"      a person pointed at it
        "relative"  computed from another element, e.g. a drag destination
    """

    x: int
    y: int
    origin: str = "vision"
    label: str = ""

    @staticmethod
    def from_element(el: Element) -> "Point":
        cx, cy = el.center
        return Point(cx, cy, origin="element", label=el.label)


Target = Union[Element, Point]


@dataclass
class Act:
    """What happened when we tried.

    Deliberately shaped like core/outcome.py's vocabulary without importing it:
    this layer reports facts, and the tool that called it turns those into the
    done / unverified / failed line the model reads. Two translations of the
    same event would be one too many.
    """

    ok: bool
    action: str
    detail: str = ""
    error: str = ""
    target: str = ""          # what we aimed at, in the screen's own language
    origin: str = ""          # where the coordinate came from
    point: tuple[int, int] = (0, 0)
    warnings: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.ok

    def __str__(self) -> str:
        head = self.action if self.ok else f"{self.action} FAILED"
        bits = [head]
        if self.target:
            bits.append(repr(self.target[:48]))
        if self.detail:
            bits.append(self.detail)
        if self.error:
            bits.append(f"({self.error})")
        if self.warnings:
            bits.append("warn: " + "; ".join(self.warnings))
        return " ".join(bits)


def _fail(action: str, error: str, **kw) -> Act:
    return Act(ok=False, action=action, error=error, **kw)


# ---------------------------------------------------------------------------
# Can this machine be driven at all?
#
# Asked once and cached. platform_support.detect() reads the environment and
# the OS, neither of which changes inside a session, and asking it before every
# keystroke would put a filesystem probe in the input path.
# ---------------------------------------------------------------------------
_support: Any = None
_support_lock = threading.Lock()


def _permitted() -> tuple[bool, str]:
    """(may we drive input, why not). Never raises."""
    global _support
    with _support_lock:
        if _support is None:
            try:
                from core import platform_support
                _support = platform_support.detect()
            except Exception:
                _support = False        # sentinel: could not tell, assume yes
    if _support is False or _support is None:
        return True, ""
    if getattr(_support, "can_control_input", True):
        return True, ""
    notes = "; ".join(getattr(_support, "notes", []) or [])
    return False, (notes or "this session does not permit programs to drive "
                            "the keyboard and mouse")


def available() -> tuple[bool, str]:
    """Is actuation possible here at all? ((True, "") when it is.)"""
    if not _HAS_PYAUTOGUI:
        return False, "pyautogui is not installed. Run: pip install pyautogui"
    return _permitted()


# ---------------------------------------------------------------------------
# Aiming
# ---------------------------------------------------------------------------
def _screen_size() -> tuple[int, int]:
    try:
        w, h = pyautogui.size()
        return int(w), int(h)
    except Exception:
        return 0, 0


def _on_screen(x: int, y: int) -> bool:
    """Is this point on ANY monitor?

    The primary monitor alone was the old test, so every control on a second
    screen was refused as "outside the visible screen" — a whole display the
    agent could see in a window list and never touch.
    """
    try:
        from core import screenview
        rect = screenview.virtual_rect()
        left, top = int(rect["left"]), int(rect["top"])
        width, height = int(rect["width"]), int(rect["height"])
    except Exception:
        left, top = 0, 0
        width, height = _screen_size()
    if not (width and height):
        return True               # nobody could say; the OS clamps anyway
    return left <= x < left + width and top <= y < top + height


# ---------------------------------------------------------------------------
# The assistant's own windows are never a target
#
# The agent's HUD sits on the same desktop it works on, shows the goal and a log
# of every step, and is very often the window in front when a task starts. A
# click that lands on it, or keystrokes delivered while it has the keyboard, do
# something to the assistant instead of to the task — and a vision model shown
# the HUD is shown exactly the words it is looking for. So these two checks run
# before input is delivered, on every platform, and refuse rather than warn.
# ---------------------------------------------------------------------------
SELF_POINT = ("that spot is on the assistant's own window; bring the program "
              "the task is in to the front first")
SELF_FOCUS = ("the assistant's own window has the keyboard, so keys would go to "
              "the assistant; bring the program the task is in to the front "
              "first")


def _lands_on_self(x: int, y: int) -> bool:
    try:
        from core import screenview
        pid = screenview.window_pid_at(int(x), int(y))
        return bool(pid) and screenview.is_own_pid(pid)
    except Exception:
        return False


def _self_has_keyboard() -> bool:
    try:
        from core import screenview
        pid = screenview.foreground_pid()
        return bool(pid) and screenview.is_own_pid(pid)
    except Exception:
        return False


def _resolve(target: Target, action: str, *, for_typing: bool = False
             ) -> tuple[Optional[Point], Optional[Act]]:
    """A target to a point, or a refusal that says exactly what was wrong.

    Every guard here answers a failure that has actually been seen in screen
    agents, and each one is cheap: none of them costs a model call, a
    screenshot or a round trip.
    """
    if isinstance(target, Point):
        # A bare point has already skipped every check an element allows, which
        # is precisely why its origin is recorded and surfaced.
        if not _on_screen(target.x, target.y):
            return None, _fail(action, "that point is off the screen",
                               origin=target.origin, point=(target.x, target.y))
        return target, None

    el = target
    if el.width < 2 or el.height < 2:
        return None, _fail(action, "that control has no clickable area",
                           target=el.label, origin="element")

    if not el.enabled:
        return None, _fail(
            action,
            "that control is disabled, so clicking it would do nothing",
            target=el.label, origin="element")

    if for_typing and el.protected:
        # The one refusal with no override. See the module docstring.
        return None, _fail(
            action,
            "that is a password or other secret field; the user has to type "
            "in it themselves",
            target=el.label, origin="element")

    cx, cy = el.center
    if not _on_screen(cx, cy):
        return None, _fail(
            action,
            "that control is outside the visible screen; scroll it into view "
            "first",
            target=el.label, origin="element", point=(cx, cy))

    return Point.from_element(el), None


# A control this large is a container wearing a control's role: a page body, a
# canvas, an editor surface. Its centre is the middle of the screen and clicking
# there hits whatever happens to be under it. The aim is reported as imprecise
# rather than refused, because a maximised text editor really is one big field
# and clicking it to focus is a legitimate thing to want.
OVERSIZED_FRACTION = 0.6


def _aim_warnings(target: Target) -> list[str]:
    if not isinstance(target, Element):
        return []
    width, height = _screen_size()
    if not (width and height):
        return []
    if target.area >= OVERSIZED_FRACTION * width * height:
        return ["that control covers most of the screen, so its centre is not "
                "a precise place to click"]
    return []


def _occluding_process(point: Point) -> str:
    """The process owning whatever is actually at this point, when the platform
    will say cheaply. Empty string when it will not.

    A BEST EFFORT ON PURPOSE
        A control can be perfectly real, enabled and on screen and still be
        underneath something — a notification, a dropdown, a dialog that opened
        between the look and the click. Windows will name the window at a point
        for the cost of one call, which is worth taking.

        It is reported, never enforced. The check has false positives of its
        own (layered windows, a click-through overlay), and refusing to act on
        one would trade a rare misclick for a common refusal. The executor sees
        the warning next to the outcome and decides.
    """
    try:
        from core import screenview
        pid = screenview.window_pid_at(int(point.x), int(point.y))
        if not pid:
            return ""
        import psutil
        return str(psutil.Process(int(pid)).name() or "")
    except Exception:
        return ""


def _move_to(point: Point) -> None:
    pyautogui.moveTo(point.x, point.y, duration=MOVE_S)
    if HOVER_S:
        time.sleep(HOVER_S)


# ---------------------------------------------------------------------------
# Pointer
# ---------------------------------------------------------------------------
def click(target: Target, *, button: str = "left", clicks: int = 1,
          expect_process: str = "") -> Act:
    """Click a control.

    `expect_process` is the executable the caller believes owns this control.
    When given, and when the platform can answer, the process actually at that
    point is compared and a mismatch is attached as a warning. It is how "a
    notification slid over the button" stops being invisible.
    """
    action = {1: "click", 2: "double_click", 3: "triple_click"}.get(clicks, "click")
    if button == "right":
        action = "right_click"

    ok, why = available()
    if not ok:
        return _fail(action, why)

    point, refusal = _resolve(target, action)
    if refusal is not None:
        return refusal
    assert point is not None

    warnings: list[str] = _aim_warnings(target)
    try:
        _move_to(point)

        if _lands_on_self(point.x, point.y):
            return _fail(action, SELF_POINT, target=point.label,
                         origin=point.origin, point=(point.x, point.y))

        if expect_process:
            actual = _occluding_process(point)
            if actual and _differs(actual, expect_process):
                warnings.append(
                    f"the window at that point belongs to {actual}, not "
                    f"{expect_process}; something may be covering it")

        pyautogui.click(button=button, clicks=max(1, clicks),
                        interval=0.06 if clicks > 1 else 0.0)
        time.sleep(AFTER_CLICK_S)
    except Exception as e:
        if type(e).__name__ == "FailSafeException":
            return _fail(action, "aborted by the mouse-corner failsafe",
                         target=point.label, origin=point.origin,
                         point=(point.x, point.y))
        return _fail(action, f"the click could not be delivered: {e}",
                     target=point.label, origin=point.origin,
                     point=(point.x, point.y))

    return Act(ok=True, action=action, target=point.label, origin=point.origin,
               point=(point.x, point.y), warnings=warnings,
               detail=f"at {point.x},{point.y}")


def double_click(target: Target, **kw) -> Act:
    return click(target, clicks=2, **kw)


def right_click(target: Target, **kw) -> Act:
    return click(target, button="right", **kw)


def hover(target: Target) -> Act:
    """Move the cursor onto something without pressing anything.

    Menus that open on hover, tooltips that hold the only readable label, a
    control that only appears once the row under the cursor lights up: all of
    them need this, and none of them needs a click that might commit to
    something.
    """
    ok, why = available()
    if not ok:
        return _fail("hover", why)
    point, refusal = _resolve(target, "hover")
    if refusal is not None:
        return refusal
    assert point is not None
    try:
        _move_to(point)
    except Exception as e:
        return _fail("hover", f"the cursor could not be moved: {e}",
                     target=point.label, origin=point.origin)
    return Act(ok=True, action="hover", target=point.label, origin=point.origin,
               point=(point.x, point.y))


def drag(start: Target, end: Target, *, duration: float = 0.45) -> Act:
    ok, why = available()
    if not ok:
        return _fail("drag", why)

    a, refusal = _resolve(start, "drag")
    if refusal is not None:
        return refusal
    b, refusal = _resolve(end, "drag")
    if refusal is not None:
        return refusal
    assert a is not None and b is not None

    try:
        # The destination is checked before anything is pressed: a drag that
        # has started cannot be taken back halfway.
        if _lands_on_self(b.x, b.y):
            return _fail("drag", SELF_POINT, target=b.label, origin=b.origin)
        pyautogui.moveTo(a.x, a.y, duration=MOVE_S)
        if _lands_on_self(a.x, a.y):
            return _fail("drag", SELF_POINT, target=a.label, origin=a.origin)
        time.sleep(HOVER_S)
        pyautogui.dragTo(b.x, b.y, duration=max(0.1, duration), button="left")
    except Exception as e:
        if type(e).__name__ == "FailSafeException":
            return _fail("drag", "aborted by the mouse-corner failsafe")
        return _fail("drag", f"the drag could not be delivered: {e}",
                     target=a.label, origin=a.origin)
    return Act(ok=True, action="drag", target=a.label, origin=a.origin,
               point=(b.x, b.y), detail=f"{a.x},{a.y} -> {b.x},{b.y}")


# What one notch of a mouse wheel is, in the unit each platform's scroll call
# counts in. pyautogui passes its number straight through, and the three
# platforms read it three different ways:
#
#     Windows   wheel delta; one notch is 120 (WHEEL_DELTA)
#     macOS     lines; a notch is about three
#     X11       button 4/5 clicks; one click is one notch
#
# The old code sent 120 everywhere, which was right on Windows and asked macOS
# for 360 lines and X11 for 360 separate wheel clicks per notch.
_NOTCH = {"windows": 120, "mac": 3, "linux": 1}


def _wheel(direction: str, notches: int) -> None:
    """Turn the wheel `notches` times, in any of the four directions."""
    d = direction.strip().lower()
    horizontal = d.startswith(("l", "r"))
    # Up and right are positive on every platform's scroll API.
    sign = 1 if d.startswith(("u", "r")) else -1
    amount = sign * notches * _NOTCH.get(_OS, 1)
    if not horizontal:
        pyautogui.scroll(amount)
        return
    if _OS == "windows":
        # pyautogui's Windows hscroll is literally its vertical scroll, so a
        # horizontal request scrolled the page down. MOUSEEVENTF_HWHEEL is the
        # real horizontal wheel.
        import ctypes
        ctypes.WinDLL("user32").mouse_event(0x01000, 0, 0, int(amount), 0)
        return
    pyautogui.hscroll(amount)


def scroll(direction: str = "down", amount: int = 3, *,
           target: Optional[Target] = None) -> Act:
    """Scroll, optionally with the cursor parked over a particular container.

    Where the cursor is matters: a page with a scrollable sidebar scrolls
    whichever of the two the pointer is over, and an agent that scrolls the
    wrong panel then reports that the item it wanted is still not visible.
    """
    ok, why = available()
    if not ok:
        return _fail("scroll", why)

    label, origin = "", ""
    if target is not None:
        point, refusal = _resolve(target, "scroll")
        if refusal is not None:
            return refusal
        assert point is not None
        label, origin = point.label, point.origin
        try:
            pyautogui.moveTo(point.x, point.y, duration=MOVE_S)
        except Exception as e:
            return _fail("scroll", f"the cursor could not be moved: {e}")
        if _lands_on_self(point.x, point.y):
            return _fail("scroll", SELF_POINT, target=label, origin=origin)

    clicks = max(1, min(20, int(amount or 3)))
    try:
        _wheel(str(direction or "down"), clicks)
        time.sleep(AFTER_KEY_S)
    except Exception as e:
        return _fail("scroll", f"the scroll could not be delivered: {e}")

    return Act(ok=True, action="scroll", target=label, origin=origin,
               detail=f"{direction} x{clicks}")


# ---------------------------------------------------------------------------
# Keyboard
#
# The alias table maps what people and models actually write onto what
# pyautogui accepts. Everything in it is an ASCII identifier, not a word in
# anybody's language: "enter" is the name of a key in the library's vocabulary,
# the same way "Button" is the name of a role in UIA's.
# ---------------------------------------------------------------------------
_OS = {"Windows": "windows", "Darwin": "mac"}.get(_platform.system(), "linux")

_KEY_ALIASES = {
    "return": "enter", "cr": "enter", "newline": "enter",
    "escape": "esc",
    "del": "delete", "ins": "insert",
    "pgup": "pageup", "pgdn": "pagedown", "pagedn": "pagedown",
    "control": "ctrl", "ctl": "ctrl",
    "option": "alt", "opt": "alt",
    "cmd": "command", "meta": "command", "apple": "command",
    "super": "winleft", "win": "winleft", "windows": "winleft", "start": "winleft",
    "spacebar": "space", "spc": "space",
    "caps": "capslock",
    "back": "backspace", "bksp": "backspace",
    "arrowup": "up", "arrowdown": "down",
    "arrowleft": "left", "arrowright": "right",
}


def normalise_key(name: str) -> str:
    """One key name, in pyautogui's vocabulary, correct for this platform.

    The platform swap is the point. A caller writes "ctrl+s" once and it is
    Command+S on macOS and Control+S everywhere else, so no plugin, prompt or
    plan ever has to branch on the operating system to save a file.

    That promise was only half kept: "cmd" became Control off the Mac, but
    "ctrl" stayed Control ON the Mac, so every "ctrl+s", "ctrl+a" and "ctrl+v" a
    planner wrote did nothing there. "ctrl" and "cmd" are now both the
    platform's shortcut modifier. The rare shortcut that needs the physical
    Control key on a Mac is written "macctrl".
    """
    key = str(name or "").strip().lower()
    if key == "macctrl":
        return "ctrl"
    key = _KEY_ALIASES.get(key, key)
    if key in ("ctrl", "command"):
        return "command" if _OS == "mac" else "ctrl"
    if key == "winleft" and _OS == "mac":
        key = "command"
    return key


def press(keys: str, *, presses: int = 1) -> Act:
    """A key or a chord: "enter", "esc", "ctrl+s", "alt+tab", "ctrl+shift+n"."""
    ok, why = available()
    if not ok:
        return _fail("press", why)

    parts = [normalise_key(k) for k in re.split(r"[+\s]+", str(keys or "")) if k.strip()]
    if not parts:
        return _fail("press", "no key was given")
    if _self_has_keyboard():
        return _fail("press", SELF_FOCUS, target="+".join(parts))

    presses = max(1, min(50, int(presses or 1)))
    try:
        for _ in range(presses):
            if len(parts) == 1:
                pyautogui.press(parts[0])
            else:
                pyautogui.hotkey(*parts)
            time.sleep(AFTER_KEY_S)
    except Exception as e:
        if type(e).__name__ == "FailSafeException":
            return _fail("press", "aborted by the mouse-corner failsafe")
        return _fail("press", f"the keystroke could not be delivered: {e}")

    shown = "+".join(parts)
    return Act(ok=True, action="press", target=shown,
               detail=shown if presses == 1 else f"{shown} x{presses}")


# ---------------------------------------------------------------------------
# Typing
# ---------------------------------------------------------------------------
def _is_ascii(text: str) -> bool:
    try:
        text.encode("ascii")
        return True
    except Exception:
        return False


def type_text(text: str, *, target: Optional[Target] = None,
              clear_first: bool = False) -> Act:
    """Type into the focused field, or into `target` after clicking it.

    TEXT IS SENT AS CHARACTERS, NOT AS KEYS
        pyautogui types by pressing the keys a US keyboard would need, and asks
        the active layout which key that is — then adds Shift whenever the
        character needs Shift on a US keyboard. Measured on a Turkish Q layout:
        "@" went out as Ctrl+Alt+Shift+Q and '"' as Shift+the key left of 1,
        both wrong characters, silently. A web address with an @ in it could not
        be typed at all. The same happens on German, French, Nordic and most
        other layouts, and on every non-Latin one.

        So text never goes through key presses. Each platform has a way to
        deliver a character itself, independent of the layout:

            Windows   SendInput with KEYEVENTF_UNICODE
            macOS     a keyboard event carrying a Unicode string
            X11       xdotool type, when it is installed

        When that route is unavailable the clipboard carries it instead, and
        the clipboard the user had is put back afterwards.

    CLEARING A FIELD NEEDS A FIELD
        `clear_first` selects everything and deletes it. With a named field it
        empties that field. Without one it would select all of whatever has the
        keyboard — every file in a folder window, a whole document — and delete
        it. So without a target nothing is cleared, and the result says so.

    A field the platform marks as secret is refused before any of this, and
    that refusal has no override.
    """
    ok, why = available()
    if not ok:
        return _fail("type", why)

    text = str(text if text is not None else "")
    label, origin = "", ""
    warnings: list[str] = []

    if target is not None:
        point, refusal = _resolve(target, "type", for_typing=True)
        if refusal is not None:
            return refusal
        assert point is not None
        label, origin = point.label, point.origin
        clicked = click(target)
        if not clicked.ok:
            return _fail("type", f"the field could not be focused: {clicked.error}",
                         target=label, origin=origin)
        time.sleep(HOVER_S)
    elif _self_has_keyboard():
        return _fail("type", SELF_FOCUS)

    if clear_first:
        if target is None:
            warnings.append("nothing was cleared first, because no field was "
                            "named and select-all would have hit whatever has "
                            "the keyboard")
        else:
            cleared = press("ctrl+a")
            if cleared.ok:
                press("delete")

    try:
        how, error = _deliver_text(text)
    except Exception as e:
        if type(e).__name__ == "FailSafeException":
            return _fail("type", "aborted by the mouse-corner failsafe")
        how, error = "", str(e)
    if error:
        return _fail("type", f"the text could not be typed: {error}",
                     target=label, origin=origin)

    shown = text if len(text) <= 40 else text[:40] + "..."
    return Act(ok=True, action="type", target=label, origin=origin,
               warnings=warnings,
               detail=f"typed {shown!r} ({len(text)} chars, {how})")


def _deliver_text(text: str) -> tuple[str, str]:
    """(how it was delivered, "") or ("", why it could not be)."""
    if not text:
        return "nothing to type", ""
    reasons: list[str] = []
    native = {"windows": _type_windows, "mac": _type_mac,
              "linux": _type_linux}.get(_OS)
    if native is not None:
        try:
            problem = native(text)
        except Exception as e:
            problem = str(e)
        if problem is None:
            return "unicode input", ""
        reasons.append(problem)

    problem = _paste(text)
    if problem is None:
        return "clipboard", ""
    reasons.append(problem)

    # The last resort, and only for plain ASCII: key presses, which are right
    # on a US layout and may alter symbols on others. Said in the result.
    if _is_ascii(text):
        pyautogui.write(text, interval=ASCII_INTERVAL)
        return "key presses (symbols may differ on non-US keyboard layouts)", ""
    return "", "; ".join(reasons)


def _type_windows(text: str) -> Optional[str]:
    """SendInput with KEYEVENTF_UNICODE. None on success, else why not.

    Every character goes as its UTF-16 code units, so a character outside the
    Basic Multilingual Plane (most emoji) travels as its surrogate pair, which
    is exactly what the receiving window expects. Line breaks and tabs are sent
    as the real Enter and Tab keys: a Unicode "\\n" is not a keystroke to most
    applications.
    """
    import ctypes
    from ctypes import wintypes

    ulong_ptr = ctypes.c_size_t

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                    ("dwExtraInfo", ulong_ptr)]

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                    ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ulong_ptr)]

    class _UNION(ctypes.Union):
        # MOUSEINPUT is the largest member, and the union has to be that size
        # or SendInput rejects every record with ERROR_INVALID_PARAMETER.
        _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]

    class INPUT(ctypes.Structure):
        _anonymous_ = ("u",)
        _fields_ = [("type", wintypes.DWORD), ("u", _UNION)]

    INPUT_KEYBOARD, KEYUP, UNICODE = 1, 0x0002, 0x0004
    VK_RETURN, VK_TAB = 0x0D, 0x09

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
    user32.SendInput.restype = wintypes.UINT

    records: list[INPUT] = []

    def key(vk: int = 0, scan: int = 0, flags: int = 0) -> None:
        record = INPUT(type=INPUT_KEYBOARD)
        record.ki = KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags, time=0,
                               dwExtraInfo=0)
        records.append(record)

    for ch in text.replace("\r\n", "\n").replace("\r", "\n"):
        if ch == "\n" or ch == "\t":
            vk = VK_RETURN if ch == "\n" else VK_TAB
            key(vk=vk)
            key(vk=vk, flags=KEYUP)
            continue
        data = ch.encode("utf-16-le")
        for i in range(0, len(data), 2):
            unit = int.from_bytes(data[i:i + 2], "little")
            key(scan=unit, flags=UNICODE)
            key(scan=unit, flags=UNICODE | KEYUP)

    # In batches, with a breath between them: an application reading its
    # message queue slowly drops characters from one enormous burst.
    batch = 64
    for start in range(0, len(records), batch):
        chunk = records[start:start + batch]
        array = (INPUT * len(chunk))(*chunk)
        sent = user32.SendInput(len(chunk), array, ctypes.sizeof(INPUT))
        if sent != len(chunk):
            return (f"Windows accepted {sent} of {len(chunk)} input records "
                    f"(error {ctypes.get_last_error()})")
        time.sleep(0.01)
    return None


def _type_mac(text: str) -> Optional[str]:
    """Quartz keyboard events carrying Unicode strings. None on success."""
    import Quartz

    RETURN, TAB = 36, 48          # virtual key codes; positions, not letters

    def post_string(chunk: str) -> None:
        units = len(chunk.encode("utf-16-le")) // 2
        for down in (True, False):
            event = Quartz.CGEventCreateKeyboardEvent(None, 0, down)
            Quartz.CGEventKeyboardSetUnicodeString(event, units, chunk)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
        time.sleep(0.008)

    def post_key(code: int) -> None:
        for down in (True, False):
            Quartz.CGEventPost(Quartz.kCGHIDEventTap,
                               Quartz.CGEventCreateKeyboardEvent(None, code, down))
        time.sleep(0.008)

    # macOS reads at most 20 UTF-16 units from one event, so text goes in short
    # runs that never split a character.
    run = ""
    for ch in text.replace("\r\n", "\n").replace("\r", "\n"):
        if ch in "\n\t":
            if run:
                post_string(run)
                run = ""
            post_key(RETURN if ch == "\n" else TAB)
            continue
        if len((run + ch).encode("utf-16-le")) // 2 > 16:
            post_string(run)
            run = ""
        run += ch
    if run:
        post_string(run)
    return None


def _type_linux(text: str) -> Optional[str]:
    """xdotool on X11. None on success, else why not."""
    import shutil
    import subprocess

    if os.environ.get("WAYLAND_DISPLAY") and not os.environ.get("DISPLAY"):
        return "a Wayland session does not accept typed input from programs"
    tool = shutil.which("xdotool")
    if not tool:
        return "xdotool is not installed"
    result = subprocess.run(
        [tool, "type", "--clearmodifiers", "--delay", "8", "--", text],
        capture_output=True, timeout=max(10.0, len(text) * 0.05))
    if result.returncode != 0:
        return (result.stderr or b"").decode("utf-8", "replace").strip() \
            or f"xdotool exited with {result.returncode}"
    return None


def _paste(text: str) -> Optional[str]:
    """Put `text` in the field through the clipboard. None on success, else why not."""
    if not _HAS_PYPERCLIP:
        return "pyperclip is not installed. Run: pip install pyperclip"

    previous = None
    try:
        previous = pyperclip.paste()
    except Exception:
        previous = None

    try:
        pyperclip.copy(text)
    except Exception as e:
        return f"the clipboard could not be written: {e}"

    time.sleep(CLIPBOARD_S)
    try:
        pyautogui.hotkey(normalise_key("ctrl"), "v")
        time.sleep(CLIPBOARD_S * 2)
    finally:
        # Restored in a finally block on purpose: a failed paste is no reason
        # to also keep somebody else's text on the user's clipboard. An empty
        # reading is not restored, because text is all pyperclip can read: an
        # image on the clipboard reads as "" and writing "" back would erase it.
        if previous:
            try:
                pyperclip.copy(previous)
            except Exception:
                pass
    return None


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------
def _differs(a: str, b: str) -> bool:
    from core.appidentity import fold
    sa = fold(str(a).rsplit(".", 1)[0])
    sb = fold(str(b).rsplit(".", 1)[0])
    if not sa or not sb:
        return False
    return sa not in sb and sb not in sa


def focus_app(name: str) -> Act:
    """Bring an application's window to the front, by name, on any platform.

    Delegated to core/appidentity.py rather than reimplemented: that module
    already matches a spoken name against what is actually running, already
    knows the Windows foreground-lock dance, and already refuses to touch the
    assistant's own processes. Two implementations of "which app did they
    mean" would be one too many, and the second one would be the one without
    the guards.
    """
    ok, why = available()
    if not ok:
        return _fail("focus_app", why)
    try:
        from core import appidentity
        matches = appidentity.running(name)
        if not matches:
            return _fail("focus_app", f"nothing called {name!r} appears to be running",
                         target=name)
        if appidentity.focus(matches[0]):
            return Act(ok=True, action="focus_app", target=name,
                       detail="window raised")
        return _fail("focus_app",
                     "the window could not be raised; the system refused the "
                     "request", target=name)
    except Exception as e:
        return _fail("focus_app", f"the window could not be raised: {e}", target=name)


# ---------------------------------------------------------------------------
# Self test — `python -m core.screenact`
#
# It moves the mouse and it never presses anything. That is the only honest way
# to check aim: a probe that clicks to prove it can click has already done the
# thing you were trying to find out whether it should do.
# ---------------------------------------------------------------------------
def _self_test() -> None:
    from core import screenview

    ok, why = available()
    print(f"actuation available: {ok}" + (f" ({why})" if why else ""))
    print(f"platform key mapping: ctrl+s -> "
          f"{'+'.join(normalise_key(k) for k in ('ctrl', 's'))}")
    print(f"                      cmd+q  -> "
          f"{'+'.join(normalise_key(k) for k in ('cmd', 'q'))}")
    if not ok:
        return

    view = screenview.look()
    print(f"\n{view.summary()}")

    buttons = [el for el in view.elements if el.interactable]
    if not buttons:
        print("nothing interactable to aim at; open a window with buttons in it")
        return

    # A named control of ordinary size. Picking the LARGEST was the first
    # version and it picked a full-window content surface every time, which is
    # exactly the target this module warns about rather than a fair test of aim.
    named = [el for el in buttons if el.label and el.area < 120_000] or buttons
    biggest = max(named, key=lambda el: el.area)
    print(f"\naiming at [{biggest.eid}] {biggest.role} {biggest.label[:50]!r} "
          f"rect={biggest.rect}")
    result = hover(biggest)
    print(f"  {result}")

    print("\nguards (nothing is clicked):")
    disabled = Element(999, screenview.ROLE_BUTTON, "pretend", "", 10, 10, 40, 20,
                       enabled=False)
    print(f"  disabled control -> {click(disabled)}")
    secret = Element(998, screenview.ROLE_EDIT, "pretend", "", 10, 10, 40, 20,
                     protected=True)
    print(f"  secret field     -> {type_text('hunter2', target=secret)}")
    offscreen = Element(997, screenview.ROLE_BUTTON, "pretend", "",
                        -5000, -5000, 40, 20)
    print(f"  off screen       -> {click(offscreen)}")

    # Resolved but NOT clicked. A self test that proves it can click by
    # clicking has already done the thing you were checking whether it should
    # do, and at these coordinates that would be a click on the user's desktop.
    guessed = Point(120, 120, origin="vision", label="a box a model chose")
    resolved, refusal = _resolve(guessed, "click")
    verdict = refusal.error if refusal else f"allowed, origin={resolved.origin}"
    print(f"  guessed point    -> {verdict}   [resolved only, not clicked]")


if __name__ == "__main__":
    _self_test()
