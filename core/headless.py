"""Headless policy and local pairing client; no desktop/audio imports."""
import argparse
import copy
import json
import os
import socket
from pathlib import Path


CONTROL_MAX_LINE_BYTES = 4096
CONTROL_MAX_RESPONSE_BYTES = 65536


def enabled() -> bool:
    return os.environ.get('JARVIS_HEADLESS', 'true').lower() not in ('0', 'false', 'no', 'off')


def capture_source() -> str:
    source = os.environ.get('JARVIS_CAPTURE_SOURCE', 'phone' if enabled() else 'local')
    if source not in ('phone', 'local'):
        raise ValueError('JARVIS_CAPTURE_SOURCE must be phone or local')
    return source


def tool_allowed(name: str, args: dict) -> bool:
    if name == 'home_assistant' and args.get('action') in ('list', 'query', 'temperature'):
        return True
    if name == 'spotify_control' and args.get('action') in (
            'play', 'pause', 'resume', 'next', 'previous', 'volume', 'current'):
        return True
    if name == 'cannanas':
        # Closed action enum only: confirm/cancel host operations, arbitrary
        # MCP tool names, HTTP verbs and everything else are denied here,
        # before any dispatch. The enum is pinned to the plugin module.
        from plugins.cannanas import CANNANAS_MODEL_ACTIONS
        return args.get('action') in CANNANAS_MODEL_ACTIONS
    return False


def error_text(error):
    if isinstance(error, BaseExceptionGroup):
        return '\n'.join(error_text(child) for child in error.exceptions)
    return str(error)


def output_device(name, hardware):
    """Resolve exact output names serially, without desktop input-rate probes."""
    if not name or name == 'System default':
        return None
    for index, device in enumerate(hardware.query_devices()):
        if device['name'] == name and device.get('max_output_channels', 0) > 0:
            return index
    raise RuntimeError('Configured output device unavailable; refusing headless fallback')


def trusted_headless_registry() -> 'PluginRegistry':
    # Import exactly the trusted plugins. Do not discover/import unused
    # desktop actions or arbitrary plugins merely to hide their declarations
    # afterward — the headless allowlist is explicit, never permissive.
    from core.plugin_loader import PluginRegistry, PluginRecord
    from plugins import home_assistant
    from plugins import spotify_control
    from plugins import cannanas

    def _runner(module):
        def _invoke(parameters, player=None, session_memory=None):
            return module.run(parameters, player=player, session_memory=session_memory)
        return _invoke

    records = []
    for module in (home_assistant, spotify_control, cannanas):
        meta = copy.deepcopy(module.PLUGIN)
        if module is home_assistant:
            meta['description'] = 'Read Home Assistant states and temperatures in °C. Read-only.'
            meta['parameters']['properties']['action'] = {
                'type': 'STRING', 'enum': ['list', 'query', 'temperature']}
            meta['parameters']['properties'].pop('value', None)
        records.append(PluginRecord(
            name=meta['name'],
            description=meta['description'],
            parameters=meta['parameters'],
            run=_runner(module),
            valid=True,
        ))
    return PluginRegistry({r.name: r for r in records}, print)


def drain(queue) -> None:
    if queue is not None:
        while not queue.empty():
            queue.get_nowait()


def control_path() -> Path:
    base = Path(os.environ.get('XDG_RUNTIME_DIR', str(Path.home() / '.cache')))
    return Path(os.environ.get('JARVIS_CONTROL_SOCKET', str(base / 'jarvis' / 'control.sock')))


def _request_line(payload: bytes, path: Path | None = None) -> dict:
    """Use the one-line private control protocol without exposing server errors."""
    if len(payload) > CONTROL_MAX_LINE_BYTES or not payload.endswith(b'\n'):
        raise RuntimeError('Control request failed')
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(2)
        try:
            sock.connect(str(path or control_path()))
            sock.sendall(payload)
        except OSError as exc:
            raise RuntimeError('Control request failed') from None
        result = b''
        try:
            while b'\n' not in result and len(result) <= CONTROL_MAX_RESPONSE_BYTES:
                chunk = sock.recv(min(4096, CONTROL_MAX_RESPONSE_BYTES + 1 - len(result)))
                if not chunk:
                    break
                result += chunk
        except OSError:
            raise RuntimeError('Control request failed') from None
    if (b'\n' not in result or len(result) > CONTROL_MAX_RESPONSE_BYTES
            or result.index(b'\n') + 1 != len(result)):
        raise RuntimeError('Control request failed')
    try:
        reply = json.loads(result)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RuntimeError('Control request failed') from None
    if not isinstance(reply, dict):
        raise RuntimeError('Control request failed')
    return reply


def request_pair(path: Path | None = None) -> str:
    """Ask the running assistant, never create a disconnected dashboard."""
    reply = _request_line(b'pair\n', path)
    if not isinstance(reply.get('url'), str):
        raise RuntimeError(reply.get('error', 'Pairing failed'))
    return reply['url']


def request_control(request: dict, path: Path | None = None) -> dict:
    """Send one bounded JSON control request and return its bounded JSON response."""
    if not isinstance(request, dict):
        raise RuntimeError('Control request failed')
    try:
        payload = json.dumps(request, separators=(',', ':'), ensure_ascii=False).encode('utf-8') + b'\n'
    except (TypeError, ValueError, UnicodeEncodeError):
        raise RuntimeError('Control request failed') from None
    return _request_line(payload, path)


def main():
    parser = argparse.ArgumentParser(description='Private control of the running JARVIS instance')
    commands = parser.add_subparsers(dest='command', required=True)
    pair = commands.add_parser('pair')
    pair.add_argument('--socket', type=Path, default=control_path())
    create = commands.add_parser('device-create')
    create.add_argument('--label', required=True)
    create.add_argument('--socket', type=Path, default=control_path())
    listing = commands.add_parser('device-list')
    listing.add_argument('--socket', type=Path, default=control_path())
    revoke = commands.add_parser('device-revoke')
    revoke.add_argument('device_id')
    revoke.add_argument('--socket', type=Path, default=control_path())
    auth_start = commands.add_parser('spotify-auth-start')
    auth_start.add_argument('--client-id', required=True)
    auth_start.add_argument('--socket', type=Path, default=control_path())
    auth_status = commands.add_parser('spotify-auth-status')
    auth_status.add_argument('--socket', type=Path, default=control_path())
    args = parser.parse_args()
    try:
        if args.command == 'pair':
            print(request_pair(args.socket))  # explicit operator output, not a service log
            return
        request = {'command': args.command}
        if args.command == 'device-create':
            request['label'] = args.label
        elif args.command == 'device-revoke':
            request['device_id'] = args.device_id
        elif args.command == 'spotify-auth-start':
            request['client_id'] = args.client_id
        reply = request_control(request, args.socket)
        if not reply.get('ok'):
            raise RuntimeError('Control request failed')
        if args.command == 'spotify-auth-start':
            print(reply['authorization_url'])
            return
        print(json.dumps(reply, separators=(',', ':'), ensure_ascii=False))
    except (OSError, ValueError, RuntimeError) as exc:
        if args.command == 'pair':
            parser.exit(1, f'JARVIS pairing unavailable: {exc}\n')
        if args.command.startswith('spotify-auth'):
            parser.exit(1, f'JARVIS control unavailable: {exc}\n')
        parser.exit(1, 'JARVIS control unavailable\n')


if __name__ == '__main__':
    main()