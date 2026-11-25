"""Reads Gmail over IMAP (read-only) and matches messages to countries."""
from __future__ import annotations

import email
import email.header
import email.utils
import imaplib
import re
from dataclasses import dataclass
from datetime import datetime
from email.message import Message
from html import unescape
from typing import Callable, Dict, Iterator, List, Optional

from .config import IMAP_HOST

BOUNCE_SENDERS = ("mailer-daemon@", "postmaster@")


@dataclass
class Incoming:
    uid: str
    message_id: str
    sender: str
    subject: str
    date: Optional[datetime]
    in_reply_to: List[str]
    msg: Optional[Message] = None  # full message, fetched only when wanted

    @property
    def is_bounce(self) -> bool:
        return self.sender.startswith(BOUNCE_SENDERS)


def decode(value: Optional[str]) -> str:
    if not value:
        return ""
    out = []
    for chunk, charset in email.header.decode_header(value):
        if isinstance(chunk, bytes):
            out.append(chunk.decode(charset or "utf-8", errors="replace"))
        else:
            out.append(chunk)
    return "".join(out)


def _ids(value: Optional[str]) -> List[str]:
    return re.findall(r"<[^<>\s]+>", value or "")


def body_text(msg: Message) -> str:
    """Plain-text body; falls back to de-tagged HTML."""
    plain, html = [], []
    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.get_content_maintype() == "multipart" or part.get_filename():
            continue
        try:
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        except (LookupError, AssertionError):
            continue
        if part.get_content_type() == "text/plain":
            plain.append(text)
        elif part.get_content_type() == "text/html":
            html.append(text)
    if plain:
        return "\n".join(plain)
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", "\n".join(html))
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", text)
    return unescape(re.sub(r"<[^>]+>", " ", text))


def strip_quoted(text: str) -> str:
    """Drop our quoted original message so the LLM reads only the embassy's words."""
    lines = []
    for line in text.splitlines():
        if re.match(r"^\s*On .+wrote:\s*$", line) or re.match(r"^-{2,}\s*Original Message", line, re.I):
            break
        if line.lstrip().startswith(">"):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def match_country(
    item: Incoming,
    sent_ids: Dict[str, str],
    sent_emails: Dict[str, str],
    subject_names: Dict[str, str],
) -> Optional[str]:
    """Which country a reply belongs to: threading, then sender, then subject."""
    for mid in item.in_reply_to:
        if mid in sent_ids:
            return sent_ids[mid]
    if item.sender in sent_emails:
        return sent_emails[item.sender]
    domain = item.sender.rsplit("@", 1)[-1]
    domain_hits = {code for addr, code in sent_emails.items() if addr.rsplit("@", 1)[-1] == domain}
    if len(domain_hits) == 1 and domain not in FREEMAIL:
        return domain_hits.pop()
    if "refugee travel document" in item.subject.lower():
        subj = item.subject.lower()
        hits = {code for name, code in subject_names.items() if name in subj}
        if len(hits) == 1:
            return hits.pop()
    return None


def bounced_addresses(item: Incoming, candidates: List[str]) -> List[str]:
    """Which of our recipient addresses a bounce message is about."""
    assert item.msg is not None
    failed = item.msg.get_all("X-Failed-Recipients") or []
    haystack = (" ".join(failed) + "\n" + item.msg.as_string()).lower()
    return [a for a in candidates if a in haystack]


FREEMAIL = {"gmail.com", "googlemail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com", "icloud.com"}


class Inbox:
    def __init__(self, user: str, password: str):
        self.user = user.lower()
        self.password = password
        self.conn: Optional[imaplib.IMAP4_SSL] = None

    def __enter__(self) -> "Inbox":
        self.conn = imaplib.IMAP4_SSL(IMAP_HOST, timeout=60)
        self.conn.login(self.user, self.password)
        self.conn.select("INBOX", readonly=True)
        return self

    def __exit__(self, *exc) -> None:
        if self.conn:
            try:
                self.conn.logout()
            except imaplib.IMAP4.error:
                pass

    def sent_inquiries(self) -> List[dict]:
        """Our RTD inquiries in Gmail's Sent Mail (to rebuild history, incl. manual sends)."""
        assert self.conn
        try:
            self.conn.select('"[Gmail]/Sent Mail"', readonly=True)
            _, data = self.conn.uid("search", None, "SUBJECT", '"Refugee Travel Document"')
            out = []
            for uid in (data[0].split() if data and data[0] else []):
                _, hdr = self.conn.uid("fetch", uid, "(BODY.PEEK[HEADER.FIELDS (TO SUBJECT DATE MESSAGE-ID)])")
                if not hdr or not hdr[0] or not isinstance(hdr[0], tuple):
                    continue
                h = email.message_from_bytes(hdr[0][1])
                try:
                    date = email.utils.parsedate_to_datetime(h.get("Date"))
                except (TypeError, ValueError):
                    continue
                out.append({
                    "to": email.utils.parseaddr(h.get("To", ""))[1],
                    "subject": decode(h.get("Subject")),
                    "date": date,
                    "message_id": (h.get("Message-ID") or "").strip(),
                })
            return out
        finally:
            self.conn.select("INBOX", readonly=True)

    def messages_since(
        self, since: datetime, skip_ids: set, want: Callable[[Incoming], bool]
    ) -> Iterator[Incoming]:
        """Yield messages whose headers pass `want`, with the full message loaded."""
        assert self.conn
        _, data = self.conn.uid("search", None, "SINCE", since.strftime("%d-%b-%Y"))
        uids = data[0].split() if data and data[0] else []
        for uid in uids:
            _, hdr = self.conn.uid(
                "fetch", uid,
                "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT MESSAGE-ID IN-REPLY-TO REFERENCES DATE)])",
            )
            if not hdr or not hdr[0] or not isinstance(hdr[0], tuple):
                continue
            h = email.message_from_bytes(hdr[0][1])
            mid = (h.get("Message-ID") or f"<uid-{uid.decode()}@imap>").strip()
            sender = email.utils.parseaddr(h.get("From", ""))[1].lower()
            if mid in skip_ids or sender == self.user:
                continue
            try:
                date = email.utils.parsedate_to_datetime(h.get("Date"))
            except (TypeError, ValueError):
                date = None
            item = Incoming(
                uid=uid.decode(),
                message_id=mid,
                sender=sender,
                subject=decode(h.get("Subject")),
                date=date,
                in_reply_to=_ids(h.get("In-Reply-To")) + _ids(h.get("References")),
            )
            if not want(item):
                continue
            _, full = self.conn.uid("fetch", uid, "(BODY.PEEK[])")
            if not full or not full[0] or not isinstance(full[0], tuple):
                continue
            item.msg = email.message_from_bytes(full[0][1])
            yield item
