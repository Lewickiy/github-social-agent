import {
  Area,
  AreaChart,
  CartesianGrid,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { FollowerHistoryPoint } from "../types";

export default function FollowersChart({
  data,
}: {
  data: FollowerHistoryPoint[];
}) {
  if (data.length === 0) {
    return (
      <div className="h-[220px] w-full flex items-center justify-center text-[13px] text-fg-subtle">
        No snapshots yet — the daily snapshot runs at 12:00 UTC.
      </div>
    );
  }

  return (
    <div className="h-[220px] w-full">
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart
          data={data}
          margin={{ top: 8, right: 8, bottom: 0, left: -12 }}
        >
          <defs>
            <linearGradient
              id="followersGradient"
              x1="0"
              y1="0"
              x2="0"
              y2="1"
            >
              <stop offset="0%" stopColor="#0969da" stopOpacity={0.25} />
              <stop offset="100%" stopColor="#0969da" stopOpacity={0} />
            </linearGradient>
            <linearGradient
              id="followingGradient"
              x1="0"
              y1="0"
              x2="0"
              y2="1"
            >
              <stop offset="0%" stopColor="#1a7f37" stopOpacity={0.18} />
              <stop offset="100%" stopColor="#1a7f37" stopOpacity={0} />
            </linearGradient>
          </defs>
          <CartesianGrid strokeDasharray="3 3" stroke="#d0d7de" vertical={false} />
          <XAxis
            dataKey="date"
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
            formatter={(v, name) => [v, name]}
          />
          <Legend
            iconType="circle"
            iconSize={8}
            wrapperStyle={{ fontSize: 12, paddingTop: 4 }}
          />
          <Area
            type="monotone"
            dataKey="followers"
            stroke="#0969da"
            strokeWidth={2}
            fill="url(#followersGradient)"
            name="Followers"
          />
          <Area
            type="monotone"
            dataKey="following"
            stroke="#1a7f37"
            strokeWidth={2}
            fill="url(#followingGradient)"
            name="Following"
            connectNulls
          />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}
