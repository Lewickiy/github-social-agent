import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import {
  Gauge,
  HeartHandshake,
  Sparkles,
  Target,
  TrendingUp,
  UserCheck,
  Users,
} from "lucide-react";
import { api, timeAgo } from "../api";
import type { Stats } from "../types";
import StatCard from "../components/StatCard";
import FollowersChart from "../components/FollowersChart";
import { ScoreBars, StatusBars } from "../components/DistributionCharts";
import { usePolling } from "../hooks/usePolling";
import { actionMeta, interactionMeta } from "../status";
import { useRefresh } from "../refresh";

/**
 * Data window for the interval-dependent blocks (the Recent activity
 * feed).  The Followers growth chart is deliberately NOT scoped by this
 * — it always covers a fixed 30-day snapshot window.
 */
export const OVERVIEW_INTERVALS = [
  { label: "Today", days: 1 },
  { label: "3 days", days: 3 },
  { label: "7 days", days: 7 },
  { label: "15 days", days: 15 },
  { label: "Month", days: 30 },
] as const;

const DEFAULT_DAYS = 30;
const STORAGE_KEY = "github-social-overview-days";

function readStoredDays(): number {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return DEFAULT_DAYS;
    const days = Number(raw);
    return OVERVIEW_INTERVALS.some((o) => o.days === days)
      ? days
      : DEFAULT_DAYS;
  } catch {
    return DEFAULT_DAYS;
  }
}

function persistDays(days: number) {
  try {
    localStorage.setItem(STORAGE_KEY, String(days));
  } catch {
    // Persistence is best-effort (private mode, etc.).
  }
}

export default function OverviewPage() {
  const [stats, setStats] = useState<Stats | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [days, setDays] = useState<number>(readStoredDays);
  const { intervalMs, intervalLabel } = useRefresh();
  // Monotonic sequence: only the *latest* in-flight request may commit its
  // result.  Prevents out-of-order responses when the user quickly switches
  // intervals (or a poll overlaps a switch) from showing stale data for the
  // wrong window.
  const requestSeq = useRef(0);

  const load = async (activeDays: number) => {
    const seq = ++requestSeq.current;
    try {
      const data = await api.stats(activeDays);
      if (seq !== requestSeq.current) return; // superseded — drop
      setStats(data);
      setError(null);
    } catch (e) {
      if (seq !== requestSeq.current) return;
      setError((e as Error).message);
    }
  };

  useEffect(() => {
    load(days);
    // Initial load only — the selected interval is restored from storage
    // via the `days` initialiser (the toggle triggers its own reload).
  }, []);

  // Auto-refresh on the user-selected cadence (default 1 min, 3s–30m slider).
  usePolling(() => load(days), intervalMs);

  const changeInterval = (nextDays: number) => {
    if (nextDays === days) return;
    setDays(nextDays);
    persistDays(nextDays);
    // Force an immediate refresh with the newly selected window.
    load(nextDays);
  };

  if (error && !stats) {
    return (
      <div className="max-w-[1280px] mx-auto p-6">
        <div className="card p-8 text-center text-danger-fg">
          <div className="text-[15px] font-semibold mb-1">API unreachable</div>
          <div className="text-[13px] text-fg-muted">{error}</div>
          <div className="mt-4 text-[13px] text-fg-muted">
            Start the backend:{" "}
            <code className="font-mono text-fg bg-canvas-subtle px-1.5 py-0.5 rounded">
              uvicorn api.app:app --port 8000
            </code>
          </div>
        </div>
      </div>
    );
  }

  if (!stats) {
    return (
      <div className="max-w-[1280px] mx-auto p-6 animate-pulse">
        <div className="h-6 w-48 bg-border-muted rounded mb-4" />
        <div className="grid grid-cols-1 min-[480px]:grid-cols-2 md:grid-cols-3 xl:grid-cols-4 gap-3">
          {Array.from({ length: 7 }).map((_, i) => (
            <div key={i} className="card h-[88px]" />
          ))}
        </div>
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 mt-4">
          <div className="card h-[300px]" />
          <div className="card h-[300px]" />
        </div>
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-4 mt-4">
          <div className="card h-[280px]" />
          <div className="card h-[280px] lg:col-span-2" />
        </div>
      </div>
    );
  }

  const t = stats.totals;
  // Follows and unfollows share ONE combined daily budget (50 actions),
  // so the meter always reflects the sum of both directions.
  const combinedToday = stats.today_follows + stats.today_unfollows;
  const intervalWord = days === 1 ? "today" : `last ${days} days`;

  return (
    <div className="max-w-[1280px] mx-auto p-4 md:p-6 space-y-4">
      {/* Title row */}
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div className="min-w-0">
          <h1 className="text-[20px] font-semibold tracking-tight">Overview</h1>
          <p className="text-[13px] text-fg-muted">
            Growth pipeline for{" "}
            <Link to="/users" className="text-accent hover:underline">
              @{stats.owner ?? "your account"}
            </Link>{" "}
            — showing {intervalWord} · auto-refreshes every {intervalLabel}
          </p>
        </div>

        {/* Data interval toggle — persisted in localStorage */}
        <div
          className="flex items-center rounded-md border border-border bg-canvas p-0.5 gap-0.5 flex-wrap"
          role="group"
          aria-label="Data interval"
        >
          {OVERVIEW_INTERVALS.map((opt) => (
            <button
              key={opt.days}
              type="button"
              onClick={() => changeInterval(opt.days)}
              aria-pressed={opt.days === days}
              className={`px-2.5 py-1 rounded text-[12px] font-medium transition-colors duration-100 select-none cursor-pointer ${
                opt.days === days
                  ? "bg-accent-emphasis text-white"
                  : "text-fg-muted hover:text-fg hover:bg-canvas-subtle"
              }`}
            >
              {opt.label}
            </button>
          ))}
        </div>
      </div>

      {/* KPI cards — fixed pipeline-flow layout (issue #18).
          Top row:    Users discovered → Follows/Unfollows → Following now → Followbacks
          Bottom row: Scored → ML followback candidates → GitHub API requests (4th slot empty) */}
      <div className="grid grid-cols-1 min-[480px]:grid-cols-2 md:grid-cols-3 xl:grid-cols-4 gap-3">
        <StatCard
          label="Users discovered"
          value={t.total}
          icon={<Users size={16} />}
          sub={`${t.queue.toLocaleString()} awaiting processing`}
        />
        {/* Combined card (issue #15): follows in green, unfollows in red,
            black slash between, and the shared daily budget on the sub-line.
            No accent: the value is fully colored by its own spans and the
            budget pressure already reads numerically in the sub-line. */}
        <StatCard
          label="Follows/Unfollows"
          value={
            <span className="inline-flex items-baseline gap-x-1">
              <span className="text-success-fg">{stats.today_follows}</span>
              <span className="text-fg">/</span>
              <span className="text-danger-fg">{stats.today_unfollows}</span>
            </span>
          }
          icon={<TrendingUp size={16} />}
          sub={`${combinedToday}/${stats.daily_limit} combined`}
        />
        {/* Pipeline-state metric (issue #16): how many users are in the
            followed status — NOT how many follow actions were performed
            (that is the Follows/Unfollows card).  Renamed from "Followed"
            so the two cards can't be confused. */}
        <StatCard
          label="Following now"
          value={t.followed}
          icon={<UserCheck size={16} />}
          accent="accent"
          sub={`${t.unfollowed_after_mutual} after mutual`}
          hint={`Users in the followed status in this period · ${t.unfollowed_after_mutual} later unfollowed after a mutual follow`}
        />
        <StatCard
          label="Followbacks"
          value={t.followbacks}
          icon={<HeartHandshake size={16} />}
          accent="success"
          sub={
            t.followed > 0
              ? `${Math.round((t.followbacks / t.followed) * 100)}% conversion`
              : "waiting for follows…"
          }
        />
        <StatCard
          label="Scored"
          value={t.scored}
          icon={<Target size={16} />}
          sub={`${t.scored_positive.toLocaleString()} with score > 0`}
        />
        <StatCard
          label="ML followback candidates"
          value={t.ml_positive}
          icon={<Sparkles size={16} />}
          accent="attention"
          sub="predicted likely to follow back"
        />
        {/* The bold figure always reports the live per-hour request rate and
            its share of the hourly quota — regardless of the selected interval.
            The sub-line then shows the interval-scoped total. */}
        <StatCard
          label="GitHub API requests"
          value={
            <span className="inline-flex items-baseline gap-x-1.5 flex-wrap">
              {stats.github_usage.requests_last_hour.toLocaleString()}
              <span className="text-[13px] font-medium text-fg-muted whitespace-nowrap">
                /hr · {stats.github_usage.percent_last_hour}% of{" "}
                {stats.github_usage.rate_limit.toLocaleString()}/hr limit
              </span>
            </span>
          }
          icon={<Gauge size={16} />}
          accent={
            stats.github_usage.percent_last_hour >= 90
              ? "danger"
              : stats.github_usage.percent_last_hour >= 70
              ? "attention"
              : "default"
          }
          sub={`${(stats.github_usage.requests_in_window ?? 0).toLocaleString()} requests ${intervalWord}`}
          hint={`${stats.github_usage.requests_total.toLocaleString()} requests logged total`}
        />
      </div>

      {/* Followers growth + Pipeline status — one row */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <div className="card p-4 min-w-0">
          <div className="flex items-center justify-between gap-2 mb-2 flex-wrap">
            <div>
              <h2 className="text-[14px] font-semibold">Followers growth</h2>
              <p className="text-[12px] text-fg-muted">
                daily snapshot · followers & following, last 30 days
              </p>
            </div>
            <span className="text-[12px] text-fg-muted shrink-0">
              {stats.followers_history.length === 0
                ? "no snapshots yet"
                : `${stats.followers_history[stats.followers_history.length - 1].followers} followers`}
            </span>
          </div>
          <FollowersChart data={stats.followers_history} />
        </div>

        <div className="card p-4 min-w-0">
          <h2 className="text-[14px] font-semibold mb-1">Pipeline status</h2>
          <p className="text-[12px] text-fg-muted mb-2">
            Users entering each status · {intervalWord}
          </p>
          <StatusBars data={stats.status_distribution} />
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <div className="card p-4 min-w-0">
          <h2 className="text-[14px] font-semibold mb-1">Score distribution</h2>
          <p className="text-[12px] text-fg-muted mb-2">
            Where the pipeline sits
          </p>
          <ScoreBars data={stats.score_buckets} />
        </div>

        {/* Recent activity — lifecycle event stream (no job info here) */}
        <div className="card p-4 min-w-0">
          <div className="flex items-center justify-between mb-2">
            <h2 className="text-[14px] font-semibold">Recent activity</h2>
            <Link to="/manage" className="text-[12px] text-accent hover:underline">
              Manage jobs →
            </Link>
          </div>
          <div className="divide-y divide-border-muted">
            {stats.recent_actions.length === 0 && (
              <div className="py-6 text-center text-[13px] text-fg-subtle">
                No actions in this interval yet — start a{" "}
                <Link to="/manage" className="text-accent hover:underline">
                  job
                </Link>{" "}
                or run{" "}
                <code className="font-mono bg-canvas-subtle px-1 rounded">
                  python main.py --silent
                </code>
              </div>
            )}
            {stats.recent_actions.slice(0, 6).map((a, i) => {
              const meta = actionMeta(a.action);
              return (
                <div
                  key={`${a.created_at}-${i}`}
                  className="py-2 flex items-center gap-2.5 min-w-0"
                >
                  <span
                    className={`badge ${meta.cls} shrink-0`}
                    title={a.action}
                  >
                    {meta.label}
                  </span>
                  <a
                    href={`https://github.com/${a.username}`}
                    target="_blank"
                    rel="noreferrer"
                    className="text-[13px] font-medium text-accent hover:underline truncate min-w-0"
                  >
                    {a.username}
                  </a>
                  <span className="ml-auto text-[12px] text-fg-subtle shrink-0">
                    {timeAgo(a.created_at)}
                  </span>
                </div>
              );
            })}
          </div>
        </div>

        {/* Interactions — people who starred/forked/opened issues or PRs
            on our repositories (issue #23).  Same visual language as the
            Recent activity feed; refreshed by the same polling. */}
        <div className="card p-4 min-w-0">
          <div className="flex items-center justify-between mb-2">
            <h2 className="text-[14px] font-semibold">Interactions</h2>
            <span className="text-[12px] text-fg-subtle">
              stars · forks · issues · PRs
            </span>
          </div>
          <div className="divide-y divide-border-muted">
            {stats.interactions.length === 0 && (
              <div className="py-6 text-center text-[13px] text-fg-subtle">
                No interactions yet — when someone stars, forks or opens an
                issue on your repositories, they appear here.
              </div>
            )}
            {stats.interactions.slice(0, 6).map((it) => {
              const meta = interactionMeta(it.event_type);
              return (
                <div
                  key={it.event_id}
                  className="py-2 flex items-center gap-2.5 min-w-0"
                >
                  <span
                    className={`badge ${meta.cls} shrink-0`}
                    title={it.event_type}
                  >
                    {meta.label}
                  </span>
                  <a
                    href={`https://github.com/${it.username}`}
                    target="_blank"
                    rel="noreferrer"
                    className="text-[13px] font-medium text-accent hover:underline truncate min-w-0"
                  >
                    {it.username}
                  </a>
                  <span className="text-[12px] text-fg-muted truncate min-w-0 hidden sm:inline">
                    {it.repo_full_name?.split("/")[1] ?? ""}
                  </span>
                  <span className="ml-auto text-[12px] text-fg-subtle shrink-0">
                    {timeAgo(it.created_at)}
                  </span>
                </div>
              );
            })}
          </div>
        </div>
      </div>
    </div>
  );
}
