"""
convert_to_xml.py  —  CLI wrapper around serializer.xml_composer.

Standalone CLI: convert a pipeline-output JSON file to TotalExport XML
for manual upload to the Resolve Actions sandbox.

This script is now a thin wrapper around convert_json_to_xml() in
serializer.xml_composer. The actual load + compose + two-layer validation
logic lives there so api.py's /convert-xml/{filename} endpoint can share
the exact same code path. CLI behavior is unchanged from the user's
perspective.

Usage:
    python convert_to_xml.py json_files/MyWorkflow_A3F9.json
    python convert_to_xml.py json_files/MyWorkflow_A3F9.json --output ./uploads/
    python convert_to_xml.py json_files/MyWorkflow_A3F9.json --dry-run

The script validates both layers of the produced XML before writing:
  1. Outer TotalExport wrapper  (xml.etree.ElementTree.fromstring)
  2. Inner Xoml string          (parsed separately)

Exit codes:
    0  — XML written successfully and both layers valid
    1  — Input file not found
    2  — JSON missing required fields
    3  — XML structural validation failed
"""

import argparse
import json
import pathlib
import sys

from serializer.xml_composer import (
    convert_json_to_xml,
    WorkflowJsonSchemaError,
    XmlValidationError,
    WorkflowXmlComposer,
)


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

    json_path = pathlib.Path(args.json_path)

    # ── Load + report metadata before invoking the composer ─────────────
    # Done here (rather than inside convert_json_to_xml) so the CLI can
    # print the workflow name + pnumber even if the composer later fails.
    if not json_path.exists():
        print(f"Error: file not found — {json_path}", file=sys.stderr)
        sys.exit(1)

    try:
        with open(json_path, encoding="utf-8") as f:
            workflow = json.load(f)
    except json.JSONDecodeError as e:
        print(f"Error: input JSON is not parseable — {e}", file=sys.stderr)
        sys.exit(2)

    name    = workflow.get("name")
    pnumber = workflow.get("pnumber")
    if not name or not pnumber:
        print(
            "Error: workflow JSON must contain 'name' and 'pnumber' fields.",
            file=sys.stderr,
        )
        sys.exit(2)

    print(f"Input:     {json_path}")
    print(f"Workflow:  {name}  │  Pnumber: {pnumber}")

    # ── Dry-run: compose + validate but do not write ─────────────────────
    # We re-implement the dry-run path here rather than adding a flag to
    # convert_json_to_xml() because the function's contract is "produce
    # a file." A dry-run is a CLI-only concept.
    if args.dry_run:
        composer   = WorkflowXmlComposer()
        xml_string = composer.compose(workflow, name, pnumber)
        from serializer.xml_composer import _validate_outer, _validate_xoml
        try:
            root = _validate_outer(xml_string)
            print("Validation [outer XML]:  VALID")
        except XmlValidationError as e:
            print(f"Validation [outer XML]:  INVALID — {e.parse_error}", file=sys.stderr)
            sys.exit(3)
        try:
            _validate_xoml(root)
            print("Validation [Xoml]:       VALID")
        except XmlValidationError as e:
            print(f"Validation [Xoml]:       INVALID — {e.parse_error}", file=sys.stderr)
            sys.exit(3)
        print("Dry run — no file written.")
        return

    # ── Real run: convert + write via the shared helper ──────────────────
    try:
        out_path = convert_json_to_xml(json_path, output_dir=args.output)
    except WorkflowJsonSchemaError as e:
        # Already covered above, but the function defends in depth too
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(2)
    except XmlValidationError as e:
        # Print which layer failed and the underlying parser message
        print(f"Validation [{e.layer}]:  INVALID — {e.parse_error}", file=sys.stderr)
        sys.exit(3)

    print("Validation [outer XML]:  VALID")
    print("Validation [Xoml]:       VALID")
    print(f"Written:   {out_path}")


if __name__ == "__main__":
    main()