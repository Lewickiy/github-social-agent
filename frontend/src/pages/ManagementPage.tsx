import {useCallback, useEffect, useRef, useState} from "react";
import {
    Activity,
    Bot,
    ChevronDown,
    ExternalLink,
    Globe,
    History,
    LocateFixed,
    RefreshCw,
    Settings2,
    Waypoints,
} from "lucide-react";
import {api, formatDate, timeAgo} from "../api";
import type {Config, DiscoveryState, Settings, WorkerState, WorkerStatus} from "../types";
import {usePolling} from "../hooks/usePolling";
import {REFRESH_OPTIONS, useRefresh} from "../refresh";
import {detectSystemTimezone, formatOffset, TIMEZONE_OPTIONS,} from "../timezones";

export default function ManagementPage() {
    const {intervalMs, intervalLabel, optionIndex, setIntervalMs} = useRefresh();
    const [workers, setWorkers] = useState<WorkerStatus[]>([]);
    const [config, setConfig] = useState<Config | null>(null);
    const [discovery, setDiscovery] = useState<DiscoveryState | null>(null);
    const [error, setError] = useState<string | null>(null);
    const [toggling, setToggling] = useState<string | null>(null);
    const [settings, setSettings] = useState<Settings | null>(null);
    const [tzSaving, setTzSaving] = useState(false);
    const [tzNotice, setTzNotice] = useState<string | null>(null);
    // ML follow gate — local drafts so the slider stays smooth while
    // dragging (the server value only catches up after the PUT + poll).
    const [mlGateOn, setMlGateOn] = useState(true);
    const [mlThreshold, setMlThreshold] = useState(0.5);
    const [mlSaving, setMlSaving] = useState(false);
    const [mlNotice, setMlNotice] = useState<string | null>(null);
    // Server rejection of an enable attempt (e.g. model trend "Too early") —
    // shown right under the switch so the reason is impossible to miss.
    const [mlGateError, setMlGateError] = useState<string | null>(null);
    const mlSaveTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

    useEffect(() => {
        if (!config) return;
        setMlGateOn(config.ml_follow_gate_enabled);
        setMlThreshold(config.ml_follow_threshold);
    }, [config]);

    useEffect(() => {
        return () => {
            if (mlSaveTimer.current) clearTimeout(mlSaveTimer.current);
        };
    }, []);

    const load = useCallback(async () => {
        try {
            const [w, c, d] = await Promise.all([
                api.workers(),
                api.config(),
                api.discovery(),
            ]);
            setWorkers(w.items);
            setConfig(c);
            setDiscovery(d);
            setError(null);
        } catch (e) {
            setError((e as Error).message);
        }
    }, []);

    const saveMLFollow = useCallback(
        async (cfg: { enabled?: boolean; threshold?: number }) => {
            setMlSaving(true);
            setMlNotice(null);
            setMlGateError(null);
            try {
                await api.saveMLFollowConfig(cfg);
                await load();
                if (cfg.threshold !== undefined) {
                    setMlNotice(
                        `Threshold ${cfg.threshold.toFixed(2)} saved — predictions are being recomputed in the background.`
                    );
                } else if (cfg.enabled !== undefined) {
                    setMlNotice(
                        `ML follow gate ${cfg.enabled ? "enabled" : "disabled"} — the FollowWorker picks it up on its next cycle.`
                    );
                }
            } catch (e) {
                const message = (e as Error).message;
                setError(message);
                if (cfg.enabled !== undefined) {
                    // The server rejected the change (e.g. the gate cannot
                    // be enabled while the model trend is "Too early") —
                    // snap the optimistic switch back to the server's value
                    // and surface the reason right under the switch.
                    setMlGateOn(config?.ml_follow_gate_enabled ?? false);
                    setMlGateError(message);
                }
            } finally {
                setMlSaving(false);
            }
        },
        [load, config]
    );

    const onMLThresholdChange = (value: number) => {
        setMlThreshold(value);
        // Debounce the PUT — dragging fires many onChange events, and the
        // API only triggers the (seconds-long) prediction recompute when
        // the stored value actually changes.
        if (mlSaveTimer.current) clearTimeout(mlSaveTimer.current);
        mlSaveTimer.current = setTimeout(() => {
            saveMLFollow({threshold: value});
        }, 400);
    };

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

    // Poll fast while any worker is running so toggles/statuses update
    // live; otherwise settle into the user-selected cadence.
    const runningWorkers = workers.filter((w) => w.running);
    usePolling(load, () =>
        runningWorkers.length > 0 ? Math.min(10000, intervalMs) : intervalMs
    );

    const toggleWorker = async (w: WorkerStatus, enabled: boolean) => {
        setToggling(w.key);
        setError(null);
        try {
            await api.setWorkerEnabled(w.key, enabled);
            await load();
        } catch (e) {
            setError((e as Error).message);
        } finally {
            setToggling(null);
        }
    };

    return (
        <div className="max-w-[1280px] mx-auto p-4 md:p-6 space-y-4">
            <div>
                <h1 className="text-[20px] font-semibold tracking-tight">Management</h1>
                <p className="text-[13px] text-fg-muted">
                    Pause/resume background workers and review configuration
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
                {/* Worker toggles */}
                <div className="card p-4">
                    <div className="flex items-center gap-2 mb-1">
                        <Activity size={15} className="text-success-fg"/>
                        <h2 className="text-[14px] font-semibold">Workers</h2>
                        <span className="ml-auto badge bg-success-subtle text-success-fg border border-success/30">
                            {workers.filter((w) => w.enabled).length}/{workers.length} on
                        </span>
                    </div>
                    <p className="text-[12px] text-fg-muted mb-3">
                        Background daemon threads. Toggle a worker off to pause it and on
                        to resume — the change applies on the worker's next cycle, no
                        restart needed. Toggle states are persisted in the database and
                        survive container restarts.
                    </p>

                    <div className="space-y-1.5">
                        {workers.map((w) => (
                            <div
                                key={w.key}
                                className="flex items-center gap-3 p-2.5 rounded-md border border-border bg-canvas-subtle/50 transition-colors group"
                            >
                                <span
                                    className={`w-2 h-2 rounded-full shrink-0 ${stateDot(w.state)}`}
                                    title={w.state}
                                />
                                <span className="flex-1 min-w-0">
                                    <span className="block text-[13px] font-medium">
                                        {w.label}
                                    </span>
                                    <span className="block text-[12px] text-fg-muted leading-snug">
                                        {w.description}
                                    </span>
                                </span>
                                <Switch
                                    checked={w.enabled}
                                    disabled={toggling !== null}
                                    label={`${w.enabled ? "Pause" : "Resume"} ${w.label} worker`}
                                    onChange={(enabled) => toggleWorker(w, enabled)}
                                />
                            </div>
                        ))}
                    </div>
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
                                value={`${String(config.daily_follow_limit)} (follows + unfollows)`}
                                mono
                            />
                            <Row
                                label="Unfollow after"
                                value={`${config.unfollow_after_days}d no interaction`}
                                mono
                            />
                            <Row
                                label="Follow interval"
                                value={`${Math.round(
                                    config.follow_interval_min_seconds / 60
                                )}–${Math.round(
                                    config.follow_interval_max_seconds / 60
                                )}m`}
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

                    {/* ML follow gate — the "strictness" dial */}
                    {config && (
                        <div className="mt-3 pt-3 border-t border-border-muted/60">
                            <div className="flex items-center gap-2">
                                <span className="text-[13px] font-medium">
                                    ML follow gate
                                </span>
                                <Switch
                                    checked={mlGateOn}
                                    disabled={mlSaving}
                                    label="Toggle ML follow gate"
                                    onChange={(v) => {
                                        setMlGateOn(v);
                                        saveMLFollow({enabled: v});
                                    }}
                                />
                                <span
                                    className={`ml-auto badge border ${
                                        mlGateOn
                                            ? "bg-success-subtle text-success-fg border-success/30"
                                            : "bg-canvas-subtle text-fg-muted border-border"
                                    }`}
                                >
                                    {mlGateOn ? "On" : "Off"}
                                </span>
                            </div>
                            {mlGateError && (
                                <p className="mt-2 text-[11px] text-danger-fg bg-danger-subtle border border-danger/40 rounded-md px-2 py-1.5 leading-snug">
                                    {mlGateError}
                                </p>
                            )}
                            <p className="text-[12px] text-fg-muted mt-1.5 leading-snug">
                                When on, the FollowWorker subscribes only to candidates
                                whose ML followback confidence is at or above the
                                threshold — a second opinion on top of the score.
                                Higher = fewer, more selective follows (and a better
                                follow-back rate).
                            </p>
                            <div className="flex items-center gap-3 mt-2.5">
                                <input
                                    type="range"
                                    min={0.1}
                                    max={0.9}
                                    step={0.05}
                                    value={mlThreshold}
                                    disabled={!mlGateOn || mlSaving}
                                    onChange={(e) =>
                                        onMLThresholdChange(Number(e.target.value))
                                    }
                                    className="flex-1 h-2 rounded-full bg-border-muted accent-accent cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed"
                                    aria-label="ML follow threshold"
                                />
                                <span className="font-mono text-[13px] w-12 text-right">
                                    {mlThreshold.toFixed(2)}
                                </span>
                            </div>
                            <div className="flex justify-between text-[10px] text-fg-subtle mt-0.5">
                                <span>0.10 permissive</span>
                                <span>0.50 current rule</span>
                                <span>0.90 strict</span>
                            </div>
                            {mlNotice && (
                                <p className="mt-1.5 text-[11px] text-success-fg">
                                    {mlNotice}
                                </p>
                            )}
                        </div>
                    )}

                    <p className="mt-3 text-[12px] text-fg-subtle">
                        Static values come from <code className="font-mono">config.py</code> /
                        <code className="font-mono">.env</code>; the ML gate is stored in the
                        database and applies without a restart.
                    </p>
                </div>

                {/* Worker activity — lifecycle from worker_status */}
                <div className="card p-4">
                    <div className="flex items-center gap-2 mb-1">
                        <History size={15} className="text-fg-muted"/>
                        <h2 className="text-[14px] font-semibold">Worker activity</h2>
                    </div>
                    <p className="text-[12px] text-fg-muted mb-3">
                        Per-worker lifecycle: when each worker started, stopped, last did
                        something, and last hit an error.
                    </p>

                    {workers.length === 0 && (
                        <div className="text-[13px] text-fg-subtle">
                            No worker data yet — status appears as soon as the bot
                            starts its threads.
                        </div>
                    )}

                    <div className="space-y-1.5 max-h-[420px] overflow-y-auto pr-1">
                        {workers.map((w) => (
                            <div
                                key={w.key}
                                className="rounded-md border border-border bg-canvas-subtle/40 overflow-hidden"
                            >
                                <div className="flex items-center gap-2 p-2">
                                    <span
                                        className={`w-2 h-2 rounded-full shrink-0 ${stateDot(w.state)}`}
                                        title={w.state}
                                    />
                                    <span className="text-[13px] font-medium flex-1 truncate">
                                        {w.label}
                                    </span>
                                    <WorkerStateBadge state={w.state}/>
                                </div>
                                <div className="grid grid-cols-2 gap-x-3 gap-y-1 px-2 pb-2 text-[11px]">
                                    <WorkerStat label="Started" value={formatDate(w.started_at)}/>
                                    <WorkerStat label="Stopped" value={formatDate(w.stopped_at)}/>
                                    <WorkerStat
                                        label="Last action"
                                        value={w.last_action_at ? timeAgo(w.last_action_at) : "—"}
                                    />
                                    <WorkerStat
                                        label="Last error"
                                        value={w.last_error_at ? timeAgo(w.last_error_at) : "—"}
                                    />
                                </div>
                                {w.last_error && (
                                    <div className="mx-2 mb-2 p-2 rounded bg-danger-subtle text-danger-fg text-[11px] font-mono max-h-[80px] overflow-auto whitespace-pre-wrap">
                                        {w.last_error}
                                    </div>
                                )}
                            </div>
                        ))}
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
                    rate to grow the network — one bounded pass at a time, so it
                    never interferes with silent-mode processing. Its pause/resume
                    toggle lives in the Workers panel above.
                </p>

                {!discovery ? (
                    <div className="text-[13px] text-fg-subtle">Loading…</div>
                ) : !discovery.enabled ? (
                    <div className="text-[13px] text-fg-subtle">
                        Worker is paused — flip its toggle in the Workers panel above to
                        resume network growth.
                    </div>
                ) : !discovery.last_run ? (
                    <div className="text-[13px] text-fg-subtle">
                        No passes recorded yet — the worker records one row per pass after
                        the first hour window.
                    </div>
                ) : (
                    <>
                        {/* Request rate of the last pass vs the hourly budget */}
                        <div className="mb-3">
                            <div className="flex items-center justify-between text-[12px] mb-1">
                                <span className="text-fg-muted">
                                    Request rate vs hourly budget (last pass)
                                </span>
                                <span className="font-mono">
                                    {discovery.requests_per_hour != null
                                        ? discovery.requests_per_hour
                                        : "—"}{" "}
                                    / {discovery.rate_limit_per_hour} req/h ·{" "}
                                    <span className="text-fg-subtle">
                                        {discovery.budget_percent}%
                                    </span>
                                </span>
                            </div>
                            <div className="h-2 rounded-full bg-border-muted overflow-hidden">
                                <div
                                    className={`h-full rounded-full transition-all ${
                                        discovery.budget_percent > 100
                                            ? "bg-danger"
                                            : discovery.budget_percent > 85
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
                        href="https://github.com/Lewickiy/github-social-agent"
                        target="_blank"
                        rel="noreferrer"
                        className="ml-auto text-[12px] text-accent hover:underline inline-flex items-center gap-1"
                    >
                        docs <ExternalLink size={12}/>
                    </a>
                </div>
                <div className="grid grid-cols-1 md:grid-cols-2 gap-2 text-[12px]">
                    <CodeBox code="python main.py --silent" desc="Stealth collect + score (follows via FollowWorker)"/>
                    <CodeBox code="python main.py --collect" desc="Discover users + fetch repos"/>
                    <CodeBox code="python main.py --score" desc="Score / re-score users"/>
                </div>
            </div>
        </div>
    );
}

function stateDot(state: WorkerState): string {
    switch (state) {
        case "running":
            return "bg-success animate-pulse";
        case "paused":
            return "bg-attention";
        case "stopped":
            return "bg-fg-subtle";
        default:
            return "bg-danger";
    }
}

function WorkerStateBadge({state}: { state: WorkerState }) {
    switch (state) {
        case "running":
            return (
                <span className="badge bg-success-subtle text-success-fg border border-success/30">
                    <span className="w-2 h-2 rounded-full bg-success animate-pulse"/>
                    Running
                </span>
            );
        case "paused":
            return (
                <span className="badge bg-attention-subtle text-attention border border-attention/30">
                    <span className="w-2 h-2 rounded-full bg-attention"/>
                    Paused
                </span>
            );
        case "stopped":
            return (
                <span className="badge bg-canvas-subtle text-fg-muted border border-border">
                    <span className="w-2 h-2 rounded-full bg-fg-subtle"/>
                    Stopped
                </span>
            );
        default:
            return (
                <span className="badge bg-canvas-subtle text-fg-muted border border-border">
                    <span className="w-2 h-2 rounded-full bg-danger"/>
                    Offline
                </span>
            );
    }
}

function Switch({
    checked,
    disabled,
    label,
    onChange,
}: {
    checked: boolean;
    disabled?: boolean;
    label: string;
    onChange: (checked: boolean) => void;
}) {
    return (
        <button
            type="button"
            role="switch"
            aria-checked={checked}
            aria-label={label}
            title={label}
            disabled={disabled}
            onClick={() => onChange(!checked)}
            className={`relative inline-flex h-[18px] w-8 shrink-0 items-center rounded-full border transition-colors duration-150 focus:outline-none focus:ring-2 focus:ring-accent/40 disabled:opacity-50 disabled:cursor-not-allowed ${
                checked ? "bg-success border-success" : "bg-border-muted border-border"
            }`}
        >
            <span
                className={`inline-block h-3.5 w-3.5 rounded-full bg-white shadow-sm transition-transform duration-150 ${
                    checked ? "translate-x-[15px]" : "translate-x-[1px]"
                }`}
            />
        </button>
    );
}

function WorkerStat({label, value}: { label: string; value: string }) {
    return (
        <>
            <span className="text-fg-subtle">{label}</span>
            <span className="text-fg-muted font-mono text-right truncate" title={value}>
        {value}
      </span>
        </>
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
