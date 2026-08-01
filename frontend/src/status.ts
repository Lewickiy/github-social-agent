import type { Job } from "./types";

export const STATUS_META: Record<
  string,
  { label: string; cls: string; dot: string }
> = {
  NEW: {
    label: "New",
    cls: "bg-canvas-subtle text-fg-muted border border-border",
    dot: "bg-fg-subtle",
  },
  FOLLOWED: {
    label: "Followed",
    cls: "bg-accent/10 text-accent border border-accent/30",
    dot: "bg-accent",
  },
  FOLLOWBACK: {
    label: "Followback",
    cls: "bg-success-subtle text-success-fg border border-success/30",
    dot: "bg-success",
  },
  UNFOLLOWED_AFTER_MUTUAL_FOLLOW: {
    label: "Unfollowed",
    cls: "bg-danger-subtle text-danger-fg border border-danger/30",
    dot: "bg-danger",
  },
  DELETED: {
    label: "Deleted",
    cls: "bg-canvas-subtle text-fg-muted border border-border-muted",
    dot: "bg-fg-subtle",
  },
  UNKNOWN: {
    label: "Unknown",
    cls: "bg-canvas-subtle text-fg-muted border border-border-muted",
    dot: "bg-fg-subtle",
  },
};

export const JOB_STATUS_META: Record<
  string,
  { label: string; cls: string; dot: string }
> = {
  PENDING: {
    label: "Pending",
    cls: "bg-canvas-subtle text-fg-muted border border-border",
    dot: "bg-fg-subtle",
  },
  RUNNING: {
    label: "Running",
    cls: "bg-accent/10 text-accent border border-accent/30",
    dot: "bg-accent animate-pulse",
  },
  SUCCESS: {
    label: "Success",
    cls: "bg-success-subtle text-success-fg border border-success/30",
    dot: "bg-success",
  },
  FAILED: {
    label: "Failed",
    cls: "bg-danger-subtle text-danger-fg border border-danger/30",
    dot: "bg-danger",
  },
};

export const STATUS_ORDER = [
  "NEW",
  "FOLLOWED",
  "FOLLOWBACK",
  "UNFOLLOWED_AFTER_MUTUAL_FOLLOW",
  "DELETED",
];

// GitHub language colours (subset of the popular ones)
export const LANGUAGE_COLORS: Record<string, string> = {
  Python: "#3572A5",
  JavaScript: "#f1e05a",
  TypeScript: "#3178c6",
  Java: "#b07219",
  "C++": "#f34b7d",
  C: "#555555",
  "C#": "#178600",
  Go: "#00ADD8",
  Rust: "#dea584",
  Ruby: "#701516",
  PHP: "#4F5D95",
  Swift: "#F05138",
  Kotlin: "#A97BFF",
  Shell: "#89e051",
  HTML: "#e34c26",
  CSS: "#563d7c",
  "Jupyter Notebook": "#DA5B0B",
  Dart: "#00B4AB",
  Scala: "#c22d40",
  Haskell: "#5e5086",
  Lua: "#000080",
  R: "#198CE7",
  ObjectiveC: "#438eff",
  "Vim Script": "#199f4b",
  Elixir: "#6e4a7e",
  Clojure: "#db5855",
  PowerShell: "#012456",
  VHDL: "#adb2cb",
  Assembly: "#6E4C13",
  Dockerfile: "#384d54",
  Makefile: "#427819",
  TeX: "#3D6117",
  Vue: "#41b883",
  Svelte: "#ff3e00",
  Zig: "#ec915c",
};

export function languageColor(lang: string): string {
  return LANGUAGE_COLORS[lang] ?? "#8b949e";
}

export function scoreColor(score: number | null | undefined): string {
  if (score == null || score <= 0) return "#8c959f";
  if (score >= 60) return "#1a7f37";
  if (score >= 35) return "#9a6700";
  return "#57606a";
}

export const MODE_LABELS: Record<string, { label: string; desc: string }> = {
  collect: { label: "Full collect", desc: "Discover users + fetch repos" },
  "collect-users": { label: "Discover users", desc: "Traverse the follower graph" },
  "collect-users-rep": { label: "Collect repos", desc: "Fetch repos & languages" },
  "collect-self": { label: "Sync owner", desc: "Refresh your own profile data" },
  score: { label: "Score", desc: "Score / re-score all users" },
  follow: { label: "Follow", desc: "Follow top-scored users" },
  silent: { label: "Silent run", desc: "Stealth collect + score + follow" },
};

export function jobDuration(job: Job): string {
  if (!job.started_at) return "—";
  const end = job.finished_at ? new Date(job.finished_at) : new Date();
  const start = new Date(job.started_at);
  const s = Math.max(0, Math.floor((end.getTime() - start.getTime()) / 1000));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${s % 60}s`;
  return `${Math.floor(m / 60)}h ${m % 60}m`;
}
