import { useCallback, useEffect, useState, type FormEvent } from "react";
import { api, type Keychain, type SecretInfo } from "../api";

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
