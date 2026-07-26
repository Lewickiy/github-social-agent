import time

from config import DAILY_FOLLOW_LIMIT, FOLLOW_DELAY
from logger import get_logger

log = get_logger(__name__)


class FollowEngine:
    """Follow top-scored users respecting the daily limit."""

    def __init__(self, db, github):
        self.db = db
        self.github = github

    def run(self):
        done = self.db.today_follows()
        left = DAILY_FOLLOW_LIMIT - done

        print("Available:", left)
        log.info("Daily follow budget: %d used, %d available", done, left)

        if left <= 0:
            log.info("Daily follow limit reached (%d/%d).", done, DAILY_FOLLOW_LIMIT)
            return

        users = self.db.top_users(left)
        log.info("Following up to %d users", len(users))

        for username, score in users:
            print("FOLLOW", username, "score", score)

            if not self.github.already_following(username):
                if self.github.follow(username):
                    self.db.mark_followed(username)
                    log.info("Followed %s (score %d)", username, score)
                else:
                    log.warning("Failed to follow %s", username)
            else:
                log.debug("Already following %s — skipped", username)

            time.sleep(FOLLOW_DELAY)

        log.info("Follow session complete.")
