# Shared Spotify/JARVIS audio (user session)

Topology, under the same unprivileged account:

```
librespot (own softvol) -> lacasa_spotify (temporary envelope)
                       -> monitor/loopback -> analog output
JARVIS ----------------------------------> analog output
```

The analog name on the tested LaCasa host is
`alsa_output.pci-0000_00_1f.3.analog-stereo`. Configure JARVIS's saved
`output_device` to the exact matching **Pulse** PortAudio device, not ALSA's
possibly digital default. Do not set JARVIS's `PULSE_SINK` to `lacasa_spotify`.
No Spotify Web API credentials are required for ducking.

## Deployment

Requires PipeWire with `pipewire-pulse`, WirePlumber, Pulse tools (`pactl`),
PortAudio/sounddevice and a librespot build with `pulseaudio` backend. Check the
installed `librespot --backend '?'` and `--help`, rather than guessing flags.
Use lingering for the shared user's desktop-free audio/session services.

For a **new** installation, first back up the receiver config and paired state
privately. A one-time system-to-user migration needs explicit interruption
approval. Ensure the old system receiver is disabled and never run both. Preserve
cache ownership, receiver identity and pairing; do not copy credentials into Git.
`deploy/spotify/raspotify.conf` is only a package-format reference, not another
service to enable. The authoritative user template here keeps the already-tested
receiver name, Pulse backend, cache locations and discovery port 38145.

Install `80-lacasa-spotify.conf` as
`~/.config/pipewire/pipewire-pulse.conf.d/80-lacasa-spotify.conf`.
Its `pulse.cmd` entries load one null sink and one loopback per fresh Pulse server
session. The receiver's bounded `ExecStartPre` helper additionally checks sinks
and existing loopback modules before loading missing routes. Repeated invocations
never knowingly create duplicates; it does not restart or unload anything.
For another host, update the analog name in both the fragment and helper/unit.

Install `raspotify.service` and `spotify-ducking-guard.service` in
`~/.config/systemd/user/`, then `systemctl --user daemon-reload`.
Enable the independent guard with
`systemctl --user enable --now spotify-ducking-guard.service`.
The guard must **not** have `PartOf=jarvis.service` or `BindsTo=jarvis.service`.
It must survive JARVIS's death. Its private runtime directory is deliberately
preserved when the guard restarts.

**Existing live LaCasa session:** equivalent routing is already installed and
Spotify is paired/playing. Do not restart PipeWire/Pulse or Spotify to install
these equivalent templates. Deploy/restart only JARVIS and start the guard.
A later deliberate maintenance restart can verify cold-start routing again.

## Envelope and recovery

On Linux headless playback, ducking defaults on. `JARVIS_DUCKING=0` explicitly
disables it, `JARVIS_DUCK_DB` defaults to 18 (allowed 0–60), and
`JARVIS_DUCK_RAMP_SECONDS` defaults to 0.08 (allowed 0–0.5). A ramp has four bounded
steps. Only the named virtual sink's per-channel raw integers and mute are read
and changed; no master, sink-input, transport or service controls are issued.
Pulse raw volume is cubic: acoustic -18 dB means multiplying raw integers by
`10**(-18/60)` (65536 -> 32846), **not** `10**(-18/20)`. The app's own Spotify
volume remains independent, so app changes and new tracks/streams survive release.
Do not manually use the dedicated envelope sink itself as an app-volume control.

Before the first mixer write, baseline, PID/start identity, generation,
heartbeat and minimum audio-tail deadline are atomically journaled with mode0600
under `$XDG_RUNTIME_DIR/jarvis-ducking/state.json` (parent0700). No tokens,
transcripts or account data are persisted. A shared bounded flock serializes
all journal and mixer changes, so old owners/guards cannot roll back a new
output generation. Begin/end are idempotent; a second controller cannot capture
a currently ducked baseline. A malformed journal degrades explicitly rather
than guessing a volume. Inspect it locally before removing it.

Heartbeat refreshes every 250 ms while writing, waiting between chunks, draining
and cleaning up. The live-owner lease is 6 seconds; guard polls every 500 ms.
Dead-owner recovery also respects the saved physical-tail minimum. Playback
waits for queue empty **and** turn completion (or Stop), then PCM duration,
reported output latency and a 250 ms room tail. A blocking write cannot reopen
the mic or release music prematurely. Cancellation/SIGTERM waits for its worker
and cleanup; hard death is covered by the independent guard. If a hardware write
hangs forever, systemd may eventually kill the process; the guard then recovers.

Commands from the repo, under the same user/Pulse session:

```sh
.venv/bin/python -m core.ducking guard --once  # bounded check; active owner untouched
.venv/bin/python -m core.ducking guard         # independent continuous guard
.venv/bin/python -m core.ducking restore       # explicit FORCE cleanup, not during speech
```

`--state PATH` and `--sink NAME` permit isolated diagnostics; production owner and
guard must use the same path/sink. Exit1 on malformed state or failed audio
recovery; baseline remains for retry. Missing sink/server only degrades ducking,
logs an `ERR: Spotify ducking degraded` line and never prevents voice or restarts
Spotify. Successful restoration is read back before deleting the journal.

## Acceptance on the actual audio host

Run the complete suite with `.venv/bin/python -m unittest discover -s tests -v`.
Tests exercise the real playback lifecycle with only the hardware boundary
substituted, plus actual SIGTERM/SIGKILL children and competing guard/owner
processes. These are not proof of physical audio.

Before claiming live acceptance, record `pactl --format=json list sinks`,
`list sink-inputs`, `list modules` and the receiver PID. Run the route helper twice
and verify the null sink/loopback counts do not increase. The bounded real
`PulseSinkGain('lacasa_spotify').read()` / `write(original)` / `read()` path must
succeed under the service user. While Spotify genuinely plays, send a harmless
JARVIS speech command and capture baseline/during/after sink gains, unchanged
analog/voice/app levels and unchanged Spotify PID with continued track progress.
Repeat Stop, a long gap/block (>6s), app volume adjustment and a newly created
Spotify stream. Use a **separate probe owner**, never kill the paired receiver,
to exercise SIGKILL and confirm that the running guard restores exact gains.

For measured acoustic isolation, inject labelled quiet PCM tones separately to
`lacasa_spotify` and analog while recording the analog monitor. Compare their
frequency amplitudes before/during/after; only the music tone should drop about
18 dB. Do not infer isolation from mixed-monitor peak level while real music is
playing. Speaker audibility/intelligibility still needs the user's confirmation.
