#!/usr/bin/env python3
"""Tests for build_dashboard.py.

All fixtures live in temporary directories. These tests never touch the
real sources under routing/ or lanes/.
"""

import json
import os
import re
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import build_dashboard as bd


def call_row(ts="2026-10-26T10:00:00Z", lane="mail_triage",
             model="muse-spark-1.3", latency_ms=1000, prompt_tokens=100,
             completion_tokens=200, est_cost_usd=0.001, status="ok", **extra):
    row = {
        "ts": ts,
        "type": "call",
        "job_ref": "abc123",
        "lane": lane,
        "model": model,
        "reasoning_effort": "low",
        "latency_ms": latency_ms,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "est_cost_usd": est_cost_usd,
        "status": status,
        "verdict": "pending",
    }
    row.update(extra)
    return row


def write_file(root, rel, text):
    path = os.path.join(root, rel)
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def write_ledger(root, rel, rows):
    lines = []
    for row in rows:
        lines.append(row if isinstance(row, str) else json.dumps(row))
    return write_file(root, rel, "\n".join(lines) + "\n")


def write_results(root, rel, entries):
    if isinstance(entries, str):
        return write_file(root, rel, entries)
    return write_file(root, rel, json.dumps(entries))


def default_tree():
    """A complete valid fixture tree. Returns the temp dir path."""
    root = tempfile.mkdtemp(prefix="dash-test-")
    write_ledger(root, "routing/ledger.jsonl", [
        call_row(),
    ])
    write_ledger(root, "lanes/ledger.jsonl", [
        call_row(ts="2026-10-26T11:00:00Z", lane="mail_triage",
                 model="muse-spark-1.3", latency_ms=1000,
                 prompt_tokens=100, completion_tokens=200, est_cost_usd=0.001),
        call_row(ts="2026-10-26T11:01:00Z", lane="mail_triage",
                 model="muse-wonder-1.0", latency_ms=3000,
                 prompt_tokens=100, completion_tokens=200, est_cost_usd=0.004),
        call_row(ts="2026-10-26T11:02:00Z", lane="job_scoring",
                 model="muse-spark-1.3", latency_ms=2000,
                 prompt_tokens=50, completion_tokens=50, est_cost_usd=0.002),
        call_row(ts="2026-10-26T11:03:00Z", lane="job_scoring",
                 model="muse-wonder-1.0", latency_ms=99999,
                 prompt_tokens=10, completion_tokens=10, est_cost_usd=0.0005,
                 status="failed"),
    ])
    write_ledger(root, "lanes/ledger-v2-low.jsonl", [
        call_row(ts="2026-10-27T10:00:00Z", lane="job_scoring",
                 model="muse-spark-1.3", latency_ms=4000,
                 prompt_tokens=200, completion_tokens=400, est_cost_usd=0.01),
        call_row(ts="2026-10-27T10:01:00Z", lane="job_scoring",
                 model="muse-wonder-1.0", latency_ms=6000,
                 prompt_tokens=200, completion_tokens=400, est_cost_usd=0.02),
    ])
    write_ledger(root, "lanes/ledger-v2-high.jsonl", [
        call_row(ts="2026-10-27T11:00:00Z", lane="job_scoring",
                 model="muse-spark-1.3", latency_ms=8000,
                 prompt_tokens=200, completion_tokens=800, est_cost_usd=0.03),
        call_row(ts="2026-10-27T11:01:00Z", lane="job_scoring",
                 model="muse-wonder-1.0", latency_ms=10000,
                 prompt_tokens=200, completion_tokens=800, est_cost_usd=0.04),
    ])
    write_ledger(root, "lanes/ledger-mail-v2-low.jsonl", [
        call_row(ts="2026-10-28T09:00:00Z", lane="mail_triage",
                 model="muse-spark-1.3", latency_ms=3000,
                 prompt_tokens=150, completion_tokens=250, est_cost_usd=0.005),
        call_row(ts="2026-10-28T09:01:00Z", lane="mail_triage",
                 model="muse-wonder-1.0", latency_ms=5000,
                 prompt_tokens=150, completion_tokens=250, est_cost_usd=0.006),
    ])
    write_results(root, "lanes/results/mail_triage-results.json", [
        {"id": 1, "agree": True, "job_ref": "a1"},
        {"id": 2, "agree": True, "job_ref": "a2"},
        {"id": 3, "agree": False, "job_ref": "a3"},
        {"id": 4, "agree": None, "job_ref": "a4"},
    ])
    write_results(root, "lanes/results/job_scoring-results.json", [
        {"id": 1, "agree": True, "job_ref": "b1"},
        {"id": 2, "agree": False, "job_ref": "b2"},
        {"id": 3, "agree": False, "job_ref": "b3"},
    ])
    write_results(root, "lanes/results/job_scoring-v2-low-results.json", [
        {"id": 1, "agree": True, "job_ref": "c1"},
        {"id": 2, "agree": True, "job_ref": "c2"},
    ])
    write_results(root, "lanes/results/job_scoring-v2-high-results.json", [
        {"id": 1, "agree": True, "job_ref": "d1"},
        {"id": 2, "agree": None, "job_ref": "d2"},
    ])
    write_results(root, "lanes/results/mail_triage-v2-low-results.json", [
        {"id": 1, "agree": True, "job_ref": "e1"},
        {"id": 2, "agree": True, "job_ref": "e2"},
        {"id": 3, "agree": True, "job_ref": "e3"},
        {"id": 4, "agree": False, "job_ref": "e4"},
    ])
    return root


class AggregationTest(unittest.TestCase):
    def setUp(self):
        self.root = default_tree()
        self.out = os.path.join(self.root, "out.html")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_totals(self):
        stats = bd.build(self.root, self.out)
        totals = stats["totals"]
        self.assertEqual(totals["calls"], 11)
        self.assertAlmostEqual(totals["est_cost"], 0.1195)
        self.assertEqual(totals["prompt_tokens"], 1460)
        self.assertEqual(totals["completion_tokens"], 3560)
        self.assertEqual(totals["date_from"], "2026-10-26")
        self.assertEqual(totals["date_to"], "2026-10-28")

    def test_per_ledger_counts_and_cost(self):
        stats = bd.build(self.root, self.out)
        by_name = {s["name"]: s for s in stats["ledgers"]}
        routing = by_name["routing/ledger.jsonl"]
        self.assertEqual((routing["calls"], routing["ok"], routing["failed"]),
                         (1, 1, 0))
        self.assertAlmostEqual(routing["est_cost"], 0.001)
        lanes = by_name["lanes/ledger.jsonl"]
        self.assertEqual((lanes["calls"], lanes["ok"], lanes["failed"]),
                         (4, 3, 1))
        self.assertAlmostEqual(lanes["est_cost"], 0.0075)
        self.assertAlmostEqual(
            by_name["lanes/ledger-v2-low.jsonl"]["est_cost"], 0.03)
        self.assertAlmostEqual(
            by_name["lanes/ledger-v2-high.jsonl"]["est_cost"], 0.07)
        self.assertAlmostEqual(
            by_name["lanes/ledger-mail-v2-low.jsonl"]["est_cost"], 0.011)

    def test_per_model_latency_tokens_cost(self):
        stats = bd.build(self.root, self.out)
        by_model = {s["model"]: s for s in stats["models"]}
        spark = by_model["muse-spark-1.3"]
        self.assertEqual(spark["calls"], 6)
        # ok latencies: 1000, 1000, 2000, 3000, 4000, 8000
        self.assertAlmostEqual(spark["mean_ms"], 19000.0 / 6.0)
        self.assertAlmostEqual(spark["median_ms"], 2500)
        self.assertEqual(spark["tokens"], 2700)
        self.assertAlmostEqual(spark["est_cost"], 0.049)
        wonder = by_model["muse-wonder-1.0"]
        self.assertEqual(wonder["calls"], 5)
        # failed 99999 excluded: ok latencies 3000, 5000, 6000, 10000
        self.assertAlmostEqual(wonder["mean_ms"], 24000.0 / 4.0)
        self.assertAlmostEqual(wonder["median_ms"], 5500)
        self.assertEqual(wonder["tokens"], 2320)
        self.assertAlmostEqual(wonder["est_cost"], 0.0705)

    def test_even_count_median(self):
        root = tempfile.mkdtemp(prefix="dash-test-even-")
        try:
            latencies = [1000, 2000, 3000, 4000, 5000]
            for rel, lat in zip(bd.LEDGER_PATHS, latencies):
                write_ledger(root, rel, [
                    call_row(latency_ms=lat, lane="job_scoring")])
            for rel in bd.RESULTS_PATHS:
                write_results(root, rel, [{"id": 1, "agree": True}])
            out = os.path.join(root, "out.html")
            stats = bd.build(root, out)
            spark = [m for m in stats["models"]
                     if m["model"] == "muse-spark-1.3"][0]
            self.assertAlmostEqual(spark["mean_ms"], 3000.0)
            self.assertAlmostEqual(spark["median_ms"], 3000.0)
        finally:
            shutil.rmtree(root, ignore_errors=True)


class AgreementTest(unittest.TestCase):
    def setUp(self):
        self.root = default_tree()
        self.out = os.path.join(self.root, "out.html")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_true_false_null_and_rate(self):
        stats = bd.build(self.root, self.out)
        by_name = {a["name"]: a for a in stats["agreements"]}
        mail = by_name["lanes/results/mail_triage-results.json"]
        self.assertEqual(
            (mail["entries"], mail["agree"], mail["disagree"], mail["unscored"]),
            (4, 2, 1, 1))
        # null excluded: 2 of 3 scored -> 67%
        self.assertEqual(mail["rate_pct"], 67)
        v1 = by_name["lanes/results/job_scoring-results.json"]
        self.assertEqual(v1["rate_pct"], 33)  # 1 of 3
        v2low = by_name["lanes/results/job_scoring-v2-low-results.json"]
        self.assertEqual(v2low["rate_pct"], 100)  # 2 of 2
        v2high = by_name["lanes/results/job_scoring-v2-high-results.json"]
        self.assertEqual(
            (v2high["entries"], v2high["agree"], v2high["unscored"]),
            (2, 1, 1))
        self.assertEqual(v2high["rate_pct"], 100)  # 1 of 1 scored
        mailv2 = by_name["lanes/results/mail_triage-v2-low-results.json"]
        self.assertEqual(
            (mailv2["entries"], mailv2["agree"], mailv2["disagree"],
             mailv2["unscored"]),
            (4, 3, 1, 0))
        self.assertEqual(mailv2["rate_pct"], 75)  # 3 of 4

    def test_empty_results_rate_is_none(self):
        root = tempfile.mkdtemp(prefix="dash-test-empty-")
        try:
            for rel in bd.LEDGER_PATHS:
                write_ledger(root, rel, [call_row(lane="job_scoring")])
            for rel in bd.RESULTS_PATHS:
                write_results(root, rel, [])
            out = os.path.join(root, "out.html")
            stats = bd.build(root, out)
            for a in stats["agreements"]:
                self.assertEqual(a["entries"], 0)
                self.assertIsNone(a["rate_pct"])
            with open(out, encoding="utf-8") as fh:
                page = fh.read()
            self.assertIn("n/a (0 of 0 scored)", page)
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_comparisons_come_from_matching_sources(self):
        stats = bd.build(self.root, self.out)
        comp1 = stats["comp_v1_v2low"]
        # v1 ledger slice: the two job_scoring rows of lanes/ledger.jsonl
        self.assertEqual(comp1["v1"]["ledger"]["calls"], 2)
        self.assertAlmostEqual(comp1["v1"]["ledger"]["est_cost"], 0.0025)
        self.assertEqual(comp1["v1"]["agreement"]["rate_pct"], 33)
        self.assertEqual(comp1["v2low"]["ledger"]["calls"], 2)
        self.assertAlmostEqual(comp1["v2low"]["ledger"]["est_cost"], 0.03)
        self.assertEqual(comp1["v2low"]["agreement"]["rate_pct"], 100)
        comp2 = stats["comp_low_high"]
        self.assertAlmostEqual(comp2["low"]["ledger"]["mean_ms"], 5000.0)
        self.assertAlmostEqual(comp2["high"]["ledger"]["mean_ms"], 9000.0)
        self.assertEqual(comp2["high"]["agreement"]["rate_pct"], 100)

    def test_mail_comparison_comes_from_matching_sources(self):
        stats = bd.build(self.root, self.out)
        comp = stats["comp_mail"]
        # v1 ledger slice: the two mail_triage rows of lanes/ledger.jsonl
        self.assertEqual(comp["v1"]["ledger"]["calls"], 2)
        self.assertAlmostEqual(comp["v1"]["ledger"]["est_cost"], 0.005)
        self.assertEqual(comp["v1"]["agreement"]["rate_pct"], 67)
        self.assertEqual(comp["v2low"]["ledger"]["calls"], 2)
        self.assertAlmostEqual(comp["v2low"]["ledger"]["est_cost"], 0.011)
        self.assertEqual(comp["v2low"]["agreement"]["rate_pct"], 75)


class FailureTest(unittest.TestCase):
    def setUp(self):
        self.root = default_tree()
        self.out = os.path.join(self.root, "out.html")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_missing_source_fails_and_writes_nothing(self):
        os.remove(os.path.join(self.root, "lanes/ledger-v2-high.jsonl"))
        with self.assertRaises(bd.DashboardBuildError) as ctx:
            bd.build(self.root, self.out)
        self.assertIn("ledger-v2-high.jsonl", str(ctx.exception))
        self.assertFalse(os.path.exists(self.out))
        self.assertFalse(os.path.exists(self.out + ".tmp"))

    def test_missing_results_fails(self):
        os.remove(os.path.join(
            self.root, "lanes/results/job_scoring-results.json"))
        with self.assertRaises(bd.DashboardBuildError) as ctx:
            bd.build(self.root, self.out)
        self.assertIn("job_scoring-results.json", str(ctx.exception))
        self.assertFalse(os.path.exists(self.out))

    def test_failure_removes_stale_output(self):
        with open(self.out, "w", encoding="utf-8") as fh:
            fh.write("stale")
        os.remove(os.path.join(self.root, "routing/ledger.jsonl"))
        with self.assertRaises(bd.DashboardBuildError):
            bd.build(self.root, self.out)
        self.assertFalse(os.path.exists(self.out))

    def test_malformed_ledger_line_names_file_and_line(self):
        write_file(self.root, "lanes/ledger.jsonl",
                   json.dumps(call_row()) + "\n{not valid json\n")
        with self.assertRaises(bd.DashboardBuildError) as ctx:
            bd.build(self.root, self.out)
        message = str(ctx.exception)
        self.assertIn("ledger.jsonl", message)
        self.assertIn("line 2", message)
        self.assertFalse(os.path.exists(self.out))

    def test_malformed_results_names_file(self):
        write_file(self.root, "lanes/results/mail_triage-results.json",
                   "[{\"agree\": true},")
        with self.assertRaises(bd.DashboardBuildError) as ctx:
            bd.build(self.root, self.out)
        self.assertIn("mail_triage-results.json", str(ctx.exception))
        self.assertFalse(os.path.exists(self.out))

    def test_bad_field_names_file_and_line(self):
        bad = call_row()
        del bad["latency_ms"]
        write_ledger(self.root, "routing/ledger.jsonl", [bad])
        with self.assertRaises(bd.DashboardBuildError) as ctx:
            bd.build(self.root, self.out)
        message = str(ctx.exception)
        self.assertIn("routing/ledger.jsonl", message)
        self.assertIn("line 1", message)
        self.assertFalse(os.path.exists(self.out))


class PrivacyTest(unittest.TestCase):
    def setUp(self):
        self.root = default_tree()
        self.out = os.path.join(self.root, "out.html")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_plant_email_in_sources_never_reaches_output(self):
        # Plant free text with email addresses in every free-text-shaped
        # field the real sources carry; the builder must emit aggregates
        # only, so none of it may appear in the page.
        write_results(root=self.root,
                      rel="lanes/results/mail_triage-results.json", entries=[
                          {"id": 1, "agree": True, "job_ref": "a1",
                           "spark": {"category": "x",
                                     "summary": "mail alice.plant@example.com"},
                           "wonder": {"category": "x",
                                      "summary": "mail bob.plant@test.org"}},
                      ])
        write_results(root=self.root,
                      rel="lanes/results/job_scoring-results.json", entries=[
                          {"id": 1, "agree": False, "job_ref": "b1",
                           "spark": {"score": 1,
                                     "reasons": ["mail carol@test.net"]},
                           "wonder": {"score": 2, "reasons": ["other"]}},
                      ])
        stats = bd.build(self.root, self.out)
        self.assertIsNotNone(stats)
        with open(self.out, encoding="utf-8") as fh:
            page = fh.read()
        self.assertNotIn("alice.plant@example.com", page)
        self.assertNotIn("bob.plant@test.org", page)
        self.assertNotIn("carol@test.net", page)
        self.assertIsNone(bd.EMAIL_RE.search(page))

    def test_scan_catches_email(self):
        with self.assertRaises(bd.DashboardBuildError):
            bd.scan_html("<html><body>mail leak@example.com</body></html>")

    def test_scan_catches_kaziprince_outside_footer(self):
        with self.assertRaises(bd.DashboardBuildError):
            bd.scan_html("<html><body>hello KaziPrince</body></html>")

    def test_scan_allows_footer_credit_and_links(self):
        page = ("<html><body><p>clean</p>"
                '<footer class="page"><p>Credit: Kazi Paruaz Prince.</p>'
                '<a href="https://linkedin.com/in/kaziprince">x</a>'
                "</footer></body></html>")
        bd.scan_html(page)  # must not raise

    def test_scan_catches_long_hex_run(self):
        with self.assertRaises(bd.DashboardBuildError):
            bd.scan_html("<html><body>ref abcdef0123456789abcdef0123456789"
                         "</body></html>")

    def test_scan_allows_short_job_ref(self):
        bd.scan_html("<html><body>ref 42e69872234c</body></html>")

    def test_scan_catches_secrets(self):
        with self.assertRaises(bd.DashboardBuildError):
            bd.scan_html("<html><body>META_MODEL_API_KEY=abc</body></html>")
        with self.assertRaises(bd.DashboardBuildError):
            bd.scan_html("<html><body>https://api.meta.ai/v1</body></html>")


class HygieneTest(unittest.TestCase):
    def setUp(self):
        self.root = default_tree()
        self.out = os.path.join(self.root, "out.html")
        bd.build(self.root, self.out)
        with open(self.out, encoding="utf-8") as fh:
            self.page = fh.read()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_no_plaintext_http(self):
        self.assertNotIn("http://", self.page)

    def test_only_allowed_https_links(self):
        found = re.findall(r"https?://[^\s\"'<>]+", self.page)
        self.assertEqual(set(found), set(bd.ALLOWED_LINKS))
        for link in bd.ALLOWED_LINKS:
            self.assertIn('href="%s"' % link, self.page)

    def test_no_external_resource_tags(self):
        for token in ("<script", "src=", "url(", "@import", "<link"):
            self.assertNotIn(token, self.page)

    def test_costs_labeled_estimated(self):
        self.assertIn("total estimated cost", self.page)
        self.assertIn("Estimated cost (USD)", self.page)
        self.assertIn("Total estimated cost (USD)", self.page)

    def test_header_and_footer_present(self):
        self.assertIn("Suite Fleet results", self.page)
        self.assertIn("Muse Spark", self.page)
        self.assertIn("Muse Wonder", self.page)
        self.assertIn("benchmark mode", self.page)
        self.assertIn("Kazi Paruaz Prince", self.page)
        self.assertIn("Built by Suite Fleet", self.page)

    def test_charts_are_inline_svg(self):
        self.assertIn("<svg", self.page)
        self.assertIn("Agreement rate per results file", self.page)


class ResponsiveTest(unittest.TestCase):
    def setUp(self):
        self.root = default_tree()
        self.out = os.path.join(self.root, "out.html")
        bd.build(self.root, self.out)
        with open(self.out, encoding="utf-8") as fh:
            self.page = fh.read()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_viewport_meta_present(self):
        self.assertIn(
            '<meta name="viewport" content="width=device-width, initial-scale=1">',
            self.page)

    def test_tables_wrapped_for_horizontal_scroll(self):
        self.assertEqual(self.page.count('class="table-wrap"'), 2)
        self.assertIn(".table-wrap { overflow-x: auto;", self.page)

    def test_responsive_css_present(self):
        self.assertIn("@media (max-width: 640px)", self.page)
        self.assertIn("figure.chart svg { max-width: 100%; height: auto;", self.page)
        self.assertIn("minmax(min(220px, 100%), 1fr)", self.page)
        self.assertIn("clamp(24px, 6vw, 32px)", self.page)

    def test_no_fixed_width_overflow(self):
        # Main column is fluid (max-width), not a fixed pixel width.
        self.assertIn("main { max-width: 960px; width: 100%;", self.page)


if __name__ == "__main__":
    unittest.main(verbosity=2)
