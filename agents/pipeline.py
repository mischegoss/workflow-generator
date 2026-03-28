"""
agents/pipeline.py

ARCHITECTURE:
  Stage 1  LLM   DecomposerAgent
  Stage 2  PY    run_pattern_match
  Stage 3  PY    run_retrieval
  Stage 4a LLM   PlacerAgent
  Stage 4b PY    run_enrichment      (normalises TypeNames)
  Stage 4c PY    run_fragments       (F1-F9)
  Stage 4d LLM   WirerAgent
  Stage 4e PY    _merge_wirer_output (preserves template fields; protects fragment fields)
  Stage 4f PY    run_fragments       (F1-F9 on merged result)
  Stage 5  PY    run_annotation
  Stage 6  PY    run_validation
  Stage 7  PY    run_output
"""

import os
import re
import copy
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
# Fields that fragments own — WirerAgent must not overwrite these
# ---------------------------------------------------------------------------

# These fields are set deterministically by fragment rules F1-F9.
# If WirerAgent output contains these fields, they are ignored during merge
# so that fragment values are preserved on the final workflow_json.
_FRAGMENT_OWNED_FIELDS: frozenset = frozenset({
    # F1
    "Condition",
    # F2
    "exitWhileInsideWhile", "isValid", "whileSequenceActivity",
    # F3
    "Counter",
    # F4
    "RowNumber", "ColumnType",
    # F5  (setdefault — only protect if already set)
    # F6/F7/F8 ReturnValue structural fields
    "IsValid", "UseStoredValue", "Formula", "UseBranchWhenTimeout",
    "Type",
})

# ReturnValue fields that WirerAgent IS allowed to set
_WIRER_ALLOWED_RV_FIELDS: frozenset = frozenset({
    "ConditionType", "Value", "Description", "description",
    "ConditionName", "ConditionNumber",
})


def _merge_wirer_output(enriched_workflow: dict, wirer_output: dict) -> dict:
    """
    Merge WirerAgent's semantic fields onto enriched_workflow.

    Rules:
    - enriched_workflow is the base (has all template fields)
    - wirer output wins on semantic fields (Description, ValueToDisplay,
      HostName, ConditionType, Value, ResultSet, TableName etc.)
    - Fragment-owned fields (_FRAGMENT_OWNED_FIELDS) are NEVER overwritten
      by wirer output — fragment values take precedence
    - For ReturnValue nodes, only _WIRER_ALLOWED_RV_FIELDS are accepted
      from wirer output (ConditionType and Value)
    - WirerAgent-only top-level activities are included
    """
    enriched = copy.deepcopy(enriched_workflow)
    wirer    = _ensure_dict(wirer_output)

    e_raw = enriched.get("workflow_raw_data", enriched)
    w_raw = wirer.get("workflow_raw_data", wirer)

    if not isinstance(e_raw, dict) or not isinstance(w_raw, dict):
        return enriched

    def merge_node(base: dict, overlay: dict, is_returnvalue: bool = False) -> dict:
        result = dict(base)
        for key, w_val in overlay.items():
            # Never overwrite fragment-owned fields
            if key in _FRAGMENT_OWNED_FIELDS:
                continue
            # For ReturnValue, only accept allowed fields
            if is_returnvalue and key not in _WIRER_ALLOWED_RV_FIELDS:
                continue
            if isinstance(w_val, dict):
                is_rv_child = w_val.get("CustomTypeName") == "ReturnValue"
                if key in result and isinstance(result[key], dict):
                    result[key] = merge_node(result[key], w_val,
                                             is_returnvalue=is_rv_child)
                else:
                    result[key] = w_val
            elif w_val is not None and w_val != "" and w_val != "{}":
                result[key] = w_val
        return result

    merged_raw = {}
    for xname, e_act in e_raw.items():
        if not isinstance(e_act, dict):
            merged_raw[xname] = e_act
            continue
        w_act = w_raw.get(xname, {})
        is_rv = e_act.get("CustomTypeName") == "ReturnValue"
        if isinstance(w_act, dict) and w_act:
            merged_raw[xname] = merge_node(e_act, w_act, is_returnvalue=is_rv)
        else:
            merged_raw[xname] = e_act

    # Include any top-level activities WirerAgent added
    for xname, w_act in w_raw.items():
        if xname not in merged_raw and isinstance(w_act, dict):
            merged_raw[xname] = w_act
            print(f"  [merge] added wirer-only activity: {xname}")

    # Sync description → Description when Description still holds the template placeholder.
    # WirerAgent fills lowercase 'description' but not uppercase 'Description'.
    # Both must be filled for the validator to pass.
    PLACEHOLDER = "activityDesc_value"
    def sync_descriptions(node):
        if not isinstance(node, dict):
            return
        desc_lower = node.get("description", "")
        desc_upper = node.get("Description", "")
        if (desc_lower and desc_lower != PLACEHOLDER
                and (not desc_upper or desc_upper == PLACEHOLDER)):
            node["Description"] = desc_lower
        for v in node.values():
            if isinstance(v, dict):
                sync_descriptions(v)
    for act in merged_raw.values():
        sync_descriptions(act)

    result = dict(enriched)
    result["workflow_raw_data"] = merged_raw
    for key in wirer:
        if key != "workflow_raw_data" and key not in result:
            result[key] = wirer[key]

    return result


# ---------------------------------------------------------------------------
# Shared post-wirer stages (4e-7)
# ---------------------------------------------------------------------------

async def _run_post_wirer_stages(ctx: InvocationContext,
                                  activity_manifest: list,
                                  enriched_workflow: dict):
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

    # Stage 4e: merge
    try:
        merged = _merge_wirer_output(enriched_workflow, workflow_json)
        ctx.session.state["workflow_json"] = merged
        workflow_json = merged
        print(f"  [pipeline] merge ok — "
              f"{len(workflow_json.get('workflow_raw_data', {}))} activities")
    except Exception as e:
        print(f"  [pipeline] merge failed (non-fatal): {e}")

    # Stage 4f: re-apply fragments
    try:
        workflow_json = run_fragments(workflow_json)
        ctx.session.state["workflow_json"] = workflow_json
        print(f"  [pipeline] post-wirer fragments ok — "
              f"{len(workflow_json.get('workflow_raw_data', {}))} activities")
    except Exception as e:
        print(f"  [pipeline] post-wirer fragments failed (non-fatal): {e}")

    # Stage 5: Annotate
    try:
        annotation_result = run_annotation(workflow_json, activity_manifest)
        ctx.session.state["annotation_result"] = annotation_result
        n_notes = len(annotation_result.get("placeholder_summary", []))
        print(f"  [pipeline] annotation ok — {n_notes} placeholder/verify items")
    except Exception as e:
        print(f"  [pipeline] annotation failed: {e}")
        ctx.session.state["output_result"] = {"status": "failed", "errors": [str(e)]}
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
        ctx.session.state["output_result"] = {"status": "failed", "errors": [str(e)]}
        return

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
        ctx.session.state["output_result"] = {"status": "failed", "errors": [str(e)]}


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

        try:
            pattern_match = run_pattern_match(decomposition)
            ctx.session.state["pattern_match"] = pattern_match
            print(f"  [pipeline] pattern_match: {pattern_match.get('match_status')}")
        except Exception as e:
            print(f"  [pipeline] pattern_match failed: {e}")
            ctx.session.state["output_result"] = {"status": "failed", "errors": [str(e)]}
            return

        try:
            activity_manifest = run_retrieval(decomposition)
            ctx.session.state["activity_manifest"] = activity_manifest
            print(f"  [pipeline] retrieval ok — {len(activity_manifest)} entries")
        except Exception as e:
            print(f"  [pipeline] retrieval failed: {e}")
            ctx.session.state["output_result"] = {"status": "failed", "errors": [str(e)]}
            return

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

        try:
            enriched_workflow = run_enrichment(placed_skeleton, activity_manifest)
            ctx.session.state["enriched_workflow"] = enriched_workflow
            print(f"  [pipeline] enrichment ok — "
                  f"{len(enriched_workflow.get('workflow_raw_data', {}))} activities enriched")
        except Exception as e:
            print(f"  [pipeline] enrichment failed: {e}")
            ctx.session.state["output_result"] = {"status": "failed", "errors": [str(e)]}
            return

        try:
            fragmented_workflow = run_fragments(enriched_workflow)
            ctx.session.state["enriched_workflow"] = fragmented_workflow
            print(f"  [pipeline] fragments ok — "
                  f"{len(fragmented_workflow.get('workflow_raw_data', {}))} activities")
        except Exception as e:
            print(f"  [pipeline] fragments failed: {e}")
            ctx.session.state["output_result"] = {"status": "failed", "errors": [str(e)]}
            return

        async for event in self.wirer.run_async(ctx):
            yield event

        await _run_post_wirer_stages(ctx, activity_manifest, fragmented_workflow)


# ---------------------------------------------------------------------------
# Correction pipeline (Attempt 2)
# ---------------------------------------------------------------------------

class CorrectionPipeline(BaseAgent):

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

        try:
            pattern_match = run_pattern_match(decomposition)
            ctx.session.state["pattern_match"] = pattern_match
            print(f"  [correction] pattern_match: {pattern_match.get('match_status')}")
        except Exception as e:
            print(f"  [correction] pattern_match failed (non-fatal): {e}")

        try:
            activity_manifest = run_retrieval(decomposition)
            ctx.session.state["activity_manifest"] = activity_manifest
            print(f"  [correction] retrieval ok — {len(activity_manifest)} entries")
        except Exception as e:
            print(f"  [correction] retrieval failed: {e}")
            ctx.session.state["output_result"] = {"status": "failed", "errors": [str(e)]}
            return

        try:
            enriched_workflow = run_enrichment(placed_skeleton, activity_manifest)
            ctx.session.state["enriched_workflow"] = enriched_workflow
            print(f"  [correction] enrichment ok — "
                  f"{len(enriched_workflow.get('workflow_raw_data', {}))} activities")
        except Exception as e:
            print(f"  [correction] enrichment failed: {e}")
            ctx.session.state["output_result"] = {"status": "failed", "errors": [str(e)]}
            return

        try:
            fragmented_workflow = run_fragments(enriched_workflow)
            ctx.session.state["enriched_workflow"] = fragmented_workflow
            print(f"  [correction] fragments ok")
        except Exception as e:
            print(f"  [correction] fragments failed (non-fatal): {e}")
            fragmented_workflow = enriched_workflow

        async for event in self.wirer.run_async(ctx):
            yield event

        await _run_post_wirer_stages(ctx, activity_manifest, fragmented_workflow)


# ---------------------------------------------------------------------------
# Base name derivation
# ---------------------------------------------------------------------------

_STOP_WORDS = {
    "a", "an", "the", "and", "or", "for", "to", "in", "of", "with",
    "by", "at", "on", "from", "into", "that", "which", "when", "if",
    "create", "creates", "creating", "build", "builds", "building",
    "generate", "generates", "generating", "make", "makes", "making",
    "run", "runs", "running", "execute", "executes", "executing",
    "process", "processes", "processing", "workflow", "automation",
    "script", "each", "every", "all", "using", "use", "uses",
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