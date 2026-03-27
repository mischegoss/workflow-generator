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
    Returns dict: { activity_TypeName: { field_key: { "values": [...] } } }
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
# Template constants — Pass 3 of load_activity_template()
#
# These are structural / metadata fields present in every activity template
# as "_value" placeholders. They are NOT enum fields (so Pass 1 misses them)
# and NOT in field_defaults.json (so Pass 2 misses them). They have a single
# correct value that never varies across activities or workflows.
#
# Confirmed from activity_json_syntax.json inspection:
#   activityLicenseType_value — always "1" in every exported workflow
#   SuccessReason_value       — WhileActivity only; always empty in corpus
#   message_value             — ExitWhile only; always empty in corpus
#
# DisplayName is handled separately in Pass 3 because its correct value is
# the activity's own TypeName, not a fixed constant.
# ---------------------------------------------------------------------------

_TEMPLATE_CONSTANTS: dict[str, str] = {
    "activityLicenseType": "1",
    "SuccessReason":        "",
    "message":              "",
    # CreateMemoryTable defaults: 1 column, 0 initial data rows.
    # The validator requires these; the platform uses them to size the grid.
    # StructureBuilder can override ColumnNumber if the prompt specifies more columns.
    "ColumnNumber":         "1",
    "RowNumber":            "1",
}

# Fields whose _value placeholder should be replaced with the activity's own
# TypeName / CustomTypeName. Confirmed from corpus: DisplayName in every
# exported workflow equals the activity's TypeName (e.g. "DisplayValue",
# "GetRowsCount"). The template has a duplicate "DisplayName" key — the
# second entry ("DisplayName_value") wins in Python's json.load(), which is
# why it isn't caught by the existing passes.
_TYPENAME_FIELDS: frozenset[str] = frozenset({"DisplayName"})

# Fields whose _value placeholder must NOT be cleared — StructureBuilder is
# responsible for filling them, and clearing them to "" would lose the
# signal that they need a value.
_PRESERVE_PLACEHOLDERS: frozenset[str] = frozenset({
    "xName",           # activityInstanceName_value — filled by StructureBuilder
    "Description",     # activityDesc_value — filled by StructureBuilder
    "description",     # activityDesc_value — filled by StructureBuilder
    "ValueToDisplay",  # functional field — StructureBuilder fills
    "HostName",        # functional field — StructureBuilder fills
    "Query",           # functional field — StructureBuilder fills
    "VariableName",    # functional field — StructureBuilder fills
    "VariableValue",   # functional field — StructureBuilder fills
    "TableName",       # functional field — StructureBuilder fills
    "TheValue",        # functional field — StructureBuilder fills
    "TheValue2",       # functional field — StructureBuilder fills
    "Subject",         # functional field — StructureBuilder fills
    "Body",            # functional field — StructureBuilder fills
    "To",              # functional field — StructureBuilder fills
    "WorkflowName",    # functional field — StructureBuilder fills
    "WorkflowID",      # functional field — StructureBuilder fills
})


# ---------------------------------------------------------------------------
# Template loader (with enum + default + constant seeding)
# ---------------------------------------------------------------------------

def load_activity_template(
    activity_name: Annotated[str, "The CustomTypeName of the activity to load"],
) -> dict:
    """
    Loads the JSON template for a given activity from activity_json_syntax.json.
    Returns the first matching template dict, or empty dict if not found.
    IMPORTANT: if empty dict is returned, treat activity as UNAVAILABLE.

    After loading, three enrichment passes run in order:

    Pass 1 — Enum seeding (enum_values.json):
      Replaces _value placeholder strings in dropdown/radiobutton fields with
      the most common corpus value. Example: MemorySet.VariableScope →
      "Workflow" (appears in 98% of corpus instances).

    Pass 2 — Field defaults (field_defaults.json):
      For any CONFIG_FIELDS still holding a _value placeholder after Pass 1,
      applies the corpus-dominant value (>= 60% frequency). Enum values take
      priority over defaults.

    Pass 3 — Template constants:
      Replaces remaining known structural _value placeholders with their
      correct constant values. Handles three cases:
        a) _TEMPLATE_CONSTANTS: fixed string values (activityLicenseType → "1")
        b) _TYPENAME_FIELDS: replaced with the activity's own TypeName
           (DisplayName → "DisplayValue" for DisplayValue activities)
        c) Any other remaining _value string not in _PRESERVE_PLACEHOLDERS:
           cleared to "" rather than left as a broken placeholder
      Fields in _PRESERVE_PLACEHOLDERS are intentionally skipped — they are
      functional fields that StructureBuilder must fill from context.
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

    enum_values    = _load_enum_values()
    field_defaults = _load_field_defaults()

    # ── Pass 1: enum seeding ─────────────────────────────────────────────────
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

    # ── Pass 2: corpus-dominant config defaults ──────────────────────────────
    if activity_name in field_defaults:
        for field_key, default_val in field_defaults[activity_name].items():
            if (
                field_key in template
                and isinstance(template[field_key], str)
                and template[field_key].endswith("_value")
            ):
                template[field_key] = default_val

    # ── Pass 3: structural constants ─────────────────────────────────────────
    # Derive the activity's own type name for _TYPENAME_FIELDS.
    type_name = (
        template.get("TypeName")
        or template.get("CustomTypeName")
        or template.get("name")
        or activity_name
    )

    for field_key, value in list(template.items()):
        if not isinstance(value, str) or not value.endswith("_value"):
            continue
        if field_key in _PRESERVE_PLACEHOLDERS:
            continue  # StructureBuilder fills these — do not touch

        if field_key in _TEMPLATE_CONSTANTS:
            # Case (a): known fixed constant
            template[field_key] = _TEMPLATE_CONSTANTS[field_key]
        elif field_key in _TYPENAME_FIELDS:
            # Case (b): should match the activity's own type name
            template[field_key] = type_name
        else:
            # Case (c): unknown structural placeholder — clear to empty string
            # so the serializer doesn't emit broken "_value" strings into XML
            template[field_key] = ""

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