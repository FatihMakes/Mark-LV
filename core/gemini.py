"""
One place where the assistant's one-shot Gemini calls are made.

WHY THIS EXISTS
    The live conversation runs on the Live API and is not what this file is
    about. Everything else — reading a screenshot, parsing a flight page,
    turning a request into a shell command, working out which WhatsApp button
    answers a call — was a separate `genai.Client(...)` built at the point of
    use, with the model name written inline. Sixteen files did it, twenty-six
    times, and not one of them set a timeout.

    That is not tidiness, it is three real faults:

    NO TIMEOUT.  The SDK waits forever by default. `gemini-flash-latest` spent
    an afternoon returning 504 DEADLINE_EXCEEDED, and every one of those calls
    became an unbounded hang — measured at ten seconds of silence while a phone
    rang, and worse elsewhere, because nothing was there to give up.

    NO FALLBACK.  One hardcoded alias meant that when that alias was unwell,
    the feature was simply gone. A ladder costs nothing when the first rung
    works and saves the feature when it does not.

    NO SINGLE PLACE TO CHANGE.  A new model release meant editing sixteen files
    and hoping none were missed.

THE LIVE MODEL DOES THIS WORK, AND IT LEADS THE LADDER
    Not the user's conversation — a separate, throwaway session per call, so
    nothing a plugin asks is ever heard by the person at the microphone.

    It leads because of quota. This is a voice assistant; the Live API is the
    dependency it already has, and it draws on a different pool from the text
    models. On the free tier it is the TEXT pool that runs out, and when it does
    every side call fails and the feature behind it dies with it. Putting Live
    first means ordinary use stops spending the pool that runs dry.

    The reply arrives through output_transcription, because these models refuse
    response_modalities=["TEXT"] with a 1007 — they only speak. That sounds
    fatal for structured output and is not: the transcription is the model's own
    text of what it said, and it returned "Mum ❤ click here for contact info",
    indented Python inside markdown fences, and src/utils/helpers_v2.py
    character for character.

    It cannot carry grounding metadata, so grounded web search stays on REST.

THE LADDER, MEASURED
    Live, one throwaway session:
        connect                     0.24s
        short structured JSON       1.7 - 2.8s
        2300 characters of code     3.39s, not truncated
        three concurrent sessions   all fine, 4.77s wall clock
    REST text models, same prompt, same afternoon:
        gemini-2.5-flash-lite       0.76s   ...then 429, quota exhausted
        gemini-2.5-flash            0.80s   ...then 429
        gemini-flash-lite-latest    2.58s
        gemini-flash-latest         504, every time
    So REST is two to four times quicker while it lasts, and the whole point of
    the ladder is that it does not last. Pinned REST names sit behind Live;
    rolling `-latest` aliases sit behind those, because they were the ones
    having a bad day.

    Where the answer depends only on stable input, cache it and neither pool is
    touched twice: `plugins/_whatsapp_core.py` is the worked example — one
    request per WhatsApp language for the lifetime of the install.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
import threading
from pathlib import Path
from typing import Optional

if getattr(sys, "frozen", False):
    _BASE = Path(sys.executable).parent
else:
    _BASE = Path(__file__).resolve().parent.parent

_KEY_FILE = _BASE / "config" / "api_keys.json"

# Ladders, tried left to right. Change a model HERE and the whole app follows.
FAST = "fast"      # short classification, extraction, one-line decisions
SMART = "smart"    # reasoning, generation, long documents, images
SEARCH = "search"  # grounded search — REST only, see below

# A rung that means "ask the Live model instead", through a short throwaway
# session rather than the REST text API.
#
# WHY IT LEADS
#     This is a voice assistant: the Live API is the dependency it already has,
#     and it draws on a DIFFERENT quota pool from the text models. On the free
#     tier the text pool is the one that runs out — an afternoon of ordinary use
#     exhausts it, and when it does, every one of these side calls fails and the
#     feature behind it dies. The Live pool is untouched by that.
#
# WHAT IT COSTS, MEASURED
#     connect                      0.24s
#     short structured JSON        1.7 - 2.8s   (REST: 0.76s)
#     2300 characters of code      3.39s, not truncated
#     three concurrent sessions    all fine, 4.77s wall clock
#     So it is two to four times slower than REST when REST is available, and
#     infinitely faster than REST when REST is out of quota.
#
# THE THING WORTH KNOWING
#     These models only speak — response_modalities=["TEXT"] is refused with a
#     1007. The reply comes back through output_transcription, which sounds like
#     it would mangle anything structured. It does not: it is the model's own
#     text of what it said, and it survived "Mum ❤ click here for contact info",
#     indented Python inside markdown fences, and src/utils/helpers_v2.py
#     character for character. That is what makes this usable at all.
#
#     What it cannot carry is grounding metadata, so grounded web search stays
#     on REST — see SEARCH.
#
#     What it cannot ACCEPT is recorded audio. A Live session takes sound through
#     realtimeInput, frame by frame, as it is spoken; a finished audio file
#     handed to it as client content comes back as
#     "1008 Operation is not implemented, or supported, or enabled". That is a
#     capability boundary rather than an outage, so it is not something the
#     cooldown should learn — resting this rung for six hours because one caller
#     sent a sound file would take the quota-preserving lead rung away from every
#     text call in the app. `call()` simply does not offer it audio. See
#     `_carries_recording`.
LIVE = "live"

# A rung that means "ask a provider from providers/ instead" — a local model, or
# another vendor's API. The id after the prefix is whatever the settings panel
# stored, so the cooldown machinery below treats it exactly like a model name
# and rests it on quota, absence or silence without knowing what it is.
#
# WHY THIS IS WORTH HAVING EVEN FOR PEOPLE WHO LIKE GEMINI
#     Gemini's Live quota and its text quota are separate pools, and on the free
#     tier it is the TEXT pool that runs out first. When it does, the
#     conversation carries on while document reading, screen reasoning, code
#     generation and every other one-shot call in the app stops working. Pointing
#     only those at a second model — without touching the voice, the latency of
#     the conversation, or anything the user hears — is the cheapest useful thing
#     the providers folder does.
PROVIDER_PREFIX = "prov:"

# Every model this key can reach, in the order to try them. A rung that runs
# out of quota is skipped for a while and the next one answers — which is the
# whole point: one model a day means one daily limit, a ladder means the sum of
# them. Names verified against the live model list rather than guessed.
# Order is measured, not assumed. Timed against this key, same prompt:
#     gemini-3.5-flash-lite      0.56s      gemini-2.5-flash        0.67s
#     gemini-3.1-flash-lite      0.60s      gemini-2.5-flash-lite   0.74s
#     gemini-flash-lite-latest   0.60s      gemini-3.5-flash        1.13s
#     gemini-3-flash-preview     504, after 14.7s of waiting
#     gemini-3.6-flash           504, after 12.0s
#     gemini-flash-latest        503 UNAVAILABLE
# The three that fail sit at the BOTTOM rather than being deleted: they are
# real quota when they are healthy, and a rung that is down is set aside by the
# cooldown after one attempt instead of being paid for on every call.
_LADDERS = {
    FAST: (LIVE,
           # Cheapest/lightest stable endpoints first; keep the existing rungs.
           "gemini-3.5-flash-lite", "gemini-3.1-flash-lite",
           "gemini-2.5-flash-lite", "gemini-flash-lite-latest",
           "gemini-3.6-flash", "gemini-3.5-flash", "gemini-2.5-flash",
           "gemini-3.7-flash", "gemini-3-flash-preview", "gemini-flash-latest",
           # Expensive/high-capability model deliberately last.
           "gemini-3.8-flash"),
    SMART: (LIVE,
            # Try lightweight models first; retain every previous SMART rung.
            "gemini-3.5-flash-lite", "gemini-3.1-flash-lite",
            "gemini-2.5-flash-lite", "gemini-flash-lite-latest",
            "gemini-3.6-flash", "gemini-3.5-flash", "gemini-2.5-flash",
            "gemini-3.7-flash", "gemini-3-flash-preview", "gemini-flash-latest",
            "gemini-3.8-flash"),
    # Grounded search needs response.candidates[...].grounding_metadata, which a
    # Live turn does not produce. REST only, and it says so rather than silently
    # returning an answer with no sources behind it. Keep all existing search
    # rungs, but prefer Flash-Lite; the 3.8 Flash endpoint is the final fallback.
    SEARCH: ("gemini-3.5-flash-lite", "gemini-3.1-flash-lite",
             "gemini-2.5-flash-lite", "gemini-2.5-flash", "gemini-3.5-flash",
             "gemini-3.6-flash", "gemini-3.7-flash", "gemini-flash-latest",
             "gemini-3.8-flash"),
}

# The conversation's own model, and ONE careful fallback behind it.
#
# Deliberately not a long ladder. The Live quota is not the one that runs out —
# the text models are — and Live models are not interchangeable the way text
# models are: they differ in which config fields they accept and in what they
# can do, so falling through a list of them risks connecting to something that
# behaves like a different assistant. The key also offers transcribe-live,
# live-translate and a robotics streaming model, none of which are assistants
# at all.
#
# So there are two: the current one, and the native-audio model this assistant
# used before it, which is known to work here. A mismatch in config is already
# survivable — the connect loop drops the tuning and proactive-audio fields and
# reconnects when the server rejects them.
LIVE_MODELS = (
    "models/gemini-3.1-flash-live-preview",
    "models/gemini-2.5-flash-native-audio-preview-12-2025",
)

# The Live model to use for one-shot calls. main.py owns the real one; this is
# only the fallback for when this module is imported without it (tests).
_LIVE_FALLBACK = "models/gemini-3.1-flash-live-preview"

# How many one-shot Live sessions may exist at once.
#
# THE USER'S CONVERSATION OUTRANKS EVERY SIDE CALL.
# Nothing here can reach the microphone or the speaker — main.py has exactly one
# `.receive()` and it is bound to its own session, and audio only reaches the
# speaker through that one loop — so a side call cannot answer the user or talk
# over the reply. Verified alongside a live main session: four side calls fired
# while it was connected, each got its own answer, and the main session replied
# correctly both before and after with no errors.
#
# What a side call CAN do is take up a concurrent-session slot. That is the one
# way it could hurt the conversation, so it is capped, and a call that cannot
# get a slot quickly does not queue behind the others — it falls to the REST
# rung, which is what the ladder is for.
# Three, from the measurement: four side calls plus the conversation ran
# together without complaint, so three leaves the conversation a slot in hand
# while still covering any burst this app actually produces — tool calls run one
# after another, and the screen agent's loop is sequential.
_LIVE_SLOTS = threading.BoundedSemaphore(3)
_LIVE_SLOT_WAIT = 3.0

_ONE_SHOT_SYSTEM = (
    "You are a data-processing function, not an assistant and not in a "
    "conversation. There is no person listening to you. Produce exactly the "
    "output the request asks for and nothing else: no greeting, no "
    "acknowledgement, no 'Understood', no explanation, no closing remark, no "
    "restating of the question. If the request asks for JSON, emit only the "
    "JSON. If it asks for code, emit only the code. If it asks for one word, "
    "emit that one word. Preserve the exact spelling, punctuation, capitals "
    "and whitespace of anything you are asked to copy or return."
)

# Milliseconds. Not a preference: the API rejects anything under ten seconds
# with "Minimum allowed deadline is 10s", so this is the tightest bound it will
# accept. Callers with a long job (a whole document, a big image) pass more.
DEFAULT_TIMEOUT_MS = 10_000
MIN_TIMEOUT_MS = 10_000

_key_lock = threading.Lock()
_cached_key: str | None = None

# A rung that answered 429 is out of quota, and on the free tier it will stay
# that way for a while. Retrying it on every single call is a wasted round trip
# in front of every request the assistant makes — measured on this key, the
# lite rung was 429ing continuously, so every call was paying for it before
# reaching the model that could actually answer. Remembering that for a few
# minutes turns the ladder from a cost into a saving.
_COOLDOWN_SECONDS = 300
# A model that does not exist, or that this key may not use, is not coming back
# in five minutes. Retrying it on every call is a wasted round trip in front of
# everything the assistant does, so it is set aside for the session instead.
_GONE_SECONDS = 6 * 60 * 60
# Overloaded or timing out. Usually passes, so a shorter rest than "gone" —
# but long enough that the fourteen-second wait is paid once, not repeatedly.
_UNAVAILABLE_SECONDS = 30 * 60
_cooldown: dict[str, float] = {}
_cool_lock = threading.Lock()


def _cool(model: str, seconds: float = _COOLDOWN_SECONDS) -> None:
    with _cool_lock:
        _cooldown[model] = time.monotonic() + seconds


def is_quota_error(err: str) -> bool:
    return "429" in err or "RESOURCE_EXHAUSTED" in err


def is_unavailable_error(err: str) -> bool:
    """The model is up but not answering — overloaded, or a deadline expired.

    Worth its own case because of what it costs: a 504 from one of these took
    fourteen seconds to arrive. Retrying that on every call puts the wait in
    front of everything the assistant does, so a rung that times out is rested
    like an exhausted one — for less long, since it is usually passing.
    """
    low = err.lower()
    return ("503" in err or "504" in err
            or "unavailable" in low or "deadline_exceeded" in low)


def is_gone_error(err: str) -> bool:
    """The model is not there, or not ours to use — a different thing from busy."""
    low = err.lower()
    return ("404" in err or "not found" in low or "is not supported" in low
            or "permission" in low or "403" in err)


def live_model() -> str:
    """The Live model to open the conversation with: the first rung that is not
    resting. If every one is resting the ladder is used from the top anyway —
    refusing to connect at all is never the better answer."""
    for m in LIVE_MODELS:
        if not _cooling(m):
            return m
    return LIVE_MODELS[0]


def note_live_failure(model: str, err: str) -> bool:
    """Record why a Live model failed. True when it is worth trying the next.

    Only quota and availability move the ladder along. A bad API key or a
    dropped network is not the model's fault, and stepping down the ladder for
    those would work through every model and reach the same wall four times.
    """
    if is_quota_error(err):
        _cool(model, _COOLDOWN_SECONDS)
        print(f"[Gemini] Live model {model} is out of quota — "
              f"switching for {_COOLDOWN_SECONDS // 60} minutes.")
        return True
    if is_gone_error(err):
        _cool(model, _GONE_SECONDS)
        print(f"[Gemini] Live model {model} is unavailable to this key — "
              f"setting it aside.")
        return True
    return False


def _cooling(model: str) -> bool:
    with _cool_lock:
        until = _cooldown.get(model, 0.0)
        if until and time.monotonic() < until:
            return True
        _cooldown.pop(model, None)
        return False


def api_key(refresh: bool = False) -> str:
    """The Gemini key from config/api_keys.json. Cached; never raises."""
    global _cached_key
    with _key_lock:
        if _cached_key is not None and not refresh:
            return _cached_key
        try:
            data = json.loads(_KEY_FILE.read_text(encoding="utf-8"))
            _cached_key = str(data.get("gemini_api_key") or "")
        except Exception:
            _cached_key = ""
        return _cached_key


def client(timeout_ms: int = DEFAULT_TIMEOUT_MS, key: str = ""):
    """A configured genai.Client with a deadline on it. Raises if there is no
    key, because a caller that cannot work without one should say so."""
    from google import genai
    from google.genai import types as gtypes

    key = key or api_key()
    if not key:
        raise RuntimeError("no Gemini API key is configured")
    return genai.Client(
        api_key=key,
        http_options=gtypes.HttpOptions(timeout=max(MIN_TIMEOUT_MS, int(timeout_ms))),
    )


class _Reply:
    """What a Live turn hands back, shaped like the REST response's `.text` so
    every existing call site keeps working unchanged."""

    __slots__ = ("text",)

    def __init__(self, text: str):
        self.text = text


def _live_model() -> str:
    """Whatever main.py is running, so upgrading the assistant upgrades this."""
    return getattr(sys.modules.get("main"), "LIVE_MODEL", None) or _LIVE_FALLBACK


def _to_live_parts(contents) -> list:
    """REST `contents` -> Live `parts`. Accepts a bare string, a list of
    strings, and the SDK's Part objects (which is how every image is passed
    here), because those are the three shapes the call sites actually use."""
    import base64

    items = contents if isinstance(contents, (list, tuple)) else [contents]
    parts = []
    for item in items:
        if isinstance(item, str):
            parts.append({"text": item})
            continue
        blob = getattr(item, "inline_data", None)
        if blob is not None:
            data = getattr(blob, "data", None)
            mime = getattr(blob, "mime_type", None) or "application/octet-stream"
            if isinstance(data, bytes):
                data = base64.b64encode(data).decode("ascii")
            parts.append({"inline_data": {"mime_type": mime, "data": data}})
            continue
        txt = getattr(item, "text", None)
        if txt:
            parts.append({"text": txt})
            continue
        if isinstance(item, dict):
            parts.append(item)
    return parts


def _carries_recording(contents) -> bool:
    """True when the request holds a sound or video file rather than text or a
    still image.

    Two rungs of the ladder cannot take one, for reasons that are permanent
    rather than temporary, so neither should be asked:

      LIVE     answers a recording with 1008 "not implemented, or supported, or
               enabled" — it wants audio streamed through realtimeInput, not a
               finished file in a turn. Measured: about 1.5s spent connecting a
               session that was always going to refuse, paid on EVERY such call,
               because the message matches none of the cooldown patterns and
               therefore nothing is ever learned from it.

      prov:    `providers.llm_openai.parts_to_openai` turns every inline_data
               payload into an `image_url` data URI, which is right for the
               images this app actually sends and wrong for a sound file. The
               endpoint would reject it, or — worse — accept it and describe
               nothing.

    Still images are deliberately NOT included: they are what the vision path
    sends, both rungs handle them, and excluding them would undo the feature
    this ladder was built around.

    Costs nothing on the calls that already exist. Every one of them passes a
    string or a list holding text and at most one image, so this returns on the
    first isinstance check or after one short loop.
    """
    if isinstance(contents, str):
        return False
    items = contents if isinstance(contents, (list, tuple)) else [contents]
    for item in items:
        if isinstance(item, str):
            continue
        mime = None
        blob = getattr(item, "inline_data", None)
        if blob is not None:
            mime = getattr(blob, "mime_type", None)
        elif isinstance(item, dict):
            inline = item.get("inline_data") or item.get("inlineData")
            if isinstance(inline, dict):
                mime = inline.get("mime_type") or inline.get("mimeType")
        if isinstance(mime, str) and mime.startswith(("audio/", "video/")):
            return True
    return False


async def _live_turn(parts: list, system: str, key: str, timeout_s: float) -> str:
    from google import genai
    from google.genai import types as gtypes

    cl = genai.Client(api_key=key, http_options={"api_version": "v1beta"})
    # Silence the persona, or it answers instead of complying.
    #
    # These are conversational models and they behave like it: asked "Reply with
    # one word: ok" a Live turn came back with "Understood." — it treated the
    # instruction as something to acknowledge rather than something to do. The
    # REST models do not, because nobody ever taught them to be in a
    # conversation. Every call through this module wants a value, not a reply,
    # so the session is told what it is before it is told what to do.
    kwargs = {
        "response_modalities": ["AUDIO"],
        "output_audio_transcription": {},
        "system_instruction": _ONE_SHOT_SYSTEM + (f"\n\n{system}" if system else ""),
    }

    cm = cl.aio.live.connect(model=_live_model(),
                             config=gtypes.LiveConnectConfig(**kwargs))
    session = await asyncio.wait_for(cm.__aenter__(), 30)
    try:
        await session.send_client_content(
            turns={"role": "user", "parts": parts}, turn_complete=True)
        chunks: list[str] = []

        async def drain():
            async for resp in session.receive():
                sc = getattr(resp, "server_content", None)
                if sc and sc.output_transcription and sc.output_transcription.text:
                    chunks.append(sc.output_transcription.text)

        await asyncio.wait_for(drain(), timeout=timeout_s)
        # The transcription can trail the audio turn by a beat; a short second
        # drain stops a reply being cut mid-token. chat_takeover learned this
        # the same way and for the same reason.
        try:
            await asyncio.wait_for(drain(), timeout=1.5)
        except asyncio.TimeoutError:
            pass
        return "".join(chunks).strip()
    finally:
        try:
            await cm.__aexit__(None, None, None)
        except Exception:
            pass


def _live_call(contents, config, timeout_ms: int, key: str):
    """One throwaway Live session, run on its own loop in its own thread.

    A dedicated thread rather than asyncio.run() on the caller's: these are
    invoked from plugin executor threads, from UI worker threads and from
    main.py's own event loop, and asyncio.run() inside a thread that already has
    a running loop raises. Its own thread has no loop to collide with, wherever
    it was called from.

    Reconnecting costs 0.24s, so nothing is kept alive between calls — no
    session lifetime cap to manage, no GoAway to handle, no shared state.
    """
    system = ""
    if config is not None:
        system = getattr(config, "system_instruction", None) or \
            (config.get("system_instruction") if isinstance(config, dict) else "") or ""

    parts = _to_live_parts(contents)
    if not parts:
        return None

    if not _LIVE_SLOTS.acquire(timeout=_LIVE_SLOT_WAIT):
        # Every slot is busy. Do not wait it out: falling to REST costs less
        # than holding a session the user's conversation might want.
        raise RuntimeError("no free Live slot — leaving them for the conversation")

    box: dict = {}

    def runner():
        try:
            box["text"] = asyncio.run(
                _live_turn(parts, str(system), key, max(10.0, timeout_ms / 1000.0)))
        except BaseException as e:                     # noqa: BLE001
            box["error"] = e

    try:
        th = threading.Thread(target=runner, daemon=True, name="gemini-live-oneshot")
        th.start()
        th.join(timeout=max(15.0, timeout_ms / 1000.0 + 20.0))
    finally:
        _LIVE_SLOTS.release()
    if "error" in box:
        raise box["error"]
    text = box.get("text")
    return _Reply(text) if text else None


# ─────────────────────────────────────────────────────────────────────────────
#  The provider rung
# ─────────────────────────────────────────────────────────────────────────────
# Everything below is inert on an install that has not configured a provider:
# `_provider_rungs()` reads one key out of the config the app already loads and
# returns an empty tuple, and the `providers` package is never imported. That is
# the whole cost of this feature to somebody who does not use it.
# The settings, cached against the config file's modification time.
#
# The first version of this read the config on every call, which is a JSON parse
# on the hottest path in the module — measured at 143 microseconds per call, paid
# by every one-shot request in the app whether or not a provider exists. An mtime
# check is a stat, and it invalidates itself the moment the panel writes, so
# nothing has to remember to tell this module that a setting changed.
_prov_cache: tuple[float, str, bool] = (-1.0, "", False)
_prov_lock = threading.Lock()


def _provider_settings() -> tuple[str, bool]:
    """(provider id or '', try it before Gemini)."""
    global _prov_cache
    try:
        mtime = _KEY_FILE.stat().st_mtime
    except OSError:
        return "", False

    with _prov_lock:
        if _prov_cache[0] == mtime:
            return _prov_cache[1], _prov_cache[2]

    try:
        from memory.config_manager import get_sidecar_first, get_sidecar_provider
        pid, first = get_sidecar_provider(), get_sidecar_first()
    except Exception:
        pid, first = "", False

    with _prov_lock:
        _prov_cache = (mtime, pid, first)
    return pid, first


def _provider_rungs(tier: str) -> tuple[str, ...]:
    """The configured side-call provider, as a ladder rung — or nothing.

    SEARCH is excluded deliberately. Grounded search needs
    `candidates[...].grounding_metadata`, which only Google's REST models
    produce; a provider would return a confident answer with no sources behind
    it, which is worse than saying it cannot search. The same reasoning already
    keeps the Live rung off that ladder.
    """
    if tier == SEARCH:
        return ()
    pid, _ = _provider_settings()
    return (PROVIDER_PREFIX + pid,) if pid else ()


def _provider_first() -> bool:
    return _provider_settings()[1]


def _to_openai_messages(contents, config) -> tuple[list[dict], Optional[float]]:
    """REST `contents` -> OpenAI `messages`, with the system instruction split
    out of the config where one was given.

    Deliberately shares `providers.llm_openai.parts_to_openai` rather than
    re-deriving the image shape here: there is exactly one correct way to turn
    this app's `inline_data` payloads into a data URI, and two copies of it will
    not stay the same.
    """
    from providers.llm_openai import parts_to_openai

    system = ""
    temperature = None
    if config is not None:
        si = getattr(config, "system_instruction", None) or (
            config.get("system_instruction") if isinstance(config, dict) else None)
        if si:
            system = si if isinstance(si, str) else str(si)
        t = getattr(config, "temperature", None) if not isinstance(config, dict) \
            else config.get("temperature")
        if t is not None:
            temperature = float(t)

    items = contents if isinstance(contents, (list, tuple)) else [contents]
    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": parts_to_openai(list(items))})
    return messages, temperature


def _provider_call(model: str, contents, config, timeout_ms: int):
    """One generation through a configured provider. Raises like every other
    rung, so the loop's error classification applies to it unchanged."""
    from memory.config_manager import get_provider_config
    from providers import registry

    pid = model[len(PROVIDER_PREFIX):]
    cfg = get_provider_config(pid)
    missing = registry.missing_fields(pid, cfg)
    if missing:
        raise RuntimeError(f"404 not configured: {', '.join(missing)}")

    llm = registry.build(pid, cfg)
    messages, temperature = _to_openai_messages(contents, config)
    text_out, _calls = llm.complete(
        messages, tools=None,
        timeout=max(MIN_TIMEOUT_MS, int(timeout_ms)) / 1000.0,
        temperature=temperature)
    if not (text_out or "").strip():
        raise RuntimeError("the provider returned an empty answer")
    return _Reply(text_out)


def call(contents, tier: str = FAST, config=None,
         timeout_ms: int = DEFAULT_TIMEOUT_MS, key: str = ""):
    """Run one generation, walking the ladder until one answers.

    Returns the SDK's own response object, so callers that need more than the
    text — grounding metadata, candidates, usage — still get it. Returns None
    when every model on the ladder failed; the reason for each is printed, since
    a silent None during a session nobody can debug is how the original problem
    stayed hidden.
    """
    # `tier` is normally FAST or SMART. Anything else is taken to be an explicit
    # model name — screen_agent lets the user pick one in its settings — and it
    # is tried first, with the reasoning ladder behind it. So a user's choice is
    # honoured, and a user's choice that is having an outage still degrades to
    # something that answers instead of to nothing.
    ladder = _LADDERS.get(tier)
    if ladder is None:
        ladder = (tier,) + tuple(m for m in _LADDERS[SMART] if m != tier)

    # A configured provider joins the ladder — behind Gemini by default, in
    # front of it when the user has asked for that. One config read; on an
    # install with no provider this is an empty tuple and nothing changes.
    rungs = _provider_rungs(tier)
    if rungs:
        ladder = (rungs + tuple(ladder)) if _provider_first() \
            else (tuple(ladder) + rungs)

    resolved_key = key or api_key()
    if not resolved_key:
        # A provider does not need a Gemini key. Before this, a missing key
        # meant every one-shot call in the app failed — which is exactly the
        # situation somebody configuring another provider is trying to leave.
        if not rungs:
            print("[Gemini] no Gemini API key is configured")
            return None
        ladder = rungs

    # A recording can only be read by the REST models. Dropping the two rungs
    # that cannot take one is not a fallback — it is declining to make a request
    # whose answer is already known, and it is why nothing has to be cooled down
    # afterwards. Text and images are untouched by this: `_carries_recording`
    # returns False for every call site that existed before it.
    if _carries_recording(contents):
        ladder = tuple(m for m in ladder
                       if m != LIVE and not m.startswith(PROVIDER_PREFIX))
        if not ladder:
            print("[Gemini] nothing on this ladder can read a recording")
            return None

    cl = None
    tried = [m for m in ladder if not _cooling(m)] or list(ladder)
    for model in tried:
        try:
            if model == LIVE:
                reply = _live_call(contents, config, timeout_ms, resolved_key)
                if reply is not None:
                    return reply
                raise RuntimeError("the Live turn came back empty")
            if model.startswith(PROVIDER_PREFIX):
                return _provider_call(model, contents, config, timeout_ms)
            if cl is None:
                cl = client(timeout_ms=timeout_ms, key=resolved_key)
            kwargs = {"model": model, "contents": contents}
            if config is not None:
                kwargs["config"] = config
            return cl.models.generate_content(**kwargs)
        except Exception as e:
            msg = str(e)
            if is_quota_error(msg):
                _cool(model)
                print(f"[Gemini] {model}: out of quota — skipping it for "
                      f"{_COOLDOWN_SECONDS // 60} minutes")
            elif is_gone_error(msg):
                _cool(model, _GONE_SECONDS)
                print(f"[Gemini] {model}: unavailable to this key — set aside")
            elif is_unavailable_error(msg):
                _cool(model, _UNAVAILABLE_SECONDS)
                print(f"[Gemini] {model}: not answering — resting it for "
                      f"{_UNAVAILABLE_SECONDS // 60} minutes")
            else:
                print(f"[Gemini] {model}: {type(e).__name__}: {msg[:140]}")
    return None


def text(contents, tier: str = FAST, config=None,
         timeout_ms: int = DEFAULT_TIMEOUT_MS, key: str = "", default: str = "") -> str:
    """`call`, reduced to the reply text. `default` when nothing answered."""
    resp = call(contents, tier=tier, config=config,
                timeout_ms=timeout_ms, key=key)
    if resp is None:
        return default
    return (getattr(resp, "text", None) or "").strip() or default


def as_json(contents, tier: str = FAST, config=None,
            timeout_ms: int = DEFAULT_TIMEOUT_MS, key: str = "", default=None):
    """`text`, parsed as JSON, tolerating the fences and prose a model wraps it
    in. `default` when nothing answered or the answer would not parse."""
    raw = text(contents, tier=tier, config=config, timeout_ms=timeout_ms, key=key)
    if not raw:
        return default
    if "{" in raw and "}" in raw:
        raw = raw[raw.find("{"): raw.rfind("}") + 1]
    elif "[" in raw and "]" in raw:
        raw = raw[raw.find("["): raw.rfind("]") + 1]
    try:
        return json.loads(raw)
    except Exception as e:
        print(f"[Gemini] reply was not JSON: {e}")
        return default
