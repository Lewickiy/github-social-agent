import type { ReactNode } from "react";

interface StatCardProps {
  label: string;
  value: ReactNode;
  sub?: ReactNode;
  icon?: ReactNode;
  accent?: "default" | "success" | "accent" | "danger" | "attention";
  hint?: string;
}

const accents = {
  default: "text-fg",
  success: "text-success-fg",
  accent: "text-accent",
  danger: "text-danger-fg",
  attention: "text-attention",
};

export default function StatCard({
  label,
  value,
  sub,
  icon,
  accent = "default",
  hint,
}: StatCardProps) {
  return (
    <div className="card px-4 py-3 flex items-start gap-3 min-w-0 transition-shadow duration-150 hover:shadow-overlay">
      {icon && (
        <div className="mt-0.5 text-fg-subtle shrink-0" aria-hidden>
          {icon}
        </div>
      )}
      <div className="min-w-0">
        <div className="text-[12px] font-medium text-fg-muted">{label}</div>
        <div
          className={`text-[22px] font-semibold leading-tight mt-0.5 ${accents[accent]}`}
          title={hint}
        >
          {value}
        </div>
        {sub && <div className="text-[12px] text-fg-subtle mt-0.5">{sub}</div>}
      </div>
    </div>
  );
}
