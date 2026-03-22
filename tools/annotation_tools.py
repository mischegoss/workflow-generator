import copy
import json as _json
import os
import re
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
    "SmtpServer":       "PLACEHOLDER_SMTP_SERVER",
    "SmtpPort":         "PLACEHOLDER_SMTP_PORT",
    "Username":         "PLACEHOLDER_SMTP_USER",
    "Password":         "PLACEHOLDER_SMTP_PASS",
    "From":             "PLACEHOLDER_SMTP_FROM",
    "TargetModuleName": "PLACEHOLDER_EMAIL_MODULE",
    "TargetModuleID":   "PLACEHOLDER_EMAIL_MODULE_ID",
}

# Credential field keys derived deterministically from activities_controls.json
# inputType: password or hiddenPassword across all activities.
# These are always PLACEHOLDER_ regardless of activity type.
CREDENTIAL_FIELD_KEYS = {
    "ACPassword", "AdminPassword", "ArchivePassword", "AuthPassword",
    "CertificatePassword", "DomainServerPassword", "EncPassword",
    "FilePassword", "LoginPassword", "NewPassword", "Password",
    "ProxyPassword", "SRVPassword", "SrcPassword", "SwitchPassword",
    "URLPassword", "adminConfirmPassword", "api_key", "confirmDomainServerPassword",
    "openai_api_key", "password", "token",
    # hiddenPassword type
    "ValueToDisplaya", "ValueToDisplayb", "ValueToDisplayc",
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

# Cached index: activityName → list of mandatory field dicts from activities_detailed.json
_detailed_index: dict | None = None


def _load_detailed_index() -> dict:
    """
    Loads activities_detailed.json and indexes by activity name.
    Used to identify date-sensitive and user-value textbox fields for VERIFY notes.
    """
    global _detailed_index
    if _detailed_index is not None:
        return _detailed_index
    data_dir = os.getenv("DATA_DIR", "/app/data")
    path = os.path.join(data_dir, "activities_detailed.json")
    try:
        with open(path, encoding="utf-8") as f:
            detailed = _json.load(f)
        _detailed_index = {
            entry["activityName"]: {
                "mandatory": entry.get("mandatoryInputValues", []),
                "optional":  entry.get("recommendedOptionalValues", []),
            }
            for entry in detailed
        }
    except Exception:
        _detailed_index = {}
    return _detailed_index


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


def _is_global_variable_ref(value: str) -> bool:
    stripped = value.strip("%")
    return stripped in PLATFORM_GLOBAL_VARIABLES


def _is_variable_reference(value: str) -> bool:
    """Returns True if value is a %variable% reference of any kind."""
    return bool(re.match(r'^%[^%]+%$', value.strip()))


def _looks_like_hardcoded_literal(value: str) -> bool:
    """
    Returns True if a string value looks like a hardcoded literal rather than
    a variable reference or structural default.
    Catches: dates, IP addresses, hostnames, email addresses, plain words
    that are not variable references.
    """
    if not value or not isinstance(value, str):
        return False
    if _is_variable_reference(value):
        return False
    if value in ("{x:Null}", "", "True", "False", "0", "1"):
        return False
    if value.startswith("PLACEHOLDER_"):
        return False
    # Dates: MM/DD/YYYY, YYYY-MM-DD, etc.
    if re.match(r'\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}', value):
        return True
    # IP addresses
    if re.match(r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$', value):
        return True
    # Email addresses
    if re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', value):
        return True
    # Hostnames / URLs with dots
    if re.match(r'^(https?://|www\.)', value):
        return True
    return False


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
    Replaces credential fields with PLACEHOLDER_ strings.

    Two passes:
    1. SMTP_FIELD_MAP — module-level SMTP connection fields (existing behaviour).
    2. CREDENTIAL_FIELD_KEYS — all password/token/api_key fields derived from
       activities_controls.json inputType=password|hiddenPassword. This is now
       deterministic — no heuristic guessing required.

    Leaves platform global variable references (%varName%) intact.
    """
    workflow_json = _ensure_dict(workflow_json)
    result = copy.deepcopy(workflow_json)

    def process_node(node: dict) -> dict:
        for key, value in list(node.items()):
            if isinstance(value, dict):
                node[key] = process_node(value)
                continue
            if not isinstance(value, str):
                continue
            # Skip if already a placeholder or global variable ref
            if value.startswith("PLACEHOLDER_"):
                continue
            if value.startswith("%") and _is_global_variable_ref(value):
                continue

            # Pass 1: SMTP module-level fields
            if key in SMTP_FIELD_MAP:
                node[key] = SMTP_FIELD_MAP[key]
                continue

            # Pass 2: credential fields from controls index
            if key in CREDENTIAL_FIELD_KEYS:
                node[key] = f"PLACEHOLDER_{key.upper()}"
                continue

        return node

    raw = result.get("workflow_raw_data", {})
    result["workflow_raw_data"] = process_node(raw)
    return result


def add_verify_notes(
    workflow_json: Annotated[dict, "Workflow JSON dict"],
) -> dict:
    """
    Adds VERIFY notes to activities requiring post-import manual configuration.

    Three categories:
    1. Manual config activities (SNGetRecord XMLTableResult).
    2. Activities with unconfirmed CLR namespaces.
    3. Hardcoded literal values in fields that should be variable references —
       detected using activities_detailed.json mandatory field metadata.
       Also flags hardcoded data inside TableAsString (CreateMemoryTable sample rows).
    """
    workflow_json = _ensure_dict(workflow_json)
    result = copy.deepcopy(workflow_json)
    detailed_index = _load_detailed_index()

    def _append_note(node: dict, msg: str):
        existing = node.get("notes", "")
        if msg not in existing:
            node["notes"] = (existing + "  " + msg).strip()

    def process_node(node: dict) -> dict:
        custom_type = node.get("CustomTypeName", "")

        # --- Category 1: Manual config activities ---
        if custom_type in MANUAL_CONFIG_ACTIVITIES:
            _append_note(node, MANUAL_CONFIG_ACTIVITIES[custom_type])

        # --- Category 2: Unconfirmed CLR namespaces ---
        if custom_type in UNCONFIRMED_NAMESPACE_ACTIVITIES:
            ns_msg = (
                f"VERIFY: CLR namespace for {custom_type} not confirmed from platform export. "
                "If import fails, export a workflow using this activity and add its namespace "
                "to NAMESPACE_REGISTRY in xml_composer.py."
            )
            _append_note(node, ns_msg)

        # --- Category 3a: Hardcoded literals in mandatory textbox fields ---
        if custom_type in detailed_index:
            for field in detailed_index[custom_type]["mandatory"]:
                field_key = field.get("fieldKey", "")
                input_type = field.get("inputType", "")
                if input_type == "textbox" and field_key in node:
                    val = node[field_key]
                    if isinstance(val, str) and _looks_like_hardcoded_literal(val):
                        _append_note(
                            node,
                            f"VERIFY: '{field_key}' contains hardcoded value '{val}'. "
                            f"Replace with a %variable% reference or correct runtime value before deployment."
                        )

        # --- Category 3b: Hardcoded sample data in TableAsString ---
        # The XML schema blob is structural; the <resultSet> rows are sample data.
        # Flag any non-empty resultSet data as requiring user replacement.
        if custom_type == "CreateMemoryTable" and "TableAsString" in node:
            table_str = node.get("TableAsString", "")
            if isinstance(table_str, str) and "<resultSet>" in table_str:
                _append_note(
                    node,
                    "VERIFY: TableAsString contains sample data rows. "
                    "Replace the <resultSet> entries with your actual data or remove them "
                    "and populate the table at runtime using AddMemoryTableRow."
                )

        # --- Always: clear DateLic ---
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
