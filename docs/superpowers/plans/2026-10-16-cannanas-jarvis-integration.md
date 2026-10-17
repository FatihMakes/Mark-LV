# Cannanas JARVIS Integration Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Integrate the pinned Cannanas MCP adapter into the Python Mark-LV headless JARVIS stack on `lacasa-server`: one trusted `cannanas` Gemini tool with a closed action enum, immutable ingress provenance, a host-side confirmation coordinator with atomic consume, a persistent serialized MCP client session, and a fail-closed write audit — without disturbing JARVIS conversation, Spotify control/ducking, Home Assistant reads, or phone authentication.

**Architecture:** The pinned adapter package (sibling plan `2026-10-16-cannanas-mcp-adapter.md`) runs as **one persistent local STDIO child** of JARVIS. `plugins/cannanas.py` is the single model-facing door: a closed `action` enum dispatching through `core/cannanas_client.py` (persistent MCP client on a dedicated loop) toward the adapter. `core/cannanas_host.py` owns the confirmation coordinator (`core/cannanas_confirmation.py`), the deterministic German confirmation grammar, the boot/restart fences, and degraded readiness — `confirm_pending`/`cancel_pending` exist **only** at the host; they are never in the model-facing enum. `core/cannanas_audit.py` writes the fail-closed JSONL audit before every POST.

**Tech Stack:** Python 3.11+, official `mcp` Python SDK client (pinned per release in `requirements-headless.txt`), Gemini Live plugin registry, `unittest` via the pinned runner, systemd user service + mode-`0600` credential env file.

**Spec:** `docs/superpowers/specs/2026-10-16-cannanas-jarvis-integration-design.md` (authoritative).

**Prerequisite:** `docs/superpowers/plans/2026-10-16-cannanas-mcp-adapter.md` Task 1–7 complete: adapter release `1.0.0` built with wheel SHA-256 and hashed constraints lock.

**Runner:** `/root/.hermes/cache/scratch/marklv-task1-venv/bin/python -m unittest discover -s tests -v`. If that venv has been pruned (the scratch directory prunes after 24 h idle), recreate it first from `requirements-headless.txt`; never substitute a different interpreter.

**Base commit:** `feat/jarvis-integration` @ `ae53d70` (293 tests green). Task 1 merges it into `design/cannanas-jarvis` before any Cannanas code lands.

## Global Constraints

- The adapter child runs on `lacasa-server` only; `lacasa15` stays the Hermes/Cannanas development host. `lacasa15` may be offline without breaking Cannanas reads.
- Launch the child via the pinned wheel's entry point at an **absolute path** (its dedicated venv python, e.g. `/opt/cannanas/adapter/venv/bin/cannanas-mcp-adapter`) with a **minimal environment** (only `CANNANAS_API_KEY`, `CANNANAS_CLUB_ID`, `CANNANAS_STATE_DIR`, `CANNANAS_AUDIT_FILE` plus a bare PATH). No listening ports, no Tailscale endpoint.
- Model-facing `cannanas` enum (exact Release 1 set): `overview`, `find_zones`, `find_strains`, `find_plants`, `find_propagation_material`, `list_recent_batches`, `list_plants`, `count_plants_by_room`, `prepare_plant_create`, `prepare_strain_create`, `prepare_batch_create`, `prepare_plant_update`, `prepare_batch_update`, `prepare_diary_create`. **No** `confirm_pending`, **no** `cancel_pending`, no raw MCP tool names, HTTP methods, URLs, club IDs or confirmation tokens ever reach Gemini.
- Existing services stay green: Spotify (OAuth, playback, ducking), HA reads, half-duplex, phone auth. Adapter failure/absence degrades Cannanas only, with one bounded spoken German notice.
- Durable device provenance: `stable_device_id` (from `core/device_credentials.py`), `auth_session_id` (from `dashboard/access.py` `SessionRecord`), `capture_id` (the existing audio epoch/capture tuple), `user_turn_id` (finalized-turn identity) flow from ingress through `_execute_tool` into the Cannanas plugin. Bearer refresh changes `auth_session_id` but retains `stable_device_id`; device revocation invalidates pending approval.
- Confirmation coordinator is the single owner of pending state; one pending action per JARVIS session; a new prepare cancels the previous; TTL ≤ 10 min; consume is atomic (pending → executing) before any MCP confirm call.
- Restart fences: adapter restart **or** JARVIS restart invalidates ordinary pending confirmations; post-restart confirmation requires a fresh preview. Retry rules: reads restart-retry once with bounded backoff; prepare (no bytes possibly sent) may retry after a verified re-prepare; confirm never auto-retries once bytes may have been sent — result is `unknown` until reconciled.
- Audit: secret-free append-only JSONL, mode `0640`, fsynced; for writes an entry is committed **before** the POST and audit failure fails the write closed. Host audit is separate from and additional to the adapter's own audit.
- Missing Cannanas configuration fails **before network I/O** naming only the missing environment variable (e.g. `CANNANAS_API_KEY`); no credential values anywhere — Git, prompts, tool results, confirmation state visible to Gemini, logs, dashboard history, audit or speech.
- 401/403 never retried; 429 honors a valid bounded `Retry-After` for at most one read, never a write; redirects rejected; malformed/oversized responses rejected without echoing bodies; TLS verification always on; fixed origin enforced by the adapter (host pins it in config too).
- Work in `/root/Mark-LV-LaCasa/.worktrees/cannanas-integration` on branch `design/cannanas-jarvis`. Never modify `/home/lacasa15/services/cannanas-mcp/` or the Mark-LV `main` checkout.
- Preserve all existing tests; every task is test-first and ends with a focused commit of only its named paths.
- The `cannabis_status` plugin is denied at the headless gate (`core/headless.py` `tool_allowed`) and never enters `trusted_headless_registry()`.

## Review Focus

1. Provenance chain: HTTP text, control-WebSocket text and phone PCM each preserve stable device, auth session, capture and user-turn identity through tool dispatch; interleaved devices cannot confirm another device's preview.
2. Enum closure: only the exact closed enum reaches the dispatcher; `confirm*`/`cancel*`/arbitrary actions denied before dispatch — pinned in Task 3 and re-asserted in Task 6.
3. Atomic consume under concurrency: two racing confirmations from the same finalized turn yield exactly one MCP confirm call; pin in Task 4/6.
4. Audit-before-POST fail-closed: missing/readonly audit blocks every write; pin in Task 5.
5. Service isolation: adapter crash, hang (25 s budget), or missing config never exits JARVIS, Spotify, or HA; pin in Task 7.

---

### Task 1: Base merge into the integration branch

**Files:** none (merge only).

- [ ] **Step 1: Merge the green base**
  - `git merge feat/jarvis-integration` (expect the Spotify/device-credentials/headless state at `ae53d70` without doc conflicts; `docs/` conflicts resolve in favor of the integration branch).

- [ ] **Step 2: Verify the runner suite is unchanged green**
  - `/root/.hermes/cache/scratch/marklv-task1-venv/bin/python -m unittest discover -s tests -v`
  - Expected: 293 tests, no failures.

- [ ] **Step 3: Commit (merge commit, no staged content)**
  - `git commit` — `merge: base cannanas integration on feat/jarvis-integration (ae53d70)`

---

### Task 2: Immutable ingress provenance envelope

**Files:**
- Create: `core/turn_provenance.py`
- Modify: `dashboard/server.py` — HTTP text endpoint, control-WebSocket text messages, phone audio WebSocket
- Create: `tests/test_ingress_provenance.py`

**Interfaces:**
- `core/turn_provenance.py`: frozen dataclass `IngressEnvelope {stable_device_id, auth_session_id, capture_id, user_turn_id, channel, final: bool}` with `channel ∈ {"http_text", "control_ws_text", "phone_pcm"}`; `create_stable(...)` stamps a fresh `user_turn_id`; `validate(envelope)` fails closed on mixed/revoked/superseded/unknown sources.
- `DashboardServer` already holds `_socket_tokens` (WebSocket → bearer token) and per-device `SessionRecord`s. Attach the envelope where each ingress source is accepted: the text endpoint, the control-WebSocket text path, and the phone audio path (the existing audio `epoch`/`capture` tuple becomes `capture_id`).
- `stable_device_id` comes from the durable credential that owns the bearer session (re-auth keeps the durable identity; revocation closes the session as today).
- Nothing is stored plaintext beyond what today's session/tokan state already holds; the envelope must never contain bearer secrets.

- [ ] **Step 1: Write failing provenance tests**
  - `TestHTTPTextEnvelopeCarriesStableDevice`, `TestControlWSTextEnvelopeCarriesAuthSession`, `TestPhonePCMEnvelopeCarriesCaptureID`, `TestRevokedDeviceRejected`, `TestBearerRefreshRetainsStableDevice`, `TestEnvelopeNeverContainsSecrets`.

- [ ] **Step 2: Implement envelope attachment** — pure additive plumbing in `dashboard/server.py`; no behavior change beyond attaching metadata.

- [ ] **Step 3: Run tests GREEN**
  - `/root/.hermes/cache/scratch/marklv-task1-venv/bin/python -m unittest tests.test_ingress_provenance -v`

- [ ] **Step 4: Commit**
  - `git add core/turn_provenance.py dashboard/server.py tests/test_ingress_provenance.py`
  - `git commit -m "feat(cannanas): attach immutable ingress provenance envelope"`

---

### Task 3: Turn finalization and tool-dispatch provenance

**Files:**
- Create: `core/user_turn.py`
- Modify: `main.py` — `_execute_tool` and the finalized-turn registration point
- Modify: `core/headless.py` — `tool_allowed()` gains the closed `cannanas` action gate
- Create: `tests/test_turn_finalization.py`

**Interfaces:**
- `core/user_turn.py`: `FinalizedTurn {envelope, finalized_at, identity}`; `turn_registry` tracks the currently finalized turn per audio/text stream.
- Phone PCM (`dashboard/server.py`) and dashboard text finalize into a `FinalizedTurn` **before** any confirmation decision; the `user_turn_id` is monotonically increasing per session and stable through the turn.
- In `main.py`, the current finalized turn is stored on the session; `_execute_tool` passes it to `self._plugin_registry.run(name, args, player=self.ui, session_memory=<finalized-turn context>)` instead of `session_memory=None` — plugins that declare a provenance-aware signature receive it; other plugins keep working unchanged (`core/plugin_loader._call_run` only injects kwargs the plugin actually declares).
- A Gemini tool call (or any tool result) arriving before finalization of the current input is rejected with a fixed response and cannot count as a user turn.
- `core/headless.py` `tool_allowed()`: adds `cannanas` gated on the **closed action enum** (imported from `plugins/cannanas.py` once Task 6 lands; until then the enum constant lives in `core/cannanas_actions.py` created in this task so the gate is testable).
  - `core/cannanas_actions.py`: `CANNANAS_READ_ACTIONS`, `CANNANAS_PREPARE_ACTIONS`, `CANNANAS_MODEL_ACTIONS = READ + PREPARE` — the exact Global-Constraints list. `confirm_pending`/`cancel_pending` are explicitly **not** defined here (they live in `core/cannanas_confirmation.py`).

- [ ] **Step 1: Write failing finalization tests**
  - `TestFinalizedTurnIdentityStable`, `TestToolCallBeforeFinalizationRejected`, `TestDispatchReplacesSessionMemoryNoneForHeadless`, `TestResponseGenerationIsNotUserTurnIdentity`, `TestToolAllowedRejectsUnknownCannanasAction`, `TestConfirmPendingNotInModelEnum`.

- [ ] **Step 2: Implement** — finalization registration in `main.py` at the PCM/text ingest points; context propagation in `_execute_tool`; the closed enum + gate in `core/cannanas_actions.py` and `core/headless.py`.

- [ ] **Step 3: Run tests GREEN**
  - `/root/.hermes/cache/scratch/marklv-task1-venv/bin/python -m unittest tests.test_turn_finalization tests.test_headless tests.test_dashboard_headless -v`

- [ ] **Step 4: Commit**
  - `git add core/user_turn.py core/cannanas_actions.py core/headless.py main.py tests/test_turn_finalization.py`
  - `git commit -m "feat(cannanas): finalize user turns and gate the closed action enum"`

---

### Task 4: Persistent serialized MCP client session

**Files:**
- Create: `core/cannanas_client.py`
- Create: `tests/fixtures/fake_adapter.py` (minimal FastMCP STDIO server for tests, dummy tool set)
- Create: `tests/test_cannanas_client.py`
- Modify: `requirements-headless.txt` — add pinned `mcp>=1.9,<2`

**Interfaces:**
- `CannanasClient` runs one dedicated asyncio loop thread; `call(tool, arguments, timeout)` and lifecycle methods are synchronous/thread-safe and serialize through a single queue — **one** persistent client session and one child process for the whole JARVIS lifetime, never per tool call.
- `start(config)`: launches the pinned entry point (absolute venv path from config), performs MCP `initialize` within a **10 s** budget after transport start, lists tools and verifies an **exact expected set** (adapter's frozen 21-tool list), then calls `get_adapter_info` and verifies `adapter_version == pinned release` and `artifact_sha256 == pinned wheel SHA` (both from Mark-LV config; on mismatch, Cannanas degrades read-only-safe: refuse all prepare/confirm, allow reads only).
- `call(...)` enforces the **25 s** per-tool budget; the synchronous plugin executor bridges via `Future`-style handoff.
- Restart policy (`on_child_exit`): exactly one restart with bounded backoff (e.g. 1 s, then give up); after restart, pending confirmations are invalidated (coordinator hook) and the initialize/version verification repeats; if restart fails, mark Cannanas degraded, never kill JARVIS.
- Child environment passes only the four adapter variables plus a bare PATH; TLS/origin policy is enforced adapter-side (client config only pins release/SHA and budgets).

- [ ] **Step 1: Write failing client tests** (against `tests/fixtures/fake_adapter.py`)
  - `TestInitBudget10s`, `TestToolBudget25s`, `TestSinglePersistentSession`, `TestExactToolSetVerified`, `TestVersionAndSHAPinned`, `TestChildExitRestartsOnce`, `TestRestartInvalidatesCoordinatorHook`, `TestMissingBinaryDegradesNotCrashes`.

- [ ] **Step 2: Implement** — client class + fixture; wire the coordinator-invalidated hook via a callback (defined as a stub; Task 6 supplies it).

- [ ] **Step 3: Run tests GREEN**
  - `/root/.hermes/cache/scratch/marklv-task1-venv/bin/python -m unittest tests.test_cannanas_client -v`

- [ ] **Step 4: Commit**
  - `git add core/cannanas_client.py tests/fixtures/fake_adapter.py tests/test_cannanas_client.py requirements-headless.txt`
  - `git commit -m "feat(cannanas): persistent serialized MCP client with budgets and restart fence"`

---

### Task 5: Fail-closed audit log

**Files:**
- Create: `core/cannanas_audit.py`
- Create: `tests/test_cannanas_audit.py`

**Interfaces:**
- `CannanasAudit(path)`: append-only JSONL, file mode `0640`, parent dir `0700`, fsync per entry, `open(..., 'a')` with O_NOFOLLOW semantics; refuses symlinked/mis-permissioned targets.
- `record(event, *, action, payload_hash, preview, stable_device_id, auth_session_id, user_turn_id, state, result_id)`.
- Entry schema (secret-free): `{timestamp, device_id (pseudonymous stable id), auth_session_id, user_turn_id, action, payload_hash, preview, state, result_id}`.
- `pre_post(action, ...)` context manager: for **write** actions, the `confirmed` transition entry is durably committed **before** the MCP confirm call; a write attempt with unavailable audit raises and the plugin returns a fixed German failure (`Cannanas-Schreibprüfung nicht verfügbar`) with zero POSTs.
- For reads, audit failure is non-fatal and produces exactly one bounded operator warning.
- Never persisted: bearer values, MCP confirmation tokens/nonces, durable phone secrets, Gemini session material, raw API responses.

- [ ] **Step 1: Write failing audit tests**
  - `TestAuditAppends0640`, `TestAuditSecretFree`, `TestAuditPreCommitBeforePost`, `TestAuditUnavailableFailsWriteClosed`, `TestReadAuditFailureDegradesWithWarning`, `TestSymlinkedAuditRefused`.

- [ ] **Step 2: Implement** — audit module only; plugin wiring is Task 7.

- [ ] **Step 3: Run tests GREEN**
  - `/root/.hermes/cache/scratch/marklv-task1-venv/bin/python -m unittest tests.test_cannanas_audit -v`

- [ ] **Step 4: Commit**
  - `git add core/cannanas_audit.py tests/test_cannanas_audit.py`
  - `git commit -m "feat(cannanas): fail-closed 0640 audit written before POST"`

---

### Task 6: Host-side confirmation coordinator with atomic consume

**Files:**
- Create: `core/cannanas_confirmation.py`
- Create: `core/cannanas_host.py` (skeleton: boot id, coordinator + grammar + fences entry points; full wiring in Task 7)
- Create: `tests/test_confirmation_coordinator.py`

**Interfaces:**
- `PendingConfirmation` — exactly the design's owner record: `{action, club_id, payload_hash, complete_preview, jarvis_session_id, stable_device_id, auth_session_id, prepared_turn_id, adapter_boot_id, created_at, expires_at (≤ 600 s), state ∈ {pending, executing, consumed, expired, cancelled}, opaque_mcp_token}`. `opaque_mcp_token` is repr-redacted and never serialized.
- `ConfirmationCoordinator` (thread-safe):
  - `store(pending)` — one pending per `jarvis_session_id`; storing a new one cancels the previous (`cancel_pending` path).
  - `consume(jarvis_session_id, turn_context, expected)` — atomic compare-and-swap `pending → executing` **before** the adapter confirm call; returns the pending or a typed refusal (`wrong_session`, `wrong_device`, `wrong_auth_session`, `wrong_turn`, `wrong_boot`, `wrong_payload`, `expired`, `already_used`, `cancelled`). Replays/duplicates lose the CAS and produce zero adapter calls.
  - `invalidate_all_on_adapter_restart()`, `mark_consumed(...)`, `cancel(...)`; expiry lazily enforced on every access.
  - Fence hooks: called from the client's restart path (Task 4) and from JARVIS startup — process-lifetime in-memory + persistence of nothing; a JARVIS restart therefore fences by construction.
- `core/cannanas_host.py` skeleton: `adapter_boot_id()` (fresh per JARVIS process, supplied to prepare/confirm), `confirmation_grammar(text) -> {"affirm" | "deny" | "none"}` — the deterministic bounded German affirmative/cancel grammar (e.g. exact "ja", "bestätigen", "abbrechen", "nein" forms with bounded variants; everything else `none`, no fuzzy matching).

- [ ] **Step 1: Write failing coordinator tests**
  - `TestStoreReplacesPreviousPending`, `TestConsumeAtomicExactlyOneExecutor` (two threads, one wins, one typed refusal), `TestConsumeRejectsWrongSessionDeviceTurnBoot`, `TestConsumeRejectsExpiredAndReplay`, `TestCancelClearsPending`, `TestTokenNeverVisible`, `TestRestartInvalidatesAll`, `TestGrammarBounded`.

- [ ] **Step 2: Implement** — coordinator; keep it free of network imports so unit tests stay fast.

- [ ] **Step 3: Run tests GREEN**
  - `/root/.hermes/cache/scratch/marklv-task1-venv/bin/python -m unittest tests.test_confirmation_coordinator -v`

- [ ] **Step 4: Commit**
  - `git add core/cannanas_confirmation.py core/cannanas_host.py tests/test_confirmation_coordinator.py`
  - `git commit -m "feat(cannanas): host-side confirmation coordinator with atomic consume"`

---

### Task 7: Wire the `cannanas` plugin to client, coordinator, and audit

**Files:**
- Create: `plugins/cannanas.py`
- Modify: `core/headless.py` — `trusted_headless_registry()` imports `plugins.cannanas` as the third trusted plugin (same `_runner` pattern as `home_assistant`/`spotify_control`)
- Modify: `core/cannanas_host.py` — full lifecycle: config checks, adapter lifecycle, prepare preview flow, host confirm flow, degraded readiness
- Modify: `main.py` — after each finalized turn, host confirmation-grammar check runs **before** normal LLM turn handling when a pending confirmation exists for that session/device
- Create: `tests/test_cannanas_plugin.py`, `tests/test_cannanas_integration.py`
- Modify: `core/headless.py` `tool_allowed()` — replaces the Task 3 stub enum with `plugins.cannanas.CANNANAS_MODEL_ACTIONS`

**Interfaces:**
- `PLUGIN`: name `cannanas`, description (bounded German), `behavior: "NON_BLOCKING"` is **not** set (confirmations must be synchronous when they occur; read actions are fast and bounded), `parameters` with a single `action` STRING property carrying the **closed enum** `CANNANAS_MODEL_ACTIONS` (Task 3's `core/cannanas_actions.py` values), `required: ["action"]`, plus small typed parameter properties the model fills (e.g. `query`, `room`, `status`, `plant_name`, `count`, `text`); no free-form params.
- `run(parameters, player=None, session_memory=None)`:
  - Validates `action` against the closed enum; unknown → fixed refusal (defense in depth behind `tool_allowed`).
  - Read actions → `cannanas_client.call(adapter_tool, args)`, project compact fields, return the German summary payload with the `exact`/truncation flags intact (speech behavior: ≤ 5 spoken records, truncation notice — the plugin returns the flag, the model speaks).
  - `prepare_*` → resolve/validate via adapter `prepare_*` tool with the full binding context (`jarvis_session_id` from the finalized turn, `stable_device_id`, `auth_session_id`, `prepared_turn_id`, club/action/payload hash from the adapter response, `adapter_boot_id`); on success the coordinator stores the pending (dropping the opaque token into host-only state) and returns the complete preview text plus a fixed "bitte bestätigen" instruction.
  - Any `confirm*`/`cancel*` action string → refusal **before** registry dispatch as well (`tool_allowed`), belt and braces.
- Host confirm flow (outside the model path): after a finalized turn with pending confirmation, `cannanas_host.evaluate(turn)` runs the bounded grammar; on affirm → `audit.pre_post(write)` commit → `coordinator.consume(...)` → `cannanas_client.call(confirm_<action>, {confirmation_token: opaque_mcp_token, binding: {...}})` → read-back-by-ID result classification (`ok` / `created_but_unverified` / `unknown` / `failed`); never an automatic retry. On deny/none → cancel pending or leave untouched.
- Degraded readiness: missing vars (`JARVIS_CANNANAS_*` for host, adapter env vars pinned in deploy), client absence, or version/SHA mismatch → Cannanas returns fixed German unavailability and JARVIS, Spotify, HA keep running; state observable in the activity log without secrets.
- Restart/retry rules (design *Packaging...* §6): read → one restart + one retry when no response may have been committed; prepare → restart invalidates preview, new prepare required; confirm → never retried, `unknown` until reconciled.

- [ ] **Step 1: Write failing plugin + integration tests** (client mocked with the Task 4 fixture or a stub)
  - `TestEnumClosedAtRegistry`, `TestReadActionCallsAdapterAndProjects`, `TestPrepareStoresPendingAndReturnsPreview`, `TestPrepareZeroPOSTs`, `TestHostConfirmConsumesAtomicallyAndPostsOnce`, `TestHostConfirmWrongSessionRefused`, `TestPostSendTimeoutIsUnknownNotRetried`, `TestRegistryRestartInvalidatesPending`, `TestDegradedCannanasKeepsJARVISAlive`, `TestMissingConfigFailsBeforeNetwork`, `TestCannabisStatusStillDenied`.

- [ ] **Step 2: Implement** — plugin + host wiring; keep `plugins/cannanas.py` free of direct `httpx`/REST imports (adapter is the only network path).

- [ ] **Step 3: Run tests GREEN**
  - `/root/.hermes/cache/scratch/marklv-task1-venv/bin/python -m unittest tests.test_cannanas_plugin tests.test_cannanas_integration -v`

- [ ] **Step 4: Run the full suite** — `... -m unittest discover -s tests -v`; expected: 293 + new, all green (Spotify/HA/phone/half-duplex untouched).

- [ ] **Step 5: Commit**
  - `git add plugins/cannanas.py core/headless.py core/cannanas_host.py main.py tests/test_cannanas_plugin.py tests/test_cannanas_integration.py`
  - `git commit -m "feat(cannanas): wire trusted plugin to adapter client, coordinator, and audit"`

---

### Task 8: Deployment artifacts and degraded-readiness observability

**Files:**
- Create: `deploy/cannanas/` — `README-DEPLOY.md`, `install-adapter.sh`, `cannanas-adapter.credentials` (template, gitignored)
- Modify: `deploy/jarvis.service` — add the four adapter env lines sourced from the mode-`0600` credential file (EnvironmentFile), unit description note
- Modify: `requirements-headless.txt` — final pin comment for the JARVIS-side `mcp` client dependency
- Create: `tests/test_deployment_cannanas.py`
- Modify: `.gitignore` — `deploy/cannanas/*.credentials`

**Interfaces:**
- Adapter artifact placement on `lacasa-server`: dedicated venv under `/opt/cannanas/adapter/venv`, wheel installed from the pinned release manifest wheel + hashed constraints, plus the release sidecar `release_info.json` (wheel SHA) next to the entry point; service runs the entry-point path as absolute executable.
- Credential file (`CANNANAS_API_KEY`, `CANNANAS_CLUB_ID`), mode `0600`, owned by the service UID — the *new* JARVIS-dedicated Cannanas API key, not the Hermes key; never in Git, service logs or reports.
- `install-adapter.sh`: verifies wheel SHA-256 against the manifest before installing; fails closed on mismatch; starts nothing by itself (the JARVIS unit starts the child).
- `deploy/jarvis.service` unchanged in every other way; no new listening sockets.
- Tests: manifest/SHA verification logic, credential-file mode/ownership checks, `jarvis.service` still parses, env template lists only the four adapter variables.

- [ ] **Step 1: Write failing deployment tests** — `TestWheelShaVerified`, `TestCredentialFileChecks`, `TestUnitStillParses`, `TestNoNewSockets`, `TestEnvTemplateMinimal`.

- [ ] **Step 2: Implement artifacts** — write `deploy/cannanas/` files + `.gitignore` + service edit.

- [ ] **Step 3: Run tests GREEN** — `/root/.hermes/cache/scratch/marklv-task1-venv/bin/python -m unittest tests.test_deployment_cannanas -v`.

- [ ] **Step 4: Commit**
  - `git add deploy/cannanas/ deploy/jarvis.service requirements-headless.txt .gitignore tests/test_deployment_cannanas.py`
  - `git commit -m "feat(cannanas): deployment artifacts and pinned artifact verification"`

---

### Task 9: Acceptance, secret scan, and documentation

**Files:**
- Create: `tests/test_acceptance_cannanas.py`
- Modify: `docs/server-voice-quickstart.md` (Cannanas section)
- Create: `docs/cannanas-jarvis-integration.md` (architecture, deployment, spoken grammar, recovery)

**Acceptance (mirrors design §*Mark-LV integration* 15–22, *Confirmation safety* 9–14, and mock-live 23–28):** one test per criterion, live staging checks explicitly gated behind an operator-approved env flag and skipped by default:
- Registry contains only approved read-only HA, Spotify, and the closed Cannanas enum (15); `cannabis_status`, generic HTTP/MCP, arbitrary paths and excluded actions denied before dispatch (16).
- 26-record bounded response → ≤ 25 structured records, truncation + next-page semantics (17).
- STOP/interruption during preview or confirmation never produces a write (18).
- Spotify/HA phone existing suite remains green (19).
- HTTP text, control-WS text, phone PCM preserve all four provenance values through dispatch (20); four simultaneous durable credentials isolated, bearer refresh retains `stable_device_id`, revocation invalidates pending approval (21).
- Tool call/affirmation before final transcription cannot confirm a write (22).
- Restart with a pending preview requires a fresh preview (27, in-memory coordinator + boot fence proof).

- [ ] **Step 1: Write the acceptance test module** — one test per criterion; skip-mark live checks (`@unittest.skipUnless(os.getenv('JARVIS_CANNANAS_ACCEPTANCE_LIVE') == 'ops-approved', ...)`) — never a raw credential comparison.

- [ ] **Step 2: Full suite green**
  - `/root/.hermes/cache/scratch/marklv-task1-venv/bin/python -m unittest discover -s tests -v`

- [ ] **Step 3: Secret scan**
  - Search for `CANNANAS_API_KEY\s*=\s*["'][^"\']`, `Bearer\s+[A-Za-z0-9._-]{20,}`, confirmation-token literals and produced token hashes across code, tests and docs — expected: no hits outside env reads and fixture dummies.

- [ ] **Step 4: Documentation** — quickstart section + `docs/cannanas-jarvis-integration.md`, including the deterministic confirmation grammar list and restart/degraded behavior; no club IDs or credentials.

- [ ] **Step 5: Commit**
  - `git add tests/test_acceptance_cannanas.py docs/`
  - `git commit -m "docs: Cannanas JARVIS integration acceptance and documentation"`

---

## Handoff to live staging/production (design criteria 23–28)

These require operator actions and explicit user approval; the plan ends at Task 9:
1. Provision the new least-privilege JARVIS Cannanas key; validate `GET /v1/auth/test`, approved Grow reads, exactly the propagation-material Inventory read; Inventory mutations and representative sensitive domains denied without reading their data (23).
2. Stop `lacasa15` (`/Tailscale path`) and prove local STDIO integration continues (24).
3. Block Cannanas HTTPS and prove bounded safe failure (25).
4. With explicit approval: one test plant, strain, batch, one plant/batch field set update, one diary record; exact IDs verified, audit transitions checked, replay denied (26).
5. Restart JARVIS with a pending preview and prove a fresh preview is required (27).
6. Final gate: adapter suite, Mark-LV full suite, `python -m compileall`, secret scans, deployed SHA/version verification (28).

## Execution notes

- All Cannanas code lives under clearly named new modules (`core/cannanas_*.py`, `core/turn_provenance.py`, `core/user_turn.py`, `core/cannanas_actions.py`, `plugins/cannanas.py`); avoid spreading Cannanas logic through existing files beyond the documented integration points (`dashboard/server.py`, `main.py`, `core/headless.py`).
- Keep model-facing prompts/tool declarations free of confirmation state: the preview text is the only write-adjacent content Gemini sees, and it never contains tokens or club IDs.
- The adapter release manifest (version + wheel SHA) is the single pinned input; update both Mark-LV config (`Pinned release` in the deployment template) and documentation together when a new adapter release ships.
- Do not modify `/home/lacasa15/services/cannanas-mcp/` — the adapter plan builds a separate packaged artifact; the canonical bridge remains untouched.