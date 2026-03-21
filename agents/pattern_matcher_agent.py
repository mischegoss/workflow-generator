from google.adk.agents import LlmAgent
from google.adk.models.lite_llm import LiteLlm
from tools.pattern_tools import load_pattern_library, match_pattern, score_pattern_match

import os
MODEL = LiteLlm(model=os.getenv("MODEL_FAST", "anthropic/claude-haiku-4-5-20251001"))

INSTRUCTION = """
You are the pattern matching stage of a workflow generation pipeline for Resolve Actions.

Input: 'decomposition' from session state — contains steps and variable_contract.

Your job:
1. Call load_pattern_library to load the mined pattern library.
2. Call match_pattern with the decomposition to find candidate patterns.
3. Call score_pattern_match with the candidates to apply the threshold gate.

OUTPUT FORMAT — return exactly this JSON:
{
  "match_status": "MATCHED" or "NO_MATCH",
  "pattern_id": "<id>" or null,
  "pattern_name": "<name>" or null,
  "score": 0.0 to 1.0,
  "scaffold": { ...full pattern JSON with PARAM_ fields... } or null,
  "fallback_examples": ["While"] or ["IfElse"] or ["Linear"] etc — control flow type for example selection
}

Rules:
- NEVER invent a pattern. Only return patterns from the library.
- If the library is empty or no candidates score above threshold, always return NO_MATCH.
- If NO_MATCH, set fallback_examples to a list with the detected control flow type
  from decomposition.variable_contract.loop_type so StructureBuilder picks the right examples.
- Output only the JSON object. No prose.
"""

pattern_matcher_agent = LlmAgent(
    name="PatternMatcherAgent",
    model=MODEL,
    instruction=INSTRUCTION,
    tools=[load_pattern_library, match_pattern, score_pattern_match],
    output_key="pattern_match",
)