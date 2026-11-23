"""Pipeline state, saved to pipeline/state.json and committed back to the repo.

The repo is public, so state holds only public embassy addresses, dates,
statuses and the short evidence quote that also goes into the PR. Raw reply
bodies are never stored.

Per-country status:
  queued          nothing sent yet
  awaiting_reply  emailed; waiting RETRY_AFTER_DAYS before the next attempt
  answered        reply parsed with confidence; goes into the next PR
  pr_open         included in an open rtd-travel-check PR
  done            the country is now in data.json
  needs_review    embassy asked a question / unclear answer / PR closed → manual
  exhausted       MAX_ATTEMPTS emails (or searches) without an answer → manual
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

from .config import EXCLUDED_CODES, STATE_PATH
from .countries import Countries

ACTIVE = {"queued", "awaiting_reply"}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def parse_dt(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def load(path: Path = STATE_PATH) -> dict:
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {"version": 1, "processed_message_ids": [], "countries": {}}


def save(state: dict, path: Path = STATE_PATH) -> None:
    state["processed_message_ids"] = sorted(set(state["processed_message_ids"]))
    state["countries"] = dict(sorted(state["countries"].items()))
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def new_entry(code: str, countries: Countries) -> dict:
    c = countries.by_code[code]
    return {
        "code": code,
        "name": c["name"],
        "display": countries.display(code),
        "status": "queued",
        "candidate_emails": [{"email": e, "source": "state.gov"} for e in c.get("emails", [])],
        "attempts": [],
        "searches": [],
        "answer": None,
        "review_reason": None,
        "pr_url": None,
        "updated_at": None,
    }


def sync_targets(state: dict, countries: Countries, covered: set, now: datetime) -> List[str]:
    """Add newly-missing countries; mark covered ones done. Returns added codes."""
    added = []
    for code in countries.by_code:
        if code in EXCLUDED_CODES:
            continue
        entry = state["countries"].get(code)
        if code in covered:
            if entry and entry["status"] != "done":
                entry["status"] = "done"
                entry["updated_at"] = iso(now)
            continue
        if entry is None:
            state["countries"][code] = new_entry(code, countries)
            added.append(code)
    return added


SUBJECT_COUNTRY = re.compile(r"Refugee Travel Document(?: holders)?\s*-\s*(.+)$", re.I | re.S)


def country_from_subject(subject: str, countries: Countries) -> Optional[str]:
    m = SUBJECT_COUNTRY.search(re.sub(r"\s+", " ", subject))
    return countries.code_for(m.group(1).strip()) if m else None


def record_sent_history(state: dict, countries: Countries, sent: List[dict]) -> List[str]:
    """Record inquiries found in Gmail's Sent Mail that state doesn't know about yet.

    Covers the January 2025 campaign and anything sent by hand. Each item:
    {"to", "subject", "date" (datetime), "message_id"}. Returns codes that changed.
    """
    known_ids = set(all_message_ids(state))
    changed = []
    for item in sorted(sent, key=lambda x: x["date"]):
        code = country_from_subject(item["subject"], countries)
        entry = state["countries"].get(code) if code else None
        if not entry or not item["to"]:
            continue  # tests ("Country1"), territories, countries already in data.json
        mid = (item.get("message_id") or "").strip() or None
        when = item["date"] if item["date"].tzinfo else item["date"].replace(tzinfo=timezone.utc)
        if mid and mid in known_ids:
            continue
        if any(a["email"].lower() == item["to"].lower() and abs(parse_dt(a["sent_at"]) - when) < timedelta(minutes=10)
               for a in entry["attempts"]):
            continue
        reply = re.match(r"^\s*(re|fwd?):", item["subject"], re.I)
        entry["attempts"].append({
            "email": item["to"],
            "sent_at": iso(when),
            "message_id": mid,
            "kind": "follow_up" if reply else ("retry" if entry["attempts"] else "initial"),
            "source": "gmail_sent",
            "bounced": False,
        })
        entry["attempts"].sort(key=lambda a: a["sent_at"])
        add_candidate(entry, item["to"], "gmail_sent")
        if entry["status"] == "queued":
            entry["status"] = "awaiting_reply"
        if mid:
            known_ids.add(mid)
        changed.append(code)
    return sorted(set(changed))


def tried_emails(entry: dict) -> set:
    return {a["email"].lower() for a in entry["attempts"]}


def bounced_emails(entry: dict) -> set:
    return {a["email"].lower() for a in entry["attempts"] if a.get("bounced")}


def add_candidate(entry: dict, email: str, source: str, first: bool = False) -> bool:
    """Add a known address. first=True (embassy told us to use it) moves it to the front."""
    existing = [c for c in entry["candidate_emails"] if c["email"].lower() == email.lower()]
    if existing and not first:
        return False
    if existing:
        entry["candidate_emails"].remove(existing[0])
    item = {"email": email, "source": source}
    if first:
        entry["candidate_emails"].insert(0, item)
    else:
        entry["candidate_emails"].append(item)
    return not existing


def all_message_ids(state: dict) -> Dict[str, str]:
    """Our sent Message-IDs → country code, for threading-based reply matching."""
    out = {}
    for code, entry in state["countries"].items():
        for a in entry["attempts"]:
            if a.get("message_id"):
                out[a["message_id"].strip()] = code
    return out


def email_to_code(state: dict) -> Dict[str, str]:
    out = {}
    for code, entry in state["countries"].items():
        for a in entry["attempts"]:
            out[a["email"].lower()] = code
    return out


def earliest_attempt(state: dict) -> Optional[datetime]:
    dates = [parse_dt(a["sent_at"]) for e in state["countries"].values() for a in e["attempts"]]
    return min(dates) if dates else None
