"""
api.py — Phase G2 HTTP service for the workflow generator.

Wraps the existing pipeline (agents/pipeline.py + tools/pipeline_stages.py)
in a FastAPI app exposing six endpoints per spec §17:

  POST /plan                       — Phase 0+1a+1b: validate, match, decompose
  POST /build-activities           — Phase 2b:      deterministic middle
  POST /generate-artifacts         — Phase 3:       Wirer + post-Wirer + retry
  GET  /download/{filename}        — serve generated JSON
  POST /outcome/{tracking_token}   — record post-import outcome
  GET  /health                     — liveness

GATE FLOW
  Each endpoint maps to one gate boundary in the §2.1 architecture. State
  persists across HTTP calls in the in-memory SessionStore (tools/session_store.py).
  Each pipeline run creates a fresh ADK runner with state seeded from the
  session entry — this avoids long-lived ADK sessions across HTTP requests
  and matches how main.py's _run_correction_pipeline already works.

  Full state is persisted per gate (the "heavy" option from G1.5 closure).
  Recomputing the deterministic middle on each request would re-run retrieval
  and skeleton building, defeating the gate split.

CORRECTION HANDLING
  /generate-artifacts handles validation/truncation retry inline. If the
  ArtifactsPipeline fails (output_result.status == "failed" with retryable
  error), the existing CorrectionPipeline from agents.pipeline runs as
  attempt 2. Same retry decision tree as main.py, no duplication of logic.

  Per Q4 from the G2 design discussion, the response body indicates whether
  retry happened (retried: bool, retry_reason: str | null) so the frontend
  can show a subtle indicator without managing it as a distinct state.

GATE 1 REJECTION
  Per Q3 from the G2 design discussion, /plan accepts an optional
  feedback_text. When present alongside an existing session_id, fires
  gate1_rejected for the prior plan and re-runs decomposition with
  the original prompt + appended feedback.

TELEMETRY
  Stage-level events (decomposer_call, wirer_call, deterministic_middle,
  validation_result, correction_fired, generation_failed) fire from inside
  agents/pipeline.py — no changes from CLI behavior.

  Gate-level events (session_start, session_complete, gate1_*, gate2_*,
  xml_downloaded, outcome_reported) fire from this module at the HTTP
  boundary. Time-to-decision is computed from gateN_shown_at timestamps
  stored in the session entry.

SESSION SWEEPER
  SessionSweeper from tools/telemetry.py is started at FastAPI lifespan
  startup against SessionStore.sessions. Emits session_abandon for any
  session inactive ≥ 4 hours. Single-process daemon thread; multi-worker
  deployments would emit duplicates (revisit when scaling).

ENV
  PORT      — defaults to 8000
  HOST      — defaults to 0.0.0.0
  DATA_DIR  — passed through to telemetry / prompt_validator / preview
  LOG_DIR   — passed through to telemetry
"""

import json
import os
import pathlib
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

import litellm
from dotenv import load_dotenv
from google.adk.runners import InMemoryRunner
from google.genai.types import Content, Part

from agents.pipeline import build_correction_pipeline
from agents.api_pipelines import (
    build_plan_pipeline,
    build_build_activities_pipeline,
    build_artifacts_pipeline,
)

from tools import telemetry
from tools.result_capture import pop_result
from tools.session_store import SessionStore
from tools.prompt_validator import validate_prompt
from tools.task_matcher import match_tasks
from tools.preview import build_task_preview
from tools.retrieval_tools import load_activity_list


# ---------------------------------------------------------------------------
# Module setup
# ---------------------------------------------------------------------------

load_dotenv()
litellm.cache = None

OUTPUT_DIR = pathlib.Path("json_files")

# In-process singletons
_store:   SessionStore | None = None
_sweeper: telemetry.SessionSweeper | None = None


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan — initialize the session store, sweeper, and warm
    the activity list cache. On shutdown, stop the sweeper cleanly."""
    global _store, _sweeper

    print("[api] startup: initializing session store and sweeper")
    _store = SessionStore()

    # Warm activity list — same call main.py makes at startup. Avoids first-
    # request latency.
    try:
        load_activity_list()
        print("[api] startup: activity list loaded")
    except Exception as e:
        print(f"[api] startup: activity list warmup failed (non-fatal): {e}")

    # Sweeper feeds on the live SessionStore
    _sweeper = telemetry.SessionSweeper(get_sessions=lambda: _store.sessions)
    _sweeper.start()
    print("[api] startup: SessionSweeper running "
          f"(timeout={telemetry.SESSION_TIMEOUT_SEC}s, "
          f"interval={telemetry.SESSION_SWEEP_INTERVAL_SEC}s)")

    try:
        yield
    finally:
        print("[api] shutdown: stopping sweeper")
        if _sweeper is not None:
            _sweeper.stop()
        print("[api] shutdown: complete")


app = FastAPI(
    title="Resolve Actions Workflow Generator",
    version="g2.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Pydantic request/response models
# ---------------------------------------------------------------------------

class PlanRequest(BaseModel):
    prompt:        str
    session_id:    str | None = None
    feedback_text: str | None = None   # if present + session_id, gate1 rejection retry


class PlanResponse(BaseModel):
    session_id:        str
    decomposition:     dict
    task_match_result: dict | None
    preview:           str
    step_count:        int
    state:             str   # gate state machine label


class BuildActivitiesRequest(BaseModel):
    session_id: str


class BuildActivitiesResponse(BaseModel):
    session_id:        str
    enriched_workflow: dict
    activity_manifest: list
    activity_count:    int
    state:             str


class GenerateArtifactsRequest(BaseModel):
    session_id: str


class GenerateArtifactsResponse(BaseModel):
    session_id:      str
    tracking_token:  str
    output_filename: str
    download_url:    str
    summary:         str
    retried:         bool
    retry_reason:    str | None
    placeholders:    list
    verify_notes:    list
    state:           str


class OutcomeRequest(BaseModel):
    worked: bool
    notes:  str = ""


class OutcomeResponse(BaseModel):
    tracking_token: str
    recorded:       bool


# ---------------------------------------------------------------------------
# Helpers — ADK runner orchestration
# ---------------------------------------------------------------------------

async def _extract_state(runner: InMemoryRunner, app_name: str,
                         session_id: str, user_id: str) -> dict:
    """Pull the full session state out of the ADK runner before we throw
    the runner away. Mirrors main.py's _extract_session_state."""
    try:
        session = await runner.session_service.get_session(
            app_name=app_name, user_id=user_id, session_id=session_id,
        )
        if session:
            return dict(session.state)
    except Exception as e:
        print(f"  [api] could not retrieve ADK state: {e}")
    return {}


def _ensure_dict(value) -> dict:
    """Same shape as main.py's helper. Tolerates already-dict, JSON-string,
    or None."""
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


def _check_retryable_failure(state: dict) -> tuple[bool, str]:
    """Mirrors main.py's _check_needs_retry. Returns (needs_retry, reason).
    Triggers: empty response, Wirer truncation, validation invalid."""
    if state.get("_empty_response_error"):
        return True, "Model returned empty response — retrying with fresh session."

    output = _ensure_dict(state.get("output_result", {}))
    if output.get("status") == "failed":
        for err in output.get("errors", []):
            err_str = str(err)
            if "truncated" in err_str or "returned 0 activities" in err_str:
                return True, "WirerAgent output truncated — retrying via correction pipeline."

    validation = _ensure_dict(state.get("validation_result", {}))
    if validation.get("status") == "invalid":
        errors = validation.get("errors", [])
        if errors:
            return True, "Workflow JSON failed validation — retrying via correction pipeline."

    return False, ""


async def _run_pipeline_with_state(pipeline, prompt: str,
                                   initial_state: dict,
                                   run_label: str) -> dict:
    """Generic runner — creates a fresh InMemoryRunner with the given
    initial state, executes the pipeline, and returns the final session
    state.

    Combines two state sources:
      1. ADK's get_session() — picks up LlmAgent output_keys (decomposition,
         workflow_json) which propagate through the session service correctly.
      2. The tools.result_capture._RESULTS dict — picks up Python-stage
         outputs (enriched_workflow, annotation_result, validation_result,
         output_result) which DON'T survive get_session(). See the docstring
         on tools/result_capture.py for why this dual-source approach is
         needed. Capture-dict values take precedence on key collisions.
    """
    run_id   = uuid.uuid4().hex[:12]
    app_name = f"wf_{run_label}_{run_id}"
    user_id  = f"system_{run_id}"

    # Generate a capture key the pipeline reads from initial_state and
    # writes its outputs against. Unique per request to avoid collisions
    # and to allow pop_result to free memory after we read.
    capture_key = f"cap_{uuid.uuid4().hex}"
    seeded_state = {**initial_state, "_capture_key": capture_key}

    runner  = InMemoryRunner(agent=pipeline, app_name=app_name)
    session = await runner.session_service.create_session(
        app_name=app_name,
        user_id=user_id,
        state=seeded_state,
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
    except ValueError as e:
        # Empty response from model — extract whatever state survived and
        # mark the failure. Caller decides retry strategy.
        if "No message in response" in str(e):
            print(f"  [api/{run_label}] empty response from model")
            state = await _extract_state(runner, app_name, session.id, user_id)
            captured = pop_result(capture_key)
            return {**state, **captured, "_empty_response_error": True}
        raise

    print(f"  [api/{run_label}] events={event_count}")
    state    = await _extract_state(runner, app_name, session.id, user_id)
    captured = pop_result(capture_key)
    if captured:
        # Captured values take precedence — they're the live outputs from
        # the pipeline, not the (possibly stale) snapshot get_session() returned.
        state = {**state, **{k: v for k, v in captured.items() if v is not None}}
    return state


# ---------------------------------------------------------------------------
# Helpers — gate event emission
# ---------------------------------------------------------------------------

def _now() -> float:
    return time.time()


def _time_since(t0: float | None) -> float:
    """Seconds since t0, or 0.0 if t0 is None. Used for time_to_decision_sec."""
    if t0 is None:
        return 0.0
    return round(_now() - t0, 2)


def _build_chat_summary(output_filename: str, validation_result: dict | None,
                        annotation_result: dict | None) -> str:
    """Mirrors main.py's _build_chat_response. Returns a multi-line summary
    string covering filename, placeholder count, and verify notes."""
    placeholders = []
    if annotation_result:
        placeholders = annotation_result.get("placeholder_summary", [])

    lines = [f"Workflow generated: {output_filename}"]
    if placeholders:
        update_items = [i for i in placeholders if i.get("kind") == "update"]
        verify_items = [i for i in placeholders if i.get("kind") == "verify"]
        if update_items:
            lines.append(f"\n{len(update_items)} field(s) to update before running:")
            for item in update_items[:10]:   # cap to keep response readable
                lines.append(f"  [{item.get('activity', '?')}] {item.get('message', '')}")
            if len(update_items) > 10:
                lines.append(f"  ... and {len(update_items) - 10} more")
        if verify_items:
            lines.append(f"\n{len(verify_items)} item(s) to verify after import:")
            for item in verify_items[:10]:
                lines.append(f"  [{item.get('activity', '?')}] {item.get('message', '')}")
            if len(verify_items) > 10:
                lines.append(f"  ... and {len(verify_items) - 10} more")
    return "\n".join(lines)


def _split_placeholders(annotation_result: dict | None) -> tuple[list, list]:
    """Returns (update_items, verify_items) for the response."""
    if not annotation_result:
        return [], []
    placeholders = annotation_result.get("placeholder_summary", [])
    update_items = [i for i in placeholders if i.get("kind") == "update"]
    verify_items = [i for i in placeholders if i.get("kind") == "verify"]
    return update_items, verify_items


def _activity_count(workflow: dict) -> int:
    raw = workflow.get("workflow_raw_data", workflow) if isinstance(workflow, dict) else {}
    return sum(1 for v in raw.values() if isinstance(v, dict))


def _serialize_match_result(match_result) -> dict | None:
    """task_matcher returns a MatchResult dataclass (or similar). Convert to
    dict for JSON response and telemetry. Tolerates None and missing
    attributes — returns the most useful subset we can extract."""
    if match_result is None:
        return None
    out: dict = {}
    for attr in ("tasks", "confidence", "fallback", "unmatched_segments"):
        if hasattr(match_result, attr):
            out[attr] = getattr(match_result, attr)
    return out or None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
async def health() -> dict:
    """Liveness check. Reports session count and sweeper status."""
    return {
        "status":         "ok",
        "session_count":  len(_store.sessions) if _store else 0,
        "sweeper_alive":  bool(_sweeper and _sweeper._thread and _sweeper._thread.is_alive()),
        "version":        "g2.0",
    }


@app.post("/plan", response_model=PlanResponse)
async def plan(req: PlanRequest) -> PlanResponse:
    """Phase 0 (validate) + Phase 1a (task match) + Phase 1b (decompose).

    If session_id is omitted: create a new session, fire session_start.
    If session_id is present + feedback_text is present: this is a Gate 1
    rejection retry. Fire gate1_rejected for the prior plan, then re-run
    decomposition with the prompt amended to include the feedback."""
    if _store is None:
        raise HTTPException(500, "session store not initialized")

    is_rejection_retry = bool(req.session_id and req.feedback_text)

    # ---- session resolution ----------------------------------------------
    if req.session_id:
        existing = _store.get(req.session_id)
        if existing is None:
            raise HTTPException(404, f"session {req.session_id} not found")
        sid = req.session_id

        if is_rejection_retry:
            # Gate 1 rejection — log it before we do anything else
            try:
                telemetry.log_gate1_rejected(
                    sid,
                    feedback_text=req.feedback_text or "",
                    time_to_decision_sec=_time_since(existing.get("gate1_shown_at")),
                    retry_count=int(existing.get("retry_count_stage1", 0)),
                )
            except Exception as e:
                print(f"  [/plan] telemetry.log_gate1_rejected failed: {e}")

            _store.update(sid, retry_count_stage1=int(existing.get("retry_count_stage1", 0)) + 1)
        else:
            # Same session, fresh prompt — just bump activity
            _store.register_activity(sid)
    else:
        sid = _store.create_session(prompt=req.prompt)
        try:
            telemetry.log_session_start(sid, prompt=req.prompt, user_agent="api")
        except Exception as e:
            print(f"  [/plan] telemetry.log_session_start failed: {e}")

    entry = _store.get(sid)

    # ---- prompt validation (checks 1-6) ----------------------------------
    # For rejection retries the feedback_text is the new "prompt" being
    # validated — per spec §4 feedback uses the lower 5-char minimum.
    is_feedback = is_rejection_retry
    text_to_validate = req.feedback_text if is_rejection_retry else req.prompt

    validation = validate_prompt(
        prompt=text_to_validate,
        is_feedback=is_feedback,
        session_id=sid,
        session_store=_store,
    )
    if not validation.valid:
        raise HTTPException(400, {
            "error":         "prompt_validation_failed",
            "failed_check":  validation.failed_check,
            "message":       validation.message,
            "details":       validation.details,
        })

    # Bump prompt count for the per-session 50-prompt ceiling
    _store.increment_prompt_count(sid)

    # ---- compose effective prompt for decomposer -------------------------
    if is_rejection_retry:
        original_prompt = entry.get("original_prompt") or entry.get("prompt") or req.prompt
        effective_prompt = (
            f"{original_prompt}\n\n"
            f"User feedback on previous plan: {req.feedback_text}"
        )
    else:
        effective_prompt = req.prompt

    # ---- Phase 1a: task matching (deterministic, fast) -------------------
    try:
        # task_matcher.match_tasks reads the original prompt, not the
        # feedback-amended version — task labels should stay anchored to
        # the user's original intent.
        match_result = match_tasks(entry.get("original_prompt") or req.prompt)
    except Exception as e:
        print(f"  [/plan] task matcher failed (non-fatal): {e}")
        match_result = None

    # ---- Phase 1b: decompose via PlanPipeline ----------------------------
    initial_state = {
        "prompt":               effective_prompt,
        "telemetry_session_id": sid,
    }

    try:
        result_state = await _run_pipeline_with_state(
            build_plan_pipeline(),
            effective_prompt,
            initial_state,
            run_label="plan",
        )
    except Exception as e:
        print(f"  [/plan] pipeline raised: {e}")
        raise HTTPException(500, f"plan pipeline failed: {e}")

    decomposition = _ensure_dict(result_state.get("decomposition"))
    if not decomposition:
        # PlanPipeline already emitted generation_failed via _log_fatal
        raise HTTPException(502, {
            "error":   "decomposer_empty",
            "message": "DecomposerAgent returned empty or unparseable output.",
        })

    step_count = len(decomposition.get("steps", []))

    # ---- build preview ----------------------------------------------------
    try:
        preview_text = build_task_preview(
            match_result=match_result,
            decomposition=decomposition,
            prompt=req.prompt if not is_rejection_retry else effective_prompt,
            include_activities=False,
        )
    except Exception as e:
        print(f"  [/plan] preview build failed (non-fatal): {e}")
        preview_text = f"({step_count} steps — preview rendering failed: {e})"

    # ---- persist + emit gate1_preview_shown ------------------------------
    match_result_dict = _serialize_match_result(match_result)
    now = _now()
    _store.update(
        sid,
        decomposition=decomposition,
        task_match_result=match_result_dict,
        gate1_shown_at=now,
        state="AWAITING_GATE1",
    )

    try:
        telemetry.log_gate1_preview_shown(
            sid,
            step_count=step_count,
            decomposition_json=decomposition,
            task_match_result=match_result_dict,
            workflow_summary=preview_text[:200],   # truncate for log size
        )
    except Exception as e:
        print(f"  [/plan] telemetry.log_gate1_preview_shown failed: {e}")

    return PlanResponse(
        session_id=sid,
        decomposition=decomposition,
        task_match_result=match_result_dict,
        preview=preview_text,
        step_count=step_count,
        state="AWAITING_GATE1",
    )


@app.post("/build-activities", response_model=BuildActivitiesResponse)
async def build_activities(req: BuildActivitiesRequest) -> BuildActivitiesResponse:
    """Phase 2b — deterministic middle. User has approved Gate 1.
    Fires gate1_approved on entry, gate2_preview_shown on success."""
    if _store is None:
        raise HTTPException(500, "session store not initialized")

    entry = _store.get(req.session_id)
    if entry is None:
        raise HTTPException(404, f"session {req.session_id} not found")

    if not entry.get("decomposition"):
        raise HTTPException(409, {
            "error":   "missing_decomposition",
            "message": "Run POST /plan first.",
        })

    sid = req.session_id

    # ---- gate1_approved --------------------------------------------------
    try:
        telemetry.log_gate1_approved(
            sid,
            time_to_decision_sec=_time_since(entry.get("gate1_shown_at")),
            retry_count=int(entry.get("retry_count_stage1", 0)),
        )
    except Exception as e:
        print(f"  [/build-activities] telemetry.log_gate1_approved failed: {e}")

    # ---- run BuildActivitiesPipeline -------------------------------------
    initial_state = {
        "prompt":               entry.get("prompt", ""),
        "decomposition":        entry["decomposition"],
        "telemetry_session_id": sid,
    }

    try:
        result_state = await _run_pipeline_with_state(
            build_build_activities_pipeline(),
            entry.get("prompt", ""),
            initial_state,
            run_label="build",
        )
    except Exception as e:
        print(f"  [/build-activities] pipeline raised: {e}")
        raise HTTPException(500, f"build pipeline failed: {e}")

    # ---- check for failure -----------------------------------------------
    output_failed = _ensure_dict(result_state.get("output_result", {}))
    if output_failed.get("status") == "failed":
        raise HTTPException(502, {
            "error":   "build_failed",
            "message": "Deterministic middle stage failed.",
            "errors":  output_failed.get("errors", []),
        })

    enriched_workflow = _ensure_dict(result_state.get("enriched_workflow", {}))
    activity_manifest = result_state.get("activity_manifest", []) or []
    pattern_match     = _ensure_dict(result_state.get("pattern_match", {}))
    placed_skeleton   = _ensure_dict(result_state.get("placed_skeleton", {}))

    if not enriched_workflow:
        raise HTTPException(502, {
            "error":   "enrichment_empty",
            "message": "Deterministic middle produced no enriched_workflow.",
        })

    # ---- persist + emit gate2_preview_shown ------------------------------
    activity_count = _activity_count(enriched_workflow)
    now = _now()
    _store.update(
        sid,
        enriched_workflow=enriched_workflow,
        activity_manifest=activity_manifest,
        pattern_match=pattern_match,
        placed_skeleton=placed_skeleton,
        gate2_shown_at=now,
        state="AWAITING_GATE2",
    )

    # Build a compact activity_list for the gate2 telemetry payload
    activity_list_for_event = []
    raw = enriched_workflow.get("workflow_raw_data", {}) if isinstance(enriched_workflow, dict) else {}
    for xname, act in raw.items():
        if isinstance(act, dict):
            activity_list_for_event.append({
                "xName":           xname,
                "CustomTypeName":  act.get("CustomTypeName", ""),
                "DisplayName":     act.get("DisplayName", ""),
            })

    try:
        telemetry.log_gate2_preview_shown(
            sid,
            activity_list=activity_list_for_event,
            data_flow=None,
            placeholders=[],
            warnings=[],
            mermaid_snapshot=None,   # populated when Phase Mermaid ships
            task_groupings=None,
        )
    except Exception as e:
        print(f"  [/build-activities] telemetry.log_gate2_preview_shown failed: {e}")

    return BuildActivitiesResponse(
        session_id=sid,
        enriched_workflow=enriched_workflow,
        activity_manifest=activity_manifest,
        activity_count=activity_count,
        state="AWAITING_GATE2",
    )


@app.post("/generate-artifacts", response_model=GenerateArtifactsResponse)
async def generate_artifacts(req: GenerateArtifactsRequest) -> GenerateArtifactsResponse:
    """Phase 3 — Wirer + post-Wirer + retry.

    Fires gate2_approved on entry. On Wirer truncation or validation failure,
    runs the existing CorrectionPipeline as attempt 2 (mirrors main.py's
    retry decision tree). Generates a UUID v4 tracking_token on success and
    fires session_complete."""
    if _store is None:
        raise HTTPException(500, "session store not initialized")

    entry = _store.get(req.session_id)
    if entry is None:
        raise HTTPException(404, f"session {req.session_id} not found")

    for required in ("decomposition", "enriched_workflow", "activity_manifest"):
        if not entry.get(required):
            raise HTTPException(409, {
                "error":   f"missing_{required}",
                "message": "Run POST /build-activities first.",
            })

    sid = req.session_id
    session_start_time = entry.get("created_at", _now())
    total_llm_calls = 1   # decomposer ran in /plan; wirer about to run

    # ---- gate2_approved --------------------------------------------------
    try:
        telemetry.log_gate2_approved(
            sid,
            time_to_decision_sec=_time_since(entry.get("gate2_shown_at")),
            retry_count=int(entry.get("retry_count_stage2", 0)),
        )
    except Exception as e:
        print(f"  [/generate-artifacts] telemetry.log_gate2_approved failed: {e}")

    # ---- Attempt 1: ArtifactsPipeline ------------------------------------
    initial_state = {
        "prompt":               entry.get("prompt", ""),
        "decomposition":        entry["decomposition"],
        "enriched_workflow":    entry["enriched_workflow"],
        "activity_manifest":    entry["activity_manifest"],
        "pattern_match":        entry.get("pattern_match", {}),
        "placed_skeleton":      entry.get("placed_skeleton", {}),
        "telemetry_session_id": sid,
    }

    try:
        result_state = await _run_pipeline_with_state(
            build_artifacts_pipeline(),
            entry.get("prompt", ""),
            initial_state,
            run_label="artifacts",
        )
        total_llm_calls += 1
    except Exception as e:
        print(f"  [/generate-artifacts] attempt 1 raised: {e}")
        _emit_session_complete(sid, session_start_time, "failed_after_retry", total_llm_calls)
        raise HTTPException(500, f"artifacts pipeline failed: {e}")

    output_result = _ensure_dict(result_state.get("output_result", {}))
    needs_retry, retry_reason = _check_retryable_failure(result_state)
    retried = False
    final_retry_reason: str | None = None

    # Success signal: output_result has a non-empty 'output_file' key.
    # Failure signal: output_result.status == "failed" (or no output_result at all).
    # See tools/output_tools.py:run_output — success returns the format_json_output
    # dict augmented with output_file; failure dicts always set status="failed".
    attempt_succeeded = bool(output_result.get("output_file")) and output_result.get("status") != "failed"

    # ---- Attempt 2: CorrectionPipeline (if needed) ----------------------
    if needs_retry and not attempt_succeeded:
        retried = True
        final_retry_reason = retry_reason
        print(f"  [/generate-artifacts] retrying via correction pipeline: {retry_reason}")

        # Build the correction message — mirrors main.py's prefix
        validation_errors = (
            _ensure_dict(result_state.get("output_result", {})).get("errors")
            or _ensure_dict(result_state.get("validation_result", {})).get("errors")
            or []
        )
        prior_workflow_json = _ensure_dict(result_state.get("workflow_json", {}))
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

        original_prompt   = entry.get("original_prompt") or entry.get("prompt", "")
        error_summary     = "\n".join(f"- {e}" for e in validation_errors) or retry_reason
        correction_prompt = (
            f"CORRECTION REQUIRED — The previous attempt produced a workflow with errors "
            f"that prevented it from being imported. Fix ALL of the following errors. "
            f"Do not reproduce any of these errors:\n\n"
            f"{error_summary}"
            f"{prior_wf_str}\n\n"
            f"Original workflow request: {original_prompt}"
        )

        correction_state = {
            "prompt":               original_prompt,
            "decomposition":        entry["decomposition"],
            "placed_skeleton":      entry.get("placed_skeleton", {}),
            "telemetry_session_id": sid,
        }

        try:
            result_state = await _run_pipeline_with_state(
                build_correction_pipeline(),
                correction_prompt,
                correction_state,
                run_label="artifacts_corr",
            )
            total_llm_calls += 1
        except Exception as e:
            print(f"  [/generate-artifacts] attempt 2 raised: {e}")
            try:
                telemetry.log_correction_fired(
                    sid, reason=retry_reason, errors=validation_errors,
                    resolved_after_correction=False,
                )
            except Exception:
                pass
            _emit_session_complete(sid, session_start_time, "failed_after_retry", total_llm_calls)
            raise HTTPException(500, f"correction pipeline failed: {e}")

        output_result = _ensure_dict(result_state.get("output_result", {}))
        attempt_succeeded = bool(output_result.get("output_file")) and output_result.get("status") != "failed"
        try:
            telemetry.log_correction_fired(
                sid, reason=retry_reason, errors=validation_errors,
                resolved_after_correction=attempt_succeeded,
            )
        except Exception as e:
            print(f"  [/generate-artifacts] telemetry.log_correction_fired failed: {e}")

        _store.update(sid, retry_count_stage2=int(entry.get("retry_count_stage2", 0)) + 1)

    # ---- final outcome ---------------------------------------------------
    if not attempt_succeeded:
        _emit_session_complete(sid, session_start_time, "failed_after_retry", total_llm_calls)
        raise HTTPException(502, {
            "error":         "generation_failed",
            "message":       "Workflow generation failed after retry.",
            "errors":        output_result.get("errors", []) or _ensure_dict(result_state.get("validation_result", {})).get("errors", []),
            "retried":       retried,
            "retry_reason":  final_retry_reason,
        })

    # output_file is present. Extract and return.
    output_file_path = output_result.get("output_file") or ""
    if not output_file_path:
        # Defensive — attempt_succeeded guarantees this is non-empty, but
        # belt-and-suspenders for any future change to the success contract.
        _emit_session_complete(sid, session_start_time, "failed_after_retry", total_llm_calls)
        raise HTTPException(500, {
            "error":   "missing_output_file",
            "message": "Pipeline reported success but produced no output file path.",
        })

    output_filename = pathlib.Path(output_file_path).name

    # ---- generate tracking token + persist -------------------------------
    annotation_result = _ensure_dict(result_state.get("annotation_result", {}))
    validation_result = _ensure_dict(result_state.get("validation_result", {}))
    workflow_json     = _ensure_dict(result_state.get("workflow_json", {}))

    token = _store.register_tracking_token(sid, output_filename=output_filename)
    if token is None:
        # Should never happen — session was just validated to exist
        raise HTTPException(500, "failed to register tracking token")

    _store.update(
        sid,
        workflow_json=workflow_json,
        annotation_result=annotation_result,
        validation_result=validation_result,
        output_result=output_result,
        gate3_shown_at=_now(),
        state="AWAITING_GATE3",
    )

    # ---- success: emit session_complete ----------------------------------
    _emit_session_complete(
        sid, session_start_time,
        "success_after_retry" if retried else "success",
        total_llm_calls,
    )

    update_items, verify_items = _split_placeholders(annotation_result)
    summary = _build_chat_summary(output_filename, validation_result, annotation_result)

    return GenerateArtifactsResponse(
        session_id=sid,
        tracking_token=token,
        output_filename=output_filename,
        download_url=f"/download/{output_filename}",
        summary=summary,
        retried=retried,
        retry_reason=final_retry_reason,
        placeholders=update_items,
        verify_notes=verify_items,
        state="AWAITING_GATE3",
    )


@app.get("/download/{filename}")
async def download(filename: str):
    """Serve a generated workflow JSON. Fires xml_downloaded.

    Filename validation: must be a bare filename (no path traversal) and
    must already exist in OUTPUT_DIR. We do NOT require the caller to
    pass a session_id or tracking_token — this endpoint is called via
    a direct link the frontend renders, and the filename itself is
    obscure enough (workflow_NNNNN_<random>.json from output_tools).

    However, we DO try to associate it back to a session for telemetry
    by scanning the tracking-token index. session_id in the event payload
    will be null if no token references this file."""
    if _store is None:
        raise HTTPException(500, "session store not initialized")

    # Path traversal guard
    if "/" in filename or "\\" in filename or filename.startswith("."):
        raise HTTPException(400, "invalid filename")

    file_path = OUTPUT_DIR / filename
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(404, f"{filename} not found")

    # Find the tracking token + session for telemetry, if any
    tracking_token = ""
    session_id     = None
    for tok, ref in list(_store._tokens.items()):
        if ref.get("output_filename") == filename:
            tracking_token = tok
            session_id     = ref.get("session_id")
            break

    file_size = file_path.stat().st_size
    try:
        telemetry.log_xml_downloaded(
            filename=filename,
            tracking_token=tracking_token or "unknown",
            file_size_bytes=file_size,
            session_id=session_id,
        )
    except Exception as e:
        print(f"  [/download] telemetry.log_xml_downloaded failed: {e}")

    if session_id:
        _store.register_activity(session_id)

    return FileResponse(
        path=str(file_path),
        media_type="application/json",
        filename=filename,
    )


@app.post("/outcome/{tracking_token}", response_model=OutcomeResponse)
async def outcome(tracking_token: str, req: OutcomeRequest) -> OutcomeResponse:
    """Record post-import outcome. Fires outcome_reported (via log_outcome,
    which writes to BOTH the outcomes log and the events log)."""
    if _store is None:
        raise HTTPException(500, "session store not initialized")

    ref = _store.lookup_tracking_token(tracking_token)
    if ref is None:
        raise HTTPException(404, f"tracking_token {tracking_token} not found")

    try:
        telemetry.log_outcome(tracking_token, worked=req.worked, notes=req.notes)
    except Exception as e:
        print(f"  [/outcome] telemetry.log_outcome failed: {e}")
        raise HTTPException(500, f"failed to log outcome: {e}")

    sid = ref.get("session_id")
    if sid:
        _store.update(sid, state="DONE")

    return OutcomeResponse(tracking_token=tracking_token, recorded=True)


# ---------------------------------------------------------------------------
# Helpers — session_complete emission
# ---------------------------------------------------------------------------

def _emit_session_complete(sid: str, started_at: float,
                           final_state: str, total_llm_calls: int) -> None:
    """Emit session_complete with computed total_duration_sec. Called at
    every terminal exit path of /generate-artifacts (success and failure).
    Does NOT delete the session — frontend may still call /download or
    /outcome for it. Sweeper handles eventual abandon/expiry."""
    try:
        telemetry.log_session_complete(
            sid,
            total_duration_sec=round(time.time() - started_at, 2),
            final_state=final_state,
            total_llm_calls=total_llm_calls,
        )
    except Exception as e:
        print(f"  [api] telemetry.log_session_complete failed: {e}")


# ---------------------------------------------------------------------------
# Module entry point — for `python api.py` convenience
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    host = os.getenv("HOST", "0.0.0.0")
    print(f"[api] starting uvicorn on {host}:{port}")
    uvicorn.run(app, host=host, port=port)