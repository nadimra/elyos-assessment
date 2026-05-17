import asyncio

async def get_user_input() -> str:
    """Get input from user."""
    pass

async def call_llm(user_input: str, conversation_history: list):
    """Send input to LLM, handle tool calls, yield streaming response."""
    pass

async def get_weather(location: str) -> dict:
    """Fetch weather from API (~200ms)."""
    pass

async def research_topic(topic: str) -> dict:
    """Research a topic (3-8 seconds). Should be cancellable."""
    pass

async def main():
    conversation_history = []

    while True:
        user_input = await get_user_input()
        if user_input.lower() in ['quit', 'exit', 'q']:
            break

        # How do you handle cancellation while streaming?
        # How do you show pending state during slow tool calls?
        async for chunk in call_llm(user_input, conversation_history):
            print(chunk, end='', flush=True)
        print()

if __name__ == "__main__":
    asyncio.run(main())