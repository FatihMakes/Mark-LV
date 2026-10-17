# Cannanas MCP Adapter Packaging and Hardening Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the editable `cannanas_mcp.py` bridge script into a real versioned, hash-pinned Python package with a valid entry point and dependency lock, and harden it so it satisfies every requirement of the design section **"Adapter hardening required before voice writes"** and the adapter-contract acceptance criteria.

**Architecture:** The hardened adapter is built as a *new* package (`cannanas_mcp_adapter`) in a *new* directory beside the canonical files. The canonical bridge **stays exactly as it is** — `/home/lacasa15/services/cannanas-mcp/cannanas_mcp.py` is read-only input for this plan; a snapshot of it is recorded as provenance baseline, never deployed. The package installs into its own venv and is consumed two ways: Hermes on `lacasa15` (replacing today's editable script usage) and JARVIS on `lacasa-server` (as the pinned local STDIO child). Release artifacts (wheel + hashed dependency lock + release manifest with version and SHA-256) are the only things that leave the build tree.

**Tech Stack:** Python 3.11+, `mcp` (FastMCP) for the STDIO server, `httpx` (already used) with `follow_redirects=False`, stdlib `hashlib`/`stat`/`os` for state files, `unittest`/`pytest` on `lacasa15`, `pip` + hashed constraints lock, wheel builds.

**Spec:** `docs/superpowers/specs/2026-10-16-cannanas-jarvis-integration-design.md` (authoritative; sections *Packaging and process lifecycle*, *Secret and host policy*, *Adapter hardening required before voice writes*, *Adapter contract*).

**Build host:** `lacasa15`. The JARVIS integration plan (sibling document) deploys the release artifact to `lacasa-server` and verifies the pinned SHA at startup — this plan only builds and publishes it.

---

## Baseline corrections (adapter reality vs. design)

Found while reading the canonical bridge (all must be fixed in the package; none require touching the canonical file):

1. **No `get_adapter_info`.** FastMCP reports `server_info.version` = the MCP **SDK** version, not a release version. The package must expose a `get_adapter_info` tool returning real adapter release version + artifact SHA-256.
2. **Missing tools for Release 1 writes.** The bridge has only plant/strain/batch create pairs. `prepare_plant_update`, `confirm_plant_update`, `prepare_batch_update`, `confirm_batch_update`, `prepare_diary_create`, `confirm_diary_create` do not exist and must be added with the same ownership/replay fences.
3. **Batch maximum is 10,000** (`prepare_batch_create`), the official `CreateBatchRequest.plant_count` maximum is **1,000**.
4. **No pagination at all.** `_list()` performs a single unfiltered GET; the official plants/batches endpoints document `offset`/`limit`. Exhaustion-proof paging, stable-ID dedupe, the 50-page/5,000-record safety ceiling, `exact` flags and the hard 25-record caller cap are all unimplemented.
5. **Raw API body echo.** `_request()` raises `RuntimeError(f"...: {response.text[:500]}")` — arbitrary server body text must never be echoed/logged/spoken.
6. **Weak confirm binding.** Pending state stores only `{expires_at, payload, preview}`; there is no session/device/club/action/payload-hash/turn/boot binding, and read-then-unlink of the token file is not an atomic consume.
7. **Pending state defaults under `/tmp/cannanas-mcp-pending`** and the token file is written then `chmod`ed (race window); must be an explicitly configured mode-`0700` directory with `O_CREAT|O_EXCL|O_NOFOLLOW` mode-`0600` files, and JARVIS must pass the state root per host.
8. **Loose batch verification.** `confirm_batch_create` accepts a zone+strain+status match as "verified"; verification must be a read-back by the exact returned ID only.
9. **Audit violates the write policy.** `_write_history()` runs *after* the operation, swallows `OSError` (fire-once-and-forget), defaults to a hardcoded `lacasa15` path and deliberately `chmod 0644`. For voice writes: audit must be available **before POST**, write failure must **fail closed**, and the operator log must be mode `0640`/`0600`.
10. **Origin is not fixed.** `CANNANAS_API_URL` env override exists; production must pin `https://api.cannanas.club` (env-free constant) with TLS verification and redirect refusal.
11. **Strain identifier is model-controllable.** `prepare_strain_create` accepts a non-empty `hid`; voice writes must always pass the empty identifier so Cannanas assigns it.
12. **No method/path allowlist.** Any tool can issue any request through `_request()`; the package enforces an exact method+path-template allowlist table with the club ID injected internally.
13. **Fragile result plumbing.** `_safe_result()` scrapes caller locals via `inspect.currentframe()`; the package passes results explicitly (no frame inspection).
14. **Token TTL is 10 minutes with no boot binding.** Restart invalidation requires an explicit adapter boot ID recorded with each pending operation.

## Global Constraints

- Never modify, move or rename anything inside `/home/lacasa15/services/cannanas-mcp/` except by *adding* new files under a new subdirectory; the canonical bridge remains a running Hermes dependency during development.
- Build tree: `/home/lacasa15/services/cannanas-mcp/adapter/` (new). Package import name: `cannanas_mcp_adapter`. Console entry point `cannanas-mcp-adapter` runs STDIO.
- Never place CANNANAS_API_KEY, real club IDs, or produced confirmation tokens in Git, tests, logs, reports or chat. Tests use fake servers/recorded fixtures with dummy credentials.
- Outbound HTTPS: fixed origin `https://api.cannanas.club`, `verify=True`, `follow_redirects=False`, connect timeout 10 s / total 20 s (matches current bridge; no config-driven relaxations).
- Operator audit/operational history files: mode `0640` (or `0600`), parent dir `0700`; never group/world readable.
- Confirm token file consume must be atomic (delete-by-rename or `O_EXCL` claim protocol) so two concurrent confirms can produce at most one POST.
- Keep the `prepare_*` / `confirm_*` vocabulary and the existing German preview/error messages; Hermes sessions and tests depend on that surface.
- Adapter tests run on `lacasa15`; the test venv is created under `/home/lacasa15/services/cannanas-mcp/adapter/.venv` (never committed).
- Commit all adapter work on `lacasa15` in a *new* local Git repository at `/home/lacasa15/services/cannanas-mcp/adapter/` (the parent service directory is not a Git checkout; do not initialize one there). Do not commit inside the Mark-LV repository.

## Review Focus

1. Atomic consume under concurrency: two parallel `confirm_*` calls with the same token yield exactly one POST; pin in Task 5.
2. Pre-POST audit fail-closed for writes; reads degrade only with an operator warning; pin in Task 6.
3. Allowlist fuzzing: no excluded method/path/club combination can ever be sent; pin in Task 3.
4. Exhaustion/ceiling semantics cannot be silently approximated; pin in Task 4.
5. Release metadata can't lie: `get_adapter_info` values must equal the deployed manifest values; pin in Task 7.

---

### Task 1: Versioned package skeleton with provenance snapshot

**Files (under `/home/lacasa15/services/cannanas-mcp/adapter/`):**
- Create: `pyproject.toml`, `README.md`, `.gitignore`, `snapshot/cannanas_mcp.py` + `snapshot/cannanas_mcp.py.sha256`
- Create: `src/cannanas_mcp_adapter/__init__.py`, `_release.py`, `server.py`, `__main__.py`
- Create: `tests/__init__.py`, `tests/test_package_contract.py`
- Create: Git repo (`git init`) with `main` branch

**Interfaces:**
- `pyproject.toml`: name `cannanas-mcp-adapter`, version `1.0.0` (Release 1), `[project.scripts] cannanas-mcp-adapter = "cannanas_mcp_adapter.__main__:main"`, dependencies `mcp>=1.9,<2` and `httpx>=0.27,<0.29`, `requires-python >= 3.11`.
- `_release.py`: `ADAPTER_NAME = "cannanas-mcp-adapter"`, `ADAPTER_VERSION = "1.0.0"`, `SOURCE_SHA256` (filled by Task 7's build step), `EXPECTED_TOOLS` — the frozen Release 1 tool set (Task 2).
- `__main__.main()`: reads only `CANNANAS_API_KEY`, `CANNANAS_CLUB_ID`, `CANNANAS_STATE_DIR`, `CANNANAS_AUDIT_FILE` from the environment; refuses to start on unknown env overrides that could change the origin or timeouts; then runs the FastMCP server on STDIO.
- `snapshot/` records the byte-exact bridge with its SHA-256 as provenance ("what this package started from").

- [ ] **Step 1: Write failing contract tests**
  - `test_snapshot_matches_recorded_sha256` (guards provenance), `test_entry_point_declared`, `test_expected_tools_constant_frozen` (asserts the exact list; will initially fail because Task 2 hasn't declared it).

- [ ] **Step 2: Implement the skeleton**
  - Import the snapshot into `src/cannanas_mcp_adapter/server.py` verbatim (single mechanical transformation of module→package) and record its source SHA.

- [ ] **Step 3: Verify package imports and the canonical bridge is untouched**
  - `/home/lacasa15/services/cannanas-mcp/adapter/.venv/bin/python -c "import cannanas_mcp_adapter"` and `sha256sum /home/lacasa15/services/cannanas-mcp/cannanas_mcp.py` equals the snapshot hash.

- [ ] **Step 4: Commit** — `feat: versioned adapter package skeleton with provenance snapshot`

---

### Task 2: Fixed tool set, `get_adapter_info`, and update/diary tools declarations

**Files:**
- Modify: `src/cannanas_mcp_adapter/server.py`
- Create: `tests/test_tool_surface.py`

**Interfaces:**
- Frozen Release 1 adapter tool set (21 tools): `get_adapter_info`, `get_cannanas_overview`, `find_zones`, `find_strains`, `find_plants`, `find_propagation_material`, `list_recent_batches`, `list_plants`, `count_plants_by_room`, `prepare_plant_create`, `confirm_plant_create`, `prepare_strain_create`, `confirm_strain_create`, `prepare_batch_create`, `confirm_batch_create`, `prepare_plant_update`, `confirm_plant_update`, `prepare_batch_update`, `confirm_batch_update`, `prepare_diary_create`, `confirm_diary_create`.
- `get_adapter_info()` (no arguments) returns `{"ok": True, "adapter_name", "adapter_version", "source_sha256", "artifact_sha256": <from release sidecar or "" at build time>, "tools": <expected list>, "config": {"origin_set": True, "club_id_bound": bool}}` — never echoes the API key or full club ID.
- `prepare_plant_update(plant, fields)` / `prepare_batch_update(batch, fields)`: spoken reference resolution plus an immutable update payload restricted to `status`, `room` (mapped to zone), cultivation dates and `notes`; status must normalize via the ten canonical statuses; unknown/extra fields rejected.
- `prepare_diary_create(plant_or_batch, text, measurements)` : text plus validated measurement/value/unit pairs only.
- Each `prepare_*` returns the same envelope as today (`requires_confirmation`, token, `preview`) — vocabulary unchanged.
- Plant/batch update and diary method+path templates come **only** from the pinned OpenAPI fixture (Task 3); they must not be guessed.

- [ ] **Step 1: Write failing tool-surface tests** — list all 21 tools over an in-process FastMCP client; assert `get_adapter_info` shape and that update/diary prepare tools produce zero POSTs (recorded fixture transport).

- [ ] **Step 2: Implement declarations** — add the six missing tools to `EXPECTED_TOOLS` incrementally (create → update → diary → `get_adapter_info`), each with its prepare returning a preview payload.

- [ ] **Step 3: Run tests GREEN** — `/home/lacasa15/services/cannanas-mcp/adapter/.venv/bin/python -m pytest tests/test_tool_surface.py -v`.

- [ ] **Step 4: Commit** — `feat: freeze Release 1 tool set with update and diary prepare tools`

---

### Task 3: Fixed origin, method/path allowlist, sanitized failures, pinned OpenAPI fixture

**Files:**
- Create: `fixtures/cannanas-api.yaml` (recorded copy of `https://api.cannanas.club/docs/cannanas-api-docs.yaml`) + `fixtures/cannanas-api.yaml.sha256`
- Modify: `src/cannanas_mcp_adapter/server.py`
- Create: `tests/test_allowlist.py`, `tests/test_error_sanitization.py`, `tests/test_openapi_fixture.py`

**Interfaces:**
- Origin becomes a module constant `ORIGIN = "https://api.cannanas.club"`; the `CANNANAS_API_URL` override is removed.
- `ALLOWED_REQUESTS`: exact `(method, path_template, purpose)` tuples covering the Release 1 reads (zones, strains, plants, batches, inventory GET list; plant/strain/batch/diary reads by ID), the create/update POSTs and the official update verbs, with `CLUB_ID injected internally` — never a parameter.
- Every outbound call goes through one guard `send(method, path, body)` that pattern-matches `(method, path)` against the allowlist after injecting the club ID and raises `RequestRefused` otherwise.
- Error taxonomy: every non-2xx becomes `{"ok": False, "error": <fixed German category>, "status": int}` — no body text, no exception strings, no headers; only class labels (`authentifizierung`, `bereich_nicht_gefunden`, `ratelimit`, `serverfehler`, `netzwerk`).
- Fixture tests assert: 185 paths/252 operations recorded, bearer auth on every operation, the ten canonical cultivation statuses, `CreateBatchRequest.plant_count` maximum 1000, and the exact method/path templates used by every adapter tool.

- [ ] **Step 1: Write failing allowlist + fuzz tests** — fuzz random method/path/club combinations across the recorded `send()` stub transport and assert no excluded request is emitted even from internal callers.

- [ ] **Step 2: Implement origin pin, allowlist guard, sanitizer** — rewire every existing `_request` call site through the guard; delete body echo.

- [ ] **Step 3: Pin the OpenAPI fixture** — record the spec + SHA; write fixture assertions; document the fixture refresh procedure (manual, requires spec-SHA re-record + review).

- [ ] **Step 4: Run tests GREEN and commit** — `feat: enforce fixed origin and request allowlist with sanitized failures`

---

### Task 4: Exhaustion-proof pagination, exactness flags and ceiling semantics

**Files:**
- Modify: `src/cannanas_mcp_adapter/server.py`
- Create: `tests/test_paging.py`

**Interfaces:**
- `paged_records(resource, response_key)`: follows the official `offset`/`limit` pagination to proven exhaustion (a page returning fewer than `limit` records, per the pinned fixture), deduplicates by stable ID, enforces the safety ceiling of **50 pages or 5,000 records**, and returns `(records, exact: bool)`.
- Caller-facing caps: every list-returning tool caps structured output at **25** records; `count_plants_by_room` and `get_cannanas_overview` counts use deduped totals with an explicit `exact` flag; when `exact` is `False`, results are qualified "mindestens N" and never presented as totals.
- Mutation lookups (plant/strain/room/parent/propagation-material resolution inside `prepare_*`) scan documented pages to proven exhaustion; a later-page exact match beats an earlier partial match; ambiguity across pages lists at most 8 candidates; a ceiling-interrupted scan is **inconclusive → no preview, no POST**.
- "Recent batches" sorts by validated `created_at` descending only after proven exhaustive retrieval; otherwise the result is flagged `bounded: true` (spoken as a bounded listing, never "die letzten Batches").
- Spoken "room" resolves to the zones query parameter `room_id` for query filtering while plant/batch payloads and records keep `zone_id` — both paths tested explicitly.

- [ ] **Step 1: Write failing paging tests** — multi-page fixtures with duplicate IDs across pages, later-page exact match, cross-page ambiguity, ceiling interruption (50 pages / 5,000 records), `created_at` ordering, exhaustion-failure cases.

- [ ] **Step 2: Implement `paged_records` and rewire every tool** — replace all single-GET `_list()` call sites; add `exact`/`bounded` flags to every affected response envelope.

- [ ] **Step 3: Run tests GREEN and commit** — `feat: exhaustion-proof paging with exactness flags and safety ceilings`

---

### Task 5: Adapter-side binding, boot fence and atomic consume

**Files:**
- Modify: `src/cannanas_mcp_adapter/server.py`
- Create: `src/cannanas_mcp_adapter/pending_state.py`, `src/cannanas_mcp_adapter/boot.py`
- Create: `tests/test_pending_binding.py`, `tests/test_atomic_consume.py`, `tests/test_boot_invalidation.py`

**Interfaces:**
- `prepare_*` accepts and persists the caller-supplied binding context in addition to the payload: `{"jarvis_session_id", "stable_device_id", "auth_session_id", "prepared_turn_id", "club_id", "action", "payload_hash", "adapter_boot_id"}` (read tools don't need it; Hermes desktop use may pass only club/action — binding fields are optional for Hermes, mandatory when the caller is JARVIS).
- Pending record lives in `CANNANAS_STATE_DIR` (required, must exist with mode `0700`, refused under `/tmp`) as one mode-`0600` file per token: `{action, club_id, payload_hash, payload, preview, binding, expires_at (≤ 10 min), boot_id, state}`.
- `confirm_*(confirmation_token, binding)`: verifies **before consume** that every binding field matches the prepared record — session, device, auth session, turn, club, action, payload hash, and current `boot_id` — otherwise zero POSTs and a fixed German refusal; on mismatch, the pending is *not* consumed (the caller may retry with a corrected context or re-prepare).
- Atomic consume: claim via `os.open(token_file, O_CREAT|O_EXCL|O_NOFOLLOW)` on a `.claim-<token-hash>` sentinel (or `os.replace` rename) followed by unlink **before** the POST is issued; a second concurrent confirm loses the claim and returns "bereits verarbeitet" — exactly one executor.
- Boot fence: `boot.py` writes a fresh random `boot_id` to the state dir on every process start; all pending records from a previous boot ID are invalidated (deleted or refused) at prepare/confirm time. This yields the design's "restart invalidates pending confirmations".
- Write-audit hook (implemented in Task 6) is invoked before the HTTP request; Task 5 leaves a `_audit_pre_post(...)` seam that currently writes to the operational file and is replaced by the fail-closed audit in Task 6.

- [ ] **Step 1: Write failing binding/consume/boot tests** — mismatch cases (each field individually), expired token (TTL 600 s), concurrent claim race (threads), replays, boot-ID mismatch, crash between claim and POST (simulated by monkeypatched `send` raising) leaves nothing pending and no POST.

- [ ] **Step 2: Implement binding verification, atomic claim, boot fence** — remove the old read-then-unlink path entirely.

- [ ] **Step 3: Implement the six confirm_* bodies on the shared fence** — create/update/diary all use the same verified binding + atomic claim + exact-ID read-back seam.

- [ ] **Step 4: Run tests GREEN and commit** — `feat: bind pending operations to session/device/turn/boot with atomic consume`

---

### Task 6: Exact-ID verification and fail-closed operator audit

**Files:**
- Create: `src/cannanas_mcp_adapter/audit.py`
- Modify: `src/cannanas_mcp_adapter/server.py`, `pending_state.py`
- Create: `tests/test_verification.py`, `tests/test_audit_fail_closed.py`

**Interfaces:**
- Every write verify step: read back by the exact returned ID (allowlisted `GET /v1/clubs/{clubId}/<resource>/{id}`) and set `verified` only on an exact-ID hit; a missing ID yields `created_but_unverified` (POST accepted, read back failed) or `unknown` (POST outcome unproven). The old loose zone+strain+status batch matching is deleted.
- `audit.py`: append-only JSONL, file mode `0640` (parent `0700`), path from `CANNANAS_AUDIT_FILE` (required for voice-grade deployments), entries: `{timestamp, boot_id, tool, action, club_id, payload_hash, preview, binding (pseudonymous ids only), state transition, returned_object_id, ok}`.
- **Never persisted:** bearer credential, MCP confirmation tokens/nonces, Gemini session material, raw API responses/bodies.
- Write policy: `prepare_*`/`confirm_*`/any tool that may POST calls `audit.ensure_writable()` and writes the confirmed-transition entry **before** `send()`; an audit write failure raises `AuditUnavailable` and the adapter returns `{"ok": False, "error": "prüfung_ausgefallen"}` with zero POSTs. Read tools may degrade with a `warnings` field plus an operator log line when audit fails — never the reverse for writes.
- Mode enforcement: existing files with wrong mode/ownership/symlink target fail the deployment-time self-check and refuse voice writes (fail closed), while Hermes reads keep working through a documented warning.

- [ ] **Step 1: Write failing verification + audit tests** — exact-ID hits/misses, `created_but_unverified` path, audit-missing/readonly/symlinked file blocks a POST (fixture transport proving zero bytes sent), no secrets in any byte of the audit file, mode assertions.

- [ ] **Step 2: Implement audit and the pre-POST gate** — wire every `confirm_*` through the gate; delete `_write_history`'s post-hoc best-effort behavior from the package.

- [ ] **Step 3: Reduce the batch maximum** — `prepare_batch_create` rejects `plant_count > 1000` (fixture-backed); delete the 10,000 ceiling; extend the loose-match deletion test.

- [ ] **Step 4: Run tests GREEN and commit** — `feat: exact-ID verification and fail-closed 0640 operator audit`

---

### Task 7: Build the pinned release (wheel + hashed lock + manifest) and Hermes cutover

**Files (build tree):**
- Create: `constraints-lock.txt` (fully pinned, `--hash` annotated), `dist/` (gitignored), `release/release-manifest-v1.0.0.json`, `docs/DEPLOY.md`, `scripts/build-release.sh`
- Create: `tests/test_release_manifest.py`, `tests/test_hermes_mode.py`

**Interfaces:**
- `scripts/build-release.sh`: clean tree → `python -m build` → generate the hashed constraint set from the *installed* resolution → compute wheel SHA-256 → write `release-manifest-v1.0.0.json` (`{"version": "1.0.0", "wheel": "<name>", "wheel_sha256": ..., "source_sha256": ..., "constraints_sha256": ..., "created_at"}`) → bake `SOURCE_SHA256` into `_release.py` and rebuild once so the metadata is inside the artifact.
- `get_adapter_info` artifact SHA: when running from an installed wheel, the packaged adapter reads `release_info.json` placed beside the installed entry-point script by the deploy step (both Hermes and JARVIS deployments get it) and returns its wheel SHA-256; absent sidecar → `artifact_sha256: ""` and the info tool reports `release_pinned: false` so JARVIS refuses to bind writes against it.
- `test_hermes_mode`: the same wheel serves the Hermes session — binding fields optional, no state-dir requirement beyond defaults, `get_adapter_info` identical; a smoke run exercises one read tool against the fixture transport.
- Nothing user-visible changes for Hermes until cutover; the canonical bridge file stays in place as fallback until acceptance.

- [ ] **Step 1: Write failing manifest tests** — wheel exists, SHA matches, constraints file fully pinned with hashes, `_release.SOURCE_SHA256` equals the manifest, entry point runs `--adapter-info` correctly via the sidecar.

- [ ] **Step 2: Build v1.0.0** — run `scripts/build-release.sh`; record the manifest in the report.

- [ ] **Step 3: Hermes-side smoke check** — run the wheel-based adapter from a short-lived smoke venv next to the existing Hermes configuration using *dummy* env values and the fixture transport; do not restart the Hermes service in this task.

- [ ] **Step 4: Commit** — `feat: build and pin adapter release 1.0.1 with hashed constraints`

---

### Task 8: Adapter acceptance per design criteria 1–8

**Files:**
- Create: `tests/test_acceptance_adapter.py` (or a pytest-parallel module), `docs/ACCEPTANCE.md`

**Acceptance checks (mapped):**
1. OpenAPI fixture pinned; bearer auth, required read/create paths, ten statuses, batch max 1000 asserted.
2. Every outbound request passes the allowlist with the club ID injected internally.
3. Fuzz proves no excluded request can be emitted.
4. TLS on, redirects off, bounded timeouts; sanitized 400/401/403/404/429/5xx handling with no body echo.
5. Recorded list-envelope contract tests; multi-page exhaustion, stable-ID dedupe, exactness flags.
6. Alias validation and rejection of unknown statuses/counts/references before network I/O.
7. Later-page exact matches, cross-page ambiguity, ceiling exhaustion; incomplete scans cannot prepare a mutation.
8. Adapter release metadata + SHA verified against the built wheel; one serialized persistent STDIO session verified from the client side (a reference-session stub for the JARVIS plan to consume).

- [ ] **Step 1: Write the acceptance module** — one test per criterion, live-API reads marked `@pytest.mark.live` and skipped without explicit credentials.

- [ ] **Step 2: Run the full adapter suite** — `/home/lacasa15/services/cannanas-mcp/adapter/.venv/bin/python -m pytest tests -v` — expected green.

- [ ] **Step 3: Secret scan** — grep the tree for `CANNANAS_API_KEY\s*=\s*["']`, real-looking bearers and produced tokens; expected: no hits outside env reads.

- [ ] **Step 4: Record acceptance and commit** — `test: adapter acceptance per design criteria 1-8`; record the v1.0.0 manifest (version + wheel SHA) in `docs/ACCEPTANCE.md`.

---

## Handoff to the JARVIS integration plan

The JARVIS integration consumes exactly:
- The built and pinned artifact: release `1.0.0`, wheel + SHA-256 from `release-manifest-v1.0.0.json`, and the hashed `constraints-lock.txt` for reproducing the runner venv on `lacasa-server`.
- The frozen 21-tool expected set from `_release.EXPECTED_TOOLS` and the `get_adapter_info` contract.
- The binding-field list from Task 5 — JARVIS supplies `jarvis_session_id`, `stable_device_id`, `auth_session_id`, `prepared_turn_id`, `club_id`, `action`, `payload_hash`, and `adapter_boot_id` with every `prepare_*`/`confirm_*` call.
- The audit policy from Task 6 — JARVIS still keeps its **own** host-side audit; the adapter audit does not replace it.

## Execution notes

- If `mcp` SDK minor updates change FastMCP behavior, re-run the tool-surface tests before shipping a new release version; adapter versions increment, never patch in place.
- The old bridge remains the Hermes fallback until the JARVIS integration plan's deployment task accepts the pinned artifact on `lacasa-server`; only then is the Hermes cutover to the package finalized as a separate small commit.