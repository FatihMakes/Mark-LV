"""Headless policy and local pairing client; no desktop/audio imports."""
import argparse
import copy
import json
import os
import socket
from pathlib import Path


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
    # Import exactly the two trusted plugins. Do not discover/import unused
    # desktop actions or arbitrary plugins merely to hide their declarations
    # afterward — the headless allowlist is explicit, never permissive.
    from core.plugin_loader import PluginRegistry, PluginRecord
    from plugins import home_assistant
    from plugins import spotify_control

    def _runner(module):
        def _invoke(parameters, player=None, session_memory=None):
            return module.run(parameters, player=player, session_memory=session_memory)
        return _invoke

    records = []
    for module in (home_assistant, spotify_control):
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


def request_pair(path=None) -> str:
    """Ask the running assistant, never create a disconnected dashboard."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(5)
        sock.connect(str(path or control_path()))
        sock.sendall(b'pair\n')
        result = b''
        while b'\n' not in result and len(result) < 4096:
            chunk = sock.recv(4096)
            if not chunk:
                break
            result += chunk
    reply = json.loads(result)
    if 'url' not in reply:
        raise RuntimeError(reply.get('error', 'Pairing failed'))
    return reply['url']


CONTROL_MAX_PAYLOAD = 4096
CONTROL_TIMEOUT = 2


def request_control(payload: dict, path: Path | None = None) -> dict:
    data = json.dumps(payload).encode("utf-8")
    if len(data) > CONTROL_MAX_PAYLOAD:
        raise ValueError("control payload exceeds 4096 bytes")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(CONTROL_TIMEOUT)
        sock.connect(str(path or control_path()))
        sock.sendall(data + b"\n")
        result = b""
        while b"\n" not in result and len(result) <= CONTROL_MAX_PAYLOAD:
            chunk = sock.recv(4096)
            if not chunk:
                break
            result += chunk
        if not result:
            raise RuntimeError("control socket closed before response")
        idx = result.find(b"\n")
        if idx == -1:
            raise RuntimeError("control socket returned malformed response")
        line = result[:idx]
        if len(line) > CONTROL_MAX_PAYLOAD:
            raise RuntimeError("control response exceeded 4096 bytes")
    return json.loads(line.decode("utf-8"))


def main():
    parser = argparse.ArgumentParser(description='Private control of the running JARVIS instance')
    parser.add_argument('command', choices=['pair', 'spotify-auth-start', 'spotify-auth-status'])
    parser.add_argument('--socket', type=Path, default=control_path())
    parser.add_argument('--client-id', dest='client_id', default=None)
    args = parser.parse_args()
    try:
        if args.command == 'pair':
            print(request_pair(args.socket))
        elif args.command == 'spotify-auth-start':
            if not args.client_id:
                parser.exit(1, 'spotify-auth-start requires --client-id\n')
            reply = request_control({'command': 'spotify-auth-start', 'client_id': args.client_id}, args.socket)
            if not reply.get('ok'):
                parser.exit(1, f'Spotify authorization failed: {reply.get("error")}\n')
            print(reply['authorization_url'])
        elif args.command == 'spotify-auth-status':
            reply = request_control({'command': 'spotify-auth-status'}, args.socket)
            if not reply.get('ok'):
                parser.exit(1, f'Spotify status failed: {reply.get("error")}\n')
            print(json.dumps(reply))
    except (OSError, ValueError, RuntimeError) as exc:
        parser.exit(1, f'JARVIS control unavailable: {exc}\n')


if __name__ == '__main__':
    main()
