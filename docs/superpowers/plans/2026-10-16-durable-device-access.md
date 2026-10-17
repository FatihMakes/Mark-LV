# Durable Device QR Access Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace restart-volatile QR sessions with four persistent, independently revocable device credentials and one-hour bearer sessions, while retaining the ten-minute emergency pairing CLI and the private headless service boundary.

**Architecture:** A focused `core/device_credentials.py` owns a versioned, atomically replaced verifier registry and private QR artifacts; it stores only per-device scrypt salts/verifiers, never durable raw secrets. `dashboard/access.py` owns short-lived in-memory bearer records, while `DashboardServer` binds those records to HTTP and both WebSocket types, closes only the affected device's live sockets, and exposes administration only through a bounded line-delimited JSON Unix-socket protocol consumed by `core.headless`. The login page consumes fragment credentials and the dashboard recovers stale open tabs by discarding only expired session state and re-exchanging the durable credential.

**Tech Stack:** Python 3.11+, `hashlib.scrypt`, `hmac.compare_digest`, asyncio Unix sockets, FastAPI/Starlette WebSockets, qrcode/Pillow, browser JavaScript exercised by Node.js `vm`, unittest.

**Spec:** `docs/superpowers/specs/2026-10-16-spotify-control-and-durable-devices-design.md`

## Global Constraints

- Scope is only durable device/QR access, shared private-control-socket protocol, documentation, and server acceptance. Do not implement Spotify OAuth, Web API calls, tools, search, playback, or audio ducking in this plan.
- Target host remains `lacasa-server`; the dashboard remains bound to `127.0.0.1:8000` behind `https://lacasa-server.tail7a9c2.ts.net` and Tailscale Serve. No Funnel, public bind, self-signed bypass, or firewall mutation.
- Preserve `python -m core.headless pair` as a ten-minute emergency pairing path. Creating a transient pair may rotate prior transient PINs/sessions, but must not rotate, revoke, or disconnect any durable device or unrelated durable bearer.
- Durable login URL is exactly `https://lacasa-server.tail7a9c2.ts.net/login#device=<device-id>.<secret>`: the durable credential must never be in a query string, HTTP request target, access log, service journal, Git, test failure, report, or chat output.
- Each durable secret is 256 random bits. The server stores only a random opaque device ID, label, UTC creation timestamp, independent random salt, `hashlib.scrypt` verifier, and optional UTC revocation timestamp. Verification uses `hmac.compare_digest`.
- `config/device_credentials.json` is versioned JSON, atomically replaced, owned by the service UID, and exactly mode `0600`. Missing means an empty registry; corrupt JSON/schema, symlinks, wrong owner, or group/world-accessible files fail closed as configuration error/exit 78, never as an empty registry.
- The credential-store parent must be a real directory owned by the service UID and not group/world writable. Atomic temporary files are created in that directory with `O_CREAT|O_EXCL|O_NOFOLLOW`, mode `0600`, fsynced, replaced, and followed by a directory fsync.
- The QR output directory is a real service-owned mode-`0700` directory. Generated `.url` and `.png` artifacts are new regular files opened with `O_CREAT|O_EXCL|O_NOFOLLOW` and mode `0600`; never follow or replace an existing path.
- No process-wide raw server secret and no persisted bearer/session encryption keys. Every successful login receives a fresh random bearer and fresh per-session encryption key held only in memory.
- Bearers have a 3,600-second absolute lifetime. HTTP requests at `now >= expires_at` return 401, and both control and audio WebSockets close with code 4001 even when idle.
- HTTP 401 and WebSocket close 4001 remove only `sessionStorage.jarvis_token` and `sessionStorage.jarvis_key`, preserve the durable local credential, and route through `/login`. Durable-login 401 removes only the rejected durable credential and leaves the manual emergency PIN form available without retry looping.
- Rate-limit transient PIN/auto-login attempts and durable credential login independently to 10 attempts per rolling 60 seconds. Do not trust forwarded client-IP headers because Uvicorn runs with `proxy_headers=False`.
- Device create/list/revoke exists only on the private mode-`0600` Unix control socket. Remove `/api/revoke-devices`; do not add any HTTP device administration route. A phone bearer cannot create, list, rename, or revoke credentials.
- Revoking one device invalidates its bearers and closes its live control and phone-audio WebSockets with 4001, including upstream audio closure, without affecting any other device.
- Initial active labels are exactly `LaCasa iPhone 1`, `LaCasa iPhone 2`, `LaCasa iPhone 3`, and `LaCasa iPhone 4`. Replacement after loss is revoke-then-create and may reuse the label; historical revoked records remain.
- Keep current half-duplex, STOP, audio-epoch, and ordered `audio_stream_end` behavior. Authentication changes must not reopen capture, discard another device's session, or weaken current handshake cleanup.
- Preserve the current essential-server lifecycle: invalid credential-store/QR configuration exits 78; ordinary HTTP/control listener failures remain retryable; model connection starts only after HTTP and control readiness.
- Use the current runner exactly: `/root/.hermes/cache/scratch/marklv-task1-venv/bin/python -m unittest discover -s tests -v`. Run JavaScript behavior through a real Node process, not source-string assertions.
- Work from commit `0045027` with selective staging. Do not stage or modify the unrelated existing `plugins/cannabis_status.py` change or untracked `DASHBOARD_GUIDE.md`; do not push or touch a remote unless separately authorized.

## Review Focus

1. **Filesystem substitution/race:** a symlinked store, QR directory/file, unsafe owner/mode, or target swapped between validation and write must fail without modifying the link target; Task 1 adds real symlink/mode tests and injected ownership/race tests.
2. **Expiry and restart boundaries:** a bearer valid just before 3,600 seconds must fail exactly at the boundary, idle WebSockets must close, and an already-open tab must recover after an abrupt service restart; Tasks 2, 3, and 5 pin all three paths.
3. **Concurrent mutation:** simultaneous creates/revokes must not lose records, resurrect a revoked device, or leave partial JSON/artifacts; Task 1 tests serialized read-modify-write and parseable post-race state.
4. **Isolation:** revoking or rotating a transient session/device must close only matching control/audio sockets and preserve the other three durable sessions and queued audio ownership; Task 3 exercises two devices plus transient pairing through real route state.
5. **Malformed/unbounded input:** oversized JSON lines, bad UTF-8, invalid labels, malformed fragments, unknown IDs, and malformed 256-bit secrets must return bounded errors without disclosure, unsafe writes, or browser retry loops; Tasks 1, 2, 4, and 5 each test their owning boundary.

---

### Task 1: Build the fail-closed persistent credential and QR artifact layer

**Files:**
- Create: `core/device_credentials.py`
- Create: `tests/test_device_credentials.py`
- Modify: `requirements-headless.txt`

**Interfaces:**
- Produces `DeviceCredentialConfigError(ValueError)` so the existing `DashboardServer` construction path maps unsafe/corrupt local configuration to exit 78.
- Produces immutable `DeviceSummary(device_id: str, label: str, created_at: str, revoked_at: str | None)` and `CreatedDevice(summary: DeviceSummary, secret: str)`.
- Produces `DeviceCredentialStore(path: Path, *, clock: Callable[[], float] = time.time)` with:
  - `create(label: str) -> CreatedDevice`
  - `verify(device_id: str, secret: str) -> DeviceSummary | None`
  - `list_devices() -> list[DeviceSummary]`
  - `revoke(device_id: str) -> DeviceSummary | None` (idempotent for an existing already-revoked record; `None` only for an unknown ID)
- Produces `write_device_artifacts(created: CreatedDevice, public_url: str, output_dir: Path) -> tuple[Path, Path]`, returning the `.url` and `.png` paths while never returning/logging the URL text. The caller revokes the just-created record and removes partial artifacts if artifact generation fails.
- Serialized schema is exactly `{"version":1,"devices":[{"device_id":...,"label":...,"created_at":...,"salt":...,"verifier":...,"revoked_at":null}]}`. Timestamps are RFC 3339 UTC with `Z`; summaries never expose `salt` or `verifier`.
- Fixed KDF/encoding values: 16-byte opaque device ID, 32-byte secret, 16-byte salt, URL-safe base64 without padding; `scrypt(n=2**14, r=8, p=1, dklen=32)`. A syntactically valid unknown ID performs a dummy scrypt and compare so known/unknown IDs do not have a trivial timing split; the dummy material is public fixed material, not a server secret.
- Labels are stripped once, stored as supplied thereafter, and must contain 1-80 Unicode characters with no C0/C1 control characters. The maximum accepted credential-store file size is exactly 1,048,576 bytes.
- Store operations hold one process-local `threading.RLock` across load/modify/atomic-write so concurrent control clients cannot lose updates.

- [ ] **Step 1: Write the first failing store tests**

Add one-behavior tests:

```python
def test_four_credentials_persist_without_raw_secrets(): ...
def test_restart_store_verifies_each_device_independently(): ...
def test_scrypt_uses_independent_salts_and_constant_time_compare(): ...
def test_revoke_is_independent_idempotent_and_persistent(): ...
def test_unknown_well_formed_device_still_runs_dummy_scrypt(): ...
def test_invalid_label_or_credential_encoding_is_rejected_without_write(): ...
```

Use a frozen clock and labels `LaCasa iPhone 1` through `LaCasa iPhone 4`. Assert four unique IDs, salts, and verifiers; decode the returned secret to exactly 32 bytes; assert none of the four raw secrets occurs in file bytes or summaries; reopen a second store instance and verify all four; revoke one and prove only it fails. Patch/wrap `hashlib.scrypt` and `hmac.compare_digest` to assert the fixed parameters and comparison call while still exercising the real functions.

- [ ] **Step 2: Run the store tests and verify RED**

Run: `/root/.hermes/cache/scratch/marklv-task1-venv/bin/python -m unittest tests.test_device_credentials -v`

Expected: FAIL because `core.device_credentials` does not exist; no test may fail first due to fixtures, permissions, or an unavailable third-party package.

- [ ] **Step 3: Implement the minimal versioned store**

Implement the interfaces above. Validate the complete schema, duplicate IDs, timestamp/string fields, base64 lengths, and verifier lengths on every load. A missing path is empty; an existing empty/corrupt/oversized/non-regular file is configuration failure. Keep the raw secret only in `CreatedDevice` long enough for artifact generation, and never include it in exceptions or `repr` (mark the dataclass secret field `repr=False`).

- [ ] **Step 4: Run the store tests and verify GREEN**

Run: `/root/.hermes/cache/scratch/marklv-task1-venv/bin/python -m unittest tests.test_device_credentials -v`

Expected: all tests added in Step 1 PASS.

- [ ] **Step 5: Write failing filesystem and concurrency tests**

Add:

```python
def test_store_rejects_symlink_wrong_mode_wrong_owner_and_corruption(): ...
def test_atomic_write_never_exposes_partial_json(): ...
def test_concurrent_creates_and_revoke_preserve_every_record(): ...
def test_qr_uses_fragment_only_and_private_artifacts(): ...
def test_qr_rejects_symlink_directory_or_existing_output_without_touching_target(): ...
def test_qr_failure_leaves_no_secret_in_exception_or_log(): ...
```

Use actual temporary-directory symlinks and chmod modes; inject an ownership mismatch where the test UID cannot chown. Have concurrent readers parse the store while worker threads create/revoke. Decode the generated QR (or inspect the `.url` file and QR library payload hook) and assert the exact `/login#device=<id>.<secret>` payload, no `?device=`, artifact modes `0600`, output-directory mode `0700`, filenames contain only device ID/suffix, and neither captured stdout/stderr/logging nor exceptions contain the secret.

- [ ] **Step 6: Run the new tests and verify RED**

Run: `/root/.hermes/cache/scratch/marklv-task1-venv/bin/python -m unittest tests.test_device_credentials -v`

Expected: new filesystem/QR tests FAIL because secure atomic writes and artifact creation are missing.

- [ ] **Step 7: Implement secure atomic writes and QR artifacts**

Use `lstat`/directory FDs rather than resolving through links; reject unsafe existing state before opening. Create same-directory temporary files with random nonsecret names, verify their stat data, flush/fsync, `os.replace`, fsync the parent, and clean up on every exception. For artifacts, use exclusive no-follow opens and pass an already-open binary file object to qrcode/Pillow; never overwrite. Add `qrcode[pil]>=8,<9` to `requirements-headless.txt`.

- [ ] **Step 8: Re-run Task 1 and the current suite**

Run:

```bash
/root/.hermes/cache/scratch/marklv-task1-venv/bin/python -m unittest tests.test_device_credentials -v
/root/.hermes/cache/scratch/marklv-task1-venv/bin/python -m unittest discover -s tests -v
```

Expected: both commands PASS with no warnings containing credential material.

- [ ] **Step 9: Commit only Task 1 paths**

```bash
git add core/device_credentials.py tests/test_device_credentials.py requirements-headless.txt
git diff --cached --check
git commit -m "feat: add persistent hashed device credentials"
```

### Task 2: Introduce one-hour bearer sessions and durable HTTP login

**Files:**
- Create: `dashboard/access.py`
- Create: `tests/test_device_access.py`
- Modify: `dashboard/server.py:462-558, 693-855, 865-1077`

**Interfaces:**
- Consumes `DeviceCredentialStore.verify()` and `DeviceSummary` from Task 1.
- Produces immutable `SessionRecord(token: str, session_key: str, device_id: str | None, created_at: float, expires_at: float)`.
- Produces `SessionRegistry(*, ttl_seconds: float = 3600, clock: Callable[[], float] = time.time)` with:
  - `issue(device_id: str | None) -> SessionRecord`
  - `authenticate(token: str) -> SessionRecord | None` (`now >= expires_at` deletes and rejects)
  - `revoke_device(device_id: str) -> set[str]`
  - `revoke_transient() -> set[str]`
  - `discard(token: str) -> None`
- Produces `AttemptLimiter(limit: int = 10, window_seconds: float = 60, clock: Callable[[], float] = time.monotonic)` with `allow(bucket: str) -> bool`; use separate process-global-per-server buckets `transient-login` and `durable-login` because forwarded IP headers are untrusted.
- `DashboardServer.__init__` retains existing positional compatibility and adds keyword-only `device_store: DeviceCredentialStore | None = None`, `qr_output_dir: Path | None = None`, `bearer_ttl_seconds: float = 3600`, `wall_clock=time.time`, and `monotonic_clock=time.monotonic`. Defaults come from `JARVIS_DEVICE_CREDENTIALS` or `BASE_DIR/config/device_credentials.json`, and `JARVIS_DEVICE_QR_DIR` or `BASE_DIR/config/device_qr`.
- `POST /api/device-login` consumes exactly `{"device_id": str, "secret": str}` and returns `{"ok": true, "token": str, "key": str, "expires_in": 3600}`. Malformed JSON/type/encoding is 400, rate limit is 429, unknown/revoked/wrong secret is the same body/status 401, and no response identifies verifier data.
- Existing authenticated routes and decryption resolve the bearer through `SessionRegistry`; remove `_tokens`, `_token_keys`, and `_device_sessions`. Each login uses a new independent session key, never the durable secret or transient PIN.

- [ ] **Step 1: Write failing session-registry tests**

Add:

```python
def test_session_record_is_valid_before_and_invalid_at_absolute_expiry(): ...
def test_session_issue_generates_fresh_bearer_and_encryption_key(): ...
def test_registry_revokes_only_matching_device_or_transient_sessions(): ...
def test_rate_limit_has_independent_ten_per_minute_buckets(): ...
```

Use fake clocks and assert the exact 3,600-second boundary. Assert two exchanges of one device credential have different token and key values and no persisted state.

- [ ] **Step 2: Run registry tests and verify RED**

Run: `/root/.hermes/cache/scratch/marklv-task1-venv/bin/python -m unittest tests.test_device_access -v`

Expected: FAIL because `dashboard.access` is absent.

- [ ] **Step 3: Implement `dashboard/access.py` minimally**

Use `secrets.token_urlsafe(32)` independently for bearer and session key. Keep records in memory only. Purge expired records on issue/auth/revoke without extending expiration on use.

- [ ] **Step 4: Run registry tests and verify GREEN**

Run: `/root/.hermes/cache/scratch/marklv-task1-venv/bin/python -m unittest tests.test_device_access -v`

Expected: registry and limiter tests PASS.

- [ ] **Step 5: Write failing route/restart tests**

Add:

```python
def test_device_login_rejects_malformed_unknown_wrong_and_revoked_identically(): ...
def test_device_login_returns_fresh_session_material_without_secret_disclosure(): ...
def test_durable_login_survives_new_dashboard_server_with_same_store(): ...
def test_http_bearer_expires_at_one_hour_without_sliding(): ...
def test_durable_login_rate_limit_does_not_consume_transient_pin_budget(): ...
def test_no_raw_server_secret_or_session_material_is_written_to_store(): ...
```

Build two `DashboardServer` objects around new `DeviceCredentialStore` instances pointing to the same temporary file. Authenticate after replacement without sharing any in-memory server state. Exercise real FastAPI routes with `TestClient`; verify `/api/status`, `/api/command`, and `/api/interrupt` all return 401 at the boundary.

- [ ] **Step 6: Run route tests and verify RED**

Run: `/root/.hermes/cache/scratch/marklv-task1-venv/bin/python -m unittest tests.test_device_access -v`

Expected: route/restart tests FAIL against the current volatile `device_token` contract.

- [ ] **Step 7: Integrate the registry and durable login route**

Replace token-set membership with session lookup everywhere. Keep one helper that returns the authenticated `SessionRecord`, so route code cannot accidentally accept an expired token. Do not change unauthenticated route policy beyond `/api/device-login`; `/login`, static content, and existing transient endpoints remain as before.

- [ ] **Step 8: Run targeted and regression tests**

Run:

```bash
/root/.hermes/cache/scratch/marklv-task1-venv/bin/python -m unittest tests.test_device_access tests.test_dashboard_headless -v
/root/.hermes/cache/scratch/marklv-task1-venv/bin/python -m unittest discover -s tests -v
```

Expected: PASS; update old test fixtures to issue sessions through supported helpers rather than mutating removed `_tokens` internals.

- [ ] **Step 9: Commit only Task 2 paths**

```bash
git add dashboard/access.py dashboard/server.py tests/test_device_access.py tests/test_dashboard_headless.py tests/test_final_lifecycle.py
git diff --cached --check
git commit -m "feat: enforce one-hour durable device sessions"
```

### Task 3: Enforce per-device revocation and live WebSocket expiry without breaking emergency pairing

**Files:**
- Modify: `dashboard/server.py:477-525, 584-653, 673-689, 781-855, 894-955, 1045-1075`
- Modify: `tests/test_device_access.py`
- Modify: `tests/test_dashboard_headless.py:34-107, 185-210`
- Modify: `tests/test_final_lifecycle.py:225-355`

**Interfaces:**
- Consumes `SessionRegistry.revoke_device()`/`revoke_transient()` from Task 2.
- Produces async `DashboardServer.revoke_device(device_id: str) -> DeviceSummary | None`; store revocation is authoritative before session termination.
- Produces internal `_terminate_tokens(tokens: set[str], *, code: int = 4001) -> None`, using `token -> set[WebSocket]` and `WebSocket -> token` indexes for both `/ws` and `/ws/phone-audio`.
- Each WebSocket receive waits only for `record.expires_at - wall_clock()` (never a refreshed interval); timeout discards the token and closes that socket with 4001. Route loops also re-check `authenticate()` after every receive to fence scheduler delay. This enforces idle expiry without hour-long background tasks leaking across TestClient/service shutdown.
- If the terminated token owns phone audio, call existing `invalidate_audio()` before/while closing so accepted upstream PCM produces the required generation-fenced closure. Never invalidate another device's current audio owner.
- Preserve `DashboardServer.new_key(expiry_secs=600) -> str` for existing callers, but narrow it to pending transient keys and transient sessions only. `/auto-login` issues a transient bearer and does not write durable local storage or create an in-memory pseudo-device.

- [ ] **Step 1: Write failing isolation and idle-expiry tests**

Add one-behavior tests:

```python
def test_revoke_one_device_closes_only_its_control_and_audio_sockets_4001(): ...
def test_revoked_audio_owner_emits_upstream_end_and_replacement_device_can_capture(): ...
def test_idle_control_and_audio_websockets_close_4001_at_absolute_expiry(): ...
def test_new_emergency_pair_rotates_only_transient_sessions(): ...
def test_transient_pair_does_not_change_store_or_durable_bearers(): ...
def test_revocation_wins_a_concurrent_relogin_and_cannot_resurrect_device(): ...
```

Use two real durable sessions plus one transient session. Keep the second durable control socket open and prove it still exchanges a command/status after revoking the first. Drive real phone PCM through the existing relay/sender fixture to assert ordered upstream end. Use a very short injected bearer TTL only for idle-close tests; retain exact fake-clock tests for the 3,600-second boundary.

- [ ] **Step 2: Run targeted tests and verify RED**

Run:

```bash
/root/.hermes/cache/scratch/marklv-task1-venv/bin/python -m unittest tests.test_device_access -v
/root/.hermes/cache/scratch/marklv-task1-venv/bin/python -m unittest tests.test_final_lifecycle.PhoneClosureTests -v
```

Expected: FAIL because sockets are not indexed by session/device and `new_key()` currently revokes everything.

- [ ] **Step 3: Implement token/socket indexing, expiry tasks, and scoped termination**

Register a socket only after successful accept, remove both indexes in `finally`, and tolerate accept/send/close cancellation exactly as current handshake tests require. Make store revocation and session fencing happen before awaits that close sockets. Keep close task failures contained and awaited by control commands; never put raw tokens in task names, logs, or exception text.

- [ ] **Step 4: Run targeted tests and verify GREEN**

Run the two Step 2 commands.

Expected: PASS, including existing stale-frame, explicit-end, owner-release, and handshake-failure regressions.

- [ ] **Step 5: Update old global-rotation assertions to the approved semantics**

Change `test_invalid_expired_and_rotation_revokes_every_credential` into separate transient-rotation and durable-survival tests. Keep `request_pair()` behavior and ten-minute default. Update the current real-socket rotation test to assert transient sockets close while a durable socket remains open.

- [ ] **Step 6: Run the complete suite**

Run: `/root/.hermes/cache/scratch/marklv-task1-venv/bin/python -m unittest discover -s tests -v`

Expected: PASS with no leaked asyncio expiry tasks or resource warnings.

- [ ] **Step 7: Commit only Task 3 paths**

```bash
git add dashboard/server.py tests/test_device_access.py tests/test_dashboard_headless.py tests/test_final_lifecycle.py
git diff --cached --check
git commit -m "feat: revoke device sessions independently"
```

### Task 4: Add bounded private JSON administration, safe QR creation, and device CLI commands

**Files:**
- Modify: `dashboard/server.py:603-653, 856-863`
- Modify: `core/headless.py:62-97`
- Modify: `tests/test_dashboard_headless.py:185-210`
- Modify: `tests/test_device_access.py`
- Modify: `tests/test_gitignore_secrets.py`
- Modify: `.gitignore`
- Modify: `deploy/jarvis.service`

**Interfaces:**
- Control transport remains a mode-`0600` Unix stream socket in a mode-`0700` owner directory, with a 2-second read deadline, `CONTROL_MAX_LINE_BYTES = 4096` including newline, and `CONTROL_MAX_RESPONSE_BYTES = 65536` including newline. A response that would exceed the bound becomes one generic error response rather than a partial line.
- Preserve literal legacy request `pair\n` and its current secret-bearing operator response for `request_pair()`; it remains explicitly invoked operator output, never a service log.
- New requests are one line of UTF-8 JSON and use exact commands:
  - `{"command":"device-create","label":"LaCasa iPhone 1"}`
  - `{"command":"device-list"}`
  - `{"command":"device-revoke","device_id":"<opaque-id>"}`
- Device-create response contains only `ok`, safe `device` summary, `url_path`, and `qr_path`; it never carries the durable secret or URL payload. Device-list returns safe summaries only. Revoke waits for Task 3 live-session closure before success.
- Produces `DashboardServer.register_control_command(name: str, handler: Callable[[dict[str, object]], Awaitable[dict[str, object]]]) -> None`. Built-in device names and `pair` cannot be overridden. Future Spotify work will register `spotify-authorize` and `spotify-status` through this interface; this plan does not implement or test Spotify behavior beyond dispatching a registered synthetic handler and rejecting an unregistered command.
- Produces `core.headless.request_control(request: dict, path: Path | None = None) -> dict`, with bounded request/response and exact error handling.
- CLI syntax:
  - `python -m core.headless pair [--socket PATH]`
  - `python -m core.headless device-create --label LABEL [--socket PATH]`
  - `python -m core.headless device-list [--socket PATH]`
  - `python -m core.headless device-revoke DEVICE_ID [--socket PATH]`
  Device commands print safe JSON only. `pair` continues to print its transient URL.

- [ ] **Step 1: Write failing protocol and CLI tests**

Add:

```python
async def test_json_control_create_list_revoke_round_trip_is_secret_free(): ...
async def test_control_rejects_oversize_bad_utf8_bad_json_and_unknown_command(): ...
async def test_registered_extension_command_uses_same_bounded_protocol(): ...
def test_device_cli_commands_emit_safe_json_and_preserve_pair_cli(): ...
def test_phone_bearer_has_no_device_admin_http_route(): ...
def test_device_create_artifact_failure_revokes_new_record_and_cleans_partial_files(): ...
```

Connect to the real temporary Unix server. Read the generated `.url` privately to obtain the test credential, then assert that secret and URL payload are absent from the socket response, CLI stdout/stderr, captured server print/log calls, filenames, and list/revoke output. For HTTP, assert `/api/revoke-devices`, `/api/devices`, `/api/device-create`, `/api/device-list`, and `/api/device-revoke` are absent from `app.routes` and return 404 even with a valid bearer.

- [ ] **Step 2: Run protocol tests and verify RED**

Run:

```bash
/root/.hermes/cache/scratch/marklv-task1-venv/bin/python -m unittest tests.test_device_access tests.test_dashboard_headless.ControlTests -v
```

Expected: FAIL because the server accepts only `pair\n`, the CLI has only `pair`, and `/api/revoke-devices` still exists.

- [ ] **Step 3: Implement bounded dispatch and remove HTTP administration**

Read at most one line, reject EOF without newline and over-limit input without echoing it, parse only a JSON object with a string `command`, and emit one bounded JSON response. Keep the Unix listener's current ownership/conflict/cleanup guarantees. Delete `/api/revoke-devices` rather than returning a bearer-authorized denial.

- [ ] **Step 4: Implement device commands and transactional artifact handling**

On create: validate public HTTPS origin, create the store record, write both private artifacts, and if either artifact fails remove partial outputs and persistently revoke that new record before returning a generic error. Do not include URL contents in server replies. On list/revoke, expose only `DeviceSummary` fields. Use the generic registry only for future non-device commands.

- [ ] **Step 5: Implement the argparse subcommands**

Retain the exact old pair invocation. Device command failures exit 1 with a concise nonsecret message; malformed CLI usage remains argparse exit 2. Do not read and print the `.url` file from the CLI.

- [ ] **Step 6: Add ignore/service configuration and verify RED then GREEN**

Extend `test_credentials_are_ignored` with:

```text
config/device_credentials.json
config/device_qr/example.url
config/device_qr/example.png
```

Add explicit `.gitignore` entries for the store and entire QR output directory, even though the existing broad credentials pattern catches the store. Add to `deploy/jarvis.service`:

```ini
Environment=JARVIS_DEVICE_CREDENTIALS=%h/Mark-LV-LaCasa/config/device_credentials.json
Environment=JARVIS_DEVICE_QR_DIR=%h/Mark-LV-LaCasa/config/device_qr
```

Run before and after the edits:

```bash
/root/.hermes/cache/scratch/marklv-task1-venv/bin/python -m unittest tests.test_gitignore_secrets -v
/root/.hermes/cache/scratch/marklv-task1-venv/bin/python -m unittest tests.test_device_access tests.test_dashboard_headless -v
```

Expected: new ignore/service/protocol assertions fail first, then PASS.

- [ ] **Step 7: Run the complete suite**

Run: `/root/.hermes/cache/scratch/marklv-task1-venv/bin/python -m unittest discover -s tests -v`

Expected: PASS; service lifecycle tests still distinguish exit 78 from retryable listener failure.

- [ ] **Step 8: Commit only Task 4 paths**

```bash
git add dashboard/server.py core/headless.py tests/test_device_access.py tests/test_dashboard_headless.py tests/test_gitignore_secrets.py .gitignore deploy/jarvis.service
git diff --cached --check
git commit -m "feat: administer device access over private control socket"
```

### Task 5: Implement fragment login and stale-open-tab recovery with a real Node harness

**Files:**
- Modify: `dashboard/static/login.html:127-226`
- Modify: `dashboard/static/app.html:156-224, 289-357, 462-631`
- Create: `tests/test_browser_device_access.py`
- Modify: `tests/test_dashboard_headless.py:213-312`

**Interfaces:**
- Durable local key is exactly `jarvis_device_credential`; its value is the opaque `<device-id>.<secret>` string. Session keys remain `jarvis_token` and `jarvis_key`.
- Login startup consumes only `location.hash` matching `#device=<id>.<secret>`, calls `history.replaceState(null, '', '/login')` before network I/O, stores the durable credential, and POSTs split fields to `/api/device-login`. Neither fetch URL nor browser history contains the credential.
- Successful durable or manual login writes only fresh returned bearer/key to session storage and `location.replace('/')`.
- Durable 401 removes only `jarvis_device_credential`, hides reconnect state, shows `Invalid or revoked device`, and does not retry. A 429 or network failure preserves the durable credential and offers a bounded user retry; malformed fragments are removed from history but never stored or posted.
- Produces app helpers `_clearSessionAuth()`, `_routeToLogin()`, and `_recoverControlSession()`. `_clearSessionAuth()` calls `removeItem` for only the two JARVIS session keys.
- `_authFetch` routes any 401 through `_routeToLogin()`. Control/audio WebSocket close code 4001 does the same. On an abnormal control close such as restart code 1006, `_recoverControlSession()` retries authenticated `/api/status` with exponential delays capped at 5 seconds: network failures retry, 401 routes to login, and 200 reloads the page to establish a new control socket. Only one recovery loop may exist.
- XHR upload 401 also routes to login; unrelated upload errors retain current UI behavior. STOP and microphone logic must not auto-resume while auth recovery is active.

- [ ] **Step 1: Write the failing Node login harness**

In `tests/test_browser_device_access.py`, spawn `node -e` and load the actual inline script from `login.html` into `vm`. Provide deterministic DOM, `location`, `history`, storage, timer, and fetch fakes. Use separate one-behavior scenarios:

```text
fragment is removed before POST and POST body contains split ID/secret
success stores durable credential plus returned fresh session values
stored durable credential reauthenticates after a simulated reload/restart
401 clears durable only and performs exactly one attempt
429/network error preserves durable and malformed fragment never posts
manual six-character PIN remains functional and never writes durable storage
```

Assert against calls/state, not source text.

- [ ] **Step 2: Run the login harness and verify RED**

Run: `/root/.hermes/cache/scratch/marklv-task1-venv/bin/python -m unittest tests.test_browser_device_access -v`

Expected: FAIL because current login expects `jarvis_device_token`, does not parse a fragment, and clears it after restart.

- [ ] **Step 3: Implement fragment consumption and durable re-exchange**

Keep the existing manual PIN form and secure Safari help. Remove the volatile `device_token` contract. Ensure all display/error paths avoid inserting the credential, secret, verifier, or full URL into DOM text, console calls, or thrown errors.

- [ ] **Step 4: Run the login harness and verify GREEN**

Run: `/root/.hermes/cache/scratch/marklv-task1-venv/bin/python -m unittest tests.test_browser_device_access -v`

Expected: all login scenarios PASS.

- [ ] **Step 5: Write the failing Node stale-tab/auth-expiry harness**

Load the actual `app.html` script in Node `vm` and add scenarios:

```text
HTTP 401 removes only token/key, preserves durable credential, and routes /login
control or audio WebSocket 4001 performs the same transition once
abrupt close + network failure + restarted-server 401 reaches /login without a loop
abrupt close + status 200 reloads once and creates no parallel recovery loop
recovery disables/stops microphone and cannot trigger automatic resume
upload XHR 401 uses the same auth transition
```

Retain the existing real Node half-duplex lifecycle assertions; adapt its storage/WebSocket mocks to the new helpers rather than weakening them.

- [ ] **Step 6: Run stale-tab tests and verify RED**

Run:

```bash
/root/.hermes/cache/scratch/marklv-task1-venv/bin/python -m unittest tests.test_browser_device_access tests.test_dashboard_headless.BrowserVoiceTests -v
```

Expected: stale-tab tests FAIL because current `ws.onclose` only prints refresh guidance and authenticated fetches ignore 401.

- [ ] **Step 7: Implement centralized browser auth recovery**

Make `_authFetch` async so it can inspect response status while preserving callers' response semantics. Route control/audio close handling through one idempotent transition. Cancel recovery timers on page hide/navigation. Do not reconnect microphone or issue a new WebSocket until a fresh page/session exists.

- [ ] **Step 8: Verify browser behavior and syntax with Node**

Run:

```bash
/root/.hermes/cache/scratch/marklv-task1-venv/bin/python -m unittest tests.test_browser_device_access tests.test_dashboard_headless.BrowserVoiceTests -v
/root/.hermes/cache/scratch/marklv-task1-venv/bin/python -c "from pathlib import Path; import re; p=Path('/root/.hermes/cache/scratch'); [(p/f'{n}.js').write_text('\\n'.join(re.findall(r'<script>([\\s\\S]*?)</script>', Path(f'dashboard/static/{n}.html').read_text())), encoding='utf-8') for n in ('login','app')]"
node --check /root/.hermes/cache/scratch/login.js
node --check /root/.hermes/cache/scratch/app.js
```

Expected: Node behavior harnesses PASS and both syntax checks exit 0.

- [ ] **Step 9: Run the complete suite and commit**

Run: `/root/.hermes/cache/scratch/marklv-task1-venv/bin/python -m unittest discover -s tests -v`

Expected: PASS.

```bash
git add dashboard/static/login.html dashboard/static/app.html tests/test_browser_device_access.py tests/test_dashboard_headless.py
git diff --cached --check
git commit -m "feat: recover durable device access in Safari"
```

### Task 6: Document operation and complete local/server acceptance

**Files:**
- Modify: `docs/server-voice-quickstart.md:15-28, 51-80, 89-97`
- Modify: `tests/test_final_lifecycle.py`
- Modify only if acceptance discovers a documented/configuration mismatch: `deploy/jarvis.service`, `requirements-headless.txt`

**Interfaces:**
- Documentation uses only safe IDs/labels and paths; never paste `.url` contents, QR payloads, bearer values, salts, or verifiers.
- The deployed active set ends with exactly four labels: `LaCasa iPhone 1`, `LaCasa iPhone 2`, `LaCasa iPhone 3`, `LaCasa iPhone 4`. Revoked historical rows may remain.
- Automated acceptance distinguishes protocol/server evidence from the final physical iPhone/Safari scan and listening check required by the design.

- [ ] **Step 1: Write failing final-lifecycle assertions for production wiring**

Extend `tests/test_final_lifecycle.py` with focused assertions that default/env-configured store and QR paths reach `DashboardServer`, unsafe default stores surface as exit 78 before model connection, the control socket remains mode `0600`, a second server instance reauthenticates from the same store, and no device-admin HTTP route exists. Extend service-file assertions for both explicit environment paths and existing `UMask=0077`/`RestartPreventExitStatus=78`.

- [ ] **Step 2: Run lifecycle tests and verify RED, then make only necessary wiring fixes**

Run: `/root/.hermes/cache/scratch/marklv-task1-venv/bin/python -m unittest tests.test_final_lifecycle -v`

Expected: new wiring assertions fail first, then PASS after only the smallest configuration propagation fixes. Do not alter model/audio behavior.

- [ ] **Step 3: Update the operator guide**

Replace the claim that every QR/pair is one-use and globally rotating. Document:

- Tailscale/Safari fragment QR flow and automatic durable re-login after bearer expiry/restart.
- The four exact `device-create` commands, `device-list`, and `device-revoke DEVICE_ID` over the existing socket.
- QR/URL artifact modes/location, scan-and-private-retention guidance, and revoke-then-create replacement procedure.
- One-hour in-memory bearer behavior and stale-tab recovery.
- Emergency `pair` as ten-minute transient access that does not revoke durable devices.
- No phone/HTTP administration endpoint; device operations require local Unix-socket access.
- Exit 78 remediation for corrupt/unsafe store or QR path.
- Final physical iPhone check remains manual and must not be claimed from Node/HTTP tests.

Do not embed a real device ID, URL fragment, secret, bearer, or QR image.

- [ ] **Step 4: Run complete local verification from the pinned environment**

Run:

```bash
/root/.hermes/cache/scratch/marklv-task1-venv/bin/python -m unittest discover -s tests -v
/root/.hermes/cache/scratch/marklv-task1-venv/bin/python -m compileall -q core dashboard tests
/root/.hermes/cache/scratch/marklv-task1-venv/bin/python -c "from pathlib import Path; import re; p=Path('/root/.hermes/cache/scratch'); [(p/f'{n}.js').write_text('\\n'.join(re.findall(r'<script>([\\s\\S]*?)</script>', Path(f'dashboard/static/{n}.html').read_text())), encoding='utf-8') for n in ('login','app')]"
node --check /root/.hermes/cache/scratch/login.js
node --check /root/.hermes/cache/scratch/app.js
git diff --check
git status --short
git grep -nE 'jarvis_device_token|/api/revoke-devices|[?&]device=' -- dashboard core
git check-ignore --no-index config/device_credentials.json config/device_qr/example.url config/device_qr/example.png
```

Expected: all tests/syntax/compile/whitespace checks PASS; grep returns no obsolete volatile-token/admin-route/query-credential implementation; ignore check exits 0. Review failures by name rather than suppressing them.

- [ ] **Step 5: Commit documentation and final wiring selectively**

```bash
git add docs/server-voice-quickstart.md tests/test_final_lifecycle.py
git add deploy/jarvis.service requirements-headless.txt  # only if changed in this task
git diff --cached --check
git commit -m "docs: document durable LaCasa device access"
```

Confirm `plugins/cannabis_status.py` and `DASHBOARD_GUIDE.md` are not staged.

- [ ] **Step 6: Review the complete implementation before deployment**

Run:

```bash
git diff 0045027..HEAD -- core/device_credentials.py dashboard/access.py dashboard/server.py core/headless.py dashboard/static/login.html dashboard/static/app.html tests requirements-headless.txt deploy/jarvis.service .gitignore docs/server-voice-quickstart.md
git log --oneline 0045027..HEAD
```

Check every Review Focus item against its named test, verify all control responses are secret-free, and verify every production behavior was preceded by a witnessed RED test. Do not deploy a dirty tree or an unreviewed fix.

- [ ] **Step 7: Deploy the exact reviewed commit to `lacasa-server`**

Using the already approved deployment mechanism, install `requirements-headless.txt`, place the updated user unit, run `systemctl --user daemon-reload`, and restart only `jarvis` (not Raspotify, PipeWire, or Spotify ducking). Record the reviewed Git SHA and verify the deployed checkout reports exactly that SHA. Never copy credential artifacts through chat or a command whose output is captured in a report.

- [ ] **Step 8: Verify service/security baseline before creating credentials**

On `lacasa-server`, verify without printing secrets:

```bash
systemctl --user is-active jarvis
systemctl --user status jarvis --no-pager
ss -ltn | grep '127.0.0.1:8000'
stat -c '%a %U %F' /run/user/1000/jarvis/control.sock
curl -s -o /dev/null -w 'HTTPS %{http_code} TLS %{ssl_verify_result}\n' https://lacasa-server.tail7a9c2.ts.net/
curl -s -o /dev/null -w 'STATUS %{http_code}\n' https://lacasa-server.tail7a9c2.ts.net/api/status
curl -s -o /dev/null -w 'ADMIN %{http_code}\n' -X POST https://lacasa-server.tail7a9c2.ts.net/api/revoke-devices
```

Expected: active service, loopback-only listener, socket `600` owned by `lacasa`, HTTPS `200`/TLS `0`, unauthenticated status `401`, removed admin route `404`.

- [ ] **Step 9: Create the initial four credentials through the private CLI**

First run `device-list`. If unknown active records exist, stop and reconcile them rather than deleting/overwriting the store. Then run as user `lacasa`:

```bash
cd /home/lacasa/Mark-LV-LaCasa
.venv/bin/python -m core.headless device-create --label 'LaCasa iPhone 1' --socket /run/user/1000/jarvis/control.sock
.venv/bin/python -m core.headless device-create --label 'LaCasa iPhone 2' --socket /run/user/1000/jarvis/control.sock
.venv/bin/python -m core.headless device-create --label 'LaCasa iPhone 3' --socket /run/user/1000/jarvis/control.sock
.venv/bin/python -m core.headless device-create --label 'LaCasa iPhone 4' --socket /run/user/1000/jarvis/control.sock
.venv/bin/python -m core.headless device-list --socket /run/user/1000/jarvis/control.sock
```

Expected: CLI output contains safe IDs/labels/timestamps and artifact paths only; four active exact labels. Verify `config/device_credentials.json` is owner `lacasa`, mode `600`; `config/device_qr` is mode `700`; every `.url`/`.png` is mode `600`; store text contains no value extracted as a URL secret. Do this comparison in a local script that emits only pass/fail, never the value.

- [ ] **Step 10: Prove restart persistence, stale bearer rejection, and independent revocation on the real server**

Use a temporary mode-`0700` local acceptance script under `/run/user/1000/jarvis/` that reads each `.url` file internally, splits the fragment in memory, POSTs `device_id`/`secret`, and prints only label plus HTTP status. For all four: login 200, retain one bearer in memory, restart `jarvis`, verify the old bearer gets 401, and re-exchange all four durable credentials for 200. Delete the script afterward.

Revoke `LaCasa iPhone 4` by its safe ID, verify its stored credential gets 401 while iPhones 1-3 still get 200, then create a replacement with the exact label `LaCasa iPhone 4` and verify it gets 200. Final `device-list` must show exactly four active labels plus the historical revoked row. Do not print request bodies, fragments, secrets, bearer tokens, or URL-file contents.

- [ ] **Step 11: Check logs and live socket effects without disclosing credentials**

Keep control and phone-audio WebSockets open for two test devices, revoke one, and verify only that device receives 4001 and its upstream phone stream closes; the other remains usable. Run a local comparison script that reads secrets from private `.url` files and scans `journalctl --user -u jarvis` in memory, returning only pass/fail if any secret or `#device=` payload occurs. Confirm emergency `python -m core.headless pair` leaves the four durable logins valid. The explicit transient URL itself must not be copied into the acceptance report.

- [ ] **Step 12: Perform the physical iPhone handoff and report honest acceptance**

Privately present each QR to its intended iPhone. On at least one already-open Safari tab, restart JARVIS and verify the page routes through `/login`, re-exchanges the local durable credential, and reconnects without rescanning. Verify a revoked old QR shows manual login once rather than looping. Then perform the design's microphone/STOP/listening check. Report automated server/protocol results separately from physical Safari/audio results; if the user has not performed the latter, mark it explicitly pending rather than calling the system complete.
