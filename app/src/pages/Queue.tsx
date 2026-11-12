import { useCallback, useEffect, useRef, useState } from "react";
import { api, type Clip, type ClipsResponse, type ClipStatus } from "../api";
import { useBlobUrl } from "../useBlobUrl";

type Tab = ClipStatus | "spot";

const TABS: { status: ClipStatus; label: string }[] = [
  { status: "pending", label: "To review" },
  { status: "approved", label: "Approved" },
  { status: "rejected", label: "Rejected" },
];

const TRIGGER_NAMES: Record<string, string> = {
  chat: "chat spike",
  hype: "hype burst",
  audio: "loud moment",
  clips: "viewers clipped it",
};

const TITLE_MAX = 100;

function when(iso: string): string {
  return new Date(iso).toLocaleString([], {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function Thumb({ clip }: { clip: Clip }) {
  const { url } = useBlobUrl(clip.has_video ? `/clips/${clip.id}/thumbnail` : null);
  return url ? <img className="thumb" src={url} alt="" /> : <div className="thumb" />;
}

function Preview({ clip, version }: { clip: Clip; version: number }) {
  const { url, error } = useBlobUrl(clip.has_video ? `/clips/${clip.id}/video` : null, version);
  if (!clip.has_video) return <div className="player player-empty">The video file is gone.</div>;
  if (error) return <div className="player player-empty">Could not load the video: {error}</div>;
  if (!url) return <div className="player player-empty">Loading…</div>;
  return <video className="player" src={url} controls autoPlay loop playsInline />;
}

function PolicyPanel({ clip }: { clip: Clip }) {
  const policy = clip.policy;
  if (!policy) {
    return <p className="muted small">Safety checks have not run on this clip yet.</p>;
  }
  if (policy.verdict === "clean") {
    return (
      <p className="small policy-clean">
        Safety checks passed.
        {policy.decision?.why.length ? ` Needs your approval because ${policy.decision.why.join("; ")}.` : ""}
      </p>
    );
  }
  const blocked = policy.verdict === "block";
  return (
    <div className={blocked ? "banner banner-bad" : "banner banner-warn"} role="status">
      <strong>
        {blocked ? "Rejected by the safety checks." : "The safety checks want you to look at this."}
      </strong>
      <ul className="findings">
        {policy.findings.map((finding, index) => (
          <li key={index}>{finding.detail}</li>
        ))}
      </ul>
    </div>
  );
}

function Detail({
  clip,
  exportDir,
  spotCheck,
  onDone,
  onChanged,
}: {
  clip: Clip;
  exportDir: string;
  spotCheck: boolean;
  onDone: (message?: string) => void;
  onChanged: () => void;
}) {
  const [title, setTitle] = useState(clip.title ?? "");
  const [caption, setCaption] = useState(clip.caption ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [version, setVersion] = useState(0);

  const dirty = title !== (clip.title ?? "") || caption !== (clip.caption ?? "");

  const saveEdits = async () => {
    if (dirty) await api.editClip(clip.id, { title, caption });
  };

  const act = async (action: "approve" | "reject" | "restore" | "export" | "rerender") => {
    setBusy(true);
    setError(null);
    try {
      if (action === "approve" || action === "export") await saveEdits();
      const result = await api.clipAction(clip.id, action);
      if (action === "rerender") {
        setVersion((v) => v + 1);
        onChanged();
      } else if (result.export_error) {
        onDone(`Approved, but not exported: ${result.export_error}`);
      } else {
        onDone();
      }
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const answer = async (ok: boolean) => {
    setBusy(true);
    setError(null);
    try {
      const result = await api.answerSpotCheck(clip.id, ok);
      if (ok) {
        onDone();
      } else if (result.taken_down === false) {
        onDone("It could not be taken down automatically: remove it on the platform by hand. Autonomy is back at L2.");
      } else {
        onDone("Taken down. Autonomy is back at L2.");
      }
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  // Keyboard: A approves, R rejects, when not typing in a field.
  const actRef = useRef(act);
  actRef.current = act;
  useEffect(() => {
    if (clip.status !== "pending") return;
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement;
      if (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || event.repeat) return;
      if (event.ctrlKey || event.metaKey || event.altKey) return;
      if (event.key === "a" || event.key === "A") void actRef.current("approve");
      if (event.key === "r" || event.key === "R") void actRef.current("reject");
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [clip.id, clip.status]);

  const triggers = clip.triggers.map((t) => TRIGGER_NAMES[t] ?? t).join(", ");

  return (
    <div className="detail">
      <Preview clip={clip} version={version} />
      <div className="detail-form">
        <label htmlFor="clip-title">Title</label>
        <input
          id="clip-title"
          value={title}
          maxLength={TITLE_MAX}
          onChange={(event) => setTitle(event.target.value)}
          disabled={busy || clip.status !== "pending"}
        />
        <div className="muted small right">
          {title.length}/{TITLE_MAX}
        </div>
        <label htmlFor="clip-caption">Caption</label>
        <textarea
          id="clip-caption"
          rows={3}
          value={caption}
          placeholder="Optional. Hashtags and a line of description."
          onChange={(event) => setCaption(event.target.value)}
          disabled={busy || clip.status !== "pending"}
        />
        <dl className="facts">
          <dt>Streamer</dt>
          <dd>
            {clip.streamer.name} ({clip.streamer.platform}) · {when(clip.start_ts)} ·{" "}
            {Math.round(clip.duration)} s
          </dd>
          <dt>Posts as</dt>
          <dd className="pre">{clip.description}</dd>
          <dt>Found by</dt>
          <dd>
            {triggers || "unknown"}
            {clip.score != null ? ` · score ${clip.score.toFixed(2)}` : ""}
          </dd>
          {clip.meta.reasons && clip.meta.reasons.length > 0 && (
            <>
              <dt>Why</dt>
              <dd>{clip.meta.reasons.join("; ")}</dd>
            </>
          )}
          <dt>Said</dt>
          <dd>{clip.transcript ?? <span className="muted">No speech captured.</span>}</dd>
          {clip.meta.captions === false && clip.meta.captions_note && (
            <>
              <dt>Captions</dt>
              <dd>None: {clip.meta.captions_note}.</dd>
            </>
          )}
          {typeof clip.meta.scoring_note === "string" && (
            <>
              <dt>Score</dt>
              <dd>Not scored: {clip.meta.scoring_note}.</dd>
            </>
          )}
          {clip.meta.exported_path && (
            <>
              <dt>Exported</dt>
              <dd className="path">{clip.meta.exported_path}</dd>
            </>
          )}
        </dl>
        <PolicyPanel clip={clip} />
        {clip.streamer.permission_status === "none" && (
          <p className="muted small">
            You have not recorded permission from {clip.streamer.name}. Check before uploading.
          </p>
        )}
        {error && (
          <p className="error" role="alert">
            {error}
          </p>
        )}
        <div className="detail-actions">
          {spotCheck && (
            <>
              <button
                className="btn btn-primary"
                disabled={busy}
                onClick={() => void answer(true)}
                title="It was right to publish this"
              >
                Looks fine
              </button>
              <button
                className="btn btn-danger"
                disabled={busy}
                onClick={() => void answer(false)}
                title="Takes it down where possible and drops autonomy to L2"
              >
                Should not be up
              </button>
            </>
          )}
          {clip.status === "pending" && (
            <>
              <button
                className="btn btn-primary"
                disabled={busy || !title.trim()}
                onClick={() => void act("approve")}
                title={`Approve and copy to ${exportDir}`}
              >
                Approve <kbd>A</kbd>
              </button>
              <button className="btn" disabled={busy} onClick={() => void act("reject")}>
                Reject <kbd>R</kbd>
              </button>
            </>
          )}
          {clip.status === "approved" && !spotCheck && (
            <button className="btn" disabled={busy} onClick={() => void act("export")}>
              Export again
            </button>
          )}
          {(clip.status === "approved" || clip.status === "rejected") && !spotCheck && (
            <button className="btn" disabled={busy} onClick={() => void act("restore")}>
              Back to review
            </button>
          )}
          {(clip.status === "pending" || clip.status === "approved") && !spotCheck && (
            <button
              className="btn"
              disabled={busy}
              onClick={() => void act("rerender")}
              title="Render again with this streamer's current layout"
            >
              {busy ? "Working…" : "Re-render"}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

export function Queue() {
  const [tab, setTab] = useState<Tab>("pending");
  const [data, setData] = useState<ClipsResponse | null>(null);
  const [selected, setSelected] = useState<number | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const listing = await api.clips(tab === "spot" ? "pending" : tab);
      if (tab === "spot") listing.clips = (await api.spotChecks()).clips;
      setData(listing);
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    }
  }, [tab]);

  useEffect(() => {
    setData(null);
    setSelected(null);
    void load();
    const timer = window.setInterval(() => void load(), 5000);
    return () => window.clearInterval(timer);
  }, [load]);

  const clips = data?.clips ?? [];
  const current = clips.find((c) => c.id === selected) ?? clips[0] ?? null;

  // Arrow keys move through the list.
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement;
      if (target.tagName === "INPUT" || target.tagName === "TEXTAREA") return;
      if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
      const index = clips.findIndex((c) => c.id === current?.id);
      const next = clips[index + (event.key === "ArrowDown" ? 1 : -1)];
      if (next) {
        event.preventDefault();
        setSelected(next.id);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [clips, current?.id]);

  const done = (message?: string) => {
    setNotice(message ?? null);
    // Move on to the next clip in the list.
    const index = clips.findIndex((c) => c.id === current?.id);
    setSelected(clips[index + 1]?.id ?? clips[index - 1]?.id ?? null);
    void load();
  };

  return (
    <section className="queue">
      <h1>Queue</h1>
      <div className="tabs">
        {TABS.map(({ status, label }) => (
          <button
            key={status}
            className={tab === status ? "tab active" : "tab"}
            onClick={() => setTab(status)}
          >
            {label}
            {data?.counts[status] ? <span className="count">{data.counts[status]}</span> : null}
          </button>
        ))}
        {data && (data.spot_checks > 0 || tab === "spot") && (
          <button className={tab === "spot" ? "tab active" : "tab"} onClick={() => setTab("spot")}>
            Spot checks
            {data.spot_checks ? <span className="count">{data.spot_checks}</span> : null}
          </button>
        )}
        {data && <span className="muted small tabs-right">Approved clips go to {data.export_dir}</span>}
      </div>
      {error && (
        <div className="banner banner-bad" role="alert">
          {error}
        </div>
      )}
      {notice && (
        <div className="banner banner-warn" role="status">
          {notice}
        </div>
      )}
      {data && clips.length === 0 && (
        <div className="card empty">
          {tab === "pending" ? (
            <>
              <p>Nothing to review.</p>
              <p className="muted small">
                Clips appear here a minute or so after a moment is detected on a live stream.
              </p>
            </>
          ) : tab === "spot" ? (
            <p>No spot checks waiting.</p>
          ) : (
            <p>No {tab} clips.</p>
          )}
        </div>
      )}
      {current && (
        <div className="queue-body">
          <ul className="clip-list">
            {clips.map((clip) => (
              <li key={clip.id}>
                <button
                  className={clip.id === current.id ? "clip-item active" : "clip-item"}
                  onClick={() => setSelected(clip.id)}
                >
                  <Thumb clip={clip} />
                  <span className="clip-item-text">
                    <strong>{clip.title ?? "Untitled"}</strong>
                    <span className="muted small">
                      {clip.streamer.name} · {when(clip.start_ts)}
                    </span>
                  </span>
                </button>
              </li>
            ))}
          </ul>
          <Detail
            key={current.id}
            clip={current}
            exportDir={data?.export_dir ?? ""}
            spotCheck={tab === "spot"}
            onDone={done}
            onChanged={() => void load()}
          />
        </div>
      )}
    </section>
  );
}
