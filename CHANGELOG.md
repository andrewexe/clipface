# Changelog

## 0.2.0 - Phase 1: Streamers and live detection (2026-11-11)

### Added

- Paste a Twitch or Kick channel URL to follow a streamer. The parser handles `www.`, `m.`,
  missing schemes, trailing paths, queries, popout and player links, and rejects lookalike
  hosts, clip and video links, and bare names with a message that says what to paste.
- Live monitor: polls Twitch (`helix/streams`) and Kick (`public/v1/channels`) every 20
  seconds with app access tokens. Going live opens a row in `sessions`; going offline closes
  it after two misses in a row, so one empty API response does not end a session.
- Streamers page: add, pause, remove, set permission status, live state with title, game,
  and viewer count, and a banner when a platform's keys are missing or rejected.
- Channels that do not exist are flagged "Channel not found" instead of looking offline.

### Differs from PLAN.md

- Polling only, no Twitch EventSub. Twitch's docs say EventSub over WebSockets needs a user
  access token (a Twitch login), and the webhook transport needs a public HTTPS endpoint,
  which a desktop app does not have.
- "Live within 60 seconds" holds from the moment the platform's API reports the stream.
  Any delay inside Twitch's or Kick's API is added on top and is outside the app's control.

## 0.1.0 - Phase 0: Scaffold (2026-11-11)

### Added

- Tauri 2 + React + TypeScript desktop shell with Streamers, Queue, Accounts, Logs, and
  Settings pages. Settings and Logs work; the other three are placeholders until their phases.
- Python 3.12 engine (FastAPI) started by the app as a child process. It listens on
  127.0.0.1 only, on a free port it picks itself, and requires a session token that the app
  generates fresh on every launch.
- Settings screen saves API keys to the OS keychain through `keyring`. Keys are write-only:
  the app reports whether each is set and can replace or remove it, never display it.
- Log redaction: every secret the engine has seen is scrubbed from log lines and tracebacks.
- SQLite database with Alembic migrations that run on every engine start. The first
  migration creates all seven tables from the plan's data model.
- `audit_log` entries for key saves and removals, shown on the Logs page.
- Engine shuts down when the app closes or crashes (it watches its stdin pipe).
- PyInstaller build of the engine (`engine.spec`), bundled into the installer as a resource.
- Tests: API auth, secret handling, redaction, migrations, and a real engine-process restart.

### Differs from PLAN.md

- The engine ships as a folder inside the app's resources rather than as a single-file Tauri
  "sidecar" binary. Single-file PyInstaller builds unpack on every start and run as two
  processes, which makes clean shutdown unreliable on Windows.
- Choices the plan left open: `publish_jobs.status` is one of scheduled / running /
  succeeded / failed / cancelled; `accounts.platform` is one of youtube / tiktok / instagram;
  `accounts.daily_cap` defaults to 3.
