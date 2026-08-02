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

export function StatusBars({ data }: { data: StatusCount[] }) {
  // Always render every status in STATUS_ORDER (zero-filled), so columns
  // stay consistent even when a status currently has no users.
  const counts = new Map(data.map((d) => [d.status, d.count]));
  const ordered = STATUS_ORDER.map((status) => ({
    status,
    count: counts.get(status) ?? 0,
  }));

  return (
    <div className="h-[200px] w-full">
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={ordered} margin={{ top: 8, right: 8, bottom: 0, left: -18 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#d0d7de" vertical={false} />
          <XAxis
            dataKey="status"
            tick={{ fontSize: 11, fill: "#656d76" }}
            tickLine={false}
            axisLine={{ stroke: "#d0d7de" }}
            tickFormatter={(s: string) => STATUS_META[s]?.label ?? s}
          />
          <YAxis allowDecimals={false} tick={{ fontSize: 11, fill: "#656d76" }} tickLine={false} axisLine={false} />
          <Tooltip
            cursor={{ fill: "#f6f8fa" }}
            contentStyle={{ fontSize: 12, borderRadius: 6, border: "1px solid #d0d7de" }}
            formatter={(v, _n, item) => [v, STATUS_META[item.payload.status]?.label ?? item.payload.status]}
          />
          <Bar dataKey="count" radius={[3, 3, 0, 0]} maxBarSize={48}>
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
