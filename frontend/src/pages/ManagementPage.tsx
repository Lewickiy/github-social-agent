import {useCallback, useEffect, useState} from "react";
import {
    Bot,
    ChevronDown,
    ChevronUp,
    ClipboardList,
    ExternalLink,
    Globe,
    LocateFixed,
    Play,
    RefreshCw,
    Settings2,
    Waypoints,
} from "lucide-react";
import {api, formatDate, timeAgo} from "../api";
import type {Config, DiscoveryState, Job, Settings} from "../types";
import {JobStatusBadge} from "../components/StatusBadge";
import {usePolling} from "../hooks/usePolling";
import {jobDuration, MODE_LABELS, type ModeMeta} from "../status";
import {REFRESH_OPTIONS, useRefresh} from "../refresh";
import {detectSystemTimezone, formatOffset, TIMEZONE_OPTIONS,} from "../timezones";

export default function ManagementPage() {
    const {intervalMs, intervalLabel, optionIndex, setIntervalMs} = useRefresh();
    const [jobs, setJobs] = useState<Job[]>([]);
    const [config, setConfig] = useState<Config | null>(null);
    const [discovery, setDiscovery] = useState<DiscoveryState | null>(null);
    const [error, setError] = useState<string | null>(null);
    const [starting, setStarting] = useState<string | null>(null);
    const [busy, setBusy] = useState(false);
    const [logFor, setLogFor] = useState<number | null>(null);
    const [logText, setLogText] = useState<string | null>(null);
    const [settings, setSettings] = useState<Settings | null>(null);
    const [tzSaving, setTzSaving] = useState(false);
    const [tzNotice, setTzNotice] = useState<string | null>(null);

    const load = useCallback(async () => {
        try {
            const [j, c, d] = await Promise.all([
                api.jobs(),
                api.config(),
                api.discovery(),
            ]);
            setJobs(j.items);
            setConfig(c);
            setDiscovery(d);
            setError(null);
        } catch (e) {
            setError((e as Error).message);
        }
    }, []);

    useEffect(() => {
        load();
    }, [load]);

    // Timezone: load once (not on every poll).  If no timezone is stored
    // yet, auto-detect the browser timezone and save it immediately — that
    // is the "default to UTC until the system zone is available" behaviour.
    useEffect(() => {
        let cancelled = false;
        api
            .settings()
            .then(async (s) => {
                if (cancelled) return;
                if (!s.timezone_set) {
                    const detected = detectSystemTimezone();
                    if (detected) {
                        try {
                            s = await api.saveSettings(detected);
                            if (!cancelled) {
                                setTzNotice(`Auto-detected ${detected} from your system.`);
                            }
                        } catch {
                            // Detection is best-effort — fall back to the UTC default.
                        }
                    }
                }
                if (!cancelled) setSettings(s);
            })
            .catch((e) => setError((e as Error).message));
        return () => {
            cancelled = true;
        };
    }, []);

    const saveTimezone = async (value: string) => {
        if (!value || tzSaving) return;
        setTzSaving(true);
        setTzNotice(null);
        try {
            const saved = await api.saveSettings(value);
            setSettings(saved);
            setTzNotice(
                `Saved — ${saved.timezone.replace(/_/g, " ")} (UTC${formatOffset(
                    saved.utc_offset_minutes
                )}).`
            );
        } catch (e) {
            setError((e as Error).message);
        } finally {
            setTzSaving(false);
        }
    };

    const useSystemTimezone = () => {
        const detected = detectSystemTimezone();
        if (detected) {
            saveTimezone(detected);
        } else {
            setError("Could not detect a timezone from your browser.");
        }
    };

    // Poll fast while a job is running so statuses update live; otherwise
    // settle into the user-selected cadence (default 1 min, 3s–30m slider).
    const running = jobs.filter((j) => j.status === "RUNNING");
    usePolling(load, () =>
        running.length > 0 ? Math.min(8000, intervalMs) : intervalMs
    );

    const startJob = async (mode: string) => {
        setStarting(mode);
        setBusy(true);
        setError(null);
        try {
            await api.startJob(mode);
            await load();
        } catch (e) {
            setError((e as Error).message);
        } finally {
            setStarting(null);
            setBusy(false);
        }
    };

    // Single working mode (silent) is the star of the launcher; the rest
    // are one-off force/backfill modes kept under a collapsible section.
    const mainModes = Object.entries(MODE_LABELS).filter(([, m]) => m.group === "main");
    const forceModes = Object.entries(MODE_LABELS).filter(([, m]) => m.group === "force");

    const modeButton = (mode: string, meta: ModeMeta) => {
        const isRunning = running.some((j) => j.mode === mode);
        return (
            <button
                key={mode}
                disabled={busy}
                onClick={() => startJob(mode)}
                className="w-full flex items-center gap-3 p-2.5 rounded-md border border-border bg-canvas-subtle/50 hover:border-success/50 hover:bg-success-subtle/40 transition-colors group disabled:opacity-60 disabled:cursor-not-allowed text-left"
            >
                <span
                    className={`w-2 h-2 rounded-full shrink-0 ${
                        isRunning ? "bg-accent animate-pulse" : "bg-fg-subtle group-hover:bg-success"
                    }`}
                />
                <span className="flex-1 min-w-0">
                    <span className="block text-[13px] font-medium">
                        {meta.label}
                    </span>
                    <span className="block text-[12px] text-fg-muted">
                        {meta.desc}
                    </span>
                </span>
                {starting === mode ? (
                    <RefreshCw size={15} className="animate-spin text-fg-subtle"/>
                ) : (
                    <Play size={15} className="text-fg-subtle group-hover:text-success"/>
                )}
            </button>
        );
    };

    const toggleLog = async (id: number) => {
        if (logFor === id) {
            setLogFor(null);
            setLogText(null);
            return;
        }
        setLogFor(id);
        setLogText(null);
        try {
            const r = await api.jobLog(id);
            setLogText(r.log);
        } catch (e) {
            setLogText(`(no log available: ${(e as Error).message})`);
        }
    };

    return (
        <div className="max-w-[1280px] mx-auto p-4 md:p-6 space-y-4">
            <div>
                <h1 className="text-[20px] font-semibold tracking-tight">Management</h1>
                <p className="text-[13px] text-fg-muted">
                    Run bot modes, watch job history, and review configuration
                </p>
            </div>

            {error && (
                <div className="card p-3 border-danger/40 text-danger-fg text-[13px]">
                    {error}
                </div>
            )}
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
                {/* Timezone — day boundaries for daily limits, windows, snapshots */}
                <div className="card p-4">
                    <div className="flex items-center gap-2 mb-1">
                        <Globe size={15} className="text-accent"/>
                        <h2 className="text-[14px] font-semibold">Timezone</h2>
                        {settings && (
                            <span className="ml-auto badge bg-accent/10 text-accent border border-accent/30">
              {settings.timezone.replace(/_/g, " ")} (UTC
                                {formatOffset(settings.utc_offset_minutes)})
            </span>
                        )}
                    </div>
                    <p className="text-[12px] text-fg-muted mb-3">
                        Day boundaries — the daily follow-limit reset, Overview windows,
                        activity buckets and the midnight snapshot — roll over at
                        midnight in this timezone. Defaults to UTC until your browser
                        timezone is detected.
                    </p>

                    <div className="flex flex-col sm:flex-row items-stretch sm:items-center gap-2">
                        <select
                            value={settings?.timezone ?? ""}
                            disabled={tzSaving}
                            onChange={(e) => e.target.value && saveTimezone(e.target.value)}
                            className="flex-1 min-w-0 px-3 py-2 rounded-md border border-border bg-canvas-subtle text-[13px] focus:outline-none focus:ring-2 focus:ring-accent/40 disabled:opacity-60"
                            aria-label="Timezone"
                        >
                            {/* The saved zone may come from browser detection and not be
                in the curated list — always render it as an option so the
                select shows the actual value. */}
                            {settings &&
                                !TIMEZONE_OPTIONS.some((o) => o.value === settings.timezone) && (
                                    <option value={settings.timezone}>
                                        {settings.timezone.replace(/_/g, " ")}
                                    </option>
                                )}
                            {TIMEZONE_OPTIONS.map((o) => (
                                <option key={o.value} value={o.value}>
                                    {o.label}
                                </option>
                            ))}
                        </select>
                        <button
                            className="btn shrink-0"
                            disabled={tzSaving}
                            onClick={useSystemTimezone}
                            title="Detect from your browser"
                        >
                            <LocateFixed size={14}/> Use system timezone
                        </button>
                    </div>

                    {tzNotice && (
                        <p className="mt-2 text-[12px] text-success-fg">{tzNotice}</p>
                    )}
                </div>

                {/* Data refresh interval — applied instantly, no confirmation */}
                <div className="card p-4">
                    <div className="flex items-center gap-2 mb-1">
                        <RefreshCw size={15} className="text-accent"/>
                        <h2 className="text-[14px] font-semibold">Data refresh interval</h2>
                        <span className="ml-auto badge bg-accent/10 text-accent border border-accent/30">
            every {intervalLabel}
          </span>
                    </div>
                    <p className="text-[12px] text-fg-muted mb-3">
                        All dashboard pages re-fetch their data on this cadence. Changes
                        apply immediately across Overview, Users and Management.
                    </p>

                    <input
                        type="range"
                        min={0}
                        max={REFRESH_OPTIONS.length - 1}
                        step={1}
                        value={optionIndex}
                        onChange={(e) =>
                            setIntervalMs(REFRESH_OPTIONS[Number(e.target.value)].ms)
                        }
                        className="w-full h-2 rounded-full bg-border-muted accent-accent cursor-pointer"
                        aria-label="Data refresh interval"
                    />

                    <div className="grid grid-cols-10 mt-1.5 text-[11px] text-fg-subtle">
                        {REFRESH_OPTIONS.map((o, i) => (
                            <span
                                key={o.ms}
                                className={`text-center truncate px-0.5 ${
                                    i === optionIndex ? "text-accent font-semibold" : ""
                                }`}
                            >
              {o.label}
            </span>
                        ))}
                    </div>
                </div>
            </div>

            <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
                {/* Job launcher */}
                <div className="card p-4">
                    <div className="flex items-center gap-2 mb-3">
                        <Play size={15} className="text-success-fg"/>
                        <h2 className="text-[14px] font-semibold">Run a job</h2>
                    </div>

                    {running.length > 0 && (
                        <div className="mb-3 p-2.5 rounded-md border border-accent/30 bg-accent/5">
                            <div className="text-[12px] font-medium text-accent mb-1 flex items-center gap-1.5">
                                <RefreshCw size={12} className="animate-spin"/>
                                {running.length} running
                            </div>
                            {running.map((j) => (
                                <div key={j.id} className="text-[12px] text-fg-muted">
                                    #{j.id} {MODE_LABELS[j.mode]?.label ?? j.mode} · pid {j.pid} ·{" "}
                                    {jobDuration(j)}
                                </div>
                            ))}
                        </div>
                    )}

                    <div className="space-y-1.5">
                        {mainModes.map(([mode, meta]) => modeButton(mode, meta))}

                        <details className="group/force mt-2">
                            <summary className="flex items-center justify-between text-[12px] text-fg-subtle hover:text-fg-muted cursor-pointer select-none py-1">
                                <span>Force / backfill modes</span>
                                <ChevronDown
                                    size={14}
                                    className="transition-transform group-open/force:rotate-180"
                                />
                            </summary>
                            <div className="space-y-1.5 mt-1.5">
                                {forceModes.map(([mode, meta]) => modeButton(mode, meta))}
                            </div>
                        </details>
                    </div>

                    <p className="mt-3 text-[12px] text-fg-subtle">
                        Jobs run <code className="font-mono">python main.py &lt;mode&gt;</code> in the
                        background. Output is captured to{" "}
                        <code className="font-mono">logs/jobs/</code>.
                    </p>
                </div>

                {/* Config */}
                <div className="card p-4">
                    <div className="flex items-center gap-2 mb-3">
                        <Settings2 size={15} className="text-fg-muted"/>
                        <h2 className="text-[14px] font-semibold">Bot configuration</h2>
                    </div>

                    {!config && (
                        <div className="text-[13px] text-fg-subtle">Loading…</div>
                    )}
                    {config && (
                        <div className="space-y-1.5 text-[13px]">
                            <Row label="Account" value={`@${config.my_username}`} mono/>
                            <Row
                                label="Daily follow limit"
                                value={String(config.daily_follow_limit)}
                                mono
                            />
                            <Row
                                label="Follow delay"
                                value={`${config.follow_delay}s`}
                                mono
                            />
                            <Row
                                label="Score threshold"
                                value={String(config.score_threshold)}
                                mono
                            />
                            <Row
                                label="Score version"
                                value={String(config.current_score_version)}
                                mono
                            />
                            <Row
                                label="ML enabled"
                                value={config.ml_enabled ? "yes" : "no"}
                            />
                            <Row
                                label="ML retrain"
                                value={`every ${config.ml_train_interval_hours}h`}
                                mono
                            />
                            <Row
                                label="Repo freshness"
                                value={`${config.repo_freshness_days}d`}
                                mono
                            />
                            <Row
                                label="Owner sync"
                                value={`${config.owner_sync_days}d`}
                                mono
                            />
                            <Row
                                label="Score freshness"
                                value={`${config.score_freshness_days}d`}
                                mono
                            />
                            <Row
                                label="Follower scan"
                                value={`${config.follower_scan_days}d`}
                                mono
                            />
                            <Row
                                label="Prioritise small"
                                value={config.prioritize_small ? "yes" : "no"}
                            />
                        </div>
                    )}

                    <p className="mt-3 text-[12px] text-fg-subtle">
                        Values come from <code className="font-mono">config.py</code> /
                        <code className="font-mono">.env</code>.
                    </p>
                </div>

                {/* Job history */}
                <div className="card p-4">
                    <div className="flex items-center gap-2 mb-3">
                        <ClipboardList size={15} className="text-fg-muted"/>
                        <h2 className="text-[14px] font-semibold">Job history</h2>
                    </div>

                    {jobs.length === 0 && (
                        <div className="text-[13px] text-fg-subtle">
                            No jobs yet — start one from the left panel.
                        </div>
                    )}

                    <div className="space-y-1.5 max-h-[420px] overflow-y-auto pr-1">
                        {jobs.map((j) => {
                            const open = logFor === j.id;
                            return (
                                <div
                                    key={j.id}
                                    className="rounded-md border border-border bg-canvas-subtle/40 overflow-hidden"
                                >
                                    <div className="flex items-center gap-2 p-2">
                    <span className="text-[12px] text-fg-subtle font-mono">
                      #{j.id}
                    </span>
                                        <span className="text-[13px] font-medium flex-1 truncate">
                      {MODE_LABELS[j.mode]?.label ?? j.mode}
                    </span>
                                        <span className="text-[12px] text-fg-subtle">
                      {jobDuration(j)}
                    </span>
                                        <JobStatusBadge status={j.status}/>
                                        <button
                                            className="btn !p-1"
                                            title="View log"
                                            onClick={() => toggleLog(j.id)}
                                        >
                                            {open ? (
                                                <ChevronUp size={14}/>
                                            ) : (
                                                <ChevronDown size={14}/>
                                            )}
                                        </button>
                                    </div>
                                    <div className="px-2 pb-1.5 text-[11px] text-fg-subtle flex justify-between">
                    <span>
                      started {timeAgo(j.started_at)} ·{" "}
                        {j.finished_at
                            ? `finished ${formatDate(j.finished_at)}`
                            : "not finished"}
                    </span>
                                        {j.exit_code != null && (
                                            <span className="font-mono">exit {j.exit_code}</span>
                                        )}
                                    </div>
                                    {j.error && (
                                        <div
                                            className="mx-2 mb-2 p-2 rounded bg-danger-subtle text-danger-fg text-[11px] font-mono max-h-[120px] overflow-auto whitespace-pre-wrap">
                                            {j.error}
                                        </div>
                                    )}
                                    {open && (
                                        <div
                                            className="mx-2 mb-2 p-2 rounded bg-canvas text-fg-muted text-[11px] font-mono max-h-[180px] overflow-auto whitespace-pre-wrap border border-border">
                                            {logText ?? "Loading log…"}
                                        </div>
                                    )}
                                </div>
                            );
                        })}
                    </div>
                </div>
            </div>

            {/* Graph discovery worker activity */}
            <div className="card p-4">
                <div className="flex items-center gap-2 mb-1">
                    <Waypoints size={15} className="text-accent"/>
                    <h2 className="text-[14px] font-semibold">Graph discovery worker</h2>
                    {discovery && (
                        <span
                            className={`ml-auto badge border ${
                                discovery.enabled
                                    ? "bg-success-subtle text-success-fg border-success/30"
                                    : "bg-canvas-subtle text-fg-muted border-border"
                            }`}
                        >
                            {discovery.enabled ? "Enabled" : "Disabled"}
                        </span>
                    )}
                </div>
                <p className="text-[12px] text-fg-muted mb-3">
                    Calm background worker that walks the follower graph at a constant
                    rate to grow the network — one bounded pass per hour window, so it
                    never interferes with silent-mode processing.
                </p>

                {!discovery ? (
                    <div className="text-[13px] text-fg-subtle">Loading…</div>
                ) : !discovery.enabled ? (
                    <div className="text-[13px] text-fg-subtle">
                        Worker is disabled — set{" "}
                        <code className="font-mono">DISCOVERY_WORKER_ENABLED=True</code> in{" "}
                        <code className="font-mono">core/config.py</code> to grow the
                        network automatically.
                    </div>
                ) : !discovery.last_run ? (
                    <div className="text-[13px] text-fg-subtle">
                        No passes recorded yet — the worker records one row per pass after
                        the first hour window.
                    </div>
                ) : (
                    <>
                        {/* Hourly budget usage of the last pass */}
                        <div className="mb-3">
                            <div className="flex items-center justify-between text-[12px] mb-1">
                                <span className="text-fg-muted">
                                    Hourly budget used (last pass)
                                </span>
                                <span className="font-mono">
                                    {discovery.last_run.requests ?? 0} /{" "}
                                    {discovery.rate_limit_per_hour} req ·{" "}
                                    <span className="text-fg-subtle">
                                        {discovery.budget_percent}%
                                    </span>
                                </span>
                            </div>
                            <div className="h-2 rounded-full bg-border-muted overflow-hidden">
                                <div
                                    className={`h-full rounded-full transition-all ${
                                        discovery.budget_percent > 90
                                            ? "bg-danger"
                                            : discovery.budget_percent > 70
                                              ? "bg-attention"
                                              : "bg-success"
                                    }`}
                                    style={{
                                        width: `${Math.min(100, discovery.budget_percent)}%`,
                                    }}
                                />
                            </div>
                        </div>

                        {/* Last pass details */}
                        <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 mb-3">
                            <MiniStat
                                label="Finished"
                                value={timeAgo(discovery.last_run.finished_at)}
                            />
                            <MiniStat
                                label="Users walked"
                                value={String(discovery.last_run.users_walked ?? 0)}
                            />
                            <MiniStat
                                label="New users"
                                value={String(discovery.last_run.new_users ?? 0)}
                            />
                            <MiniStat
                                label="Duration"
                                value={fmtDuration(discovery.last_run.duration_seconds)}
                            />
                        </div>

                        {/* Pass history */}
                        {discovery.history.length > 1 && (
                            <details className="group/hist">
                                <summary className="flex items-center justify-between text-[12px] text-fg-subtle hover:text-fg-muted cursor-pointer select-none py-1">
                                    <span>Pass history ({discovery.history.length})</span>
                                    <ChevronDown
                                        size={14}
                                        className="transition-transform group-open/hist:rotate-180"
                                    />
                                </summary>
                                <div className="space-y-1 mt-1.5 max-h-[180px] overflow-y-auto pr-1">
                                    {discovery.history.map((r) => (
                                        <div
                                            key={r.id}
                                            className="flex items-center justify-between gap-2 text-[11px] text-fg-muted py-1 border-b border-border-muted/60 last:border-0"
                                        >
                                            <span>{timeAgo(r.finished_at)}</span>
                                            <span className="font-mono">
                                                {r.users_walked ?? 0} walked ·{" "}
                                                {r.new_users ?? 0} new · {r.requests ?? 0} req
                                            </span>
                                        </div>
                                    ))}
                                </div>
                            </details>
                        )}
                    </>
                )}
            </div>

            {/* Quick commands hint */}
            <div className="card p-4">
                <div className="flex items-center gap-2 mb-2">
                    <Bot size={15} className="text-fg-muted"/>
                    <h2 className="text-[14px] font-semibold">CLI equivalents</h2>
                    <a
                        href="https://github.com/Lewickiy/github-follow-master"
                        target="_blank"
                        rel="noreferrer"
                        className="ml-auto text-[12px] text-accent hover:underline inline-flex items-center gap-1"
                    >
                        docs <ExternalLink size={12}/>
                    </a>
                </div>
                <div className="grid grid-cols-1 md:grid-cols-2 gap-2 text-[12px]">
                    <CodeBox code="python main.py --silent" desc="Stealth collect + score + follow"/>
                    <CodeBox code="python main.py --collect" desc="Discover users + fetch repos"/>
                    <CodeBox code="python main.py --score" desc="Score / re-score users"/>
                    <CodeBox code="python main.py --follow" desc="Follow top-scored users"/>
                </div>
            </div>
        </div>
    );
}

function MiniStat({label, value}: { label: string; value: string }) {
    return (
        <div className="rounded-md border border-border bg-canvas-subtle/50 px-2.5 py-2">
            <div className="text-[11px] text-fg-subtle">{label}</div>
            <div className="text-[13px] font-medium truncate">{value}</div>
        </div>
    );
}

function fmtDuration(seconds: number | null): string {
    if (seconds == null) return "—";
    if (seconds < 60) return `${Math.round(seconds)}s`;
    const m = Math.floor(seconds / 60);
    if (m < 60) return `${m}m ${Math.round(seconds % 60)}s`;
    return `${Math.floor(m / 60)}h ${m % 60}m`;
}

function Row({label, value, mono}: { label: string; value: string; mono?: boolean }) {
    return (
        <div className="flex items-center justify-between py-1 border-b border-border-muted/60 last:border-0">
            <span className="text-fg-muted">{label}</span>
            <span className={`font-medium ${mono ? "font-mono text-[12px]" : ""}`}>
        {value}
      </span>
        </div>
    );
}

function CodeBox({code, desc}: { code: string; desc: string }) {
    return (
        <div className="p-2.5 rounded-md border border-border bg-canvas-subtle/50">
            <code className="font-mono text-[12px] text-fg">{code}</code>
            <div className="text-[11px] text-fg-subtle mt-0.5">{desc}</div>
        </div>
    );
}
