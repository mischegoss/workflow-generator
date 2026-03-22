import html
import xml.etree.ElementTree as ET
from typing import Annotated


def validate_xml_output(
    xml_string: Annotated[str, "TotalExport XML string returned by serialize_to_xml"],
) -> dict:
    """
    Validates the TotalExport XML string produced by the serializer.

    Two-stage validation:
    1. Parse the outer TotalExport structure as XML.
    2. Extract the Xoml attribute from WorkflowInfo, HTML-unescape it,
       and parse it independently as XML.

    Both must parse cleanly for the workflow to be importable.

    Returns:
        {"valid": True}
        {"valid": False, "error": "<parse error message>", "stage": "outer" | "xoml"}
    """
    if not xml_string or not isinstance(xml_string, str):
        return {"valid": False, "error": "xml_string is empty or not a string", "stage": "outer"}

    # Stage 1: parse outer TotalExport XML
    try:
        root = ET.fromstring(xml_string)
    except ET.ParseError as e:
        return {
            "valid": False,
            "error": f"Outer XML parse error: {e}",
            "stage": "outer",
        }

    # Stage 2: find WorkflowInfo and validate the inner Xoml string
    workflow_info = root.find(".//WorkflowInfo")
    if workflow_info is None:
        return {
            "valid": False,
            "error": "WorkflowInfo element not found in TotalExport XML",
            "stage": "outer",
        }

    xoml_escaped = workflow_info.get("Xoml", "")
    if not xoml_escaped:
        return {
            "valid": False,
            "error": "WorkflowInfo.Xoml attribute is empty",
            "stage": "xoml",
        }

    xoml_decoded = html.unescape(xoml_escaped)

    try:
        ET.fromstring(xoml_decoded)
    except ET.ParseError as e:
        return {
            "valid": False,
            "error": f"Inner Xoml XML parse error: {e}",
            "stage": "xoml",
        }

    return {"valid": True}