"""
agents/api_pipelines.py

Phase G2 — split WorkflowPipeline into three gate-bounded pipelines that
api.py invokes one per HTTP endpoint.

PIPELINE BOUNDARIES
  PlanPipeline           — Stage 1 only (DecomposerAgent)
                           Outputs: decomposition
                           Endpoint: POST /plan

  BuildActivitiesPipeline — Stages 2 → 4c.6 (deterministic middle)
                           Inputs:  decomposition (seeded into ADK state)
                           Outputs: pattern_match, activity_manifest, enriched_workflow
                           Endpoint: POST /build-activities

  ArtifactsPipeline      — Stage 4d (Wirer) + post-Wirer (4f, 4f.5, 4g, 5, 6, 7)
                           Inputs:  decomposition, activity_manifest,
                                    enriched_workflow, pattern_match
                           Outputs: workflow_json, output_result
                           Endpoint: POST /generate-artifacts

CODE REUSE
  All three pipelines call the SAME stage functions from
  tools.pipeline_stages, tools.post_wirer_repair, tools.output_tools, etc.
  that the existing WorkflowPipeline / CorrectionPipeline use. The only
  thing new in this module is the BaseAgent subclasses that decide WHERE
  to start and stop. Stage logic lives where it always has.

  The post-Wirer chain is imported directly from agents.pipeline rather
  than reimplemented — _run_post_wirer_stages handles repair, validation,
  and output identically for CLI and HTTP paths.

ADK STATE SEEDING
  Each pipeline runs in its own InMemoryRunner with state seeded by api.py.
  Because LlmAgent output_keys persist through get_session() but Python-stage
  mutations do not (per agents/pipeline.py docstring), api.py extracts
  state from one pipeline's runner before it's destroyed and seeds the
  next pipeline's runner via create_session(state=...). The full ADK state
  for the full pipeline is reconstructed at each gate boundary.

CORRECTION HANDLING
  Validation/truncation retry inside ArtifactsPipeline mirrors main.py's
  retry path — the api.py orchestration layer detects failure in the
  result and dispatches the existing CorrectionPipeline. We do NOT embed
  retry inside ArtifactsPipeline itself because main.py needs it too and
  duplicating the retry decision tree in two places would diverge.

TELEMETRY
  Each pipeline reads telemetry_session_id from initial_state via _sid()
  (same helper as agents.pipeline). Per-stage events
  (decomposer_call, deterministic_middle, wirer_call, validation_result,
  generation_failed) are emitted from inside the stage functions — no
  changes from CLI behavior. Gate-level events
  (gate1_*, gate2_*, xml_downloaded, outcome_reported, session_complete)
  are emitted from api.py at the HTTP boundary.
"""

import time
from typing import AsyncGenerator

from google.adk.agents import BaseAgent, LlmAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event

from agents.decomposer_agent import INSTRUCTION as DECOMPOSER_INSTRUCTION
from agents.wirer_agent import INSTRUCTION as WIRER_INSTRUCTION

from agents.pipeline import (
    _model_decomposer,
    _model_wirer,
    _sid,
    _log_fatal,
    _run_post_wirer_stages,
)

from tools import telemetry
from tools.decompose_tools import (
    assess_complexity,
    decompose_workflow,
    estimate_activity_count,
)
from tools.build_tools import load_activity_template
from tools.pipeline_stages import (
    run_pattern_match,
    run_retrieval,
    run_skeleton_builder,
    run_enrichment,
    _backfill_table_vars,
    run_fragments,
    run_content_scaffold,
    run_wiring,
)
from tools.annotation_tools import _ensure_dict
from tools.result_capture import capture_result, _capture_key


# ---------------------------------------------------------------------------
# Pipeline 1: PlanPipeline — Stage 1 only (Decomposer)
# ---------------------------------------------------------------------------

class PlanPipeline(BaseAgent):
    """Runs DecomposerAgent and stops. Output is in session state under
    'decomposition' (LlmAgent output_key, persists through get_session)."""

    decomposer: LlmAgent

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:

        sid = _sid(ctx)

        # Stage 1: Decompose (LLM)
        decomposer_start = time.time()
        async for event in self.decomposer.run_async(ctx):
            yield event
        try:
            telemetry.log_decomposer_call(
                sid,
                duration_ms=round((time.time() - decomposer_start) * 1000, 1),
                mode="freeform_fallback",   # Path B: always freeform
            )
        except Exception as telem_err:
            print(f"  [plan_pipeline] telemetry.log_decomposer_call failed: {telem_err}")

        raw = ctx.session.state.get("decomposition")
        print(f"  [plan_pipeline] decomposition raw type: {type(raw).__name__}")
        decomposition = _ensure_dict(raw)

        if not decomposition:
            print("  [plan_pipeline] decomposition is empty — aborting")
            ctx.session.state["_empty_response_error"] = True
            ctx.session.state["output_result"] = {
                "status": "failed",
                "errors": ["DecomposerAgent returned empty or unparseable output."],
            }
            _log_fatal(ctx, "decomposer_output",
                       RuntimeError("DecomposerAgent returned empty output."))
            return

        # Mark plan stage as complete in session state. api.py reads this to
        # decide whether to advance to /build-activities.
        ctx.session.state["_plan_complete"] = True


# ---------------------------------------------------------------------------
# Pipeline 2: BuildActivitiesPipeline — Stages 2 to 4c.6
# ---------------------------------------------------------------------------

class BuildActivitiesPipeline(BaseAgent):
    """Runs the deterministic middle from decomposition forward. Requires
    'decomposition' to be present in session state at start."""

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:

        # This pipeline runs no LlmAgent — only deterministic Python stages.
        # ADK requires _run_async_impl to be an async generator, not a plain
        # coroutine. Without at least one `yield` statement anywhere in the
        # function body, Python treats it as a coroutine function and the
        # ADK runner raises "'coroutine' object has no attribute 'aclose'"
        # when it tries `async for event in agent.run_async(ctx)`.
        # The unreachable yield below is the standard idiom for declaring
        # an empty async generator while keeping the rest of the body as
        # straight-line code.
        if False:
            yield   # pragma: no cover — declares this an async generator

        sid = _sid(ctx)
        ckey = _capture_key(ctx)

        decomposition = _ensure_dict(ctx.session.state.get("decomposition"))
        if not decomposition:
            print("  [build_pipeline] decomposition missing — aborting")
            ctx.session.state["output_result"] = {
                "status": "failed",
                "errors": ["BuildActivitiesPipeline: decomposition not in session state."],
            }
            _log_fatal(ctx, "build_decomposition_missing",
                       RuntimeError("BuildActivitiesPipeline: decomposition not found."))
            capture_result(ckey, output_result=ctx.session.state["output_result"])
            return

        print(f"  [build_pipeline] reusing decomposition "
              f"({len(decomposition.get('steps', []))} steps)")

        # Begin deterministic middle timing
        middle_start = time.time()
        middle_warnings: list = []

        # Stage 2: Pattern match
        try:
            pattern_match = run_pattern_match(decomposition)
            ctx.session.state["pattern_match"] = pattern_match
            print(f"  [build_pipeline] pattern_match: {pattern_match.get('match_status')}")
        except Exception as e:
            print(f"  [build_pipeline] pattern_match failed (non-fatal): {e}")
            middle_warnings.append(f"pattern_match: {e}")

        # Stage 3: Retrieve
        try:
            activity_manifest = run_retrieval(decomposition)
            ctx.session.state["activity_manifest"] = activity_manifest
            print(f"  [build_pipeline] retrieval ok — {len(activity_manifest)} entries")
        except Exception as e:
            print(f"  [build_pipeline] retrieval failed: {e}")
            _log_fatal(ctx, "retrieval", e)
            ctx.session.state["output_result"] = {
                "status": "failed",
                "errors": [f"Retrieval failed: {e}"],
            }
            capture_result(ckey, output_result=ctx.session.state["output_result"])
            return

        # Stage 4a: Skeleton builder
        try:
            placed_skeleton = run_skeleton_builder(decomposition, activity_manifest)
            ctx.session.state["placed_skeleton"] = placed_skeleton
            print(f"  [build_pipeline] placed_skeleton ok — "
                  f"{len(placed_skeleton.get('workflow_raw_data', {}))} activities")
        except Exception as e:
            print(f"  [build_pipeline] skeleton builder failed: {e}")
            _log_fatal(ctx, "skeleton_builder", e)
            ctx.session.state["output_result"] = {
                "status": "failed",
                "errors": [f"Skeleton builder failed: {e}"],
            }
            capture_result(ckey, output_result=ctx.session.state["output_result"])
            return

        # Stage 4b: Enrich
        try:
            enriched_workflow = run_enrichment(placed_skeleton, activity_manifest)
            ctx.session.state["enriched_workflow"] = enriched_workflow
            print(f"  [build_pipeline] enrichment ok — "
                  f"{len(enriched_workflow.get('workflow_raw_data', {}))} activities")
        except Exception as e:
            print(f"  [build_pipeline] enrichment failed: {e}")
            _log_fatal(ctx, "enrichment", e)
            ctx.session.state["output_result"] = {
                "status": "failed",
                "errors": [f"Enrichment failed: {e}"],
            }
            capture_result(ckey, output_result=ctx.session.state["output_result"])
            return

        # Stage 4b.5: Backfill table variable names
        try:
            _backfill_table_vars(enriched_workflow)
        except Exception as e:
            print(f"  [build_pipeline] table var backfill failed (non-fatal): {e}")
            middle_warnings.append(f"table_backfill: {e}")

        # Stage 4c: Fragments
        try:
            enriched_workflow = run_fragments(enriched_workflow)
            ctx.session.state["enriched_workflow"] = enriched_workflow
            print(f"  [build_pipeline] fragments ok")
        except Exception as e:
            print(f"  [build_pipeline] fragments failed: {e}")
            _log_fatal(ctx, "fragments", e)
            ctx.session.state["output_result"] = {
                "status": "failed",
                "errors": [f"Fragment application failed: {e}"],
            }
            capture_result(ckey, output_result=ctx.session.state["output_result"])
            return

        # Stage 4c.5: Content scaffold
        try:
            enriched_workflow = run_content_scaffold(enriched_workflow)
            ctx.session.state["enriched_workflow"] = enriched_workflow
            print(f"  [build_pipeline] scaffold ok")
        except Exception as e:
            print(f"  [build_pipeline] scaffold failed (non-fatal): {e}")
            middle_warnings.append(f"scaffold: {e}")

        # Stage 4c.6: Deterministic wiring pass
        try:
            enriched_workflow = run_wiring(enriched_workflow)
            ctx.session.state["enriched_workflow"] = enriched_workflow
            print(f"  [build_pipeline] wiring pass ok")
        except Exception as e:
            print(f"  [build_pipeline] wiring pass failed (non-fatal): {e}")
            middle_warnings.append(f"wiring: {e}")

        # End of deterministic middle — emit cumulative event
        try:
            telemetry.log_deterministic_middle(
                sid,
                duration_ms=round((time.time() - middle_start) * 1000, 1),
                stage_timings=None,
                warnings=middle_warnings,
            )
        except Exception as telem_err:
            print(f"  [build_pipeline] telemetry.log_deterministic_middle failed: {telem_err}")

        ctx.session.state["_build_complete"] = True

        # Capture results for api.py to read after the runner exits.
        # See module docstring on _RESULTS for why this is needed instead of
        # ctx.session.state writes (which don't survive get_session()).
        capture_result(
            _capture_key(ctx),
            pattern_match=ctx.session.state.get("pattern_match"),
            activity_manifest=ctx.session.state.get("activity_manifest"),
            placed_skeleton=ctx.session.state.get("placed_skeleton"),
            enriched_workflow=ctx.session.state.get("enriched_workflow"),
            output_result=ctx.session.state.get("output_result"),
        )


# ---------------------------------------------------------------------------
# Pipeline 3: ArtifactsPipeline — Stage 4d (Wirer) + post-Wirer
# ---------------------------------------------------------------------------

class ArtifactsPipeline(BaseAgent):
    """Runs WirerAgent and the post-Wirer chain (repair, cleanup, annotation,
    validation, output). Requires decomposition, activity_manifest,
    enriched_workflow, pattern_match in session state at start.

    On Wirer truncation or validation failure, this pipeline returns the
    failure in output_result. api.py is responsible for invoking the
    existing CorrectionPipeline (from agents.pipeline) for retry — same
    retry logic as main.py uses, no duplication."""

    wirer: LlmAgent

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:

        sid  = _sid(ctx)
        ckey = _capture_key(ctx)

        # Sanity check — prerequisites must be in session state
        for required_key in ("decomposition", "enriched_workflow",
                             "activity_manifest"):
            if not ctx.session.state.get(required_key):
                msg = f"ArtifactsPipeline: {required_key} not in session state."
                print(f"  [artifacts_pipeline] {msg}")
                ctx.session.state["output_result"] = {
                    "status": "failed",
                    "errors": [msg],
                }
                _log_fatal(ctx, "artifacts_prereq_missing", RuntimeError(msg))
                capture_result(ckey, output_result=ctx.session.state["output_result"])
                return

        activity_manifest = ctx.session.state.get("activity_manifest", [])

        # Stage 4d: WirerAgent — semantic fills only
        wirer_start = time.time()
        async for event in self.wirer.run_async(ctx):
            yield event
        try:
            telemetry.log_wirer_call(
                sid,
                duration_ms=round((time.time() - wirer_start) * 1000, 1),
            )
        except Exception as telem_err:
            print(f"  [artifacts_pipeline] telemetry.log_wirer_call failed: {telem_err}")

        # Stages 4f, 4f.5, 4g, 5, 6, 7 — shared helper from agents.pipeline
        await _run_post_wirer_stages(ctx, activity_manifest)

        # Capture all post-Wirer outputs for api.py to read after the runner
        # exits. _run_post_wirer_stages writes these via Python state mutation
        # which doesn't survive get_session(); see _RESULTS docstring.
        capture_result(
            ckey,
            workflow_json=ctx.session.state.get("workflow_json"),
            annotation_result=ctx.session.state.get("annotation_result"),
            validation_result=ctx.session.state.get("validation_result"),
            output_result=ctx.session.state.get("output_result"),
            _empty_response_error=ctx.session.state.get("_empty_response_error"),
        )


# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------

def build_plan_pipeline() -> PlanPipeline:
    decomposer = LlmAgent(
        name="DecomposerAgent",
        model=_model_decomposer(),
        instruction=DECOMPOSER_INSTRUCTION,
        tools=[assess_complexity, decompose_workflow, estimate_activity_count],
        output_key="decomposition",
        include_contents="none",
    )
    return PlanPipeline(
        name="plan_pipeline",
        decomposer=decomposer,
    )


def build_build_activities_pipeline() -> BuildActivitiesPipeline:
    """Note: no LlmAgent here. The middle is fully deterministic, so the
    pipeline is just orchestration around stage functions."""
    return BuildActivitiesPipeline(
        name="build_activities_pipeline",
    )


def build_artifacts_pipeline() -> ArtifactsPipeline:
    wirer = LlmAgent(
        name="WirerAgent",
        model=_model_wirer(),
        instruction=WIRER_INSTRUCTION,
        tools=[load_activity_template],
        output_key="workflow_json",
        include_contents="none",
    )
    return ArtifactsPipeline(
        name="artifacts_pipeline",
        wirer=wirer,
    )