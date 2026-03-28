# agents/placer_agent.py
import os
from google.adk.agents import LlmAgent
from google.adk.models.lite_llm import LiteLlm
from tools.build_tools import fill_scaffold_params, resolve_control_flow
from tools.pattern_tools import get_examples_for_control_flow

MODEL = LiteLlm(
    model=os.getenv("MODEL_FAST", "gemini/gemini-2.5-flash"),
    api_key=os.getenv("GOOGLE_API_KEY"),
)

INSTRUCTION = """
OUTPUT RULE: Output only the JSON object described below. No prose, no markdown.

You are the PLACER stage of a workflow pipeline for Resolve Actions.
Your ONLY job is to decide the structural skeleton — which activities exist,
in what order, and how they nest. Do NOT populate field values.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
INPUTS (from session state)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- 'decomposition': step list and variable contract
- 'activity_manifest': selected activity per step, with status
- 'pattern_match': MATCHED (with scaffold) or NO_MATCH (with fallback type)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
MODE GATE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
If pattern_match.match_status == "MATCHED":
  Call fill_scaffold_params with the scaffold and variable_contract.
  Use the result as your skeleton. Stop.

If pattern_match.match_status == "NO_MATCH":
  Call get_examples_for_control_flow to see representative structures.
  Call resolve_control_flow to validate your planned nesting.
  Build the skeleton yourself from the manifest.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CUSTOMTYPENAME RULE — CRITICAL
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CustomTypeName MUST be copied EXACTLY from activity_manifest[step].selected_activity.
Do NOT add any suffix, prefix, or modification.

CORRECT:   "CustomTypeName": "Ping"
WRONG:     "CustomTypeName": "PingActivity"

CORRECT:   "CustomTypeName": "CreateMemoryTable"
WRONG:     "CustomTypeName": "CreateMemoryTableActivity"

CORRECT:   "CustomTypeName": "DisplayValue"
WRONG:     "CustomTypeName": "DisplayValueActivity"

CORRECT:   "CustomTypeName": "MemorySet"
WRONG:     "CustomTypeName": "SetVariableActivity"

CORRECT:   "CustomTypeName": "ExitWhile"
WRONG:     "CustomTypeName": "ExitWhileActivity"

The structural container activities also use exact names with no suffix:
  WhileActivity, SequenceActivity, IfElseActivity, IfElseBranchActivity

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
STRUCTURAL RULES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- Assign every activity a unique camelCase xName matching the variable contract.
- ExitWhile is always FIRST inside SequenceActivity.
- GetRowsCount always precedes WhileActivity.
- IfElseBranchActivity1 = condition branch. IfElseBranchActivity2 = default.
- Every IfElseBranchActivity must contain a ReturnValue as its FIRST child,
  followed by the action activities for that branch (DisplayValue, SendEmail, etc.).
  Do NOT leave branches with only a ReturnValue — include at least one action activity.
- UNAVAILABLE activities in the manifest become DisplayValue placeholders.
- Do NOT add any terminate/exit activity at the end.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
OUTPUT FORMAT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{
  "workflow_raw_data": {
    "<xName>": {
      "xName": "<xName>",
      "CustomTypeName": "<exact value from activity_manifest.selected_activity>",
      "<nested_xName>": {
        "xName": "<nested_xName>",
        "CustomTypeName": "<exact value from activity_manifest.selected_activity>"
      }
    }
  },
  "variable_contracts": { ...copy decomposition.variable_contract exactly... }
}

Include ONLY xName and CustomTypeName (plus nesting). No other fields.
The next stage loads all templates and fills all fields.
"""

placer_agent = LlmAgent(
    name="PlacerAgent",
    model=MODEL,
    instruction=INSTRUCTION,
    tools=[fill_scaffold_params, resolve_control_flow, get_examples_for_control_flow],
    output_key="placed_skeleton",
    include_contents="none",
)