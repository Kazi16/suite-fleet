"""Unit tests for lanes.py (fake transport only, no live API calls)."""

import json
import os
import tempfile
import unittest
from pathlib import Path

import fleet_router
import lanes

SPARK = "muse-spark-1.3"
WONDER = "muse-wonder-1.0"
HERE = Path(__file__).resolve().parent


def make_fake(mapping_or_single):
    calls = []
    is_mapping = (
        isinstance(mapping_or_single, dict)
        and (SPARK in mapping_or_single or WONDER in mapping_or_single)
    )

    def fake(model, messages, effort):
        calls.append({"model": model, "effort": effort, "messages": messages})
        if is_mapping:
            outcome = mapping_or_single[model]
        else:
            outcome = mapping_or_single
        return dict(outcome)

    fake.calls = calls
    return fake


def make_config(ledger_file):
    lane = {"default_model": SPARK, "reasoning_effort": "low",
            "heavy_model": WONDER, "benchmark": True,
            "escalation_allowed": True}
    return {
        "api_base": "https://api.meta.ai/v1",
        "budget_cap_usd": 1000.0,
        "starting_offset_usd": 0.0,
        "ledger_path": str(ledger_file),
        "prices_usd_per_1k_tokens": {
            SPARK: {"prompt": 0.0015, "completion": 0.006},
            WONDER: {"prompt": 0.004, "completion": 0.02},
        },
        "lanes": {"mail_triage": dict(lane), "job_scoring": dict(lane),
                  "deal_eval": dict(lane), "notes_qa": dict(lane)},
    }


MAIL_SPARK = {"content": json.dumps({"category": "finance",
                                     "summary": "statement ready",
                                     "priority": "low", "confidence": "high"}),
              "prompt_tokens": 100, "completion_tokens": 40}
MAIL_WONDER = {"content": json.dumps({"category": "finance",
                                      "summary": "card statement available",
                                      "priority": "medium", "confidence": "high"}),
               "prompt_tokens": 100, "completion_tokens": 50}
JOB_SPARK = {"content": json.dumps({"score": 72, "reasons": ["a"],
                                    "confidence": "high"}),
             "prompt_tokens": 120, "completion_tokens": 45}
JOB_WONDER = {"content": json.dumps({"score": 80, "reasons": ["b"],
                                     "confidence": "high"}),
              "prompt_tokens": 120, "completion_tokens": 55}


class ParseTests(unittest.TestCase):
    def test_plain_json(self):
        parsed = lanes.parse_model_content('{"category": "x", "n": 3}')
        self.assertEqual(parsed, {"category": "x", "n": 3})

    def test_code_fenced_json(self):
        parsed = lanes.parse_model_content(
            '```json\n{"category": "spam"}\n```')
        self.assertEqual(parsed, {"category": "spam"})

    def test_prose_around_json(self):
        parsed = lanes.parse_model_content(
            'Here you go: {"verdict": "approve"} thanks')
        self.assertEqual(parsed, {"verdict": "approve"})

    def test_invalid_returns_error_placeholder(self):
        parsed = lanes.parse_model_content("no json here at all")
        self.assertTrue(parsed.get("_parse_error"))
        self.assertIn("raw", parsed)

    def test_non_dict_json_returns_error_placeholder(self):
        parsed = lanes.parse_model_content("[1, 2, 3]")
        self.assertTrue(parsed.get("_parse_error"))


class AgreementTests(unittest.TestCase):
    def test_mail_agree(self):
        self.assertTrue(lanes.lanes_agree(
            "mail_triage", {"category": "finance"}, {"category": "finance"}))

    def test_mail_disagree(self):
        self.assertFalse(lanes.lanes_agree(
            "mail_triage", {"category": "finance"}, {"category": "promo"}))

    def test_mail_missing_category_disagrees(self):
        self.assertFalse(lanes.lanes_agree(
            "mail_triage", {"category": "finance"}, {"_parse_error": True}))
        self.assertFalse(lanes.lanes_agree("mail_triage", {}, {}))

    def test_job_agree_within_tolerance(self):
        self.assertTrue(lanes.lanes_agree(
            "job_scoring", {"score": 70}, {"score": 84}))
        self.assertTrue(lanes.lanes_agree(
            "job_scoring", {"score": 70}, {"score": 85}))  # boundary: 15
        self.assertFalse(lanes.lanes_agree(
            "job_scoring", {"score": 70}, {"score": 86}))

    def test_job_missing_or_nonnumeric_disagrees(self):
        self.assertFalse(lanes.lanes_agree(
            "job_scoring", {"score": 70}, {}))
        self.assertFalse(lanes.lanes_agree(
            "job_scoring", {"score": "high"}, {"score": 70}))

    def test_job_band_thresholds(self):
        self.assertEqual(lanes.score_band(70), "strong")
        self.assertEqual(lanes.score_band(100), "strong")
        self.assertEqual(lanes.score_band(69), "possible")
        self.assertEqual(lanes.score_band(40), "possible")
        self.assertEqual(lanes.score_band(39), "weak")
        self.assertEqual(lanes.score_band(0), "weak")
        self.assertIsNone(lanes.score_band(None))
        self.assertIsNone(lanes.score_band("high"))

    def test_deal_agree(self):
        self.assertTrue(lanes.lanes_agree(
            "deal_eval", {"verdict": "approve"}, {"verdict": "approve"}))
        self.assertFalse(lanes.lanes_agree(
            "deal_eval", {"verdict": "approve"}, {"verdict": "reject"}))
        self.assertFalse(lanes.lanes_agree("deal_eval", {}, {"verdict": "x"}))

    def test_notes_not_scored(self):
        self.assertIsNone(lanes.lanes_agree(
            "notes_qa", {"answer": "a"}, {"answer": "a"}))
        self.assertIsNone(lanes.lanes_agree(
            "notes_qa", {"answer": "a"}, {"answer": "b"}))


class BatchRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.ledger = str(self.dir / "ledger.jsonl")
        self.old_key = os.environ.pop("META_MODEL_API_KEY", None)

    def tearDown(self):
        if self.old_key is not None:
            os.environ["META_MODEL_API_KEY"] = self.old_key
        self.tmp.cleanup()

    def _write_input(self, name, items):
        path = self.dir / name
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(items, fh)
        return str(path)

    def test_mail_results_shape_and_pending(self):
        cfg = make_config(self.ledger)
        fake = make_fake({SPARK: MAIL_SPARK, WONDER: MAIL_WONDER})
        inp = self._write_input("mail.json", [
            {"id": 1, "from": "a@x.com", "subject": "hi"},
            {"id": 2, "from": "b@x.com", "subject": "yo"},
        ])
        out = str(self.dir / "mail_triage-results.json")
        entries = lanes.run_lane("mail_triage", inp, output_path=out,
                                 config=cfg, ledger_path=self.ledger,
                                 transport=fake)
        self.assertEqual(len(entries), 2)
        # benchmark mode: both models called and ledgered per item
        self.assertEqual(len(fake.calls), 4)
        self.assertEqual([c["model"] for c in fake.calls],
                         [SPARK, WONDER, SPARK, WONDER])
        with open(out, encoding="utf-8") as fh:
            on_disk = json.load(fh)
        self.assertEqual(on_disk, entries)
        for entry, want_id in zip(entries, [1, 2]):
            self.assertEqual(entry["id"], want_id)
            self.assertEqual(entry["spark"]["category"], "finance")
            self.assertEqual(entry["wonder"]["category"], "finance")
            self.assertTrue(entry["agree"])
            self.assertEqual(entry["spark_priority"], "low")
            self.assertEqual(entry["wonder_priority"], "medium")
            self.assertEqual(entry["verdict"], "pending")
            self.assertTrue(entry["job_ref"])
        refs = [e["job_ref"] for e in entries]
        self.assertEqual(len(set(refs)), 2)

    def test_job_results_shape_bands_and_agreement(self):
        cfg = make_config(self.ledger)
        fake = make_fake({SPARK: JOB_SPARK, WONDER: JOB_WONDER})
        inp = self._write_input("jobs.json", [
            {"id": 7, "title": "t", "company": "c", "location": "l",
             "pay_text": "p", "source": "s"},
        ])
        out = str(self.dir / "job_scoring-results.json")
        entries = lanes.run_lane("job_scoring", inp, output_path=out,
                                 config=cfg, ledger_path=self.ledger,
                                 transport=fake)
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["spark"]["score"], 72)
        self.assertEqual(entry["wonder"]["score"], 80)
        self.assertTrue(entry["agree"])  # within 15
        self.assertEqual(entry["spark_band"], "strong")
        self.assertEqual(entry["wonder_band"], "strong")
        self.assertEqual(entry["verdict"], "pending")

    def test_deal_and_notes_entries(self):
        cfg = make_config(self.ledger)
        fake = make_fake({
            SPARK: {"content": json.dumps({"verdict": "approve",
                                           "value": "good", "risks": [],
                                           "confidence": "high"}),
                    "prompt_tokens": 50, "completion_tokens": 30},
            WONDER: {"content": json.dumps({"verdict": "reject",
                                            "value": "meh", "risks": ["r"],
                                            "confidence": "high"}),
                     "prompt_tokens": 50, "completion_tokens": 30},
        })
        inp = self._write_input("deal.json", [{"id": "d1", "title": "t",
                                               "detail": "x"}])
        entries = lanes.run_lane("deal_eval", inp,
                                 output_path=str(self.dir / "d.json"),
                                 config=cfg, ledger_path=self.ledger,
                                 transport=fake)
        self.assertEqual(len(entries), 1)
        self.assertFalse(entries[0]["agree"])
        self.assertEqual(entries[0]["verdict"], "pending")

        fake2 = make_fake({
            SPARK: {"content": json.dumps({"answer": "yes",
                                           "confidence": "high"}),
                    "prompt_tokens": 50, "completion_tokens": 20},
            WONDER: {"content": json.dumps({"answer": "no",
                                            "confidence": "low"}),
                     "prompt_tokens": 50, "completion_tokens": 20},
        })
        inp2 = self._write_input("notes.json", [{"id": "n1", "question": "q",
                                                 "extract": "e"}])
        entries2 = lanes.run_lane("notes_qa", inp2,
                                  output_path=str(self.dir / "n.json"),
                                  config=cfg, ledger_path=self.ledger,
                                  transport=fake2)
        self.assertEqual(len(entries2), 1)
        self.assertIsNone(entries2[0]["agree"])
        self.assertEqual(entries2[0]["spark_answer"], "yes")
        self.assertEqual(entries2[0]["wonder_answer"], "no")
        self.assertEqual(entries2[0]["verdict"], "pending")

    def test_full_fake_run_of_both_batches(self):
        cfg = make_config(self.ledger)
        with open(HERE / "extracts" / "mail_batch.json", encoding="utf-8") as fh:
            mail_items = json.load(fh)
        with open(HERE / "extracts" / "jobs_batch.json", encoding="utf-8") as fh:
            jobs_items = json.load(fh)
        self.assertEqual(len(mail_items), 10)
        self.assertEqual(len(jobs_items), 9)
        mail_fake = make_fake({SPARK: MAIL_SPARK, WONDER: MAIL_WONDER})
        mail_entries = lanes.run_lane(
            "mail_triage", str(HERE / "extracts" / "mail_batch.json"),
            output_path=str(self.dir / "mail_triage-results.json"),
            config=cfg, ledger_path=self.ledger, transport=mail_fake)
        job_fake = make_fake({SPARK: JOB_SPARK, WONDER: JOB_WONDER})
        job_entries = lanes.run_lane(
            "job_scoring", str(HERE / "extracts" / "jobs_batch.json"),
            output_path=str(self.dir / "job_scoring-results.json"),
            config=cfg, ledger_path=self.ledger, transport=job_fake)
        # exactly one entry per input item, in input order
        self.assertEqual(len(mail_entries), 10)
        self.assertEqual(len(job_entries), 9)
        self.assertEqual([e["id"] for e in mail_entries],
                         [m["id"] for m in mail_items])
        self.assertEqual([e["id"] for e in job_entries],
                         [j["id"] for j in jobs_items])
        for entry in mail_entries + job_entries:
            self.assertEqual(entry["verdict"], "pending")
            self.assertTrue(entry["job_ref"])
        # benchmark mode ledgered both models for every item
        with open(self.ledger, encoding="utf-8") as fh:
            lines = [json.loads(line) for line in fh if line.strip()]
        self.assertEqual(len(lines), (10 + 9) * 2)
        self.assertEqual(sum(1 for l in lines if l["model"] == SPARK), 19)
        self.assertEqual(sum(1 for l in lines if l["model"] == WONDER), 19)

    def test_spend_cap_stops_before_send(self):
        cfg = make_config(self.ledger)
        with open(self.ledger, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "call", "est_cost_usd": 6.0,
                                 "status": "ok"}) + "\n")
        fake = make_fake({SPARK: MAIL_SPARK, WONDER: MAIL_WONDER})
        inp = self._write_input("mail.json", [{"id": 1}])
        with self.assertRaises(lanes.SpendCapExceeded) as ctx:
            lanes.run_lane("mail_triage", inp,
                           output_path=str(self.dir / "o.json"),
                           config=cfg, ledger_path=self.ledger,
                           transport=fake, spend_cap_usd=5.00)
        self.assertEqual(ctx.exception.entries, [])
        self.assertEqual(len(fake.calls), 0)

    def test_unknown_lane_raises(self):
        cfg = make_config(self.ledger)
        fake = make_fake(MAIL_SPARK)
        inp = self._write_input("x.json", [{"id": 1}])
        with self.assertRaises(ValueError):
            lanes.run_lane("nope", inp,
                           output_path=str(self.dir / "o.json"),
                           config=cfg, ledger_path=self.ledger,
                           transport=fake)

    def test_summarize(self):
        tally = lanes.summarize([
            {"agree": True, "verdict": "pending"},
            {"agree": False, "verdict": "pending"},
            {"agree": None, "verdict": "pending"},
        ])
        self.assertEqual(tally, {"items": 3, "agreements": 1, "pending": 3})


if __name__ == "__main__":
    unittest.main()
