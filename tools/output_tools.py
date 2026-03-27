"""
tools/output_tools.py

Replaces ComposerAgent. Deterministic Python output stage.

Live path:  format_json_output() → write_output_file()
Manual path: workflow JSON → convert_to_xml.py (separate CLI script)

ARCHITECTURE NOTE:
  _enrich_workflow() has been removed from format_json_output(). Template
  enrichment now happens in Stage 4b (run_enrichment in pipeline_stages.py),
  upstream of annotation and validation. By the time workflow_json reaches
  this stage it is already fully enriched — re-enriching here would overwrite
  WirerAgent's semantic field values with template placeholders.

  _enrich_activity() and _enrich_workflow() are kept below as reference
  implementations but are no longer called by the live pipeline.

Fix 9 note: verify_notes in the validation_result dict is correctly
populated by run_validation() in pipeline_stages.py (derived from
placeholder_summary items with kind == "verify"). This file reads it
directly.

Output directory: json_files/ (relative to project root, created if missing)
"""

import copy
import json
import pathlib

from tools.build_tools import generate_pnumber, generate_workflow_name, load_activity_template


# ---------------------------------------------------------------------------
# Metadata enrichment (reference only — no longer called by live pipeline)
# ---------------------------------------------------------------------------

# Fields that WirerAgent sets and owns — never overwrite with template values.
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
    Reference implementation — kept for manual use and convert_to_xml.py.
    Not called by the live pipeline (enrichment now happens in Stage 4b).
    """
    ct = activity.get("CustomTypeName", "")
    if not ct:
        return activity

    template = load_activity_template(ct) if ct else {}
    merged = copy.deepcopy(template) if template else {}

    for key, val in activity.items():
        if key in _PRESERVE_FIELDS:
            merged[key] = val
        elif isinstance(val, dict) and val.get("CustomTypeName"):
            merged[key] = _enrich_activity(val)
        elif isinstance(val, dict):
            merged[key] = val

    merged["xName"] = activity.get("xName", merged.get("xName", ""))

    for key, val in activity.items():
        if key not in merged:
            if isinstance(val, dict) and val.get("CustomTypeName"):
                merged[key] = _enrich_activity(val)
            else:
                merged[key] = val

    return merged


def _enrich_workflow(workflow_raw_data: dict) -> dict:
    """
    Reference implementation — kept for manual use and convert_to_xml.py.
    Not called by the live pipeline (enrichment now happens in Stage 4b).
    """
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
# Stage 7 — Output
# ---------------------------------------------------------------------------

def format_json_output(
    validation_result: dict,
    base_name: str = "Workflow",
) -> dict:
    """
    Adds metadata to a validated workflow JSON and returns the output dict.
    Raises PipelineValidationError if validation_result.status != 'valid'.

    NOTE: workflow_raw_data is used as-is. Enrichment happened upstream in
    Stage 4b (run_enrichment). Do not re-enrich here.
    """
    if validation_result.get("status") != "valid":
        raise PipelineValidationError(validation_result.get("errors", []))

    workflow_json = validation_result["workflow_json"]
    raw_data      = workflow_json.get("workflow_raw_data", workflow_json)

    return {
        "name":                generate_workflow_name(base_name),
        "pnumber":             generate_pnumber(),
        "workflow_type":       "Regular",
        "created_by":          "adk-pipeline-v2",
        "workflow_raw_data":   raw_data,
        "placeholder_summary": validation_result.get("placeholder_summary", []),
        "pipeline_notes":      validation_result.get("verify_notes", []),
        "errors":              [],
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