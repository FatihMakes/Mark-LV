#!/usr/bin/env python3
"""Idempotently provision the dedicated Spotify path, without restarting audio."""
import argparse
import json
import shlex
import subprocess

ANALOG_SINK = 'alsa_output.pci-0000_00_1f.3.analog-stereo'


def pactl(*args):
    return subprocess.run(['pactl', *args], capture_output=True, text=True,
                          check=True, timeout=2).stdout


def ensure_routes(analog_sink=ANALOG_SINK):
    sinks = json.loads(pactl('--format=json', 'list', 'sinks'))
    if analog_sink == 'lacasa_spotify' or not any(s['name'] == analog_sink for s in sinks):
        raise RuntimeError(f'Configured analog sink unavailable: {analog_sink}')
    if not any(s['name'] == 'lacasa_spotify' for s in sinks):
        pactl('load-module', 'module-null-sink', 'sink_name=lacasa_spotify',
              'sink_properties=device.description=LaCasa_Spotify')
    modules = json.loads(pactl('--format=json', 'list', 'modules'))
    for module in modules:
        if module['name'] == 'module-loopback':
            args = dict(part.split('=', 1) for part in shlex.split(module.get('argument') or '')
                        if '=' in part)
            if args.get('source') == 'lacasa_spotify.monitor' and args.get('sink') == analog_sink:
                return
    pactl('load-module', 'module-loopback', 'source=lacasa_spotify.monitor',
          f'sink={analog_sink}', 'latency_msec=40')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--analog-sink', default=ANALOG_SINK)
    ensure_routes(parser.parse_args().analog_sink)
