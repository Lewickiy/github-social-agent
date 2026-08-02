import { useCallback, useEffect, useState } from "react";
import {
  Building2,
  ExternalLink,
  GitFork,
  MapPin,
  Star,
  Twitter,
  X,
} from "lucide-react";
import { api, formatDate, timeAgo } from "../api";
import type { UserProfile } from "../types";
import { languageColor, scoreColor } from "../status";
import { StatusBadge } from "./StatusBadge";
import ScoreRing from "./ScoreRing";

interface Props {
  username: string | null;
  onClose: () => void;
}

export default function ProfileDrawer({ username, onClose }: Props) {
  const [profile, setProfile] = useState<UserProfile | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!username) {
      setProfile(null);
      return;
    }
    setError(null);
    setProfile(null);
    api
      .user(username)
      .then(setProfile)
      .catch((e) => setError(e.message));
  }, [username]);

  const onKeyDown = useCallback(
    (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    },
    [onClose]
  );

  useEffect(() => {
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onKeyDown]);

  const open = username != null;

  return (
    <>
      {/* Backdrop */}
      <div
        className={`fixed inset-0 bg-fg/40 z-40 transition-opacity duration-200 ${
          open ? "opacity-100" : "opacity-0 pointer-events-none"
        }`}
        onClick={onClose}
        aria-hidden
      />

      {/* Panel */}
      <aside
        className={`fixed top-0 right-0 h-full w-full max-w-[560px] bg-canvas z-50 shadow-overlay transform transition-transform duration-300 ease-out flex flex-col ${
          open ? "translate-x-0" : "translate-x-full"
        }`}
        role="dialog"
        aria-label="User profile"
      >
        {/* Header */}
        <div className="flex items-center justify-between px-4 py-3 border-b border-border bg-canvas-subtle shrink-0">
          <span className="text-[14px] font-semibold">Developer profile</span>
          <button
            className="btn !p-1.5"
            onClick={onClose}
            aria-label="Close profile"
          >
            <X size={16} />
          </button>
        </div>

        <div className="flex-1 overflow-y-auto">
          {error && (
            <div className="p-6 text-danger-fg text-[13px]">
              Could not load profile: {error}
            </div>
          )}

          {!error && !profile && username && (
            <div className="p-6 text-fg-subtle text-[13px]">Loading…</div>
          )}

          {profile && (
            <div className="p-4 space-y-5">
              {/* Identity */}
              <div className="flex items-start gap-3">
                <img
                  src={profile.avatar_url}
                  alt={profile.username}
                  className="w-14 h-14 rounded-full border border-border"
                  loading="lazy"
                />
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2 flex-wrap">
                    <a
                      href={`https://github.com/${profile.username}`}
                      target="_blank"
                      rel="noreferrer"
                      className="text-[18px] font-semibold text-accent hover:underline"
                    >
                      {profile.name || profile.username}
                    </a>
                    {profile.type === "Organization" && (
                      <span className="badge bg-accent/10 text-accent border border-accent/30">
                        org
                      </span>
                    )}
                    <StatusBadge status={profile.status} />
                  </div>
                  <div className="text-fg-muted text-[13px]">
                    @{profile.username}
                  </div>
                  {profile.bio && (
                    <p className="text-[13px] mt-1.5">{profile.bio}</p>
                  )}
                  <div className="flex items-center gap-3 mt-1.5 text-[12px] text-fg-muted flex-wrap">
                    {profile.company && (
                      <span className="inline-flex items-center gap-1">
                        <Building2 size={13} /> {profile.company}
                      </span>
                    )}
                    {profile.location && (
                      <span className="inline-flex items-center gap-1">
                        <MapPin size={13} /> {profile.location}
                      </span>
                    )}
                    {profile.twitter_username && (
                      <a
                        href={`https://twitter.com/${profile.twitter_username}`}
                        target="_blank"
                        rel="noreferrer"
                        className="inline-flex items-center gap-1 hover:text-accent"
                      >
                        <Twitter size={13} /> @{profile.twitter_username}
                      </a>
                    )}
                    <span>
                      <strong className="text-fg">{profile.followers}</strong>{" "}
                      followers
                    </span>
                    {profile.following != null && (
                      <span>
                        <strong className="text-fg">{profile.following}</strong>{" "}
                        following
                      </span>
                    )}
                  </div>
                </div>
                <ScoreRing score={profile.score} size={52} />
              </div>

              {/* Meta */}
              <div className="grid grid-cols-2 gap-2 text-[12px]">
                <Meta label="Discovered" value={timeAgo(profile.created_at)} />
                <Meta label="Scored" value={timeAgo(profile.scored_at)} />
                <Meta label="Followed" value={timeAgo(profile.followed_at)} />
                <Meta
                  label="Repos fetched"
                  value={timeAgo(profile.repos_fetched_at)}
                />
                <Meta label="Source" value={profile.discovered_from ?? "—"} />
                <Meta
                  label="ML prediction"
                  value={
                    profile.ml_follow_prediction == null
                      ? "—"
                      : profile.ml_follow_prediction === 1
                        ? "👍 likely"
                        : "👎 unlikely"
                  }
                />
              </div>

              {/* Languages */}
              {profile.languages.length > 0 && (
                <div>
                  <h3 className="text-[13px] font-semibold mb-2">
                    Languages
                  </h3>
                  <div className="space-y-1.5">
                    {profile.languages.slice(0, 8).map(([name, pct]) => (
                      <div key={name} className="flex items-center gap-2">
                        <span
                          className="w-2.5 h-2.5 rounded-full shrink-0"
                          style={{ backgroundColor: languageColor(name) }}
                        />
                        <span className="text-[12px] w-28 truncate">{name}</span>
                        <div className="flex-1 h-1.5 bg-canvas-subtle rounded-full overflow-hidden">
                          <div
                            className="h-full rounded-full"
                            style={{
                              width: `${Math.min(100, pct)}%`,
                              backgroundColor: languageColor(name),
                            }}
                          />
                        </div>
                        <span className="text-[12px] text-fg-muted w-10 text-right">
                          {pct.toFixed(1)}%
                        </span>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {/* Topics */}
              {profile.topics.length > 0 && (
                <div>
                  <h3 className="text-[13px] font-semibold mb-2">Topics</h3>
                  <div className="flex flex-wrap gap-1.5">
                    {profile.topics.slice(0, 20).map((t) => (
                      <span
                        key={t}
                        className="text-[12px] px-2 py-0.5 rounded-full bg-canvas-subtle border border-border text-accent"
                      >
                        {t}
                      </span>
                    ))}
                  </div>
                </div>
              )}

              {/* Companies */}
              {profile.companies.length > 0 && (
                <div>
                  <h3 className="text-[13px] font-semibold mb-2">Companies</h3>
                  <div className="flex flex-wrap gap-1.5">
                    {profile.companies.map((c) => (
                      <a
                        key={c.login}
                        href={`https://github.com/${c.login}`}
                        target="_blank"
                        rel="noreferrer"
                        className="text-[12px] px-2 py-0.5 rounded-md bg-canvas-subtle border border-border text-fg hover:border-accent hover:text-accent transition-colors"
                      >
                        @{c.login}
                        {c.name ? ` · ${c.name}` : ""}
                      </a>
                    ))}
                  </div>
                </div>
              )}

              {/* Repos */}
              {profile.repos.length > 0 && (
                <div>
                  <h3 className="text-[13px] font-semibold mb-2">
                    Top repositories
                  </h3>
                  <div className="space-y-2">
                    {profile.repos.map((r) => (
                      <div
                        key={r.name}
                        className="p-2.5 rounded-md border border-border bg-canvas-subtle/50"
                      >
                        <div className="flex items-center justify-between gap-2">
                          <a
                            href={r.html_url}
                            target="_blank"
                            rel="noreferrer"
                            className="text-[13px] font-medium text-accent hover:underline truncate inline-flex items-center gap-1"
                          >
                            {r.name}
                            <ExternalLink size={11} />
                          </a>
                          <div className="flex items-center gap-2.5 text-[12px] text-fg-muted shrink-0">
                            {r.is_fork && <span>fork</span>}
                            <span className="inline-flex items-center gap-0.5">
                              <Star size={12} /> {r.stars}
                            </span>
                            <span className="inline-flex items-center gap-0.5">
                              <GitFork size={12} /> {r.forks}
                            </span>
                          </div>
                        </div>
                        {r.description && (
                          <p className="text-[12px] text-fg-muted mt-1 line-clamp-2">
                            {r.description}
                          </p>
                        )}
                        {r.updated_at && (
                          <div className="text-[11px] text-fg-subtle mt-1">
                            Updated {formatDate(r.updated_at)}
                          </div>
                        )}
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {/* Score footnote */}
              <div className="text-[12px] text-fg-subtle pt-2 border-t border-border">
                Score {profile.score ?? "—"}/100 — similarity to your profile (
                <span style={{ color: scoreColor(profile.score) }}>
                  base + language + topics + recency
                </span>
                ).
              </div>
            </div>
          )}
        </div>
      </aside>
    </>
  );
}

function Meta({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex justify-between gap-2 py-1 border-b border-border-muted/60">
      <span className="text-fg-muted">{label}</span>
      <span className="font-medium text-fg truncate">{value}</span>
    </div>
  );
}
