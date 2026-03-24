import argparse
import asyncio
import json
import pathlib
import time
import uuid

import litellm
from dotenv import load_dotenv
from google.adk.runners import InMemoryRunner
from google.genai.types import Content, Part

from agents import build_pipeline
from tools.retrieval_tools import load_activity_list
from tools.decompose_tools import assess_complexity, estimate_activity_count

load_dotenv()

litellm.cache = None

MVP_CEILING  = 25
OUTPUT_DIR   = pathlib.Path("json_files")


async def _extract_session_state(runner: InMemoryRunner,
                                  app_name: str,
                                  session_id: str,
                                  user_id: str) -> dict:
    """Read session state via the public session service API."""
    try:
        session = await runner.session_service.get_session(
            app_name=app_name,
            user_id=user_id,
            session_id=session_id,
        )
        if session:
            return dict(session.state)
    except Exception as e:
        print(f"  [warning] Could not retrieve session state: {e}")
    return {}


def _ensure_dict(value) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1]
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


def _find_output_file(run_start: float) -> pathlib.Path | None:
    """
    Scan json_files/ for any .json file written after run_start.

    Python-stage state mutations (including output_result) don't survive
    ADK's get_session() call — only LlmAgent output_key values are persisted.
    The pipeline writes the file to disk successfully; we find it by mtime.
    """
    if not OUTPUT_DIR.exists():
        return None
    candidates = [
        f for f in OUTPUT_DIR.glob("*.json")
        if f.stat().st_mtime >= run_start
    ]
    if not candidates:
        return None
    # Most recently written file wins (handles parallel runs gracefully)
    return max(candidates, key=lambda f: f.stat().st_mtime)


def _check_needs_retry(state: dict) -> tuple:
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


async def _run_pipeline(prompt: str, run_id: str) -> tuple:
    """
    Returns (output_file: pathlib.Path | None, session_state: dict).
    Output file is found by scanning json_files/ for files written during this run.
    """
    app_name    = f"wf_gen_{run_id}"
    user_id     = f"system_{run_id}"
    run_start   = time.time()

    pipeline = build_pipeline()
    runner   = InMemoryRunner(agent=pipeline, app_name=app_name)

    session = await runner.session_service.create_session(
        app_name=app_name,
        user_id=user_id,
    )
    session.state["prompt"] = prompt

    user_message = Content(role="user", parts=[Part(text=prompt)])
    event_count  = 0

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
            state = await _extract_session_state(runner, app_name, session.id, user_id)
            return None, {**state, "_empty_response_error": True}
        raise

    print(f"  Total events: {event_count}")
    state       = await _extract_session_state(runner, app_name, session.id, user_id)
    output_file = _find_output_file(run_start)

    print(f"  Session keys: {[k for k in state if not k.startswith('_')]}")
    if output_file:
        print(f"  Output file:  {output_file}")
    else:
        print("  Output file:  NOT FOUND — checking for validation errors in state")

    return output_file, state


async def run(prompt: str) -> tuple:
    """
    Returns (output_file_path: str | None, chat_response: str).
    """
    load_activity_list()

    complexity = assess_complexity(prompt)
    estimate   = estimate_activity_count(prompt, complexity)

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
    output_file, state = await _run_pipeline(prompt, run_id)

    if output_file:
        return str(output_file), _build_chat_response(output_file, state)

    needs_retry, error_summary = _check_needs_retry(state)

    if not needs_retry:
        # No output file and no retry signal — unexpected failure
        return None, "Workflow generation failed — no output produced and no error captured."

    # ── Attempt 2 (retry) ─────────────────────────────────────────────────
    print(f"\n[attempt 2] First attempt failed. Retrying...")
    print(f"  Reason: {error_summary[:200]}")

    retry_prompt = prompt if state.get("_empty_response_error") else (
        f"{prompt}\n\n"
        f"CORRECTION REQUIRED — The previous attempt produced a workflow with errors "
        f"that prevented it from being imported. Fix ALL of the following errors in "
        f"your workflow output. Do not reproduce any of these errors:\n\n"
        f"{error_summary}"
    )

    retry_run_id = uuid.uuid4().hex[:12]
    print(f"  retry run_id={retry_run_id}")
    retry_output_file, retry_state = await _run_pipeline(retry_prompt, retry_run_id)

    if retry_output_file:
        return str(retry_output_file), _build_chat_response(retry_output_file, retry_state)

    retry_needs_retry, retry_error = _check_needs_retry(retry_state)
    if retry_needs_retry or True:  # surface the error regardless
        msg = (
            f"Workflow generation failed after 2 attempts.\n\n"
            f"Attempt 1: {error_summary}\n\n"
            f"Attempt 2: {retry_error if retry_needs_retry else 'No output produced.'}\n\n"
            f"Try breaking the workflow into smaller pieces or simplifying the prompt."
        )
        return None, msg

    return None, "Workflow generation failed after 2 attempts."


def _build_chat_response(output_file: pathlib.Path, state: dict) -> str:
    """Build human-readable summary from the written JSON file."""
    try:
        with open(output_file, encoding="utf-8") as f:
            output = json.load(f)
    except Exception:
        return f"Workflow written to {output_file} — could not read summary."

    lines = [
        f"Workflow generated: {output.get('name', '?')}",
        f"Pnumber:            {output.get('pnumber', '?')}",
        f"File:               {output_file}",
        "",
        f"To import: python convert_to_xml.py {output_file}",
    ]

    placeholders = output.get("placeholder_summary", [])
    if placeholders:
        placeholder_items = [i for i in placeholders if i.get("kind") == "placeholder"]
        verify_items      = [i for i in placeholders if i.get("kind") == "verify"]
        if placeholder_items:
            lines.append(f"\n{len(placeholder_items)} field(s) need values before import:")
            for item in placeholder_items:
                lines.append(f"  [{item['activity']}] {item['field']}: {item['placeholder']}")
        if verify_items:
            lines.append(f"\n{len(verify_items)} item(s) require manual review:")
            for item in verify_items:
                lines.append(f"  [{item['activity']}] {item['message']}")

    return "\n".join(lines)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Resolve Actions Workflow Generator")
    parser.add_argument("--prompt", required=True, help="Natural language workflow description")
    args = parser.parse_args()
    file_path, chat_result = asyncio.run(run(args.prompt))
    print(chat_result)
    if file_path:
        print(f"\nJSON output: {file_path}")