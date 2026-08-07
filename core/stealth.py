"""Small helpers for stealthy (human-like) pacing."""

import random


def jitter(seconds, factor=0.3):
    """Add ±factor jitter to *seconds* to look more natural.

    Clamps the lower bound to at least 1 second to avoid near-zero
    delays that would defeat the stealth purpose.
    """
    lo = max(seconds * (1 - factor), 1)
    hi = seconds * (1 + factor)
    return random.uniform(lo, hi)
