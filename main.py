import argparse
import asyncio
import os

from dotenv import load_dotenv
from google.adk.runners import InMemoryRunner
from google.genai.types import Content, Part

from agents import build_pipeline
from tools.retrieval_tools import load_activity_list
from tools.decompose_tools import assess_complexity, estimate_activity_count

load_dotenv()

MVP_CEILING = 25


async def run(prompt: str) -> str:
    load_activity_list()

    complexity = assess_complexity(prompt)
    estimate = estimate_activity_count(prompt, complexity)

    if estimate["estimated_total"] > MVP_CEILING:
        return (
            f"This workflow exceeds the 25-activity MVP limit. "
            f"Estimated: ~{estimate['estimated_total']} activities.\n\n"
            f"Suggested approach: break this into focused sub-workflows of "
            f"25 activities or fewer. Each can be generated and imported independently.\n"
            f"{estimate.get('suggested_split', '')}"
        )

    pipeline = build_pipeline()
    runner = InMemoryRunner(agent=pipeline, app_name="workflow_generator")

    session = await runner.session_service.create_session(
        app_name="workflow_generator",
        user_id="system",
    )

    user_message = Content(role="user", parts=[Part(text=prompt)])

    final_response = ""
    event_count = 0

    async for event in runner.run_async(
        user_id="system",
        session_id=session.id,
        new_message=user_message,
    ):
        event_count += 1
        print(f"  [event {event_count}] author={getattr(event, 'author', '?')} is_final={event.is_final_response()}")

        if event.is_final_response() and event.content:
            for part in event.content.parts:
                if hasattr(part, 'text') and part.text:
                    final_response += part.text

    print(f"  Total events received: {event_count}")
    return final_response if final_response else "No response captured"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Resolve Actions Workflow Generator")
    parser.add_argument("--prompt", required=True, help="Natural language workflow description")
    args = parser.parse_args()
    result = asyncio.run(run(args.prompt))
    print(result)