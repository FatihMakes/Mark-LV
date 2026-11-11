"""
One file, every OpenAI-compatible endpoint — local servers and hosted APIs alike.

WHY ONE FILE COVERS SO MANY
    Ollama, LM Studio, llama.cpp's server, vLLM, LocalAI, KoboldCpp, Jan and
    text-generation-webui all serve `/v1/chat/completions`, and so do OpenAI,
    Groq, DeepSeek, Mistral, xAI, OpenRouter, Together, Fireworks, Perplexity,
    Cerebras and DashScope. The difference between "a local model" and "somebody
    else's API" is a base URL and whether a key is sent.

    That is the single reason this folder is a handful of files rather than
    twenty, and why "run it on my own machine" and "use a different vendor" —
    two requests that sound unrelated — are answered by the same code.

NO NEW DEPENDENCY
    `httpx` is a hard dependency of `google-genai`, which this app cannot run
    without. So it is present on every machine that already runs the assistant,
    on every operating system, and nothing has to be installed to use this file.
    It is still checked with `find_spec` rather than assumed, because a provider
    that raises ImportError during discovery would take the panel down with it.

WHAT IS DELIBERATELY NOT HERE
    No language handling of any kind. Not a greeting, not a keyword, not a
    locale test. What language the assistant speaks is decided by the system
    prompt and the model, exactly as it is with Gemini today, and this file
    passes text through without inspecting it. A provider that recognised words
    would work in the languages its author happened to speak and quietly fail in
    every other one.
"""
from __future__ import annotations

import json
import time
import contextlib
from importlib.util import find_spec
from typing import Any, AsyncIterator, Optional

from .base import DEFAULT_CAPS, Field, FunctionCall

# ─────────────────────────────────────────────────────────────────────────────
#  Known endpoints — so nobody has to remember a port number
# ─────────────────────────────────────────────────────────────────────────────
# Local ones are written as 127.0.0.1 and NOT as localhost. That is not a style
# choice and it is the single most common way this feature fails on Windows:
# `localhost` resolves to ::1 before 127.0.0.1 there, Ollama and llama.cpp bind
# to the IPv4 loopback only, and the connection is refused or waits out the
# resolver before falling back. macOS and most Linux distributions happen to
# order it the other way, which is exactly why it gets shipped broken — it works
# on the developer's machine.
# Spare Gemini models, fastest first, for when the chosen one is out of quota.
_GEMINI_SPARES = ("gemini-3.1-flash-lite", "gemini-3.5-flash", "gemini-3-flash-preview",
                  "gemini-flash-lite-latest")
_gemini_rest: dict[str, float] = {}     # model -> monotonic time it may be tried again

PRESETS: dict[str, str] = {
    "Ollama":               "http://127.0.0.1:11434/v1",
    "LM Studio":            "http://127.0.0.1:1234/v1",
    "llama.cpp server":     "http://127.0.0.1:8080/v1",
    "vLLM":                 "http://127.0.0.1:8000/v1",
    "Jan":                  "http://127.0.0.1:1337/v1",
    "OpenAI":               "https://api.openai.com/v1",
    "Groq":                 "https://api.groq.com/openai/v1",
    "DeepSeek":             "https://api.deepseek.com/v1",
    "Mistral":              "https://api.mistral.ai/v1",
    "xAI":                  "https://api.x.ai/v1",
    "OpenRouter":           "https://openrouter.ai/api/v1",
    "Together":             "https://api.together.xyz/v1",
    "Cerebras":             "https://api.cerebras.ai/v1",
    "MiniMax":              "https://api.minimax.io/v1",
    "Google Gemini":        "https://generativelanguage.googleapis.com/v1beta/openai",
}

# ─────────────────────────────────────────────────────────────────────────────
#  The same list again, shaped for a person rather than for the code
# ─────────────────────────────────────────────────────────────────────────────
# WHY THIS EXISTS, AND IT IS NOT A DUPLICATE
#     One provider covering thirteen services is the right shape for the code —
#     it is why this file is 400 lines instead of five files of 400. It is the
#     WRONG shape for the panel, and that was found by watching somebody use it:
#     they wanted to run a model on their own machine, opened the dropdown, saw
#     one entry reading "OpenAI-compatible", and concluded the feature was not
#     there. Nobody looking for Ollama searches for the name of a protocol.
#
#     So the panel expands this list instead of showing the provider once.
#     `local` decides the wording the interface puts next to it, because "on
#     this computer" and "someone else's server" is the distinction people
#     actually care about — and the one with privacy consequences.
#
#     Everything here is a shortcut for filling in the address. Anybody with an
#     endpoint that is not on this list picks the last entry and types it.
# A budget applied to LOCAL entries only, and it comes from a measurement.
#
# This install declares 20 tools and 18,039 characters with no plugins enabled,
# and more with them. Reported from a real session on qwen3:8b over Ollama, on
# the CPU:
#
#     think  13.02s   first word 11.92s   275 chars
#     think   4.84s   first word  4.56s    50 chars
#
# Between 89 and 94 percent of the model's time was spent before the first word
# — that is prompt processing, not generation, and the tool declarations are the
# largest part of the prompt. Cutting to 14 tools / 12,000 characters keeps
# every tool that makes this assistant itself (schema.CORE_TOOLS decides the
# order) and removes a third of the prompt from every turn.
#
# Cloud entries get none of this: they process prompts on hardware where it does
# not show, and taking tools away from a capable model would be a loss for no
# gain. Anybody can change or clear it under MORE.
_LOCAL_BUDGET = {"max_tools": "14", "max_tool_chars": "12000"}

# What every entry resets when it is picked. All of these share ONE stored
# config, so without it the model name from the last service carried over —
# "qwen3:8b" sent to OpenAI is a 404 — and Ollama's tool budget followed the
# user to a cloud model that has no use for it.
_LOCAL = {"model": "", **_LOCAL_BUDGET}
_CLOUD = {"model": "", "max_tools": "", "max_tool_chars": ""}

CATALOG = (
    {"id": "ollama",     "label": "Ollama",
     "note": "on this computer · free", "local": True,
     "values": {"base_url": PRESETS["Ollama"], **_LOCAL}},
    {"id": "lmstudio",   "label": "LM Studio",
     "note": "on this computer · free", "local": True,
     "values": {"base_url": PRESETS["LM Studio"], **_LOCAL}},
    {"id": "llamacpp",   "label": "llama.cpp",
     "note": "on this computer · free", "local": True,
     "values": {"base_url": PRESETS["llama.cpp server"], **_LOCAL}},
    {"id": "vllm",       "label": "vLLM",
     "note": "on this computer · free", "local": True,
     "values": {"base_url": PRESETS["vLLM"], **_LOCAL}},
    {"id": "jan",        "label": "Jan",
     "note": "on this computer · free", "local": True,
     "values": {"base_url": PRESETS["Jan"], **_LOCAL}},
    {"id": "gemini",     "label": "Google Gemini",
     "note": "cloud · leave the key empty to use your Gemini key", "local": False,
     "values": {"base_url": PRESETS["Google Gemini"], **_CLOUD,
                "model": "gemini-3.5-flash"}},
    {"id": "minimax",    "label": "MiniMax",
     "note": "cloud · needs a key", "local": False,
     "values": {"base_url": PRESETS["MiniMax"], **_CLOUD,
                "model": "MiniMax-M3.1-Flash"}},
    {"id": "groq",       "label": "Groq",
     "note": "cloud · very fast · free tier", "local": False,
     "values": {"base_url": PRESETS["Groq"], **_CLOUD}},
    {"id": "openai",     "label": "OpenAI",
     "note": "cloud · needs a key", "local": False,
     "values": {"base_url": PRESETS["OpenAI"], **_CLOUD}},
    {"id": "deepseek",   "label": "DeepSeek",
     "note": "cloud · needs a key", "local": False,
     "values": {"base_url": PRESETS["DeepSeek"], **_CLOUD}},
    {"id": "mistral",    "label": "Mistral",
     "note": "cloud · needs a key", "local": False,
     "values": {"base_url": PRESETS["Mistral"], **_CLOUD}},
    {"id": "xai",        "label": "xAI (Grok)",
     "note": "cloud · needs a key", "local": False,
     "values": {"base_url": PRESETS["xAI"], **_CLOUD}},
    {"id": "openrouter", "label": "OpenRouter",
     "note": "cloud · many models behind one key", "local": False,
     "values": {"base_url": PRESETS["OpenRouter"], **_CLOUD}},
    {"id": "together",   "label": "Together",
     "note": "cloud · needs a key", "local": False,
     "values": {"base_url": PRESETS["Together"], **_CLOUD}},
    {"id": "cerebras",   "label": "Cerebras",
     "note": "cloud · needs a key", "local": False,
     "values": {"base_url": PRESETS["Cerebras"], **_CLOUD}},
    {"id": "custom",     "label": "Something else",
     "note": "type the address yourself", "local": None,
     "values": {}},
)


def _normalise(url: str) -> str:
    """Whatever the user pasted, turned into a base URL that works.

    Three things get fixed, all of them things people actually type:
      * `localhost` -> `127.0.0.1`, for the Windows resolver reason above.
      * A missing scheme, because "192.168.1.40:11434" is what a person writes.
      * A missing or duplicated `/v1`, because half the setup guides include it
        and half do not, and `.../v1/v1/chat/completions` is a 404 that reads
        like the server is down.
    """
    url = (url or "").strip().rstrip("/")
    if not url:
        return ""
    if "://" not in url:
        url = "http://" + url
    scheme, _, rest = url.partition("://")
    host, slash, path = rest.partition("/")
    if host.split(":")[0].lower() == "localhost":
        port = host.partition(":")[2]
        host = "127.0.0.1" + (f":{port}" if port else "")
    url = f"{scheme}://{host}{slash}{path}".rstrip("/")
    # Only append /v1 when there is no path at all. An endpoint served under a
    # prefix (a reverse proxy, OpenRouter's /api/v1) must be left alone.
    if not path:
        url += "/v1"
    return url


def _int_or(value, default):
    """A number out of a settings field, where "" and "0" both mean "not set".

    The panel writes 0 into a numeric field nobody filled in, so an explicit
    zero and an untouched box look the same in the stored settings. Reading both
    as "decide for me" is the useful interpretation: somebody who genuinely
    wants every tool offered to a local model types a large number, which is a
    thing they can see they did."""
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return n if n > 0 else default


def _is_own_hardware(url: str) -> bool:
    """Is this model running on a machine the user owns?

    Loopback, and also the private address ranges — because "my own model" very
    often means Ollama on the desktop in the next room, reached from a laptop.
    That machine has the same CPU-bound prompt processing as this one, so it
    wants the same budget; a hosted endpoint does not.
    """
    host = url.partition("://")[2].partition("/")[0].partition(":")[0].lower()
    if host in ("localhost", "::1") or host.startswith("127."):
        return True
    if host.endswith(".local") or host.endswith(".lan"):
        return True
    parts = host.split(".")
    if len(parts) == 4 and all(p.isdigit() for p in parts):
        a, b = int(parts[0]), int(parts[1])
        return a == 10 or (a == 192 and b == 168) or (a == 172 and 16 <= b <= 31)
    return False


def _is_loopback(url: str) -> bool:
    host = url.partition("://")[2].partition("/")[0].partition(":")[0].lower()
    return host in ("127.0.0.1", "::1", "localhost") or host.startswith("127.")


# ─────────────────────────────────────────────────────────────────────────────
#  Message and tool-call plumbing
# ─────────────────────────────────────────────────────────────────────────────
def parts_to_openai(parts: list) -> Any:
    """Gemini `parts` -> an OpenAI message `content`.

    The app builds image payloads as `{"inline_data": {"mime_type", "data"}}`,
    with the data already base64 — see `_flush_pending_vision` in main.py and
    `_to_live_parts` in core/gemini.py. OpenAI wants the same bytes as a data
    URI, so this is a reshape rather than a re-encode.

    Text-only content is returned as a bare string, not a one-element list.
    Several local servers accept only the string form for plain text, and every
    server accepts it, so it is the safer of the two shapes.
    """
    out: list[dict] = []
    texts: list[str] = []
    for p in parts or []:
        if isinstance(p, str):
            texts.append(p)
            out.append({"type": "text", "text": p})
            continue
        if not isinstance(p, dict):
            # An SDK Part object.
            blob = getattr(p, "inline_data", None)
            if blob is not None:
                data = getattr(blob, "data", None)
                mime = getattr(blob, "mime_type", None) or "image/png"
                if isinstance(data, bytes):
                    import base64
                    data = base64.b64encode(data).decode("ascii")
                out.append({"type": "image_url",
                            "image_url": {"url": f"data:{mime};base64,{data}"}})
                continue
            txt = getattr(p, "text", None)
            if txt:
                texts.append(txt)
                out.append({"type": "text", "text": txt})
            continue
        if "text" in p:
            texts.append(p["text"])
            out.append({"type": "text", "text": p["text"]})
        elif "inline_data" in p:
            blob = p["inline_data"]
            data = blob.get("data")
            mime = blob.get("mime_type") or "image/png"
            if isinstance(data, bytes):
                import base64
                data = base64.b64encode(data).decode("ascii")
            out.append({"type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{data}"}})

    if len(out) == len(texts):
        return "\n".join(texts)
    return out


class _ToolCallBuffer:
    """Reassembles tool calls that arrive in fragments.

    A streamed tool call does not come as one object. The name arrives in one
    delta, the arguments in a dozen more, each tagged with an `index`, and a
    burst can carry two calls interleaved. Concatenating without honouring the
    index produces one call with two names welded together — which then fails to
    match any tool and looks like the model hallucinated.
    """

    def __init__(self) -> None:
        self._slots: dict[int, dict] = {}

    def feed(self, deltas: list) -> None:
        for d in deltas or []:
            idx = d.get("index", 0)
            slot = self._slots.setdefault(idx, {"id": "", "name": "", "args": ""})
            if d.get("id"):
                slot["id"] = d["id"]
            fn = d.get("function") or {}
            if fn.get("name"):
                slot["name"] += fn["name"]
            if fn.get("arguments"):
                slot["args"] += fn["arguments"]

    def drain(self) -> list[FunctionCall]:
        calls = []
        for idx in sorted(self._slots):
            slot = self._slots[idx]
            if not slot["name"]:
                continue
            raw = slot["args"].strip() or "{}"
            try:
                args = json.loads(raw)
            except Exception:
                # A model that emits invalid JSON is common enough that dropping
                # the call is the wrong answer: the tool name is usually right
                # and the arguments usually salvageable. Try the largest object
                # in the string before giving up on the call entirely.
                args = {}
                if "{" in raw and "}" in raw:
                    try:
                        args = json.loads(raw[raw.find("{"):raw.rfind("}") + 1])
                    except Exception:
                        args = {}
            if not isinstance(args, dict):
                args = {"value": args}
            calls.append(FunctionCall(id=slot["id"] or f"call_{idx}",
                                      name=slot["name"], args=args))
        self._slots.clear()
        return calls


# ─────────────────────────────────────────────────────────────────────────────
#  The provider
# ─────────────────────────────────────────────────────────────────────────────
class OpenAICompatLLM:
    """Speaks to anything that serves /v1/chat/completions."""

    def __init__(self, cfg: dict):
        import httpx  # inside the factory path — never at module scope

        self._httpx = httpx
        self.base = _normalise(cfg.get("base_url") or "")
        self.model = (cfg.get("model") or "").strip()
        self.key = (cfg.get("api_key") or "").strip()
        if not self.key and "generativelanguage.googleapis.com" in self.base:
            # The Gemini entry needs no second key: the one Gemini Live already
            # uses is the natural choice, and a different key pasted here is how
            # a free-tier project ends up behind a paid one (20 requests a day).
            try:
                from memory.config_manager import get_gemini_key
                self.key = (get_gemini_key() or "").strip()
            except Exception:
                pass
        self.timeout = float(cfg.get("timeout") or 60.0)

        # A budget on a model running on your own hardware, unless the user
        # chose one themselves.
        #
        # It used to live only in the catalog's preset values, which are applied
        # when a service is PICKED in the panel. Anybody who had already set up
        # Ollama before the budget existed never received it — measured on this
        # install, 20 tools and 18,039 characters instead of 14 and 11,871, on
        # every turn, which is most of a fourteen-second wait for the first word.
        # A default that only reaches new installs is not a default.
        #
        # Hosted endpoints get none of it: they process prompts on hardware
        # where this does not show, and taking tools away from a capable model
        # is a loss for no gain.
        max_tools = _int_or(cfg.get("max_tools"), None)
        max_chars = _int_or(cfg.get("max_tool_chars"), None)
        if _is_own_hardware(self.base) and max_tools is None and max_chars is None:
            max_tools = int(_LOCAL_BUDGET["max_tools"])
            max_chars = int(_LOCAL_BUDGET["max_tool_chars"])

        self.caps = dict(DEFAULT_CAPS)
        self.caps.update({
            "tools": bool(cfg.get("tools", True)),
            "tool_format": "openai",
            "vision": bool(cfg.get("vision", False)),
            "max_tools": max_tools,
            "max_tool_chars": max_chars,
        })

        # A proxy configured for the whole machine must not be used to reach a
        # server on that same machine. On a corporate Windows or macOS setup
        # HTTP_PROXY is often set globally, and httpx honours it by default, so
        # a request to 127.0.0.1 is handed to a proxy that refuses it. The
        # symptom is a timeout that looks exactly like "Ollama is not running",
        # which sends people to reinstall a server that was working the whole
        # time.
        self._trust_env = not _is_loopback(self.base)
        # Which model the stored vision answer was measured on. A cached "no"
        # from qwen3 must not silently disable a llava the user switched to.
        self._vision_known = (str(cfg.get("vision_model") or "").strip()
                              if cfg.get("vision_model") else "")
        if self._vision_known and self._vision_known != self.model:
            self._vision_known = ""

    # -- headers ------------------------------------------------------------
    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.key:
            h["Authorization"] = f"Bearer {self.key}"
        return h

    def _body(self, messages: list[dict], tools: list[dict] | None,
              stream: bool, temperature: float | None) -> dict:
        body: dict[str, Any] = {"model": self.model, "messages": messages,
                                "stream": stream}
        if tools and self.caps["tools"]:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        if temperature is not None:
            body["temperature"] = temperature
        if "generativelanguage.googleapis.com" in self.base and "flash" in self.model:
            # Gemini 3 Flash models think before every answer unless told not
            # to, which is most of their wait in a spoken turn. Measured
            # 2026-11-04: 3.5-flash first word ~2.4 s -> ~1.6 s at "low";
            # the Lite models answer as well with no thinking at all.
            body["reasoning_effort"] = "none" if "lite" in self.model else "low"
        if "minimax" in self.base:
            # MiniMax's M2 models put their thinking inline as <think>…</think>
            # unless asked not to. The pipeline filters that out of speech, but
            # `complete()` feeds background requests that have no such filter —
            # so it is moved out of the answer at the source.
            body["reasoning_split"] = True
        return body

    # -- one-shot, synchronous: this is what core/gemini.py's ladder uses ----
    def complete(self, messages: list[dict], tools: list[dict] | None = None,
                 *, timeout: float | None = None,
                 temperature: float | None = None) -> tuple[str, list[FunctionCall]]:
        """Text and any tool calls. Raises on failure so the ladder can read the
        error, classify it and rest this rung exactly as it rests a Gemini one."""
        try:
            with self._httpx.Client(timeout=timeout or self.timeout,
                                    trust_env=self._trust_env) as cl:
                r = cl.post(f"{self.base}/chat/completions",
                            headers=self._headers(),
                            json=self._body(messages, tools, False, temperature))
                if r.status_code >= 400:
                    raise RuntimeError(f"{r.status_code} {_terse(r.text)}")
                data = r.json()
        except Exception as e:
            raise _with_address(e, self.base) from None

        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        text = msg.get("content") or ""
        buf = _ToolCallBuffer()
        buf.feed([{"index": i, **tc} for i, tc in
                  enumerate(msg.get("tool_calls") or [])])
        return text, buf.drain()

    # -- streaming, async: this is what the pipeline engine will use ---------
    async def chat(self, messages: list[dict], tools: list[dict] | None = None,
                   *, stream: bool = True,
                   temperature: float | None = None) -> AsyncIterator[dict]:
        """Yields {"text": ...} as it arrives and one {"tool_calls": [...]} at
        the end. Streaming is what lets speech start before the sentence is
        finished; a server that cannot stream still works, it just answers all
        at once."""
        body = self._body(messages, tools, stream, temperature)

        # Not wrapped with the address here: the streaming path is only used by
        # the pipeline engine, which names the provider and its target itself —
        # see providers/engine_pipeline._report. `complete()` IS wrapped, because
        # its caller is core/gemini.py's ladder, which has no such context.
        async with self._httpx.AsyncClient(timeout=self.timeout,
                                           trust_env=self._trust_env) as cl:
            if not stream:
                r = await cl.post(f"{self.base}/chat/completions",
                                  headers=self._headers(), json=body)
                if r.status_code >= 400:
                    raise RuntimeError(f"{r.status_code} {_terse(r.text)}")
                msg = ((r.json().get("choices") or [{}])[0].get("message") or {})
                if msg.get("content"):
                    yield {"text": msg["content"]}
                if msg.get("tool_calls"):
                    buf = _ToolCallBuffer()
                    buf.feed([{"index": i, **tc}
                              for i, tc in enumerate(msg["tool_calls"])])
                    yield {"tool_calls": buf.drain()}
                return

            buf = _ToolCallBuffer()
            async with self._open_stream(cl, body) as r:
                async for line in r.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        delta = ((json.loads(payload).get("choices") or [{}])[0]
                                 .get("delta") or {})
                    except Exception:
                        # One malformed frame must not end the answer. Servers
                        # do emit keepalives and comments that are not JSON.
                        continue
                    if delta.get("content"):
                        yield {"text": delta["content"]}
                    if delta.get("tool_calls"):
                        buf.feed(delta["tool_calls"])

            calls = buf.drain()
            if calls:
                yield {"tool_calls": calls}

    def _gemini_order(self) -> list[str]:
        """The chosen model first, then spares, skipping any resting one.

        Google counts Gemini quotas per model: on a free-tier project the
        chosen Flash allows 5 requests a minute, and a spoken conversation
        passes that in one minute. A 429 or 503 on one model says nothing
        about the next, so the turn moves on instead of failing."""
        now = time.monotonic()
        order = [self.model] + [m for m in _GEMINI_SPARES if m != self.model]
        awake = [m for m in order if _gemini_rest.get(m, 0) <= now]
        return awake or order

    @contextlib.asynccontextmanager
    async def _open_stream(self, cl, body: dict):
        """POST the streaming request; on Gemini, step past a model that is
        out of quota or overloaded before anything has been said."""
        gemini = "generativelanguage.googleapis.com" in self.base
        models = self._gemini_order() if gemini else [body.get("model")]
        last = ""
        for model in models:
            req = dict(body, model=model)
            if gemini and "flash" in model:
                req["reasoning_effort"] = "none" if "lite" in model else "low"
            async with cl.stream("POST", f"{self.base}/chat/completions",
                                 headers=self._headers(), json=req) as r:
                if r.status_code < 400:
                    yield r
                    return
                last = f"{r.status_code} {_terse((await r.aread()).decode('utf-8', 'replace'))}"
                if not gemini or r.status_code not in (429, 503):
                    raise RuntimeError(last)
                # a minute's quota or a load spike: rest this model briefly
                _gemini_rest[model] = time.monotonic() + (60 if r.status_code == 429 else 20)
        raise RuntimeError(last)

    # -- for the settings panel ---------------------------------------------
    def models(self, timeout: float = 8.0) -> list[str]:
        """What this endpoint offers, so the panel can present a list instead of
        asking someone to type a model name exactly right."""
        with self._httpx.Client(timeout=timeout,
                                trust_env=self._trust_env) as cl:
            r = cl.get(f"{self.base}/models", headers=self._headers())
            r.raise_for_status()
            data = r.json()
        rows = data.get("data") if isinstance(data, dict) else data
        return sorted({str(m.get("id")) for m in (rows or []) if m.get("id")})

    # A 1x1 transparent PNG — 68 bytes, the smallest thing that is a real image.
    _PIXEL = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
              "YPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")

    def can_see(self, timeout: float = 25.0) -> bool:
        """Whether this model accepts an image. ASKED, not guessed.

        The first version of the panel put the question to the user — a
        checkbox reading "This model can see images" — which is the wrong person
        to ask. Somebody who has just installed a model does not necessarily
        know whether it is multimodal, the answer is different for every tag of
        the same family, and getting it wrong is not harmless: say yes and the
        screen tool is offered to a model that will reject the request; say no
        and the assistant is told it has no eyes when it does.

        Guessing from the name was the other option and it is worse than it
        looks — `llava`, `-vl`, `vision`, `moondream`, `minicpm-v`, `pixtral`,
        `gemma3`, `internvl` and a dozen more, each with exceptions, and a list
        that is wrong the week after it is written.

        So the endpoint is asked. One tiny image, one word back. A model that
        cannot take one says so, in an error, immediately.
        """
        try:
            self.complete([{
                "role": "user",
                "content": [
                    {"type": "text", "text": "Reply with one word: ok"},
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/png;base64,{self._PIXEL}"}},
                ]}], timeout=timeout)
            return True
        except Exception:
            return False

    def ensure_vision_known(self, save=None) -> bool:
        """Find out whether this model can see, once, and remember the answer.

        WHY THIS IS NOT LEFT TO THE TEST BUTTON
            Pressing TEST answers it, and most people will never press TEST. They
            will pick Ollama, pick a model, and start talking — and a model that
            CAN see would spend the whole session being told it has no eyes,
            with the screen and camera tools withheld and no sign anything was
            wrong. A capability the assistant guesses conservatively about is a
            capability it does not have.

            So it is measured on the first connect instead, in about a second,
            and written back to the settings. Every later session reads the
            stored answer and costs nothing.

        The answer is stored WITH the model it was measured on. Changing the
        model in Ollama from qwen3 to llava changes the answer, and a cached
        "no" would quietly disable vision for a model that has it.
        """
        if self._vision_known == self.model:
            return bool(self.caps.get("vision"))
        seen = self.can_see()
        self.caps["vision"] = seen
        self._vision_known = self.model
        if save is not None:
            try:
                save({"vision": "1" if seen else "",
                      "vision_model": self.model})
            except Exception:
                pass
        return seen

    def probe(self, check_vision: bool = True) -> tuple[bool, str]:
        """Reachable and usable? Returns (ok, one line to show the user).

        The message says what to do, not what went wrong in protocol terms: the
        person reading it is trying to get a model working, not debug HTTP.
        """
        if not self.base:
            return False, "No address set."
        if not self.model:
            return False, "No model chosen."
        import time
        t0 = time.monotonic()
        try:
            text, _ = self.complete(
                [{"role": "user", "content": "Reply with the single word: ok"}],
                timeout=20.0)
        except Exception as e:
            msg = str(e)
            if "401" in msg or "403" in msg:
                return False, "The server refused the key."
            if "404" in msg:
                return False, f"No model called '{self.model}' at that address."
            if "Connect" in msg or "connect" in msg or "timed out" in msg:
                return False, ("Nothing answered at that address — is the "
                               "server running?")
            return False, _terse(msg, 90)

        took = time.monotonic() - t0
        note = ""
        if check_vision:
            self._vision_known = ""          # TEST always re-measures
            seen = self.ensure_vision_known()
            note = "  |  can see images" if seen else "  |  text only"
        return True, f"Answered in {took:.2f}s{note}"


def _with_address(exc: Exception, base: str) -> Exception:
    """The same failure, carrying the address it happened at.

    httpx says "All connection attempts failed" and names no host, which in a
    pipeline with three services in it is not enough to act on — it reads the
    same whether a local model server is down or a hosted voice is unreachable.
    The exception type is preserved so anything classifying it still can."""
    text = " ".join(str(exc).split())
    if base and base not in text:
        text = f"{text} (at {base})" if text else f"could not reach {base}"
    try:
        return type(exc)(text)
    except Exception:
        # Some exception types will not take a plain message.
        return RuntimeError(f"{type(exc).__name__}: {text}")


def _terse(text: str, limit: int = 160) -> str:
    """A server's error body, reduced to something a person can read.

    Local servers return HTML error pages and hosted ones return nested JSON;
    both are hundreds of characters of nothing when they reach a log line.
    """
    text = " ".join((text or "").split())
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            err = obj.get("error", obj)
            if isinstance(err, dict):
                text = str(err.get("message") or err)
            else:
                text = str(err)
    except Exception:
        pass
    return text[:limit] + ("..." if len(text) > limit else "")


def _make(cfg: dict) -> OpenAICompatLLM:
    return OpenAICompatLLM(cfg)


PROVIDER = {
    "id": "openai_compat",
    "kind": "llm",
    "label": "OpenAI-compatible",
    "hint": "Ollama · LM Studio · llama.cpp · vLLM · Groq · DeepSeek · OpenRouter · OpenAI · MiniMax",
    # httpx ships with google-genai, so this is always true — checked anyway,
    # because a provider must never raise during discovery.
    "requires": ("httpx",),
    "install": "httpx",
    # What the panel lists instead of this provider's own name. See CATALOG.
    "catalog": CATALOG,
    "needs": (
        Field("base_url", "Address", kind="choice",
              choices=tuple(PRESETS.values()),
              default=PRESETS["Ollama"],
              hint="A preset, or any address serving /v1/chat/completions"),
        # A choice rather than free text, even though the list starts empty:
        # LIST fills it from the endpoint itself, and the panel keeps a choice
        # field typeable so an unlisted model can still be entered. Declared as
        # plain text it rendered as a box LIST had nothing to populate.
        Field("model", "Model", kind="choice", choices=(),
              hint="Filled in from the server — press REFRESH if it is empty"),
        Field("api_key", "API key", kind="secret", required=False,
              hint="Local servers usually need none"),
        # Filled in by TEST, not asked. The user is the wrong person to ask
        # whether a model is multimodal — see OpenAICompatLLM.can_see. Kept as a
        # field so the answer is stored and so somebody who knows better can
        # override it under MORE.
        Field("vision", "Can see images", kind="bool",
              default=False, required=False, advanced=True,
              hint="Set automatically when you press TEST"),
        Field("max_tools", "Tool limit", kind="number", default=0,
              required=False, advanced=True,
              hint="0 = no limit. Small models choose badly past about 12"),
        Field("max_tool_chars", "Tool prompt limit", kind="number", default=0,
              required=False, advanced=True,
              hint="0 = no limit. Characters of tool descriptions sent each  turn"),
    ),
    "caps": {"tools": True, "tool_format": "openai"},
    "factory": _make,
}


def available() -> bool:
    """True when this provider can be built. Uses find_spec, which answers
    'is it installed' without executing the module."""
    return all(find_spec(m) is not None for m in PROVIDER["requires"])
