import csv
import os
import re
from typing import Annotated

_valid_activities: set[str] = set()
_activity_descriptions: dict[str, str] = {}


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
        # Score on query words appearing in name+description
        hits = sum(1 for word in query_words if word in combined)
        # Bonus: camelCase-split activity name words appearing in query
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
    """
    load_activity_list()

    CONTROL_FLOW_INTENTS = {"branch", "parallel"}
    CONTROL_FLOW_CF = {"ifelse", "parallel"}

    manifest = []
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
            })
            continue

        candidates = retrieve_activities(description)

        if not candidates:
            manifest.append({
                "step_id": step_id,
                "query": description,
                "candidates": [],
                "selected_activity": None,
                "status": "UNAVAILABLE",
            })
            continue

        top = candidates[0]["activity_name"]
        validation = validate_activity(top)

        if validation["valid"]:
            manifest.append({
                "step_id": step_id,
                "query": description,
                "candidates": [
                    {"activity_name": c["activity_name"], "keyword_hits": c["keyword_hits"]}
                    for c in candidates[:3]
                ],
                "selected_activity": top,
                "status": "MATCHED",
            })
        else:
            manifest.append({
                "step_id": step_id,
                "query": description,
                "candidates": candidates[:3],
                "selected_activity": None,
                "status": "UNAVAILABLE",
            })

    return manifest
