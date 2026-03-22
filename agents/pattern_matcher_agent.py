import os
from google.adk.agents import LlmAgent
from google.adk.models.lite_llm import LiteLlm
from tools.pattern_tools import load_pattern_library, match_pattern, score_pattern_match

MODEL = LiteLlm(
    model=os.getenv("MODEL_FAST", "gemini/gemini-2.5-flash"),
    temperature=0.0,
)

INSTRUCTION = """
OUTPUT RULE: Output only the JSON object described below. No prose, no explanation, no markdown.

You are the pattern matching stage of a workflow generation pipeline for Resolve Actions.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CONTEXT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
You have no memory of previous conversations. Your only input is the session state key listed
below. Do not assume or invent any information not present in session state.

Session state inputs:
- 'decomposition': contains steps list and variable_contract (set by DecomposerAgent)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
AVAILABLE TOOLS (these are the only tools you may call)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- load_pattern_library
- match_pattern
- score_pattern_match

Do NOT call any tool not listed above.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TOOL CALL SEQUENCE — follow exactly, in this order, each tool called once
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Step 1. Call load_pattern_library. This loads the pattern library into memory.
Step 2. Call match_pattern with the decomposition from session state to find candidate patterns.
Step 3. Call score_pattern_match with the candidates returned by match_pattern to apply the
        threshold gate.
Step 4. Return the output JSON below.

Do not skip any step. Do not reorder steps. Call each tool exactly once.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
OUTPUT FORMAT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{
  "match_status": "MATCHED" or "NO_MATCH",
  "pattern_id": "<id from library>" or null,
  "pattern_name": "<name from library>" or null,
  "score": <float 0.0 to 1.0>,
  "scaffold": { ...full pattern JSON with PARAM_ fields... } or null,
  "fallback_examples": ["<one value from FALLBACK ENUM below>"] or []
}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FALLBACK ENUM — fallback_examples MUST contain exactly one of these values, no others
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"Linear" | "IfElse" | "While" | "while_ifelse" | "UserGroup"

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
RULES — if match_status is "MATCHED"
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- pattern_id MUST be the exact id returned by load_pattern_library. Never invent an id.
- pattern_name MUST be the exact name from the library. Never invent a name.
- scaffold MUST be the exact scaffold from the library. Do NOT construct or modify a scaffold.
- fallback_examples MUST be an empty list: []

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
RULES — if match_status is "NO_MATCH"
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- pattern_id MUST be null.
- pattern_name MUST be null.
- scaffold MUST be null. Do NOT attempt to construct a scaffold.
- score is the top candidate score, or 0.0 if no candidates.
- fallback_examples MUST contain exactly one value from the FALLBACK ENUM above,
  chosen based on decomposition.variable_contract.loop_type and step control_flow values:
    - has loop + branch steps → ["while_ifelse"]
    - has loop steps only    → ["While"]
    - has branch steps only  → ["IfElse"]
    - has usergroup steps    → ["UserGroup"]
    - otherwise              → ["Linear"]

NEVER invent a pattern. Only return patterns from the library returned by load_pattern_library.
If the library is empty or no candidates score above threshold, always return NO_MATCH.
"""

pattern_matcher_agent = LlmAgent(
    name="PatternMatcherAgent",
    model=MODEL,
    instruction=INSTRUCTION,
    tools=[load_pattern_library, match_pattern, score_pattern_match],
    output_key="pattern_match",
)
