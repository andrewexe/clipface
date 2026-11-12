# Clipper

A local-first desktop app that watches chosen Twitch and Kick streamers, finds highlight
moments, renders vertical clips, and publishes them after a safety gate. The full plan is in
[PLAN.md](PLAN.md); progress is in [CHANGELOG.md](CHANGELOG.md).

**Status: Phases 0 to 8.** The app follows Twitch and Kick streamers, records a
rolling buffer while they are live, detects highlight moments, renders vertical clips with
captions, and puts them in a review queue. Clips are scored by Claude, pass a
safety gate, and wait in a review queue. Approved clips are exported to a folder and, for
streamers who gave permission, posted to your YouTube channel (private by default).

## Setup on Windows

Install these once (PowerShell):

```powershell
winget install Rustlang.Rustup
```

```powershell
winget install OpenJS.NodeJS.LTS
```

```powershell
winget install astral-sh.uv
```

```powershell
npm install -g pnpm
```

Rust on Windows also needs the Microsoft C++ Build Tools: install "Visual Studio Build Tools"
and tick **Desktop development with C++**. WebView2 is already present on Windows 11.

Then, in the project folder:

```powershell
uv sync
```

```powershell
cd app; pnpm install
```

## Run

From `app/`:

```powershell
pnpm tauri dev
```

The first run compiles the Rust shell and takes a few minutes. The app starts the engine
itself; you do not run Python by hand.

Enter API keys under **Settings**. They go to Windows Credential Manager (look for entries
named `clipper`), never to a file. Do not put keys in `.env` files, code, or chat.

App data lives in `%LOCALAPPDATA%\Clipper`: `clipper.db` and `logs\engine.log`.

## Test

From the project folder:

```powershell
uv run pytest
```

```powershell
uv run ruff check .
```

From `app/`:

```powershell
pnpm typecheck
```

## Build an installer

From the project folder, build the engine:

```powershell
uv run pyinstaller engine.spec --noconfirm
```

Then from `app/`, build the app with the engine inside it:

```powershell
pnpm tauri build --bundles nsis --config src-tauri/tauri.bundle.conf.json
```

The installer is written to `app\src-tauri\target\release\bundle\nsis\`.

To smoke-test the built engine on its own:

```powershell
$env:CLIPPER_ENGINE_BIN = "$PWD\dist\clipper-engine\clipper-engine.exe"; uv run pytest tests/test_e2e_process.py
```

## How the pieces fit

- `app/` is the Tauri + React control panel. `app/src-tauri/src/lib.rs` starts the engine,
  gives the UI its port and session token, and stops it on exit.
- `engine/` is the Python engine. `python -m engine` binds `127.0.0.1` on a free port and
  prints `CLIPPER_ENGINE_READY {"port": ...}`. Every request needs
  `Authorization: Bearer <session token>`.
- `engine/keychain.py` is the only code that touches secrets. `engine/logging_setup.py`
  redacts them from logs.
- `engine/db/migrations/versions/` holds schema migrations; they run at every engine start.

## macOS and Linux

The same commands work. Linux needs the WebKitGTK development packages
(`libwebkit2gtk-4.1-dev`, `libgtk-3-dev`, `librsvg2-dev`) and a Secret Service keychain such
as GNOME Keyring; without one the Settings page says so and refuses to save keys.
