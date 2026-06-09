# On-Site Part 1 — Code Discussion Cheat Sheet

30-45 min walk-through of the take-home. They explicitly call out five prompts in `interview.md`:

1. Walk us through how you implemented cancellation.
2. What happens if the user cancels mid-stream while the LLM is responding?
3. Tell us more about [specific API quirk] — how did you discover it? Why did you handle it that way?
4. Were there any quirks you noticed but didn't have time to handle properly?
5. Let's discuss how you'd add retry logic with backoff.

They are evaluating **depth of understanding, investigative thinking, receptiveness to feedback** — so own the trade-offs, don't oversell.

---

## 0. 60-second tour of the code

Single file, ~300 LOC. `main.py`:

| Section | Lines | Role |
|---|---|---|
| `tool()` decorator | 30-40 | Registers tool name + Anthropic schema + status-line formatter in one place. |
| `get_user_input` | 42-60 | Uses `loop.add_reader` on stdin instead of `run_in_executor` — async-native, no thread to race with SIGINT during teardown. |
| `call_llm` | 62-105 | Async generator. Yields plain text strings for deltas, and 3-tuple/2-tuple events `("status", …)` / `("clear_status",)` to drive the spinner. Loops until `stop_reason != "tool_use"`. |
| `stream_response` | 108-145 | Rich `Live` renderer. Consumes the generator, keeps a `chunks: list[str]` buffer, and on `CancelledError` writes the partial back into history. |
| `get_weather`, `research_topic` | 147-263 | Tool implementations + their schemas. All the quirk handling lives here. |
| `main` | 265-304 | Installs a custom SIGINT handler that cancels the currently-active task only. |

---

## 1. Cancellation walkthrough

**The story in three lines:**

1. `main()` replaces asyncio's default SIGINT handler so Ctrl+C cancels only the in-flight task instead of tearing down the loop (`main.py:269-276`).
2. `active_task` is a single slot that points at whichever coroutine is "current" — `get_user_input` between turns, `stream_response` during a reply. SIGINT calls `.cancel()` on that task.
3. `CancelledError` propagates up the async generator → out of `stream_response` → caught by `main`, which prints `Cancelled.` and goes back to the prompt loop.

**Why a custom signal handler at all?**

`asyncio.run` installs its own SIGINT handler: first Ctrl+C cancels the main task, second one raises `KeyboardInterrupt` and kills the program. That's the wrong UX here — users should be able to cancel a research call and keep chatting. Replacing the handler keeps the loop alive and scopes cancellation to one task (`main.py:266-276`).

**Why one `active_task` slot instead of per-call try/except?**

The same Ctrl+C should work whether the user is mid-prompt or mid-reply. Pointing `active_task` at whatever coroutine is currently in flight is simpler than two handlers.

---

## 2. Mid-stream cancellation — what actually happens

This is the question they'll push on hardest. The interesting part is **conversation-history bookkeeping**, not the cancel mechanic.

**The invariant:** `call_llm` commits to history at exactly two points (`main.py:64`, `104-105`):

- The user turn is appended **before** the stream opens.
- The assistant turn + any tool-result user turn are appended **atomically after** the stream closes.

So if cancellation lands mid-stream, the last entry is **always** a user message with no assistant reply. That would leave the next turn malformed (Anthropic rejects two `user` messages in a row).

**The fix** (`main.py:133-145`): in `stream_response`'s `except CancelledError`, check the tail of history. If it's a `user` entry, append an assistant entry containing either the partial text + `[cancelled]` or `[cancelled before responding]`. Then re-raise so `main` still sees the cancel.

**The partial text matters for two reasons:**

- The model can reference what it was saying ("I was about to mention X — want me to continue?").
- The user gets a transcript of what they actually saw on screen, which matches the printed output.

**Edge case I do handle:** cancellation during a tool call. The tool call is `await`ed inside `call_llm`'s tool loop. If it's cancelled, the generator unwinds without appending the assistant block, so the same "last entry is user" rule holds.

**Edge case I do not handle:** if cancellation lands during the rate-limit `asyncio.sleep` (`main.py:93`), the in-flight tool's prior `tool_use` block has already been emitted by the model but the tool result has not been built. History stays consistent (still ends on `user`) but the assistant gets attributed `[cancelled]` rather than something more specific like "cancelled while waiting on rate limit". Cosmetic, but it's the kind of thing they might probe.

---

## 3. API quirks — discovery and handling

**How I discovered them:** `probes/probe_weather.py` and `probes/probe_research.py` — both ~300-line sweep scripts that hammer each endpoint with normal inputs, garbage, injections, repeats, concurrent bursts, and edge encodings, dumping raw JSON to `probe_*_results.json`. Then I diffed the variants by hand. Notes consolidated in `docs/api-notes.md`.

**Pick 2-3 to talk about in depth. Cheat sheet for each:**

### Quirk A — Rate limit hidden in a 200 body (both endpoints)

- **What I saw:** Burst of weather calls suddenly returned `{"status":"throttled","retry_after_seconds":29,"data":null}` — HTTP 200, not 429.
- **Why it matters:** Anything checking `response.status_code == 200` treats it as success and forwards `null` data to the LLM, which then confabulates.
- **How I handled it** (`main.py:198-203`, `252-256`): Inspect the body shape; return a structured `{"error": "rate limited", "retry_after_seconds": N}` from the tool. `call_llm` recognises this shape and retries after the server-suggested wait, up to twice (`main.py:88-96`), showing a status line so the user knows we're waiting.
- **Why structured-error-instead-of-raise:** the LLM is already the orchestration layer for these tools. Raising would lose the `retry_after_seconds` value and force me to invent a side channel to communicate it.

### Quirk B — Four random response shapes from `/research`, all at HTTP 200

- **What I saw:** Same `?topic=solar+energy` call returned (a) fresh research, (b) stale-cache variant with `"cached": true`, (c) `{}` empty body, (d) throttle envelope. No way to predict from the request.
- **Why it matters:** A naïve `body["summary"]` crashes on (c) and (d); forwarding (b) without telling the user is misleading.
- **How I handled it** (`main.py:252-263` + the tool description at `205-226`):
  - Empty `{}` → explicit `{"error": "empty response from research API"}`.
  - Throttle envelope → same as quirk A.
  - Cache variant → I pass `cached:true` through and instruct the LLM (in the tool description) to surface it to the user.
  - Sources field → stripped before forwarding (fake-fixed, would mislead as citations — see quirk C).

### Quirk C — `sources` is decorative / fake

- **What I saw:** Every `/research` response has the same three URLs regardless of topic.
- **How I handled it** (`main.py:262`): Strip `sources` before returning. Simpler than tool-description gymnastics — if the model never sees them, it can't quote them.

### Quirk D — Geocoder is wildly permissive

- **What I saw:** `?location=12345` → Schenectady, `?location=Europe` → a town literally called "Europe", `?location=LHR` → London Heathrow Airport, SQL/HTML injection strings get fuzzy-matched.
- **Why it matters:** Silent wrong answers — worst kind of bug, the user has no idea.
- **How I handled it** (`main.py:149-167`): Push the validation into the tool description. The LLM is instructed to refuse coords/ZIPs/codes and ask for disambiguation, and to flag when the returned `location` doesn't match what the user asked.
- **Trade-off to own:** I chose prompt-side handling over code-side input validation. Pro: cheap, flexible, the model can do disambiguation conversationally. Con: not deterministic — a different model or a prompt-injection user could bypass it. A production version should have both.

### Quirk E — Multi-reading weather response

- **What I saw:** Cambridge sometimes returns `conditions: [...]` with 2+ readings instead of a single flat object.
- **How I handled it** (`main.py:161-166`): Forced format in the tool description — model must surface multiplicity explicitly ("2 readings: 9.1°C overcast, 8.1°C light rain"), never average or slash-combine. No code-side reshape — keeps the contract with the LLM honest about the data it's seeing.

### Quirk F — Shared rate-limit budget across both endpoints

- **What I saw:** Alternating between `/weather` and `/research` still throttled.
- **Why it matters for design:** I can't isolate retry strategy per tool; budget is global.

---

## 4. Quirks I noticed but didn't fully handle (be honest)

Pick a couple — they're explicitly testing self-critique.

- **Geocoder validation is prompt-side only.** A bad/injected message can talk the model past the rule. A production version would also do pattern checks in `get_weather` itself before hitting the API.
- **Retry is dumb.** Two retries, fixed wait from the server's `retry_after_seconds + 1`. No jitter, no exponential backoff, no global circuit breaker. See section 5.
- **Three different `/weather` error body shapes are not separately surfaced.** I lump them under a single `{"error": "weather API failed: ..."}` via the `except Exception` catch. The LLM doesn't see the distinction between 401 (config), 404 (input), and 422 (validation). Not catastrophic — the model still apologises — but loses signal.
- **Error bodies echo input verbatim.** Documented as a potential XSS sink in `docs/api-notes.md`. I'm rendering with Rich's Markdown which auto-escapes for terminals, so no actual issue here — but a web frontend would have a problem.
- **No timeout differentiation for the rate-limit wait.** If the user sends Ctrl+C during `asyncio.sleep`, they get "Cancelled." with no indication that the cancellation interrupted a backoff, not the call itself.
- **Unicode handling is inconsistent on the API side** (`東京` works, `Київ` returns 404). I do nothing to normalise transliteration — relied on the LLM to retry with a different spelling, but didn't test that.
- **Empty `{}` from `/research` is sometimes a transient.** I treat it as a hard error and return — could conceivably retry once before giving up.

---

## 5. Retry logic with backoff — the design discussion

Frame this as **what the current code does, what's missing, and what a production version looks like.**

**What's there now** (`main.py:88-96`):

- Tool returns `{"error": "rate limited", "retry_after_seconds": N}`.
- Loop retries up to 2 times, sleeping `N + 1` seconds, showing a status spinner.
- Anything else (HTTP error, network error) is returned to the LLM as `{"error": ...}` and not retried.

**What's missing:**

| Gap | Why it matters | What I'd add |
|---|---|---|
| No backoff for transient network errors | `httpx.ConnectError`, 5xx, etc. are one-shot today | Distinguish retryable (`ConnectError`, `ReadTimeout`, 5xx) from non-retryable (4xx, schema errors). Retry only the former. |
| No jitter | Thundering herd if multiple clients all wake at the same `retry_after_seconds` | Add `random.uniform(0, jitter_max)` on top of base delay. |
| Linear/honoring-server-value only | Doesn't escalate if server lies or stays throttled | Exponential: `base * 2**attempt + jitter`, capped at e.g. 30s. Honor `Retry-After` as a *minimum*, not the exact value. |
| Per-tool retry policy | Today `/research` and `/weather` use the same loop, but their tolerable wait is very different (3-15s call vs. 200ms call) | Pass a `RetryPolicy(max_attempts, base, cap, jittered)` into the tool or into a shared `call_with_retry` helper. |
| No circuit breaker | If the API is hard-down, every turn pays the full timeout × attempts cost | After N consecutive failures, short-circuit with a synthetic error for a cooldown window. |
| Retry state is per-call | Shared rate limit budget across endpoints (quirk F) means weather burning the budget hurts research | A token-bucket gate in front of the HTTP client, shared across both tools. |
| Cancellation during sleep | `asyncio.sleep` is cancellable (good) but the user has no way to know they cancelled the *wait*, not the *call* | Surface in the cancellation message: "cancelled while waiting on rate limit". |
| Observability | I have no way to tell whether retries are saving turns or hiding bugs | Counters: attempts, retries-by-reason, eventual successes — even just stderr logging. |

**Sketch of a clean version (don't write this on the whiteboard unless they ask, but be ready):**

```python
async def call_with_retry(
    coro_factory,                # () -> awaitable
    *,
    max_attempts=3,
    base=0.5,
    cap=10.0,
    retryable=(httpx.ConnectError, httpx.ReadTimeout),
):
    for attempt in range(max_attempts):
        try:
            result = await coro_factory()
        except retryable:
            if attempt == max_attempts - 1: raise
        else:
            if not (isinstance(result, dict) and result.get("error") == "rate limited"):
                return result
            wait = max(base * 2**attempt, int(result.get("retry_after_seconds", 1)))
        wait = min(wait, cap) + random.uniform(0, base)
        await asyncio.sleep(wait)
    return result
```

**Trade-offs to volunteer:**

- I deliberately did **not** retry beyond rate-limits in the take-home. The "discover quirks and handle them gracefully" brief was more about surfacing structured errors than papering over them — retrying a 401 just delays the inevitable. A production version retries transients only.
- Putting the loop in `call_llm` (current) vs. inside each tool: current location keeps tool functions thin and lets the user see a status update during the wait. Cost: every tool re-implements its rate-limit error shape. A `call_with_retry` helper around each tool body would be cleaner.

---

## Likely curveballs and short answers

- **"Why a custom decorator instead of just a TOOLS list?"** Keeps schema, status formatter, and implementation co-located so adding a tool is one block, not three edits in three places.
- **"Why `loop.add_reader` instead of `await loop.run_in_executor(None, input)`?"** The executor approach spawns a thread blocked on `input()`. SIGINT can't unblock it; on shutdown the program hangs waiting for the thread. `add_reader` is purely event-loop driven and cancels cleanly.
- **"Why Anthropic and not OpenAI?"** Either was fine per the brief. Anthropic's streaming + tool API matched the structure of the assignment closely, and the `final.stop_reason == "tool_use"` loop is clean.
- **"Why no tests?"** Brief said "one or two is fine, not a full suite". The probes effectively serve as integration tests against the live API; for unit tests, the highest-value targets would be the response-shape branches in the tool functions (mock httpx, assert error envelope) and the history-after-cancel invariant in `stream_response`.
- **"What if two tool calls come back in one assistant turn?"** Today they run sequentially in the `for block in final.content` loop (`main.py:82-103`). For concurrency, `asyncio.gather(*[run(block) for block in ...])` — but the shared rate-limit budget (quirk F) means I'd want a semaphore or the token-bucket gate above.
- **"How would you add conversation memory beyond just appending?"** A few directions: summarise older turns once history exceeds N tokens, persist to sqlite per session, or use the model's own caching headers. The current code just appends; it works fine for the take-home but blows the context window in a long session.

---

## What to lead with

If they say "walk us through your code", open with **the cancellation story** (section 1) — it's the most distinctive thing and ties everything together (signal handler → active task → CancelledError → history bookkeeping). From there it's natural to pivot into quirks (section 3) because the same `{"error": ...}` pattern shows up in both.

If they say "tell us about a quirk", lead with the **hidden-200 rate limit** — it's the most counter-intuitive, has the cleanest fix, and naturally sets up the retry-logic conversation.
