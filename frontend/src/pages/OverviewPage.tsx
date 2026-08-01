import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  Activity,
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
import { JobStatusBadge } from "../components/StatusBadge";
import { usePolling } from "../hooks/usePolling";
import { MODE_LABELS } from "../status";
import { useRefresh } from "../refresh";

export default function OverviewPage() {
  const [stats, setStats] = useState<Stats | null>(null);
  const [error, setError] = useState<string | null>(null);
  const { intervalMs, intervalLabel } = useRefresh();

  const load = async () => {
    try {
      setStats(await api.stats());
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  useEffect(() => {
    load();
  }, []);

  // Auto-refresh on the user-selected cadence (default 1 min, 3s–30m slider).
  usePolling(load, intervalMs);

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
        <div className="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-4 gap-3">
          {Array.from({ length: 8 }).map((_, i) => (
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
  const totalActions = stats.activity.reduce((a, b) => a + b.count, 0);
  const pctLimit = Math.round((stats.today_follows / stats.daily_limit) * 100);

  return (
    <div className="max-w-[1280px] mx-auto p-4 md:p-6 space-y-4">
      {/* Title row */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-[20px] font-semibold tracking-tight">Overview</h1>
          <p className="text-[13px] text-fg-muted">
            Growth pipeline for{" "}
            <Link to="/users" className="text-accent hover:underline">
              @{stats.owner ?? "your account"}
            </Link>{" "}
            — auto-refreshes every {intervalLabel}
          </p>
        </div>
      </div>

      {/* KPI cards */}
      <div className="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-4 gap-3">
        <StatCard
          label="Users discovered"
          value={t.total}
          icon={<Users size={16} />}
          sub={`${t.new} new in queue`}
        />
        <StatCard
          label="Scored"
          value={t.scored}
          icon={<Target size={16} />}
          sub={`${t.total > 0 ? Math.round((t.scored / t.total) * 100) : 0}% of pipeline`}
        />
        <StatCard
          label="Followed"
          value={t.followed}
          icon={<UserCheck size={16} />}
          accent="accent"
          sub={`${t.unfollowed_after_mutual} unfollowed after mutual`}
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
          label="Today's follows"
          value={
            <span>
              {stats.today_follows}
              <span className="text-[14px] text-fg-subtle font-normal">
                {" "}
                / {stats.daily_limit}
              </span>
            </span>
          }
          icon={<TrendingUp size={16} />}
          accent={pctLimit >= 90 ? "danger" : pctLimit >= 70 ? "attention" : "default"}
          sub={stats.followers_count != null ? `${stats.followers_count} followers now` : "no follower snapshot yet"}
        />
        <StatCard
          label="ML followback candidates"
          value={t.ml_positive}
          icon={<Sparkles size={16} />}
          accent="attention"
          sub="predicted likely to follow back"
        />
        <StatCard
          label="Activity (30d)"
          value={totalActions}
          icon={<Activity size={16} />}
          sub="follow actions logged"
        />
        <StatCard
          label="GitHub API requests / hour"
          value={stats.github_usage.requests_last_hour}
          icon={<Gauge size={16} />}
          accent={
            stats.github_usage.percent_last_hour >= 90
              ? "danger"
              : stats.github_usage.percent_last_hour >= 70
              ? "attention"
              : "default"
          }
          sub={`${stats.github_usage.requests_today} today · ${stats.github_usage.percent_last_hour}% of ${stats.github_usage.rate_limit.toLocaleString()}/hr limit`}
          hint={`${stats.github_usage.requests_total.toLocaleString()} requests logged total`}
        />
      </div>

      {/* Followers growth + Pipeline status — one row */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <div className="card p-4">
          <div className="flex items-center justify-between mb-2">
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

        <div className="card p-4">
          <h2 className="text-[14px] font-semibold mb-1">Pipeline status</h2>
          <p className="text-[12px] text-fg-muted mb-2">
            Users by lifecycle status
          </p>
          <StatusBars data={stats.status_distribution} />
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <div className="card p-4">
          <h2 className="text-[14px] font-semibold mb-1">Score distribution</h2>
          <p className="text-[12px] text-fg-muted mb-2">
            Where the pipeline sits
          </p>
          <ScoreBars data={stats.score_buckets} />
        </div>

        {/* Recent actions + jobs */}
        <div className="card p-4 lg:col-span-2">
          <div className="flex items-center justify-between mb-2">
            <h2 className="text-[14px] font-semibold">Recent activity</h2>
            <Link to="/manage" className="text-[12px] text-accent hover:underline">
              Manage jobs →
            </Link>
          </div>
          <div className="divide-y divide-border-muted">
            {stats.recent_actions.length === 0 && (
              <div className="py-6 text-center text-[13px] text-fg-subtle">
                No actions yet — start a{" "}
                <Link to="/manage" className="text-accent hover:underline">
                  job
                </Link>{" "}
                or run{" "}
                <code className="font-mono bg-canvas-subtle px-1 rounded">
                  python main.py --silent
                </code>
              </div>
            )}
            {stats.recent_actions.slice(0, 6).map((a, i) => (
              <div key={i} className="py-2 flex items-center gap-2.5">
                <span className="badge bg-canvas-subtle text-fg-muted border border-border">
                  {a.action}
                </span>
                <a
                  href={`https://github.com/${a.username}`}
                  target="_blank"
                  rel="noreferrer"
                  className="text-[13px] font-medium text-accent hover:underline"
                >
                  {a.username}
                </a>
                <span className="ml-auto text-[12px] text-fg-subtle shrink-0">
                  {timeAgo(a.created_at)}
                </span>
              </div>
            ))}
          </div>

          {stats.jobs.length > 0 && (
            <div className="mt-3 pt-3 border-t border-border">
              <h3 className="text-[12px] font-semibold text-fg-muted mb-1.5 uppercase tracking-wide">
                Latest jobs
              </h3>
              <div className="flex flex-wrap gap-1.5">
                {stats.jobs.slice(0, 6).map((j) => (
                  <Link
                    key={j.id}
                    to="/manage"
                    className="badge bg-canvas-subtle border border-border text-fg-muted hover:border-accent hover:text-accent transition-colors"
                    title={j.error ?? undefined}
                  >
                    #{j.id} {MODE_LABELS[j.mode]?.label ?? j.mode}
                    <JobStatusBadge status={j.status} />
                  </Link>
                ))}
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
