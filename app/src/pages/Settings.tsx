import { useCallback, useEffect, useState, type FormEvent } from "react";
import { api, type AppSettings, type Keychain, type SecretInfo } from "../api";

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
    help: "How much of each live stream is kept so a moment can be cut after it happens.",
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
    help: "Recordings are deleted this long after a stream ends, unless a clip uses them.",
    min: 0.5,
    max: 72,
    step: 0.5,
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
    (f) => draft[f.key] !== undefined && Number(draft[f.key]) !== settings[f.key],
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
      <h2>Recording</h2>
      <NumberSettings title="Stream buffer" fields={RECORDING_FIELDS} />
      <h2>Highlight detection</h2>
      <NumberSettings title="When is it a moment?" fields={DETECTION_FIELDS} />
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
