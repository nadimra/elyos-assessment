"""Systematic probe of the /weather endpoint.

Runs ~45 cases across happy path, name disambiguation, formatting,
adversarial inputs, auth/method, determinism, and a deliberate
rate-limit burst. Writes full request/response detail to
probes/probe_weather_results.json.

Pacing: 3 requests per ~32s window (the observed limit is ~4 / 30s, so
one head-room request below). The burst section deliberately ignores
pacing to map the limiter's behaviour, then sleeps 35s before resuming.

Run: python probes/probe_weather.py
"""
from __future__ import annotations

import json
import os
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.environ["ELYOS_API_KEY"]
BASE_URL = "https://elyos-interview-907656039105.europe-west2.run.app"
WEATHER_URL = f"{BASE_URL}/weather"

BATCH_SIZE = 3
BATCH_SLEEP = 32
BURST_RECOVERY_SLEEP = 35

RESULTS_PATH = Path(__file__).parent / "probe_weather_results.json"

client = httpx.Client(timeout=30.0)
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
        "url": WEATHER_URL,
        "params": params,
        "auth": auth,
        "status": None,
        "elapsed_ms": None,
        "response_headers": None,
        "response_body": None,
        "error": None,
    }

    t0 = time.perf_counter()
    try:
        r = client.request(method, WEATHER_URL, params=params, headers=headers)
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


def run() -> tuple[list[dict], dict | None]:
    global in_batch
    burst: list[dict] = []
    recovery: dict | None = None

    print("== Happy path ==", flush=True)
    probe("happy-london", "happy", {"location": "London"})
    probe("happy-tokyo", "happy", {"location": "Tokyo"})
    probe("happy-newyork", "happy", {"location": "New York"})

    print("== Ambiguous / duplicate names ==", flush=True)
    for city in ["Springfield", "Cambridge", "Boston", "Newcastle", "Athens", "Tripoli"]:
        slug = city.lower().replace(" ", "-")
        probe(f"ambig-{slug}", "ambiguous", {"location": city})

    print("== Case / whitespace / punctuation / encoding ==", flush=True)
    probe("case-upper", "format", {"location": "LONDON"})
    probe("case-lower", "format", {"location": "london"})
    probe("ws-padded", "format", {"location": "  London  "})
    probe("qual-nospace", "format", {"location": "London,UK"})
    probe("qual-space", "format", {"location": "London, UK"})
    probe("qual-disambig", "format", {"location": "London, Ontario"})
    # httpx serialises list values as repeated query params: ?location=London&location=Tokyo
    probe("repeated-param", "format", {"location": ["London", "Tokyo"]})

    print("== Adversarial inputs ==", flush=True)
    probe("adv-empty", "adversarial", {"location": ""})
    probe("adv-no-param", "adversarial", None)
    probe("adv-wrong-paramname", "adversarial", {"city": "London"})
    probe("adv-long-1000", "adversarial", {"location": "a" * 1000})
    probe("adv-unicode-jp", "adversarial", {"location": "東京"})
    probe("adv-unicode-ru", "adversarial", {"location": "Москва"})
    probe("adv-unicode-pt", "adversarial", {"location": "São Paulo"})
    probe("adv-emoji", "adversarial", {"location": "🗼"})
    probe("adv-numeric", "adversarial", {"location": "12345"})
    probe("adv-coords", "adversarial", {"location": "51.5074,-0.1278"})
    probe("adv-sql", "adversarial", {"location": "London'; DROP TABLE cities;--"})
    probe("adv-xss", "adversarial", {"location": "<script>alert(1)</script>"})
    probe("adv-atlantis", "adversarial", {"location": "Atlantis"})
    probe("adv-hogwarts", "adversarial", {"location": "Hogwarts"})
    probe("adv-gibberish", "adversarial", {"location": "Asdfjklqwerty"})
    probe("adv-postcode", "adversarial", {"location": "SW1A 1AA"})
    probe("adv-airport", "adversarial", {"location": "LHR"})
    probe("adv-country", "adversarial", {"location": "France"})
    probe("adv-continent", "adversarial", {"location": "Europe"})
    probe("adv-comma-cities", "adversarial", {"location": "London,Tokyo"})
    probe("adv-newline", "adversarial", {"location": "London\nTokyo"})

    print("== Auth / method ==", flush=True)
    probe("auth-no-key", "auth", {"location": "London"}, auth="missing")
    probe("auth-wrong-key", "auth", {"location": "London"}, auth="wrong")
    probe("method-post", "method", {"location": "London"}, method="POST")

    print("== Determinism sample #2 (compared later) ==", flush=True)
    probe("determ-london-2", "determinism", {"location": "London"})

    print("== Rate-limit burst (intentional) ==", flush=True)
    # Start from a fresh window so the burst itself is what trips the limiter.
    force_batch_break()
    for i in range(6):
        burst.append(probe(f"burst-{i+1}", "ratelimit", {"location": "London"}, pace=False))

    print(f"  ... sleeping {BURST_RECOVERY_SLEEP}s for rate-limit recovery ...", flush=True)
    time.sleep(BURST_RECOVERY_SLEEP)
    in_batch = 0
    recovery = probe("burst-recovery", "ratelimit-recovery", {"location": "London"})

    print("== Determinism sample #3 (final) ==", flush=True)
    probe("determ-london-3", "determinism", {"location": "London"})

    return burst, recovery


def write_results(burst: list[dict] | None, recovery: dict | None) -> None:
    by_status = Counter(
        str(p["status"]) if p["status"] is not None else "ERR" for p in results
    )

    london_samples = [
        p for p in results
        if p["name"] in {"happy-london", "determ-london-2", "determ-london-3"}
        and p["status"] == 200
    ]
    if len(london_samples) >= 2:
        bodies = [json.dumps(p["response_body"], sort_keys=True, ensure_ascii=False)
                  for p in london_samples]
        london_identical = all(b == bodies[0] for b in bodies)
    else:
        london_identical = None

    burst_summary: dict | None = None
    if burst:
        first_429 = next(
            (i for i, r in enumerate(burst) if r["status"] == 429), None
        )
        retry_after = None
        rate_limit_headers: dict[str, str] = {}
        if first_429 is not None:
            hdrs = burst[first_429]["response_headers"] or {}
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
            "retry_after_header": retry_after,
            "rate_limit_headers": rate_limit_headers,
            "recovery_confirmed": (recovery is not None and recovery["status"] == 200),
            "recovery_elapsed_ms": recovery["elapsed_ms"] if recovery else None,
        }

    summary = {
        "total": len(results),
        "by_status": dict(by_status),
        "client_errors": sum(1 for p in results if p["error"]),
        "london_responses_identical": london_identical,
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
    print(f"London identical across run: {summary['london_responses_identical']}", flush=True)
    if burst_summary:
        print(
            f"Rate-limit burst: first 429 at index "
            f"{burst_summary['first_429_at_request_index']}, "
            f"retry-after={burst_summary['retry_after_header']}, "
            f"recovery_confirmed={burst_summary['recovery_confirmed']}",
            flush=True,
        )
    print(f"Wrote {RESULTS_PATH}", flush=True)


def main() -> None:
    burst: list[dict] = []
    recovery: dict | None = None
    try:
        burst, recovery = run()
    except KeyboardInterrupt:
        print("\n[interrupted — writing partial results]", flush=True)
    except Exception as e:
        print(f"\n[fatal: {type(e).__name__}: {e} — writing partial results]", flush=True)
    finally:
        try:
            write_results(burst, recovery)
        finally:
            client.close()


if __name__ == "__main__":
    main()
