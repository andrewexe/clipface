# Clipper

A local-first desktop app that watches chosen Twitch and Kick streamers, finds highlight
moments, renders vertical clips, and publishes them after a safety gate. The plan it was
built from is in [PLAN.md](PLAN.md); what each phase added, and where it differs from the
plan, is in [CHANGELOG.md](CHANGELOG.md).

**Status: every phase of the plan is built and tested with fakes of the outside services.
It has not yet been run on Windows, or against the real Twitch, Kick, Deepgram, Anthropic,
YouTube, TikTok, Instagram or Telegram services** (only Twitch chat was exercised live).
Treat the first real session as the acceptance test.

## What it does

1. **Streamers.** Paste a Twitch or Kick channel URL. The app polls for live status.
2. **Recorder.** While a streamer is live, it keeps a rolling 20-minute buffer on disk.
3. **Detector.** Chat rate, hype messages and audio loudness are scored against their own
   last five minutes. A spike becomes a moment.
4. **Clip.** Each moment is cut (30 seconds before to 10 after), transcribed, scored by
   Claude, and rendered 1080x1920 with captions.
5. **Safety gate.** Blocked words, personal details, gambling promotion, music, and a
   Claude check of the frames. Clear violations are rejected; anything uncertain waits
   for you.
6. **Review.** Approve or reject in the Queue (keys `A` and `R`) or from your phone.
7. **Publish.** Approved clips are exported to a folder. For streamers whose permission
   is Granted or Clipping program, they are also posted to your connected accounts.

Nothing is posted without your approval unless you switch autonomy to L2, and never for
a streamer without recorded permission.

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

Rust on Windows also needs the Microsoft C++ Build Tools: install "Visual Studio Build
Tools" and tick **Desktop development with C++**. WebView2 is already present on
Windows 11. ffmpeg does not need installing: a copy ships with the engine.

Then, in the project folder:

```powershell
uv sync
```

```powershell
cd app; pnpm install
```

Check that the engine has everything it needs:

```powershell
uv run python -m engine --self-check
```

## Run

From `app/`:

```powershell
pnpm tauri dev
```

The first run compiles the Rust shell and takes a few minutes. The app starts the engine
itself. App data lives in `%LOCALAPPDATA%\Clipper`.

## Keys

Enter keys under **Settings**. They go to Windows Credential Manager, never to a file.
Do not put keys in `.env` files, code, or chat.

| Key | Needed for | Where to get it |
| --- | --- | --- |
| Twitch client ID and secret | Twitch live status and viewer clips | dev.twitch.tv, register an application |
| Kick client ID and secret | Kick live status | kick.com developer settings |
| Transcription (Deepgram) | Captions | console.deepgram.com |
| Anthropic | Scoring and the content safety check | console.anthropic.com; set a monthly limit |
| Google OAuth client ID and secret | YouTube posting | Google Cloud project with YouTube Data API, OAuth client of type "Desktop app" |
| TikTok client key and secret | TikTok drafts | developers.tiktok.com, app with Login Kit (redirect `http://127.0.0.1:*/callback/`) and Content Posting API |
| Instagram access token | Instagram Reels | Meta app with Facebook Login for Business and `instagram_content_publish` |
| Telegram bot token | Phone approvals | message @BotFather in Telegram |

Without a key, that part is skipped and the app says so: no Twitch keys means no Twitch
status, no Deepgram key means clips without captions, no Anthropic key means unscored
clips that all wait for you, no platform keys means export only.

## First session checklist

These are the plan's acceptance checks that could not be run where the app was built:

1. Save a key, restart the app, confirm it still shows "Saved".
2. Add a streamer who is live. They should show "Live" within a minute, then "Recording".
3. Let it run for an hour. Disk use should stay under the limit in Settings.
4. Watch the Queue. Compare the moments it finds with what happened on stream, and adjust
   the sensitivity threshold in Settings (lower finds more, higher finds fewer).
5. Check a clip: timing, captions, layout. Set the facecam box on the Streamers page.
6. Run the safety checks on real clips before trusting L2:

   ```powershell
   uv run python -m engine.policy.check path\to\clip.mp4
   ```

7. Connect YouTube, approve one clip for a streamer with permission, and confirm it
   uploads once and is private.

## Safety controls

- **Kill switch**: "Pause all publishing" at the bottom of the sidebar, or `/pause` from
  your phone.
- **Autonomy**: L1 (default) needs your approval for every clip. L2 lets clean,
  high-scoring clips of permitted streamers publish on their own. L3 is locked.
- **Stop**: on a streamer, when they ask you to stop. Pauses them, withdraws permission,
  drops their clips, cancels scheduled posts, and deletes their YouTube videos. TikTok
  and Instagram posts cannot be deleted by the app; it lists them for you.
- **Claims and strikes**: "Report a claim" on an account pauses it. A post that YouTube
  rejects for copyright, or that disappears, pauses the account automatically.
- **Spending**: daily limits for Anthropic and transcription, in Settings.

## Phone approvals

Add a Telegram bot token in Settings, then send the bot the `/start <code>` message the
app shows. Clips needing review arrive with Approve and Reject buttons. The bot obeys
that one chat only.

## Claude Desktop (MCP)

Turn it on under Settings, then add this to Claude Desktop's MCP configuration, with the
path to the installed engine:

```json
{
  "mcpServers": {
    "clipper": {
      "command": "C:\\path\\to\\Clipper\\engine\\clipper-engine.exe",
      "args": ["--mcp"]
    }
  }
}
```

From source, use `uv` as the command with the arguments `run python -m engine --mcp` and
the project folder as the working directory. Five tools: list clips, approve, reject,
pause, stats. Approving is off until you allow it in Settings.

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

The full suite takes about ten minutes (it renders real video). Useful tools:

- `uv run python -m engine.detector.replay chat.jsonl --z 3` replays the detector over a
  chat log recorded with `uv run python -m tests.record_chat --out chat.jsonl <channels>`.
- `uv run python -m tests.soak_recorder --minutes 60 --out result.json` is the one-hour
  recorder test.
- `uv run python -m tests.dev_engine` runs the engine with scripted fake platforms for UI
  work (see the file's header).

## Build an installer

From the project folder, build the engine:

```powershell
uv run pyinstaller engine.spec --noconfirm
```

```powershell
dist\clipper-engine\clipper-engine.exe --self-check
```

Then from `app/`, build the app with the engine inside it:

```powershell
pnpm tauri build --bundles nsis --config src-tauri/tauri.bundle.conf.json
```

The installer is written to `app\src-tauri\target\release\bundle\nsis\`.

## How the pieces fit

- `app/` is the Tauri + React control panel. `app/src-tauri/src/lib.rs` starts the engine,
  gives the UI its port and session token, and stops it on exit.
- `engine/` is the Python engine: `monitor` (live status), `recorder`, `signals` and
  `detector`, `scorer` (transcription and Claude), `render`, `policy` (the gate),
  `publish` (scheduler and platforms), `control` (spend, stop, phone bot), `mcp_server`,
  `api` (the loopback HTTP API), `db` (models and migrations).
- The engine listens on `127.0.0.1` only and every request needs the session token.
  Secrets live only in the OS keychain; `engine/logging_setup.py` scrubs them from logs.

## macOS and Linux

The same commands work. Linux needs the WebKitGTK development packages
(`libwebkit2gtk-4.1-dev`, `libgtk-3-dev`, `librsvg2-dev`), a Secret Service keychain such
as GNOME Keyring, and GStreamer's H.264 decoder for clip previews in the Queue.
