import { useCallback, useEffect, useState } from "react";
import { api, type AppSettings, type Control } from "../api";

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
