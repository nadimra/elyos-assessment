# System Design Discussion Prep — Part 3 (45 min)

Grounded in the actual code: Deepgram STT → Anthropic Claude → ElevenLabs TTS,
single async Python process, all I/O-bound.

---

## Diagram 1 — Single turn request flow (current CLI)

```
 ┌────────────┐  PCM (16kHz mono)   ┌──────────────────────┐
 │ Microphone │────────────────────►│  Deepgram WebSocket  │
 └────────────┘                     │  nova-2, linear16    │
                                    └──────────┬───────────┘
                                               │ transcript (string)
                                    ┌──────────▼───────────┐
                                    │       main.py        │
                                    │   asyncio event loop  │
                                    │                      │
                                    │  conversation_history│◄── grows each turn;
                                    │  (list, in-process)  │    compressed when
                                    └──────────┬───────────┘    >80K tokens
                                               │ messages + tools schema
                                    ┌──────────▼───────────┐
                                    │   Anthropic Claude   │
                                    │  sonnet-4-6 stream() │
                                    └────────┬─────────────┘
                          ┌─────────────────┐│ stop_reason
                          │  text chunks    ││ = "tool_use"
                          │  (streaming)    │▼
                          │          ┌──────────────────────┐
                          │          │  asyncio.gather()    │
                          │          ├──────────────────────┤
                          │          │ get_weather()        │ ~200ms
                          │          │ httpx timeout=10s    │
                          │          ├──────────────────────┤
                          │          │ research_topic()     │ 3–8s
                          │          │ httpx timeout=30s    │
                          │          └──────────┬───────────┘
                          │                     │ tool results
                          │          ┌──────────▼───────────┐
                          │          │  Claude (continued)  │
                          │          │  stream() pass 2     │
                          │          └──────────┬───────────┘
                          └─────────────────────┘
                                     │ complete text
                          ┌──────────▼───────────┐
                          │   Rich Live panel    │ (display always)
                          └──────────┬───────────┘
                                     │ if --voice
                          ┌──────────▼───────────┐
                          │  ElevenLabs TTS       │
                          │  turbo_v2_5 stream()  │ ~300–500ms TTFT
                          └──────────┬───────────┘
                                     │ PCM audio (streamed)
                          ┌──────────▼───────────┐
                          │      Speaker         │
                          └──────────────────────┘
```

---

## Diagram 2 — Scaled server architecture (100 concurrent calls)

```
  Client A ──WS──┐
  Client B ──WS──┤     ┌──────────────────────────────────────────────┐
  Client C ──WS──┼────►│           FastAPI + uvicorn                  │
     ...         │     │         (single async process)               │
  Client N ──WS──┘     │                                              │
                        │  sessions: dict[session_id → history]       │
                        │  ┌────────────────────────────────────────┐ │
                        │  │          asyncio event loop            │ │
                        │  │  100 concurrent sessions, all I/O-     │ │
                        │  │  bound — GIL is not a bottleneck       │ │
                        │  └────────────────────────────────────────┘ │
                        └───────────┬───────────┬──────────┬──────────┘
                                    │           │          │
                         ┌──────────▼──┐  ┌─────▼───┐  ┌──▼──────────┐
                         │  Deepgram   │  │Anthropic│  │ ElevenLabs  │
                         │  1 WS per   │  │ Claude  │  │ TTS stream  │
                         │  session    │  │ shared  │  │ shared      │
                         │  ⚠ breaks  │  │ client  │  │ client      │
                         │  here ~#3  │  │ ⚠ breaks│  │ ⚠ breaks   │
                         │             │  │ here #1 │  │ here #2    │
                         └─────────────┘  └─────────┘  └────────────┘

  Bottleneck order: (1) Anthropic RPM/TPM → (2) ElevenLabs streams → (3) Deepgram WS connections
```

---

## Diagram 3 — Horizontal scaling (1,000+ calls)

```
                         ┌─────────────────────┐
   Clients               │    Load Balancer     │
   ──────────────────►   │  (sticky sessions)   │  ← session affinity required:
                         └──────┬────────┬──────┘    history must follow the client
                                │        │
                     ┌──────────▼──┐  ┌──▼──────────┐
                     │  Server 1   │  │  Server 2   │   ... Server N
                     │  FastAPI    │  │  FastAPI    │
                     │  uvicorn    │  │  uvicorn    │
                     └──────┬──────┘  └──────┬──────┘
                            │                │
                     ┌──────▼────────────────▼──────┐
                     │             Redis             │
                     │  session_id → history (TTL)  │  +1–2ms per turn vs in-process
                     │  rate_limit state (shared)   │  needed once you have >1 server
                     └───────────────────────────────┘
```

---

## Q1: "How would you handle 100 concurrent voice calls?"

### Lead with the current shape
Right now the app is a single-process CLI — one conversation, one user. Every "call" is:
- 1 Deepgram WebSocket (mic → text)
- 1 Anthropic streaming request (text → tokens)
- N tool calls via httpx (parallel with asyncio.gather)
- 1 ElevenLabs TTS stream (text → audio)

All of this is pure I/O — no CPU-bound work. That's important because it means a
single Python asyncio event loop can multiplex many concurrent sessions cheaply.

### The architecture move
**CLI → stateful API server.** Each call becomes an async session tracked by session ID.

```
Client (browser/native app)
  └─ WebSocket (audio in / audio out)
        └─ FastAPI + uvicorn (async, one process handles 100+ sessions)
              ├─ Session store: { session_id → conversation_history }
              │   (in-process dict for 100 calls; Redis if horizontally scaled)
              ├─ Deepgram WS: one per active session
              ├─ Anthropic client: shared singleton (already the pattern in main.py)
              └─ ElevenLabs client: shared singleton (already in voice.py)
```

A single uvicorn worker with asyncio can comfortably hold 100 concurrent sessions
because every session is waiting on network I/O at any given moment. Python's GIL
is irrelevant here.

### What needs to change in the code
1. **Conversation state off the stack** — `conversation_history` is currently a local
   variable in `main()`. At 100 sessions it becomes a dict keyed by session ID.
   At higher scale (sessions surviving restarts, multiple servers), you externalize
   it to Redis with a TTL.

2. **Audio routing** — pyaudio/ffplay works locally. For a server you'd receive raw
   PCM over the WebSocket and forward it to Deepgram, then pipe TTS chunks back.

3. **Deepgram connection lifecycle** — currently one long-lived WS per call, which
   is the right model. Just need to manage the pool: open on session start, close
   on session end or 30s of silence.

4. **Context compression already helps here** — the `maybe_compress_history()` we
   added bounds memory per session to ~512 token summaries for old turns. At 100
   sessions that's the difference between unbounded RAM growth and a stable footprint.

### Trade-off to mention
*Vertical vs horizontal scaling.* One big async server handles 100 concurrent calls
fine. At 1,000+ you'd scale horizontally behind a load balancer, but now session
affinity matters (conversation history must live on the right node, or in Redis).
The Redis option costs ~1-2ms per history read/write — totally acceptable.

---

## Q2: "What breaks first? How would you monitor it?"

### What breaks, in order

**1. Anthropic rate limits** — almost certainly first.
Claude Sonnet has tokens-per-minute and requests-per-minute limits. At 100 concurrent
sessions, each generating ~1,000 output tokens, with a turn every ~10s, you're at
~10,000 tokens/second = 600,000 tokens/minute. The standard tier limit is far lower.
**Signal**: 529 overload errors or elevated TTFT latency (the API slows before it
hard-rejects). The `RateLimitManager` in main.py handles single-provider throttling;
at scale you'd add backpressure that queues new requests rather than failing them.

**2. ElevenLabs concurrent stream limits** — plan-dependent. Most tiers cap simultaneous
streams. **Signal**: HTTP 429 on TTS requests. Mitigation: queue TTS requests, or
allow text-only fallback mode.

**3. Deepgram WebSocket connections** — connection count limits per API key.
**Signal**: connection refused or auth errors on WS open.

**4. Memory** — conversation histories. Without context compression, 100 sessions
× 200 turns × ~500 tokens each = ~100MB just in Python lists (rough order of magnitude).
With compression it stays bounded. **Signal**: process RSS climbing unbounded.

**5. The ELYOS mock APIs** — not designed for scale. The rate limit envelope
(`{"status": "throttled"}`) triggers at ~3 req/32s (from probe_weather.py observations).
At 100 sessions calling weather simultaneously you'd be throttled immediately.
**Signal**: tool results start returning rate limit errors.

### How to monitor it

What you instrument in the code:
- **Structured JSON logs** with `session_id`, `turn_id`, and per-stage timing.
  Wrapping `call_llm` with `t_start = time.monotonic()` and logging on exit gives
  per-turn latency that you can aggregate.
- **Per-API call spans** — wrap each Deepgram/Anthropic/ElevenLabs call in an
  OpenTelemetry span. This gives you p50/p95/p99 latency per provider.
- **Rate limit counters** — every time `RateLimitManager.throttle()` fires, emit a
  metric. A spike tells you which provider is the current bottleneck.
- **Active session gauge** — how many concurrent conversations are in-flight.

What to alert on:
- Anthropic p95 TTFT > 3s (indicates rate limiting or overload)
- ElevenLabs 429 rate > 1%
- Rate limit throttle events per minute for any provider
- Process RSS above threshold (history leak)

The most useful single number to watch: **end-to-end turn latency** (utterance end →
first audio byte). That integrates all the bottlenecks into one user-visible signal.

---

## Q3: "Walk me through a request from mic input to speaker output — where's the latency?"

```
Phase                     │ Latency        │ Code location
──────────────────────────┼────────────────┼──────────────────────────────────
1. Silence detection (VAD)│ ~1,000ms       │ utterance_end_ms=1000 in voice.py
2. Deepgram STT (final)   │ ~200–400ms     │ WebSocket msg in voice.py
3. Anthropic TTFT         │ ~300–600ms     │ client.messages.stream() in main.py
   (or compress if needed)│ (+500ms)       │ maybe_compress_history() in main.py
4a. Simple response       │ streaming      │ text_stream in call_llm
──────────────────────────┼────────────────┼
4b. Tool call branch      │                │
    • LLM emits tool_use  │ ~200ms         │ get_final_message() in call_llm
    • Tool execution      │                │ asyncio.gather() in call_llm
      – weather           │ ~200ms         │ _get_weather_once()
      – research          │ 3–8s           │ _research_topic_once()
    • Continuation LLM    │ +300–600ms     │ second stream() pass
──────────────────────────┼────────────────┼
5. ElevenLabs TTS TTFT    │ ~300–500ms     │ speak() in voice.py (turbo_v2_5)
6. Audio playback starts  │ overlaps #5    │ asyncio.to_thread(stream, ...)
```

**Total — simple turn (no tools):** ~2–3s perceived
→ 1s silence buffer + 300ms STT + 500ms LLM TTFT + 400ms TTS TTFT

**Total — with research:** ~8–12s
→ The 3–8s research call completely dominates.

### Where the biggest wins are

**1. Silence detection (1,000ms)** — biggest single lever. Tuning
`utterance_end_ms` to 300–500ms saves 500–700ms on every turn. Risk: false
positives on mid-sentence pauses. Solution: adaptive VAD or a push-to-talk mode.

**2. Sentence-level TTS pipeline** — currently we wait for the entire LLM response
before calling ElevenLabs. Instead, buffer until you hit the first sentence
boundary (`.`, `!`, `?`), then stream that to TTS while the LLM continues. This
means audio starts playing ~500ms after TTFT rather than after the full response.
This is the most impactful latency optimization for voice.

**3. Tool call latency** — research at 3–8s is user-visible. You handle it correctly
already (pending spinner, cancellation). At scale you'd add caching (same topic
within 5 min returns cached result) and a timeout that returns a "still researching"
partial response rather than blocking audio.

**4. Anthropic streaming** — the current code streams text in real-time to the
display, but TTS only starts after `stream_response` completes. The sentence-level
pipeline described above fixes this.

### The number to remember
**~2 seconds** for a simple no-tool turn from utterance end to first audio.
The 1s VAD silence buffer accounts for roughly half of that.

---

## Key trade-offs to raise proactively

These signal "I think about systems, not just happy paths":

- **Stateless vs stateful LLM calls**: Anthropic doesn't maintain session state —
  you send the full history every time. This is simple but means cost and latency
  grow with conversation length. Context compression bounds both. Alternative:
  fine-tune a model with session state, but that's overkill here.

- **`utterance_end_ms` 1000ms vs 300ms**: Accuracy vs latency. Shorter window
  gives faster response but more false utterance endings mid-sentence.

- **asyncio.gather for tools**: Correct for concurrent HTTP calls. If tools had
  side effects that could conflict, you'd need ordering guarantees — not an issue
  here since weather and research are read-only.

- **In-process session state vs Redis**: Simple at 1 server, required at N servers.
  The switch is a one-line change in how `conversation_history` is stored/retrieved
  if you design the session lookup as a function from day one.
