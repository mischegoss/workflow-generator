import argparse
import asyncio
import json
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
    """
    Safely converts agent output to a dict.

    Handles two common ADK/Gemini serialization quirks:
    1. Agent outputs a JSON string instead of a dict (output_key passthrough).
    2. Agent wraps its JSON output in markdown code fences (```json ... ```)
       despite being instructed not to. Gemini Flash does this frequently.
    """
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        # Strip markdown code fences before attempting JSON parse.
        text = value.strip()
        if text.startswith("```"):
            # Remove opening fence line (e.g. ```json or ```)
            text = text.split("\n", 1)[-1]
            # Remove closing fence
            if text.endswith("```"):
                text = text[: text.rfind("```")]
            text = text.strip()
        try:
            result = json.loads(text)
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
    - validation_result.status == "invalid": JSON structure validation failed
      (xml_error removed — ComposerAgent is gone, XML is offline via convert_to_xml.py)
    """
    if state.get("_empty_response_error"):
        return True, "Model returned empty response mid-pipeline. Retrying with fresh session."

    validation = _ensure_dict(state.get("validation_result", {}))

    if validation.get("status") == "invalid":
        errors = validation.get("errors", [])
        if errors:
            error_lines = "\n".join(f"- {e}" for e in errors)
            return True, f"Workflow JSON validation errors:\n{error_lines}"
        return True, "Workflow JSON validation failed (no error details available)"

    return False, ""


def _extract_output(state: dict) -> dict | None:
    """
    Retrieves the output result dict from pipeline session state.
    Returns None if the output stage did not complete successfully.
    """
    output = _ensure_dict(state.get("output_result", {}))
    if output.get("status") == "failed":
        return None
    # output_result must have at minimum a name and output_file to be valid
    if output.get("name") and output.get("output_file"):
        return output
    return None


async def _run_pipeline(prompt: str, run_id: str) -> tuple:
    """
    Runs the full pipeline for a given prompt.
    Returns (output_dict | None, session_state_dict).

    output_dict contains the written JSON file path and workflow metadata.
    None on failure — caller checks session state for error details.

    Every call creates completely isolated objects:
    - Fresh pipeline (new WorkflowPipeline instance via build_pipeline())
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
    output = _extract_output(state)
    return output, state


async def run(prompt: str) -> tuple:
    """
    Runs the pipeline for a given prompt.
    Returns (output_file_path | None, chat_response: str).

    output_file_path: path to the written .json file in json_files/.
                      None if generation failed.
    chat_response: human-readable summary of the result including placeholders/errors.

    To convert to XML for manual import into Resolve Actions:
        python convert_to_xml.py json_files/<workflow_name>.json
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
    output, state = await _run_pipeline(prompt, run_id)

    needs_retry, error_summary = _check_needs_retry(state)

    if not needs_retry:
        chat = _build_chat_response(output, state)
        return output.get("output_file") if output else None, chat

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
    retry_output, retry_state = await _run_pipeline(retry_prompt, retry_run_id)

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

    chat = _build_chat_response(retry_output, retry_state)
    return retry_output.get("output_file") if retry_output else None, chat


def _build_chat_response(output: dict | None, state: dict) -> str:
    """
    Builds a human-readable summary of the pipeline result.
    Replaces the old composer_result.chat_response from ComposerAgent.
    """
    if not output:
        validation = _ensure_dict(state.get("validation_result", {}))
        errors = validation.get("errors", [])
        return "Workflow generation failed.\n" + "\n".join(f"- {e}" for e in errors)

    lines = [
        f"Workflow generated: {output['name']}",
        f"Pnumber: {output['pnumber']}",
        f"File: {output['output_file']}",
        f"",
        f"To import: python convert_to_xml.py {output['output_file']}",
    ]

    placeholders = output.get("placeholder_summary", [])
    if placeholders:
        lines.append(f"\n{len(placeholders)} item(s) require manual configuration:")
        for item in placeholders:
            if item["kind"] == "placeholder":
                lines.append(f"  - [{item['activity']}] {item['field']}: {item['placeholder']}")
            elif item["kind"] == "verify":
                lines.append(f"  - [{item['activity']}] {item['message']}")

    return "\n".join(lines)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Resolve Actions Workflow Generator")
    parser.add_argument("--prompt", required=True, help="Natural language workflow description")
    args = parser.parse_args()
    file_path, chat_result = asyncio.run(run(args.prompt))
    print(chat_result)
    if file_path:
        print(f"\nJSON output: {file_path}")