import { useEffect, useState } from "react";
import { api, type Health } from "./api";
import { Accounts } from "./pages/Accounts";
import { Logs } from "./pages/Logs";
import { Queue } from "./pages/Queue";
import { KillSwitch } from "./pages/Safety";
import { Settings } from "./pages/Settings";
import { Streamers } from "./pages/Streamers";

const PAGES = {
  Streamers,
  Queue,
  Accounts,
  Logs,
  Settings,
} as const;

type PageName = keyof typeof PAGES;

type EngineState =
  | { kind: "connecting" }
  | { kind: "ok"; health: Health }
  | { kind: "down"; message: string };

function useEngineHealth(): EngineState {
  const [state, setState] = useState<EngineState>({ kind: "connecting" });
  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;
    const poll = async () => {
      try {
        const health = await api.health();
        if (!cancelled) setState({ kind: "ok", health });
      } catch (err) {
        if (!cancelled) setState({ kind: "down", message: (err as Error).message });
      }
      if (!cancelled) timer = window.setTimeout(poll, 5000);
    };
    void poll();
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, []);
  return state;
}

function EngineBadge({ engine }: { engine: EngineState }) {
  if (engine.kind === "connecting") {
    return (
      <div className="engine-badge">
        <span className="dot dot-wait" />
        Starting engine…
      </div>
    );
  }
  if (engine.kind === "down") {
    return (
      <div className="engine-badge" title={engine.message}>
        <span className="dot dot-bad" />
        Engine offline
      </div>
    );
  }
  const healthy = engine.health.status === "ok";
  return (
    <div className="engine-badge" data-testid="engine-ok">
      <span className={`dot ${healthy ? "dot-ok" : "dot-bad"}`} />
      {healthy ? "Engine running" : "Engine degraded"}
      <span className="muted"> · v{engine.health.version}</span>
    </div>
  );
}

export function App() {
  const [page, setPage] = useState<PageName>("Settings");
  const engine = useEngineHealth();
  const Page = PAGES[page];

  return (
    <div className="shell">
      <nav className="sidebar">
        <div className="brand">Clipper</div>
        {(Object.keys(PAGES) as PageName[]).map((name) => (
          <button
            key={name}
            className={name === page ? "nav-item active" : "nav-item"}
            onClick={() => setPage(name)}
          >
            {name}
          </button>
        ))}
        <div className="sidebar-foot">
          {engine.kind === "ok" && <KillSwitch />}
          <EngineBadge engine={engine} />
        </div>
      </nav>
      <main className="content">
        {engine.kind === "down" ? (
          <div className="banner banner-bad" role="alert">
            <strong>The engine is not running.</strong> {engine.message}
          </div>
        ) : (
          <Page />
        )}
      </main>
    </div>
  );
}
