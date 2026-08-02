import { STATUS_META, JOB_STATUS_META } from "../status";

export function StatusBadge({ status }: { status: string }) {
  const meta = STATUS_META[status] ?? STATUS_META.UNKNOWN;
  return (
    <span className={`badge ${meta.cls}`}>
      <span className={`w-2 h-2 rounded-full ${meta.dot}`} />
      {meta.label}
    </span>
  );
}

export function JobStatusBadge({ status }: { status: string }) {
  const meta = JOB_STATUS_META[status] ?? JOB_STATUS_META.PENDING;
  return (
    <span className={`badge ${meta.cls}`}>
      <span className={`w-2 h-2 rounded-full ${meta.dot}`} />
      {meta.label}
    </span>
  );
}
