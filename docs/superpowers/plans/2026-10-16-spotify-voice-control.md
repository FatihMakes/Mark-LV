# Spotify Voice Control Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add official Spotify Web API voice control that securely authorizes with PKCE and always controls the exact `raspotify (lacasa-server)` receiver without changing the existing Spotify audio/ducking path.

**Architecture:** `core/spotify.py` owns the private OAuth store, in-memory PKCE flow, locked token refresh, bounded HTTP policy, exact device selection, and normalized German control results. `plugins/spotify_control.py` exposes one narrow Gemini tool, while `core/headless.py` and `dashboard/server.py` expose only local operator authorization start/status plus the state-protected HTTPS callback. The existing librespot user service and `lacasa_spotify` sink remain the playback and ducking implementation; this feature only sends official Web API commands to the exact Connect device.

**Tech Stack:** Python 3.11+, stdlib PKCE/JSON/filesystem/threading primitives, `requests>=2.32,<3`, FastAPI, unittest/TestClient, Gemini plugin registry, Unix domain control socket, systemd user services, Spotify Web API.

**Spec:** `docs/superpowers/specs/2026-10-16-spotify-control-and-durable-devices-design.md`

## Global Constraints

- Scope is only Spotify PKCE authorization, secure token lifecycle, exact Raspotify targeting, the headless tool allowlist, local authorization CLI/control commands, documentation, and Spotify/server acceptance. Durable QR/device credentials are a separate implementation; define no device-registry, durable-login, bearer-lifetime, QR, or revocation behavior here.
- Use Spotify Authorization Code with PKCE and the official Web API only; never use a client secret, password, browser automation, stream extraction, `yt-dlp`, or YouTube Music.
- Register and send the redirect URI exactly as `https://lacasa-server.tail7a9c2.ts.net/api/spotify/callback`.
- Request only `user-read-playback-state`, `user-read-currently-playing`, and `user-modify-playback-state`.
- Store OAuth data only in ignored `config/spotify_oauth.json`, written atomically with mode `0600`; reject symlinks, unsafe ownership, unsafe permissions, and corrupt data as configuration errors (exit 78 at headless startup).
- Never print or return access tokens, refresh tokens, authorization codes, PKCE verifiers, or HTTP Authorization headers. The operator-visible authorization URL may contain only the expected Client ID, state, challenge, redirect URI, response type, and scopes.
- Refresh under one process lock before access-token expiry and once after HTTP 401. A rotated refresh token replaces the old value; an omitted refresh token preserves it.
- Refresh authorization expires six calendar months after the original authorization timestamp; refreshes do not move that timestamp or deadline. `invalid_grant`, revocation, the deadline, or a second 401 atomically clears unusable access/refresh tokens and requires consent again.
- Resolve the Connect target by case-sensitive exact name `raspotify (lacasa-server)` on every action; never fall back to the active phone, a partial name, a cached ID, or a different receiver.
- Fail closed for zero/multiple exact matches, null device IDs, restricted receivers, and `supports_volume == false` for volume. Accept volume only as an integer from 0 through 100 (booleans are not integers for this interface).
- `current` reports playback only when the account playback-state device ID equals the freshly resolved LaCasa receiver ID and `is_playing` is true. Do not preflight playback state for any mutating action.
- For HTTP 429, honor `Retry-After` with one bounded wait/retry and then return an honest German rate-limit result. Network/Spotify failures must not crash Gemini or Home Assistant.
- Headless Gemini exposes exactly the existing read-only `home_assistant` tool and the new mutating `spotify_control` tool. Desktop, shell, camera, memory, shutdown, arbitrary plugins, and HA writes remain unreachable.
- Keep Raspotify as the existing user service on `lacasa_spotify`. Do not modify `core/ducking.py`, the ducking guard, sink gain formula `10**(-18/60)`, master gain, stream volume, or pause/restart/seek/transfer Spotify as part of announcements.
- Preserve `python -m core.headless pair` and the current transient pairing path. New control messages are bounded line-delimited JSON and must not rotate or revoke any current/future durable device credentials.
- Preserve unrelated working-tree changes (`plugins/cannabis_status.py` and `DASHBOARD_GUIDE.md`). Stage only paths named by each task.
- Local test runner is `/root/.hermes/cache/scratch/marklv-task1-venv/bin/python -m unittest discover -s tests -v`. Do not put real Spotify credentials, callback values, or authorization URLs into tests, fixtures, commits, documentation, reports, or chat.

## Review Focus

1. Two callers hit an expiring token simultaneously: one refresh request occurs under the process lock, both receive the saved token, and refresh-token rotation/omission cannot race an older value back into the store; pin this in Task 3.
2. A valid callback is delayed, duplicated, or races another callback: state is consumed before token exchange, so only one exchange can occur and no failed/replayed callback can write tokens; pin this in Task 2.
3. Spotify returns structurally valid but hostile device/playback payloads (duplicate exact names, null IDs, restricted devices, wrong active ID, null item): every path fails closed without controlling or reporting another device; pin this in Task 4.
4. A request encounters both authentication and rate-limit failures: auth and 429 retries each occur at most once under a total three-attempt ceiling, with no recursive/unbounded loop; pin this in Task 3.
5. Responses, exceptions, and service logs contain malformed JSON or server-supplied error text that echoes credentials: return a fixed concise German result and never interpolate token/verifier/code/header values; pin this in Tasks 1–4.

---

### Task 1: Private OAuth store and six-month authorization model

**Files:**
- Create: `core/spotify.py`
- Create: `tests/test_spotify_oauth.py`

**Interfaces:**
- Produces `REDIRECT_URI = "https://lacasa-server.tail7a9c2.ts.net/api/spotify/callback"`, `TARGET_DEVICE_NAME = "raspotify (lacasa-server)"`, and `REQUIRED_SCOPES: tuple[str, ...]` containing exactly the three scopes in Global Constraints.
- Produces `SpotifyConfigError(ValueError)`, `SpotifyAuthorizationRequired(RuntimeError)`, and immutable `OAuthRecord(client_id: str, authorized_at: datetime, authorization_expires_at: datetime, access_token: str | None, access_expires_at: datetime | None, refresh_token: str | None, scopes: tuple[str, ...])`.
- Produces `OAuthStore(path: Path = Path(__file__).resolve().parents[1] / "config" / "spotify_oauth.json")` with `load() -> OAuthRecord | None`, `save(record: OAuthRecord) -> None`, and `invalidate_tokens() -> OAuthRecord | None`.
- `authorization_expires_at` is computed once by a private calendar-month helper: add six UTC calendar months to `authorized_at`, clamp the day to the destination month, and never recompute it during refresh.
- Missing store means “not authorized.” Existing store/parent ownership or permission violations, symlinks, malformed JSON, invalid timestamps/scopes, and missing required fields raise `SpotifyConfigError`; because it subclasses `ValueError`, current headless startup maps it to exit 78.

- [ ] **Step 1: Write failing tests for constants, serialization, and calendar lifetime**

Add `SpotifyOAuthStoreTests` using a temporary `config/` directory. Pin the public contract rather than atomic-write internals:

```python
def test_constants_are_exact(self):
    self.assertEqual(REDIRECT_URI, "https://lacasa-server.tail7a9c2.ts.net/api/spotify/callback")
    self.assertEqual(TARGET_DEVICE_NAME, "raspotify (lacasa-server)")
    self.assertEqual(REQUIRED_SCOPES, (
        "user-read-playback-state",
        "user-read-currently-playing",
        "user-modify-playback-state",
    ))

def test_store_round_trip_is_private_and_six_calendar_months(self):
    record = authorized_record(authorized_at="2026-08-31T12:00:00Z")
    store.save(record)
    self.assertEqual(store.load().authorization_expires_at.isoformat(),
                     "2027-02-28T12:00:00+00:00")
    self.assertEqual(store.path.stat().st_mode & 0o777, 0o600)
    self.assertNotIn("client_secret", store.path.read_text())
```

Also assert `save()` writes the exact JSON fields `client_id`, `authorized_at`, `authorization_expires_at`, `access_token`, `access_expires_at`, `refresh_token`, and `scopes`, and preserves the Client ID byte-for-byte rather than normalizing it.

- [ ] **Step 2: Write failing tests for fail-closed storage and atomic invalidation**

Cover: missing file returns `None`; mode `0644`; symlinked store; symlinked parent; mocked foreign `st_uid`; corrupt JSON; wrong types; naive/non-UTC timestamps; missing required scopes; and a simulated `os.replace` failure that leaves the old valid file readable. Assert `invalidate_tokens()` atomically sets only `access_token`, `access_expires_at`, and `refresh_token` to null while retaining non-secret Client ID, original authorization timestamp/deadline, and scopes. Capture the injected logger and assert unique fake access/refresh values never occur in logs or exception strings.

- [ ] **Step 3: Run the OAuth-store tests to verify RED**

Run:

```bash
/root/.hermes/cache/scratch/marklv-task1-venv/bin/python -m unittest discover -s tests -p 'test_spotify_oauth.py' -v
```

Expected: FAIL because `core.spotify`, `OAuthRecord`, and `OAuthStore` do not exist; no existing test should be changed to manufacture the failure.

- [ ] **Step 4: Implement the minimal private store**

Use `lstat`/`os.open` with no symlink following where available, owner `os.getuid()`, parent not group/world-writable (`mode & 0o022 == 0`), and existing-file mode exactly `0600`. Create a same-directory mode-`0600` temporary file, flush and `fsync`, `os.replace`, then `fsync` the directory. Parse ISO-8601 UTC explicitly. The serialized file must contain no client secret field and no verifier/state. Do not log record values.

- [ ] **Step 5: Run targeted tests to verify GREEN**

Run the Task 1 command again. Expected: all `SpotifyOAuthStoreTests` pass with no warnings or secret-bearing output.

- [ ] **Step 6: Commit only the store slice**

```bash
git add core/spotify.py tests/test_spotify_oauth.py
git diff --cached --check
git commit -m "feat: add private Spotify OAuth store"
```

### Task 2: Exact PKCE flow, callback, and local authorization commands

**Files:**
- Modify: `core/spotify.py`
- Modify: `core/headless.py`
- Modify: `dashboard/server.py`
- Modify: `tests/test_spotify_oauth.py`
- Modify: `tests/test_dashboard_headless.py`

**Interfaces:**
- Consumes `OAuthStore`, `OAuthRecord`, `REDIRECT_URI`, and `REQUIRED_SCOPES` from Task 1.
- Produces immutable `AuthorizationStart(url: str, state_expires_at: datetime)` and `AuthorizationStatus(authorized: bool, reauthorization_required: bool, authorized_at: datetime | None, authorization_expires_at: datetime | None, access_expires_at: datetime | None)`; neither type exposes tokens, code, state, or verifier.
- Produces `SpotifyOAuth(store: OAuthStore, request: Callable = requests.request, clock: Callable[[], datetime] = utc_now, pending_ttl_seconds: int = 600, logger: Callable[[str], None] = no_secret_logger)` with:
  - `start_authorization(client_id: str) -> AuthorizationStart`
  - `complete_authorization(*, state: str | None, code: str | None, error: str | None = None) -> AuthorizationStatus`
  - `status() -> AuthorizationStatus`
- A Client ID must be a non-empty printable string of at most 128 characters with no whitespace/control characters; preserve it exactly and do not guess/repair a rejected identifier.
- `DashboardServer(public_url=None, host=None, spotify_oauth: SpotifyOAuth | None = None)` uses the lazily shared default OAuth instance in production and an injected instance in tests.
- Produces `default_oauth() -> SpotifyOAuth`, a lock-protected lazy process singleton used by the production dashboard and later by the default controller; import alone has no filesystem or network side effect.
- `GET /api/spotify/callback` is the only new unauthenticated route. It calls `complete_authorization`; success returns a no-store HTML page saying Spotify is connected, while all errors return fixed no-store HTML/400 without echoing query values.
- Produces `request_control(payload: dict, path: Path | None = None) -> dict` in `core.headless`; requests/responses are one UTF-8 JSON object plus newline, maximum 4096 bytes.
- Control commands are `{"command":"spotify-auth-start","client_id":"<literal>"}` and `{"command":"spotify-auth-status"}`. Start replies `{"ok":true,"authorization_url":"...","state_expires_at":"..."}`; status replies `{"ok":true,"authorized":bool,"reauthorization_required":bool,"authorized_at":str|null,"authorization_expires_at":str|null,"access_expires_at":str|null}`; failures use only `{"ok":false,"error":"<fixed text>"}`. Keep legacy raw `pair\n` and `request_pair()` behavior unchanged.
- CLI adds `spotify-auth-start --client-id CLIENT_ID` and `spotify-auth-status`; start prints only the authorization URL, status prints concise non-secret JSON, and errors exit 1.

- [ ] **Step 1: Write the failing exact-PKCE tests**

Add tests with deterministic injected randomness/clock or patch `secrets.token_urlsafe`. Parse the URL and assert:

```python
params = parse_qs(urlsplit(start.url).query)
self.assertEqual(urlsplit(start.url)._replace(query="").geturl(),
                 "https://accounts.spotify.com/authorize")
self.assertEqual(params["response_type"], ["code"])
self.assertEqual(params["redirect_uri"], [REDIRECT_URI])
self.assertEqual(params["code_challenge_method"], ["S256"])
self.assertEqual(params["scope"], [" ".join(REQUIRED_SCOPES)])
self.assertEqual(params["code_challenge"], [
    base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
          .rstrip(b"=").decode("ascii")
])
self.assertNotIn("client_secret", params)
```

Assert the verifier is 43–128 RFC 7636 unreserved characters, state is random, the pending verifier/state live only in memory, and starting authorization performs no token-store write.

- [ ] **Step 2: Write failing one-use callback tests**

Test missing, wrong, stale (at exactly `created_at + 600s`), and replayed state; missing code; Spotify `error=access_denied`; malformed token JSON; missing required scope; and token endpoint failure. Every case must consume a matching state before exchange where applicable, call the token endpoint zero or one time as appropriate, and leave the store unchanged. Add a two-thread/barrier callback test proving one state produces exactly one token exchange/save.

For a valid callback assert one form-encoded `POST https://accounts.spotify.com/api/token` with exactly `grant_type=authorization_code`, literal code, `REDIRECT_URI`, Client ID, and original verifier; no Basic Authorization header and no client secret. Assert the saved record uses callback time as the original authorization timestamp, token `expires_in`, returned refresh token/scopes, and six-month deadline.

- [ ] **Step 3: Write failing callback-route and control-socket tests**

In `tests/test_dashboard_headless.py`, inject the fake OAuth object and assert:

```python
response = client.get("/api/spotify/callback", params={"state": "one-use", "code": "code"})
self.assertEqual(response.status_code, 200)
self.assertEqual(response.headers["cache-control"], "no-store")
self.assertNotIn("code", response.text)
```

Add 400 cases for absent/wrong/stale/replayed state without token writes. Extend `ControlTests` to send both JSON commands through a real temporary Unix socket and assert authorization status contains no `access_token`, `refresh_token`, verifier, or state. Add malformed JSON, unknown command, over-4096-byte/no-newline framing, and timeout tests. Assert legacy `pair\n` still works and does not call Spotify OAuth.

- [ ] **Step 4: Run the focused tests to verify RED**

Run:

```bash
/root/.hermes/cache/scratch/marklv-task1-venv/bin/python -m unittest discover -s tests -p 'test_spotify_oauth.py' -v
/root/.hermes/cache/scratch/marklv-task1-venv/bin/python -m unittest discover -s tests -p 'test_dashboard_headless.py' -v
```

Expected: FAIL on missing PKCE methods, callback route, JSON control commands, and CLI interfaces while existing pairing tests remain green.

- [ ] **Step 5: Implement PKCE and callback minimally**

Generate the verifier/state with `secrets`, SHA-256 the ASCII verifier, base64url without padding, and set `code_challenge_method=S256`. Hold pending flows only in a lock-protected in-memory `state -> (client_id, verifier, created_at)` map. On callback, validate then pop under the lock before any token HTTP request. Build form data explicitly; never pass a client secret or Basic header. Treat returned scopes as a set for validation but store them in `REQUIRED_SCOPES` order.

- [ ] **Step 6: Implement bounded JSON control and CLI dispatch**

Read at most 4097 bytes under the existing two-second timeout, require one newline and no trailing second command, and return fixed errors. Keep `pair` as a compatibility branch; it must continue to rotate only the current transient pairing state exactly as before. Route CLI output explicitly by command so status cannot accidentally print the dataclass/store.

- [ ] **Step 7: Run Task 2 tests to verify GREEN**

Run both focused commands again. Expected: all pass; callback response/control output/log captures contain none of the fake code/verifier/access/refresh values.

- [ ] **Step 8: Commit only authorization plumbing**

```bash
git add core/spotify.py core/headless.py dashboard/server.py tests/test_spotify_oauth.py tests/test_dashboard_headless.py
git diff --cached --check
git commit -m "feat: authorize Spotify with PKCE"
```

### Task 3: Locked refresh, 401 recovery, and bounded HTTP policy

**Files:**
- Modify: `core/spotify.py`
- Modify: `tests/test_spotify_oauth.py`
- Create: `tests/test_spotify.py`

**Interfaces:**
- Consumes `SpotifyOAuth` and its store from Tasks 1–2.
- Adds `SpotifyOAuth.access_token(*, force_refresh: bool = False) -> str` protected by one `threading.Lock` shared by all callers of the production default OAuth object.
- Produces `SpotifyAPIError(RuntimeError)` with a non-secret category (`unauthorized`, `premium`, `no_device`, `rate_limited`, `network`, `bad_response`) and no raw response/request body.
- Produces `SpotifyHTTP(oauth: SpotifyOAuth, request: Callable = requests.request, sleep: Callable[[float], None] = time.sleep, timeout_seconds: float = 10.0, max_retry_after_seconds: float = 5.0)` with `request(method: str, path: str, *, params: dict | None = None, json_body: dict | None = None) -> requests.Response`. Every call keeps TLS verification enabled and sets `allow_redirects=False` so a bearer is never forwarded through an unexpected redirect.
- API base is exactly `https://api.spotify.com/v1`. Each logical request permits at most one forced-refresh retry after 401 and one sleep/retry after 429, with at most three HTTP attempts total. No recursion.
- A 429 retry occurs only for a finite, nonnegative numeric `Retry-After`; clamp it to `5.0` seconds. Missing/invalid/negative values return `rate_limited` immediately rather than guessing a delay.

- [ ] **Step 1: Write failing pre-expiry and refresh-response tests**

Use a fake clock and token endpoint. Cover valid token outside the 60-second refresh window (no call), token at/before the 60-second boundary (one refresh), rotated refresh token (replace), omitted refresh token (preserve), omitted refresh scope (preserve scopes), and original authorization timestamp/deadline unchanged after every refresh. Assert PKCE refresh form is exactly `grant_type=refresh_token`, existing refresh token, and Client ID, with no client secret or Basic header.

- [ ] **Step 2: Write failing expiry/revocation and concurrency tests**

Assert the exact six-calendar-month boundary invalidates tokens before returning even a nominally live access token. Assert refresh `invalid_grant` invalidates once and immediately raises `SpotifyAuthorizationRequired`; a second call performs no refresh loop. Simulate a second 401 and assert invalidation/reauthorization. Use two real threads blocked on the refresh request and assert one HTTP refresh/save occurs and both callers receive the same new access token.

- [ ] **Step 3: Write failing 401/429/network policy tests**

In `tests/test_spotify.py`, queue fake responses and assert:

```python
self.assertEqual(methods_for([401, 204]), ["PUT", "PUT"])
self.assertEqual(oauth.force_refresh_calls, 1)
self.assertEqual(methods_for([429, 204], retry_after="2"), ["PUT", "PUT"])
sleep.assert_called_once_with(2.0)
self.assertLessEqual(len(methods_for([401, 429, 429], retry_after="60")), 3)
```

Also test 429 without/invalid/negative `Retry-After` performs no retry, cap `60` to `5.0`, a second 429 returns `rate_limited`, connect/read timeout returns `network`, and malformed/error JSON returns a fixed `bad_response` without raw payload text. Pin 403 `reason=PREMIUM_REQUIRED` to `premium` and the known “No active device found” condition to `no_device`, with no retry and no raw Spotify message in the exception. Assert every request uses timeout `10.0`, normal TLS verification, and `allow_redirects=False`. Include 401→429→success and 429→401→success to pin the total-attempt bound.

- [ ] **Step 4: Run focused tests to verify RED**

Run:

```bash
/root/.hermes/cache/scratch/marklv-task1-venv/bin/python -m unittest discover -s tests -p 'test_spotify_oauth.py' -v
/root/.hermes/cache/scratch/marklv-task1-venv/bin/python -m unittest discover -s tests -p 'test_spotify.py' -v
```

Expected: FAIL because refresh/token and API retry interfaces are absent.

- [ ] **Step 5: Implement the token and request state machines**

Inside the refresh lock, reload the store after acquiring it so a waiting caller observes the first caller's token. Check the six-month deadline before access expiry. Persist a complete replacement record atomically after successful refresh. Classify endpoint errors from status plus bounded, sanitized error fields only; never retain or log response headers/bodies. Use loop booleans (`did_auth_retry`, `did_rate_retry`) and an attempt counter, not recursive calls.

- [ ] **Step 6: Run focused tests to verify GREEN**

Run both Task 3 commands. Expected: all pass, including real-thread single-refresh and mixed 401/429 ceilings.

- [ ] **Step 7: Commit only token/HTTP lifecycle changes**

```bash
git add core/spotify.py tests/test_spotify_oauth.py tests/test_spotify.py
git diff --cached --check
git commit -m "feat: refresh Spotify tokens safely"
```

### Task 4: Exact receiver resolution and all voice-control actions

**Files:**
- Modify: `core/spotify.py`
- Modify: `tests/test_spotify.py`

**Interfaces:**
- Consumes `SpotifyHTTP` and `TARGET_DEVICE_NAME`.
- Produces immutable `SpotifyDevice(id: str, name: str, restricted: bool, supports_volume: bool)` and `SpotifyController(http: SpotifyHTTP, target_name: str = TARGET_DEVICE_NAME)`.
- Produces `default_controller() -> SpotifyController`, a lazy process singleton built from `default_oauth()` and used by the plugin; tests inject their own controller and never touch the live store/network.
- Produces `SpotifyController.perform(action: str, *, query: str | None = None, kind: str = "track", volume_percent: int | None = None) -> str` for actions `play`, `pause`, `resume`, `next`, `previous`, `volume`, and `current`.
- Private `resolve_device(*, require_volume: bool = False) -> SpotifyDevice` always performs `GET /me/player/devices` and accepts exactly one case-sensitive name match with non-null/non-empty string ID, `is_restricted is False`, and (for volume) `supports_volume is True`.
- `play` performs exactly one `GET /search` with `q=<literal query>`, `type=<kind>`, `limit=3`, chooses `items[0]`, then resolves the receiver and calls play. Track body is `{"uris":[uri]}`; album/artist body is `{"context_uri":uri}`.
- Mutating endpoint map is: pause `PUT /me/player/pause`, resume `PUT /me/player/play`, next `POST /me/player/next`, previous `POST /me/player/previous`, and volume `PUT /me/player/volume`; every request includes `device_id=<resolved ID>`, volume also includes `volume_percent=<int>`.
- `current` resolves the receiver, then calls `GET /me/player` exactly once and reports a track only if returned `device.id` equals the resolved ID, `is_playing is True`, and a valid item exists.

- [ ] **Step 1: Write failing exact-device tests**

Cover no exact match (including a prefix/case-only near match), two exact matches even if only one is active/unrestricted, null/empty/non-string ID, `is_restricted` true/missing/malformed, and volume support false/missing. For every rejected payload assert no mutating endpoint or playback-state call occurs. Add malformed top-level `devices` payload and server-supplied fake token text; assert a fixed German safe result with no echoed value.

- [ ] **Step 2: Write failing search-to-play tests for every kind**

For `track`, `album`, and `artist`, assert exactly one search request, `limit=3`, first item selected, device lookup after search, and the exact play body/device ID. Assert no `/me/player` preflight and no transfer endpoint. Add empty/malformed results and missing/invalid URI tests; expect one short German clarification/no-result response and no device/play request. Validate missing/blank query and invalid kind before any HTTP call.

- [ ] **Step 3: Write failing action and LaCasa-only current tests**

Test every endpoint/method/parameter listed in Interfaces. Assert volume accepts `0` and `100` and rejects `-1`, `101`, float, string, `None`, and `True` without HTTP. For `current`, cover same ID playing track (include track and artist in German result), another phone/speaker ID, null device, paused state, 204, null item, episode/ad/unknown, and malformed JSON. Every nonmatching case must say nothing is currently playing on LaCasa and must not reveal/report the other device's item.

- [ ] **Step 4: Write failing normalized-failure tests**

Pin German user-facing semantics without exposing remote bodies:

```python
self.assertIn("einmal", not_authorized.lower())
self.assertIn("raspotify (lacasa-server)", missing_receiver)
self.assertIn("premium", premium_required.lower())
self.assertIn("rate", rate_limited.lower())
self.assertIn("nicht erreichbar", network_failure.lower())
```

Assert Premium/receiver/rate/network errors do not invoke another action, restart/reinstall librespot, call systemd, change sink/master, pause as fallback, transfer playback, or escape from `perform()`.

- [ ] **Step 5: Run `test_spotify.py` to verify RED**

Run:

```bash
/root/.hermes/cache/scratch/marklv-task1-venv/bin/python -m unittest discover -s tests -p 'test_spotify.py' -v
```

Expected: FAIL on missing controller/device/action behavior.

- [ ] **Step 6: Implement the minimal controller**

Validate action-specific arguments first. Keep response parsing defensive and local to each endpoint. Resolve devices afresh for every action. Do not cache IDs, inspect currently active playback before mutations, or add playlist/library/queue/seek/shuffle/repeat features. Convert only known sanitized item fields to concise German text.

- [ ] **Step 7: Run Task 4 tests to verify GREEN**

Run the Task 4 command again. Expected: all device, action, current, and fixed-error tests pass.

- [ ] **Step 8: Commit exact targeting/control**

```bash
git add core/spotify.py tests/test_spotify.py
git diff --cached --check
git commit -m "feat: control the exact LaCasa Spotify receiver"
```

### Task 5: One Spotify Gemini tool and the exact headless allowlist

**Files:**
- Create: `plugins/spotify_control.py`
- Modify: `core/headless.py`
- Modify: `main.py:642-646`
- Modify: `tests/test_headless.py:64-73`
- Create: `tests/test_spotify_plugin.py`
- Modify: `deploy/spotify/raspotify.conf:3`

**Interfaces:**
- Consumes `SpotifyController.perform()` from Task 4 through a lazy `get_controller() -> SpotifyController` helper in the plugin, so importing the plugin never reads/writes credentials or performs network I/O.
- Produces one `PLUGIN` named `spotify_control` with required `action` enum `play|pause|resume|next|previous|volume|current`; optional `query`; optional `kind` enum `track|album|artist` defaulting to `track`; optional integer `volume_percent` minimum 0, maximum 100.
- Produces `run(parameters: dict, player=None, session_memory=None) -> str`; it validates the action-specific contract and calls `get_controller().perform(...)` once. It returns a German string and never raises an operational Spotify failure into Gemini.
- Replaces misleading `read_only_registry()` with `trusted_headless_registry() -> PluginRegistry`, importing only `plugins.home_assistant` and `plugins.spotify_control`. It preserves the narrowed HA declaration (`list|query|temperature`, no `value`) and registers Spotify unchanged.
- `tool_allowed(name: str, args: dict) -> bool` permits only narrowed HA reads and the seven Spotify actions. Plugin validation remains the second fail-closed layer for required/query/kind/volume argument combinations.
- `deploy/spotify/raspotify.conf` changes only `LIBRESPOT_NAME` to `"raspotify (lacasa-server)"`; all backend/device/mixer/ducking routing lines remain byte-for-byte unchanged.

- [ ] **Step 1: Write failing plugin schema/dispatch tests**

In `tests/test_spotify_plugin.py`, assert the exact plugin name, action enum, kind enum/default, volume bounds, and action-specific rejection before controller invocation. Patch `get_controller()` with a real fake controller and assert valid parameters are passed once and the returned German text is unchanged. Assert fake access/refresh/code/verifier strings in a raised internal exception never appear in the result or captured output.

- [ ] **Step 2: Extend failing headless registry and dispatch tests**

Change the expected declarations to exactly `['home_assistant', 'spotify_control']` in deterministic order. Assert HA `set` and all unrelated tools remain denied at `JarvisLive._execute_tool`. Assert all seven Spotify actions pass the policy gate and dispatch only to `plugins.spotify_control.run`; an unknown Spotify action is denied before plugin execution. Assert registry construction does not discover/import `plugins/cannabis_status.py` or any desktop action.

- [ ] **Step 3: Add the failing receiver-config consistency assertion**

In `tests/test_spotify.py`, read only the public deployment template and assert `LIBRESPOT_NAME="raspotify (lacasa-server)"`, `LIBRESPOT_DEVICE="lacasa_spotify"`, and `LIBRESPOT_MIXER=softvol`. This guards the exact Web API target without testing or altering ducking internals.

- [ ] **Step 4: Run focused tests to verify RED**

Run:

```bash
/root/.hermes/cache/scratch/marklv-task1-venv/bin/python -m unittest discover -s tests -p 'test_spotify_plugin.py' -v
/root/.hermes/cache/scratch/marklv-task1-venv/bin/python -m unittest discover -s tests -p 'test_headless.py' -v
/root/.hermes/cache/scratch/marklv-task1-venv/bin/python -m unittest discover -s tests -p 'test_spotify.py' -v
```

Expected: FAIL because the plugin/registry are absent and the reference receiver name is stale; existing HA write-denial assertions remain green.

- [ ] **Step 5: Implement the plugin and trusted registry**

Construct explicit `PluginRecord`s from deep-copied metadata as the current registry does; do not invoke plugin discovery. Update `main.py` only to call `trusted_headless_registry()`. Keep controller creation lazy and do not add Spotify configuration fields to `config/api_keys.json`.

- [ ] **Step 6: Align only the Raspotify display-name template**

Set `LIBRESPOT_NAME="raspotify (lacasa-server)"`. Do not change or restart `deploy/audio/raspotify.service`, `lacasa_spotify`, Pulse/PipeWire routes, softvol, initial volume, ducking controller, or guard.

- [ ] **Step 7: Run Task 5 tests to verify GREEN**

Run all three focused commands. Expected: two and only two headless declarations, safe runtime dispatch, and exact receiver configuration pass.

- [ ] **Step 8: Commit only tool/allowlist integration**

```bash
git add plugins/spotify_control.py core/headless.py main.py tests/test_headless.py tests/test_spotify_plugin.py tests/test_spotify.py deploy/spotify/raspotify.conf
git diff --cached --check
git commit -m "feat: expose Spotify voice control in headless mode"
```

### Task 6: Ignore rules, operator documentation, full verification, and server acceptance

**Files:**
- Modify: `.gitignore`
- Modify: `tests/test_gitignore_secrets.py`
- Modify: `docs/server-voice-quickstart.md`
- Modify: `docs/spotify-server.md`
- Modify: `requirements-headless.txt` only if the deployed file does not already satisfy `requests>=2.32,<3` (it currently does; expect no change)

**Interfaces:**
- Consumes `python -m core.headless spotify-auth-start --client-id ...` and `spotify-auth-status` from Task 2.
- Documentation gives the Spotify developer-app fields, exact redirect URI, Web API selection, no-client-secret rule, Premium/development-user prerequisite, six-month reauthorization, seven German voice-action examples, exact receiver, safe troubleshooting, and explicit automated/server/physical acceptance boundaries.
- Durable QR/device commands and behavior are explicitly deferred to the separate durable-device plan; current transient `pair` instructions stay valid.

- [ ] **Step 1: Write the failing explicit-ignore assertions**

Extend `tests/test_gitignore_secrets.py` to assert both `git check-ignore --no-index config/spotify_oauth.json` succeeds and `git ls-files -- config/spotify_oauth.json` is empty. Keep existing generic secret tests.

- [ ] **Step 2: Run the ignore test to verify RED**

Run:

```bash
/root/.hermes/cache/scratch/marklv-task1-venv/bin/python -m unittest discover -s tests -p 'test_gitignore_secrets.py' -v
```

Expected: the explicit Spotify OAuth path assertion fails before `.gitignore` is changed.

- [ ] **Step 3: Add the explicit private OAuth ignore and update docs**

Add `config/spotify_oauth.json` by exact name to `.gitignore`, despite broad patterns, so intent is reviewable. Update `docs/server-voice-quickstart.md` with:

```text
App name: any personal name
Website: blank
Redirect URI: https://lacasa-server.tail7a9c2.ts.net/api/spotify/callback
API/SDK: Web API
Credential requested from user: Client ID only (never client secret/password)
Start: .venv/bin/python -m core.headless spotify-auth-start --client-id '<CLIENT_ID>'
Status: .venv/bin/python -m core.headless spotify-auth-status
```

Explain that the authorization URL is opened by the operator over trusted HTTPS, consent must be repeated after six calendar months/revocation/`invalid_grant`, and the status command never displays tokens. Reconcile `docs/spotify-server.md` with the current user-service architecture and exact receiver; remove stale “LaCasa Boxen”/package-service instructions rather than creating a second setup path.

- [ ] **Step 4: Run ignore and all automated verification**

Run:

```bash
/root/.hermes/cache/scratch/marklv-task1-venv/bin/python -m unittest discover -s tests -p 'test_gitignore_secrets.py' -v
/root/.hermes/cache/scratch/marklv-task1-venv/bin/python -m unittest discover -s tests -v
/root/.hermes/cache/scratch/marklv-task1-venv/bin/python -m compileall -q core plugins dashboard tests main.py
python - <<'PY'
from pathlib import Path
import os, re
text = Path('dashboard/static/app.html').read_text()
Path(os.environ['TMPDIR'], 'spotify-dashboard-app.js').write_text(
    '\n'.join(re.findall(r'<script>([\s\S]*?)</script>', text)))
PY
node --check "$TMPDIR/spotify-dashboard-app.js"
rm -f "$TMPDIR/spotify-dashboard-app.js"
git check-ignore --no-index config/spotify_oauth.json
test -z "$(git ls-files -- config/spotify_oauth.json)"
git diff --check
```

Expected: all unittest cases pass using the stated runner; compileall and Node syntax exit 0; OAuth file is ignored and untracked; diff check is clean.

- [ ] **Step 5: Perform a secret/scope and out-of-scope review before commit**

Inspect the owned working diff before staging:

```bash
git diff -- .gitignore tests/test_gitignore_secrets.py docs/server-voice-quickstart.md docs/spotify-server.md
git status --short
git diff -- core/ducking.py deploy/audio/spotify-ducking-guard.service deploy/audio/ensure-routes.py
```

Expected: no real token/code/verifier/authorization URL/client secret; only the three required scopes; no durable-device implementation; no ducking/audio-route diff; unrelated working-tree files unstaged.

- [ ] **Step 6: Commit documentation and ignore rules selectively**

```bash
git add .gitignore tests/test_gitignore_secrets.py docs/server-voice-quickstart.md docs/spotify-server.md
git diff --cached --check
git commit -m "docs: add Spotify authorization and acceptance guide"
```

- [ ] **Step 7: Deploy the exact reviewed commit without exposing credentials**

Record `git rev-parse HEAD`, deploy that exact revision to `/home/lacasa/Mark-LV-LaCasa` on `lacasa-server`, and verify the target returns the same hash before restart. Install `requirements-headless.txt` in the existing target venv. Do not copy a test OAuth file, print environment/config values, restart Raspotify, or alter Tailscale/audio routing. Restart only `jarvis.service` after `systemctl --user daemon-reload` if its deployed files changed.

- [ ] **Step 8: Verify service boundaries before authorization**

On `lacasa-server` as `lacasa`, run:

```bash
systemctl --user is-active jarvis raspotify spotify-ducking-guard pipewire pipewire-pulse wireplumber
curl -sS -o /dev/null -w 'HTTPS=%{http_code} TLS=%{ssl_verify_result}\n' https://lacasa-server.tail7a9c2.ts.net/
curl -sS -o /dev/null -w 'STATUS=%{http_code}\n' https://lacasa-server.tail7a9c2.ts.net/api/status
stat -c '%a %U %n' /run/user/$(id -u)/jarvis/control.sock
ss -ltnp | grep '127.0.0.1:8000'
.venv/bin/python -m core.headless spotify-auth-status
```

Expected: all user services active; HTTPS `200` and TLS verify `0`; unauthenticated status `401`; control socket `600` owned by `lacasa`; dashboard only on loopback; Spotify status says authorization is required without showing token fields.

- [ ] **Step 9: Complete real PKCE consent with the user-owned Spotify app**

Have the user register the exact redirect URI and add the intended Premium account to Spotify's development-mode user allowlist if required. Run `spotify-auth-start` with only their Client ID, give them only the returned authorization URL, and have them complete Spotify's consent page themselves. Never request their Spotify password, client secret, callback code, or tokens in chat. Re-run `spotify-auth-status`; expected: authorized with non-secret authorization/access-expiry timestamps and the fixed six-month reauthorization date. Verify `config/spotify_oauth.json` is regular, `0600`, owned by `lacasa`, and ignored without printing it.

- [ ] **Step 10: Exercise real Web API control against the exact receiver**

With `raspotify (lacasa-server)` visible in Spotify, use the trusted dashboard/voice path to issue a uniquely identifiable low-volume test track request, then `current`, volume, pause, resume, next, and previous. Record sanitized outcomes only. Expected: search chooses the first requested-type result; playback starts on the exact receiver, not the iPhone; `current` reports it only while that device ID is active; no action transfers to or controls another device. Temporarily make the receiver unavailable and confirm the German missing-receiver prompt appears without service restart/reinstall.

- [ ] **Step 11: Verify restart persistence and refresh/failure acceptance**

Restart `jarvis.service`, confirm `spotify-auth-status` remains authorized, and repeat a harmless `current`/pause-resume cycle. Use test doubles for 401/429/`invalid_grant`; do not intentionally revoke or wait out live credentials merely to test errors. Review `journalctl --user -u jarvis` locally for fixed status messages and absence of token/code/verifier/header values; do not paste the journal into reports. Confirm Home Assistant reads still work after Spotify network/rate-limit failures.

- [ ] **Step 12: Confirm audio/ducking regression boundaries and final physical check**

While a real track progresses, record Raspotify PID and `lacasa_spotify` gain before/during/after one spoken JARVIS response. Expected: existing behavior remains `65536 → 32846 → 65536` (or exact equivalent relative to the current baseline), analog master unchanged, PID unchanged, track progress continuous, and no pause/restart/seek/transfer caused by speech. The user then verifies on the physical iPhone: request a song by voice, hear it on server speakers, hear JARVIS duck over it, continue speaking after the reply, use STOP, and confirm music never moves to the phone. Report this human hearing/Safari result separately from automated and server evidence.
