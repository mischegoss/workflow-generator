"""
agents/pipeline.py

WorkflowPipeline — BaseAgent subclass that explicitly orchestrates all 7 stages.

Stages 1 and 4 are LLM agents (Decomposer, StructureBuilder).
Stages 2, 3, 5, 6, 7 are deterministic Python function calls.

Retry logic:
  - _empty_response_error: either LLM agent returned nothing → full retry from main.py
  - validation failure: StructureBuilder produced invalid JSON → one in-pipeline
    correction attempt before returning failure to main.py
"""

import os
from typing import AsyncGenerator

from google.adk.agents import BaseAgent, LlmAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from google.adk.models.lite_llm import LiteLlm

from agents.decomposer_agent import decomposer_agent
from agents.structure_builder_agent import structure_builder_agent

from tools.pipeline_stages import (
    run_pattern_match,
    run_retrieval,
    run_annotation,
    run_validation,
)
from tools.output_tools import run_output
from tools.annotation_tools import _ensure_dict


def _model():
    return LiteLlm(
        model=os.getenv("MODEL", "gemini/gemini-2.5-pro"),
        max_tokens=8192,
        api_key=os.getenv("GOOGLE_API_KEY"),
    )


def _model_fast():
    return LiteLlm(
        model=os.getenv("MODEL_FAST", "gemini/gemini-2.5-flash"),
        api_key=os.getenv("GOOGLE_API_KEY"),
    )


class WorkflowPipeline(BaseAgent):
    """
    Sequential pipeline with 2 LLM stages and 5 Python stages.

    Session state keys consumed and produced:
      IN  (set before run):   'prompt' — natural language workflow description
      OUT (set by pipeline):  'decomposition', 'pattern_match', 'activity_manifest',
                              'workflow_json', 'annotation_result', 'validation_result',
                              'output_result'
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # Create fresh LlmAgent instances each pipeline instantiation.
        # ADK raises a validation error if an agent already has a parent
        # (singleton reuse is broken). decomposer_agent and structure_builder_agent
        # are module-level instances — we copy their config into new instances here.
        self.decomposer = LlmAgent(
            name="DecomposerAgent",
            model=_model_fast(),
            instruction=decomposer_agent.instruction,
            tools=list(decomposer_agent.tools),
            output_key="decomposition",
            include_contents="none",
        )
        self.structure_builder = LlmAgent(
            name="StructureBuilderAgent",
            model=_model(),
            instruction=structure_builder_agent.instruction,
            tools=list(structure_builder_agent.tools),
            output_key="workflow_json",
            include_contents="none",
        )

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:

        # ── Stage 1: LLM — Decompose ────────────────────────────────────────
        async for event in self.decomposer.run_async(ctx):
            yield event

        decomposition = _ensure_dict(ctx.session.state.get("decomposition", {}))
        if not decomposition:
            ctx.session.state["_empty_response_error"] = True
            return

        # ── Stage 2: Python — Pattern match ────────────────────────────────
        ctx.session.state["pattern_match"] = run_pattern_match(decomposition)

        # ── Stage 3: Python — Retrieve activities ───────────────────────────
        activity_manifest = run_retrieval(decomposition)
        ctx.session.state["activity_manifest"] = activity_manifest

        # ── Stage 4: LLM — Assemble structure ──────────────────────────────
        async for event in self.structure_builder.run_async(ctx):
            yield event

        workflow_json = _ensure_dict(ctx.session.state.get("workflow_json", {}))
        if not workflow_json:
            ctx.session.state["_empty_response_error"] = True
            return

        # ── Stage 5: Python — Annotate ──────────────────────────────────────
        annotation_result = run_annotation(workflow_json, activity_manifest)
        ctx.session.state["annotation_result"] = annotation_result

        # ── Stage 6: Python — Validate ──────────────────────────────────────
        validation_result = run_validation(annotation_result)
        ctx.session.state["validation_result"] = validation_result

        # ── Correction retry on validation failure ──────────────────────────
        if validation_result["status"] == "invalid":
            error_lines = "\n".join(validation_result.get("errors", []))
            ctx.session.state["correction_prompt"] = (
                f"CORRECTION REQUIRED\n"
                f"The workflow you generated failed validation. "
                f"Fix ALL of the following errors and regenerate the complete workflow:\n\n"
                f"{error_lines}"
            )

            async for event in self.structure_builder.run_async(ctx):
                yield event

            corrected_json = _ensure_dict(ctx.session.state.get("workflow_json", {}))
            if not corrected_json:
                ctx.session.state["_empty_response_error"] = True
                return

            annotation_result = run_annotation(corrected_json, activity_manifest)
            ctx.session.state["annotation_result"] = annotation_result

            validation_result = run_validation(annotation_result)
            ctx.session.state["validation_result"] = validation_result

        # ── Stage 7: Python — Output JSON ───────────────────────────────────
        if validation_result["status"] == "valid":
            prompt = ctx.session.state.get("prompt", "Workflow")
            output_result = run_output(validation_result, _derive_base_name(prompt))
            ctx.session.state["output_result"] = output_result
        else:
            ctx.session.state["output_result"] = {
                "status": "failed",
                "errors": validation_result.get("errors", []),
            }


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _derive_base_name(prompt: str, max_words: int = 4) -> str:
    """
    Produce a CamelCase base name from the first few meaningful words of the prompt.
    Example: 'monitor server disk space and alert' → 'MonitorServerDiskSpace'
    """
    STOP_WORDS = {"a", "an", "the", "and", "or", "for", "to", "in", "of", "with"}
    words = [
        w.capitalize()
        for w in prompt.split()
        if w.lower() not in STOP_WORDS
    ][:max_words]
    return "".join(words) if words else "Workflow"


# ---------------------------------------------------------------------------
# Factory — called from agents/__init__.py and main.py
# ---------------------------------------------------------------------------

def build_pipeline() -> WorkflowPipeline:
    """Return a fresh WorkflowPipeline instance for each run."""
    return WorkflowPipeline(name="workflow_pipeline")