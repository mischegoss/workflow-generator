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

CREDENTIAL_FIELD_KEYS = {
    "ACPassword", "AdminPassword", "ArchivePassword", "AuthPassword",
    "CertificatePassword", "DomainServerPassword", "EncPassword",
    "FilePassword", "LoginPassword", "NewPassword", "Password",
    "ProxyPassword", "SRVPassword", "SrcPassword", "SwitchPassword",
    "URLPassword", "adminConfirmPassword", "api_key", "confirmDomainServerPassword",
    "openai_api_key", "password", "token",
    "ValueToDisplaya", "ValueToDisplayb", "ValueToDisplayc",
}

MANUAL_CONFIG_ACTIVITIES = {
    "SNGetRecord": (
        "VERIFY: XMLTableResult requires manual UI configuration after import. "
        "Open SNGetRecord in the platform UI and configure the filter and table fields manually."
    ),
}

# Activities whose CLR namespace is NOT confirmed from a real platform export.
# These receive a VERIFY note on every run.
# Removed from original list: GetDate, CreateMemoryTable, PowerShellScript,
# TSQLStatement, TSQLQuery, ReadXLS, WriteXLS, PowerShell, RunWorkflow
# — all confirmed in namespace_registry.json.
# Remaining: FormatDate, ReadCSV, HTTPRequest — not present in namespace_registry.json.
UNCONFIRMED_NAMESPACE_ACTIVITIES = {
    "FormatDate",
    "ReadCSV",
    "HTTPRequest",
}

_detailed_index: dict | None = None


def _load_detailed_index() -> dict:
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
    return bool(re.match(r'^%[^%]+%$', value.strip()))


def _looks_like_hardcoded_literal(value: str) -> bool:
    if not value or not isinstance(value, str):
        return False
    if _is_variable_reference(value):
        return False
    if value in ("{x:Null}", "", "True", "False", "0", "1"):
        return False
    if value.startswith("PLACEHOLDER_"):
        return False
    if re.match(r'\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}', value):
        return True
    if re.match(r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$', value):
        return True
    if re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', value):
        return True
    if re.match(r'^(https?://|www\.)', value):
        return True
    return False


def inject_unavailable_stubs(
    workflow_json: Annotated[dict, "Workflow JSON with UNAVAILABLE step markers"],
    activity_manifest: Annotated[dict, "Manifest from retriever with UNAVAILABLE entries"],
) -> dict:
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
    workflow_json = _ensure_dict(workflow_json)
    result = copy.deepcopy(workflow_json)

    def process_node(node: dict) -> dict:
        for key, value in list(node.items()):
            if isinstance(value, dict):
                node[key] = process_node(value)
                continue
            if not isinstance(value, str):
                continue
            if value.startswith("PLACEHOLDER_"):
                continue
            if value.startswith("%") and _is_global_variable_ref(value):
                continue
            if key in SMTP_FIELD_MAP:
                node[key] = SMTP_FIELD_MAP[key]
                continue
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
    workflow_json = _ensure_dict(workflow_json)
    result = copy.deepcopy(workflow_json)
    detailed_index = _load_detailed_index()

    def _append_note(node: dict, msg: str):
        existing = node.get("notes", "")
        if msg not in existing:
            node["notes"] = (existing + "  " + msg).strip()

    def process_node(node: dict) -> dict:
        custom_type = node.get("CustomTypeName", "")

        if custom_type in MANUAL_CONFIG_ACTIVITIES:
            _append_note(node, MANUAL_CONFIG_ACTIVITIES[custom_type])

        if custom_type in UNCONFIRMED_NAMESPACE_ACTIVITIES:
            ns_msg = (
                f"VERIFY: CLR namespace for {custom_type} not confirmed from platform export. "
                "If import fails, export a workflow using this activity and add its namespace "
                "to NAMESPACE_REGISTRY in xml_composer.py."
            )
            _append_note(node, ns_msg)

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

        if custom_type == "CreateMemoryTable" and "TableAsString" in node:
            table_str = node.get("TableAsString", "")
            if isinstance(table_str, str) and "<resultSet>" in table_str:
                _append_note(
                    node,
                    "VERIFY: TableAsString contains sample data rows. "
                    "Replace the <resultSet> entries with your actual data or remove them "
                    "and populate the table at runtime using AddMemoryTableRow."
                )

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
