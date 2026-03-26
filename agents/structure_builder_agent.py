import os
import json
from google.adk.models.lite_llm import LiteLlm
from google.adk.agents import LlmAgent
from tools.build_tools import (
    load_activity_template, resolve_control_flow,
    build_activity_json, fill_scaffold_params,
)
from tools.pattern_tools import get_examples_for_control_flow

MODEL = LiteLlm(
    model=os.getenv("MODEL", "gemini/gemini-2.5-pro"),
    temperature=0.2,
)

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

_EXAMPLE_WHILE_IFELSE     = _load_example("while_ifelse", 4)
_EXAMPLE_WHILE_WITH_EMAIL = _load_example("while", 5)
_EXAMPLE_LINEAR           = _load_example("linear", 4)
_EXAMPLE_IFELSE           = _load_example("ifelse", 4)

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
  Entries with keys prefixed "_wire_hint_" are wiring hints — they name the source
  activity TYPE that typically feeds a given field (e.g. "_wire_hint_ResultSet":
  "TSQLQuery:91pct"). Use these as context when choosing which upstream %xName% to
  wire into a field. They are guidance, not authoritative values.
- 'pattern_match': scaffold or NO_MATCH with fallback type (from PatternMatcherAgent)
-  Entries may also include 'prerequisite_note': a warning that a required table
  or session provider is missing from the steps preceding this one. If present,
  add the indicated provider activity immediately before the flagged step before
  assembling the workflow structure.

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
10. Every condition branch ReturnValue uses the correct Type for its preceding activity —
    consult the RETURNVALUE TYPE RULES table below before setting Type on any ReturnValue.

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

WIRING HINTS — use as context:
Manifest entries may contain keys prefixed "_wire_hint_<fieldName>" in pre_filled_fields.
These are NOT field values — they name the source activity type that typically feeds
that field (format: "ActivityType:pct"). Use them to identify which upstream activity's
%xName% should wire into the field. Example: "_wire_hint_ResultSet": "TSQLQuery:91pct"
means 91% of the time, ResultSet is wired from a TSQLQuery activity. Look for a
TSQLQuery in the manifest and wire its xName into ResultSet.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CONTROLS — STRUCTURAL ONLY, NO OUTPUT VARIABLE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
The following are Controls. They determine workflow flow and structure.
They do NOT produce a %xName% output variable. Never reference any of these
as %xName% in a downstream activity field.

  Clean Memory, Display Multi Value, Display Value, Exit While, For Each,
  Goto, If-Else, Lock Executor, Multi Memory Set, Parallel, Run Workflow,
  Stop Workflow, Terminate, Terminate Workflow, Unlock Executor, While,
  Workflow Counter

TWO EXCEPTIONS — these controls produce a named variable, but NOT via xName:

  Memory Set        → variable is %VariableName% where VariableName is the value
                      of the VariableName field. Never use %xName% for Memory Set.
  Create Memory Table → variable is %TableName% where TableName is the value of
                        the TableName field. Never use %xName% for Create Memory Table.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ACTIVITY OUTPUT TYPES — consult before wiring any variable
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Every regular (non-Control) activity stores its result in %xName%.
Before wiring %xName% into a downstream field, determine the outputType:

RULE: If the upstream activity is in the DataTable list below, its output is a
DataTable. It must feed into a table-consuming field (ResultSet, ResultSetName,
or ForEach Table Variable). You cannot use it directly as a string value.
To extract a value from it, use Get Rows Count or Get Cell Value first.

RULE: If the upstream activity is NOT in the DataTable list, its output is a
scalar value. Use %xName% directly in string fields, conditions, ExitWhile
Counter, or display values.

DATATABLE PRODUCERS — these activities return a DataTable:
  AD LDAP Query, BMC Helix Remedyforce Get Record, BMC Remedyforce Get Record,
  BMC TrueSight Operations Management Get Event, Convert Text To Table,
  Convert to DB Statement, Create Memory Table, DB2 Query, ESM Get Alert,
  ESM Get Event, Get CPU, Get Interfaces Status, Get Windows Event Logs,
  HP ArcSight Get Case, HP ArcSight Get Event, HPOM Get Alert, HPOM Get Annotations,
  HPSM Query Entry, HTTP Request, Hash Check, HyperV Info, HyperV List,
  IBMTO Get Alert, JSON Get Key Paths, JSON to Table, Jira Get Issue,
  List Folder, Match Regular Expression, MySQL Query, Oracle Query, Ping Latency,
  QRadar Get Event Details, QRadar Get Offense, QRadar Get Offense Events,
  Read Excel, Read File, Remove Empty Rows And Columns From Table, SN Get Record,
  Self Service Response, SolarWinds NPM Get Alert, SolarWinds NPM Get Node,
  Splunk Get Alert, Splunk Get Alert Events, Splunk Get Events, Splunk Get Report,
  Sub String by Text, Submit File, TSQL Query, URL Check, VM Host List,
  WMI Query, Write CSV, Write Excel, XML Elements to Table, XML to Table

BOOLEAN PRODUCERS — these activities return True or False as a scalar:
  AD Is Account Disabled, AD Is Account Locked, AD User Exists, Contains,
  File Checksum Comparison, File Exists, File Writable, Folder Exists,
  Is Alert in HTML Format, Is Empty, Is Numeric

ALL OTHER activities return a scalar value (string, ID, date, count, path, etc.).

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
RESULTSET vs RESULTSETNAME — dual-field rule
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Activities that operate on a table (Get Cell Value, Add Row to Memory Table,
Delete Row from Memory Table, Get Rows Count, Get Column Name, etc.) require
BOTH fields populated differently:

  ResultSet     → %tableName%   (percent-wrapped variable reference)
  ResultSetName → tableName     (bare string, no percent signs)

CORRECT:   ResultSet="%serverList%"   ResultSetName="serverList"
WRONG:     ResultSet="%serverList%"   ResultSetName="%serverList%"
WRONG:     ResultSet="serverList"     ResultSetName="serverList"

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ROW ACCESS — MOST CRITICAL RULE:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
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
GETCELLVALUE COLUMN RULES
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
  "Formula" | ">" | "<" | ">=" | "<=" | "=>"
- Default branch: Type="StoredValue", ConditionType="", Value="", IsValid="False",
  UseBranchWhenTimeout="True"
  NOTE: Do NOT set UseStoredValue on default branches — leave it absent.
        Corpus analysis of 1,361 default branches: UseStoredValue is absent in 91%
        of cases. Setting UseStoredValue="False" causes platform import issues.

EMPTY BRANCH RULE:
- Empty branch = ONLY ReturnValue. No Continue, DisplayValue, or filler.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
RETURNVALUE TYPE RULES — corpus-derived from 1,888 condition branches / 625 workflows
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
The Type field on a CONDITION branch ReturnValue depends on the preceding activity.
DEFAULT branches always use Type="StoredValue" regardless of preceding activity.

STEP 1 — Identify the preceding activity: the last non-container activity before
the IfElseActivity in sibling order. That is the activity whose output is being tested.

STEP 2 — Apply the rule for that activity type:

── TIER 1: Type="StoredValue", UseStoredValue="True" ──────────────────────────
Use for boolean producers and activities that return a known platform-defined string.

  Activity                    ConditionType   Confirmed Values
  ────────────────────────────────────────────────────────────────────
  IsEmpty                     Equals          "True" or "False"
  IsNumeric                   Equals          "True" or "False"
  ADUserExists                Equals          "True" or "False"
  FileExist                   Equals          "True" or "False"
  FolderExist                 Equals          "True" or "False"
  FolderExistRemote           Equals          "True" or "False"
  Contains                    Equals          "True" or "False"
                              Formula         (complex multi-condition expressions)
  XMLEditNode                 Equals          "Success" or "Failure"
  Ping                        Equals          "Success" or "Failure"
  URLCheck                    (empty)         (no ConditionType needed)
  ServiceStart                Equals          "Success"
  ServiceStop                 Equals          "Success"
  ADRemoveFromGroup           Equals          "Success"
  ADAddtoGroup                Equals          "Success"
  FileCopy                    Equals          "Success"
  VMPowerOn                   Equals          "Success"
  VMMarkTemplate              Equals          "Success"
  SLNPMManageNode             Equals          "Success"
  SLNPMUnManageNode           Equals          "Success"
  PowerShellScript            Equals          "Success"

── TIER 2: Type="UserDefinedValue", UseStoredValue="False" ────────────────────
Use for activities that return a number, computed string, or value requiring
operator comparison. The branch supplies its own comparison value.

  Activity                    ConditionType options
  ────────────────────────────────────────────────────────────────────
  GetRowsCount                > | Equals | Formula
  Length                      > | Equals | Formula
  WorkflowCounter             > | => | < | Formula
  DateDifference              > | < | =>
  Counter                     > | Equals | <= | =>
  FunctionCalculator          Formula
  GetCellValue                Equals | Contains | Formula
  GetCellValueAdvanced        Formula | Contains
  MemorySet                   Formula | Equals
  MultiMemorySet              Formula
  RunWorkflow                 Formula
  DisplayValue                Formula
  DisplayMultiValue           Formula | Equals
  TSQLStatement               Equals | Formula
  TSQLQuery                   > | Formula
  ResultSetFilter             Equals | Formula | Not Contains
  HTTPRequest                 Formula
  SingleSSHCommand            Formula | Contains | Equals | Not Contains
  PowerShell                  Contains | Equals | <
  SendEmail                   Contains | Equals | Formula | Not Contains
  MatchRegularExpression      Equals | Contains | Formula
  ReplaceString               Contains | Equals | > | Formula
  RegistryQuery               Contains | Not Equals
  LowerCase                   Formula | Equals
  ConvertPasswordToPlaintext  Equals | Formula
  ReadFile                    Contains | Equals | Not Contains
  XMLEvaluateXpathExpression  Equals | Formula | Not Equals
  MsTeamsSendMessage          Equals
  ChatGPTQuery                Contains | Formula
  ADPassExpDaysLeft           > | => | <= | Contains
  FileSize                    Equals | < | => | Not Equals
  DiskSpace                   => | <
  SystemUptime                > | < | <=
  TerminateWorkflow           Formula | Equals | >
  VMPowerState                Equals | Formula               (71% UDV, n=21 — weak signal)
  SetCellValue                Equals | Formula
  NestedJsonToTable           Formula | Equals
  GetDate                     Formula | Equals | >
  ReadContinuousFile          Contains | Not Contains
  NetBackupChangePolicy       Not Equals | Formula
  ConvertToPlainText          Formula
  SLNPMGetNode                Contains
  CherwellCreateRecord        Formula | Contains
  VMPowerOff                  Formula | Equals
  CreateMemoryTable           Formula

── MIXED — apply disambiguation rule ──────────────────────────────────────────
No clear majority in corpus. Choose Type by examining what the condition tests:

  ServiceStatus         SV=54% / UDV=46%  (n=13) — use StoredValue for "Running"/"Stopped"; UserDefinedValue for computed expressions
  SNUpdateRecord        SV=68% / UDV=32%  (n=19) — use StoredValue when testing a known status string; UserDefinedValue for Formula
  SNOWupdateCatalogVariable SV=71% / UDV=29% (n=7) — prefer StoredValue; use UserDefinedValue if condition is complex
  ServerRestart         SV=67% / UDV=33%  (n=3)  — prefer StoredValue Equals "Success"
  VMExists              SV=56% / UDV=44%  (n=9)
  AdvancedCommunicate   SV=40% / UDV=60%  (n=5)
  GoTo                  SV=75% / UDV=25%  (n=4)
  SNGetRecord           SV=33% / UDV=67%  (n=3)
  SLNPMAcknowledgeAlert SV=25% / UDV=50%  (n=4)
  FolderList            SV=33% / UDV=67%  (n=3)
  TableReplaceCellValues SV=50% / UDV=50% (n=4)

  DISAMBIGUATION RULE:
  - Condition tests a known platform status string ("Running", "Success", "True",
    "False") → Type="StoredValue", UseStoredValue="True"
  - Condition uses >, <, =>, <=, Formula, or Contains on a runtime/computed value
    → Type="UserDefinedValue", UseStoredValue="False"

── For any activity not listed above ─────────────────────────────────────────
Apply the disambiguation rule directly. Do not guess a tier.

── Formula field ──────────────────────────────────────────────────────────────
Do NOT set the Formula field on any ReturnValue. Leave it out entirely.
The serializer computes it from Type, ConditionType, and Value.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
WORKFLOW TERMINATION:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
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
      IfElseBranchActivity (condition) → ReturnValue (Type="UserDefinedValue", UseStoredValue="False") + action activity
      IfElseBranchActivity (default)   → ReturnValue (Type="StoredValue", UseBranchWhenTimeout="True") only, no UseStoredValue

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
EXAMPLE A — while_ifelse structure:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{_EXAMPLE_WHILE_IFELSE}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
EXAMPLE B — while with post-loop email:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{_EXAMPLE_WHILE_WITH_EMAIL}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
EXAMPLE C — linear (no loop, no branch):
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{_EXAMPLE_LINEAR}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
EXAMPLE D — if-else branching (no loop):
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{_EXAMPLE_IFELSE}

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