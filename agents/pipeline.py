"""
agents/pipeline.py

WorkflowPipeline — BaseAgent subclass orchestrating all 7 stages.

KEY ARCHITECTURE CHANGE from previous version:

ADK's InMemorySessionService only persists state changes that go through the
LlmAgent output_key mechanism. Arbitrary ctx.session.state mutations made
inside BaseAgent._run_async_impl are visible in-process during execution
but are NOT guaranteed to survive a get_session() call after the run completes.

More importantly: _run_async_impl must YIELD events after each async sub-agent
call. The runner's async for loop terminates as soon as the generator returns.
If the Decomposer runs and writes "decomposition" to state, but then Python
stages run and _run_async_impl returns before StructureBuilder yields any events,
the runner sees the generator as complete and stops.

SOLUTION: All Python stages (pattern match, retrieve, annotate, validate, output)
run BEFORE StructureBuilder is invoked, and their results are stored in session
state so StructureBuilder's prompt context includes them. StructureBuilder then
runs and writes "workflow_json" via output_key. The remaining Python stages
(annotate, validate, output) run after StructureBuilder's events are exhausted,
which is safe because the generator is still active.

State mutations to ctx.session.state within _run_async_impl are in-process and
visible to the next stage in the same run, even if get_session() after the run
only shows LlmAgent output_key values.
"""

import os
import re
from typing import AsyncGenerator

from google.adk.agents import BaseAgent, LlmAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from google.adk.models.lite_llm import LiteLlm

from agents.decomposer_agent import INSTRUCTION as DECOMPOSER_INSTRUCTION
from agents.structure_builder_agent import INSTRUCTION as STRUCTURE_INSTRUCTION

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
    """Matches original structure_builder_agent.py: Pro + temperature=0.2."""
    return LiteLlm(
        model=os.getenv("MODEL", "gemini/gemini-2.5-pro"),
        max_tokens=8192,
        temperature=0.2,
        api_key=os.getenv("GOOGLE_API_KEY"),
    )


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

class WorkflowPipeline(BaseAgent):
    """
    Sequential pipeline. Sub-agents declared as Pydantic fields (required by ADK).
    Instantiated via build_pipeline() which passes fresh agents as kwargs.
    """

    decomposer:        LlmAgent
    structure_builder: LlmAgent

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:

        # ── Stage 1: LLM — Decompose ────────────────────────────────────────
        async for event in self.decomposer.run_async(ctx):
            yield event

        # Read decomposition from session state.
        # ADK stores output_key values as strings or dicts depending on version.
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

        # ── Stage 2: Python — Pattern match ────────────────────────────────
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

        # ── Stage 3: Python — Retrieve activities ───────────────────────────
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

        # ── Stage 4: LLM — Assemble structure ──────────────────────────────
        async for event in self.structure_builder.run_async(ctx):
            yield event

        raw_wf = ctx.session.state.get("workflow_json")
        print(f"  [pipeline] workflow_json raw type: {type(raw_wf).__name__}")

        workflow_json = _ensure_dict(raw_wf)

        if not workflow_json:
            print("  [pipeline] workflow_json is empty — aborting")
            ctx.session.state["_empty_response_error"] = True
            ctx.session.state["output_result"] = {
                "status": "failed",
                "errors": ["StructureBuilderAgent returned empty or unparseable output."],
            }
            return

        print(f"  [pipeline] workflow_json ok — {len(workflow_json.get('workflow_raw_data', {}))} activities")

        # ── Stage 5: Python — Annotate ──────────────────────────────────────
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

        # ── Stage 6: Python — Validate ──────────────────────────────────────
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

        # ── Stage 7: Python — Output JSON ───────────────────────────────────
        if validation_result["status"] == "valid":
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
        else:
            ctx.session.state["output_result"] = {
                "status": "failed",
                "errors": validation_result.get("errors", []),
            }


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
# Factory
# ---------------------------------------------------------------------------

def build_pipeline() -> WorkflowPipeline:
    """Return a fresh WorkflowPipeline with new LlmAgent instances each call."""
    decomposer = LlmAgent(
        name="DecomposerAgent",
        model=_model_fast(),
        instruction=DECOMPOSER_INSTRUCTION,
        tools=[assess_complexity, decompose_workflow, estimate_activity_count],
        output_key="decomposition",
        include_contents="none",
    )
    structure_builder = LlmAgent(
        name="StructureBuilderAgent",
        model=_model_structure(),
        instruction=STRUCTURE_INSTRUCTION,
        tools=[
            load_activity_template,
            resolve_control_flow,
            build_activity_json,
            fill_scaffold_params,
            get_examples_for_control_flow,
        ],
        output_key="workflow_json",
        include_contents="none",
    )
    return WorkflowPipeline(
        name="workflow_pipeline",
        decomposer=decomposer,
        structure_builder=structure_builder,
    )