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

/** Fetch a file from the engine (video, image) as a blob. The session token goes in a
 *  header, so it never appears in a URL. */
export async function fetchBlob(path: string): Promise<Blob> {
  connection ??= connect();
  const conn = await connection;
  const response = await fetch(conn.baseUrl + path, {
    headers: { Authorization: `Bearer ${conn.token}` },
  });
  if (!response.ok) {
    let detail = `Engine returned ${response.status}`;
    try {
      const data = await response.json();
      if (typeof data.detail === "string") detail = data.detail;
    } catch {
      // keep the status-code message
    }
    throw new EngineError(detail, response.status);
  }
  return await response.blob();
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
  facecam: Facecam | null;
  program_rules: ProgramRules;
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

export type ProgramRules = { required_tags: string[]; credit_format: string; banned: string[] };

export type Control = {
  publishing_paused: boolean;
  autonomy_level: "L1" | "L2";
  autonomy_l2_since: string | null;
  l3: { eligible: boolean; reason: string };
};

export type StopResult = {
  dropped: number;
  removed: number;
  cancelled_posts: number;
  could_not_remove: number[];
};

export type Facecam = { x: number; y: number; w: number; h: number };

export type ClipStatus = "pending" | "approved" | "rejected" | "published" | "removed";

export type Clip = {
  id: number;
  status: ClipStatus;
  title: string | null;
  caption: string | null;
  transcript: string | null;
  score: number | null;
  start_ts: string;
  duration: number;
  streamer: {
    id: number;
    name: string;
    platform: "twitch" | "kick";
    url: string;
    permission_status: PermissionStatus;
  };
  credit: string;
  triggers: string[];
  signal_scores: Record<string, unknown> | null;
  description: string;
  policy: {
    verdict: "clean" | "flag" | "block";
    findings: { check: string; verdict: "flag" | "block"; detail: string }[];
    decision?: { action: string; why: string[] };
    sampled?: boolean;
  } | null;
  meta: {
    captions?: boolean;
    captions_note?: string | null;
    layout?: string;
    exported_path?: string | null;
    reasons?: string[];
    [key: string]: unknown;
  };
  has_video: boolean;
  export_error?: string | null;
};

export type ClipsResponse = {
  clips: Clip[];
  counts: Partial<Record<ClipStatus, number>>;
  export_dir: string;
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
  clip_before_s: number;
  clip_after_s: number;
  captions: boolean;
  transcription_daily_cap_usd: number;
  scoring: boolean;
  scoring_model: string;
  score_min: number;
  anthropic_daily_cap_usd: number;
  auto_approve_score: number;
  review_sample_rate: number;
  music_threshold: number;
  music_action: "review" | "reject";
  blocked_words: string[];
  export_dir: string;
  [key: string]: number | string | boolean | string[];
};

export type StreamersResponse = {
  streamers: Streamer[];
  platforms: Record<string, PlatformStatus>;
  recorder: { total_bytes: number; cap_bytes: number; recording_sessions: number[] } | null;
  poll_interval_s: number | null;
};

export const api = {
  control: () => request<Control>("GET", "/control"),
  setPaused: (paused: boolean) => request<Control>("POST", "/control/kill-switch", { paused }),
  setAutonomy: (level: "L1" | "L2" | "L3") => request<Control>("POST", "/control/autonomy", { level }),
  stopStreamer: (id: number) => request<StopResult>("POST", `/streamers/${id}/stop`),
  clips: (status: ClipStatus) => request<ClipsResponse>("GET", `/clips?status=${status}`),
  editClip: (id: number, patch: { title?: string; caption?: string }) =>
    request<Clip>("PATCH", `/clips/${id}`, patch),
  clipAction: (id: number, action: "approve" | "reject" | "restore" | "export" | "rerender") =>
    request<Clip>("POST", `/clips/${id}/${action}`),
  appSettings: () => request<AppSettings>("GET", "/settings/app"),
  updateAppSettings: (patch: Partial<AppSettings>) =>
    request<AppSettings>("PATCH", "/settings/app", patch),
  streamers: () => request<StreamersResponse>("GET", "/streamers"),
  addStreamer: (url: string) => request<Streamer>("POST", "/streamers", { url }),
  updateStreamer: (
    id: number,
    patch: {
      enabled?: boolean;
      permission_status?: PermissionStatus;
      layout?: { facecam: Facecam | null };
      program_rules?: ProgramRules;
    },
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
