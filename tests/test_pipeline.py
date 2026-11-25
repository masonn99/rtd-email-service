"""Offline tests for the embassy pipeline.  Run: python -m unittest discover tests"""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

from pipeline import scheduler, state as st, travel_check
from pipeline.countries import Countries
from pipeline.inbox import Incoming, bounced_addresses, match_country, strip_quoted
from pipeline.llm import normalize_parsed, select_emails
from pipeline.mailer import build

NOW = datetime(2026, 11, 23, 15, 0, tzinfo=timezone.utc)
C = Countries()


def entry(code="AO", attempts=(), status=None, candidates=None):
    e = st.new_entry(code, C)
    if candidates is not None:
        e["candidate_emails"] = [{"email": x, "source": "state.gov"} for x in candidates]
    e["attempts"] = [dict(a) for a in attempts]
    e["status"] = status or ("awaiting_reply" if attempts else "queued")
    return e


def attempt(email, days_ago, kind="initial", bounced=False, mid=None):
    return {"email": email, "sent_at": st.iso(NOW - timedelta(days=days_ago)), "message_id": mid,
            "kind": kind, "source": "state.gov", "bounced": bounced}


class CountryMatching(unittest.TestCase):
    def test_data_json_names_resolve(self):
        for name, code in [("Türkiye", "TR"), ("Turkey", "TR"), ("Korea, Republic of", "KR"),
                           ("United Kingdom of Great Britain and Northern Ireland", "GB"),
                           ("Taiwan, Province of China", "TW"), ("Viet Nam", "VN"), ("Kosovo", "XK"),
                           ("Czechia", "CZ"), ("Macedonia", "MK"), ("Congo the Democratic Republic of the", "CD")]:
            self.assertEqual(C.code_for(name), code, name)

    def test_territories_are_not_targets(self):
        for name in ["Hong Kong", "Macao", "Greenland", "Bouvet Island", "Aruba"]:
            self.assertIsNone(C.code_for(name))

    def test_sovereign_list(self):
        self.assertEqual(len(C.items), 196)

    def test_sync_targets_queues_missing_and_closes_covered(self):
        s = {"processed_message_ids": [], "countries": {}}
        st.sync_targets(s, C, {"AL", "JP"}, NOW)
        self.assertNotIn("AL", s["countries"])
        self.assertNotIn("US", s["countries"])  # excluded
        self.assertEqual(s["countries"]["AO"]["status"], "queued")
        self.assertIn("visa.consular@angola.org", [c["email"] for c in s["countries"]["AO"]["candidate_emails"]])
        st.sync_targets(s, C, {"AL", "JP", "AO"}, NOW)  # Angola's PR merged
        self.assertEqual(s["countries"]["AO"]["status"], "done")


class Scheduling(unittest.TestCase):
    def test_first_contact_is_due(self):
        self.assertTrue(scheduler.is_due(entry(), NOW))

    def test_waits_three_days(self):
        self.assertFalse(scheduler.is_due(entry(attempts=[attempt("a@x.org", 2)]), NOW))
        self.assertTrue(scheduler.is_due(entry(attempts=[attempt("a@x.org", 3)]), NOW))

    def test_unread_reply_blocks_sending(self):
        e = entry(attempts=[attempt("a@x.org", 30)])
        e["reply_pending"] = True
        self.assertFalse(scheduler.is_due(e, NOW))

    def test_bounce_is_due_immediately(self):
        self.assertTrue(scheduler.is_due(entry(attempts=[attempt("a@x.org", 0, bounced=True)]), NOW))

    def test_terminal_statuses_never_due(self):
        for s in ["answered", "pr_open", "done", "needs_review", "exhausted"]:
            self.assertFalse(scheduler.is_due(entry(attempts=[attempt("a@x.org", 30)], status=s), NOW))

    def test_retry_prefers_untried_address(self):
        e = entry(candidates=["a@x.org", "b@x.org"], attempts=[attempt("a@x.org", 4)])
        self.assertEqual(scheduler.next_untried(e), ("b@x.org", "retry", "state.gov"))

    def test_embassy_redirect_jumps_the_queue(self):
        e = entry(candidates=["a@x.org", "b@x.org", "c@x.org"], attempts=[attempt("a@x.org", 4)])
        st.add_candidate(e, "c@x.org", "embassy_redirect", first=True)
        self.assertEqual(scheduler.next_untried(e), ("c@x.org", "retry", "embassy_redirect"))
        self.assertEqual(len(e["candidate_emails"]), 3)

    def test_follow_up_once_per_address(self):
        e = entry(candidates=["a@x.org"], attempts=[attempt("a@x.org", 4)])
        self.assertIsNone(scheduler.next_untried(e))
        self.assertEqual(scheduler.follow_up(e)[1], "follow_up")
        e["attempts"].append(attempt("a@x.org", 0, kind="follow_up"))
        self.assertIsNone(scheduler.follow_up(e))

    def test_no_follow_up_to_bounced(self):
        e = entry(candidates=["a@x.org"], attempts=[attempt("a@x.org", 4, bounced=True)])
        self.assertIsNone(scheduler.follow_up(e))

    def test_max_attempts(self):
        e = entry(attempts=[attempt("a@x.org", 9), attempt("b@x.org", 6), attempt("c@x.org", 3)])
        self.assertTrue(scheduler.out_of_attempts(e))


class ReplyMatching(unittest.TestCase):
    def setUp(self):
        self.ids = {"<abc@gmail.com>": "AO"}
        self.emails = {"visa.consular@angola.org": "AO", "info@embassy-dz.org": "DZ"}
        self.names = {"angola": "AO", "algeria": "DZ"}

    def item(self, sender, subject="Re: hello", refs=()):
        return Incoming("1", "<m@x>", sender, subject, None, list(refs))

    def test_by_thread(self):
        self.assertEqual(match_country(self.item("someone@else.com", refs=["<abc@gmail.com>"]),
                                       self.ids, self.emails, self.names), "AO")

    def test_by_sender_and_domain(self):
        self.assertEqual(match_country(self.item("visa.consular@angola.org"), self.ids, self.emails, self.names), "AO")
        self.assertEqual(match_country(self.item("consul@angola.org"), self.ids, self.emails, self.names), "AO")

    def test_freemail_domain_does_not_match_by_domain(self):
        emails = {"embassy.x@gmail.com": "AO"}
        self.assertIsNone(match_country(self.item("random@gmail.com"), {}, emails, self.names))

    def test_by_subject(self):
        it = self.item("noreply@portal.example", "Re: Inquiry: US Refugee Travel Document holders - Algeria")
        self.assertEqual(match_country(it, self.ids, self.emails, self.names), "DZ")

    def test_unrelated(self):
        self.assertIsNone(match_country(self.item("news@shop.com", "Sale!"), self.ids, self.emails, self.names))

    def test_bounce_addresses(self):
        m = EmailMessage()
        m["X-Failed-Recipients"] = "info@embassy-dz.org"
        m.set_content("Delivery failed")
        it = Incoming("1", "<b@x>", "mailer-daemon@googlemail.com", "Delivery Status", None, [], m)
        self.assertTrue(it.is_bounce)
        self.assertEqual(bounced_addresses(it, list(self.emails)), ["info@embassy-dz.org"])

    def test_strip_quoted(self):
        body = "No visa needed for 30 days.\n\nOn Mon, 1 Jan 2026, Mason wrote:\n> Dear embassy\n> question"
        self.assertEqual(strip_quoted(body), "No visa needed for 30 days.")


class EmailFinder(unittest.TestCase):
    def test_accepts_domain_from_search_source(self):
        got = select_emails("visa.consular@angola.org | https://angola.org/contact", ["angola.org"], [], [],
                            mx_ok=lambda d: True)
        self.assertEqual(got, ["visa.consular@angola.org"])

    def test_rejects_visa_agency_and_unsourced(self):
        text = "help@passportvisasexpress.com\nrandom@unknown-site.com"
        self.assertEqual(select_emails(text, ["angola.org"], [text], [], mx_ok=lambda d: True), [])

    def test_accepts_official_looking_grounded(self):
        text = "consular@mfa.gov.xx"
        self.assertEqual(select_emails(text, [], ["Contact consular@mfa.gov.xx for visas"], [],
                                       mx_ok=lambda d: True), ["consular@mfa.gov.xx"])

    def test_rejects_without_mx_and_excluded(self):
        self.assertEqual(select_emails("a@angola.org", ["angola.org"], [], [], mx_ok=lambda d: False), [])
        self.assertEqual(select_emails("a@angola.org", ["angola.org"], [], ["A@angola.org"],
                                       mx_ok=lambda d: True), [])


class ParsedReply(unittest.TestCase):
    def test_normalize(self):
        p = normalize_parsed({"category": "answer", "visaRequirement": "E-Visa", "duration": "N/A",
                              "notes": " x ", "evidence": "e", "confidence": "1.4",
                              "redirect_emails": ["bad", "ok@a.org"], "question": ""})
        self.assertEqual((p["duration"], p["notes"], p["confidence"], p["redirect_emails"]),
                         ("", "x", 1.0, ["ok@a.org"]))


class DataJson(unittest.TestCase):
    def answered(self, code, req="Visa not required"):
        e = st.new_entry(code, C)
        e["answer"] = {"visaRequirement": req, "duration": "30 days", "notes": "", "evidence": "q",
                       "confidence": 0.9, "from_domain": "x.org", "received_at": st.iso(NOW)}
        return e

    def test_appends_new_and_skips_existing(self):
        data = [{"country": "Albania", "visaRequirement": "Visa not required", "duration": "", "notes": ""}]
        out = travel_check.apply_answers(data, [self.answered("AO"), self.answered("AL")], C)
        self.assertEqual([r["country"] for r in out], ["Albania", "Angola"])
        self.assertEqual(out[1], {"country": "Angola", "visaRequirement": "Visa not required",
                                  "duration": "30 days", "notes": ""})

    def test_dump_matches_file_style(self):
        data = [{"country": "Türkiye", "visaRequirement": "E-Visa", "duration": "", "notes": ""}]
        self.assertFalse(travel_check.dump_data(data, "[]").endswith("\n"))
        self.assertTrue(travel_check.dump_data(data, "[]\n").endswith("\n"))
        self.assertIn("Türkiye", travel_check.dump_data(data, ""))
        self.assertIn('\n  {\n    "country"', travel_check.dump_data(data, ""))

    def test_pr_body_escapes_pipes(self):
        e = self.answered("AO")
        e["answer"]["notes"] = "a | b"
        self.assertIn("a \\| b", travel_check.pr_body([e]))

    def test_new_names_match_data_json_style(self):
        # names written to data.json must resolve back to the same code
        for c in C.items:
            self.assertEqual(C.code_for(c["name"]), c["code"])


class Mail(unittest.TestCase):
    def test_build(self):
        m = build("a@x.org", "Angola", "initial", "me@gmail.com", "Mason")
        self.assertIn("Angola", m["Subject"])
        self.assertTrue(m["Message-ID"].endswith("@gmail.com>"))
        self.assertIn("Form I-571", m.get_body(("plain",)).get_content())
        self.assertEqual([p.get_filename() for p in m.iter_attachments()], ["RefugeeTravelDocument.pdf"])

    def test_follow_up_threads(self):
        m = build("a@x.org", "Angola", "follow_up", "me@gmail.com", "Mason", in_reply_to="<abc@gmail.com>")
        self.assertEqual(m["In-Reply-To"], "<abc@gmail.com>")
        self.assertTrue(m["Subject"].startswith("Re: "))


class SentHistory(unittest.TestCase):
    def sent(self, to, subject, days_ago, mid):
        return {"to": to, "subject": subject, "date": NOW - timedelta(days=days_ago), "message_id": mid}

    def fresh(self):
        s = {"processed_message_ids": [], "countries": {}}
        st.sync_targets(s, C, {"AL"}, NOW)
        return s

    def test_records_campaign_and_skips_tests_and_covered(self):
        s = self.fresh()
        changed = st.record_sent_history(s, C, [
            self.sent("me@x.com", "Visa Inquiry - US Refugee Travel Document - Country1", 700, "<1@g>"),
            self.sent("e@mfa.gov.al", "Visa Inquiry - US Refugee Travel Document - Albania", 700, "<2@g>"),
            self.sent("visa.consular@angola.org", "Visa Inquiry - US Refugee Travel Document - Angola", 700, "<3@g>"),
            self.sent("rusvisawdc@usaconsul.ru",
                      "Re: Fwd: Visa Inquiry - US Refugee Travel Document - Russian\r\n Federation", 690, "<4@g>"),
        ])
        self.assertEqual(changed, ["AO", "RU"])
        ao = s["countries"]["AO"]
        self.assertEqual((ao["status"], ao["attempts"][0]["kind"], ao["attempts"][0]["message_id"]),
                         ("awaiting_reply", "initial", "<3@g>"))
        self.assertEqual(s["countries"]["RU"]["attempts"][0]["kind"], "follow_up")
        self.assertTrue(scheduler.is_due(ao, NOW))

    def test_idempotent_and_skips_pipeline_sends(self):
        s = self.fresh()
        item = self.sent("visa.consular@angola.org", "Visa Inquiry - US Refugee Travel Document - Angola", 5, "<3@g>")
        st.record_sent_history(s, C, [item])
        self.assertEqual(st.record_sent_history(s, C, [item]), [])
        # same send without a matching Message-ID (e.g. rewritten) is still recognised
        item2 = dict(item, message_id="<other@g>", date=item["date"] + timedelta(minutes=2))
        self.assertEqual(st.record_sent_history(s, C, [item2]), [])
        self.assertEqual(len(s["countries"]["AO"]["attempts"]), 1)

    def test_new_subject_format_parses(self):
        subj = "Inquiry: entry requirements for US Refugee Travel Document holders - Bolivia"
        self.assertEqual(st.country_from_subject(subj, C), "BO")


class DisplayNames(unittest.TestCase):
    def test_display(self):
        self.assertEqual(C.display("BO"), "Bolivia")
        self.assertEqual(C.display("AO"), "Angola")
        m = build("a@x.org", C.display("IR"), "initial", "me@gmail.com", "Mason")
        self.assertIn("Embassy of Iran,", m.get_body(("plain",)).get_content())


class GeminiPacing(unittest.TestCase):
    def setUp(self):
        from pipeline import llm
        self.llm = llm
        self.saved = (llm._client, llm.GEMINI_RPM, llm._exhausted)
        llm.GEMINI_RPM, llm._exhausted = 100000, set()

    def tearDown(self):
        self.llm._client, self.llm.GEMINI_RPM, self.llm._exhausted = self.saved

    def fake(self, errors, result="ok"):
        calls = []

        class Models:
            def generate_content(self, **kw):
                calls.append(kw)
                if errors:
                    raise Exception(errors.pop(0))
                return result

        class Client:
            models = Models()
        self.llm._client = lambda key: Client()
        return calls

    def test_retries_rate_limit_then_succeeds(self):
        calls = self.fake(["429 RESOURCE_EXHAUSTED ... Please retry in 0.01s."])
        self.assertEqual(self.llm._generate("k", "m", contents="x"), "ok")
        self.assertEqual(len(calls), 2)

    def test_429_without_retry_hint_is_treated_as_daily(self):
        calls = self.fake(["429 RESOURCE_EXHAUSTED. You exceeded your current quota"])
        self.assertIsNone(self.llm.find_emails("k", "Fiji", []))
        self.assertIsNone(self.llm.find_emails("k", "Benin", []))
        self.assertEqual(len(calls), 1)

    def test_daily_quota_stops_all_calls(self):
        quota = "429 RESOURCE_EXHAUSTED GenerateRequestsPerDayPerProjectPerModel-FreeTier"
        calls = self.fake([quota, quota, quota])
        # each parse model is tried once, in order, then given up for the run
        self.assertIsNone(self.llm.parse_reply("k", "Angola", "a@b.org", "body"))
        self.assertEqual([c["model"] for c in calls], self.llm.GEMINI_PARSE_MODELS)
        self.assertIsNone(self.llm.parse_reply("k", "Benin", "a@b.org", "body"))
        self.assertEqual(len(calls), len(self.llm.GEMINI_PARSE_MODELS))  # no new calls
        # the search tool has its own quota, so it is still tried; failure ≠ "nothing found"
        self.assertIsNone(self.llm.find_emails("k", "Angola", []))
        self.assertEqual(len(calls), len(self.llm.GEMINI_PARSE_MODELS) + 1)

    def test_parse_falls_back_to_next_model(self):
        class Resp:
            text = json.dumps({"category": "answer", "visaRequirement": "Visa required", "duration": "",
                               "notes": "", "evidence": "x", "redirect_emails": [], "question": "",
                               "confidence": 0.9})
        calls = self.fake(["429 RESOURCE_EXHAUSTED GenerateRequestsPerDayPerProjectPerModel-FreeTier"], Resp())
        p = self.llm.parse_reply("k", "Angola", "a@b.org", "body")
        self.assertEqual(p["visaRequirement"], "Visa required")
        self.assertEqual([c["model"] for c in calls], self.llm.GEMINI_PARSE_MODELS[:2])


if __name__ == "__main__":
    unittest.main()
