import json
import os
from typing import Annotated

_controls_index: dict | None = None

VALID_CONDITION_TYPES = {
    "", "Equals", "Contains", "Not Contains", "Not Equals",
    "Formula", ">", "<", ">=", "<="
}

CONTAINER_TYPES = {
    "WhileActivity", "SequenceActivity", "IfElseActivity", "IfElseBranchActivity",
    "ParallelActivity", "UserGroup", "ForEachActivity", "ExitWhile", "ReturnValue",
    "IfElseCondition",
}

EXCLUDED_REQUIRED_FIELDS = {
    "XMLTableResult",
}


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
    Checks required fields per activities_controls.json.
    Skips EXCLUDED_REQUIRED_FIELDS (e.g. XMLTableResult — configured manually after import).
    If activity not in index, adds a VERIFY note instead of failing.
    """
    workflow_json = _ensure_dict(workflow_json)
    controls_index = _load_controls_index()
    errors = []
    verify_notes = []

    def check_node(node: dict, path: str = ""):
        type_name = node.get("TypeName") or node.get("CustomTypeName", "")

        if type_name and type_name not in CONTAINER_TYPES:
            if type_name in controls_index:
                for control in controls_index[type_name]:
                    if (
                        control.get("required")
                        and control["fieldKey"] not in node
                        and control["fieldKey"] not in EXCLUDED_REQUIRED_FIELDS
                    ):
                        errors.append(
                            f"[{path}] '{type_name}' missing required field "
                            f"'{control['fieldKey']}' ({control['fieldName']})"
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
    SequenceActivity inside WhileActivity is allowed to have full attributes —
    confirmed across all 5 While examples in the corpus.
    """
    workflow_json = _ensure_dict(workflow_json)
    errors = []

    def check_node(node: dict, parent_type: str = "", path: str = ""):
        type_name = node.get("CustomTypeName", "")

        if type_name == "WhileActivity" and "Counter" in node:
            errors.append(
                f"[{path}] WhileActivity must not have Counter at its own level — "
                "Counter belongs on ExitWhile only."
            )

        if type_name == "ExitWhile" and "Counter" not in node:
            errors.append(
                f"[{path}] ExitWhile is missing required Counter attribute."
            )

        if "ForEachOutputVariableName" in node:
            val = node["ForEachOutputVariableName"]
            if val.lower().startswith("foreach"):
                errors.append(
                    f"[{path}] ForEachOutputVariableName '{val}' must not be prefixed "
                    "with 'forEach' — causes xName collision."
                )

        if type_name == "ReturnValue" and "ConditionType" in node:
            ct = node["ConditionType"]
            if ct not in VALID_CONDITION_TYPES:
                errors.append(
                    f"[{path}] ReturnValue has unconfirmed ConditionType '{ct}'. "
                    f"Confirmed values: {sorted(VALID_CONDITION_TYPES)}."
                )

        for key, value in node.items():
            if isinstance(value, dict):
                check_node(value, parent_type=type_name, path=f"{path}.{key}")

    raw = workflow_json.get("workflow_raw_data", {})
    check_node(raw)

    if errors:
        return {"passed": False, "errors": errors}
    return {"passed": True, "errors": []}


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

    r4 = validate_required_fields(workflow_json)
    results["required_fields"] = r4
    all_errors.extend(r4.get("errors", []))

    return {
        "status": "valid" if not all_errors else "invalid",
        "errors": all_errors,
        "verify_notes": all_verify_notes,
        "detail": results,
    }
    