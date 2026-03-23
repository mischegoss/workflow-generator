import json as _json
import os
from typing import Annotated
from serializer.xml_composer import WorkflowXmlComposer
from tools.build_tools import generate_pnumber, generate_workflow_name

# ── XML cache ─────────────────────────────────────────────────────────────────
# serialize_to_xml stores its output here so main.py can retrieve it directly
# without asking ComposerAgent to echo multi-kilobyte XML back inside a JSON
# string. That echo pattern reliably corrupts or truncates the model output.
_xml_cache: dict = {}   # {"xml": str, "workflow_name": str, "pnumber": str}


def get_cached_xml() -> dict:
    """Returns the last XML produced by serialize_to_xml, or empty dict if none."""
    return dict(_xml_cache)


def clear_xml_cache() -> None:
    """
    Clears the XML cache. Called at the start of each pipeline run to prevent
    a stale result from a previous (failed) run being returned as the current result.
    """
    global _xml_cache
    _xml_cache = {}


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


def _ensure_list_of_dicts(value) -> list:
    """
    Safely converts a list (or JSON string of a list) where individual items
    may themselves be JSON strings rather than dicts.

    ADK sometimes serializes list items to JSON strings when passing session
    state between pipeline stages — this unwraps them.
    """
    items = _ensure_list(value)
    result = []
    for item in items:
        if isinstance(item, dict):
            result.append(item)
        elif isinstance(item, str):
            try:
                parsed = _json.loads(item)
                if isinstance(parsed, dict):
                    result.append(parsed)
            except Exception:
                pass  # drop unparseable items silently
    return result


def serialize_to_xml(
    workflow_json: Annotated[dict, "Validated workflow JSON from session state"],
    workflow_name: Annotated[str, "Human-readable workflow name"],
    pnumber: Annotated[str, "Unique Pnumber for this workflow"],
) -> str:
    """
    Converts workflow JSON to a TotalExport XML string importable by Resolve Actions.
    The XML is stored in a module-level cache so main.py can retrieve it directly
    without requiring ComposerAgent to echo the full XML in its JSON output.
    Returns a short confirmation token to the agent instead of the full XML.
    """
    global _xml_cache
    workflow_json = _ensure_dict(workflow_json)
    composer = WorkflowXmlComposer()
    xml = composer.compose(
        workflow_json=workflow_json,
        workflow_name=workflow_name,
        pnumber=pnumber,
    )
    _xml_cache = {"xml": xml, "workflow_name": workflow_name, "pnumber": pnumber}
    # Return a short confirmation token — NOT the full XML.
    # The full XML is in _xml_cache; main.py reads it from get_cached_xml().
    return f"XML_SERIALIZED_OK:{len(xml)}_BYTES"


def format_chat_response(
    validation_result: Annotated[dict, "Result from ValidationAgent"],
    placeholder_summary: Annotated[list, "List of PLACEHOLDERs and VERIFY notes"],
) -> str:
    """
    Builds the structured chat response returned to the user.
    Includes status and all items needing review.
    File writing is handled outside the pipeline by test-pipeline.py.
    """
    validation_result = _ensure_dict(validation_result)

    # ADK sometimes serializes list items individually to JSON strings.
    # _ensure_list_of_dicts handles both the outer container and inner items.
    placeholder_summary = _ensure_list_of_dicts(placeholder_summary)

    if validation_result.get("status") == "invalid":
        errors = validation_result.get("errors", [])
        return (
            "STATUS: error\n\n"
            "The workflow could not be generated due to validation errors:\n"
            + "\n".join(f"  - {e}" for e in errors)
        )

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
        "\nTo import: open Resolve Actions → Workflows → Import → select the XML file."
    )

    return "\n".join(lines)
