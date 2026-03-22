import csv
import json
import os
import re
from typing import Annotated

_valid_activities: set[str] = set()
_activity_descriptions: dict[str, str] = {}

# Activity frequency ranks loaded from activity_ranks.json.
# Used for re-ranking retrieval candidates and surfacing frequency tier.
# Fields in activity_ranks.json: activity, next, rank
_activity_ranks: list | None = None
_rank_lookup: dict[str, int] | None = None   # activityName → rank position (0 = most common)
_rank_scores: dict[str, int] | None = None   # activityName → aggregated rank score

# Re-ranking weight: how much co-occurrence frequency boosts a semantic match.
# λ = 0.3 means a perfect semantic match (1.0) can be overridden by a high-frequency
# activity with a slightly lower semantic score.
_COOCCURRENCE_LAMBDA = 0.3


def load_activity_list() -> None:
    """
    Loads activity_list.txt into memory. Safe to call on every startup — no-op if already loaded.
    """
    global _valid_activities, _activity_descriptions
    if _valid_activities:
        return
    data_dir = os.getenv("DATA_DIR", "/app/data")
    path = os.path.join(data_dir, "activity_list.txt")
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f, delimiter="\t"):
            name = row["name"].strip()
            _valid_activities.add(name)
            _activity_descriptions[name] = row.get("description", "").strip()
    print(f"[retrieval] Loaded {len(_valid_activities)} activities into memory.")


def _load_rank_data() -> tuple[dict, dict]:
    """
    Loads activity_ranks.json and builds two lookup dicts:
      _rank_lookup:  activityName → rank position (0 = most frequent)
      _rank_scores:  activityName → aggregated rank score

    Fields in activity_ranks.json: activity, next, rank
    """
    global _activity_ranks, _rank_lookup, _rank_scores
    if _rank_lookup is not None:
        return _rank_lookup, _rank_scores

    data_dir = os.getenv("DATA_DIR", "/app/data")
    path = os.path.join(data_dir, "activity_ranks.json")
    try:
        with open(path, encoding="utf-8") as f:
            _activity_ranks = json.load(f)
    except Exception:
        _activity_ranks = []

    # Build aggregated score per activity: sum of all pair ranks where
    # this activity appears as either activity or next.
    # Higher score = more commonly used in real workflows.
    freq: dict[str, int] = {}
    for pair in _activity_ranks:
        a1 = pair.get("activity", "")   # first activity in sequence
        a2 = pair.get("next", "")       # activity that follows
        f  = pair.get("rank", 0)        # co-occurrence count
        if a1:
            freq[a1] = freq.get(a1, 0) + f
        if a2:
            freq[a2] = freq.get(a2, 0) + f

    # Sort by score descending → rank position (0 = most common)
    sorted_acts = sorted(freq.items(), key=lambda x: -x[1])
    _rank_lookup = {name: idx for idx, (name, _) in enumerate(sorted_acts)}
    _rank_scores = {name: score for name, score in sorted_acts}

    print(f"[retrieval] Loaded rank data for {len(_rank_lookup)} activities.")
    return _rank_lookup, _rank_scores


def _frequency_tier(activity_name: str, rank_scores: dict) -> str:
    """
    Returns a frequency tier label based on the activity's aggregated rank score.
    Used to signal the StructureBuilder which activities are platform staples.
    """
    score = rank_scores.get(activity_name, 0)
    if score == 0:
        return "low"
    max_score = max(rank_scores.values()) if rank_scores else 1
    ratio = score / max_score
    if ratio >= 0.4:
        return "high"
    if ratio >= 0.1:
        return "medium"
    return "low"


def _rerank_candidates(
    candidates: list[dict],
    confirmed_activities: set[str],
    rank_scores: dict,
) -> list[dict]:
    """
    Re-ranks retrieval candidates using co-occurrence frequency.

    Scoring: combined_score = semantic_score + λ * cooccurrence_boost

    semantic_score:     normalised keyword hit count (0.0–1.0)
    cooccurrence_boost: normalised score based on how often this activity
                        appears alongside activities already confirmed in
                        the manifest for this workflow run.

    This ensures high-frequency platform activities beat low-frequency ones
    when semantic scores are close.
    """
    if not candidates:
        return candidates

    _, _ = _load_rank_data()
    max_hits = max(c["keyword_hits"] for c in candidates) or 1
    max_score = max(rank_scores.values()) if rank_scores else 1

    def cooccurrence_score(name: str) -> float:
        if not _activity_ranks or not confirmed_activities:
            return rank_scores.get(name, 0) / max_score
        total = 0
        for pair in _activity_ranks:
            a1 = pair.get("activity", "")
            a2 = pair.get("next", "")
            f  = pair.get("rank", 0)
            if (a1 == name and a2 in confirmed_activities) or \
               (a2 == name and a1 in confirmed_activities):
                total += f
        # Fall back to raw frequency if no co-occurrence with confirmed activities yet
        if total == 0:
            return rank_scores.get(name, 0) / max_score
        return min(total / max_score, 1.0)

    for c in candidates:
        semantic = c["keyword_hits"] / max_hits
        cooc = cooccurrence_score(c["activity_name"])
        c["combined_score"] = round(semantic + _COOCCURRENCE_LAMBDA * cooc, 4)

    return sorted(candidates, key=lambda x: x["combined_score"], reverse=True)


def retrieve_activities(
    query: Annotated[str, "Natural language description of what this step needs to do"],
) -> list[dict]:
    """
    Returns activity candidates from the in-memory list using keyword matching.
    Matches on both whole query words and camelCase-split activity name words.
    Returns top 10 candidates sorted by keyword hit count.
    """
    load_activity_list()
    query_lower = query.lower()
    query_words = [w for w in query_lower.split() if len(w) > 3]
    candidates = []
    for name, desc in _activity_descriptions.items():
        combined = f"{name} {desc}".lower()
        hits = sum(1 for word in query_words if word in combined)
        name_words = [w.lower() for w in re.sub(r'([A-Z])', r' \1', name).split() if len(w) > 3]
        name_bonus = sum(1 for w in name_words if w in query_lower)
        total = hits + name_bonus
        if total > 0:
            candidates.append({
                "activity_name": name,
                "description": desc,
                "keyword_hits": total,
            })
    return sorted(candidates, key=lambda x: x["keyword_hits"], reverse=True)[:10]


def validate_activity(
    activity_name: Annotated[str, "Activity name to validate against the confirmed list"],
) -> dict:
    """
    Hard gate — confirms the activity name exists in activity_list.txt.
    If load_activity_template() returns an empty dict for this activity,
    treat it as UNAVAILABLE regardless of this check passing.
    """
    load_activity_list()
    if activity_name in _valid_activities:
        return {"valid": True, "activity_name": activity_name}
    close = [n for n in _valid_activities if activity_name.lower() in n.lower()][:3]
    return {
        "valid": False,
        "activity_name": activity_name,
        "reason": f"'{activity_name}' not found in confirmed activity list.",
        "suggestions": close,
    }


def get_activity_description(
    activity_name: Annotated[str, "Activity name to look up"],
) -> str:
    """Returns the description for a known activity, or empty string if not found."""
    load_activity_list()
    return _activity_descriptions.get(activity_name, "")


def retrieve_all_steps(
    steps: Annotated[list, "Full list of step dicts from decomposition"],
) -> list[dict]:
    """
    Retrieves and validates activity candidates for ALL steps in one call.
    Returns the complete manifest in one shot — no per-step looping needed.

    Enhancements over baseline:
    - Co-occurrence re-ranking: candidates are re-ranked using activity_ranks.json
      so that high-frequency platform activities beat low-frequency ones when
      semantic scores are close.
    - frequency_tier: each manifest entry includes a tier label (high/medium/low)
      derived from the activity's aggregated rank score. The StructureBuilder uses
      this to prefer mainstream activities during assembly.
    """
    load_activity_list()
    rank_lookup, rank_scores = _load_rank_data()

    CONTROL_FLOW_INTENTS = {"branch", "parallel"}
    CONTROL_FLOW_CF = {"ifelse", "parallel"}

    manifest = []
    confirmed_activities: set[str] = set()  # activities confirmed earlier in this manifest

    for step in steps:
        step_id = step.get("step_id", "")
        description = step.get("description", "")
        control_flow = step.get("control_flow", "linear")
        intent = step.get("intent", "")

        # Control flow containers — not in activity list
        if control_flow in CONTROL_FLOW_CF or intent in CONTROL_FLOW_INTENTS:
            manifest.append({
                "step_id": step_id,
                "query": description,
                "candidates": [],
                "selected_activity": "IfElseActivity",
                "status": "CONTROL_FLOW",
                "frequency_tier": "high",
            })
            confirmed_activities.add("IfElseActivity")
            continue

        raw_candidates = retrieve_activities(description)

        if not raw_candidates:
            manifest.append({
                "step_id": step_id,
                "query": description,
                "candidates": [],
                "selected_activity": None,
                "status": "UNAVAILABLE",
                "frequency_tier": "low",
            })
            continue

        # Re-rank using co-occurrence with already-confirmed activities
        reranked = _rerank_candidates(raw_candidates, confirmed_activities, rank_scores)
        top = reranked[0]["activity_name"]
        validation = validate_activity(top)

        if validation["valid"]:
            tier = _frequency_tier(top, rank_scores)
            manifest.append({
                "step_id": step_id,
                "query": description,
                "candidates": [
                    {
                        "activity_name": c["activity_name"],
                        "keyword_hits": c["keyword_hits"],
                        "combined_score": c.get("combined_score", c["keyword_hits"]),
                    }
                    for c in reranked[:3]
                ],
                "selected_activity": top,
                "status": "MATCHED",
                "frequency_tier": tier,
            })
            confirmed_activities.add(top)
        else:
            manifest.append({
                "step_id": step_id,
                "query": description,
                "candidates": reranked[:3],
                "selected_activity": None,
                "status": "UNAVAILABLE",
                "frequency_tier": "low",
            })

    return manifest
