import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { ActivityPoint } from "../types";

export default function ActivityChart({ data }: { data: ActivityPoint[] }) {
  return (
    <div className="h-[220px] w-full">
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={data} margin={{ top: 8, right: 8, bottom: 0, left: -18 }}>
          <defs>
            <linearGradient id="followGradient" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="#0969da" stopOpacity={0.25} />
              <stop offset="100%" stopColor="#0969da" stopOpacity={0} />
            </linearGradient>
          </defs>
          <CartesianGrid strokeDasharray="3 3" stroke="#d0d7de" vertical={false} />
          <XAxis
            dataKey="day"
            tick={{ fontSize: 11, fill: "#656d76" }}
            tickLine={false}
            axisLine={{ stroke: "#d0d7de" }}
            tickFormatter={(d: string) => d.slice(5)}
            minTickGap={24}
          />
          <YAxis
            allowDecimals={false}
            tick={{ fontSize: 11, fill: "#656d76" }}
            tickLine={false}
            axisLine={false}
          />
          <Tooltip
            contentStyle={{
              fontSize: 12,
              borderRadius: 6,
              border: "1px solid #d0d7de",
              boxShadow: "0 8px 24px rgba(66,74,83,0.12)",
            }}
            labelFormatter={(d: string) => d}
            formatter={(v) => [v, "follows"]}
          />
          <Area
            type="monotone"
            dataKey="count"
            stroke="#0969da"
            strokeWidth={2}
            fill="url(#followGradient)"
            name="follows"
          />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}
