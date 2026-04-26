"""
agents/pipeline.py

WorkflowPipeline — BaseAgent subclass orchestrating all stages.

STAGE MAP:
  Stage 1     LLM   DecomposerAgent      → session state: decomposition
  Stage 2     PY    run_pattern_match    → session state: pattern_match
  Stage 3     PY    run_retrieval        → session state: activity_manifest
  Stage 4a    PY    run_skeleton_builder → session state: placed_skeleton
  Stage 4b    PY    run_enrichment       → session state: enriched_workflow
  Stage 4b.5  PY    _backfill_table_vars → mutates enriched_workflow in place
  Stage 4c    PY    run_fragments        → session state: enriched_workflow
  Stage 4c.5  PY    run_content_scaffold → session state: enriched_workflow
  Stage 4c.6  PY    run_wiring           → session state: enriched_workflow
  Stage 4d    LLM   WirerAgent           → session state: workflow_json
  Stage 4f    PY    run_fragments+scaffold → session state: workflow_json
  Stage 4f.5  PY    repair_workflow      → session state: workflow_json
  Stage 4g    PY    run_cleanup          → session state: workflow_json
  Stage 5     PY    run_annotation       → session state: annotation_result
  Stage 6     PY    run_validation       → session state: validation_result
  Stage 7     PY    run_output           → json_files/<n>.json
  Stage 8     PY    write_mermaid        → json_files/<n>.mmd  (non-fatal)

DETERMINISTIC STAGES (3, 4a-4c.6):
  By Stage 4c.6 the workflow has correct structure, template defaults,
  TableName backfilled, F1-F9 enforced, scaffold rules applied, and
  authoritative wiring from wiring_map.json. Wirer fills only semantics:
  descriptions, variable refs not covered by wiring_map, email/query content.

POST-WIRER REPAIR (Stage 4f.5):
  The Wirer occasionally overwrites enrichment-seeded enum values with
  hallucinated alternatives, or drops required fields entirely. The repair
  pass clamps out-of-enum values to the first allowed value, restores
  missing fields from corpus defaults, and annotates fields with no
  deterministic source as UPDATE BEFORE RUNNING. Runs after Stage 4f
  scaffold (so the scaffold-injected baseline is included) and before
  Stage 4g cleanup.

VISUALIZATION (Stage 8):
  After Stage 7 writes the JSON, write_mermaid emits a sibling .mmd file
  using task subgraphs derived from data/task_taxonomy.json. NEVER blocks
  the pipeline — failures are logged and execution continues. The .mmd
  path is stashed in session state as "mermaid_file" for api.py / frontend
  consumption.

MODEL CONFIGURATION:
  _model_decomposer()  Flash  temp=0.1  top_p=0.8  max_tokens=4096
  _model_wirer()       Pro    temp=0.1  top_p=0.7  max_tokens=16384

RETRY ARCHITECTURE:
  On validation failure, main.py calls build_correction_pipeline().
  CorrectionPipeline skips DecomposerAgent. Reruns all deterministic stages
  from decomposition (persists as LlmAgent output_key). WirerAgent receives
  CORRECTION REQUIRED prompt with embedded workflow_json.

ADK STATE NOTE:
  LlmAgent output_key values persist through get_session().
  Python-stage mutations do NOT persist through get_session().
  decomposition and workflow_json survive retry; activity_manifest and
  enriched_workflow must be recomputed.

TELEMETRY (Phase E):
  ctx.session.state["telemetry_session_id"] is read at the top of each
  pipeline run. main.py is responsible for setting it before invoking
  the runner. When unset, the literal string "unknown" is used so events
  still log (joinable later via the prompt or session timestamps).

  Events emitted per successful run:
    decomposer_call         once after Stage 1 (Path B: mode="freeform_fallback")
    deterministic_middle    once after Stage 4c.6 with cumulative duration
    wirer_call              once after Stage 4d
    validation_result       once after Stage 6 (status=valid|invalid)

  Errors on fatal stage failures emit log_error which writes both a
  detailed errors record and a generation_failed event referencing
  the state dump.
"""

import os
import re
import time
from typing import AsyncGenerator

from google.adk.agents import BaseAgent, LlmAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from google.adk.models.lite_llm import LiteLlm

from agents.decomposer_agent import INSTRUCTION as DECOMPOSER_INSTRUCTION
from agents.wirer_agent import INSTRUCTION as WIRER_INSTRUCTION

from tools import telemetry
from tools.decompose_tools import assess_complexity, decompose_workflow, estimate_activity_count
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
    normalize_wirer_output,
    run_cleanup,
    run_annotation,
    run_validation,
)
from tools.output_tools import run_output
from tools.annotation_tools import _ensure_dict
from tools.post_wirer_repair import repair_workflow
from tools.visualize import write_mermaid


# ---------------------------------------------------------------------------
# Model factories
# ---------------------------------------------------------------------------

def _model_decomposer() -> LiteLlm:
    return LiteLlm(
        model=os.getenv("MODEL_FAST", "gemini/gemini-2.5-flash"),
        temperature=0.1,
        top_p=0.8,
        max_tokens=4096,
        api_key=os.getenv("GOOGLE_API_KEY"),
    )


def _model_wirer() -> LiteLlm:
    return LiteLlm(
        model=os.getenv("MODEL", "gemini/gemini-2.5-pro"),
        temperature=0.1,
        top_p=0.7,
        max_tokens=16384,
        api_key=os.getenv("GOOGLE_API_KEY"),
    )


# ---------------------------------------------------------------------------
# Telemetry helpers
# ---------------------------------------------------------------------------

def _sid(ctx: InvocationContext) -> str:
    """Pulls the telemetry session_id out of ADK session state. Falls back
    to a literal so events still emit when main.py forgot to set it."""
    return str(ctx.session.state.get("telemetry_session_id") or "unknown")


def _log_fatal(ctx: InvocationContext, stage: str, exception: Exception) -> None:
    """Emit a structured error + generation_failed event for a fatal stage
    failure. State dump is included so the failure can be reproduced."""
    try:
        telemetry.log_error(
            stage=stage,
            error_type=type(exception).__name__,
            error_message=str(exception),
            session_id=_sid(ctx),
            state=dict(ctx.session.state),
            exception=exception,
        )
    except Exception as telem_err:
        # Telemetry itself should never break the pipeline. Print and continue.
        print(f"  [pipeline] telemetry.log_error failed (swallowed): {telem_err}")


# ---------------------------------------------------------------------------
# Activity count guard
# ---------------------------------------------------------------------------

def _count_top_level_activities(workflow: dict) -> int:
    raw = workflow.get("workflow_raw_data", workflow) if isinstance(workflow, dict) else {}
    return sum(1 for v in raw.values() if isinstance(v, dict))


def _check_activity_count(workflow_json: dict, enriched_workflow: dict) -> str | None:
    expected = _count_top_level_activities(enriched_workflow)
    actual   = _count_top_level_activities(workflow_json)
    if expected == 0:
        return None
    if actual == 0:
        return f"WirerAgent returned 0 activities (expected ~{expected})."
    ratio = actual / expected
    if ratio < 0.70:
        return (
            f"WirerAgent truncated: returned {actual} top-level activities, "
            f"expected at least {int(expected * 0.70)} (70% of {expected})."
        )
    print(f"  [pipeline] activity count ok — {actual}/{expected} top-level activities")
    return None


# ---------------------------------------------------------------------------
# Shared post-wirer stages (Stages 4f, 4f.5, 4g, 5, 6, 7, 8)
# ---------------------------------------------------------------------------

async def _run_post_wirer_stages(ctx: InvocationContext, activity_manifest: list):
    """
    Runs Stages 4f-8 after WirerAgent completes.

    Normalizes Wirer output (Children-list → xName-keyed, metadata strip,
    TableName backfill), then runs fragments/scaffold/repair/cleanup/
    annotation/validation/output/visualization. No fallback to
    enriched_workflow — if Wirer returns empty the outer retry fires and
    CorrectionPipeline reruns with a better prompt.
    """
    sid = _sid(ctx)

    _raw_wj = ctx.session.state.get("workflow_json")
    workflow_json = _ensure_dict(_raw_wj)

    if not workflow_json:
        msg = (f"WirerAgent output empty — type={type(_raw_wj).__name__} "
               f"len={len(str(_raw_wj)) if _raw_wj is not None else 0}")
        print(f"  [pipeline] {msg}")
        ctx.session.state["_empty_response_error"] = True
        ctx.session.state["output_result"] = {
            "status": "failed",
            "errors": ["WirerAgent returned empty or unparseable output."],
        }
        _log_fatal(ctx, "wirer_output", RuntimeError(msg))
        return

    # Normalize Wirer output — fast no-op for clean xName-keyed responses.
    try:
        workflow_json = normalize_wirer_output(workflow_json)
        ctx.session.state["workflow_json"] = workflow_json
    except Exception as e:
        print(f"  [pipeline] normalize failed: {e}")
        _log_fatal(ctx, "normalize", e)
        ctx.session.state["output_result"] = {
            "status": "failed",
            "errors": [f"Wirer output normalization failed: {e}"],
        }
        return

    raw_data = workflow_json.get("workflow_raw_data", {})
    n_dicts  = sum(1 for v in raw_data.values() if isinstance(v, dict))
    print(f"  [pipeline] workflow_json ok — {n_dicts} activity dicts in workflow_raw_data")

    # Activity count guard
    enriched_workflow = _ensure_dict(ctx.session.state.get("enriched_workflow", {}))
    truncation_error  = _check_activity_count(workflow_json, enriched_workflow)
    if truncation_error:
        print(f"  [pipeline] activity count guard: {truncation_error}")
        ctx.session.state["output_result"] = {
            "status": "failed",
            "errors": [truncation_error],
        }
        _log_fatal(ctx, "activity_count_guard", RuntimeError(truncation_error))
        return

    # Stage 4f: Re-apply fragments + scaffold on Wirer output (idempotent)
    try:
        workflow_json = run_fragments(workflow_json)
        ctx.session.state["workflow_json"] = workflow_json
        print(f"  [pipeline] stage 4f fragments ok — "
              f"{len(workflow_json.get('workflow_raw_data', {}))} activities")
    except Exception as e:
        print(f"  [pipeline] stage 4f fragments failed (non-fatal): {e}")

    try:
        workflow_json = run_content_scaffold(workflow_json)
        ctx.session.state["workflow_json"] = workflow_json
        print(f"  [pipeline] stage 4f scaffold ok")
    except Exception as e:
        print(f"  [pipeline] stage 4f scaffold failed (non-fatal): {e}")

    # Stage 4f.5: Post-Wirer repair (clamp / restore / annotate)
    # Catches enum violations the Wirer introduced, restores dropped required
    # fields from corpus defaults, and annotates anything we can't fix.
    try:
        workflow_json, repair_log = repair_workflow(workflow_json)
        ctx.session.state["workflow_json"] = workflow_json
        if repair_log:
            print(f"  [pipeline] stage 4f.5 repair: {len(repair_log)} change(s)")
            for entry in repair_log:
                print(entry)
        else:
            print(f"  [pipeline] stage 4f.5 repair: no changes needed")
    except Exception as e:
        print(f"  [pipeline] stage 4f.5 repair failed (non-fatal): {e}")

    # Stage 4g: Post-Wirer cleanup
    try:
        workflow_json = run_cleanup(workflow_json)
        ctx.session.state["workflow_json"] = workflow_json
    except Exception as e:
        print(f"  [pipeline] stage 4g cleanup failed (non-fatal): {e}")

    # Stage 5: Annotate
    try:
        annotation_result = run_annotation(workflow_json, activity_manifest)
        ctx.session.state["annotation_result"] = annotation_result
        n_notes = len(annotation_result.get("placeholder_summary", []))
        print(f"  [pipeline] annotation ok — {n_notes} placeholder/verify items")
    except Exception as e:
        print(f"  [pipeline] annotation failed: {e}")
        _log_fatal(ctx, "annotation", e)
        ctx.session.state["output_result"] = {
            "status": "failed",
            "errors": [f"Annotation failed: {e}"],
        }
        return

    # Stage 6: Validate
    try:
        validation_result = run_validation(annotation_result)
        ctx.session.state["validation_result"] = validation_result
        print(f"  [pipeline] validation: {validation_result['status']}")
        if validation_result["errors"]:
            for err in validation_result["errors"]:
                print(f"    - {err}")
    except Exception as e:
        print(f"  [pipeline] validation failed: {e}")
        _log_fatal(ctx, "validation", e)
        ctx.session.state["output_result"] = {
            "status": "failed",
            "errors": [f"Validation failed: {e}"],
        }
        return

    # Emit validation outcome event regardless of status (valid OR invalid).
    # invalid is intentional retry signal, not an unexpected error, so no
    # log_error emitted — log_validation_result captures the failure shape.
    try:
        telemetry.log_validation_result(
            sid,
            status=validation_result.get("status", "unknown"),
            errors=validation_result.get("errors", []),
            verify_notes=validation_result.get("verify_notes", []),
        )
    except Exception as telem_err:
        print(f"  [pipeline] telemetry.log_validation_result failed: {telem_err}")

    if validation_result["status"] == "invalid":
        print(f"  [pipeline] validation failed — returning for outer retry")
        ctx.session.state["output_result"] = {
            "status": "failed",
            "errors": validation_result.get("errors", []),
        }
        return

    # Stage 7: Output
    try:
        prompt = ctx.session.state.get("prompt", "Workflow")
        output_result = run_output(validation_result, _derive_base_name(prompt))
        ctx.session.state["output_result"] = output_result
        print(f"  [pipeline] output written: {output_result.get('output_file')}")
    except Exception as e:
        print(f"  [pipeline] output stage failed: {e}")
        _log_fatal(ctx, "output", e)
        ctx.session.state["output_result"] = {
            "status": "failed",
            "errors": [f"Output stage failed: {e}"],
        }
        return

    # Stage 8: Mermaid visualization
    # Generates a sibling .mmd file alongside the workflow JSON for the
    # frontend (Phase H) and for human inspection. NEVER blocks the
    # pipeline — failures here are logged but do not affect output_result.
    output_file = (output_result.get("output_file")
                   if isinstance(output_result, dict) else None)
    if output_file:
        try:
            decomposition = _ensure_dict(ctx.session.state.get("decomposition"))
            mmd_path = write_mermaid(workflow_json, decomposition, output_file)
            if mmd_path:
                ctx.session.state["mermaid_file"] = mmd_path
                print(f"  [pipeline] mermaid written: {mmd_path}")
            else:
                print(f"  [pipeline] mermaid skipped (renderer returned empty)")
        except Exception as e:
            print(f"  [pipeline] mermaid stage failed (non-fatal): {e}")


# ---------------------------------------------------------------------------
# Full pipeline (Attempt 1)
# ---------------------------------------------------------------------------

class WorkflowPipeline(BaseAgent):

    decomposer: LlmAgent
    wirer:      LlmAgent

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
            print(f"  [pipeline] telemetry.log_decomposer_call failed: {telem_err}")

        raw = ctx.session.state.get("decomposition")
        print(f"  [pipeline] decomposition raw type: {type(raw).__name__}")
        decomposition = _ensure_dict(raw)

        if not decomposition:
            print("  [pipeline] decomposition is empty — aborting")
            ctx.session.state["_empty_response_error"] = True
            ctx.session.state["output_result"] = {
                "status": "failed",
                "errors": ["DecomposerAgent returned empty or unparseable output."],
            }
            _log_fatal(ctx, "decomposer_output",
                       RuntimeError("DecomposerAgent returned empty output."))
            return

        # Begin deterministic middle timing (Stages 2 through 4c.6)
        middle_start = time.time()
        middle_warnings: list = []

        # Stage 2: Pattern match
        try:
            pattern_match = run_pattern_match(decomposition)
            ctx.session.state["pattern_match"] = pattern_match
            print(f"  [pipeline] pattern_match: {pattern_match.get('match_status')}")
        except Exception as e:
            print(f"  [pipeline] pattern_match failed (non-fatal): {e}")
            middle_warnings.append(f"pattern_match: {e}")

        # Stage 3: Retrieve
        try:
            activity_manifest = run_retrieval(decomposition)
            ctx.session.state["activity_manifest"] = activity_manifest
            print(f"  [pipeline] retrieval ok — {len(activity_manifest)} entries")
        except Exception as e:
            print(f"  [pipeline] retrieval failed: {e}")
            _log_fatal(ctx, "retrieval", e)
            ctx.session.state["output_result"] = {
                "status": "failed",
                "errors": [f"Retrieval failed: {e}"],
            }
            return

        # Stage 4a: Skeleton builder (deterministic, replaces PlacerAgent)
        try:
            placed_skeleton = run_skeleton_builder(decomposition, activity_manifest)
            ctx.session.state["placed_skeleton"] = placed_skeleton
            print(f"  [pipeline] placed_skeleton ok — "
                  f"{len(placed_skeleton.get('workflow_raw_data', {}))} activities placed")
        except Exception as e:
            print(f"  [pipeline] skeleton builder failed: {e}")
            _log_fatal(ctx, "skeleton_builder", e)
            ctx.session.state["output_result"] = {
                "status": "failed",
                "errors": [f"Skeleton builder failed: {e}"],
            }
            return

        # Stage 4b: Enrich
        try:
            enriched_workflow = run_enrichment(placed_skeleton, activity_manifest)
            ctx.session.state["enriched_workflow"] = enriched_workflow
            print(f"  [pipeline] enrichment ok — "
                  f"{len(enriched_workflow.get('workflow_raw_data', {}))} activities enriched")
        except Exception as e:
            print(f"  [pipeline] enrichment failed: {e}")
            _log_fatal(ctx, "enrichment", e)
            ctx.session.state["output_result"] = {
                "status": "failed",
                "errors": [f"Enrichment failed: {e}"],
            }
            return

        # Stage 4b.5: Backfill table variable names before scaffold
        try:
            _backfill_table_vars(enriched_workflow)
        except Exception as e:
            print(f"  [pipeline] table var backfill failed (non-fatal): {e}")
            middle_warnings.append(f"table_backfill: {e}")

        # Stage 4c: Fragments
        try:
            enriched_workflow = run_fragments(enriched_workflow)
            ctx.session.state["enriched_workflow"] = enriched_workflow
            print(f"  [pipeline] fragments ok — "
                  f"{len(enriched_workflow.get('workflow_raw_data', {}))} activities")
        except Exception as e:
            print(f"  [pipeline] fragments failed: {e}")
            _log_fatal(ctx, "fragments", e)
            ctx.session.state["output_result"] = {
                "status": "failed",
                "errors": [f"Fragment application failed: {e}"],
            }
            return

        # Stage 4c.5: Content scaffold
        try:
            enriched_workflow = run_content_scaffold(enriched_workflow)
            ctx.session.state["enriched_workflow"] = enriched_workflow
            print(f"  [pipeline] scaffold ok — "
                  f"{len(enriched_workflow.get('workflow_raw_data', {}))} activities")
        except Exception as e:
            print(f"  [pipeline] scaffold failed (non-fatal): {e}")
            middle_warnings.append(f"scaffold: {e}")

        # Stage 4c.6: Deterministic wiring pass (wiring_map.json rules)
        try:
            enriched_workflow = run_wiring(enriched_workflow)
            ctx.session.state["enriched_workflow"] = enriched_workflow
            print(f"  [pipeline] wiring pass ok")
        except Exception as e:
            print(f"  [pipeline] wiring pass failed (non-fatal): {e}")
            middle_warnings.append(f"wiring: {e}")

        # End of deterministic middle — emit cumulative event
        try:
            telemetry.log_deterministic_middle(
                sid,
                duration_ms=round((time.time() - middle_start) * 1000, 1),
                stage_timings=None,  # boundary timing only; per-stage TODO
                warnings=middle_warnings,
            )
        except Exception as telem_err:
            print(f"  [pipeline] telemetry.log_deterministic_middle failed: {telem_err}")

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
            print(f"  [pipeline] telemetry.log_wirer_call failed: {telem_err}")

        # Stages 4f-8
        await _run_post_wirer_stages(ctx, activity_manifest)


# ---------------------------------------------------------------------------
# Correction pipeline (Attempt 2 on validation failure)
# ---------------------------------------------------------------------------

class CorrectionPipeline(BaseAgent):
    """
    Targeted retry pipeline. Skips DecomposerAgent entirely.
    Rebuilds all deterministic stages from decomposition (which persists
    as a LlmAgent output_key). WirerAgent receives the CORRECTION REQUIRED
    prompt with embedded workflow_json from attempt 1.
    """

    wirer: LlmAgent

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:

        sid = _sid(ctx)

        decomposition = _ensure_dict(ctx.session.state.get("decomposition"))

        if not decomposition:
            print("  [correction] decomposition missing — aborting")
            ctx.session.state["_empty_response_error"] = True
            ctx.session.state["output_result"] = {
                "status": "failed",
                "errors": ["CorrectionPipeline: decomposition not found in session state."],
            }
            _log_fatal(ctx, "correction_decomposition_missing",
                       RuntimeError("CorrectionPipeline: decomposition not found."))
            return

        print(f"  [correction] reusing decomposition "
              f"({len(decomposition.get('steps', []))} steps) — "
              f"rebuilding skeleton and enrichment deterministically")

        # Begin deterministic middle timing (correction skips Stage 1)
        middle_start = time.time()
        middle_warnings: list = []

        # Stage 2: Pattern match
        try:
            pattern_match = run_pattern_match(decomposition)
            ctx.session.state["pattern_match"] = pattern_match
            print(f"  [correction] pattern_match: {pattern_match.get('match_status')}")
        except Exception as e:
            print(f"  [correction] pattern_match failed (non-fatal): {e}")
            middle_warnings.append(f"pattern_match: {e}")

        # Stage 3: Retrieve
        try:
            activity_manifest = run_retrieval(decomposition)
            ctx.session.state["activity_manifest"] = activity_manifest
            print(f"  [correction] retrieval ok — {len(activity_manifest)} entries")
        except Exception as e:
            print(f"  [correction] retrieval failed: {e}")
            _log_fatal(ctx, "retrieval", e)
            ctx.session.state["output_result"] = {
                "status": "failed",
                "errors": [f"Correction retrieval failed: {e}"],
            }
            return

        # Stage 4a: Skeleton builder
        try:
            placed_skeleton = run_skeleton_builder(decomposition, activity_manifest)
            ctx.session.state["placed_skeleton"] = placed_skeleton
            print(f"  [correction] placed_skeleton ok — "
                  f"{len(placed_skeleton.get('workflow_raw_data', {}))} activities placed")
        except Exception as e:
            print(f"  [correction] skeleton builder failed: {e}")
            _log_fatal(ctx, "skeleton_builder", e)
            ctx.session.state["output_result"] = {
                "status": "failed",
                "errors": [f"Skeleton builder failed: {e}"],
            }
            return

        # Stage 4b: Enrich
        try:
            enriched_workflow = run_enrichment(placed_skeleton, activity_manifest)
            ctx.session.state["enriched_workflow"] = enriched_workflow
            print(f"  [correction] enrichment ok — "
                  f"{len(enriched_workflow.get('workflow_raw_data', {}))} activities")
        except Exception as e:
            print(f"  [correction] enrichment failed: {e}")
            _log_fatal(ctx, "enrichment", e)
            ctx.session.state["output_result"] = {
                "status": "failed",
                "errors": [f"Correction enrichment failed: {e}"],
            }
            return

        # Stage 4b.5: Backfill table variable names
        try:
            _backfill_table_vars(enriched_workflow)
        except Exception as e:
            print(f"  [correction] table var backfill failed (non-fatal): {e}")
            middle_warnings.append(f"table_backfill: {e}")

        # Stage 4c: Fragments
        try:
            enriched_workflow = run_fragments(enriched_workflow)
            ctx.session.state["enriched_workflow"] = enriched_workflow
            print(f"  [correction] fragments ok")
        except Exception as e:
            print(f"  [correction] fragments failed (non-fatal): {e}")
            middle_warnings.append(f"fragments: {e}")

        # Stage 4c.5: Content scaffold
        try:
            enriched_workflow = run_content_scaffold(enriched_workflow)
            ctx.session.state["enriched_workflow"] = enriched_workflow
            print(f"  [correction] scaffold ok")
        except Exception as e:
            print(f"  [correction] scaffold failed (non-fatal): {e}")
            middle_warnings.append(f"scaffold: {e}")

        # Stage 4c.6: Deterministic wiring pass
        try:
            enriched_workflow = run_wiring(enriched_workflow)
            ctx.session.state["enriched_workflow"] = enriched_workflow
            print(f"  [correction] wiring pass ok")
        except Exception as e:
            print(f"  [correction] wiring pass failed (non-fatal): {e}")
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
            print(f"  [correction] telemetry.log_deterministic_middle failed: {telem_err}")

        # Stage 4d: WirerAgent
        wirer_start = time.time()
        async for event in self.wirer.run_async(ctx):
            yield event
        try:
            telemetry.log_wirer_call(
                sid,
                duration_ms=round((time.time() - wirer_start) * 1000, 1),
            )
        except Exception as telem_err:
            print(f"  [correction] telemetry.log_wirer_call failed: {telem_err}")

        # Stages 4f-8
        await _run_post_wirer_stages(ctx, activity_manifest)


# ---------------------------------------------------------------------------
# Base name derivation
# ---------------------------------------------------------------------------

_STOP_WORDS = {
    "a", "an", "the", "and", "or", "for", "to", "in", "of", "with",
    "by", "at", "on", "from", "into", "that", "which", "when", "if",
    "create", "creates", "creating",
    "build", "builds", "building",
    "generate", "generates", "generating",
    "make", "makes", "making",
    "run", "runs", "running",
    "execute", "executes", "executing",
    "process", "processes", "processing",
    "workflow", "automation", "script",
    "each", "every", "all",
    "using", "use", "uses",
}


def _derive_base_name(prompt: str, max_words: int = 4) -> str:
    words = [
        w.capitalize()
        for w in re.split(r'\W+', prompt)
        if w.lower() not in _STOP_WORDS and w.isalpha() and len(w) > 2
    ][:max_words]
    return "".join(words) if words else "Workflow"


# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------

def build_pipeline() -> WorkflowPipeline:
    """Return a fresh WorkflowPipeline with per-agent optimised models."""
    decomposer = LlmAgent(
        name="DecomposerAgent",
        model=_model_decomposer(),
        instruction=DECOMPOSER_INSTRUCTION,
        tools=[assess_complexity, decompose_workflow, estimate_activity_count],
        output_key="decomposition",
        include_contents="none",
    )
    wirer = LlmAgent(
        name="WirerAgent",
        model=_model_wirer(),
        instruction=WIRER_INSTRUCTION,
        tools=[load_activity_template],
        output_key="workflow_json",
        include_contents="none",
    )
    return WorkflowPipeline(
        name="workflow_pipeline",
        decomposer=decomposer,
        wirer=wirer,
    )


def build_correction_pipeline() -> CorrectionPipeline:
    """Return a CorrectionPipeline for validation-failure retries."""
    wirer = LlmAgent(
        name="WirerAgent",
        model=_model_wirer(),
        instruction=WIRER_INSTRUCTION,
        tools=[load_activity_template],
        output_key="workflow_json",
        include_contents="none",
    )
    return CorrectionPipeline(
        name="correction_pipeline",
        wirer=wirer,
    )