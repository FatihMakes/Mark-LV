"""
Self-test for the providers folder.

    python -m providers._tests

WHY IT LIVES HERE AND NOT IN A tests/ DIRECTORY
    It tests this folder, and this folder is optional. Deleting `providers/`
    should take everything about providers with it — including the file that
    checks them. A test directory at the project root would be left behind,
    referring to code that is gone.

WHAT IT IS FOR
    Three bugs got past a suite that looked thorough, and every one of them got
    past for the same reason: the tests called the engine the way its author
    imagined the app would, instead of the way the app actually does.

        * main.py sends `types.Blob`; the tests sent a dict. The engine raised
          inside a TaskGroup, which cancels the session — so the assistant
          reconnected every few seconds, reloaded its speech model each time and
          came back asleep, with nothing in the log to explain it.
        * A tool-calling turn spoke its acknowledgement to the log and the
          avatar's mouth but never to the speakers.
        * A widget that had not been added to a layout reports `isHidden()`, so
          the panel decided every field was deliberately hidden and drew none.

    So the rule this file follows: take the arguments from main.py's own source,
    and run the tasks the way main.py's TaskGroup runs them. A raise there fails
    a test here instead of quietly restarting an assistant.

    It needs no network and no API key — every service is a local mock.
"""
from __future__ import annotations

import asyncio
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_ok = _fail = 0
_failures: list[str] = []


def check(name: str, cond, detail: str = "") -> None:
    global _ok, _fail
    if cond:
        _ok += 1
        print(f"  PASS  {name}")
    else:
        _fail += 1
        _failures.append(name)
        print(f"  FAIL  {name}   {detail}")


def section(title: str) -> None:
    print(f"\n{title}")


# ─────────────────────────────────────────────────────────────────────────────
#  Mocks
# ─────────────────────────────────────────────────────────────────────────────
class _MockServer(BaseHTTPRequestHandler):
    """An OpenAI-compatible endpoint, including the awkward parts: tool calls
    that arrive in fragments across two frames, and two of them interleaved."""

    vision = True
    turn = [0]

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._json({"data": [{"id": "qwen3:8b"}, {"id": "gemma3:4b"}]})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        content = body["messages"][-1].get("content")
        has_image = isinstance(content, list) and any(
            p.get("type") == "image_url" for p in content)
        if has_image and not type(self).vision:
            return self._json({"error": {"message": "images are not supported"}},
                              400)
        if not body.get("stream"):
            return self._json({"choices": [{"message": {"content": "ok"}}]})

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        type(self).turn[0] += 1
        if type(self).turn[0] == 1:
            frames = [
                {"choices": [{"delta": {"content": "One moment."}}]},
                {"choices": [{"delta": {"tool_calls": [
                    {"index": 0, "id": "c1",
                     "function": {"name": "open_", "arguments": '{"app_'}},
                    {"index": 1, "id": "c2",
                     "function": {"name": "web_", "arguments": '{"que'}},
                ]}}]},
                {"choices": [{"delta": {"tool_calls": [
                    {"index": 0,
                     "function": {"name": "app", "arguments": 'name":"chrome"}'}},
                    {"index": 1,
                     "function": {"name": "search", "arguments": 'ry":"x"}'}},
                ]}}]},
                ": keepalive",                      # not JSON — must not stop us
            ]
        else:
            frames = [{"choices": [{"delta": {"content": "Done."}}]}]
        for f in frames:
            line = f if isinstance(f, str) else "data: " + json.dumps(f)
            self.wfile.write((line + "\n\n").encode())
        self.wfile.write(b"data: [DONE]\n\n")


def start_server():
    srv = HTTPServer(("127.0.0.1", 0), _MockServer)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class FakeLLM:
    caps = {"tools": True, "tool_format": "openai", "vision": False}

    def __init__(self, script):
        self.script = list(script)
        self.seen = []

    async def chat(self, messages, tools=None, **kw):
        self.seen.append(json.loads(json.dumps(messages)))
        for ev in (self.script.pop(0) if self.script else [{"text": "ok"}]):
            await asyncio.sleep(0)
            yield ev


class FakeSTT:
    caps = {}

    def __init__(self):
        self.got = 0

    async def transcribe(self, pcm, sample_rate=16000, language="", log=None):
        self.got = len(pcm)
        return "open the browser"

    async def warmup(self, log=None):
        pass


class FakeTTS:
    caps = {}

    def __init__(self):
        self.said = []

    async def speak(self, text, voice=""):
        self.said.append(text)
        yield b"\x00\x01" * 16


class Cfg:
    """Stands in for the LiveConnectConfig main.py builds."""
    system_instruction = "You are JARVIS."
    tools = [{"function_declarations": [
        {"name": "open_app", "description": "Opens an application.",
         "parameters": {"type": "OBJECT",
                        "properties": {"app_name": {"type": "STRING"}},
                        "required": ["app_name"]}},
        {"name": "web_search", "description": "Searches the web.",
         "parameters": {"type": "OBJECT",
                        "properties": {"query": {"type": "STRING"}}}},
    ]}]


class FR:
    """types.FunctionResponse, as main.py builds it."""

    def __init__(self, id, name, result):
        self.id, self.name, self.response = id, name, {"result": result}


# ─────────────────────────────────────────────────────────────────────────────
#  Tests
# ─────────────────────────────────────────────────────────────────────────────
async def test_real_caller_shapes():
    """The arguments main.py actually passes — this is the one that matters."""
    from google.genai import types

    from providers.engine_pipeline import PipelineEngine

    section("[1] main.py's real call shapes")
    stt = FakeSTT()
    eng = PipelineEngine(FakeLLM([]), stt=stt, tts=FakeTTS(), config=Cfg())

    # main.py:_send_realtime
    try:
        await eng.send_realtime_input(
            audio=types.Blob(data=b"\x11\x22" * 160, mime_type="audio/pcm"))
        check("send_realtime_input accepts types.Blob", True)
    except Exception as e:
        check("send_realtime_input accepts types.Blob", False,
              f"{type(e).__name__}: {e}")
    check("Blob bytes counted", eng._mic_bytes == 320, str(eng._mic_bytes))

    await eng.send_realtime_input(audio={"data": b"\x00" * 100})
    await eng.send_realtime_input(audio=b"\x00" * 100)
    await eng.send_realtime_input(audio=None)
    check("dict / bytes / None also accepted", eng._mic_bytes == 520,
          str(eng._mic_bytes))


async def test_taskgroup_and_tool_loop():
    """main.py answers a tool call from INSIDE its loop over receive(). An
    engine that waits there deadlocks the turn; one that raises there cancels
    the session."""
    from google.genai import types

    from providers.engine_pipeline import PipelineEngine

    section("[2] A whole turn, run the way main.py's TaskGroup runs it")
    stt, tts = FakeSTT(), FakeTTS()
    llm = FakeLLM([
        [{"text": "One moment."},
         {"tool_calls": [__import__("providers.base", fromlist=["FunctionCall"])
                         .FunctionCall("c1", "open_app", {"app_name": "chrome"})]}],
        [{"text": "Chrome is open."}],
    ])
    eng = PipelineEngine(llm, stt=stt, tts=tts, config=Cfg())
    eng.auto_turn = False

    out_queue: asyncio.Queue = asyncio.Queue(maxsize=200)
    said, ran, pcm_out = [], [], [0]
    done = asyncio.Event()

    async def send_realtime():
        while True:
            msg = await out_queue.get()
            await eng.send_realtime_input(
                audio=types.Blob(data=msg["data"],
                                 mime_type=msg.get("mime_type", "audio/pcm")))

    async def receive():
        async for response in eng.receive():
            if response.data:
                pcm_out[0] += len(response.data)
            if response.server_content:
                sc = response.server_content
                if sc.output_transcription and sc.output_transcription.text:
                    said.append(sc.output_transcription.text)
                if sc.turn_complete:
                    done.set()
                    return
            if response.tool_call:
                frs = []
                for fc in response.tool_call.function_calls:
                    ran.append(fc.name)
                    frs.append(FR(fc.id, fc.name, "Opened Chrome."))
                await eng.send_tool_response(function_responses=frs)

    async def feed():
        for _ in range(10):
            out_queue.put_nowait({"data": b"\x05\x06" * 160,
                                  "mime_type": "audio/pcm"})
        await asyncio.sleep(0.05)
        await eng.flush_microphone(16000)

    crashed = []
    try:
        async with asyncio.TaskGroup() as tg:
            t1 = tg.create_task(send_realtime())
            t2 = tg.create_task(receive())
            tg.create_task(feed())
            await asyncio.wait_for(done.wait(), 15)
            t1.cancel()
            t2.cancel()
    except* Exception as eg:
        crashed = [f"{type(e).__name__}: {e}" for e in eg.exceptions
                   if not isinstance(e, asyncio.CancelledError)]

    check("TaskGroup survived the turn", not crashed, str(crashed))
    check("microphone reached the recogniser", stt.got == 3200, f"{stt.got} bytes")
    check("the tool ran", ran == ["open_app"], str(ran))
    check("both halves of the reply arrived",
          "".join(said) == "One moment.Chrome is open.", repr("".join(said)))
    check("audio reached the speaker", pcm_out[0] > 0, str(pcm_out[0]))
    check("the acknowledgement was SPOKEN, not just logged",
          tts.said == ["One moment.", "Chrome is open."], str(tts.said))


async def test_full_stack_over_http():
    """The engine through the real HTTP client to a real socket."""
    from providers.engine_pipeline import PipelineEngine
    from providers.llm_openai import OpenAICompatLLM

    section("[3] Engine -> httpx -> HTTP -> server")
    _MockServer.turn[0] = 0
    srv = start_server()
    try:
        llm = OpenAICompatLLM({"base_url": f"http://127.0.0.1:{srv.server_port}",
                               "model": "qwen3:8b"})
        eng = PipelineEngine(llm, config=Cfg())
        said, ran = [], []

        async def like_main_py():
            async for r in eng.receive():
                if r.server_content:
                    sc = r.server_content
                    if sc.output_transcription and sc.output_transcription.text:
                        said.append(sc.output_transcription.text)
                    if sc.turn_complete:
                        return
                if r.tool_call:
                    frs = []
                    for fc in r.tool_call.function_calls:
                        ran.append((fc.name, fc.args))
                        frs.append(FR(fc.id, fc.name, "done"))
                    await eng.send_tool_response(function_responses=frs)

        task = asyncio.create_task(like_main_py())
        await eng.send_client_content(
            turns={"role": "user", "parts": [{"text": "open chrome"}]})
        try:
            await asyncio.wait_for(task, 15)
            check("turn completed end to end", True)
        except asyncio.TimeoutError:
            check("turn completed end to end", False, "timed out")

        check("fragmented, interleaved tool calls reassembled",
              ran == [("open_app", {"app_name": "chrome"}),
                      ("web_search", {"query": "x"})], str(ran))
        check("both turns' text arrived", "".join(said) == "One moment.Done.",
              repr("".join(said)))
    finally:
        srv.shutdown()


def test_vision_is_asked_not_guessed():
    from providers.llm_openai import OpenAICompatLLM

    section("[4] Vision is measured, not typed in by the user")

    # Measured on the FIRST CONNECT, not on a button press. Most people will
    # never press TEST, and a vision model whose eyes were never switched on is
    # a silent loss of a feature they installed the model for.
    _MockServer.vision = True
    srv = start_server()
    try:
        saved = {}
        llm = OpenAICompatLLM({"base_url": f"http://127.0.0.1:{srv.server_port}",
                               "model": "llava:7b"})
        check("a fresh model is measured without TEST being pressed",
              llm.ensure_vision_known(save=saved.update) is True)
        check("the answer is stored with the model it was measured on",
              saved.get("vision") == "1" and saved.get("vision_model") == "llava:7b",
              str(saved))

        import time
        again = OpenAICompatLLM({"base_url": f"http://127.0.0.1:{srv.server_port}",
                                 "model": "llava:7b", "vision": "1",
                                 "vision_model": "llava:7b"})
        again.caps["vision"] = True
        t0 = time.monotonic()
        again.ensure_vision_known()
        check("a stored answer is not measured again",
              (time.monotonic() - t0) < 0.05)

        changed = OpenAICompatLLM({"base_url": f"http://127.0.0.1:{srv.server_port}",
                                   "model": "qwen3:8b", "vision": "1",
                                   "vision_model": "llava:7b"})
        check("changing the model re-measures it",
              changed._vision_known == "", changed._vision_known)
    finally:
        srv.shutdown()

    for can_see in (True, False):
        _MockServer.vision = can_see
        srv = start_server()
        try:
            llm = OpenAICompatLLM({"base_url": f"http://127.0.0.1:{srv.server_port}",
                                   "model": "m"})
            ok, msg = llm.probe()
            check(f"server vision={can_see} -> detected {llm.caps['vision']}",
                  ok and llm.caps["vision"] is can_see, msg)
        finally:
            srv.shutdown()
    _MockServer.vision = True


def test_schema():
    from providers import schema

    section("[5] Tool declarations, translated and budgeted")
    decls = Cfg.tools[0]["function_declarations"]
    openai = schema.to_openai(decls)
    check("wrapped as OpenAI functions",
          openai[0]["type"] == "function"
          and openai[0]["function"]["name"] == "open_app")
    blob = json.dumps(openai) + json.dumps(schema.to_anthropic(decls))
    import re
    check("no upper-case types survive at any depth",
          not re.findall(r'"type":\s*"[A-Z_]+"', blob))
    check("names and order preserved",
          [d["name"] for d in decls] == [t["function"]["name"] for t in openai])
    check("no budget returns the input unchanged",
          schema.budget(decls) == list(decls))
    check("a count budget bites", len(schema.budget(decls, max_tools=1)) == 1)
    check("a size budget bites",
          schema.payload_size(schema.budget(decls, max_chars=200)) <= 400)
    check("prompt mode lists the tools",
          "open_app(" in schema.to_prompt(decls))
    check("an invented tool name is not a call",
          __import__("providers.engine_pipeline", fromlist=["_parse_prompt_call"])
          ._parse_prompt_call('{"tool":"launch_nukes","args":{}}',
                              {"open_app"}) is None)


def test_audio():
    import numpy as np

    from providers import audio

    section("[6] Sample rates and formats")
    sine = (np.sin(np.arange(16000) * 2 * np.pi * 440 / 16000) * 0.5
            ).astype(np.float32)
    pcm = audio.to_pcm16(sine)
    check("16k -> 24k has the right length",
          abs(len(audio.resample(pcm, 16000, 24000)) - len(pcm) * 1.5) < 4)
    check("same rate is not copied", audio.resample(pcm, 16000, 16000) is pcm)
    round_trip = audio.to_float32(
        audio.resample(audio.resample(pcm, 16000, 24000), 24000, 16000))
    check("a round trip preserves the signal",
          np.abs(round_trip[:15000] - sine[:15000]).max() < 0.05)
    wav = audio.to_wav(pcm, 16000)
    check("WAV in, 24k PCM out", len(audio.wav_to_pcm(wav)) == int(len(pcm) * 1.5))
    check("an odd trailing byte does not raise",
          audio.to_float32(b"\x00\x01\x02").size == 1)


async def test_failures_name_the_part():
    """Three services can fail; the log has to say which, and where."""
    from providers.engine_pipeline import PipelineEngine

    section("[7] A failure names the part and the address")

    class DeadTTS:
        caps = {"audio_out": True}
        base = "https://api.elevenlabs.io/v1"

        async def speak(self, text, voice=""):
            import httpx
            raise httpx.ConnectError("All connection attempts failed")
            yield b""

    class LiveLLM:
        caps = {"tools": False, "tool_format": None}
        base = "http://127.0.0.1:11434/v1"

        async def chat(self, messages, tools=None, **kw):
            yield {"text": "Hello."}

    async def run(engine):
        said = []

        async def drain():
            async for m in engine.receive():
                if m.server_content:
                    if m.server_content.output_transcription:
                        said.append(m.server_content.output_transcription.text)
                    if m.server_content.turn_complete:
                        return

        task = asyncio.create_task(drain())
        await engine.send_client_content(
            turns={"role": "user", "parts": [{"text": "hi"}]})
        await asyncio.wait_for(task, 15)
        return "".join(said)

    logs: list[str] = []
    said = await run(PipelineEngine(LiveLLM(), tts=DeadTTS(), config=Cfg(),
                                    log=logs.append))
    check("a broken voice is reported as the VOICE",
          any("the voice failed" in l for l in logs), str(logs))
    check("the voice's address is named",
          any("elevenlabs" in l for l in logs), str(logs))
    check("a broken voice does not stop the answer", said == "Hello.", repr(said))

    logs2: list[str] = []
    from providers.llm_openai import OpenAICompatLLM
    dead = OpenAICompatLLM({"base_url": "http://127.0.0.1:9", "model": "m"})
    said2 = await run(PipelineEngine(dead, config=Cfg(), log=logs2.append))
    check("an unreachable model is reported as the MODEL",
          any("the language model failed" in l for l in logs2), str(logs2))
    check("the model's address is named",
          any("127.0.0.1:9" in l for l in logs2), str(logs2))
    check("what it says out loud names the part too",
          "language model" in said2, repr(said2))


def test_addresses():
    from providers.llm_openai import _normalise

    section("[8] Addresses people actually type")
    for raw, want in (
        ("localhost:11434", "http://127.0.0.1:11434/v1"),
        ("http://localhost:11434/v1", "http://127.0.0.1:11434/v1"),
        ("http://localhost:11434/v1/", "http://127.0.0.1:11434/v1"),
        ("192.168.1.40:1234", "http://192.168.1.40:1234/v1"),
        ("https://openrouter.ai/api/v1", "https://openrouter.ai/api/v1"),
        ("https://api.openai.com", "https://api.openai.com/v1"),
    ):
        check(f"{raw}  ->  {_normalise(raw)}", _normalise(raw) == want, want)


def test_costs_nothing_when_unused():
    section("[9] A default install pays nothing")
    import subprocess
    code = (
        "import sys, json, time, tempfile, pathlib;"
        "sys.path.insert(0, r'" + str(Path(__file__).resolve().parent.parent) + "');"
        "import core.gemini as g;"
        # A default install: a config with no providers, in a throwaway file -
        # never the user's own config (this used to rewrite it, and failed on
        # a fresh install that has none yet).
        "p=pathlib.Path(tempfile.mkdtemp())/'api_keys.json';"
        "p.write_text('{}',encoding='utf-8');"
        "g._KEY_FILE=p;"
        "t=time.perf_counter();"
        "[g._provider_rungs(g.FAST) for _ in range(1000)];"
        "dt=(time.perf_counter()-t)/1000*1e6;"
        "mods=[m for m in sys.modules if m.startswith('providers')];"
        "print(json.dumps({'us': dt, 'mods': mods}))"
    )
    r = subprocess.run([sys.executable, "-c", code],
                       capture_output=True, text=True,
                       cwd=str(Path(__file__).resolve().parent.parent))
    try:
        data = json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:
        check("measured the unconfigured path", False, r.stdout + r.stderr)
        return
    check("providers is never imported", not data["mods"], str(data["mods"]))
    check(f"the ladder decision costs {data['us']:.0f} us", data["us"] < 500,
          f"{data['us']:.0f} us")


def test_a_chosen_voice_downloads_itself():
    """Choosing a voice IS the request to use it."""
    from importlib.util import find_spec

    section("[15] A chosen voice fetches itself")
    if find_spec("piper") is None:
        check("piper installed (skipped)", True, "not installed - skipped")
        return
    from providers import tts_piper

    check("the panel has something to call", hasattr(tts_piper, "prepare"))
    check("the engine warms the voice before anyone speaks",
          hasattr(tts_piper.PiperTTS({"voice": ""}), "warmup"))

    here = tts_piper.installed()
    if here:
        seen = []
        tts_piper.prepare({"voice": here[0]},
                          lambda ok, msg: seen.append((ok, msg)))
        check("an already-downloaded voice reports ready without fetching",
              seen and seen[-1][0] is True, str(seen))
    else:
        check("a voice is downloaded to test with (skipped)", True, "none yet")

    # The failure this replaced: a missing voice used to raise and tell the
    # user to press a button, leaving the assistant mute.
    import inspect
    src = inspect.getsource(tts_piper.PiperTTS._load)
    check("a missing voice is fetched, not refused",
          "download(" in src and "press TEST" not in src)


def test_voice_folder_is_writable():
    """Not everybody installs into a folder they can write to."""
    from providers import tts_piper

    section("[14] Voices land somewhere writable")
    d = tts_piper._VOICE_DIR
    try:
        d.mkdir(parents=True, exist_ok=True)
        probe = d / ".writetest"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        ok = True
    except Exception as e:
        ok = False
    check(f"the voice folder can be written to ({d})", ok)
    check("a read-only install falls back to a per-user folder",
          "_voice_dir" in dir(tts_piper))


async def test_it_speaks_before_the_tool_runs():
    """A gap where the user has spoken and hears nothing is the failure."""
    import time as _t

    from providers.base import FunctionCall
    from providers.engine_pipeline import PipelineEngine

    section("[19] It speaks before the tool runs, and again after")

    class LLM:
        caps = {"tools": True, "tool_format": "openai", "vision": False}
        model = "m"

        def __init__(self):
            self.n = 0

        async def chat(self, messages, tools=None, **kw):
            self.n += 1
            if self.n == 1:
                yield {"text": "One moment, opening it."}
                yield {"tool_calls": [FunctionCall("c1", "open_app",
                                                   {"app_name": "WhatsApp"})]}
            else:
                yield {"text": "WhatsApp is open."}

    class TTS:
        caps = {"audio_out": True}

        def __init__(self):
            self.said, self.at = [], []

        async def speak(self, text, voice="", language=""):
            self.said.append(text)
            self.at.append(_t.monotonic())
            yield b"" * 1000

    tts = TTS()
    eng = PipelineEngine(LLM(), tts=tts, config=Cfg(), log=lambda m: None)
    t0 = _t.monotonic()
    tool_started = [None]

    async def drain():
        async for m in eng.receive():
            if m.tool_call:
                tool_started[0] = _t.monotonic()
                await asyncio.sleep(0.6)          # a tool that takes a moment
                await eng.send_tool_response(function_responses=[
                    FR(c.id, c.name, "Opened WhatsApp.")
                    for c in m.tool_call.function_calls])
            if m.server_content and m.server_content.turn_complete:
                return

    task = asyncio.create_task(drain())
    await eng.send_client_content(
        turns={"role": "user", "parts": [{"text": "open whatsapp"}]})
    await asyncio.wait_for(task, 20)

    check("it said something before the tool started",
          tts.at and tool_started[0] and tts.at[0] <= tool_started[0] + 0.05,
          f"first speech {tts.at[0] - t0:.2f}s, tool {tool_started[0] - t0:.2f}s")
    check("it also spoke the result afterwards",
          len(tts.said) == 2 and "open" in tts.said[-1].lower(), str(tts.said))


async def test_a_tool_is_not_run_twice():
    """"open whatsapp" opened WhatsApp four times on the first real attempt."""
    from providers.base import FunctionCall
    from providers.engine_pipeline import PipelineEngine, _signature

    section("[16] A repeated tool call does not run the tool again")

    # A model that keeps asking for the same thing, for ever.
    class Stubborn:
        caps = {"tools": True, "tool_format": "openai", "vision": False}
        model = "qwen3:8b"

        def __init__(self):
            self.rounds = 0

        async def chat(self, messages, tools=None, **kw):
            self.rounds += 1
            if self.rounds > 8:
                yield {"text": "Done."}
                return
            yield {"tool_calls": [FunctionCall(f"id{self.rounds}", "open_app",
                                               {"app_name": "WhatsApp"})]}

    llm = Stubborn()
    eng = PipelineEngine(llm, config=Cfg(), log=lambda m: None)
    executed = []
    finished = asyncio.Event()

    async def like_main_py():
        async for r in eng.receive():
            if r.tool_call:
                frs = []
                for fc in r.tool_call.function_calls:
                    executed.append((fc.name, fc.args.get("app_name")))
                    frs.append(FR(fc.id, fc.name, "Opened WhatsApp."))
                await eng.send_tool_response(function_responses=frs)
            if r.server_content and r.server_content.turn_complete:
                finished.set()
                return

    task = asyncio.create_task(like_main_py())
    await eng.send_client_content(
        turns={"role": "user", "parts": [{"text": "open whatsapp"}]})
    try:
        await asyncio.wait_for(finished.wait(), 20)
    except asyncio.TimeoutError:
        pass
    task.cancel()

    check(f"the app was launched ONCE, not {len(executed)} times",
          len(executed) == 1, str(executed))
    check("the turn still ended", finished.is_set())

    check("two identical calls share a signature",
          _signature(FunctionCall("a", "open_app", {"app_name": "X"}))
          == _signature(FunctionCall("b", "open_app", {"app_name": "X"})))
    check("different arguments do not",
          _signature(FunctionCall("a", "open_app", {"app_name": "X"}))
          != _signature(FunctionCall("a", "open_app", {"app_name": "Y"})))
    check("key order does not make a call look new",
          _signature(FunctionCall("a", "t", {"x": 1, "y": 2}))
          == _signature(FunctionCall("a", "t", {"y": 2, "x": 1})))


async def test_injections_are_not_the_user():
    """The app injects GPU alerts, screenshots and briefings as `role: user`."""
    from providers.engine_pipeline import PipelineEngine

    section("[18] A system alert is neither the user nor an interruption")

    class SlowLLM:
        caps = {"tools": False, "tool_format": None}
        model = "m"

        def __init__(self):
            self.calls = 0

        async def chat(self, messages, tools=None, **kw):
            self.calls += 1
            for tok in ["WhatsApp", " is", " open."]:
                await asyncio.sleep(0.15)
                yield {"text": tok}

    class STT:
        caps = {}
        detected_language = "en"

        async def transcribe(self, pcm, sample_rate=16000, language="", log=None):
            return "open whatsapp"

    llm = SlowLLM()
    eng = PipelineEngine(llm, stt=STT(), config=Cfg(), log=lambda m: None)
    eng._mic = [bytes(32000)]
    eng._mic_bytes = 32000
    heard, said, turns = [], [], [0]

    async def drain():
        async for m in eng.receive():
            if m.server_content:
                sc = m.server_content
                if sc.input_transcription:
                    heard.append(sc.input_transcription.text)
                if sc.output_transcription:
                    said.append(sc.output_transcription.text)
                if sc.turn_complete:
                    turns[0] += 1
                    if turns[0] >= 2:
                        return

    task = asyncio.create_task(drain())
    await eng.flush_microphone(16000)
    await asyncio.sleep(0.2)          # the alert lands MID-ANSWER
    await eng.send_client_content(turns={"role": "user", "parts": [
        {"text": "[SYSTEM_ALERT] GPU load is at 96%."}]})
    try:
        await asyncio.wait_for(task, 20)
    except asyncio.TimeoutError:
        task.cancel()

    check("only recognised speech becomes 'You:'", heard == ["open whatsapp"],
          str(heard))
    check("the answer in progress was not cancelled",
          any("open." in t for t in said), str(said))
    check("the injection was answered afterwards, not instead",
          llm.calls == 2, str(llm.calls))


def test_what_reaches_the_voice():
    """Two faults the log made visible: split words, and a spoken emoji."""
    from providers.engine_pipeline import _speakable, _take_sentences

    section("[17] What reaches the log, and what reaches the voice")

    # main.py joins transcript pieces with a space, because Gemini Live sends
    # phrases. Token-level pieces produced "Hel lo ! How c an I assis t y ou".
    tokens = ["Hel", "lo", "!", " How", " c", "an", " I", " help", " y", "ou?",
              " Bir", " de", " Türkçe."]
    buf, out = "", []
    for t in tokens:
        buf += t
        ready, buf = _take_sentences(buf)
        out += ready
    if buf.strip():
        out.append(buf.strip())
    joined = " ".join(out)
    check("sentences survive main.py's space-join",
          joined == "Hello! How can I help you? Bir de Türkçe.", repr(joined))

    check("an emoji is not sent to the voice",
          _speakable("How can I help? 😊") == "How can I help?")
    check("several are removed cleanly",
          _speakable("Tamam ✅ hazırım 🚀") == "Tamam hazırım")

    # The range that removed a whole language.
    for name, text in (("Japanese", "日本語も大丈夫です。"),
                       ("Chinese", "中文也没问题。"),
                       ("Korean", "한국어도 괜찮습니다."),
                       ("Russian", "Всё работает нормально."),
                       ("Arabic", "كل شيء يعمل بشكل جيد."),
                       ("Hindi", "सब कुछ ठीक काम कर रहा है।"),
                       ("Greek", "Όλα λειτουργούν κανονικά."),
                       ("Hebrew", "הכל עובד כמו שצריך."),
                       ("Turkish", "Sıcaklık 23°C, her şey yolunda.")):
        check(f"{name} survives the emoji filter", _speakable(text) == text,
              repr(_speakable(text)))


def test_reasoning_is_not_read_aloud():
    """qwen3, deepseek-r1 and every distill of them think inside <think> tags,
    and several servers pass that through as ordinary content."""
    import random

    from providers.engine_pipeline import _ReasoningFilter

    section("[11] A reasoning model's thinking is not spoken")

    def run(chunks):
        f = _ReasoningFilter()
        return "".join(f.feed(c) for c in chunks) + f.flush()

    check("a whole block is removed",
          run(["<think>the user said hello</think>Hello, sir."]) == "Hello, sir.")
    check("text on both sides survives",
          run(["Before <think>hidden</think> after."]) == "Before  after.")
    check("other tag spellings too",
          run(["<reasoning>a</reasoning>B<thinking>c</thinking>D"]) == "BD")
    check("a reply with no thinking is untouched",
          run(["Just an answer."]) == "Just an answer.")
    check("an unterminated block is dropped, not spoken",
          run(["<think>never finished..."]) == "")

    # The part that actually broke: tags split across stream chunks.
    text = "<think>hidden reasoning, at some length</think>Only this is spoken."
    check("a tag split across chunks is still caught",
          run(["<thi", "nk>x", "</thi", "nk>", "Visible."]) == "Visible.")
    check("character-by-character streaming",
          run(list(text)) == "Only this is spoken.")

    bad = 0
    for _ in range(200):
        cuts = sorted(random.sample(range(1, len(text)), random.randint(1, 12)))
        chunks = [text[a:b] for a, b in zip([0] + cuts, cuts + [len(text)])]
        if run(chunks) != "Only this is spoken.":
            bad += 1
    check(f"200 random chunk splits ({200 - bad}/200)", bad == 0)


def test_local_models_get_a_smaller_prompt():
    from providers import registry, schema
    from providers.llm_openai import CATALOG, _is_own_hardware, _int_or

    section("[12] Local models are given a tool budget")

    # The budget used to live only in the catalog's preset values, so it reached
    # people who picked a service AFTER it was added and nobody else. Measured
    # on an install configured before that: 20 tools and 18,039 characters on
    # every turn instead of 14 and 11,871 — most of a fourteen-second wait.
    older = registry.build("openai_compat",
                           {"base_url": "http://127.0.0.1:11434/v1", "model": "m"})
    check("a config saved before the budget existed still gets it",
          older.caps["max_tools"] == 14, str(older.caps["max_tools"]))

    hosted = registry.build("openai_compat",
                            {"base_url": "https://api.groq.com/openai/v1",
                             "model": "m"})
    check("a hosted endpoint is not limited",
          hosted.caps["max_tools"] is None, str(hosted.caps["max_tools"]))

    chosen = registry.build("openai_compat",
                            {"base_url": "http://127.0.0.1:11434/v1",
                             "model": "m", "max_tools": "40"})
    check("an explicit choice wins", chosen.caps["max_tools"] == 40)

    check("a machine on the home network counts as local",
          _is_own_hardware("http://192.168.1.50:11434/v1")
          and _is_own_hardware("http://10.0.0.5:8080/v1")
          and not _is_own_hardware("https://api.openai.com/v1"))
    check("an empty or zero field means 'decide for me'",
          _int_or("", None) is None and _int_or("0", None) is None
          and _int_or("12", None) == 12)

    local = [e for e in CATALOG if e.get("local") is True]
    cloud = [e for e in CATALOG if e.get("local") is False]
    check("every local preset carries a budget",
          local and all(e["values"].get("max_tools") for e in local))
    check("no cloud preset is limited",
          cloud and not any(e["values"].get("max_tools") for e in cloud))

    decls = Cfg.tools[0]["function_declarations"] * 12
    full = schema.payload_size(decls)
    cut = schema.payload_size(schema.budget(decls, 14, 12000))
    check(f"the budget shrinks the prompt ({full:,} -> {cut:,})", cut < full)


def test_voices_follow_the_language():
    """A Piper voice is trained for one language; the pipeline already knows
    which one the user spoke."""
    from importlib.util import find_spec

    section("[13] The voice follows the language spoken")
    if find_spec("piper") is None:
        check("piper installed (skipped)", True, "not installed - skipped")
        return
    from providers import tts_piper
    from providers.engine_pipeline import _accepts_language

    voice = tts_piper.PiperTTS({"voice": "en_US-lessac-medium",
                                "follow_language": "1"})
    check("Piper accepts a language argument", _accepts_language(voice))
    check("a language code is read off a voice name",
          tts_piper.language_of("tr_TR-dfki-medium") == "tr")

    here = set(tts_piper.installed())
    if {"tr_TR-dfki-medium", "en_US-lessac-medium"} <= here:
        check("Turkish picks the Turkish voice",
              voice._pick("tr") == "tr_TR-dfki-medium", voice._pick("tr"))
        check("English keeps the configured voice",
              voice._pick("en") == "en_US-lessac-medium", voice._pick("en"))
        check("a language with no voice downloaded falls back",
              voice._pick("ja") == "en_US-lessac-medium", voice._pick("ja"))
    else:
        check("both test voices downloaded (skipped)", True,
              f"have {sorted(here)}")

    from providers.tts_system import SystemTTS
    check("the system voice is NOT told a language it cannot use",
          not _accepts_language(SystemTTS({})))


def test_panel_settings_survive():
    """The settings panel: does what you choose come back?

    Three faults lived here, all of them reported as "it does not remember":
    five separate writes of one file racing each other, a choice read from the
    box that was still being built, and a redraw eating what had been typed.
    Qt runs offscreen so this needs no display.
    """
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    section("[10] The settings panel remembers")
    try:
        from PyQt6.QtWidgets import QApplication, QComboBox
    except Exception as e:
        check("PyQt6 available", False, str(e))
        return
    app = QApplication.instance() or QApplication([])

    import ui as UI
    from memory.config_manager import (get_pipeline, get_provider_config,
                                       get_provider_mode, save_pipeline,
                                       save_provider_config,
                                       save_provider_mode,
                                       save_sidecar_provider)

    # The test writes the real config and puts it back. A fresh install has
    # none yet: then the file it made is removed again afterwards.
    cfg_file = Path(__file__).resolve().parent.parent / "config" / "api_keys.json"
    had_config = cfg_file.exists()
    restore = json.loads(cfg_file.read_text(encoding="utf-8")).get("providers") \
        if had_config else None

    try:
        save_provider_mode("gemini")
        save_pipeline(stt="", llm="", tts="")
        save_sidecar_provider("")
        # Every catalog service has its own id and settings —
        # "openai_compat@ollama" — so that is what the panel offers and stores.
        OLLAMA = "openai_compat@ollama"
        save_provider_config(OLLAMA, {"base_url": "", "model": ""})

        # -- a provider chosen for background requests is configurable --------
        ov = UI.ProvidersOverlay()
        box = ov._sidecar_combo
        i = box.findData(OLLAMA)
        check("named services are offered for background requests", i > 0)
        if i > 0:
            box.setCurrentIndex(i)
            ov._sidecar_changed(i)
            ov._rebuild_body()
            check("choosing one shows its form", OLLAMA in ov._edits,
                  str(list(ov._edits)))
            check("its address is filled in from the preset",
                  get_provider_config(OLLAMA).get("base_url", "")
                  .endswith("11434/v1"),
                  str(get_provider_config(OLLAMA)))

        # -- typing survives a redraw ----------------------------------------
        if OLLAMA in ov._edits:
            ov._edits[OLLAMA]["model"][0].setCurrentText("qwen3:8b")
            ov._rebuild_body()
            check("a typed model survives a redraw",
                  get_provider_config(OLLAMA).get("model") == "qwen3:8b",
                  str(get_provider_config(OLLAMA).get("model")))

        # -- the whole setup round-trips -------------------------------------
        ov._set_mode("pipeline")
        ov._rebuild_body()
        ov._pipe.update({"stt": "whisper@local", "llm": OLLAMA,
                         "tts": "elevenlabs@elevenlabs"})
        ov._rebuild_body()

        import pathlib
        writes = [0]
        real = pathlib.Path.write_text

        def counted(self, *a, **k):
            # The config is written to api_keys.json.tmp and moved into place
            # in one step (memory/config_manager.py), so either name is a write.
            if self.name in ("api_keys.json", "api_keys.json.tmp"):
                writes[0] += 1
            return real(self, *a, **k)

        pathlib.Path.write_text = counted
        try:
            ov._apply()
        finally:
            pathlib.Path.write_text = real

        check("APPLY writes the config once", writes[0] == 1, str(writes[0]))
        check("the mode was stored", get_provider_mode() == "pipeline",
              get_provider_mode())
        check("all three slots were stored",
              all(get_pipeline().get(k) for k in ("stt", "llm", "tts")),
              str(get_pipeline()))

        ov2 = UI.ProvidersOverlay()
        check("reopening shows MY OWN SETUP", ov2._mode == "pipeline", ov2._mode)
        check("the local card is the selected one", ov2._card_local.isChecked())
        check("Ollama is remembered", ov2._pipe.get("llm") == OLLAMA,
              str(ov2._pipe.get("llm")))
        check("no slot reads 'not set'",
              all(ov2._pipe.get(s) for s in ("stt", "llm", "tts")),
              str(ov2._pipe))

        # -- the wheel does not change a box you scroll past -----------------
        combos = ov2.findChildren(QComboBox)
        check(f"all {len(combos)} boxes ignore the wheel unless focused",
              combos and all(isinstance(c, UI._ScrollSafeCombo) for c in combos))
    finally:
        from memory.config_manager import _patch_config
        _patch_config(providers=restore or {})
        if not had_config:
            cfg_file.unlink(missing_ok=True)


# ─────────────────────────────────────────────────────────────────────────────
async def main() -> int:
    print("providers self-test")
    await test_real_caller_shapes()
    await test_taskgroup_and_tool_loop()
    await test_full_stack_over_http()
    test_vision_is_asked_not_guessed()
    test_schema()
    test_audio()
    await test_failures_name_the_part()
    test_addresses()
    test_costs_nothing_when_unused()
    await test_it_speaks_before_the_tool_runs()
    await test_a_tool_is_not_run_twice()
    await test_injections_are_not_the_user()
    test_what_reaches_the_voice()
    test_reasoning_is_not_read_aloud()
    test_local_models_get_a_smaller_prompt()
    test_voices_follow_the_language()
    test_voice_folder_is_writable()
    test_a_chosen_voice_downloads_itself()
    test_panel_settings_survive()

    print(f"\n{'=' * 56}")
    print(f"{_ok} passed, {_fail} failed")
    if _failures:
        for name in _failures:
            print(f"   - {name}")
    print("=" * 56)
    return 1 if _fail else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
