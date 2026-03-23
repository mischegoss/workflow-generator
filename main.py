import argparse
import asyncio
import json
import os
import uuid

import litellm
from dotenv import load_dotenv
from google.adk.runners import InMemoryRunner
from google.genai.types import Content, Part

from agents import build_pipeline
from tools.retrieval_tools import load_activity_list
from tools.decompose_tools import assess_complexity, estimate_activity_count

load_dotenv()

# Disable LiteLlm response caching entirely.
# LiteLlm can cache completions for identical/near-identical prompts and return
# stale responses without hitting the API.
litellm.cache = None

MVP_CEILING = 25


def _extract_session_state(runner: InMemoryRunner,
                            app_name: str,
                            session_id: str,
                            user_id: str) -> dict:
    """
    Reads the current session state from the runner's session service.
    Returns a dict of all output_key values stored by agents.
    """
    try:
        sessions = runner.session_service._sessions
        key = (app_name, user_id, session_id)
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


def _check_needs_retry(state: dict) -> tuple:
    """
    Inspects session state after a pipeline run to determine if a retry is needed.

    Returns (needs_retry: bool, error_summary: str).

    Retry triggers:
    - _empty_response_error: model returned empty response mid-pipeline
    - composer_result.status == "xml_error": XML serialization failed
    - validation_result.status == "invalid": JSON structure validation failed
    """
    if state.get("_empty_response_error"):
        return True, "Model returned empty response mid-pipeline. Retrying with fresh session."

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


def _extract_xml(state: dict) -> str | None:
    """
    Pulls xml_content out of composer_result.
    Returns the XML string if status is complete, None otherwise.
    """
    composer = _ensure_dict(state.get("composer_result", {}))
    if composer.get("status") == "complete":
        return composer.get("xml_content") or None
    return None


async def _run_pipeline(prompt: str, run_id: str) -> tuple:
    """
    Runs the full 7-agent pipeline for a given prompt.
    Returns (xml_string | None, session_state_dict).

    xml_string is the validated XML content from ComposerAgent, or None on failure.
    The caller is responsible for writing it to disk or sending it via webhook.

    Every call creates completely isolated objects:
    - Fresh pipeline (new agent instances via build_pipeline())
    - Fresh InMemoryRunner (creates its own internal session service)
    - Unique app_name per run (prevents any session key collision in ADK)
    - Unique user_id per run
    """
    app_name = f"wf_gen_{run_id}"
    user_id = f"system_{run_id}"

    pipeline = build_pipeline()
    runner = InMemoryRunner(agent=pipeline, app_name=app_name)

    session = await runner.session_service.create_session(
        app_name=app_name,
        user_id=user_id,
    )

    user_message = Content(role="user", parts=[Part(text=prompt)])
    event_count = 0

    try:
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

    except ValueError as e:
        if "No message in response" in str(e):
            print(f"  [pipeline] Empty response from model — will retry")
            state = _extract_session_state(runner, app_name, session.id, user_id)
            return None, {**state, "_empty_response_error": True}
        raise

    print(f"  Total events: {event_count}")
    state = _extract_session_state(runner, app_name, session.id, user_id)
    xml = _extract_xml(state)
    return xml, state


async def run(prompt: str) -> tuple:
    """
    Runs the pipeline for a given prompt.
    Returns (xml_string | None, chat_response: str).

    xml_string: the validated XML content ready to write to disk or send via webhook.
                None if generation failed.
    chat_response: human-readable summary of the result including placeholders/errors.

    The caller decides what to do with the XML — write to disk for manual import,
    or POST to the Actions webhook in production.
    """
    load_activity_list()

    # Pre-flight ceiling check (fast, no API calls)
    complexity = assess_complexity(prompt)
    estimate = estimate_activity_count(prompt, complexity)

    if estimate["estimated_total"] > MVP_CEILING:
        msg = (
            f"This workflow exceeds the 25-activity MVP limit. "
            f"Estimated: ~{estimate['estimated_total']} activities.\n\n"
            f"Suggested approach: break this into focused sub-workflows of "
            f"25 activities or fewer. Each can be generated and imported independently.\n"
            f"{estimate.get('suggested_split', '')}"
        )
        return None, msg

    # ── Attempt 1 ─────────────────────────────────────────────────────────
    run_id = uuid.uuid4().hex[:12]
    print(f"\n[attempt 1] run_id={run_id}")
    xml, state = await _run_pipeline(prompt, run_id)

    needs_retry, error_summary = _check_needs_retry(state)

    if not needs_retry:
        chat = _ensure_dict(state.get("composer_result", {})).get("chat_response", "")
        return xml, chat

    # ── Attempt 2 (retry) ──────────────────────────────────────────────────
    print(f"\n[attempt 2] First attempt failed. Retrying...")
    print(f"  Reason: {error_summary[:200]}")

    if state.get("_empty_response_error"):
        retry_prompt = prompt
    else:
        retry_prompt = (
            f"{prompt}\n\n"
            f"CORRECTION REQUIRED — The previous attempt produced a workflow with errors "
            f"that prevented it from being imported. Fix ALL of the following errors in "
            f"your workflow output. Do not reproduce any of these errors:\n\n"
            f"{error_summary}"
        )

    retry_run_id = uuid.uuid4().hex[:12]
    print(f"  retry run_id={retry_run_id}")
    retry_xml, retry_state = await _run_pipeline(retry_prompt, retry_run_id)

    retry_needs_retry, retry_error = _check_needs_retry(retry_state)
    if retry_needs_retry:
        print(f"\n[attempt 2] Retry also failed: {retry_error[:200]}")
        msg = (
            f"Workflow generation failed after 2 attempts.\n\n"
            f"Attempt 1 error: {error_summary}\n\n"
            f"Attempt 2 error: {retry_error}\n\n"
            f"Try breaking the workflow into smaller pieces or simplifying the prompt."
        )
        return None, msg

    chat = _ensure_dict(retry_state.get("composer_result", {})).get("chat_response", "")
    return retry_xml, chat


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Resolve Actions Workflow Generator")
    parser.add_argument("--prompt", required=True, help="Natural language workflow description")
    args = parser.parse_args()
    xml_result, chat_result = asyncio.run(run(args.prompt))
    print(chat_result)
    if xml_result:
        print(f"\nXML ({len(xml_result)} bytes):\n{xml_result[:500]}...")