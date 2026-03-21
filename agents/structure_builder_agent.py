import os
import json
from google.adk.agents import LlmAgent
from google.adk.models.lite_llm import LiteLlm
from tools.build_tools import (
    load_activity_template, resolve_control_flow,
    build_activity_json, fill_scaffold_params,
)
from tools.pattern_tools import get_examples_for_control_flow

MODEL = LiteLlm(model=os.getenv("MODEL", "anthropic/claude-sonnet-4-5-20250929"))

# Fields to keep when trimming examples for injection — enough structure to follow,
# lean enough to not blow the context window
_KEEP_FIELDS = {
    "xName", "CustomTypeName", "name", "IsValid", "Description", "description",
    "TypeName", "Counter", "exitWhileInsideWhile", "isValid", "whileSequenceActivity",
    "ResultSet", "ResultSetName", "RowNumber", "ColumnNumber", "ColumnType",
    "VariableName", "VariableValue", "VariableScope", "IsSaved", "IsAppend",
    "ValueToDisplay", "FuturePast", "TimeInterval", "TimeToAdd", "DateFormat",
    "TimeZoneName", "FirstDate", "SecondDate", "ReturnFormat", "TableName",
    "Formula", "ConditionType", "Type", "Value", "UseBranchWhenTimeout",
    "To", "Subject", "Body", "MessageType", "DestinationType", "DestinationNumber",
    "TemplateNumber", "IsNowSelected", "FirstDateFormat", "SecondDateFormat",
    "Condition",
}


def _trim(node):
    """Strip verbose fields from an activity dict, keeping only structurally important ones."""
    if not isinstance(node, dict):
        return node
    result = {}
    for k, v in node.items():
        if isinstance(v, dict):
            result[k] = _trim(v)
        elif k in _KEEP_FIELDS:
            result[k] = v
    return result


def _load_example(control_flow_type: str, index: int) -> str:
    """Load and trim a single example workflow for injection into the instruction."""
    data_dir = os.getenv("DATA_DIR", "/app/data")
    path = os.path.join(data_dir, "examples", f"example_{control_flow_type}_{index}.json")
    if not os.path.exists(path):
        return "{}"
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    trimmed = _trim(data.get("workflow_raw_data", {}))
    return json.dumps(trimmed, indent=2)


# Load examples at module import time so they are baked into the instruction.
# while_ifelse_4: CreateMemoryTable → GetRowsCount → WhileActivity → GetCellValue →
#                 VMPowerState → IfElseActivity → (action branch + Continue default)
# This is the closest structural match to the cert workflow pattern.
_EXAMPLE_WHILE_IFELSE = _load_example("while_ifelse", 4)

# while_5: CreateMemoryTable → GetRowsCount → WhileActivity → GetCellValue →
#          [loop body] → GetDate → WriteXLS → ConvertToHTMLTable → SendEmail
# Shows SendEmail after a loop — confirms post-loop action pattern.
_EXAMPLE_WHILE_WITH_EMAIL = _load_example("while", 5)


INSTRUCTION = f"""
You are the structure builder stage of a workflow generation pipeline for Resolve Actions.

Inputs from session state:
- 'decomposition': step list and variable contract
- 'activity_manifest': selected activity per step with MATCHED/UNAVAILABLE/CONTROL_FLOW status
- 'pattern_match': either a matched scaffold or NO_MATCH with fallback control flow type

You operate in one of two modes:

═══════════════════════════════════════════════
MODE 1 — SCAFFOLD-FILL (pattern_match.match_status == "MATCHED")
═══════════════════════════════════════════════
1. Call fill_scaffold_params with the scaffold and variable contract.
   Fill ONLY the PARAM_ fields. Do NOT add, remove, or reorder activities.
2. Return the filled workflow JSON.

═══════════════════════════════════════════════
MODE 2 — EXAMPLE-GUIDED (pattern_match.match_status == "NO_MATCH")
═══════════════════════════════════════════════
1. Call get_examples_for_control_flow using pattern_match.fallback_examples[0].
   Valid values: "Linear", "IfElse", "While", "while_ifelse", "UserGroup".
   Use max_examples=2.
2. Call load_activity_template for each MATCHED activity in the manifest.
3. Call resolve_control_flow with the step list to validate nesting.
4. Call build_activity_json to assemble the final workflow dict.
5. Return the assembled workflow JSON.

═══════════════════════════════════════════════
CONFIRMED WORKING EXAMPLE A — While + IfElse pattern
(source: 130_Create power off VMs List.xml — confirmed importable)
Use this as your structural template for any workflow with a loop + branch:
═══════════════════════════════════════════════
{_EXAMPLE_WHILE_IFELSE}

Key things to observe in Example A:
- GetRowsCount xName="getRowsCount1", ExitWhile Counter="%getRowsCount1%"
- GetCellValue RowNumber="%exitWhile1%"  ← ExitWhile xName, NOT WhileActivity xName
- IfElseActivity is INSIDE SequenceActivity which is INSIDE WhileActivity
- Default branch has ReturnValue Type="StoredValue" + Continue activity
- SequenceActivity has full attributes: name, IsValid, Description, description

═══════════════════════════════════════════════
CONFIRMED WORKING EXAMPLE B — While loop with SendEmail after loop
(source: 133_Create VM Capacity Report.xml — confirmed importable)
Use this when the workflow sends an email as a final action after processing:
═══════════════════════════════════════════════
{_EXAMPLE_WHILE_WITH_EMAIL}

Key things to observe in Example B:
- SendEmail comes AFTER the WhileActivity closes, not inside the loop
- ConvertToHTMLTable feeds into SendEmail Body
- GetDate for timestamps appears after the loop

═══════════════════════════════════════════════
COMPLETENESS CHECK — verify before returning:
- Every step in decomposition.steps has a corresponding activity in workflow_raw_data.
- If the prompt mentions sending an email → SendEmail MUST be present.
- If the prompt mentions a condition, branch, or check → IfElseActivity MUST be present.
- If the prompt mentions calculating days or date difference → DateDifference MUST be present.
- If any step is missing, add it before returning.
- The workflow must implement the FULL prompt — never return a partial workflow.

═══════════════════════════════════════════════
PLATFORM RULES — enforce exactly:

LOOP STRUCTURE:
- Always use WhileActivity for loops. ForEachActivity does not exist in this corpus.
- Always precede WhileActivity with GetRowsCount.
- ExitWhile Counter = "%<getRowsCountXName>%"
- ExitWhile requires: exitWhileInsideWhile="True", isValid="True",
  TypeName="ExitWhile", whileSequenceActivity="<sequenceActivityXName>"

ROW ACCESS — MOST CRITICAL RULE:
- GetCellValue RowNumber MUST reference the ExitWhile xName, NOT the WhileActivity xName.
- CORRECT:  ExitWhile xName="exitWhile1" → GetCellValue RowNumber="%exitWhile1%"
- WRONG:    WhileActivity xName="loopCerts1" → GetCellValue RowNumber="%loopCerts1%"

SEQUENCE ACTIVITY:
- SequenceActivity inside WhileActivity needs full attributes:
  xName, CustomTypeName, name, IsValid, Description, description (see Example A).
- Do NOT strip attributes from SequenceActivity.

XNAME RULES:
- Every xName must be unique, alphanumeric, camelCase, no spaces or symbols.
- Both uppercase Description AND lowercase description required on every activity.
- WhileActivity carries NO Counter attribute — Counter belongs ONLY on ExitWhile.

IFELSE STRUCTURE:
- IfElseActivity contains IfElseBranchActivity children.
- Each IfElseBranchActivity: ReturnValue first, then branch activities.
- Valid ConditionType values: "", "Equals", "Contains", "Not Contains",
  "Not Equals", "Formula", ">", "<", ">=", "<="
- Default branch: Type="StoredValue", ConditionType="", Formula=null,
  Value="", IsValid="False", UseBranchWhenTimeout="True"
- Use Continue activity when a branch should skip to next loop iteration (see Example A).

VARIABLE REFERENCES:
- Use ONLY variable names from decomposition.variable_contract.variables.
- Variable references use %variableName% syntax.
- Never invent variable names not in the contract.

CERT WORKFLOW PATTERN — for date-check + email workflows:
CreateMemoryTable → GetRowsCount → GetDate → WhileActivity →
  SequenceActivity (full attributes) →
    ExitWhile (Counter=%getRowsCountXName%, exitWhileInsideWhile="True")
    GetCellValue (RowNumber=%exitWhileXName%)  ← ExitWhile xName
    GetCellValue
    DateDifference
    IfElseActivity →
      IfElseBranchActivity (condition days <= 5) →
        ReturnValue (ConditionType="<=", Type="UserDefinedValue")
        SendEmail
      IfElseBranchActivity (default) →
        ReturnValue (Type="StoredValue", UseBranchWhenTimeout="True")
        Continue

OUTPUT:
{{
  "workflow_raw_data": {{
    "<xName>": {{ ...activity object... }},
    ...
  }},
  "variable_contracts": {{ ...from decomposition... }}
}}

Output only the JSON. No prose.
"""

structure_builder_agent = LlmAgent(
    name="StructureBuilderAgent",
    model=MODEL,
    instruction=INSTRUCTION,
    tools=[
        load_activity_template,
        resolve_control_flow,
        build_activity_json,
        fill_scaffold_params,
        get_examples_for_control_flow,
    ],
    output_key="workflow_json",
)
