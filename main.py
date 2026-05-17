import asyncio
import os
import signal
import sys

from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from anthropic import AsyncAnthropic
from dotenv import load_dotenv

load_dotenv()

MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 1024
console = Console()

client = AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

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
            messages=conversation_history,
        ) as stream:
            async for text in stream.text_stream:
                yield text
            final = await stream.get_final_message()

        conversation_history.append({"role": "assistant", "content": final.content})
        return

async def stream_response(user_input: str, conversation_history: list):
    chunks: list[str] = []
    try:
        # Live re-renders the chunks list as markdown
        with Live(console=console, refresh_per_second=15) as live:
            def render():
                parts = []
                text = "".join(chunks)
                if text:
                    parts.append(Markdown(text))
                return Group(*parts)

            async for event in call_llm(user_input, conversation_history):
                if isinstance(event, str):
                    chunks.append(event)
                live.update(render())
        print()
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

async def get_weather(location: str) -> dict:
    """Fetch weather from API (~200ms)."""
    pass


async def research_topic(topic: str) -> dict:
    """Research a topic (3-8 seconds). Should be cancellable."""
    pass

async def main():
    # asyncio.run's SIGINT handler counts cumulative Ctrl+Cs and raises
    # KeyboardInterrupt out of the loop on the 2nd one, exiting the program.
    # Install our own handler so only the in-flight task is cancelled.
    loop = asyncio.get_running_loop()
    active_task: asyncio.Task | None = None

    def on_sigint():
        if active_task and not active_task.done():
            active_task.cancel()

    loop.add_signal_handler(signal.SIGINT, on_sigint)

    # Mutable list — call_llm mutates in place so history persists across turns.
    conversation_history = []

    while True:
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