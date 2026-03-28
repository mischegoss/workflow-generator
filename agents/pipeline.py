"""
agents/pipeline.py

WorkflowPipeline — BaseAgent subclass orchestrating all stages.

ARCHITECTURE — micro-agent design:

  Stage 1  LLM   DecomposerAgent     → session state: decomposition
  Stage 2  PY    run_pattern_match   → session state: pattern_match
  Stage 3  PY    run_retrieval       → session state: activity_manifest
  Stage 4a LLM   PlacerAgent         → session state: placed_skeleton
  Stage 4b PY    run_enrichment      → session state: enriched_workflow
  Stage 4c PY    run_fragments       → session state: enriched_workflow (overwritten)
  Stage 4d LLM   WirerAgent          → session state: workflow_json
  Stage 5  PY    run_annotation      → session state: annotation_result
  Stage 6  PY    run_validation      → session state: validation_result
  Stage 7  PY    run_output          → json_files/<n>.json

RETRY ARCHITECTURE:
  On validation failure, main.py calls build_correction_pipeline() instead of
  build_pipeline(). CorrectionPipeline skips DecomposerAgent and PlacerAgent
  entirely — both decomposition and placed_skeleton persist as output_key
  values and are reused from attempt 1's session state. Stages 3 and 4b/4c are
  re-run deterministically (same inputs = same outputs). WirerAgent then
  receives the CORRECTION REQUIRED prompt as its user message.

  This prevents the old failure mode where the full pipeline sent a
  CORRECTION REQUIRED error list to DecomposerAgent, which tried to
  decompose it as a workflow description and returned 0 steps.

ADK STATE NOTE:
  LlmAgent output_key values persist through get_session().
  Python-stage ctx.session.state mutations do NOT persist through get_session().
  Therefore: decomposition and placed_skeleton (output_keys) survive retry.
  activity_manifest and enriched_workflow (Python stage) must be recomputed.
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
    """Pro model for WirerAgent — needs strongest semantic reasoning."""
    return LiteLlm(
        model=os.getenv("MODEL", "gemini/gemini-2.5-pro"),
        max_tokens=8192,
        temperature=0.1,
        api_key=os.getenv("GOOGLE_API_KEY"),
    )


# ---------------------------------------------------------------------------
# Shared post-wirer stages (Stages 5-7)
# Used by both WorkflowPipeline and CorrectionPipeline to avoid duplication.
# ---------------------------------------------------------------------------

async def _run_post_wirer_stages(ctx: InvocationContext, activity_manifest: list):
    """
    Runs Stages 5-7 (annotate, validate, output) after WirerAgent completes.
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
    # Python-stage mutations don't survive ADK's get_session() call, so flags
    # set here aren't visible to main.py. Instead: return without writing output.
    # main.py._run_pipeline re-validates workflow_json (which IS persisted as an
    # output_key) after the run and builds the correction prompt from there.
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
    """
    Sequential pipeline. Sub-agents declared as Pydantic fields (required by ADK).
    Instantiated via build_pipeline() which passes fresh agents as kwargs.
    """

    decomposer: LlmAgent
    placer:     LlmAgent
    wirer:      LlmAgent

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:

        # ── Stage 1: LLM — Decompose ─────────────────────────────────────────
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

        print(f"  [pipeline] decomposition ok — {len(decomposition.get('steps', []))} steps")

        # ── Stage 2: Python — Pattern match ──────────────────────────────────
        try:
            pattern_match = run_pattern_match(decomposition)
            ctx.session.state["pattern_match"] = pattern_match
            print(f"  [pipeline] pattern_match: {pattern_match.get('match_status')}")
        except Exception as e:
            print(f"  [pipeline] pattern_match failed: {e}")
            ctx.session.state["output_result"] = {
                "status": "failed",
                "errors": [f"Pattern matching failed: {e}"],
            }
            return

        # ── Stage 3: Python — Retrieve activities ─────────────────────────────
        try:
            activity_manifest = run_retrieval(decomposition)
            ctx.session.state["activity_manifest"] = activity_manifest
            print(f"  [pipeline] retrieval ok — {len(activity_manifest)} entries")
        except Exception as e:
            print(f"  [pipeline] retrieval failed: {e}")
            ctx.session.state["output_result"] = {
                "status": "failed",
                "errors": [f"Activity retrieval failed: {e}"],
            }
            return

        # ── Stage 4a: LLM — Place activities (structure skeleton only) ────────
        async for event in self.placer.run_async(ctx):
            yield event

        raw_skeleton = ctx.session.state.get("placed_skeleton")
        print(f"  [pipeline] placed_skeleton raw type: {type(raw_skeleton).__name__}")
        placed_skeleton = _ensure_dict(raw_skeleton)

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

        # ── Stage 4b: Python — Enrich with templates + wiring hints ───────────
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

        # ── Stage 4c: Python — Apply structural fragments (F1-F8) ─────────────
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

        # ── Stage 4d: LLM — Wire semantic fields only ─────────────────────────
        async for event in self.wirer.run_async(ctx):
            yield event

        await _run_post_wirer_stages(ctx, activity_manifest)


# ---------------------------------------------------------------------------
# Correction pipeline (Attempt 2 on validation failure)
# Skips DecomposerAgent and PlacerAgent entirely.
# ---------------------------------------------------------------------------

class CorrectionPipeline(BaseAgent):
    """
    Targeted retry pipeline for validation failures.

    Expects a session pre-loaded with:
      - decomposition   (persisted output_key from attempt 1)
      - placed_skeleton (persisted output_key from attempt 1)
      - prompt          (original user prompt)

    The user message passed to this pipeline is the CORRECTION REQUIRED
    prompt, which WirerAgent receives directly.
    """

    wirer: LlmAgent

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:

        decomposition   = _ensure_dict(ctx.session.state.get("decomposition"))
        placed_skeleton = _ensure_dict(ctx.session.state.get("placed_skeleton"))

        if not decomposition:
            print("  [correction] decomposition missing from state — aborting")
            ctx.session.state["_empty_response_error"] = True
            ctx.session.state["output_result"] = {
                "status": "failed",
                "errors": ["CorrectionPipeline: decomposition not found in session state."],
            }
            return

        if not placed_skeleton:
            print("  [correction] placed_skeleton missing from state — aborting")
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

        # Re-run retrieval (deterministic — same decomposition = same manifest)
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

        # Re-run enrichment (deterministic — same skeleton + manifest = same result)
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

        # Re-apply fragments (deterministic — same inputs = same output)
        try:
            fragmented_workflow = run_fragments(enriched_workflow)
            ctx.session.state["enriched_workflow"] = fragmented_workflow
            print(f"  [correction] fragments ok")
        except Exception as e:
            print(f"  [correction] fragments failed (non-fatal): {e}")

        # WirerAgent receives CORRECTION REQUIRED prompt as user message
        async for event in self.wirer.run_async(ctx):
            yield event

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
    """Return a fresh WorkflowPipeline (full run) with new LlmAgent instances."""
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
    """
    Return a CorrectionPipeline for validation-failure retries.
    WirerAgent only — DecomposerAgent and PlacerAgent are not included.
    Reuses decomposition and placed_skeleton from the prior session.
    """
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