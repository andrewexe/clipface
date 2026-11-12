# Changelog

## 0.12.0 - Autonomy level L3 (2026-11-12)

The plan said when L3 unlocks but not what it does. This is the definition that was
built, chosen so that L3 adds autonomy without loosening any safety rule.

### Added

- **L3: nothing clean waits for you.** At L2, one in ten of the clips that would publish
  on their own is held for your approval first. At L3 those publish too, and appear in a
  new "Spot checks" tab in the Queue for you to look at afterwards.
- L3 changes when you look, not what may publish. The content checks, the streamer
  permission requirement, the score level, the daily caps, the posting window and the
  kill switch are identical at L2 and L3. Flagged clips still wait for you; blocked
  clips are still rejected.
- L3 can be switched on only after two weeks of L2 with no strikes, as the plan requires.
- L3 is lost automatically, back to L2 with the two weeks starting again, when an account
  gets a claim, strike or takedown, or when you answer a spot check with "Should not be
  up". That answer also cancels the clip's waiting posts and takes its posts down where
  the platform allows (YouTube); for TikTok and Instagram it tells you to remove it by
  hand.
- A spot check is announced on the paired phone.

## 0.11.0 - Phase 10: more platforms, insights, MCP (2026-11-12)

### Added

- TikTok: connected accounts receive each approved clip in their TikTok inbox as a draft,
  uploaded with the Content Posting API. You finish and post it in the TikTok app.
- Instagram: approved clips are published as Reels through the Graph API, uploaded from
  this computer with Meta's resumable upload.
- Both follow the same rules as YouTube: one post per clip per account, scheduled with
  gaps and caps, re-checked at posting time, and a retry never makes a second post. A
  platform that is still processing a video is waited on without counting as a failure.
- Insights on the Accounts page: views by Claude score, once posts have been up for a
  day. After 20 posts it may suggest a different auto-approve level or detection
  threshold, with the numbers behind it and a button to apply it. It never changes a
  setting by itself.
- MCP server for Claude Desktop (`clipper-engine --mcp`): five tools, to list clips,
  approve, reject, pause publishing and read stats. It uses its own token, which opens
  only those five routes. It returns no keys or file paths, cannot change settings,
  cannot resume publishing, and cannot bring back a rejected clip.
- `clipper-engine --self-check` verifies a build: ffmpeg and its filters, the Twitch and
  Kick stream plugins, migrations, the blocked-word list, the keychain, and the SDKs.
- The app's content security policy now allows the Queue's clip previews.

### Differs from PLAN.md

- TikTok is drafts, not automatic posts. TikTok's rules require the account owner to
  choose the privacy setting by hand for every post and to consent before upload, and
  limit unaudited apps to private posts; its audit is for apps with a wide audience.
  Sending drafts to the inbox is the route TikTok's docs offer that fits.
- Instagram needs a Facebook Login for Business token, pasted into Settings. Meta allows
  uploading a local file only with that kind of token; every other route needs the video
  on a public web server.
- Approving over MCP is off by default. Clip titles and transcripts come from streams,
  so text in them could try to talk an assistant into approving. You can switch it on.
- The Higgsfield plugin is a placeholder that reports "not connected". The plan says to
  confirm API access before building on it, and that could not be confirmed.
- TikTok and Instagram posts cannot be deleted by the app (their APIs do not allow it).
  A streamer's stop request lists them for you to remove by hand.

## 0.10.0 - Phase 9: Phone approvals (2026-11-12)

### Added

- Telegram bot (official Bot API, long polling). Each clip that needs your review is
  sent to your phone as a video with its title, score and safety findings, and Approve
  and Reject buttons. Approving schedules the post exactly as approving in the app does.
- `/pause` and `/resume` are the kill switch from your phone; `/status` shows the queue.
  Alerts (a strike, an expired sign-in) are forwarded.
- Pairing: the app shows a one-time code; sending `/start <code>` to the bot from a
  private chat pairs that phone. The bot obeys that one chat only. Messages and button
  presses from anyone else, or in a group, do nothing and get no reply.
- The bot can approve or reject a pending clip, pause, resume and report status. It
  cannot change settings, read keys, or reverse an earlier decision.
- Settings page: phone pairing, a test message, unpair, and a switch for clip messages.
- Autonomy level, the kill switch and phone pairing can no longer be changed through
  the general settings endpoint, only through their own audited controls.

### Differs from PLAN.md

- Telegram, not Discord: it needs only a bot token and outgoing HTTPS, with no gateway
  connection to keep alive.
- Under L1 every clip needs approval, so every clip is sent to the phone, not only
  flagged ones.

## 0.9.0 - Phase 8: YouTube publisher (2026-11-12)

### Added

- Connect a YouTube channel with Google's installed-app sign-in (loopback redirect and
  PKCE). The refresh token is stored in the OS keychain only. Disconnecting deletes it
  and revokes the grant.
- Approved clips are scheduled onto connected accounts: a random gap between posts
  (20 to 90 minutes), inside a daily time window (9:00 to 23:00), up to each account's
  daily cap (default 3). All are settings.
- Uploads use YouTube's resumable protocol. The upload session is saved before any bytes
  are sent, so a retry after a dropped connection, a crash or a restart continues the
  same upload. If the session itself is gone, the channel's recent uploads are checked
  for the clip before uploading again.
- One post per clip per account, enforced by a unique idempotency key.
- Everything is checked again at the moment of posting: kill switch, account, streamer
  permission, clip status, safety verdict, and the daily cap.
- Clips are posted only for streamers whose permission is Granted or Clipping program.
  For anyone else, approving a clip exports the file and posts nothing.
- A copyright claim, strike or takedown pauses the account and raises an alert. You can
  report one from the Accounts page; a post that YouTube rejects for copyright, or that
  disappears from the channel, is treated as one automatically (checked every 30 minutes).
- "Stop" on a streamer now deletes their videos from YouTube.
- Accounts page: connect, daily cap, pause and resume, report a claim, disconnect, upload
  privacy, and the list of scheduled and finished posts with cancel and retry.
- Views, likes and comments of recent posts are recorded in a new `metrics` table.
- HTTP libraries no longer log request URLs, which can carry tokens or sign-in codes.

### Differs from PLAN.md

- Uploads default to Private, as the plan asks, but this is now a setting. Google's
  current docs say uploads from unaudited API projects are no longer forced to private,
  and the upload quota is 100 videos a day. Switch to Unlisted or Public on the Accounts
  page when you choose to.
- The sign-in asks for the `youtube.force-ssl` scope as well as `youtube.upload`. It is
  needed to delete a video when a streamer asks you to stop, and to read post status.

## 0.8.0 - Phase 7: Policy gate and controls (2026-11-12)

### Added

- Policy gate: every rendered clip is checked before it can be published.
  - Local, always on: blocked words (a built-in list stored as hashes, plus your own),
    personal details in speech (phone, card and ID numbers, emails, street addresses),
    gambling promotion, and a streamer's program-banned topics.
  - Music: the clip's audio is checked for held notes and a steady beat. Music sends the
    clip to you, or rejects it if you choose.
  - Content check: Claude looks at five frames of the finished clip plus the transcript
    for nudity or sexual content, slurs or hate, a possible minor on screen, personal
    information, private messages on screen, gambling promotion, and abusive donation or
    text-to-speech messages. "Yes" rejects the clip; "unsure" sends it to you.
  - Fail closed: if the content check cannot run (no key, budget spent, API down,
    declined or unreadable reply) or the gate itself crashes, the clip is held for you.
- Autonomy levels: L1 (default, every clip needs approval) and L2 (a clean clip publishes
  on its own only if it scored at least 0.8, the streamer's permission is Granted or
  Clipping program, the streamer is active and publishing is not paused). One in ten of
  those still comes to you at random.
- Kill switch in the sidebar on every page: pauses all automatic approval and publishing.
- "Stop" on a streamer, for when they ask you to: pauses them, withdraws permission,
  drops their queued clips, cancels scheduled posts, and takes down published ones.
  Posts it could not take down are listed so you can remove them by hand.
- Clipping-program rules per streamer (required hashtags, credit format, banned topics),
  applied to every description. The credit always names the streamer and links their
  channel, whatever the format says.
- The Queue shows each clip's findings and the exact description it will post with.
- `python -m engine.policy.check clip.mp4` runs the checks on any file with your key;
  `tests/make_policy_samples.py` makes sample clips to try it on.

### Differs from PLAN.md

- Music detection finds that music is playing, not whose music it is. Naming a
  copyrighted track needs a paid fingerprint service; the detector is built so one can be
  added. It was tuned on generated music and synthesized speech only, so expect to adjust
  `music_threshold` once you see it on real clips.
- Clips with music are held or rejected. Muting them is not built.
- L3 is locked and not built. The plan says when it unlocks (two weeks of L2 with no
  strikes), which the app tracks and shows, but not what L3 would change.

## 0.7.0 - Phase 6: Claude scoring (2026-11-11)

### Added

- Each moment's transcript and signal scores are sent to Claude (Anthropic Python SDK,
  structured output) before rendering. The reply gives a score from 0 to 1, the cut
  inside the window, a title, a caption and short reasons.
- Moments scored below the minimum (default 0.4, a setting) are dropped before
  rendering. The score and reasons are shown on each clip in the queue.
- The transcript, stream title and game are sent as marked untrusted data, and the reply
  is range-checked and cleaned (single line, no links, length limits) before use. Claude
  returns data only; nothing in a reply can trigger an action.
- An unusable reply (out-of-range score, cut outside the window, cut off, declined) is
  retried once. If it is still unusable, or there is no key, the API is down, or the
  daily budget is spent, the clip goes to the review queue unscored with the reason.
- Daily Anthropic spend cap (default $5, a setting), counted from each reply's token
  usage. Settings page: clip window, minimum score, and both daily spending limits.

### Differs from PLAN.md

- A reply that stays malformed does not discard the moment. The reply is rejected and
  the clip is reviewed by a person, unscored. An unscored clip can never be
  auto-approved.
- The model is `claude-opus-5-5` (setting `scoring_model` in `settings.json`).

## 0.6.0 - Phase 5: Review queue (MVP complete) (2026-11-11)

### Added

- Queue page: clips to review on the left, a looping preview on the right, with title
  (100 characters) and caption editing, the streamer credit, what triggered the moment,
  and the transcript. `A` approves, `R` rejects, arrow keys move through the list.
- Approving a clip copies it to the export folder as "<streamer> - <title> [<id>].mp4"
  with a text file holding the title, caption and the credit line with the channel link.
  The folder defaults to `Videos\Clipper` and is a setting. If the folder can't be
  written, the clip is still approved and the queue says why it was not exported.
- Approved and Rejected tabs, with "Back to review" to undo and "Export again".
- Facecam editor on the Streamers page: drag a box over a recent frame of the stream.
  "Re-render" on a clip applies the new layout without paying for transcription again.
- A reminder on each clip when no permission is recorded for its streamer.

### Changed

- Two clips now count as the same footage only when both the pictures and the sound
  match. Pictures alone would have rejected every later moment from a streamer sitting
  in front of a fixed camera.

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
