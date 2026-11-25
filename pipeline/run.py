"""Daily pipeline run.

  1. Load data.json from rtd-travel-check; queue sovereign countries missing from it.
  2. Read Gmail: mark bounces, parse embassy replies with Gemini.
  3. Email countries that are due: first contact, or no reply after RETRY_AFTER_DAYS
     (next known address → address found via Google Search → one follow-up).
  4. Open one rtd-travel-check PR with every confidently-answered country.
  5. Save state and print a summary.

Dry run (default) does everything read-only and prints what it would send.
--live sends email, opens the PR and saves pipeline/state.json.
"""
from __future__ import annotations

import argparse
import logging
import os
import smtplib
import sys
import tempfile
import time
from collections import Counter
from datetime import timedelta
from pathlib import Path
from typing import List

from . import llm, scheduler, state as st, travel_check
from .config import (MAX_SEARCHES_PER_RUN, MAX_SENDS_PER_RUN, MIN_CONFIDENCE, SEND_DELAY_SECONDS,
                     env, load_dotenv_if_present)
from .countries import Countries, norm
from .inbox import Inbox, body_text, bounced_addresses, match_country, strip_quoted
from .mailer import Mailer, build

log = logging.getLogger("pipeline")


def process_inbox(state: dict, countries: Countries, inbox: Inbox, api_key: str, now) -> List[str]:
    events: List[str] = []
    sent_ids = st.all_message_ids(state)
    sent_emails = st.email_to_code(state)
    if not sent_emails:
        log.info("Inbox: nothing sent yet, skipping")
        return events
    subject_names = {
        norm(n): code for code in state["countries"] for n in countries.all_names(code) if len(norm(n)) > 3
    }
    since = (st.earliest_attempt(state) or now) - timedelta(days=1)
    processed = set(state["processed_message_ids"])

    def want(item) -> bool:
        return item.is_bounce or match_country(item, sent_ids, sent_emails, subject_names) is not None

    for item in inbox.messages_since(since, processed, want):
        if item.is_bounce:
            for addr in bounced_addresses(item, list(sent_emails)):
                entry = state["countries"][sent_emails[addr]]
                for a in reversed(entry["attempts"]):
                    if a["email"].lower() == addr and not a.get("bounced"):
                        a["bounced"] = True
                        entry["updated_at"] = st.iso(now)
                        events.append(f"{entry['name']}: {addr} bounced")
                        break
            state["processed_message_ids"].append(item.message_id)
            continue

        code = match_country(item, sent_ids, sent_emails, subject_names)
        entry = state["countries"].get(code) if code else None
        if not entry or entry["status"] in {"answered", "pr_open", "done"}:
            state["processed_message_ids"].append(item.message_id)
            continue

        text = body_text(item.msg)
        parsed = llm.parse_reply(api_key, countries.display(entry["code"]), item.sender,
                                 strip_quoted(text) or text)
        if parsed is None:
            # Gemini unavailable: leave the message unprocessed and hold off emailing
            # this country until its reply has been read on a later run.
            entry["reply_pending"] = True
            events.append(f"{entry['name']}: reply received, will read it next run")
            continue
        state["processed_message_ids"].append(item.message_id)
        entry["reply_pending"] = False
        entry["updated_at"] = st.iso(now)
        cat = parsed["category"]
        received = st.iso(item.date or now)

        if cat == "answer" and parsed["visaRequirement"] != "Unknown" and parsed["confidence"] >= MIN_CONFIDENCE:
            entry["status"] = "answered"
            entry["answer"] = {
                "visaRequirement": parsed["visaRequirement"],
                "duration": parsed["duration"],
                "notes": parsed["notes"],
                "evidence": parsed["evidence"],
                "confidence": parsed["confidence"],
                "from_domain": item.sender.rsplit("@", 1)[-1],
                "received_at": received,
            }
            events.append(f"{entry['name']}: answered → {parsed['visaRequirement']}")
        elif cat == "redirect" and (new_addrs := [
            e for e in parsed["redirect_emails"]
            if e.lower() != item.sender and e.lower() not in st.tried_emails(entry)
        ]):
            for addr in reversed(new_addrs):
                st.add_candidate(entry, addr, "embassy_redirect", first=True)
            entry["send_now"] = True
            events.append(f"{entry['name']}: redirected to {', '.join(new_addrs)}")
        elif cat in {"answer", "question", "redirect"}:
            entry["status"] = "needs_review"
            entry["review_reason"] = (
                f"Embassy asked: {parsed['question']}" if cat == "question" and parsed["question"]
                else f"Unclear reply (confidence {parsed['confidence']:.2f}): {parsed['evidence'][:160]}"
            )
            events.append(f"{entry['name']}: needs review ({cat})")
        else:
            events.append(f"{entry['name']}: {cat.replace('_', ' ')} ignored, still waiting")
    return events


def send_due(state: dict, countries: Countries, live: bool, max_sends: int, user: str, password: str, api_key: str,
             sender_name: str, now, only: set) -> List[str]:
    events: List[str] = []
    entries = [e for code, e in state["countries"].items() if (not only or code in only)]
    due = [e for e in entries if scheduler.is_due(e, now)]
    # Bounces and redirects first, then retries, then first contacts.
    due.sort(key=lambda e: (not e.get("send_now") and not (e["attempts"] and e["attempts"][-1].get("bounced")),
                            not e["attempts"], e["name"]))
    log.info("Due this run: %d countries (send cap %d)", len(due), max_sends)

    sends = searches = 0
    mailer = Mailer(user, password) if live else None
    try:
        for entry in due:
            if sends >= max_sends:
                break
            if scheduler.out_of_attempts(entry):
                entry["status"] = "exhausted"
                entry["review_reason"] = f"No answer after {len(entry['attempts'])} emails"
                entry["updated_at"] = st.iso(now)
                events.append(f"{entry['name']}: exhausted after {len(entry['attempts'])} emails")
                continue

            rec = scheduler.next_untried(entry)
            if not rec and scheduler.can_search(entry) and searches < MAX_SEARCHES_PER_RUN:
                searches += 1
                exclude = sorted(st.tried_emails(entry) | {c["email"].lower() for c in entry["candidate_emails"]})
                found = llm.find_emails(api_key, countries.display(entry["code"]), exclude)
                if found is None:
                    events.append(f"{entry['name']}: web search unavailable, will retry next run")
                    continue
                entry["searches"].append({"at": st.iso(now), "found": found})
                for addr in found:
                    st.add_candidate(entry, addr, "web_search")
                events.append(f"{entry['name']}: web search found {found or 'nothing'}")
                rec = scheduler.next_untried(entry)
            if not rec and entry["attempts"]:
                rec = scheduler.follow_up(entry)
            if not rec:
                if not scheduler.can_search(entry):
                    entry["status"] = "exhausted"
                    entry["review_reason"] = "No working email address found"
                    entry["updated_at"] = st.iso(now)
                    events.append(f"{entry['name']}: exhausted (no working address)")
                continue

            addr, kind, source = rec
            prev_id = next((a.get("message_id") for a in reversed(entry["attempts"])
                            if a["email"].lower() == addr.lower() and a.get("message_id")), None)
            msg = build(addr, countries.display(entry["code"]), kind, user, sender_name,
                        in_reply_to=prev_id if kind == "follow_up" else None)
            if not live:
                events.append(f"[dry run] would email {entry['name']} at {addr} ({kind}, from {source})")
                sends += 1
                continue

            if sends:
                time.sleep(SEND_DELAY_SECONDS)
            try:
                mailer.send(msg)
                refused = False
            except smtplib.SMTPRecipientsRefused:
                refused = True  # Gmail rejected the address outright: treat like a bounce
            except (smtplib.SMTPException, OSError) as e:
                log.error("SMTP error, stopping sends for this run: %s", e)
                events.append(f"sending stopped early: {type(e).__name__}")
                break
            sends += 1
            entry["attempts"].append({
                "email": addr, "sent_at": st.iso(st.now_utc()), "message_id": msg["Message-ID"],
                "kind": kind, "source": source, "bounced": refused,
            })
            entry["status"] = "awaiting_reply"
            entry["send_now"] = False
            entry["updated_at"] = st.iso(st.now_utc())
            st.save(state)  # persist after every send so a crash never causes a double-send
            events.append(f"{entry['name']}: {'address refused ' if refused else 'emailed '}{addr} ({kind})")
    finally:
        if mailer:
            mailer.close()
    return events


def summary(state: dict, events: List[str], live: bool, pr_url: str) -> str:
    counts = Counter(e["status"] for e in state["countries"].values())
    review = sorted((e for e in state["countries"].values() if e["status"] in {"needs_review", "exhausted"}),
                    key=lambda e: e["name"])
    lines = [
        f"## Embassy pipeline ({'live' if live else 'DRY RUN, nothing sent or saved'})",
        "",
        "| Status | Countries |", "|---|---|",
        *[f"| {k} | {v} |" for k, v in sorted(counts.items())],
        "",
    ]
    if pr_url:
        lines += [f"**PR opened:** {pr_url}", ""]
    lines += ["### This run", *([f"- {e}" for e in events] or ["- nothing to do"]), ""]
    if review:
        lines += ["### Needs you", *[f"- **{e['name']}** ({e['status']}): {e.get('review_reason') or ''}"
                                     for e in review], ""]
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--live", action="store_true", help="send email, open PRs, save state")
    ap.add_argument("--max-sends", type=int, default=MAX_SENDS_PER_RUN)
    ap.add_argument("--only", default="", help="comma-separated ISO codes to limit sending to (testing)")
    ap.add_argument("--skip-inbox", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    for noisy in ("httpx", "google_genai"):  # per-request lines and harmless SDK notices
        logging.getLogger(noisy).setLevel(logging.ERROR)
    load_dotenv_if_present()
    user, password = env("EMAIL_USER"), env("EMAIL_PASSWORD")
    api_key = env("GEMINI_API_KEY")
    sender_name = os.getenv("SENDER_NAME", "").strip() or "Mason"
    only = {c.strip().upper() for c in args.only.split(",") if c.strip()}
    now = st.now_utc()

    countries = Countries()
    state = st.load()
    events: List[str] = []
    pr_url = ""
    failed = False

    with tempfile.TemporaryDirectory() as tmp:
        repo = travel_check.clone(Path(tmp) / "rtd-travel-check")
        data = travel_check.load_data(repo)
        covered = countries.covered_codes(data)
        added = st.sync_targets(state, countries, covered, now)
        if added:
            events.append(f"queued {len(added)} countries missing from data.json")

        for entry in state["countries"].values():
            if entry["status"] == "pr_open" and entry.get("pr_url"):
                s = travel_check.pr_state(entry["pr_url"])
                if s == "CLOSED":
                    entry["status"] = "needs_review"
                    entry["review_reason"] = f"PR closed without merging: {entry['pr_url']}"
                    events.append(f"{entry['name']}: PR was closed without merging")

        if not args.skip_inbox:
            with Inbox(user, password) as inbox:
                history = st.record_sent_history(state, countries, inbox.sent_inquiries())
                if history:
                    events.append(f"recorded earlier Gmail-sent inquiries for {len(history)} countries")
                events += process_inbox(state, countries, inbox, api_key, now)

        events += send_due(state, countries, args.live, args.max_sends, user, password, api_key, sender_name, now, only)

        answered = sorted((e for e in state["countries"].values() if e["status"] == "answered"),
                          key=lambda e: e["name"])
        if answered and args.live:
            try:
                pr_url = travel_check.open_pr(repo, answered, countries, now.strftime("%Y-%m-%d"))
                for e in answered:
                    e["status"], e["pr_url"], e["updated_at"] = "pr_open", pr_url, st.iso(now)
            except Exception as e:  # answers stay "answered" and are retried next run
                log.error("Opening the rtd-travel-check PR failed: %s", e)
                events.append(f"PR failed, will retry next run: {str(e)[:200]}")
                failed = True
        elif answered:
            events.append(f"[dry run] would open a PR for: {', '.join(e['name'] for e in answered)}")

    if args.live:
        st.save(state)

    report = summary(state, events, args.live, pr_url)
    print("\n" + report)
    step_summary = os.getenv("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a", encoding="utf-8") as f:
            f.write(report + "\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
