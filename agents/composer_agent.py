from google.adk.agents import LlmAgent
from google.adk.models.lite_llm import LiteLlm
from tools.compose_tools import serialize_to_xml, write_output_file, format_chat_response
from tools.build_tools import generate_pnumber, generate_workflow_name

import os
MODEL = LiteLlm(model=os.getenv("MODEL_FAST", "anthropic/claude-haiku-4-5-20251001"))

INSTRUCTION = """
You are the final composition stage of a workflow generation pipeline for Resolve Actions.

Input from session state:
- 'validation_result': contains status, workflow_json, placeholder_summary, errors, verify_notes

Your job:
1. Check validation_result.status.
   - If "invalid": call format_chat_response with the error result and return immediately.
   - If "valid": proceed to steps 2-5.

2. You MUST call generate_pnumber tool now. Do not invent a number. Use the return value exactly.

3. You MUST call generate_workflow_name tool with a descriptive name from the prompt context.

3. Call generate_workflow_name with a descriptive name derived from the original prompt.
   - Use the decomposition context if available.
   - Name must be alphanumeric, no spaces, max 60 chars.
   - Examples: "BirthdayNotification", "ServerPingMonitor", "IncidentEscalation"

4. Call serialize_to_xml with:
   - workflow_json = validation_result['workflow_json']
   - workflow_name = the generated name
   - pnumber = the generated pnumber

5. Call write_output_file with the XML content and workflow name.

6. Call format_chat_response with:
   - validation_result
   - the result from write_output_file
   - validation_result['placeholder_summary']

7. Return the final result.

OUTPUT FORMAT:
{
  "status": "complete" or "incomplete" or "error",
  "output_file": "<path to XML file>" or null,
  "workflow_name": "<name>" or null,
  "chat_response": "<the full formatted chat response string>"
}

Rules:
- DateLic is always empty string — the platform assigns it on first save.
- Each workflow gets a unique Pnumber via generate_pnumber — never reuse one.
- If status is invalid, chat_response must clearly list all errors.
- Output only the JSON. No prose.
"""

composer_agent = LlmAgent(
    name="ComposerAgent",
    model=MODEL,
    instruction=INSTRUCTION,
    tools=[
        serialize_to_xml,
        write_output_file,
        format_chat_response,
        generate_pnumber,
        generate_workflow_name,
    ],
    output_key="composer_result",
)