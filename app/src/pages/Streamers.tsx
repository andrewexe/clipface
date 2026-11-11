import { useCallback, useEffect, useState, type FormEvent } from "react";
import {
  api,
  type PermissionStatus,
  type PlatformStatus,
  type Streamer,
  type StreamersResponse,
} from "../api";

const PLATFORM_NAMES: Record<string, string> = { twitch: "Twitch", kick: "Kick" };

const PERMISSIONS: { value: PermissionStatus; label: string }[] = [
  { value: "none", label: "No permission" },
  { value: "asked", label: "Asked" },
  { value: "granted", label: "Granted" },
  { value: "program", label: "Clipping program" },
];

function since(iso: string): string {
  const minutes = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 60000));
  if (minutes < 60) return `${minutes} min`;
  return `${Math.floor(minutes / 60)} h ${minutes % 60} min`;
}

function size(bytes: number): string {
  if (bytes >= 1024 ** 3) return `${(bytes / 1024 ** 3).toFixed(1)} GB`;
  return `${Math.round(bytes / 1024 ** 2)} MB`;
}

function RecordingNote({ streamer }: { streamer: Streamer }) {
  const rec = streamer.session?.recording;
  if (!streamer.session) return null;
  if (!rec) return <div className="muted small">Starting to record…</div>;
  if (rec.recording) {
    return (
      <div className="muted small">
        <span className="rec-dot" /> Recording · last {Math.round(rec.buffered_seconds / 60)} min
        kept ({size(rec.bytes)})
      </div>
    );
  }
  return (
    <div className="error small">
      Not recording: {rec.error ?? "waiting to retry"}. Retrying automatically.
    </div>
  );
}

function StatePill({ streamer }: { streamer: Streamer }) {
  switch (streamer.state) {
    case "live":
      return <span className="pill pill-live">Live</span>;
    case "offline":
      return <span className="pill">Offline</span>;
    case "paused":
      return <span className="pill">Paused</span>;
    case "not_found":
      return <span className="pill pill-bad">Channel not found</span>;
    default:
      return <span className="pill">Checking…</span>;
  }
}

function PlatformBanner({ name, status }: { name: string; status: PlatformStatus }) {
  const label = PLATFORM_NAMES[name] ?? name;
  if (status.state === "needs_keys") {
    return (
      <div className="banner banner-warn" role="status">
        <strong>{label} keys are missing.</strong> Add the {label} client ID and secret in
        Settings; {label} channels can't be checked until then.
      </div>
    );
  }
  if (status.state === "bad_keys") {
    return (
      <div className="banner banner-bad" role="alert">
        <strong>{label} rejected your keys.</strong> Check the {label} client ID and secret in
        Settings.
      </div>
    );
  }
  if (status.state === "error") {
    return (
      <div className="banner banner-warn" role="status">
        <strong>Can't reach {label} right now.</strong> {status.message} Retrying automatically.
      </div>
    );
  }
  return null;
}

function StreamerRow({ streamer, onChanged }: { streamer: Streamer; onChanged: () => void }) {
  const [busy, setBusy] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const run = async (action: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try {
      await action();
      onChanged();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
      setConfirming(false);
    }
  };

  const session = streamer.session;
  return (
    <div className="streamer" data-streamer={streamer.username}>
      <div className="streamer-main">
        <div className="streamer-name">
          <strong>{streamer.display_name ?? streamer.username}</strong>
          <span className={`tag tag-${streamer.platform}`}>{PLATFORM_NAMES[streamer.platform]}</span>
          <StatePill streamer={streamer} />
        </div>
        <div className="muted small">
          {session ? (
            <>
              {session.title ?? "Untitled stream"}
              {session.game ? ` · ${session.game}` : ""} · live for {since(session.started_at)}
              {session.viewer_count != null
                ? ` · ${session.viewer_count.toLocaleString()} viewers`
                : ""}
            </>
          ) : streamer.state === "not_found" ? (
            <>No channel exists at {streamer.url}. Remove it and add the right URL.</>
          ) : (
            streamer.url
          )}
        </div>
        <RecordingNote streamer={streamer} />
        {error && (
          <div className="error small" role="alert">
            {error}
          </div>
        )}
      </div>
      <div className="streamer-actions">
        <select
          aria-label={`Permission for ${streamer.username}`}
          title="Clips are never auto-published without Granted or Clipping program"
          value={streamer.permission_status}
          disabled={busy}
          onChange={(event) =>
            void run(() =>
              api.updateStreamer(streamer.id, {
                permission_status: event.target.value as PermissionStatus,
              }),
            )
          }
        >
          {PERMISSIONS.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
        <button
          className="btn"
          disabled={busy}
          onClick={() =>
            void run(() => api.updateStreamer(streamer.id, { enabled: !streamer.enabled }))
          }
        >
          {streamer.enabled ? "Pause" : "Resume"}
        </button>
        {confirming ? (
          <>
            <button
              className="btn btn-danger"
              disabled={busy}
              onClick={() => void run(() => api.removeStreamer(streamer.id))}
            >
              Confirm remove
            </button>
            <button className="btn" disabled={busy} onClick={() => setConfirming(false)}>
              Cancel
            </button>
          </>
        ) : (
          <button className="btn" disabled={busy} onClick={() => setConfirming(true)}>
            Remove
          </button>
        )}
      </div>
    </div>
  );
}

export function Streamers() {
  const [data, setData] = useState<StreamersResponse | null>(null);
  const [url, setUrl] = useState("");
  const [adding, setAdding] = useState(false);
  const [addError, setAddError] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setData(await api.streamers());
      setLoadError(null);
    } catch (err) {
      setLoadError((err as Error).message);
    }
  }, []);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), 5000);
    return () => window.clearInterval(timer);
  }, [load]);

  const add = async (event: FormEvent) => {
    event.preventDefault();
    if (!url.trim()) return;
    setAdding(true);
    setAddError(null);
    try {
      await api.addStreamer(url);
      setUrl("");
      await load();
    } catch (err) {
      setAddError((err as Error).message);
    } finally {
      setAdding(false);
    }
  };

  const usedPlatforms = new Set(data?.streamers.map((s) => s.platform));

  return (
    <section>
      <h1>Streamers</h1>
      <form className="add-row" onSubmit={add}>
        <input
          aria-label="Channel URL"
          placeholder="Paste a Twitch or Kick channel URL"
          value={url}
          spellCheck={false}
          onChange={(event) => setUrl(event.target.value)}
          disabled={adding}
        />
        <button type="submit" className="btn btn-primary" disabled={adding || !url.trim()}>
          Add
        </button>
      </form>
      {addError && (
        <p className="error" role="alert">
          {addError}
        </p>
      )}
      {loadError && (
        <div className="banner banner-bad" role="alert">
          Could not load streamers: {loadError}
        </div>
      )}
      {data &&
        Object.entries(data.platforms)
          .filter(([name]) => usedPlatforms.has(name as Streamer["platform"]))
          .map(([name, status]) => <PlatformBanner key={name} name={name} status={status} />)}
      {data && data.streamers.length === 0 && (
        <div className="card empty">
          <p>No streamers yet. Paste a channel URL above to start watching for live streams.</p>
          <p className="muted small">
            Only clip streamers who allow it. Each streamer starts at "No permission", which keeps
            their clips out of auto-publishing.
          </p>
        </div>
      )}
      {data && data.streamers.length > 0 && (
        <div className="card">
          {data.streamers.map((streamer) => (
            <StreamerRow key={streamer.id} streamer={streamer} onChanged={() => void load()} />
          ))}
        </div>
      )}
      {data?.poll_interval_s && data.streamers.length > 0 && (
        <p className="muted small">
          Live status is checked every {data.poll_interval_s} seconds.
          {data.recorder
            ? ` Recordings use ${size(data.recorder.total_bytes)} of the ${size(data.recorder.cap_bytes)} disk limit.`
            : ""}
        </p>
      )}
    </section>
  );
}
