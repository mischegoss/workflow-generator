"""
agents/pipeline.py

WorkflowPipeline — BaseAgent subclass orchestrating all stages.

STAGE MAP:
  Stage 1    LLM   DecomposerAgent      → session state: decomposition
  Stage 2    PY    run_pattern_match    → session state: pattern_match
  Stage 3    PY    run_retrieval        → session state: activity_manifest
  Stage 4a   LLM   PlacerAgent          → session state: placed_skeleton
  Stage 4b   PY    run_enrichment       → session state: enriched_workflow
  Stage 4c   PY    run_fragments        → session state: enriched_workflow
  Stage 4c.5 PY    run_content_scaffold → session state: enriched_workflow
  Stage 4d   LLM   WirerAgent           → session state: workflow_json
  Stage 4f   PY    run_fragments        → session state: workflow_json (enforces
                                          invariants on activities Wirer added)
  Stage 5    PY    run_annotation       → session state: annotation_result
  Stage 6    PY    run_validation       → session state: validation_result
  Stage 7    PY    run_output           → json_files/<n>.json

WHY STAGE 4f EXISTS:
  PlacerAgent collapse — placing only 3 of 9 activities in a skeleton —
  means WirerAgent reconstructs the missing activities (ExitWhile, ReturnValues,
  nested IfElse branches) from scratch. Those reconstructed activities never
  passed through Stage 4c fragments, so F1-F9 invariants are not enforced on
  them. WirerAgent also adds spurious fields (LeftOperand, Operator,
  RightOperand on WhileActivity) that the platform rejects.

  Stage 4f re-runs run_fragments() on the Wirer output. apply_fragments() is
  idempotent — safe to call twice. It strips spurious container fields and
  enforces F1-F9 on everything Wirer produced, not just what Placer placed.

  Stage 4f is a bridge fix. Phase 3 (deterministic skeleton builder) eliminates
  the collapse entirely, at which point 4f becomes a no-op safety net.

RETRY ARCHITECTURE:
  On validation failure, main.py calls build_correction_pipeline().
  CorrectionPipeline skips DecomposerAgent and PlacerAgent entirely.
  Stages 3, 4b, 4c, 4c.5, and 4f are re-run deterministically.
  WirerAgent receives the CORRECTION REQUIRED prompt with embedded
  workflow_json from attempt 1.

ACTIVITY COUNT GUARD:
  After WirerAgent runs, _run_post_wirer_stages compares workflow_json
  activity count against enriched_workflow. If Wirer returned fewer than
  70% of expected top-level activities the output is rejected as truncation.

ADK STATE NOTE:
  LlmAgent output_key values persist through get_session().
  Python-stage ctx.session.state mutations do NOT persist through get_session().
  Therefore: decomposition, placed_skeleton, workflow_json (output_keys) survive
  retry. activity_manifest and enriched_workflow must be recomputed.
"""

import os
import re
from typing import AsyncGenerator

from google.adk.agents import BaseAgent, LlmAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from google.adk.models.lite_llm import LiteLlm

from agents.decomposer_agent import INSTRUCTION as DECOMPOSER_INSTRUCTION
from agents.placer_agent import INSTRUCTION as PLACER_INSTRUCTION
from agents.wirer_agent import INSTRUCTION as WIRER_INSTRUCTION

from tools.decompose_tools import assess_complexity, decompose_workflow, estimate_activity_count
from tools.build_tools import (
    load_activity_template,
    resolve_control_flow,
    build_activity_json,
    fill_scaffold_params,
)
from tools.pattern_tools import get_examples_for_control_flow

from tools.pipeline_stages import (
    run_pattern_match,
    run_retrieval,
    run_enrichment,
    run_fragments,
    run_content_scaffold,
    run_cleanup,
    run_annotation,
    run_validation,
)
from tools.output_tools import run_output
from tools.annotation_tools import _ensure_dict


# ---------------------------------------------------------------------------
# Model factories
# ---------------------------------------------------------------------------

def _model_fast() -> LiteLlm:
    return LiteLlm(
        model=os.getenv("MODEL_FAST", "gemini/gemini-2.5-flash"),
        api_key=os.getenv("GOOGLE_API_KEY"),
    )


def _model_structure() -> LiteLlm:
    return LiteLlm(
        model=os.getenv("MODEL", "gemini/gemini-2.5-pro"),
        max_tokens=8192,
        temperature=0.1,
        api_key=os.getenv("GOOGLE_API_KEY"),
    )


# ---------------------------------------------------------------------------
# Activity count guard
# ---------------------------------------------------------------------------

def _count_top_level_activities(workflow: dict) -> int:
    raw = workflow.get("workflow_raw_data", workflow) if isinstance(workflow, dict) else {}
    return sum(1 for v in raw.values() if isinstance(v, dict))


def _check_activity_count(
    workflow_json: dict,
    enriched_workflow: dict,
) -> str | None:
    """
    Returns an error string if WirerAgent dropped too many activities, else None.
    Threshold: workflow_json must have at least 70% of enriched_workflow's
    top-level activity count. Below this the output is almost certainly
    a partial/truncated response rather than a complete workflow.
    """
    expected = _count_top_level_activities(enriched_workflow)
    actual   = _count_top_level_activities(workflow_json)

    if expected == 0:
        return None

    if actual == 0:
        return (
            f"WirerAgent returned a workflow with 0 activities "
            f"(expected ~{expected}). Output is empty."
        )

    ratio = actual / expected
    if ratio < 0.70:
        return (
            f"WirerAgent truncated the workflow: returned {actual} top-level "
            f"activities, expected at least {int(expected * 0.70)} "
            f"(70% of {expected}). This is likely a partial response — "
            f"the full workflow JSON was not returned."
        )

    print(f"  [pipeline] activity count ok — {actual}/{expected} top-level activities")
    return None


# ---------------------------------------------------------------------------
# Shared post-wirer stages (Stages 4f, 5, 6, 7)
# ---------------------------------------------------------------------------

async def _run_post_wirer_stages(ctx: InvocationContext, activity_manifest: list):
    """
    Runs Stages 4f-7 after WirerAgent completes.
    Writes output_result to ctx.session.state in all cases.
    """
    workflow_json = _ensure_dict(ctx.session.state.get("workflow_json"))

    if not workflow_json:
        ctx.session.state["_empty_response_error"] = True
        ctx.session.state["output_result"] = {
            "status": "failed",
            "errors": ["WirerAgent returned empty or unparseable output."],
        }
        return

    print(f"  [pipeline] workflow_json ok — "
          f"{len(workflow_json.get('workflow_raw_data', {}))} activities")

    # ── Activity count guard ──────────────────────────────────────────────────
    enriched_workflow = _ensure_dict(ctx.session.state.get("enriched_workflow", {}))
    truncation_error  = _check_activity_count(workflow_json, enriched_workflow)
    if truncation_error:
        print(f"  [pipeline] activity count guard: {truncation_error}")
        ctx.session.state["output_result"] = {
            "status": "failed",
            "errors": [truncation_error],
        }
        return

    # ── Stage 4f: Re-apply fragments + scaffold on Wirer output ─────────────
    # Enforces F1-F9 and F10 on activities WirerAgent reconstructed that were
    # absent from the PlacerAgent skeleton and never processed by Stages 4c/4c.5.
    # Both apply_fragments() and apply_content_scaffold() are idempotent.
    # F10 (description sync) lives in run_content_scaffold, not run_fragments —
    # both must run here to cover Wirer-added nested activities.
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
        # Fall through — annotation runs on unpatched Wirer output

    # ── Stage 4g: Post-Wirer cleanup ────────────────────────────────────────
    # Removes inert nodes WirerAgent added when reconstructing a collapsed
    # PlacerAgent skeleton — e.g. MemorySet with empty VariableName/VariableValue.
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
        ctx.session.state["output_result"] = {
            "status": "failed",
            "errors": [f"Validation failed: {e}"],
        }
        return

    # Stage 6b: validation failure — return without output for outer retry.
    if validation_result["status"] == "invalid":
        print(f"  [pipeline] validation failed — returning without output for outer retry")
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
        ctx.session.state["output_result"] = {
            "status": "failed",
            "errors": [f"Output stage failed: {e}"],
        }


# ---------------------------------------------------------------------------
# Full pipeline (Attempt 1)
# ---------------------------------------------------------------------------

class WorkflowPipeline(BaseAgent):

    decomposer: LlmAgent
    placer:     LlmAgent
    wirer:      LlmAgent

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:

        # Stage 1: Decompose
        async for event in self.decomposer.run_async(ctx):
            yield event

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
            return

        # Stage 2: Pattern match
        try:
            pattern_match = run_pattern_match(decomposition)
            ctx.session.state["pattern_match"] = pattern_match
            print(f"  [pipeline] pattern_match: {pattern_match.get('match_status')}")
        except Exception as e:
            print(f"  [pipeline] pattern_match failed (non-fatal): {e}")

        # Stage 3: Retrieve
        try:
            activity_manifest = run_retrieval(decomposition)
            ctx.session.state["activity_manifest"] = activity_manifest
            print(f"  [pipeline] retrieval ok — {len(activity_manifest)} entries")
        except Exception as e:
            print(f"  [pipeline] retrieval failed: {e}")
            ctx.session.state["output_result"] = {
                "status": "failed",
                "errors": [f"Retrieval failed: {e}"],
            }
            return

        # Stage 4a: Place
        async for event in self.placer.run_async(ctx):
            yield event

        placed_skeleton = _ensure_dict(ctx.session.state.get("placed_skeleton"))

        if not placed_skeleton:
            print("  [pipeline] placed_skeleton is empty — aborting")
            ctx.session.state["_empty_response_error"] = True
            ctx.session.state["output_result"] = {
                "status": "failed",
                "errors": ["PlacerAgent returned empty or unparseable output."],
            }
            return

        print(f"  [pipeline] placed_skeleton ok — "
              f"{len(placed_skeleton.get('workflow_raw_data', {}))} activities placed")

        # Stage 4b: Enrich
        try:
            enriched_workflow = run_enrichment(placed_skeleton, activity_manifest)
            ctx.session.state["enriched_workflow"] = enriched_workflow
            print(f"  [pipeline] enrichment ok — "
                  f"{len(enriched_workflow.get('workflow_raw_data', {}))} activities enriched")
        except Exception as e:
            print(f"  [pipeline] enrichment failed: {e}")
            ctx.session.state["output_result"] = {
                "status": "failed",
                "errors": [f"Enrichment failed: {e}"],
            }
            return

        # Stage 4c: Fragments
        try:
            fragmented_workflow = run_fragments(enriched_workflow)
            ctx.session.state["enriched_workflow"] = fragmented_workflow
            print(f"  [pipeline] fragments ok — "
                  f"{len(fragmented_workflow.get('workflow_raw_data', {}))} activities")
        except Exception as e:
            print(f"  [pipeline] fragments failed: {e}")
            ctx.session.state["output_result"] = {
                "status": "failed",
                "errors": [f"Fragment application failed: {e}"],
            }
            return

        # Stage 4c.5: Content scaffold
        try:
            scaffolded_workflow = run_content_scaffold(fragmented_workflow)
            ctx.session.state["enriched_workflow"] = scaffolded_workflow
            print(f"  [pipeline] scaffold ok — "
                  f"{len(scaffolded_workflow.get('workflow_raw_data', {}))} activities")
        except Exception as e:
            print(f"  [pipeline] scaffold failed (non-fatal): {e}")
            scaffolded_workflow = fragmented_workflow

        # Stage 4d: Wire
        async for event in self.wirer.run_async(ctx):
            yield event

        # Stages 4f-7
        await _run_post_wirer_stages(ctx, activity_manifest)


# ---------------------------------------------------------------------------
# Correction pipeline (Attempt 2 on validation failure)
# ---------------------------------------------------------------------------

class CorrectionPipeline(BaseAgent):
    """
    Targeted retry pipeline. Skips DecomposerAgent and PlacerAgent.
    Expects session pre-loaded with decomposition and placed_skeleton
    from attempt 1. WirerAgent receives the CORRECTION REQUIRED prompt
    with embedded workflow_json from attempt 1.
    """

    wirer: LlmAgent

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:

        decomposition   = _ensure_dict(ctx.session.state.get("decomposition"))
        placed_skeleton = _ensure_dict(ctx.session.state.get("placed_skeleton"))

        if not decomposition:
            print("  [correction] decomposition missing — aborting")
            ctx.session.state["_empty_response_error"] = True
            ctx.session.state["output_result"] = {
                "status": "failed",
                "errors": ["CorrectionPipeline: decomposition not found in session state."],
            }
            return

        if not placed_skeleton:
            print("  [correction] placed_skeleton missing — aborting")
            ctx.session.state["_empty_response_error"] = True
            ctx.session.state["output_result"] = {
                "status": "failed",
                "errors": ["CorrectionPipeline: placed_skeleton not found in session state."],
            }
            return

        print(f"  [correction] reusing decomposition "
              f"({len(decomposition.get('steps', []))} steps) and placed_skeleton "
              f"({len(placed_skeleton.get('workflow_raw_data', {}))} activities)")

        # Re-run pattern match (deterministic)
        try:
            pattern_match = run_pattern_match(decomposition)
            ctx.session.state["pattern_match"] = pattern_match
            print(f"  [correction] pattern_match: {pattern_match.get('match_status')}")
        except Exception as e:
            print(f"  [correction] pattern_match failed (non-fatal): {e}")

        # Re-run retrieval (deterministic)
        try:
            activity_manifest = run_retrieval(decomposition)
            ctx.session.state["activity_manifest"] = activity_manifest
            print(f"  [correction] retrieval ok — {len(activity_manifest)} entries")
        except Exception as e:
            print(f"  [correction] retrieval failed: {e}")
            ctx.session.state["output_result"] = {
                "status": "failed",
                "errors": [f"Correction retrieval failed: {e}"],
            }
            return

        # Re-run enrichment (deterministic)
        try:
            enriched_workflow = run_enrichment(placed_skeleton, activity_manifest)
            ctx.session.state["enriched_workflow"] = enriched_workflow
            print(f"  [correction] enrichment ok — "
                  f"{len(enriched_workflow.get('workflow_raw_data', {}))} activities")
        except Exception as e:
            print(f"  [correction] enrichment failed: {e}")
            ctx.session.state["output_result"] = {
                "status": "failed",
                "errors": [f"Correction enrichment failed: {e}"],
            }
            return

        # Re-apply fragments (deterministic)
        try:
            fragmented_workflow = run_fragments(enriched_workflow)
            ctx.session.state["enriched_workflow"] = fragmented_workflow
            print(f"  [correction] fragments ok")
        except Exception as e:
            print(f"  [correction] fragments failed (non-fatal): {e}")
            fragmented_workflow = enriched_workflow

        # Re-apply content scaffold (deterministic)
        try:
            scaffolded_workflow = run_content_scaffold(fragmented_workflow)
            ctx.session.state["enriched_workflow"] = scaffolded_workflow
            print(f"  [correction] scaffold ok")
        except Exception as e:
            print(f"  [correction] scaffold failed (non-fatal): {e}")
            scaffolded_workflow = fragmented_workflow

        # WirerAgent receives CORRECTION REQUIRED prompt with embedded workflow_json
        async for event in self.wirer.run_async(ctx):
            yield event

        # Stages 4f-7
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
    decomposer = LlmAgent(
        name="DecomposerAgent",
        model=_model_fast(),
        instruction=DECOMPOSER_INSTRUCTION,
        tools=[assess_complexity, decompose_workflow, estimate_activity_count],
        output_key="decomposition",
        include_contents="none",
    )
    placer = LlmAgent(
        name="PlacerAgent",
        model=_model_fast(),
        instruction=PLACER_INSTRUCTION,
        tools=[fill_scaffold_params, resolve_control_flow, get_examples_for_control_flow],
        output_key="placed_skeleton",
        include_contents="none",
    )
    wirer = LlmAgent(
        name="WirerAgent",
        model=_model_structure(),
        instruction=WIRER_INSTRUCTION,
        tools=[load_activity_template],
        output_key="workflow_json",
        include_contents="none",
    )
    return WorkflowPipeline(
        name="workflow_pipeline",
        decomposer=decomposer,
        placer=placer,
        wirer=wirer,
    )


def build_correction_pipeline() -> CorrectionPipeline:
    wirer = LlmAgent(
        name="WirerAgent",
        model=_model_structure(),
        instruction=WIRER_INSTRUCTION,
        tools=[load_activity_template],
        output_key="workflow_json",
        include_contents="none",
    )
    return CorrectionPipeline(
        name="correction_pipeline",
        wirer=wirer,
    )