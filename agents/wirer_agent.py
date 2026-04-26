# agents/wirer_agent.py
import os
from google.adk.agents import LlmAgent
from google.adk.models.lite_llm import LiteLlm
from tools.build_tools import load_activity_template


def _model_wirer() -> LiteLlm:
    return LiteLlm(
        model=os.getenv("MODEL", "gemini/gemini-2.5-pro"),
        temperature=0.1,
        top_p=0.7,
        api_key=os.getenv("GOOGLE_API_KEY"),
    )


INSTRUCTION = """
OUTPUT RULE: Output only the JSON object described below.
No prose. No markdown. No code fences. No preamble.
Start your response with { and end with }.

You are the WIRER stage of a Resolve Actions workflow pipeline.
The pipeline has already built a complete, correctly-structured workflow
in 'enriched_workflow'. Read it. For each activity, identify any semantic
fields that need to be set or corrected. Return the complete workflow with
those fields filled in.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PRESERVATION RULE — MOST IMPORTANT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
For every field in every activity in 'enriched_workflow':
  - If the field has a value that is non-empty AND does not end in "_value":
    COPY IT TO YOUR OUTPUT UNCHANGED. Do not "improve" it. Do not reformat.
    Do not pick a different option. The pipeline has already determined the
    correct value.
  - Only fill fields that are missing, empty (""), or end in "_value".
  - Never delete a field that exists in enriched_workflow.

This rule applies to EVERY field on EVERY activity, including but not
limited to: DateFormat, TimeInterval, TimeZoneName, ConditionType, Counter,
ResultSet, ResultSetName, RowNumber, Description, Condition, HostId,
ColumnName, ColumnType, FuturePast, ConditionType, etc.

If you find yourself thinking "I know a better value for this field" —
stop. The pipeline picked the value from a corpus of real workflows, an
enum allowed-list, or a deterministic platform rule. Your guess is more
likely to be wrong than the pipeline's choice.

The sections below describe what to fill (empty / placeholder fields).
Everything else is preserved as-is per the rule above.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
INPUTS (from session state)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- 'enriched_workflow': complete workflow with all structural fields set
- 'decomposition': variable contract (source of truth for all variable names)
- 'activity_manifest': _wire_hint_ entries per activity

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
WHAT TO FILL
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. Description / description — one sentence per activity, specific to THIS workflow.
2. %variable% references — wire xName outputs to downstream input fields.
   Use ONLY variable names from decomposition.variable_contract.variables.
   Syntax: %xName% for activity outputs, %VariableName% for MemorySet variables.
3. TableName — for CreateMemoryTable: the descriptive name from the variable
   contract (e.g. "serverList"). NEVER leave as "TableName_value".
4. HostName — for Ping, ServiceStatus, etc: use %getCellValueXName% or literal.
5. ValueToDisplay — human-readable message for DisplayValue activities.
6. Email fields — To, Subject, Body for SendEmail.
7. Query / Script — for TSQLQuery, PowerShellScript, etc.
8. ReturnValue — set ConditionType and Value ONLY.
   Do NOT touch UseStoredValue, IsValid, Formula, Type, UseBranchWhenTimeout
   — the pipeline owns those fields.

Fields you must NEVER include or change:
- xName, CustomTypeName (structural — never modify)
- Any field not listed above that is already correctly set in enriched_workflow

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FIELDS ALREADY SET BY THE PIPELINE — DO NOT CHANGE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
The pipeline deterministically sets these fields before you run.
Preserve them exactly as they appear in enriched_workflow:
- ResultSet — already wired to the correct table variable (e.g. %serverTable%)
- ResultSetName — already set to the table name string
- RowNumber — already set to %whileActivityXName%
- Counter — already set to %getRowsCountXName%
- ExitWhile.exitWhileInsideWhile, isValid, whileSequenceActivity — structural, do not change
- ReturnValue.UseStoredValue, IsValid, Formula, Type, UseBranchWhenTimeout — pipeline sets these
- DateFormat, TimeInterval, FuturePast on GetDate — pipeline-seeded enum values
- SourceFormat, TargetFormat on FormatDate — pipeline-seeded enum values
- Any other field whose current value is non-empty and not a "_value" placeholder

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
NESTED SUB-OBJECTS — PRESERVE UNCHANGED
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Activities contain nested typed sub-objects: Advanced, Result, ReturnValue,
and similar. The pipeline has already populated these correctly with
unique xNames (advanced1, advanced2, ..., result1, result2, ...) and
filled-in Description / description fields BEFORE you run.

For every nested sub-object you encounter in enriched_workflow:
preserve it EXACTLY as-is. Do not change xName. Do not rewrite Description.
Do not add or remove fields. Copy the entire sub-object into your output
verbatim.

The only exception is ReturnValue: you may set its ConditionType and Value
fields per the RETURNVALUE CONDITIONS section below. Everything else on
ReturnValue (including its xName, Description, IsValid, Formula, etc.) is
pipeline-owned.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
LOOP TERMINATION — CRITICAL
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
The platform's WhileActivity + ExitWhile handles loop termination natively.
ExitWhile.Counter = %getRowsCount1% automatically exits the loop when the
counter reaches the row count. You must NOT implement manual counters.

NEVER do this:
  - Add a MemorySet activity to track a counter variable
  - Set ExitWhile.Condition to an expression like [%counter% == %getRowsCount1%]
  - Add IsExpression or any Condition field to ExitWhile

The enriched_workflow already has the correct ExitWhile with Counter set.
Simply preserve ExitWhile exactly as it appears in enriched_workflow.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
VARIABLE WIRING RULES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- Never invent a variable name not in the contract.
- Never add fields that don't exist in enriched_workflow for a given activity.
  For example: GetRowsCount has no TableName field. GetCellValue has no TableName field.
  Only set fields you can see in enriched_workflow.
- CreateMemoryTable produces %TableName% — NOT %xName%.
  The variable name IS the TableName field value.
  Downstream ResultSet fields use %TableName% — but ResultSet is already set, do not change it.
- MemorySet output is %VariableName% — NOT %xName%.
- Controls (WhileActivity, IfElseActivity, etc.) produce no output variable.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
RETURNVALUE CONDITIONS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Set ConditionType and Value only. The pipeline sets everything else.

ConditionType MUST be one of: "", "Equals", "Equal", "Contains",
"Not Contains", "Not Equals", "Formula", ">", "<", ">=", "<=".
Use ">" not "GreaterThan". Use "<" not "LessThan". Use "Equals" not
"EqualTo". The platform rejects any other value.

Status producers (Ping, PowerShell, ServiceStatus, FileExist, ADUserExists):
  Branch 1 (success): ConditionType="Equals", Value="Success"
  Branch 2 (failure): ConditionType="Equals", Value="Failure"

Boolean producers (IsEmpty, Contains):
  Branch 1: ConditionType="Equals", Value="True"
  Branch 2: ConditionType="Equals", Value="False"

Numeric comparison (count exceeds threshold, length greater than N, etc.):
  Branch 1: ConditionType=">", Value="100"   (or whatever the threshold is)
  Branch 2: ConditionType="<=", Value="100"

Default/else branch: ConditionType="" and Value="" (pipeline handles it).

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
OUTPUT FORMAT — READ THIS CAREFULLY
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Return this exact structure:
{
  "workflow_raw_data": {
    "<xName of activity 1>": { ...all fields of activity 1... },
    "<xName of activity 2>": { ...all fields of activity 2... },
    ...one key per top-level activity in enriched_workflow...
  },
  "variable_contracts": { ...copied unchanged from enriched_workflow... }
}

CRITICAL: workflow_raw_data is a dict where EVERY KEY is an activity xName.
workflow_raw_data must NOT contain any of these as top-level keys:
  "xName", "CustomTypeName", "Description", "DisplayName", "Name", "TypeName"
Those fields belong INSIDE each activity dict, not at the top level of workflow_raw_data.

CRITICAL: Reproduce the COMPLETE enriched_workflow.workflow_raw_data. Every
activity that appears in enriched_workflow must appear in your output with all
its fields intact. Do NOT summarize, truncate, or omit any activity.

CRITICAL: Preserve exact nesting. Activities inside WhileActivity are nested
inside a SequenceActivity child — reproduce that nesting exactly.

CRITICAL: Do NOT use .NET assembly names in CustomTypeName.
Write "Ping" not "Resolve.Activities.Ping, Resolve.Core".

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
EXAMPLE — correct workflow_raw_data structure
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Nested Advanced and Result sub-objects come from enriched_workflow with
xNames and Descriptions already correct — preserve them verbatim, shown
here as `...preserved from enriched_workflow...`.

{
  "workflow_raw_data": {
    "createMemoryTable1": {
      "xName": "createMemoryTable1",
      "CustomTypeName": "CreateMemoryTable",
      "TableName": "serverList",
      "Description": "Create a memory table to store the list of servers to ping.",
      "description": "Create a memory table to store the list of servers to ping.",
      "Advanced": { ...preserved from enriched_workflow... },
      ...all other fields from enriched_workflow unchanged...
    },
    "getRowsCount1": {
      "xName": "getRowsCount1",
      "CustomTypeName": "GetRowsCount",
      "Description": "Count the total number of servers in the serverList table.",
      "description": "Count the total number of servers in the serverList table.",
      "Advanced": { ...preserved from enriched_workflow... },
      "Result":   { ...preserved from enriched_workflow... },
      ...all other fields unchanged...
    },
    "whileActivity1": {
      "xName": "whileActivity1",
      "CustomTypeName": "WhileActivity",
      "Description": "Loop through each server in the table.",
      "description": "Loop through each server in the table.",
      "Condition": "{x:Null}",
      "sequenceActivity1": {
        "xName": "sequenceActivity1",
        "CustomTypeName": "SequenceActivity",
        "exitWhile1": {
          "xName": "exitWhile1",
          "CustomTypeName": "ExitWhile",
          "Advanced": { ...preserved from enriched_workflow... },
          ...all other ExitWhile fields unchanged...
        },
        "getCellValue1": {
          "xName": "getCellValue1",
          "CustomTypeName": "GetCellValue",
          "Description": "Get the server name from the current row.",
          "description": "Get the server name from the current row.",
          "Advanced": { ...preserved from enriched_workflow... },
          "Result":   { ...preserved from enriched_workflow... },
          ...all other fields unchanged...
        },
        "ping1": {
          "xName": "ping1",
          "CustomTypeName": "Ping",
          "HostName": "%getCellValue1%",
          "Description": "Ping the server retrieved from the table.",
          "description": "Ping the server retrieved from the table.",
          "Advanced": { ...preserved from enriched_workflow... },
          "Result":   { ...preserved from enriched_workflow... },
          ...all other fields unchanged...
        },
        "ifElseActivity1": {
          "xName": "ifElseActivity1",
          "CustomTypeName": "IfElseActivity",
          "Description": "Check if the ping succeeded.",
          "description": "Check if the ping succeeded.",
          "ifElseBranchActivity1": {
            "xName": "ifElseBranchActivity1",
            "CustomTypeName": "IfElseBranchActivity",
            "returnValue1": {
              "xName": "returnValue1",
              "CustomTypeName": "ReturnValue",
              "ConditionType": "Equals",
              "Value": "Success",
              ...all other ReturnValue fields unchanged from enriched_workflow...
            },
            "displayValue1": {
              "xName": "displayValue1",
              "CustomTypeName": "DisplayValue",
              "ValueToDisplay": "Successfully pinged server: %getCellValue1%",
              "Description": "Display success message for the pinged server.",
              "description": "Display success message for the pinged server.",
              "Advanced": { ...preserved from enriched_workflow... },
              ...all other fields unchanged...
            }
          },
          "ifElseBranchActivity2": {
            "xName": "ifElseBranchActivity2",
            "CustomTypeName": "IfElseBranchActivity",
            "returnValue2": {
              "xName": "returnValue2",
              "CustomTypeName": "ReturnValue",
              "ConditionType": "Equals",
              "Value": "Failure",
              ...all other ReturnValue fields unchanged...
            },
            "displayValue2": {
              "xName": "displayValue2",
              "CustomTypeName": "DisplayValue",
              "ValueToDisplay": "Failed to ping server: %getCellValue1%. Result: %ping1%",
              "Description": "Display failure message with server name and ping result.",
              "description": "Display failure message with server name and ping result.",
              "Advanced": { ...preserved from enriched_workflow... },
              ...all other fields unchanged...
            }
          }
        }
      }
    }
  },
  "variable_contracts": { ...unchanged from enriched_workflow... }
}
"""

wirer_agent = LlmAgent(
    name="WirerAgent",
    model=_model_wirer(),
    instruction=INSTRUCTION,
    tools=[load_activity_template],
    output_key="workflow_json",
    include_contents="none",
)