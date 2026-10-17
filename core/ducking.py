"""Independent Spotify sink envelope and a separate-process recovery guard.

Only the dedicated sink is touched, never a sink input, master or receiver.
Journal/lock contain gain and process identity only; they live in XDG runtime.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid


@dataclass(frozen=True)
class Gain:
    volumes: tuple[int, ...]
    mute: bool

    def __post_init__(self):
        if (not self.volumes or len(self.volumes) > 32
                or any(type(v) is not int or not 0 <= v <= 0xffffffff for v in self.volumes)
                or type(self.mute) is not bool):
            raise ValueError('Invalid channel gain/mute')


class PulseSinkGain:
    def __init__(self, sink_name: str = 'lacasa_spotify'):
        if not sink_name or sink_name.startswith('-'):
            raise ValueError('A stable sink name is required')
        self.sink_name = sink_name

    @staticmethod
    def _run(*args):
        return subprocess.run(['pactl', *args], check=True, capture_output=True,
                              text=True, timeout=1.0).stdout

    def read(self) -> Gain:
        for sink in json.loads(self._run('--format=json', 'list', 'sinks')):
            if sink['name'] == self.sink_name:
                return Gain(tuple(v['value'] for v in sink['volume'].values()), sink['mute'])
        raise RuntimeError(f'Spotify gain sink {self.sink_name!r} unavailable')

    def write(self, gain: Gain):
        self._run('set-sink-volume', self.sink_name, *(str(v) for v in gain.volumes))
        self._run('set-sink-mute', self.sink_name, '1' if gain.mute else '0')


def _process_identity(pid: int) -> str:
    # /proc stat comm can contain spaces and parentheses. Field 22 = starttime.
    fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
    if fields[0] == 'Z':
        raise ProcessLookupError('Zombie owner')
    return fields[19]


def owner_alive(state: dict) -> bool:
    try:
        return _process_identity(state['pid']) == state['process_start']
    except (OSError, ValueError, IndexError):
        return False


def default_state_path() -> Path:
    runtime = os.environ.get('XDG_RUNTIME_DIR', f'/run/user/{os.getuid()}')
    return Path(runtime) / 'jarvis-ducking' / 'state.json'


class DuckingController:
    """Single output owner, idempotent begin/end, generation-fenced recovery.

    Every read/modify/write AND mixer operation shares one flock with guard.
    A contender cannot capture ducked gain, and an old owner cannot release a
    restarted owner's envelope. Failures retain the original recovery journal.
    """
    def __init__(self, gain=None, state_path=None, clock=time.monotonic,
                 reduction_db=18.0, ramp_seconds=0.08, lease_seconds=6.0):
        self.gain = gain if gain is not None else PulseSinkGain()
        self.state_path = Path(state_path) if state_path else default_state_path()
        self.clock = clock
        self.reduction_db = float(reduction_db)
        self.ramp_seconds = float(ramp_seconds)
        self.lease_seconds = float(lease_seconds)
        if not (math.isfinite(self.reduction_db) and 0 <= self.reduction_db <= 60
                and math.isfinite(self.ramp_seconds) and 0 <= self.ramp_seconds <= .5
                and math.isfinite(self.lease_seconds) and 1 <= self.lease_seconds <= 60):
            raise ValueError('Unsafe ducking/lease/ramp configuration')
        self.token = None
        self.error = None
        self.sink_name = getattr(self.gain, 'sink_name', 'lacasa_spotify')

    @contextmanager
    def _lock(self):
        import fcntl
        directory = self.state_path.parent
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        st = directory.lstat()
        if directory.is_symlink() or st.st_uid != os.getuid() or st.st_mode & 0o077:
            raise PermissionError('Ducking runtime directory must be private and user-owned')
        fd = os.open(str(self.state_path) + '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            deadline = time.monotonic() + 1.0
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError('Ducking journal lock busy')
                    time.sleep(.01)
            yield
        finally:
            os.close(fd)

    def _read(self):
        try:
            fd = os.open(self.state_path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return None
        with os.fdopen(fd) as f:
            state = json.load(f)
        try:
            Gain(tuple(state['baseline']['volumes']), state['baseline']['mute'])
            if (state['version'] != 1 or state['sink'] != self.sink_name
                    or not isinstance(state['generation'], str) or not state['generation']
                    or type(state['pid']) is not int or state['pid'] <= 0
                    or not isinstance(state['process_start'], str)
                    or any(not isinstance(state[k], (int, float)) or not math.isfinite(state[k])
                           for k in ('heartbeat', 'expires', 'not_before'))):
                raise ValueError('Invalid ownership journal')
        except (KeyError, TypeError) as exc:
            raise ValueError('Malformed ducking journal; refusing guessed recovery') from exc
        return state

    def _save(self, state):
        fd, name = tempfile.mkstemp(prefix='.gain-', dir=self.state_path.parent)
        try:
            with os.fdopen(fd, 'w') as f:
                json.dump(state, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(name, self.state_path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def _ramp(self, target: Gain):
        current = self.gain.read()
        if current == target:
            return
        if len(current.volumes) != len(target.volumes):
            raise RuntimeError('Spotify sink channel layout changed; refusing guessed restoration')
        steps = 4 if self.ramp_seconds else 1
        for step in range(1, steps + 1):
            values = tuple(round(a + (b - a) * step / steps)
                           for a, b in zip(current.volumes, target.volumes))
            self.gain.write(Gain(values, target.mute))
            if step < steps:
                time.sleep(self.ramp_seconds / (steps - 1))
        if self.gain.read() != target:
            raise RuntimeError('Spotify sink gain readback mismatch')

    def _restore(self, state):
        self._ramp(Gain(tuple(state['baseline']['volumes']), state['baseline']['mute']))
        self.state_path.unlink()

    def _recover_locked(self, state, force=False):
        now = self.clock()
        if (not force and (now < state['not_before']
                           or (owner_alive(state) and now < state['expires']))):
            return False
        self._restore(state)
        return True

    def _failure(self, exc):
        self.error = f'{type(exc).__name__}: {exc}'
        return False

    def _attenuated(self, baseline):
        # Pulse raw values are cubic: -18 dB acoustic => 10**(-18/60).
        scale = 10 ** (-self.reduction_db / 60)
        return Gain(tuple(round(v * scale) for v in baseline.volumes), baseline.mute)

    def begin(self) -> bool:
        self.error = None
        try:
            with self._lock():
                state = self._read()
                if state and state['generation'] == self.token:
                    self._refresh(state, 0)
                    # Owning a journal is not proof a previous mixer operation
                    # succeeded. Retry from its ORIGINAL baseline, never current.
                    baseline = Gain(tuple(state['baseline']['volumes']), state['baseline']['mute'])
                    self._ramp(self._attenuated(baseline))
                    return True
                if state and not self._recover_locked(state):
                    raise RuntimeError('Spotify ducking already owned by another output phase')
                baseline = self.gain.read()
                now = self.clock()
                token = uuid.uuid4().hex
                state = dict(version=1, sink=self.sink_name, generation=token,
                             pid=os.getpid(), process_start=_process_identity(os.getpid()),
                             heartbeat=now, expires=now + self.lease_seconds, not_before=now,
                             baseline=dict(volumes=baseline.volumes, mute=baseline.mute))
                self._save(state)  # MUST precede the first mixer write, including partial ramps.
                self.token = token
                self._ramp(self._attenuated(baseline))
                return True
        except Exception as exc:
            return self._failure(exc)

    def _refresh(self, state, hold_for):
        now = self.clock()
        state.update(heartbeat=now, expires=now + self.lease_seconds,
                     not_before=max(state['not_before'], now + hold_for))
        self._save(state)

    def heartbeat(self, hold_for=0.0) -> bool:
        expected_token = self.token
        if not expected_token:
            return False
        self.error = None
        try:
            if not math.isfinite(hold_for) or not 0 <= hold_for <= 120:
                raise ValueError('Invalid buffered playback duration')
            with self._lock():
                # A heartbeat can pass the pre-lock ownership check and then wait
                # behind this controller's normal end().  That completed release
                # is not a lost lease; this stale heartbeat must not touch a newer
                # generation started while it waited.
                if self.token != expected_token:
                    return True
                state = self._read()
                if state and state['generation'] == expected_token:
                    self._refresh(state, hold_for)
                    return True
                raise RuntimeError('Spotify ducking lease lost: output phase recovered or replaced')
        except Exception as exc:
            return self._failure(exc)

    def end(self) -> bool:
        try:
            with self._lock():
                state = self._read()
                if state and state['generation'] == self.token:
                    self._restore(state)
                self.token = None
                return True
        except Exception as exc:
            return self._failure(exc)

    def recover(self, force=False) -> bool:
        self.error = None
        try:
            with self._lock():
                state = self._read()
                return self._recover_locked(state, force) if state else False
        except Exception as exc:
            return self._failure(exc)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('guard', 'restore'))
    parser.add_argument('--state', type=Path, default=default_state_path())
    parser.add_argument('--sink', default='lacasa_spotify')
    parser.add_argument('--once', action='store_true', help='One bounded guard check')
    parser.add_argument('--interval', type=float, default=.5)
    args = parser.parse_args(argv)
    if not math.isfinite(args.interval) or not .05 <= args.interval <= 60:
        parser.error('interval must be between .05 and 60 seconds')
    ctl = DuckingController(PulseSinkGain(args.sink), args.state)
    while True:
        restored = ctl.recover(force=args.command == 'restore')
        if restored:
            print('Spotify sink gain restored', flush=True)
        if ctl.error:
            print(f'Ducking degraded: {ctl.error}', file=sys.stderr, flush=True)
        if args.command == 'restore' or args.once:
            return 1 if ctl.error else 0
        time.sleep(args.interval)


if __name__ == '__main__':
    raise SystemExit(main())
