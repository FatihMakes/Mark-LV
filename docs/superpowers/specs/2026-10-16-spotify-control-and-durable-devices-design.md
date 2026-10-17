# Spotify Voice Control and Durable Device Access Design

**Date:** 2026-10-16  
**Status:** User authorized implementation with safe defaults  
**Target host:** `lacasa-server` only

## Intent

Finish Mark-LV-LaCasa as a headless, private JARVIS that the user can open from an iPhone, speak to repeatedly, and use like an Alexa for Spotify on the analog speakers attached to `lacasa-server`.

YouTube Music is explicitly out of scope. Spotify is the sole music provider. Existing Home Assistant read access, Gemini Live conversation, phone microphone, analog JARVIS speech, Spotify Connect, and crash-safe Spotify ducking remain in service.

## User experience

A user scans one of four named QR codes once or whenever arriving at LaCasa. With Tailscale active, Safari opens the trusted private HTTPS dashboard and authenticates without an expiring pairing PIN. The user taps the microphone and can hold a conversation. During JARVIS speech the phone microphone closes; after the physical playback tail it resumes once. STOP discards the remaining answer.

Voice music examples:

- “JARVIS, spiele In the End von Linkin Park.”
- “JARVIS, spiele Musik von Queen.”
- “JARVIS, pausiere Spotify.”
- “JARVIS, weiter.”
- “JARVIS, nächster Titel.”
- “JARVIS, vorheriger Titel.”
- “JARVIS, Spotify auf 35 Prozent.”
- “JARVIS, was läuft gerade?”

For a clear request, JARVIS selects the top relevant Spotify result and starts it immediately on `raspotify (lacasa-server)`. If the request is genuinely ambiguous, JARVIS asks one short question instead of listing many results. Music playback is never routed to the iPhone.

## Goals

1. Control Spotify search and playback by voice through the official Spotify Web API.
2. Always target the existing headless Spotify Connect receiver on `lacasa-server` by its configured display name, not whichever phone happens to be active.
3. Keep Spotify and JARVIS running concurrently. Duck only the dedicated Spotify sink during actual JARVIS output and restore its exact prior gain/mute state afterward.
4. Provide four persistent, independently revocable QR/device credentials that survive JARVIS and host restarts.
5. Store OAuth tokens and device-verifier material only in private ignored files with mode `0600`; never store raw durable device secrets server-side.
6. Preserve the trusted Tailscale HTTPS origin and current half-duplex/STOP semantics.
7. Produce real acceptance evidence on `lacasa-server`, while distinguishing automated tests from the final physical iPhone listening check.

## Non-goals

- YouTube Music, stream extraction, `yt-dlp`, or browser automation.
- Telegram or a desktop GUI.
- Moving JARVIS to `lacasa15` or `lacasa17`.
- Home Assistant write control.
- Spotify playlist editing or library management in the first completion pass.
- Sharing Spotify passwords, OAuth tokens, QR payloads, or other credentials in Git, logs, documentation, or chat text.
- Replacing Raspotify/librespot or restarting it for ordinary commands or ducking.

## Architecture

### Spotify account authorization

Use Spotify Authorization Code with PKCE; no client secret is required. The operator creates one personal Spotify developer application and registers this redirect URI exactly:

`https://lacasa-server.tail7a9c2.ts.net/api/spotify/callback`

A private control-socket command starts authorization with a supplied Client ID. The running dashboard generates a random state and PKCE verifier, stores only the short-lived pending flow in memory, and returns the Spotify authorization URL to the operator. The callback validates and consumes state before exchanging the code. It atomically stores the Client ID, authorization timestamp, refresh token, access token, expiry and granted scopes in `config/spotify_oauth.json` with mode `0600`.

Tokens are refreshed under a process lock before expiry and once after HTTP 401. A refresh response that rotates the refresh token replaces it atomically; a response that omits it preserves the existing refresh token. Spotify refresh authorization expires six months after the original authorization and refreshing does not extend that deadline. `invalid_grant`, revocation or the six-month deadline atomically invalidates the stored access/refresh tokens and requires the user to run authorization again. Authorization errors are returned as concise tool results; tokens are never printed. Required scopes are limited to playback state and modification:

- `user-read-playback-state`
- `user-read-currently-playing`
- `user-modify-playback-state`

### Spotify control module

`core/spotify.py` owns OAuth storage, refresh, HTTP calls, device selection and normalized errors. `plugins/spotify_control.py` exposes one Gemini tool in headless mode.

Supported actions:

- `play` with `query` and `kind` (`track`, `album`, `artist`, default `track`)
- `pause`
- `resume`
- `next`
- `previous`
- `volume` with integer `volume_percent` from 0 through 100
- `current`

For `play`, perform exactly one catalog search with a small result limit, choose the first matching item of the requested type, resolve the target device by exact configured name, then play a track URI or context URI on that device. Other mutating actions target the same device. Device resolution fails safely if no exact match exists, multiple exact matches exist, the device ID is null, or the receiver is restricted. `volume` additionally rejects a receiver whose `supports_volume` flag is false.

Spotify's playback-state endpoint cannot target a device. Therefore `current` first resolves the exact LaCasa receiver, then fetches account playback state and reports the track only when the returned active device ID equals the resolved receiver ID. If another phone/speaker is active, it reports that nothing is currently playing on LaCasa. Do not preflight playback state for actions that do not require it. A missing receiver, non-Premium response, expired/revoked authorization, rate limit or network failure produces an honest German result suitable for JARVIS to speak.

The headless registry contains only the existing read-only Home Assistant tool and the new Spotify control tool. Spotify is intentionally allowed to mutate playback; no unrelated desktop or system tools become reachable.

### Audio and ducking

Raspotify remains a user service and keeps its current PulseAudio/PipeWire routing to `lacasa_spotify`. Spotify Web API volume changes affect the receiver’s application volume. JARVIS ducking continues to alter only the `lacasa_spotify` sink envelope, preserving the app volume and analog master.

Ducking must retain all existing guarantees:

- raw Pulse gain factor for −18 dB remains `10**(-18/60)`;
- overlapping speech cannot save an already-ducked baseline;
- normal release/heartbeat races are benign;
- genuine lost leases remain visible and generation-fenced;
- guard recovery restores after owner death;
- Spotify is not paused, restarted, sought or transferred away for a spoken announcement.

### Durable QR/device credentials

Store the device registry in `config/device_credentials.json`, mode `0600`, atomically written and ignored by Git. Each entry contains:

- random opaque device ID;
- operator label;
- creation timestamp;
- independent random salt;
- `hashlib.scrypt` verifier of a 256-bit random secret;
- optional revocation timestamp.

The raw secret exists only in the generated QR payload and the receiving phone’s local storage. The server never stores it. Constant-time comparison validates a login.

A QR contains a trusted HTTPS `/login` URL with the credential in the URL fragment, not the query string:

`https://lacasa-server.tail7a9c2.ts.net/login#device=<device-id>.<secret>`

Fragments are not sent in HTTP requests or access logs. Login JavaScript consumes the fragment, stores the credential in local storage, removes the fragment from browser history, and POSTs the ID and secret to `/api/device-login`. The server returns a short-lived in-memory bearer token and a fresh per-session encryption key. On later page loads or server restarts, Safari re-authenticates with the stored durable credential.

The private Unix control socket uses bounded line-delimited JSON commands to create, list and revoke devices and to start/status Spotify authorization. It preserves the existing `python -m core.headless pair` operator behavior for emergency pairing. Creation produces exactly one secret-bearing URL and a QR image in a private runtime/output directory; normal service logs contain only the device ID and label. Revocation terminates bearer sessions belonging to that device and closes its control/audio WebSockets without affecting other devices. Device creation/list/revocation has no HTTP endpoint reachable by a phone; the existing `/api/revoke-devices` route is removed or permanently denied.

The initial deployment creates four credentials labeled:

1. `LaCasa iPhone 1`
2. `LaCasa iPhone 2`
3. `LaCasa iPhone 3`
4. `LaCasa iPhone 4`

A later rename is optional and does not change security. A lost QR is handled by revoking only that device and creating a replacement.

The legacy ten-minute pairing command remains available for emergency setup. Creating a transient pairing key no longer revokes durable credentials or unrelated devices.

## Security boundaries

- Dashboard remains bound to `127.0.0.1:8000` and exposed only by Tailscale Serve at the trusted private HTTPS origin.
- Unauthenticated API routes remain denied except the state-protected Spotify OAuth callback and the credential-verification login route.
- OAuth state is random, one-use and short-lived; callbacks with missing, stale or wrong state fail without writing tokens.
- Durable QR secrets never appear in query strings, access logs, Git, reports or command output captured in service journals.
- `config/spotify_oauth.json`, `config/device_credentials.json` and the QR output directory are explicitly ignored by Git. Device/OAuth stores and QR files reject symlinks, unsafe ownership or unsafe permissions rather than silently widening access; unrecoverable store configuration is exit 78.
- Rate limiting applies to transient PIN and durable credential login attempts.
- Revocation checks are authoritative for new logins and immediately close live sessions for the revoked device.
- Bearer sessions have a one-hour absolute lifetime enforced for HTTP requests and both WebSocket types. A valid durable credential transparently exchanges for a fresh bearer; bearer expiry does not extend device-credential lifetime.
- HTTP 401 and WebSocket close code 4001 clear only `sessionStorage` bearer/session-key values and route through `/login`. The login page retries the durable credential. If durable login returns 401 because the device was revoked, it clears that durable local credential and shows manual login instead of looping.

## Failure behavior

- Spotify not authorized: JARVIS says Spotify must be connected once; conversation and HA continue.
- Receiver missing: JARVIS asks the user to open/select `raspotify (lacasa-server)` once; it does not reinstall or restart a healthy receiver.
- Spotify 401: refresh and retry once; persistent failure requests reauthorization.
- Spotify refresh `invalid_grant`, revocation or six-month authorization expiry: clear unusable tokens atomically and request a new consent flow; never loop refresh calls.
- Spotify 403 Premium: no retry; say Premium playback control is required.
- Spotify 429: one bounded retry after `Retry-After`, then report the rate limit.
- Spotify network error: fail the command without crashing or reconnecting Gemini.
- Invalid/revoked QR: show the secure login page and identify only the label/device ID if safe; never disclose verifier data.
- Corrupt/unsafe credential store: service configuration failure (exit 78), not a silent empty registry.

## Verification and acceptance

Automated acceptance:

1. TDD tests for Spotify search-to-play, exact/duplicate/null/restricted device selection, unsupported volume, LaCasa-only `current`, token refresh, rotated/omitted refresh-token fields, six-month/`invalid_grant` reauthorization, 401 retry, Premium/no-device/rate-limit errors and exact tool allowlisting.
2. TDD tests for exact PKCE S256 parameters and missing/stale/wrong/replayed OAuth state without token writes.
3. TDD tests for four-device persistence, restart login, one-hour bearer expiry, already-open stale tab recovery, no raw server secret, fragment-only QR, independent revocation, active-session closure, rate limiting and transient-pair non-rotation.
4. TDD tests that phone bearers cannot create, list or revoke devices, and that unsafe/symlinked stores or QR outputs fail closed without logging secrets.
5. Full Python suite, Python compilation, extracted dashboard JavaScript syntax, and Git secret/whitespace checks.
6. Server deployment at the exact Git commit; all user services active; HTTPS 200/TLS verify 0; unauthenticated status 401; control socket 0600.
7. Real Spotify Web API search and playback on `raspotify (lacasa-server)` with audible output path active.
8. Real spoken JARVIS response over active Spotify: music sink `65536 → 32846 → 65536` (or exact equivalent relative to the current baseline), unchanged master, unchanged Raspotify PID, continued track progress.
9. Service restart followed by login using each durable credential; exercise stale bearer recovery in an already-open tab; revoke one and verify only that credential fails while the other three still work.
10. Final user check on physical iPhone: scan, Safari microphone permission, play a requested song, hear JARVIS duck over it, continue speaking after the reply, use STOP.

The system is not called fully complete until OAuth authorization and the final physical iPhone/listening check are performed. Automated WebSocket/PCM tests are necessary but do not substitute for human hearing or Safari permission behavior.

## External prerequisite

Spotify requires the user to create/own a lightweight developer application, keep its owner on active Premium, register the exact redirect URI, add the intended Spotify account to any development-mode user allowlist Spotify requires, and approve the OAuth consent screen. Authorization must be repeated when Spotify's six-month refresh authorization expires. None of this can be fabricated or bypassed. When implementation reaches authorization, provide the user only the exact dashboard fields and authorization link; never request a Spotify password in chat.

## Reference

- Spotify Hermes setup and official Web API control pattern: https://hermes-agent.nousresearch.com/docs/user-guide/features/spotify
- Spotify Web API: https://developer.spotify.com/documentation/web-api
