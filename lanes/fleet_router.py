"""Suite Fleet routing layer (SPEC v1).

One entry point for model work: pick the model, call the Meta Model API,
and append one ledger line per call.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = MODULE_DIR / "config.json"

VALID_LANES = ("mail_triage", "job_scoring", "deal_eval", "notes_qa")
VALID_VERDICTS = ("accepted", "rejected", "pending")
VALID_STATUSES = ("ok", "error", "refused_budget")

API_BASE_FALLBACK = "https://api.meta.ai/v1"

LANE_PROMPTS = {
    "mail_triage": (
        "You classify and summarize a structured mail extract. "
        "Reply with JSON only: {\"category\": str, \"summary\": str, "
        "\"priority\": \"high|medium|low\", \"confidence\": \"high|medium|low\"}."
    ),
    "job_scoring": (
        "You score a job lead against the operator profile. "
        "Reply with JSON only: {\"score\": 0-100, \"reasons\": [str], "
        "\"confidence\": \"high|medium|low\"}."
    ),
    "deal_eval": (
        "You evaluate a deal or offer for value and risk. "
        "Reply with JSON only: {\"verdict\": str, \"value\": str, "
        "\"risks\": [str], \"confidence\": \"high|medium|low\"}."
    ),
    "notes_qa": (
        "You answer a question against a notes extract. "
        "Reply with JSON only: {\"answer\": str, "
        "\"confidence\": \"high|medium|low\"}."
    ),
}

LOW_CONFIDENCE_STRINGS = ("low", "very low", "uncertain", "unsure")
CONFIDENCE_NUMERIC_THRESHOLD = 0.6


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def load_config(config_path=None):
    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def resolve_ledger_path(config, ledger_path=None):
    if ledger_path:
        return Path(ledger_path)
    configured = (config or {}).get("ledger_path", "ledger.jsonl")
    candidate = Path(configured)
    if not candidate.is_absolute():
        return MODULE_DIR / candidate
    return candidate


def estimate_cost_usd(model, prompt_tokens, completion_tokens, config):
    table = (config or {}).get("prices_usd_per_1k_tokens", {})
    entry = table.get(model)
    if not entry:
        return 0.0
    prompt_rate = float(entry.get("prompt", 0.0))
    completion_rate = float(entry.get("completion", 0.0))
    cost = (prompt_tokens / 1000.0) * prompt_rate
    cost += (completion_tokens / 1000.0) * completion_rate
    return round(cost, 6)


def current_spend_usd(config, ledger_path=None):
    if "budget_cap_usd" not in config or "starting_offset_usd" not in config:
        raise KeyError("config must define budget_cap_usd and starting_offset_usd")
    total = float(config["starting_offset_usd"])
    path = resolve_ledger_path(config, ledger_path)
    if not path.exists():
        return round(total, 6)
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("type") == "verdict":
                continue
            value = record.get("est_cost_usd")
            if isinstance(value, (int, float)):
                total += float(value)
    return round(total, 6)


def _append_ledger_line(record, ledger_path):
    path = Path(ledger_path)
    if path.parent != Path("") and not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


def build_messages(lane, input_data):
    system = LANE_PROMPTS[lane]
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(input_data)},
    ]


def _extract_usage(payload):
    usage = payload.get("usage", {}) or {}
    prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
    completion_tokens = int(usage.get("completion_tokens", 0) or 0)
    reasoning_tokens = usage.get("reasoning_tokens")
    if reasoning_tokens is None:
        details = usage.get("completion_tokens_details", {}) or {}
        reasoning_tokens = details.get("reasoning_tokens")
    if reasoning_tokens is not None:
        reasoning_tokens = int(reasoning_tokens)
    return prompt_tokens, completion_tokens, reasoning_tokens


def real_transport(model, messages, reasoning_effort, api_base, api_key, timeout=60):
    url = api_base.rstrip("/") + "/chat/completions"
    body = {
        "model": model,
        "messages": messages,
        "reasoning_effort": reasoning_effort,
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + api_key,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", "replace")[:500]
        except Exception:
            detail = ""
        raise RuntimeError("API HTTP %s: %s" % (exc.code, detail))
    except urllib.error.URLError as exc:
        raise RuntimeError("API request failed: %s" % exc)
    choices = payload.get("choices", [])
    if not choices:
        raise RuntimeError("API response has no choices")
    message = choices[0].get("message", {}) or {}
    content = message.get("content", "")
    if not isinstance(content, str):
        content = json.dumps(content)
    prompt_tokens, completion_tokens, reasoning_tokens = _extract_usage(payload)
    result = {
        "content": content,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
    }
    if reasoning_tokens is not None:
        result["reasoning_tokens"] = reasoning_tokens
    return result


def _strip_code_fences(text):
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()
    return cleaned


def is_low_confidence(content):
    try:
        cleaned = _strip_code_fences(content)
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return False
        data = json.loads(cleaned[start : end + 1])
    except (json.JSONDecodeError, ValueError, AttributeError):
        return False
    if not isinstance(data, dict) or "confidence" not in data:
        return False
    value = data["confidence"]
    if isinstance(value, str):
        return value.strip().lower() in LOW_CONFIDENCE_STRINGS
    if isinstance(value, (int, float)):
        return float(value) < CONFIDENCE_NUMERIC_THRESHOLD
    return False


def _refused_line(ts, job_ref, lane, model, effort):
    return {
        "ts": ts,
        "type": "call",
        "job_ref": job_ref,
        "lane": lane,
        "model": model,
        "reasoning_effort": effort,
        "latency_ms": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "est_cost_usd": 0.0,
        "status": "refused_budget",
        "verdict": "pending",
    }


def _call_model(job_ref, lane, model, effort, messages, config, ledger_file,
                transport_fn, cap):
    spent = current_spend_usd(config, ledger_file)
    if spent >= float(cap):
        line = _refused_line(utc_now_iso(), job_ref, lane, model, effort)
        _append_ledger_line(line, ledger_file)
        return {
            "job_ref": job_ref,
            "lane": lane,
            "model": model,
            "status": "refused_budget",
            "error": "budget cap reached: spend %.6f >= cap" % spent,
            "latency_ms": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "est_cost_usd": 0.0,
        }
    start = time.perf_counter()
    try:
        outcome = transport_fn(model, messages, effort)
    except Exception as exc:
        latency_ms = int((time.perf_counter() - start) * 1000)
        line = {
            "ts": utc_now_iso(),
            "type": "call",
            "job_ref": job_ref,
            "lane": lane,
            "model": model,
            "reasoning_effort": effort,
            "latency_ms": latency_ms,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "est_cost_usd": 0.0,
            "status": "error",
            "verdict": "pending",
            "error": str(exc)[:500],
        }
        _append_ledger_line(line, ledger_file)
        return {
            "job_ref": job_ref,
            "lane": lane,
            "model": model,
            "status": "error",
            "error": str(exc)[:500],
            "latency_ms": latency_ms,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "est_cost_usd": 0.0,
        }
    latency_ms = int((time.perf_counter() - start) * 1000)
    prompt_tokens = int(outcome.get("prompt_tokens", 0) or 0)
    completion_tokens = int(outcome.get("completion_tokens", 0) or 0)
    reasoning_tokens = outcome.get("reasoning_tokens")
    cost = estimate_cost_usd(model, prompt_tokens, completion_tokens, config)
    line = {
        "ts": utc_now_iso(),
        "type": "call",
        "job_ref": job_ref,
        "lane": lane,
        "model": model,
        "reasoning_effort": effort,
        "latency_ms": latency_ms,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "est_cost_usd": cost,
        "status": "ok",
        "verdict": "pending",
    }
    if reasoning_tokens is not None:
        line["reasoning_tokens"] = int(reasoning_tokens)
    _append_ledger_line(line, ledger_file)
    result = {
        "job_ref": job_ref,
        "lane": lane,
        "model": model,
        "status": "ok",
        "content": outcome.get("content", ""),
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "latency_ms": latency_ms,
        "est_cost_usd": cost,
    }
    if reasoning_tokens is not None:
        result["reasoning_tokens"] = int(reasoning_tokens)
    return result


def run_job(lane, input_data, job_ref=None, config=None, config_path=None,
            ledger_path=None, transport=None, api_key=None,
            benchmark_override=None):
    if lane not in VALID_LANES:
        raise ValueError("unknown lane: %r (expected one of %s)" % (lane, ", ".join(VALID_LANES)))
    cfg = dict(config) if config is not None else load_config(config_path)
    if "budget_cap_usd" not in cfg or "starting_offset_usd" not in cfg:
        raise KeyError("config must define budget_cap_usd and starting_offset_usd")
    lanes = cfg.get("lanes", {})
    if lane not in lanes:
        raise KeyError("config has no lane %r" % lane)
    lane_cfg = lanes[lane]
    default_model = lane_cfg["default_model"]
    effort = lane_cfg["reasoning_effort"]
    heavy_model = lane_cfg["heavy_model"]
    benchmark = lane_cfg.get("benchmark", False)
    if benchmark_override is not None:
        benchmark = bool(benchmark_override)
    escalation_allowed = lane_cfg.get("escalation_allowed", False)

    ledger_file = resolve_ledger_path(cfg, ledger_path)
    ref = job_ref or uuid.uuid4().hex[:12]
    messages = build_messages(lane, input_data)
    cap = cfg["budget_cap_usd"]

    if transport is not None:
        transport_fn = transport
    else:
        key = api_key or os.environ.get("META_MODEL_API_KEY", "")
        if not key:
            raise RuntimeError("META_MODEL_API_KEY is not set")
        api_base = cfg.get("api_base", API_BASE_FALLBACK)

        def transport_fn(model, msgs, eff):
            return real_transport(model, msgs, eff, api_base, key)

    heavy_requested = False
    if isinstance(input_data, dict) and input_data.get("heavy") is True:
        heavy_requested = True
    if lane_cfg.get("force_heavy") is True:
        heavy_requested = True

    if benchmark:
        first = _call_model(ref, lane, default_model, effort, messages,
                            cfg, ledger_file, transport_fn, cap)
        second = _call_model(ref, lane, heavy_model, effort, messages,
                             cfg, ledger_file, transport_fn, cap)
        first = dict(first)
        first["benchmark"] = True
        first["escalated"] = False
        first["benchmark_results"] = [first_copy(first), first_copy(second)]
        return first

    if heavy_requested:
        result = _call_model(ref, lane, heavy_model, effort, messages,
                             cfg, ledger_file, transport_fn, cap)
        result = dict(result)
        result["benchmark"] = False
        result["escalated"] = False
        result["heavy"] = True
        return result

    first = _call_model(ref, lane, default_model, effort, messages,
                        cfg, ledger_file, transport_fn, cap)
    if first.get("status") != "ok":
        result = dict(first)
        result["benchmark"] = False
        result["escalated"] = False
        return result
    if escalation_allowed and is_low_confidence(first.get("content", "")):
        second = _call_model(ref, lane, heavy_model, effort, messages,
                             cfg, ledger_file, transport_fn, cap)
        if second.get("status") == "ok":
            escalated = dict(second)
            escalated["benchmark"] = False
            escalated["escalated"] = True
            escalated["initial_model"] = first.get("model")
            escalated["initial_content"] = first.get("content", "")
            return escalated
        result = dict(first)
        result["benchmark"] = False
        result["escalated"] = False
        result["escalation_attempt"] = first_copy(second)
        return result
    result = dict(first)
    result["benchmark"] = False
    result["escalated"] = False
    return result


def first_copy(result):
    return {key: result.get(key) for key in (
        "model", "status", "content", "prompt_tokens", "completion_tokens",
        "reasoning_tokens", "latency_ms", "est_cost_usd", "error",
    ) if key in result}


def set_verdict(job_ref, verdict, ledger_path=None, config_path=None):
    if verdict not in VALID_VERDICTS:
        raise ValueError("unknown verdict: %r (expected one of %s)"
                         % (verdict, ", ".join(VALID_VERDICTS)))
    cfg = load_config(config_path)
    ledger_file = resolve_ledger_path(cfg, ledger_path)
    line = {
        "ts": utc_now_iso(),
        "type": "verdict",
        "job_ref": job_ref,
        "verdict": verdict,
    }
    _append_ledger_line(line, ledger_file)
    return line


def _cmd_run(args):
    try:
        with open(args.input, encoding="utf-8") as fh:
            input_data = json.load(fh)
    except FileNotFoundError:
        print("input file not found: %s" % args.input, file=sys.stderr)
        return 1
    except json.JSONDecodeError as exc:
        print("input file is not valid JSON: %s" % exc, file=sys.stderr)
        return 1
    try:
        cfg = load_config(args.config)
    except FileNotFoundError:
        print("config file not found: %s" % (args.config or DEFAULT_CONFIG_PATH),
              file=sys.stderr)
        return 1
    benchmark_override = None
    if args.no_benchmark:
        benchmark_override = False
    try:
        result = run_job(
            args.lane,
            input_data,
            job_ref=args.job_ref,
            config=cfg,
            ledger_path=args.ledger,
            benchmark_override=benchmark_override,
        )
    except (ValueError, KeyError, RuntimeError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    if result.get("status") == "refused_budget":
        return 2
    if result.get("status") != "ok":
        return 1
    return 0


def _cmd_set_verdict(args):
    try:
        line = set_verdict(args.job_ref, args.verdict,
                           ledger_path=args.ledger, config_path=args.config)
    except (ValueError, FileNotFoundError, KeyError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    print(json.dumps(line, indent=2))
    return 0


def build_parser():
    parser = argparse.ArgumentParser(prog="fleet_router",
                                     description="Suite Fleet routing layer")
    sub = parser.add_subparsers(dest="command", required=True)
    run_p = sub.add_parser("run", help="run one routing job")
    run_p.add_argument("--lane", required=True, choices=list(VALID_LANES))
    run_p.add_argument("--input", required=True, help="path to JSON input file")
    run_p.add_argument("--job-ref", default=None)
    run_p.add_argument("--config", default=None, help="path to config JSON")
    run_p.add_argument("--ledger", default=None, help="path to ledger JSONL")
    run_p.add_argument("--no-benchmark", action="store_true",
                       help="run the default route once instead of benchmark dual run")
    run_p.set_defaults(func=_cmd_run)
    verdict_p = sub.add_parser("set-verdict", help="append a verdict line")
    verdict_p.add_argument("--job-ref", required=True)
    verdict_p.add_argument("--verdict", required=True, choices=list(VALID_VERDICTS))
    verdict_p.add_argument("--config", default=None)
    verdict_p.add_argument("--ledger", default=None)
    verdict_p.set_defaults(func=_cmd_set_verdict)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
