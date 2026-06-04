import email
import email.header
import email.utils
import imaplib
import json
import logging
import os
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

from google import genai
from dotenv import load_dotenv

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('gmail_monitor.log'),
        logging.StreamHandler()
    ]
)

CURRENT_DIR = Path(__file__).parent
DATA_JSON_PATH = CURRENT_DIR.parent.parent / 'rtd-travel-check' / 'data.json'


class GmailMonitor:
    IMAP_HOST = 'imap.gmail.com'
    NO_REPLY_DAYS = 7

    def __init__(self):
        load_dotenv(CURRENT_DIR / '.env')
        self.email_user = os.getenv('EMAIL_USER')
        self.email_password = os.getenv('EMAIL_PASSWORD')
        self.api_key = os.getenv('GEMINI_API_KEY')

        self.sent_log_path = CURRENT_DIR / 'sent_emails.json'
        self.processed_ids_path = CURRENT_DIR / 'processed_message_ids.json'

        if not self.email_user or not self.email_password:
            raise ValueError("EMAIL_USER and EMAIL_PASSWORD must be set in .env")

    # ── persistence ──────────────────────────────────────────────────────────

    def load_sent_log(self):
        with open(self.sent_log_path, encoding='utf-8') as f:
            return json.load(f)

    def save_sent_log(self, data):
        with open(self.sent_log_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    def load_processed_ids(self):
        if self.processed_ids_path.exists():
            with open(self.processed_ids_path) as f:
                return set(json.load(f))
        return set()

    def save_processed_ids(self, ids):
        with open(self.processed_ids_path, 'w') as f:
            json.dump(list(ids), f)

    # ── IMAP helpers ─────────────────────────────────────────────────────────

    def connect(self):
        mail = imaplib.IMAP4_SSL(self.IMAP_HOST)
        mail.sock.settimeout(30)
        mail.login(self.email_user, self.email_password)
        return mail

    def fetch_messages(self, mail, *search_criteria):
        mail.select('INBOX', readonly=True)
        _, uids = mail.uid('search', None, *search_criteria)
        messages = []
        if not uids[0]:
            return messages
        for uid in uids[0].split():
            _, data = mail.uid('fetch', uid, '(RFC822)')
            if data and data[0]:
                msg = email.message_from_bytes(data[0][1])
                messages.append((uid.decode(), msg))
        return messages

    def fetch_headers_only(self, mail, *search_criteria):
        """Fetch only headers (no body) — fast pre-filter before downloading full messages."""
        mail.select('INBOX', readonly=True)
        _, uids = mail.uid('search', None, *search_criteria)
        if not uids[0]:
            return []
        results = []
        for uid in uids[0].split():
            _, data = mail.uid('fetch', uid, '(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT MESSAGE-ID)])')
            if data and data[0]:
                msg = email.message_from_bytes(data[0][1])
                results.append((uid.decode(), msg))
        return results

    def get_body(self, msg):
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == 'text/plain':
                    try:
                        return part.get_payload(decode=True).decode(
                            part.get_content_charset() or 'utf-8', errors='replace'
                        )
                    except Exception:
                        pass
        else:
            try:
                return msg.get_payload(decode=True).decode(
                    msg.get_content_charset() or 'utf-8', errors='replace'
                )
            except Exception:
                pass
        return ''

    def decode_header(self, value):
        if not value:
            return ''
        parts = []
        for chunk, charset in email.header.decode_header(value):
            if isinstance(chunk, bytes):
                parts.append(chunk.decode(charset or 'utf-8', errors='replace'))
            else:
                parts.append(chunk)
        return ''.join(parts)

    # ── bounce detection ──────────────────────────────────────────────────────

    def check_bounces(self, mail, sent_log, processed_ids):
        logging.info("Checking for bounce notifications...")

        # reverse map: embassy email address → country name
        email_to_country = {
            v['email'].lower(): k
            for k, v in sent_log.items()
            if 'email' in v
        }

        bounce_senders = ['mailer-daemon@googlemail.com', 'mailer-daemon@google.com']
        found = 0

        for sender in bounce_senders:
            try:
                for uid, msg in self.fetch_messages(mail, 'FROM', f'"{sender}"'):
                    msg_id = msg.get('Message-ID', uid)
                    if msg_id in processed_ids:
                        continue

                    body = self.get_body(msg)
                    subject = self.decode_header(msg.get('Subject', ''))
                    haystack = (body + subject).lower()

                    bounced_to = next(
                        (addr for addr in email_to_country if addr in haystack), None
                    )

                    if bounced_to:
                        country = email_to_country[bounced_to]
                        sent_log[country]['status'] = 'invalid_email'
                        sent_log[country]['bounce_date'] = datetime.now().isoformat()
                        logging.info(f"  ✗ {country} — invalid email ({bounced_to})")
                        found += 1

                    processed_ids.add(msg_id)
            except Exception as e:
                logging.error(f"Error checking bounces from {sender}: {e}")

        logging.info(f"Bounce check done — {found} invalid email(s) found")
        return sent_log, processed_ids

    # ── reply detection ───────────────────────────────────────────────────────

    def check_replies(self, mail, sent_log, processed_ids):
        logging.info("Checking for embassy replies...")

        # reverse map: embassy email → country
        email_to_country = {
            v['email'].lower(): k
            for k, v in sent_log.items()
            if 'email' in v
        }

        # earliest sent date — only look at emails since the campaign started
        sent_dates = [
            v['sent_date'] for v in sent_log.values()
            if 'sent_date' in v
        ]
        if sent_dates:
            earliest = datetime.fromisoformat(min(sent_dates))
            since = earliest.strftime('%d-%b-%Y')  # e.g. "15-Jan-2025"
        else:
            since = '01-Jan-2025'

        # Step 1: fetch headers only for all inbox mail since campaign start
        # (fast — no body download yet)
        headers = self.fetch_headers_only(mail, 'SINCE', since)
        logging.info(f"  {len(headers)} inbox message(s) since {since} — scanning headers...")

        # Step 2: from those, pick only messages from known embassy senders
        # or with our subject in the subject line, that we haven't processed yet
        candidates = []
        for uid, hdr in headers:
            msg_id = hdr.get('Message-ID', uid)
            if msg_id in processed_ids:
                continue
            sender = email.utils.parseaddr(hdr.get('From', ''))[1].lower()
            subject = self.decode_header(hdr.get('Subject', ''))
            if sender == self.email_user.lower():
                continue
            country = email_to_country.get(sender)
            if not country:
                country = next(
                    (c for c in sent_log if c.lower() in subject.lower()), None
                )
            if country:
                candidates.append((uid, msg_id, sender, subject, country))

        logging.info(f"  {len(candidates)} candidate reply/replies to fetch in full...")

        # Step 3: only now download the full body for actual candidates
        found = 0
        for uid, msg_id, sender, subject, country in candidates:
            if sent_log[country].get('status') == 'replied':
                processed_ids.add(msg_id)
                continue

            _, data = mail.uid('fetch', uid, '(RFC822)')
            if not data or not data[0]:
                continue
            msg = email.message_from_bytes(data[0][1])
            body = self.get_body(msg)
            logging.info(f"  ✉ Reply from {country} ({sender})")

            visa_info = self.parse_reply(country, body)

            sent_log[country]['status'] = 'replied'
            sent_log[country]['reply_date'] = datetime.now().isoformat()
            sent_log[country]['raw_reply'] = body[:2000]

            if visa_info and visa_info.get('visaRequirement', 'Unknown') != 'Unknown':
                # use the country name Gemini detected from the reply body
                # (catches cases where sent_log key is a placeholder like "Country2")
                actual_country = visa_info.pop('country', country) or country
                if actual_country != country:
                    logging.info(f"  Country name corrected: '{country}' → '{actual_country}'")
                self.update_data_json(actual_country, visa_info)
                pr_url = self.create_pr(actual_country)
                if pr_url:
                    sent_log[country]['pr_url'] = pr_url
                    logging.info(f"  → PR created: {pr_url}")
            else:
                logging.info(f"  → Reply stored but visa info unclear — needs manual review")

            processed_ids.add(msg_id)
            found += 1

        logging.info(f"Reply check done — {found} new reply/replies found")
        return sent_log, processed_ids

    # ── no-reply sweep ────────────────────────────────────────────────────────

    def check_no_replies(self, sent_log):
        logging.info(f"Checking for no-replies older than {self.NO_REPLY_DAYS} days...")
        cutoff = datetime.now() - timedelta(days=self.NO_REPLY_DAYS)
        count = 0
        for country, info in sent_log.items():
            if info.get('status') == 'sent':
                try:
                    if datetime.fromisoformat(info['sent_date']) < cutoff:
                        info['status'] = 'no_reply'
                        logging.info(f"  ⏱ {country} — marked no_reply")
                        count += 1
                except (KeyError, ValueError):
                    pass
        logging.info(f"No-reply check done — {count} marked")
        return sent_log

    # ── Gemini reply parser ───────────────────────────────────────────────────

    # Exact visaRequirement values used in data.json
    VISA_REQUIREMENT_VALUES = [
        'Visa required',
        'Visa not required',
        'E-Visa',
        'Does not recognize US issued Refugee Travel Document',
    ]

    def parse_reply(self, country, body):
        if not self.api_key:
            logging.warning("No GEMINI_API_KEY — raw reply stored, skipping auto-parse")
            return None

        prompt = f"""You are extracting visa requirements for US Refugee Travel Document (RTD / Form I-571) holders from an embassy email reply.

Expected country: {country}

Embassy reply:
{body[:3000]}

Your task: return a JSON object that matches exactly this structure used in our database:

{{
  "country": <the actual country name mentioned in the reply — use this if it differs from the expected country above, otherwise use "{country}">,
  "visaRequirement": <one of the exact strings below>,
  "duration": <string — e.g. "90 days", "30 days", "6 months", or "N/A" if not mentioned>,
  "notes": <string — any extra details like application process, conditions, fees; empty string if none>
}}

Allowed values for "visaRequirement" (use EXACTLY one of these, case-sensitive):
- "Visa required"
- "Visa not required"
- "E-Visa"
- "Does not recognize US issued Refugee Travel Document"
- "Unknown" (only if the reply is an auto-response, out-of-office, or contains no visa information)

Real examples from our database for reference:
{{"country": "Albania", "visaRequirement": "Visa not required", "duration": "90 days", "notes": ""}}
{{"country": "Japan", "visaRequirement": "Visa required", "duration": "N/A", "notes": "Must apply in person at the embassy"}}
{{"country": "Kenya", "visaRequirement": "E-Visa", "duration": "90 days", "notes": "E-Visa can be applied online"}}
{{"country": "India", "visaRequirement": "Does not recognize US issued Refugee Travel Document", "duration": "N/A", "notes": ""}}

Return ONLY the JSON object — no markdown, no explanation."""

        try:
            client = genai.Client(api_key=self.api_key)
            response = client.models.generate_content(
                model='gemini-2.5-flash',
                contents=prompt,
                config=genai.types.GenerateContentConfig(
                    response_mime_type='application/json'
                )
            )
            return json.loads(response.text)
        except Exception as e:
            logging.error(f"Gemini parse failed for {country}: {e}")
            return None

    # ── data.json + PR ────────────────────────────────────────────────────────

    def update_data_json(self, country, visa_info):
        try:
            with open(DATA_JSON_PATH, encoding='utf-8') as f:
                data = json.load(f)

            for entry in data:
                if entry['country'].lower() == country.lower():
                    entry.update(visa_info)
                    logging.info(f"  Updated existing entry for {country}")
                    break
            else:
                data.append({'country': country, **visa_info})
                logging.info(f"  Added new entry for {country}")

            with open(DATA_JSON_PATH, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logging.error(f"Failed to update data.json for {country}: {e}")

    def create_pr(self, country):
        repo = DATA_JSON_PATH.parent
        slug = country.lower().replace(' ', '-').replace(',', '').replace('.', '').replace("'", '')
        branch = f'embassy-reply/{slug}'

        # remember where we are so we can return after the PR
        orig = subprocess.run(
            ['git', 'branch', '--show-current'], cwd=repo, capture_output=True, text=True
        ).stdout.strip() or 'main'

        try:
            # stash everything (including our data.json write)
            subprocess.run(['git', 'stash', '--include-untracked'], cwd=repo, check=True, capture_output=True)

            # get onto a clean main
            subprocess.run(['git', 'checkout', 'main'], cwd=repo, check=True, capture_output=True)
            subprocess.run(['git', 'pull', 'origin', 'main'], cwd=repo, capture_output=True)

            # new branch
            subprocess.run(['git', 'checkout', '-b', branch], cwd=repo, check=True, capture_output=True)

            # restore stash so data.json is updated on this branch
            subprocess.run(['git', 'stash', 'pop'], cwd=repo, check=True, capture_output=True)

            # commit only data.json (ignore .DS_Store etc.)
            subprocess.run(['git', 'add', 'data.json'], cwd=repo, check=True, capture_output=True)
            subprocess.run(
                ['git', 'commit', '-m', f'Add visa info for {country} from embassy reply'],
                cwd=repo, check=True, capture_output=True
            )
            subprocess.run(['git', 'push', 'origin', branch], cwd=repo, check=True, capture_output=True)

            result = subprocess.run(
                ['gh', 'pr', 'create',
                 '--title', f'Add visa info for {country}',
                 '--body', f'Auto-generated from embassy reply for **{country}**.\n\n🤖 Generated by rtd-email-service'],
                cwd=repo, capture_output=True, text=True
            )

            # return to original branch
            subprocess.run(['git', 'checkout', orig], cwd=repo, capture_output=True)

            if result.returncode == 0:
                return result.stdout.strip()
            else:
                logging.error(f"gh pr create failed: {result.stderr.strip()}")
        except subprocess.CalledProcessError as e:
            logging.error(f"Git error for {country}: {e}")
            subprocess.run(['git', 'checkout', orig], cwd=repo, capture_output=True)

        return None

    # ── reparse utility ───────────────────────────────────────────────────────

    def reparse_pending(self):
        """Re-run Gemini parse on replied entries that never got a PR (e.g. due to expired API key)."""
        sent_log = self.load_sent_log()
        pending = {
            country: info for country, info in sent_log.items()
            if info.get('status') == 'replied'
            and 'pr_url' not in info
            and info.get('raw_reply')
        }

        if not pending:
            logging.info("No pending replies to reparse")
            return

        logging.info(f"Reparsing {len(pending)} replied entries without a PR...")
        for country, info in pending.items():
            logging.info(f"  Reparsing {country}...")
            visa_info = self.parse_reply(country, info['raw_reply'])
            if visa_info and visa_info.get('visaRequirement', 'Unknown') != 'Unknown':
                actual_country = visa_info.pop('country', country) or country
                if actual_country != country:
                    logging.info(f"  Country name corrected: '{country}' → '{actual_country}'")
                self.update_data_json(actual_country, visa_info)
                pr_url = self.create_pr(actual_country)
                if pr_url:
                    sent_log[country]['pr_url'] = pr_url
                    logging.info(f"  → PR created: {pr_url}")
            else:
                logging.info(f"  → {country}: reply has no clear visa info — needs manual follow-up")
                logging.info(f"     Raw reply: {info['raw_reply'][:200]}")

        self.save_sent_log(sent_log)

    # ── main ──────────────────────────────────────────────────────────────────

    def run(self):
        logging.info("=" * 50)
        logging.info("Gmail Monitor started")

        sent_log = self.load_sent_log()
        processed_ids = self.load_processed_ids()

        try:
            mail = self.connect()
            logging.info("Connected to Gmail IMAP")
            sent_log, processed_ids = self.check_bounces(mail, sent_log, processed_ids)
            sent_log, processed_ids = self.check_replies(mail, sent_log, processed_ids)
            mail.logout()
        except Exception as e:
            logging.error(f"IMAP error: {e}")

        sent_log = self.check_no_replies(sent_log)

        self.save_sent_log(sent_log)
        self.save_processed_ids(processed_ids)

        # summary
        statuses = {}
        for info in sent_log.values():
            s = info.get('status', 'unknown')
            statuses[s] = statuses.get(s, 0) + 1
        logging.info(f"Summary: {statuses}")
        logging.info("Gmail Monitor done")


if __name__ == '__main__':
    monitor = GmailMonitor()
    monitor.run()
