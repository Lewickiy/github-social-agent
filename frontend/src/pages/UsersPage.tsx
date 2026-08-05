import { useCallback, useEffect, useState } from "react";
import {
  ArrowDown,
  ArrowUp,
  ChevronLeft,
  ChevronRight,
  Search,
} from "lucide-react";
import { api, timeAgo } from "../api";
import type { LanguageOption, UserRow } from "../types";
import { scoreColor } from "../status";
import ScoreRing from "../components/ScoreRing";
import { LanguageDots } from "../components/LanguageDots";
import { StatusBadge } from "../components/StatusBadge";
import ProfileDrawer from "../components/ProfileDrawer";
import { usePolling } from "../hooks/usePolling";
import { useRefresh } from "../refresh";

type SortKey = "score" | "followers" | "repos" | "followed_at" | "created_at";

const SORTS: { key: SortKey; label: string }[] = [
  { key: "score", label: "Score" },
  { key: "followers", label: "Followers" },
  { key: "repos", label: "Repos" },
  { key: "followed_at", label: "Followed" },
  { key: "created_at", label: "Discovered" },
];

const STATUS_FILTERS = [
  { value: "", label: "All statuses" },
  { value: "NEW", label: "New" },
  { value: "FOLLOWED", label: "Followed" },
  { value: "FOLLOWBACK", label: "Followback" },
  { value: "UNFOLLOWED_AFTER_MUTUAL_FOLLOW", label: "Unfollowed" },
  { value: "DELETED", label: "Deleted" },
];

const ML_FILTERS = [
  { value: "", label: "ML: any" },
  { value: "1", label: "ML: likely followback" },
  { value: "0", label: "ML: unlikely" },
  { value: "none", label: "ML: not predicted" },
];

export default function UsersPage() {
  const { intervalMs } = useRefresh();
  const [query, setQuery] = useState("");
  const [debounced, setDebounced] = useState("");
  const [status, setStatus] = useState("");
  const [language, setLanguage] = useState("");
  const [ml, setMl] = useState("");
  const [sort, setSort] = useState<SortKey>("score");
  const [order, setOrder] = useState<"asc" | "desc">("desc");
  const [page, setPage] = useState(1);
  const perPage = 25;

  const [items, setItems] = useState<UserRow[]>([]);
  const [total, setTotal] = useState(0);
  const [languages, setLanguages] = useState<LanguageOption[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);

  // Debounce the search box
  useEffect(() => {
    const t = setTimeout(() => {
      setDebounced(query);
      setPage(1);
    }, 300);
    return () => clearTimeout(t);
  }, [query]);

  // Language options (once)
  useEffect(() => {
    api.languages().then((r) => setLanguages(r.items)).catch(() => {});
  }, []);

  const fetchUsers = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await api.users({
        q: debounced || undefined,
        status: status || undefined,
        language: language || undefined,
        ml: ml || undefined,
        sort,
        order,
        page,
        per_page: perPage,
      });
      setItems(res.items);
      setTotal(res.total);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [debounced, status, language, ml, sort, order, page]);

  useEffect(() => {
    fetchUsers();
  }, [fetchUsers]);

  // Auto-refresh on the shared cadence (default 1 min, adjustable in Management).
  usePolling(fetchUsers, intervalMs);

  const totalPages = Math.max(1, Math.ceil(total / perPage));

  const toggleSort = (key: SortKey) => {
    if (sort === key) {
      setOrder(order === "desc" ? "asc" : "desc");
    } else {
      setSort(key);
      setOrder("desc");
    }
    setPage(1);
  };

  return (
    <div className="max-w-[1280px] mx-auto p-4 md:p-6 space-y-4">
      <div>
        <h1 className="text-[20px] font-semibold tracking-tight">Users</h1>
        <p className="text-[13px] text-fg-muted">
          {total.toLocaleString()} developers in the pipeline — click a row for
          the full profile
        </p>
      </div>

      {/* Toolbar */}
      <div className="card p-3 flex flex-col lg:flex-row gap-2">
        <div className="relative flex-1">
          <Search
            size={14}
            className="absolute left-2.5 top-1/2 -translate-y-1/2 text-fg-subtle"
          />
          <input
            className="input pl-8 w-full"
            placeholder="Search by username or bio…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </div>
        <div className="flex flex-wrap gap-2">
          <select
            className="input"
            value={status}
            onChange={(e) => {
              setStatus(e.target.value);
              setPage(1);
            }}
          >
            {STATUS_FILTERS.map((s) => (
              <option key={s.value} value={s.value}>
                {s.label}
              </option>
            ))}
          </select>
          <select
            className="input max-w-[200px]"
            value={language}
            onChange={(e) => {
              setLanguage(e.target.value);
              setPage(1);
            }}
          >
            <option value="">All languages</option>
            {languages.map((l) => (
              <option key={l.name} value={l.name}>
                {l.name} ({l.users})
              </option>
            ))}
          </select>
          <select
            className="input"
            value={ml}
            onChange={(e) => {
              setMl(e.target.value);
              setPage(1);
            }}
          >
            {ML_FILTERS.map((m) => (
              <option key={m.value} value={m.value}>
                {m.label}
              </option>
            ))}
          </select>
        </div>
      </div>

      {/* Table */}
      <div className="card overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full text-[13px]">
            <thead>
              <tr className="text-left text-[12px] text-fg-muted border-b border-border bg-canvas-subtle">
                <th className="px-3 py-2.5 font-medium w-[46%]">Developer</th>
                {SORTS.map((s) => (
                  <th
                    key={s.key}
                    className={`px-3 py-2.5 font-medium cursor-pointer select-none hover:text-fg transition-colors ${
                      sort === s.key ? "text-fg" : ""
                    }`}
                    onClick={() => toggleSort(s.key)}
                  >
                    <span className="inline-flex items-center gap-1">
                      {s.label}
                      {sort === s.key &&
                        (order === "desc" ? (
                          <ArrowDown size={12} />
                        ) : (
                          <ArrowUp size={12} />
                        ))}
                    </span>
                  </th>
                ))}
                <th className="px-3 py-2.5 font-medium">Languages</th>
                <th className="px-3 py-2.5 font-medium">Status</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border-muted">
              {loading && items.length === 0 && (
                <tr>
                  <td colSpan={8} className="px-3 py-10 text-center text-fg-subtle">
                    Loading…
                  </td>
                </tr>
              )}
              {!loading && error && (
                <tr>
                  <td colSpan={8} className="px-3 py-10 text-center text-danger-fg">
                    {error}
                  </td>
                </tr>
              )}
              {!loading && !error && items.length === 0 && (
                <tr>
                  <td colSpan={8} className="px-3 py-10 text-center text-fg-subtle">
                    No users match these filters.
                  </td>
                </tr>
              )}
              {items.map((u) => (
                <tr
                  key={u.username}
                  className="hover:bg-canvas-subtle/60 cursor-pointer transition-colors"
                  onClick={() => setSelected(u.username)}
                >
                  <td className="px-3 py-2.5">
                    <div className="flex items-center gap-2.5">
                      <img
                        src={u.avatar_url}
                        alt=""
                        className="w-7 h-7 rounded-full border border-border shrink-0"
                        loading="lazy"
                      />
                      <div className="min-w-0">
                        <a
                          href={`https://github.com/${u.username}`}
                          target="_blank"
                          rel="noreferrer"
                          onClick={(e) => e.stopPropagation()}
                          className="font-medium text-accent hover:underline block truncate"
                        >
                          {u.username}
                        </a>
                        {u.bio && (
                          <div className="text-[12px] text-fg-muted truncate max-w-[300px]">
                            {u.bio}
                          </div>
                        )}
                      </div>
                    </div>
                  </td>
                  <td className="px-3 py-2.5">
                    <div className="flex items-center gap-2">
                      <ScoreRing score={u.score} size={32} />
                      <span
                        className="text-[12px] font-semibold"
                        style={{ color: scoreColor(u.score) }}
                      >
                        {u.score ?? "—"}
                      </span>
                    </div>
                  </td>
                  <td className="px-3 py-2.5 text-fg-muted">{u.followers}</td>
                  <td className="px-3 py-2.5 text-fg-muted">{u.public_repos}</td>
                  <td className="px-3 py-2.5 text-fg-muted whitespace-nowrap">
                    {timeAgo(u.followed_at)}
                  </td>
                  <td className="px-3 py-2.5 text-fg-muted whitespace-nowrap">
                    {timeAgo(u.created_at)}
                  </td>
                  <td className="px-3 py-2.5">
                    <LanguageDots languages={u.top_languages} />
                  </td>
                  <td className="px-3 py-2.5">
                    <StatusBadge status={u.status} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        {/* Pagination */}
        <div className="flex items-center justify-between px-3 py-2.5 border-t border-border bg-canvas-subtle">
          <div className="text-[12px] text-fg-muted">
            {total.toLocaleString()} results · page {page} of {totalPages}
          </div>
          <div className="flex items-center gap-2">
            <button
              className="btn"
              disabled={page <= 1}
              onClick={() => setPage((p) => Math.max(1, p - 1))}
            >
              <ChevronLeft size={14} /> Prev
            </button>
            <button
              className="btn"
              disabled={page >= totalPages}
              onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
            >
              Next <ChevronRight size={14} />
            </button>
          </div>
        </div>
      </div>

      <ProfileDrawer username={selected} onClose={() => setSelected(null)} />
    </div>
  );
}
