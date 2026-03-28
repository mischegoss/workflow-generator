import json
import os
import re
from typing import Annotated

_controls_index: dict | None = None
_enum_values: dict | None = None

VALID_CONDITION_TYPES = {
    "", "Equals", "Equal", "Contains", "Not Contains", "Not Equals",
    "Formula", ">", "<", ">=", "<="
}

CONTAINER_TYPES = {
    "WhileActivity", "SequenceActivity", "IfElseActivity", "IfElseBranchActivity",
    "ParallelActivity", "UserGroup", "ForEachActivity", "ExitWhile", "ReturnValue",
    "IfElseCondition",
}

# All propertiesControl fields across all activities are opaque UI blobs
# that cannot be serialized from outside the platform. Confirmed from
# activities_controls.json — all 4 propertiesControl fieldKeys.
# Also includes TargetModuleName and TemplateName which require UI selection.
EXCLUDED_REQUIRED_FIELDS = {
    "XMLTableResult",
    "XMLTableSelectionResult",
    "DictionaryAsXml",
    "FieldsList",
    "TargetModuleName",
    "TemplateName",
    "ColumnType",
    # CreateMemoryTable grid-sizing fields — configured post-import in the platform UI.
    # The platform accepts these as empty and lets the user define columns/rows there.
    "ColumnNumber",
    "RowNumber",
}

# Formula pattern: =ConditionType(&&&,Value) — no quotes on either operand
# Valid examples: =Equals(&&&,Success)  =<=(&&&,5)  =Contains(&&&,ERROR)
_FORMULA_PATTERN = re.compile(r'^=.+\(&&&,.+\)$')


def _ensure_dict(value) -> dict:
    """
    Safely converts string JSON to dict.
    Agents sometimes pass session state as JSON strings instead of dicts
    when output_key values are serialized between pipeline stages.
    """
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            result = json.loads(value)
            if isinstance(result, dict):
                return result
        except Exception:
            pass
    return {}


def _load_controls_index() -> dict:
    global _controls_index
    if _controls_index is not None:
        return _controls_index
    data_dir = os.getenv("DATA_DIR", "/app/data")
    path = os.path.join(data_dir, "activities_controls.json")
    with open(path, encoding="utf-8") as f:
        controls = json.load(f)
    _controls_index = {
        entry["activityName"]: entry.get("controls", [])
        for entry in controls
    }
    return _controls_index


def _load_enum_values() -> dict:
    """
    Load enum_values.json once and cache.
    Returns dict: { activityName: { fieldKey: { values: [...] } } }
    """
    global _enum_values
    if _enum_values is not None:
        return _enum_values
    data_dir = os.getenv("DATA_DIR", "/app/data")
    path = os.path.join(data_dir, "enum_values.json")
    try:
        with open(path, encoding="utf-8") as f:
            _enum_values = json.load(f)
    except FileNotFoundError:
        _enum_values = {}
    return _enum_values


def validate_xname_uniqueness(
    workflow_json: Annotated[dict, "Workflow JSON dict"],
) -> dict:
    """Every xName in the workflow must be unique across all activities including nested ones."""
    workflow_json = _ensure_dict(workflow_json)
    seen = {}
    duplicates = []

    def walk(node: dict, path: str = ""):
        xname = node.get("xName", "")
        if xname:
            if xname in seen:
                duplicates.append(
                    f"Duplicate xName '{xname}' at {path} (first seen at {seen[xname]})"
                )
            else:
                seen[xname] = path or "root"
        for key, value in node.items():
            if isinstance(value, dict):
                walk(value, f"{path}.{key}")

    raw = workflow_json.get("workflow_raw_data", {})
    for xname, activity in raw.items():
        if isinstance(activity, dict):
            walk(activity, xname)

    if duplicates:
        return {"passed": False, "errors": duplicates}
    return {"passed": True, "errors": []}


def validate_activity_schema(
    workflow_json: Annotated[dict, "Workflow JSON dict"],
) -> dict:
    """
    Checks required fields and enum values per activities_controls.json
    and enum_values.json.

    Required field checks:
      Skips EXCLUDED_REQUIRED_FIELDS (propertiesControl blobs and UI-configured fields).
      If activity not in index, adds a VERIFY note instead of failing.

    Enum value checks:
      For fields that have known allowed values in enum_values.json, flags any value
      that is not in the allowed list and is not a variable reference (not starting
      with %). Only fires when enum_values.json has a non-empty values list for that
      field — entries with zero observations are still used for validation because
      they represent manually confirmed platform values (Step 5).
    """
    workflow_json = _ensure_dict(workflow_json)
    controls_index = _load_controls_index()
    enum_values = _load_enum_values()
    errors = []
    verify_notes = []

    def check_node(node: dict, path: str = ""):
        type_name = node.get("TypeName") or node.get("CustomTypeName", "")

        if type_name and type_name not in CONTAINER_TYPES:
            if type_name in controls_index:
                for control in controls_index[type_name]:
                    field_key = control["fieldKey"]

                    # --- Required field check ---
                    if (
                        control.get("required")
                        and field_key not in node
                        and field_key not in EXCLUDED_REQUIRED_FIELDS
                    ):
                        errors.append(
                            f"[{path}] '{type_name}' missing required field "
                            f"'{field_key}' ({control['fieldName']})"
                        )

                    # --- Enum value check ---
                    # Only fire when the field is present with a non-variable, non-null value
                    if field_key in node:
                        val = str(node[field_key])
                        if val and not val.startswith("%") and val not in ("{x:Null}", ""):
                            enum_entry = (
                                enum_values
                                .get(type_name, {})
                                .get(field_key, {})
                            )
                            # Extract allowed values — skip annotation-only dict entries
                            allowed = [
                                str(v["value"])
                                for v in enum_entry.get("values", [])
                                if isinstance(v, dict) and "value" in v and "note" not in v
                            ]
                            if allowed and val not in allowed:
                                errors.append(
                                    f"[{path}] '{type_name}.{field_key}' = '{val}' "
                                    f"is not a valid option. "
                                    f"Allowed: {allowed[:6]}"
                                    + (" ..." if len(allowed) > 6 else "")
                                )
            else:
                if type_name:
                    verify_notes.append(
                        f"[{path}] '{type_name}' has no controls entry — "
                        "required fields unverified. Check mandatory fields manually."
                    )

        for key, value in node.items():
            if isinstance(value, dict):
                check_node(value, f"{path}.{key}")

    raw = workflow_json.get("workflow_raw_data", {})
    check_node(raw)

    if errors:
        return {"passed": False, "errors": errors, "verify_notes": verify_notes}
    return {"passed": True, "errors": [], "verify_notes": verify_notes}


def validate_control_flow_rules(
    workflow_json: Annotated[dict, "Workflow JSON dict"],
) -> dict:
    """
    Enforces platform-specific control flow rules confirmed from real workflow exports.

    Rules enforced:
    - ReturnValue must only appear inside IfElseBranchActivity (confirmed RitaLab March 2026:
      ReturnValue at top level or inside WhileActivity causes 'Template is not a member of
      Network' compilation error on import)
    - WhileActivity must not carry Counter (belongs on ExitWhile)
    - ExitWhile must have Counter
    - ForEachOutputVariableName must not start with 'forEach'
    - ReturnValue ConditionType must be a confirmed valid value
    - ReturnValue Formula must match =ConditionType(&&&,Value) format when ConditionType is set
      (Formula is now built deterministically by the serializer, but we validate here as a safety net)
    - Continue inside IfElseBranchActivity is flagged as a VERIFY warning (likely filler)
    """
    workflow_json = _ensure_dict(workflow_json)
    errors = []
    verify_notes = []

    def check_node(node: dict, parent_type: str = "", path: str = ""):
        type_name = node.get("CustomTypeName", "")

        # --- ReturnValue: must only appear inside IfElseBranchActivity ---
        # Confirmed RitaLab March 2026: ReturnValue at top level or inside
        # WhileActivity/SequenceActivity directly causes "Activity compilation error:
        # 'Template' is not a member of 'Network'" on import.
        # Linear workflows and UserGroup workflows do not use ReturnValue.
        if type_name == "ReturnValue" and parent_type not in (
            "IfElseBranchActivity", "ReturnValue"
        ):
            errors.append(
                f"[{path}] ReturnValue is only valid inside IfElseBranchActivity. "
                f"Found inside '{parent_type or 'workflow_raw_data'}'. "
                "Remove it — linear workflows and UserGroup workflows do not use ReturnValue."
            )

        # --- WhileActivity: Counter must NOT be at this level ---
        if type_name == "WhileActivity" and "Counter" in node:
            errors.append(
                f"[{path}] WhileActivity must not have Counter at its own level — "
                "Counter belongs on ExitWhile only."
            )

        # --- WhileActivity: must have a nested SequenceActivity with activities ---
        if type_name == "WhileActivity":
            seq = next(
                (v for v in node.values()
                 if isinstance(v, dict) and v.get("CustomTypeName") == "SequenceActivity"),
                None,
            )
            if seq is None:
                errors.append(
                    f"[{path}] WhileActivity has no nested SequenceActivity. "
                    "All loop body activities (ExitWhile, GetCellValue, action activities) "
                    "must be nested inside a SequenceActivity child of WhileActivity. "
                    "Do not place them at the top level of workflow_raw_data."
                )
            else:
                body_activities = [
                    v for v in seq.values()
                    if isinstance(v, dict) and v.get("CustomTypeName")
                ]
                if not body_activities:
                    errors.append(
                        f"[{path}] WhileActivity SequenceActivity is empty — "
                        "no loop body activities were generated. "
                        "Add ExitWhile, GetCellValue, and action activities "
                        "inside the SequenceActivity."
                    )

        # --- ExitWhile: Counter is required ---
        if type_name == "ExitWhile" and "Counter" not in node:
            errors.append(
                f"[{path}] ExitWhile is missing required Counter attribute."
            )

        # --- ForEachOutputVariableName prefix check ---
        if "ForEachOutputVariableName" in node:
            val = node["ForEachOutputVariableName"]
            if val.lower().startswith("foreach"):
                errors.append(
                    f"[{path}] ForEachOutputVariableName '{val}' must not be prefixed "
                    "with 'forEach' — causes xName collision."
                )

        # --- ReturnValue: ConditionType and Formula validation ---
        if type_name == "ReturnValue":
            ct = node.get("ConditionType", "")
            if ct not in VALID_CONDITION_TYPES:
                errors.append(
                    f"[{path}] ReturnValue has unconfirmed ConditionType '{ct}'. "
                    f"Confirmed values: {sorted(VALID_CONDITION_TYPES)}."
                )

            formula = node.get("Formula", "")
            is_valid = node.get("IsValid", "True")
            if (
                ct
                and is_valid == "True"
                and formula
                and formula != "{x:Null}"
                and not _FORMULA_PATTERN.match(formula)
            ):
                errors.append(
                    f"[{path}] ReturnValue Formula '{formula}' is malformed. "
                    f"Expected format: =ConditionType(&&&,Value) with no quoted operands. "
                    f"Example for ConditionType='{ct}': ={ct}(&&&,<value>)"
                )

        # --- Continue inside IfElseBranchActivity is almost always filler ---
        if type_name == "Continue" and parent_type == "IfElseBranchActivity":
            verify_notes.append(
                f"[{path}] Continue inside IfElseBranchActivity is likely unnecessary filler. "
                "If this branch performs no action, remove Continue and leave the branch empty. "
                "The workflow proceeds automatically without an explicit continue."
            )

        for key, value in node.items():
            if isinstance(value, dict):
                check_node(value, parent_type=type_name, path=f"{path}.{key}")

    raw = workflow_json.get("workflow_raw_data", {})
    check_node(raw)

    if errors:
        return {"passed": False, "errors": errors, "verify_notes": verify_notes}
    return {"passed": True, "errors": [], "verify_notes": verify_notes}


def validate_required_fields(
    workflow_json: Annotated[dict, "Workflow JSON dict"],
) -> dict:
    """Checks every leaf activity has both Description (uppercase) and description (lowercase)."""
    workflow_json = _ensure_dict(workflow_json)
    errors = []

    def check_node(node: dict, path: str = ""):
        type_name = node.get("CustomTypeName", "")
        if type_name and type_name not in CONTAINER_TYPES:
            if "Description" not in node:
                errors.append(
                    f"[{path}] '{type_name}' missing 'Description' (uppercase D)."
                )
            if "description" not in node:
                errors.append(
                    f"[{path}] '{type_name}' missing 'description' (lowercase d)."
                )
        for key, value in node.items():
            if isinstance(value, dict):
                check_node(value, f"{path}.{key}")

    raw = workflow_json.get("workflow_raw_data", {})
    check_node(raw)

    if errors:
        return {"passed": False, "errors": errors}
    return {"passed": True, "errors": []}


def run_all_validators(
    workflow_json: Annotated[dict, "Workflow JSON dict"],
) -> dict:
    """Runs all four validators in sequence. Returns combined result."""
    workflow_json = _ensure_dict(workflow_json)
    all_errors = []
    all_verify_notes = []
    results = {}

    r1 = validate_xname_uniqueness(workflow_json)
    results["xname_uniqueness"] = r1
    all_errors.extend(r1.get("errors", []))

    r2 = validate_activity_schema(workflow_json)
    results["activity_schema"] = r2
    all_errors.extend(r2.get("errors", []))
    all_verify_notes.extend(r2.get("verify_notes", []))

    r3 = validate_control_flow_rules(workflow_json)
    results["control_flow_rules"] = r3
    all_errors.extend(r3.get("errors", []))
    all_verify_notes.extend(r3.get("verify_notes", []))

    r4 = validate_required_fields(workflow_json)
    results["required_fields"] = r4
    all_errors.extend(r4.get("errors", []))

    return {
        "status": "valid" if not all_errors else "invalid",
        "errors": all_errors,
        "verify_notes": all_verify_notes,
        "detail": results,
    }