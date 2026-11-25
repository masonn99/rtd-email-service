"""Gemini: parse embassy replies, and find fresh embassy emails via Google Search."""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Callable, Iterable, List, Optional

from .config import GEMINI_PARSE_MODELS, GEMINI_RPM, GEMINI_SEARCH_MODEL, VISA_REQUIREMENTS

log = logging.getLogger(__name__)

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

# Hints that a domain belongs to a government or diplomatic mission.
OFFICIAL_HINTS = ("gov", "gob", "gouv", "mfa", "mofa", "mae", "mrree", "mre", "minrel", "embassy",
                  "embajada", "embaixada", "ambassade", "botschaft", "consul", "diplomat", "foreign")

REPLY_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "category": {
            "type": "STRING",
            "enum": ["answer", "question", "redirect", "auto_reply", "unrelated"],
            "description": "answer = states the rule; question = asks us something back; "
                           "redirect = tells us to contact another address/office; "
                           "auto_reply = out-of-office/ticket acknowledgement; unrelated = anything else",
        },
        "visaRequirement": {"type": "STRING", "enum": VISA_REQUIREMENTS + ["Unknown"]},
        "duration": {"type": "STRING", "description": "Allowed stay such as '90 days'; empty if not stated"},
        "notes": {"type": "STRING", "description": "One or two short sentences of practical detail; empty if none"},
        "evidence": {"type": "STRING", "description": "The single sentence from the reply that states the rule, verbatim"},
        "redirect_emails": {"type": "ARRAY", "items": {"type": "STRING"}},
        "question": {"type": "STRING", "description": "Only if category is question: the embassy's own "
                     "question back to the traveller, quoted from their reply (e.g. 'Where do you reside?'). "
                     "Never repeat the traveller's original question. Empty otherwise."},
        "confidence": {"type": "NUMBER", "description": "0 to 1: how clearly the reply states the rule"},
    },
    "required": ["category", "visaRequirement", "duration", "notes", "evidence", "redirect_emails",
                 "question", "confidence"],
}

REPLY_PROMPT = """You read replies from embassies to this question: "Can a holder of a United States Refugee \
Travel Document (Form I-571) enter {country}, and is a visa required?"

The traveller's original email may be quoted inside the reply; ignore it and read only what the embassy wrote.
Classify the reply and extract the rule. Map it to visaRequirement exactly:
- "Visa not required": RTD holders can enter without a visa (visa on arrival that is free/automatic counts here; say so in notes).
- "E-Visa": they must apply online before travel.
- "Visa required": they must apply for a visa at an embassy/consulate.
- "Does not recognize US issued Refugee Travel Document": the document is not accepted for entry.
- "Unknown": the reply does not say.

Only use information the embassy actually wrote. Do not use outside knowledge. Write notes in English.

Embassy reply (from {sender}):
\"\"\"
{body}
\"\"\"
"""

FIND_PROMPT = """Find the current official email address of the Embassy of {country} in Washington, D.C. \
(or its consular section, or a consulate of {country} in the United States) that answers visa and \
entry questions. Use only official embassy, consulate or ministry websites.
{exclude}
List up to 3 addresses, one per line, as:  email | official page URL
If you cannot find one on an official site, reply NONE."""


_clients: dict = {}


def _client(api_key: str):
    """One long-lived client: a temporary one closes its HTTP session when garbage-collected."""
    if api_key not in _clients:
        from google import genai
        _clients[api_key] = genai.Client(api_key=api_key)
    return _clients[api_key]


class QuotaExhausted(Exception):
    """Daily free-tier quota is used up; stop calling Gemini this run."""


_last_call: dict = {}
_exhausted: set = set()


def _generate(api_key: str, model: str, **kwargs):
    """generate_content with free-tier pacing and retry on 429/503.

    Quotas are tracked per model, and separately for Google-Search-grounded
    calls (the search tool has its own daily allowance).
    """
    cfg = kwargs.get("config")
    key = model + ("+search" if cfg is not None and getattr(cfg, "tools", None) else "")
    if key in _exhausted:
        raise QuotaExhausted()
    for attempt in range(4):
        wait = 60.0 / GEMINI_RPM - (time.monotonic() - _last_call.get(key, 0.0))
        if wait > 0:
            time.sleep(wait)
        _last_call[key] = time.monotonic()
        try:
            return _client(api_key).models.generate_content(model=model, **kwargs)
        except Exception as e:
            msg = str(e)
            hint = re.search(r"retry in ([\d.]+)s", msg)
            if "429" in msg and ("PerDay" in msg or not hint):
                # daily allowance (no retry hint) → stop using this model/tool this run
                _exhausted.add(key)
                log.warning("Gemini quota for %s used up; remaining work waits for the next run", key)
                raise QuotaExhausted() from e
            if attempt < 3 and ("429" in msg or "503" in msg or "UNAVAILABLE" in msg):
                delay = float(hint.group(1)) + 1 if hint else 15.0 * (attempt + 1)
                log.info("Gemini busy (%s); retrying in %.0fs", msg[:3], delay)
                time.sleep(delay)
                continue
            raise


def parse_reply(api_key: str, country: str, sender: str, body: str) -> Optional[dict]:
    """Structured reading of an embassy reply; None if no model could be reached."""
    from google.genai import types
    for model in GEMINI_PARSE_MODELS:
        try:
            resp = _generate(
                api_key, model,
                contents=REPLY_PROMPT.format(country=country, sender=sender, body=body[:6000]),
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=REPLY_SCHEMA,
                    temperature=0,
                ),
            )
            return normalize_parsed(json.loads(resp.text))
        except QuotaExhausted:
            continue  # try the next model
        except Exception as e:  # network, bad JSON
            log.error("Gemini reply parse failed for %s on %s: %s", country, model, str(e)[:300])
            return None
    return None


def normalize_parsed(p: dict) -> dict:
    p["duration"] = (p.get("duration") or "").strip()
    if p["duration"].upper() in {"N/A", "NA", "-", "UNKNOWN"}:
        p["duration"] = ""
    p["notes"] = (p.get("notes") or "").strip()
    p["evidence"] = (p.get("evidence") or "").strip()[:300]
    try:
        p["confidence"] = max(0.0, min(1.0, float(p.get("confidence", 0))))
    except (TypeError, ValueError):
        p["confidence"] = 0.0
    p["redirect_emails"] = [e for e in p.get("redirect_emails") or [] if EMAIL_RE.fullmatch(e.strip())]
    return p


def has_mx(domain: str) -> bool:
    import dns.exception
    import dns.resolver
    try:
        return bool(dns.resolver.resolve(domain, "MX", lifetime=8))
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.resolver.NoNameservers, dns.exception.Timeout):
        return False


def _domain_of_title(title: str) -> str:
    t = title.lower().strip()
    t = re.sub(r"^https?://", "", t).split("/")[0]
    return t[4:] if t.startswith("www.") else t


def select_emails(
    text: str,
    source_domains: Iterable[str],
    grounded_segments: Iterable[str],
    exclude: Iterable[str],
    mx_ok: Callable[[str], bool] = has_mx,
) -> List[str]:
    """Keep only addresses we can trust enough to email.

    An address is accepted when its domain has mail servers AND either
      - its domain matches a website Google Search actually returned, or
      - it appears in a search-grounded sentence and its domain looks official.
    """
    sources = {d for d in (_domain_of_title(s) for s in source_domains) if d}
    grounded = " ".join(grounded_segments).lower()
    skip = {e.lower() for e in exclude}
    out: List[str] = []
    for raw in EMAIL_RE.findall(text):
        addr = raw.strip(".").lower()
        if addr in skip or addr in out:
            continue
        domain = addr.split("@", 1)[1]
        matches_source = any(domain == s or domain.endswith("." + s) or s.endswith("." + domain) for s in sources)
        official_and_grounded = addr in grounded and any(h in domain for h in OFFICIAL_HINTS)
        if (matches_source or official_and_grounded) and mx_ok(domain):
            out.append(addr)
    return out


def find_emails(api_key: str, country: str, exclude: List[str]) -> Optional[List[str]]:
    """Verified addresses found via Google Search; None if the search itself failed."""
    from google.genai import types
    ex = f"These addresses did not work, do not return them: {', '.join(exclude)}" if exclude else ""
    try:
        resp = _generate(
            api_key, GEMINI_SEARCH_MODEL,
            contents=FIND_PROMPT.format(country=country, exclude=ex),
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())],
                temperature=0,
            ),
        )
    except QuotaExhausted:
        return None
    except Exception as e:
        log.error("Gemini email search failed for %s: %s", country, str(e)[:300])
        return None
    text = resp.text or ""
    gm = resp.candidates[0].grounding_metadata if resp.candidates else None
    titles = [c.web.title for c in (gm.grounding_chunks or []) if c.web and c.web.title] if gm else []
    segments = [s.segment.text for s in (gm.grounding_supports or []) if s.segment and s.segment.text] if gm else []
    return select_emails(text, titles, segments, exclude)
