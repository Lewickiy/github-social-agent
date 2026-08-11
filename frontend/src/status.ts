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
  UNFOLLOWED_NO_INTERACTION: {
    label: "Unfollowed (no interaction)",
    cls: "bg-attention-subtle text-attention border border-attention/30",
    dot: "bg-attention",
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

export interface ActionMeta {
  label: string;
  cls: string;
}

// Activity-feed event badges (Overview → Recent activity).  The stream
// carries FOLLOW / FOLLOWBACK / UNFOLLOWED / DELETED lifecycle events;
// unknown future event types fall back to a neutral badge.
export const ACTION_META: Record<string, ActionMeta> = {
  FOLLOW: {
    label: "Followed",
    cls: "bg-accent/10 text-accent border border-accent/30",
  },
  FOLLOWBACK: {
    label: "Followback",
    cls: "bg-success-subtle text-success-fg border border-success/30",
  },
  UNFOLLOWED: {
    label: "Unfollowed",
    cls: "bg-danger-subtle text-danger-fg border border-danger/30",
  },
  UNFOLLOW: {
    label: "Unfollowed (no interaction)",
    cls: "bg-attention-subtle text-attention border border-attention/30",
  },
  DELETED: {
    label: "Deleted",
    cls: "bg-canvas-subtle text-fg-muted border border-border",
  },
};

const FALLBACK_ACTION: ActionMeta = {
  label: "",
  cls: "bg-canvas-subtle text-fg-muted border border-border",
};

export function actionMeta(action: string): ActionMeta {
  const meta = ACTION_META[action];
  return meta ? meta : { ...FALLBACK_ACTION, label: action };
}

export const STATUS_ORDER = [
  "FOLLOWED",
  "FOLLOWBACK",
  "UNFOLLOWED_AFTER_MUTUAL_FOLLOW",
  "UNFOLLOWED_NO_INTERACTION",
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

