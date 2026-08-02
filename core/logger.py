"""Centralised logging configuration.

Every module that needs logging does::

    from logger import get_logger
    log = get_logger(__name__)

File handler (DEBUG → github_social.log) captures everything.
Console handler (WARNING+) keeps the terminal clean — only errors,
rate-limit warnings, and retry messages appear on screen.
Progress messages like "Fetching repos for …" use log.info() so they
land in the file but stay off the console.
"""

import logging
import os
from core.config import LOG_FILE


def get_logger(name: str) -> logging.Logger:
    """Return a logger with file + console handlers."""
    logger = logging.getLogger(name)

    # Avoid adding duplicate handlers on repeated imports
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)

    # --- File handler: everything goes to the log file ---
    # Ensure the parent directory exists (e.g. logs/ is gitignored and
    # may be missing on a fresh checkout; FileHandler does not create it).
    os.makedirs(os.path.dirname(os.path.abspath(LOG_FILE)), exist_ok=True)
    fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(
        logging.Formatter(
            "%(asctime)s  %(levelname)-8s  [%(name)s]  %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    logger.addHandler(fh)

    # --- Console handler: only warnings and above ---
    ch = logging.StreamHandler()
    ch.setLevel(logging.WARNING)
    ch.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(ch)

    return logger
