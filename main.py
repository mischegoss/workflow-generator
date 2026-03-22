import argparse
import asyncio
import json
import os
import uuid

from dotenv import load_dotenv
from google.adk.runners import InMemoryRunner
from google.genai.types import Content, Part

from agents import build_pipeline
from tools.retrieval_tools import load_activity_list
from tools.decompose_tools import assess_complexity, estimate_activity_count

load_dotenv()

MVP_CEILING = 25


def _extract_session_state(runner, session_id: str, user_id: str) -> dict:
    """
    Reads the current session state from an InMemoryRunner.
    Returns a dict of all output_key values stored by agents.
    """
    try:
        sessions = runner.session_service._sessions
        key = (runner.app_name, user_id, session_id)
        session = sessions.get(key)
        if session:
            return dict(session.state)
    except Exception:
        pass
    return {}


def _ensure_dict(value) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            result = json.loads(value)
            if isinstance(result, dict):
                return result
        except Exception:
            pass
    return {}


def _check_needs_retry(state: dict) -> tuple[bool, str]:
    """
    Inspects session state after a pipeline run to determine if a retry is needed.

    Returns (needs_retry: bool, error_summary: str).

    Retry triggers:
    - composer_result.status == "xml_error": XML serialization failed
    - validation_result.status == "invalid": JSON structure validation failed
    """
    composer = _ensure_dict(state.get("composer_result", {}))
    validation = _ensure_dict(state.get("validation_result", {}))

    if composer.get("status") == "xml_error":
        stage = composer.get("xml_error_stage", "unknown")
        error = composer.get("xml_error", "unknown XML error")
        return True, f"XML serialization error (stage: {stage}): {error}"

    if validation.get("status") == "invalid":
        errors = validation.get("errors", [])
        if errors:
            error_lines = "\n".join(f"- {e}" for e in errors)
            return True, f"Workflow JSON validation errors:\n{error_lines}"
        return True, "Workflow JSON validation failed (no error details available)"

    return False, ""


async def _run_pipeline(prompt: str, app_name: str, user_id: str) -> tuple[str, dict]:
    """
    Runs the full 7-agent pipeline for a given prompt.
    Returns (final_response_text, session_state_dict).
    """
    pipeline = build_pipeline()
    runner = InMemoryRunner(agent=pipeline, app_name=app_name)

    session = await runner.session_service.create_session(
        app_name=app_name,
        user_id=user_id,
    )

    user_message = Content(role="user", parts=[Part(text=prompt)])
    final_response = ""
    event_count = 0

    async for event in runner.run_async(
        user_id=user_id,
        session_id=session.id,
        new_message=user_message,
    ):
        event_count += 1
        print(
            f"  [event {event_count}] "
            f"author={getattr(event, 'author', '?')} "
            f"is_final={event.is_final_response()}"
        )
        if event.is_final_response() and event.content:
            for part in event.content.parts:
                if hasattr(part, "text") and part.text:
                    final_response += part.text

    print(f"  Total events: {event_count}")
    state = _extract_session_state(runner, session.id, user_id)
    return final_response, state


async def run(prompt: str) -> str:
    load_activity_list()

    # Pre-flight ceiling check (fast, no API calls)
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

    app_name = "workflow_generator"
    # Fresh unique user_id every run — prevents ADK from reusing cached session state
    run_user_id = f"system_{uuid.uuid4().hex[:8]}"

    # ── Attempt 1 ───────────────────────────────────────────────────────────
    print("\n[attempt 1] Running pipeline...")
    response, state = await _run_pipeline(prompt, app_name, run_user_id)

    needs_retry, error_summary = _check_needs_retry(state)

    if not needs_retry:
        return response if response else "No response captured"

    # ── Attempt 2 (retry) ───────────────────────────────────────────────────
    print(f"\n[attempt 2] First attempt failed. Retrying with error context...")
    print(f"  Error: {error_summary[:200]}")

    retry_prompt = (
        f"{prompt}\n\n"
        f"CORRECTION REQUIRED — The previous attempt produced a workflow with errors "
        f"that prevented it from being imported. Fix ALL of the following errors in "
        f"your workflow output. Do not reproduce any of these errors:\n\n"
        f"{error_summary}"
    )

    # Fresh user_id for retry so session state is clean for upstream agents
    retry_user_id = f"system_{uuid.uuid4().hex[:8]}"
    retry_response, retry_state = await _run_pipeline(retry_prompt, app_name, retry_user_id)

    # Check retry result
    retry_needs_retry, retry_error = _check_needs_retry(retry_state)
    if retry_needs_retry:
        print(f"\n[attempt 2] Retry also failed: {retry_error[:200]}")
        return (
            f"Workflow generation failed after 2 attempts.\n\n"
            f"Attempt 1 error: {error_summary}\n\n"
            f"Attempt 2 error: {retry_error}\n\n"
            f"Try breaking the workflow into smaller pieces or simplifying the prompt."
        )

    return retry_response if retry_response else "No response captured"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Resolve Actions Workflow Generator")
    parser.add_argument("--prompt", required=True, help="Natural language workflow description")
    args = parser.parse_args()
    result = asyncio.run(run(args.prompt))
    print(result)