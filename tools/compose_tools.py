import json as _json
import os
from typing import Annotated
from serializer.xml_composer import WorkflowXmlComposer
from tools.build_tools import generate_pnumber, generate_workflow_name


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


def _ensure_list(value) -> list:
    """Safely converts string JSON to list."""
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            result = _json.loads(value)
            if isinstance(result, list):
                return result
        except Exception:
            pass
    return []


def serialize_to_xml(
    workflow_json: Annotated[dict, "Validated workflow JSON from session state"],
    workflow_name: Annotated[str, "Human-readable workflow name"],
    pnumber: Annotated[str, "Unique Pnumber for this workflow"],
) -> str:
    """Converts workflow JSON to a TotalExport XML string importable by Resolve Actions."""
    workflow_json = _ensure_dict(workflow_json)
    composer = WorkflowXmlComposer()
    return composer.compose(
        workflow_json=workflow_json,
        workflow_name=workflow_name,
        pnumber=pnumber,
    )


def write_output_file(
    xml_content: Annotated[str, "TotalExport XML string"],
    workflow_name: Annotated[str, "Used to name the output file"],
) -> dict:
    """Writes the XML to the output directory. Returns the file path."""
    output_dir = os.getenv("OUTPUT_DIR", "/app/output")
    os.makedirs(output_dir, exist_ok=True)
    safe_name = generate_workflow_name(workflow_name)
    output_path = os.path.join(output_dir, f"{safe_name}.xml")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(xml_content)
    return {"output_file": output_path, "workflow_name": workflow_name}


def format_chat_response(
    validation_result: Annotated[dict, "Result from ValidationAgent"],
    composer_result: Annotated[dict, "Result from write_output_file"],
    placeholder_summary: Annotated[list, "List of PLACEHOLDERs and VERIFY notes"],
) -> str:
    """
    Builds the structured chat response returned to the user.
    Includes status, output file path, and all items needing review.
    """
    validation_result = _ensure_dict(validation_result)
    composer_result = _ensure_dict(composer_result)
    placeholder_summary = _ensure_list(placeholder_summary)

    if validation_result.get("status") == "invalid":
        errors = validation_result.get("errors", [])
        return (
            "STATUS: error\n\n"
            "The workflow could not be generated due to validation errors:\n"
            + "\n".join(f"  - {e}" for e in errors)
        )

    output_file = composer_result.get("output_file", "unknown")
    workflow_name = composer_result.get("workflow_name", "unknown")

    placeholders = [i for i in placeholder_summary if i.get("kind") == "placeholder"]
    verify_notes = [i for i in placeholder_summary if i.get("kind") == "verify"]

    if not placeholders and not verify_notes:
        status = "complete"
        status_note = "Workflow is complete and ready to import."
    else:
        status = "incomplete"
        status_note = (
            f"Workflow requires {len(placeholders)} value(s) and "
            f"{len(verify_notes)} manual step(s) before deployment."
        )

    lines = [
        f"STATUS: {status}",
        f"OUTPUT_FILE: {output_file}",
        f"WORKFLOW_NAME: {workflow_name}",
        "",
        status_note,
    ]

    if placeholders:
        lines.append("\nITEMS NEEDING VALUES:")
        for item in placeholders:
            lines.append(
                f"  - [{item['activity']}] {item['field']}: {item['placeholder']}"
            )

    if verify_notes:
        lines.append("\nMANUAL STEPS REQUIRED:")
        for item in verify_notes:
            msg = item.get("message", "")
            lines.append(f"  - [{item['activity']}] {msg[:120]}")

    verify_from_validation = validation_result.get("verify_notes", [])
    if verify_from_validation:
        lines.append("\nACTIVITIES WITH UNVERIFIED FIELD RULES:")
        for note in verify_from_validation:
            lines.append(f"  - {note[:120]}")

    lines.append(
        "\nTo import: open Resolve Actions → Workflows → Import → select the XML file above."
    )

    return "\n".join(lines)