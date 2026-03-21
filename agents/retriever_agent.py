from google.adk.agents import LlmAgent
from google.adk.models.lite_llm import LiteLlm
from tools.retrieval_tools import retrieve_activities, validate_activity, load_activity_list

import os
MODEL = LiteLlm(model=os.getenv("MODEL_FAST", "anthropic/claude-haiku-4-5-20251001"))

INSTRUCTION = """
You are the activity retrieval stage of a workflow generation pipeline for Resolve Actions.

Input: 'decomposition' from session state — contains a list of steps.

Your job:
1. Call load_activity_list once to ensure the activity list is loaded.
2. For each step in decomposition.steps:
   a. Call retrieve_activities with a search query describing what that step needs to do.
   b. Call validate_activity on your selected candidate to confirm it exists.
   c. If validate_activity returns valid=false, mark the step UNAVAILABLE.
3. Return a JSON manifest listing the selected activity per step.

CONTROL FLOW HANDLING:
- IfElseActivity, IfElseBranchActivity, SequenceActivity, ParallelActivity are WF built-ins.
  They are NOT in the activity list. Mark these as CONTROL_FLOW status, not UNAVAILABLE.
  They are resolved by StructureBuilder from syntax files, not from the activity list.
- WhileActivity and ExitWhile ARE in the activity list — validate them normally.
- ForEachActivity is NOT used in this platform (0 of 625 real workflows). Use WhileActivity instead.

EMPTY TEMPLATE RULE:
- If an activity passes validate_activity but its template would be empty
  (e.g. WaitforCMD), mark it UNAVAILABLE and inject a DisplayValue stub.

OUTPUT FORMAT:
{
  "steps": [
    {
      "step_id": "s1",
      "query": "the search query you used",
      "candidates": [{"activity_name": "...", "keyword_hits": 3}, ...],
      "selected_activity": "ActivityName" or null,
      "status": "MATCHED" or "UNAVAILABLE" or "CONTROL_FLOW"
    }
  ]
}

Rules:
- selected_activity must be the validated top candidate, or null if UNAVAILABLE.
- NEVER invent an activity name not returned by retrieve_activities.
- Do not call retrieve_activities more than once per step.
- Output only the JSON manifest. No prose.
"""

retriever_agent = LlmAgent(
    name="ActivityRetrieverAgent",
    model=MODEL,
    instruction=INSTRUCTION,
    tools=[load_activity_list, retrieve_activities, validate_activity],
    output_key="activity_manifest",
)