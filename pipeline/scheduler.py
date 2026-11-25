"""Decides, per country, whether to email now and to which address. No I/O."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional, Tuple

from .config import MAX_ATTEMPTS, MAX_SEARCHES_PER_COUNTRY, RETRY_AFTER_DAYS
from .state import ACTIVE, bounced_emails, parse_dt, tried_emails

Recipient = Tuple[str, str, str]  # (email, kind, source); kind: initial | retry | follow_up


def is_due(entry: dict, now: datetime) -> bool:
    """True when this country should get its next email this run."""
    if entry["status"] not in ACTIVE or entry.get("reply_pending"):
        return False  # finished, or a reply arrived that we haven't been able to read yet
    if not entry["attempts"]:
        return True
    last = entry["attempts"][-1]
    if last.get("bounced") or entry.get("send_now"):
        return True
    return now - parse_dt(last["sent_at"]) >= timedelta(days=RETRY_AFTER_DAYS)


def out_of_attempts(entry: dict) -> bool:
    return len(entry["attempts"]) >= MAX_ATTEMPTS


def next_untried(entry: dict) -> Optional[Recipient]:
    """First known address we have not emailed yet."""
    skip = tried_emails(entry) | bounced_emails(entry)
    kind = "retry" if entry["attempts"] else "initial"
    for c in entry["candidate_emails"]:
        if c["email"].lower() not in skip:
            return c["email"], kind, c["source"]
    return None


def follow_up(entry: dict) -> Optional[Recipient]:
    """Re-send to the last working address, at most once per address."""
    for a in reversed(entry["attempts"]):
        if a.get("bounced"):
            continue
        already = any(
            x["kind"] == "follow_up" and x["email"].lower() == a["email"].lower()
            for x in entry["attempts"]
        )
        return None if already else (a["email"], "follow_up", a["source"])
    return None


def can_search(entry: dict) -> bool:
    return len(entry.get("searches", [])) < MAX_SEARCHES_PER_COUNTRY
