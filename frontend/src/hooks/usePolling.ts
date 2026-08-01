import { useEffect, useRef } from "react";

/**
 * Re-run `callback` automatically.
 *
 * Unlike a plain `setInterval`, the next tick is scheduled only after the
 * previous `callback` resolves — long fetches never overlap.
 *
 * `intervalMs` may be a number or a function returning a number (evaluated
 * on every tick), which lets callers switch cadence, e.g. poll fast while a
 * job is running and settle back to 5 minutes when idle.
 */
export function usePolling(
  callback: () => void | Promise<void>,
  intervalMs: number | (() => number)
) {
  const cbRef = useRef(callback);
  cbRef.current = callback;

  const intervalRef = useRef(intervalMs);
  intervalRef.current = intervalMs;

  useEffect(() => {
    let timer: ReturnType<typeof setTimeout>;
    let cancelled = false;

    const run = async () => {
      try {
        await cbRef.current();
      } catch {
        // Polling must never crash the page; errors are surfaced in-page.
      } finally {
        if (!cancelled) schedule();
      }
    };

    const schedule = () => {
      const delay =
        typeof intervalRef.current === "function"
          ? intervalRef.current()
          : intervalRef.current;
      if (delay > 0) timer = setTimeout(run, delay);
    };

    schedule();
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
    // Empty deps: refs hold the latest callback/interval on every render.
  }, []);
}
