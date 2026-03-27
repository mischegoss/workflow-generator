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

UNCONFIRMED_NAMESPACE_ACTIVITIES = set()

TABLE_STRUCTURE_FIELDS = {
    "ColumnNumber",
    "ResultSetName",
    "TableName",
}

# Fields that MUST receive a DataTable variable (not a scalar)
TABLE_INPUT_FIELDS = {"ResultSet", "ResultSetName"}

# Fields that MUST receive a scalar (not a DataTable)
SCALAR_INPUT_FIELDS = {
    "RowNumber", "Counter", "HostName", "ValueToDisplay",
    "VariableValue", "TheValue", "TheValue2",
}


# ---------------------------------------------------------------------------
# Table producers — loaded from corpus-mined table_producers.json
# ---------------------------------------------------------------------------

_table_producers_cache: set | None = None


def _load_table_producers() -> set:
    global _table_producers_cache
    if _table_producers_cache is not None:
        return _table_producers_cache
    data_dir = os.getenv("DATA_DIR", "/app/data")
    path = os.path.join(data_dir, "table_producers.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = _json.load(f)
        _table_producers_cache = set(data.get("producers", []))
        print(f"[annotation_tools] Loaded {len(_table_producers_cache)} table producers")
    except FileNotFoundError:
        print(f"[annotation_tools] Warning: table_producers.json not found. Using fallback.")
        _table_producers_cache = {
            "CreateMemoryTable", "ReadXLS", "ReadCSV",
            "TSQLQuery", "TSQLStatement", "GetRows",
            "ResultSetFilter", "SNGetRecord", "ADListOU",
            "ADListGroup", "GetOpenIncidents", "ProcessList",
            "ServiceList", "FolderList", "GetInstalledSoftware",
            "GetWindowEventLogs",
        }
    return _table_producers_cache


def _get_table_producing_activities() -> set:
    return _load_table_producers()


# ---------------------------------------------------------------------------
# Output registry — loaded from activity_output_registry.json (Category 6)
# ---------------------------------------------------------------------------

_output_registry_cache: dict | None = None


def _load_output_registry() -> dict:
    """
    Load activity_output_registry.json once and cache as a dict keyed by
    activityName → outputType.

    outputType values: "DataTable", "Scalar", "Boolean", "Status", "None"
    """
    global _output_registry_cache
    if _output_registry_cache is not None:
        return _output_registry_cache
    data_dir = os.getenv("DATA_DIR", "/app/data")
    path = os.path.join(data_dir, "activity_output_registry.json")
    try:
        with open(path, encoding="utf-8") as f:
            entries = _json.load(f)
        _output_registry_cache = {
            e["activityName"]: e["outputType"]
            for e in entries
            if "activityName" in e and "outputType" in e
        }
        print(f"[annotation_tools] Loaded {len(_output_registry_cache)} output type entries")
    except FileNotFoundError:
        print(f"[annotation_tools] Warning: activity_output_registry.json not found at {path}. "
              f"Category 6 checks disabled.")
        _output_registry_cache = {}
    return _output_registry_cache


# ---------------------------------------------------------------------------
# Other cached indices
# ---------------------------------------------------------------------------

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


def _extract_var_name(value: str) -> str | None:
    """Extract the variable name from a %varName% reference, or None."""
    m = re.match(r'^%([^%]+)%$', value.strip())
    return m.group(1) if m else None


def _looks_like_hardcoded_literal(value: str) -> bool:
    if not value or not isinstance(value, str):
        return False
    if _is_variable_reference(value):
        return False
    if value.startswith("PLACEHOLDER_"):
        return False
    if value in ("{x:Null}", "", "True", "False", "0", "1", "-1", "-2",
                 "None", "null", "LocalHost", "localhost"):
        return False
    if re.match(r'^[\d\-]+$', value):
        return False
    if re.match(r'^\d{2}:\d{2}:\d{2}$', value):
        return False
    if re.match(r'^[^@\s]+@[^@\s]+$', value):
        return False
    if re.match(r'^(https?://|www\.)', value):
        return True
    return len(value) > 2


def _is_plain_word(value: str) -> bool:
    if not value or not isinstance(value, str):
        return False
    if _is_variable_reference(value):
        return False
    if value in ("{x:Null}", "", "True", "False", "0", "1", "Name", "Index"):
        return False
    if value.startswith("PLACEHOLDER_"):
        return False
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
    """
    workflow_json = _ensure_dict(workflow_json)
    result = copy.deepcopy(workflow_json)

    def process_node(node: dict) -> dict:
        # Skip credential annotation on container/structural activities.
        # These types (SequenceActivity, IfElseActivity, etc.) are structural
        # scaffolding — they should never have SMTP or credential fields applied.
        # Applying PLACEHOLDER_ values to them causes platform upload failures.
        custom_type = node.get("CustomTypeName", "")
        is_container = custom_type in {
            "WhileActivity", "SequenceActivity", "IfElseActivity",
            "IfElseBranchActivity", "ParallelActivity", "UserGroup",
            "ForEachActivity", "ExitWhile", "ReturnValue", "IfElseCondition",
        }

        for key, value in list(node.items()):
            if isinstance(value, dict):
                node[key] = process_node(value)
                continue
            if is_container:
                continue  # never annotate credentials on container types
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
    Category 3b: Plain word values in structural table/column reference fields.
    Category 4: Empty mandatory textbox/textarea fields that are not credentials.
    Category 5: Table variable prerequisite check.
    Category 6: Output type mismatch — a scalar-producing activity's %xName%
                is referenced in a table-input field, or a DataTable-producing
                activity's %xName% is referenced in a scalar-input field.
    """
    workflow_json = _ensure_dict(workflow_json)
    result = copy.deepcopy(workflow_json)
    detailed_index  = _load_detailed_index()
    output_registry = _load_output_registry()

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
                    continue

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
                if input_type in ("textbox", "textarea") and val == "":
                    _append_note(
                        node,
                        f"VERIFY: '{field_key}' ({field_name}) is a required field but "
                        f"is empty. This activity will fail at runtime without a value. "
                        f"Set this field after import.",
                    )

        # ── Category 3b: Plain word in structural table/column fields ────────
        for field_key in TABLE_STRUCTURE_FIELDS:
            if field_key in node:
                val = node[field_key]
                if isinstance(val, str) and (val.isdigit() or _is_plain_word(val)):
                    _append_note(
                        node,
                        f"VERIFY: '{field_key}' is set to the literal value '{val}'. "
                        f"Confirm this matches the exact table or column name used at runtime.",
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
    _check_table_prerequisites(result)

    # ── Category 6: Output type mismatch check ───────────────────────────────
    if output_registry:
        _check_output_type_mismatches(result, output_registry)

    return result


def _check_table_prerequisites(workflow_json: dict) -> None:
    """
    Category 5: Detects table variables referenced in the workflow that are not
    produced by any activity within the workflow.
    Uses the corpus-loaded TABLE_PRODUCING_ACTIVITIES set (from table_producers.json).
    """
    raw = workflow_json.get("workflow_raw_data", {})
    if not raw:
        return

    table_producing_activities = _get_table_producing_activities()
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

    has_table_producer = bool(all_custom_types & table_producing_activities)

    TABLE_REF_FIELDS = {"ResultSet", "ResultSetName", "TableName", "Value"}
    referenced_tables: dict = {}

    def _find_table_refs(node: dict):
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

    for varname, first_node in referenced_tables.items():
        if varname in all_xnames:
            continue
        if has_table_producer:
            continue
        msg = (
            f"VERIFY: '%{varname}%' is referenced as a table but is not created by any "
            f"activity in this workflow. Ensure '{varname}' is populated before running."
        )
        existing = first_node.get("notes", "")
        if msg not in existing:
            first_node["notes"] = (existing + "  " + msg).strip()


def _check_output_type_mismatches(workflow_json: dict, output_registry: dict) -> None:
    """
    Category 6: Detects output type mismatches using activity_output_registry.json.

    Two checks:
    A. A DataTable-producing activity's %xName% is referenced in a SCALAR_INPUT_FIELD.
       (e.g. TSQLQuery result wired directly into HostName — needs GetCellValue first)

    B. A non-DataTable activity's %xName% is referenced in a TABLE_INPUT_FIELD.
       (e.g. GetDate result wired into ResultSet — GetDate returns a string, not a table)

    Builds an xName → (CustomTypeName, outputType) map, then walks all activities
    looking for %xName% references in the relevant fields.
    """
    raw = workflow_json.get("workflow_raw_data", {})
    if not raw:
        return

    # Build xName → (type, outputType) index
    xname_to_output: dict = {}

    def _index(node: dict):
        xn = node.get("xName", "")
        ct = node.get("CustomTypeName", "")
        if xn and ct:
            output_type = output_registry.get(ct, "Scalar")  # default: Scalar
            xname_to_output[xn] = (ct, output_type)
        for v in node.values():
            if isinstance(v, dict):
                _index(v)

    for activity in raw.values():
        if isinstance(activity, dict):
            _index(activity)

    def _append_note(node: dict, msg: str):
        existing = node.get("notes", "")
        if msg not in existing:
            node["notes"] = (existing + "  " + msg).strip()

    def _check_node(node: dict):
        for field_key, val in node.items():
            if not isinstance(val, str):
                continue
            var_name = _extract_var_name(val)
            if not var_name or var_name not in xname_to_output:
                continue

            source_type, output_type = xname_to_output[var_name]

            # Check A: DataTable wired into scalar field
            if output_type == "DataTable" and field_key in SCALAR_INPUT_FIELDS:
                _append_note(
                    node,
                    f"VERIFY: '{field_key}' references '%{var_name}%' which is a "
                    f"DataTable output from {source_type}. This field expects a scalar "
                    f"value. Use GetCellValue to extract a single cell from the table first.",
                )

            # Check B: Non-DataTable wired into table field
            if output_type != "DataTable" and output_type != "None" and field_key in TABLE_INPUT_FIELDS:
                _append_note(
                    node,
                    f"VERIFY: '{field_key}' references '%{var_name}%' which is a "
                    f"{output_type} output from {source_type}. This field expects a "
                    f"DataTable variable. Ensure the upstream activity produces a table.",
                )

        for v in node.values():
            if isinstance(v, dict):
                _check_node(v)

    for activity in raw.values():
        if isinstance(activity, dict):
            _check_node(activity)


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
                        "activity":    xname,
                        "type":        custom_type,
                        "field":       key,
                        "placeholder": value,
                        "kind":        "placeholder",
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
