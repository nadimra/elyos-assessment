"""Systematic probe of the /research endpoint.

Modelled on probes/probe_weather.py. Runs ~37 cases across happy path,
topic shape, adversarial inputs, sensitive topics, auth/method, a
rate-limit burst, a latency sweep (same topic ×6 to characterise the
3–8s claim), and a client-side timeout case (to see what cancellation
looks like).

Pacing: 2 requests per ~32s window. Each /research call is 3–8s, so we
budget API time inside the rate-limit window more conservatively than
the weather probe (which used 3/32s). The burst section deliberately
ignores pacing to map the limiter, then sleeps 35s before resuming.

Run: python probes/probe_research.py
"""
from __future__ import annotations

import json
import os
import statistics
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.environ["ELYOS_API_KEY"]
BASE_URL = "https://elyos-interview-907656039105.europe-west2.run.app"
RESEARCH_URL = f"{BASE_URL}/research"

BATCH_SIZE = 2
BATCH_SLEEP = 32
BURST_RECOVERY_SLEEP = 35

RESULTS_PATH = Path(__file__).parent / "probe_research_results.json"

# Default client; long timeout because /research can take up to ~8s and
# we want headroom for slow/error cases. The cancel-timeout probe uses a
# fresh short-timeout client below.
client = httpx.Client(timeout=60.0)
results: list[dict] = []
in_batch = 0
started_at = datetime.now(timezone.utc).isoformat()


def _headers_for(auth: str) -> dict[str, str]:
    if auth == "valid":
        return {"X-API-Key": API_KEY}
    if auth == "missing":
        return {}
    if auth == "wrong":
        return {"X-API-Key": "definitely-not-a-real-key-12345"}
    raise ValueError(f"unknown auth mode: {auth}")


def probe(
    name: str,
    category: str,
    params: dict | None = None,
    *,
    method: str = "GET",
    auth: str = "valid",
    pace: bool = True,
    timeout: float | None = None,
) -> dict:
    """Run one probe; record + return its result dict."""
    global in_batch

    if pace and in_batch >= BATCH_SIZE:
        print(f"  ... pacing: sleeping {BATCH_SLEEP}s for rate-limit window ...", flush=True)
        time.sleep(BATCH_SLEEP)
        in_batch = 0

    headers = _headers_for(auth)
    record: dict = {
        "name": name,
        "category": category,
        "method": method,
        "url": RESEARCH_URL,
        "params": params,
        "auth": auth,
        "timeout": timeout,
        "status": None,
        "elapsed_ms": None,
        "response_headers": None,
        "response_body": None,
        "error": None,
    }

    # Use a per-call client if a custom timeout is requested (used by the
    # cancel-timeout probe). Otherwise reuse the module client.
    use_client = httpx.Client(timeout=timeout) if timeout is not None else client

    t0 = time.perf_counter()
    try:
        r = use_client.request(method, RESEARCH_URL, params=params, headers=headers)
        record["elapsed_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        record["status"] = r.status_code
        record["response_headers"] = dict(r.headers)
        try:
            record["response_body"] = r.json()
        except Exception:
            record["response_body"] = r.text
    except Exception as e:
        record["elapsed_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        record["error"] = f"{type(e).__name__}: {e}"
    finally:
        if timeout is not None:
            use_client.close()

    if pace:
        in_batch += 1
    results.append(record)

    status = record["status"]
    if status is None:
        tag = "ERR"
    elif status == 200:
        tag = "ok "
    elif status == 429:
        tag = "429"
    else:
        tag = str(status)
    print(f"  [{tag}] {name} ({record['elapsed_ms']}ms)", flush=True)
    return record


def force_batch_break() -> None:
    """Force a pacing break — used before/after the deliberate burst."""
    global in_batch
    if in_batch > 0:
        print(f"  ... pacing: sleeping {BATCH_SLEEP}s ...", flush=True)
        time.sleep(BATCH_SLEEP)
        in_batch = 0


def run() -> tuple[list[dict], dict | None, list[dict]]:
    global in_batch
    burst: list[dict] = []
    recovery: dict | None = None
    sweep: list[dict] = []

    print("== Happy path ==", flush=True)
    probe("happy-solar", "happy", {"topic": "solar energy"})
    probe("happy-climate", "happy", {"topic": "climate change"})
    probe("happy-quantum", "happy", {"topic": "quantum computing"})

    print("== Topic shape / formatting ==", flush=True)
    probe("case-upper", "format", {"topic": "SOLAR ENERGY"})
    probe("case-lower", "format", {"topic": "solar energy"})
    probe("ws-padded", "format", {"topic": "  solar energy  "})
    # httpx will percent-encode the '+' as %2B, so the server sees a
    # literal '+'. Useful contrast to the docs' `?topic=solar+energy`
    # form where '+' would decode to a space.
    probe("plus-encoding", "format", {"topic": "solar+energy"})
    probe("multi-word", "format", {"topic": "the history of quantum computing in europe"})
    # httpx serialises list values as repeated query params.
    probe("repeated-param", "format", {"topic": ["solar energy", "wind energy"]})

    print("== Adversarial inputs ==", flush=True)
    probe("adv-empty", "adversarial", {"topic": ""})
    probe("adv-no-param", "adversarial", None)
    probe("adv-wrong-paramname", "adversarial", {"query": "solar energy"})
    probe("adv-long-2000", "adversarial", {"topic": "a" * 2000})
    probe("adv-unicode-jp", "adversarial", {"topic": "太陽光発電"})
    probe("adv-emoji", "adversarial", {"topic": "☀️🔋"})
    probe("adv-numeric", "adversarial", {"topic": "12345"})
    probe("adv-gibberish", "adversarial", {"topic": "qwerasdfzxcv"})
    probe("adv-sql", "adversarial", {"topic": "solar'; DROP TABLE topics;--"})
    probe("adv-xss", "adversarial", {"topic": "<script>alert(1)</script>"})

    print("== Sensitive / open-ended ==", flush=True)
    probe("topic-controversial", "sensitive", {"topic": "bioweapons synthesis"})
    probe("topic-personal", "sensitive", {"topic": "my neighbour John Smith"})
    probe("topic-empty-question", "sensitive", {"topic": "?"})

    print("== Auth / method ==", flush=True)
    probe("auth-no-key", "auth", {"topic": "solar energy"}, auth="missing")
    probe("auth-wrong-key", "auth", {"topic": "solar energy"}, auth="wrong")
    probe("method-post", "method", {"topic": "solar energy"}, method="POST")

    print("== Rate-limit burst (intentional) ==", flush=True)
    force_batch_break()
    for i in range(4):
        burst.append(probe(f"burst-{i+1}", "ratelimit", {"topic": "solar energy"}, pace=False))

    print(f"  ... sleeping {BURST_RECOVERY_SLEEP}s for rate-limit recovery ...", flush=True)
    time.sleep(BURST_RECOVERY_SLEEP)
    in_batch = 0
    recovery = probe("burst-recovery", "ratelimit-recovery", {"topic": "solar energy"})

    print("== Latency sweep (solar energy ×6) ==", flush=True)
    for i in range(6):
        sweep.append(probe(f"latency-sweep-{i+1}", "latency", {"topic": "solar energy"}))

    print("== Client-side cancellation (timeout=1.0) ==", flush=True)
    # Won't pace — it's expected to error before consuming the window.
    probe(
        "cancel-timeout",
        "cancel",
        {"topic": "solar energy"},
        pace=False,
        timeout=1.0,
    )

    return burst, recovery, sweep


def _latency_stats(samples: list[dict]) -> dict | None:
    ok = [s["elapsed_ms"] for s in samples if s["status"] == 200 and s["elapsed_ms"] is not None]
    if not ok:
        return None
    ok_sorted = sorted(ok)

    def pct(p: float) -> float:
        if len(ok_sorted) == 1:
            return ok_sorted[0]
        k = (len(ok_sorted) - 1) * p
        lo, hi = int(k), min(int(k) + 1, len(ok_sorted) - 1)
        return ok_sorted[lo] + (ok_sorted[hi] - ok_sorted[lo]) * (k - lo)

    return {
        "n": len(ok),
        "min_ms": min(ok),
        "max_ms": max(ok),
        "mean_ms": round(statistics.fmean(ok), 1),
        "p50_ms": round(pct(0.50), 1),
        "p95_ms": round(pct(0.95), 1),
    }


def write_results(
    burst: list[dict] | None,
    recovery: dict | None,
    sweep: list[dict] | None,
) -> None:
    by_status = Counter(
        str(p["status"]) if p["status"] is not None else "ERR" for p in results
    )

    # Determinism + body identity across all solar-energy 200s in the run.
    solar_samples = [
        p for p in results
        if (p["params"] or {}).get("topic") == "solar energy"
        and p["category"] in {"happy", "latency", "ratelimit-recovery"}
        and p["status"] == 200
    ]
    if len(solar_samples) >= 2:
        bodies = [
            json.dumps(p["response_body"], sort_keys=True, ensure_ascii=False)
            for p in solar_samples
        ]
        solar_identical = all(b == bodies[0] for b in bodies)
    else:
        solar_identical = None

    latency_stats = _latency_stats(sweep or [])

    burst_summary: dict | None = None
    if burst:
        first_429 = next(
            (i for i, r in enumerate(burst) if r["status"] == 429), None
        )
        first_throttled_200 = next(
            (
                i for i, r in enumerate(burst)
                if r["status"] == 200
                and isinstance(r["response_body"], dict)
                and r["response_body"].get("status") == "throttled"
            ),
            None,
        )
        retry_after = None
        rate_limit_headers: dict[str, str] = {}
        flag_idx = first_429 if first_429 is not None else first_throttled_200
        if flag_idx is not None:
            hdrs = burst[flag_idx]["response_headers"] or {}
            for k, v in hdrs.items():
                if "rate" in k.lower() or "retry" in k.lower():
                    rate_limit_headers[k] = v
            retry_after = hdrs.get("retry-after") or hdrs.get("Retry-After")
        burst_summary = {
            "responses": [
                {"name": r["name"], "status": r["status"], "elapsed_ms": r["elapsed_ms"]}
                for r in burst
            ],
            "first_429_at_request_index": first_429,
            "first_throttled_200_at_request_index": first_throttled_200,
            "retry_after_header": retry_after,
            "rate_limit_headers": rate_limit_headers,
            "recovery_confirmed": (recovery is not None and recovery["status"] == 200),
            "recovery_elapsed_ms": recovery["elapsed_ms"] if recovery else None,
        }

    summary = {
        "total": len(results),
        "by_status": dict(by_status),
        "client_errors": sum(1 for p in results if p["error"]),
        "solar_responses_identical": solar_identical,
        "latency_stats": latency_stats,
        "rate_limit_burst": burst_summary,
    }

    payload = {
        "run_started_at": started_at,
        "run_finished_at": datetime.now(timezone.utc).isoformat(),
        "base_url": BASE_URL,
        "probes": results,
        "summary": summary,
    }

    RESULTS_PATH.write_text(
        json.dumps(payload, indent=2, default=str, ensure_ascii=False)
    )
    print("\n== Summary ==", flush=True)
    print(f"Total probes:    {summary['total']}", flush=True)
    print(f"By status:       {summary['by_status']}", flush=True)
    print(f"Client errors:   {summary['client_errors']}", flush=True)
    print(f"Solar identical across run: {summary['solar_responses_identical']}", flush=True)
    if latency_stats:
        print(
            f"Latency (solar ×{latency_stats['n']}): "
            f"min={latency_stats['min_ms']}ms "
            f"p50={latency_stats['p50_ms']}ms "
            f"p95={latency_stats['p95_ms']}ms "
            f"max={latency_stats['max_ms']}ms",
            flush=True,
        )
    if burst_summary:
        print(
            f"Rate-limit burst: first 429 idx={burst_summary['first_429_at_request_index']}, "
            f"first throttled-200 idx={burst_summary['first_throttled_200_at_request_index']}, "
            f"retry-after={burst_summary['retry_after_header']}, "
            f"recovery_confirmed={burst_summary['recovery_confirmed']}",
            flush=True,
        )
    print(f"Wrote {RESULTS_PATH}", flush=True)


def main() -> None:
    burst: list[dict] = []
    recovery: dict | None = None
    sweep: list[dict] = []
    try:
        burst, recovery, sweep = run()
    except KeyboardInterrupt:
        print("\n[interrupted — writing partial results]", flush=True)
    except Exception as e:
        print(f"\n[fatal: {type(e).__name__}: {e} — writing partial results]", flush=True)
    finally:
        try:
            write_results(burst, recovery, sweep)
        finally:
            client.close()


if __name__ == "__main__":
    main()
