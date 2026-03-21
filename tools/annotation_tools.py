import copy
import json as _json
from typing import Annotated

PLATFORM_GLOBAL_VARIABLES = {
    "incidentId", "incidentNumber", "incidentTitle", "incidentDescription",
    "incidentPriority", "incidentUrgency", "incidentImpact", "incidentStatus",
    "incidentAssignee", "incidentAssigneeGroup", "incidentCreatedBy",
    "incidentCreatedDate", "incidentUpdatedDate", "incidentResolvedDate",
    "incidentClosedDate", "incidentSLA", "incidentCategory", "incidentSubcategory",
    "incidentConfigItem", "incidentLocation", "incidentCompany", "incidentContact",
    "incidentEmail", "incidentPhone", "incidentExternalId", "incidentSource",
    "incidentEscalationLevel", "incidentWorkNotes",
}

SMTP_FIELD_MAP = {
    "SmtpServer":   "PLACEHOLDER_SMTP_SERVER",
    "SmtpPort":     "PLACEHOLDER_SMTP_PORT",
    "Username":     "PLACEHOLDER_SMTP_USER",
    "Password":     "PLACEHOLDER_SMTP_PASS",
    "From":         "PLACEHOLDER_SMTP_FROM",
}

MANUAL_CONFIG_ACTIVITIES = {
    "SNGetRecord": (
        "VERIFY: XMLTableResult requires manual UI configuration after import. "
        "Open SNGetRecord in the platform UI and configure the filter and table fields manually."
    ),
}

UNCONFIRMED_NAMESPACE_ACTIVITIES = {
    "GetDate", "FormatDate", "CreateMemoryTable", "PowerShellScript",
    "TSQLStatement", "TSQLQuery", "ReadCSV", "ReadXLS", "WriteXLS",
    "PowerShell", "HTTPRequest", "RunWorkflow",
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
            result = _json.loads(value)
            if isinstance(result, dict):
                return result
        except Exception:
            pass
    return {}


def inject_unavailable_stubs(
    workflow_json: Annotated[dict, "Workflow JSON with UNAVAILABLE step markers"],
    activity_manifest: Annotated[dict, "Manifest from retriever with UNAVAILABLE entries"],
) -> dict:
    """
    Replaces UNAVAILABLE steps with DisplayValue placeholder activities.
    """
    workflow_json = _ensure_dict(workflow_json)
    activity_manifest = _ensure_dict(activity_manifest)

    result = copy.deepcopy(workflow_json)
    raw = result.get("workflow_raw_data", {})
    manifest_steps = activity_manifest.get("steps", [])

    for step in manifest_steps:
        if step.get("status") == "UNAVAILABLE":
            step_id = step["step_id"]
            description = step.get("query", "Unknown step")
            xname = f"placeholder_{step_id}"
            raw[xname] = {
                "xName": xname,
                "activityLicenseType": "1",
                "id": "431",
                "name": "DisplayValue",
                "visible": "True",
                "disabled": "False",
                "isFavorite": "False",
                "isJsonValid": "True",
                "readPermission": True,
                "writePermission": True,
                "modulePermissions": None,
                "IsValid": "True",
                "Timeout": "00:01:00",
                "TimeInSeconds": "60",
                "RecoveryMethodSelection": None,
                "Path": None,
                "DisplayName": "DisplayValue",
                "Description": f"PLACEHOLDER — {description}",
                "description": f"PLACEHOLDER — {description}",
                "ValueToDisplay": f"PLACEHOLDER_{step_id.upper()}",
                "TargetModuleID": "",
                "TargetModuleName": "",
                "TypeName": "DisplayValue",
                "label": "DisplayValue",
                "notes": (
                    f"VERIFY: No matching activity found for: '{description}'. "
                    "Replace this placeholder with the correct activity before deployment."
                ),
                "CustomTypeName": "DisplayValue",
            }

    result["workflow_raw_data"] = raw
    return result


def annotate_placeholders(
    workflow_json: Annotated[dict, "Workflow JSON dict"],
) -> dict:
    """
    Replaces SMTP and module-level credential fields with PLACEHOLDER_ strings.
    Leaves platform global variable references (%varName%) intact.
    """
    workflow_json = _ensure_dict(workflow_json)
    result = copy.deepcopy(workflow_json)

    def _is_global_variable_ref(value: str) -> bool:
        stripped = value.strip("%")
        return stripped in PLATFORM_GLOBAL_VARIABLES

    def process_node(node: dict) -> dict:
        for key, value in node.items():
            if isinstance(value, dict):
                node[key] = process_node(value)
            elif key in SMTP_FIELD_MAP and isinstance(value, str):
                if (
                    not value.startswith("PLACEHOLDER_")
                    and not (value.startswith("%") and _is_global_variable_ref(value))
                ):
                    node[key] = SMTP_FIELD_MAP[key]
        return node

    raw = result.get("workflow_raw_data", {})
    result["workflow_raw_data"] = process_node(raw)
    return result


def add_verify_notes(
    workflow_json: Annotated[dict, "Workflow JSON dict"],
) -> dict:
    """
    Adds VERIFY notes to activities requiring post-import manual configuration.
    Also flags activities with unconfirmed CLR namespaces.
    """
    workflow_json = _ensure_dict(workflow_json)
    result = copy.deepcopy(workflow_json)

    def process_node(node: dict) -> dict:
        custom_type = node.get("CustomTypeName", "")

        if custom_type in MANUAL_CONFIG_ACTIVITIES:
            existing = node.get("notes", "")
            verify_msg = MANUAL_CONFIG_ACTIVITIES[custom_type]
            if verify_msg not in existing:
                node["notes"] = (existing + "  " + verify_msg).strip()

        if custom_type in UNCONFIRMED_NAMESPACE_ACTIVITIES:
            existing = node.get("notes", "")
            ns_msg = (
                f"VERIFY: CLR namespace for {custom_type} not confirmed from platform export. "
                "If import fails, export a workflow using this activity and add its namespace "
                "to NAMESPACE_REGISTRY in xml_composer.py."
            )
            if "CLR namespace" not in existing:
                node["notes"] = (existing + "  " + ns_msg).strip()

        if "DateLic" in node:
            node["DateLic"] = ""

        for key, value in node.items():
            if isinstance(value, dict):
                node[key] = process_node(value)

        return node

    raw = result.get("workflow_raw_data", {})
    result["workflow_raw_data"] = process_node(raw)
    return result


def collect_placeholder_summary(
    workflow_json: Annotated[dict, "Annotated workflow JSON"],
) -> list[dict]:
    """
    Walks the workflow JSON and collects all PLACEHOLDER_ values and VERIFY notes.
    Returns a list of items for the chat response to the user.
    """
    workflow_json = _ensure_dict(workflow_json)
    items = []

    def walk(node: dict, path: str = ""):
        custom_type = node.get("CustomTypeName", "")
        xname = node.get("xName", path)

        for key, value in node.items():
            if isinstance(value, str):
                if value.startswith("PLACEHOLDER_"):
                    items.append({
                        "activity": xname,
                        "type": custom_type,
                        "field": key,
                        "placeholder": value,
                        "kind": "placeholder",
                    })
            if key == "notes" and isinstance(value, str) and "VERIFY" in value:
                for note in value.split("VERIFY:"):
                    note = note.strip()
                    if note:
                        items.append({
                            "activity": xname,
                            "type": custom_type,
                            "field": "notes",
                            "message": "VERIFY: " + note,
                            "kind": "verify",
                        })
            if isinstance(value, dict):
                walk(value, xname)

    raw = workflow_json.get("workflow_raw_data", {})
    for xname, activity in raw.items():
        if isinstance(activity, dict):
            walk(activity, xname)

    return items