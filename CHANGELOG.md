# Changelog

## 0.5.0 - Phase 4: Render (2026-11-11)

### Added

- Clip pipeline: each detected moment is cut from 30 seconds before to 10 seconds after
  its peak (both settings), checked for duplicates, transcribed, and rendered to a
  1080x1920 H.264/AAC MP4 with loudness normalised to -14 LUFS.
- Two layouts: the full frame over a blurred copy of itself (default), or, once a
  streamer's facecam rectangle is saved, facecam on top and gameplay below.
- Captions burned in from word timestamps, three words at a time. Transcript text is
  treated as untrusted and cannot inject subtitle commands.
- Transcription through Deepgram's pre-recorded API. Only the clip's audio is sent. A
  daily spend cap (default $2) is checked before each call and recorded after it.
  Without a key, or over the cap, clips still render, without captions, and say why.
- Duplicates are skipped before any paid call or render: a moment whose window mostly
  overlaps an existing clip, or whose frames look the same as a clip from the last 24
  hours (64-bit difference hashes of one frame every 4 seconds).
- Raw footage of a moment is deleted 6 hours later unless a pending or approved clip was
  cut from it.
- Database migration 0002: `streamers.layout`, `clips.meta`, wider `clips.phash`, and a
  `spend` table.

### Differs from PLAN.md

- Deepgram is the transcription provider; the plan left it open. The interface is small,
  so another provider with word timestamps can be swapped in.
- An eighth table, `spend`, backs the daily spend caps.

## 0.4.0 - Phase 3: Signals and detector (2026-11-11)

### Added

- Twitch chat reader over the official IRC WebSocket endpoint, read-only and without an
  account. It keeps only channel, time and text; user names are never read or stored.
- Signals per live session: chat messages per second, hype messages per second (emotes,
  keywords, shouting, "?????"), audio loudness of the recorded segments, and as context
  the viewer count and the number of viewer-made Twitch clips.
- Detector: each signal's last 10 seconds are scored against its previous 5 minutes.
  A score above the threshold (default z = 3, a setting) for at least 3 seconds starts a
  burst; the strongest second of the burst becomes the moment. One moment per streamer
  per 60 seconds; a second signal firing inside that window confirms the same moment.
- Moments are saved to `moments` with their scores and written to the audit log.
- `python -m engine.detector.replay` runs the detector over a recorded chat log to tune
  the threshold; `tests/record_chat.py` records live chat activity for it (no text, no names).
- Settings page: detection section (threshold, cooldown, audio threshold, viewer clips).

### Differs from PLAN.md

- The anonymous chat login the plan relies on works (tested against live Twitch chat) but
  Twitch's docs describe only token logins. If Twitch ever closes it, chat signals stop
  and detection continues on audio; the fix would be a "Connect Twitch" login.
- Kick has no chat signal. Kick's public API has no endpoint to read chat, and its chat
  webhook needs a public HTTPS address. Kick moments come from audio loudness spikes.
- The score is a robust z-score: median and median absolute deviation instead of mean and
  standard deviation, and message counts are compared on a square-root scale. A plain
  z-score fired about three times as often on bursty chat and stayed blind for minutes
  after each highlight.
- "Viewer jumps" are recorded with each moment but do not trigger one on their own: the
  viewer count only updates every 20 seconds and mostly jumps on raids.

## 0.3.0 - Phase 2: Rolling recorder (2026-11-11)

### Added

- Recorder: while a streamer is live, Streamlink reads the stream and ffmpeg cuts it into
  10-second segments without re-encoding. One worker per live session; it stops when the
  session ends and restarts with a growing pause if the stream drops.
- Rolling buffer: the last 20 minutes per streamer are kept, older segments are deleted,
  buffers of ended streams are removed after 6 hours, and a total disk limit deletes the
  oldest footage first. All four values are settings.
- Footage for a time window can be copied out of the buffer so pruning cannot remove it
  (used by the renderer in Phase 4).
- Settings page: recording section (buffer length, disk limit, raw footage retention).
  Non-secret settings live in `settings.json` in the data folder; changes are audited.
- Streamers page shows whether each live stream is being recorded and how much is kept.
- ffmpeg is found on PATH or falls back to the copy shipped with the app.

### Differs from PLAN.md

- Segment timing comes from ffmpeg's own segment list, mapped to clock time, rather than
  from file timestamps. Streams arrive in bursts, so file times alone misplace segments.
- Embedded ad breaks are skipped while recording (Streamlink's ad filter) so ads cannot end
  up inside a clip.

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
