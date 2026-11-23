# rtd-email-service

Asks embassies whether holders of a US Refugee Travel Document (Form I-571) can visit,
and turns their answers into pull requests for [rtd-travel-check](https://github.com/masonn99/rtd-travel-check)'s `data.json`.

## How it works

A GitHub Actions workflow (`.github/workflows/embassy-pipeline.yml`) runs `python -m pipeline.run` once a day:

1. **Find gaps.** Clones rtd-travel-check and queues every sovereign country (UN members plus the Holy See, Taiwan and Kosovo) that is not yet in `data.json`. Territories are skipped.
2. **Rebuild history.** Reads Gmail's Sent Mail for earlier inquiries, including the January 2025 campaign and anything sent by hand, so no embassy gets a duplicate first email.
3. **Read the inbox** (read-only):
   - **Bounces:** the address is marked dead, and the country is retried on the same run.
   - **Replies:** Gemini reads each one and classifies it.
     - *Answer:* queued for the PR.
     - *Embassy asks something back, or the answer is unclear:* listed under **Needs you**.
     - *Embassy says to write somewhere else:* that address goes to the front of the queue.
     - *Auto-reply:* ignored.
4. **Email countries that are due.** A country is due when it has never been contacted, or when it got no reply for 3 days. Each retry tries, in order:
   - the next known address,
   - a new address found by Gemini with Google Search,
   - a single follow-up to the last address that worked.

   After 3 emails, or 3 searches that find no address, the country is marked `exhausted` and listed for you. A run sends at most 15 emails, 45 seconds apart.
5. **Open one PR** on rtd-travel-check with every answered country. The PR quotes the sentence each embassy wrote, so you can check it before merging. Once merged, those countries are marked `done`.
6. **Save** `pipeline/state.json` back to this repo and write a run summary on the Actions page.

### Trusting addresses found online
An address from a web search is only emailed if its domain has mail servers (MX records) **and** one of these holds:
- the domain matches a website Google Search actually returned, or
- the address appears in a search-grounded sentence and the domain looks official (`gov`, `mfa`, `embassy`, `consul`, …).

This filters out visa agencies and made-up addresses.

### Privacy
This repo is public. `state.json` stores only public embassy addresses, dates, statuses and the one-sentence evidence quote that also goes into the PR. Full reply text is never saved.

## Setup (GitHub Actions)

**Secrets** (Settings → Secrets and variables → Actions → Secrets):

| Secret | What |
|---|---|
| `EMAIL_USER` | Gmail address that sends and receives |
| `EMAIL_PASSWORD` | A Gmail [app password](https://myaccount.google.com/apppasswords) for that account |
| `GEMINI_API_KEY` | Google AI Studio key |
| `TRAVEL_CHECK_TOKEN` | Fine-grained GitHub token for **rtd-travel-check** only, with *Contents: read and write* and *Pull requests: read and write* |

**Variables** (same page, Variables tab):

| Variable | What |
|---|---|
| `SEND_ENABLED` | Set to `true` to let the daily schedule actually send. Until then, scheduled runs are dry runs. |
| `SENDER_NAME` | Name used to sign the emails (default `Mason`) |

To run it by hand: **Actions → Embassy pipeline → Run workflow**. Leave *live* unticked for a dry run, which shows exactly who would be emailed and changes nothing.

## Running locally

```bash
python3 -m venv .venv && .venv/bin/pip install -r pipeline/requirements.txt
# credentials go in emailService/.env: EMAIL_USER, EMAIL_PASSWORD, GEMINI_API_KEY (+ TRAVEL_CHECK_TOKEN for --live PRs)
.venv/bin/python -m pipeline.run                 # dry run
.venv/bin/python -m pipeline.run --live --only AO --max-sends 1   # really email one country
.venv/bin/python -m unittest discover tests      # offline tests
```

Tuning knobs (retry days, attempt limits, send caps, models) are in `pipeline/config.py`.

## Files

- `pipeline/`: the automated pipeline
  - `countries.json`: generated from the `country-list` package rtd-travel-check uses, so new rows always map to the right flag and ISO code
  - `state.json`: per-country progress (created on the first live run)
- `emailService/`, `emailScrapper/`: the original one-off scripts. `RefugeeTravelDocument.pdf` (the public CBP information sheet) is still attached to every email. travel.state.gov now blocks the scraper behind Cloudflare, which is why fresh addresses come from Gemini search.
