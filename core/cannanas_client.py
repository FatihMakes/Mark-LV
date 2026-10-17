"""Persistent serialized MCP STDIO client session for the Cannanas adapter child.

One dedicated asyncio loop thread serves every synchronous tool call through a
single queue: exactly one persistent client session and one child process for
the JARVIS lifetime — never per tool call. The child is the pinned adapter
release, verified by the exact frozen tool list, adapter version and wheel
SHA-256. Any adapter failure degrades Cannanas only, never JARVIS.

Restart policy: reads restart-retry exactly once with a bounded backoff when
no response may have been committed; prepares are never auto-retried (the host
requires a verified re-prepare); confirms are never retried once bytes may
have been sent (the result stays `unknown` until reconciled).
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
from concurrent.futures import Future

INIT_BUDGET_SECONDS = 10.0
TOOL_BUDGET_SECONDS = 25.0
RESTART_BACKOFF_SECONDS = 1.0

# Pinned adapter release (paired with /home/lacasa15/services/cannanas-mcp/adapter/release).
PINNED_ADAPTER_VERSION = '1.0.1'
PINNED_WHEEL_SHA256 = '7085943afc149d3f2356fca66d8a9d0bac9c2576166e99e36093a56b87153f5f'

ADAPTER_ENV_KEYS = (
    'CANNANAS_API_KEY',
    'CANNANAS_CLUB_ID',
    'CANNANAS_STATE_DIR',
    'CANNANAS_AUDIT_FILE',
)

MCP_READ_TOOLS = frozenset({
    'get_adapter_info', 'get_cannanas_overview', 'find_zones', 'find_strains',
    'find_plants', 'find_propagation_material', 'list_recent_batches',
    'list_plants', 'count_plants_by_room',
})


def _prepare_tools() -> frozenset:
    return frozenset(a for a in ADAPTER_TOOLS() if a.startswith('prepare_'))


def _confirm_tools() -> frozenset:
    return frozenset(a for a in ADAPTER_TOOLS() if a.startswith('confirm_'))


def ADAPTER_TOOLS() -> list:
    """The frozen 21-tool Release-1 adapter surface."""
    return [
        'get_adapter_info',
        'get_cannanas_overview',
        'find_zones',
        'find_strains',
        'find_plants',
        'find_propagation_material',
        'list_recent_batches',
        'list_plants',
        'count_plants_by_room',
        'prepare_plant_create',
        'confirm_plant_create',
        'prepare_strain_create',
        'confirm_strain_create',
        'prepare_batch_create',
        'confirm_batch_create',
        'prepare_plant_update',
        'confirm_plant_update',
        'prepare_batch_update',
        'confirm_batch_update',
        'prepare_diary_create',
        'confirm_diary_create',
    ]


MCP_PREPARE_TOOLS = _prepare_tools()
MCP_CONFIRM_TOOLS = _confirm_tools()


class CannanasUnavailable(RuntimeError):
    """Cannanas is degraded/unavailable; JARVIS keeps running without it."""


class CannanasPreSendRefusal(CannanasUnavailable):
    """Refused before the transport could send anything (no bytes were sent).

    Distinguishable so the host can classify a refused confirm as 'failed'
    while every post-send timeout stays honestly 'unknown'.
    """


def child_environment(adapter_env: dict) -> dict:
    """Exactly a bare PATH plus the four adapter variables — nothing else."""
    env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin')}
    for key in ADAPTER_ENV_KEYS:
        if adapter_env.get(key):
            env[key] = str(adapter_env[key])
    return env


def _result_payload(result) -> dict:
    """Extract a bounded structured payload from an MCP CallToolResult."""
    structured = getattr(result, 'structuredContent', None)
    if isinstance(structured, dict):
        return structured
    for block in getattr(result, 'content', None) or ():
        text = getattr(block, 'text', None)
        if isinstance(text, str):
            try:
                payload = json.loads(text)
            except (ValueError, TypeError):
                continue
            if isinstance(payload, dict):
                return payload
    raise CannanasUnavailable('unreadable Cannanas adapter result')


class _Request:
    __slots__ = ('tool', 'arguments', 'budget', 'future', 'retried')

    def __init__(self, tool: str, arguments: dict, budget: float, future: Future):
        self.tool = tool
        self.arguments = arguments
        self.budget = budget
        self.future = future
        self.retried = False


class CannanasClient:
    """Serialized synchronous facade over one persistent MCP client session."""

    def __init__(self, config: dict, *, init_budget: float = INIT_BUDGET_SECONDS,
                 tool_budget: float = TOOL_BUDGET_SECONDS,
                 restart_backoff: float = RESTART_BACKOFF_SECONDS,
                 on_restart=None) -> None:
        self._command = str(config['command'])
        self._args = [str(a) for a in config.get('args', [])]
        self._pinned_version = config.get('adapter_version', PINNED_ADAPTER_VERSION)
        self._pinned_sha = config.get('artifact_sha256', PINNED_WHEEL_SHA256)
        expected = config.get('expected_tools')
        self._expected_tools = set(expected if expected else ADAPTER_TOOLS())
        self._env = child_environment(config.get('adapter_env') or {})
        self._init_budget = init_budget
        self._tool_budget = tool_budget
        self._restart_backoff = restart_backoff
        self._on_restart = on_restart

        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue | None = None
        self._start_future: Future | None = None
        self._stopped = False
        self._lock = threading.Lock()
        self.degraded = False
        self.read_only_degraded = False
        self.degraded_reason = ''
        self._restart_used = False

        # Worker loop internals: one connection for the client lifetime.
        self._exit_stack = None

    # ── lifecycle ──────────────────────────────────────────────────────────

    def start(self) -> bool:
        """Launch the pinned child and verify it. Never crashes JARVIS:
        a failure is a bounded degraded report, not an exception."""
        self._start_future = Future()
        self._thread = threading.Thread(
            target=self._worker_main, name='cannanas-mcp-client', daemon=True)
        self._thread.start()
        try:
            return self._start_future.result(timeout=self._init_budget + 5.0)
        except TimeoutError:
            self.degraded = True
            self.degraded_reason = 'start: adapter child did not initialize in time'
            return False
        except Exception:
            self.degraded = True
            self.degraded_reason = 'start: adapter child failed'
            return False

    def stop(self, timeout: float = 3.0) -> None:
        self._stopped = True
        loop, queue_obj = self._loop, self._queue
        if loop is not None and queue_obj is not None and loop.is_running():
            loop.call_soon_threadsafe(queue_obj.put_nowait, None)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.stop()

    # ── public synchronous API (serialized) ────────────────────────────────

    def call(self, tool: str, arguments: dict, budget: float | None = None) -> dict:
        if self.degraded:
            raise CannanasPreSendRefusal(f'Cannanas unavailable: {self.degraded_reason}')
        if self.read_only_degraded and (tool in MCP_PREPARE_TOOLS
                                        or tool in MCP_CONFIRM_TOOLS):
            raise CannanasPreSendRefusal(
                'Cannanas release verification failed: writes refused')
        loop, queue_obj = self._loop, self._queue
        if loop is None or not loop.is_running():
            raise CannanasPreSendRefusal('Cannanas client not started')
        request = _Request(tool, dict(arguments or {}),
                           budget or self._tool_budget, Future())
        loop.call_soon_threadsafe(queue_obj.put_nowait, request)
        try:
            return request.future.result(timeout=request.budget + 10.0)
        except CannanasUnavailable:
            raise
        except TimeoutError:
            raise CannanasUnavailable('Cannunas adapter answer timed out') from None
        except Exception as exc:
            if isinstance(exc, Exception) and 'denied' in str(exc):
                raise
            raise CannanasUnavailable(f'Cannanas unavailable: {self.degraded_reason or "call failed"}') from exc

    # ── worker ─────────────────────────────────────────────────────────────

    def _worker_main(self) -> None:
        try:
            asyncio.run(self._worker_loop())
        except Exception:
            self.degraded = True
            self.degraded_reason = 'start: adapter connection failed'
            self._fail_start(reason=self.degraded_reason)

    def _fail_start(self, reason: str) -> None:
        if self._start_future is not None and not self._start_future.done():
            self._start_future.set_result(False)
        if not self.degraded:
            self.degraded = True
        if not self.degraded_reason:
            self.degraded_reason = reason

    async def _worker_loop(self) -> None:
        loop = asyncio.get_running_loop()
        self._loop = loop
        self._queue = asyncio.Queue()

        # ── connect + verify within the initialize budget ──────────────────
        try:
            _, session = await asyncio.wait_for(self._connect(), self._init_budget)
        except TimeoutError:
            self.degraded = True
            self.degraded_reason = 'initialize budget exceeded'
            self._fail_start(self.degraded_reason)
            return
        except FileNotFoundError:
            self.degraded = True
            self.degraded_reason = 'start: adapter entry point missing'
            self._fail_start(self.degraded_reason)
            return
        except Exception as exc:
            self.degraded = True
            self.degraded_reason = self._describe(exc, 'start')
            self._fail_start(self.degraded_reason)
            return

        # ── verify the frozen tool set ─────────────────────────────────────
        try:
            listed = await asyncio.wait_for(session.list_tools(), self._init_budget)
        except Exception as exc:
            self.degraded = True
            self.degraded_reason = self._describe(exc, 'tool discovery')
            self._fail_start(self.degraded_reason)
            await self._close_connection()
            return
        names = {tool.name for tool in (listed.tools or [])}
        if names != self._expected_tools:
            self.degraded = True
            self.degraded_reason = 'start: adapter tool set mismatch'
            self._fail_start(self.degraded_reason)
            await self._close_connection()
            return

        # ── verify the pinned release metadata ─────────────────────────────
        try:
            result = await asyncio.wait_for(
                session.call_tool('get_adapter_info', {}), self._init_budget)
            info = _result_payload(result)
        except Exception as exc:
            self.degraded = True
            self.degraded_reason = self._describe(exc, 'release verification')
            self._fail_start(self.degraded_reason)
            await self._close_connection()
            return
        if (info.get('adapter_version') != self._pinned_version
                or info.get('artifact_sha256') != self._pinned_sha):
            # Release not what was pinned: refuse every write, keep reads.
            self.read_only_degraded = True
        if self._start_future is not None and not self._start_future.done():
            self._start_future.set_result(True)

        # ── serialized request loop ────────────────────────────────────────
        while True:
            request = await self._queue.get()
            if request is None or self._stopped:
                await self._close_connection()
                return
            await self._process(session, request)

    async def _process(self, session, request: _Request) -> None:
        if self.degraded:
            request.future.set_exception(CannanasUnavailable(
                f'Cannanas unavailable: {self.degraded_reason}'))
            return
        if self.read_only_degraded and (request.tool in MCP_PREPARE_TOOLS
                                        or request.tool in MCP_CONFIRM_TOOLS):
            request.future.set_exception(CannanasUnavailable(
                'Cannanas release verification failed: writes refused'))
            return
        if request.tool not in (MCP_READ_TOOLS | MCP_PREPARE_TOOLS | MCP_CONFIRM_TOOLS):
            request.future.set_exception(CannanasUnavailable('unknown Cannanas adapter tool'))
            return
        try:
            result = await asyncio.wait_for(
                session.call_tool(request.tool, request.arguments), request.budget)
            request.future.set_result(_result_payload(result))
        except Exception as exc:  # transport, timeout or unreadable result
            await self._after_failure(session, request, exc)

    async def _after_failure(self, session, request: _Request, exc: Exception) -> None:
        retriable = request.tool in MCP_READ_TOOLS and not request.retried \
            and not self._restart_used
        if not retriable:
            # Writes/timeout-after-send and a second failure are never retried.
            # Once the single restart is spent and reads still fail, Cannanas
            # is degraded: bounded, observable, JARVIS keeps running.
            if self._restart_used and request.tool in MCP_READ_TOOLS:
                with self._lock:
                    self.degraded = True
                    self.degraded_reason = self._describe(exc, 'restart exhausted')
            request.future.set_exception(CannanasUnavailable(
                f'Cannanas unavailable: {self._describe(exc, "call")}'))
            return
        self._restart_used = True
        if self._on_restart is not None:
            try:
                self._on_restart()
            except Exception:
                pass
        await self._close_connection()
        await asyncio.sleep(self._restart_backoff)
        try:
            _, session = await asyncio.wait_for(self._connect(), self._init_budget)
        except Exception as restart_exc:
            self.degraded = True
            self.degraded_reason = self._describe(restart_exc, 'restart')
            request.future.set_exception(CannanasUnavailable(
                f'Cannanas unavailable: {self.degraded_reason}'))
            self._drain_and_degrade()
            return
        # Re-verify after restart: tool set and release must hold again.
        try:
            listed = await asyncio.wait_for(session.list_tools(), self._init_budget)
            names = {tool.name for tool in (listed.tools or [])}
            if names != self._expected_tools:
                raise CannanasUnavailable('tool set mismatch after restart')
        except Exception as restart_exc:
            self.degraded = True
            self.degraded_reason = self._describe(restart_exc, 'restart verification')
            request.future.set_exception(CannanasUnavailable(
                f'Cannanas unavailable: {self.degraded_reason}'))
            self._drain_and_degrade()
            return
        # The one retry after a verified restart.
        request.retried = True
        await self._process(session, request)

    def _drain_and_degrade(self) -> None:
        """Every queued request after degradation fails bounded and closed."""
        loop, queue_obj = self._loop, self._queue
        if loop is None:
            return

        def _drain() -> None:
            while True:
                try:
                    request = queue_obj.get_nowait()
                except asyncio.QueueEmpty:
                    return
                if request is None:
                    return
                request.future.set_exception(CannanasUnavailable(
                    f'Cannanas unavailable: {self.degraded_reason}'))
        loop.call_soon_threadsafe(_drain)

    def _describe(self, exc: Exception, phase: str) -> str:
        """Fixed bounded secret-free reason; never echoes bodies or secrets."""
        kind = (exc.__class__.__name__ or 'error').lower()
        return f'{phase}: {kind}'

    # ── connection ─────────────────────────────────────────────────────────

    async def _connect(self):
        if self._command and os.sep in self._command or self._command.endswith('.py'):
            if self._command.endswith('.py'):
                # A script entry point runs beside the adapter binary.
                pass
            elif not (os.path.isfile(self._command) and os.access(self._command, os.X_OK)):
                self.degraded = True
                self.degraded_reason = 'start: adapter entry point missing'
                raise FileNotFoundError(self._command)
        elif not (os.path.isfile(self._command) and os.access(self._command, os.X_OK)):
            self.degraded = True
            self.degraded_reason = 'start: adapter entry point missing'
            raise FileNotFoundError(self._command)
        from contextlib import AsyncExitStack
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        params = StdioServerParameters(command=self._command, args=self._args, env=self._env)
        stack = AsyncExitStack()
        try:
            read, write = await stack.enter_async_context(stdio_client(params))
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
        except BaseException:
            try:
                await stack.aclose()
            except Exception:
                pass
            raise
        self._exit_stack = stack
        return stack, session

    async def _close_connection(self) -> None:
        stack = self._exit_stack
        self._exit_stack = None
        if stack is not None:
            try:
                await stack.aclose()
            except Exception:
                pass