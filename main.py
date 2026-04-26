"""
main.py — entry point for the workflow generator.

Retry logic:
  Attempt 1: full pipeline (Decomposer → Placer → Enrichment → Wirer)
  Attempt 2 on validation failure: CorrectionPipeline (Wirer only)
  Attempt 2 on Wirer truncation/drop: CorrectionPipeline (Wirer only)
  Attempt 2 on empty response: full pipeline retry with fresh session

ADK SESSION STATE NOTE:
  State passed to create_session(state=initial_state) persists through
  get_session() and is visible inside pipeline agents.
  Post-create mutations (session.state[x] = y after create_session) do NOT
  persist — ADK reads from the session service, not the local object.
  The correction pipeline therefore passes all required state via the
  state= parameter, not via post-create mutation.

TELEMETRY (Phase E):
  One telemetry_session_id is generated per run() call and survives across
  retry attempts — the user's session is one logical thing even if the
  pipeline runs twice. It's set in initial_state for both _run_pipeline
  and _run_correction_pipeline, where agents/pipeline.py reads it.

  Events emitted by main.py:
    session_start         once at the top of run()
    correction_fired      once when attempt 2 fires, with resolution status
    session_complete      once via try/finally, regardless of exit path

  All other events (decomposer_call, deterministic_middle, wirer_call,
  validation_result, generation_failed) come from agents/pipeline.py.
"""

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

from agents import build_pipeline, WorkflowPipeline
from agents.pipeline import build_correction_pipeline, CorrectionPipeline
from tools import telemetry
from tools.retrieval_tools import load_activity_list
from tools.decompose_tools import assess_complexity, estimate_activity_count

load_dotenv()

litellm.cache = None

MVP_CEILING = 25
OUTPUT_DIR  = pathlib.Path("json_files")


async def _extract_session_state(runner: InMemoryRunner,
                                  app_name: str,
                                  session_id: str,
                                  user_id: str) -> dict:
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
    if not OUTPUT_DIR.exists():
        return None
    candidates = [
        f for f in OUTPUT_DIR.glob("*.json")
        if f.stat().st_mtime >= run_start
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda f: f.stat().st_mtime)


def _check_needs_retry(state: dict) -> tuple:
    """
    Returns (needs_retry, reason_string).

    Retry triggers, in priority order:
      1. Empty LLM response  →  full pipeline retry (fresh session)
      2. Wirer truncation / dropped activities  →  correction pipeline
      3. Validation status invalid  →  correction pipeline

    Trigger 2 was added after Phase E verified that activity count guard
    failures were silently terminating runs. The CorrectionPipeline already
    receives "do NOT omit any activities" guidance in its prompt, so giving
    the Wirer a second swing usually resolves truncation.
    """
    if state.get("_empty_response_error"):
        return True, "Model returned empty response mid-pipeline. Retrying with fresh session."

    # Catch activity count guard failures (Wirer truncated or dropped activities).
    # The guard writes its diagnostic into output_result.errors before exiting.
    output = _ensure_dict(state.get("output_result", {}))
    if output.get("status") == "failed":
        for err in output.get("errors", []):
            err_str = str(err)
            if "truncated" in err_str or "returned 0 activities" in err_str:
                error_lines = "\n".join(f"- {e}" for e in output["errors"])
                return True, (
                    f"WirerAgent output incomplete:\n{error_lines}\n"
                    f"On retry, return ALL top-level activities from the input — "
                    f"do not omit any."
                )

    validation = _ensure_dict(state.get("validation_result", {}))
    if validation.get("status") == "invalid":
        errors = validation.get("errors", [])
        if errors:
            error_lines = "\n".join(f"- {e}" for e in errors)
            return True, f"Workflow JSON validation errors:\n{error_lines}"
        return True, "Workflow JSON validation failed (no error details available)"

    return False, ""


def _llm_calls_in_attempt(state: dict, was_correction: bool) -> int:
    """
    Approximate LLM call count from a single pipeline attempt's session state.

    For full pipeline: decomposer + wirer = up to 2 calls. We count what
    actually produced output (decomposition present means decomposer ran;
    workflow_json present means wirer ran, even if output was empty).

    For correction pipeline: only wirer runs; decomposer is reused via
    initial state. Count the wirer call only.
    """
    if was_correction:
        return 1 if state.get("workflow_json") is not None else 0
    n = 0
    if state.get("decomposition"):
        n += 1
    if state.get("workflow_json") is not None:
        n += 1
    return n


async def _run_pipeline(prompt: str, run_id: str,
                        telemetry_session_id: str) -> tuple:
    """Full pipeline run (Attempt 1). The telemetry_session_id is propagated
    into initial_state so agents/pipeline.py can read it via _sid()."""
    app_name  = f"wf_gen_{run_id}"
    user_id   = f"system_{run_id}"
    run_start = time.time()

    pipeline = build_pipeline()
    runner   = InMemoryRunner(agent=pipeline, app_name=app_name)

    # Pass prompt + telemetry session id in initial state so they survive
    # get_session() and are readable inside the pipeline.
    session = await runner.session_service.create_session(
        app_name=app_name,
        user_id=user_id,
        state={
            "prompt":               prompt,
            "telemetry_session_id": telemetry_session_id,
        },
    )

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
        raw_wf = _ensure_dict(state.get("workflow_json"))
        if raw_wf:
            try:
                from tools.validation_tools import run_all_validators
                val = run_all_validators(raw_wf)
                if val["status"] == "invalid":
                    state = {**state, "validation_result": val}
                    errors = val.get("errors", [])
                    correction_lines = ["CORRECTION REQUIRED"] + [f"- {e}" for e in errors]
                    state["_correction_prompt"] = "\n".join(correction_lines)
                    print(f"  Post-run validation: invalid ({len(errors)} error(s))")
            except Exception as e:
                print(f"  Post-run validation check failed: {e}")

    return output_file, state


async def _run_correction_pipeline(
    prior_state: dict,
    error_summary: str,
    original_prompt: str,
    run_id: str,
    telemetry_session_id: str,
) -> tuple:
    """
    Correction pipeline run (Attempt 2 on validation failure or Wirer drop).

    KEY: all required state is passed via state=initial_state to create_session().
    Post-create mutations (session.state[x] = y) are NOT visible inside the
    pipeline — ADK reads state from the session service, not the local object.
    """
    app_name  = f"wf_corr_{run_id}"
    user_id   = f"system_{run_id}"
    run_start = time.time()

    pipeline = build_correction_pipeline()
    runner   = InMemoryRunner(agent=pipeline, app_name=app_name)

    # Build the correction message with embedded prior workflow_json
    prior_workflow_json = _ensure_dict(prior_state.get("workflow_json", {}))
    prior_wf_str = ""
    if prior_workflow_json:
        try:
            prior_wf_str = (
                "\n\nHere is the workflow JSON from the previous attempt "
                "(the one that contained the errors listed above). "
                "Fix the specific errors in this workflow and return the COMPLETE "
                "corrected workflow_raw_data — do NOT omit any activities:\n\n"
                + json.dumps(prior_workflow_json, indent=2)
            )
        except Exception:
            prior_wf_str = ""

    correction_message = (
        f"CORRECTION REQUIRED — The previous attempt produced a workflow with errors "
        f"that prevented it from being imported. Fix ALL of the following errors. "
        f"Do not reproduce any of these errors:\n\n"
        f"{error_summary}"
        f"{prior_wf_str}\n\n"
        f"Original workflow request: {original_prompt}"
    )

    # All state passed here — the only way to make it visible inside the pipeline
    initial_state = {
        "prompt":               original_prompt,
        "decomposition":        _ensure_dict(prior_state.get("decomposition", {})),
        "placed_skeleton":      _ensure_dict(prior_state.get("placed_skeleton", {})),
        "telemetry_session_id": telemetry_session_id,
    }

    session = await runner.session_service.create_session(
        app_name=app_name,
        user_id=user_id,
        state=initial_state,
    )

    user_message = Content(role="user", parts=[Part(text=correction_message)])
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
            print(f"  [correction] Empty response from model")
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
        print("  Output file:  NOT FOUND after correction attempt")

    return output_file, state


async def run(prompt: str) -> tuple:
    """Returns (output_file_path: str | None, chat_response: str)."""
    load_activity_list()

    # Telemetry session setup. One sid for the entire user run, even if we
    # retry. session_complete fires in the finally block at every exit path.
    telemetry_session_id = f"sess_{uuid.uuid4().hex[:12]}"
    session_start_time   = time.time()
    total_llm_calls      = 0
    final_state          = "unknown"

    try:
        try:
            telemetry.log_session_start(
                telemetry_session_id,
                prompt=prompt,
                user_agent="cli",
            )
        except Exception as telem_err:
            print(f"  [main] telemetry.log_session_start failed: {telem_err}")

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
            final_state = "rejected_oversized"
            return None, msg

        # Attempt 1
        run_id = uuid.uuid4().hex[:12]
        print(f"\n[attempt 1] run_id={run_id}")
        output_file, state = await _run_pipeline(
            prompt, run_id, telemetry_session_id
        )
        total_llm_calls += _llm_calls_in_attempt(state, was_correction=False)

        if output_file:
            final_state = "success"
            return str(output_file), _build_chat_response(output_file, state)

        needs_retry, error_summary = _check_needs_retry(state)

        if not needs_retry:
            final_state = "failed_no_retry"
            return None, "Workflow generation failed — no output produced and no error captured."

        # Attempt 2
        print(f"\n[attempt 2] First attempt failed. Retrying...")
        print(f"  Reason: {error_summary[:200]}")

        retry_run_id = uuid.uuid4().hex[:12]
        print(f"  retry run_id={retry_run_id}")

        was_empty_response = bool(state.get("_empty_response_error"))
        retry_reason = "empty_response" if was_empty_response else "validation_errors"
        retry_errors: list = []
        if not was_empty_response:
            # For both validation-invalid and Wirer-truncation paths, the
            # error list comes from output_result first, falling back to
            # validation_result.
            output     = _ensure_dict(state.get("output_result", {}))
            validation = _ensure_dict(state.get("validation_result", {}))
            retry_errors = list(output.get("errors", []) or
                                validation.get("errors", []))

        if was_empty_response:
            print(f"  Strategy: full pipeline retry (empty response)")
            retry_output_file, retry_state = await _run_pipeline(
                prompt, retry_run_id, telemetry_session_id
            )
            total_llm_calls += _llm_calls_in_attempt(retry_state, was_correction=False)
        else:
            print(f"  Strategy: correction pipeline (WirerAgent only)")
            retry_output_file, retry_state = await _run_correction_pipeline(
                prior_state=state,
                error_summary=error_summary,
                original_prompt=prompt,
                run_id=retry_run_id,
                telemetry_session_id=telemetry_session_id,
            )
            total_llm_calls += _llm_calls_in_attempt(retry_state, was_correction=True)

        # Emit correction_fired with resolution status known.
        # Fired AFTER the retry so resolved_after_correction reflects reality
        # rather than being null.
        try:
            telemetry.log_correction_fired(
                telemetry_session_id,
                reason=retry_reason,
                errors=retry_errors,
                resolved_after_correction=bool(retry_output_file),
            )
        except Exception as telem_err:
            print(f"  [main] telemetry.log_correction_fired failed: {telem_err}")

        if retry_output_file:
            final_state = "success_after_retry"
            return str(retry_output_file), _build_chat_response(retry_output_file, retry_state)

        _, retry_error = _check_needs_retry(retry_state)
        msg = (
            f"Workflow generation failed after 2 attempts.\n\n"
            f"Attempt 1: {error_summary}\n\n"
            f"Attempt 2: {retry_error if retry_error else 'No output produced.'}\n\n"
            f"Try breaking the workflow into smaller pieces or simplifying the prompt."
        )
        final_state = "failed_after_retry"
        return None, msg

    finally:
        try:
            telemetry.log_session_complete(
                telemetry_session_id,
                total_duration_sec=round(time.time() - session_start_time, 2),
                final_state=final_state,
                total_llm_calls=total_llm_calls,
            )
        except Exception as telem_err:
            print(f"  [main] telemetry.log_session_complete failed: {telem_err}")


def _build_chat_response(output_file: pathlib.Path, state: dict) -> str:
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
        update_items  = [i for i in placeholders if i.get("kind") == "update"]
        verify_items  = [i for i in placeholders if i.get("kind") == "verify"]
        if update_items:
            lines.append(f"\n{len(update_items)} field(s) to update before running:")
            for item in update_items:
                lines.append(f"  [{item['activity']}] {item['message']}")
        if verify_items:
            lines.append(f"\n{len(verify_items)} item(s) to verify after import:")
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