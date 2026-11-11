"""
Play a video where the avatar normally is.

The HUD already had a surface for this — the live camera takes the centre of
the screen and gives it back — so a video is the same idea with a different
source, and it shares the same stack rather than inventing a second one. That
means the avatar, the camera and a video can never be on screen together.

WHAT IT ACCEPTS
    a local file        C:\\Users\\me\\clip.mp4, ~/Videos/holiday.mkv
    a direct media URL  https://example.com/trailer.mp4
    a YouTube link      https://youtube.com/watch?v=...
    a YouTube search    "play the new Dune trailer"

    The first two are handed straight to Qt. The last two have to be resolved
    to something playable first, which is what yt-dlp does; without it, this
    says so and offers to open the video in the browser instead of failing.

SOUND IS OFF UNTIL ASKED FOR
    A soundtrack talking over the assistant is the one way this feature could
    make JARVIS worse rather than better, and the microphone is open while it
    plays. So it starts muted, and the user turns sound on — from the button in
    the video header, or by saying so. Closing it works both ways too.
"""
from __future__ import annotations

import re
import threading
import time
from pathlib import Path

from core import outcome

_YT = re.compile(r"(youtube\.com/|youtu\.be/)", re.I)

# Which open request is the current one. Opening happens on a thread now, so
# "stop" can arrive while a video is still being resolved — and without this the
# stop would find nothing playing, say so, and then the video would appear
# anyway a second later. Bumping the number abandons whatever is in flight.
_open_lock = threading.Lock()
_open_seq = 0


def _begin_open() -> int:
    global _open_seq
    with _open_lock:
        _open_seq += 1
        return _open_seq


def _still_wanted(token: int) -> bool:
    with _open_lock:
        return token == _open_seq


def _is_url(text: str) -> bool:
    return text.lower().startswith(("http://", "https://", "file://"))


_IMAGE_EXTS = {".jpg", ".jpeg", ".jfif", ".png", ".bmp", ".webp", ".gif",
               ".tif", ".tiff", ".avif", ".heic"}
_VIDEO_EXTS = {".mp4", ".m4v", ".mkv", ".mov", ".avi", ".webm", ".wmv",
               ".flv", ".mpg", ".mpeg", ".ts", ".3gp", ".ogv"}


def _local_path(text: str) -> str:
    """The text as an existing file on disk, or ''. `file://` URIs included."""
    from urllib.parse import unquote, urlparse
    raw = (text or "").strip().strip('"').strip("'")
    if raw.lower().startswith("file://"):
        raw = unquote(urlparse(raw).path)
        if len(raw) > 2 and raw[0] == "/" and raw[2] == ":":   # file:///C:/x
            raw = raw[1:]
    if not raw:
        return ""
    try:
        p = Path(raw).expanduser()
        if p.is_file():
            return str(p.resolve())
    except Exception:
        pass
    return ""


def _looks_like_file(text: str) -> bool:
    """A path or a file name, as opposed to something to search YouTube for.

    Decided by shape - a folder separator, or a media extension on the end -
    never by the words around it, so it holds in every language. This is the
    check that was missing: "holiday.mp4" that was not found on disk fell
    through to a YouTube search, and from there to the browser.
    """
    raw = text.strip().strip('"').strip("'")
    if raw.lower().startswith("file://"):
        return True
    if _is_url(raw):
        return False
    suffix = Path(raw).suffix.lower()
    return ("/" in raw or "\\" in raw
            or suffix in _IMAGE_EXTS or suffix in _VIDEO_EXTS)


def _seconds(value) -> float | None:
    """A point or a distance in a video: 90, "90", "1:30", "01:02:03" -> seconds."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if ":" in text:
        try:
            parts = [float(p) for p in text.split(":")]
        except ValueError:
            return None
        total = 0.0
        for part in parts:
            total = total * 60 + part
        return total
    m = re.search(r"-?\d+(?:[.,]\d+)?", text)
    return float(m.group(0).replace(",", ".")) if m else None


def _clock(seconds: float) -> str:
    s = int(max(0, seconds))
    h, rest = divmod(s, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _show_local(player, path: Path) -> str:
    """Put a file from this machine on the display: a picture or a video.

    The kind is decided by the file, not by the verb the user used. "Show",
    "play", "open" and "put on the screen" are one request in every language,
    and the only honest way to tell a photo from a film is its extension.
    """
    if path.suffix.lower() in _IMAGE_EXTS:
        shower = getattr(player, "show_image", None)
        if not callable(shower):
            return outcome.failed("video_player",
                                  "this build of the display cannot show "
                                  "pictures", file=path.name)
        _begin_open()               # a YouTube open still resolving is abandoned
        shower(str(path), path.name)
        return f"status=showing image={path.name} where=display"
    _begin_open()
    player.show_video(str(path), path.name, muted=True)
    return f"status=playing file={path.name} where=display sound=muted"


def _resolve_youtube(url_or_query: str) -> tuple[str, str, str, str]:
    """(video_url, audio_url, title, error). Needs yt-dlp; says so when absent.

    TWO urls, because YouTube no longer offers one.
    The first version of this asked for a format carrying both picture and
    sound and got "Requested format is not available". That was not a bad
    selector, it was a wrong assumption: checked against three videos —
    including the oldest upload on the site — every one offered ZERO combined
    formats. Picture and sound are separate streams now, and both are returned
    so the player can run them together.

    `audio_url` comes back empty when only a combined or video-only stream
    exists, which is the normal case for everything that is not YouTube.
    """
    try:
        import yt_dlp
    except Exception:
        return "", "", "", ("yt-dlp is not installed, so a YouTube video "
                            "cannot be played inside the HUD. "
                            "Run: pip install yt-dlp")

    query = url_or_query if _YT.search(url_or_query) else f"ytsearch1:{url_or_query}"
    # No "format" here on purpose: ask for everything and choose below, so a
    # video that lacks whatever was requested cannot fail outright.
    opts = {"quiet": True, "no_warnings": True, "skip_download": True,
            "noplaylist": True}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(query, download=False)
        if info and info.get("entries"):
            info = info["entries"][0]
        if not info:
            return "", "", "", "nothing was found for that."
    except Exception as e:
        return "", "", "", f"the video could not be resolved: {e}"

    formats = [f for f in (info.get("formats") or [])
               if str(f.get("protocol") or "").startswith("http") and f.get("url")]

    def has(f, key):
        return f.get(key) not in (None, "none")

    def best(candidates):
        """Highest quality — but the ORIGINAL language before quality.

        YouTube auto-dubs a lot of videos now, and it offers every dub as its
        own audio stream at the SAME bitrate as the original: measured on one
        video, English original at 129.483 and Arabic, Bangla, German, Spanish,
        French, Hindi and Indonesian all at 129.482. Choosing on bitrate alone
        therefore came down to a thousandth of a kilobit, and a video would play
        in Arabic one time and English the next for no reason the user could
        see.

        YouTube marks the original track with language_preference 10 and every
        dub with -1, so that decides it first and bitrate only breaks ties
        within the same track. Video formats carry no language, so this is the
        same ordering for them with the first term always equal.
        """
        if not candidates:
            return None
        return max(candidates,
                   key=lambda f: (f.get("language_preference") or 0,
                                  f.get("tbr") or 0))

    # Combined first — if a video still has one, one player is simpler and
    # cannot drift. Then picture and sound separately.
    combined = best([f for f in formats if has(f, "vcodec") and has(f, "acodec")])
    if combined:
        return combined["url"], "", info.get("title") or "", ""

    # 720p is the ceiling on purpose: the HUD panel is nowhere near 4K, and a
    # smaller stream starts sooner and drifts less.
    video = best([f for f in formats
                  if has(f, "vcodec") and not has(f, "acodec")
                  and f.get("ext") == "mp4" and (f.get("height") or 0) <= 720])
    if not video:
        video = best([f for f in formats
                      if has(f, "vcodec") and not has(f, "acodec")])
    audio = best([f for f in formats
                  if has(f, "acodec") and not has(f, "vcodec")
                  and f.get("ext") == "m4a"])
    if not audio:
        audio = best([f for f in formats
                      if has(f, "acodec") and not has(f, "vcodec")])

    if not video:
        return "", "", "", "no playable stream was offered for that video."
    if audio and audio.get("language"):
        print(f"[Video] audio track: {audio.get('language')} "
              f"({audio.get('format_note') or audio.get('format_id')})")
    return video["url"], (audio or {}).get("url", ""), info.get("title") or "", ""


def video_player(parameters: dict = None, response=None, player=None,
                 session_memory=None) -> str:
    params = parameters or {}
    action = (params.get("action") or "play").strip().lower()
    # `source` is the declared name; a model that has just read another tool's
    # answer often says `path` or `url` instead, and refusing over the key name
    # is a failure the user cannot see the cause of.
    source = str(params.get("source") or params.get("path")
                 or params.get("url") or params.get("file")
                 or params.get("query") or "").strip()

    if player is None:
        return "There is no display to play a video on."

    # ---- stop -------------------------------------------------------------
    if action in ("stop", "close", "hide"):
        # Cancel an open that has not finished yet, so a video the user has
        # already changed their mind about never reaches the screen.
        was_opening = not player.video_is_playing()
        _begin_open()
        player.stop_video()
        if was_opening:
            return "Stopped it before it opened."
        return "Closed what was on the display."

    # ---- sound ------------------------------------------------------------
    if action in ("mute", "unmute", "sound_on", "sound_off"):
        if not player.video_is_playing():
            return "Nothing is playing."
        muted = action in ("mute", "sound_off")
        player.set_video_muted(muted)
        return "Muted the video." if muted else "Turned the video's sound on."

    # ---- position and pause -----------------------------------------------
    # Exact, because the display's own player knows where it is. Checked after:
    # the answer is the position the player reports, not the one that was asked.
    if action in ("seek", "forward", "back", "rewind", "seek_to", "pause", "resume"):
        state = player.video_state() if callable(getattr(player, "video_state", None)) else {}
        if state.get("kind") != "video":
            return outcome.failed(
                "video_player",
                "no video is playing on the assistant's display - for a video in another "
                "app or the browser, use computer_settings (seek_forward, seek_backward, "
                "media_play_pause)", wanted=action)

        if action in ("pause", "resume"):
            player.pause_video(action == "pause")
            time.sleep(0.4)
            after = player.video_state()
            if bool(after.get("paused")) == (action == "pause"):
                return outcome.done("video_player", wanted=action,
                                    position=_clock(after.get("position") or 0), verified=True)
            return outcome.unverified("video_player", "the player has not reported the change yet",
                                      wanted=action)

        amount = _seconds(params.get("seconds"))
        if action == "seek_to":
            target = _seconds(params.get("position"))
            if target is None:
                target = amount
            if target is None:
                return outcome.ask("video_player", "to which point in the video (minutes:seconds)?")
            relative = False
        else:
            if amount is None:
                amount = 10.0
            if action in ("back", "rewind"):
                amount = -abs(amount)
            elif action == "forward":
                amount = abs(amount)
            target, relative = amount, True

        duration = float(state.get("duration") or 0)
        expected = (float(state.get("position") or 0) + target) if relative else target
        if duration:
            expected = min(expected, duration)
        expected = max(0.0, expected)
        player.seek_video(float(target), relative)
        time.sleep(0.5)
        now = float(player.video_state().get("position") or 0)
        if abs(now - expected) <= 3:
            return outcome.done("video_player", wanted=action, position=_clock(now),
                                duration=_clock(duration) if duration else None, verified=True)
        return outcome.unverified(
            "video_player",
            "the jump was sent but the player has not reached that point yet - a stream "
            "may still be buffering", wanted=action, position=_clock(now), asked=_clock(expected))

    # ---- play / show ------------------------------------------------------
    if not source:
        return outcome.ask("video_player", "what should I play or show?")

    # WHERE the user meant is the model's call, because it is a question about
    # what they SAID, in whatever language they said it in. This file only
    # acts on the answer: a file request never reaches YouTube or the browser.
    where = (params.get("source_type") or "auto").strip().lower()
    wants_file = where in ("file", "local", "computer", "disk")

    local = _local_path(source)
    if local:
        return _show_local(player, Path(local))

    if wants_file or _looks_like_file(source):
        return outcome.failed(
            "video_player",
            f"there is no file at '{source}'. If the exact path is not known, "
            f"find it with file_controller (action='find') and call again with "
            f"the path it returns", asked=source)

    if _is_url(source) and not _YT.search(source):
        if Path(source.split("?")[0]).suffix.lower() in _IMAGE_EXTS:
            return outcome.failed(
                "video_player",
                "a picture on the web cannot be shown on the display, only a "
                "file on this computer - offer to open it in the browser",
                url=source)
        # A direct media URL can go straight to the player; a YouTube page cannot.
        _begin_open()
        player.show_video(source, source.rsplit("/", 1)[-1][:44], muted=True)
        return "status=playing where=display sound=muted"

    # ANSWER FIRST, OPEN SECOND.
    #
    # Working out what to play takes seconds that nothing can remove: measured
    # against YouTube, 3.3s for a spoken phrase (a search, then the video) and
    # 2.0s for a link, plus however long Qt spends buffering before the first
    # frame. Restricting yt-dlp to a lighter client was tried and is not the
    # answer — the fast clients came back with zero usable formats.
    #
    # So the seconds stay, and what changes is where the user spends them:
    # listening to JARVIS say it is coming, instead of watching nothing happen.
    # The same shape whatsapp_call uses, and for the same reason.
    threading.Thread(target=_play_youtube, args=(player, source, _begin_open()),
                     daemon=True, name="video-open").start()
    # Deliberately a status line rather than a finished English sentence: the
    # model writes the words, in the user's own language.
    return f"status=opening source={source}"


def _play_youtube(player, source: str, token: int) -> None:
    """The part that takes seconds. Speaks only if something goes wrong —
    silence means the video is on screen, which the user can see for
    themselves."""
    try:
        url, audio_url, title, err = _resolve_youtube(source)
    except Exception as e:                                  # noqa: BLE001
        url = audio_url = title = ""
        err = str(e)

    if not _still_wanted(token):
        return                       # the user closed it while it was resolving

    if url:
        try:
            player.show_video(url, title or source, muted=True,
                              audio_source=audio_url)
            return
        except Exception as e:                              # noqa: BLE001
            err = str(e)

    # Resolution failed, and the reason is reported rather than swallowed.
    #
    # This used to open a browser on its own, and in use that was the worst
    # thing it did: a request to watch something ON THE DISPLAY turned into a
    # browser window appearing unasked, very often for a search page built
    # out of the name of a file that was on the user's own disk. Whether to go
    # to the browser instead is the user's decision, so it is offered, not
    # taken — `browser_control` is one sentence away if they want it.
    _say(player, "Tell the user, in one short sentence in their own language, "
                 f"that '{source}' could not be played on the display ({err}). "
                 "Offer to open it in the browser instead; do NOT open it "
                 "unless they say yes.")


def _say(player, instruction: str) -> None:
    if player is not None and hasattr(player, "request_say"):
        try:
            player.request_say(instruction)
        except Exception:
            pass


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "video_player",
    "description": (
        "Puts a VIDEO or a PICTURE on the assistant's own display, where the "
        "avatar normally is - and closes it, mutes it or unmutes it. Use it "
        "whenever the user wants to watch or look at something on screen, "
        "whatever words or language they use for it. "
        "'source' accepts the full path of a file on this computer, a direct "
        "video URL, a YouTube link, or a description to search YouTube for. "
        "For a file, copy its path exactly from the tool that produced it; if "
        "you do not have the path, find the file with file_controller "
        "action='find' first - never invent one. Pictures (including one "
        "generate_image just made) are shown; videos play. "
        "Set 'source_type' from what the user MEANT: 'file' when it is "
        "something on their own computer, 'web' when they mean YouTube or "
        "something online, 'auto' when they did not say. "
        "Video always starts MUTED - only call action='unmute' when the user "
        "actually asks to hear it. action='stop' closes a picture as well. "
        "While a video is on the display: 'forward' / 'back' with seconds "
        "('skip 30 seconds' -> forward, seconds=30; no amount = 10), 'seek_to' "
        "with position ('go to 1:20' -> position='1:20'), 'pause', 'resume'. "
        "When it answers 'status=opening', say in one short sentence that you "
        "are putting it on the display, and do not wait or call it again. "
        "If it fails, report why; never open a browser instead unless the user "
        "asks for that. Do NOT use 'youtube_video' for this: that plugin works "
        "in the web browser and never shows anything on this display."
    ),
    # Used in place of `description` only when a small model is on a tool
    # budget - Gemini still gets the full text. See providers/schema.py.
    "brief": (
        "Shows a picture or plays a video on the HUD - a file path on this "
        "computer, a URL, a YouTube link, or a description to search for. "
        "source_type: file | web | auto."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": ("'play' (default; also shows pictures), 'stop' "
                                "to close it, 'unmute' to turn the sound on, "
                                "'mute' to silence it, 'forward' / 'back' by "
                                "seconds, 'seek_to' a position, 'pause', 'resume'."),
                "enum": ["play", "stop", "mute", "unmute", "forward", "back",
                         "seek_to", "pause", "resume"],
            },
            "seconds": {
                "type": "NUMBER",
                "description": "For forward / back: how many seconds (default 10).",
            },
            "position": {
                "type": "STRING",
                "description": "For seek_to: where to go, as minutes:seconds ('1:20') or seconds.",
            },
            "source": {
                "type": "STRING",
                "description": (
                    "For action='play': a file path on this computer, a direct "
                    "video URL, a YouTube link, or what to search "
                    "YouTube for. Pass what the user actually named, in their "
                    "own words."
                ),
            },
            "source_type": {
                "type": "STRING",
                "description": (
                    "Where the user means it is: 'file' on this computer, "
                    "'web' for YouTube or online, 'auto' if they did not say."
                ),
                "enum": ["auto", "file", "web"],
            },
        },
        "required": ["action"],
    },
    "handler": video_player,
}
