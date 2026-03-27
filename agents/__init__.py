import os

from google.adk.agents import LlmAgent
from google.adk.models.lite_llm import LiteLlm

# LiteLlm routes gemini/ models to Google AI Studio using GEMINI_API_KEY specifically.
# If only GOOGLE_API_KEY is set, copy it so LiteLlm can find it.
if not os.environ.get("GEMINI_API_KEY") and os.environ.get("GOOGLE_API_KEY"):
    os.environ["GEMINI_API_KEY"] = os.environ["GOOGLE_API_KEY"]

from agents.pipeline import build_pipeline, WorkflowPipeline

__all__ = ["build_pipeline", "WorkflowPipeline", "_model", "_model_fast"]


def _model():
    """
    WirerAgent — LiteLlm with Gemini 2.5 Pro.
    Native Gemini() class causes 400 errors on tool schema serialization
    (additional_properties=null ADK bug). Using LiteLlm until ADK fixes this.
    Pro chosen for strongest semantic reasoning on field wiring tasks.
    temperature=0.1: low creativity, high determinism for field population.
    api_key passed explicitly to force Google AI Studio routing.
    """
    return LiteLlm(
        model=os.getenv("MODEL", "gemini/gemini-2.5-pro"),
        max_tokens=8192,
        temperature=0.1,
        api_key=os.getenv("GOOGLE_API_KEY"),
    )


def _model_fast():
    """
    DecomposerAgent + PlacerAgent — LiteLlm with Gemini 2.5 Flash.
    Flash is sufficient for decomposition and structural placement.
    PlacerAgent output is a tiny skeleton (xName + CustomTypeName only)
    so token cost is low and Flash handles it reliably.
    api_key passed explicitly to force Google AI Studio routing.
    """
    return LiteLlm(
        model=os.getenv("MODEL_FAST", "gemini/gemini-2.5-flash"),
        api_key=os.getenv("GOOGLE_API_KEY"),
    )
    