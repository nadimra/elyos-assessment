import argparse
import asyncio
import os
import time
import signal
import sys
import json
import random

import httpx
from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.spinner import Spinner
from anthropic import AsyncAnthropic
from dotenv import load_dotenv

load_dotenv()

MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 1024
console = Console()

client = AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

ELYOS_BASE = "https://elyos-interview-907656039105.europe-west2.run.app"
ELYOS_API_KEY = os.environ["ELYOS_API_KEY"]

TOOLS: list[dict] = []
TOOL_FUNCS: dict = {}
TOOL_STATUS: dict = {}

def tool(*, name, description, input_schema, status):
    def decorator(func):
        TOOLS.append({
            "name": name,
            "description": description,
            "input_schema": input_schema,
        })
        TOOL_FUNCS[name] = func
        TOOL_STATUS[name] = status
        return func
    return decorator

async def get_user_input() -> str:
    """Get input from user."""
    print("\n\033[1;36mYou\033[0m › ", end="", flush=True)

    loop = asyncio.get_event_loop()
    future: asyncio.Future[str] = loop.create_future()

    def _on_readable():
        loop.remove_reader(sys.stdin.fileno())
        if not future.done():
            future.set_result(sys.stdin.readline().strip())

    # add_reader avoids the thread-based race that breaks Ctrl+C cleanup.
    loop.add_reader(sys.stdin.fileno(), _on_readable)
    try:
        return await future
    except (asyncio.CancelledError, KeyboardInterrupt):
        loop.remove_reader(sys.stdin.fileno())
        raise

async def call_llm(user_input: str, conversation_history: list):
    """Send input to LLM, yield streaming text deltas, update history."""
    conversation_history.append({"role": "user", "content": user_input})

    while True:
        async with client.messages.stream(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            tools=TOOLS,
            messages=conversation_history,
        ) as stream:
            async for text in stream.text_stream:
                yield text
            final = await stream.get_final_message()

        if final.stop_reason != "tool_use":
            conversation_history.append({"role": "assistant", "content": final.content})
            return
        
        tool_blocks = [b for b in final.content if b.type == "tool_use"]
        tool_results = []

        for b in tool_blocks:
            yield ("status_add", b.id, TOOL_STATUS[b.name](b.input))

        results = await asyncio.gather(
            *[TOOL_FUNCS[b.name](**b.input) for b in tool_blocks]
        )

        for b, result in zip(tool_blocks, results):
            yield ("status_remove", b.id)
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": b.id,
                "content": json.dumps(result),
                "is_error": isinstance(result, dict) and "error" in result,
            })
        conversation_history.append({"role": "assistant", "content": final.content})
        conversation_history.append({"role": "user", "content": tool_results})


async def stream_response(user_input: str, conversation_history: list, voice: bool = False):
    chunks: list[str] = []
    statuses: dict[str, str] = {}
    try:
        with Live(console=console, refresh_per_second=15) as live:
            def render():
                parts = []
                text = "".join(chunks)
                if text:
                    parts.append(Markdown(text))
                for msg in statuses.values():
                    parts.append(Spinner("dots", text=msg, style="yellow"))
                return Group(*parts)

            async for event in call_llm(user_input, conversation_history):
                if isinstance(event, str):
                    chunks.append(event)
                elif event[0] == "status_add":
                    statuses[event[1]] = event[2]
                elif event[0] == "status_remove":
                    statuses.pop(event[1], None)
                live.update(render())
        print()
        if voice and chunks:
            from voice import speak
            await speak("".join(chunks))
    except asyncio.CancelledError:
        # Preserve the cancelled turn so follow-ups have context.
        # call_llm always commits user before streaming and assistant+tool_results
        # atomically, so on cancel the last entry is always 'user'. Append either
        # the partial text or a placeholder as the assistant response.
        if conversation_history and conversation_history[-1]["role"] == "user":
            partial = "".join(chunks).strip()
            content = (
                partial + " [cancelled]" if partial
                else "[cancelled before responding]"
            )
            conversation_history.append({"role": "assistant", "content": content})
        raise

@tool(
    name="get_weather",
    description=(
        "Get current weather for a city. "
        "Expects a city name, optionally with a country or region qualifier "
        "(e.g. 'London', 'London, UK', 'Springfield, IL'). "
        "The underlying geocoder is fuzzy and will silently return the wrong "
        "place for coordinates, ZIP/postal codes, ISO country codes (e.g. 'GB'), "
        "airport codes, or vague inputs — for any of those, ASK the user to "
        "clarify which city they mean instead of calling this tool. "
        "If the returned location field doesn't match what the user asked for, "
        "mention the ambiguity in your reply rather than presenting it as the "
        "answer. "
        "Responses sometimes contain a 'conditions' array with multiple "
        "readings instead of a single reading. When that happens, you MUST "
        "make the multiplicity explicit — e.g. 'Manchester (2 readings: "
        "9.1°C overcast, 8.1°C light rain)'. Never combine them with a "
        "slash like '9.1°C / 8.1°C', and never average or range them. The "
        "semantics of multi-reading responses aren't documented, so the "
        "user needs to see them as distinct data points."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "location": {
                "type": "string",
                "description": (
                    "City name, optionally with a country/region qualifier — "
                    "e.g. 'London', 'London, UK', 'Springfield, IL'. "
                    "Do not pass coordinates, ZIP codes, country codes, or "
                    "airport codes."
                ),
            },
        },
        "required": ["location"],
    },
    status=lambda args: f"Looking up weather in {args.get('location', '?')}...",
)
async def get_weather(location: str) -> dict:
    """Fetch weather from API (~200ms typical)."""
    return await call_with_retry(lambda: _get_weather_once(location))

async def _get_weather_once(location: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=10) as http:
            r = await http.get(
                f"{ELYOS_BASE}/weather",
                params={"location": location},
                headers={"X-API-Key": ELYOS_API_KEY},
            )
        body = r.json()
    except RETRYABLE_EXCEPTIONS:
        raise
    except Exception as e:
        return {"error": f"weather API failed: {type(e).__name__}"}
    # Throttle hits come back HTTP 200 with envelope — see docs/api-notes.md.
    if isinstance(body, dict) and body.get("status") == "throttled":
        return {
            "error": "rate limited",
            "retry_after_seconds": body.get("retry_after_seconds", 30),
        }
    return body

@tool(
    name="research_topic",
    description=(
        "Research a topic in depth. Takes 3-15 seconds. Use only for "
        "research-style asks, not for facts you already know well. "
        "Only the 'summary' field is research content; the rest is metadata. "
        "RESPOND IN THIS EXACT FORMAT: "
        "'Our dedicated research API returned this summary: \"<verbatim "
        "summary text>\".' Quote the summary verbatim — do not paraphrase, "
        "condense, or rewrite it. "
        "ONLY if the summary is genuinely insufficient for what the user "
        "asked (too brief, or missing the specific aspect they asked about), "
        "append a second paragraph starting EXACTLY with: 'Outside of the "
        "research API, I found this information: ' followed by your own "
        "knowledge. If the summary covers the question, do NOT add anything "
        "beyond the quoted summary. "
        "Never blend your own knowledge into the quoted summary or present "
        "it as if it came from the research. "
        "If 'cached': true, tell the user the info may be out of date. "
        "For ambiguous topics ('football', 'mercury'), ASK the user to "
        "clarify first — each call is slow and rate-limited."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "topic": {
                "type": "string",
                "description": "A specific, disambiguated topic to research.",
            },
        },
        "required": ["topic"],
    },
    status=lambda args: f"Researching {args.get('topic', '?')}... (CTRL+C to cancel)",
)
async def research_topic(topic: str) -> dict:
    """Research a topic. 3-15s observed — timeout set above the worst case."""
    return await call_with_retry(lambda: _research_topic_once(topic))

async def _research_topic_once(topic: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=30) as http:
            r = await http.get(
                f"{ELYOS_BASE}/research",
                params={"topic": topic},
                headers={"X-API-Key": ELYOS_API_KEY},
            )
        body = r.json()
    except RETRYABLE_EXCEPTIONS:
        raise
    except Exception as e:
        return {"error": f"research API failed: {type(e).__name__}"}
    # Shared throttle envelope with /weather.
    if isinstance(body, dict) and body.get("status") == "throttled":
        return {
            "error": "rate limited",
            "retry_after_seconds": body.get("retry_after_seconds", 30),
        }
    # Empty {} is a third variant — server returned nothing. Surface as error.
    if isinstance(body, dict) and not body:
        return {"error": "empty response from research API"}
    # Sources are fake-fixed; strip so the LLM doesn't quote them as citations.
    if isinstance(body, dict):
        body.pop("sources", None)
    return body

RETRYABLE_EXCEPTIONS = (
    httpx.ConnectError,
    httpx.ReadTimeout,
    httpx.RemoteProtocolError,
)

class RateLimitManager:
    def __init__(self):
        self._blocked_until = 0.0
        self._lock = asyncio.Lock()

    async def wait_if_needed(self):
        while True:
            delay = self._blocked_until - time.monotonic()
            if delay <= 0:
                return
            await asyncio.sleep(delay)

    async def throttle(self, retry_after: float):
        async with self._lock:
            self._blocked_until = max(
                self._blocked_until,
                time.monotonic() + retry_after,
            )

rate_limit = RateLimitManager()

async def call_with_retry(
    fn,
    *,
    max_attempts: int = 3,
    base_delay: float = 0.5,
    max_delay: float = 30.0,
):
    last_err = None
    for attempt in range(max_attempts):
        await rate_limit.wait_if_needed()
        try:
            result = await fn()
        except RETRYABLE_EXCEPTIONS as e:
            last_err = e
            if attempt < max_attempts - 1:
                await asyncio.sleep(backoff(attempt, base_delay, max_delay))
            continue

        if not (isinstance(result, dict) and result.get("error") == "rate limited"):
            return result

        last_err = "rate limited"
        if attempt < max_attempts - 1:
            await rate_limit.throttle(result.get("retry_after_seconds") or 0)

    return {"error": f"failed after {max_attempts} attempts: {last_err}"}
            
def backoff(attempt:int, base: float, cap: float):
    return min(cap, random.uniform(0,base* 2**attempt))

async def main():
    # asyncio.run's SIGINT handler counts cumulative Ctrl+Cs and raises
    # KeyboardInterrupt out of the loop on the 2nd one, exiting the program.
    # Install our own handler so only the in-flight task is cancelled.
    loop = asyncio.get_running_loop()
    active_task: asyncio.Task | None = None

    parser = argparse.ArgumentParser(description='Optional app description')
    parser.add_argument('--voice', action=argparse.BooleanOptionalAction)
    args = parser.parse_args()

    def on_sigint():
        if active_task and not active_task.done():
            active_task.cancel()

    loop.add_signal_handler(signal.SIGINT, on_sigint)

    # Mutable list — call_llm mutates in place so history persists across turns.
    conversation_history = []

    if args.voice:
        from voice import get_voice_input

    while True:
        if args.voice:
            active_task = asyncio.create_task(get_voice_input())
        else:
            active_task = asyncio.create_task(get_user_input())
        try:
            user_input = await active_task
        except asyncio.CancelledError:
            print()
            break
        if not user_input:
            continue
        if user_input.lower() in {'quit', 'exit', 'q'}:
            break

        print()
        if args.voice:
            active_task = asyncio.create_task(
                stream_response(user_input, conversation_history, voice=True)
            )
        else:
            active_task = asyncio.create_task(
                stream_response(user_input, conversation_history)
            ) 
        try:
            await active_task
        except asyncio.CancelledError:
            # Cancel just this response; stay in the prompt loop.
            print("\n\033[33mCancelled.\033[0m")

if __name__ == "__main__":
    asyncio.run(main())
