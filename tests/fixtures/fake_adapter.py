"""Minimal local MCP STDIO fake adapter for client tests.

Never touches the Cannanas API: it speaks the real MCP protocol over STDIO
with a dummy tool set and scriptable test modes. Run as:

    <python> tests/fixtures/fake_adapter.py

Modes come from FAKE_ADAPTER_MODE:
  ok            – normal adapter (default)
  slow-init     – sleeps FAKE_ADAPTER_INIT_DELAY seconds before serving
  slow-tool     – tools/call sleeps FAKE_ADAPTER_TOOL_DELAY seconds
  extra-tool    – exposes one unexpected extra tool
  wrong-version – reports a different adapter version and artifact SHA
  one-shot      – serves the first tools/call, then exits on the second
  exit-before-init – exits without ever answering initialize
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

try:
    from mcp.server.lowlevel import Server
    import mcp.types as mcp_types
    from mcp.server.stdio import stdio_server
except ImportError:  # pragma: no cover - runner venv provides mcp
    raise SystemExit('the mcp package is required for the fake adapter')

MODE = 'ok'
PID = os.getpid()
BOOT_ID = 'fake-boot-1'


def _parse_argv(argv):
    """--mode=NAME switches the test mode; --set KEY=VALUE adjusts knobs."""
    settings = {}
    mode = 'ok'
    for arg in argv[1:]:
        if arg.startswith('--mode='):
            mode = arg.split('=', 1)[1]
        elif arg.startswith('--set:'):
            key, value = arg.split('=', 1)
            settings[key.removeprefix('--set:')] = value
    return mode, settings


ARGV_MODE, ARGV_SETTINGS = _parse_argv(sys.argv)
MODE = ARGV_SETTINGS.get('FAKE_ADAPTER_MODE', ARGV_MODE)
BOOT_ID = ARGV_SETTINGS.get('FAKE_ADAPTER_BOOT_ID', BOOT_ID)

ADAPTER_INFO = {
    'ok': True,
    'adapter_name': 'cannanas-fake-adapter',
    'adapter_version': '1.0.1',
    'artifact_sha256': 'a' * 64,
    'release_pinned': True,
    'config': {'club_id_bound': True, 'boot_id': BOOT_ID},
}

EXPECTED_TOOLS = [
    'get_adapter_info', 'get_cannanas_overview', 'find_zones', 'find_strains',
    'find_plants', 'find_propagation_material', 'list_recent_batches',
    'list_plants', 'count_plants_by_room',
    'prepare_plant_create', 'confirm_plant_create', 'prepare_strain_create',
    'confirm_strain_create', 'prepare_batch_create', 'confirm_batch_create',
    'prepare_plant_update', 'confirm_plant_update', 'prepare_batch_update',
    'confirm_batch_update', 'prepare_diary_create', 'confirm_diary_create',
]

server = Server('cannanas-fake-adapter')
_request_count = 0


@server.list_tools()
async def _list_tools() -> list:
    names = list(EXPECTED_TOOLS)
    if MODE == 'extra-tool':
        names.append('rogue_delete_everything')
    return [mcp_types.Tool(name=name, inputSchema={'type': 'object', 'properties': {}})
            for name in names]


@server.call_tool()
async def _call_tool(name: str, arguments: dict | None) -> dict:
    global _request_count
    if MODE == 'one-shot':
        _request_count += 1
        if _request_count > 2:
            # The handshake consumed one tools/call already; the second
            # business call kills this child mid-session.
            await asyncio.sleep(0.05)
            os._exit(0)
    if name == 'get_adapter_info':
        info = dict(ADAPTER_INFO)
        if MODE == 'wrong-version':
            info['adapter_version'] = '0.9.9'
            info['artifact_sha256'] = 'b' * 64
        return {**info, 'pid': PID}
    delay = float(ARGV_SETTINGS.get('FAKE_ADAPTER_TOOL_DELAY', '0'))
    if delay:
        await asyncio.sleep(delay)
    if name.startswith('confirm_'):
        return {'ok': True, 'pid': PID, 'result_id': 'plant-1'}
    if name.startswith('prepare_'):
        return {'ok': True, 'pid': PID, 'preview': 'Vorschau des Eintrags',
                'payload_hash': 'payload-hash-1', 'confirmation_token': 'opaque-token-1'}
    return {'ok': True, 'pid': PID, 'items': []}


async def main() -> None:
    if MODE == 'slow-init':
        await asyncio.sleep(float(ARGV_SETTINGS.get('FAKE_ADAPTER_INIT_DELAY', '2')))
    if MODE == 'exit-before-init':
        return  # exit(0) without answering initialize
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == '__main__':
    asyncio.run(main())