"""Multi-turn smoke test: prove ClaudeSDKClient keeps state across query() calls."""

import asyncio

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
)
from dotenv import load_dotenv

# Loads .env into os.environ. The SDK reads ANTHROPIC_API_KEY (or the Bedrock vars)
# from the environment when it spawns the Claude Code subprocess, so this must run
# before the client connects.
load_dotenv()


async def main() -> None:
    options = ClaudeAgentOptions(
        model="claude-opus-5",
        setting_sources=[],  # isolation: do NOT inherit CLAUDE.md or ~/.claude settings
        allowed_tools=[],  # no tool schemas shipped; a chat needs none
        system_prompt="You are a friendly assistant. Keep answers to two sentences.",
    )

    # The context manager calls connect() on entry and disconnect() on exit.
    # One connection == one session; every query() below lands in the same transcript.
    async with ClaudeSDKClient(options=options) as client:
        while True:
            user_text = input("your message here> ")  # blocking; fine for a smoke test
            if user_text.strip().lower() == "quit":
                break

            # .query() only SENDS. It returns nothing; messages come back via receive_response().
            await client.query(user_text)

            # receive_response() yields messages for THIS turn and stops after the ResultMessage.
            async for message in client.receive_response():
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            print(f"assistant> {block.text}")

                if isinstance(message, ResultMessage):
                    print(f"  [turn {message.num_turns}  cost_usd: {message.total_cost_usd}]")


if __name__ == "__main__":
    asyncio.run(main())
