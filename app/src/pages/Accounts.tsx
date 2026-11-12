import { useCallback, useEffect, useState } from "react";
import { api, type Account, type AccountsResponse, type PostJob } from "../api";

const JOB_LABEL: Record<PostJob["status"], string> = {
  scheduled: "Scheduled",
  running: "Posting…",
  succeeded: "Posted",
  failed: "Failed",
  cancelled: "Cancelled",
};

function when(iso: string): string {
  return new Date(iso).toLocaleString([], {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function AccountRow({ account, onChanged }: { account: Account; onChanged: () => void }) {
  const [cap, setCap] = useState(String(account.daily_cap));
  const [mode, setMode] = useState<"idle" | "strike" | "disconnect">("idle");
  const [kind, setKind] = useState<"claim" | "strike" | "takedown">("claim");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const run = async (action: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try {
      await action();
      setMode("idle");
      setNote("");
      onChanged();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="streamer" data-account={account.handle}>
      <div className="streamer-main">
        <div className="streamer-name">
          <strong>{account.handle}</strong>
          <span className="tag">YouTube</span>
          <span className={account.paused ? "pill pill-bad" : "pill pill-ok"}>
            {account.paused ? "Paused" : "Active"}
          </span>
        </div>
        {account.paused && account.pause_reason && (
          <div className="error small">{account.pause_reason}</div>
        )}
        {error && (
          <div className="error small" role="alert">
            {error}
          </div>
        )}
      </div>
      <div className="streamer-actions">
        <label className="muted small" htmlFor={`cap-${account.id}`}>
          Posts per day
        </label>
        <input
          id={`cap-${account.id}`}
          className="cap-input"
          type="number"
          min={0}
          max={50}
          value={cap}
          onChange={(event) => setCap(event.target.value)}
          onBlur={() => {
            if (Number(cap) !== account.daily_cap) {
              void run(() => api.updateAccount(account.id, { daily_cap: Number(cap) }));
            }
          }}
        />
        <button
          className="btn"
          disabled={busy}
          onClick={() => void run(() => api.updateAccount(account.id, { paused: !account.paused }))}
        >
          {account.paused ? "Resume" : "Pause"}
        </button>
        <button className="btn" disabled={busy} onClick={() => setMode("strike")}>
          Report a claim
        </button>
        {mode === "disconnect" ? (
          <>
            <button
              className="btn btn-danger"
              disabled={busy}
              onClick={() => void run(() => api.disconnectAccount(account.id))}
            >
              Confirm disconnect
            </button>
            <button className="btn" disabled={busy} onClick={() => setMode("idle")}>
              Cancel
            </button>
          </>
        ) : (
          <button className="btn" disabled={busy} onClick={() => setMode("disconnect")}>
            Disconnect
          </button>
        )}
      </div>
      {mode === "strike" && (
        <div className="banner banner-warn full-row">
          <strong>Report a claim, strike or takedown request.</strong> This pauses the account
          until you resume it.
          <div className="secret-controls" style={{ marginTop: 8 }}>
            <select
              aria-label="What happened"
              value={kind}
              onChange={(event) => setKind(event.target.value as typeof kind)}
            >
              <option value="claim">Copyright claim</option>
              <option value="strike">Strike</option>
              <option value="takedown">Takedown request</option>
            </select>
            <input
              aria-label="Note"
              placeholder="Which video, who asked (optional)"
              value={note}
              maxLength={300}
              onChange={(event) => setNote(event.target.value)}
            />
            <button
              className="btn btn-danger"
              disabled={busy}
              onClick={() => void run(() => api.reportStrike(account.id, kind, note))}
            >
              Pause account
            </button>
            <button className="btn" disabled={busy} onClick={() => setMode("idle")}>
              Cancel
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

export function Accounts() {
  const [data, setData] = useState<AccountsResponse | null>(null);
  const [jobs, setJobs] = useState<PostJob[]>([]);
  const [authUrl, setAuthUrl] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [accounts, posts] = await Promise.all([api.accounts(), api.postJobs()]);
      setData(accounts);
      setJobs(posts.jobs);
      if (!accounts.connecting) setAuthUrl(null);
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    }
  }, []);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), 3000);
    return () => window.clearInterval(timer);
  }, [load]);

  const connect = async () => {
    setError(null);
    try {
      const result = await api.connectYouTube();
      setAuthUrl(result.auth_url);
      await load();
    } catch (err) {
      setError((err as Error).message);
    }
  };

  const jobAction = async (id: number, action: "cancel" | "retry") => {
    try {
      await api.postJobAction(id, action);
      await load();
    } catch (err) {
      setError((err as Error).message);
    }
  };

  const setPrivacy = async (privacy: AccountsResponse["privacy"]) => {
    try {
      await api.updateAppSettings({ youtube_privacy: privacy });
      await load();
    } catch (err) {
      setError((err as Error).message);
    }
  };

  return (
    <section>
      <h1>Accounts</h1>
      {error && (
        <div className="banner banner-bad" role="alert">
          {error}
        </div>
      )}
      {data && !data.google_keys_set && (
        <div className="card empty">
          <p>
            To post to YouTube, add your Google OAuth client ID and secret under Settings, in the
            YouTube group.
          </p>
          <p className="muted small">
            They come from a Google Cloud project with the YouTube Data API enabled and an OAuth
            client of type "Desktop app".
          </p>
        </div>
      )}
      {data && data.google_keys_set && (
        <div className="card">
          <div className="form-foot" style={{ paddingTop: 0 }}>
            <button className="btn btn-primary" onClick={() => void connect()}>
              Connect a YouTube channel
            </button>
            {data.connecting && <span className="muted small">Waiting for you to sign in…</span>}
            {data.connect_error && (
              <span className="error small" role="alert">
                Sign-in failed: {data.connect_error}
              </span>
            )}
          </div>
          {authUrl && data.connecting && (
            <div className="field">
              <span className="muted small">
                Finish signing in in your browser. If it did not open, copy this link into it:
              </span>
              <input readOnly value={authUrl} onFocus={(event) => event.target.select()} />
            </div>
          )}
        </div>
      )}
      {data && data.accounts.length > 0 && (
        <>
          <div className="card">
            {data.accounts.map((account) => (
              <AccountRow key={account.id} account={account} onChanged={() => void load()} />
            ))}
          </div>
          <div className="card">
            <div className="field" style={{ margin: 0 }}>
              <label htmlFor="privacy">New uploads are</label>
              <select
                id="privacy"
                value={data.privacy}
                onChange={(event) => void setPrivacy(event.target.value as AccountsResponse["privacy"])}
              >
                <option value="private">Private: only you can see them</option>
                <option value="unlisted">Unlisted: anyone with the link</option>
                <option value="public">Public</option>
              </select>
              <span className="muted small">
                Private is the safe default. Clips are only ever posted for streamers whose
                permission is Granted or Clipping program.
              </span>
            </div>
          </div>
        </>
      )}
      {jobs.length > 0 && (
        <>
          <h2>Posts</h2>
          <table className="table">
            <thead>
              <tr>
                <th>Clip</th>
                <th>Account</th>
                <th>When</th>
                <th>Status</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {jobs.map((job) => (
                <tr key={job.id} data-job={job.id}>
                  <td>{job.title ?? `Clip ${job.clip_id}`}</td>
                  <td>{job.account}</td>
                  <td>{when(job.scheduled_at)}</td>
                  <td>
                    {job.removed ? "Taken down" : JOB_LABEL[job.status]}
                    {job.url && !job.removed && <div className="path">{job.url}</div>}
                    {job.error && job.status !== "succeeded" && (
                      <div className="error small">{job.error}</div>
                    )}
                  </td>
                  <td>
                    {job.status === "scheduled" && (
                      <button className="btn" onClick={() => void jobAction(job.id, "cancel")}>
                        Cancel
                      </button>
                    )}
                    {(job.status === "failed" || job.status === "cancelled") && (
                      <button className="btn" onClick={() => void jobAction(job.id, "retry")}>
                        Retry
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </section>
  );
}
