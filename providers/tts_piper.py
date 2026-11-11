"""
Piper — a neural voice that runs on this machine, in about fifty languages.

WHY THIS IS THE ONE THAT MATTERS FOR A PROJECT IN MANY LANGUAGES
    The other two local-mode voices each give up something this assistant
    cannot afford to give up.

    The system voice speaks only the languages the machine has voices installed
    for. Measured on an ordinary Windows 10 install: two, both English. A
    Turkish sentence came out as English phonetics; a Russian one came out as
    silence. That is a voice for one person's laptop, not for a project people
    use in their own languages.

    ElevenLabs speaks every language beautifully and needs an account, a key,
    and a working internet connection for every sentence.

    Piper needs a download the first time and then nothing at all. Measured
    here, Turkish, on the CPU:

        download        5.0s   (63 MB, once per voice)
        load            2.4s   (once per session)
        synthesis       0.34s  for 5.15s of speech  -  15x faster than real time

    Fifteen times real time on a CPU is the number that makes this the default
    rather than the fallback: the voice is never what you are waiting for.

IT FOLLOWS THE LANGUAGE BEING SPOKEN, WITHOUT GUESSING AT IT
    A Piper voice is trained for one language, so a multilingual assistant needs
    to know which to use. Detecting that from the text would mean a language
    detector - another dependency, and one that is unreliable on the short
    sentences an assistant actually says.

    It is already known. Whisper reports the language of what the USER said, as
    part of the recognition that has just happened, and the assistant answers in
    the language it was addressed in. So the engine passes that code down and
    this file picks a downloaded voice for it. No detection, no dependency, no
    guess - just information the pipeline was already holding and throwing away.

    Only voices already on disk are considered. Downloading 63 MB in the middle
    of a sentence would be a worse failure than answering in the wrong voice.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import threading
import time
from importlib.util import find_spec
from pathlib import Path
from typing import AsyncIterator, Optional

from . import audio
from .base import DEFAULT_CAPS, Field

def _voice_dir() -> Path:
    """Where downloaded voices live.

    Inside this folder by preference, so removing `providers/` removes the
    models with it and nothing is left behind in a cache directory nobody
    remembers.

    But not everybody installs a project into a folder they can write to. On a
    system-wide install — Program Files, /opt, a distro package, site-packages —
    the application directory is read-only, and a voice download would fail with
    a permission error that reads like the download itself was refused. So it
    falls back to the place each system keeps user data, which is writable by
    definition because it belongs to the person running the program.
    """
    here = Path(__file__).resolve().parent / "voices"
    try:
        here.mkdir(parents=True, exist_ok=True)
        probe = here / ".writable"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return here
    except Exception:
        pass

    import os
    import platform

    system = platform.system()
    if system == "Windows":
        root = Path(os.environ.get("LOCALAPPDATA")
                    or Path.home() / "AppData" / "Local")
    elif system == "Darwin":
        root = Path.home() / "Library" / "Application Support"
    else:
        root = Path(os.environ.get("XDG_DATA_HOME")
                    or Path.home() / ".local" / "share")
    fallback = root / "MarkAssistant" / "piper-voices"
    try:
        fallback.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return fallback


_VOICE_DIR = _voice_dir()

_CATALOG_URL = ("https://huggingface.co/rhasspy/piper-voices/resolve/main/"
                "voices.json?download=true")

# Piper builds most voices at 22.05 kHz; the speaker is open at 24 kHz. The
# model reports its own rate and that is what gets used - assuming 22050 would
# play a 16 kHz voice nine percent slow and sound like a worse model rather than
# a bug.
_catalog: Optional[dict] = None
_catalog_lock = threading.Lock()
_voices: dict = {}          # model path -> loaded PiperVoice
_voice_lock = threading.Lock()


# ─────────────────────────────────────────────────────────────────────────────
#  What exists, and what is already here
# ─────────────────────────────────────────────────────────────────────────────
def catalog(refresh: bool = False) -> dict:
    """Every voice Piper publishes, as {name: {language, quality, ...}}.

    One network read, cached for the session. Never blocks a caller that cannot
    wait: `installed()` and synthesis work entirely from disk."""
    global _catalog
    with _catalog_lock:
        if _catalog is not None and not refresh:
            return _catalog
    found: dict = {}
    try:
        import httpx
        # follow_redirects, because the catalog lives behind a HuggingFace
        # `resolve` URL that 302s to a CDN. httpx does NOT follow redirects by
        # default, so without this the request "succeeds" with a redirect body
        # and the voice list comes back empty — a hundred and fifty voices
        # silently reduced to nothing.
        with httpx.Client(timeout=20.0, follow_redirects=True) as cl:
            data = cl.get(_CATALOG_URL).json()
        for key, entry in (data or {}).items():
            lang = (entry.get("language") or {})
            found[key] = {
                "language": lang.get("code", "") or key.split("-")[0],
                "family": lang.get("family", ""),
                "name_native": lang.get("name_native", ""),
                "name_english": lang.get("name_english", ""),
                "quality": entry.get("quality", ""),
            }
    except Exception:
        found = {}
    with _catalog_lock:
        _catalog = found
    return found


def prefetch(on_ready=None) -> None:
    """Warm the catalog off the interface thread — it is one HTTP request, and
    the settings panel must never wait on a network call to draw a form."""
    if _catalog is not None:
        if on_ready:
            on_ready()
        return

    def work():
        catalog()
        if on_ready:
            try:
                on_ready()
            except Exception:
                pass

    threading.Thread(target=work, daemon=True).start()


def installed() -> list[str]:
    """Voices already on disk. Reads the folder, so a voice added by hand or
    left over from a previous run is found without anything being registered."""
    try:
        return sorted(p.stem for p in _VOICE_DIR.glob("*.onnx"))
    except Exception:
        return []


def language_of(voice: str) -> str:
    """"tr_TR-dfki-medium" -> "tr". The catalog is authoritative when it has
    been fetched; the name is enough when it has not, and it is right for every
    voice Piper publishes."""
    entry = (_catalog or {}).get(voice) or {}
    code = str(entry.get("language") or "")
    if code:
        return code.split("_")[0].lower()
    return voice.split("_")[0].lower() if voice else ""


def _use_certifi() -> bool:
    """Point urllib at certifi's certificates. False when there is nothing new to try.

    Piper's downloader uses urllib, and python.org's macOS Python has NO
    certificate store until its "Install Certificates.command" has been run — so
    every HTTPS download fails with CERTIFICATE_VERIFY_FAILED, while the voice
    list (fetched with httpx, which brings certifi) loads fine. Only applied
    after that failure, so a machine whose own store works is left alone."""
    try:
        import certifi
    except Exception:
        return False
    bundle = certifi.where()
    if os.environ.get("SSL_CERT_FILE") == bundle:
        return False
    os.environ["SSL_CERT_FILE"] = bundle
    return True


def download(voice: str, log=None) -> Path:
    """Fetch one voice. Blocking — callers run it off the interface thread.

    Into a scratch folder first, moved into place only once both files are
    complete. Piper writes straight to the final name, so a download cut short
    left a truncated .onnx that every later check took for an installed voice."""
    from piper.download_voices import download_voice

    _VOICE_DIR.mkdir(parents=True, exist_ok=True)
    target = _VOICE_DIR / f"{voice}.onnx"
    if target.exists():
        return target
    if log:
        log(f"SYS: downloading the {voice} voice (about 60 MB, once)")
    t0 = time.monotonic()
    partial = _VOICE_DIR / ".partial"
    shutil.rmtree(partial, ignore_errors=True)
    partial.mkdir(parents=True, exist_ok=True)
    try:
        try:
            download_voice(voice, partial)
        except Exception as e:
            if "CERTIFICATE_VERIFY_FAILED" not in str(e):
                raise
            if not _use_certifi():
                raise RuntimeError(
                    "this Python cannot check HTTPS certificates. On macOS run "
                    "'Install Certificates.command' from the Python folder in "
                    "Applications, or: pip install certifi") from e
            if log:
                log("SYS: this Python has no certificate store — using certifi's and retrying")
            download_voice(voice, partial)
        for part in partial.iterdir():
            os.replace(part, _VOICE_DIR / part.name)
    finally:
        shutil.rmtree(partial, ignore_errors=True)
    if log:
        log(f"SYS: {voice} downloaded in {time.monotonic() - t0:.1f}s")
    return target


# ─────────────────────────────────────────────────────────────────────────────
#  The provider
# ─────────────────────────────────────────────────────────────────────────────
class PiperTTS:
    def __init__(self, cfg: dict):
        self.voice = (cfg.get("voice") or "").strip()
        # Follow the language the user spoke, when a voice for it is on disk.
        self.follow = str(cfg.get("follow_language", "1")) not in ("", "0",
                                                                   "False",
                                                                   "false")
        self.speed = _num(cfg.get("speed"), 1.0)
        self.caps = dict(DEFAULT_CAPS)
        self.caps["audio_out"] = True
        # The voice that actually spoke last. With language-following the
        # configured voice and the one used can differ, and a timing report
        # naming the wrong one is a report that misleads.
        self.last_voice = ""
        self._log = cfg.get("log") or (lambda m: None)
        self._scan: list = []
        self._scan_at = 0.0

    # -- loading ------------------------------------------------------------
    def _load(self, voice: str):
        with _voice_lock:
            if voice in _voices:
                return _voices[voice]
        from piper import PiperVoice

        path = _VOICE_DIR / f"{voice}.onnx"
        if not path.exists():
            # Fetch it rather than refuse.
            #
            # This used to raise, telling the user to open the panel and press
            # TEST — which meant that choosing a voice and saving produced an
            # assistant with no voice at all, and an error naming a button
            # rather than doing the obvious thing. Choosing a voice IS the
            # request to use it.
            #
            # It still costs sixty megabytes, so `warmup()` does it at connect
            # time, before anybody has spoken. Reaching here means that did not
            # happen — a voice changed mid-session, or the language followed to
            # one that is not here yet — and a five-second wait is a better
            # answer than silence.
            self._log(f"SYS: fetching the {voice} voice (about 60 MB, once)")
            download(voice, self._log)
        loaded = PiperVoice.load(path)
        with _voice_lock:
            _voices[voice] = loaded
        return loaded

    def _pick(self, language: str) -> str:
        """Which voice speaks this turn.

        The chosen voice unless the user is speaking another language and a
        voice for it is already here. Downloading mid-sentence is deliberately
        not attempted: sixty megabytes of silence is a worse answer than the
        wrong accent."""
        if not (self.follow and language):
            return self.voice
        code = language.split("-")[0].split("_")[0].lower()
        if self.voice and language_of(self.voice) == code:
            return self.voice
        # `installed()` reads the folder, and this runs on every sentence.
        # Cached for a few seconds: a voice appearing mid-conversation is
        # possible (a download just finished) but not something to pay a disk
        # scan per utterance to notice.
        now = time.monotonic()
        if now - self._scan_at > 5.0:
            self._scan, self._scan_at = installed(), now
        for name in self._scan:
            if language_of(name) == code:
                return name
        return self.voice

    async def warmup(self, log=None) -> None:
        """Download and load the voice before anybody speaks.

        TWO PROBLEMS, ONE FIX.

        The download: sixty megabytes fetched on the first sentence is sixty
        megabytes of silence in the middle of a conversation. Worse, before this
        it was not fetched at all — the voice simply failed, with an error
        telling the user to press a button.

        The load: `PiperVoice.load` brings up an ONNX Runtime session, measured
        here at 2.4 seconds. It runs in a worker thread, but ONNX's session
        construction is C++ that holds the GIL for long stretches, so the event
        loop and the Qt bridge stall with it — which is why typing a message
        just after switching to Piper felt like the interface had caught.

        Both are one-off costs. This moves them to the moment the session
        connects, where nobody is waiting.
        """
        if log:
            self._log = log
        if not self.voice:
            return
        try:
            t0 = time.monotonic()
            await asyncio.to_thread(download, self.voice, self._log)
            model = await asyncio.to_thread(self._load, self.voice)
            rate = int(getattr(model.config, "sample_rate", 22050))
            # One tiny synthesis: the first call also builds ONNX's internal
            # buffers, and paying that here is the whole point of a warm-up.
            await asyncio.to_thread(self._synth, model, "ok")
            self._log(f"SYS: voice ready in {time.monotonic() - t0:.1f}s "
                      f"({self.voice}, {rate} Hz)")
        except Exception as e:
            self._log(f"SYS: voice not ready - {type(e).__name__}: {str(e)[:100]}")

    # -- the contract -------------------------------------------------------
    async def speak(self, text: str, voice: str = "",
                    language: str = "") -> AsyncIterator[bytes]:
        """24 kHz mono PCM, a sentence at a time.

        Sentence by sentence so speech starts before the whole answer has been
        synthesised — at fifteen times real time the difference is small, but it
        is free, and it is what makes an interrupt land quickly."""
        chosen = voice or self._pick(language)
        if not chosen:
            raise RuntimeError("no Piper voice is chosen")
        model = await asyncio.to_thread(self._load, chosen)
        self.last_voice = chosen
        rate = int(getattr(model.config, "sample_rate", 22050))

        for sentence in _split(text):
            pcm = await asyncio.to_thread(self._synth, model, sentence)
            for piece in audio.chunk(audio.resample(pcm, rate,
                                                    audio.SPEAKER_RATE)):
                yield piece

    def _synth(self, model, sentence: str) -> bytes:
        from piper import SynthesisConfig

        cfg = None
        if self.speed and abs(self.speed - 1.0) > 0.01:
            try:
                # Piper's "length scale" is the inverse of speed: a longer
                # sentence takes more time, so 2x speed is 0.5 length.
                cfg = SynthesisConfig(length_scale=1.0 / self.speed)
            except Exception:
                cfg = None
        out = []
        for chunk in (model.synthesize(sentence, cfg) if cfg
                      else model.synthesize(sentence)):
            out.append(chunk.audio_int16_bytes)
        return b"".join(out)

    # -- for the panel ------------------------------------------------------
    def voices(self) -> list[tuple[str, str]]:
        """(name, label) for the dropdown — everything Piper publishes, with
        what is already downloaded marked, ordered so a language somebody is
        looking for is findable."""
        cat = catalog()
        here = set(installed())
        rows = []
        for name in sorted(cat or {k: {} for k in here}):
            meta = (cat or {}).get(name, {})
            native = meta.get("name_native") or meta.get("name_english") or ""
            mark = "* " if name in here else "  "
            rows.append((name, f"{mark}{name}"
                               f"{'   (' + native + ')' if native else ''}"))
        for name in sorted(here):
            if not any(n == name for n, _ in rows):
                rows.append((name, f"* {name}"))
        return rows

    def probe(self) -> tuple[bool, str]:
        if find_spec("piper") is None:
            return False, "Piper is not installed."
        if not self.voice:
            n = len(catalog())
            return False, (f"Choose a voice - {n} available in about "
                           f"{len({language_of(v) for v in catalog()})} languages."
                           if n else "Choose a voice.")
        try:
            path = download(self.voice)
            if not path.exists():
                return False, f"Could not download {self.voice}."
            t0 = time.monotonic()
            model = self._load(self.voice)
            pcm = self._synth(model, "Test.")
            took = time.monotonic() - t0
        except Exception as e:
            return False, f"{type(e).__name__}: {str(e)[:120]}"
        rate = int(getattr(model.config, "sample_rate", 22050))
        secs = len(pcm) / float(rate * 2)
        extra = ""
        if self.follow:
            others = [v for v in installed() if v != self.voice]
            extra = (f"  |  also here: {', '.join(sorted(language_of(v) for v in others))}"
                     if others else "  |  download more voices to follow other languages")
        return True, (f"Ready in {took:.1f}s, {secs:.1f}s of test audio, "
                      f"{rate} Hz{extra}")


def _num(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _split(text: str, max_chars: int = 300) -> list[str]:
    """Speakable pieces. Punctuation only — the same two rules as the system
    voice: a space is required after `.!?` so version numbers survive, and not
    after the CJK, Arabic and Devanagari stops, which are not written with one."""
    from .tts_system import _split as _shared
    return _shared(text, max_chars)


def prepare(values: dict, report=None) -> None:
    """Fetch a voice as soon as it is chosen, not when it is first needed.

    The settings panel calls this on a background thread the moment somebody
    picks a voice. Before it, choosing a voice and pressing SAVE produced an
    assistant with no voice at all until either TEST was pressed or the next
    connect warmed it up — and the panel gave no sign that anything was still
    missing.

    Optional by design: the panel looks for this function and skips providers
    that do not have one, so nothing else has to know that voices are
    downloadable."""
    voice = str((values or {}).get("voice") or "").strip()
    if not voice:
        return
    if (_VOICE_DIR / f"{voice}.onnx").exists():
        if report:
            report(True, f"{voice} is ready.")
        return
    try:
        if report:
            report(None, f"downloading {voice} (about 60 MB, once)...")
        t0 = time.monotonic()
        download(voice)
        if report:
            report(True, f"{voice} downloaded in {time.monotonic() - t0:.1f}s.")
    except Exception as e:
        if report:
            report(False, f"{type(e).__name__}: {str(e)[:120]}")


def _make(cfg: dict) -> PiperTTS:
    return PiperTTS(cfg)


PROVIDER = {
    "id": "piper",
    "kind": "tts",
    "label": "Piper",
    "hint": "A neural voice on this computer, in about 50 languages",
    "requires": ("piper",),
    "install": "piper-tts",
    "catalog": (
        {"id": "piper", "label": "Piper (on this computer)",
         "note": "free - private - ~50 languages - downloads once",
         "local": True, "values": {}},
    ),
    "needs": (
        Field("voice", "Voice", kind="choice", choices=(),
              hint="A star means it is already downloaded; TEST fetches the rest"),
        Field("follow_language", "Follow the language you speak", kind="bool",
              default="1", required=False,
              hint="Uses a downloaded voice for whatever language Whisper heard"),
        Field("speed", "Speed", kind="number", default=1.0, required=False,
              advanced=True, hint="1.0 is normal"),
    ),
    "caps": {"audio_out": True},
    "factory": _make,
}


def available() -> bool:
    return find_spec("piper") is not None


def choices_for(field_key: str) -> tuple:
    """The voice list for the panel. Downloaded ones first so the useful
    entries are at the top rather than buried in a hundred and fifty."""
    if field_key != "voice":
        return ()
    here = installed()
    cat = catalog()
    if not cat:
        return tuple(here)
    rest = [v for v in sorted(cat) if v not in here]
    return tuple(here) + tuple(rest)
