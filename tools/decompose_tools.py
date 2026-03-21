from typing import Annotated

SINGLE_WORKFLOW_CEILING = 25


def assess_complexity(
    prompt: Annotated[str, "The natural language workflow description"],
) -> dict:
    """
    Classifies a workflow prompt as simple, moderate, or complex based on keyword signals.
    Simple: linear steps, no loops, no branching.
    Moderate: one loop or one branch.
    Complex: nested loops, multiple branches, or parallel execution.
    """
    lower = prompt.lower()

    loop_signals = [
        "for each", "iterate", "every row", "all records",
        "repeat", "for every", "loop over", "loop through",
        "create a list", "build a list",
    ]
    branch_signals = [
        "if ", "when ", "depending on", "based on", "otherwise",
        "else", "check if", "in case",
    ]
    parallel_signals = [
        "at the same time", "simultaneously", "in parallel", "concurrently",
    ]

    loop_count = min(sum(1 for s in loop_signals if s in lower), 2)
    branch_count = min(sum(1 for s in branch_signals if s in lower), 2)
    parallel_count = sum(1 for s in parallel_signals if s in lower)

    if parallel_count > 0 or (loop_count > 1 and branch_count > 1):
        complexity = "complex"
    elif loop_count > 0 and branch_count > 0:
        complexity = "moderate"
    elif loop_count > 0 or branch_count > 0:
        complexity = "moderate"
    else:
        complexity = "simple"

    return {
        "complexity": complexity,
        "loop_signals": loop_count,
        "branch_signals": branch_count,
        "parallel_signals": parallel_count,
    }


def estimate_activity_count(
    prompt: Annotated[str, "The natural language workflow description"],
    complexity_result: Annotated[dict, "Output from assess_complexity"],
) -> dict:
    """
    Estimates total activity count using heuristics.
    Base: 6 activities for simple linear flow.
    +8 per loop (GetRowsCount + While + Sequence + ExitWhile + body activities).
    +5 per branch (IfElse + 2 branches + ReturnValues).
    +6 per parallel block.
    Loops and branches are capped at 1 each for moderate workflows to avoid
    over-rejecting legitimate single-loop-with-branch workflows.
    """
    loop_count = complexity_result.get("loop_signals", 0)
    branch_count = complexity_result.get("branch_signals", 0)
    parallel_count = complexity_result.get("parallel_signals", 0)
    complexity = complexity_result.get("complexity", "simple")

    if complexity == "moderate":
        loop_count = min(loop_count, 1)
        branch_count = min(branch_count, 1)

    base = 6
    estimated = base + (loop_count * 8) + (branch_count * 5) + (parallel_count * 6)

    routing = "single" if estimated <= SINGLE_WORKFLOW_CEILING else "rejected"

    result = {
        "estimated_total": estimated,
        "complexity": complexity,
        "routing": routing,
    }

    if estimated > SINGLE_WORKFLOW_CEILING:
        result["suggested_split"] = (
            "Break this into focused sub-workflows of 25 activities or fewer. "
            "Each can be generated and imported independently."
        )

    return result


def decompose_workflow(
    prompt: Annotated[str, "The natural language workflow description"],
    complexity: Annotated[str, "simple | moderate | complex"],
) -> dict:
    """
    Stub — the LlmAgent fills in the actual decomposition using its instruction.
    Returns the expected output structure for the LLM to populate.
    """
    return {
        "path": "single",
        "prompt": prompt,
        "complexity": complexity,
        "steps": [],
        "variable_contract": {
            "variables": [],
            "loop_type": "none",
            "loop_source": None,
        },
    }
    