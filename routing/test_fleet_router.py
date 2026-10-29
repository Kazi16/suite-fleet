"""Unit tests for fleet_router (fake transport only, no live API calls)."""

import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import fleet_router


SPARK = "muse-spark-1.3"
WONDER = "muse-wonder-1.0"

HIGH = json.dumps({"category": "update", "summary": "s", "priority": "low",
                   "confidence": "high"})
LOW = json.dumps({"category": "update", "summary": "s", "priority": "low",
                 "confidence": "low"})


def make_config(ledger_file, benchmark=False, escalation_allowed=True,
                cap=1000.0, offset=0.0):
    lane = {"default_model": SPARK, "reasoning_effort": "low",
            "heavy_model": WONDER, "benchmark": benchmark,
            "escalation_allowed": escalation_allowed}
    return {
        "api_base": "https://api.meta.ai/v1",
        "budget_cap_usd": cap,
        "starting_offset_usd": offset,
        "ledger_path": str(ledger_file),
        "prices_usd_per_1k_tokens": {
            SPARK: {"prompt": 0.0015, "completion": 0.006},
            WONDER: {"prompt": 0.004, "completion": 0.016},
        },
        "lanes": {"mail_triage": dict(lane), "job_scoring": dict(lane),
                  "deal_eval": dict(lane), "notes_qa": dict(lane)},
    }


def make_fake(mapping_or_single):
    calls = []

    is_mapping = (
        isinstance(mapping_or_single, dict)
        and (SPARK in mapping_or_single or WONDER in mapping_or_single)
    )

    def fake(model, messages, effort):
        calls.append({"model": model, "effort": effort,
                      "messages": messages})
        if is_mapping:
            outcome = mapping_or_single[model]
        else:
            outcome = mapping_or_single
        return dict(outcome)

    fake.calls = calls
    return fake


def read_ledger(path):
    lines = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                lines.append(json.loads(line))
    return lines


REQUIRED_CALL_KEYS = ("ts", "job_ref", "lane", "model", "reasoning_effort",
                      "latency_ms", "prompt_tokens", "completion_tokens",
                      "est_cost_usd", "status", "verdict")


class RouterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = str(Path(self.tmp.name) / "ledger.jsonl")
        self.old_key = os.environ.pop("META_MODEL_API_KEY", None)

    def tearDown(self):
        if self.old_key is not None:
            os.environ["META_MODEL_API_KEY"] = self.old_key
        self.tmp.cleanup()

    def test_default_routing_single_spark_call(self):
        cfg = make_config(self.ledger)
        fake = make_fake({"content": HIGH, "prompt_tokens": 100,
                          "completion_tokens": 50})
        result = fleet_router.run_job("mail_triage", {"subject": "hi"},
                                      job_ref="job-default", config=cfg,
                                      ledger_path=self.ledger, transport=fake)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["model"], SPARK)
        self.assertFalse(result["escalated"])
        self.assertFalse(result["benchmark"])
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(fake.calls[0]["model"], SPARK)
        self.assertEqual(fake.calls[0]["effort"], "low")
        lines = read_ledger(self.ledger)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["model"], SPARK)
        self.assertEqual(lines[0]["status"], "ok")

    def test_benchmark_dual_run_returns_spark(self):
        cfg = make_config(self.ledger, benchmark=True)
        fake = make_fake({
            SPARK: {"content": HIGH, "prompt_tokens": 100,
                    "completion_tokens": 50},
            WONDER: {"content": HIGH, "prompt_tokens": 100,
                     "completion_tokens": 80},
        })
        result = fleet_router.run_job("mail_triage", {"subject": "hi"},
                                      job_ref="job-bench", config=cfg,
                                      ledger_path=self.ledger, transport=fake)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["model"], SPARK)
        self.assertTrue(result["benchmark"])
        self.assertEqual(len(fake.calls), 2)
        self.assertEqual([c["model"] for c in fake.calls], [SPARK, WONDER])
        lines = read_ledger(self.ledger)
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]["model"], SPARK)
        self.assertEqual(lines[1]["model"], WONDER)
        self.assertEqual(len(result["benchmark_results"]), 2)

    def test_escalation_once_on_low_confidence(self):
        cfg = make_config(self.ledger, escalation_allowed=True)
        fake = make_fake({
            SPARK: {"content": LOW, "prompt_tokens": 100,
                    "completion_tokens": 50},
            WONDER: {"content": HIGH, "prompt_tokens": 100,
                     "completion_tokens": 60},
        })
        result = fleet_router.run_job("mail_triage", {"subject": "hi"},
                                      job_ref="job-esc", config=cfg,
                                      ledger_path=self.ledger, transport=fake)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["model"], WONDER)
        self.assertTrue(result["escalated"])
        self.assertEqual(len(fake.calls), 2)

    def test_escalation_only_once_even_if_wonder_low(self):
        cfg = make_config(self.ledger, escalation_allowed=True)
        fake = make_fake({
            SPARK: {"content": LOW, "prompt_tokens": 100,
                    "completion_tokens": 50},
            WONDER: {"content": LOW, "prompt_tokens": 100,
                     "completion_tokens": 60},
        })
        result = fleet_router.run_job("mail_triage", {"subject": "hi"},
                                      job_ref="job-esconce", config=cfg,
                                      ledger_path=self.ledger, transport=fake)
        self.assertEqual(result["model"], WONDER)
        self.assertEqual(len(fake.calls), 2)
        self.assertEqual(len(read_ledger(self.ledger)), 2)

    def test_no_escalation_when_not_allowed(self):
        cfg = make_config(self.ledger, escalation_allowed=False)
        fake = make_fake({
            SPARK: {"content": LOW, "prompt_tokens": 100,
                    "completion_tokens": 50},
            WONDER: {"content": HIGH, "prompt_tokens": 100,
                     "completion_tokens": 60},
        })
        result = fleet_router.run_job("mail_triage", {"subject": "hi"},
                                      job_ref="job-noesc", config=cfg,
                                      ledger_path=self.ledger, transport=fake)
        self.assertEqual(result["model"], SPARK)
        self.assertFalse(result["escalated"])
        self.assertEqual(len(fake.calls), 1)

    def test_heavy_flag_routes_direct_to_wonder(self):
        cfg = make_config(self.ledger)
        fake = make_fake({
            SPARK: {"content": HIGH, "prompt_tokens": 1, "completion_tokens": 1},
            WONDER: {"content": HIGH, "prompt_tokens": 100,
                     "completion_tokens": 60},
        })
        result = fleet_router.run_job("mail_triage", {"heavy": True},
                                      job_ref="job-heavy", config=cfg,
                                      ledger_path=self.ledger, transport=fake)
        self.assertEqual(result["model"], WONDER)
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(fake.calls[0]["model"], WONDER)

    def test_budget_refusal_before_send(self):
        cfg = make_config(self.ledger, cap=0.000001)
        with open(self.ledger, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "call", "est_cost_usd": 1.0,
                                 "status": "ok"}) + "\n")
        fake = make_fake({"content": HIGH, "prompt_tokens": 100,
                          "completion_tokens": 50})
        result = fleet_router.run_job("mail_triage", {"subject": "hi"},
                                      job_ref="job-refused", config=cfg,
                                      ledger_path=self.ledger, transport=fake)
        self.assertEqual(result["status"], "refused_budget")
        self.assertIn("budget", result["error"].lower())
        self.assertEqual(len(fake.calls), 0)
        lines = read_ledger(self.ledger)
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[1]["status"], "refused_budget")
        self.assertEqual(lines[1]["prompt_tokens"], 0)
        self.assertEqual(lines[1]["est_cost_usd"], 0.0)

    def test_ledger_line_shape_and_cost(self):
        cfg = make_config(self.ledger)
        fake = make_fake({"content": HIGH, "prompt_tokens": 200,
                          "completion_tokens": 100,
                          "reasoning_tokens": 10})
        fleet_router.run_job("mail_triage", {"subject": "hi"},
                             job_ref="job-shape", config=cfg,
                             ledger_path=self.ledger, transport=fake)
        lines = read_ledger(self.ledger)
        self.assertEqual(len(lines), 1)
        line = lines[0]
        for key in REQUIRED_CALL_KEYS:
            self.assertIn(key, line, "missing ledger key %s" % key)
        self.assertEqual(line["reasoning_tokens"], 10)
        self.assertEqual(line["verdict"], "pending")
        self.assertEqual(line["job_ref"], "job-shape")
        self.assertEqual(line["lane"], "mail_triage")
        self.assertEqual(line["reasoning_effort"], "low")
        datetime.fromisoformat(line["ts"].replace("Z", "+00:00"))
        expected = (200 / 1000.0) * 0.0015 + (100 / 1000.0) * 0.006
        self.assertAlmostEqual(line["est_cost_usd"], round(expected, 6))
        self.assertIsInstance(line["latency_ms"], int)

    def test_ledger_omits_reasoning_tokens_when_absent(self):
        cfg = make_config(self.ledger)
        fake = make_fake({"content": HIGH, "prompt_tokens": 10,
                          "completion_tokens": 5})
        result = fleet_router.run_job("mail_triage", {"subject": "hi"},
                                      job_ref="job-noreason", config=cfg,
                                      ledger_path=self.ledger, transport=fake)
        self.assertEqual(result["status"], "ok")
        line = read_ledger(self.ledger)[0]
        self.assertEqual(line["status"], "ok")
        self.assertNotIn("reasoning_tokens", line)

    def test_verdict_append_does_not_edit_history(self):
        cfg = make_config(self.ledger)
        fake = make_fake({"content": HIGH, "prompt_tokens": 10,
                          "completion_tokens": 5})
        fleet_router.run_job("mail_triage", {"subject": "hi"},
                             job_ref="job-verdict", config=cfg,
                             ledger_path=self.ledger, transport=fake)
        before = read_ledger(self.ledger)
        self.assertEqual(len(before), 1)
        line = fleet_router.set_verdict("job-verdict", "accepted",
                                        ledger_path=self.ledger)
        self.assertEqual(line["verdict"], "accepted")
        after = read_ledger(self.ledger)
        self.assertEqual(len(after), 2)
        self.assertEqual(after[0], before[0])
        self.assertEqual(after[1]["type"], "verdict")
        self.assertEqual(after[1]["job_ref"], "job-verdict")
        self.assertEqual(after[1]["verdict"], "accepted")
        with self.assertRaises(ValueError):
            fleet_router.set_verdict("job-verdict", "maybe",
                                     ledger_path=self.ledger)

    def test_cap_missing_from_config_raises(self):
        cfg = make_config(self.ledger)
        del cfg["budget_cap_usd"]
        fake = make_fake({"content": HIGH, "prompt_tokens": 1,
                          "completion_tokens": 1})
        with self.assertRaises(KeyError):
            fleet_router.run_job("mail_triage", {}, job_ref="x",
                                 config=cfg, ledger_path=self.ledger,
                                 transport=fake)

    def test_unknown_lane_raises(self):
        cfg = make_config(self.ledger)
        fake = make_fake({"content": HIGH, "prompt_tokens": 1,
                          "completion_tokens": 1})
        with self.assertRaises(ValueError):
            fleet_router.run_job("nope", {}, job_ref="x", config=cfg,
                                 ledger_path=self.ledger, transport=fake)


if __name__ == "__main__":
    unittest.main()
