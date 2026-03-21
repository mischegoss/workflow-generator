import json
import os
from typing import Annotated

_pattern_library: list[dict] | None = None
_activity_ranks: list[dict] | None = None


def load_pattern_library() -> list[dict]:
    """
    Loads pattern_library.json from data/patterns/. Cached after first load.
    Returns empty list if file does not exist — pipeline falls through to example-guided mode.
    """
    global _pattern_library
    if _pattern_library is not None:
        return _pattern_library
    data_dir = os.getenv("DATA_DIR", "/app/data")
    path = os.path.join(data_dir, "patterns", "pattern_library.json")
    if not os.path.exists(path):
        print("[patterns] pattern_library.json not found — NO_MATCH mode only.")
        _pattern_library = []
        return _pattern_library
    with open(path, encoding="utf-8") as f:
        _pattern_library = json.load(f)
    print(f"[patterns] Loaded {len(_pattern_library)} patterns.")
    return _pattern_library


def load_activity_ranks() -> list[dict]:
    """
    Loads activity_ranks.json — 271 activity pair frequencies from real execution history.
    Used for co-occurrence validation (e.g. flag if GetRowsCount missing before WhileActivity).
    """
    global _activity_ranks
    if _activity_ranks is not None:
        return _activity_ranks
    data_dir = os.getenv("DATA_DIR", "/app/data")
    path = os.path.join(data_dir, "activity_ranks.json")
    if not os.path.exists(path):
        _activity_ranks = []
        return _activity_ranks
    with open(path, encoding="utf-8") as f:
        _activity_ranks = json.load(f)
    print(f"[patterns] Loaded {len(_activity_ranks)} activity rank pairs.")
    return _activity_ranks


def match_pattern(
    decomposition: Annotated[dict, "Decomposition output from DecomposerAgent"],
) -> list[dict]:
    """
    Finds candidate patterns from the library matching the decomposition.
    Scores on: control_flow type match (0.6 weight) + trigger keyword hits (0.4 weight).
    Returns top 5 candidates sorted by score descending.
    """
    library = load_pattern_library()
    if not library:
        return []

    loop_type = (
        decomposition.get("variable_contract", {})
        .get("loop_type", "none")
        .lower()
    )
    steps = decomposition.get("steps", [])
    step_text = " ".join(
        s.get("description", "") for s in steps
    ).lower()

    # Map decomposition loop_type to pattern control_flow labels
    cf_map = {
        "while": "While",
        "foreach": "While",   # ForEach not in corpus — map to While
        "none": "Linear",
        "ifelse": "IfElse",
        "usergroup": "UserGroup",
    }
    target_cf = cf_map.get(loop_type, "Linear")

    scored = []
    for pattern in library:
        pattern_cf = pattern.get("control_flow", "Linear")
        cf_score = 1.0 if pattern_cf == target_cf else 0.0

        keywords = pattern.get("trigger_keywords", [])
        kw_hits = sum(1 for kw in keywords if kw.lower() in step_text)
        kw_score = min(kw_hits / max(len(keywords), 1), 1.0)

        raw_score = (cf_score * 0.6) + (kw_score * 0.4)
        if raw_score > 0:
            scored.append({**pattern, "_score": round(raw_score, 3)})

    return sorted(scored, key=lambda x: x["_score"], reverse=True)[:5]


def score_pattern_match(
    candidates: Annotated[list, "Top candidates from match_pattern"],
    threshold: Annotated[float, "Match threshold (default from env)"] = None,
) -> dict:
    """
    Applies threshold gate to top candidate.
    Returns MATCHED with scaffold, or NO_MATCH with fallback example IDs.
    """
    if threshold is None:
        threshold = float(os.getenv("PATTERN_MATCH_THRESHOLD", "0.80"))

    if not candidates:
        return {
            "match_status": "NO_MATCH",
            "pattern_id": None,
            "pattern_name": None,
            "score": 0.0,
            "scaffold": None,
            "fallback_examples": [],
        }

    top = candidates[0]
    score = top["_score"]

    if score >= threshold:
        return {
            "match_status": "MATCHED",
            "pattern_id": top.get("pattern_id"),
            "pattern_name": top.get("control_flow"),
            "score": score,
            "scaffold": top.get("scaffold"),
            "fallback_examples": [],
        }

    # No match — return closest control_flow type for example selection
    cf = top.get("control_flow", "Linear")
    return {
        "match_status": "NO_MATCH",
        "pattern_id": None,
        "pattern_name": None,
        "score": score,
        "scaffold": None,
        "fallback_examples": [cf],   # used by StructureBuilder to pick examples
    }


def get_examples_for_control_flow(
    control_flow: Annotated[str, "Control flow type: Linear | IfElse | While | While+IfElse | UserGroup"],
    max_examples: Annotated[int, "Max examples to return"] = 2,
) -> list[dict]:
    """
    Loads up to max_examples example workflows for the given control flow type.
    Used by StructureBuilder in example-guided mode.
    Returns list of workflow_raw_data dicts.
    """
    data_dir = os.getenv("DATA_DIR", "/app/data")
    index_path = os.path.join(data_dir, "examples", "examples_index.json")

    if not os.path.exists(index_path):
        return []

    with open(index_path, encoding="utf-8") as f:
        index = json.load(f)

    # Normalize control_flow label for matching
    cf_norm = control_flow.lower().replace("+", "_").replace(" ", "_")
    cf_map = {
        "while_ifelse": "while_ifelse",
        "while+ifelse": "while_ifelse",
        "while": "while",
        "ifelse": "ifelse",
        "linear": "linear",
        "usergroup": "usergroup",
    }
    target = cf_map.get(cf_norm, "linear")

    matches = [e for e in index if e.get("control_flow", "").lower() == target]
    examples = []
    for entry in matches[:max_examples]:
        ex_path = os.path.join(data_dir, "examples", entry["file"])
        if os.path.exists(ex_path):
            with open(ex_path, encoding="utf-8") as f:
                ex = json.load(f)
            examples.append({
                "source_file": entry["source_file"],
                "control_flow": entry["control_flow"],
                "activity_types_present": entry["activity_types_present"],
                "workflow_raw_data": ex.get("workflow_raw_data", {}),
            })
    return examples


def check_cooccurrence(
    activity_sequence: Annotated[list, "Ordered list of activity type names in the workflow"],
) -> list[dict]:
    """
    Checks the sequence against known high-frequency pairs from activity_ranks.json.
    Flags missing expected follow-on activities as warnings (not errors).
    E.g. flags if WhileActivity appears but GetRowsCount does not precede it.
    """
    ranks = load_activity_ranks()
    if not ranks:
        return []

    # Build a set of high-confidence pairs (rank > 50)
    strong_pairs = {
        (r["activity"], r["next"]): r["rank"]
        for r in ranks if r.get("rank", 0) > 50
    }

    warnings = []
    seq_set = set(activity_sequence)

    # Key structural rules derived from corpus
    if "WhileActivity" in seq_set and "GetRowsCount" not in seq_set:
        warnings.append({
            "type": "missing_cooccurrence",
            "message": "WhileActivity present but GetRowsCount not found. "
                       "GetRowsCount → WhileActivity appears 147x in corpus — "
                       "verify loop bound source.",
            "severity": "warning",
        })

    if "ForEachActivity" in seq_set:
        warnings.append({
            "type": "corpus_mismatch",
            "message": "ForEachActivity used but appears in 0 of 625 real workflows. "
                       "Consider WhileActivity instead.",
            "severity": "warning",
        })

    if "SendEmail" in seq_set and "ConvertToHTMLTable" not in seq_set:
        # ConvertToHTMLTable → SendEmail is 214x in corpus
        # Not a hard rule but worth noting for table-based content
        pass  # Only flag if table data is in the workflow — skip for now

    return warnings
