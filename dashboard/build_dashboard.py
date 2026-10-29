#!/usr/bin/env python3
"""Suite Fleet public results dashboard builder.

Reads the router/benchmark ledgers and lane results files (read-only),
computes aggregate statistics, and writes a single self-contained
dashboard.html page.

Pure Python standard library. Makes no network calls and needs no API key.

Usage:
    python3 build_dashboard.py [--source-root DIR] [--output FILE]

Defaults: source root is the parent of this script's directory
(/home/kazi/hackathon-staging), output is dashboard.html next to the script.

Fails loud: any missing or malformed source, or any privacy-scan hit,
exits nonzero and leaves no output file behind (a stale previous output
is removed).
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import statistics
import sys


LEDGER_PATHS = [
    "routing/ledger.jsonl",
    "lanes/ledger.jsonl",
    "lanes/ledger-v2-low.jsonl",
    "lanes/ledger-v2-high.jsonl",
    "lanes/ledger-mail-v2-low.jsonl",
]

RESULTS_PATHS = [
    "lanes/results/mail_triage-results.json",
    "lanes/results/job_scoring-results.json",
    "lanes/results/job_scoring-v2-low-results.json",
    "lanes/results/job_scoring-v2-high-results.json",
    "lanes/results/mail_triage-v2-low-results.json",
]

RESULT_LABELS = {
    "lanes/results/mail_triage-results.json": "mail triage",
    "lanes/results/job_scoring-results.json": "job scoring v1",
    "lanes/results/job_scoring-v2-low-results.json": "job scoring v2 low",
    "lanes/results/job_scoring-v2-high-results.json": "job scoring v2 high",
    "lanes/results/mail_triage-v2-low-results.json": "mail triage v2 low",
}

ALLOWED_LINKS = [
    "https://github.com/Kazi16",
    "https://linkedin.com/in/kaziprince",
    "https://dev.meta.ai/events/global-hackathon",
]

FOOTER_CREDIT = "Kazi Paruaz Prince"

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
HEX_RUN_RE = re.compile(r"[0-9a-fA-F]{24,}")
DETAILS_RE = re.compile(r"<details\b.*?</details>", re.DOTALL | re.IGNORECASE)
FOOTER_RE = re.compile(r"<footer\b.*?</footer>", re.DOTALL | re.IGNORECASE)


class DashboardBuildError(Exception):
    """Raised when the dashboard cannot be built."""


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------

def _read_text(path):
    """Read a source file, failing loud with the file named."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except FileNotFoundError:
        raise DashboardBuildError("missing source file: %s" % path)
    except OSError as exc:
        raise DashboardBuildError("cannot read source file %s: %s" % (path, exc))


def _req_str(row, key, path, lineno):
    value = row.get(key)
    if not isinstance(value, str) or not value:
        raise DashboardBuildError(
            "%s line %d: field %r missing or not a non-empty string"
            % (path, lineno, key)
        )
    return value


def _req_num(row, key, path, lineno, integer=False):
    value = row.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DashboardBuildError(
            "%s line %d: field %r missing or not a number" % (path, lineno, key)
        )
    if integer and not isinstance(value, int):
        raise DashboardBuildError(
            "%s line %d: field %r must be an integer" % (path, lineno, key)
        )
    return value


def load_ledger(path):
    """Load one JSONL ledger. Every non-blank line is one call record."""
    text = _read_text(path)
    rows = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise DashboardBuildError(
                "%s line %d: malformed JSON: %s" % (path, lineno, exc.msg)
            )
        if not isinstance(row, dict):
            raise DashboardBuildError(
                "%s line %d: malformed record: not a JSON object" % (path, lineno)
            )
        _req_str(row, "ts", path, lineno)
        _req_str(row, "status", path, lineno)
        _req_str(row, "model", path, lineno)
        _req_str(row, "lane", path, lineno)
        _req_num(row, "latency_ms", path, lineno)
        _req_num(row, "prompt_tokens", path, lineno, integer=True)
        _req_num(row, "completion_tokens", path, lineno, integer=True)
        _req_num(row, "est_cost_usd", path, lineno)
        rows.append(row)
    return rows


def load_results(path):
    """Load one results JSON file: a list of entries each with an agree flag."""
    text = _read_text(path)
    try:
        entries = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DashboardBuildError(
            "%s line %d: malformed JSON: %s" % (path, exc.lineno, exc.msg)
        )
    if not isinstance(entries, list):
        raise DashboardBuildError("%s: malformed results: not a JSON list" % path)
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise DashboardBuildError(
                "%s entry %d: malformed entry: not a JSON object" % (path, index)
            )
        if "agree" not in entry:
            raise DashboardBuildError(
                "%s entry %d: malformed entry: missing 'agree' field" % (path, index)
            )
        if entry["agree"] is not True and entry["agree"] is not False \
                and entry["agree"] is not None:
            raise DashboardBuildError(
                "%s entry %d: malformed entry: 'agree' must be true, false or null"
                % (path, index)
            )
    return entries


# --------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------

def summarize_ledger(name, rows):
    ok = sum(1 for row in rows if row["status"] == "ok")
    return {
        "name": name,
        "calls": len(rows),
        "ok": ok,
        "failed": len(rows) - ok,
        "est_cost": sum(row["est_cost_usd"] for row in rows),
        "prompt_tokens": sum(row["prompt_tokens"] for row in rows),
        "completion_tokens": sum(row["completion_tokens"] for row in rows),
    }


def summarize_models(ledgers_rows):
    """Per-model stats across all ledgers. Latency over status-ok calls only."""
    by_model = {}
    for _name, rows in ledgers_rows:
        for row in rows:
            by_model.setdefault(row["model"], []).append(row)
    summaries = []
    for model in sorted(by_model):
        rows = by_model[model]
        ok_lat = sorted(row["latency_ms"] for row in rows if row["status"] == "ok")
        if ok_lat:
            mean_ms = statistics.mean(ok_lat)
            median_ms = statistics.median(ok_lat)
        else:
            mean_ms = None
            median_ms = None
        summaries.append({
            "model": model,
            "calls": len(rows),
            "ok": len(ok_lat),
            "mean_ms": mean_ms,
            "median_ms": median_ms,
            "tokens": sum(row["prompt_tokens"] + row["completion_tokens"]
                           for row in rows),
            "est_cost": sum(row["est_cost_usd"] for row in rows),
        })
    return summaries


def summarize_agreement(name, entries):
    agree = sum(1 for e in entries if e["agree"] is True)
    disagree = sum(1 for e in entries if e["agree"] is False)
    unscored = sum(1 for e in entries if e["agree"] is None)
    scored = agree + disagree
    rate_pct = int(100.0 * agree / scored + 0.5) if scored else None
    return {
        "name": name,
        "label": RESULT_LABELS.get(name, name),
        "entries": len(entries),
        "agree": agree,
        "disagree": disagree,
        "unscored": unscored,
        "scored": scored,
        "rate_pct": rate_pct,
    }


def _slice_stats(rows):
    ok_lat = sorted(row["latency_ms"] for row in rows if row["status"] == "ok")
    return {
        "calls": len(rows),
        "est_cost": sum(row["est_cost_usd"] for row in rows),
        "mean_ms": statistics.mean(ok_lat) if ok_lat else None,
    }


def compute_stats(source_root):
    """Load every source and compute all aggregates. Returns a stats dict."""
    ledger_rows = []
    for rel in LEDGER_PATHS:
        ledger_rows.append((rel, load_ledger(os.path.join(source_root, rel))))
    results = {}
    for rel in RESULTS_PATHS:
        results[rel] = load_results(os.path.join(source_root, rel))

    ledgers = [summarize_ledger(name, rows) for name, rows in ledger_rows]
    models = summarize_models(ledger_rows)
    agreements = [summarize_agreement(rel, results[rel]) for rel in RESULTS_PATHS]
    by_agreement = {a["name"]: a for a in agreements}
    by_ledger = {name: rows for name, rows in ledger_rows}

    all_ts = [row["ts"] for _name, rows in ledger_rows for row in rows]
    totals = {
        "calls": sum(s["calls"] for s in ledgers),
        "est_cost": sum(s["est_cost"] for s in ledgers),
        "prompt_tokens": sum(s["prompt_tokens"] for s in ledgers),
        "completion_tokens": sum(s["completion_tokens"] for s in ledgers),
        "date_from": min(all_ts)[:10] if all_ts else None,
        "date_to": max(all_ts)[:10] if all_ts else None,
    }

    # Comparison 1: job scoring v1 (v1 results + v1 lane slice of the v1
    # ledger) against v2 low (v2 low results + v2 low ledger).
    v1_slice = [r for r in by_ledger["lanes/ledger.jsonl"]
                if r["lane"] == "job_scoring"]
    comp_v1_v2low = {
        "v1": {"agreement": by_agreement["lanes/results/job_scoring-results.json"],
               "ledger": _slice_stats(v1_slice)},
        "v2low": {"agreement":
                  by_agreement["lanes/results/job_scoring-v2-low-results.json"],
                  "ledger": _slice_stats(by_ledger["lanes/ledger-v2-low.jsonl"])},
    }
    # Comparison 2: v2 low effort against v2 high effort.
    comp_low_high = {
        "low": {"agreement":
                by_agreement["lanes/results/job_scoring-v2-low-results.json"],
                "ledger": _slice_stats(by_ledger["lanes/ledger-v2-low.jsonl"])},
        "high": {"agreement":
                 by_agreement["lanes/results/job_scoring-v2-high-results.json"],
                 "ledger": _slice_stats(by_ledger["lanes/ledger-v2-high.jsonl"])},
    }

    # Comparison 3: mail triage v1 (v1 results + v1 lane slice of the v1
    # ledger) against v2 low (mail v2 low results + mail v2 low ledger).
    mail_v1_slice = [r for r in by_ledger["lanes/ledger.jsonl"]
                     if r["lane"] == "mail_triage"]
    comp_mail = {
        "v1": {"agreement": by_agreement["lanes/results/mail_triage-results.json"],
               "ledger": _slice_stats(mail_v1_slice)},
        "v2low": {"agreement":
                  by_agreement["lanes/results/mail_triage-v2-low-results.json"],
                  "ledger": _slice_stats(by_ledger["lanes/ledger-mail-v2-low.jsonl"])},
    }

    return {
        "totals": totals,
        "ledgers": ledgers,
        "models": models,
        "agreements": agreements,
        "comp_v1_v2low": comp_v1_v2low,
        "comp_low_high": comp_low_high,
        "comp_mail": comp_mail,
    }


# --------------------------------------------------------------------------
# Formatting
# --------------------------------------------------------------------------

def fmt_cost(value):
    return "$%.4f" % value


def fmt_signed_cost(value):
    return "$%+.4f" % value


def fmt_grouped(value):
    return "{:,}".format(int(value + 0.5) if isinstance(value, float) else value)


def fmt_ms(value):
    if value is None:
        return "n/a"
    return "{:,}".format(int(value + 0.5))


def fmt_rate(summary):
    if summary["rate_pct"] is None:
        return "n/a (0 of 0 scored)"
    return "%d%% (%d of %d scored)" % (
        summary["rate_pct"], summary["agree"], summary["scored"])


def esc(text):
    return html.escape(str(text), quote=True)


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

CSS = """
* { box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica,
  Arial, sans-serif; color: #1c1c1c; background: #ffffff; margin: 0;
  line-height: 1.5; overflow-wrap: break-word; }
main { max-width: 960px; width: 100%; margin: 0 auto; padding: 32px 20px 64px; }
header.page { border-bottom: 3px solid #0f766e; padding-bottom: 16px;
  margin-bottom: 28px; }
h1 { font-size: clamp(24px, 6vw, 32px); margin: 0 0 8px; letter-spacing: -0.5px; }
header.page p { margin: 0; font-size: clamp(14px, 2.5vw, 16px); }
h2 { font-size: clamp(18px, 4vw, 21px); margin: 36px 0 12px; }
h3 { font-size: 16px; margin: 20px 0 8px; }
.strip { display: flex; flex-wrap: wrap; gap: 12px; margin: 16px 0 8px; }
.stat { flex: 1 1 150px; border: 1px solid #d8d8d8; border-radius: 10px;
  padding: 12px 14px; background: #f7f7f7; min-width: 0; }
.stat .v { font-size: clamp(18px, 4vw, 22px); font-weight: 700; }
.stat .k { font-size: 13px; color: #333; }
.table-wrap { overflow-x: auto; -webkit-overflow-scrolling: touch;
  margin: 8px 0 4px; max-width: 100%; }
.table-wrap table { margin: 0; }
table { border-collapse: collapse; width: 100%; font-size: 15px; }
th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid #ddd; }
th { background: #f0f0f0; font-weight: 700; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
caption { text-align: left; font-size: 13px; color: #333; padding: 4px 0; }
.agree-grid { display: grid; grid-template-columns: repeat(auto-fit,
  minmax(min(220px, 100%), 1fr)); gap: 12px; margin: 12px 0; }
.agree-card { border: 1px solid #d8d8d8; border-radius: 10px; padding: 12px 14px;
  min-width: 0; }
.agree-card .rate { font-size: clamp(20px, 5vw, 26px); font-weight: 700; color: #0f766e; }
.agree-card ul { margin: 8px 0 0; padding-left: 20px; font-size: 14px; }
.compare { display: grid; grid-template-columns: repeat(auto-fit,
  minmax(min(260px, 100%), 1fr)); gap: 12px; margin: 12px 0; }
.compare-side { border: 1px solid #d8d8d8; border-radius: 10px;
  padding: 12px 14px; min-width: 0; }
.compare-side dl { margin: 8px 0 0; font-size: 14px; }
.compare-side dt { font-weight: 700; }
.compare-side dd { margin: 0 0 6px; }
.delta { font-size: 14px; background: #f7f7f7; border: 1px solid #d8d8d8;
  border-radius: 8px; padding: 8px 12px; margin: 4px 0 12px; }
figure.chart { margin: 16px 0; max-width: 100%; }
figure.chart svg { max-width: 100%; height: auto; display: block; }
figure.chart figcaption { font-size: 13px; color: #333; margin-top: 4px; }
.sources { font-size: 13px; color: #333; overflow-wrap: anywhere; }
footer.page { border-top: 3px solid #0f766e; margin-top: 40px;
  padding-top: 16px; font-size: 14px; }
footer.page ul { list-style: none; margin: 8px 0 0; padding: 0; }
footer.page li { display: inline; margin-right: 16px; }
a { color: #0b5fff; }
@media (max-width: 640px) {
  main { padding: 20px 12px 40px; }
  h2 { margin: 28px 0 10px; }
  table { font-size: 13px; }
  th, td { padding: 6px 8px; }
  .table-wrap table { min-width: 560px; }
  .stat { flex: 1 1 calc(50% - 6px); }
  .agree-card .rate { overflow-wrap: anywhere; }
  footer.page li { display: block; margin: 4px 0; }
}
@media (max-width: 420px) {
  .stat { flex: 1 1 100%; }
  .strip { gap: 8px; }
}
"""


def svg_bars(title, rows):
    """Render a horizontal bar chart as inline SVG.

    rows: list of (label, fraction 0..1, value_text). All inputs are
    computed numbers or fixed labels; everything is escaped.
    """
    width = 620
    label_w = 250
    bar_max = 230
    row_h = 30
    height = row_h * len(rows) + 12
    out = ['<svg viewBox="0 0 %d %d" width="%d" height="%d" role="img">'
           % (width, height, width, height)]
    out.append("<title>%s</title>" % esc(title))
    for i, (label, frac, value_text) in enumerate(rows):
        frac = max(0.0, min(1.0, frac))
        bar_w = int(bar_max * frac + 0.5)
        y = 8 + i * row_h
        out.append(
            '<text x="0" y="%d" font-size="13">%s</text>'
            % (y + 14, esc(label)))
        out.append(
            '<rect x="%d" y="%d" width="%d" height="16" rx="3" fill="#0f766e"/>'
            % (label_w, y, bar_w))
        out.append(
            '<text x="%d" y="%d" font-size="13">%s</text>'
            % (label_w + bar_max + 10, y + 14, esc(value_text)))
    out.append("</svg>")
    return "".join(out)


def render_totals(totals):
    if totals["date_from"] and totals["date_to"]:
        if totals["date_from"] == totals["date_to"]:
            date_range = totals["date_from"]
        else:
            date_range = "%s to %s" % (totals["date_from"], totals["date_to"])
    else:
        date_range = "n/a"
    cells = [
        (fmt_grouped(totals["calls"]), "total calls"),
        (fmt_cost(totals["est_cost"]), "total estimated cost"),
        (fmt_grouped(totals["prompt_tokens"]), "total prompt tokens"),
        (fmt_grouped(totals["completion_tokens"]), "total completion tokens"),
        (esc(date_range), "ledger date range"),
    ]
    parts = ['<section aria-label="Totals"><h2>Totals</h2><div class="strip">']
    for value, kind in cells:
        parts.append(
            '<div class="stat"><div class="v">%s</div>'
            '<div class="k">%s</div></div>' % (value, kind))
    parts.append("</div></section>")
    return "".join(parts)


def render_ledger_table(ledgers):
    parts = ['<section aria-label="Per ledger"><h2>Calls per ledger</h2>',
             '<div class="table-wrap"><table><caption>One row per source ledger. '
             "Failed means status other than ok.</caption>",
             "<tr><th>Ledger</th><th class=\"num\">Calls</th>"
             "<th class=\"num\">Ok</th><th class=\"num\">Failed</th>"
             "<th class=\"num\">Estimated cost (USD)</th></tr>"]
    for s in ledgers:
        parts.append(
            "<tr><td>%s</td><td class=\"num\">%s</td><td class=\"num\">%s</td>"
            "<td class=\"num\">%s</td><td class=\"num\">%s</td></tr>"
            % (esc(s["name"]), fmt_grouped(s["calls"]), fmt_grouped(s["ok"]),
               fmt_grouped(s["failed"]), fmt_cost(s["est_cost"])))
    parts.append("</table></div></section>")
    return "".join(parts)


def render_model_table(models):
    parts = ['<section aria-label="Per model"><h2>Calls per model</h2>',
             '<div class="table-wrap"><table><caption>Latency over status-ok calls only.</caption>',
             "<tr><th>Model</th><th class=\"num\">Calls</th>"
             "<th class=\"num\">Mean latency (ms)</th>"
             "<th class=\"num\">Median latency (ms)</th>"
             "<th class=\"num\">Total tokens</th>"
             "<th class=\"num\">Total estimated cost (USD)</th></tr>"]
    for s in models:
        parts.append(
            "<tr><td>%s</td><td class=\"num\">%s</td><td class=\"num\">%s</td>"
            "<td class=\"num\">%s</td><td class=\"num\">%s</td>"
            "<td class=\"num\">%s</td></tr>"
            % (esc(s["model"]), fmt_grouped(s["calls"]), fmt_ms(s["mean_ms"]),
               fmt_ms(s["median_ms"]), fmt_grouped(s["tokens"]),
               fmt_cost(s["est_cost"])))
    parts.append("</table></div></section>")
    return "".join(parts)


def render_agreement(agreements):
    parts = ['<section aria-label="Agreement"><h2>Model agreement</h2>',
             "<p>Each results entry carries its own agree flag. "
             "Null means unscored: excluded from the rate, counted below.</p>",
             '<div class="agree-grid">']
    for a in agreements:
        parts.append(
            '<div class="agree-card"><h3>%s</h3>'
            '<div class="rate">%s</div>'
            "<ul><li>entries: %s</li><li>agreements: %s</li>"
            "<li>disagreements: %s</li><li>unscored: %s</li></ul>"
            "<p><small>source: %s</small></p></div>"
            % (esc(a["label"]), esc(fmt_rate(a)), fmt_grouped(a["entries"]),
               fmt_grouped(a["agree"]), fmt_grouped(a["disagree"]),
               fmt_grouped(a["unscored"]), esc(a["name"])))
    parts.append("</div></section>")
    return "".join(parts)


def _delta_rate(a, b):
    if a["rate_pct"] is None or b["rate_pct"] is None:
        return "agreement delta: n/a"
    return "agreement delta: %+.d pts (%s vs %s)" % (
        b["rate_pct"] - a["rate_pct"], fmt_rate(a), fmt_rate(b))


def _delta_cost(ca, cb):
    return "estimated cost delta: %s" % fmt_signed_cost(cb - ca)


def _delta_lat(ma, mb):
    if ma is None or mb is None:
        return "mean latency delta: n/a"
    return "mean latency delta: %+.d ms" % int(mb - ma + (0.5 if mb >= ma else -0.5))


def render_comparison_v1_v2low(comp):
    v1, v2 = comp["v1"], comp["v2low"]
    return (
        '<section aria-label="v1 versus v2 low"><h2>Job scoring: v1 vs v2 low</h2>'
        '<div class="compare">'
        '<div class="compare-side"><h3>v1</h3><dl>'
        "<dt>Agreement</dt><dd>%s</dd>"
        "<dt>Estimated cost</dt><dd>%s</dd>"
        "</dl><p><small>sources: lanes/results/job_scoring-results.json; "
        "lanes/ledger.jsonl lane job_scoring</small></p></div>"
        '<div class="compare-side"><h3>v2 low</h3><dl>'
        "<dt>Agreement</dt><dd>%s</dd>"
        "<dt>Estimated cost</dt><dd>%s</dd>"
        "</dl><p><small>sources: "
        "lanes/results/job_scoring-v2-low-results.json; "
        "lanes/ledger-v2-low.jsonl</small></p></div>"
        "</div>"
        '<p class="delta">%s. %s.</p>'
        "</section>"
        % (esc(fmt_rate(v1["agreement"])), fmt_cost(v1["ledger"]["est_cost"]),
           esc(fmt_rate(v2["agreement"])), fmt_cost(v2["ledger"]["est_cost"]),
           esc(_delta_rate(v1["agreement"], v2["agreement"])),
           esc(_delta_cost(v1["ledger"]["est_cost"], v2["ledger"]["est_cost"])))
    )


def render_comparison_low_high(comp):
    low, high = comp["low"], comp["high"]
    return (
        '<section aria-label="v2 low versus v2 high"><h2>Job scoring: '
        "v2 low vs v2 high effort</h2>"
        '<div class="compare">'
        '<div class="compare-side"><h3>v2 low</h3><dl>'
        "<dt>Agreement</dt><dd>%s</dd>"
        "<dt>Total estimated cost</dt><dd>%s</dd>"
        "<dt>Mean latency</dt><dd>%s ms</dd>"
        "</dl><p><small>sources: "
        "lanes/results/job_scoring-v2-low-results.json; "
        "lanes/ledger-v2-low.jsonl</small></p></div>"
        '<div class="compare-side"><h3>v2 high</h3><dl>'
        "<dt>Agreement</dt><dd>%s</dd>"
        "<dt>Total estimated cost</dt><dd>%s</dd>"
        "<dt>Mean latency</dt><dd>%s ms</dd>"
        "</dl><p><small>sources: "
        "lanes/results/job_scoring-v2-high-results.json; "
        "lanes/ledger-v2-high.jsonl</small></p></div>"
        "</div>"
        '<p class="delta">%s. %s. %s.</p>'
        "</section>"
        % (esc(fmt_rate(low["agreement"])), fmt_cost(low["ledger"]["est_cost"]),
           fmt_ms(low["ledger"]["mean_ms"]),
           esc(fmt_rate(high["agreement"])), fmt_cost(high["ledger"]["est_cost"]),
           fmt_ms(high["ledger"]["mean_ms"]),
           esc(_delta_rate(low["agreement"], high["agreement"])),
           esc(_delta_cost(low["ledger"]["est_cost"], high["ledger"]["est_cost"])),
           esc(_delta_lat(low["ledger"]["mean_ms"], high["ledger"]["mean_ms"])))
    )


def render_comparison_mail(comp):
    v1, v2 = comp["v1"], comp["v2low"]
    return (
        '<section aria-label="mail v1 versus v2 low"><h2>Mail triage: '
        'v1 vs v2 low</h2>'
        '<div class="compare">'
        '<div class="compare-side"><h3>v1</h3><dl>'
        "<dt>Agreement</dt><dd>%s</dd>"
        "<dt>Estimated cost</dt><dd>%s</dd>"
        "</dl><p><small>sources: lanes/results/mail_triage-results.json; "
        "lanes/ledger.jsonl lane mail_triage</small></p></div>"
        '<div class="compare-side"><h3>v2 low</h3><dl>'
        "<dt>Agreement</dt><dd>%s</dd>"
        "<dt>Estimated cost</dt><dd>%s</dd>"
        "</dl><p><small>sources: "
        "lanes/results/mail_triage-v2-low-results.json; "
        "lanes/ledger-mail-v2-low.jsonl</small></p></div>"
        "</div>"
        '<p class="delta">%s. %s.</p>'
        "</section>"
        % (esc(fmt_rate(v1["agreement"])), fmt_cost(v1["ledger"]["est_cost"]),
           esc(fmt_rate(v2["agreement"])), fmt_cost(v2["ledger"]["est_cost"]),
           esc(_delta_rate(v1["agreement"], v2["agreement"])),
           esc(_delta_cost(v1["ledger"]["est_cost"], v2["ledger"]["est_cost"])))
    )


def render_charts(stats):
    agree_rows = []
    for a in stats["agreements"]:
        frac = a["rate_pct"] / 100.0 if a["rate_pct"] is not None else 0.0
        agree_rows.append((a["label"], frac, fmt_rate(a)))
    max_mean = max([m["mean_ms"] for m in stats["models"]
                    if m["mean_ms"] is not None] or [0]) or 1
    lat_rows = [(m["model"],
                 (m["mean_ms"] / max_mean) if m["mean_ms"] is not None else 0.0,
                 "%s ms mean" % fmt_ms(m["mean_ms"]))
                for m in stats["models"]]
    max_cost = max([s["est_cost"] for s in stats["ledgers"]] or [0]) or 1
    cost_rows = [(s["name"], s["est_cost"] / max_cost, fmt_cost(s["est_cost"]))
                 for s in stats["ledgers"]]
    return (
        '<section aria-label="Charts"><h2>Charts</h2>'
        "<figure class=\"chart\">%s<figcaption>"
        "Agreement rate per results file.</figcaption></figure>"
        "<figure class=\"chart\">%s<figcaption>"
        "Mean latency per model, status-ok calls only.</figcaption></figure>"
        "<figure class=\"chart\">%s<figcaption>"
        "Estimated cost per ledger.</figcaption></figure>"
        "</section>"
        % (svg_bars("Agreement rate per results file", agree_rows),
           svg_bars("Mean latency per model", lat_rows),
           svg_bars("Estimated cost per ledger", cost_rows))
    )


def render_sources():
    items = "".join("<li>%s</li>" % esc(p) for p in LEDGER_PATHS + RESULTS_PATHS)
    return (
        '<section aria-label="Sources"><h2>Sources</h2>'
        '<p class="sources">Every number on this page was computed at build '
        "time from these read-only sources. Costs are estimates: the Spark "
        "prices in the router config are placeholders.</p>"
        '<ul class="sources">%s</ul></section>' % items
    )


def render_footer():
    links = (
        '<li><a href="https://github.com/Kazi16">github.com/Kazi16</a></li>'
        '<li><a href="https://linkedin.com/in/kaziprince">'
        "linkedin.com/in/kaziprince</a></li>"
        '<li><a href="https://dev.meta.ai/events/global-hackathon">'
        "Meta Global AI Developer Hackathon</a></li>"
    )
    return (
        '<footer class="page"><p>Built by Suite Fleet. Credit: %s.</p><p>© 2026 Kazi Paruaz Prince. All rights reserved.</p><p>Muse and Meta are trademarks of Meta Platforms, Inc. This project is independent and is not endorsed by Meta.</p>'
        "<ul>%s</ul></footer>" % (esc(FOOTER_CREDIT), links)
    )


def render(stats):
    totals = stats["totals"]
    return (
        "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n"
        "<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        "<title>Suite Fleet results</title>\n"
        "<style>%s</style>\n</head>\n<body>\n<main>\n"
        '<header class="page"><h1>Suite Fleet results</h1>'
        "<p>Two Meta models, Muse Spark and Muse Wonder, run the same real "
        "jobs in benchmark mode while the router ledgers every call; "
        "this page is drawn from those ledgers.</p></header>\n"
        "%s\n%s\n%s\n%s\n%s\n%s\n%s\n%s\n%s\n%s\n"
        "</main>\n</body>\n</html>\n"
        % (CSS,
           render_totals(totals),
           render_ledger_table(stats["ledgers"]),
           render_model_table(stats["models"]),
           render_agreement(stats["agreements"]),
           render_comparison_v1_v2low(stats["comp_v1_v2low"]),
           render_comparison_low_high(stats["comp_low_high"]),
           render_comparison_mail(stats["comp_mail"]),
           render_charts(stats),
           render_sources(),
           render_footer())
    )


# --------------------------------------------------------------------------
# Privacy scan
# --------------------------------------------------------------------------

def scan_html(page):
    """Fail loud if the generated page leaks private patterns.

    This builder emits aggregates and fixed labels only, and no per-item
    details elements, so any hit below is a bug and blocks the write.
    """
    problems = []
    if EMAIL_RE.search(page):
        problems.append("email address pattern matched")
    outside_footer = FOOTER_RE.sub("", page)
    if "kaziprince" in outside_footer.lower():
        problems.append('"kaziprince" appears outside the footer')
    outside_details = DETAILS_RE.sub("", page)
    if HEX_RUN_RE.search(outside_details):
        problems.append("24+ hex character run outside an allowed details element")
    if "META_MODEL_API_KEY" in page:
        problems.append('"META_MODEL_API_KEY" appears in output')
    if "api.meta.ai" in page:
        problems.append('"api.meta.ai" appears in output')
    if problems:
        raise DashboardBuildError("privacy scan failed: " + "; ".join(problems))


# --------------------------------------------------------------------------
# Build entry points
# --------------------------------------------------------------------------

def build(source_root, output_path):
    """Build the dashboard. Returns the stats dict.

    On any failure the output file is not left behind: a stale previous
    output is removed.
    """
    try:
        stats = compute_stats(source_root)
        page = render(stats)
        scan_html(page)
        tmp_path = output_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as fh:
            fh.write(page)
        os.replace(tmp_path, output_path)
        return stats
    except Exception:
        for path in (output_path, output_path + ".tmp"):
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass
        raise


def default_paths():
    here = os.path.dirname(os.path.abspath(__file__))
    source_root = os.path.dirname(here)
    return source_root, os.path.join(here, "dashboard.html")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Build the Suite Fleet results dashboard.")
    parser.add_argument("--source-root", default=None,
                        help="directory holding routing/ and lanes/")
    parser.add_argument("--output", default=None,
                        help="output HTML path (default: dashboard.html)")
    args = parser.parse_args(argv)
    default_root, default_out = default_paths()
    source_root = args.source_root or default_root
    output_path = args.output or default_out
    try:
        build(source_root, output_path)
    except DashboardBuildError as exc:
        print("build_dashboard: error: %s" % exc, file=sys.stderr)
        return 1
    print("wrote %s" % output_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())

