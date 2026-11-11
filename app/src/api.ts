import { invoke, isTauri } from "@tauri-apps/api/core";

// The desktop shell starts the engine and hands us its port and session token.
// Both live in memory only: never localStorage, never a file.

type ShellStatus =
  | { state: "starting" }
  | { state: "ready"; port: number; token: string }
  | { state: "failed"; message: string };

type Connection = { baseUrl: string; token: string };

export class EngineError extends Error {
  constructor(
    message: string,
    readonly status?: number,
  ) {
    super(message);
  }
}

let connection: Promise<Connection> | null = null;

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

async function connect(): Promise<Connection> {
  if (!isTauri()) {
    // Browser-only UI development: run the engine by hand and pass its address in.
    const { VITE_ENGINE_URL: baseUrl, VITE_ENGINE_TOKEN: token } = import.meta.env;
    if (import.meta.env.DEV && baseUrl && token) return { baseUrl, token };
    throw new EngineError("Not running inside the Clipper desktop app.");
  }
  for (;;) {
    const status = await invoke<ShellStatus>("engine_status");
    if (status.state === "ready") {
      return { baseUrl: `http://127.0.0.1:${status.port}`, token: status.token };
    }
    if (status.state === "failed") throw new EngineError(status.message);
    await sleep(250);
  }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  connection ??= connect();
  let conn: Connection;
  try {
    conn = await connection;
  } catch (err) {
    connection = null;
    throw err;
  }
  let response: Response;
  try {
    response = await fetch(conn.baseUrl + path, {
      method,
      headers: {
        Authorization: `Bearer ${conn.token}`,
        ...(body === undefined ? {} : { "Content-Type": "application/json" }),
      },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    throw new EngineError("The engine is not responding.");
  }
  if (!response.ok) {
    let detail = `Engine returned ${response.status}`;
    try {
      const data = await response.json();
      if (typeof data.detail === "string") detail = data.detail;
      else if (Array.isArray(data.detail) && data.detail[0]?.msg) detail = data.detail[0].msg;
    } catch {
      // keep the status-code message
    }
    throw new EngineError(detail, response.status);
  }
  return (await response.json()) as T;
}

export type Health = {
  status: "ok" | "degraded";
  version: string;
  db: { ok: boolean; revision?: string | null; error?: string };
  keychain: Keychain;
  uptime_s: number;
};

export type Keychain = { name: string; available: boolean };

export type SecretInfo = {
  name: string;
  label: string;
  group: string;
  help: string;
  is_set: boolean;
};

export type AuditEntry = {
  id: number;
  ts: string;
  actor: "system" | "user" | "claude";
  action: string;
  object_id: string | null;
  details: Record<string, unknown> | null;
};

export type PlatformStatus = {
  state: "idle" | "ok" | "needs_keys" | "bad_keys" | "error";
  message: string | null;
  last_ok_at: string | null;
};

export type PermissionStatus = "none" | "asked" | "granted" | "program";

export type Streamer = {
  id: number;
  platform: "twitch" | "kick";
  username: string;
  display_name: string | null;
  url: string;
  enabled: boolean;
  permission_status: PermissionStatus;
  added_at: string;
  state: "live" | "offline" | "checking" | "paused" | "not_found";
  last_checked_at: string | null;
  session: {
    id: number;
    started_at: string;
    title: string | null;
    game: string | null;
    viewer_count: number | null;
    recording: {
      recording: boolean;
      buffered_seconds: number;
      bytes: number;
      error: string | null;
    } | null;
  } | null;
};

export type AppSettings = {
  buffer_minutes: number;
  segment_seconds: number;
  disk_cap_gb: number;
  raw_retention_hours: number;
  stream_quality: string;
  detect_z_threshold: number;
  detect_cooldown_s: number;
  detect_audio: boolean;
  detect_audio_z: number;
  detect_clips_count: number;
  [key: string]: number | string | boolean;
};

export type StreamersResponse = {
  streamers: Streamer[];
  platforms: Record<string, PlatformStatus>;
  recorder: { total_bytes: number; cap_bytes: number; recording_sessions: number[] } | null;
  poll_interval_s: number | null;
};

export const api = {
  appSettings: () => request<AppSettings>("GET", "/settings/app"),
  updateAppSettings: (patch: Partial<AppSettings>) =>
    request<AppSettings>("PATCH", "/settings/app", patch),
  streamers: () => request<StreamersResponse>("GET", "/streamers"),
  addStreamer: (url: string) => request<Streamer>("POST", "/streamers", { url }),
  updateStreamer: (
    id: number,
    patch: { enabled?: boolean; permission_status?: PermissionStatus },
  ) => request<Streamer>("PATCH", `/streamers/${id}`, patch),
  removeStreamer: (id: number) => request<{ id: number }>("DELETE", `/streamers/${id}`),
  health: () => request<Health>("GET", "/health"),
  listSecrets: () =>
    request<{ keychain: Keychain; secrets: SecretInfo[] }>("GET", "/settings/secrets"),
  setSecret: (name: string, value: string) =>
    request<{ name: string; is_set: boolean }>("PUT", `/settings/secrets/${name}`, { value }),
  deleteSecret: (name: string) =>
    request<{ name: string; is_set: boolean }>("DELETE", `/settings/secrets/${name}`),
  audit: (limit = 100) => request<{ entries: AuditEntry[] }>("GET", `/audit?limit=${limit}`),
  logs: (lines = 300) => request<{ lines: string[] }>("GET", `/logs?lines=${lines}`),
};
