"""Score users based on their public profile and similarity to the owner.

Score breakdown (0-100):
    Base score (0-35):
        - Repository count        : up to 15
        - Followers               : up to 15
        - Bio                     :  5

    Similarity score (0-65) — based on actual owner data:
        - Language similarity     : up to 30  (histogram intersection)
        - Topic similarity        : up to 20  (Jaccard index)
        - Repository recency      : up to 15  (days since last update)
"""

import time

from logger import get_logger

log = get_logger(__name__)


class Scorer:
    """Score users based on their public profile and language data."""

    def __init__(self, db, github):
        self.db = db
        self.github = github

    # --------------------------------------------------
    # Scoring rules
    # --------------------------------------------------

    @staticmethod
    def calculate(info, language_list, topic_list=None,
                  owner_langs=None, owner_topics=None, repo_days=None):
        """Calculate a composite score (0-100) for a user.

        Parameters
        ----------
        info : dict
            GitHub user profile (public_repos, followers, bio, …).
        language_list : list[(str, float)]
            Aggregated language percentages.
        topic_list : list[str] | None
            User's repository topics.
        owner_langs : dict[str, float] | None
            Owner's language distribution {name: percentage}.
        owner_topics : list[str] | None
            Owner's topic list.
        repo_days : int | None
            Days since the user's most recently updated repo.

        Returns
        -------
        int
            Score in range 0-100.
        """
        score = 0

        repos = info.get("public_repos", 0)
        followers = info.get("followers", 0)

        # ── Base score (0-35) ──────────────────────────

        # --- repository count (up to 15) ---
        if repos > 5:
            score += 5
        if repos > 20:
            score += 10

        # --- followers (up to 15) ---
        if followers > 20:
            score += 5
        if followers > 100:
            score += 10

        # --- bio (5) ---
        if info.get("bio"):
            score += 5

        # Cap base score at 35 to guarantee the 35/65 split
        score = min(score, 35)

        # ── Similarity score (0-65) ────────────────────

        if owner_langs is not None:
            score += Scorer._language_similarity(language_list, owner_langs)

        if owner_topics is not None:
            score += Scorer._topic_similarity(
                topic_list or [], owner_topics
            )

        if repo_days is not None:
            score += Scorer._recency_score(repo_days)

        return min(score, 100)

    # --------------------------------------------------
    # Similarity sub-scores
    # --------------------------------------------------

    @staticmethod
    def _language_similarity(user_langs, owner_langs):
        """Histogram intersection — up to 30 points.

        Both inputs are {lang_name: percentage} dicts.
        Overlap is the sum of minimum percentages for each language.
        """
        if not owner_langs:
            return 0

        all_langs = set(user_langs) | set(owner_langs)
        overlap = sum(
            min(user_langs.get(l, 0), owner_langs.get(l, 0))
            for l in all_langs
        )
        return int((overlap / 100.0) * 30)

    @staticmethod
    def _topic_similarity(user_topics, owner_topics):
        """Jaccard index — up to 20 points."""
        u = set(user_topics)
        o = set(owner_topics)
        if not u and not o:
            return 0
        if not u or not o:
            return 0
        jaccard = len(u & o) / len(u | o)
        return int(jaccard * 20)

    @staticmethod
    def _recency_score(days):
        """Repository recency — up to 15 points.

        Days since the user's most recently updated repository.
        """
        if days is None:
            return 0
        if days <= 7:
            return 15
        if days <= 30:
            return 10
        if days <= 90:
            return 5
        return 0

    # --------------------------------------------------
    # Main loop
    # --------------------------------------------------

    def run(self):
        # Load owner data once for the similarity comparison
        owner_username = self.db.get_owner()
        owner_langs = None
        owner_topics = None

        if owner_username:
            owner_langs = {
                name: pct
                for name, pct in self.db.user_languages(owner_username)
            }
            owner_topics = self.db.user_topics(owner_username)
            log.info("Owner profile loaded: %s", owner_username)
        else:
            log.info("No owner set — using base scoring only (max 35).")

        users = self.db.unscored_users()

        if not users:
            print("All users are up to date.")
            log.info("All users are up to date — nothing to score.")
            return

        log.info("Scoring %d users", len(users))

        for (username,) in users:
            print("Scoring", username)
            log.debug("Scoring %s", username)

            info = self.github.user(username)

            if info:
                lang_list = self.db.user_languages(username)
                topic_list = self.db.user_topics(username)
                repo_days = self.db.user_repo_recency(username)

                score = self.calculate(
                    info, lang_list, topic_list,
                    owner_langs=owner_langs,
                    owner_topics=owner_topics,
                    repo_days=repo_days,
                )
                self.db.update_score(username, score, info)
            else:
                log.warning("No profile data returned for %s", username)

            time.sleep(1)

        log.info("Scoring complete.")
