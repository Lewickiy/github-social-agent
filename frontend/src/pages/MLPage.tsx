import { useCallback, useEffect, useState } from "react";
import {
  BrainCircuit,
  Database,
  FlaskConical,
  Gauge,
  Sparkles,
  TrendingUp,
} from "lucide-react";
import {
  CartesianGrid,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { api, formatDate, timeAgo } from "../api";
import type { MLState, MLTrainingRun } from "../types";
import StatCard from "../components/StatCard";
import { usePolling } from "../hooks/usePolling";
import { useRefresh } from "../refresh";

/** Ratios in the API are 0..1; AUC/precision/recall are 0..1 too. */
const pct = (v: number | null | undefined): string =>
  v == null ? "—" : `${Math.round(v * 100)}%`;

const auc = (v: number | null | undefined): string =>
  v == null ? "—" : v.toFixed(3);

export default function MLPage() {
  const { intervalMs, intervalLabel } = useRefresh();
  const [state, setState] = useState<MLState | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setState(await api.ml());
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  usePolling(load, intervalMs);

  if (error && !state) {
    return (
      <div className="max-w-[1280px] mx-auto p-6">
        <div className="card p-8 text-center text-danger-fg">
          <div className="text-[15px] font-semibold mb-1">API unreachable</div>
          <div className="text-[13px] text-fg-muted">{error}</div>
        </div>
      </div>
    );
  }

  if (!state) {
    return (
      <div className="max-w-[1280px] mx-auto p-6 animate-pulse">
        <div className="h-6 w-40 bg-border-muted rounded mb-4" />
        <div className="grid grid-cols-1 min-[480px]:grid-cols-2 md:grid-cols-4 gap-3">
          {Array.from({ length: 4 }).map((_, i) => (
            <div key={i} className="card h-[88px]" />
          ))}
        </div>
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 mt-4">
          <div className="card h-[300px]" />
          <div className="card h-[300px]" />
        </div>
        <div className="card h-[280px] mt-4" />
      </div>
    );
  }

  const m = state.current_model;
  const d = state.dataset;
  const currentVersion = m?.version;
  const chartData = [...state.history]
    .reverse()
    .map((r) => ({
      version: `v${r.version}`,
      cvAuc: r.cv_auc,
      valAcc: r.val_accuracy,
    }))
    .filter((p) => p.cvAuc != null || p.valAcc != null);

  // Y-axis domain derived from the data (with the 0.5 random baseline in
  // view and a small pad) so extreme retrains never get clipped.
  const chartValues = chartData
    .flatMap((p) => [p.cvAuc, p.valAcc])
    .filter((v): v is number => v != null);
  const dataMin = chartValues.length ? Math.min(...chartValues) : 0.5;
  const dataMax = chartValues.length ? Math.max(...chartValues) : 0.5;
  const yDomain: [number, number] = [
    Math.min(0.5, dataMin - 0.05),
    Math.max(0.55, dataMax + 0.05),
  ];

  const hasModel = m != null;

  return (
    <div className="max-w-[1280px] mx-auto p-4 md:p-6 space-y-4">
      {/* Title row */}
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div className="min-w-0">
          <h1 className="text-[20px] font-semibold tracking-tight flex items-center gap-2">
            <BrainCircuit size={19} className="text-accent" />
            ML
          </h1>
          <p className="text-[13px] text-fg-muted">
            Followback prediction model — dataset, quality &amp; retrain
            history · auto-refreshes every {intervalLabel}
          </p>
        </div>
        {m && (
          <span className="badge bg-accent/10 text-accent border border-accent/30 text-[13px]">
            current model v{m.version}
          </span>
        )}
      </div>

      {error && (
        <div className="card p-3 border-danger/40 text-danger-fg text-[13px]">
          {error}
        </div>
      )}

      {/* KPI cards */}
      <div className="grid grid-cols-1 min-[480px]:grid-cols-2 md:grid-cols-4 gap-3">
        <StatCard
          label="Predictions computed"
          value={d.with_prediction.toLocaleString()}
          icon={<Database size={16} />}
          sub={`of ${d.total_users.toLocaleString()} users tracked`}
        />
        <StatCard
          label="Predicted followback"
          value={d.pred_1.toLocaleString()}
          icon={<Sparkles size={16} />}
          accent="accent"
          sub={`${d.positive_share_pct}% of predictions`}
        />
        <StatCard
          label="Predicted no followback"
          value={d.pred_0.toLocaleString()}
          icon={<FlaskConical size={16} />}
          sub="rest of the prediction pool"
        />
        <StatCard
          label="Training samples"
          value={d.training_samples}
          icon={<Gauge size={16} />}
          accent={d.training_samples > 0 ? "success" : "default"}
          sub={
            d.training_samples > 0
              ? `${d.training_positives} positive · ${d.training_negatives} negative`
              : "waiting for followback data…"
          }
        />
      </div>

      {!hasModel && state.history.length === 0 ? (
        <div className="card p-10 text-center">
          <BrainCircuit size={28} className="mx-auto text-fg-subtle mb-3" />
          <div className="text-[15px] font-semibold mb-1">
            No model trained yet
          </div>
          <p className="text-[13px] text-fg-muted max-w-md mx-auto">
            The trainer needs enough labeled users before its first run.
            Once trained, this page shows the model state, dataset size and
            retrain history.
          </p>
        </div>
      ) : (
        <>
          {/* Current model */}
          <div className="card p-4">
            <div className="flex items-center gap-2 mb-3">
              <BrainCircuit size={15} className="text-accent" />
              <h2 className="text-[14px] font-semibold">Current model</h2>
              <span className="ml-auto text-[12px] text-fg-subtle">
                {m ? (
                  <>
                    trained {timeAgo(m.trained_at)} ·{" "}
                    <code className="font-mono">{m.device ?? "?"}</code>
                  </>
                ) : (
                  "no model deployed"
                )}
              </span>
            </div>

            {!m ? (
              <div className="text-[13px] text-fg-subtle">
                Predictions can't be computed until the first model is
                trained — check the Management page or bot logs.
              </div>
            ) : (
              <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-4 gap-x-6 gap-y-1">
                <Metric label="Dataset" value={`${m.num_samples} users`} sub={`${m.num_positives} pos · ${m.num_negatives} neg`} />
                <Metric label="Features" value={String(m.input_dim)} sub={`top ${m.top_languages.length} langs · ${m.top_topics.length} topics`} />
                <Metric label="CV AUC (5-fold)" value={auc(m.cv_auc)} sub={`CV acc ${pct(m.cv_accuracy)}`} highlight={m.cv_auc != null && m.cv_auc > 0.5} />
                <Metric label="CV predict-1 ratio" value={pct(m.cv_pred1_ratio)} sub="honest discrimination check" />
                <Metric label="Hold-out val accuracy" value={pct(m.val_accuracy)} sub={`AUC ${auc(m.val_auc)} · loss ${m.val_loss ?? "—"}`} />
                <Metric label="Hold-out precision" value={pct(m.val_precision)} sub={`recall ${pct(m.val_recall)}`} />
                <Metric label="Early stopping" value={m.early_stopped ? `yes · epoch ${m.epochs}` : `no · ${m.epochs} epochs`} sub={m.training_time_seconds != null ? `${m.training_time_seconds}s training` : "training time unknown"} />
                <Metric label="Label scheme" value={m.label_scheme ? "followback 7d+" : "—"} sub="1 = followed back · 0 = no followback" />
              </div>
            )}
          </div>

          {/* CV AUC evolution chart */}
          <div className="card p-4">
            <div className="flex items-center justify-between gap-2 mb-2 flex-wrap">
              <div>
                <h2 className="text-[14px] font-semibold">
                  CV AUC across retrains
                </h2>
                <p className="text-[12px] text-fg-muted">
                  5-fold cross-validation mean — the honest quality estimate.
                  The dashed line is the random baseline (0.5).
                </p>
              </div>
              <TrendingUp size={15} className="text-fg-subtle" />
            </div>
            {chartData.length === 0 ? (
              <div className="h-[220px] w-full flex items-center justify-center text-[13px] text-fg-subtle">
                No CV metrics recorded yet.
              </div>
            ) : (
              <div className="h-[220px] w-full">
                <ResponsiveContainer width="100%" height="100%">
                  <LineChart
                    data={chartData}
                    margin={{ top: 8, right: 8, bottom: 0, left: -12 }}
                  >
                    <CartesianGrid
                      strokeDasharray="3 3"
                      stroke="#d0d7de"
                      vertical={false}
                    />
                    <XAxis
                      dataKey="version"
                      tick={{ fontSize: 11, fill: "#656d76" }}
                      tickLine={false}
                      axisLine={{ stroke: "#d0d7de" }}
                      minTickGap={16}
                    />
                    <YAxis
                      domain={yDomain}
                      tick={{ fontSize: 11, fill: "#656d76" }}
                      tickLine={false}
                      axisLine={false}
                      tickFormatter={(v: number) => v.toFixed(2)}
                    />
                    <Tooltip
                      contentStyle={{
                        fontSize: 12,
                        borderRadius: 6,
                        border: "1px solid #d0d7de",
                        boxShadow: "0 8px 24px rgba(66,74,83,0.12)",
                      }}
                    />
                    <ReferenceLine
                      y={0.5}
                      stroke="#afb8c1"
                      strokeDasharray="4 4"
                    />
                    <Line
                      type="monotone"
                      dataKey="cvAuc"
                      stroke="#0969da"
                      strokeWidth={2}
                      dot={{ r: 3 }}
                      connectNulls
                      name="CV AUC"
                    />
                    <Line
                      type="monotone"
                      dataKey="valAcc"
                      stroke="#1a7f37"
                      strokeWidth={2}
                      strokeDasharray="5 3"
                      dot={{ r: 3 }}
                      connectNulls
                      name="Val accuracy"
                    />
                  </LineChart>
                </ResponsiveContainer>
              </div>
            )}
          </div>

          {/* Training-run history */}
          <div className="card p-4">
            <div className="flex items-center gap-2 mb-3">
              <FlaskConical size={15} className="text-fg-muted" />
              <h2 className="text-[14px] font-semibold">
                Training-run history
              </h2>
              <span className="ml-auto text-[12px] text-fg-subtle">
                {state.history.length} run
                {state.history.length === 1 ? "" : "s"} recorded
              </span>
            </div>

            {state.history.length === 0 ? (
              <div className="text-[13px] text-fg-subtle">
                No training runs recorded yet — they appear after the next
                retrain.
              </div>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-[13px]">
                  <thead>
                    <tr className="text-left text-[12px] text-fg-muted border-b border-border-muted">
                      <th className="py-1.5 pr-3 font-medium">Version</th>
                      <th className="py-1.5 pr-3 font-medium">Trained</th>
                      <th className="py-1.5 pr-3 font-medium text-right">Samples</th>
                      <th className="py-1.5 pr-3 font-medium text-right">Features</th>
                      <th className="py-1.5 pr-3 font-medium text-right">CV AUC</th>
                      <th className="py-1.5 pr-3 font-medium text-right">Val acc</th>
                      <th className="py-1.5 pr-3 font-medium text-right">Pred-1</th>
                      <th className="py-1.5 pr-3 font-medium text-right">Recompute 1/0</th>
                      <th className="py-1.5 font-medium text-right">Time</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-border-muted">
                    {state.history.map((r: MLTrainingRun) => {
                      const isCurrent = r.version === currentVersion;
                      return (
                        <tr
                          key={r.id}
                          className={
                            isCurrent ? "bg-accent/5" : "hover:bg-canvas-subtle/50"
                          }
                        >
                          <td className="py-1.5 pr-3">
                            <span className="inline-flex items-center gap-1.5">
                              <span className="font-mono font-semibold">
                                v{r.version}
                              </span>
                              {isCurrent && (
                                <span className="badge bg-accent/10 text-accent border border-accent/30 !text-[10px]">
                                  current
                                </span>
                              )}
                            </span>
                          </td>
                          <td className="py-1.5 pr-3 text-fg-muted whitespace-nowrap">
                            {r.trained_at ? formatDate(r.trained_at) : "—"}
                          </td>
                          <td className="py-1.5 pr-3 text-right font-mono text-[12px]">
                            {r.num_samples ?? "—"}
                            {r.num_positives != null && (
                              <span className="text-fg-subtle">
                                {" "}
                                ({r.num_positives}+/{r.num_negatives}−)
                              </span>
                            )}
                          </td>
                          <td className="py-1.5 pr-3 text-right font-mono text-[12px]">
                            {r.input_dim ?? "—"}
                          </td>
                          <td
                            className={`py-1.5 pr-3 text-right font-mono text-[12px] ${
                              r.cv_auc != null && r.cv_auc > 0.5
                                ? "text-success-fg"
                                : r.cv_auc != null
                                ? "text-fg"
                                : "text-fg-subtle"
                            }`}
                          >
                            {auc(r.cv_auc)}
                          </td>
                          <td className="py-1.5 pr-3 text-right font-mono text-[12px]">
                            {pct(r.val_accuracy)}
                          </td>
                          <td className="py-1.5 pr-3 text-right font-mono text-[12px]">
                            {pct(r.cv_pred1_ratio)}
                          </td>
                          <td className="py-1.5 pr-3 text-right font-mono text-[12px] whitespace-nowrap">
                            {r.recompute_pred_1 != null ? (
                              <>
                                <span className="text-accent">
                                  {r.recompute_pred_1}
                                </span>
                                <span className="text-fg-subtle">
                                  {" "}
                                  / {r.recompute_pred_0}
                                </span>
                              </>
                            ) : (
                              <span className="text-fg-subtle">
                                not recorded
                              </span>
                            )}
                          </td>
                          <td className="py-1.5 text-right font-mono text-[12px] text-fg-subtle">
                            {r.training_time_seconds != null
                              ? `${r.training_time_seconds}s`
                              : "—"}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}

            <p className="mt-3 text-[12px] text-fg-subtle">
              Recorded on every retrain (startup + every 24h). The recompute
              columns show the population distribution produced by the
              automatic background re-prediction after each model save.
            </p>
          </div>
        </>
      )}
    </div>
  );
}

function Metric({
  label,
  value,
  sub,
  highlight,
}: {
  label: string;
  value: string;
  sub?: string;
  highlight?: boolean;
}) {
  return (
    <div className="py-1.5 border-b border-border-muted/60 last:border-0">
      <div className="text-[12px] text-fg-muted">{label}</div>
      <div
        className={`text-[15px] font-semibold leading-tight mt-0.5 ${
          highlight ? "text-success-fg" : ""
        }`}
      >
        {value}
      </div>
      {sub && <div className="text-[12px] text-fg-subtle mt-0.5">{sub}</div>}
    </div>
  );
}
