"""
tools/output_tools.py

Replaces ComposerAgent. Deterministic Python output stage.

Live path:  format_json_output() → write_output_file()
Manual path: workflow JSON → convert_to_xml.py (separate CLI script)

Fix 9 note: verify_notes in the validation_result dict is now correctly
populated by run_validation() in pipeline_stages.py (derived from
placeholder_summary items with kind == "verify"). This file reads it
directly — no change needed here beyond confirming the field is used.

Output directory: json_files/ (relative to project root, created if missing)
"""

import copy
import json
import pathlib

from tools.build_tools import generate_pnumber, generate_workflow_name, load_activity_template


# ---------------------------------------------------------------------------
# Metadata enrichment
# ---------------------------------------------------------------------------

# Fields that StructureBuilder sets and owns — never overwrite with template values.
# Everything else (id, activityLicenseType, visible, Timeout, etc.) comes from
# the template so the JSON is complete for direct consumption by Kevin's workflow.
_PRESERVE_FIELDS = {
    "xName", "CustomTypeName", "TypeName", "name",
    "IsValid", "Description", "description",
    "Counter", "exitWhileInsideWhile", "whileSequenceActivity", "isValid",
    "ConditionType", "Value", "Type", "UseBranchWhenTimeout", "UseStoredValue",
    "Formula", "IsValid",
    # Activity-specific wiring fields
    "ResultSet", "ResultSetName", "RowNumber", "ColumnNumber", "ColumnType",
    "HostName", "HostId",
    "ValueToDisplay",
    "VariableName", "VariableValue", "VariableScope", "IsSaved", "IsAppend",
    "FuturePast", "TimeInterval", "TimeToAdd", "DateFormat", "TimeZoneName",
    "FirstDate", "SecondDate", "ReturnFormat",
    "TableName", "Condition",
    "To", "Subject", "Body", "MessageType",
    "notes",
}


def _enrich_activity(activity: dict) -> dict:
    """
    Merge full template metadata into a generated activity dict.

    Strategy: load the template for this activity's CustomTypeName, then
    build a merged dict where:
      - Template fields provide the base (id, activityLicenseType, visible,
        Timeout, TimeInSeconds, RecoveryMethodSelection, Path, DisplayName,
        TargetModuleID, TargetModuleName, label, readPermission, writePermission,
        modulePermissions, isFavorite, isJsonValid, disabled, etc.)
      - _PRESERVE_FIELDS from the generated activity always win — these are
        the functional fields StructureBuilder set intentionally.
      - Nested child activities are recursed into.
    """
    ct = activity.get("CustomTypeName", "")
    if not ct:
        return activity

    template = load_activity_template(ct) if ct else {}

    # Start with template as base, then overlay preserved fields
    merged = copy.deepcopy(template) if template else {}

    # Overlay all _PRESERVE_FIELDS from the generated activity
    for key, val in activity.items():
        if key in _PRESERVE_FIELDS:
            merged[key] = val
        elif isinstance(val, dict) and val.get("CustomTypeName"):
            # Nested activity — recurse
            merged[key] = _enrich_activity(val)
        elif isinstance(val, dict):
            # Non-activity dict (e.g. null fields) — keep as-is
            merged[key] = val

    # Ensure xName is correct (template has placeholder value)
    merged["xName"] = activity.get("xName", merged.get("xName", ""))

    # Preserve any nested activity children not already in merged
    for key, val in activity.items():
        if key not in merged:
            if isinstance(val, dict) and val.get("CustomTypeName"):
                merged[key] = _enrich_activity(val)
            else:
                merged[key] = val

    return merged


def _enrich_workflow(workflow_raw_data: dict) -> dict:
    """Walk all top-level and nested activities and enrich each with template metadata."""
    enriched = {}
    for xname, activity in workflow_raw_data.items():
        if isinstance(activity, dict):
            enriched[xname] = _enrich_activity(activity)
        else:
            enriched[xname] = activity
    return enriched


# ---------------------------------------------------------------------------
# Custom exception
# ---------------------------------------------------------------------------

class PipelineValidationError(Exception):
    """Raised when format_json_output receives an invalid validation result."""
    def __init__(self, errors: list):
        self.errors = errors
        super().__init__(f"Validation failed with {len(errors)} error(s): {errors}")


# ---------------------------------------------------------------------------
# Stage 7 — Output  (replaces ComposerAgent)
# ---------------------------------------------------------------------------

def format_json_output(
    validation_result: dict,
    base_name: str = "Workflow",
) -> dict:
    """
    Adds metadata to a validated workflow JSON and returns the output dict.
    Raises PipelineValidationError if validation_result.status != 'valid'.

    Fix 7 (via build_tools): generate_workflow_name now uses base_name,
    so the output file is named meaningfully rather than WF_{timestamp}_{suffix}.

    Fix 9: pipeline_notes is populated from validation_result["verify_notes"],
    which is now correctly derived in run_validation() from the placeholder_summary.
    """
    if validation_result.get("status") != "valid":
        raise PipelineValidationError(validation_result.get("errors", []))

    workflow_json    = validation_result["workflow_json"]
    raw_data         = workflow_json.get("workflow_raw_data", workflow_json)
    enriched_raw     = _enrich_workflow(raw_data)

    return {
        "name":               generate_workflow_name(base_name),
        "pnumber":            generate_pnumber(),
        "workflow_type":      "Regular",
        "created_by":         "adk-pipeline-v2",
        "workflow_raw_data":  enriched_raw,
        "placeholder_summary": validation_result.get("placeholder_summary", []),
        "pipeline_notes":     validation_result.get("verify_notes", []),
        "errors":             [],
    }


def write_output_file(output: dict, output_dir: str) -> pathlib.Path:
    """
    Writes output dict to <output_dir>/<name>.json.
    Creates output_dir if it does not exist.
    Returns the written file path.
    """
    out_path = pathlib.Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    file_path = out_path / f"{output['name']}.json"
    file_path.write_text(json.dumps(output, indent=2), encoding="utf-8")
    return file_path


def run_output(validation_result: dict, base_name: str = "Workflow") -> dict:
    """
    Convenience wrapper for pipeline.py Stage 7.
    Formats and writes the workflow JSON to disk.

    Returns output dict extended with:
      'output_file': str path to written .json file
    """
    output    = format_json_output(validation_result, base_name)
    file_path = write_output_file(output, "json_files")

    return {
        **output,
        "output_file": str(file_path),
    }