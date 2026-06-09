# System Design Discussion — Script & Structure (Part 3, 45 min)

> **What they're evaluating:** systems thinking, operational awareness, trade-off reasoning.
> **What this is NOT:** a whiteboard test. They said so explicitly. They want to see *how you think*.

---

## 0. The Universal Framework (use this for every question)

Before diving into any answer, run through this mental checklist. It takes 20 seconds and signals you think in systems:

```
1. CLARIFY    — make sure you understand the constraint/goal
2. BASELINE   — describe what exists today (anchor in your actual code)
3. BOTTLENECK — what breaks first and why
4. EVOLVE     — what architectural change solves it
5. TRADE-OFFS — what you give up and why it's acceptable
```

This is the same structure hellointerview and top engineers use. When in doubt, say the step aloud:
*"Let me start with the current state of the system..."* — this buys you thinking time and sounds structured.

---

## 1. Opening (first ~2 min)

Set the frame before any question lands. Shows you came prepared:

> *"I've been thinking about this in terms of the actual code we built — it's a single Python program
> that spends almost all its time waiting on network calls: Deepgram for speech-to-text, Claude for the LLM,
> ElevenLabs for audio. That's an important starting point for any scale question — a program that's mostly
> waiting can handle far more concurrent users than one doing heavy computation.
> I'm happy to go wherever you want to take it."*

This tells them:
- You understand your own system
- You'll reason from specifics, not generic buzzwords
- You're ready to be guided

---

## 2. Q1: "How would you handle 100 concurrent voice calls?"

### Step 1 — Baseline: what exists today

Draw this or describe it:

```
 ┌────────────┐  PCM audio    ┌──────────────────────┐
 │ Microphone │──────────────►│  Deepgram WebSocket  │
 └────────────┘               │  (1 WS per session)  │
                              └──────────┬───────────┘
                                         │ transcript
                              ┌──────────▼───────────┐
                              │       main.py        │
                              │  asyncio event loop  │
                              │  conversation_history│  ← local list, single user
                              └──────────┬───────────┘
                                         │ messages + tools
                              ┌──────────▼───────────┐
                              │   Anthropic Claude   │
                              │   stream() call      │
                              └──────────┬───────────┘
                                         │ tool_use?
                              ┌──────────▼───────────┐
                              │  asyncio.gather()    │
                              │  weather + research  │
                              └──────────┬───────────┘
                                         │ text response
                              ┌──────────▼───────────┐
                              │  ElevenLabs TTS      │
                              │  stream to speaker   │
                              └──────────────────────┘
```

> *"Right now this is a CLI — one user, one asyncio event loop. Each turn is: Deepgram WebSocket
> for STT, Anthropic streaming for the LLM, optional tool calls via httpx, then ElevenLabs for TTS.
> Crucially, every step is pure I/O — no CPU-bound work at all."*

### Step 3 — Why that matters for 100 calls

> *"Because it's all just waiting on network calls, one Python program can handle many sessions at the
> same time — while one session waits for Claude to respond, another can be sending audio to Deepgram.
> This is the opposite of something like video encoding, where you're doing heavy computation and
> genuinely need more machines to do more work in parallel."*

### Step 4 — The architecture move: CLI → WebSocket API server

Draw this:

```
  Client A ──WS──┐
  Client B ──WS──┤
  Client C ──WS──┼────► FastAPI + uvicorn (single async process)
     ...         │
  Client N ──WS──┘         sessions: { session_id → conversation_history }
                            │
              ┌─────────────┼─────────────────┐
              ▼             ▼                  ▼
         Deepgram      Anthropic         ElevenLabs
         1 WS per      shared            shared
         session       client            client
```

> *"The main architectural move is: wrap the CLI logic in a FastAPI WebSocket server.
> Each call gets a session ID, and the conversation history moves from a local variable
> into a dict keyed by session ID. Audio I/O shifts: instead of pyaudio reading from the mic,
> the client sends raw PCM over the WebSocket, we forward it to Deepgram, and we pipe TTS
> audio chunks back the same way."*

### Step 5 — Specific code changes needed

> *"Three concrete things change:*
> *1. `conversation_history` moves from a stack variable to `sessions[session_id]`*
> *2. Audio in/out routes through the WebSocket instead of pyaudio/ffplay*
> *3. Deepgram connection lifecycle: open on session start, close on end or 30s silence"*

### Step 6 — Trade-off: vertical vs horizontal scaling

> *"One big async server handles 100 concurrent calls fine. At 1,000+ you'd scale
> horizontally behind a load balancer — but now you need sticky sessions, because
> conversation history has to follow the client to the right node. That's when you
> push history to Redis with a TTL. It adds ~1-2ms per turn for the history read/write,
> totally acceptable for a voice app.*
>
> *One thing that already helps us here: the context compression we built bounds memory
> per session to ~512-token summaries for old turns. Without it, 100 sessions × 200 turns
> would be unbounded RAM growth."*

---

## 3. Q2: "What breaks first? How would you monitor it?"

### The bottleneck order — say this clearly and in order

> *"I've thought through this specifically for our stack. The order is:"*

```
Bottleneck #1: Anthropic rate limits
  ↓ 100 sessions × ~1,000 tokens/turn × ~6 turns/min = ~600k tokens/min
    Standard tier is far below that.
  Signal: 529 overload errors, or LLM latency spiking before hard failure

Bottleneck #2: ElevenLabs concurrent stream limits
  ↓ Most plans cap simultaneous TTS streams
  Signal: HTTP 429 on TTS requests

Bottleneck #3: Deepgram WebSocket connection limits
  ↓ 1 WS per active session = 100 open connections under the same API key
  Signal: connection refused or auth errors on WS open

Bottleneck #4: Memory (conversation histories)
  ↓ Without compression: 100 sessions × 200 turns × ~500 tokens ≈ 100MB+ in Python lists
  Signal: process RSS climbing unbounded — already mitigated by our context compression

Bottleneck #5: The Elyos mock APIs
  ↓ Rate limit envelope triggers at ~3 req/32s — unusable at any real scale
  Signal: tool results returning {"status": "throttled"}
```

> *"Almost certainly Anthropic hits the wall first. The signal is elevated TTFT latency —
> the API slows before it hard-rejects with 529s. The rate limit manager we have handles
> single-provider throttling; at scale you'd add backpressure that queues new requests
> rather than failing them outright."*

### How to monitor it

> *"The most useful single number: end-to-end turn latency — utterance end to first audio byte.
> That integrates every bottleneck into one user-visible signal.*
>
> *What I'd instrument:*
> *- Structured JSON logs per turn: session_id, turn_id, per-stage timing*
> *- OpenTelemetry spans around each API call — gives p50/p95/p99 per provider*
> *- A counter every time the rate limit manager fires — spike tells you which provider is bottlenecked*
> *- Active session gauge — how many conversations are in-flight right now*
>
> *Alerts I'd set: Anthropic p95 TTFT > 3s, ElevenLabs 429 rate > 1%, process RSS above threshold."*

---

## 4. Q3: "Walk me through a request from mic to speaker — where's the latency?"

This is the one to draw on the board. Put this table up:

```
Phase                      │ Latency      │ Where in our code
───────────────────────────┼──────────────┼─────────────────────────────────
1. Silence detection (VAD) │ ~1,000ms     │ utterance_end_ms=1000 in voice.py
2. Deepgram STT (final)    │ ~200–400ms   │ WebSocket transcript in voice.py
3. Anthropic TTFT          │ ~300–600ms   │ client.messages.stream() in main.py
───────────────────────────┼──────────────┼
   PATH A: simple response │ streaming    │ text_stream in call_llm
───────────────────────────┼──────────────┼
   PATH B: tool call       │              │
   • LLM emits tool_use    │ ~200ms       │ get_final_message()
   • weather               │ ~200ms       │ asyncio.gather()
   • research              │ 3–8 seconds  │ ← dominates everything
   • continuation LLM      │ +300–600ms   │ second stream() pass
───────────────────────────┼──────────────┼
4. ElevenLabs TTS TTFT     │ ~300–500ms   │ speak() in voice.py (turbo_v2_5)
5. Audio starts playing    │ overlaps #4  │ asyncio.to_thread(stream, ...)
```

> *"Simple no-tool turn: about 2 seconds perceived. 1s silence buffer, 300ms STT, 500ms LLM, 400ms TTS.*
> *With a research tool call: 8–12 seconds total — the 3–8s research completely dominates everything else."*

### The optimization opportunities (raise these proactively — shows system thinking)

**1. Silence detection — biggest single lever:**
> *"Tuning utterance_end_ms from 1000ms to 300ms saves ~700ms on every turn.
> The risk is false endings mid-sentence. You'd mitigate with adaptive VAD or offer a push-to-talk mode."*

**2. Sentence-level TTS pipelining — highest impact for voice:**
> *"Right now we wait for the full LLM response before calling ElevenLabs. Instead, buffer until
> the first sentence boundary — `.`, `!`, `?` — then stream that chunk to TTS while Claude continues.
> Audio starts playing ~500ms after the first token instead of after the full response.
> This is the single highest-impact optimization for voice latency."*

**3. Research tool caching:**
> *"Same topic within 5 minutes returns a cached result — eliminates the 3-8s blocking call for
> repeated queries. Low implementation cost, high user impact."*

---

## 5. Trade-offs to raise proactively

Drop one of these naturally per question if they don't ask — it signals you think beyond happy paths:

| Topic | What to say |
|-------|-------------|
| Stateless LLM calls | *"Anthropic doesn't maintain session state — we send the full history every turn. Simple, but cost and latency grow with conversation length. Context compression bounds both. Alternative would be fine-tuned model with session state, but that's overkill here."* |
| Silence detection window | *"1,000ms is conservative — faster VAD cuts latency but risks false-positive utterance endings mid-sentence."* |
| In-process state vs Redis | *"Fine for one server. One-line switch to Redis when you need horizontal scale — if you design the session lookup as a function from day one, the migration is trivial."* |
| asyncio.gather for tools | *"Correct here because weather and research are read-only. If tools had side effects that could conflict, you'd need ordering guarantees."* |

---

## 6. If they ask "what would you change with more time?"

Three good answers, each showing a different dimension:

**Operational (shows you think about production):**
> *"I'd add a proper health check endpoint that probes each provider with a lightweight request —
> not just 'is the process alive' but 'can we actually reach Deepgram, Anthropic, and ElevenLabs
> right now.' That's what matters for on-call."*

**Latency (shows you know the bottleneck):**
> *"Sentence-level TTS pipelining. It's the single highest-leverage latency optimization for
> a voice app — start streaming to ElevenLabs as soon as we hit the first sentence boundary,
> not after the full response."*

**Resilience (shows you think about failure modes):**
> *"Graceful degradation when ElevenLabs is down — fall back to text output rather than failing
> the whole turn. The user loses audio but the conversation continues."*

---

## 7. If you don't know something — what to say

Never bluff. These phrasings work:

> *"I haven't worked with X at scale, but reasoning from first principles I'd expect [Y].
> I'd want to look at [specific thing] to validate that."*

> *"I'm not sure of the exact limits off the top of my head, but the failure mode I'd watch for
> is [signal], and I'd mitigate with [approach]."*

They're evaluating your reasoning process, not your recall of API tier limits.

---

## 8. Timing guide (45 min)

| Time | What's happening |
|------|-----------------|
| 0–2 min | You open with the framing (Section 1 above) |
| 2–15 min | Q1 (100 concurrent calls) — baseline → architecture → code changes → scale trade-off |
| 15–28 min | Q2 (what breaks / monitoring) — bottleneck order → instrumentation → alerts |
| 28–40 min | Q3 (mic to speaker latency) — draw the table → optimizations |
| 40–45 min | Free-form follow-ups, "what else would you add?" — use Section 6 |

---

## 9. Quick-reference cheat sheet

```
KEY NUMBERS TO REMEMBER:
• Simple turn latency: ~2 seconds (1s VAD + 300ms STT + 500ms LLM + 400ms TTS)
• With research: ~8–12s (research dominates)
• Redis overhead: ~1-2ms per turn
• Context compression: ~512 token summaries for old turns
• Weather API: ~200ms  |  Research API: 3–8s
• Anthropic: breaks first at ~100 sessions (tokens/min limit)

ARCHITECTURE IN ONE SENTENCE:
"CLI → FastAPI WebSocket server, sessions dict for state,
 same async event loop handles 100+ I/O-bound sessions,
 Redis when you need to go multi-server."

LATENCY WIN ORDER:
1. VAD threshold (1000ms → 300ms) — saves ~700ms every turn
2. Sentence-level TTS pipeline — audio starts 500ms after TTFT
3. Research caching — eliminates 3-8s blocking for repeat topics
```
