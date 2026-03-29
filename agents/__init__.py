import os

from google.adk.agents import LlmAgent
from google.adk.models.lite_llm import LiteLlm

# LiteLlm routes gemini/ models to Google AI Studio using GEMINI_API_KEY specifically.
# If only GOOGLE_API_KEY is set, copy it so LiteLlm can find it.
if not os.environ.get("GEMINI_API_KEY") and os.environ.get("GOOGLE_API_KEY"):
    os.environ["GEMINI_API_KEY"] = os.environ["GOOGLE_API_KEY"]

from agents.pipeline import (
    build_pipeline,
    build_correction_pipeline,
    WorkflowPipeline,
    CorrectionPipeline,
    _model_decomposer,
    _model_wirer,
)

__all__ = [
    "build_pipeline",
    "build_correction_pipeline",
    "WorkflowPipeline",
    "CorrectionPipeline",
    "_model_decomposer",
    "_model_wirer",
]