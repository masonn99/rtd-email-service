from __future__ import annotations

import os
from pathlib import Path

PIPELINE_DIR = Path(__file__).resolve().parent
REPO_ROOT = PIPELINE_DIR.parent

STATE_PATH = PIPELINE_DIR / "state.json"
COUNTRIES_PATH = PIPELINE_DIR / "countries.json"
ATTACHMENT_PATH = REPO_ROOT / "emailService" / "RefugeeTravelDocument.pdf"

# ── Policy ────────────────────────────────────────────────────────────────────
RETRY_AFTER_DAYS = 3          # no reply after this long → try again
MAX_ATTEMPTS = 3              # total emails per country before giving up
MAX_SENDS_PER_RUN = 15        # keep Gmail happy and avoid looking like spam
SEND_DELAY_SECONDS = 45       # pause between emails
MAX_SEARCHES_PER_RUN = 8      # Gemini + Google Search lookups per run (free tier is small)
MAX_SEARCHES_PER_COUNTRY = 3  # stop searching for a country after this many misses
MIN_CONFIDENCE = 0.7          # below this, a parsed reply goes to manual review

# Countries never emailed (the RTD is issued by the US itself).
EXCLUDED_CODES = {"US"}

# ── Integrations ──────────────────────────────────────────────────────────────
# Separate models → separate free-tier quotas: a light model reads replies,
# a stronger one runs the Google-Search-grounded email lookups.
# Reply parsing tries each model in order, moving on when one's daily quota runs out.
GEMINI_PARSE_MODELS = [m.strip() for m in os.getenv(
    "GEMINI_PARSE_MODELS", "gemini-3.5-flash,gemini-3.5-flash-lite").split(",") if m.strip()]
GEMINI_SEARCH_MODEL = os.getenv("GEMINI_SEARCH_MODEL", "gemini-3.5-flash")
GEMINI_RPM = float(os.getenv("GEMINI_RPM", "5"))  # per model; conservative free-tier pace
TRAVEL_CHECK_REPO = os.getenv("TRAVEL_CHECK_REPO", "masonn99/rtd-travel-check")
TRAVEL_CHECK_BASE = os.getenv("TRAVEL_CHECK_BASE", "main")
IMAP_HOST = "imap.gmail.com"
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587

# Exact visaRequirement strings data.json uses.
VISA_REQUIREMENTS = [
    "Visa required",
    "Visa not required",
    "E-Visa",
    "Does not recognize US issued Refugee Travel Document",
]


def env(name: str, required: bool = True) -> str:
    value = os.getenv(name, "").strip()
    if required and not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def load_dotenv_if_present() -> None:
    """Local runs read emailService/.env; in GitHub Actions secrets come from env."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(REPO_ROOT / "emailService" / ".env")
    load_dotenv(REPO_ROOT / ".env")
