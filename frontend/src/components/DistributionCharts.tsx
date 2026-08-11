import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { BucketCount, StatusCount } from "../types";
import { STATUS_META, STATUS_ORDER } from "../status";

const BUCKET_COLORS: Record<string, string> = {
  "1-19": "#c2973d",
  "20-39": "#bc4c00",
  "40-59": "#9a6700",
  "60-79": "#1a7f37",
  "80-100": "#116329",
};

function statusColor(status: string): string {
  const dot = STATUS_META[status]?.dot;
  const map: Record<string, string> = {
    "bg-fg-subtle": "#8c959f",
    "bg-accent": "#0969da",
    "bg-success": "#1a7f37",
    "bg-danger": "#cf222e",
  };
  return map[dot] ?? "#8c959f";
}

/**
 * Split a legend label into at most two balanced lines (word-aware).
 *
 * Keeps every Pipeline Status label fully readable on narrow screens:
 * instead of letting recharts clip or drop a tick, long labels (like
 * "They unfollowed us" / "We unfollowed (no interaction)") wrap onto
 * two short lines that always fit the chart width.
 */
function splitLabelLines(label: string, maxLen: number): string[] {
  if (label.length <= maxLen) return [label];
  const words = label.split(" ");
  if (words.length < 2) {
    // Single unbreakable word — hard-break near the middle.
    const mid = Math.ceil(label.length / 2);
    return [label.slice(0, mid), label.slice(mid)];
  }
  let bestIdx = 1;
  let bestDiff = Infinity;
  for (let i = 1; i < words.length; i++) {
    const first = words.slice(0, i).join(" ").length;
    const second = words.slice(i).join(" ").length;
    const diff = Math.abs(first - second);
    if (diff < bestDiff) {
      bestDiff = diff;
      bestIdx = i;
    }
  }
  return [words.slice(0, bestIdx).join(" "), words.slice(bestIdx).join(" ")];
}

/**
 * Custom XAxis tick for the Pipeline Status chart — wraps long category
 * labels onto two lines so the legend never clips at any screen width.
 */
function StatusTick(props: any) {
  const { x, y, payload } = props;
  // payload.value is the raw status key — map to the human label exactly
  // like the old tickFormatter did (the wrapped text must stay readable).
  const label = STATUS_META[payload.value]?.label ?? payload.value;
  const lines = splitLabelLines(label, 12);
  return (
    <text x={x} y={y} dy={12} textAnchor="middle" fill="#656d76" fontSize={10}>
      {lines.map((line, i) => (
        <tspan key={i} x={x} dy={i === 0 ? 0 : 10}>
          {line}
        </tspan>
      ))}
    </text>
  );
}

export function StatusBars({ data }: { data: StatusCount[] }) {
  // Issue #14: hide zero-value categories and order the visible bars from
  // the largest value to the smallest (left → right), so the chart is
  // scannable and never wastes space on empty columns.  With fewer bars
  // rendered, each one scales to fill the whole allocated width.
  const counts = new Map(data.map((d) => [d.status, d.count]));
  const ordered = STATUS_ORDER.map((status) => ({
    status,
    count: counts.get(status) ?? 0,
  }))
    .filter((entry) => entry.count > 0)
    .sort((a, b) => b.count - a.count);

  if (ordered.length === 0) {
    // Every category is zero in this window — the bars are hidden by
    // design (issue #14); show a quiet hint instead of a bare grid.
    return (
      <div className="h-[200px] w-full flex items-center justify-center">
        <span className="text-[13px] text-fg-subtle">
          No status transitions in this period
        </span>
      </div>
    );
  }

  return (
    <div className="h-[200px] w-full">
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={ordered} margin={{ top: 8, right: 8, bottom: 4, left: -18 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#d0d7de" vertical={false} />
          <XAxis
            dataKey="status"
            interval={0}
            height={38}
            tick={<StatusTick />}
            tickLine={false}
            axisLine={{ stroke: "#d0d7de" }}
          />
          <YAxis allowDecimals={false} tick={{ fontSize: 11, fill: "#656d76" }} tickLine={false} axisLine={false} />
          <Tooltip
            cursor={{ fill: "#f6f8fa" }}
            contentStyle={{ fontSize: 12, borderRadius: 6, border: "1px solid #d0d7de" }}
            formatter={(v, _n, item) => [v, STATUS_META[item.payload.status]?.label ?? item.payload.status]}
          />
          {/* No maxBarSize cap: the visible bars always fill the chart
              width, re-scaling whenever the number of non-zero categories
              changes (issue #14). */}
          <Bar dataKey="count" radius={[3, 3, 0, 0]}>
            {ordered.map((entry) => (
              <Cell key={entry.status} fill={statusColor(entry.status)} />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

export function ScoreBars({ data }: { data: BucketCount[] }) {
  return (
    <div className="h-[200px] w-full">
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={data} margin={{ top: 8, right: 8, bottom: 0, left: -18 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#d0d7de" vertical={false} />
          <XAxis
            dataKey="bucket"
            tick={{ fontSize: 11, fill: "#656d76" }}
            tickLine={false}
            axisLine={{ stroke: "#d0d7de" }}
          />
          <YAxis allowDecimals={false} tick={{ fontSize: 11, fill: "#656d76" }} tickLine={false} axisLine={false} />
          <Tooltip
            cursor={{ fill: "#f6f8fa" }}
            contentStyle={{ fontSize: 12, borderRadius: 6, border: "1px solid #d0d7de" }}
            formatter={(v) => [v, "users"]}
          />
          <Bar dataKey="count" radius={[3, 3, 0, 0]} maxBarSize={48}>
            {data.map((entry) => (
              <Cell key={entry.bucket} fill={BUCKET_COLORS[entry.bucket] ?? "#8c959f"} />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}
