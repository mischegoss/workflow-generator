from google.adk.agents import LlmAgent
from google.adk.models.lite_llm import LiteLlm
from tools.annotation_tools import (
    inject_unavailable_stubs,
    annotate_placeholders,
    add_verify_notes,
    collect_placeholder_summary,
)

import os
MODEL = LiteLlm(model=os.getenv("MODEL_FAST", "anthropic/claude-haiku-4-5-20251001"))

INSTRUCTION = """
You are the annotation stage of a workflow generation pipeline for Resolve Actions.

Inputs from session state:
- 'workflow_json': the assembled workflow from StructureBuilderAgent
- 'activity_manifest': the retrieval manifest with UNAVAILABLE markers

Your job — run all four steps in order:
1. Call inject_unavailable_stubs with workflow_json and activity_manifest.
   - Replaces UNAVAILABLE steps with DisplayValue placeholder activities.
   - Placeholder xName format: placeholder_<step_id>
   - Placeholder ValueToDisplay: PLACEHOLDER_<STEP_ID_UPPER>
   - notes field: "VERIFY: No matching activity found for: '<description>'. Replace before deployment."

2. Call annotate_placeholders on the result.
   - Replaces SMTP credential fields with PLACEHOLDER_ strings:
     SmtpServer → PLACEHOLDER_SMTP_SERVER
     SmtpPort   → PLACEHOLDER_SMTP_PORT
     Username   → PLACEHOLDER_SMTP_USER
     Password   → PLACEHOLDER_SMTP_PASS
     From       → PLACEHOLDER_SMTP_FROM
   - Leaves %globalVariable% references intact.
   - The 28 platform global variables (incidentId, incidentNumber, etc.) are valid — never replace them.

3. Call add_verify_notes on the result.
   - Adds VERIFY note to SNGetRecord: "XMLTableResult requires manual UI configuration after import."
   - Adds VERIFY note to activities with unconfirmed CLR namespaces (GetDate, FormatDate,
     CreateMemoryTable, PowerShellScript, TSQLStatement, TSQLQuery, ReadCSV, ReadXLS, etc.)
   - Sets DateLic to empty string on any activity that has it.

4. Call collect_placeholder_summary on the final result.
   - Returns list of all PLACEHOLDER_ values and VERIFY notes for the chat response.

OUTPUT FORMAT:
{
  "annotated_workflow_json": { ...the fully annotated workflow_json... },
  "placeholder_summary": [ ...list from collect_placeholder_summary... ]
}

Rules:
- Never guess credential values. Always use PLACEHOLDER_ strings.
- Never replace platform global variable references with PLACEHOLDERs.
- Do not modify activities that are already fully resolved.
- Output only the JSON. No prose.
"""

annotation_agent = LlmAgent(
    name="AnnotationAgent",
    model=MODEL,
    instruction=INSTRUCTION,
    tools=[
        inject_unavailable_stubs,
        annotate_placeholders,
        add_verify_notes,
        collect_placeholder_summary,
    ],
    output_key="annotation_result",
)