import type {
  Config,
  DiscoveryState,
  JobsResponse,
  LanguageOption,
  MLState,
  Settings,
  Stats,
  UserProfile,
  UsersResponse,
} from "./types";

const BASE = "/api";

async function get<T>(path: string): Promise<T> {
  const res = await fetch(BASE + path);
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    throw new Error(body?.detail ?? `HTTP ${res.status}`);
  }
  return res.json() as Promise<T>;
}

async function post<T>(path: string, body?: unknown): Promise<T> {
  const res = await fetch(BASE + path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    const b = await res.json().catch(() => null);
    throw new Error(b?.detail ?? `HTTP ${res.status}`);
  }
  return res.json() as Promise<T>;
}

async function put<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(BASE + path, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const b = await res.json().catch(() => null);
    throw new Error(b?.detail ?? `HTTP ${res.status}`);
  }
  return res.json() as Promise<T>;
}

export interface UsersParams {
  q?: string;
  status?: string;
  language?: string;
  ml?: string;
  min_score?: number;
  sort?: string;
  order?: string;
  page?: number;
  per_page?: number;
}

export const api = {
  /** Aggregate overview stats; `days` scopes the activity/feed blocks. */
  stats: (days = 30) => get<Stats>(`/stats?days=${days}`),
  users: (params: UsersParams = {}) => {
    const qs = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) {
      if (v !== undefined && v !== null && v !== "") qs.set(k, String(v));
    }
    const q = qs.toString();
    return get<UsersResponse>(`/users${q ? `?${q}` : ""}`);
  },
  user: (username: string) =>
    get<UserProfile>(`/users/${encodeURIComponent(username)}`),
  languages: () => get<{ items: LanguageOption[] }>("/languages"),
  ml: () => get<MLState>("/ml"),
  discovery: () => get<DiscoveryState>("/discovery"),
  config: () => get<Config>("/config"),
  settings: () => get<Settings>("/settings"),
  saveSettings: (timezone: string) => put<Settings>("/settings", { timezone }),
  jobs: () => get<JobsResponse>("/jobs"),
  startJob: (mode: string) => post<unknown>("/jobs", { mode }),
  jobLog: (id: number) => get<{ log: string }>(`/jobs/${id}/log`),
};

export function formatDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function timeAgo(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  const diff = Date.now() - d.getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days < 30) return `${days}d ago`;
  return formatDate(iso);
}
