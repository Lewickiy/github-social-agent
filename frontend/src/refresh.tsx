import { createContext, useCallback, useContext, useMemo, useState } from "react";
import type { ReactNode } from "react";

export interface RefreshOption {
  label: string;
  ms: number;
}

/**
 * The discrete refresh intervals the dashboard may poll at.
 *
 * The default (1 min) sits at the center of the slider — per the original
 * spec, the ten stops are 3s … 30m with 1m as the default selection.
 */
export const REFRESH_OPTIONS: RefreshOption[] = [
  { label: "3s", ms: 3_000 },
  { label: "5s", ms: 5_000 },
  { label: "15s", ms: 15_000 },
  { label: "30s", ms: 30_000 },
  { label: "1m", ms: 60_000 },
  { label: "3m", ms: 180_000 },
  { label: "5m", ms: 300_000 },
  { label: "10m", ms: 600_000 },
  { label: "15m", ms: 900_000 },
  { label: "30m", ms: 1_800_000 },
];

export const DEFAULT_REFRESH_MS = 60_000;

const STORAGE_KEY = "github-social-refresh-ms";

interface RefreshContextValue {
  intervalMs: number;
  intervalLabel: string;
  optionIndex: number;
  setIntervalMs: (ms: number) => void;
}

const RefreshContext = createContext<RefreshContextValue | null>(null);

function readStored(): number | null {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const ms = Number(raw);
    return REFRESH_OPTIONS.some((o) => o.ms === ms) ? ms : null;
  } catch {
    return null;
  }
}

export function RefreshProvider({ children }: { children: ReactNode }) {
  const [intervalMs, setIntervalMsState] = useState<number>(
    () => readStored() ?? DEFAULT_REFRESH_MS
  );

  const setIntervalMs = useCallback((ms: number) => {
    setIntervalMsState(ms);
    try {
      localStorage.setItem(STORAGE_KEY, String(ms));
    } catch {
      // Persistence is best-effort (private mode, etc.).
    }
  }, []);

  const value = useMemo<RefreshContextValue>(() => {
    const found = REFRESH_OPTIONS.findIndex((o) => o.ms === intervalMs);
    const optionIndex =
      found === -1
        ? REFRESH_OPTIONS.findIndex((o) => o.ms === DEFAULT_REFRESH_MS)
        : found;
    return {
      intervalMs,
      intervalLabel: REFRESH_OPTIONS[optionIndex].label,
      optionIndex,
      setIntervalMs,
    };
  }, [intervalMs, setIntervalMs]);

  return (
    <RefreshContext.Provider value={value}>{children}</RefreshContext.Provider>
  );
}

export function useRefresh(): RefreshContextValue {
  const ctx = useContext(RefreshContext);
  if (!ctx) {
    throw new Error("useRefresh must be used within <RefreshProvider>");
  }
  return ctx;
}
