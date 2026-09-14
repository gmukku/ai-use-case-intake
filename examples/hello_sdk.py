import asyncio

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    query,
)
from dotenv import load_dotenv

# Loads .env into os.environ. The SDK reads ANTHROPIC_API_KEY (or the Bedrock vars)
# from the environment when it spawns the Claude Code subprocess, so this must run
# before the first query().
load_dotenv()


async def main() -> None:
    options = ClaudeAgentOptions(
        model="claude-opus-5",
        max_turns=1,  # one model turn; no tool loop for this smoke test
    )

    # query() is an async generator: it yields messages as the subprocess emits them.
    # Expect roughly: SystemMessage (init) -> AssistantMessage -> ResultMessage.
    async for message in query(prompt="Say hello in one sentence.", options=options):
        print(f"[{type(message).__name__}]")

        if isinstance(message, AssistantMessage):
            # An assistant turn is a list of content blocks, not a single string.
            for block in message.content:
                if isinstance(block, TextBlock):
                    print(f"  text: {block.text}")

        if isinstance(message, ResultMessage):
            # This is what the audit layer will log per turn later.
            print(f"  session_id: {message.session_id}")
            print(f"  turns: {message.num_turns}  cost_usd: {message.total_cost_usd}")


if __name__ == "__main__":
    asyncio.run(main())
