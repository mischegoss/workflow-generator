import os
import json
from google.adk.models.lite_llm import LiteLlm
from google.adk.agents import LlmAgent
from tools.build_tools import (
    load_activity_template, resolve_control_flow,
    build_activity_json, fill_scaffold_params,
)
from tools.pattern_tools import get_examples_for_control_flow

# LiteLlm used instead of native Gemini() class — the native class causes
# 400 errors on tool schema serialization (additional_properties=null ADK bug).
# temperature=0.2: reduced creativity on the most complex assembly task.
MODEL = LiteLlm(
    model=os.getenv("MODEL", "gemini/gemini-2.5-pro"),
    temperature=0.2,
)

# Fields to keep when trimming examples for injection
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
    "Condition", "UseStoredValue",
}


def _trim(node):
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
    data_dir = os.getenv("DATA_DIR", "/app/data")
    path = os.path.join(data_dir, "examples", f"example_{control_flow_type}_{index}.json")
    if not os.path.exists(path):
        return "{}"
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    trimmed = _trim(data.get("workflow_raw_data", {}))
    return json.dumps(trimmed, indent=2)


_EXAMPLE_WHILE_IFELSE = _load_example("while_ifelse", 4)
_EXAMPLE_WHILE_WITH_EMAIL = _load_example("while", 5)


INSTRUCTION = f"""
OUTPUT RULE: Output only the JSON object described below. No prose, no explanation, no markdown.

You are the structure builder stage of a workflow generation pipeline for Resolve Actions.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CONTEXT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
You have no memory of previous conversations. Your only inputs are the session state keys
listed below. Do not assume or invent any information not present in session state.

Session state inputs:
- 'decomposition': step list and variable contract (from DecomposerAgent)
- 'activity_manifest': compact list — each entry has step_id, selected_activity, status,
  and frequency_tier. Some entries also include pre_filled_fields: a dict of
  field_key -> value pairs confirmed by corpus analysis. Treat these as authoritative.
- 'pattern_match': scaffold or NO_MATCH with fallback type (from PatternMatcherAgent)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CORRECTION MODE — check this before doing anything else
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
If the user message contains the text "CORRECTION REQUIRED", this is a retry run.
The section after "CORRECTION REQUIRED" lists specific errors from the previous attempt.

On a retry run:
1. Read every error in the correction list carefully.
2. Identify which activities or fields caused each error.
3. Fix every listed error in your output. Do not reproduce any error from the list.
4. Then proceed with the normal MODE GATE and tool sequence below.

If the user message does NOT contain "CORRECTION REQUIRED", proceed normally.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
AVAILABLE TOOLS (these are the only tools you may call)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- load_activity_template
- resolve_control_flow
- build_activity_json
- fill_scaffold_params
- get_examples_for_control_flow

Do NOT call any tool not listed above.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
MODE GATE — do this first, before calling any tool
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Read pattern_match.match_status.
- If "MATCHED": you are in MODE 1. Follow ONLY the MODE 1 steps.
- If "NO_MATCH": you are in MODE 2. Follow ONLY the MODE 2 steps.
Do not mix steps from both modes.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
MODE 1 — SCAFFOLD-FILL (pattern_match.match_status == "MATCHED")
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Step 1. Call fill_scaffold_params with the scaffold from pattern_match and the variable
        contract from decomposition. Fill ONLY the PARAM_ fields. Do NOT add, remove,
        or reorder activities.
Step 2. Run the PRE-OUTPUT CHECKLIST below.
Step 3. Return the output JSON.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
MODE 2 — EXAMPLE-GUIDED (pattern_match.match_status == "NO_MATCH")
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Step 1. Call get_examples_for_control_flow with:
        - control_flow_type = pattern_match.fallback_examples[0]
        - Valid values: "Linear", "IfElse", "While", "while_ifelse", "UserGroup"
        - max_examples = 2
Step 2. Call load_activity_template for each MATCHED activity in the activity_manifest.
        Use the selected_activity field from each entry where status == "MATCHED".
        If load_activity_template returns an empty dict, treat that activity as UNAVAILABLE.
Step 3. Call resolve_control_flow with the steps list from decomposition.
Step 4. Call build_activity_json to assemble the final workflow dict.
Step 5. Run the PRE-OUTPUT CHECKLIST below.
Step 6. Return the output JSON.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PRE-OUTPUT CHECKLIST — verify all 10 items before returning
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. Every xName in the output is unique. If any two share an xName, rename one.
2. Every activity has both "Description" (uppercase D) and "description" (lowercase d).
3. No activity uses ForEachActivity. Use WhileActivity for all iteration.
4. Every WhileActivity is preceded by GetRowsCount in activity order.
5. No WhileActivity has a Counter attribute — Counter belongs only on ExitWhile.
6. Every ExitWhile has: Counter="%<getRowsCountXName>%", exitWhileInsideWhile="True",
   isValid="True", TypeName="ExitWhile", whileSequenceActivity="<sequenceActivityXName>".
7. Every GetCellValue RowNumber references the WhileActivity xName, NOT the ExitWhile xName.
8. Every %varName% reference exists in decomposition.variable_contract.variables.
   If not in the contract, remove it or replace with a PLACEHOLDER_.
9. Every SequenceActivity inside a WhileActivity has full attributes:
   xName, CustomTypeName, name, IsValid, Description, description.
10. Every empty IfElse branch contains only ReturnValue — no Continue, DisplayValue, or filler.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PLATFORM RULES — enforce exactly
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

SCOPE: Structure only. Do NOT annotate credentials. Leave credential fields as they
come from templates. AnnotationAgent handles credentials downstream.

LOOP STRUCTURE:
- ONLY WhileActivity for loops. ForEachActivity does NOT exist (625 real workflows confirm this).
- Always precede WhileActivity with GetRowsCount.
- ExitWhile Counter = "%<getRowsCountXName>%"
- ExitWhile requires: exitWhileInsideWhile="True", isValid="True",
  TypeName="ExitWhile", whileSequenceActivity="<sequenceActivityXName>"

PRE-FILLED FIELDS — treat as authoritative:
If a manifest entry contains a pre_filled_fields dict, those field values have been
confirmed by analysis of 609 real workflows. Use them exactly as provided.
Do NOT override, ignore, or second-guess pre_filled_fields values.
Fields not covered by pre_filled_fields are filled using your normal assembly logic.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ROW ACCESS — MOST CRITICAL RULE:
- GetCellValue RowNumber MUST reference the WhileActivity xName.
- Confirmed from corpus analysis of 609 real workflows: WhileActivity used 188 times,
  ExitWhile used 83 times. WhileActivity xName is the correct and dominant pattern.
- CORRECT:  WhileActivity xName="loopServers" → GetCellValue RowNumber="%loopServers%"
- WRONG:    ExitWhile xName="exitWhile1"      → GetCellValue RowNumber="%exitWhile1%"

SEQUENCE ACTIVITY:
- SequenceActivity inside WhileActivity needs full attributes:
  xName, CustomTypeName, name, IsValid, Description, description.
- Do NOT strip attributes from SequenceActivity.

XNAME RULES:
- Every xName must be unique, alphanumeric, camelCase, no spaces or symbols.
- Both uppercase Description AND lowercase description required on every activity.
- WhileActivity carries NO Counter attribute — Counter belongs ONLY on ExitWhile.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
GETCELLVALUE COLUMN RULES — fix for Issue 1
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- ColumnType is always "Name" — the serializer sets this automatically. Do NOT set it.
- ColumnNumber MUST be the EXACT column name from the prompt or variable contract —
  never a number index. Read the column name from the user's prompt description.
- CORRECT: prompt says "a column called 'server'" → ColumnNumber="server"
- CORRECT: prompt says "the hostname column" → ColumnNumber="hostname"
- WRONG:   ColumnNumber="1" — a numeric index will silently return empty with ColumnType=Name.
- If the prompt does not name the column, use the most specific name from the description
  (e.g. "host" for a hostnames column, "name" for a names column).

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
IFELSE STRUCTURE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- IfElseActivity contains IfElseBranchActivity children.
- Each IfElseBranchActivity: ReturnValue first, then branch activities (if any).
- Valid ConditionType values: "" | "Equals" | "Contains" | "Not Contains" | "Not Equals" |
  "Formula" | ">" | "<" | ">=" | "<="
- Default branch: Type="StoredValue", ConditionType="", Value="", IsValid="False",
  UseBranchWhenTimeout="True"

RETURNVALUE FIELD RULES — fix for Issue 4:
- Type must ALWAYS be "StoredValue". This is the only confirmed-working value across
  625 real workflows. "UserDefinedValue" does NOT work for runtime comparisons.
- UseStoredValue:
    Condition branch (non-default): UseStoredValue="True"
    Default branch:                 omit or set to "False"
- UseBranchWhenTimeout:
    Condition branch: UseBranchWhenTimeout="False"
    Default branch:   UseBranchWhenTimeout="True"
- Formula: Do NOT set. Leave it out entirely — the serializer computes it.

EMPTY BRANCH RULE:
- Empty branch = ONLY ReturnValue. No Continue, DisplayValue, or filler.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ACTIVITY OUTPUT VALUES — fix for Issue 5
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
When an IfElseActivity branches on the result of a preceding activity, use these
confirmed success values for the condition branch ReturnValue.Value field.
These are the exact strings the platform returns — using any other value means
the condition branch will never fire.

  Ping              → Value="Success",  ConditionType="Equals"
  ServiceStatus     → Value="Running",  ConditionType="Equals"
  ServiceStart      → Value="Success",  ConditionType="Equals"
  ServiceStop       → Value="Success",  ConditionType="Equals"
  FileExist         → Value="True",     ConditionType="Equals"
  URLCheck          → Value="Success",  ConditionType="Equals"
  ADIsAccountLocked → Value="True",     ConditionType="Equals"
  ADIsAccountDisabled → Value="True",   ConditionType="Equals"
  Contains          → Value="True",     ConditionType="Equals"
  IsEmpty           → Value="True",     ConditionType="Equals"

For activities not in this list, read the description of the condition in the prompt
and use "Equals" with the most specific literal value mentioned.
If the prompt says "if the result contains X", use ConditionType="Contains", Value="X".
If the prompt says "if it fails", use the default branch — do not invent a failure value.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
WORKFLOW TERMINATION:
- Do NOT add any end, terminate, or exit activity at the end of the workflow.

VARIABLE REFERENCES:
- Use ONLY names from decomposition.variable_contract.variables.
- Syntax: %variableName%. Never invent names not in the contract.

CERT WORKFLOW PATTERN — for date-check + email workflows:
CreateMemoryTable → GetRowsCount → GetDate → WhileActivity →
  SequenceActivity (full attributes) →
    ExitWhile (Counter=%getRowsCountXName%, exitWhileInsideWhile="True")
    GetCellValue (RowNumber=%whileActivityXName%)  ← WhileActivity xName, ColumnNumber=<column name from prompt>
    GetCellValue
    DateDifference
    IfElseActivity →
      IfElseBranchActivity (condition) → ReturnValue (Type="StoredValue") + action activity
      IfElseBranchActivity (default)   → ReturnValue only, nothing else

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
EXAMPLE A — while_ifelse structure:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{_EXAMPLE_WHILE_IFELSE}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
EXAMPLE B — while with post-loop email:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{_EXAMPLE_WHILE_WITH_EMAIL}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
OUTPUT FORMAT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{{
  "workflow_raw_data": {{
    "<xName>": {{ ...activity object... }},
    ...
  }},
  "variable_contracts": {{ ...copy decomposition.variable_contract exactly, without modification... }}
}}
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
