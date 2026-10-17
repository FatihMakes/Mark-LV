# Cannanas deployment on lacasa-server (JARVIS child)

The JARVIS-side MCP client keeps **one persistent serialized STDIO child** of the
pinned Cannanas MCP adapter release. No listening ports, no Tailscale endpoint,
no new sockets: the child inherits the JARVIS service's private stdio.

## Artifacts (release 1.0.0)

- Pinned wheel: `cannanas_mcp_adapter-1.0.0-py3-none-any.whl`
- Pinned wheel SHA-256: `8349fbd2fc6a3761334e035c06077e4e2f3eff3ee7da2b7bf001de7c709e113c`
- The release manifest and hashed constraints lock ship with the release bundle
  (see the adapter package's `release/` directory).

## Placement

- Dedicated venv: `%h/cannanas-adapter/venv` (`/home/lacasa/cannanas-adapter/venv`; service-user owned, no sudo needed)
- Entry point (absolute path, run by the JARVIS process):
  `cannanas-adapter/venv/bin/cannanas-mcp-adapter`
- The `release_info.json` sidecar sits next to the entry point in
  `cannanas-adapter/venv/bin/release_info.json` (mode 0600); JARVIS refuses writes against
  an unpinned artifact.

## Install

```sh
deploy/cannanas/install-adapter.sh --verify <wheel>
deploy/cannanas/install-adapter.sh --install <wheel> \
    %h/cannanas-adapter/venv <constraints-lock.txt> <release/release_info.json>
# (run as user lacasa; %h = /home/lacasa)
```

`--install` never starts anything (the JARVIS unit starts the child).

## Credentials

Copy `cannanas-adapter.credentials.example` to
`deploy/cannanas/cannanas-adapter.credentials`, fill in a **new least-privilege
JARVIS key** (never the Hermes key) and the fixed club id, then:

```sh
chmod 0600 deploy/cannanas/cannanas-adapter.credentials
deploy/cannanas/install-adapter.sh --check-env deploy/cannanas/cannanas-adapter.credentials
```

The file holds exactly the four adapter variables (`CANNANAS_API_KEY`,
`CANNANAS_CLUB_ID`, `CANNANAS_STATE_DIR`, `CANNANAS_AUDIT_FILE`). The real file
is gitignored. `CANNANAS_API_URL` must never be set: the fixed origin is
baked into the pinned adapter and re-supplied overrides are refused.

## Service

`deploy/jarvis.service` gains one line: the `EnvironmentFile` above. The unit
description notes the pinned adapter child. Nothing else changed: no new
listening sockets, same `ExecStart`, same restart policy. Missing or invalid
Cannanas configuration degrades Cannanas only (one bounded German notice) —
JARVIS, Spotify, Home Assistant and phone authentication keep running.