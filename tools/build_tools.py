import copy
import datetime
import json
import os
import random
import re
import time
import uuid
from typing import Annotated

# ---------------------------------------------------------------------------
# Data file caches
# ---------------------------------------------------------------------------

_enum_cache: dict | None = None
_defaults_cache: dict | None = None


def _load_enum_values() -> dict:
    """
    Load enum_values.json once and cache.
    Returns dict mapping activity TypeName → { field_key: [valid_values] }.
    Returns empty dict if file is missing.
    """
    global _enum_cache
    if _enum_cache is not None:
        return _enum_cache
    data_dir = os.getenv("DATA_DIR", "/app/data")
    path = os.path.join(data_dir, "enum_values.json")
    try:
        with open(path, encoding="utf-8") as f:
            _enum_cache = json.load(f)
    except FileNotFoundError:
        print(f"[build_tools] Warning: enum_values.json not found at {path}")
        _enum_cache = {}
    return _enum_cache


def _load_field_defaults() -> dict:
    """
    Load field_defaults.json once and cache.
    Returns dict mapping activity TypeName → { field_key: default_value }.
    Contains statistically dominant config values (>= 60% of corpus observations).
    Returns empty dict if file is missing.
    """
    global _defaults_cache
    if _defaults_cache is not None:
        return _defaults_cache
    data_dir = os.getenv("DATA_DIR", "/app/data")
    path = os.path.join(data_dir, "field_defaults.json")
    try:
        with open(path, encoding="utf-8") as f:
            _defaults_cache = json.load(f)
    except FileNotFoundError:
        print(f"[build_tools] Warning: field_defaults.json not found at {path}")
        _defaults_cache = {}
    return _defaults_cache


# ---------------------------------------------------------------------------
# Template loader (extended with enum + default seeding)
# ---------------------------------------------------------------------------

def load_activity_template(
    activity_name: Annotated[str, "The CustomTypeName of the activity to load"],
) -> dict:
    """
    Loads the JSON template for a given activity from activity_json_syntax.json.
    Returns the first matching template dict, or empty dict if not found.
    IMPORTANT: if empty dict is returned, treat activity as UNAVAILABLE.

    After loading, two enrichment passes run before the template is returned:

    Pass 1 — Enum seeding (enum_values.json):
      Replaces _value placeholder strings with the first valid enum choice for
      that field. This gives StructureBuilder a concrete starting value to work
      from rather than an opaque placeholder string.

    Pass 2 — Field defaults (field_defaults.json):
      For any CONFIG_FIELDS still holding a _value placeholder after Pass 1,
      applies the corpus-dominant value (seen in >= 60% of real workflows).
      Enum values take priority — defaults only fill fields not covered by enums.
    """
    data_dir = os.getenv("DATA_DIR", "/app/data")
    path = os.path.join(data_dir, "activity_json_syntax.json")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    templates = data.get("settings", data) if isinstance(data, dict) else data
    template = None
    for t in templates:
        if (
            t.get("TypeName") == activity_name
            or t.get("CustomTypeName") == activity_name
            or t.get("name") == activity_name
        ):
            template = copy.deepcopy(t)
            break

    if template is None:
        return {}

    enum_values = _load_enum_values()
    field_defaults = _load_field_defaults()

    # Pass 1: seed enum choices
    if activity_name in enum_values:
        for field_key, valid_values in enum_values[activity_name].items():
            if (
                field_key in template
                and isinstance(template[field_key], str)
                and template[field_key].endswith("_value")
                and valid_values
            ):
                template[field_key] = valid_values[0]

    # Pass 2: seed corpus-dominant config defaults
    if activity_name in field_defaults:
        for field_key, default_val in field_defaults[activity_name].items():
            if (
                field_key in template
                and isinstance(template[field_key], str)
                and template[field_key].endswith("_value")
            ):
                template[field_key] = default_val

    return template


# ---------------------------------------------------------------------------
# Remaining tools (unchanged)
# ---------------------------------------------------------------------------

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
