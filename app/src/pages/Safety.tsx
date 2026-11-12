import { useCallback, useEffect, useState } from "react";
import {
  api,
  type AppSettings,
  type Control,
  type McpStatus,
  type TelegramStatus,
} from "../api";

/** Autonomy level, music handling and your own blocked words. */
export function SafetySettings() {
  const [control, setControl] = useState<Control | null>(null);
  const [settings, setSettings] = useState<AppSettings | null>(null);
  const [words, setWords] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  const load = useCallback(async () => {
    try {
      const [c, s] = await Promise.all([api.control(), api.appSettings()]);
      setControl(c);
      setSettings(s);
      setWords(s.blocked_words.join("\n"));
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  if (!control || !settings) {
    return error ? (
      <div className="banner banner-bad" role="alert">
        Could not load safety settings: {error}
      </div>
    ) : null;
  }

  const run = async (action: () => Promise<unknown>) => {
    setError(null);
    setSaved(false);
    try {
      await action();
      await load();
      setSaved(true);
    } catch (err) {
      setError((err as Error).message);
    }
  };

  const wordList = words
    .split(/[\n,]/)
    .map((w) => w.trim())
    .filter(Boolean);
  const wordsChanged = wordList.join("\n") !== settings.blocked_words.join("\n");

  return (
    <div className="card">
      <h3>How much runs without you</h3>
      <div className="choice-row" role="radiogroup" aria-label="Autonomy level">
        <button
          role="radio"
          aria-checked={control.autonomy_level === "L1"}
          className={control.autonomy_level === "L1" ? "btn choice active" : "btn choice"}
          onClick={() => void run(() => api.setAutonomy("L1"))}
        >
          <strong>L1: you approve every clip</strong>
          <span className="muted small">Nothing is published without you. The default.</span>
        </button>
        <button
          role="radio"
          aria-checked={control.autonomy_level === "L2"}
          className={control.autonomy_level === "L2" ? "btn choice active" : "btn choice"}
          onClick={() => void run(() => api.setAutonomy("L2"))}
        >
          <strong>L2: clean, high-scoring clips publish on their own</strong>
          <span className="muted small">
            Only for streamers with permission. Flagged clips and a random sample still come to
            you.
          </span>
        </button>
        <button role="radio" aria-checked={false} className="btn choice" disabled>
          <strong>L3: locked</strong>
          <span className="muted small">{control.l3.reason}</span>
        </button>
      </div>

      <div className="field">
        <label htmlFor="music-action">When music is detected</label>
        <select
          id="music-action"
          value={settings.music_action}
          onChange={(event) =>
            void run(() =>
              api.updateAppSettings({ music_action: event.target.value as "review" | "reject" }),
            )
          }
        >
          <option value="review">Send the clip to me to decide</option>
          <option value="reject">Reject the clip</option>
        </select>
        <span className="muted small">
          This detects that music is playing, not whose music it is. Game soundtracks and in-game
          radio are a common source of copyright claims.
        </span>
      </div>

      <div className="field">
        <label htmlFor="blocked-words">Your own blocked words</label>
        <textarea
          id="blocked-words"
          rows={3}
          value={words}
          placeholder="One per line. A clip where any of these is said is rejected."
          onChange={(event) => {
            setSaved(false);
            setWords(event.target.value);
          }}
        />
        <span className="muted small">
          Added to a built-in list of slurs, which is always on and not shown.
        </span>
      </div>
      <div className="form-foot">
        <button
          className="btn btn-primary"
          disabled={!wordsChanged}
          onClick={() => void run(() => api.updateAppSettings({ blocked_words: wordList }))}
        >
          Save words
        </button>
        {saved && <span className="muted small">Saved.</span>}
        {error && (
          <span className="error small" role="alert">
            {error}
          </span>
        )}
      </div>
    </div>
  );
}

/** The kill switch, shown in the sidebar on every page. */
export function KillSwitch() {
  const [control, setControl] = useState<Control | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      setControl(await api.control());
    } catch {
      // The engine badge below already reports an engine that is down.
    }
  }, []);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), 5000);
    return () => window.clearInterval(timer);
  }, [load]);

  if (!control) return null;
  const paused = control.publishing_paused;
  return (
    <button
      className={paused ? "btn kill on" : "btn kill"}
      disabled={busy}
      title="Stops every automatic approval and post at once"
      onClick={async () => {
        setBusy(true);
        try {
          setControl(await api.setPaused(!paused));
        } finally {
          setBusy(false);
        }
      }}
    >
      {paused ? "Publishing paused. Resume" : "Pause all publishing"}
    </button>
  );
}

/** Pairing a phone through Telegram, for approvals and the kill switch on the go. */
export function PhoneApprovals() {
  const [status, setStatus] = useState<TelegramStatus | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setStatus(await api.telegram());
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    }
  }, []);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), 4000);
    return () => window.clearInterval(timer);
  }, [load]);

  if (!status) {
    return error ? (
      <div className="banner banner-bad" role="alert">
        {error}
      </div>
    ) : null;
  }

  const act = async (action: () => Promise<unknown>, done: string) => {
    setNote(null);
    setError(null);
    try {
      await action();
      await load();
      setNote(done);
    } catch (err) {
      setError((err as Error).message);
    }
  };

  const bot = status.bot_username ? `@${status.bot_username}` : "your bot";
  return (
    <div className="card">
      <h3>Approve from your phone</h3>
      {(status.state === "no_token" || status.state === "off") && (
        <p className="muted">
          Create a bot by messaging @BotFather in Telegram, then paste its token under API keys
          below, in the "Phone approvals" group.
        </p>
      )}
      {status.state === "bad_token" && (
        <p className="error">Telegram rejected the bot token. Check it under API keys below.</p>
      )}
      {status.state === "error" && (
        <p className="muted">Telegram can't be reached right now. Retrying automatically.</p>
      )}
      {status.state === "waiting_to_pair" && status.pairing_code && (
        <>
          <p>
            In Telegram, open a chat with {bot} and send this message. It pairs that phone with
            this app, and only that phone.
          </p>
          <p>
            <code className="pair-code">/start {status.pairing_code}</code>
          </p>
        </>
      )}
      {status.paired && (
        <>
          <p>
            A phone is paired. Clips that need your review are sent to it with Approve and Reject
            buttons, and it can send /pause, /resume and /status.
          </p>
          <label className="check">
            <input
              type="checkbox"
              checked={status.notify}
              onChange={(event) =>
                void act(
                  () => api.updateAppSettings({ telegram_notify: event.target.checked }),
                  "Saved.",
                )
              }
            />
            Send clips to my phone
          </label>
          <div className="form-foot">
            <button
              className="btn"
              onClick={() => void act(() => api.telegramAction("test"), "Test message sent.")}
            >
              Send a test message
            </button>
            <button
              className="btn"
              onClick={() => void act(() => api.telegramAction("unpair"), "Phone unpaired.")}
            >
              Unpair
            </button>
            {note && <span className="muted small">{note}</span>}
          </div>
        </>
      )}
      {error && (
        <p className="error small" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}

const MCP_CONFIG = `{
  "mcpServers": {
    "clipper": {
      "command": "<path to Clipper>\\\\engine\\\\clipper-engine.exe",
      "args": ["--mcp"]
    }
  }
}`;

/** Letting Claude Desktop operate the app through five narrow tools. */
export function McpSettings() {
  const [status, setStatus] = useState<McpStatus | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .mcp()
      .then(setStatus)
      .catch((err: Error) => setError(err.message));
  }, []);

  const set = async (patch: { enabled?: boolean; can_approve?: boolean }) => {
    setError(null);
    try {
      setStatus(await api.setMcp(patch));
    } catch (err) {
      setError((err as Error).message);
    }
  };

  if (!status) {
    return error ? (
      <div className="banner banner-bad" role="alert">
        {error}
      </div>
    ) : null;
  }
  return (
    <div className="card">
      <h3>Claude Desktop (MCP)</h3>
      <p className="muted">
        Lets Claude Desktop list clips, reject a clip, pause publishing and read stats. It cannot
        see keys or files, change settings, or resume publishing.
      </p>
      <label className="check">
        <input
          type="checkbox"
          checked={status.enabled}
          onChange={(event) => void set({ enabled: event.target.checked })}
        />
        Allow Claude Desktop to connect
      </label>
      <label className="check">
        <input
          type="checkbox"
          checked={status.can_approve}
          disabled={!status.enabled}
          onChange={(event) => void set({ can_approve: event.target.checked })}
        />
        Also allow it to approve clips
      </label>
      <p className="muted small">
        Approving is off by default: clip titles and transcripts come from streams, and text in
        them could try to talk an assistant into approving. Leave it off unless you review what
        it approves.
      </p>
      {status.enabled && (
        <>
          <p className="small">Add this to Claude Desktop's MCP configuration:</p>
          <pre className="log">{MCP_CONFIG}</pre>
        </>
      )}
      {error && (
        <p className="error small" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}
