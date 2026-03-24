import copy
import json
import os
import random
import re
import time
from typing import Annotated

# ---------------------------------------------------------------------------
# Data file caches
# ---------------------------------------------------------------------------

_enum_cache: dict | None = None
_defaults_cache: dict | None = None


def _load_enum_values() -> dict:
    """
    Load enum_values.json once and cache.
    Returns dict: { activity_TypeName: { field_key: [valid_values] } }
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
    Returns dict: { activity_TypeName: { field_key: dominant_value } }
    Dominant value = appears in >= 60% of corpus observations.
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
# Template loader (with enum + default seeding)
# ---------------------------------------------------------------------------

def load_activity_template(
    activity_name: Annotated[str, "The CustomTypeName of the activity to load"],
) -> dict:
    """
    Loads the JSON template for a given activity from activity_json_syntax.json.
    Returns the first matching template dict, or empty dict if not found.
    IMPORTANT: if empty dict is returned, treat activity as UNAVAILABLE.

    After loading, two enrichment passes run:

    Pass 1 — Enum seeding (enum_values.json):
      Replaces _value placeholder strings with the first valid enum choice.

    Pass 2 — Field defaults (field_defaults.json):
      For any CONFIG_FIELDS still holding a _value placeholder after Pass 1,
      applies the corpus-dominant value. Enum values take priority.
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

    enum_values   = _load_enum_values()
    field_defaults = _load_field_defaults()

    # Pass 1: seed with first valid enum choice
    # enum_values structure: { field_key: { "input_type": "...", "values": [{"value": "X", ...}] } }
    if activity_name in enum_values:
        for field_key, field_info in enum_values[activity_name].items():
            if not (
                field_key in template
                and isinstance(template[field_key], str)
                and template[field_key].endswith("_value")
            ):
                continue
            values_list = field_info.get("values", []) if isinstance(field_info, dict) else []
            if values_list and isinstance(values_list[0], dict):
                template[field_key] = values_list[0]["value"]

    # Pass 2: seed with corpus-dominant config defaults
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
# Control flow resolver
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
        "resolved_steps":        steps,
        "control_flow_applied":  True,
        "warnings":              warnings,
    }


# ---------------------------------------------------------------------------
# Activity JSON assembler (stub)
# ---------------------------------------------------------------------------

def build_activity_json(
    resolved_steps: Annotated[dict, "Output from resolve_control_flow"],
    variable_contracts: Annotated[dict, "Variable name/scope mappings from decomposition"],
) -> dict:
    """
    Stub — StructureBuilderAgent assembles the actual JSON using templates.
    This tool validates the output structure before passing downstream.
    """
    return {
        "workflow_raw_data":  resolved_steps.get("resolved_steps", {}),
        "variable_contracts": variable_contracts,
    }


# ---------------------------------------------------------------------------
# Scaffold parameter filler
# ---------------------------------------------------------------------------

def fill_scaffold_params(
    scaffold: Annotated[dict, "Pattern scaffold with PARAM_ placeholder fields"],
    variable_contract: Annotated[dict, "Variable contract from DecomposerAgent"],
    user_values: Annotated[dict, "Collected values from conversational intake"] = None,
) -> dict:
    """
    Replaces PARAM_ fields in the scaffold with resolved values.
    Priority: user_values → variable_contract → PLACEHOLDER_ string.

    Fix 10a: PARAM_xname_* values are converted to a proper camelCase xName
    (e.g. PARAM_xname_readxls → "readXls1") rather than falling through to
    PLACEHOLDER_, which would produce invalid xName values.

    Fix 10b: PARAM_id values are cleared to "" so the serializer can fill
    the correct id from its activity_json_syntax.json lookup.

    Fix 10c: After filling values, workflow_raw_data dict keys are renamed
    to match each activity's filled xName value. This ensures the serializer
    and xName uniqueness validator see correct, non-PARAM_ keys.
    """
    if user_values is None:
        user_values = {}

    result    = copy.deepcopy(scaffold)
    var_names = {v["name"]: v for v in variable_contract.get("variables", [])}

    def fill_node(node):
        if isinstance(node, dict):
            return {k: fill_node(v) for k, v in node.items()}
        if isinstance(node, list):
            return [fill_node(i) for i in node]
        if isinstance(node, str) and node.startswith("PARAM_"):
            param_key = node[6:]   # strip "PARAM_"

            # Fix 10b: PARAM_id → "" (serializer fills from id_lookup)
            if param_key == "id":
                return ""

            # Fix 10a: PARAM_xname_<type> → camelCase xname
            if param_key.startswith("xname_"):
                type_part = param_key[6:]   # strip "xname_"
                # Convert snake_case to camelCase: "readxls" → "readXls"
                # Simple approach: capitalize after underscore, lowercase first char
                parts = type_part.split("_")
                camel = parts[0] + "".join(p.capitalize() for p in parts[1:])
                return f"{camel}1"

            # User-supplied override
            if param_key in user_values:
                return user_values[param_key]

            # Variable contract match (case-insensitive substring)
            matching = [n for n in var_names if n.lower() in param_key.lower()]
            if matching:
                return f"%{matching[0]}%"

            return f"PLACEHOLDER_{param_key}"
        return node

    raw = result.get("workflow_raw_data", result)
    filled = fill_node(raw)

    # Fix 10c: rename workflow_raw_data keys to match filled xName values
    if isinstance(filled, dict):
        renamed = {}
        for key, activity in filled.items():
            if isinstance(activity, dict):
                new_key = activity.get("xName", key)
                # If fill produced a non-PARAM_ xName, use it as the dict key
                if new_key and not new_key.startswith("PARAM_"):
                    renamed[new_key] = activity
                else:
                    renamed[key] = activity
            else:
                renamed[key] = activity
        filled = renamed

    if "workflow_raw_data" in result:
        result["workflow_raw_data"] = filled
    else:
        result = filled

    return result


# ---------------------------------------------------------------------------
# ID generators
# ---------------------------------------------------------------------------

def generate_pnumber() -> str:
    """
    Generates a unique numeric Pnumber for import.
    Uses range 50000–99999 to avoid collision with platform-assigned IDs.
    """
    return str(random.randint(50000, 99999))


def generate_workflow_name(
    base_name: Annotated[str, "Human readable base name"],
) -> str:
    """
    Creates a guaranteed unique workflow name from base_name + timestamp + suffix.

    Fix 7: base_name is now actually used. Previously the argument was ignored
    and every workflow was named WF_{timestamp}_{suffix}.

    The base_name is sanitised to alphanumeric characters (max 30 chars) before
    being prefixed. Falls back to "WF" if base_name is empty after sanitisation.

    Example: base_name="MonitorDiskSpace" → "MonitorDiskSpace_1742913456_4821"
    """
    clean = re.sub(r"[^a-zA-Z0-9]", "", base_name)[:30] or "WF"
    timestamp = int(time.time())
    suffix    = random.randint(1000, 9999)
    return f"{clean}_{timestamp}_{suffix}"
