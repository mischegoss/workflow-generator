"""
agents/pipeline.py

WorkflowPipeline — BaseAgent subclass orchestrating all stages.

STAGE MAP:
  Stage 1    LLM   DecomposerAgent      → session state: decomposition
  Stage 2    PY    run_pattern_match    → session state: pattern_match
  Stage 3    PY    run_retrieval        → session state: activity_manifest
  Stage 4a   PY    run_skeleton_builder → session state: placed_skeleton
  Stage 4b   PY    run_enrichment       → session state: enriched_workflow
  Stage 4c   PY    run_fragments        → session state: enriched_workflow
  Stage 4c.5 PY    run_content_scaffold → session state: enriched_workflow
  Stage 4d   LLM   WirerAgent           → session state: workflow_json
  Stage 4f   PY    run_fragments+scaffold → session state: workflow_json
  Stage 4g   PY    run_cleanup          → session state: workflow_json
  Stage 5    PY    run_annotation       → session state: annotation_result
  Stage 6    PY    run_validation       → session state: validation_result
  Stage 7    PY    run_output           → json_files/<n>.json

MODEL CONFIGURATION:
  Two distinct model factories — each tuned for its task:

  _model_decomposer()  Flash  temp=0.1  top_p=0.8  max_tokens=4096  JSON mode
    Decomposition is closed-set classification (intent enum, zone enum, control_flow
    enum). Needs enough latitude to interpret natural language but must stay within
    the defined enums. 0.1/0.8 balances interpretation with consistency.

  _model_wirer()       Pro    temp=0.1  top_p=0.7  max_tokens=8192  JSON mode
    Semantic field filling — descriptions, variable references, email content.
    Needs more reasoning than Flash but must not hallucinate field names or
    variable references. 0.1/0.7 keeps it grounded.

  JSON mode via response_mime_type is NOT used — not supported by the
  LiteLLM/Gemini routing path. _ensure_dict() handles markdown fence stripping.

WHY STAGE 4f EXISTS:
  WirerAgent may modify or reconstruct structural activities in ways that bypass
  the fragment/scaffold layers run at Stages 4c/4c.5. Stage 4f re-runs both
  idempotently after Wirer to enforce F1-F9 and F10 on everything Wirer produced.
  apply_fragments() and apply_content_scaffold() are both idempotent — safe to
  call twice.

RETRY ARCHITECTURE:
  On validation failure, main.py calls build_correction_pipeline().
  CorrectionPipeline skips DecomposerAgent entirely. It re-runs all deterministic
  stages (retrieval, skeleton builder, enrichment, fragments, scaffold) from
  decomposition (which persists as an LlmAgent output_key). It does NOT read
  placed_skeleton from session state — Python-stage state mutations do not
  persist through ADK get_session() in the correction run.
  WirerAgent receives CORRECTION REQUIRED prompt with embedded workflow_json.

ACTIVITY COUNT GUARD:
  After WirerAgent, workflow_json is compared against enriched_workflow.
  Below 70% of expected top-level activity count → rejected as truncation.
  D4 structural check verifies WhileActivity bodies are not empty.

ADK STATE NOTE:
  LlmAgent output_key values persist through get_session().
  Python-stage ctx.session.state mutations do NOT persist through get_session().
  decomposition and workflow_json (output_keys) survive retry.
  placed_skeleton, activity_manifest, enriched_workflow must be recomputed.
"""

import os
import re
from typing import AsyncGenerator

from google.adk.agents import BaseAgent, LlmAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from google.adk.models.lite_llm import LiteLlm

from agents.decomposer_agent import INSTRUCTION as DECOMPOSER_INSTRUCTION
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
    run_skeleton_builder,
    normalize_wirer_output,
    run_enrichment,
    _apply_wirer_patches,
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

def _model_decomposer() -> LiteLlm:
    """
    DecomposerAgent — Flash with low temperature.

    Decomposition is closed-set classification: the output must use only
    the defined intent enum, zone enum, and control_flow enum values.
    temperature=0.1 keeps the model on-enum while allowing natural language
    interpretation. top_p=0.8 provides slightly more vocabulary range than
    Wirer since the Decomposer must interpret free-form user prompts.
    """
    return LiteLlm(
        model=os.getenv("MODEL_FAST", "gemini/gemini-2.5-flash"),
        temperature=0.1,
        top_p=0.8,
        max_tokens=4096,
        api_key=os.getenv("GOOGLE_API_KEY"),
    )


def _model_wirer() -> LiteLlm:
    """
    WirerAgent — Pro with constrained sampling.

    Semantic field filling requires stronger reasoning than Flash for
    correct variable references, contextual descriptions, and SQL/script
    content. temperature=0.1 keeps field values grounded. top_p=0.7
    prevents the model from sampling unlikely tokens that produce hallucinated
    variable names or field keys. max_tokens=8192 accommodates large workflows.
    """
    return LiteLlm(
        model=os.getenv("MODEL", "gemini/gemini-2.5-pro"),
        temperature=0.1,
        top_p=0.7,
        max_tokens=8192,
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
    Threshold: 70% of enriched_workflow top-level activity count.
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
    # D4 structural check removed — validate_control_flow_rules (r3) enforces
    # WhileActivity/SequenceActivity structure and allows the correction pipeline
    # to retry. Hard-failing here bypassed retry for recoverable Wirer collapses.
    return None


# ---------------------------------------------------------------------------
# Shared deterministic stages (Stages 3, 4a-4c.5)
# ---------------------------------------------------------------------------

def _run_deterministic_stages(
    ctx: InvocationContext,
    decomposition: dict,
    label: str,
) -> tuple[list, dict] | None:
    """
    Runs Stages 3 (retrieval) through 4c.5 (scaffold) deterministically.
    Returns (activity_manifest, scaffolded_workflow) on success, or None on failure.
    label is used in log messages ('pipeline' or 'correction').
    """
    # Stage 3: Retrieve
    try:
        activity_manifest = run_retrieval(decomposition)
        ctx.session.state["activity_manifest"] = activity_manifest
        print(f"  [{label}] retrieval ok — {len(activity_manifest)} entries")
    except Exception as e:
        print(f"  [{label}] retrieval failed: {e}")
        ctx.session.state["output_result"] = {
            "status": "failed",
            "errors": [f"Retrieval failed: {e}"],
        }
        return None

    # Stage 4a: Build skeleton (deterministic — no LLM)
    try:
        placed_skeleton = run_skeleton_builder(decomposition, activity_manifest)
        ctx.session.state["placed_skeleton"] = placed_skeleton
        print(f"  [{label}] placed_skeleton ok — "
              f"{len(placed_skeleton.get('workflow_raw_data', {}))} activities placed")
    except Exception as e:
        print(f"  [{label}] skeleton builder failed: {e}")
        ctx.session.state["output_result"] = {
            "status": "failed",
            "errors": [f"Skeleton builder failed: {e}"],
        }
        return None

    # Stage 4b: Enrich
    try:
        enriched_workflow = run_enrichment(placed_skeleton, activity_manifest)
        ctx.session.state["enriched_workflow"] = enriched_workflow
        print(f"  [{label}] enrichment ok — "
              f"{len(enriched_workflow.get('workflow_raw_data', {}))} activities enriched")
    except Exception as e:
        print(f"  [{label}] enrichment failed: {e}")
        ctx.session.state["output_result"] = {
            "status": "failed",
            "errors": [f"Enrichment failed: {e}"],
        }
        return None

    # Stage 4c: Fragments
    try:
        fragmented_workflow = run_fragments(enriched_workflow)
        ctx.session.state["enriched_workflow"] = fragmented_workflow
        print(f"  [{label}] fragments ok — "
              f"{len(fragmented_workflow.get('workflow_raw_data', {}))} activities")
    except Exception as e:
        print(f"  [{label}] fragments failed: {e}")
        ctx.session.state["output_result"] = {
            "status": "failed",
            "errors": [f"Fragment application failed: {e}"],
        }
        return None

    # Stage 4c.5: Content scaffold
    try:
        scaffolded_workflow = run_content_scaffold(fragmented_workflow)
        ctx.session.state["enriched_workflow"] = scaffolded_workflow
        print(f"  [{label}] scaffold ok — "
              f"{len(scaffolded_workflow.get('workflow_raw_data', {}))} activities")
    except Exception as e:
        print(f"  [{label}] scaffold failed (non-fatal): {e}")
        scaffolded_workflow = fragmented_workflow

    return activity_manifest, scaffolded_workflow


# ---------------------------------------------------------------------------
# Shared post-wirer stages (Stages 4f, 4g, 5, 6, 7)
# ---------------------------------------------------------------------------

async def _run_post_wirer_stages(ctx: InvocationContext, activity_manifest: list):
    """
    Runs Stages 4f-7 after WirerAgent completes.
    Writes output_result to ctx.session.state in all cases.
    """
    _raw_wj = ctx.session.state.get("workflow_json")
    workflow_json = _ensure_dict(_raw_wj)

    if not workflow_json:
        print(f"  [pipeline] WirerAgent output empty — "
              f"type={type(_raw_wj).__name__} "
              f"len={len(str(_raw_wj)) if _raw_wj is not None else 0}")
        ctx.session.state["_empty_response_error"] = True
        ctx.session.state["output_result"] = {
            "status": "failed",
            "errors": ["WirerAgent returned empty or unparseable output."],
        }
        return

    # ── Apply Wirer patches onto enriched_workflow ───────────────────────────
    # WirerAgent now returns only the fields it changed as a flat
    # {xName: {field: value}} patch dict under "wirer_patches".
    # The pipeline merges these onto the enriched_workflow skeleton.
    # Falls back to normalize_wirer_output for legacy full-workflow responses.
    enriched_for_merge = _ensure_dict(
        ctx.session.state.get("enriched_workflow", {})
    )
    try:
        workflow_json = _apply_wirer_patches(
            workflow_json, enriched_for_merge
        )
        ctx.session.state["workflow_json"] = workflow_json
    except Exception as e:
        print(f"  [pipeline] patch apply failed: {e}")
        ctx.session.state["output_result"] = {
            "status": "failed",
            "errors": [f"Wirer patch application failed: {e}"],
        }
        return

    raw_data = workflow_json.get("workflow_raw_data", {})
    n_dicts = sum(1 for v in raw_data.values() if isinstance(v, dict))
    print(f"  [pipeline] workflow_json ok — "
          f"{n_dicts} activity dicts in workflow_raw_data")

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

    # ── Stage 4f: Re-apply fragments + scaffold on Wirer output ──────────────
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

    # ── Stage 4g: Post-Wirer cleanup ─────────────────────────────────────────
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

    # Stage 6b: validation failure — return without output for outer retry
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

        # Stages 3, 4a-4c.5: deterministic stages
        result = _run_deterministic_stages(ctx, decomposition, label="pipeline")
        if result is None:
            return
        activity_manifest, _ = result

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
    Targeted retry pipeline. Skips DecomposerAgent entirely.

    Re-runs all deterministic stages (retrieval, skeleton builder, enrichment,
    fragments, scaffold) from decomposition, which persists as an LlmAgent
    output_key. Does NOT read placed_skeleton or enriched_workflow from session
    state — Python-stage state mutations do not persist through ADK get_session()
    in the correction run.

    WirerAgent receives the CORRECTION REQUIRED prompt with embedded workflow_json
    from attempt 1.
    """

    wirer: LlmAgent

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:

        decomposition = _ensure_dict(ctx.session.state.get("decomposition"))

        if not decomposition:
            print("  [correction] decomposition missing — aborting")
            ctx.session.state["_empty_response_error"] = True
            ctx.session.state["output_result"] = {
                "status": "failed",
                "errors": ["CorrectionPipeline: decomposition not found in session state."],
            }
            return

        print(f"  [correction] reusing decomposition "
              f"({len(decomposition.get('steps', []))} steps) — "
              f"rebuilding skeleton and enrichment deterministically")

        # Stage 2: Pattern match (deterministic)
        try:
            pattern_match = run_pattern_match(decomposition)
            ctx.session.state["pattern_match"] = pattern_match
            print(f"  [correction] pattern_match: {pattern_match.get('match_status')}")
        except Exception as e:
            print(f"  [correction] pattern_match failed (non-fatal): {e}")

        # Stages 3, 4a-4c.5: deterministic stages (re-run from decomposition)
        result = _run_deterministic_stages(ctx, decomposition, label="correction")
        if result is None:
            return
        activity_manifest, _ = result

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