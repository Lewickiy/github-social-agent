export interface StatusCount {
  status: string;
  count: number;
}

export interface BucketCount {
  bucket: string;
  count: number;
}

export interface ActivityPoint {
  day: string;
  count: number;
}

export interface FollowerHistoryPoint {
  date: string;
  followers: number;
  following: number | null;
  public_repos: number | null;
}

export interface RecentAction {
  username: string;
  action: string;
  created_at: string;
}

export interface Job {
  id: number;
  mode: string;
  status: "PENDING" | "RUNNING" | "SUCCESS" | "FAILED";
  pid: number | null;
  started_at: string | null;
  finished_at: string | null;
  exit_code: number | null;
  error: string | null;
  created_at: string | null;
}

export interface GitHubUsage {
  requests_last_hour: number;
  requests_today: number;
  requests_total: number;
  rate_limit: number;
  percent_last_hour: number;
}

export interface Stats {
  github_usage: GitHubUsage;
  totals: {
    total: number;
    scored: number;
    scored_positive: number;
    queue: number;
    followed: number;
    followbacks: number;
    unfollowed_after_mutual: number;
    deleted: number;
    ml_positive: number;
  };
  today_follows: number;
  daily_limit: number;
  owner: string | null;
  followers_count: number | null;
  activity: ActivityPoint[];
  followers_history: FollowerHistoryPoint[];
  status_distribution: StatusCount[];
  score_buckets: BucketCount[];
  recent_actions: RecentAction[];
  jobs: Job[];
}

export interface UserRow {
  username: string;
  score: number | null;
  followers: number;
  public_repos: number;
  status: string;
  bio: string | null;
  company: string | null;
  created_at: string | null;
  followed_at: string | null;
  scored_at: string | null;
  ml_follow_prediction: number | null;
  avatar_url: string;
  top_languages: string[];
}

export interface UsersResponse {
  items: UserRow[];
  total: number;
  page: number;
  per_page: number;
}

export interface Repo {
  name: string;
  description: string | null;
  html_url: string;
  stars: number;
  forks: number;
  watchers: number;
  is_fork: boolean;
  is_archived: boolean;
  updated_at: string | null;
}

export interface CompanyRef {
  login: string;
  name: string | null;
  type: string | null;
}

export interface UserProfile {
  username: string;
  score: number | null;
  public_repos: number;
  followers: number;
  bio: string | null;
  company: string | null;
  status: string;
  created_at: string | null;
  scored_at: string | null;
  followed_at: string | null;
  discovered_from: string | null;
  repos_fetched_at: string | null;
  followers_count: number | null;
  ml_follow_prediction: number | null;
  avatar_url: string;
  name?: string;
  location?: string;
  blog?: string;
  twitter_username?: string;
  email?: string;
  type?: string;
  following?: number;
  languages: [string, number][];
  topics: string[];
  companies: CompanyRef[];
  repos: Repo[];
}

export interface LanguageOption {
  name: string;
  users: number;
}

export interface Config {
  my_username: string;
  daily_follow_limit: number;
  follow_delay: number;
  score_threshold: number;
  current_score_version: number;
  ml_enabled: boolean;
  ml_train_interval_hours: number;
  repo_freshness_days: number;
  owner_sync_days: number;
  score_freshness_days: number;
  follower_scan_days: number;
  prioritize_small: boolean;
}

export interface JobsResponse {
  items: Job[];
}
