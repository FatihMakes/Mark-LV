# LaCasa Server Voice and Spotify Ducking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver a running headless JARVIS on lacasa-server with private iPhone microphone control, local spoken answers, and uninterrupted Spotify with temporary ducking.

**Architecture:** Retain JarvisLive, Gemini Live and DashboardServer. Run remote capture without local input, server playback through a common PipeWire/Pulse session. Route Spotify through a dedicated named virtual sink with its own gain, so application volume and JARVIS gain remain independent. Ducking is released by playback completion with latency/tail and independently recovered after a crash.

**Tech Stack:** Python 3.11+, asyncio, google-genai, FastAPI/Uvicorn, sounddevice, PipeWire/Pulse compatibility, librespot pulseaudio backend, systemd user services, Tailscale Serve.

**Spec:** `docs/superpowers/specs/2026-10-16-server-voice-and-spotify-ducking-design.md`

## Global Constraints

- JARVIS runs headless on `lacasa-server`, no display, GUI, global keyboard hook or local microphone required.
- iPhone input; server speakers output; future satellites and Telegram are out of scope.
- Microphone closed during speech and audible tail; authenticated Stop available; discard stale microphone frames and remaining interrupted-turn output.
- Spotify continues playing: no pause, seek, stop, restart or master-volume change during ducking. Restore prior gain; preserve intentional Spotify-app volume changes.
- Private trusted HTTPS `https://lacasa-server.tail7a9c2.ts.net`; no public Funnel or automatic firewall mutation.
- Fail closed on protected actions. Only HA read operations are enabled in initial headless allowlist; keep desktop/shell/camera/shutdown tools out.
- Credentials remain local, mode 0600, untracked. Preserve unrelated pending `plugins/cannabis_status.py` and `DASHBOARD_GUIDE.md`.
- User explicitly approved design and ordered full deployment/testing; proceed through tasks without another architecture/continuation question. Use existing checkout with selective staging, as the user has authorized work and pushes in this fork; do not erase unfinished unrelated work.
- Commit and push coherent reviewed changes to user's fork. Verify remote HEAD and deployment revision separately. Acoustic/iPhone-only results remain unverified without user observation.

## Review Focus

1. Safari recording permission resolves after Stop/reconnect: check busy state again after asynchronous getUserMedia; close tracks on every failure.
2. Cancelling a turn with queued realtime input must not replay the microphone or revive discarded audio: test server gate and interruption across receive/playback boundary.
3. Spotify app changes volume while ducked: independent sink gain restores without overwriting application stream volume.
4. Audio server or JARVIS dies while ducked: watchdog restores persisted gain; empty sink/absent Spotify does not fail voice startup.
5. Invalid credentials and backend failure must surface in status/logs without repeated API/key creation or private GUI access.

---

### Task 1: Safe headless phone-input/server-output path

**Files:** Modify `main.py`, `ui_headless.py`, `dashboard/server.py`, `dashboard/static/app.html`, `plugins/home_assistant.py`; create `core/headless.py`, `tests/test_headless.py`, `tests/test_dashboard_headless.py`, `tests/test_home_assistant.py`, `requirements-headless.txt` and `deploy/jarvis.service` as needed.

**Interfaces:**
- `core.headless.enabled() -> bool` from `JARVIS_HEADLESS` (default true for this server fork); `capture_source() -> str` default `phone`.
- `DashboardServer` supports external public URL + loopback bind; callable `set_interrupt_callback(fn)`, `set_audio_allowed_callback(fn)`, and current status delivery for reconnect. Authenticated POST `/api/interrupt` and GET `/api/status`. Existing `/api/command`, `/ws`, `/ws/phone-audio` remain compatible.
- A local-only Unix control channel/CLI or mode-0600 pairing request file produces short-lived rotating pairing links without a GUI. Client link target is real external origin. Rotation revokes prior sessions/keys and live WebSockets; do not leak bearer tokens in logs.
- Existing `JarvisLive.set_speaking(value)` remains the playback hook for Task 2. Audio gating checks speaking plus tail, not UI state alone. Incoming realtime microphone data uses `audio/pcm;rate=16000`. Stop discards output until this turn completes; explicit microphone stop signals `audio_stream_end` after queued valid frames, so Gemini does not need silence forever.

- [ ] Step 1: Write tests for fail-closed `show_confirm(title, detail)`, safe missing/invalid-key path, capture mode skipping local RawInputStream, read-only tool allowlist (dispatch rejection too, not merely schema filtering), authenticated interrupt/status, invalid/expired/rotated keys, only one audio source, stale frame rejection, and interrupted turn suppression. Add HA tests converting actual °F sensor data to °C, ignoring unavailable/non-temperature values, and rejecting non-on/off set values; deployed headless use stays read-only.
- [ ] Step 2: Run targeted tests; record expected RED. Use mocks only for external APIs/audio hardware, exercise real route/state code.
- [ ] Step 3: Implement smallest headless adaptations. Disable global hotkeys/wake/local input/proactive system actions in headless mode; keep server output and relays. No desktop dependency installation for unused actions. Remove `_win` use from auth-error path. Default allowlist HA reads only; a model cannot force `set` through dispatch. Preserve existing desktop behavior where practicable.
- [ ] Step 4: Add half-duplex browser control and Stop button, remove insecure chrome://flags guidance, keep mic closed after speech until explicitly tapped. Always stop mic tracks on failed connection, disconnect and speaking-status event; handle permissions resolving after gate changed. No elaborate new UI.
- [ ] Step 5: Verify targeted tests plus full discovered suite, `py_compile` changed Python, JS syntax check, and startup/import in target venv. Document any pre-existing missing dependencies honestly. Provide an explicit headless requirements set and service command.
- [ ] Step 6: Commit explicit owned paths with `feat: enable secure headless iPhone input and local speech` and report RED/GREEN evidence and current operational limitations.

### Task 2: Independent Spotify ducking and crash recovery

**Files:** Create `core/ducking.py`, `tests/test_ducking.py`, `deploy/audio/spotify-ducking-guard.service`, `deploy/audio/raspotify.service`, `deploy/audio/README.md`; modify `main.py` playback lifecycle and `deploy/spotify/raspotify.conf` only as necessary.

**Interfaces:**
- `PulseSinkGain(sink_name: str)` uses bounded `pactl --format=json` calls, stable named sink `lacasa_spotify`, per-channel integer gains/mute, no master or Spotify input manipulation.
- `DuckingController.begin()`, `.heartbeat()`, `.end()` idempotent; constructor injectable gain adapter/state path/clock, default reduction 18 dB. Persist baseline plus owner/heartbeat atomically in private runtime storage before changing gain. Never baseline from already-ducked state.
- Independent `python -m core.ducking guard` checks expired ownership/heartbeat and restores original sink gain; `restore` supports explicit cleanup. Main playback refreshes heartbeat while holding ducking. Guard must not restore while valid ongoing output lease exists.
- Route librespot app stream to `lacasa_spotify` null sink. Loopback monitor to actual analog sink; JARVIS outputs directly to analog. Sink gain is the temporary envelope only; librespot/app volume remains independent. Null sink + loopback modules must be reproducibly created with duplicate avoidance at session start.

- [ ] Step 1: Failing tests for different channel baselines, zero gain/mute preservation, idempotent/overlapping begin, changed Spotify-app input volume untouched, absent sink, new Spotify stream, failed pactl/no restart, exception/cancellation, persisted state recovery after killed owner, stale heartbeat, active lease, malformed journal and unavailable audio server. Assert no pause/stop/seek/service commands occur.
- [ ] Step 2: Run RED; implement adapter/controller/guard with subprocess timeouts, atomic journal, bounded ramps and recovery. Avoid scaling already-scaled volume.
- [ ] Step 3: Integrate begin immediately before first actual output, refresh during blocks and inter-chunk waits, release only after turn done AND queue empty AND buffered playback duration/latency/tail expired. Stop and finally release safely; don't reopen mic before that tail. Do not duck during tool-only/text-only turns. Tests exercise actual playback lifecycle through hardware adapter, not assertions on source strings.
- [ ] Step 4: Test actual common audio session and null-sink gain with real PCM injection; verify original gain restored and distinct Spotify/JARVIS routes, including owner-process kill and long ongoing turn.
- [ ] Step 5: Run complete suite, diff checks; commit `feat: duck Spotify independently during JARVIS speech with recovery`.

### Task 3: Deploy, verify and document real access

**Files:** Create/modify `docs/server-voice-quickstart.md`, `deploy/audio/*`, `deploy/jarvis.service`, `requirements-headless.txt` as needed; no real keys committed.

**Interfaces:** Uses Tasks 1/2 CLI/control, user units and mixer. Deploy `/home/lacasa/Mark-LV-LaCasa` as user `lacasa`, `.venv/bin/python main.py`, bind 127.0.0.1:8000 behind private Tailscale Serve. Reuse actual local credentials by secure SSH transfer, no raw values in reports.

- [ ] Step 1: Capture current service/config/audio baseline and backup relevant files outside Git. Install PipeWire/WirePlumber, Pulse tools and runtime library using explicit normal sudo authentication; retain SSH/Tailscale networking.
- [ ] Step 2: Set up persistent user audio session and stable analog sink, separate virtual Spotify sink/loopback. Confirm installed librespot supports `pulseaudio`. Migrate once from package Raspotify service to same-user receiver after backup; preserve device name/cache and disable old unit only when replacement ready. No second competing receiver.
- [ ] Step 3: Copy ignored credentials securely, verify key presence/HA read from target interpreter without printing credentials. Start JARVIS + guard with user systemd units, enable linger; read back health and listeners. Fix observed dependency/runtime errors, not invented stubs.
- [ ] Step 4: Configure Tailscale Serve after checking previous config; verify HTTPS with normal certificate verification. Test unauthenticated command/audio rejection, pair once with temporary test session, issue unique harmless text, prove Gemini transcript plus nonempty spoken audio reaches correct route. Read actual HA sensor state and compare returned values. Rotate test session before delivering user pairing link/QR.
- [ ] Step 5: Verify simultaneous PCM routes, duck/restore measurements, Stop, disconnected client, killed-owner recovery, no Spotify service identity change during normal ducking. Real Spotify-account pairing and audibility need user; distinguish infrastructure/protocol evidence from acoustic pass.
- [ ] Step 6: Run task review and whole-change review, fix important findings, rerun covering tests. Commit/push explicit paths; read GitHub HEAD, deployed revision, actual services, HTTPS health. Document quickstart, pairing renewal, service control and exact remaining manual tests. Deliver actual usable URL/QR and Spotify receiver name, never claim audible success before confirmation.
