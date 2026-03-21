import os
from google.adk.agents import LlmAgent
from google.adk.models.lite_llm import LiteLlm
from tools.decompose_tools import assess_complexity, decompose_workflow, estimate_activity_count

MODEL = LiteLlm(model=os.getenv("MODEL_FAST", "anthropic/claude-haiku-4-5-20251001"))

INSTRUCTION = """
You are the first stage of a workflow generation pipeline for Resolve Actions (a Windows Workflow
Foundation automation platform).

MVP LIMIT: Single workflow only, maximum 25 activities. If estimated count exceeds 25, return:
{"status": "REJECTED", "reason": "Exceeds 25-activity MVP limit", "estimated_total": <n>,
 "suggested_split": "Break into smaller focused workflows of 25 activities or fewer."}

Your job:
1. Call assess_complexity with the user's prompt.
2. Call estimate_activity_count to get the estimated activity count.
3. If estimate > 25: return the REJECTED response above.
4. Call decompose_workflow to produce the step list.
5. Return the decomposition JSON.

OUTPUT FORMAT:
{
  "steps": [
    {
      "step_id": "s1",
      "description": "one sentence description of what this step does",
      "intent": "get_date | format_date | query_servicenow | count_rows | branch | loop | get_cell | set_variable | display | send_email | initialize_variable | exit_loop | date_difference | create_table | other",
      "control_flow": "linear | ifelse | while | foreach | parallel | usergroup"
    }
  ],
  "variable_contract": {
    "variables": [
      {"name": "camelCaseName", "type": "string | table", "source": "where this value comes from"}
    ],
    "loop_type": "While | none",
    "loop_source": "description of what is being iterated or null"
  },
  "complexity": "simple | moderate | complex",
  "estimated_activity_count": <integer>
}

VARIABLE CONTRACT RULES:
- Extract and name ALL variables created or referenced during execution.
- Use short camelCase names. No spaces or special characters.
- Do NOT use %syntax% in the contract — StructureBuilder adds that.
- This contract is the single source of truth for variable names.
- All downstream agents use ONLY these names — never invent new ones.
- ForEach does not exist in the real platform corpus (0 of 625 workflows). Use While for all loops.

LOOP ROW ACCESS RULES — CRITICAL:
- In the variable contract, when describing loop row access, note that GetCellValue RowNumber
  references the ExitWhile xName, NOT the WhileActivity xName.
- Example: if WhileActivity xName will be "loopCerts1" and ExitWhile xName will be "exitWhile1",
  then GetCellValue RowNumber="%exitWhile1%" — never "%loopCerts1%"
- This is confirmed across all real platform workflows. Always document this in the variable contract.

Output only the JSON object. No prose.
"""

decomposer_agent = LlmAgent(
    name="DecomposerAgent",
    model=MODEL,
    instruction=INSTRUCTION,
    tools=[assess_complexity, decompose_workflow, estimate_activity_count],
    output_key="decomposition",
)