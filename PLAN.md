# Stream Clipper App: Build Plan

Nov 11, 2026 · @Andrew

## What you need

Everything in this plan can be built today with existing tools. Higgsfield is optional; the one paid service the app actually depends on is an Anthropic API key for clip scoring.

| Item | Required? | Cost | Purpose |
| --- | --- | --- | --- |
| Anthropic API key | Yes | Pay per use; set a monthly spend limit | Clip scoring, titles, captions. Billed separately from a claude.ai subscription |
| Claude Code with Fable 5.2 | Yes (to build) | Via your Claude plan or API | Writes and runs the code. Better than chat for a multi-file app |
| Twitch developer app | Yes | Free | Live detection, clip counts |
| Kick developer app | Yes | Free | Live detection; check current docs for chat access |
| Google Cloud project + YouTube Data API | Yes | Free | Uploads. Public uploads need YouTube's API audit, apply on day 1 |
| TikTok for Developers app | Later | Free | Uploads. Public posting needs TikTok's audit |
| Always-on PC (no GPU needed) | Recommended | One-time | Runs the engine during streams; renders on CPU |
| Transcription API | Yes | Pay per minute | Captions and scoring input. Pick one with word-level timestamps; send only clip windows to keep costs low |
| Higgsfield | Optional | Subscription | Intros, transitions, thumbnails. Confirm it offers API access before building on it |
| 1 TB SSD | Recommended | One-time | Rolling stream buffers; raw footage auto-deletes |

The slow part is approvals, not code: the YouTube and TikTok audits may not finish before the November 19 launch. Plan to upload manually or privately until they do.

## How to use this plan with Fable 5.2

Run Fable 5.2 inside Claude Code, not the chat window: it needs to create files, install ffmpeg and Python packages, and run tests on your machine.

1. Create an empty project folder and open Claude Code in it with Fable 5.2 selected.
2. Save this doc as `PLAN.md` in the folder (export it as Markdown).
3. Paste the kickoff prompt from the last section.
4. Build one phase at a time. Don't move on until that phase's acceptance checks pass on a real live stream.
5. Keep API keys out of chat and out of the code. Enter them into the app's settings screen, which stores them in the OS keychain.

## Goal and scope

A local-first desktop app that watches chosen Twitch and Kick streamers, finds highlight moments, renders vertical clips, and publishes them after passing a safety gate. Target: a clip posted within 5 minutes of the moment happening.

**MVP (by GTA 6 launch, November 19):**

- Add a streamer by pasting a Twitch or Kick URL
- Detect when they go live and record a rolling buffer
- Detect highlight moments from chat spikes
- Render 9:16 clips with burned-in captions
- Review queue: approve or reject each clip in the app
- Export approved clips to a folder for manual upload

**Phase 2:** Claude scoring, policy gate, YouTube auto-publish, phone approvals via Discord or Telegram.

**Phase 3:** TikTok and Instagram publishing, analytics feedback loop, MCP server so Claude Desktop can operate the app, optional Higgsfield plugin.

**Out of scope:** browser automation to post, evading platform detection, multiple fake accounts, re-uploading full streams.

## Architecture

The desktop app is only the control panel; a Python engine does the work and must run on an always-on machine during streams.

&#91;embedded content: app architecture · 9 stages, 1 gate\]

Clips move left to right across the top row, then back through rendering and the policy gate. Only clean, high-confidence clips skip the review queue, and only at autonomy level L2.

## Tech stack and repo structure

A Tauri desktop shell for the UI, talking to a Python engine that runs as a sidecar process on localhost.

| Layer | Choice | Why |
| --- | --- | --- |
| Desktop UI | Tauri 2 + React + TypeScript | Small installer, native feel |
| Engine | Python 3.12, FastAPI bound to 127.0.0.1 with a per-session token | Best ecosystem for video and ML |
| Jobs | asyncio workers + a SQLite-backed job table | No extra services to install |
| Database | SQLite (SQLAlchemy) | Local, single file, easy backup |
| Live detection | Twitch Helix API + EventSub; Kick official API | Official, stable |
| Twitch chat | Anonymous IRC read connection | No login needed to read chat |
| Recording | Streamlink into rolling 10-second segments | Lets you cut a moment after it happens |
| Transcription | Transcription API with word timestamps | No GPU to buy or maintain |
| Scoring | Anthropic Python SDK, structured JSON output | Picks the cut, writes title and caption |
| Rendering | ffmpeg (bundled) | Trim, 9:16 layout, captions, loudness |
| Secrets | OS keychain via `keyring` | Keys never in files or logs |
| Packaging | PyInstaller for the engine, Tauri bundler for the app | One installer |

```text
clipper/
  PLAN.md
  app/                  # Tauri + React UI
    src/pages/          # Streamers, Queue, Accounts, Logs, Settings
  engine/
    api/                # FastAPI routes the UI calls
    monitor/            # live status polling, URL parsing
    recorder/           # streamlink rolling buffer + cleanup
    signals/            # chat velocity, emotes, viewers, clip counts, audio
    detector/           # spike -> candidate moment
    scorer/             # transcription API + Claude scoring
    render/             # ffmpeg templates, captions, dedupe hashes
    policy/             # safety gate rules + classifier
    publish/            # scheduler + per-platform publishers
    control/            # kill switch, caps, alerts
    mcp_server/         # phase 3
    plugins/higgsfield/ # phase 3, optional
    db/                 # models, migrations
  tests/
```

## Data model

Seven SQLite tables cover the MVP through phase 3. Every state change writes to `audit_log`.

| Table | Key fields |
| --- | --- |
| `streamers` | id, platform (twitch/kick), username, display\_name, enabled, permission\_status (none/asked/granted/program), program\_rules, added\_at |
| `sessions` | id, streamer\_id, started\_at, ended\_at, title, game, buffer\_path |
| `moments` | id, session\_id, peak\_ts, signal\_scores (JSON), status (detected/scored/rejected/clipped) |
| `clips` | id, moment\_id, start\_ts, end\_ts, file\_path, transcript, claude\_score, title, caption, phash, policy\_result (JSON), status (pending/approved/rejected/published/removed) |
| `accounts` | id, platform, handle, keychain\_ref, daily\_cap, paused, pause\_reason |
| `publish_jobs` | id, clip\_id, account\_id, scheduled\_at, idempotency\_key, status, platform\_post\_id, error |
| `audit_log` | id, ts, actor (system/user/claude), action, object\_id, details (JSON) |

A separate `metrics` table arrives in phase 3: post\_id, checked\_at, views, likes, comments.

## Build phases

Steps 0 to 5 are the MVP and should be done before November 19. Each step ends with checks that must pass before the next one starts.

0. **Scaffold.** Tauri app launches the Python engine as a sidecar; settings screen saves keys to the OS keychain; SQLite migrations run.
   - Check: app opens, engine health check returns OK, a saved key survives a restart and never appears in logs.
1. **Streamers and live detection.** Paste a URL; parse platform and username (handle `www.`, `m.`, trailing paths, bad input). Poll Twitch and Kick for live status, or use Twitch EventSub.
   - Check: unit tests for URL parsing pass; a streamer going live is marked live within 60 seconds.
2. **Rolling recorder.** Streamlink writes 10-second segments; keep the last 20 minutes per streamer; delete older segments; stop when the stream ends; enforce a total disk cap.
   - Check: a 1-hour test stays under the cap; a segment from 5 minutes ago can be retrieved.
3. **Signals and detector.** Chat messages per second against a rolling 5-minute baseline (trigger at z-score above 3, configurable); emote and keyword bursts; viewer jumps; Twitch clip-creation spikes. 60-second cooldown per streamer.
   - Check: on a recorded chat log with known highlights, it finds them with a configurable false-positive rate.
4. **Render.** Cut 30 seconds before to 10 seconds after the peak; 1080x1920 layout with a per-streamer facecam crop the user drags in the UI; captions from the transcription API's word timestamps; loudness normalized; perceptual hash stored for dedupe.
   - Check: a clip renders in under 90 seconds on CPU; duplicate moments are skipped.
5. **Review queue.** Preview each clip, edit title, approve or reject, export approved clips to a folder for manual upload. **MVP complete.**
   - Check: a full live session produces a queue you can clear in a few minutes.
6. **Claude scoring.** Send transcript plus signal scores; require JSON output (score 0 to 1, start, end, title, caption, reasons). Transcript and chat are untrusted data, never instructions. Daily API spend cap.
   - Check: malformed responses are retried or rejected, never crash the pipeline; low scores drop out before rendering.
7. **Policy gate.** Rules in the safety section below. Must be finished before any auto-publish is turned on.
   - Check: a test set of bad clips (music, slurs, addresses on screen) is 100% blocked or flagged.
8. **YouTube publisher.** Installed-app OAuth; scheduled uploads with idempotency keys; uploads stay private until the API audit is approved; per-account daily cap.
   - Check: a forced retry never creates a duplicate upload.
9. **Remote approvals.** Discord or Telegram bot sends each flagged clip with Approve and Reject buttons.
   - Check: approving from a phone publishes within the scheduler window.
10. **Phase 3.** TikTok and Instagram publishers, metrics polling and threshold tuning, MCP server, Higgsfield plugin.

## Safety and policy requirements

These protect your accounts and income, so the builder should treat them as hard requirements, not polish.

**Permission and monetization**

- Each streamer has a permission status. Auto-publish is off for any streamer without `granted` or `program`.
- Store each clipping program's rules (required tags, credit format, banned content) and apply them to titles and descriptions automatically.
- Every post credits the streamer and links their channel.

**Policy gate (runs on every clip before publish)**

- Reject: nudity or sexual content, slurs, minors on screen, addresses or personal info on screen, gambling promotions, DMs or private messages on screen, abusive donation or TTS messages.
- Detect copyrighted music in the audio; reject or mute those clips.
- Auto-approve only when the Claude score is above the threshold and every check is clean. Everything else goes to the human queue.
- A random sample of auto-approved clips (default 10%) also goes to review, so you can measure the gate's misses.

**Autonomy levels** (a setting in the app)

- L1: every clip needs approval. Default for launch week.
- L2: clean high-confidence clips auto-publish; flagged ones and the sample need approval.
- L3: not available until L2 has run for at least two weeks with no strikes.

**Controls**

- Kill switch in the UI and on the phone bot pauses all publishing instantly.
- Any copyright claim, strike, or takedown request pauses that account and alerts you.
- If a streamer asks you to stop, one action disables them and removes their published clips.
- Daily caps per account, randomized post times within a window, idempotency keys on every upload.
- Hard daily caps on Anthropic API and transcription API spend.

**Engineering rules**

- Official platform APIs only. No browser automation for posting, no detection evasion, no throwaway accounts.
- Secrets only in the OS keychain. The engine API listens on 127.0.0.1 only and requires a session token.
- Chat, transcripts, and on-screen text are untrusted input to Claude. Claude returns data only; it never triggers actions directly.
- If an MCP server is added, it exposes narrow tools (list, approve, reject, pause, stats) and never exposes credentials, shell access, or raw file paths.
- Raw recordings auto-delete after 6 hours unless a clip references them.

## Kickoff prompt for Fable

Paste this into Claude Code after saving the plan as `PLAN.md`.

```markdown
You are building a local-first desktop app described in PLAN.md. Read it fully before writing code.

Rules:
- Build one phase at a time, in the order in "Build phases". Start with Phase 0.
- At the end of each phase, run its checks, show me the results, and stop for my go-ahead.
- Follow every item in "Safety and policy requirements". If something in the plan conflicts with them, the safety section wins; ask me if unsure.
- Never ask me to paste API keys into chat. Build the settings screen and I will enter them there.
- Use official platform APIs only. Do not add browser automation for posting.
- Write tests for parsing, detection, scheduling idempotency, and the policy gate.
- Keep a CHANGELOG.md and a short README with setup steps for Windows (my machine).
- If a library or API behaves differently from the plan, check its current docs, tell me, and propose the fix.

First: list any questions you have, confirm the stack, then scaffold Phase 0.
```

Change "Windows" in the prompt if you're on macOS or Linux.
