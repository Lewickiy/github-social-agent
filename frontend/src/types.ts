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
  /** Requests within the selected Overview interval (null when not scoped). */
  requests_in_window: number | null;
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
  /** All lifecycle events (FOLLOW/FOLLOWBACK/UNFOLLOWED/DELETED) in the window. */
  total_actions: number;
  followers_history: FollowerHistoryPoint[];
  status_distribution: StatusCount[];
  score_buckets: BucketCount[];
  recent_actions: RecentAction[];
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
  /** Random pause between two follow actions (seconds, range). */
  follow_interval_min_seconds: number;
  follow_interval_max_seconds: number;
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

export interface Settings {
  /** Effective IANA timezone (stored value or the UTC default). */
  timezone: string;
  /** True once the user (or auto-detection) has picked a timezone. */
  timezone_set: boolean;
  /** Current UTC offset of the timezone in minutes (e.g. 180 = UTC+3). */
  utc_offset_minutes: number;
}

/** Metadata of one trained model (current.json / ml_training_runs row). */
export interface MLModelInfo {
  version: number;
  trained_at: string | null;
  label_scheme: string | null;
  num_samples: number | null;
  num_positives: number | null;
  num_negatives: number | null;
  input_dim: number | null;
  hidden_dim: number | null;
  dropout: number | null;
  val_accuracy: number | null;
  val_auc: number | null;
  val_precision: number | null;
  val_recall: number | null;
  val_loss: number | null;
  pred1_ratio: number | null;
  cv_folds: number | null;
  cv_accuracy: number | null;
  cv_auc: number | null;
  cv_precision: number | null;
  cv_recall: number | null;
  cv_pred1_ratio: number | null;
  epochs: number | null;
  early_stopped: boolean | null;
  device: string | null;
  training_time_seconds: number | null;
  top_languages: string[];
  top_topics: string[];
}

/** One retrain row — history of how the model evolved. */
export interface MLTrainingRun {
  id: number;
  version: number;
  trained_at: string | null;
  num_samples: number | null;
  num_positives: number | null;
  num_negatives: number | null;
  input_dim: number | null;
  val_accuracy: number | null;
  val_auc: number | null;
  val_precision: number | null;
  val_recall: number | null;
  pred1_ratio: number | null;
  cv_folds: number | null;
  cv_accuracy: number | null;
  cv_auc: number | null;
  cv_pred1_ratio: number | null;
  early_stopped: boolean | null;
  training_time_seconds: number | null;
  /** Prediction distribution produced by the post-train recompute. */
  recompute_total: number | null;
  recompute_pred_1: number | null;
  recompute_pred_0: number | null;
  recompute_failed: number | null;
  recompute_done_at: string | null;
  top_languages: string[];
  top_topics: string[];
}

export interface MLDataset {
  total_users: number;
  with_prediction: number;
  pred_1: number;
  pred_0: number;
  positive_share_pct: number;
  training_samples: number;
  training_positives: number;
  training_negatives: number;
  training_positive_share_pct: number;
}

export interface MLState {
  current_model: MLModelInfo | null;
  dataset: MLDataset;
  history: MLTrainingRun[];
}

/** One graph-discovery pass recorded by the background worker. */
export interface DiscoveryRun {
  id: number;
  started_at: string | null;
  finished_at: string | null;
  users_walked: number | null;
  new_users: number | null;
  requests: number | null;
  duration_seconds: number | null;
}

export interface DiscoveryState {
  enabled: boolean;
  rate_limit_per_hour: number;
  pass_max_users: number;
  last_run: DiscoveryRun | null;
  /** Requests of the last pass as a % of the hourly budget. */
  budget_percent: number;
  history: DiscoveryRun[];
}
