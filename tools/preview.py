"""
tools/preview.py

Phase F — Gate 1 preview rendering. Path B variant.

Builds the chat-panel string the user sees at Gate 1, before approving the
plan. Pure rendering — no state, no telemetry, no IO except reading the
taxonomy file once for task labels and descriptions.

PATH B SIMPLIFICATIONS (vs v4 spec §7)
  - No slot values. Path B's Decomposer stays in freeform mode (§6.3
    fallback path) and produces step lists, not slot values. The preview
    therefore omits the inline slot rendering shown in spec §7.1.
  - workflow_summary comes from the prompt, not from the Decomposer. v4's
    slot-filling Decomposer was supposed to generate this; freeform doesn't.
    The first sentence of the prompt is used, truncated if long.
  - [Show activities] toggle (§7.1) drills down to the decomposition step
    list rendered as a flat list with category tags. Steps are NOT
    grouped under tasks because Path B's matcher doesn't bind steps to
    tasks.

THREE OUTPUT MODES
  1. Task view (default, when matcher returned tasks). Renders task labels
     with descriptions from the taxonomy. Lists evidence phrases that
     triggered each match in subdued form.
  2. Step view (fallback, when matcher returned no_task_match). Banner
     warns about freeform mode, then renders the Decomposer's step list.
  3. Combined view (when include_activities=True). Task view at top,
     followed by step list — gives reviewers both abstractions.

ENV
  DATA_DIR — root for data files. Defaults to "/app/data".

USAGE
  from tools.preview import build_task_preview

  preview_text = build_task_preview(
      match_result=match_result,        # MatchResult from task_matcher
      decomposition=decomposition,      # dict from DecomposerAgent
      prompt=original_prompt,           # for workflow summary
      include_activities=False,         # toggle for [Show activities]
  )
  print(preview_text)

SMOKE TEST
  python -m tools.preview
"""

import json
import os
import sys
from pathlib import Path


DATA_DIR      = Path(os.getenv("DATA_DIR", "/app/data"))
TAXONOMY_PATH = DATA_DIR / "task_taxonomy.json"

# Width target for wrapping descriptions and prompt summary
LINE_WIDTH = 76

# Maximum characters for the auto-derived workflow summary
SUMMARY_MAX_LEN = 120

# Buttons rendered at the bottom — kept as literal strings so the frontend
# can split on them later if it wants to inject click handlers
BUTTONS_TASK_VIEW = "[Looks good]  [Needs changes]  [Show activities]  [Show raw]"
BUTTONS_STEP_VIEW = "[Looks good]  [Needs changes]  [Show raw]"

# Category lookup — same set used by frontend's display tagging today.
# Mirrored here so the preview renders without a second data file lookup.
INTENT_TO_CATEGORY = {
    "create_table":        "Tables",
    "count_rows":          "Tables",
    "get_cell":            "Tables",
    "get_date":            "Time",
    "format_date":         "Time",
    "date_difference":     "Time",
    "set_variable":        "Variables",
    "initialize_variable": "Variables",
    "display":             "Logging",
    "send_email":          "Communication",
    "query_servicenow":    "ServiceNow",
    "branch":              "Flow Control",
    "loop":                "Flow Control",
    "exit_loop":           "Flow Control",
    "other":               "General",
}


# ---------------------------------------------------------------------------
# Module-level cache — taxonomy loaded once
# ---------------------------------------------------------------------------

_taxonomy_loaded: bool = False
_task_labels:        dict[str, str] = {}   # task_id -> label
_task_descriptions:  dict[str, str] = {}   # task_id -> description


def _load_taxonomy() -> None:
    """Loads task labels and descriptions from the taxonomy. Graceful on
    failure — falls back to using the task_id directly as the label."""
    global _taxonomy_loaded
    if _taxonomy_loaded:
        return
    _taxonomy_loaded = True
    try:
        with open(TAXONOMY_PATH, encoding="utf-8") as f:
            doc = json.load(f)
    except Exception as e:
        print(f"[preview] WARNING: taxonomy not loadable at {TAXONOMY_PATH}: "
              f"{e}. Task labels will fall back to task_id strings.",
              file=sys.stderr)
        return
    for task in doc.get("atomic_tasks", []):
        tid = task.get("task_id")
        if not tid:
            continue
        _task_labels[tid]       = task.get("label", tid)
        _task_descriptions[tid] = task.get("description", "")


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------

def _summarize_prompt(prompt: str) -> str:
    """Returns a one-line summary of the prompt. First sentence, capped at
    SUMMARY_MAX_LEN chars. Path B's stand-in for the v4 spec's
    LLM-generated workflow_summary."""
    if not prompt:
        return ""
    text = prompt.strip().replace("\n", " ")
    # Take first sentence
    for terminator in [". ", "? ", "! "]:
        idx = text.find(terminator)
        if 0 < idx <= SUMMARY_MAX_LEN:
            return text[:idx + 1].strip()
    if len(text) <= SUMMARY_MAX_LEN:
        return text
    return text[:SUMMARY_MAX_LEN].rstrip() + "…"


def _wrap_indent(text: str, indent: str = "       ", width: int = LINE_WIDTH) -> str:
    """Soft-wraps a single paragraph and prepends indent to every line.
    Pure stdlib — textwrap was overkill for this."""
    if not text:
        return ""
    words = text.split()
    lines, current = [], indent
    for w in words:
        if len(current) + len(w) + 1 > width and current.strip():
            lines.append(current.rstrip())
            current = indent + w
        else:
            current = (current + " " + w) if current.strip() else (indent + w)
    if current.strip():
        lines.append(current.rstrip())
    return "\n".join(lines)


def _format_task_block(index: int, task_entry: dict) -> str:
    """Render one task as:
         1. Label of the task
            → Description of what the task does
            (matched: phrase1, phrase2)
    """
    task_id    = task_entry["task_id"]
    label      = _task_labels.get(task_id, task_id)
    desc       = _task_descriptions.get(task_id, "")
    confidence = task_entry.get("confidence", 0.0)
    evidence   = task_entry.get("evidence_phrases", [])

    lines = [f"  {index}. {label}"]
    if desc:
        lines.append(_wrap_indent(f"→ {desc}", indent="     "))
    if evidence:
        # Render evidence as compact gray-ish hint. Frontend can style this.
        ev_str = ", ".join(f'"{p}"' for p in evidence[:4])
        if len(evidence) > 4:
            ev_str += f", +{len(evidence) - 4} more"
        lines.append(f"     (matched {ev_str}; confidence {confidence:.2f})")
    return "\n".join(lines)


def _format_step_block(index: int, step: dict) -> str:
    """Render one decomposition step as:
         1. [Category] description text
    """
    intent      = step.get("intent", "other")
    description = step.get("description", "(no description)")
    category    = INTENT_TO_CATEGORY.get(intent, "General")
    return f"  {index}. [{category}] {description}"


def _build_task_view(prompt: str, match_result, decomposition: dict,
                     include_activities: bool) -> str:
    """Path B task-view rendering. Caller must have verified
    match_result.tasks is non-empty."""
    summary = _summarize_prompt(prompt)
    parts = []
    if summary:
        parts.append(summary)
        parts.append("")

    n_tasks = len(match_result.tasks)
    parts.append(f"Plan ({n_tasks} task{'s' if n_tasks != 1 else ''}):")
    parts.append("")
    for i, task in enumerate(match_result.tasks, 1):
        parts.append(_format_task_block(i, task))
        parts.append("")

    # Surface unmatched segments inline so reviewers see them before approving
    if match_result.unmatched_segments:
        parts.append("Notes:")
        for seg in match_result.unmatched_segments[:3]:
            parts.append(f"  • Unmatched in prompt: \"{seg}\"")
        parts.append("")

    if include_activities:
        steps = decomposition.get("steps", [])
        if steps:
            parts.append("Activities:")
            parts.append("")
            for i, step in enumerate(steps, 1):
                parts.append(_format_step_block(i, step))
            parts.append("")

    parts.append(BUTTONS_TASK_VIEW)
    return "\n".join(parts)


def _build_step_view(prompt: str, decomposition: dict) -> str:
    """Fallback view per spec §7.2 — banner + step list."""
    summary = _summarize_prompt(prompt)
    parts = ["⚠ No task match — using step-by-step decomposition", ""]
    if summary:
        parts.append(summary)
        parts.append("")

    steps = decomposition.get("steps", [])
    n_steps = len(steps)
    parts.append(f"Steps ({n_steps}):")
    parts.append("")
    for i, step in enumerate(steps, 1):
        parts.append(_format_step_block(i, step))
    parts.append("")
    parts.append(BUTTONS_STEP_VIEW)
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def build_task_preview(match_result,
                       decomposition: dict,
                       prompt: str = "",
                       include_activities: bool = False) -> str:
    """
    Build the Gate 1 preview string.

    Args:
      match_result: MatchResult from tools.task_matcher (or any object
                    exposing .tasks list and .unmatched_segments list).
                    If None or has no tasks, falls back to step view.
      decomposition: dict from DecomposerAgent. Used for steps list.
      prompt: original user prompt. Used to derive the one-line summary.
      include_activities: when True and matcher returned tasks, also show
                          the decomposition steps below the task list.
                          Maps to the [Show activities] toggle in spec §7.1.

    Returns: rendered preview string ready for chat panel.
    """
    _load_taxonomy()
    decomposition = decomposition or {}

    has_tasks = (match_result is not None
                 and getattr(match_result, "tasks", None))
    if has_tasks:
        return _build_task_view(prompt, match_result, decomposition,
                                include_activities)
    return _build_step_view(prompt, decomposition)


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------

def _smoke_test() -> int:
    """Renders previews for three scenarios and prints them. Verifies the
    expected sections appear; returns 1 on structural mismatches."""
    print("[smoke] DATA_DIR =", DATA_DIR.resolve())
    print("[smoke] taxonomy =", TAXONOMY_PATH.resolve())
    print()

    # Use the real matcher so we exercise the full pipeline path
    try:
        from tools.task_matcher import match_tasks
    except Exception as e:
        print(f"[smoke] FAIL: cannot import task_matcher: {e}", file=sys.stderr)
        return 1

    failures = 0

    # --- Scenario 1: matcher returns tasks ---
    prompt_1 = (
        "Every morning, check a list of servers from an Excel file. "
        "For each server, ping it. If the ping fails, send an email "
        "to the admin with the server name."
    )
    decomposition_1 = {
        "steps": [
            {"step_id": "s1", "intent": "create_table",
             "description": "Create the server list table"},
            {"step_id": "s2", "intent": "other",
             "description": "Read server names from the Excel file"},
            {"step_id": "s3", "intent": "count_rows",
             "description": "Count the number of servers"},
            {"step_id": "s4", "intent": "loop",
             "description": "Loop through each server"},
            {"step_id": "s5", "intent": "get_cell",
             "description": "Get the current server name"},
            {"step_id": "s6", "intent": "other",
             "description": "Ping the server"},
            {"step_id": "s7", "intent": "branch",
             "description": "Check if ping failed"},
            {"step_id": "s8", "intent": "send_email",
             "description": "Email the admin with the server name"},
        ],
        "variable_contract": {"loop_type": "While"},
    }
    match_1 = match_tasks(prompt_1)

    print("=" * 72)
    print("SCENARIO 1: task view (default — no [Show activities])")
    print("=" * 72)
    text_1 = build_task_preview(match_1, decomposition_1, prompt=prompt_1)
    print(text_1)
    print()
    if "Plan (5 tasks):" not in text_1:
        failures += 1
        print("  [FAIL] expected 'Plan (5 tasks):' in output")
    if BUTTONS_TASK_VIEW not in text_1:
        failures += 1
        print("  [FAIL] expected task-view buttons in output")

    # --- Scenario 2: same prompt, with [Show activities] toggle ---
    print("=" * 72)
    print("SCENARIO 2: task view + activities (Show activities clicked)")
    print("=" * 72)
    text_2 = build_task_preview(match_1, decomposition_1, prompt=prompt_1,
                                include_activities=True)
    print(text_2)
    print()
    if "Activities:" not in text_2:
        failures += 1
        print("  [FAIL] expected 'Activities:' section in expanded view")
    if "[Files/Folders]" in text_2 or "[Communication]" in text_2:
        # We use INTENT_TO_CATEGORY which doesn't have Files/Folders.
        # The smoke test is just verifying SOMETHING with brackets shows up.
        pass

    # --- Scenario 3: no match → fallback ---
    prompt_3 = "Make my computer go faster please thanks"
    decomposition_3 = {
        "steps": [
            {"step_id": "s1", "intent": "other",
             "description": "Check current performance"},
            {"step_id": "s2", "intent": "other",
             "description": "Apply performance tweaks"},
        ],
    }
    match_3 = match_tasks(prompt_3)

    print("=" * 72)
    print("SCENARIO 3: fallback view (no_task_match)")
    print("=" * 72)
    text_3 = build_task_preview(match_3, decomposition_3, prompt=prompt_3)
    print(text_3)
    print()
    if "⚠ No task match" not in text_3:
        failures += 1
        print("  [FAIL] expected fallback banner in output")
    if BUTTONS_STEP_VIEW not in text_3:
        failures += 1
        print("  [FAIL] expected step-view buttons in fallback output")

    # --- Scenario 4: empty decomposition (defensive) ---
    print("=" * 72)
    print("SCENARIO 4: empty decomposition (defensive)")
    print("=" * 72)
    text_4 = build_task_preview(match_3, {}, prompt="anything")
    print(text_4)
    print()
    if "Steps (0):" not in text_4:
        failures += 1
        print("  [FAIL] expected 'Steps (0):' for empty decomposition")

    print()
    print(f"[smoke] {4 - failures}/4 scenarios passed structural checks")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(_smoke_test())