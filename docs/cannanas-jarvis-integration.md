# Cannanas ↔ JARVIS integration (Mark-LV)

Status: code complete and acceptance-tested; **live enablement on lacasa-server
is still an operator step** (see "Operator runbook" — nothing is deployed and no
credentials exist yet). The authoritative design is
`docs/superpowers/specs/2026-10-16-cannanas-jarvis-integration-design.md`, the
adapter packaging plan is `docs/superpowers/plans/2026-10-16-cannanas-jarvis-integration.md`,
and the deployment details live in `deploy/cannanas/README-DEPLOY.md`.

## What JARVIS can speak to Cannanas

| Type | Model-facing actions |
|---|---|
| Reads | `overview`, `find_zones`, `find_strains`, `find_plants`, `find_propagation_material`, `list_recent_batches`, `list_plants`, `count_plants_by_room` |
| Write preparation | `prepare_plant_create`, `prepare_strain_create`, `prepare_batch_create`, `prepare_plant_update`, `prepare_batch_update`, `prepare_diary_create` |

Writes are Release-1 only: no deletes, harvest or compliance transitions, no
finance/member/chat/POS/transport domains, no arbitrary uploads. `find_propagation_material`
is the only Inventory operation and is read-only.

## Architecture

```
iPhone (Tailscale/HTTPS) ─┐
HTTP text                ─┼─ immutable provenance envelope
control-WS text          ─┤   {stable_device_id, auth_session_id, capture_id,
phone PCM                ─┘    user_turn_id, channel}
        │ finalized user turn (core/user_turn.py)
        ▼
Gemini  ⇄ single trusted tool "cannanas" (closed action enum, plugins/cannanas.py)
        │ pre-dispatch gate: core/headless.tool_allowed — denies host ops,
        │ unknown MCP names, HTTP verbs and paths before any dispatch
        ▼
core/cannanas_host.py (CannanasHost)
  ├─ persistent serialized MCP STDIO client (core/cannanas_client.py)
  │    pinned release 1.0.1 + wheel SHA; boot verifies the exact tool set
  ├─ ConfirmationCoordinator (core/cannanas_confirmation.py) — in-memory,
  │    one pending per device, atomic pending→executing consume
  ├─ host-only confirmation grammar (exact German forms, no fuzzy match)
  └─ fail-closed JSONL audit (core/cannanas_audit.py, written before POST)
        ▼ child process, minimal environment, no listening ports
pinned adapter wheel  /opt/cannanas/adapter/venv  →  https://api.cannanas.club
```

Key properties:

- **Closed registry.** The headless trust set is exactly
  `home_assistant` (list/query/temperature), `spotify_control` (seven approved
  actions) and `cannanas` (the enum above). Confirm/cancel are host state
  operations and never model-facing.
- **Two-step writes.** A tool call can only *prepare*: complete German preview,
  zero POSTs, pending stored host-side with payload hash and opaque token.
  The confirmation decision is made by the host grammar on a **later finalized
  turn** from the same stable device; same-turn assent, pre-finalization tool
  calls, STOP/interruption, wrong device/session/boot/payload, expiry, replay,
  cancelled and post-restart confirmations all refuse before the adapter.
- **Bounded answers.** Structured records are capped at 25, spoken names at
  5, with explicit truncation and qualified ("mindestens") non-exact totals;
  confirmation tokens, club IDs and payload hashes never appear in model
  results, logs, dashboard history, audit or speech.
- **Fail-safe readiness.** Missing or invalid configuration degrades Cannanas
  only (one bounded German notice naming missing variable names, never
  values); JARVIS, Spotify and Home Assistant keep running. A failed release
  verification degrades to reads only; writes are refused.

## Confirmation grammar (host-side, exact forms only)

- Affirm: „ja", „ok", „okay", „bestätigen", „bestätige", „bitte bestätigen",
  „ja bitte", „speichern", „speichere"
- Cancel: „nein", „abbrechen", „brich ab", „abbruch", „stop", „stopp",
  „stoppe", „stoppen"

Everything else is `none`: unrelated speech leaves the pending untouched until
it expires (five minutes) or is cancelled. STOP during a pending preview
cancels it without writing; an interrupted capture never becomes a finalized
turn, so it can never confirm anything.

## Restart and degradation behavior

- **JARVIS restart:** pending confirmations live in process memory only — a
  restart clears them; every affirm needs a fresh `prepare_*`.
- **Adapter restart:** one restart-retry per read read-only; any restart
  invalidates every pending preview (explicit fence), prepares are required
  again; a confirm that may already have sent bytes is never retried and
  reports `unknown` ("Unklar — in Cannanas kontrollieren").
- **Audit sink unavailable for writes:** the audit refuses before any POST and
  the pending is spent — nothing is written unaccounted.
- **Blocked network / start failure:** Cannanas reports temporarily
  unavailable and everything else continues.

## Operator runbook (lacasa-server)

These steps require a human operator; **they were not executed by this
integration work**.

1. **Provision the dedicated least-privilege JARVIS key.** Create a brand-new
   Cannanas API key for JARVIS on the club side — never copy the Hermes key.
   Its role must permit the approved Grow reads/writes plus exactly the
   read-only propagation-material Inventory endpoint, and deny every Inventory
   mutation and sensitive non-Grow domain. Validate the role with
   `GET /v1/auth/test` before installing anything.
2. **Fill the credentials file.** Copy
   `deploy/cannanas/cannanas-adapter.credentials.example` to
   `deploy/cannanas/cannanas-adapter.credentials`, add the new key and fixed
   club id, `chmod 0600`. Verify with
   `deploy/cannanas/install-adapter.sh --check-env <file>`. The real file is
   gitignored; never commit or paste it.
3. **Install the pinned adapter wheel.**

   ```sh
   deploy/cannanas/install-adapter.sh --verify <wheel>
   deploy/cannanas/install-adapter.sh --install <wheel> \
       /opt/cannanas/adapter/venv <constraints-lock.txt> <release/release_info.json>
   ```

   Nothing is started by the installer; the JARVIS unit launches the child.
   After installing, re-verify the deployed artifact against the pinned SHA:

   ```sh
   deploy/cannanas/install-adapter.sh --verify <wheel>
   sha256sum <wheel>   # must equal the pinned
   # 7085943afc149d3f2356fca66d8a9d0bac9c2576166e99e36093a56b87153f5f
   ```
   The same pinned SHA is compiled into `core/cannanas_client.py`
   (`PINNED_WHEEL_SHA256`) and is verified again at every child start.
4. **Wire the unit environment.** The adapter credential file is the single new
   `EnvironmentFile=` in the JARVIS user unit (see
   `deploy/cannanas/README-DEPLOY.md`, including `CANNANAS_API_URL` never being
   set), then `systemctl --user daemon-reload` and restart the unit.
5. **Hermes SSH-STDIO cutover.** The development-host Hermes bridge on
   `lacasa15` stays on its own path; only after the JARVIS integration is
   proven should the Hermes STDIO adapter be switched over. The canonical
   adapter source under `/home/lacasa15/services/cannanas-mcp/` is never
   modified by this integration.
6. **Live acceptance (explicit approval).** With approval: one test plant, one
   strain, one batch, one plant/batch field update, one diary record — verify
   exact returned IDs, audit transitions and replay denial. Then prove the
   restart fence: restart JARVIS with a pending preview and confirm a fresh
   preview is required. Keep `JARVIS_CANNANAS_ACCEPTANCE_LIVE=ops-approved`
   reserved for the target host's acceptance run; the acceptance suite skips
   every live-dependent check without it.
7. **Final gate.** Adapter suite, Mark-LV full unittest suite, `python -m compileall`,
   secret scans and deployed SHA/version verification, before calling the
   integration ready.

## Security posture

- One dedicated least-privilege API key (not the Hermes key), fixed club
  origin, TLS verification on, no redirects, minimal child environment
  (`PATH` + the four adapter variables only).
- Credentials live only in the mode-0600 environment file readable by the
  unprivileged service user; nothing secret-bearing enters Git, arguments,
  prompts, tool results, audit, logs or speech.
- The audit JSONL before-POST rule and the atomic pending→executing consume
  guarantee exactly one executor per approved write with an accountable
  record; replay is denied.