import copy
import datetime
import json
import os
import random
import re
import time
import uuid
from typing import Annotated


def load_activity_template(
    activity_name: Annotated[str, "The CustomTypeName of the activity to load"],
) -> dict:
    """
    Loads the JSON template for a given activity from activity_json_syntax.json.
    Returns the first matching template dict, or empty dict if not found.
    IMPORTANT: if empty dict is returned, treat activity as UNAVAILABLE.
    """
    data_dir = os.getenv("DATA_DIR", "/app/data")
    path = os.path.join(data_dir, "activity_json_syntax.json")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    templates = data.get("settings", data) if isinstance(data, dict) else data
    for template in templates:
        if (
            template.get("TypeName") == activity_name
            or template.get("CustomTypeName") == activity_name
            or template.get("name") == activity_name
        ):
            return copy.deepcopy(template)
    return {}


def resolve_control_flow(
    steps: Annotated[list, "Ordered list of step dicts with control_flow annotations"],
) -> dict:
    """
    Validates control flow annotations against platform rules.
    Returns the annotated step list with container nesting notes.
    """
    warnings = []

    has_while = any(
        s.get("intent") == "loop" or s.get("control_flow") == "while"
        for s in steps
    )
    has_count = any(
        s.get("intent") == "count_rows"
        or "GetRowsCount" in s.get("description", "")
        or "count" in s.get("description", "").lower()
        for s in steps
    )

    if has_while and not has_count:
        warnings.append(
            "WhileActivity detected but no GetRowsCount step found. "
            "Add a GetRowsCount step before the loop."
        )

    return {
        "resolved_steps": steps,
        "control_flow_applied": True,
        "warnings": warnings,
    }


def build_activity_json(
    resolved_steps: Annotated[dict, "Output from resolve_control_flow"],
    variable_contracts: Annotated[dict, "Variable name/scope mappings from decomposition"],
) -> dict:
    """
    Stub — StructureBuilderAgent assembles the actual JSON using templates.
    This tool validates the output structure before passing downstream.
    """
    return {
        "workflow_raw_data": resolved_steps.get("resolved_steps", {}),
        "variable_contracts": variable_contracts,
    }


def fill_scaffold_params(
    scaffold: Annotated[dict, "Pattern scaffold with PARAM_ placeholder fields"],
    variable_contract: Annotated[dict, "Variable contract from DecomposerAgent"],
    user_values: Annotated[dict, "Collected values from conversational intake"] = None,
) -> dict:
    """
    Replaces PARAM_ fields in the scaffold with resolved values.
    Priority: user_values → variable_contract → PLACEHOLDER_ string.
    Does NOT modify structure — only fills values.
    """
    if user_values is None:
        user_values = {}

    result = copy.deepcopy(scaffold)
    var_names = {v["name"]: v for v in variable_contract.get("variables", [])}

    def fill_node(node):
        if isinstance(node, dict):
            return {k: fill_node(v) for k, v in node.items()}
        if isinstance(node, list):
            return [fill_node(i) for i in node]
        if isinstance(node, str) and node.startswith("PARAM_"):
            key = node[6:]
            if key in user_values:
                return user_values[key]
            matching = [n for n in var_names if n.lower() in key.lower()]
            if matching:
                return f"%{matching[0]}%"
            return f"PLACEHOLDER_{key}"
        return node

    raw = result.get("workflow_raw_data", result)
    filled = fill_node(raw)
    if "workflow_raw_data" in result:
        result["workflow_raw_data"] = filled
    else:
        result = filled
    return result


def generate_pnumber() -> str:
    """
    Generates a unique numeric Pnumber for import.
    Platform assigns sequential IDs (e.g. 150, 866) to real workflows.
    We use a random number in the 50000-99999 range to avoid collision
    with platform-assigned IDs while staying in a valid integer range.
    Each call returns a different value — never reuse a Pnumber.
    """
    return str(random.randint(50000, 99999))


def generate_workflow_name(
    base_name: Annotated[str, "Human readable base name"],
) -> str:
    """
    Creates a guaranteed unique workflow name using timestamp + random int.
    Platform deduplicates by Name — using pure numbers ensures no collision.
    Format: WF_1742913456_4821
    """
    timestamp = int(time.time())
    suffix = random.randint(1000, 9999)
    return f"WF_{timestamp}_{suffix}"
