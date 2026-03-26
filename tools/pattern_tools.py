import json
import os
from typing import Annotated

_pattern_library: list | None = None
_activity_ranks: list | None = None


def load_pattern_library() -> list:
    """Loads the mined pattern library from data/patterns/pattern_library.json."""
    global _pattern_library
    if _pattern_library is not None:
        return _pattern_library
    data_dir = os.getenv("DATA_DIR", "/app/data")
    path = os.path.join(data_dir, "patterns", "pattern_library.json")
    with open(path, encoding="utf-8") as f:
        _pattern_library = json.load(f)
    print(f"[patterns] Loaded {len(_pattern_library)} patterns.")
    return _pattern_library


def load_activity_ranks() -> list:
    """Loads activity co-occurrence rank pairs from data/activity_ranks.json."""
    global _activity_ranks
    if _activity_ranks is not None:
        return _activity_ranks
    data_dir = os.getenv("DATA_DIR", "/app/data")
    path = os.path.join(data_dir, "activity_ranks.json")
    with open(path, encoding="utf-8") as f:
        _activity_ranks = json.load(f)
    print(f"[patterns] Loaded {len(_activity_ranks)} activity rank pairs.")
    return _activity_ranks


def match_pattern(
    decomposition: Annotated[dict, "Decomposition output from DecomposerAgent"],
) -> list:
    """
    Matches decomposition steps against the pattern library using keyword overlap.
    Returns top candidate patterns sorted by score descending.

    Fixes applied:
    1. Reads both 'description' AND 'intent' from each step — previously only
       read 'description', so intent values like 'loop' never matched keywords.
    2. Normalises underscores in intent values to spaces so 'count_rows' matches
       the keyword 'count rows'.
    3. Scores by hits / sqrt(keyword_count) instead of hits / keyword_count —
       prevents penalising patterns with comprehensive keyword lists.
    4. Sorts ties in favour of patterns that have scaffolds, so MODE 1 fires
       preferentially when multiple patterns score equally.
    """
    patterns = load_pattern_library()
    steps = decomposition.get("steps", [])

    # Fix 1 + 2: include intent with underscores normalised to spaces
    step_text = " ".join(
        f"{s.get('description', '')} {s.get('intent', '').replace('_', ' ')}"
        for s in steps
    ).lower()

    candidates = []
    for pattern in patterns:
        keywords = pattern.get("trigger_keywords", [])
        if not keywords:
            continue
        hits = sum(1 for kw in keywords if kw.lower() in step_text)
        if hits > 0:
            # Fix 3: sqrt normalisation — dampens length penalty without removing it
            score = hits / (len(keywords) ** 0.5)
            candidates.append({**pattern, "_score": round(score, 3)})

    # Fix 4: break ties by preferring patterns that have scaffolds
    return sorted(
        candidates,
        key=lambda x: (x["_score"], 1 if x.get("scaffold") else 0),
        reverse=True,
    )


def _detect_fallback_cf(decomposition: dict) -> str:
    """
    Detects the best fallback control flow type from decomposition.
    Used when no pattern matches above the threshold.
    """
    if not decomposition:
        return "Linear"

    steps = decomposition.get("steps", [])
    loop_type = decomposition.get("variable_contract", {}).get("loop_type", "none")
    if isinstance(loop_type, str):
        loop_type = loop_type.lower()

    has_loop = loop_type in ("while", "foreach") or any(
        s.get("control_flow") in ("while", "foreach") or s.get("intent") == "loop"
        for s in steps
    )
    has_branch = any(
        s.get("control_flow") == "ifelse" or s.get("intent") == "branch"
        for s in steps
    )
    has_usergroup = any(s.get("control_flow") == "usergroup" for s in steps)

    if has_loop and has_branch:
        return "while_ifelse"
    elif has_loop:
        return "While"
    elif has_branch:
        return "IfElse"
    elif has_usergroup:
        return "UserGroup"
    else:
        return "Linear"


def score_pattern_match(
    candidates: Annotated[list, "Top candidates from match_pattern"],
    decomposition: Annotated[dict, "Decomposition from DecomposerAgent"] = None,
    threshold: Annotated[float, "Match threshold, defaults to env var"] = None,
) -> dict:
    """
    Applies threshold gate to pattern candidates.
    Returns MATCHED with scaffold or NO_MATCH with fallback control flow type.

    Fix applied: if the top-scoring pattern has no scaffold, treat as NO_MATCH
    and fall through to example-guided MODE 2. Prevents fill_scaffold_params
    being called with a null scaffold.
    """
    if threshold is None:
        threshold = float(os.getenv("PATTERN_MATCH_THRESHOLD", "0.80"))

    fallback_cf = _detect_fallback_cf(decomposition)

    if not candidates:
        return {
            "match_status": "NO_MATCH",
            "pattern_id": None,
            "pattern_name": None,
            "score": 0.0,
            "scaffold": None,
            "fallback_examples": [fallback_cf],
        }

    top = candidates[0]
    score = top["_score"]

    if score >= threshold:
        scaffold = top.get("scaffold")
        if not scaffold:
            # Pattern matched above threshold but has no scaffold —
            # fall through to NO_MATCH so MODE 2 example-guided path runs.
            return {
                "match_status": "NO_MATCH",
                "pattern_id": None,
                "pattern_name": None,
                "score": round(score, 3),
                "scaffold": None,
                "fallback_examples": [fallback_cf],
            }
        return {
            "match_status": "MATCHED",
            "pattern_id": top.get("pattern_id"),
            "pattern_name": top.get("control_flow"),
            "score": score,
            "scaffold": scaffold,
            "fallback_examples": [],
        }

    return {
        "match_status": "NO_MATCH",
        "pattern_id": None,
        "pattern_name": None,
        "score": round(score, 3),
        "scaffold": None,
        "fallback_examples": [fallback_cf],
    }


def get_examples_for_control_flow(
    control_flow_type: Annotated[str, "Control flow type: Linear, IfElse, While, while_ifelse, UserGroup"],
    max_examples: Annotated[int, "Maximum number of examples to return"] = 2,
) -> list:
    """
    Retrieves example workflows for the given control flow type from data/examples/.
    Supports: Linear, IfElse, While, while_ifelse, UserGroup.
    Returns list of workflow dicts with source_file and workflow_raw_data.
    """
    data_dir = os.getenv("DATA_DIR", "/app/data")
    examples_dir = os.path.join(data_dir, "examples")

    type_map = {
        "linear": "linear",
        "ifelse": "ifelse",
        "while": "while",
        "while_ifelse": "while_ifelse",
        "while+ifelse": "while_ifelse",
        "usergroup": "usergroup",
    }
    normalized = type_map.get(control_flow_type.lower(), "linear")

    examples = []
    for i in range(1, 6):
        path = os.path.join(examples_dir, f"example_{normalized}_{i}.json")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                examples.append(json.load(f))
        if len(examples) >= max_examples:
            break

    return examples


def check_cooccurrence(
    activity_list: Annotated[list, "List of activity CustomTypeNames to check"],
) -> list:
    """
    Checks activity co-occurrence against the mined rank pairs.
    Returns warnings for missing strongly associated activities.

    Fields in activity_ranks.json: activity, next, rank
    """
    ranks = load_activity_ranks()
    warnings = []

    has_while = "WhileActivity" in activity_list
    has_count = "GetRowsCount" in activity_list

    if has_while and not has_count:
        warnings.append({
            "type": "missing_cooccurrence",
            "message": (
                "WhileActivity present but GetRowsCount not found. "
                "GetRowsCount must precede WhileActivity — confirmed in 80 of 97 loop sequences."
            ),
        })

    activity_set = set(activity_list)
    for pair in ranks[:50]:
        a1 = pair.get("activity", "")
        a2 = pair.get("next", "")
        freq = pair.get("rank", 0)
        if freq < 10:
            break
        if a1 in activity_set and a2 not in activity_set:
            warnings.append({
                "type": "missing_cooccurrence",
                "message": (
                    f"'{a1}' is present but strongly associated '{a2}' "
                    f"is missing (co-occurrence rank: {freq})."
                ),
            })

    return warnings
