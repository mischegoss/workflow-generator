"""
tools/output_tools.py

Replaces ComposerAgent. Deterministic Python output stage.

Live path:  format_json_output() → write_output_file()
Manual path: workflow JSON → convert_to_xml.py (separate CLI script)

Webhook/import integration with Resolve Actions is deferred.
The pipeline writes a .json file to OUTPUT_DIR. From there,
run convert_to_xml.py to produce the TotalExport XML for manual upload.

Output directory: json_files/ (relative to project root, created if missing)
"""

import json
import pathlib

from tools.build_tools import generate_pnumber, generate_workflow_name


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

    Input:  validation_result dict from run_validation()
    Output: complete output dict ready for write_output_file()
    """
    if validation_result.get("status") != "valid":
        raise PipelineValidationError(validation_result.get("errors", []))

    workflow_json = validation_result["workflow_json"]

    return {
        "name": generate_workflow_name(base_name),
        "pnumber": generate_pnumber(),
        "workflow_type": "Regular",
        "created_by": "adk-pipeline-v2",
        "workflow_raw_data": workflow_json.get("workflow_raw_data", workflow_json),
        "placeholder_summary": validation_result.get("placeholder_summary", []),
        "pipeline_notes": validation_result.get("verify_notes", []),
        "errors": [],
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
    output_dir = "json_files"
    output = format_json_output(validation_result, base_name)
    file_path = write_output_file(output, output_dir)

    return {
        **output,
        "output_file": str(file_path),
    }