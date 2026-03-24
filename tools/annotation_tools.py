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
    # hiddenPassword type
    "ValueToDisplaya", "ValueToDisplayb", "ValueToDisplayc",
}

MANUAL_CONFIG_ACTIVITIES = {
    "SNGetRecord": (
        "VERIFY: XMLTableResult requires manual UI configuration after import. "
        "Open SNGetRecord in the platform UI and configure the filter and table fields manually."
    ),
}

# Remaining unconfirmed CLR namespaces (FormatDate, ReadCSV, HTTPRequest
# were not present in namespace_registry.json).
UNCONFIRMED_NAMESPACE_ACTIVITIES = {
    "FormatDate",
    "ReadCSV",
    "HTTPRequest",
}

# Fields that reference table structure by name — a plain word value here
# is almost certainly a hardcoded table/column name that needs verification.
# Issue 4 fix: extend Category 3 to catch these structural fields.
TABLE_STRUCTURE_FIELDS = {
    "ColumnNumber",    # GetCellValue / SetCellValue — column name
    "ResultSetName",   # GetCellValue / GetRows — table variable name literal
    "TableName",       # GetRowsCount / CreateMemoryTable — table name literal
}

# Activities that create or produce a new table in the workflow.
# Used for Issue 6 (prerequisite check): if a table variable is referenced
# but none of these activity types exist in the workflow, it's external.
TABLE_PRODUCING_ACTIVITIES = {
    "CreateMemoryTable",
    "ReadXLS",
    "ReadCSV",
    "TSQLQuery",
    "TSQLStatement",
    "GetRows",
    "ResultSetFilter",
    "SNGetRecord",
    "ADListOU",
    "ADListGroup",
    "GetOpenIncidents",
    "ProcessList",
    "ServiceList",
    "FolderList",
    "GetInstalledSoftware",
    "GetWindowEventLogs",
}

# Cached index: activityName → { "mandatory": [...], "optional": [...] }
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
        text = value.strip()
        # Strip markdown code fences — Gemini Flash frequently wraps JSON in ```json ... ```
        if text.startswith("```"):
            text = text.split("\n", 1)[-1]
            if text.endswith("```"):
                text = text[: text.rfind("```")]
            text = text.strip()
        try:
            result = _json.loads(text)
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
    """
    Returns True if a string value looks like a hardcoded literal rather
    than a variable reference or structural default.
    """
    if not value or not isinstance(value, str):
        return False
    if _is_variable_reference(value):
        return False
    if value in ("{x:Null}", "", "True", "False", "0", "1"):
        return False
    if value.startswith("PLACEHOLDER_"):
        return False
    # Dates
    if re.match(r'\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}', value):
        return True
    # IP addresses
    if re.match(r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$', value):
        return True
    # Email addresses
    if re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', value):
        return True
    # URLs / hostnames
    if re.match(r'^(https?://|www\.)', value):
        return True
    return False


def _is_plain_word(value: str) -> bool:
    """
    Returns True if value is a plain alphanumeric word (no spaces, no %%, not a
    structural platform value). Used for Issue 4: catching hardcoded column/table names.
    """
    if not value or not isinstance(value, str):
        return False
    if _is_variable_reference(value):
        return False
    if value in ("{x:Null}", "", "True", "False", "0", "1", "Name", "Index"):
        return False
    if value.startswith("PLACEHOLDER_"):
        return False
    # Plain word: only letters and digits, no separators
    return bool(re.match(r'^[a-zA-Z][a-zA-Z0-9_]*$', value))


# ---------------------------------------------------------------------------
# Annotation functions
# ---------------------------------------------------------------------------

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
    """
    Replaces credential fields with PLACEHOLDER_ strings.
    Two passes: SMTP module fields, then all password/token/api_key fields.
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
    """
    Adds VERIFY notes to activities requiring post-import attention.

    Category 1: Manual config activities (SNGetRecord XMLTableResult).
    Category 2: Unconfirmed CLR namespaces.
    Category 3a: Hardcoded literal values in mandatory textbox fields.
    Category 3b: Plain word values in structural table/column reference fields
                 (ColumnNumber, ResultSetName, TableName). These are hardcoded
                 names that must match the actual table/column at runtime.
                 (Fix for Issue 4.)
    Category 4: Empty mandatory textbox/textarea fields that are not credentials.
                Empty required fields will cause the activity to fail silently
                at runtime with no platform error message.
                (Fix for Issue 2.)
    Category 5: Table variable prerequisite check — table variables referenced
                in the workflow that are not produced by any activity within it.
                These are external dependencies the user must populate before
                running the workflow.
                (Fix for Issue 6.)
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

        # ── Category 1: Manual config activities ────────────────────────────
        if custom_type in MANUAL_CONFIG_ACTIVITIES:
            _append_note(node, MANUAL_CONFIG_ACTIVITIES[custom_type])

        # ── Category 2: Unconfirmed CLR namespaces ──────────────────────────
        if custom_type in UNCONFIRMED_NAMESPACE_ACTIVITIES:
            _append_note(
                node,
                f"VERIFY: CLR namespace for {custom_type} not confirmed from platform "
                "export. If import fails, export a workflow using this activity and add "
                "its namespace to NAMESPACE_REGISTRY in xml_composer.py.",
            )

        if custom_type in detailed_index:
            for field in detailed_index[custom_type]["mandatory"]:
                field_key   = field.get("fieldKey", "")
                input_type  = field.get("inputType", "")
                field_name  = field.get("fieldName", field_key)

                if field_key not in node:
                    continue

                val = node[field_key]
                if not isinstance(val, str):
                    continue

                is_credential = (
                    input_type in ("password", "hiddenPassword")
                    or field_key in CREDENTIAL_FIELD_KEYS
                )

                if is_credential:
                    continue  # handled by annotate_placeholders

                # ── Category 3a: Hardcoded literal in mandatory textbox ──────
                if input_type in ("textbox", "textarea"):
                    if _looks_like_hardcoded_literal(val):
                        _append_note(
                            node,
                            f"VERIFY: '{field_key}' contains hardcoded value '{val}'. "
                            f"Replace with a %variable% reference or correct runtime "
                            f"value before deployment.",
                        )

                # ── Category 4: Empty mandatory non-credential field ─────────
                # Fix for Issue 2: empty required fields fail silently at runtime.
                if input_type in ("textbox", "textarea") and val == "":
                    _append_note(
                        node,
                        f"VERIFY: '{field_key}' ({field_name}) is a required field but "
                        f"is empty. This activity will fail at runtime without a value. "
                        f"Set this field after import.",
                    )

        # ── Category 3b: Plain word in structural table/column fields ────────
        # Fix for Issue 4: ColumnNumber="server" or TableName="certData" are
        # hardcoded names that silently return empty if the name doesn't match.
        for field_key in TABLE_STRUCTURE_FIELDS:
            if field_key in node:
                val = node[field_key]
                if isinstance(val, str) and (val.isdigit() or _is_plain_word(val)):
                    _append_note(
                        node,
                        f"VERIFY: '{field_key}' is set to the literal value '{val}'. "
                        f"Confirm this matches the exact table or column name used at "
                        f"runtime.",
                    )

        # ── Category 3b (CreateMemoryTable): TableAsString sample rows ───────
        if custom_type == "CreateMemoryTable" and "TableAsString" in node:
            table_str = node.get("TableAsString", "")
            if isinstance(table_str, str) and "<resultSet>" in table_str:
                _append_note(
                    node,
                    "VERIFY: TableAsString contains sample data rows. Replace the "
                    "<resultSet> entries with your actual data or remove them and "
                    "populate the table at runtime using AddMemoryTableRow.",
                )

        # ── Always: clear DateLic ────────────────────────────────────────────
        if "DateLic" in node:
            node["DateLic"] = ""

        for key, value in node.items():
            if isinstance(value, dict):
                node[key] = process_node(value)

        return node

    raw = result.get("workflow_raw_data", {})
    result["workflow_raw_data"] = process_node(raw)

    # ── Category 5: Table variable prerequisite check ────────────────────────
    # Fix for Issue 6: warn when a table variable is referenced in the workflow
    # but no activity within the workflow creates or produces it.
    _check_table_prerequisites(result)

    return result


def _check_table_prerequisites(workflow_json: dict) -> None:
    """
    Category 5: Detects table variables referenced in the workflow that are not
    produced by any activity within the workflow.

    These are external dependencies — tables that must exist before execution
    (e.g. populated by a global variable, a prior workflow, or a trigger).
    Adds a VERIFY note to the first activity that references the variable.

    Strategy:
    1. Collect all xNames in the workflow.
    2. Collect CustomTypeNames of all activities.
    3. Find all %variable% references used in table-context fields (ResultSet,
       ResultSetName, TableName).
    4. For each such variable: if its xName is not in the workflow AND no
       table-producing activity exists in the workflow, flag it.
    """
    raw = workflow_json.get("workflow_raw_data", {})
    if not raw:
        return

    # Collect all xNames in the workflow (flat walk)
    all_xnames: set = set()
    all_custom_types: set = set()

    def _collect(node: dict):
        xn = node.get("xName", "")
        if xn:
            all_xnames.add(xn)
        ct = node.get("CustomTypeName", "")
        if ct:
            all_custom_types.add(ct)
        for v in node.values():
            if isinstance(v, dict):
                _collect(v)

    for activity in raw.values():
        if isinstance(activity, dict):
            _collect(activity)

    has_table_producer = bool(all_custom_types & TABLE_PRODUCING_ACTIVITIES)

    # Fields whose values are table-context variable references
    TABLE_REF_FIELDS = {"ResultSet", "ResultSetName", "TableName", "Value"}

    # Find all table variable references and where they first appear
    referenced_tables: dict = {}  # varname → first activity node that references it

    def _find_table_refs(node: dict):
        xn = node.get("xName", "")
        for field_key, val in node.items():
            if field_key in TABLE_REF_FIELDS and isinstance(val, str):
                m = re.match(r'^%([^%]+)%$', val.strip())
                if m:
                    varname = m.group(1)
                    if varname not in referenced_tables:
                        referenced_tables[varname] = node
        for v in node.values():
            if isinstance(v, dict):
                _find_table_refs(v)

    for activity in raw.values():
        if isinstance(activity, dict):
            _find_table_refs(activity)

    # For each referenced table variable: if its xName is not in the workflow
    # and no table-producing activity exists, it's an external dependency.
    for varname, first_node in referenced_tables.items():
        if varname in all_xnames:
            continue  # produced by an activity in this workflow
        if has_table_producer:
            continue  # a table-creating activity exists; assume it covers this
        # External dependency — add VERIFY note to the first referencing activity
        msg = (
            f"VERIFY: '%{varname}%' is referenced as a table but is not created by any "
            f"activity in this workflow. Ensure '{varname}' is populated (e.g. via a "
            f"global variable, prior workflow, or trigger) before running this workflow."
        )
        existing = first_node.get("notes", "")
        if msg not in existing:
            first_node["notes"] = (existing + "  " + msg).strip()


def collect_placeholder_summary(
    workflow_json: Annotated[dict, "Annotated workflow JSON"],
) -> list[dict]:
    """
    Walks the workflow JSON and collects all PLACEHOLDER_ values and VERIFY notes.
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
                        "type":     custom_type,
                        "field":    key,
                        "placeholder": value,
                        "kind":     "placeholder",
                    })
            if key == "notes" and isinstance(value, str) and "VERIFY" in value:
                for note in value.split("VERIFY:"):
                    note = note.strip()
                    if note:
                        items.append({
                            "activity": xname,
                            "type":     custom_type,
                            "field":    "notes",
                            "message":  "VERIFY: " + note,
                            "kind":     "verify",
                        })
            if isinstance(value, dict):
                walk(value, xname)

    raw = workflow_json.get("workflow_raw_data", {})
    for xname, activity in raw.items():
        if isinstance(activity, dict):
            walk(activity, xname)

    return items
