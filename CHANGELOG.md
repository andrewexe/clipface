# Changelog

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
