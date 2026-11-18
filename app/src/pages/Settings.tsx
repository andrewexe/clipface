import { useCallback, useEffect, useState, type FormEvent } from "react";
import { api, type AppSettings, type Keychain, type SecretInfo, type StorageUsage } from "../api";
import { McpSettings, PhoneApprovals, SafetySettings } from "./Safety";

function SecretRow({ secret, onChanged }: { secret: SecretInfo; onChanged: () => void }) {
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const run = async (action: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try {
      await action();
      setValue(""); // the key is in the keychain now; don't keep it in the page
      onChanged();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const save = (event: FormEvent) => {
    event.preventDefault();
    if (value.trim()) void run(() => api.setSecret(secret.name, value));
  };

  return (
    <form className="secret-row" onSubmit={save} data-secret={secret.name}>
      <div className="secret-head">
        <label htmlFor={secret.name}>{secret.label}</label>
        <span className={secret.is_set ? "pill pill-ok" : "pill"}>
          {secret.is_set ? "Saved" : "Not set"}
        </span>
      </div>
      <p className="muted small">{secret.help}</p>
      <div className="secret-controls">
        <input
          id={secret.name}
          type="password"
          autoComplete="off"
          spellCheck={false}
          placeholder={secret.is_set ? "Enter a new value to replace it" : "Paste here"}
          value={value}
          onChange={(event) => setValue(event.target.value)}
          disabled={busy}
        />
        <button type="submit" className="btn btn-primary" disabled={busy || !value.trim()}>
          {secret.is_set ? "Replace" : "Save"}
        </button>
        {secret.is_set && (
          <button
            type="button"
            className="btn"
            disabled={busy}
            onClick={() => void run(() => api.deleteSecret(secret.name))}
          >
            Remove
          </button>
        )}
      </div>
      {error && (
        <p className="error small" role="alert">
          {error}
        </p>
      )}
    </form>
  );
}

type NumberField = {
  key: keyof AppSettings & string;
  label: string;
  unit: string;
  help: string;
  min: number;
  max: number;
  step?: number;
};

const RECORDING_FIELDS: NumberField[] = [
  {
    key: "buffer_minutes",
    label: "Rolling buffer",
    unit: "minutes",
    help:
      "How much of each live stream is kept so a moment can be cut after it happens. Moments are cut within about three minutes, so 8 is plenty.",
    min: 2,
    max: 120,
  },
  {
    key: "disk_cap_gb",
    label: "Disk limit",
    unit: "GB",
    help: "Total space for stream recordings. The oldest footage is deleted first.",
    min: 1,
    max: 4000,
  },
  {
    key: "raw_retention_hours",
    label: "Keep raw footage",
    unit: "hours",
    help: "Recordings are deleted this long after a stream ends, and an approved clip's footage this long after it is cut. Clips waiting for review keep theirs.",
    min: 0.5,
    max: 72,
    step: 0.5,
  },
  {
    key: "pending_expiry_hours",
    label: "Delete unreviewed clips after",
    unit: "hours",
    help: "Clips left in To review this long are rejected and deleted. 0 keeps them until you decide.",
    min: 0,
    max: 720,
  },
];

const DETECTION_FIELDS: NumberField[] = [
  {
    key: "detect_z_threshold",
    label: "Sensitivity threshold",
    unit: "z-score",
    help: "How far chat must jump above its last 5 minutes to count as a moment. Lower finds more moments and more false alarms; 3 is the default, 4 to 6 is strict.",
    min: 1.5,
    max: 12,
    step: 0.5,
  },
  {
    key: "detect_cooldown_s",
    label: "Cooldown",
    unit: "seconds",
    help: "At most one moment per streamer in this many seconds.",
    min: 10,
    max: 600,
  },
  {
    key: "detect_audio_z",
    label: "Audio threshold",
    unit: "z-score",
    help: "Same idea for loudness spikes in the stream's audio. The only signal available for Kick.",
    min: 2,
    max: 12,
    step: 0.5,
  },
  {
    key: "detect_clips_count",
    label: "Viewer clips",
    unit: "clips",
    help: "Twitch only: this many viewer-made clips within 90 seconds also marks a moment. 0 turns it off.",
    min: 0,
    max: 50,
  },
];

const CLIP_FIELDS: NumberField[] = [
  {
    key: "clip_min_s",
    label: "Shortest clip",
    unit: "seconds",
    help: "Finished clips are never shorter than this.",
    min: 5,
    max: 60,
  },
  {
    key: "clip_max_s",
    label: "Longest clip",
    unit: "seconds",
    help: "Finished clips are never longer than this. The cut keeps the pay-off and trims the start.",
    min: 10,
    max: 180,
  },
  {
    key: "clip_before_s",
    label: "Before the moment",
    unit: "seconds",
    help: "How much footage before the peak goes into the clip window.",
    min: 5,
    max: 120,
  },
  {
    key: "clip_after_s",
    label: "After the moment",
    unit: "seconds",
    help: "How much footage after the peak goes into the clip window.",
    min: 2,
    max: 60,
  },
  {
    key: "score_min",
    label: "Minimum score",
    unit: "0 to 1",
    help: "Moments Claude scores below this are dropped before rendering.",
    min: 0,
    max: 1,
    step: 0.05,
  },
];

const SPEND_FIELDS: NumberField[] = [
  {
    key: "anthropic_daily_cap_usd",
    label: "Anthropic daily limit",
    unit: "USD",
    help: "Scoring stops for the day at this spend; clips then go to review unscored. Also set a monthly limit in the Anthropic console.",
    min: 0,
    max: 1000,
    step: 0.5,
  },
  {
    key: "transcription_daily_cap_usd",
    label: "Transcription daily limit",
    unit: "USD",
    help: "Captions stop for the day at this spend; clips then render without captions.",
    min: 0,
    max: 1000,
    step: 0.5,
  },
];

const AUTONOMY_FIELDS: NumberField[] = [
  {
    key: "auto_approve_score",
    label: "Score needed to publish without you",
    unit: "0.5 to 1",
    help: "At L2, a clip must score at least this and pass every safety check.",
    min: 0.5,
    max: 1,
    step: 0.05,
  },
  {
    key: "review_sample_rate",
    label: "Random spot-check",
    unit: "0 to 1",
    help: "This share of clips that would publish on their own comes to you instead, so you can see what the checks miss. 0.1 is one in ten.",
    min: 0,
    max: 1,
    step: 0.05,
  },
];

export function NumberSettings({ title, fields }: { title: string; fields: NumberField[] }) {
  const [settings, setSettings] = useState<AppSettings | null>(null);
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    api
      .appSettings()
      .then(setSettings)
      .catch((err: Error) => setError(err.message));
  }, []);

  if (!settings) {
    return error ? (
      <div className="banner banner-bad" role="alert">
        Could not load settings: {error}
      </div>
    ) : null;
  }

  const changed = fields.filter(
    (f) => draft[f.key] !== undefined && Number(draft[f.key]) !== Number(settings[f.key]),
  );

  const save = async (event: FormEvent) => {
    event.preventDefault();
    setError(null);
    setSaved(false);
    try {
      const patch = Object.fromEntries(changed.map((f) => [f.key, Number(draft[f.key])]));
      setSettings(await api.updateAppSettings(patch));
      setDraft({});
      setSaved(true);
    } catch (err) {
      setError((err as Error).message);
    }
  };

  return (
    <form className="card" onSubmit={save}>
      <h3>{title}</h3>
      {fields.map((field) => (
        <div className="number-row" key={field.key}>
          <div>
            <label htmlFor={field.key}>{field.label}</label>
            <p className="muted small">{field.help}</p>
          </div>
          <div className="number-input">
            <input
              id={field.key}
              type="number"
              min={field.min}
              max={field.max}
              step={field.step ?? 1}
              value={draft[field.key] ?? String(settings[field.key])}
              onChange={(event) => {
                setSaved(false);
                setDraft({ ...draft, [field.key]: event.target.value });
              }}
            />
            <span className="muted small">{field.unit}</span>
          </div>
        </div>
      ))}
      <div className="form-foot">
        <button type="submit" className="btn btn-primary" disabled={changed.length === 0}>
          Save
        </button>
        {saved && <span className="muted small">Saved.</span>}
        {error && (
          <span className="error small" role="alert">
            {error}
          </span>
        )}
      </div>
    </form>
  );
}

function gb(bytes: number): string {
  if (bytes < 1024 ** 3) return `${Math.round(bytes / 1024 ** 2)} MB`;
  return `${(bytes / 1024 ** 3).toFixed(1)} GB`;
}

const QUALITIES: [string, string][] = [
  ["720p60,720p,best", "720p (recommended): half the bandwidth and disk of 1080p"],
  ["1080p60,1080p,best", "1080p: sharper, needs about 8 Mbps per streamer"],
  ["480p,best", "480p: for a slow connection"],
];

function QualityPicker() {
  const [quality, setQuality] = useState<string | null>(null);
  useEffect(() => {
    api
      .appSettings()
      .then((s) => setQuality(s.stream_quality))
      .catch(() => setQuality(null));
  }, []);
  if (quality === null) return null;
  const known = QUALITIES.some(([value]) => value === quality);
  return (
    <div className="field">
      <label htmlFor="stream-quality">Recording quality</label>
      <select
        id="stream-quality"
        value={quality}
        onChange={async (event) => {
          const saved = await api.updateAppSettings({ stream_quality: event.target.value });
          setQuality(saved.stream_quality);
        }}
      >
        {!known && <option value={quality}>{quality}</option>}
        {QUALITIES.map(([value, label]) => (
          <option key={value} value={value}>
            {label}
          </option>
        ))}
      </select>
      <span className="muted small">
        If the engine log shows "Download of segment failed" or "Sequence gap", your connection
        can't keep up: pick a lower quality or follow fewer streamers at once. Applies the next
        time a stream starts recording.
      </span>
    </div>
  );
}

function KeepAwakeToggle() {
  const [on, setOn] = useState<boolean | null>(null);
  useEffect(() => {
    api
      .appSettings()
      .then((s) => setOn(s.keep_awake_while_recording))
      .catch(() => setOn(null));
  }, []);
  if (on === null) return null;
  return (
    <div className="field">
      <label>
        <input
          type="checkbox"
          checked={on}
          onChange={async (event) => {
            const saved = await api.updateAppSettings({
              keep_awake_while_recording: event.target.checked,
            });
            setOn(saved.keep_awake_while_recording);
          }}
        />{" "}
        Keep the computer awake while recording
      </label>
      <span className="muted small">
        While a live stream is recording, the computer will not go to sleep (the screen can still
        turn off). Sleep stops recording and posting until you come back. When nothing is live, it
        sleeps as usual.
      </span>
    </div>
  );
}

function StorageCard() {
  const [usage, setUsage] = useState<StorageUsage | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api
      .storage()
      .then(setUsage)
      .catch(() => setUsage(null));
  }, []);

  if (!usage) return null;
  const rows: [string, number, string][] = [
    ["Live stream buffers", usage.bytes.buffer, "the last few minutes of each live stream"],
    ["Clips", usage.bytes.clips, "clips waiting for review or posting"],
    ["Footage for clips", usage.bytes.raw, "kept while a clip can still be re-rendered"],
    ["Logs and database", usage.bytes.logs + usage.bytes.database, ""],
  ];
  return (
    <div className="card">
      <h3>Storage</h3>
      <QualityPicker />
      <KeepAwakeToggle />
      <table className="table">
        <tbody>
          {rows.map(([label, bytes, help]) => (
            <tr key={label}>
              <td>
                {label}
                {help && <div className="muted small">{help}</div>}
              </td>
              <td className="right">{gb(bytes)}</td>
            </tr>
          ))}
          <tr>
            <td>
              <strong>Clipper total</strong>
            </td>
            <td className="right">
              <strong>{gb(usage.total_bytes)}</strong>
            </td>
          </tr>
          <tr>
            <td>
              Exported clips
              <div className="muted small path">{usage.export_dir}</div>
            </td>
            <td className="right">{gb(usage.export_bytes)}</td>
          </tr>
        </tbody>
      </table>
      <p className="muted small">
        Rejected clips are deleted at once, and clips left unreviewed are deleted after the time
        set under Stream buffer. A clip's footage is deleted once it is posted or rejected, or a
        few hours after approval, and a stream's buffer about ten minutes after it ends. Exported
        clips are yours to delete once uploaded.
      </p>
      <div className="form-foot">
        <button
          className="btn"
          disabled={busy}
          onClick={async () => {
            setBusy(true);
            try {
              const result = await api.cleanUp();
              setUsage(result);
              setNote(`Freed ${gb(result.freed_bytes ?? 0)}.`);
            } finally {
              setBusy(false);
            }
          }}
        >
          Clean up now
        </button>
        {note && <span className="muted small">{note}</span>}
      </div>
    </div>
  );
}

export function Settings() {
  const [secrets, setSecrets] = useState<SecretInfo[] | null>(null);
  const [keychain, setKeychain] = useState<Keychain | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const data = await api.listSecrets();
      setSecrets(data.secrets);
      setKeychain(data.keychain);
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const groups = new Map<string, SecretInfo[]>();
  for (const secret of secrets ?? []) {
    groups.set(secret.group, [...(groups.get(secret.group) ?? []), secret]);
  }

  return (
    <section>
      <h1>Settings</h1>
      <h2>Safety</h2>
      <SafetySettings />
      <NumberSettings title="Publishing without approval (L2)" fields={AUTONOMY_FIELDS} />
      <PhoneApprovals />
      <McpSettings />
      <h2>Recording</h2>
      <NumberSettings title="Stream buffer" fields={RECORDING_FIELDS} />
      <StorageCard />
      <h2>Highlight detection</h2>
      <NumberSettings title="When is it a moment?" fields={DETECTION_FIELDS} />
      <h2>Clips and spending</h2>
      <NumberSettings title="Clip window and scoring" fields={CLIP_FIELDS} />
      <NumberSettings title="Daily spending limits" fields={SPEND_FIELDS} />
      <h2>API keys</h2>
      <p className="muted">
        Keys are saved to your operating system's keychain
        {keychain?.available ? ` (${keychain.name})` : ""}. They are never written to a file or a
        log, and the app cannot show them again after saving; it can only replace or remove them.
      </p>
      {keychain && !keychain.available && (
        <div className="banner banner-bad" role="alert">
          <strong>No OS keychain is available on this machine.</strong> Keys cannot be saved until
          one is. Clipper will not fall back to storing them in a file.
        </div>
      )}
      {error && (
        <div className="banner banner-bad" role="alert">
          Could not load settings: {error}{" "}
          <button className="btn" onClick={() => void load()}>
            Retry
          </button>
        </div>
      )}
      {!secrets && !error && <p className="muted">Loading…</p>}
      {[...groups.entries()].map(([group, items]) => (
        <div className="card" key={group}>
          <h3>{group}</h3>
          {items.map((secret) => (
            <SecretRow key={secret.name} secret={secret} onChanged={() => void load()} />
          ))}
        </div>
      ))}
    </section>
  );
}
