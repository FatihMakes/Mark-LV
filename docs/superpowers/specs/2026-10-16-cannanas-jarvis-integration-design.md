# Cannanas MCP → JARVIS Integration Design

**Date:** 2026-10-16
**Status:** Proposed for user approval; implementation follows Spotify voice control and durable device access
**Target runtime:** `lacasa-server` only
**Existing canonical adapter:** `/home/lacasa15/services/cannanas-mcp/cannanas_mcp.py`

## Intent

Add Cannanas cultivation operations to the private headless JARVIS without exposing the broader club API, trusting model-generated confirmation tokens, or making `lacasa15` a runtime dependency for every voice request.

A gardener should be able to ask natural German questions about plants, rooms, strains, batches and propagation material. Safe create, daily update and diary actions use a strict spoken preview followed by an explicit confirmation in a later turn. JARVIS must never silently choose among ambiguous plants/rooms/strains or perform a destructive operation.

## Delivery order

1. Finish and verify Spotify voice control.
2. Finish and verify four durable phone credentials.
3. Deploy this Cannanas integration.
4. Run combined iPhone, Spotify, ducking, restart, authentication and Cannanas acceptance.

Cannanas design and contract work may proceed while Spotify/phone implementation runs, but Cannanas production code is not mixed into their commits.

## API findings that shape the design

The official Cannanas document is OpenAPI 3.0, version `v1`. The fetched specification contains 185 paths, 252 operations and 97 component schemas. Every documented operation inherits HTTP bearer authentication.[1][2][3]

The API exposes much more than cultivation: clubs, members, finances, chat, carts, contacts, reports, roles and other private domains. JARVIS therefore receives a method-and-path allowlist rather than general REST access.[2][3]

The Grow API includes plants, batches, strains, zones, harvests, diaries, grow configuration, analyses, drying logs, cultivation mass logs, waste disposal and their file endpoints.[2][3] Plants and batches expose documented `offset`/`limit` filters, but the specification does not define response envelopes, ordering, page defaults, totals or `has_more`; JARVIS must never infer an exact total from one response.[2][3]

Canonical cultivation statuses are:

- `SEED`
- `GERMINATION`
- `CUTTING`
- `VEGETATIVE`
- `FLOWERING`
- `HARVEST`
- `CURING`
- `DRYING`
- `QUARANTINE`
- `DESTROYED`

`CreateBatchRequest.plant_count` has an official maximum of 1000.[3]

## Chosen architecture

Deploy a **version-pinned Cannanas MCP adapter on `lacasa-server` and launch it as one persistent local STDIO child of JARVIS**.

```text
authenticated iPhone/dashboard ingress envelope
  → stable device + auth session + capture + finalized user-turn identity
  → one trusted Mark-LV `cannanas` tool
  → session-fenced confirmation coordinator
  → persistent MCP client
  → pinned local Cannanas FastMCP child over STDIO
  → fixed HTTPS origin https://api.cannanas.club
```

### Why this option

- No listening MCP port and no public or Tailscale application endpoint.
- `lacasa15` may be offline without breaking JARVIS cultivation queries.
- The existing MCP normalization, name resolution and prepare/confirm vocabulary remain reusable.
- Mark-LV does not gain a generic REST client or broad API escape hatch.
- One long-lived child avoids a fresh process and handshake for every spoken request.

### Rejected initial options

- **Remote MCP on `lacasa15`:** adds a second host, Tailscale path, remote service, transport authentication and reconnection failure domain. The current server supports STDIO only.
- **Direct Cannanas REST inside Mark-LV:** duplicates adapter validation, normalization, audit and confirmation logic, and makes API surface expansion easier to do accidentally.
- **Current `plugins/cannabis_status.py`:** not an MCP client; invokes a nonexistent guessed `python -m cannanas` CLI, has no implemented REST fallback, uses a `lacasa15` filesystem path, and is denied by the current headless allowlist.

## Packaging and process lifecycle

The MCP adapter becomes a real versioned Python package/artifact with a valid entry point, dependency lock, release version and SHA-256. Do not hand-copy an editable source file. The same artifact can serve Hermes on `lacasa15` and JARVIS on `lacasa-server`.

JARVIS starts the child with an absolute executable/module path and a minimal environment, then performs MCP initialize and tool discovery. Startup verifies an exact expected tool set and an explicit adapter release version. The current FastMCP `server_info.version` reflects the SDK version, so the packaged adapter must either set real release metadata or expose a narrow `get_adapter_info` result containing adapter version and artifact SHA-256. Initialization has a 10-second budget; individual tools have a 25-second budget.

The JARVIS environment also pins the official Python MCP client dependency; the child venv alone is not sufficient. One serialized MCP client session runs on a dedicated async loop/worker and bridges safely to the current synchronous plugin executor. Do not create a client session or subprocess per tool call.

Unexpected child exit:

- read action: restart once with bounded backoff and retry once only when no response may have been committed;
- prepare action: restart invalidates the pending preview and requires preparation again;
- confirm/write action: never retry automatically after bytes may have been sent; result becomes `unknown` until reconciled.

JARVIS readiness may degrade Cannanas without stopping conversation, Spotify or Home Assistant. Invalid local Cannanas configuration must be explicit, secret-free and observable.

## Secret and host policy

Create a new dedicated JARVIS Cannanas API credential rather than copying the Hermes credential. The fixed club ID and bearer credential are stored only in a mode-0600 systemd credential or environment file readable by the unprivileged `lacasa` service. Its API role permits only the approved Grow reads/writes plus the single read-only Inventory endpoint needed for propagation material; it denies every Inventory mutation and every sensitive non-Grow domain.

Never place credentials in:

- Git or deployment documentation;
- process arguments;
- model prompts or tool results;
- confirmation state visible to Gemini;
- service logs, dashboard history, audit records or spoken responses;
- exception strings or raw HTTP body diagnostics.

Keep TLS verification enabled, use only the fixed `https://api.cannanas.club` origin, refuse redirects and pass a minimal environment to the MCP child. A dedicated API-side role should deny non-grow domains except the exact read-only Inventory endpoint needed for propagation material; because formal bearer scopes are not described in OpenAPI, acceptance verifies representative excluded domains operationally without reading their contents.[2][3]

## JARVIS tool surface

Expose one trusted Gemini tool named `cannanas` with a closed `action` enum. Do not expose arbitrary MCP tool names, HTTP methods, URLs, club IDs or confirmation tokens.

### Release 1: read actions

- `overview`
- `find_zones`
- `find_strains`
- `find_plants`
- `find_propagation_material`
- `list_recent_batches`
- `list_plants`
- `count_plants_by_room`

Example requests:

- “JARVIS, wie viele Pflanzen stehen in Madrid?”
- “Welche Pflanzen sind in der Blüte?”
- “Zeig mir Brain Damage Nummer 3.”
- “Welche Batches wurden zuletzt angelegt?”
- “Welche Mutterpflanzen oder Stecklinge sind verfügbar?”

### Release 1: confirmed cultivation actions

- `prepare_plant_create`
- `prepare_strain_create`
- `prepare_batch_create`
- `prepare_plant_update`: status, room, cultivation dates and notes
- `prepare_batch_update`: status, room, cultivation dates and notes
- `prepare_diary_create`: text plus validated measurements and units
- host-only `confirm_pending`
- host-only `cancel_pending`

Strain `identifier` is always passed as the empty string so Cannanas assigns it automatically.

Gemini may invoke read and `prepare_*` actions only. `confirm_pending` and `cancel_pending` are host state-machine operations and are never included in the model-facing action enum or function declaration.

### Release 2: additional read depth

After release 1 acceptance, add read harvests, diaries, grow configuration, analyses, cultivation mass logs and drying logs. Harvest and compliance-impacting mutations remain outside ordinary voice control.

### Excluded from ordinary voice control

- all DELETE operations;
- member, finance, chat, feed, cart, contact, role, transport, subscription, ledger and POS data;
- arbitrary uploads or file deletion;
- waste disposal;
- inventory transfer, transform, dispense or disposal;
- lab-result mutation and dropoff links;
- harvest finalization and other compliance-impacting state transitions;
- grow-configuration changes.

Harvest workflows may receive a separate elevated-authorization design later.

## Read semantics and speech behavior

- Default returned page: 10 records; hard caller maximum: 25.
- Model-facing objects contain compact cultivation fields only; unexpected/private fields are projected out.
- JARVIS speaks at most five records initially, states when output is truncated and offers the next page.
- Resolve by exact normalized name or ID first, then unique partial match. Ambiguity returns at most eight candidates and performs no mutation.
- Map spoken “room” to official query `room_id`, while plant/batch payloads and records use `zone_id`; test this explicitly.
- A direct plant zone wins over a batch-inherited zone. A missing direct zone may be resolved from the plant's batch.
- `find_propagation_material` may call only `GET /v1/clubs/{clubId}/inventory`; it projects propagation identifiers, names, type, availability/location fields needed for selection and drops all unrelated inventory fields. No other Inventory operation is allowed.
- Plant-name search, parent-plant resolution and every mutation reference lookup scan documented pages to proven exhaustion. A later-page exact match wins over an early partial match; ambiguity across pages is reported. Reaching the safety ceiling makes a mutation lookup inconclusive and produces no preview or POST.
- “Recent batches” is computed only after proven exhaustive retrieval and validation of `created_at`, then sorted descending. Until the live API contract proves exhaustive paging/order, the spoken response is qualified as a bounded batch listing rather than “the latest”.
- Plant/batch counts enumerate pages with stable-ID deduplication and a safety ceiling of 50 pages or 5000 records.
- If exhaustion cannot be proven, say “mindestens N”, return `exact: false`, and never present N as a total.
- Overview uses the same exactness rules and returns at most ten rooms and five recent batches.

## Two-step write protocol

1. **Authenticated ingress:** Dashboard HTTP text, control-WebSocket text and phone PCM enter as one immutable envelope: `{stable_device_id, auth_session_id, capture_id, user_turn_id, channel, text_or_pcm}`. A turn is pinned to one stable durable-device identity. Mixed, revoked, superseded or unknown sources are rejected. Bearer refresh changes `auth_session_id` but retains the durable `stable_device_id` after successful reauthentication.
2. **Turn finalization:** PCM transcription and text input become a finalized user-input turn before any confirmation decision. Tool dispatch receives immutable turn/device context rather than `session_memory=None`. Response generations are not used as user-turn identity.
3. **Prepare:** Resolve every spoken reference against live data. Reject zero/multiple/inconclusive matches. Validate the immutable payload against both the pinned official schema and stricter voice rules. Perform no POST.
4. **Single authoritative owner:** A host-side confirmation coordinator is the sole owner of pending state and stores `{action, club_id, payload_hash, complete_preview, jarvis_session_id, stable_device_id, auth_session_id, prepared_turn_id, adapter_boot_id, created_at, expires_at, state, opaque_mcp_token}`. The raw MCP token is never given to Gemini. Only one pending action exists per JARVIS session; a new prepare cancels the previous one.
5. **Preview:** JARVIS reads every field that would be written and asks a direct yes/no question. The confirmation nonce/token is retained by the coordinator and never becomes a model argument or tool result.
6. **Deterministic confirmation grammar:** After a later finalized turn from the same stable device, the host—not Gemini—accepts an exact bounded German affirmative/cancel grammar. Model tool calls cannot confirm or cancel. Same-turn assent, input before transcription finalization, silence, interruption, STOP, device revocation, mixed/interleaved devices, reconnect ambiguity, ambiguous assent or unrelated speech produces no POST. A reconnect requires a new preview.
7. **Atomic consume:** Change pending → executing using compare-and-swap or atomic rename before calling MCP. Concurrent/replayed confirms yield one executor. The coordinator supplies the adapter boot/action/club/payload binding with the opaque MCP token; the adapter verifies the same binding before POST.
8. **Restart fence:** Adapter or JARVIS restart invalidates ordinary pending confirmations; a new preview is required.
9. **Result:** No automatic retry once a POST may have been sent. Clear pre-send failures are `failed`; timeout/disconnect after send is `unknown`.
10. **Verification:** Read back by the exact created/updated ID. Missing ID is `created_but_unverified` or `unknown`; never use loose room+strain+status matching as proof.

For strain creation, the preview says the identifier will be assigned automatically by Cannanas. Plant and batch previews include room, strain, status, count/plant name, dates, propagation/parent source and notes.

## Adapter hardening required before voice writes

The current MCP adapter is a useful base but must be corrected before deployment:

- bind pending operations to session, device, club, action, payload hash, turn and boot;
- atomically consume confirmation state to prevent duplicate concurrent POSTs;
- invalidate pending confirmations on restart;
- reduce the batch maximum from 10,000 to the official 1,000;
- sanitize raw API bodies and never speak/log arbitrary first-500-character errors;
- verify plant, strain and batch creation by exact returned ID;
- stop using loose batch matching as successful readback;
- keep operational history private (mode 0640/0600), not world-readable;
- make write-audit failure fail before POST;
- implement official server-side pagination and explicit exact/truncated semantics;
- expose a real adapter release version/artifact SHA rather than the MCP SDK version;
- add prepared/confirmed plant update, batch update and diary-create tools using the same ownership and replay fences.

## Audit policy

Persist a secret-free append-only audit containing pseudonymous device/session identity, action, payload hash, complete human preview, timestamps, state transitions and returned object ID.

Do not persist bearer credentials, MCP confirmation tokens/nonces, durable phone secrets, Gemini session material or raw API responses.

For reads, audit failure may degrade with an operator warning. For writes, audit availability is required before POST so every approved mutation has an accountable record.

## Failure handling

- Missing API credential/club configuration: fail before network I/O and identify only missing variable names.
- 401/403: do not retry; report authorization/permission failure.
- 429: read `Retry-After` only if valid and bounded; retry one read once, never a write.
- 5xx/connect failure: retry a read once; fail a write unless it is proven no bytes were sent.
- Redirect: reject.
- Malformed/oversized response: reject without echoing body.
- MCP unavailable after one restart: say Cannanas is temporarily unavailable; keep JARVIS, Spotify and HA active.
- Post-send timeout: say the result is uncertain and require reconciliation; never repeat automatically.

## Acceptance criteria

### Adapter contract

1. Pin an OpenAPI fixture and assert bearer authentication, required read/create paths, ten cultivation statuses and batch maximum 1000.
2. Assert every outbound request matches an exact method/path-template allowlist with the configured club ID injected internally.
3. Fuzz method/path/club attempts and prove no excluded request is emitted.
4. Verify TLS on, redirects off, bounded timeouts and secret-free 400/401/403/404/429/5xx handling.
5. Contract-test the actual list envelope safely; implement multi-page exhaustion, stable-ID deduplication and exactness flags.
6. Validate all spoken aliases and reject unknown statuses/counts/references before network I/O.
7. Prove later-page exact matches, ambiguity across pages and safety-ceiling exhaustion; incomplete scans cannot prepare a mutation.
8. Verify the explicit adapter release metadata and SHA against the installed pinned artifact, and verify the pinned Mark-LV MCP client maintains one serialized persistent STDIO session.

### Confirmation safety

9. Every `prepare_*` produces zero POSTs and a complete immutable preview/payload hash.
10. Same-turn, other-session/device/club/action/payload/boot, ambiguous, expired, cancelled and post-restart confirmation all produce zero POSTs.
11. Two concurrent confirmations result in exactly one POST; replay is denied.
12. Kill/restart before prepare, after prepare, before send and after send. Reads recover; pending writes fence; post-send writes never duplicate.
13. Plant, strain, batch and diary creates plus plant/batch updates read back by exact returned ID; missing ID is not called verified.
14. Confirmation tokens/nonces and bearer values never appear in console, service logs, model results, dashboard history, audit or speech.

### Mark-LV integration

15. The headless registry contains only approved read-only Home Assistant, Spotify and the closed Cannanas action enum.
16. `cannabis_status`, generic HTTP/MCP operations, arbitrary paths and excluded actions are denied before dispatch.
17. A 26-record response returns at most 25 structured records and five spoken records, with truncation and next-page behavior.
18. STOP/interruption during preview or confirmation cannot produce a write.
19. Existing half-duplex, phone authorization, Spotify playback/ducking and HA tests remain green.
20. HTTP text, control-WebSocket text and phone audio each preserve stable device, auth session, capture and user-turn provenance through tool dispatch.
21. Four simultaneous durable credentials remain isolated; interleaved turns cannot confirm another device's preview, bearer refresh retains stable identity, and device revocation invalidates pending approval.
22. A tool call or affirmative arriving before final input transcription cannot confirm a write.

### Live staging/production

23. Provision a new least-privilege JARVIS key and validate `GET /v1/auth/test`; approved Grow reads and exactly the propagation-material Inventory read work while Inventory mutations and representative sensitive domains are denied without reading their data.
24. Stop `lacasa15` or its Tailscale path and prove local STDIO integration continues.
25. Block Cannanas HTTPS and prove bounded safe failure.
26. With explicit user approval, create one test plant, one strain and one batch, update one plant/batch field set and create one diary record; verify exact IDs, audit transitions and replay denial. Do not perform destructive cleanup through voice.
27. Restart JARVIS with a pending preview and prove a fresh preview is required.
28. Run adapter tests, Mark-LV full suite, Python compilation, secret scans and deployment SHA/version verification before calling the integration ready.

## Builder calls made

- Release 1 includes the important daily plant/batch updates and diary creation rather than postponing them; this adds MCP development but makes the first production release operationally useful.
- Writes require a healthy audit sink before POST.
- The adapter is packaged and pinned rather than copied manually.
- A separate JARVIS API key is mandatory.
- `lacasa-server` owns the runtime adapter; `lacasa15` remains the Hermes/Cannanas development host.
- Stable durable-device and finalized user-turn provenance is implemented before any voice mutation is enabled.

## Sources

[1] https://api.cannanas.club/docs — Cannanas API Documentation
[2] https://api.cannanas.club/docs/swagger-ui-init.js — Cannanas OpenAPI Swagger Initialization
[3] https://api.cannanas.club/docs/cannanas-api-docs.yaml — Cannanas OpenAPI Specification
