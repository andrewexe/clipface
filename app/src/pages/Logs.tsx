import { useCallback, useEffect, useState } from "react";
import { api, type AuditEntry } from "../api";

type Tab = "audit" | "engine";

export function Logs() {
  const [tab, setTab] = useState<Tab>("audit");
  const [entries, setEntries] = useState<AuditEntry[]>([]);
  const [lines, setLines] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      if (tab === "audit") setEntries((await api.audit()).entries);
      else setLines((await api.logs()).lines);
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    }
  }, [tab]);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <section>
      <h1>Logs</h1>
      <div className="tabs">
        <button className={tab === "audit" ? "tab active" : "tab"} onClick={() => setTab("audit")}>
          Audit trail
        </button>
        <button
          className={tab === "engine" ? "tab active" : "tab"}
          onClick={() => setTab("engine")}
        >
          Engine log
        </button>
        <button className="btn tabs-right" onClick={() => void load()}>
          Refresh
        </button>
      </div>
      {error && (
        <div className="banner banner-bad" role="alert">
          {error}
        </div>
      )}
      {tab === "audit" &&
        (entries.length === 0 ? (
          <p className="muted">
            Nothing yet. Every state change (keys saved, clips approved, posts published) is
            recorded here.
          </p>
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th>When</th>
                <th>Who</th>
                <th>Action</th>
                <th>Object</th>
                <th>Details</th>
              </tr>
            </thead>
            <tbody>
              {entries.map((entry) => (
                <tr key={entry.id}>
                  <td>{new Date(entry.ts).toLocaleString()}</td>
                  <td>{entry.actor}</td>
                  <td>
                    <code>{entry.action}</code>
                  </td>
                  <td>{entry.object_id ?? ""}</td>
                  <td>
                    <code>{entry.details ? JSON.stringify(entry.details) : ""}</code>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ))}
      {tab === "engine" && <pre className="log">{lines.join("\n") || "The log is empty."}</pre>}
    </section>
  );
}
