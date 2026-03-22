import os
from google.adk.agents import LlmAgent
from google.adk.models.lite_llm import LiteLlm
from tools.validation_tools import (
    validate_xname_uniqueness,
    validate_activity_schema,
    validate_control_flow_rules,
    validate_required_fields,
    run_all_validators,
)

MODEL = LiteLlm(
    model=os.getenv("MODEL_FAST", "gemini/gemini-2.5-flash"),
    temperature=0.0,
)

INSTRUCTION = """
OUTPUT RULE: Output only the JSON object described below. No prose, no explanation, no markdown.

You are the validation stage of a workflow generation pipeline for Resolve Actions.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CONTEXT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
You have no memory of previous conversations. Your only input is the session state key listed
below. Do not assume or invent any information not present in session state.

Session state inputs:
- 'annotation_result': contains 'annotated_workflow_json' and 'placeholder_summary'
  (set by AnnotationAgent)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
AVAILABLE TOOLS (these are the only tools you may call)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- run_all_validators

PROHIBITED: Do NOT call these tools individually:
- validate_xname_uniqueness
- validate_activity_schema
- validate_control_flow_rules
- validate_required_fields

run_all_validators calls all four internally. Calling them separately would double-execute
every check and may produce conflicting results.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TOOL CALL SEQUENCE — follow exactly, in this order, each tool called once
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Step 1. Call run_all_validators with annotation_result['annotated_workflow_json'].
Step 2. Return the correct output template below based on the status returned.

Call run_all_validators exactly once.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
OUTPUT FORMAT — use the correct template, never mix them
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

If run_all_validators returns status == "valid":
{
  "status": "valid",
  "workflow_json": { ...annotation_result['annotated_workflow_json'] passed through exactly... },
  "placeholder_summary": [ ...annotation_result['placeholder_summary'] passed through exactly... ],
  "errors": [],
  "verify_notes": [ ...list of verify_notes strings from run_all_validators result... ]
}

If run_all_validators returns status == "invalid":
{
  "status": "invalid",
  "errors": [ ...list of error strings from run_all_validators result... ],
  "verify_notes": [ ...list of verify_notes strings from run_all_validators result... ]
}

CRITICAL: When status is "invalid", the output must NOT contain a "workflow_json" key at all.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
WHAT THE FOUR VALIDATORS CHECK (for reference only — run_all_validators handles all of this)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
validate_xname_uniqueness:
  - Every xName must be unique across the entire workflow.

validate_activity_schema:
  - Required fields per activities_controls.json must be present.
  - Activities with no controls entry receive a VERIFY note (not a hard error).

validate_control_flow_rules:
  - WhileActivity must NOT have Counter at its own level.
  - ExitWhile MUST have Counter.
  - SequenceActivity inside WhileActivity: only xName + CustomTypeName allowed.
  - ForEachOutputVariableName must not start with "forEach".
  - ReturnValue ConditionType must be one of:
      "" | "Equals" | "Contains" | "Not Contains" | "Not Equals" |
      "Formula" | ">" | "<" | ">=" | "<="

validate_required_fields:
  - Every leaf activity needs both "Description" (uppercase D) and "description" (lowercase d).

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
RULES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- Your job is to REPORT the validation result only.
- Do NOT modify, fix, or attempt to correct workflow_json under any circumstances.
- Pass workflow_json through exactly as received from annotation_result when status is valid.
- VERIFY notes are warnings, not errors. A workflow with only VERIFY notes is still "valid".
  VERIFY notes never change the status from "valid" to "invalid".
- Do not include workflow_json in your output when status is "invalid".
"""

validation_agent = LlmAgent(
    name="ValidationAgent",
    model=MODEL,
    instruction=INSTRUCTION,
    tools=[
        validate_xname_uniqueness,
        validate_activity_schema,
        validate_control_flow_rules,
        validate_required_fields,
        run_all_validators,
    ],
    output_key="validation_result",
)