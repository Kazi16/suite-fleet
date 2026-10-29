"""Suite Fleet Meta lanes: lane adapters on top of fleet_router (SPEC v1).

Each lane takes a batch file holding a list of extract objects, runs every
item through the router in benchmark mode (Spark 1.3 and Wonder both answer,
both ledgered, Spark returned), and writes one results entry per input item.
"""

import argparse
import json
import sys
from pathlib import Path

import fleet_router

MODULE_DIR = Path(__file__).resolve().parent
DEFAULT_RESULTS_DIR = MODULE_DIR / "results"

VALID_LANES = fleet_router.VALID_LANES

# Agreement tolerance for the job_scoring lane: scores within this many
# points (inclusive) count as agreement.
JOB_SCORE_TOLERANCE = 15

# job_scoring bands: score >= 70 strong, >= 40 possible, else weak.
BAND_STRONG_AT = 70
BAND_POSSIBLE_AT = 40

# Default stop-guard for live runs (task rule): stop before starting another
# item once the ledger total reaches this.
DEFAULT_SPEND_CAP_USD = 5.00


class SpendCapExceeded(RuntimeError):
    """Raised when the ledger total reaches spend_cap_usd mid-batch."""

    def __init__(self, message, entries):
        super().__init__(message)
        self.entries = entries


def parse_model_content(content):
    """Parse one model reply into a dict. Never raises.

    Returns the parsed JSON object on success, else a placeholder dict
    {"_parse_error": True, "raw": <text>} so the entry still records it.
    """
    text = content if isinstance(content, str) else ""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            data = json.loads(cleaned[start:end + 1])
        except (json.JSONDecodeError, ValueError):
            data = None
        if isinstance(data, dict):
            return data
    return {"_parse_error": True, "raw": text[:2000]}


def _parsed_or_error(raw):
    """Parsed content of one benchmark leg, or an error placeholder."""
    if not isinstance(raw, dict) or raw.get("status") != "ok":
        raw = raw if isinstance(raw, dict) else {}
        return {
            "_parse_error": True,
            "status": raw.get("status", "missing"),
            "error": str(raw.get("error", ""))[:500],
        }
    return parse_model_content(raw.get("content", ""))


def score_band(score):
    """Band for a job score: strong / possible / weak, None if not numeric."""
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        return None
    if score >= BAND_STRONG_AT:
        return "strong"
    if score >= BAND_POSSIBLE_AT:
        return "possible"
    return "weak"


def lanes_agree(lane, spark, wonder):
    """Whether the two models agree on the lane's key field.

    mail_triage: equal category. job_scoring: scores within 15 points.
    deal_eval: equal verdict. notes_qa: not scored in v1 (None).
    Missing/unparseable key fields count as disagreement (False).
    """
    spark = spark if isinstance(spark, dict) else {}
    wonder = wonder if isinstance(wonder, dict) else {}
    if lane == "mail_triage":
        a, b = spark.get("category"), wonder.get("category")
        return a is not None and b is not None and a == b
    if lane == "job_scoring":
        a, b = spark.get("score"), wonder.get("score")
        if isinstance(a, bool) or isinstance(b, bool):
            return False
        if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
            return False
        return abs(a - b) <= JOB_SCORE_TOLERANCE
    if lane == "deal_eval":
        a, b = spark.get("verdict"), wonder.get("verdict")
        return a is not None and b is not None and a == b
    if lane == "notes_qa":
        return None
    raise ValueError("unknown lane: %r" % (lane,))


def build_entry(lane, item, result):
    """Build one results entry from an input item and its router result."""
    bench = result.get("benchmark_results", []) or []
    spark_raw = bench[0] if len(bench) > 0 else {}
    wonder_raw = bench[1] if len(bench) > 1 else {}
    spark = _parsed_or_error(spark_raw)
    wonder = _parsed_or_error(wonder_raw)
    entry = {
        "id": item.get("id") if isinstance(item, dict) else None,
        "spark": spark,
        "wonder": wonder,
        "agree": lanes_agree(lane, spark, wonder),
        "job_ref": result.get("job_ref"),
        "verdict": "pending",
    }
    if lane == "mail_triage":
        entry["spark_priority"] = spark.get("priority")
        entry["wonder_priority"] = wonder.get("priority")
    elif lane == "job_scoring":
        entry["spark_band"] = score_band(spark.get("score"))
        entry["wonder_band"] = score_band(wonder.get("score"))
    elif lane == "notes_qa":
        entry["spark_answer"] = spark.get("answer")
        entry["wonder_answer"] = wonder.get("answer")
    return entry


def run_lane(lane, input_path, output_path=None, config=None, config_path=None,
             ledger_path=None, transport=None, api_key=None,
             benchmark_override=True, spend_cap_usd=None):
    """Run one lane batch; returns the list of result entries.

    Runs every input item through the router in benchmark mode and writes
    one entry per input item to output_path (default results/<lane>.json
    naming per spec: results/<lane>-results.json). If spend_cap_usd is set
    and the ledger total reaches it, raises SpendCapExceeded carrying the
    entries completed so far (nothing further is sent).
    """
    if lane not in VALID_LANES:
        raise ValueError("unknown lane: %r (expected one of %s)"
                         % (lane, ", ".join(VALID_LANES)))
    with open(input_path, encoding="utf-8") as fh:
        items = json.load(fh)
    if not isinstance(items, list):
        raise ValueError("input file must hold a JSON list: %s" % input_path)
    cfg = dict(config) if config is not None else fleet_router.load_config(config_path)
    ledger_file = fleet_router.resolve_ledger_path(cfg, ledger_path)
    out = Path(output_path) if output_path else DEFAULT_RESULTS_DIR / ("%s-results.json" % lane)
    entries = []
    for item in items:
        if spend_cap_usd is not None:
            spent = fleet_router.current_spend_usd(cfg, str(ledger_file))
            if spent >= float(spend_cap_usd):
                raise SpendCapExceeded(
                    "ledger total %.6f USD reached cap %.2f USD; "
                    "stopping after %d of %d items"
                    % (spent, float(spend_cap_usd), len(entries), len(items)),
                    entries,
                )
        result = fleet_router.run_job(
            lane, item, config=cfg, ledger_path=str(ledger_file),
            transport=transport, api_key=api_key,
            benchmark_override=benchmark_override,
        )
        entries.append(build_entry(lane, item, result))
    if out.parent != Path("") and not out.parent.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(entries, fh, indent=2)
        fh.write("\n")
    return entries


def summarize(entries):
    """Small tally over result entries: items, agreements, pending count."""
    return {
        "items": len(entries),
        "agreements": sum(1 for e in entries if e.get("agree") is True),
        "pending": sum(1 for e in entries if e.get("verdict") == "pending"),
    }


def build_parser():
    parser = argparse.ArgumentParser(prog="lanes",
                                     description="Suite Fleet Meta lane batches")
    parser.add_argument("--lane", required=True, choices=list(VALID_LANES))
    parser.add_argument("--input", required=True, help="path to JSON batch file")
    parser.add_argument("--output", default=None,
                        help="results path (default results/<lane>-results.json)")
    parser.add_argument("--config", default=None, help="path to config JSON")
    parser.add_argument("--ledger", default=None, help="path to ledger JSONL")
    parser.add_argument("--spend-cap-usd", type=float,
                        default=DEFAULT_SPEND_CAP_USD,
                        help="stop before the next item once the ledger total "
                             "reaches this (default %.2f)" % DEFAULT_SPEND_CAP_USD)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        cfg = fleet_router.load_config(args.config)
    except FileNotFoundError:
        print("config file not found: %s"
              % (args.config or fleet_router.DEFAULT_CONFIG_PATH), file=sys.stderr)
        return 1
    ledger_file = fleet_router.resolve_ledger_path(cfg, args.ledger)
    try:
        entries = run_lane(
            args.lane, args.input, output_path=args.output, config=cfg,
            ledger_path=str(ledger_file), spend_cap_usd=args.spend_cap_usd,
        )
    except FileNotFoundError:
        print("input file not found: %s" % args.input, file=sys.stderr)
        return 1
    except (ValueError, KeyError, RuntimeError) as exc:
        partial = exc.entries if isinstance(exc, SpendCapExceeded) else None
        if partial is not None:
            out = Path(args.output) if args.output else DEFAULT_RESULTS_DIR / (
                "%s-results.json" % args.lane)
            if out.parent != Path("") and not out.parent.exists():
                out.parent.mkdir(parents=True, exist_ok=True)
            with open(out, "w", encoding="utf-8") as fh:
                json.dump(partial, fh, indent=2)
                fh.write("\n")
        print("error: %s" % exc, file=sys.stderr)
        return 3 if partial is not None else 1
    spent = fleet_router.current_spend_usd(cfg, str(ledger_file))
    tally = summarize(entries)
    tally.update({"lane": args.lane, "ledger_total_usd": spent,
                  "stopped_early": False})
    print(json.dumps(tally, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
