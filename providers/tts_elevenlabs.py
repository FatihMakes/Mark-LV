"""
ElevenLabs speech synthesis — the multilingual voice.

WHY IT IS WORTH A FILE OF ITS OWN
    The system voice speaks the languages the machine has voices for, which on
    an ordinary install means one. This assistant is used in many, and people
    switch language mid-conversation. One ElevenLabs voice speaks all of them,
    with the same voice, without anything being installed or downloaded — which
    is the thing neither of the local options can do.

    It costs a key and an internet connection. That is the trade, and it is why
    this is a choice rather than the default.

THE FORMAT NEGOTIATION, WHICH IS NOT OPTIONAL
    Asking for `pcm_24000` gives audio at exactly the rate the speaker is
    already open at — no decoding, no resampling, first sample straight to the
    queue. But raw PCM output is a paid-tier feature, and a free key is refused
    with a 422 that names the tier.

    Rejecting it there would mean this provider works for some accounts and
    returns an error for others, which is not a useful thing to ship. So the PCM
    request is tried once, and an account that cannot have it drops to MP3 and
    decodes. The decoder is PyAV, which arrives with faster-whisper; when
    neither is present the failure says exactly which of the two things to do
    about it rather than reporting a format error nobody can act on.

NO LANGUAGE PARAMETER IS SENT
    The multilingual models detect the language from the text. Passing one would
    mean this file deciding what language the assistant is speaking, which is a
    decision it has no business making — the model and the system prompt own it.
"""
from __future__ import annotations

import asyncio
import json
import re
from importlib.util import find_spec
from typing import AsyncIterator

from . import audio
from .base import DEFAULT_CAPS, Field

API = "https://api.elevenlabs.io/v1"

# Latency first. This is a conversation: a voice that arrives half a second
# sooner is worth more here than one that is slightly richer, and the flash
# models are multilingual too. The higher-quality models stay available for
# anybody who would rather have them.
MODELS = (
    "eleven_flash_v2_5",        # fastest, multilingual
    "eleven_turbo_v2_5",        # a little richer, still quick
    "eleven_multilingual_v2",   # best quality, slowest
)
DEFAULT_MODEL = MODELS[0]

# Asked for in this order. The first is free of any conversion; the second needs
# decoding but is available on every account.
_PCM = "pcm_24000"
_MP3 = "mp3_44100_128"

# The shape of a voice ID: one run of letters and digits. A voice NAME almost
# never looks like that — it has spaces, punctuation, or is far shorter.
_VOICE_ID = re.compile(r"[A-Za-z0-9]{16,40}")


def _match_voice(wanted: str, voices: list[tuple[str, str]]) -> str:
    """The ID for `wanted`, which may already be an ID or may be a voice's name.

    Earlier panels saved the name of a listed voice instead of its ID, and the
    API answered every sentence with "invalid ID". Matching the name against the
    account's own voices repairs such a setting without the user doing anything.
    Empty string when nothing on the account matches.
    """
    wanted = (wanted or "").strip()
    if not wanted:
        return ""
    for voice_id, name in voices:
        if voice_id == wanted:
            return voice_id
    folded = wanted.casefold()
    for voice_id, name in voices:
        if str(name).strip().casefold() == folded:
            return voice_id
    return ""


class ElevenLabsTTS:
    def __init__(self, cfg: dict):
        self.key = (cfg.get("api_key") or "").strip()
        self.voice_id = (cfg.get("voice_id") or "").strip()
        self.model = (cfg.get("model") or DEFAULT_MODEL).strip()
        self.stability = _num(cfg.get("stability"), 0.5)
        self.similarity = _num(cfg.get("similarity"), 0.75)
        self.caps = dict(DEFAULT_CAPS)
        self.caps["audio_out"] = True
        self.base = API
        # None until the first request has learned what this account may have.
        self._format: str | None = None
        # The voice value last looked up by name, so a lookup happens once.
        self._resolved_from = ""

    def _resolve_voice(self) -> None:
        """Turn a saved voice NAME into its ID, once. Never raises.

        Only values that do not look like an ID are looked up, so a correctly
        configured voice costs nothing extra.
        """
        wanted = self.voice_id
        if (not wanted or not self.key or _VOICE_ID.fullmatch(wanted)
                or wanted == self._resolved_from):
            return
        self._resolved_from = wanted
        try:
            found = _match_voice(wanted, self.voices())
        except Exception:
            return
        if found:
            self.voice_id = found
            self._resolved_from = found

    # ── requests ─────────────────────────────────────────────────────────────
    def _headers(self) -> dict:
        return {"xi-api-key": self.key, "Content-Type": "application/json"}

    def _payload(self, text: str) -> dict:
        return {
            "text": text,
            "model_id": self.model,
            # No `language_code`: the model detects it. See the module docstring.
            "voice_settings": {"stability": self.stability,
                               "similarity_boost": self.similarity},
        }

    async def _stream_once(self, cl, text: str, fmt: str) -> AsyncIterator[bytes]:
        url = f"{API}/text-to-speech/{self.voice_id}/stream"
        async with cl.stream("POST", url, headers=self._headers(),
                             params={"output_format": fmt},
                             json=self._payload(text)) as r:
            if r.status_code >= 400:
                body = (await r.aread()).decode("utf-8", "replace")
                raise _HttpError(r.status_code, body)
            async for piece in r.aiter_bytes():
                if piece:
                    yield piece

    async def speak(self, text: str, voice: str = "") -> AsyncIterator[bytes]:
        import httpx

        if voice:
            self.voice_id = voice
        if not self.key:
            raise RuntimeError("no ElevenLabs API key is set")
        if not self.voice_id:
            raise RuntimeError("no ElevenLabs voice is chosen")
        await asyncio.to_thread(self._resolve_voice)
        text = (text or "").strip()
        if not text:
            return

        async with httpx.AsyncClient(timeout=60.0) as cl:
            fmt = self._format or _PCM
            try:
                async for piece in self._emit(cl, text, fmt):
                    yield piece
                self._format = fmt
                return
            except _HttpError as e:
                # 422 on the PCM request is the tier refusing the format, not a
                # bad voice or a bad key. Learn it once and never ask again.
                if fmt == _PCM and e.status in (400, 401, 422):
                    self._format = _MP3
                else:
                    raise RuntimeError(f"{e.status} {_message(e.body)}") from None

            async for piece in self._emit(cl, text, _MP3):
                yield piece

    async def _emit(self, cl, text: str, fmt: str) -> AsyncIterator[bytes]:
        if fmt == _PCM:
            # Already 24 kHz mono 16-bit — exactly what the speaker wants, so it
            # goes straight through, sliced only for the interrupt granularity.
            buf = b""
            async for piece in self._stream_once(cl, text, fmt):
                buf += piece
                while len(buf) >= 2400:
                    yield buf[:2400]
                    buf = buf[2400:]
            if buf:
                yield buf
            return

        mp3 = b""
        async for piece in self._stream_once(cl, text, fmt):
            mp3 += piece
        pcm = await asyncio.to_thread(_decode_mp3, mp3)
        for piece in audio.chunk(pcm):
            yield piece

    # ── for the panel ────────────────────────────────────────────────────────
    def voices(self) -> list[tuple[str, str]]:
        """(id, name) for every voice on the account."""
        import httpx
        with httpx.Client(timeout=15.0) as cl:
            r = cl.get(f"{API}/voices", headers={"xi-api-key": self.key})
            if r.status_code >= 400:
                raise RuntimeError(f"{r.status_code} {_message(r.text)}")
            return [(v.get("voice_id", ""), v.get("name", ""))
                    for v in (r.json().get("voices") or [])]

    def probe(self) -> tuple[bool, str]:
        if not self.key:
            return False, "No API key set."
        try:
            vs = self.voices()
        except Exception as e:
            msg = str(e)
            if "401" in msg:
                return False, "The key was refused."
            return False, msg[:120]
        if not self.voice_id:
            names = ", ".join(n for _, n in vs[:4])
            return False, f"Key works. Choose a voice — {len(vs)} available: {names}"
        found = _match_voice(self.voice_id, vs)
        if not found and not _VOICE_ID.fullmatch(self.voice_id):
            return False, (f"No voice called {self.voice_id!r} on this account. "
                           f"Pick one from the list.")
        # An ID that is not on the account's own list is left alone, as before:
        # a voice can be usable without being listed there.
        self.voice_id = found or self.voice_id
        name = next((n for i, n in vs if i == self.voice_id), self.voice_id)
        note = "" if find_spec("av") else \
            "  (MP3 fallback would need PyAV — only matters on a free tier)"
        return True, f"Ready: {name}, {self.model}{note}"


# ─────────────────────────────────────────────────────────────────────────────
class _HttpError(Exception):
    def __init__(self, status: int, body: str):
        super().__init__(f"{status}")
        self.status, self.body = status, body


def _num(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _message(body: str) -> str:
    """The sentence out of an ElevenLabs error, which nests it two deep."""
    try:
        obj = json.loads(body)
        detail = obj.get("detail", obj)
        if isinstance(detail, dict):
            return str(detail.get("message") or detail.get("status") or detail)
        return str(detail)
    except Exception:
        return " ".join((body or "").split())[:160]


def _decode_mp3(data: bytes) -> bytes:
    """MP3 -> 24 kHz mono 16-bit PCM, using PyAV.

    PyAV is a dependency of faster-whisper, so anybody running local speech
    recognition already has it. When it is missing the message says what to
    install AND what the alternative is, because "no decoder" on its own tells
    somebody nothing about why their voice stopped working.
    """
    if find_spec("av") is None:
        raise RuntimeError(
            "this account cannot receive raw PCM, so the audio arrives as MP3 "
            "and needs a decoder: pip install av — or use a paid ElevenLabs "
            "tier, or the system voice")
    import io

    import av
    import numpy as np

    out = []
    with av.open(io.BytesIO(data)) as container:
        resampler = av.AudioResampler(format="s16", layout="mono",
                                      rate=audio.SPEAKER_RATE)
        for frame in container.decode(audio=0):
            for res in resampler.resample(frame):
                out.append(res.to_ndarray().reshape(-1))
        for res in resampler.resample(None):        # flush
            out.append(res.to_ndarray().reshape(-1))
    if not out:
        return b""
    return np.concatenate(out).astype("<i2").tobytes()


def _make(cfg: dict) -> ElevenLabsTTS:
    return ElevenLabsTTS(cfg)


PROVIDER = {
    "id": "elevenlabs",
    "kind": "tts",
    "label": "ElevenLabs",
    "hint": "One voice that speaks every language — needs a key and a connection",
    "requires": ("httpx",),
    "catalog": (
        {"id": "elevenlabs", "label": "ElevenLabs",
         "note": "cloud · one voice speaks every language", "local": False,
         "values": {}},
    ),
    "needs": (
        Field("api_key", "API key", kind="secret",
              hint="elevenlabs.io -> Profile -> API key"),
        Field("voice_id", "Voice", kind="choice", choices=(),
              hint="Press LIST to read the voices on your account"),
        Field("model", "Model", kind="choice", choices=MODELS,
              default=DEFAULT_MODEL, required=False,
              hint="flash is the quickest and still multilingual"),
        Field("stability", "Stability", kind="number", default=0.5,
              required=False, advanced=True, hint="0 to 1"),
        Field("similarity", "Similarity", kind="number", default=0.75,
              required=False, advanced=True, hint="0 to 1"),
    ),
    "caps": {"audio_out": True},
    "factory": _make,
}


def available() -> bool:
    return find_spec("httpx") is not None
