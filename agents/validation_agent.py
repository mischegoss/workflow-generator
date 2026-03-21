from google.adk.agents import LlmAgent
from google.adk.models.lite_llm import LiteLlm
from tools.validation_tools import (
    validate_xname_uniqueness,
    validate_activity_schema,
    validate_control_flow_rules,
    validate_required_fields,
    run_all_validators,
)

import os
MODEL = LiteLlm(model=os.getenv("MODEL_FAST", "anthropic/claude-haiku-4-5-20251001"))

INSTRUCTION = """
You are the validation stage of a workflow generation pipeline for Resolve Actions.

Input from session state:
- 'annotation_result': contains 'annotated_workflow_json' and 'placeholder_summary'

Your job:
1. Call run_all_validators with annotation_result['annotated_workflow_json'].
2. Evaluate the result:
   - If status == "valid": proceed to output.
   - If status == "invalid": return the error list. The pipeline will halt.

The four validators check:
- validate_xname_uniqueness: every xName must be unique across the entire workflow.
- validate_activity_schema: required fields per activities_controls.json must be present.
  Activities with no controls entry get a VERIFY note — they are not hard failures.
- validate_control_flow_rules:
    * WhileActivity must NOT have Counter at its own level
    * ExitWhile MUST have Counter
    * SequenceActivity inside WhileActivity/ForEachActivity: only xName + CustomTypeName
    * ForEachOutputVariableName must not start with 'forEach'
    * ReturnValue ConditionType must be one of: "", "Equals", "Contains", "Not Contains", "Formula"
- validate_required_fields: every leaf activity needs both Description and description.

OUTPUT FORMAT:
{
  "status": "valid" or "invalid",
  "workflow_json": { ...annotated_workflow_json if valid... },
  "placeholder_summary": [ ...passed through from annotation_result... ],
  "errors": [ ...list of error strings if invalid, empty list if valid... ],
  "verify_notes": [ ...list of VERIFY note strings... ]
}

Rules:
- If invalid, do NOT pass workflow_json — return only errors.
- verify_notes are warnings, not errors. They do not cause invalid status.
- Output only the JSON. No prose.
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