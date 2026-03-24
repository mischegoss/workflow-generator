"""
convert_to_xml.py  —  root of project, no ADK dependency.

Standalone CLI: convert a pipeline-output JSON file to TotalExport XML
for manual upload to the Resolve Actions sandbox.

Usage:
    python convert_to_xml.py output/MyWorkflow_A3F9.json
    python convert_to_xml.py output/MyWorkflow_A3F9.json --output ./uploads/
    python convert_to_xml.py output/MyWorkflow_A3F9.json --dry-run

The script validates both layers of the produced XML before writing:
  1. Outer TotalExport wrapper  (xml.etree.ElementTree.fromstring)
  2. Inner Xoml string          (unescaped and parsed separately)

Exit codes:
    0  — XML written successfully and both layers valid
    1  — Input file not found
    2  — JSON missing required fields
    3  — XML structural validation failed
"""

import argparse
import html
import json
import pathlib
import sys
import xml.etree.ElementTree as ET

from serializer.xml_composer import WorkflowXmlComposer


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def _validate_outer(xml_string: str) -> ET.Element:
    """Parse TotalExport wrapper. Raises ET.ParseError on failure."""
    return ET.fromstring(xml_string)


def _validate_xoml(root: ET.Element) -> None:
    """
    Locate the Xoml attribute on WorkflowInfo, unescape, and parse.
    Raises ET.ParseError on failure.
    Prints a warning (non-fatal) if no WorkflowInfo element is found.
    """
    workflow_info = root.find(".//WorkflowInfo")
    if workflow_info is None:
        print("  [warn] No WorkflowInfo element found — skipping Xoml validation")
        return
    xoml = workflow_info.get("Xoml", "")
    if not xoml:
        print("  [warn] Xoml attribute is empty — skipping Xoml validation")
        return
    ET.fromstring(html.unescape(xoml))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert workflow JSON to TotalExport XML for Resolve Actions"
    )
    parser.add_argument("json_path", help="Path to workflow JSON file")
    parser.add_argument(
        "--output",
        default=None,
        help="Output directory (default: same directory as input file)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate only — do not write the XML file",
    )
    args = parser.parse_args()

    # ── 1. Load input JSON ──────────────────────────────────────────────────
    json_path = pathlib.Path(args.json_path)
    if not json_path.exists():
        print(f"Error: file not found — {json_path}", file=sys.stderr)
        sys.exit(1)

    with open(json_path, encoding="utf-8") as f:
        workflow = json.load(f)

    name = workflow.get("name")
    pnumber = workflow.get("pnumber")

    if not name or not pnumber:
        print(
            "Error: workflow JSON must contain 'name' and 'pnumber' fields.",
            file=sys.stderr,
        )
        sys.exit(2)

    print(f"Input:     {json_path}")
    print(f"Workflow:  {name}  │  Pnumber: {pnumber}")

    # ── 2. Compose XML ──────────────────────────────────────────────────────
    composer = WorkflowXmlComposer()
    xml_string = composer.compose(workflow, name, pnumber)

    # ── 3. Validate outer TotalExport wrapper ───────────────────────────────
    try:
        root = _validate_outer(xml_string)
        print("Validation [outer XML]:  VALID")
    except ET.ParseError as e:
        print(f"Validation [outer XML]:  INVALID — {e}", file=sys.stderr)
        sys.exit(3)

    # ── 4. Validate inner Xoml string ───────────────────────────────────────
    try:
        _validate_xoml(root)
        print("Validation [Xoml]:       VALID")
    except ET.ParseError as e:
        print(f"Validation [Xoml]:       INVALID — {e}", file=sys.stderr)
        sys.exit(3)

    # ── 5. Write output ─────────────────────────────────────────────────────
    if args.dry_run:
        print("Dry run — no file written.")
        return

    out_dir = pathlib.Path(args.output) if args.output else json_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / (json_path.stem + ".xml")

    out_path.write_text(xml_string, encoding="utf-8")
    print(f"Written:   {out_path}")


if __name__ == "__main__":
    main()